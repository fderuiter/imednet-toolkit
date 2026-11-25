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
* ``(:Visit)-[:HAS_RECORD]->(:Record)`` (scheduled records with ``visit_id > 0``)
* ``(:Subject)-[:HAS_RECORD]->(:Record)`` (unscheduled records with ``visit_id`` omitted or ``<= 0``)

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

    sdk = ImednetSDK(
        api_key=os.environ["IMEDNET_API_KEY"],
        security_key=os.environ["IMEDNET_SECURITY_KEY"],
    )
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

import json
import logging
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any
from unittest.mock import NonCallableMock

from imednet.errors import ExportConfigurationError
from imednet.sdk import ImednetSDK
from imednet.spi.sink_base import (
    _redact_uri,
    _require_optional_dep,
)
from imednet.spi.sinks import (
    CentralizedMapper,
    ExportSink,
    SinkConfig,
    apply_quality_gate,
    bind_sdtm_stream_metadata,
    build_sink_mapper,
    commit_sdtm_lineage,
    deduplicate_sdtm_rows,
    extract_record_id,
    normalize_sink_value,
    rehydrate_sdtm_lineage,
    resolve_sdtm_domain,
    resolve_voided_sdtm_keys,
    run_streaming_export,
    should_preserve_sdtm_record_id,
)

_CYPHER_PROP_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

logger = logging.getLogger(__name__)

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_$]+$")
_CYPHER_PROP_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SDTM_DOMAIN_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_SDTM_SEQ_PROP_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*SEQ$")


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
    MERGE (v:Visit {visit_id: row.visit_id, subject_key: row.subject_key, study_key: row.study_key})
    MERGE (su)-[:HAS_VISIT]->(v)
    MERGE (v)-[:HAS_RECORD]->(r)
)
FOREACH (_ IN CASE WHEN row.visit_id IS NULL THEN [1] ELSE [] END |
    MERGE (su)-[:HAS_RECORD]->(r)
)
"""

_CREATE_RECORD_CYPHER = """\
UNWIND $rows AS row
MERGE (s:Study   {study_key:   row.study_key})
MERGE (su:Subject {subject_key: row.subject_key, study_key: row.study_key})
CREATE (r:Record)
SET r = row
MERGE (s)-[:HAS_SUBJECT]->(su)
FOREACH (_ IN CASE WHEN row.visit_id IS NOT NULL THEN [1] ELSE [] END |
    MERGE (v:Visit {visit_id: row.visit_id, subject_key: row.subject_key, study_key: row.study_key})
    MERGE (su)-[:HAS_VISIT]->(v)
    MERGE (v)-[:HAS_RECORD]->(r)
)
FOREACH (_ IN CASE WHEN row.visit_id IS NULL THEN [1] ELSE [] END |
    MERGE (su)-[:HAS_RECORD]->(r)
)
"""

_DELETE_RECORD_CYPHER = """\
UNWIND $record_ids AS rid
MATCH (r:Record {record_id: rid, study_key: $study_key})
DETACH DELETE r
"""


def _graph_json_default(value: Any) -> Any:
    """Encode ``date``/``datetime`` as ISO-8601 strings and ``Decimal`` as exact strings."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _normalize_visit_id(visit_id: Any) -> Any:
    """Normalize positive visit identifiers and map non-positive markers (``<= 0``) to ``None``."""
    if visit_id is None or isinstance(visit_id, NonCallableMock):
        return visit_id
    if isinstance(visit_id, bool):
        return None
    if isinstance(visit_id, int):
        return visit_id if visit_id > 0 else None
    if isinstance(visit_id, float):
        if not math.isfinite(visit_id) or visit_id <= 0:
            return None
        return int(visit_id) if visit_id.is_integer() else visit_id
    if isinstance(visit_id, Decimal):
        if not visit_id.is_finite() or visit_id <= 0:
            return None
        integral = visit_id.to_integral_value()
        return int(integral) if visit_id == integral else float(visit_id)
    if isinstance(visit_id, str):
        stripped = visit_id.strip()
        if not stripped:
            return None
        if stripped.lstrip("+-").isdigit():
            parsed_int = int(stripped)
            return parsed_int if parsed_int > 0 else None
        try:
            parsed_float = float(stripped)
        except ValueError:
            return stripped
        if not math.isfinite(parsed_float) or parsed_float <= 0:
            return None
        return int(parsed_float) if parsed_float.is_integer() else parsed_float
    return visit_id


