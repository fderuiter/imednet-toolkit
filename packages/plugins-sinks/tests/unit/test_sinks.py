"""TODO: Add docstring."""

from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace
from unittest.mock import ANY, MagicMock

import pytest
from imednet_sinks import (
    Neo4jSinkConfig,
)

from imednet.errors import ExportBatchError, ExportConfigurationError


def _fake_neo4j_module(fail_connect: bool = False) -> ModuleType:
    """TODO: Add docstring."""
    neo4j = ModuleType("neo4j")
    driver = MagicMock()
    if fail_connect:
        driver.verify_connectivity.side_effect = Exception("failed to connect")
    neo4j.GraphDatabase = MagicMock()
    neo4j.GraphDatabase.driver.return_value = driver
    return neo4j, driver


def _fake_pymongo_module(fail_connect: bool = False) -> ModuleType:
    """TODO: Add docstring."""
    pymongo = ModuleType("pymongo")
    client = MagicMock()
    if fail_connect:
        client.admin.command.side_effect = Exception("connection refused")
    collection = MagicMock()
    result = MagicMock()
    result.upserted_count = 2
    result.modified_count = 1
    collection.bulk_write.return_value = result
    result_insert = MagicMock()
    result_insert.inserted_ids = [1, 2, 3]
    collection.insert_many.return_value = result_insert
    inner = MagicMock(__getitem__=MagicMock(return_value=collection))
    client.__getitem__ = MagicMock(return_value=inner)
    pymongo.MongoClient = MagicMock(return_value=client)
    return pymongo, client, collection


def _fake_snowflake_module() -> tuple:
    """TODO: Add docstring."""
    sf = ModuleType("snowflake.connector")
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    sf.connect = MagicMock(return_value=conn)
    return sf, conn, cursor


def _fake_pyarrow_modules() -> tuple:
    """TODO: Add docstring."""
    pa = ModuleType("pyarrow")
    pq = ModuleType("pyarrow.parquet")
    table = MagicMock()
    pa.Table = MagicMock()
    pa.Table.from_pylist = MagicMock(return_value=table)
    pq.write_table = MagicMock()
    return pa, pq, table


