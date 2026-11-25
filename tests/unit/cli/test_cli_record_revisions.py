"""Unit tests for `imednet record-revisions list` CLI filters, summary rendering, and exports."""

from __future__ import annotations

import csv
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from imednet import cli
from imednet.models.record_revisions import RecordRevision
from imednet.models.study_config import StudyConfiguration


class _CliResult:
    def __init__(self, exit_code: int, stdout: str, stderr: str) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


def _invoke_cli(args: list[str]) -> _CliResult:
    out = io.StringIO()
    err = io.StringIO()
    code = 0
    try:
        with redirect_stdout(out), redirect_stderr(err):
            cli.app(args)
    except SystemExit as exc:
        code = int(exc.code) if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return _CliResult(code, out.getvalue(), err.getvalue())


@pytest.fixture(autouse=True)
def _cli_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("IMEDNET_API_KEY", "test-key")  # pragma: allowlist secret
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "test-secret")  # pragma: allowlist secret
    yield


def _sample_revisions() -> list[RecordRevision]:
    return [
        RecordRevision.model_validate(
            {
                "recordRevisionId": 9001,
                "studyKey": "STUDY_CLI",
                "recordId": 601,
                "recordOid": "OID-601-FULL-DETAIL",
                "recordRevision": 1,
                "dataRevision": 1,
                "recordStatus": "Complete",
                "subjectId": 501,
                "subjectOid": "SUBJ-OID-501",
                "subjectKey": "SUB1",
                "siteId": 201,
                "formKey": "F_VS",
                "intervalId": 401,
                "role": "CRC",
                "user": "crc_alice",
                "reasonForChange": '=CMD|patientName="Alice Secret" updated vitals',
                "deleted": False,
                "dateCreated": "2026-01-15 10:00:00",
                "lastUpdated": "2026-01-15 10:05:00",
            }
        )
    ]


