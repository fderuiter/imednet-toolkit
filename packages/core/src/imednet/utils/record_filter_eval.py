"""Client-side Record filter and recordDataFilter compilation and evaluation."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any

from imednet.errors.validation import ConfigurationError
from imednet.models.records import Record
from imednet.utils.dates import parse_iso_datetime
from imednet.utils.filters import (
    _camel_to_snake,
    _parse_clause_operator,
    _snake_to_camel,
    _split_filter_clauses,
    _unquote_filter_scalar,
    build_filter_string,
    validate_filter_key,
    validate_filter_syntax,
    validate_record_filter,
)

RECORDS_FILTER_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "intervalId",
        "formId",
        "formKey",
        "siteId",
        "recordId",
        "recordOid",
        "recordType",
        "recordStatus",
        "deleted",
        "dateCreated",
        "dateModified",
        "lastUpdated",
        "subjectId",
        "subjectOid",
        "subjectKey",
        "visitId",
        "parentRecordId",
    }
)

_MISSING = object()
_IGNORED_PASSTHROUGH_KEYS = frozenset({"page", "size", "sort"})


@dataclass(frozen=True, slots=True)
class _CompiledClause:
    """Single compiled comparison predicate for Record metadata or recordData."""

    field: str
    operator: str
    expected: str
    was_quoted: bool
    pattern: re.Pattern[str] | None = None


def _compile_clause_group(
    raw_expr: str,
    *,
    allow_regex: bool,
) -> tuple[str, tuple[_CompiledClause, ...]]:
    """Validate and compile a ';' (AND) or ',' (OR) filter string into clauses."""
    if allow_regex:
        validate_record_filter(raw_expr)
    else:
        validate_filter_syntax(raw_expr)

    stripped = raw_expr.strip()
    if not stripped or stripped.lower() == "none":
        return "and", ()

    raw_clauses, connector_char = _split_filter_clauses(stripped)
    compiled: list[_CompiledClause] = []
    for raw_clause in raw_clauses:
        parsed = _parse_clause_operator(raw_clause)
        if parsed is None:
            raise ValueError(f"Invalid filter clause '{raw_clause}': missing comparison operator")
        attr, op, raw_val = parsed
        validate_filter_key(attr)
        if "." in attr and any(not part for part in attr.split(".")):
            raise ValueError(f"Invalid dotted filter key '{attr}': empty path segment")
        normalized_op = "==" if op == "=" else op
        text_val, was_quoted = _unquote_filter_scalar(raw_val, raw_expr)
        regex_obj: re.Pattern[str] | None = None
        if normalized_op == "=~":
            try:
                regex_obj = re.compile(text_val, re.IGNORECASE)
            except re.error as exc:
                raise ValueError(f"Invalid regular expression '{text_val}': {exc}") from exc
        compiled.append(
            _CompiledClause(
                field=attr,
                operator=normalized_op,
                expected=text_val,
                was_quoted=was_quoted,
                pattern=regex_obj,
            )
        )

    connector = "or" if connector_char == "," else "and"
    return connector, tuple(compiled)


def _try_float(text: str) -> float | None:
    """Parse finite float token, rejecting booleans and NaN/Inf."""
    stripped = text.strip()
    if not stripped:
        return None
    try:
        value = float(stripped)
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    return value


def _try_utc_datetime(value: Any) -> datetime | None:
    """Convert a datetime, date, or ISO-8601 string to a timezone-aware UTC datetime."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if isinstance(value, str):
        stripped = value.strip()
        if len(stripped) < 10 or not stripped[:4].isdigit():
            return None
        try:
            parsed = parse_iso_datetime(stripped)
        except ValueError:
            return None
        if parsed is None:
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    return None


def _compare_values(op: str, left: Any, right: Any) -> bool:
    """Apply equality or ordering comparison between two normalized values."""
    if op == "==":
        return bool(left == right)
    if op == "!=":
        return bool(left != right)
    if op == "<":
        return bool(left < right)
    if op == "<=":
        return bool(left <= right)
    if op == ">":
        return bool(left > right)
    if op == ">=":
        return bool(left >= right)
    return False


