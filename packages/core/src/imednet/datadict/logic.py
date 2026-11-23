"""Business logic XML parsing and AST evaluation for iMednet data dictionary."""

from __future__ import annotations

import ast
import datetime as dt
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import defusedxml.ElementTree as ET  # noqa: N817
from defusedxml.common import DefusedXmlException

from imednet.errors import EvaluationError, RestrictedEvaluationError
from imednet.utils.evaluator import (
    DEFAULT_SAFE_FUNCTIONS,
    RestrictedAstEvaluator,
    parse_expression,
)

from .normalize import normalize_variable_bounds, to_camel_case


def _parse_xml_element(elem: ET.Element) -> Any:
    """Parse an XML Element into a structured Python dictionary/value.

    Rules:
    - Empty XML elements (<true/>, <new_record/>, <last/>) become boolean True.
    - Sibling tags with identical names collapse into arrays.
    - Tags are camelCased.
    - Text values are preserved.
    """
    text = (elem.text or "").strip()
    has_children = len(elem) > 0
    has_attrib = len(elem.attrib) > 0

    # 1. Empty element without children, text, or attributes becomes boolean True
    if not has_children and not text and not has_attrib:
        return True

    # 2. Leaf element with only text (no children, no attributes)
    if not has_children and not has_attrib:
        return text

    node_dict: dict[str, Any] = {}

    # Store XML attributes prefixed with @
    for attr_name, attr_val in elem.attrib.items():
        node_dict[f"@{attr_name}"] = attr_val

    # Group children by camelCased tag name
    children_by_tag: dict[str, list[Any]] = {}
    for child in elem:
        camel_tag = to_camel_case(child.tag)
        parsed_child = _parse_xml_element(child)
        if camel_tag not in children_by_tag:
            children_by_tag[camel_tag] = []
        children_by_tag[camel_tag].append(parsed_child)

    # Collapse lists: single elements are stored directly, multiple become lists
    for camel_tag, val_list in children_by_tag.items():
        if len(val_list) == 1:
            node_dict[camel_tag] = val_list[0]
        else:
            node_dict[camel_tag] = val_list

    # If element has both text and children/attributes
    if text:
        node_dict["#text"] = text

    return node_dict


def parse_logic_xml(xml_content: str | None) -> dict[str, Any] | None:
    """Parse BusinessLogic XML into a structured AST dictionary.

    Args:
        xml_content: Verbatim XML string, or None.

    Returns:
        Structured dictionary representation of the rule logic, or None if input is empty.

    Raises:
        ValueError: If XML parsing fails due to invalid syntax.
    """
    if not xml_content or not xml_content.strip():
        return None

    stripped = xml_content.strip()
    try:
        root = ET.fromstring(stripped)
    except (ET.ParseError, DefusedXmlException) as exc:
        raise ValueError(f"Malformed business logic XML: {exc}") from exc

    parsed = _parse_xml_element(root)

    # If root is a dict and the tag is a generic rule container, unwrap its contents
    root_tag = root.tag.lower()
    if isinstance(parsed, dict) and root_tag in {
        "rule",
        "logic",
        "businesslogic",
        "business_logic",
        "root",
    }:
        return parsed

    # Otherwise return rooted under camelCased root tag
    return {to_camel_case(root.tag): parsed}


_MIN_BOUND_KEYS = (
    "minValue",
    "min_value",
    "min",
    "minimum",
    "lowerBound",
    "lower_bound",
    "rangeMin",
    "range_min",
    "textValidationMin",
    "text_validation_min",
    "hardMin",
    "hard_min",
)

_MAX_BOUND_KEYS = (
    "maxValue",
    "max_value",
    "max",
    "maximum",
    "upperBound",
    "upper_bound",
    "rangeMax",
    "range_max",
    "textValidationMax",
    "text_validation_max",
    "hardMax",
    "hard_max",
)

_MIN_LENGTH_KEYS = (
    "minLength",
    "min_length",
)

_MAX_LENGTH_KEYS = (
    "maxLength",
    "max_length",
    "length",
    "fieldLength",
    "field_length",
    "textLength",
    "text_length",
)

_COMPARE_VAR_KEYS = (
    "compareVariable",
    "otherVariable",
    "rightVariable",
    "targetVariable",
    "valueVariable",
)

_CROSS_SCOPE_KEYS = (
    "visit",
    "visitKey",
    "interval",
    "intervalName",
    "intervalId",
    "recordScope",
    "historical",
    "previousRecord",
    "acrossVisits",
    "crossForm",
    "crossRecord",
    "@visit",
    "@interval",
)

_UI_ACTION_TYPES = frozenset(
    {
        "disable",
        "enable",
        "hide",
        "show",
        "sequence",
        "auto_sequence",
        "autosequence",
        "email",
        "notify",
        "calculate",
        "set_value",
        "setvalue",
    }
)

_WARNING_ACTION_TYPES = frozenset(
    {
        "auto_query",
        "auto query",
        "autoquery",
        "warning",
        "soft",
    }
)

_QUERY_ACTION_TYPES = _WARNING_ACTION_TYPES | frozenset({"query"})

_ERROR_ACTION_TYPES = frozenset(
    {
        "error",
        "alert",
        "reject",
        "block",
        "form",
        "validation",
        "hard",
        "hard_error",
        "required",
    }
)

_OPERATOR_TAG_MAP: dict[str, str] = {
    "eq": "==",
    "equals": "==",
    "ne": "!=",
    "neq": "!=",
    "lt": "<",
    "lte": "<=",
    "le": "<=",
    "gt": ">",
    "gte": ">=",
    "ge": ">=",
    "in": "in",
    "notin": "not_in",
    "not_in": "not_in",
    "contains": "contains",
    "notcontains": "not_contains",
    "not_contains": "not_contains",
    "empty": "empty",
    "isempty": "empty",
    "notempty": "not_empty",
    "isnotempty": "not_empty",
    "regex": "regex",
}

_COMBINATOR_ATTR_NAMES = frozenset(
    {
        "operator",
        "logic",
        "logicaloperator",
        "logicoperator",
        "combinator",
        "conjunction",
        "type",
        "op",
        "join",
        "booleanoperator",
        "boolop",
        "mode",
    }
)

_OR_GROUP_KEYS = ("or", "orGroup", "any", "OR")
_AND_GROUP_KEYS = ("and", "andGroup", "all", "AND")
_NOT_GROUP_KEYS = ("not", "NOT")
_CONTAINER_GROUP_KEYS = ("condition", "conditions", "conditionGroup", "group", "if")
_SOFT_MIN_BOUND_KEYS = (
    "softMin",
    "soft_min",
    "softMinValue",
    "soft_min_value",
)

_SOFT_MAX_BOUND_KEYS = (
    "softMax",
    "soft_max",
    "softMaxValue",
    "soft_max_value",
)

_EXPLICIT_MIN_DATE_KEYS = (
    "minDate",
    "min_date",
)

_EXPLICIT_MAX_DATE_KEYS = (
    "maxDate",
    "max_date",
)

_TEMPORAL_VARIABLE_TYPES = frozenset(
    {
        "date",
        "date ymd",
        "date mdy",
        "date dmy",
        "datetime",
        "date/time",
        "date time",
        "date/time precision",
        "datetime precision",
        "date time precision",
        "datetimeprecision",
        "durationdatetime",
        "intervaldatetime",
        "time",
    }
)

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_YEAR_RE = re.compile(r"^\d{4}$")
_ISO_YEAR_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_ISO_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:?\d{2})?$"
)
_ISO_TIME_RE = re.compile(r"^\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:?\d{2})?$")
_ISO_DURATION_RE = re.compile(
    r"^P(?=\d|T\d)(\d+Y)?(\d+M)?(\d+W)?(\d+D)?(T(?=\d)(\d+H)?(\d+M)?(\d+(\.\d+)?S)?)?$"
)

VALID_RANGE_COMPARATORS = frozenset({"LT", "LE", "GT", "GE", "EQ", "NE", "IN", "NOTIN"})


def _normalize_iso_tz_suffix(raw: str) -> str:
    """Normalize trailing Z or +-HHMM offsets for stdlib fromisoformat."""
    if raw.endswith("Z"):
        return raw[:-1] + "+00:00"
    if len(raw) >= 5 and raw[-5] in "+-" and raw[-4:].isdigit():
        return f"{raw[:-2]}:{raw[-2:]}"
    return raw


def is_valid_iso_date(value: Any) -> bool:
    """Return True when ``value`` is a strict calendar ISO YYYY-MM-DD string."""
    if not isinstance(value, str) or not _ISO_DATE_RE.match(value):
        return False
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def is_valid_iso_datetime(value: Any) -> bool:
    """Return True when ``value`` is a strict calendar+clock ISO datetime string."""
    if not isinstance(value, str) or not _ISO_DATETIME_RE.match(value):
        return False
    try:
        dt.datetime.fromisoformat(_normalize_iso_tz_suffix(value))
    except ValueError:
        return False
    return True


def is_valid_iso_datetime_precision(value: Any) -> bool:
    """Return True when ``value`` is YYYY, YYYY-MM, YYYY-MM-DD, or full ISO datetime."""
    if not isinstance(value, str):
        return False
    if _ISO_YEAR_RE.match(value):
        year = int(value)
        return 1 <= year <= 9999
    if _ISO_YEAR_MONTH_RE.match(value):
        year = int(value[:4])
        return 1 <= year <= 9999
    return is_valid_iso_date(value) or is_valid_iso_datetime(value)


def is_valid_iso_time(value: Any) -> bool:
    """Return True when ``value`` is a strict 24-hour clock ISO HH:MM[:SS] string."""
    if not isinstance(value, str) or not _ISO_TIME_RE.match(value):
        return False
    try:
        dt.time.fromisoformat(_normalize_iso_tz_suffix(value))
    except ValueError:
        return False
    return True


def is_valid_iso_duration_datetime(value: Any) -> bool:
    """Return True when ``value`` is an ISO-8601 duration or ISO datetime string."""
    if not isinstance(value, str):
        return False
    return bool(_ISO_DURATION_RE.match(value)) or is_valid_iso_datetime(value)


def is_valid_iso_interval_datetime(value: Any) -> bool:
    """Return True when ``value`` is an ISO-8601 interval (start/end) or ISO datetime."""
    if not isinstance(value, str):
        return False
    if "/" in value:
        parts = value.split("/")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            return False
        left_ok = (
            is_valid_iso_date(parts[0])
            or is_valid_iso_datetime(parts[0])
            or bool(_ISO_DURATION_RE.match(parts[0]))
        )
        right_ok = (
            is_valid_iso_date(parts[1])
            or is_valid_iso_datetime(parts[1])
            or bool(_ISO_DURATION_RE.match(parts[1]))
        )
        return left_ok and right_ok
    return is_valid_iso_datetime(value)


