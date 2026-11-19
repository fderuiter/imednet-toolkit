"""In-memory HTTP transport for offline simulation mode."""

from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any

import httpx

from imednet.simulation.state import SimulationState, get_default_simulation_state
from imednet.utils.filters import ORDERED_FILTER_OPERATORS


def _to_camel_or_snake(key: str) -> list[str]:
    """Return both camelCase and snake_case representations of a key."""
    # Convert camelCase to snake_case
    snake = "".join(["_" + c.lower() if c.isupper() else c for c in key]).lstrip("_")
    # Convert snake_case to camelCase
    parts = snake.split("_")
    camel = parts[0] + "".join(word.capitalize() for word in parts[1:])
    return list(dict.fromkeys([key, snake, camel]))


def _compare_values(actual_val: Any, op: str, expected_raw: str) -> bool:
    """Evaluate a filter comparison operator against an item field value."""
    expected_val = expected_raw.strip()
    if len(expected_val) >= 2 and expected_val[0] == '"' and expected_val[-1] == '"':
        expected_val = expected_val[1:-1].replace(r"\"", '"').replace("\\\\", "\\")

    if op == "=~":
        try:
            return re.search(expected_val, str(actual_val)) is not None
        except re.error:
            return False

    if isinstance(actual_val, bool):
        actual_cmp: Any = str(actual_val).lower()
        expected_cmp: Any = expected_val.lower()
    elif isinstance(actual_val, int):
        try:
            actual_cmp = actual_val
            expected_cmp = int(expected_val)
        except ValueError:
            return op == "!="
    elif isinstance(actual_val, float):
        try:
            actual_cmp = actual_val
            expected_cmp = float(expected_val)
        except ValueError:
            return op == "!="
    else:
        actual_cmp = str(actual_val).strip()
        expected_cmp = expected_val.strip()
        if op in ("==", "!="):
            actual_cmp = actual_cmp.lower()
            expected_cmp = expected_cmp.lower()

    if op == "==":
        return bool(actual_cmp == expected_cmp)
    if op == "!=":
        return bool(actual_cmp != expected_cmp)
    if op == "<":
        return bool(actual_cmp < expected_cmp)
    if op == "<=":
        return bool(actual_cmp <= expected_cmp)
    if op == ">":
        return bool(actual_cmp > expected_cmp)
    if op == ">=":
        return bool(actual_cmp >= expected_cmp)
    return False


def _matches_filter(item: dict[str, Any], field: str, expected_val: str, op: str = "==") -> bool:
    """Check if item matches the expected filter value across naming variants."""
    candidates = _to_camel_or_snake(field)
    actual_val: Any = None
    found = False
    for c in candidates:
        if c in item:
            actual_val = item[c]
            found = True
            break

    if not found:
        return False

    return _compare_values(actual_val, op, expected_val)


def _parse_clause(term: str) -> tuple[str, str, str] | None:
    """Parse ``field<op>value`` into ``(field, op, value)``."""
    for op in ORDERED_FILTER_OPERATORS:
        if op in term:
            field, val = term.split(op, 1)
            return field.strip(), op, val.strip()
    return None


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

            if resource in ("recordRevisions", "record-revisions"):
                items = self.state.get_record_revisions(study_key)
                return self._filter_and_paginate(request, items, params)

            if resource == "codings":
                items = self.state.get_codings(study_key)
                return self._filter_and_paginate(request, items, params)

        # 3. Direct /jobs/{batch_id} path
        if len(segments) >= 1 and segments[0] == "jobs":
            batch_id = segments[1] if len(segments) >= 2 else "BATCH"
            job_status = self.state.get_job(None, batch_id)
            return httpx.Response(200, json=job_status, request=request)

        # Fallback for unrecognized endpoints: return empty paginated response
        return self._paginate_response(request, [], params)

    def _filter_and_paginate(
        self, request: httpx.Request, items: list[dict[str, Any]], params: dict[str, str]
    ) -> httpx.Response:
        """Apply query filter, recordDataFilter, and sort parameters before pagination."""
        filter_str = params.get("filter")
        if filter_str and filter_str.strip().lower() != "none":
            use_or = "," in filter_str and ";" not in filter_str
            raw_terms = filter_str.split("," if use_or else ";")
            parsed_clauses = [c for term in raw_terms if (c := _parse_clause(term)) is not None]
            if parsed_clauses:
                filtered = []
                for item in items:
                    outcomes = [
                        _matches_filter(item, field, val, op=op)
                        for field, op, val in parsed_clauses
                    ]
                    if any(outcomes) if use_or else all(outcomes):
                        filtered.append(item)
                items = filtered

        rdf_str = params.get("recordDataFilter")
        if rdf_str and rdf_str.strip().lower() != "none":
            use_or_rdf = "," in rdf_str and ";" not in rdf_str
            raw_rdf_terms = rdf_str.split("," if use_or_rdf else ";")
            rdf_clauses = [c for term in raw_rdf_terms if (c := _parse_clause(term)) is not None]
            if rdf_clauses:
                rdf_filtered = []
                for item in items:
                    rec_data = item.get("recordData")
                    if not isinstance(rec_data, dict):
                        continue
                    outcomes = [
                        _matches_filter(rec_data, field, val, op=op)
                        for field, op, val in rdf_clauses
                    ]
                    if any(outcomes) if use_or_rdf else all(outcomes):
                        rdf_filtered.append(item)
                items = rdf_filtered

        sort_str = params.get("sort")
        if sort_str:
            parts = [p.strip() for p in sort_str.split(",") if p.strip()]
            if parts:
                sort_field = parts[0]
                reverse = len(parts) > 1 and parts[1].lower() == "desc"
                candidates = _to_camel_or_snake(sort_field)

                def _sort_key(entry: dict[str, Any]) -> tuple[int, str]:
                    for cand in candidates:
                        if cand in entry and entry[cand] is not None:
                            return (0, str(entry[cand]))
                    return (1, "")

                items = sorted(items, key=_sort_key, reverse=reverse)

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