class TestNeo4jExportSink:
    """TODO: Add docstring."""

    def test_connect_calls_verify_connectivity(self, monkeypatch):
        """TODO: Add docstring."""
        import imednet_sinks.graph as graph_mod

        neo4j, driver = _fake_neo4j_module()
        monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j)

        from imednet_sinks.graph import Neo4jExportSink

        sink = Neo4jExportSink(
            Neo4jSinkConfig(study_key="STUDY1", uri="bolt://localhost:7687", auth=("neo4j", "pass"))
        )
        driver.verify_connectivity.assert_called_once()
        sink.close()

    def test_connect_raises_configuration_error_on_failure(self, monkeypatch):
        """TODO: Add docstring."""
        import imednet_sinks.graph as graph_mod

        neo4j, driver = _fake_neo4j_module(fail_connect=True)
        monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j)

        from imednet_sinks.graph import Neo4jExportSink

        with pytest.raises(ExportConfigurationError, match="Cannot connect to Neo4j"):
            Neo4jExportSink(
                Neo4jSinkConfig(
                    study_key="STUDY1", uri="bolt://localhost:7687", auth=("neo4j", "pass")
                )
            )

    def test_write_batch_returns_count(self, monkeypatch):
        """TODO: Add docstring."""
        import imednet_sinks.graph as graph_mod

        neo4j, driver = _fake_neo4j_module()
        monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j)

        from imednet_sinks.graph import Neo4jExportSink

        records = [
            MagicMock(record_id=i, form_id=1, visit_id=1, subject_key="S1", record_data={})
            for i in range(5)
        ]
        with Neo4jExportSink(
            Neo4jSinkConfig(study_key="STUDY1", uri="bolt://localhost:7687", auth=("neo4j", "pass"))
        ) as sink:
            count = sink.write_batch(records, batch_id="STUDY1/F1/0")
        assert count == 5

    def test_write_batch_uses_structure_preserving_payload_shape(self, monkeypatch):
        """TODO: Add docstring."""
        import imednet_sinks.graph as graph_mod

        neo4j, driver = _fake_neo4j_module()
        monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j)

        from imednet_sinks.graph import Neo4jExportSink

        record = SimpleNamespace(
            record_id=1234,
            form_id=7,
            visit_id=42,
            subject_key="SUBJ-001",
            record_data={"labs": {"hemoglobin": 13.2}, "status": "Complete"},
        )
        with Neo4jExportSink(
            Neo4jSinkConfig(study_key="STUDY1", uri="bolt://localhost:7687", auth=("neo4j", "pass"))
        ) as sink:
            count = sink.write_batch([record], batch_id="STUDY1/F7/0")

        assert count == 1
        run_args = driver.session.return_value.__enter__.return_value.run.call_args
        assert run_args.args[0] == graph_mod._MERGE_RECORD_CYPHER
        assert run_args.kwargs["rows"] == [
            {
                "study_key": "STUDY1",
                "interval_id": None,
                "form_id": 7,
                "form_key": None,
                "site_id": None,
                "record_id": 1234,
                "record_oid": None,
                "record_type": None,
                "record_status": None,
                "deleted": False,
                "date_created": None,
                "date_modified": ANY,
                "subject_id": None,
                "subject_oid": None,
                "subject_key": "SUBJ-001",
                "visit_id": 42,
                "parent_record_id": None,
                "record_data": '{"labs": {"hemoglobin": 13.2}, "status": "Complete"}',
                "last_updated": None,
                "keywords": None,
            }
        ]

    def test_write_batch_empty_returns_zero(self, monkeypatch):
        """TODO: Add docstring."""
        import imednet_sinks.graph as graph_mod

        neo4j, driver = _fake_neo4j_module()
        monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j)

        from imednet_sinks.graph import Neo4jExportSink

        with Neo4jExportSink(
            Neo4jSinkConfig(study_key="STUDY1", uri="bolt://localhost:7687", auth=("neo4j", "pass"))
        ) as sink:
            count = sink.write_batch([], batch_id="STUDY1/F1/0")
        assert count == 0

    def test_write_batch_raises_after_retries_exhausted(self, monkeypatch):
        """TODO: Add docstring."""
        import imednet_sinks.graph as graph_mod

        neo4j, driver = _fake_neo4j_module()
        # Make session.run always fail
        driver.session.return_value.__enter__.return_value.run.side_effect = RuntimeError("db down")
        monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j)
        monkeypatch.setattr("time.sleep", lambda _: None)

        from imednet_sinks.graph import Neo4jExportSink, Neo4jSinkConfig

        cfg = Neo4jSinkConfig(
            study_key="STUDY1",
            uri="bolt://localhost:7687",
            auth=("n", "p"),
            max_retries=1,
            retry_backoff=0.0,
        )
        sink = Neo4jExportSink(config=cfg)
        with pytest.raises(ExportBatchError, match="STUDY1/F1/0"):  # noqa: PT012
            records = [
                MagicMock(record_id=1, form_id=1, visit_id=1, subject_key="S", record_data={})
            ]
            sink.write_batch(records, batch_id="STUDY1/F1/0")
        sink.close()

    def test_close_is_idempotent(self, monkeypatch):
        """TODO: Add docstring."""
        import imednet_sinks.graph as graph_mod

        neo4j, driver = _fake_neo4j_module()
        monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j)

        from imednet_sinks.graph import Neo4jExportSink

        sink = Neo4jExportSink(
            Neo4jSinkConfig(study_key="S1", uri="bolt://localhost:7687", auth=("neo4j", "pass"))
        )
        sink.close()
        sink.close()  # must not raise

    def test_write_batch_reuses_mapper_instance(self, monkeypatch):
        """Verify CentralizedMapper is instantiated once per write_batch execution."""
        import imednet_sinks.graph as graph_mod

        from imednet.integrations.enrichment import CentralizedMapper

        neo4j, driver = _fake_neo4j_module()
        monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j)

        real_init = CentralizedMapper.__init__
        instances = []

        def tracked_init(self, *args, **kwargs):
            instances.append(self)
            return real_init(self, *args, **kwargs)

        monkeypatch.setattr(CentralizedMapper, "__init__", tracked_init)

        from imednet_sinks.graph import Neo4jExportSink

        records = [
            MagicMock(record_id=i, form_id=1, visit_id=1, subject_key="S1", record_data={})
            for i in range(5)
        ]
        with Neo4jExportSink(
            Neo4jSinkConfig(study_key="STUDY1", uri="bolt://localhost:7687", auth=("neo4j", "pass"))
        ) as sink:
            count = sink.write_batch(records, batch_id="STUDY1/F1/0")

        assert count == 5
        assert len(instances) == 1
        assert "STUDY1" in instances[0]._pipeline_cache

    def test_record_to_row_accepts_and_uses_injected_mapper(self):
        """Verify _record_to_row uses injected mapper and maintains backward compatibility."""
        from imednet_sinks.graph import _post_process_graph, _record_to_row

        from imednet.integrations.enrichment import CentralizedMapper

        record = MagicMock(record_id=10, form_id=1, visit_id=1, subject_key="S1", record_data={})

        # Backward compatibility when mapper is not passed
        row1 = _record_to_row(record, study_key="STUDY1")
        assert row1["record_id"] == 10

        # Injected mapper
        custom_mapper = CentralizedMapper(mode="document", post_processor=_post_process_graph)
        row2 = _record_to_row(record, study_key="STUDY1", mapper=custom_mapper)
        assert row2["record_id"] == 10
        assert "STUDY1" in custom_mapper._pipeline_cache


