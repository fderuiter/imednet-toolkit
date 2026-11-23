"""Unit and integration tests for stream-staged SDTM sequencing and unified readiness gates."""

from __future__ import annotations

import csv
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, patch

import pandas as pd
import pyarrow.parquet as pq
import pytest

from imednet import cli as cli_mod
from imednet.errors import ExportBatchError, ExportError
from imednet.integrations.export import (
    TabularDuckDBSink,
    TabularSinkConfig,
    _TabularTransformSink,
    export_to_csv,
    export_to_duckdb,
    export_to_duckdb_by_form,
    export_to_parquet,
)
from imednet.integrations.sink_base import (
    ExportSink,
    SinkConfig,
    _enforce_batch_standards_readiness,
    apply_quality_gate,
)
from imednet.models.forms import Form
from imednet.models.records import Record
from imednet.models.sdtm import (
    _SDTM_RECORDS_SORT,
    SDTM_SEQUENCER_METADATA_KEY,
    TransactionalSdtmSequencer,
    iter_staged_sdtm_records,
)
from imednet.models.standards import (
    PROFILE_REGISTRY,
    BiologicSafetyProfile,
    CategoricalNormalizer,
    GeneralClinicalProfile,
    StandardsReadinessValidator,
    VaccineSafetyProfile,
    resolve_standards_profile,
)
from imednet.models.variables import Variable
from imednet.spi.sinks import build_sink_mapper
from imednet_sinks.document import MongoDbExportSink, MongoDbSinkConfig, export_to_mongodb
from imednet_sinks.graph import Neo4jExportSink, Neo4jSinkConfig, export_to_neo4j
from imednet_sinks.warehouse import SnowflakeExportSink, SnowflakeSinkConfig, export_to_snowflake
from imednet_workflows.record_mapper import RecordMapper
from imednet_workflows.state_ledger import AirflowStateProvider, FileStateProvider


def _make_mock_sdk(
    records: list[Record],
    *,
    forms: list[Form] | None = None,
    variables: list[Variable] | None = None,
) -> MagicMock:
    sdk = MagicMock()
    sdk.forms.list.return_value = forms or [
        Form(form_id=1, form_key="AE", form_name="Adverse Events"),
        Form(form_id=2, form_key="VS", form_name="Vital Signs"),
        Form(form_id=3, form_key="DM", form_name="Demographics"),
    ]
    sdk.variables.list.return_value = variables or [
        Variable(variable_id=1, form_id=1, form_key="AE", variable_name="AETERM"),
        Variable(variable_id=2, form_id=1, form_key="AE", variable_name="AESTDTC"),
        Variable(variable_id=3, form_id=2, form_key="VS", variable_name="VSTESTCD"),
        Variable(variable_id=4, form_id=2, form_key="VS", variable_name="VSORRES"),
        Variable(variable_id=5, form_id=2, form_key="VS", variable_name="VSDTC"),
        Variable(variable_id=6, form_id=3, form_key="DM", variable_name="SEX"),
        Variable(variable_id=7, form_id=3, form_key="DM", variable_name="BRTHDTC"),
        Variable(variable_id=8, form_id=3, form_key="DM", variable_name="RACE"),
        Variable(variable_id=9, form_id=3, form_key="DM", variable_name="ETHNIC"),
    ]
    sdk.intervals.list.return_value = []
    sdk.subjects.list.return_value = []
    sdk.sites.list.return_value = []

    def _list_records(**kwargs: Any) -> list[Record]:
        items = list(records)
        fid = kwargs.get("formId", kwargs.get("form_id"))
        if fid is not None:
            items = [r for r in items if r.form_id == fid]
        fkey = kwargs.get("formKey", kwargs.get("form_key"))
        if fkey is not None:
            items = [r for r in items if r.form_key == fkey]
        sort_spec = kwargs.get("sort")
        if sort_spec == _SDTM_RECORDS_SORT:
            items.sort(key=lambda r: (r.subject_key or "", r.record_id or 0))
        return items

    sdk.records.list.side_effect = _list_records
    sdk.records.iter.side_effect = _list_records
    return sdk


