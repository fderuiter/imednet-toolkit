"""Unit tests for execute pipeline."""

from __future__ import annotations

import logging
from time import sleep
from typing import Any
from unittest.mock import MagicMock

import pytest

from imednet.core.context import get_current_study
from imednet.orchestration import MultiStudyOrchestrator


def test_isolation_study_a_failure_does_not_affect_b_and_c(
    orchestrator: MultiStudyOrchestrator,
) -> None:
    """STUDY-A raises RuntimeError; STUDY-B and STUDY-C must succeed."""

    def pipeline(study_key: str, sdk: object, logger: logging.LoggerAdapter, **kwargs: Any) -> str:
        """Helper function to pipeline."""
        if study_key == "STUDY-A":
            raise RuntimeError("Simulated failure")
        return f"processed:{study_key}"

    results = orchestrator.execute_pipeline(pipeline)

    assert results["STUDY-A"]["status"] == "FAILED"
    assert "RuntimeError" in (results["STUDY-A"]["error"] or "")
    assert results["STUDY-B"]["status"] == "SUCCESS"
    assert results["STUDY-B"]["data"] == "processed:STUDY-B"
    assert results["STUDY-C"]["status"] == "SUCCESS"


def test_sdk_immutability_across_threads(mock_sdk: MagicMock) -> None:
    """SDK object identity and state must be unchanged after parallel execution."""
    original_id = id(mock_sdk)

    def pipeline(study_key: str, sdk: object, logger: logging.LoggerAdapter) -> bool:
        """Helper function to pipeline."""
        assert id(sdk) == original_id
        return True

    orch = MultiStudyOrchestrator(mock_sdk, max_workers=3)
    results = orch.execute_pipeline(pipeline)

    assert all(r["status"] == "SUCCESS" for r in results.values())
    assert id(orch.sdk) == original_id


def test_logger_study_key_propagated_to_worker(
    orchestrator: MultiStudyOrchestrator, caplog: pytest.LogCaptureFixture
) -> None:
    """Log records emitted inside workers must carry the correct study_key."""

    def pipeline(study_key: str, sdk: object, logger: logging.LoggerAdapter) -> str:
        """Helper function to pipeline."""
        logger.info("Processing %s", study_key)
        return study_key

    with caplog.at_level(logging.INFO, logger="imednet.orchestration"):
        orchestrator.execute_pipeline(pipeline)

    worker_logs = [record for record in caplog.records if record.msg == "Processing %s"]
    assert worker_logs
    for record in worker_logs:
        assert hasattr(record, "study_key")
        assert record.study_key in {"STUDY-A", "STUDY-B", "STUDY-C"}
        assert hasattr(record, "studyKey")
        assert record.studyKey in {"STUDY-A", "STUDY-B", "STUDY-C"}


def test_result_contains_duration_seconds(orchestrator: MultiStudyOrchestrator) -> None:
    """Test that result contains duration seconds."""

    def pipeline(study_key: str, sdk: object, logger: logging.LoggerAdapter) -> str:
        """Helper function to pipeline."""
        sleep(0.02)
        return study_key

    results = orchestrator.execute_pipeline(pipeline)

    assert all(result["duration_seconds"] > 0 for result in results.values())


def test_successful_result_error_is_none(orchestrator: MultiStudyOrchestrator) -> None:
    """Test that successful result error is none."""

    def pipeline(study_key: str, sdk: object, logger: logging.LoggerAdapter) -> str:
        """Helper function to pipeline."""
        return study_key

    results = orchestrator.execute_pipeline(pipeline)

    assert all(result["data"] is not None for result in results.values())
    assert all(result["error"] is None for result in results.values())


def test_failed_result_data_is_none(orchestrator: MultiStudyOrchestrator) -> None:
    """Test that failed result data is none."""

    def pipeline(study_key: str, sdk: object, logger: logging.LoggerAdapter) -> str:
        """Helper function to pipeline."""
        raise RuntimeError(f"Boom:{study_key}")

    results = orchestrator.execute_pipeline(pipeline)

    assert all(result["status"] == "FAILED" for result in results.values())
    assert all(result["data"] is None for result in results.values())
    assert all(isinstance(result["error"], str) for result in results.values())


