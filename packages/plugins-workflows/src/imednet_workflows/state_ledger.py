"""Stateful incremental high-water mark tracker for workflow streams."""

from __future__ import annotations

import abc
import contextlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Generator
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

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

    last_timestamp: datetime
    records_processed: int = 0
    last_run_status: StreamExecutionStatus = StreamExecutionStatus.SUCCESS
    error_message: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

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
        timestamp: datetime,
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


class FileStateProvider(BaseStateProvider):
    """Manages transactional state bookmarks per study using a local JSON file."""

    def __init__(self, ledger_path: str = DEFAULT_LEDGER_PATH) -> None:
        """Initialize the extraction state ledger.

        Args:
            ledger_path: Path to the JSON file where state is persisted.
        """
        if ledger_path == DEFAULT_LEDGER_PATH and "IMEDNET_WORKFLOW_LEDGER_PATH" in os.environ:
            ledger_path = os.environ["IMEDNET_WORKFLOW_LEDGER_PATH"]
        self.ledger_path = Path(ledger_path)
        self._lock_path = self.ledger_path.with_suffix(".lock")

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
            # Ensure return datetime is timezone-aware
            ts = stream.last_timestamp
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            return ts

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
        timestamp: datetime,
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

            # Ensure timezone-aware datetime
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)

            study.streams[stream_name] = StreamState(
                last_timestamp=timestamp,
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
    ) -> Generator[dict[str, Any], None, None]:
        """Context manager for transactional state tracking.

        Yields a dict where user can record 'records_processed', 'new_timestamp', and 'metadata'.
        Saves automatically upon exiting the context with no exceptions.
        The ledger file lock is held for the entire duration of the context.
        """
        with self._lock():
            state = self.read_state()
            study = state.studies.setdefault(study_key, StudyState())
            current_stream = study.streams.get(stream_name)
            current_status = current_stream.last_run_status if current_stream else None

            last_ts: datetime | None = None
            if current_stream:
                last_ts = current_stream.last_timestamp
                if last_ts.tzinfo is None:
                    last_ts = last_ts.replace(tzinfo=UTC)
            if last_ts is None:
                last_ts = fallback_timestamp
            if last_ts is not None and last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=UTC)

            # Atomic transaction guard: ensure transition to RUNNING is valid
            validate_transition(current_status, StreamExecutionStatus.RUNNING)

            running_ts = last_ts or datetime.now(UTC)
            study.streams[stream_name] = StreamState(
                last_timestamp=running_ts,
                records_processed=0,
                last_run_status=StreamExecutionStatus.RUNNING,
            )
            self.write_state(state)

            tx_data: dict[str, Any] = {
                "last_timestamp": last_ts,
                "new_timestamp": None,
                "records_processed": 0,
                "metadata": {},
            }
            try:
                yield tx_data
                # Validate transition to SUCCESS
                validate_transition(StreamExecutionStatus.RUNNING, StreamExecutionStatus.SUCCESS)

                # Commit changes only if successful
                new_ts = tx_data.get("new_timestamp")
                commit_ts = new_ts or last_ts or running_ts
                if commit_ts.tzinfo is None:
                    commit_ts = commit_ts.replace(tzinfo=UTC)

                study_entry = state.studies.setdefault(study_key, StudyState())
                study_entry.streams[stream_name] = StreamState(
                    last_timestamp=commit_ts,
                    records_processed=tx_data.get("records_processed", 0),
                    last_run_status=StreamExecutionStatus.SUCCESS,
                    metadata=tx_data.get("metadata") or {},
                )
                self.write_state(state)
            except BaseException as err:
                # Mark stream as failed and preserve previous high-water mark
                validate_transition(StreamExecutionStatus.RUNNING, StreamExecutionStatus.FAILED)
                err_ts = last_ts or running_ts
                study_entry = state.studies.setdefault(study_key, StudyState())
                study_entry.streams[stream_name] = StreamState(
                    last_timestamp=err_ts,
                    records_processed=tx_data.get("records_processed", 0),
                    last_run_status=StreamExecutionStatus.FAILED,
                    error_message=str(err),
                    metadata=tx_data.get("metadata") or {},
                )
                self.write_state(state)
                raise

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

    def __init__(self, variable_prefix: str = DEFAULT_VARIABLE_PREFIX) -> None:
        """Initialize the Airflow Variable state provider.

        Args:
            variable_prefix: Prefix namespace for Airflow ``Variable`` checkpoint keys.
        """
        cleaned_prefix = variable_prefix.strip().rstrip(":")
        self.variable_prefix = cleaned_prefix or self.DEFAULT_VARIABLE_PREFIX

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
        ts = datetime.fromisoformat(raw_ts)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return ts

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

    def set_last_timestamp(
        self,
        study_key: str,
        stream_name: str,
        timestamp: datetime,
        records_processed: int = 0,
        status: StreamExecutionStatus | str = StreamExecutionStatus.SUCCESS,
        error_message: str | None = None,
        metadata: dict[str, Any] | None = None,
        force: bool = False,
    ) -> None:
        """Persist the high-water mark checkpoint for a Study and stream in Airflow Variables."""
        validated_status = _coerce_and_validate_status(status)

        if not force:
            current_status = self.get_last_status(study_key, stream_name)
            validate_transition(current_status, validated_status)

        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)

        val: dict[str, Any] = {
            "study_key": study_key,
            "stream_name": stream_name,
            "last_timestamp": timestamp.isoformat(),
            "records_processed": records_processed,
            "status": validated_status.value,
            "error_message": error_message,
            "metadata": metadata or {},
        }
        self._write_variable(self._get_variable_key(study_key, stream_name), val)

    @contextlib.contextmanager
    def transaction(
        self,
        study_key: str,
        stream_name: str,
        fallback_timestamp: datetime | None = None,
    ) -> Generator[dict[str, Any], None, None]:
        """Context manager for transactional state tracking backed by Airflow Variables."""
        last_ts = self.get_last_timestamp(study_key, stream_name)
        if last_ts is None:
            last_ts = fallback_timestamp
        if last_ts is not None and last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=UTC)

        current_status = self.get_last_status(study_key, stream_name)
        validate_transition(current_status, StreamExecutionStatus.RUNNING)

        running_ts = last_ts or datetime.now(UTC)
        self.set_last_timestamp(
            study_key=study_key,
            stream_name=stream_name,
            timestamp=running_ts,
            records_processed=0,
            status=StreamExecutionStatus.RUNNING,
            force=True,
        )

        tx_data: dict[str, Any] = {
            "last_timestamp": last_ts,
            "new_timestamp": None,
            "records_processed": 0,
            "metadata": {},
        }

        try:
            yield tx_data
            validate_transition(StreamExecutionStatus.RUNNING, StreamExecutionStatus.SUCCESS)
            new_ts = tx_data.get("new_timestamp")
            commit_ts = new_ts or last_ts or running_ts
            self.set_last_timestamp(
                study_key=study_key,
                stream_name=stream_name,
                timestamp=commit_ts,
                records_processed=tx_data.get("records_processed", 0),
                status=StreamExecutionStatus.SUCCESS,
                metadata=tx_data.get("metadata"),
                force=True,
            )
        except BaseException as err:
            validate_transition(StreamExecutionStatus.RUNNING, StreamExecutionStatus.FAILED)
            err_ts = last_ts or running_ts
            self.set_last_timestamp(
                study_key=study_key,
                stream_name=stream_name,
                timestamp=err_ts,
                records_processed=tx_data.get("records_processed", 0),
                status=StreamExecutionStatus.FAILED,
                error_message=str(err),
                metadata=tx_data.get("metadata"),
                force=True,
            )
            raise

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
            if not isinstance(s_key, str) or not isinstance(s_name, str) or ts is None:
                continue

            status = self._parse_status(payload) or StreamExecutionStatus.SUCCESS
            study = state.studies.setdefault(s_key, StudyState())
            study.streams[s_name] = StreamState(
                last_timestamp=ts,
                records_processed=payload.get("records_processed", 0),
                last_run_status=status,
                error_message=payload.get("error_message"),
                metadata=payload.get("metadata") or {},
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
