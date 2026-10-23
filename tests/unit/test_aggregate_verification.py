"""Exercise report validation and GitHub outputs without live services."""

import importlib.util
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from imednet.models.verification import VerificationReport

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "aggregate_verification.py"


@pytest.fixture
def aggregator(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("aggregate_verification", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "outputs.txt"))
    return module


def report(status="passed", violations=None):
    return {
        "version": "1.0",
        "track": "drift",
        "status": status,
        "violations": violations or [],
        "summary": "Synthetic verification result",
    }


def test_no_reports_publishes_explicit_empty_result(aggregator, tmp_path, capsys):
    aggregator.main()
    assert "No verification reports found" in capsys.readouterr().out
    assert (tmp_path / "outputs.txt").read_text() == "drift_detected=false\ndrift_count=0\n"


@pytest.mark.parametrize("failed", [False, True])
def test_aggregates_valid_reports_and_metrics(aggregator, tmp_path, failed):
    data = report(
        "failed" if failed else "passed",
        [{"violation_type": "additive", "message": "Unexpected field"}] if failed else [],
    )
    data["metrics"] = [{"name": "duration", "value": 1.25, "unit": "seconds"}]
    (tmp_path / "drift_report.json").write_text(json.dumps(data))
    aggregator.main()
    expected = (
        "drift_detected=true\ndrift_count=1\n"
        if failed
        else "drift_detected=false\ndrift_count=0\n"
    )
    assert (tmp_path / "outputs.txt").read_text() == expected
    if failed:
        assert (
            "additive in track `drift`: Unexpected field"
            in (tmp_path / "drift_report.txt").read_text()
        )


@pytest.mark.parametrize("contents", ['{', '{"status":"passed"}'])
def test_malformed_report_fails_closed(aggregator, tmp_path, contents):
    (tmp_path / "invalid_report.json").write_text(contents)
    with pytest.raises(SystemExit) as exc:
        aggregator.main()
    assert exc.value.code == 1
    assert not (tmp_path / "outputs.txt").exists()


def test_failed_report_without_violations_is_still_failed(aggregator, tmp_path):
    (tmp_path / "failed_report.json").write_text(json.dumps(report("failed")))
    aggregator.main()
    assert (tmp_path / "outputs.txt").read_text() == "drift_detected=true\ndrift_count=0\n"


@pytest.mark.parametrize("field,value", [("version", "2.0"), ("track", "typo"), ("status", "typo")])
def test_report_rejects_unsupported_contract_values(field, value):
    data = report()
    data[field] = value
    with pytest.raises(ValidationError):
        VerificationReport.model_validate(data)


def test_aggregation_without_github_outputs(aggregator, tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT")
    (tmp_path / "valid_report.json").write_text(json.dumps(report()))
    aggregator.main()
    assert not (tmp_path / "outputs.txt").exists()
