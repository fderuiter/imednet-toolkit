"""Guard the boundary between scheduled offline CI and approved live validation."""

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def test_scheduled_workflows_do_not_use_live_edc_credentials() -> None:
    """Reject unattended EDC validation while allowing offline scheduled checks."""
    for path in (*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")):
        source = path.read_text(encoding="utf-8")
        workflow = yaml.load(source, Loader=yaml.BaseLoader)
        if "schedule" in workflow.get("on", {}):
            assert "secrets.IMEDNET_API_KEY" not in source, path.name
            assert "secrets.IMEDNET_SECURITY_KEY" not in source, path.name
            assert "IMEDNET_RUN_E2E" not in source, path.name


def test_live_validation_requires_manual_approval() -> None:
    """Keep the remaining live suite explicit and separately gate mutations."""
    workflow = yaml.load(
        (WORKFLOWS / "live-tests.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader
    )
    assert set(workflow["on"]) == {"workflow_dispatch"}
    approval = workflow["on"]["workflow_dispatch"]["inputs"]["approved"]
    assert approval["type"] == "boolean"
    assert approval["required"] == "true"
    assert approval["default"] == "false"
    job = workflow["jobs"]["live-validation"]
    assert job["if"] == "${{ github.event_name == 'workflow_dispatch' && inputs.approved }}"
    assert job["environment"] == "live-testing"
    suite = next(step for step in job["steps"] if step.get("id") == "live_tests")
    assert suite["env"]["IMEDNET_RUN_E2E"] == "1"
    assert suite["env"]["IMEDNET_ALLOW_MUTATION"] == "${{ vars.IMEDNET_ALLOW_MUTATION }}"
    assert "pytest tests/live/" in suite["run"]


def test_retired_smoke_helpers_are_not_executable_workflow_steps() -> None:
    """Prevent an obsolete smoke pre-step or failure-issue loop from returning."""
    assert not (WORKFLOWS / "smoke.yml").exists()
    assert not (WORKFLOWS.parents[1] / "scripts" / "post_smoke_record.py").exists()
    for path in (*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")):
        source = path.read_text(encoding="utf-8")
        assert "post_smoke_record.py" not in source, path.name
        assert "Scheduled Smoke Test Failed" not in source, path.name
