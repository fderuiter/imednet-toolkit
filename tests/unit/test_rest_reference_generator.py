"""Unit tests for the REST reference table generator."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.generate_rest_reference import (
    generate_rest_reference,
)

ROOT_DIR = Path(__file__).resolve().parents[2]
COLLECTION_PATH = ROOT_DIR / "imednet.postman_collection.json"
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


def test_generated_files_formatting(tmp_path: Path) -> None:
    """Verify generated files end with exactly one newline and no trailing blank lines."""
    generated = generate_rest_reference(collection_path=COLLECTION_PATH, output_dir=tmp_path)
    for p in generated:
        content = p.read_text(encoding="utf-8")
        assert content.endswith("\n")
        assert not content.endswith("\n\n")
