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

    def test_write_batch_unscheduled_record_skips_visit_node(self, monkeypatch):
        """Verify Neo4jExportSink preserves visit_id=None and guards Cypher Visit MERGE/MATCH on non-null visit_id."""
        import json

        import imednet_sinks.graph as graph_mod
        from imednet_sinks.graph import Neo4jExportSink

        from imednet.models.records import Record

        neo4j, driver = _fake_neo4j_module()
        monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j)

        unscheduled = Record.from_json(
            {
                "studyKey": "STUDY1",
                "recordId": 77,
                "formId": 7,
                "subjectKey": "SUBJ-UNSCHED",
                "keywords": [
                    {
                        "keywordName": "AE",
                        "keywordKey": "AE_KEY",
                        "keywordId": 5,
                        "dateAdded": "2026-05-13T14:06:50Z",
                    }
                ],
                "recordData": {"aeterm": "Headache"},
            }
        )
        assert unscheduled.visit_id is None
        assert unscheduled.parent_record_id is None

        with Neo4jExportSink(
            Neo4jSinkConfig(study_key="STUDY1", uri="bolt://localhost:7687", auth=("neo4j", "pass"))
        ) as sink:
            count = sink.write_batch([unscheduled], batch_id="STUDY1/unsched/0")

        assert count == 1
        run_args = driver.session.return_value.__enter__.return_value.run.call_args
        cypher = run_args.args[0]
        assert "CASE WHEN row.visit_id IS NOT NULL" in cypher
        assert "WHERE row.visit_id IS NOT NULL" in graph_mod._CREATE_RECORD_CYPHER
        row = run_args.kwargs["rows"][0]
        assert row["visit_id"] is None
        assert row["parent_record_id"] is None
        assert json.loads(row["keywords"])[0]["keywordId"] == 5


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

    @pytest.mark.parametrize(
        "param_name,invalid_val",
        [
            ("database", "DB; DROP TABLE TBL;--"),
            ("database", "DB' OR '1'='1"),
            ("database", "DB WITH SPACES"),
            ("schema", "PUBLIC;--"),
            ("warehouse", "WH /* comment */"),
            ("stage", "STG/PATH"),
            ("table", "TBL; TRUNCATE TABLE TBL;"),
            ("table", 'TBL"'),
        ],
    )
    def test_config_raises_for_invalid_identifier(self, param_name, invalid_val, tmp_path):
        """Test that invalid Snowflake database coordinates raise ExportConfigurationError."""
        from imednet_sinks.warehouse import SnowflakeSinkConfig

        valid_kwargs = {
            "study_key": "STUDY1",
            "account": "acct",
            "user": "user",
            "password": "secret",
            "database": "DB",
            "schema": "PUBLIC",
            "warehouse": "WH",
            "stage": "STG",
            "table": "TBL",
            "local_staging_dir": str(tmp_path),
        }
        valid_kwargs[param_name] = invalid_val
        with pytest.raises(
            ExportConfigurationError, match=f"parameter '{param_name}' contains invalid characters"
        ):
            SnowflakeSinkConfig(**valid_kwargs)

    @pytest.mark.parametrize(
        "prefix_field,invalid_val",
        [
            ("stage_prefix", "prefix; DROP TABLE TBL;--"),
            ("stage_prefix", "prefix with spaces"),
            ("stage_prefix", "prefix' OR '1'='1"),
            ("prefix", "prefix; DROP TABLE TBL;--"),
        ],
    )
    def test_config_raises_for_invalid_stage_prefix(self, prefix_field, invalid_val, tmp_path):
        """Test that invalid stage prefixes raise ExportConfigurationError."""
        from imednet_sinks.warehouse import SnowflakeSinkConfig

        kwargs = {
            "study_key": "STUDY1",
            "account": "acct",
            "user": "user",
            "password": "secret",
            "database": "DB",
            "schema": "PUBLIC",
            "warehouse": "WH",
            "stage": "STG",
            "table": "TBL",
            "local_staging_dir": str(tmp_path),
            prefix_field: invalid_val,
        }
        with pytest.raises(
            ExportConfigurationError, match="parameter 'stage_prefix' contains invalid characters"
        ):
            SnowflakeSinkConfig(**kwargs)

    def test_sink_validates_coordinates_on_connect(self, monkeypatch, tmp_path):
        """Test that sink validation catches invalid coordinates modified after config instantiation."""
        from imednet_sinks.warehouse import SnowflakeExportSink

        cfg, sf, conn, cursor, pq = self._make_sink(monkeypatch, tmp_path=tmp_path)
        cfg.table = "TBL; DROP TABLE TBL;--"
        with pytest.raises(
            ExportConfigurationError, match="parameter 'table' contains invalid characters"
        ):
            SnowflakeExportSink(config=cfg)

    def test_write_batch_sanitizes_special_characters_in_batch_id(self, monkeypatch, tmp_path):
        """Test that write_batch sanitizes special characters in batch identifier into safe alphanumeric format."""
        from imednet_sinks.warehouse import SnowflakeExportSink

        cfg, sf, conn, cursor, pq = self._make_sink(monkeypatch, tmp_path=tmp_path)
        sink = SnowflakeExportSink(config=cfg)
        records = [MagicMock(record_id=1, form_id=1, visit_id=1, subject_key="S", record_data={})]

        count = sink.write_batch(records, batch_id="STUDY-1/FORM 1/batch:0#extra")
        assert count == 1

        # Check call arguments
        calls = [c.args[0] for c in cursor.execute.call_args_list if c.args]
        put_call = next(c for c in calls if "PUT" in c)
        copy_call = next(c for c in calls if "COPY INTO" in c)

        assert "STUDY_1_FORM_1_batch_0_extra.parquet" in put_call
        assert "STUDY_1_FORM_1_batch_0_extra.parquet" in copy_call
        sink.close()

    @pytest.mark.parametrize(
        "unsafe_batch_id",
        [
            "",
            "   ",
            ";;;;",
            "!@#$%^&*()",
            "___",
            "---",
            "/ / /",
        ],
    )
    def test_write_batch_rejects_empty_sanitized_batch_id(
        self, monkeypatch, tmp_path, unsafe_batch_id
    ):
        """Test that write_batch rejects batch IDs resolving to empty strings after sanitization."""
        from imednet_sinks.warehouse import SnowflakeExportSink

        cfg, sf, conn, cursor, pq = self._make_sink(monkeypatch, tmp_path=tmp_path)
        sink = SnowflakeExportSink(config=cfg)
        records = [MagicMock(record_id=1, form_id=1, visit_id=1, subject_key="S", record_data={})]

        with pytest.raises(
            ExportBatchError, match="resolved to an empty string after sanitization"
        ):
            sink.write_batch(records, batch_id=unsafe_batch_id)
        sink.close()

    def test_write_batch_quotes_identifiers_and_stage_path(self, monkeypatch, tmp_path):
        """Test that database identifiers and stage paths are properly quoted in queries."""
        from imednet_sinks.warehouse import SnowflakeExportSink

        cfg, sf, conn, cursor, pq = self._make_sink(monkeypatch, tmp_path=tmp_path)
        sink = SnowflakeExportSink(config=cfg)
        records = [MagicMock(record_id=1, form_id=1, visit_id=1, subject_key="S", record_data={})]

        sink.write_batch(records, batch_id="STUDY/FORM/0")

        calls = [c.args[0] for c in cursor.execute.call_args_list if c.args]
        put_call = next(c for c in calls if "PUT" in c)
        copy_call = next(c for c in calls if "COPY INTO" in c)
        merge_call = next(c for c in calls if "MERGE INTO" in c)

        # Stage path in PUT should be quoted and overwrite existing staged files
        assert "'@STG/imednet/'" in put_call
        assert "OVERWRITE = TRUE" in put_call
        # Database identifiers and stage path in COPY INTO and MERGE INTO should be safely quoted
        assert 'COPY INTO "DB"."PUBLIC"."TBL__imednet_stage"' in copy_call
        assert "FROM '@STG/imednet/STUDY_FORM_0.parquet'" in copy_call
        assert "MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE" in copy_call
        assert "FORCE = TRUE" in copy_call
        assert 'MERGE INTO "DB"."PUBLIC"."TBL" AS target' in merge_call
        assert 'SELECT * FROM "DB"."PUBLIC"."TBL__imednet_stage"' in merge_call
        sink.close()

    def test_write_batch_handles_non_standard_casing(self, monkeypatch, tmp_path):
        """Test that lowercase and mixed-case table coordinates are preserved and safely quoted."""
        import imednet_sinks.warehouse as wh_mod
        from imednet_sinks.warehouse import SnowflakeExportSink, SnowflakeSinkConfig

        sf, conn, cursor = _fake_snowflake_module()
        pa, pq, table = _fake_pyarrow_modules()

        monkeypatch.setattr(
            wh_mod,
            "_require_optional_dep",
            lambda pkg, extras: (
                sf if pkg == "snowflake.connector" else (pq if "parquet" in pkg else pa)
            ),
        )

        cfg = SnowflakeSinkConfig(
            study_key="study1",
            account="my_acct",
            user="my_user",
            password="secret",
            database="clinical_db",
            schema="study_schema",
            warehouse="compute_wh",
            stage="my_stage",
            table="SubjectRecords_v1",
            stage_prefix="imednet/staging",
            local_staging_dir=str(tmp_path),
        )
        sink = SnowflakeExportSink(config=cfg)
        records = [MagicMock(record_id=1, form_id=1, visit_id=1, subject_key="S", record_data={})]

        sink.write_batch(records, batch_id="study1/subj/1")

        calls = [c.args[0] for c in cursor.execute.call_args_list if c.args]
        copy_call = next(c for c in calls if "COPY INTO" in c)
        merge_call = next(c for c in calls if "MERGE INTO" in c)

        assert (
            'COPY INTO "clinical_db"."study_schema"."SubjectRecords_v1__imednet_stage"' in copy_call
        )
        assert "FROM '@my_stage/imednet/staging/study1_subj_1.parquet'" in copy_call
        assert "MATCH_BY_COLUMN_NAME = CASE_INSENSITIVE" in copy_call
        assert 'MERGE INTO "clinical_db"."study_schema"."SubjectRecords_v1" AS target' in merge_call
        sink.close()

    def test_unmocked_pyarrow_mixed_forms_preserve_metadata_and_apply_phi_and_enrichment(
        self, tmp_path
    ):
        """Convert mixed-Form Record batches with int/string Variable collisions using real PyArrow without ArrowInvalid."""
        import json

        import pyarrow as pa
        import pyarrow.parquet as pq
        from imednet_sinks.warehouse import _post_process_warehouse, _records_to_arrow_table

        from imednet.integrations.enrichment import CentralizedMapper, EnrichmentPipeline
        from imednet.models.records import Keyword, Record
        from imednet.models.study_config import MappingRule, StudyConfiguration

        records = [
            Record(
                record_id=101,
                subject_id=11,
                subject_key="SUBJ-01",
                visit_id=1,
                form_id=10,
                form_key="VITALS",
                interval_id=5,
                record_status="Complete",
                deleted=False,
                date_created="2026-01-01T00:00:00Z",
                date_modified="2026-01-02T00:00:00Z",
                keywords=[
                    Keyword(
                        keyword_name="urgent",
                        keyword_key="URGENT",
                        keyword_id=1,
                        date_added="2026-01-01T00:00:00Z",
                    ),
                    Keyword(
                        keyword_name="baseline",
                        keyword_key="BASELINE",
                        keyword_id=2,
                        date_added="2026-01-01T00:00:00Z",
                    ),
                ],
                record_data={
                    "lab_value": 42,
                    "assessment": {"grade": 1},
                    "arm_code": "A",
                    "patient_name": "Jane Doe",
                },
            ),
            Record(
                record_id=102,
                subject_id=12,
                subject_key="SUBJ-02",
                visit_id=2,
                form_id=20,
                form_key="LABS",
                interval_id=6,
                record_status="In Progress",
                deleted=False,
                date_created="2026-01-03T00:00:00Z",
                date_modified="2026-01-04T00:00:00Z",
                keywords=None,
                record_data={
                    "lab_value": "below_detection_limit",
                    "assessment": "not_done",
                    "arm_code": "B",
                    "ssn": "123-45-6789",
                },
            ),
            {
                "record_id": 103,
                "subject_id": 13,
                "subject_key": "SUBJ-03",
                "visit_id": 3,
                "form_id": 30,
                "form_key": "AE",
                "record_status": "Complete",
                "keywords": [{"keyword_name": "serious", "keyword_id": 9}],
                "record_data": {
                    "lab_value": [1.5, 2.5],
                    "arm_code": "A",
                },
            },
        ]

        study_cfg = StudyConfiguration(
            studyKey="STUDY_HETERO",
            terminologyLookups={"arm_code": {"A": "Active", "B": "Placebo"}},
            mappings=[
                MappingRule(
                    domain="DM",
                    targetField="treatment_arm",
                    sourceFormKey="VITALS",
                    sourceVariableName="arm_code",
                )
            ],
        )
        pipeline = EnrichmentPipeline(study_cfg)
        mapper = CentralizedMapper(mode="document", post_processor=_post_process_warehouse)
        mapper._pipeline_cache["STUDY_HETERO"] = pipeline

        arrow_table = _records_to_arrow_table(
            records,
            study_key="STUDY_HETERO",
            mapper=mapper,
        )
        assert isinstance(arrow_table, pa.Table)
        assert arrow_table.num_rows == 3
        assert pa.types.is_string(
            arrow_table.schema.field("record_data").type
        ) or pa.types.is_large_string(arrow_table.schema.field("record_data").type)

        parquet_path = tmp_path / "heterogeneous_batch.parquet"
        pq.write_table(arrow_table, str(parquet_path))
        reloaded = pq.read_table(str(parquet_path)).to_pylist()

        assert reloaded[0]["study_key"] == "STUDY_HETERO"
        assert reloaded[0]["record_id"] == 101
        assert reloaded[0]["subject_key"] == "SUBJ-01"
        assert reloaded[0]["form_key"] == "VITALS"
        assert [kw["keyword_name"] for kw in json.loads(reloaded[0]["keywords"])] == [
            "urgent",
            "baseline",
        ]
        payload_0 = json.loads(reloaded[0]["record_data"])
        assert payload_0["lab_value"] == 42
        assert payload_0["assessment"] == {"grade": 1}
        assert payload_0["treatment_arm"] == "Active"
        assert payload_0["patient_name"] == "***MASKED***"

        assert reloaded[1]["study_key"] == "STUDY_HETERO"
        assert reloaded[1]["record_id"] == 102
        assert reloaded[1]["subject_key"] == "SUBJ-02"
        assert reloaded[1]["form_key"] == "LABS"
        payload_1 = json.loads(reloaded[1]["record_data"])
        assert payload_1["lab_value"] == "below_detection_limit"
        assert payload_1["assessment"] == "not_done"
        assert payload_1["treatment_arm"] == "Placebo"
        assert payload_1["ssn"] == "***MASKED***"

        assert reloaded[2]["record_id"] == 103
        assert json.loads(reloaded[2]["keywords"]) == [{"keyword_name": "serious", "keyword_id": 9}]
        payload_2 = json.loads(reloaded[2]["record_data"])
        assert payload_2["lab_value"] == [1.5, 2.5]
        assert payload_2["treatment_arm"] == "Active"

    def test_write_batch_idempotent_merges_and_deduplicates_scheduled_reruns(
        self, monkeypatch, tmp_path
    ):
        """Verify idempotent write_batch overwrites staged Parquet and merges by (study_key, record_id) without duplicating rows."""
        import json

        import imednet_sinks.warehouse as wh_mod
        import pyarrow.parquet as pq
        from imednet_sinks.warehouse import SnowflakeExportSink, SnowflakeSinkConfig

        from imednet.models.records import Record

        sf, conn, cursor = _fake_snowflake_module()
        orig_require = wh_mod._require_optional_dep

        def require_with_real_pyarrow(pkg, extras):
            if pkg == "snowflake.connector":
                return sf
            return orig_require(pkg, extras)

        monkeypatch.setattr(wh_mod, "_require_optional_dep", require_with_real_pyarrow)

        # Simulate Snowflake target table and staging table across scheduled re-runs
        warehouse_store: dict[tuple[str, int], dict[str, object]] = {}
        staged_rows: list[dict[str, object]] = []
        last_staged_parquet: list[str] = []

        def fake_execute(sql: str):
            nonlocal staged_rows
            if sql.startswith("PUT file://"):
                assert "OVERWRITE = TRUE" in sql
                local_file = sql.split("PUT file://", 1)[1].split(" ", 1)[0]
                last_staged_parquet.append(local_file)
                staged_rows = pq.read_table(local_file).to_pylist()
                cursor.fetchall.return_value = []
            elif sql.startswith("MERGE INTO"):
                assert (
                    "ON target.study_key = source.study_key AND target.record_id = source.record_id"
                    in sql
                )
                assert "WHEN MATCHED THEN UPDATE SET" in sql
                assert "WHEN NOT MATCHED THEN INSERT" in sql
                for row in staged_rows:
                    key = (str(row["study_key"]), int(row["record_id"]))
                    warehouse_store[key] = dict(row)
                cursor.fetchall.return_value = []
            else:
                cursor.fetchall.return_value = []

        cursor.execute.side_effect = fake_execute

        cfg = SnowflakeSinkConfig(
            study_key="STUDY_MERGE",
            account="acct",
            user="loader",
            password="secret",  # pragma: allowlist secret
            database="DB",
            schema="PUBLIC",
            warehouse="WH",
            stage="STG",
            table="RECORDS",
            local_staging_dir=str(tmp_path),
            idempotent=True,
        )

        first_run = [
            Record(
                record_id=1,
                form_id=10,
                form_key="F1",
                subject_key="S1",
                record_status="Incomplete",
                record_data={"score": 10},
            ),
            Record(
                record_id=2,
                form_id=20,
                form_key="F2",
                subject_key="S2",
                record_status="Complete",
                record_data={"score": "pending"},
            ),
        ]
        second_run = [
            Record(
                record_id=1,
                form_id=10,
                form_key="F1",
                subject_key="S1",
                record_status="Complete",
                record_data={"score": 99},
            ),
            Record(
                record_id=3,
                form_id=20,
                form_key="F2",
                subject_key="S3",
                record_status="Complete",
                record_data={"score": "final"},
            ),
        ]

        with SnowflakeExportSink(config=cfg) as sink:
            assert sink.write_batch(first_run, batch_id="STUDY_MERGE/batch/0") == 2
            assert sink.write_batch(second_run, batch_id="STUDY_MERGE/batch/0") == 2

        assert len(warehouse_store) == 3
        assert warehouse_store[("STUDY_MERGE", 1)]["record_status"] == "Complete"
        assert json.loads(str(warehouse_store[("STUDY_MERGE", 1)]["record_data"])) == {"score": 99}
        assert json.loads(str(warehouse_store[("STUDY_MERGE", 2)]["record_data"])) == {
            "score": "pending"
        }
        assert json.loads(str(warehouse_store[("STUDY_MERGE", 3)]["record_data"])) == {
            "score": "final"
        }

    def test_write_batch_non_idempotent_uses_direct_copy_into_force_true(
        self, monkeypatch, tmp_path
    ):
        """Verify idempotent=False overwrites staged file via PUT and executes direct COPY INTO with FORCE = TRUE."""
        import imednet_sinks.warehouse as wh_mod
        from imednet_sinks.warehouse import SnowflakeExportSink, SnowflakeSinkConfig

        sf, conn, cursor = _fake_snowflake_module()
        pa, pq, _table = _fake_pyarrow_modules()
        monkeypatch.setattr(
            wh_mod,
            "_require_optional_dep",
            lambda pkg, extras: (
                sf if pkg == "snowflake.connector" else (pq if "parquet" in pkg else pa)
            ),
        )

        cfg = SnowflakeSinkConfig(
            study_key="STUDY1",
            account="acct",
            user="user",
            password="secret",  # pragma: allowlist secret
            database="DB",
            schema="PUBLIC",
            warehouse="WH",
            stage="STG",
            table="TBL",
            local_staging_dir=str(tmp_path),
            idempotent=False,
        )
        with SnowflakeExportSink(config=cfg) as sink:
            sink.write_batch(
                [MagicMock(record_id=1, form_id=1, visit_id=1, subject_key="S", record_data={})],
                batch_id="STUDY1/batch/0",
            )

        calls = [c.args[0] for c in cursor.execute.call_args_list if c.args]
        put_call = next(c for c in calls if "PUT" in c)
        copy_call = next(c for c in calls if "COPY INTO" in c)
        assert "OVERWRITE = TRUE" in put_call
        assert 'COPY INTO "DB"."PUBLIC"."TBL"' in copy_call
        assert "FORCE = TRUE" in copy_call
        assert not any("MERGE INTO" in c for c in calls)


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


