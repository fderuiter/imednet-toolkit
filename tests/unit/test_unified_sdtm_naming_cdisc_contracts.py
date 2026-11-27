"""Comprehensive contract tests for unified SDTM, naming_convention, and CDISC exchange."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import sqlalchemy as sa

import imednet.integrations.enrichment as enrichment_mod
import imednet.spi.cdisc as spi_cdisc
import imednet.spi.sinks as spi_sinks
import imednet_sinks.document as doc_mod
import imednet_sinks.graph as graph_mod
import imednet_sinks.warehouse as wh_mod
from apache_airflow_providers_imednet.operators.export import ImednetExportOperator
from imednet.cli import app as cli_app
from imednet.datadict import cdisc_builders, to_dataset_json, to_odm_file, to_odm_xml
from imednet.datadict.cdisc_builders import (
    ODM_NS,
    build_dataset_json_envelope,
    build_odm_archive,
    validate_dataset_json,
    validate_odm_xml,
)
from imednet.datadict.exchange import (
    OdmItemData,
    OdmItemGroupData,
    OdmReferenceData,
)
from imednet.errors import ExportConfigurationError
from imednet.integrations.cdisc_export import export_to_dataset_json, export_to_odm
from imednet.integrations.enrichment import CentralizedMapper, EnrichmentPipeline
from imednet.integrations.export import (
    export_to_csv,
    export_to_duckdb,
    export_to_excel,
    export_to_json,
    export_to_long_sql,
    export_to_parquet,
    export_to_sql,
)
from imednet.integrations.parquet import export_to_hive_parquet
from imednet.integrations.sink_base import SinkConfig
from imednet.models.forms import Form
from imednet.models.records import Record
from imednet.models.sdtm import (
    deduplicate_sdtm_rows,
    format_nested_variable_name,
    prefix_colliding_variable_name,
    transform_records_to_sdtm,
    transform_records_to_sdtm_rows,
)
from imednet.models.study_config import MappingRule, StudyConfiguration
from imednet.models.variables import Variable
from imednet_sinks import (
    MongoDbExportSink,
    MongoDbSinkConfig,
    Neo4jExportSink,
    Neo4jSinkConfig,
    SnowflakeExportSink,
    SnowflakeSinkConfig,
)
from imednet_workflows.record_mapper import AsyncRecordMapper, RecordMapper
from imednet_workflows.state_ledger import ExtractionStateLedger


def _make_mock_sdk(
    records: list[Record],
    *,
    forms: list[Form] | None = None,
    variables: list[Variable] | None = None,
) -> MagicMock:
    sdk = MagicMock(sinks=None)
    resolved_forms = forms or [
        Form(form_id=10, form_key="DM", form_name="Demographics", form_type="Enrollment"),
        Form(form_id=20, form_key="VS", form_name="Vital Signs", form_type="Visit"),
        Form(form_id=30, form_key="AE", form_name="Adverse Events", form_type="Visit"),
    ]
    resolved_vars = variables or []
    sdk.forms.list.return_value = resolved_forms
    sdk.get_forms.return_value = resolved_forms
    sdk.variables.list.return_value = resolved_vars
    sdk.get_variables.return_value = resolved_vars
    sdk.visits.list.return_value = []
    sdk.get_visits.return_value = []
    sdk.intervals.list.return_value = []
    sdk.get_intervals.return_value = []
    sdk.sites.list.return_value = []
    sdk.get_sites.return_value = []

    def _list_records(**kw: Any) -> Any:
        flt = str(kw.get("filter", "") or "")
        out = [r.model_copy(deep=True) for r in records]
        if kw.get("formId") is not None:
            raw_fid = kw["formId"]
            fids = (
                {int(x) for x in raw_fid}
                if isinstance(raw_fid, (list, tuple, set))
                else {int(raw_fid)}
            )
            out = [r for r in out if r.form_id in fids]
        if "deleted==false" in flt:
            out = [r for r in out if not r.deleted]
        return iter(out)

    sdk.records.list.side_effect = _list_records
    sdk.records.iter.side_effect = _list_records
    sdk.get_records.side_effect = lambda **kw: list(_list_records(**kw))
    return sdk


def test_spi_stateless_helpers_exposed_on_sinks_and_cdisc() -> None:
    """Verify stateless helpers are re-exported on imednet.spi.sinks and imednet.spi.cdisc."""
    assert spi_sinks.format_nested_variable_name is format_nested_variable_name
    assert spi_sinks.prefix_colliding_variable_name is prefix_colliding_variable_name
    assert spi_sinks.deduplicate_sdtm_rows is deduplicate_sdtm_rows
    assert callable(spi_sinks.resolve_effective_as_sdtm)
    assert callable(spi_sinks.is_incremental_sink_config)
    assert callable(spi_sinks.should_preserve_sdtm_record_id)

    assert spi_cdisc.format_nested_variable_name is format_nested_variable_name
    assert spi_cdisc.prefix_colliding_variable_name is prefix_colliding_variable_name
    assert spi_cdisc.deduplicate_sdtm_rows is deduplicate_sdtm_rows


def test_unassigned_subject_preserves_none_in_sdtm_dataset_json_and_neo4j(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 1: Missing/blank subject_key preserves USUBJID=None and guards Neo4j Subject merges."""
    recs = [
        Record(
            record_id=1,
            study_key="STUDY_UNASSIGNED",
            subject_key=None,
            form_id=20,
            form_key="VS",
            record_data={"VSTESTCD": "HR", "VSORRES": 70},
        ),
        Record(
            record_id=2,
            study_key="STUDY_UNASSIGNED",
            subject_key="   ",
            form_id=20,
            form_key="VS",
            record_data={"VSTESTCD": "SYSBP", "VSORRES": 120},
        ),
    ]
    rows = transform_records_to_sdtm_rows(recs, domain="VS", study_key="STUDY_UNASSIGNED")
    assert len(rows) == 2
    assert rows[0]["USUBJID"] is None
    assert rows[1]["USUBJID"] is None
    assert [r["VSSEQ"] for r in rows] == [1, 2]

    df = transform_records_to_sdtm(recs, domain="VS", study_key="STUDY_UNASSIGNED")
    assert df["USUBJID"].isna().all()

    env = build_dataset_json_envelope(
        "STUDY_UNASSIGNED",
        records=recs,
        as_sdtm=True,
        sdtm_domain="VS",
    )
    assert validate_dataset_json(to_dataset_json(env)) == []
    ig = env.clinical_data.item_group_data["IG.VS"]
    usub_idx = [it.name for it in ig.items].index("USUBJID")
    assert [row[usub_idx] for row in ig.item_data] == [None, None]

    # Neo4j SDTM export guards null/empty USUBJID and links Study -> Record without Subject node
    neo4j_calls: list[tuple[str, dict[str, Any]]] = []
    neo4j_mod = MagicMock()
    session = MagicMock()
    session.run.side_effect = lambda q, **p: neo4j_calls.append((q, p)) or MagicMock()
    session_cm = MagicMock()
    session_cm.__enter__.return_value = session
    session_cm.__exit__.return_value = False
    driver = MagicMock()
    driver.session.return_value = session_cm
    neo4j_mod.GraphDatabase.driver.return_value = driver
    monkeypatch.setattr(graph_mod, "_require_optional_dep", lambda *_: neo4j_mod)

    cfg = Neo4jSinkConfig(
        study_key="STUDY_UNASSIGNED",
        uri="bolt://localhost:7687",
        auth=("neo4j", "secret"),
        as_sdtm=True,
        sdtm_domain="VS",
    )
    with Neo4jExportSink(cfg) as sink:
        assert sink.write_batch(recs, batch_id="b_unassigned") == 2

    assert len(neo4j_calls) == 1
    cypher, params = neo4j_calls[0]
    assert "USUBJID: row.USUBJID" not in cypher.split("SET r += row")[0]
    assert "WHEN row.USUBJID IS NOT NULL AND row.USUBJID <> ''" in cypher
    assert "WHEN row.USUBJID IS NULL OR row.USUBJID = ''" in cypher
    assert "MERGE (s)-[:HAS_RECORD]->(r)" in cypher
    assert all(r["USUBJID"] is None for r in params["rows"])