def _build_convention_cypher(
    study_prop: str,
    subject_prop: str,
    record_prop: str,
    visit_prop: str,
) -> tuple[str, str]:
    """Build MERGE and CREATE Cypher statements aligned to a naming convention."""
    for prop_name, prop_val in (
        ("study_prop", study_prop),
        ("subject_prop", subject_prop),
        ("record_prop", record_prop),
        ("visit_prop", visit_prop),
    ):
        if not isinstance(prop_val, str) or not _CYPHER_PROP_PATTERN.fullmatch(prop_val):
            raise ExportConfigurationError(
                f"Neo4j Cypher property {prop_name} contains invalid characters: {prop_val!r}"
            )
    merge_cypher = (
        "UNWIND $rows AS row\n"
        f"MERGE (s:Study   {{{study_prop}: row.{study_prop}}})\n"
        f"MERGE (su:Subject {{{subject_prop}: row.{subject_prop}, {study_prop}: row.{study_prop}}})\n"
        f"MERGE (r:Record   {{{record_prop}: row.{record_prop}, {study_prop}: row.{study_prop}}})\n"
        "SET r += row\n"
        "MERGE (s)-[:HAS_SUBJECT]->(su)\n"
        f"FOREACH (_ IN CASE WHEN row.{visit_prop} IS NOT NULL THEN [1] ELSE [] END |\n"
        f"    MERGE (v:Visit {{{visit_prop}: row.{visit_prop}, {subject_prop}: row.{subject_prop}, {study_prop}: row.{study_prop}}})\n"
        "    MERGE (su)-[:HAS_VISIT]->(v)\n"
        "    MERGE (v)-[:HAS_RECORD]->(r)\n"
        ")\n"
        f"FOREACH (_ IN CASE WHEN row.{visit_prop} IS NULL THEN [1] ELSE [] END |\n"
        "    MERGE (su)-[:HAS_RECORD]->(r)\n"
        ")\n"
    )
    create_cypher = (
        "UNWIND $rows AS row\n"
        f"MERGE (s:Study   {{{study_prop}: row.{study_prop}}})\n"
        f"MERGE (su:Subject {{{subject_prop}: row.{subject_prop}, {study_prop}: row.{study_prop}}})\n"
        "CREATE (r:Record)\n"
        "SET r = row\n"
        "MERGE (s)-[:HAS_SUBJECT]->(su)\n"
        f"FOREACH (_ IN CASE WHEN row.{visit_prop} IS NOT NULL THEN [1] ELSE [] END |\n"
        f"    MERGE (v:Visit {{{visit_prop}: row.{visit_prop}, {subject_prop}: row.{subject_prop}, {study_prop}: row.{study_prop}}})\n"
        "    MERGE (su)-[:HAS_VISIT]->(v)\n"
        "    MERGE (v)-[:HAS_RECORD]->(r)\n"
        ")\n"
        f"FOREACH (_ IN CASE WHEN row.{visit_prop} IS NULL THEN [1] ELSE [] END |\n"
        "    MERGE (su)-[:HAS_RECORD]->(r)\n"
        ")\n"
    )
    return merge_cypher, create_cypher


