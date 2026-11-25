"""Unit tests for SDK export barrel callability, SinkConfig parity, and long-SQL whitelists."""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import subprocess
import sys
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import duckdb
import httpx
import pandas as pd
import pytest
import respx
from sqlalchemy import create_engine

import imednet.integrations as integrations_pkg
from imednet.integrations import export as export_mod
from imednet.integrations.export import (
    TabularSinkConfig,
    _build_tabular_sink_config,
    _LongSQLSink,
    _unpack_long_sql_rows,
    export_to_long_sql,
)
from imednet.integrations.sink_base import SinkConfig
from imednet.models.forms import Form
from imednet.models.records import Record
from imednet.models.variables import Variable
from imednet.sdk import ImednetSDK
from imednet.utils.tabular import MASKED_SENTINEL


def test_callable_export_barrel_and_lazy_spi_in_fresh_process() -> None:
    """Verify lazy SPI loading, callable export module semantics, reload resilience, and mock.patch."""
    script = """
import importlib
import inspect
import sys
import types
from unittest.mock import MagicMock, patch

import imednet.spi as spi

# Importing imednet.spi must stay lazy until spi.export or spi.export_helpers is accessed.
assert "imednet.integrations.export" not in sys.modules

export_attr = spi.export
assert "imednet.integrations.export" in sys.modules
assert callable(export_attr)
assert isinstance(export_attr, types.ModuleType)
assert inspect.ismodule(export_attr)
assert hasattr(export_attr, "export_to_csv")
assert hasattr(export_attr, "MAX_SQLITE_COLUMNS")
assert callable(spi.export_helpers.export_to_hive_parquet)

import imednet.integrations as integrations

assert integrations.export is export_attr
assert callable(integrations.export)

# Reloading integrations and spi must keep export callable and identical to sys.modules entry.
importlib.reload(integrations)
importlib.reload(spi)
assert callable(integrations.export)
assert callable(spi.export)
assert integrations.export is sys.modules["imednet.integrations.export"]
assert spi.export is integrations.export

fake_sdk = MagicMock()
with patch("imednet.integrations.export.export_to_csv", return_value="patched-ok") as patched_csv:
    assert integrations.export.export_to_csv(fake_sdk, "STUDY", "out.csv") == "patched-ok"
    patched_csv.assert_called_once_with(fake_sdk, "STUDY", "out.csv")

with patch("imednet.integrations.dispatcher.export", return_value="dispatched-ok") as patched_disp:
    cfg = spi.sink_base.SinkConfig(study_key="STUDY", mask_phi=False)
    res = spi.export("csv", fake_sdk, "STUDY", config=cfg, path="out.csv")
    assert res == "dispatched-ok"
    patched_disp.assert_called_once_with("csv", fake_sdk, "STUDY", config=cfg, path="out.csv")
"""
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    if env.get("IMEDNET_TEST_WHEELS") != "1":
        core_src = str(repo_root / "packages" / "core" / "src")
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = os.pathsep.join([core_src, existing]) if existing else core_src

    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr


