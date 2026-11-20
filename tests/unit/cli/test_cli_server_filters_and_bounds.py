"""End-to-end CLI and HTTP contract tests for server filters, sort, limit, and bounded fetches."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from imednet import ImednetSDK, cli
from imednet.cli.utils.grid import InteractiveDataGrid
from imednet.core.paginator import Paginator


def _make_sdk() -> ImednetSDK:
    return ImednetSDK(
        api_key="test-key",  # pragma: allowlist secret
        security_key="test-secret",  # pragma: allowlist secret
        base_url="https://api.test",
    )


def _paginated_payload(
    items: list[dict[str, Any]],
    *,
    page: int = 0,
    size: int = 100,
    total_pages: int = 1,
    total_items: int | None = None,
) -> dict[str, Any]:
    return {
        "data": items,
        "pagination": {
            "page": page,
            "size": size,
            "totalPages": total_pages,
            "totalItems": total_items if total_items is not None else len(items),
        },
    }


class _CaptureWindow:
    """Minimal curses window double capturing rendered lines for assertion."""

    def __init__(self, height: int = 24, width: int = 120) -> None:
        self.height = height
        self.width = width
        self.lines: dict[int, str] = {}

    def getmaxyx(self) -> tuple[int, int]:
        return self.height, self.width

    def erase(self) -> None:
        self.lines.clear()

    def refresh(self) -> None:
        pass

    def keypad(self, flag: bool) -> None:
        pass

    def move(self, y: int, x: int) -> None:
        pass

    def addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        existing = self.lines.get(y, " " * self.width)
        padded = existing.ljust(self.width)
        updated = padded[:x] + text + padded[x + len(text) :]
        self.lines[y] = updated[: self.width]


@respx.mock
def test_records_list_forwards_filter_record_data_filter_sort_and_limit(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """Acceptance Criterion 1: records list forwards filter, recordDataFilter, sort, and size=50."""
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", _make_sdk)
    monkeypatch.setattr("imednet.cli.utils.grid.is_interactive_terminal", lambda: False)

    page_items = [
        {
            "recordId": 100 + idx,
            "studyKey": "STUDY1",
            "subjectKey": f"SUBJ-{idx}",
            "formKey": "AE",
            "recordStatus": "Record Complete",
            "dateCreated": "2026-01-15T10:00:00Z",
        }
        for idx in range(1, 51)
    ]
    route = respx.get("https://api.test/api/v1/edc/studies/STUDY1/records").mock(
        return_value=httpx.Response(
            200,
            json=_paginated_payload(
                page_items,
                page=0,
                size=50,
                total_pages=3,
                total_items=150,
            ),
        )
    )

    cli.app(
        [
            "records",
            "list",
            "STUDY1",
            "--filter",
            "formKey==AE;recordId>=100",
            "--record-data-filter",
            "AESER==Serious;AGE>=18",
            "--sort",
            "dateCreated,desc",
            "--limit",
            "50",
        ]
    )

    assert route.call_count == 1
    req_params = dict(route.calls[0].request.url.params)
    assert req_params["page"] == "0"
    assert req_params["size"] == "50"
    assert req_params["sort"] == "dateCreated,desc"
    assert req_params["recordDataFilter"] == "AESER==Serious;AGE>=18"
    assert "formKey==AE" in req_params["filter"]
    assert "recordId>=100" in req_params["filter"]
    assert "sort" not in req_params["filter"]
    assert "size" not in req_params["filter"]
    assert "page" not in req_params["filter"]

    out = capfd.readouterr().out
    assert "SUBJ-1" in out
    assert "Notice: Showing first 50 records" in out


@pytest.mark.parametrize(
    ("subcommand", "endpoint_path", "needs_study", "sample_item"),
    [
        ("studies", "/api/v1/edc/studies", False, {"studyKey": "S1", "studyName": "Trial 1"}),
        ("sites", "/api/v1/edc/studies/S1/sites", True, {"siteId": 1, "siteName": "Site A"}),
        ("forms", "/api/v1/edc/studies/S1/forms", True, {"formId": 10, "formKey": "DM"}),
        (
            "intervals",
            "/api/v1/edc/studies/S1/intervals",
            True,
            {"intervalId": 2, "intervalName": "V1"},
        ),
        ("queries", "/api/v1/edc/studies/S1/queries", True, {"annotationId": 3, "type": "Open"}),
        (
            "record-revisions",
            "/api/v1/edc/studies/S1/recordRevisions",
            True,
            {"recordRevisionId": 4},
        ),
        (
            "variables",
            "/api/v1/edc/studies/S1/variables",
            True,
            {"variableId": 5, "variableName": "AGE"},
        ),
        ("visits", "/api/v1/edc/studies/S1/visits", True, {"visitId": 6}),
        ("records", "/api/v1/edc/studies/S1/records", True, {"recordId": 7, "subjectKey": "SUB-1"}),
        (
            "subjects",
            "/api/v1/edc/studies/S1/subjects",
            True,
            {"subjectId": 8, "subjectKey": "SUB-1"},
        ),
        ("codings", "/api/v1/edc/studies/S1/codings", True, {"codingId": 9, "variable": "AE"}),
    ],
)
@respx.mock
def test_all_eleven_filterable_list_commands_accept_filter_sort_limit_and_all(
    subcommand: str,
    endpoint_path: str,
    needs_study: bool,
    sample_item: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Acceptance Criterion 2 & Req 1/4/5: All 11 filterable list commands forward top-level params."""
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", _make_sdk)
    monkeypatch.setattr("imednet.cli.utils.grid.is_interactive_terminal", lambda: False)

    route = respx.get(f"https://api.test{endpoint_path}").mock(
        return_value=httpx.Response(
            200,
            json=_paginated_payload([sample_item], page=0, size=25, total_pages=1),
        )
    )

    filter_expr = (
        "studyKey==S1,lastUpdated>=2025-01-01"
        if not needs_study
        else "lastUpdated>=2025-01-01,lastUpdated<=2025-12-31"
    )
    argv = [subcommand, "list"]
    if needs_study:
        argv.append("S1")
    argv.extend(
        [
            "-f",
            filter_expr,
            "--sort",
            "lastUpdated,asc",
            "--limit",
            "25",
            "--format",
            "json",
        ]
    )
    cli.app(argv)
    capfd.readouterr()

    assert route.call_count == 1
    params = dict(route.calls[0].request.url.params)
    assert params["filter"] == filter_expr
    assert params["sort"] == "lastUpdated,asc"
    assert params["size"] == "25"
    assert params["page"] == "0"

    # Also verify --all is accepted by the parser on every command
    route.reset()
    argv_all = [subcommand, "list"]
    if needs_study:
        argv_all.append("S1")
    argv_all.extend(["--all", "--format", "json"])
    cli.app(argv_all)
    capfd.readouterr()
    assert route.call_count == 1


