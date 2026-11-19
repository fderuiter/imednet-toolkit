"""In-memory HTTP transport for offline simulation mode."""

from __future__ import annotations

import json
import re
import urllib.parse
from datetime import UTC, datetime
from typing import Any

import httpx

from imednet.simulation.state import SimulationState, get_default_simulation_state
from imednet.utils.dates import parse_iso_datetime
from imednet.utils.filters import ORDERED_FILTER_OPERATORS

_TWO_CHAR_FILTER_OPS = frozenset(op for op in ORDERED_FILTER_OPERATORS if len(op) == 2)
_ONE_CHAR_FILTER_OPS = frozenset({*(op for op in ORDERED_FILTER_OPERATORS if len(op) == 1), "="})


def _to_camel_or_snake(key: str) -> list[str]:
    """Return both camelCase and snake_case representations of a key."""
    # Convert camelCase to snake_case
    snake = "".join(["_" + c.lower() if c.isupper() else c for c in key]).lstrip("_")
    # Convert snake_case to camelCase
    parts = snake.split("_")
    camel = parts[0] + "".join(word.capitalize() for word in parts[1:])
    return list(dict.fromkeys([key, snake, camel]))


def _split_unquoted(expr: str, delimiter: str) -> list[str]:
    """Split a filter string on an unquoted delimiter character."""
    parts: list[str] = []
    buf: list[str] = []
    in_quotes = False
    escape = False
    for ch in expr:
        if escape:
            buf.append(ch)
            escape = False
            continue
        if ch == "\\" and in_quotes:
            buf.append(ch)
            escape = True
            continue
        if ch == '"':
            in_quotes = not in_quotes
            buf.append(ch)
            continue
        if ch == delimiter and not in_quotes:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))
    return [part for part in (p.strip() for p in parts) if part]


def _unquote_filter_value(raw_val: str) -> str:
    """Strip outer double quotes and unescape escaped characters."""
    stripped = raw_val.strip()
    if len(stripped) >= 2 and stripped[0] == '"' and stripped[-1] == '"':
        inner = stripped[1:-1]
        out: list[str] = []
        escape = False
        for ch in inner:
            if escape:
                out.append(ch)
                escape = False
            elif ch == "\\":
                escape = True
            else:
                out.append(ch)
        return "".join(out)
    return stripped


def _parse_clause(term: str) -> tuple[str, str, str] | None:
    """Parse ``field<op>value`` outside quotation marks into ``(field, op, unquoted_value)``."""
    in_quotes = False
    escape = False
    for idx in range(len(term)):
        ch = term[idx]
        if escape:
            escape = False
            continue
        if ch == "\\" and in_quotes:
            escape = True
            continue
        if ch == '"':
            in_quotes = not in_quotes
            continue
        if in_quotes:
            continue
        two_char = term[idx : idx + 2]
        if len(two_char) == 2 and two_char in _TWO_CHAR_FILTER_OPS:
            return term[:idx].strip(), two_char, _unquote_filter_value(term[idx + 2 :])
        if ch in _ONE_CHAR_FILTER_OPS:
            return term[:idx].strip(), ch, _unquote_filter_value(term[idx + 1 :])
    return None


