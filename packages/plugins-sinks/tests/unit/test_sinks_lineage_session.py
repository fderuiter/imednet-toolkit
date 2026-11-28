"""Unit tests for standalone SDTM lineage sessions and zero-copy _seq_overrides_ref in plugins-sinks."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import imednet_sinks.document as doc_mod
import imednet_sinks.graph as graph_mod
import imednet_sinks.warehouse as wh_mod
from imednet.errors import ExportBatchError
from imednet.integrations.sink_base import run_streaming_export
from imednet.models.forms import Form
from imednet.models.records import Record
from imednet.models.variables import Variable
from imednet_sinks.document import AsyncMongoDbExportSink, MongoDbExportSink, MongoDbSinkConfig
from imednet_sinks.graph import AsyncNeo4jExportSink, Neo4jExportSink, Neo4jSinkConfig
from imednet_sinks.warehouse import (
    AsyncSnowflakeExportSink,
    SnowflakeExportSink,
    SnowflakeSinkConfig,
)
from imednet_workflows.state_ledger import FileStateProvider


def _build_sdtm_sdk(records: list[Record] | None = None) -> MagicMock:
    forms = [
        Form(
            form_id=303,
            form_key="F_AE",
            form_name="Adverse Events",
            form_type="Standard",
            date_created="2026-01-01T00:00:00Z",
            date_modified="2026-01-01T00:00:00Z",
        )
    ]
    variables = [
        Variable(
            variable_id=7,
            variable_name="AETERM",
            label="AE Term",
            variable_type="text",
            form_id=303,
            form_key="F_AE",
        )
    ]
    sdk = MagicMock()
    sdk.forms.list.return_value = forms
    sdk.get_forms.return_value = forms
    sdk.variables.list.return_value = variables
    sdk.get_variables.return_value = variables
    sdk.studies.list.return_value = []
    sdk.get_studies.return_value = []
    if records is not None:
        sdk.records.list.side_effect = lambda **_kw: iter(records)
    return sdk


def _ae_rec(record_id: int, *, deleted: bool = False, minute: int = 0) -> Record:
    return Record(
        record_id=record_id,
        subject_id=1,
        subject_key="SUBJ-01",
        visit_id=1,
        form_id=303,
        form_key="F_AE",
        record_status="Complete",
        deleted=deleted,
        date_created="2026-04-01T00:00:00Z",
        date_modified=f"2026-04-10T12:{minute:02d}:00Z",
        record_data={"AETERM": f"Event {record_id}"},
    )


def test_standalone_sinks_hydrate_once_persist_deltas_and_zero_copy_seq_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Standalone Snowflake, Neo4j, and MongoDB sinks hydrate once, persist via record_sdtm_lineage, and perform 0 copies on batch 2+."""
    # Mock MongoDB
    pymongo_mod = MagicMock()
    collection = MagicMock()
    db_obj = MagicMock()
    db_obj.__getitem__.return_value = collection
    mongo_client = MagicMock()
    mongo_client.__getitem__.return_value = db_obj
    pymongo_mod.MongoClient.return_value = mongo_client
    monkeypatch.setattr(doc_mod, "_require_optional_dep", lambda *_: pymongo_mod)

    # Mock Neo4j
    neo4j_mod = MagicMock()
    neo_session = MagicMock()
    neo_cm = MagicMock()
    neo_cm.__enter__.return_value = neo_session
    neo_cm.__exit__.return_value = False
    neo_driver = MagicMock()
    neo_driver.session.return_value = neo_cm
    neo4j_mod.GraphDatabase.driver.return_value = neo_driver
    monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j_mod)

    # Mock Snowflake
    sf_mod = MagicMock()
    sf_cursor = MagicMock()
    sf_conn = MagicMock()
    sf_conn.cursor.return_value = sf_cursor
    sf_mod.connect.return_value = sf_conn

    def _sf_dep(pkg: str, _extra: str) -> Any:
        if pkg == "snowflake.connector":
            return sf_mod
        if "parquet" in pkg:
            return pq
        return pa

    monkeypatch.setattr(wh_mod, "_require_optional_dep", _sf_dep)

    for sink_kind in ("mongodb", "neo4j", "snowflake"):
        study_key = f"S_{sink_kind.upper()}"
        stream_name = f"{sink_kind}_stream"
        provider = FileStateProvider(tmp_path / f"{sink_kind}_standalone.json")
        # Seed 1 historical record in provider (with mask_phi=False so USUBJID is deterministic)
        provider.record_sdtm_lineage(
            study_key,
            stream_name,
            lineage_updates={
                "1": {
                    "study_id": study_key,
                    "domain": "AE",
                    "usubjid": "SUBJ-01",
                    "seq_col": "AESEQ",
                    "seq_val": 4,
                }
            },
            seq_watermarks={"AE:SUBJ-01": 4},
        )

        if sink_kind == "mongodb":
            cfg: Any = MongoDbSinkConfig(
                study_key=study_key,
                uri="mongodb://localhost:27017",
                database="clinical",
                collection="ae",
                as_sdtm=True,
                mask_phi=False,
                state_provider=provider,
                stream_name=stream_name,
            )
            sink_ctx: Any = MongoDbExportSink(cfg)
        elif sink_kind == "neo4j":
            cfg = Neo4jSinkConfig(
                study_key=study_key,
                uri="bolt://localhost:7687",
                auth=("neo4j", "pw"),  # pragma: allowlist secret
                as_sdtm=True,
                mask_phi=False,
                state_provider=provider,
                stream_name=stream_name,
            )
            sink_ctx = Neo4jExportSink(cfg)
        else:
            cfg = SnowflakeSinkConfig(
                study_key=study_key,
                account="acct",
                user="usr",
                password="pw",  # pragma: allowlist secret
                warehouse="WH",
                database="DB",
                schema="PUBLIC",
                stage="STG",
                table="AE",
                local_staging_dir=tmp_path / f"sf_stage_{sink_kind}",
                as_sdtm=True,
                mask_phi=False,
                state_provider=provider,
                stream_name=stream_name,
            )
            sink_ctx = SnowflakeExportSink(cfg)

        with (
            patch.object(
                provider, "get_sdtm_lineage", wraps=provider.get_sdtm_lineage
            ) as spy_get_lineage,
            patch.object(
                provider, "record_sdtm_lineage", wraps=provider.record_sdtm_lineage
            ) as spy_record_lineage,
            sink_ctx as sink,
        ):
            assert sink._active_tx is None
            # Batch 1 hydrates once
            assert sink.write_batch([_ae_rec(101, minute=1)], batch_id="b1") == 1
            first_ref = sink._seq_overrides_ref
            assert first_ref is not None

            with (
                patch.object(first_ref, "clear", wraps=first_ref.clear) as spy_clear,
                patch.object(first_ref, "update", wraps=first_ref.update) as spy_update,
            ):
                # Batches 2, 3, 4 and standalone reconcile_deleted perform 0 copies and 0 re-reads
                assert sink.write_batch([_ae_rec(102, minute=2)], batch_id="b2") == 1
                assert sink.write_batch([_ae_rec(103, minute=3)], batch_id="b3") == 1
                assert sink.write_batch([_ae_rec(104, minute=4)], batch_id="b4") == 1
                assert (
                    sink.reconcile_deleted([_ae_rec(102, deleted=True, minute=5)], batch_id="d1")
                    == 1
                )

                assert spy_clear.call_count == 0
                assert spy_update.call_count == 0
                assert sink._seq_overrides_ref is first_ref

            assert spy_get_lineage.call_count == 1
            assert spy_record_lineage.call_count == 4

        persisted = provider.get_sdtm_lineage(study_key, stream_name)
        assert persisted["sdtm_seq_watermarks"]["AE:SUBJ-01"] == 8
        assert persisted["sdtm_lineage"]["104"]["seq_val"] == 8


