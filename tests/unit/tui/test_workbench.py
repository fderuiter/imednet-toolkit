"""Comprehensive interactive workbench unit tests for imednet-tui."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from textual.widgets import Button, DataTable, Input, Static, TabbedContent

from imednet.models.codings import Coding
from imednet.models.forms import Form
from imednet.models.intervals import Interval
from imednet.models.jobs import Job, JobStatus
from imednet.models.queries import Query, QueryComment
from imednet.models.record_revisions import RecordRevision
from imednet.models.records import Record
from imednet.models.sites import Site
from imednet.models.studies import Study
from imednet.models.subjects import Subject
from imednet.models.triage import TriageItem, TriageStatus
from imednet.models.users import User
from imednet.models.variables import Variable
from imednet.models.visits import Visit
from imednet.spi.datadict import BusinessLogicRule, DataDictionary, Metadata
from imednet.spi.datadict import Form as DDForm
from imednet.spi.datadict import Variable as DDVariable
from imednet.spi.jobs import JobLedger
from imednet_tui.app import (
    DashboardScreen,
    ImednetTuiApp,
    JobMonitor,
    StudyList,
    SubjectTable,
)
from imednet_tui.triage_pane import TriagePane, _load_triage_store_class
from imednet_tui.views import DataDictionaryPane, EntityTablePane, _stringify_cell
from imednet_tui.workflows_pane import (
    CANONICAL_WORKFLOW_DESCRIPTORS,
    WorkflowsPane,
    _execute_workflow_sync,
    _extract_batch_ids,
    _parse_kv_params,
    _parse_optional_json,
    discover_workflow_plugins,
)
from imednet_workflows.triage_store import TriageStore


@pytest.fixture(autouse=True)
def isolated_workbench_stores(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Use hermetic SQLite paths for JobLedger and TriageStore in every TUI test."""
    monkeypatch.setenv("IMEDNET_JOB_LEDGER_PATH", str(tmp_path / "tui_jobs.db"))
    monkeypatch.setenv("IMEDNET_TRIAGE_DB_PATH", str(tmp_path / "tui_triage.db"))


def _build_clinical_sdk() -> MagicMock:
    """Construct a rich mock SDK populated with multi-subject clinical entities."""
    sdk = MagicMock()
    sdk.config.base_url = "https://edc.example.org"

    sdk.studies.list.return_value = [Study(studyKey="ONCO_01", studyName="Oncology Phase II")]
    del sdk.studies.async_list

    sdk.sites.list.return_value = [Site(studyKey="ONCO_01", siteId=10, siteName="Mayo Site")]
    del sdk.sites.async_list

    sdk.subjects.list.return_value = [
        Subject(
            studyKey="ONCO_01",
            subjectId=1001,
            subjectKey="SUBJ-ALPHA",
            siteId=10,
            subjectStatus="Enrolled",
        ),
        Subject(
            studyKey="ONCO_01",
            subjectId=1002,
            subjectKey="SUBJ-BETA",
            siteId=10,
            subjectStatus="Screened",
        ),
    ]
    del sdk.subjects.async_list
    del sdk.subjects.list_by_site

    sdk.records.list.return_value = [
        Record(
            studyKey="ONCO_01",
            recordId=1,
            subjectKey="SUBJ-ALPHA",
            formKey="DM",
            visitId=11,
            recordStatus="Complete",
            recordData={"patient_name": "Jane Doe", "AGE": 44},
        ),
        Record(
            studyKey="ONCO_01",
            recordId=2,
            subjectKey="SUBJ-BETA",
            formKey="VS",
            visitId=12,
            recordStatus="Incomplete",
            recordData={"SYSBP": 120},
        ),
    ]
    del sdk.records.async_list

    sdk.visits.list.return_value = [
        Visit(studyKey="ONCO_01", visitId=11, subjectKey="SUBJ-ALPHA", intervalName="Baseline"),
        Visit(studyKey="ONCO_01", visitId=12, subjectKey="SUBJ-BETA", intervalName="Week 4"),
    ]
    del sdk.visits.async_list

    sdk.queries.list.return_value = [
        Query(
            studyKey="ONCO_01",
            annotationId=71,
            subjectKey="SUBJ-ALPHA",
            variable="AGE",
            description="Confirm birth year",
            queryComments=[],
        ),
        Query(
            studyKey="ONCO_01",
            annotationId=72,
            subjectKey="SUBJ-BETA",
            variable="SYSBP",
            description="Out of range BP",
            queryComments=[],
        ),
    ]
    del sdk.queries.async_list

    sdk.forms.list.return_value = [
        Form(studyKey="ONCO_01", formId=1, formKey="DM", formName="Demographics", formType="CRF"),
        Form(studyKey="ONCO_01", formId=2, formKey="AE", formName="Adverse Events", formType="CRF"),
    ]
    del sdk.forms.async_list

    sdk.variables.list.return_value = [
        Variable(
            studyKey="ONCO_01",
            variableId=10,
            variableName="AGE",
            formKey="DM",
            variableType="integer",
            label="Subject Age",
        ),
        Variable(
            studyKey="ONCO_01",
            variableId=11,
            variableName="AETERM",
            formKey="AE",
            variableType="text",
            label="Adverse Event Term",
        ),
    ]
    del sdk.variables.async_list

    sdk.record_revisions.list.return_value = [
        RecordRevision(
            studyKey="ONCO_01",
            recordRevisionId=500,
            recordId=1,
            subjectKey="SUBJ-ALPHA",
            recordStatus="Complete",
            user="pi_smith",
            reasonForChange="Verified source document",
        ),
        RecordRevision(
            studyKey="ONCO_01",
            recordRevisionId=501,
            recordId=2,
            subjectKey="SUBJ-BETA",
            recordStatus="Incomplete",
            user="crc_jones",
            reasonForChange="Initial entry",
        ),
    ]
    del sdk.record_revisions.async_list

    sdk.intervals.list.return_value = [
        Interval(studyKey="ONCO_01", intervalId=1, intervalName="Baseline", intervalSequence=1)
    ]
    del sdk.intervals.async_list

    sdk.codings.list.return_value = [
        Coding(studyKey="ONCO_01", codingId=9, recordId=1, variable="AETERM", code="10019211")
    ]
    del sdk.codings.async_list

    sdk.users.list.return_value = [
        User(userId="U-1", login="pi_smith", firstName="Alice", lastName="Smith", active=True)
    ]
    del sdk.users.async_list

    # JobsEndpoint has .get ONLY (no .list / .async_list)
    del sdk.jobs.list
    del sdk.jobs.async_list
    sdk.jobs.get.return_value = JobStatus(
        jobId="J-1", batchId="B-1", state="COMPLETED", progress=100
    )

    return sdk


