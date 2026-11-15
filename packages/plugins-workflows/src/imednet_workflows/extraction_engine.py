"""Canonical data extraction engine for iMednet.

This module provides the logic to map raw EDC records into standardized clinical
models (Adverse Events, Protocol Deviations, etc.) based on study-specific
mapping rules.
"""

from __future__ import annotations

import collections
import functools
import logging
from typing import Any, cast

from pydantic import BaseModel, Field, ValidationError

from imednet.spi.models import (
    AdverseEvent,
    AnalysisAdverseEvent,
    AnalysisLabResult,
    DeviceDeficiency,
    MappingRule,
    ProtocolDeviation,
    Record,
    StudyConfiguration,
    SubjectLevelAnalysis,
)
from imednet.spi.utils import (
    evaluate_expression,
    is_missing_value,
    parse_expression,
)

logger = logging.getLogger(__name__)

_DOMAIN_MODEL_MAP = {
    "AE": AdverseEvent,
    "PD": ProtocolDeviation,
    "DD": DeviceDeficiency,
    "ADSL": SubjectLevelAnalysis,
    "ADAE": AnalysisAdverseEvent,
    "ADLB": AnalysisLabResult,
}

_RECORD_FIELD_MAP: dict[str, str] = {name: name for name in Record.model_fields}
for _name, _field in Record.model_fields.items():
    if _field.alias:
        _RECORD_FIELD_MAP[_field.alias] = _name


class ExtractionResult(BaseModel):
    """Canonical extraction output grouped by reporting domain."""

    adverse_events: list[AdverseEvent] = Field(default_factory=list)
    protocol_deviations: list[ProtocolDeviation] = Field(default_factory=list)
    device_deficiencies: list[DeviceDeficiency] = Field(default_factory=list)
    adsl_records: list[SubjectLevelAnalysis] = Field(default_factory=list)
    adae_records: list[AnalysisAdverseEvent] = Field(default_factory=list)
    adlb_records: list[AnalysisLabResult] = Field(default_factory=list)
    validation_errors: list[dict[str, Any]] = Field(default_factory=list)


def _get_from_path(value: Any, path: str) -> Any:
    """Retrieve a nested value from a dictionary using a dot-separated path.

    Args:
        value: The dictionary to search.
        path: A dot-separated string path (e.g., "nested.field.name").

    Returns:
        The value at the path, or None if not found or if the path is invalid.
    """
    if not path:
        return None
    current = value
    for part in path.split("."):
        if isinstance(current, dict):
            if part not in current:
                return None
            current = current[part]
        else:
            return None
    return current


class _LazyRecordPayload(dict[str, Any]):
    """Lazy dictionary that serializes a Record only upon first dictionary access.

    Avoids expensive model_dump serialization for records where values
    can be resolved directly from attributes or raw record data.
    """

    def __init__(self, record: Record) -> None:
        """Initialize the lazy record payload.

        Args:
            record: The raw Record model instance.
        """
        super().__init__()
        self._record = record
        self._populated = False

    def _ensure_populated(self) -> None:
        """Populate the dictionary from the record model if not already done."""
        if not self._populated:
            self._populated = True
            self.update(self._record.model_dump(by_alias=False))
            self.update(self._record.model_dump(by_alias=True))

    def __getitem__(self, key: Any) -> Any:
        self._ensure_populated()
        return super().__getitem__(key)

    def __contains__(self, key: Any) -> bool:
        self._ensure_populated()
        return super().__contains__(key)

    def get(self, key: Any, default: Any = None) -> Any:
        self._ensure_populated()
        return super().get(key, default)

    def __iter__(self) -> Any:
        self._ensure_populated()
        return super().__iter__()

    def __len__(self) -> int:
        self._ensure_populated()
        return super().__len__()

    def items(self) -> Any:
        self._ensure_populated()
        return super().items()

    def keys(self) -> Any:
        self._ensure_populated()
        return super().keys()

    def values(self) -> Any:
        self._ensure_populated()
        return super().values()


def _extract_rule_value_from_payload(
    record: Record, rule: MappingRule, top_level_payload: dict[str, Any] | None = None
) -> Any:
    """Extract the value for a mapping rule from a record's data or payload.

    Args:
        record: The raw Record model.
        rule: The MappingRule to apply.
        top_level_payload: The flattened or lazy payload of the record.

    Returns:
        The extracted value or None.
    """
    source_path = rule.source_variable_name
    if not source_path:
        return None

    if source_path.startswith("recordData."):
        return _get_from_path(record.record_data, source_path[len("recordData.") :])

    if source_path.startswith("record_data."):
        return _get_from_path(record.record_data, source_path[len("record_data.") :])

    # Direct resolution from record fields or aliases
    field_name = _RECORD_FIELD_MAP.get(source_path)
    if field_name is not None and hasattr(record, field_name):
        val = getattr(record, field_name)
        if val is not None:
            return val
    elif hasattr(record, source_path):
        val = getattr(record, source_path)
        if val is not None:
            return val

    # Direct lookup in record_data for unprefixed identifiers
    if (
        "." not in source_path
        and isinstance(record.record_data, dict)
        and source_path in record.record_data
    ):
        return record.record_data[source_path]

    # Fall back to top-level payload for dotted paths or aliases not present in raw record data
    if top_level_payload is None:
        top_level_payload = _LazyRecordPayload(record)
    value = _get_from_path(top_level_payload, source_path)
    if value is not None:
        return value

    if "." not in source_path and isinstance(record.record_data, dict):
        return record.record_data.get(source_path)

    return None


