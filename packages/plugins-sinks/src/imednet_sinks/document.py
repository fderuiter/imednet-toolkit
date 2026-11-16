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

    from imednet.integrations.document import MongoDbExportSink
    from imednet_workflows.data_extraction import DataExtractionWorkflow

    records = DataExtractionWorkflow(sdk).extract_records_by_criteria(
        study_key="MYSTUDY",
    )

    with MongoDbExportSink(
        uri="******localhost:27017",
        database="imednet",
        collection="records",
    ) as sink:
        for i, batch in enumerate(batched(records, 500)):
            sink.write_batch(batch, batch_id=f"MYSTUDY/all/{i}")
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from imednet.integrations.enrichment import CentralizedMapper

from imednet.errors import ExportConfigurationError
from imednet.integrations.sink_base import (
    ExportSink,
    SinkConfig,
    _redact_uri,
    _require_optional_dep,
    iter_batches,
)
from imednet.sdk import ImednetSDK

logger = logging.getLogger(__name__)


from dataclasses import dataclass


@dataclass
class MongoDbSinkConfig(SinkConfig):
    """Configuration for MongoDB export sink."""

    uri: str = ""
    database: str = ""
    collection: str = ""

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


def _post_process_document(doc: dict[str, Any]) -> dict[str, Any]:
    study_key = doc.get("study_key")
    record_id = doc.get("record_id")
    doc["_id"] = _make_document_id(str(study_key), record_id)
    doc["exported_at"] = datetime.now(tz=UTC).isoformat()
    return doc


def _record_to_document(
    record: Any, study_key: str, mapper: CentralizedMapper | None = None
) -> dict[str, Any]:
    """Wrap a typed ``Record`` model in the standard document envelope."""
    if mapper is None:
        from imednet.integrations.enrichment import CentralizedMapper

        mapper = CentralizedMapper(mode="document", post_processor=_post_process_document)
    return mapper.map_record(record, study_key=study_key)


class MongoDbExportSink(ExportSink):
    """Export iMednet records as MongoDB document envelopes.

    Parameters
    ----------
    config:
        Mandatory :class:`~imednet.integrations.document.MongoDbSinkConfig`.

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
        from imednet.integrations.enrichment import CentralizedMapper

        mapper = CentralizedMapper(mode="document", post_processor=_post_process_document)
        docs = [_record_to_document(r, self._study_key, mapper=mapper) for r in records]
        if not docs:
            return 0

        def execute_export() -> int:
            """Perform the actual write or upsert operation to MongoDB."""
            if self.config.idempotent:
                pymongo = _require_optional_dep("pymongo", "mongodb")
                ops = [
                    pymongo.UpdateOne(
                        {"_id": doc["_id"]},
                        {"$set": doc},
                        upsert=True,
                    )
                    for doc in docs
                ]
                self._collection.bulk_write(ops, ordered=False)
                written = len(docs)
            else:
                result = self._collection.insert_many(docs, ordered=False)
                written = len(result.inserted_ids)

            logger.debug("Wrote batch %s (%d records)", batch_id, written)
            return written

        return self._execute_with_retry("export_mongodb", batch_id, execute_export)

    def flush(self) -> None:
        """No-op: MongoDB writes are committed per bulk operation."""

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
) -> int:
    """Export study records to MongoDB using :class:`MongoDbExportSink`."""
    from imednet.integrations.sink_base import apply_quality_gate

    if config is None:
        config = MongoDbSinkConfig(
            study_key=study_key,
            uri=uri,
            database=database,
            collection=collection,
        )

    records = sdk.records.list(study_key=study_key, record_data_filter=None)
    filtered_records = list(apply_quality_gate(sdk, study_key, records, config))

    total_written = 0
    with MongoDbExportSink(config=config) as sink:
        for index, batch in enumerate(iter_batches(filtered_records, config.batch_size)):
            total_written += sink.write_batch(batch, batch_id=f"{study_key}/records/{index}")
    return total_written


__all__ = ["MongoDbExportSink", "MongoDbSinkConfig", "export_to_mongodb"]