def _build_sdtm_cypher(
    seq_prop: str | None,
    has_visit: bool,
    *,
    has_record_id: bool = False,
) -> tuple[str, str]:
    """Build MERGE and CREATE Cypher statements for SDTM domain rows."""
    if seq_prop is not None and (
        not isinstance(seq_prop, str) or not _SDTM_SEQ_PROP_PATTERN.fullmatch(seq_prop)
    ):
        raise ExportConfigurationError(
            f"Neo4j SDTM sequence property contains invalid characters: {seq_prop!r}"
        )
    if has_record_id:
        record_key_props = "STUDYID: row.STUDYID, RECORD_ID: row.RECORD_ID"
    elif seq_prop:
        record_key_props = (
            f"STUDYID: row.STUDYID, DOMAIN: row.DOMAIN, USUBJID: row.USUBJID, "
            f"{seq_prop}: row.{seq_prop}"
        )
    else:
        record_key_props = "STUDYID: row.STUDYID, DOMAIN: row.DOMAIN, USUBJID: row.USUBJID"
    if has_visit:
        visit_block = (
            "FOREACH (_ IN CASE WHEN row.VISITNUM IS NOT NULL THEN [1] ELSE [] END |\n"
            "    MERGE (v:Visit {VISITNUM: row.VISITNUM, USUBJID: row.USUBJID, STUDYID: row.STUDYID})\n"
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
        f"{visit_block}"
    )
    return merge_cypher, create_cypher


def _partition_sdtm_graph_rows(
    rows: Sequence[dict[str, Any]],
) -> list[tuple[str, str | None, list[dict[str, Any]]]]:
    """Group mapped SDTM rows by ``(DOMAIN, seq_prop)`` to avoid cross-domain *SEQ collisions."""
    grouped: dict[tuple[str, str | None], list[dict[str, Any]]] = {}
    for row in rows:
        dom = str(row.get("DOMAIN") or "").strip().upper()
        seq_prop: str | None = None
        if dom and dom != "DM" and _SDTM_DOMAIN_PATTERN.fullmatch(dom) is not None:
            candidate = f"{dom}SEQ"
            if candidate in row and _SDTM_SEQ_PROP_PATTERN.fullmatch(candidate) is not None:
                seq_prop = candidate
        grouped.setdefault((dom, seq_prop), []).append(row)
    return [(dom, seq_prop, part_rows) for (dom, seq_prop), part_rows in grouped.items()]


def _post_process_graph(row: dict[str, Any]) -> dict[str, Any]:
    for visit_key in ("visit_id", "visitId", "VISITNUM", "VISIT_ID"):
        if visit_key in row:
            norm_visit = _normalize_visit_id(row.get(visit_key))
            if norm_visit is None and visit_key == "VISITNUM":
                raw_rd = row.get("RECORD_DATA")
                if isinstance(raw_rd, Mapping) and "VISITNUM" in raw_rd:
                    norm_visit = _normalize_visit_id(raw_rd.get("VISITNUM"))
            row[visit_key] = norm_visit
            break
    else:
        row["visit_id"] = _normalize_visit_id(row.get("visit_id"))

    for key, val in list(row.items()):
        if (
            key in ("record_data", "recordData", "RECORD_DATA", "keywords", "KEYWORDS")
            or val is None
            or isinstance(val, NonCallableMock)
        ):
            continue
        if isinstance(val, Enum):
            row[key] = val.value
        elif isinstance(val, (datetime, date)):
            row[key] = val.isoformat()
        elif isinstance(val, Decimal):
            row[key] = str(val)

    for rd_key in ("record_data", "recordData", "RECORD_DATA"):
        if rd_key in row:
            normalized_record_data = normalize_sink_value(row.get(rd_key, {}))
            row[rd_key] = json.dumps(normalized_record_data, default=_graph_json_default)
            break
    else:
        row["record_data"] = json.dumps({}, default=_graph_json_default)

    for kw_key in ("keywords", "KEYWORDS"):
        keywords = row.get(kw_key)
        if keywords is not None and not isinstance(keywords, (str, NonCallableMock)):
            normalized_keywords = normalize_sink_value(keywords, by_alias=True)
            row[kw_key] = json.dumps(normalized_keywords, default=_graph_json_default)
    return row


