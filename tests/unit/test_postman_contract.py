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


# ---------------------------------------------------------------------------
# Offline httpx.MockTransport SDK Contract Suite (all 15 Postman operations)
# ---------------------------------------------------------------------------


class UnbackedPostmanRouteError(AssertionError):
    """Raised when a test attempts to mock or request an endpoint without Postman backing."""


def _materialize_postman_placeholders(value: Any, *, key: str | None = None) -> Any:
    """Replace only `<integer>` and `<boolean>` placeholders while keeping keys/values verbatim."""
    if isinstance(value, dict):
        return {k: _materialize_postman_placeholders(v, key=k) for k, v in value.items()}
    if isinstance(value, list):
        return [_materialize_postman_placeholders(item, key=key) for item in value]
    if value == "<integer>":
        return 0 if key == "currentPage" else 1
    if value == "<boolean>":
        return False
    return value


class PostmanContractRouter:
    """Strict `httpx.MockTransport` router backed exclusively by `imednet.postman_collection.json`."""

    def __init__(self, operations: list[dict[str, Any]] | None = None) -> None:
        import re

        if operations is None:
            collection = json.loads(COLLECTION.read_text(encoding="utf-8"))
            operations = get_collection_operations(collection.get("item", []))

        if not operations:
            raise UnbackedPostmanRouteError(
                "Cannot initialize MockTransport without Postman operations"
            )

        self.routes: list[dict[str, Any]] = []
        self.request_bodies: dict[str, Any] = {}
        self.matched_operations: list[str] = []

        for op in operations:
            name = op["name"]
            req = op.get("request", {})
            method = req.get("method", "").upper()
            url_obj = req.get("url", {})
            raw_segments = url_obj.get("path", []) if isinstance(url_obj, dict) else []
            responses = op.get("response", [])
            if not method or not raw_segments or not responses or not responses[0].get("body"):
                raise UnbackedPostmanRouteError(
                    f"Operation '{name}' is missing request path or backed response payload"
                )

            regex_segments = [
                "[^/]+" if seg.startswith(":") else re.escape(seg) for seg in raw_segments
            ]
            pattern = re.compile(r"^/api/v1/edc/" + "/".join(regex_segments) + r"$")
            raw_body = json.loads(responses[0]["body"])
            materialized_body = _materialize_postman_placeholders(raw_body)
            status_code = int(responses[0].get("code", 200))

            req_body_raw = req.get("body", {}).get("raw")
            if req_body_raw:
                self.request_bodies[name] = _materialize_postman_placeholders(
                    json.loads(req_body_raw)
                )

            self.routes.append(
                {
                    "name": name,
                    "method": method,
                    "pattern": pattern,
                    "status": status_code,
                    "body": materialized_body,
                    "scripts": extract_test_scripts(op),
                }
            )

    def handle_request(self, request: Any) -> Any:
        import httpx

        method = request.method.upper()
        path = request.url.path
        for route in self.routes:
            if route["method"] == method and route["pattern"].match(path):
                self.matched_operations.append(route["name"])
                return httpx.Response(status_code=route["status"], json=route["body"])
        raise UnbackedPostmanRouteError(
            f"Unbacked HTTP request {method} {path}: no matching operation in imednet.postman_collection.json"
        )

    def transport(self) -> Any:
        import httpx

        return httpx.MockTransport(self.handle_request)


