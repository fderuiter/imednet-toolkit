"""Shared lexical credential guards and PHI-safe Job summary helpers for Airflow."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, cast

from imednet.spi.sinks import redact_uri
from imednet.spi.utils import (
    JobFailedError,
    global_sensitivity_registry,
    mask_clinical_phi,
    redact_sensitive_payload,
    redact_sensitive_text,
    validate_path_segment,
)

from ._airflow_compat import AirflowException

_CREDENTIAL_KEY_EXPR = (
    r"x-imn-security-key|x-api-key|"
    r"(?:[a-z0-9]+[_-]+)*(?:api[_-]?key|security[_-]?key|auth[_-]?key|access[_-]?key|"
    r"private[_-]?key(?:[_-]?passphrase)?|secret[_-]?key|csrf[_-]?key|client[_-]?secret|"
    r"oidc[_-]?token|access[_-]?token|refresh[_-]?token|id[_-]?token|api[_-]?token|"
    r"auth[_-]?token|bearer[_-]?token|session[_-]?token|csrf[_-]?token)|"
    r"(?:[a-z0-9]+[_-]+)+(?:token|tokens|secret|secrets|password|passwords|passwd|"
    r"passphrase|credential|credentials)|"
    r"authorization|token|tokens|password|passwords|passwd|pwd|passphrase|"
    r"secret|secrets|credential|credentials"
)

_SINGLE_ASSIGNMENT_SECRET_RE = re.compile(
    rf"(?i)(?<![a-z0-9_])([\"']?)(?<![a-z0-9_])({_CREDENTIAL_KEY_EXPR})(?![a-z0-9_])\1"
    r"\s*(?<![=!<>])[:=](?![=~])\s*"
    r"(?:([\"'])(?!\*{3})(?:[^\"'\r\n]+)\3|(?:(?:bearer|basic)\s+)?(?!\*{3})[^\s,;\"')\]}&]+)"
)

_BEARER_JWT_RE = re.compile(r"(?i)\bbearer\s+eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")

_STANDALONE_JWT_RE = re.compile(
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{4,}\.eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*"
)


def has_embedded_uri_credentials(value: str) -> bool:
    """Return True if *value* embeds plaintext URI userinfo credentials."""
    if "://" not in value or "://***@" in value:
        return False
    return redact_uri(value) != value


def has_inline_secret_tokens(value: str) -> bool:
    """Return True if *value* embeds query secrets, single-delimiter secret pairs, or JWTs."""
    existing_masks = value.count("***")
    if redact_sensitive_text(value).count("***") > existing_masks:
        return True
    if _SINGLE_ASSIGNMENT_SECRET_RE.search(value):
        return True
    return bool(_BEARER_JWT_RE.search(value) or _STANDALONE_JWT_RE.search(value))


def validate_non_secret_identifier(
    value: str,
    *,
    field_name: str,
    path_segment: bool = False,
    parameter_kind: str = "trigger parameter",
) -> str:
    """Validate an identifier and reject blank, unsafe, or credential-bearing strings."""
    if not isinstance(value, str) or not value.strip():
        raise AirflowException(f"'{field_name}' must be a non-empty string.")
    if has_embedded_uri_credentials(value):
        raise AirflowException(
            f"Plaintext URI credentials are not allowed in {parameter_kind} '{field_name}'."
        )
    if has_inline_secret_tokens(value):
        raise AirflowException(
            f"Sensitive credentials or tokens are not allowed in {parameter_kind} '{field_name}'."
        )
    if path_segment:
        validate_path_segment(value, name=field_name)
    return value


_JOB_SUMMARY_KEY_ALIASES: dict[str, str] = {
    "batch_id": "batchId",
    "job_id": "jobId",
    "study_key": "studyKey",
    "date_created": "dateCreated",
    "date_started": "dateStarted",
    "date_finished": "dateFinished",
    "result_url": "resultUrl",
    "result_error": "resultError",
    "record_total_count": "totalRecords",
    "total_records": "totalRecords",
    "record_success_count": "successfulRecords",
    "successful_records": "successfulRecords",
    "record_failure_count": "failedRecords",
    "failed_records": "failedRecords",
}


def _int_or_none(value: Any) -> int | None:
    """Return an integer count when *value* is a non-boolean int, else None."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _normalize_job_summary_mapping(mapping: Mapping[Any, Any]) -> dict[str, Any]:
    """Strip ``results`` and normalize snake_case Job keys to REST camelCase aliases."""
    normalized: dict[str, Any] = {}
    for raw_key, val in mapping.items():
        key_str = str(raw_key)
        if key_str == "results":
            continue
        target_key = _JOB_SUMMARY_KEY_ALIASES.get(key_str, key_str)
        if target_key == key_str or target_key not in normalized or normalized[target_key] is None:
            normalized[target_key] = val
    return normalized


