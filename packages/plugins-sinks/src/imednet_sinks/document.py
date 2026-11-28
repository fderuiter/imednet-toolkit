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

    sdk = ImednetSDK(
        api_key=os.environ["IMEDNET_API_KEY"],
        security_key=os.environ["IMEDNET_SECURITY_KEY"],
    )
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

import contextlib
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Self

from imednet.errors import ExportConfigurationError
from imednet.spi.facade import AsyncImednetFacade, ImednetFacade
from imednet.spi.sink_base import (
    _redact_uri,
    _require_optional_dep,
)
from imednet.spi.sinks import (
    AsyncExportSink,
    CentralizedMapper,
    ExportSink,
    SinkConfig,
    apply_quality_gate,
    async_apply_quality_gate,
    async_run_streaming_export,
    bind_sdtm_stream_metadata,
    build_sink_mapper,
    commit_sdtm_lineage,
    extract_record_id,
    extract_sdtm_composite_key,
    normalize_sink_value,
    rehydrate_sdtm_lineage,
    require_optional_dep,
    resolve_voided_sdtm_keys,
    run_streaming_export,
    should_preserve_sdtm_record_id,
)
from imednet.spi.utils import is_async_facade

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
        super().__post_init__()
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


def _make_non_idempotent_id() -> Any:
    """Return a unique ``_id`` for append-only MongoDB writes (``ObjectId`` or UUID string)."""
    try:
        import bson

        object_id_cls = getattr(bson, "ObjectId", None)
        if callable(object_id_cls):
            return object_id_cls()
    except ImportError:
        pass
    return str(uuid.uuid4())


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
    if rid is None:
        rid = doc.get("RECORD_ID")
    resolved_study = str(doc.get("STUDYID") or study_key)
    domain = str(doc.get("DOMAIN") or "DM").strip().upper()
    raw_usubjid = doc.get("USUBJID")
    usubjid = str(raw_usubjid).strip() if raw_usubjid is not None else ""
    if domain == "DM" and usubjid:
        doc["_id"] = f"{resolved_study}/DM/{usubjid}"
    elif rid is not None:
        doc["_id"] = _make_document_id(resolved_study, rid)
    elif domain == "DM":
        doc["_id"] = f"{resolved_study}/DM/"
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
        if comp is not None and comp[1] == "DM":
            if comp in index_by_key:
                ordered[index_by_key[comp]] = None
            index_by_key[comp] = len(ordered)
            ordered.append(doc)
            continue
        if "_id" in doc and "RECORD_ID" in doc and doc.get("RECORD_ID") is not None:
            comp_id: tuple[Any, ...] = ("_id", doc["_id"])
            if comp_id in index_by_key:
                ordered[index_by_key[comp_id]] = None
            index_by_key[comp_id] = len(ordered)
            ordered.append(doc)
            continue
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


def _build_sdtm_superseded_prune_query(
    mapped_docs: Sequence[dict[str, Any]],
) -> dict[str, Any] | None:
    """Build an optional delete filter for superseded SDTM documents."""
    superseded_clauses: list[dict[str, Any]] = []
    for doc in mapped_docs:
        comp = extract_sdtm_composite_key(doc)
        if "RECORD_ID" in doc and (comp is None or comp[1] != "DM"):
            continue
        if comp is not None and "_id" in doc and (comp[1] == "DM" or comp[4] is not None):
            flt: dict[str, Any] = {
                "STUDYID": comp[0],
                "DOMAIN": comp[1],
                "USUBJID": comp[2],
                "_id": {"$ne": doc["_id"]},
            }
            if comp[1] != "DM" and comp[3] is not None:
                flt[comp[3]] = comp[4]
            superseded_clauses.append(flt)
    if not superseded_clauses:
        return None
    return superseded_clauses[0] if len(superseded_clauses) == 1 else {"$or": superseded_clauses}


