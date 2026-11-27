"""Unit tests for native async database sinks (MongoDB, Neo4j, Snowflake) without worker threads."""

from __future__ import annotations

import asyncio
import concurrent.futures
from datetime import datetime, timezone
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from imednet import AsyncImednetSDK, ImednetSDK, SimulationState, SimulationTransport
from imednet.errors import ExportConfigurationError
from imednet.integrations.dispatcher import async_export
from imednet.models.records import Record
from imednet_sinks import (
    AsyncMongoDbExportSink,
    AsyncNeo4jExportSink,
    AsyncSinksNamespace,
    AsyncSnowflakeExportSink,
    MongoDbExportSink,
    MongoDbSinkConfig,
    Neo4jExportSink,
    Neo4jSinkConfig,
    SnowflakeSinkConfig,
    async_export_to_mongodb,
    async_export_to_neo4j,
    async_export_to_snowflake,
    export_to_mongodb,
    export_to_neo4j,
    export_to_snowflake,
)


def _fake_async_pymongo(fail_connect: bool = False) -> tuple[ModuleType, MagicMock, MagicMock]:
    pymongo = ModuleType("pymongo")

    class _UpdateOne:
        def __init__(self, flt: dict[str, Any], upd: dict[str, Any], upsert: bool = False) -> None:
            self.filter = flt
            self.update = upd
            self.upsert = upsert

    pymongo.UpdateOne = _UpdateOne  # type: ignore[attr-defined]

    collection = MagicMock()
    collection.bulk_write = AsyncMock(return_value=MagicMock(upserted_count=2, modified_count=0))
    collection.insert_many = AsyncMock(return_value=MagicMock(inserted_ids=["a", "b"]))
    collection.delete_many = AsyncMock(return_value=MagicMock(deleted_count=1))

    db = MagicMock(__getitem__=MagicMock(return_value=collection))
    client = MagicMock(__getitem__=MagicMock(return_value=db))
    client.admin = MagicMock()
    if fail_connect:
        client.admin.command = AsyncMock(side_effect=RuntimeError("ping failed"))
    else:
        client.admin.command = AsyncMock(return_value={"ok": 1})
    client.close = AsyncMock()

    pymongo.AsyncMongoClient = MagicMock(return_value=client)  # type: ignore[attr-defined]

    # Sync MongoClient for parity comparisons
    sync_collection = MagicMock()
    sync_collection.bulk_write = MagicMock(return_value=MagicMock(upserted_count=2))
    sync_collection.delete_many = MagicMock(return_value=MagicMock(deleted_count=1))
    sync_db = MagicMock(__getitem__=MagicMock(return_value=sync_collection))
    sync_client = MagicMock(__getitem__=MagicMock(return_value=sync_db))
    sync_client.admin = MagicMock()
    sync_client.admin.command = MagicMock(return_value={"ok": 1})
    pymongo.MongoClient = MagicMock(return_value=sync_client)  # type: ignore[attr-defined]
    pymongo._sync_collection = sync_collection  # type: ignore[attr-defined]

    return pymongo, client, collection


def _fake_async_neo4j(fail_connect: bool = False) -> tuple[ModuleType, MagicMock, MagicMock]:
    neo4j = ModuleType("neo4j")

    result_obj = MagicMock()
    result_obj.consume = AsyncMock()

    session = MagicMock()
    session.run = AsyncMock(return_value=result_obj)
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    driver = MagicMock()
    if fail_connect:
        driver.verify_connectivity = AsyncMock(side_effect=RuntimeError("bolt unreachable"))
    else:
        driver.verify_connectivity = AsyncMock()
    driver.session = MagicMock(return_value=session_cm)
    driver.close = AsyncMock()

    neo4j.AsyncGraphDatabase = MagicMock()  # type: ignore[attr-defined]
    neo4j.AsyncGraphDatabase.driver = MagicMock(return_value=driver)  # type: ignore[attr-defined]

    # Sync GraphDatabase for parity comparisons
    sync_session = MagicMock()
    sync_session_cm = MagicMock()
    sync_session_cm.__enter__ = MagicMock(return_value=sync_session)
    sync_session_cm.__exit__ = MagicMock(return_value=False)
    sync_driver = MagicMock()
    sync_driver.session = MagicMock(return_value=sync_session_cm)
    neo4j.GraphDatabase = MagicMock()  # type: ignore[attr-defined]
    neo4j.GraphDatabase.driver = MagicMock(return_value=sync_driver)  # type: ignore[attr-defined]
    neo4j._sync_session = sync_session  # type: ignore[attr-defined]

    return neo4j, driver, session


