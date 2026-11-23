"""Unit tests for shared VerificationReporter, baselines, drift collection, and CI workflows."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from tests.core import test_performance
from tests.live.test_drift_postman import test_postman_collection_drift as run_drift_check
from tests.utils.verification_reporter import VerificationReporter

from imednet.models.verification import VerificationReport

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


def test_verification_reporter_emits_report_even_on_assertion_failure(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="boom"):
        with VerificationReporter("validation", report_dir=tmp_path) as reporter:
            reporter.add_metric("items_checked", 42.0, unit="count")
            assert False, "boom"

    report_path = tmp_path / "validation_report.json"
    assert report_path.exists()
    parsed = VerificationReport.model_validate_json(report_path.read_text(encoding="utf-8"))
    assert parsed.status == "failed"
    assert parsed.track == "validation"
    assert len(parsed.violations) == 1
    assert parsed.violations[0].violation_type == "AssertionError"
    assert "boom" in parsed.violations[0].message
    assert parsed.metrics[0].name == "items_checked"


@pytest.mark.parametrize("slow_metric", test_performance.BENCHMARK_METRICS)
def test_performance_suite_fails_and_writes_failed_report_on_2x_slowdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    slow_metric: str,
) -> None:
    baseline_path = tmp_path / "performance_baseline.json"
    baseline_payload = {
        "version": "1.0",
        "record_count": 50,
        "calibration_iterations": 100,
        "median_runs": 3,
        "max_regression_tolerance": 0.45,
        "reference_calibration_seconds": 0.05,
        "metrics": {
            "record_generation_latency": {"static_ceiling_seconds": 4.5, "normalized_ratio": 2.0},
            "mongo_conversion_latency": {"static_ceiling_seconds": 0.35, "normalized_ratio": 1.0},
            "snowflake_arrow_conversion_latency": {
                "static_ceiling_seconds": 1.25,
                "normalized_ratio": 1.5,
            },
            "neo4j_conversion_latency": {"static_ceiling_seconds": 0.2, "normalized_ratio": 0.5},
        },
    }
    baseline_path.write_text(json.dumps(baseline_payload), encoding="utf-8")

    monkeypatch.setattr(test_performance, "calibrate_runner_cpu", lambda **_: 0.05)

    def fake_run_benchmarks(_record_count: int = 50) -> dict[str, float]:
        sample = {
            "record_generation_latency": 0.10,
            "mongo_conversion_latency": 0.05,
            "snowflake_arrow_conversion_latency": 0.075,
            "neo4j_conversion_latency": 0.025,
        }
        sample[slow_metric] *= 2.0
        return sample

    monkeypatch.setattr(test_performance, "run_benchmarks", fake_run_benchmarks)

    report_dir = tmp_path / "reports"
    with pytest.raises(AssertionError, match=slow_metric):
        test_performance.evaluate_performance_suite(
            baseline_path=baseline_path,
            update_baseline=False,
            report_dir=report_dir,
        )

    report_file = report_dir / "performance_report.json"
    assert report_file.exists()
    report = VerificationReport.model_validate_json(report_file.read_text(encoding="utf-8"))
    assert report.status == "failed"
    assert any(slow_metric in v.message for v in report.violations)


def test_performance_suite_update_baseline_rewrites_committed_ratios(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline_path = tmp_path / "performance_baseline.json"
    baseline_payload = {
        "version": "1.0",
        "record_count": 50,
        "calibration_iterations": 100,
        "median_runs": 3,
        "max_regression_tolerance": 0.45,
        "reference_calibration_seconds": 0.01,
        "metrics": {
            m: {"static_ceiling_seconds": 5.0, "normalized_ratio": 999.0}
            for m in test_performance.BENCHMARK_METRICS
        },
    }
    baseline_path.write_text(json.dumps(baseline_payload), encoding="utf-8")

    monkeypatch.setattr(test_performance, "calibrate_runner_cpu", lambda **_: 0.10)
    monkeypatch.setattr(
        test_performance,
        "run_benchmarks",
        lambda _rc=50: {
            "record_generation_latency": 0.40,
            "mongo_conversion_latency": 0.05,
            "snowflake_arrow_conversion_latency": 0.30,
            "neo4j_conversion_latency": 0.02,
        },
    )

    report_dir = tmp_path / "reports"
    test_performance.evaluate_performance_suite(
        baseline_path=baseline_path,
        update_baseline=True,
        report_dir=report_dir,
    )

    updated = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert updated["metrics"]["record_generation_latency"]["normalized_ratio"] == pytest.approx(4.0)
    assert updated["metrics"]["mongo_conversion_latency"]["normalized_ratio"] == pytest.approx(0.5)
    assert updated["metrics"]["snowflake_arrow_conversion_latency"][
        "normalized_ratio"
    ] == pytest.approx(3.0)
    assert updated["metrics"]["neo4j_conversion_latency"]["normalized_ratio"] == pytest.approx(0.2)

    report = VerificationReport.model_validate_json(
        (report_dir / "performance_report.json").read_text(encoding="utf-8")
    )
    assert report.status == "passed"


def test_postman_drift_checks_all_endpoints_and_records_every_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMEDNET_VERIFICATION_REPORT_DIR", str(tmp_path))

    called_paths: list[str] = []

    class DummyEndpoint:
        def __init__(self, endpoint_name: str) -> None:
            self._endpoint_name = endpoint_name

        def _get_endpoint_path(self, sk: str | None) -> str:
            return f"/api/v1/{self._endpoint_name}/{sk or 'root'}"

    class DummySDK:
        def __init__(self) -> None:
            self._client = MagicMock()

        def __getattr__(self, name: str) -> DummyEndpoint:
            return DummyEndpoint(name)

    sdk = DummySDK()

    def fake_client_get(path: str):
        called_paths.append(path)
        resp = MagicMock()
        if "/studies/" in path or "/codings/" in path:
            resp.json.return_value = {"data": [{"unexpectedDriftField": True}]}
        else:
            resp.json.return_value = {"data": []}
        return resp

    sdk._client.get.side_effect = fake_client_get

    with pytest.raises(AssertionError, match="Postman schema drift detected"):
        run_drift_check(sdk, "STUDY1", monkeypatch)  # type: ignore[arg-type]

    # All discovered endpoints were checked rather than aborting on the first mismatch
    assert len(called_paths) > 2
    drift_report_path = tmp_path / "drift_report.json"
    assert drift_report_path.exists()
    drift_report = VerificationReport.model_validate_json(
        drift_report_path.read_text(encoding="utf-8")
    )
    assert drift_report.status == "failed"
    assert drift_report.track == "drift"
    violation_msgs = "\n".join(v.message for v in drift_report.violations)
    assert "studies" in violation_msgs
    assert "codings" in violation_msgs
    assert len(drift_report.violations) >= 2


def test_verification_workflows_enforce_reports_caching_and_deep_fuzzing() -> None:
    perf = yaml.safe_load((WORKFLOWS / "performance.yml").read_text(encoding="utf-8"))
    perf_steps = perf["jobs"]["benchmark"]["steps"]
    assert any(
        "aggregate_verification.py --report-dir reports/verification --expect-track performance"
        in s.get("run", "")
        for s in perf_steps
    )
    perf_upload = next(s for s in perf_steps if s.get("name") == "Save Verification Artifact")
    assert perf_upload["with"]["path"] == "reports/verification/performance_report.json"
    assert perf_upload["with"]["if-no-files-found"] == "error"

    main_wf = yaml.safe_load((WORKFLOWS / "main.yml").read_text(encoding="utf-8"))
    fuzz_steps = main_wf["jobs"]["fuzzing"]["steps"]
    assert any("25,000 iterations" in s.get("name", "") for s in fuzz_steps)
    assert any(
        s.get("uses", "").startswith("actions/cache/restore@")
        and ".cache/verification/fuzz_corpus" in s.get("with", {}).get("path", "")
        for s in fuzz_steps
    )
    save_corpus = next(s for s in fuzz_steps if s.get("name") == "Save Fuzzing Corpus Cache")
    assert "github.event_name == 'push'" in save_corpus["if"]
    assert "refs/heads/main" in save_corpus["if"]
    fuzz_upload = next(s for s in fuzz_steps if s.get("name") == "Save Verification Artifact")
    assert fuzz_upload["with"]["path"] == "reports/verification/fuzzing_report.json"
    assert fuzz_upload["with"]["if-no-files-found"] == "error"

    deep = yaml.safe_load((WORKFLOWS / "fuzzing-deep.yml").read_text(encoding="utf-8"))
    triggers = deep.get("on") or deep.get(True)
    assert "schedule" in triggers and "workflow_dispatch" in triggers
    deep_steps = deep["jobs"]["deep-fuzzing"]["steps"]
    assert any(s.get("env", {}).get("IMEDNET_FUZZ_RUNS") == "1000000" for s in deep_steps)
    deep_upload = next(s for s in deep_steps if s.get("name") == "Save Verification Artifact")
    assert deep_upload["with"]["if-no-files-found"] == "error"