@pytest.mark.asyncio
async def test_subject_key_display_drilldown_clear_and_phi_masking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify SubjectTable displays subject_key, filters child views on selection, clears, and masks PHI."""
    sdk = _build_clinical_sdk()
    fake_dd = DataDictionary(
        metadata=Metadata(studyName="ONCO_01"),
        forms=[
            DDForm(
                formName="Demographics",
                formKey="DM",
                variables=[DDVariable(form="DM", variableName="AGE", variableType="integer")],
            )
        ],
    )
    monkeypatch.setattr("imednet_tui.views.load_from_api", lambda _s, _k: fake_dd)

    app = ImednetTuiApp(sdk)
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DashboardScreen)

        # Select Study ONCO_01
        screen.on_study_list_study_selected(StudyList.StudySelected("ONCO_01", "Oncology Phase II"))
        await pilot.pause()
        await screen.workers.wait_for_complete()
        await pilot.pause()

        subject_table = screen.query_one(SubjectTable)
        assert subject_table.row_count == 2
        first_row = subject_table.get_row_at(0)
        # Column 0 is Subject Key (SUBJ-ALPHA), Column 1 is Subject ID (1001)
        assert first_row[0] == "SUBJ-ALPHA"
        assert first_row[1] == "1001"

        records_pane = screen.query_one("#records_pane", EntityTablePane)
        visits_pane = screen.query_one("#visits_pane", EntityTablePane)
        queries_pane = screen.query_one("#queries_pane", EntityTablePane)
        revisions_pane = screen.query_one("#revisions_pane", EntityTablePane)

        assert records_pane.query_one("#records_table", DataTable).row_count == 2
        assert visits_pane.query_one("#visits_table", DataTable).row_count == 2
        assert queries_pane.query_one("#queries_table", DataTable).row_count == 2
        assert revisions_pane.query_one("#record_revisions_table", DataTable).row_count == 2
        assert revisions_pane.is_read_only is True
        assert "21 CFR Part 11" in str(
            revisions_pane.query_one("#record_revisions_detail", Static).render()
        )

        # Keyboard selection on SubjectTable row 0 -> drills down to SUBJ-ALPHA
        subject_table.focus()
        await pilot.press("enter")
        await pilot.pause()

        assert screen.selected_subject_key == "SUBJ-ALPHA"
        assert records_pane.query_one("#records_table", DataTable).row_count == 1
        assert visits_pane.query_one("#visits_table", DataTable).row_count == 1
        assert queries_pane.query_one("#queries_table", DataTable).row_count == 1
        assert revisions_pane.query_one("#record_revisions_table", DataTable).row_count == 1

        # Press 'm' to toggle PHI masking via mask_clinical_phi
        await pilot.press("m")
        await pilot.pause()
        assert screen.phi_masked is True
        # Pseudonymous subject_key stays visible per SensitivityRegistry._exempt_keys
        masked_subject_row = subject_table.get_row_at(0)
        assert masked_subject_row[0] == "SUBJ-ALPHA"
        # Sensitive PHI keys (patient_name) and sensitive columns (Query Description, Revision Author) are masked
        masked_rec_row = records_pane.query_one("#records_table", DataTable).get_row_at(0)
        assert "***MASKED***" in masked_rec_row[5]
        assert "Jane Doe" not in masked_rec_row[5]
        masked_rev_row = revisions_pane.query_one("#record_revisions_table", DataTable).get_row_at(
            0
        )
        assert masked_rev_row[4] == "***MASKED***"

        # Press 'c' to clear Subject filter back to Study-wide scope
        await pilot.press("c")
        await pilot.pause()
        assert screen.selected_subject_key is None
        assert records_pane.query_one("#records_table", DataTable).row_count == 2


@pytest.mark.asyncio
async def test_entity_keyboard_filtering_and_datadict_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify Forms, Variables, Data Dictionary (API + CSV + validate), and keyboard filter input."""
    sdk = _build_clinical_sdk()
    fake_dd = DataDictionary(
        metadata=Metadata(studyName="ONCO_01"),
        forms=[
            DDForm(
                formName="Demographics",
                formKey="DM",
                variables=[
                    DDVariable(form="DM", variableName="AGE", variableType="integer"),
                    DDVariable(form="DM", variableName="SEX", variableType="dropdown"),
                ],
            )
        ],
    )
    monkeypatch.setattr("imednet_tui.views.load_from_api", lambda _s, _k: fake_dd)

    app = ImednetTuiApp(sdk)
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DashboardScreen)

        screen.on_study_list_study_selected(StudyList.StudySelected("ONCO_01", "Oncology Phase II"))
        await pilot.pause()
        await screen.workers.wait_for_complete()
        await pilot.pause()

        vars_pane = screen.query_one("#variables_pane", EntityTablePane)
        vars_table = vars_pane.query_one("#variables_table", DataTable)
        assert vars_table.row_count == 2

        # Filter Variables table via keyboard filter Input
        vars_filter = vars_pane.query_one("#variables_filter_input", Input)
        vars_filter.value = "AETERM"
        await pilot.pause()
        assert vars_table.row_count == 1
        assert vars_table.get_row_at(0)[1] == "AETERM"

        # Verify DataDictionaryPane loaded and validated cleanly
        dd_pane = screen.query_one("#datadict_pane", DataDictionaryPane)
        assert dd_pane.validation_errors == []
        dd_status = str(dd_pane.query_one("#datadict_status", Static).render())
        assert "VALID" in dd_status
        assert dd_pane.query_one("#datadict_vars_table", DataTable).row_count == 2

        # Test loading from REDCap CSV source path via DataDictionaryPane
        redcap_csv = tmp_path / "redcap.csv"
        redcap_csv.write_text(
            "Variable / Field Name,Form Name,Section Header,Field Type,Field Label,"
            'Choices, Calculations, OR Slider Labels\n'
            "subj_age,demographics,,text,Age,\n",
            encoding="utf-8",
        )
        await dd_pane.load_from_source_path(str(redcap_csv))
        assert dd_pane.dictionary is not None
        assert dd_pane.validation_errors == []
        assert dd_pane.query_one("#datadict_vars_table", DataTable).row_count == 1


