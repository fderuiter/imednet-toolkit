"""In-memory HTTP transport for offline simulation mode."""

from __future__ import annotations

import json
import math
import re
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from imednet.core.paginator import MAX_PAGE_SIZE, _parse_int_param
from imednet.simulation.state import SimulationState, get_default_simulation_state
from imednet.utils.filters import (
    _parse_clause_operator,
    _unquote_filter_scalar,
    validate_filter_key,
    validate_filter_operator,
    validate_filter_syntax,
    validate_record_filter,
)
from imednet.utils.filters import (
    _split_filter_clauses as _split_raw_filter_clauses,
)

_ISO_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}|\b|$)")
_STANDARD_LIST_QUERY_KEYS: frozenset[str] = frozenset({"page", "size", "sort", "filter"})


@dataclass(frozen=True)
class SimulationRouteSpec:
    """Contract metadata for one documented iMednet EDC REST operation."""

    name: str
    method: str
    path_template: str
    segments: tuple[str, ...]
    allowed_query_keys: frozenset[str]


SIMULATION_ROUTES: tuple[SimulationRouteSpec, ...] = (
    SimulationRouteSpec(
        name="List studies",
        method="GET",
        path_template="/api/v1/edc/studies",
        segments=("api", "v1", "edc", "studies"),
        allowed_query_keys=_STANDARD_LIST_QUERY_KEYS,
    ),
    SimulationRouteSpec(
        name="List forms",
        method="GET",
        path_template="/api/v1/edc/studies/{studyKey}/forms",
        segments=("api", "v1", "edc", "studies", ":studyKey", "forms"),
        allowed_query_keys=_STANDARD_LIST_QUERY_KEYS,
    ),
    SimulationRouteSpec(
        name="List variables",
        method="GET",
        path_template="/api/v1/edc/studies/{studyKey}/variables",
        segments=("api", "v1", "edc", "studies", ":studyKey", "variables"),
        allowed_query_keys=_STANDARD_LIST_QUERY_KEYS,
    ),
    SimulationRouteSpec(
        name="List intervals",
        method="GET",
        path_template="/api/v1/edc/studies/{studyKey}/intervals",
        segments=("api", "v1", "edc", "studies", ":studyKey", "intervals"),
        allowed_query_keys=frozenset({"page", "size", "sort"}),
    ),
    SimulationRouteSpec(
        name="List sites",
        method="GET",
        path_template="/api/v1/edc/studies/{studyKey}/sites",
        segments=("api", "v1", "edc", "studies", ":studyKey", "sites"),
        allowed_query_keys=_STANDARD_LIST_QUERY_KEYS,
    ),
    SimulationRouteSpec(
        name="List subjects",
        method="GET",
        path_template="/api/v1/edc/studies/{studyKey}/subjects",
        segments=("api", "v1", "edc", "studies", ":studyKey", "subjects"),
        allowed_query_keys=_STANDARD_LIST_QUERY_KEYS,
    ),
    SimulationRouteSpec(
        name="List records",
        method="GET",
        path_template="/api/v1/edc/studies/{studyKey}/records",
        segments=("api", "v1", "edc", "studies", ":studyKey", "records"),
        allowed_query_keys=_STANDARD_LIST_QUERY_KEYS | {"recordDataFilter"},
    ),
    SimulationRouteSpec(
        name="Create records",
        method="POST",
        path_template="/api/v1/edc/studies/{studyKey}/records",
        segments=("api", "v1", "edc", "studies", ":studyKey", "records"),
        allowed_query_keys=frozenset(),
    ),
    SimulationRouteSpec(
        name="List record revisions",
        method="GET",
        path_template="/api/v1/edc/studies/{studyKey}/recordRevisions",
        segments=("api", "v1", "edc", "studies", ":studyKey", "recordRevisions"),
        allowed_query_keys=_STANDARD_LIST_QUERY_KEYS,
    ),
    SimulationRouteSpec(
        name="List codings",
        method="GET",
        path_template="/api/v1/edc/studies/{studyKey}/codings",
        segments=("api", "v1", "edc", "studies", ":studyKey", "codings"),
        allowed_query_keys=_STANDARD_LIST_QUERY_KEYS,
    ),
    SimulationRouteSpec(
        name="List queries",
        method="GET",
        path_template="/api/v1/edc/studies/{studyKey}/queries",
        segments=("api", "v1", "edc", "studies", ":studyKey", "queries"),
        allowed_query_keys=_STANDARD_LIST_QUERY_KEYS,
    ),
    SimulationRouteSpec(
        name="List visits",
        method="GET",
        path_template="/api/v1/edc/studies/{studyKey}/visits",
        segments=("api", "v1", "edc", "studies", ":studyKey", "visits"),
        allowed_query_keys=_STANDARD_LIST_QUERY_KEYS,
    ),
    SimulationRouteSpec(
        name="List users",
        method="GET",
        path_template="/api/v1/edc/studies/{studyKey}/users",
        segments=("api", "v1", "edc", "studies", ":studyKey", "users"),
        allowed_query_keys=frozenset({"page", "size", "sort", "includeInactive"}),
    ),
    SimulationRouteSpec(
        name="Retrieve job status",
        method="GET",
        path_template="/api/v1/edc/studies/{studyKey}/jobs/{batchId}",
        segments=("api", "v1", "edc", "studies", ":studyKey", "jobs", ":batchId"),
        allowed_query_keys=frozenset(),
    ),
)


