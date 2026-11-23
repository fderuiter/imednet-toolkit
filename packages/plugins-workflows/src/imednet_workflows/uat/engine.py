"""Engine for automated UAT (User Acceptance Testing) and edit check verification."""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from imednet.spi.errors import (
    AuthenticationError,
    AuthorizationError,
    BadRequestError,
    ForbiddenError,
    NotFoundError,
    RateLimitError,
    ServerError,
    UnauthorizedError,
    ValidationError,
)
from imednet.spi.facade import ImednetFacade
from imednet.spi.utils import (
    DEFAULT_BATCH_CHUNK_SIZE,
    BatchCoordinator,
    JobFailedError,
    JobPoller,
    JobPollSummary,
    JobTimeoutError,
    PathTraversalValidationError,
    is_site_enrollment_eligible,
    mask_clinical_phi,
    redact_sensitive_payload,
    redact_sensitive_text,
    resolve_sdk_result_fetcher,
    validate_path_segment,
)
from imednet.spi.validation import (
    DataDictionary,
    ExtractedCondition,
    LogicTreeEvaluator,
    extract_variable_constraints,
)

from .submission import (
    _callable_accepts_kwarg,
    _clean_str,
    _extract_batch_result_entries,
    _get_attr_or_key,
    _is_unconfigured_mock_method,
    _subject_has_keyword,
)

logger = logging.getLogger(__name__)

_LOGIC_EVALUATOR = LogicTreeEvaluator()

_NON_RULE_ERROR_RE = re.compile(
    r"\b(?:"
    r"(?:unknown|unrecognized|missing)\s+(?:subject|site|form|interval)s?\b"
    r"|(?:subject|subjectkey|subject_key|site|sitename|site_name|form|formkey|form_key"
    r"|interval|intervalname|interval_name)\b[^.;\n]{0,80}"
    r"\b(?:not\s+found|does\s+not\s+exist|not\s+enrolled|unrecognized)\b"
    r"|invalid\s+(?:subject(?:\s*key)?|site(?:\s*name)?|form(?:\s*key)?|interval(?:\s*name)?)\b"
    r"|system\s+(?:error|failure)\b"
    r"|internal\s+server\s+error\b"
    r"|database\s+error\b"
    r")",
    re.IGNORECASE,
)

_TRANSPORT_ERROR_TYPES = (
    AuthenticationError,
    AuthorizationError,
    ForbiddenError,
    NotFoundError,
    RateLimitError,
    ServerError,
    UnauthorizedError,
)
_TRANSPORT_ERROR_STATUS_CODES = {401, 403, 404, 429}
_FAILED_RECORD_ITEM_STATUSES = {"FAIL", "FAILED", "ERROR"}


def _redacted_text(val: Any) -> str:
    """Return a string representation with sensitive tokens/URIs redacted."""
    return str(redact_sensitive_payload(redact_sensitive_text(str(val))))


def _is_non_rule_error_text(text: str) -> bool:
    """Return True if ``text`` describes a missing subject/site/form/interval or system failure."""
    return bool(text and _NON_RULE_ERROR_RE.search(text))


def _is_transport_level_error(exc: Exception) -> bool:
    """Return True for HTTP 401/403/404/429/5xx transport errors."""
    if isinstance(exc, _TRANSPORT_ERROR_TYPES):
        return True
    status_code = getattr(exc, "status_code", None)
    return isinstance(status_code, int) and (
        status_code in _TRANSPORT_ERROR_STATUS_CODES or status_code >= 500
    )


def _classify_sync_exception(exc: Exception) -> tuple[EditCheckResultStatus, str]:
    """Classify a synchronous ``create_record`` exception into status and response text."""
    if _is_transport_level_error(exc):
        status_code = getattr(exc, "status_code", None)
        if isinstance(exc, ServerError) or (isinstance(status_code, int) and status_code >= 500):
            msg = (
                f"Error: Server error {status_code}"
                if status_code is not None
                else f"Error: {_redacted_text(exc)}"
            )
            return EditCheckResultStatus.ERROR, msg
        return EditCheckResultStatus.ERROR, f"Error: {_redacted_text(exc)}"

    if isinstance(exc, (ValidationError, BadRequestError)):
        status_code = getattr(exc, "status_code", None)
        if status_code is None or status_code in {400, 422}:
            raw_msg = str(exc)
            if _is_non_rule_error_text(raw_msg):
                return EditCheckResultStatus.ERROR, f"Error: {_redacted_text(raw_msg)}"
            return EditCheckResultStatus.PASS, "Rejected"

    return EditCheckResultStatus.ERROR, f"Error: {_redacted_text(exc)}"


def _extract_item_error_text(item: Any) -> str:
    """Extract concatenated error text from a per-record JobStatus result entry."""
    if not isinstance(item, dict):
        return str(item or "")
    parts: list[str] = []
    for key in ("error", "message", "description", "detail"):
        val = item.get(key)
        if val:
            parts.append(str(val))
    errs = item.get("errors")
    if isinstance(errs, list):
        for entry in errs:
            if isinstance(entry, dict):
                parts.extend(
                    str(entry[k]) for k in ("error", "message", "description") if entry.get(k)
                )
            elif entry:
                parts.append(str(entry))
    elif errs:
        parts.append(str(errs))
    return "; ".join(parts)


@dataclass
class _StudyVerificationContext:
    """Discovered study metadata used to construct valid UAT verification payloads."""

    resolved_site_name: str
    enrollment_form_keys: set[str] = field(default_factory=set)
    enrollment_forms: list[str] = field(default_factory=list)
    scheduled_form_keys: set[str] = field(default_factory=set)
    unscheduled_form_keys: set[str] = field(default_factory=set)
    intervals_by_form: dict[str, str] = field(default_factory=dict)
    default_interval_name: str | None = None
    subjects: list[Any] | None = None
    discovery_error: Exception | None = None


