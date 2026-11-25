"""Unit tests for shared VerificationReporter, baselines, drift collection, and CI workflows."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
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
    perf_triggers = perf.get("on") or perf.get(True) or {}
    assert {"push", "pull_request", "merge_group"} <= set(perf_triggers)
    assert "paths-ignore" not in (perf_triggers.get("push") or {})
    assert "paths-ignore" not in (perf_triggers.get("pull_request") or {})

    filter_job = perf["jobs"]["filter"]
    assert filter_job["permissions"] == {"contents": "read"}
    assert filter_job["outputs"]["run_benchmarks"] == "${{ steps.evaluate.outputs.run_benchmarks }}"
    for step in filter_job["steps"]:
        if "uses" in step:
            assert len(step["uses"].split("@", 1)[1]) == 40

    benchmark_job = perf["jobs"]["benchmark"]
    assert benchmark_job["needs"] == ["filter"]
    assert benchmark_job["if"] == "needs.filter.outputs.run_benchmarks == 'true'"
    assert benchmark_job["permissions"] == {"contents": "read"}

    perf_steps = benchmark_job["steps"]
    for step in perf_steps:
        if "uses" in step:
            assert len(step["uses"].split("@", 1)[1]) == 40
    assert any(
        "aggregate_verification.py --report-dir reports/verification --expect-track performance"
        in s.get("run", "")
        for s in perf_steps
    )
    perf_upload = next(s for s in perf_steps if s.get("name") == "Save Verification Artifact")
    assert perf_upload["with"]["path"] == "reports/verification/performance_report.json"
    assert perf_upload["with"]["if-no-files-found"] == "error"

    perf_gate = perf["jobs"]["performance-gate"]
    assert perf_gate["name"] == "Performance Gate"
    assert perf_gate["if"] == "always()"
    assert perf_gate["needs"] == ["filter", "benchmark"]
    assert perf_gate["permissions"] == {}

    expected_restore_key = (
        "fuzz-corpus-${{ runner.os }}-sha256-restore-${{ github.run_id }}-${{ github.run_attempt }}"
    )
    expected_restore_prefixes = [
        "fuzz-corpus-${{ runner.os }}-sha256-",
        "fuzz-corpus-${{ runner.os }}-",
    ]
    expected_save_key = (
        "fuzz-corpus-${{ runner.os }}-sha256-${{ hashFiles('.cache/verification/fuzz_corpus/**') }}"
    )
    expected_non_empty_digest = "hashFiles('.cache/verification/fuzz_corpus/**') != ''"
    expected_matched_key_guard = (
        "steps.restore-fuzz-corpus.outputs.cache-matched-key != "
        "format('fuzz-corpus-{0}-sha256-{1}', runner.os, "
        "hashFiles('.cache/verification/fuzz_corpus/**'))"
    )
    legacy_static_key = "fuzz-corpus-${{ runner.os }}-${{ github.sha }}"

    main_raw = (WORKFLOWS / "main.yml").read_text(encoding="utf-8")
    deep_raw = (WORKFLOWS / "fuzzing-deep.yml").read_text(encoding="utf-8")
    assert legacy_static_key not in main_raw
    assert legacy_static_key not in deep_raw

    main_wf = yaml.safe_load(main_raw)
    fuzz_job = main_wf["jobs"]["fuzzing"]
    assert fuzz_job["needs"] == ["plan", "build"]
    assert fuzz_job["if"] == "needs.plan.outputs.matrix != '[]'"
    assert fuzz_job["permissions"] == {"contents": "read"}

    fuzz_steps = fuzz_job["steps"]
    fuzz_runs = [s.get("run", "") for s in fuzz_steps if "run" in s]
    assert not any("pip install" in r for r in fuzz_runs)
    assert any("uv sync --locked --all-packages --all-extras" in r for r in fuzz_runs)
    assert any("25,000 iterations" in s.get("name", "") for s in fuzz_steps)

    deep = yaml.safe_load(deep_raw)
    triggers = deep.get("on") or deep.get(True)
    assert "schedule" in triggers and "workflow_dispatch" in triggers
    deep_job = deep["jobs"]["deep-fuzzing"]
    assert deep_job["permissions"] == {"contents": "read"}
    deep_steps = deep_job["steps"]
    deep_setup_uv = next(
        s for s in deep_steps if str(s.get("uses", "")).startswith("astral-sh/setup-uv@")
    )
    fuzz_setup_uv = next(
        s for s in fuzz_steps if str(s.get("uses", "")).startswith("astral-sh/setup-uv@")
    )
    assert fuzz_setup_uv["uses"] == deep_setup_uv["uses"]
    assert len(fuzz_setup_uv["uses"].split("@", 1)[1]) == 40
    assert fuzz_setup_uv["with"]["enable-cache"] == "false"

    for steps in (fuzz_steps, deep_steps):
        for step in steps:
            if "uses" in step:
                assert len(step["uses"].split("@", 1)[1]) == 40

        aggregate_step = next(s for s in steps if s.get("name") == "Aggregate Verification Report")
        assert aggregate_step["if"] == "always()"
        assert (
            "aggregate_verification.py --report-dir reports/verification --expect-track fuzzing"
            in aggregate_step["run"]
        )

        upload_step = next(s for s in steps if s.get("name") == "Save Verification Artifact")
        assert upload_step["if"] == "always()"

        restore_corpus = next(s for s in steps if s.get("id") == "restore-fuzz-corpus")
        assert restore_corpus["uses"].startswith("actions/cache/restore@")
        assert restore_corpus["with"]["path"] == ".cache/verification/fuzz_corpus/"
        assert restore_corpus["with"]["key"] == expected_restore_key
        restore_prefixes = [
            line.strip()
            for line in restore_corpus["with"]["restore-keys"].splitlines()
            if line.strip()
        ]
        assert restore_prefixes == expected_restore_prefixes

        save_corpus = next(s for s in steps if s.get("name") == "Save Fuzzing Corpus Cache")
        assert save_corpus["uses"].startswith("actions/cache/save@")
        assert save_corpus["with"]["path"] == ".cache/verification/fuzz_corpus/"
        assert save_corpus["with"]["key"] == expected_save_key
        save_cond = " ".join(str(save_corpus["if"]).split())
        assert expected_non_empty_digest in save_cond
        assert expected_matched_key_guard in save_cond
        assert "cache-hit" not in save_cond

    main_save_cond = " ".join(
        str(
            next(s for s in fuzz_steps if s.get("name") == "Save Fuzzing Corpus Cache")["if"]
        ).split()
    )
    assert "github.event_name == 'push'" in main_save_cond
    assert "github.ref == 'refs/heads/main'" in main_save_cond

    fuzz_upload = next(s for s in fuzz_steps if s.get("name") == "Save Verification Artifact")
    assert fuzz_upload["with"]["name"] == "fuzzing-report"
    assert fuzz_upload["with"]["path"] == "reports/verification/fuzzing_report.json"
    assert fuzz_upload["with"]["if-no-files-found"] == "error"

    assert any(s.get("env", {}).get("IMEDNET_FUZZ_RUNS") == "1000000" for s in deep_steps)
    deep_save_cond = " ".join(
        str(
            next(s for s in deep_steps if s.get("name") == "Save Fuzzing Corpus Cache")["if"]
        ).split()
    )
    assert "refs/heads/main" not in deep_save_cond
    assert "github.event_name" not in deep_save_cond

    deep_upload = next(s for s in deep_steps if s.get("name") == "Save Verification Artifact")
    assert deep_upload["with"]["name"] == "deep-fuzzing-report"
    assert deep_upload["with"]["path"] == "reports/verification/fuzzing_report.json"
    assert deep_upload["with"]["if-no-files-found"] == "error"


@pytest.mark.skipif(shutil.which("bash") is None, reason="Bash is needed for workflow scripts")
def test_performance_workflow_path_filter_merge_group_and_gate_execution(tmp_path: Path) -> None:
    perf = yaml.safe_load((WORKFLOWS / "performance.yml").read_text(encoding="utf-8"))
    eval_step = next(s for s in perf["jobs"]["filter"]["steps"] if s.get("id") == "evaluate")
    gate_step = perf["jobs"]["performance-gate"]["steps"][0]
    assert gate_step["if"] == "always()"

    def run_filter(
        *,
        all_changed: str = "",
        event_name: str = "pull_request",
        base_sha: str = "",
        head_sha: str = "",
        cwd: Path = tmp_path,
    ) -> str:
        out_file = tmp_path / "perf_filter_out.txt"
        out_file.write_text("", encoding="utf-8")
        subprocess.run(
            [shutil.which("bash") or "bash", "-e", "-c", eval_step["run"]],
            cwd=cwd,
            env={
                **os.environ,
                "EVENT_NAME": event_name,
                "MERGE_GROUP_BASE_SHA": base_sha,
                "MERGE_GROUP_HEAD_SHA": head_sha,
                "ALL_CHANGED_FILES": all_changed,
                "GITHUB_OUTPUT": str(out_file),
            },
            check=True,
            capture_output=True,
            text=True,
        )
        outputs = dict(
            line.split("=", 1)
            for line in out_file.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )
        return outputs["run_benchmarks"]

    assert run_filter(all_changed="docs/index.rst,README.md,examples/demo.py") == "false"
    assert run_filter(all_changed="./docs/guide.rst,AGENTS.md") == "false"
    assert run_filter(all_changed="docs/index.rst,.github/workflows/performance.yml") == "true"
    assert run_filter(all_changed="packages/core/src/imednet/client.py") == "true"
    assert run_filter(all_changed="") == "true"

    repo = tmp_path / "mg_perf"
    repo.mkdir()
    git_bin = shutil.which("git") or "git"
    subprocess.run([git_bin, "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run([git_bin, "config", "user.email", "ci@example.com"], cwd=repo, check=True)
    subprocess.run([git_bin, "config", "user.name", "CI"], cwd=repo, check=True)
    (repo / "README.md").write_text("# Init\n", encoding="utf-8")
    subprocess.run([git_bin, "add", "README.md"], cwd=repo, check=True)
    subprocess.run(
        [git_bin, "commit", "-m", "chore: init"], cwd=repo, check=True, capture_output=True
    )
    base_sha = subprocess.check_output([git_bin, "rev-parse", "HEAD"], cwd=repo, text=True).strip()

    (repo / "docs").mkdir()
    (repo / "docs" / "intro.rst").write_text("Intro\n=====\n", encoding="utf-8")
    subprocess.run([git_bin, "add", "docs/intro.rst"], cwd=repo, check=True)
    subprocess.run(
        [git_bin, "commit", "-m", "docs: intro (#1)"], cwd=repo, check=True, capture_output=True
    )
    docs_sha = subprocess.check_output([git_bin, "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    assert (
        run_filter(
            event_name="merge_group",
            base_sha=base_sha,
            head_sha=docs_sha,
            cwd=repo,
        )
        == "false"
    )

    (repo / "packages").mkdir()
    (repo / "packages" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run([git_bin, "add", "packages/mod.py"], cwd=repo, check=True)
    subprocess.run(
        [git_bin, "commit", "-m", "feat: add mod (#2)"], cwd=repo, check=True, capture_output=True
    )
    code_sha = subprocess.check_output([git_bin, "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    assert (
        run_filter(
            event_name="merge_group",
            base_sha=base_sha,
            head_sha=code_sha,
            cwd=repo,
        )
        == "true"
    )

    def run_perf_gate(filter_result: str, run_benchmarks: str, benchmark_result: str) -> int:
        needs = {
            "filter": {"result": filter_result, "outputs": {"run_benchmarks": run_benchmarks}},
            "benchmark": {"result": benchmark_result, "outputs": {}},
        }
        proc = subprocess.run(
            [shutil.which("bash") or "bash", "-e", "-c", gate_step["run"]],
            cwd=tmp_path,
            env={**os.environ, "NEEDS_JSON": json.dumps(needs)},
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode

    assert run_perf_gate("success", "false", "skipped") == 0
    assert run_perf_gate("success", "true", "success") == 0
    assert run_perf_gate("success", "true", "failure") == 1
    assert run_perf_gate("success", "true", "cancelled") == 1
    assert run_perf_gate("success", "true", "skipped") == 1
    assert run_perf_gate("failure", "false", "skipped") == 1