# ---------------------------------------------------------------------------
# MongoDbExportSink
# ---------------------------------------------------------------------------


def _fake_pymongo_module(fail_connect: bool = False) -> ModuleType:
    """TODO: Add docstring."""
    pymongo = ModuleType("pymongo")
    client = MagicMock()
    if fail_connect:
        client.admin.command.side_effect = Exception("connection refused")
    collection = MagicMock()
    result = MagicMock()
    result.upserted_count = 2
    result.modified_count = 1
    collection.bulk_write.return_value = result
    result_insert = MagicMock()
    result_insert.inserted_ids = [1, 2, 3]
    collection.insert_many.return_value = result_insert
    inner = MagicMock(__getitem__=MagicMock(return_value=collection))
    client.__getitem__ = MagicMock(return_value=inner)
    pymongo.MongoClient = MagicMock(return_value=client)
    pymongo.UpdateOne = MagicMock(side_effect=lambda *a, **kw: MagicMock())
    return pymongo, client, collection


class TestMongoDbExportSink:
    """TODO: Add docstring."""

    def test_connect_raises_configuration_error_on_failure(self, monkeypatch):
        """TODO: Add docstring."""
        import imednet_sinks.document as doc_mod

        pymongo, client, collection = _fake_pymongo_module(fail_connect=True)
        monkeypatch.setattr(doc_mod, "_require_optional_dep", lambda *_: pymongo)

        from imednet_sinks.document import MongoDbExportSink, MongoDbSinkConfig

        with pytest.raises(ExportConfigurationError, match="Cannot connect to MongoDB"):
            MongoDbExportSink(
                MongoDbSinkConfig(
                    study_key="STUDY1",
                    uri="mongodb://localhost:27017",
                    database="db",
                    collection="col",
                )
            )

    def test_connect_error_message_redacts_credentials(self, monkeypatch):
        """TODO: Add docstring."""
        import imednet_sinks.document as doc_mod

        pymongo, client, collection = _fake_pymongo_module(fail_connect=True)
        monkeypatch.setattr(doc_mod, "_require_optional_dep", lambda *_: pymongo)

        from imednet_sinks.document import MongoDbExportSink, MongoDbSinkConfig

        uri = "******localhost:27017"
        with pytest.raises(ExportConfigurationError) as exc_info:
            MongoDbExportSink(
                MongoDbSinkConfig(study_key="STUDY1", uri=uri, database="db", collection="col")
            )

        message = str(exc_info.value)
        assert "secret" not in message
        assert "localhost:27017" in message

    def test_write_batch_upsert_returns_count(self, monkeypatch):
        """TODO: Add docstring."""
        import imednet_sinks.document as doc_mod

        pymongo, client, collection = _fake_pymongo_module()
        monkeypatch.setattr(doc_mod, "_require_optional_dep", lambda *_: pymongo)

        from imednet_sinks.document import MongoDbExportSink, MongoDbSinkConfig

        records = [
            MagicMock(record_id=i, form_id=1, visit_id=1, subject_key="S1", record_data={})
            for i in range(3)
        ]
        with MongoDbExportSink(
            MongoDbSinkConfig(
                study_key="STUDY1", uri="mongodb://localhost:27017", database="db", collection="col"
            )
        ) as sink:
            count = sink.write_batch(records, batch_id="STUDY1/F1/0")
        # return value reflects number of exported records in the batch
        assert count == 3

    def test_write_batch_upsert_document_envelope_shape(self, monkeypatch):
        """TODO: Add docstring."""
        import imednet_sinks.document as doc_mod

        pymongo, client, collection = _fake_pymongo_module()
        monkeypatch.setattr(doc_mod, "_require_optional_dep", lambda *_: pymongo)

        from imednet_sinks.document import MongoDbExportSink, MongoDbSinkConfig

        record = SimpleNamespace(
            record_id=1234,
            subject_id=100,
            subject_key="SUBJ-001",
            visit_id=42,
            form_id=7,
            form_key="BASELINE",
            record_status="Complete",
            deleted=False,
            date_created=datetime(2024, 1, 1, tzinfo=timezone.utc),
            date_modified=datetime(2024, 1, 15, tzinfo=timezone.utc),
            record_data={"labs": {"hemoglobin": 13.2}, "symptoms": ["fatigue"]},
        )
        with MongoDbExportSink(
            MongoDbSinkConfig(
                study_key="STUDY1", uri="mongodb://localhost:27017", database="db", collection="col"
            )
        ) as sink:
            count = sink.write_batch([record], batch_id="STUDY1/F1/0")

        assert count == 1
        assert pymongo.UpdateOne.call_count == 1
        filter_doc, update_doc = pymongo.UpdateOne.call_args.args[:2]
        assert filter_doc == {"_id": "STUDY1/1234"}
        assert update_doc["$set"]["study_key"] == "STUDY1"
        assert update_doc["$set"]["record_id"] == 1234
        assert update_doc["$set"]["subject_id"] == 100
        assert update_doc["$set"]["subject_key"] == "SUBJ-001"
        assert update_doc["$set"]["visit_id"] == 42
        assert update_doc["$set"]["form_id"] == 7
        assert update_doc["$set"]["form_key"] == "BASELINE"
        assert update_doc["$set"]["record_status"] == "Complete"
        assert update_doc["$set"]["deleted"] is False
        assert update_doc["$set"]["date_created"] == datetime(2024, 1, 1, tzinfo=timezone.utc)
        assert update_doc["$set"]["date_modified"] == datetime(2024, 1, 15, tzinfo=timezone.utc)
        assert update_doc["$set"]["record_data"] == {
            "labs": {"hemoglobin": 13.2},
            "symptoms": ["fatigue"],
        }
        assert "exported_at" in update_doc["$set"]

    def test_write_batch_empty_returns_zero(self, monkeypatch):
        """TODO: Add docstring."""
        import imednet_sinks.document as doc_mod

        pymongo, client, collection = _fake_pymongo_module()
        monkeypatch.setattr(doc_mod, "_require_optional_dep", lambda *_: pymongo)

        from imednet_sinks.document import MongoDbExportSink, MongoDbSinkConfig

        with MongoDbExportSink(
            MongoDbSinkConfig(
                study_key="STUDY1", uri="mongodb://localhost:27017", database="db", collection="col"
            )
        ) as sink:
            count = sink.write_batch([], batch_id="b0")
        assert count == 0

    def test_write_batch_raises_after_retries(self, monkeypatch):
        """TODO: Add docstring."""
        import imednet_sinks.document as doc_mod

        pymongo, client, collection = _fake_pymongo_module()
        collection.bulk_write.side_effect = RuntimeError("mongo down")
        monkeypatch.setattr(doc_mod, "_require_optional_dep", lambda *_: pymongo)
        monkeypatch.setattr("time.sleep", lambda _: None)

        from imednet_sinks.document import MongoDbExportSink, MongoDbSinkConfig

        cfg = MongoDbSinkConfig(
            study_key="STUDY1",
            uri="mongodb://localhost:27017",
            database="db",
            collection="col",
            max_retries=1,
            retry_backoff=0.0,
        )
        with MongoDbExportSink(config=cfg) as sink:  # noqa: SIM117
            with pytest.raises(ExportBatchError, match="S1/F1/0"):  # noqa: PT012
                records = [
                    MagicMock(record_id=1, form_id=1, visit_id=1, subject_key="S", record_data={})
                ]
                sink.write_batch(records, batch_id="S1/F1/0")

    def test_document_id_format(self):
        """TODO: Add docstring."""
        from imednet_sinks.document import _make_document_id

        assert _make_document_id("MYSTUDY", 42) == "MYSTUDY/42"

    def test_close_is_idempotent(self, monkeypatch):
        """TODO: Add docstring."""
        import imednet_sinks.document as doc_mod

        pymongo, client, collection = _fake_pymongo_module()
        monkeypatch.setattr(doc_mod, "_require_optional_dep", lambda *_: pymongo)

        from imednet_sinks.document import MongoDbExportSink, MongoDbSinkConfig

        sink = MongoDbExportSink(
            MongoDbSinkConfig(
                study_key="S1", uri="mongodb://localhost:27017", database="db", collection="col"
            )
        )
        sink.close()
        sink.close()  # must not raise

    def test_write_batch_reuses_mapper_instance(self, monkeypatch):
        """Verify CentralizedMapper is instantiated once per write_batch execution."""
        import imednet_sinks.document as doc_mod

        from imednet.integrations.enrichment import CentralizedMapper

        pymongo, client, collection = _fake_pymongo_module()
        monkeypatch.setattr(doc_mod, "_require_optional_dep", lambda *_: pymongo)

        real_init = CentralizedMapper.__init__
        instances = []

        def tracked_init(self, *args, **kwargs):
            instances.append(self)
            return real_init(self, *args, **kwargs)

        monkeypatch.setattr(CentralizedMapper, "__init__", tracked_init)

        from imednet_sinks.document import MongoDbExportSink, MongoDbSinkConfig

        records = [
            MagicMock(record_id=i, form_id=1, visit_id=1, subject_key="S1", record_data={})
            for i in range(5)
        ]
        with MongoDbExportSink(
            MongoDbSinkConfig(
                study_key="STUDY1", uri="mongodb://localhost:27017", database="db", collection="col"
            )
        ) as sink:
            count = sink.write_batch(records, batch_id="STUDY1/F1/0")

        assert count == 5
        assert len(instances) == 1
        assert "STUDY1" in instances[0]._pipeline_cache

    def test_record_to_document_accepts_and_uses_injected_mapper(self):
        """Verify _record_to_document uses injected mapper and maintains backward compatibility."""
        from imednet_sinks.document import _post_process_document, _record_to_document

        from imednet.integrations.enrichment import CentralizedMapper

        record = MagicMock(record_id=10, form_id=1, visit_id=1, subject_key="S1", record_data={})

        # Backward compatibility when mapper is not passed
        doc1 = _record_to_document(record, study_key="STUDY1")
        assert doc1["_id"] == "STUDY1/10"

        # Injected mapper
        custom_mapper = CentralizedMapper(mode="document", post_processor=_post_process_document)
        doc2 = _record_to_document(record, study_key="STUDY1", mapper=custom_mapper)
        assert doc2["_id"] == "STUDY1/10"
        assert "STUDY1" in custom_mapper._pipeline_cache