def _evaluate_clause_value(actual: Any, clause: _CompiledClause) -> bool:
    """Evaluate one compiled clause against an extracted Python value."""
    if actual is None:
        return False
    if isinstance(actual, Enum):
        actual = actual.value

    op = clause.operator
    expected = clause.expected

    if op == "=~":
        if isinstance(actual, (Mapping, list, tuple, set)):
            return False
        assert clause.pattern is not None
        text_actual = actual.isoformat() if isinstance(actual, (datetime, date)) else str(actual)
        return bool(clause.pattern.search(text_actual))

    if isinstance(actual, (Mapping, list, tuple, set)):
        return op == "!="

    if isinstance(actual, bool):
        lowered = expected.strip().lower()
        if lowered == "true":
            return _compare_values(op, actual, True)
        if lowered == "false":
            return _compare_values(op, actual, False)
        return op == "!="

    if isinstance(actual, (int, float)):
        exp_num = _try_float(expected)
        if exp_num is None:
            return op == "!="
        return _compare_values(op, float(actual), exp_num)

    expected_dt = _try_utc_datetime(expected)
    if expected_dt is not None:
        actual_dt = _try_utc_datetime(actual)
        if actual_dt is not None:
            return _compare_values(op, actual_dt, expected_dt)
        if not clause.was_quoted and op in ("<", "<=", ">", ">="):
            return False

    actual_str = actual.isoformat() if isinstance(actual, (datetime, date)) else str(actual)
    if not clause.was_quoted and op in ("<", "<=", ">", ">="):
        exp_num = _try_float(expected)
        if exp_num is not None:
            act_num = _try_float(actual_str)
            if act_num is None:
                return False
            return _compare_values(op, act_num, exp_num)

    return _compare_values(op, actual_str, expected)


def _name_variants(name: str) -> tuple[str, ...]:
    """Return distinct field name candidates across original, camelCase, snake_case, and UPPER_SNAKE."""
    camel = _snake_to_camel(name)
    snake = _camel_to_snake(name)
    return tuple(dict.fromkeys((name, camel, snake, snake.upper())))


def _lookup_mapping_value(data: Mapping[str, Any], field: str) -> tuple[bool, Any]:
    """Resolve a field or dotted path against a dictionary across naming variants."""
    for candidate in _name_variants(field):
        if candidate in data:
            return True, data[candidate]

    if "." not in field:
        return False, None

    segments = field.split(".")
    current: Any = data
    for idx, seg in enumerate(segments):
        if not isinstance(current, Mapping):
            return False, None
        if idx > 0:
            remainder = ".".join(segments[idx:])
            for candidate in _name_variants(remainder):
                if candidate in current:
                    return True, current[candidate]
        found_seg = False
        for candidate in _name_variants(seg):
            if candidate in current:
                current = current[candidate]
                found_seg = True
                break
        if not found_seg:
            return False, None
    return True, current


def _lookup_record_attribute(record: Record, field: str) -> tuple[bool, Any]:
    """Resolve a top-level Record attribute across camelCase and snake_case aliases."""
    canonical = _snake_to_camel(field)
    if canonical == "deleted":
        return True, bool(record.is_deleted)

    for candidate in _name_variants(field):
        val = getattr(record, candidate, _MISSING)
        if val is not _MISSING:
            return True, val

    extra = getattr(record, "__pydantic_extra__", None)
    if isinstance(extra, Mapping):
        return _lookup_mapping_value(extra, field)
    return False, None


def _evaluate_group(
    connector: str,
    clauses: tuple[_CompiledClause, ...],
    lookup_fn: Any,
) -> bool:
    """Evaluate a compiled clause group with AND or OR semantics."""
    if not clauses:
        return True

    def _check(clause: _CompiledClause) -> bool:
        found, actual = lookup_fn(clause.field)
        if not found:
            return False
        return _evaluate_clause_value(actual, clause)

    if connector == "or":
        return any(_check(c) for c in clauses)
    return all(_check(c) for c in clauses)


