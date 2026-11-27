"""Unit tests for Data Dictionary, rule validation, UAT, and CDM routing guides."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from scripts.validate_docs import check_cli_snippets
from scripts.validate_showcases import execute_showcases_in_simulation

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = REPO_ROOT / "docs"
EXAMPLES_DIR = REPO_ROOT / "examples"

DATADICT_SUBCOMMANDS = (
    "from-csv",
    "from-odm",
    "to-odm",
    "from-redcap",
    "from-api",
    "validate",
    "diff",
    "schema",
)


def test_data_dictionary_guide_registered_and_comprehensive() -> None:
    """docs/how-to/data_dictionary.rst is indexed and covers loaders, ODM, diff, rules, and CLI."""
    guide_path = DOCS_DIR / "how-to" / "data_dictionary.rst"
    assert guide_path.is_file()
    content = guide_path.read_text(encoding="utf-8")

    index_content = (DOCS_DIR / "index.rst").read_text(encoding="utf-8")
    assert "how-to/data_dictionary" in index_content

    expected_api_tokens = (
        "load_from_csv_files",
        "load_from_csv",
        "load_from_cdisc_odm",
        "load_from_redcap",
        "load_from_api",
        "to_odm_xml",
        "to_json",
        "get_schema",
        "validate",
        "diff",
        "LogicTreeEvaluator",
        "parse_logic_xml",
        "extract_variable_constraints",
        "RangeCheckConstraint",
        "validate_regex_pattern",
        "range_check_soft",
        "unsafe_regex",
        "schema=",
    )
    for token in expected_api_tokens:
        assert token in content, f"Missing API token {token!r} in data_dictionary.rst"

    import imednet.datadict as datadict_pkg
    from imednet.datadict import RangeCheckConstraint, validate_regex_pattern

    assert "RangeCheckConstraint" in datadict_pkg.__all__
    assert "validate_regex_pattern" in datadict_pkg.__all__
    assert RangeCheckConstraint is not None
    assert callable(validate_regex_pattern)

    for subcmd in DATADICT_SUBCOMMANDS:
        assert f"imednet datadict {subcmd}" in content, (
            f"Missing CLI subcommand {subcmd!r} in data_dictionary.rst"
        )


def test_cli_guide_documents_all_datadict_subcommands_and_keeps_required_tokens() -> None:
    """docs/how-to/cli.rst documents all eight datadict subcommands and passes CLI parser checks."""
    cli_content = (DOCS_DIR / "how-to" / "cli.rst").read_text(encoding="utf-8")

    for subcmd in DATADICT_SUBCOMMANDS:
        assert f"imednet datadict {subcmd}" in cli_content, (
            f"Missing 'imednet datadict {subcmd}' in docs/how-to/cli.rst"
        )

    for required_token in (
        "imednet tui",
        "--high-contrast",
        "InteractiveDataGrid",
        "IMEDNET_NON_INTERACTIVE=1",
        "--interactive",
    ):
        assert required_token in cli_content

    assert check_cli_snippets() is False


def test_clinical_data_manager_routing_targets_data_dictionary_and_uat() -> None:
    """Clinical Data Manager card and showcase panel link to Data Dictionary and UAT guides."""
    index_content = (DOCS_DIR / "index.rst").read_text(encoding="utf-8")

    cdm_card_match = re.search(
        r"\.\.\s+rubric::\s+Clinical Data Managers\b.*?(?=\n\s*\.\.\s+container::\s+role-card)",
        index_content,
        flags=re.DOTALL,
    )
    assert cdm_card_match is not None
    cdm_card = cdm_card_match.group(0)
    assert "how-to/data_dictionary" in cdm_card
    assert "how-to/workflows/uat_workflow" in cdm_card
    assert "form_builder" not in cdm_card
    assert (
        'aria-label="Inspect Form Schemas - Open Clinical Data Manager Live Explorer"' in cdm_card
    )

    panel_match = re.search(
        r'<div id="panel-datamanager"[^>]*>.*?(?=<!-- Panel 3: Regulatory Compliance Auditor -->)',
        index_content,
        flags=re.DOTALL,
    )
    assert panel_match is not None
    panel_html = panel_match.group(0)
    assert 'href="how-to/data_dictionary.html"' in panel_html
    assert 'href="how-to/workflows/uat_workflow.html"' in panel_html
    assert "form_builder.html" not in panel_html
    assert 'aria-label="Copy JSON — Copy schema JSON"' in panel_html


def test_form_builder_guide_states_legacy_scope_and_uses_uv_run() -> None:
    """docs/how-to/form_builder.rst warns about legacy formdez_save.php scope and uses uv run."""
    content = (DOCS_DIR / "how-to" / "form_builder.rst").read_text(encoding="utf-8")
    assert ".. warning::" in content
    assert "formdez_save.php" in content
    assert "PHPSESSID" in content
    assert "CSRFKey" in content
    assert ":doc:`/how-to/data_dictionary`" in content
    assert ":doc:`/how-to/workflows/uat_workflow`" in content
    assert 'uv run --with "imednet[cli]" python examples/build_form_payload.py' in content
    assert "hatch run" not in content


def test_schema_validation_and_caching_contrast_schemacache_with_datadictionary() -> None:
    """schema_validation.rst and caching.rst contrast BaseSchemaCache enforcement with DataDictionary rules."""
    schema_val = (DOCS_DIR / "explanation" / "schema_validation.rst").read_text(encoding="utf-8")
    caching = (DOCS_DIR / "how-to" / "caching.rst").read_text(encoding="utf-8")

    assert "ValidationSchemaProtocol" in schema_val
    assert "BaseSchemaCache" in schema_val
    assert "DataDictionary" in schema_val
    assert "LogicTreeEvaluator" in schema_val
    assert "***MASKED***" in schema_val
    assert "masked_phi" in schema_val
    assert "_retired_form_variables" in schema_val
    assert "Yes" in schema_val and "Auto Query" in schema_val
    assert "returns early" not in schema_val
    assert "required fields are present" not in schema_val

    assert "BaseSchemaCache" in caching
    assert "Enforced by" in caching
    assert "Skipped by" in caching
    assert "Yes" in caching and "Auto Query" in caching
    assert "masked_phi" in caching
    assert "DataDictionary" in caching
    assert ":doc:`/how-to/data_dictionary`" in caching
    assert ":doc:`/explanation/schema_validation`" in caching


def test_workflow_guides_cover_datadictionary_schema_and_uat_engine() -> None:
    """Workflow guides document schema=data_dictionary, UAT enrichment, and UATExecutionEngine."""
    reg_content = (DOCS_DIR / "how-to" / "register_subjects.rst").read_text(encoding="utf-8")
    bulk_content = (DOCS_DIR / "how-to" / "workflows" / "bulk_submission.rst").read_text(
        encoding="utf-8"
    )
    uat_content = (DOCS_DIR / "how-to" / "workflows" / "uat_workflow.rst").read_text(
        encoding="utf-8"
    )

    assert "RegisterSubjectsWorkflow" in reg_content
    assert "DataDictionary" in reg_content
    assert "schema=" in reg_content

    assert "RecordUpdateWorkflow" in bulk_content
    assert "create_batch" in bulk_content
    assert "DataDictionary" in bulk_content
    assert "schema=" in bulk_content

    for token in (
        "UATWorkflow",
        "UATSpecificationBuilder",
        "SyntheticRecordGenerator",
        "UATExecutionEngine",
        "EditCheckVerificationReport",
        "data_dictionary=",
    ):
        assert token in uat_content, f"Missing {token!r} in uat_workflow.rst"


def test_data_dictionary_and_uat_showcase_scripts_indexed_and_run_offline(tmp_path: Path) -> None:
    """examples/data_dictionary.py and examples/workflow_uat.py are indexed and pass offline execution."""
    examples_index = (DOCS_DIR / "tutorials" / "examples" / "index.rst").read_text(encoding="utf-8")
    assert "data_dictionary" in examples_index

    dd_tutorial = DOCS_DIR / "tutorials" / "examples" / "data_dictionary.rst"
    assert dd_tutorial.is_file()
    assert "/../examples/data_dictionary.py" in dd_tutorial.read_text(encoding="utf-8")

    dd_script = EXAMPLES_DIR / "data_dictionary.py"
    uat_script = EXAMPLES_DIR / "workflow_uat.py"
    assert dd_script.is_file()
    assert uat_script.is_file()

    shutil.copy2(dd_script, tmp_path / dd_script.name)
    shutil.copy2(uat_script, tmp_path / uat_script.name)
    assert execute_showcases_in_simulation(tmp_path, repo_root=REPO_ROOT) == []
