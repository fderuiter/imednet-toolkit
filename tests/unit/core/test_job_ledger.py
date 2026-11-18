"""Unit tests for the shared SQLite JobLedger, SPI exports, RecordCreate integration, and jobs CLI."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from imednet import cli
from imednet.core.endpoint.operations.record_create import RecordCreateOperation
from imednet.endpoints.jobs import JobsEndpoint
from imednet.models.jobs import Job, JobState, JobStatus
from imednet.sdk import ImednetSDK
from imednet.spi import datadict as spi_datadict
from imednet.spi import jobs as spi_jobs
from imednet.spi.jobs import JobLedger, get_default_job_ledger_path


@pytest.fixture(autouse=True)
def isolated_ledger(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point default JobLedger storage to a temporary SQLite file per test."""
    db_file = tmp_path / "test_job_ledger.db"
    monkeypatch.setenv("IMEDNET_JOB_LEDGER_PATH", str(db_file))
    return db_file


def test_job_ledger_persists_across_instances_and_filters_active(isolated_ledger: Path) -> None:
    """Verify JobLedger persists active/recent jobs per Study across separate sessions."""
    assert get_default_job_ledger_path() == isolated_ledger

    ledger_a = JobLedger()
    entry1 = ledger_a.record_job(
        "STUDY_A",
        "BATCH-001",
        job_id="JOB-001",
        job_type="CreateRecords",
        state=JobState.PROCESSING,
        progress=45,
        source="sdk",
    )
    assert entry1.is_active is True
    assert entry1.is_terminal is False
    assert entry1.progress == 45

    ledger_a.record_job(
        "STUDY_A",
        "BATCH-002",
        job_id="JOB-002",
        job_type="RegisterSubjects",
        state=JobState.COMPLETED,
        source="cli",
    )
    ledger_a.record_job("STUDY_B", "BATCH-999", state="PENDING")

    # Re-open in a fresh instance (simulating separate CLI / TUI process)
    ledger_b = JobLedger(isolated_ledger)
    all_study_a = ledger_b.list_jobs("STUDY_A")
    assert {j.batch_id for j in all_study_a} == {"BATCH-001", "BATCH-002"}

    completed = ledger_b.get_job("STUDY_A", "BATCH-002")
    assert completed is not None
    assert completed.progress == 100
    assert completed.is_terminal is True

    active_study_a = ledger_b.list_active_jobs("STUDY_A")
    assert [j.batch_id for j in active_study_a] == ["BATCH-001"]

    assert ledger_b.remove_job("STUDY_A", "BATCH-002") is True
    assert ledger_b.remove_job("STUDY_A", "BATCH-002") is False
    assert [j.batch_id for j in ledger_b.list_jobs("STUDY_A")] == ["BATCH-001"]

    assert ledger_b.clear_study("STUDY_A") == 1
    assert ledger_b.list_jobs("STUDY_A") == []
    assert len(ledger_b.list_jobs("STUDY_B")) == 1


