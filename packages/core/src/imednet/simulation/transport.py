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

from imednet.simulation.state import SimulationState, get_default_simulation_state
from imednet.utils.filters import ORDERED_FILTER_OPERATORS, validate_filter_key

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
            if (
                ch.isspace()
                and idx + 4 < length
                and filter_str[idx + 1 : idx + 4].lower() == "and"
                and filter_str[idx + 4].isspace()
            ):
                has_and = True
                tokens.append("".join(buf))
                buf = []
                idx += 5
                continue
            if (
                ch.isspace()
                and idx + 3 < length
                and filter_str[idx + 1 : idx + 3].lower() == "or"
                and filter_str[idx + 3].isspace()
            ):
                has_or = True
                tokens.append("".join(buf))
                buf = []
                idx += 4
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
            compiled_re = re.compile(unquoted_val)
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

    def _dispatch(self, request: httpx.Request) -> httpx.Response:
        """Dispatch HTTP request to in-memory fixtures."""
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

        # Route matching
        # 1. Studies: /studies
        if segments == ["studies"]:
            items = self.state.get_studies()
            return self._filter_and_paginate(request, items, params)

        # 2. Study-level resources: /studies/{study_key}/...
        if len(segments) >= 2 and segments[0] == "studies":
            study_key = segments[1]
            resource = segments[2] if len(segments) >= 3 else None
            sub_id = segments[3] if len(segments) >= 4 else None

            if resource is None:
                # Single study lookup
                items = [s for s in self.state.get_studies() if s.get("studyKey") == study_key]
                if not items:
                    return httpx.Response(
                        404, json={"detail": f"Study {study_key} not found"}, request=request
                    )
                return httpx.Response(200, json=items[0], request=request)

            if resource == "sites":
                items = self.state.get_sites(study_key)
                return self._filter_and_paginate(request, items, params)

            if resource == "subjects":
                items = self.state.get_subjects(study_key)
                return self._filter_and_paginate(request, items, params)

            if resource == "forms":
                items = self.state.get_forms(study_key)
                return self._filter_and_paginate(request, items, params)

            if resource == "visits":
                items = self.state.get_visits(study_key)
                return self._filter_and_paginate(request, items, params)

            if resource == "records":
                if method == "POST":
                    records_data = []
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

                items = self.state.get_records(study_key)
                return self._filter_and_paginate(request, items, params)

            if resource == "recordRevisions":
                items = self.state.get_record_revisions(study_key)
                return self._filter_and_paginate(request, items, params)

            if resource == "codings":
                items = self.state.get_codings(study_key)
                return self._filter_and_paginate(request, items, params)

            if resource == "queries":
                items = self.state.get_queries(study_key)
                return self._filter_and_paginate(request, items, params)

            if resource == "jobs":
                if sub_id is not None:
                    sub_action = segments[4] if len(segments) >= 5 else None
                    job_status = self.state.get_job(study_key, sub_id)
                    if sub_action == "results":
                        return httpx.Response(
                            200, json=job_status.get("results", []), request=request
                        )
                    # Single job status lookup
                    return httpx.Response(200, json=job_status, request=request)
                # List jobs: JobsEndpoint uses JsonListPaginator which expects a raw JSON list
                items = self.state.get_jobs(study_key)
                return httpx.Response(200, json=items, request=request)

            if resource == "intervals":
                items = self.state.get_intervals(study_key)
                return self._filter_and_paginate(request, items, params)

            if resource == "variables":
                items = self.state.get_variables(study_key)
                return self._filter_and_paginate(request, items, params)

            if resource == "users":
                items = self.state.get_users(study_key)
                include_inactive = params.get("includeInactive", "false").strip().lower() == "true"
                if not include_inactive:
                    items = [u for u in items if u.get("userActiveInStudy", True) is not False]
                return self._filter_and_paginate(request, items, params)

        # 3. Direct /jobs/{batch_id} path
        if len(segments) >= 1 and segments[0] == "jobs":
            batch_id = segments[1] if len(segments) >= 2 else "BATCH"
            job_status = self.state.get_job(None, batch_id)
            if len(segments) >= 3 and segments[2] == "results":
                return httpx.Response(200, json=job_status.get("results", []), request=request)
            return httpx.Response(200, json=job_status, request=request)

        # Fallback for unrecognized endpoints: return empty paginated response
        return self._filter_and_paginate(request, [], params)

    def _bad_request_response(
        self, request: httpx.Request, exc: _FilterParseError
    ) -> httpx.Response:
        """Build a contract-compliant HTTP 400 error envelope."""
        now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = {
            "metadata": {
                "status": "BAD_REQUEST",
                "method": request.method.upper(),
                "path": request.url.path,
                "timestamp": now_iso,
                "error": {
                    "code": "400",
                    "description": str(exc),
                    "message": str(exc),
                    "field": {
                        "attribute": exc.attribute,
                        "value": exc.value,
                    },
                },
            }
        }
        return httpx.Response(
            400,
            json=payload,
            headers={"content-type": "application/json"},
            request=request,
        )

    def _filter_and_paginate(
        self, request: httpx.Request, items: list[dict[str, Any]], params: dict[str, str]
    ) -> httpx.Response:
        """Apply filter, recordDataFilter, and sort parameters, then paginate."""
        try:
            filter_str = params.get("filter")
            if filter_str:
                connector, clauses = _split_filter_clauses(filter_str, attribute="filter")
                if clauses:
                    items = [item for item in items if _matches_clauses(item, connector, clauses)]

            rec_filter_str = params.get("recordDataFilter")
            project_none_record_data = False
            if rec_filter_str is not None and rec_filter_str.strip():
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
        except _FilterParseError as exc:
            return self._bad_request_response(request, exc)

        return self._paginate_response(request, items, params, sort_specs=sort_specs)

    def _paginate_response(
        self,
        request: httpx.Request,
        items: list[dict[str, Any]],
        params: dict[str, str],
        *,
        sort_specs: list[dict[str, str]] | None = None,
    ) -> httpx.Response:
        """Format items into standard iMedNet ApiResponse JSON envelope."""
        try:
            page = int(params.get("page", 0))
        except (ValueError, TypeError):
            page = 0

        try:
            size = int(params.get("size", 25))
        except (ValueError, TypeError):
            size = 25
        if size <= 0:
            size = 25
        page = max(page, 0)

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
