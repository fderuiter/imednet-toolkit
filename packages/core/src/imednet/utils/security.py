"""Security utilities."""

import functools
import json
import re
from collections.abc import Collection
from typing import Any

from imednet.errors import ClientError, PathTraversalValidationError
from imednet.utils.secrets import is_sensitive_key, redact_sensitive_payload

_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[/\\]")
_CAMEL_TO_SNAKE_RE1 = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_TO_SNAKE_RE2 = re.compile(r"([a-z\d])([A-Z])")
_DELIMITER_RE = re.compile(r"[^a-zA-Z0-9]+")
_JSON_OR_KEY_BLOCK_START_RE = re.compile(
    r"""(?<![A-Za-z0-9_<:/.-])(?P<q1>['"]?)(?P<key>[A-Za-z][A-Za-z0-9_-]{0,63})(?P=q1)"""
    r"""(?P<op>\s*[:=]\s*)(?P<b1>[{\[])|(?P<b2>[{\[])"""
)
_DIAGNOSTIC_QUOTED_KV_RE = re.compile(
    r"""(?<![A-Za-z0-9_<:/.-])(?P<q1>['"]?)(?P<key>[A-Za-z][A-Za-z0-9_-]{0,63})(?P=q1)"""
    r"""(?P<op>\s*(?:==|!=|=~|<=|>=|<|>|=|:)\s*)"""
    r"""(?P<q2>['"])(?P<val>(?:\\[\s\S]|(?!(?P=q2))[^\\])*)(?P=q2)"""
)
_DIAGNOSTIC_PLAIN_KV_RE = re.compile(
    r"""(?<![A-Za-z0-9_<:/.-])(?P<q1>['"]?)(?P<key>[A-Za-z][A-Za-z0-9_-]{0,63})(?P=q1)"""
    r"""(?P<op>\s*(?:==|!=|=~|<=|>=|<|>|=|:)\s*)"""
    r"""(?P<val>[^'"\s,;&\)\]\}\r\n\x00]"""
    r"""(?:[^\r\n,;&\)\]\}\x00]*?(?=(?:\s+(?:(?:and|or|AND|OR)\s+)?[A-Za-z][A-Za-z0-9_.-]*\s*(?:==|!=|=~|<=|>=|<|>|=|:))|[,;&\)\]\}\r\n\x00]|$))?)"""
)
_JSON_ARRAY_ELEM_START_CHARS = frozenset(
    {'"', "{", "[", "]", "-", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "t", "f", "n"}
)
_JSON_DECODER = json.JSONDecoder()


def _normalize_key_uncached(key: Any) -> str:
    """Normalize a field key to canonical lowercase snake_case without caching."""
    if key is None:
        return ""
    s = str(key).strip()
    if not s:
        return ""
    s = _CAMEL_TO_SNAKE_RE1.sub(r"\1_\2", s)
    s = _CAMEL_TO_SNAKE_RE2.sub(r"\1_\2", s)
    s = _DELIMITER_RE.sub("_", s)
    return s.strip("_").lower()


@functools.lru_cache(maxsize=4096)
def _normalize_key_cached(key: Any) -> str:
    """LRU-cached key normalization helper."""
    return _normalize_key_uncached(key)


def _normalize_key(key: Any) -> str:
    """Normalize a field key to canonical lowercase snake_case.

    Converts camelCase, PascalCase, uppercase acronyms, and delimited names
    (hyphens, whitespace, etc.) into lowercase snake_case. Non-string inputs
    are converted to strings first.
    """
    try:
        return _normalize_key_cached(key)
    except TypeError:
        return _normalize_key_uncached(key)


_normalize_key.cache_info = _normalize_key_cached.cache_info  # type: ignore[attr-defined]
_normalize_key.cache_clear = _normalize_key_cached.cache_clear  # type: ignore[attr-defined]


