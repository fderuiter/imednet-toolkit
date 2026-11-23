"""Unit tests for workflows data extraction."""

from unittest.mock import MagicMock

from imednet.models.record_revisions import RecordRevision
from imednet.models.records import Record
from imednet.models.subjects import Subject
from imednet.models.visits import Visit
from imednet_workflows.data_extraction import DataExtractionWorkflow


def test_extract_records_by_criteria_filters_subject_and_visit() -> None:
    """Test that extract records by criteria filters subject and visit."""
    sdk = MagicMock()
    sdk.get_subjects.return_value = [Subject(subject_key="S1"), Subject(subject_key="S2")]
    sdk.get_visits.return_value = [
        Visit(visit_id=1, subject_key="S1"),
        Visit(visit_id=2, subject_key="S2"),
    ]
    records = [
        Record(record_id=1, subject_key="S1", visit_id=1),
        Record(record_id=2, subject_key="S2", visit_id=2),
        Record(record_id=3, subject_key="S1", visit_id=99),
    ]
    sdk.get_records.return_value = records

    wf = DataExtractionWorkflow(sdk)
    result = wf.extract_records_by_criteria(
        "STUDY",
        subject_filter={"status": "active"},
        visit_filter={"visit_id": 1},
    )

    sdk.get_subjects.assert_called_once_with("STUDY", status="active")
    assert sdk.get_subjects.call_args.kwargs == {"status": "active"}
    sdk.get_visits.assert_called_once_with("STUDY", visit_id=1)
    assert sdk.get_visits.call_args.kwargs == {"visit_id": 1}
    sdk.get_records.assert_called_once_with(study_key="STUDY", record_data_filter=None)
    assert sdk.get_records.call_args.kwargs == {"study_key": "STUDY", "record_data_filter": None}

    assert [r.record_id for r in result] == [1, 2]


def test_extract_records_by_criteria_empty_subjects() -> None:
    """Test early return of empty list when subject filter returns no subjects."""
    sdk = MagicMock()
    sdk.get_subjects.return_value = []

    wf = DataExtractionWorkflow(sdk)
    result = wf.extract_records_by_criteria("STUDY", subject_filter={"site": "SITE01"})

    assert result == []
    sdk.get_subjects.assert_called_once_with("STUDY", site="SITE01")
    sdk.get_visits.assert_not_called()
    sdk.get_records.assert_not_called()


def test_extract_records_by_criteria_empty_visits() -> None:
    """Test early return of empty list when visit filter returns no visits."""
    sdk = MagicMock()
    sdk.get_subjects.return_value = [Subject(subject_key="S1")]
    sdk.get_visits.return_value = []

    wf = DataExtractionWorkflow(sdk)
    result = wf.extract_records_by_criteria(
        "STUDY",
        subject_filter={"site": "SITE01"},
        visit_filter={"name": "V1"},
    )

    assert result == []
    sdk.get_subjects.assert_called_once_with("STUDY", site="SITE01")
    sdk.get_visits.assert_called_once_with("STUDY", name="V1")
    sdk.get_records.assert_not_called()


def test_extract_records_by_criteria_no_visits_match_subjects() -> None:
    """Test early return when queried visits belong to different subjects."""
    sdk = MagicMock()
    sdk.get_subjects.return_value = [Subject(subject_key="S1")]
    sdk.get_visits.return_value = [Visit(visit_id=10, subject_key="S99")]

    wf = DataExtractionWorkflow(sdk)
    result = wf.extract_records_by_criteria(
        "STUDY",
        subject_filter={"site": "SITE01"},
        visit_filter={"name": "V1"},
    )

    assert result == []
    sdk.get_records.assert_not_called()


def test_extract_records_by_criteria_subject_filter_only() -> None:
    """Test filtering by subject criteria only when visit filter is None."""
    sdk = MagicMock()
    sdk.get_subjects.return_value = [Subject(subject_key="S1")]
    sdk.get_records.return_value = [
        Record(record_id=1, subject_key="S1", visit_id=10),
        Record(record_id=2, subject_key="S2", visit_id=20),
    ]

    wf = DataExtractionWorkflow(sdk)
    result = wf.extract_records_by_criteria("STUDY", subject_filter={"site": "SITE01"})

    assert [r.record_id for r in result] == [1]
    sdk.get_visits.assert_not_called()


def test_extract_records_by_criteria_visit_filter_only() -> None:
    """Test filtering by visit criteria only when subject filter is None."""
    sdk = MagicMock()
    sdk.get_visits.return_value = [Visit(visit_id=10, subject_key="S1")]
    sdk.get_records.return_value = [
        Record(record_id=1, subject_key="S1", visit_id=10),
        Record(record_id=2, subject_key="S1", visit_id=20),
    ]

    wf = DataExtractionWorkflow(sdk)
    result = wf.extract_records_by_criteria("STUDY", visit_filter={"name": "V1"})

    assert [r.record_id for r in result] == [1]
    sdk.get_subjects.assert_not_called()


