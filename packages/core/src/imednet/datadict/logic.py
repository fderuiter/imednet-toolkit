"""Business logic XML parsing and AST evaluation for iMednet data dictionary."""

from __future__ import annotations

import functools
import math
import re
import re._constants as _re_constants  # type: ignore[import-not-found]
import re._parser as _re_parser  # type: ignore[import-not-found]
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any, Literal

import defusedxml.ElementTree as ET  # noqa: N817
from defusedxml.common import DefusedXmlException

from .normalize import to_camel_case


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
    }
)


RangeCheckComparator = Literal["LT", "LE", "GT", "GE", "EQ", "NE", "IN", "NOTIN"]

_VALID_RANGE_COMPARATORS: frozenset[str] = frozenset(
    {"LT", "LE", "GT", "GE", "EQ", "NE", "IN", "NOTIN"}
)

_TEMPORAL_COMPARE_MDY_RE = re.compile(
    r"^(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])-([0-9]{4})"
    r"(?:[ T]([01][0-9]|2[0-3]):([0-5][0-9])(?::([0-5][0-9])(?:\.([0-9]{1,6})[0-9]*)?)?)?"
    r"(Z|[+-](?:[01][0-9]|2[0-3]):?[0-5][0-9])?$"
)
_TEMPORAL_COMPARE_YMD_RE = re.compile(
    r"^([0-9]{4})-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])"
    r"(?:[ T]([01][0-9]|2[0-3]):([0-5][0-9])(?::([0-5][0-9])(?:\.([0-9]{1,6})[0-9]*)?)?)?"
    r"(Z|[+-](?:[01][0-9]|2[0-3]):?[0-5][0-9])?$"
)


@dataclass(frozen=True)
class RangeCheckConstraint:
    """Normalized CDISC ODM RangeCheck comparator, check values, and SoftHard severity."""

    comparator: RangeCheckComparator
    check_values: tuple[str, ...]
    soft_hard: Literal["Soft", "Hard"] = "Hard"
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