class SensitivityRegistry:
    """Registry for managing sensitive fields (PHI) that should be masked in logs."""

    def __init__(self) -> None:
        """Initialize the registry with default sensitive and exempt keys."""
        # Default clinical and CDISC PHI fields
        self._sensitive_keys = {
            "patient_name",
            "patient_initials",
            "dob",
            "date_of_birth",
            "ssn",
            "phone",
            "email",
            "address",
            "birth_date",
            "brthdtc",
            "brthdat",
            "brthyr",
            "subjinit",
        }
        # Keys explicitly exempted from masking
        self._exempt_keys = {
            "subject_key",
            "study_key",
            "record_id",
            "usubjid",
            "subjid",
            "studyid",
        }
        self._sens_snapshot = frozenset(self._sensitive_keys)
        self._exempt_snapshot = frozenset(self._exempt_keys)
        self._collapsed_sensitive = {k.replace("_", "") for k in self._sensitive_keys}
        self._collapsed_exempt = {k.replace("_", "") for k in self._exempt_keys}
        self._key_cache: dict[str, bool] = {}

    def _sync_collapsed_sets(self) -> None:
        """Resynchronize collapsed key sets if underlying sets were mutated directly."""
        if self._sensitive_keys != self._sens_snapshot:
            self._sens_snapshot = frozenset(self._sensitive_keys)
            self._collapsed_sensitive = {k.replace("_", "") for k in self._sensitive_keys}
            self._key_cache.clear()
        if self._exempt_keys != self._exempt_snapshot:
            self._exempt_snapshot = frozenset(self._exempt_keys)
            self._collapsed_exempt = {k.replace("_", "") for k in self._exempt_keys}
            self._key_cache.clear()

    def add_sensitive_key(self, key: Any) -> None:
        """Add a key to the sensitive list if it is not exempted."""
        self._sync_collapsed_sets()
        norm_key = _normalize_key(key)
        if not norm_key:
            return
        collapsed = norm_key.replace("_", "")
        if norm_key not in self._exempt_keys and collapsed not in self._collapsed_exempt:
            self._sensitive_keys.add(norm_key)
            self._collapsed_sensitive.add(collapsed)
            self._sens_snapshot = frozenset(self._sensitive_keys)
            self._key_cache.clear()

    def remove_sensitive_key(self, key: Any) -> None:
        """Remove a key from the sensitive list."""
        self._sync_collapsed_sets()
        norm_key = _normalize_key(key)
        if not norm_key:
            return
        collapsed = norm_key.replace("_", "")
        for existing in list(self._sensitive_keys):
            if existing == norm_key or existing.replace("_", "") == collapsed:
                self._sensitive_keys.discard(existing)
        self._collapsed_sensitive.discard(collapsed)
        self._sens_snapshot = frozenset(self._sensitive_keys)
        self._key_cache.clear()

    def add_exempt_key(self, key: Any) -> None:
        """Add a key to the exemption list."""
        self._sync_collapsed_sets()
        norm_key = _normalize_key(key)
        if norm_key:
            self._exempt_keys.add(norm_key)
            self._collapsed_exempt.add(norm_key.replace("_", ""))
            self._exempt_snapshot = frozenset(self._exempt_keys)
            self._key_cache.clear()

    def remove_exempt_key(self, key: Any) -> None:
        """Remove a key from the exemption list."""
        self._sync_collapsed_sets()
        norm_key = _normalize_key(key)
        if not norm_key:
            return
        collapsed = norm_key.replace("_", "")
        for existing in list(self._exempt_keys):
            if existing == norm_key or existing.replace("_", "") == collapsed:
                self._exempt_keys.discard(existing)
        self._collapsed_exempt.discard(collapsed)
        self._exempt_snapshot = frozenset(self._exempt_keys)
        self._key_cache.clear()

    def is_exempt(self, key: Any) -> bool:
        """Check if a key is explicitly exempted from PHI masking."""
        self._sync_collapsed_sets()
        norm_key = _normalize_key(key)
        if not norm_key:
            return False
        return norm_key in self._exempt_keys or norm_key.replace("_", "") in self._collapsed_exempt

    def is_sensitive(self, key: Any) -> bool:
        """Check if a key is considered sensitive and not exempted."""
        self._sync_collapsed_sets()
        if key.__class__ is str:
            cached = self._key_cache.get(key)
            if cached is not None:
                return cached
        norm_key = _normalize_key(key)
        if not norm_key:
            res = False
        else:
            collapsed = norm_key.replace("_", "")
            if norm_key in self._exempt_keys or collapsed in self._collapsed_exempt:
                res = False
            else:
                res = norm_key in self._sensitive_keys or collapsed in self._collapsed_sensitive
        if key.__class__ is str:
            if len(self._key_cache) >= 8192:
                self._key_cache.clear()
            self._key_cache[key] = res
        return res

    def snapshot(self) -> tuple[frozenset[str], frozenset[str]]:
        """Return an immutable snapshot of active sensitive and exempt key sets."""
        self._sync_collapsed_sets()
        return (self._sens_snapshot, self._exempt_snapshot)


global_sensitivity_registry = SensitivityRegistry()