def _try_parse_utc_datetime(raw: str) -> datetime | None:
    """Parse an ISO-8601 date or timestamp string into a UTC-aware datetime if valid."""
    text = raw.strip()
    if len(text) < 10 or not text[:4].isdigit() or text[4] != "-":
        return None
    try:
        dt = parse_iso_datetime(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _compare_ordered(actual: Any, expected: Any, op: str) -> bool:
    """Apply an equality or ordering operator across two comparable values."""
    if op in ("==", "="):
        return bool(actual == expected)
    if op == "!=":
        return bool(actual != expected)
    if op == "<":
        return bool(actual < expected)
    if op == "<=":
        return bool(actual <= expected)
    if op == ">":
        return bool(actual > expected)
    if op == ">=":
        return bool(actual >= expected)
    return False


def _matches_clause(item: dict[str, Any], field: str, op: str, expected_val: str) -> bool:
    """Check if ``item`` satisfies ``field <op> expected_val`` across naming variants."""
    candidates = _to_camel_or_snake(field)
    actual_val: Any = None
    found = False
    for candidate in candidates:
        if candidate in item:
            actual_val = item[candidate]
            found = True
            break

    if not found:
        return False

    if actual_val is None:
        is_none_literal = expected_val.strip().lower() == "none"
        if op in ("==", "="):
            return is_none_literal
        if op == "!=":
            return not is_none_literal
        return False

    if op == "=~":
        try:
            return re.search(expected_val, str(actual_val)) is not None
        except re.error:
            return expected_val in str(actual_val)

    if isinstance(actual_val, bool):
        expected_lower = expected_val.strip().lower()
        if expected_lower not in ("true", "false"):
            return op == "!="
        expected_bool = expected_lower == "true"
        if op in ("==", "="):
            return actual_val is expected_bool
        if op == "!=":
            return actual_val is not expected_bool
        return False

    if isinstance(actual_val, (int, float)):
        try:
            expected_num = float(expected_val)
        except ValueError:
            return op == "!="
        return _compare_ordered(float(actual_val), expected_num, op)

    actual_str = str(actual_val).strip()
    expected_str = expected_val.strip()

    actual_dt = _try_parse_utc_datetime(actual_str)
    expected_dt = _try_parse_utc_datetime(expected_str)
    if actual_dt is not None and expected_dt is not None:
        return _compare_ordered(actual_dt, expected_dt, op)

    try:
        actual_float = float(actual_str)
        expected_float = float(expected_str)
    except ValueError:
        pass
    else:
        return _compare_ordered(actual_float, expected_float, op)

    if op in ("==", "="):
        return actual_str.lower() == expected_str.lower()
    if op == "!=":
        return actual_str.lower() != expected_str.lower()
    return _compare_ordered(actual_str, expected_str, op)


def _evaluate_filter_expression(
    items: list[dict[str, Any]], filter_str: str
) -> list[dict[str, Any]]:
    """Filter ``items`` using ';' (AND) or ',' (OR) expressions."""
    stripped = filter_str.strip()
    if not stripped or stripped.lower() == "none":
        return items

    and_terms = _split_unquoted(stripped, ";")
    if len(and_terms) > 1:
        parsed_and = [_parse_clause(t) for t in and_terms]
        return [
            item
            for item in items
            if all(
                clause is not None and _matches_clause(item, clause[0], clause[1], clause[2])
                for clause in parsed_and
            )
        ]

    or_terms = _split_unquoted(stripped, ",")
    parsed_or = [_parse_clause(t) for t in or_terms]
    return [
        item
        for item in items
        if any(
            clause is not None and _matches_clause(item, clause[0], clause[1], clause[2])
            for clause in parsed_or
        )
    ]


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
                    return httpx.Response(200, json=job_res, request=request)

                items = self.state.get_records(study_key)
                return self._filter_and_paginate(request, items, params)

            if resource == "queries":
                items = self.state.get_queries(study_key)
                return self._filter_and_paginate(request, items, params)

            if resource == "jobs":
                if sub_id is not None:
                    # Single job status lookup
                    job_status = self.state.get_job(study_key, sub_id)
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
                return self._filter_and_paginate(request, items, params)

        # 3. Direct /jobs/{batch_id} path
        if len(segments) >= 1 and segments[0] == "jobs":
            batch_id = segments[1] if len(segments) >= 2 else "BATCH"
            job_status = self.state.get_job(None, batch_id)
            return httpx.Response(200, json=job_status, request=request)

        # Fallback for unrecognized endpoints: return empty paginated response
        return self._filter_and_paginate(request, [], params)

    def _filter_and_paginate(
        self, request: httpx.Request, items: list[dict[str, Any]], params: dict[str, str]
    ) -> httpx.Response:
        """Apply query filter, recordDataFilter, sort, and pagination parameters."""
        filter_str = params.get("filter")
        if filter_str:
            items = _evaluate_filter_expression(items, filter_str)

        record_data_filter = params.get("recordDataFilter")
        if record_data_filter is not None:
            stripped_rdf = record_data_filter.strip()
            if stripped_rdf.lower() == "none":
                items = [{**item, "recordData": None} for item in items]
            elif stripped_rdf:
                items = [
                    item
                    for item in items
                    if isinstance(item.get("recordData"), dict)
                    and bool(_evaluate_filter_expression([item["recordData"]], stripped_rdf))
                ]

        sort_str = params.get("sort")
        if sort_str:
            sort_clauses = [seg.strip() for seg in sort_str.split(";") if seg.strip()]
            for clause in reversed(sort_clauses):
                sort_parts = [part.strip() for part in clause.split(",") if part.strip()]
                if not sort_parts:
                    continue
                if len(sort_parts) >= 2 and sort_parts[-1].lower() in ("asc", "desc"):
                    reverse = sort_parts[-1].lower() == "desc"
                    sort_fields = sort_parts[:-1]
                else:
                    reverse = False
                    sort_fields = sort_parts

                for sort_field in reversed(sort_fields):
                    candidates = _to_camel_or_snake(sort_field)

                    def _sort_value(
                        entry: dict[str, Any], _candidates: list[str] = candidates
                    ) -> Any:
                        for candidate in _candidates:
                            if candidate in entry and entry[candidate] is not None:
                                val = entry[candidate]
                                if isinstance(val, bool):
                                    return (0, int(val))
                                if isinstance(val, (int, float)):
                                    return (1, float(val))
                                text = str(val)
                                dt = _try_parse_utc_datetime(text)
                                if dt is not None:
                                    return (2, dt.timestamp())
                                return (3, text.lower())
                        return None

                    present = [item for item in items if _sort_value(item) is not None]
                    missing = [item for item in items if _sort_value(item) is None]
                    present.sort(key=_sort_value, reverse=reverse)
                    items = present + missing

        return self._paginate_response(request, items, params)

    def _paginate_response(
        self, request: httpx.Request, items: list[dict[str, Any]], params: dict[str, str]
    ) -> httpx.Response:
        """Format items into standard iMednet paginated JSON format."""
        try:
            page = int(params.get("page", 0))
        except (ValueError, TypeError):
            page = 0

        try:
            size = int(params.get("size", 100))
        except (ValueError, TypeError):
            size = 100

        total_items = len(items)
        if total_items == 0:
            total_pages = 0
            page_items: list[dict[str, Any]] = []
        else:
            total_pages = max(1, (total_items + size - 1) // size)
            start_idx = page * size
            end_idx = start_idx + size
            page_items = items[start_idx:end_idx]

        payload = {
            "data": page_items,
            "pagination": {
                "page": page,
                "size": size,
                "totalPages": total_pages,
                "totalItems": total_items,
            },
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
