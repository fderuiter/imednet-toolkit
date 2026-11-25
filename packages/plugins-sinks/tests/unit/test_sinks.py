"""TODO: Add docstring."""

from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace
from unittest.mock import ANY, MagicMock

import pytest

from imednet.errors import ExportBatchError, ExportConfigurationError
from imednet_sinks import (
    Neo4jSinkConfig,
)


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
        driver.close.assert_called_once()

    def test_connect_closes_driver_and_suppresses_close_error_on_failure(self, monkeypatch):
        """Neo4jExportSink._connect closes driver, clears _driver, and suppresses secondary close() errors."""
        import imednet_sinks.graph as graph_mod
        from imednet_sinks.graph import Neo4jExportSink

        neo4j, driver = _fake_neo4j_module(fail_connect=True)
        driver.close.side_effect = RuntimeError("secondary close failure")
        monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j)

        sink = Neo4jExportSink.__new__(Neo4jExportSink)
        sink._uri = "bolt://neo4j:topsecret@localhost:7687"  # pragma: allowlist secret
        sink._auth = ("neo4j", "topsecret")  # pragma: allowlist secret
        sink._driver = None

        with pytest.raises(ExportConfigurationError) as exc_info:
            sink._connect()

        driver.close.assert_called_once()
        assert sink._driver is None
        assert "topsecret" not in str(exc_info.value)
        assert "localhost:7687" in str(exc_info.value)

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
        from imednet.integrations.enrichment import CentralizedMapper
        from imednet_sinks.graph import _post_process_graph, _record_to_row

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
        from imednet.models.records import Record
        from imednet_sinks.graph import Neo4jExportSink

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
        assert "CASE WHEN row.visit_id IS NULL" in cypher
        assert "MERGE (su)-[:HAS_RECORD]->(r)" in cypher
        assert "CASE WHEN row.visit_id IS NOT NULL" in graph_mod._CREATE_RECORD_CYPHER
        assert "CASE WHEN row.visit_id IS NULL" in graph_mod._CREATE_RECORD_CYPHER
        assert "MERGE (su)-[:HAS_RECORD]->(r)" in graph_mod._CREATE_RECORD_CYPHER
        row = run_args.kwargs["rows"][0]
        assert row["visit_id"] is None
        assert row["parent_record_id"] is None
        assert json.loads(row["keywords"])[0]["keywordId"] == 5

    @pytest.mark.parametrize("idempotent", [True, False])
    @pytest.mark.parametrize(
        ("convention", "study_prop", "subject_prop", "cypher_visit_prop", "row_visit_prop"),
        [
            ("snake_case", "study_key", "subject_key", "visit_id", "visit_id"),
            ("camelCase", "studyKey", "subjectKey", "visitId", "visitId"),
            ("sdtm", "STUDYID", "USUBJID", "VISIT_ID", "VISITNUM"),
        ],
    )
    def test_write_batch_null_subject_key_guards_subject_and_visit_merges(
        self,
        monkeypatch,
        idempotent: bool,
        convention: str,
        study_prop: str,
        subject_prop: str,
        cypher_visit_prop: str,
        row_visit_prop: str,
    ):
        """Unassigned records (subject_key=None) skip Subject/Visit merges and link directly to Study."""
        import re

        import imednet_sinks.graph as graph_mod
        from imednet.models.records import Record
        from imednet_sinks.graph import Neo4jExportSink

        neo4j, driver = _fake_neo4j_module()
        monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j)

        records = [
            Record(
                record_id=1,
                study_key="STUDY_NULL_SUBJ",
                subject_key=None,
                visit_id=None,
                form_id=10,
                record_data={"note": "unassigned_unscheduled"},
            ),
            Record(
                record_id=2,
                study_key="STUDY_NULL_SUBJ",
                subject_key=None,
                visit_id=5,
                form_id=10,
                record_data={"note": "unassigned_with_visit"},
            ),
            Record(
                record_id=3,
                study_key="STUDY_NULL_SUBJ",
                subject_key="SUBJ-3",
                visit_id=5,
                form_id=10,
                record_data={"note": "assigned_scheduled"},
            ),
            Record(
                record_id=4,
                study_key="STUDY_NULL_SUBJ",
                subject_key="SUBJ-3",
                visit_id=None,
                form_id=10,
                record_data={"note": "assigned_unscheduled"},
            ),
        ]

        with Neo4jExportSink(
            Neo4jSinkConfig(
                study_key="STUDY_NULL_SUBJ",
                uri="bolt://localhost:7687",
                auth=("neo4j", "pass"),  # pragma: allowlist secret
                idempotent=idempotent,
                naming_convention=convention,
            )
        ) as sink:
            assert sink.write_batch(records, batch_id="STUDY_NULL_SUBJ/0") == 4

        run_call = driver.session.return_value.__enter__.return_value.run.call_args
        cypher = run_call.args[0]
        rows = run_call.kwargs["rows"]

        # Outer FOREACH guards Subject and Visit merges on non-null subject property with distinct variable
        outer_match = re.search(
            rf"FOREACH\s*\(\s*(\w+)\s+IN\s+CASE\s+WHEN\s+row\.{subject_prop}\s+IS\s+NOT\s+NULL",
            cypher,
        )
        assert outer_match is not None
        outer_var = outer_match.group(1)

        inner_vars = re.findall(
            rf"FOREACH\s*\(\s*(\w+)\s+IN\s+CASE\s+WHEN\s+row\.{cypher_visit_prop}\s+IS",
            cypher,
        )
        assert len(inner_vars) == 2
        assert all(v != outer_var for v in inner_vars)

        assert f"CASE WHEN row.{cypher_visit_prop} IS NOT NULL" in cypher
        assert f"CASE WHEN row.{cypher_visit_prop} IS NULL" in cypher
        assert f"CASE WHEN row.{subject_prop} IS NULL" in cypher
        assert "MERGE (s)-[:HAS_RECORD]->(r)" in cypher

        assert rows[0][subject_prop] is None
        assert rows[0][row_visit_prop] is None
        assert rows[1][subject_prop] is None
        assert rows[1][row_visit_prop] == 5
        assert rows[2][subject_prop] == "SUBJ-3"
        assert rows[2][row_visit_prop] == 5
        assert rows[3][subject_prop] == "SUBJ-3"
        assert rows[3][row_visit_prop] is None
        assert all(r[study_prop] == "STUDY_NULL_SUBJ" for r in rows)


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
        client.close.assert_called_once()

    def test_connect_closes_client_and_suppresses_close_error_on_failure(self, monkeypatch):
        """MongoDbExportSink._connect closes client, clears _client, and suppresses secondary close() errors."""
        import imednet_sinks.document as doc_mod
        from imednet_sinks.document import MongoDbExportSink

        pymongo, client, _collection = _fake_pymongo_module(fail_connect=True)
        client.close.side_effect = RuntimeError("secondary close failure")
        monkeypatch.setattr(doc_mod, "_require_optional_dep", lambda *_: pymongo)

        sink = MongoDbExportSink.__new__(MongoDbExportSink)
        sink._uri = "mongodb://admin:topsecret@localhost:27017"  # pragma: allowlist secret
        sink._database = "db"
        sink._collection_name = "col"
        sink._client = None
        sink._collection = None

        with pytest.raises(ExportConfigurationError) as exc_info:
            sink._connect()

        client.close.assert_called_once()
        assert sink._client is None
        assert sink._collection is None
        assert "topsecret" not in str(exc_info.value)
        assert "localhost:27017" in str(exc_info.value)

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
        from imednet.integrations.enrichment import CentralizedMapper
        from imednet_sinks.document import _post_process_document, _record_to_document

        record = MagicMock(record_id=10, form_id=1, visit_id=1, subject_key="S1", record_data={})

        # Backward compatibility when mapper is not passed
        doc1 = _record_to_document(record, study_key="STUDY1")
        assert doc1["_id"] == "STUDY1/10"

        # Injected mapper
        custom_mapper = CentralizedMapper(mode="document", post_processor=_post_process_document)
        doc2 = _record_to_document(record, study_key="STUDY1", mapper=custom_mapper)
        assert doc2["_id"] == "STUDY1/10"
        assert "STUDY1" in custom_mapper._pipeline_cache

    def test_write_batch_non_idempotent_assigns_unique_ids_with_bson_and_uuid_fallback(
        self, monkeypatch
    ):
        """MongoDbExportSink(idempotent=False) generates unique _id per Record while keeping default helper _id."""
        import sys
        import uuid

        from bson import ObjectId

        import imednet_sinks.document as doc_mod
        from imednet.models.records import Record
        from imednet_sinks.document import (
            MongoDbExportSink,
            MongoDbSinkConfig,
            _post_process_document,
            _record_to_document,
        )

        stored_ids: set[object] = set()
        inserted_docs: list[dict] = []

        def strict_insert_many(docs, ordered=False):
            batch_ids = []
            for doc in docs:
                doc_id = doc["_id"]
                if doc_id in stored_ids:
                    raise RuntimeError(f"E11000 duplicate key error: {doc_id!r}")
                stored_ids.add(doc_id)
                inserted_docs.append(dict(doc))
                batch_ids.append(doc_id)
            return SimpleNamespace(inserted_ids=batch_ids)

        pymongo, _client, collection = _fake_pymongo_module()
        collection.insert_many.side_effect = strict_insert_many
        monkeypatch.setattr(doc_mod, "_require_optional_dep", lambda *_: pymongo)

        rec = Record(
            record_id=42,
            study_key="STUDY_APPEND",
            subject_key="SUBJ-42",
            visit_id=1,
            form_id=5,
            form_key="AE",
            record_data={"aeterm": "Headache"},
        )

        # Standalone helpers preserve deterministic <study_key>/<record_id> _id
        assert _record_to_document(rec, study_key="STUDY_APPEND")["_id"] == "STUDY_APPEND/42"
        assert (
            _post_process_document({"study_key": "STUDY_APPEND", "record_id": 42})["_id"]
            == "STUDY_APPEND/42"
        )

        # Repeated idempotent=False writes assign distinct ObjectId values when bson is present
        append_cfg = MongoDbSinkConfig(
            study_key="STUDY_APPEND",
            uri="mongodb://localhost:27017",
            database="clinical",
            collection="append_log",
            idempotent=False,
        )
        with MongoDbExportSink(config=append_cfg) as sink:
            assert sink.write_batch([rec, rec], batch_id="STUDY_APPEND/0") == 2
            assert sink.write_batch([rec], batch_id="STUDY_APPEND/1") == 1

        assert len(inserted_docs) == 3
        assert all(isinstance(d["_id"], ObjectId) for d in inserted_docs)
        assert len({d["_id"] for d in inserted_docs}) == 3

        # SDTM + idempotent=False also assigns unique _id per Record across repeated batches
        sdtm_append_cfg = MongoDbSinkConfig(
            study_key="STUDY_APPEND",
            uri="mongodb://localhost:27017",
            database="clinical",
            collection="append_sdtm",
            idempotent=False,
            as_sdtm=True,
            sdtm_domain="AE",
        )
        with MongoDbExportSink(config=sdtm_append_cfg) as sdtm_sink:
            assert sdtm_sink.write_batch([rec], batch_id="STUDY_APPEND/s0") == 1
            assert sdtm_sink.write_batch([rec], batch_id="STUDY_APPEND/s1") == 1
        assert len(inserted_docs) == 5

        # Fall back to UUID strings when bson is unavailable
        monkeypatch.setitem(sys.modules, "bson", None)
        with MongoDbExportSink(config=append_cfg) as fallback_sink:
            assert fallback_sink.write_batch([rec, rec], batch_id="STUDY_APPEND/uuid") == 2

        uuid_docs = inserted_docs[-2:]
        assert uuid_docs[0]["_id"] != uuid_docs[1]["_id"]
        for d in uuid_docs:
            assert isinstance(d["_id"], str)
            assert str(uuid.UUID(d["_id"])) == d["_id"]


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

        from imednet.integrations.enrichment import CentralizedMapper, EnrichmentPipeline
        from imednet.models.records import Keyword, Record
        from imednet.models.study_config import MappingRule, StudyConfiguration
        from imednet_sinks.warehouse import _post_process_warehouse, _records_to_arrow_table

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

        import pyarrow.parquet as pq

        import imednet_sinks.warehouse as wh_mod
        from imednet.models.records import Record
        from imednet_sinks.warehouse import SnowflakeExportSink, SnowflakeSinkConfig

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
        from imednet.integrations.enrichment import CentralizedMapper
        from imednet_sinks.document import _post_process_document, _record_to_document
        from imednet_sinks.graph import _post_process_graph, _record_to_row

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
        from imednet.utils.security import global_sensitivity_registry
        from imednet_sinks.document import MongoDbExportSink, MongoDbSinkConfig

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
        from imednet.models.records import Record
        from imednet.models.variables import Variable
        from imednet_sinks.graph import Neo4jSinkConfig, export_to_neo4j

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
        from imednet.models.records import Record
        from imednet.models.study_config import StudyConfiguration
        from imednet.spi.utils import global_sensitivity_registry
        from imednet_sinks.document import MongoDbExportSink, MongoDbSinkConfig
        from imednet_sinks.graph import Neo4jExportSink, Neo4jSinkConfig
        from imednet_sinks.warehouse import SnowflakeExportSink, SnowflakeSinkConfig

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

    def test_sinks_reuse_mapper_across_fifty_sdtm_batches_with_single_sqlite_decryption(
        self, monkeypatch, tmp_path
    ):
        """50 streaming batches across Snowflake, MongoDB, and Neo4j invoke build_sink_mapper once per sink, decrypt SQLite once, and preserve monotonic SDTM *SEQ."""
        import imednet_sinks.document as doc_mod
        import imednet_sinks.graph as graph_mod
        import imednet_sinks.warehouse as wh_mod
        import imednet_workflows.config_version_control as cvc
        from imednet.models.records import Record
        from imednet.models.study_config import StudyConfiguration
        from imednet_sinks.document import MongoDbExportSink, MongoDbSinkConfig
        from imednet_sinks.graph import Neo4jExportSink, Neo4jSinkConfig
        from imednet_sinks.warehouse import SnowflakeExportSink, SnowflakeSinkConfig
        from imednet_workflows.config_version_control import ConfigVersionStore

        db_file = tmp_path / "fifty_batch_store" / "config_versions.sqlite3"
        monkeypatch.setenv("IMEDNET_CONFIG_DB_PATH", str(db_file))
        monkeypatch.setenv(
            "IMEDNET_SECURITY_KEY", "fifty-batch-test-secret-key"
        )  # pragma: allowlist secret

        sdk_admin = MagicMock()
        sdk_admin.auth.get_user_roles.return_value = ["admin"]
        sdk_admin.auth.get_user_id.return_value = "auditor"

        study_key = "STUDY_50_BATCH"
        store = ConfigVersionStore(db_path=db_file)
        store.commit_config(
            study_key,
            StudyConfiguration(studyKey=study_key, phiFields=["customBatchPhi"]),
            desc="governed PHI for 50-batch sink test",
            sdk=sdk_admin,
        )

        decrypt_calls = 0
        orig_decrypt = cvc._decrypt_payload

        def counting_decrypt(payload: str, *, fallback_key: str | None = None) -> str:
            nonlocal decrypt_calls
            decrypt_calls += 1
            return orig_decrypt(payload, fallback_key=fallback_key)

        rollback_calls = 0
        orig_rollback = ConfigVersionStore.rollback_config

        def counting_rollback(self_store, sk: str, cid: str):
            nonlocal rollback_calls
            rollback_calls += 1
            return orig_rollback(self_store, sk, cid)

        monkeypatch.setattr(cvc, "_decrypt_payload", counting_decrypt)
        monkeypatch.setattr(ConfigVersionStore, "rollback_config", counting_rollback)

        mapper_counts = {"snowflake": 0, "mongodb": 0, "neo4j": 0}
        orig_wh_builder = wh_mod.build_sink_mapper
        orig_doc_builder = doc_mod.build_sink_mapper
        orig_graph_builder = graph_mod.build_sink_mapper

        monkeypatch.setattr(
            wh_mod,
            "build_sink_mapper",
            lambda *a, **kw: (
                mapper_counts.__setitem__("snowflake", mapper_counts["snowflake"] + 1),
                orig_wh_builder(*a, **kw),
            )[1],
        )
        monkeypatch.setattr(
            doc_mod,
            "build_sink_mapper",
            lambda *a, **kw: (
                mapper_counts.__setitem__("mongodb", mapper_counts["mongodb"] + 1),
                orig_doc_builder(*a, **kw),
            )[1],
        )
        monkeypatch.setattr(
            graph_mod,
            "build_sink_mapper",
            lambda *a, **kw: (
                mapper_counts.__setitem__("neo4j", mapper_counts["neo4j"] + 1),
                orig_graph_builder(*a, **kw),
            )[1],
        )

        batches = [
            [
                Record(
                    record_id=idx + 1,
                    study_key=study_key,
                    subject_key="SUBJ-001",
                    visit_id=1,
                    form_id=20,
                    form_key="AE",
                    record_data={
                        "AETERM": f"Event {idx + 1}",
                        "customBatchPhi": f"RAW-{idx + 1}",
                    },
                )
            ]
            for idx in range(50)
        ]

        # 1. SnowflakeExportSink across 50 batches
        sf, _conn, _cursor = _fake_snowflake_module()
        pa, pq, _table = _fake_pyarrow_modules()
        sf_staged_seqs: list[int] = []

        def capture_pylist(rows):
            sf_staged_seqs.append(rows[0]["AESEQ"])
            assert rows[0]["customBatchPhi"] == "***MASKED***"
            tbl = MagicMock()
            tbl.column_names = list(rows[0].keys())
            tbl.num_rows = len(rows)
            return tbl

        pa.Table.from_pylist.side_effect = capture_pylist
        monkeypatch.setattr(
            wh_mod,
            "_require_optional_dep",
            lambda pkg, _ex: (
                sf if pkg == "snowflake.connector" else (pq if "parquet" in pkg else pa)
            ),
        )
        with SnowflakeExportSink(
            SnowflakeSinkConfig(
                study_key=study_key,
                account="acct",
                user="usr",
                password="placeholder",  # pragma: allowlist secret
                database="DB",
                schema="PUBLIC",
                warehouse="WH",
                stage="STG",
                table="AE_SDTM",
                local_staging_dir=str(tmp_path / "sf_stage"),
                as_sdtm=True,
                sdtm_domain="AE",
            )
        ) as sf_sink:
            for idx, batch in enumerate(batches):
                assert sf_sink.write_batch(batch, batch_id=f"{study_key}_AE_{idx}") == 1

        # 2. MongoDbExportSink across 50 batches
        pymongo, _client, _collection = _fake_pymongo_module()
        mongo_seqs: list[int] = []

        def capture_update_one(flt, upd, upsert=False):
            doc = upd["$set"]
            mongo_seqs.append(doc["AESEQ"])
            assert doc["customBatchPhi"] == "***MASKED***"
            return (flt, upd, upsert)

        pymongo.UpdateOne.side_effect = capture_update_one
        monkeypatch.setattr(doc_mod, "_require_optional_dep", lambda *_: pymongo)
        with MongoDbExportSink(
            MongoDbSinkConfig(
                study_key=study_key,
                uri="mongodb://localhost:27017",
                database="clinical",
                collection="ae_sdtm",
                as_sdtm=True,
                sdtm_domain="AE",
            )
        ) as mongo_sink:
            for idx, batch in enumerate(batches):
                assert mongo_sink.write_batch(batch, batch_id=f"{study_key}/AE/{idx}") == 1

        # 3. Neo4jExportSink across 50 batches
        neo4j_mod, driver = _fake_neo4j_module()
        session_mock = driver.session.return_value.__enter__.return_value
        neo4j_seqs: list[int] = []

        def capture_neo4j_run(_cypher, rows=None, **_kw):
            if rows:
                neo4j_seqs.append(rows[0]["AESEQ"])
                assert rows[0]["customBatchPhi"] == "***MASKED***"

        session_mock.run.side_effect = capture_neo4j_run
        monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j_mod)
        with Neo4jExportSink(
            Neo4jSinkConfig(
                study_key=study_key,
                uri="bolt://localhost:7687",
                auth=("neo4j", "placeholder"),
                as_sdtm=True,
                sdtm_domain="AE",
            )
        ) as neo4j_sink:
            for idx, batch in enumerate(batches):
                assert neo4j_sink.write_batch(batch, batch_id=f"{study_key}/AE/{idx}") == 1

        expected_seqs = list(range(1, 51))
        assert sf_staged_seqs == expected_seqs
        assert mongo_seqs == expected_seqs
        assert neo4j_seqs == expected_seqs
        assert mapper_counts == {"snowflake": 1, "mongodb": 1, "neo4j": 1}
        assert decrypt_calls == 1
        assert rollback_calls == 0


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

        from imednet.models.records import Record
        from imednet.spi.sinks import CentralizedMapper
        from imednet_sinks.warehouse import _records_to_arrow_table

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
        import pyarrow.parquet as pq

        import imednet_sinks.warehouse as wh_mod
        from imednet.models.records import Record
        from imednet.spi.sinks import CentralizedMapper
        from imednet_sinks.warehouse import (
            SnowflakeExportSink,
            SnowflakeSinkConfig,
            _records_to_arrow_table,
        )

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

        import pyarrow.parquet as pq

        import imednet_sinks.warehouse as wh_mod
        from imednet.models.records import Record
        from imednet_sinks.warehouse import SnowflakeExportSink, SnowflakeSinkConfig

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

    def test_snowflake_ensure_table_schema_case_sensitive_and_varchar_swap_widening(
        self, monkeypatch, tmp_path
    ):
        """_ensure_table_schema matches exact schema/table case and widens conflicting types to VARCHAR via swap DDL."""
        import re

        import pyarrow.parquet as pq

        import imednet_sinks.warehouse as wh_mod
        from imednet.models.records import Record
        from imednet_sinks.warehouse import SnowflakeExportSink, SnowflakeSinkConfig

        sf, _conn, cursor = _fake_snowflake_module()
        orig_require = wh_mod._require_optional_dep
        monkeypatch.setattr(
            wh_mod,
            "_require_optional_dep",
            lambda pkg, extras: sf if pkg == "snowflake.connector" else orig_require(pkg, extras),
        )

        # Pre-populate an UPPERCASE table ("STUDY_SCHEMA", "SUBJECTRECORDS_V1") to ensure
        # case-sensitive ("study_schema", "SubjectRecords_v1") does NOT collide via UPPER(...)
        catalog: dict[tuple[str, str], dict[str, str]] = {
            ("STUDY_SCHEMA", "SUBJECTRECORDS_V1"): {
                "STUDY_KEY": "TEXT",
                "RECORD_ID": "NUMBER",
            }
        }
        table_data: dict[tuple[str, int], dict[str, object]] = {}
        staged_rows: list[dict[str, object]] = []
        executed_sqls: list[str] = []

        def fake_execute(sql: str) -> None:
            nonlocal staged_rows
            executed_sqls.append(sql)
            if "INFORMATION_SCHEMA.COLUMNS" in sql:
                assert "UPPER(" not in sql
                m_schema = re.search(r"TABLE_SCHEMA = '([^']+)'", sql)
                m_table = re.search(r"TABLE_NAME = '([^']+)'", sql)
                assert m_schema is not None and m_table is not None
                cols_map = catalog.get((m_schema.group(1), m_table.group(1)), {})
                cursor.fetchall.return_value = [
                    {"column_name": c_name, "data_type": c_type}
                    for c_name, c_type in cols_map.items()
                ]
            elif sql.startswith("CREATE TABLE IF NOT EXISTS"):
                col_pairs = re.findall(
                    r'"([A-Za-z0-9_$]+)"\s+(NUMBER\(38,\s*\d+\)|[A-Z_]+)',
                    sql.split("(", 1)[1],
                )
                catalog[("study_schema", "SubjectRecords_v1")] = {
                    c_name: (
                        "NUMBER"
                        if c_type == "BIGINT"
                        else ("TEXT" if c_type == "VARCHAR" else c_type)
                    )
                    for c_name, c_type in col_pairs
                }
                cursor.fetchall.return_value = []
            elif sql.startswith("ALTER TABLE") and "ADD COLUMN" in sql:
                cols_map = catalog[("study_schema", "SubjectRecords_v1")]
                for c_name, c_type in re.findall(
                    r'"([A-Za-z0-9_$]+)"\s+(NUMBER\(38,\s*\d+\)|[A-Z_]+)',
                    sql.split("ADD COLUMN", 1)[1],
                ):
                    cols_map[c_name] = c_type
                cursor.fetchall.return_value = []
            elif sql.startswith("UPDATE ") and "TO_VARCHAR(" in sql:
                m_upd = re.search(r'SET "([^"]+)" = TO_VARCHAR\("([^"]+)"\)', sql)
                assert m_upd is not None
                tmp_col, src_col = m_upd.group(1).lower(), m_upd.group(2).lower()
                for row in table_data.values():
                    val = row.get(src_col)
                    row[tmp_col] = None if val is None else str(val)
                cursor.fetchall.return_value = []
            elif sql.startswith("ALTER TABLE") and "DROP COLUMN" in sql:
                m_drop = re.search(r'DROP COLUMN "([^"]+)"', sql)
                assert m_drop is not None
                dropped = m_drop.group(1)
                catalog[("study_schema", "SubjectRecords_v1")].pop(dropped, None)
                for row in table_data.values():
                    row.pop(dropped.lower(), None)
                cursor.fetchall.return_value = []
            elif sql.startswith("ALTER TABLE") and "RENAME COLUMN" in sql:
                m_ren = re.search(r'RENAME COLUMN "([^"]+)" TO "([^"]+)"', sql)
                assert m_ren is not None
                src_col, dst_col = m_ren.group(1), m_ren.group(2)
                cols_map = catalog[("study_schema", "SubjectRecords_v1")]
                cols_map[dst_col] = cols_map.pop(src_col)
                for row in table_data.values():
                    row[dst_col.lower()] = row.pop(src_col.lower(), None)
                cursor.fetchall.return_value = []
            elif sql.startswith("PUT file://"):
                local_file = sql.split("PUT file://", 1)[1].split(" ", 1)[0]
                staged_rows = pq.read_table(local_file).to_pylist()
                cursor.fetchall.return_value = []
            elif sql.startswith("MERGE INTO"):
                cols_map = catalog[("study_schema", "SubjectRecords_v1")]
                for row in staged_rows:
                    coerced_row = dict(row)
                    for k, v in list(coerced_row.items()):
                        if v is not None and cols_map.get(k.upper()) in ("VARCHAR", "TEXT"):
                            coerced_row[k] = str(v)
                    key = (str(coerced_row["study_key"]), int(coerced_row["record_id"]))
                    existing = table_data.get(key, {})
                    existing.update(coerced_row)
                    table_data[key] = existing
                cursor.fetchall.return_value = []
            else:
                cursor.fetchall.return_value = []

        cursor.execute.side_effect = fake_execute

        cfg = SnowflakeSinkConfig(
            study_key="study_case",
            account="acct",
            user="loader",
            password="secret",  # pragma: allowlist secret
            database="clinical_db",
            schema="study_schema",
            warehouse="compute_wh",
            stage="my_stage",
            table="SubjectRecords_v1",
            local_staging_dir=str(tmp_path),
        )

        with SnowflakeExportSink(config=cfg) as sink:
            # Batch 1: creates mixed-case table despite uppercase sibling in INFORMATION_SCHEMA
            sink.write_batch(
                [
                    Record(
                        record_id=1,
                        form_id=1,
                        subject_key="S1",
                        record_data={"lab_value": 42, "ratio": 1.5, "note": "initial"},
                    )
                ],
                batch_id="b1",
            )
            assert ("study_schema", "SubjectRecords_v1") in catalog
            assert catalog[("study_schema", "SubjectRecords_v1")]["LAB_VALUE"] == "NUMBER"
            assert catalog[("study_schema", "SubjectRecords_v1")]["RATIO"] == "FLOAT"
            assert catalog[("study_schema", "SubjectRecords_v1")]["NOTE"] == "TEXT"

            # Batch 2: all-null lab_value and narrower int note into VARCHAR -> no swap DDL
            before_b2 = len(executed_sqls)
            sink.write_batch(
                [
                    Record(
                        record_id=2,
                        form_id=1,
                        subject_key="S2",
                        record_data={"lab_value": None, "ratio": 2.25, "note": 100},
                    )
                ],
                batch_id="b2",
            )
            b2_sqls = executed_sqls[before_b2:]
            assert not any("TO_VARCHAR" in s or "RENAME COLUMN" in s for s in b2_sqls)
            assert catalog[("study_schema", "SubjectRecords_v1")]["LAB_VALUE"] == "NUMBER"
            assert catalog[("study_schema", "SubjectRecords_v1")]["NOTE"] == "TEXT"

            # Batch 3: string values for lab_value and ratio -> widens both to VARCHAR via swap DDL
            before_b3 = len(executed_sqls)
            sink.write_batch(
                [
                    Record(
                        record_id=3,
                        form_id=1,
                        subject_key="S3",
                        record_data={
                            "lab_value": "below_detection_limit",
                            "ratio": "not_applicable",
                            "note": "text_again",
                        },
                    )
                ],
                batch_id="b3",
            )
            b3_sqls = executed_sqls[before_b3:]
            assert any('ADD COLUMN "LAB_VALUE__IMEDNET_TMP" VARCHAR' in s for s in b3_sqls)
            assert any(
                'SET "LAB_VALUE__IMEDNET_TMP" = TO_VARCHAR("LAB_VALUE")' in s for s in b3_sqls
            )
            assert any('DROP COLUMN "LAB_VALUE"' in s for s in b3_sqls)
            assert any(
                'RENAME COLUMN "LAB_VALUE__IMEDNET_TMP" TO "LAB_VALUE"' in s for s in b3_sqls
            )
            assert catalog[("study_schema", "SubjectRecords_v1")]["LAB_VALUE"] == "VARCHAR"
            assert catalog[("study_schema", "SubjectRecords_v1")]["RATIO"] == "VARCHAR"

            # Batch 4: narrower numeric types into widened VARCHAR columns -> kept as VARCHAR without DDL
            before_b4 = len(executed_sqls)
            sink.write_batch(
                [
                    Record(
                        record_id=4,
                        form_id=1,
                        subject_key="S4",
                        record_data={"lab_value": 99, "ratio": 3.5, "note": "done"},
                    )
                ],
                batch_id="b4",
            )
            b4_sqls = executed_sqls[before_b4:]
            assert not any("ALTER TABLE" in s or "TO_VARCHAR" in s for s in b4_sqls)
            assert catalog[("study_schema", "SubjectRecords_v1")]["LAB_VALUE"] == "VARCHAR"

        assert table_data[("study_case", 1)]["lab_value"] == "42"
        assert table_data[("study_case", 1)]["ratio"] == "1.5"
        assert table_data[("study_case", 3)]["lab_value"] == "below_detection_limit"
        assert table_data[("study_case", 4)]["lab_value"] == "99"

    def test_export_to_snowflake_run_scoped_batches_and_soft_vs_hard_delete(
        self, monkeypatch, tmp_path
    ):
        """export_to_snowflake generates run-scoped batch IDs and handles soft vs hard deletes without duplicates."""
        import pyarrow.parquet as pq

        import imednet_sinks.warehouse as wh_mod
        from imednet.models.records import Record
        from imednet_sinks.warehouse import SnowflakeSinkConfig, export_to_snowflake

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

        def fake_execute(sql: str, params: object = None):
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
            elif sql.startswith("DELETE FROM"):
                assert isinstance(params, list) and len(params) >= 2
                study_val = str(params[0])
                for rid in params[1:]:
                    warehouse_rows.pop((study_val, int(rid)), None)
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
        # Run 2: record 1 updated, matched record 2 voided, unmatched record 99 voided
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
            Record(
                record_id=99,
                form_id=1,
                form_key="F1",
                subject_key="S99",
                deleted=True,
                date_modified="2026-04-02T10:10:00Z",
                record_data={"score": 0},
            ),
        ]

        default_cfg = SnowflakeSinkConfig(
            study_key="STUDY_DEL",
            account="acct",
            user="loader",
            password="secret",  # pragma: allowlist secret
            database="DB",
            schema="PUBLIC",
            warehouse="WH",
            stage="STG",
            table="RECORDS",
            local_staging_dir=str(tmp_path / "default"),
            include_deleted=False,
            hard_delete=False,
        )

        sdk_mock.records.list.side_effect = lambda **_kw: iter(run1_records)
        assert export_to_snowflake(sdk_mock, "STUDY_DEL", config=default_cfg) == 2

        sdk_mock.records.list.side_effect = lambda **_kw: iter(run2_records)
        assert export_to_snowflake(sdk_mock, "STUDY_DEL", config=default_cfg) == 1

        # Default include_deleted=False deletes matched voided row 2 and excludes unmatched row 99
        assert len(copied_stage_paths) == 2
        assert copied_stage_paths[0] != copied_stage_paths[1]
        assert list(warehouse_rows.keys()) == [("STUDY_DEL", 1)]
        assert warehouse_rows[("STUDY_DEL", 1)]["score"] == 15

        # Explicit include_deleted=True, hard_delete=False retains deleted=True tombstones
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
            include_deleted=True,
            hard_delete=False,
        )
        sdk_mock.records.list.side_effect = lambda **_kw: iter(run1_records)
        assert export_to_snowflake(sdk_mock, "STUDY_DEL", config=soft_cfg) == 2

        sdk_mock.records.list.side_effect = lambda **_kw: iter(run2_records[:2])
        assert export_to_snowflake(sdk_mock, "STUDY_DEL", config=soft_cfg) == 2
        assert len(warehouse_rows) == 2
        assert warehouse_rows[("STUDY_DEL", 1)]["score"] == 15
        assert warehouse_rows[("STUDY_DEL", 2)]["deleted"] is True

        # Explicit include_deleted=True, hard_delete=True deletes matched row 2 and skips unmatched row 99
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
            include_deleted=True,
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
        from imednet.errors import ExportConfigurationError
        from imednet.models.records import Record
        from imednet_sinks.warehouse import SnowflakeExportSink, SnowflakeSinkConfig

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

    def test_single_pass_record_status_keywords_and_deduplicated_checks(
        self, monkeypatch, tmp_path
    ):
        """Standard Record batches normalize RecordStatus/keywords in place and deduplicate batch checks."""
        import pyarrow.parquet as pq

        import imednet_sinks.warehouse as wh_mod
        from imednet.models.records import Record, RecordStatus
        from imednet.spi.utils import global_sensitivity_registry
        from imednet_sinks.warehouse import (
            SnowflakeExportSink,
            SnowflakeSinkConfig,
            _records_to_arrow_table,
        )

        records = [
            Record(
                record_id=1,
                study_key="STUDY_FAST",
                subject_key="SUBJ-1",
                subject_oid="OID-SECRET-1",
                form_id=10,
                form_key="VITALS",
                visit_id=1,
                record_status=RecordStatus.RECORD_COMPLETE,
                deleted=False,
                date_modified="2026-05-01T10:00:00Z",
                record_data={"hr": 70, "patient_name": "Alice Doe"},
            ),
            Record(
                record_id=2,
                study_key="STUDY_FAST",
                subject_key="SUBJ-2",
                subject_oid="OID-SECRET-2",
                form_id=20,
                form_key="LABS",
                visit_id=2,
                record_status=RecordStatus.INCOMPLETE,
                deleted=False,
                date_modified="2026-05-01T10:05:00Z",
                record_data={"glucose": 92.5},
            ),
        ]

        counts = {
            "normalize": 0,
            "omit_empty": 0,
            "scalar_family": 0,
            "validate_keys": 0,
            "is_sensitive": 0,
        }
        orig_norm = wh_mod._normalize_warehouse_value
        orig_omit = wh_mod._omit_empty_structs
        orig_fam = wh_mod._scalar_family
        orig_val = wh_mod._validate_nested_variable_keys
        orig_sens = global_sensitivity_registry.is_sensitive

        def spy_norm(v):
            counts["normalize"] += 1
            return orig_norm(v)

        def spy_omit(r):
            counts["omit_empty"] += 1
            return orig_omit(r)

        def spy_fam(v):
            counts["scalar_family"] += 1
            return orig_fam(v)

        def spy_val(v):
            counts["validate_keys"] += 1
            return orig_val(v)

        def spy_sens(k):
            counts["is_sensitive"] += 1
            return orig_sens(k)

        monkeypatch.setattr(wh_mod, "_normalize_warehouse_value", spy_norm)
        monkeypatch.setattr(wh_mod, "_omit_empty_structs", spy_omit)
        monkeypatch.setattr(wh_mod, "_scalar_family", spy_fam)
        monkeypatch.setattr(wh_mod, "_validate_nested_variable_keys", spy_val)
        monkeypatch.setattr(global_sensitivity_registry, "is_sensitive", spy_sens)

        sf, _conn, cursor = _fake_snowflake_module()
        orig_require = wh_mod._require_optional_dep
        monkeypatch.setattr(
            wh_mod,
            "_require_optional_dep",
            lambda pkg, extras: sf if pkg == "snowflake.connector" else orig_require(pkg, extras),
        )
        cursor.fetchall.return_value = []

        cfg = SnowflakeSinkConfig(
            study_key="STUDY_FAST",
            account="acct",
            user="loader",
            password="secret",  # pragma: allowlist secret
            database="DB",
            schema="PUBLIC",
            warehouse="WH",
            stage="STG",
            table="FAST_RECORDS",
            local_staging_dir=str(tmp_path),
        )
        _records_to_arrow_table(records, study_key="STUDY_FAST")
        assert counts["normalize"] == 0
        assert counts["omit_empty"] == 0
        assert counts["scalar_family"] == 0
        assert counts["validate_keys"] == len(records)
        # 19 canonical metadata keys checked once per batch + 3 total record_data keys across 2 records
        assert counts["is_sensitive"] == (len(wh_mod._RECORD_METADATA_KEYS) - 1) + 3

        for k in counts:
            counts[k] = 0
        with SnowflakeExportSink(config=cfg) as sink:
            assert sink.write_batch(records, batch_id="STUDY_FAST/0") == 2

        assert counts["normalize"] == 0
        assert counts["omit_empty"] == 0
        assert counts["scalar_family"] == 0
        assert counts["validate_keys"] == len(records)

        staged = pq.read_table(str(tmp_path / "STUDY_FAST_0.parquet")).to_pylist()
        assert staged[0]["record_status"] == "Record Complete"
        assert staged[0]["record_status"].__class__ is str
        assert staged[0]["keywords"] == "[]"
        assert staged[0]["patient_name"] == "***MASKED***"
        assert staged[0]["hr"] == 70
        assert staged[0]["glucose"] is None
        assert staged[1]["record_status"] == "Incomplete"
        assert staged[1]["keywords"] == "[]"
        assert staged[1]["hr"] is None
        assert staged[1]["glucose"] == 92.5

        # Dynamically added sensitive metadata key is still picked up per batch
        snapshot = set(global_sensitivity_registry._sensitive_keys)
        global_sensitivity_registry.add_sensitive_key("subject_oid")
        try:
            sens_tbl = _records_to_arrow_table(records, study_key="STUDY_FAST")
            sens_rows = sens_tbl.to_pylist()
            assert sens_rows[0]["subject_oid"] == "***MASKED***"
            assert sens_rows[1]["subject_oid"] == "***MASKED***"
        finally:
            global_sensitivity_registry._sensitive_keys.clear()
            global_sensitivity_registry._sensitive_keys.update(snapshot)

    def test_harmonize_tabular_rows_single_pass_selective_copy_and_other_family(self):
        """_harmonize_tabular_rows aligns rows[0], copies later dicts only on coercion, and skips 'other' family."""
        import pyarrow as pa

        from imednet.models.records import RecordStatus
        from imednet_sinks.warehouse import _harmonize_tabular_rows

        class IntSub(int):
            pass

        # Non-conflicting mixed-form rows align missing keys on rows[0] without copying rows[1:]
        r0 = {"record_id": 1, "hr": 72}
        r1 = {"record_id": 2, "temp": 98.6}
        aligned = _harmonize_tabular_rows([r0, r1])
        assert aligned[0] == {"record_id": 1, "hr": 72, "temp": None}
        assert aligned[0] is not r0
        assert aligned[1] is r1
        tbl = pa.Table.from_pylist(aligned)
        assert tbl.column_names == ["record_id", "hr", "temp"]
        assert tbl.to_pylist() == [
            {"record_id": 1, "hr": 72, "temp": None},
            {"record_id": 2, "hr": None, "temp": 98.6},
        ]

        # Conflicting types copy only rows whose values require conversion; 'other' (MagicMock) skips coercion
        mock_marker = MagicMock()
        c0 = {"record_id": 1, "mixed": IntSub(5), "status": RecordStatus.COMPLETE, "mock_col": 1}
        c1 = {"record_id": 2, "mixed": "already_str", "status": "Incomplete", "mock_col": "txt"}
        c2 = {"record_id": 3, "mixed": [1, 2], "mock_col": mock_marker}
        coerced = _harmonize_tabular_rows([c0, c1, c2])
        assert coerced[0]["mixed"] == "5"
        assert coerced[0]["mock_col"] == 1
        assert coerced[1] is c1  # 'already_str' needed no conversion and mock_col skipped coercion
        assert coerced[2] is not c2
        assert coerced[2]["mixed"] == "[1, 2]"
        assert coerced[2]["mock_col"] is mock_marker

    def test_standalone_post_process_omits_empty_structs_and_validates_string_payloads(self):
        """Standalone _post_process_warehouse omits empty structs, masks PHI, and validates string record_data."""
        import json

        import pyarrow as pa

        from imednet.errors import ExportConfigurationError
        from imednet.models.records import RecordStatus
        from imednet_sinks.warehouse import _post_process_warehouse

        standalone = _post_process_warehouse(
            {
                "record_id": 77,
                "record_status": RecordStatus.VERIFIED,
                "keywords": [],
                "patient_name": "Top Secret",
                "empty_meta": {},
                "nested_empty": {"leaf": {}},
                "valid_meta": {"tag": "v1", "drop_empty": {}},
                "tuple_meta": ("a", "b"),
                "record_data": json.dumps(
                    {"ssn": "999-00-1111", "bp": 118, "nested": {"pulse": 64}}
                ),
            }
        )
        assert standalone["record_status"] == "Verified"
        assert standalone["record_status"].__class__ is str
        assert standalone["keywords"] == "[]"
        assert standalone["patient_name"] == "***MASKED***"
        assert "empty_meta" not in standalone
        assert "nested_empty" not in standalone
        assert standalone["valid_meta"] == {"tag": "v1"}
        assert standalone["tuple_meta"] == ["a", "b"]
        assert standalone["ssn"] == "***MASKED***"
        assert standalone["bp"] == 118
        assert standalone["nested_pulse"] == 64

        # PyArrow infers valid struct for valid_meta without failing on zero-field structs
        arrow_tbl = pa.Table.from_pylist([standalone])
        assert arrow_tbl.num_rows == 1
        assert "empty_meta" not in arrow_tbl.column_names

        # Unsafe keys in standalone Mapping and JSON-string record_data raise ExportConfigurationError
        with pytest.raises(ExportConfigurationError, match="invalid characters"):
            _post_process_warehouse({"record_id": 1, "record_data": {"empty_unsafe;sql": {}}})

        with pytest.raises(ExportConfigurationError, match="invalid characters"):
            _post_process_warehouse(
                {"record_id": 1, "record_data": json.dumps({"empty_unsafe;sql": {}})}
            )