# ---------------------------------------------------------------------------
# SnowflakeExportSink
# ---------------------------------------------------------------------------


def _fake_snowflake_module() -> ModuleType:
    """TODO: Add docstring."""
    sf = ModuleType("snowflake.connector")
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    sf.connect = MagicMock(return_value=conn)
    return sf, conn, cursor


def _fake_pyarrow_modules() -> tuple:
    """TODO: Add docstring."""
    pa = ModuleType("pyarrow")
    pq = ModuleType("pyarrow.parquet")
    table = MagicMock()
    pa.Table = MagicMock()
    pa.Table.from_pylist = MagicMock(return_value=table)
    pq.write_table = MagicMock()
    return pa, pq, table


class TestSnowflakeExportSink:
    """TODO: Add docstring."""

    def _make_sink(self, monkeypatch, *, tmp_path, fail_connect=False):
        """TODO: Add docstring."""
        import imednet_sinks.warehouse as wh_mod
        from imednet_sinks.warehouse import SnowflakeSinkConfig

        sf, conn, cursor = _fake_snowflake_module()
        if fail_connect:
            sf.connect.side_effect = Exception("auth error")
        pa, pq, table = _fake_pyarrow_modules()

        def fake_require(pkg, extras):
            """TODO: Add docstring."""
            if pkg == "snowflake.connector":
                return sf
            if pkg == "pyarrow":
                return pa
            if pkg == "pyarrow.parquet":
                return pq
            return MagicMock()

        monkeypatch.setattr(wh_mod, "_require_optional_dep", fake_require)

        cfg = SnowflakeSinkConfig(
            study_key="STUDY1",
            account="acct",
            user="user",
            password="secret",
            database="DB",
            schema="PUBLIC",
            warehouse="WH",
            stage="STG",
            table="TBL",
            local_staging_dir=str(tmp_path),
        )
        return cfg, sf, conn, cursor, pq

    def test_connect_raises_configuration_error_on_failure(self, monkeypatch, tmp_path):
        """TODO: Add docstring."""
        import imednet_sinks.warehouse as wh_mod
        from imednet_sinks.warehouse import SnowflakeExportSink, SnowflakeSinkConfig

        sf, conn, cursor = _fake_snowflake_module()
        sf.connect.side_effect = Exception("auth failed")
        pa, pq, table = _fake_pyarrow_modules()

        def fake_require(pkg, extras):
            """TODO: Add docstring."""
            if pkg == "snowflake.connector":
                return sf
            return pa if "pyarrow" in pkg else MagicMock()

        monkeypatch.setattr(wh_mod, "_require_optional_dep", fake_require)

        cfg = SnowflakeSinkConfig(
            study_key="STUDY1",
            account="acct",
            user="user",
            password="secret",
            database="DB",
            schema="PUBLIC",
            warehouse="WH",
            stage="STG",
            table="TBL",
            local_staging_dir=str(tmp_path),
        )
        with pytest.raises(ExportConfigurationError, match="Cannot connect to Snowflake"):
            SnowflakeExportSink(config=cfg)

    def test_raises_for_missing_required_config(self, monkeypatch, tmp_path):
        """TODO: Add docstring."""
        import imednet_sinks.warehouse as wh_mod
        from imednet_sinks.warehouse import SnowflakeExportSink, SnowflakeSinkConfig

        sf, conn, cursor = _fake_snowflake_module()
        monkeypatch.setattr(wh_mod, "_require_optional_dep", lambda *_: sf)

        # account is empty → should raise
        cfg = SnowflakeSinkConfig(study_key="STUDY1", local_staging_dir=str(tmp_path))
        with pytest.raises(ExportConfigurationError, match="missing required fields"):
            SnowflakeExportSink(config=cfg)

    def test_records_to_arrow_table_uses_snowflake_extra(self, monkeypatch):
        """TODO: Add docstring."""
        import imednet_sinks.warehouse as wh_mod

        pa, _, table = _fake_pyarrow_modules()
        seen: list[tuple[str, str]] = []

        def fake_require(pkg, extras):
            """TODO: Add docstring."""
            seen.append((pkg, extras))
            return pa

        monkeypatch.setattr(wh_mod, "_require_optional_dep", fake_require)

        result = wh_mod._records_to_arrow_table(
            [MagicMock(record_id=1, form_id=2, visit_id=3, subject_key="S", record_data={})]
        )

        assert result is table
        assert seen == [("pyarrow", "snowflake")]

    def test_write_batch_calls_put_and_copy(self, monkeypatch, tmp_path):
        """TODO: Add docstring."""
        from imednet_sinks.warehouse import SnowflakeExportSink

        cfg, sf, conn, cursor, pq = self._make_sink(monkeypatch, tmp_path=tmp_path)
        sink = SnowflakeExportSink(config=cfg)
        records = [
            MagicMock(record_id=i, form_id=1, visit_id=1, subject_key="S", record_data={})
            for i in range(3)
        ]
        count = sink.write_batch(records, batch_id="S1/F1/0")
        assert count == 3
        # PUT and COPY INTO must both be called
        calls = [str(c) for c in cursor.execute.call_args_list]
        assert any("PUT" in c for c in calls)
        assert any("COPY INTO" in c for c in calls)
        sink.close()

    def test_write_batch_uses_snowflake_extra_for_parquet(self, monkeypatch, tmp_path):
        """TODO: Add docstring."""
        import imednet_sinks.warehouse as wh_mod
        from imednet_sinks.warehouse import SnowflakeExportSink

        cfg, sf, conn, cursor, pq = self._make_sink(monkeypatch, tmp_path=tmp_path)
        seen: list[tuple[str, str]] = []

        def fake_require(pkg, extras):
            """TODO: Add docstring."""
            seen.append((pkg, extras))
            if pkg == "snowflake.connector":
                return sf
            if pkg == "pyarrow":
                return MagicMock(Table=MagicMock(from_pylist=MagicMock(return_value=MagicMock())))
            if pkg == "pyarrow.parquet":
                return pq
            return MagicMock()

        monkeypatch.setattr(wh_mod, "_require_optional_dep", fake_require)

        sink = SnowflakeExportSink(config=cfg)
        sink.write_batch(
            [MagicMock(record_id=1, form_id=1, visit_id=1, subject_key="S", record_data={})],
            batch_id="S1/F1/0",
        )
        sink.close()

        assert ("pyarrow", "snowflake") in seen
        assert ("pyarrow.parquet", "snowflake") in seen

    def test_write_batch_empty_returns_zero(self, monkeypatch, tmp_path):
        """TODO: Add docstring."""
        from imednet_sinks.warehouse import SnowflakeExportSink

        cfg, sf, conn, cursor, pq = self._make_sink(monkeypatch, tmp_path=tmp_path)
        sink = SnowflakeExportSink(config=cfg)
        assert sink.write_batch([], batch_id="b0") == 0
        sink.close()

    def test_write_batch_raises_after_retries(self, monkeypatch, tmp_path):
        """TODO: Add docstring."""
        import imednet_sinks.warehouse as wh_mod
        from imednet_sinks.warehouse import SnowflakeExportSink, SnowflakeSinkConfig

        sf, conn, cursor = _fake_snowflake_module()
        cursor.execute.side_effect = RuntimeError("network error")
        pa, pq, table = _fake_pyarrow_modules()

        def fake_require(pkg, extras):
            """TODO: Add docstring."""
            if pkg == "snowflake.connector":
                return sf
            if pkg == "pyarrow":
                return pa
            if pkg == "pyarrow.parquet":
                return pq
            return MagicMock()

        monkeypatch.setattr(wh_mod, "_require_optional_dep", fake_require)
        monkeypatch.setattr("time.sleep", lambda _: None)

        cfg = SnowflakeSinkConfig(
            study_key="STUDY1",
            account="acct",
            user="user",
            password="secret",
            database="DB",
            schema="PUBLIC",
            warehouse="WH",
            stage="STG",
            table="TBL",
            local_staging_dir=str(tmp_path),
            max_retries=1,
            retry_backoff=0.0,
        )
        sink = SnowflakeExportSink(config=cfg)
        records = [MagicMock(record_id=1, form_id=1, visit_id=1, subject_key="S", record_data={})]
        with pytest.raises(ExportBatchError, match="S1/F1/0"):
            sink.write_batch(records, batch_id="S1/F1/0")
        sink.close()

    def test_manifest_appended(self, monkeypatch, tmp_path):
        """TODO: Add docstring."""
        import json

        from imednet_sinks.warehouse import SnowflakeExportSink, SnowflakeSinkConfig

        cfg_dict, sf, conn, cursor, pq = self._make_sink(monkeypatch, tmp_path=tmp_path)
        manifest = tmp_path / "manifest.jsonl"

        cfg = SnowflakeSinkConfig(
            study_key="STUDY1",
            account="acct",
            user="user",
            password="secret",
            database="DB",
            schema="PUBLIC",
            warehouse="WH",
            stage="STG",
            table="TBL",
            local_staging_dir=str(tmp_path),
            manifest_path=str(manifest),
        )

        # re-apply monkeypatch with correct cfg
        import imednet_sinks.warehouse as wh_mod2

        sf2, conn2, cursor2 = _fake_snowflake_module()
        pa2, pq2, table2 = _fake_pyarrow_modules()

        def fake_require2(pkg, extras):
            """TODO: Add docstring."""
            if pkg == "snowflake.connector":
                return sf2
            if pkg == "pyarrow":
                return pa2
            if pkg == "pyarrow.parquet":
                return pq2
            return MagicMock()

        monkeypatch.setattr(wh_mod2, "_require_optional_dep", fake_require2)

        sink = SnowflakeExportSink(config=cfg)
        records = [MagicMock(record_id=1, form_id=1, visit_id=1, subject_key="S", record_data={})]
        sink.write_batch(records, batch_id="STUDY/FORM/0")
        sink.close()

        assert manifest.exists()
        entry = json.loads(manifest.read_text().strip())
        assert entry["batch_id"] == "STUDY/FORM/0"
        assert entry["row_count"] == 1

    def test_close_is_idempotent(self, monkeypatch, tmp_path):
        """TODO: Add docstring."""
        from imednet_sinks.warehouse import SnowflakeExportSink

        cfg, sf, conn, cursor, pq = self._make_sink(monkeypatch, tmp_path=tmp_path)
        sink = SnowflakeExportSink(config=cfg)
        sink.close()
        sink.close()  # must not raise