def _post_process_graph_sdtm(row: dict[str, Any]) -> dict[str, Any]:
    """Validate SDTM property keys and serialize non-primitive values for Neo4j."""
    if "VISITNUM" in row and not isinstance(row.get("VISITNUM"), NonCallableMock):
        norm_visit = _normalize_visit_id(row.get("VISITNUM"))
        if norm_visit is None:
            row.pop("VISITNUM", None)
        else:
            row["VISITNUM"] = norm_visit
    for key, val in list(row.items()):
        if not isinstance(key, str) or not _IDENTIFIER_PATTERN.fullmatch(key):
            raise ExportConfigurationError(
                f"Neo4j SDTM property key contains invalid characters: {key!r}"
            )
        if val is None or isinstance(val, NonCallableMock):
            continue
        if isinstance(val, Enum):
            row[key] = val.value
        elif isinstance(val, (datetime, date)):
            row[key] = val.isoformat()
        elif isinstance(val, Decimal):
            row[key] = str(val)
        elif isinstance(val, (dict, list, tuple)):
            normalized_val = normalize_sink_value(val)
            row[key] = json.dumps(normalized_val, default=_graph_json_default)
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

    _delegates_sdtm_redaction = True
    reconciles_deleted_on_full_sync = True

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
        self._domain_seq_counters: dict[str, dict[str, int]] = {}
        self._record_seqs: dict[str, int] = {}
        self._form_domain_map_ref: dict[Any, str] = {}
        self._seq_overrides_ref: dict[str, int] = {}
        self._lineage_out_ref: dict[str, dict[str, Any]] = {}
        self._mapper: CentralizedMapper | None = None
        self._mapper_key: tuple[Any, ...] | None = None
        self._driver: Any = None
        self._connect()

    def bind_stream_metadata(self, metadata: dict[str, Any]) -> None:
        """Hydrate and bind SDTM sequence counters into transactional stream metadata."""
        if self.config.as_sdtm:
            bind_sdtm_stream_metadata(metadata, self._seq_counters, self._record_seqs)

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

    def _get_mapper(
        self,
        post_proc: Any,
        seq_overrides: Mapping[str, int] | None,
    ) -> CentralizedMapper:
        """Return a reusable sink-scoped mapper synchronized for the active Neo4j batch."""
        graph_cfg = self.config
        self._form_domain_map_ref.clear()
        active_map = getattr(self, "_active_form_domain_map", None)
        if isinstance(active_map, Mapping):
            self._form_domain_map_ref.update(active_map)
        self._seq_overrides_ref.clear()
        if seq_overrides:
            self._seq_overrides_ref.update(seq_overrides)
        self._lineage_out_ref.clear()

        extra = graph_cfg.extra if isinstance(graph_cfg.extra, dict) else {}
        key = (
            bool(graph_cfg.mask_phi),
            bool(graph_cfg.as_sdtm),
            graph_cfg.sdtm_domain,
            graph_cfg.naming_convention,
            should_preserve_sdtm_record_id(graph_cfg),
            id(extra.get("study_config")),
            id(extra.get("enrichment_pipeline")),
        )
        if self._mapper is None or self._mapper_key != key:
            self._mapper = build_sink_mapper(
                graph_cfg,
                mode="document",
                post_processor=post_proc,
                seq_counters=(
                    self._seq_counters
                    if (not graph_cfg.as_sdtm or graph_cfg.sdtm_domain is not None)
                    else None
                ),
                domain_seq_counters=self._domain_seq_counters if graph_cfg.as_sdtm else None,
                form_domain_map=self._form_domain_map_ref,
                seq_overrides=self._seq_overrides_ref if graph_cfg.as_sdtm else None,
                lineage_out=self._lineage_out_ref if graph_cfg.as_sdtm else None,
                record_seqs=self._record_seqs,
            )
            self._mapper_key = key
        else:
            self._mapper.post_processor = post_proc
        return self._mapper

    def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Write *records* to Neo4j using MERGE (idempotent) or CREATE."""
        if not records:
            return 0

        seq_overrides: dict[str, int] | None = None
        if self.config.as_sdtm:
            rehydrated_counters, seq_overrides, _ = rehydrate_sdtm_lineage(self)
            for dom_code, subj_map in rehydrated_counters.items():
                local_map = self._domain_seq_counters.setdefault(dom_code, {})
                for subj_key, wm_val in subj_map.items():
                    local_map[subj_key] = max(local_map.get(subj_key, 0), wm_val)

        post_proc = _post_process_graph_sdtm if self.config.as_sdtm else _post_process_graph
        mapper = self._get_mapper(post_proc, seq_overrides)
        lineage_out = self._lineage_out_ref
        database = getattr(self.config, "database", "neo4j")
        try:
            if self.config.as_sdtm:
                raw_sdtm_rows = mapper.map_batch(records, study_key=self._study_key)
                if raw_sdtm_rows and any("RECORD_ID" in r for r in raw_sdtm_rows):
                    dedup_by_rec: dict[tuple[Any, Any], int] = {}
                    staged_rows: list[dict[str, Any] | None] = []
                    without_rid: list[dict[str, Any]] = []
                    for r_row in raw_sdtm_rows:
                        if "RECORD_ID" not in r_row or r_row.get("RECORD_ID") is None:
                            without_rid.append(r_row)
                            continue
                        rk = (r_row.get("STUDYID"), r_row.get("RECORD_ID"))
                        if rk in dedup_by_rec:
                            staged_rows[dedup_by_rec[rk]] = None
                        dedup_by_rec[rk] = len(staged_rows)
                        staged_rows.append(r_row)
                    mapped_rows = [r_row for r_row in staged_rows if r_row is not None]
                    if without_rid:
                        mapped_rows.extend(deduplicate_sdtm_rows(without_rid))
                else:
                    mapped_rows = deduplicate_sdtm_rows(raw_sdtm_rows)
            else:
                mapped_rows = [_record_to_row(r, self._study_key, mapper=mapper) for r in records]
        except ExportConfigurationError:
            raise
        except Exception:

            def _raise_malformed() -> int:
                raise TypeError("Malformed Neo4j record batch payload.") from None

            return self._execute_with_retry("export_graph", batch_id, _raise_malformed)

        if not mapped_rows:
            return 0

        cypher_batches: list[tuple[str, list[dict[str, Any]]]] = []
        if self.config.as_sdtm:
            rows_with_rid = [
                r for r in mapped_rows if "RECORD_ID" in r and r.get("RECORD_ID") is not None
            ]
            rows_without_rid = [
                r for r in mapped_rows if "RECORD_ID" not in r or r.get("RECORD_ID") is None
            ]
            if rows_with_rid:
                for dom, seq_prop, part_rows in _partition_sdtm_graph_rows(rows_with_rid):
                    if isinstance(self.config.sdtm_domain, str) and self.config.sdtm_domain.strip():
                        cfg_dom = resolve_sdtm_domain(self.config.sdtm_domain)
                        cand_seq = f"{cfg_dom}SEQ"
                        effective_seq = (
                            cand_seq
                            if cfg_dom != "DM"
                            and _SDTM_DOMAIN_PATTERN.fullmatch(cfg_dom) is not None
                            and any(cand_seq in r for r in part_rows)
                            else None
                        )
                    else:
                        effective_seq = seq_prop if dom != "DM" else None
                    has_visit = any("VISITNUM" in r for r in part_rows)
                    merge_cypher, create_cypher = _build_sdtm_cypher(
                        effective_seq,
                        has_visit,
                        has_record_id=True,
                    )
                    cypher_batches.append(
                        (merge_cypher if self.config.idempotent else create_cypher, part_rows)
                    )
            if rows_without_rid:
                for _dom, seq_prop, part_rows in _partition_sdtm_graph_rows(rows_without_rid):
                    has_visit = any("VISITNUM" in r for r in part_rows)
                    merge_cypher, create_cypher = _build_sdtm_cypher(seq_prop, has_visit)
                    part_cypher = merge_cypher if self.config.idempotent else create_cypher
                    cypher_batches.append((part_cypher, part_rows))
        else:
            if self.config.naming_convention == "camelCase":
                merge_cypher, create_cypher = _build_convention_cypher(
                    "studyKey", "subjectKey", "recordId", "visitId"
                )
            elif self.config.naming_convention == "sdtm":
                merge_cypher, create_cypher = _build_convention_cypher(
                    "STUDYID", "USUBJID", "RECORD_ID", "VISITNUM"
                )
            else:
                merge_cypher, create_cypher = _MERGE_RECORD_CYPHER, _CREATE_RECORD_CYPHER
            cypher_batches.append(
                (merge_cypher if self.config.idempotent else create_cypher, mapped_rows)
            )

        def execute_export() -> int:
            """Execute Cypher transaction to write or merge records in Neo4j."""
            with self._driver.session(database=database) as session:
                for part_cypher, part_rows in cypher_batches:
                    session.run(part_cypher, rows=part_rows)
            logger.debug("Wrote batch %s (%d records)", batch_id, len(mapped_rows))
            return len(mapped_rows)

        written_total = self._execute_with_retry("export_graph", batch_id, execute_export)
        if self.config.as_sdtm and lineage_out:
            commit_sdtm_lineage(self, lineage_out)
        return written_total

    def flush(self) -> None:
        """No-op: Neo4j writes are committed per transaction."""

    def reconcile_deleted(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Delete newly voided Record nodes from Neo4j when running idempotently."""
        if not self.config.idempotent or not records:
            return 0
        cfg = (
            self.config
            if isinstance(self.config, Neo4jSinkConfig)
            else Neo4jSinkConfig(study_key=self._study_key, uri=self._uri, auth=self._auth)
        )
        record_ids = [rid for r in records if (rid := extract_record_id(r)) is not None]
        if cfg.as_sdtm and (not should_preserve_sdtm_record_id(cfg) or not record_ids):
            sdtm_entries = resolve_voided_sdtm_keys(
                records,
                self,
                study_key=getattr(self, "_active_study_key", None) or self._study_key,
                stream_name=getattr(self, "_active_stream_name", None) or cfg.stream_name,
                sdtm_domain=cfg.sdtm_domain,
                form_domain_map=getattr(self, "_active_form_domain_map", None),
            )
            if not sdtm_entries:
                return 0
            grouped_keys: dict[str | None, list[dict[str, Any]]] = {}
            for entry in sdtm_entries:
                dom = str(entry.get("domain") or "").strip().upper()
                raw_seq_col = entry.get("seq_col")
                seq_col = (
                    None
                    if dom == "DM" or not raw_seq_col
                    else (
                        str(raw_seq_col)
                        if _SDTM_SEQ_PROP_PATTERN.fullmatch(str(raw_seq_col))
                        else None
                    )
                )
                if dom != "DM" and (seq_col is None or entry.get("seq_val") is None):
                    continue
                grouped_keys.setdefault(seq_col, []).append(entry)
            if not grouped_keys:
                return 0
            total_keys = sum(len(v) for v in grouped_keys.values())

            def execute_sdtm_reconcile() -> int:
                with self._driver.session(database=cfg.database) as session:
                    for seq_col, part_entries in grouped_keys.items():
                        if seq_col is None:
                            del_cypher = (
                                "UNWIND $keys AS k\n"
                                "MATCH (r:Record {STUDYID: k.study_id, DOMAIN: k.domain, USUBJID: k.usubjid})\n"
                                "DETACH DELETE r\n"
                            )
                        else:
                            del_cypher = (
                                "UNWIND $keys AS k\n"
                                f"MATCH (r:Record {{STUDYID: k.study_id, DOMAIN: k.domain, USUBJID: k.usubjid, {seq_col}: k.seq_val}})\n"
                                "DETACH DELETE r\n"
                            )
                        session.run(del_cypher, keys=part_entries)
                logger.debug("Reconciled %d voided SDTM records in batch %s", total_keys, batch_id)
                return total_keys

            return self._execute_with_retry(
                "reconcile_graph_deleted",
                batch_id,
                execute_sdtm_reconcile,
            )

        if not record_ids:
            return 0

        delete_cypher = _DELETE_RECORD_CYPHER
        if cfg.naming_convention == "camelCase":
            delete_cypher = (
                "UNWIND $record_ids AS rid\n"
                "MATCH (r:Record {recordId: rid, studyKey: $study_key})\n"
                "DETACH DELETE r\n"
            )
        elif cfg.as_sdtm or cfg.naming_convention == "sdtm":
            delete_cypher = (
                "UNWIND $record_ids AS rid\n"
                "MATCH (r:Record {RECORD_ID: rid, STUDYID: $study_key})\n"
                "DETACH DELETE r\n"
            )

        def execute_reconcile() -> int:
            with self._driver.session(database=cfg.database) as session:
                session.run(
                    delete_cypher,
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
