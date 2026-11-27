"""JSON Schema generation and validation for iMednet Data Dictionary."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .logic import LogicTreeEvaluator, extract_variable_constraints, parse_logic_xml
from .models import BusinessLogicRule, DataDictionary, Form
from .normalize import _is_file_target, _safe_is_file, normalize_form_id

SCHEMA_PATH = Path(__file__).parent / "resources" / "data-dictionary-1.0.0.schema.json"
_RULE_VALIDATOR = LogicTreeEvaluator()

__all__ = [
    "SCHEMA_PATH",
    "_is_file_target",
    "_safe_is_file",
    "get_schema",
    "validate",
]


def _validate_rule_list(
    rules: list[BusinessLogicRule],
    loc_prefix: str,
) -> list[str]:
    errors: list[str] = []
    for idx, rule in enumerate(rules):
        rule_label = str(rule.name or rule.id or f"index {idx}")
        for issue in _RULE_VALIDATOR.validate_rule_regex_patterns(rule):
            errors.append(f"[{loc_prefix}.{idx}.logic] Rule '{rule_label}': {issue}")
    return errors


def _validate_business_logic_regexes(dd: DataDictionary) -> list[str]:
    errors: list[str] = []
    for form_idx, form in enumerate(dd.forms):
        errors.extend(_validate_rule_list(form.businessLogic, f"forms.{form_idx}.businessLogic"))
    errors.extend(_validate_rule_list(dd.unassignedBusinessLogic, "unassignedBusinessLogic"))
    return errors


def get_schema() -> dict[str, Any]:
    """Generate or retrieve the canonical JSON Schema for DataDictionary.

    Returns:
        JSON Schema dictionary.
    """
    schema = DataDictionary.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "iMednet Data Dictionary Schema"
    schema["version"] = "1.0.0"
    required = list(schema.get("required", []))
    if "forms" not in required:
        required.append("forms")
    schema["required"] = required
    return schema


_SCHEMA_COND_REF_KEYS = (
    "variable",
    "@variable",
    "field",
    "@field",
    "target",
    "@target",
    "item",
    "@item",
    "compareVariable",
    "@compareVariable",
)


def _collect_condition_nodes(node: Any) -> list[dict[str, Any]]:
    """Collect atomic condition dicts from a parsed BusinessLogicRule logic AST."""
    found: list[dict[str, Any]] = []
    if isinstance(node, list):
        for item in node:
            found.extend(_collect_condition_nodes(item))
        return found
    if not isinstance(node, dict):
        return found
    if any(k in node for k in _SCHEMA_COND_REF_KEYS):
        found.append(node)
    for key, val in node.items():
        if key in ("actions", "action", "then", "formalExpressions", "aliases"):
            continue
        if isinstance(val, (dict, list)):
            found.extend(_collect_condition_nodes(val))
    return found


def _validate_rule_semantics(
    rule: BusinessLogicRule,
    loc_prefix: str,
    *,
    container_form_key: str | None,
    forms_by_key: dict[str, Form],
    vars_by_form: dict[str, set[str]],
    seen_rule_ids: set[str],
    errors: list[str],
) -> None:
    """Check duplicate rule IDs and broken form/variable references on a rule."""
    if rule.id is not None and str(rule.id).strip():
        rid = str(rule.id).strip()
        if rid in seen_rule_ids:
            errors.append(f"[{loc_prefix}.id] Duplicate business logic rule id '{rid}'")
        else:
            seen_rule_ids.add(rid)

    rule_form = rule.form.strip() if isinstance(rule.form, str) and rule.form.strip() else None
    if rule_form is not None:
        if rule_form not in forms_by_key:
            errors.append(
                f"[{loc_prefix}.form] Broken reference: form '{rule_form}' does not exist"
            )
        elif container_form_key is not None and rule_form != container_form_key:
            errors.append(
                f"[{loc_prefix}.form] Rule form '{rule_form}' does not match parent form "
                f"'{container_form_key}'"
            )

    logic_dict: dict[str, Any] | None = rule.logic if isinstance(rule.logic, dict) else None
    if logic_dict is None:
        broken_marker = (
            rule.extraAttributes.get("malformedLogicXml")
            or rule.extraAttributes.get("logicXmlError")
            or rule.extraAttributes.get("brokenLogicXml")
        )
        if isinstance(rule.logicRawXml, str) and rule.logicRawXml.strip():
            try:
                parsed_raw = parse_logic_xml(rule.logicRawXml)
                if isinstance(parsed_raw, dict):
                    logic_dict = parsed_raw
            except ValueError as exc:
                errors.append(f"[{loc_prefix}.logicRawXml] {exc}")
                return
        elif broken_marker:
            marker_msg = (
                broken_marker if isinstance(broken_marker, str) else "Malformed business logic XML"
            )
            errors.append(f"[{loc_prefix}.logic] {marker_msg}")
            return

    if not isinstance(logic_dict, dict) or logic_dict.get("odmElement"):
        return

    default_form = rule_form or container_form_key
    for cond in _collect_condition_nodes(logic_dict.get("conditions", logic_dict)):
        for ref_key in _SCHEMA_COND_REF_KEYS:
            raw_ref = cond.get(ref_key)
            if not isinstance(raw_ref, str) or not raw_ref.strip():
                continue
            is_compare = ref_key.lstrip("@") == "compareVariable"
            explicit_form = (
                (cond.get("form") or cond.get("@form"))
                if not is_compare
                else (cond.get("compareForm") or cond.get("@compareForm") or None)
            )
            explicit_fk = (
                str(explicit_form).strip()
                if isinstance(explicit_form, str) and explicit_form.strip()
                else None
            )
            ref_var, ref_fk, _ = LogicTreeEvaluator._resolve_scoped_variable(
                raw_ref.strip(),
                explicit_fk,
                None,
            )
            target_fk = explicit_fk or ref_fk or default_form
            if target_fk is not None:
                if target_fk not in forms_by_key:
                    errors.append(
                        f"[{loc_prefix}.logic] Broken reference: form '{target_fk}' "
                        f"referenced by '{raw_ref}' does not exist"
                    )
                else:
                    defined_vars = vars_by_form[target_fk]
                    if (
                        defined_vars
                        and ref_var not in defined_vars
                        and raw_ref.strip() not in defined_vars
                    ):
                        errors.append(
                            f"[{loc_prefix}.logic] Broken reference: variable '{ref_var}' "
                            f"is not defined on form '{target_fk}'"
                        )


def _validate_semantics(dd: DataDictionary) -> list[str]:
    """Validate duplicate keys, broken references, and inverted variable bounds."""
    errors: list[str] = []
    forms_by_key: dict[str, Form] = {}
    vars_by_form: dict[str, set[str]] = {}
    seen_form_ids: dict[str, str] = {}

    # Pass 1: Check duplicate formKeys, formIds, variableNames, choiceValues, and inverted bounds
    for f_idx, form in enumerate(dd.forms):
        fk = form.formKey.strip()
        if fk in forms_by_key:
            errors.append(f"[forms.{f_idx}.formKey] Duplicate formKey '{fk}'")
        else:
            forms_by_key[fk] = form
            vars_by_form[fk] = set()

        norm_fid = normalize_form_id(form.formId, fk)
        if norm_fid is not None:
            fid_key = str(norm_fid)
            if fid_key in seen_form_ids:
                errors.append(
                    f"[forms.{f_idx}.formId] Duplicate formId '{fid_key}' "
                    f"(already used by form '{seen_form_ids[fid_key]}')"
                )
            else:
                seen_form_ids[fid_key] = fk

        seen_vars: set[str] = set()
        for v_idx, var in enumerate(form.variables):
            vn = var.variableName.strip()
            v_loc = f"forms.{f_idx}.variables.{v_idx}"
            if vn in seen_vars:
                errors.append(
                    f"[{v_loc}.variableName] Duplicate variableName '{vn}' on form '{fk}'"
                )
            else:
                seen_vars.add(vn)
                if fk in vars_by_form:
                    vars_by_form[fk].add(vn)

            seen_choices: set[str] = set()
            for c_idx, choice in enumerate(var.choices):
                cv = choice.choiceValue
                if cv in seen_choices:
                    errors.append(
                        f"[{v_loc}.choices.{c_idx}.choiceValue] Duplicate choiceValue '{cv}' "
                        f"on variable '{vn}'"
                    )
                else:
                    seen_choices.add(cv)

            constraints = extract_variable_constraints(var)
            if (
                constraints.min_value is not None
                and constraints.max_value is not None
                and constraints.min_value > constraints.max_value
            ):
                errors.append(
                    f"[{v_loc}] Inverted numeric bounds on variable '{vn}': "
                    f"minValue ({constraints.min_value}) > maxValue ({constraints.max_value})"
                )
            if (
                constraints.min_length is not None
                and constraints.max_length is not None
                and constraints.min_length > constraints.max_length
            ):
                errors.append(
                    f"[{v_loc}] Inverted length bounds on variable '{vn}': "
                    f"minLength ({constraints.min_length}) > maxLength ({constraints.max_length})"
                )

    # Collect all known rule IDs for OID reference checks
    all_rule_ids: set[str] = set()
    for form in dd.forms:
        for rule in form.businessLogic:
            if rule.id is not None and str(rule.id).strip():
                all_rule_ids.add(str(rule.id).strip())
    for rule in dd.unassignedBusinessLogic:
        if rule.id is not None and str(rule.id).strip():
            all_rule_ids.add(str(rule.id).strip())

    # Pass 2: Validate rules and cross-entity references
    seen_rule_ids: set[str] = set()
    for f_idx, form in enumerate(dd.forms):
        fk = form.formKey.strip()
        f_cond = form.extraAttributes.get("collectionExceptionConditionOid")
        if isinstance(f_cond, str) and f_cond.strip() and f_cond.strip() not in all_rule_ids:
            errors.append(
                f"[forms.{f_idx}.extraAttributes.collectionExceptionConditionOid] "
                f"Broken reference: condition '{f_cond.strip()}' does not exist"
            )
        for v_idx, var in enumerate(form.variables):
            v_loc = f"forms.{f_idx}.variables.{v_idx}.extraAttributes"
            m_oid = var.extraAttributes.get("methodOid")
            if isinstance(m_oid, str) and m_oid.strip() and m_oid.strip() not in all_rule_ids:
                errors.append(
                    f"[{v_loc}.methodOid] Broken reference: method '{m_oid.strip()}' does not exist"
                )
            c_oid = var.extraAttributes.get("collectionExceptionConditionOid")
            if isinstance(c_oid, str) and c_oid.strip() and c_oid.strip() not in all_rule_ids:
                errors.append(
                    f"[{v_loc}.collectionExceptionConditionOid] "
                    f"Broken reference: condition '{c_oid.strip()}' does not exist"
                )
        for r_idx, rule in enumerate(form.businessLogic):
            _validate_rule_semantics(
                rule,
                f"forms.{f_idx}.businessLogic.{r_idx}",
                container_form_key=fk,
                forms_by_key=forms_by_key,
                vars_by_form=vars_by_form,
                seen_rule_ids=seen_rule_ids,
                errors=errors,
            )

    for r_idx, rule in enumerate(dd.unassignedBusinessLogic):
        _validate_rule_semantics(
            rule,
            f"unassignedBusinessLogic.{r_idx}",
            container_form_key=None,
            forms_by_key=forms_by_key,
            vars_by_form=vars_by_form,
            seen_rule_ids=seen_rule_ids,
            errors=errors,
        )

    visits = dd.metadata.extraAttributes.get("visits")
    visit_oids: set[str] = set()
    if isinstance(visits, list):
        for vis_idx, visit in enumerate(visits):
            if not isinstance(visit, dict):
                continue
            v_oid = str(visit.get("oid") or "").strip()
            if v_oid:
                if v_oid in visit_oids:
                    errors.append(
                        f"[metadata.extraAttributes.visits.{vis_idx}.oid] "
                        f"Duplicate visit oid '{v_oid}'"
                    )
                else:
                    visit_oids.add(v_oid)
            v_forms = visit.get("forms")
            if isinstance(v_forms, list):
                for vf_idx, vf in enumerate(v_forms):
                    if not isinstance(vf, dict):
                        continue
                    ref_fk = str(vf.get("formOid") or "").strip()
                    if ref_fk and ref_fk not in forms_by_key:
                        errors.append(
                            f"[metadata.extraAttributes.visits.{vis_idx}.forms.{vf_idx}.formOid] "
                            f"Broken reference: form '{ref_fk}' does not exist"
                        )

    proto = dd.metadata.extraAttributes.get("protocol")
    if isinstance(proto, dict) and isinstance(visits, list):
        se_refs = proto.get("studyEventRefs")
        if isinstance(se_refs, list):
            for se_idx, se_ref in enumerate(se_refs):
                if not isinstance(se_ref, dict):
                    continue
                se_oid = str(se_ref.get("studyEventOid") or "").strip()
                if se_oid and se_oid not in visit_oids:
                    errors.append(
                        f"[metadata.extraAttributes.protocol.studyEventRefs.{se_idx}.studyEventOid] "
                        f"Broken reference: study event '{se_oid}' does not exist"
                    )

    return errors


def _parse_json_validation_source(source: str | Path) -> tuple[Any, str | None]:
    """Load JSON from a file path or JSON string, returning ``(data, error_message)``."""
    path_obj = Path(source)
    if _is_file_target(source):
        if not _safe_is_file(path_obj):
            return None, f"FileNotFoundError: Target file '{source}' does not exist"
        try:
            return json.loads(path_obj.read_text(encoding="utf-8")), None
        except Exception as exc:
            return None, f"Failed to read JSON file '{source}': {exc}"
    if _safe_is_file(path_obj):
        try:
            return json.loads(path_obj.read_text(encoding="utf-8")), None
        except Exception as exc:
            return None, f"Failed to read JSON file '{source}': {exc}"
    try:
        return json.loads(str(source)), None
    except Exception as exc:
        return None, f"Failed to parse JSON string: {exc}"


def validate(
    target: dict[str, Any] | str | Path | DataDictionary,
    schema: dict[str, Any] | None = None,
) -> list[str]:
    """Validate a data dictionary against the canonical model or an optional JSON schema.

    Args:
        target: DataDictionary instance, dict, JSON string, or path to JSON file.
        schema: Optional JSON Schema to validate against via jsonschema.

    Returns:
        List of error strings (empty if valid).
    """
    errors: list[str] = []

    # 1. Load target to dict/object
    data: Any
    if isinstance(target, DataDictionary):
        data = target.model_dump(mode="json")
    elif isinstance(target, str | Path):
        data, load_err = _parse_json_validation_source(target)
        if load_err is not None:
            return [load_err]
    elif isinstance(target, dict):
        data = target
    else:
        return [f"Unsupported target type for validation: {type(target)}"]

    # 2. If a custom JSON schema is provided, validate via jsonschema if available
    if schema is not None:
        try:
            import jsonschema

            validator = jsonschema.Draft202012Validator(schema)
            for err in validator.iter_errors(data):
                loc = ".".join(str(p) for p in err.absolute_path)
                prefix = f"[{loc}] " if loc else ""
                errors.append(f"{prefix}{err.message}")
            if not errors:
                with contextlib.suppress(ValidationError):
                    dd_checked = DataDictionary.model_validate(data)
                    errors.extend(_validate_business_logic_regexes(dd_checked))
                    errors.extend(_validate_semantics(dd_checked))
            return errors
        except ImportError:
            errors.append(
                "jsonschema library is not installed; falling back to Pydantic model validation."
            )

    # 3. Model validation via Pydantic
    if not isinstance(target, DataDictionary) and isinstance(data, dict) and "forms" not in data:
        errors.append("[forms] Field required")

    try:
        dd = DataDictionary.model_validate(data)
    except ValidationError as val_err:
        for err in val_err.errors():
            loc = ".".join(str(p) for p in err.get("loc", ()))
            msg = err.get("msg", "Validation error")
            errors.append(f"[{loc}] {msg}")
        return errors

    if errors:
        return errors

    # 4. Regex safety and semantic validation (duplicate keys, broken references, inverted bounds)
    errors.extend(_validate_business_logic_regexes(dd))
    errors.extend(_validate_semantics(dd))
    return errors
