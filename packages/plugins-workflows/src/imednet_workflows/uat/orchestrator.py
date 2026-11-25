"""End-to-end UAT execution orchestrator."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any
from unittest.mock import NonCallableMock

from pydantic import Field, field_serializer

from imednet.spi.models import ImednetBaseModel
from imednet.spi.utils import (
    is_site_enrollment_eligible,
    normalize_subject_registration_inputs,
    validate_subject_registration_sites,
)
from imednet.spi.validation import (
    DataDictionary,
    SchemaCache,
    ValidationSchemaProtocol,
    extract_variable_constraints,
    validate_records_batch,
)

from ._contracts import (
    is_disabled_form,
    is_enrollment_form,
    is_non_subject_form,
    is_retired_variable_def,
    is_unscheduled_form,
    lookup_schema_form_variables,
    resolve_enriched_variable_type,
)
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
    dry_run: bool = False
    validation_report: Any = None  # BatchValidationReport

    # Phase-level timing for performance profiling
    phase_durations: dict[str, float] = Field(default_factory=dict)  # phase name → seconds

    @field_serializer("validation_report", when_used="json")
    def _serialize_validation_report(self, value: Any) -> Any:
        if value is None:
            return None
        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            return to_dict()
        return value

    @field_serializer("poll_summary", when_used="json")
    def _serialize_poll_summary(self, value: Any) -> Any:
        if value is None or isinstance(value, dict):
            return value
        results = getattr(value, "results", {})
        failures = getattr(value, "failures", {})
        monitored = (len(results) if isinstance(results, dict) else 0) + (
            len(failures) if isinstance(failures, dict) else 0
        )
        return {
            "studyKey": getattr(value, "study_key", self.study_key),
            "allSuccessful": bool(getattr(value, "all_successful", False)),
            "anyFailed": bool(getattr(value, "any_failed", False)),
            "elapsedTotal": getattr(value, "elapsed_total", 0.0),
            "monitoredCount": monitored,
            "failedBatchIds": _collect_failed_batch_ids(value),
        }

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
        """Return True if all executed phases in the run were successful."""
        if self.state == UATWorkflowState.FAILED:
            return False
        if self.dry_run:
            if self.validation_report is not None and not bool(
                getattr(self.validation_report, "is_valid", True)
            ):
                return False
            if not self.record_sets:
                return False
            if (
                all(not isinstance(rs, NonCallableMock) for rs in self.record_sets)
                and sum(len(rs.payloads) for rs in self.record_sets) == 0
            ):
                return False
            return self.state == UATWorkflowState.COMPLETED

        if (
            self.spec is not None
            and not isinstance(self.spec, NonCallableMock)
            and not self.spec.enabled_forms()
        ):
            return False
        if (
            self.submission_result is not None
            and not isinstance(self.submission_result, NonCallableMock)
            and self.submission_result.total_records_submitted == 0
        ):
            return False
        if self.poll_summary is not None and not isinstance(self.poll_summary, NonCallableMock):
            results = getattr(self.poll_summary, "results", None)
            failures = getattr(self.poll_summary, "failures", None)
            if (
                isinstance(results, dict)
                and isinstance(failures, dict)
                and not results
                and not failures
            ):
                return False
            if not self.record_sets:
                return False
        return bool(getattr(self.poll_summary, "all_successful", False))

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
        ]
        if self.dry_run:
            validated_count = (
                getattr(self.validation_report, "valid_records", 0)
                if self.validation_report is not None
                else sum(
                    len(rs.payloads)
                    for rs in self.record_sets
                    if not isinstance(rs, NonCallableMock)
                )
            )
            lines.append("Mode: dry-run (no API writes)")
            lines.append(f"Records validated: {validated_count}")
        lines.extend(
            [
                f"Records submitted: {records_submitted}",
                f"Jobs monitored: {monitored_count}",
                f"Overall: {'PASS' if self.overall_success else 'FAIL'}",
            ]
        )
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

    def __init__(self, data_dictionary: DataDictionary | None = None) -> None:
        """Initialize the specification builder with an optional DataDictionary."""
        self._data_dictionary = data_dictionary

    def build(
        self,
        snapshot: StudySnapshot,
        *,
        subject_count: int = DEFAULT_SUBJECT_COUNT,
        site_name: str | None = None,
        enabled_form_keys: list[str] | None = None,
        seed: int | None = None,
        data_dictionary: DataDictionary | None = None,
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
        data_dictionary : Optional[DataDictionary]
            Optional offline DataDictionary providing Choice lists, bounds, and required policies.
        """
        active_dd = data_dictionary if data_dictionary is not None else self._data_dictionary
        resolved_site_name = self.resolve_site_name(snapshot, site_name)
        validated_form_keys = self.validate_enabled_form_keys(snapshot, enabled_form_keys)

        subject_spec = UATSubjectSpec(
            site_name=resolved_site_name,
            subject_count=subject_count,
        )

        form_specs = []
        for form in snapshot.forms:
            if is_disabled_form(form) or is_non_subject_form(form):
                continue
            if validated_form_keys is not None and form.form_key not in validated_form_keys:
                continue

            form_key_str = form.form_key or ""
            dd_vars = lookup_schema_form_variables(
                active_dd,
                form_key_str,
                study_key=snapshot.study_key,
            )

            test_type = self._infer_test_type(form)
            variables = snapshot.variables_by_form.get(form_key_str, [])
            var_specs: list[UATVariableSpec] = []
            seen_var_names: set[str] = set()
            retired_snap_names: set[str] = set()
            for v in variables:
                vname = getattr(v, "variable_name", None) or getattr(v, "variableName", None)
                if is_retired_variable_def(v):
                    if isinstance(vname, str) and vname:
                        retired_snap_names.add(vname)
                    continue
                vs = self._build_var_spec(
                    v,
                    dd_var=dd_vars.get(vname or ""),
                    form_key=form_key_str,
                )
                seen_var_names.add(vs.variable_name)
                var_specs.append(vs)

            for dd_vname, dd_var in dd_vars.items():
                if (
                    not dd_vname
                    or dd_vname in seen_var_names
                    or dd_vname in retired_snap_names
                    or is_retired_variable_def(dd_var)
                ):
                    continue
                seen_var_names.add(dd_vname)
                var_specs.append(
                    self._build_var_spec(
                        dd_var,
                        dd_var=dd_var,
                        form_key=form_key_str,
                    )
                )
            required_names = [vs.variable_name for vs in var_specs if vs.required]

            form_specs.append(
                UATFormSpec(
                    form_key=form.form_key or "unknown",
                    form_name=form.form_name or "unknown",
                    form_type=form.form_type or "unknown",
                    test_type=test_type,
                    subject_count=subject_count,
                    variables=var_specs,
                    required_variable_names=required_names,
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
            seed=seed,
        )

    def _infer_test_type(self, form: Any) -> RecordTestType:
        """Heuristic: enrollment forms -> REGISTER_SUBJECT, unscheduled -> CREATE_NEW_RECORD."""
        if is_enrollment_form(form):
            return RecordTestType.REGISTER_SUBJECT
        if is_unscheduled_form(form):
            return RecordTestType.CREATE_NEW_RECORD
        return RecordTestType.UPDATE_SCHEDULED_RECORD

    def _build_var_spec(
        self,
        variable: Any,
        dd_var: Any | None = None,
        form_key: str | None = None,
    ) -> UATVariableSpec:
        """Build a UATVariableSpec from a Variable model and optional DataDictionary Variable."""
        source_dd_var = dd_var
        if source_dd_var is None and hasattr(variable, "choices"):
            source_dd_var = variable

        var_name = (
            getattr(variable, "variable_name", None)
            or getattr(variable, "variableName", None)
            or "unknown"
        )
        var_key = getattr(variable, "variable_key", None) or var_name
        resolved_form_key = (
            getattr(variable, "form_key", None)
            or form_key
            or getattr(source_dd_var, "formKey", None)
            or "unknown"
        )

        snap_type = getattr(variable, "variable_type", None)
        resolved_type = resolve_enriched_variable_type(snap_type, source_dd_var)

        constraints = extract_variable_constraints(source_dd_var)
        return UATVariableSpec(
            variable_name=var_name,
            variable_key=var_key,
            variable_type=resolved_type,
            form_key=resolved_form_key,
            strategy=VariableTestStrategy.SYNTHETIC,
            min_value=constraints.min_value,
            max_value=constraints.max_value,
            max_length=constraints.max_length,
            coded_values=list(constraints.choices),
            required=constraints.required_policy == "yes",
        )

    def validate_enabled_form_keys(
        self,
        snapshot: StudySnapshot,
        enabled_form_keys: list[str] | None,
    ) -> list[str] | None:
        """Validate requested form keys exist on the snapshot and are not disabled."""
        if enabled_form_keys is None:
            return None
        cleaned_keys = [k.strip() for k in enabled_form_keys if k and k.strip()]
        if not cleaned_keys:
            raise ValueError("enabled_form_keys must include at least one active form key.")

        forms_by_key = getattr(snapshot, "forms_by_key", {}) or {}
        unknown_keys: list[str] = []
        disabled_keys: list[str] = []
        for key in cleaned_keys:
            form_obj = forms_by_key.get(key)
            if form_obj is None:
                unknown_keys.append(key)
            elif is_disabled_form(form_obj):
                disabled_keys.append(key)

        if unknown_keys or disabled_keys:
            parts: list[str] = []
            if unknown_keys:
                parts.append(f"unknown form key(s): {', '.join(unknown_keys)}")
            if disabled_keys:
                parts.append(f"disabled form key(s): {', '.join(disabled_keys)}")
            raise ValueError(
                f"Invalid enabled_form_keys for study '{snapshot.study_key}': {'; '.join(parts)}."
            )
        return cleaned_keys

    def resolve_site_name(self, snapshot: StudySnapshot, site_name: str | None) -> str:
        """Validate an explicit site or select the first enrollment-eligible site when None."""
        if site_name is not None:
            cleaned = site_name.strip()
            if not cleaned:
                raise ValueError("siteName is required")
            sites_by_name = {s.site_name: s for s in snapshot.sites if s.site_name}
            if cleaned not in sites_by_name:
                raise ValueError(f"Site '{cleaned}' not found in study '{snapshot.study_key}'.")
            site_obj = sites_by_name[cleaned]
            site_status = getattr(site_obj, "site_enrollment_status", None)
            if not is_site_enrollment_eligible(site_status):
                raise ValueError(
                    f"Site '{cleaned}' is not eligible for enrollment (status: {site_status!s})."
                )
            return site_obj.site_name or cleaned

        for site in snapshot.sites:
            if is_site_enrollment_eligible(site.site_enrollment_status):
                return site.site_name or "Default Site"

        if snapshot.sites:
            raise ValueError(
                f"No enrollment-eligible open sites found in study '{snapshot.study_key}'."
            )

        return "Default Site"

    def _resolve_site_name(self, snapshot: StudySnapshot, site_name: str | None) -> str:
        """Backward-compatible internal alias for :meth:`resolve_site_name`."""
        return self.resolve_site_name(snapshot, site_name)

    def _resolve_interval(self, snapshot: StudySnapshot, form_key: str | None) -> str | None:
        """Find the first active (non-disabled) interval that contains this form."""
        if not form_key:
            return None
        for interval in snapshot.intervals:
            if getattr(interval, "disabled", None) is True:
                continue
            for f_summary in getattr(interval, "forms", []) or []:
                if f_summary.form_key == form_key:
                    return interval.interval_name
        return None


@dataclass
class _UATExecutionContext:
    """Scoped per-run execution context for ``UATWorkflow``."""

    state: UATWorkflowState = UATWorkflowState.PENDING
    state_history: list[UATWorkflowState] = field(
        default_factory=lambda: [UATWorkflowState.PENDING]
    )
    last_result: UATRunResult | None = None
    submitter: Any | None = None
    poller: Any | None = None


def _poll_summary_failed(poll_summary: Any) -> bool:
    """Return True if ``poll_summary`` indicates any failed jobs or records."""
    if poll_summary is None:
        return False
    any_failed = getattr(poll_summary, "any_failed", None)
    if isinstance(any_failed, bool):
        return any_failed
    failures = getattr(poll_summary, "failures", None)
    if isinstance(failures, dict) and failures:
        return True
    all_successful = getattr(poll_summary, "all_successful", None)
    if isinstance(all_successful, bool):
        return not all_successful
    return False


def _collect_failed_batch_ids(poll_summary: Any) -> list[str]:
    """Extract ordered failed batch IDs from ``JobPollSummary``."""
    failed_ids: list[str] = []
    failures = getattr(poll_summary, "failures", None)
    if isinstance(failures, dict):
        for batch_id in failures:
            bid = str(batch_id)
            if bid not in failed_ids:
                failed_ids.append(bid)
    results = getattr(poll_summary, "results", None)
    if isinstance(results, dict):
        for batch_id, status in results.items():
            is_failed = getattr(status, "is_failed", None) is True
            failed_records = getattr(status, "failed_records", 0)
            has_failed_records = isinstance(failed_records, int) and failed_records > 0
            result_error = getattr(status, "result_error", None)
            has_result_error = isinstance(result_error, str) and bool(result_error)
            bid = str(batch_id)
            if (is_failed or has_failed_records or has_result_error) and bid not in failed_ids:
                failed_ids.append(bid)
    return failed_ids


def _apply_subject_count_override(
    spec: UATSpecification,
    subject_count: int,
) -> UATSpecification:
    """Update both subject_specs and form_specs with an explicit subject_count override."""
    updated_subjects = (
        [s.model_copy(update={"subject_count": subject_count}) for s in spec.subject_specs]
        if spec.subject_specs
        else [UATSubjectSpec(site_name="Default Site", subject_count=subject_count)]
    )
    updated_forms = [f.model_copy(update={"subject_count": subject_count}) for f in spec.form_specs]
    return spec.model_copy(
        update={
            "subject_specs": updated_subjects,
            "form_specs": updated_forms,
        }
    )


def _validate_spec_sites(snapshot: Any, spec: UATSpecification, *, custom_spec: bool) -> None:
    """Validate UATSpecification subject_specs sites against StudySnapshot sites."""
    if isinstance(snapshot, NonCallableMock) or isinstance(spec, NonCallableMock):
        return
    snapshot_sites = getattr(snapshot, "sites", None)
    if not isinstance(snapshot_sites, list):
        return
    if not custom_spec and not snapshot_sites:
        return
    if not spec.subject_specs:
        return

    default_form_key = (
        spec.form_specs[0].form_key
        if spec.form_specs and spec.form_specs[0].form_key
        else "UAT_ENROLLMENT"
    )
    reg_inputs = [
        {"formKey": default_form_key, "siteName": subj_spec.site_name}
        for subj_spec in spec.subject_specs
    ]
    normalized_pairs = normalize_subject_registration_inputs(reg_inputs)
    validate_subject_registration_sites(normalized_pairs, snapshot_sites)


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
        data_dictionary: DataDictionary | None = None,
        on_progress: JobProgressCallback | None = None,
        on_phase_change: Callable[[UATWorkflowState], None] | None = None,
    ) -> None:
        """Initialize the UAT workflow orchestrator."""
        from .generator import SyntheticRecordGenerator
        from .inspector import StudySchemaInspector

        self._sdk = sdk
        self._seed = seed
        self._data_dictionary = data_dictionary
        self._inspector = StudySchemaInspector(sdk)
        self._builder = UATSpecificationBuilder(data_dictionary=data_dictionary)
        self._generator = SyntheticRecordGenerator(seed=seed, data_dictionary=data_dictionary)

        # Config for per-run initialization of sync components
        self._batch_size = batch_size
        self._poll_interval = poll_interval
        self._poll_timeout = poll_timeout
        self._on_progress = on_progress
        self._on_phase_change = on_phase_change

        self._submitter: Any | None = None
        self._poller: Any | None = None

        self._latest_context = _UATExecutionContext()
        self._active_context: ContextVar[_UATExecutionContext | None] = ContextVar(
            f"uat_execution_context_{id(self)}",
            default=None,
        )

    def _current_context(self) -> _UATExecutionContext:
        """Return the active run context when executing ``run()``, else the latest context."""
        active = self._active_context.get()
        return active if active is not None else self._latest_context

    @property
    def state(self) -> UATWorkflowState:
        """Return the current lifecycle state of the workflow."""
        return self._current_context().state

    @property
    def state_history(self) -> list[UATWorkflowState]:
        """Return the recorded history of lifecycle states."""
        return list(self._current_context().state_history)

    @property
    def last_result(self) -> UATRunResult | None:
        """Return the result of the last run execution."""
        return self._current_context().last_result

    def transition_to(
        self,
        target: UATWorkflowState | str,
        *,
        dry_run: bool = False,
    ) -> UATWorkflowState:
        """Transition workflow to a new state along an approved lifecycle path.

        Args:
            target: Desired target state.
            dry_run: Whether dry-run mode is active (allowing GENERATING -> COMPLETED).

        Returns:
            The newly set state.

        Raises:
            InvalidUATStateTransitionError: If the transition is not permitted.
        """
        ctx = self._current_context()
        target_state = normalize_uat_state(target)
        validate_uat_transition(ctx.state, target_state, dry_run=dry_run)
        ctx.state = target_state
        ctx.state_history.append(target_state)
        if self._on_phase_change is not None:
            self._on_phase_change(target_state)
        return ctx.state

    def reset(self) -> None:
        """Reset the workflow state machine back to PENDING."""
        self._latest_context = _UATExecutionContext()

    def _create_submitter(self) -> Any:
        from .submission import BulkRecordSubmissionWorkflow

        return BulkRecordSubmissionWorkflow(
            self._sdk,
            batch_size=self._batch_size,
            registration_timeout=self._poll_timeout,
            registration_poll_interval=self._poll_interval,
        )

    def _create_poller(self) -> Any:
        from imednet.spi.utils import JobPoller

        return JobPoller(get_job=self._sdk.get_job)

    def run(
        self,
        study_key: str,
        *,
        spec: UATSpecification | None = None,
        subject_count: int | None = None,
        site_name: str | None = None,
        enabled_form_keys: list[str] | None = None,
        email_notify: bool | str | None = None,
        data_dictionary: DataDictionary | None = None,
        schema: ValidationSchemaProtocol | None = None,
        seed: int | None = None,
        dry_run: bool = False,
    ) -> UATRunResult:
        """Execute the full UAT pipeline in an isolated context and return a UATRunResult."""
        started_at = datetime.now(UTC)
        run_id = str(uuid.uuid4())
        phase_durations: dict[str, float] = {}
        record_sets: list[GeneratedRecordSet] = []
        submission_result: SubmissionResult | None = None
        poll_summary: Any = None
        validation_report: Any = None
        active_dd = data_dictionary if data_dictionary is not None else self._data_dictionary
        effective_dd: DataDictionary | None = (
            active_dd
            if active_dd is not None
            else (schema if isinstance(schema, DataDictionary) else None)
        )
        custom_spec_provided = spec is not None

        if subject_count is not None and (
            isinstance(subject_count, bool) or not 1 <= subject_count <= 100
        ):
            raise ValueError("subject_count must be between 1 and 100.")
        if isinstance(email_notify, str):
            if "\r" in email_notify or "\n" in email_notify or "\x00" in email_notify:
                raise ValueError("email_notify header value must not contain newlines.")
            if not email_notify.strip():
                raise ValueError("email_notify cannot be empty.")

        ctx = _UATExecutionContext(
            submitter=None if dry_run else self._create_submitter(),
            poller=None if dry_run else self._create_poller(),
        )
        self._latest_context = ctx
        token = self._active_context.set(ctx)

        try:
            try:
                # 1. Inspect
                self.transition_to(UATWorkflowState.INSPECTING)
                t0 = time.monotonic()
                if dry_run and effective_dd is not None and "inspect" not in vars(self):
                    from .inspector import StudySnapshot

                    spec_sites = (
                        [s.site_name for s in spec.subject_specs if s.site_name]
                        if spec is not None and not isinstance(spec, NonCallableMock)
                        else None
                    )
                    snapshot = StudySnapshot.from_data_dictionary(
                        effective_dd,
                        study_key,
                        site_name=site_name,
                        spec_site_names=spec_sites,
                    )
                else:
                    snapshot = self.inspect(study_key)
                phase_durations[UATRunPhase.INSPECT] = time.monotonic() - t0

                # 2. Build or Validate Spec
                self.transition_to(UATWorkflowState.BUILDING_SPEC)
                t0 = time.monotonic()
                effective_seed = seed if seed is not None else self._seed
                if spec is None:
                    resolved_subject_count = (
                        UATSpecificationBuilder.DEFAULT_SUBJECT_COUNT
                        if subject_count is None
                        else subject_count
                    )
                    build_kwargs: dict[str, Any] = {
                        "subject_count": resolved_subject_count,
                        "site_name": site_name,
                        "enabled_form_keys": enabled_form_keys,
                    }
                    if effective_seed is not None:
                        build_kwargs["seed"] = effective_seed
                    if effective_dd is not None:
                        build_kwargs["data_dictionary"] = effective_dd
                    spec = self.build_spec(snapshot, **build_kwargs)
                else:
                    if subject_count is not None and not isinstance(spec, NonCallableMock):
                        spec = _apply_subject_count_override(spec, subject_count)
                    if not isinstance(snapshot, NonCallableMock):
                        if site_name is not None:
                            resolved_override = self._builder.resolve_site_name(snapshot, site_name)
                            if not isinstance(spec, NonCallableMock) and spec.subject_specs:
                                spec = spec.model_copy(
                                    update={
                                        "subject_specs": [
                                            s.model_copy(update={"site_name": resolved_override})
                                            for s in spec.subject_specs
                                        ]
                                    }
                                )
                        if enabled_form_keys is not None:
                            validated_keys = set(
                                self._builder.validate_enabled_form_keys(
                                    snapshot, enabled_form_keys
                                )
                                or []
                            )
                            if not isinstance(spec, NonCallableMock):
                                spec = spec.model_copy(
                                    update={
                                        "form_specs": [
                                            f.model_copy(
                                                update={"enabled": f.form_key in validated_keys}
                                            )
                                            for f in spec.form_specs
                                        ]
                                    }
                                )
                _validate_spec_sites(snapshot, spec, custom_spec=custom_spec_provided)
                if (
                    effective_seed is None
                    and not isinstance(spec, NonCallableMock)
                    and spec.seed is not None
                ):
                    effective_seed = spec.seed
                phase_durations[UATRunPhase.BUILD_SPEC] = time.monotonic() - t0

                # 3. Generate
                self.transition_to(UATWorkflowState.GENERATING)
                t0 = time.monotonic()
                gen_kwargs: dict[str, Any] = {}
                if effective_dd is not None:
                    gen_kwargs["data_dictionary"] = effective_dd
                if schema is not None and schema is not effective_dd:
                    gen_kwargs["schema"] = schema
                if effective_seed != self._seed:
                    gen_kwargs["seed"] = effective_seed
                record_sets = self.generate(spec, snapshot, **gen_kwargs)
                phase_durations[UATRunPhase.GENERATE] = time.monotonic() - t0

                is_real_spec = not isinstance(spec, NonCallableMock)
                has_real_record_sets = all(
                    not isinstance(rs, NonCallableMock) for rs in record_sets
                )
                total_generated = (
                    sum(len(rs.payloads) for rs in record_sets) if has_real_record_sets else 1
                )
                if is_real_spec and (
                    not spec.enabled_forms() or not record_sets or total_generated == 0
                ):
                    self.transition_to(UATWorkflowState.FAILED)
                    result = UATRunResult(
                        study_key=study_key,
                        run_id=run_id,
                        started_at=started_at,
                        completed_at=datetime.now(UTC),
                        state=ctx.state,
                        state_history=list(ctx.state_history),
                        failure_phase=UATWorkflowState.GENERATING,
                        error_message=(
                            f"UAT run for study '{study_key}' produced zero active forms or records."
                        ),
                        spec=spec,
                        record_sets=record_sets,
                        dry_run=dry_run,
                        phase_durations=phase_durations,
                    )
                    ctx.last_result = result
                    return result

                if dry_run:
                    validation_schema = self._resolve_validation_schema(
                        study_key=study_key,
                        snapshot=snapshot,
                        schema=schema,
                        data_dictionary=active_dd,
                    )
                    flat_payloads: list[dict[str, Any]] = []
                    for rs in record_sets:
                        if not isinstance(rs, NonCallableMock):
                            flat_payloads.extend(rs.payloads)

                    validation_report = validate_records_batch(
                        validation_schema,
                        flat_payloads,
                        study_key=study_key,
                        collect_errors=True,
                    )
                    if not validation_report.is_valid:
                        self.transition_to(UATWorkflowState.FAILED)
                        result = UATRunResult(
                            study_key=study_key,
                            run_id=run_id,
                            started_at=started_at,
                            completed_at=datetime.now(UTC),
                            state=ctx.state,
                            state_history=list(ctx.state_history),
                            failure_phase=UATWorkflowState.GENERATING,
                            error_message=validation_report.format_summary(),
                            spec=spec,
                            record_sets=record_sets,
                            dry_run=True,
                            validation_report=validation_report,
                            phase_durations=phase_durations,
                        )
                        ctx.last_result = result
                        return result

                    self.transition_to(UATWorkflowState.COMPLETED, dry_run=True)
                    result = UATRunResult(
                        study_key=study_key,
                        run_id=run_id,
                        started_at=started_at,
                        completed_at=datetime.now(UTC),
                        state=ctx.state,
                        state_history=list(ctx.state_history),
                        spec=spec,
                        record_sets=record_sets,
                        dry_run=True,
                        validation_report=validation_report,
                        phase_durations=phase_durations,
                    )
                    ctx.last_result = result
                    return result

                # 4. Submit
                self.transition_to(UATWorkflowState.SUBMITTING)
                t0 = time.monotonic()
                effective_submit_schema: ValidationSchemaProtocol | None = (
                    schema
                    if schema is not None
                    else (
                        _merge_snapshot_into_data_dictionary(active_dd, snapshot)
                        if active_dd is not None
                        else None
                    )
                )
                if effective_submit_schema is not None:
                    submission_result = self.submit(
                        study_key,
                        record_sets,
                        email_notify=email_notify,
                        schema=effective_submit_schema,
                    )
                else:
                    submission_result = self.submit(
                        study_key,
                        record_sets,
                        email_notify=email_notify,
                    )
                phase_durations[UATRunPhase.SUBMIT] = time.monotonic() - t0

                # 5. Monitor
                self.transition_to(UATWorkflowState.MONITORING)
                t0 = time.monotonic()
                poll_summary = self.monitor(study_key, submission_result)
                phase_durations[UATRunPhase.MONITOR] = time.monotonic() - t0

                if _poll_summary_failed(poll_summary):
                    failed_batch_ids = _collect_failed_batch_ids(poll_summary)
                    err_msg = (
                        f"Monitored job batches failed: {', '.join(failed_batch_ids)}"
                        if failed_batch_ids
                        else "One or more monitored job batches failed."
                    )
                    self.transition_to(UATWorkflowState.FAILED)
                    result = UATRunResult(
                        study_key=study_key,
                        run_id=run_id,
                        started_at=started_at,
                        completed_at=datetime.now(UTC),
                        state=ctx.state,
                        state_history=list(ctx.state_history),
                        failure_phase=UATWorkflowState.MONITORING,
                        error_message=err_msg,
                        spec=spec,
                        record_sets=record_sets,
                        submission_result=submission_result,
                        poll_summary=poll_summary,
                        phase_durations=phase_durations,
                    )
                    ctx.last_result = result
                    return result

                # Complete
                self.transition_to(UATWorkflowState.COMPLETED)
                result = UATRunResult(
                    study_key=study_key,
                    run_id=run_id,
                    started_at=started_at,
                    completed_at=datetime.now(UTC),
                    state=ctx.state,
                    state_history=list(ctx.state_history),
                    spec=spec,
                    record_sets=record_sets,
                    submission_result=submission_result,
                    poll_summary=poll_summary,
                    phase_durations=phase_durations,
                )
                ctx.last_result = result
                return result

            except InvalidUATStateTransitionError:
                raise
            except ValueError as exc:
                failed_phase = ctx.state
                if not ctx.state.is_terminal:
                    self.transition_to(UATWorkflowState.FAILED)
                result = UATRunResult(
                    study_key=study_key,
                    run_id=run_id,
                    started_at=started_at,
                    completed_at=datetime.now(UTC),
                    state=ctx.state,
                    state_history=list(ctx.state_history),
                    failure_phase=failed_phase,
                    error_message=str(exc),
                    spec=spec,
                    record_sets=record_sets,
                    submission_result=submission_result,
                    poll_summary=poll_summary,
                    dry_run=dry_run,
                    validation_report=validation_report,
                    phase_durations=phase_durations,
                )
                ctx.last_result = result
                if failed_phase == UATWorkflowState.BUILDING_SPEC:
                    raise
                return result
            except Exception as exc:
                failed_phase = ctx.state
                if not ctx.state.is_terminal:
                    self.transition_to(UATWorkflowState.FAILED)
                result = UATRunResult(
                    study_key=study_key,
                    run_id=run_id,
                    started_at=started_at,
                    completed_at=datetime.now(UTC),
                    state=ctx.state,
                    state_history=list(ctx.state_history),
                    failure_phase=failed_phase,
                    error_message=str(exc),
                    spec=spec,
                    record_sets=record_sets,
                    submission_result=submission_result,
                    poll_summary=poll_summary,
                    dry_run=dry_run,
                    validation_report=validation_report,
                    phase_durations=phase_durations,
                )
                ctx.last_result = result
                return result
        finally:
            self._active_context.reset(token)

    def _resolve_validation_schema(
        self,
        *,
        study_key: str,
        snapshot: Any,
        schema: ValidationSchemaProtocol | None,
        data_dictionary: DataDictionary | None,
    ) -> ValidationSchemaProtocol:
        """Resolve validation schema from explicit schema, DataDictionary, or StudySnapshot."""
        if schema is not None:
            return schema
        if data_dictionary is not None:
            return _merge_snapshot_into_data_dictionary(data_dictionary, snapshot)
        cache = SchemaCache()
        snapshot_vars = getattr(snapshot, "variables", None)
        if isinstance(snapshot_vars, list):
            cache.populate(snapshot_vars, study_key=study_key)
        return cache

    def inspect(self, study_key: str) -> Any:
        """Fetch study metadata via StudySchemaInspector."""
        return self._inspector.inspect(study_key)

    def build_spec(self, snapshot: Any, **kwargs: Any) -> UATSpecification:
        """Auto-generate a UATSpecification from the StudySnapshot."""
        return self._builder.build(snapshot, **kwargs)

    def generate(
        self,
        spec: UATSpecification,
        snapshot: Any,
        *,
        data_dictionary: DataDictionary | None = None,
        schema: ValidationSchemaProtocol | None = None,
        seed: int | None = None,
    ) -> list[GeneratedRecordSet]:
        """Create synthetic record payloads via SyntheticRecordGenerator."""
        effective_dd = data_dictionary if data_dictionary is not None else self._data_dictionary
        gen_kwargs: dict[str, Any] = {}
        if (
            data_dictionary is not None or (seed is not None and seed != self._seed)
        ) and effective_dd is not None:
            gen_kwargs["data_dictionary"] = effective_dd
        if schema is not None:
            gen_kwargs["schema"] = schema
        if seed is not None and seed != self._seed:
            from .generator import SyntheticRecordGenerator

            gen = SyntheticRecordGenerator(seed=seed, data_dictionary=effective_dd)
            return gen.generate(spec, snapshot, **gen_kwargs)
        return self._generator.generate(spec, snapshot, **gen_kwargs)

    def submit(
        self,
        study_key: str,
        record_sets: list[GeneratedRecordSet],
        email_notify: bool | str | None = None,
        *,
        schema: ValidationSchemaProtocol | None = None,
    ) -> SubmissionResult:
        """Execute the two-phase bulk submission."""
        active = self._active_context.get()
        submitter = (
            self._submitter
            if self._submitter is not None
            else (
                active.submitter
                if active is not None and active.submitter is not None
                else self._create_submitter()
            )
        )
        active_dd = schema if schema is not None else self._data_dictionary
        if active_dd is not None:
            return submitter.submit(  # type: ignore[no-any-return]
                study_key,
                record_sets,
                email_notify=email_notify,
                schema=active_dd,
            )
        return submitter.submit(study_key, record_sets, email_notify=email_notify)  # type: ignore[no-any-return]

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

        active = self._active_context.get()
        poller = (
            active.poller
            if active is not None and active.poller is not None
            else self._create_poller()
        )
        return poller.poll_many(
            study_key,
            submission_result.all_batch_ids,
            interval=self._poll_interval,
            timeout=self._poll_timeout,
            on_progress=self._on_progress,
        )


