"""Tests for BatchExecutionLedger, resumable chunk submission, error collection, and CLI JSON envelopes."""

from __future__ import annotations

import importlib
import json
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from imednet import ImednetSDK, cli
from imednet.core.client import Client
from imednet.core.context import reset_strict_mode_context, set_strict_mode_context
from imednet.errors import ServerError, ValidationError
from imednet.models.jobs import Job, JobStatus
from imednet.models.sites import Site
from imednet.models.subjects import Subject
from imednet.utils.batch_coordinator import AsyncBatchCoordinator, BatchCoordinator
from imednet.utils.batch_ledger import BatchExecutionLedger, resolve_opt_in_ledger
from imednet.utils.job_poller import AsyncJobPoller, JobFailedError, JobPoller
from imednet.utils.write_pipeline import (
    MASKED_SENTINEL,
    compute_chunk_sha256,
    compute_sequence_sha256,
)
from imednet_workflows.record_update import RecordUpdateWorkflow
from imednet_workflows.register_subjects import RegisterSubjectsWorkflow


class _CliResult:
    """Captured CLI invocation result."""

    def __init__(self, exit_code: int, stdout: str, stderr: str) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


def _invoke_cli(capsys: pytest.CaptureFixture[str], args: list[str]) -> _CliResult:
    capsys.readouterr()
    exit_code = 0
    try:
        cli.app(args)
    except SystemExit as exc:
        exit_code = int(exc.code) if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    captured = capsys.readouterr()
    return _CliResult(exit_code, captured.out, captured.err)


@pytest.fixture(autouse=True)
def disable_strict_mode(monkeypatch: pytest.MonkeyPatch):
    """Ensure contextvar strict mode and CLI env do not leak across tests."""
    token = set_strict_mode_context(False)
    monkeypatch.setenv("IMEDNET_API_KEY", "test-key")  # pragma: allowlist secret
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "test-secret")  # pragma: allowlist secret
    importlib.reload(cli)
    try:
        yield
    finally:
        importlib.reload(cli)
        reset_strict_mode_context(token)