class TestSinkPhiMaskingControls:
    """Verify default ***MASKED*** PHI redaction and mask_phi=False opt-out across all destination sinks."""

    @staticmethod
    def _make_study1_phi_record() -> SimpleNamespace:
        return SimpleNamespace(
            record_id=9001,
            study_key="STUDY1",
            subject_id=101,
            subject_key="SUBJ-9001",
            visit_id=1,
            form_id=10,
            form_key="DEMOGRAPHICS",
            record_status="Complete",
            deleted=False,
            date_created="2025-01-01T00:00:00Z",
            date_modified="2025-01-02T00:00:00Z",
            record_data={
                "patientName": "Jane Doe",
                "DOB": "1985-07-19",
                "SSN": "123-45-6789",
                "email": "jane.doe@clinical.example",
                "systolicBp": 122,
            },
        )

    def test_mongodb_sink_masks_phi_by_default_and_honors_opt_out(self, monkeypatch):
        """MongoDbExportSink replaces patientName, DOB, SSN, and email with ***MASKED*** unless mask_phi=False."""
        import imednet_sinks.document as doc_mod
        from imednet_sinks.document import MongoDbExportSink, MongoDbSinkConfig

        from imednet.utils.security import global_sensitivity_registry

        registry_before = set(global_sensitivity_registry._sensitive_keys)
        pymongo, _client, _collection = _fake_pymongo_module()
        monkeypatch.setattr(doc_mod, "_require_optional_dep", lambda *_: pymongo)

        record = self._make_study1_phi_record()
        with MongoDbExportSink(
            MongoDbSinkConfig(
                study_key="STUDY1",
                uri="mongodb://localhost:27017",
                database="clinical",
                collection="records",
            )
        ) as sink:
            sink.write_batch([record], batch_id="STUDY1/DEMOGRAPHICS/0")

        _flt, update_doc = pymongo.UpdateOne.call_args.args[:2]
        doc = update_doc["$set"]
        assert doc["study_key"] == "STUDY1"
        assert doc["subject_key"] == "SUBJ-9001"
        assert doc["record_id"] == 9001
        assert doc["record_data"] == {
            "patient_name": "***MASKED***",
            "dob": "***MASKED***",
            "ssn": "***MASKED***",
            "email": "***MASKED***",
            "systolic_bp": 122,
        }

        pymongo.UpdateOne.reset_mock()
        with MongoDbExportSink(
            MongoDbSinkConfig(
                study_key="STUDY1",
                uri="mongodb://localhost:27017",
                database="clinical",
                collection="records",
                mask_phi=False,
            )
        ) as sink_unmasked:
            sink_unmasked.write_batch([record], batch_id="STUDY1/DEMOGRAPHICS/1")

        _flt2, update_doc_unmasked = pymongo.UpdateOne.call_args.args[:2]
        assert update_doc_unmasked["$set"]["record_data"] == {
            "patient_name": "Jane Doe",
            "dob": "1985-07-19",
            "ssn": "123-45-6789",
            "email": "jane.doe@clinical.example",
            "systolic_bp": 122,
        }
        assert global_sensitivity_registry._sensitive_keys == registry_before

    def test_neo4j_sink_masks_phi_by_default_and_honors_opt_out(self, monkeypatch):
        """Neo4jExportSink replaces patientName, DOB, SSN, and email with ***MASKED*** while preserving graph keys."""
        import json

        import imednet_sinks.graph as graph_mod
        from imednet_sinks.graph import Neo4jExportSink, Neo4jSinkConfig

        neo4j, driver = _fake_neo4j_module()
        monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j)

        record = self._make_study1_phi_record()
        with Neo4jExportSink(
            Neo4jSinkConfig(
                study_key="STUDY1",
                uri="bolt://localhost:7687",
                auth=("neo4j", "testpass"),
            )
        ) as sink:
            sink.write_batch([record], batch_id="STUDY1/DEMOGRAPHICS/0")

        session_mock = driver.session.return_value.__enter__.return_value
        row = session_mock.run.call_args.kwargs["rows"][0]
        assert row["study_key"] == "STUDY1"
        assert row["subject_key"] == "SUBJ-9001"
        assert row["record_id"] == 9001
        assert json.loads(row["record_data"]) == {
            "patient_name": "***MASKED***",
            "dob": "***MASKED***",
            "ssn": "***MASKED***",
            "email": "***MASKED***",
            "systolic_bp": 122,
        }

        session_mock.run.reset_mock()
        with Neo4jExportSink(
            Neo4jSinkConfig(
                study_key="STUDY1",
                uri="bolt://localhost:7687",
                auth=("neo4j", "testpass"),
                mask_phi=False,
            )
        ) as sink_unmasked:
            sink_unmasked.write_batch([record], batch_id="STUDY1/DEMOGRAPHICS/1")

        unmasked_row = session_mock.run.call_args.kwargs["rows"][0]
        assert json.loads(unmasked_row["record_data"])["patient_name"] == "Jane Doe"
        assert json.loads(unmasked_row["record_data"])["ssn"] == "123-45-6789"

    def test_snowflake_sink_masks_phi_by_default_and_honors_opt_out(self, monkeypatch, tmp_path):
        """SnowflakeExportSink masks sensitive fields to ***MASKED*** in staged Arrow rows unless mask_phi=False."""
        import json

        import imednet_sinks.warehouse as wh_mod
        from imednet_sinks.warehouse import SnowflakeExportSink, SnowflakeSinkConfig

        sf, _conn, _cursor = _fake_snowflake_module()
        pa, pq, _table = _fake_pyarrow_modules()

        def fake_require(pkg, _extras):
            if pkg == "snowflake.connector":
                return sf
            if pkg == "pyarrow":
                return pa
            if pkg == "pyarrow.parquet":
                return pq
            return MagicMock()

        monkeypatch.setattr(wh_mod, "_require_optional_dep", fake_require)

        record = self._make_study1_phi_record()
        placeholder = "test-only"
        base_kwargs = {
            "study_key": "STUDY1",
            "account": "acct",
            "user": "usr",
            "password": placeholder,
            "database": "DB",
            "schema": "PUBLIC",
            "warehouse": "WH",
            "stage": "STG",
            "table": "RECORDS",
            "local_staging_dir": str(tmp_path),
        }

        with SnowflakeExportSink(SnowflakeSinkConfig(**base_kwargs)) as sink:
            sink.write_batch([record], batch_id="STUDY1/DEMOGRAPHICS/0")

        staged_rows = pa.Table.from_pylist.call_args.args[0]
        assert staged_rows[0]["study_key"] == "STUDY1"
        assert staged_rows[0]["subject_key"] == "SUBJ-9001"
        assert staged_rows[0]["record_id"] == 9001
        assert json.loads(staged_rows[0]["record_data"]) == {
            "patient_name": "***MASKED***",
            "dob": "***MASKED***",
            "ssn": "***MASKED***",
            "email": "***MASKED***",
            "systolic_bp": 122,
        }

        pa.Table.from_pylist.reset_mock()
        with SnowflakeExportSink(
            SnowflakeSinkConfig(**base_kwargs, mask_phi=False)
        ) as sink_opt_out:
            sink_opt_out.write_batch([record], batch_id="STUDY1/DEMOGRAPHICS/1")

        unmasked_rows = pa.Table.from_pylist.call_args.args[0]
        assert json.loads(unmasked_rows[0]["record_data"]) == {
            "patient_name": "Jane Doe",
            "dob": "1985-07-19",
            "ssn": "123-45-6789",
            "email": "jane.doe@clinical.example",
            "systolic_bp": 122,
        }

    def test_export_to_neo4j_enforces_quality_gate_before_batch_writes(self, monkeypatch):
        """Setting quality_gate_enabled=True on Neo4jSinkConfig drops low-readiness Record items via apply_quality_gate."""
        import json

        import imednet_sinks.graph as graph_mod
        from imednet_sinks.graph import Neo4jSinkConfig, export_to_neo4j

        from imednet.models.records import Record
        from imednet.models.variables import Variable

        neo4j, driver = _fake_neo4j_module()
        monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j)

        sdk_mock = MagicMock()
        sdk_mock.get_variables.return_value = [
            Variable(variable_name="age", variable_type="int", form_id=10, form_key="DEMO"),
            Variable(variable_name="weight", variable_type="int", form_id=10, form_key="DEMO"),
        ]
        valid_rec = Record(
            record_id=1,
            study_key="STUDY1",
            subject_key="SUBJ-1",
            visit_id=1,
            form_id=10,
            form_key="DEMO",
            record_data={"age": 35, "weight": 70, "SSN": "123-45-6789"},
        )
        invalid_rec = Record(
            record_id=2,
            study_key="STUDY1",
            subject_key="SUBJ-2",
            visit_id=1,
            form_id=10,
            form_key="DEMO",
            record_data={"age": "not-an-int", "weight": "bad"},
        )
        sdk_mock.records.list.return_value = [valid_rec, invalid_rec]

        cfg = Neo4jSinkConfig(
            study_key="STUDY1",
            uri="bolt://localhost:7687",
            auth=("neo4j", "pass"),
            quality_gate_enabled=True,
            min_schema_readiness_score=90.0,
        )
        exported = export_to_neo4j(sdk_mock, "STUDY1", config=cfg)
        assert exported == 1

        session_mock = driver.session.return_value.__enter__.return_value
        rows = session_mock.run.call_args.kwargs["rows"]
        assert len(rows) == 1
        assert rows[0]["record_id"] == 1
        assert json.loads(rows[0]["record_data"])["ssn"] == "***MASKED***"

    def test_all_sinks_mask_six_sensitive_keys_and_apply_committed_study_config(
        self, monkeypatch, tmp_path
    ):
        """Snowflake, MongoDB, and Neo4j mask all 6 sensitive keys plus committed StudyConfiguration phiFields."""
        import json

        import imednet_sinks.document as doc_mod
        import imednet_sinks.graph as graph_mod
        import imednet_sinks.warehouse as wh_mod

        from imednet.integrations import enrichment as enrichment_mod
        from imednet.models.records import Record
        from imednet.models.study_config import StudyConfiguration

        committed_cfg = StudyConfiguration(
            study_key="STUDY-GOV",
            phi_fields=["customMrn"],
            terminology_lookups={"gender": {"2": "Female"}},
        )
        monkeypatch.setattr(
            enrichment_mod,
            "resolve_study_configuration",
            lambda sk: committed_cfg if sk == "STUDY-GOV" else None,
        )

        rec = Record(
            record_id=301,
            study_key="STUDY-GOV",
            subject_key="SUBJ-301",
            visit_id=1,
            form_id=10,
            form_key="DEMO",
            record_data={
                "SSN": "123-45-6789",
                "DOB": "1990-02-14",
                "patientName": "Clara Barton",
                "email": "clara@example.org",
                "phone": "555-0144",
                "address": "42 Health Ave",
                "customMrn": "MRN-301",
                "gender": "2",
            },
        )
        sdk_mock = MagicMock()
        sdk_mock.records.list.side_effect = lambda **_kw: [rec.model_copy(deep=True)]

        # 1. Neo4j
        neo4j, driver = _fake_neo4j_module()
        monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j)
        assert (
            graph_mod.export_to_neo4j(sdk_mock, "STUDY-GOV", "bolt://localhost:7687", ("n", "p"))
            == 1
        )
        neo_payload = json.loads(
            driver.session.return_value.__enter__.return_value.run.call_args.kwargs["rows"][0][
                "record_data"
            ]
        )
        for k in ("ssn", "dob", "patient_name", "email", "phone", "address", "custom_mrn"):
            assert neo_payload[k] == "***MASKED***"
        assert neo_payload["gender"] == "Female"

        # 2. MongoDB
        pymongo, _client, _col = _fake_pymongo_module()
        monkeypatch.setattr(doc_mod, "_require_optional_dep", lambda *_: pymongo)
        assert (
            doc_mod.export_to_mongodb(sdk_mock, "STUDY-GOV", "mongodb://localhost", "db", "col")
            == 1
        )
        mongo_payload = pymongo.UpdateOne.call_args.args[1]["$set"]["record_data"]
        for k in ("ssn", "dob", "patient_name", "email", "phone", "address", "custom_mrn"):
            assert mongo_payload[k] == "***MASKED***"
        assert mongo_payload["gender"] == "Female"

        # 3. Snowflake (and mask_phi=False explicit opt-out override)
        sf, _conn, _cursor = _fake_snowflake_module()
        pa, pq, _table = _fake_pyarrow_modules()
        monkeypatch.setattr(
            wh_mod,
            "_require_optional_dep",
            lambda pkg, _ex: (
                sf if pkg == "snowflake.connector" else (pa if pkg == "pyarrow" else pq)
            ),
        )
        sf_cfg = wh_mod.SnowflakeSinkConfig(
            study_key="STUDY-GOV",
            account="a",
            user="u",
            password="p",  # pragma: allowlist secret
            database="DB",
            schema="PUBLIC",
            warehouse="WH",
            stage="STG",
            table="TBL",
            local_staging_dir=str(tmp_path),
        )
        assert wh_mod.export_to_snowflake(sdk_mock, "STUDY-GOV", config=sf_cfg) == 1
        sf_payload = json.loads(pa.Table.from_pylist.call_args.args[0][0]["record_data"])
        for k in ("ssn", "dob", "patient_name", "email", "phone", "address", "custom_mrn"):
            assert sf_payload[k] == "***MASKED***"
        assert sf_payload["gender"] == "Female"

        pa.Table.from_pylist.reset_mock()
        assert wh_mod.export_to_snowflake(sdk_mock, "STUDY-GOV", config=sf_cfg, mask_phi=False) == 1
        sf_unmasked = json.loads(pa.Table.from_pylist.call_args.args[0][0]["record_data"])
        assert sf_unmasked["ssn"] == "123-45-6789"
        assert sf_unmasked["custom_mrn"] == "MRN-301"
        assert sf_unmasked["gender"] == "Female"

    def test_all_sink_plugins_redact_custom_registry_and_study_config_phi_fields(
        self, monkeypatch, tmp_path
    ):
        """MongoDB, Neo4j, and Snowflake sinks redact custom global_sensitivity_registry keys and case-insensitive StudyConfiguration phi_fields."""
        import json

        import imednet_sinks.document as doc_mod
        import imednet_sinks.graph as graph_mod
        import imednet_sinks.warehouse as wh_mod
        from imednet_sinks.document import MongoDbExportSink, MongoDbSinkConfig
        from imednet_sinks.graph import Neo4jExportSink, Neo4jSinkConfig
        from imednet_sinks.warehouse import SnowflakeExportSink, SnowflakeSinkConfig

        from imednet.models.records import Record
        from imednet.models.study_config import StudyConfiguration
        from imednet.spi.utils import global_sensitivity_registry

        snapshot = set(global_sensitivity_registry._sensitive_keys)
        global_sensitivity_registry.add_sensitive_key("customGeneticMarker")

        try:
            study_cfg = StudyConfiguration(
                studyKey="STUDY_SINK_PHI",
                phiFields=["siteChartId"],
            )
            record = Record(
                record_id=9500,
                study_key="STUDY_SINK_PHI",
                subject_key="SUBJ-9500",
                visit_id=1,
                form_id=10,
                form_key="GENOMICS",
                record_data={
                    "customGeneticMarker": "BRCA-RAW",
                    "SITE_CHART_ID": "CHART-99",
                    "patientName": "Jane Doe",
                    "age": 51,
                },
            )

            # 1. MongoDB
            pymongo, _client, _collection = _fake_pymongo_module()
            monkeypatch.setattr(doc_mod, "_require_optional_dep", lambda *_: pymongo)
            with MongoDbExportSink(
                MongoDbSinkConfig(
                    study_key="STUDY_SINK_PHI",
                    uri="mongodb://localhost:27017",
                    database="clinical",
                    collection="records",
                    extra={"study_config": study_cfg},
                )
            ) as mongo_sink:
                mongo_sink.write_batch([record], batch_id="STUDY_SINK_PHI/GENOMICS/0")

            _flt, update_doc = pymongo.UpdateOne.call_args.args[:2]
            mongo_doc = update_doc["$set"]
            assert mongo_doc["subject_key"] == "SUBJ-9500"
            assert mongo_doc["record_data"]["custom_genetic_marker"] == "***MASKED***"
            assert mongo_doc["record_data"]["site_chart_id"] == "***MASKED***"
            assert mongo_doc["record_data"]["patient_name"] == "***MASKED***"
            assert mongo_doc["record_data"]["age"] == 51

            # 2. Neo4j
            neo4j_mod, driver = _fake_neo4j_module()
            session_mock = driver.session.return_value.__enter__.return_value
            monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j_mod)
            with Neo4jExportSink(
                Neo4jSinkConfig(
                    study_key="STUDY_SINK_PHI",
                    uri="bolt://localhost:7687",
                    auth=("neo4j", "placeholder"),
                    extra={"study_config": study_cfg},
                )
            ) as neo4j_sink:
                neo4j_sink.write_batch([record], batch_id="STUDY_SINK_PHI/GENOMICS/0")

            neo4j_row = session_mock.run.call_args.kwargs["rows"][0]
            assert neo4j_row["subject_key"] == "SUBJ-9500"
            neo4j_payload = json.loads(neo4j_row["record_data"])
            assert neo4j_payload["custom_genetic_marker"] == "***MASKED***"
            assert neo4j_payload["site_chart_id"] == "***MASKED***"
            assert neo4j_payload["patient_name"] == "***MASKED***"
            assert neo4j_payload["age"] == 51

            # 3. Snowflake
            sf, _conn, _cursor = _fake_snowflake_module()
            pa, pq, _table = _fake_pyarrow_modules()
            monkeypatch.setattr(
                wh_mod,
                "_require_optional_dep",
                lambda pkg, _ex: (
                    sf if pkg == "snowflake.connector" else (pq if "parquet" in pkg else pa)
                ),
            )
            with SnowflakeExportSink(
                SnowflakeSinkConfig(
                    study_key="STUDY_SINK_PHI",
                    account="acct",
                    user="usr",
                    password="placeholder",  # pragma: allowlist secret
                    database="DB",
                    schema="PUBLIC",
                    warehouse="WH",
                    stage="STG",
                    table="RECORDS",
                    local_staging_dir=str(tmp_path),
                    extra={"study_config": study_cfg},
                )
            ) as sf_sink:
                sf_sink.write_batch([record], batch_id="STUDY_SINK_PHI/GENOMICS/0")

            sf_row = pa.Table.from_pylist.call_args.args[0][0]
            assert sf_row["subject_key"] == "SUBJ-9500"
            sf_payload = json.loads(sf_row["record_data"])
            assert sf_payload["custom_genetic_marker"] == "***MASKED***"
            assert sf_payload["site_chart_id"] == "***MASKED***"
            assert sf_payload["patient_name"] == "***MASKED***"
            assert sf_payload["age"] == 51
        finally:
            global_sensitivity_registry._sensitive_keys.clear()
            global_sensitivity_registry._sensitive_keys.update(snapshot)