def is_valid_iso_temporal_bound(value: Any, *, allow_partial: bool = False) -> bool:
    """Return True when ``value`` is a valid ISO date, datetime, or time bound."""
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if not stripped:
        return False
    if (
        is_valid_iso_date(stripped)
        or is_valid_iso_datetime(stripped)
        or is_valid_iso_time(stripped)
    ):
        return True
    return bool(allow_partial and is_valid_iso_datetime_precision(stripped))


def _expand_partial_date_range(raw: str) -> tuple[dt.date, dt.date] | None:
    """Expand YYYY, YYYY-MM, or YYYY-MM-DD into inclusive (start_date, end_date)."""
    if is_valid_iso_date(raw):
        parsed = dt.date.fromisoformat(raw)
        return parsed, parsed
    if _ISO_YEAR_MONTH_RE.match(raw):
        year = int(raw[:4])
        month = int(raw[5:7])
        if not (1 <= year <= 9999):
            return None
        start = dt.date(year, month, 1)
        if month == 12:
            end = dt.date(year, 12, 31)
        else:
            end = dt.date(year, month + 1, 1) - dt.timedelta(days=1)
        return start, end
    if _ISO_YEAR_RE.match(raw):
        year = int(raw)
        if not (1 <= year <= 9999):
            return None
        return dt.date(year, 1, 1), dt.date(year, 12, 31)
    return None


def _eval_ordered_pair(lhs: Any, op: str, rhs: Any) -> bool | None:
    if op in {"==", "EQ"}:
        return bool(lhs == rhs)
    if op in {"!=", "NE"}:
        return bool(lhs != rhs)
    if op in {"<", "LT"}:
        return bool(lhs < rhs)
    if op in {"<=", "LE"}:
        return bool(lhs <= rhs)
    if op in {">", "GT"}:
        return bool(lhs > rhs)
    if op in {">=", "GE"}:
        return bool(lhs >= rhs)
    return None


def compare_temporal_strings(actual: str, op: str, expected: str) -> bool | None:
    """Compare two validated ISO temporal strings accurately across date/datetime/time."""
    act = actual.strip()
    exp = expected.strip()

    if is_valid_iso_time(act) and is_valid_iso_time(exp):
        t_act = dt.time.fromisoformat(_normalize_iso_tz_suffix(act)).replace(tzinfo=None)
        t_exp = dt.time.fromisoformat(_normalize_iso_tz_suffix(exp)).replace(tzinfo=None)
        return _eval_ordered_pair(t_act, op, t_exp)

    if is_valid_iso_datetime(act) and is_valid_iso_datetime(exp):
        dt_act = dt.datetime.fromisoformat(_normalize_iso_tz_suffix(act))
        dt_exp = dt.datetime.fromisoformat(_normalize_iso_tz_suffix(exp))
        if (dt_act.tzinfo is None) != (dt_exp.tzinfo is None):
            dt_act = (
                dt_act.replace(tzinfo=dt.UTC)
                if dt_act.tzinfo is None
                else dt_act.astimezone(dt.UTC)
            )
            dt_exp = (
                dt_exp.replace(tzinfo=dt.UTC)
                if dt_exp.tzinfo is None
                else dt_exp.astimezone(dt.UTC)
            )
        return _eval_ordered_pair(dt_act, op, dt_exp)

    if is_valid_iso_datetime(act) and is_valid_iso_date(exp):
        d_act = dt.datetime.fromisoformat(_normalize_iso_tz_suffix(act)).date()
        d_exp = dt.date.fromisoformat(exp)
        return _eval_ordered_pair(d_act, op, d_exp)

    if is_valid_iso_date(act) and is_valid_iso_datetime(exp):
        d_act = dt.date.fromisoformat(act)
        d_exp = dt.datetime.fromisoformat(_normalize_iso_tz_suffix(exp)).date()
        return _eval_ordered_pair(d_act, op, d_exp)

    act_range = _expand_partial_date_range(act)
    exp_range = _expand_partial_date_range(exp)
    if act_range is not None and exp_range is not None:
        if act_range[0] == act_range[1] and exp_range[0] == exp_range[1]:
            return _eval_ordered_pair(act_range[0], op, exp_range[0])
        # Partial precision date comparison: check if partial span satisfies bound
        if op in ("<=", "LE"):
            return act_range[0] <= exp_range[1]
        if op in ("<", "LT"):
            return act_range[1] < exp_range[0]
        if op in (">=", "GE"):
            return act_range[1] >= exp_range[0]
        if op in (">", "GT"):
            return act_range[0] > exp_range[1]
        if op in ("==", "EQ"):
            return act == exp
        if op in ("!=", "NE"):
            return act != exp
    return None


_NON_CONDITION_RULE_KEYS = frozenset(
    {
        "actions",
        "action",
        "then",
        "message",
        "errorMessage",
        "description",
        "id",
        "name",
        "status",
        "form",
        "odmElement",
        "methodType",
        "formalExpressions",
        "aliases",
        "compiledExpression",
        "variableMap",
        "checkboxMap",
        "targetVariables",
        "sourceFormat",
        "branchingExpression",
        "compiledBranching",
        "referencedForms",
    }
)


@dataclass(frozen=True)
class RangeCheckConstraint:
    """Normalized ODM RangeCheck or validation bound constraint."""

    comparator: str | None = None
    soft_hard: Literal["Hard", "Soft"] = "Hard"
    check_value: str | None = None
    check_values: tuple[str, ...] = ()
    formal_expression: str | None = None
    context: str | None = None
    measurement_unit_oid: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class VariableConstraints:
    """Normalized validation constraints extracted from a DataDictionary Variable."""

    choices: list[str] = field(default_factory=list)
    required_policy: Literal["yes", "auto_query", "no"] = "no"
    min_value: float | None = None
    max_value: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    min_date: str | None = None
    max_date: str | None = None
    soft_min_value: float | None = None
    soft_max_value: float | None = None
    range_checks: tuple[RangeCheckConstraint, ...] = ()


def _lookup_attr_or_extra(var: Any, keys: tuple[str, ...]) -> Any:
    """Look up the first non-empty attribute or extraAttributes entry matching ``keys``."""
    extra: Mapping[str, Any] = getattr(var, "extraAttributes", None) or {}
    lower_extra = {str(k).lower(): v for k, v in extra.items()}
    for key in keys:
        if hasattr(var, key):
            val = getattr(var, key)
            if val is not None and val != "" and not callable(val):
                return val
        if key in extra and extra[key] is not None and extra[key] != "":
            return extra[key]
        low = key.lower()
        if low in lower_extra and lower_extra[low] is not None and lower_extra[low] != "":
            return lower_extra[low]
    return None


def _to_finite_float(value: Any) -> float | None:
    """Convert numeric or string input into a finite float, rejecting booleans."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        fval = float(value)
        return fval if math.isfinite(fval) else None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            fval = float(stripped)
        except ValueError:
            return None
        return fval if math.isfinite(fval) else None
    return None


def _to_non_negative_int(value: Any) -> int | None:
    """Convert numeric or string input into a non-negative integer, rejecting booleans."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
    return None


def _extract_temporal_bound(
    var: Any,
    explicit_keys: tuple[str, ...],
    fallback_keys: tuple[str, ...],
    *,
    is_temporal_var: bool,
) -> str | None:
    raw_explicit = _lookup_attr_or_extra(var, explicit_keys)
    if isinstance(raw_explicit, str) and is_valid_iso_temporal_bound(
        raw_explicit, allow_partial=True
    ):
        return raw_explicit.strip()

    raw_fallback = _lookup_attr_or_extra(var, fallback_keys)
    if isinstance(raw_fallback, str):
        candidate = raw_fallback.strip()
        if is_temporal_var and is_valid_iso_temporal_bound(candidate, allow_partial=True):
            return candidate
        if (("-" in candidate and not candidate.startswith("-")) or ":" in candidate) and (
            is_valid_iso_temporal_bound(candidate, allow_partial=False)
        ):
            return candidate
    return None


def _parse_range_checks(extra_attrs: Mapping[str, Any] | None) -> tuple[RangeCheckConstraint, ...]:
    """Normalize explicit ODM rangeChecks via normalize_variable_bounds."""
    if not isinstance(extra_attrs, Mapping) or not extra_attrs.get("rangeChecks"):
        return ()

    normalized = normalize_variable_bounds(dict(extra_attrs))
    out: list[RangeCheckConstraint] = []
    for entry in normalized:
        sh_raw = str(entry.get("softHard") or "Hard").strip().lower()
        soft_hard: Literal["Hard", "Soft"] = "Soft" if sh_raw == "soft" else "Hard"
        comp_raw = entry.get("comparator")
        comp = str(comp_raw).strip().upper() if comp_raw else None
        cvals_raw = entry.get("checkValues")
        check_values = tuple(str(v) for v in cvals_raw) if isinstance(cvals_raw, list) else ()
        check_val = entry.get("checkValue")
        if check_val is None and len(check_values) == 1 and comp not in {"IN", "NOTIN"}:
            check_val = check_values[0]
        out.append(
            RangeCheckConstraint(
                comparator=comp,
                soft_hard=soft_hard,
                check_value=str(check_val) if check_val is not None else None,
                check_values=check_values,
                formal_expression=entry.get("formalExpression"),
                context=entry.get("context"),
                measurement_unit_oid=entry.get("measurementUnitOid"),
                error_message=entry.get("errorMessage"),
            )
        )
    return tuple(out)


