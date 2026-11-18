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

    argv = [subcommand, "list"]
    if needs_study:
        argv.append("S1")
    argv.extend(
        [
            "-f",
            "dateCreated>=2025-01-01,dateModified>=2025-01-01",
            "--sort",
            "dateCreated,asc",
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
    assert params["filter"] == "dateCreated>=2025-01-01,dateModified>=2025-01-01"
    assert params["sort"] == "dateCreated,asc"
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
    assert "truncated" in captured_grid_kwargs["status_notice"].lower()
    assert "--all" in captured_grid_kwargs["status_notice"]

    # Also verify InteractiveDataGrid renders the status_notice in its header and status bars
    win = _CaptureWindow(height=20, width=140)
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
        )
        grid.draw()

    assert "truncated" in win.lines[0].lower()
    assert "truncated" in win.lines[19].lower()


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

    # Direct Paginator instantiation also caps page_size at 500 and rejects <= 0
    paginator = Paginator(MagicMock(), "/items", page_size=999)
    assert paginator.page_size == 500
    with pytest.raises(ValueError, match="positive integer"):
        Paginator(MagicMock(), "/items", page_size=0)


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
