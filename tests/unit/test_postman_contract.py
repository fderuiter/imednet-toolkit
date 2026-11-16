"""Execute the Postman contract assertions against synthetic responses without HTTP."""

from __future__ import annotations

import atexit
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

COLLECTION = Path(__file__).resolve().parents[2] / "imednet.postman_collection.json"
NODE = shutil.which("node")

HARNESS_TEMPLATE = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');

function makeExpect(val) {
    return {
        to: {
            be: {
                an: (type) => {
                    if (type === 'array') {
                        assert.ok(Array.isArray(val), 'expected array');
                    } else if (type === 'object') {
                        assert.ok(typeof val === 'object' && val !== null && !Array.isArray(val), 'expected object');
                    } else {
                        assert.equal(typeof val, type);
                    }
                },
                a: (type) => {
                    if (type === 'array') {
                        assert.ok(Array.isArray(val), 'expected array');
                    } else if (type === 'object') {
                        assert.ok(typeof val === 'object' && val !== null && !Array.isArray(val), 'expected object');
                    } else {
                        assert.equal(typeof val, type);
                    }
                }
            },
            have: {
                property: (prop) => {
                    assert.ok(typeof val === 'object' && val !== null, 'expected object');
                    assert.ok(prop in val, 'missing property: ' + prop);
                }
            }
        }
    };
}

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
let failures = 0;
let tests = 0;
const pm = {
    test: (_name, fn) => { tests++; try { fn(); } catch (_) { failures++; } },
    response: {
        to: { have: { status: code => assert.equal(input.status, code) } },
        json: () => input.body
    },
    expect: makeExpect
};
vm.runInNewContext(input.scripts, {pm});
process.stdout.write(JSON.stringify({tests, failures}));
"""

HARNESS_WORKER = r"""
const readline = require('node:readline');
const vm = require('node:vm');
const assert = require('node:assert/strict');

function makeExpect(val) {
    return {
        to: {
            be: {
                an: (type) => {
                    if (type === 'array') {
                        assert.ok(Array.isArray(val), 'expected array');
                    } else if (type === 'object') {
                        assert.ok(typeof val === 'object' && val !== null && !Array.isArray(val), 'expected object');
                    } else {
                        assert.equal(typeof val, type);
                    }
                },
                a: (type) => {
                    if (type === 'array') {
                        assert.ok(Array.isArray(val), 'expected array');
                    } else if (type === 'object') {
                        assert.ok(typeof val === 'object' && val !== null && !Array.isArray(val), 'expected object');
                    } else {
                        assert.equal(typeof val, type);
                    }
                }
            },
            have: {
                property: (prop) => {
                    assert.ok(typeof val === 'object' && val !== null, 'expected object');
                    assert.ok(prop in val, 'missing property: ' + prop);
                }
            }
        }
    };
}