# ---------------------------------------------------------------------------
# Centralized SPI governance, scalar serialization, and Neo4j Subject lineage
# ---------------------------------------------------------------------------


def _simulate_neo4j_cypher_execution(cypher: str, rows: list[dict]) -> dict[str, set]:
    """Execute the node/relationship semantics of _MERGE_RECORD_CYPHER and _CREATE_RECORD_CYPHER in memory."""
    is_merge_record = "MERGE (r:Record" in cypher
    assert "MERGE (s:Study" in cypher
    assert "MERGE (su:Subject {subject_key: row.subject_key, study_key: row.study_key})" in cypher
    assert (
        "MERGE (v:Visit {visit_id: row.visit_id, subject_key: row.subject_key, study_key: row.study_key})"
        in cypher
    )
    assert "MERGE (su)-[:HAS_RECORD]->(r)" in cypher

    studies: set[str] = set()
    subjects: set[tuple[str, str]] = set()
    visits: set[tuple[int, str, str]] = set()
    records: list[tuple[object, dict]] = []
    has_subject: set[tuple[str, tuple[str, str]]] = set()
    has_visit: set[tuple[tuple[str, str], tuple[int, str, str]]] = set()
    visit_has_record: set[tuple[tuple[int, str, str], object]] = set()
    subject_has_record: set[tuple[tuple[str, str], object]] = set()

    for idx, row in enumerate(rows):
        sk = row["study_key"]
        sub_key = (row["subject_key"], sk)
        studies.add(sk)
        subjects.add(sub_key)
        has_subject.add((sk, sub_key))

        rec_node_id: object = (
            (row["record_id"], sk) if is_merge_record else ("created", idx, row["record_id"], sk)
        )
        records.append((rec_node_id, dict(row)))

        if row["visit_id"] is not None:
            v_key = (row["visit_id"], row["subject_key"], sk)
            visits.add(v_key)
            has_visit.add((sub_key, v_key))
            visit_has_record.add((v_key, rec_node_id))
        else:
            subject_has_record.add((sub_key, rec_node_id))

    return {
        "studies": studies,
        "subjects": subjects,
        "visits": visits,
        "records": set(r_id for r_id, _ in records),
        "has_subject": has_subject,
        "has_visit": has_visit,
        "visit_has_record": visit_has_record,
        "subject_has_record": subject_has_record,
    }