def test_batch_execution_ledger_stores_zero_phi_or_clinical_values_in_wal_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BatchExecutionLedger uses WAL SQLite and persists zero clinical Variable values or PHI."""
    ledger_file = tmp_path / "audit_ledger.sqlite3"
    ledger = BatchExecutionLedger(ledger_file)

    phi_patient_name = "Jane Clinical-Patient"
    phi_ssn = "999-88-7777"
    phi_subject_key = "SUBJ-PHI-98765"

    chunks = [
        [
            {
                "formKey": "DEMOG",
                "subjectKey": phi_subject_key,
                "data": {"FULL_NAME": phi_patient_name, "SSN": phi_ssn},
            }
        ],
        [
            {
                "formKey": "DEMOG",
                "subjectKey": "SUBJ-PHI-54321",
                "data": {"FULL_NAME": "John Doe", "SSN": "111-22-3333"},
            }
        ],
    ]

    digests = [compute_chunk_sha256("STUDY_PHI_CHECK", c) for c in chunks]
    seq_sha = compute_sequence_sha256("STUDY_PHI_CHECK", 1, digests)
    resumed = ledger.prepare_run(
        "STUDY_PHI_CHECK",
        chunk_size=1,
        chunk_digests=digests,
        sequence_sha256=seq_sha,
        resume=False,
    )
    assert resumed == {}
    assert ledger.has_run("STUDY_PHI_CHECK") is True

    digest_0 = digests[0]
    ledger.record_chunk_accepted(
        "STUDY_PHI_CHECK",
        chunk_index=0,
        chunk_sha256=digest_0,
        job=Job(jobId="JOB-100", batchId="BATCH-100", state="PROCESSING"),
    )
    ledger.update_job_states(
        "STUDY_PHI_CHECK",
        [JobStatus(jobId="JOB-100", batchId="BATCH-100", state="COMPLETED")],
    )

    assert ledger_file.is_file()
    raw_bytes = ledger_file.read_bytes()
    for forbidden in (
        phi_patient_name.encode("utf-8"),
        phi_ssn.encode("utf-8"),
        phi_subject_key.encode("utf-8"),
        b"FULL_NAME",
        b"DEMOG",
    ):
        assert forbidden not in raw_bytes

    with sqlite3.connect(ledger_file) as conn:
        journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert str(journal_mode).lower() == "wal"

        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()
        }
        assert tables == {"batch_runs", "batch_chunks"}

        chunk_cols = [row[1] for row in conn.execute("PRAGMA table_info(batch_chunks);").fetchall()]
        assert chunk_cols == [
            "study_key",
            "chunk_index",
            "chunk_sha256",
            "batch_id",
            "job_id",
            "job_state",
            "failed_only",
            "created_at",
            "updated_at",
        ]

        stored_row = conn.execute(
            "SELECT study_key, chunk_index, chunk_sha256, batch_id, job_id, job_state "
            "FROM batch_chunks"
        ).fetchone()
        assert stored_row == (
            "STUDY_PHI_CHECK",
            0,
            digest_0,
            "BATCH-100",
            "JOB-100",
            "COMPLETED",
        )

    # Verify resolve_opt_in_ledger respects IMEDNET_BATCH_LEDGER_PATH when resume=True
    assert resolve_opt_in_ledger(resume=False) is None
    env_ledger = tmp_path / "from_env.sqlite3"
    monkeypatch.setenv("IMEDNET_BATCH_LEDGER_PATH", str(env_ledger))
    resolved = resolve_opt_in_ledger(resume=True)
    assert resolved is not None and resolved.db_path == env_ledger


def test_ledger_is_opt_in_and_masked_sentinel_writes_no_ledger_or_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stateless SDK calls create no SQLite files, and ***MASKED*** fails before ledger/HTTP."""
    monkeypatch.chdir(tmp_path)
    http_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        http_calls.append(request.url.path)
        return httpx.Response(
            202,
            json={"jobId": "J1", "batchId": "B1", "state": "PROCESSING"},
        )

    sdk = ImednetSDK(
        client=Client(
            api_key="k",  # pragma: allowlist secret
            security_key="s",  # pragma: allowlist secret
            base_url="https://edc.example.test",
            transport=httpx.MockTransport(handler),
        )
    )

    # 1. Stateless create_batch creates zero SQLite files
    summary = sdk.records.create_batch(
        "STUDY_STATELESS",
        [{"formKey": "DEMOG", "subjectKey": "S1", "data": {"AGE": 30}}],
        skip_validation=True,
    )
    assert summary.batch_ids == ["B1"]
    assert list(tmp_path.rglob("*.sqlite3")) == []

    # 2. ***MASKED*** sentinel fails BEFORE writing ledger or sending HTTP POST
    http_calls.clear()
    explicit_ledger_path = tmp_path / "should_not_exist.sqlite3"
    with pytest.raises(ValidationError, match=r"\*\*\*MASKED\*\*\*"):
        sdk.records.create_batch(
            "STUDY_MASKED",
            [{"formKey": "DEMOG", "subjectKey": "S1", "data": {"NOTE": MASKED_SENTINEL}}],
            skip_validation=True,
            ledger_path=explicit_ledger_path,
        )
    assert http_calls == []
    assert not explicit_ledger_path.exists()