def _build_sdk_operation_matrix() -> list[tuple[str, Any, Any, Any]]:
    """Map all 15 Postman operations to sync SDK callables, async SDK callables, and validators."""
    from imednet.models.codings import Coding
    from imednet.models.forms import Form
    from imednet.models.intervals import Interval
    from imednet.models.jobs import Job, JobState, JobStatus
    from imednet.models.queries import Query
    from imednet.models.record_revisions import RecordRevision
    from imednet.models.records import Record
    from imednet.models.sites import Site
    from imednet.models.studies import Study
    from imednet.models.subjects import Subject, SubjectKeyword
    from imednet.models.users import User
    from imednet.models.variables import Variable
    from imednet.models.visits import Visit

    def _expect_nonempty_list(model_cls: type[Any]) -> Any:
        def _check(res: Any) -> None:
            assert isinstance(res, list)
            assert len(res) >= 1
            assert all(isinstance(item, model_cls) for item in res)

        return _check

    def _check_subjects(res: Any) -> None:
        _expect_nonempty_list(Subject)(res)
        assert len(res[0].keywords) == 1
        assert isinstance(res[0].keywords[0], SubjectKeyword)
        assert res[0].keywords[0].keyword_id == 1

    def _check_study(res: Any) -> None:
        assert isinstance(res, Study)
        assert res.study_key == "<string>"

    def _check_job_created(res: Any) -> None:
        assert isinstance(res, Job)
        assert res.state == JobState.OPEN

    def _check_job_status(res: Any) -> None:
        assert isinstance(res, JobStatus)
        assert res.state == JobState.COMPLETED
        assert res.progress == 100

    return [
        (
            "List studies",
            lambda sdk, _r: sdk.get_studies(),
            lambda sdk, _r: sdk.async_get_studies(),
            _expect_nonempty_list(Study),
        ),
        (
            "Retrieve a study",
            lambda sdk, _r: sdk.studies.get("STUDY_1"),
            lambda sdk, _r: sdk.studies.get("STUDY_1"),
            _check_study,
        ),
        (
            "List forms",
            lambda sdk, _r: sdk.get_forms("STUDY_1"),
            lambda sdk, _r: sdk.async_get_forms("STUDY_1"),
            _expect_nonempty_list(Form),
        ),
        (
            "List variables",
            lambda sdk, _r: sdk.get_variables("STUDY_1"),
            lambda sdk, _r: sdk.async_get_variables("STUDY_1"),
            _expect_nonempty_list(Variable),
        ),
        (
            "List intervals",
            lambda sdk, _r: sdk.get_intervals("STUDY_1"),
            lambda sdk, _r: sdk.async_get_intervals("STUDY_1"),
            _expect_nonempty_list(Interval),
        ),
        (
            "List sites",
            lambda sdk, _r: sdk.get_sites("STUDY_1"),
            lambda sdk, _r: sdk.async_get_sites("STUDY_1"),
            _expect_nonempty_list(Site),
        ),
        (
            "List subjects",
            lambda sdk, _r: sdk.get_subjects("STUDY_1"),
            lambda sdk, _r: sdk.async_get_subjects("STUDY_1"),
            _check_subjects,
        ),
        (
            "List records",
            lambda sdk, _r: sdk.get_records("STUDY_1"),
            lambda sdk, _r: sdk.async_get_records("STUDY_1"),
            _expect_nonempty_list(Record),
        ),
        (
            "Create records",
            lambda sdk, r: sdk.create_record("STUDY_1", r.request_bodies["Create records"]),
            lambda sdk, r: sdk.async_create_record("STUDY_1", r.request_bodies["Create records"]),
            _check_job_created,
        ),
        (
            "List record revisions",
            lambda sdk, _r: sdk.get_record_revisions("STUDY_1"),
            lambda sdk, _r: sdk.async_get_record_revisions("STUDY_1"),
            _expect_nonempty_list(RecordRevision),
        ),
        (
            "List codings",
            lambda sdk, _r: sdk.get_codings("STUDY_1"),
            lambda sdk, _r: sdk.async_get_codings("STUDY_1"),
            _expect_nonempty_list(Coding),
        ),
        (
            "List queries",
            lambda sdk, _r: sdk.get_queries("STUDY_1"),
            lambda sdk, _r: sdk.async_get_queries("STUDY_1"),
            _expect_nonempty_list(Query),
        ),
        (
            "List visits",
            lambda sdk, _r: sdk.get_visits("STUDY_1"),
            lambda sdk, _r: sdk.async_get_visits("STUDY_1"),
            _expect_nonempty_list(Visit),
        ),
        (
            "List users",
            lambda sdk, _r: sdk.get_users("STUDY_1"),
            lambda sdk, _r: sdk.async_get_users("STUDY_1"),
            _expect_nonempty_list(User),
        ),
        (
            "Retrieve job status",
            lambda sdk, _r: sdk.get_job("STUDY_1", "BATCH_1"),
            lambda sdk, _r: sdk.async_get_job("STUDY_1", "BATCH_1"),
            _check_job_status,
        ),
    ]


SDK_OPERATION_MATRIX = _build_sdk_operation_matrix()


