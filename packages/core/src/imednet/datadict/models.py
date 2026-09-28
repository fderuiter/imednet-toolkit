"""Canonical Pydantic v2 data models for iMednet Data Dictionary."""

# ruff: noqa: N815

from __future__ import annotations

import contextlib
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .normalize import to_camel_case


class DictCompatMixin:
    """Provides dictionary-like interface for legacy callers."""

    def __getitem__(self, key: str) -> Any:
        """Allow dict-like indexing with header names or attribute names."""
        # 1. Exact attribute
        if hasattr(self, key):
            val = getattr(self, key)
            if not callable(val):
                return val

        # 2. camelCased attribute
        camel = to_camel_case(key)
        if hasattr(self, camel):
            val = getattr(self, camel)
            if not callable(val):
                return val

        # 3. extraAttributes
        extra: dict[str, Any] = getattr(self, "extraAttributes", {})
        if key in extra:
            return extra[key]
        if camel in extra:
            return extra[camel]

        # 4. Case-insensitive lookup in extraAttributes
        for k, v in extra.items():
            if k.lower() == key.lower():
                return v

        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        """Return value for key or default if absent."""
        try:
            val = self[key]
            return default if val is None else val
        except KeyError:
            return default

    def __contains__(self, key: str) -> bool:
        """Check if key exists in attributes or extraAttributes."""
        try:
            self[key]
            return True
        except KeyError:
            return False

    def keys(self) -> list[str]:
        """Return list of accessible keys."""
        res: list[str] = []
        if isinstance(self, BaseModel):
            res.extend(type(self).model_fields.keys())
        extra = getattr(self, "extraAttributes", {})
        res.extend(extra.keys())
        # Include dynamic backward-compatibility properties if present on object
        for prop in ("questions", "choices", "business_logic"):
            if hasattr(self, prop) and prop not in res:
                res.append(prop)
        return res

    def items(self) -> list[tuple[str, Any]]:
        """Return list of (key, value) pairs."""
        res: list[tuple[str, Any]] = []
        for k in self.keys():
            res.append((k, self.get(k)))
        return res


class Metadata(BaseModel):
    """Metadata envelope for the study data dictionary."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    schemaVersion: str = Field(default="1.0.0", description="Data dictionary schema version")
    studyKey: str | None = Field(default=None, description="Clinical trial study key")
    generatedAt: str | None = Field(
        default=None, description="Timestamp of data dictionary generation"
    )
    source: str | None = Field(default=None, description="Source of generation (csv, api)")
    validationWarnings: list[str] = Field(
        default_factory=list, description="Warnings generated during loading/validation"
    )
    extraAttributes: dict[str, Any] = Field(
        default_factory=dict, description="Unknown metadata columns or properties"
    )

    def __eq__(self, other: Any) -> bool:
        """Equality comparison excluding generatedAt for deterministic testing."""
        if not isinstance(other, Metadata):
            return False
        d1 = self.model_dump(exclude={"generatedAt"})
        d2 = other.model_dump(exclude={"generatedAt"})
        return d1 == d2

    def __hash__(self) -> int:
        """Hash method based on object identity."""
        return id(self)


class Choice(DictCompatMixin, BaseModel):
    """A choice option for single-select or multi-select variables."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    choiceText: str = Field(description="Display text of the choice")
    choiceValue: str = Field(description="Coded value of the choice")
    position: int | None = Field(default=None, description="Display order sequence")
    extraAttributes: dict[str, Any] = Field(
        default_factory=dict, description="Unknown columns for this choice"
    )


class Variable(DictCompatMixin, BaseModel):
    """A clinical data variable (data point) on a form."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    variableName: str = Field(description="Unique identifier for variable within form")
    label: str | None = Field(default=None, description="Display prompt/label on the eCRF")
    variableType: str | None = Field(
        default=None, description="Field type (Text, Radio, Date/Time Precision, etc.)"
    )
    sequence: int | None = Field(default=None, description="Display order within form")
    required: str | None = Field(
        default=None, description="Required policy (e.g. 'Yes', 'No', 'Auto Query')"
    )
    format: str | None = Field(default=None, description="Format mask or pattern")
    precision: str | None = Field(default=None, description="Date/Time or number precision")
    choices: list[Choice] = Field(
        default_factory=list, description="Allowed choices for categorical variables"
    )
    extraAttributes: dict[str, Any] = Field(
        default_factory=dict, description="Unknown columns for this variable"
    )


class BusinessLogicRule(DictCompatMixin, BaseModel):
    """An edit check, workflow trigger, or business logic rule."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: str | int | None = Field(default=None, description="Unique rule identifier")
    name: str | None = Field(default=None, description="Descriptive name of the rule")
    type: str | None = Field(
        default=None, description="Rule category: Form, Workflow, Query, Approval, Required"
    )
    status: str | None = Field(default=None, description="Rule status (Active, Inactive)")
    form: str | None = Field(
        default=None, description="Form key if rule is form-scoped or attached"
    )
    sequence: int | None = Field(default=None, description="Execution sequence")
    logic: dict[str, Any] | None = Field(
        default=None, description="Structured parsed AST of the business logic"
    )
    logicRawXml: str | None = Field(
        default=None, description="Verbatim XML string (optional, omitted with --no-raw-xml)"
    )
    extraAttributes: dict[str, Any] = Field(
        default_factory=dict, description="Additional rule properties or unknown columns"
    )


