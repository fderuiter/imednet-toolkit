"""Regression tests for whole-second UTC bounds, Record timestamp reduction, and primary ID allowlists."""

from __future__ import annotations

import contextlib
import io
import json
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from imednet import cli
from imednet.endpoints.visits import VisitsEndpoint
from imednet.errors import ExportConfigurationError
from imednet.integrations._record_timestamp_bounds import (
    _combine_branch_with_watermark,
    _merge_and_condition,
    normalize_watermark_datetime,
)
from imednet.integrations.sink_base import SinkConfig, build_record_query_branches
from imednet.models.visits import Visit
from imednet.utils.dates import format_iso_datetime, normalize_utc_timestamp_bound
from imednet.utils.filters import build_filter_string, parse_filter_expressions


def _invoke_cli(argv: list[str]) -> tuple[int, str, str]:
    """Run ``cli.app(argv)`` capturing exit code, stdout, and stderr."""
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    exit_code = 0
    with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        try:
            cli.app(argv)
        except SystemExit as exc:
            exit_code = int(exc.code) if isinstance(exc.code, int) else 1
    return exit_code, out_buf.getvalue(), err_buf.getvalue()


@pytest.mark.parametrize(
    ("dt_input", "expected"),
    [
        (datetime(2026, 1, 15, 12, 30, 45, 999999), "2026-01-15T12:30:45Z"),
        (datetime(2026, 1, 15, 12, 30, 45, 1, tzinfo=UTC), "2026-01-15T12:30:45Z"),
        (
            datetime(2026, 1, 15, 14, 30, 45, 987654, tzinfo=timezone(timedelta(hours=2))),
            "2026-01-15T12:30:45Z",
        ),
    ],
)
def test_format_iso_datetime_truncates_fractional_seconds_toward_floor(
    dt_input: datetime,
    expected: str,
) -> None:
    """format_iso_datetime truncates microseconds toward the floor second in UTC."""
    assert format_iso_datetime(dt_input) == expected


@pytest.mark.parametrize(
    ("raw", "is_end", "expected"),
    [
        ("2026-03-10", False, "2026-03-10T00:00:00Z"),
        ("2026-03-10", True, "2026-03-10T23:59:59Z"),
        ("2026-03-10T08:15:59.999999Z", False, "2026-03-10T08:15:59Z"),
        ("2026-03-10T10:15:59.876543+02:00", True, "2026-03-10T08:15:59Z"),
        ("2024-01-02 00:00:00.500000+00:00", False, "2024-01-02T00:00:00Z"),
        ("2026-03-10T08:15:59.123456", False, "2026-03-10T08:15:59Z"),
    ],
)
def test_normalize_utc_timestamp_bound_emits_whole_second_utc(
    raw: str,
    is_end: bool,
    expected: str,
) -> None:
    """normalize_utc_timestamp_bound expands date-only inputs and floors fractional timestamps."""
    assert normalize_utc_timestamp_bound(raw, is_end=is_end) == expected


def test_visit_date_fields_preserve_yyyy_mm_dd_on_filter_serialization() -> None:
    """Visit date-only fields remain YYYY-MM-DD while Record timestamps expand to UTC."""
    visit_dates = parse_filter_expressions(
        [
            "startDate>=2026-01-01",
            "endDate<=2026-01-31",
            "dueDate==2026-01-15",
            "visitDate!=2026-01-20",
        ],
        model=Visit,
        allowed_attributes=VisitsEndpoint.FILTER_ATTRIBUTES,
    )
    assert build_filter_string(visit_dates) == (
        "startDate>=2026-01-01;endDate<=2026-01-31;dueDate==2026-01-15;visitDate!=2026-01-20"
    )