class TestStreamStagedSdtmSequencerParity:
    """Verify (STUDYID, DOMAIN, USUBJID) scoping, --DTC ordering, and cross-sink parity."""

    def test_out_of_order_ae_and_vs_batch_size_one_matches_record_mapper_across_all_sinks(
        self,
        tmp_path: Path,
    ) -> None:
        study = "STUDY_SEQ"
        records = [
            # Intentionally out-of-order dates and interleaved domains
            Record(
                record_id=102,
                study_key=study,
                form_id=1,
                form_key="AE",
                subject_key="SUBJ-01",
                record_status="Record Complete",
                date_modified=datetime(2025, 3, 2, 12, 0, tzinfo=UTC),
                record_data={"AETERM": "Nausea", "AESTDTC": "2025-03-02"},
            ),
            Record(
                record_id=202,
                study_key=study,
                form_id=2,
                form_key="VS",
                subject_key="SUBJ-01",
                record_status="Record Complete",
                date_modified=datetime(2025, 3, 4, 12, 0, tzinfo=UTC),
                record_data={"VSTESTCD": "TEMP", "VSORRES": "98.6", "VSDTC": "2025-03-04"},
            ),
            Record(
                record_id=101,
                study_key=study,
                form_id=1,
                form_key="AE",
                subject_key="SUBJ-01",
                record_status="Record Complete",
                date_modified=datetime(2025, 3, 1, 12, 0, tzinfo=UTC),
                record_data={"AETERM": "Headache", "AESTDTC": "2025-03-01"},
            ),
            Record(
                record_id=201,
                study_key=study,
                form_id=2,
                form_key="VS",
                subject_key="SUBJ-01",
                record_status="Record Complete",
                date_modified=datetime(2025, 3, 1, 9, 0, tzinfo=UTC),
                record_data={"VSTESTCD": "SYSBP", "VSORRES": "120", "VSDTC": "2025-03-01"},
            ),
        ]
        sdk = _make_mock_sdk(records)

        # Reference output from RecordMapper.iter_sdtm_dataframes
        mapper = RecordMapper(sdk)
        ae_ref_df = pd.concat(
            list(mapper.iter_sdtm_dataframes(study, "AE", form_whitelist=[1], mask_phi=False))
        )
        vs_ref_df = pd.concat(
            list(mapper.iter_sdtm_dataframes(study, "VS", form_whitelist=[2], mask_phi=False))
        )
        expected_ae = [
            (row["AETERM"], row["AESTDTC"], int(row["AESEQ"])) for _, row in ae_ref_df.iterrows()
        ]
        expected_vs = [
            (row["VSTESTCD"], row["VSDTC"], int(row["VSSEQ"])) for _, row in vs_ref_df.iterrows()
        ]
        assert expected_ae == [
            ("Headache", "2025-03-01", 1),
            ("Nausea", "2025-03-02", 2),
        ]
        assert expected_vs == [
            ("SYSBP", "2025-03-01", 1),
            ("TEMP", "2025-03-04", 2),
        ]

        # 1. CSV with batch_size=1 (mixed AE + VS stream)
        csv_file = tmp_path / "sdtm_all.csv"
        export_to_csv(
            sdk,
            study,
            str(csv_file),
            mask_phi=False,
            as_sdtm=True,
            batch_size=1,
        )
        assert any(
            call.kwargs.get("sort") == _SDTM_RECORDS_SORT
            for call in (sdk.records.list.call_args_list + sdk.records.iter.call_args_list)
        )
        with csv_file.open(encoding="utf-8") as fh:
            csv_rows = list(csv.DictReader(fh))
        csv_ae = [
            (r["AETERM"], r["AESTDTC"], int(float(r["AESEQ"])))
            for r in csv_rows
            if r["DOMAIN"] == "AE"
        ]
        csv_vs = [
            (r["VSTESTCD"], r["VSDTC"], int(float(r["VSSEQ"])))
            for r in csv_rows
            if r["DOMAIN"] == "VS"
        ]
        assert csv_ae == expected_ae
        assert csv_vs == expected_vs

        # 2. Parquet with batch_size=1 (mixed AE + VS stream)
        pq_file = tmp_path / "sdtm_all.parquet"
        export_to_parquet(
            sdk,
            study,
            str(pq_file),
            mask_phi=False,
            as_sdtm=True,
            batch_size=1,
        )
        pq_rows = pq.read_table(pq_file).to_pylist()
        pq_ae = [
            (r["AETERM"], r["AESTDTC"], int(r["AESEQ"])) for r in pq_rows if r.get("DOMAIN") == "AE"
        ]
        pq_vs = [
            (r["VSTESTCD"], r["VSDTC"], int(r["VSSEQ"])) for r in pq_rows if r.get("DOMAIN") == "VS"
        ]
        assert pq_ae == expected_ae
        assert pq_vs == expected_vs

        # 3. DuckDB with batch_size=1
        import duckdb

        duck_path = tmp_path / "sdtm.duckdb"
        export_to_duckdb_by_form(
            sdk,
            study,
            str(duck_path),
            mask_phi=False,
            as_sdtm=True,
            batch_size=1,
            quality_gate_enabled=False,
        )
        with duckdb.connect(str(duck_path)) as conn:
            duck_ae = conn.execute(
                "SELECT AETERM, AESTDTC, CAST(AESEQ AS INTEGER) FROM AE ORDER BY AESEQ"
            ).fetchall()
            duck_vs = conn.execute(
                "SELECT VSTESTCD, VSDTC, CAST(VSSEQ AS INTEGER) FROM VS ORDER BY VSSEQ"
            ).fetchall()
        assert duck_ae == expected_ae
        assert duck_vs == expected_vs

        # 4. Snowflake with batch_size=1
        sf_stage_dir = tmp_path / "sf_stage"
        sf_conn = MagicMock()
        sf_cur = MagicMock()
        sf_cur.fetchall.return_value = []
        sf_conn.cursor.return_value = sf_cur
        sf_mod = MagicMock()
        sf_mod.connect.return_value = sf_conn
        with patch.dict("sys.modules", {"snowflake": MagicMock(), "snowflake.connector": sf_mod}):
            sf_cfg = SnowflakeSinkConfig(
                study_key=study,
                account="acct",
                user="usr",
                password="pwd",  # pragma: allowlist secret
                database="DB",
                schema="PUBLIC",
                warehouse="WH",
                stage="STG",
                table="SDTM_OUT",
                local_staging_dir=str(sf_stage_dir),
                mask_phi=False,
                as_sdtm=True,
                batch_size=1,
            )
            export_to_snowflake(sdk, study, config=sf_cfg)
        staged_rows: list[dict[str, Any]] = []
        for pq_file in sorted(sf_stage_dir.glob("*.parquet")):
            staged_rows.extend(pq.read_table(pq_file).to_pylist())
        sf_ae = sorted(
            [
                (r["AETERM"], r["AESTDTC"], int(r["AESEQ"]))
                for r in staged_rows
                if r.get("DOMAIN") == "AE"
            ],
            key=lambda item: item[2],
        )
        sf_vs = sorted(
            [
                (r["VSTESTCD"], r["VSDTC"], int(r["VSSEQ"]))
                for r in staged_rows
                if r.get("DOMAIN") == "VS"
            ],
            key=lambda item: item[2],
        )
        assert sf_ae == expected_ae
        assert sf_vs == expected_vs

        # 5. Neo4j with batch_size=1
        neo_rows: list[dict[str, Any]] = []
        neo_session = MagicMock()
        neo_session.__enter__.return_value = neo_session
        neo_session.run.side_effect = lambda _q, rows=None, **_kw: neo_rows.extend(rows or [])
        neo_driver = MagicMock()
        neo_driver.session.return_value = neo_session
        neo_mod = MagicMock()
        neo_mod.GraphDatabase.driver.return_value = neo_driver
        with patch.dict("sys.modules", {"neo4j": neo_mod}):
            neo_cfg = Neo4jSinkConfig(
                study_key=study,
                uri="bolt://localhost:7687",
                auth=("neo4j", "pwd"),
                mask_phi=False,
                as_sdtm=True,
                batch_size=1,
            )
            export_to_neo4j(sdk, study, config=neo_cfg)
        assert [
            (r["AETERM"], r["AESTDTC"], int(r["AESEQ"]))
            for r in neo_rows
            if r.get("DOMAIN") == "AE"
        ] == expected_ae
        assert [
            (r["VSTESTCD"], r["VSDTC"], int(r["VSSEQ"]))
            for r in neo_rows
            if r.get("DOMAIN") == "VS"
        ] == expected_vs

        # 6. MongoDB with batch_size=1
        mongo_docs: list[dict[str, Any]] = []
        mongo_col = MagicMock()
        mongo_client = MagicMock()
        mongo_client.__getitem__.return_value.__getitem__.return_value = mongo_col
        pymongo_mod = MagicMock()
        pymongo_mod.MongoClient.return_value = mongo_client
        pymongo_mod.UpdateOne.side_effect = lambda flt, upd, upsert=False: {
            "filter": flt,
            "doc": upd["$set"],
            "upsert": upsert,
        }
        mongo_col.bulk_write.side_effect = lambda ops, ordered=False: mongo_docs.extend(
            op["doc"] for op in ops
        )
        with patch.dict("sys.modules", {"pymongo": pymongo_mod}):
            mongo_cfg = MongoDbSinkConfig(
                study_key=study,
                uri="mongodb://localhost:27017",
                database="db",
                collection="sdtm",
                mask_phi=False,
                as_sdtm=True,
                batch_size=1,
            )
            export_to_mongodb(sdk, study, config=mongo_cfg)
        assert [
            (d["AETERM"], d["AESTDTC"], int(d["AESEQ"]))
            for d in mongo_docs
            if d.get("DOMAIN") == "AE"
        ] == expected_ae
        assert [
            (d["VSTESTCD"], d["VSDTC"], int(d["VSSEQ"]))
            for d in mongo_docs
            if d.get("DOMAIN") == "VS"
        ] == expected_vs

    def test_iter_staged_sdtm_records_respects_max_buffered_records_ceiling(self) -> None:
        study = "STUDY_BUF"
        ungrouped = [
            Record(
                record_id=idx,
                study_key=study,
                form_key="AE",
                subject_key=f"SUBJ-{idx % 4:02d}",
                record_status="Record Complete",
                record_data={"AETERM": f"Event {idx}", "AESTDTC": f"2025-01-{10 - idx:02d}"},
            )
            for idx in range(1, 9)
        ]
        staged = list(
            iter_staged_sdtm_records(
                ungrouped,
                study_key=study,
                include_deleted=False,
                mask_phi=False,
                max_buffered_records=3,
            )
        )
        assert len(staged) == 8
        assert {r.record_id for r in staged} == set(range(1, 9))


