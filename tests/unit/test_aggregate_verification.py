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


def test_no_reports_publishes_explicit_empty_result_and_exits_nonzero(aggregator, tmp_path, capsys):
    report_dir = tmp_path / "reports" / "verification"
    with pytest.raises(SystemExit) as exc:
        aggregator.main(["--report-dir", str(report_dir)])
    assert exc.value.code == 1
    assert "No verification reports found" in capsys.readouterr().out
    assert (tmp_path / "outputs.txt").read_text() == "drift_detected=false\ndrift_count=0\n"


def test_missing_expected_track_exits_nonzero(aggregator, tmp_path, capsys):
    report_dir = tmp_path / "reports" / "verification"
    report_dir.mkdir(parents=True)
    perf_report = report("passed")
    perf_report["track"] = "performance"
    (report_dir / "performance_report.json").write_text(json.dumps(perf_report))

    with pytest.raises(SystemExit) as exc:
        aggregator.main(["--report-dir", str(report_dir), "--expect-track", "drift"])
    assert exc.value.code == 1
    assert "Missing expected verification track(s): drift" in capsys.readouterr().out
    assert (tmp_path / "outputs.txt").read_text() == "drift_detected=false\ndrift_count=0\n"


@pytest.mark.parametrize("failed", [False, True])
def test_aggregates_valid_reports_and_metrics(aggregator, tmp_path, failed):
    report_dir = tmp_path / "reports" / "verification"
    report_dir.mkdir(parents=True)
    data = report(
        "failed" if failed else "passed",
        [{"violation_type": "additive", "message": "Unexpected field"}] if failed else [],
    )
    data["metrics"] = [{"name": "duration", "value": 1.25, "unit": "seconds"}]
    (report_dir / "drift_report.json").write_text(json.dumps(data))

    if failed:
        with pytest.raises(SystemExit) as exc:
            aggregator.main(["--report-dir", str(report_dir), "--expect-track", "drift"])
        assert exc.value.code == 1
    else:
        aggregator.main(["--report-dir", str(report_dir), "--expect-track", "drift"])

    expected = (
        "drift_detected=true\ndrift_count=1\n"
        if failed
        else "drift_detected=false\ndrift_count=0\n"
    )
    assert (tmp_path / "outputs.txt").read_text() == expected
    if failed:
        assert (
            "additive in track `drift`: Unexpected field"
            in (report_dir / "drift_report.txt").read_text()
        )


@pytest.mark.parametrize("contents", ['{', '{"status":"passed"}'])
def test_malformed_report_fails_closed(aggregator, tmp_path, contents):
    report_dir = tmp_path / "reports" / "verification"
    report_dir.mkdir(parents=True)
    (report_dir / "invalid_report.json").write_text(contents)
    with pytest.raises(SystemExit) as exc:
        aggregator.main(["--report-dir", str(report_dir)])
    assert exc.value.code == 1
    assert not (tmp_path / "outputs.txt").exists()


def test_failed_report_without_violations_still_exits_nonzero(aggregator, tmp_path):
    report_dir = tmp_path / "reports" / "verification"
    report_dir.mkdir(parents=True)
    (report_dir / "failed_report.json").write_text(json.dumps(report("failed")))
    with pytest.raises(SystemExit) as exc:
        aggregator.main(["--report-dir", str(report_dir), "--expect-track", "drift"])
    assert exc.value.code == 1
    assert (tmp_path / "outputs.txt").read_text() == "drift_detected=true\ndrift_count=0\n"


@pytest.mark.parametrize("field,value", [("version", "2.0"), ("track", "typo"), ("status", "typo")])
def test_report_rejects_unsupported_contract_values(field, value):
    data = report()
    data[field] = value
    with pytest.raises(ValidationError):
        VerificationReport.model_validate(data)


def test_aggregation_without_github_outputs(aggregator, tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT")
    report_dir = tmp_path / "reports" / "verification"
    report_dir.mkdir(parents=True)
    (report_dir / "valid_report.json").write_text(json.dumps(report()))
    aggregator.main()
    assert not (tmp_path / "outputs.txt").exists()
