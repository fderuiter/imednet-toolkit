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

from imednet.constants import MAX_PAGE_SIZE
from imednet.endpoints.registry import ENDPOINT_REGISTRY
from imednet.simulation.state import SimulationState, get_default_simulation_state
from imednet.utils.filters import (
    ORDERED_FILTER_OPERATORS,
    _snake_to_camel,
    extract_filter_attributes,
    validate_filter_key,
    validate_filter_syntax,
    validate_record_filter,
)

_ISO_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}|\b|$)")
_TWO_CHAR_OPS = frozenset(op for op in ORDERED_FILTER_OPERATORS if len(op) == 2)
_ONE_CHAR_OPS = frozenset({*(op for op in ORDERED_FILTER_OPERATORS if len(op) == 1), "="})


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


def _unquote_filter_value(raw_value: str, *, attribute: str, full_expr: str) -> tuple[str, bool]:
    """Unquote a filter scalar token, validating backslash escapes inside double quotes."""
    if not raw_value:
        raise _FilterParseError(
            "Filter value cannot be empty", attribute=attribute, value=full_expr
        )
    if raw_value.startswith('"'):
        if len(raw_value) < 2 or not raw_value.endswith('"'):
            raise _FilterParseError(
                "Filter expression contains unclosed quotation marks",
                attribute=attribute,
                value=full_expr,
            )
        inner = raw_value[1:-1]
        chars: list[str] = []
        idx = 0
        while idx < len(inner):
            ch = inner[idx]
            if ch == "\\":
                if idx + 1 >= len(inner):
                    raise _FilterParseError(
                        "Filter expression contains trailing escape in quotation marks",
                        attribute=attribute,
                        value=full_expr,
                    )
                chars.append(inner[idx + 1])
                idx += 2
                continue
            if ch == '"':
                raise _FilterParseError(
                    "Filter expression contains unescaped quotation mark",
                    attribute=attribute,
                    value=full_expr,
                )
            chars.append(ch)
            idx += 1
        return "".join(chars), True

    if '"' in raw_value:
        raise _FilterParseError(
            "Filter expression contains unexpected quotation mark",
            attribute=attribute,
            value=full_expr,
        )
    return raw_value, False


def _split_filter_clauses(filter_str: str, *, attribute: str) -> tuple[str, list[_FilterClause]]:
    """Tokenize a filter string into ('and'|'or', clauses), validating connectors and regexes."""
    stripped = filter_str.strip()
    if not stripped or stripped.lower() == "none":
        return "and", []

    tokens: list[str] = []
    buf: list[str] = []
    in_quotes = False
    escape = False
    has_and = False
    has_or = False
    idx = 0
    length = len(filter_str)

    while idx < length:
        ch = filter_str[idx]
        if escape:
            buf.append(ch)
            escape = False
            idx += 1
            continue
        if ch == "\\" and in_quotes:
            buf.append(ch)
            escape = True
            idx += 1
            continue
        if ch == '"':
            in_quotes = not in_quotes
            buf.append(ch)
            idx += 1
            continue

        if not in_quotes:
            if ch == ";":
                has_and = True
                tokens.append("".join(buf))
                buf = []
                idx += 1
                continue
            if ch == ",":
                has_or = True
                tokens.append("".join(buf))
                buf = []
                idx += 1
                continue

        buf.append(ch)
        idx += 1

    if in_quotes:
        raise _FilterParseError(
            "Filter expression contains unclosed quotation marks",
            attribute=attribute,
            value=filter_str,
        )
    if has_and and has_or:
        raise _FilterParseError(
            "Filter expression cannot combine both AND and OR connectors",
            attribute=attribute,
            value=filter_str,
        )

    tokens.append("".join(buf))
    clauses = [
        _parse_single_clause(tok, attribute=attribute, full_expr=filter_str) for tok in tokens
    ]
    connector = "or" if has_or else "and"
    if connector == "and":
        seen_and_attrs: set[str] = set()
        for clause in clauses:
            dedup_attr = (
                clause.field if attribute == "recordDataFilter" else _snake_to_camel(clause.field)
            )
            if dedup_attr in seen_and_attrs:
                raise _FilterParseError(
                    f"Duplicate filter attribute '{dedup_attr}' in AND clause",
                    attribute=attribute,
                    value=filter_str,
                )
            seen_and_attrs.add(dedup_attr)
    return connector, clauses