def build_study_sensitivity_registry(
    custom_phi_keys: Collection[str] | None = None,
) -> SensitivityRegistry:
    """Return a SensitivityRegistry combining global rules with *custom_phi_keys*."""
    if not custom_phi_keys:
        return global_sensitivity_registry
    reg = SensitivityRegistry()
    sens_keys, exempt_keys = global_sensitivity_registry.snapshot()
    for ex_key in exempt_keys:
        reg.add_exempt_key(ex_key)
    for sens_key in sens_keys:
        reg.add_sensitive_key(sens_key)
    for custom_key in custom_phi_keys:
        reg.add_sensitive_key(custom_key)
    return reg


_FORMULA_PREFIX_CHARS = frozenset("=+-@")
_CONTAINER_TYPES = (dict, list, tuple)


def _mask_clinical_phi_synced(
    value: Any,
    active_registry: SensitivityRegistry,
    cache_get: Any,
) -> Any:
    """Recursively mask sensitive keys after syncing ``active_registry`` once."""
    vc = value.__class__
    if vc is dict or isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            if k.__class__ is str:
                is_sens = cache_get(k)
                if is_sens is None:
                    is_sens = active_registry.is_sensitive(k)
            else:
                is_sens = active_registry.is_sensitive(k)
            if is_sens:
                out[k] = "***MASKED***"
            elif isinstance(v, _CONTAINER_TYPES):
                out[k] = _mask_clinical_phi_synced(v, active_registry, cache_get)
            else:
                out[k] = v
        return out
    if vc is list or isinstance(value, list):
        return [
            _mask_clinical_phi_synced(v, active_registry, cache_get)
            if isinstance(v, _CONTAINER_TYPES)
            else v
            for v in value
        ]
    if vc is tuple or isinstance(value, tuple):
        return tuple(
            _mask_clinical_phi_synced(v, active_registry, cache_get)
            if isinstance(v, _CONTAINER_TYPES)
            else v
            for v in value
        )
    return value


def mask_clinical_phi(
    value: Any,
    *,
    registry: SensitivityRegistry | None = None,
) -> Any:
    """Recursively mask sensitive keys in unstructured data."""
    if not isinstance(value, _CONTAINER_TYPES):
        return value
    active_registry = global_sensitivity_registry if registry is None else registry
    active_registry._sync_collapsed_sets()
    return _mask_clinical_phi_synced(value, active_registry, active_registry._key_cache.get)


def _find_balanced_bracket_end(text: str, open_pos: int) -> int:
    """Return index immediately after matching ``}`` or ``]`` from ``open_pos`` (or ``len(text)``)."""
    n = len(text)
    stack = ["}" if text[open_pos] == "{" else "]"]
    idx = open_pos + 1
    in_quote: str | None = None
    while idx < n and stack:
        ch = text[idx]
        if in_quote is not None:
            if ch == "\\":
                idx += 2
                continue
            if ch == in_quote:
                in_quote = None
        elif ch in {'"', "'"}:
            in_quote = ch
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch == stack[-1]:
            stack.pop()
        idx += 1
    return idx


def _try_raw_decode_json_container(text: str, brace_pos: int) -> tuple[Any, int] | None:
    """Attempt fast JSON decoding of a ``dict`` or ``list`` starting at ``brace_pos``."""
    n = len(text)
    probe = brace_pos + 1
    while probe < n and text[probe] in " \t\r\n":
        probe += 1
    if probe >= n:
        return None
    next_ch = text[probe]
    if text[brace_pos] == "{":
        if next_ch not in {'"', "}"}:
            return None
    elif next_ch not in _JSON_ARRAY_ELEM_START_CHARS:
        return None
    try:
        parsed, end_idx = _JSON_DECODER.raw_decode(text, brace_pos)
    except ValueError:
        return None
    if isinstance(parsed, (dict, list)):
        return parsed, end_idx
    return None


