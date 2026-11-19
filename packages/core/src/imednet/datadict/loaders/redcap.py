"""REDCap data dictionary CSV loader for iMednet."""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any, BinaryIO, TextIO

from ..models import (
    Choice,
    DataDictionary,
    Form,
    Metadata,
    Variable,
)
from ..normalize import (
    _is_file_target,
    _safe_is_file,
    normalize_form_type,
    normalize_variable_type,
    to_camel_case,
)

# Canonical internal keys mapped from various common REDCap header formats
COLUMN_ALIASES: dict[str, str] = {
    "variable / field name": "variableName",
    "field name": "variableName",
    "variable name": "variableName",
    "variablename": "variableName",
    "fieldname": "variableName",
    "variable": "variableName",
    "field_name": "variableName",
    "form name": "formName",
    "form": "formName",
    "formname": "formName",
    "form_name": "formName",
    "instrument name": "formName",
    "instrument": "formName",
    "section header": "sectionHeader",
    "section_header": "sectionHeader",
    "section": "sectionHeader",
    "field type": "fieldType",
    "fieldtype": "fieldType",
    "field_type": "fieldType",
    "type": "fieldType",
    "field label": "fieldLabel",
    "fieldlabel": "fieldLabel",
    "field_label": "fieldLabel",
    "label": "fieldLabel",
    "description": "fieldLabel",
    "choices, calculations, or slider labels": "choices",
    "select choices or calculations": "choices",
    "select_choices_or_calculations": "choices",
    "choices": "choices",
    "choices / calculations": "choices",
    "field note": "fieldNote",
    "fieldnote": "fieldNote",
    "field_note": "fieldNote",
    "note": "fieldNote",
    "text validation type or show slider number": "textValidationType",
    "text_validation_type_or_show_slider_number": "textValidationType",
    "text validation type": "textValidationType",
    "text_validation_type": "textValidationType",
    "validation type": "textValidationType",
    "text validation min": "textValidationMin",
    "text_validation_min": "textValidationMin",
    "min": "textValidationMin",
    "text validation max": "textValidationMax",
    "text_validation_max": "textValidationMax",
    "max": "textValidationMax",
    "identifier?": "identifier",
    "identifier": "identifier",
    "is_identifier": "identifier",
    "branching logic (show field only if...)": "branchingLogic",
    "branching logic": "branchingLogic",
    "branching_logic": "branchingLogic",
    "required field?": "requiredField",
    "required field": "requiredField",
    "required": "requiredField",
    "required_field": "requiredField",
    "required?": "requiredField",
    "custom alignment": "customAlignment",
    "custom_alignment": "customAlignment",
    "question number (surveys only)": "questionNumber",
    "question number": "questionNumber",
    "question_number": "questionNumber",
    "matrix group name": "matrixGroupName",
    "matrix_group_name": "matrixGroupName",
    "matrix ranking?": "matrixRanking",
    "matrix ranking": "matrixRanking",
    "matrix_ranking": "matrixRanking",
    "field annotation": "fieldAnnotation",
    "field_annotation": "fieldAnnotation",
    "action tags": "fieldAnnotation",
    "action tags / field annotation": "fieldAnnotation",
}

KNOWN_CANONICAL_FIELDS = {
    "variableName",
    "formName",
    "sectionHeader",
    "fieldType",
    "fieldLabel",
    "choices",
    "fieldNote",
    "textValidationType",
    "textValidationMin",
    "textValidationMax",
    "identifier",
    "branchingLogic",
    "requiredField",
    "customAlignment",
    "questionNumber",
    "matrixGroupName",
    "matrixRanking",
    "fieldAnnotation",
}

SUPPORTED_FIELD_TYPES = {
    "text",
    "notes",
    "calc",
    "dropdown",
    "radio",
    "checkbox",
    "yesno",
    "truefalse",
    "file",
    "slider",
    "descriptive",
    "sql",
}


