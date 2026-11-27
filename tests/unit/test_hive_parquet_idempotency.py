"""Unit tests for Hive Parquet full-refresh idempotency, rollback safety, and JSONL manifests."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpx
import pandas as pd
import pytest
import respx

import imednet.integrations.parquet as parquet_mod
from imednet.integrations.parquet import export_to_hive_parquet
from imednet.integrations.parquet_engine import PyArrowDatasetPartitionedStorageEngine
from imednet.integrations.sink_base import SinkConfig
from imednet.models.forms import Form
from imednet.models.records import Record
from imednet.models.variables import Variable
from imednet.sdk import ImednetSDK


def _read_manifest_entries(manifest_path: Path) -> list[dict[str, Any]]:
    """Parse JSONL manifest records written by export_to_hive_parquet."""
    assert manifest_path.exists()
    lines = [ln for ln in manifest_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


@respx.mock
def test_hive_parquet_single_pass_router_idempotency_sibling_isolation_and_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify full idempotent=True reruns replace prior partition batches, remove vanished forms, keep sibling studies, and log JSONL manifests."""
    pytest.importorskip("pyarrow")
    import imednet_workflows.cached_loader as cached_loader_mod

    monkeypatch.setattr(cached_loader_mod, "DEFAULT_CACHE_DIR", tmp_path / "loader_cache")
    base_dir = tmp_path / "hive_lake"
    manifest_file = tmp_path / "audit" / "hive.manifest.jsonl"

    # Seed a sibling study partition to ensure STUDY_A cleanup never touches STUDY_B.
    sibling_part = base_dir / "study_key=STUDY_B" / "form_key=DM"
    sibling_part.mkdir(parents=True)
    sibling_file = sibling_part / "STUDY_B_DM_batch_0.parquet"
    pd.DataFrame([{"recordId": 999, "AGE": 77}]).to_parquet(sibling_file, index=False)

    current_forms: list[dict[str, Any]] = [
        {"studyKey": "STUDY_A", "formId": 10, "formKey": "DM", "formName": "Demographics"},
        {"studyKey": "STUDY_A", "formId": 20, "formKey": "VS", "formName": "Vitals"},
    ]
    current_records: list[dict[str, Any]] = [
        {
            "studyKey": "STUDY_A",
            "recordId": 1,
            "subjectId": 1,
            "subjectKey": "S01",
            "formId": 10,
            "formKey": "DM",
            "deleted": False,
            "dateModified": "2026-06-01 10:00:00",
            "recordData": {"AGE": 35, "patientName": "Unmasked Name"},
        },
        {
            "studyKey": "STUDY_A",
            "recordId": 2,
            "subjectId": 1,
            "subjectKey": "S01",
            "formId": 20,
            "formKey": "VS",
            "deleted": False,
            "dateModified": "2026-06-01 10:05:00",
            "recordData": {"HR": 64},
        },
    ]

    respx.get("https://edc.prod.imednetapi.com/api/v1/edc/studies/STUDY_A/forms").mock(
        side_effect=lambda _req: httpx.Response(200, json={"data": current_forms})
    )
    respx.get("https://edc.prod.imednetapi.com/api/v1/edc/studies/STUDY_A/variables").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "studyKey": "STUDY_A",
                        "variableId": 1,
                        "variableName": "AGE",
                        "label": "Age",
                        "formId": 10,
                        "formKey": "DM",
                    },
                    {
                        "studyKey": "STUDY_A",
                        "variableId": 2,
                        "variableName": "patientName",
                        "label": "Name",
                        "formId": 10,
                        "formKey": "DM",
                    },
                    {
                        "studyKey": "STUDY_A",
                        "variableId": 3,
                        "variableName": "HR",
                        "label": "Heart Rate",
                        "formId": 20,
                        "formKey": "VS",
                    },
                ]
            },
        )
    )
    respx.get("https://edc.prod.imednetapi.com/api/v1/edc/studies/STUDY_A/intervals").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    respx.get("https://edc.prod.imednetapi.com/api/v1/edc/studies/STUDY_A/records").mock(
        side_effect=lambda _req: httpx.Response(
            200,
            json={
                "pagination": {
                    "currentPage": 0,
                    "size": 25,
                    "totalPages": 1,
                    "totalElements": len(current_records),
                },
                "data": current_records,
            },
        )
    )

    cfg = SinkConfig(
        study_key="STUDY_A",
        mask_phi=False,
        extra={"manifest_path": str(manifest_file)},
    )

    with ImednetSDK(api_key="k", security_key="s") as sdk:
        export_to_hive_parquet(sdk, "STUDY_A", str(base_dir), config=cfg)

        dm_part = base_dir / "study_key=STUDY_A" / "form_key=DM"
        vs_part = base_dir / "study_key=STUDY_A" / "form_key=VS"
        run1_dm_files = sorted(dm_part.rglob("*.parquet"))
        run1_vs_files = sorted(vs_part.rglob("*.parquet"))
        assert len(run1_dm_files) == 1
        assert len(run1_vs_files) == 1
        assert pd.read_parquet(run1_dm_files[0])["patientName"].iloc[0] == "Unmasked Name"

        # Run 2: update AGE and void VS record -> old DM batch replaced and empty VS partition removed.
        current_records[:] = [
            {
                "studyKey": "STUDY_A",
                "recordId": 1,
                "subjectId": 1,
                "subjectKey": "S01",
                "formId": 10,
                "formKey": "DM",
                "deleted": False,
                "dateModified": "2026-06-02 10:00:00",
                "recordData": {"AGE": 36, "patientName": "Unmasked Name"},
            },
            {
                "studyKey": "STUDY_A",
                "recordId": 2,
                "subjectId": 1,
                "subjectKey": "S01",
                "formId": 20,
                "formKey": "VS",
                "deleted": True,
                "dateModified": "2026-06-02 10:05:00",
                "recordData": {"HR": 64},
            },
        ]
        export_to_hive_parquet(sdk, "STUDY_A", str(base_dir), config=cfg)

    run2_dm_files = sorted(dm_part.rglob("*.parquet"))
    assert len(run2_dm_files) == 1
    assert run2_dm_files[0] != run1_dm_files[0]
    assert not run1_dm_files[0].exists()
    assert int(pd.read_parquet(run2_dm_files[0])["AGE"].iloc[0]) == 36
    assert not vs_part.exists()
    assert sibling_file.exists()

    entries = _read_manifest_entries(manifest_file)
    assert len(entries) == 3  # Run 1 wrote DM + VS; Run 2 wrote DM
    for entry in entries:
        assert set(entry) >= {"batch_id", "destination", "row_count", "checksum", "loaded_at"}
        assert entry["row_count"] == 1
        assert len(entry["checksum"]) == 64


