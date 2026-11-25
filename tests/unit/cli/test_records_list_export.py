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


def test_records_list_json_masks_default_and_study_phi_while_preserving_scalars_and_opt_out(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Mask PHI by default and with --mask-phi in --format json while preserving scalars and --no-mask-phi."""
    sdk = MagicMock()
    sdk._client.base_url = "https://edc.example.org"
    sdk._client.auth.security_key = "test-sec-key"
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda: sdk)

    observed_contexts: list[tuple[str | None, str | None]] = []

    def _fake_resolve_study_cfg(sk: str | None) -> StudyConfiguration | None:
        from imednet.core.context import get_base_url_context, get_security_key_context

        observed_contexts.append((get_base_url_context(), get_security_key_context()))
        if sk == "STUDY-1":
            return StudyConfiguration(
                study_key="STUDY-1",
                phi_fields=["custom_chart_id", "subjectKey"],
            )
        return None

    monkeypatch.setattr(
        "imednet.cli.utils.export.resolve_study_configuration",
        _fake_resolve_study_cfg,
    )

    def _rich_record() -> Record:
        return _sample_record(
            record_data={
                "usubjid": "STUDY-1-SUBJ-001",
                "subjid": "SUBJ-001",
                "studyid": "STUDY-1",
                "patient_name": "Jane Doe",
                "ssn": "123-45-6789",
                "dob": "1980-01-02",
                "custom_chart_id": "CHART-9001",
                "value": 0,
                "flag": False,
                "empty": None,
                "vitals": {
                    "sys": 122,
                    "note": "=cmd|' /C calc'!A0",
                    "customChartId": "CHART-NESTED",
                    "patientInitials": "JD",
                },
            }
        )

    # 1. Default (--mask-phi enabled by default) with --format json
    sdk.records.list.return_value = [_rich_record()]
    cli.app(["records", "list", "STUDY-1", "--format", "json"])
    default_payload = json.loads(capsys.readouterr().out)
    default_item = default_payload["items"][0]
    default_rec_data = default_item["recordData"]
    assert default_item["studyKey"] == "STUDY-1"
    assert default_item["subjectKey"] == "SUBJ-001"
    assert default_item["recordId"] == 101
    assert default_rec_data["usubjid"] == "STUDY-1-SUBJ-001"
    assert default_rec_data["subjid"] == "SUBJ-001"
    assert default_rec_data["studyid"] == "STUDY-1"
    assert default_rec_data["patient_name"] == "***MASKED***"
    assert default_rec_data["ssn"] == "***MASKED***"
    assert default_rec_data["dob"] == "***MASKED***"
    assert default_rec_data["custom_chart_id"] == "***MASKED***"
    assert default_rec_data["value"] == 0 and type(default_rec_data["value"]) is int
    assert default_rec_data["flag"] is False
    assert default_rec_data["empty"] is None
    assert default_rec_data["vitals"] == {
        "sys": 122,
        "note": "=cmd|' /C calc'!A0",
        "customChartId": "***MASKED***",
        "patientInitials": "***MASKED***",
    }
    assert observed_contexts == [("https://edc.example.org", "test-sec-key")]

    # 2. Explicit --mask-phi --format json verification check
    sdk.records.list.return_value = [_rich_record()]
    cli.app(["records", "list", "STUDY-1", "--mask-phi", "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    rec_data = payload["items"][0]["recordData"]
    assert rec_data["patient_name"] == "***MASKED***"
    assert rec_data["ssn"] == "***MASKED***"
    assert rec_data["custom_chart_id"] == "***MASKED***"
    assert payload["items"][0]["subjectKey"] == "SUBJ-001"

    # 3. Explicit --no-mask-phi --format json opt-out returns raw Record payload
    observed_contexts.clear()
    sdk.records.list.return_value = [_rich_record()]
    cli.app(["records", "list", "STUDY-1", "--no-mask-phi", "--format", "json"])
    raw_payload = json.loads(capsys.readouterr().out)
    raw_rec_data = raw_payload["items"][0]["recordData"]
    assert raw_rec_data["patient_name"] == "Jane Doe"
    assert raw_rec_data["ssn"] == "123-45-6789"
    assert raw_rec_data["custom_chart_id"] == "CHART-9001"
    assert raw_rec_data["vitals"]["customChartId"] == "CHART-NESTED"
    assert raw_rec_data["vitals"]["patientInitials"] == "JD"
    assert observed_contexts == []


def test_records_list_interactive_grid_masks_initial_details_and_lazy_pagination_once(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Mask InteractiveDataGrid details and on_load_all records while resolving StudyConfiguration once."""
    from imednet.cli.utils.export import mask_cli_records
    from imednet.cli.utils.grid import extract_item_details

    sdk = MagicMock()
    sdk._client.base_url = "https://edc.example.org"
    sdk._client.auth.security_key = "grid-sec-key"
    sdk.records.list.return_value = iter(
        [
            _sample_record(record_id=101, subject_key="SUBJ-001"),
            _sample_record(
                record_id=102,
                subject_key="SUBJ-002",
                record_data={
                    "patient_name": "John Smith",
                    "ssn": "999-00-1111",
                    "custom_chart_id": "CHART-9002",
                    "usubjid": "STUDY-1-SUBJ-002",
                },
            ),
        ]
    )
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda: sdk)
    monkeypatch.setattr("imednet.cli.utils.grid.is_interactive_terminal", lambda: True)

    resolve_spy = MagicMock(
        return_value=StudyConfiguration(study_key="STUDY-1", phi_fields=["custom_chart_id"])
    )
    monkeypatch.setattr("imednet.cli.utils.export.resolve_study_configuration", resolve_spy)

    captured_grid: dict[str, object] = {}

    def _fake_run_interactive_grid(**kwargs: object) -> bool:
        captured_grid.update(kwargs)
        loader = kwargs.get("on_load_all")
        assert callable(loader)
        captured_grid["loaded_more"] = loader()
        return True

    monkeypatch.setattr("imednet.cli.utils.grid.run_interactive_grid", _fake_run_interactive_grid)

    code, out, _err = _invoke_cli(["records", "list", "STUDY-1", "--limit", "1"], capsys)
    assert code == 0
    assert out == ""
    resolve_spy.assert_called_once_with("STUDY-1")

    initial_details = captured_grid["details"]
    assert isinstance(initial_details, list) and len(initial_details) == 1
    meta_1, vars_1 = extract_item_details(initial_details[0])
    assert meta_1["subject_key"] == "SUBJ-001"
    assert meta_1["record_id"] == 101
    assert vars_1["patient_name"] == "***MASKED***"
    assert vars_1["ssn"] == "***MASKED***"
    assert vars_1["custom_chart_id"] == "***MASKED***"
    assert vars_1["usubjid"] == "STUDY-1-SUBJ-001"

    loaded_rows, loaded_details = captured_grid["loaded_more"]  # type: ignore[misc]
    assert len(loaded_rows) == 1 and loaded_rows[0]["Subject"] == "SUBJ-002"
    assert len(loaded_details) == 1
    meta_2, vars_2 = extract_item_details(loaded_details[0])
    assert meta_2["subject_key"] == "SUBJ-002"
    assert vars_2["patient_name"] == "***MASKED***"
    assert vars_2["ssn"] == "***MASKED***"
    assert vars_2["custom_chart_id"] == "***MASKED***"
    assert vars_2["usubjid"] == "STUDY-1-SUBJ-002"

    # Direct mask_cli_records call without precomputed custom_phi_fields also resolves under sdk context
    direct_masked = mask_cli_records(
        [_sample_record(record_id=103, subject_key="SUBJ-003")],
        study_key="STUDY-1",
        mask_phi=True,
        sdk=sdk,
    )
    assert direct_masked[0].record_data["custom_chart_id"] == "***MASKED***"
    assert direct_masked[0].record_data["patient_name"] == "***MASKED***"
    assert direct_masked[0].subject_key == "SUBJ-003"


