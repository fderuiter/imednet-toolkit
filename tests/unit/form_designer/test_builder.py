"""Unit tests for builder."""

import pytest

from imednet.form_designer.builder import FormBuilder


def test_form_builder_initialization():
    """Test that form builder initialization."""
    builder = FormBuilder()
    assert len(builder.pages) == 1
    assert builder.current_page is not None


def test_form_builder_add_page():
    """Test that form builder add page."""
    builder = FormBuilder()
    builder.add_page()
    assert len(builder.pages) == 2


def test_form_builder_add_field_text():
    """Test that form builder add field text."""
    builder = FormBuilder()
    builder.add_field(type="text", label="My Text", question_name="my_text")

    page = builder.current_page
    assert len(page.entities) == 1
    table = page.entities[0]
    assert table.props.type == "table"
    assert len(table.rows) == 1

    row = table.rows[0]
    assert len(row.cols) == 2

    label_col = row.cols[0]
    assert label_col.entities[0].props.type == "label"
    assert label_col.entities[0].props.label == "My Text"

    field_col = row.cols[1]
    assert field_col.entities[0].props.type == "text"
    assert field_col.entities[0].props.question_name == "my_text"


def test_form_builder_add_field_invalid_type():
    """Test that form builder add field invalid type."""
    builder = FormBuilder()
    with pytest.raises(ValueError, match="Unsupported field type: invalid"):
        builder.add_field(type="invalid", label="Invalid", question_name="inv")  # type: ignore[arg-type]


def test_form_builder_add_all_field_types():
    """Test that form builder add all field types."""
    builder = FormBuilder()
    types = ["text", "number", "radio", "dropdown", "datetime", "upload", "checkbox", "memo"]

    for idx, t in enumerate(types):
        if t in ["radio", "dropdown", "checkbox"]:
            choices = [("Option A", "1"), ("Option B", "2")]
        else:
            choices = None

        builder.add_field(type=t, label=f"Label {t}", question_name=f"qn_{t}", choices=choices)

    page = builder.current_page
    # It adds everything into the same table if it exists
    table = page.entities[0]
    assert len(table.rows) == len(types)

    for idx, t in enumerate(types):
        field_col = table.rows[idx].cols[1]
        assert field_col.entities[0].props.type == t


def test_form_builder_add_section_header():
    """Test that form builder add section header."""
    builder = FormBuilder()
    builder.add_section_header("My Section")

    page = builder.current_page
    assert len(page.entities) == 1
    assert page.entities[0].props.type == "sep"
    assert page.entities[0].props.label == "My Section"


def test_form_builder_add_group_header():
    """Test that form builder add group header."""
    builder = FormBuilder()
    builder.add_group_header("My Group")

    page = builder.current_page
    assert len(page.entities) == 1
    table = page.entities[0]
    assert table.props.type == "table"
    assert len(table.rows) == 1
    row = table.rows[0]
    assert len(row.cols) == 2
    assert row.cols[0].entities[0].props.type == "label"
    assert row.cols[0].entities[0].props.label == "My Group"
    assert len(row.cols[1].entities) == 0


def test_form_builder_build():
    """Test that form builder build."""
    builder = FormBuilder()
    builder.add_section_header("My Section")
    layout = builder.build()
    assert layout.pages[0].entities[0].props.label == "My Section"


def test_form_builder_none_rows_initialization():
    """Test that form builder none rows initialization."""
    builder = FormBuilder()

    # We need to manually inject a table with no rows to hit lines 92 and 133
    from imednet.form_designer.models import Entity, TableProps

    table_props = TableProps(type="table", columns=2)
    table_no_rows = Entity(id="test_id", props=table_props, rows=None)
    builder.current_page.entities.append(table_no_rows)

    # Adding a group header should initialize `rows` to []
    builder.add_group_header("Test Group")
    assert table_no_rows.rows is not None
    assert len(table_no_rows.rows) == 1

    # Same for add_field
    table_no_rows.rows = None
    builder.add_field(type="text", label="Test Text", question_name="qn_test")
    assert table_no_rows.rows is not None
    assert len(table_no_rows.rows) == 1
