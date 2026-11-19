"""Engine for automated UAT (User Acceptance Testing) and edit check verification."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from imednet.spi.errors import ApiError, ClientError
from imednet.spi.facade import AsyncImednetFacade, ImednetFacade
from imednet.spi.validation import DataDictionary

from .._guards import ensure_async_sdk, ensure_sync_sdk

logger = logging.getLogger(__name__)
_OWNER = "UATExecutionEngine"


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

    def __init__(self, sdk: ImednetFacade | AsyncImednetFacade, data_dictionary: DataDictionary):
        """Initialize the UAT execution engine.

        Args:
            sdk: An instance of the iMednet SDK facade.
            data_dictionary: The data dictionary containing business rules.
        """
        self._sdk = sdk
        self._data_dictionary = data_dictionary
        self._rules = self._parse_business_logic(data_dictionary.business_logic)

    def _parse_business_logic(self, rows: list[dict[str, str]]) -> list[dict[str, Any]]:
        """Internal helper to parse raw data dictionary rows into structured rules."""
        rules = []
        for row in rows:
            status = row.get("Status", "Active")
            if status.lower() != "active":
                continue

            rule = {
                "rule_name": row.get("Name", row.get("rule_name", "Unknown Rule")),
                "form": row.get("Form", row.get("form", "UnknownForm")),
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

    def generate_negative_test_case(self, rule: dict[str, Any]) -> dict[str, Any]:
        """Generate a data payload designed to violate the given rule."""
        data = {}  # type: ignore
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

    def _prepare_rule_submission(
        self,
        idx: int,
        rule: dict[str, Any],
        report: EditCheckVerificationReport,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Prepare payload and result scaffold for a single rule, or record SKIPPED."""
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
            return None

        payload = {
            "formKey": form_key,
            "subjectKey": f"UAT-SUBJ-{idx}",
            "data": test_data,
        }
        result = {
            "rule_name": rule.get("rule_name"),
            "form": form_key,
            "test_data": test_data,
            "expected_response": "Rejected",
        }
        return payload, result

    @staticmethod
    def _record_rule_accepted(result: dict[str, Any], report: EditCheckVerificationReport) -> None:
        """Record a rule failure when an invalid negative test record is accepted."""
        result["actual_response"] = "Accepted"
        result["status"] = EditCheckResultStatus.FAIL
        report.failed_rules += 1
        report.results.append(result)

    @staticmethod
    def _record_rule_exception(
        exc: Exception,
        result: dict[str, Any],
        report: EditCheckVerificationReport,
    ) -> None:
        """Record a rule outcome from an API or unexpected exception."""
        if isinstance(exc, (ApiError, ClientError)):
            status_code = getattr(exc, "status_code", None)
            if status_code and status_code >= 500:
                result["actual_response"] = f"Error: Server error {status_code}"
                result["status"] = EditCheckResultStatus.ERROR
                report.failed_rules += 1
            else:
                result["actual_response"] = "Rejected"
                result["status"] = EditCheckResultStatus.PASS
                report.passed_rules += 1
        else:
            result["actual_response"] = f"Error: {exc}"
            result["status"] = EditCheckResultStatus.ERROR
            report.failed_rules += 1
        report.results.append(result)

    def run_verification(
        self, study_key: str, form_scope: str | None = None, site_name: str = "TestSite"
    ) -> EditCheckVerificationReport:
        """Run the end-to-end UAT verification synchronously against the EDC API."""
        from typing import cast

        ensure_sync_sdk(self._sdk, _OWNER, "run_verification", "async_run_verification")
        sdk = cast("ImednetFacade", self._sdk)
        report = EditCheckVerificationReport(study_key=study_key)

        for idx, rule in enumerate(self._rules):
            if form_scope and rule.get("form") != form_scope:
                continue

            prepared = self._prepare_rule_submission(idx, rule, report)
            if prepared is not None:
                payload, result = prepared
                try:
                    sdk.create_record(study_key, [payload])
                    self._record_rule_accepted(result, report)
                except Exception as exc:
                    self._record_rule_exception(exc, result, report)

            if report.total_rules >= 100:
                logger.warning("Reached maximum subject limit of 100 per UAT run.")
                break

        return report

    async def async_run_verification(
        self, study_key: str, form_scope: str | None = None, site_name: str = "TestSite"
    ) -> EditCheckVerificationReport:
        """Asynchronously run the end-to-end UAT verification against the EDC API."""
        from typing import cast

        ensure_async_sdk(self._sdk, _OWNER, "async_run_verification", "run_verification")
        sdk = cast("AsyncImednetFacade", self._sdk)
        report = EditCheckVerificationReport(study_key=study_key)

        for idx, rule in enumerate(self._rules):
            if form_scope and rule.get("form") != form_scope:
                continue

            prepared = self._prepare_rule_submission(idx, rule, report)
            if prepared is not None:
                payload, result = prepared
                try:
                    await sdk.async_create_record(study_key, [payload])
                    self._record_rule_accepted(result, report)
                except Exception as exc:
                    self._record_rule_exception(exc, result, report)

            if report.total_rules >= 100:
                logger.warning("Reached maximum subject limit of 100 per UAT run.")
                break

        return report