def test_standalone_ledger_path_caching_failed_batch_reset_and_multi_run_rebinding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Implicit ledger_path caches provider once, failed standalone batches reset cached SEQ, and mappers rebind across runs."""
    pymongo_mod = MagicMock()
    collection = MagicMock()
    db_obj = MagicMock()
    db_obj.__getitem__.return_value = collection
    mongo_client = MagicMock()
    mongo_client.__getitem__.return_value = db_obj
    pymongo_mod.MongoClient.return_value = mongo_client
    monkeypatch.setattr(doc_mod, "_require_optional_dep", lambda *_: pymongo_mod)

    # 1. Implicit ledger_path standalone calls reuse cached provider without re-hydrating
    ledger_file = tmp_path / "implicit_ledger.json"
    cfg = MongoDbSinkConfig(
        study_key="S_IMP",
        uri="mongodb://localhost:27017",
        database="clinical",
        collection="ae",
        max_retries=0,
        incremental=True,
        as_sdtm=True,
        mask_phi=False,
        ledger_path=ledger_file,
        stream_name="imp_mongo",
    )
    with MongoDbExportSink(cfg) as sink:
        assert sink.write_batch([_ae_rec(201, minute=1)], batch_id="b1") == 1
        cached_provider = getattr(sink, "_cached_ledger_provider", None)
        assert cached_provider is not None
        assert sink.write_batch([_ae_rec(202, minute=2)], batch_id="b2") == 1
        assert getattr(sink, "_cached_ledger_provider", None) is cached_provider

        # Simulate destination failure on standalone batch 3 -> cached session must reset so retry gets AESEQ=3
        collection.bulk_write.side_effect = RuntimeError("mongo write error")
        with pytest.raises(ExportBatchError):
            sink.write_batch([_ae_rec(203, minute=3)], batch_id="b3_fail")

        collection.bulk_write.side_effect = None
        collection.bulk_write.reset_mock()
        assert sink.write_batch([_ae_rec(203, minute=3)], batch_id="b3_retry") == 1

    reloaded = FileStateProvider(ledger_file).get_sdtm_lineage("S_IMP", "imp_mongo")
    assert reloaded["sdtm_lineage"]["201"]["seq_val"] == 1
    assert reloaded["sdtm_lineage"]["202"]["seq_val"] == 2
    assert reloaded["sdtm_lineage"]["203"]["seq_val"] == 3

    # 2. Reusing sink instance across two streaming runs rebinds mapper without rebuilding
    stream_provider = FileStateProvider(tmp_path / "rebind_ledger.json")
    run1_sdk = _build_sdtm_sdk([_ae_rec(301, minute=1), _ae_rec(302, minute=2)])
    stream_cfg = MongoDbSinkConfig(
        study_key="S_REBIND",
        uri="mongodb://localhost:27017",
        database="clinical",
        collection="ae",
        batch_size=1,
        incremental=True,
        as_sdtm=True,
        mask_phi=False,
        state_provider=stream_provider,
        stream_name="rebind_mongo",
    )
    reused_sink = MongoDbExportSink(stream_cfg)
    with patch.object(
        doc_mod, "build_sink_mapper", wraps=getattr(doc_mod, "build_sink_mapper")
    ) as spy_build_mapper:
        assert (
            run_streaming_export(
                run1_sdk, "S_REBIND", reused_sink, stream_cfg, stream_name="rebind_mongo"
            )
            == 2
        )
        run2_sdk = _build_sdtm_sdk([_ae_rec(303, minute=10)])
        reused_sink._connect()
        assert (
            run_streaming_export(
                run2_sdk, "S_REBIND", reused_sink, stream_cfg, stream_name="rebind_mongo"
            )
            == 1
        )
        assert spy_build_mapper.call_count == 1

    rebind_state = stream_provider.get_sdtm_lineage("S_REBIND", "rebind_mongo")
    assert rebind_state["sdtm_lineage"]["303"]["seq_val"] == 3


def test_async_standalone_sinks_hydrate_once_and_persist_deltas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Async Mongo, Neo4j, and Snowflake standalone sinks hydrate once and persist deltas."""
    pymongo_async_mod = MagicMock()
    async_coll = MagicMock()
    async_coll.bulk_write = AsyncMock()
    async_coll.delete_many = AsyncMock()
    async_db = MagicMock()
    async_db.__getitem__.return_value = async_coll
    async_client = MagicMock()
    async_client.admin = MagicMock()
    async_client.admin.command = AsyncMock(return_value={"ok": 1})
    async_client.close = AsyncMock()
    async_client.__getitem__.return_value = async_db
    pymongo_async_mod.AsyncMongoClient.return_value = async_client

    neo4j_mod = MagicMock()
    async_neo_session = MagicMock()
    async_neo_session.run = AsyncMock()
    async_neo_cm = MagicMock()
    async_neo_cm.__aenter__ = AsyncMock(return_value=async_neo_session)
    async_neo_cm.__aexit__ = AsyncMock(return_value=False)
    async_neo_driver = MagicMock()
    async_neo_driver.session.return_value = async_neo_cm
    async_neo_driver.verify_connectivity = AsyncMock()
    async_neo_driver.close = AsyncMock()
    neo4j_mod.AsyncGraphDatabase.driver.return_value = async_neo_driver
    monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j_mod)

    sf_mod = MagicMock()
    sf_cursor = MagicMock()
    sf_conn = MagicMock()
    sf_conn.cursor.return_value = sf_cursor
    sf_mod.connect.return_value = sf_conn

    def _sf_dep(pkg: str, _extra: str) -> Any:
        if pkg == "snowflake.connector":
            return sf_mod
        if "parquet" in pkg:
            return pq
        return pa

    monkeypatch.setattr(wh_mod, "_require_optional_dep", _sf_dep)
    monkeypatch.setattr(doc_mod, "_require_optional_dep", lambda *_: pymongo_async_mod)

    provider = FileStateProvider(tmp_path / "async_sinks.json")

    async def _exercise_async_sinks() -> None:
        m_cfg = MongoDbSinkConfig(
            study_key="S_AMONGO",
            uri="mongodb://localhost:27017",
            database="clinical",
            collection="ae",
            as_sdtm=True,
            mask_phi=False,
            state_provider=provider,
            stream_name="amongo",
        )
        async with AsyncMongoDbExportSink(m_cfg) as msink:
            assert await msink.write_batch([_ae_rec(401, minute=1)], batch_id="b1") == 1
            assert await msink.write_batch([_ae_rec(402, minute=2)], batch_id="b2") == 1
            assert (
                await msink.reconcile_deleted([_ae_rec(401, deleted=True, minute=3)], batch_id="d1")
                == 1
            )

        n_cfg = Neo4jSinkConfig(
            study_key="S_ANEO",
            uri="bolt://localhost:7687",
            auth=("neo4j", "pw"),  # pragma: allowlist secret
            as_sdtm=True,
            mask_phi=False,
            state_provider=provider,
            stream_name="aneo",
        )
        async with AsyncNeo4jExportSink(n_cfg) as nsink:
            assert await nsink.write_batch([_ae_rec(501, minute=1)], batch_id="b1") == 1
            assert await nsink.write_batch([_ae_rec(502, minute=2)], batch_id="b2") == 1

        s_cfg = SnowflakeSinkConfig(
            study_key="S_ASF",
            account="acct",
            user="usr",
            password="pw",  # pragma: allowlist secret
            warehouse="WH",
            database="DB",
            schema="PUBLIC",
            stage="STG",
            table="AE",
            local_staging_dir=tmp_path / "asf_stage",
            as_sdtm=True,
            mask_phi=False,
            state_provider=provider,
            stream_name="asf",
        )
        async with AsyncSnowflakeExportSink(s_cfg) as ssink:
            assert await ssink.write_batch([_ae_rec(601, minute=1)], batch_id="b1") == 1
            assert await ssink.write_batch([_ae_rec(602, minute=2)], batch_id="b2") == 1

    with patch.object(provider, "get_sdtm_lineage", wraps=provider.get_sdtm_lineage) as spy_gl:
        asyncio.run(_exercise_async_sinks())

    assert spy_gl.call_count == 3  # 1 hydration per async sink stream
    assert provider.get_sdtm_lineage("S_AMONGO", "amongo")["sdtm_lineage"]["402"]["seq_val"] == 2
    assert provider.get_sdtm_lineage("S_ANEO", "aneo")["sdtm_lineage"]["502"]["seq_val"] == 2
    assert provider.get_sdtm_lineage("S_ASF", "asf")["sdtm_lineage"]["602"]["seq_val"] == 2
