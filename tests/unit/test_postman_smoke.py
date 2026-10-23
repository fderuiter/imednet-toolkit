"""Execute the Postman smoke assertions against synthetic responses without HTTP."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

COLLECTION = Path(__file__).resolve().parents[2] / "imednet.postman_collection.json"
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node.js is required to execute Postman assertions")
@pytest.mark.parametrize(
    "status,body,failures",
    [(200, {"data": []}, 0), (403, {"data": []}, 1), (200, {"error": "failure"}, 1)],
)
def test_smoke_assertions_reject_forbidden_or_invalid_responses(status, body, failures):
    collection = json.loads(COLLECTION.read_text())
    studies = collection["item"][0]["item"][0]
    assert studies["name"] == "List studies"
    scripts = "\n".join(
        "\n".join(event["script"]["exec"])
        for event in studies["event"]
        if event["listen"] == "test"
    )
    harness = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
let failures = 0;
let tests = 0;
const pm = {
    test: (_name, fn) => { tests++; try { fn(); } catch (_) { failures++; } },
    response: {
        to: { have: { status: code => assert.equal(input.status, code) } },
        json: () => input.body
    },
    expect: value => ({ to: { be: { an: type => {
        assert.equal(type, 'array'); assert.ok(Array.isArray(value));
    } } } })
};
vm.runInNewContext(input.scripts, {pm});
process.stdout.write(JSON.stringify({tests, failures}));
"""
    result = subprocess.run(
        [NODE, "-e", harness],
        input=json.dumps({"scripts": scripts, "status": status, "body": body}),
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    assert json.loads(result.stdout) == {"tests": 2, "failures": failures}