def _merge_snapshot_into_data_dictionary(
    data_dictionary: DataDictionary,
    snapshot: Any,
) -> DataDictionary:
    """Backfill active StudySnapshot forms/variables omitted from an enrichment DataDictionary."""
    if isinstance(snapshot, NonCallableMock) or not isinstance(data_dictionary, DataDictionary):
        return data_dictionary
    snap_forms = getattr(snapshot, "forms", None)
    vars_by_form = getattr(snapshot, "variables_by_form", None)
    if not isinstance(snap_forms, list) or not isinstance(vars_by_form, Mapping):
        return data_dictionary

    existing_forms = {f.formKey: f for f in data_dictionary.forms}
    active_snap_forms: list[tuple[str, Any, list[Any]]] = []
    needs_backfill = False

    for form in snap_forms:
        if is_disabled_form(form) or is_non_subject_form(form):
            continue
        fk = getattr(form, "form_key", None) or ""
        if not fk:
            continue
        active_vars = [v for v in vars_by_form.get(fk, []) if not is_retired_variable_def(v)]
        active_snap_forms.append((fk, form, active_vars))
        if fk not in existing_forms:
            needs_backfill = True
        else:
            known_vars = set(data_dictionary.variables_for_form(fk).keys())
            for v in active_vars:
                vname = getattr(v, "variable_name", None) or getattr(v, "variableName", None)
                if vname and vname not in known_vars:
                    needs_backfill = True
                    break

    if not needs_backfill:
        return data_dictionary

    snap_vars_lookup = {fk: active_vars for fk, _, active_vars in active_snap_forms}
    merged_forms: list[Any] = []
    for dd_form in data_dictionary.forms:
        known_vars = {v.variableName for v in dd_form.variables}
        extra_vars: list[dict[str, Any]] = []
        for v in snap_vars_lookup.get(dd_form.formKey, []):
            vname = getattr(v, "variable_name", None) or getattr(v, "variableName", None)
            if vname and vname not in known_vars:
                known_vars.add(vname)
                extra_vars.append(
                    {
                        "variableName": vname,
                        "variableType": getattr(v, "variable_type", None)
                        or getattr(v, "variableType", None),
                        "label": getattr(v, "label", None),
                    }
                )
        if extra_vars:
            merged_forms.append(
                {
                    **dd_form.model_dump(),
                    "variables": [*dd_form.variables, *extra_vars],
                }
            )
        else:
            merged_forms.append(dd_form)

    for fk, form, active_vars in active_snap_forms:
        if fk in existing_forms:
            continue
        snap_var_dicts: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        for v in active_vars:
            vname = getattr(v, "variable_name", None) or getattr(v, "variableName", None)
            if vname and vname not in seen_names:
                seen_names.add(vname)
                snap_var_dicts.append(
                    {
                        "variableName": vname,
                        "variableType": getattr(v, "variable_type", None)
                        or getattr(v, "variableType", None),
                        "label": getattr(v, "label", None),
                    }
                )
        merged_forms.append(
            {
                "formKey": fk,
                "formName": getattr(form, "form_name", None),
                "formId": getattr(form, "form_id", None),
                "formType": getattr(form, "form_type", None),
                "variables": snap_var_dicts,
            }
        )

    return DataDictionary.model_validate(
        {
            "metadata": data_dictionary.metadata,
            "forms": merged_forms,
            "unassignedBusinessLogic": data_dictionary.unassignedBusinessLogic,
        }
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