def _read_source_text(source: Path | str | TextIO | BinaryIO) -> str:
    """Read CSV content from path, string buffer, or stream into str."""
    if isinstance(source, Path):
        if not _safe_is_file(source):
            raise FileNotFoundError(f"Target file '{source}' does not exist")
        return source.read_text(encoding="utf-8-sig")
    if isinstance(source, str):
        if "\n" in source or "\r" in source:
            return source.lstrip("\ufeff")
        if _is_file_target(source):
            p = Path(source)
            if not _safe_is_file(p):
                raise FileNotFoundError(f"Target file '{source}' does not exist")
            return p.read_text(encoding="utf-8-sig")
        p = Path(source)
        if _safe_is_file(p):
            return p.read_text(encoding="utf-8-sig")
        return source.lstrip("\ufeff")
    if hasattr(source, "read"):
        data = source.read()
        if isinstance(data, bytes):
            return data.decode("utf-8-sig")
        return data.lstrip("\ufeff")
    raise TypeError(f"Unsupported source type for REDCap loader: {type(source)}")


def _map_field_type(
    field_type: str | None,
    validation_type: str | None,
    var_name: str,
    record_warning: Any,
) -> str:
    """Map REDCap field_type and validation_type to canonical variableType."""
    ft = (field_type or "").strip().lower()
    vt = (validation_type or "").strip().lower()

    if not ft:
        return "Text"

    if ft not in SUPPORTED_FIELD_TYPES:
        record_warning(
            f"Unsupported field type '{field_type}' on variable '{var_name}'; defaulted to 'Text'."
        )
        return "Text"

    if ft == "text":
        if "datetime" in vt:
            return "DateTime"
        if "date" in vt:
            return "Date"
        if "time" in vt:
            return "Time"
        if vt in ("int", "integer"):
            return "Integer"
        if vt in ("number", "float", "decimal"):
            return "Float"
        return "Text"

    return normalize_variable_type(ft) or "Text"


def _parse_redcap_choices(raw_choices: str | None, field_type: str | None) -> list[Choice]:
    """Parse pipe-separated REDCap choices or synthesize defaults for yesno/truefalse."""
    ft = (field_type or "").strip().lower()
    if ft == "calc":
        return []

    choices: list[Choice] = []
    if raw_choices and raw_choices.strip():
        parts = [p.strip() for p in raw_choices.split("|") if p.strip()]
        for idx, part in enumerate(parts):
            if "," in part:
                code, label = part.split(",", 1)
                c_val = code.strip()
                c_text = label.strip()
            else:
                c_val = part.strip()
                c_text = part.strip()
            choices.append(
                Choice(
                    choiceValue=c_val,
                    choiceText=c_text,
                    position=idx + 1,
                )
            )

    # Automatic choices for yesno / truefalse if none provided
    if not choices:
        if ft == "yesno":
            choices = [
                Choice(choiceValue="1", choiceText="Yes", position=1),
                Choice(choiceValue="0", choiceText="No", position=2),
            ]
        elif ft == "truefalse":
            choices = [
                Choice(choiceValue="1", choiceText="True", position=1),
                Choice(choiceValue="0", choiceText="False", position=2),
            ]

    return choices