def test_build_tabular_sink_config_honors_sink_config_and_strips_sanitize() -> None:
    """Verify _build_tabular_sink_config preserves SinkConfig fields without leaking sanitize."""
    sink_cfg = SinkConfig(
        study_key="OLD_STUDY",
        batch_size=250,
        max_retries=5,
        idempotent=True,
        mask_phi=False,
        quality_gate_enabled=True,
        min_schema_readiness_score=92.5,
        record_filter={"subjectKey": "SUBJ-1"},
        record_data_filter="AGE>18",
        include_deleted=True,
        incremental=True,
        stream_name="dm_stream",
        ledger_path="/tmp/custom_ledger.json",
        max_buffered_records=2000,
        as_sdtm=True,
        sdtm_domain="DM",
        standards_profile="drug",
        min_standards_score=85.0,
        naming_convention="snake_case",
        preserve_record_id=True,
        extra={"manifest_path": "/tmp/audit.manifest.jsonl"},
    )

    call_kwargs = {"config": sink_cfg, "sanitize": True, "sep": "|"}
    tab_cfg = _build_tabular_sink_config(
        "STUDY_CFG",
        call_kwargs,
        use_labels_as_columns=True,
        variable_whitelist=["AGE"],
        form_whitelist=[10],
    )

    assert isinstance(tab_cfg, TabularSinkConfig)
    assert tab_cfg.study_key == "STUDY_CFG"
    assert tab_cfg.batch_size == 250
    assert tab_cfg.max_retries == 5
    assert tab_cfg.idempotent is True
    assert tab_cfg.mask_phi is False
    assert tab_cfg.quality_gate_enabled is True
    assert tab_cfg.min_schema_readiness_score == 92.5
    assert tab_cfg.record_filter == {"subjectKey": "SUBJ-1"}
    assert tab_cfg.record_data_filter == "AGE>18"
    assert tab_cfg.include_deleted is True
    assert tab_cfg.incremental is True
    assert tab_cfg.stream_name == "dm_stream"
    assert tab_cfg.ledger_path == "/tmp/custom_ledger.json"
    assert tab_cfg.max_buffered_records == 2000
    assert tab_cfg.as_sdtm is True
    assert tab_cfg.sdtm_domain == "DM"
    assert tab_cfg.standards_profile == "drug"
    assert tab_cfg.min_standards_score == 85.0
    assert tab_cfg.naming_convention == "snake_case"
    assert tab_cfg.preserve_record_id is True
    assert tab_cfg.manifest_path == "/tmp/audit.manifest.jsonl"
    assert tab_cfg.use_labels_as_columns is True
    assert tab_cfg.variable_whitelist == ["AGE"]
    assert tab_cfg.form_whitelist == [10]
    assert tab_cfg.sanitize is True
    assert tab_cfg.pandas_kwargs == {"sep": "|"}
    assert "sanitize" not in tab_cfg.pandas_kwargs

    # Passing an existing TabularSinkConfig updates study_key and merges remaining pandas_kwargs.
    updated_cfg = _build_tabular_sink_config(
        "STUDY_CFG_2",
        {"config": tab_cfg, "sanitize": False, "encoding": "utf-8"},
    )
    assert updated_cfg.study_key == "STUDY_CFG_2"
    assert updated_cfg.preserve_record_id is True
    assert updated_cfg.manifest_path == "/tmp/audit.manifest.jsonl"
    assert updated_cfg.pandas_kwargs == {"sep": "|", "encoding": "utf-8"}
    assert "sanitize" not in updated_cfg.pandas_kwargs


def _mock_study_routes(study_key: str) -> None:
    """Register synthetic study forms, variables, intervals, and PHI-bearing records on respx."""
    respx.get(f"https://edc.prod.imednetapi.com/api/v1/edc/studies/{study_key}/forms").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "studyKey": study_key,
                        "formId": 10,
                        "formKey": "DM",
                        "formName": "Demographics",
                    }
                ]
            },
        )
    )
    respx.get(f"https://edc.prod.imednetapi.com/api/v1/edc/studies/{study_key}/variables").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "studyKey": study_key,
                        "variableId": 1,
                        "variableName": "AGE",
                        "label": "Subject Age",
                        "formId": 10,
                        "formKey": "DM",
                    },
                    {
                        "studyKey": study_key,
                        "variableId": 2,
                        "variableName": "patientName",
                        "label": "Patient Name",
                        "formId": 10,
                        "formKey": "DM",
                    },
                ]
            },
        )
    )
    respx.get(f"https://edc.prod.imednetapi.com/api/v1/edc/studies/{study_key}/intervals").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    respx.get(f"https://edc.prod.imednetapi.com/api/v1/edc/studies/{study_key}/records").mock(
        return_value=httpx.Response(
            200,
            json={
                "pagination": {"currentPage": 0, "size": 25, "totalPages": 1, "totalElements": 1},
                "data": [
                    {
                        "studyKey": study_key,
                        "recordId": 501,
                        "subjectId": 1,
                        "subjectKey": "SUBJ-01",
                        "formId": 10,
                        "formKey": "DM",
                        "deleted": False,
                        "dateModified": "2026-06-01 10:00:00",
                        "recordData": {"AGE": 44, "patientName": "Alice Example"},
                    }
                ],
            },
        )
    )