# ---------------------------------------------------------------------------
# Snowflake tabular flattening, schema evolution, and hard_delete tests
# ---------------------------------------------------------------------------


class TestSnowflakeSchemaEvolutionAndTabularMerge:
    """Tests for flattened Arrow staging, DDL schema evolution, and soft/hard delete merges."""

    def test_records_to_arrow_table_flattens_variables_prefixes_collisions_and_masks_phi(
        self, tmp_path
    ):
        """Flatten record_data via CentralizedMapper, prefix metadata collisions with var_, and mask PHI."""
        import pyarrow.parquet as pq
        from imednet_sinks.warehouse import _records_to_arrow_table

        from imednet.models.records import Record
        from imednet.spi.sinks import CentralizedMapper

        rec = Record(
            record_id=42,
            study_key="STUDY_FLAT",
            subject_key="SUBJ-042",
            form_id=10,
            form_key="VITALS",
            visit_id=1,
            deleted=False,
            date_modified="2026-04-01T12:00:00Z",
            record_data={
                "systolicBp": 120,
                "vitals": {"heartRate": 72},
                "readings": [98.6, 98.4],
                "patientName": "Alice Example",
                "ssn": "123-45-6789",
                "recordId": "EXT-REC-99",
                "studyKey": "INNER-STUDY",
                "subjectKey": "INNER-SUBJ",
                "deleted": "no",
            },
        )

        table = _records_to_arrow_table([rec], study_key="STUDY_FLAT")
        parquet_file = tmp_path / "flattened.parquet"
        pq.write_table(table, str(parquet_file))
        loaded = pq.read_table(str(parquet_file)).to_pylist()

        assert len(loaded) == 1
        row = loaded[0]
        assert row["study_key"] == "STUDY_FLAT"
        assert row["record_id"] == 42
        assert row["subject_key"] == "SUBJ-042"
        assert row["deleted"] is False
        assert row["systolic_bp"] == 120
        assert row["vitals_heart_rate"] == 72
        assert row["readings_0"] == 98.6
        assert row["readings_1"] == 98.4
        assert row["patient_name"] == "***MASKED***"
        assert row["ssn"] == "***MASKED***"
        assert row["var_record_id"] == "EXT-REC-99"
        assert row["var_study_key"] == "INNER-STUDY"
        assert row["var_subject_key"] == "INNER-SUBJ"
        assert row["var_deleted"] == "no"

        # Explicit CentralizedMapper(mode="tabular") without custom post_processor also flattens safely
        explicit_mapper = CentralizedMapper(mode="tabular", mask_phi=True)
        explicit_table = _records_to_arrow_table(
            [rec], study_key="STUDY_FLAT", mapper=explicit_mapper
        )
        explicit_row = explicit_table.to_pylist()[0]
        assert explicit_row["study_key"] == "STUDY_FLAT"
        assert explicit_row["record_id"] == 42
        assert explicit_row["var_record_id"] == "EXT-REC-99"
        assert explicit_row["patient_name"] == "***MASKED***"

    def test_empty_record_data_batches_serialize_to_parquet_and_merge_without_arrow_error(
        self, monkeypatch, tmp_path
    ):
        """Batches where every record has record_data={} serialize to Parquet without ArrowNotImplementedError."""
        import imednet_sinks.warehouse as wh_mod
        import pyarrow.parquet as pq
        from imednet_sinks.warehouse import (
            SnowflakeExportSink,
            SnowflakeSinkConfig,
            _records_to_arrow_table,
        )

        from imednet.models.records import Record
        from imednet.spi.sinks import CentralizedMapper

        draft_records = [
            Record(
                record_id=1,
                study_key="STUDY_EMPTY",
                subject_key="S-1",
                form_id=1,
                form_key="DRAFT",
                record_status="Incomplete",
                record_data={},
            ),
            Record(
                record_id=2,
                study_key="STUDY_EMPTY",
                subject_key="S-2",
                form_id=1,
                form_key="DRAFT",
                record_status="Incomplete",
                record_data={},
            ),
        ]

        # Direct CentralizedMapper(mode="tabular") path
        arrow_tbl = _records_to_arrow_table(
            draft_records,
            study_key="STUDY_EMPTY",
            mapper=CentralizedMapper(mode="tabular"),
        )
        direct_pq = tmp_path / "draft_direct.parquet"
        pq.write_table(arrow_tbl, str(direct_pq))
        assert [r["record_id"] for r in pq.read_table(str(direct_pq)).to_pylist()] == [1, 2]

        # Full SnowflakeExportSink.write_batch path
        sf, _conn, cursor = _fake_snowflake_module()
        orig_require = wh_mod._require_optional_dep
        monkeypatch.setattr(
            wh_mod,
            "_require_optional_dep",
            lambda pkg, extras: sf if pkg == "snowflake.connector" else orig_require(pkg, extras),
        )
        cursor.fetchall.return_value = []

        cfg = SnowflakeSinkConfig(
            study_key="STUDY_EMPTY",
            account="acct",
            user="loader",
            password="secret",  # pragma: allowlist secret
            database="DB",
            schema="PUBLIC",
            warehouse="WH",
            stage="STG",
            table="DRAFT_RECORDS",
            local_staging_dir=str(tmp_path / "stage"),
        )
        with SnowflakeExportSink(config=cfg) as sink:
            written = sink.write_batch(draft_records, batch_id="STUDY_EMPTY/draft/0")

        assert written == 2
        executed = [c.args[0] for c in cursor.execute.call_args_list]
        assert any(
            'CREATE TABLE IF NOT EXISTS "DB"."PUBLIC"."DRAFT_RECORDS"' in s for s in executed
        )
        assert any('MERGE INTO "DB"."PUBLIC"."DRAFT_RECORDS"' in s for s in executed)

    def test_snowflake_schema_evolution_creates_table_alters_columns_and_deduplicates_batch(
        self, monkeypatch, tmp_path
    ):
        """write_batch creates missing table, runs ALTER TABLE for unseen variables, and deduplicates by date_modified."""
        import re

        import imednet_sinks.warehouse as wh_mod
        import pyarrow.parquet as pq
        from imednet_sinks.warehouse import SnowflakeExportSink, SnowflakeSinkConfig

        from imednet.models.records import Record

        sf, _conn, cursor = _fake_snowflake_module()
        orig_require = wh_mod._require_optional_dep
        monkeypatch.setattr(
            wh_mod,
            "_require_optional_dep",
            lambda pkg, extras: sf if pkg == "snowflake.connector" else orig_require(pkg, extras),
        )

        known_columns: list[str] = []
        warehouse_rows: dict[tuple[str, int], dict[str, object]] = {}
        staged_rows: list[dict[str, object]] = []
        ddl_statements: list[str] = []

        def fake_execute(sql: str):
            nonlocal staged_rows
            if "INFORMATION_SCHEMA.COLUMNS" in sql:
                cursor.fetchall.return_value = [(col,) for col in known_columns]
            elif sql.startswith("CREATE TABLE IF NOT EXISTS"):
                ddl_statements.append(sql)
                cols = re.findall(r'"([A-Z0-9_$]+)"\s+[A-Z_]+', sql.split("(", 1)[1])
                for col in cols:
                    if col not in known_columns:
                        known_columns.append(col)
                cursor.fetchall.return_value = []
            elif sql.startswith("ALTER TABLE"):
                ddl_statements.append(sql)
                cols = re.findall(r'"([A-Z0-9_$]+)"\s+[A-Z_]+', sql.split("ADD COLUMN", 1)[1])
                for col in cols:
                    if col not in known_columns:
                        known_columns.append(col)
                cursor.fetchall.return_value = []
            elif sql.startswith("PUT file://"):
                local_file = sql.split("PUT file://", 1)[1].split(" ", 1)[0]
                staged_rows = pq.read_table(local_file).to_pylist()
                cursor.fetchall.return_value = []
            elif sql.startswith("MERGE INTO"):
                assert (
                    "QUALIFY ROW_NUMBER() OVER (PARTITION BY study_key, record_id ORDER BY date_modified DESC NULLS LAST) = 1"
                    in sql
                )
                deduped: dict[tuple[str, int], dict[str, object]] = {}
                for row in sorted(
                    staged_rows,
                    key=lambda r: str(r.get("date_modified") or ""),
                    reverse=True,
                ):
                    key = (str(row["study_key"]), int(row["record_id"]))
                    if key not in deduped:
                        deduped[key] = row
                for key, row in deduped.items():
                    existing = warehouse_rows.get(key, {})
                    existing.update(row)
                    warehouse_rows[key] = existing
                cursor.fetchall.return_value = []
            else:
                cursor.fetchall.return_value = []

        cursor.execute.side_effect = fake_execute

        cfg = SnowflakeSinkConfig(
            study_key="STUDY_EVOLVE",
            account="acct",
            user="loader",
            password="secret",  # pragma: allowlist secret
            database="DB",
            schema="PUBLIC",
            warehouse="WH",
            stage="STG",
            table="CLINICAL_RECORDS",
            local_staging_dir=str(tmp_path),
        )

        # Batch 1 includes duplicate (STUDY_EVOLVE, 1) with older and newer date_modified
        batch_1 = [
            Record(
                record_id=1,
                form_id=10,
                form_key="VITALS",
                subject_key="S1",
                date_modified="2026-04-01T09:00:00Z",
                record_data={"systolicBp": 118},
            ),
            Record(
                record_id=1,
                form_id=10,
                form_key="VITALS",
                subject_key="S1",
                date_modified="2026-04-01T11:00:00Z",
                record_data={"systolicBp": 124},
            ),
        ]
        # Batch 2 adds protocol amendment variables diastolicBp and assessmentNote
        batch_2 = [
            Record(
                record_id=1,
                form_id=10,
                form_key="VITALS",
                subject_key="S1",
                date_modified="2026-04-02T08:00:00Z",
                record_data={"systolicBp": 120, "diastolicBp": 78, "assessmentNote": "normal"},
            ),
            Record(
                record_id=2,
                form_id=10,
                form_key="VITALS",
                subject_key="S2",
                date_modified="2026-04-02T08:30:00Z",
                record_data={"systolicBp": 130, "diastolicBp": 84, "assessmentNote": "elevated"},
            ),
        ]

        with SnowflakeExportSink(config=cfg) as sink:
            sink.write_batch(batch_1, batch_id="STUDY_EVOLVE/run1/0")
            assert warehouse_rows[("STUDY_EVOLVE", 1)]["systolic_bp"] == 124

            sink.write_batch(batch_2, batch_id="STUDY_EVOLVE/run2/0")

        assert len(ddl_statements) == 2
        assert ddl_statements[0].startswith(
            'CREATE TABLE IF NOT EXISTS "DB"."PUBLIC"."CLINICAL_RECORDS"'
        )
        assert '"SYSTOLIC_BP" BIGINT' in ddl_statements[0]
        assert ddl_statements[1].startswith(
            'ALTER TABLE "DB"."PUBLIC"."CLINICAL_RECORDS" ADD COLUMN'
        )
        assert '"DIASTOLIC_BP" BIGINT' in ddl_statements[1]
        assert '"ASSESSMENT_NOTE" VARCHAR' in ddl_statements[1]
        assert len(warehouse_rows) == 2
        assert warehouse_rows[("STUDY_EVOLVE", 1)]["diastolic_bp"] == 78
        assert warehouse_rows[("STUDY_EVOLVE", 2)]["assessment_note"] == "elevated"

    def test_export_to_snowflake_run_scoped_batches_and_soft_vs_hard_delete(
        self, monkeypatch, tmp_path
    ):
        """export_to_snowflake generates run-scoped batch IDs and handles soft vs hard deletes without duplicates."""
        import imednet_sinks.warehouse as wh_mod
        import pyarrow.parquet as pq
        from imednet_sinks.warehouse import SnowflakeSinkConfig, export_to_snowflake

        from imednet.models.records import Record

        sf, _conn, cursor = _fake_snowflake_module()
        orig_require = wh_mod._require_optional_dep
        monkeypatch.setattr(
            wh_mod,
            "_require_optional_dep",
            lambda pkg, extras: sf if pkg == "snowflake.connector" else orig_require(pkg, extras),
        )

        warehouse_rows: dict[tuple[str, int], dict[str, object]] = {}
        staged_rows: list[dict[str, object]] = []
        copied_stage_paths: list[str] = []

        def fake_execute(sql: str):
            nonlocal staged_rows
            if "INFORMATION_SCHEMA.COLUMNS" in sql:
                cursor.fetchall.return_value = (
                    [{"COLUMN_NAME": k.upper()} for k in next(iter(warehouse_rows.values()))]
                    if warehouse_rows
                    else []
                )
            elif sql.startswith("PUT file://"):
                assert "AUTO_COMPRESS = FALSE OVERWRITE = TRUE" in sql
                local_file = sql.split("PUT file://", 1)[1].split(" ", 1)[0]
                staged_rows = pq.read_table(local_file).to_pylist()
                cursor.fetchall.return_value = []
            elif sql.startswith("COPY INTO") and "__imednet_stage" in sql:
                stage_ref = sql.split("FROM '", 1)[1].split("'", 1)[0]
                copied_stage_paths.append(stage_ref)
                cursor.fetchall.return_value = []
            elif sql.startswith("MERGE INTO"):
                hard_del = 'THEN DELETE' in sql
                for row in staged_rows:
                    key = (str(row["study_key"]), int(row["record_id"]))
                    is_del = bool(row.get("deleted"))
                    if key in warehouse_rows:
                        if hard_del and is_del:
                            del warehouse_rows[key]
                        else:
                            warehouse_rows[key].update(row)
                    elif not (hard_del and is_del):
                        warehouse_rows[key] = dict(row)
                cursor.fetchall.return_value = []
            else:
                cursor.fetchall.return_value = []

        cursor.execute.side_effect = fake_execute

        sdk_mock = MagicMock()
        run1_records = [
            Record(
                record_id=1,
                form_id=1,
                form_key="F1",
                subject_key="S1",
                deleted=False,
                date_modified="2026-04-01T10:00:00Z",
                record_data={"score": 10},
            ),
            Record(
                record_id=2,
                form_id=1,
                form_key="F1",
                subject_key="S2",
                deleted=False,
                date_modified="2026-04-01T10:05:00Z",
                record_data={"score": 20},
            ),
        ]
        # Run 2 (soft delete default): record 1 updated, record 2 marked deleted=True
        run2_records = [
            Record(
                record_id=1,
                form_id=1,
                form_key="F1",
                subject_key="S1",
                deleted=False,
                date_modified="2026-04-02T10:00:00Z",
                record_data={"score": 15},
            ),
            Record(
                record_id=2,
                form_id=1,
                form_key="F1",
                subject_key="S2",
                deleted=True,
                date_modified="2026-04-02T10:05:00Z",
                record_data={"score": 20},
            ),
        ]

        soft_cfg = SnowflakeSinkConfig(
            study_key="STUDY_DEL",
            account="acct",
            user="loader",
            password="secret",  # pragma: allowlist secret
            database="DB",
            schema="PUBLIC",
            warehouse="WH",
            stage="STG",
            table="RECORDS",
            local_staging_dir=str(tmp_path / "soft"),
            hard_delete=False,
        )

        sdk_mock.records.list.side_effect = lambda **_kw: iter(run1_records)
        assert export_to_snowflake(sdk_mock, "STUDY_DEL", config=soft_cfg) == 2

        sdk_mock.records.list.side_effect = lambda **_kw: iter(run2_records)
        assert export_to_snowflake(sdk_mock, "STUDY_DEL", config=soft_cfg) == 2

        # Distinct run-scoped stage paths and soft-delete metadata retained
        assert len(copied_stage_paths) == 2
        assert copied_stage_paths[0] != copied_stage_paths[1]
        assert len(warehouse_rows) == 2
        assert warehouse_rows[("STUDY_DEL", 1)]["score"] == 15
        assert warehouse_rows[("STUDY_DEL", 2)]["deleted"] is True

        # Run 3 (hard_delete=True): matched deleted=True row 2 is deleted; unmatched deleted=True row 99 is not inserted
        run3_records = [
            Record(
                record_id=1,
                form_id=1,
                form_key="F1",
                subject_key="S1",
                deleted=False,
                date_modified="2026-04-03T10:00:00Z",
                record_data={"score": 18},
            ),
            Record(
                record_id=2,
                form_id=1,
                form_key="F1",
                subject_key="S2",
                deleted=True,
                date_modified="2026-04-03T10:05:00Z",
                record_data={"score": 20},
            ),
            Record(
                record_id=99,
                form_id=1,
                form_key="F1",
                subject_key="S99",
                deleted=True,
                date_modified="2026-04-03T10:10:00Z",
                record_data={"score": 0},
            ),
        ]
        hard_cfg = SnowflakeSinkConfig(
            study_key="STUDY_DEL",
            account="acct",
            user="loader",
            password="secret",  # pragma: allowlist secret
            database="DB",
            schema="PUBLIC",
            warehouse="WH",
            stage="STG",
            table="RECORDS",
            local_staging_dir=str(tmp_path / "hard"),
            hard_delete=True,
        )
        sdk_mock.records.list.side_effect = lambda **_kw: iter(run3_records)
        assert export_to_snowflake(sdk_mock, "STUDY_DEL", config=hard_cfg) == 3

        assert list(warehouse_rows.keys()) == [("STUDY_DEL", 1)]
        assert warehouse_rows[("STUDY_DEL", 1)]["score"] == 18

    @pytest.mark.parametrize(
        "bad_record_data",
        [
            {"score; DROP TABLE records": 1},
            {"bad column": 2},
            {"": 3},
            {"nested": {"bad-key": 4}},
            {"empty_unsafe;sql": {}},
        ],
    )
    def test_unsafe_variable_names_and_invalid_hard_delete_fail_before_sql_executes(
        self, monkeypatch, tmp_path, bad_record_data
    ):
        """Unsafe clinical variable names and non-boolean hard_delete raise ExportConfigurationError before SQL execution."""
        import imednet_sinks.warehouse as wh_mod
        from imednet_sinks.warehouse import SnowflakeExportSink, SnowflakeSinkConfig

        from imednet.errors import ExportConfigurationError
        from imednet.models.records import Record

        with pytest.raises(ExportConfigurationError, match="hard_delete"):
            SnowflakeSinkConfig(study_key="S", hard_delete="yes")  # type: ignore[arg-type]

        sf, _conn, cursor = _fake_snowflake_module()
        orig_require = wh_mod._require_optional_dep
        monkeypatch.setattr(
            wh_mod,
            "_require_optional_dep",
            lambda pkg, extras: sf if pkg == "snowflake.connector" else orig_require(pkg, extras),
        )

        cfg = SnowflakeSinkConfig(
            study_key="STUDY_SEC",
            account="acct",
            user="loader",
            password="secret",  # pragma: allowlist secret
            database="DB",
            schema="PUBLIC",
            warehouse="WH",
            stage="STG",
            table="RECORDS",
            local_staging_dir=str(tmp_path),
        )
        rec = Record(
            record_id=1,
            form_id=1,
            form_key="F1",
            subject_key="S1",
            record_data=bad_record_data,
        )
        with SnowflakeExportSink(config=cfg) as sink:
            with pytest.raises(ExportConfigurationError, match="invalid characters"):
                sink.write_batch([rec], batch_id="STUDY_SEC/0")

        cursor.execute.assert_not_called()