def test_mid_batch_failure_persists_chunk1_exposes_batch_ids_collects_errors_and_resumes(
    tmp_path: Path,
) -> None:
    """Chunk 2 502 failure persists Chunk 1, exposes batch_ids, supports collect, and resumes."""
    records = [
        {"formKey": "DEMOG", "subjectKey": f"S-{i}", "data": {"AGE": 20 + i}} for i in range(5)
    ]
    ledger_path = tmp_path / "records_resume.sqlite3"
    submitted_payloads: list[list[dict[str, Any]]] = []
    fail_second_post = True

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal fail_second_post
        if request.url.path.endswith("/records") and request.method == "POST":
            body = json.loads(request.content.decode("utf-8"))
            submitted_payloads.append(body)
            call_num = len(submitted_payloads)
            if fail_second_post and call_num == 2:
                return httpx.Response(502, json={"message": "Bad Gateway"})
            return httpx.Response(
                202,
                json={
                    "jobId": f"JOB-{call_num}",
                    "batchId": f"BATCH-{call_num}",
                    "state": "PROCESSING",
                },
            )
        return httpx.Response(404)

    sdk = ImednetSDK(
        client=Client(
            api_key="k",  # pragma: allowlist secret
            security_key="s",  # pragma: allowlist secret
            base_url="https://edc.example.test",
            transport=httpx.MockTransport(handler),
        )
    )

    # 1. Run 1 with on_error="raise": Chunk 0 succeeds (BATCH-1), Chunk 1 fails 502
    with pytest.raises(ServerError) as exc_info:
        sdk.records.create_batch(
            "STUDY_R",
            records,
            chunk_size=2,
            skip_validation=True,
            ledger_path=ledger_path,
            on_error="raise",
        )
    err = exc_info.value
    assert getattr(err, "batch_ids", None) == ["BATCH-1"]
    partial = getattr(err, "partial_summary", None) or getattr(err, "batch_summary", None)
    assert partial is not None
    assert partial.batch_ids == ["BATCH-1"]
    assert len(submitted_payloads) == 2

    # 2. Resume Run with on_error="collect" while 502 still active on Chunk 1:
    # Chunk 0 is skipped from ledger, Chunk 1 fails and is collected without throwing!
    submitted_payloads.clear()
    fail_second_post = False

    def collect_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        submitted_payloads.append(body)
        if body[0]["subjectKey"] == "S-2":
            return httpx.Response(502, json={"message": "Still Bad Gateway"})
        return httpx.Response(
            202,
            json={"jobId": "JOB-X", "batchId": "BATCH-X", "state": "PROCESSING"},
        )

    sdk_collect = ImednetSDK(
        client=Client(
            api_key="k",  # pragma: allowlist secret
            security_key="s",  # pragma: allowlist secret
            base_url="https://edc.example.test",
            transport=httpx.MockTransport(collect_handler),
        )
    )
    collected_summary = sdk_collect.records.create_batch(
        "STUDY_R",
        records,
        chunk_size=2,
        skip_validation=True,
        ledger_path=ledger_path,
        resume=True,
        on_error="collect",
    )
    # In collect mode without fail_fast, Chunk 0 is resumed ("BATCH-1"), Chunk 1 fails (502),
    # and Chunk 2 (S-4) is also attempted or halted depending on coordinator loop; check contract:
    assert "BATCH-1" in collected_summary.batch_ids
    assert collected_summary.resumed_chunks == 1
    assert 1 in collected_summary.chunk_errors
    assert len(collected_summary.unsubmitted_chunks) >= 1
    assert collected_summary.has_errors is True
    # Chunk 0 (S-0, S-1) was NOT re-sent!
    sent_in_collect = [r["subjectKey"] for chunk in submitted_payloads for r in chunk]
    assert "S-0" not in sent_in_collect and "S-1" not in sent_in_collect
    assert "S-2" in sent_in_collect

    # 3. Healthy Resume Run from a clean Chunk-0-only ledger: skips Chunk 0, submits ONLY Chunk 1 & 2!
    clean_ledger = tmp_path / "clean_resume.sqlite3"
    submitted_payloads.clear()
    fail_second_post = True
    with pytest.raises(ServerError):
        sdk.records.create_batch(
            "STUDY_R2",
            records,
            chunk_size=2,
            skip_validation=True,
            ledger_path=clean_ledger,
            on_error="raise",
        )
    submitted_payloads.clear()
    fail_second_post = False
    resumed_summary = sdk.records.create_batch(
        "STUDY_R2",
        records,
        chunk_size=2,
        skip_validation=True,
        ledger_path=clean_ledger,
        resume=True,
    )
    assert resumed_summary.resumed_chunks == 1
    assert len(resumed_summary.batch_ids) == 3
    sent_subjects = [r["subjectKey"] for chunk in submitted_payloads for r in chunk]
    assert sent_subjects == ["S-2", "S-3", "S-4"]


def test_ledger_rejects_resume_when_chunk_size_or_sha256_digest_changes(
    tmp_path: Path,
) -> None:
    """BatchExecutionLedger rejects resume when chunk_size or payload digest changes."""
    ledger_path = tmp_path / "drift.sqlite3"
    records = [
        {"formKey": "DEMOG", "subjectKey": "S-1", "data": {"AGE": 30}},
        {"formKey": "DEMOG", "subjectKey": "S-2", "data": {"AGE": 40}},
    ]
    coordinator = BatchCoordinator(chunk_size=1)
    coordinator.submit(
        "STUDY_DRIFT",
        records,
        lambda chunk: Job(jobId="J1", batchId="B1", state="PROCESSING"),
        ledger_path=ledger_path,
    )

    # Drift 1: chunk_size changed (1 -> 2)
    with pytest.raises(ValidationError, match="chunk_size"):
        BatchCoordinator(chunk_size=2).submit(
            "STUDY_DRIFT",
            records,
            lambda chunk: Job(jobId="J2", batchId="B2", state="PROCESSING"),
            ledger_path=ledger_path,
            resume=True,
        )

    # Drift 2: payload modified (AGE 40 -> 41 changes SHA-256 sequence)
    drifted_records = [
        {"formKey": "DEMOG", "subjectKey": "S-1", "data": {"AGE": 30}},
        {"formKey": "DEMOG", "subjectKey": "S-2", "data": {"AGE": 41}},
    ]
    with pytest.raises(ValidationError, match="SHA-256"):
        coordinator.submit(
            "STUDY_DRIFT",
            drifted_records,
            lambda chunk: Job(jobId="J2", batchId="B2", state="PROCESSING"),
            ledger_path=ledger_path,
            resume=True,
        )