@respx.mock
def test_callable_export_forwards_sink_config_across_all_ten_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify integrations.export(...) honors SinkConfig(mask_phi=False) vs default across all 10 targets."""
    pytest.importorskip("pyarrow")
    import imednet_workflows.cached_loader as cached_loader_mod

    monkeypatch.setattr(cached_loader_mod, "DEFAULT_CACHE_DIR", tmp_path / "loader_cache")
    study_key = "STUDY_ALL_10"
    _mock_study_routes(study_key)

    manifest_csv = tmp_path / "manifests" / "csv.manifest.jsonl"
    manifest_hive = tmp_path / "manifests" / "hive.manifest.jsonl"

    unmasked_cfg = SinkConfig(
        study_key=study_key,
        mask_phi=False,
        extra={"manifest_path": str(manifest_csv)},
    )

    with ImednetSDK(
        api_key="test-key",  # pragma: allowlist secret
        security_key="test-secret",  # pragma: allowlist secret
    ) as sdk:
        csv_unmasked = tmp_path / "unmasked.csv"
        csv_default = tmp_path / "default.csv"
        integrations_pkg.export("csv", sdk, study_key, config=unmasked_cfg, path=str(csv_unmasked))
        integrations_pkg.export("csv", sdk, study_key, path=str(csv_default))

        pq_path = tmp_path / "unmasked.parquet"
        integrations_pkg.export(
            "parquet",
            sdk,
            study_key,
            config=SinkConfig(study_key=study_key, mask_phi=False),
            path=str(pq_path),
        )

        hive_dir = tmp_path / "hive_out"
        integrations_pkg.export(
            "hive_parquet",
            sdk,
            study_key,
            config=SinkConfig(
                study_key=study_key,
                mask_phi=False,
                extra={"manifest_path": str(manifest_hive)},
            ),
            base_dir=str(hive_dir),
        )

        xl_path = tmp_path / "unmasked.xlsx"
        integrations_pkg.export(
            "excel",
            sdk,
            study_key,
            config=SinkConfig(study_key=study_key, mask_phi=False),
            path=str(xl_path),
        )

        json_flat = tmp_path / "flat.json"
        json_hier = tmp_path / "hier.json"
        integrations_pkg.export(
            "json",
            sdk,
            study_key,
            config=SinkConfig(study_key=study_key, mask_phi=False),
            path=str(json_flat),
        )
        integrations_pkg.export(
            "json",
            sdk,
            study_key,
            config=SinkConfig(study_key=study_key, mask_phi=False),
            path=str(json_hier),
            hierarchical=True,
        )

        ddb_path = tmp_path / "single.duckdb"
        ddb_forms_path = tmp_path / "by_form.duckdb"
        integrations_pkg.export(
            "duckdb",
            sdk,
            study_key,
            config=SinkConfig(study_key=study_key, mask_phi=False),
            db_path=str(ddb_path),
            table_name="records",
        )
        integrations_pkg.export(
            "duckdb_by_form",
            sdk,
            study_key,
            config=SinkConfig(study_key=study_key, mask_phi=False),
            db_path=str(ddb_forms_path),
        )

        sql_single = tmp_path / "single.sqlite"
        sql_forms = tmp_path / "by_form.sqlite"
        sql_long = tmp_path / "long.sqlite"
        integrations_pkg.export(
            "sql",
            sdk,
            study_key,
            config=SinkConfig(study_key=study_key, mask_phi=False),
            table="records",
            conn_str=f"sqlite:///{sql_single}",
        )
        integrations_pkg.export(
            "sql_by_form",
            sdk,
            study_key,
            config=SinkConfig(study_key=study_key, mask_phi=False),
            conn_str=f"sqlite:///{sql_forms}",
        )
        integrations_pkg.export(
            "long_sql",
            sdk,
            study_key,
            config=SinkConfig(study_key=study_key, mask_phi=False),
            table_name="long_records",
            conn_str=f"sqlite:///{sql_long}",
        )

    assert pd.read_csv(csv_unmasked)["patientName"].iloc[0] == "Alice Example"
    assert pd.read_csv(csv_default)["patientName"].iloc[0] == MASKED_SENTINEL
    assert manifest_csv.exists()
    csv_manifest_row = json.loads(manifest_csv.read_text(encoding="utf-8").strip())
    assert csv_manifest_row["row_count"] == 1

    assert pd.read_parquet(pq_path)["patientName"].iloc[0] == "Alice Example"

    hive_files = sorted((hive_dir / f"study_key={study_key}" / "form_key=DM").rglob("*.parquet"))
    assert len(hive_files) == 1
    assert pd.read_parquet(hive_files[0])["patientName"].iloc[0] == "Alice Example"
    assert manifest_hive.exists()

    assert pd.read_excel(xl_path)["patientName"].iloc[0] == "Alice Example"
    assert json.loads(json_flat.read_text(encoding="utf-8"))[0]["patientName"] == "Alice Example"
    hier_text = json_hier.read_text(encoding="utf-8")
    hier_payload = json.loads(hier_text)
    assert isinstance(hier_payload, list) and len(hier_payload) == 1
    assert "Alice Example" in hier_text
    assert MASKED_SENTINEL not in hier_text

    with duckdb.connect(str(ddb_path), config={"threads": "1"}) as conn:
        assert conn.execute('SELECT "patientName" FROM "records"').fetchone() == ("Alice Example",)
    with duckdb.connect(str(ddb_forms_path), config={"threads": "1"}) as conn:
        assert conn.execute('SELECT "patientName" FROM "DM"').fetchone() == ("Alice Example",)

    with sqlite3.connect(sql_single) as conn:
        assert conn.execute('SELECT "patientName" FROM "records"').fetchone() == ("Alice Example",)
    with sqlite3.connect(sql_forms) as conn:
        assert conn.execute('SELECT "patientName" FROM "DM"').fetchone() == ("Alice Example",)
    with sqlite3.connect(sql_long) as conn:
        assert conn.execute(
            "SELECT value FROM long_records WHERE variable_name = 'patientName'"
        ).fetchone() == ("Alice Example",)


def _build_streaming_long_sql_sdk(study_key: str) -> MagicMock:
    """Construct a streaming SDK mock with two forms and mixed temporal values."""
    forms = [
        Form(form_id=10, form_key="DM", form_name="Demographics"),
        Form(form_id=20, form_key="VS", form_name="Vital Signs"),
    ]
    variables = [
        Variable(
            variable_id=1, variable_name="AGE", variable_type="integer", form_id=10, form_key="DM"
        ),
        Variable(
            variable_id=2, variable_name="SEX", variable_type="text", form_id=10, form_key="DM"
        ),
        Variable(
            variable_id=3, variable_name="HR", variable_type="integer", form_id=20, form_key="VS"
        ),
    ]
    records = [
        Record(
            record_id=101,
            study_key=study_key,
            subject_key="S01",
            form_id=10,
            form_key="DM",
            deleted=False,
            date_modified=dt.datetime(2026, 6, 1, 10, 0, tzinfo=dt.UTC),
            record_data={
                "AGE": 52,
                "SEX": "F",
                "VISIT_TS": dt.datetime(2026, 6, 1, 9, 30, tzinfo=dt.UTC),
            },
        ),
        Record(
            record_id=202,
            study_key=study_key,
            subject_key="S01",
            form_id=20,
            form_key="VS",
            deleted=False,
            date_modified=dt.datetime(2026, 6, 1, 10, 5, tzinfo=dt.UTC),
            record_data={
                "HR": 72,
                "VISIT_DT": dt.date(2026, 6, 1),
            },
        ),
    ]

    sdk = MagicMock(sinks=None)
    sdk.forms.list.return_value = forms
    sdk.get_forms.return_value = forms
    sdk.variables.list.return_value = variables
    sdk.get_variables.return_value = variables
    sdk.get_intervals.return_value = []
    sdk.records.list.side_effect = lambda **_kw: iter([r.model_copy(deep=True) for r in records])
    return sdk


def test_long_sql_whitelists_and_sqlite_temporal_normalization(tmp_path: Path) -> None:
    """Verify form_whitelist, variable_whitelist, naming_convention, empty whitelists, and SQLite dates."""
    study_key = "STUDY_LONG_WL"
    sdk = _build_streaming_long_sql_sdk(study_key)

    db_filtered = tmp_path / "long_filtered.sqlite"
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        export_to_long_sql(
            sdk,
            study_key,
            "clinical_long",
            conn_str=f"sqlite:///{db_filtered}",
            form_whitelist=[10],
            variable_whitelist=["age", "visit_ts"],
            naming_convention="snake_case",
        )

    with sqlite3.connect(db_filtered) as conn:
        rows = conn.execute(
            "SELECT record_id, form_id, variable_name, value "
            "FROM clinical_long ORDER BY variable_name"
        ).fetchall()
    assert rows == [
        (101, 10, "age", "52"),
        (101, 10, "visit_ts", "2026-06-01 09:30:00+00:00"),
    ]

    # Empty form_whitelist creates an empty schema table cleanly with zero rows.
    db_empty = tmp_path / "long_empty.sqlite"
    export_to_long_sql(
        sdk,
        study_key,
        "empty_long",
        conn_str=f"sqlite:///{db_empty}",
        form_whitelist=[],
        variable_whitelist=["AGE"],
    )
    with sqlite3.connect(db_empty) as conn:
        empty_rows = conn.execute("SELECT * FROM empty_long").fetchall()
        assert empty_rows == []

    # Direct unit check for _unpack_long_sql_rows and _LongSQLSink whitelist filtering.
    unpacked = _unpack_long_sql_rows(
        [
            {"recordId": 1, "formId": 10, "recordData": {"AGE": 30, "SEX": "M"}},
            {"recordId": 2, "formId": 20, "recordData": {"HR": 65}},
        ],
        mask_phi=False,
        pipeline=None,
        phi_keys=None,
        variable_whitelist=["AGE", "HR"],
        form_whitelist=[20],
    )
    assert unpacked == [
        {
            "record_id": 2,
            "form_id": 20,
            "variable_name": "HR",
            "value": 65,
            "timestamp": None,
        }
    ]

    db_sink = tmp_path / "sink_direct.sqlite"
    engine = create_engine(f"sqlite:///{db_sink}")
    sink_cfg = TabularSinkConfig(study_key="STUDY_SINK")
    with _LongSQLSink(
        "sink_tbl",
        engine,
        sink_cfg,
        chunk_size=100,
        pipeline=None,
        variable_whitelist=["HR"],
        form_whitelist=[20],
    ) as sink:
        sink.write_batch(
            [
                SimpleNamespace(
                    record_id=1,
                    form_id=10,
                    form_key="DM",
                    date_modified=None,
                    record_data={"AGE": 30},
                ),
                SimpleNamespace(
                    record_id=2,
                    form_id=20,
                    form_key="VS",
                    date_modified=None,
                    record_data={"HR": 68, "BP": 120},
                ),
            ],
            batch_id="batch-1",
        )
    with sqlite3.connect(db_sink) as conn:
        sink_rows = conn.execute(
            "SELECT record_id, form_id, variable_name, value FROM sink_tbl"
        ).fetchall()
    assert sink_rows == [(2, 20, "HR", 68)]


def test_long_sql_fallback_mapper_respects_whitelists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify non-streaming _fetch_records mock fallback in export_to_long_sql applies both whitelists."""
    mock_mapper = MagicMock()
    mock_mapper._fetch_records.return_value = [
        {
            "recordId": 11,
            "formId": 10,
            "formKey": "DM",
            "dateModified": dt.date(2026, 6, 2),
            "recordData": {"AGE": 41, "SEX": "M"},
        },
        {
            "recordId": 22,
            "formId": 20,
            "formKey": "VS",
            "dateModified": dt.date(2026, 6, 3),
            "recordData": {"HR": 80, "TEMP": 37},
        },
    ]

    monkeypatch.setattr(export_mod, "_create_record_mapper", lambda *_a, **_kw: mock_mapper)
    fake_sdk = MagicMock()
    db_fallback = tmp_path / "fallback_long.sqlite"

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        export_to_long_sql(
            fake_sdk,
            "STUDY_FB",
            "fb_long",
            conn_str=f"sqlite:///{db_fallback}",
            form_whitelist=[20],
            variable_whitelist=["HR"],
        )

    with sqlite3.connect(db_fallback) as conn:
        rows = conn.execute(
            "SELECT record_id, form_id, variable_name, value, timestamp FROM fb_long"
        ).fetchall()
    assert rows == [(22, 20, "HR", 80, "2026-06-03")]


