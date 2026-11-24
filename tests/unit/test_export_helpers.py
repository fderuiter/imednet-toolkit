"""Unit tests for extracted module-private helpers in imednet.integrations.export."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

import imednet.integrations.export as export_mod
from imednet.integrations.export import TabularCSVSink, TabularSinkConfig, TabularSQLSink


def test_append_jsonl_manifest_and_removed_sink_duplicates(tmp_path: Path) -> None:
    """_append_jsonl_manifest writes JSONL entries and sinks define no duplicate _append_manifest."""
    assert not hasattr(TabularCSVSink, "_append_manifest")
    assert not hasattr(TabularSQLSink, "_append_manifest")

    export_mod._append_jsonl_manifest(
        None,
        batch_id="b0",
        destination="ignored",
        row_count=1,
        checksum="abc",
    )

    manifest_file = tmp_path / "manifest.jsonl"
    export_mod._append_jsonl_manifest(
        str(manifest_file),
        batch_id="STUDY/csv/0",
        destination="out.csv",
        row_count=3,
        checksum="deadbeef",
    )

    lines = manifest_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["batch_id"] == "STUDY/csv/0"
    assert entry["destination"] == "out.csv"
    assert entry["row_count"] == 3
    assert entry["checksum"] == "deadbeef"
    assert "loaded_at" in entry


def test_resolve_non_streaming_export_df_monkeypatch_signature_parity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Monkeypatched _prepare_export_df omits mask_phi/_mapper and passes sanitize only when True."""
    sdk = MagicMock()
    expected_df = pd.DataFrame({"A": [1]})
    patched = MagicMock(return_value=expected_df)
    monkeypatch.setattr(export_mod, "_prepare_export_df", patched)

    cfg = TabularSinkConfig(study_key="STUDY", use_labels_as_columns=True, form_whitelist=[1])

    res_default = export_mod._resolve_non_streaming_export_df(sdk, "STUDY", cfg)
    assert res_default is expected_df
    patched.assert_called_once_with(
        sdk,
        "STUDY",
        use_labels_as_columns=True,
        variable_whitelist=None,
        form_whitelist=[1],
    )

    patched.reset_mock()
    res_sanitized = export_mod._resolve_non_streaming_export_df(
        sdk,
        "STUDY",
        cfg,
        sanitize=True,
    )
    assert res_sanitized is expected_df
    patched.assert_called_once_with(
        sdk,
        "STUDY",
        use_labels_as_columns=True,
        variable_whitelist=None,
        form_whitelist=[1],
        sanitize=True,
    )

    streaming_cfg = TabularSinkConfig(study_key="STUDY", incremental=True)
    assert export_mod._resolve_non_streaming_export_df(sdk, "STUDY", streaming_cfg) is None


def test_build_mock_form_df_preserves_column_coercion_parameter() -> None:
    """_build_mock_form_df coerces column names to str only when coerce_columns_to_str=True."""
    mapper = MagicMock()
    mapper._fetch_records.return_value = ["raw"]
    mapper._parse_records.return_value = ([{"1": "ok", "SSN": "123-45-6789"}], 0)
    mapper._build_dataframe.side_effect = lambda *_args: pd.DataFrame(
        [[10, "123-45-6789"]],
        columns=[1, "SSN"],
    )

    ctx = export_mod._FormExportContext(
        form=SimpleNamespace(form_id=7, form_key="F7"),
        form_id=7,
        form_key="F7",
        variables=[],
        variable_keys=[],
        label_map={},
        record_model=object(),
        contract_types={},
        stream_name="test_F7",
    )
    state = export_mod._FormExportState(
        mapper=mapper,
        pipeline=None,
        phi_keys=None,
        has_native_enrichment=False,
        use_mock_fetch=True,
    )
    cfg = TabularSinkConfig(study_key="STUDY", mask_phi=True)

    df_coerced = export_mod._build_mock_form_df(
        "STUDY",
        ctx,
        state,
        cfg,
        use_labels_as_columns=False,
        coerce_columns_to_str=True,
    )
    assert list(df_coerced.columns) == ["1", "SSN"]
    assert df_coerced["SSN"].iloc[0] == "***MASKED***"

    df_uncoerced = export_mod._build_mock_form_df(
        "STUDY",
        ctx,
        state,
        cfg,
        use_labels_as_columns=False,
        coerce_columns_to_str=False,
    )
    assert list(df_uncoerced.columns) == [1, "SSN"]
    assert df_uncoerced["SSN"].iloc[0] == "***MASKED***"


def test_enter_form_transform_sink_and_stream_callbacks() -> None:
    """_enter_form_transform_sink registers sink and _make_form_stream_callbacks routes batches."""
    inner_sink = MagicMock()
    inner_sink.write_batch.return_value = 2
    inner_sink.reconcile_deleted.return_value = 1

    mapper = MagicMock()
    mapper._parse_records.return_value = ([{"A": 1}, {"A": 2}], 0)
    mapper._build_dataframe.return_value = pd.DataFrame({"A": [1, 2]})

    ctx = export_mod._FormExportContext(
        form=SimpleNamespace(form_id=1, form_key="F_AE"),
        form_id=1,
        form_key="F_AE",
        variables=[],
        variable_keys=["A"],
        label_map={"A": "A"},
        record_model=object(),
        contract_types={},
        stream_name="sql_F_AE",
    )
    state = export_mod._FormExportState(
        mapper=mapper,
        pipeline=None,
        phi_keys=None,
        has_native_enrichment=True,
        use_mock_fetch=False,
    )
    form_cfg = TabularSinkConfig(study_key="STUDY")

    with contextlib.ExitStack() as stack:
        export_mod._enter_form_transform_sink(stack, ctx, inner_sink, form_cfg, state)
        on_active, on_voided = export_mod._make_form_stream_callbacks("STUDY")
        assert on_active(ctx, [{"recordId": 1}, {"recordId": 2}], 0) == 2
        assert on_voided(ctx, [{"recordId": 3}], 1) == 1

    assert inner_sink.write_batch.call_args.kwargs["batch_id"] == "STUDY/F_AE/0"
    assert inner_sink.reconcile_deleted.call_args.kwargs["batch_id"] == "STUDY/F_AE/voided/1"


def test_sql_by_form_validates_engine_before_fetching_forms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """export_to_sql_by_form invokes create_engine(conn_str) before sdk.forms.list."""
    from sqlalchemy.exc import ArgumentError

    sdk = MagicMock()
    mapper_cls = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(export_mod, "_record_mapper", lambda: mapper_cls)

    with pytest.raises(ArgumentError):
        export_mod.export_to_sql_by_form(sdk, "STUDY", "not-a-valid-sqlalchemy-url")

    sdk.forms.list.assert_not_called()