def test_mask_cli_records_nested_sequences_identity_and_pipeline_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover nested lists/tuples, diagnostic scrubbing, top-level custom PHI, and clean identity."""
    from imednet.cli.utils.export import mask_cli_records, resolve_cli_phi_fields

    assert resolve_cli_phi_fields(None) == set()
    assert resolve_cli_phi_fields("   ") == set()

    clean = Record(record_id=201, subject_key="SUBJ-CLEAN", record_data={"value": 0, "flag": False})
    assert mask_cli_records([clean], study_key="STUDY-CLEAN", mask_phi=True)[0] is clean
    assert mask_cli_records([clean], study_key="STUDY-CLEAN", mask_phi=False)[0] is clean
    assert mask_cli_records([], study_key="STUDY-CLEAN", mask_phi=True) == []

    # Fallback when resolve_enrichment_pipeline is patched in tests
    pipeline = EnrichmentPipeline(
        StudyConfiguration(study_key="STUDY-PIPE", phi_fields=["subjectOid", "chartCode"])
    )
    monkeypatch.setattr(
        "imednet.cli.utils.export.resolve_enrichment_pipeline",
        lambda sk: pipeline if sk == "STUDY-PIPE" else None,
    )

    rec = Record(
        study_key="STUDY-PIPE",
        record_id=202,
        subject_key="SUBJ-202",
        subject_oid="OID-SECRET-202",
        record_data={
            "history": [
                {"chartCode": "C-1", "keep": 1},
                ({"ssn": "111-22-3333"}, "plain"),
            ],
            "reasonForChange": "Updated patientName=Alice chartCode=C-9",
        },
    )
    masked = mask_cli_records([rec], study_key=None, mask_phi=True)[0]
    assert masked.subject_oid == "***MASKED***"
    assert masked.subject_key == "SUBJ-202"
    assert masked.record_data["history"][0] == {"chartCode": "***MASKED***", "keep": 1}
    assert masked.record_data["history"][1] == ({"ssn": "***MASKED***"}, "plain")
    assert "Alice" not in masked.record_data["reasonForChange"]
    assert "C-9" not in masked.record_data["reasonForChange"]