class _MongoDbSinkCoreMixin:
    """Shared document-mapping, SDTM lineage, and reconciliation query logic for MongoDB sinks."""

    config: SinkConfig
    _uri: str
    _database: str
    _collection_name: str
    _study_key: str
    _seq_counters: dict[str, int]
    _domain_seq_counters: dict[str, dict[str, int]]
    _record_seqs: dict[str, int]
    _form_domain_map_ref: dict[Any, str]
    _seq_overrides_ref: dict[str, int]
    _lineage_out_ref: dict[str, dict[str, Any]]
    _mapper: CentralizedMapper | None
    _mapper_key: tuple[Any, ...] | None
    _synced_sdtm_session_id: int | None

    def _init_mongo_state(self, config: MongoDbSinkConfig) -> None:
        """Initialize shared MongoDB mapping and SDTM lineage state."""
        self.config = config
        self._uri = config.uri
        self._database = config.database
        self._collection_name = config.collection
        self._study_key = config.study_key
        self._seq_counters = {}
        self._domain_seq_counters = {}
        self._record_seqs = {}
        self._form_domain_map_ref = {}
        self._seq_overrides_ref = {}
        self._lineage_out_ref = {}
        self._mapper = None
        self._mapper_key = None
        self._synced_sdtm_session_id = None

    def bind_stream_metadata(self, metadata: dict[str, Any]) -> None:
        """Hydrate and bind SDTM sequence counters into transactional stream metadata."""
        if self.config.as_sdtm:
            bind_sdtm_stream_metadata(metadata, self._seq_counters, self._record_seqs)

    def _get_mapper(
        self,
        post_proc: Any,
        seq_overrides: dict[str, int] | None,
        domain_seq_counters: dict[str, dict[str, int]] | None = None,
    ) -> CentralizedMapper:
        """Return a reusable sink-scoped mapper synchronized for the active MongoDB batch."""
        doc_cfg = self.config
        self._form_domain_map_ref.clear()
        active_map = getattr(self, "_active_form_domain_map", None)
        if isinstance(active_map, Mapping):
            self._form_domain_map_ref.update(active_map)
        if seq_overrides is not None and self._seq_overrides_ref is not seq_overrides:
            self._seq_overrides_ref = seq_overrides
        self._lineage_out_ref.clear()

        live_domain_counters = (
            domain_seq_counters if domain_seq_counters is not None else self._domain_seq_counters
        )
        live_seq_counters = (
            self._seq_counters if (not doc_cfg.as_sdtm or doc_cfg.sdtm_domain is not None) else None
        )

        extra = doc_cfg.extra if isinstance(doc_cfg.extra, dict) else {}
        key = (
            bool(doc_cfg.mask_phi),
            bool(doc_cfg.as_sdtm),
            doc_cfg.sdtm_domain,
            doc_cfg.naming_convention,
            should_preserve_sdtm_record_id(doc_cfg),
            id(extra.get("study_config")),
            id(extra.get("enrichment_pipeline")),
        )
        if self._mapper is None or self._mapper_key != key:
            self._mapper = build_sink_mapper(
                doc_cfg,
                mode="document",
                post_processor=post_proc,
                seq_counters=live_seq_counters,
                domain_seq_counters=live_domain_counters if doc_cfg.as_sdtm else None,
                form_domain_map=self._form_domain_map_ref,
                seq_overrides=seq_overrides if doc_cfg.as_sdtm else None,
                lineage_out=self._lineage_out_ref if doc_cfg.as_sdtm else None,
                record_seqs=self._record_seqs,
            )
            self._mapper_key = key
        else:
            self._mapper.post_processor = post_proc
            if doc_cfg.as_sdtm:
                self._mapper.bind_sdtm_lineage(
                    domain_seq_counters=live_domain_counters,
                    seq_overrides=seq_overrides,
                    seq_counters=live_seq_counters,
                )
        return self._mapper

    def _prepare_batch_mapper(self) -> tuple[CentralizedMapper, dict[str, dict[str, Any]], bool]:
        """Rehydrate SDTM lineage and return ``(mapper, lineage_out, is_incremental)``."""
        domain_seq_counters: dict[str, dict[str, int]] | None = None
        seq_overrides: dict[str, int] | None = None
        if self.config.as_sdtm:
            domain_seq_counters, seq_overrides, _ = rehydrate_sdtm_lineage(self)
            session_id = id(domain_seq_counters)
            if self._synced_sdtm_session_id != session_id:
                if self._domain_seq_counters:
                    for dom_code, subj_map in self._domain_seq_counters.items():
                        live_map = domain_seq_counters.setdefault(dom_code, {})
                        for subj_key, wm_val in subj_map.items():
                            live_map[subj_key] = max(live_map.get(subj_key, 0), wm_val)
                self._synced_sdtm_session_id = session_id

        post_proc = None if self.config.as_sdtm else _post_process_document
        mapper = self._get_mapper(post_proc, seq_overrides, domain_seq_counters)
        is_incremental = bool(
            self.config.incremental
            or self.config.modified_since is not None
            or self.config.state_provider is not None
        )
        return mapper, self._lineage_out_ref, is_incremental

    def _map_batch_documents(
        self,
        records: Sequence[Any],
        mapper: CentralizedMapper,
    ) -> list[dict[str, Any]]:
        """Map *records* into MongoDB BSON-ready document envelopes."""
        try:
            if self.config.as_sdtm:
                pairs = mapper.map_batch_with_sources(records, study_key=self._study_key)
                raw_docs = [
                    _attach_sdtm_document_id(doc, src_rec, self._study_key)
                    for src_rec, doc in pairs
                ]
                mapped_docs = (
                    _dedupe_sdtm_mongo_docs(raw_docs) if self.config.idempotent else raw_docs
                )
            else:
                mapped_docs = [
                    _record_to_document(r, self._study_key, mapper=mapper) for r in records
                ]
            if not self.config.idempotent:
                for doc in mapped_docs:
                    doc["_id"] = _make_non_idempotent_id()
            return mapped_docs
        except ExportConfigurationError:
            raise
        except Exception:
            raise TypeError("Malformed MongoDB record batch payload.") from None

    def _build_reconcile_query(
        self,
        records: Sequence[Any],
    ) -> tuple[dict[str, Any] | None, int, bool]:
        """Build ``(delete_query, expected_count, is_sdtm)`` for voided MongoDB documents."""
        if self.config.as_sdtm:
            keyed_records = [r for r in records if extract_record_id(r) is not None]
            unkeyed_records = [r for r in records if extract_record_id(r) is None]
            rids = [rid for r in keyed_records if (rid := extract_record_id(r)) is not None]
            preserve_rid = should_preserve_sdtm_record_id(self.config)
            doc_ids = [_make_document_id(self._study_key, rid) for rid in rids]
            seen_doc_ids = set(doc_ids)
            may_include_dm = self.config.sdtm_domain in (None, "DM")
            resolve_kwargs: dict[str, Any] = {
                "study_key": getattr(self, "_active_study_key", None) or self._study_key,
                "stream_name": getattr(self, "_active_stream_name", None)
                or self.config.stream_name,
                "sdtm_domain": self.config.sdtm_domain,
                "form_domain_map": getattr(self, "_active_form_domain_map", None),
            }
            tagged_entries: list[tuple[dict[str, Any], bool]] = []
            if keyed_records and (not preserve_rid or may_include_dm):
                tagged_entries.extend(
                    (entry, True)
                    for entry in resolve_voided_sdtm_keys(keyed_records, self, **resolve_kwargs)
                )
            if unkeyed_records:
                tagged_entries.extend(
                    (entry, False)
                    for entry in resolve_voided_sdtm_keys(unkeyed_records, self, **resolve_kwargs)
                )
            comp_filters: list[dict[str, Any]] = []
            seen_comp_sigs: set[tuple[Any, ...]] = set()
            unkeyed_matched = 0
            for entry, has_rid in tagged_entries:
                dom_upper = str(entry.get("domain") or entry.get("DOMAIN") or "").strip().upper()
                study_id = str(
                    entry.get("study_id") or entry.get("STUDYID") or self._study_key
                ).strip()
                usubjid = str(entry.get("usubjid") or entry.get("USUBJID") or "").strip()
                sig = (study_id, dom_upper, usubjid, entry.get("seq_col"), entry.get("seq_val"))
                if sig in seen_comp_sigs:
                    continue
                seen_comp_sigs.add(sig)
                if dom_upper == "DM" and study_id and usubjid:
                    dm_id = f"{study_id}/DM/{usubjid}"
                    if dm_id not in seen_doc_ids:
                        seen_doc_ids.add(dm_id)
                        doc_ids.append(dm_id)
                    if preserve_rid and has_rid:
                        continue
                if preserve_rid and has_rid:
                    continue
                flt = _sdtm_composite_mongo_filter(entry)
                if flt is not None:
                    comp_filters.append(flt)
                    if not has_rid:
                        unkeyed_matched += 1
            if self.config.sdtm_domain == "DM" and preserve_rid and doc_ids and rids:
                # Filter out raw STUDY/rid IDs when DM IDs resolved cleanly so doc_ids stays minimal
                dm_only_ids = [did for did in doc_ids if "/DM/" in did]
                if len(dm_only_ids) >= len(rids):
                    doc_ids = dm_only_ids
            or_clauses: list[dict[str, Any]] = list(comp_filters)
            if doc_ids:
                or_clauses.append({"_id": {"$in": doc_ids}})
            if not or_clauses:
                return None, 0, True
            delete_query = or_clauses[0] if len(or_clauses) == 1 else {"$or": or_clauses}
            expected_count = (
                len(rids) + unkeyed_matched if preserve_rid else max(len(rids), len(comp_filters))
            )
            return delete_query, expected_count, True

        doc_ids = [
            _make_document_id(self._study_key, rid)
            for r in records
            if (rid := extract_record_id(r)) is not None
        ]
        if not doc_ids:
            return None, 0, False
        return {"_id": {"$in": doc_ids}}, len(doc_ids), False


