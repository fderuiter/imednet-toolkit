"""Tests for fail-closed record streaming, facade-only get_records adapters, and output rollback."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
import pytest
import respx

from imednet.errors import ServerError
from imednet.integrations.export import (
    TabularSinkConfig,
    export_to_csv,
    export_to_parquet,
)
from imednet.integrations.parquet import export_to_hive_parquet
from imednet.integrations.sink_base import iter_filtered_sdk_records
from imednet.models.forms import Form
from imednet.models.records import Record
from imednet.models.variables import Variable
from imednet.sdk import ImednetSDK
from imednet.utils.filters import build_filter_string, validate_filter_syntax


def _setup_two_page_503_study(router: respx.MockRouter) -> respx.Route:
    """Mock forms, variables, and a 2-page records stream where page 0 succeeds and page 1 returns 503."""
    router.get("/api/v1/edc/studies/STUDY/forms").respond(
        200,
        json={
            "data": [
                {"studyKey": "STUDY", "formId": 10, "formKey": "F_DM", "formName": "Demographics"},
            ]
        },
    )
    router.get("/api/v1/edc/studies/STUDY/variables").respond(
        200,
        json={
            "data": [
                {
                    "studyKey": "STUDY",
                    "variableId": 1,
                    "variableName": "AGE",
                    "label": "Age",
                    "formId": 10,
                    "formKey": "F_DM",
                }
            ]
        },
    )
    router.get("/api/v1/edc/studies/STUDY/sites").respond(200, json={"data": []})
    router.get("/api/v1/edc/studies/STUDY/intervals").respond(200, json={"data": []})
    router.get("/api/v1/edc/studies/STUDY/visits").respond(200, json={"data": []})

    def _records_page_responder(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "0"))
        if page == 0:
            return httpx.Response(
                200,
                json={
                    "metadata": {"status": "OK"},
                    "pagination": {
                        "currentPage": 0,
                        "size": 1,
                        "totalPages": 2,
                        "totalElements": 2,
                    },
                    "data": [
                        {
                            "studyKey": "STUDY",
                            "recordId": 101,
                            "recordOid": "REC-101",
                            "subjectKey": "SUBJ-01",
                            "visitId": 1,
                            "formId": 10,
                            "formKey": "F_DM",
                            "recordStatus": "Complete",
                            "deleted": False,
                            "dateCreated": "2026-06-01T10:00:00Z",
                            "dateModified": "2026-06-01T10:00:00Z",
                            "recordData": {"AGE": 42},
                        }
                    ],
                },
            )
        return httpx.Response(
            503,
            json={"metadata": {"status": "ERROR", "message": "Upstream service unavailable"}},
        )

    return router.get("/api/v1/edc/studies/STUDY/records").mock(side_effect=_records_page_responder)


def test_csv_and_parquet_exports_rollback_partial_outputs_and_preserve_prior_files(
    tmp_path: Path,
) -> None:
    """Mid-stream HTTP 503 leaves zero partial CSV/Parquet/manifest files and preserves existing targets."""
    pyarrow_pq = pytest.importorskip("pyarrow.parquet")
    base_url = "https://api.example.com"

    with respx.mock(base_url=base_url, assert_all_called=False) as router:
        _setup_two_page_503_study(router)
        with ImednetSDK(api_key="k", security_key="s", base_url=base_url, retries=0) as sdk:
            # 1. Fresh CSV + manifest: batch 0 streams on page 0, page 1 fails with 503 -> zero files left
            fresh_csv = tmp_path / "fresh.csv"
            fresh_manifest = tmp_path / "fresh.manifest.jsonl"
            with pytest.raises(ServerError):
                export_to_csv(
                    sdk,
                    "STUDY",
                    fresh_csv,
                    manifest_path=str(fresh_manifest),
                    batch_size=1,
                    max_retries=0,
                )
            assert not fresh_csv.exists()
            assert not fresh_manifest.exists()
            assert list(tmp_path.glob("*.tmp")) == []

            # 2. Pre-existing CSV + manifest + ledger: preserved byte-for-byte on mid-stream 503
            prior_csv = tmp_path / "prior.csv"
            prior_manifest = tmp_path / "prior.manifest.jsonl"
            prior_csv_bytes = b"recordId,AGE\n999,21\n"
            prior_manifest_bytes = b'{"batch_id":"prior/0","row_count":1}\n'
            prior_csv.write_bytes(prior_csv_bytes)
            prior_manifest.write_bytes(prior_manifest_bytes)

            for idempotent_flag in (True, False):
                with pytest.raises(ServerError):
                    export_to_csv(
                        sdk,
                        "STUDY",
                        prior_csv,
                        manifest_path=str(prior_manifest),
                        batch_size=1,
                        idempotent=idempotent_flag,
                        max_retries=0,
                    )
                assert prior_csv.read_bytes() == prior_csv_bytes
                assert prior_manifest.read_bytes() == prior_manifest_bytes
                assert list(tmp_path.glob("*.tmp")) == []

            # 3. Fresh Parquet: batch 0 streams on page 0, page 1 fails -> zero Parquet or temp files left
            fresh_pq = tmp_path / "fresh.parquet"
            with pytest.raises(ServerError):
                export_to_parquet(
                    sdk,
                    "STUDY",
                    fresh_pq,
                    batch_size=1,
                    max_retries=0,
                )
            assert not fresh_pq.exists()
            assert list(tmp_path.glob("*parquet*")) == []

            # 4. Pre-existing Parquet (both full and incremental): preserved intact on mid-stream 503
            prior_pq = tmp_path / "prior.parquet"
            pd.DataFrame({"recordId": [999], "AGE": [21]}).to_parquet(prior_pq, index=False)
            prior_pq_bytes = prior_pq.read_bytes()

            for incremental_flag in (False, True):
                with pytest.raises(ServerError):
                    export_to_parquet(
                        sdk,
                        "STUDY",
                        prior_pq,
                        batch_size=1,
                        incremental=incremental_flag,
                        ledger_path=str(tmp_path / "pq_ledger.json") if incremental_flag else None,
                        max_retries=0,
                    )
                assert prior_pq.read_bytes() == prior_pq_bytes
                assert pyarrow_pq.read_table(prior_pq).to_pydict() == {
                    "recordId": [999],
                    "AGE": [21],
                }
                assert list(tmp_path.glob("*.tmp")) == []
                assert list(tmp_path.glob("*.delta.parquet")) == []


def test_hive_parquet_rollback_removes_only_failed_batches_and_preserves_prior_partitions(
    tmp_path: Path,
) -> None:
    """Hive Parquet rollback deletes only failed _batch_<commit_id> dirs and preserves prior partitions."""
    pytest.importorskip("pyarrow")
    pyarrow_pq = pytest.importorskip("pyarrow.parquet")
    base_url = "https://api.example.com"

    with respx.mock(base_url=base_url, assert_all_called=False) as router:
        _setup_two_page_503_study(router)
        with ImednetSDK(api_key="k", security_key="s", base_url=base_url, retries=0) as sdk:
            # 1. Fresh Hive base_dir (default single-pass router path with chunk_size=1)
            fresh_hive = tmp_path / "fresh_hive"
            fresh_manifest = tmp_path / "fresh_hive.manifest.jsonl"
            with pytest.raises(ServerError):
                export_to_hive_parquet(
                    sdk,
                    "STUDY",
                    str(fresh_hive),
                    chunk_size=1,
                    manifest_path=str(fresh_manifest),
                    max_retries=0,
                )
            assert not fresh_hive.exists()
            assert not fresh_manifest.exists()

            # 2. Pre-existing Hive partitions across BOTH default path and streaming-options path
            existing_hive = tmp_path / "existing_hive"
            prior_dm_batch = existing_hive / "study_key=STUDY" / "form_key=F_DM" / "_batch_prior001"
            prior_ae_batch = existing_hive / "study_key=STUDY" / "form_key=F_AE" / "_batch_prior002"
            prior_dm_batch.mkdir(parents=True)
            prior_ae_batch.mkdir(parents=True)
            prior_dm_file = prior_dm_batch / "prior-0.parquet"
            prior_ae_file = prior_ae_batch / "prior-0.parquet"
            pd.DataFrame({"recordId": [801], "AGE": [64]}).to_parquet(prior_dm_file, index=False)
            pd.DataFrame({"recordId": [802], "AETERM": ["Headache"]}).to_parquet(
                prior_ae_file, index=False
            )
            prior_manifest = tmp_path / "existing_hive.manifest.jsonl"
            prior_manifest_content = b'{"batch_id":"STUDY/F_DM/prior","row_count":1}\n'
            prior_manifest.write_bytes(prior_manifest_content)

            for extra_opts in (
                {},
                {"incremental": True, "ledger_path": str(tmp_path / "hive_ledger.json")},
            ):
                with pytest.raises(ServerError):
                    export_to_hive_parquet(
                        sdk,
                        "STUDY",
                        str(existing_hive),
                        chunk_size=1,
                        manifest_path=str(prior_manifest),
                        max_retries=0,
                        **extra_opts,
                    )

                dm_batches = sorted(
                    p.name for p in (existing_hive / "study_key=STUDY" / "form_key=F_DM").iterdir()
                )
                ae_batches = sorted(
                    p.name for p in (existing_hive / "study_key=STUDY" / "form_key=F_AE").iterdir()
                )
                assert dm_batches == ["_batch_prior001"]
                assert ae_batches == ["_batch_prior002"]
                assert not (existing_hive / ".imednet_staging").exists()
                assert pyarrow_pq.ParquetFile(prior_dm_file).read().to_pydict() == {
                    "recordId": [801],
                    "AGE": [64],
                }
                assert prior_manifest.read_bytes() == prior_manifest_content


class _FacadeOnlyRecordsAdapter:
    """Adapter implementing facade get_* methods without .records, .forms, or .variables endpoints."""

    def __init__(self, *, fail_after_first: bool = False) -> None:
        self.fail_after_first = fail_after_first
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_forms(self, study_key: str, **_kwargs: Any) -> list[Form]:
        assert study_key == "STUDY"
        return [
            Form(form_id=10, form_key="F_DM", form_name="Demographics"),
            Form(form_id=20, form_key="F_VS", form_name="Vitals"),
        ]

    def get_variables(self, study_key: str, **_kwargs: Any) -> list[Variable]:
        assert study_key == "STUDY"
        return [
            Variable(variable_name="AGE", label="Age", form_id=10, form_key="F_DM"),
            Variable(variable_name="SYSBP", label="Systolic BP", form_id=20, form_key="F_VS"),
        ]

    def get_sites(self, _study_key: str, **_kwargs: Any) -> list[Any]:
        return []

    def get_intervals(self, _study_key: str, **_kwargs: Any) -> list[Any]:
        return []

    def get_visits(self, _study_key: str, **_kwargs: Any) -> list[Any]:
        return []

    def get_records(self, study_key: str, **filters: Any) -> Iterator[Record]:
        assert study_key == "STUDY"
        filter_copy = {k: v for k, v in filters.items() if k not in ("sort", "record_data_filter")}
        rendered = build_filter_string(filter_copy)
        validate_filter_syntax(rendered)
        assert not (";" in rendered and "," in rendered)
        self.calls.append((rendered, dict(filters)))

        def _gen() -> Iterator[Record]:
            yield Record(
                record_id=101,
                study_key="STUDY",
                subject_key="SUB1",
                visit_id=1,
                form_id=10,
                form_key="F_DM",
                record_status="Complete",
                deleted=False,
                date_modified=datetime(2026, 6, 10, 12, 0, tzinfo=UTC),
                record_data={"AGE": 35, "SYSBP": None},
            )
            if self.fail_after_first:
                raise ServerError("Adapter page 2 failed", status_code=503)
            yield Record(
                record_id=102,
                study_key="STUDY",
                subject_key="SUB2",
                visit_id=1,
                form_id=20,
                form_key="F_VS",
                record_status="Complete",
                deleted=False,
                date_modified=datetime(2026, 6, 25, 12, 0, tzinfo=UTC),
                record_data={"AGE": None, "SYSBP": 120},
            )

        return _gen()


def test_facade_only_get_records_adapter_branching_watermark_dedup_and_rollback(
    tmp_path: Path,
) -> None:
    """Facade-only get_records adapters support branching, watermark bounds, dedup, and rollback."""
    pytest.importorskip("pyarrow")
    adapter = _FacadeOnlyRecordsAdapter()
    assert not hasattr(adapter, "records")

    # 1. Direct iter_filtered_sdk_records: OR branches + watermark + upper bound post-filter + dedup
    records = list(
        iter_filtered_sdk_records(
            adapter,
            "STUDY",
            watermark=datetime(2026, 6, 1, tzinfo=UTC),
            record_filter={
                "formKey": ["F_DM", "F_VS"],
                "dateModified": ("<=", "2026-06-15T23:59:59Z"),
            },
            record_data_filter="AGE>=18",
        )
    )
    # Record 102 (2026-06-25) filtered out by upper bound post-predicate; 101 deduplicated across 2 branches
    assert [r.record_id for r in records] == [101]
    assert len(adapter.calls) == 2
    assert all(
        call_kwargs.get("record_data_filter") == "AGE>=18" for _, call_kwargs in adapter.calls
    )

    # 2. End-to-end export_to_csv with facade-only adapter and comma-separated OR record_filter
    adapter.calls.clear()
    csv_out = tmp_path / "adapter.csv"
    export_to_csv(
        adapter,  # type: ignore[arg-type]
        "STUDY",
        csv_out,
        config=TabularSinkConfig(
            study_key="STUDY",
            record_filter="formKey==F_DM,formKey==F_VS",
            batch_size=1,
        ),
    )
    exported_df = pd.read_csv(csv_out)
    assert sorted(exported_df["recordId"].tolist()) == [101, 102]
    assert len(adapter.calls) == 2

    # Non-idempotent CSV append onto existing file commits staged delta cleanly on success
    export_to_csv(
        adapter,  # type: ignore[arg-type]
        "STUDY",
        csv_out,
        config=TabularSinkConfig(
            study_key="STUDY",
            record_filter="formKey==F_DM",
            batch_size=1,
            idempotent=False,
        ),
    )
    appended_df = pd.read_csv(csv_out)
    assert len(appended_df) == 4

    # 3. Failing facade-only adapter propagates fail-closed and rolls back CSV, Parquet, and Hive outputs
    failing_adapter = _FacadeOnlyRecordsAdapter(fail_after_first=True)
    fail_csv = tmp_path / "fail_adapter.csv"
    fail_manifest = tmp_path / "fail_adapter.manifest.jsonl"
    with pytest.raises(ServerError, match="Adapter page 2 failed"):
        export_to_csv(
            failing_adapter,  # type: ignore[arg-type]
            "STUDY",
            fail_csv,
            manifest_path=str(fail_manifest),
            batch_size=1,
            max_retries=0,
        )
    assert not fail_csv.exists()
    assert not fail_manifest.exists()

    fail_hive = tmp_path / "fail_adapter_hive"
    with pytest.raises(ServerError, match="Adapter page 2 failed"):
        export_to_hive_parquet(
            failing_adapter,  # type: ignore[arg-type]
            "STUDY",
            str(fail_hive),
            chunk_size=1,
            max_retries=0,
        )
    assert not fail_hive.exists()

    # Filtered Hive Parquet streaming path (_build_form_export_contexts + _stream_records_across_forms)
    ok_hive = tmp_path / "ok_adapter_hive"
    export_to_hive_parquet(
        adapter,  # type: ignore[arg-type]
        "STUDY",
        str(ok_hive),
        record_filter="formKey==F_DM,formKey==F_VS",
        chunk_size=1,
    )
    assert (ok_hive / "study_key=STUDY" / "form_key=F_DM").exists()
    assert (ok_hive / "study_key=STUDY" / "form_key=F_VS").exists()

    fail_filtered_hive = tmp_path / "fail_filtered_adapter_hive"
    with pytest.raises(ServerError, match="Adapter page 2 failed"):
        export_to_hive_parquet(
            failing_adapter,  # type: ignore[arg-type]
            "STUDY",
            str(fail_filtered_hive),
            record_filter="formKey==F_DM,formKey==F_VS",
            chunk_size=1,
            max_retries=0,
        )
    assert not fail_filtered_hive.exists()
