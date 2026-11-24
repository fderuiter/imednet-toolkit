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
from collections.abc import Generator, Mapping
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from imednet.spi.sinks import redact_uri
from imednet.spi.utils import redact_sensitive_payload, redact_sensitive_text

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

            study.streams[stream_name] = StreamState(
                last_timestamp=normalized_ts,
                records_processed=records_processed,
                last_run_status=validated_status,
                error_message=error_message,
                metadata=merged_meta,
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
            merged_meta = _merge_preserved_lineage_metadata(prior_meta, incoming)
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
            prior_meta = copy.deepcopy(current_stream.metadata) if current_stream else {}
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
                        metadata=dict(prior_meta),
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
                metadata=dict(prior_meta),
                owner_pid=os.getpid(),
                owner_host=socket.gethostname(),
                lease_expires_at=now + timedelta(seconds=ttl),
            )
            self.write_state(state)

        tx_data: dict[str, Any] = {
            "last_timestamp": effective_ts,
            "new_timestamp": None,
            "records_processed": 0,
            "metadata": {},
        }
        try:
            yield tx_data
        except BaseException as err:
            if isinstance(err, GeneratorExit):
                raise
            # Mark stream as failed and preserve previous committed high-water mark and lineage
            with self._lock():
                state = self.read_state()
                validate_transition(StreamExecutionStatus.RUNNING, StreamExecutionStatus.FAILED)
                study_entry = state.studies.setdefault(study_key, StudyState())
                failed_meta = dict(tx_data.get("metadata") or {})
                failed_meta.pop("sdtm_lineage", None)
                failed_meta.pop("sdtm_seq_watermarks", None)
                merged_failed_meta = _merge_preserved_lineage_metadata(prior_meta, failed_meta)
                study_entry.streams[stream_name] = StreamState(
                    last_timestamp=prior_ts,
                    records_processed=tx_data.get("records_processed", 0),
                    last_run_status=StreamExecutionStatus.FAILED,
                    error_message=str(err),
                    metadata=merged_failed_meta,
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
                study_entry = state.studies.setdefault(study_key, StudyState())
                running_stream = study_entry.streams.get(stream_name)
                base_meta = _merge_preserved_lineage_metadata(
                    prior_meta,
                    running_stream.metadata if running_stream else None,
                )
                commit_meta = _merge_preserved_lineage_metadata(
                    base_meta,
                    tx_data.get("metadata") or {},
                )
                study_entry.streams[stream_name] = StreamState(
                    last_timestamp=commit_ts,
                    records_processed=tx_data.get("records_processed", 0),
                    last_run_status=StreamExecutionStatus.SUCCESS,
                    metadata=commit_meta,
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

    def _study_prefix(self, study_key: str) -> str:
        """Return the Airflow Variable key prefix for a specific Study."""
        return f"{self.variable_prefix}:{self._encode_key_part(study_key)}:"

    def _get_variable_key(self, study_key: str, stream_name: str) -> str:
        """Return the deterministic Airflow Variable key bound to Study and stream."""
        return f"{self._study_prefix(study_key)}{self._encode_key_part(stream_name)}"

    def _parse_variable_key(self, key: str) -> tuple[str, str] | None:
        """Decode ``(study_key, stream_name)`` from a prefixed Airflow Variable key."""
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

        if isinstance(raw_val, str):
            try:
                raw_val = json.loads(raw_val)
            except json.JSONDecodeError:
                return None
        return raw_val if isinstance(raw_val, dict) else None

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

    def _list_variable_keys(self, prefix: str) -> list[str]:
        """List Airflow Variable keys matching ``prefix``."""
        if not self._airflow_available():
            return []

        if self._has_active_task_sdk_comms():
            from airflow.sdk import Variable as SdkVariable

            return [str(k) for k in SdkVariable.keys(prefix=prefix)]

        try:
            from airflow.models.variable import Variable as ModelVariable
            from airflow.utils.session import create_session
            from sqlalchemy import select
        except ImportError:
            return []

        self._ensure_orm_session()
        with create_session() as session:
            rows = session.scalars(
                select(ModelVariable.key).where(ModelVariable.key.startswith(prefix))
            ).all()
            return [str(k) for k in rows]

    def _delete_variable_key(self, key: str) -> bool:
        """Delete a single Airflow Variable key, returning True when an entry existed."""
        if not self._airflow_available():
            return False

        if self._has_active_task_sdk_comms():
            from airflow.sdk import Variable as SdkVariable

            if self._read_variable(key) is None:
                return False
            SdkVariable.delete(key)
            return True

        try:
            from airflow.models.variable import Variable as ModelVariable
        except ImportError:
            return False
        self._ensure_orm_session()
        return bool(ModelVariable.delete(key))

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

    def get_last_timestamp(self, study_key: str, stream_name: str) -> datetime | None:
        """Return the high-water mark timestamp for a Study and stream from Airflow Variables."""
        payload = self._read_variable(self._get_variable_key(study_key, stream_name))
        return self._parse_timestamp(payload)

    def get_last_status(self, study_key: str, stream_name: str) -> StreamExecutionStatus | None:
        """Return the execution status for a Study and stream from Airflow Variables."""
        payload = self._read_variable(self._get_variable_key(study_key, stream_name))
        return self._parse_status(payload)

    def get_sdtm_lineage(self, study_key: str, stream_name: str) -> dict[str, Any]:
        """Return persisted SDTM sidecar lineage and sequence watermarks from Airflow Variables."""
        payload = self._read_variable(self._get_variable_key(study_key, stream_name))
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
        var_key = self._get_variable_key(study_key, stream_name)
        existing = self._read_variable(var_key)
        prior_meta = existing.get("metadata") if isinstance(existing, dict) else {}
        incoming: dict[str, Any] = {}
        if lineage_updates:
            incoming["sdtm_lineage"] = dict(lineage_updates)
        if seq_watermarks:
            incoming["sdtm_seq_watermarks"] = dict(seq_watermarks)
        merged_meta = _merge_preserved_lineage_metadata(
            prior_meta if isinstance(prior_meta, Mapping) else None,
            incoming,
        )
        if isinstance(existing, dict):
            updated = dict(existing)
            raw_redacted = redact_sensitive_payload(merged_meta) if merged_meta else {}
            updated["metadata"] = raw_redacted if isinstance(raw_redacted, dict) else {}
            self._write_variable(var_key, updated)
        else:
            self.set_last_timestamp(
                study_key=study_key,
                stream_name=stream_name,
                timestamp=None,
                records_processed=0,
                status=StreamExecutionStatus.SUCCESS,
                metadata=merged_meta,
                force=True,
            )

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
        var_key = self._get_variable_key(study_key, stream_name)
        existing_payload = self._read_variable(var_key)

        if not force:
            current_status = self._parse_status(existing_payload)
            validate_transition(current_status, validated_status)

        normalized_ts = _normalize_optional_utc(timestamp)
        normalized_lease = _normalize_optional_utc(lease_expires_at)
        redacted_error = (
            redact_sensitive_text(str(redact_sensitive_payload(redact_uri(error_message))))
            if error_message is not None
            else None
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
        raw_redacted_meta = redact_sensitive_payload(merged_meta) if merged_meta else {}
        redacted_meta = raw_redacted_meta if isinstance(raw_redacted_meta, dict) else {}

        val: dict[str, Any] = {
            "study_key": study_key,
            "stream_name": stream_name,
            "last_timestamp": normalized_ts.isoformat() if normalized_ts is not None else None,
            "records_processed": records_processed,
            "status": validated_status.value,
            "error_message": redacted_error,
            "metadata": redacted_meta,
            "owner_pid": owner_pid,
            "owner_host": owner_host,
            "lease_expires_at": (
                normalized_lease.isoformat() if normalized_lease is not None else None
            ),
        }
        self._write_variable(var_key, val)

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
        raw_entry = self._read_variable(self._get_variable_key(study_key, stream_name))
        prior_ts = _normalize_optional_utc(self._parse_timestamp(raw_entry))
        prior_meta = (
            copy.deepcopy(raw_entry.get("metadata"))
            if isinstance(raw_entry, dict) and isinstance(raw_entry.get("metadata"), dict)
            else {}
        )
        effective_ts = (
            prior_ts if prior_ts is not None else _normalize_optional_utc(fallback_timestamp)
        )

        current_status = self._parse_status(raw_entry)
        now = datetime.now(UTC)
        if current_status == StreamExecutionStatus.RUNNING and raw_entry is not None:
            raw_pid = raw_entry.get("owner_pid")
            owner_pid_val = (
                raw_pid if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) else None
            )
            raw_host = raw_entry.get("owner_host")
            owner_host_val = raw_host if isinstance(raw_host, str) else None
            with contextlib.suppress(ValueError):
                lease_exp_val = _parse_optional_timestamp(raw_entry.get("lease_expires_at"))
            if "lease_exp_val" not in locals():
                lease_exp_val = None
            stale_reason = _check_stale_running(
                owner_pid_val,
                owner_host_val,
                lease_exp_val,
                now=now,
            )
            if stale_reason is not None:
                validate_transition(current_status, StreamExecutionStatus.FAILED)
                raw_processed = raw_entry.get("records_processed", 0)
                raw_meta = raw_entry.get("metadata")
                self.set_last_timestamp(
                    study_key=study_key,
                    stream_name=stream_name,
                    timestamp=prior_ts,
                    records_processed=(
                        raw_processed
                        if isinstance(raw_processed, int) and not isinstance(raw_processed, bool)
                        else 0
                    ),
                    status=StreamExecutionStatus.FAILED,
                    error_message=f"Recovered stale running stream: {stale_reason}",
                    metadata=raw_meta if isinstance(raw_meta, dict) else None,
                    force=True,
                )
                current_status = StreamExecutionStatus.FAILED

        # Atomic transaction guard: ensure transition to RUNNING is valid
        validate_transition(current_status, StreamExecutionStatus.RUNNING)

        ttl = self.lease_ttl_seconds if lease_ttl_seconds is None else lease_ttl_seconds
        self.set_last_timestamp(
            study_key=study_key,
            stream_name=stream_name,
            timestamp=prior_ts,
            records_processed=0,
            status=StreamExecutionStatus.RUNNING,
            force=True,
            owner_pid=os.getpid(),
            owner_host=socket.gethostname(),
            lease_expires_at=now + timedelta(seconds=ttl),
        )

        tx_data: dict[str, Any] = {
            "last_timestamp": effective_ts,
            "new_timestamp": None,
            "records_processed": 0,
            "metadata": {},
        }

        try:
            yield tx_data
        except BaseException as err:
            if isinstance(err, GeneratorExit):
                raise
            validate_transition(StreamExecutionStatus.RUNNING, StreamExecutionStatus.FAILED)
            failed_meta = dict(tx_data.get("metadata") or {})
            failed_meta.pop("sdtm_lineage", None)
            failed_meta.pop("sdtm_seq_watermarks", None)
            merged_failed_meta = _merge_preserved_lineage_metadata(prior_meta, failed_meta)
            self.set_last_timestamp(
                study_key=study_key,
                stream_name=stream_name,
                timestamp=prior_ts,
                records_processed=tx_data.get("records_processed", 0),
                status=StreamExecutionStatus.FAILED,
                error_message=str(err),
                metadata=merged_failed_meta,
                force=True,
                _preserve_existing_lineage=False,
            )
            raise
        else:
            validate_transition(StreamExecutionStatus.RUNNING, StreamExecutionStatus.SUCCESS)
            raw_new_ts = tx_data.get("new_timestamp")
            commit_ts = _normalize_optional_utc(raw_new_ts) if raw_new_ts is not None else prior_ts
            self.set_last_timestamp(
                study_key=study_key,
                stream_name=stream_name,
                timestamp=commit_ts,
                records_processed=tx_data.get("records_processed", 0),
                status=StreamExecutionStatus.SUCCESS,
                metadata=tx_data.get("metadata"),
                force=True,
            )

    def delete_entry(self, study_key: str, stream_name: str | None = None) -> bool:
        """Delete a Study or specific stream checkpoint from Airflow Variables."""
        if stream_name is not None:
            return self._delete_variable_key(self._get_variable_key(study_key, stream_name))

        keys = self._list_variable_keys(self._study_prefix(study_key))
        deleted_any = False
        for key in keys:
            if self._delete_variable_key(key):
                deleted_any = True
        return deleted_any

    def read_state(self) -> LedgerState:
        """Read and validate the full ledger state across all Study checkpoints."""
        state = LedgerState()
        keys = self._list_variable_keys(f"{self.variable_prefix}:")
        for key in sorted(keys):
            payload = self._read_variable(key)
            if not payload:
                continue

            parsed_key = self._parse_variable_key(key)
            s_key = payload.get("study_key") or (parsed_key[0] if parsed_key else None)
            s_name = payload.get("stream_name") or (parsed_key[1] if parsed_key else None)
            ts = self._parse_timestamp(payload)
            if not isinstance(s_key, str) or not isinstance(s_name, str):
                continue
            if ts is None and "status" not in payload:
                continue

            raw_pid = payload.get("owner_pid")
            owner_pid_val = (
                raw_pid if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) else None
            )
            raw_host = payload.get("owner_host")
            owner_host_val = raw_host if isinstance(raw_host, str) else None
            lease_exp_val: datetime | None = None
            with contextlib.suppress(ValueError):
                lease_exp_val = _parse_optional_timestamp(payload.get("lease_expires_at"))

            status = self._parse_status(payload) or StreamExecutionStatus.SUCCESS
            raw_processed = payload.get("records_processed", 0)
            records_processed = (
                raw_processed
                if isinstance(raw_processed, int) and not isinstance(raw_processed, bool)
                else 0
            )
            study = state.studies.setdefault(s_key, StudyState())
            study.streams[s_name] = StreamState(
                last_timestamp=ts,
                records_processed=records_processed,
                last_run_status=status,
                error_message=payload.get("error_message"),
                metadata=payload.get("metadata") or {},
                owner_pid=owner_pid_val,
                owner_host=owner_host_val,
                lease_expires_at=lease_exp_val,
            )
        return state


def get_state_provider(
    ledger_path: str = DEFAULT_LEDGER_PATH,
) -> BaseStateProvider:
    """Factory to get the appropriate state provider based on environment."""
    if "AIRFLOW_CTX_TASK_ID" in os.environ or os.environ.get("USE_AIRFLOW_STATE_PROVIDER") == "1":
        return AirflowStateProvider()
    if ledger_path == DEFAULT_LEDGER_PATH and "IMEDNET_WORKFLOW_LEDGER_PATH" in os.environ:
        ledger_path = os.environ["IMEDNET_WORKFLOW_LEDGER_PATH"]
    return FileStateProvider(ledger_path)
