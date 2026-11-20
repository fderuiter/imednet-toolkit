"""Engine for automated UAT (User Acceptance Testing) and edit check verification."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from imednet.spi.errors import ApiError, ClientError, ServerError
from imednet.spi.facade import ImednetFacade
from imednet.spi.utils import JobFailedError, JobPoller, JobPollSummary, JobTimeoutError
from imednet.spi.validation import (
    DataDictionary,
    ExtractedCondition,
    LogicTreeEvaluator,
    extract_variable_constraints,
)

logger = logging.getLogger(__name__)

_LOGIC_EVALUATOR = LogicTreeEvaluator()


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
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
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

    def _classify_polled_batch(
        self,
        result: dict[str, Any],
        batch_id: str,
        summary: Any,
        report: EditCheckVerificationReport,
    ) -> None:
        """Classify a polled asynchronous Job batch into ``EditCheckResultStatus``."""
        failures = getattr(summary, "failures", {}) or {}
        results = getattr(summary, "results", {}) or {}

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
                if raw_state == "TIMEOUT" or is_result_fetch_error:
                    result["actual_response"] = f"Error: {exc}"
                    result["status"] = EditCheckResultStatus.ERROR
                    report.failed_rules += 1
                else:
                    result["actual_response"] = "Rejected"
                    result["status"] = EditCheckResultStatus.PASS
                    report.passed_rules += 1
                return
            result["actual_response"] = f"Error: {exc}"
            result["status"] = EditCheckResultStatus.ERROR
            report.failed_rules += 1
            return

        if batch_id in results:
            status = results[batch_id]
            raw_state = str(getattr(status, "state", "") or "").upper()
            failed_recs = getattr(status, "failed_records", 0)
            has_failed_recs = isinstance(failed_recs, int) and failed_recs > 0
            is_failed = getattr(status, "is_failed", None)
            if raw_state == "TIMEOUT":
                result["actual_response"] = "Error: Job timed out"
                result["status"] = EditCheckResultStatus.ERROR
                report.failed_rules += 1
            elif (
                has_failed_recs
                or raw_state in {"FAILED", "CANCELLED"}
                or (isinstance(is_failed, bool) and is_failed)
            ):
                result["actual_response"] = "Rejected"
                result["status"] = EditCheckResultStatus.PASS
                report.passed_rules += 1
            else:
                result["actual_response"] = "Accepted"
                result["status"] = EditCheckResultStatus.FAIL
                report.failed_rules += 1
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
        del site_name
        report = EditCheckVerificationReport(study_key=study_key)
        pending_jobs: list[tuple[dict[str, Any], str]] = []

        for idx, rule in enumerate(self._rules):
            if form_scope and rule.get("form") != form_scope:
                continue

            report.total_rules += 1
            form_key = rule.get("form")

            test_data = self.generate_negative_test_case(rule)
            if not test_data:
                # If we cannot generate test data (e.g. variable is missing), mark as skipped
                report.skipped_rules += 1
                report.results.append(
                    {
                        "rule_name": rule.get("rule_name"),
                        "status": EditCheckResultStatus.SKIPPED,
                        "reason": "Missing variable for data generation",
                    }
                )
            else:
                # Use CreateNewRecordRequest format
                subject_key = f"UAT-SUBJ-{idx}"
                payload = {"formKey": form_key, "subjectKey": subject_key, "data": test_data}

                result: dict[str, Any] = {
                    "rule_name": rule.get("rule_name"),
                    "form": form_key,
                    "test_data": test_data,
                    "expected_response": "Rejected",
                }

                try:
                    job = self._sdk.create_record(study_key, [payload])
                    batch_id = self._extract_job_batch_id(job)
                    if batch_id is not None:
                        result["batch_id"] = batch_id
                        pending_jobs.append((result, batch_id))
                    else:
                        result["actual_response"] = "Accepted"
                        result["status"] = EditCheckResultStatus.FAIL
                        report.failed_rules += 1
                except (ApiError, ClientError) as e:
                    status_code = getattr(e, "status_code", None)
                    if isinstance(e, ServerError) or (
                        isinstance(status_code, int) and status_code >= 500
                    ):
                        result["actual_response"] = (
                            f"Error: Server error {status_code}"
                            if status_code is not None
                            else f"Error: {e}"
                        )
                        result["status"] = EditCheckResultStatus.ERROR
                        report.failed_rules += 1
                    else:
                        result["actual_response"] = "Rejected"
                        result["status"] = EditCheckResultStatus.PASS
                        report.passed_rules += 1
                except Exception as e:
                    result["actual_response"] = f"Error: {e}"
                    result["status"] = EditCheckResultStatus.ERROR
                    report.failed_rules += 1

                report.results.append(result)

            if report.total_rules >= 100:
                logger.warning("Reached maximum subject limit of 100 per UAT run.")
                break

        if pending_jobs:
            batch_ids = list(dict.fromkeys(bid for _, bid in pending_jobs))
            effective_interval = (
                self._poll_interval if poll_interval is None else float(poll_interval)
            )
            effective_timeout = self._poll_timeout if poll_timeout is None else float(poll_timeout)
            summary = self._poll_verification_batches(
                study_key,
                batch_ids,
                interval=effective_interval,
                timeout=effective_timeout,
            )
            for pending_result, batch_id in pending_jobs:
                self._classify_polled_batch(pending_result, batch_id, summary, report)

        return report