def test_extract_records_by_criteria_no_filters() -> None:
    """Test extract records when no subject or visit filters are provided."""
    sdk = MagicMock()
    records = [
        Record(record_id=1, subject_key="S1", visit_id=10),
        Record(record_id=2, subject_key="S2", visit_id=20),
    ]
    sdk.get_records.return_value = records

    wf = DataExtractionWorkflow(sdk)
    result = wf.extract_records_by_criteria("STUDY")

    assert [r.record_id for r in result] == [1, 2]
    sdk.get_subjects.assert_not_called()
    sdk.get_visits.assert_not_called()


def test_extract_records_by_criteria_scaling_benchmark() -> None:
    """Benchmark constant-time set lookups with 1,000 subjects, 2,000 visits, and 10,000 records."""
    import time

    sdk = MagicMock()
    subjects = [Subject(subject_key=f"SUBJ_{i}") for i in range(1000)]
    visits = [Visit(visit_id=i, subject_key=f"SUBJ_{i % 1000}") for i in range(2000)]
    records = [
        Record(record_id=i, subject_key=f"SUBJ_{i % 1200}", visit_id=i % 2500) for i in range(10000)
    ]

    sdk.get_subjects.return_value = subjects
    sdk.get_visits.return_value = visits
    sdk.get_records.return_value = records

    wf = DataExtractionWorkflow(sdk)
    start = time.perf_counter()
    result = wf.extract_records_by_criteria(
        "STUDY",
        subject_filter={"site": "SITE01"},
        visit_filter={"type": "scheduled"},
    )
    elapsed = time.perf_counter() - start

    assert isinstance(result, list)
    expected_subjects = {s.subject_key for s in subjects}
    expected_visits = {v.visit_id for v in visits}
    for record in result:
        assert record.subject_key in expected_subjects
        assert record.visit_id in expected_visits

    # Expected count:
    # subject_key SUBJ_{i % 1200} in expected_subjects when (i % 1200) < 1000
    # visit_id (i % 2500) in expected_visits when (i % 2500) < 2000
    expected_count = sum(1 for i in range(10000) if (i % 1200) < 1000 and (i % 2500) < 2000)
    assert len(result) == expected_count

    # Set lookups execute membership filtering in sub-0.1s (typically ~0.01s),
    # eliminating the 0.31s+ quadratic linear scan latency.
    assert elapsed < 0.15


def test_extract_audit_trail_builds_filters_and_dates() -> None:
    """Test that extract audit trail normalizes UTC bounds and builds dateCreated range filters."""
    sdk = MagicMock()
    revisions = [RecordRevision(record_revision_id=1)]
    sdk.get_record_revisions.return_value = revisions

    wf = DataExtractionWorkflow(sdk)
    result = wf.extract_audit_trail(
        "STUDY",
        start_date="2026-01-01",
        end_date="2026-02-01",
        user_filter={"role": "data"},
        status="open",
    )

    sdk.get_record_revisions.assert_called_once_with(
        "STUDY",
        role="data",
        status="open",
        dateCreated=[
            (">=", "2026-01-01T00:00:00Z"),
            ("<=", "2026-02-01T23:59:59Z"),
        ],
    )
    assert result == revisions


def test_extract_audit_trail_emits_semicolon_joined_date_created_http_filter() -> None:
    """Test that extract_audit_trail sends dateCreated>=...;dateCreated<=... over HTTP."""
    import respx

    from imednet.sdk import ImednetSDK

    with respx.mock(assert_all_called=True) as router:
        route = router.get("https://api.test/api/v1/edc/studies/STUDY/recordRevisions").respond(
            200,
            json={"data": [{"recordRevisionId": 5, "recordId": 10, "recordRevision": 1}]},
        )
        sdk = ImednetSDK(api_key="k", security_key="s", base_url="https://api.test")
        wf = DataExtractionWorkflow(sdk)

        revisions = wf.extract_audit_trail(
            "STUDY",
            start_date="2026-01-01",
            end_date="2026-02-01",
        )

        sent_filter = route.calls.last.request.url.params["filter"]
        assert sent_filter == "dateCreated>=2026-01-01T00:00:00Z;dateCreated<=2026-02-01T23:59:59Z"
        assert "studyKey" not in sent_filter
        assert "startDate" not in sent_filter
        assert "endDate" not in sent_filter
        assert len(revisions) == 1
        assert revisions[0].record_revision_id == 5


