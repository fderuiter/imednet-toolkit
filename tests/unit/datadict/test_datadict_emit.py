"""Tests for deterministic JSON emission."""

from __future__ import annotations

import json
from pathlib import Path

from imednet.datadict.emit import sort_data_dictionary, to_file, to_json
from imednet.datadict.models import (
    BusinessLogicRule,
    Choice,
    DataDictionary,
    Form,
    Metadata,
    Variable,
)


def test_sort_data_dictionary_deterministic() -> None:
    """Test deterministic ordering of forms, variables, choices, and rules."""
    form_z = Form(
        formKey="Z_FORM",
        variables=[
            Variable(variableName="B_VAR", sequence=2),
            Variable(variableName="A_VAR", sequence=1),
        ],
    )
    form_a = Form(
        formKey="A_FORM",
        variables=[
            Variable(
                variableName="V1",
                choices=[
                    Choice(choiceText="Two", choiceValue="2", position=2),
                    Choice(choiceText="One", choiceValue="1", position=1),
                ],
            )
        ],
        businessLogic=[
            BusinessLogicRule(id=200, name="Rule B", sequence=2),
            BusinessLogicRule(id=100, name="Rule A", sequence=1),
        ],
    )

    dd = DataDictionary(
        forms=[form_z, form_a],
        unassignedBusinessLogic=[
            BusinessLogicRule(id=999, name="Unassigned B", sequence=2),
            BusinessLogicRule(id=888, name="Unassigned A", sequence=1),
        ],
    )

    sorted_dd = sort_data_dictionary(dd)

    # Forms ordered by formKey
    assert [f.formKey for f in sorted_dd.forms] == ["A_FORM", "Z_FORM"]

    # Variables ordered by sequence
    assert [v.variableName for v in sorted_dd.forms[1].variables] == ["A_VAR", "B_VAR"]

    # Choices ordered by position
    assert [c.choiceValue for c in sorted_dd.forms[0].variables[0].choices] == ["1", "2"]

    # Form rules ordered by sequence
    assert [r.id for r in sorted_dd.forms[0].businessLogic] == [100, 200]

    # Unassigned rules ordered by sequence
    assert [r.id for r in sorted_dd.unassignedBusinessLogic] == [888, 999]


def test_to_json_exclude_generated_at() -> None:
    """Test to_json emission with and without generatedAt."""
    dd = DataDictionary(
        metadata=Metadata(studyKey="TEST", generatedAt="09-28-2026 12:00:00"),
        forms=[Form(formKey="F1")],
    )

    json_with_time = to_json(dd, exclude_generated_at=False)
    assert "generatedAt" in json_with_time
    assert "09-28-2026 12:00:00" in json_with_time

    json_without_time = to_json(dd, exclude_generated_at=True)
    assert "generatedAt" not in json_without_time
    assert "studyKey" in json_without_time


def test_to_file(tmp_path: Path) -> None:
    """Test writing canonical JSON to file."""
    dd = DataDictionary(
        metadata=Metadata(studyKey="TEST"),
        forms=[Form(formKey="F1", formName="Form One")],
    )
    dest = tmp_path / "subdir" / "datadict.json"
    to_file(dd, dest)

    assert dest.exists()
    content = dest.read_text(encoding="utf-8")
    loaded = json.loads(content)
    assert loaded["forms"][0]["formKey"] == "F1"
