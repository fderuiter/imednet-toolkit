"""Tests for structural data dictionary diffing."""

from __future__ import annotations

import json

from imednet.datadict.diff import diff
from imednet.datadict.models import (
    BusinessLogicRule,
    Choice,
    DataDictionary,
    Form,
    Metadata,
    Variable,
)


def test_diff_identical_dictionaries() -> None:
    """Test diffing two identical data dictionaries."""
    dd1 = DataDictionary(
        metadata=Metadata(studyKey="S1", generatedAt="09-28-2026 10:00:00"),
        forms=[Form(formKey="F1", variables=[Variable(variableName="V1")])],
    )
    dd2 = DataDictionary(
        metadata=Metadata(studyKey="S1", generatedAt="09-28-2026 12:00:00"),
        forms=[Form(formKey="F1", variables=[Variable(variableName="V1")])],
    )
    report = diff(dd1, dd2)
    assert not report.has_changes
    assert "No differences found" in report.to_text()
    assert "No differences found" in report.to_markdown()


def test_diff_structural_changes() -> None:
    """Test detecting additions, removals, and modifications."""
    old_dd = DataDictionary(
        metadata=Metadata(studyKey="OLD_STUDY"),
        forms=[
            Form(
                formKey="F1",
                formName="Form 1",
                variables=[
                    Variable(
                        variableName="V1",
                        label="Old Label",
                        choices=[Choice(choiceText="Old", choiceValue="1")],
                    ),
                    Variable(variableName="V_REMOVED"),
                ],
                businessLogic=[BusinessLogicRule(id=1, name="Rule Old")],
            ),
            Form(formKey="F_REMOVED"),
        ],
    )

    new_dd = DataDictionary(
        metadata=Metadata(studyKey="NEW_STUDY"),
        forms=[
            Form(
                formKey="F1",
                formName="Form 1 Renamed",
                variables=[
                    Variable(
                        variableName="V1",
                        label="New Label",
                        choices=[Choice(choiceText="New", choiceValue="1")],
                    ),
                    Variable(variableName="V_ADDED"),
                ],
                businessLogic=[BusinessLogicRule(id=1, name="Rule Renamed")],
            ),
            Form(formKey="F_ADDED"),
        ],
        unassignedBusinessLogic=[BusinessLogicRule(id=99, name="Workflow Rule")],
    )

    report = diff(old_dd, new_dd)
    assert report.has_changes

    # Test report export formats
    text = report.to_text()
    assert "+ [Added]" in text
    assert "- [Removed]" in text
    assert "~ [Modified]" in text

    md = report.to_markdown()
    assert "### Data Dictionary Diff" in md
    assert "🟢 Added" in md
    assert "🔴 Removed" in md
    assert "🟡 Modified" in md

    json_str = report.to_json()
    parsed = json.loads(json_str)
    assert parsed["has_changes"] is True
    assert parsed["total_changes"] > 0


def test_diff_extra_attributes() -> None:
    """Test detecting additions, removals, and modifications in extraAttributes."""
    dd1 = DataDictionary(
        metadata=Metadata(extraAttributes={"versionTag": "v1", "dropped": "yes"}),
        forms=[
            Form(
                formKey="F1",
                extraAttributes={"formFlag": "alpha"},
                variables=[Variable(variableName="V1", extraAttributes={"custom": "old"})],
            )
        ],
    )
    dd2 = DataDictionary(
        metadata=Metadata(extraAttributes={"versionTag": "v2", "added": "yes"}),
        forms=[
            Form(
                formKey="F1",
                extraAttributes={"formFlag": "beta"},
                variables=[Variable(variableName="V1", extraAttributes={"custom": "new"})],
            )
        ],
    )
    report = diff(dd1, dd2)
    paths = {item.path for item in report.items}
    assert "metadata.versionTag" in paths
    assert "metadata.dropped" in paths
    assert "metadata.added" in paths
    assert "forms[F1].formFlag" in paths
    assert "forms[F1].variables[V1].custom" in paths


def test_diff_markdown_multiline_logic_does_not_break_table() -> None:
    """Test that multiline logic values are formatted on a single line in Markdown tables."""
    multiline_xml = "<rule>\n  <condition>true</condition>\n</rule>"
    dd1 = DataDictionary(
        forms=[
            Form(
                formKey="F1",
                businessLogic=[BusinessLogicRule(id=1, logicRawXml=multiline_xml)],
            )
        ]
    )
    dd2 = DataDictionary(
        forms=[
            Form(
                formKey="F1",
                businessLogic=[BusinessLogicRule(id=1, logicRawXml="<rule><false/></rule>")],
            )
        ]
    )
    report = diff(dd1, dd2)
    md = report.to_markdown()
    # Ensure no table row has unescaped line breaks splitting a row
    table_rows = [line for line in md.splitlines() if line.startswith("| 🟡 Modified")]
    assert len(table_rows) == 1
    assert "\n" not in table_rows[0]


def test_diff_rules_without_id_or_name() -> None:
    """Test that rules without explicit ID or name do not collide in diff."""
    dd1 = DataDictionary(
        unassignedBusinessLogic=[
            BusinessLogicRule(sequence=1, status="Active"),
            BusinessLogicRule(sequence=2, status="Inactive"),
        ]
    )
    dd2 = DataDictionary(
        unassignedBusinessLogic=[
            BusinessLogicRule(sequence=1, status="Inactive"),
            BusinessLogicRule(sequence=2, status="Active"),
        ]
    )
    report = diff(dd1, dd2)
    assert report.has_changes
    assert len(report.items) == 2