def test_plugin_namespace():
    """TODO: Add docstring."""
    from imednet_sinks.plugin import create_sinks

    sdk_mock = MagicMock()
    sinks = create_sinks(sdk_mock)

    assert sinks.export_to_mongodb is not None
    assert sinks.MongoDbExportSink is not None
    assert sinks.export_to_neo4j is not None
    assert sinks.Neo4jSinkConfig is not None
    assert sinks.Neo4jExportSink is not None
    assert sinks.export_to_snowflake is not None
    assert sinks.SnowflakeSinkConfig is not None
    assert sinks.SnowflakeExportSink is not None


def test_export_to_neo4j(monkeypatch):
    """TODO: Add docstring."""
    import imednet_sinks.graph as graph_mod

    neo4j, driver = _fake_neo4j_module()
    monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j)

    sdk_mock = MagicMock()
    sdk_mock.records.list.return_value = [MagicMock()]

    total = graph_mod.export_to_neo4j(sdk_mock, "S1", "uri", ("user", "pass"))
    assert total == 1


def test_export_to_mongodb(monkeypatch):
    """TODO: Add docstring."""
    import imednet_sinks.document as doc_mod

    pymongo, client, collection = _fake_pymongo_module()
    monkeypatch.setattr(doc_mod, "_require_optional_dep", lambda *_: pymongo)

    sdk_mock = MagicMock()
    sdk_mock.records.list.return_value = [MagicMock()]

    total = doc_mod.export_to_mongodb(sdk_mock, "S1", "uri", "db", "col")
    assert total == 1