def _sample_records() -> tuple[list[Record], list[Record]]:
    ts = datetime(2026, 6, 1, 8, 30, tzinfo=timezone.utc).isoformat()
    active = [
        Record(
            study_key="STUDY1",
            record_id=10,
            form_id=1,
            form_key="F_VS",
            subject_key="SUBJ-01",
            visit_id=1,
            date_modified=ts,
            deleted=False,
            record_data={"patient_name": "Alice Secret", "V_WEIGHT": 64.5},
        ),
        Record(
            study_key="STUDY1",
            record_id=11,
            form_id=1,
            form_key="F_VS",
            subject_key="SUBJ-01",
            visit_id=0,
            date_modified=ts,
            deleted=False,
            record_data={"patient_name": "Bob Secret", "V_WEIGHT": 81.2},
        ),
    ]
    voided = [
        Record(
            study_key="STUDY1",
            record_id=12,
            form_id=1,
            form_key="F_VS",
            subject_key="SUBJ-01",
            visit_id=1,
            date_modified=ts,
            deleted=True,
            record_data={"patient_name": "Voided Secret", "V_WEIGHT": 70.0},
        )
    ]
    return active, voided


@pytest.mark.asyncio
async def test_async_mongodb_sink_zero_thread_pool_and_parity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AsyncMongoDbExportSink uses native AsyncMongoClient with zero ThreadPoolExecutor calls and matches sync parity."""
    import imednet_sinks.document as doc_mod

    pymongo, client, collection = _fake_async_pymongo()
    monkeypatch.setattr(doc_mod, "_require_optional_dep", lambda *_: pymongo)

    # Forbid ANY worker-thread offloading during native AsyncMongoDbExportSink execution!
    def _forbid_threads(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("AsyncMongoDbExportSink must not use ThreadPoolExecutor or asyncio.to_thread")

    monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor", _forbid_threads)
    monkeypatch.setattr(asyncio, "to_thread", _forbid_threads)

    active, voided = _sample_records()
    cfg = MongoDbSinkConfig(
        study_key="STUDY1",
        uri="mongodb://user:topsecret@localhost:27017",  # pragma: allowlist secret
        database="clinical",
        collection="records",
        mask_phi=True,
        as_sdtm=True,
        sdtm_domain="VS",
    )

    async with AsyncMongoDbExportSink(cfg) as async_sink:
        written = await async_sink.write_batch(active, batch_id="STUDY1/F_VS/0")
        reconciled = await async_sink.reconcile_deleted(voided, batch_id="STUDY1/F_VS/del")
        await async_sink.flush()

    assert written == 2
    assert reconciled == 1
    client.admin.command.assert_awaited_once_with("ping")
    collection.bulk_write.assert_awaited_once()
    collection.delete_many.assert_awaited_once()
    client.close.assert_awaited_once()

    async_ops = collection.bulk_write.await_args.args[0]
    async_docs = [op.update["$set"] for op in async_ops]

    with MongoDbExportSink(cfg) as sync_sink:
        sync_sink.write_batch(active, batch_id="STUDY1/F_VS/0")
        sync_sink.reconcile_deleted(voided, batch_id="STUDY1/F_VS/del")

    sync_ops = pymongo._sync_collection.bulk_write.call_args.args[0]
    sync_docs = [op.update["$set"] for op in sync_ops]

    # Exact parity on PHI masking, SDTM lineage keys, and 21 CFR Part 11 delete filter
    assert async_docs == sync_docs
    for doc in async_docs:
        assert "Secret" not in str(doc)
        assert doc["STUDYID"] == "STUDY1"
        assert doc["DOMAIN"] == "VS"
    assert (
        collection.delete_many.await_args.args[0]
        == pymongo._sync_collection.delete_many.call_args.args[0]
    )


@pytest.mark.asyncio
async def test_async_mongodb_sink_connect_error_redacts_uri_and_closes_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AsyncMongoDbExportSink redacts credentials on connection errors and closes client."""
    import imednet_sinks.document as doc_mod

    pymongo, client, _ = _fake_async_pymongo(fail_connect=True)
    monkeypatch.setattr(doc_mod, "_require_optional_dep", lambda *_: pymongo)

    cfg = MongoDbSinkConfig(
        study_key="STUDY1",
        uri="mongodb://admin:supersecret@localhost:27017",  # pragma: allowlist secret
        database="db",
        collection="col",
    )
    with pytest.raises(ExportConfigurationError) as exc_info:
        async with AsyncMongoDbExportSink(cfg):
            pass
    assert "supersecret" not in str(exc_info.value)
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_neo4j_sink_zero_thread_pool_and_parity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AsyncNeo4jExportSink uses native AsyncGraphDatabase with zero ThreadPoolExecutor calls and matches sync parity."""
    import imednet_sinks.graph as graph_mod

    neo4j, driver, session = _fake_async_neo4j()
    monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j)

    def _forbid_threads(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("AsyncNeo4jExportSink must not use ThreadPoolExecutor or asyncio.to_thread")

    monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor", _forbid_threads)
    monkeypatch.setattr(asyncio, "to_thread", _forbid_threads)

    active, voided = _sample_records()
    cfg = Neo4jSinkConfig(
        study_key="STUDY1",
        uri="bolt://localhost:7687",
        auth=("neo4j", "topsecret"),  # pragma: allowlist secret
        mask_phi=True,
    )

    async with AsyncNeo4jExportSink(cfg) as async_sink:
        written = await async_sink.write_batch(active, batch_id="STUDY1/F_VS/0")
        reconciled = await async_sink.reconcile_deleted(voided, batch_id="STUDY1/F_VS/del")
        await async_sink.flush()

    assert written == 2
    assert reconciled == 1
    driver.verify_connectivity.assert_awaited_once()
    assert session.run.await_count == 2
    driver.close.assert_awaited_once()

    async_cypher, async_kwargs = (
        session.run.await_args_list[0].args[0],
        session.run.await_args_list[0].kwargs,
    )

    with Neo4jExportSink(cfg) as sync_sink:
        sync_sink.write_batch(active, batch_id="STUDY1/F_VS/0")
        sync_sink.reconcile_deleted(voided, batch_id="STUDY1/F_VS/del")

    sync_cypher, sync_kwargs = (
        neo4j._sync_session.run.call_args_list[0].args[0],
        neo4j._sync_session.run.call_args_list[0].kwargs,
    )
    assert async_cypher == sync_cypher
    assert async_kwargs["rows"] == sync_kwargs["rows"]
    for row in async_kwargs["rows"]:
        assert "***MASKED***" in row["record_data"]
        assert "Secret" not in row["record_data"]
    # Second record had visit_id == 0 -> normalized to None for unscheduled visit link
    assert async_kwargs["rows"][1]["visit_id"] is None


@pytest.mark.asyncio
async def test_async_neo4j_sink_connect_error_redacts_uri_and_closes_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AsyncNeo4jExportSink redacts credentials on connection errors and closes driver."""
    import imednet_sinks.graph as graph_mod

    neo4j, driver, _ = _fake_async_neo4j(fail_connect=True)
    monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j)

    cfg = Neo4jSinkConfig(
        study_key="STUDY1",
        uri="bolt://neo4j:supersecret@localhost:7687",  # pragma: allowlist secret
        auth=("neo4j", "supersecret"),  # pragma: allowlist secret
    )
    with pytest.raises(ExportConfigurationError) as exc_info:
        async with AsyncNeo4jExportSink(cfg):
            pass
    assert "supersecret" not in str(exc_info.value)
    driver.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_snowflake_sink_stages_parquet_merges_and_reconciles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AsyncSnowflakeExportSink stages Parquet asynchronously, merges rows, masks PHI, and reconciles deletes."""
    import imednet_sinks.warehouse as wh_mod

    sf = ModuleType("snowflake.connector")
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    conn.cursor.return_value = cursor
    sf.connect = MagicMock(return_value=conn)  # type: ignore[attr-defined]

    pa = ModuleType("pyarrow")
    pq = ModuleType("pyarrow.parquet")
    staged_rows_box: list[list[dict[str, Any]]] = []

    def _from_pylist(rows: list[dict[str, Any]]) -> Any:
        staged_rows_box.append(rows)
        tbl = MagicMock()
        tbl.column_names = list(rows[0].keys()) if rows else []
        return tbl

    pa.Table = MagicMock()  # type: ignore[attr-defined]
    pa.Table.from_pylist = MagicMock(side_effect=_from_pylist)
    pq.write_table = MagicMock()  # type: ignore[attr-defined]

    def _fake_dep(name: str, _extra: str) -> Any:
        if name == "snowflake.connector":
            return sf
        if name == "pyarrow":
            return pa
        if name == "pyarrow.parquet":
            return pq
        raise AssertionError(f"Unexpected dep {name}")

    monkeypatch.setattr(wh_mod, "_require_optional_dep", _fake_dep)

    active, voided = _sample_records()
    cfg = SnowflakeSinkConfig(
        study_key="STUDY1",
        account="acct",
        user="svc",
        password="secret",  # pragma: allowlist secret
        database="CLINICAL",
        schema="RAW",
        warehouse="WH",
        stage="STG",
        table="RECORDS",
        mask_phi=True,
    )

    async with AsyncSnowflakeExportSink(cfg) as sink:
        written = await sink.write_batch(active, batch_id="STUDY1/F_VS/0")
        reconciled = await sink.reconcile_deleted(voided, batch_id="STUDY1/F_VS/del")
        await sink.flush()

    assert written == 2
    assert reconciled == 1
    pq.write_table.assert_called_once()
    assert len(staged_rows_box) == 1
    for row in staged_rows_box[0]:
        assert row["patient_name"] == "***MASKED***"
    executed_sql = "\n".join(str(call.args[0]) for call in cursor.execute.call_args_list)
    assert "DELETE FROM" in executed_sql
    assert "PUT " in executed_sql
    assert "MERGE INTO" in executed_sql
    conn.close.assert_called_once()


@pytest.mark.asyncio
async def test_async_sdk_sinks_end_to_end_streaming_and_facade_type_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AsyncImednetSDK.sinks and async_export stream SimulationTransport records into native async sinks."""
    import imednet_sinks.document as doc_mod
    import imednet_sinks.graph as graph_mod

    pymongo, _m_client, m_col = _fake_async_pymongo()
    neo4j, _n_driver, n_session = _fake_async_neo4j()
    monkeypatch.setattr(doc_mod, "_require_optional_dep", lambda *_: pymongo)
    monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j)

    state = SimulationState()
    async with AsyncImednetSDK(
        simulation_mode=True,
        transport=SimulationTransport(state),
    ) as async_sdk:
        assert isinstance(async_sdk.sinks, AsyncSinksNamespace)

        # Sync export helpers reject AsyncImednetSDK with TypeError
        with pytest.raises(TypeError, match="async_export_to_mongodb"):
            export_to_mongodb(async_sdk, "STUDY", "mongodb://localhost:27017", "db", "col")  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="async_export_to_neo4j"):
            export_to_neo4j(async_sdk, "STUDY", "bolt://localhost:7687", ("neo4j", "p"))  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="async_export_to_snowflake"):
            export_to_snowflake(
                async_sdk,  # type: ignore[arg-type]
                "STUDY",
                config=SnowflakeSinkConfig(study_key="STUDY"),
            )

        n_mongo = await async_sdk.sinks.export_to_mongodb(
            "STUDY",
            "mongodb://localhost:27017",
            "clinical",
            "records",
        )
        n_neo = await async_sdk.sinks.export_to_neo4j(
            "STUDY",
            "bolt://localhost:7687",
            ("neo4j", "pass"),
        )
        # Also test async_export dispatcher resolving imednet.async_sinks entry points!
        disp_res = await async_export(
            "mongodb",
            async_sdk,
            "STUDY",
            config=MongoDbSinkConfig(
                study_key="STUDY",
                uri="mongodb://localhost:27017",
                database="clinical",
                collection="records_disp",
            ),
        )
        assert n_mongo > 0
        assert n_neo > 0
        assert disp_res == n_mongo
        assert m_col.bulk_write.await_count >= 2
        assert n_session.run.await_count >= 1

    with ImednetSDK(simulation_mode=True, transport=SimulationTransport(state)) as sync_sdk:
        with pytest.raises(TypeError, match="AsyncImednetFacade"):
            await async_export_to_mongodb(
                sync_sdk,  # type: ignore[arg-type]
                "STUDY",
                "mongodb://localhost:27017",
                "db",
                "col",
            )
        with pytest.raises(TypeError, match="AsyncImednetFacade"):
            await async_export_to_neo4j(
                sync_sdk,  # type: ignore[arg-type]
                "STUDY",
                "bolt://localhost:7687",
                ("neo4j", "p"),
            )
        with pytest.raises(TypeError, match="AsyncImednetFacade"):
            await async_export_to_snowflake(
                sync_sdk,  # type: ignore[arg-type]
                "STUDY",
                config=SnowflakeSinkConfig(study_key="STUDY"),
            )


