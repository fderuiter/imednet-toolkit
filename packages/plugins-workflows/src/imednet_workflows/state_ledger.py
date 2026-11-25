"""Stateful incremental high-water mark tracker for workflow streams."""

from __future__ import annotations

import abc
import contextlib
import copy
import json
import os
import re
import socket
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from imednet.spi.sinks import redact_uri
from imednet.spi.utils import mask_clinical_phi, redact_sensitive_payload, redact_sensitive_text


def _sanitize_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Redact credentials and mask clinical PHI keys in stream checkpoint metadata."""
    if not isinstance(metadata, dict) or not metadata:
        return {}
    masked = mask_clinical_phi(metadata)
    redacted = redact_sensitive_payload(masked)
    return redacted if isinstance(redacted, dict) else {}


# Graceful fallback if fcntl is not available (e.g. non-UNIX environments)
try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]


class StreamExecutionStatus(str, Enum):
    """Execution status for workflow stream checkpoints."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"

    def __str__(self) -> str:
        """Return the string value of the execution status."""
        return self.value


class InvalidStateTransitionError(ValueError):
    """Raised when an invalid stream execution state transition is requested."""


TRANSITION_MATRIX: dict[StreamExecutionStatus | None, set[StreamExecutionStatus]] = {
    None: {
        StreamExecutionStatus.PENDING,
        StreamExecutionStatus.RUNNING,
        StreamExecutionStatus.SUCCESS,
        StreamExecutionStatus.FAILED,
    },
    StreamExecutionStatus.PENDING: {
        StreamExecutionStatus.PENDING,
        StreamExecutionStatus.RUNNING,
        StreamExecutionStatus.FAILED,
    },
    StreamExecutionStatus.RUNNING: {
        StreamExecutionStatus.SUCCESS,
        StreamExecutionStatus.FAILED,
    },
    StreamExecutionStatus.SUCCESS: {
        StreamExecutionStatus.PENDING,
        StreamExecutionStatus.RUNNING,
        StreamExecutionStatus.SUCCESS,
    },
    StreamExecutionStatus.FAILED: {
        StreamExecutionStatus.PENDING,
        StreamExecutionStatus.RUNNING,
        StreamExecutionStatus.FAILED,
    },
}

VALID_TRANSITIONS = TRANSITION_MATRIX


def _coerce_and_validate_status(status: StreamExecutionStatus | str) -> StreamExecutionStatus:
    """Coerces a status argument into StreamExecutionStatus or raises ValueError."""
    if isinstance(status, StreamExecutionStatus):
        return status
    if isinstance(status, str):
        try:
            return StreamExecutionStatus(status.lower())
        except ValueError as exc:
            valid_values = ", ".join(repr(s.value) for s in StreamExecutionStatus)
            raise ValueError(
                f"Invalid stream execution status '{status}'. Must be one of: {valid_values}."
            ) from exc
    raise ValueError(f"Status must be a StreamExecutionStatus or str, got {type(status).__name__}")


def is_valid_transition(
    current_status: StreamExecutionStatus | str | None,
    target_status: StreamExecutionStatus | str,
) -> bool:
    """Checks whether transitioning from current_status to target_status is permitted."""
    resolved_current: StreamExecutionStatus | None = None
    if current_status is not None:
        if isinstance(current_status, StreamExecutionStatus):
            resolved_current = current_status
        elif isinstance(current_status, str):
            try:
                resolved_current = StreamExecutionStatus(current_status.lower())
            except ValueError:
                resolved_current = StreamExecutionStatus.FAILED

    try:
        resolved_target = _coerce_and_validate_status(target_status)
    except ValueError:
        return False

    allowed_targets = TRANSITION_MATRIX.get(resolved_current, set())
    return resolved_target in allowed_targets


def validate_transition(
    current_status: StreamExecutionStatus | str | None,
    target_status: StreamExecutionStatus | str,
) -> StreamExecutionStatus:
    """Validates and returns target_status if valid, or raises InvalidStateTransitionError."""
    try:
        resolved_target = _coerce_and_validate_status(target_status)
    except ValueError as exc:
        raise InvalidStateTransitionError(str(exc)) from exc

    resolved_current: StreamExecutionStatus | None = None
    if current_status is not None:
        if isinstance(current_status, StreamExecutionStatus):
            resolved_current = current_status
        elif isinstance(current_status, str):
            try:
                resolved_current = StreamExecutionStatus(current_status.lower())
            except ValueError:
                resolved_current = StreamExecutionStatus.FAILED

    allowed_targets = TRANSITION_MATRIX.get(resolved_current, set())
    if resolved_target not in allowed_targets:
        curr_str = resolved_current.value if resolved_current else "None"
        raise InvalidStateTransitionError(
            f"Cannot transition stream state from '{curr_str}' to '{resolved_target.value}'."
        )
    return resolved_target


class StreamState(BaseModel):
    """Schema for individual stream execution checkpoints."""

    last_timestamp: datetime | None = None
    records_processed: int = 0
    last_run_status: StreamExecutionStatus = StreamExecutionStatus.SUCCESS
    error_message: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    owner_pid: int | None = None
    owner_host: str | None = None
    lease_expires_at: datetime | None = None

    @field_validator("last_run_status", mode="before")
    @classmethod
    def _coerce_status(cls, v: Any) -> StreamExecutionStatus:
        if isinstance(v, StreamExecutionStatus):
            return v
        if isinstance(v, str):
            try:
                return StreamExecutionStatus(v.lower())
            except ValueError:
                return StreamExecutionStatus.FAILED
        return StreamExecutionStatus.FAILED


class StudyState(BaseModel):
    """Schema for all streams in a given study context."""

    streams: dict[str, StreamState] = Field(default_factory=dict)


class LedgerState(BaseModel):
    """Schema for the entire ledger file containing all studies."""

    studies: dict[str, StudyState] = Field(default_factory=dict)


class BaseStateProvider(abc.ABC):
    """Abstract interface for managing high-water marks and state transactions."""

    @abc.abstractmethod
    def get_last_timestamp(self, study_key: str, stream_name: str) -> datetime | None:
        """Returns the high-water mark timestamp for a given study and stream."""

    def get_last_status(self, study_key: str, stream_name: str) -> StreamExecutionStatus | None:
        """Returns the last execution status for a given study and stream."""
        return None

    @abc.abstractmethod
    def set_last_timestamp(
        self,
        study_key: str,
        stream_name: str,
        timestamp: datetime | None,
        records_processed: int = 0,
        status: StreamExecutionStatus | str = StreamExecutionStatus.SUCCESS,
        error_message: str | None = None,
        metadata: dict[str, Any] | None = None,
        force: bool = False,
    ) -> None:
        """Sets the high-water mark timestamp atomically."""

    @abc.abstractmethod
    @contextlib.contextmanager
    def transaction(
        self,
        study_key: str,
        stream_name: str,
        fallback_timestamp: datetime | None = None,
        *,
        lease_ttl_seconds: float | None = None,
    ) -> Generator[dict[str, Any], None, None]:
        """Context manager for transactional state tracking."""

    @contextlib.contextmanager
    def batch_transaction(
        self,
        study_key: str,
        stream_names: Sequence[str],
        fallback_timestamp: datetime | None = None,
        *,
        lease_ttl_seconds: float | None = None,
    ) -> Generator[dict[str, dict[str, Any]], None, None]:
        """Context manager for transactional state tracking across multiple streams in one study."""
        unique_streams = list(dict.fromkeys(stream_names))
        with contextlib.ExitStack() as stack:
            tx_map: dict[str, dict[str, Any]] = {}
            for stream_name in unique_streams:
                tx_map[stream_name] = stack.enter_context(
                    self.transaction(
                        study_key,
                        stream_name,
                        fallback_timestamp=fallback_timestamp,
                        lease_ttl_seconds=lease_ttl_seconds,
                    )
                )
            yield tx_map

    @abc.abstractmethod
    def delete_entry(self, study_key: str, stream_name: str | None = None) -> bool:
        """Deletes a study or specific stream entry from the state.

        Returns ``True`` if the entry existed and was removed, ``False`` otherwise.
        """

    @abc.abstractmethod
    def read_state(self) -> LedgerState:
        """Reads and validates the current full state (for CLI display)."""

    def get_sdtm_lineage(self, study_key: str, stream_name: str) -> dict[str, Any]:
        """Return persisted SDTM sidecar lineage and monotonic sequence watermarks."""
        state = self.read_state()
        study = state.studies.get(study_key)
        if not study:
            return {"sdtm_lineage": {}, "sdtm_seq_watermarks": {}}
        stream = study.streams.get(stream_name)
        if not stream or not isinstance(stream.metadata, dict):
            return {"sdtm_lineage": {}, "sdtm_seq_watermarks": {}}
        raw_lin = stream.metadata.get("sdtm_lineage")
        raw_wm = stream.metadata.get("sdtm_seq_watermarks")
        return {
            "sdtm_lineage": copy.deepcopy(raw_lin) if isinstance(raw_lin, dict) else {},
            "sdtm_seq_watermarks": copy.deepcopy(raw_wm) if isinstance(raw_wm, dict) else {},
        }

    def record_sdtm_lineage(
        self,
        study_key: str,
        stream_name: str,
        lineage_updates: Mapping[str, dict[str, Any] | None] | None = None,
        seq_watermarks: Mapping[str, int] | None = None,
    ) -> None:
        """Persist SDTM sidecar lineage and monotonic sequence watermarks."""
        _ = (study_key, stream_name, lineage_updates, seq_watermarks)