def test_export_to_snowflake(monkeypatch, tmp_path):
    """TODO: Add docstring."""
    import imednet_sinks.warehouse as wh_mod
    from imednet_sinks.warehouse import SnowflakeSinkConfig

    sf, conn, cursor = _fake_snowflake_module()
    pa, pq, table = _fake_pyarrow_modules()

    def fake_require(pkg, extras):
        """TODO: Add docstring."""
        if pkg == "snowflake.connector":
            return sf
        if pkg == "pyarrow":
            return pa
        if pkg == "pyarrow.parquet":
            return pq
        return MagicMock()

    monkeypatch.setattr(wh_mod, "_require_optional_dep", fake_require)

    sdk_mock = MagicMock()
    sdk_mock.records.list.return_value = [MagicMock()]

    cfg = SnowflakeSinkConfig(
        study_key="STUDY1",
        account="acct",
        user="user",
        password="secret",
        database="DB",
        schema="PUBLIC",
        warehouse="WH",
        stage="STG",
        table="TBL",
        local_staging_dir=str(tmp_path),
    )

    total = wh_mod.export_to_snowflake(sdk_mock, "S1", config=cfg)
    assert total == 1


# ---------------------------------------------------------------------------
# integrations/__init__.py re-exports
# ---------------------------------------------------------------------------