def test_register_subjects_workflow_resumes_without_duplicates_when_chunk1_already_enrolled(
    tmp_path: Path,
) -> None:
    """RegisterSubjectsWorkflow resumes cleanly even after Chunk 1 subjects appear in EDC."""
    ledger_path = tmp_path / "subjects_resume.sqlite3"
    subjects_input = [
        {"formId": 42, "siteName": "Site 1", "subjectKey": f"SUB-{i}", "INITIALS": f"P{i}"}
        for i in range(4)
    ]

    enrolled_in_edc: list[Subject] = []
    posted_chunks: list[list[dict[str, Any]]] = []
    should_fail_chunk_2 = True

    mock_sdk = MagicMock()
    mock_sdk.get_sites.return_value = [
        Site(
            studyKey="STUDY_SUB",
            siteId=1,
            siteName="Site 1",
            siteEnrollmentStatus="Enrollment Open",
        )
    ]
    mock_sdk.get_subjects.side_effect = lambda study_key=None: list(enrolled_in_edc)

    def create_record_side_effect(
        study_key: str, records_data: list[dict[str, Any]], **kw: Any
    ) -> Job:
        posted_chunks.append(records_data)
        if should_fail_chunk_2 and len(posted_chunks) == 2:
            raise RuntimeError("Gateway timeout on chunk 2")
        if len(posted_chunks) == 1 and should_fail_chunk_2:
            # Simulate Chunk 0 subjects becoming enrolled in EDC after 202 Accepted
            for item in records_data:
                enrolled_in_edc.append(
                    Subject(
                        studyKey=study_key,
                        subjectKey=item["subjectKey"],
                        siteName="Site 1",
                    )
                )
            return Job(jobId="SJ-1", batchId="SB-1", state="PROCESSING")
        return Job(jobId="SJ-2", batchId="SB-2", state="PROCESSING")

    mock_sdk.create_record.side_effect = create_record_side_effect
    wf = RegisterSubjectsWorkflow(mock_sdk)

    # Run 1: Chunk 0 accepted ("SB-1"), Chunk 1 fails
    collected = wf.register_subjects_batch(
        "STUDY_SUB",
        subjects_input,
        chunk_size=2,
        skip_existing=True,
        ledger_path=ledger_path,
        on_error="collect",
    )
    assert collected.batch_ids == ["SB-1"]
    assert collected.has_errors is True

    # Run 2 with resume=True: even though SUB-0 and SUB-1 are now in enrolled_in_edc,
    # canonical chunk digests match, Chunk 0 is resumed, and ONLY SUB-2 & SUB-3 are sent!
    posted_chunks.clear()
    should_fail_chunk_2 = False
    resumed = wf.register_subjects_batch(
        "STUDY_SUB",
        subjects_input,
        chunk_size=2,
        skip_existing=True,
        ledger_path=ledger_path,
        resume=True,
    )
    assert resumed.resumed_chunks == 1
    assert resumed.batch_ids == ["SB-1", "SB-2"]
    assert len(posted_chunks) == 1
    assert [item["subjectKey"] for item in posted_chunks[0]] == ["SUB-2", "SUB-3"]


async def test_async_batch_coordinator_and_async_register_subjects_resume_and_collect(
    tmp_path: Path,
) -> None:
    """AsyncBatchCoordinator and AsyncRegisterSubjectsWorkflow support resume and collect."""
    ledger_path = tmp_path / "async_subjects.sqlite3"
    subjects_input = [
        {"formId": 10, "siteName": "Site A", "subjectKey": f"ASUB-{i}"} for i in range(4)
    ]
    posted: list[list[dict[str, Any]]] = []
    fail_chunk_2 = True

    class _AsyncFacadeStub:
        async def async_get_sites(self, study_key: str | None = None) -> list[Site]:
            return [
                Site(
                    studyKey="STUDY_ASYNC",
                    siteId=1,
                    siteName="Site A",
                    siteEnrollmentStatus="Enrollment Open",
                )
            ]

        async def async_get_subjects(self, study_key: str | None = None) -> list[Any]:
            return []

        async def async_create_record(
            self, study_key: str, records_data: list[dict[str, Any]], **kw: Any
        ) -> Job:
            posted.append(records_data)
            if fail_chunk_2 and len(posted) == 2:
                raise RuntimeError("Async chunk 2 failure")
            idx = len(posted)
            return Job(jobId=f"AJ-{idx}", batchId=f"AB-{idx}", state="PROCESSING")

    wf = RegisterSubjectsWorkflow(_AsyncFacadeStub())
    first = await wf.async_register_subjects_batch(
        "STUDY_ASYNC",
        subjects_input,
        chunk_size=2,
        ledger_path=ledger_path,
        on_error="collect",
    )
    assert first.batch_ids == ["AB-1"]
    assert first.has_errors is True

    posted.clear()
    fail_chunk_2 = False
    second = await wf.async_register_subjects_batch(
        "STUDY_ASYNC",
        subjects_input,
        chunk_size=2,
        ledger_path=ledger_path,
        resume=True,
    )
    assert second.resumed_chunks == 1
    assert len(second.batch_ids) == 2
    assert len(posted) == 1
    assert [item["subjectKey"] for item in posted[0]] == ["ASUB-2", "ASUB-3"]