def extract_variable_constraints(var: Any) -> VariableConstraints:
    """Extract choices, required policy, numeric/temporal bounds, and ODM RangeChecks."""
    if var is None:
        return VariableConstraints()

    raw_choices = getattr(var, "choices", None) or []
    choices: list[str] = []
    if not callable(raw_choices):
        for item in raw_choices:
            if isinstance(item, str):
                choices.append(item)
            elif isinstance(item, Mapping):
                cval = item.get("choiceValue", item.get("Choice Value"))
                if cval is not None:
                    choices.append(str(cval))
            else:
                cval = getattr(item, "choiceValue", None)
                if cval is not None:
                    choices.append(str(cval))

    raw_req = _lookup_attr_or_extra(var, ("required", "requiredField", "required_field"))
    required_policy: Literal["yes", "auto_query", "no"] = "no"
    if isinstance(raw_req, bool):
        required_policy = "yes" if raw_req else "no"
    elif isinstance(raw_req, str):
        norm_req = raw_req.strip().lower()
        if norm_req in {"yes", "y", "true", "1", "hard", "required"}:
            required_policy = "yes"
        elif norm_req in {"auto query", "auto_query", "autoquery", "query", "soft"}:
            required_policy = "auto_query"

    vtype = (
        re.sub(
            r"[\s_]+",
            " ",
            str(getattr(var, "variableType", None) or getattr(var, "variable_type", None) or ""),
        )
        .strip()
        .lower()
    )
    is_temporal_var = vtype in _TEMPORAL_VARIABLE_TYPES

    min_date = _extract_temporal_bound(
        var, _EXPLICIT_MIN_DATE_KEYS, _MIN_BOUND_KEYS, is_temporal_var=is_temporal_var
    )
    max_date = _extract_temporal_bound(
        var, _EXPLICIT_MAX_DATE_KEYS, _MAX_BOUND_KEYS, is_temporal_var=is_temporal_var
    )

    min_value = (
        None if is_temporal_var else _to_finite_float(_lookup_attr_or_extra(var, _MIN_BOUND_KEYS))
    )
    max_value = (
        None if is_temporal_var else _to_finite_float(_lookup_attr_or_extra(var, _MAX_BOUND_KEYS))
    )
    soft_min_value = (
        None
        if is_temporal_var
        else _to_finite_float(_lookup_attr_or_extra(var, _SOFT_MIN_BOUND_KEYS))
    )
    soft_max_value = (
        None
        if is_temporal_var
        else _to_finite_float(_lookup_attr_or_extra(var, _SOFT_MAX_BOUND_KEYS))
    )
    min_length = _to_non_negative_int(_lookup_attr_or_extra(var, _MIN_LENGTH_KEYS))
    max_length = _to_non_negative_int(_lookup_attr_or_extra(var, _MAX_LENGTH_KEYS))

    if max_length is None:
        fmt = getattr(var, "format", None)
        if (
            isinstance(fmt, str)
            and fmt.strip().isdigit()
            and vtype
            in {
                "text",
                "string",
                "textarea",
            }
        ):
            parsed_len = int(fmt.strip())
            if parsed_len > 0:
                max_length = parsed_len

    extra_raw = getattr(var, "extraAttributes", None)
    extra_attrs = extra_raw if isinstance(extra_raw, Mapping) else None
    range_checks = _parse_range_checks(extra_attrs)

    for rc in range_checks:
        comp = rc.comparator
        cval = rc.check_value
        if cval is None or comp is None:
            continue
        if not is_temporal_var:
            fval = _to_finite_float(cval)
            if fval is not None:
                if comp in {"GE", "GT"}:
                    if rc.soft_hard == "Hard":
                        min_value = fval if min_value is None else max(min_value, fval)
                    else:
                        soft_min_value = (
                            fval if soft_min_value is None else max(soft_min_value, fval)
                        )
                elif comp in {"LE", "LT"}:
                    if rc.soft_hard == "Hard":
                        max_value = fval if max_value is None else min(max_value, fval)
                    else:
                        soft_max_value = (
                            fval if soft_max_value is None else min(soft_max_value, fval)
                        )
                elif comp == "EQ":
                    if rc.soft_hard == "Hard":
                        min_value = fval if min_value is None else max(min_value, fval)
                        max_value = fval if max_value is None else min(max_value, fval)
                    else:
                        soft_min_value = (
                            fval if soft_min_value is None else max(soft_min_value, fval)
                        )
                        soft_max_value = (
                            fval if soft_max_value is None else min(soft_max_value, fval)
                        )
                continue

        if is_valid_iso_temporal_bound(cval, allow_partial=True):
            if comp in {"GE", "GT"}:
                if min_date is None or compare_temporal_strings(cval, ">", min_date):
                    min_date = cval
            elif comp in {"LE", "LT"}:
                if max_date is None or compare_temporal_strings(cval, "<", max_date):
                    max_date = cval
            elif comp == "EQ":
                min_date = cval
                max_date = cval

    return VariableConstraints(
        choices=choices,
        required_policy=required_policy,
        min_value=min_value,
        max_value=max_value,
        min_length=min_length,
        max_length=max_length,
        min_date=min_date,
        max_date=max_date,
        soft_min_value=soft_min_value,
        soft_max_value=soft_max_value,
        range_checks=range_checks,
    )


@dataclass(frozen=True)
class CompiledClinicalExpression:
    """AST-validated representation of an ODM FormalExpression or REDCap branchingLogic."""

    compiled_expression: str
    variable_map: dict[str, str] = field(default_factory=dict)
    checkbox_map: dict[str, tuple[str, str]] = field(default_factory=dict)
    conditions_tree: dict[str, Any] | None = None
    referenced_forms: tuple[str, ...] = ()
    is_cross_scope: bool = False


_QUOTED_LITERAL_RE = re.compile(r"'(?:\\.|''|[^'])*'|\"(?:\\.|\"\"|[^\"])*\"")
_XPATH_FORM_OID_RE = re.compile(r"FormData\[\s*@FormOID\s*=\s*__QSTR(\d+)__\s*\]")
_XPATH_ITEM_DATA_RE = re.compile(
    r"(?:(?:\.\./|\./|//|/)*(?:StudyEventData\[[^\]]*\]/)?"
    r"(?:FormData\[[^\]]*\]/)?(?:ItemGroupData\[[^\]]*\]/)*)?"
    r"ItemData\[\s*@ItemOID\s*=\s*__QSTR(\d+)__\s*\](?:/@Value)?"
)
_XPATH_REL_VAR_RE = re.compile(r"(?:\.\./|\./)+\$?([A-Za-z_][A-Za-z0-9_.-]*)")
_REDCAP_CROSS_BRACKET_RE = re.compile(r"\[([A-Za-z0-9_]+)\]\s*\[([^\[\]]+)\]")
_REDCAP_BRACKET_RE = re.compile(r"\[([^\[\]]*)\]")
_REDCAP_CHECKBOX_INNER_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\(([A-Za-z0-9_.-]+)\)$")
_REDCAP_VAR_INNER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SINGLE_EQ_RE = re.compile(r"(?<![<>!=])=(?!=)")
_IDENT_OR_DOTTED_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_.]*\b")
_PYTHON_KEYWORDS_AND_BUILTINS = frozenset(
    {"and", "or", "not", "in", "is", "if", "else", "True", "False", "None"}
    | set(DEFAULT_SAFE_FUNCTIONS.keys())
)
_FLIPPED_COMPARE_OPS = {
    "==": "==",
    "!=": "!=",
    "<": ">",
    "<=": ">=",
    ">": "<",
    ">=": "<=",
}


def _extract_quoted_strings(raw: str) -> tuple[str, list[str]]:
    strings: list[str] = []

    def _repl(match: re.Match[str]) -> str:
        tok = match.group(0)
        quote = tok[0]
        inner = tok[1:-1]
        if quote == "'":
            inner = inner.replace("''", "'").replace("\\'", "'")
        else:
            inner = inner.replace('""', '"').replace('\\"', '"')
        idx = len(strings)
        strings.append(inner)
        return f"__QSTR{idx}__"

    masked = _QUOTED_LITERAL_RE.sub(_repl, raw)
    # Check for unbalanced quotes outside masked regions
    if "'" in masked or '"' in masked:
        raise ValueError(f"Unterminated string literal in expression: {raw!r}")
    return masked, strings


def _restore_quoted_strings(masked: str, strings: list[str]) -> str:
    def _repl(match: re.Match[str]) -> str:
        idx = int(match.group(1))
        return repr(strings[idx])

    return re.sub(r"__QSTR(\d+)__", _repl, masked)


def _normalize_boolean_and_rel_ops(masked: str, *, allow_xpath_math: bool) -> str:
    s = masked.replace("<>", "!=")
    s = _SINGLE_EQ_RE.sub("==", s)
    if allow_xpath_math:
        s = re.sub(r"\bdiv\b", "/", s)
        s = re.sub(r"\bmod\b", "%", s)
    s = re.sub(r"\band\b", "and", s, flags=re.IGNORECASE)
    s = re.sub(r"\bor\b", "or", s, flags=re.IGNORECASE)
    return re.sub(r"\bnot\b", "not", s, flags=re.IGNORECASE)


def _ast_literal_value(node: ast.AST) -> tuple[bool, Any]:
    if isinstance(node, ast.Constant):
        return True, node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        ok, inner = _ast_literal_value(node.operand)
        if ok and isinstance(inner, (int, float)) and not isinstance(inner, bool):
            return True, (-inner if isinstance(node.op, ast.USub) else +inner)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        vals: list[Any] = []
        for elt in node.elts:
            ok, item = _ast_literal_value(elt)
            if not ok:
                return False, None
            vals.append(item)
        return True, vals
    return False, None


def _ast_compare_op_str(op: ast.cmpop) -> str | None:
    if isinstance(op, ast.Eq):
        return "=="
    if isinstance(op, ast.NotEq):
        return "!="
    if isinstance(op, ast.Lt):
        return "<"
    if isinstance(op, ast.LtE):
        return "<="
    if isinstance(op, ast.Gt):
        return ">"
    if isinstance(op, ast.GtE):
        return ">="
    if isinstance(op, ast.In):
        return "in"
    if isinstance(op, ast.NotIn):
        return "not_in"
    return None


def _single_compare_to_leaf(
    left: ast.AST,
    op: ast.cmpop,
    right: ast.AST,
    variable_map: Mapping[str, str],
    checkbox_map: Mapping[str, tuple[str, str]],
) -> dict[str, Any] | None:
    op_str = _ast_compare_op_str(op)
    if op_str is None:
        return None

    # Handle REDCap checkbox comparison: cb0 == '1' / '0'
    for cb_side, val_side in ((left, right), (right, left)):
        if isinstance(cb_side, ast.Name) and cb_side.id in checkbox_map:
            ok, lit_val = _ast_literal_value(val_side)
            if ok and op_str in {"==", "!="}:
                var_name, code = checkbox_map[cb_side.id]
                norm_lit = str(lit_val).strip().lower()
                is_one = norm_lit in {"1", "true", "yes", "y"}
                is_zero = norm_lit in {"0", "false", "no", "n", ""}
                if (op_str == "==" and is_one) or (op_str == "!=" and is_zero):
                    return {
                        "variable": var_name,
                        "operator": "checkbox_checked",
                        "value": code,
                        "@literalVariable": True,
                        "@gating": True,
                    }
                if (op_str == "==" and is_zero) or (op_str == "!=" and is_one):
                    return {
                        "variable": var_name,
                        "operator": "checkbox_unchecked",
                        "value": code,
                        "@literalVariable": True,
                        "@gating": True,
                    }
            return None

    # Variable vs Variable
    if (
        isinstance(left, ast.Name)
        and left.id in variable_map
        and isinstance(right, ast.Name)
        and right.id in variable_map
        and op_str not in {"in", "not_in"}
    ):
        return {
            "variable": variable_map[left.id],
            "operator": op_str,
            "compareVariable": variable_map[right.id],
            "@literalVariable": True,
            "@gating": True,
        }

    # Variable vs Constant
    if isinstance(left, ast.Name) and left.id in variable_map:
        ok, lit_val = _ast_literal_value(right)
        if ok:
            return {
                "variable": variable_map[left.id],
                "operator": op_str,
                "value": "" if lit_val is None else lit_val,
                "@literalVariable": True,
                "@gating": True,
            }

    # Constant vs Variable (reversed)
    if isinstance(right, ast.Name) and right.id in variable_map and op_str in _FLIPPED_COMPARE_OPS:
        ok, lit_val = _ast_literal_value(left)
        if ok:
            return {
                "variable": variable_map[right.id],
                "operator": _FLIPPED_COMPARE_OPS[op_str],
                "value": "" if lit_val is None else lit_val,
                "@literalVariable": True,
                "@gating": True,
            }

    return None


