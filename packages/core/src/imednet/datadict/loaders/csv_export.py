"""CSV export loader for 4-file iMednet Data Dictionary packages."""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, TextIO

from ..logic import parse_logic_xml
from ..models import (
    BusinessLogicRule,
    Choice,
    DataDictionary,
    Form,
    Metadata,
    Variable,
)
from ..normalize import normalize_row_data, to_camel_case, validate_timestamp

KNOWN_FORM_FIELDS = {"formId", "formKey", "formType", "formName"}
KNOWN_QUESTION_FIELDS = {
    "form",
    "formKey",
    "variableType",
    "variableName",
    "label",
    "required",
    "requiredField",
    "sequence",
    "precision",
    "format",
}
KNOWN_CHOICE_FIELDS = {
    "form",
    "formKey",
    "variableType",
    "variableName",
    "choiceText",
    "choiceValue",
    "position",
}
KNOWN_RULE_FIELDS = {
    "type",
    "name",
    "status",
    "id",
    "form",
    "formKey",
    "sequence",
    "logic",
    "logicXml",
    "logicRawXml",
    "businessLogic",
    "ruleXml",
}

LOGIC_XML_HEADERS = {
    "logic",
    "logicxml",
    "logicrawxml",
    "businesslogic",
    "rulexml",
    "xml",
}


@contextmanager
def _open_text(source: Path | str | TextIO) -> Iterator[TextIO]:
    """Provide a text stream for a Path or existing TextIO stream."""
    if isinstance(source, str | Path):
        with open(source, encoding="utf-8-sig", newline="") as f:
            yield f
    else:
        yield source


def _read_csv(source: Path | str | TextIO) -> list[dict[str, str]]:
    """Read a CSV source into list of string dictionaries."""
    with _open_text(source) as f:
        reader = csv.DictReader(f)
        return list(reader)