def test_odm_unassigned_records_route_to_reference_data_and_round_trip(tmp_path: Path) -> None:
    """AC 2: Unassigned Records emit inside <ReferenceData>, pass strict XSD, and round-trip incrementally."""
    forms = [Form(form_id=20, form_key="VS", form_name="Vital Signs", form_type="Visit")]
    variables = [
        Variable(
            variable_id=1,
            variable_name="HR",
            label="Heart Rate",
            variable_type="integer",
            form_id=20,
            form_key="VS",
        )
    ]
    static_ref = OdmReferenceData(
        study_oid="STUDY_ODM_REF",
        metadata_version_oid="MDV.001",
        item_group_data=(
            OdmItemGroupData(
                item_group_oid="IG.VS",
                item_group_repeat_key="STATIC-1",
                items=(OdmItemData(item_oid="IT.VS.HR", value="65"),),
            ),
        ),
    )
    rec_unassigned = Record(
        record_id=901,
        study_key="STUDY_ODM_REF",
        subject_key=None,
        form_id=20,
        form_key="VS",
        date_modified=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
        record_data={"HR": 72},
    )
    rec_assigned = Record(
        record_id=902,
        study_key="STUDY_ODM_REF",
        subject_key="SUBJ-01",
        form_id=20,
        form_key="VS",
        date_modified=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
        record_data={"HR": 80},
    )

    archive = build_odm_archive(
        "STUDY_ODM_REF",
        forms=forms,
        variables=variables,
        records=[rec_unassigned, rec_assigned],
        reference_data=[static_ref],
    )
    xml_text = to_odm_xml(archive)
    assert 'SubjectKey="UNKNOWN"' not in xml_text
    assert len(archive.clinical_data[0].subjects) == 1
    assert archive.clinical_data[0].subjects[0].subject_key == "SUBJ-01"
    assert len(archive.reference_data) == 1
    repeat_keys = {ig.item_group_repeat_key for ig in archive.reference_data[0].item_group_data}
    assert repeat_keys == {"STATIC-1", "901"}

    odm_file = tmp_path / "study.odm.xml"
    to_odm_file(archive, odm_file)
    assert validate_odm_xml(odm_file) == []

    # Incremental export_to_odm updates unassigned Record 901 without duplicating or losing STATIC-1
    rec_unassigned_v2 = rec_unassigned.model_copy(
        update={
            "record_data": {"HR": 75},
            "date_modified": datetime(2026, 7, 2, 10, 0, tzinfo=UTC),
        }
    )
    sdk = _make_mock_sdk([rec_unassigned_v2], forms=forms, variables=variables)
    ledger = ExtractionStateLedger(ledger_path=tmp_path / "odm_ledger.json")
    export_to_odm(
        sdk,
        "STUDY_ODM_REF",
        odm_file,
        incremental=True,
        state_provider=ledger,
    )
    assert validate_odm_xml(odm_file) == []
    root = ET.fromstring(odm_file.read_text(encoding="utf-8"))  # noqa: S314
    ns = {"odm": ODM_NS}
    subj_keys = [el.attrib["SubjectKey"] for el in root.findall(".//odm:SubjectData", ns)]
    assert subj_keys == ["SUBJ-01"]
    ref_igs = root.findall(".//odm:ReferenceData/odm:ItemGroupData", ns)
    by_repeat = {
        ig.attrib["ItemGroupRepeatKey"]: ig.find("odm:ItemData", ns).attrib["Value"]  # type: ignore[union-attr]
        for ig in ref_igs
    }
    assert by_repeat == {"STATIC-1": "65", "901": "75"}

    # Voiding unassigned Record 901 removes it from <ReferenceData> while retaining STATIC-1
    rec_unassigned_void = rec_unassigned_v2.model_copy(
        update={"deleted": True, "date_modified": datetime(2026, 7, 3, 10, 0, tzinfo=UTC)}
    )
    sdk_void = _make_mock_sdk([rec_unassigned_void], forms=forms, variables=variables)
    export_to_odm(
        sdk_void,
        "STUDY_ODM_REF",
        odm_file,
        incremental=True,
        state_provider=ledger,
    )
    assert validate_odm_xml(odm_file) == []
    root_after_void = ET.fromstring(odm_file.read_text(encoding="utf-8"))  # noqa: S314
    ref_igs_after = root_after_void.findall(".//odm:ReferenceData/odm:ItemGroupData", ns)
    assert [ig.attrib["ItemGroupRepeatKey"] for ig in ref_igs_after] == ["STATIC-1"]