@pytest.mark.parametrize(
    "op_name,sync_call,async_call,validator",
    SDK_OPERATION_MATRIX,
    ids=[row[0] for row in SDK_OPERATION_MATRIX],
)
def test_sync_sdk_executes_postman_operation_via_mock_transport(
    op_name: str,
    sync_call: Any,
    async_call: Any,
    validator: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Execute each of the 15 Postman operations through ImednetSDK with IMEDNET_STRICT_MODE=1."""
    from imednet import ImednetSDK

    del async_call
    monkeypatch.setenv("IMEDNET_STRICT_MODE", "1")
    router = PostmanContractRouter()
    with ImednetSDK(
        api_key="k", security_key="s", strict_mode=True, transport=router.transport()
    ) as sdk:
        result = sync_call(sdk, router)

    validator(result)
    assert router.matched_operations == [op_name]


@pytest.mark.parametrize(
    "op_name,sync_call,async_call,validator",
    SDK_OPERATION_MATRIX,
    ids=[row[0] for row in SDK_OPERATION_MATRIX],
)
async def test_async_sdk_executes_postman_operation_via_mock_transport(
    op_name: str,
    sync_call: Any,
    async_call: Any,
    validator: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Execute each of the 15 Postman operations through AsyncImednetSDK with IMEDNET_STRICT_MODE=1."""
    from imednet import AsyncImednetSDK

    del sync_call
    monkeypatch.setenv("IMEDNET_STRICT_MODE", "1")
    router = PostmanContractRouter()
    async with AsyncImednetSDK(
        api_key="k",
        security_key="s",
        strict_mode=True,
        transport=router.transport(),
    ) as sdk:
        result = await async_call(sdk, router)

    validator(result)
    assert router.matched_operations == [op_name]


async def test_sdk_matrix_covers_all_fifteen_postman_operations_and_poll_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify full 15-operation coverage and job polling across sync and async SDKs."""
    from imednet import AsyncImednetSDK, ImednetSDK
    from imednet.models.jobs import JobState

    monkeypatch.setenv("IMEDNET_STRICT_MODE", "1")
    collection = json.loads(COLLECTION.read_text(encoding="utf-8"))
    expected_names = {op["name"] for op in get_collection_operations(collection.get("item", []))}
    assert len(expected_names) == 15
    assert {row[0] for row in SDK_OPERATION_MATRIX} == expected_names

    sync_router = PostmanContractRouter()
    with ImednetSDK(
        api_key="k",
        security_key="s",
        strict_mode=True,
        transport=sync_router.transport(),
    ) as sdk:
        for _name, sync_call, _async_call, validator in SDK_OPERATION_MATRIX:
            validator(sync_call(sdk, sync_router))
        polled = sdk.poll_job("STUDY_1", "BATCH_1", interval=0.01, timeout=1.0)
        assert polled.state == JobState.COMPLETED
    assert set(sync_router.matched_operations) == expected_names

    async_router = PostmanContractRouter()
    async with AsyncImednetSDK(
        api_key="k",
        security_key="s",
        strict_mode=True,
        transport=async_router.transport(),
    ) as async_sdk:
        for _name, _sync_call, async_call, validator in SDK_OPERATION_MATRIX:
            validator(await async_call(async_sdk, async_router))
        async_polled = await async_sdk.async_poll_job(
            "STUDY_1", "BATCH_1", interval=0.01, timeout=1.0
        )
        assert async_polled.state == JobState.COMPLETED
    assert set(async_router.matched_operations) == expected_names


async def test_postman_mock_transport_rejects_unbacked_routes_and_synthetic_mocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject HTTP routes or synthetic mock definitions lacking Postman collection backing."""
    from pydantic import ValidationError

    from imednet import AsyncImednetSDK, ImednetSDK

    monkeypatch.setenv("IMEDNET_STRICT_MODE", "1")
    router = PostmanContractRouter()

    # Unbacked HTTP route (Postman has no DELETE or /studies/{studyKey}/nonexistent endpoint)
    with ImednetSDK(
        api_key="k",
        security_key="s",
        strict_mode=True,
        transport=router.transport(),
    ) as sdk:
        with pytest.raises(UnbackedPostmanRouteError, match="Unbacked HTTP request"):
            sdk._client.get("/api/v1/edc/studies/STUDY_1/unbackedResource")

    async with AsyncImednetSDK(
        api_key="k",
        security_key="s",
        strict_mode=True,
        transport=router.transport(),
    ) as async_sdk:
        with pytest.raises(UnbackedPostmanRouteError, match="Unbacked HTTP request"):
            await async_sdk._async_client.get("/api/v1/edc/studies/STUDY_1/unbackedResource")

    # Synthetic operation without Postman response backing must be rejected at router construction
    with pytest.raises(UnbackedPostmanRouteError):
        PostmanContractRouter(
            operations=[
                {
                    "name": "Synthetic unbacked operation",
                    "request": {"method": "GET", "url": {"path": ["studies"]}},
                    "response": [],
                }
            ]
        )

    # Confirm IMEDNET_STRICT_MODE=1 actively rejects undocumented additive fields on MockTransport
    drifted_router = PostmanContractRouter()
    for route in drifted_router.routes:
        if route["name"] == "Retrieve job status":
            route["body"] = {**route["body"], "undocumentedSyntheticField": "drift"}
    with ImednetSDK(
        api_key="k",
        security_key="s",
        strict_mode=True,
        transport=drifted_router.transport(),
    ) as sdk:
        with pytest.raises(ValidationError, match="undocumentedSyntheticField"):
            sdk.get_job("STUDY_1", "BATCH_1")