@respx.mock
def test_users_list_supports_sort_limit_all_and_omits_filter(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """Req 4 & Constraint 3: users list supports --sort/--limit/--all and omits --filter."""
    sdk = _make_sdk()
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda: sdk)

    route = respx.get("https://api.test/api/v1/edc/studies/S1/users").mock(
        return_value=httpx.Response(
            200,
            json=_paginated_payload([{"userId": "u1", "login": "alice"}], page=0, size=10),
        )
    )

    cli.app(
        [
            "users",
            "list",
            "S1",
            "--include-inactive",
            "--sort",
            "login,asc",
            "--limit",
            "10",
            "--format",
            "json",
        ]
    )
    out = capfd.readouterr().out
    payload = json.loads(out)
    assert payload["schemaVersion"] == "1.0"
    assert payload["count"] == 1

    params = dict(route.calls[0].request.url.params)
    assert params == {
        "includeInactive": "true",
        "sort": "login,asc",
        "size": "10",
        "page": "0",
    }

    # Passing --filter to users list must be rejected by argparse with exit code 2
    with pytest.raises(SystemExit) as exc_info:
        cli.app(["users", "list", "S1", "--filter", "login==alice"])
    assert exc_info.value.code == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["records", "list", "S1", "--filter", "formKey==DM;recordId>1,subjectKey==S01"],
        ["records", "list", "S1", "-f", "formKey==DM,formKey==VS", "-f", "recordId>1"],
        ["records", "list", "S1", "--record-data-filter", "AESER==Serious;AGE>18,SEX==F"],
        ["subjects", "list", "S1", "--filter", "subjectStatus==Screened;siteId==1,siteId==2"],
        ["forms", "list", "S1", "--filter", "formId>10;formType==SUBJECT,formKey==DM"],
    ],
)
@respx.mock
def test_mixed_semicolon_and_comma_connectors_exit_one_before_http(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Acceptance Criterion 3 & Constraint 2: mixed ';' and ',' connectors exit 1 on stderr."""
    sdk = _make_sdk()
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda: sdk)
    route = respx.get(url__regex=r"https://api\.test/.*").mock(
        return_value=httpx.Response(200, json=_paginated_payload([]))
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.app(argv)

    assert exc_info.value.code == 1
    assert route.call_count == 0
    captured = capfd.readouterr()
    assert captured.out == ""
    assert "cannot combine both ';' (AND) and ',' (OR)" in captured.err


@respx.mock
def test_interactive_records_list_2500_study_issues_single_http_request_and_shows_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance Criterion 4 & Req 6/7: 2,500-record Study issues 1 HTTP request in interactive TTY."""
    sdk = _make_sdk()
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda: sdk)
    monkeypatch.setattr("imednet.cli.utils.grid.is_interactive_terminal", lambda: True)

    page_zero_items = [
        {
            "recordId": i,
            "studyKey": "LARGE_STUDY",
            "subjectKey": f"SUBJ-{i:04d}",
            "formKey": "VS",
            "recordStatus": "Record Complete",
            "dateCreated": "2026-02-01T00:00:00Z",
        }
        for i in range(1, 101)
    ]

    route = respx.get("https://api.test/api/v1/edc/studies/LARGE_STUDY/records").mock(
        return_value=httpx.Response(
            200,
            json=_paginated_payload(
                page_zero_items,
                page=0,
                size=100,
                total_pages=25,
                total_items=2500,
            ),
        )
    )

    captured_grid_kwargs: dict[str, Any] = {}

    def _fake_run_grid(**kwargs: Any) -> bool:
        captured_grid_kwargs.update(kwargs)
        return True

    monkeypatch.setattr("imednet.cli.utils.grid.run_interactive_grid", _fake_run_grid)

    cli.app(["records", "list", "LARGE_STUDY"])

    assert route.call_count == 1
    params = dict(route.calls[0].request.url.params)
    assert params["page"] == "0"
    assert params["size"] == "100"
    assert len(captured_grid_kwargs["items"]) == 100
    assert captured_grid_kwargs["truncated"] is True
    assert callable(captured_grid_kwargs["on_load_all"])
    assert "truncated" in captured_grid_kwargs["status_notice"].lower()
    assert "[a]" in captured_grid_kwargs["status_notice"]
    assert "--all" in captured_grid_kwargs["status_notice"]

    # Verify InteractiveDataGrid renders unclipped notices on both wide (140x20) and 80x24 viewports
    for height, width in ((20, 140), (24, 80)):
        win = _CaptureWindow(height=height, width=width)
        with (
            patch("curses.has_colors", return_value=False),
            patch("curses.curs_set"),
        ):
            grid = InteractiveDataGrid(
                win,
                items=captured_grid_kwargs["items"],
                fields=["ID", "Subject", "Form", "Status", "Created"],
                label="records",
                details=captured_grid_kwargs["details"],
                status_notice=captured_grid_kwargs["status_notice"],
                truncated=captured_grid_kwargs["truncated"],
                on_load_all=captured_grid_kwargs["on_load_all"],
            )
            grid.draw()

        for row_idx in (0, height - 1):
            assert "truncated" in win.lines[row_idx].lower()
            assert "[a]" in win.lines[row_idx]
            assert "--all" in win.lines[row_idx]

        if width == 80:
            grid._handle_normal_input(" ")
            grid.draw()
            top_grid_pane = win.lines[0][:43]
            assert "truncated" in top_grid_pane.lower()
            assert "[a]" in top_grid_pane
            assert "--all" in top_grid_pane
            assert "[[/]] Scroll" in win.lines[23]
            assert "[Esc] Close" in win.lines[23]
            assert "[a]" in win.lines[23]
            assert "--all" in win.lines[23]