def test_incremental_only_preserve_record_id_and_explicit_as_sdtm_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 3: should_preserve_sdtm_record_id defaults to incremental-only and as_sdtm=False is honored."""
    full_cfg = SinkConfig(study_key="S1", as_sdtm=True)
    assert spi_sinks.is_incremental_sink_config(full_cfg) is False
    assert spi_sinks.should_preserve_sdtm_record_id(full_cfg) is False

    inc_cfg = SinkConfig(study_key="S1", as_sdtm=True, incremental=True, ledger_path="l.json")
    assert spi_sinks.is_incremental_sink_config(inc_cfg) is True
    assert spi_sinks.should_preserve_sdtm_record_id(inc_cfg) is True

    mod_cfg = SinkConfig(
        study_key="S1",
        as_sdtm=True,
        modified_since=datetime(2026, 7, 1, tzinfo=UTC),
    )
    assert spi_sinks.should_preserve_sdtm_record_id(mod_cfg) is True

    # Explicit as_sdtm=False with sdtm_domain="AE" stays raw across SinkConfig, CentralizedMapper,
    # export_to_csv, RecordMapper, and ImednetExportOperator
    raw_domain_cfg = SinkConfig(
        study_key="S1",
        as_sdtm=False,
        sdtm_domain="AE",
        naming_convention="sdtm",
    )
    assert raw_domain_cfg.as_sdtm is False

    mapper = CentralizedMapper(
        mode="tabular",
        as_sdtm=False,
        sdtm_domain="AE",
        naming_convention="sdtm",
    )
    mapped = mapper.map_record(
        Record(
            record_id=10,
            study_key="S1",
            subject_key="SUBJ-1",
            form_id=20,
            form_key="VS",
            record_data={"heartRate": 74},
        ),
        study_key="S1",
    )
    assert "AESEQ" not in mapped
    assert "DOMAIN" not in mapped
    assert mapped["STUDYID"] == "S1"
    assert mapped["RECORD_DATA"]["HEARTRATE"] == 74

    # export_to_csv with as_sdtm=False, sdtm_domain="AE" does NOT filter out VS forms
    vs_rec = Record(
        record_id=11,
        study_key="S1",
        subject_key="SUBJ-1",
        form_id=20,
        form_key="VS",
        record_data={"heartRate": 74},
    )
    sdk = _make_mock_sdk(
        [vs_rec],
        variables=[
            Variable(
                variable_id=1,
                variable_name="heartRate",
                label="Heart Rate",
                variable_type="integer",
                form_id=20,
                form_key="VS",
            )
        ],
    )
    raw_csv = tmp_path / "raw_with_domain.csv"
    export_to_csv(
        sdk,
        "S1",
        raw_csv,
        as_sdtm=False,
        sdtm_domain="AE",
        naming_convention="sdtm",
    )
    df_raw = pd.read_csv(raw_csv)
    assert len(df_raw) == 1
    assert "HEARTRATE" in df_raw.columns
    assert "AESEQ" not in df_raw.columns

    # RecordMapper.to_dataset_json honors explicit as_sdtm=False with domain="AE"
    rm = RecordMapper(sdk)
    dj_env = rm.to_dataset_json("S1", as_sdtm=False, domain="AE", naming_convention="sdtm")
    assert "IG.VS" in dj_env.clinical_data.item_group_data

    # Airflow ImednetExportOperator honors explicit as_sdtm=False when sdtm_domain="AE"
    airflow_csv = tmp_path / "airflow.csv"
    op = ImednetExportOperator(
        task_id="export_raw_domain",
        study_key="S1",
        destination="csv",
        output_path=str(airflow_csv),
        as_sdtm=False,
        sdtm_domain="AE",
        naming_convention="sdtm",
    )
    assert op.as_sdtm is False
    fake_hook = MagicMock()
    fake_hook.get_sdk_client.return_value = sdk
    monkeypatch.setattr(
        "apache_airflow_providers_imednet.operators.export.ImednetHook",
        lambda *_a, **_kw: fake_hook,
    )
    op.execute({})
    df_airflow = pd.read_csv(airflow_csv)
    assert len(df_airflow) == 1
    assert "HEARTRATE" in df_airflow.columns
    assert "AESEQ" not in df_airflow.columns


def test_dm_deduplication_and_composite_keying_across_mappers_and_all_sinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 4 & 5: Multi-record DM deduplicates to 1 row per Subject and upserts on (STUDYID, DOMAIN, USUBJID)."""
    study_key = "STUDY_DM_DEDUP"
    dm_r1 = Record(
        record_id=101,
        study_key=study_key,
        subject_key="SUBJ-01",
        form_id=10,
        form_key="DM",
        date_modified=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
        record_data={"SEX": "U", "AGE": 40, "BRTHDTC": "1985-04-12"},
    )
    dm_r2 = Record(
        record_id=102,
        study_key=study_key,
        subject_key="SUBJ-01",
        form_id=10,
        form_key="DM",
        date_modified=datetime(2026, 7, 2, 10, 0, tzinfo=UTC),
        record_data={"SEX": "F", "AGE": 41, "BRTHDTC": "1985-04-12"},
    )

    # Core SDTM rows & DataFrame deduplicate DM even when preserve_record_id=True
    sdtm_rows = transform_records_to_sdtm_rows(
        [dm_r1, dm_r2],
        domain="DM",
        study_key=study_key,
        preserve_record_id=True,
    )
    assert len(sdtm_rows) == 1
    assert sdtm_rows[0]["SEX"] == "F"
    assert sdtm_rows[0]["RECORD_ID"] == 102
    assert sdtm_rows[0]["BRTHDTC"] == "1985"

    # RecordMapper chunked & combined DM deduplication
    sdk_both = _make_mock_sdk([dm_r1, dm_r2])
    rm = RecordMapper(sdk_both, chunk_size=1)
    df_dm = rm.sdtm_dataframe(study_key, "DM")
    assert len(df_dm) == 1
    assert df_dm.iloc[0]["SEX"] == "F"
    env_dm = rm.sdtm_dataset_json(study_key, "DM")
    assert env_dm.clinical_data.item_group_data["IG.DM"].records == 1

    # Incremental Run 1 (recordId=101) -> Run 2 (recordId=102) across tabular, CDISC, and plugin sinks
    current_records = [dm_r1]
    sdk_inc = MagicMock(sinks=None)
    dm_forms = [Form(form_id=10, form_key="DM", form_name="Demographics", form_type="Enrollment")]
    sdk_inc.forms.list.return_value = dm_forms
    sdk_inc.get_forms.return_value = dm_forms
    sdk_inc.variables.list.return_value = []
    sdk_inc.get_variables.return_value = []
    sdk_inc.visits.list.return_value = []
    sdk_inc.get_visits.return_value = []
    sdk_inc.intervals.list.return_value = []
    sdk_inc.get_intervals.return_value = []
    sdk_inc.records.list.side_effect = lambda **_kw: iter(
        [r.model_copy(deep=True) for r in current_records]
    )
    sdk_inc.records.iter.side_effect = lambda **_kw: iter(
        [r.model_copy(deep=True) for r in current_records]
    )

    csv_file = tmp_path / "dm.csv"
    pq_file = tmp_path / "dm.parquet"
    xlsx_file = tmp_path / "dm.xlsx"
    json_file = tmp_path / "dm.json"
    sql_uri = f"sqlite:///{tmp_path / 'dm.sqlite'}"
    ddb_file = tmp_path / "dm.duckdb"
    hive_dir = tmp_path / "dm_hive"
    dj_file = tmp_path / "dm.dataset.json"

    wm1 = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    wm2 = datetime(2026, 7, 2, 0, 0, tzinfo=UTC)

    for wm in (wm1, wm2):
        if wm == wm2:
            current_records[:] = [dm_r2]
        export_to_csv(
            sdk_inc, study_key, csv_file, as_sdtm=True, sdtm_domain="DM", modified_since=wm
        )
        export_to_parquet(
            sdk_inc, study_key, pq_file, as_sdtm=True, sdtm_domain="DM", modified_since=wm
        )
        export_to_excel(
            sdk_inc, study_key, xlsx_file, as_sdtm=True, sdtm_domain="DM", modified_since=wm
        )
        export_to_json(
            sdk_inc, study_key, json_file, as_sdtm=True, sdtm_domain="DM", modified_since=wm
        )
        export_to_sql(
            sdk_inc, study_key, "dm_tbl", sql_uri, as_sdtm=True, sdtm_domain="DM", modified_since=wm
        )
        export_to_duckdb(
            sdk_inc,
            study_key,
            ddb_file,
            "dm_tbl",
            as_sdtm=True,
            sdtm_domain="DM",
            modified_since=wm,
        )
        export_to_hive_parquet(
            sdk_inc, study_key, hive_dir, as_sdtm=True, sdtm_domain="DM", modified_since=wm
        )
        export_to_dataset_json(
            sdk_inc, study_key, dj_file, as_sdtm=True, sdtm_domain="DM", modified_since=wm
        )

    assert len(pd.read_csv(csv_file)) == 1
    assert pd.read_csv(csv_file).iloc[0]["SEX"] == "F"
    assert int(pd.read_csv(csv_file).iloc[0]["RECORD_ID"]) == 102

    assert len(pd.read_parquet(pq_file)) == 1
    assert pd.read_parquet(pq_file).iloc[0]["SEX"] == "F"

    df_xl = pd.read_excel(xlsx_file)
    assert len(df_xl) == 1
    assert df_xl.iloc[0]["SEX"] == "F"

    js_rows = json.loads(json_file.read_text(encoding="utf-8"))
    assert len(js_rows) == 1
    assert js_rows[0]["SEX"] == "F"

    eng = sa.create_engine(sql_uri)
    with eng.connect() as conn:
        df_sql = pd.read_sql_table("dm_tbl", conn)
    eng.dispose()
    assert len(df_sql) == 1
    assert df_sql.iloc[0]["SEX"] == "F"

    with duckdb.connect(str(ddb_file), read_only=True) as dconn:
        df_ddb = dconn.execute('SELECT * FROM "dm_tbl"').df()
    assert len(df_ddb) == 1
    assert df_ddb.iloc[0]["SEX"] == "F"

    hive_dfs = [pd.read_parquet(p) for p in sorted(hive_dir.rglob("*.parquet"))]
    df_hive = pd.concat(hive_dfs, ignore_index=True)
    assert len(df_hive) == 1
    assert df_hive.iloc[0]["SEX"] == "F"

    dj_payload = json.loads(dj_file.read_text(encoding="utf-8"))
    assert validate_dataset_json(dj_payload) == []
    assert dj_payload["clinicalData"]["itemGroupData"]["IG.DM"]["records"] == 1

    # Plugin sinks (MongoDB, Neo4j, Snowflake) key DM on (STUDYID, DOMAIN, USUBJID) even with RECORD_ID
    docs_store: dict[str, dict[str, Any]] = {}
    pymongo_mod = MagicMock()
    pymongo_mod.UpdateOne.side_effect = lambda flt, upd, upsert=False: (flt, upd["$set"], upsert)
    collection = MagicMock()
    collection.bulk_write.side_effect = lambda ops, ordered=False: [
        docs_store.__setitem__(flt["_id"], payload) for flt, payload, _ in ops
    ]
    db_obj = MagicMock()
    db_obj.__getitem__.return_value = collection
    client = MagicMock()
    client.__getitem__.return_value = db_obj
    pymongo_mod.MongoClient.return_value = client
    monkeypatch.setattr(doc_mod, "_require_optional_dep", lambda *_: pymongo_mod)

    with MongoDbExportSink(
        MongoDbSinkConfig(
            study_key=study_key,
            uri="mongodb://localhost:27017",
            database="db",
            collection="dm",
            as_sdtm=True,
            sdtm_domain="DM",
            modified_since=wm2,
        )
    ) as m_sink:
        m_sink.write_batch([dm_r1, dm_r2], batch_id="b1")
    assert list(docs_store.keys()) == [f"{study_key}/DM/SUBJ-01"]
    assert docs_store[f"{study_key}/DM/SUBJ-01"]["SEX"] == "F"
    assert docs_store[f"{study_key}/DM/SUBJ-01"]["RECORD_ID"] == 102