def test_export_contract_helper_edge_cases(tmp_path: Path) -> None:
    """Verify edge cases for manifest resolution, form whitelist fallbacks, and empty partition cleanup."""
    from imednet.integrations._export_contracts import (
        append_dataframe_manifest,
        delete_superseded_hive_files,
        filter_records_by_form_whitelist,
        record_matches_form_whitelist,
        resolve_config_manifest_path,
    )

    tab_cfg = TabularSinkConfig(study_key="S", manifest_path="/tmp/direct.jsonl")
    assert resolve_config_manifest_path(tab_cfg) == "/tmp/direct.jsonl"
    assert resolve_config_manifest_path(SinkConfig(study_key="S")) is None

    # Non-integer form_id exercises fallback branch after extract_record_form_ref returns None.
    assert not record_matches_form_whitelist({"form_id": "non-int"}, {10})
    assert not record_matches_form_whitelist(SimpleNamespace(form_id="non-int"), {10})
    assert filter_records_by_form_whitelist([{"formId": 10}], []) == []

    # Empty or disabled manifest calls are safe no-ops.
    calls: list[int] = []
    append_dataframe_manifest(
        None,
        batch_id="b0",
        destination="d",
        df=[{"a": 1}],
        append_fn=lambda *_a, **_k: calls.append(1),
    )
    append_dataframe_manifest(
        str(tmp_path / "m.jsonl"),
        batch_id="b0",
        destination="d",
        df=pd.DataFrame(),
        append_fn=lambda *_a, **_k: calls.append(1),
    )
    assert calls == []

    # Nested empty subdirectory under a vacated partition is cleaned up safely.
    part_dir = tmp_path / "study_key=S" / "form_key=DM"
    nested_dir = part_dir / "batch_sub" / "leaf"
    nested_dir.mkdir(parents=True)
    old_pq = part_dir / "old_batch.parquet"
    old_pq.write_bytes(b"parquet")
    delete_superseded_hive_files({"DM": [old_pq]}, base_dir=str(tmp_path), study_key="S")
    assert not part_dir.exists()