@pytest.mark.parametrize(
    ("argv", "endpoint_path", "item_factory", "match_token"),
    [
        (
            ["studies", "list"],
            "/api/v1/edc/studies",
            lambda n: {"studyKey": f"ST-{n:04d}", "studyName": f"Study {n}"},
            "ST-0225",
        ),
        (
            ["record-revisions", "list", "S1"],
            "/api/v1/edc/studies/S1/recordRevisions",
            lambda n: {"recordRevisionId": n, "studyKey": "S1", "subjectKey": f"REV-{n:04d}"},
            "REV-0225",
        ),
        (
            ["records", "list", "S1"],
            "/api/v1/edc/studies/S1/records",
            lambda n: {
                "recordId": n,
                "studyKey": "S1",
                "subjectKey": f"REC-{n:04d}",
                "formKey": "DM",
                "recordStatus": "Active",
            },
            "REC-0225",
        ),
        (
            ["subjects", "list", "S1"],
            "/api/v1/edc/studies/S1/subjects",
            lambda n: {"subjectId": n, "studyKey": "S1", "subjectKey": f"SUB-{n:04d}"},
            "SUB-0225",
        ),
        (
            ["users", "list", "S1"],
            "/api/v1/edc/studies/S1/users",
            lambda n: {"userId": f"U-{n}", "login": f"user_{n:04d}"},
            "user_0225",
        ),
        (
            ["codings", "list", "S1"],
            "/api/v1/edc/studies/S1/codings",
            lambda n: {"codingId": n, "studyKey": "S1", "code": f"CD-{n:04d}", "variable": "AE"},
            "CD-0225",
        ),
    ],
)
@respx.mock
def test_interactive_cli_list_key_a_drains_remaining_pages_without_refetching_page_zero(
    argv: list[str],
    endpoint_path: str,
    item_factory: Any,
    match_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pressing 'a' in InteractiveDataGrid drains pages 1..2 without re-fetching page 0 and updates filter."""
    sdk = _make_sdk()
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda: sdk)
    monkeypatch.setattr("imednet.cli.utils.grid.is_interactive_terminal", lambda: True)

    requested_pages: list[int] = []

    def _responder(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        size = int(request.url.params["size"])
        requested_pages.append(page)
        start = page * 100
        count = 100 if page < 2 else 50
        data = [item_factory(start + idx + 1) for idx in range(count)]
        return httpx.Response(
            200,
            json=_paginated_payload(data, page=page, size=size, total_pages=3, total_items=250),
        )

    respx.get(f"https://api.test{endpoint_path}").mock(side_effect=_responder)

    observed_final_matches: list[int] = []
    observed_total_entries: list[int] = []

    def _drive_grid(**kwargs: Any) -> bool:
        # Before pressing 'a', only page 0 has been fetched
        assert requested_pages == [0]
        win = _CaptureWindow(height=24, width=80)
        with (
            patch("curses.has_colors", return_value=False),
            patch("curses.curs_set"),
        ):
            grid = InteractiveDataGrid(
                win,
                items=kwargs["items"],
                fields=kwargs["fields"],
                label=kwargs["label"],
                details=kwargs["details"],
                status_notice=kwargs.get("status_notice"),
                truncated=kwargs.get("truncated"),
                on_load_all=kwargs.get("on_load_all"),
            )
            grid._handle_normal_input("/")
            for ch in match_token:
                grid._handle_search_input(ch)
            grid._handle_search_input(10)
            assert len(grid.filtered_entries) == 0

            # Press 'a' while SDK context is still live inside @with_sdk
            grid._handle_normal_input("a")
            observed_total_entries.append(len(grid.all_entries))
            observed_final_matches.append(len(grid.filtered_entries))
        return True

    monkeypatch.setattr("imednet.cli.utils.grid.run_interactive_grid", _drive_grid)
    cli.app(argv)

    assert requested_pages == [0, 1, 2]
    assert observed_total_entries == [250]
    assert observed_final_matches == [1]


def test_collect_list_items_with_remainder_handles_sequences_and_plain_iterators() -> None:
    """collect_list_items_with_remainder drains Sequence and non-paginated Iterable remainders once."""
    from imednet.cli.utils.args import collect_list_items_with_remainder

    seq_items, seq_trunc, seq_more = collect_list_items_with_remainder([1, 2, 3, 4, 5], 3)
    assert seq_items == [1, 2, 3]
    assert seq_trunc is True
    assert seq_more is not None
    assert seq_more() == [4, 5]
    assert seq_more() == []

    gen_items, gen_trunc, gen_more = collect_list_items_with_remainder((x for x in range(6)), 4)
    assert gen_items == [0, 1, 2, 3]
    assert gen_trunc is True
    assert gen_more is not None
    assert gen_more() == [4, 5]
    assert gen_more() == []


@respx.mock
def test_non_interactive_json_fetches_all_pages_and_emits_v1_envelope(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """Acceptance Criterion 5 & Constraint 4: --format json without --limit fetches all pages."""
    sdk = _make_sdk()
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda: sdk)

    def _page_handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        size = int(request.url.params["size"])
        assert size == 100
        start = page * 100
        count = 100 if page < 2 else 50
        items = [
            {"recordId": start + idx + 1, "studyKey": "S1", "subjectKey": f"S-{start + idx + 1}"}
            for idx in range(count)
        ]
        return httpx.Response(
            200,
            json=_paginated_payload(items, page=page, size=size, total_pages=3, total_items=250),
        )

    route = respx.get("https://api.test/api/v1/edc/studies/S1/records").mock(
        side_effect=_page_handler
    )

    cli.app(["records", "list", "S1", "--format", "json"])

    assert route.call_count == 3
    captured = capfd.readouterr()
    envelope = json.loads(captured.out)
    assert envelope["schemaVersion"] == "1.0"
    assert envelope["resource"] == "records"
    assert envelope["status"] == "complete"
    assert envelope["count"] == 250
    assert len(envelope["items"]) == 250


@respx.mock
def test_upstream_size_capped_at_500_when_limit_exceeds_500(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """Constraint 1: --limit 1200 caps upstream size=500 per page and stops at 1,200 items."""
    sdk = _make_sdk()
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda: sdk)

    def _page_handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        size = int(request.url.params["size"])
        assert size == 500
        start = page * 500
        items = [{"recordId": start + idx + 1, "studyKey": "S1"} for idx in range(500)]
        return httpx.Response(
            200,
            json=_paginated_payload(items, page=page, size=size, total_pages=5, total_items=2500),
        )

    route = respx.get("https://api.test/api/v1/edc/studies/S1/records").mock(
        side_effect=_page_handler
    )

    cli.app(["records", "list", "S1", "--limit", "1200", "--format", "json"])

    assert route.call_count == 3
    envelope = json.loads(capfd.readouterr().out)
    assert envelope["count"] == 1200

    # Direct Paginator instantiation enforces 1 <= page_size <= 500
    paginator = Paginator(MagicMock(), "/items", page_size=500)
    assert paginator.page_size == 500
    with pytest.raises(ValueError, match="between 1 and 500"):
        Paginator(MagicMock(), "/items", page_size=0)
    with pytest.raises(ValueError, match="between 1 and 500"):
        Paginator(MagicMock(), "/items", page_size=999)


@pytest.mark.parametrize(
    ("argv", "expected_err"),
    [
        (["records", "list", "S1", "--limit", "0"], "--limit must be a positive integer"),
        (["records", "list", "S1", "--limit", "-5"], "--limit must be a positive integer"),
        (["records", "list", "S1", "--limit", "10", "--all"], "Cannot combine --limit and --all"),
        (["records", "list", "S1", "--sort", "   "], "--sort cannot be empty"),
        (
            ["records", "list", "S1", "--record-data-filter", "   "],
            "Invalid record data filter",
        ),
        (
            ["records", "list", "S1", "--record-data-filter", "AGE >= 18"],
            "contains whitespace",
        ),
    ],
)
def test_cli_query_validation_errors_exit_one(
    argv: list[str],
    expected_err: str,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Invalid --limit, --all, --sort, and --record-data-filter combinations exit 1 on stderr."""
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda: MagicMock())
    with pytest.raises(SystemExit) as exc_info:
        cli.app(argv)
    assert exc_info.value.code == 1
    captured = capfd.readouterr()
    assert captured.out == ""
    assert expected_err in captured.err