class _FilterParseError(ValueError):
    """Internal error raised when a simulation query parameter violates API syntax."""

    def __init__(self, message: str, *, attribute: str = "filter", value: str = "") -> None:
        super().__init__(message)
        self.attribute = attribute
        self.value = value


@dataclass(frozen=True)
class _FilterClause:
    """Parsed comparison clause for filter or recordDataFilter evaluation."""

    field: str
    operator: str
    value: str
    was_quoted: bool
    regex: re.Pattern[str] | None = None


def _to_camel_or_snake(key: str) -> list[str]:
    """Return exact, snake_case, camelCase, and UPPER_SNAKE_CASE representations of a key."""
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", key)
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()
    parts = snake.split("_")
    camel = parts[0] + "".join(word.capitalize() for word in parts[1:])
    return list(dict.fromkeys([key, snake, camel, snake.upper()]))


def _parse_iso_datetime(raw: str) -> datetime | None:
    """Parse an ISO-8601 date or timestamp string into a UTC-normalized datetime."""
    text = raw.strip()
    if not _ISO_DATE_PREFIX_RE.match(text):
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _parse_float(raw: str) -> float | None:
    """Parse a finite numeric string to float, returning None if non-numeric."""
    try:
        val = float(raw)
    except (ValueError, TypeError):
        return None
    if not math.isfinite(val):
        return None
    return val


def _split_filter_clauses(filter_str: str, *, attribute: str) -> tuple[str, list[_FilterClause]]:
    """Validate and parse a filter or recordDataFilter string using shared SDK syntax rules."""
    allow_regex = attribute == "recordDataFilter"
    try:
        if allow_regex:
            validate_record_filter(filter_str)
        else:
            validate_filter_syntax(filter_str)
    except ValueError as exc:
        raise _FilterParseError(str(exc), attribute=attribute, value=filter_str) from exc

    stripped = filter_str.strip()
    if not stripped or stripped.lower() == "none":
        return "and", []

    raw_clauses, connector_char = _split_raw_filter_clauses(stripped)
    clauses: list[_FilterClause] = []
    for raw_clause in raw_clauses:
        try:
            parsed = _parse_clause_operator(raw_clause)
        except ValueError as exc:
            raise _FilterParseError(str(exc), attribute=attribute, value=filter_str) from exc
        if parsed is None:
            raise _FilterParseError(
                f"Invalid filter clause '{raw_clause}': missing comparison operator",
                attribute=attribute,
                value=filter_str,
            )
        raw_key, matched_op, raw_val = parsed
        try:
            validate_filter_operator(matched_op, allow_regex=allow_regex)
            validate_filter_key(raw_key)
        except ValueError as exc:
            raise _FilterParseError(str(exc), attribute=attribute, value=filter_str) from exc

        if "." in raw_key and any(not segment for segment in raw_key.split(".")):
            raise _FilterParseError(
                f"Invalid dotted filter key '{raw_key}': empty path segment",
                attribute=attribute,
                value=filter_str,
            )

        try:
            unquoted_val, was_quoted = _unquote_filter_scalar(raw_val, filter_str)
        except ValueError as exc:
            raise _FilterParseError(str(exc), attribute=attribute, value=filter_str) from exc

        compiled_re: re.Pattern[str] | None = None
        if matched_op == "=~":
            try:
                compiled_re = re.compile(unquoted_val, re.IGNORECASE)
            except re.error as exc:
                raise _FilterParseError(
                    f"Invalid regular expression '{unquoted_val}': {exc}",
                    attribute=attribute,
                    value=filter_str,
                ) from exc

        clauses.append(
            _FilterClause(
                field=raw_key,
                operator=matched_op,
                value=unquoted_val,
                was_quoted=was_quoted,
                regex=compiled_re,
            )
        )

    connector = "or" if connector_char == "," else "and"
    return connector, clauses