class MongoDbExportSink(_MongoDbSinkCoreMixin, ExportSink):
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
    reconciles_deleted_on_full_sync = True

    def __init__(self, config: MongoDbSinkConfig) -> None:
        """Initialize the MongoDB export sink.

        Args:
            config: Mandatory sink configuration.
        """
        super().__init__(config)
        self._init_mongo_state(config)
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
            if self._client is not None:
                with contextlib.suppress(Exception):
                    self._client.close()
                self._client = None
            self._collection = None
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

        mapper, lineage_out, is_incremental = self._prepare_batch_mapper()
        mapped_docs: list[dict[str, Any]] | None = None

        def execute_export() -> int:
            """Perform the actual write or upsert operation to MongoDB."""
            nonlocal mapped_docs
            if mapped_docs is None:
                mapped_docs = self._map_batch_documents(records, mapper)
            if not mapped_docs:
                return 0
            if self.config.idempotent:
                pymongo = _require_optional_dep("pymongo", "mongodb")
                if self.config.as_sdtm and is_incremental:
                    prune_q = _build_sdtm_superseded_prune_query(mapped_docs)
                    if prune_q is not None:
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
        delete_query, expected_count, is_sdtm = self._build_reconcile_query(records)
        if delete_query is None:
            return 0

        def execute_reconcile() -> int:
            self._collection.delete_many(delete_query)
            if is_sdtm:
                logger.debug(
                    "Reconciled %d voided SDTM records in batch %s", expected_count, batch_id
                )
            else:
                logger.debug("Reconciled %d voided records in batch %s", expected_count, batch_id)
            return expected_count

        return self._execute_with_retry("reconcile_mongodb_deleted", batch_id, execute_reconcile)

    def close(self) -> None:
        """Close the underlying PyMongo client."""
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None
                self._collection = None