@pytest.mark.parametrize(
    ("argv", "expected_model"),
    [
        (["studies", "list", "--filter", "visit_key=SCREENING"], "Study"),
        (["sites", "list", "S1", "--filter", "visit_key=SCREENING"], "Site"),
        (["forms", "list", "S1", "--filter", "visit_key=SCREENING"], "Form"),
        (["variables", "list", "S1", "--filter", "visit_key=SCREENING"], "Variable"),
        (["intervals", "list", "S1", "--filter", "visit_key=SCREENING"], "Interval"),
        (["visits", "list", "S1", "--filter", "visit_key=SCREENING"], "Visit"),
        (["queries", "list", "S1", "--filter", "visit_key=SCREENING"], "Query"),
        (["record-revisions", "list", "S1", "--filter", "visit_key=SCREENING"], "RecordRevision"),
        (["records", "list", "S1", "--filter", "visit_key=SCREENING"], "Record"),
        (["subjects", "list", "S1", "--filter", "visit_key=SCREENING"], "Subject"),
        (["codings", "list", "S1", "--filter", "visit_key=SCREENING"], "Coding"),
    ],
)
@respx.mock
def test_all_eleven_list_commands_reject_unknown_model_filter_attributes(
    argv: list[str],
    expected_model: str,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """All 11 filterable list commands validate filter keys against their resource model before HTTP."""
    sdk = _make_sdk()
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda: sdk)
    route = respx.get(url__regex=r"https://api\.test/.*").mock(
        return_value=httpx.Response(200, json=_paginated_payload([]))
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.app(argv)

    assert exc_info.value.code == 1
    assert route.call_count == 0
    captured = capfd.readouterr()
    assert captured.out == ""
    assert f"Unknown filter attribute 'visit_key' for {expected_model}" in captured.err


@respx.mock
def test_interactive_all_flag_fetches_every_page_without_truncation_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance Criterion 3: interactive --all fetches all pages without truncation notice."""
    sdk = _make_sdk()
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda: sdk)
    monkeypatch.setattr("imednet.cli.utils.grid.is_interactive_terminal", lambda: True)

    def _responder(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        size = int(request.url.params["size"])
        assert size == 100
        count = 100 if page < 2 else 35
        start = page * 100
        items = [
            {"recordId": start + idx + 1, "studyKey": "S1", "subjectKey": f"SUB-{start + idx + 1}"}
            for idx in range(count)
        ]
        return httpx.Response(
            200,
            json=_paginated_payload(items, page=page, size=size, total_pages=3, total_items=235),
        )

    route = respx.get("https://api.test/api/v1/edc/studies/S1/records").mock(side_effect=_responder)
    captured_grid: dict[str, Any] = {}
    monkeypatch.setattr(
        "imednet.cli.utils.grid.run_interactive_grid",
        lambda **kwargs: captured_grid.update(kwargs) or True,
    )

    cli.app(["records", "list", "S1", "--all"])

    assert route.call_count == 3
    assert len(captured_grid["items"]) == 235
    assert captured_grid.get("status_notice") is None


@respx.mock
def test_interactive_exact_100_items_single_page_is_not_marked_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance Criterion 2: exact 100-item single-page study reports truncated=False."""
    sdk = _make_sdk()
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda: sdk)
    monkeypatch.setattr("imednet.cli.utils.grid.is_interactive_terminal", lambda: True)

    items = [
        {"recordId": idx, "studyKey": "EXACT_100", "subjectKey": f"SUB-{idx}"}
        for idx in range(1, 101)
    ]
    route = respx.get("https://api.test/api/v1/edc/studies/EXACT_100/records").mock(
        return_value=httpx.Response(
            200,
            json=_paginated_payload(items, page=0, size=100, total_pages=1, total_items=100),
        )
    )
    captured_grid: dict[str, Any] = {}
    monkeypatch.setattr(
        "imednet.cli.utils.grid.run_interactive_grid",
        lambda **kwargs: captured_grid.update(kwargs) or True,
    )

    cli.app(["records", "list", "EXACT_100"])

    assert route.call_count == 1
    assert len(captured_grid["items"]) == 100
    assert captured_grid.get("status_notice") is None