def test_record_export_normalizes_date_created_modified_and_last_updated_with_and_without_watermark(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normalize dateCreated/dateModified/lastUpdated bounds, evaluate post-predicates, and guard idempotency."""
    with pytest.raises(ExportConfigurationError, match="idempotent"):
        SinkConfig(study_key="S-GUARD", incremental=True, idempotent=False)

    assert normalize_watermark_datetime(None) is None
    assert normalize_watermark_datetime("2026-06-01") == datetime(2026, 6, 1, tzinfo=UTC)

    dm_upper_only = _combine_branch_with_watermark({"date_modified": ("<=", "2026-06-10")}, None)
    assert dm_upper_only == ({"dateModified": ("<=", "2026-06-10T23:59:59Z")}, None)

    dm_scalar_offset = _combine_branch_with_watermark(
        {"dateModified": "2026-06-10T14:30:15.987654+02:00"},
        None,
    )
    assert dm_scalar_offset == ({"dateModified": ("==", "2026-06-10T12:30:15Z")}, None)

    normalize_wm = normalize_watermark_datetime("2026-06-01T08:15:30.999999Z")
    assert normalize_wm is not None
    combined = _combine_branch_with_watermark(
        {
            "date_created": [(">=", "2026-05-01"), ("<=", "2026-05-10")],
            "last_updated": [(">", "2026-05-02"), ("!=", "2026-05-05T00:00:00Z")],
            "dateModified": ("<", "2026-06-20"),
        },
        normalize_wm,
    )
    assert combined is not None
    branch_dict, post_pred = combined
    assert branch_dict == {
        "dateModified": (">=", "2026-06-01T08:15:30Z"),
        "dateCreated": (">=", "2026-05-01T00:00:00Z"),
        "lastUpdated": (">", "2026-05-02T00:00:00Z"),
    }
    assert post_pred is not None
    assert (
        post_pred(
            {
                "dateCreated": "2026-05-10T23:59:59Z",
                "lastUpdated": date(2026, 5, 6),
                "dateModified": "2026-06-19T23:59:59Z",
            }
        )
        is True
    )
    assert (
        post_pred(
            SimpleNamespace(
                date_created="2026-05-10T12:00:00Z",
                last_updated=datetime(2026, 5, 6, 12, 0, tzinfo=UTC),
                date_modified="2026-06-10T00:00:00Z",
            )
        )
        is True
    )
    assert (
        post_pred(
            {
                "dateCreated": "2026-05-11T00:00:00Z",
                "lastUpdated": "2026-05-06T00:00:00Z",
                "dateModified": "2026-06-10T00:00:00Z",
            }
        )
        is False
    )
    assert (
        post_pred(
            {
                "dateCreated": "2026-05-05T00:00:00Z",
                "lastUpdated": "2026-05-05T00:00:00Z",
                "dateModified": "2026-06-10T00:00:00Z",
            }
        )
        is False
    )
    assert (
        post_pred(
            {
                "dateCreated": "2026-05-05T00:00:00Z",
                "lastUpdated": "2026-05-06T00:00:00Z",
                "dateModified": "2026-06-20T23:59:59Z",
            }
        )
        is False
    )
    assert post_pred({"dateCreated": True, "lastUpdated": None, "dateModified": "bad"}) is False

    assert (
        _combine_branch_with_watermark(
            {"dateCreated": [(">=", "2026-06-10"), ("<", "2026-06-01")]},
            None,
        )
        is None
    )
    assert (
        _combine_branch_with_watermark(
            {"lastUpdated": [("==", "2026-06-01"), ("!=", "2026-06-01")]},
            None,
        )
        is None
    )

    date_obj_branch = _combine_branch_with_watermark(
        {"dateCreated": ("<=", date(2026, 6, 15))},
        None,
    )
    assert date_obj_branch == ({"dateCreated": ("<=", "2026-06-15T23:59:59Z")}, None)

    hybrid_branches = build_record_query_branches(
        record_filter={
            "date_created": (">=", "2026-05-01"),
            "filter": "dateCreated<=2026-05-10",
        }
    )
    assert hybrid_branches == [{"dateCreated": (">=", "2026-05-01T00:00:00Z"), "deleted": False}]

    for bad_branch in (
        {"dateCreated": ("==", None)},
        {"dateCreated": ("==", True)},
        {"dateCreated": ("==", "   ")},
        {"dateCreated": ("==", 123)},
        {"dateCreated": ("==",)},
        {"dateCreated": [("==",)]},
        {"dateCreated": "2026-06-01", "date_created": "2026-06-01"},
    ):
        with pytest.raises(ValueError):
            _combine_branch_with_watermark(bad_branch, None)

    seen_http_filters: list[str] = []
    with respx.mock(assert_all_called=True) as router:

        def _cli_records_handler(request: httpx.Request) -> httpx.Response:
            flt = request.url.params.get("filter", "")
            seen_http_filters.append(flt)
            assert flt.count("dateCreated") == 1
            assert flt.count("dateModified") == 1
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "recordId": 10,
                            "subjectKey": "SUBJ-01",
                            "visitId": 1,
                            "formId": 10,
                            "formKey": "AE",
                            "deleted": False,
                            "dateCreated": "2026-05-10T12:00:00Z",
                            "dateModified": "2026-06-01T10:00:00Z",
                            "recordData": {"AGE": 41},
                        },
                        {
                            "recordId": 11,
                            "subjectKey": "SUBJ-02",
                            "visitId": 1,
                            "formId": 10,
                            "formKey": "AE",
                            "deleted": False,
                            "dateCreated": "2026-05-25T12:00:00Z",
                            "dateModified": "2026-06-01T10:00:00Z",
                            "recordData": {"AGE": 52},
                        },
                    ]
                },
            )

        router.get("https://api.test/api/v1/edc/studies/S-CLI-BOUNDS/records").mock(
            side_effect=_cli_records_handler
        )
        router.get("https://api.test/api/v1/edc/studies/S-CLI-BOUNDS/variables").respond(
            200,
            json={
                "data": [
                    {
                        "variableName": "AGE",
                        "variableType": "int",
                        "label": "Age",
                        "formId": 10,
                        "formKey": "AE",
                    }
                ]
            },
        )

        monkeypatch.setenv("IMEDNET_API_KEY", "k")
        monkeypatch.setenv("IMEDNET_SECURITY_KEY", "s")
        monkeypatch.setenv("IMEDNET_BASE_URL", "https://api.test")
        cli_out = tmp_path / "cli_bounds.json"
        code, _, stderr = _invoke_cli(
            [
                "export",
                "json",
                "S-CLI-BOUNDS",
                str(cli_out),
                "--since",
                "2026-06-01T10:00:00.999999Z",
                "--filter",
                "dateCreated>=2026-05-01",
                "--filter",
                "dateCreated<=2026-05-15",
            ]
        )
        assert code == 0, stderr
        rows = json.loads(cli_out.read_text(encoding="utf-8"))
        assert [r["recordId"] for r in rows] == [10]
        assert len(seen_http_filters) == 1
        assert "dateCreated>=2026-05-01T00:00:00Z" in seen_http_filters[0]
        assert "dateModified>=2026-06-01T10:00:00Z" in seen_http_filters[0]


def test_timestamp_clause_reduction_tightening_and_exclusion_edge_cases() -> None:
    """Verify boundary tightening, equality collapse, and multi-exclusion post-predicates."""
    # Boundary != upgrades >= to > and <= to <
    tightened = _combine_branch_with_watermark(
        {
            "dateCreated": [
                (">=", "2026-05-01T00:00:00Z"),
                ("!=", "2026-05-01T00:00:00Z"),
                ("!=", "2026-04-01T00:00:00Z"),
            ],
            "lastUpdated": [
                ("<=", "2026-05-10T23:59:59Z"),
                ("!=", "2026-05-10T23:59:59Z"),
                ("!=", "2026-05-20T00:00:00Z"),
            ],
        },
        None,
    )
    assert tightened == (
        {
            "dateCreated": (">", "2026-05-01T00:00:00Z"),
            "lastUpdated": ("<", "2026-05-10T23:59:59Z"),
        },
        None,
    )

    # Matching inclusive lower and upper collapse to == unless excluded or strict
    collapsed = _combine_branch_with_watermark(
        {"dateCreated": [(">=", "2026-05-01T12:00:00Z"), ("<=", "2026-05-01T12:00:00Z")]},
        None,
    )
    assert collapsed == ({"dateCreated": ("==", "2026-05-01T12:00:00Z")}, None)

    assert (
        _combine_branch_with_watermark(
            {"dateCreated": [(">", "2026-05-01T12:00:00Z"), ("<=", "2026-05-01T12:00:00Z")]},
            None,
        )
        is None
    )
    assert (
        _combine_branch_with_watermark(
            {"dateCreated": [("==", "2026-05-01T12:00:00Z"), (">", "2026-05-01T12:00:00Z")]},
            None,
        )
        is None
    )
    assert (
        _combine_branch_with_watermark(
            {"dateCreated": [("==", "2026-05-05T12:00:00Z"), ("<", "2026-05-05T12:00:00Z")]},
            None,
        )
        is None
    )

    # Multiple != clauses emit min exclusion upstream and filter remaining locally
    multi_neq = _combine_branch_with_watermark(
        {
            "dateCreated": [
                ("!=", "2026-05-03T00:00:00Z"),
                ("!=", "2026-05-01T00:00:00Z"),
            ]
        },
        None,
    )
    assert multi_neq is not None
    neq_branch, neq_pred = multi_neq
    assert neq_branch == {"dateCreated": ("!=", "2026-05-01T00:00:00Z")}
    assert neq_pred is not None
    assert neq_pred({"dateCreated": "2026-05-02T00:00:00Z"}) is True
    assert neq_pred({"dateCreated": "2026-05-03T00:00:00Z"}) is False

    # _merge_and_condition combines multi-bounds for timestamps and rejects non-timestamp duplicates
    merged_target: dict[str, Any] = {"date_created": "2026-05-01"}
    _merge_and_condition(merged_target, "dateCreated", [("<=", "2026-05-10"), "2026-05-01"])
    assert merged_target == {
        "dateCreated": [("==", "2026-05-01"), ("<=", "2026-05-10"), ("==", "2026-05-01")]
    }
    with pytest.raises(ValueError, match="Duplicate filter attribute 'formKey'"):
        _merge_and_condition({"form_key": "AE"}, "formKey", "CM")
    with pytest.raises(ValueError, match="must have 2 elements"):
        _merge_and_condition({"dateCreated": "2026-05-01"}, "dateCreated", ("<=",))
    with pytest.raises(ValueError, match="must have 2 elements"):
        _merge_and_condition({"dateCreated": "2026-05-01"}, "dateCreated", [("<=",)])


@respx.mock
@pytest.mark.parametrize(
    ("subcmd", "path_suffix", "camel_flag", "snake_flag", "expected_filter", "row"),
    [
        (
            "queries",
            "queries",
            "annotationId==701",
            "annotation_id=701",
            "annotationId==701",
            {"annotationId": 701, "subjectKey": "SUBJ-01", "type": "question"},
        ),
        (
            "codings",
            "codings",
            "codingId==1101",
            "coding_id=1101",
            "codingId==1101",
            {"codingId": 1101, "subjectKey": "SUBJ-01", "dictionaryName": "MedDRA"},
        ),
        (
            "visits",
            "visits",
            "visitId==401",
            "visit_id=401",
            "visitId==401",
            {"visitId": 401, "subjectKey": "SUBJ-01", "intervalName": "Baseline"},
        ),
    ],
)
def test_list_commands_accept_primary_ids_in_camel_and_snake_case(
    subcmd: str,
    path_suffix: str,
    camel_flag: str,
    snake_flag: str,
    expected_filter: str,
    row: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Queries, Codings, and Visits CLI list commands accept primary IDs in camelCase and snake_case."""
    monkeypatch.setenv("IMEDNET_API_KEY", "k")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "s")
    monkeypatch.setenv("IMEDNET_BASE_URL", "https://api.test")

    route = respx.get(f"https://api.test/api/v1/edc/studies/STUDY/{path_suffix}").mock(
        return_value=httpx.Response(200, json={"data": [row], "pagination": {"totalPages": 1}})
    )

    for flt in (camel_flag, snake_flag):
        route.reset()
        cli.app([subcmd, "list", "STUDY", "-f", flt, "--format", "json"])
        out, err = capfd.readouterr()
        assert err == ""
        assert route.call_count == 1
        assert dict(route.calls[0].request.url.params)["filter"] == expected_filter
        assert json.loads(out) != []
