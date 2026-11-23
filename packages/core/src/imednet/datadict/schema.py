"""JSON Schema generation and validation for iMednet Data Dictionary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .logic import (
    VALID_RANGE_COMPARATORS,
    compare_temporal_strings,
    compile_odm_formal_expression,
    compile_redcap_branching_logic,
    extract_variable_constraints,
    is_valid_iso_temporal_bound,
    parse_logic_xml,
)
from .models import BusinessLogicRule, DataDictionary, Form, Variable
from .normalize import _is_file_target, _safe_is_file

SCHEMA_PATH = Path(__file__).parent / "resources" / "data-dictionary-1.0.0.schema.json"

__all__ = [
    "SCHEMA_PATH",
    "_is_file_target",
    "_safe_is_file",
    "get_schema",
    "validate",
]

_BENIGN_LOADER_WARNING_PREFIXES = (
    "Unknown column ",
    "Unmapped attribute ",
    "Unmapped attribute/column ",
    "Unmapped element ",
)

_CATEGORICAL_CHOICE_TYPES = frozenset(
    {
        "coded",
        "radio",
        "radiobutton",
        "radio button",
        "dropdown",
        "drop down",
        "dropdownlist",
        "dropdown list",
        "select",
        "single select",
        "singleselect",
        "checkbox",
        "check box",
        "multi select",
        "multiselect",
    }
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
        "durationdatetime",
        "intervaldatetime",
        "date/time precision",
        "datetime precision",
        "date time precision",
        "time",
    }
)

_NUMERIC_VARIABLE_TYPES = frozenset(
    {
        "int",
        "integer",
        "number",
        "numeric",
        "float",
        "decimal",
        "double",
        "calculation",
        "calc",
        "calculated",
        "slider",
    }
)


def _safe_is_dir(p: Path) -> bool:
    """Safely check if a Path is an existing directory."""
    try:
        return p.is_dir()
    except (OSError, ValueError):
        return False


def _normalize_type_label(var_type: str | None) -> str:
    if not var_type:
        return ""
    return " ".join(var_type.strip().lower().replace("_", " ").replace("-", " ").split())


def _promote_loader_warnings(warnings: list[str]) -> list[str]:
    """Convert structural/semantic loader warnings into validation errors."""
    return [w for w in warnings if not w.startswith(_BENIGN_LOADER_WARNING_PREFIXES)]


def _collect_logic_leaf_refs(
    node: Any,
    out: list[tuple[str | None, str | None, str | None]],
) -> None:
    """Recursively collect (form_ref, var_ref, compare_var_ref) tuples from native rule ASTs."""
    if isinstance(node, list):
        for item in node:
            _collect_logic_leaf_refs(item, out)
        return
    if not isinstance(node, dict):
        return

    for logic_key in ("and", "or", "not", "conditions", "criteria", "when", "if"):
        if logic_key in node:
            _collect_logic_leaf_refs(node[logic_key], out)

    raw_var = (
        node.get("variable")
        or node.get("variableName")
        or node.get("field")
        or node.get("question")
        or node.get("@variable")
        or node.get("@field")
    )
    has_operator_or_val = any(
        k in node
        for k in (
            "operator",
            "op",
            "@operator",
            "@op",
            "value",
            "expected",
            "@value",
            "compareVariable",
            "otherVariable",
        )
    )
    if isinstance(raw_var, str) and raw_var.strip() and has_operator_or_val:
        raw_form = node.get("form") or node.get("formKey") or node.get("@form")
        form_ref = str(raw_form).strip() if isinstance(raw_form, str) and raw_form.strip() else None
        var_ref = raw_var.strip()
        if not node.get("@literalVariable") and form_ref is None and "." in var_ref:
            prefix, remainder = var_ref.split(".", 1)
            if prefix and remainder:
                form_ref = prefix
                var_ref = remainder

        cmp_raw = (
            node.get("compareVariable") or node.get("otherVariable") or node.get("@compareVariable")
        )
        cmp_ref = str(cmp_raw).strip() if isinstance(cmp_raw, str) and cmp_raw.strip() else None
        out.append((form_ref, var_ref, cmp_ref))

    for key, val in node.items():
        if key not in ("and", "or", "not", "conditions", "criteria", "when", "if") and isinstance(
            val, dict | list
        ):
            _collect_logic_leaf_refs(val, out)


def _validate_variable_bounds_and_ranges(
    var: Variable,
    form_key: str,
    norm_type: str,
    errors: list[str],
) -> None:
    """Check variable bounds and ODM RangeChecks for malformed or inverted definitions."""
    extra = var.extraAttributes or {}
    is_temporal = norm_type in _TEMPORAL_VARIABLE_TYPES
    is_numeric = norm_type in _NUMERIC_VARIABLE_TYPES

    # Check raw temporal bounds before extraction so malformed date strings are caught
    if is_temporal:
        for min_key in ("minDate", "min_date", "textValidationMin", "min"):
            raw_min = extra.get(min_key)
            if (
                isinstance(raw_min, str)
                and raw_min.strip()
                and not is_valid_iso_temporal_bound(raw_min, allow_partial=True)
            ):
                errors.append(
                    f"Invalid temporal minimum bound '{raw_min}' on variable "
                    f"'{var.variableName}' in form '{form_key}'"
                )
        for max_key in ("maxDate", "max_date", "textValidationMax", "max"):
            raw_max = extra.get(max_key)
            if (
                isinstance(raw_max, str)
                and raw_max.strip()
                and not is_valid_iso_temporal_bound(raw_max, allow_partial=True)
            ):
                errors.append(
                    f"Invalid temporal maximum bound '{raw_max}' on variable "
                    f"'{var.variableName}' in form '{form_key}'"
                )

    # Check negative raw length bounds
    for len_key in ("minLength", "min_length", "maxLength", "max_length", "length"):
        raw_len = extra.get(len_key)
        if isinstance(raw_len, int | float) and not isinstance(raw_len, bool) and raw_len < 0:
            errors.append(
                f"Negative length bound ({len_key}={raw_len}) on variable "
                f"'{var.variableName}' in form '{form_key}'"
            )

    # Validate raw rangeChecks entries
    raw_rcs = extra.get("rangeChecks")
    if isinstance(raw_rcs, list):
        for idx, rc in enumerate(raw_rcs):
            if not isinstance(rc, dict):
                errors.append(
                    f"Malformed RangeCheck #{idx + 1} on variable "
                    f"'{var.variableName}' in form '{form_key}': expected object"
                )
                continue
            comp_raw = rc.get("comparator")
            fe_raw = rc.get("formalExpression")
            has_fe = False
            if isinstance(fe_raw, dict):
                expr_str = str(fe_raw.get("expression") or "").strip()
                ctx_str = fe_raw.get("context")
                has_fe = bool(expr_str)
                if has_fe:
                    try:
                        compile_odm_formal_expression(
                            expr_str, context=ctx_str, self_var=var.variableName
                        )
                    except ValueError as exc:
                        errors.append(
                            f"Malformed FormalExpression in RangeCheck on variable "
                            f"'{var.variableName}' in form '{form_key}': {exc}"
                        )
            elif isinstance(fe_raw, str) and fe_raw.strip():
                has_fe = True
                try:
                    compile_odm_formal_expression(
                        fe_raw.strip(), context=rc.get("context"), self_var=var.variableName
                    )
                except ValueError as exc:
                    errors.append(
                        f"Malformed FormalExpression in RangeCheck on variable "
                        f"'{var.variableName}' in form '{form_key}': {exc}"
                    )

            if comp_raw is not None and str(comp_raw).strip() != "":
                comp_norm = str(comp_raw).strip().upper()
                if comp_norm not in VALID_RANGE_COMPARATORS:
                    errors.append(
                        f"Unsupported RangeCheck comparator '{comp_raw}' on variable "
                        f"'{var.variableName}' in form '{form_key}'"
                    )
                raw_vals = rc.get("checkValues")
                single_val = rc.get("checkValue")
                vals: list[str] = []
                if isinstance(raw_vals, list):
                    vals = [str(v) for v in raw_vals if v is not None and str(v).strip() != ""]
                if not vals and single_val is not None and str(single_val).strip() != "":
                    vals = [str(single_val)]
                if not vals:
                    errors.append(
                        f"RangeCheck with comparator '{comp_norm}' on variable "
                        f"'{var.variableName}' in form '{form_key}' is missing CheckValue"
                    )
                elif is_numeric and comp_norm in {"LT", "LE", "GT", "GE"}:
                    try:
                        float(vals[0])
                    except ValueError:
                        errors.append(
                            f"Non-numeric CheckValue '{vals[0]}' for RangeCheck '{comp_norm}' "
                            f"on numeric variable '{var.variableName}' in form '{form_key}'"
                        )
                elif (
                    is_temporal
                    and comp_norm in {"LT", "LE", "GT", "GE"}
                    and not is_valid_iso_temporal_bound(vals[0], allow_partial=True)
                ):
                    errors.append(
                        f"Invalid temporal CheckValue '{vals[0]}' for RangeCheck '{comp_norm}' "
                        f"on temporal variable '{var.variableName}' in form '{form_key}'"
                    )
            elif not has_fe:
                errors.append(
                    f"RangeCheck #{idx + 1} on variable '{var.variableName}' in form "
                    f"'{form_key}' must define either Comparator or FormalExpression"
                )

    constraints = extract_variable_constraints(var)
    if (
        constraints.min_value is not None
        and constraints.max_value is not None
        and constraints.min_value > constraints.max_value
    ):
        errors.append(
            f"Inverted numeric bounds (min={constraints.min_value} > max={constraints.max_value}) "
            f"on variable '{var.variableName}' in form '{form_key}'"
        )

    if (
        constraints.soft_min_value is not None
        and constraints.soft_max_value is not None
        and constraints.soft_min_value > constraints.soft_max_value
    ):
        errors.append(
            f"Inverted soft numeric bounds "
            f"(min={constraints.soft_min_value} > max={constraints.soft_max_value}) "
            f"on variable '{var.variableName}' in form '{form_key}'"
        )

    # Check contradictory strict numeric RangeChecks (e.g. GT 10 and LT 10, or GT 10 and LE 10)
    lower_strict: tuple[float, bool] | None = None  # (bound, is_exclusive)
    upper_strict: tuple[float, bool] | None = None
    for rc_item in constraints.range_checks:
        comp = (rc_item.comparator or "").upper()
        if comp in {"GT", "GE", "LT", "LE"} and rc_item.check_value is not None:
            try:
                num_v = float(rc_item.check_value)
            except ValueError:
                continue
            if comp in {"GT", "GE"}:
                excl = comp == "GT"
                if (
                    lower_strict is None
                    or num_v > lower_strict[0]
                    or (num_v == lower_strict[0] and excl)
                ):
                    lower_strict = (num_v, excl)
            else:
                excl = comp == "LT"
                if (
                    upper_strict is None
                    or num_v < upper_strict[0]
                    or (num_v == upper_strict[0] and excl)
                ):
                    upper_strict = (num_v, excl)
    if (
        lower_strict is not None
        and upper_strict is not None
        and (
            lower_strict[0] > upper_strict[0]
            or (lower_strict[0] == upper_strict[0] and (lower_strict[1] or upper_strict[1]))
        )
    ):
        errors.append(
            f"Contradictory RangeCheck numeric bounds on variable "
            f"'{var.variableName}' in form '{form_key}'"
        )

    if (
        constraints.min_length is not None
        and constraints.max_length is not None
        and constraints.min_length > constraints.max_length
    ):
        errors.append(
            f"Inverted length bounds "
            f"(min_length={constraints.min_length} > max_length={constraints.max_length}) "
            f"on variable '{var.variableName}' in form '{form_key}'"
        )

    if (
        constraints.min_date is not None
        and constraints.max_date is not None
        and compare_temporal_strings(constraints.min_date, ">", constraints.max_date) is True
    ):
        errors.append(
            f"Inverted date bounds "
            f"(min_date='{constraints.min_date}' > max_date='{constraints.max_date}') "
            f"on variable '{var.variableName}' in form '{form_key}'"
        )


def _validate_business_logic_rule(
    rule: BusinessLogicRule,
    forms_by_key: dict[str, Form],
    vars_by_form: dict[str, set[str]],
    all_var_names: set[str],
    errors: list[str],
) -> None:
    """Validate syntax and Form/Variable references on a BusinessLogicRule."""
    rule_label = rule.id or rule.name or "<unnamed>"

    if rule.form is not None and (not rule.form.strip() or rule.form not in forms_by_key):
        errors.append(f"BusinessLogicRule '{rule_label}' references unknown form '{rule.form}'")

    if rule.logicRawXml is not None and rule.logicRawXml.strip():
        try:
            parse_logic_xml(rule.logicRawXml)
        except ValueError as exc:
            errors.append(f"Malformed logicRawXml on rule '{rule_label}': {exc}")

    if not isinstance(rule.logic, dict):
        return

    odm_element = rule.logic.get("odmElement")
    if odm_element in {"ConditionDef", "MethodDef"}:
        formal_exprs = rule.logic.get("formalExpressions")
        if isinstance(formal_exprs, list):
            for fe in formal_exprs:
                if isinstance(fe, dict):
                    expr_text = str(fe.get("expression") or "").strip()
                    ctx = fe.get("context")
                    if expr_text:
                        try:
                            compiled = compile_odm_formal_expression(expr_text, context=ctx)
                            for ref_form in compiled.referenced_forms:
                                if ref_form not in forms_by_key:
                                    errors.append(
                                        f"ODM {odm_element} '{rule_label}' references "
                                        f"unknown FormOID '{ref_form}'"
                                    )
                        except ValueError as exc:
                            errors.append(
                                f"Malformed FormalExpression on ODM {odm_element} "
                                f"'{rule_label}': {exc}"
                            )
        return

    if rule.logic.get("sourceFormat") == "redcap_branching":
        expr_raw = rule.logic.get("branchingExpression")
        if isinstance(expr_raw, str) and expr_raw.strip():
            try:
                rc_compiled = compile_redcap_branching_logic(expr_raw)
                if not rc_compiled.is_cross_scope:
                    ref_vars = list(rc_compiled.variable_map.values()) + [
                        cb_var for cb_var, _ in rc_compiled.checkbox_map.values()
                    ]
                    for ref_var in dict.fromkeys(ref_vars):
                        if ref_var not in all_var_names:
                            errors.append(
                                f"REDCap rule '{rule_label}' references unknown variable "
                                f"'{ref_var}'"
                            )
            except ValueError as exc:
                errors.append(f"Malformed REDCap branchingLogic on rule '{rule_label}': {exc}")
        return

    # Native XML/JSON condition tree reference validation
    leaf_refs: list[tuple[str | None, str | None, str | None]] = []
    _collect_logic_leaf_refs(rule.logic, leaf_refs)
    for form_ref, var_ref, cmp_ref in leaf_refs:
        target_form = form_ref or rule.form
        if form_ref is not None and form_ref not in forms_by_key:
            errors.append(f"BusinessLogicRule '{rule_label}' references unknown form '{form_ref}'")
            continue
        if target_form is not None and target_form in vars_by_form:
            form_vars = vars_by_form[target_form]
            if var_ref and var_ref not in form_vars:
                errors.append(
                    f"BusinessLogicRule '{rule_label}' references unknown variable "
                    f"'{var_ref}' on form '{target_form}'"
                )
            if cmp_ref and cmp_ref not in form_vars:
                errors.append(
                    f"BusinessLogicRule '{rule_label}' references unknown compareVariable "
                    f"'{cmp_ref}' on form '{target_form}'"
                )
        elif target_form is None:
            if var_ref and all_var_names and var_ref not in all_var_names:
                errors.append(
                    f"Unassigned BusinessLogicRule '{rule_label}' references unknown variable "
                    f"'{var_ref}'"
                )
            if cmp_ref and all_var_names and cmp_ref not in all_var_names:
                errors.append(
                    f"Unassigned BusinessLogicRule '{rule_label}' references unknown "
                    f"compareVariable '{cmp_ref}'"
                )


def _validate_semantics(dd: DataDictionary) -> list[str]:
    """Validate semantic invariants, constraints, types, and cross-references on DataDictionary."""
    from ..validation.cache import is_known_variable_type

    errors: list[str] = []
    is_api_source = (dd.metadata.source or "").strip().lower() == "api"
    is_redcap_source = (dd.metadata.source or "").strip().lower() == "redcap"

    forms_by_key: dict[str, Form] = {}
    vars_by_form: dict[str, set[str]] = {}
    all_var_names: set[str] = set()
    seen_form_ids: dict[str, str] = {}
    seen_redcap_vars: dict[str, str] = {}

    # Collect all rule IDs first for OID reference validation
    seen_rule_ids: set[str] = set()
    all_rules: list[BusinessLogicRule] = []
    for form in dd.forms:
        all_rules.extend(form.businessLogic)
    all_rules.extend(dd.unassignedBusinessLogic)

    for rule in all_rules:
        if rule.id is not None and str(rule.id).strip() != "":
            rid = str(rule.id).strip()
            if rid in seen_rule_ids:
                errors.append(f"Duplicate BusinessLogicRule id '{rid}' in DataDictionary")
            seen_rule_ids.add(rid)

    # 1. Form, Variable, and Choice checks
    for form in dd.forms:
        fkey = form.formKey
        if not fkey or not fkey.strip():
            errors.append("Form has an empty formKey")
        elif fkey in forms_by_key:
            errors.append(f"Duplicate formKey '{fkey}' in DataDictionary")
        else:
            forms_by_key[fkey] = form

        if form.formId is not None and str(form.formId).strip() != "":
            norm_fid = str(form.formId).strip()
            if norm_fid in seen_form_ids:
                errors.append(
                    f"Duplicate formId '{norm_fid}' shared by forms "
                    f"'{seen_form_ids[norm_fid]}' and '{fkey}'"
                )
            else:
                seen_form_ids[norm_fid] = fkey

        seen_vars: set[str] = vars_by_form.setdefault(fkey, set())
        for var in form.variables:
            vname = var.variableName
            if not vname or not vname.strip():
                errors.append(f"Variable has an empty variableName in form '{fkey}'")
            elif vname in seen_vars:
                errors.append(f"Duplicate variableName '{vname}' in form '{fkey}'")
            else:
                seen_vars.add(vname)
                all_var_names.add(vname)

            if is_redcap_source and vname and vname.strip():
                prev_form = seen_redcap_vars.get(vname)
                if prev_form is not None and prev_form != fkey:
                    errors.append(
                        f"Duplicate REDCap variableName '{vname}' across forms "
                        f"'{prev_form}' and '{fkey}'"
                    )
                else:
                    seen_redcap_vars[vname] = fkey

            # Variable type check (None is allowed on programmatic/minimal Variable models)
            if var.variableType is not None and (
                not var.variableType.strip() or not is_known_variable_type(var.variableType)
            ):
                errors.append(
                    f"Unknown variableType '{var.variableType}' on variable "
                    f"'{vname}' in form '{fkey}'"
                )

            norm_type = _normalize_type_label(var.variableType)

            # Choice checks: duplicate choice codes & empty categorical choice lists
            seen_choices: set[str] = set()
            for choice in var.choices:
                cval = choice.choiceValue
                if cval is None or str(cval) == "":
                    errors.append(f"Empty choiceValue on variable '{vname}' in form '{fkey}'")
                elif cval in seen_choices:
                    errors.append(
                        f"Duplicate choice code '{cval}' on variable '{vname}' in form '{fkey}'"
                    )
                else:
                    seen_choices.add(cval)

            is_sql_field = (var.format or "").strip().lower() == "sql" or str(
                (var.extraAttributes or {}).get("originalFieldType") or ""
            ).strip().lower() == "sql"
            if (
                norm_type in _CATEGORICAL_CHOICE_TYPES
                and len(var.choices) == 0
                and not is_api_source
                and not is_sql_field
            ):
                errors.append(
                    f"Categorical variable '{vname}' (type '{var.variableType}') in form "
                    f"'{fkey}' has an empty Choice list"
                )

            # Bounds and RangeCheck checks
            _validate_variable_bounds_and_ranges(var, fkey, norm_type, errors)

    # 2. Second pass over Variable extraAttributes references (now that all_var_names is complete)
    for form in dd.forms:
        fkey = form.formKey
        for var in form.variables:
            vname = var.variableName
            extra = var.extraAttributes or {}

            cond_oid = extra.get("collectionExceptionConditionOid")
            if (
                isinstance(cond_oid, str)
                and cond_oid.strip()
                and cond_oid.strip() not in seen_rule_ids
            ):
                errors.append(
                    f"Variable '{vname}' in form '{fkey}' references unknown "
                    f"CollectionExceptionConditionOID '{cond_oid.strip()}'"
                )

            method_oid = extra.get("methodOid")
            if (
                isinstance(method_oid, str)
                and method_oid.strip()
                and method_oid.strip() not in seen_rule_ids
            ):
                errors.append(
                    f"Variable '{vname}' in form '{fkey}' references unknown "
                    f"MethodOID '{method_oid.strip()}'"
                )

            branching_raw = extra.get("branchingLogic")
            if isinstance(branching_raw, str) and branching_raw.strip():
                try:
                    rc_info = compile_redcap_branching_logic(branching_raw)
                    if not rc_info.is_cross_scope:
                        ref_vars = list(rc_info.variable_map.values()) + [
                            cb_var for cb_var, _ in rc_info.checkbox_map.values()
                        ]
                        for ref_var in dict.fromkeys(ref_vars):
                            if ref_var not in all_var_names:
                                errors.append(
                                    f"Variable '{vname}' in form '{fkey}' has branchingLogic "
                                    f"referencing unknown variable '{ref_var}'"
                                )
                except ValueError as exc:
                    errors.append(
                        f"Malformed branchingLogic on variable '{vname}' in form '{fkey}': {exc}"
                    )

    # 3. Validate all BusinessLogicRules
    for rule in all_rules:
        _validate_business_logic_rule(rule, forms_by_key, vars_by_form, all_var_names, errors)

    return errors


def _load_target_path(
    p: Path, display_target: str | Path
) -> tuple[DataDictionary | None, Any, list[str]]:
    """Load an existing filesystem file or directory into a DataDictionary or raw JSON data."""
    if _safe_is_dir(p):
        from .loaders.csv_export import load_from_csv

        try:
            dd = load_from_csv(p, strict=False)
        except Exception as exc:
            return None, None, [f"Failed to load CSV directory '{display_target}': {exc}"]
        return dd, None, _promote_loader_warnings(dd.metadata.validationWarnings)

    suffix = p.suffix.lower()
    if suffix == ".zip":
        from .loaders.csv_export import load_from_csv

        try:
            dd = load_from_csv(p, strict=False)
        except Exception as exc:
            return None, None, [f"Failed to load CSV ZIP archive '{display_target}': {exc}"]
        return dd, None, _promote_loader_warnings(dd.metadata.validationWarnings)

    if suffix in {".xml", ".odm"}:
        from .loaders.cdisc_odm import load_from_cdisc_odm

        try:
            dd = load_from_cdisc_odm(p, strict=False)
        except Exception as exc:
            return None, None, [f"Failed to load CDISC ODM XML file '{display_target}': {exc}"]
        return dd, None, _promote_loader_warnings(dd.metadata.validationWarnings)

    if suffix == ".csv":
        from .loaders.redcap import load_from_redcap

        try:
            dd = load_from_redcap(p, strict=False)
        except Exception as exc:
            return None, None, [f"Failed to load REDCap CSV file '{display_target}': {exc}"]
        return dd, None, _promote_loader_warnings(dd.metadata.validationWarnings)

    try:
        raw_text = p.read_text(encoding="utf-8")
    except Exception as exc:
        return None, None, [f"Failed to read JSON file '{display_target}': {exc}"]

    if suffix != ".json" and raw_text.lstrip().startswith("<"):
        from .loaders.cdisc_odm import load_from_cdisc_odm

        try:
            dd = load_from_cdisc_odm(p, strict=False)
        except Exception as exc:
            return None, None, [f"Failed to load CDISC ODM XML file '{display_target}': {exc}"]
        return dd, None, _promote_loader_warnings(dd.metadata.validationWarnings)

    try:
        return None, json.loads(raw_text), []
    except Exception as exc:
        return None, None, [f"Failed to read JSON file '{display_target}': {exc}"]


def get_schema() -> dict[str, Any]:
    """Generate or retrieve the canonical JSON Schema for DataDictionary.

    Returns:
        JSON Schema dictionary.
    """
    schema = DataDictionary.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "iMednet Data Dictionary Schema"
    schema["version"] = "1.0.0"
    return schema


def validate(
    target: dict[str, Any] | str | Path | DataDictionary,
    schema: dict[str, Any] | None = None,
) -> list[str]:
    """Validate a data dictionary against the canonical model, semantic rules, and optional schema.

    Supports ``DataDictionary`` instances, dictionaries, raw JSON/ODM XML strings, JSON files,
    4-file CSV directories or ``.zip`` archives, CDISC ODM XML files, and REDCap CSV files.

    Args:
        target: DataDictionary instance, dict, JSON/ODM string, or path to supported input.
        schema: Optional JSON Schema to validate against via jsonschema.

    Returns:
        List of error strings (empty if valid).
    """
    errors: list[str] = []
    dd: DataDictionary | None = None
    data: Any = None

    # 1. Load target to DataDictionary or dict
    if isinstance(target, DataDictionary):
        dd = target
        if schema is not None:
            data = dd.model_dump(mode="json")
    elif isinstance(target, str | Path):
        p = Path(target)
        if _safe_is_dir(p):
            dd, data, load_errors = _load_target_path(p, target)
            if dd is None and data is None:
                return load_errors
            errors.extend(load_errors)
            if dd is not None and schema is not None:
                data = dd.model_dump(mode="json")
        elif _is_file_target(target) or _safe_is_file(p):
            if not _safe_is_file(p):
                return [f"FileNotFoundError: Target file '{target}' does not exist"]
            dd, data, load_errors = _load_target_path(p, target)
            if dd is None and data is None:
                return load_errors
            errors.extend(load_errors)
            if dd is not None and schema is not None:
                data = dd.model_dump(mode="json")
        else:
            raw_str = str(target)
            stripped = raw_str.lstrip()
            first_line = stripped.splitlines()[0].lower() if stripped else ""
            if stripped.startswith(("<?xml", "<ODM", "<odm:")):
                import io

                from .loaders.cdisc_odm import load_from_cdisc_odm

                try:
                    dd = load_from_cdisc_odm(io.StringIO(raw_str), strict=False)
                except Exception as exc:
                    return [f"Failed to parse CDISC ODM XML string: {exc}"]
                errors.extend(_promote_loader_warnings(dd.metadata.validationWarnings))
                if schema is not None:
                    data = dd.model_dump(mode="json")
            elif not stripped.startswith(("{", "[")) and (
                "variable / field name" in first_line
                or ("field_name" in first_line and "form_name" in first_line)
                or ("variable_name" in first_line and "form_name" in first_line)
            ):
                import io

                from .loaders.redcap import load_from_redcap

                try:
                    dd = load_from_redcap(io.StringIO(raw_str), strict=False)
                except Exception as exc:
                    return [f"Failed to parse REDCap CSV string: {exc}"]
                errors.extend(_promote_loader_warnings(dd.metadata.validationWarnings))
                if schema is not None:
                    data = dd.model_dump(mode="json")
            else:
                try:
                    data = json.loads(raw_str)
                except Exception as exc:
                    return [f"Failed to parse JSON string: {exc}"]
    elif isinstance(target, dict):
        data = target
    else:
        return [f"Unsupported target type for validation: {type(target)}"]

    # 2. If a custom JSON schema is provided, validate via jsonschema if available
    if schema is not None:
        try:
            import jsonschema

            validator = jsonschema.Draft202012Validator(schema)
            schema_errors: list[str] = []
            for err in validator.iter_errors(data):
                loc = ".".join(str(p) for p in err.absolute_path)
                prefix = f"[{loc}] " if loc else ""
                schema_errors.append(f"{prefix}{err.message}")
            if schema_errors:
                errors.extend(schema_errors)
                return errors
        except ImportError:
            errors.append(
                "jsonschema library is not installed; falling back to Pydantic model validation."
            )

    # 3. Model validation via Pydantic
    if dd is None:
        try:
            dd = DataDictionary.model_validate(data)
        except ValidationError as val_err:
            for err in val_err.errors():
                loc = ".".join(str(p) for p in err.get("loc", ()))
                msg = err.get("msg", "Validation error")
                errors.append(f"[{loc}] {msg}")
            return errors

    # 4. Semantic validation (duplicate keys/choices, bounds, types, XML, and references)
    errors.extend(_validate_semantics(dd))
    return errors