def load_from_csv_files(
    *,
    forms_file: Path | str | TextIO,
    questions_file: Path | str | TextIO,
    choices_file: Path | str | TextIO,
    business_logic_file: Path | str | TextIO,
    strict: bool = False,
    no_raw_xml: bool = False,
    study_key: str | None = None,
) -> DataDictionary:
    """Load canonical DataDictionary from 4 individual CSV files or streams.

    Args:
        forms_file: Path or stream for FORMS.csv.
        questions_file: Path or stream for QUESTIONS.csv.
        choices_file: Path or stream for CHOICES.csv.
        business_logic_file: Path or stream for BUSINESS_LOGIC.csv.
        strict: If True, raise on missing references or validation warnings.
        no_raw_xml: If True, omit logicRawXml from BusinessLogicRule instances.
        study_key: Optional study identifier.

    Returns:
        Canonical DataDictionary instance.
    """
    raw_forms = _read_csv(forms_file)
    raw_questions = _read_csv(questions_file)
    raw_choices = _read_csv(choices_file)
    raw_rules = _read_csv(business_logic_file)

    warnings: list[str] = []
    warning_set: set[str] = set()

    def record_warning(msg: str) -> None:
        if msg not in warning_set:
            warning_set.add(msg)
            warnings.append(msg)
        if strict:
            raise ValueError(f"Strict validation error: {msg}")

    # 1. Parse Forms
    forms_by_key: dict[str, Form] = {}
    for row in raw_forms:
        known, extra, row_warns = normalize_row_data(row, KNOWN_FORM_FIELDS, "FORMS.csv")
        for w in row_warns:
            record_warning(w)

        fk = known.get("formKey") or known.get("form") or ""
        if not fk:
            record_warning(f"Form row without formKey: {row}")
            continue

        form = Form(
            formKey=fk,
            formName=known.get("formName"),
            formId=known.get("formId"),
            formType=known.get("formType"),
            extraAttributes=extra,
        )
        forms_by_key[fk] = form

    # 2. Parse Variables (QUESTIONS)
    vars_by_key: dict[tuple[str, str], Variable] = {}
    for idx, row in enumerate(raw_questions):
        known, extra, row_warns = normalize_row_data(row, KNOWN_QUESTION_FIELDS, "QUESTIONS.csv")
        for w in row_warns:
            record_warning(w)

        fk = known.get("form") or known.get("formKey") or ""
        vname = known.get("variableName") or ""
        if not fk or not vname:
            record_warning(f"Question missing form or variable name: {row}")
            continue

        if fk not in forms_by_key:
            record_warning(f"Question '{vname}' references undefined form '{fk}'")
            # Create form placeholder in non-strict mode
            forms_by_key[fk] = Form(formKey=fk)

        req_val = known.get("required") or known.get("requiredField")
        seq_val = known.get("sequence")
        sequence = int(seq_val) if seq_val and str(seq_val).isdigit() else (idx + 1)

        variable = Variable(
            variableName=vname,
            label=known.get("label"),
            variableType=known.get("variableType"),
            sequence=sequence,
            required=req_val,
            format=known.get("format"),
            precision=known.get("precision"),
            extraAttributes=extra,
        )
        forms_by_key[fk].variables.append(variable)
        vars_by_key[(fk, vname)] = variable

    # 3. Parse Choices
    for row in raw_choices:
        known, extra, row_warns = normalize_row_data(row, KNOWN_CHOICE_FIELDS, "CHOICES.csv")
        for w in row_warns:
            record_warning(w)

        fk = known.get("form") or known.get("formKey") or ""
        vname = known.get("variableName") or ""
        ctext = known.get("choiceText") or ""
        cval = known.get("choiceValue") or ""
        pos_val = known.get("position")
        pos = int(pos_val) if pos_val and str(pos_val).isdigit() else None

        choice = Choice(
            choiceText=ctext,
            choiceValue=cval,
            position=pos,
            extraAttributes=extra,
        )

        var = vars_by_key.get((fk, vname))
        if var is not None:
            var.choices.append(choice)
        else:
            record_warning(
                f"Choice '{cval}' references undefined variable '{vname}' on form '{fk}'"
            )

    # 4. Parse Business Logic
    unassigned_logic: list[BusinessLogicRule] = []
    for idx, row in enumerate(raw_rules):
        known, extra, row_warns = normalize_row_data(row, KNOWN_RULE_FIELDS, "BUSINESS_LOGIC.csv")
        for w in row_warns:
            record_warning(w)

        # Detect raw XML from known XML headers or XML-shaped cell
        xml_content: str | None = None
        for raw_k, raw_v in row.items():
            if not raw_v:
                continue
            canonical_col = to_camel_case(raw_k).lower()
            if canonical_col in LOGIC_XML_HEADERS:
                xml_content = raw_v
                break
            if raw_v.strip().startswith("<") and raw_v.strip().endswith(">"):
                xml_content = raw_v
                break

        parsed_ast = None
        if xml_content:
            try:
                parsed_ast = parse_logic_xml(xml_content)
            except ValueError as exc:
                record_warning(f"Malformed XML in business logic row {idx + 1}: {exc}")

        rule_type = known.get("type")
        rule_name = known.get("name")
        rule_stat = known.get("status")
        rule_id = known.get("id")
        rule_form = known.get("form") or known.get("formKey")
        seq_val = known.get("sequence")
        sequence = int(seq_val) if seq_val and str(seq_val).isdigit() else (idx + 1)

        rule = BusinessLogicRule(
            id=rule_id,
            name=rule_name,
            type=rule_type,
            status=rule_stat,
            form=rule_form,
            sequence=sequence,
            logic=parsed_ast,
            logicRawXml=None if no_raw_xml else xml_content,
            extraAttributes=extra,
        )

        # 5 rule types: Form (attached to form), Workflow, Query, Approval, Required (routed to unassigned)
        is_form_rule = (
            rule_type is not None
            and rule_type.strip().lower() == "form"
            and rule_form is not None
            and rule_form in forms_by_key
        )

        if is_form_rule and rule_form:
            forms_by_key[rule_form].businessLogic.append(rule)
        else:
            unassigned_logic.append(rule)

    # Validate timestamps across all entities if date/time fields exist
    all_extras: list[tuple[str, dict[str, Any]]] = []
    for fk, form in forms_by_key.items():
        all_extras.append((f"Form '{fk}'", form.extraAttributes))
        for var in form.variables:
            all_extras.append(
                (f"Variable '{var.variableName}' on form '{fk}'", var.extraAttributes)
            )
            for choice in var.choices:
                all_extras.append(
                    (
                        f"Choice '{choice.choiceValue}' for variable '{var.variableName}'",
                        choice.extraAttributes,
                    )
                )
        for rule in form.businessLogic:
            all_extras.append(
                (f"Rule '{rule.name or rule.id}' on form '{fk}'", rule.extraAttributes)
            )
    for rule in unassigned_logic:
        all_extras.append((f"Unassigned rule '{rule.name or rule.id}'", rule.extraAttributes))

    for entity_name, extra in all_extras:
        for k, v in extra.items():
            if isinstance(v, str) and v.strip() and ("date" in k.lower() or "time" in k.lower()):
                try:
                    validate_timestamp(v)
                except ValueError as exc:
                    record_warning(f"{entity_name} attribute '{k}': {exc}")

    now_timestamp = datetime.now(timezone.utc).strftime("%m-%d-%Y %H:%M:%S")
    metadata = Metadata(
        schemaVersion="1.0.0",
        studyKey=study_key,
        generatedAt=now_timestamp,
        source="csv",
        validationWarnings=warnings,
    )

    return DataDictionary(
        metadata=metadata,
        forms=list(forms_by_key.values()),
        unassignedBusinessLogic=unassigned_logic,
    )


