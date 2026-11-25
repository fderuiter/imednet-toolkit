"""Regression tests for workflow filter-bound planning and local multi-item filtering."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import respx

from imednet.models.queries import Query
from imednet.models.records import Record
from imednet.models.subjects import Subject
from imednet.sdk import ImednetSDK
from imednet_workflows.cached_loader import CachedRecordsLoader
from imednet_workflows.query_management import QueryManagementWorkflow
from imednet_workflows.record_mapper import RecordMapper


def test_delta_and_missing_record_queries_emit_whole_second_utc_and_single_attribute_bounds(
    tmp_path: Path,
) -> None:
    """_fetch_delta_records emits whole-second UTC and _fetch_missing_records uses == or >= with local filtering."""
    with respx.mock(assert_all_called=True) as router:
        seen_filters: list[str] = []

        def _records_responder(request: httpx.Request) -> httpx.Response:
            flt = request.url.params.get("filter", "")
            seen_filters.append(flt)
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "studyKey": "STUDY",
                            "recordId": 3,
                            "formId": 10,
                            "formKey": "FORM",
                            "subjectKey": "S1",
                            "dateModified": "2026-02-01T12:30:45Z",
                            "recordData": {"v": 3},
                        },
                        {
                            "studyKey": "STUDY",
                            "recordId": 5,
                            "formId": 10,
                            "formKey": "FORM",
                            "subjectKey": "S1",
                            "dateModified": "2026-02-01T12:30:45Z",
                            "recordData": {"v": 5},
                        },
                        {
                            "studyKey": "STUDY",
                            "recordId": 8,
                            "formId": 10,
                            "formKey": "FORM",
                            "subjectKey": "S1",
                            "dateModified": "2026-02-01T12:30:45Z",
                            "recordData": {"v": 8},
                        },
                    ]
                },
            )

        router.get("https://api.test/api/v1/edc/studies/STUDY/records").mock(
            side_effect=_records_responder
        )

        with ImednetSDK(api_key="k", security_key="s", base_url="https://api.test") as sdk:
            loader = CachedRecordsLoader(sdk, cache_dir=tmp_path / "http-contract-cache")

            blank_hwm_delta = loader._fetch_delta_records("STUDY", "   ")
            assert len(blank_hwm_delta) == 3
            assert seen_filters[-1] == "deleted==false"

            delta = loader._fetch_delta_records("STUDY", "2026-02-01T14:30:45.999999+02:00")
            assert len(delta) == 3
            assert seen_filters[-1] == "dateModified>=2026-02-01T12:30:45Z"

            assert loader._fetch_missing_records("STUDY", set()) == []
            assert len(seen_filters) == 2

            single_missing = loader._fetch_missing_records("STUDY", {5})
            assert [r.record_id for r in single_missing] == [5]
            assert seen_filters[-1] == "recordId==5"

            multi_missing = loader._fetch_missing_records("STUDY", {3, 8})
            assert [r.record_id for r in multi_missing] == [3, 8]
            assert seen_filters[-1] == "recordId>=3"


def test_sync_records_preflight_and_multi_missing_backfill_uses_single_ge_bound(
    tmp_path: Path,
) -> None:
    """Count-preflight reconciliation with multiple missing draft IDs emits recordId>=min_id once."""
    active_records: dict[int, dict[str, Any]] = {
        1: Record(
            study_key="STUDY",
            record_id=1,
            form_id=10,
            form_key="FORM",
            subject_key="S1",
            date_modified="2024-01-01 00:00:00+00:00",
            record_data={"v": 1},
        ).model_dump(mode="json", by_alias=True),
    }
    observed_filters: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        filt = params.get("filter", "")
        rdf = params.get("recordDataFilter")
        size = int(params.get("size", "100"))
        items = [active_records[k] for k in sorted(active_records)]
        if filt:
            observed_filters.append(filt)

        if "dateModified>=" in filt:
            return httpx.Response(
                200,
                json={
                    "data": [],
                    "pagination": {
                        "currentPage": 0,
                        "size": size,
                        "totalPages": 0,
                        "totalElements": 0,
                    },
                },
            )
        if filt.startswith("recordId>="):
            floor_id = int(filt.removeprefix("recordId>="))
            matched = [r for r in items if int(r["recordId"]) >= floor_id]
            return httpx.Response(
                200,
                json={
                    "data": matched,
                    "pagination": {
                        "currentPage": 0,
                        "size": size,
                        "totalPages": 1,
                        "totalElements": len(matched),
                    },
                },
            )
        if rdf == "none" and size == 1:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"studyKey": "STUDY", "recordId": items[0]["recordId"], "formKey": "FORM"}
                    ],
                    "pagination": {
                        "currentPage": 0,
                        "size": 1,
                        "totalPages": len(items),
                        "totalElements": len(items),
                    },
                },
            )
        if rdf == "none":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"studyKey": "STUDY", "recordId": r["recordId"], "formKey": r["formKey"]}
                        for r in items
                    ],
                    "pagination": {
                        "currentPage": 0,
                        "size": size,
                        "totalPages": 1,
                        "totalElements": len(items),
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "data": items,
                "pagination": {
                    "currentPage": 0,
                    "size": size,
                    "totalPages": 1,
                    "totalElements": len(items),
                },
            },
        )

    with respx.mock(assert_all_called=True) as router:
        router.get("https://api.test/api/v1/edc/studies/STUDY/records").mock(side_effect=_handler)
        with ImednetSDK(api_key="k", security_key="s", base_url="https://api.test") as sdk:
            loader = CachedRecordsLoader(sdk, cache_dir=tmp_path / "preflight-multi-missing")
            loader.sync_records("STUDY", reconcile=True)
            assert [r.record_id for r in loader.get_cached_records("STUDY")] == [1]

            observed_filters.clear()
            for draft_id in (4, 9):
                active_records[draft_id] = Record(
                    study_key="STUDY",
                    record_id=draft_id,
                    form_id=10,
                    form_key="FORM",
                    subject_key=f"S{draft_id}",
                    date_modified=None,
                    record_data={"v": draft_id},
                ).model_dump(mode="json", by_alias=True)
            loader.sync_records("STUDY", reconcile=True)
            assert [r.record_id for r in loader.get_cached_records("STUDY")] == [1, 4, 9]
            assert "dateModified>=2024-01-01T00:00:00Z" in observed_filters
            assert "recordId>=4" in observed_filters


def test_workflow_site_query_and_variable_metadata_single_vs_multi_item_edge_cases() -> None:
    """Verify single-Subject caller-filter collision avoidance and variable whitelist deduplication."""
    sdk = MagicMock()
    sdk.get_subjects.return_value = [Subject(subject_key="S1")]
    sdk.get_queries.return_value = [
        Query(annotation_id=1, subject_key=" S1 "),
        Query(annotation_id=2, subject_key="OTHER"),
        Query(annotation_id=3, subject_key=None),
    ]
    wf = QueryManagementWorkflow(sdk)

    # Caller already supplied an inline subjectKey filter on a single-Subject Site -> filter locally
    matched = wf.get_queries_by_site(
        "STUDY",
        "Site 1",
        additional_filter={"filter": "subjectKey==OTHER"},
    )
    sdk.get_queries.assert_called_once_with("STUDY", filter="subjectKey==OTHER")
    assert [q.annotation_id for q in matched] == [1]

    # Single-Subject Site with caller OR filter avoids mixing ';' and ',' upstream
    sdk.get_queries.reset_mock()
    or_matched = wf.get_queries_by_site(
        "STUDY",
        "Site 1",
        additional_filter={"annotation_type": ["subject", "record"]},
    )
    sdk.get_queries.assert_called_once_with("STUDY", annotation_type=["subject", "record"])
    assert [q.annotation_id for q in or_matched] == [1]

    sdk.get_queries.reset_mock()
    raw_or_matched = wf.get_queries_by_site(
        "STUDY",
        "Site 1",
        additional_filter={"filter": "annotationType==subject,annotationType==record"},
    )
    sdk.get_queries.assert_called_once_with(
        "STUDY",
        filter="annotationType==subject,annotationType==record",
    )
    assert [q.annotation_id for q in raw_or_matched] == [1]

    with respx.mock(assert_all_called=True) as router:
        var_route = router.get("https://api.test/api/v1/edc/studies/STUDY/variables").respond(
            200,
            json={
                "data": [
                    {"variableName": "AGE", "label": "Age", "formId": 10},
                    {"variableName": "WT", "label": "Weight", "formId": 20},
                ]
            },
        )
        with ImednetSDK(api_key="k", security_key="s", base_url="https://api.test") as real_sdk:
            mapper = RecordMapper(real_sdk)

            # Deduplicated single-item whitelists still push down scalar filters
            mapper._fetch_variable_metadata(
                "STUDY",
                variable_whitelist=["AGE", "AGE"],
                form_whitelist=[10, 10],
            )
            assert (
                var_route.calls.last.request.url.params["filter"] == "formId==10;variableName==AGE"
            )

            # Single variable with multi-form pushes down only variableName
            var_route.calls.clear()
            mapper._fetch_variable_metadata(
                "STUDY",
                variable_whitelist=["AGE"],
                form_whitelist=[10, 20],
            )
            assert var_route.calls.last.request.url.params["filter"] == "variableName==AGE"

            # Multi-variable without form omits server filter and filters in memory
            var_route.calls.clear()
            var_keys, _ = mapper._fetch_variable_metadata(
                "STUDY",
                variable_whitelist=["AGE", "WT"],
                form_whitelist=None,
            )
            assert "filter" not in var_route.calls.last.request.url.params
            assert var_keys == ["AGE", "WT"]
