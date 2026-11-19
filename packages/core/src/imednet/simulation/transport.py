"""In-memory HTTP transport for offline simulation mode."""

from __future__ import annotations

import json
import re
import urllib.parse
from datetime import UTC
from typing import Any

import httpx

from imednet.simulation.state import SimulationState, get_default_simulation_state
from imednet.utils.dates import parse_iso_datetime
from imednet.utils.filters import _parse_clause_operator, _split_filter_clauses


def _to_camel_or_snake(key: str) -> list[str]:
    """Return both camelCase and snake_case representations of a key."""
    # Convert camelCase to snake_case
    snake = "".join(["_" + c.lower() if c.isupper() else c for c in key]).lstrip("_")
    # Convert snake_case to camelCase
    parts = snake.split("_")
    camel = parts[0] + "".join(word.capitalize() for word in parts[1:])
    return list(dict.fromkeys([key, snake, camel]))


def _lookup_field(item: dict[str, Any], field: str) -> tuple[bool, Any]:
    """Look up a field across camelCase and snake_case variants."""
    for candidate in _to_camel_or_snake(field):
        if candidate in item:
            return True, item[candidate]
    return False, None


def _unquote_filter_value(raw_val: str) -> str:
    """Strip surrounding double quotes and unescape escaped characters."""
    stripped = raw_val.strip()
    if len(stripped) >= 2 and stripped.startswith('"') and stripped.endswith('"'):
        inner = stripped[1:-1]
        return inner.replace(r"\"", '"').replace("\\\\", "\\")
    return stripped


def _to_utc_epoch(val: Any) -> float | None:
    """Parse an ISO-8601 timestamp string into a UTC epoch float if valid."""
    if not isinstance(val, str) or len(val) < 10 or val[4:5] != "-":
        return None
    try:
        dt = parse_iso_datetime(val)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _apply_ordering_op(left: Any, op: str, right: Any) -> bool:
    """Apply a relational or equality comparison operator to two comparable scalars."""
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


def _compare_filter_value(actual_val: Any, op: str, raw_expected: str) -> bool:
    """Evaluate a filter clause operator against an item attribute value."""
    expected_val = _unquote_filter_value(raw_expected)
    if actual_val is None:
        return op == "!=" and expected_val.lower() != "null"

    if op == "=~":
        return bool(re.search(expected_val, str(actual_val)))

    if isinstance(actual_val, bool):
        actual_bool_str = str(actual_val).lower()
        expected_bool_str = expected_val.lower()
        if op == "==":
            return actual_bool_str == expected_bool_str
        if op == "!=":
            return actual_bool_str != expected_bool_str
        return False

    if isinstance(actual_val, (int, float)):
        try:
            expected_num = float(expected_val)
        except ValueError:
            return False
        return _apply_ordering_op(float(actual_val), op, expected_num)

    actual_epoch = _to_utc_epoch(actual_val)
    expected_epoch = _to_utc_epoch(expected_val)
    if actual_epoch is not None and expected_epoch is not None:
        return _apply_ordering_op(actual_epoch, op, expected_epoch)

    if op in ("==", "!="):
        return _apply_ordering_op(
            str(actual_val).strip().lower(),
            op,
            expected_val.strip().lower(),
        )
    return _apply_ordering_op(str(actual_val).strip(), op, expected_val.strip())


def _matches_clause(item: dict[str, Any], field: str, op: str, expected_val: str) -> bool:
    """Check if item matches a parsed filter clause across naming variants."""
    found, actual_val = _lookup_field(item, field.strip())
    if not found:
        return False
    return _compare_filter_value(actual_val, op, expected_val)


def _sort_items(
    items: list[dict[str, Any]], sort_field: str, *, descending: bool
) -> list[dict[str, Any]]:
    """Sort simulated resource dictionaries by a field while keeping nulls last."""
    non_null = [item for item in items if _lookup_field(item, sort_field)[1] is not None]
    nulls = [item for item in items if _lookup_field(item, sort_field)[1] is None]
    if non_null and all(
        isinstance(_lookup_field(item, sort_field)[1], (int, float))
        and not isinstance(_lookup_field(item, sort_field)[1], bool)
        for item in non_null
    ):
        non_null.sort(
            key=lambda item: float(_lookup_field(item, sort_field)[1]),
            reverse=descending,
        )
    else:
        non_null.sort(
            key=lambda item: str(_lookup_field(item, sort_field)[1]),
            reverse=descending,
        )
    return non_null + nulls


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
                return self._filter_and_paginate(request, items, params)

            if resource == "codings":
                items = self.state.get_codings(study_key)
                return self._filter_and_paginate(request, items, params)

            if resource == "recordRevisions":
                items = self.state.get_record_revisions(study_key)
                return self._filter_and_paginate(request, items, params)

        # 3. Direct /jobs/{batch_id} path
        if len(segments) >= 1 and segments[0] == "jobs":
            batch_id = segments[1] if len(segments) >= 2 else "BATCH"
            job_status = self.state.get_job(None, batch_id)
            if len(segments) >= 3 and segments[2] == "results":
                return httpx.Response(200, json=job_status.get("results", []), request=request)
            return httpx.Response(200, json=job_status, request=request)

        # Fallback for unrecognized endpoints: return empty paginated response
        return self._paginate_response(request, [], params)

    def _filter_and_paginate(
        self, request: httpx.Request, items: list[dict[str, Any]], params: dict[str, str]
    ) -> httpx.Response:
        """Apply query filter parameters and return a paginated response."""
        filter_str = params.get("filter")
        if filter_str and filter_str.strip() and filter_str.strip().lower() != "none":
            clauses, connector = _split_filter_clauses(filter_str.strip())
            parsed = [p for c in clauses if c and (p := _parse_clause_operator(c)) is not None]
            if parsed:
                is_or = connector == ","
                matcher = any if is_or else all
                items = [
                    item
                    for item in items
                    if matcher(_matches_clause(item, attr, op, val) for attr, op, val in parsed)
                ]

        record_data_filter = params.get("recordDataFilter")
        if record_data_filter is not None:
            stripped_rdf = record_data_filter.strip()
            if stripped_rdf.lower() == "none":
                items = [{k: v for k, v in item.items() if k != "recordData"} for item in items]
            elif stripped_rdf:
                clauses, connector = _split_filter_clauses(stripped_rdf)
                parsed_rdf = [
                    p for c in clauses if c and (p := _parse_clause_operator(c)) is not None
                ]
                if parsed_rdf:
                    is_or = connector == ","
                    matcher = any if is_or else all
                    items = [
                        item
                        for item in items
                        if isinstance(item.get("recordData"), dict)
                        and matcher(
                            _matches_clause(item["recordData"], attr, op, val)
                            for attr, op, val in parsed_rdf
                        )
                    ]

        return self._paginate_response(request, items, params)

    def _paginate_response(
        self, request: httpx.Request, items: list[dict[str, Any]], params: dict[str, str]
    ) -> httpx.Response:
        """Format items into standard iMednet paginated JSON format."""
        sort_param = params.get("sort")
        if sort_param and sort_param.strip():
            sort_parts = [part.strip() for part in sort_param.split(",", 1)]
            sort_field = sort_parts[0]
            descending = len(sort_parts) > 1 and sort_parts[1].lower() == "desc"
            if sort_field:
                items = _sort_items(items, sort_field, descending=descending)
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
