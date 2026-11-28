"""Module-private optimistic Variable CAS, lease verification, and wildcard helpers."""

# ruff: noqa: SLF001

from __future__ import annotations

import contextlib
import copy
import json
import os
import socket
import time
import uuid
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .state_ledger import AirflowStateProvider


@dataclass
class OrmStudySnapshot:
    """Immutable snapshot of an Airflow Study Variable row for optimistic CAS updates."""

    resolved_study: str
    streams: dict[str, dict[str, Any]]
    legacy_keys: list[str]
    has_study_row: bool
    expected_raw_val: str | None


def escape_like_prefix(prefix: str, escape_char: str = "\\") -> str:
    r"""Escape SQL ``LIKE`` wildcard characters (``\\``, ``%``, ``_``) in a key prefix."""
    escaped = prefix.replace(escape_char, escape_char + escape_char)
    escaped = escaped.replace("%", escape_char + "%")
    return escaped.replace("_", escape_char + "_")


def key_matches_study(
    provider: AirflowStateProvider,
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

    decoded_study = provider._parse_study_variable_key(key)
    if decoded_study is not None:
        return decoded_study == study_key

    parsed_legacy = provider._parse_variable_key(key)
    if parsed_legacy is not None:
        return parsed_legacy[0] == study_key

    # Support legacy unencoded study keys containing special characters such as '%'
    raw_study_key = f"{provider.variable_prefix}:{study_key}"
    if ":" not in study_key:
        if key == raw_study_key:
            return True
        if key.startswith(f"{raw_study_key}:") and len(key) > len(raw_study_key) + 1:
            return True

    return False


def list_airflow_variable_keys(provider: AirflowStateProvider, prefix: str) -> list[str]:
    """List Airflow Variable keys matching literal ``prefix`` with wildcards escaped."""
    if not provider._airflow_available():
        return []

    if provider._has_active_task_sdk_comms():
        from airflow.sdk import Variable as SdkVariable

        query_prefix: str | None = prefix
        if "\\" in prefix:
            head = prefix.split("\\", 1)[0]
            query_prefix = head if head else None
        return [str(k) for k in SdkVariable.keys(prefix=query_prefix) if str(k).startswith(prefix)]

    try:
        from airflow.models.variable import Variable as ModelVariable
        from sqlalchemy import select
    except ImportError:
        return []

    _ = escape_like_prefix(prefix)
    with provider._open_orm_read_session() as session:
        rows = session.scalars(
            select(ModelVariable.key)
            .where(ModelVariable.key.startswith(prefix, autoescape=True))
            .order_by(ModelVariable.key)
        ).all()
        return [str(k) for k in rows if str(k).startswith(prefix)]


def monotonic_timestamp(
    normalize_fn: Callable[[datetime | None], datetime | None],
    *candidates: datetime | None,
) -> datetime | None:
    """Return the highest normalized UTC timestamp across non-None candidates."""
    normalized = [ts_utc for cand in candidates if (ts_utc := normalize_fn(cand)) is not None]
    return max(normalized) if normalized else None


def merge_legacy_stream_entries(
    provider: AirflowStateProvider,
    study_key: str,
    legacy_entries: Sequence[tuple[str, dict[str, Any]]],
) -> tuple[str, dict[str, dict[str, Any]]]:
    """Reconstruct ``(resolved_study, streams)`` from legacy per-stream Variable entries."""
    streams: dict[str, dict[str, Any]] = {}
    resolved_study = study_key
    for legacy_key, entry in legacy_entries:
        parsed = provider._parse_variable_key(legacy_key)
        entry_study = entry.get("study_key")
        if isinstance(entry_study, str) and entry_study:
            resolved_study = entry_study
        s_name = entry.get("stream_name") or (parsed[1] if parsed else None)
        if isinstance(s_name, str) and s_name:
            streams[s_name] = dict(entry)
    return resolved_study, streams


def load_orm_cas_snapshot(provider: AirflowStateProvider, study_key: str) -> OrmStudySnapshot:
    """Load Study checkpoints and raw DB column value for optimistic ORM CAS."""
    if not provider._airflow_available():
        return OrmStudySnapshot(
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
        return OrmStudySnapshot(
            resolved_study=study_key,
            streams={},
            legacy_keys=[],
            has_study_row=False,
            expected_raw_val=None,
        )

    study_var_key = provider._get_study_variable_key(study_key)
    legacy_prefix = provider._study_prefix(study_key)

    raw_rows: list[tuple[str, str | None, dict[str, Any] | None]] = []
    with provider._open_orm_read_session() as session:
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
                    provider._deserialize_json_dict(row.val),
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
            if key_matches_study(provider, row_key, study_key, decoded):
                consolidated_payload = decoded
        elif key_matches_study(provider, row_key, study_key, decoded):
            legacy_keys.append(row_key)
            if decoded is not None:
                legacy_entries.append((row_key, decoded))

    if has_study_row:
        if consolidated_payload is None:
            return OrmStudySnapshot(
                resolved_study=study_key,
                streams={},
                legacy_keys=legacy_keys,
                has_study_row=True,
                expected_raw_val=expected_raw_val,
            )
        resolved_study, streams = provider._extract_study_streams(consolidated_payload, study_key)
        return OrmStudySnapshot(
            resolved_study=resolved_study,
            streams=streams,
            legacy_keys=legacy_keys,
            has_study_row=True,
            expected_raw_val=expected_raw_val,
        )

    resolved_study, streams = merge_legacy_stream_entries(provider, study_key, legacy_entries)
    return OrmStudySnapshot(
        resolved_study=resolved_study,
        streams=streams,
        legacy_keys=legacy_keys,
        has_study_row=False,
        expected_raw_val=None,
    )


def try_commit_orm_cas(
    provider: AirflowStateProvider,
    study_key: str,
    streams: dict[str, dict[str, Any]],
    snapshot: OrmStudySnapshot,
) -> tuple[bool, OrmStudySnapshot | None]:
    """Attempt an atomic conditional INSERT/UPDATE on the Study Variable row."""
    if not provider._airflow_available():
        return True, snapshot

    try:
        from airflow.models.variable import Variable as ModelVariable
        from airflow.utils.session import create_session
        from sqlalchemy import delete, insert, update
        from sqlalchemy.exc import IntegrityError, OperationalError
    except ImportError:
        return True, snapshot

    study_var_key = provider._get_study_variable_key(study_key)
    next_description = provider._compute_lease_description(streams)
    payload: dict[str, Any] = {
        "study_key": study_key,
        "streams": streams,
    }
    serialized = json.dumps(payload, indent=2)
    val_col = getattr(ModelVariable, "_val", ModelVariable.val)
    if hasattr(ModelVariable, "_val"):
        model_var_cls: Any = ModelVariable
        init_kwargs: dict[str, Any] = {"key": study_var_key, "val": serialized}
        if hasattr(ModelVariable, "description"):
            init_kwargs["description"] = next_description
        tmp_var = model_var_cls(**init_kwargs)
        new_raw_val = str(getattr(tmp_var, "_val", serialized))
        col_values: dict[str, Any] = {"_val": new_raw_val}
        if hasattr(ModelVariable, "is_encrypted"):
            col_values["is_encrypted"] = bool(tmp_var.is_encrypted)
        if hasattr(ModelVariable, "description"):
            col_values["description"] = next_description
    else:
        new_raw_val = serialized
        col_values = {"val": new_raw_val}

    provider._ensure_orm_session()
    for lock_attempt in range(5):
        try:
            with create_session() as session:
                if not snapshot.has_study_row:
                    session.execute(insert(ModelVariable).values(key=study_var_key, **col_values))
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
        OrmStudySnapshot(
            resolved_study=study_key,
            streams=copy.deepcopy(streams),
            legacy_keys=[],
            has_study_row=True,
            expected_raw_val=new_raw_val,
        ),
    )


def cas_apply_orm(
    provider: AirflowStateProvider,
    study_key: str,
    mutator_fn: Callable[[str, dict[str, dict[str, Any]]], dict[str, dict[str, Any]] | None],
    *,
    initial_snapshot: OrmStudySnapshot | None = None,
    max_retries: int = 12,
) -> OrmStudySnapshot:
    """Atomically read-modify-write Study checkpoints via optimistic compare-and-swap."""
    from .state_ledger import InvalidStateTransitionError

    for attempt in range(max_retries):
        snapshot = (
            initial_snapshot
            if (attempt == 0 and initial_snapshot is not None)
            else load_orm_cas_snapshot(provider, study_key)
        )
        working_streams = copy.deepcopy(snapshot.streams)
        updated_streams = mutator_fn(snapshot.resolved_study, working_streams)
        if updated_streams is None:
            return snapshot
        ok, next_snapshot = try_commit_orm_cas(
            provider,
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


def verify_sdk_streams_lease(
    provider: AirflowStateProvider,
    study_key: str,
    stream_names: Sequence[str],
    *,
    owner_pid: int,
    owner_host: str,
    lease_id: str,
    phase: str,
) -> tuple[str, dict[str, dict[str, Any]]]:
    """Verify via ``airflow.sdk.Variable`` that ``lease_id`` still owns ``stream_names``."""
    from .state_ledger import InvalidStateTransitionError

    if not provider._airflow_available():
        return study_key, {}
    study_var_key = provider._get_study_variable_key(study_key)
    persisted = provider._read_variable(study_var_key)
    if persisted is None or not key_matches_study(provider, study_var_key, study_key, persisted):
        first_stream = stream_names[0] if stream_names else ""
        raise InvalidStateTransitionError(
            f"Cannot {phase} displaced RUNNING lease (missing checkpoint lease) "
            f"for study '{study_key}' stream '{first_stream}'."
        )
    resolved_study, persisted_streams = provider._extract_study_streams(persisted, study_key)
    for s_name in stream_names:
        if not provider._entry_matches_lease(
            persisted_streams.get(s_name),
            expected_pid=owner_pid,
            expected_host=owner_host,
            expected_lease_id=lease_id,
        ):
            raise InvalidStateTransitionError(
                f"Cannot {phase} displaced RUNNING lease (lease token mismatch) "
                f"for study '{study_key}' stream '{s_name}' "
                f"(owner_pid={owner_pid}, owner_host={owner_host!r}, lease_id={lease_id!r})."
            )
    return resolved_study, persisted_streams


@contextlib.contextmanager
def transact_airflow_streams(
    provider: AirflowStateProvider,
    study_key: str,
    unique_streams: Sequence[str],
    fallback_timestamp: datetime | None,
    *,
    lease_ttl_seconds: float | None,
    verify_sdk_on_acquire: bool,
) -> Generator[dict[str, dict[str, Any]], None, None]:
    """Execute a transactional checkpoint lease across one or more Airflow Study streams."""
    from .state_ledger import (
        InvalidStateTransitionError,
        StreamExecutionStatus,
        _check_stale_running,
        _merge_preserved_lineage_metadata,
        _normalize_optional_utc,
        validate_transition,
    )

    ttl = provider.lease_ttl_seconds if lease_ttl_seconds is None else lease_ttl_seconds
    fallback_utc = _normalize_optional_utc(fallback_timestamp)
    owner_pid = os.getpid()
    owner_host = socket.gethostname()
    lease_id = uuid.uuid4().hex

    def _prepare_running_streams(
        resolved_study: str,
        streams: dict[str, dict[str, Any]],
        *,
        record_stale_failures: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[
        dict[str, datetime | None],
        dict[str, dict[str, Any]],
        dict[str, dict[str, Any]],
    ]:
        now = datetime.now(UTC)
        local_prior_ts: dict[str, datetime | None] = {}
        local_prior_meta: dict[str, dict[str, Any]] = {}
        local_tx_map: dict[str, dict[str, Any]] = {}

        for s_name in unique_streams:
            raw_entry = streams.get(s_name)
            prior_ts = _normalize_optional_utc(provider._parse_timestamp(raw_entry))
            raw_prior_meta = raw_entry.get("metadata") if isinstance(raw_entry, dict) else None
            prior_meta: dict[str, Any] = (
                copy.deepcopy(raw_prior_meta) if isinstance(raw_prior_meta, dict) else {}
            )
            local_prior_ts[s_name] = prior_ts
            local_prior_meta[s_name] = prior_meta
            effective_ts = prior_ts if prior_ts is not None else fallback_utc
            current_status = provider._parse_status(raw_entry)

            if current_status == StreamExecutionStatus.RUNNING and raw_entry is not None:
                cur_pid, cur_host, cur_exp, _ = provider._extract_lease_fields(raw_entry)
                stale_reason = _check_stale_running(cur_pid, cur_host, cur_exp, now=now)
                if stale_reason is not None:
                    validate_transition(current_status, StreamExecutionStatus.FAILED)
                    if record_stale_failures is not None:
                        raw_processed = raw_entry.get("records_processed", 0)
                        record_stale_failures[s_name] = provider._build_stream_payload(
                            study_key=resolved_study,
                            stream_name=s_name,
                            timestamp=prior_ts,
                            records_processed=(
                                raw_processed
                                if isinstance(raw_processed, int)
                                and not isinstance(raw_processed, bool)
                                else 0
                            ),
                            status=StreamExecutionStatus.FAILED,
                            error_message=f"Recovered stale running stream: {stale_reason}",
                            metadata=copy.deepcopy(prior_meta),
                        )
                    current_status = StreamExecutionStatus.FAILED

            validate_transition(current_status, StreamExecutionStatus.RUNNING)
            local_tx_map[s_name] = {
                "last_timestamp": effective_ts,
                "new_timestamp": None,
                "records_processed": 0,
                "metadata": copy.deepcopy(prior_meta),
                "_lineage_hydrated": True,
            }

        lease_exp = now + timedelta(seconds=ttl)
        for s_name in unique_streams:
            streams[s_name] = provider._build_stream_payload(
                study_key=resolved_study,
                stream_name=s_name,
                timestamp=local_prior_ts[s_name],
                records_processed=0,
                status=StreamExecutionStatus.RUNNING,
                metadata=copy.deepcopy(local_prior_meta[s_name]),
                owner_pid=owner_pid,
                owner_host=owner_host,
                lease_expires_at=lease_exp,
                lease_id=lease_id,
                lease_token=lease_id,
            )
        return local_prior_ts, local_prior_meta, local_tx_map

    def _build_committed_entry(
        resolved_study: str,
        s_name: str,
        current_entry: dict[str, Any] | None,
        prior_ts: datetime | None,
        prior_meta: dict[str, Any],
        tx_data: dict[str, Any],
    ) -> dict[str, Any]:
        validate_transition(StreamExecutionStatus.RUNNING, StreamExecutionStatus.SUCCESS)
        raw_new_ts = tx_data.get("new_timestamp")
        candidate_ts = _normalize_optional_utc(raw_new_ts) if raw_new_ts is not None else prior_ts
        commit_ts = monotonic_timestamp(
            _normalize_optional_utc,
            prior_ts,
            provider._parse_timestamp(current_entry),
            candidate_ts,
        )
        running_meta = current_entry.get("metadata") if isinstance(current_entry, dict) else None
        base_meta = _merge_preserved_lineage_metadata(
            prior_meta,
            running_meta if isinstance(running_meta, Mapping) else None,
        )
        raw_commit_meta = tx_data.get("metadata")
        merged_commit_meta = _merge_preserved_lineage_metadata(
            base_meta,
            raw_commit_meta if isinstance(raw_commit_meta, Mapping) else {},
        )
        return provider._build_stream_payload(
            study_key=resolved_study,
            stream_name=s_name,
            timestamp=commit_ts,
            records_processed=tx_data.get("records_processed", 0),
            status=StreamExecutionStatus.SUCCESS,
            metadata=merged_commit_meta,
        )

    if provider._has_active_task_sdk_comms():
        resolved_study, streams, legacy_keys = provider._load_study_state(study_key)
        stale_failures: dict[str, dict[str, Any]] = {}
        prior_ts_map, prior_meta_map, tx_map = _prepare_running_streams(
            resolved_study,
            streams,
            record_stale_failures=stale_failures if verify_sdk_on_acquire else None,
        )
        if stale_failures:
            stale_snapshot = dict(streams)
            stale_snapshot.update(stale_failures)
            provider._save_study_state(
                resolved_study,
                stale_snapshot,
                legacy_keys_to_delete=legacy_keys,
            )
            legacy_keys = []
        provider._save_study_state(
            resolved_study,
            streams,
            legacy_keys_to_delete=legacy_keys,
        )
        if verify_sdk_on_acquire and provider._airflow_available():
            verify_sdk_streams_lease(
                provider,
                resolved_study,
                unique_streams,
                owner_pid=owner_pid,
                owner_host=owner_host,
                lease_id=lease_id,
                phase="acquire",
            )

        try:
            yield tx_map
        except BaseException as err:
            if isinstance(err, GeneratorExit):
                raise
            err_msg = str(err)
            if provider._airflow_available():
                latest_study, latest_streams, latest_legacy = provider._load_study_state(study_key)
                owned = [
                    s_name
                    for s_name in unique_streams
                    if provider._entry_matches_lease(
                        latest_streams.get(s_name),
                        expected_pid=owner_pid,
                        expected_host=owner_host,
                        expected_lease_id=lease_id,
                    )
                ]
                if owned:
                    for s_name in owned:
                        validate_transition(
                            StreamExecutionStatus.RUNNING, StreamExecutionStatus.FAILED
                        )
                        current_entry = latest_streams.get(s_name)
                        rollback_ts = monotonic_timestamp(
                            _normalize_optional_utc,
                            prior_ts_map[s_name],
                            provider._parse_timestamp(current_entry),
                        )
                        latest_streams[s_name] = provider._build_stream_payload(
                            study_key=latest_study,
                            stream_name=s_name,
                            timestamp=rollback_ts,
                            records_processed=tx_map[s_name].get("records_processed", 0),
                            status=StreamExecutionStatus.FAILED,
                            error_message=err_msg,
                            metadata=copy.deepcopy(prior_meta_map[s_name]),
                        )
                    provider._save_study_state(
                        latest_study,
                        latest_streams,
                        legacy_keys_to_delete=latest_legacy,
                    )
            raise
        else:
            if provider._airflow_available():
                latest_study, latest_streams = verify_sdk_streams_lease(
                    provider,
                    resolved_study,
                    unique_streams,
                    owner_pid=owner_pid,
                    owner_host=owner_host,
                    lease_id=lease_id,
                    phase="commit",
                )
            else:
                latest_study, latest_streams = resolved_study, streams
            for s_name in unique_streams:
                latest_streams[s_name] = _build_committed_entry(
                    latest_study,
                    s_name,
                    latest_streams.get(s_name),
                    prior_ts_map[s_name],
                    prior_meta_map[s_name],
                    tx_map[s_name],
                )
            provider._save_study_state(latest_study, latest_streams)
        return

    orm_prior_ts_map: dict[str, datetime | None] = {}
    orm_prior_meta_map: dict[str, dict[str, Any]] = {}
    orm_tx_map: dict[str, dict[str, Any]] = {}

    def _acquire_orm(
        resolved_study: str,
        streams: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        local_ts, local_meta, local_tx = _prepare_running_streams(resolved_study, streams)
        orm_prior_ts_map.clear()
        orm_prior_ts_map.update(local_ts)
        orm_prior_meta_map.clear()
        orm_prior_meta_map.update(local_meta)
        orm_tx_map.clear()
        orm_tx_map.update(local_tx)
        return streams

    active_snapshot = cas_apply_orm(provider, study_key, _acquire_orm)

    try:
        yield orm_tx_map
    except BaseException as err:
        if isinstance(err, GeneratorExit):
            raise
        err_msg = str(err)

        def _rollback_orm(
            resolved_study: str,
            streams: dict[str, dict[str, Any]],
        ) -> dict[str, dict[str, Any]] | None:
            mutated = False
            for s_name in unique_streams:
                validate_transition(StreamExecutionStatus.RUNNING, StreamExecutionStatus.FAILED)
                current_entry = streams.get(s_name)
                if provider._airflow_available() and not provider._entry_matches_lease(
                    current_entry,
                    expected_pid=owner_pid,
                    expected_host=owner_host,
                    expected_lease_id=lease_id,
                ):
                    continue
                mutated = True
                rollback_ts = monotonic_timestamp(
                    _normalize_optional_utc,
                    orm_prior_ts_map[s_name],
                    provider._parse_timestamp(current_entry),
                )
                streams[s_name] = provider._build_stream_payload(
                    study_key=resolved_study,
                    stream_name=s_name,
                    timestamp=rollback_ts,
                    records_processed=orm_tx_map[s_name].get("records_processed", 0),
                    status=StreamExecutionStatus.FAILED,
                    error_message=err_msg,
                    metadata=copy.deepcopy(orm_prior_meta_map[s_name]),
                )
            return streams if mutated else None

        cas_apply_orm(
            provider,
            study_key,
            _rollback_orm,
            initial_snapshot=active_snapshot,
        )
        raise
    else:

        def _commit_orm(
            resolved_study: str,
            streams: dict[str, dict[str, Any]],
        ) -> dict[str, dict[str, Any]]:
            for s_name in unique_streams:
                current_entry = streams.get(s_name)
                if provider._airflow_available() and not provider._entry_matches_lease(
                    current_entry,
                    expected_pid=owner_pid,
                    expected_host=owner_host,
                    expected_lease_id=lease_id,
                ):
                    raise InvalidStateTransitionError(
                        f"Cannot commit displaced RUNNING lease (lease token mismatch) "
                        f"for study '{study_key}' stream '{s_name}' "
                        f"(owner_pid={owner_pid}, owner_host={owner_host!r}, lease_id={lease_id!r})."
                    )
                streams[s_name] = _build_committed_entry(
                    resolved_study,
                    s_name,
                    current_entry,
                    orm_prior_ts_map[s_name],
                    orm_prior_meta_map[s_name],
                    orm_tx_map[s_name],
                )
            return streams

        cas_apply_orm(
            provider,
            study_key,
            _commit_orm,
            initial_snapshot=active_snapshot,
        )