@dataclass(frozen=True, slots=True)
class CompiledRecordPredicate:
    """Compiled Record metadata and recordData predicate with optional projection."""

    meta_groups: tuple[tuple[str, tuple[_CompiledClause, ...]], ...] = ()
    data_connector: str = "and"
    data_clauses: tuple[_CompiledClause, ...] = ()
    project_empty_record_data: bool = False
    always_empty: bool = False

    def matches(self, record: Record) -> bool:
        """Return True when *record* satisfies all metadata and recordData predicates."""
        if self.always_empty:
            return False

        for connector, clauses in self.meta_groups:
            if not _evaluate_group(
                connector,
                clauses,
                lambda f: _lookup_record_attribute(record, f),
            ):
                return False

        if self.data_clauses:
            raw_data = record.record_data
            if not isinstance(raw_data, Mapping):
                return False
            if not _evaluate_group(
                self.data_connector,
                self.data_clauses,
                lambda f: _lookup_mapping_value(raw_data, f),
            ):
                return False

        return True

    def apply(self, record: Record) -> Record | None:
        """Return *record* (or projected copy for ``recordDataFilter='none'``) if matched."""
        if not self.matches(record):
            return None
        if self.project_empty_record_data and record.record_data:
            return record.model_copy(update={"record_data": {}})
        return record


def compile_record_predicate(
    *,
    extra_filters: Mapping[str, Any] | None = None,
    record_data_filter: str | None = None,
) -> CompiledRecordPredicate:
    """Validate and compile ``extra_filters`` and ``record_data_filter`` for cached Records."""
    working_filters: dict[str, Any] = dict(extra_filters) if extra_filters else {}
    for ignored in _IGNORED_PASSTHROUGH_KEYS:
        working_filters.pop(ignored, None)

    embedded_rdf = working_filters.pop("recordDataFilter", None)
    if embedded_rdf is None:
        embedded_rdf = working_filters.pop("record_data_filter", None)
    effective_rdf = record_data_filter if record_data_filter is not None else embedded_rdf

    raw_filter = working_filters.pop("filter", None)
    meta_groups: list[tuple[str, tuple[_CompiledClause, ...]]] = []

    invalid_attrs: list[str] = []
    has_empty_list = False
    for key, val in working_filters.items():
        validate_filter_key(key)
        camel_key = _snake_to_camel(key)
        if camel_key not in RECORDS_FILTER_ATTRIBUTES:
            label = f"{key} ({camel_key})" if key != camel_key else camel_key
            invalid_attrs.append(label)
        if isinstance(val, list) and len(val) == 0:
            has_empty_list = True

    if invalid_attrs:
        invalid_str = ", ".join(dict.fromkeys(invalid_attrs))
        allowed_str = ", ".join(sorted(RECORDS_FILTER_ATTRIBUTES))
        raise ConfigurationError(
            f"Unsupported filter attribute(s) for RecordsEndpoint: {invalid_str}. "
            f"Allowed filter attributes: {allowed_str}."
        )

    if has_empty_list:
        return CompiledRecordPredicate(always_empty=True)

    built_filter: str | None = None
    if working_filters:
        built_filter = build_filter_string(working_filters)

    combined_raw: str | None = None
    if raw_filter is not None:
        validate_filter_syntax(raw_filter)
        stripped_raw = raw_filter.strip()
        if stripped_raw and stripped_raw.lower() != "none":
            _, raw_clauses = _compile_clause_group(stripped_raw, allow_regex=False)
            raw_invalid = [c.field for c in raw_clauses if c.field not in RECORDS_FILTER_ATTRIBUTES]
            if raw_invalid:
                invalid_str = ", ".join(dict.fromkeys(raw_invalid))
                allowed_str = ", ".join(sorted(RECORDS_FILTER_ATTRIBUTES))
                raise ConfigurationError(
                    f"Unsupported filter attribute(s) for RecordsEndpoint: {invalid_str}. "
                    f"Allowed filter attributes: {allowed_str}."
                )
            combined_raw = stripped_raw

    if built_filter:
        if combined_raw:
            merged = f"{combined_raw};{built_filter}"
            validate_filter_syntax(merged)
            combined_raw = merged
        else:
            combined_raw = built_filter

    if combined_raw:
        conn, clauses = _compile_clause_group(combined_raw, allow_regex=False)
        if clauses:
            meta_groups.append((conn, clauses))

    project_empty_data = False
    data_connector = "and"
    data_clauses: tuple[_CompiledClause, ...] = ()
    if effective_rdf is not None:
        validate_record_filter(effective_rdf)
        stripped_rdf = effective_rdf.strip()
        if stripped_rdf.lower() == "none":
            project_empty_data = True
        elif stripped_rdf:
            data_connector, data_clauses = _compile_clause_group(stripped_rdf, allow_regex=True)

    return CompiledRecordPredicate(
        meta_groups=tuple(meta_groups),
        data_connector=data_connector,
        data_clauses=data_clauses,
        project_empty_record_data=project_empty_data,
        always_empty=False,
    )


