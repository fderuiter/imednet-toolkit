"""Engine for automated UAT (User Acceptance Testing) and edit check verification."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from imednet.spi.errors import (
    ApiError,
    AuthenticationError,
    AuthorizationError,
    ClientError,
    ConflictError,
    NotFoundError,
    RateLimitError,
    ServerError,
)
from imednet.spi.facade import ImednetFacade
from imednet.spi.models import QueryStatus, is_site_enrollment_eligible
from imednet.spi.utils import (
    JobFailedError,
    JobPoller,
    JobPollSummary,
    JobTimeoutError,
    PathTraversalValidationError,
    validate_path_segment,
)
from imednet.spi.validation import (
    DataDictionary,
    ExtractedCondition,
    LogicTreeEvaluator,
    extract_variable_constraints,
)

from .inspector import (
    StudySchemaInspector,
    StudySnapshot,
    is_enrollment_form,
    is_unscheduled_form,
)
from .submission import is_unconfigured_mock_method, subject_has_keyword

logger = logging.getLogger(__name__)

_LOGIC_EVALUATOR = LogicTreeEvaluator()

_NON_REJECTION_HTTP_STATUSES = frozenset({401, 403, 404, 409, 429})
_NON_REJECTION_ERROR_TYPES = (
    ServerError,
    AuthenticationError,
    AuthorizationError,
    NotFoundError,
    ConflictError,
    RateLimitError,
)
_SOFT_RULE_CHECK_TYPES = frozenset(
    {"query", "auto query", "auto_query", "autoquery", "warning", "soft"}
)
_STRUCTURAL_ERROR_RE = re.compile(
    r"(?:"
    r"\b(?:unknown|missing|unregistered|required)\s+(?:\w+\s+){0,2}"
    r"(?:subject|site|interval|form|subjectkey|subject_key|sitename|site_name"
    r"|intervalname|interval_name|formkey|form_key)\b"
    r"|\binvalid\s+(?:subject|site|interval|form|subjectkey|subject_key|sitename|site_name"
    r"|intervalname|interval_name|formkey|form_key)(?:\s+(?:key|id|oid|name))?(?!\s+data)\b"
    r"|\b(?:subject|site|interval|form)(?:\s+(?:key|id|oid|name))?\b[^\n]{0,60}"
    r"\b(?:not\s+found|does\s+not\s+exist|is\s+not\s+enrolled|not\s+enrolled"
    r"|is\s+closed|not\s+eligible\s+for\s+enrollment|is\s+disabled)\b"
    r"|\b(?:subjectkey|subject_key|sitename|site_name|intervalname|interval_name"
    r"|formkey|form_key)\b[^\n]{0,40}"
    r"\b(?:required|missing|unknown|not\s+found|does\s+not\s+exist|invalid)\b"
    r")",
    re.IGNORECASE,
)


def _is_structural_error_text(text: str | None) -> bool:
    """Return True if an error message indicates missing/invalid Subject, Site, Interval, or Form."""
    if not text:
        return False
    return bool(_STRUCTURAL_ERROR_RE.search(text))


def _record_exception_error(result: dict[str, Any], exc: BaseException) -> None:
    """Populate ``actual_response``, ``status``, and ``diagnostic_context`` from ``exc``."""
    diag = getattr(exc, "diagnostic_context", None)
    status_code = getattr(exc, "status_code", None)
    details: list[str] = []
    if isinstance(status_code, int) and not isinstance(status_code, bool):
        details.append(f"HTTP {status_code}")
    if diag is not None and not type(diag).__module__.startswith("unittest.mock"):
        method = getattr(diag, "method", None)
        url = getattr(diag, "url", None)
        retry_after = getattr(diag, "retry_after", None)
        attempt_count = getattr(diag, "attempt_count", None)
        cause_type = getattr(diag, "cause_type", None)
        if any(v is not None for v in (method, url, retry_after, attempt_count, cause_type)):
            result["diagnostic_context"] = diag
        if isinstance(method, str) and method:
            details.append(method)
        if isinstance(url, str) and url:
            details.append(url)
        if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool):
            details.append(f"retry_after={retry_after}")
    suffix = f" [{' '.join(details)}]" if details else ""
    result["actual_response"] = f"Error: {exc}{suffix}"
    result["status"] = EditCheckResultStatus.ERROR


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
    ) -> None:
        """Initialize the UAT execution engine.

        Args:
            sdk: An instance of the iMednet SDK facade.
            data_dictionary: The data dictionary containing business rules.
            poll_interval: Seconds between background job status polls.
            poll_timeout: Maximum seconds to wait for background jobs.
        """
        self._sdk = sdk
        self._data_dictionary = data_dictionary
        self._poll_interval = poll_interval
        self._poll_timeout = poll_timeout
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
                        "condition_variables": [c.variable for c in meta.conditions if c.variable],
                        "severity": meta.severity,
                        "raw": row,
                    }
                )
                continue

            check_type = row.get(
                "CheckType",
                row.get("check_type", row.get("Type", row.get("type", "Range"))),
            )
            raw_sev = row.get("Severity", row.get("severity"))
            sev_norm = str(raw_sev or "").strip().lower()
            ct_norm = str(check_type or "").strip().lower()
            severity = (
                "warning"
                if sev_norm == "warning" or (not sev_norm and ct_norm in _SOFT_RULE_CHECK_TYPES)
                else "error"
            )
            target_var = row.get("Variable", row.get("variable"))
            dep_var_val = row.get("DependencyVariable", row.get("dependency_variable"))
            cond_vars = [v for v in (target_var, dep_var_val) if isinstance(v, str) and v]

            rule = {
                "rule_name": row.get("Name", row.get("rule_name", "Unknown Rule")),
                "form": row.get("Form", row.get("form", default_form or "UnknownForm")),
                "variable": target_var,
                "check_type": check_type,
                "operator": row.get("Operator", row.get("operator", "==")),
                "value": row.get("Value", row.get("value", "")),
                "dependency_variable": dep_var_val,
                "dependency_value": row.get("DependencyValue", row.get("dependency_value")),
                "condition_variables": cond_vars,
                "severity": severity,
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

    def _poll_verification_batches(
        self,
        study_key: str,
        batch_ids: list[str],
        *,
        interval: float,
        timeout: float,
    ) -> JobPollSummary:
        """Poll UAT verification batches via ImednetFacade.poll_jobs with Mock fallback."""
        if callable(getattr(self._sdk, "poll_jobs", None)):
            summary = self._sdk.poll_jobs(
                study_key,
                batch_ids,
                interval=interval,
                timeout=timeout,
                fail_fast=False,
            )
            if isinstance(summary, JobPollSummary):
                return summary
        return JobPoller(get_job=self._sdk.get_job).poll_many(
            study_key,
            batch_ids,
            interval=interval,
            timeout=timeout,
            fail_fast=False,
        )

    @staticmethod
    def _collect_job_error_texts(status: Any) -> list[str]:
        """Extract all error/diagnostic strings from a ``JobStatus`` and its ``results``."""
        texts: list[str] = []
        if status is None:
            return texts

        for attr in ("result_error", "resultError", "description"):
            raw = status.get(attr) if isinstance(status, Mapping) else getattr(status, attr, None)
            if isinstance(raw, str) and raw.strip():
                texts.append(raw.strip())

        raw_results = (
            status.get("results")
            if isinstance(status, Mapping)
            else getattr(status, "results", None)
        )

        def _harvest(node: Any) -> None:
            if isinstance(node, str):
                if node.strip():
                    texts.append(node.strip())
                return
            if isinstance(node, list):
                for item in node:
                    _harvest(item)
                return
            if isinstance(node, Mapping):
                for key in (
                    "error",
                    "errors",
                    "message",
                    "messages",
                    "description",
                    "reason",
                    "detail",
                    "statusDescription",
                    "status_description",
                ):
                    if key in node and node[key] is not None:
                        _harvest(node[key])
                for list_key in ("records", "items", "data", "results"):
                    if list_key in node and isinstance(node[list_key], list):
                        _harvest(node[list_key])

        _harvest(raw_results)
        return texts

    @classmethod
    def _extract_structural_job_error(
        cls, status: Any, fallback_text: str | None = None
    ) -> str | None:
        """Return the structural error message if ``status`` failed due to missing/invalid metadata."""
        for msg in cls._collect_job_error_texts(status):
            if _is_structural_error_text(msg):
                return msg
        if fallback_text and _is_structural_error_text(fallback_text):
            return fallback_text
        return None

    @staticmethod
    def _extract_job_record_ids(status: Any) -> set[int]:
        """Extract created/updated ``recordId`` integers from ``JobStatus.results`` when present."""
        record_ids: set[int] = set()
        if status is None:
            return record_ids
        raw_results = (
            status.get("results")
            if isinstance(status, Mapping)
            else getattr(status, "results", None)
        )
        items: list[Any] = []
        if isinstance(raw_results, list):
            items = raw_results
        elif isinstance(raw_results, Mapping):
            for k in ("records", "items", "data", "results"):
                if isinstance(raw_results.get(k), list):
                    items.extend(raw_results[k])
            items.append(raw_results)
        for entry in items:
            if not isinstance(entry, Mapping):
                continue
            raw_id = (
                entry.get("recordId")
                if entry.get("recordId") is not None
                else entry.get("record_id")
            )
            if isinstance(raw_id, int) and not isinstance(raw_id, bool):
                record_ids.add(raw_id)
            elif isinstance(raw_id, str) and raw_id.strip().isdigit():
                record_ids.add(int(raw_id.strip()))
        return record_ids

    @staticmethod
    def _extract_job_subject_key(status: Any) -> str | None:
        """Extract enrolled ``subjectKey`` from ``Job`` / ``JobStatus.results`` when present."""
        if status is None:
            return None
        raw_results = (
            status.get("results")
            if isinstance(status, Mapping)
            else getattr(status, "results", None)
        )
        candidates: list[Any] = []
        if isinstance(raw_results, list):
            candidates.extend(raw_results)
        elif isinstance(raw_results, Mapping):
            candidates.append(raw_results)
            for k in ("records", "items", "data", "results"):
                if isinstance(raw_results.get(k), list):
                    candidates.extend(raw_results[k])
        for entry in candidates:
            if not isinstance(entry, Mapping):
                continue
            sk = entry.get("subjectKey") or entry.get("subject_key")
            if isinstance(sk, str) and sk.strip():
                return sk.strip()
        return None

    @staticmethod
    def _is_query_open(query: Any) -> bool:
        """Return True when ``query`` is in ``QueryStatus.OPEN`` state."""
        status_val = (
            query.get("status") if isinstance(query, Mapping) else getattr(query, "status", None)
        )
        if status_val == QueryStatus.OPEN:
            return True
        if isinstance(status_val, str) and status_val.strip().upper() == "OPEN":
            return True
        if status_val is not None and not type(status_val).__module__.startswith("unittest.mock"):
            return False

        comments = (
            (query.get("query_comments") or query.get("queryComments"))
            if isinstance(query, Mapping)
            else (getattr(query, "query_comments", None) or getattr(query, "queryComments", None))
        )
        if isinstance(comments, list):
            if not comments:
                return True
            last = comments[-1]
            closed = (
                last.get("closed") if isinstance(last, Mapping) else getattr(last, "closed", None)
            )
            return closed is not True
        return False

    def _has_matching_open_query(
        self,
        study_key: str,
        *,
        rule: dict[str, Any],
        payload: dict[str, Any],
        job_status: Any = None,
    ) -> bool:
        """Query ``sdk.get_queries`` and return True if an open Query matches this soft rule."""
        get_queries_fn = getattr(self._sdk, "get_queries", None)
        if is_unconfigured_mock_method(get_queries_fn):
            return False

        subject_key = str(payload.get("subjectKey") or "").strip()
        if subject_key:
            try:
                queries = self._sdk.get_queries(study_key, subject_key=subject_key)
            except TypeError:
                queries = self._sdk.get_queries(study_key)
        else:
            queries = self._sdk.get_queries(study_key)

        expected_record_ids = self._extract_job_record_ids(job_status)
        rule_vars = {
            v
            for v in (
                rule.get("variable"),
                *(rule.get("condition_variables") or []),
            )
            if isinstance(v, str) and v
        }

        for q in queries:
            if not self._is_query_open(q):
                continue

            q_rec_id = (
                (q.get("record_id") if q.get("record_id") is not None else q.get("recordId"))
                if isinstance(q, Mapping)
                else (getattr(q, "record_id", None) or getattr(q, "recordId", None))
            )
            if isinstance(q_rec_id, str) and q_rec_id.strip().isdigit():
                q_rec_id = int(q_rec_id.strip())
            if (
                expected_record_ids
                and isinstance(q_rec_id, int)
                and not isinstance(q_rec_id, bool)
                and q_rec_id > 0
                and q_rec_id not in expected_record_ids
            ):
                continue

            q_subj = (
                (q.get("subject_key") or q.get("subjectKey"))
                if isinstance(q, Mapping)
                else (getattr(q, "subject_key", None) or getattr(q, "subjectKey", None))
            )
            q_subj_str = q_subj.strip() if isinstance(q_subj, str) else ""
            if subject_key and q_subj_str and q_subj_str != subject_key:
                continue

            q_var = q.get("variable") if isinstance(q, Mapping) else getattr(q, "variable", None)
            q_var_str = q_var.strip() if isinstance(q_var, str) else ""
            if rule_vars and q_var_str and q_var_str not in rule_vars:
                continue

            return True
        return False

    @staticmethod
    def _record_rejection(result: dict[str, Any], report: EditCheckVerificationReport) -> None:
        """Apply a record rejection outcome according to ``expected_response``."""
        result["actual_response"] = "Rejected"
        if result.get("expected_response") == "Rejected":
            result["status"] = EditCheckResultStatus.PASS
            report.passed_rules += 1
        else:
            result["status"] = EditCheckResultStatus.FAIL
            report.failed_rules += 1

    def _record_acceptance(
        self,
        study_key: str,
        result: dict[str, Any],
        report: EditCheckVerificationReport,
        *,
        rule: dict[str, Any],
        payload: dict[str, Any],
        job_status: Any = None,
    ) -> None:
        """Apply a record acceptance outcome, checking open Queries for warning rules."""
        if result.get("expected_response") != "Query Opened":
            result["actual_response"] = "Accepted"
            result["status"] = EditCheckResultStatus.FAIL
            report.failed_rules += 1
            return

        try:
            matched = self._has_matching_open_query(
                study_key,
                rule=rule,
                payload=payload,
                job_status=job_status,
            )
        except Exception as exc:
            _record_exception_error(result, exc)
            report.failed_rules += 1
            return

        if matched:
            result["actual_response"] = "Query Opened"
            result["status"] = EditCheckResultStatus.PASS
            report.passed_rules += 1
        else:
            result["actual_response"] = "Accepted (No Open Query)"
            result["status"] = EditCheckResultStatus.FAIL
            report.failed_rules += 1

    def _inspect_study_metadata(self, study_key: str) -> StudySnapshot | None:
        """Inspect study metadata via ``StudySchemaInspector`` when SDK metadata methods exist."""
        method_names = ("get_forms", "get_variables", "get_intervals", "get_sites")
        configured = {
            name: not is_unconfigured_mock_method(getattr(self._sdk, name, None))
            for name in method_names
        }
        if not any(configured.values()):
            return None
        try:
            if all(configured.values()):
                return StudySchemaInspector(self._sdk).inspect(study_key)

            return StudySnapshot(
                study_key=study_key,
                forms=self._sdk.get_forms(study_key) if configured["get_forms"] else [],
                variables=self._sdk.get_variables(study_key) if configured["get_variables"] else [],
                intervals=self._sdk.get_intervals(study_key) if configured["get_intervals"] else [],
                sites=self._sdk.get_sites(study_key) if configured["get_sites"] else [],
            )
        except NotFoundError:
            return None

    @staticmethod
    def _resolve_verification_site(
        study_key: str,
        snapshot: StudySnapshot | None,
        site_name: str,
    ) -> str:
        """Resolve an open, enrollment-eligible ``Site`` for verification."""
        if snapshot is None or not snapshot.sites:
            return site_name

        for site in snapshot.sites:
            if site.site_name == site_name:
                if not is_site_enrollment_eligible(site.site_enrollment_status):
                    raise ValueError(
                        f"Site '{site_name}' is not eligible for enrollment "
                        f"(status: {site.site_enrollment_status!s})."
                    )
                return site.site_name or site_name

        active_sites = snapshot.active_sites()
        if site_name == "TestSite":
            if active_sites:
                return active_sites[0].site_name or site_name
            raise ValueError(f"No enrollment-eligible open sites found in study '{study_key}'.")

        raise ValueError(f"Site '{site_name}' not found in study '{study_key}'.")

    @staticmethod
    def _resolve_active_interval(
        snapshot: StudySnapshot | None, form_key: str | None
    ) -> str | None:
        """Return the first non-disabled ``Interval`` containing ``form_key`` for scheduled forms."""
        if snapshot is None or not form_key:
            return None
        snap_form = snapshot.forms_by_key.get(form_key)
        if snap_form is not None and (
            is_enrollment_form(snap_form) or is_unscheduled_form(snap_form)
        ):
            return None
        for interval in snapshot.intervals:
            if getattr(interval, "disabled", None) is True:
                continue
            for f_summary in getattr(interval, "forms", None) or []:
                if f_summary.form_key == form_key:
                    return interval.interval_name
        return None

    def _list_study_subjects(self, study_key: str) -> list[Any]:
        """Fetch non-deleted subjects when ``get_subjects`` is configured on ``sdk``."""
        get_subj_fn = getattr(self._sdk, "get_subjects", None)
        if is_unconfigured_mock_method(get_subj_fn):
            return []
        try:
            return list(self._sdk.get_subjects(study_key, deleted=False))
        except TypeError:
            try:
                return list(self._sdk.get_subjects(study_key))
            except NotFoundError:
                return []
        except NotFoundError:
            return []

    @staticmethod
    def _find_uat_subject_at_site(
        subjects: list[Any],
        site_name: str,
        *,
        exclude_keys: set[str] | None = None,
        require_keyword: bool = True,
    ) -> str | None:
        """Find a non-deleted UAT-tagged ``subjectKey`` matching ``site_name``."""
        excluded = exclude_keys or set()
        fallback_any_site: str | None = None
        for subj in subjects:
            deleted = (
                subj.get("deleted", False)
                if isinstance(subj, Mapping)
                else getattr(subj, "deleted", False)
            )
            if deleted is True:
                continue
            if require_keyword and not subject_has_keyword(subj, "UAT"):
                continue
            key = (
                (subj.get("subject_key") or subj.get("subjectKey"))
                if isinstance(subj, Mapping)
                else (getattr(subj, "subject_key", None) or getattr(subj, "subjectKey", None))
            )
            if not isinstance(key, str) or not key.strip() or key.strip() in excluded:
                continue
            clean_key = key.strip()
            subj_site = (
                (subj.get("site_name") or subj.get("siteName"))
                if isinstance(subj, Mapping)
                else (getattr(subj, "site_name", None) or getattr(subj, "siteName", None))
            )
            subj_site_str = subj_site.strip() if isinstance(subj_site, str) else ""
            if not subj_site_str or subj_site_str == site_name:
                return clean_key
            if fallback_any_site is None:
                fallback_any_site = clean_key
        return fallback_any_site

    def _build_prerequisite_registration_data(
        self,
        form_key: str,
        snapshot: StudySnapshot | None,
    ) -> dict[str, Any]:
        """Synthesize valid baseline data for an enrollment form to register a UAT subject."""
        reg_data: dict[str, Any] = {}
        self._populate_other_required_fields(form_key, reg_data)
        if snapshot is not None:
            for var in snapshot.variables_by_form.get(form_key, []):
                if getattr(var, "deleted", None) is True or getattr(var, "disabled", None) is True:
                    continue
                vname = getattr(var, "variable_name", None)
                if not isinstance(vname, str) or not vname or vname in reg_data:
                    continue
                if getattr(var, "required", None) is not True:
                    continue
                vtype = (getattr(var, "variable_type", None) or "").strip().lower()
                if vtype in {"int", "integer", "number"}:
                    reg_data[vname] = 18
                elif vtype in {"float", "decimal", "double", "numeric", "slider"}:
                    reg_data[vname] = 50.0
                elif vtype in {"bool", "boolean"}:
                    reg_data[vname] = True
                elif vtype in {"date", "date/time precision"}:
                    reg_data[vname] = "2026-01-01"
                else:
                    reg_data[vname] = "UAT"
        return reg_data

    def _ensure_prerequisite_subject(
        self,
        study_key: str,
        *,
        snapshot: StudySnapshot | None,
        site_name: str,
        interval: float,
        timeout: float,
    ) -> str | None:
        """Reuse an existing UAT Subject at ``site_name`` or register a single prerequisite one."""
        has_get_subjects = not is_unconfigured_mock_method(getattr(self._sdk, "get_subjects", None))
        if snapshot is None and not has_get_subjects:
            return None

        existing_subjects = self._list_study_subjects(study_key)
        reused = self._find_uat_subject_at_site(existing_subjects, site_name)
        if reused is not None:
            return reused

        reg_form_key: str | None = None
        if snapshot is not None:
            enr_forms = snapshot.enrollment_forms()
            if enr_forms and enr_forms[0].form_key:
                reg_form_key = enr_forms[0].form_key
        if reg_form_key is None and isinstance(self._data_dictionary, DataDictionary):
            for dd_form in self._data_dictionary.forms:
                if is_enrollment_form(dd_form):
                    reg_form_key = dd_form.formKey
                    break

        if not reg_form_key:
            return None

        before_keys: set[str] = set()
        for subj in existing_subjects:
            sk = (
                (subj.get("subject_key") or subj.get("subjectKey"))
                if isinstance(subj, Mapping)
                else (getattr(subj, "subject_key", None) or getattr(subj, "subjectKey", None))
            )
            if isinstance(sk, str) and sk.strip():
                before_keys.add(sk.strip())

        reg_payload: dict[str, Any] = {
            "formKey": reg_form_key,
            "siteName": site_name,
            "keywords": [{"keywordName": "UAT", "keywordKey": "UAT"}],
            "data": self._build_prerequisite_registration_data(reg_form_key, snapshot),
        }
        job = self._sdk.create_record(study_key, [reg_payload])
        direct_sk = self._extract_job_subject_key(job)
        batch_id = self._extract_job_batch_id(job)
        if batch_id is not None and (
            not is_unconfigured_mock_method(getattr(self._sdk, "poll_jobs", None))
            or not is_unconfigured_mock_method(getattr(self._sdk, "get_job", None))
        ):
            reg_summary = self._poll_verification_batches(
                study_key,
                [batch_id],
                interval=interval,
                timeout=timeout,
            )
            reg_failures = getattr(reg_summary, "failures", {}) or {}
            if batch_id in reg_failures:
                raise reg_failures[batch_id]
            polled_status = (getattr(reg_summary, "results", {}) or {}).get(batch_id)
            if polled_status is not None:
                reg_state = str(getattr(polled_status, "state", "") or "").upper()
                reg_failed_recs = getattr(polled_status, "failed_records", 0)
                reg_struct_err = self._extract_structural_job_error(polled_status)
                if (
                    reg_state in {"FAILED", "TIMEOUT", "CANCELLED", "CANCELED"}
                    or (isinstance(reg_failed_recs, int) and reg_failed_recs > 0)
                    or reg_struct_err is not None
                ):
                    raise RuntimeError(
                        reg_struct_err or f"Prerequisite subject registration job {batch_id} failed"
                    )
            polled_sk = self._extract_job_subject_key(polled_status)
            if polled_sk:
                return polled_sk

        if direct_sk:
            return direct_sk

        post_subjects = self._list_study_subjects(study_key)
        newly_tagged = self._find_uat_subject_at_site(
            post_subjects,
            site_name,
            exclude_keys=before_keys,
            require_keyword=True,
        )
        if newly_tagged is not None:
            return newly_tagged
        return self._find_uat_subject_at_site(
            post_subjects,
            site_name,
            exclude_keys=before_keys,
            require_keyword=False,
        )

    def _classify_polled_batch(
        self,
        result: dict[str, Any],
        batch_id: str,
        summary: Any,
        report: EditCheckVerificationReport,
        *,
        study_key: str = "",
        rule: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Classify a polled asynchronous Job batch into ``EditCheckResultStatus``."""
        failures = getattr(summary, "failures", {}) or {}
        results = getattr(summary, "results", {}) or {}
        eff_study_key = study_key or report.study_key
        eff_rule = rule or {}
        eff_payload = payload or {}

        if batch_id in failures:
            exc = failures[batch_id]
            if isinstance(exc, JobTimeoutError):
                result["actual_response"] = f"Error: {exc}"
                result["status"] = EditCheckResultStatus.ERROR
                report.failed_rules += 1
                return
            if isinstance(exc, JobFailedError):
                status = getattr(exc, "status", None)
                raw_state = str(getattr(status, "state", "") or "").upper()
                failed_recs = getattr(status, "failed_records", 0)
                has_failed_recs = isinstance(failed_recs, int) and failed_recs > 0
                res_err = getattr(exc, "result_error", None) or getattr(
                    status, "result_error", None
                )
                is_result_fetch_error = (
                    bool(res_err)
                    and not has_failed_recs
                    and raw_state in {"COMPLETED", "COMPLETE", "SUCCESS"}
                )
                structural_err = self._extract_structural_job_error(status, str(exc))
                if (
                    raw_state in {"TIMEOUT", "CANCELLED", "CANCELED"}
                    or is_result_fetch_error
                    or structural_err is not None
                ):
                    err_detail = structural_err if structural_err is not None else str(exc)
                    result["actual_response"] = f"Error: {err_detail}"
                    result["status"] = EditCheckResultStatus.ERROR
                    report.failed_rules += 1
                else:
                    self._record_rejection(result, report)
                return
            result["actual_response"] = f"Error: {exc}"
            result["status"] = EditCheckResultStatus.ERROR
            report.failed_rules += 1
            return

        if batch_id in results:
            status = results[batch_id]
            raw_state = str(getattr(status, "state", "") or "").upper()
            failed_recs = getattr(status, "failed_records", 0)
            rec_fail_cnt = getattr(status, "record_failure_count", 0)
            has_failed_recs = (isinstance(failed_recs, int) and failed_recs > 0) or (
                isinstance(rec_fail_cnt, int)
                and not isinstance(rec_fail_cnt, bool)
                and rec_fail_cnt > 0
            )
            is_failed = getattr(status, "is_failed", None)
            structural_err = self._extract_structural_job_error(status)
            if raw_state in {"TIMEOUT", "CANCELLED", "CANCELED"}:
                label = "timed out" if raw_state == "TIMEOUT" else "cancelled"
                result["actual_response"] = f"Error: Job {label}"
                result["status"] = EditCheckResultStatus.ERROR
                report.failed_rules += 1
            elif structural_err is not None:
                result["actual_response"] = f"Error: {structural_err}"
                result["status"] = EditCheckResultStatus.ERROR
                report.failed_rules += 1
            elif (
                has_failed_recs
                or raw_state == "FAILED"
                or (isinstance(is_failed, bool) and is_failed)
            ):
                self._record_rejection(result, report)
            else:
                self._record_acceptance(
                    eff_study_key,
                    result,
                    report,
                    rule=eff_rule,
                    payload=eff_payload,
                    job_status=status,
                )
            return

        result["actual_response"] = f"Error: Missing poll status for batch {batch_id}"
        result["status"] = EditCheckResultStatus.ERROR
        report.failed_rules += 1

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
        effective_interval = self._poll_interval if poll_interval is None else float(poll_interval)
        effective_timeout = self._poll_timeout if poll_timeout is None else float(poll_timeout)

        snapshot: StudySnapshot | None = None
        prerequisite_error: Exception | None = None
        try:
            snapshot = self._inspect_study_metadata(study_key)
        except PathTraversalValidationError:
            raise
        except Exception as exc:
            prerequisite_error = exc

        resolved_site = (
            self._resolve_verification_site(study_key, snapshot, site_name)
            if prerequisite_error is None
            else site_name
        )

        report = EditCheckVerificationReport(study_key=study_key)
        pending_jobs: list[tuple[dict[str, Any], str, dict[str, Any], dict[str, Any]]] = []
        enrolled_subject_key: str | None = None
        prerequisite_checked = prerequisite_error is not None

        for idx, rule in enumerate(self._rules):
            if form_scope and rule.get("form") != form_scope:
                continue

            report.total_rules += 1
            form_key = rule.get("form")

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
                if not prerequisite_checked:
                    prerequisite_checked = True
                    try:
                        enrolled_subject_key = self._ensure_prerequisite_subject(
                            study_key,
                            snapshot=snapshot,
                            site_name=resolved_site,
                            interval=effective_interval,
                            timeout=effective_timeout,
                        )
                    except PathTraversalValidationError:
                        raise
                    except Exception as exc:
                        prerequisite_error = exc

                is_soft = rule.get("severity") == "warning"
                expected_response = "Query Opened" if is_soft else "Rejected"
                result: dict[str, Any] = {
                    "rule_name": rule.get("rule_name"),
                    "form": form_key,
                    "test_data": test_data,
                    "expected_response": expected_response,
                }

                if prerequisite_error is not None:
                    _record_exception_error(result, prerequisite_error)
                    report.failed_rules += 1
                    report.results.append(result)
                    if report.total_rules >= 100:
                        logger.warning(
                            "Reached maximum rule limit of 100 per UAT verification run."
                        )
                        break
                    continue

                snap_form = (
                    snapshot.forms_by_key.get(form_key)
                    if snapshot is not None and isinstance(form_key, str)
                    else None
                )
                payload: dict[str, Any] = {
                    "formKey": form_key,
                    "siteName": resolved_site,
                    "data": test_data,
                }
                if enrolled_subject_key is not None:
                    payload["subjectKey"] = enrolled_subject_key
                elif snap_form is not None and is_enrollment_form(snap_form):
                    payload["keywords"] = [{"keywordName": "UAT", "keywordKey": "UAT"}]
                else:
                    payload["subjectKey"] = f"UAT-SUBJ-{idx}"

                interval_name = self._resolve_active_interval(snapshot, form_key)
                if interval_name:
                    payload["intervalName"] = interval_name

                try:
                    job = self._sdk.create_record(study_key, [payload])
                    batch_id = self._extract_job_batch_id(job)
                    if batch_id is not None:
                        result["batch_id"] = batch_id
                        pending_jobs.append((result, batch_id, rule, payload))
                    else:
                        self._record_acceptance(
                            study_key,
                            result,
                            report,
                            rule=rule,
                            payload=payload,
                            job_status=job,
                        )
                except PathTraversalValidationError:
                    raise
                except (ApiError, ClientError) as e:
                    status_code = getattr(e, "status_code", None)
                    is_error_status = isinstance(status_code, int) and (
                        status_code >= 500 or status_code in _NON_REJECTION_HTTP_STATUSES
                    )
                    if (
                        isinstance(e, _NON_REJECTION_ERROR_TYPES)
                        or is_error_status
                        or _is_structural_error_text(str(e))
                    ):
                        _record_exception_error(result, e)
                        report.failed_rules += 1
                    else:
                        self._record_rejection(result, report)
                except Exception as e:
                    _record_exception_error(result, e)
                    report.failed_rules += 1

                report.results.append(result)

            if report.total_rules >= 100:
                logger.warning("Reached maximum rule limit of 100 per UAT verification run.")
                break

        if pending_jobs:
            batch_ids = list(dict.fromkeys(bid for _, bid, _, _ in pending_jobs))
            summary = self._poll_verification_batches(
                study_key,
                batch_ids,
                interval=effective_interval,
                timeout=effective_timeout,
            )
            for pending_result, batch_id, pending_rule, pending_payload in pending_jobs:
                self._classify_polled_batch(
                    pending_result,
                    batch_id,
                    summary,
                    report,
                    study_key=study_key,
                    rule=pending_rule,
                    payload=pending_payload,
                )

        return report