def test_execute_pipeline_respects_whitelist(orchestrator: MultiStudyOrchestrator) -> None:
    """Test that execute pipeline respects whitelist."""

    def pipeline(study_key: str, sdk: object, logger: logging.LoggerAdapter) -> str:
        """Helper function to pipeline."""
        return study_key

    results = orchestrator.execute_pipeline(pipeline, whitelist={"STUDY-A"})

    assert set(results) == {"STUDY-A"}


def test_execute_pipeline_context_propagation(orchestrator: MultiStudyOrchestrator) -> None:
    """Test that execute pipeline context propagation."""

    def pipeline(study_key: str, sdk: object, logger: logging.LoggerAdapter) -> str:
        """Helper function to pipeline."""
        return get_current_study()

    results = orchestrator.execute_pipeline(pipeline)

    assert all(result["status"] == "SUCCESS" for result in results.values())
    for study_key, result in results.items():
        assert result["data"] == study_key


@pytest.mark.parametrize("mock_sdk", [[]], indirect=True)
def test_empty_study_list_returns_empty_dict(mock_sdk: MagicMock) -> None:
    """Test that empty study list returns empty dict."""
    orchestrator = MultiStudyOrchestrator(mock_sdk, max_workers=3)

    results = orchestrator.execute_pipeline(lambda *_args, **_kwargs: True)

    assert results == {}


def test_execute_pipeline_bypasses_thread_pool_for_max_workers_one_and_emscripten(
    mock_sdk: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MultiStudyOrchestrator runs sequentially without ThreadPoolExecutor when max_workers==1 or on Emscripten."""
    import sys

    import imednet.orchestration.orchestrator as orch_mod

    def forbid_tpe(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(orch_mod, "ThreadPoolExecutor", forbid_tpe)

    def pipeline(study_key: str, sdk: object, logger: logging.LoggerAdapter) -> str:
        if study_key == "STUDY-B":
            raise ValueError("isolated-study-error")
        return f"ok:{get_current_study()}"

    single_worker_orch = MultiStudyOrchestrator(mock_sdk, max_workers=1)
    single_res = single_worker_orch.execute_pipeline(pipeline)
    assert single_res["STUDY-A"]["status"] == "SUCCESS"
    assert single_res["STUDY-A"]["data"] == "ok:STUDY-A"
    assert single_res["STUDY-B"]["status"] == "FAILED"
    assert "isolated-study-error" in (single_res["STUDY-B"]["error"] or "")
    assert single_res["STUDY-C"]["status"] == "SUCCESS"

    monkeypatch.setattr(sys, "platform", "emscripten")
    emscripten_orch = MultiStudyOrchestrator(mock_sdk, max_workers=4)
    em_res = emscripten_orch.execute_pipeline(pipeline)
    assert em_res["STUDY-A"]["status"] == "SUCCESS"
    assert em_res["STUDY-B"]["status"] == "FAILED"
    assert em_res["STUDY-C"]["status"] == "SUCCESS"


def test_execute_pipeline_sequential_enforces_300s_global_timeout(
    mock_sdk: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sequential MultiStudyOrchestrator aborts remaining studies once 300s global timeout elapses."""
    import imednet.orchestration.orchestrator as orch_mod

    ticks = iter([0.0, 0.0, 305.0, 305.0, 305.1, 305.2])
    monkeypatch.setattr(orch_mod.time, "monotonic", lambda: next(ticks))

    orch = MultiStudyOrchestrator(mock_sdk, max_workers=1)
    executed: list[str] = []

    def pipeline(study_key: str, sdk: object, logger: logging.LoggerAdapter) -> str:
        executed.append(study_key)
        return study_key

    results = orch.execute_pipeline(pipeline)
    assert executed == ["STUDY-A"]
    assert results["STUDY-A"]["status"] == "SUCCESS"
    assert results["STUDY-B"]["status"] == "FAILED"
    assert results["STUDY-B"]["error"] == "TimeoutError: Global orchestration timeout reached."
    assert results["STUDY-C"]["status"] == "FAILED"
    assert results["STUDY-C"]["error"] == "TimeoutError: Global orchestration timeout reached."