class TestTransactionalSequencerRollbackOnRetry:
    """Verify transient sink write failures roll back uncommitted sequence increments."""

    def test_snowflake_neo4j_mongodb_and_tabular_sink_rollback_on_transient_error(
        self,
        tmp_path: Path,
    ) -> None:
        study = "STUDY_RETRY"
        rec = Record(
            record_id=10,
            study_key=study,
            form_id=1,
            form_key="AE",
            subject_key="SUBJ-01",
            record_status="Record Complete",
            record_data={"AETERM": "Headache", "AESTDTC": "2025-02-01"},
        )

        # 1. SnowflakeExportSink retry writes AESEQ=1
        sf_stage_dir = tmp_path / "sf_retry"
        sf_conn = MagicMock()
        attempts_sf = 0

        def _make_cursor() -> MagicMock:
            nonlocal attempts_sf
            attempts_sf += 1
            cur = MagicMock()
            cur.fetchall.return_value = []
            if attempts_sf == 1:
                cur.execute.side_effect = RuntimeError("transient network drop")
            return cur

        sf_conn.cursor.side_effect = _make_cursor
        sf_mod = MagicMock()
        sf_mod.connect.return_value = sf_conn
        with patch.dict("sys.modules", {"snowflake": MagicMock(), "snowflake.connector": sf_mod}):
            sf_sink = SnowflakeExportSink(
                SnowflakeSinkConfig(
                    study_key=study,
                    account="acct",
                    user="usr",
                    password="pwd",  # pragma: allowlist secret
                    database="DB",
                    schema="PUBLIC",
                    warehouse="WH",
                    stage="STG",
                    table="AE",
                    local_staging_dir=str(sf_stage_dir),
                    mask_phi=False,
                    as_sdtm=True,
                    sdtm_domain="AE",
                    max_retries=1,
                    retry_backoff=0.0,
                )
            )
            assert sf_sink.write_batch([rec], batch_id="b0") == 1
        assert attempts_sf == 2
        sf_rows = pq.read_table(sf_stage_dir / "b0.parquet").to_pylist()
        assert int(sf_rows[0]["AESEQ"]) == 1

        # 2. Neo4jExportSink retry writes AESEQ=1
        attempts_neo = 0
        captured_neo: list[dict[str, Any]] = []
        neo_session = MagicMock()
        neo_session.__enter__.return_value = neo_session

        def _neo_run(_stmt: str, rows: list[dict[str, Any]]) -> None:
            nonlocal attempts_neo
            attempts_neo += 1
            if attempts_neo == 1:
                raise RuntimeError("transient neo4j leader switch")
            captured_neo.extend(rows)

        neo_session.run.side_effect = _neo_run
        neo_driver = MagicMock()
        neo_driver.session.return_value = neo_session
        neo_mod = MagicMock()
        neo_mod.GraphDatabase.driver.return_value = neo_driver
        with patch.dict("sys.modules", {"neo4j": neo_mod}):
            neo_sink = Neo4jExportSink(
                Neo4jSinkConfig(
                    study_key=study,
                    uri="bolt://localhost:7687",
                    auth=("neo4j", "pwd"),
                    mask_phi=False,
                    as_sdtm=True,
                    sdtm_domain="AE",
                    max_retries=1,
                    retry_backoff=0.0,
                )
            )
            assert neo_sink.write_batch([rec], batch_id="b0") == 1
        assert attempts_neo == 2
        assert captured_neo[0]["AESEQ"] == 1

        # 3. MongoDbExportSink retry writes AESEQ=1
        attempts_mongo = 0
        captured_mongo: list[dict[str, Any]] = []
        mongo_col = MagicMock()

        def _mongo_bulk_write(ops: list[dict[str, Any]], ordered: bool = False) -> None:
            nonlocal attempts_mongo
            _ = ordered
            attempts_mongo += 1
            if attempts_mongo == 1:
                raise RuntimeError("transient primary stepdown")
            captured_mongo.extend(op["doc"] for op in ops)

        mongo_col.bulk_write.side_effect = _mongo_bulk_write
        mongo_client = MagicMock()
        mongo_client.__getitem__.return_value.__getitem__.return_value = mongo_col
        pymongo_mod = MagicMock()
        pymongo_mod.MongoClient.return_value = mongo_client
        pymongo_mod.UpdateOne.side_effect = lambda flt, upd, upsert=False: {
            "filter": flt,
            "doc": upd["$set"],
            "upsert": upsert,
        }
        with patch.dict("sys.modules", {"pymongo": pymongo_mod}):
            mongo_sink = MongoDbExportSink(
                MongoDbSinkConfig(
                    study_key=study,
                    uri="mongodb://localhost:27017",
                    database="db",
                    collection="ae",
                    mask_phi=False,
                    as_sdtm=True,
                    sdtm_domain="AE",
                    max_retries=1,
                    retry_backoff=0.0,
                )
            )
            assert mongo_sink.write_batch([rec], batch_id="b0") == 1
        assert attempts_mongo == 2
        assert captured_mongo[0]["AESEQ"] == 1

        # 4. _TabularTransformSink rolls back uncommitted sequence increment on inner failure
        class _FlakyInnerSink(ExportSink):
            def __init__(self, config: SinkConfig) -> None:
                super().__init__(config)
                self.calls = 0
                self.frames: list[pd.DataFrame] = []

            def write_batch(self, records: Any, *, batch_id: str) -> int:
                _ = batch_id
                self.calls += 1
                if self.calls == 1:
                    raise ExportBatchError("transient inner failure", batch_id="AE/0")
                assert isinstance(records, pd.DataFrame)
                self.frames.append(records.copy())
                return len(records)

            def flush(self) -> None:
                return None

            def close(self) -> None:
                return None

        tab_cfg = TabularSinkConfig(
            study_key=study,
            as_sdtm=True,
            sdtm_domain="AE",
            mask_phi=False,
        )
        inner = _FlakyInnerSink(tab_cfg)
        tab_sink = _TabularTransformSink(
            inner,
            RecordMapper(_make_mock_sdk([rec])),
            None,
            [],
            {},
            tab_cfg,
            sanitize=False,
        )
        with pytest.raises(ExportBatchError):
            tab_sink.write_batch([rec], batch_id="b0")
        # Sequencer was rolled back, so retry starts cleanly at AESEQ=1
        assert tab_sink.write_batch([rec], batch_id="b0") == 1
        assert int(inner.frames[0].iloc[0]["AESEQ"]) == 1


