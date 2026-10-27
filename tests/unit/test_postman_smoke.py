"""Execute the Postman smoke assertions against synthetic responses without HTTP."""

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

COLLECTION = Path(__file__).resolve().parents[2] / "imednet.postman_collection.json"
NODE = shutil.which("node")
BASH = shutil.which("bash")


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


@pytest.mark.skipif(BASH is None, reason="Bash is required to execute the smoke workflow command")
@pytest.mark.parametrize("host", ["https://edc.prod.imednetapi.com", "https://sandbox.example"])
@pytest.mark.parametrize("trailing_slash", ["", "/"])
@pytest.mark.parametrize("exit_code", [0, 1])
def test_workflow_targets_edc_api_and_preserves_newman_failure(
    tmp_path, monkeypatch, host, trailing_slash, exit_code
):
    workflow = (COLLECTION.parent / ".github/workflows/smoke.yml").read_text()
    step = workflow.split("      - name: Run Postman tests\n", 1)[1]
    command = textwrap.dedent(step.split("        run: |\n", 1)[1].split("      - name:", 1)[0])
    captured = tmp_path / "newman-arguments.txt"
    monkeypatch.setenv("CAPTURE_PATH", str(captured))
    monkeypatch.setenv("BASE_URL", host + trailing_slash)
    monkeypatch.setenv("API_KEY", "synthetic-api")
    monkeypatch.setenv("SECURITY_KEY", "synthetic-security")
    monkeypatch.setenv("NEWMAN_EXIT_CODE", str(exit_code))
    # Capture the actual shell-expanded invocation without making HTTP requests.
    stub = 'newman() { printf "%s\\n" "$@" > "$CAPTURE_PATH"; return "$NEWMAN_EXIT_CODE"; }\n'
    result = subprocess.run(
        [BASH, "-e", "-c", stub + command],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == exit_code
    args = captured.read_text().splitlines()
    assert args[:4] == ["run", "imednet.postman_collection.json", "--folder", "List studies"]
    variables = dict(
        args[index + 1].split("=", 1) for index, value in enumerate(args) if value == "--env-var"
    )
    studies = json.loads(COLLECTION.read_text())["item"][0]["item"][0]
    request_url = studies["request"]["url"]
    resolved_host = ".".join(request_url["host"]).replace("{{baseUrl}}", variables["baseUrl"])
    assert resolved_host + "/" + "/".join(request_url["path"]) == host + "/api/v1/edc/studies"
    assert variables["api_key"] == "synthetic-api"
    assert variables["security_key"] == "synthetic-security"