def _extract_and_sanitize_json_blocks(
    text: str,
    *,
    active_registry: SensitivityRegistry,
    placeholders: dict[str, str],
    sentinel: str,
) -> str:
    """Sanitize embedded single-line/multiline JSON containers and sensitive structured blocks."""
    if "{" not in text and "[" not in text:
        return text

    parts: list[str] = []
    idx = 0
    n = len(text)
    while idx < n:
        match = _JSON_OR_KEY_BLOCK_START_RE.search(text, idx)
        if match is None:
            parts.append(text[idx:])
            break

        if match.group("b1") is not None:
            brace_pos = match.start("b1")
            key = match.group("key")
            q1 = match.group("q1")
            op = match.group("op")
            if active_registry.is_sensitive(key) or is_sensitive_key(key):
                parts.append(text[idx : match.start()])
                end_pos = _find_balanced_bracket_end(text, brace_pos)
                mask_literal = "***MASKED***" if active_registry.is_sensitive(key) else "***"
                quote_ch = q1 if q1 else ('"' if ":" in op else "")
                rendered = f"{q1}{key}{q1}{op}{quote_ch}{mask_literal}{quote_ch}"
                tok = f"{sentinel}IMNBLK{len(placeholders)}{sentinel}"
                placeholders[tok] = rendered
                parts.append(tok)
                idx = end_pos
                continue

            decoded = _try_raw_decode_json_container(text, brace_pos)
            if decoded is not None:
                parsed, end_idx = decoded
                parts.append(text[idx:brace_pos])
                sanitized_obj = sanitize_diagnostic_payload(parsed, registry=active_registry)
                raw_span = text[brace_pos:end_idx]
                if sanitized_obj != parsed:
                    rendered = (
                        json.dumps(sanitized_obj, indent=2, ensure_ascii=False)
                        if "\n" in raw_span
                        else json.dumps(sanitized_obj, ensure_ascii=False)
                    )
                else:
                    rendered = raw_span
                tok = f"{sentinel}IMNBLK{len(placeholders)}{sentinel}"
                placeholders[tok] = rendered
                parts.append(tok)
                idx = end_idx
                continue

            parts.append(text[idx : brace_pos + 1])
            idx = brace_pos + 1
            continue

        brace_pos = match.start("b2")
        decoded = _try_raw_decode_json_container(text, brace_pos)
        if decoded is not None:
            parsed, end_idx = decoded
            parts.append(text[idx:brace_pos])
            sanitized_obj = sanitize_diagnostic_payload(parsed, registry=active_registry)
            raw_span = text[brace_pos:end_idx]
            if sanitized_obj != parsed:
                rendered = (
                    json.dumps(sanitized_obj, indent=2, ensure_ascii=False)
                    if "\n" in raw_span
                    else json.dumps(sanitized_obj, ensure_ascii=False)
                )
            else:
                rendered = raw_span
            tok = f"{sentinel}IMNBLK{len(placeholders)}{sentinel}"
            placeholders[tok] = rendered
            parts.append(tok)
            idx = end_idx
        else:
            parts.append(text[idx : brace_pos + 1])
            idx = brace_pos + 1

    return "".join(parts)


def sanitize_diagnostic_text(
    value: Any,
    *,
    registry: SensitivityRegistry | None = None,
    _depth: int = 0,
) -> str:
    """Redact credentials, URL filters, multiline JSON, and inline PHI expressions in text."""
    from imednet.utils.url import redact_sensitive_text

    active_registry = global_sensitivity_registry if registry is None else registry
    if isinstance(value, BaseException):
        if len(value.args) == 1 and isinstance(value.args[0], _CONTAINER_TYPES):
            return str(sanitize_diagnostic_payload(value.args[0], registry=active_registry))
        raw_text = str(value)
    elif isinstance(value, _CONTAINER_TYPES):
        return str(sanitize_diagnostic_payload(value, registry=active_registry))
    elif isinstance(value, str):
        raw_text = value
    else:
        raw_text = str(value)

    if not raw_text or raw_text in {"[REDACTED]", "***MASKED***", "***"}:
        return raw_text

    sentinel = "\x00"
    if sentinel in raw_text:
        sentinel = "\x01\x02"
        while sentinel in raw_text:
            sentinel += "\x02"

    placeholders: dict[str, str] = {}
    staged = _extract_and_sanitize_json_blocks(
        raw_text,
        active_registry=active_registry,
        placeholders=placeholders,
        sentinel=sentinel,
    )
    url_sanitized = redact_sensitive_text(staged, registry=active_registry, _depth=_depth)
    redacted = str(redact_sensitive_payload(url_sanitized))

    def _mask_quoted(match: re.Match[str]) -> str:
        key = match.group("key")
        q1 = match.group("q1")
        op = match.group("op")
        q2 = match.group("q2")
        val = match.group("val")
        if active_registry.is_sensitive(key):
            return f"{q1}{key}{q1}{op}{q2}***MASKED***{q2}"
        if is_sensitive_key(key):
            return f"{q1}{key}{q1}{op}{q2}***{q2}"
        if val and _depth < 3 and val not in {"***MASKED***", "***", "[REDACTED]"}:
            esc_seq = f"\\{q2}"
            unescaped = val.replace(esc_seq, q2) if esc_seq in val else val
            inner = sanitize_diagnostic_text(
                unescaped,
                registry=active_registry,
                _depth=_depth + 1,
            )
            if inner != unescaped:
                reescaped = inner.replace(q2, esc_seq) if esc_seq in val else inner
                return f"{q1}{key}{q1}{op}{q2}{reescaped}{q2}"
        return match.group(0)

    def _mask_plain(match: re.Match[str]) -> str:
        key = match.group("key")
        is_phi = active_registry.is_sensitive(key)
        is_sec = False if is_phi else is_sensitive_key(key)
        if not is_phi and not is_sec:
            return match.group(0)
        raw_val = match.group("val")
        stripped = raw_val.rstrip()
        if not stripped or stripped in {"***MASKED***", "***", "[REDACTED]"}:
            return match.group(0)
        trailing = raw_val[len(stripped) :]
        q1 = match.group("q1")
        op = match.group("op")
        replacement = "***MASKED***" if is_phi else "***"
        return f"{q1}{key}{q1}{op}{replacement}{trailing}"

    masked = _DIAGNOSTIC_QUOTED_KV_RE.sub(_mask_quoted, redacted)
    masked = _DIAGNOSTIC_PLAIN_KV_RE.sub(_mask_plain, masked)

    if placeholders:
        for tok, rendered in placeholders.items():
            masked = masked.replace(tok, rendered)
    return masked