class TestBatchScopedMapperPerformance:
    """Performance and caching tests for batch-scoped CentralizedMapper reuse."""

    def test_pipeline_cache_reuse_across_1000_records(self):
        """Verify pipeline cache is populated once and reused across 1,000 records."""
        from imednet_sinks.document import _post_process_document, _record_to_document
        from imednet_sinks.graph import _post_process_graph, _record_to_row

        from imednet.integrations.enrichment import CentralizedMapper

        records = [
            SimpleNamespace(
                record_id=i,
                subject_id=i % 50,
                subject_key=f"SUBJ-{i % 50}",
                visit_id=1,
                form_id=2,
                form_key="BASELINE",
                record_status="Complete",
                deleted=False,
                date_created="2024-01-01T00:00:00Z",
                date_modified="2024-01-02T00:00:00Z",
                record_data={"score": i, "result": "PASS"},
            )
            for i in range(1000)
        ]

        # MongoDB document mapper cache reuse
        doc_mapper = CentralizedMapper(mode="document", post_processor=_post_process_document)
        assert len(doc_mapper._pipeline_cache) == 0
        docs = [_record_to_document(r, "STUDY1", mapper=doc_mapper) for r in records]
        assert len(docs) == 1000
        assert len(doc_mapper._pipeline_cache) == 1
        assert "STUDY1" in doc_mapper._pipeline_cache

        # Neo4j graph row mapper cache reuse
        graph_mapper = CentralizedMapper(mode="document", post_processor=_post_process_graph)
        assert len(graph_mapper._pipeline_cache) == 0
        rows = [_record_to_row(r, "STUDY1", mapper=graph_mapper) for r in records]
        assert len(rows) == 1000
        assert len(graph_mapper._pipeline_cache) == 1
        assert "STUDY1" in graph_mapper._pipeline_cache