class AsyncMongoDbExportSink(_MongoDbSinkCoreMixin, AsyncExportSink):
    """Asynchronously export iMednet records to MongoDB via native ``pymongo.AsyncMongoClient``."""

    _delegates_sdtm_redaction = True
    reconciles_deleted_on_full_sync = True

    def __init__(self, config: MongoDbSinkConfig) -> None:
        """Initialize the asynchronous MongoDB export sink."""
        super().__init__(config)
        self._init_mongo_state(config)
        _require_optional_dep("pymongo", "mongodb")
        self._client: Any = None
        self._collection: Any = None
        self._connected: bool = False

    async def __aenter__(self) -> Self:
        """Connect to MongoDB asynchronously when entering the sink context."""
        await self._connect()
        return await super().__aenter__()

    async def _connect(self) -> None:
        """Establish a native async connection to MongoDB and verify connectivity."""
        if self._connected and self._collection is not None:
            return
        pymongo = _require_optional_dep("pymongo", "mongodb")
        redacted = _redact_uri(self._uri)
        logger.debug("Connecting asynchronously to MongoDB at %s", redacted)
        try:
            async_client_cls = getattr(pymongo, "AsyncMongoClient", None)
            if async_client_cls is None:
                raise ImportError("pymongo.AsyncMongoClient requires pymongo>=4.9.")
            self._client = async_client_cls(self._uri)
            await self._client.admin.command("ping")
            db = self._client[self._database]
            self._collection = db[self._collection_name]
            self._connected = True
        except Exception as exc:
            if self._client is not None:
                with contextlib.suppress(Exception):
                    await self._client.close()
                self._client = None
            self._collection = None
            self._connected = False
            raise ExportConfigurationError(
                f"Cannot connect to MongoDB at {redacted}. Driver error type: {type(exc).__name__}"
            ) from exc

    async def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Asynchronously write *records* to MongoDB using upsert (idempotent) or insert."""
        await self._connect()
        if not records:
            return 0

        mapper, lineage_out, is_incremental = self._prepare_batch_mapper()
        mapped_docs: list[dict[str, Any]] | None = None

        async def execute_export() -> int:
            nonlocal mapped_docs
            if mapped_docs is None:
                mapped_docs = self._map_batch_documents(records, mapper)
            if not mapped_docs:
                return 0
            if self.config.idempotent:
                pymongo = _require_optional_dep("pymongo", "mongodb")
                if self.config.as_sdtm and is_incremental:
                    prune_q = _build_sdtm_superseded_prune_query(mapped_docs)
                    if prune_q is not None:
                        await self._collection.delete_many(prune_q)
                ops = [
                    pymongo.UpdateOne(
                        {"_id": doc["_id"]},
                        {"$set": doc},
                        upsert=True,
                    )
                    for doc in mapped_docs
                ]
                await self._collection.bulk_write(ops, ordered=False)
                written = len(mapped_docs)
            else:
                result = await self._collection.insert_many(mapped_docs, ordered=False)
                written = len(result.inserted_ids)

            logger.debug("Wrote async batch %s (%d records)", batch_id, written)
            return written

        written_total = await self._execute_with_retry("export_mongodb", batch_id, execute_export)
        if self.config.as_sdtm and lineage_out:
            commit_sdtm_lineage(self, lineage_out)
        return written_total

    async def flush(self) -> None:
        """No-op: MongoDB writes are committed per bulk operation."""

    async def reconcile_deleted(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Asynchronously delete newly voided documents from MongoDB when running idempotently."""
        if not self.config.idempotent or not records:
            return 0
        await self._connect()
        delete_query, expected_count, is_sdtm = self._build_reconcile_query(records)
        if delete_query is None:
            return 0

        async def execute_reconcile() -> int:
            await self._collection.delete_many(delete_query)
            if is_sdtm:
                logger.debug(
                    "Reconciled %d voided SDTM records in async batch %s", expected_count, batch_id
                )
            else:
                logger.debug(
                    "Reconciled %d voided records in async batch %s", expected_count, batch_id
                )
            return expected_count

        return await self._execute_with_retry(
            "reconcile_mongodb_deleted",
            batch_id,
            execute_reconcile,
        )

    async def close(self) -> None:
        """Asynchronously close the underlying ``pymongo.AsyncMongoClient``."""
        if self._client is not None:
            try:
                await self._client.close()
            finally:
                self._client = None
                self._collection = None
                self._connected = False