@pytest.mark.asyncio
async def test_async_sinks_namespace_properties_deprecated_args_and_snowflake_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover AsyncSinksNamespace lazy class accessors, deprecated sdk args, and AsyncSnowflakeExportSink branches."""
    import imednet_sinks.warehouse as wh_mod
    from imednet.errors import ExportBatchError

    sf = ModuleType("snowflake.connector")
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    cursor.rowcount = 1
    conn.cursor.return_value = cursor
    sf.connect = MagicMock(return_value=conn)  # type: ignore[attr-defined]

    pa = ModuleType("pyarrow")
    pq = ModuleType("pyarrow.parquet")
    pa.Table = MagicMock()  # type: ignore[attr-defined]
    pa.Table.from_pylist = MagicMock(
        side_effect=lambda rows: MagicMock(column_names=list(rows[0].keys()) if rows else [])
    )
    pq.write_table = MagicMock()  # type: ignore[attr-defined]

    monkeypatch.setattr(
        wh_mod,
        "_require_optional_dep",
        lambda name, _extra: (
            sf if name == "snowflake.connector" else (pa if name == "pyarrow" else pq)
        ),
    )

    state = SimulationState()
    async with AsyncImednetSDK(
        simulation_mode=True,
        transport=SimulationTransport(state),
    ) as async_sdk:
        ns = async_sdk.sinks
        assert ns.AsyncMongoDbExportSink is AsyncMongoDbExportSink
        assert ns.AsyncNeo4jExportSink is AsyncNeo4jExportSink
        assert ns.AsyncSnowflakeExportSink is AsyncSnowflakeExportSink
        assert ns.MongoDbSinkConfig is MongoDbSinkConfig
        assert ns.Neo4jSinkConfig is Neo4jSinkConfig
        assert ns.SnowflakeSinkConfig is SnowflakeSinkConfig

        sf_cfg = SnowflakeSinkConfig(
            study_key="STUDY",
            account="acct",
            user="svc",
            password="secret",  # pragma: allowlist secret
            database="CLINICAL",
            schema="RAW",
            warehouse="WH",
            stage="STG",
            table="RECORDS",
            naming_convention="camelCase",
        )
        n_sf = await ns.export_to_snowflake("STUDY", config=sf_cfg, mask_phi=True)
        assert n_sf > 0

        # Deprecated positional and keyword sdk arguments on AsyncSinksNamespace
        with pytest.deprecated_call():
            await ns.export_to_snowflake(async_sdk, "STUDY", config=sf_cfg)
        with pytest.deprecated_call():
            await ns.export_to_snowflake("STUDY", sdk=async_sdk, config=sf_cfg)  # type: ignore[call-overload]

        with pytest.raises(TypeError, match="study_key"):
            await ns.export_to_snowflake(12345, config=sf_cfg)  # type: ignore[call-overload]

        # Exercise AsyncSnowflakeExportSink empty batch, unsafe batch_id, in-batch voided records, and camelCase reconcile
        active, voided = _sample_records()
        async with AsyncSnowflakeExportSink(sf_cfg) as sink:
            assert await sink.write_batch([], batch_id="empty") == 0
            with pytest.raises(ExportBatchError):
                await sink.write_batch(active, batch_id="///")
            assert await sink.write_batch(voided, batch_id="only_voided") == 0
            assert await sink.reconcile_deleted([], batch_id="empty_rec") == 0

    with ImednetSDK(simulation_mode=True, transport=SimulationTransport(state)) as sync_sdk:
        bad_ns = AsyncSinksNamespace(sync_sdk)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="AsyncImednetSDK"):
            await bad_ns.export_to_mongodb("STUDY", "mongodb://localhost:27017", "db", "col")