def test_odm_rejects_sdtm_and_naming_before_phi_and_applies_enrichment_preserving_brthdat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC 6: export_to_odm & CLI reject SDTM/naming before PHI masking and apply EnrichmentPipeline."""
    phi_called = False

    def _boom_phi(*_a: Any, **_k: Any) -> Any:
        nonlocal phi_called
        phi_called = True
        raise AssertionError("PHI redaction should not execute before ODM option validation")

    monkeypatch.setattr(cdisc_builders, "build_study_sensitivity_registry", _boom_phi)

    sdk = _make_mock_sdk([])
    for bad_kwargs in (
        {"as_sdtm": True},
        {"sdtm_domain": "DM"},
        {"naming_convention": "sdtm"},
        {"preserve_record_id": True},
        {"config": SinkConfig(study_key="S_ODM", as_sdtm=True)},
        {"config": SinkConfig(study_key="S_ODM", naming_convention="camelCase")},
    ):
        with pytest.raises(ExportConfigurationError, match="CDISC ODM 1.3.2"):
            export_to_odm(sdk, "S_ODM", tmp_path / "bad.odm.xml", **bad_kwargs)
    assert phi_called is False

    for cli_flag in (
        ["--as-sdtm"],
        ["--sdtm-domain", "DM"],
        ["--domain", "DM"],
        ["--naming-convention", "snake_case"],
        ["--preserve-record-id"],
    ):
        with pytest.raises(SystemExit) as exc_info:
            cli_app(["export", "odm", "S_ODM", str(tmp_path / "cli_bad.odm.xml"), *cli_flag])
        assert exc_info.value.code != 0
        captured = capsys.readouterr()
        err_out = (captured.err + captured.out).lower()
        assert "odm" in err_out or "unrecognized arguments" in err_out
    assert phi_called is False

    monkeypatch.undo()

    # EnrichmentPipeline runs on ODM while preserving ***MASKED*** on BRTHDAT
    study_cfg = StudyConfiguration(
        study_key="S_ODM_ENRICH",
        mappings=[
            MappingRule(
                domain="DM",
                source_form_key="DM",
                source_variable_name="raw_temp_c",
                target_field="TEMP_F",
                business_logic="value * 1.8 + 32",
            )
        ],
    )
    monkeypatch.setattr(
        enrichment_mod,
        "resolve_study_configuration",
        lambda sk: study_cfg if sk == "S_ODM_ENRICH" else None,
    )
    forms = [Form(form_id=10, form_key="DM", form_name="Demographics", form_type="Enrollment")]
    variables = [
        Variable(
            variable_id=1,
            variable_name="BRTHDAT",
            label="Birth Date",
            variable_type="date",
            form_id=10,
            form_key="DM",
        ),
        Variable(
            variable_id=2,
            variable_name="raw_temp_c",
            label="Raw Temp C",
            variable_type="float",
            form_id=10,
            form_key="DM",
        ),
    ]
    rec = Record(
        record_id=1,
        study_key="S_ODM_ENRICH",
        subject_key="SUBJ-1",
        form_id=10,
        form_key="DM",
        record_data={"BRTHDAT": "1980-06-15", "raw_temp_c": 37.0},
    )
    sdk_enrich = _make_mock_sdk([rec], forms=forms, variables=variables)
    odm_path = tmp_path / "enriched.odm.xml"
    export_to_odm(sdk_enrich, "S_ODM_ENRICH", odm_path)

    assert validate_odm_xml(odm_path) == []
    root = ET.fromstring(odm_path.read_text(encoding="utf-8"))  # noqa: S314
    ns = {"odm": ODM_NS}
    item_vals = {
        el.attrib["ItemOID"]: el.attrib.get("Value")
        for el in root.findall(".//odm:ClinicalData//odm:ItemData", ns)
    }
    assert item_vals["BRTHDAT"] == "***MASKED***"
    assert float(item_vals["TEMP_F"] or 0.0) == pytest.approx(98.6)

    # Meanwhile SDTM BRTHDTC continues to truncate to 4-digit birth year
    sdtm_df = transform_records_to_sdtm(
        [
            Record(
                record_id=2,
                study_key="S_ODM_ENRICH",
                subject_key="SUBJ-1",
                form_id=10,
                form_key="DM",
                record_data={"BRTHDAT": "1980-06-15"},
            )
        ],
        domain="DM",
        study_key="S_ODM_ENRICH",
        mask_phi=True,
    )
    assert sdtm_df.iloc[0]["BRTHDTC"] == "1980"


def test_dataset_json_enrichment_order_and_collision_disambiguation() -> None:
    """AC 7: build_dataset_json_envelope runs EnrichmentPipeline before discovery & disambiguates headers/labels/OIDs."""
    pipeline = EnrichmentPipeline(
        StudyConfiguration(
            study_key="S_DJ",
            mappings=[
                MappingRule(
                    domain="VS",
                    source_form_key="VS",
                    source_variable_name="raw_score",
                    target_field="DERIVED_SCORE",
                    business_logic="value + 10",
                )
            ],
        )
    )
    forms = [Form(form_id=20, form_key="VS", form_name="Vitals", form_type="Visit")]
    variables = [
        Variable(
            variable_id=1,
            variable_name="bp-val",
            label="Blood Pressure",
            variable_type="integer",
            form_id=20,
            form_key="VS",
        ),
        Variable(
            variable_id=2,
            variable_name="bp_val",
            label="Blood Pressure",
            variable_type="integer",
            form_id=20,
            form_key="VS",
        ),
        Variable(
            variable_id=3,
            variable_name="record_id",
            label="Record Identifier",
            variable_type="integer",
            form_id=20,
            form_key="VS",
        ),
    ]
    rec = Record(
        record_id=501,
        study_key="S_DJ",
        subject_key="SUBJ-1",
        form_id=20,
        form_key="VS",
        record_data={"raw_score": 5, "bp-val": 120, "bp_val": 80, "record_id": 999},
    )

    # 1. snake_case naming_convention disambiguates colliding headers & metadata collisions
    env_snake = build_dataset_json_envelope(
        "S_DJ",
        records=[rec],
        forms=forms,
        variables=variables,
        naming_convention="snake_case",
        enrichment_pipeline=pipeline,
    )
    assert validate_dataset_json(to_dataset_json(env_snake)) == []
    ig_snake = env_snake.clinical_data.item_group_data["IG.VS"]
    snake_names = [it.name for it in ig_snake.items]
    snake_oids = [it.oid for it in ig_snake.items]
    assert len(snake_names) == len(set(snake_names))
    assert len(snake_oids) == len(set(snake_oids))
    assert "derived_score" in snake_names
    assert "var_record_id" in snake_names
    assert "bp_val" in snake_names
    assert "bp_val_2" in snake_names
    row_map = dict(zip(snake_names, ig_snake.item_data[0], strict=True))
    assert row_map["derived_score"] == 15
    assert row_map["record_id"] == 501
    assert row_map["var_record_id"] == 999
    assert {row_map["bp_val"], row_map["bp_val_2"]} == {120, 80}

    # 2. use_labels_as_columns=True disambiguates duplicate labels & punctured OIDs
    env_labels = build_dataset_json_envelope(
        "S_DJ",
        records=[rec],
        forms=forms,
        variables=variables,
        use_labels_as_columns=True,
        enrichment_pipeline=pipeline,
    )
    assert validate_dataset_json(to_dataset_json(env_labels)) == []
    ig_lbl = env_labels.clinical_data.item_group_data["IG.VS"]
    lbl_names = [it.name for it in ig_lbl.items]
    lbl_oids = [it.oid for it in ig_lbl.items]
    assert len(lbl_names) == len(set(lbl_names))
    assert len(lbl_oids) == len(set(lbl_oids))
    assert "Blood Pressure" in lbl_names
    assert "Blood Pressure_2" in lbl_names
    assert "DERIVED_SCORE" in lbl_names


def test_nested_formatting_collision_preservation_and_snowflake_uppercase_ddl_dedup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 8: Shared nested Variable formatting, var_ collision prefixing, and Snowflake uppercase DDL dedup."""
    for conv, expected in (
        ("snake_case", "vitals_bp_flag"),
        ("camelCase", "vitalsBpFlag"),
        ("sdtm", "VITALS_BPFLAG"),
    ):
        assert format_nested_variable_name("vitals.bpFlag", conv) == expected

    # CentralizedMapper and _post_process_warehouse produce identical nested variable names
    for conv in ("snake_case", "camelCase", "sdtm"):
        cm = CentralizedMapper(
            mode="tabular",
            naming_convention=conv,
            post_processor=wh_mod._post_process_warehouse,
        )
        cm_row = cm.map_record(
            {
                "recordId": 1,
                "studyKey": "S_NEST",
                "subjectKey": "U1",
                "formKey": "VS",
                "recordData": {"vitals": {"bpFlag": "HIGH"}},
            },
            study_key="S_NEST",
        )
        wh_row = wh_mod._post_process_warehouse(
            {
                "record_id": 1,
                "study_key": "S_NEST",
                "subject_key": "U1",
                "form_key": "VS",
                (
                    "record_data"
                    if conv == "snake_case"
                    else ("recordData" if conv == "camelCase" else "RECORD_DATA")
                ): {"vitals": {"bpFlag": "HIGH"}},
            },
            naming_convention=conv,
        )
        expected_nested = format_nested_variable_name("vitals.bpFlag", conv)
        assert cm_row[expected_nested] == "HIGH"
        assert wh_row[expected_nested] == "HIGH"

    # Tabular export retains colliding Variables (var_ for metadata collisions, _2 for variable-to-variable)
    forms = [Form(form_id=20, form_key="VS", form_name="Vitals", form_type="Visit")]
    variables = [
        Variable(variable_id=1, variable_name="heartRate", label="HR 1", form_id=20, form_key="VS"),
        Variable(
            variable_id=2, variable_name="heart_rate", label="HR 2", form_id=20, form_key="VS"
        ),
        Variable(
            variable_id=3, variable_name="record_id", label="Rec ID", form_id=20, form_key="VS"
        ),
    ]
    rec = Record(
        record_id=7,
        study_key="S_COLLIDE",
        subject_key="U1",
        form_id=20,
        form_key="VS",
        record_data={"heartRate": 71, "heart_rate": 73, "record_id": 700},
    )
    sdk = _make_mock_sdk([rec], forms=forms, variables=variables)
    csv_out = tmp_path / "collide.csv"
    export_to_csv(sdk, "S_COLLIDE", csv_out, naming_convention="snake_case")
    df_col = pd.read_csv(csv_out)
    assert "var_record_id" in df_col.columns
    assert int(df_col.iloc[0]["record_id"]) == 7
    assert int(df_col.iloc[0]["var_record_id"]) == 700
    assert "heart_rate" in df_col.columns
    assert "heart_rate_2" in df_col.columns
    assert {int(df_col.iloc[0]["heart_rate"]), int(df_col.iloc[0]["heart_rate_2"])} == {71, 73}

    # Snowflake _ensure_table_schema deduplicates case-colliding columns in CREATE TABLE DDL
    sf_mod = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    conn = MagicMock()
    conn.cursor.return_value = cursor
    sf_mod.connect.return_value = conn
    monkeypatch.setattr(
        wh_mod,
        "_require_optional_dep",
        lambda pkg, _extra: (
            sf_mod if pkg == "snowflake.connector" else (pq if "parquet" in pkg else pa)
        ),
    )
    sf_cfg = SnowflakeSinkConfig(
        study_key="S_DDL",
        account="acct",
        user="usr",
        password="pwd",  # pragma: allowlist secret
        database="DB",
        schema="PUBLIC",
        warehouse="WH",
        stage="STG",
        table="DDL_TEST",
        local_staging_dir=str(tmp_path / "sf_ddl"),
    )
    with SnowflakeExportSink(sf_cfg) as sf_sink:
        dup_table = pa.table(
            {"bp": [120], "BP": ["120/80"], "study_key": ["S_DDL"], "record_id": [1]}
        )
        sf_sink._ensure_table_schema(
            cursor, '"DB"."PUBLIC"."DDL_TEST"', dup_table, dup_table.column_names
        )

    create_sql = [c.args[0] for c in cursor.execute.call_args_list if "CREATE TABLE" in c.args[0]][
        0
    ]
    assert create_sql.count('"BP"') == 1
    assert '"BP" VARCHAR' in create_sql