def _parse_tz_offset(tz_suffix: str | None) -> timezone | None:
    """Parse an ISO-8601 timezone suffix ('Z', '+HH:MM', '-HHMM') into a timezone."""
    if not tz_suffix:
        return None
    if tz_suffix == "Z":
        return UTC
    sign = 1 if tz_suffix[0] == "+" else -1
    digits = tz_suffix[1:].replace(":", "")
    hours = int(digits[:2])
    minutes = int(digits[2:4])
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def _parse_comparable_temporal(value: Any) -> datetime | None:
    """Parse MM-DD-YYYY or YYYY-MM-DD temporal inputs into a UTC-normalized naive datetime."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(UTC).replace(tzinfo=None)
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if not isinstance(value, str):
        return None

    cleaned = value.strip()
    if not cleaned:
        return None

    year: int
    month: int
    day: int
    mdy_match = _TEMPORAL_COMPARE_MDY_RE.match(cleaned)
    if mdy_match is not None:
        month = int(mdy_match.group(1))
        day = int(mdy_match.group(2))
        year = int(mdy_match.group(3))
        hh, mm, ss, frac, tz_tok = mdy_match.group(4, 5, 6, 7, 8)
    else:
        ymd_match = _TEMPORAL_COMPARE_YMD_RE.match(cleaned)
        if ymd_match is None:
            return None
        year = int(ymd_match.group(1))
        month = int(ymd_match.group(2))
        day = int(ymd_match.group(3))
        hh, mm, ss, frac, tz_tok = ymd_match.group(4, 5, 6, 7, 8)

    hour = int(hh) if hh is not None else 0
    minute = int(mm) if mm is not None else 0
    second = int(ss) if ss is not None else 0
    microsecond = int(frac.ljust(6, "0")) if frac is not None else 0
    tz_info = _parse_tz_offset(tz_tok)
    try:
        dt = datetime(year, month, day, hour, minute, second, microsecond, tzinfo=tz_info)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(UTC).replace(tzinfo=None)
    return dt


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


def _extract_odm_range_checks(
    var: Any,
) -> tuple[tuple[RangeCheckConstraint, ...], float | None, float | None]:
    """Extract normalized ODM RangeCheck constraints and implied min/max bounds."""
    extra: Mapping[str, Any] = getattr(var, "extraAttributes", None) or {}
    raw_checks = extra.get("rangeChecks")
    if not isinstance(raw_checks, list) or not raw_checks:
        return (), None, None

    parsed: list[RangeCheckConstraint] = []
    implied_min: float | None = None
    implied_max: float | None = None

    for item in raw_checks:
        if not isinstance(item, Mapping):
            continue
        raw_comp = item.get("comparator") or item.get("Comparator")
        if not isinstance(raw_comp, str) or not raw_comp.strip():
            continue
        comp_norm = raw_comp.strip().upper().replace(" ", "").replace("_", "")
        if comp_norm not in _VALID_RANGE_COMPARATORS:
            continue

        check_vals: list[str] = []
        raw_multi = item.get("checkValues")
        if isinstance(raw_multi, list):
            check_vals = [str(v).strip() for v in raw_multi if v is not None and str(v).strip()]
        elif item.get("checkValue") is not None and str(item["checkValue"]).strip():
            check_vals = [str(item["checkValue"]).strip()]
        if not check_vals:
            continue

        raw_sh = str(item.get("softHard") or item.get("SoftHard") or "").strip().lower()
        soft_hard: Literal["Soft", "Hard"] = "Soft" if raw_sh == "soft" else "Hard"
        raw_err = item.get("errorMessage")
        err_msg = str(raw_err).strip() if isinstance(raw_err, str) and raw_err.strip() else None

        parsed.append(
            RangeCheckConstraint(
                comparator=comp_norm,  # type: ignore[arg-type]
                check_values=tuple(check_vals),
                soft_hard=soft_hard,
                error_message=err_msg,
            )
        )

        if len(check_vals) == 1:
            num_bound = _to_finite_float(check_vals[0])
            if num_bound is not None:
                if comp_norm in {"GE", "GT"}:
                    implied_min = num_bound if implied_min is None else max(implied_min, num_bound)
                elif comp_norm in {"LE", "LT"}:
                    implied_max = num_bound if implied_max is None else min(implied_max, num_bound)

    return tuple(parsed), implied_min, implied_max


def extract_variable_constraints(var: Any) -> VariableConstraints:
    """Extract choices, required policy, numeric bounds, and string lengths from a Variable."""
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

    min_value = _to_finite_float(_lookup_attr_or_extra(var, _MIN_BOUND_KEYS))
    max_value = _to_finite_float(_lookup_attr_or_extra(var, _MAX_BOUND_KEYS))
    range_checks, implied_min, implied_max = _extract_odm_range_checks(var)
    if min_value is None and implied_min is not None:
        min_value = implied_min
    if max_value is None and implied_max is not None:
        max_value = implied_max

    min_length = _to_non_negative_int(_lookup_attr_or_extra(var, _MIN_LENGTH_KEYS))
    max_length = _to_non_negative_int(_lookup_attr_or_extra(var, _MAX_LENGTH_KEYS))

    if max_length is None:
        fmt = getattr(var, "format", None)
        vtype = (
            (getattr(var, "variableType", None) or getattr(var, "variable_type", None) or "")
            .strip()
            .lower()
        )
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

    return VariableConstraints(
        choices=choices,
        required_policy=required_policy,
        min_value=min_value,
        max_value=max_value,
        min_length=min_length,
        max_length=max_length,
        range_checks=range_checks,
    )


@dataclass(frozen=True)
class ExtractedCondition:
    """Flattened representation of a single XML condition leaf."""

    variable: str
    operator: str = "=="
    value: Any = ""
    expected_truth: bool = True
    form: str | None = None
    compare_variable: str | None = None


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
    is_cross_scope: bool = False


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


_MAX_REGEX_PATTERN_LENGTH = 512
_MAX_REGEX_INPUT_LENGTH = 1024
_MAX_REGEX_REPEAT_COUNT = 256

_REPEAT_OPCODES = frozenset(
    op
    for op in (
        _re_constants.MAX_REPEAT,
        _re_constants.MIN_REPEAT,
        getattr(_re_constants, "POSSESSIVE_REPEAT", None),
    )
    if op is not None
)
_BACKREF_OPCODES = frozenset(
    op
    for op in (
        _re_constants.GROUPREF,
        _re_constants.GROUPREF_EXISTS,
        getattr(_re_constants, "GROUPREF_IGNORE", None),
        getattr(_re_constants, "GROUPREF_LOC_IGNORE", None),
        getattr(_re_constants, "GROUPREF_UNI_IGNORE", None),
    )
    if op is not None
)
_LOOKAROUND_OPCODES = frozenset({_re_constants.ASSERT, _re_constants.ASSERT_NOT})
_ATOMIC_GROUP_OPCODE = getattr(_re_constants, "ATOMIC_GROUP", None)

_DIGIT_ASCII_MASK = sum(1 << c for c in range(48, 58))
_UPPER_ASCII_MASK = sum(1 << c for c in range(65, 91))
_LOWER_ASCII_MASK = sum(1 << c for c in range(97, 123))
_WORD_ASCII_MASK = _DIGIT_ASCII_MASK | _UPPER_ASCII_MASK | _LOWER_ASCII_MASK | (1 << 95)
_SPACE_ASCII_MASK = sum(1 << c for c in (9, 10, 11, 12, 13, 32))
_LINEBREAK_ASCII_MASK = (1 << 10) | (1 << 13)

_DIGIT_CATEGORIES = frozenset(
    cat
    for cat in (
        _re_constants.CATEGORY_DIGIT,
        getattr(_re_constants, "CATEGORY_UNI_DIGIT", None),
    )
    if cat is not None
)
_NOT_DIGIT_CATEGORIES = frozenset(
    cat
    for cat in (
        _re_constants.CATEGORY_NOT_DIGIT,
        getattr(_re_constants, "CATEGORY_UNI_NOT_DIGIT", None),
    )
    if cat is not None
)
_WORD_CATEGORIES = frozenset(
    cat
    for cat in (
        _re_constants.CATEGORY_WORD,
        getattr(_re_constants, "CATEGORY_UNI_WORD", None),
        getattr(_re_constants, "CATEGORY_LOC_WORD", None),
    )
    if cat is not None
)
_NOT_WORD_CATEGORIES = frozenset(
    cat
    for cat in (
        _re_constants.CATEGORY_NOT_WORD,
        getattr(_re_constants, "CATEGORY_UNI_NOT_WORD", None),
        getattr(_re_constants, "CATEGORY_LOC_NOT_WORD", None),
    )
    if cat is not None
)
_SPACE_CATEGORIES = frozenset(
    cat
    for cat in (
        _re_constants.CATEGORY_SPACE,
        getattr(_re_constants, "CATEGORY_UNI_SPACE", None),
    )
    if cat is not None
)
_NOT_SPACE_CATEGORIES = frozenset(
    cat
    for cat in (
        _re_constants.CATEGORY_NOT_SPACE,
        getattr(_re_constants, "CATEGORY_UNI_NOT_SPACE", None),
    )
    if cat is not None
)
_LINEBREAK_CATEGORIES = frozenset(
    cat
    for cat in (
        getattr(_re_constants, "CATEGORY_LINEBREAK", None),
        getattr(_re_constants, "CATEGORY_UNI_LINEBREAK", None),
    )
    if cat is not None
)
_NOT_LINEBREAK_CATEGORIES = frozenset(
    cat
    for cat in (
        getattr(_re_constants, "CATEGORY_NOT_LINEBREAK", None),
        getattr(_re_constants, "CATEGORY_UNI_NOT_LINEBREAK", None),
    )
    if cat is not None
)


def _has_quantified_pipe_group(pattern: str) -> bool:
    """Return True if ``pattern`` has a parenthesized ``|`` group with a repeating quantifier."""
    group_has_pipe: list[bool] = []
    escaped = False
    in_char_class = False
    class_initial = False
    length = len(pattern)

    for idx, ch in enumerate(pattern):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if in_char_class:
            if class_initial:
                if ch == "^":
                    continue
                class_initial = False
                if ch == "]":
                    continue
            if ch == "]":
                in_char_class = False
            continue
        if ch == "[":
            in_char_class = True
            class_initial = True
            continue
        if ch == "(":
            group_has_pipe.append(False)
            continue
        if ch == "|":
            if group_has_pipe:
                group_has_pipe[-1] = True
            continue
        if ch == ")" and group_has_pipe:
            had_pipe = group_has_pipe.pop()
            if had_pipe:
                if idx + 1 < length and pattern[idx + 1] in {"*", "+", "{"}:
                    return True
                if group_has_pipe:
                    group_has_pipe[-1] = True
    return False


class _RegexCharsetBuilder:
    """Bitmask representation of character sets across ASCII and Unicode intervals."""

    def __init__(self, items: Sequence[Any]) -> None:
        boundaries: set[int] = {128, 0x110000}
        self._collect_boundaries(items, boundaries)
        pts = sorted(boundaries)
        self._segments: list[tuple[int, int, int]] = []
        self.all_mask = (1 << 128) - 1
        bit_cursor = 128
        for idx in range(len(pts) - 1):
            seg_lo = pts[idx]
            seg_hi = pts[idx + 1] - 1
            self._segments.append((seg_lo, seg_hi, bit_cursor))
            self.all_mask |= self._present_subclasses(seg_lo, seg_hi) << bit_cursor
            bit_cursor += 4

    @staticmethod
    def _codepoint_subclass(cp: int) -> int:
        ch = chr(cp)
        if ch.isdigit():
            return 0
        if ch.isalnum() or cp == 95:
            return 1
        if ch.isspace():
            return 2
        return 3

    @classmethod
    def _present_subclasses(cls, seg_lo: int, seg_hi: int) -> int:
        if seg_hi - seg_lo > 256:
            return 0b1111
        present = 0
        for cp in range(seg_lo, seg_hi + 1):
            present |= 1 << cls._codepoint_subclass(cp)
            if present == 0b1111:
                break
        return present

    def _register_span(self, lo: int, hi: int, boundaries: set[int]) -> None:
        if hi >= 128:
            clamped_lo = max(128, lo)
            clamped_hi = min(0x10FFFF, hi)
            if clamped_lo <= clamped_hi:
                boundaries.add(clamped_lo)
                boundaries.add(clamped_hi + 1)
        if hi - lo <= 256:
            for cp in range(max(0, lo), min(0x10FFFF, hi) + 1):
                ch = chr(cp)
                for folded in (ch.lower(), ch.upper()):
                    if len(folded) == 1 and ord(folded) >= 128:
                        fcp = ord(folded)
                        boundaries.add(fcp)
                        boundaries.add(fcp + 1)

    def _collect_boundaries(self, items: Sequence[Any], boundaries: set[int]) -> None:
        for op, av in items:
            if op in {_re_constants.LITERAL, _re_constants.NOT_LITERAL}:
                cp = int(av)
                self._register_span(cp, cp, boundaries)
            elif op == _re_constants.IN:
                for sub_op, sub_av in av:
                    if sub_op == _re_constants.LITERAL:
                        cp = int(sub_av)
                        self._register_span(cp, cp, boundaries)
                    elif sub_op == _re_constants.RANGE:
                        self._register_span(int(sub_av[0]), int(sub_av[1]), boundaries)
            elif op in _REPEAT_OPCODES:
                self._collect_boundaries(av[2], boundaries)
            elif op == _re_constants.SUBPATTERN:
                self._collect_boundaries(av[3], boundaries)
            elif _ATOMIC_GROUP_OPCODE is not None and op == _ATOMIC_GROUP_OPCODE:
                self._collect_boundaries(av, boundaries)
            elif op == _re_constants.BRANCH:
                for branch in av[1]:
                    self._collect_boundaries(branch, boundaries)
            elif op in _LOOKAROUND_OPCODES:
                self._collect_boundaries(av[1], boundaries)

    def _non_ascii_span_mask(self, lo: int, hi: int) -> int:
        mask = 0
        for seg_lo, seg_hi, offset in self._segments:
            if seg_lo <= hi and seg_hi >= lo:
                mask |= self._present_subclasses(seg_lo, seg_hi) << offset
        return mask

    def _non_ascii_subclass_mask(self, subclass_bits: int) -> int:
        mask = 0
        for seg_lo, seg_hi, offset in self._segments:
            mask |= (self._present_subclasses(seg_lo, seg_hi) & subclass_bits) << offset
        return mask

    def literal_mask(self, code: int, flags: int) -> int:
        codes = {code}
        if flags & int(_re_constants.SRE_FLAG_IGNORECASE):
            ch = chr(code)
            for folded in (ch.lower(), ch.upper()):
                if len(folded) == 1:
                    codes.add(ord(folded))
        mask = 0
        for cp in codes:
            if cp < 128:
                mask |= 1 << cp
            else:
                mask |= self._non_ascii_span_mask(cp, cp)
        return mask

    def range_mask(self, lo: int, hi: int, flags: int) -> int:
        if hi < lo:
            return 0
        ignore_case = bool(flags & int(_re_constants.SRE_FLAG_IGNORECASE))
        if ignore_case and hi - lo <= 256:
            mask = 0
            for cp in range(lo, hi + 1):
                mask |= self.literal_mask(cp, flags)
            return mask
        mask = 0
        lo_ascii = max(0, lo)
        hi_ascii = min(127, hi)
        if lo_ascii <= hi_ascii:
            if ignore_case:
                for cp in range(lo_ascii, hi_ascii + 1):
                    mask |= self.literal_mask(cp, flags)
            else:
                mask |= ((1 << (hi_ascii - lo_ascii + 1)) - 1) << lo_ascii
        if hi >= 128:
            mask |= self._non_ascii_span_mask(max(128, lo), hi)
        return mask

    def category_mask(self, cat: Any, flags: int) -> int:
        ascii_only = bool(flags & int(_re_constants.SRE_FLAG_ASCII))
        if cat in _DIGIT_CATEGORIES:
            mask = _DIGIT_ASCII_MASK
            if not ascii_only:
                mask |= self._non_ascii_subclass_mask(0b0001)
            return mask
        if cat in _NOT_DIGIT_CATEGORIES:
            return self.all_mask ^ self.category_mask(_re_constants.CATEGORY_DIGIT, flags)
        if cat in _WORD_CATEGORIES:
            mask = _WORD_ASCII_MASK
            if not ascii_only:
                mask |= self._non_ascii_subclass_mask(0b0011)
            return mask
        if cat in _NOT_WORD_CATEGORIES:
            return self.all_mask ^ self.category_mask(_re_constants.CATEGORY_WORD, flags)
        if cat in _SPACE_CATEGORIES:
            mask = _SPACE_ASCII_MASK
            if not ascii_only:
                mask |= self._non_ascii_subclass_mask(0b0100)
            return mask
        if cat in _NOT_SPACE_CATEGORIES:
            return self.all_mask ^ self.category_mask(_re_constants.CATEGORY_SPACE, flags)
        if cat in _LINEBREAK_CATEGORIES:
            return _LINEBREAK_ASCII_MASK
        if cat in _NOT_LINEBREAK_CATEGORIES:
            return self.all_mask ^ _LINEBREAK_ASCII_MASK
        return self.all_mask

    def in_item_mask(self, sub_op: Any, sub_av: Any, flags: int) -> int:
        if sub_op == _re_constants.LITERAL:
            return self.literal_mask(int(sub_av), flags)
        if sub_op == _re_constants.RANGE:
            return self.range_mask(int(sub_av[0]), int(sub_av[1]), flags)
        if sub_op == _re_constants.CATEGORY:
            return self.category_mask(sub_av, flags)
        return 0

    def in_mask(self, in_items: Sequence[Any], flags: int) -> int:
        if not in_items:
            return 0
        negated = in_items[0][0] == _re_constants.NEGATE
        entries = in_items[1:] if negated else in_items
        union_mask = 0
        for sub_op, sub_av in entries:
            union_mask |= self.in_item_mask(sub_op, sub_av, flags)
        return (self.all_mask ^ union_mask) if negated else union_mask

    def first_mask_and_nullable(self, items: Sequence[Any], flags: int) -> tuple[int, bool]:
        """Return ``(first_char_mask, can_match_empty)`` for ``items``."""
        mask = 0
        for op, av in items:
            if op == _re_constants.AT or op in _LOOKAROUND_OPCODES:
                continue
            if op == _re_constants.LITERAL:
                mask |= self.literal_mask(int(av), flags)
                return mask, False
            if op == _re_constants.NOT_LITERAL:
                mask |= self.all_mask ^ self.literal_mask(int(av), flags)
                return mask, False
            if op == _re_constants.ANY:
                dotall = bool(flags & int(_re_constants.SRE_FLAG_DOTALL))
                mask |= self.all_mask if dotall else (self.all_mask ^ (1 << 10))
                return mask, False
            if op == _re_constants.IN:
                mask |= self.in_mask(av, flags)
                return mask, False
            if op == _re_constants.SUBPATTERN:
                sub_flags = (flags | int(av[1])) & ~int(av[2])
                sub_mask, sub_null = self.first_mask_and_nullable(av[3], sub_flags)
                mask |= sub_mask
                if not sub_null:
                    return mask, False
                continue
            if _ATOMIC_GROUP_OPCODE is not None and op == _ATOMIC_GROUP_OPCODE:
                sub_mask, sub_null = self.first_mask_and_nullable(av, flags)
                mask |= sub_mask
                if not sub_null:
                    return mask, False
                continue
            if op == _re_constants.BRANCH:
                branch_null = False
                for branch in av[1]:
                    b_mask, b_null = self.first_mask_and_nullable(branch, flags)
                    mask |= b_mask
                    branch_null = branch_null or b_null
                if not branch_null:
                    return mask, False
                continue
            if op in _REPEAT_OPCODES:
                min_rep, max_rep, sub_items = int(av[0]), av[1], av[2]
                if max_rep == 0:
                    continue
                sub_mask, sub_null = self.first_mask_and_nullable(sub_items, flags)
                mask |= sub_mask
                if min_rep > 0 and not sub_null:
                    return mask, False
                continue
        return mask, True


def _strip_common_literal_prefix(
    branch_a: Sequence[Any],
    branch_b: Sequence[Any],
    builder: _RegexCharsetBuilder,
    flags: int,
) -> tuple[Sequence[Any], Sequence[Any]]:
    idx = 0
    limit = min(len(branch_a), len(branch_b))
    while idx < limit:
        op_a, av_a = branch_a[idx]
        op_b, av_b = branch_b[idx]
        if op_a != _re_constants.LITERAL or op_b != _re_constants.LITERAL:
            break
        if builder.literal_mask(int(av_a), flags) != builder.literal_mask(int(av_b), flags):
            break
        idx += 1
    return branch_a[idx:], branch_b[idx:]


def _validate_regex_subpattern(
    items: Sequence[Any],
    *,
    builder: _RegexCharsetBuilder,
    flags: int,
    in_any_quantifier: bool,
    in_repeating_quantifier: bool,
    has_quantified_pipe: bool,
) -> None:
    for op, av in items:
        if op in _BACKREF_OPCODES:
            raise ValueError("Regex backreferences are not permitted")

        if op in _LOOKAROUND_OPCODES:
            if in_any_quantifier:
                raise ValueError("Quantified lookaround assertions are not permitted")
            _validate_regex_subpattern(
                av[1],
                builder=builder,
                flags=flags,
                in_any_quantifier=False,
                in_repeating_quantifier=False,
                has_quantified_pipe=has_quantified_pipe,
            )
            continue

        if op in _REPEAT_OPCODES:
            min_rep = int(av[0])
            max_rep = av[1]
            sub_items = av[2]
            if min_rep > _MAX_REGEX_REPEAT_COUNT or (
                max_rep != _re_constants.MAXREPEAT and int(max_rep) > _MAX_REGEX_REPEAT_COUNT
            ):
                raise ValueError(f"Regex repeat count exceeds maximum of {_MAX_REGEX_REPEAT_COUNT}")
            if in_repeating_quantifier:
                raise ValueError("Nested quantifiers are not permitted in regex pattern")
            is_repeating = max_rep == _re_constants.MAXREPEAT or int(max_rep) > 1
            if is_repeating:
                first_mask, is_null = builder.first_mask_and_nullable(sub_items, flags)
                if is_null and first_mask == 0:
                    raise ValueError("Zero-width quantified subpatterns are not permitted")
            _validate_regex_subpattern(
                sub_items,
                builder=builder,
                flags=flags,
                in_any_quantifier=True,
                in_repeating_quantifier=in_repeating_quantifier or is_repeating,
                has_quantified_pipe=has_quantified_pipe,
            )
            continue

        if op == _re_constants.SUBPATTERN:
            sub_flags = (flags | int(av[1])) & ~int(av[2])
            _validate_regex_subpattern(
                av[3],
                builder=builder,
                flags=sub_flags,
                in_any_quantifier=in_any_quantifier,
                in_repeating_quantifier=in_repeating_quantifier,
                has_quantified_pipe=has_quantified_pipe,
            )
            continue

        if _ATOMIC_GROUP_OPCODE is not None and op == _ATOMIC_GROUP_OPCODE:
            _validate_regex_subpattern(
                av,
                builder=builder,
                flags=flags,
                in_any_quantifier=in_any_quantifier,
                in_repeating_quantifier=in_repeating_quantifier,
                has_quantified_pipe=has_quantified_pipe,
            )
            continue

        if op == _re_constants.BRANCH:
            branches: Sequence[Sequence[Any]] = av[1]
            for branch in branches:
                _validate_regex_subpattern(
                    branch,
                    builder=builder,
                    flags=flags,
                    in_any_quantifier=in_any_quantifier,
                    in_repeating_quantifier=in_repeating_quantifier,
                    has_quantified_pipe=has_quantified_pipe,
                )
            if in_repeating_quantifier:
                for branch in branches:
                    _, b_null = builder.first_mask_and_nullable(branch, flags)
                    if b_null:
                        raise ValueError("Overlapping quantified alternations are not permitted")
                num_branches = len(branches)
                for i in range(num_branches):
                    for j in range(i + 1, num_branches):
                        rem_a, rem_b = _strip_common_literal_prefix(
                            branches[i], branches[j], builder, flags
                        )
                        mask_a, null_a = builder.first_mask_and_nullable(rem_a, flags)
                        mask_b, null_b = builder.first_mask_and_nullable(rem_b, flags)
                        if null_a or null_b or (mask_a & mask_b) != 0:
                            raise ValueError(
                                "Overlapping quantified alternations are not permitted"
                            )
            continue

        if (
            op == _re_constants.IN
            and in_repeating_quantifier
            and has_quantified_pipe
            and len(av) > 1
            and av[0][0] != _re_constants.NEGATE
        ):
            seen_mask = 0
            for sub_op, sub_av in av:
                item_mask = builder.in_item_mask(sub_op, sub_av, flags)
                if seen_mask & item_mask:
                    raise ValueError("Overlapping quantified alternations are not permitted")
                seen_mask |= item_mask


def validate_regex_pattern(pattern: str) -> None:
    """Validate that ``pattern`` is syntactically valid and safe against ReDoS.

    Args:
        pattern: Regular expression string to inspect.

    Raises:
        ValueError: If ``pattern`` exceeds length limits, fails syntax parsing,
            or contains backtracking-prone constructs.
    """
    if len(pattern) > _MAX_REGEX_PATTERN_LENGTH:
        raise ValueError(
            f"Regex pattern length {len(pattern)} exceeds maximum of {_MAX_REGEX_PATTERN_LENGTH}"
        )
    try:
        parsed = _re_parser.parse(pattern)
    except re.error as exc:
        raise ValueError(f"Invalid regex syntax: {exc}") from exc

    items = list(parsed)
    flags = int(parsed.state.flags)
    builder = _RegexCharsetBuilder(items)
    _validate_regex_subpattern(
        items,
        builder=builder,
        flags=flags,
        in_any_quantifier=False,
        in_repeating_quantifier=False,
        has_quantified_pipe=_has_quantified_pipe_group(pattern),
    )


@functools.lru_cache(maxsize=256)
def _compile_safe_regex(pattern: str) -> re.Pattern[str]:
    """Validate ``pattern`` AST and compile it with bounded LRU caching."""
    validate_regex_pattern(pattern)
    return re.compile(pattern)


def _get_compiled_safe_regex(pattern: str) -> re.Pattern[str] | None:
    """Return a compiled safe regex ``Pattern``, or ``None`` if invalid or unsafe."""
    if len(pattern) > _MAX_REGEX_PATTERN_LENGTH:
        return None
    try:
        return _compile_safe_regex(pattern)
    except ValueError:
        return None


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
                is_cross_scope=is_cross_scope,
            )

        return RuleEvaluationResult(
            rule_id=rule_id,
            rule_name=rule_name,
            rule_type=rule_type,
            severity=severity,
            triggered=triggered,
            message=default_msg,
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
        if not is_cross_scope and not form_key:
            cond_forms = {c.form for c in conditions if c.form}
            if len(cond_forms) > 1:
                is_cross_scope = True
        if is_cross_scope:
            return False, True, "cross_scope"
        if not conditions:
            return False, False, "no_conditions"

        if self._has_unsafe_regex_condition(conditions, data):
            return False, False, "unsafe_regex"

        root_val = self._eval_node(
            cond_root,
            data,
            form_key=form_key,
            rule_type=rule_type,
            group_combinator=root_combinator,
        )
        return root_val is True, False, None

    @staticmethod
    def _has_unsafe_regex_condition(
        conditions: Iterable[ExtractedCondition],
        data: Mapping[str, Any],
    ) -> bool:
        for cond in conditions:
            if cond.operator != "regex":
                continue
            if cond.compare_variable is not None:
                rhs = data.get(cond.compare_variable)
                if _is_empty_record_value(rhs):
                    continue
                pattern_str = str(rhs)
            else:
                pattern_str = "" if cond.value is None or cond.value is True else str(cond.value)
            if _get_compiled_safe_regex(pattern_str) is None:
                return True
        return False

    def validate_rule_regex_patterns(self, rule: Any) -> list[str]:
        """Return validation error descriptions for invalid or unsafe static regex conditions."""
        logic_ast, _xml_err = self._resolve_logic_ast(rule)
        if not isinstance(logic_ast, Mapping):
            return []
        unwrapped = self._unwrap_root(logic_ast)
        cond_root = unwrapped.get("conditions", unwrapped.get("if", unwrapped))
        if not isinstance(cond_root, (Mapping, list)):
            return []
        conditions, _ = self._collect_leaf_conditions(
            cond_root,
            None,
            allow_cross_scope=True,
        )
        issues: list[str] = []
        for cond in conditions:
            if cond.operator != "regex" or cond.compare_variable is not None:
                continue
            pattern_str = "" if cond.value is None or cond.value is True else str(cond.value)
            try:
                validate_regex_pattern(pattern_str)
            except ValueError as exc:
                issues.append(f"variable '{cond.variable}' has unsafe or invalid regex: {exc}")
        return issues

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
        return any(k in node for k in ("variable", "field", "var", "@variable"))

    def _collect_leaf_conditions(
        self,
        node: Any,
        form_key: str | None,
        *,
        negated: bool = False,
        default_operator: str | None = None,
        allow_cross_scope: bool = False,
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
                    allow_cross_scope=allow_cross_scope,
                )
                out.extend(sub_conds)
                cross = cross or sub_cross
            return out, cross

        if not isinstance(node, Mapping):
            return [], False

        if not allow_cross_scope:
            for ckey in _CROSS_SCOPE_KEYS:
                if node.get(ckey):
                    return [], True

        if self._is_leaf_condition(node):
            leaf, is_cross = self._parse_leaf_condition(
                node,
                form_key,
                negated=negated,
                default_operator=default_operator,
                allow_cross_scope=allow_cross_scope,
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
                    allow_cross_scope=allow_cross_scope,
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
                    allow_cross_scope=allow_cross_scope,
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
                    allow_cross_scope=allow_cross_scope,
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
        allow_cross_scope: bool = False,
    ) -> tuple[ExtractedCondition | None, bool]:
        if not allow_cross_scope:
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

        var_name, resolved_form, is_cross = self._resolve_scoped_variable(
            raw_var, cond_form, form_key
        )
        if is_cross and not allow_cross_scope:
            return None, True

        compare_var: str | None = None
        raw_cv_form = cond.get("compareForm") or cond.get("@compareForm")
        cond_cv_form = (
            str(raw_cv_form).strip()
            if isinstance(raw_cv_form, str) and raw_cv_form.strip()
            else None
        )
        for cv_key in _COMPARE_VAR_KEYS:
            cv_raw = cond.get(cv_key) or cond.get(f"@{cv_key}")
            if isinstance(cv_raw, str) and cv_raw.strip():
                cv_name, cv_form, cv_cross = self._resolve_scoped_variable(
                    cv_raw, cond_cv_form, form_key or resolved_form
                )
                if (
                    cv_cross or (resolved_form and cv_form and cv_form != resolved_form)
                ) and not allow_cross_scope:
                    return None, True
                if resolved_form is None and cv_form is not None:
                    resolved_form = cv_form
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
            ),
            False,
        )

    @staticmethod
    def _kleene_not(value: bool | None) -> bool | None:
        return None if value is None else (not value)

    @staticmethod
    def _kleene_combine(
        values: Sequence[bool | None],
        combinator: Literal["AND", "OR"],
    ) -> bool | None:
        if not values:
            return False
        if combinator == "OR":
            if any(v is True for v in values):
                return True
            if all(v is False for v in values):
                return False
            return None
        if any(v is False for v in values):
            return False
        if all(v is True for v in values):
            return True
        return None

    def _eval_combinator_branch(
        self,
        branch_val: Any,
        branch_combinator: Literal["AND", "OR"],
        data: Mapping[str, Any],
        *,
        form_key: str | None,
        rule_type: str | None,
        default_operator: str | None,
        results: list[bool | None],
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
    ) -> bool | None:
        if isinstance(node, list):
            if not node:
                return False
            return self._kleene_combine(
                [
                    self._eval_node(
                        item,
                        data,
                        form_key=form_key,
                        rule_type=rule_type,
                        default_operator=default_operator,
                    )
                    for item in node
                ],
                "OR" if group_combinator == "OR" else "AND",
            )
        if not isinstance(node, Mapping):
            return False

        if self._is_leaf_condition(node):
            leaf, is_cross = self._parse_leaf_condition(
                node,
                form_key,
                default_operator=default_operator,
            )
            if is_cross or leaf is None:
                return None
            return self._eval_leaf(leaf, data, rule_type=rule_type)

        combinator = self._node_combinator(node, default=group_combinator or "AND")
        results: list[bool | None] = []

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
                        self._kleene_not(
                            self._eval_node(
                                item,
                                data,
                                form_key=form_key,
                                rule_type=rule_type,
                                default_operator=default_operator,
                            )
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
        return self._kleene_combine(results, combinator)

    def _eval_leaf(
        self,
        cond: ExtractedCondition,
        data: Mapping[str, Any],
        *,
        rule_type: str | None,
    ) -> bool | None:
        actual = data.get(cond.variable)
        op = cond.operator

        if op == "empty":
            base = _is_empty_record_value(actual)
            return base if cond.expected_truth else (not base)

        if op == "not_empty":
            base = not _is_empty_record_value(actual)
            return base if cond.expected_truth else (not base)

        if cond.compare_variable is not None:
            rhs = data.get(cond.compare_variable)
            if _is_empty_record_value(actual) or _is_empty_record_value(rhs):
                return None
            if op == "regex" and _get_compiled_safe_regex(str(rhs)) is None:
                return False
        else:
            rhs = cond.value
            # Empty XML tag <value/> parses to True via _parse_xml_element
            if rhs is True:
                rhs = ""
            if op == "regex":
                pattern_str = "" if rhs is None or rhs is True else str(rhs)
                if _get_compiled_safe_regex(pattern_str) is None:
                    return False

            if _is_empty_record_value(actual):
                # Explicit comparison to empty string (e.g. VAR == "" or VAR != "")
                if rhs == "" and op in {"==", "!="}:
                    base = actual is None or actual == ""
                    if op == "!=":
                        base = not base
                    return base if cond.expected_truth else (not base)
                # Required rule checking missingness via <false/>
                if (rule_type or "").strip().lower() == "required" and not cond.expected_truth:
                    return True
                return None

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
            if isinstance(actual, str) and len(actual) > _MAX_REGEX_INPUT_LENGTH:
                return False
            actual_str = actual if isinstance(actual, str) else str(actual)
            if len(actual_str) > _MAX_REGEX_INPUT_LENGTH:
                return False
            expected_str = "" if expected is None or expected is True else str(expected)
            compiled = _get_compiled_safe_regex(expected_str)
            if compiled is None:
                return False
            return compiled.search(actual_str) is not None

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
            return op == "!="

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

        # Chronological comparison when both operands parse as valid MM-DD-YYYY or ISO dates
        dt_actual = _parse_comparable_temporal(actual)
        dt_expected = _parse_comparable_temporal(expected)
        if dt_actual is not None and dt_expected is not None:
            if op == "==":
                return dt_actual == dt_expected
            if op == "!=":
                return dt_actual != dt_expected
            if op == "<":
                return dt_actual < dt_expected
            if op == "<=":
                return dt_actual <= dt_expected
            if op == ">":
                return dt_actual > dt_expected
            if op == ">=":
                return dt_actual >= dt_expected
            return False

        # Fallback lexical/string comparison (supports Coded strings)
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