def _parse_pagination_params(params: dict[str, str]) -> tuple[int, int]:
    """Validate and parse ``page`` (>= 0) and ``size`` (1..MAX_PAGE_SIZE) via shared paginator rules."""
    page = 0
    if "page" in params:
        raw_page = params["page"]
        if not raw_page or raw_page != raw_page.strip():
            raise _FilterParseError(
                f"Pagination 'page' must be an integer, got {raw_page!r}.",
                attribute="page",
                value=raw_page,
            )
        try:
            page = _parse_int_param("page", raw_page)
        except ValueError as exc:
            raise _FilterParseError(str(exc), attribute="page", value=raw_page) from exc
        if page < 0:
            raise _FilterParseError(
                f"Pagination 'page' must be >= 0, got {page}.",
                attribute="page",
                value=raw_page,
            )

    size = 25
    if "size" in params:
        raw_size = params["size"]
        if not raw_size or raw_size != raw_size.strip():
            raise _FilterParseError(
                f"Pagination 'size' must be an integer, got {raw_size!r}.",
                attribute="size",
                value=raw_size,
            )
        try:
            size = _parse_int_param("size", raw_size)
        except ValueError as exc:
            raise _FilterParseError(str(exc), attribute="size", value=raw_size) from exc
        if size < 1 or size > MAX_PAGE_SIZE:
            raise _FilterParseError(
                f"Pagination 'size' must be between 1 and {MAX_PAGE_SIZE}, got {size}.",
                attribute="size",
                value=raw_size,
            )

    return page, size


def _match_route_segments(
    template_segments: tuple[str, ...], actual_segments: tuple[str, ...]
) -> dict[str, str] | None:
    """Match decoded URL path segments against a route template, extracting `:param` bindings."""
    if len(template_segments) != len(actual_segments):
        return None
    bindings: dict[str, str] = {}
    for tmpl, actual in zip(template_segments, actual_segments, strict=True):
        if tmpl.startswith(":"):
            bindings[tmpl[1:]] = actual
        elif tmpl != actual:
            return None
    return bindings


def _parse_records_post_body(request: httpx.Request) -> list[dict[str, Any]]:
    """Validate that a POST /records body is a non-empty JSON array of non-empty objects."""
    raw_bytes = request.content
    if not raw_bytes or not raw_bytes.strip():
        raise _FilterParseError(
            "Record creation request body cannot be empty",
            attribute="body",
            value="",
        )
    try:
        parsed = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _FilterParseError(
            f"Malformed JSON request body: {exc}",
            attribute="body",
            value="",
        ) from exc

    if not isinstance(parsed, list) or not parsed:
        raise _FilterParseError(
            "Record creation request body must be a non-empty JSON array of objects",
            attribute="body",
            value=type(parsed).__name__,
        )

    validated: list[dict[str, Any]] = []
    for idx, entry in enumerate(parsed):
        if not isinstance(entry, dict) or not entry:
            raise _FilterParseError(
                f"Record item at index {idx} must be a non-empty JSON object",
                attribute=f"body[{idx}]",
                value=type(entry).__name__,
            )
        for data_key in ("data", "recordData", "record_data"):
            if (
                data_key in entry
                and entry[data_key] is not None
                and not isinstance(entry[data_key], dict)
            ):
                raise _FilterParseError(
                    f"Record item at index {idx} '{data_key}' must be a JSON object",
                    attribute=f"body[{idx}].{data_key}",
                    value=type(entry[data_key]).__name__,
                )
        validated.append(entry)
    return validated