class TestIncrementalSequencerLedgerAndUpserts:
    """Verify incremental sequence watermarks, zero-PHI digests, and multi-sink upserts."""

    def test_incremental_runs_preserve_seq_1_insert_seq_2_and_store_zero_phi_in_ledgers(
        self,
        tmp_path: Path,
    ) -> None:
        study = "STUDY_INC"
        rec_10_v1 = Record(
            record_id=10,
            study_key=study,
            form_id=1,
            form_key="AE",
            subject_key="SUBJ-01",
            record_status="Record Complete",
            date_modified=datetime(2025, 1, 10, 8, 0, tzinfo=UTC),
            record_data={"AETERM": "Headache", "AESTDTC": "2025-01-10"},
        )
        rec_10_v2 = Record(
            record_id=10,
            study_key=study,
            form_id=1,
            form_key="AE",
            subject_key="SUBJ-01",
            record_status="Record Complete",
            date_modified=datetime(2025, 1, 15, 8, 0, tzinfo=UTC),
            record_data={"AETERM": "Migraine", "AESTDTC": "2025-01-10"},
        )
        rec_20 = Record(
            record_id=20,
            study_key=study,
            form_id=1,
            form_key="AE",
            subject_key="SUBJ-01",
            record_status="Record Complete",
            date_modified=datetime(2025, 1, 16, 8, 0, tzinfo=UTC),
            record_data={"AETERM": "Nausea", "AESTDTC": "2025-01-16"},
        )

        # DuckDB incremental runs + FileStateProvider zero-PHI + failure rollback check
        ledger_file = tmp_path / "ledger.json"
        duck_path = tmp_path / "inc.duckdb"

        export_to_duckdb(
            _make_mock_sdk([rec_10_v1]),
            study,
            str(duck_path),
            "AE",
            mask_phi=False,
            as_sdtm=True,
            sdtm_domain="AE",
            incremental=True,
            ledger_path=ledger_file,
            quality_gate_enabled=False,
        )

        raw_ledger_text = ledger_file.read_text(encoding="utf-8")
        assert "SUBJ-01" not in raw_ledger_text
        ledger_json = json.loads(raw_ledger_text)
        streams_map = ledger_json["studies"][study]["streams"]
        stream_name, stream_entry = next(iter(streams_map.items()))
        seq_meta = stream_entry["metadata"][SDTM_SEQUENCER_METADATA_KEY]
        assert len(seq_meta["counters"]) == 1
        digest_key = next(iter(seq_meta["counters"]))
        assert len(digest_key) == 64

        # Simulate a failed incremental transaction -> prior_metadata must be restored intact
        provider = FileStateProvider(ledger_file)
        with pytest.raises(RuntimeError, match="boom"):
            with provider.transaction(study, stream_name) as tx:
                assert SDTM_SEQUENCER_METADATA_KEY in tx["metadata"]
                tx["metadata"][SDTM_SEQUENCER_METADATA_KEY] = {"corrupted": True}
                raise RuntimeError("boom")
        restored_entry = json.loads(ledger_file.read_text(encoding="utf-8"))["studies"][study][
            "streams"
        ][stream_name]
        assert restored_entry["last_run_status"].lower() == "failed"
        assert restored_entry["metadata"][SDTM_SEQUENCER_METADATA_KEY] == seq_meta

        # Run 2: recordId=20 arrives (plus updated recordId=10) -> AESEQ=1 updated, AESEQ=2 inserted
        export_to_duckdb(
            _make_mock_sdk([rec_10_v2, rec_20]),
            study,
            str(duck_path),
            "AE",
            mask_phi=False,
            as_sdtm=True,
            sdtm_domain="AE",
            incremental=True,
            ledger_path=ledger_file,
            quality_gate_enabled=False,
        )
        import duckdb

        with duckdb.connect(str(duck_path)) as conn:
            duck_rows = conn.execute(
                "SELECT CAST(AESEQ AS INTEGER), AETERM FROM AE ORDER BY AESEQ"
            ).fetchall()
        assert duck_rows == [(1, "Migraine"), (2, "Nausea")]

        # Snowflake incremental Run 1 (recordId=10) -> Run 2 (recordId=20 only)
        sf_ledger = tmp_path / "sf_ledger.json"
        sf_stage = tmp_path / "sf_inc"
        sf_sql: list[str] = []
        sf_conn = MagicMock()
        sf_cur = MagicMock()
        sf_cur.fetchall.return_value = []
        sf_cur.execute.side_effect = lambda sql, *_a, **_kw: sf_sql.append(sql)
        sf_conn.cursor.return_value = sf_cur
        sf_mod = MagicMock()
        sf_mod.connect.return_value = sf_conn
        with patch.dict("sys.modules", {"snowflake": MagicMock(), "snowflake.connector": sf_mod}):
            sf_cfg = SnowflakeSinkConfig(
                study_key=study,
                account="acct",
                user="usr",
                password="pwd",  # pragma: allowlist secret
                database="DB",
                schema="PUBLIC",
                warehouse="WH",
                stage="STG",
                table="AE",
                local_staging_dir=str(sf_stage),
                mask_phi=False,
                as_sdtm=True,
                sdtm_domain="AE",
                incremental=True,
                ledger_path=sf_ledger,
            )
            export_to_snowflake(_make_mock_sdk([rec_10_v1]), study, config=sf_cfg)
            first_files = set(sf_stage.glob("*.parquet"))
            first_sf_rows = pq.read_table(next(iter(first_files))).to_pylist()
            assert int(first_sf_rows[0]["AESEQ"]) == 1

            export_to_snowflake(_make_mock_sdk([rec_20]), study, config=sf_cfg)
            second_files = set(sf_stage.glob("*.parquet")) - first_files
            second_sf_rows = pq.read_table(next(iter(second_files))).to_pylist()
            assert int(second_sf_rows[0]["AESEQ"]) == 2
            assert any('target."AESEQ" = source."AESEQ"' in sql for sql in sf_sql)

        # Neo4j incremental Run 1 (recordId=10) -> Run 2 (recordId=20 only)
        neo_ledger = tmp_path / "neo_ledger.json"
        neo_store: dict[tuple[Any, ...], dict[str, Any]] = {}
        neo_session = MagicMock()
        neo_session.__enter__.return_value = neo_session

        def _neo_merge(_cypher: str, rows: list[dict[str, Any]]) -> None:
            for row in rows:
                key = (row["STUDYID"], row["DOMAIN"], row["USUBJID"], row.get("AESEQ"))
                neo_store[key] = dict(row)

        neo_session.run.side_effect = _neo_merge
        neo_driver = MagicMock()
        neo_driver.session.return_value = neo_session
        neo_mod = MagicMock()
        neo_mod.GraphDatabase.driver.return_value = neo_driver
        with patch.dict("sys.modules", {"neo4j": neo_mod}):
            neo_cfg = Neo4jSinkConfig(
                study_key=study,
                uri="bolt://localhost:7687",
                auth=("neo4j", "pwd"),
                mask_phi=False,
                as_sdtm=True,
                sdtm_domain="AE",
                incremental=True,
                ledger_path=neo_ledger,
            )
            export_to_neo4j(_make_mock_sdk([rec_10_v1]), study, config=neo_cfg)
            export_to_neo4j(_make_mock_sdk([rec_20]), study, config=neo_cfg)
        assert sorted((k[3], v["AETERM"]) for k, v in neo_store.items()) == [
            (1, "Headache"),
            (2, "Nausea"),
        ]

        # MongoDB incremental Run 1 (recordId=10) -> Run 2 (recordId=20 only)
        mongo_ledger = tmp_path / "mongo_ledger.json"
        mongo_store: dict[str, dict[str, Any]] = {}
        mongo_col = MagicMock()
        mongo_client = MagicMock()
        mongo_client.__getitem__.return_value.__getitem__.return_value = mongo_col
        pymongo_mod = MagicMock()
        pymongo_mod.MongoClient.return_value = mongo_client
        pymongo_mod.UpdateOne.side_effect = lambda flt, upd, upsert=False: {
            "id": flt["_id"],
            "doc": upd["$set"],
            "upsert": upsert,
        }
        mongo_col.bulk_write.side_effect = lambda ops, ordered=False: mongo_store.update(
            {op["id"]: dict(op["doc"]) for op in ops}
        )
        with patch.dict("sys.modules", {"pymongo": pymongo_mod}):
            mongo_cfg = MongoDbSinkConfig(
                study_key=study,
                uri="mongodb://localhost:27017",
                database="db",
                collection="ae",
                mask_phi=False,
                as_sdtm=True,
                sdtm_domain="AE",
                incremental=True,
                ledger_path=mongo_ledger,
            )
            export_to_mongodb(_make_mock_sdk([rec_10_v1]), study, config=mongo_cfg)
            export_to_mongodb(_make_mock_sdk([rec_20]), study, config=mongo_cfg)
        assert sorted((d["AESEQ"], d["AETERM"], doc_id) for doc_id, d in mongo_store.items()) == [
            (1, "Headache", f"{study}/10"),
            (2, "Nausea", f"{study}/20"),
        ]

    def test_airflow_state_provider_hydrates_and_rolls_back_prior_metadata(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        variables_store: dict[str, str] = {}

        class _FakeVariable:
            @staticmethod
            def get(key: str, default: Any = None, deserialize_json: bool = False) -> Any:
                if key not in variables_store:
                    return default
                raw = variables_store[key]
                return json.loads(raw) if deserialize_json else raw

            @staticmethod
            def set(
                key: str,
                value: Any,
                description: str | None = None,
                serialize_json: bool = False,
            ) -> None:
                _ = description
                variables_store[key] = json.dumps(value) if serialize_json else str(value)

            @staticmethod
            def delete(key: str) -> None:
                variables_store.pop(key, None)

            @staticmethod
            def keys(prefix: str | None = None) -> list[str]:
                if prefix is None:
                    return sorted(variables_store.keys())
                return sorted(k for k in variables_store if k.startswith(prefix))

        airflow_mod = ModuleType("airflow")
        sdk_mod = ModuleType("airflow.sdk")
        sdk_mod.Variable = _FakeVariable  # type: ignore[attr-defined]
        airflow_mod.sdk = sdk_mod  # type: ignore[attr-defined]

        monkeypatch.setitem(sys.modules, "airflow", airflow_mod)
        monkeypatch.setitem(sys.modules, "airflow.sdk", sdk_mod)
        monkeypatch.delitem(sys.modules, "airflow.sdk.execution_time", raising=False)
        monkeypatch.delitem(sys.modules, "airflow.sdk.execution_time.task_runner", raising=False)

        provider = AirflowStateProvider()
        seq = TransactionalSdtmSequencer()
        with seq.stage():
            seq.assign_seq("S1", "AE", "SUBJ-01", record_id=10)
        with provider.transaction("S1", "ae_stream") as tx:
            seq.write_to_metadata(tx["metadata"])
            tx["records_processed"] = 1
            tx["new_timestamp"] = datetime(2025, 2, 1, tzinfo=UTC)

        raw_var = next(iter(variables_store.values()))
        assert "SUBJ-01" not in raw_var
        committed_meta = json.loads(raw_var)["metadata"]
        assert SDTM_SEQUENCER_METADATA_KEY in committed_meta

        # Failed transaction preserves prior_metadata
        with pytest.raises(RuntimeError, match="abort"):
            with provider.transaction("S1", "ae_stream") as tx2:
                assert tx2["metadata"] == committed_meta
                tx2["metadata"]["mutated"] = True
                raise RuntimeError("abort")

        failed_payload = json.loads(next(iter(variables_store.values())))
        assert failed_payload["status"].lower() == "failed"
        assert failed_payload["metadata"] == committed_meta


class TestRecordLifecycleFilteringAndDmConsolidation:
    """Verify deleted/draft exclusion, DM consolidation without DMSEQ, and DM upserts."""

    def test_excludes_deleted_and_incomplete_drafts_and_consolidates_dm_across_sinks(
        self,
        tmp_path: Path,
    ) -> None:
        study = "STUDY_DM"
        records = [
            Record(
                record_id=1,
                study_key=study,
                form_id=3,
                form_key="DM",
                subject_key="SUBJ-01",
                deleted=True,
                record_status="Record Complete",
                record_data={"SEX": "M"},
            ),
            Record(
                record_id=2,
                study_key=study,
                form_id=3,
                form_key="DM",
                subject_key="SUBJ-01",
                deleted=None,
                record_status="DELETED",
                record_data={"SEX": "M"},
            ),
            Record(
                record_id=3,
                study_key=study,
                form_id=3,
                form_key="DM",
                subject_key="SUBJ-01",
                deleted=False,
                record_status="Record Incomplete",
                record_data={"SEX": "U"},
            ),
            Record(
                record_id=4,
                study_key=study,
                form_id=3,
                form_key="DM",
                subject_key="SUBJ-01",
                deleted=False,
                record_status="Partial",
                record_data={"RACE": "UNKNOWN"},
            ),
            # Two valid DM revisions for SUBJ-01 -> consolidated into 1 row without DMSEQ
            Record(
                record_id=5,
                study_key=study,
                form_id=3,
                form_key="DM",
                subject_key="SUBJ-01",
                deleted=False,
                record_status="Record Complete",
                date_modified=datetime(2025, 1, 1, tzinfo=UTC),
                record_data={"SEX": "F", "BRTHDTC": "1985-04-12"},
            ),
            Record(
                record_id=6,
                study_key=study,
                form_id=3,
                form_key="DM",
                subject_key="SUBJ-01",
                deleted=False,
                record_status="Record Complete",
                date_modified=datetime(2025, 1, 2, tzinfo=UTC),
                record_data={"RACE": "ASIAN", "ETHNIC": "NOT HISPANIC OR LATINO"},
            ),
        ]

        sdk = _make_mock_sdk(records)
        mapper = RecordMapper(sdk)
        dm_df = mapper.sdtm_dataframe(study, domain="DM", mask_phi=False, include_deleted=False)
        assert len(dm_df) == 1
        assert "DMSEQ" not in dm_df.columns
        assert dm_df.iloc[0]["SEX"] == "F"
        assert dm_df.iloc[0]["RACE"] == "ASIAN"

        # CentralizedMapper via build_sink_mapper honors SinkConfig.include_deleted=False
        sink_mapper = build_sink_mapper(
            SinkConfig(
                study_key=study,
                as_sdtm=True,
                sdtm_domain="DM",
                include_deleted=False,
                mask_phi=False,
            ),
            mode="document",
        )
        for inactive_rec in records[:4]:
            assert sink_mapper.map_record(inactive_rec, study_key=study) == {}

        # DuckDB DM upsert by (STUDYID, DOMAIN, USUBJID) without DMSEQ
        import duckdb

        duck_path = tmp_path / "dm.duckdb"
        dm_ledger = tmp_path / "dm_ledger.json"
        export_to_duckdb(
            sdk,
            study,
            str(duck_path),
            "DM",
            mask_phi=False,
            as_sdtm=True,
            sdtm_domain="DM",
            incremental=True,
            ledger_path=dm_ledger,
            quality_gate_enabled=False,
        )
        updated_dm = Record(
            record_id=7,
            study_key=study,
            form_id=3,
            form_key="DM",
            subject_key="SUBJ-01",
            record_status="Record Complete",
            date_modified=datetime(2025, 1, 5, tzinfo=UTC),
            record_data={"SEX": "F", "RACE": "WHITE"},
        )
        export_to_duckdb(
            _make_mock_sdk([updated_dm]),
            study,
            str(duck_path),
            "DM",
            mask_phi=False,
            as_sdtm=True,
            sdtm_domain="DM",
            incremental=True,
            ledger_path=dm_ledger,
            quality_gate_enabled=False,
        )
        with duckdb.connect(str(duck_path)) as conn:
            cols = [r[0] for r in conn.execute("DESCRIBE DM").fetchall()]
            assert "DMSEQ" not in cols
            dm_rows = conn.execute("SELECT USUBJID, SEX, RACE FROM DM").fetchall()
        assert dm_rows == [("SUBJ-01", "F", "WHITE")]

        # Direct TabularDuckDBSink also handles Record batches in SDTM mode
        direct_duck = tmp_path / "direct_dm.duckdb"
        with TabularDuckDBSink(
            direct_duck,
            "DM",
            TabularSinkConfig(
                study_key=study,
                as_sdtm=True,
                sdtm_domain="DM",
                mask_phi=False,
                incremental=True,
            ),
        ) as duck_sink:
            assert duck_sink.write_batch(dm_df, batch_id="dm/0") == 1

        # Snowflake DM MERGE INTO keys on (STUDYID, DOMAIN, USUBJID) without DMSEQ
        sf_sql: list[str] = []
        sf_conn = MagicMock()
        sf_cur = MagicMock()
        sf_cur.fetchall.return_value = []
        sf_cur.execute.side_effect = lambda sql, *_a, **_kw: sf_sql.append(sql)
        sf_conn.cursor.return_value = sf_cur
        sf_mod = MagicMock()
        sf_mod.connect.return_value = sf_conn
        with patch.dict("sys.modules", {"snowflake": MagicMock(), "snowflake.connector": sf_mod}):
            sf_sink = SnowflakeExportSink(
                SnowflakeSinkConfig(
                    study_key=study,
                    account="acct",
                    user="usr",
                    password="pwd",  # pragma: allowlist secret
                    database="DB",
                    schema="PUBLIC",
                    warehouse="WH",
                    stage="STG",
                    table="DM",
                    local_staging_dir=str(tmp_path / "sf_dm"),
                    mask_phi=False,
                    as_sdtm=True,
                    sdtm_domain="DM",
                )
            )
            assert sf_sink.write_batch(records, batch_id="dm0") == 1
        merge_stmt = next(s for s in sf_sql if s.startswith("MERGE INTO"))
        assert "DMSEQ" not in merge_stmt
        assert (
            'target."STUDYID" = source."STUDYID" '
            'AND target."DOMAIN" = source."DOMAIN" '
            'AND target."USUBJID" = source."USUBJID"'
        ) in merge_stmt

        # Neo4j DM MERGE keys on {STUDYID, DOMAIN, USUBJID} without DMSEQ
        neo_calls: list[tuple[str, list[dict[str, Any]]]] = []
        neo_session = MagicMock()
        neo_session.__enter__.return_value = neo_session
        neo_session.run.side_effect = lambda q, rows=None, **_kw: neo_calls.append(
            (q, list(rows or []))
        )
        neo_driver = MagicMock()
        neo_driver.session.return_value = neo_session
        neo_mod = MagicMock()
        neo_mod.GraphDatabase.driver.return_value = neo_driver
        with patch.dict("sys.modules", {"neo4j": neo_mod}):
            neo_sink = Neo4jExportSink(
                Neo4jSinkConfig(
                    study_key=study,
                    uri="bolt://localhost:7687",
                    auth=("neo4j", "pwd"),
                    mask_phi=False,
                    as_sdtm=True,
                    sdtm_domain="DM",
                )
            )
            assert neo_sink.write_batch(records, batch_id="dm0") == 1
        assert len(neo_calls) == 1
        cypher_used, neo_dm_rows = neo_calls[0]
        assert (
            "MERGE (r:Record   {STUDYID: row.STUDYID, DOMAIN: row.DOMAIN, USUBJID: row.USUBJID})"
            in cypher_used
        )
        assert len(neo_dm_rows) == 1
        assert "DMSEQ" not in neo_dm_rows[0]

        # MongoDB DM upsert keys _id on "<STUDYID>/DM/<USUBJID>" without DMSEQ
        mongo_ops: list[dict[str, Any]] = []
        mongo_col = MagicMock()
        mongo_client = MagicMock()
        mongo_client.__getitem__.return_value.__getitem__.return_value = mongo_col
        pymongo_mod = MagicMock()
        pymongo_mod.MongoClient.return_value = mongo_client
        pymongo_mod.UpdateOne.side_effect = lambda flt, upd, upsert=False: {
            "filter": flt,
            "doc": upd["$set"],
            "upsert": upsert,
        }
        mongo_col.bulk_write.side_effect = lambda ops, ordered=False: mongo_ops.extend(ops)
        with patch.dict("sys.modules", {"pymongo": pymongo_mod}):
            mongo_sink = MongoDbExportSink(
                MongoDbSinkConfig(
                    study_key=study,
                    uri="mongodb://localhost:27017",
                    database="db",
                    collection="dm",
                    mask_phi=False,
                    as_sdtm=True,
                    sdtm_domain="DM",
                )
            )
            assert mongo_sink.write_batch(records, batch_id="dm0") == 1
        assert len(mongo_ops) == 1
        assert mongo_ops[0]["filter"] == {"_id": f"{study}/DM/SUBJ-01"}
        assert "DMSEQ" not in mongo_ops[0]["doc"]
        assert mongo_ops[0]["doc"]["SEX"] == "F"
        assert mongo_ops[0]["doc"]["RACE"] == "ASIAN"


class TestStandardsProfilesBooleanTokensAndReadinessGates:
    """Verify 'F' preservation, biologic/vaccine profiles, and unified pre-masking gates."""

    def test_categorical_normalizer_preserves_f_and_coerces_only_explicit_booleans(self) -> None:
        normalizer = CategoricalNormalizer()
        res = normalizer.normalize_record(
            {
                "subjectKey": "SUBJ-01",
                "sex": "F",
                "vsUnit": "F",
                "aeSerious": "true",
                "aeOngoing": "FALSE",
                "rawFlag": "N",
            }
        )
        assert res.normalized_record["sex"] == "F"
        assert res.normalized_record["vsUnit"] == "F"
        assert res.normalized_record["aeSerious"] is True
        assert res.normalized_record["aeOngoing"] is False
        assert res.normalized_record["rawFlag"] == "N"

        validator = StandardsReadinessValidator(GeneralClinicalProfile())
        report = validator.score_records(
            records_by_domain={
                "DM": [
                    {
                        "subjectKey": "SUBJ-01",
                        "sex": "F",
                        "birthDate": "1985-04-12",
                        "age": 40,
                        "race": "ASIAN",
                        "ethnicity": "NOT HISPANIC OR LATINO",
                    }
                ],
                "VS": [
                    {
                        "subjectKey": "SUBJ-01",
                        "vsTest": "Temperature",
                        "vsTestCd": "TEMP",
                        "vsResult": 98.6,
                        "vsUnit": "F",
                        "vsDate": "2025-03-01",
                    }
                ],
            }
        )
        assert report.score == 100.0
        assert report.is_ready is True
        assert report.violations == []

    def test_biologic_and_vaccine_profiles_resolve_across_registry_export_and_cli(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        assert isinstance(resolve_standards_profile("biologic"), BiologicSafetyProfile)
        assert isinstance(resolve_standards_profile("vaccine"), VaccineSafetyProfile)
        assert {"biologic", "vaccine"}.issubset(set(PROFILE_REGISTRY.list_profiles()))

        # Biologic enforces EX route and CTCAE grades on AE
        bio = BiologicSafetyProfile()
        bio_ex_violations = bio.validate(
            "EX",
            {
                "subjectKey": "S1",
                "exTreatment": "MAB-101",
                "exDose": 100,
                "exStartDate": "2025-01-01",
            },
        )
        assert any(v.field == "exRoute" and v.severity == "ERROR" for v in bio_ex_violations)

        # Vaccine enforces AE seriousness and EX route/dose
        vax = VaccineSafetyProfile()
        vax_ae_violations = vax.validate(
            "AE",
            {
                "subjectKey": "S1",
                "aeTerm": "Injection site pain",
                "aeDecod": "INJECTION SITE PAIN",
                "aeSeverity": "MILD",
                "aeRelationship": "RELATED",
                "aeOutcome": "RECOVERED",
            },
        )
        assert any(v.field == "aeSerious" and v.severity == "ERROR" for v in vax_ae_violations)

        # CLI export --standards-profile biologic & vaccine succeed on compliant EX records
        study = "STUDY_PROFILES"
        ex_rec = Record(
            record_id=1,
            study_key=study,
            form_id=10,
            form_key="EX",
            subject_key="SUBJ-01",
            record_status="Record Complete",
            record_data={
                "exTreatment": "VAX-01",
                "exDose": 0.5,
                "exDoseUnit": "mL",
                "exRoute": "INTRAMUSCULAR",
                "exStartDate": "2025-02-01",
                "exEndDate": "2025-02-01",
            },
        )
        sdk = _make_mock_sdk(
            [ex_rec],
            forms=[Form(form_id=10, form_key="EX", form_name="Exposure")],
            variables=[
                Variable(variable_id=1, form_id=10, form_key="EX", variable_name="exTreatment"),
                Variable(variable_id=2, form_id=10, form_key="EX", variable_name="exDose"),
                Variable(variable_id=3, form_id=10, form_key="EX", variable_name="exDoseUnit"),
                Variable(variable_id=4, form_id=10, form_key="EX", variable_name="exRoute"),
                Variable(variable_id=5, form_id=10, form_key="EX", variable_name="exStartDate"),
                Variable(variable_id=6, form_id=10, form_key="EX", variable_name="exEndDate"),
            ],
        )
        for prof_name in ("biologic", "vaccine"):
            out_csv = tmp_path / f"{prof_name}_ex.csv"
            with patch("imednet.cli.utils.context.get_sdk", return_value=sdk):
                cli_mod.app(
                    [
                        "export",
                        "csv",
                        study,
                        str(out_csv),
                        "--sdtm-domain",
                        "EX",
                        "--standards-profile",
                        prof_name,
                        "--min-standards-score",
                        "90",
                    ]
                )
            assert out_csv.exists()
            capsys.readouterr()

    def test_quality_gate_batch_readiness_and_validate_standards_pre_masking_scoring(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        study = "STUDY_GATE"
        valid_dm = Record(
            record_id=1,
            study_key=study,
            form_id=3,
            form_key="DM",
            subject_key="SUBJ-01",
            record_status="Record Complete",
            record_data={
                "sex": "F",
                "birthDate": "1980-05-15",
                "age": 44,
                "race": "ASIAN",
                "ethnicity": "NOT HISPANIC OR LATINO",
            },
        )
        invalid_dm = Record(
            record_id=2,
            study_key=study,
            form_id=3,
            form_key="DM",
            subject_key="SUBJ-02",
            record_status="Record Complete",
            record_data={
                "sex": "",
                "birthDate": "bad-1980-05-15",
                "age": 44,
                "race": "ASIAN",
                "ethnicity": "NOT HISPANIC OR LATINO",
            },
        )
        sdk = _make_mock_sdk([valid_dm, invalid_dm])

        # 1. apply_quality_gate drops invalid_dm under min_standards_score while keeping valid_dm
        gate_cfg = SinkConfig(
            study_key=study,
            sdtm_domain="DM",
            standards_profile="general",
            min_standards_score=95.0,
            quality_gate_enabled=True,
            min_schema_readiness_score=0.0,
        )
        kept = list(apply_quality_gate(sdk, study, [valid_dm, invalid_dm], gate_cfg, variables=[]))
        assert [r.record_id for r in kept] == [1]

        # 2. _enforce_batch_standards_readiness rejects ANY ERROR violation even when min_standards_score=0.0
        with pytest.raises(ExportError, match="error_violations="):
            _enforce_batch_standards_readiness(
                [invalid_dm],
                batch_id="dm/0",
                study_key=study,
                config=SinkConfig(study_key=study, mask_phi=True),
                standards_profile="general",
                min_standards_score=0.0,
                sdtm_domain="DM",
            )

        # 3. validate-standards scores unmasked DM (birthDate + sex='F') at 100.0 without [MASKED] errors
        sdk_valid = _make_mock_sdk(
            [valid_dm],
            forms=[Form(form_id=3, form_key="DM", form_name="Demographics")],
            variables=[],
        )
        with patch("imednet.cli.utils.context.get_sdk", return_value=sdk_valid):
            cli_mod.app(
                [
                    "workflows",
                    "validate-standards",
                    study,
                    "--profile",
                    "general",
                    "--domain",
                    "DM",
                    "--format",
                    "json",
                ]
            )
        out_ok, err_ok = capsys.readouterr()
        assert err_ok == ""
        assert "1980-05-15" not in out_ok
        payload_ok = json.loads(out_ok)
        assert payload_ok["score"] == 100.0
        assert payload_ok["is_ready"] is True
        assert payload_ok["passed"] is True
        assert payload_ok["violations"] == []

        # 4. validate-standards masks PHI in emitted diagnostics and exits 1 when passed is False
        sdk_invalid = _make_mock_sdk(
            [invalid_dm],
            forms=[Form(form_id=3, form_key="DM", form_name="Demographics")],
            variables=[],
        )
        with (
            patch("imednet.cli.utils.context.get_sdk", return_value=sdk_invalid),
            pytest.raises(SystemExit) as exc_fail,
        ):
            cli_mod.app(
                [
                    "workflows",
                    "validate-standards",
                    study,
                    "--profile",
                    "general",
                    "--domain",
                    "DM",
                    "--format",
                    "json",
                ]
            )
        assert exc_fail.value.code == 1
        out_fail, _ = capsys.readouterr()
        assert "1980-05-15" not in out_fail
        payload_fail = json.loads(out_fail)
        assert payload_fail["passed"] is False
        assert any(v["severity"] == "ERROR" for v in payload_fail["violations"])
