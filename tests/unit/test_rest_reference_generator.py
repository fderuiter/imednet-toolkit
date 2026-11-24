"""Unit tests for the REST reference table generator."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from scripts.generate_rest_reference import (
    _extract_operations,
    _extract_sortable_fields,
    _generate_endpoint_rst,
    _normalize_resource_name,
    generate_rest_reference,
)

from imednet.models.users import User
from imednet.utils.filters import validate_filter_syntax, validate_record_filter

ROOT_DIR = Path(__file__).resolve().parents[2]
COLLECTION_PATH = ROOT_DIR / "imednet.postman_collection.json"
PACKAGE_COLLECTION_PATH = (
    ROOT_DIR
    / "packages"
    / "core"
    / "src"
    / "imednet"
    / "resources"
    / "imednet.postman_collection.json"
)
REST_TABLES_DIR = ROOT_DIR / "docs" / "reference" / "rest_tables"
REST_API_REFERENCE = ROOT_DIR / "docs" / "reference" / "rest_api_reference.rst"

EXPECTED_RESOURCES = [
    "codings",
    "forms",
    "intervals",
    "jobs",
    "queries",
    "record_revisions",
    "records",
    "sites",
    "studies",
    "subjects",
    "users",
    "variables",
    "visits",
]


def test_postman_collection_exists() -> None:
    """Verify the root Postman collection file is present and valid JSON."""
    assert COLLECTION_PATH.exists(), f"Missing Postman collection at {COLLECTION_PATH}"
    data = json.loads(COLLECTION_PATH.read_text(encoding="utf-8"))
    assert "item" in data
    assert "variable" in data


def test_generate_rest_reference_creates_all_thirteen_files(tmp_path: Path) -> None:
    """Verify generator outputs all thirteen clinical resource RST files."""
    generated = generate_rest_reference(
        collection_path=COLLECTION_PATH,
        output_dir=tmp_path,
    )
    assert len(generated) == 13

    generated_names = {p.stem for p in generated}
    assert generated_names == set(EXPECTED_RESOURCES)

    for p in generated:
        assert p.exists()
        assert p.stat().st_size > 0


def test_records_reference_specifies_post_route_payload_and_202(tmp_path: Path) -> None:
    """Verify records resource includes POST route, request body schema, and 202 status code."""
    generate_rest_reference(collection_path=COLLECTION_PATH, output_dir=tmp_path)
    records_rst = (tmp_path / "records.rst").read_text(encoding="utf-8")

    # Route and method
    assert "POST /api/v1/edc/studies/{studyKey}/records" in records_rst
    assert "GET /api/v1/edc/studies/{studyKey}/records" in records_rst

    # Request headers
    assert "``x-api-key``" in records_rst
    assert "``x-imn-security-key``" in records_rst
    assert "``Content-Type``" in records_rst

    # Request body schema fields
    assert "Request Body Schema" in records_rst
    assert "``formKey``" in records_rst
    assert "``subjectKey``" in records_rst
    assert "``siteName``" in records_rst
    assert "``intervalName``" in records_rst
    assert "``data``" in records_rst

    # Sample JSON payload
    assert ".. code-block:: json" in records_rst

    # Status code 202
    assert "``202``" in records_rst
    assert "Accepted" in records_rst


def test_subjects_reference_specifies_query_params_and_path_vars(tmp_path: Path) -> None:
    """Verify subjects resource includes path variable studyKey and query parameters."""
    generate_rest_reference(collection_path=COLLECTION_PATH, output_dir=tmp_path)
    subjects_rst = (tmp_path / "subjects.rst").read_text(encoding="utf-8")

    assert "GET /api/v1/edc/studies/{studyKey}/subjects" in subjects_rst
    assert "``studyKey``" in subjects_rst
    assert "``page``" in subjects_rst
    assert "``size``" in subjects_rst
    assert "``sort``" in subjects_rst
    assert "``filter``" in subjects_rst
    assert ":ref:`filter_syntax`" in subjects_rst


def test_jobs_reference_specifies_batch_id_path_var(tmp_path: Path) -> None:
    """Verify jobs resource includes batchId path variable."""
    generate_rest_reference(collection_path=COLLECTION_PATH, output_dir=tmp_path)
    jobs_rst = (tmp_path / "jobs.rst").read_text(encoding="utf-8")

    assert "GET /api/v1/edc/studies/{studyKey}/jobs/{batchId}" in jobs_rst
    assert "``studyKey``" in jobs_rst
    assert "``batchId``" in jobs_rst
    assert "``200``" in jobs_rst


def test_rest_api_reference_includes_all_thirteen_resources() -> None:
    """Verify docs/reference/rest_api_reference.rst includes all 13 generated files."""
    assert REST_API_REFERENCE.exists()
    content = REST_API_REFERENCE.read_text(encoding="utf-8")

    for res in EXPECTED_RESOURCES:
        expected_include = f".. include:: rest_tables/{res}.rst"
        assert expected_include in content, f"Missing {expected_include} in rest_api_reference.rst"


def test_generator_idempotence(tmp_path: Path) -> None:
    """Verify running generator repeatedly produces identical output."""
    dir1 = tmp_path / "run1"
    dir2 = tmp_path / "run2"

    generate_rest_reference(collection_path=COLLECTION_PATH, output_dir=dir1)
    generate_rest_reference(collection_path=COLLECTION_PATH, output_dir=dir2)

    for res in EXPECTED_RESOURCES:
        file1 = dir1 / f"{res}.rst"
        file2 = dir2 / f"{res}.rst"
        assert file1.read_text(encoding="utf-8") == file2.read_text(encoding="utf-8")


def _extract_subsection_definition_literals(rst_text: str, heading: str) -> list[str]:
    """Extract top-level inline literal terms (``...``) from a named ``~`` RST subsection.

    Preserves quoted spaces and escapes verbatim without stripping internal whitespace.
    """
    lines = rst_text.splitlines()
    examples: list[str] = []
    in_section = False
    i = 0
    while i < len(lines):
        if (
            lines[i] == heading
            and i + 1 < len(lines)
            and set(lines[i + 1]) == {"~"}
            and len(lines[i + 1]) >= len(heading)
        ):
            in_section = True
            i += 2
            continue

        if in_section:
            if (
                lines[i]
                and i + 1 < len(lines)
                and lines[i + 1]
                and len(set(lines[i + 1])) == 1
                and lines[i + 1][0] in ("~", "-", "=")
                and len(lines[i + 1]) >= len(lines[i])
            ):
                break
            match = re.fullmatch(r"``(.+)``", lines[i])
            if match:
                examples.append(match.group(1))
        i += 1
    return examples


def test_generated_files_formatting(tmp_path: Path) -> None:
    """Verify generated files end with exactly one newline and no trailing blank lines."""
    generated = generate_rest_reference(collection_path=COLLECTION_PATH, output_dir=tmp_path)
    for p in generated:
        content = p.read_text(encoding="utf-8")
        assert content.endswith("\n")
        assert not content.endswith("\n\n")


def test_purified_rest_tables_have_zero_unbacked_routes_or_fields() -> None:
    """Verify committed REST reference tables match upstream Studies, Jobs, Records, and Intervals."""
    studies_rst = (REST_TABLES_DIR / "studies.rst").read_text(encoding="utf-8")
    assert "GET /api/v1/edc/studies" in studies_rst
    assert "Retrieve a study" not in studies_rst
    assert "/api/v1/edc/studies/{studyKey}" not in studies_rst
    assert '"lastUpdated"' in studies_rst
    assert "``studyKey,asc``" in studies_rst

    jobs_rst = (REST_TABLES_DIR / "jobs.rst").read_text(encoding="utf-8")
    assert '"progress"' not in jobs_rst
    assert '"resultUrl"' not in jobs_rst
    assert '"dateStarted"' in jobs_rst

    intervals_rst = (REST_TABLES_DIR / "intervals.rst").read_text(encoding="utf-8")
    assert "``filter``" not in intervals_rst
    assert "``intervalId,asc``" in intervals_rst

    users_rst = (REST_TABLES_DIR / "users.rst").read_text(encoding="utf-8")
    assert "``filter``" not in users_rst
    assert "``login,asc``" in users_rst

    records_rst = (REST_TABLES_DIR / "records.rst").read_text(encoding="utf-8")
    assert "   * - ``formKey``\n     - String\n     - Conditional" in records_rst
    assert "   * - ``subjectKey``\n     - String\n     - Conditional" in records_rst
    assert "   * - ``siteName``\n     - String\n     - Conditional" in records_rst
    assert "   * - ``intervalName``\n     - String\n     - Conditional" in records_rst
    assert "   * - ``data``\n     - Object\n     - Required" in records_rst

    ref_rst = REST_API_REFERENCE.read_text(encoding="utf-8")
    assert "``202``" in ref_rst
    assert "``GET /api/v1/edc/studies/{studyKey}``" not in ref_rst


def test_rest_api_reference_filter_examples_pass_sdk_validators() -> None:
    """Verify every filter and recordDataFilter example in rest_api_reference.rst passes SDK syntax validation."""
    content = REST_API_REFERENCE.read_text(encoding="utf-8")

    filter_examples = _extract_subsection_definition_literals(content, "Filter examples")
    record_filter_examples = _extract_subsection_definition_literals(
        content, "recordDataFilter examples"
    )

    assert "formId>10" in filter_examples
    assert 'formType=="SUBJECT"' in filter_examples
    assert 'formId>10;formType=="SUBJECT"' in filter_examples
    assert 'siteName=="Bright Test Site"' in filter_examples
    assert len(filter_examples) >= 5

    for expr in filter_examples:
        validate_filter_syntax(expr)

    assert "AESER==Serious" in record_filter_examples
    assert "AESER==Serious;GENDER==Male" in record_filter_examples
    assert "AESER=~bronch" in record_filter_examples
    assert "AESER==bronch" not in record_filter_examples

    for expr in record_filter_examples:
        validate_record_filter(expr)
        if "=~" in expr:
            with pytest.raises(ValueError, match="Invalid filter operator '=~'"):
                validate_filter_syntax(expr)
        else:
            validate_filter_syntax(expr)


def test_subsection_literal_extractor_preserves_quoted_spaces() -> None:
    """Verify RST example parser preserves spaces inside double-quoted filter values."""
    sample_rst = (
        "Filter examples\n"
        "~~~~~~~~~~~~~~~\n\n"
        '``siteName=="General Hospital West";formType=="SUBJECT"``\n'
        "  Quoted multi-word value.\n"
    )
    extracted = _extract_subsection_definition_literals(sample_rst, "Filter examples")
    assert extracted == ['siteName=="General Hospital West";formType=="SUBJECT"']
    validate_filter_syntax(extracted[0])


def test_rest_api_reference_documents_status_codes_pagination_envelopes_and_exclusions() -> None:
    """Verify manual REST reference covers 202/409, paging limits, envelopes, connector exclusivity, and exclusions."""
    content = REST_API_REFERENCE.read_text(encoding="utf-8")

    # Status codes 202 and 409
    assert "``202``" in content
    assert "``409``" in content
    assert "ConflictError" in content

    # Pagination and envelopes
    assert "``page``" in content
    assert "``size``" in content
    assert "``500``" in content
    assert "``sort``" in content
    assert '"pagination"' in content
    assert "/api/v1/edc/studies/{studyKey}" in content
    assert "/api/v1/edc/studies/{studyKey}/jobs/{batchId}" in content

    # Connector exclusivity and filter exclusions
    assert "validate_connector_exclusivity" in content
    assert "400 Bad Request" in content
    assert "/users" in content
    assert "/jobs/{batchId}" in content
    assert ".. _filter_syntax:" in content


def test_users_reference_sorts_only_on_valid_user_fields(tmp_path: Path) -> None:
    """Verify users.rst defaults to login,asc and sorts exclusively on valid User fields."""
    generate_rest_reference(collection_path=COLLECTION_PATH, output_dir=tmp_path)
    users_rst = (tmp_path / "users.rst").read_text(encoding="utf-8")

    valid_user_fields = {
        field_info.alias or field_name
        for field_name, field_info in User.model_fields.items()
        if (field_info.alias or field_name) != "roles"
    }
    assert valid_user_fields == {
        "userId",
        "login",
        "firstName",
        "lastName",
        "email",
        "userActiveInStudy",
    }

    assert "``login,asc``" in users_rst
    assert "studyKey,asc" not in users_rst
    assert "dateCreated,desc" not in users_rst
    assert "``filter``" not in users_rst

    sort_exprs = re.findall(r"``([A-Za-z0-9_]+),(?:asc|desc)``", users_rst)
    assert sort_exprs, "Expected at least one sort example in users.rst"
    assert set(sort_exprs) <= valid_user_fields

    for field in valid_user_fields:
        assert f"``{field}``" in users_rst


def test_all_generated_sort_examples_match_postman_sample_fields(tmp_path: Path) -> None:
    """Verify every generated resource sort example uses fields present in its Postman sample data."""
    generate_rest_reference(collection_path=COLLECTION_PATH, output_dir=tmp_path)
    collection = json.loads(COLLECTION_PATH.read_text(encoding="utf-8"))

    fields_by_resource: dict[str, set[str]] = {}
    for folder, op in _extract_operations(collection["item"]):
        res_key = _normalize_resource_name(folder)
        fields_by_resource.setdefault(res_key, set()).update(_extract_sortable_fields(op))

    for res_key in EXPECTED_RESOURCES:
        rst_content = (tmp_path / f"{res_key}.rst").read_text(encoding="utf-8")
        sort_fields = re.findall(r"``([A-Za-z0-9_]+),(?:asc|desc)``", rst_content)
        if sort_fields:
            assert set(sort_fields) <= fields_by_resource[res_key], (
                f"{res_key}.rst references sort field(s) outside Postman sample: "
                f"{set(sort_fields) - fields_by_resource[res_key]}"
            )


def test_sort_derivation_handles_non_list_sample_responses() -> None:
    """Verify operations lacking a sample data list (flat dict or empty) render cleanly."""
    synthetic_op = {
        "name": "Retrieve flat resource",
        "request": {
            "method": "GET",
            "url": {
                "path": ["studies", ":studyKey", "jobs", ":batchId"],
                "query": [{"key": "sort", "value": "<string>"}],
            },
        },
        "response": [
            {
                "code": 200,
                "status": "OK",
                "body": json.dumps({"jobId": "1", "batchId": "b1", "state": "COMPLETED"}),
            }
        ],
    }
    assert _extract_sortable_fields(synthetic_op) == []
    rendered = "\n".join(_generate_endpoint_rst(synthetic_op, res_key="jobs"))
    assert "``sort``" in rendered
    assert "studyKey,asc" not in rendered
    assert "dateCreated,desc" not in rendered


def test_records_post_documents_email_notify_array_framing_and_conditional_ids(
    tmp_path: Path,
) -> None:
    """Verify POST /records documents x-email-notify, JSON array body, required data, and conditional IDs."""
    generate_rest_reference(collection_path=COLLECTION_PATH, output_dir=tmp_path)
    records_rst = (tmp_path / "records.rst").read_text(encoding="utf-8")

    list_section, create_section = records_rst.split("Create records\n^^^^^^^^^^^^^^", maxsplit=1)

    assert "``x-email-notify``" not in list_section
    assert "``x-email-notify``" in create_section
    assert "Optional" in create_section
    assert "JSON array of record objects" in create_section

    conditional_fields = (
        "formKey",
        "formId",
        "siteName",
        "siteId",
        "subjectKey",
        "subjectId",
        "subjectOid",
        "intervalName",
        "intervalId",
        "recordId",
    )
    for field in conditional_fields:
        pattern = rf"\* - ``{field}``\n\s+- (?:String|Integer)\n\s+- Conditional\n"
        assert re.search(pattern, create_section), (
            f"Expected {field} marked Conditional in records.rst"
        )

    assert re.search(r"\* - ``data``\n\s+- Object\n\s+- Required\n", create_section)


def test_committed_rest_tables_match_fresh_generator_output(tmp_path: Path) -> None:
    """Verify committed docs/reference/rest_tables/*.rst match fresh generator output."""
    generate_rest_reference(collection_path=COLLECTION_PATH, output_dir=tmp_path)
    for res in EXPECTED_RESOURCES:
        committed = (REST_TABLES_DIR / f"{res}.rst").read_text(encoding="utf-8")
        fresh = (tmp_path / f"{res}.rst").read_text(encoding="utf-8")
        assert committed == fresh, (
            f"Committed rest_tables/{res}.rst is out of sync with generate_rest_reference.py"
        )


def test_postman_collection_files_stay_byte_identical() -> None:
    """Verify root and packaged Postman collection JSON files remain byte-identical."""
    assert COLLECTION_PATH.exists()
    assert PACKAGE_COLLECTION_PATH.exists()
    assert COLLECTION_PATH.read_bytes() == PACKAGE_COLLECTION_PATH.read_bytes()