def _ast_node_to_logic_tree(
    node: ast.AST,
    full_expr: str,
    variable_map: Mapping[str, str],
    checkbox_map: Mapping[str, tuple[str, str]],
) -> dict[str, Any]:
    if isinstance(node, ast.BoolOp):
        key = "and" if isinstance(node.op, ast.And) else "or"
        return {
            key: [
                _ast_node_to_logic_tree(child, ast.unparse(child), variable_map, checkbox_map)
                for child in node.values
            ]
        }

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return {
            "not": _ast_node_to_logic_tree(
                node.operand, ast.unparse(node.operand), variable_map, checkbox_map
            )
        }

    if isinstance(node, ast.Compare):
        if len(node.ops) == 1:
            leaf = _single_compare_to_leaf(
                node.left, node.ops[0], node.comparators[0], variable_map, checkbox_map
            )
            if leaf is not None:
                return leaf
        else:
            chained_leaves: list[dict[str, Any]] = []
            cur_left = node.left
            all_ok = True
            for cmp_op, cmp_right in zip(node.ops, node.comparators, strict=True):
                sub_leaf = _single_compare_to_leaf(
                    cur_left, cmp_op, cmp_right, variable_map, checkbox_map
                )
                if sub_leaf is None:
                    all_ok = False
                    break
                chained_leaves.append(sub_leaf)
                cur_left = cmp_right
            if all_ok and chained_leaves:
                return {"and": chained_leaves}

    if isinstance(node, ast.Name) and node.id in variable_map:
        return {
            "variable": variable_map[node.id],
            "operator": "not_empty",
            "@literalVariable": True,
            "@gating": True,
        }

    used_names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
    sub_var_map = {k: v for k, v in variable_map.items() if k in used_names}
    sub_cb_map = {k: list(v) for k, v in checkbox_map.items() if k in used_names}
    return {
        "astExpression": full_expr,
        "variableMap": sub_var_map,
        "checkboxMap": sub_cb_map,
    }


def compile_odm_formal_expression(
    expression: str,
    *,
    context: str | None = None,
    self_var: str | None = None,
    item_alias_map: Mapping[str, str] | None = None,
) -> CompiledClinicalExpression:
    """Compile an ODM FormalExpression (XPath or clinical math/predicate) into safe AST nodes.

    Raises:
        ValueError: If the expression has invalid syntax or uses forbidden constructs.
    """
    _ = context
    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("ODM FormalExpression cannot be empty")

    masked, strings = _extract_quoted_strings(expression.strip())
    referenced_forms = tuple(
        dict.fromkeys(strings[int(m.group(1))] for m in _XPATH_FORM_OID_RE.finditer(masked))
    )
    is_cross_event = "StudyEventData[" in masked

    variable_map: dict[str, str] = {}
    rev_var_map: dict[str, str] = {}

    def _alloc_var(var_name: str) -> str:
        cleaned = var_name.strip()
        if item_alias_map and cleaned in item_alias_map:
            cleaned = item_alias_map[cleaned]
        if cleaned not in rev_var_map:
            placeholder = f"v{len(rev_var_map)}"
            rev_var_map[cleaned] = placeholder
            variable_map[placeholder] = cleaned
        return rev_var_map[cleaned]

    # 1. XPath ItemData[@ItemOID='...']
    def _repl_item_data(match: re.Match[str]) -> str:
        oid = strings[int(match.group(1))]
        return _alloc_var(oid)

    masked = _XPATH_ITEM_DATA_RE.sub(_repl_item_data, masked)

    # 2. XPath relative variable ../VAR or ./VAR
    masked = _XPATH_REL_VAR_RE.sub(lambda m: _alloc_var(m.group(1)), masked)

    # 3. @Value or standalone '.' when evaluating RangeCheck FormalExpression
    if self_var:
        masked = re.sub(r"\$?@Value\b", lambda _: _alloc_var(self_var), masked)
        masked = re.sub(
            r"(?<![0-9A-Za-z_.])\.(?![0-9A-Za-z_.])", lambda _: _alloc_var(self_var), masked
        )

    # 4. Normalize operators
    masked = _normalize_boolean_and_rel_ops(masked, allow_xpath_math=True)

    # 5. Map remaining bare variable identifiers (preserving keywords, QSTRs, and functions)
    def _repl_ident(match: re.Match[str]) -> str:
        tok = match.group(0)
        if tok.startswith("__QSTR") and tok.endswith("__"):
            return tok
        if tok in variable_map:
            return tok
        if tok in _PYTHON_KEYWORDS_AND_BUILTINS:
            return tok
        # Check if numeric with decimal like 30.0 (will not match [A-Za-z_] start anyway)
        # Reject attribute access chains like ''.__class__ or str.format by leaving method calls intact
        end_pos = match.end()
        rest = masked[end_pos:].lstrip()
        if rest.startswith("(") and "." not in tok:
            # Function call; let validate_ast enforce allowlist
            return tok
        if tok.startswith("_") or ".__" in tok:
            return tok
        return _alloc_var(tok)

    masked = _IDENT_OR_DOTTED_RE.sub(_repl_ident, masked)
    py_expr = _restore_quoted_strings(masked, strings)

    try:
        tree = parse_expression(py_expr)
    except (EvaluationError, RestrictedEvaluationError) as exc:
        raise ValueError(f"Invalid ODM FormalExpression {expression!r}: {exc}") from exc

    cond_tree = _ast_node_to_logic_tree(tree.body, py_expr, variable_map, {})
    return CompiledClinicalExpression(
        compiled_expression=py_expr,
        variable_map=variable_map,
        checkbox_map={},
        conditions_tree=cond_tree,
        referenced_forms=referenced_forms,
        is_cross_scope=is_cross_event,
    )