def _apply_ordering_op(op: str, left: Any, right: Any) -> bool:
    """Apply an ordering or equality operator to two mutually comparable values."""
    if op == "==":
        return bool(left == right)
    if op == "!=":
        return bool(left != right)
    if op == "<":
        return bool(left < right)
    if op == "<=":
        return bool(left <= right)
    if op == ">":
        return bool(left > right)
    if op == ">=":
        return bool(left >= right)
    return False


def _evaluate_value(actual_val: Any, clause: _FilterClause) -> bool:
    """Evaluate a parsed filter clause against a single field value without raising."""
    if actual_val is None:
        return False

    op = clause.operator
    expected = clause.value

    if op == "=~":
        if isinstance(actual_val, (dict, list)):
            return False
        assert clause.regex is not None
        return bool(clause.regex.search(str(actual_val)))

    if isinstance(actual_val, (dict, list)):
        return op == "!="

    if isinstance(actual_val, bool):
        lowered = expected.strip().lower()
        if lowered == "true":
            exp_bool = True
        elif lowered == "false":
            exp_bool = False
        else:
            return op == "!="
        return _apply_ordering_op(op, actual_val, exp_bool)

    if isinstance(actual_val, (int, float)):
        exp_num = _parse_float(expected)
        if exp_num is None:
            return op == "!="
        return _apply_ordering_op(op, float(actual_val), exp_num)

    actual_str = str(actual_val)
    expected_dt = _parse_iso_datetime(expected)
    if expected_dt is not None:
        actual_dt = _parse_iso_datetime(actual_str)
        if actual_dt is not None:
            return _apply_ordering_op(op, actual_dt, expected_dt)
        if not clause.was_quoted and op in ("<", "<=", ">", ">="):
            return False

    if not clause.was_quoted and op in ("<", "<=", ">", ">="):
        expected_num = _parse_float(expected)
        if expected_num is not None:
            actual_num = _parse_float(actual_str)
            if actual_num is None:
                return False
            return _apply_ordering_op(op, actual_num, expected_num)

    return _apply_ordering_op(op, actual_str, expected)


def _lookup_field(mapping: dict[str, Any], field: str) -> tuple[bool, Any]:
    """Resolve field value from a dict across naming variants, traversing dotted paths."""
    for candidate in _to_camel_or_snake(field):
        if candidate in mapping:
            return True, mapping[candidate]

    if "." not in field:
        return False, None

    segments = field.split(".")
    if any(not segment for segment in segments):
        return False, None

    current: Any = mapping
    for idx, segment in enumerate(segments):
        if not isinstance(current, dict):
            return False, None
        if idx > 0:
            remaining = ".".join(segments[idx:])
            for candidate in _to_camel_or_snake(remaining):
                if candidate in current:
                    return True, current[candidate]
        matched = False
        for candidate in _to_camel_or_snake(segment):
            if candidate in current:
                current = current[candidate]
                matched = True
                break
        if not matched:
            return False, None
    return True, current


def _matches_clauses(mapping: dict[str, Any], connector: str, clauses: list[_FilterClause]) -> bool:
    """Evaluate AND/OR clauses against a target dictionary, skipping missing keys."""
    if not clauses:
        return True

    def _eval_one(clause: _FilterClause) -> bool:
        found, actual_val = _lookup_field(mapping, clause.field)
        if not found:
            return False
        return _evaluate_value(actual_val, clause)

    if connector == "or":
        return any(_eval_one(c) for c in clauses)
    return all(_eval_one(c) for c in clauses)