@respx.mock
def test_hive_parquet_rollback_safety_preserves_prior_batches_on_mid_run_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify pre-existing Parquet files remain intact when a mid-run error aborts export_to_hive_parquet."""
    pytest.importorskip("pyarrow")
    import imednet_workflows.cached_loader as cached_loader_mod

    monkeypatch.setattr(cached_loader_mod, "DEFAULT_CACHE_DIR", tmp_path / "loader_cache")
    base_dir = tmp_path / "hive_rollback"

    respx.get("https://edc.prod.imednetapi.com/api/v1/edc/studies/STUDY_RB/forms").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "studyKey": "STUDY_RB",
                        "formId": 10,
                        "formKey": "DM",
                        "formName": "Demographics",
                    }
                ]
            },
        )
    )
    respx.get("https://edc.prod.imednetapi.com/api/v1/edc/studies/STUDY_RB/variables").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "studyKey": "STUDY_RB",
                        "variableId": 1,
                        "variableName": "AGE",
                        "label": "Age",
                        "formId": 10,
                        "formKey": "DM",
                    }
                ]
            },
        )
    )
    respx.get("https://edc.prod.imednetapi.com/api/v1/edc/studies/STUDY_RB/intervals").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    respx.get("https://edc.prod.imednetapi.com/api/v1/edc/studies/STUDY_RB/records").mock(
        return_value=httpx.Response(
            200,
            json={
                "pagination": {"currentPage": 0, "size": 25, "totalPages": 1, "totalElements": 1},
                "data": [
                    {
                        "studyKey": "STUDY_RB",
                        "recordId": 1,
                        "subjectId": 1,
                        "subjectKey": "S01",
                        "formId": 10,
                        "formKey": "DM",
                        "deleted": False,
                        "recordData": {"AGE": 50},
                    }
                ],
            },
        )
    )

    with ImednetSDK(api_key="k", security_key="s") as sdk:
        export_to_hive_parquet(sdk, "STUDY_RB", str(base_dir))
        dm_part = base_dir / "study_key=STUDY_RB" / "form_key=DM"
        initial_files = sorted(dm_part.rglob("*.parquet"))
        assert len(initial_files) == 1

        def _boom(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("simulated disk error during batch write")

        monkeypatch.setattr(PyArrowDatasetPartitionedStorageEngine, "write_form_table", _boom)
        with pytest.raises(RuntimeError, match="simulated disk error"):
            export_to_hive_parquet(sdk, "STUDY_RB", str(base_dir))

    assert initial_files[0].exists()
    assert int(pd.read_parquet(initial_files[0])["AGE"].iloc[0]) == 50


def test_hive_parquet_streaming_and_sdtm_full_reruns_preserve_record_id_contract(
    tmp_path: Path,
) -> None:
    """Verify Branch 2 (quality_gate_enabled + as_sdtm) replaces prior batches and honors preserve_record_id."""
    pytest.importorskip("pyarrow")
    study_key = "STUDY_SDTM_HIVE"
    forms = [Form(form_id=10, form_key="DM", form_name="Demographics", form_type="Enrollment")]
    variables = [
        Variable(
            variable_id=1, variable_name="AGE", variable_type="integer", form_id=10, form_key="DM"
        )
    ]
    current_age = [41]

    sdk = MagicMock(sinks=None)
    sdk.forms.list.return_value = forms
    sdk.get_forms.return_value = forms
    sdk.variables.list.return_value = variables
    sdk.get_variables.return_value = variables
    sdk.get_intervals.return_value = []
    sdk.records.list.side_effect = lambda **_kw: iter(
        [
            Record(
                record_id=77,
                study_key=study_key,
                subject_key="S01",
                form_id=10,
                form_key="DM",
                deleted=False,
                date_modified=dt.datetime(2026, 6, 1, 12, 0, tzinfo=dt.UTC),
                record_data={"AGE": current_age[0]},
            )
        ]
    )

    sdtm_dir = tmp_path / "hive_sdtm"
    manifest_sdtm = tmp_path / "sdtm.manifest.jsonl"

    export_to_hive_parquet(
        sdk,
        study_key,
        str(sdtm_dir),
        as_sdtm=True,
        quality_gate_enabled=True,
        manifest_path=str(manifest_sdtm),
    )
    dm_part = sdtm_dir / f"study_key={study_key}" / "form_key=DM"
    files_r1 = sorted(dm_part.rglob("*.parquet"))
    assert len(files_r1) == 1
    df_r1 = pd.read_parquet(files_r1[0])
    assert "RECORD_ID" not in df_r1.columns

    # Rerun with updated AGE and preserve_record_id=True via SinkConfig
    current_age[0] = 42
    export_to_hive_parquet(
        sdk,
        study_key,
        str(sdtm_dir),
        config=SinkConfig(
            study_key=study_key,
            as_sdtm=True,
            preserve_record_id=True,
            quality_gate_enabled=True,
            extra={"manifest_path": str(manifest_sdtm)},
        ),
    )
    files_r2 = sorted(dm_part.rglob("*.parquet"))
    assert len(files_r2) == 1
    assert not files_r1[0].exists()
    df_r2 = pd.read_parquet(files_r2[0])
    assert "RECORD_ID" in df_r2.columns
    assert "recordId" not in df_r2.columns
    assert int(df_r2["AGE"].iloc[0]) == 42
    assert len(_read_manifest_entries(manifest_sdtm)) == 2


def test_hive_parquet_iter_parsed_rows_and_fetch_records_fallbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify Branch 3 (_iter_parsed_rows) and Branch 4 (_fetch_records) idempotent reruns vs append mode."""
    pytest.importorskip("pyarrow")
    sdk = MagicMock()
    sdk.forms.list.return_value = [SimpleNamespace(form_id=10, form_key="DM")]
    sdk.variables.list.return_value = [
        SimpleNamespace(form_id=10, variable_name="AGE", label="Age")
    ]

    # Branch 3: mapper with _iter_records + _iter_parsed_rows (without _parse_record)
    iter_val = [21]

    class IterRowsMapper:
        def __init__(self, _sdk: Any) -> None:
            pass

        def _build_record_model(self, vkeys: list[str], lmap: dict[str, str]) -> Any:
            return (vkeys, lmap)

        def _iter_records(
            self, _study_key: str, extra_filters: dict[str, Any] | None = None
        ) -> list[dict[str, Any]]:
            _ = extra_filters
            return [{"recordId": 1, "AGE": iter_val[0]}]

        def _iter_parsed_rows(self, records: list[dict[str, Any]], _model: Any) -> Any:
            yield records, len(records)

        def _build_dataframe(
            self,
            rows: list[dict[str, Any]],
            _vkeys: list[str],
            _lmap: dict[str, str],
            _use_labels: bool,
        ) -> pd.DataFrame:
            return pd.DataFrame(rows)

    monkeypatch.setattr(parquet_mod, "_record_mapper", lambda: IterRowsMapper)
    b3_dir = tmp_path / "hive_b3"
    b3_manifest = tmp_path / "b3.manifest.jsonl"

    export_to_hive_parquet(sdk, "STUDY_B3", str(b3_dir), manifest_path=str(b3_manifest))
    iter_val[0] = 22
    export_to_hive_parquet(sdk, "STUDY_B3", str(b3_dir), manifest_path=str(b3_manifest))

    b3_files = sorted((b3_dir / "study_key=STUDY_B3" / "form_key=DM").rglob("*.parquet"))
    assert len(b3_files) == 1
    assert int(pd.read_parquet(b3_files[0])["AGE"].iloc[0]) == 22
    assert len(_read_manifest_entries(b3_manifest)) == 2

    # Branch 4: mapper with _fetch_records only (idempotent=True replaces, idempotent=False appends)
    fetch_val = [31]

    class FetchOnlyMapper:
        def __init__(self, _sdk: Any) -> None:
            pass

        def _build_record_model(self, vkeys: list[str], lmap: dict[str, str]) -> Any:
            return (vkeys, lmap)

        def _fetch_records(
            self, _study_key: str, extra_filters: dict[str, Any] | None = None
        ) -> list[dict[str, Any]]:
            _ = extra_filters
            return [{"recordId": 1, "AGE": fetch_val[0]}]

        def _parse_records(
            self, records: list[dict[str, Any]], _model: Any
        ) -> tuple[list[dict[str, Any]], int]:
            return records, len(records)

        def _build_dataframe(
            self,
            rows: list[dict[str, Any]],
            _vkeys: list[str],
            _lmap: dict[str, str],
            _use_labels: bool,
        ) -> pd.DataFrame:
            return pd.DataFrame(rows)

    monkeypatch.setattr(parquet_mod, "_record_mapper", lambda: FetchOnlyMapper)
    b4_dir = tmp_path / "hive_b4"
    b4_manifest = tmp_path / "b4.manifest.jsonl"

    export_to_hive_parquet(
        sdk, "STUDY_B4", str(b4_dir), idempotent=True, manifest_path=str(b4_manifest)
    )
    fetch_val[0] = 32
    export_to_hive_parquet(
        sdk, "STUDY_B4", str(b4_dir), idempotent=True, manifest_path=str(b4_manifest)
    )

    b4_part = b4_dir / "study_key=STUDY_B4" / "form_key=DM"
    assert len(list(b4_part.rglob("*.parquet"))) == 1
    assert int(pd.read_parquet(next(b4_part.rglob("*.parquet")))["AGE"].iloc[0]) == 32

    # idempotent=False retains prior files and appends a second batch file
    fetch_val[0] = 33
    export_to_hive_parquet(
        sdk, "STUDY_B4", str(b4_dir), idempotent=False, manifest_path=str(b4_manifest)
    )
    assert len(list(b4_part.rglob("*.parquet"))) == 2
    assert len(_read_manifest_entries(b4_manifest)) == 3