@pytest.mark.asyncio
async def test_job_monitor_auto_loads_ledger_renders_progress_and_pauses_when_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify JobMonitor loads persisted ledger jobs, tracks manual IDs, renders progress, and pauses."""
    sdk = _build_clinical_sdk()
    monkeypatch.setattr(
        "imednet_tui.views.load_from_api",
        lambda _s, _k: DataDictionary(metadata=Metadata(studyName="ONCO_01")),
    )

    # Pre-seed JobLedger with an active job (simulating sdk.records.create in a prior CLI/SDK call)
    ledger = JobLedger()
    ledger.record_job(
        "ONCO_01",
        "BATCH-PERSISTED-1",
        job_id="JOB-P1",
        job_type="CreateRecords",
        state="PROCESSING",
        progress=80,
    )

    def _get_job_status(_study: str, batch_id: str) -> JobStatus:
        if batch_id == "BATCH-PERSISTED-1":
            return JobStatus(
                jobId="JOB-P1", batchId="BATCH-PERSISTED-1", state="PROCESSING", progress=80
            )
        return JobStatus(jobId="JOB-M1", batchId=batch_id, state="COMPLETED", progress=100)

    sdk.jobs.get.side_effect = _get_job_status

    app = ImednetTuiApp(sdk)
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DashboardScreen)

        screen.on_study_list_study_selected(StudyList.StudySelected("ONCO_01", "Oncology Phase II"))
        await pilot.pause()
        await screen.workers.wait_for_complete()
        await pilot.pause()

        monitor = screen.query_one(JobMonitor)
        rendered = str(monitor.render())
        assert "BATCH-PERSISTED-1" in rendered
        assert "80%" in rendered
        # 1 active job -> timer must NOT be paused
        assert monitor.is_polling_paused is False

        # Track a manual batch_id via #manual_job_input + #track_job_btn
        manual_input = screen.query_one("#manual_job_input", Input)
        manual_input.value = "BATCH-MANUAL-2"
        screen.on_button_pressed(Button.Pressed(screen.query_one("#track_job_btn", Button)))
        await pilot.pause()
        await monitor.workers.wait_for_complete()
        await pilot.pause()

        rendered_both = str(monitor.render())
        assert "BATCH-PERSISTED-1" in rendered_both
        assert "BATCH-MANUAL-2" in rendered_both
        assert JobLedger().get_job("ONCO_01", "BATCH-MANUAL-2") is not None

        # Transition BATCH-PERSISTED-1 to COMPLETED -> 0 active jobs -> timer pauses automatically
        sdk.jobs.get.side_effect = lambda _s, bid: JobStatus(
            jobId=f"J-{bid}", batchId=bid, state="COMPLETED", progress=100
        )
        await monitor.refresh_jobs()
        rendered_done = str(monitor.render())
        assert "100%" in rendered_done
        assert "Polling paused (0 active jobs)" in rendered_done
        assert monitor.is_polling_paused is True


@pytest.mark.asyncio
async def test_workflows_pane_discovers_all_9_runs_off_ui_thread_and_registers_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify WorkflowsPane discovers all 9 workflows, runs off UI thread, logs, and tracks jobs."""
    sdk = _build_clinical_sdk()
    monkeypatch.setattr(
        "imednet_tui.views.load_from_api",
        lambda _s, _k: DataDictionary(metadata=Metadata(studyName="ONCO_01")),
    )

    ui_thread_id = threading.get_ident()
    worker_thread_ids: list[int] = []

    def _mock_create_or_update(
        study_key: str,
        records_data: list[dict[str, Any]],
        wait_for_completion: bool = False,
    ) -> Job:
        del study_key, records_data, wait_for_completion
        worker_thread_ids.append(threading.get_ident())
        return Job(jobId="JOB-WF-77", batchId="BATCH-WF-77", state="PROCESSING")

    wf_ns = MagicMock()
    for wf_name in CANONICAL_WORKFLOW_DESCRIPTORS:
        setattr(wf_ns, wf_name, MagicMock())
    wf_ns.record_update.create_or_update_records.side_effect = _mock_create_or_update
    sdk.workflows = wf_ns

    discovered, notice = discover_workflow_plugins(sdk)
    assert notice is None
    assert {d.name for d in discovered} == set(CANONICAL_WORKFLOW_DESCRIPTORS.keys())
    assert len(discovered) == 9

    app = ImednetTuiApp(sdk)
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DashboardScreen)

        screen.on_study_list_study_selected(StudyList.StudySelected("ONCO_01", "Oncology Phase II"))
        await pilot.pause()
        await screen.workers.wait_for_complete()

        wf_pane = screen.query_one("#workflows_pane", WorkflowsPane)
        assert len(wf_pane.descriptors) == 9

        # Input validation error check (malformed JSON)
        wf_pane._select_descriptor(CANONICAL_WORKFLOW_DESCRIPTORS["record_update"])
        wf_pane.query_one("#workflow_payload_input", Input).value = "{invalid-json"
        await wf_pane.execute_selected_workflow()
        assert "Validation Error" in str(wf_pane.query_one("#workflow_result", Static).render())

        # Valid execution off the UI thread emitting BATCH-WF-77
        wf_pane.query_one(
            "#workflow_payload_input", Input
        ).value = '[{"formKey": "DM", "siteName": "Mayo Site", "data": {"AGE": 44}}]'
        await wf_pane.execute_selected_workflow()
        await pilot.pause()

        assert worker_thread_ids and worker_thread_ids[0] != ui_thread_id
        assert "BATCH-WF-77" in str(wf_pane.query_one("#workflow_result", Static).render())
        assert JobLedger().get_job("ONCO_01", "BATCH-WF-77") is not None
        assert "BATCH-WF-77" in screen.query_one(JobMonitor).tracked_job_ids
        assert any("record_update" in line for line in screen.log_viewer.lines)

    # Graceful degradation when imednet-workflows is not installed
    sdk_no_wf = MagicMock()

    def _raise_import() -> Any:
        raise ImportError("imednet-workflows not installed")

    type(sdk_no_wf).workflows = property(lambda _self: _raise_import())
    empty_list, deg_msg = discover_workflow_plugins(sdk_no_wf)
    assert empty_list == []
    assert deg_msg is not None and "imednet-workflows" in deg_msg