def build_job_summary(status: Any, *, study_key: str, batch_id: str) -> dict[str, Any]:
    """Project a compact, ``results``-free, PHI-masked camelCase ``Job`` summary dictionary."""
    raw_batch = getattr(status, "batch_id", getattr(status, "batchId", None))
    resolved_batch_id = raw_batch if isinstance(raw_batch, str) and raw_batch else batch_id

    model_dump = getattr(status, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump(mode="json", by_alias=True, exclude={"results"})
        except TypeError:
            try:
                dumped = model_dump(mode="json", by_alias=True)
            except TypeError:
                dumped = model_dump(mode="json")
        if isinstance(dumped, Mapping):
            summary = _normalize_job_summary_mapping(dumped)
            if not summary.get("batchId"):
                summary["batchId"] = resolved_batch_id
            for attr_name, camel_key in (
                ("total_records", "totalRecords"),
                ("successful_records", "successfulRecords"),
                ("failed_records", "failedRecords"),
            ):
                computed = _int_or_none(getattr(status, attr_name, None))
                if computed is not None:
                    summary[camel_key] = computed
            return cast(dict[str, Any], mask_clinical_phi(summary))

    raw_state = getattr(status, "state", None)
    state_val = getattr(raw_state, "value", raw_state)
    raw_job_id = getattr(status, "job_id", getattr(status, "jobId", None))
    job_id_val = (
        raw_job_id
        if isinstance(raw_job_id, (str, int)) and not isinstance(raw_job_id, bool)
        else None
    )
    fallback_summary: dict[str, Any] = {
        "studyKey": study_key,
        "batchId": resolved_batch_id,
        "jobId": job_id_val,
        "state": str(state_val) if state_val is not None else None,
        "totalRecords": _int_or_none(
            getattr(status, "total_records", getattr(status, "totalRecords", None))
        ),
        "successfulRecords": _int_or_none(
            getattr(status, "successful_records", getattr(status, "successfulRecords", None))
        ),
        "failedRecords": _int_or_none(
            getattr(status, "failed_records", getattr(status, "failedRecords", None))
        ),
    }
    return cast(dict[str, Any], mask_clinical_phi(fallback_summary))


def sanitize_job_summary(summary: Any) -> Any:
    """Strip any ``results`` payload, normalize keys to camelCase, and mask clinical PHI."""
    if isinstance(summary, Mapping):
        cleaned = _normalize_job_summary_mapping(summary)
        return mask_clinical_phi(cleaned)
    return mask_clinical_phi(summary)


def _collect_phi_values(value: Any, out: set[str]) -> None:
    """Collect raw scalar values stored under sensitive clinical PHI keys."""
    if isinstance(value, Mapping):
        for k, v in value.items():
            if global_sensitivity_registry.is_sensitive(k):
                if isinstance(v, (str, int, float)) and not isinstance(v, bool):
                    text = str(v).strip()
                    if text and text != "***MASKED***":
                        out.add(text)
                elif isinstance(v, (Mapping, list, tuple)):
                    _collect_all_scalars(v, out)
            else:
                _collect_phi_values(v, out)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_phi_values(item, out)


def _collect_all_scalars(value: Any, out: set[str]) -> None:
    """Collect all non-empty scalar string representations inside a sensitive container."""
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        text = str(value).strip()
        if text and text != "***MASKED***":
            out.add(text)
    elif isinstance(value, Mapping):
        for v in value.values():
            _collect_all_scalars(v, out)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_all_scalars(item, out)


def _format_masked_job_failed_error(exc: JobFailedError) -> str:
    """Format ``JobFailedError`` after scrubbing per-``Record`` diagnostics with ``mask_clinical_phi``."""
    masked_errors = cast(list[dict[str, Any]], mask_clinical_phi(exc.record_errors))
    raw_msg = str(exc)

    phi_literals: set[str] = set()
    _collect_phi_values(exc.record_errors, phi_literals)
    _collect_phi_values(getattr(exc.status, "results", None), phi_literals)

    # Rebuild the JobFailedError message with PHI-masked record_errors when record_errors exist
    if exc.record_errors:
        marker = "record_errors="
        idx = raw_msg.find(marker)
        if idx != -1:
            summaries: list[str] = []
            for entry in masked_errors[:5]:
                detail = (
                    entry.get("error")
                    or entry.get("message")
                    or entry.get("description")
                    or entry.get("errors")
                    or entry
                )
                summaries.append(str(detail))
            trailing = ")" if raw_msg.endswith(")") else ""
            raw_msg = f"{raw_msg[:idx]}record_errors={summaries}{trailing}"

    for literal in sorted(phi_literals, key=len, reverse=True):
        raw_msg = raw_msg.replace(literal, "***MASKED***")
    return raw_msg


def scrub_job_error_message(exc_or_message: BaseException | str) -> str:
    """Scrub clinical PHI and sensitive credentials from a Job error or message string."""
    if isinstance(exc_or_message, JobFailedError):
        raw_text = _format_masked_job_failed_error(exc_or_message)
    else:
        raw_text = str(exc_or_message)
    return str(redact_sensitive_payload(redact_uri(redact_sensitive_text(raw_text))))


__all__ = [
    "build_job_summary",
    "has_embedded_uri_credentials",
    "has_inline_secret_tokens",
    "sanitize_job_summary",
    "scrub_job_error_message",
    "validate_non_secret_identifier",
]