def _group_mappings_by_domain_and_form(
    study_configuration: StudyConfiguration,
) -> dict[str, dict[str, list[MappingRule]]]:
    """Organize study mappings by their target domain and source form.

    Returns:
        A nested dictionary mapping domain -> form_key -> list of rules.
    """
    grouped: dict[str, dict[str, list[MappingRule]]] = {}
    for rule in study_configuration.mappings:
        domain_key = rule.domain.upper()
        if domain_key not in _DOMAIN_MODEL_MAP:
            continue
        grouped.setdefault(domain_key, {}).setdefault(rule.source_form_key, []).append(rule)
    return grouped


class SubjectContext:
    """Contextual state maintained for a subject during extraction.

    Used to track longitudinal state and baseline values across multiple
    records for the same subject.
    """

    def __init__(self, subject_key: str):
        """Initialize the subject context.

        Args:
            subject_key: Unique identifier for the subject.
        """
        self.subject_key = subject_key
        self.state: dict[str, Any] = {}
        self.baseline: dict[str, Any] = {}


@functools.lru_cache(maxsize=1024)
def _compile_business_logic(logic: str) -> Any:
    """Compile a business logic Python expression into a code object.

    Args:
        logic: Python expression string to compile.

    Returns:
        The compiled code object.
    """
    return parse_expression(logic)


def _evaluate_business_logic(
    logic: str | Any,
    record: Record,
    payload: dict[str, Any],
    context: SubjectContext,
    value: Any = None,
) -> Any:
    """Safely evaluate Python business logic for a mapping rule.

    Args:
        logic: Python expression string to evaluate.
        record: The current Record being processed.
        payload: The partially built canonical payload.
        context: The SubjectContext for the current subject.
        value: The current value extracted before logic application.

    Returns:
        The result of the evaluation, or None if it fails.
    """
    try:
        code = _compile_business_logic(logic) if isinstance(logic, str) else logic
        # evaluate safely
        env = {
            "record": record,
            "payload": payload,
            "context": context,
            "state": context.state,
            "baseline": context.baseline,
            "subject_key": context.subject_key,
            "value": value,
        }
        return evaluate_expression(code, env, raise_on_error=True)
    except Exception as e:
        logger.warning(f"Derivation logic failed for {context.subject_key}: {e}")
        return None


def extract_canonical_records(
    records: list[Record], study_configuration: StudyConfiguration
) -> ExtractionResult:
    """Extract canonical models from raw records using study mappings."""
    result = ExtractionResult()
    grouped_mappings = _group_mappings_by_domain_and_form(study_configuration)

    # Subject-centric processing: group records by subject, sort chronologically
    subject_records: dict[str, list[Record]] = collections.defaultdict(list)
    for record in records:
        subject_records[record.subject_key].append(record)  # type: ignore

    for subject_key, s_records in subject_records.items():
        # sort by date_created to simulate longitudinal timeline
        s_records.sort(key=lambda r: r.date_created or r.date_updated)  # type: ignore

        context = SubjectContext(subject_key)

        # We will collect ADSL components iteratively, emitting one ADSL record per subject at the end
        adsl_payload: dict[str, Any] = {"subjectKey": subject_key}
        has_adsl_rules = False

        for record in s_records:
            top_level_payload = _LazyRecordPayload(record)

            for domain, by_form in grouped_mappings.items():
                rules = by_form.get(record.form_key)  # type: ignore
                if not rules:
                    continue

                payload: dict[str, Any] = {}
                for rule in rules:
                    # 1. Source value extraction
                    if rule.source_variable_name:
                        value = _extract_rule_value_from_payload(record, rule, top_level_payload)
                    else:
                        value = None

                    if is_missing_value(value) and rule.fallback_value is not None:
                        value = rule.fallback_value

                    # 2. Business Logic Execution
                    if rule.business_logic:
                        derived = _evaluate_business_logic(
                            rule.business_logic, record, payload, context, value
                        )
                        if derived is not None:
                            value = derived

                    payload[rule.target_field] = value

                    # Update ADSL payload if domain is ADSL
                    if domain == "ADSL":
                        has_adsl_rules = True
                        if value is not None:
                            adsl_payload[rule.target_field] = value

                    # Update state context for cross-dataset dependency
                    context.state[f"{domain}.{rule.target_field}"] = value

                    # Update baseline if marked
                    if getattr(rule, "is_baseline", False) and value is not None:
                        baseline_key = f"{domain}.{rule.target_field}"
                        if baseline_key not in context.baseline:
                            context.baseline[baseline_key] = value

                # ADSL is subject-level, we accumulate and create it at the end
                if domain == "ADSL":
                    continue

                model_type = _DOMAIN_MODEL_MAP[domain]
                try:
                    model_instance = model_type(**payload)
                except ValidationError as exc:
                    result.validation_errors.append(
                        {
                            "recordId": cast(str, record.record_id),
                            "formKey": record.form_key,
                            "domain": domain,
                            "payload": payload,
                            "errors": exc.errors(),
                        }
                    )
                    continue

                if domain == "AE":
                    result.adverse_events.append(model_instance)
                elif domain == "PD":
                    result.protocol_deviations.append(model_instance)
                elif domain == "DD":
                    result.device_deficiencies.append(model_instance)
                elif domain == "ADAE":
                    result.adae_records.append(model_instance)
                elif domain == "ADLB":
                    result.adlb_records.append(model_instance)

        if has_adsl_rules:
            try:
                adsl_instance = SubjectLevelAnalysis(**adsl_payload)
                result.adsl_records.append(adsl_instance)
            except ValidationError as exc:
                result.validation_errors.append(
                    {
                        "recordId": "ADSL",
                        "formKey": "N/A",
                        "domain": "ADSL",
                        "payload": adsl_payload,
                        "errors": exc.errors(),
                    }
                )

    return result


__all__ = ["ExtractionResult", "extract_canonical_records"]