@pytest.mark.asyncio
async def test_triage_pane_enforces_allowed_transitions_and_non_empty_comments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify TriagePane lists items and enforces ALLOWED_TRANSITIONS + non-empty comments."""
    triage_db = tmp_path / "tui_triage_test.db"
    store = TriageStore(triage_db)
    store.upsert_item(
        TriageItem(
            item_id="TRI-100",
            study_key="ONCO_01",
            status=TriageStatus.NEW,
            severity="HIGH",
        )
    )

    sdk = _build_clinical_sdk()
    monkeypatch.setattr(
        "imednet_tui.views.load_from_api",
        lambda _s, _k: DataDictionary(metadata=Metadata(studyName="ONCO_01")),
    )

    app = ImednetTuiApp(sdk)
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DashboardScreen)

        triage_pane = screen.query_one("#triage_pane", TriagePane)
        triage_pane._explicit_db_path = triage_db
        await triage_pane.load_study("ONCO_01")
        await pilot.pause()

        assert triage_pane.query_one("#triage_table", DataTable).row_count == 1
        assert triage_pane.selected_item_id == "TRI-100"
        # Self-transition NEW -> NEW must be disabled per ALLOWED_TRANSITIONS
        assert triage_pane.query_one("#triage_to_new_btn", Button).disabled is True
        assert triage_pane.query_one("#triage_to_under_review_btn", Button).disabled is False
        assert triage_pane.query_one("#triage_to_resolved_btn", Button).disabled is False

        # Empty reviewer comment must be rejected
        triage_pane.query_one("#triage_comment_input", Input).value = "   "
        await triage_pane.transition_selected_item(TriageStatus.UNDER_REVIEW)
        assert "non-empty reviewer comment" in str(
            triage_pane.query_one("#triage_detail", Static).render()
        )
        assert store.get_triage_item("TRI-100").status == TriageStatus.NEW  # type: ignore[union-attr]

        # Valid transition NEW -> UNDER_REVIEW -> RESOLVED with comments
        triage_pane.query_one("#triage_reviewer_input", Input).value = "dm_reviewer"
        triage_pane.query_one("#triage_comment_input", Input).value = "Checking source eCRF"
        await triage_pane.transition_selected_item(TriageStatus.UNDER_REVIEW)
        assert store.get_triage_item("TRI-100").status == TriageStatus.UNDER_REVIEW  # type: ignore[union-attr]

        triage_pane.query_one("#triage_comment_input", Input).value = "Reviewer note during review"
        await triage_pane.annotate_selected_item()

        # Repeating Add Item on an existing reviewed item must reject duplicates and preserve history
        triage_pane.query_one("#triage_item_id_input", Input).value = "TRI-100"
        triage_pane.query_one("#triage_comment_input", Input).value = "Duplicate add attempt"
        triage_pane.on_button_pressed(
            Button.Pressed(triage_pane.query_one("#triage_add_btn", Button))
        )
        await triage_pane.workers.wait_for_complete()
        await pilot.pause()
        assert "already exists" in str(triage_pane.query_one("#triage_detail", Static).render())
        reviewed_item = store.get_triage_item("TRI-100")
        assert reviewed_item is not None
        assert reviewed_item.status == TriageStatus.UNDER_REVIEW
        assert reviewed_item.severity == "HIGH"
        assert [ann.comment for ann in reviewed_item.annotations] == ["Reviewer note during review"]
        assert len(reviewed_item.history) == 1

        triage_pane.query_one("#triage_comment_input", Input).value = "Verified and resolved"
        await triage_pane.transition_selected_item(TriageStatus.RESOLVED)
        final_item = store.get_triage_item("TRI-100")
        assert final_item is not None
        assert final_item.status == TriageStatus.RESOLVED
        assert len(final_item.history) == 2

        # Repeating Add Item on a RESOLVED item also rejects duplicates and preserves history
        triage_pane.query_one("#triage_item_id_input", Input).value = "  TRI-100  "
        triage_pane.query_one("#triage_comment_input", Input).value = "Duplicate resolved add"
        await triage_pane.create_triage_item(
            "  TRI-100  ", initial_comment="Duplicate resolved add"
        )
        assert "already exists" in str(triage_pane.query_one("#triage_detail", Static).render())
        still_resolved = store.get_triage_item("TRI-100")
        assert still_resolved is not None
        assert still_resolved.status == TriageStatus.RESOLVED
        assert len(still_resolved.history) == 2

        # From RESOLVED, ALLOWED_TRANSITIONS only permits UNDER_REVIEW (RESOLVED -> NEW is forbidden)
        assert triage_pane.query_one("#triage_to_new_btn", Button).disabled is True
        assert triage_pane.query_one("#triage_to_resolved_btn", Button).disabled is True
        assert triage_pane.query_one("#triage_to_under_review_btn", Button).disabled is False
        triage_pane.query_one("#triage_comment_input", Input).value = "Forbidden RESOLVED -> NEW"
        await triage_pane.transition_selected_item(TriageStatus.NEW)
        assert "Invalid Transition" in str(triage_pane.query_one("#triage_detail", Static).render())
        assert store.get_triage_item("TRI-100").status == TriageStatus.RESOLVED  # type: ignore[union-attr]

        # Switch bottom tab to triage-tab and verify focus_active_filter ('/') focuses filter input
        tabs = screen.query_one("#bottom-pane", TabbedContent)
        tabs.active = "triage-tab"
        await pilot.pause()
        screen.focus_active_filter()
        await pilot.pause()
        assert app.focused is triage_pane.query_one("#triage_filter_input", Input)


def test_execute_all_9_workflows_sync_and_helpers() -> None:
    """Verify parameter parsers, batch extraction, and _execute_workflow_sync across all 9 workflows."""
    assert _parse_kv_params("") == {}
    assert _parse_kv_params("mode=audit, start_date=2026-01-01") == {
        "mode": "audit",
        "start_date": "2026-01-01",
    }
    with pytest.raises(ValueError, match="key=value"):
        _parse_kv_params("missing_equals")
    with pytest.raises(ValueError, match="Empty parameter"):
        _parse_kv_params("=val")

    assert _parse_optional_json("   ") is None
    assert _parse_optional_json('{"a": 1}') == {"a": 1}

    uat_out = MagicMock(
        batchId=None,
        batch_id=None,
        submission_result=MagicMock(all_batch_ids=["B-UAT-1", "B-UAT-2"]),
    )
    assert _extract_batch_ids(uat_out) == ["B-UAT-1", "B-UAT-2"]

    logs: list[str] = []
    sdk = MagicMock()
    wf = sdk.workflows

    # 1. data_extraction (records & audit)
    wf.data_extraction.extract_records_by_criteria.return_value = [1, 2]
    wf.data_extraction.extract_audit_trail.return_value = [1]
    s1, b1 = _execute_workflow_sync(
        sdk, "data_extraction", "S1", {}, {"form_key": "DM"}, logs.append
    )
    assert "2 matching" in s1 and b1 == []
    s1b, _ = _execute_workflow_sync(
        sdk, "data_extraction", "S1", {"mode": "audit"}, None, logs.append
    )
    assert "1 audit" in s1b

    # 2. duckdb_centralizer (:memory:, custom db_path, invalid mode)
    wf.duckdb_centralizer.ingest_revisions.return_value = 4
    s2, _ = _execute_workflow_sync(
        sdk,
        "duckdb_centralizer",
        "S1",
        {"db_path": ":memory:"},
        {"start_date": "2026-01-01"},
        logs.append,
    )
    assert "4 revision" in s2
    with pytest.raises(ValueError, match="append"):
        _execute_workflow_sync(sdk, "duckdb_centralizer", "S1", {"mode": "bad"}, None, logs.append)

    # 3. query_management (open, subject:<K>, site:<N>, counts)
    wf.query_management.get_open_queries.return_value = [1]
    wf.query_management.get_queries_for_subject.return_value = [1, 2]
    wf.query_management.get_queries_by_site.return_value = [1]
    wf.query_management.get_query_state_counts.return_value = {"Open": 2}
    assert (
        "1 open"
        in _execute_workflow_sync(
            sdk, "query_management", "S1", {"action": "open"}, None, logs.append
        )[0]
    )
    assert (
        "SUB-1"
        in _execute_workflow_sync(
            sdk, "query_management", "S1", {"action": "subject:SUB-1"}, None, logs.append
        )[0]
    )
    assert (
        "Mayo"
        in _execute_workflow_sync(
            sdk, "query_management", "S1", {"action": "site:Mayo"}, None, logs.append
        )[0]
    )
    assert (
        "Open"
        in _execute_workflow_sync(
            sdk, "query_management", "S1", {"action": "counts"}, None, logs.append
        )[0]
    )

    # 4. record_mapper (dataframe, sdtm:AE, hierarchy)
    fake_df = MagicMock()
    fake_df.__len__.return_value = 3
    fake_df.columns = ["A", "B"]
    wf.record_mapper.dataframe.return_value = fake_df
    wf.record_mapper.sdtm_dataframe.return_value = fake_df
    wf.record_mapper.build_hierarchy.return_value = {"SUB-1": {}}
    assert (
        "3 study"
        in _execute_workflow_sync(
            sdk, "record_mapper", "S1", {"mode": "dataframe"}, "11", logs.append
        )[0]
    )
    assert (
        "AE"
        in _execute_workflow_sync(
            sdk, "record_mapper", "S1", {"mode": "sdtm:AE"}, None, logs.append
        )[0]
    )
    assert (
        "hierarchy"
        in _execute_workflow_sync(
            sdk, "record_mapper", "S1", {"mode": "hierarchy"}, None, logs.append
        )[0]
    )

    # 5 & 6. record_update validation & register_subjects
    with pytest.raises(ValueError, match="non-empty JSON array"):
        _execute_workflow_sync(sdk, "record_update", "S1", {}, None, logs.append)
    wf.register_subjects.register_subjects.return_value = Job(
        jobId="J-R", batchId="B-REG-9", state="PENDING"
    )
    _, reg_batches = _execute_workflow_sync(
        sdk,
        "register_subjects",
        "S1",
        {"wait": "false"},
        [{"formKey": "DM", "siteName": "Site 1", "subjectKey": "SUB-9", "data": {}}],
        logs.append,
    )
    assert reg_batches == ["B-REG-9"]

    # 7. subject_data
    with pytest.raises(ValueError, match="subject_key"):
        _execute_workflow_sync(sdk, "subject_data", "S1", {}, None, logs.append)
    wf.subject_data.get_all_subject_data.return_value = MagicMock(
        visits=[1], records=[1, 2], queries=[]
    )
    assert (
        "SUB-9"
        in _execute_workflow_sync(
            sdk, "subject_data", "S1", {"subject_key": "SUB-9"}, None, logs.append
        )[0]
    )

    # 8 & 9. uat & uat_inspector
    class _FakeUat:
        def __init__(self, _sdk: Any, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def run(self, **_kw: Any) -> Any:
            self.kwargs["on_phase_change"]("INSPECTING")
            self.kwargs["on_progress"]("B-UAT-99", "PROCESSING", 1.0)
            res = MagicMock()
            res.batchId = None
            res.batch_id = None
            res.submission_result = MagicMock(all_batch_ids=["B-UAT-99"])
            res.summary.return_value = "UAT OK"
            return res

    wf.uat = _FakeUat(sdk)
    _, uat_batches = _execute_workflow_sync(
        sdk, "uat", "S1", {"subject_count": "1"}, ["DM"], logs.append
    )
    assert uat_batches == ["B-UAT-99"]

    wf.uat_inspector.inspect.return_value = MagicMock(
        forms=[1], variables=[1, 2], intervals=[1], sites=[1]
    )
    assert (
        "Inspected 'S1'"
        in _execute_workflow_sync(
            sdk, "uat_inspector", "S1", {"force_refresh": "true"}, None, logs.append
        )[0]
    )
    assert len(logs) >= 10


@pytest.mark.asyncio
async def test_triage_crud_filters_degradation_and_view_details(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Exercise TriagePane item creation, annotations, filter buttons, degradation, and view previews."""
    assert _stringify_cell(None) == "-"
    assert _stringify_cell([]) == "-"
    assert _stringify_cell([1, 2]) == "2 item(s)"
    assert _stringify_cell(TriageStatus.NEW) == "NEW"

    sdk = _build_clinical_sdk()
    dd_with_rules = DataDictionary(
        metadata=Metadata(studyName="ONCO_01", validationWarnings=["Minor note"]),
        forms=[
            DDForm(formName="EmptyForm", formKey="EMPTY", variables=[]),
            DDForm(
                formName="Demographics",
                formKey="DM",
                variables=[DDVariable(form="DM", variableName="AGE", variableType="integer")],
            ),
        ],
        business_logic=[BusinessLogicRule(id="R1", name="AgeCheck", type="EditCheck", form="DM")],
    )
    monkeypatch.setattr("imednet_tui.views.load_from_api", lambda _s, _k: dd_with_rules)

    triage_db = tmp_path / "crud_triage.db"
    app = ImednetTuiApp(sdk)
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DashboardScreen)

        screen.on_study_list_study_selected(StudyList.StudySelected("ONCO_01", "Oncology Phase II"))
        await pilot.pause()
        await screen.workers.wait_for_complete()
        await pilot.pause()

        # Verify DataDictionary rules & filter & Validate button
        dd_pane = screen.query_one("#datadict_pane", DataDictionaryPane)
        assert dd_pane.query_one("#datadict_rules_table", DataTable).row_count == 1
        dd_pane.query_one("#datadict_filter_input", Input).value = "AgeCheck"
        await pilot.pause()
        dd_pane.on_button_pressed(
            Button.Pressed(dd_pane.query_one("#datadict_validate_btn", Button))
        )
        assert "Warnings: 1" in str(dd_pane.query_one("#datadict_status", Static).render())

        # Verify EntityTablePane row selection preview & All Subjects button
        revisions_pane = screen.query_one("#revisions_pane", EntityTablePane)
        rev_table = revisions_pane.query_one("#record_revisions_table", DataTable)
        rev_table.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert "(READ-ONLY)" in str(
            revisions_pane.query_one("#record_revisions_detail", Static).render()
        )
        revisions_pane.on_button_pressed(
            Button.Pressed(revisions_pane.query_one("#record_revisions_clear_subject_btn", Button))
        )
        await pilot.pause()

        # Verify TriagePane item creation, annotation, status filters, and text filter
        triage_pane = screen.query_one("#triage_pane", TriagePane)
        triage_pane._explicit_db_path = triage_db
        await triage_pane.load_study("ONCO_01")

        await triage_pane.create_triage_item("")
        assert "must not be empty" in str(triage_pane.query_one("#triage_detail", Static).render())

        triage_pane.query_one("#triage_item_id_input", Input).value = "TRI-NEW-1"
        triage_pane.query_one("#triage_reviewer_input", Input).value = "cra_1"
        triage_pane.query_one("#triage_comment_input", Input).value = "Created in TUI"
        triage_pane.on_button_pressed(
            Button.Pressed(triage_pane.query_one("#triage_add_btn", Button))
        )
        await triage_pane.workers.wait_for_complete()
        await pilot.pause()
        assert triage_pane.query_one("#triage_table", DataTable).row_count == 1

        triage_pane.query_one("#triage_comment_input", Input).value = ""
        await triage_pane.annotate_selected_item()
        assert "must not be empty" in str(triage_pane.query_one("#triage_detail", Static).render())

        triage_pane.query_one("#triage_comment_input", Input).value = "Follow-up note"
        triage_pane.on_button_pressed(
            Button.Pressed(triage_pane.query_one("#triage_annotate_btn", Button))
        )
        await triage_pane.workers.wait_for_complete()

        triage_pane.query_one("#triage_comment_input", Input).value = "Reviewing item"
        triage_pane.on_button_pressed(
            Button.Pressed(triage_pane.query_one("#triage_to_under_review_btn", Button))
        )
        await triage_pane.workers.wait_for_complete()

        for btn_id in (
            "#triage_filter_new",
            "#triage_filter_resolved",
            "#triage_filter_review",
            "#triage_filter_all",
        ):
            triage_pane.on_button_pressed(Button.Pressed(triage_pane.query_one(btn_id, Button)))
            await pilot.pause()
        await triage_pane.workers.wait_for_complete()

        triage_pane.query_one("#triage_filter_input", Input).value = "TRI-NEW-1"
        await pilot.pause()
        assert triage_pane.query_one("#triage_table", DataTable).row_count == 1

        # Verify WorkflowsPane graceful degradation + validation + ImportError + RuntimeError
        wf_pane = screen.query_one("#workflows_pane", WorkflowsPane)
        wf_pane.selected_workflow = None
        await wf_pane.execute_selected_workflow()
        assert "Select a workflow" in str(wf_pane.query_one("#workflow_result", Static).render())

        wf_pane._select_descriptor(CANONICAL_WORKFLOW_DESCRIPTORS["uat_inspector"])
        wf_pane.query_one("#workflow_study_input", Input).value = ""
        wf_pane.current_study_key = None
        await wf_pane.execute_selected_workflow()
        assert "study_key is required" in str(
            wf_pane.query_one("#workflow_result", Static).render()
        )

        wf_pane.set_study("ONCO_01")
        monkeypatch.setattr(
            "imednet_tui.workflows_pane._execute_workflow_sync",
            MagicMock(side_effect=ImportError("duckdb missing")),
        )
        await wf_pane.execute_selected_workflow()
        assert "Optional dependency unavailable" in str(
            wf_pane.query_one("#workflow_result", Static).render()
        )

        monkeypatch.setattr(
            "imednet_tui.workflows_pane._execute_workflow_sync",
            MagicMock(side_effect=RuntimeError("unexpected failure")),
        )
        wf_pane.on_button_pressed(Button.Pressed(wf_pane.query_one("#workflow_run_btn", Button)))
        await wf_pane.workers.wait_for_complete()
        assert "failed: unexpected failure" in str(
            wf_pane.query_one("#workflow_result", Static).render()
        )
        wf_pane.on_button_pressed(
            Button.Pressed(wf_pane.query_one("#workflow_refresh_btn", Button))
        )

    # Verify _load_triage_store_class graceful degradation when imednet_workflows is missing
    monkeypatch.setattr(
        "imednet_tui.triage_pane.importlib.import_module",
        MagicMock(side_effect=ImportError("no imednet_workflows")),
    )
    cls_res, err_msg = _load_triage_store_class()
    assert cls_res is None and err_msg is not None and "imednet-workflows" in err_msg