def test_polled_job_failure_retains_all_sibling_job_statuses_in_sync_and_collect(
    tmp_path: Path,
) -> None:
    """JobFailedError and on_error='collect' retain all completed sibling JobStatus items."""
    statuses_by_id = {
        "B-1": JobStatus(jobId="J-1", batchId="B-1", state="COMPLETED", successfulRecords=2),
        "B-2": JobStatus(jobId="J-2", batchId="B-2", state="FAILED", failedRecords=2),
        "B-3": JobStatus(jobId="J-3", batchId="B-3", state="COMPLETED", successfulRecords=1),
    }

    b2_calls = 0

    def _get_sync_status(sk: str, bid: str) -> JobStatus:
        nonlocal b2_calls
        if bid == "B-2":
            b2_calls += 1
            if b2_calls == 1:
                return JobStatus(jobId="J-2", batchId="B-2", state="PROCESSING")
        return statuses_by_id[bid]

    poller = JobPoller(_get_sync_status)

    # 1. JobPoller.poll_many(fail_fast=True) attaches completed sibling statuses to JobFailedError
    with pytest.raises(JobFailedError) as poll_exc_info:
        poller.poll_many("STUDY_POLL", ["B-1", "B-2", "B-3"], interval=0.01, fail_fast=True)
    poll_err = poll_exc_info.value
    assert set(poll_err.batch_ids) == {"B-1", "B-2", "B-3"}
    assert {
        bid: str(getattr(st.state, "value", st.state))
        for bid, st in poll_err.sibling_statuses.items()
    } == {
        "B-1": "COMPLETED",
        "B-2": "FAILED",
        "B-3": "COMPLETED",
    }

    # 2. BatchCoordinator with wait_for_completion=True & on_error="raise"
    ledger_path = tmp_path / "poll_ledger.sqlite3"
    records = [{"x": i} for i in range(5)]
    call_idx = 0

    def submit_chunk(chunk: list[dict[str, Any]]) -> Job:
        nonlocal call_idx
        call_idx += 1
        return Job(jobId=f"J-{call_idx}", batchId=f"B-{call_idx}", state="PROCESSING")

    coordinator = BatchCoordinator(chunk_size=2)
    with pytest.raises(JobFailedError) as coord_exc_info:
        coordinator.submit(
            "STUDY_POLL",
            records,
            submit_chunk,
            wait_for_completion=True,
            poll_many=lambda bids: poller.poll_many(
                "STUDY_POLL", bids, interval=0.0, fail_fast=False
            ),
            ledger_path=ledger_path,
            on_error="raise",
        )
    coord_err = coord_exc_info.value
    assert coord_err.batch_ids == ["B-1", "B-2", "B-3"]
    assert set(coord_err.sibling_statuses.keys()) == {"B-1", "B-2", "B-3"}

    # Ledger recorded terminal states; B-2 had successfulRecords=0 so failed_only=True -> retryable!
    ledger = BatchExecutionLedger(ledger_path)
    loaded = ledger.get_chunks("STUDY_POLL")
    assert loaded[0].job_state == "COMPLETED" and loaded[0].failed_only is False
    assert loaded[1].job_state == "FAILED" and loaded[1].failed_only is True
    assert loaded[2].job_state == "COMPLETED" and loaded[2].failed_only is False

    # 3. BatchCoordinator with wait_for_completion=True & on_error="collect"
    call_idx = 0
    collected = coordinator.submit(
        "STUDY_POLL_COLLECT",
        records,
        submit_chunk,
        wait_for_completion=True,
        poll_many=lambda bids: poller.poll_many(
            "STUDY_POLL_COLLECT", bids, interval=0.0, fail_fast=False
        ),
        on_error="collect",
    )
    assert collected.batch_ids == ["B-1", "B-2", "B-3"]
    assert {
        bid: str(getattr(st.state, "value", st.state)) for bid, st in collected.job_statuses.items()
    } == {
        "B-1": "COMPLETED",
        "B-2": "FAILED",
        "B-3": "COMPLETED",
    }
    assert collected.failed_batch_ids == ["B-2"]
    assert collected.has_errors is True