def _parse_sort_specs(raw_sorts: list[str]) -> list[dict[str, str]]:
    """Parse `sort` query values into a list of `{'property': ..., 'direction': ...}`."""
    specs: list[dict[str, str]] = []
    for raw in raw_sorts:
        if not raw or not raw.strip():
            continue
        for token in raw.split(";"):
            part = token.strip()
            if not part:
                continue
            if "," in part:
                prop_raw, dir_raw = part.split(",", 1)
            elif ":" in part:
                prop_raw, dir_raw = part.split(":", 1)
            else:
                prop_raw, dir_raw = part, "ASC"

            prop = prop_raw.strip()
            direction = dir_raw.strip().upper()
            if not prop:
                raise _FilterParseError(
                    "Sort property cannot be empty", attribute="sort", value=raw
                )
            if direction not in ("ASC", "DESC"):
                raise _FilterParseError(
                    f"Invalid sort direction '{dir_raw}'. Expected ASC or DESC.",
                    attribute="sort",
                    value=raw,
                )
            specs.append({"property": prop, "direction": direction})
    return specs


def _normalize_sort_value(val: Any) -> tuple[int, float, str]:
    """Produce a mutually comparable tuple for heterogeneous non-None values."""
    if isinstance(val, bool):
        return (0, float(int(val)), "")
    if isinstance(val, (int, float)):
        return (1, float(val), "")
    if isinstance(val, str):
        dt = _parse_iso_datetime(val)
        if dt is not None:
            return (2, dt.timestamp(), val)
        return (3, 0.0, val)
    return (4, 0.0, str(val))


def _apply_sort(
    items: list[dict[str, Any]], sort_specs: list[dict[str, str]]
) -> list[dict[str, Any]]:
    """Stably sort items across all sort specifications, placing missing/None values last."""
    result = list(items)
    for spec in reversed(sort_specs):
        prop = spec["property"]
        reverse = spec["direction"] == "DESC"
        present: list[tuple[tuple[int, float, str], int, dict[str, Any]]] = []
        missing: list[dict[str, Any]] = []
        for idx, item in enumerate(result):
            found, val = _lookup_field(item, prop)
            if not found and isinstance(item.get("recordData"), dict):
                found, val = _lookup_field(item["recordData"], prop)
            if not found or val is None:
                missing.append(item)
            else:
                present.append((_normalize_sort_value(val), idx, item))

        present.sort(key=lambda entry: entry[0], reverse=reverse)
        result = [entry[2] for entry in present] + missing
    return result