def _has_phi_field_target(data: dict[Any, Any], registry: SensitivityRegistry) -> bool:
    """Return True when an indirect ``{'variable': '<phi>', 'value': ...}`` dict targets PHI."""
    if "value" not in data:
        return False
    for target_key in ("attribute", "variable", "name", "field"):
        raw_target = data.get(target_key)
        if isinstance(raw_target, str) and registry.is_sensitive(raw_target):
            return True
    return False


def _scrub_diagnostic_leaves(value: Any, *, registry: SensitivityRegistry) -> Any:
    if isinstance(value, str):
        if value in {"[REDACTED]", "***MASKED***", "***"}:
            return value
        return sanitize_diagnostic_text(value, registry=registry)
    if isinstance(value, dict):
        mask_target_value = _has_phi_field_target(value, registry)
        return {
            k: (
                "***MASKED***"
                if (mask_target_value and k == "value")
                else _scrub_diagnostic_leaves(v, registry=registry)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub_diagnostic_leaves(v, registry=registry) for v in value]
    if isinstance(value, tuple):
        return tuple(_scrub_diagnostic_leaves(v, registry=registry) for v in value)
    return value


def sanitize_diagnostic_payload(
    value: Any,
    *,
    registry: SensitivityRegistry | None = None,
) -> Any:
    """Deep-sanitize structured payloads by redacting credentials and masking clinical PHI."""
    active_registry = global_sensitivity_registry if registry is None else registry
    redacted = redact_sensitive_payload(value)
    masked = mask_clinical_phi(redacted, registry=active_registry)
    return _scrub_diagnostic_leaves(masked, registry=active_registry)


def sanitize_csv_formula(value: Any) -> Any:
    """Sanitize a value to prevent CSV/Excel Formula Injection.

    Prefixes strings starting with =, +, -, or @ (even after whitespace) with a single quote.
    Lists and tuples are recursively sanitized.
    """
    if value.__class__ is str:
        if not value:
            return value
        ch0 = value[0]
        if ch0 in _FORMULA_PREFIX_CHARS:
            return f"'{value}"
        if not ch0.isspace():
            return value
        stripped = value.lstrip()
        if stripped and stripped[0] in _FORMULA_PREFIX_CHARS:
            return f"'{value}"
        return value
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return f"'{value}"
    if isinstance(value, list):
        return [sanitize_csv_formula(v) for v in value]
    if isinstance(value, tuple):
        return tuple(sanitize_csv_formula(v) for v in value)
    return value


def validate_header_value(value: str) -> None:
    """Validate that a header value does not contain newline characters.

    Args:
        value: The header value to validate.

    Raises:
        ClientError: If the value contains newline characters.
    """
    if "\n" in value or "\r" in value:
        raise ClientError(f"Header value must not contain newlines: {value!r}")


def validate_partition_key(key: str) -> None:
    """Reject partition keys that could escape or reshape the target directory."""
    if "\x00" in key:
        raise PathTraversalValidationError(f"Partition key must not contain null bytes: {key!r}")
    if key.startswith(("/", "\\")) or _WINDOWS_ABSOLUTE_PATH_RE.match(key):
        raise PathTraversalValidationError(
            f"Partition key must not use absolute path modifiers: {key!r}"
        )
    if "../" in key or "..\\" in key or key == "..":
        raise PathTraversalValidationError(
            f"Partition key must not contain parent-directory traversal: {key!r}"
        )
    if "/" in key or "\\" in key:
        raise PathTraversalValidationError(
            f"Partition key must not contain path separators: {key!r}"
        )
