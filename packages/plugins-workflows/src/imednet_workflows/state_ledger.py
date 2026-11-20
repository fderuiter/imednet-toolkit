"""Stateful incremental high-water mark tracker for workflow streams."""

from __future__ import annotations

import abc
import contextlib
import json
import os
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
        StreamExecutionStatus.RUNNING,
        StreamExecutionStatus.PENDING,
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
            # Ensure return datetime is timezone-aware when present
            ts = stream.last_timestamp
            if ts is None:
                return None
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

            # Ensure timezone-aware datetime when present
            if timestamp is not None and timestamp.tzinfo is None:
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
            if current_stream and current_stream.last_timestamp is not None:
                last_ts = current_stream.last_timestamp
                if last_ts.tzinfo is None:
                    last_ts = last_ts.replace(tzinfo=UTC)
            if last_ts is None:
                last_ts = fallback_timestamp
            if last_ts is not None and last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=UTC)

            # Atomic transaction guard: ensure transition to RUNNING is valid
            validate_transition(current_status, StreamExecutionStatus.RUNNING)

            study.streams[stream_name] = StreamState(
                last_timestamp=last_ts,
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
                commit_ts: datetime | None = new_ts if new_ts is not None else last_ts
                if commit_ts is not None and commit_ts.tzinfo is None:
                    commit_ts = commit_ts.replace(tzinfo=UTC)

                study_entry = state.studies.setdefault(study_key, StudyState())
                study_entry.streams[stream_name] = StreamState(
                    last_timestamp=commit_ts,
                    records_processed=tx_data.get("records_processed", 0),
                    last_run_status=StreamExecutionStatus.SUCCESS,
                    metadata=tx_data.get("metadata") or {},
                )
                self.write_state(state)
            except Exception as err:
                # Mark stream as failed and preserve previous high-water mark (including None)
                validate_transition(StreamExecutionStatus.RUNNING, StreamExecutionStatus.FAILED)
                study_entry = state.studies.setdefault(study_key, StudyState())
                study_entry.streams[stream_name] = StreamState(
                    last_timestamp=last_ts,
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


class AirflowStateProvider(BaseStateProvider):  # pragma: no cover
    """Manages transactional state using Airflow XCom metadata."""

    def _get_xcom_key(self, study_key: str, stream_name: str) -> str:
        return f"state_{study_key}_{stream_name}"

    @staticmethod
    def _get_current_task_instance() -> Any:
        """Resolve active Airflow TaskInstance via Airflow 3 SDK with legacy fallback."""
        import sys

        try:
            from airflow.sdk import get_current_context as sdk_get_current_context

            context = sdk_get_current_context()
            return context["ti"]
        except ImportError:
            pass
        except Exception:
            if "airflow.operators.python" not in sys.modules:
                return None

        try:
            from airflow.operators.python import get_current_context as legacy_get_current_context

            context = legacy_get_current_context()
            return context["ti"]
        except Exception:
            return None

    @staticmethod
    def _extract_xcom_payload(xcom_obj: Any, xcom_cls: Any) -> dict[str, Any] | None:
        """Deserialize an XComModel row's value into a dictionary when present."""
        if xcom_obj is None:
            return None
        raw_val = getattr(xcom_obj, "value", None)
        if isinstance(raw_val, dict):
            return raw_val
        deserialize = getattr(xcom_cls, "deserialize_value", None)
        if callable(deserialize):
            try:
                decoded = deserialize(xcom_obj)
                if isinstance(decoded, dict):
                    return decoded
            except Exception:  # noqa: S110
                pass
        if isinstance(raw_val, (str, bytes)):
            try:
                decoded = json.loads(raw_val)
                if isinstance(decoded, dict):
                    return decoded
            except Exception:  # noqa: S110
                pass
        return None

    @staticmethod
    def _parse_optional_timestamp(raw_ts: Any) -> datetime | None:
        """Parse an optional ISO-8601 timestamp string or datetime to UTC."""
        if raw_ts is None:
            return None
        if isinstance(raw_ts, datetime):
            return raw_ts.replace(tzinfo=UTC) if raw_ts.tzinfo is None else raw_ts
        if isinstance(raw_ts, str):
            parsed = datetime.fromisoformat(raw_ts)
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
        return None

    @staticmethod
    def _ensure_dag_run_for_xcom(
        session: Any,
        *,
        dag_id: str,
        task_id: str,
        run_id: str,
    ) -> None:
        """Ensure minimal DagModel/DagRun/TaskInstance rows exist for out-of-context XCom writes."""
        from airflow.models.dag import DagModel
        from airflow.models.dagrun import DagRun
        from airflow.utils.types import DagRunType

        now_utc = datetime.now(UTC)
        bundle_name = "imednet_state_sync"
        with contextlib.suppress(Exception):
            from airflow.models.dagbundle import DagBundleModel

            if (
                session.query(DagBundleModel).filter(DagBundleModel.name == bundle_name).first()
                is None
            ):
                session.add(DagBundleModel(name=bundle_name))
                session.flush()

        dag_model = session.query(DagModel).filter(DagModel.dag_id == dag_id).first()
        if dag_model is None:
            dag_cls: Any = DagModel
            session.add(dag_cls(dag_id=dag_id, bundle_name=bundle_name))
            session.flush()

        dag_run = (
            session.query(DagRun).filter(DagRun.dag_id == dag_id, DagRun.run_id == run_id).first()
        )
        if dag_run is None:
            try:
                dag_run = DagRun(
                    dag_id=dag_id,
                    run_id=run_id,
                    run_type=DagRunType.MANUAL,
                    logical_date=now_utc,
                    run_after=now_utc,
                )
            except TypeError:
                dag_run = DagRun(dag_id=dag_id, run_id=run_id)
            session.add(dag_run)
            session.flush()

        with contextlib.suppress(Exception):
            from sqlalchemy import insert as sa_insert
            from sqlalchemy import inspect as sa_inspect

            bind = session.get_bind()
            if bind is not None and sa_inspect(bind).has_table("task_instance"):
                from airflow.models.taskinstance import TaskInstance

                existing_ti = (
                    session.query(TaskInstance)
                    .filter(
                        TaskInstance.dag_id == dag_id,
                        TaskInstance.task_id == task_id,
                        TaskInstance.run_id == run_id,
                    )
                    .first()
                )
                if existing_ti is None:
                    ti_table = TaskInstance.__table__
                    row_values: dict[str, Any] = {
                        "task_id": task_id,
                        "dag_id": dag_id,
                        "run_id": run_id,
                        "map_index": -1,
                    }
                    if "pool" in ti_table.c:
                        row_values["pool"] = "default_pool"
                    if "dag_version_id" in ti_table.c:
                        from airflow.models.dag_version import DagVersion

                        dv = session.query(DagVersion).filter(DagVersion.dag_id == dag_id).first()
                        if dv is None:
                            dv = DagVersion(
                                dag_id=dag_id,
                                version_number=1,
                                bundle_name="imednet_state_sync",
                            )
                            session.add(dv)
                            session.flush()
                        row_values["dag_version_id"] = dv.id
                    session.execute(sa_insert(ti_table).values(**row_values))
                    session.flush()

    def get_last_timestamp(self, study_key: str, stream_name: str) -> datetime | None:
        """Returns the high-water mark timestamp from Airflow XCom."""
        ti = self._get_current_task_instance()
        if ti is not None:
            try:
                val = ti.xcom_pull(
                    key=self._get_xcom_key(study_key, stream_name), include_prior_dates=True
                )
                if isinstance(val, dict) and "last_timestamp" in val:
                    return self._parse_optional_timestamp(val["last_timestamp"])
            except Exception:  # noqa: S110
                pass

        # Fallback to DB query if not running within a context (or when XCom pull returned empty)
        try:
            from airflow.models.xcom import XComModel
            from airflow.utils.session import provide_session

            @provide_session  # type: ignore[misc]
            def _get_xcom(session: Any = None) -> Any:
                return (
                    session.query(XComModel)
                    .filter(XComModel.key == self._get_xcom_key(study_key, stream_name))
                    .order_by(XComModel.timestamp.desc())
                    .first()
                )

            xcom_obj = _get_xcom()
            payload = self._extract_xcom_payload(xcom_obj, XComModel)
            if payload is not None and "last_timestamp" in payload:
                return self._parse_optional_timestamp(payload["last_timestamp"])
        except ImportError:
            pass
        return None

    def get_last_status(self, study_key: str, stream_name: str) -> StreamExecutionStatus | None:
        """Returns the execution status from Airflow XCom."""
        ti = self._get_current_task_instance()
        if ti is not None:
            try:
                val = ti.xcom_pull(
                    key=self._get_xcom_key(study_key, stream_name), include_prior_dates=True
                )
                if isinstance(val, dict) and "status" in val:
                    raw_status = val["status"]
                    if isinstance(raw_status, StreamExecutionStatus):
                        return raw_status
                    if isinstance(raw_status, str):
                        try:
                            return StreamExecutionStatus(raw_status.lower())
                        except ValueError:
                            return StreamExecutionStatus.FAILED
                    return StreamExecutionStatus.FAILED
            except Exception:  # noqa: S110
                pass

        try:
            from airflow.models.xcom import XComModel
            from airflow.utils.session import provide_session

            @provide_session  # type: ignore[misc]
            def _get_xcom(session: Any = None) -> Any:
                return (
                    session.query(XComModel)
                    .filter(XComModel.key == self._get_xcom_key(study_key, stream_name))
                    .order_by(XComModel.timestamp.desc())
                    .first()
                )

            xcom_obj = _get_xcom()
            payload = self._extract_xcom_payload(xcom_obj, XComModel)
            if payload is not None and "status" in payload:
                raw_status = payload["status"]
                if isinstance(raw_status, StreamExecutionStatus):
                    return raw_status
                if isinstance(raw_status, str):
                    try:
                        return StreamExecutionStatus(raw_status.lower())
                    except ValueError:
                        return StreamExecutionStatus.FAILED
                return StreamExecutionStatus.FAILED
        except ImportError:
            pass
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
    ) -> None:
        """Sets the high-water mark timestamp atomically using Airflow XCom."""
        _ = force
        validated_status = _coerce_and_validate_status(status)

        if timestamp is not None and timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)

        val = {
            "last_timestamp": timestamp.isoformat() if timestamp is not None else None,
            "records_processed": records_processed,
            "status": validated_status.value,
            "error_message": error_message,
            "metadata": metadata or {},
        }

        ti = self._get_current_task_instance()
        if ti is not None:
            try:
                ti.xcom_push(key=self._get_xcom_key(study_key, stream_name), value=val)
                return
            except Exception:  # noqa: S110
                pass

        # Fallback to DB if out of context but within Airflow environment
        try:
            from airflow.models.xcom import XComModel

            dag_id = os.environ.get("AIRFLOW_CTX_DAG_ID", "manual_state_sync")
            task_id = os.environ.get("AIRFLOW_CTX_TASK_ID", "manual_state_sync")
            run_id = (
                os.environ.get("AIRFLOW_CTX_DAG_RUN_ID")
                or os.environ.get("AIRFLOW_CTX_EXECUTION_DATE")
                or "manual__state_sync"
            )
            xcom_key = self._get_xcom_key(study_key, stream_name)

            try:
                XComModel.set(
                    key=xcom_key,
                    value=val,
                    task_id=task_id,
                    dag_id=dag_id,
                    run_id=run_id,
                )
            except Exception as exc:
                err_msg = str(exc)
                if "DAG run not found" not in err_msg and "FOREIGN KEY" not in err_msg.upper():
                    raise
                from airflow.utils.session import provide_session

                @provide_session  # type: ignore[misc]
                def _set_with_dag_run(session: Any = None) -> None:
                    self._ensure_dag_run_for_xcom(
                        session,
                        dag_id=dag_id,
                        task_id=task_id,
                        run_id=run_id,
                    )
                    XComModel.set(
                        key=xcom_key,
                        value=val,
                        task_id=task_id,
                        dag_id=dag_id,
                        run_id=run_id,
                        session=session,
                    )

                _set_with_dag_run()
        except ImportError:
            pass

    @contextlib.contextmanager
    def transaction(
        self,
        study_key: str,
        stream_name: str,
        fallback_timestamp: datetime | None = None,
    ) -> Generator[dict[str, Any], None, None]:
        """Context manager for transactional state tracking using Airflow XCom."""
        last_ts = self.get_last_timestamp(study_key, stream_name)
        if last_ts is None:
            last_ts = fallback_timestamp
        if last_ts is not None and last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=UTC)

        current_status = self.get_last_status(study_key, stream_name)
        # Atomic transaction guard: ensure transition to RUNNING is valid
        validate_transition(current_status, StreamExecutionStatus.RUNNING)

        self.set_last_timestamp(
            study_key=study_key,
            stream_name=stream_name,
            timestamp=last_ts,
            records_processed=0,
            status=StreamExecutionStatus.RUNNING,
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
            commit_ts: datetime | None = new_ts if new_ts is not None else last_ts
            self.set_last_timestamp(
                study_key=study_key,
                stream_name=stream_name,
                timestamp=commit_ts,
                records_processed=tx_data.get("records_processed", 0),
                status=StreamExecutionStatus.SUCCESS,
                metadata=tx_data.get("metadata"),
            )
        except Exception as err:
            validate_transition(StreamExecutionStatus.RUNNING, StreamExecutionStatus.FAILED)
            self.set_last_timestamp(
                study_key=study_key,
                stream_name=stream_name,
                timestamp=last_ts,
                records_processed=tx_data.get("records_processed", 0),
                status=StreamExecutionStatus.FAILED,
                error_message=str(err),
                metadata=tx_data.get("metadata"),
            )
            raise

    def delete_entry(self, study_key: str, stream_name: str | None = None) -> bool:
        """Deletes a study or specific stream entry from XCom."""
        try:
            from airflow.models.xcom import XComModel
            from airflow.utils.session import provide_session

            @provide_session  # type: ignore[misc]
            def _delete_xcom(session: Any = None) -> bool:
                query = session.query(XComModel)
                if stream_name:
                    query = query.filter(
                        XComModel.key == self._get_xcom_key(study_key, stream_name)
                    )
                else:
                    query = query.filter(XComModel.key.like(f"state_{study_key}_%"))

                deleted = query.delete(synchronize_session=False)
                return deleted > 0  # type: ignore[no-any-return]

            return bool(_delete_xcom())
        except ImportError:
            return False

    def read_state(self) -> LedgerState:
        """Reads and validates the current full state from XComs for CLI display."""
        state = LedgerState()
        try:
            from airflow.models.xcom import XComModel
            from airflow.utils.session import provide_session

            @provide_session  # type: ignore[misc]
            def _get_all_xcoms(session: Any = None) -> Any:
                return (
                    session.query(XComModel)
                    .filter(XComModel.key.like("state_%"))
                    .order_by(XComModel.timestamp.desc())
                    .all()
                )

            xcoms = _get_all_xcoms()

            # To avoid duplicates if there are multiple XComs for the same key over time,
            # keep track of keys processed. Since it's ordered by desc, first one is latest.
            processed_keys = set()
            for x in xcoms:
                if x.key in processed_keys:
                    continue
                processed_keys.add(x.key)

                parts = x.key.split("_", 2)
                if len(parts) == 3 and parts[0] == "state":
                    s_key = parts[1]
                    s_name = parts[2]

                    val = self._extract_xcom_payload(x, XComModel)
                    if val is not None and "last_timestamp" in val:
                        ts = self._parse_optional_timestamp(val["last_timestamp"])

                        study = state.studies.setdefault(s_key, StudyState())
                        study.streams[s_name] = StreamState(
                            last_timestamp=ts,
                            records_processed=val.get("records_processed", 0),
                            last_run_status=val.get("status", "success"),
                            error_message=val.get("error_message"),
                            metadata=val.get("metadata", {}),
                        )
        except ImportError:
            pass
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