def _lookup_model_attribute(item: Any, field: str) -> tuple[bool, Any]:
    """Resolve a top-level or dotted attribute across Pydantic models and mappings."""
    if isinstance(item, Mapping):
        return _lookup_mapping_value(item, field)
    if isinstance(item, Record):
        return _lookup_record_attribute(item, field)

    for candidate in _name_variants(field):
        val = getattr(item, candidate, _MISSING)
        if val is not _MISSING:
            return True, val

    if "." in field:
        segments = field.split(".")
        current: Any = item
        for seg in segments:
            if isinstance(current, Mapping):
                found_seg, current = _lookup_mapping_value(current, seg)
                if not found_seg:
                    return False, None
                continue
            matched = False
            for candidate in _name_variants(seg):
                val = getattr(current, candidate, _MISSING)
                if val is not _MISSING:
                    current = val
                    matched = True
                    break
            if not matched:
                return False, None
        return True, current

    extra = getattr(item, "__pydantic_extra__", None)
    if isinstance(extra, Mapping):
        return _lookup_mapping_value(extra, field)
    return False, None


@dataclass(frozen=True, slots=True)
class CompiledModelPredicate:
    """Compiled client-side filter predicate for general SDK models and mappings."""

    groups: tuple[tuple[str, tuple[_CompiledClause, ...]], ...] = ()
    always_empty: bool = False

    def matches(self, item: Any) -> bool:
        """Return True when *item* satisfies all compiled filter groups."""
        if self.always_empty:
            return False
        return all(
            _evaluate_group(connector, clauses, lambda f: _lookup_model_attribute(item, f))
            for connector, clauses in self.groups
        )


def compile_model_predicate(
    extra_filters: Mapping[str, Any] | None = None,
) -> CompiledModelPredicate:
    """Compile parsed filter kwargs (from ``parse_filter_expressions``) for client-side evaluation."""
    working_filters: dict[str, Any] = dict(extra_filters) if extra_filters else {}
    for ignored in _IGNORED_PASSTHROUGH_KEYS:
        working_filters.pop(ignored, None)
    if not working_filters:
        return CompiledModelPredicate()

    raw_filter = working_filters.pop("filter", None)
    if any(isinstance(val, list) and len(val) == 0 for val in working_filters.values()):
        return CompiledModelPredicate(always_empty=True)

    built_filter = build_filter_string(working_filters) if working_filters else None
    combined_raw: str | None = None
    if raw_filter is not None:
        validate_filter_syntax(raw_filter)
        stripped_raw = raw_filter.strip()
        if stripped_raw and stripped_raw.lower() != "none":
            combined_raw = stripped_raw

    if built_filter:
        if combined_raw:
            merged = f"{combined_raw};{built_filter}"
            validate_filter_syntax(merged)
            combined_raw = merged
        else:
            combined_raw = built_filter

    groups: list[tuple[str, tuple[_CompiledClause, ...]]] = []
    if combined_raw:
        conn, clauses = _compile_clause_group(combined_raw, allow_regex=False)
        if clauses:
            groups.append((conn, clauses))

    return CompiledModelPredicate(groups=tuple(groups))


__all__ = [
    "RECORDS_FILTER_ATTRIBUTES",
    "CompiledModelPredicate",
    "CompiledRecordPredicate",
    "compile_model_predicate",
    "compile_record_predicate",
]
