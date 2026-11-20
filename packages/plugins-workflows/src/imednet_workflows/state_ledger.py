"""Stateful incremental high-water mark tracker for workflow streams."""

from __future__ import annotations

import abc
import contextlib
import json
import os
import socket
import sys
import tempfile
from collections.abc import Generator
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

            if not force:
                current_stream = study.streams.get(stream_name)
                current_status = current_stream.last_run_status if current_stream else None
                validate_transition(current_status, validated_status)

            normalized_ts = _normalize_optional_utc(timestamp)

            study.streams[stream_name] = StreamState(
                last_timestamp=normalized_ts,
                records_processed=records_processed,
                last_run_status=validated_status,
                error_message=error_message,
                metadata=metadata or {},
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
                        metadata=dict(current_stream.metadata),
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
            # Mark stream as failed and preserve previous committed high-water mark
            with self._lock():
                state = self.read_state()
                validate_transition(StreamExecutionStatus.RUNNING, StreamExecutionStatus.FAILED)
                study_entry = state.studies.setdefault(study_key, StudyState())
                study_entry.streams[stream_name] = StreamState(
                    last_timestamp=prior_ts,
                    records_processed=tx_data.get("records_processed", 0),
                    last_run_status=StreamExecutionStatus.FAILED,
                    error_message=str(err),
                    metadata=tx_data.get("metadata") or {},
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
                study_entry.streams[stream_name] = StreamState(
                    last_timestamp=commit_ts,
                    records_processed=tx_data.get("records_processed", 0),
                    last_run_status=StreamExecutionStatus.SUCCESS,
                    metadata=tx_data.get("metadata") or {},
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
    """Manages transactional state bookmarks using Airflow 3 ``Variable`` JSON state."""

    VARIABLE_PREFIX: str = "imednet_state__"

    def __init__(self, *, lease_ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS) -> None:
        """Initialize the Airflow 3 Variable state provider."""
        self.lease_ttl_seconds = lease_ttl_seconds

    def _get_variable_key(self, study_key: str, stream_name: str) -> str:
        """Build the Airflow Variable key for ``(study_key, stream_name)``."""
        return f"{self.VARIABLE_PREFIX}{study_key}__{stream_name}"

    @staticmethod
    def _is_airflow_disabled() -> bool:
        """Return ``True`` when Airflow has been explicitly disabled in ``sys.modules``."""
        return "airflow" in sys.modules and sys.modules["airflow"] is None

    @staticmethod
    def _ensure_orm_session() -> None:
        """Ensure ``airflow.settings.Session`` is configured before direct ORM Variable calls."""
        import airflow.settings

        if getattr(airflow.settings, "Session", None) is None:
            configure_fn: Any = getattr(airflow.settings, "configure_orm", None)
            if callable(configure_fn):
                configure_fn()

    @staticmethod
    def _is_task_sdk_runtime() -> bool:
        """Return ``True`` when executing inside an Airflow 3 Task SDK worker."""
        task_runner = sys.modules.get("airflow.sdk.execution_time.task_runner")
        return (
            task_runner is not None and getattr(task_runner, "SUPERVISOR_COMMS", None) is not None
        )

    def _get_variable_payload(self, key: str) -> dict[str, Any] | None:
        """Read and deserialize a JSON state payload from Airflow ``Variable``."""
        if self._is_airflow_disabled():
            return None
        try:
            if self._is_task_sdk_runtime():
                from airflow.sdk import Variable as SdkVariable

                raw: Any = SdkVariable.get(key, default=None, deserialize_json=True)
            else:
                from airflow.models import Variable as OrmVariable

                self._ensure_orm_session()
                raw = OrmVariable.get(key, default_var=None, deserialize_json=True)
        except (ImportError, ModuleNotFoundError, json.JSONDecodeError):
            return None

        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                return None
        if isinstance(raw, dict):
            return dict(raw)
        return None

    def _set_variable_payload(self, key: str, payload: dict[str, Any]) -> None:
        """Serialize and write a JSON state payload to Airflow ``Variable``."""
        if self._is_airflow_disabled():
            return
        try:
            if self._is_task_sdk_runtime():
                from airflow.sdk import Variable as SdkVariable

                SdkVariable.set(key, payload, serialize_json=True)
            else:
                from airflow.models import Variable as OrmVariable

                self._ensure_orm_session()
                OrmVariable.set(key, payload, serialize_json=True)
        except (ImportError, ModuleNotFoundError):
            return

    def _delete_variable_key(self, key: str) -> bool:
        """Delete a single Airflow ``Variable`` key if present."""
        if self._is_airflow_disabled():
            return False
        try:
            if self._is_task_sdk_runtime():
                from airflow.sdk import Variable as SdkVariable

                if SdkVariable.get(key, default=None) is None:
                    return False
                SdkVariable.delete(key)
                return True

            from airflow.models import Variable as OrmVariable

            self._ensure_orm_session()
            deleted = OrmVariable.delete(key)
            return bool(deleted and deleted > 0)
        except (ImportError, ModuleNotFoundError):
            return False

    def _list_variable_keys(self, prefix: str) -> list[str]:
        """List Airflow ``Variable`` keys matching ``prefix`` without wildcard collisions."""
        if self._is_airflow_disabled():
            return []
        try:
            if self._is_task_sdk_runtime():
                from airflow.sdk import Variable as SdkVariable

                return [
                    str(key)
                    for key in SdkVariable.keys(prefix=prefix)
                    if isinstance(key, str) and key.startswith(prefix)
                ]

            from airflow.models import Variable as OrmVariable
            from airflow.utils.session import create_session
            from sqlalchemy import select

            self._ensure_orm_session()
            with create_session() as session:
                rows = session.scalars(
                    select(OrmVariable.key).where(OrmVariable.key.like("imednet%"))
                ).all()
            return [str(key) for key in rows if isinstance(key, str) and key.startswith(prefix)]
        except (ImportError, ModuleNotFoundError):
            return []

    def _resolve_entry_identity(
        self,
        key: str,
        payload: dict[str, Any] | None,
    ) -> tuple[str, str] | None:
        """Extract ``(study_key, stream_name)`` from payload metadata or Variable key."""
        if payload is not None:
            s_key = payload.get("study_key")
            s_name = payload.get("stream_name")
            if isinstance(s_key, str) and s_key and isinstance(s_name, str) and s_name:
                return s_key, s_name
        if not key.startswith(self.VARIABLE_PREFIX):
            return None
        remainder = key[len(self.VARIABLE_PREFIX) :]
        parts = remainder.split("__", 1)
        if len(parts) == 2 and parts[0] and parts[1]:
            return parts[0], parts[1]
        return None

    @staticmethod
    def _safe_parse_timestamp(raw: Any) -> datetime | None:
        """Parse an optional timestamp safely, returning ``None`` on invalid strings."""
        try:
            return _parse_optional_timestamp(raw)
        except ValueError:
            return None

    def get_last_timestamp(self, study_key: str, stream_name: str) -> datetime | None:
        """Return the high-water mark timestamp from Airflow ``Variable`` state."""
        payload = self._get_variable_payload(self._get_variable_key(study_key, stream_name))
        if not payload:
            return None
        return self._safe_parse_timestamp(payload.get("last_timestamp"))

    def get_last_status(self, study_key: str, stream_name: str) -> StreamExecutionStatus | None:
        """Return the last execution status from Airflow ``Variable`` state."""
        payload = self._get_variable_payload(self._get_variable_key(study_key, stream_name))
        if not payload:
            return None
        raw_status = payload.get("last_run_status", payload.get("status"))
        if raw_status is None:
            return None
        if isinstance(raw_status, StreamExecutionStatus):
            return raw_status
        if isinstance(raw_status, str):
            try:
                return StreamExecutionStatus(raw_status.lower())
            except ValueError:
                return StreamExecutionStatus.FAILED
        return StreamExecutionStatus.FAILED

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
    ) -> None:
        """Persist the high-water mark checkpoint using Airflow ``Variable`` JSON state."""
        validated_status = _coerce_and_validate_status(status)
        if not force:
            previous_status = self.get_last_status(study_key, stream_name)
            validate_transition(previous_status, validated_status)

        normalized_ts = _normalize_optional_utc(timestamp)
        normalized_lease = _normalize_optional_utc(lease_expires_at)
        redacted_error = (
            redact_sensitive_text(redact_uri(error_message)) if error_message is not None else None
        )
        redacted_meta_raw = redact_sensitive_payload(metadata or {})
        redacted_meta = dict(redacted_meta_raw) if isinstance(redacted_meta_raw, dict) else {}

        payload: dict[str, Any] = {
            "study_key": study_key,
            "stream_name": stream_name,
            "last_timestamp": normalized_ts.isoformat() if normalized_ts is not None else None,
            "records_processed": int(records_processed),
            "status": validated_status.value,
            "last_run_status": validated_status.value,
            "error_message": redacted_error,
            "metadata": redacted_meta,
            "owner_pid": owner_pid,
            "owner_host": owner_host,
            "lease_expires_at": (
                normalized_lease.isoformat() if normalized_lease is not None else None
            ),
        }
        self._set_variable_payload(self._get_variable_key(study_key, stream_name), payload)

    @contextlib.contextmanager
    def transaction(
        self,
        study_key: str,
        stream_name: str,
        fallback_timestamp: datetime | None = None,
        *,
        lease_ttl_seconds: float | None = None,
    ) -> Generator[dict[str, Any], None, None]:
        """Context manager for transactional state tracking backed by Airflow ``Variable``."""
        prior_ts = _normalize_optional_utc(self.get_last_timestamp(study_key, stream_name))
        effective_ts = (
            prior_ts if prior_ts is not None else _normalize_optional_utc(fallback_timestamp)
        )

        current_status = self.get_last_status(study_key, stream_name)
        now = datetime.now(UTC)
        if current_status == StreamExecutionStatus.RUNNING:
            raw_entry = (
                self._get_variable_payload(self._get_variable_key(study_key, stream_name)) or {}
            )
            raw_pid = raw_entry.get("owner_pid")
            owner_pid_val = (
                raw_pid if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) else None
            )
            raw_host = raw_entry.get("owner_host")
            owner_host_val = raw_host if isinstance(raw_host, str) else None
            lease_exp_val = self._safe_parse_timestamp(raw_entry.get("lease_expires_at"))
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
            self.set_last_timestamp(
                study_key=study_key,
                stream_name=stream_name,
                timestamp=prior_ts,
                records_processed=int(tx_data.get("records_processed", 0)),
                status=StreamExecutionStatus.FAILED,
                error_message=str(err),
                metadata=tx_data.get("metadata"),
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
                records_processed=int(tx_data.get("records_processed", 0)),
                status=StreamExecutionStatus.SUCCESS,
                metadata=tx_data.get("metadata"),
            )

    def delete_entry(self, study_key: str, stream_name: str | None = None) -> bool:
        """Delete a study or specific stream entry from Airflow ``Variable`` state."""
        if stream_name is not None:
            return self._delete_variable_key(self._get_variable_key(study_key, stream_name))

        study_prefix = f"{self.VARIABLE_PREFIX}{study_key}__"
        removed = False
        for key in self._list_variable_keys(study_prefix):
            payload = self._get_variable_payload(key)
            identity = self._resolve_entry_identity(key, payload)
            if identity is not None and identity[0] != study_key:
                continue
            if self._delete_variable_key(key):
                removed = True
        return removed

    def read_state(self) -> LedgerState:
        """Read and validate the full ledger state from Airflow ``Variable`` entries."""
        state = LedgerState()
        for key in sorted(self._list_variable_keys(self.VARIABLE_PREFIX)):
            payload = self._get_variable_payload(key)
            if not payload:
                continue
            identity = self._resolve_entry_identity(key, payload)
            if identity is None:
                continue
            has_state_fields = (
                "last_timestamp" in payload or "last_run_status" in payload or "status" in payload
            )
            if not has_state_fields:
                continue
            raw_ts = payload.get("last_timestamp")
            if raw_ts is None:
                parsed_ts = None
            elif isinstance(raw_ts, str):
                parsed_ts = self._safe_parse_timestamp(raw_ts)
                if parsed_ts is None:
                    continue
            elif isinstance(raw_ts, datetime):
                parsed_ts = _normalize_optional_utc(raw_ts)
            else:
                continue

            raw_pid = payload.get("owner_pid")
            owner_pid_val = (
                raw_pid if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) else None
            )
            raw_host = payload.get("owner_host")
            owner_host_val = raw_host if isinstance(raw_host, str) else None
            lease_exp_val = self._safe_parse_timestamp(payload.get("lease_expires_at"))

            s_key, s_name = identity
            study = state.studies.setdefault(s_key, StudyState())
            raw_meta = payload.get("metadata")
            raw_processed = payload.get("records_processed", 0)
            study.streams[s_name] = StreamState(
                last_timestamp=parsed_ts,
                records_processed=(
                    int(raw_processed)
                    if isinstance(raw_processed, int) and not isinstance(raw_processed, bool)
                    else 0
                ),
                last_run_status=payload.get("last_run_status", payload.get("status", "success")),
                error_message=payload.get("error_message"),
                metadata=dict(raw_meta) if isinstance(raw_meta, dict) else {},
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
