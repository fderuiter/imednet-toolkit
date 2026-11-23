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
    resolve_max_buffered_records,
)
from imednet.spi.sinks import (
    CentralizedMapper,
    ExportSink,
    SinkConfig,
    TransactionalSdtmSequencer,
    apply_quality_gate,
    build_sink_mapper,
    extract_record_id,
    iter_staged_sdtm_records,
    normalize_sink_value,
    require_optional_dep,
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
    domain = str(doc.get("DOMAIN") or "DM").upper()
    usubjid = str(doc.get("USUBJID") or "")
    if domain == "DM" and usubjid:
        doc["_id"] = f"{resolved_study}/DM/{usubjid}"
    elif rid is not None:
        doc["_id"] = _make_document_id(resolved_study, rid)
    else:
        seq_val = next((doc[k] for k in doc if k.endswith("SEQ")), 1)
        doc["_id"] = f"{resolved_study}/{domain}/{usubjid}/{seq_val}"
    normalized: dict[str, Any] = normalize_sink_value(doc)
    return _encode_bson_scalars(normalized)  # type: ignore[no-any-return]


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
    if not mapped:
        return {}
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
        self._sdtm_sequencer = TransactionalSdtmSequencer()
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

        if self.config.as_sdtm:
            max_buf = resolve_max_buffered_records(
                self.config.batch_size, self.config.max_buffered_records
            )

            def execute_sdtm_export() -> int:
                """Stage SDTM sequences transactionally and commit only after MongoDB write succeeds."""
                with self._sdtm_sequencer.stage():
                    sdtm_mapper = build_sink_mapper(
                        self.config,
                        mode="document",
                        post_processor=None,
                        seq_counters=self._sdtm_sequencer,
                    )
                    try:
                        staged_records = list(
                            iter_staged_sdtm_records(
                                records,
                                study_key=self._study_key,
                                sdtm_domain=self.config.sdtm_domain,
                                include_deleted=self.config.include_deleted,
                                mask_phi=self.config.mask_phi,
                                max_buffered_records=max_buf,
                            )
                        )
                        sdtm_docs = [
                            doc
                            for r in staged_records
                            if (doc := _record_to_document(r, self._study_key, mapper=sdtm_mapper))
                        ]
                    except ExportConfigurationError:
                        raise
                    except Exception:
                        raise TypeError("Malformed MongoDB record batch payload.") from None
                    if not sdtm_docs:
                        return 0
                    if self.config.idempotent:
                        pymongo = _require_optional_dep("pymongo", "mongodb")
                        ops = [
                            pymongo.UpdateOne(
                                {"_id": doc["_id"]},
                                {"$set": doc},
                                upsert=True,
                            )
                            for doc in sdtm_docs
                        ]
                        self._collection.bulk_write(ops, ordered=False)
                        written = len(sdtm_docs)
                    else:
                        result = self._collection.insert_many(sdtm_docs, ordered=False)
                        written = len(result.inserted_ids)

                    logger.debug("Wrote batch %s (%d records)", batch_id, written)
                    return written

            return self._execute_with_retry("export_mongodb", batch_id, execute_sdtm_export)

        mapper = build_sink_mapper(
            self.config,
            mode="document",
            post_processor=_post_process_document,
            seq_counters=self._sdtm_sequencer,
        )
        mapped_docs: list[dict[str, Any]] | None = None

        def execute_export() -> int:
            """Perform the actual write or upsert operation to MongoDB."""
            nonlocal mapped_docs
            if mapped_docs is None:
                try:
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

        return self._execute_with_retry("export_mongodb", batch_id, execute_export)

    def flush(self) -> None:
        """No-op: MongoDB writes are committed per bulk operation."""

    def reconcile_deleted(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Delete newly voided documents from MongoDB when running idempotently."""
        if not self.config.idempotent or not records:
            return 0
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