def _resolve_mongodb_config(
    study_key: str,
    uri: str,
    database: str,
    collection: str,
    *,
    config: MongoDbSinkConfig | None,
    mask_phi: bool | None,
    kwargs: dict[str, Any],
) -> MongoDbSinkConfig:
    """Resolve explicit or keyword-constructed ``MongoDbSinkConfig``."""
    if config is None:
        kwargs.setdefault("mask_phi", True if mask_phi is None else mask_phi)
        return MongoDbSinkConfig(
            study_key=study_key,
            uri=uri,
            database=database,
            collection=collection,
            **kwargs,
        )
    if mask_phi is not None:
        return replace(config, mask_phi=mask_phi)
    return config


def export_to_mongodb(
    sdk: ImednetFacade,
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
    if is_async_facade(sdk):
        raise TypeError(
            "export_to_mongodb() requires a synchronous ImednetSDK (ImednetFacade); "
            "use await async_export_to_mongodb(...) with AsyncImednetSDK."
        )
    resolved_cfg = _resolve_mongodb_config(
        study_key,
        uri,
        database,
        collection,
        config=config,
        mask_phi=mask_phi,
        kwargs=kwargs,
    )
    return run_streaming_export(
        sdk,
        study_key,
        lambda: MongoDbExportSink(config=resolved_cfg),
        resolved_cfg,
        stream_name=f"mongodb_{resolved_cfg.collection}",
        quality_gate_fn=apply_quality_gate,
    )


async def async_export_to_mongodb(
    sdk: AsyncImednetFacade,
    study_key: str,
    uri: str = "",
    database: str = "",
    collection: str = "",
    *,
    config: MongoDbSinkConfig | None = None,
    mask_phi: bool | None = None,
    **kwargs: Any,
) -> int:
    """Asynchronously export study records to MongoDB using :class:`AsyncMongoDbExportSink`."""
    if not is_async_facade(sdk):
        raise TypeError(
            "async_export_to_mongodb() requires an AsyncImednetSDK (AsyncImednetFacade); "
            "use export_to_mongodb(...) with ImednetSDK."
        )
    resolved_cfg = _resolve_mongodb_config(
        study_key,
        uri,
        database,
        collection,
        config=config,
        mask_phi=mask_phi,
        kwargs=kwargs,
    )
    return await async_run_streaming_export(
        sdk,
        study_key,
        lambda: AsyncMongoDbExportSink(config=resolved_cfg),
        resolved_cfg,
        stream_name=f"mongodb_{resolved_cfg.collection}",
        quality_gate_fn=async_apply_quality_gate,
    )


__all__ = [
    "AsyncMongoDbExportSink",
    "MongoDbExportSink",
    "MongoDbSinkConfig",
    "async_export_to_mongodb",
    "export_to_mongodb",
]
