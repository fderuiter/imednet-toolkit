"""Module-private target-scoped stream naming and pre-upgrade watermark seeding for Airflow."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from typing import Any

from imednet.spi import sink_base
from imednet.spi.sinks import redact_uri
from imednet.spi.utils import redact_sensitive_payload, redact_sensitive_text


def sanitize_stream_segment(value: str, *, max_len: int = 80) -> str:
    """Redact URI/inline secrets and normalize an identifier into a ``stream_name`` token."""
    raw = str(value).strip()
    if not raw:
        return ""
    redacted = redact_sensitive_text(str(redact_sensitive_payload(redact_uri(raw))))
    cleaned = redacted.replace("***", "").replace("[REDACTED]", "")
    sanitized = re.sub(r"[^A-Za-z0-9_-]+", "_", cleaned).strip("_")
    sanitized = re.sub(r"_+", "_", sanitized)
    if not sanitized:
        return ""
    if len(sanitized) <= max_len:
        return sanitized
    digest = hashlib.sha256(redacted.encode("utf-8")).hexdigest()[:8]
    half = max((max_len - 10) // 2, 8)
    return f"{sanitized[:half]}_{sanitized[-half:]}_{digest}"


def resolve_operator_dag_id(
    operator: Any,
    context: Mapping[str, Any] | None = None,
) -> str | None:
    """Resolve the enclosing DAG identifier without touching unassigned ``operator.dag``."""
    if isinstance(context, Mapping):
        for ctx_key in ("dag", "dag_run", "ti", "task_instance"):
            ctx_obj = context.get(ctx_key)
            ctx_dag_id = getattr(ctx_obj, "dag_id", None) if ctx_obj is not None else None
            if isinstance(ctx_dag_id, str) and ctx_dag_id.strip():
                return ctx_dag_id.strip()
        raw_ctx_dag_id = context.get("dag_id")
        if isinstance(raw_ctx_dag_id, str) and raw_ctx_dag_id.strip():
            return raw_ctx_dag_id.strip()

    has_dag_fn = getattr(operator, "has_dag", None)
    if callable(has_dag_fn):
        if has_dag_fn():
            raw_dag_id = getattr(operator, "dag_id", None)
            if isinstance(raw_dag_id, str) and raw_dag_id.strip():
                return raw_dag_id.strip()
    else:
        raw_dag_id = getattr(operator, "dag_id", None)
        if (
            isinstance(raw_dag_id, str)
            and raw_dag_id.strip()
            and raw_dag_id.strip() != "adhoc_airflow"
        ):
            return raw_dag_id.strip()

    env_dag_id = os.environ.get("AIRFLOW_CTX_DAG_ID", "").strip()
    return env_dag_id or None


def resolve_export_target_identifier(
    operator: Any,
    dest: str,
    runtime_kwargs: Mapping[str, Any],
    conn_settings: Mapping[str, Any] | None = None,
) -> str:
    """Extract and sanitize destination-specific target segments for ``stream_name``."""
    conn = conn_settings or {}
    output_path = getattr(operator, "output_path", None)
    raw_parts: list[Any] = []

    if dest == "snowflake":
        raw_parts.extend(
            [
                runtime_kwargs.get("database") or conn.get("database"),
                runtime_kwargs.get("schema") or conn.get("schema"),
                runtime_kwargs.get("table") or conn.get("table") or output_path,
            ]
        )
    elif dest == "mongodb":
        raw_parts.extend(
            [
                runtime_kwargs.get("database") or conn.get("database"),
                runtime_kwargs.get("collection") or conn.get("collection") or output_path,
            ]
        )
    elif dest == "neo4j":
        raw_parts.extend(
            [
                runtime_kwargs.get("database") or conn.get("database"),
                runtime_kwargs.get("label") or conn.get("label") or output_path,
            ]
        )
    elif dest == "duckdb":
        raw_parts.extend(
            [
                output_path
                or runtime_kwargs.get("db_path")
                or runtime_kwargs.get("path")
                or conn.get("db_path"),
                runtime_kwargs.get("table_name")
                or runtime_kwargs.get("table")
                or conn.get("table_name"),
            ]
        )
    elif dest == "duckdb_by_form":
        raw_parts.append(
            output_path
            or runtime_kwargs.get("db_path")
            or runtime_kwargs.get("path")
            or conn.get("db_path")
        )
    elif dest in ("sql", "long_sql"):
        conn_str = (
            conn.get("conn_str")
            or runtime_kwargs.get("conn_str")
            or (output_path if isinstance(output_path, str) and "://" in output_path else None)
        )
        table_target = (
            (output_path if isinstance(output_path, str) and "://" not in output_path else None)
            or runtime_kwargs.get("table")
            or runtime_kwargs.get("table_name")
            or conn.get("table")
        )
        raw_parts.extend([conn_str, table_target])
    elif dest == "sql_by_form":
        raw_parts.append(output_path or runtime_kwargs.get("conn_str") or conn.get("conn_str"))
    elif dest == "hive_parquet":
        raw_parts.append(
            output_path or runtime_kwargs.get("base_dir") or runtime_kwargs.get("path")
        )
    else:
        raw_parts.append(
            output_path
            or runtime_kwargs.get("output_path")
            or runtime_kwargs.get("path")
            or runtime_kwargs.get("table")
            or runtime_kwargs.get("table_name")
            or runtime_kwargs.get("collection")
        )

    sanitized_parts = [
        seg
        for part in raw_parts
        if part is not None
        and isinstance(part, (str, os.PathLike))
        and str(part).strip()
        and (seg := sanitize_stream_segment(str(part), max_len=64))
    ]
    if not sanitized_parts:
        return ""
    return sanitize_stream_segment("_".join(sanitized_parts), max_len=80)


def build_default_airflow_stream_name(
    operator: Any,
    dest: str,
    *,
    sink_prefix: bool = False,
    context: Mapping[str, Any] | None = None,
    runtime_kwargs: Mapping[str, Any] | None = None,
    conn_settings: Mapping[str, Any] | None = None,
) -> str:
    """Return a target-scoped default stream name invariant across mapped task indexes."""
    clean_dest = sanitize_stream_segment(dest, max_len=32) or "records"
    base = f"airflow_{clean_dest}" if sink_prefix else clean_dest
    segments: list[str] = [base]

    dag_id = resolve_operator_dag_id(operator, context)
    if dag_id:
        dag_seg = sanitize_stream_segment(dag_id, max_len=48)
        if dag_seg:
            segments.append(dag_seg)

    task_id = getattr(operator, "task_id", None)
    if isinstance(task_id, str) and task_id.strip():
        task_seg = sanitize_stream_segment(task_id.strip(), max_len=48)
        if task_seg:
            segments.append(task_seg)

    effective_kwargs = (
        runtime_kwargs if runtime_kwargs is not None else operator._get_runtime_export_kwargs()  # noqa: SLF001
    )
    resolved_conn = conn_settings
    if resolved_conn is None:
        try:
            resolved_conn = operator._resolve_destination_connection(dest)  # noqa: SLF001
        except Exception:
            resolved_conn = {}
    target_seg = resolve_export_target_identifier(
        operator,
        dest,
        effective_kwargs,
        resolved_conn,
    )
    if target_seg:
        segments.append(target_seg)

    return "_".join(segments)


def pre_upgrade_candidate_streams(operator: Any, dest: str) -> list[str]:
    """Return candidate pre-upgrade stream names for single-target checkpoint migration."""
    candidates: list[str] = []
    task_id = getattr(operator, "task_id", None)
    clean_task = task_id.strip() if isinstance(task_id, str) and task_id.strip() else None
    if clean_task:
        for cand in (f"{dest}_{clean_task}", f"airflow_{dest}_{clean_task}"):
            if cand not in candidates:
                candidates.append(cand)
    if dest in ("snowflake", "neo4j", "mongodb"):
        sink_cand = f"airflow_{dest}"
        if sink_cand not in candidates:
            candidates.append(sink_cand)
    return candidates


def _load_provider_study_streams(provider: Any, study_key: str) -> Mapping[str, Any] | None:
    """Extract existing stream entries for ``study_key`` from a checkpoint state provider."""
    load_study_fn = getattr(provider, "_load_study_state", None)
    if callable(load_study_fn):
        loaded = load_study_fn(study_key)
        if isinstance(loaded, tuple) and len(loaded) >= 2 and isinstance(loaded[1], Mapping):
            return loaded[1]
        return None

    read_state_fn = getattr(provider, "read_state", None)
    if callable(read_state_fn):
        ledger_state = read_state_fn()
        studies_map = getattr(ledger_state, "studies", None)
        if isinstance(studies_map, Mapping):
            study_obj = studies_map.get(study_key)
            streams_attr = getattr(study_obj, "streams", None)
            if isinstance(streams_attr, Mapping):
                return streams_attr
    return None


def seed_pre_upgrade_watermark(
    operator: Any,
    *,
    dest: str,
    scoped_stream: str,
    state_provider: Any,
    ledger_path: Any,
    default_provider_name: str,
) -> datetime | None:
    """Seed ``scoped_stream`` from one matching pre-upgrade checkpoint when state is absent."""
    try:
        provider = (
            state_provider
            if state_provider is not None
            else sink_base.get_state_provider(
                ledger_path=ledger_path,
                provider_name=default_provider_name,
            )
        )
    except Exception:
        return None

    if provider is None:
        return None

    study_key = str(getattr(operator, "study_key", ""))
    task_id = getattr(operator, "task_id", None)
    clean_task = task_id.strip() if isinstance(task_id, str) and task_id.strip() else None
    task_seg = sanitize_stream_segment(clean_task, max_len=48) if clean_task else ""

    try:
        study_streams = _load_provider_study_streams(provider, study_key)

        if dest in ("sql_by_form", "duckdb_by_form"):
            if not clean_task or study_streams is None:
                return None
            scoped_prefix = f"{scoped_stream}_"
            if any(str(k).startswith(scoped_prefix) for k in study_streams):
                return None
            legacy_prefix = f"{dest}_{clean_task}_"
            seeded_any: datetime | None = None
            for s_key, s_entry in list(study_streams.items()):
                if not str(s_key).startswith(legacy_prefix):
                    continue
                form_suffix = str(s_key)[len(legacy_prefix) :]
                if not form_suffix:
                    continue
                raw_val = (
                    s_entry.get("last_timestamp")
                    if isinstance(s_entry, Mapping)
                    else getattr(s_entry, "last_timestamp", None)
                )
                if not isinstance(raw_val, (datetime, str)):
                    continue
                norm_ts = sink_base.normalize_watermark_datetime(raw_val)
                if norm_ts is not None and callable(getattr(provider, "set_last_timestamp", None)):
                    provider.set_last_timestamp(
                        study_key,
                        f"{scoped_stream}_{form_suffix}",
                        norm_ts,
                        status="success",
                    )
                    seeded_any = norm_ts
            return seeded_any

        candidates = [
            cand for cand in pre_upgrade_candidate_streams(operator, dest) if cand != scoped_stream
        ]
        if not candidates:
            return None

        if study_streams is not None:
            if scoped_stream in study_streams:
                return None
            other_scoped_prefixes = [f"{dest}_", f"airflow_{dest}_"]
            existing_other_scoped = [
                str(k)
                for k in study_streams
                if str(k) not in candidates
                and any(str(k).startswith(p) for p in other_scoped_prefixes)
                and (not task_seg or f"_{task_seg}_" in f"_{k}_")
            ]
            if existing_other_scoped:
                return None

            matched_timestamps: list[tuple[str, datetime]] = []
            for cand in candidates:
                if cand not in study_streams:
                    continue
                entry = study_streams[cand]
                raw_ts = (
                    entry.get("last_timestamp")
                    if isinstance(entry, Mapping)
                    else getattr(entry, "last_timestamp", None)
                )
                if isinstance(raw_ts, (datetime, str)):
                    norm_ts = sink_base.normalize_watermark_datetime(raw_ts)
                    if norm_ts is not None:
                        matched_timestamps.append((cand, norm_ts))
        else:
            existing_ts = provider.get_last_timestamp(study_key, scoped_stream)
            if (
                isinstance(existing_ts, (datetime, str))
                and sink_base.normalize_watermark_datetime(existing_ts) is not None
            ):
                return None
            get_status_fn = getattr(provider, "get_last_status", None)
            if callable(get_status_fn):
                existing_status = get_status_fn(study_key, scoped_stream)
                if isinstance(existing_status, (str, Enum)):
                    return None

            matched_timestamps = []
            for cand in candidates:
                raw_ts = provider.get_last_timestamp(study_key, cand)
                if isinstance(raw_ts, (datetime, str)):
                    norm_ts = sink_base.normalize_watermark_datetime(raw_ts)
                    if norm_ts is not None:
                        matched_timestamps.append((cand, norm_ts))

        if len(matched_timestamps) != 1:
            return None

        _, seed_ts = matched_timestamps[0]
        set_ts_fn = getattr(provider, "set_last_timestamp", None)
        if callable(set_ts_fn):
            set_ts_fn(
                study_key,
                scoped_stream,
                seed_ts,
                status="success",
            )
        return seed_ts
    except Exception:
        return None