def _parse_single_clause(raw_clause: str, *, attribute: str, full_expr: str) -> _FilterClause:
    """Parse one `field<op>value` clause and pre-compile regex patterns for `=~`."""
    clause = raw_clause.strip()
    if not clause:
        raise _FilterParseError(
            "Empty filter clause in expression", attribute=attribute, value=full_expr
        )

    matched_op: str | None = None
    op_idx = -1
    for idx in range(len(clause)):
        if clause[idx] == '"':
            break
        two_char = clause[idx : idx + 2]
        if len(two_char) == 2 and two_char in _TWO_CHAR_OPS:
            matched_op = two_char
            op_idx = idx
            break
        one_char = clause[idx]
        if one_char in _ONE_CHAR_OPS:
            matched_op = one_char
            op_idx = idx
            break

    if matched_op is None:
        raise _FilterParseError(
            f"Invalid filter clause '{clause}': missing comparison operator",
            attribute=attribute,
            value=full_expr,
        )

    raw_key = clause[:op_idx].strip()
    raw_val = clause[op_idx + len(matched_op) :].strip()
    if (matched_op == "==" and raw_val.startswith("=")) or (
        matched_op == "<" and raw_val.startswith(">")
    ):
        raise _FilterParseError(
            f"Unsupported operator in filter clause '{clause}'",
            attribute=attribute,
            value=full_expr,
        )

    try:
        validate_filter_key(raw_key)
    except ValueError as exc:
        raise _FilterParseError(str(exc), attribute=attribute, value=full_expr) from exc
    if "." in raw_key and any(not segment for segment in raw_key.split(".")):
        raise _FilterParseError(
            f"Invalid dotted filter key '{raw_key}': empty path segment",
            attribute=attribute,
            value=full_expr,
        )

    unquoted_val, was_quoted = _unquote_filter_value(
        raw_val, attribute=attribute, full_expr=full_expr
    )
    normalized_op = "==" if matched_op == "=" else matched_op

    compiled_re: re.Pattern[str] | None = None
    if normalized_op == "=~":
        try:
            compiled_re = re.compile(unquoted_val, re.IGNORECASE)
        except re.error as exc:
            raise _FilterParseError(
                f"Invalid regular expression '{unquoted_val}': {exc}",
                attribute=attribute,
                value=full_expr,
            ) from exc

    return _FilterClause(
        field=raw_key,
        operator=normalized_op,
        value=unquoted_val,
        was_quoted=was_quoted,
        regex=compiled_re,
    )


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


_STUDY_SUBROUTES: dict[str, tuple[str, Any]] | None = None