@pytest.mark.parametrize("export_fmt", ["json", "csv"])
@respx.mock
def test_records_list_output_file_preserves_full_fetch_even_in_interactive_tty(
    export_fmt: str,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Acceptance Criterion 3: --output json|csv fetches all pages regardless of TTY interactivity."""
    monkeypatch.chdir(tmp_path)
    sdk = _make_sdk()
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda: sdk)
    monkeypatch.setattr("imednet.cli.utils.grid.is_interactive_terminal", lambda: True)

    def _responder(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        count = 100 if page == 0 else 25
        start = page * 100
        data = [
            {
                "recordId": start + idx + 1,
                "studyKey": "S1",
                "subjectKey": f"SUB-{start + idx + 1}",
                "recordData": {"AGE": 30},
            }
            for idx in range(count)
        ]
        return httpx.Response(
            200,
            json=_paginated_payload(data, page=page, size=100, total_pages=2, total_items=125),
        )

    route = respx.get("https://api.test/api/v1/edc/studies/S1/records").mock(side_effect=_responder)
    cli.app(["records", "list", "S1", "--output", export_fmt])
    capfd.readouterr()

    assert route.call_count == 2
    out_file = tmp_path / f"records.{export_fmt}"
    assert out_file.exists()
    content = out_file.read_text(encoding="utf-8")
    assert "SUB-125" in content


@respx.mock
def test_console_script_entry_point_dispatches_sys_argv_offline(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Acceptance Criterion 4: installed 'imednet' console_scripts entry point dispatches offline."""
    import importlib.metadata

    eps = [
        ep
        for ep in importlib.metadata.entry_points(group="console_scripts")
        if ep.name == "imednet"
    ]
    assert len(eps) == 1
    entry_callable = eps[0].load()
    assert entry_callable is cli.app

    sdk = _make_sdk()
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda: sdk)
    route = respx.get("https://api.test/api/v1/edc/studies").mock(
        return_value=httpx.Response(
            200,
            json=_paginated_payload(
                [
                    {"studyKey": f"ENTRY-{idx}", "studyName": f"Entry Trial {idx}"}
                    for idx in range(1, 16)
                ],
                page=0,
                size=15,
                total_pages=2,
                total_items=30,
            ),
        )
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "imednet",
            "studies",
            "list",
            "--sort",
            "studyKey,asc",
            "--limit",
            "15",
            "--format",
            "json",
        ],
    )

    entry_callable()

    assert route.call_count == 1
    assert dict(route.calls[0].request.url.params) == {
        "sort": "studyKey,asc",
        "size": "15",
        "page": "0",
    }
    envelope = json.loads(capfd.readouterr().out)
    assert envelope["count"] == 15
    assert envelope["items"][0]["studyKey"] == "ENTRY-1"