@pytest.mark.asyncio
async def test_uniform_naming_convention_across_hive_parquet_hierarchical_json_long_sql_and_mapper(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC 9: Partitioned Parquet, hierarchical JSON, long SQL, and RecordMapper/AsyncRecordMapper apply naming_convention."""
    forms = [Form(form_id=20, form_key="VS", form_name="Vitals", form_type="Visit")]
    variables = [
        Variable(
            variable_id=1,
            variable_name="sys_bp",
            label="Systolic BP",
            variable_type="integer",
            form_id=20,
            form_key="VS",
        )
    ]
    rec = Record(
        record_id=42,
        study_key="S_NAMING",
        subject_key="SUBJ-42",
        form_id=20,
        form_key="VS",
        visit_id=2,
        date_modified=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
        record_data={"sys_bp": 118},
    )
    sdk = _make_mock_sdk([rec], forms=forms, variables=variables)

    # 1. export_to_hive_parquet strips formatted partition keys under camelCase & sdtm
    for conv, forbidden_cols, expected_var in (
        ("camelCase", {"study_key", "form_key", "studyKey", "formKey"}, "sysBp"),
        ("sdtm", {"study_key", "form_key", "STUDYID", "FORM_KEY"}, "SYSBP"),
    ):
        h_dir = tmp_path / f"hive_{conv}"
        export_to_hive_parquet(sdk, "S_NAMING", h_dir, naming_convention=conv)
        pq_files = list(h_dir.rglob("*.parquet"))
        assert len(pq_files) == 1
        df_h = pd.read_parquet(pq_files[0])
        assert forbidden_cols.isdisjoint(set(df_h.columns))
        assert expected_var in df_h.columns

    # 2. export_to_json(..., hierarchical=True) formats hierarchy + rejects as_sdtm=True
    with pytest.raises(ExportConfigurationError, match="(?i)hierarchical"):
        export_to_json(sdk, "S_NAMING", tmp_path / "bad_hier.json", hierarchical=True, as_sdtm=True)

    with pytest.raises(SystemExit) as cli_exc:
        cli_app(
            [
                "export",
                "json",
                "S_NAMING",
                str(tmp_path / "cli_bad_hier.json"),
                "--hierarchical",
                "--as-sdtm",
            ]
        )
    assert cli_exc.value.code != 0
    cli_out = capsys.readouterr()
    assert "hierarchical" in (cli_out.err + cli_out.out).lower()

    hier_file = tmp_path / "hier_sdtm_conv.json"
    export_to_json(sdk, "S_NAMING", hier_file, hierarchical=True, naming_convention="sdtm")
    hier_data = json.loads(hier_file.read_text(encoding="utf-8"))
    assert hier_data[0]["USUBJID"] == "SUBJ-42"
    assert hier_data[0]["VISITS"][0]["VISITNUM"] == 2
    rec_node = hier_data[0]["VISITS"][0]["FORMS"][0]["RECORDS"][0]
    assert rec_node["RECORD_ID"] == 42
    assert rec_node["SYSBP"] == 118

    # 3. export_to_long_sql preserves default snake_case when naming_convention=None, and formats camelCase & sdtm
    default_sql = f"sqlite:///{tmp_path / 'long_default.sqlite'}"
    export_to_long_sql(sdk, "S_NAMING", "eav_def", default_sql)
    eng_def = sa.create_engine(default_sql)
    with eng_def.connect() as conn:
        df_def = pd.read_sql_table("eav_def", conn)
    eng_def.dispose()
    assert list(df_def.columns) == ["record_id", "form_id", "variable_name", "value", "timestamp"]

    camel_sql = f"sqlite:///{tmp_path / 'long_camel.sqlite'}"
    export_to_long_sql(sdk, "S_NAMING", "eav_camel", camel_sql, naming_convention="camelCase")
    eng_cam = sa.create_engine(camel_sql)
    with eng_cam.connect() as conn:
        df_cam = pd.read_sql_table("eav_camel", conn)
    eng_cam.dispose()
    assert list(df_cam.columns) == ["recordId", "formId", "variableName", "value", "timestamp"]
    assert df_cam.iloc[0]["variableName"] == "sysBp"

    sdtm_sql = f"sqlite:///{tmp_path / 'long_sdtm.sqlite'}"
    export_to_long_sql(sdk, "S_NAMING", "eav_sdtm", sdtm_sql, naming_convention="sdtm")
    eng_sdtm = sa.create_engine(sdtm_sql)
    with eng_sdtm.connect() as conn:
        df_sdtm = pd.read_sql_table("eav_sdtm", conn)
    eng_sdtm.dispose()
    assert list(df_sdtm.columns) == ["RECORD_ID", "FORM_ID", "VARIABLE_NAME", "VALUE", "TIMESTAMP"]
    assert df_sdtm.iloc[0]["VARIABLE_NAME"] == "SYSBP"

    # 4. RecordMapper & AsyncRecordMapper preserve default camelCase metadata when naming_convention=None
    #    and apply naming_convention across dataframe, iter_dataframes, build_hierarchy, sdtm_dataframe, sdtm_dataset_json
    rm = RecordMapper(sdk)
    df_default = rm.dataframe("S_NAMING", use_labels_as_columns=False)
    assert "recordId" in df_default.columns
    assert "subjectKey" in df_default.columns
    assert "sys_bp" in df_default.columns

    df_snake = rm.dataframe("S_NAMING", use_labels_as_columns=False, naming_convention="snake_case")
    assert "record_id" in df_snake.columns
    assert "subject_key" in df_snake.columns
    assert "recordId" not in df_snake.columns

    tree_default = rm.build_hierarchy("S_NAMING")
    assert "subject_key" in tree_default[0]
    assert "recordId" in tree_default[0]["visits"][0]["forms"][0]["records"][0]

    tree_camel = rm.build_hierarchy("S_NAMING", naming_convention="camelCase")
    assert "subjectKey" in tree_camel[0]
    assert "sysBp" in tree_camel[0]["visits"][0]["forms"][0]["records"][0]

    sdtm_camel_df = rm.sdtm_dataframe(
        "S_NAMING", "VS", naming_convention="camelCase", preserve_record_id=True
    )
    assert "studyid" in sdtm_camel_df.columns
    assert "vsseq" in sdtm_camel_df.columns
    assert "recordId" in sdtm_camel_df.columns
    assert "sysBp" in sdtm_camel_df.columns

    sdtm_camel_dj = rm.sdtm_dataset_json(
        "S_NAMING", "VS", naming_convention="camelCase", preserve_record_id=True
    )
    assert validate_dataset_json(to_dataset_json(sdtm_camel_dj)) == []
    dj_item_names = [it.name for it in sdtm_camel_dj.clinical_data.item_group_data["IG.VS"].items]
    assert "studyid" in dj_item_names
    assert "vsseq" in dj_item_names
    assert "recordId" in dj_item_names
    assert "sysBp" in dj_item_names

    # AsyncRecordMapper parity
    async_sdk = MagicMock()
    async_sdk.forms.list = AsyncMock(return_value=forms)
    async_sdk.variables.list = AsyncMock(return_value=variables)
    async_sdk.visits.list = AsyncMock(return_value=[])
    async_sdk.intervals.list = AsyncMock(return_value=[])
    async_sdk.sites.list = AsyncMock(return_value=[])
    async_sdk.records.list = AsyncMock(return_value=[rec])

    arm = AsyncRecordMapper(async_sdk)
    adf = await arm.dataframe("S_NAMING", use_labels_as_columns=False, naming_convention="sdtm")
    assert "RECORD_ID" in adf.columns
    assert "USUBJID" in adf.columns
    assert "VISITNUM" in adf.columns
    assert "SYSBP" in adf.columns

    atree = await arm.build_hierarchy("S_NAMING", naming_convention="camelCase")
    assert atree[0]["subjectKey"] == "SUBJ-42"

    adj = await arm.sdtm_dataset_json(
        "S_NAMING", "VS", naming_convention="snake_case", preserve_record_id=True
    )
    assert validate_dataset_json(to_dataset_json(adj)) == []
    adj_names = [it.name for it in adj.clinical_data.item_group_data["IG.VS"].items]
    assert "studyid" in adj_names
    assert "record_id" in adj_names
    assert "sys_bp" in adj_names
