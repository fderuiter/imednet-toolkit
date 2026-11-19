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
    from imednet_sinks import Neo4jExportSink, Neo4jSinkConfig
    from imednet_workflows.data_extraction import DataExtractionWorkflow

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
        for i, batch in enumerate(batched(records, config.batch_size)):
            sink.write_batch(batch, batch_id=f"MYSTUDY/all/{i}")
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
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
    extract_record_id,
    run_streaming_export,
)

logger = logging.getLogger(__name__)


@dataclass
class Neo4jSinkConfig(SinkConfig):
    """Extended :class:`~imednet.integrations.sink_base.SinkConfig` for Neo4j.

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


def _post_process_graph(row: dict[str, Any]) -> dict[str, Any]:
    import json

    row["record_data"] = json.dumps(row.get("record_data", {}))
    keywords = row.get("keywords")
    if isinstance(keywords, list):
        row["keywords"] = json.dumps(
            [
                k.model_dump(mode="json", by_alias=True) if hasattr(k, "model_dump") else k
                for k in keywords
            ]
        )
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
        from imednet.spi.sinks import CentralizedMapper

        mapper = CentralizedMapper(
            mode="document",
            post_processor=_post_process_graph,
            mask_phi=self.config.mask_phi,
        )
        rows = [_record_to_row(r, self._study_key, mapper=mapper) for r in records]
        if not rows:
            return 0

        cypher = _MERGE_RECORD_CYPHER if self.config.idempotent else _CREATE_RECORD_CYPHER
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
    mask_phi: bool = True,
    **kwargs: Any,
) -> int:
    """Export study records to Neo4j using :class:`Neo4jExportSink`."""
    if config is None:
        kwargs.setdefault("mask_phi", mask_phi)
        config = Neo4jSinkConfig(
            study_key=study_key,
            uri=uri,
            auth=auth,
            **kwargs,
        )
    return run_streaming_export(
        sdk,
        study_key,
        lambda: Neo4jExportSink(config=config),
        config,
        stream_name=f"neo4j_{config.database}",
    )


__all__ = ["Neo4jExportSink", "Neo4jSinkConfig", "export_to_neo4j"]
