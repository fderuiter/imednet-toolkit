"""Unit tests for RecordMapper SDTM domain filtering, empty-Study scoring, and StateLedger SDTM lineage."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from imednet import ImednetSDK
from imednet import cli as cli_mod
from imednet.models.forms import Form
from imednet.models.standards import (
    PROFILE_REGISTRY,
    DeviceSafetyProfile,
    DrugSafetyProfile,
    GeneralClinicalProfile,
    StandardsReadinessValidator,
    resolve_standards_profile,
)
from imednet.models.variables import Variable
from imednet_workflows.record_mapper import RecordMapper
from imednet_workflows.state_ledger import ExtractionStateLedger, FileStateProvider


def test_backed_profiles_registered_and_empty_study_scores_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """PROFILE_REGISTRY registers only backed profiles, and empty studies score 0.0 (is_ready=False)."""
    assert PROFILE_REGISTRY.list_profiles() == [
        "device",
        "drug",
        "general",
    ]
    assert isinstance(PROFILE_REGISTRY.get("device"), DeviceSafetyProfile)
    assert isinstance(PROFILE_REGISTRY.get("general"), GeneralClinicalProfile)
    for unknown_name in ("biologic", "vaccine", "unknown"):
        with pytest.raises(ValueError, match="Unknown standards_profile"):
            resolve_standards_profile(unknown_name)

    validator = StandardsReadinessValidator(profile=DrugSafetyProfile())
    report = validator.score_records(records_by_domain={})
    assert report.checked_records == 0
    assert report.score == 0.0
    assert report.is_ready is False
    assert any("readiness score is 0.0" in w for w in report.warnings)

    empty_sdk = MagicMock()
    empty_sdk.forms.list.return_value = [
        Form(form_id=1, form_key="F_AE", form_name="Adverse Events")
    ]
    empty_sdk.variables.list.return_value = [
        Variable(variable_id=1, form_id=1, form_key="F_AE", variable_name="aeTerm")
    ]
    empty_sdk.records.list.return_value = iter([])

    with (
        patch("imednet.cli.utils.context.get_sdk", return_value=empty_sdk),
        pytest.raises(SystemExit) as exc_info,
    ):
        cli_mod.app(
            [
                "workflows",
                "validate-standards",
                "EMPTY_STUDY",
                "--profile",
                "drug",
                "--format",
                "json",
            ]
        )
    assert exc_info.value.code == 1
    out, _ = capsys.readouterr()
    envelope = json.loads(out)
    assert envelope["schemaVersion"] == "1.0"
    item = envelope["items"][0]
    assert item["score"] == 0.0
    assert item["is_ready"] is False
    assert item["passed"] is False
    assert any("readiness score is 0.0" in w for w in item["warnings"])


def test_record_mapper_sdtm_dataframe_filters_forms_and_infers_empty_schema() -> None:
    """RecordMapper.sdtm_dataframe filters forms by sdtm_domain and infers empty schemas."""
    from imednet.simulation import SimulationState, SimulationTransport

    sim_sdk = ImednetSDK(
        simulation_mode=True,
        transport=SimulationTransport(state=SimulationState()),
    )
    mapper = RecordMapper(sim_sdk)

    vs_df = mapper.sdtm_dataframe("STUDY", domain="VS")
    assert len(vs_df) == 1
    assert list(vs_df["DOMAIN"]) == ["VS"]
    assert list(vs_df["VSSEQ"]) == [1]
    assert "AESEQ" not in vs_df.columns
    assert "DMSEQ" not in vs_df.columns

    dm_df = mapper.sdtm_dataframe("STUDY", domain="DM")
    assert len(dm_df) == 1
    assert list(dm_df["DOMAIN"]) == ["DM"]
    assert "DMSEQ" not in dm_df.columns
    assert list(dm_df["BRTHDTC"]) == ["1981"]

    # Empty study with F_DM form infers DM columns without DMSEQ or AESEQ
    empty_sdk = MagicMock()
    empty_sdk.forms.list.return_value = [
        Form(form_id=301, form_key="F_DM", form_name="Demographics", form_type="Enrollment")
    ]
    empty_sdk.get_forms.return_value = empty_sdk.forms.list.return_value
    empty_sdk.variables.list.return_value = []
    empty_sdk.get_variables.return_value = []
    empty_sdk.records.list.return_value = iter([])

    empty_mapper = RecordMapper(empty_sdk)
    empty_df = empty_mapper.sdtm_dataframe("EMPTY", domain="DM")
    assert empty_df.empty
    assert {"STUDYID", "DOMAIN", "USUBJID"}.issubset(set(empty_df.columns))
    assert "DMSEQ" not in empty_df.columns
    assert "AESEQ" not in empty_df.columns


def test_state_ledger_sdtm_lineage_and_monotonic_watermarks_persistence(
    tmp_path: Path,
) -> None:
    """ExtractionStateLedger & FileStateProvider persist SDTM lineage and monotonic watermarks."""
    ledger_file = tmp_path / "lineage_state.json"
    provider = FileStateProvider(ledger_file)
    t1 = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)

    with provider.transaction("STUDY_L", "parquet") as tx:
        tx["new_timestamp"] = t1
        tx["records_processed"] = 2
        tx["metadata"] = {
            "sdtm_lineage": {
                "101": {
                    "study_id": "STUDY_L",
                    "domain": "DM",
                    "usubjid": "S01",
                    "seq_col": None,
                    "seq_val": None,
                },
                "102": {
                    "study_id": "STUDY_L",
                    "domain": "AE",
                    "usubjid": "S01",
                    "seq_col": "AESEQ",
                    "seq_val": 4,
                },
            },
            "sdtm_seq_watermarks": {"AE|S01": 4},
        }

    reloaded = ExtractionStateLedger(ledger_path=ledger_file)
    persisted = reloaded.get_sdtm_lineage("STUDY_L", "parquet")
    lineage = persisted["sdtm_lineage"]
    assert lineage["101"]["seq_col"] is None
    assert lineage["102"]["seq_val"] == 4
    assert persisted["sdtm_seq_watermarks"] == {"AE|S01": 4}

    # Lower watermark values never regress monotonic sequence high-water marks
    reloaded.record_sdtm_lineage("STUDY_L", "parquet", seq_watermarks={"AE|S01": 2})
    assert reloaded.get_sdtm_lineage("STUDY_L", "parquet")["sdtm_seq_watermarks"] == {"AE|S01": 4}
    reloaded.record_sdtm_lineage("STUDY_L", "parquet", seq_watermarks={"AE|S01": 7})
    assert reloaded.get_sdtm_lineage("STUDY_L", "parquet")["sdtm_seq_watermarks"] == {"AE|S01": 7}

    # Failed transaction does not overwrite committed lineage
    with pytest.raises(RuntimeError, match="boom"):
        with reloaded.transaction("STUDY_L", "parquet") as tx2:
            tx2["metadata"] = {"sdtm_seq_watermarks": {"AE|S01": 99}}
            raise RuntimeError("boom")
    assert reloaded.get_sdtm_lineage("STUDY_L", "parquet")["sdtm_seq_watermarks"] == {"AE|S01": 7}
