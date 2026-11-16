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

    from imednet.integrations.graph import Neo4jExportSink, Neo4jSinkConfig
    from imednet_workflows.data_extraction import DataExtractionWorkflow

    records = DataExtractionWorkflow(sdk).extract_records_by_criteria(
        study_key="MYSTUDY",
    )

    config = Neo4jSinkConfig(batch_size=200)
    with Neo4jExportSink(
        uri="bolt://localhost:7687",
        auth=("neo4j", "password"),
        config=config,
    ) as sink:
        for i, batch in enumerate(batched(records, config.batch_size)):
            sink.write_batch(batch, batch_id=f"MYSTUDY/all/{i}")
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from imednet.spi.sinks import CentralizedMapper

from imednet.errors import ExportConfigurationError
from imednet.sdk import ImednetSDK
from imednet.spi.sinks import (
    ExportSink,
    SinkConfig,
    iter_batches,
)
from imednet.spi.sinks import (
    redact_uri as _redact_uri,
)
from imednet.spi.sinks import (
    require_optional_dep as _require_optional_dep,
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
MERGE (v:Visit    {visit_id:   row.visit_id,    study_key: row.study_key})
MERGE (r:Record   {record_id:  row.record_id,   study_key: row.study_key})
SET r += row
MERGE (s)-[:HAS_SUBJECT]->(su)
MERGE (su)-[:HAS_VISIT]->(v)
MERGE (v)-[:HAS_RECORD]->(r)
"""

_CREATE_RECORD_CYPHER = """\
UNWIND $rows AS row
CREATE (r:Record)
SET r = row
WITH r, row
MATCH (v:Visit {visit_id: row.visit_id, study_key: row.study_key})
MERGE (v)-[:HAS_RECORD]->(r)
"""


def _post_process_graph(row: dict[str, Any]) -> dict[str, Any]:
    import json

    row["record_data"] = json.dumps(row.get("record_data", {}))
    return row


def _record_to_row(
    record: Any, study_key: str, mapper: CentralizedMapper | None = None
) -> dict[str, Any]:
    """Convert a typed ``Record`` model to a flat Cypher parameter dict."""
    if mapper is None:
        from imednet.spi.sinks import CentralizedMapper

        mapper = CentralizedMapper(mode="document", post_processor=_post_process_graph)
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

        mapper = CentralizedMapper(mode="document", post_processor=_post_process_graph)
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
) -> int:
    """Export study records to Neo4j using :class:`Neo4jExportSink`."""
    if config is None:
        config = Neo4jSinkConfig(
            study_key=study_key,
            uri=uri,
            auth=auth,
        )
    records = sdk.records.list(study_key=study_key, record_data_filter=None)
    total_written = 0
    with Neo4jExportSink(config=config) as sink:
        for index, batch in enumerate(iter_batches(list(records), config.batch_size)):
            total_written += sink.write_batch(batch, batch_id=f"{study_key}/records/{index}")
    return total_written


__all__ = ["Neo4jExportSink", "Neo4jSinkConfig", "export_to_neo4j"]