def _get_study_subroutes() -> dict[str, tuple[str, Any]]:
    """Return cached ``{cls.PATH: (endpoint_key, endpoint_cls)}`` for Study sub-routes."""
    global _STUDY_SUBROUTES
    if _STUDY_SUBROUTES is None:
        _STUDY_SUBROUTES = {
            str(cls.PATH): (key, cls)
            for key, cls in ENDPOINT_REGISTRY.items()
            if getattr(cls, "PATH", "")
        }
    return _STUDY_SUBROUTES


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

    def _get_study_resource_items(
        self, endpoint_key: str, study_key: str, params: dict[str, str]
    ) -> list[dict[str, Any]]:
        """Return raw Study-scoped fixture dictionaries for a backed list endpoint."""
        if endpoint_key == "sites":
            return self.state.get_sites(study_key)
        if endpoint_key == "subjects":
            return self.state.get_subjects(study_key)
        if endpoint_key == "forms":
            return self.state.get_forms(study_key)
        if endpoint_key == "visits":
            return self.state.get_visits(study_key)
        if endpoint_key == "records":
            return self.state.get_records(study_key)
        if endpoint_key == "record_revisions":
            return self.state.get_record_revisions(study_key)
        if endpoint_key == "codings":
            return self.state.get_codings(study_key)
        if endpoint_key == "queries":
            return self.state.get_queries(study_key)
        if endpoint_key == "intervals":
            return self.state.get_intervals(study_key)
        if endpoint_key == "variables":
            return self.state.get_variables(study_key)
        if endpoint_key == "users":
            items = self.state.get_users(study_key)
            include_inactive = params.get("includeInactive", "false").strip().lower() == "true"
            if not include_inactive:
                items = [u for u in items if u.get("userActiveInStudy", True) is not False]
            return items
        return []

    def _dispatch(self, request: httpx.Request) -> httpx.Response:
        """Dispatch HTTP request to in-memory fixtures backed by ``ENDPOINT_REGISTRY``."""
        method = request.method.upper()
        raw_path = request.url.path
        raw_segments = [urllib.parse.unquote(s) for s in raw_path.strip("/").split("/") if s]

        # Strip prefixes like api/v1 or edc
        segments = list(raw_segments)
        if len(segments) >= 2 and segments[0].lower() == "api" and segments[1].lower() == "v1":
            segments = segments[2:]
        if len(segments) >= 1 and segments[0].lower() == "edc":
            segments = segments[1:]

        params = dict(request.url.params)

        # 1. Studies: GET /studies
        if segments == ["studies"]:
            studies_cls = ENDPOINT_REGISTRY["studies"]
            if method != "GET" or not hasattr(studies_cls, "list"):
                return self._not_found_response(request, f"Route {method} {raw_path} not found")
            items = self.state.get_studies()
            return self._filter_and_paginate(request, items, params, endpoint_key="studies")

        # 2. Study-level resources: /studies/{study_key}/{resource}[/{item_id}]
        if len(segments) >= 2 and segments[0] == "studies":
            study_key = segments[1]
            if len(segments) == 2:
                if not self.state.has_study(study_key):
                    return self._not_found_response(request, f"Study '{study_key}' not found")
                return self._not_found_response(request, f"Route {method} {raw_path} not found")

            resource = segments[2]
            subroute = _get_study_subroutes().get(resource)
            if subroute is None:
                return self._not_found_response(request, f"Route {method} {raw_path} not found")

            if not self.state.has_study(study_key):
                return self._not_found_response(request, f"Study '{study_key}' not found")

            endpoint_key, endpoint_cls = subroute
            if not hasattr(endpoint_cls, "list"):
                if method != "GET" or len(segments) != 4 or not hasattr(endpoint_cls, "get"):
                    return self._not_found_response(request, f"Route {method} {raw_path} not found")
                batch_id = segments[3]
                job_status = self.state.get_job(study_key, batch_id)
                if job_status is None:
                    return self._not_found_response(request, f"Job {batch_id} not found")
                return httpx.Response(
                    200,
                    json=job_status,
                    headers={"content-type": "application/json"},
                    request=request,
                )

            if len(segments) != 3:
                return self._not_found_response(request, f"Route {method} {raw_path} not found")

            if method == "POST":
                if not hasattr(endpoint_cls, "create"):
                    return self._not_found_response(request, f"Route {method} {raw_path} not found")
                records_data: list[Any] = []
                if request.content:
                    try:
                        parsed = json.loads(request.content)
                        if isinstance(parsed, list):
                            records_data = parsed
                        elif isinstance(parsed, dict):
                            records_data = [parsed]
                    except Exception:
                        records_data = []
                job_res = self.state.create_records(study_key, records_data)
                return httpx.Response(
                    202,
                    json=job_res,
                    headers={"content-type": "application/json"},
                    request=request,
                )

            if method != "GET":
                return self._not_found_response(request, f"Route {method} {raw_path} not found")

            items = self._get_study_resource_items(endpoint_key, study_key, params)
            return self._filter_and_paginate(request, items, params, endpoint_key=endpoint_key)

        return self._not_found_response(request, f"Route {method} {raw_path} not found")

    def _error_response(
        self,
        request: httpx.Request,
        status_code: int,
        status_text: str,
        message: str,
        *,
        field: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Build a contract-compliant HTTP error envelope."""
        now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        error_body: dict[str, Any] = {
            "code": str(status_code),
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
        return httpx.Response(
            status_code,
            json=payload,
            headers={"content-type": "application/json"},
            request=request,
        )

    def _not_found_response(self, request: httpx.Request, message: str) -> httpx.Response:
        """Build a contract-compliant HTTP 404 Not Found error envelope."""
        return self._error_response(request, 404, "NOT_FOUND", message)

    def _bad_request_response(
        self, request: httpx.Request, exc: _FilterParseError
    ) -> httpx.Response:
        """Build a contract-compliant HTTP 400 error envelope."""
        return self._error_response(
            request,
            400,
            "BAD_REQUEST",
            str(exc),
            field={"attribute": exc.attribute, "value": exc.value},
        )

    def _filter_and_paginate(
        self,
        request: httpx.Request,
        items: list[dict[str, Any]],
        params: dict[str, str],
        *,
        endpoint_key: str,
    ) -> httpx.Response:
        """Validate and apply filter, recordDataFilter, sort, and pagination parameters."""
        endpoint_cls = ENDPOINT_REGISTRY[endpoint_key]
        allowed_attrs: frozenset[str] = getattr(endpoint_cls, "FILTER_ATTRIBUTES", frozenset())
        try:
            if "filter" in params:
                filter_str = params["filter"]
                if not allowed_attrs:
                    raise _FilterParseError(
                        f"Unsupported filter parameter for {endpoint_cls.__name__}: "
                        "filter is not supported.",
                        attribute="filter",
                        value=filter_str,
                    )
                try:
                    validate_filter_syntax(filter_str)
                except ValueError as exc:
                    raise _FilterParseError(str(exc), attribute="filter", value=filter_str) from exc
                if filter_str.strip() and filter_str.strip().lower() != "none":
                    for attr in extract_filter_attributes(filter_str):
                        try:
                            validate_filter_key(attr)
                        except ValueError as exc:
                            raise _FilterParseError(
                                str(exc), attribute="filter", value=filter_str
                            ) from exc
                        if attr not in allowed_attrs:
                            allowed_str = ", ".join(sorted(allowed_attrs))
                            raise _FilterParseError(
                                f"Unsupported filter attribute '{attr}' for {endpoint_cls.__name__}. "
                                f"Allowed filter attributes: {allowed_str}.",
                                attribute="filter",
                                value=filter_str,
                            )
                    connector, clauses = _split_filter_clauses(filter_str, attribute="filter")
                    if clauses:
                        items = [
                            item for item in items if _matches_clauses(item, connector, clauses)
                        ]

            project_none_record_data = False
            if "recordDataFilter" in params:
                rec_filter_str = params["recordDataFilter"]
                if endpoint_key != "records":
                    raise _FilterParseError(
                        f"Unsupported parameter 'recordDataFilter' for {endpoint_cls.__name__}.",
                        attribute="recordDataFilter",
                        value=rec_filter_str,
                    )
                try:
                    validate_record_filter(rec_filter_str)
                except ValueError as exc:
                    raise _FilterParseError(
                        str(exc), attribute="recordDataFilter", value=rec_filter_str
                    ) from exc
                if rec_filter_str.strip():
                    if rec_filter_str.strip().lower() == "none":
                        project_none_record_data = True
                    else:
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

            page = 0
            if "page" in params:
                raw_page = params["page"]
                if not raw_page.strip():
                    raise _FilterParseError(
                        f"Pagination 'page' must be a non-negative integer, got {raw_page!r}.",
                        attribute="page",
                        value=str(raw_page),
                    )
                try:
                    page = int(raw_page.strip())
                except (ValueError, TypeError) as exc:
                    raise _FilterParseError(
                        f"Pagination 'page' must be a non-negative integer, got {raw_page!r}.",
                        attribute="page",
                        value=str(raw_page),
                    ) from exc
                if page < 0:
                    raise _FilterParseError(
                        f"Pagination 'page' must be a non-negative integer, got {page}.",
                        attribute="page",
                        value=str(raw_page),
                    )

            size = 25
            if "size" in params:
                raw_size = params["size"]
                if not raw_size.strip():
                    raise _FilterParseError(
                        f"Pagination 'size' must be an integer between 1 and {MAX_PAGE_SIZE}, "
                        f"got {raw_size!r}.",
                        attribute="size",
                        value=str(raw_size),
                    )
                try:
                    size = int(raw_size.strip())
                except (ValueError, TypeError) as exc:
                    raise _FilterParseError(
                        f"Pagination 'size' must be an integer between 1 and {MAX_PAGE_SIZE}, "
                        f"got {raw_size!r}.",
                        attribute="size",
                        value=str(raw_size),
                    ) from exc
                if size < 1 or size > MAX_PAGE_SIZE:
                    raise _FilterParseError(
                        f"Pagination 'size' must be between 1 and {MAX_PAGE_SIZE}, got {size}.",
                        attribute="size",
                        value=str(raw_size),
                    )
        except _FilterParseError as exc:
            return self._bad_request_response(request, exc)

        return self._paginate_response(request, items, page=page, size=size, sort_specs=sort_specs)

    def _paginate_response(
        self,
        request: httpx.Request,
        items: list[dict[str, Any]],
        *,
        page: int = 0,
        size: int = 25,
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
