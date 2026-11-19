"""Multi-study orchestration engine for iMednet SDK.

Provides :class:`MultiStudyOrchestrator` for executing pipeline functions
concurrently across multiple iMednet study boundaries with fault isolation,
telemetry context propagation, and normalized result reporting.

Quick start
-----------

.. code-block:: python

    from imednet import ImednetSDK, MultiStudyOrchestrator

    def my_pipeline(study_key, sdk, study_logger, **kwargs):
        study_logger.info("Processing study")
        subjects = sdk.subjects.list(study_key=study_key)
        return {"subject_count": len(list(subjects))}

    with ImednetSDK() as sdk:
        orchestrator = MultiStudyOrchestrator(sdk, max_workers=4)
        results = orchestrator.execute_pipeline(my_pipeline)

    for study_key, result in results.items():
        if result["status"] == "SUCCESS":
            print(f"{study_key}: {result['data']}")
        else:
            print(f"{study_key}: FAILED — {result['error']}")

Public API
----------

- :class:`MultiStudyOrchestrator` — main orchestration engine
- :class:`StudyContextLogAdapter` — per-study log adapter
- :func:`make_study_logger` — factory for study-bound loggers
- :data:`OrchestratorResult` — per-study result schema (TypedDict)
- :data:`StudyWorkerCallable` — protocol for pipeline callables

Errors
------

- :exc:`~imednet.errors.OrchestratorError` — base orchestration error
- :exc:`~imednet.errors.FilterConflictError` — raised when both whitelist
  and blacklist are provided simultaneously
"""

from __future__ import annotations

from imednet.orchestration.logging import StudyContextLogAdapter, make_study_logger
from imednet.orchestration.orchestrator import MultiStudyOrchestrator
from imednet.orchestration.types import (
    AsyncStudyWorkerCallable,
    OrchestratorResult,
    StudyWorkerCallable,
    T_Output,
)

__all__ = [
    "AsyncStudyWorkerCallable",
    "MultiStudyOrchestrator",
    "OrchestratorResult",
    "StudyContextLogAdapter",
    "StudyWorkerCallable",
    "T_Output",
    "make_study_logger",
]