class Form(DictCompatMixin, BaseModel):
    """An electronic Case Report Form (eCRF)."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    formKey: str = Field(description="Unique short code/mnemonic for the form")
    formName: str | None = Field(default=None, description="Human-readable form title")
    formId: str | int | None = Field(default=None, description="Unique form identifier")
    formType: str | None = Field(
        default=None, description="Type of form (Patient, Study, Admin, etc.)"
    )
    variables: list[Variable] = Field(
        default_factory=list, description="Variables defined on this form"
    )
    businessLogic: list[BusinessLogicRule] = Field(
        default_factory=list, description="Form-scoped business logic rules"
    )
    extraAttributes: dict[str, Any] = Field(
        default_factory=dict, description="Unknown columns for this form"
    )


class DataDictionary(DictCompatMixin, BaseModel):
    """Canonical root data dictionary container."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    metadata: Metadata = Field(default_factory=Metadata)
    forms: list[Form] = Field(default_factory=list)
    unassignedBusinessLogic: list[BusinessLogicRule] = Field(
        default_factory=list,
        description="Business logic not tied to a single form (Workflow, Query, Approval, Required)",
    )

    @model_validator(mode="before")
    @classmethod
    def _handle_legacy_or_flexible_init(cls, data: Any) -> Any:
        """Support legacy initialization via business_logic, choices, forms, questions."""
        if not isinstance(data, dict):
            return data

        # If legacy keywords are provided
        has_legacy = any(k in data for k in ("business_logic", "questions", "choices")) or (
            "forms" in data
            and data["forms"]
            and isinstance(data["forms"][0], dict)
            and "formKey" not in data["forms"][0]
            and "Form Key" in data["forms"][0]
        )

        if not has_legacy:
            return data

        raw_forms = data.get("forms", [])
        raw_questions = data.get("questions", [])
        raw_choices = data.get("choices", [])
        raw_logic = data.get("business_logic", [])

        # Process forms
        forms_map: dict[str, Form] = {}
        for item in raw_forms:
            if isinstance(item, Form):
                forms_map[item.formKey] = item
            elif isinstance(item, dict):
                fk = item.get("formKey") or item.get("Form Key") or item.get("Form", "")
                fn = item.get("formName") or item.get("Form Name")
                fid = item.get("formId") or item.get("Form ID")
                ft = item.get("formType") or item.get("Form Type")
                extra = {
                    to_camel_case(k): v
                    for k, v in item.items()
                    if k
                    not in {
                        "formKey",
                        "Form Key",
                        "Form",
                        "formName",
                        "Form Name",
                        "formId",
                        "Form ID",
                        "formType",
                        "Form Type",
                        "variables",
                        "businessLogic",
                    }
                }
                forms_map[fk] = Form(
                    formKey=fk, formName=fn, formId=fid, formType=ft, extraAttributes=extra
                )

        # Process variables (questions)
        var_map: dict[tuple[str, str], Variable] = {}
        for item in raw_questions:
            if isinstance(item, Variable):
                continue
            if isinstance(item, dict):
                fk = item.get("Form") or item.get("Form Key") or item.get("formKey") or ""
                vname = item.get("Variable Name") or item.get("variableName") or ""
                if not fk and not vname:
                    continue
                vtype = item.get("Variable Type") or item.get("variableType")
                lbl = item.get("Label") or item.get("label")
                req = item.get("Required Field") or item.get("Required") or item.get("required")
                seq_val = item.get("Sequence") or item.get("sequence")
                seq = int(seq_val) if seq_val is not None and str(seq_val).isdigit() else None
                fmt = item.get("Format") or item.get("format")
                prec = item.get("Precision") or item.get("precision")
                extra = {
                    to_camel_case(k): v
                    for k, v in item.items()
                    if k
                    not in {
                        "Form",
                        "Form Key",
                        "formKey",
                        "Variable Name",
                        "variableName",
                        "Variable Type",
                        "variableType",
                        "Label",
                        "label",
                        "Required Field",
                        "Required",
                        "required",
                        "Sequence",
                        "sequence",
                        "Format",
                        "format",
                        "Precision",
                        "precision",
                        "choices",
                    }
                }
                var = Variable(
                    variableName=vname,
                    label=lbl,
                    variableType=vtype,
                    sequence=seq,
                    required=req,
                    format=fmt,
                    precision=prec,
                    extraAttributes=extra,
                )
                if fk not in forms_map:
                    forms_map[fk] = Form(formKey=fk)
                forms_map[fk].variables.append(var)
                var_map[(fk, vname)] = var

        # Process choices
        for item in raw_choices:
            if isinstance(item, Choice):
                continue
            if isinstance(item, dict):
                fk = item.get("Form") or item.get("Form Key") or item.get("formKey") or ""
                vname = item.get("Variable Name") or item.get("variableName") or ""
                ctext = str(item.get("Choice Text") or item.get("choiceText") or "")
                cval = str(item.get("Choice Value") or item.get("choiceValue") or "")
                pos_val = item.get("Position") or item.get("position")
                pos = int(pos_val) if pos_val is not None and str(pos_val).isdigit() else None
                extra = {
                    to_camel_case(k): v
                    for k, v in item.items()
                    if k
                    not in {
                        "Form",
                        "Form Key",
                        "formKey",
                        "Variable Name",
                        "variableName",
                        "Choice Text",
                        "choiceText",
                        "Choice Value",
                        "choiceValue",
                        "Position",
                        "position",
                    }
                }
                choice = Choice(
                    choiceText=ctext, choiceValue=cval, position=pos, extraAttributes=extra
                )
                if (fk, vname) in var_map:
                    var_map[(fk, vname)].choices.append(choice)
                else:
                    placeholder_var = Variable(
                        variableName=vname,
                        variableType=item.get("Variable Type") or item.get("variableType"),
                        choices=[choice],
                    )
                    if fk not in forms_map:
                        forms_map[fk] = Form(formKey=fk)
                    forms_map[fk].variables.append(placeholder_var)
                    var_map[(fk, vname)] = placeholder_var

        # Process business logic
        unassigned: list[BusinessLogicRule] = []
        for item in raw_logic:
            if isinstance(item, BusinessLogicRule):
                if (item.type or "").strip().lower() == "form" and item.form in forms_map:
                    forms_map[item.form].businessLogic.append(item)
                else:
                    unassigned.append(item)
            elif isinstance(item, dict):
                rtype = item.get("Type") or item.get("type")
                rname = item.get("Name") or item.get("name")
                rstat = item.get("Status") or item.get("status")
                rid = item.get("ID") or item.get("id")
                rform = item.get("Form") or item.get("form")
                rseq_val = item.get("Sequence") or item.get("sequence")
                rseq = int(rseq_val) if rseq_val is not None and str(rseq_val).isdigit() else None

                raw_logic_val = (
                    item.get("Logic")
                    or item.get("logic")
                    or item.get("LogicRawXml")
                    or item.get("logicRawXml")
                    or item.get("ruleXml")
                    or item.get("xml")
                )
                logic_ast: dict[str, Any] | None = None
                xml_content: str | None = None
                if isinstance(raw_logic_val, dict):
                    logic_ast = raw_logic_val
                    raw_xml = item.get("LogicRawXml") or item.get("logicRawXml")
                    if isinstance(raw_xml, str):
                        xml_content = raw_xml
                elif isinstance(raw_logic_val, str) and raw_logic_val.strip():
                    xml_content = raw_logic_val
                    with contextlib.suppress(Exception):
                        from .logic import parse_logic_xml

                        logic_ast = parse_logic_xml(raw_logic_val)

                extra = {
                    to_camel_case(k): v
                    for k, v in item.items()
                    if k
                    not in {
                        "Type",
                        "type",
                        "Name",
                        "name",
                        "Status",
                        "status",
                        "ID",
                        "id",
                        "Form",
                        "form",
                        "Sequence",
                        "sequence",
                        "Logic",
                        "logic",
                        "LogicRawXml",
                        "logicRawXml",
                        "ruleXml",
                        "rulexml",
                        "xml",
                        "XML",
                        "businessLogic",
                    }
                }
                rule = BusinessLogicRule(
                    id=rid,
                    name=rname,
                    type=rtype,
                    status=rstat,
                    form=rform,
                    sequence=rseq,
                    logic=logic_ast,
                    logicRawXml=xml_content,
                    extraAttributes=extra,
                )
                if (rtype or "").strip().lower() == "form" and rform in forms_map:
                    forms_map[rform].businessLogic.append(rule)
                else:
                    unassigned.append(rule)

        res_data: dict[str, Any] = {
            "metadata": data.get("metadata", Metadata()),
            "forms": list(forms_map.values()),
            "unassignedBusinessLogic": unassigned,
        }
        return res_data

    @property
    def questions(self) -> list[Variable]:
        """Backward-compatible access to all variables as questions."""
        return [v for f in self.forms for v in f.variables]

    @property
    def choices(self) -> list[Choice]:
        """Backward-compatible access to all choices."""
        return [c for f in self.forms for v in f.variables for c in v.choices]

    @property
    def business_logic(self) -> list[Any]:
        """Backward-compatible access to all business logic rules."""
        res: list[Any] = []
        for f in self.forms:
            res.extend(f.businessLogic)
        res.extend(self.unassignedBusinessLogic)
        return res

    def __eq__(self, other: Any) -> bool:
        """Compare data dictionaries ignoring generatedAt timestamp."""
        if not isinstance(other, DataDictionary):
            return False
        return (
            self.metadata == other.metadata
            and self.forms == other.forms
            and self.unassignedBusinessLogic == other.unassignedBusinessLogic
        )

    def __hash__(self) -> int:
        """Hash method based on object identity."""
        return id(self)