const rl = readline.createInterface({ input: process.stdin, terminal: false });
rl.on('line', (line) => {
    if (!line.trim()) return;
    const input = JSON.parse(line);
    let failures = 0;
    let tests = 0;
    const pm = {
        test: (_name, fn) => { tests++; try { fn(); } catch (_) { failures++; } },
        response: {
            to: { have: { status: code => assert.equal(input.status, code) } },
            json: () => input.body
        },
        expect: makeExpect
    };
    try {
        vm.runInNewContext(input.scripts, {pm});
    } catch (_) {
        failures++;
    }
    console.log(JSON.stringify({tests, failures}));
});
"""


class NodeWorker:
    """Persistent Node.js worker for fast offline contract evaluation."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen[str] | None = None
        self._start()

    def _start(self) -> None:
        if NODE is not None:
            self.proc = subprocess.Popen(
                [NODE, "-e", HARNESS_WORKER],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

    def evaluate(self, scripts: str, status: int, body: Any) -> dict[str, int]:
        if self.proc is None or self.proc.poll() is not None:
            self._start()
        if self.proc is None or self.proc.stdin is None or self.proc.stdout is None:
            # Fallback to single subprocess invocation
            res = subprocess.run(
                [NODE, "-e", HARNESS_TEMPLATE],  # type: ignore[list-item]
                input=json.dumps({"scripts": scripts, "status": status, "body": body}),
                text=True,
                capture_output=True,
                check=True,
                timeout=10,
            )
            return json.loads(res.stdout)  # type: ignore[no-any-return]

        payload = json.dumps({"scripts": scripts, "status": status, "body": body})
        try:
            self.proc.stdin.write(payload + "\n")
            self.proc.stdin.flush()
            output = self.proc.stdout.readline()
            return json.loads(output)  # type: ignore[no-any-return]
        except (BrokenPipeError, OSError, json.JSONDecodeError):
            self.close()
            self._start()
            res = subprocess.run(
                [NODE, "-e", HARNESS_TEMPLATE],  # type: ignore[list-item]
                input=payload,
                text=True,
                capture_output=True,
                check=True,
                timeout=10,
            )
            return json.loads(res.stdout)  # type: ignore[no-any-return]

    def close(self) -> None:
        if self.proc is not None:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.kill()
            finally:
                self.proc = None


_WORKER = NodeWorker() if NODE is not None else None


def _cleanup_worker() -> None:
    if _WORKER is not None:
        _WORKER.close()


atexit.register(_cleanup_worker)


def get_collection_operations(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Traverse all collection items recursively to collect API operations."""
    operations: list[dict[str, Any]] = []
    for item in items:
        if "item" in item:
            operations.extend(get_collection_operations(item["item"]))
        elif "request" in item:
            operations.append(item)
    return operations


def extract_test_scripts(operation: dict[str, Any]) -> str:
    """Extract concatenated JavaScript test scripts from an operation item."""
    return "\n".join(
        "\n".join(event["script"]["exec"])
        for event in operation.get("event", [])
        if event.get("listen") == "test"
    )


def build_contract_test_cases() -> list[tuple[str, str, int, Any, int, str]]:
    """Build parameterized test cases for all operations in the collection.

    Returns tuples of (operation_name, scripts, status, body, expected_failures, case_type).
    """
    if not COLLECTION.exists():
        return []

    collection = json.loads(COLLECTION.read_text())
    operations = get_collection_operations(collection.get("item", []))
    cases: list[tuple[str, str, int, Any, int, str]] = []

    for op in operations:
        name = op["name"]
        scripts = extract_test_scripts(op)
        responses = op.get("response", [])
        expected_status = responses[0].get("code", 200) if responses else 200

        # Valid synthetic payload from contract response example or standard schema
        if responses and responses[0].get("body"):
            try:
                valid_body = json.loads(responses[0]["body"])
            except json.JSONDecodeError:
                valid_body = {"data": []}
        elif name.startswith("List "):
            valid_body = {"data": []}
        elif "study" in name.lower():
            valid_body = {"studyKey": "study-key"}
        else:
            valid_body = {"jobId": "job-1"}

        # 1. Valid response: correct status and schema -> 0 failures
        cases.append((name, scripts, expected_status, valid_body, 0, "valid"))

        # 2. Forbidden / mismatched status code -> 1 failure
        mismatched_status = 403 if expected_status != 403 else 500
        cases.append((name, scripts, mismatched_status, valid_body, 1, "invalid_status"))

        # 3. Invalid schema / missing required payload keys -> 1 failure
        cases.append((name, scripts, expected_status, {"error": "failure"}, 1, "invalid_body"))

    return cases


CONTRACT_CASES = build_contract_test_cases()


def test_postman_collection_discovers_all_fifteen_operations() -> None:
    """Verify recursive traversal discovers all 15 operations with contract assertion scripts."""
    collection = json.loads(COLLECTION.read_text())
    operations = get_collection_operations(collection.get("item", []))
    assert len(operations) == 15, f"Expected 15 operations, found {len(operations)}"

    for op in operations:
        scripts = extract_test_scripts(op)
        assert scripts.strip(), f"Operation '{op['name']}' missing test assertion script"
        assert "pm.response.to.have.status" in scripts, (
            f"Operation '{op['name']}' missing status code assertion"
        )
        assert "pm.expect" in scripts, f"Operation '{op['name']}' missing payload assertion"


@pytest.mark.skipif(NODE is None, reason="Node.js is required to execute Postman assertions")
@pytest.mark.parametrize(
    "op_name,scripts,status,body,expected_failures,case_type",
    CONTRACT_CASES,
    ids=[f"{c[0]}-{c[5]}" for c in CONTRACT_CASES],
)
def test_postman_assertions_reject_forbidden_or_invalid_responses(
    op_name: str,
    scripts: str,
    status: int,
    body: Any,
    expected_failures: int,
    case_type: str,
) -> None:
    """Verify Postman assertions across all 15 operations for valid and invalid synthetic responses."""
    assert _WORKER is not None
    result = _WORKER.evaluate(scripts, status, body)
    assert result == {"tests": 2, "failures": expected_failures}, (
        f"Operation '{op_name}' failed assertion check for scenario '{case_type}'"
    )