def _find_case_insensitive_file(names: list[str], target: str) -> str | None:
    """Find a filename case-insensitively in a list of names."""
    target_lower = target.lower()
    for name in names:
        if name.lower() == target_lower or Path(name).name.lower() == target_lower:
            return name
    return None


def load_from_csv(
    source: Path | str | BinaryIO,
    *,
    strict: bool = False,
    no_raw_xml: bool = False,
    study_key: str | None = None,
) -> DataDictionary:
    """Load canonical DataDictionary from a directory or ZIP file.

    Args:
        source: Directory path or ZIP archive path / file-like buffer.
        strict: If True, raise on validation errors.
        no_raw_xml: If True, omit logicRawXml.
        study_key: Optional study identifier.

    Returns:
        Loaded DataDictionary.
    """
    # Check if source is a ZIP file (path ending in .zip or binary stream)
    is_zip = False
    if isinstance(source, str | Path):
        p = Path(source)
        if p.is_file() and (p.suffix.lower() == ".zip" or zipfile.is_zipfile(p)):
            is_zip = True
        elif p.is_dir():
            is_zip = False
        elif zipfile.is_zipfile(p):
            is_zip = True
        else:
            raise FileNotFoundError(f"Source path '{source}' is neither a directory nor a zip file")
    else:
        is_zip = True

    if is_zip:
        with zipfile.ZipFile(source) as zf:
            namelist = zf.namelist()
            forms_name = _find_case_insensitive_file(namelist, "FORMS.csv")
            questions_name = _find_case_insensitive_file(namelist, "QUESTIONS.csv")
            choices_name = _find_case_insensitive_file(namelist, "CHOICES.csv")
            logic_name = _find_case_insensitive_file(namelist, "BUSINESS_LOGIC.csv")

            for required, found in [
                ("FORMS.csv", forms_name),
                ("QUESTIONS.csv", questions_name),
                ("CHOICES.csv", choices_name),
                ("BUSINESS_LOGIC.csv", logic_name),
            ]:
                if not found:
                    raise FileNotFoundError(f"Required file '{required}' not found in ZIP archive")

            if not (forms_name and questions_name and choices_name and logic_name):
                raise FileNotFoundError("Missing required files in ZIP archive")

            with (
                zf.open(forms_name) as f_fh,
                zf.open(questions_name) as q_fh,
                zf.open(choices_name) as c_fh,
                zf.open(logic_name) as l_fh,
                io.TextIOWrapper(f_fh, encoding="utf-8-sig", newline="") as f_text,
                io.TextIOWrapper(q_fh, encoding="utf-8-sig", newline="") as q_text,
                io.TextIOWrapper(c_fh, encoding="utf-8-sig", newline="") as c_text,
                io.TextIOWrapper(l_fh, encoding="utf-8-sig", newline="") as l_text,
            ):
                return load_from_csv_files(
                    forms_file=f_text,
                    questions_file=q_text,
                    choices_file=c_text,
                    business_logic_file=l_text,
                    strict=strict,
                    no_raw_xml=no_raw_xml,
                    study_key=study_key,
                )
    else:
        if not isinstance(source, str | Path):
            raise TypeError("Directory source must be a string or Path")
        dir_path = Path(source)
        dir_files = [f.name for f in dir_path.iterdir() if f.is_file()]

        forms_name = _find_case_insensitive_file(dir_files, "FORMS.csv")
        questions_name = _find_case_insensitive_file(dir_files, "QUESTIONS.csv")
        choices_name = _find_case_insensitive_file(dir_files, "CHOICES.csv")
        logic_name = _find_case_insensitive_file(dir_files, "BUSINESS_LOGIC.csv")

        for required, found in [
            ("FORMS.csv", forms_name),
            ("QUESTIONS.csv", questions_name),
            ("CHOICES.csv", choices_name),
            ("BUSINESS_LOGIC.csv", logic_name),
        ]:
            if not found:
                raise FileNotFoundError(
                    f"Required file '{required}' not found in directory '{source}'"
                )

        if not (forms_name and questions_name and choices_name and logic_name):
            raise FileNotFoundError(f"Missing required CSV files in directory '{source}'")

        return load_from_csv_files(
            forms_file=dir_path / forms_name,
            questions_file=dir_path / questions_name,
            choices_file=dir_path / choices_name,
            business_logic_file=dir_path / logic_name,
            strict=strict,
            no_raw_xml=no_raw_xml,
            study_key=study_key,
        )