def test_iter_records_by_criteria_streams_pages_lazily_and_supports_next_page() -> None:
    """iter_records_by_criteria fetches pages lazily, filters deleted/non-matching items, and steps pages."""
    import itertools

    import httpx
    import respx

    from imednet.sdk import ImednetSDK

    requested_pages: list[int] = []

    def _records_responder(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        requested_pages.append(page)
        if page == 0:
            data = [
                {"recordId": 1, "studyKey": "STUDY", "subjectKey": "S1", "visitId": 10},
                {
                    "recordId": 2,
                    "studyKey": "STUDY",
                    "subjectKey": "S1",
                    "visitId": 10,
                    "deleted": True,
                },
                {"recordId": 3, "studyKey": "STUDY", "subjectKey": "S2", "visitId": 10},
                {"recordId": 4, "studyKey": "STUDY", "subjectKey": "S1", "visitId": 10},
            ]
        else:
            data = [
                {"recordId": 5, "studyKey": "STUDY", "subjectKey": "S1", "visitId": 10},
            ]
        return httpx.Response(
            200,
            json={"data": data, "pagination": {"page": page, "size": 4, "totalPages": 2}},
        )

    with respx.mock(assert_all_called=False) as router:
        router.get("https://api.test/api/v1/edc/studies/STUDY/subjects").respond(
            200,
            json={"data": [{"subjectId": 1, "studyKey": "STUDY", "subjectKey": "S1"}]},
        )
        router.get("https://api.test/api/v1/edc/studies/STUDY/visits").respond(
            200,
            json={"data": [{"visitId": 10, "studyKey": "STUDY", "subjectKey": "S1"}]},
        )
        router.get("https://api.test/api/v1/edc/studies/STUDY/records").mock(
            side_effect=_records_responder
        )

        sdk = ImednetSDK(api_key="k", security_key="s", base_url="https://api.test", retries=0)
        wf = DataExtractionWorkflow(sdk)
        stream = wf.iter_records_by_criteria(
            "STUDY",
            subject_filter={"subject_status": "Screened"},
            visit_filter={"interval_name": "V1"},
            size=4,
        )

        # Taking 1 matching item fetches only page 0 and buffers recordId=4
        first = list(itertools.islice(stream, 1))
        assert [r.record_id for r in first] == [1]
        assert requested_pages == [0]
        assert stream.has_more is True  # type: ignore[attr-defined]

        # next_page() drains buffered recordId=4 from page 0 without a new HTTP call
        buffered_batch = stream.next_page()  # type: ignore[attr-defined]
        assert [r.record_id for r in buffered_batch] == [4]
        assert requested_pages == [0]
        assert stream.has_more is True  # type: ignore[attr-defined]

        # Subsequent next_page() fetches page 1 and marks stream exhausted
        final_batch = stream.next_page()  # type: ignore[attr-defined]
        assert [r.record_id for r in final_batch] == [5]
        assert requested_pages == [0, 1]
        assert stream.has_more is False  # type: ignore[attr-defined]


def test_iter_records_by_criteria_plain_iterator_fallback_and_lookahead() -> None:
    """iter_records_by_criteria supports plain iterables for both __next__ and next_page batches."""
    from unittest.mock import MagicMock

    from imednet.models.records import Record
    from imednet_workflows.data_extraction import DataExtractionWorkflow

    raw_records = [
        Record(
            record_id=idx,
            study_key="S1",
            subject_key="SUB-OK" if idx % 2 == 1 else "SUB-SKIP",
            deleted=(idx == 3),
        )
        for idx in range(1, 225)
    ]

    mock_sdk = MagicMock()
    mock_sdk.get_subjects.return_value = [MagicMock(subject_key="SUB-OK")]
    mock_sdk.records.list.side_effect = lambda *_a, **_kw: (r for r in raw_records)

    wf = DataExtractionWorkflow(mock_sdk)
    stream = wf.iter_records_by_criteria(
        "S1",
        subject_filter={"subject_status": "Enrolled"},
    )
    first_item = next(stream)
    assert first_item.record_id == 1
    assert stream.has_more is True  # type: ignore[attr-defined]

    batch_one = stream.next_page()  # type: ignore[attr-defined]
    assert len(batch_one) == 100
    assert 3 not in [r.record_id for r in batch_one]
    assert stream.has_more is True  # type: ignore[attr-defined]

    batch_two = stream.next_page()  # type: ignore[attr-defined]
    assert len(batch_two) == 10
    assert stream.has_more is False  # type: ignore[attr-defined]
    assert stream.next_page() == []  # type: ignore[attr-defined]