@pytest.mark.asyncio
async def test_entity_sensitive_field_masking_in_table_and_detail_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify table cells and selected-row detail previews apply each entity's sensitive field rules."""
    sdk = _build_clinical_sdk()
    sdk.users.list.return_value = [
        User(
            userId="U-SYNTH-1",
            login="synth_login_handle",
            firstName="SynthFirst",
            lastName="SynthLast",
            email="synth.investigator@example.test",
            active=True,
        )
    ]
    sdk.queries.list.return_value = [
        Query(
            studyKey="ONCO_01",
            annotationId=701,
            subjectKey="SUBJ-ALPHA",
            variable="AGE",
            description="SynthQueryDescriptionText",
            queryComments=[
                QueryComment(
                    sequence=1,
                    user="synth_query_author",
                    comment="SynthQueryThreadComment",
                )
            ],
        )
    ]
    sdk.record_revisions.list.return_value = [
        RecordRevision(
            studyKey="ONCO_01",
            recordRevisionId=901,
            recordId=1,
            subjectKey="SUBJ-ALPHA",
            recordStatus="Complete",
            user="synth_revision_author",
            reasonForChange="SynthRevisionReasonText",
        )
    ]
    sdk.records.list.return_value = [
        Record(
            studyKey="ONCO_01",
            recordId=1,
            subjectKey="SUBJ-ALPHA",
            formKey="DM",
            visitId=11,
            recordStatus="Complete",
            recordData={"patient_name": "SynthPatientFullName", "AGE": 51},
        )
    ]
    monkeypatch.setattr(
        "imednet_tui.views.load_from_api",
        lambda _s, _k: DataDictionary(metadata=Metadata(studyName="ONCO_01")),
    )

    app = ImednetTuiApp(sdk)
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DashboardScreen)

        screen.on_study_list_study_selected(StudyList.StudySelected("ONCO_01", "Oncology Phase II"))
        await pilot.pause()
        await screen.workers.wait_for_complete()
        await pilot.pause()

        users_pane = screen.query_one("#users_pane", EntityTablePane)
        queries_pane = screen.query_one("#queries_pane", EntityTablePane)
        revisions_pane = screen.query_one("#revisions_pane", EntityTablePane)
        records_pane = screen.query_one("#records_pane", EntityTablePane)

        def _select_first_row(pane: EntityTablePane) -> str:
            table = pane.query_one(f"#{pane.table_id}", DataTable)
            pane.on_data_table_row_selected(MagicMock(data_table=table, cursor_row=0))
            return str(pane.query_one(f"#{pane.spec.key}_detail", Static).render())

        # Unmasked: synthetic values appear in both table cells and detail previews
        unmasked_user_row = users_pane.query_one("#users_table", DataTable).get_row_at(0)
        assert unmasked_user_row[1:4] == ["synth_login_handle", "SynthFirst", "SynthLast"]
        unmasked_user_detail = _select_first_row(users_pane)
        for token in ("synth_login_handle", "SynthFirst", "SynthLast"):
            assert token in unmasked_user_detail

        unmasked_query_detail = _select_first_row(queries_pane)
        assert "SynthQueryDescriptionText" in unmasked_query_detail
        unmasked_rev_detail = _select_first_row(revisions_pane)
        assert "SynthRevisionReasonText" in unmasked_rev_detail
        unmasked_rec_detail = _select_first_row(records_pane)
        assert "SynthPatientFullName" in unmasked_rec_detail

        # Enable PHI masking ('m'): both table cells AND active/newly-selected previews mask sensitive fields
        await pilot.press("m")
        await pilot.pause()
        assert screen.phi_masked is True

        masked_user_row = users_pane.query_one("#users_table", DataTable).get_row_at(0)
        assert masked_user_row[0] == "U-SYNTH-1"
        assert masked_user_row[1:4] == ["***MASKED***", "***MASKED***", "***MASKED***"]

        for user_detail in (
            str(users_pane.query_one("#users_detail", Static).render()),
            _select_first_row(users_pane),
        ):
            assert "U-SYNTH-1" in user_detail
            assert "***MASKED***" in user_detail
            for secret in (
                "synth_login_handle",
                "SynthFirst",
                "SynthLast",
                "synth.investigator@example.test",
            ):
                assert secret not in user_detail

        masked_query_row = queries_pane.query_one("#queries_table", DataTable).get_row_at(0)
        assert masked_query_row[4] == "***MASKED***"
        masked_query_detail = _select_first_row(queries_pane)
        assert "***MASKED***" in masked_query_detail
        for secret in (
            "SynthQueryDescriptionText",
            "synth_query_author",
            "SynthQueryThreadComment",
        ):
            assert secret not in masked_query_detail

        masked_rev_row = revisions_pane.query_one("#record_revisions_table", DataTable).get_row_at(
            0
        )
        assert masked_rev_row[4:6] == ["***MASKED***", "***MASKED***"]
        masked_rev_detail = _select_first_row(revisions_pane)
        assert "***MASKED***" in masked_rev_detail
        for secret in ("synth_revision_author", "SynthRevisionReasonText"):
            assert secret not in masked_rev_detail

        masked_rec_row = records_pane.query_one("#records_table", DataTable).get_row_at(0)
        assert "***MASKED***" in masked_rec_row[5]
        assert "SynthPatientFullName" not in masked_rec_row[5]
        masked_rec_detail = _select_first_row(records_pane)
        assert "***MASKED***" in masked_rec_detail
        assert "SynthPatientFullName" not in masked_rec_detail
        assert "51" in masked_rec_detail


