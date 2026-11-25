"""Tests for unified CLI read guards in the shared SPI pipeline."""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
from collections import UserDict
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import respx
from pydantic import BaseModel, ConfigDict

from imednet import cli
from imednet.errors import (
    ApiError,
    AuthenticationError,
    CircuitBreakerError,
    DiagnosticContext,
    ForbiddenError,
    RateLimitError,
    RequestError,
)
from imednet.integrations.enrichment import EnrichmentPipeline, StudyConfiguration
from imednet.models.records import Record
from imednet.sdk import ImednetSDK
from imednet.simulation import SimulationState, create_simulation_transport
from imednet.spi import cli as spi_cli
from imednet.utils.filters import (
    parse_filter_expression,
    validate_filter_operator,
    validate_filter_syntax,
    validate_record_filter,
)
from imednet.utils.record_filter_eval import compile_model_predicate
from imednet.utils.security import global_sensitivity_registry


def _invoke_cli(
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> tuple[int, str, str]:
    """Run ``cli.app(argv)`` capturing ``(exit_code, stdout, stderr)`` cleanly under ``log_cli``."""
    capsys.readouterr()
    code = 0
    try:
        cli.app(argv)
    except SystemExit as exc:
        code = int(exc.code) if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_spi_cli_exports_shared_read_guards_from_isolated_module() -> None:
    """Req 1, 8, 11: spi.cli and cli.utils export shared read guards isolated in _read_guards."""
    from imednet.cli import _read_guards
    from imednet.cli import utils as cli_utils
    from imednet.cli.utils import export as cli_export

    for symbol in (
        "bootstrap_cli_dotenv",
        "format_cli_diagnostic_error",
        "is_cli_phi_key_sensitive",
        "sanitize_cli_phi",
        "sanitize_cli_records",
    ):
        assert symbol in spi_cli.__all__
        assert callable(getattr(spi_cli, symbol))

    assert spi_cli.is_cli_phi_key_sensitive is _read_guards.is_cli_phi_key_sensitive
    assert spi_cli.sanitize_cli_phi is _read_guards.sanitize_cli_phi
    assert spi_cli.sanitize_cli_records is _read_guards.sanitize_cli_records
    assert spi_cli.format_cli_diagnostic_error is _read_guards.format_cli_diagnostic_error
    assert cli_utils.sanitize_cli_phi is cli_export.sanitize_cli_phi
    assert cli_utils.sanitize_cli_records is cli_export.sanitize_cli_records


def test_cli_help_does_not_import_integrations_at_startup() -> None:
    """Maintainability guardrail: _read_guards keeps imednet.integrations lazy on --help."""
    probe = (
        "import sys\n"
        "from imednet import cli\n"
        "try:\n"
        "    cli.app(['--help'])\n"
        "except SystemExit:\n"
        "    pass\n"
        "loaded = [m for m in sys.modules if m.startswith('imednet.integrations')]\n"
        "assert not loaded, f'Unexpected integrations loaded at startup: {loaded}'\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_records_list_json_masks_default_and_study_phi_unless_opted_out(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC 1 & Reqs 1-3: records list --format json --simulation masks PHI unless --no-mask-phi."""
    state = SimulationState()
    state.records.append(
        {
            "recordId": 9901,
            "recordOid": "REC-9901",
            "studyKey": "DEMO-STUDY",
            "subjectKey": "SUBJ-001",
            "subjectId": 202,
            "siteId": 102,
            "formId": 301,
            "formKey": "F_DM",
            "visitId": 401,
            "intervalId": 501,
            "recordType": "Standard",
            "recordStatus": "Complete",
            "recordData": {
                "SSN": "123-45-6789",
                "DOB": "1981-05-14",
                "patientName": "Jane Patient",
                "BRTHDTC": "1981-05-14",
                "customChartId": "CHART-7788",
                "V_AGE": 45,
            },
            "deleted": False,
        }
    )

    def _make_sim_sdk() -> ImednetSDK:
        return ImednetSDK(
            transport=create_simulation_transport(state),
            simulation_mode=True,
        )

    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", _make_sim_sdk)
    pipeline = EnrichmentPipeline(
        StudyConfiguration(study_key="DEMO-STUDY", phi_fields=["customChartId"])
    )
    monkeypatch.setattr(
        "imednet.integrations.enrichment.resolve_enrichment_pipeline",
        lambda sk: pipeline if sk == "DEMO-STUDY" else None,
    )

    code_masked, out_masked, err_masked = _invoke_cli(
        capsys,
        ["records", "list", "DEMO-STUDY", "--format", "json", "--simulation"],
    )
    assert code_masked == 0, err_masked
    payload_masked = json.loads(out_masked)
    rec_masked = next(r for r in payload_masked["items"] if r["recordId"] == 9901)
    assert rec_masked["subjectKey"] == "SUBJ-001"
    assert rec_masked["studyKey"] == "DEMO-STUDY"
    rd_masked = rec_masked["recordData"]
    assert rd_masked["SSN"] == "***MASKED***"
    assert rd_masked["DOB"] == "***MASKED***"
    assert rd_masked["patientName"] == "***MASKED***"
    assert rd_masked["BRTHDTC"] == "***MASKED***"
    assert rd_masked["customChartId"] == "***MASKED***"
    assert rd_masked["V_AGE"] == 45
    assert "123-45-6789" not in out_masked
    assert "Jane Patient" not in out_masked
    assert "CHART-7788" not in out_masked

    code_raw, out_raw, err_raw = _invoke_cli(
        capsys,
        [
            "records",
            "list",
            "DEMO-STUDY",
            "--format",
            "json",
            "--no-mask-phi",
            "--simulation",
        ],
    )
    assert code_raw == 0, err_raw
    payload_raw = json.loads(out_raw)
    rec_raw = next(r for r in payload_raw["items"] if r["recordId"] == 9901)
    rd_raw = rec_raw["recordData"]
    assert rd_raw["SSN"] == "123-45-6789"
    assert rd_raw["DOB"] == "1981-05-14"
    assert rd_raw["patientName"] == "Jane Patient"
    assert rd_raw["BRTHDTC"] == "1981-05-14"
    assert rd_raw["customChartId"] == "CHART-7788"


def test_records_list_interactive_grid_and_load_all_mask_phi(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC 2 & Req 2: InteractiveDataGrid side panels and [a] load-more mask Record PHI."""
    from imednet.cli.utils.grid import InteractiveDataGrid

    page_items = [
        Record(
            record_id=101,
            study_key="DEMO-STUDY",
            subject_key="SUBJ-001",
            form_key="F_DM",
            record_status="Complete",
            record_data={
                "SSN": "111-11-1111",
                "patientName": "Alice Secret",
                "customChartId": "CH-1",
                "V_AGE": 31,
            },
        ),
        Record(
            record_id=102,
            study_key="DEMO-STUDY",
            subject_key="SUBJ-002",
            form_key="F_DM",
            record_status="Complete",
            record_data={
                "DOB": "1979-02-02",
                "BRTHDTC": "1979-02-02",
                "customChartId": "CH-2",
                "V_AGE": 47,
            },
        ),
    ]

    sdk = MagicMock()
    sdk.records.list.return_value = iter(page_items)
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda: sdk)

    pipeline = EnrichmentPipeline(
        StudyConfiguration(study_key="DEMO-STUDY", phi_fields=["customChartId"])
    )
    monkeypatch.setattr(
        "imednet.integrations.enrichment.resolve_enrichment_pipeline",
        lambda sk: pipeline if sk == "DEMO-STUDY" else None,
    )

    captured_grid: dict[str, Any] = {}

    def _capture_display_list(
        items: list[dict[str, Any]],
        label: str,
        empty_msg: str | None = None,
        **kwargs: Any,
    ) -> None:
        captured_grid["items"] = items
        captured_grid["details"] = kwargs.get("details")
        captured_grid["on_load_all"] = kwargs.get("on_load_all")

    monkeypatch.setattr("imednet.cli.records.display_list", _capture_display_list)

    code, _, err = _invoke_cli(capsys, ["records", "list", "DEMO-STUDY", "--limit", "1"])
    assert code == 0, err
    assert len(captured_grid["details"]) == 1

    stdscr = MagicMock()
    stdscr.getmaxyx.return_value = (30, 120)
    monkeypatch.setattr(InteractiveDataGrid, "_setup_curses", lambda self: None)

    grid = InteractiveDataGrid(
        stdscr=stdscr,
        items=captured_grid["items"],
        fields=["record_id", "subject_key", "form_key", "status"],
        label="records",
        details=captured_grid["details"],
        truncated=True,
        on_load_all=captured_grid["on_load_all"],
    )
    initial_lines_text = "\n".join(text for text, _ in grid._build_side_panel_lines(60))
    assert "***MASKED***" in initial_lines_text
    assert "111-11-1111" not in initial_lines_text
    assert "Alice Secret" not in initial_lines_text
    assert "CH-1" not in initial_lines_text

    # Simulate pressing 'a' to load remaining records
    assert callable(grid.on_load_all)
    more_rows, more_details = grid.on_load_all()
    grid.all_entries.extend(zip(more_rows, more_details, strict=False))
    grid.filtered_entries = list(grid.all_entries)
    grid.selected_idx = 1

    second_lines_text = "\n".join(text for text, _ in grid._build_side_panel_lines(60))
    assert "***MASKED***" in second_lines_text
    assert "1979-02-02" not in second_lines_text
    assert "CH-2" not in second_lines_text


def test_encrypted_config_version_store_sdk_governance_masks_records_and_cli_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Upstream #2119 alignment: encrypted ConfigVersionStore phiFields resolve via SDK governance in CLI reads and errors without mutating global_sensitivity_registry."""
    from imednet.core.context import sdk_governance_context
    from imednet_workflows.config_version_control import ConfigVersionStore

    db_path = tmp_path / "gov_store" / "config_versions.sqlite3"
    monkeypatch.setenv("IMEDNET_CONFIG_DB_PATH", str(db_path))
    monkeypatch.delenv("IMEDNET_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)

    state = SimulationState()
    state.records.append(
        {
            "recordId": 7701,
            "studyKey": "DEMO-STUDY",
            "subjectKey": "SUBJ-001",
            "formKey": "F_DM",
            "recordStatus": "Complete",
            "recordData": {
                "customChartId": "ENCRYPTED-CHART-7701",
                "V_AGE": 38,
            },
        }
    )

    scoped_base_url = "https://private-gov.imednet.test"

    def _make_scoped_sdk() -> ImednetSDK:
        return ImednetSDK(
            api_key="gov-api-key",  # pragma: allowlist secret
            security_key="gov-only-sdk-secret-key",  # pragma: allowlist secret
            base_url=scoped_base_url,
            transport=create_simulation_transport(state),
        )

    seed_sdk = _make_scoped_sdk()
    try:
        with sdk_governance_context(seed_sdk):
            store = ConfigVersionStore()
            store.commit_config(
                "DEMO-STUDY",
                StudyConfiguration(
                    study_key="DEMO-STUDY",
                    version="1.0",
                    phi_fields=["customChartId"],
                ),
                "Commit encrypted customChartId PHI rule",
                sdk=seed_sdk,
            )
    finally:
        seed_sdk.close()

    sens_before, exempt_before = global_sensitivity_registry.snapshot()

    # 1) Direct sanitize_cli_records(..., sdk=...) resolves encrypted store without env security key
    direct_sdk = _make_scoped_sdk()
    try:
        raw_record = Record(
            record_id=7702,
            study_key="DEMO-STUDY",
            subject_key="SUBJ-001",
            record_data={"customChartId": "DIRECT-CHART-7702", "V_AGE": 29},
        )
        masked_records = spi_cli.sanitize_cli_records(
            [raw_record],
            study_key="DEMO-STUDY",
            mask_phi=True,
            sdk=direct_sdk,
        )
        assert masked_records[0].record_data == {
            "customChartId": "***MASKED***",
            "V_AGE": 29,
        }
    finally:
        direct_sdk.close()

    # 2) records list --format json resolves encrypted store through with_sdk governance context
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", _make_scoped_sdk)
    code, out, err = _invoke_cli(capsys, ["records", "list", "DEMO-STUDY", "--format", "json"])
    assert code == 0, err
    rec_7701 = next(r for r in json.loads(out)["items"] if r["recordId"] == 7701)
    assert rec_7701["recordData"]["customChartId"] == "***MASKED***"
    assert rec_7701["recordData"]["V_AGE"] == 38
    assert "ENCRYPTED-CHART-7701" not in out

    # 3) Structured ApiError field + URL query + message custom phiFields masked in with_sdk
    def _make_failing_scoped_sdk() -> ImednetSDK:
        sdk_inst = _make_scoped_sdk()
        sdk_inst.records.list = MagicMock(  # type: ignore[method-assign]
            side_effect=ApiError(
                {
                    "metadata": {"status": "BAD_REQUEST"},
                    "errors": [
                        {
                            "code": "400",
                            "description": "Rejected customChartId=INLINE-CHART-11 for SUBJ-001",
                            "field": {"variable": "customChartId", "value": "FIELD-CHART-22"},
                        }
                    ],
                },
                status_code=400,
                diagnostic_context=DiagnosticContext(
                    method="GET",
                    url=(
                        f"{scoped_base_url}/api/v1/edc/studies/DEMO-STUDY/records"
                        "?filter=customChartId%3D%3DURL-CHART-33%3Bsubject_key%3D%3DSUBJ-001"
                    ),
                ),
            )
        )
        return sdk_inst

    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", _make_failing_scoped_sdk)
    err_code, err_out, err_stderr = _invoke_cli(
        capsys,
        ["records", "list", "DEMO-STUDY", "--format", "json"],
    )
    assert err_code == 1
    assert err_out == ""
    assert "field customChartId='***MASKED***'" in err_stderr
    assert "INLINE-CHART-11" not in err_stderr
    assert "FIELD-CHART-22" not in err_stderr
    assert "URL-CHART-33" not in err_stderr
    assert "SUBJ-001" in err_stderr

    # Global sensitivity registry must remain completely untouched
    assert global_sensitivity_registry.snapshot() == (sens_before, exempt_before)
    assert not global_sensitivity_registry.is_sensitive("customChartId")


def test_workflow_uat_issue_and_failure_masking_aligns_with_shared_spi_read_guards(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Upstream #2128 alignment: camelCase custom phiFields mask UAT quotes while exempt keys stay intact."""
    from imednet_workflows import cli as wf_cli

    assert spi_cli.is_cli_phi_key_sensitive("customChartId", custom_phi_fields={"customChartId"})
    assert spi_cli.is_cli_phi_key_sensitive("custom_chart_id", custom_phi_fields={"customChartId"})
    assert not spi_cli.is_cli_phi_key_sensitive(
        "subjectKey", custom_phi_fields={"subjectKey", "customChartId"}
    )

    masked_issue = wf_cli._sanitize_phi_issue_message(
        "Invalid value 'CHART-SECRET-99' for variable 'customChartId' on subject 'SUBJ-001'",
        variable_name="customChartId",
        custom_phi_fields={"customChartId"},
    )
    assert "CHART-SECRET-99" not in masked_issue
    assert "'***MASKED***'" in masked_issue
    assert "'customChartId'" in masked_issue

    exempt_issue = wf_cli._sanitize_phi_issue_message(
        "Subject 'SUBJ-001' failed lookup for variable 'subjectKey'",
        variable_name="subjectKey",
        custom_phi_fields={"subjectKey", "customChartId"},
    )
    assert "'SUBJ-001'" in exempt_issue

    monkeypatch.setattr(
        wf_cli,
        "_resolve_custom_phi_fields",
        lambda sk: {"customChartId"} if sk == "DEMO-STUDY" else set(),
    )
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc_info:
        wf_cli._emit_uat_json_failure(
            "[variable=customChartId] Value 'CHART-FAIL-88' rejected",
            study_key="DEMO-STUDY",
            dry_run=True,
            failure_phase="VALIDATION",
        )
    assert exc_info.value.code == 1
    uat_out = json.loads(capsys.readouterr().out)
    assert uat_out["status"] == "failed"
    assert uat_out["items"][0]["studyKey"] == "DEMO-STUDY"
    assert "CHART-FAIL-88" not in uat_out["items"][0]["errorMessage"]
    assert "'***MASKED***'" in uat_out["items"][0]["errorMessage"]


def test_sanitize_cli_phi_handles_models_extras_mappings_tuples_and_identity() -> None:
    """Req 1 & 3: sanitize_cli_phi preserves identity when clean and masks nested containers."""

    class _ExtraModel(BaseModel):
        model_config = ConfigDict(extra="allow", populate_by_name=True)
        subject_key: str = ""
        patient_name: str | None = None

    clean_rec = Record(record_id=1, study_key="S1", subject_key="SUB1", record_data={"V_AGE": 22})
    assert spi_cli.sanitize_cli_phi(clean_rec, mask_phi=True) is clean_rec

    extra_item = _ExtraModel(
        subject_key="SUB1",
        patient_name="Bob Hidden",
        custom_note="SECRET-CHART",
    )
    payload = UserDict(
        {
            "study_key": "S1",
            "nested_tuple": (extra_item, {"SSN": "999-00-1111", "custom_chart_id": "CH-9"}),
            "clean_list": [1, 2],
        }
    )
    masked = spi_cli.sanitize_cli_phi(
        payload,
        study_key="S1",
        mask_phi=True,
        custom_phi_fields={"customNote", "customChartId"},
    )
    assert masked["study_key"] == "S1"
    masked_model, masked_inner = masked["nested_tuple"]
    assert masked_model.subject_key == "SUB1"
    assert masked_model.patient_name == "***MASKED***"
    assert masked_model.__pydantic_extra__["custom_note"] == "***MASKED***"
    assert masked_inner["SSN"] == "***MASKED***"
    assert masked_inner["custom_chart_id"] == "***MASKED***"
    assert masked["clean_list"] is payload["clean_list"]


@pytest.mark.parametrize(
    "bad_clause",
    [
        "subject_key=@SUB",
        "subject_key=^SUB",
        "subject_key=$SUB",
        "subject_key!@SUB",
        "subject_key!^SUB",
        "subject_key!$SUB",
    ],
)
def test_unsupported_pattern_operators_exit_one_with_common_api_guidance(
    bad_clause: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC 3 & Reqs 4-5: =@, =^, =$, !@, !^, !$ exit 1 citing allowed operators and =~."""
    not_called = MagicMock(side_effect=AssertionError("get_sdk should not run on bad operator"))
    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", not_called)

    code, out, err = _invoke_cli(capsys, ["records", "list", "STUDY", "--filter", bad_clause])
    assert code == 1
    assert out == ""
    for token in ("'<'", "'<='", "'>'", "'>='", "'=='", "'!='", "'=~'"):
        assert token in err
    assert "--record-data-filter" in err
    not_called.assert_not_called()

    op = bad_clause.split("subject_key", 1)[1][:2]
    with pytest.raises(ValueError, match="records list --record-data-filter"):
        validate_filter_operator(op, allow_regex=True)
    with pytest.raises(ValueError, match="records list --record-data-filter"):
        validate_filter_syntax(bad_clause)
    with pytest.raises(ValueError, match="records list --record-data-filter"):
        validate_record_filter(bad_clause)


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ('subject_key="@SUB"', ("subject_key", "@SUB")),
        ('subject_key="^SUB"', ("subject_key", "^SUB")),
        ('subject_key="$SUB"', ("subject_key", "$SUB")),
        ('subject_key=="@SUB"', ("subject_key", ("==", "@SUB"))),
        ('subject_key!="@SUB"', ("subject_key", ("!=", "@SUB"))),
    ],
)
def test_double_quoted_at_caret_dollar_literals_are_preserved(
    expr: str,
    expected: tuple[str, Any],
) -> None:
    """Req 5: Double-quoted '@...', '^...', and '$...' filter values remain literal strings."""
    assert parse_filter_expression(expr) == expected


@respx.mock
def test_intervals_list_simulation_and_upstream_client_side_filtering(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC 4, Req 6-7 & Guardrail 3: intervals list evaluates --filter locally without upstream filter param."""
    code, out, err = _invoke_cli(
        capsys,
        [
            "intervals",
            "list",
            "STUDY",
            "--filter",
            "interval_name=Baseline",
            "--format",
            "json",
            "--simulation",
        ],
    )
    assert code == 0, err
    sim_payload = json.loads(out)
    assert sim_payload["count"] == 1
    assert sim_payload["items"][0]["intervalName"] == "Baseline"

    # Verify upstream HTTP never receives `filter` and --limit applies AFTER client-side filtering
    monkeypatch.setattr(
        "imednet.cli.utils.context.get_sdk",
        lambda: ImednetSDK(
            api_key="k",  # pragma: allowlist secret
            security_key="s",  # pragma: allowlist secret
            base_url="https://api.test",
        ),
    )
    route = respx.get("https://api.test/api/v1/edc/studies/STUDY/intervals").mock(
        return_value=httpx.Response(
            200,
            json={
                "metadata": {"status": "OK"},
                "pagination": {
                    "currentPage": 0,
                    "size": 100,
                    "totalPages": 1,
                    "totalElements": 3,
                },
                "data": [
                    {"intervalId": 10, "intervalName": "Screening", "disabled": False},
                    {"intervalId": 20, "intervalName": "Baseline", "disabled": False},
                    {"intervalId": 30, "intervalName": "Baseline", "disabled": True},
                ],
            },
        )
    )

    code_http, out_http, err_http = _invoke_cli(
        capsys,
        [
            "intervals",
            "list",
            "STUDY",
            "--filter",
            "interval_name=Baseline;disabled==false",
            "--limit",
            "1",
            "--format",
            "json",
        ],
    )
    assert code_http == 0, err_http
    http_payload = json.loads(out_http)
    assert http_payload["count"] == 1
    assert http_payload["items"][0]["intervalId"] == 20
    sent_params = dict(route.calls[0].request.url.params)
    assert "filter" not in sent_params
    assert "intervalName" not in sent_params

    # Preflight failure before credential checks on intervals and users
    monkeypatch.delenv("IMEDNET_API_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_OIDC_TOKEN", raising=False)
    monkeypatch.delenv("IMEDNET_SIMULATION_MODE", raising=False)

    for cmd in (
        ["intervals", "list", "STUDY", "--limit", "0"],
        ["users", "list", "STUDY", "--limit", "0"],
        ["intervals", "list", "STUDY", "--filter", "unknown_field=1"],
    ):
        c, _, e = _invoke_cli(capsys, cmd)
        assert c == 1
        assert "IMEDNET_API_KEY" not in e


def test_compile_model_predicate_nested_and_or_semantics() -> None:
    """Req 7: compile_model_predicate supports mappings, dotted paths, OR filters, and empty lists."""
    pred_or = compile_model_predicate({"filter": "intervalName==Baseline,intervalId>=500"})
    assert pred_or.matches({"intervalId": 100, "intervalName": "Baseline"}) is True
    assert pred_or.matches({"intervalId": 501, "intervalName": "Other"}) is True
    assert pred_or.matches({"intervalId": 100, "intervalName": "Other"}) is False

    pred_empty = compile_model_predicate({"interval_id": []})
    assert pred_empty.matches({"intervalId": 1}) is False


def test_cwd_dotenv_bootstrap_preserves_shell_vars_and_applies_study_key_and_high_contrast(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC 5 & Reqs 8, 9, 10: cwd ./.env loads with override=False, IMEDNET_STUDY_KEY, and parse_bool."""
    from imednet.cli.utils.grid import InteractiveDataGrid
    from imednet_tui.app import ImednetTuiApp

    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text(
        "IMEDNET_API_KEY=dotenv_api_key\n"  # pragma: allowlist secret
        "IMEDNET_SECURITY_KEY=dotenv_sec_key\n"  # pragma: allowlist secret
        "IMEDNET_STUDY_KEY=DEMO-STUDY\n"
        "IMEDNET_HIGH_CONTRAST=true\n"
        "IMEDNET_SIMULATION_MODE=yes\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IMEDNET_API_KEY", "shell_api_key_wins")  # pragma: allowlist secret
    monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_STUDY_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_HIGH_CONTRAST", raising=False)
    monkeypatch.delenv("IMEDNET_SIMULATION_MODE", raising=False)

    observed_env: dict[str, str | None] = {}
    orig_get_sdk = cli.utils.context.get_sdk

    def _spy_get_sdk() -> ImednetSDK:
        observed_env["api_key"] = os.environ.get("IMEDNET_API_KEY")
        observed_env["sec_key"] = os.environ.get("IMEDNET_SECURITY_KEY")
        observed_env["hc"] = os.environ.get("IMEDNET_HIGH_CONTRAST")
        observed_env["study"] = os.environ.get("IMEDNET_STUDY_KEY")
        return orig_get_sdk()

    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", _spy_get_sdk)

    # Omit study_key positional/flag: IMEDNET_STUDY_KEY=DEMO-STUDY from ./.env resolves it!
    code, out, err = _invoke_cli(capsys, ["sites", "list", "--format", "json"])
    assert code == 0, err
    sites_payload = json.loads(out)
    assert sites_payload["items"][0]["studyKey"] == "DEMO-STUDY"
    assert observed_env["api_key"] == "shell_api_key_wins"  # pragma: allowlist secret
    assert observed_env["sec_key"] == "dotenv_sec_key"  # pragma: allowlist secret
    assert observed_env["hc"] == "true"

    # Also verify subject-data hybrid resolver falls back to IMEDNET_STUDY_KEY
    code_sd, out_sd, err_sd = _invoke_cli(capsys, ["subject-data", "SUBJ-001", "--format", "json"])
    assert code_sd == 0, err_sd
    sd_payload = json.loads(out_sd)
    sd_item = sd_payload["items"][0]
    subj_details = sd_item.get("subjectDetails") or sd_item.get("subject_details") or {}
    assert (subj_details.get("subjectKey") or subj_details.get("subject_key")) == "SUBJ-001"

    # Verify parse_bool across "1", "true", "yes" for output.py, grid.py, terminal.py, and TUI
    stdscr = MagicMock()
    stdscr.getmaxyx.return_value = (24, 80)
    monkeypatch.setattr(InteractiveDataGrid, "_setup_curses", lambda self: None)

    for truthy in ("1", "true", "yes", "TRUE", "Yes"):
        monkeypatch.setenv("IMEDNET_HIGH_CONTRAST", truthy)
        monkeypatch.setenv("IMEDNET_NON_INTERACTIVE", truthy)
        assert spi_cli.is_non_interactive() is True

        grid = InteractiveDataGrid(
            stdscr=stdscr,
            items=[{"status": "Complete"}],
            fields=["status"],
            label="records",
        )
        assert grid.is_high_contrast is True

        tui_app = ImednetTuiApp(MagicMock())
        assert tui_app.high_contrast_enabled is True

    # Verify high-contrast ANSI table rendering for IMEDNET_HIGH_CONTRAST=yes
    monkeypatch.setenv("IMEDNET_HIGH_CONTRAST", "yes")
    monkeypatch.setenv("IMEDNET_NON_INTERACTIVE", "0")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr("imednet.cli.utils.output.supports_ansi_color", lambda *a, **k: True)
    table_buf = io.StringIO()
    with redirect_stdout(table_buf):
        spi_cli.display_list(
            [{"job_id": "J1", "state": "COMPLETED"}],
            "jobs",
            fields=["job_id", "state"],
            interactive=False,
        )
    assert "\033[1;38;2;0;255;102;48;2;0;0;0m" in table_buf.getvalue()


def test_resolve_study_key_and_hybrid_study_key_env_and_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Req 10 & Guardrail 4: IMEDNET_STUDY_KEY fallback and exit code 2 on conflicts."""
    parser = argparse.ArgumentParser(prog="test")
    spi_cli.add_study_key_args(parser)
    parser.add_argument("subject_key", nargs="?", default=None)
    parser.add_argument("--subject-key", dest="subject_key_flag", default=None)

    monkeypatch.setenv("IMEDNET_STUDY_KEY", "ENV-STUDY")

    ns_single = parser.parse_args(["SUBJ-9"])
    assert spi_cli.resolve_hybrid_study_and_key(parser, ns_single) == ("ENV-STUDY", "SUBJ-9")

    ns_flag_subj = parser.parse_args(["--subject-key", "SUBJ-9"])
    assert spi_cli.resolve_hybrid_study_and_key(parser, ns_flag_subj) == ("ENV-STUDY", "SUBJ-9")

    ns_explicit = parser.parse_args(["POS-STUDY", "SUBJ-9"])
    assert spi_cli.resolve_hybrid_study_and_key(parser, ns_explicit) == ("POS-STUDY", "SUBJ-9")

    ns_conflict = parser.parse_args(["POS-STUDY", "--study-key", "FLAG-STUDY"])
    with pytest.raises(SystemExit) as exc_info:
        spi_cli.resolve_study_key(parser, ns_conflict)
    assert exc_info.value.code == 2


def test_tui_cli_bootstraps_cwd_dotenv_at_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Req 8 & 9: imednet tui loads ./.env early before reading IMEDNET_STUDY_KEY and contrast."""
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text(
        "IMEDNET_STUDY_KEY=TUI-ENV-STUDY\n"
        "IMEDNET_HIGH_CONTRAST=yes\n"
        "IMEDNET_SIMULATION_MODE=true\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("IMEDNET_STUDY_KEY", raising=False)
    monkeypatch.delenv("IMEDNET_HIGH_CONTRAST", raising=False)
    monkeypatch.delenv("IMEDNET_SIMULATION_MODE", raising=False)
    monkeypatch.delenv("IMEDNET_NON_INTERACTIVE", raising=False)

    launched: dict[str, Any] = {}

    def _fake_run_tui(sdk: Any, **kwargs: Any) -> None:
        launched["study_key"] = kwargs.get("study_key")
        launched["hc_env"] = os.environ.get("IMEDNET_HIGH_CONTRAST")
        launched["sim"] = sdk.config.simulation_mode

    monkeypatch.setattr("imednet_tui.app.run_tui", _fake_run_tui)

    code, _, err = _invoke_cli(capsys, ["tui", "--force"])
    assert code == 0, err
    assert launched["study_key"] == "TUI-ENV-STUDY"
    assert launched["hc_env"] == "yes"
    assert launched["sim"] is True


def test_cli_error_presenter_simulation_job_and_structured_api_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC 6, Req 11-12 & Guardrails 1, 5: plain-language CLI errors without raw dicts or prod URLs."""
    monkeypatch.setenv("IMEDNET_NON_INTERACTIVE", "1")
    code, out, err = _invoke_cli(
        capsys,
        ["jobs", "status", "STUDY", "UNKNOWN-JOB", "--simulation", "--format", "json"],
    )
    assert code == 1
    assert out == ""
    err_lines = [ln for ln in err.strip().splitlines() if ln.strip()]
    assert len(err_lines) == 1
    line = err_lines[0]
    assert line.startswith("Not Found:")
    assert "Response:" not in line
    assert "{'metadata'" not in line
    assert "edc.prod.imednetapi.com" not in line
    assert "simulation GET /api/v1/edc/studies/STUDY/jobs/UNKNOWN-JOB" in line

    # Programmatic ApiError.__str__ and ApiError.response remain backwards-compatible
    secret_tok = "super_secret_api_token_999"  # pragma: allowlist secret
    monkeypatch.setenv("IMEDNET_API_KEY", secret_tok)
    api_err = ApiError(
        {
            "metadata": {"status": "BAD_REQUEST"},
            "errors": [
                {
                    "code": "400",
                    "description": f"Invalid variable payload for {secret_tok}",
                    "field": {"variable": "V_AGE", "value": "bad_int"},
                }
            ],
        },
        status_code=400,
        diagnostic_context=DiagnosticContext(
            method="POST",
            url="https://edc.prod.imednetapi.com/api/v1/edc/studies/STUDY/records",
            retry_after=2.5,
        ),
    )
    assert isinstance(api_err.response, dict)
    assert "Response:" in str(api_err)

    cli_msg = spi_cli.format_cli_diagnostic_error(api_err, simulation=False)
    assert "Response:" not in cli_msg
    assert "{'metadata'" not in cli_msg
    assert "HTTP 400" in cli_msg
    assert "field V_AGE='bad_int'" in cli_msg
    assert "POST https://edc.prod.imednetapi.com/api/v1/edc/studies/STUDY/records" in cli_msg
    assert "retry after 2.5s" in cli_msg
    assert secret_tok not in cli_msg
    assert "********" in cli_msg

    # Verify Auth, Forbidden, RateLimit, RequestError, and CircuitBreakerError through with_sdk
    cases: list[tuple[BaseException, str]] = [
        (AuthenticationError("Bad credentials", status_code=401), "Authentication Failed:"),
        (ForbiddenError("Study access denied", status_code=403), "Permission Denied:"),
        (
            RateLimitError(
                "Too many calls",
                status_code=429,
                diagnostic_context=DiagnosticContext(retry_after=5),
            ),
            "Rate Limit Exceeded:",
        ),
        (
            RequestError(
                f"Connection reset using {secret_tok}",
                diagnostic_context=DiagnosticContext(
                    method="GET",
                    url="https://edc.prod.imednetapi.com/api/v1/edc/studies",
                    attempt_count=3,
                    cause_type="ConnectError",
                ),
            ),
            "Network Error:",
        ),
        (
            CircuitBreakerError(f"Circuit open for {secret_tok}"),
            "Circuit Breaker Open:",
        ),
    ]
    for exc_obj, expected_prefix in cases:
        failing_sdk = MagicMock()
        failing_sdk.config.simulation_mode = False
        failing_sdk.studies.list.side_effect = exc_obj
        monkeypatch.setattr("imednet.cli.utils.context.get_sdk", lambda s=failing_sdk: s)
        c, o, e = _invoke_cli(capsys, ["studies", "list", "--format", "json"])
        assert c == 1
        assert o == ""
        lines = [ln for ln in e.strip().splitlines() if ln.strip()]
        assert len(lines) == 1
        assert lines[0].startswith(expected_prefix)
        assert secret_tok not in lines[0]


def test_network_retry_and_circuit_breaker_emit_single_stderr_line_in_subprocess() -> None:
    """AC 6 & Req 12: NullHandler suppresses logging.lastResort duplicate stderr lines."""
    retry_script = (
        "import httpx\n"
        "from imednet import cli\n"
        "from imednet.config import Config\n"
        "from imednet.sdk import ImednetSDK\n"
        "import imednet.cli.utils.context as ctx\n"
        "def _fail_transport(req):\n"
        "    raise httpx.ConnectError('connection refused', request=req)\n"
        "def _get_sdk():\n"
        "    cfg = Config(api_key='k', security_key='s')\n"  # pragma: allowlist secret
        "    return ImednetSDK(config=cfg, transport=httpx.MockTransport(_fail_transport), retries=2, backoff_factor=0.001)\n"
        "ctx.get_sdk = _get_sdk\n"
        "cli.app(['studies', 'list', '--format', 'json'])\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", retry_script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert proc.stdout == ""
    stderr_lines = [ln for ln in proc.stderr.strip().splitlines() if ln.strip()]
    assert len(stderr_lines) == 1, f"Expected 1 stderr line, got {stderr_lines!r}"
    assert stderr_lines[0].startswith("Network Error:")
    assert "3 attempts" in stderr_lines[0]
    assert "ConnectError" in stderr_lines[0]

    cb_script = (
        "import httpx\n"
        "from imednet import cli\n"
        "from imednet.config import Config\n"
        "from imednet.core.operations.circuit_breaker import CircuitBreaker\n"
        "from imednet.sdk import ImednetSDK\n"
        "import imednet.cli.utils.context as ctx\n"
        "def _fail_503(req):\n"
        "    return httpx.Response(503, json={'message': 'down'}, request=req)\n"
        "cb = CircuitBreaker(failure_threshold=1, recovery_timeout=60.0)\n"
        "def _get_sdk():\n"
        "    cfg = Config(api_key='k', security_key='s')\n"  # pragma: allowlist secret
        "    sdk = ImednetSDK(config=cfg, transport=httpx.MockTransport(_fail_503), retries=0)\n"
        "    sdk._client._executor.circuit_breaker = cb\n"
        "    try:\n"
        "        list(sdk.studies.list())\n"
        "    except Exception:\n"
        "        pass\n"
        "    return sdk\n"
        "ctx.get_sdk = _get_sdk\n"
        "cli.app(['studies', 'list', '--format', 'json'])\n"
    )
    cb_proc = subprocess.run(
        [sys.executable, "-c", cb_script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert cb_proc.returncode == 1
    assert cb_proc.stdout == ""
    cb_lines = [ln for ln in cb_proc.stderr.strip().splitlines() if ln.strip()]
    assert len(cb_lines) == 1, f"Expected 1 stderr line, got {cb_lines!r}"
    assert cb_lines[0].startswith("Circuit Breaker Open:")