@respx.mock
def test_subjects_and_codings_preserve_zero_padded_keys_and_superscripts(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Zero-padded subject_key=001, code=0042, and value=² emit exact server filter strings."""
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", _make_sdk)

    subj_route = respx.get("https://api.test/api/v1/edc/studies/S1/subjects").mock(
        return_value=httpx.Response(
            200,
            json=_paginated_payload(
                [{"subjectId": 1, "studyKey": "S1", "subjectKey": "001", "subjectStatus": "Active"}]
            ),
        )
    )
    cli.app(["subjects", "list", "S1", "-f", "subject_key=001", "--format", "json"])
    subj_out = json.loads(capfd.readouterr().out)
    assert subj_route.call_count == 1
    assert dict(subj_route.calls[0].request.url.params)["filter"] == "subjectKey==001"
    assert subj_out["count"] == 1
    assert subj_out["items"][0]["subjectKey"] == "001"

    coding_route = respx.get("https://api.test/api/v1/edc/studies/S1/codings").mock(
        return_value=httpx.Response(
            200,
            json=_paginated_payload(
                [
                    {
                        "codingId": 9,
                        "studyKey": "S1",
                        "code": "0042",
                        "value": "kg/m²",
                        "revision": 2,
                    }
                ]
            ),
        )
    )
    cli.app(
        [
            "codings",
            "list",
            "S1",
            "-f",
            "code=0042",
            "-f",
            "revision>=2",
            "--format",
            "json",
        ]
    )
    coding_out = json.loads(capfd.readouterr().out)
    assert coding_route.call_count == 1
    assert dict(coding_route.calls[0].request.url.params)["filter"] == "code==0042;revision>=2"
    assert coding_out["count"] == 1
    assert coding_out["items"][0]["code"] == "0042"

    coding_route.reset()
    cli.app(["codings", "list", "S1", "-f", "value=²", "--format", "json"])
    capfd.readouterr()
    assert coding_route.call_count == 1
    assert dict(coding_route.calls[0].request.url.params)["filter"] == 'value=="²"'


@pytest.mark.parametrize(
    ("argv", "expected_err"),
    [
        (
            ["codings", "list", "S1", "-f", "revision=abc"],
            "Invalid integer value 'abc' for attribute 'revision'",
        ),
        (
            ["forms", "list", "S1", "-f", "revision=abc"],
            "Invalid integer value 'abc' for attribute 'revision'",
        ),
        (
            ["record-revisions", "list", "S1", "-f", "record_revision=abc"],
            "Invalid integer value 'abc' for attribute 'record_revision'",
        ),
        (
            ["forms", "list", "S1", "-f", "disabled=notabool"],
            "Invalid boolean value 'notabool' for attribute 'disabled'",
        ),
        (
            ["records", "list", "S1", "-f", "deleted=2"],
            "Invalid boolean value '2' for attribute 'deleted'",
        ),
    ],
)
@respx.mock
def test_cli_list_commands_reject_invalid_numeric_and_boolean_values_before_http(
    argv: list[str],
    expected_err: str,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Invalid integer or boolean filter values exit 1 with diagnostic on stderr and zero HTTP calls."""
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", _make_sdk)
    route = respx.get(url__regex=r"https://api\.test/.*").mock(
        return_value=httpx.Response(200, json=_paginated_payload([]))
    )

    with pytest.raises(SystemExit) as exc_info:
        cli.app(argv)

    assert exc_info.value.code == 1
    assert route.call_count == 0
    captured = capfd.readouterr()
    assert captured.out == ""
    assert expected_err in captured.err


@respx.mock
def test_records_list_record_data_filter_with_json_and_file_output(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """records list forwards --record-data-filter alone and alongside -f with --format json and --output."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", _make_sdk)

    route = respx.get("https://api.test/api/v1/edc/studies/S1/records").mock(
        return_value=httpx.Response(
            200,
            json=_paginated_payload(
                [
                    {
                        "recordId": 10,
                        "studyKey": "S1",
                        "subjectKey": "001",
                        "formKey": "VS",
                        "recordData": {"SYSBP": 140},
                    }
                ]
            ),
        )
    )

    cli.app(
        [
            "records",
            "list",
            "S1",
            "-f",
            "subject_key=001",
            "--record-data-filter",
            "SYSBP>=130",
            "--format",
            "json",
        ]
    )
    envelope = json.loads(capfd.readouterr().out)
    assert route.call_count == 1
    params = dict(route.calls[0].request.url.params)
    assert params["filter"] == "subjectKey==001"
    assert params["recordDataFilter"] == "SYSBP>=130"
    assert envelope["count"] == 1
    assert envelope["items"][0]["subjectKey"] == "001"

    route.reset()
    cli.app(
        [
            "records",
            "list",
            "S1",
            "--record-data-filter",
            "SYSBP>=130",
            "--output",
            "json",
        ]
    )
    capfd.readouterr()
    assert route.call_count == 1
    params_out = dict(route.calls[0].request.url.params)
    assert "filter" not in params_out
    assert params_out["recordDataFilter"] == "SYSBP>=130"
    assert (tmp_path / "records.json").exists()


@pytest.mark.parametrize(
    "subcommand",
    [
        "sites",
        "forms",
        "intervals",
        "queries",
        "record-revisions",
        "variables",
        "visits",
        "records",
        "subjects",
        "codings",
    ],
)
@pytest.mark.parametrize("study_filter", ["study_key=STUDY", "studyKey==STUDY"])
def test_all_ten_study_scoped_list_commands_reject_study_key_in_preflight(
    subcommand: str,
    study_filter: str,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Acceptance Criterion 1: -f study_key=STUDY exits 1 in preflight on all 10 Study-scoped list commands."""
    monkeypatch.delenv("IMEDNET_API_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    mock_get_sdk = MagicMock(side_effect=AssertionError("get_sdk must not be called in preflight"))
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", mock_get_sdk)

    with pytest.raises(SystemExit) as exc_info:
        cli.app([subcommand, "list", "STUDY", "-f", study_filter])

    assert exc_info.value.code == 1
    mock_get_sdk.assert_not_called()
    captured = capfd.readouterr()
    assert captured.out == ""
    assert "'study_key' and 'studyKey' cannot be used in --filter" in captured.err
    assert "Unexpected error" not in captured.err


@respx.mock
def test_studies_list_preserves_study_key_and_study_key_camel_filters(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Guardrail: study_key and studyKey remain valid on `imednet studies list`."""
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", _make_sdk)
    route = respx.get("https://api.test/api/v1/edc/studies").mock(
        return_value=httpx.Response(
            200,
            json=_paginated_payload([{"studyKey": "STUDY", "studyName": "Demo"}]),
        )
    )

    for flt in ("study_key=STUDY", "studyKey==STUDY"):
        route.reset()
        cli.app(["studies", "list", "-f", flt, "--format", "json"])
        capfd.readouterr()
        assert route.call_count == 1
        assert dict(route.calls[0].request.url.params)["filter"] == "studyKey==STUDY"


@pytest.mark.parametrize(
    ("argv", "expected_attr"),
    [
        (["queries", "list", "STUDY", "-f", "status=Open"], "annotationType"),
        (["visits", "list", "STUDY", "-f", "visit_id=1"], "intervalName"),
        (["codings", "list", "STUDY", "-f", "coding_id=1"], "dictionaryName"),
        (["intervals", "list", "STUDY", "-f", "forms=DEMO"], "intervalName"),
        (["records", "list", "STUDY", "-f", "keywords=urgent"], "formKey"),
        (["subjects", "list", "STUDY", "-f", "keywords=vip"], "subjectStatus"),
    ],
)
def test_list_commands_reject_non_allowlisted_attributes_and_list_valid_attributes(
    argv: list[str],
    expected_attr: str,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Acceptance Criterion 2: non-allowlisted attributes exit 1 during preflight and list valid attributes."""
    monkeypatch.delenv("IMEDNET_API_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    mock_get_sdk = MagicMock(side_effect=AssertionError("get_sdk must not be called in preflight"))
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", mock_get_sdk)

    with pytest.raises(SystemExit) as exc_info:
        cli.app(argv)

    assert exc_info.value.code == 1
    mock_get_sdk.assert_not_called()
    captured = capfd.readouterr()
    assert captured.out == ""
    assert "Valid attributes:" in captured.err
    assert expected_attr in captured.err
    assert "Unexpected error" not in captured.err


def test_help_text_and_preflight_enforce_table2_for_filter_and_table7_for_record_data_filter(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Acceptance Criterion 3: --help omits =~ from --filter, includes =~ on --record-data-filter, and -f form_key=~DEMO exits 1."""
    for help_argv in (
        ["codings", "list", "--help"],
        ["visits", "list", "--help"],
    ):
        with pytest.raises(SystemExit) as help_exc:
            cli.app(help_argv)
        assert help_exc.value.code == 0
        help_out = capfd.readouterr().out
        assert "=~" not in help_out

    for rdf_help_argv in (
        ["records", "list", "--help"],
        ["export", "json", "--help"],
    ):
        with pytest.raises(SystemExit) as rdf_exc:
            cli.app(rdf_help_argv)
        assert rdf_exc.value.code == 0
        rdf_out = capfd.readouterr().out
        # Split at --record-data-filter to verify --filter section omits =~ while --record-data-filter includes =~
        before_rdf, after_rdf = rdf_out.split("--record-data-filter", 1)
        assert "=~" not in before_rdf
        assert "=~" in after_rdf

    monkeypatch.delenv("IMEDNET_API_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    with pytest.raises(SystemExit) as preflight_exc:
        cli.app(["records", "list", "STUDY", "-f", "form_key=~DEMO"])
    assert preflight_exc.value.code == 1
    err = capfd.readouterr().err
    assert "Allowed operators: ['!=', '<', '<=', '==', '>', '>=']" in err


def test_simulation_mode_codings_list_preserves_empty_quoted_string_and_returns_200(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Acceptance Criterion 4: `codings list STUDY -f 'reason==\"\"'` sends `filter=reason==\"\"` and gets HTTP 200 in simulation mode."""
    from imednet.simulation import SimulationTransport, reset_default_simulation_state

    reset_default_simulation_state()
    monkeypatch.setenv("IMEDNET_SIMULATION_MODE", "true")
    monkeypatch.delenv("IMEDNET_API_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)

    seen_requests: list[tuple[str, int]] = []
    orig_handle = SimulationTransport.handle_request

    def _spy_handle(self: SimulationTransport, request: httpx.Request) -> httpx.Response:
        response = orig_handle(self, request)
        seen_requests.append((request.url.params.get("filter", ""), response.status_code))
        return response

    with patch.object(SimulationTransport, "handle_request", _spy_handle):
        cli.app(["codings", "list", "STUDY", "-f", 'reason==""', "--format", "json"])

    captured = capfd.readouterr()
    envelope = json.loads(captured.out)
    assert envelope["schemaVersion"] == "1.0"
    assert envelope["resource"] == "codings"
    assert seen_requests == [('reason==""', 200)]


@pytest.mark.parametrize(
    "argv",
    [
        ["records", "list", "STUDY", "-f", "record_id>=1", "-f", "recordId<=10"],
        ["records", "list", "STUDY", "-f", "recordId>=1;recordId<=10"],
    ],
)
def test_records_list_rejects_duplicate_and_attributes_in_preflight(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Acceptance Criterion 5: duplicate normalized AND attributes exit 1 during preflight."""
    monkeypatch.delenv("IMEDNET_API_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    mock_get_sdk = MagicMock(side_effect=AssertionError("get_sdk must not be called in preflight"))
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", mock_get_sdk)

    with pytest.raises(SystemExit) as exc_info:
        cli.app(argv)

    assert exc_info.value.code == 1
    mock_get_sdk.assert_not_called()
    captured = capfd.readouterr()
    assert captured.out == ""
    assert "Duplicate filter attribute 'recordId'" in captured.err