@dataclass
class _ActionableRuleEntry:
    """Prepared negative-test rule entry awaiting batched submission and polling."""

    rule_idx: int
    rule: dict[str, Any]
    form_key: str
    test_data: dict[str, Any]
    result: dict[str, Any]
    is_enrollment: bool = False
    interval_name: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


class EditCheckResultStatus(str, Enum):
    """Possible outcomes for an edit check verification rule."""

    PASS = "Pass"  # noqa: S105
    FAIL = "Fail"
    ERROR = "Error"
    SKIPPED = "Skipped"


@dataclass
class EditCheckVerificationReport:
    """Consolidated report for an automated edit check verification run."""

    study_key: str
    total_rules: int = 0
    passed_rules: int = 0
    failed_rules: int = 0
    skipped_rules: int = 0
    results: list[dict[str, Any]] = field(default_factory=list)


class UATExecutionEngine:
    """Automated Edit Check Verification execution engine."""

    def __init__(
        self,
        sdk: ImednetFacade,
        data_dictionary: DataDictionary,
        *,
        poll_interval: float = 5.0,
        poll_timeout: float = 300.0,
        batch_size: int = DEFAULT_BATCH_CHUNK_SIZE,
    ) -> None:
        """Initialize the UAT execution engine.

        Args:
            sdk: An instance of the iMednet SDK facade.
            data_dictionary: The data dictionary containing business rules.
            poll_interval: Seconds between background job status polls.
            poll_timeout: Maximum seconds to wait for background jobs.
            batch_size: Maximum number of records per ``BatchCoordinator`` chunk.
        """
        if batch_size <= 0:
            raise ValueError("batch_size must be a positive integer.")
        self._sdk = sdk
        self._data_dictionary = data_dictionary
        self._poll_interval = poll_interval
        self._poll_timeout = poll_timeout
        self._batch_size = batch_size
        self._form_variables: dict[str, dict[str, Any]] = {}
        if isinstance(data_dictionary, DataDictionary):
            for form in data_dictionary.forms:
                self._form_variables[form.formKey] = data_dictionary.variables_for_form(
                    form.formKey
                )
            scoped_rows: list[Any] = []
            for form in data_dictionary.forms:
                for rule in form.businessLogic:
                    scoped_rows.append((rule, form.formKey))
            for rule in data_dictionary.unassignedBusinessLogic:
                scoped_rows.append((rule, rule.form))
            self._rules = self._parse_business_logic(scoped_rows)
        else:
            self._rules = self._parse_business_logic(data_dictionary.business_logic)

    def _parse_business_logic(self, rows: list[Any]) -> list[dict[str, Any]]:
        """Internal helper to parse raw data dictionary rows or XML rules into structured rules."""
        rules: list[dict[str, Any]] = []
        for entry in rows:
            if isinstance(entry, tuple) and len(entry) == 2:
                row, default_form = entry
            else:
                row, default_form = entry, None

            status = row.get("Status", row.get("status", "Active")) or "Active"
            if str(status).strip().lower() != "active":
                continue

            meta = _LOGIC_EVALUATOR.extract_rule_metadata(row, default_form=default_form)
            if meta is not None:
                if meta.is_ui_only or meta.is_cross_scope or not meta.conditions:
                    continue
                target = meta.conditions[-1]
                if len(meta.conditions) >= 2:
                    dep = meta.conditions[0]
                    dep_var: str | None = dep.variable
                    dep_val: str | None = (
                        "" if dep.value is None or dep.value is True else str(dep.value)
                    )
                else:
                    dep_var = target.compare_variable
                    dep_val = None

                raw_target_val = target.value
                norm_val = (
                    "" if raw_target_val is None or raw_target_val is True else str(raw_target_val)
                )

                rules.append(
                    {
                        "rule_name": meta.rule_name,
                        "form": meta.form or "UnknownForm",
                        "variable": target.variable,
                        "check_type": meta.rule_type or "Form",
                        "operator": target.operator,
                        "value": norm_val,
                        "dependency_variable": dep_var,
                        "dependency_value": dep_val,
                        "logical_operator": meta.logical_operator,
                        "xml_conditions": list(meta.conditions),
                        "severity": meta.severity,
                        "raw": row,
                    }
                )
                continue

            rule = {
                "rule_name": row.get("Name", row.get("rule_name", "Unknown Rule")),
                "form": row.get("Form", row.get("form", default_form or "UnknownForm")),
                "variable": row.get("Variable", row.get("variable")),
                "check_type": row.get("CheckType", row.get("check_type", "Range")),
                "operator": row.get("Operator", row.get("operator", "==")),
                "value": row.get("Value", row.get("value", "")),
                "dependency_variable": row.get(
                    "DependencyVariable", row.get("dependency_variable")
                ),
                "dependency_value": row.get("DependencyValue", row.get("dependency_value")),
                "raw": row,
            }
            rules.append(rule)
        return rules

    def _format_typed_value(self, form_key: str | None, var_name: str, candidate: Any) -> Any:
        """Format synthesized value according to DataDictionary Variable type when known."""
        var_def = self._form_variables.get(form_key or "", {}).get(var_name)
        if var_def is None:
            return str(candidate)
        vtype = (
            (getattr(var_def, "variable_type", getattr(var_def, "variableType", None)) or "")
            .strip()
            .lower()
        )
        if candidate == "":
            return ""
        if vtype in {"int", "integer", "number"}:
            try:
                return int(float(candidate))
            except (TypeError, ValueError):
                return candidate
        if vtype in {"float", "decimal", "slider"}:
            try:
                return float(candidate)
            except (TypeError, ValueError):
                return candidate
        if vtype in {"bool", "boolean"} and isinstance(candidate, bool):
            return candidate
        return str(candidate)

    def _apply_xml_condition(
        self,
        cond: ExtractedCondition,
        form_key: str | None,
        data: dict[str, Any],
    ) -> None:
        """Populate ``data`` so ``cond`` evaluates to True (triggering the XML edit check)."""
        var_def = self._form_variables.get(form_key or "", {}).get(cond.variable)
        constraints = extract_variable_constraints(var_def) if var_def is not None else None
        choices = constraints.choices if constraints is not None else []

        op = cond.operator
        want_match = cond.expected_truth

        if cond.compare_variable:
            rhs_var = cond.compare_variable
            if rhs_var not in data:
                data[rhs_var] = self._format_typed_value(form_key, rhs_var, 10)
            try:
                base_num = float(data[rhs_var])
            except (TypeError, ValueError):
                base_num = 10.0

            invert_map = {">": "<=", ">=": "<", "<": ">=", "<=": ">", "==": "!=", "!=": "=="}
            eff_op = op if want_match else invert_map.get(op, "!=")
            if eff_op in {">", ">="}:
                lhs_val: Any = base_num + 1
            elif eff_op in {"<", "<="}:
                lhs_val = base_num - 1
            elif eff_op == "==":
                lhs_val = data[rhs_var]
            else:
                lhs_val = base_num + 1
            data[cond.variable] = self._format_typed_value(form_key, cond.variable, lhs_val)
            return

        if (op == "empty" and want_match) or (op == "not_empty" and not want_match):
            data[cond.variable] = ""
            return

        if (op == "not_empty" and want_match) or (op == "empty" and not want_match):
            fallback = choices[0] if choices else "1"
            data[cond.variable] = self._format_typed_value(form_key, cond.variable, fallback)
            return

        raw_val = "" if cond.value is None or cond.value is True else cond.value

        if (op == "==" and want_match) or (op == "!=" and not want_match):
            data[cond.variable] = self._format_typed_value(form_key, cond.variable, raw_val)
            return

        if (op == "==" and not want_match) or (op == "!=" and want_match):
            alt_choices = [c for c in choices if c != str(raw_val)]
            if alt_choices:
                data[cond.variable] = alt_choices[0]
            else:
                try:
                    num_v = float(raw_val)
                    data[cond.variable] = self._format_typed_value(
                        form_key, cond.variable, num_v + 1
                    )
                except (TypeError, ValueError):
                    data[cond.variable] = f"NOT_{raw_val}"
            return

        if op in {">", ">=", "<", "<="}:
            invert_rel = {">": "<=", ">=": "<", "<": ">=", "<=": ">"}
            eff_rel = op if want_match else invert_rel[op]
            try:
                bound = float(raw_val)
                delta_val = bound + 1 if eff_rel in {">", ">="} else bound - 1
                data[cond.variable] = self._format_typed_value(form_key, cond.variable, delta_val)
            except (TypeError, ValueError):
                fallback_str = "9999" if eff_rel in {">", ">="} else "0"
                data[cond.variable] = fallback_str
            return

        if op in {"in", "not_in"}:
            tokens = [p.strip() for p in str(raw_val).replace("`", ",").split(",") if p.strip()]
            if (op == "in" and want_match) or (op == "not_in" and not want_match):
                chosen = tokens[0] if tokens else (choices[0] if choices else "VALID")
                data[cond.variable] = self._format_typed_value(form_key, cond.variable, chosen)
            else:
                outside = [c for c in choices if c not in set(tokens)]
                data[cond.variable] = (
                    outside[0] if outside else f"NOT_{tokens[0] if tokens else 'IN'}"
                )
            return

        if op in {"contains", "not_contains"}:
            if (op == "contains" and want_match) or (op == "not_contains" and not want_match):
                data[cond.variable] = str(raw_val)
            else:
                data[cond.variable] = "OTHER_VALUE"
            return

        data[cond.variable] = "Invalid_Test_Value"

    def _populate_other_required_fields(self, form_key: str | None, data: dict[str, Any]) -> None:
        """Populate non-targeted Required='Yes' fields on ``form_key`` with valid defaults."""
        if not form_key or form_key not in self._form_variables:
            return
        for vname, var_def in self._form_variables[form_key].items():
            if vname in data:
                continue
            constraints = extract_variable_constraints(var_def)
            if constraints.required_policy != "yes":
                continue
            if constraints.choices:
                data[vname] = constraints.choices[0]
            else:
                vtype = (
                    (
                        getattr(var_def, "variable_type", getattr(var_def, "variableType", None))
                        or ""
                    )
                    .strip()
                    .lower()
                )
                if vtype in {"int", "integer", "number"}:
                    base = int(constraints.min_value) if constraints.min_value is not None else 1
                    data[vname] = base
                elif vtype in {"float", "decimal", "slider"}:
                    base_f = constraints.min_value if constraints.min_value is not None else 1.0
                    data[vname] = base_f
                elif vtype in {"bool", "boolean"}:
                    data[vname] = True
                elif vtype in {"yes/no", "yesno"}:
                    data[vname] = "Yes"
                elif vtype in {"date", "date/time precision"}:
                    data[vname] = "2026-01-01"
                else:
                    max_l = constraints.max_length or 5
                    data[vname] = "X" * min(max_l, 5)

    def generate_negative_test_case(self, rule: dict[str, Any]) -> dict[str, Any]:
        """Generate a data payload designed to violate (trigger) the given rule."""
        xml_conditions: list[ExtractedCondition] | None = rule.get("xml_conditions")
        if xml_conditions:
            data: dict[str, Any] = {}
            form_key = rule.get("form")
            combinator = str(rule.get("logical_operator", "AND")).upper()
            active_conds = xml_conditions if combinator != "OR" else [xml_conditions[-1]]
            for cond in active_conds:
                self._apply_xml_condition(cond, form_key, data)
            self._populate_other_required_fields(form_key, data)
            raw_rule = rule.get("raw")
            if (
                combinator == "OR"
                and len(xml_conditions) > 1
                and raw_rule is not None
                and not _LOGIC_EVALUATOR.evaluate_rule(raw_rule, data, form_key=form_key).triggered
            ):
                for cond in xml_conditions:
                    self._apply_xml_condition(cond, form_key, data)
            return data

        data = {}
        variable = rule.get("variable")
        if not variable:
            return data

        operator = rule.get("operator")
        value = rule.get("value")

        dep_var = rule.get("dependency_variable")
        dep_val = rule.get("dependency_value")

        if dep_var and dep_val:
            data[dep_var] = dep_val
            if operator == "==":
                data[variable] = f"NOT_{value}"
            elif operator == "!=":
                data[variable] = value
            else:
                data[variable] = "Invalid_Value"
        elif operator == ">=":
            try:
                data[variable] = str(float(value or 0) - 1)
            except (ValueError, TypeError):
                data[variable] = "0"
        elif operator == "<=":
            try:
                data[variable] = str(float(value or 0) + 1)
            except (ValueError, TypeError):
                data[variable] = "9999"
        elif operator == "==":
            data[variable] = f"NOT_{value}"
        elif operator == "!=":
            data[variable] = value
        elif operator == "Required" or str(value).lower() == "required":
            data[variable] = ""
        else:
            data[variable] = "Invalid_Test_Value"

        return data

    def _extract_job_batch_id(self, job: Any) -> str | None:
        """Extract a non-empty string ``batch_id`` from a ``create_record`` Job response."""
        raw = (
            job.get("batch_id") or job.get("batchId")
            if isinstance(job, dict)
            else (getattr(job, "batch_id", None) or getattr(job, "batchId", None))
        )
        if isinstance(raw, str):
            if not raw:
                return None
            if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
                validate_path_segment(raw, name="batch_id")
            cleaned = raw.strip()
            if not cleaned:
                return None
            validate_path_segment(cleaned, name="batch_id")
            return cleaned
        return None

    def _call_create_record(self, study_key: str, payloads: list[dict[str, Any]]) -> Any:
        """Invoke ``sdk.create_record`` passing ``skip_validation=True`` when supported."""
        if _callable_accepts_kwarg(self._sdk.create_record, "skip_validation"):
            return self._sdk.create_record(study_key, payloads, skip_validation=True)
        return self._sdk.create_record(study_key, payloads)

    def _poll_verification_batches(
        self,
        study_key: str,
        batch_ids: list[str],
        *,
        interval: float,
        timeout: float,
    ) -> JobPollSummary:
        """Poll UAT verification batches via ImednetFacade.poll_jobs with Mock fallback."""
        poll_jobs_fn = getattr(self._sdk, "poll_jobs", None)
        if callable(poll_jobs_fn) and not _is_unconfigured_mock_method(poll_jobs_fn):
            summary = poll_jobs_fn(
                study_key,
                batch_ids,
                interval=interval,
                timeout=timeout,
                fail_fast=False,
            )
            if isinstance(summary, JobPollSummary):
                return summary
        get_job_fn = getattr(self._sdk, "get_job", None)
        if _is_unconfigured_mock_method(get_job_fn):
            return JobPollSummary(
                study_key=study_key,
                results={},
                failures={
                    bid: RuntimeError("Unconfigured mock get_job method on SDK test double")
                    for bid in batch_ids
                },
                elapsed_total=0.0,
            )
        return JobPoller(
            get_job=self._sdk.get_job,
            fetch_result=resolve_sdk_result_fetcher(self._sdk),
        ).poll_many(
            study_key,
            batch_ids,
            interval=interval,
            timeout=timeout,
            fail_fast=False,
        )

    @staticmethod
    def _record_outcome(
        result: dict[str, Any],
        status: EditCheckResultStatus,
        actual_response: str,
        report: EditCheckVerificationReport,
    ) -> None:
        """Assign terminal status and increment ``report`` counters."""
        result["actual_response"] = actual_response
        result["status"] = status
        if status == EditCheckResultStatus.PASS:
            report.passed_rules += 1
        else:
            report.failed_rules += 1

    def _classify_result_entry(
        self,
        result: dict[str, Any],
        item: Any,
        report: EditCheckVerificationReport,
    ) -> None:
        """Classify a single correlated ``JobStatus.results`` entry for a rule."""
        if isinstance(item, dict):
            item_status = str(item.get("status") or "").upper()
            err_text = _extract_item_error_text(item)
            is_failed_entry = item_status in _FAILED_RECORD_ITEM_STATUSES or (
                not item_status and bool(err_text)
            )
            if is_failed_entry:
                if _is_non_rule_error_text(err_text):
                    self._record_outcome(
                        result,
                        EditCheckResultStatus.ERROR,
                        f"Error: {_redacted_text(err_text)}",
                        report,
                    )
                else:
                    self._record_outcome(result, EditCheckResultStatus.PASS, "Rejected", report)
                return
        self._record_outcome(result, EditCheckResultStatus.FAIL, "Accepted", report)

    def _classify_terminal_job_status(
        self,
        batch_results: list[dict[str, Any]],
        status: Any,
        report: EditCheckVerificationReport,
        *,
        record_errors: list[dict[str, Any]] | None = None,
        result_error: str | None = None,
        fallback_error_text: str | None = None,
    ) -> None:
        """Classify all rule entries belonging to a single polled terminal ``JobStatus``."""
        raw_state = str(getattr(status, "state", "") or "").upper()
        res_err = result_error or getattr(status, "result_error", None)
        if res_err:
            err_msg = f"Error: {_redacted_text(fallback_error_text or res_err)}"
            for res in batch_results:
                self._record_outcome(res, EditCheckResultStatus.ERROR, err_msg, report)
            return

        if raw_state == "TIMEOUT":
            for res in batch_results:
                self._record_outcome(
                    res, EditCheckResultStatus.ERROR, "Error: Job timed out", report
                )
            return

        if raw_state == "CANCELLED":
            msg = f"Error: {_redacted_text(fallback_error_text or 'Job cancelled')}"
            for res in batch_results:
                self._record_outcome(res, EditCheckResultStatus.ERROR, msg, report)
            return

        raw_results = getattr(status, "results", None)
        if isinstance(raw_results, dict):
            for key in ("records", "data", "results"):
                nested = raw_results.get(key)
                if isinstance(nested, list):
                    raw_results = nested
                    break

        if isinstance(raw_results, list) and len(raw_results) == len(batch_results):
            for res, entry in zip(batch_results, raw_results, strict=False):
                self._classify_result_entry(res, entry, report)
            return

        extracted_errors: list[str] = []
        if record_errors:
            extracted_errors.extend(_extract_item_error_text(e) for e in record_errors if e)
        if isinstance(raw_results, list):
            for item in raw_results:
                if isinstance(item, dict):
                    item_status = str(item.get("status") or "").upper()
                    txt = _extract_item_error_text(item)
                    if item_status in _FAILED_RECORD_ITEM_STATUSES or txt:
                        extracted_errors.append(txt)
        elif isinstance(raw_results, dict):
            txt = _extract_item_error_text(raw_results)
            if txt:
                extracted_errors.append(txt)

        status_msg = str(
            _get_attr_or_key(status, "message", "error", "status_message", "description") or ""
        )
        combined_err_texts = [
            t for t in [*extracted_errors, status_msg, fallback_error_text or ""] if t
        ]
        for txt in combined_err_texts:
            if _is_non_rule_error_text(txt):
                err_msg = f"Error: {_redacted_text(txt)}"
                for res in batch_results:
                    self._record_outcome(res, EditCheckResultStatus.ERROR, err_msg, report)
                return

        failed_recs = getattr(status, "failed_records", None)
        if not isinstance(failed_recs, int):
            failed_recs = getattr(status, "record_failure_count", 0) or 0
        has_failed_recs = isinstance(failed_recs, int) and failed_recs > 0
        has_record_rejections = has_failed_recs or bool(extracted_errors)

        if raw_state == "FAILED" and not has_record_rejections:
            err_msg = f"Error: {_redacted_text(fallback_error_text or 'System job failure')}"
            for res in batch_results:
                self._record_outcome(res, EditCheckResultStatus.ERROR, err_msg, report)
            return

        if has_record_rejections:
            effective_failed = failed_recs if has_failed_recs else len(extracted_errors)
            if effective_failed >= len(batch_results):
                for res in batch_results:
                    self._record_outcome(res, EditCheckResultStatus.PASS, "Rejected", report)
            else:
                err_msg = (
                    f"Error: Partial batch rejection ({effective_failed}/{len(batch_results)}) "
                    "without per-record result correlation"
                )
                for res in batch_results:
                    self._record_outcome(res, EditCheckResultStatus.ERROR, err_msg, report)
            return

        if raw_state in {"COMPLETED", "COMPLETE", "SUCCESS"}:
            for res in batch_results:
                self._record_outcome(res, EditCheckResultStatus.FAIL, "Accepted", report)
            return

        err_msg = f"Error: Unexpected job state {raw_state or 'UNKNOWN'}"
        for res in batch_results:
            self._record_outcome(res, EditCheckResultStatus.ERROR, err_msg, report)

    def _classify_polled_batch_group(
        self,
        batch_results: list[dict[str, Any]],
        batch_id: str,
        summary: Any,
        report: EditCheckVerificationReport,
    ) -> None:
        """Classify a polled asynchronous Job batch group into ``EditCheckResultStatus``."""
        failures = getattr(summary, "failures", {}) or {}
        results = getattr(summary, "results", {}) or {}

        if batch_id in failures:
            exc = failures[batch_id]
            if isinstance(exc, JobTimeoutError):
                err_msg = f"Error: {_redacted_text(exc)}"
                for res in batch_results:
                    self._record_outcome(res, EditCheckResultStatus.ERROR, err_msg, report)
                return
            if isinstance(exc, JobFailedError):
                self._classify_terminal_job_status(
                    batch_results,
                    getattr(exc, "status", None),
                    report,
                    record_errors=getattr(exc, "record_errors", None),
                    result_error=getattr(exc, "result_error", None),
                    fallback_error_text=str(exc),
                )
                return
            err_msg = f"Error: {_redacted_text(exc)}"
            for res in batch_results:
                self._record_outcome(res, EditCheckResultStatus.ERROR, err_msg, report)
            return

        if batch_id in results:
            self._classify_terminal_job_status(batch_results, results[batch_id], report)
            return

        missing_msg = f"Error: Missing poll status for batch {batch_id}"
        for res in batch_results:
            self._record_outcome(res, EditCheckResultStatus.ERROR, missing_msg, report)

    def _classify_polled_batch(
        self,
        result: dict[str, Any],
        batch_id: str,
        summary: Any,
        report: EditCheckVerificationReport,
    ) -> None:
        """Classify a single rule entry against a polled batch summary."""
        self._classify_polled_batch_group([result], batch_id, summary, report)

    def _fetch_optional_metadata_list(self, study_key: str, method_name: str) -> list[Any] | None:
        """Call an optional SDK metadata list method if configured, ignoring 404 on minimal mocks."""
        fn = getattr(self._sdk, method_name, None)
        if not callable(fn) or _is_unconfigured_mock_method(fn):
            return None
        try:
            items = fn(study_key)
        except PathTraversalValidationError:
            raise
        except NotFoundError:
            return None
        return items if isinstance(items, list) else None

    def _discover_study_context(
        self,
        study_key: str,
        site_name: str,
    ) -> _StudyVerificationContext:
        """Inspect forms, intervals, sites, and subjects when configured on ``self._sdk``."""
        requested_site = _clean_str(site_name) or "TestSite"
        ctx = _StudyVerificationContext(resolved_site_name=requested_site)
        try:
            sites = self._fetch_optional_metadata_list(study_key, "get_sites")
            forms = self._fetch_optional_metadata_list(study_key, "get_forms")
            intervals = self._fetch_optional_metadata_list(study_key, "get_intervals")
            subjects = self._fetch_optional_metadata_list(study_key, "get_subjects")
        except PathTraversalValidationError:
            raise
        except Exception as exc:
            ctx.discovery_error = exc
            return ctx

        if sites:
            matched_site: str | None = None
            for site in sites:
                cand_name = _clean_str(_get_attr_or_key(site, "site_name", "siteName"))
                if cand_name == requested_site:
                    matched_site = cand_name
                    break
            if matched_site:
                ctx.resolved_site_name = matched_site
            elif site_name == "TestSite":
                eligible = [
                    _clean_str(_get_attr_or_key(s, "site_name", "siteName"))
                    for s in sites
                    if is_site_enrollment_eligible(
                        _get_attr_or_key(s, "site_enrollment_status", "siteEnrollmentStatus")
                    )
                ]
                valid_eligible = [s for s in eligible if s]
                if valid_eligible:
                    ctx.resolved_site_name = valid_eligible[0]
                else:
                    first_site = _clean_str(_get_attr_or_key(sites[0], "site_name", "siteName"))
                    if first_site:
                        ctx.resolved_site_name = first_site

        if intervals:
            for interval in intervals:
                if bool(_get_attr_or_key(interval, "disabled")):
                    continue
                iname = _clean_str(_get_attr_or_key(interval, "interval_name", "intervalName"))
                if not iname:
                    continue
                if ctx.default_interval_name is None:
                    ctx.default_interval_name = iname
                for f_ref in _get_attr_or_key(interval, "forms") or []:
                    fk = _clean_str(_get_attr_or_key(f_ref, "form_key", "formKey"))
                    if fk and fk not in ctx.intervals_by_form:
                        ctx.intervals_by_form[fk] = iname

        if forms:
            for form in forms:
                if bool(_get_attr_or_key(form, "disabled")):
                    continue
                fk = _clean_str(_get_attr_or_key(form, "form_key", "formKey"))
                if not fk:
                    continue
                ftype = _clean_str(_get_attr_or_key(form, "form_type", "formType")) or ""
                if ftype in {"Enrollment", "Registration"}:
                    ctx.enrollment_form_keys.add(fk)
                    ctx.enrollment_forms.append(fk)
                elif bool(_get_attr_or_key(form, "unscheduled_visit", "unscheduledVisit")):
                    ctx.unscheduled_form_keys.add(fk)
                else:
                    ctx.scheduled_form_keys.add(fk)

        ctx.subjects = subjects
        return ctx

    @staticmethod
    def _extract_reusable_uat_subjects(
        subjects: list[Any] | None,
        resolved_site_name: str,
    ) -> tuple[list[str], set[str]]:
        """Return ``(reusable_uat_subject_keys, all_existing_subject_keys)``."""
        reusable: list[str] = []
        all_keys: set[str] = set()
        if not subjects:
            return reusable, all_keys
        for subj in subjects:
            sk = _clean_str(_get_attr_or_key(subj, "subject_key", "subjectKey"))
            if sk:
                all_keys.add(sk)
            if bool(_get_attr_or_key(subj, "deleted")) or not sk:
                continue
            subj_site = _clean_str(_get_attr_or_key(subj, "site_name", "siteName"))
            if subj_site and subj_site != resolved_site_name:
                continue
            if (_subject_has_keyword(subj, "UAT") or sk.upper().startswith("UAT")) and (
                sk not in reusable
            ):
                reusable.append(sk)
        return reusable, all_keys

    def _build_valid_enrollment_data(
        self,
        study_key: str,
        enroll_form_key: str,
        index: int,
    ) -> dict[str, Any]:
        """Build a valid enrollment payload ``data`` dict for enrolling a UAT subject."""
        data: dict[str, Any] = {}
        self._populate_other_required_fields(enroll_form_key, data)
        variables = self._fetch_optional_metadata_list(study_key, "get_variables")
        if variables:
            for var in variables:
                v_fk = _clean_str(_get_attr_or_key(var, "form_key", "formKey"))
                v_name = _clean_str(_get_attr_or_key(var, "variable_name", "variableName"))
                if v_fk != enroll_form_key or not v_name or v_name in data:
                    continue
                if bool(_get_attr_or_key(var, "required")):
                    v_type = (
                        _clean_str(_get_attr_or_key(var, "variable_type", "variableType")) or ""
                    ).lower()
                    if v_type in {"int", "integer", "number"}:
                        data[v_name] = 25 + index
                    elif v_type in {"date", "date/time precision"}:
                        data[v_name] = "2026-01-01"
                    else:
                        data[v_name] = f"UAT{index}"

        for rule in self._rules:
            if rule.get("form") != enroll_form_key:
                continue
            var_name = rule.get("variable")
            if not var_name:
                continue
            op = rule.get("operator")
            raw_val = rule.get("value")
            xml_conds: list[ExtractedCondition] | None = rule.get("xml_conditions")
            if xml_conds:
                target = xml_conds[-1]
                want_valid = not target.expected_truth
                inv_cond = ExtractedCondition(
                    variable=target.variable,
                    operator=target.operator,
                    value=target.value,
                    compare_variable=target.compare_variable,
                    expected_truth=want_valid,
                )
                self._apply_xml_condition(inv_cond, enroll_form_key, data)
            elif op == ">=":
                try:
                    data[var_name] = self._format_typed_value(
                        enroll_form_key, var_name, float(raw_val or 0) + 1
                    )
                except (TypeError, ValueError):
                    data[var_name] = "1"
            elif op == "<=":
                try:
                    data[var_name] = self._format_typed_value(
                        enroll_form_key, var_name, float(raw_val or 0) - 1
                    )
                except (TypeError, ValueError):
                    data[var_name] = "0"
        return data

    def _enroll_uat_subjects(
        self,
        study_key: str,
        ctx: _StudyVerificationContext,
        needed_count: int,
        pre_existing_keys: set[str],
        *,
        interval: float,
        timeout: float,
    ) -> tuple[list[str], str | None]:
        """Enroll ``needed_count`` UAT subjects on ``ctx.resolved_site_name`` via ``BatchCoordinator``."""
        enroll_fk = ctx.enrollment_forms[0]
        payloads = [
            {
                "formKey": enroll_fk,
                "siteName": ctx.resolved_site_name,
                "data": self._build_valid_enrollment_data(study_key, enroll_fk, i),
            }
            for i in range(needed_count)
        ]
        try:
            coordinator = BatchCoordinator(chunk_size=self._batch_size)
            batch_summary = coordinator.submit(
                study_key,
                payloads,
                lambda chunk: self._call_create_record(study_key, chunk),
                skip_validation=True,
            )
        except PathTraversalValidationError:
            raise
        except Exception as exc:
            return (
                [],
                f"Failed to enroll UAT subject at site '{ctx.resolved_site_name}': {_redacted_text(exc)}",
            )

        batch_ids: list[str] = []
        for job in batch_summary.jobs:
            bid = self._extract_job_batch_id(job)
            if bid is not None:
                batch_ids.append(bid)

        enrolled_keys: list[str] = []
        if batch_ids:
            poll_summary = self._poll_verification_batches(
                study_key,
                batch_ids,
                interval=interval,
                timeout=timeout,
            )
            if poll_summary.any_failed:
                first_err = (
                    next(iter(poll_summary.failures.values()))
                    if poll_summary.failures
                    else "Registration job failed"
                )
                return (
                    [],
                    f"Failed to enroll UAT subject at site '{ctx.resolved_site_name}': {_redacted_text(first_err)}",
                )
            for bid in batch_ids:
                status = poll_summary.results.get(bid)
                for entry in _extract_batch_result_entries(getattr(status, "results", None)):
                    sk = (
                        _clean_str(entry)
                        if isinstance(entry, str)
                        else _clean_str(_get_attr_or_key(entry, "subjectKey", "subject_key"))
                    )
                    if sk and sk not in pre_existing_keys and sk not in enrolled_keys:
                        enrolled_keys.append(sk)

        if len(enrolled_keys) < needed_count:
            refreshed = self._fetch_optional_metadata_list(study_key, "get_subjects")
            if refreshed:
                for subj in refreshed:
                    if bool(_get_attr_or_key(subj, "deleted")):
                        continue
                    s_site = _clean_str(_get_attr_or_key(subj, "site_name", "siteName"))
                    if s_site and s_site != ctx.resolved_site_name:
                        continue
                    sk = _clean_str(_get_attr_or_key(subj, "subject_key", "subjectKey"))
                    if sk and sk not in pre_existing_keys and sk not in enrolled_keys:
                        enrolled_keys.append(sk)

        if not enrolled_keys:
            return (
                [],
                f"Enrolled UAT subject batch completed without resolvable subjectKey at site '{ctx.resolved_site_name}'",
            )
        return enrolled_keys, None

    def _submit_entries_in_batches(
        self,
        study_key: str,
        entries: list[_ActionableRuleEntry],
        report: EditCheckVerificationReport,
    ) -> dict[str, list[dict[str, Any]]]:
        """Submit actionable rule payloads via ``BatchCoordinator`` and return ``batch_id -> results``."""
        pending_by_batch: dict[str, list[dict[str, Any]]] = {}
        if not entries:
            return pending_by_batch

        payload_to_entry: list[_ActionableRuleEntry] = list(entries)
        all_payloads = [entry.payload for entry in payload_to_entry]
        cursor = 0

        def _record_chunk_job(chunk_entries: list[_ActionableRuleEntry], job: Any) -> None:
            batch_id = self._extract_job_batch_id(job)
            if batch_id is not None:
                for entry in chunk_entries:
                    entry.result["batch_id"] = batch_id
                pending_by_batch.setdefault(batch_id, []).extend(
                    entry.result for entry in chunk_entries
                )
            else:
                for entry in chunk_entries:
                    self._record_outcome(
                        entry.result, EditCheckResultStatus.FAIL, "Accepted", report
                    )

        def _isolate_single_entries(chunk_entries: list[_ActionableRuleEntry]) -> None:
            single_coord = BatchCoordinator(chunk_size=1)
            for single_entry in chunk_entries:
                try:
                    single_summary = single_coord.submit(
                        study_key,
                        [single_entry.payload],
                        lambda one: self._call_create_record(study_key, one),
                        skip_validation=True,
                    )
                    _record_chunk_job([single_entry], single_summary.jobs[0])
                except PathTraversalValidationError:
                    raise
                except Exception as single_exc:
                    status, actual = _classify_sync_exception(single_exc)
                    self._record_outcome(single_entry.result, status, actual, report)

        def _submit_chunk(chunk: list[dict[str, Any]]) -> Any:
            nonlocal cursor
            chunk_entries = payload_to_entry[cursor : cursor + len(chunk)]
            cursor += len(chunk)
            try:
                job = self._call_create_record(study_key, chunk)
            except PathTraversalValidationError:
                raise
            except Exception as exc:
                if len(chunk_entries) > 1 and not _is_transport_level_error(exc):
                    _isolate_single_entries(chunk_entries)
                else:
                    status, actual = _classify_sync_exception(exc)
                    for entry in chunk_entries:
                        self._record_outcome(entry.result, status, actual, report)
                return {}
            _record_chunk_job(chunk_entries, job)
            return job

        coordinator = BatchCoordinator(chunk_size=self._batch_size)
        coordinator.submit(
            study_key,
            all_payloads,
            _submit_chunk,
            skip_validation=True,
        )
        return pending_by_batch

    def run_verification(
        self,
        study_key: str,
        form_scope: str | None = None,
        site_name: str = "TestSite",
        *,
        poll_interval: float | None = None,
        poll_timeout: float | None = None,
    ) -> EditCheckVerificationReport:
        """Run the end-to-end UAT verification against the EDC API."""
        validate_path_segment(study_key, name="study_key")
        report = EditCheckVerificationReport(study_key=study_key)
        actionable_entries: list[_ActionableRuleEntry] = []

        for idx, rule in enumerate(self._rules):
            if form_scope and rule.get("form") != form_scope:
                continue

            report.total_rules += 1
            form_key = str(rule.get("form") or "UnknownForm")
            test_data = self.generate_negative_test_case(rule)
            if not test_data:
                report.skipped_rules += 1
                report.results.append(
                    {
                        "rule_name": rule.get("rule_name"),
                        "status": EditCheckResultStatus.SKIPPED,
                        "reason": "Missing variable for data generation",
                    }
                )
            else:
                result: dict[str, Any] = {
                    "rule_name": rule.get("rule_name"),
                    "form": form_key,
                    "test_data": mask_clinical_phi(redact_sensitive_payload(test_data)),
                    "expected_response": "Rejected",
                }
                report.results.append(result)
                actionable_entries.append(
                    _ActionableRuleEntry(
                        rule_idx=idx,
                        rule=rule,
                        form_key=form_key,
                        test_data=test_data,
                        result=result,
                    )
                )

            if report.total_rules >= 100:
                logger.warning("Reached maximum subject limit of 100 per UAT run.")
                break

        if not actionable_entries:
            return report

        effective_interval = self._poll_interval if poll_interval is None else float(poll_interval)
        effective_timeout = self._poll_timeout if poll_timeout is None else float(poll_timeout)

        ctx = self._discover_study_context(study_key, site_name)
        if ctx.discovery_error is not None:
            status, actual = _classify_sync_exception(ctx.discovery_error)
            if status == EditCheckResultStatus.PASS:
                status = EditCheckResultStatus.ERROR
                actual = f"Error: {_redacted_text(ctx.discovery_error)}"
            for entry in actionable_entries:
                self._record_outcome(entry.result, status, actual, report)
            return report

        for entry in actionable_entries:
            if entry.form_key in ctx.enrollment_form_keys:
                entry.is_enrollment = True
            elif entry.form_key in ctx.scheduled_form_keys:
                entry.interval_name = (
                    ctx.intervals_by_form.get(entry.form_key) or ctx.default_interval_name
                )
            elif entry.form_key not in ctx.unscheduled_form_keys:
                entry.interval_name = ctx.intervals_by_form.get(entry.form_key)

        non_enroll_entries = [e for e in actionable_entries if not e.is_enrollment]
        reusable_uat_keys, pre_existing_keys = self._extract_reusable_uat_subjects(
            ctx.subjects, ctx.resolved_site_name
        )

        entries_to_submit: list[_ActionableRuleEntry] = [
            e for e in actionable_entries if e.is_enrollment
        ]
        for entry in entries_to_submit:
            entry.payload = {
                "formKey": entry.form_key,
                "siteName": ctx.resolved_site_name,
                "data": entry.test_data,
            }

        if non_enroll_entries:
            slot_counts = Counter((e.form_key, e.interval_name) for e in non_enroll_entries)
            needed_subjects = max(slot_counts.values())
            enroll_error: str | None = None
            if len(reusable_uat_keys) < needed_subjects and ctx.enrollment_forms:
                newly_enrolled, enroll_error = self._enroll_uat_subjects(
                    study_key,
                    ctx,
                    needed_subjects - len(reusable_uat_keys),
                    pre_existing_keys,
                    interval=effective_interval,
                    timeout=effective_timeout,
                )
                reusable_uat_keys.extend(newly_enrolled)

            if enroll_error and not reusable_uat_keys:
                err_msg = f"Error: {enroll_error}"
                for entry in non_enroll_entries:
                    self._record_outcome(entry.result, EditCheckResultStatus.ERROR, err_msg, report)
            else:
                slot_seen: Counter[tuple[str, str | None]] = Counter()
                for entry in non_enroll_entries:
                    slot = (entry.form_key, entry.interval_name)
                    slot_idx = slot_seen[slot]
                    slot_seen[slot] += 1
                    if reusable_uat_keys:
                        subj_key = reusable_uat_keys[slot_idx % len(reusable_uat_keys)]
                    else:
                        subj_key = f"UAT-SUBJ-{entry.rule_idx}"
                    payload: dict[str, Any] = {
                        "formKey": entry.form_key,
                        "subjectKey": subj_key,
                        "data": entry.test_data,
                    }
                    if entry.interval_name:
                        payload["intervalName"] = entry.interval_name
                    entry.payload = payload
                    entries_to_submit.append(entry)

        entries_to_submit.sort(key=lambda e: e.rule_idx)
        pending_by_batch = self._submit_entries_in_batches(study_key, entries_to_submit, report)

        if pending_by_batch:
            batch_ids = list(pending_by_batch.keys())
            summary = self._poll_verification_batches(
                study_key,
                batch_ids,
                interval=effective_interval,
                timeout=effective_timeout,
            )
            for batch_id, batch_results in pending_by_batch.items():
                self._classify_polled_batch_group(batch_results, batch_id, summary, report)

        return report