def test_record_revisions_list_bounds_summary_and_json_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify --start-date/--end-date, dual -f bounds, concise table summary, and full JSON output."""
    mock_sdk = MagicMock()
    revisions = _sample_revisions()
    mock_sdk.record_revisions.list.return_value = revisions
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=mock_sdk))

    # 1. Table mode renders concise summary_fields (omitting verbose OIDs like recordOid/subjectOid)
    res_table = _invoke_cli(
        [
            "--no-interactive",
            "record-revisions",
            "list",
            "STUDY_CLI",
            "--start-date",
            "2026-01-01",
            "--end-date",
            "2026-01-31",
            "-f",
            "lastUpdated>=2026-01-10",
            "-f",
            "lastUpdated<=2026-01-20",
        ]
    )
    assert res_table.exit_code == 0, res_table.stderr
    assert "9001" in res_table.stdout
    assert "F_VS" in res_table.stdout
    assert "OID-601-FULL-DETAIL" not in res_table.stdout
    assert "Alice Secret" not in res_table.stdout

    call_args, call_kwargs = mock_sdk.record_revisions.list.call_args
    assert call_args == ("STUDY_CLI",)
    assert call_kwargs["start_date"] == "2026-01-01T00:00:00Z"
    assert call_kwargs["end_date"] == "2026-01-31T23:59:59Z"
    assert len(call_kwargs["lastUpdated"]) == 2

    # 2. --format json preserves all 19 RecordRevision attributes while masking inline PHI by default
    res_json = _invoke_cli(
        [
            "record-revisions",
            "list",
            "STUDY_CLI",
            "-f",
            "dateCreated>=2026-01-01",
            "-f",
            "dateCreated<=2026-01-31",
            "--format",
            "json",
        ]
    )
    assert res_json.exit_code == 0, res_json.stderr
    envelope = json.loads(res_json.stdout)
    assert envelope["resource"] == "record_revisions"
    item = envelope["items"][0]
    assert item["recordOid"] == "OID-601-FULL-DETAIL"
    assert item["subjectOid"] == "SUBJ-OID-501"
    assert item["intervalId"] == 401
    assert "Alice Secret" not in item["reasonForChange"]
    assert "***MASKED***" in item["reasonForChange"]

    # 3. Duplicate non-date -f attribute is still rejected
    res_dup = _invoke_cli(
        [
            "record-revisions",
            "list",
            "STUDY_CLI",
            "-f",
            "recordId==1",
            "-f",
            "recordId==2",
        ]
    )
    assert res_dup.exit_code == 1


def test_record_revisions_list_output_csv_and_json_phi_masking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify -o/--output writes PHI-masked CSV/JSON by default and honors --no-mask-phi."""
    study_cfg = StudyConfiguration(studyKey="STUDY_CLI", phiFields=["user"])
    monkeypatch.setattr(
        "imednet.integrations.enrichment.resolve_study_configuration",
        lambda sk: study_cfg if sk == "STUDY_CLI" else None,
    )
    mock_sdk = MagicMock()
    mock_sdk.record_revisions.list.return_value = _sample_revisions()
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=mock_sdk))

    csv_path = tmp_path / "revisions_masked.csv"
    res_csv = _invoke_cli(
        [
            "record-revisions",
            "list",
            "STUDY_CLI",
            "--start-date",
            "2026-01-01",
            "-o",
            str(csv_path),
        ]
    )
    assert res_csv.exit_code == 0, res_csv.stderr
    with csv_path.open("r", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    assert rows[0]["user"] == "***MASKED***"
    assert "Alice Secret" not in rows[0]["reasonForChange"]
    assert rows[0]["reasonForChange"].startswith("'=CMD")

    json_unmasked = tmp_path / "revisions_raw.json"
    res_raw = _invoke_cli(
        [
            "record-revisions",
            "list",
            "STUDY_CLI",
            "--no-mask-phi",
            "-o",
            str(json_unmasked),
        ]
    )
    assert res_raw.exit_code == 0, res_raw.stderr
    raw_items = json.loads(json_unmasked.read_text(encoding="utf-8"))
    assert raw_items[0]["user"] == "crc_alice"
    assert "Alice Secret" in raw_items[0]["reasonForChange"]


def test_build_study_sensitivity_registry_and_embedded_json_phi_in_reason_for_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify build_study_sensitivity_registry preserves exemptions and scrubs embedded JSON/URL PHI."""
    from imednet.spi.utils import build_study_sensitivity_registry, global_sensitivity_registry

    assert build_study_sensitivity_registry() is global_sensitivity_registry
    assert build_study_sensitivity_registry(set()) is global_sensitivity_registry

    custom_reg = build_study_sensitivity_registry({"mrnCode", "subject_key"})
    assert custom_reg is not global_sensitivity_registry
    assert custom_reg.is_sensitive("mrn_code") is True
    assert custom_reg.is_exempt("subject_key") is True
    assert custom_reg.is_sensitive("subject_key") is False
    assert global_sensitivity_registry.is_sensitive("mrn_code") is False

    study_cfg = StudyConfiguration(studyKey="STUDY_CLI", phiFields=["mrnCode"])
    monkeypatch.setattr(
        "imednet.integrations.enrichment.resolve_study_configuration",
        lambda sk: study_cfg if sk == "STUDY_CLI" else None,
    )
    rev = _sample_revisions()[0].model_copy(
        update={
            "reason_for_change": (
                'Audit {"variable": "mrnCode", "value": "MRN-777"} '
                "https://edc.example.org/view?patientName=Alice%20Secret"
            )
        }
    )
    mock_sdk = MagicMock()
    mock_sdk.record_revisions.list.return_value = [rev]
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", MagicMock(return_value=mock_sdk))

    res = _invoke_cli(["record-revisions", "list", "STUDY_CLI", "--format", "json"])
    assert res.exit_code == 0, res.stderr
    item = json.loads(res.stdout)["items"][0]
    assert "MRN-777" not in item["reasonForChange"]
    assert "Alice" not in item["reasonForChange"]
    assert "***MASKED***" in item["reasonForChange"]