def _merge_preserved_lineage_metadata(
    existing_meta: Mapping[str, Any] | None,
    incoming_meta: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Merge incoming checkpoint metadata while preserving SDTM sidecar lineage and watermarks."""
    merged: dict[str, Any] = dict(incoming_meta) if isinstance(incoming_meta, Mapping) else {}
    if not isinstance(existing_meta, Mapping):
        return merged

    prior_lin = existing_meta.get("sdtm_lineage")
    inc_lin = merged.get("sdtm_lineage")
    if isinstance(prior_lin, Mapping) or isinstance(inc_lin, Mapping):
        combined_lin: dict[str, Any] = (
            {str(k): copy.deepcopy(v) for k, v in prior_lin.items() if isinstance(v, Mapping)}
            if isinstance(prior_lin, Mapping)
            else {}
        )
        if isinstance(inc_lin, Mapping):
            for rk, rv in inc_lin.items():
                if rv is None:
                    combined_lin.pop(str(rk), None)
                elif isinstance(rv, Mapping):
                    combined_lin[str(rk)] = copy.deepcopy(dict(rv))
        if combined_lin:
            merged["sdtm_lineage"] = combined_lin

    prior_wm = existing_meta.get("sdtm_seq_watermarks")
    inc_wm = merged.get("sdtm_seq_watermarks")
    if isinstance(prior_wm, Mapping) or isinstance(inc_wm, Mapping):
        combined_wm: dict[str, int] = {}
        if isinstance(prior_wm, Mapping):
            for wk, wv in prior_wm.items():
                if isinstance(wv, int) and not isinstance(wv, bool):
                    combined_wm[str(wk)] = wv
        if isinstance(inc_wm, Mapping):
            for wk, wv in inc_wm.items():
                if isinstance(wv, int) and not isinstance(wv, bool):
                    combined_wm[str(wk)] = max(combined_wm.get(str(wk), 0), wv)
        if combined_wm:
            merged["sdtm_seq_watermarks"] = combined_wm

    return merged


DEFAULT_LEDGER_PATH: str = os.environ.get(
    "IMEDNET_WORKFLOW_LEDGER_PATH",
    str(Path.home() / ".imednet" / "pipeline_ledger.json"),
)
DEFAULT_LEASE_TTL_SECONDS: float = 3600.0


def _normalize_optional_utc(ts: datetime | None) -> datetime | None:
    """Normalize an optional datetime into timezone-aware UTC."""
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def _parse_optional_timestamp(raw: Any) -> datetime | None:
    """Parse an optional ISO-8601 timestamp string or datetime into UTC."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return _normalize_optional_utc(raw)
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return None
        return _normalize_optional_utc(datetime.fromisoformat(stripped))
    return None


def _is_pid_alive(pid: int) -> bool:
    """Return True if a local OS process with the given PID exists and is not a zombie."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False

    stat_path = Path(f"/proc/{pid}/stat")
    if stat_path.exists():
        with contextlib.suppress(OSError):
            content = stat_path.read_text(encoding="utf-8")
            rparen = content.rfind(")")
            if rparen != -1:
                fields = content[rparen + 2 :].split()
                if fields and fields[0] == "Z":
                    return False
    return True


def _check_stale_running(
    owner_pid: int | None,
    owner_host: str | None,
    lease_expires_at: datetime | None,
    *,
    now: datetime | None = None,
) -> str | None:
    """Return a recovery reason if a RUNNING stream lease is stale, or None if still active."""
    current_time = now if now is not None else datetime.now(UTC)
    normalized_expiry = _normalize_optional_utc(lease_expires_at)
    if normalized_expiry is not None and current_time >= normalized_expiry:
        return f"lease expired at {normalized_expiry.isoformat()}"

    current_host = socket.gethostname()
    if (
        (owner_host is None or owner_host == current_host)
        and owner_pid is not None
        and not _is_pid_alive(owner_pid)
    ):
        host_label = owner_host or current_host
        return f"owner process {owner_pid} on {host_label} is no longer running"

    return None


class FileStateProvider(BaseStateProvider):
    """Manages transactional state bookmarks per study using a local JSON file."""

    def __init__(
        self,
        ledger_path: str | Path = DEFAULT_LEDGER_PATH,
        *,
        lease_ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS,
    ) -> None:
        """Initialize the extraction state ledger.

        Args:
            ledger_path: Path to the JSON file where state is persisted.
            lease_ttl_seconds: Lease duration in seconds for active RUNNING transactions.
        """
        if ledger_path == DEFAULT_LEDGER_PATH and "IMEDNET_WORKFLOW_LEDGER_PATH" in os.environ:
            ledger_path = os.environ["IMEDNET_WORKFLOW_LEDGER_PATH"]
        self.ledger_path = Path(ledger_path)
        self._lock_path = self.ledger_path.with_suffix(".lock")
        self.lease_ttl_seconds = lease_ttl_seconds

    def _ensure_ledger_exists(self) -> None:
        """Ensure the ledger file and its parent directory exist."""
        if not self.ledger_path.exists():
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.ledger_path, "w", encoding="utf-8") as f:
                json.dump({"studies": {}}, f, indent=2)

    @contextlib.contextmanager
    def _lock(self) -> Generator[None, None, None]:
        """Cross-process file lock using flock on UNIX."""
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        if fcntl is None or not hasattr(fcntl, "flock"):
            # Fallback for systems without fcntl (e.g. Windows)
            yield
            return

        with open(self._lock_path, "w") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)  # type: ignore[attr-defined]
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]

    def read_state(self) -> LedgerState:
        """Reads and validates the current ledger state."""
        self._ensure_ledger_exists()
        with open(self.ledger_path, encoding="utf-8") as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError:
                # If file is empty or corrupted, fallback to empty ledger
                return LedgerState(studies={})
        return LedgerState.model_validate(data)

    def write_state(self, state: LedgerState) -> None:
        """Writes the ledger state atomically using a temporary file."""
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        # Serialize first to ensure the data is perfectly valid
        serialized = state.model_dump_json(indent=2)

        dir_name = self.ledger_path.parent
        # Write to temp file in the same directory, then rename atomically
        with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tf:
            tf.write(serialized)
            temp_name = tf.name

        try:
            os.replace(temp_name, self.ledger_path)
        except Exception:
            # Cleanup temp file on failure
            if os.path.exists(temp_name):
                os.remove(temp_name)
            raise

    def get_last_timestamp(self, study_key: str, stream_name: str) -> datetime | None:
        """Returns the high-water mark timestamp for a given study and stream."""
        with self._lock():
            state = self.read_state()
            study = state.studies.get(study_key)
            if not study:
                return None
            stream = study.streams.get(stream_name)
            if not stream:
                return None
            return _normalize_optional_utc(stream.last_timestamp)

    def get_last_status(self, study_key: str, stream_name: str) -> StreamExecutionStatus | None:
        """Returns the last execution status for a given study and stream."""
        with self._lock():
            state = self.read_state()
            study = state.studies.get(study_key)
            if not study:
                return None
            stream = study.streams.get(stream_name)
            if not stream:
                return None
            return stream.last_run_status

    def set_last_timestamp(
        self,
        study_key: str,
        stream_name: str,
        timestamp: datetime | None,
        records_processed: int = 0,
        status: StreamExecutionStatus | str = StreamExecutionStatus.SUCCESS,
        error_message: str | None = None,
        metadata: dict[str, Any] | None = None,
        force: bool = False,
    ) -> None:
        """Sets the high-water mark timestamp atomically."""
        validated_status = _coerce_and_validate_status(status)

        with self._lock():
            state = self.read_state()
            study = state.studies.setdefault(study_key, StudyState())

            current_stream = study.streams.get(stream_name)
            if not force:
                current_status = current_stream.last_run_status if current_stream else None
                validate_transition(current_status, validated_status)

            normalized_ts = _normalize_optional_utc(timestamp)
            prior_meta = current_stream.metadata if current_stream else None
            merged_meta = _merge_preserved_lineage_metadata(prior_meta, metadata)
            redacted_meta = _sanitize_metadata(merged_meta)

            study.streams[stream_name] = StreamState(
                last_timestamp=normalized_ts,
                records_processed=records_processed,
                last_run_status=validated_status,
                error_message=error_message,
                metadata=redacted_meta,
            )
            self.write_state(state)

    def get_sdtm_lineage(self, study_key: str, stream_name: str) -> dict[str, Any]:
        """Return persisted SDTM sidecar lineage and sequence watermarks under file lock."""
        with self._lock():
            return super().get_sdtm_lineage(study_key, stream_name)

    def record_sdtm_lineage(
        self,
        study_key: str,
        stream_name: str,
        lineage_updates: Mapping[str, dict[str, Any] | None] | None = None,
        seq_watermarks: Mapping[str, int] | None = None,
    ) -> None:
        """Atomically merge SDTM sidecar lineage and monotonic sequence watermarks into the ledger."""
        if not lineage_updates and not seq_watermarks:
            return
        with self._lock():
            state = self.read_state()
            study = state.studies.setdefault(study_key, StudyState())
            current_stream = study.streams.get(stream_name)
            prior_meta = current_stream.metadata if current_stream else {}
            incoming: dict[str, Any] = {}
            if lineage_updates:
                incoming["sdtm_lineage"] = dict(lineage_updates)
            if seq_watermarks:
                incoming["sdtm_seq_watermarks"] = dict(seq_watermarks)
            merged_meta = _sanitize_metadata(
                _merge_preserved_lineage_metadata(prior_meta, incoming)
            )
            if current_stream is not None:
                current_stream.metadata = merged_meta
            else:
                study.streams[stream_name] = StreamState(
                    last_timestamp=None,
                    records_processed=0,
                    last_run_status=StreamExecutionStatus.SUCCESS,
                    metadata=merged_meta,
                )
            self.write_state(state)

    @contextlib.contextmanager
    def transaction(
        self,
        study_key: str,
        stream_name: str,
        fallback_timestamp: datetime | None = None,
        *,
        lease_ttl_seconds: float | None = None,
    ) -> Generator[dict[str, Any], None, None]:
        """Context manager for transactional state tracking.

        Yields a dict where user can record 'records_processed', 'new_timestamp', and 'metadata'.
        Saves automatically upon exiting the context with no exceptions.
        """
        with self._lock():
            state = self.read_state()
            study = state.studies.setdefault(study_key, StudyState())
            current_stream = study.streams.get(stream_name)
            current_status = current_stream.last_run_status if current_stream else None

            prior_ts = (
                _normalize_optional_utc(current_stream.last_timestamp) if current_stream else None
            )
            prior_metadata: dict[str, Any] = (
                copy.deepcopy(dict(current_stream.metadata))
                if current_stream is not None and isinstance(current_stream.metadata, dict)
                else {}
            )
            effective_ts = (
                prior_ts if prior_ts is not None else _normalize_optional_utc(fallback_timestamp)
            )

            now = datetime.now(UTC)
            if current_stream is not None and current_status == StreamExecutionStatus.RUNNING:
                stale_reason = _check_stale_running(
                    current_stream.owner_pid,
                    current_stream.owner_host,
                    current_stream.lease_expires_at,
                    now=now,
                )
                if stale_reason is not None:
                    validate_transition(current_status, StreamExecutionStatus.FAILED)
                    study.streams[stream_name] = StreamState(
                        last_timestamp=prior_ts,
                        records_processed=current_stream.records_processed,
                        last_run_status=StreamExecutionStatus.FAILED,
                        error_message=f"Recovered stale running stream: {stale_reason}",
                        metadata=copy.deepcopy(prior_metadata),
                    )
                    self.write_state(state)
                    current_status = StreamExecutionStatus.FAILED

            # Atomic transaction guard: ensure transition to RUNNING is valid
            validate_transition(current_status, StreamExecutionStatus.RUNNING)

            ttl = self.lease_ttl_seconds if lease_ttl_seconds is None else lease_ttl_seconds
            study.streams[stream_name] = StreamState(
                last_timestamp=prior_ts,
                records_processed=0,
                last_run_status=StreamExecutionStatus.RUNNING,
                metadata=copy.deepcopy(prior_metadata),
                owner_pid=os.getpid(),
                owner_host=socket.gethostname(),
                lease_expires_at=now + timedelta(seconds=ttl),
            )
            self.write_state(state)

        tx_data: dict[str, Any] = {
            "last_timestamp": effective_ts,
            "new_timestamp": None,
            "records_processed": 0,
            "metadata": copy.deepcopy(prior_metadata),
        }
        try:
            yield tx_data
        except BaseException as err:
            if isinstance(err, GeneratorExit):
                raise
            # Mark stream as failed and roll back uncommitted high-water mark and metadata
            with self._lock():
                state = self.read_state()
                validate_transition(StreamExecutionStatus.RUNNING, StreamExecutionStatus.FAILED)
                study_entry = state.studies.setdefault(study_key, StudyState())
                study_entry.streams[stream_name] = StreamState(
                    last_timestamp=prior_ts,
                    records_processed=tx_data.get("records_processed", 0),
                    last_run_status=StreamExecutionStatus.FAILED,
                    error_message=str(err),
                    metadata=copy.deepcopy(prior_metadata),
                )
                self.write_state(state)
            raise
        else:
            with self._lock():
                state = self.read_state()
                validate_transition(StreamExecutionStatus.RUNNING, StreamExecutionStatus.SUCCESS)
                raw_new_ts = tx_data.get("new_timestamp")
                commit_ts = (
                    _normalize_optional_utc(raw_new_ts) if raw_new_ts is not None else prior_ts
                )
                raw_commit_meta = tx_data.get("metadata")
                study_entry = state.studies.setdefault(study_key, StudyState())
                running_stream = study_entry.streams.get(stream_name)
                base_meta = _merge_preserved_lineage_metadata(
                    prior_metadata,
                    running_stream.metadata if running_stream else None,
                )
                commit_meta = _merge_preserved_lineage_metadata(
                    base_meta,
                    raw_commit_meta if isinstance(raw_commit_meta, Mapping) else {},
                )
                redacted_commit_meta = _sanitize_metadata(commit_meta)
                study_entry.streams[stream_name] = StreamState(
                    last_timestamp=commit_ts,
                    records_processed=tx_data.get("records_processed", 0),
                    last_run_status=StreamExecutionStatus.SUCCESS,
                    metadata=redacted_commit_meta,
                )
                self.write_state(state)

    @contextlib.contextmanager
    def batch_transaction(
        self,
        study_key: str,
        stream_names: Sequence[str],
        fallback_timestamp: datetime | None = None,
        *,
        lease_ttl_seconds: float | None = None,
    ) -> Generator[dict[str, dict[str, Any]], None, None]:
        """Context manager for transactional state tracking across multiple streams in one study."""
        unique_streams = list(dict.fromkeys(stream_names))
        if not unique_streams:
            yield {}
            return

        fallback_utc = _normalize_optional_utc(fallback_timestamp)
        prior_ts_map: dict[str, datetime | None] = {}
        prior_meta_map: dict[str, dict[str, Any]] = {}
        tx_map: dict[str, dict[str, Any]] = {}

        with self._lock():
            state = self.read_state()
            study = state.studies.setdefault(study_key, StudyState())
            now = datetime.now(UTC)

            for stream_name in unique_streams:
                current_stream = study.streams.get(stream_name)
                current_status = current_stream.last_run_status if current_stream else None
                prior_ts = (
                    _normalize_optional_utc(current_stream.last_timestamp)
                    if current_stream
                    else None
                )
                prior_meta: dict[str, Any] = (
                    copy.deepcopy(dict(current_stream.metadata))
                    if current_stream is not None and isinstance(current_stream.metadata, dict)
                    else {}
                )
                prior_ts_map[stream_name] = prior_ts
                prior_meta_map[stream_name] = prior_meta
                effective_ts = prior_ts if prior_ts is not None else fallback_utc

                if current_stream is not None and current_status == StreamExecutionStatus.RUNNING:
                    stale_reason = _check_stale_running(
                        current_stream.owner_pid,
                        current_stream.owner_host,
                        current_stream.lease_expires_at,
                        now=now,
                    )
                    if stale_reason is not None:
                        validate_transition(current_status, StreamExecutionStatus.FAILED)
                        current_status = StreamExecutionStatus.FAILED

                validate_transition(current_status, StreamExecutionStatus.RUNNING)
                tx_map[stream_name] = {
                    "last_timestamp": effective_ts,
                    "new_timestamp": None,
                    "records_processed": 0,
                    "metadata": copy.deepcopy(prior_meta),
                }

            ttl = self.lease_ttl_seconds if lease_ttl_seconds is None else lease_ttl_seconds
            lease_exp = now + timedelta(seconds=ttl)
            owner_pid = os.getpid()
            owner_host = socket.gethostname()
            for stream_name in unique_streams:
                study.streams[stream_name] = StreamState(
                    last_timestamp=prior_ts_map[stream_name],
                    records_processed=0,
                    last_run_status=StreamExecutionStatus.RUNNING,
                    metadata=copy.deepcopy(prior_meta_map[stream_name]),
                    owner_pid=owner_pid,
                    owner_host=owner_host,
                    lease_expires_at=lease_exp,
                )
            self.write_state(state)

        try:
            yield tx_map
        except BaseException as err:
            if isinstance(err, GeneratorExit):
                raise
            with self._lock():
                state = self.read_state()
                study_entry = state.studies.setdefault(study_key, StudyState())
                for stream_name in unique_streams:
                    validate_transition(StreamExecutionStatus.RUNNING, StreamExecutionStatus.FAILED)
                    tx_data = tx_map[stream_name]
                    study_entry.streams[stream_name] = StreamState(
                        last_timestamp=prior_ts_map[stream_name],
                        records_processed=tx_data.get("records_processed", 0),
                        last_run_status=StreamExecutionStatus.FAILED,
                        error_message=str(err),
                        metadata=copy.deepcopy(prior_meta_map[stream_name]),
                    )
                self.write_state(state)
            raise
        else:
            with self._lock():
                state = self.read_state()
                study_entry = state.studies.setdefault(study_key, StudyState())
                for stream_name in unique_streams:
                    validate_transition(
                        StreamExecutionStatus.RUNNING, StreamExecutionStatus.SUCCESS
                    )
                    tx_data = tx_map[stream_name]
                    raw_new_ts = tx_data.get("new_timestamp")
                    commit_ts = (
                        _normalize_optional_utc(raw_new_ts)
                        if raw_new_ts is not None
                        else prior_ts_map[stream_name]
                    )
                    raw_commit_meta = tx_data.get("metadata")
                    running_stream = study_entry.streams.get(stream_name)
                    base_meta = _merge_preserved_lineage_metadata(
                        prior_meta_map[stream_name],
                        running_stream.metadata if running_stream else None,
                    )
                    commit_meta = _merge_preserved_lineage_metadata(
                        base_meta,
                        raw_commit_meta if isinstance(raw_commit_meta, Mapping) else {},
                    )
                    redacted_commit_meta = _sanitize_metadata(commit_meta)
                    study_entry.streams[stream_name] = StreamState(
                        last_timestamp=commit_ts,
                        records_processed=tx_data.get("records_processed", 0),
                        last_run_status=StreamExecutionStatus.SUCCESS,
                        metadata=redacted_commit_meta,
                    )
                self.write_state(state)

    def delete_entry(self, study_key: str, stream_name: str | None = None) -> bool:
        """Deletes a study or specific stream entry from the ledger under the file lock.

        Returns ``True`` if the entry existed and was removed, ``False`` otherwise.
        """
        with self._lock():
            state = self.read_state()
            if study_key not in state.studies:
                return False
            if stream_name is None:
                del state.studies[study_key]
            else:
                if stream_name not in state.studies[study_key].streams:
                    return False
                del state.studies[study_key].streams[stream_name]
            self.write_state(state)
        return True


# Backward compatibility alias
ExtractionStateLedger = FileStateProvider


class AirflowStateProvider(BaseStateProvider):
    """Manage transactional stream checkpoints using Airflow Variables."""

    DEFAULT_VARIABLE_PREFIX = "imednet_state"

    def __init__(
        self,
        variable_prefix: str = DEFAULT_VARIABLE_PREFIX,
        *,
        lease_ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS,
    ) -> None:
        """Initialize the Airflow Variable state provider.

        Args:
            variable_prefix: Prefix namespace for Airflow ``Variable`` checkpoint keys.
            lease_ttl_seconds: Duration in seconds before an active ``RUNNING`` lease expires.
        """
        cleaned_prefix = variable_prefix.strip().rstrip(":")
        self.variable_prefix = cleaned_prefix or self.DEFAULT_VARIABLE_PREFIX
        self.lease_ttl_seconds = lease_ttl_seconds

    @staticmethod
    def _encode_key_part(value: str) -> str:
        """Encode a key segment using RFC 3986 unreserved characters safe for FastAPI paths."""
        return re.sub(
            r"[^A-Za-z0-9_-]",
            lambda m: "".join(f"~{b:02x}" for b in m.group(0).encode("utf-8")),
            value,
        )

    @staticmethod
    def _decode_key_part(encoded: str) -> str:
        """Decode a key segment previously encoded with :meth:`_encode_key_part`."""
        return re.sub(
            r"(?:~[0-9a-fA-F]{2})+",
            lambda m: bytes.fromhex(m.group(0).replace("~", "")).decode("utf-8", errors="replace"),
            encoded,
        )

    def _get_study_variable_key(self, study_key: str) -> str:
        """Return the deterministic consolidated Airflow Variable key bound to a Study."""
        return f"{self.variable_prefix}:{self._encode_key_part(study_key)}"

    def _study_prefix(self, study_key: str) -> str:
        """Return the legacy per-stream Airflow Variable key prefix for a specific Study."""
        return f"{self._get_study_variable_key(study_key)}:"

    def _get_variable_key(self, study_key: str, stream_name: str | None = None) -> str:
        """Return the consolidated Study Variable key or legacy per-stream Variable key."""
        if stream_name is None:
            return self._get_study_variable_key(study_key)
        return f"{self._study_prefix(study_key)}{self._encode_key_part(stream_name)}"

    def _parse_study_variable_key(self, key: str) -> str | None:
        """Decode ``study_key`` from a consolidated ``<prefix>:<encoded_study>`` Variable key."""
        root_prefix = f"{self.variable_prefix}:"
        if not key.startswith(root_prefix):
            return None
        remainder = key[len(root_prefix) :]
        if not remainder or ":" in remainder:
            return None
        return self._decode_key_part(remainder)

    def _parse_variable_key(self, key: str) -> tuple[str, str] | None:
        """Decode ``(study_key, stream_name)`` from a legacy per-stream Airflow Variable key."""
        root_prefix = f"{self.variable_prefix}:"
        if not key.startswith(root_prefix):
            return None
        remainder = key[len(root_prefix) :]
        encoded_study, sep, encoded_stream = remainder.partition(":")
        if not sep or not encoded_study or not encoded_stream:
            return None
        return self._decode_key_part(encoded_study), self._decode_key_part(encoded_stream)

    @staticmethod
    def _airflow_available() -> bool:
        """Return True when the ``airflow`` package is importable."""
        if "airflow" in sys.modules and sys.modules["airflow"] is None:
            return False
        try:
            import airflow  # noqa: F401
        except ImportError:
            return False
        return True

    @classmethod
    def _has_active_task_sdk_comms(cls) -> bool:
        """Return True when running inside an Airflow 3 Task SDK supervisor or SDK stub."""
        if not cls._airflow_available():
            return False
        try:
            from airflow.sdk import Variable as SdkVariable
        except ImportError:
            return False
        if SdkVariable is None:
            return False
        try:
            from airflow.sdk.execution_time import task_runner
        except ImportError:
            return True
        return getattr(task_runner, "SUPERVISOR_COMMS", None) is not None

    @staticmethod
    def _ensure_orm_session() -> None:
        """Ensure Airflow's SQLAlchemy Session factory is initialized for out-of-task calls."""
        try:
            from airflow import settings

            if getattr(settings, "Session", None) is None:
                configure_orm = getattr(settings, "configure_orm", None)
                if callable(configure_orm):
                    configure_orm()
        except Exception:  # noqa: S110
            pass

    @classmethod
    @contextlib.contextmanager
    def _open_orm_read_session(cls) -> Generator[Any, None, None]:
        """Open a read-only SQLAlchemy session without emitting an implicit commit."""
        cls._ensure_orm_session()
        from airflow import settings

        session_factory = getattr(settings, "Session", None)
        if callable(session_factory):
            with session_factory() as session:
                yield session
            return
        from airflow.utils.session import create_session

        with create_session() as session:
            yield session

    @staticmethod
    def _deserialize_json_dict(raw_val: Any) -> dict[str, Any] | None:
        """Normalize an Airflow Variable value into a JSON dictionary or None."""
        if isinstance(raw_val, str):
            try:
                raw_val = json.loads(raw_val)
            except json.JSONDecodeError:
                return None
        return raw_val if isinstance(raw_val, dict) else None

    def _read_variable(self, key: str) -> dict[str, Any] | None:
        """Read and deserialize a checkpoint JSON payload from Airflow Variables."""
        if not self._airflow_available():
            return None

        raw_val: Any = None
        if self._has_active_task_sdk_comms():
            from airflow.sdk import Variable as SdkVariable

            try:
                raw_val = SdkVariable.get(key, default=None, deserialize_json=True)
            except (json.JSONDecodeError, ValueError):
                return None
        else:
            try:
                from airflow.models.variable import Variable as ModelVariable
            except ImportError:
                return None
            self._ensure_orm_session()
            try:
                raw_val = ModelVariable.get(key, default_var=None, deserialize_json=True)
            except (json.JSONDecodeError, ValueError):
                return None

        return self._deserialize_json_dict(raw_val)

    def _write_variable(self, key: str, payload: dict[str, Any]) -> None:
        """Persist a checkpoint JSON payload to Airflow Variables."""
        if not self._airflow_available():
            return

        if self._has_active_task_sdk_comms():
            from airflow.sdk import Variable as SdkVariable

            SdkVariable.set(key, payload, serialize_json=True)
            return

        try:
            from airflow.models.variable import Variable as ModelVariable
        except ImportError:
            return
        self._ensure_orm_session()
        ModelVariable.set(key, payload, serialize_json=True)

    @staticmethod
    def _escape_like_prefix(prefix: str, escape_char: str = "\\") -> str:
        r"""Escape SQL ``LIKE`` wildcard characters (``\\``, ``%``, ``_``) in a key prefix."""
        escaped = prefix.replace(escape_char, escape_char + escape_char)
        escaped = escaped.replace("%", escape_char + "%")
        return escaped.replace("_", escape_char + "_")

    def _key_matches_study(
        self,
        key: str,
        study_key: str,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        """Return True only when ``key`` and optional ``payload`` match ``study_key`` exactly."""
        if isinstance(payload, dict):
            raw_payload_study = payload.get("study_key")
            if (
                isinstance(raw_payload_study, str)
                and raw_payload_study
                and raw_payload_study != study_key
            ):
                return False

        decoded_study = self._parse_study_variable_key(key)
        if decoded_study is not None:
            return decoded_study == study_key

        parsed_legacy = self._parse_variable_key(key)
        if parsed_legacy is not None:
            return parsed_legacy[0] == study_key

        return False

    def _list_variable_keys(self, prefix: str) -> list[str]:
        """List Airflow Variable keys matching literal ``prefix`` with wildcards escaped."""
        if not self._airflow_available():
            return []

        if self._has_active_task_sdk_comms():
            from airflow.sdk import Variable as SdkVariable

            query_prefix: str | None = prefix
            if "\\" in prefix:
                head = prefix.split("\\", 1)[0]
                query_prefix = head if head else None
            return [
                str(k) for k in SdkVariable.keys(prefix=query_prefix) if str(k).startswith(prefix)
            ]

        try:
            from airflow.models.variable import Variable as ModelVariable
            from sqlalchemy import select
        except ImportError:
            return []

        _ = self._escape_like_prefix(prefix)
        with self._open_orm_read_session() as session:
            rows = session.scalars(
                select(ModelVariable.key)
                .where(ModelVariable.key.startswith(prefix, autoescape=True))
                .order_by(ModelVariable.key)
            ).all()
            return [str(k) for k in rows if str(k).startswith(prefix)]

    def _delete_listed_variable_key(self, key: str) -> None:
        """Delete a listed Airflow Variable key without issuing a pre-delete GetVariable read."""
        if not self._airflow_available():
            return

        if self._has_active_task_sdk_comms():
            from airflow.sdk import Variable as SdkVariable

            SdkVariable.delete(key)
            return

        try:
            from airflow.models.variable import Variable as ModelVariable
        except ImportError:
            return
        self._ensure_orm_session()
        ModelVariable.delete(key)

    @staticmethod
    def _parse_timestamp(payload: dict[str, Any] | None) -> datetime | None:
        """Extract a timezone-aware UTC timestamp from a checkpoint payload."""
        if not payload or "last_timestamp" not in payload:
            return None
        raw_ts = payload["last_timestamp"]
        if not isinstance(raw_ts, str):
            return None
        with contextlib.suppress(ValueError):
            return _parse_optional_timestamp(raw_ts)
        return None

    @staticmethod
    def _parse_status(payload: dict[str, Any] | None) -> StreamExecutionStatus | None:
        """Extract a validated StreamExecutionStatus from a checkpoint payload."""
        if not payload or "status" not in payload:
            return None
        raw_status = payload["status"]
        if isinstance(raw_status, StreamExecutionStatus):
            return raw_status
        if isinstance(raw_status, str):
            try:
                return StreamExecutionStatus(raw_status.lower())
            except ValueError:
                return StreamExecutionStatus.FAILED
        return StreamExecutionStatus.FAILED

    @classmethod
    def _extract_study_streams(
        cls,
        payload: dict[str, Any],
        fallback_study_key: str,
    ) -> tuple[str, dict[str, dict[str, Any]]]:
        """Extract ``(study_key, streams_map)`` from a consolidated Study Variable payload."""
        raw_study = payload.get("study_key")
        resolved_study = (
            raw_study if isinstance(raw_study, str) and raw_study else fallback_study_key
        )
        raw_streams = payload.get("streams")
        if isinstance(raw_streams, dict):
            valid_streams = {
                str(s_name): dict(s_val)
                for s_name, s_val in raw_streams.items()
                if isinstance(s_name, str) and s_name and isinstance(s_val, dict)
            }
            return resolved_study, valid_streams

        if "status" not in payload and "last_timestamp" not in payload:
            flat_streams = {
                str(s_name): dict(s_val)
                for s_name, s_val in payload.items()
                if isinstance(s_name, str)
                and s_name != "study_key"
                and isinstance(s_val, dict)
                and ("status" in s_val or "last_timestamp" in s_val)
            }
            if flat_streams:
                return resolved_study, flat_streams
        return resolved_study, {}

    @classmethod
    def _payload_to_stream_state(cls, entry: dict[str, Any]) -> StreamState | None:
        """Validate and convert a raw stream checkpoint dictionary into StreamState."""
        ts = cls._parse_timestamp(entry)
        if ts is None and "status" not in entry:
            return None

        raw_pid = entry.get("owner_pid")
        owner_pid_val = (
            raw_pid if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) else None
        )
        raw_host = entry.get("owner_host")
        owner_host_val = raw_host if isinstance(raw_host, str) else None
        lease_exp_val: datetime | None = None
        with contextlib.suppress(ValueError):
            lease_exp_val = _parse_optional_timestamp(entry.get("lease_expires_at"))

        status = cls._parse_status(entry) or StreamExecutionStatus.SUCCESS
        raw_processed = entry.get("records_processed", 0)
        records_processed = (
            raw_processed
            if isinstance(raw_processed, int) and not isinstance(raw_processed, bool)
            else 0
        )
        raw_error = entry.get("error_message")
        raw_meta = entry.get("metadata")
        return StreamState(
            last_timestamp=ts,
            records_processed=records_processed,
            last_run_status=status,
            error_message=raw_error if isinstance(raw_error, str) else None,
            metadata=raw_meta if isinstance(raw_meta, dict) else {},
            owner_pid=owner_pid_val,
            owner_host=owner_host_val,
            lease_expires_at=lease_exp_val,
        )

    @staticmethod
    def _monotonic_timestamp(*candidates: datetime | None) -> datetime | None:
        """Return the highest normalized UTC timestamp across non-None candidates."""
        normalized = [
            ts_utc for cand in candidates if (ts_utc := _normalize_optional_utc(cand)) is not None
        ]
        return max(normalized) if normalized else None

    @staticmethod
    def _build_stream_payload(
        *,
        study_key: str,
        stream_name: str,
        timestamp: datetime | None,
        records_processed: int = 0,
        status: StreamExecutionStatus = StreamExecutionStatus.SUCCESS,
        error_message: str | None = None,
        metadata: dict[str, Any] | None = None,
        owner_pid: int | None = None,
        owner_host: str | None = None,
        lease_expires_at: datetime | None = None,
        lease_token: str | None = None,
    ) -> dict[str, Any]:
        """Build a secret-redacted checkpoint payload for a single stream."""
        normalized_ts = _normalize_optional_utc(timestamp)
        normalized_lease = _normalize_optional_utc(lease_expires_at)
        redacted_error = (
            redact_sensitive_text(str(redact_sensitive_payload(redact_uri(error_message))))
            if error_message is not None
            else None
        )
        redacted_meta = _sanitize_metadata(metadata)
        payload: dict[str, Any] = {
            "study_key": study_key,
            "stream_name": stream_name,
            "last_timestamp": normalized_ts.isoformat() if normalized_ts is not None else None,
            "records_processed": records_processed,
            "status": status.value,
            "error_message": redacted_error,
            "metadata": redacted_meta,
            "owner_pid": owner_pid,
            "owner_host": owner_host,
            "lease_expires_at": (
                normalized_lease.isoformat() if normalized_lease is not None else None
            ),
        }
        if lease_token is not None:
            payload["lease_token"] = lease_token
        return payload

    def _read_stream_entry(self, study_key: str, stream_name: str) -> dict[str, Any] | None:
        """Read a single stream checkpoint from consolidated Study key or legacy fallback."""
        if not self._airflow_available():
            return None

        study_var_key = self._get_study_variable_key(study_key)
        legacy_var_key = self._get_variable_key(study_key, stream_name)

        if self._has_active_task_sdk_comms():
            consolidated = self._read_variable(study_var_key)
            if consolidated is not None:
                if not self._key_matches_study(study_var_key, study_key, consolidated):
                    return None
                _, streams = self._extract_study_streams(consolidated, study_key)
                return streams.get(stream_name)
            legacy_payload = self._read_variable(legacy_var_key)
            if legacy_payload is not None and not self._key_matches_study(
                legacy_var_key, study_key, legacy_payload
            ):
                return None
            return legacy_payload

        try:
            from airflow.models.variable import Variable as ModelVariable
            from sqlalchemy import select
        except ImportError:
            return None

        found: dict[str, dict[str, Any] | None] = {}
        with self._open_orm_read_session() as session:
            rows = session.scalars(
                select(ModelVariable).where(ModelVariable.key.in_([study_var_key, legacy_var_key]))
            ).all()
            for row in rows:
                found[str(row.key)] = self._deserialize_json_dict(row.val)

        if study_var_key in found:
            consolidated = found[study_var_key]
            if consolidated is None or not self._key_matches_study(
                study_var_key, study_key, consolidated
            ):
                return None
            _, streams = self._extract_study_streams(consolidated, study_key)
            return streams.get(stream_name)
        legacy_found = found.get(legacy_var_key)
        if legacy_found is not None and not self._key_matches_study(
            legacy_var_key, study_key, legacy_found
        ):
            return None
        return legacy_found

    def _load_study_state(
        self,
        study_key: str,
    ) -> tuple[str, dict[str, dict[str, Any]], list[str]]:
        """Load consolidated Study checkpoints, falling back to legacy per-stream keys."""
        if not self._airflow_available():
            return study_key, {}, []

        study_var_key = self._get_study_variable_key(study_key)
        legacy_prefix = self._study_prefix(study_key)

        if self._has_active_task_sdk_comms():
            consolidated = self._read_variable(study_var_key)
            if consolidated is not None and self._key_matches_study(
                study_var_key, study_key, consolidated
            ):
                resolved_study, streams = self._extract_study_streams(consolidated, study_key)
                return resolved_study, streams, []

            legacy_keys = [
                k
                for k in sorted(self._list_variable_keys(legacy_prefix))
                if self._key_matches_study(k, study_key)
            ]
            if not legacy_keys:
                return study_key, {}, []

            streams = {}
            resolved_study = study_key
            valid_legacy_keys: list[str] = []
            for legacy_key in legacy_keys:
                entry = self._read_variable(legacy_key)
                if not entry or not self._key_matches_study(legacy_key, study_key, entry):
                    continue
                valid_legacy_keys.append(legacy_key)
                parsed = self._parse_variable_key(legacy_key)
                entry_study = entry.get("study_key")
                if isinstance(entry_study, str) and entry_study:
                    resolved_study = entry_study
                s_name = entry.get("stream_name") or (parsed[1] if parsed else None)
                if isinstance(s_name, str) and s_name:
                    streams[s_name] = dict(entry)
            return resolved_study, streams, valid_legacy_keys

        snapshot = self._load_orm_cas_snapshot(study_key)
        return snapshot.resolved_study, snapshot.streams, snapshot.legacy_keys

    def _load_orm_cas_snapshot(self, study_key: str) -> _OrmStudySnapshot:
        """Load Study checkpoints and raw DB column value for optimistic ORM CAS."""
        if not self._airflow_available():
            return _OrmStudySnapshot(
                resolved_study=study_key,
                streams={},
                legacy_keys=[],
                has_study_row=False,
                expected_raw_val=None,
            )

        try:
            from airflow.models.variable import Variable as ModelVariable
            from sqlalchemy import or_, select
        except ImportError:
            return _OrmStudySnapshot(
                resolved_study=study_key,
                streams={},
                legacy_keys=[],
                has_study_row=False,
                expected_raw_val=None,
            )

        study_var_key = self._get_study_variable_key(study_key)
        legacy_prefix = self._study_prefix(study_key)

        raw_rows: list[tuple[str, str | None, dict[str, Any] | None]] = []
        with self._open_orm_read_session() as session:
            rows = session.scalars(
                select(ModelVariable)
                .where(
                    or_(
                        ModelVariable.key == study_var_key,
                        ModelVariable.key.startswith(legacy_prefix, autoescape=True),
                    )
                )
                .order_by(ModelVariable.key)
            ).all()
            for row in rows:
                raw_col_val = getattr(row, "_val", row.val)
                raw_rows.append(
                    (
                        str(row.key),
                        str(raw_col_val) if raw_col_val is not None else None,
                        self._deserialize_json_dict(row.val),
                    )
                )

        has_study_row = False
        expected_raw_val: str | None = None
        consolidated_payload: dict[str, Any] | None = None
        legacy_keys: list[str] = []
        legacy_entries: list[tuple[str, dict[str, Any]]] = []
        for row_key, raw_col_val, decoded in raw_rows:
            if row_key == study_var_key:
                has_study_row = True
                expected_raw_val = raw_col_val
                if self._key_matches_study(row_key, study_key, decoded):
                    consolidated_payload = decoded
            elif self._key_matches_study(row_key, study_key, decoded):
                legacy_keys.append(row_key)
                if decoded is not None:
                    legacy_entries.append((row_key, decoded))

        if has_study_row:
            if consolidated_payload is None:
                return _OrmStudySnapshot(
                    resolved_study=study_key,
                    streams={},
                    legacy_keys=legacy_keys,
                    has_study_row=True,
                    expected_raw_val=expected_raw_val,
                )
            resolved_study, streams = self._extract_study_streams(consolidated_payload, study_key)
            return _OrmStudySnapshot(
                resolved_study=resolved_study,
                streams=streams,
                legacy_keys=legacy_keys,
                has_study_row=True,
                expected_raw_val=expected_raw_val,
            )

        streams = {}
        resolved_study = study_key
        for legacy_key, entry in legacy_entries:
            parsed = self._parse_variable_key(legacy_key)
            entry_study = entry.get("study_key")
            if isinstance(entry_study, str) and entry_study:
                resolved_study = entry_study
            s_name = entry.get("stream_name") or (parsed[1] if parsed else None)
            if isinstance(s_name, str) and s_name:
                streams[s_name] = dict(entry)
        return _OrmStudySnapshot(
            resolved_study=resolved_study,
            streams=streams,
            legacy_keys=legacy_keys,
            has_study_row=False,
            expected_raw_val=None,
        )

    def _try_commit_orm_cas(
        self,
        study_key: str,
        streams: dict[str, dict[str, Any]],
        snapshot: _OrmStudySnapshot,
    ) -> tuple[bool, _OrmStudySnapshot | None]:
        """Attempt an atomic conditional INSERT/UPDATE on the Study Variable row."""
        if not self._airflow_available():
            return True, snapshot

        try:
            from airflow.models.variable import Variable as ModelVariable
            from airflow.utils.session import create_session
            from sqlalchemy import delete, insert, update
            from sqlalchemy.exc import IntegrityError, OperationalError
        except ImportError:
            return True, snapshot

        study_var_key = self._get_study_variable_key(study_key)
        payload: dict[str, Any] = {
            "study_key": study_key,
            "streams": streams,
        }
        serialized = json.dumps(payload, indent=2)
        val_col = getattr(ModelVariable, "_val", ModelVariable.val)
        if hasattr(ModelVariable, "_val"):
            model_var_cls: Any = ModelVariable
            tmp_var = model_var_cls(key=study_var_key, val=serialized)
            new_raw_val = str(getattr(tmp_var, "_val", serialized))
            col_values: dict[str, Any] = {"_val": new_raw_val}
            if hasattr(ModelVariable, "is_encrypted"):
                col_values["is_encrypted"] = bool(tmp_var.is_encrypted)
        else:
            new_raw_val = serialized
            col_values = {"val": new_raw_val}

        self._ensure_orm_session()
        for lock_attempt in range(5):
            try:
                with create_session() as session:
                    if not snapshot.has_study_row:
                        session.execute(
                            insert(ModelVariable).values(key=study_var_key, **col_values)
                        )
                    else:
                        stmt = update(ModelVariable).where(ModelVariable.key == study_var_key)
                        if snapshot.expected_raw_val is None:
                            stmt = stmt.where(val_col.is_(None))
                        else:
                            stmt = stmt.where(val_col == snapshot.expected_raw_val)
                        result = session.execute(stmt.values(**col_values))
                        if int(getattr(result, "rowcount", 0) or 0) != 1:
                            return False, None
                    if snapshot.legacy_keys:
                        session.execute(
                            delete(ModelVariable).where(
                                ModelVariable.key.in_(list(snapshot.legacy_keys))
                            )
                        )
                break
            except IntegrityError:
                return False, None
            except OperationalError as exc:
                if "locked" in str(exc).lower() and lock_attempt < 4:
                    time.sleep(0.01 * (lock_attempt + 1))
                    continue
                raise

        with contextlib.suppress(Exception):
            from airflow.sdk import SecretCache

            SecretCache.invalidate_variable(study_var_key)
            for legacy_key in snapshot.legacy_keys:
                SecretCache.invalidate_variable(legacy_key)

        return (
            True,
            _OrmStudySnapshot(
                resolved_study=study_key,
                streams=copy.deepcopy(streams),
                legacy_keys=[],
                has_study_row=True,
                expected_raw_val=new_raw_val,
            ),
        )

    def _cas_apply_orm(
        self,
        study_key: str,
        mutator_fn: Callable[[str, dict[str, dict[str, Any]]], dict[str, dict[str, Any]] | None],
        *,
        initial_snapshot: _OrmStudySnapshot | None = None,
        max_retries: int = 12,
    ) -> _OrmStudySnapshot:
        """Atomically read-modify-write Study checkpoints via optimistic compare-and-swap."""
        for attempt in range(max_retries):
            snapshot = (
                initial_snapshot
                if (attempt == 0 and initial_snapshot is not None)
                else self._load_orm_cas_snapshot(study_key)
            )
            working_streams = copy.deepcopy(snapshot.streams)
            updated_streams = mutator_fn(snapshot.resolved_study, working_streams)
            if updated_streams is None:
                return snapshot
            ok, next_snapshot = self._try_commit_orm_cas(
                snapshot.resolved_study,
                updated_streams,
                snapshot,
            )
            if ok and next_snapshot is not None:
                return next_snapshot
            time.sleep(0.002 * (attempt + 1))
        raise InvalidStateTransitionError(
            f"Concurrent modification conflict persisted for study '{study_key}' after {max_retries} CAS retries."
        )

    def _save_study_state(
        self,
        study_key: str,
        streams: dict[str, dict[str, Any]],
        *,
        legacy_keys_to_delete: Sequence[str] = (),
    ) -> None:
        """Persist consolidated Study checkpoints and delete superseded legacy keys."""
        if not self._airflow_available():
            return

        if self._has_active_task_sdk_comms():
            study_var_key = self._get_study_variable_key(study_key)
            payload: dict[str, Any] = {
                "study_key": study_key,
                "streams": streams,
            }
            self._write_variable(study_var_key, payload)
            for legacy_key in legacy_keys_to_delete:
                self._delete_listed_variable_key(legacy_key)
            return

        self._cas_apply_orm(study_key, lambda _resolved_study, _existing: dict(streams))

    def get_last_timestamp(self, study_key: str, stream_name: str) -> datetime | None:
        """Return the high-water mark timestamp for a Study and stream from Airflow Variables."""
        payload = self._read_stream_entry(study_key, stream_name)
        return self._parse_timestamp(payload)

    def get_last_status(self, study_key: str, stream_name: str) -> StreamExecutionStatus | None:
        """Return the execution status for a Study and stream from Airflow Variables."""
        payload = self._read_stream_entry(study_key, stream_name)
        return self._parse_status(payload)

    def get_sdtm_lineage(self, study_key: str, stream_name: str) -> dict[str, Any]:
        """Return persisted SDTM sidecar lineage and sequence watermarks from Airflow Variables."""
        payload = self._read_stream_entry(study_key, stream_name)
        meta = payload.get("metadata") if isinstance(payload, dict) else None
        if not isinstance(meta, dict):
            return {"sdtm_lineage": {}, "sdtm_seq_watermarks": {}}
        raw_lin = meta.get("sdtm_lineage")
        raw_wm = meta.get("sdtm_seq_watermarks")
        return {
            "sdtm_lineage": copy.deepcopy(raw_lin) if isinstance(raw_lin, dict) else {},
            "sdtm_seq_watermarks": copy.deepcopy(raw_wm) if isinstance(raw_wm, dict) else {},
        }

    def record_sdtm_lineage(
        self,
        study_key: str,
        stream_name: str,
        lineage_updates: Mapping[str, dict[str, Any] | None] | None = None,
        seq_watermarks: Mapping[str, int] | None = None,
    ) -> None:
        """Persist SDTM sidecar lineage and monotonic sequence watermarks in Airflow Variables."""
        if not lineage_updates and not seq_watermarks:
            return

        incoming: dict[str, Any] = {}
        if lineage_updates:
            incoming["sdtm_lineage"] = dict(lineage_updates)
        if seq_watermarks:
            incoming["sdtm_seq_watermarks"] = dict(seq_watermarks)

        def _apply_lineage(
            resolved_study: str,
            streams: dict[str, dict[str, Any]],
        ) -> dict[str, dict[str, Any]]:
            existing = streams.get(stream_name)
            prior_meta = existing.get("metadata") if isinstance(existing, dict) else {}
            merged_meta = _merge_preserved_lineage_metadata(
                prior_meta if isinstance(prior_meta, Mapping) else None,
                incoming,
            )
            if isinstance(existing, dict):
                updated = dict(existing)
                updated["metadata"] = _sanitize_metadata(merged_meta)
                streams[stream_name] = updated
            else:
                streams[stream_name] = self._build_stream_payload(
                    study_key=resolved_study,
                    stream_name=stream_name,
                    timestamp=None,
                    records_processed=0,
                    status=StreamExecutionStatus.SUCCESS,
                    metadata=merged_meta,
                )
            return streams

        if self._has_active_task_sdk_comms():
            resolved_study, streams, legacy_keys = self._load_study_state(study_key)
            _apply_lineage(resolved_study, streams)
            self._save_study_state(
                resolved_study,
                streams,
                legacy_keys_to_delete=legacy_keys,
            )
            return

        self._cas_apply_orm(study_key, _apply_lineage)

    def set_last_timestamp(
        self,
        study_key: str,
        stream_name: str,
        timestamp: datetime | None,
        records_processed: int = 0,
        status: StreamExecutionStatus | str = StreamExecutionStatus.SUCCESS,
        error_message: str | None = None,
        metadata: dict[str, Any] | None = None,
        force: bool = False,
        *,
        owner_pid: int | None = None,
        owner_host: str | None = None,
        lease_expires_at: datetime | None = None,
        _preserve_existing_lineage: bool = True,
    ) -> None:
        """Persist the high-water mark checkpoint for a Study and stream in Airflow Variables."""
        validated_status = _coerce_and_validate_status(status)

        def _apply_set(
            resolved_study: str,
            streams: dict[str, dict[str, Any]],
        ) -> dict[str, dict[str, Any]]:
            existing_payload = streams.get(stream_name)
            effective_ts = _normalize_optional_utc(timestamp)
            if not force:
                current_status = self._parse_status(existing_payload)
                validate_transition(current_status, validated_status)
                effective_ts = self._monotonic_timestamp(
                    self._parse_timestamp(existing_payload),
                    effective_ts,
                )

            prior_meta = (
                existing_payload.get("metadata")
                if _preserve_existing_lineage and isinstance(existing_payload, dict)
                else None
            )
            merged_meta = _merge_preserved_lineage_metadata(
                prior_meta if isinstance(prior_meta, Mapping) else None,
                metadata,
            )
            streams[stream_name] = self._build_stream_payload(
                study_key=resolved_study,
                stream_name=stream_name,
                timestamp=effective_ts,
                records_processed=records_processed,
                status=validated_status,
                error_message=error_message,
                metadata=merged_meta,
                owner_pid=owner_pid,
                owner_host=owner_host,
                lease_expires_at=lease_expires_at,
            )
            return streams

        if self._has_active_task_sdk_comms():
            resolved_study, streams, legacy_keys = self._load_study_state(study_key)
            _apply_set(resolved_study, streams)
            self._save_study_state(
                resolved_study,
                streams,
                legacy_keys_to_delete=legacy_keys,
            )
            return

        self._cas_apply_orm(study_key, _apply_set)

    @staticmethod
    def _extract_lease_fields(
        raw_entry: dict[str, Any],
    ) -> tuple[int | None, str | None, datetime | None]:
        """Extract ``(owner_pid, owner_host, lease_expires_at)`` from a raw stream payload."""
        raw_pid = raw_entry.get("owner_pid")
        owner_pid_val = (
            raw_pid if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) else None
        )
        raw_host = raw_entry.get("owner_host")
        owner_host_val = raw_host if isinstance(raw_host, str) else None
        lease_exp_val: datetime | None = None
        with contextlib.suppress(ValueError):
            lease_exp_val = _parse_optional_timestamp(raw_entry.get("lease_expires_at"))
        return owner_pid_val, owner_host_val, lease_exp_val

    def _verify_sdk_lease_ownership(
        self,
        study_key: str,
        stream_name: str,
        expected_lease_token: str,
    ) -> dict[str, dict[str, Any]]:
        """Verify via ``airflow.sdk.Variable`` that ``expected_lease_token`` owns ``stream_name``."""
        if not self._airflow_available():
            return {}
        study_var_key = self._get_study_variable_key(study_key)
        persisted = self._read_variable(study_var_key)
        if persisted is None or not self._key_matches_study(study_var_key, study_key, persisted):
            raise InvalidStateTransitionError(
                f"Concurrent transaction conflict: missing checkpoint lease for '{study_key}' -> '{stream_name}'."
            )
        _, persisted_streams = self._extract_study_streams(persisted, study_key)
        entry = persisted_streams.get(stream_name)
        if (
            not isinstance(entry, dict)
            or self._parse_status(entry) != StreamExecutionStatus.RUNNING
            or entry.get("lease_token") != expected_lease_token
        ):
            raise InvalidStateTransitionError(
                f"Concurrent transaction conflict: lease token mismatch for '{study_key}' -> '{stream_name}'."
            )
        return persisted_streams

    @contextlib.contextmanager
    def transaction(
        self,
        study_key: str,
        stream_name: str,
        fallback_timestamp: datetime | None = None,
        *,
        lease_ttl_seconds: float | None = None,
    ) -> Generator[dict[str, Any], None, None]:
        """Context manager for transactional state tracking backed by Airflow Variables."""
        ttl = self.lease_ttl_seconds if lease_ttl_seconds is None else lease_ttl_seconds
        lease_token = uuid.uuid4().hex
        fallback_utc = _normalize_optional_utc(fallback_timestamp)

        if self._has_active_task_sdk_comms():
            resolved_study, streams, legacy_keys = self._load_study_state(study_key)
            raw_entry = streams.get(stream_name)
            prior_ts = _normalize_optional_utc(self._parse_timestamp(raw_entry))
            raw_prior_meta = raw_entry.get("metadata") if isinstance(raw_entry, dict) else None
            prior_metadata: dict[str, Any] = (
                copy.deepcopy(raw_prior_meta) if isinstance(raw_prior_meta, dict) else {}
            )
            effective_ts = prior_ts if prior_ts is not None else fallback_utc

            current_status = self._parse_status(raw_entry)
            now = datetime.now(UTC)
            if current_status == StreamExecutionStatus.RUNNING and raw_entry is not None:
                owner_pid_val, owner_host_val, lease_exp_val = self._extract_lease_fields(raw_entry)
                stale_reason = _check_stale_running(
                    owner_pid_val,
                    owner_host_val,
                    lease_exp_val,
                    now=now,
                )
                if stale_reason is not None:
                    validate_transition(current_status, StreamExecutionStatus.FAILED)
                    raw_processed = raw_entry.get("records_processed", 0)
                    streams[stream_name] = self._build_stream_payload(
                        study_key=resolved_study,
                        stream_name=stream_name,
                        timestamp=prior_ts,
                        records_processed=(
                            raw_processed
                            if isinstance(raw_processed, int)
                            and not isinstance(raw_processed, bool)
                            else 0
                        ),
                        status=StreamExecutionStatus.FAILED,
                        error_message=f"Recovered stale running stream: {stale_reason}",
                        metadata=copy.deepcopy(prior_metadata),
                    )
                    self._save_study_state(
                        resolved_study,
                        streams,
                        legacy_keys_to_delete=legacy_keys,
                    )
                    legacy_keys = []
                    current_status = StreamExecutionStatus.FAILED

            validate_transition(current_status, StreamExecutionStatus.RUNNING)

            streams[stream_name] = self._build_stream_payload(
                study_key=resolved_study,
                stream_name=stream_name,
                timestamp=prior_ts,
                records_processed=0,
                status=StreamExecutionStatus.RUNNING,
                metadata=copy.deepcopy(prior_metadata),
                owner_pid=os.getpid(),
                owner_host=socket.gethostname(),
                lease_expires_at=now + timedelta(seconds=ttl),
                lease_token=lease_token,
            )
            self._save_study_state(
                resolved_study,
                streams,
                legacy_keys_to_delete=legacy_keys,
            )
            if self._airflow_available():
                self._verify_sdk_lease_ownership(resolved_study, stream_name, lease_token)

            tx_data: dict[str, Any] = {
                "last_timestamp": effective_ts,
                "new_timestamp": None,
                "records_processed": 0,
                "metadata": copy.deepcopy(prior_metadata),
            }

            try:
                yield tx_data
            except BaseException as err:
                if isinstance(err, GeneratorExit):
                    raise
                validate_transition(StreamExecutionStatus.RUNNING, StreamExecutionStatus.FAILED)
                if self._airflow_available():
                    study_var_key = self._get_study_variable_key(resolved_study)
                    latest_doc = self._read_variable(study_var_key)
                    if latest_doc is not None and self._key_matches_study(
                        study_var_key, resolved_study, latest_doc
                    ):
                        _, latest_streams = self._extract_study_streams(latest_doc, resolved_study)
                        latest_entry = latest_streams.get(stream_name)
                        if (
                            isinstance(latest_entry, dict)
                            and self._parse_status(latest_entry) == StreamExecutionStatus.RUNNING
                            and latest_entry.get("lease_token") == lease_token
                        ):
                            rollback_ts = self._monotonic_timestamp(
                                prior_ts,
                                self._parse_timestamp(latest_entry),
                            )
                            latest_streams[stream_name] = self._build_stream_payload(
                                study_key=resolved_study,
                                stream_name=stream_name,
                                timestamp=rollback_ts,
                                records_processed=tx_data.get("records_processed", 0),
                                status=StreamExecutionStatus.FAILED,
                                error_message=str(err),
                                metadata=copy.deepcopy(prior_metadata),
                            )
                            self._save_study_state(resolved_study, latest_streams)
                else:
                    streams[stream_name] = self._build_stream_payload(
                        study_key=resolved_study,
                        stream_name=stream_name,
                        timestamp=prior_ts,
                        records_processed=tx_data.get("records_processed", 0),
                        status=StreamExecutionStatus.FAILED,
                        error_message=str(err),
                        metadata=copy.deepcopy(prior_metadata),
                    )
                    self._save_study_state(resolved_study, streams)
                raise
            else:
                validate_transition(StreamExecutionStatus.RUNNING, StreamExecutionStatus.SUCCESS)
                latest_streams = (
                    self._verify_sdk_lease_ownership(resolved_study, stream_name, lease_token)
                    if self._airflow_available()
                    else streams
                )
                latest_entry = latest_streams.get(stream_name)
                raw_new_ts = tx_data.get("new_timestamp")
                candidate_ts = (
                    _normalize_optional_utc(raw_new_ts) if raw_new_ts is not None else prior_ts
                )
                commit_ts = self._monotonic_timestamp(
                    prior_ts,
                    self._parse_timestamp(latest_entry),
                    candidate_ts,
                )
                raw_commit_meta = tx_data.get("metadata")
                merged_commit_meta = _merge_preserved_lineage_metadata(
                    prior_metadata,
                    raw_commit_meta if isinstance(raw_commit_meta, Mapping) else {},
                )
                latest_streams[stream_name] = self._build_stream_payload(
                    study_key=resolved_study,
                    stream_name=stream_name,
                    timestamp=commit_ts,
                    records_processed=tx_data.get("records_processed", 0),
                    status=StreamExecutionStatus.SUCCESS,
                    metadata=merged_commit_meta,
                )
                self._save_study_state(resolved_study, latest_streams)
            return

        # ORM mode outside RuntimeTaskInstance: atomic SQL compare-and-swap
        acquired_state: dict[str, Any] = {}

        def _acquire_orm_lease(
            resolved_study: str,
            streams: dict[str, dict[str, Any]],
        ) -> dict[str, dict[str, Any]]:
            raw_entry = streams.get(stream_name)
            prior_ts_val = _normalize_optional_utc(self._parse_timestamp(raw_entry))
            raw_prior_meta = raw_entry.get("metadata") if isinstance(raw_entry, dict) else None
            prior_meta_val: dict[str, Any] = (
                copy.deepcopy(raw_prior_meta) if isinstance(raw_prior_meta, dict) else {}
            )
            current_status = self._parse_status(raw_entry)
            now = datetime.now(UTC)
            if current_status == StreamExecutionStatus.RUNNING and raw_entry is not None:
                owner_pid_val, owner_host_val, lease_exp_val = self._extract_lease_fields(raw_entry)
                stale_reason = _check_stale_running(
                    owner_pid_val,
                    owner_host_val,
                    lease_exp_val,
                    now=now,
                )
                if stale_reason is not None:
                    validate_transition(current_status, StreamExecutionStatus.FAILED)
                    current_status = StreamExecutionStatus.FAILED

            validate_transition(current_status, StreamExecutionStatus.RUNNING)
            acquired_state["resolved_study"] = resolved_study
            acquired_state["prior_ts"] = prior_ts_val
            acquired_state["prior_metadata"] = prior_meta_val
            acquired_state["effective_ts"] = (
                prior_ts_val if prior_ts_val is not None else fallback_utc
            )
            streams[stream_name] = self._build_stream_payload(
                study_key=resolved_study,
                stream_name=stream_name,
                timestamp=prior_ts_val,
                records_processed=0,
                status=StreamExecutionStatus.RUNNING,
                metadata=copy.deepcopy(prior_meta_val),
                owner_pid=os.getpid(),
                owner_host=socket.gethostname(),
                lease_expires_at=now + timedelta(seconds=ttl),
                lease_token=lease_token,
            )
            return streams

        active_snapshot = self._cas_apply_orm(study_key, _acquire_orm_lease)
        orm_prior_ts: datetime | None = acquired_state["prior_ts"]
        orm_prior_meta: dict[str, Any] = acquired_state["prior_metadata"]
        orm_tx_data: dict[str, Any] = {
            "last_timestamp": acquired_state["effective_ts"],
            "new_timestamp": None,
            "records_processed": 0,
            "metadata": copy.deepcopy(orm_prior_meta),
        }

        try:
            yield orm_tx_data
        except BaseException as err:
            if isinstance(err, GeneratorExit):
                raise
            err_msg = str(err)
            validate_transition(StreamExecutionStatus.RUNNING, StreamExecutionStatus.FAILED)

            def _rollback_orm_lease(
                resolved_study: str,
                streams: dict[str, dict[str, Any]],
            ) -> dict[str, dict[str, Any]] | None:
                current_entry = streams.get(stream_name)
                if (
                    not isinstance(current_entry, dict)
                    or self._parse_status(current_entry) != StreamExecutionStatus.RUNNING
                    or current_entry.get("lease_token") != lease_token
                ):
                    return None
                rollback_ts = self._monotonic_timestamp(
                    orm_prior_ts,
                    self._parse_timestamp(current_entry),
                )
                streams[stream_name] = self._build_stream_payload(
                    study_key=resolved_study,
                    stream_name=stream_name,
                    timestamp=rollback_ts,
                    records_processed=orm_tx_data.get("records_processed", 0),
                    status=StreamExecutionStatus.FAILED,
                    error_message=err_msg,
                    metadata=copy.deepcopy(orm_prior_meta),
                )
                return streams

            self._cas_apply_orm(
                study_key,
                _rollback_orm_lease,
                initial_snapshot=active_snapshot,
            )
            raise
        else:
            validate_transition(StreamExecutionStatus.RUNNING, StreamExecutionStatus.SUCCESS)

            def _commit_orm_lease(
                resolved_study: str,
                streams: dict[str, dict[str, Any]],
            ) -> dict[str, dict[str, Any]]:
                current_entry = streams.get(stream_name)
                if self._airflow_available() and (
                    not isinstance(current_entry, dict)
                    or self._parse_status(current_entry) != StreamExecutionStatus.RUNNING
                    or current_entry.get("lease_token") != lease_token
                ):
                    raise InvalidStateTransitionError(
                        f"Concurrent transaction conflict: lease lost on '{study_key}' -> '{stream_name}'."
                    )
                raw_new_ts = orm_tx_data.get("new_timestamp")
                candidate_ts = (
                    _normalize_optional_utc(raw_new_ts) if raw_new_ts is not None else orm_prior_ts
                )
                commit_ts = self._monotonic_timestamp(
                    orm_prior_ts,
                    self._parse_timestamp(current_entry),
                    candidate_ts,
                )
                raw_commit_meta = orm_tx_data.get("metadata")
                merged_commit_meta = _merge_preserved_lineage_metadata(
                    orm_prior_meta,
                    raw_commit_meta if isinstance(raw_commit_meta, Mapping) else {},
                )
                streams[stream_name] = self._build_stream_payload(
                    study_key=resolved_study,
                    stream_name=stream_name,
                    timestamp=commit_ts,
                    records_processed=orm_tx_data.get("records_processed", 0),
                    status=StreamExecutionStatus.SUCCESS,
                    metadata=merged_commit_meta,
                )
                return streams

            self._cas_apply_orm(
                study_key,
                _commit_orm_lease,
                initial_snapshot=active_snapshot,
            )

    @contextlib.contextmanager
    def batch_transaction(
        self,
        study_key: str,
        stream_names: Sequence[str],
        fallback_timestamp: datetime | None = None,
        *,
        lease_ttl_seconds: float | None = None,
    ) -> Generator[dict[str, dict[str, Any]], None, None]:
        """Context manager for batched transactional tracking across multiple Study streams."""
        unique_streams = list(dict.fromkeys(stream_names))
        if not unique_streams:
            yield {}
            return

        fallback_utc = _normalize_optional_utc(fallback_timestamp)
        ttl = self.lease_ttl_seconds if lease_ttl_seconds is None else lease_ttl_seconds
        lease_token = uuid.uuid4().hex

        if self._has_active_task_sdk_comms():
            resolved_study, streams, legacy_keys = self._load_study_state(study_key)
            now = datetime.now(UTC)
            prior_ts_map: dict[str, datetime | None] = {}
            prior_meta_map: dict[str, dict[str, Any]] = {}
            tx_map: dict[str, dict[str, Any]] = {}

            for stream_name in unique_streams:
                raw_entry = streams.get(stream_name)
                prior_ts = _normalize_optional_utc(self._parse_timestamp(raw_entry))
                raw_prior_meta = raw_entry.get("metadata") if isinstance(raw_entry, dict) else None
                prior_meta: dict[str, Any] = (
                    copy.deepcopy(raw_prior_meta) if isinstance(raw_prior_meta, dict) else {}
                )
                prior_ts_map[stream_name] = prior_ts
                prior_meta_map[stream_name] = prior_meta
                effective_ts = prior_ts if prior_ts is not None else fallback_utc
                current_status = self._parse_status(raw_entry)

                if current_status == StreamExecutionStatus.RUNNING and raw_entry is not None:
                    owner_pid_val, owner_host_val, lease_exp_val = self._extract_lease_fields(
                        raw_entry
                    )
                    stale_reason = _check_stale_running(
                        owner_pid_val,
                        owner_host_val,
                        lease_exp_val,
                        now=now,
                    )
                    if stale_reason is not None:
                        validate_transition(current_status, StreamExecutionStatus.FAILED)
                        current_status = StreamExecutionStatus.FAILED

                validate_transition(current_status, StreamExecutionStatus.RUNNING)
                tx_map[stream_name] = {
                    "last_timestamp": effective_ts,
                    "new_timestamp": None,
                    "records_processed": 0,
                    "metadata": copy.deepcopy(prior_meta),
                }

            lease_exp = now + timedelta(seconds=ttl)
            owner_pid = os.getpid()
            owner_host = socket.gethostname()
            for stream_name in unique_streams:
                streams[stream_name] = self._build_stream_payload(
                    study_key=resolved_study,
                    stream_name=stream_name,
                    timestamp=prior_ts_map[stream_name],
                    records_processed=0,
                    status=StreamExecutionStatus.RUNNING,
                    metadata=copy.deepcopy(prior_meta_map[stream_name]),
                    owner_pid=owner_pid,
                    owner_host=owner_host,
                    lease_expires_at=lease_exp,
                    lease_token=lease_token,
                )
            self._save_study_state(
                resolved_study,
                streams,
                legacy_keys_to_delete=legacy_keys,
            )

            try:
                yield tx_map
            except BaseException as err:
                if isinstance(err, GeneratorExit):
                    raise
                for stream_name in unique_streams:
                    validate_transition(StreamExecutionStatus.RUNNING, StreamExecutionStatus.FAILED)
                    tx_data = tx_map[stream_name]
                    streams[stream_name] = self._build_stream_payload(
                        study_key=resolved_study,
                        stream_name=stream_name,
                        timestamp=prior_ts_map[stream_name],
                        records_processed=tx_data.get("records_processed", 0),
                        status=StreamExecutionStatus.FAILED,
                        error_message=str(err),
                        metadata=copy.deepcopy(prior_meta_map[stream_name]),
                    )
                self._save_study_state(resolved_study, streams)
                raise
            else:
                for stream_name in unique_streams:
                    validate_transition(
                        StreamExecutionStatus.RUNNING, StreamExecutionStatus.SUCCESS
                    )
                    tx_data = tx_map[stream_name]
                    raw_new_ts = tx_data.get("new_timestamp")
                    candidate_ts = (
                        _normalize_optional_utc(raw_new_ts)
                        if raw_new_ts is not None
                        else prior_ts_map[stream_name]
                    )
                    commit_ts = self._monotonic_timestamp(
                        prior_ts_map[stream_name],
                        candidate_ts,
                    )
                    raw_commit_meta = tx_data.get("metadata")
                    merged_commit_meta = _merge_preserved_lineage_metadata(
                        prior_meta_map[stream_name],
                        raw_commit_meta if isinstance(raw_commit_meta, Mapping) else {},
                    )
                    streams[stream_name] = self._build_stream_payload(
                        study_key=resolved_study,
                        stream_name=stream_name,
                        timestamp=commit_ts,
                        records_processed=tx_data.get("records_processed", 0),
                        status=StreamExecutionStatus.SUCCESS,
                        metadata=merged_commit_meta,
                    )
                self._save_study_state(resolved_study, streams)
            return

        # ORM mode outside RuntimeTaskInstance: atomic SQL compare-and-swap
        orm_prior_ts_map: dict[str, datetime | None] = {}
        orm_prior_meta_map: dict[str, dict[str, Any]] = {}
        orm_tx_map: dict[str, dict[str, Any]] = {}

        def _acquire_batch_orm_lease(
            resolved_study: str,
            streams: dict[str, dict[str, Any]],
        ) -> dict[str, dict[str, Any]]:
            now = datetime.now(UTC)
            local_prior_ts: dict[str, datetime | None] = {}
            local_prior_meta: dict[str, dict[str, Any]] = {}
            local_tx_map: dict[str, dict[str, Any]] = {}

            for stream_name in unique_streams:
                raw_entry = streams.get(stream_name)
                prior_ts = _normalize_optional_utc(self._parse_timestamp(raw_entry))
                raw_prior_meta = raw_entry.get("metadata") if isinstance(raw_entry, dict) else None
                prior_meta: dict[str, Any] = (
                    copy.deepcopy(raw_prior_meta) if isinstance(raw_prior_meta, dict) else {}
                )
                local_prior_ts[stream_name] = prior_ts
                local_prior_meta[stream_name] = prior_meta
                effective_ts = prior_ts if prior_ts is not None else fallback_utc
                current_status = self._parse_status(raw_entry)

                if current_status == StreamExecutionStatus.RUNNING and raw_entry is not None:
                    owner_pid_val, owner_host_val, lease_exp_val = self._extract_lease_fields(
                        raw_entry
                    )
                    stale_reason = _check_stale_running(
                        owner_pid_val,
                        owner_host_val,
                        lease_exp_val,
                        now=now,
                    )
                    if stale_reason is not None:
                        validate_transition(current_status, StreamExecutionStatus.FAILED)
                        current_status = StreamExecutionStatus.FAILED

                validate_transition(current_status, StreamExecutionStatus.RUNNING)
                local_tx_map[stream_name] = {
                    "last_timestamp": effective_ts,
                    "new_timestamp": None,
                    "records_processed": 0,
                    "metadata": copy.deepcopy(prior_meta),
                }

            lease_exp = now + timedelta(seconds=ttl)
            owner_pid = os.getpid()
            owner_host = socket.gethostname()
            for stream_name in unique_streams:
                streams[stream_name] = self._build_stream_payload(
                    study_key=resolved_study,
                    stream_name=stream_name,
                    timestamp=local_prior_ts[stream_name],
                    records_processed=0,
                    status=StreamExecutionStatus.RUNNING,
                    metadata=copy.deepcopy(local_prior_meta[stream_name]),
                    owner_pid=owner_pid,
                    owner_host=owner_host,
                    lease_expires_at=lease_exp,
                    lease_token=lease_token,
                )

            orm_prior_ts_map.clear()
            orm_prior_ts_map.update(local_prior_ts)
            orm_prior_meta_map.clear()
            orm_prior_meta_map.update(local_prior_meta)
            orm_tx_map.clear()
            orm_tx_map.update(local_tx_map)
            return streams

        active_batch_snapshot = self._cas_apply_orm(study_key, _acquire_batch_orm_lease)

        try:
            yield orm_tx_map
        except BaseException as err:
            if isinstance(err, GeneratorExit):
                raise
            err_msg = str(err)

            def _rollback_batch_orm_lease(
                resolved_study: str,
                streams: dict[str, dict[str, Any]],
            ) -> dict[str, dict[str, Any]] | None:
                mutated = False
                for stream_name in unique_streams:
                    validate_transition(StreamExecutionStatus.RUNNING, StreamExecutionStatus.FAILED)
                    current_entry = streams.get(stream_name)
                    if (
                        not isinstance(current_entry, dict)
                        or self._parse_status(current_entry) != StreamExecutionStatus.RUNNING
                        or current_entry.get("lease_token") != lease_token
                    ):
                        continue
                    mutated = True
                    tx_data = orm_tx_map[stream_name]
                    rollback_ts = self._monotonic_timestamp(
                        orm_prior_ts_map[stream_name],
                        self._parse_timestamp(current_entry),
                    )
                    streams[stream_name] = self._build_stream_payload(
                        study_key=resolved_study,
                        stream_name=stream_name,
                        timestamp=rollback_ts,
                        records_processed=tx_data.get("records_processed", 0),
                        status=StreamExecutionStatus.FAILED,
                        error_message=err_msg,
                        metadata=copy.deepcopy(orm_prior_meta_map[stream_name]),
                    )
                return streams if mutated else None

            self._cas_apply_orm(
                study_key,
                _rollback_batch_orm_lease,
                initial_snapshot=active_batch_snapshot,
            )
            raise
        else:

            def _commit_batch_orm_lease(
                resolved_study: str,
                streams: dict[str, dict[str, Any]],
            ) -> dict[str, dict[str, Any]]:
                for stream_name in unique_streams:
                    validate_transition(
                        StreamExecutionStatus.RUNNING, StreamExecutionStatus.SUCCESS
                    )
                    current_entry = streams.get(stream_name)
                    if self._airflow_available() and (
                        not isinstance(current_entry, dict)
                        or self._parse_status(current_entry) != StreamExecutionStatus.RUNNING
                        or current_entry.get("lease_token") != lease_token
                    ):
                        raise InvalidStateTransitionError(
                            f"Concurrent batch transaction conflict: lease lost on '{study_key}' -> '{stream_name}'."
                        )
                    tx_data = orm_tx_map[stream_name]
                    raw_new_ts = tx_data.get("new_timestamp")
                    candidate_ts = (
                        _normalize_optional_utc(raw_new_ts)
                        if raw_new_ts is not None
                        else orm_prior_ts_map[stream_name]
                    )
                    commit_ts = self._monotonic_timestamp(
                        orm_prior_ts_map[stream_name],
                        self._parse_timestamp(current_entry),
                        candidate_ts,
                    )
                    raw_commit_meta = tx_data.get("metadata")
                    merged_commit_meta = _merge_preserved_lineage_metadata(
                        orm_prior_meta_map[stream_name],
                        raw_commit_meta if isinstance(raw_commit_meta, Mapping) else {},
                    )
                    streams[stream_name] = self._build_stream_payload(
                        study_key=resolved_study,
                        stream_name=stream_name,
                        timestamp=commit_ts,
                        records_processed=tx_data.get("records_processed", 0),
                        status=StreamExecutionStatus.SUCCESS,
                        metadata=merged_commit_meta,
                    )
                return streams

            self._cas_apply_orm(
                study_key,
                _commit_batch_orm_lease,
                initial_snapshot=active_batch_snapshot,
            )

    def _delete_study_variables(self, study_key: str) -> bool:
        """Delete the consolidated Study Variable and any legacy per-stream Variables."""
        if not self._airflow_available():
            return False

        study_var_key = self._get_study_variable_key(study_key)
        legacy_prefix = self._study_prefix(study_key)
        raw_study_key = f"{self.variable_prefix}:{study_key}"
        raw_legacy_prefix = f"{raw_study_key}:"

        if self._has_active_task_sdk_comms():
            candidates = list(self._list_variable_keys(study_var_key))
            if raw_study_key != study_var_key and ":" not in study_key:
                for extra_key in self._list_variable_keys(raw_study_key):
                    if extra_key not in candidates:
                        candidates.append(extra_key)
            matched = [
                k
                for k in candidates
                if (
                    k == study_var_key
                    or k.startswith(legacy_prefix)
                    or (
                        raw_study_key != study_var_key
                        and ":" not in study_key
                        and (k == raw_study_key or k.startswith(raw_legacy_prefix))
                    )
                )
                and self._key_matches_study(k, study_key)
            ]
            if not matched:
                return False
            for key in matched:
                self._delete_listed_variable_key(key)
            return True

        try:
            from airflow.models.variable import Variable as ModelVariable
            from airflow.utils.session import create_session
            from sqlalchemy import delete, or_
        except ImportError:
            return False

        conditions = [
            ModelVariable.key == study_var_key,
            ModelVariable.key.startswith(legacy_prefix, autoescape=True),
        ]
        if raw_study_key != study_var_key and ":" not in study_key:
            conditions.extend(
                [
                    ModelVariable.key == raw_study_key,
                    ModelVariable.key.startswith(raw_legacy_prefix, autoescape=True),
                ]
            )

        self._ensure_orm_session()
        with create_session() as session:
            result = session.execute(delete(ModelVariable).where(or_(*conditions)))
            deleted_rows = int(getattr(result, "rowcount", 0) or 0)
        if deleted_rows > 0:
            with contextlib.suppress(Exception):
                from airflow.sdk import SecretCache

                SecretCache.invalidate_variable(study_var_key)
        return deleted_rows > 0

    def delete_entry(self, study_key: str, stream_name: str | None = None) -> bool:
        """Delete a Study or specific stream checkpoint from Airflow Variables."""
        if stream_name is None:
            return self._delete_study_variables(study_key)

        if self._has_active_task_sdk_comms():
            resolved_study, streams, legacy_keys = self._load_study_state(study_key)
            if resolved_study != study_key or stream_name not in streams:
                return False

            del streams[stream_name]
            if streams:
                self._save_study_state(
                    resolved_study,
                    streams,
                    legacy_keys_to_delete=legacy_keys,
                )
                return True

            if legacy_keys:
                for key in legacy_keys:
                    if self._key_matches_study(key, study_key):
                        self._delete_listed_variable_key(key)
            else:
                self._delete_listed_variable_key(self._get_study_variable_key(study_key))
            return True

        removed_state = {"removed": False, "empty_after": False}

        def _remove_stream_orm(
            resolved_study: str,
            streams: dict[str, dict[str, Any]],
        ) -> dict[str, dict[str, Any]] | None:
            if resolved_study != study_key or stream_name not in streams:
                removed_state["removed"] = False
                removed_state["empty_after"] = False
                return None
            removed_state["removed"] = True
            del streams[stream_name]
            if not streams:
                removed_state["empty_after"] = True
                return None
            removed_state["empty_after"] = False
            return streams

        self._cas_apply_orm(study_key, _remove_stream_orm)
        if not removed_state["removed"]:
            return False
        if removed_state["empty_after"]:
            return self._delete_study_variables(study_key)
        return True

    def read_state(self) -> LedgerState:
        """Read and validate the full ledger state across all Study checkpoints."""
        state = LedgerState()
        if not self._airflow_available():
            return state

        root_prefix = f"{self.variable_prefix}:"
        consolidated_rows: list[tuple[str, dict[str, Any]]] = []
        legacy_rows: list[tuple[str, dict[str, Any]]] = []
        consolidated_encoded_studies: set[str] = set()

        if self._has_active_task_sdk_comms():
            keys = sorted(self._list_variable_keys(root_prefix))
            study_keys_list: list[str] = []
            legacy_keys_list: list[tuple[str, str]] = []
            for key in keys:
                remainder = key[len(root_prefix) :]
                encoded_study, sep, encoded_stream = remainder.partition(":")
                if not sep:
                    if encoded_study:
                        study_keys_list.append(key)
                        consolidated_encoded_studies.add(encoded_study)
                elif encoded_study and encoded_stream:
                    legacy_keys_list.append((encoded_study, key))

            for key in study_keys_list:
                payload = self._read_variable(key)
                if payload:
                    consolidated_rows.append((key, payload))

            for encoded_study, key in legacy_keys_list:
                if encoded_study in consolidated_encoded_studies:
                    continue
                payload = self._read_variable(key)
                if payload:
                    legacy_rows.append((key, payload))
        else:
            try:
                from airflow.models.variable import Variable as ModelVariable
                from sqlalchemy import select
            except ImportError:
                return state

            fetched: list[tuple[str, dict[str, Any] | None]] = []
            with self._open_orm_read_session() as session:
                rows = session.scalars(
                    select(ModelVariable)
                    .where(ModelVariable.key.startswith(root_prefix, autoescape=True))
                    .order_by(ModelVariable.key)
                ).all()
                for row in rows:
                    fetched.append((str(row.key), self._deserialize_json_dict(row.val)))

            for key, payload in fetched:
                remainder = key[len(root_prefix) :]
                encoded_study, sep, encoded_stream = remainder.partition(":")
                if not sep:
                    if encoded_study:
                        consolidated_encoded_studies.add(encoded_study)
                        if payload:
                            consolidated_rows.append((key, payload))
                elif encoded_study and encoded_stream and payload:
                    legacy_rows.append((key, payload))

        for key, payload in consolidated_rows:
            decoded_study = self._parse_study_variable_key(key)
            if decoded_study is None:
                continue
            s_key, streams_map = self._extract_study_streams(payload, decoded_study)
            for s_name, entry in streams_map.items():
                stream_state = self._payload_to_stream_state(entry)
                if stream_state is not None:
                    study = state.studies.setdefault(s_key, StudyState())
                    study.streams[s_name] = stream_state

        for key, payload in legacy_rows:
            remainder = key[len(root_prefix) :]
            encoded_study, _, _ = remainder.partition(":")
            if encoded_study in consolidated_encoded_studies:
                continue
            parsed_key = self._parse_variable_key(key)
            raw_s_key = payload.get("study_key") or (parsed_key[0] if parsed_key else None)
            raw_s_name = payload.get("stream_name") or (parsed_key[1] if parsed_key else None)
            if not isinstance(raw_s_key, str) or not isinstance(raw_s_name, str):
                continue
            stream_state = self._payload_to_stream_state(payload)
            if stream_state is not None:
                study = state.studies.setdefault(raw_s_key, StudyState())
                study.streams[raw_s_name] = stream_state

        return state


@dataclass
class _OrmStudySnapshot:
    """Immutable snapshot of an Airflow Study Variable row for optimistic CAS updates."""

    resolved_study: str
    streams: dict[str, dict[str, Any]]
    legacy_keys: list[str]
    has_study_row: bool
    expected_raw_val: str | None


def get_state_provider(
    ledger_path: str | Path = DEFAULT_LEDGER_PATH,
) -> BaseStateProvider:
    """Factory to get the appropriate state provider based on environment."""
    if ledger_path is not None and ledger_path != DEFAULT_LEDGER_PATH:
        return FileStateProvider(ledger_path)
    if "AIRFLOW_CTX_TASK_ID" in os.environ or os.environ.get("USE_AIRFLOW_STATE_PROVIDER") == "1":
        return AirflowStateProvider()
    resolved_path: str | Path = DEFAULT_LEDGER_PATH if ledger_path is None else ledger_path
    if resolved_path == DEFAULT_LEDGER_PATH and "IMEDNET_WORKFLOW_LEDGER_PATH" in os.environ:
        resolved_path = os.environ["IMEDNET_WORKFLOW_LEDGER_PATH"]
    return FileStateProvider(resolved_path)