def compile_redcap_branching_logic(branching_logic: str) -> CompiledClinicalExpression:
    """Compile REDCap branchingLogic into safe LogicTreeEvaluator and RestrictedAstEvaluator nodes.

    Raises:
        ValueError: If bracket variable syntax or expression syntax is malformed.
    """
    if not isinstance(branching_logic, str) or not branching_logic.strip():
        raise ValueError("REDCap branchingLogic cannot be empty")

    masked, strings = _extract_quoted_strings(branching_logic.strip())
    is_cross_scope = False

    # Detect cross-event syntax: [event_1_arm_1][var_name]
    def _strip_event_prefix(match: re.Match[str]) -> str:
        nonlocal is_cross_scope
        is_cross_scope = True
        return f"[{match.group(2)}]"

    masked = _REDCAP_CROSS_BRACKET_RE.sub(_strip_event_prefix, masked)

    variable_map: dict[str, str] = {}
    rev_var_map: dict[str, str] = {}
    checkbox_map: dict[str, tuple[str, str]] = {}
    rev_cb_map: dict[tuple[str, str], str] = {}

    def _repl_bracket(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        if not inner:
            raise ValueError(
                f"Empty variable reference '[]' in branchingLogic: {branching_logic!r}"
            )
        cb_match = _REDCAP_CHECKBOX_INNER_RE.match(inner)
        if cb_match:
            key = (cb_match.group(1), cb_match.group(2))
            if key not in rev_cb_map:
                ph = f"cb{len(rev_cb_map)}"
                rev_cb_map[key] = ph
                checkbox_map[ph] = key
            return rev_cb_map[key]
        if _REDCAP_VAR_INNER_RE.match(inner):
            if inner not in rev_var_map:
                ph = f"v{len(rev_var_map)}"
                rev_var_map[inner] = ph
                variable_map[ph] = inner
            return rev_var_map[inner]
        raise ValueError(
            f"Invalid REDCap variable reference '[{inner}]' in branchingLogic: {branching_logic!r}"
        )

    masked = _REDCAP_BRACKET_RE.sub(_repl_bracket, masked)
    if "[" in masked or "]" in masked:
        raise ValueError(f"Unbalanced brackets in REDCap branchingLogic: {branching_logic!r}")

    masked = _normalize_boolean_and_rel_ops(masked, allow_xpath_math=False)
    py_expr = _restore_quoted_strings(masked, strings)

    try:
        tree = parse_expression(py_expr)
    except (EvaluationError, RestrictedEvaluationError) as exc:
        raise ValueError(f"Invalid REDCap branchingLogic {branching_logic!r}: {exc}") from exc

    cond_tree = _ast_node_to_logic_tree(tree.body, py_expr, variable_map, checkbox_map)
    if is_cross_scope:
        cond_tree = {"crossForm": True, "conditions": cond_tree}

    return CompiledClinicalExpression(
        compiled_expression=py_expr,
        variable_map=variable_map,
        checkbox_map=checkbox_map,
        conditions_tree=cond_tree,
        is_cross_scope=is_cross_scope,
    )


def build_redcap_conditional_required_logic(
    branching_logic: str,
    target_variable: str,
) -> dict[str, Any]:
    """Build a LogicTreeEvaluator AST for a REDCap field gated by branchingLogic."""
    compiled = compile_redcap_branching_logic(branching_logic)
    cond_node: dict[str, Any]
    if compiled.is_cross_scope:
        cond_node = {"crossForm": True}
    else:
        assert compiled.conditions_tree is not None
        cond_node = {
            "and": [
                compiled.conditions_tree,
                {
                    "variable": target_variable,
                    "operator": "empty",
                    "@literalVariable": True,
                },
            ]
        }
    return {
        "sourceFormat": "redcap_branching",
        "branchingExpression": branching_logic,
        "compiledBranching": {
            "compiledExpression": compiled.compiled_expression,
            "variableMap": compiled.variable_map,
            "checkboxMap": {k: list(v) for k, v in compiled.checkbox_map.items()},
        },
        "conditions": cond_node,
        "actions": {
            "action": {
                "type": "error",
                "message": (
                    f"Variable '{target_variable}' is required when "
                    f"branching logic ({branching_logic}) is satisfied"
                ),
            }
        },
    }


def _is_checkbox_code_selected(data: Mapping[str, Any], var_name: str, code: str) -> bool:
    """Evaluate REDCap checkbox selection across list, delimited string, or var___code keys."""
    dummy_key = f"{var_name}___{code}"
    if dummy_key in data:
        dval = data[dummy_key]
        return str(dval).strip().lower() in {"1", "true", "yes", "y"}

    raw_val = data.get(var_name)
    if _is_empty_record_value(raw_val):
        return False
    if isinstance(raw_val, Mapping):
        for k, v in raw_val.items():
            if str(k).strip() == code:
                return str(v).strip().lower() in {"1", "true", "yes", "y"}
        return False
    if isinstance(raw_val, (list, tuple, set)):
        return code in {str(item).strip() for item in raw_val}
    sval = str(raw_val).strip()
    if "`" in sval:
        return code in {tok.strip() for tok in sval.split("`") if tok.strip()}
    if "," in sval:
        return code in {tok.strip() for tok in sval.split(",") if tok.strip()}
    return sval == code


def evaluate_compiled_ast_expression(
    compiled_expression: str,
    variable_map: Mapping[str, str],
    data: Mapping[str, Any],
    *,
    checkbox_map: Mapping[str, Sequence[str] | tuple[str, str]] | None = None,
    numeric_preferred: bool = False,
) -> tuple[bool, Any]:
    """Safely evaluate a compiled expression with RestrictedAstEvaluator (never eval).

    Returns:
        ``(ok, result)`` where ``ok`` is False when required input variables are missing
        or evaluation fails.
    """
    env: dict[str, Any] = {}
    for ph, var_name in variable_map.items():
        if var_name not in data or _is_empty_record_value(data[var_name]):
            return False, None
        raw = data[var_name]
        fval = _to_finite_float(raw)
        if fval is not None and (numeric_preferred or isinstance(raw, (int, float))):
            env[ph] = fval
        elif fval is not None and isinstance(raw, str):
            # Coerce clean numeric strings if expression uses arithmetic/numeric comparison
            env[ph] = fval if numeric_preferred else raw
        else:
            env[ph] = raw

    if checkbox_map:
        for ph, pair in checkbox_map.items():
            var_name, code = str(pair[0]), str(pair[1])
            env[ph] = "1" if _is_checkbox_code_selected(data, var_name, code) else "0"

    try:
        evaluator = RestrictedAstEvaluator(compiled_expression)
        res = evaluator.evaluate(env, raise_on_error=True)
        return True, res
    except (EvaluationError, RestrictedEvaluationError, TypeError, ValueError):
        if not numeric_preferred:
            # Retry once coercing numeric strings to float for mixed string/number comparisons
            retry_env = dict(env)
            changed = False
            for ph, var_name in variable_map.items():
                fval = _to_finite_float(data.get(var_name))
                if fval is not None and not isinstance(retry_env.get(ph), float):
                    retry_env[ph] = fval
                    changed = True
            if changed:
                try:
                    res = RestrictedAstEvaluator(compiled_expression).evaluate(
                        retry_env, raise_on_error=True
                    )
                    return True, res
                except (EvaluationError, RestrictedEvaluationError, TypeError, ValueError):
                    return False, None
        return False, None


@dataclass(frozen=True)
class ExtractedCondition:
    """Flattened representation of a single XML condition leaf."""

    variable: str
    operator: str = "=="
    value: Any = ""
    expected_truth: bool = True
    form: str | None = None
    compare_variable: str | None = None
    is_gating: bool = False


@dataclass(frozen=True)
class ExtractedRuleMetadata:
    """Structured metadata extracted from a BusinessLogicRule AST."""

    rule_id: str | int | None
    rule_name: str
    form: str | None
    rule_type: str | None
    severity: Literal["error", "warning", "none"]
    logical_operator: Literal["AND", "OR"]
    conditions: list[ExtractedCondition] = field(default_factory=list)
    message: str | None = None
    is_cross_scope: bool = False
    is_ui_only: bool = False


@dataclass(frozen=True)
class RuleEvaluationResult:
    """Outcome of evaluating a single BusinessLogicRule against a Record."""

    rule_id: str | int | None
    rule_name: str
    rule_type: str | None
    severity: Literal["error", "warning", "none"]
    triggered: bool
    message: str
    skipped: bool = False
    skip_reason: str | None = None


@dataclass(frozen=True)
class LogicEvaluationReport:
    """Aggregated results of evaluating multiple BusinessLogicRules."""

    results: list[RuleEvaluationResult] = field(default_factory=list)

    @property
    def errors(self) -> list[RuleEvaluationResult]:
        """Blocking rule violations (e.g. Form or Required edit checks)."""
        return [r for r in self.results if r.triggered and not r.skipped and r.severity == "error"]

    @property
    def warnings(self) -> list[RuleEvaluationResult]:
        """Non-blocking rule triggers (e.g. Query or Auto Query rules)."""
        return [
            r for r in self.results if r.triggered and not r.skipped and r.severity == "warning"
        ]

    @property
    def skipped(self) -> list[RuleEvaluationResult]:
        """Rules skipped due to inactive status, missing logic, UI actions, or cross-scope refs."""
        return [r for r in self.results if r.skipped]

    @property
    def has_errors(self) -> bool:
        """Return True when one or more blocking rules triggered."""
        return bool(self.errors)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _is_empty_record_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value == ""
    if isinstance(value, (list, tuple, set)):
        return len(value) == 0
    return False


def _normalize_operator(raw_op: str) -> str:
    cleaned = raw_op.strip().lower()
    mapping = {
        "=": "==",
        "==": "==",
        "eq": "==",
        "equals": "==",
        "is": "==",
        "!=": "!=",
        "<>": "!=",
        "ne": "!=",
        "not_equals": "!=",
        "not equals": "!=",
        "is_not": "!=",
        "is not": "!=",
        "<": "<",
        "lt": "<",
        "<=": "<=",
        "le": "<=",
        "lte": "<=",
        ">": ">",
        "gt": ">",
        ">=": ">=",
        "ge": ">=",
        "gte": ">=",
        "in": "in",
        "one_of": "in",
        "not_in": "not_in",
        "not in": "not_in",
        "contains": "contains",
        "not_contains": "not_contains",
        "not contains": "not_contains",
        "starts_with": "starts_with",
        "startswith": "starts_with",
        "ends_with": "ends_with",
        "endswith": "ends_with",
        "regex": "regex",
        "matches": "regex",
        "empty": "empty",
        "is_empty": "empty",
        "is empty": "empty",
        "null": "empty",
        "is_null": "empty",
        "missing": "empty",
        "not_empty": "not_empty",
        "not empty": "not_empty",
        "is_not_empty": "not_empty",
        "not_null": "not_empty",
        "is_not_null": "not_empty",
        "present": "not_empty",
        "required": "not_empty",
    }
    return mapping.get(cleaned, cleaned or "==")


class LogicTreeEvaluator:
    """Deterministic, non-``eval`` evaluator for parsed BusinessLogicRule XML ASTs."""

    def evaluate_rules(
        self,
        rules: Iterable[Any],
        data: Mapping[str, Any],
        *,
        form_key: str | None = None,
    ) -> LogicEvaluationReport:
        """Evaluate a sequence of BusinessLogicRule instances or dicts against ``data``."""
        results = [self.evaluate_rule(rule, data, form_key=form_key) for rule in rules]
        return LogicEvaluationReport(results=results)

    def evaluate_rule(
        self,
        rule: Any,
        data: Mapping[str, Any],
        *,
        form_key: str | None = None,
    ) -> RuleEvaluationResult:
        """Evaluate a single BusinessLogicRule or dict against ``data``."""
        rule_id = self._read_field(rule, "id", "ID")
        rule_name = str(self._read_field(rule, "name", "Name") or "Unnamed Rule")
        rule_type_raw = self._read_field(rule, "type", "Type")
        rule_type = str(rule_type_raw) if rule_type_raw is not None else None
        status_raw = self._read_field(rule, "status", "Status")
        rule_form_raw = self._read_field(rule, "form", "Form")
        rule_form = str(rule_form_raw).strip() if rule_form_raw else None

        if (
            status_raw is not None
            and str(status_raw).strip()
            and str(status_raw).strip().lower() != "active"
        ):
            return RuleEvaluationResult(
                rule_id=rule_id,
                rule_name=rule_name,
                rule_type=rule_type,
                severity="none",
                triggered=False,
                message=rule_name,
                skipped=True,
                skip_reason="inactive",
            )

        effective_form = form_key or rule_form
        if form_key and rule_form and rule_form != form_key:
            return RuleEvaluationResult(
                rule_id=rule_id,
                rule_name=rule_name,
                rule_type=rule_type,
                severity="none",
                triggered=False,
                message=rule_name,
                skipped=True,
                skip_reason="different_form",
            )

        logic_ast, xml_err = self._resolve_logic_ast(rule)
        if xml_err is not None:
            return RuleEvaluationResult(
                rule_id=rule_id,
                rule_name=rule_name,
                rule_type=rule_type,
                severity="none",
                triggered=False,
                message=xml_err,
                skipped=True,
                skip_reason="malformed_xml",
            )
        if not isinstance(logic_ast, Mapping):
            return RuleEvaluationResult(
                rule_id=rule_id,
                rule_name=rule_name,
                rule_type=rule_type,
                severity="none",
                triggered=False,
                message=rule_name,
                skipped=True,
                skip_reason="no_logic",
            )

        unwrapped = self._unwrap_root(logic_ast)
        odm_elem = unwrapped.get("odmElement")

        if odm_elem == "MethodDef":
            return self._evaluate_odm_method_rule(
                rule_id=rule_id,
                rule_name=rule_name,
                rule_type=rule_type,
                unwrapped=unwrapped,
                data=data,
            )

        severity, is_ui_only, action_message = self._classify_rule(rule_type, unwrapped)
        default_msg = action_message or f"Business logic rule '{rule_name}' failed"

        if is_ui_only or severity == "none":
            return RuleEvaluationResult(
                rule_id=rule_id,
                rule_name=rule_name,
                rule_type=rule_type,
                severity="none",
                triggered=False,
                message=default_msg,
                skipped=True,
                skip_reason="non_validation_rule",
            )

        triggered, is_cross_scope, skip_reason = self.evaluate_tree(
            unwrapped,
            data,
            form_key=effective_form,
            rule_type=rule_type,
        )
        if skip_reason is not None or is_cross_scope:
            return RuleEvaluationResult(
                rule_id=rule_id,
                rule_name=rule_name,
                rule_type=rule_type,
                severity=severity,
                triggered=False,
                message=default_msg,
                skipped=True,
                skip_reason=skip_reason or "cross_scope",
            )

        return RuleEvaluationResult(
            rule_id=rule_id,
            rule_name=rule_name,
            rule_type=rule_type,
            severity=severity,
            triggered=triggered,
            message=default_msg,
        )

    def evaluate_method(
        self,
        rule: Any,
        data: Mapping[str, Any],
        *,
        form_key: str | None = None,
    ) -> Any:
        """Evaluate an ODM MethodDef equation via RestrictedAstEvaluator."""
        logic_ast, xml_err = self._resolve_logic_ast(rule)
        if xml_err is not None or not isinstance(logic_ast, Mapping):
            return None
        unwrapped = self._unwrap_root(logic_ast)
        ref_forms = unwrapped.get("referencedForms")
        if (
            form_key
            and isinstance(ref_forms, (list, tuple))
            and ref_forms
            and any(str(f) != form_key for f in ref_forms)
        ):
            return None
        expr = unwrapped.get("compiledExpression")
        var_map = unwrapped.get("variableMap")
        if not isinstance(expr, str) or not isinstance(var_map, Mapping):
            formal_list = unwrapped.get("formalExpressions")
            if isinstance(formal_list, list) and formal_list:
                first_expr = formal_list[0]
                raw_fe = (
                    first_expr.get("expression")
                    if isinstance(first_expr, Mapping)
                    else str(first_expr)
                )
                if isinstance(raw_fe, str) and raw_fe.strip():
                    try:
                        compiled = compile_odm_formal_expression(raw_fe)
                    except ValueError:
                        return None
                    expr = compiled.compiled_expression
                    var_map = compiled.variable_map
        if not isinstance(expr, str) or not isinstance(var_map, Mapping):
            return None
        ok, val = evaluate_compiled_ast_expression(expr, var_map, data, numeric_preferred=True)
        return val if ok else None

    def evaluate_condition(
        self,
        rule: Any,
        data: Mapping[str, Any],
        *,
        form_key: str | None = None,
    ) -> bool | None:
        """Evaluate a ConditionDef or conditional rule predicate, returning None if cross-scope."""
        logic_ast, xml_err = self._resolve_logic_ast(rule)
        if xml_err is not None or not isinstance(logic_ast, Mapping):
            return None
        unwrapped = self._unwrap_root(logic_ast)
        ref_forms = unwrapped.get("referencedForms")
        if (
            form_key
            and isinstance(ref_forms, (list, tuple))
            and ref_forms
            and any(str(f) != form_key for f in ref_forms)
        ):
            return None
        triggered, is_cross, skip_reason = self.evaluate_tree(
            unwrapped, data, form_key=form_key, rule_type="ConditionDef"
        )
        if is_cross:
            return None
        if skip_reason is not None:
            expr = unwrapped.get("compiledExpression")
            var_map = unwrapped.get("variableMap")
            if isinstance(expr, str) and isinstance(var_map, Mapping):
                ok, res = evaluate_compiled_ast_expression(expr, var_map, data)
                return bool(res) if ok else False
            return None
        return triggered

    def _evaluate_odm_method_rule(
        self,
        *,
        rule_id: str | int | None,
        rule_name: str,
        rule_type: str | None,
        unwrapped: Mapping[str, Any],
        data: Mapping[str, Any],
    ) -> RuleEvaluationResult:
        computed = self.evaluate_method({"logic": unwrapped}, data)
        if computed is None:
            return RuleEvaluationResult(
                rule_id=rule_id,
                rule_name=rule_name,
                rule_type=rule_type,
                severity="none",
                triggered=False,
                message=rule_name,
                skipped=True,
                skip_reason="missing_inputs",
            )
        targets = unwrapped.get("targetVariables")
        if not isinstance(targets, list) or not targets:
            return RuleEvaluationResult(
                rule_id=rule_id,
                rule_name=rule_name,
                rule_type=rule_type,
                severity="none",
                triggered=False,
                message=rule_name,
                skipped=True,
                skip_reason="no_target_variables",
            )

        populated_targets = [
            str(t) for t in targets if str(t) in data and not _is_empty_record_value(data[str(t)])
        ]
        if not populated_targets:
            return RuleEvaluationResult(
                rule_id=rule_id,
                rule_name=rule_name,
                rule_type=rule_type,
                severity="warning",
                triggered=False,
                message=rule_name,
                skipped=True,
                skip_reason="target_not_populated",
            )

        for target_var in populated_targets:
            actual = data[target_var]
            num_act = _to_finite_float(actual)
            num_exp = _to_finite_float(computed)
            if num_act is not None and num_exp is not None:
                matched = math.isclose(num_act, num_exp, rel_tol=1e-4, abs_tol=1e-2)
            else:
                matched = str(actual).strip() == str(computed).strip()
            if not matched:
                msg = f"Computed value mismatch for '{target_var}' in MethodDef '{rule_name}'"
                return RuleEvaluationResult(
                    rule_id=rule_id,
                    rule_name=rule_name,
                    rule_type=rule_type,
                    severity="error",
                    triggered=True,
                    message=msg,
                )

        return RuleEvaluationResult(
            rule_id=rule_id,
            rule_name=rule_name,
            rule_type=rule_type,
            severity="error",
            triggered=False,
            message=rule_name,
        )

    def evaluate_tree(
        self,
        logic_tree: Mapping[str, Any],
        data: Mapping[str, Any],
        *,
        form_key: str | None = None,
        rule_type: str | None = None,
    ) -> tuple[bool, bool, str | None]:
        """Evaluate an unwrapped logic AST against ``data``.

        Returns:
            ``(triggered, is_cross_scope, skip_reason)``
        """
        unwrapped = self._unwrap_root(logic_tree)
        root_combinator = self._root_combinator(unwrapped)
        cond_root = unwrapped.get("conditions", unwrapped.get("if", unwrapped))
        if not isinstance(cond_root, (Mapping, list)):
            return False, False, "no_conditions"

        conditions, is_cross_scope = self._collect_leaf_conditions(cond_root, form_key)
        if is_cross_scope:
            return False, True, "cross_scope"
        if not conditions:
            return False, False, "no_conditions"

        triggered = self._eval_node(
            cond_root,
            data,
            form_key=form_key,
            rule_type=rule_type,
            group_combinator=root_combinator,
        )
        return triggered, False, None

    def extract_rule_metadata(
        self,
        rule: Any,
        *,
        default_form: str | None = None,
    ) -> ExtractedRuleMetadata | None:
        """Extract structured condition and target metadata from an XML BusinessLogicRule."""
        logic_ast, xml_err = self._resolve_logic_ast(rule)
        if xml_err is not None or not isinstance(logic_ast, Mapping):
            return None

        rule_id = self._read_field(rule, "id", "ID")
        rule_name = str(self._read_field(rule, "name", "Name") or "Unknown Rule")
        rule_type_raw = self._read_field(rule, "type", "Type")
        rule_type = str(rule_type_raw) if rule_type_raw is not None else None
        rule_form_raw = self._read_field(rule, "form", "Form")
        resolved_form = (
            str(rule_form_raw).strip()
            if rule_form_raw
            else (default_form.strip() if default_form else None)
        )

        unwrapped = self._unwrap_root(logic_ast)
        root_combinator = self._root_combinator(unwrapped)
        severity, is_ui_only, message = self._classify_rule(rule_type, unwrapped)
        cond_root = unwrapped.get("conditions", unwrapped.get("if", unwrapped))
        if not isinstance(cond_root, (Mapping, list)):
            return ExtractedRuleMetadata(
                rule_id=rule_id,
                rule_name=rule_name,
                form=resolved_form,
                rule_type=rule_type,
                severity=severity,
                logical_operator=root_combinator or "AND",
                conditions=[],
                message=message,
                is_cross_scope=False,
                is_ui_only=is_ui_only,
            )

        conditions, is_cross_scope = self._collect_leaf_conditions(cond_root, resolved_form)
        if not resolved_form:
            cond_forms = {c.form for c in conditions if c.form}
            if len(cond_forms) == 1:
                resolved_form = next(iter(cond_forms))
            elif len(cond_forms) > 1:
                is_cross_scope = True

        combinator: Literal["AND", "OR"] = (
            self._effective_combinator(cond_root, default=root_combinator or "AND")
            if isinstance(cond_root, Mapping)
            else (root_combinator or "AND")
        )
        return ExtractedRuleMetadata(
            rule_id=rule_id,
            rule_name=rule_name,
            form=resolved_form,
            rule_type=rule_type,
            severity=severity,
            logical_operator=combinator,
            conditions=conditions,
            message=message,
            is_cross_scope=is_cross_scope,
            is_ui_only=is_ui_only,
        )

    @staticmethod
    def _read_field(obj: Any, *names: str) -> Any:
        for name in names:
            if isinstance(obj, Mapping) and name in obj:
                return obj[name]
            if hasattr(obj, name):
                val = getattr(obj, name)
                if not callable(val):
                    return val
        return None

    def _resolve_logic_ast(self, rule: Any) -> tuple[Mapping[str, Any] | None, str | None]:
        logic_val = self._read_field(rule, "logic", "Logic")
        if isinstance(logic_val, Mapping):
            return logic_val, None
        if isinstance(logic_val, str) and logic_val.strip():
            try:
                return parse_logic_xml(logic_val), None
            except ValueError as exc:
                return None, str(exc)

        raw_xml = self._read_field(rule, "logicRawXml", "LogicRawXml", "ruleXml", "xml")
        if isinstance(raw_xml, str) and raw_xml.strip():
            try:
                return parse_logic_xml(raw_xml), None
            except ValueError as exc:
                return None, str(exc)
        return None, None

    @staticmethod
    def _unwrap_root(logic_ast: Mapping[str, Any]) -> Mapping[str, Any]:
        known_keys = (
            {
                "then",
                "actions",
                "action",
                "variable",
                "field",
                "var",
            }
            | set(_CONTAINER_GROUP_KEYS)
            | set(_AND_GROUP_KEYS)
            | set(_OR_GROUP_KEYS)
            | set(_NOT_GROUP_KEYS)
            | set(_OPERATOR_TAG_MAP)
        )
        if not any(k in logic_ast for k in known_keys) and len(logic_ast) == 1:
            only_val = next(iter(logic_ast.values()))
            if isinstance(only_val, Mapping):
                return only_val
        return logic_ast

    @staticmethod
    def _classify_rule(
        rule_type: str | None,
        unwrapped: Mapping[str, Any],
    ) -> tuple[Literal["error", "warning", "none"], bool, str | None]:
        actions_container = unwrapped.get("actions", unwrapped.get("action", unwrapped.get("then")))
        raw_actions: list[Any] = []
        if isinstance(actions_container, Mapping):
            if "action" in actions_container:
                raw_actions.extend(_as_list(actions_container["action"]))
            else:
                raw_actions.append(actions_container)
        elif isinstance(actions_container, list):
            raw_actions.extend(actions_container)

        action_types: list[str] = []
        messages: list[str] = []

        for act in raw_actions:
            if isinstance(act, Mapping):
                atype = act.get("type") or act.get("@type") or act.get("actionType")
                if isinstance(atype, str) and atype.strip():
                    action_types.append(atype.strip().lower())
                for msg_key in ("message", "text", "queryText", "errorMessage", "#text"):
                    msg_val = act.get(msg_key)
                    if isinstance(msg_val, str) and msg_val.strip():
                        messages.append(msg_val.strip())
                        break
            elif isinstance(act, str) and act.strip():
                action_types.append(act.strip().lower())

        if not messages:
            for top_key in ("message", "errorMessage", "description"):
                top_msg = unwrapped.get(top_key)
                if isinstance(top_msg, str) and top_msg.strip():
                    messages.append(top_msg.strip())
                    break

        message = messages[0] if messages else None
        rt = (rule_type or "").strip().lower()

        has_error_action = any(at in _ERROR_ACTION_TYPES for at in action_types)
        has_query_action = any(at in _QUERY_ACTION_TYPES for at in action_types)
        has_warning_only_action = any(at in _WARNING_ACTION_TYPES for at in action_types)

        if (
            unwrapped.get("odmElement") == "ConditionDef"
            and not has_error_action
            and not has_query_action
        ):
            return "none", True, message

        if action_types and set(action_types).issubset(_UI_ACTION_TYPES):
            return "none", True, message
        if rt in {"workflow", "approval"} and not has_error_action and not has_query_action:
            return "none", True, message

        if rt in {"query", "auto query", "auto_query", "autoquery", "warning", "soft"}:
            return "warning", False, message
        if has_warning_only_action and not has_error_action:
            return "warning", False, message
        if rt in {"form", "required", "error", "hard"}:
            return "error", False, message
        if has_error_action:
            return "error", False, message
        if has_query_action:
            return "warning", False, message
        return "error", False, message

    @staticmethod
    def _explicit_combinator(node: Mapping[str, Any]) -> Literal["AND", "OR"] | None:
        for raw_key, raw_val in node.items():
            if not isinstance(raw_val, str):
                continue
            norm_key = str(raw_key).lstrip("@").replace("_", "").replace("-", "").lower()
            if norm_key not in _COMBINATOR_ATTR_NAMES:
                continue
            norm_val = raw_val.strip().upper()
            if norm_val in {"OR", "ANY"}:
                return "OR"
            if norm_val in {"AND", "ALL"}:
                return "AND"
        return None

    def _root_combinator(self, node: Mapping[str, Any]) -> Literal["AND", "OR"] | None:
        explicit = self._explicit_combinator(node)
        if explicit is not None:
            return explicit
        has_or_marker = any(node.get(k) is True for k in _OR_GROUP_KEYS)
        has_and_marker = any(node.get(k) is True for k in _AND_GROUP_KEYS)
        if has_or_marker and not has_and_marker:
            return "OR"
        if has_and_marker and not has_or_marker:
            return "AND"
        return None

    def _node_combinator(
        self,
        node: Mapping[str, Any],
        default: Literal["AND", "OR"] = "AND",
    ) -> Literal["AND", "OR"]:
        root_comb = self._root_combinator(node)
        return root_comb if root_comb is not None else default

    @staticmethod
    def _content_keys(node: Mapping[str, Any]) -> list[str]:
        return [
            str(k)
            for k in node
            if not str(k).startswith("@")
            and k != "#text"
            and k not in _NON_CONDITION_RULE_KEYS
            and node[k] is not True
            and str(k).lstrip("@").replace("_", "").replace("-", "").lower()
            not in _COMBINATOR_ATTR_NAMES
        ]

    def _effective_combinator(
        self,
        node: Mapping[str, Any],
        default: Literal["AND", "OR"] = "AND",
    ) -> Literal["AND", "OR"]:
        explicit = self._root_combinator(node)
        if explicit is not None:
            return explicit
        content_keys = self._content_keys(node)
        if len(content_keys) == 1:
            only_key = content_keys[0]
            only_val = node[only_key]
            if only_key in _OR_GROUP_KEYS:
                if isinstance(only_val, Mapping):
                    return self._effective_combinator(only_val, default="OR")
                if isinstance(only_val, list) and (
                    not only_val or not any(self._is_xml_group_dict(item) for item in only_val)
                ):
                    return "OR"
            if only_key in _AND_GROUP_KEYS:
                if isinstance(only_val, Mapping):
                    return self._effective_combinator(only_val, default="AND")
                if isinstance(only_val, list) and (
                    not only_val or not any(self._is_xml_group_dict(item) for item in only_val)
                ):
                    return "AND"
            if only_key in ("conditions", "conditionGroup", "group", "if") and isinstance(
                only_val, Mapping
            ):
                return self._effective_combinator(only_val, default=default)
        return default

    def _is_xml_group_dict(self, item: Any) -> bool:
        if not isinstance(item, Mapping) or self._is_leaf_condition(item):
            return False
        if any(
            k in item
            for k in (
                *_CONTAINER_GROUP_KEYS,
                *_AND_GROUP_KEYS,
                *_OR_GROUP_KEYS,
                *_NOT_GROUP_KEYS,
            )
        ):
            return True
        content_keys = self._content_keys(item)
        if len(content_keys) > 1:
            return True
        if len(content_keys) == 1:
            only_key = content_keys[0].lower()
            if only_key in _OPERATOR_TAG_MAP and isinstance(item[content_keys[0]], (Mapping, list)):
                return True
        return False

    @staticmethod
    def _is_leaf_condition(node: Mapping[str, Any]) -> bool:
        return any(k in node for k in ("variable", "field", "var", "@variable", "astExpression"))

    def _collect_leaf_conditions(
        self,
        node: Any,
        form_key: str | None,
        *,
        negated: bool = False,
        default_operator: str | None = None,
    ) -> tuple[list[ExtractedCondition], bool]:
        if isinstance(node, list):
            out: list[ExtractedCondition] = []
            cross = False
            for item in node:
                sub_conds, sub_cross = self._collect_leaf_conditions(
                    item,
                    form_key,
                    negated=negated,
                    default_operator=default_operator,
                )
                out.extend(sub_conds)
                cross = cross or sub_cross
            return out, cross

        if not isinstance(node, Mapping):
            return [], False

        for ckey in _CROSS_SCOPE_KEYS:
            if node.get(ckey):
                return [], True

        if self._is_leaf_condition(node):
            if "astExpression" in node:
                var_map = node.get("variableMap")
                cb_map = node.get("checkboxMap")
                ast_conds: list[ExtractedCondition] = []
                if isinstance(var_map, Mapping):
                    for vname in var_map.values():
                        ast_conds.append(
                            ExtractedCondition(
                                variable=str(vname),
                                operator="ast",
                                value=str(node["astExpression"]),
                                expected_truth=not negated,
                                form=form_key,
                                is_gating=True,
                            )
                        )
                if isinstance(cb_map, Mapping):
                    for pair in cb_map.values():
                        if isinstance(pair, (list, tuple)) and pair:
                            ast_conds.append(
                                ExtractedCondition(
                                    variable=str(pair[0]),
                                    operator="checkbox_checked",
                                    value=str(pair[1]) if len(pair) > 1 else "",
                                    expected_truth=not negated,
                                    form=form_key,
                                    is_gating=True,
                                )
                            )
                if not ast_conds:
                    ast_conds.append(
                        ExtractedCondition(
                            variable="__constant__",
                            operator="ast",
                            value=str(node["astExpression"]),
                            expected_truth=not negated,
                            form=form_key,
                            is_gating=True,
                        )
                    )
                return ast_conds, False
            leaf, is_cross = self._parse_leaf_condition(
                node,
                form_key,
                negated=negated,
                default_operator=default_operator,
            )
            return ([leaf] if leaf is not None else []), is_cross

        out_conds: list[ExtractedCondition] = []
        is_cross_any = False
        for key in (*_CONTAINER_GROUP_KEYS, *_AND_GROUP_KEYS, *_OR_GROUP_KEYS):
            if key in node and node[key] is not True:
                sub_c, sub_x = self._collect_leaf_conditions(
                    node[key],
                    form_key,
                    negated=negated,
                    default_operator=default_operator,
                )
                out_conds.extend(sub_c)
                is_cross_any = is_cross_any or sub_x
        for raw_key, child_val in node.items():
            op_mapped = _OPERATOR_TAG_MAP.get(str(raw_key).lower())
            if op_mapped is not None and isinstance(child_val, (Mapping, list)):
                sub_c, sub_x = self._collect_leaf_conditions(
                    child_val,
                    form_key,
                    negated=negated,
                    default_operator=op_mapped,
                )
                out_conds.extend(sub_c)
                is_cross_any = is_cross_any or sub_x
        for not_key in _NOT_GROUP_KEYS:
            if not_key in node and node[not_key] is not True:
                sub_c, sub_x = self._collect_leaf_conditions(
                    node[not_key],
                    form_key,
                    negated=not negated,
                    default_operator=default_operator,
                )
                out_conds.extend(sub_c)
                is_cross_any = is_cross_any or sub_x
        return out_conds, is_cross_any

    @staticmethod
    def _resolve_scoped_variable(
        raw_var: str,
        cond_form: str | None,
        form_key: str | None,
    ) -> tuple[str, str | None, bool]:
        var_name = raw_var.strip()
        resolved_cond_form = cond_form
        if "." in var_name:
            prefix, rest = var_name.split(".", 1)
            prefix = prefix.strip()
            rest = rest.strip()
            if resolved_cond_form is None:
                resolved_cond_form = prefix
            if form_key and prefix != form_key:
                return var_name, resolved_cond_form, True
            var_name = rest

        if form_key and resolved_cond_form and resolved_cond_form != form_key:
            return var_name, resolved_cond_form, True

        return var_name, resolved_cond_form, False

    def _parse_leaf_condition(
        self,
        cond: Mapping[str, Any],
        form_key: str | None,
        *,
        negated: bool = False,
        default_operator: str | None = None,
    ) -> tuple[ExtractedCondition | None, bool]:
        for ckey in _CROSS_SCOPE_KEYS:
            if cond.get(ckey):
                return None, True

        raw_var = str(
            cond.get("variable")
            or cond.get("field")
            or cond.get("var")
            or cond.get("@variable")
            or ""
        ).strip()
        if not raw_var:
            return None, False

        raw_form = cond.get("form") or cond.get("@form") or cond.get("formKey")
        cond_form = (
            str(raw_form).strip() if isinstance(raw_form, str) and raw_form.strip() else None
        )
        literal_var = bool(cond.get("literalVariable") or cond.get("@literalVariable"))
        is_gating = bool(cond.get("gating") or cond.get("@gating"))

        if literal_var:
            var_name = raw_var
            resolved_form = cond_form
            is_cross = bool(form_key and resolved_form and resolved_form != form_key)
        else:
            var_name, resolved_form, is_cross = self._resolve_scoped_variable(
                raw_var, cond_form, form_key
            )
        if is_cross:
            return None, True

        compare_var: str | None = None
        for cv_key in _COMPARE_VAR_KEYS:
            cv_raw = cond.get(cv_key)
            if isinstance(cv_raw, str) and cv_raw.strip():
                if literal_var:
                    compare_var = cv_raw.strip()
                else:
                    cv_name, _, cv_cross = self._resolve_scoped_variable(
                        cv_raw, cond_form, form_key
                    )
                    if cv_cross:
                        return None, True
                    compare_var = cv_name
                break

        raw_op = str(cond.get("operator") or cond.get("@operator") or cond.get("op") or "").strip()
        if not raw_op:
            if default_operator is not None:
                op = default_operator
            elif cond.get("empty") is True or cond.get("isEmpty") is True:
                op = "empty"
            elif cond.get("notEmpty") is True or cond.get("isNotEmpty") is True:
                op = "not_empty"
            elif (
                "value" in cond
                or "@value" in cond
                or "targetValue" in cond
                or compare_var is not None
            ):
                op = "=="
            else:
                op = "not_empty"
        else:
            op = _normalize_operator(raw_op)

        raw_val = cond.get("value", cond.get("@value", cond.get("targetValue", "")))
        has_false = cond.get("false") is True or str(cond.get("false", "")).lower() == "true"
        has_negate = (
            cond.get("negate") is True
            or str(cond.get("negate", "")).lower() == "true"
            or str(cond.get("@negate", "")).lower() == "true"
        )
        expected_truth = True
        if has_false:
            expected_truth = not expected_truth
        if has_negate:
            expected_truth = not expected_truth
        if negated:
            expected_truth = not expected_truth

        return (
            ExtractedCondition(
                variable=var_name,
                operator=op,
                value=raw_val,
                expected_truth=expected_truth,
                form=resolved_form or form_key,
                compare_variable=compare_var,
                is_gating=is_gating,
            ),
            False,
        )

    def _eval_combinator_branch(
        self,
        branch_val: Any,
        branch_combinator: Literal["AND", "OR"],
        data: Mapping[str, Any],
        *,
        form_key: str | None,
        rule_type: str | None,
        default_operator: str | None,
        results: list[bool],
    ) -> None:
        if branch_val is True:
            return
        if isinstance(branch_val, list) and (
            not branch_val or not any(self._is_xml_group_dict(item) for item in branch_val)
        ):
            results.append(
                self._eval_node(
                    branch_val,
                    data,
                    form_key=form_key,
                    rule_type=rule_type,
                    default_operator=default_operator,
                    group_combinator=branch_combinator,
                )
            )
            return
        for item in _as_list(branch_val):
            results.append(
                self._eval_node(
                    item,
                    data,
                    form_key=form_key,
                    rule_type=rule_type,
                    default_operator=default_operator,
                    group_combinator=branch_combinator,
                )
            )

    def _eval_node(
        self,
        node: Any,
        data: Mapping[str, Any],
        *,
        form_key: str | None,
        rule_type: str | None,
        default_operator: str | None = None,
        group_combinator: Literal["AND", "OR"] | None = None,
    ) -> bool:
        if isinstance(node, list):
            if not node:
                return False
            eval_fn = any if group_combinator == "OR" else all
            return eval_fn(
                self._eval_node(
                    item,
                    data,
                    form_key=form_key,
                    rule_type=rule_type,
                    default_operator=default_operator,
                )
                for item in node
            )
        if not isinstance(node, Mapping):
            return False

        if self._is_leaf_condition(node):
            if "astExpression" in node:
                expr_str = str(node["astExpression"])
                var_map = node.get("variableMap")
                cb_map = node.get("checkboxMap")
                ok, val = evaluate_compiled_ast_expression(
                    expr_str,
                    var_map if isinstance(var_map, Mapping) else {},
                    data,
                    checkbox_map=cb_map if isinstance(cb_map, Mapping) else None,
                )
                return bool(val) if ok else False
            leaf, is_cross = self._parse_leaf_condition(
                node,
                form_key,
                default_operator=default_operator,
            )
            if is_cross or leaf is None:
                return False
            return self._eval_leaf(leaf, data, rule_type=rule_type)

        combinator = self._node_combinator(node, default=group_combinator or "AND")
        results: list[bool] = []

        for and_key in _AND_GROUP_KEYS:
            if and_key in node:
                self._eval_combinator_branch(
                    node[and_key],
                    "AND",
                    data,
                    form_key=form_key,
                    rule_type=rule_type,
                    default_operator=default_operator,
                    results=results,
                )
        for or_key in _OR_GROUP_KEYS:
            if or_key in node:
                self._eval_combinator_branch(
                    node[or_key],
                    "OR",
                    data,
                    form_key=form_key,
                    rule_type=rule_type,
                    default_operator=default_operator,
                    results=results,
                )
        for not_key in _NOT_GROUP_KEYS:
            if not_key in node and node[not_key] is not True:
                for item in _as_list(node[not_key]):
                    results.append(
                        not self._eval_node(
                            item,
                            data,
                            form_key=form_key,
                            rule_type=rule_type,
                            default_operator=default_operator,
                        )
                    )

        content_keys = self._content_keys(node)
        has_node_combinator = (
            group_combinator is not None or self._root_combinator(node) is not None
        )
        for key in _CONTAINER_GROUP_KEYS:
            if key in node and node[key] is not True:
                child_val = node[key]
                inherited_combinator: Literal["AND", "OR"] | None = (
                    combinator
                    if key in ("conditions", "conditionGroup", "group", "if")
                    and has_node_combinator
                    and len(content_keys) == 1
                    and isinstance(child_val, Mapping)
                    else None
                )
                for child in _as_list(child_val):
                    results.append(
                        self._eval_node(
                            child,
                            data,
                            form_key=form_key,
                            rule_type=rule_type,
                            default_operator=default_operator,
                            group_combinator=inherited_combinator,
                        )
                    )

        for raw_key, child_val in node.items():
            op_mapped = _OPERATOR_TAG_MAP.get(str(raw_key).lower())
            if op_mapped is not None and isinstance(child_val, (Mapping, list)):
                for child in _as_list(child_val):
                    results.append(
                        self._eval_node(
                            child,
                            data,
                            form_key=form_key,
                            rule_type=rule_type,
                            default_operator=op_mapped,
                        )
                    )

        if not results:
            return False
        return any(results) if combinator == "OR" else all(results)

    def _eval_leaf(
        self,
        cond: ExtractedCondition,
        data: Mapping[str, Any],
        *,
        rule_type: str | None,
    ) -> bool:
        actual = data.get(cond.variable)
        op = cond.operator

        if op == "checkbox_checked":
            base = _is_checkbox_code_selected(data, cond.variable, str(cond.value))
            return base if cond.expected_truth else (not base)

        if op == "checkbox_unchecked":
            base = not _is_checkbox_code_selected(data, cond.variable, str(cond.value))
            return base if cond.expected_truth else (not base)

        if op == "empty":
            base = _is_empty_record_value(actual)
            return base if cond.expected_truth else (not base)

        if op == "not_empty":
            base = not _is_empty_record_value(actual)
            return base if cond.expected_truth else (not base)

        if cond.compare_variable is not None:
            rhs = data.get(cond.compare_variable)
            if _is_empty_record_value(actual) or _is_empty_record_value(rhs):
                return False
        else:
            rhs = cond.value
            # Empty XML tag <value/> parses to True via _parse_xml_element
            if rhs is True and not isinstance(actual, bool):
                rhs = ""

            if _is_empty_record_value(actual):
                # Explicit comparison to empty string (e.g. VAR == "" or VAR != "")
                if rhs == "" and op in {"==", "!="}:
                    base = actual is None or actual == ""
                    if op == "!=":
                        base = not base
                    return base if cond.expected_truth else (not base)
                if cond.is_gating:
                    return False
                # Required rule checking missingness via <false/>
                return (rule_type or "").strip().lower() == "required" and not cond.expected_truth

        base_result = self._compare_values(actual, op, rhs)
        return base_result if cond.expected_truth else (not base_result)

    @staticmethod
    def _compare_values(actual: Any, op: str, expected: Any) -> bool:
        if op in {"in", "not_in"}:
            if isinstance(expected, (list, tuple, set)):
                allowed = {str(item).strip() for item in expected}
            else:
                exp_str = str(expected)
                sep = "`" if "`" in exp_str else ","
                allowed = {part.strip() for part in exp_str.split(sep) if part.strip()}
            if isinstance(actual, (list, tuple, set)):
                matched = all(str(item).strip() in allowed for item in actual)
            else:
                act_str = str(actual).strip()
                if "`" in act_str and act_str not in allowed:
                    matched = all(
                        tok.strip() in allowed for tok in act_str.split("`") if tok.strip()
                    )
                else:
                    matched = act_str in allowed
            return matched if op == "in" else (not matched)

        if op in {"contains", "not_contains"}:
            needle = str(expected).strip()
            if isinstance(actual, (list, tuple, set)):
                found = needle in {str(item).strip() for item in actual}
            else:
                act_str = str(actual)
                if "`" in act_str:
                    found = needle in {tok.strip() for tok in act_str.split("`") if tok.strip()}
                else:
                    found = needle in act_str
            return found if op == "contains" else (not found)

        if op == "starts_with":
            return str(actual).startswith(str(expected))

        if op == "ends_with":
            return str(actual).endswith(str(expected))

        if op == "regex":
            try:
                return re.search(str(expected), str(actual)) is not None
            except re.error:
                return False

        # Boolean comparison when actual is a Python bool
        if isinstance(actual, bool):
            if isinstance(expected, bool):
                exp_bool: bool | None = expected
            elif isinstance(expected, str):
                low = expected.strip().lower()
                if low in {"true", "1", "yes", "y"}:
                    exp_bool = True
                elif low in {"false", "0", "no", "n"}:
                    exp_bool = False
                else:
                    exp_bool = None
            else:
                exp_bool = None

            if exp_bool is not None:
                if op == "==":
                    return actual is exp_bool
                if op == "!=":
                    return actual is not exp_bool
            return False

        # Numeric comparison when both operands parse cleanly as finite numbers
        num_actual = _to_finite_float(actual)
        num_expected = _to_finite_float(expected)
        if num_actual is not None and num_expected is not None:
            if op == "==":
                return math.isclose(num_actual, num_expected, rel_tol=1e-9, abs_tol=1e-12)
            if op == "!=":
                return not math.isclose(num_actual, num_expected, rel_tol=1e-9, abs_tol=1e-12)
            if op == "<":
                return num_actual < num_expected
            if op == "<=":
                return num_actual <= num_expected
            if op == ">":
                return num_actual > num_expected
            if op == ">=":
                return num_actual >= num_expected
            return False

        # Fallback lexical/string comparison (supports Coded strings and ISO-8601 dates)
        str_actual = str(actual).strip()
        str_expected = str(expected).strip()
        if op == "==":
            return str_actual == str_expected
        if op == "!=":
            return str_actual != str_expected
        if op == "<":
            return str_actual < str_expected
        if op == "<=":
            return str_actual <= str_expected
        if op == ">":
            return str_actual > str_expected
        if op == ">=":
            return str_actual >= str_expected
        return False