class TestCentralizedSpiGovernanceScalarsAndLineage:
    """Verify governance rules, rich scalar serialization, and Neo4j Subject lineage across all sinks."""

    def test_all_sinks_apply_uppercase_camelcase_and_lowercase_governance_rules(
        self, monkeypatch, tmp_path
    ):
        """MongoDB, Neo4j, and Snowflake enforce uppercase, camelCase, and lowercase terminologyLookups and mappings."""
        import json

        import pyarrow.parquet as pq

        import imednet_sinks.document as doc_mod
        import imednet_sinks.graph as graph_mod
        import imednet_sinks.warehouse as wh_mod
        from imednet.models.records import Record
        from imednet.models.study_config import MappingRule, StudyConfiguration
        from imednet_sinks.document import MongoDbExportSink, MongoDbSinkConfig
        from imednet_sinks.graph import Neo4jExportSink, Neo4jSinkConfig
        from imednet_sinks.warehouse import SnowflakeExportSink, SnowflakeSinkConfig

        study_cfg = StudyConfiguration(
            studyKey="STUDY_GOV_ALL",
            phiFields=["chartId", "subjectKey", "recordId", "studyKey"],
            terminologyLookups={
                "SEX": {"1": "Female", "2": "Male"},
                "armCode": {"A": "Active", "B": "Placebo"},
                "cohort": {"c1": "Cohort 1"},
            },
            mappings=[
                MappingRule(
                    domain="DM",
                    targetField="DMSEX",
                    sourceFormKey="DM",
                    sourceVariableName="SEX",
                ),
                MappingRule(
                    domain="DM",
                    targetField="treatmentArm",
                    sourceFormKey="DM",
                    sourceVariableName="armCode",
                    businessLogic="str(value).upper()",
                ),
                MappingRule(
                    domain="DM",
                    targetField="cohort_label",
                    sourceFormKey="DM",
                    sourceVariableName="cohort",
                ),
            ],
        )

        rec = Record(
            record_id=701,
            study_key="STUDY_GOV_ALL",
            subject_key="SUBJ-701",
            visit_id=1,
            form_id=10,
            form_key="DM",
            record_data={
                "SEX": "1",
                "armCode": "A",
                "cohort": "c1",
                "chartId": "RAW-CHART-701",
                "patientName": "Jane Governance",
            },
        )

        # 1. MongoDB
        pymongo, _client, _col = _fake_pymongo_module()
        monkeypatch.setattr(doc_mod, "_require_optional_dep", lambda *_: pymongo)
        with MongoDbExportSink(
            MongoDbSinkConfig(
                study_key="STUDY_GOV_ALL",
                uri="mongodb://localhost:27017",
                database="clinical",
                collection="records",
                extra={"study_config": study_cfg},
            )
        ) as mongo_sink:
            assert mongo_sink.write_batch([rec], batch_id="STUDY_GOV_ALL/DM/0") == 1

        mongo_doc = pymongo.UpdateOne.call_args.args[1]["$set"]
        assert mongo_doc["study_key"] == "STUDY_GOV_ALL"
        assert mongo_doc["subject_key"] == "SUBJ-701"
        assert mongo_doc["record_id"] == 701
        assert mongo_doc["record_data"] == {
            "dmsex": "Female",
            "treatment_arm": "ACTIVE",
            "cohort_label": "Cohort 1",
            "chart_id": "***MASKED***",
            "patient_name": "***MASKED***",
        }

        # 2. Neo4j
        neo4j_mod, driver = _fake_neo4j_module()
        monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j_mod)
        with Neo4jExportSink(
            Neo4jSinkConfig(
                study_key="STUDY_GOV_ALL",
                uri="bolt://localhost:7687",
                auth=("neo4j", "placeholder"),
                extra={"study_config": study_cfg},
            )
        ) as neo_sink:
            assert neo_sink.write_batch([rec], batch_id="STUDY_GOV_ALL/DM/0") == 1

        neo_row = driver.session.return_value.__enter__.return_value.run.call_args.kwargs["rows"][0]
        assert neo_row["study_key"] == "STUDY_GOV_ALL"
        assert neo_row["subject_key"] == "SUBJ-701"
        assert neo_row["record_id"] == 701
        assert json.loads(neo_row["record_data"]) == {
            "dmsex": "Female",
            "treatment_arm": "ACTIVE",
            "cohort_label": "Cohort 1",
            "chart_id": "***MASKED***",
            "patient_name": "***MASKED***",
        }

        # 3. Snowflake (with real PyArrow + Parquet)
        sf, _conn, cursor = _fake_snowflake_module()
        orig_require = wh_mod._require_optional_dep
        monkeypatch.setattr(
            wh_mod,
            "_require_optional_dep",
            lambda pkg, extras: sf if pkg == "snowflake.connector" else orig_require(pkg, extras),
        )
        cursor.fetchall.return_value = []
        with SnowflakeExportSink(
            SnowflakeSinkConfig(
                study_key="STUDY_GOV_ALL",
                account="acct",
                user="loader",
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
            assert sf_sink.write_batch([rec], batch_id="STUDY_GOV_ALL/DM/0") == 1

        staged_parquet = tmp_path / "STUDY_GOV_ALL_DM_0.parquet"
        sf_row = pq.read_table(str(staged_parquet)).to_pylist()[0]
        assert sf_row["study_key"] == "STUDY_GOV_ALL"
        assert sf_row["subject_key"] == "SUBJ-701"
        assert sf_row["record_id"] == 701
        assert sf_row["dmsex"] == "Female"
        assert sf_row["treatment_arm"] == "ACTIVE"
        assert sf_row["cohort_label"] == "Cohort 1"
        assert sf_row["chart_id"] == "***MASKED***"
        assert sf_row["patient_name"] == "***MASKED***"

    def test_mongodb_bson_encode_succeeds_for_keywords_dates_and_exact_decimals(self, monkeypatch):
        """bson.BSON.encode succeeds on _record_to_document output for Keyword, datetime.date, and Decimal values."""
        from datetime import UTC, date
        from decimal import Decimal

        import bson
        from bson import Decimal128

        import imednet_sinks.document as doc_mod
        from imednet.models.records import Keyword, Record, RecordStatus
        from imednet_sinks.document import (
            MongoDbExportSink,
            MongoDbSinkConfig,
            _record_to_document,
        )

        ts_sample = datetime(2026, 5, 1, 8, 30, tzinfo=UTC)
        rec = Record(
            record_id=801,
            study_key="STUDY_BSON",
            subject_key="SUBJ-801",
            visit_id=2,
            form_id=12,
            form_key="LABS",
            record_status=RecordStatus.COMPLETE,
            date_created="2026-05-01T08:30:00Z",
            date_modified="2026-05-02T09:45:00Z",
            keywords=[
                Keyword(
                    keyword_name="Critical Lab",
                    keyword_key="CRIT_LAB",
                    keyword_id=11,
                    date_added=datetime(2026, 5, 2, 10, 0, tzinfo=UTC),
                )
            ],
            record_data={
                "collectionDate": date(2026, 5, 1),
                "collectionTimestamp": ts_sample,
                "troponinNgMl": Decimal("0.00425"),
                "highPrecisionRatio": Decimal("1234567890.0987654321"),
                "nestedPanel": {
                    "repeatDate": date(2026, 5, 2),
                    "readings": (Decimal("1.10"), Decimal("1.20")),
                },
            },
        )

        doc = _record_to_document(rec, study_key="STUDY_BSON")
        assert doc["record_status"] == "Complete"
        assert doc["keywords"] == [
            {
                "keyword_name": "Critical Lab",
                "keyword_key": "CRIT_LAB",
                "keyword_id": 11,
                "date_added": "2026-05-02T10:00:00Z",
            }
        ]
        assert doc["date_created"] == "2026-05-01T08:30:00Z"
        assert doc["record_data"]["collection_date"] == "2026-05-01"
        assert doc["record_data"]["collection_timestamp"] == ts_sample
        assert isinstance(doc["record_data"]["troponin_ng_ml"], Decimal128)
        assert doc["record_data"]["troponin_ng_ml"].to_decimal() == Decimal("0.00425")
        assert doc["record_data"]["high_precision_ratio"].to_decimal() == Decimal(
            "1234567890.0987654321"
        )
        assert doc["record_data"]["nested_panel"] == {
            "repeatDate": "2026-05-02",
            "readings": [Decimal128("1.10"), Decimal128("1.20")],
        }

        raw_bson = bson.BSON.encode(doc)
        decoded = bson.BSON(raw_bson).decode()
        assert decoded["_id"] == "STUDY_BSON/801"
        assert decoded["record_data"]["troponin_ng_ml"].to_decimal() == Decimal("0.00425")
        assert decoded["record_data"]["collection_date"] == "2026-05-01"
        assert decoded["keywords"][0]["keyword_id"] == 11

        # Malformed payload in MongoDbExportSink.write_batch wraps in ExportBatchError
        pymongo, _client, _col = _fake_pymongo_module()
        monkeypatch.setattr(doc_mod, "_require_optional_dep", lambda *_: pymongo)
        with MongoDbExportSink(
            MongoDbSinkConfig(
                study_key="STUDY_BSON",
                uri="mongodb://localhost:27017",
                database="db",
                collection="col",
            )
        ) as sink:
            with pytest.raises(ExportBatchError) as exc_info:
                sink.write_batch([SimpleNamespace(record_data=12345)], batch_id="STUDY_BSON/bad/0")
            assert exc_info.value.batch_id == "STUDY_BSON/bad/0"

    def test_neo4j_serializes_dates_and_decimals_and_wraps_malformed_payloads(self, monkeypatch):
        """Neo4jExportSink.write_batch serializes datetime.date and Decimal values and wraps malformed payloads in ExportBatchError."""
        import json
        from datetime import UTC, date
        from decimal import Decimal

        import imednet_sinks.graph as graph_mod
        from imednet.models.records import Keyword, Record, RecordStatus
        from imednet_sinks.graph import Neo4jExportSink, Neo4jSinkConfig

        neo4j_mod, driver = _fake_neo4j_module()
        monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j_mod)

        rec = Record(
            record_id=802,
            study_key="STUDY_NEO_SCALAR",
            subject_key="SUBJ-802",
            visit_id=3,
            form_id=14,
            form_key="AE",
            record_status=RecordStatus.VERIFIED,
            date_created="2026-05-03T12:00:00Z",
            keywords=[
                Keyword(
                    keyword_name="SAE",
                    keyword_key="SAE_KEY",
                    keyword_id=99,
                    date_added=datetime(2026, 5, 3, 12, 5, tzinfo=UTC),
                )
            ],
            record_data={
                "onsetDate": date(2026, 5, 1),
                "reportedAt": datetime(2026, 5, 3, 12, 0, tzinfo=UTC),
                "doseMg": Decimal("0.00425"),
                "measurements": (Decimal("12.340"), date(2026, 5, 2)),
            },
        )

        with Neo4jExportSink(
            Neo4jSinkConfig(
                study_key="STUDY_NEO_SCALAR",
                uri="bolt://localhost:7687",
                auth=("neo4j", "placeholder"),
            )
        ) as sink:
            assert sink.write_batch([rec], batch_id="STUDY_NEO_SCALAR/AE/0") == 1

            # Malformed payloads raise ExportBatchError without retrying or leaking payload secrets
            for idx, bad_rec in enumerate(
                [
                    SimpleNamespace(
                        record_id=1,
                        subject_key="S1",
                        visit_id=1,
                        record_data={"unserializable": object()},
                    ),
                    SimpleNamespace(
                        record_id=2,
                        subject_key="S1",
                        visit_id=1,
                        record_data=99999,
                    ),
                    SimpleNamespace(
                        record_id=3,
                        subject_key="S1",
                        visit_id=1,
                        record_data={},
                        keywords=[object()],
                    ),
                ]
            ):
                with pytest.raises(ExportBatchError) as caught:
                    sink.write_batch([bad_rec], batch_id=f"STUDY_NEO_SCALAR/bad/{idx}")
                assert caught.value.batch_id == f"STUDY_NEO_SCALAR/bad/{idx}"

        session_mock = driver.session.return_value.__enter__.return_value
        row = session_mock.run.call_args_list[0].kwargs["rows"][0]
        assert row["record_status"] == "Verified"
        assert row["date_created"] == "2026-05-03T12:00:00Z"
        parsed_rd = json.loads(row["record_data"])
        assert parsed_rd == {
            "onset_date": "2026-05-01",
            "reported_at": "2026-05-03T12:00:00+00:00",
            "dose_mg": "0.00425",
            "measurements": ["12.340", "2026-05-02"],
        }
        parsed_kw = json.loads(row["keywords"])
        assert parsed_kw == [
            {
                "keywordName": "SAE",
                "keywordKey": "SAE_KEY",
                "keywordId": 99,
                "dateAdded": "2026-05-03T12:05:00Z",
            }
        ]

    @pytest.mark.parametrize("idempotent", [True, False])
    def test_neo4j_scheduled_and_unscheduled_lineage_without_cross_subject_leakage(
        self, monkeypatch, idempotent
    ):
        """Scheduled and unscheduled Records (visit_id=None or <=0) link to owning Subject without cross-Subject leakage."""
        import imednet_sinks.graph as graph_mod
        from imednet.models.records import Record
        from imednet_sinks.graph import Neo4jExportSink, Neo4jSinkConfig

        neo4j_mod, driver = _fake_neo4j_module()
        monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j_mod)

        batch = [
            # Subject 1: scheduled visit_id=10 + unscheduled visit_id=None + unscheduled visit_id=0
            Record(
                record_id=1,
                study_key="STUDY_LINEAGE",
                subject_key="SUBJ-1",
                visit_id=10,
                form_id=1,
                record_data={"note": "s1_sched"},
            ),
            Record(
                record_id=2,
                study_key="STUDY_LINEAGE",
                subject_key="SUBJ-1",
                visit_id=None,
                form_id=2,
                record_data={"note": "s1_unsched_none"},
            ),
            Record(
                record_id=3,
                study_key="STUDY_LINEAGE",
                subject_key="SUBJ-1",
                visit_id=0,
                form_id=2,
                record_data={"note": "s1_unsched_zero"},
            ),
            # Subject 2: SAME visit_id=10 + unscheduled visit_id=-1
            Record(
                record_id=4,
                study_key="STUDY_LINEAGE",
                subject_key="SUBJ-2",
                visit_id=10,
                form_id=1,
                record_data={"note": "s2_sched"},
            ),
            {
                "record_id": 5,
                "study_key": "STUDY_LINEAGE",
                "subject_key": "SUBJ-2",
                "visit_id": -1,
                "form_id": 2,
                "record_data": {"note": "s2_unsched_neg"},
            },
        ]

        with Neo4jExportSink(
            Neo4jSinkConfig(
                study_key="STUDY_LINEAGE",
                uri="bolt://localhost:7687",
                auth=("neo4j", "placeholder"),
                idempotent=idempotent,
            )
        ) as sink:
            assert sink.write_batch(batch, batch_id="STUDY_LINEAGE/all/0") == 5

        call = driver.session.return_value.__enter__.return_value.run.call_args
        cypher = call.args[0]
        rows = call.kwargs["rows"]
        expected_cypher = (
            graph_mod._MERGE_RECORD_CYPHER if idempotent else graph_mod._CREATE_RECORD_CYPHER
        )
        assert cypher == expected_cypher

        # Non-positive visit_id values (0, -1) are normalized to None
        by_id = {r["record_id"]: r for r in rows}
        assert by_id[1]["visit_id"] == 10
        assert by_id[2]["visit_id"] is None
        assert by_id[3]["visit_id"] is None
        assert by_id[4]["visit_id"] == 10
        assert by_id[5]["visit_id"] is None

        graph = _simulate_neo4j_cypher_execution(cypher, rows)
        assert graph["studies"] == {"STUDY_LINEAGE"}
        assert graph["subjects"] == {("SUBJ-1", "STUDY_LINEAGE"), ("SUBJ-2", "STUDY_LINEAGE")}
        # Two distinct Visit nodes scoped by subject_key even though both use visit_id=10
        assert graph["visits"] == {
            (10, "SUBJ-1", "STUDY_LINEAGE"),
            (10, "SUBJ-2", "STUDY_LINEAGE"),
        }

        # Collect reachable Record IDs per Subject via scheduled (Subject->Visit->Record) + unscheduled (Subject->Record)
        def _subject_record_ids(subject_key: str) -> set[int]:
            sub_node = (subject_key, "STUDY_LINEAGE")
            direct = {
                rec_node[-2] if rec_node[0] == "created" else rec_node[0]
                for s_node, rec_node in graph["subject_has_record"]
                if s_node == sub_node
            }
            sub_visits = {v for s_node, v in graph["has_visit"] if s_node == sub_node}
            via_visit = {
                rec_node[-2] if rec_node[0] == "created" else rec_node[0]
                for v_node, rec_node in graph["visit_has_record"]
                if v_node in sub_visits
            }
            return direct | via_visit

        assert _subject_record_ids("SUBJ-1") == {1, 2, 3}
        assert _subject_record_ids("SUBJ-2") == {4, 5}
        assert _subject_record_ids("SUBJ-1").isdisjoint(_subject_record_ids("SUBJ-2"))

    def test_snowflake_arrow_table_decimal_preserve_and_mixed_string_harmonization(self):
        """_records_to_arrow_table preserves decimal128 on homogeneous Decimal batches and coerces mixed [Decimal, str] / [str, Decimal] to VARCHAR."""
        from datetime import date
        from decimal import Decimal

        import pyarrow as pa

        from imednet.models.records import Keyword, Record
        from imednet_sinks.warehouse import _records_to_arrow_table, _resolve_column_types

        # 1. Homogeneous Decimal + datetime.date + Keyword batch
        homogeneous = [
            Record(
                record_id=1,
                study_key="STUDY_DEC",
                subject_key="S1",
                keywords=[Keyword(keyword_name="Lab", keyword_key="LAB", keyword_id=1)],
                record_data={
                    "labResult": Decimal("0.050"),
                    "sampleDate": date(2026, 5, 1),
                },
            ),
            Record(
                record_id=2,
                study_key="STUDY_DEC",
                subject_key="S2",
                record_data={
                    "labResult": Decimal("12.345"),
                    "sampleDate": date(2026, 5, 2),
                },
            ),
        ]
        homo_tbl = _records_to_arrow_table(homogeneous, study_key="STUDY_DEC")
        assert pa.types.is_decimal(homo_tbl.schema.field("lab_result").type)
        assert pa.types.is_date(homo_tbl.schema.field("sample_date").type)
        col_types = _resolve_column_types(homo_tbl, homo_tbl.column_names)
        assert col_types["lab_result"] == "NUMBER(38, 3)"
        assert col_types["sample_date"] == "DATE"
        assert homo_tbl.column("lab_result").to_pylist() == [Decimal("0.050"), Decimal("12.345")]

        # 2. Mixed [Decimal, str] batch (e.g. numeric lab result + "BLQ")
        dec_then_str = [
            Record(
                record_id=3,
                study_key="STUDY_DEC",
                subject_key="S3",
                record_data={"labResult": Decimal("0.00425")},
            ),
            Record(
                record_id=4,
                study_key="STUDY_DEC",
                subject_key="S4",
                record_data={"labResult": "BLQ"},
            ),
        ]
        tbl_ds = _records_to_arrow_table(dec_then_str, study_key="STUDY_DEC")
        assert pa.types.is_string(
            tbl_ds.schema.field("lab_result").type
        ) or pa.types.is_large_string(tbl_ds.schema.field("lab_result").type)
        assert tbl_ds.column("lab_result").to_pylist() == ["0.00425", "BLQ"]

        # 3. Mixed [str, Decimal] batch (reverse order)
        tbl_sd = _records_to_arrow_table(list(reversed(dec_then_str)), study_key="STUDY_DEC")
        assert pa.types.is_string(
            tbl_sd.schema.field("lab_result").type
        ) or pa.types.is_large_string(tbl_sd.schema.field("lab_result").type)
        assert tbl_sd.column("lab_result").to_pylist() == ["BLQ", "0.00425"]


