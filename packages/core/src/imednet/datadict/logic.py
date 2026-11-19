"""Business logic XML parsing and AST evaluation for iMednet data dictionary."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class VariableConstraints:
    """Normalized validation constraints extracted from a DataDictionary Variable."""

    choices: list[str] = field(default_factory=list)
    required_policy: Literal["yes", "auto_query", "no"] = "no"
    min_value: float | None = None
    max_value: float | None = None
    min_length: int | None = None
    max_length: int | None = None


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
        cond_root = unwrapped.get("conditions", unwrapped.get("if", unwrapped))
        if not isinstance(cond_root, (Mapping, list)):
            return False, False, "no_conditions"

        conditions, is_cross_scope = self._collect_leaf_conditions(cond_root, form_key)
        if is_cross_scope:
            return False, True, "cross_scope"
        if not conditions:
            return False, False, "no_conditions"

        triggered = self._eval_node(cond_root, data, form_key=form_key, rule_type=rule_type)
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
        severity, is_ui_only, message = self._classify_rule(rule_type, unwrapped)
        cond_root = unwrapped.get("conditions", unwrapped.get("if", unwrapped))
        if not isinstance(cond_root, (Mapping, list)):
            return ExtractedRuleMetadata(
                rule_id=rule_id,
                rule_name=rule_name,
                form=resolved_form,
                rule_type=rule_type,
                severity=severity,
                logical_operator="AND",
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
            self._effective_combinator(cond_root) if isinstance(cond_root, Mapping) else "AND"
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

    def _node_combinator(
        self,
        node: Mapping[str, Any],
        default: Literal["AND", "OR"] = "AND",
    ) -> Literal["AND", "OR"]:
        explicit = self._explicit_combinator(node)
        if explicit is not None:
            return explicit
        has_or_marker = any(node.get(k) is True for k in _OR_GROUP_KEYS)
        has_and_marker = any(node.get(k) is True for k in _AND_GROUP_KEYS)
        if has_or_marker and not has_and_marker:
            return "OR"
        if has_and_marker and not has_or_marker:
            return "AND"
        return default

    def _effective_combinator(self, node: Mapping[str, Any]) -> Literal["AND", "OR"]:
        explicit = self._explicit_combinator(node)
        if explicit is not None:
            return explicit
        if any(node.get(k) is True for k in _OR_GROUP_KEYS) and not any(
            node.get(k) is True for k in _AND_GROUP_KEYS
        ):
            return "OR"
        content_keys = [
            k
            for k in node
            if not str(k).startswith("@")
            and k != "#text"
            and node[k] is not True
            and str(k).lstrip("@").replace("_", "").replace("-", "").lower()
            not in _COMBINATOR_ATTR_NAMES
        ]
        if len(content_keys) == 1:
            only_key = content_keys[0]
            only_val = node[only_key]
            if only_key in _OR_GROUP_KEYS:
                if isinstance(only_val, Mapping):
                    return "OR"
                if isinstance(only_val, list) and (
                    not only_val or not all(self._is_xml_group_dict(item) for item in only_val)
                ):
                    return "OR"
            if only_key in ("conditions", "conditionGroup", "group", "if") and isinstance(
                only_val, Mapping
            ):
                return self._effective_combinator(only_val)
        return "AND"

    def _is_xml_group_dict(self, item: Any) -> bool:
        if not isinstance(item, Mapping) or self._is_leaf_condition(item):
            return False
        if any(k in item for k in _CONTAINER_GROUP_KEYS):
            return True
        content_keys = [k for k in item if not str(k).startswith("@") and k != "#text"]
        if len(content_keys) > 1:
            return True
        if len(content_keys) == 1:
            only_key = str(content_keys[0]).lower()
            if only_key in _OPERATOR_TAG_MAP and isinstance(item[content_keys[0]], list):
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

        var_name, resolved_form, is_cross = self._resolve_scoped_variable(
            raw_var, cond_form, form_key
        )
        if is_cross:
            return None, True

        compare_var: str | None = None
        for cv_key in _COMPARE_VAR_KEYS:
            cv_raw = cond.get(cv_key)
            if isinstance(cv_raw, str) and cv_raw.strip():
                cv_name, _, cv_cross = self._resolve_scoped_variable(cv_raw, cond_form, form_key)
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
            not branch_val or not all(self._is_xml_group_dict(item) for item in branch_val)
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

        for key in _CONTAINER_GROUP_KEYS:
            if key in node and node[key] is not True:
                for child in _as_list(node[key]):
                    results.append(
                        self._eval_node(
                            child,
                            data,
                            form_key=form_key,
                            rule_type=rule_type,
                            default_operator=default_operator,
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