def load_from_redcap(
    source: Path | str | TextIO | BinaryIO,
    *,
    study_key: str | None = None,
    strict: bool = False,
    no_raw_xml: bool = False,
) -> DataDictionary:
    """Parse a REDCap CSV data dictionary into a canonical DataDictionary.

    Args:
        source: CSV file path, string buffer, or file-like stream.
        study_key: Optional study key identifier.
        strict: If True, raise ValueError on unmapped attributes or format warnings.
        no_raw_xml: Ignored (reserved for compatibility).

    Returns:
        Canonical DataDictionary representation.

    Raises:
        ValueError: If strict is True and validation warnings are encountered, or CSV is invalid.
    """
    warnings: list[str] = []

    def record_warning(msg: str) -> None:
        warnings.append(msg)
        if strict:
            raise ValueError(f"Strict loading error: {msg}")

    # 1. Read CSV text and parse rows
    text = _read_source_text(source)
    if not text.strip():
        record_warning("REDCap CSV data dictionary is empty.")
        metadata = Metadata.create(
            study_key=study_key,
            source="redcap",
            validation_warnings=warnings,
        )
        return DataDictionary(metadata=metadata, forms=[])

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        record_warning("REDCap CSV data dictionary has no header columns.")
        metadata = Metadata.create(
            study_key=study_key,
            source="redcap",
            validation_warnings=warnings,
        )
        return DataDictionary(metadata=metadata, forms=[])

    # Map column headers to canonical names
    header_map: dict[str, str] = {}
    for raw_col in reader.fieldnames:
        if not raw_col:
            continue
        cleaned_col = raw_col.strip().lower()
        if cleaned_col in COLUMN_ALIASES:
            header_map[raw_col] = COLUMN_ALIASES[cleaned_col]
        else:
            header_map[raw_col] = to_camel_case(raw_col.strip())

    # 2. Iterate rows and group variables by form
    forms_order: list[str] = []
    forms_vars: dict[str, list[Variable]] = {}

    for row_idx, raw_row in enumerate(reader, start=1):
        if not raw_row or not any(v and v.strip() for v in raw_row.values()):
            continue

        known: dict[str, str] = {}
        extra: dict[str, Any] = {}

        for k, v in raw_row.items():
            if not k:
                continue
            canonical_key = header_map.get(k, to_camel_case(k.strip()))
            val_clean = (v or "").strip()
            if canonical_key in KNOWN_CANONICAL_FIELDS:
                known[canonical_key] = val_clean
            else:
                if val_clean:
                    extra[canonical_key] = val_clean
                record_warning(
                    f"Unmapped attribute/column '{k.strip()}' on variable '{known.get('variableName', f'row_{row_idx}')}'."
                )

        var_name = known.get("variableName")
        if not var_name:
            record_warning(f"Row {row_idx} is missing variable/field name; skipping.")
            continue

        form_key = known.get("formName")
        if not form_key:
            record_warning(
                f"Variable '{var_name}' at row {row_idx} has no form name; assigned to 'default_form'."
            )
            form_key = "default_form"

        if form_key not in forms_vars:
            forms_order.append(form_key)
            forms_vars[form_key] = []

        field_type = known.get("fieldType")
        val_type = known.get("textValidationType")
        var_type = _map_field_type(field_type, val_type, var_name, record_warning)

        # Store calculation equation, bounds, annotations, and metadata in extraAttributes
        raw_choices = known.get("choices")
        if (field_type or "").lower() == "calc" and raw_choices:
            extra["calculation"] = raw_choices

        for extra_key in (
            "branchingLogic",
            "fieldNote",
            "sectionHeader",
            "identifier",
            "textValidationMin",
            "textValidationMax",
            "fieldAnnotation",
            "customAlignment",
            "questionNumber",
            "matrixGroupName",
            "matrixRanking",
        ):
            extra_val = known.get(extra_key)
            if extra_val:
                extra[extra_key] = extra_val

        min_bound = known.get("textValidationMin")
        if min_bound:
            extra["textValidationMin"] = min_bound

        max_bound = known.get("textValidationMax")
        if max_bound:
            extra["textValidationMax"] = max_bound

        choices = _parse_redcap_choices(raw_choices, field_type)

        req_raw = (known.get("requiredField") or "").lower()
        req_val = "Yes" if req_raw in ("y", "yes", "1", "true") else ("No" if req_raw else None)

        seq = len(forms_vars[form_key]) + 1

        var = Variable(
            variableName=var_name,
            label=known.get("fieldLabel") or var_name,
            variableType=var_type,
            sequence=seq,
            required=req_val,
            format=val_type or None,
            precision=None,
            choices=choices,
            extraAttributes=extra,
        )
        forms_vars[form_key].append(var)

    # 3. Construct Form models
    forms: list[Form] = []
    for f_key in forms_order:
        f_name = f_key.replace("_", " ").title()
        form = Form(
            formKey=f_key,
            formName=f_name,
            formId=None,
            formType=normalize_form_type(None, default="Subject"),
            variables=forms_vars[f_key],
        )
        forms.append(form)

    # 4. Construct Metadata and DataDictionary
    metadata = Metadata.create(
        study_key=study_key,
        source="redcap",
        validation_warnings=warnings,
    )

    return DataDictionary(
        metadata=metadata,
        forms=forms,
    )