class TestPluginSinksSdtmAndNamingConventionParity:
    """Verify Snowflake, MongoDB, and Neo4j sinks support as_sdtm and naming_convention parity."""

    def test_all_three_plugin_sinks_emit_sdtm_with_cross_batch_seq_and_phi_masking(
        self, monkeypatch, tmp_path
    ):
        """Snowflake, MongoDB, and Neo4j emit uppercase SDTM schemas with monotonic --SEQ per USUBJID."""
        import pyarrow.parquet as pq

        import imednet_sinks.document as doc_mod
        import imednet_sinks.graph as graph_mod
        import imednet_sinks.warehouse as wh_mod
        from imednet.models.records import Record
        from imednet_sinks import (
            MongoDbExportSink,
            MongoDbSinkConfig,
            Neo4jExportSink,
            Neo4jSinkConfig,
            SnowflakeExportSink,
            SnowflakeSinkConfig,
        )

        batch_1 = [
            Record(
                record_id=1,
                study_key="SDTM_STUDY",
                form_key="AE",
                subject_key="SUBJ-9",
                record_data={
                    "aeTerm": "Headache",
                    "aeStartDate": "2026-02-01 10:00:00",
                    "aeSerious": "yes",
                    "patientName": "Hidden Name",
                },
            )
        ]
        batch_2 = [
            Record(
                record_id=2,
                study_key="SDTM_STUDY",
                form_key="AE",
                subject_key="SUBJ-9",
                record_data={
                    "ae_term": "Nausea",
                    "ae_start_date": "2026-02-02",
                    "ae_serious": False,
                },
            )
        ]

        # 1. SnowflakeExportSink
        sf, _conn, cursor = _fake_snowflake_module()
        cursor.fetchall.return_value = []
        orig_require = wh_mod._require_optional_dep
        monkeypatch.setattr(
            wh_mod,
            "_require_optional_dep",
            lambda pkg, extras: sf if pkg == "snowflake.connector" else orig_require(pkg, extras),
        )
        sf_stage_dir = tmp_path / "sf_sdtm"
        sf_cfg = SnowflakeSinkConfig(
            study_key="SDTM_STUDY",
            account="acct",
            user="usr",
            password="pwd",  # pragma: allowlist secret
            database="DB",
            schema="PUBLIC",
            warehouse="WH",
            stage="STG",
            table="AE",
            local_staging_dir=str(sf_stage_dir),
            as_sdtm=True,
            sdtm_domain="AE",
        )
        with SnowflakeExportSink(config=sf_cfg) as sf_sink:
            sf_sink.write_batch(batch_1, batch_id="b1")
            sf_sink.write_batch(batch_2, batch_id="b2")

        t1 = pq.read_table(sf_stage_dir / "b1.parquet").to_pylist()
        t2 = pq.read_table(sf_stage_dir / "b2.parquet").to_pylist()
        assert t1[0]["STUDYID"] == "SDTM_STUDY"
        assert t1[0]["DOMAIN"] == "AE"
        assert t1[0]["USUBJID"] == "SUBJ-9"
        assert t1[0]["AESEQ"] == 1
        assert t1[0]["AESTDTC"] == "2026-02-01T10:00:00"
        assert t1[0]["AESER"] == "Y"
        assert t1[0]["patientName"] == "***MASKED***"
        assert t1[0]["RECORD_ID"] == 1
        assert "record_data" not in t1[0]
        assert t2[0]["AESEQ"] == 2
        assert t2[0]["RECORD_ID"] == 2
        executed_sql = "\n".join(c.args[0] for c in cursor.execute.call_args_list)
        assert 'PARTITION BY "STUDYID", "RECORD_ID"' in executed_sql

        cursor.execute.reset_mock()
        sf_strict_cfg = SnowflakeSinkConfig(
            study_key="SDTM_STUDY",
            account="acct",
            user="usr",
            password="pwd",  # pragma: allowlist secret
            database="DB",
            schema="PUBLIC",
            warehouse="WH",
            stage="STG",
            table="AE",
            local_staging_dir=str(tmp_path / "sf_sdtm_strict"),
            as_sdtm=True,
            sdtm_domain="AE",
            preserve_record_id=False,
        )
        with SnowflakeExportSink(config=sf_strict_cfg) as sf_strict_sink:
            sf_strict_sink.write_batch(batch_1, batch_id="b1_strict")
        strict_sql = "\n".join(c.args[0] for c in cursor.execute.call_args_list)
        assert 'PARTITION BY "STUDYID", "DOMAIN", "USUBJID", "AESEQ"' in strict_sql

        # 2. MongoDbExportSink
        pymongo_mod, _client, collection = _fake_pymongo_module()
        pymongo_mod.UpdateOne = MagicMock(side_effect=lambda flt, upd, upsert: (flt, upd, upsert))
        monkeypatch.setattr(doc_mod, "_require_optional_dep", lambda *_: pymongo_mod)
        mongo_cfg = MongoDbSinkConfig(
            study_key="SDTM_STUDY",
            uri="mongodb://localhost:27017",
            database="clin",
            collection="ae",
            as_sdtm=True,
            sdtm_domain="AE",
        )
        with MongoDbExportSink(config=mongo_cfg) as mongo_sink:
            mongo_sink.write_batch(batch_1, batch_id="b1")
            mongo_sink.write_batch(batch_2, batch_id="b2")
        ops_1 = collection.bulk_write.call_args_list[0].args[0]
        ops_2 = collection.bulk_write.call_args_list[1].args[0]
        doc_1 = ops_1[0][1]["$set"]
        doc_2 = ops_2[0][1]["$set"]
        assert doc_1["_id"] == "SDTM_STUDY/1"
        assert doc_1["AESEQ"] == 1
        assert doc_1["patientName"] == "***MASKED***"
        assert doc_2["_id"] == "SDTM_STUDY/2"
        assert doc_2["AESEQ"] == 2

        # 3. Neo4jExportSink
        neo4j_mod, driver = _fake_neo4j_module()
        monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j_mod)
        neo_cfg = Neo4jSinkConfig(
            study_key="SDTM_STUDY",
            uri="bolt://localhost:7687",
            auth=("neo4j", "pass"),
            as_sdtm=True,
            sdtm_domain="AE",
        )
        with Neo4jExportSink(config=neo_cfg) as neo_sink:
            neo_sink.write_batch(batch_1, batch_id="b1")
            neo_sink.write_batch(batch_2, batch_id="b2")
        neo_calls = driver.session.return_value.__enter__.return_value.run.call_args_list
        assert "MERGE (s:Study   {STUDYID: row.STUDYID})" in neo_calls[0].args[0]
        assert neo_calls[0].kwargs["rows"][0]["AESEQ"] == 1
        assert neo_calls[1].kwargs["rows"][0]["AESEQ"] == 2

    @pytest.mark.parametrize("convention", ["snake_case", "camelCase", "sdtm"])
    def test_naming_convention_parity_across_parquet_snowflake_mongodb_and_neo4j(
        self, monkeypatch, tmp_path, convention
    ):
        """naming_convention produces aligned clinical variable and identifier keys across Parquet and plugin sinks."""
        import pandas as pd
        import pyarrow.parquet as pq

        import imednet_sinks.document as doc_mod
        import imednet_sinks.graph as graph_mod
        import imednet_sinks.warehouse as wh_mod
        from imednet.integrations import export as export_mod
        from imednet.models.forms import Form
        from imednet.models.records import Record
        from imednet.models.variables import Variable
        from imednet_sinks import (
            MongoDbExportSink,
            MongoDbSinkConfig,
            Neo4jExportSink,
            Neo4jSinkConfig,
            SnowflakeExportSink,
            SnowflakeSinkConfig,
        )

        rec = Record(
            record_id=55,
            study_key="PARITY_STUDY",
            form_id=1,
            form_key="VS",
            subject_key="SUBJ-55",
            visit_id=3,
            record_data={"heartRate": 72, "sys_bp": 120},
        )

        sdk = MagicMock()
        sdk.forms.list.return_value = [Form(form_id=1, form_key="VS", form_name="Vitals")]
        sdk.variables.list.return_value = [
            Variable(variable_id=1, form_id=1, form_key="VS", variable_name="heartRate"),
            Variable(variable_id=2, form_id=1, form_key="VS", variable_name="sys_bp"),
        ]
        sdk.records.list.side_effect = lambda **_kw: iter([rec])

        pq_file = tmp_path / f"parity_{convention}.parquet"
        export_mod.export_to_parquet(
            sdk, "PARITY_STUDY", str(pq_file), naming_convention=convention
        )
        pq_cols = set(pd.read_parquet(pq_file).columns)

        sf, _conn, cursor = _fake_snowflake_module()
        cursor.fetchall.return_value = []
        orig_require = wh_mod._require_optional_dep
        monkeypatch.setattr(
            wh_mod,
            "_require_optional_dep",
            lambda pkg, extras: sf if pkg == "snowflake.connector" else orig_require(pkg, extras),
        )
        sf_dir = tmp_path / f"sf_{convention}"
        sf_cfg = SnowflakeSinkConfig(
            study_key="PARITY_STUDY",
            account="acct",
            user="usr",
            password="pwd",  # pragma: allowlist secret
            database="DB",
            schema="PUBLIC",
            warehouse="WH",
            stage="STG",
            table="RECORDS",
            local_staging_dir=str(sf_dir),
            naming_convention=convention,
        )
        with SnowflakeExportSink(config=sf_cfg) as sf_sink:
            sf_sink.write_batch([rec], batch_id="b0")
        sf_cols = set(pq.read_table(sf_dir / "b0.parquet").column_names)

        expected_vars = {
            "snake_case": {"study_key", "subject_key", "record_id", "heart_rate", "sys_bp"},
            "camelCase": {"studyKey", "subjectKey", "recordId", "heartRate", "sysBp"},
            "sdtm": {"STUDYID", "USUBJID", "RECORD_ID", "HEARTRATE", "SYSBP"},
        }[convention]
        assert expected_vars.issubset(pq_cols)
        assert expected_vars.issubset(sf_cols)

        pymongo_mod, _client, collection = _fake_pymongo_module()
        pymongo_mod.UpdateOne = MagicMock(side_effect=lambda flt, upd, upsert: (flt, upd, upsert))
        monkeypatch.setattr(doc_mod, "_require_optional_dep", lambda *_: pymongo_mod)
        with MongoDbExportSink(
            MongoDbSinkConfig(
                study_key="PARITY_STUDY",
                uri="mongodb://localhost:27017",
                database="db",
                collection="col",
                naming_convention=convention,
            )
        ) as mongo_sink:
            mongo_sink.write_batch([rec], batch_id="b0")
        mongo_doc = collection.bulk_write.call_args.args[0][0][1]["$set"]
        rd_key = {"snake_case": "record_data", "camelCase": "recordData", "sdtm": "RECORD_DATA"}[
            convention
        ]
        expected_var_only = {
            "snake_case": {"heart_rate", "sys_bp"},
            "camelCase": {"heartRate", "sysBp"},
            "sdtm": {"HEARTRATE", "SYSBP"},
        }[convention]
        assert set(mongo_doc[rd_key].keys()) == expected_var_only

        neo4j_mod, driver = _fake_neo4j_module()
        monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j_mod)
        with Neo4jExportSink(
            Neo4jSinkConfig(
                study_key="PARITY_STUDY",
                uri="bolt://localhost:7687",
                auth=("neo4j", "pass"),
                naming_convention=convention,
            )
        ) as neo_sink:
            neo_sink.write_batch([rec], batch_id="b0")
        neo_row = driver.session.return_value.__enter__.return_value.run.call_args.kwargs["rows"][0]
        assert (expected_vars - expected_var_only).issubset(set(neo_row.keys()))

    @pytest.mark.parametrize(
        ("convention", "expected_neo_fragment", "expected_sf_where"),
        [
            (
                "snake_case",
                "MATCH (r:Record {record_id: rid, study_key: $study_key})",
                "WHERE STUDY_KEY = %s AND RECORD_ID IN (%s)",
            ),
            (
                "camelCase",
                "MATCH (r:Record {recordId: rid, studyKey: $study_key})",
                'WHERE "STUDYKEY" = %s AND "RECORDID" IN (%s)',
            ),
            (
                "sdtm",
                "MATCH (r:Record {RECORD_ID: rid, STUDYID: $study_key})",
                'WHERE "STUDYID" = %s AND "RECORD_ID" IN (%s)',
            ),
        ],
    )
    def test_reconcile_deleted_respects_naming_convention_in_neo4j_and_snowflake(
        self,
        monkeypatch,
        tmp_path,
        convention: str,
        expected_neo_fragment: str,
        expected_sf_where: str,
    ):
        """Neo4j and Snowflake reconcile_deleted match identifiers formatted by naming_convention."""
        import imednet_sinks.graph as graph_mod
        import imednet_sinks.warehouse as wh_mod
        from imednet.models.records import Record
        from imednet_sinks import (
            Neo4jExportSink,
            Neo4jSinkConfig,
            SnowflakeExportSink,
            SnowflakeSinkConfig,
        )

        voided = Record(record_id=88, study_key="DEL_STUDY", deleted=True, record_data={})

        neo4j_mod, driver = _fake_neo4j_module()
        monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j_mod)
        with Neo4jExportSink(
            Neo4jSinkConfig(
                study_key="DEL_STUDY",
                uri="bolt://localhost:7687",
                auth=("neo4j", "pass"),
                naming_convention=convention,
            )
        ) as neo_sink:
            assert neo_sink.reconcile_deleted([voided], batch_id="del/0") == 1
        neo_cypher = driver.session.return_value.__enter__.return_value.run.call_args.args[0]
        assert expected_neo_fragment in neo_cypher

        sf, _conn, cursor = _fake_snowflake_module()
        monkeypatch.setattr(wh_mod, "_require_optional_dep", lambda *_: sf)
        with SnowflakeExportSink(
            SnowflakeSinkConfig(
                study_key="DEL_STUDY",
                account="acct",
                user="usr",
                password="pwd",  # pragma: allowlist secret
                database="DB",
                schema="PUBLIC",
                warehouse="WH",
                stage="STG",
                table="RECORDS",
                local_staging_dir=str(tmp_path / f"sf_del_{convention}"),
                naming_convention=convention,
            )
        ) as sf_sink:
            assert sf_sink.reconcile_deleted([voided], batch_id="del/0") == 1
        sf_sql = cursor.execute.call_args.args[0]
        assert expected_sf_where in sf_sql

    def test_neo4j_sdtm_validates_cypher_keys_and_binds_seq_prop_to_domain(self, monkeypatch):
        """Neo4j SDTM exports reject unsafe row/builder keys, omit *SEQ from DM MERGE, and sanitize form_key domains."""
        import imednet_sinks.graph as graph_mod
        from imednet.errors import ExportConfigurationError
        from imednet.models.records import Record
        from imednet_sinks import Neo4jExportSink, Neo4jSinkConfig
        from imednet_sinks.graph import (
            _build_convention_cypher,
            _build_sdtm_cypher,
            _post_process_graph_sdtm,
        )

        neo4j_mod, driver = _fake_neo4j_module()
        monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j_mod)
        run_mock = driver.session.return_value.__enter__.return_value.run

        # 1. DM record with malicious *SEQ key raises ExportConfigurationError before session.run
        dm_rec = Record(
            record_id=101,
            study_key="STUDY_CLI_DM",
            form_key="DM",
            subject_key="SUBJ-DM-1",
            record_data={"SEX": "F", "canary: 1}) //SEQ": 1},
        )
        dm_cfg = Neo4jSinkConfig(
            study_key="STUDY_CLI_DM",
            uri="bolt://localhost:7687",
            auth=("neo4j", "pass"),
            as_sdtm=True,
            sdtm_domain="DM",
        )
        with Neo4jExportSink(config=dm_cfg) as dm_sink:
            with pytest.raises(ExportConfigurationError, match="invalid characters"):
                dm_sink.write_batch([dm_rec], batch_id="dm/bad")
        run_mock.assert_not_called()

        # 2. DM record with benign alphanumeric *SEQ keys keeps seq_prop=None in MERGE
        dm_benign = Record(
            record_id=102,
            study_key="STUDY_CLI_DM",
            form_key="DM",
            subject_key="SUBJ-DM-2",
            record_data={"SEX": "M", "CUSTOMSEQ": 1, "PRIORSEQ": 7},
        )
        with Neo4jExportSink(config=dm_cfg) as dm_sink:
            assert dm_sink.write_batch([dm_benign], batch_id="dm/ok") == 1
        dm_default_cypher = run_mock.call_args.args[0]
        assert (
            "MERGE (r:Record   {STUDYID: row.STUDYID, RECORD_ID: row.RECORD_ID})"
            in dm_default_cypher
        )

        run_mock.reset_mock()
        dm_strict_cfg = Neo4jSinkConfig(
            study_key="STUDY_CLI_DM",
            uri="bolt://localhost:7687",
            auth=("neo4j", "pass"),
            as_sdtm=True,
            sdtm_domain="DM",
            preserve_record_id=False,
        )
        with Neo4jExportSink(config=dm_strict_cfg) as dm_strict_sink:
            assert dm_strict_sink.write_batch([dm_benign], batch_id="dm/strict") == 1
        dm_cypher = run_mock.call_args.args[0]
        dm_rows = run_mock.call_args.kwargs["rows"]
        assert (
            "MERGE (r:Record   {STUDYID: row.STUDYID, DOMAIN: row.DOMAIN, USUBJID: row.USUBJID})"
            in dm_cypher
        )
        assert "CUSTOMSEQ" not in dm_cypher
        assert "PRIORSEQ" not in dm_cypher
        assert dm_rows[0]["CUSTOMSEQ"] == 1
        assert dm_rows[0]["PRIORSEQ"] == 7

        # 3. Unaliased punctured form_key is sanitized in resolve_sdtm_domain and never injects MERGE clauses
        run_mock.reset_mock()
        dom_rec = Record(
            record_id=103,
            study_key="STUDY_AF_DOM",
            form_key="ZZ: 1}) //",
            subject_key="SUBJ-DOM-1",
            record_data={"note": "unaliased_form_domain"},
        )
        dom_cfg = Neo4jSinkConfig(
            study_key="STUDY_AF_DOM",
            uri="bolt://localhost:7687",
            auth=("neo4j", "pass"),
            as_sdtm=True,
            preserve_record_id=False,
        )
        with Neo4jExportSink(config=dom_cfg) as dom_sink:
            assert dom_sink.write_batch([dom_rec], batch_id="dom/0") == 1
        dom_cypher = run_mock.call_args.args[0]
        dom_rows = run_mock.call_args.kwargs["rows"]
        assert dom_rows[0]["DOMAIN"] == "ZZ1"
        assert dom_rows[0]["ZZ1SEQ"] == 1
        assert (
            "MERGE (r:Record   {STUDYID: row.STUDYID, DOMAIN: row.DOMAIN, USUBJID: row.USUBJID, ZZ1SEQ: row.ZZ1SEQ})"
            in dom_cypher
        )
        assert "//" not in dom_cypher
        assert dom_cypher.count("MERGE (r:Record") == 1

        # 4. Custom SDTM domains (FA, SUPPAE) bind seq_prop to f"{domain}SEQ" even with extra *SEQ keys
        for custom_dom, expected_seq in (("FA", "FASEQ"), ("SUPPAE", "SUPPAESEQ")):
            run_mock.reset_mock()
            custom_rec = Record(
                record_id=104,
                study_key="STUDY_CUSTOM_DOM",
                form_key=custom_dom,
                subject_key="SUBJ-CUST-1",
                record_data={"PRIORSEQ": 99, "FAOBJ": "Pain"},
            )
            custom_cfg = Neo4jSinkConfig(
                study_key="STUDY_CUSTOM_DOM",
                uri="bolt://localhost:7687",
                auth=("neo4j", "pass"),
                as_sdtm=True,
                sdtm_domain=custom_dom,
                preserve_record_id=False,
            )
            with Neo4jExportSink(config=custom_cfg) as custom_sink:
                assert custom_sink.write_batch([custom_rec], batch_id=f"{custom_dom}/0") == 1
            custom_cypher = run_mock.call_args.args[0]
            assert f"{expected_seq}: row.{expected_seq}" in custom_cypher
            assert "PRIORSEQ" not in custom_cypher

        # 5. Direct validation in _post_process_graph_sdtm, _build_sdtm_cypher, and _build_convention_cypher
        with pytest.raises(ExportConfigurationError, match="invalid characters"):
            _post_process_graph_sdtm({"canary: 1}) //SEQ": None})

        merge_none, _ = _build_sdtm_cypher(None, False)
        assert (
            "MERGE (r:Record   {STUDYID: row.STUDYID, DOMAIN: row.DOMAIN, USUBJID: row.USUBJID})"
            in merge_none
        )
        merge_ae, _ = _build_sdtm_cypher("AESEQ", True)
        assert "AESEQ: row.AESEQ" in merge_ae

        for bad_seq in ("", "SEQ", "aeseq", "1AESEQ", "canary: 1}) //SEQ", "AESEQ\n"):
            with pytest.raises(ExportConfigurationError, match="invalid characters"):
                _build_sdtm_cypher(bad_seq, False)

        for bad_args in (
            ("study: 1}", "subject_key", "record_id", "visit_id"),
            ("study_key", "1subject", "record_id", "visit_id"),
            ("study_key", "subject_key", "record-id", "visit_id"),
            ("study_key", "subject_key", "record_id", "visit_id\n"),
        ):
            with pytest.raises(ExportConfigurationError, match="invalid characters"):
                _build_convention_cypher(*bad_args)