class SimulationTransport(httpx.BaseTransport, httpx.AsyncBaseTransport):
    """In-memory HTTP transport implementing offline clinical REST simulation."""

    def __init__(self, state: SimulationState | None = None) -> None:
        """Initialize the transport with an in-memory state store."""
        self.state = state or get_default_simulation_state()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Handle synchronous HTTP request offline without network sockets."""
        request.read()
        return self._dispatch(request)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Handle asynchronous HTTP request offline without network sockets."""
        await request.aread()
        return self._dispatch(request)

    def _validate_headers(self, request: httpx.Request) -> httpx.Response | None:
        """Validate authentication credentials (API keys or OIDC Bearer) and Accept header."""
        auth_header = request.headers.get("authorization")
        has_valid_bearer = False
        if auth_header is not None:
            stripped_auth = auth_header.strip()
            if stripped_auth.lower().startswith("bearer "):
                has_valid_bearer = bool(stripped_auth[7:].strip())

        api_key = request.headers.get("x-api-key", "").strip()
        security_key = request.headers.get("x-imn-security-key", "").strip()
        has_valid_api_keys = bool(api_key) and bool(security_key)

        if not (has_valid_api_keys or has_valid_bearer):
            return self._error_response(
                request,
                401,
                "UNAUTHORIZED",
                "Missing or blank authentication credentials",
                code="9001",
            )

        accept = request.headers.get("accept", "").lower()
        if "application/json" not in accept:
            return self._error_response(
                request,
                400,
                "BAD_REQUEST",
                "Accept header must include application/json",
                code="1000",
                field={"attribute": "Accept", "value": request.headers.get("accept", "")},
            )
        return None

    def _dispatch(self, request: httpx.Request) -> httpx.Response:
        """Dispatch HTTP request strictly against the 14 documented EDC operations."""
        header_err = self._validate_headers(request)
        if header_err is not None:
            return header_err

        method = request.method.upper()
        raw_path = request.url.path
        if not raw_path.startswith("/"):
            return self._not_found_response(request, f"Endpoint {raw_path} not found")
        raw_parts = raw_path.split("/")[1:]
        if not raw_parts or any(not part for part in raw_parts):
            return self._not_found_response(request, f"Endpoint {raw_path} not found")
        decoded_segments = tuple(urllib.parse.unquote(part) for part in raw_parts)
        if any(not seg or "/" in seg for seg in decoded_segments):
            return self._not_found_response(request, f"Endpoint {raw_path} not found")

        path_matches: list[tuple[SimulationRouteSpec, dict[str, str]]] = []
        for spec in SIMULATION_ROUTES:
            bindings = _match_route_segments(spec.segments, decoded_segments)
            if bindings is not None:
                path_matches.append((spec, bindings))

        if not path_matches:
            return self._not_found_response(request, f"Endpoint {raw_path} not found")

        matched_pair = next(((spec, b) for spec, b in path_matches if spec.method == method), None)
        if matched_pair is None:
            allowed_methods = ", ".join(sorted({spec.method for spec, _ in path_matches}))
            return self._error_response(
                request,
                405,
                "METHOD_NOT_ALLOWED",
                f"Method {method} not allowed for {raw_path}",
                extra_headers={"Allow": allowed_methods},
            )

        route, bindings = matched_pair
        study_key = bindings.get("studyKey")
        if study_key is not None and not self.state.is_registered_study(study_key):
            return self._not_found_response(request, f"Study {study_key} not found")

        unexpected_keys = [key for key in request.url.params if key not in route.allowed_query_keys]
        if unexpected_keys:
            bad_key = unexpected_keys[0]
            return self._bad_request_response(
                request,
                _FilterParseError(
                    f"Unsupported query parameter(s) for {route.name}: {', '.join(unexpected_keys)}",
                    attribute=bad_key,
                    value=request.url.params.get(bad_key, ""),
                ),
            )

        params = dict(request.url.params)

        if route.name == "List studies":
            return self._filter_and_paginate(request, self.state.get_studies(), params)

        assert study_key is not None

        if route.name == "Retrieve job status":
            batch_id = bindings["batchId"]
            job_status = self.state.get_job(study_key, batch_id)
            if job_status is None:
                return self._not_found_response(request, f"Job {batch_id} not found")
            return httpx.Response(
                200,
                json=job_status,
                headers={"content-type": "application/json"},
                request=request,
            )

        if route.name == "Create records":
            try:
                records_data = _parse_records_post_body(request)
                job_res = self.state.create_records(study_key, records_data)
            except _FilterParseError as exc:
                return self._bad_request_response(request, exc)
            except ValueError as exc:
                return self._bad_request_response(
                    request, _FilterParseError(str(exc), attribute="body", value="")
                )
            return httpx.Response(
                202,
                json=job_res,
                headers={"content-type": "application/json"},
                request=request,
            )

        if route.name == "List users":
            raw_inactive = params.get("includeInactive", "false")
            norm_inactive = raw_inactive.strip().lower()
            if norm_inactive not in {"true", "false"}:
                return self._bad_request_response(
                    request,
                    _FilterParseError(
                        f"Query parameter 'includeInactive' must be 'true' or 'false', got {raw_inactive!r}",
                        attribute="includeInactive",
                        value=raw_inactive,
                    ),
                )
            items = self.state.get_users(study_key)
            if norm_inactive != "true":
                items = [u for u in items if u.get("userActiveInStudy", True) is not False]
            return self._filter_and_paginate(request, items, params)

        study_getters = {
            "List forms": self.state.get_forms,
            "List variables": self.state.get_variables,
            "List intervals": self.state.get_intervals,
            "List sites": self.state.get_sites,
            "List subjects": self.state.get_subjects,
            "List records": self.state.get_records,
            "List record revisions": self.state.get_record_revisions,
            "List codings": self.state.get_codings,
            "List queries": self.state.get_queries,
            "List visits": self.state.get_visits,
        }
        items = study_getters[route.name](study_key)
        return self._filter_and_paginate(request, items, params)

    def _error_response(
        self,
        request: httpx.Request,
        status_code: int,
        status_text: str,
        message: str,
        *,
        code: str | None = None,
        field: dict[str, str] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Build a contract-compliant HTTP error envelope."""
        now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        resolved_code = (
            code
            if code is not None
            else (
                "1000" if status_code == 400 else "9001" if status_code == 401 else str(status_code)
            )
        )
        error_body: dict[str, Any] = {
            "code": resolved_code,
            "description": message,
            "message": message,
        }
        if field is not None:
            error_body["field"] = field
        payload = {
            "metadata": {
                "status": status_text,
                "method": request.method.upper(),
                "path": request.url.path,
                "timestamp": now_iso,
                "error": error_body,
            }
        }
        headers = {"content-type": "application/json"}
        if extra_headers:
            headers.update(extra_headers)
        return httpx.Response(
            status_code,
            json=payload,
            headers=headers,
            request=request,
        )

    def _not_found_response(self, request: httpx.Request, message: str) -> httpx.Response:
        """Build a contract-compliant HTTP 404 Not Found error envelope."""
        return self._error_response(request, 404, "NOT_FOUND", message)

    def _bad_request_response(
        self, request: httpx.Request, exc: _FilterParseError
    ) -> httpx.Response:
        """Build a contract-compliant HTTP 400 error envelope with code '1000'."""
        return self._error_response(
            request,
            400,
            "BAD_REQUEST",
            str(exc),
            code="1000",
            field={"attribute": exc.attribute, "value": exc.value},
        )

    def _filter_and_paginate(
        self, request: httpx.Request, items: list[dict[str, Any]], params: dict[str, str]
    ) -> httpx.Response:
        """Apply filter, recordDataFilter, sort, and shared pagination bounds."""
        try:
            page, size = _parse_pagination_params(params)

            filter_str = params.get("filter")
            if filter_str is not None:
                connector, clauses = _split_filter_clauses(filter_str, attribute="filter")
                if clauses:
                    items = [item for item in items if _matches_clauses(item, connector, clauses)]

            rec_filter_str = params.get("recordDataFilter")
            project_none_record_data = False
            if rec_filter_str is not None:
                if rec_filter_str.strip().lower() == "none":
                    project_none_record_data = True
                elif rec_filter_str.strip():
                    rec_conn, rec_clauses = _split_filter_clauses(
                        rec_filter_str, attribute="recordDataFilter"
                    )
                    if rec_clauses:
                        items = [
                            item
                            for item in items
                            if isinstance(item.get("recordData"), dict)
                            and _matches_clauses(item["recordData"], rec_conn, rec_clauses)
                        ]

            raw_sorts = list(request.url.params.get_list("sort"))
            if not raw_sorts and "sort" in params:
                raw_sorts = [params["sort"]]
            sort_specs = _parse_sort_specs(raw_sorts)
            if sort_specs:
                items = _apply_sort(items, sort_specs)

            if project_none_record_data:
                items = [
                    {**item, "recordData": None} if "recordData" in item else item for item in items
                ]
        except _FilterParseError as exc:
            return self._bad_request_response(request, exc)

        return self._paginate_response(request, items, page=page, size=size, sort_specs=sort_specs)

    def _paginate_response(
        self,
        request: httpx.Request,
        items: list[dict[str, Any]],
        *,
        page: int,
        size: int,
        sort_specs: list[dict[str, str]] | None = None,
    ) -> httpx.Response:
        """Format items into standard iMedNet ApiResponse JSON envelope."""
        total_elements = len(items)
        if total_elements == 0:
            total_pages = 0
            page_items: list[dict[str, Any]] = []
        else:
            total_pages = max(1, (total_elements + size - 1) // size)
            start_idx = page * size
            end_idx = start_idx + size
            page_items = items[start_idx:end_idx]

        now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = {
            "metadata": {
                "status": "OK",
                "method": request.method.upper(),
                "path": request.url.path,
                "timestamp": now_iso,
                "error": {},
            },
            "pagination": {
                "currentPage": page,
                "size": size,
                "totalPages": total_pages,
                "totalElements": total_elements,
                "sort": sort_specs or [],
            },
            "data": page_items,
        }
        return httpx.Response(
            200,
            json=payload,
            headers={"content-type": "application/json"},
            request=request,
        )


def create_simulation_transport(state: SimulationState | None = None) -> SimulationTransport:
    """Create a new SimulationTransport instance."""
    return SimulationTransport(state=state)
