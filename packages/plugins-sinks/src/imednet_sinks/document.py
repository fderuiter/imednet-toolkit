# pylint: disable=duplicate-code
"""MongoDB document-envelope export sink.

This module implements the **structure-preserving export path** for a MongoDB
destination.  Records fetched via
:class:`~imednet_workflows.data_extraction.DataExtractionWorkflow` are wrapped
in a consistent document envelope that preserves the clinical hierarchy.

Document envelope
-----------------
Each document stored in MongoDB represents a single iMednet **Record** and
carries the metadata needed to locate and de-duplicate it:

.. code-block:: json

    {
        "_id": "<study_key>/<record_id>",
        "study_key":     "MYSTUDY",
        "record_id":     1234,
        "subject_id":    99,
        "subject_key":   "SUBJ-001",
        "visit_id":      42,
        "form_id":       7,
        "form_key":      "BASELINE",
        "record_status": "Complete",
        "deleted":       false,
        "date_created":  "2024-01-01T08:00:00Z",
        "date_modified": "2024-01-15T12:00:00Z",
        "record_data":   { "var1": "value1", "var2": 99 },
        "exported_at":   "2024-01-15T12:00:00Z"
    }

The ``_id`` field is a composite key ``<study_key>/<record_id>`` which
guarantees uniqueness within a collection and enables efficient upserts.

Optional dependency
-------------------
Requires ``pymongo`` (install via ``pip install 'imednet[mongodb]'``).
The client is imported lazily at connection time.

Idempotency
-----------
When ``SinkConfig.idempotent`` is ``True`` (default) the sink uses
``bulk_write`` with ``UpdateOne(..., upsert=True)`` operations so that
re-running an export for the same batch updates existing documents rather
than inserting duplicates.

Usage
-----
.. code-block:: python

    import os
    from imednet import ImednetSDK
    from imednet.spi.sink_base import iter_batches
    from imednet_sinks import MongoDbExportSink, MongoDbSinkConfig
    from imednet_workflows.data_extraction import DataExtractionWorkflow

    sdk = ImednetSDK(api_key=os.environ["IMEDNET_API_KEY"])
    records = DataExtractionWorkflow(sdk).extract_records_by_criteria(
        study_key="MYSTUDY",
    )

    config = MongoDbSinkConfig(
        study_key="MYSTUDY",
        uri=os.environ["MONGODB_URI"],
        database="imednet",
        collection="records",
    )
    with MongoDbExportSink(config=config) as sink:
        for i, batch in enumerate(iter_batches(records, config.batch_size)):
            sink.write_batch(batch, batch_id=f"MYSTUDY/all/{i}")
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

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
    build_sink_mapper,
    commit_sdtm_lineage,
    extract_record_id,
    extract_sdtm_composite_key,
    normalize_sink_value,
    rehydrate_sdtm_lineage,
    require_optional_dep,
    resolve_voided_sdtm_keys,
    run_streaming_export,
)

logger = logging.getLogger(__name__)


@dataclass
class MongoDbSinkConfig(SinkConfig):
    """Configuration for MongoDB export sink."""

    uri: str = ""
    database: str = ""
    collection: str = ""
    ledger_path: str | Path | None = None

    def __post_init__(self) -> None:
        """Validate MongoDB config properties after initialization."""
        super().__post_init__()  # type: ignore[no-untyped-call]
        if not self.uri or not isinstance(self.uri, str) or not self.uri.strip():
            raise ValueError("uri must be a non-empty string")
        if not self.database or not isinstance(self.database, str) or not self.database.strip():
            raise ValueError("database must be a non-empty string")
        if (
            not self.collection
            or not isinstance(self.collection, str)
            or not self.collection.strip()
        ):
            raise ValueError("collection must be a non-empty string")


def _make_document_id(study_key: str, record_id: Any) -> str:
    """Return the composite ``_id`` for a MongoDB record document."""
    return f"{study_key}/{record_id}"


def _encode_bson_scalars(value: Any) -> Any:
    """Recursively encode pure ``date`` values to ISO strings and ``Decimal`` to ``Decimal128``."""
    if isinstance(value, dict):
        return {k: _encode_bson_scalars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_encode_bson_scalars(item) for item in value]
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        bson_mod = require_optional_dep("bson", "mongodb")
        return bson_mod.Decimal128(value)
    return value


def _post_process_document(doc: dict[str, Any]) -> dict[str, Any]:
    study_key = doc.get("study_key") or doc.get("studyKey") or doc.get("STUDYID")
    if "record_id" in doc:
        record_id = doc.get("record_id")
    elif "recordId" in doc:
        record_id = doc.get("recordId")
    else:
        record_id = doc.get("RECORD_ID")
    doc["_id"] = _make_document_id(str(study_key), record_id)
    exported_ts = datetime.now(tz=UTC).isoformat()
    if "studyKey" in doc and "study_key" not in doc:
        doc["exportedAt"] = exported_ts
    elif "STUDYID" in doc and "study_key" not in doc:
        doc["EXPORTED_AT"] = exported_ts
    else:
        doc["exported_at"] = exported_ts
    normalized: dict[str, Any] = normalize_sink_value(doc)
    return _encode_bson_scalars(normalized)  # type: ignore[no-any-return]


def _attach_sdtm_document_id(doc: dict[str, Any], record: Any, study_key: str) -> dict[str, Any]:
    """Attach a deterministic MongoDB ``_id`` to a flat SDTM domain document."""
    rid = extract_record_id(record)
    resolved_study = str(doc.get("STUDYID") or study_key)
    if rid is not None:
        doc["_id"] = _make_document_id(resolved_study, rid)
    else:
        domain = str(doc.get("DOMAIN") or "DM").strip().upper()
        usubjid = str(doc.get("USUBJID") or "")
        if domain == "DM":
            doc["_id"] = f"{resolved_study}/DM/{usubjid}"
        else:
            seq_val = next((doc[k] for k in doc if k.endswith("SEQ")), 1)
            doc["_id"] = f"{resolved_study}/{domain}/{usubjid}/{seq_val}"
    normalized: dict[str, Any] = normalize_sink_value(doc)
    return _encode_bson_scalars(normalized)  # type: ignore[no-any-return]


def _sdtm_composite_mongo_filter(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Build a MongoDB query filter matching an SDTM composite key."""
    study_id = entry.get("study_id") or entry.get("STUDYID")
    domain = entry.get("domain") or entry.get("DOMAIN")
    usubjid = entry.get("usubjid") or entry.get("USUBJID")
    if not study_id or not domain or not usubjid:
        return None
    dom_str = str(domain).strip().upper()
    base_flt: dict[str, Any] = {
        "STUDYID": str(study_id),
        "DOMAIN": dom_str,
        "USUBJID": str(usubjid),
    }
    if dom_str == "DM":
        return base_flt
    seq_col = entry.get("seq_col") or f"{dom_str}SEQ"
    seq_val = entry.get("seq_val")
    if seq_val is None:
        return None
    base_flt[str(seq_col)] = int(seq_val)
    return base_flt