@pytest.mark.asyncio
async def test_record_create_operation_and_jobs_endpoint_sync_ledger() -> None:
    """Verify RecordCreateOperation records batch_id and JobsEndpoint.get updates state/progress."""
    op: RecordCreateOperation[Job] = RecordCreateOperation(
        "/api/v1/edc/studies/STUDY_REC/records",
        [{"formKey": "DM", "data": {"AGE": 50}}],
    )

    sync_resp = MagicMock()
    sync_resp.json.return_value = {
        "jobId": "J-SYNC-1",
        "batchId": "BATCH-SYNC-1",
        "state": "PENDING",
    }
    sync_client = MagicMock()
    sync_client.post.return_value = sync_resp

    job_sync = op.execute_sync(sync_client)
    assert isinstance(job_sync, Job)
    assert job_sync.batch_id == "BATCH-SYNC-1"

    async_op: RecordCreateOperation[Job] = RecordCreateOperation(
        "/api/v1/edc/studies/STUDY_REC/records",
        [{"formKey": "DM", "data": {"AGE": 51}}],
    )
    async_resp = MagicMock()
    async_resp.json.return_value = {
        "jobId": "J-ASYNC-1",
        "batchId": "BATCH-ASYNC-1",
        "state": "PROCESSING",
    }
    async_client = MagicMock()
    async_client.post = AsyncMock(return_value=async_resp)
    job_async = await async_op.execute_async(async_client)
    assert job_async.batch_id == "BATCH-ASYNC-1"

    ledger = JobLedger()
    recorded_ids = {item.batch_id for item in ledger.list_jobs("STUDY_REC")}
    assert {"BATCH-SYNC-1", "BATCH-ASYNC-1"} == recorded_ids

    # JobsEndpoint must NOT expose .list, and .get must refresh state & progress in JobLedger
    assert not hasattr(JobsEndpoint, "list")
    status_resp = MagicMock()
    status_resp.json.return_value = {
        "jobId": "J-SYNC-1",
        "batchId": "BATCH-SYNC-1",
        "state": "COMPLETED",
        "progress": 100,
    }
    ep_client = MagicMock()
    ep_client.get.return_value = status_resp
    jobs_ep = JobsEndpoint(ep_client)

    polled: JobStatus = jobs_ep.get("STUDY_REC", "BATCH-SYNC-1")
    assert polled.state == "COMPLETED"
    updated = ledger.get_job("STUDY_REC", "BATCH-SYNC-1")
    assert updated is not None
    assert updated.state == "COMPLETED"
    assert updated.progress == 100


def test_simulation_sdk_records_create_persists_to_job_ledger() -> None:
    """Verify sdk.records.create in simulation mode records the created batch_id in JobLedger."""
    sdk = ImednetSDK(simulation_mode=True)
    created_job = sdk.records.create("SIM_STUDY", [{"formKey": "DM", "data": {"SUBJ": "01"}}])
    assert created_job.batch_id
    persisted = JobLedger().get_job("SIM_STUDY", created_job.batch_id)
    assert persisted is not None
    assert persisted.state in {"COMPLETED", "PROCESSING", "PENDING"}


def test_jobs_list_cli_json_and_active_filter() -> None:
    """Verify `imednet jobs list <study_key>` outputs v1.0 JSON envelope and respects --active."""
    ledger = JobLedger()
    ledger.record_job("CLI_STUDY", "B-ACTIVE", job_id="J-1", state="PROCESSING", progress=60)
    ledger.record_job("CLI_STUDY", "B-DONE", job_id="J-2", state="COMPLETED", progress=100)

    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        cli.app(["jobs", "list", "CLI_STUDY", "--active", "--format", "json"])

    payload = json.loads(out.getvalue())
    assert payload["schemaVersion"] == "1.0"
    assert payload["count"] == 1
    assert payload["items"][0]["batchId"] == "B-ACTIVE"
    assert payload["items"][0]["progress"] == 60


def test_spi_datadict_and_jobs_exports() -> None:
    """Verify imednet.spi.datadict and imednet.spi.jobs expose complete public contracts."""
    assert callable(spi_jobs.JobLedger)
    assert callable(spi_jobs.record_study_job)
    assert callable(spi_datadict.load_from_api)
    assert callable(spi_datadict.load_from_csv)
    assert callable(spi_datadict.load_from_cdisc_odm)
    assert callable(spi_datadict.load_from_redcap)
    assert callable(spi_datadict.validate)
    assert callable(spi_datadict.get_schema)

    sdk = ImednetSDK(simulation_mode=True)
    studies = list(sdk.studies.list())
    dd = spi_datadict.load_from_api(sdk, studies[0].study_key)
    errors = spi_datadict.validate(dd, spi_datadict.get_schema())
    assert isinstance(dd, spi_datadict.DataDictionary)
    assert errors == []
