"""Unit tests for ``imednet records list -o`` PHI masking, flattening, and file path targets."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from imednet import cli
from imednet.integrations.enrichment import EnrichmentPipeline, StudyConfiguration
from imednet.models.records import Record


def _sample_record(
    *,
    record_id: int = 101,
    subject_key: str = "SUBJ-001",
    record_data: dict[str, object] | None = None,
) -> Record:
    return Record(
        study_key="STUDY-1",
        record_id=record_id,
        subject_key=subject_key,
        form_key="DM",
        record_status="Record Complete",
        record_data=record_data
        or {
            "usubjid": f"STUDY-1-{subject_key}",
            "subjid": subject_key,
            "studyid": "STUDY-1",
            "patient_name": "Jane Doe",
            "ssn": "123-45-6789",
            "dob": "1980-01-02",
            "email": "jane.doe@example.com",
            "brthdtc": "1980-01-02",
            "custom_chart_id": "CHART-9001",
            "vitals": {"sys": 122, "note": "=cmd|' /C calc'!A0"},
            "AE_NOTE": "+SUM(A1:A2)",
        },
    )


def _invoke_cli(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    exit_code = 0
    try:
        cli.app(args)
    except SystemExit as exc:
        exit_code = int(exc.code) if isinstance(exc.code, int) else 1
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


def test_records_list_explicit_csv_path_flattens_masks_phi_and_sanitizes_formulas(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Write flattened, PHI-masked, formula-safe CSV to an explicit destination path by default."""
    sdk = MagicMock()
    sdk.records.list.return_value = [
        _sample_record(record_id=101, subject_key="SUBJ-001"),
        _sample_record(
            record_id=102,
            subject_key="SUBJ-002",
            record_data={"EXTRA_VAR": "extra-value", "patient_name": "John Smith"},
        ),
    ]
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda: sdk)

    pipeline = EnrichmentPipeline(
        StudyConfiguration(study_key="STUDY-1", phi_fields=["custom_chart_id"])
    )
    monkeypatch.setattr(
        "imednet.cli.utils.export.resolve_enrichment_pipeline",
        lambda sk: pipeline if sk == "STUDY-1" else None,
    )
    mapper_spy = MagicMock(side_effect=AssertionError("RecordMapper must not be called"))
    monkeypatch.setattr("imednet.integrations.export._record_mapper", mapper_spy)

    out_csv = tmp_path / "out" / "records.csv"
    code, out, err = _invoke_cli(["records", "list", "STUDY-1", "-o", str(out_csv)], capsys)

    assert code == 0
    assert out_csv.exists()
    assert f"Saved 2 records to {out_csv}" in out
    assert "deprecated" in err.lower()
    mapper_spy.assert_not_called()

    with out_csv.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(rows) == 2
    first = rows[0]
    assert "recordData" not in first
    # Exempt relational identifiers stay unmasked
    assert first["studyKey"] == "STUDY-1"
    assert first["subjectKey"] == "SUBJ-001"
    assert first["recordId"] == "101"
    assert first["usubjid"] == "STUDY-1-SUBJ-001"
    assert first["subjid"] == "SUBJ-001"
    assert first["studyid"] == "STUDY-1"
    # Default PHI and StudyConfiguration phiFields are masked
    assert first["patient_name"] == "***MASKED***"
    assert first["ssn"] == "***MASKED***"
    assert first["dob"] == "***MASKED***"
    assert first["email"] == "***MASKED***"
    assert first["brthdtc"] == "***MASKED***"
    assert first["custom_chart_id"] == "***MASKED***"
    # Nested recordData keys are flattened and formula prefixes are escaped
    assert first["vitals.sys"] == "122"
    assert first["vitals.note"] == "'=cmd|' /C calc'!A0"
    assert first["AE_NOTE"] == "'+SUM(A1:A2)"
    # Second row union column is preserved
    assert rows[1]["EXTRA_VAR"] == "extra-value"
    assert rows[1]["patient_name"] == "***MASKED***"


def test_records_list_no_mask_phi_and_explicit_json_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Allow --no-mask-phi opt-out and explicit .json destination paths while keeping CSV formula safety."""
    sdk = MagicMock()
    sdk.records.list.return_value = [_sample_record()]
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda: sdk)
    monkeypatch.setattr(
        "imednet.cli.utils.export.resolve_enrichment_pipeline",
        lambda _sk: EnrichmentPipeline(
            StudyConfiguration(study_key="STUDY-1", phi_fields=["custom_chart_id"])
        ),
    )

    out_csv = tmp_path / "raw" / "unmasked.csv"
    code_csv, _, _ = _invoke_cli(
        ["records", "list", "STUDY-1", "-o", str(out_csv), "--no-mask-phi"],
        capsys,
    )
    assert code_csv == 0
    with out_csv.open(encoding="utf-8", newline="") as handle:
        csv_row = next(csv.DictReader(handle))
    assert csv_row["patient_name"] == "Jane Doe"
    assert csv_row["ssn"] == "123-45-6789"
    assert csv_row["custom_chart_id"] == "CHART-9001"
    assert csv_row["vitals.note"] == "'=cmd|' /C calc'!A0"

    # Default --mask-phi on explicit .json destination path
    sdk.records.list.return_value = [_sample_record()]
    out_json = tmp_path / "CasePreserved" / "AE_Export.json"
    code_json, _, _ = _invoke_cli(
        ["records", "list", "STUDY-1", "--mask-phi", "-o", str(out_json)],
        capsys,
    )
    assert code_json == 0
    assert out_json.exists()
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert len(payload) == 1
    assert payload[0]["patient_name"] == "***MASKED***"
    assert payload[0]["custom_chart_id"] == "***MASKED***"
    assert payload[0]["vitals.sys"] == 122
    assert payload[0]["subjectKey"] == "SUBJ-001"


def test_records_list_legacy_tokens_and_conflict_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Preserve ./records.csv and ./records.json for -o csv|json and exit 1 on conflicts."""
    sdk = MagicMock()
    sdk.records.list.return_value = [_sample_record()]
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda: sdk)
    monkeypatch.chdir(tmp_path)

    code_csv, _, _ = _invoke_cli(["records", "list", "STUDY-1", "-o", "csv"], capsys)
    assert code_csv == 0
    assert (tmp_path / "records.csv").exists()

    sdk.records.list.return_value = [_sample_record()]
    code_json, _, _ = _invoke_cli(["records", "list", "STUDY-1", "-o", "json"], capsys)
    assert code_json == 0
    assert (tmp_path / "records.json").exists()

    sdk.records.list.reset_mock()
    code_conflict, out_conflict, err_conflict = _invoke_cli(
        ["records", "list", "STUDY-1", "--format", "json", "-o", str(tmp_path / "conflict.csv")],
        capsys,
    )
    assert code_conflict == 1
    assert out_conflict == ""
    assert "Choose --format json for stdout or --output for a file." in err_conflict
    sdk.records.list.assert_not_called()

    for bad_target in ("./out/records.parquet", ".csv", "   "):
        code_bad, out_bad, err_bad = _invoke_cli(
            ["records", "list", "STUDY-1", "-o", bad_target],
            capsys,
        )
        assert code_bad == 1
        assert out_bad == ""
        assert "Invalid output format" in err_bad
        sdk.records.list.assert_not_called()