@pytest.mark.asyncio
async def test_job_monitor_keeps_polling_when_16th_job_is_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify JobMonitor keeps polling when jobs 1..15 are COMPLETED and job 16 is PROCESSING."""
    sdk = _build_clinical_sdk()
    monkeypatch.setattr(
        "imednet_tui.views.load_from_api",
        lambda _s, _k: DataDictionary(metadata=Metadata(studyName="ONCO_01")),
    )

    completed_ids = [f"BATCH-DONE-{idx:02d}" for idx in range(1, 16)]
    active_16th_id = "BATCH-PROC-16"
    job_16_state = {"state": "PROCESSING", "progress": 45, "polls": 0}

    def _get_job(_study: str, batch_id: str) -> JobStatus:
        if batch_id == active_16th_id:
            job_16_state["polls"] += 1
            return JobStatus(
                jobId="JOB-16",
                batchId=active_16th_id,
                state=str(job_16_state["state"]),
                progress=int(job_16_state["progress"]),
            )
        return JobStatus(
            jobId=f"JOB-{batch_id}",
            batchId=batch_id,
            state="COMPLETED",
            progress=100,
        )

    sdk.jobs.get.side_effect = _get_job

    app = ImednetTuiApp(sdk)
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DashboardScreen)

        monitor = screen.query_one(JobMonitor)
        if monitor.timer is not None:
            monitor.timer.stop()
        monitor.poll_interval = 0.05
        monitor.timer = monitor.set_interval(0.05, monitor.refresh_jobs, pause=True)
        monitor._pause_polling()

        monitor.tracked_job_ids = [*completed_ids, active_16th_id]
        monitor.current_study_key = "ONCO_01"
        monitor._resume_polling()
        await monitor.refresh_jobs()

        rendered_active = str(monitor.render())
        assert "Active & Recent Jobs (16)" in rendered_active
        assert "BATCH-DONE-01" in rendered_active
        assert "BATCH-DONE-15" in rendered_active
        assert active_16th_id not in rendered_active
        assert "1 more tracked job(s) (1 active total)" in rendered_active
        assert "Polling paused" not in rendered_active
        assert monitor.is_polling_paused is False

        # Verify background interval timer continues polling job 16 while it is PROCESSING
        polls_before = int(job_16_state["polls"])
        await pilot.pause(0.14)
        assert int(job_16_state["polls"]) > polls_before
        assert monitor.is_polling_paused is False

        # Once job 16 reaches COMPLETED, polling pauses automatically
        job_16_state["state"] = "COMPLETED"
        job_16_state["progress"] = 100
        await pilot.pause(0.14)
        await monitor.workers.wait_for_complete()

        rendered_idle = str(monitor.render())
        assert monitor.is_polling_paused is True
        assert "Polling paused (0 active jobs)" in rendered_idle


@pytest.mark.asyncio
async def test_synthetic_adapters_mask_detail_keep_polling_and_preserve_triage_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify synthetic SimpleNamespace/dataclass/dict adapters across previews, polling, and triage."""

    @dataclass
    class _SyntheticRevision:
        record_revision_id: int
        record_id: int
        subject_key: str
        record_status: str
        modified_by: str
        reason_for_change: str

    class _BaseSlotsCoding:
        __slots__ = (
            "coding_id",
            "record_id",
            "variable",
            "code",
            "coded_by",
            "reason",
            "dictionary_name",
            "email",
        )

    class _SlotsCoding(_BaseSlotsCoding):
        __slots__ = "value"  # noqa: PLC0205

        def __init__(self) -> None:
            self.coding_id = "COD-SLOTS-1"
            self.record_id = 1
            self.variable = "AETERM"
            self.code = "10019211"
            self.coded_by = "slots_coder_login"
            self.reason = "SlotsCodingReason"
            self.dictionary_name = "MedDRA"
            self.email = "coder.slots@example.test"
            self.value = "SlotsVerbatimTerm"

    from collections import namedtuple

    _AuditTuple = namedtuple("_AuditTuple", ["login", "role_code"])

    sdk = _build_clinical_sdk()
    sdk.codings.list.return_value = [_SlotsCoding()]
    sdk.users.list.return_value = [
        SimpleNamespace(
            user_id="U-NS-1",
            login="ns_login_handle",
            first_name="NsFirst",
            last_name="NsLast",
            email="ns.investigator@example.test",
            username="ns_username",
            audit_meta=_AuditTuple("nested_login_handle", "CRA"),
            user_active_in_study=True,
        )
    ]
    sdk.record_revisions.list.return_value = [
        _SyntheticRevision(
            record_revision_id=950,
            record_id=1,
            subject_key="SUBJ-ALPHA",
            record_status="Complete",
            modified_by="ns_revision_author",
            reason_for_change="NsRevisionReasonText",
        )
    ]
    sdk.records.list.return_value = [
        SimpleNamespace(
            record_id=77,
            subject_key="SUBJ-ALPHA",
            form_key="DM",
            visit_id=11,
            record_status="Complete",
            record_data=SimpleNamespace(
                patient_name="NsPatientFullName",
                first_name="NsRecordFirstName",
                AGE=64,
            ),
        )
    ]
    monkeypatch.setattr(
        "imednet_tui.views.load_from_api",
        lambda _s, _k: DataDictionary(metadata=Metadata(studyName="ONCO_01")),
    )

    # Synthetic dict/namespace job adapter with 15 completed jobs + 16th processing job (state=None, status='PROCESSING')
    completed_ids = [f"SYNTH-DONE-{idx:02d}" for idx in range(1, 16)]
    active_16th = "SYNTH-PROC-16"
    poll_state = {"status": "PROCESSING", "polls": 0}

    def _get_synthetic_job(_study: str, batch_id: str) -> Any:
        if batch_id == active_16th:
            poll_state["polls"] += 1
            return SimpleNamespace(
                batch_id=active_16th,
                state="",
                status=str(poll_state["status"]),
                progress=30,
            )
        return {"batchId": batch_id, "state": "COMPLETED", "progress": 100}

    sdk.jobs.get.side_effect = _get_synthetic_job

    # Synthetic legacy TriageStore adapter lacking create_item()
    existing_triage = TriageItem(
        item_id="SYNTH-TRI-1",
        study_key="ONCO_01",
        status=TriageStatus.UNDER_REVIEW,
        assignee="lead_reviewer",
        severity="HIGH",
    )

    class _LegacyStoreAdapter:
        def __init__(self, _db_path: Path) -> None:
            self.upserts = 0

        def get_queue(self, _study_key: str, _status: Any = None) -> list[TriageItem]:
            return [existing_triage]

        def get_triage_item(self, item_id: str) -> TriageItem | None:
            return existing_triage if item_id == "SYNTH-TRI-1" else None

        def upsert_item(self, _item: TriageItem) -> None:
            self.upserts += 1

    monkeypatch.setattr(
        "imednet_tui.triage_pane._load_triage_store_class",
        lambda: (_LegacyStoreAdapter, None),
    )

    app = ImednetTuiApp(sdk)
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DashboardScreen)

        screen.on_study_list_study_selected(StudyList.StudySelected("ONCO_01", "Oncology Phase II"))
        await pilot.pause()
        await screen.workers.wait_for_complete()
        await pilot.pause()

        await pilot.press("m")
        await pilot.pause()

        users_pane = screen.query_one("#users_pane", EntityTablePane)
        revisions_pane = screen.query_one("#revisions_pane", EntityTablePane)
        records_pane = screen.query_one("#records_pane", EntityTablePane)
        codings_pane = screen.query_one("#codings_pane", EntityTablePane)

        for pane in (users_pane, revisions_pane, records_pane, codings_pane):
            tbl = pane.query_one(f"#{pane.table_id}", DataTable)
            pane.on_data_table_row_selected(MagicMock(data_table=tbl, cursor_row=0))

        user_detail = str(users_pane.query_one("#users_detail", Static).render())
        assert "U-NS-1" in user_detail
        assert "CRA" in user_detail
        assert "***MASKED***" in user_detail
        for sensitive_val in (
            "ns_login_handle",
            "NsFirst",
            "NsLast",
            "ns.investigator@example.test",
            "ns_username",
            "nested_login_handle",
        ):
            assert sensitive_val not in user_detail

        coding_detail = str(codings_pane.query_one("#codings_detail", Static).render())
        assert "COD-SLOTS-1" in coding_detail
        assert "MedDRA" in coding_detail
        for sensitive_val in (
            "slots_coder_login",
            "SlotsCodingReason",
            "SlotsVerbatimTerm",
            "coder.slots@example.test",
        ):
            assert sensitive_val not in coding_detail

        rev_detail = str(revisions_pane.query_one("#record_revisions_detail", Static).render())
        for sensitive_val in ("ns_revision_author", "NsRevisionReasonText"):
            assert sensitive_val not in rev_detail

        rec_row = records_pane.query_one("#records_table", DataTable).get_row_at(0)
        assert "NsPatientFullName" not in rec_row[5]
        assert "NsRecordFirstName" not in rec_row[5]
        assert "64" in rec_row[5]

        rec_detail = str(records_pane.query_one("#records_detail", Static).render())
        assert "NsPatientFullName" not in rec_detail
        assert "NsRecordFirstName" not in rec_detail
        assert "64" in rec_detail

        # Fallback triage store adapter rejects duplicate add without calling upsert_item
        triage_pane = screen.query_one("#triage_pane", TriagePane)
        await triage_pane.create_triage_item("SYNTH-TRI-1", initial_comment="Should reject")
        assert "already exists" in str(triage_pane.query_one("#triage_detail", Static).render())

        # Synthetic dict + SimpleNamespace(state=None, status='PROCESSING') 16-job polling
        monitor = screen.query_one(JobMonitor)
        if monitor.timer is not None:
            monitor.timer.stop()
        monitor.poll_interval = 0.05
        monitor.timer = monitor.set_interval(0.05, monitor.refresh_jobs, pause=True)
        monitor.tracked_job_ids = [*completed_ids, active_16th]
        monitor.current_study_key = "ONCO_01"
        monitor._resume_polling()
        await monitor.refresh_jobs()

        assert monitor.is_polling_paused is False
        polls_before = int(poll_state["polls"])
        await pilot.pause(0.14)
        assert int(poll_state["polls"]) > polls_before

        poll_state["status"] = "COMPLETED"
        await pilot.pause(0.14)
        await monitor.workers.wait_for_complete()
        assert monitor.is_polling_paused is True


