"""Stateful incremental high-water mark tracker for workflow streams."""

from __future__ import annotations

import abc
import contextlib
import importlib
import json
import os
import socket
import sys
import tempfile
import uuid
from collections.abc import Generator, Mapping
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, TypeGuard

from pydantic import BaseModel, Field, field_validator

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
    """Manages transactional state using Airflow XCom metadata."""

    def __init__(self, *, lease_ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS) -> None:
        """Initialize the Airflow XCom state provider."""
        self.lease_ttl_seconds = lease_ttl_seconds

    def _get_xcom_key(self, study_key: str, stream_name: str) -> str:
        return f"state_{study_key}_{stream_name}"

    @staticmethod
    def _parse_iso_utc(raw_value: Any) -> datetime | None:
        with contextlib.suppress(ValueError):
            return _parse_optional_timestamp(raw_value)
        return None

    @staticmethod
    def _parse_status(raw_status: Any) -> StreamExecutionStatus:
        if isinstance(raw_status, StreamExecutionStatus):
            return raw_status
        if isinstance(raw_status, str):
            try:
                return StreamExecutionStatus(raw_status.lower())
            except ValueError:
                return StreamExecutionStatus.FAILED
        return StreamExecutionStatus.FAILED

    @staticmethod
    def _extract_ti_from_context(context: Any) -> Any | None:
        if isinstance(context, Mapping):
            return context.get("ti")
        if hasattr(context, "__getitem__"):
            with contextlib.suppress(Exception):
                return context["ti"]
        return None

    @classmethod
    def _get_active_ti(cls) -> tuple[bool, Any | None]:
        """Resolve the active Airflow TaskInstance when executing inside a task context."""
        sdk_missing = False
        try:
            from airflow.sdk import get_current_context as sdk_get_current_context

            try:
                ti = cls._extract_ti_from_context(sdk_get_current_context())
                if ti is not None:
                    return True, ti
            except Exception:  # noqa: S110
                pass
        except ImportError:
            sdk_missing = True

        legacy_mod = sys.modules.get("airflow.operators.python")
        if legacy_mod is not None or sdk_missing:
            try:
                legacy_get_ctx: Any = None
                if legacy_mod is not None:
                    legacy_get_ctx = getattr(legacy_mod, "get_current_context", None)
                else:
                    from airflow.operators.python import (
                        get_current_context as _legacy_get_ctx,
                    )

                    legacy_get_ctx = _legacy_get_ctx
                if callable(legacy_get_ctx):
                    ti = cls._extract_ti_from_context(legacy_get_ctx())
                    if ti is not None:
                        return True, ti
            except Exception:  # noqa: S110
                pass

        return False, None

    @classmethod
    def _pull_ti_xcom_dict(cls, ti: Any, key: str) -> dict[str, Any] | None:
        """Pull the most recent task-scoped XCom payload for ``ti``."""
        task_id = getattr(ti, "task_id", None)
        map_index = getattr(ti, "map_index", None)
        if not isinstance(task_id, str) or not task_id:
            val = ti.xcom_pull(key=key, include_prior_dates=True)
        elif type(map_index) is int:
            try:
                val = ti.xcom_pull(
                    task_ids=task_id,
                    key=key,
                    include_prior_dates=True,
                    map_indexes=map_index,
                )
            except TypeError:
                try:
                    val = ti.xcom_pull(
                        task_ids=task_id,
                        key=key,
                        include_prior_dates=True,
                    )
                except TypeError:
                    val = ti.xcom_pull(key=key, include_prior_dates=True)
        else:
            try:
                val = ti.xcom_pull(
                    task_ids=task_id,
                    key=key,
                    include_prior_dates=True,
                )
            except TypeError:
                val = ti.xcom_pull(key=key, include_prior_dates=True)

        if isinstance(val, dict):
            return val
        if isinstance(val, (list, tuple)):
            dicts = [item for item in val if isinstance(item, dict)]
            if not dicts:
                return None
            return max(
                dicts,
                key=lambda item: (
                    cls._parse_iso_utc(item.get("last_timestamp"))
                    or datetime.min.replace(tzinfo=UTC)
                ),
            )
        return None

    @staticmethod
    def _is_orm_model_class(candidate: object) -> TypeGuard[Any]:
        return isinstance(candidate, type)

    @staticmethod
    def _deserialize_xcom_row(xcom_model_cls: Any, row: Any) -> dict[str, Any] | None:
        """Decode an ``XComModel`` ORM row payload into a state dictionary."""
        raw_val = getattr(row, "value", None)
        if isinstance(raw_val, dict):
            return raw_val
        deserialize = getattr(xcom_model_cls, "deserialize_value", None)
        if callable(deserialize):
            try:
                decoded = deserialize(row)
                if isinstance(decoded, dict):
                    return decoded
            except Exception:  # noqa: S110
                pass
        if isinstance(raw_val, (bytes, bytearray)):
            raw_val = raw_val.decode("utf-8", errors="ignore")
        if isinstance(raw_val, str):
            try:
                parsed = json.loads(raw_val)
                if isinstance(parsed, dict):
                    return parsed
            except ValueError:
                pass
        return None

    def _query_latest_xcom_dict(self, study_key: str, stream_name: str) -> dict[str, Any] | None:
        """Query the latest state payload from ``XComModel`` outside task execution."""
        try:
            xcom_mod = importlib.import_module("airflow.models.xcom")
            session_mod = importlib.import_module("airflow.utils.session")
            from sqlalchemy import select
        except ImportError:
            return None

        key = self._get_xcom_key(study_key, stream_name)
        xcom_model_cls = getattr(xcom_mod, "XComModel", None)
        if self._is_orm_model_class(xcom_model_cls):
            with session_mod.create_session() as session:
                row = session.scalars(
                    select(xcom_model_cls)
                    .where(xcom_model_cls.key == key)
                    .order_by(xcom_model_cls.timestamp.desc())
                    .limit(1)
                ).first()
                if row is None:
                    return None
                return self._deserialize_xcom_row(xcom_model_cls, row)

        with contextlib.suppress(Exception):
            legacy_xcom = getattr(xcom_mod, "XCom", None)
            provide_session = getattr(session_mod, "provide_session", None)
            if legacy_xcom is not None and callable(provide_session):

                @provide_session  # type: ignore[misc,untyped-decorator]
                def _get_xcom(session: Any = None) -> Any:
                    return (
                        session.query(legacy_xcom)
                        .filter(legacy_xcom.key == key)
                        .order_by(legacy_xcom.timestamp.desc())
                        .first()
                    )

                xcom_obj = _get_xcom()
                if xcom_obj is not None:
                    return self._deserialize_xcom_row(legacy_xcom, xcom_obj)
        return None

    def _pull_xcom_dict(self, study_key: str, stream_name: str) -> dict[str, Any] | None:
        """Pull the latest XCom state dictionary from active task context or metadata DB."""
        in_context, ti = self._get_active_ti()
        if in_context:
            return self._pull_ti_xcom_dict(ti, self._get_xcom_key(study_key, stream_name))
        return self._query_latest_xcom_dict(study_key, stream_name)

    @staticmethod
    def _ensure_manual_task_instance(
        session: Any,
        *,
        dag_id: str,
        task_id: str,
        run_id: str,
        logical_date: datetime,
    ) -> None:
        """Ensure prerequisite ``DagRun`` and ``TaskInstance`` rows exist for manual XCom writes."""
        from airflow.models.dagrun import DagRun
        from airflow.models.taskinstance import TaskInstance
        from airflow.utils.state import DagRunState, TaskInstanceState
        from airflow.utils.types import DagRunType
        from sqlalchemy import insert, select

        existing_dr_id = session.scalar(
            select(DagRun.id).where(DagRun.dag_id == dag_id, DagRun.run_id == run_id)
        )
        if existing_dr_id is None:
            session.add(
                DagRun(
                    dag_id=dag_id,
                    run_id=run_id,
                    run_type=DagRunType.MANUAL,
                    logical_date=logical_date,
                    run_after=logical_date,
                    state=DagRunState.SUCCESS,
                )
            )
            session.flush()

        existing_ti = session.scalar(
            select(TaskInstance.task_id).where(
                TaskInstance.dag_id == dag_id,
                TaskInstance.task_id == task_id,
                TaskInstance.run_id == run_id,
                TaskInstance.map_index == -1,
            )
        )
        if existing_ti is None:
            session.execute(
                insert(TaskInstance).values(
                    task_id=task_id,
                    dag_id=dag_id,
                    run_id=run_id,
                    map_index=-1,
                    pool="default_pool",
                    pool_slots=1,
                    state=TaskInstanceState.SUCCESS,
                )
            )
            session.flush()

    def get_last_timestamp(self, study_key: str, stream_name: str) -> datetime | None:
        """Returns the high-water mark timestamp from Airflow XCom."""
        val = self._pull_xcom_dict(study_key, stream_name)
        if val is not None:
            return self._parse_iso_utc(val.get("last_timestamp"))
        return None

    def get_last_status(self, study_key: str, stream_name: str) -> StreamExecutionStatus | None:
        """Returns the execution status from Airflow XCom."""
        val = self._pull_xcom_dict(study_key, stream_name)
        if val is not None and "status" in val:
            return self._parse_status(val["status"])
        return None

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
        """Sets the high-water mark timestamp atomically using Airflow XCom."""
        _ = force
        validated_status = _coerce_and_validate_status(status)
        normalized_ts = _normalize_optional_utc(timestamp)
        normalized_lease = _normalize_optional_utc(lease_expires_at)

        val: dict[str, Any] = {
            "study_key": study_key,
            "stream_name": stream_name,
            "last_timestamp": normalized_ts.isoformat() if normalized_ts is not None else None,
            "records_processed": records_processed,
            "status": validated_status.value,
            "error_message": error_message,
            "metadata": metadata or {},
            "owner_pid": owner_pid,
            "owner_host": owner_host,
            "lease_expires_at": (
                normalized_lease.isoformat() if normalized_lease is not None else None
            ),
        }

        in_context, ti = self._get_active_ti()
        if in_context and ti is not None:
            ti.xcom_push(key=self._get_xcom_key(study_key, stream_name), value=val)
            return

        # Out-of-context persistence via Airflow 3 XComModel
        try:
            xcom_mod = importlib.import_module("airflow.models.xcom")
            session_mod = importlib.import_module("airflow.utils.session")
        except ImportError:
            return

        now = datetime.now(UTC)
        dag_id = os.environ.get("AIRFLOW_CTX_DAG_ID", "manual_state_sync")
        task_id = os.environ.get("AIRFLOW_CTX_TASK_ID", "manual_state_sync")
        run_id = (
            os.environ.get("AIRFLOW_CTX_DAG_RUN_ID")
            or f"manual__{now.isoformat()}_{uuid.uuid4().hex[:8]}"
        )

        xcom_model_cls = getattr(xcom_mod, "XComModel", None)
        if self._is_orm_model_class(xcom_model_cls):
            with session_mod.create_session() as session:
                self._ensure_manual_task_instance(
                    session,
                    dag_id=dag_id,
                    task_id=task_id,
                    run_id=run_id,
                    logical_date=now,
                )
                xcom_model_cls.set(
                    key=self._get_xcom_key(study_key, stream_name),
                    value=val,
                    dag_id=dag_id,
                    task_id=task_id,
                    run_id=run_id,
                    map_index=-1,
                    session=session,
                )
            return

        legacy_xcom = getattr(xcom_mod, "XCom", None)
        if legacy_xcom is not None:
            exec_date_str = os.environ.get("AIRFLOW_CTX_EXECUTION_DATE")
            exec_date = datetime.fromisoformat(exec_date_str) if exec_date_str else now
            legacy_xcom.set(
                key=self._get_xcom_key(study_key, stream_name),
                value=val,
                task_id=task_id,
                dag_id=dag_id,
                execution_date=exec_date,
            )

    @contextlib.contextmanager
    def transaction(
        self,
        study_key: str,
        stream_name: str,
        fallback_timestamp: datetime | None = None,
        *,
        lease_ttl_seconds: float | None = None,
    ) -> Generator[dict[str, Any], None, None]:
        """Context manager for transactional state tracking using Airflow XCom."""
        prior_ts = _normalize_optional_utc(self.get_last_timestamp(study_key, stream_name))
        effective_ts = (
            prior_ts if prior_ts is not None else _normalize_optional_utc(fallback_timestamp)
        )

        current_status = self.get_last_status(study_key, stream_name)
        now = datetime.now(UTC)
        if current_status == StreamExecutionStatus.RUNNING:
            raw_entry = self._pull_xcom_dict(study_key, stream_name) or {}
            raw_pid = raw_entry.get("owner_pid")
            owner_pid_val = (
                raw_pid if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) else None
            )
            raw_host = raw_entry.get("owner_host")
            owner_host_val = raw_host if isinstance(raw_host, str) else None
            lease_exp_val = _parse_optional_timestamp(raw_entry.get("lease_expires_at"))
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
                records_processed=tx_data.get("records_processed", 0),
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
                records_processed=tx_data.get("records_processed", 0),
                status=StreamExecutionStatus.SUCCESS,
                metadata=tx_data.get("metadata"),
            )

    def delete_entry(self, study_key: str, stream_name: str | None = None) -> bool:
        """Deletes a study or specific stream entry from XComModel."""
        try:
            xcom_mod = importlib.import_module("airflow.models.xcom")
            session_mod = importlib.import_module("airflow.utils.session")
            from sqlalchemy import delete, select
        except ImportError:
            return False

        xcom_model_cls = getattr(xcom_mod, "XComModel", None)
        if self._is_orm_model_class(xcom_model_cls):
            with session_mod.create_session() as session:
                if stream_name:
                    result = session.execute(
                        delete(xcom_model_cls).where(
                            xcom_model_cls.key == self._get_xcom_key(study_key, stream_name)
                        )
                    )
                    return int(getattr(result, "rowcount", 0) or 0) > 0

                rows = session.scalars(
                    select(xcom_model_cls).where(xcom_model_cls.key.like("state_%"))
                ).all()
                prefix = f"state_{study_key}_"
                matching_keys = set()
                for row in rows:
                    payload = self._deserialize_xcom_row(xcom_model_cls, row)
                    if isinstance(payload, dict) and "study_key" in payload:
                        if payload["study_key"] == study_key:
                            matching_keys.add(row.key)
                    elif isinstance(row.key, str) and row.key.startswith(prefix):
                        matching_keys.add(row.key)

                if not matching_keys:
                    return False

                result = session.execute(
                    delete(xcom_model_cls).where(xcom_model_cls.key.in_(matching_keys))
                )
                return int(getattr(result, "rowcount", 0) or 0) > 0

        legacy_xcom = getattr(xcom_mod, "XCom", None)
        provide_session = getattr(session_mod, "provide_session", None)
        if legacy_xcom is None or not callable(provide_session):
            return False

        @provide_session  # type: ignore[misc,untyped-decorator]
        def _delete_xcom(session: Any = None) -> bool:
            query = session.query(legacy_xcom)
            if stream_name:
                query = query.filter(legacy_xcom.key == self._get_xcom_key(study_key, stream_name))
            else:
                query = query.filter(legacy_xcom.key.like(f"state_{study_key}_%"))
            deleted = query.delete(synchronize_session=False)
            return bool(deleted > 0)

        return bool(_delete_xcom())

    def _populate_ledger_from_rows(
        self,
        state: LedgerState,
        xcom_cls: Any,
        rows: Any,
    ) -> None:
        processed_keys: set[str] = set()
        for row in rows:
            row_key = getattr(row, "key", None)
            if not isinstance(row_key, str) or row_key in processed_keys:
                continue
            processed_keys.add(row_key)

            val = self._deserialize_xcom_row(xcom_cls, row)
            if not isinstance(val, dict) or ("last_timestamp" not in val and "status" not in val):
                continue

            s_key: str | None = str(val["study_key"]) if "study_key" in val else None
            s_name: str | None = str(val["stream_name"]) if "stream_name" in val else None
            if not s_key or not s_name:
                parts = row_key.split("_", 2)
                if len(parts) == 3 and parts[0] == "state":
                    s_key = parts[1]
                    s_name = parts[2]

            if not s_key or not s_name:
                continue

            raw_ts = val.get("last_timestamp")
            ts = self._parse_iso_utc(raw_ts)
            if raw_ts is not None and ts is None and "status" not in val:
                continue

            raw_pid = val.get("owner_pid")
            owner_pid_val = (
                raw_pid if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) else None
            )
            raw_host = val.get("owner_host")
            owner_host_val = raw_host if isinstance(raw_host, str) else None
            lease_exp_val = self._parse_iso_utc(val.get("lease_expires_at"))

            study = state.studies.setdefault(s_key, StudyState())
            study.streams[s_name] = StreamState(
                last_timestamp=ts,
                records_processed=int(val.get("records_processed", 0)),
                last_run_status=self._parse_status(val.get("status", "success")),
                error_message=val.get("error_message"),
                metadata=val.get("metadata") or {},
                owner_pid=owner_pid_val,
                owner_host=owner_host_val,
                lease_expires_at=lease_exp_val,
            )

    def read_state(self) -> LedgerState:
        """Reads and validates the current full state from XComModel for CLI display."""
        state = LedgerState()
        try:
            xcom_mod = importlib.import_module("airflow.models.xcom")
            session_mod = importlib.import_module("airflow.utils.session")
            from sqlalchemy import select
        except ImportError:
            return state

        xcom_model_cls = getattr(xcom_mod, "XComModel", None)
        if self._is_orm_model_class(xcom_model_cls):
            with session_mod.create_session() as session:
                xcoms = session.scalars(
                    select(xcom_model_cls)
                    .where(xcom_model_cls.key.like("state_%"))
                    .order_by(xcom_model_cls.timestamp.desc())
                ).all()
                self._populate_ledger_from_rows(state, xcom_model_cls, xcoms)
            return state

        legacy_xcom = getattr(xcom_mod, "XCom", None)
        provide_session = getattr(session_mod, "provide_session", None)
        if legacy_xcom is not None and callable(provide_session):

            @provide_session  # type: ignore[misc,untyped-decorator]
            def _get_all_xcoms(session: Any = None) -> Any:
                return (
                    session.query(legacy_xcom)
                    .filter(legacy_xcom.key.like("state_%"))
                    .order_by(legacy_xcom.timestamp.desc())
                    .all()
                )

            self._populate_ledger_from_rows(state, legacy_xcom, _get_all_xcoms())
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
