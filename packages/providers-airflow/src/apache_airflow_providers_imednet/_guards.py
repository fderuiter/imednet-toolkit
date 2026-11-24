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
    redact_url_query,
    validate_path_segment,
)

from ._airflow_compat import AirflowException

_CREDENTIAL_KEY_EXPR = (
    r"x-imn-security-key|x-api-key|"
    r"x[-_]amz[-_](?:signature|credential|security[-_]token)|"
    r"x[-_]goog[-_](?:signature|credential)|"
    r"aws[_-]?access[_-]?key[_-]?id|"
    r"(?:[a-z0-9]+[_-]+)*(?:api[_-]?key|security[_-]?key|auth[_-]?key|access[_-]?key|"
    r"private[_-]?key(?:[_-]?passphrase)?|secret[_-]?key|csrf[_-]?key|client[_-]?secret|"
    r"oidc[_-]?token|access[_-]?token|refresh[_-]?token|id[_-]?token|api[_-]?token|"
    r"auth[_-]?token|bearer[_-]?token|session[_-]?token|csrf[_-]?token)|"
    r"(?:[a-z0-9]+[_-]+)+(?:token|tokens|secret|secrets|password|passwords|passwd|"
    r"passphrase|credential|credentials|signature|signatures)|"
    r"authorization|token|tokens|password|passwords|passwd|pwd|passphrase|"
    r"secret|secrets|credential|credentials|signature|signatures|sig"
)

_SINGLE_ASSIGNMENT_SECRET_RE = re.compile(
    rf"(?i)(?<![a-z0-9_])([\"']?)(?<![a-z0-9_])({_CREDENTIAL_KEY_EXPR})(?![a-z0-9_])\1"
    r"\s*(?<![=!<>])[:=](?!=)\s*"
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


def _redact_summary_urls_and_secrets(value: Any, *, key_name: str | None = None) -> Any:
    """Recursively redact URL signatures, query tokens, URI credentials, and secret payloads."""
    if isinstance(value, Mapping):
        sanitized_map: dict[str, Any] = {}
        for k, v in value.items():
            k_str = str(k)
            if k_str == "results":
                continue
            sanitized_map[k_str] = _redact_summary_urls_and_secrets(v, key_name=k_str)
        return redact_sensitive_payload(sanitized_map)
    if isinstance(value, list):
        return [_redact_summary_urls_and_secrets(item, key_name=key_name) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_summary_urls_and_secrets(item, key_name=key_name) for item in value)
    if isinstance(value, str):
        redacted = redact_uri(redact_sensitive_text(value))
        is_url_field = key_name is not None and key_name.lower() in {"result_url", "resulturl"}
        if "?" in redacted and ("://" in redacted or redacted.startswith("/") or is_url_field):
            redacted = redact_url_query(redacted)
        return redact_sensitive_payload(redacted)
    return value


def build_job_summary(status: Any, *, study_key: str, batch_id: str) -> dict[str, Any]:
    """Project a compact, ``results``-free, PHI-masked, and credential-redacted ``Job`` summary."""
    model_dump = getattr(status, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump(mode="json", exclude={"results"})
        except TypeError:
            dumped = model_dump(mode="json")
        if isinstance(dumped, Mapping):
            summary = {str(k): v for k, v in dumped.items() if str(k) != "results"}
            return cast(dict[str, Any], sanitize_job_summary(summary))

    raw_state = getattr(status, "state", None)
    state_val = getattr(raw_state, "value", raw_state)
    raw_batch = getattr(status, "batch_id", None)
    fallback_summary: dict[str, Any] = {
        "study_key": study_key,
        "batch_id": raw_batch if isinstance(raw_batch, str) and raw_batch else batch_id,
        "state": str(state_val) if state_val is not None else None,
    }
    raw_result_url = getattr(status, "result_url", None)
    if raw_result_url is None:
        raw_result_url = getattr(status, "resultUrl", None)
    if isinstance(raw_result_url, str) and raw_result_url:
        fallback_summary["result_url"] = raw_result_url
    return cast(dict[str, Any], sanitize_job_summary(fallback_summary))


def sanitize_job_summary(summary: Any) -> Any:
    """Strip ``results``, mask clinical PHI, and redact URL signatures/tokens on a Job summary."""
    if isinstance(summary, Mapping):
        cleaned = {str(k): v for k, v in summary.items() if str(k) != "results"}
        phi_masked = mask_clinical_phi(cleaned)
        return _redact_summary_urls_and_secrets(phi_masked)
    phi_masked = mask_clinical_phi(summary)
    return _redact_summary_urls_and_secrets(phi_masked)


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