def test_preview_extraction_property_opaque_enum_and_mapping_edge_cases() -> None:
    """Property-only, Enum, tuple, dict, and opaque adapters extract and mask cleanly."""
    from enum import Enum

    from imednet_tui.views import (
        ENTITY_VIEW_SPECS,
        _extract_attr,
        _extract_preview_payload,
        _mask_entity_preview_payload,
    )

    class _RoleState(Enum):
        ACTIVE = "ACTIVE_ROLE"

    class _PropertyOnlyUser:
        __slots__ = ()

        @property
        def user_id(self) -> str:
            return "U-PROP-1"

        @property
        def login(self) -> str:
            return "prop_login_handle"

        @property
        def first_name(self) -> str:
            return ""

        @property
        def user_active_in_study(self) -> _RoleState:
            return _RoleState.ACTIVE

    users_spec = ENTITY_VIEW_SPECS["users"]
    forms_spec = ENTITY_VIEW_SPECS["forms"]

    prop_payload, prop_opaque = _extract_preview_payload(_PropertyOnlyUser(), users_spec)
    assert prop_opaque is False
    assert prop_payload["user_id"] == "U-PROP-1"
    assert prop_payload["user_active_in_study"] == "ACTIVE_ROLE"
    masked_prop = _mask_entity_preview_payload(prop_payload, users_spec)
    assert masked_prop["login"] == "***MASKED***"
    assert masked_prop["first_name"] == ""

    nested_payload, nested_opaque = _extract_preview_payload(
        {"user_id": "U-2", "aliases": ({"login": "tuple_alias_login"},)},
        users_spec,
    )
    assert nested_opaque is False
    masked_nested = _mask_entity_preview_payload(nested_payload, users_spec)
    assert masked_nested["aliases"] == [{"login": "***MASKED***"}]

    class _OpaqueToken:
        __slots__ = ()

        def __str__(self) -> str:
            return "opaque-secret-token"

    opaque_payload, is_opaque = _extract_preview_payload(_OpaqueToken(), users_spec)
    assert is_opaque is True
    assert _mask_entity_preview_payload(
        opaque_payload, users_spec, is_opaque_fallback=is_opaque
    ) == {"item": "***MASKED***"}

    form_payload, form_opaque = _extract_preview_payload({"formKey": "DM"}, forms_spec)
    assert form_opaque is False
    assert _mask_entity_preview_payload(form_payload, forms_spec) == {"formKey": "DM"}
    assert _extract_attr({"form_key": None, "formKey": "AE"}, "form_key", "formKey") == "AE"
