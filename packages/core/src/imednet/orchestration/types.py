"""Type protocols and result schemas for the MultiStudyOrchestrator engine.

This module exports:

- :class:`StudyWorkerCallable` — a :class:`typing.Protocol` that pipeline
  functions must conform to in order to be accepted by
  :meth:`~imednet.orchestration.MultiStudyOrchestrator.execute_pipeline`.
- :class:`OrchestratorResult` — a :class:`typing_extensions.TypedDict` that
  describes the per-study result entry returned by ``execute_pipeline``.
- ``T_Output`` — a covariant :class:`~typing.TypeVar` used to parameterize
  :class:`StudyWorkerCallable`.

StudyWorkerCallable protocol
-----------------------------

Any callable passed to ``execute_pipeline`` must accept at minimum::

    def my_pipeline(
        study_key: str,           # the study identifier for this worker
        sdk: ImednetSDK,          # shared (read-only) SDK instance
        study_logger: Any,        # StudyContextLogAdapter bound to study_key
        *args: Any,
        **kwargs: Any,
    ) -> Any: ...

Example::

    from imednet.orchestration import StudyWorkerCallable

    def count_subjects(study_key, sdk, study_logger, **kwargs):
        subjects = sdk.subjects.list(study_key=study_key)
        return len(list(subjects))

    # Runtime check via @runtime_checkable:
    assert isinstance(count_subjects, StudyWorkerCallable)

OrchestratorResult schema
--------------------------

Each entry in the dict returned by ``execute_pipeline`` is an
:class:`OrchestratorResult` TypedDict with the following fields:

``status``
    ``"SUCCESS"`` or ``"FAILED"``.

``data``
    The value returned by the pipeline callable on success, or ``None``
    on failure.

``error``
    ``repr()`` of the exception on failure, or ``None`` on success.

``duration_seconds``
    Wall-clock time (seconds, rounded to 4 decimal places) for this study.

Example::

    results = orchestrator.execute_pipeline(count_subjects)
    for study_key, r in results.items():
        if r["status"] == "SUCCESS":
            print(f"{study_key}: {r['data']} subjects ({r['duration_seconds']:.2f}s)")
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, TypeVar

from typing_extensions import Protocol, TypedDict, runtime_checkable

T_Output = TypeVar("T_Output", covariant=True)


@runtime_checkable
class StudyWorkerCallable(Protocol[T_Output]):
    """Defines the explicit signature required for injected pipeline tasks.

    Pipeline callables injected into ``MultiStudyOrchestrator.execute_pipeline``
    must conform to this protocol.
    """

    def __call__(
        self,
        study_key: str,
        sdk_client: Any,
        logger: Any,
        *args: Any,
        **kwargs: Any,
    ) -> T_Output:
        """Invoke the pipeline task for a specific study context.

        Args:
            study_key: The identifier of the study being processed.
            sdk_client: A shared (read-only) instance of the iMednet SDK.
            logger: A study-specific logger instance.
            *args: Variable positional arguments passed to the pipeline.
            **kwargs: Variable keyword arguments passed to the pipeline.

        Returns:
            The output of the pipeline task of type T_Output.
        """
        ...


@runtime_checkable
class AsyncStudyWorkerCallable(Protocol[T_Output]):
    """Defines the signature for asynchronous pipeline tasks.

    Pipeline callables injected into ``MultiStudyOrchestrator.async_execute_pipeline``
    may conform to either :class:`AsyncStudyWorkerCallable` or
    :class:`StudyWorkerCallable`.
    """

    def __call__(
        self,
        study_key: str,
        sdk_client: Any,
        logger: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Awaitable[T_Output]:
        """Invoke the asynchronous pipeline task for a specific study context.

        Args:
            study_key: The identifier of the study being processed.
            sdk_client: A shared (read-only) instance of the async iMednet SDK.
            logger: A study-specific logger instance.
            *args: Variable positional arguments passed to the pipeline.
            **kwargs: Variable keyword arguments passed to the pipeline.

        Returns:
            An awaitable resolving to the output of the pipeline task.
        """
        ...


class OrchestratorResult(TypedDict, total=False):
    """Normalized result entry returned per study by ``execute_pipeline``."""

    status: str
    data: Any | None
    error: str | None
    duration_seconds: float


__all__ = [
    "AsyncStudyWorkerCallable",
    "OrchestratorResult",
    "StudyWorkerCallable",
    "T_Output",
]