async def test_async_polled_job_failure_retains_all_sibling_job_statuses() -> None:
    """AsyncJobPoller and AsyncBatchCoordinator retain all sibling JobStatus items on failure."""
    statuses_by_id = {
        "AB-1": JobStatus(jobId="AJ-1", batchId="AB-1", state="COMPLETED", successfulRecords=2),
        "AB-2": JobStatus(jobId="AJ-2", batchId="AB-2", state="FAILED", failedRecords=2),
    }

    ab2_calls = 0

    async def _get_job(sk: str, bid: str) -> JobStatus:
        nonlocal ab2_calls
        if bid == "AB-2":
            ab2_calls += 1
            if ab2_calls == 1:
                return JobStatus(jobId="AJ-2", batchId="AB-2", state="PROCESSING")
        return statuses_by_id[bid]

    poller = AsyncJobPoller(_get_job)
    with pytest.raises(JobFailedError) as exc_info:
        await poller.async_poll_many("STUDY_APOLL", ["AB-1", "AB-2"], interval=0.01, fail_fast=True)
    assert set(exc_info.value.sibling_statuses.keys()) == {"AB-1", "AB-2"}

    idx = 0

    async def _submit(chunk: list[dict[str, Any]]) -> Job:
        nonlocal idx
        idx += 1
        return Job(jobId=f"AJ-{idx}", batchId=f"AB-{idx}", state="PROCESSING")

    async_coord = AsyncBatchCoordinator(chunk_size=2)
    collected = await async_coord.submit(
        "STUDY_APOLL",
        [{"x": 1}, {"x": 2}, {"x": 3}],
        _submit,
        wait_for_completion=True,
        poll_many=lambda bids: poller.async_poll_many(
            "STUDY_APOLL", bids, interval=0.0, fail_fast=False
        ),
        on_error="collect",
    )
    assert collected.batch_ids == ["AB-1", "AB-2"]
    assert {
        bid: str(getattr(st.state, "value", st.state)) for bid, st in collected.job_statuses.items()
    } == {"AB-1": "COMPLETED", "AB-2": "FAILED"}


