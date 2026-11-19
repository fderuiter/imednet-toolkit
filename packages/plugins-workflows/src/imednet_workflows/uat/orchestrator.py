"""End-to-end UAT execution orchestrator."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from pydantic import Field

from imednet.spi.models import ImednetBaseModel

from .generator import GeneratedRecordSet
from .models import (
    TERMINAL_UAT_STATES,
    InvalidUATStateTransitionError,
    RecordTestType,
    UATFormSpec,
    UATSpecification,
    UATSubjectSpec,
    UATVariableSpec,
    UATWorkflowState,
    VariableTestStrategy,
    normalize_uat_state,
    validate_uat_transition,
)
from .submission import SubmissionResult

if TYPE_CHECKING:
    from imednet.spi.facade import ImednetFacade
    from imednet.spi.utils import JobProgressCallback

    from .inspector import StudySnapshot


class UATRunPhase(str, Enum):
    """Phases of a UAT workflow execution."""

    INSPECT = "inspect"
    BUILD_SPEC = "build_spec"
    GENERATE = "generate"
    SUBMIT = "submit"
    MONITOR = "monitor"


class UATRunResult(ImednetBaseModel):
    """Complete result of a UATWorkflow.run() execution."""

    study_key: str
    run_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    started_at: datetime
    completed_at: datetime | None = None

    state: UATWorkflowState = UATWorkflowState.COMPLETED
    state_history: list[UATWorkflowState] = Field(default_factory=list)
    failure_phase: UATWorkflowState | str | None = None
    error_message: str | None = None

    spec: UATSpecification | None = None
    record_sets: list[GeneratedRecordSet] = Field(default_factory=list)
    submission_result: SubmissionResult | None = None
    poll_summary: Any = None  # JobPollSummary

    # Phase-level timing for performance profiling
    phase_durations: dict[str, float] = Field(default_factory=dict)  # phase name → seconds

    @property
    def failed_phase(self) -> UATWorkflowState | str | None:
        """Alias for failure_phase."""
        return self.failure_phase

    @property
    def is_terminal(self) -> bool:
        """Return True if the run reached a terminal state."""
        return self.state in TERMINAL_UAT_STATES

    @property
    def overall_success(self) -> bool:
        """Return True if all jobs in the run were successful."""
        if self.state == UATWorkflowState.FAILED:
            return False
        return getattr(self.poll_summary, "all_successful", False)

    def summary(self) -> str:
        """Return a human-readable summary string suitable for CLI output."""
        records_submitted = (
            self.submission_result.total_records_submitted if self.submission_result else 0
        )
        monitored_count = (
            len(getattr(self.poll_summary, "results", {}))
            + len(getattr(self.poll_summary, "failures", {}))
            if self.poll_summary
            else 0
        )
        lines = [
            f"UAT Run: {self.run_id}",
            f"Study:   {self.study_key}",
            f"Forms tested: {len(self.record_sets)}",
            f"Records submitted: {records_submitted}",
            f"Jobs monitored: {monitored_count}",
            f"Overall: {'PASS' if self.overall_success else 'FAIL'}",
        ]
        if self.failure_phase:
            phase_val = (
                self.failure_phase.value
                if isinstance(self.failure_phase, Enum)
                else str(self.failure_phase)
            )
            lines.append(f"Failed phase: {phase_val}")
        if self.error_message:
            lines.append(f"Error: {self.error_message}")
        if self.completed_at and self.started_at:
            duration = (self.completed_at - self.started_at).total_seconds()
            lines.append(f"Duration: {duration:.1f}s")

        return "\n".join(lines)


class UATSpecificationBuilder:
    """Auto-generate a UATSpecification from a StudySnapshot.

    The generated spec uses SYNTHETIC strategy for all variables.
    Users can then edit the JSON output to apply FIXED or SKIP strategies
    for specific variables before feeding it back into UATWorkflow.run().
    """

    DEFAULT_SUBJECT_COUNT = 2

    def build(
        self,
        snapshot: StudySnapshot,
        *,
        subject_count: int = DEFAULT_SUBJECT_COUNT,
        site_name: str | None = None,
        enabled_form_keys: list[str] | None = None,
        seed: int | None = None,
    ) -> UATSpecification:
        """Build a complete UATSpecification from a StudySnapshot.

        Parameters
        ----------
        snapshot : StudySnapshot
            The study metadata snapshot from StudySchemaInspector.
        subject_count : int
            Number of test subjects to register. Default 2.
        site_name : Optional[str]
            Site to register subjects at. If None, uses the first active site.
        enabled_form_keys : Optional[List[str]]
            Restrict to a subset of forms. If None, all non-disabled forms are included.
        seed : Optional[int]
            Passed through to UATSpecification for traceability.
        """
        resolved_site_name = self._resolve_site_name(snapshot, site_name)

        subject_spec = UATSubjectSpec(
            site_name=resolved_site_name,
            subject_count=subject_count,
        )

        form_specs = []
        for form in snapshot.forms:
            if getattr(form, "disabled", False):
                continue
            if enabled_form_keys and form.form_key not in enabled_form_keys:
                continue

            test_type = self._infer_test_type(form)
            variables = snapshot.variables_by_form.get(form.form_key or "", [])
            var_specs = [
                self._build_var_spec(v) for v in variables if not getattr(v, "disabled", False)
            ]

            form_specs.append(
                UATFormSpec(
                    form_key=form.form_key or "unknown",
                    form_name=form.form_name or "unknown",
                    form_type=form.form_type or "unknown",
                    test_type=test_type,
                    subject_count=subject_count,
                    variables=var_specs,
                    interval_name=self._resolve_interval(snapshot, form.form_key),
                )
            )

        return UATSpecification(
            study_key=snapshot.study_key,
            study_name=snapshot.study_key,
            subject_specs=[subject_spec],
            form_specs=form_specs,
            forms_snapshot_count=len(snapshot.forms),
            variables_snapshot_count=len(snapshot.variables),
            intervals_snapshot_count=len(snapshot.intervals),
            sites_snapshot_count=len(snapshot.sites),
        )

    def _infer_test_type(self, form: Any) -> RecordTestType:
        """Heuristic: enrollment forms → REGISTER_SUBJECT, rest → CREATE_NEW_RECORD."""
        form_type = getattr(form, "form_type", "")
        if form_type in ("Enrollment", "Registration"):
            return RecordTestType.REGISTER_SUBJECT
        if getattr(form, "unscheduled_visit", False):
            return RecordTestType.CREATE_NEW_RECORD
        return RecordTestType.UPDATE_SCHEDULED_RECORD

    def _build_var_spec(self, variable: Any) -> UATVariableSpec:
        """Build a UATVariableSpec from a Variable model."""
        return UATVariableSpec(
            variable_name=getattr(variable, "variable_name", "unknown"),
            variable_key=getattr(variable, "variable_key", "unknown"),
            variable_type=getattr(variable, "variable_type", "Text"),
            form_key=getattr(variable, "form_key", "unknown"),
            strategy=VariableTestStrategy.SYNTHETIC,
        )

    def _resolve_site_name(self, snapshot: StudySnapshot, site_name: str | None) -> str:
        """Find the requested site or the first active site."""
        if site_name:
            for site in snapshot.sites:
                if site.site_name == site_name:
                    return site.site_name or site_name

        active_sites = snapshot.active_sites()
        if active_sites:
            return active_sites[0].site_name or "Default Site"

        if snapshot.sites:
            return snapshot.sites[0].site_name or "Default Site"

        return "Default Site"

    def _resolve_interval(self, snapshot: StudySnapshot, form_key: str | None) -> str | None:
        """Find the first interval that contains this form."""
        if not form_key:
            return None
        for interval in snapshot.intervals:
            for f_summary in getattr(interval, "forms", []) or []:
                if f_summary.form_key == form_key:
                    return interval.interval_name
        return None


class UATWorkflow:
    """End-to-end UAT execution coordinator.

    Composes StudySchemaInspector, UATSpecificationBuilder,
    SyntheticRecordGenerator, BulkRecordSubmissionWorkflow, and JobPoller
    into a single pipeline.
    """

    def __init__(
        self,
        sdk: ImednetFacade,
        *,
        batch_size: int = 100,
        poll_interval: float = 10.0,
        poll_timeout: float = 600.0,
        seed: int | None = None,
        on_progress: JobProgressCallback | None = None,
        on_phase_change: Callable[[UATWorkflowState], None] | None = None,
    ) -> None:
        """Initialize the UAT workflow orchestrator."""
        from .generator import SyntheticRecordGenerator
        from .inspector import StudySchemaInspector

        self._sdk = sdk
        self._inspector = StudySchemaInspector(sdk)
        self._builder = UATSpecificationBuilder()
        self._generator = SyntheticRecordGenerator(seed=seed)

        # Config for lazy initialization of sync components
        self._batch_size = batch_size
        self._poll_interval = poll_interval
        self._poll_timeout = poll_timeout
        self._on_progress = on_progress
        self._on_phase_change = on_phase_change

        self._submitter: Any | None = None
        self._poller: Any | None = None

        self._state: UATWorkflowState = UATWorkflowState.PENDING
        self._state_history: list[UATWorkflowState] = [UATWorkflowState.PENDING]
        self._last_result: UATRunResult | None = None

    @property
    def state(self) -> UATWorkflowState:
        """Return the current lifecycle state of the workflow."""
        return self._state

    @property
    def state_history(self) -> list[UATWorkflowState]:
        """Return the recorded history of lifecycle states."""
        return list(self._state_history)

    @property
    def last_result(self) -> UATRunResult | None:
        """Return the result of the last run execution."""
        return self._last_result

    def transition_to(self, target: UATWorkflowState | str) -> UATWorkflowState:
        """Transition workflow to a new state along an approved lifecycle path.

        Args:
            target: Desired target state.

        Returns:
            The newly set state.

        Raises:
            InvalidUATStateTransitionError: If the transition is not permitted.
        """
        target_state = normalize_uat_state(target)
        validate_uat_transition(self._state, target_state)
        self._state = target_state
        self._state_history.append(target_state)
        if self._on_phase_change is not None:
            self._on_phase_change(target_state)
        return self._state

    def reset(self) -> None:
        """Reset the workflow state machine back to PENDING."""
        self._state = UATWorkflowState.PENDING
        self._state_history = [UATWorkflowState.PENDING]
        self._last_result = None

    def _get_submitter(self) -> Any:
        if self._submitter is None:
            from .submission import BulkRecordSubmissionWorkflow

            self._submitter = BulkRecordSubmissionWorkflow(
                self._sdk,
                batch_size=self._batch_size,
                registration_timeout=self._poll_timeout,
                registration_poll_interval=self._poll_interval,
            )
        return self._submitter

    def _get_poller(self) -> Any:
        if self._poller is None:
            from imednet.spi.utils import JobPoller

            self._poller = JobPoller(get_job=self._sdk.get_job)

        return self._poller

    def run(
        self,
        study_key: str,
        *,
        spec: UATSpecification | None = None,
        subject_count: int = 2,
        site_name: str | None = None,
        enabled_form_keys: list[str] | None = None,
        email_notify: bool | str | None = None,
    ) -> UATRunResult:
        """Execute the full UAT pipeline and return a UATRunResult."""
        started_at = datetime.now(UTC)
        run_id = str(uuid.uuid4())
        phase_durations: dict[str, float] = {}
        record_sets: list[GeneratedRecordSet] = []
        submission_result: SubmissionResult | None = None
        poll_summary: Any = None

        try:
            # 1. Inspect
            self.transition_to(UATWorkflowState.INSPECTING)
            t0 = time.monotonic()
            snapshot = self.inspect(study_key)
            phase_durations[UATRunPhase.INSPECT] = time.monotonic() - t0

            # 2. Build Spec (if not provided)
            self.transition_to(UATWorkflowState.BUILDING_SPEC)
            t0 = time.monotonic()
            if spec is None:
                spec = self.build_spec(
                    snapshot,
                    subject_count=subject_count,
                    site_name=site_name,
                    enabled_form_keys=enabled_form_keys,
                )
            phase_durations[UATRunPhase.BUILD_SPEC] = time.monotonic() - t0

            # 3. Generate
            self.transition_to(UATWorkflowState.GENERATING)
            t0 = time.monotonic()
            record_sets = self.generate(spec, snapshot)
            phase_durations[UATRunPhase.GENERATE] = time.monotonic() - t0

            # 4. Submit
            self.transition_to(UATWorkflowState.SUBMITTING)
            t0 = time.monotonic()
            submission_result = self.submit(study_key, record_sets, email_notify=email_notify)
            phase_durations[UATRunPhase.SUBMIT] = time.monotonic() - t0

            # 5. Monitor
            self.transition_to(UATWorkflowState.MONITORING)
            t0 = time.monotonic()
            poll_summary = self.monitor(study_key, submission_result)
            phase_durations[UATRunPhase.MONITOR] = time.monotonic() - t0

            # Complete
            self.transition_to(UATWorkflowState.COMPLETED)
            result = UATRunResult(
                study_key=study_key,
                run_id=run_id,
                started_at=started_at,
                completed_at=datetime.now(UTC),
                state=self._state,
                state_history=list(self._state_history),
                spec=spec,
                record_sets=record_sets,
                submission_result=submission_result,
                poll_summary=poll_summary,
                phase_durations=phase_durations,
            )
            self._last_result = result
            return result

        except InvalidUATStateTransitionError:
            raise
        except Exception as exc:
            failed_phase = self._state
            self.transition_to(UATWorkflowState.FAILED)
            result = UATRunResult(
                study_key=study_key,
                run_id=run_id,
                started_at=started_at,
                completed_at=datetime.now(UTC),
                state=self._state,
                state_history=list(self._state_history),
                failure_phase=failed_phase,
                error_message=str(exc),
                spec=spec,
                record_sets=record_sets,
                submission_result=submission_result,
                poll_summary=poll_summary,
                phase_durations=phase_durations,
            )
            self._last_result = result
            return result

    def inspect(self, study_key: str) -> Any:
        """Fetch study metadata via StudySchemaInspector."""
        return self._inspector.inspect(study_key)

    def build_spec(self, snapshot: Any, **kwargs: Any) -> UATSpecification:
        """Auto-generate a UATSpecification from the StudySnapshot."""
        return self._builder.build(snapshot, **kwargs)

    def generate(self, spec: UATSpecification, snapshot: Any) -> list[GeneratedRecordSet]:
        """Create synthetic record payloads via SyntheticRecordGenerator."""
        return self._generator.generate(spec, snapshot)

    def submit(
        self,
        study_key: str,
        record_sets: list[GeneratedRecordSet],
        email_notify: bool | str | None = None,
    ) -> SubmissionResult:
        """Execute the two-phase bulk submission."""
        return self._get_submitter().submit(study_key, record_sets, email_notify=email_notify)  # type: ignore[no-any-return]

    def monitor(self, study_key: str, submission_result: SubmissionResult) -> Any:
        """Poll all resulting jobs to completion via facade poll_jobs (or JobPoller fallback)."""
        if self._poller is not None:
            return self._poller.poll_many(
                study_key,
                submission_result.all_batch_ids,
                interval=self._poll_interval,
                timeout=self._poll_timeout,
                on_progress=self._on_progress,
            )

        from imednet.spi.utils import JobPollSummary

        poll_jobs_fn = getattr(self._sdk, "poll_jobs", None)
        if callable(poll_jobs_fn):
            candidate = poll_jobs_fn(
                study_key,
                submission_result.all_batch_ids,
                interval=self._poll_interval,
                timeout=self._poll_timeout,
                on_progress=self._on_progress,
            )
            if isinstance(candidate, JobPollSummary):
                return candidate

        return self._get_poller().poll_many(
            study_key,
            submission_result.all_batch_ids,
            interval=self._poll_interval,
            timeout=self._poll_timeout,
            on_progress=self._on_progress,
        )


UATRunResult.model_rebuild()

__all__ = [
    "InvalidUATStateTransitionError",
    "UATRunPhase",
    "UATRunResult",
    "UATSpecificationBuilder",
    "UATWorkflow",
    "UATWorkflowState",
]