def _dedupe_sdtm_mongo_docs(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate mapped SDTM MongoDB documents in-batch by SDTM composite key."""
    if len(docs) <= 1:
        return docs
    index_by_key: dict[tuple[Any, ...], int] = {}
    ordered: list[dict[str, Any] | None] = []
    for doc in docs:
        comp = extract_sdtm_composite_key(doc)
        if comp is None or (comp[1] != "DM" and comp[4] is None):
            ordered.append(doc)
            continue
        if comp in index_by_key:
            ordered[index_by_key[comp]] = None
        index_by_key[comp] = len(ordered)
        ordered.append(doc)
    return [d for d in ordered if d is not None]


def _record_to_document(
    record: Any,
    study_key: str,
    mapper: CentralizedMapper | None = None,
    *,
    mask_phi: bool = True,
) -> dict[str, Any]:
    """Wrap a typed ``Record`` model in the standard document envelope."""
    if mapper is None:
        mapper = CentralizedMapper(
            mode="document",
            post_processor=_post_process_document,
            mask_phi=mask_phi,
        )
    mapped = mapper.map_record(record, study_key=study_key)
    if getattr(mapper, "as_sdtm", False) and "_id" not in mapped:
        return _attach_sdtm_document_id(mapped, record, study_key)
    return mapped


class MongoDbExportSink(ExportSink):
    """Export iMednet records as MongoDB document envelopes.

    Parameters
    ----------
    config:
        Mandatory :class:`~imednet_sinks.document.MongoDbSinkConfig`.

    Raises:
        ~imednet.errors.ExportConfigurationError: When the client cannot connect to the server.
        ImportError: When the ``pymongo`` package is not installed.
    """

    _delegates_sdtm_redaction = True

    def __init__(self, config: MongoDbSinkConfig) -> None:
        """Initialize the MongoDB export sink.

        Args:
            config: Mandatory sink configuration.
        """
        super().__init__(config)
        self.config: MongoDbSinkConfig = config
        self._uri = self.config.uri
        self._database = self.config.database
        self._collection_name = self.config.collection
        self._study_key = self.config.study_key
        self._seq_counters: dict[str, int] = {}
        self._domain_seq_counters: dict[str, dict[str, int]] = {}
        self._client: Any = None
        self._collection: Any = None
        self._connect()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _connect(self) -> None:
        """Establish connection to MongoDB and select the target collection."""
        pymongo = _require_optional_dep("pymongo", "mongodb")
        redacted = _redact_uri(self._uri)
        logger.debug("Connecting to MongoDB at %s", redacted)
        try:
            self._client = pymongo.MongoClient(self._uri)
            # Force a connection check
            self._client.admin.command("ping")
            db = self._client[self._database]
            self._collection = db[self._collection_name]
        except Exception as exc:
            raise ExportConfigurationError(
                f"Cannot connect to MongoDB at {redacted}. Driver error type: {type(exc).__name__}"
            ) from exc

    # ------------------------------------------------------------------
    # ExportSink interface
    # ------------------------------------------------------------------

    def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Write *records* to MongoDB using upsert (idempotent) or insert."""
        if not records:
            return 0

        lineage_out: dict[str, dict[str, Any]] = {}
        seq_overrides: dict[str, int] | None = None
        if self.config.as_sdtm:
            rehydrated_counters, seq_overrides, _ = rehydrate_sdtm_lineage(self)
            for dom_code, subj_map in rehydrated_counters.items():
                local_map = self._domain_seq_counters.setdefault(dom_code, {})
                for subj_key, wm_val in subj_map.items():
                    local_map[subj_key] = max(local_map.get(subj_key, 0), wm_val)

        post_proc = None if self.config.as_sdtm else _post_process_document
        mapper = build_sink_mapper(
            self.config,
            mode="document",
            post_processor=post_proc,
            seq_counters=self._seq_counters if not self.config.as_sdtm else None,
            domain_seq_counters=self._domain_seq_counters if self.config.as_sdtm else None,
            form_domain_map=getattr(self, "_active_form_domain_map", None),
            seq_overrides=seq_overrides,
            lineage_out=lineage_out if self.config.as_sdtm else None,
        )
        mapped_docs: list[dict[str, Any]] | None = None
        is_incremental = bool(
            self.config.incremental
            or self.config.modified_since is not None
            or self.config.state_provider is not None
        )

        def execute_export() -> int:
            """Perform the actual write or upsert operation to MongoDB."""
            nonlocal mapped_docs
            if mapped_docs is None:
                try:
                    if self.config.as_sdtm:
                        pairs = mapper.map_batch_with_sources(records, study_key=self._study_key)
                        raw_docs = [
                            _attach_sdtm_document_id(doc, src_rec, self._study_key)
                            for src_rec, doc in pairs
                        ]
                        mapped_docs = _dedupe_sdtm_mongo_docs(raw_docs)
                    else:
                        mapped_docs = [
                            _record_to_document(r, self._study_key, mapper=mapper) for r in records
                        ]
                except ExportConfigurationError:
                    raise
                except Exception:
                    raise TypeError("Malformed MongoDB record batch payload.") from None
            if not mapped_docs:
                return 0
            if self.config.idempotent:
                pymongo = _require_optional_dep("pymongo", "mongodb")
                if self.config.as_sdtm and is_incremental:
                    superseded_clauses: list[dict[str, Any]] = []
                    for doc in mapped_docs:
                        comp = extract_sdtm_composite_key(doc)
                        if comp is not None and (comp[1] == "DM" or comp[4] is not None):
                            flt: dict[str, Any] = {
                                "STUDYID": comp[0],
                                "DOMAIN": comp[1],
                                "USUBJID": comp[2],
                                "_id": {"$ne": doc["_id"]},
                            }
                            if comp[1] != "DM" and comp[3] is not None:
                                flt[comp[3]] = comp[4]
                            superseded_clauses.append(flt)
                    if superseded_clauses:
                        prune_q = (
                            superseded_clauses[0]
                            if len(superseded_clauses) == 1
                            else {"$or": superseded_clauses}
                        )
                        self._collection.delete_many(prune_q)
                ops = [
                    pymongo.UpdateOne(
                        {"_id": doc["_id"]},
                        {"$set": doc},
                        upsert=True,
                    )
                    for doc in mapped_docs
                ]
                self._collection.bulk_write(ops, ordered=False)
                written = len(mapped_docs)
            else:
                result = self._collection.insert_many(mapped_docs, ordered=False)
                written = len(result.inserted_ids)

            logger.debug("Wrote batch %s (%d records)", batch_id, written)
            return written

        written_total = self._execute_with_retry("export_mongodb", batch_id, execute_export)
        if self.config.as_sdtm and lineage_out:
            commit_sdtm_lineage(self, lineage_out)
        return written_total

    def flush(self) -> None:
        """No-op: MongoDB writes are committed per bulk operation."""

    def reconcile_deleted(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Delete newly voided documents from MongoDB when running idempotently."""
        if not self.config.idempotent or not records:
            return 0
        if self.config.as_sdtm:
            sdtm_entries = resolve_voided_sdtm_keys(
                records,
                self,
                study_key=getattr(self, "_active_study_key", None) or self._study_key,
                stream_name=getattr(self, "_active_stream_name", None) or self.config.stream_name,
                sdtm_domain=self.config.sdtm_domain,
                form_domain_map=getattr(self, "_active_form_domain_map", None),
            )
            comp_filters = [
                flt for e in sdtm_entries if (flt := _sdtm_composite_mongo_filter(e)) is not None
            ]
            doc_ids = [
                _make_document_id(self._study_key, rid)
                for r in records
                if (rid := extract_record_id(r)) is not None
            ]
            or_clauses: list[dict[str, Any]] = list(comp_filters)
            if doc_ids:
                or_clauses.append({"_id": {"$in": doc_ids}})
            if not or_clauses:
                return 0
            delete_query = or_clauses[0] if len(or_clauses) == 1 else {"$or": or_clauses}
            expected_count = len(comp_filters) if comp_filters else len(doc_ids)

            def execute_sdtm_reconcile() -> int:
                self._collection.delete_many(delete_query)
                logger.debug(
                    "Reconciled %d voided SDTM records in batch %s", expected_count, batch_id
                )
                return expected_count

            return self._execute_with_retry(
                "reconcile_mongodb_deleted",
                batch_id,
                execute_sdtm_reconcile,
            )

        doc_ids = [
            _make_document_id(self._study_key, rid)
            for r in records
            if (rid := extract_record_id(r)) is not None
        ]
        if not doc_ids:
            return 0

        def execute_reconcile() -> int:
            self._collection.delete_many({"_id": {"$in": doc_ids}})
            logger.debug("Reconciled %d voided records in batch %s", len(doc_ids), batch_id)
            return len(doc_ids)

        return self._execute_with_retry("reconcile_mongodb_deleted", batch_id, execute_reconcile)

    def close(self) -> None:
        """Close the underlying PyMongo client."""
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None
                self._collection = None


def export_to_mongodb(
    sdk: ImednetSDK,
    study_key: str,
    uri: str = "",
    database: str = "",
    collection: str = "",
    *,
    config: MongoDbSinkConfig | None = None,
    mask_phi: bool | None = None,
    **kwargs: Any,
) -> int:
    """Export study records to MongoDB using :class:`MongoDbExportSink`."""
    if config is None:
        kwargs.setdefault("mask_phi", True if mask_phi is None else mask_phi)
        config = MongoDbSinkConfig(
            study_key=study_key,
            uri=uri,
            database=database,
            collection=collection,
            **kwargs,
        )
    elif mask_phi is not None:
        config = replace(config, mask_phi=mask_phi)

    resolved_cfg = config
    return run_streaming_export(
        sdk,
        study_key,
        lambda: MongoDbExportSink(config=resolved_cfg),
        resolved_cfg,
        stream_name=f"mongodb_{resolved_cfg.collection}",
        quality_gate_fn=apply_quality_gate,
    )


__all__ = ["MongoDbExportSink", "MongoDbSinkConfig", "export_to_mongodb"]