def test_cli_wait_format_json_failure_emits_v1_envelope_and_exits_1_across_write_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI records create/update/import and subjects register write v1.0 JSON to stdout & exit 1."""
    csv_file = tmp_path / "batch.csv"
    csv_file.write_text(
        "formKey,subjectKey,AGE\nDEMOG,S-1,30\nDEMOG,S-2,40\nDEMOG,S-3,50\n",
        encoding="utf-8",
    )

    def make_failing_poll_sdk(**_kwargs: Any) -> ImednetSDK:
        post_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal post_count
            path = request.url.path
            if path.endswith("/forms"):
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "studyKey": "STUDY_CLI",
                                "formId": 101,
                                "formKey": "DEMOG",
                                "formName": "Demographics",
                            }
                        ]
                    },
                )
            if path.endswith("/variables"):
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "studyKey": "STUDY_CLI",
                                "formId": 101,
                                "formKey": "DEMOG",
                                "variableId": 1,
                                "variableName": "AGE",
                                "variableType": "Integer",
                            }
                        ]
                    },
                )
            if path.endswith("/sites") and request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "studyKey": "STUDY_CLI",
                                "siteId": 1,
                                "siteName": "Site A",
                                "siteEnrollmentStatus": "Enrollment Open",
                            }
                        ]
                    },
                )
            if path.endswith("/subjects") and request.method == "GET":
                return httpx.Response(200, json={"data": []})
            if path.endswith("/records") and request.method == "POST":
                post_count += 1
                return httpx.Response(
                    202,
                    json={
                        "jobId": f"JOB-{post_count}",
                        "batchId": f"BATCH-{post_count}",
                        "state": "PROCESSING",
                    },
                )
            if "/jobs/" in path:
                bid = path.rsplit("/", 1)[-1]
                state = "FAILED" if bid == "BATCH-2" else "COMPLETED"
                return httpx.Response(
                    200,
                    json={"jobId": f"J-{bid}", "batchId": bid, "state": state},
                )
            return httpx.Response(404)

        return ImednetSDK(
            client=Client(
                api_key="k",  # pragma: allowlist secret
                security_key="s",  # pragma: allowlist secret
                base_url="https://edc.example.test",
                transport=httpx.MockTransport(handler),
            )
        )

    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", make_failing_poll_sdk)

    # 1. records create --wait --format json
    res_create = _invoke_cli(
        capsys,
        [
            "records",
            "create",
            "STUDY_CLI",
            "--file",
            str(csv_file),
            "--chunk-size",
            "2",
            "--wait",
            "--interval",
            "0.01",
            "--format",
            "json",
        ],
    )
    assert res_create.exit_code == 1
    env_create = json.loads(res_create.stdout)
    assert env_create["schemaVersion"] == "1.0"
    assert env_create["status"] == "failed"
    item_create = env_create["items"][0]
    assert item_create["batchIds"] == ["BATCH-1", "BATCH-2"]
    assert {j["batchId"]: j["state"] for j in item_create["jobs"]} == {
        "BATCH-1": "COMPLETED",
        "BATCH-2": "FAILED",
    }

    # 2. records import --wait --format json
    res_import = _invoke_cli(
        capsys,
        [
            "records",
            "import",
            "STUDY_CLI",
            str(csv_file),
            "--chunk-size",
            "2",
            "--wait",
            "--interval",
            "0.01",
            "--format",
            "json",
        ],
    )
    assert res_import.exit_code == 1
    env_import = json.loads(res_import.stdout)
    assert env_import["schemaVersion"] == "1.0"
    assert env_import["status"] == "failed"
    item_import = env_import["items"][0]
    assert item_import["batchIds"] == ["BATCH-1", "BATCH-2"]
    assert len(item_import["jobs"]) == 2

    # 3. subjects register --wait --format json
    sub_csv = tmp_path / "subs.csv"
    sub_csv.write_text(
        "siteName,subjectKey,AGE\nSite A,S-1,30\nSite A,S-2,40\nSite A,S-3,50\n",
        encoding="utf-8",
    )
    res_sub = _invoke_cli(
        capsys,
        [
            "subjects",
            "register",
            "STUDY_CLI",
            "--file",
            str(sub_csv),
            "--form-id",
            "101",
            "--chunk-size",
            "2",
            "--wait",
            "--interval",
            "0.01",
            "--format",
            "json",
        ],
    )
    assert res_sub.exit_code == 1
    env_sub = json.loads(res_sub.stdout)
    assert env_sub["schemaVersion"] == "1.0"
    assert env_sub["status"] == "failed"
    item_sub = env_sub["items"][0]
    assert item_sub["batchIds"] == ["BATCH-1", "BATCH-2"]
    assert {j["batchId"]: j["state"] for j in item_sub["jobs"]} == {
        "BATCH-1": "COMPLETED",
        "BATCH-2": "FAILED",
    }

    # 4. records update --wait --format json (single record failing poll)
    def make_single_fail_sdk(**_kwargs: Any) -> ImednetSDK:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/forms"):
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "studyKey": "STUDY_CLI",
                                "formId": 101,
                                "formKey": "DEMOG",
                                "formName": "Demographics",
                            }
                        ]
                    },
                )
            if path.endswith("/variables"):
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "studyKey": "STUDY_CLI",
                                "formId": 101,
                                "formKey": "DEMOG",
                                "variableId": 1,
                                "variableName": "AGE",
                                "variableType": "Integer",
                            }
                        ]
                    },
                )
            if path.endswith("/records") and request.method == "POST":
                return httpx.Response(
                    202,
                    json={"jobId": "J-UPD", "batchId": "BATCH-UPD", "state": "PROCESSING"},
                )
            if "/jobs/" in path:
                return httpx.Response(
                    200,
                    json={"jobId": "J-UPD", "batchId": "BATCH-UPD", "state": "FAILED"},
                )
            return httpx.Response(404)

        return ImednetSDK(
            client=Client(
                api_key="k",  # pragma: allowlist secret
                security_key="s",  # pragma: allowlist secret
                base_url="https://edc.example.test",
                transport=httpx.MockTransport(handler),
            )
        )

    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", make_single_fail_sdk)
    res_update = _invoke_cli(
        capsys,
        [
            "records",
            "update",
            "STUDY_CLI",
            "--record-id",
            "999",
            "--form-key",
            "DEMOG",
            "-d",
            "AGE=35",
            "--wait",
            "--interval",
            "0.01",
            "--format",
            "json",
        ],
    )
    assert res_update.exit_code == 1
    env_update = json.loads(res_update.stdout)
    assert env_update["schemaVersion"] == "1.0"
    assert env_update["status"] == "failed"
    assert env_update["items"][0]["batchIds"] == ["BATCH-UPD"]


def test_cli_resume_and_ledger_path_skips_accepted_chunks_on_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI --resume --ledger-path persists accepted chunks on failure and skips them on retry."""
    csv_file = tmp_path / "resume_cli.csv"
    csv_file.write_text(
        "formKey,subjectKey,AGE\nDEMOG,S-1,21\nDEMOG,S-2,22\nDEMOG,S-3,23\n",
        encoding="utf-8",
    )
    ledger_file = tmp_path / "cli_ledger.sqlite3"
    posted_batches: list[list[dict[str, Any]]] = []
    fail_chunk_2 = True

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/forms"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "studyKey": "STUDY_CLI_RESUME",
                            "formId": 101,
                            "formKey": "DEMOG",
                            "formName": "Demographics",
                        }
                    ]
                },
            )
        if path.endswith("/variables"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "studyKey": "STUDY_CLI_RESUME",
                            "formId": 101,
                            "formKey": "DEMOG",
                            "variableId": 1,
                            "variableName": "AGE",
                            "variableType": "Integer",
                        }
                    ]
                },
            )
        if path.endswith("/records") and request.method == "POST":
            body = json.loads(request.content.decode("utf-8"))
            posted_batches.append(body)
            if fail_chunk_2 and len(posted_batches) == 2:
                return httpx.Response(502, json={"message": "Upstream 502"})
            idx = len(posted_batches)
            return httpx.Response(
                202,
                json={"jobId": f"J-{idx}", "batchId": f"CLI-BATCH-{idx}", "state": "PROCESSING"},
            )
        return httpx.Response(404)

    def _get_sdk(**_kwargs: Any) -> ImednetSDK:
        return ImednetSDK(
            client=Client(
                api_key="k",  # pragma: allowlist secret
                security_key="s",  # pragma: allowlist secret
                base_url="https://edc.example.test",
                transport=httpx.MockTransport(handler),
            )
        )

    monkeypatch.setattr("imednet.cli.utils.context.get_sdk", _get_sdk)

    # Attempt 1: Chunk 0 accepted (CLI-BATCH-1), Chunk 1 fails 502 -> exit 1 with JSON envelope
    run1 = _invoke_cli(
        capsys,
        [
            "records",
            "create",
            "STUDY_CLI_RESUME",
            "--file",
            str(csv_file),
            "--chunk-size",
            "2",
            "--ledger-path",
            str(ledger_file),
            "--schema",
            "api",
            "--format",
            "json",
        ],
    )
    assert run1.exit_code == 1
    env1 = json.loads(run1.stdout)
    assert env1["schemaVersion"] == "1.0"
    assert env1["status"] == "failed"
    assert env1["items"][0]["batchIds"] == ["CLI-BATCH-1"]

    # Attempt 2 with --resume: skips Chunk 0 (S-1, S-2) and sends ONLY Chunk 1 (S-3)!
    posted_batches.clear()
    fail_chunk_2 = False
    run2 = _invoke_cli(
        capsys,
        [
            "records",
            "create",
            "STUDY_CLI_RESUME",
            "--file",
            str(csv_file),
            "--chunk-size",
            "2",
            "--ledger-path",
            str(ledger_file),
            "--resume",
            "--schema",
            "api",
            "--format",
            "json",
        ],
    )
    assert run2.exit_code == 0, run2.stderr
    env2 = json.loads(run2.stdout)
    assert env2["schemaVersion"] == "1.0"
    assert env2["status"] == "complete"
    assert env2["items"][0]["resumedChunks"] == 1
    assert len(env2["items"][0]["batchIds"]) == 2
    assert len(posted_batches) == 1
    assert [r["subjectKey"] for r in posted_batches[0]] == ["S-3"]

    # Also verify RecordUpdateWorkflow exposes ledger_path/resume/on_error directly
    wf = RecordUpdateWorkflow(_get_sdk())
    posted_batches.clear()
    wf_resumed = wf.create_or_update_records_batch(
        "STUDY_CLI_RESUME",
        csv_file,
        chunk_size=2,
        ledger_path=ledger_file,
        resume=True,
    )
    assert wf_resumed.resumed_chunks == 2
    assert posted_batches == []
