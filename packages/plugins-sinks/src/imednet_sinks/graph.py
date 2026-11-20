# pylint: disable=duplicate-code
"""Neo4j graph export sink.

This module implements the **structure-preserving export path** for a Neo4j
property-graph destination.  Records fetched via
:class:`~imednet_workflows.data_extraction.DataExtractionWorkflow` are written
as graph nodes and relationships that mirror the clinical data hierarchy.

Graph shape
-----------
Nodes
~~~~~
* ``(:Study   {study_key})``
* ``(:Subject {subject_key, study_key})``
* ``(:Visit   {visit_id, subject_key, study_key})``
* ``(:Record  {record_id, form_id, visit_id, subject_key, study_key, **record_data})``

Relationships
~~~~~~~~~~~~~
* ``(:Study)-[:HAS_SUBJECT]->(:Subject)``
* ``(:Subject)-[:HAS_VISIT]->(:Visit)``
* ``(:Visit)-[:HAS_RECORD]->(:Record)``

Optional dependency
-------------------
Requires ``neo4j`` (install via ``pip install 'imednet[neo4j]'``).
The driver is imported lazily at connection time so that importing this
module never fails when ``neo4j`` is not installed.

Idempotency
-----------
When ``SinkConfig.idempotent`` is ``True`` (default) the sink uses
``MERGE`` on the node's primary key property so that re-running an export
for the same batch updates existing nodes rather than creating duplicates.

Usage
-----
.. code-block:: python

    import os
    from imednet import ImednetSDK
    from imednet.spi.sink_base import iter_batches
    from imednet_sinks import Neo4jExportSink, Neo4jSinkConfig
    from imednet_workflows.data_extraction import DataExtractionWorkflow

    sdk = ImednetSDK(api_key=os.environ["IMEDNET_API_KEY"])
    records = DataExtractionWorkflow(sdk).extract_records_by_criteria(
        study_key="MYSTUDY",
    )

    config = Neo4jSinkConfig(
        study_key="MYSTUDY",
        uri=os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        auth=("neo4j", os.environ["NEO4J_PASSWORD"]),
        batch_size=200,
    )
    with Neo4jExportSink(config=config) as sink:
        for i, batch in enumerate(iter_batches(records, config.batch_size)):
            sink.write_batch(batch, batch_id=f"MYSTUDY/all/{i}")
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from imednet.spi.sinks import CentralizedMapper

from imednet.errors import ExportConfigurationError
from imednet.sdk import ImednetSDK
from imednet.spi.sink_base import (
    ExportSink,
    SinkConfig,
    _redact_uri,
    _require_optional_dep,
    apply_quality_gate,
    extract_record_id,
    run_streaming_export,
)

logger = logging.getLogger(__name__)


@dataclass
class Neo4jSinkConfig(SinkConfig):
    """Extended :class:`~imednet.spi.sink_base.SinkConfig` for Neo4j.

    Parameters
    ----------
    database:
        Target Neo4j database name (default ``"neo4j"``).
    """

    uri: str = ""
    auth: tuple[str, str] = ("", "")
    database: str = "neo4j"
    modified_since: datetime | str | None = None
    ledger_path: str | Path | None = None

    def __post_init__(self) -> None:
        """Validate Neo4j config properties after initialization."""
        super().__post_init__()  # type: ignore[no-untyped-call]
        if not self.uri or not isinstance(self.uri, str) or not self.uri.strip():
            raise ValueError("uri must be a non-empty string")
        if not self.auth or not isinstance(self.auth, tuple) or len(self.auth) != 2:
            raise ValueError("auth must be a tuple of (username, password)")


# ---------------------------------------------------------------------------
# Cypher templates
# ---------------------------------------------------------------------------

_MERGE_RECORD_CYPHER = """\
UNWIND $rows AS row
MERGE (s:Study   {study_key:   row.study_key})
MERGE (su:Subject {subject_key: row.subject_key, study_key: row.study_key})
MERGE (r:Record   {record_id:  row.record_id,   study_key: row.study_key})
SET r += row
MERGE (s)-[:HAS_SUBJECT]->(su)
FOREACH (_ IN CASE WHEN row.visit_id IS NOT NULL THEN [1] ELSE [] END |
    MERGE (v:Visit {visit_id: row.visit_id, study_key: row.study_key})
    MERGE (su)-[:HAS_VISIT]->(v)
    MERGE (v)-[:HAS_RECORD]->(r)
)
"""

_CREATE_RECORD_CYPHER = """\
UNWIND $rows AS row
CREATE (r:Record)
SET r = row
WITH r, row
WHERE row.visit_id IS NOT NULL
MATCH (v:Visit {visit_id: row.visit_id, study_key: row.study_key})
MERGE (v)-[:HAS_RECORD]->(r)
"""

_DELETE_RECORD_CYPHER = """\
UNWIND $record_ids AS rid
MATCH (r:Record {record_id: rid, study_key: $study_key})
DETACH DELETE r
"""


def _build_convention_cypher(
    study_prop: str,
    subject_prop: str,
    record_prop: str,
    visit_prop: str,
) -> tuple[str, str]:
    """Build MERGE and CREATE Cypher statements aligned to a naming convention."""
    merge_cypher = (
        "UNWIND $rows AS row\n"
        f"MERGE (s:Study   {{{study_prop}: row.{study_prop}}})\n"
        f"MERGE (su:Subject {{{subject_prop}: row.{subject_prop}, {study_prop}: row.{study_prop}}})\n"
        f"MERGE (r:Record   {{{record_prop}: row.{record_prop}, {study_prop}: row.{study_prop}}})\n"
        "SET r += row\n"
        "MERGE (s)-[:HAS_SUBJECT]->(su)\n"
        f"FOREACH (_ IN CASE WHEN row.{visit_prop} IS NOT NULL THEN [1] ELSE [] END |\n"
        f"    MERGE (v:Visit {{{visit_prop}: row.{visit_prop}, {study_prop}: row.{study_prop}}})\n"
        "    MERGE (su)-[:HAS_VISIT]->(v)\n"
        "    MERGE (v)-[:HAS_RECORD]->(r)\n"
        ")\n"
    )
    create_cypher = (
        "UNWIND $rows AS row\n"
        "CREATE (r:Record)\n"
        "SET r = row\n"
        "WITH r, row\n"
        f"WHERE row.{visit_prop} IS NOT NULL\n"
        f"MATCH (v:Visit {{{visit_prop}: row.{visit_prop}, {study_prop}: row.{study_prop}}})\n"
        "MERGE (v)-[:HAS_RECORD]->(r)\n"
    )
    return merge_cypher, create_cypher


def _build_sdtm_cypher(seq_prop: str | None, has_visit: bool) -> tuple[str, str]:
    """Build MERGE and CREATE Cypher statements for SDTM domain rows."""
    record_key_props = (
        f"STUDYID: row.STUDYID, DOMAIN: row.DOMAIN, USUBJID: row.USUBJID, {seq_prop}: row.{seq_prop}"
        if seq_prop
        else "STUDYID: row.STUDYID, DOMAIN: row.DOMAIN, USUBJID: row.USUBJID"
    )
    if has_visit:
        visit_block = (
            "FOREACH (_ IN CASE WHEN row.VISITNUM IS NOT NULL THEN [1] ELSE [] END |\n"
            "    MERGE (v:Visit {VISITNUM: row.VISITNUM, STUDYID: row.STUDYID})\n"
            "    MERGE (su)-[:HAS_VISIT]->(v)\n"
            "    MERGE (v)-[:HAS_RECORD]->(r)\n"
            ")\n"
            "FOREACH (_ IN CASE WHEN row.VISITNUM IS NULL THEN [1] ELSE [] END |\n"
            "    MERGE (su)-[:HAS_RECORD]->(r)\n"
            ")\n"
        )
    else:
        visit_block = "MERGE (su)-[:HAS_RECORD]->(r)\n"

    merge_cypher = (
        "UNWIND $rows AS row\n"
        "MERGE (s:Study   {STUDYID: row.STUDYID})\n"
        "MERGE (su:Subject {USUBJID: row.USUBJID, STUDYID: row.STUDYID})\n"
        f"MERGE (r:Record   {{{record_key_props}}})\n"
        "SET r += row\n"
        "MERGE (s)-[:HAS_SUBJECT]->(su)\n"
        f"{visit_block}"
    )
    create_cypher = (
        "UNWIND $rows AS row\n"
        "MERGE (s:Study   {STUDYID: row.STUDYID})\n"
        "MERGE (su:Subject {USUBJID: row.USUBJID, STUDYID: row.STUDYID})\n"
        "CREATE (r:Record)\n"
        "SET r = row\n"
        "MERGE (s)-[:HAS_SUBJECT]->(su)\n"
        "MERGE (su)-[:HAS_RECORD]->(r)\n"
    )
    return merge_cypher, create_cypher


def _post_process_graph(row: dict[str, Any]) -> dict[str, Any]:
    import json

    for rd_key in ("record_data", "recordData", "RECORD_DATA"):
        if rd_key in row:
            row[rd_key] = json.dumps(row.get(rd_key, {}))
            break
    else:
        row["record_data"] = json.dumps({})

    for kw_key in ("keywords", "KEYWORDS"):
        keywords = row.get(kw_key)
        if isinstance(keywords, list):
            row[kw_key] = json.dumps(
                [
                    k.model_dump(mode="json", by_alias=True) if hasattr(k, "model_dump") else k
                    for k in keywords
                ]
            )
    return row


def _post_process_graph_sdtm(row: dict[str, Any]) -> dict[str, Any]:
    """Serialize any non-primitive SDTM row values for Neo4j property compatibility."""
    import json

    for key, val in list(row.items()):
        if isinstance(val, (dict, list, tuple)):
            row[key] = json.dumps(val, default=str)
    return row


def _record_to_row(
    record: Any,
    study_key: str,
    mapper: CentralizedMapper | None = None,
    *,
    mask_phi: bool = True,
) -> dict[str, Any]:
    """Convert a typed ``Record`` model to a flat Cypher parameter dict."""
    if mapper is None:
        from imednet.spi.sinks import CentralizedMapper

        mapper = CentralizedMapper(
            mode="document",
            post_processor=_post_process_graph,
            mask_phi=mask_phi,
        )
    return mapper.map_record(record, study_key=study_key)


class Neo4jExportSink(ExportSink):
    """Export iMednet records as Neo4j graph nodes and relationships.

    Parameters
    ----------
    config:
        Mandatory :class:`Neo4jSinkConfig`.

    Raises:
        ~imednet.errors.ExportConfigurationError: When the driver cannot connect to the database.
        ImportError: When the ``neo4j`` package is not installed.
    """

    def __init__(self, config: Neo4jSinkConfig) -> None:
        """Initialize the Neo4j export sink.

        Args:
            config: Mandatory Neo4j-specific sink configuration.
        """
        super().__init__(config)
        self.config: Neo4jSinkConfig = config
        self._uri = self.config.uri
        self._auth = self.config.auth
        self._study_key = self.config.study_key
        self._seq_counters: dict[str, int] = {}
        self._driver: Any = None
        self._connect()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _connect(self) -> None:
        """Establish connection to Neo4j and verify connectivity."""
        neo4j_mod = _require_optional_dep("neo4j", "neo4j")
        redacted = _redact_uri(self._uri)
        logger.debug("Connecting to Neo4j at %s", redacted)
        try:
            self._driver = neo4j_mod.GraphDatabase.driver(self._uri, auth=self._auth)
            self._driver.verify_connectivity()
        except Exception as exc:
            raise ExportConfigurationError(f"Cannot connect to Neo4j at {redacted}: {exc}") from exc

    # ------------------------------------------------------------------
    # ExportSink interface
    # ------------------------------------------------------------------

    def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Write *records* to Neo4j using MERGE (idempotent) or CREATE."""
        from imednet.spi.sinks import build_sink_mapper

        post_proc = _post_process_graph_sdtm if self.config.as_sdtm else _post_process_graph
        mapper = build_sink_mapper(
            self.config,
            mode="document",
            post_processor=post_proc,
            seq_counters=self._seq_counters,
        )
        custom_pipeline = self.config.extra.get("enrichment_pipeline")
        if custom_pipeline is not None and hasattr(custom_pipeline, "process"):
            mapper.register_pipeline(self._study_key, custom_pipeline)
        rows = [_record_to_row(r, self._study_key, mapper=mapper) for r in records]
        if not rows:
            return 0

        if self.config.as_sdtm:
            sample_row = rows[0]
            seq_prop = next(
                (
                    k
                    for k in sample_row
                    if k.endswith("SEQ") and k not in ("STUDYID", "DOMAIN", "USUBJID")
                ),
                None,
            )
            has_visit = any("VISITNUM" in r for r in rows)
            merge_cypher, create_cypher = _build_sdtm_cypher(seq_prop, has_visit)
        elif self.config.naming_convention == "camelCase":
            merge_cypher, create_cypher = _build_convention_cypher(
                "studyKey", "subjectKey", "recordId", "visitId"
            )
        elif self.config.naming_convention == "sdtm":
            merge_cypher, create_cypher = _build_convention_cypher(
                "STUDYID", "USUBJID", "RECORD_ID", "VISIT_ID"
            )
        else:
            merge_cypher, create_cypher = _MERGE_RECORD_CYPHER, _CREATE_RECORD_CYPHER

        cypher = merge_cypher if self.config.idempotent else create_cypher
        cfg = self.config if isinstance(self.config, Neo4jSinkConfig) else Neo4jSinkConfig()

        def execute_export() -> int:
            """Execute Cypher transaction to write or merge records in Neo4j."""
            with self._driver.session(database=cfg.database) as session:
                session.run(cypher, rows=rows)
            logger.debug("Wrote batch %s (%d records)", batch_id, len(rows))
            return len(rows)

        return self._execute_with_retry("export_graph", batch_id, execute_export)

    def flush(self) -> None:
        """No-op: Neo4j writes are committed per transaction."""

    def reconcile_deleted(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Delete newly voided Record nodes from Neo4j when running idempotently."""
        if not self.config.idempotent or not records:
            return 0
        record_ids = [rid for r in records if (rid := extract_record_id(r)) is not None]
        if not record_ids:
            return 0

        cfg = (
            self.config
            if isinstance(self.config, Neo4jSinkConfig)
            else Neo4jSinkConfig(study_key=self._study_key, uri=self._uri, auth=self._auth)
        )

        def execute_reconcile() -> int:
            with self._driver.session(database=cfg.database) as session:
                session.run(
                    _DELETE_RECORD_CYPHER,
                    record_ids=record_ids,
                    study_key=self._study_key,
                )
            logger.debug("Reconciled %d voided records in batch %s", len(record_ids), batch_id)
            return len(record_ids)

        return self._execute_with_retry("reconcile_graph_deleted", batch_id, execute_reconcile)

    def close(self) -> None:
        """Close the underlying Neo4j driver connection."""
        if self._driver is not None:
            try:
                self._driver.close()
            finally:
                self._driver = None


def export_to_neo4j(
    sdk: ImednetSDK,
    study_key: str,
    uri: str = "",
    auth: tuple[str, str] = ("", ""),
    *,
    config: Neo4jSinkConfig | None = None,
    mask_phi: bool | None = None,
    **kwargs: Any,
) -> int:
    """Export study records to Neo4j using :class:`Neo4jExportSink`."""
    if config is None:
        kwargs.setdefault("mask_phi", True if mask_phi is None else mask_phi)
        config = Neo4jSinkConfig(
            study_key=study_key,
            uri=uri,
            auth=auth,
            **kwargs,
        )
    elif mask_phi is not None:
        config = replace(config, mask_phi=mask_phi)
    resolved_cfg = config
    return run_streaming_export(
        sdk,
        study_key,
        lambda: Neo4jExportSink(config=resolved_cfg),
        resolved_cfg,
        stream_name=f"neo4j_{resolved_cfg.database}",
        quality_gate_fn=apply_quality_gate,
    )


__all__ = ["Neo4jExportSink", "Neo4jSinkConfig", "export_to_neo4j"]
