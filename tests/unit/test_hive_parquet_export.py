"""Unit tests for hive parquet export."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest

import imednet.integrations.parquet as parquet_mod


def _read_partition_dataframe(path: Path) -> pd.DataFrame:
    """Helper function to  read partition dataframe."""
    parquet_files = sorted(path.glob("**/*.parquet"))
    assert parquet_files
    return pd.read_parquet(parquet_files[0], engine="pyarrow")


def test_export_to_hive_parquet_directory_structure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that export to hive parquet directory structure."""
    pytest.importorskip("pyarrow")

    sdk = MagicMock()
    sdk.forms.list.return_value = [SimpleNamespace(form_id=1, form_key="DEMOGRAPHICS")]
    sdk.variables.list.return_value = [SimpleNamespace(form_id=1, variable_name="age", label="Age")]

    class FakeMapper:
        """Test suite for FakeMapper."""

        def __init__(self, sdk: MagicMock) -> None:
            """Initialize the test object."""
            self._sdk = sdk

        def _build_record_model(
            self, variable_keys: list[str], label_map: dict[str, str]
        ) -> tuple[list[str], dict[str, str]]:
            """Helper function to  build record model."""
            return (variable_keys, label_map)

        def _fetch_records(self, _study_key: str, extra_filters: dict[str, Any]) -> list[int]:
            """Helper function to  fetch records."""
            return [extra_filters["formId"]]

        def _parse_records(
            self,
            records: list[int],
            _record_model: tuple[list[str], dict[str, str]],
        ) -> tuple[list[int], int]:
            """Helper function to  parse records."""
            return records, len(records)

        def _build_dataframe(
            self,
            rows: list[int],
            _variable_keys: list[str],
            _label_map: dict[str, str],
            _use_labels_as_columns: bool,
        ) -> pd.DataFrame:
            """Helper function to  build dataframe."""
            return pd.DataFrame([{"age": rows[0]}])

    monkeypatch.setattr(parquet_mod, "_record_mapper", lambda: FakeMapper)

    parquet_mod.export_to_hive_parquet(sdk, "STUDY_A", str(tmp_path))

    assert (tmp_path / "study_key=STUDY_A" / "form_key=DEMOGRAPHICS").exists()


def test_export_to_hive_parquet_concurrent_studies_no_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that export to hive parquet concurrent studies no conflict."""
    pytest.importorskip("pyarrow")

    sdk = MagicMock()
    sdk.forms.list.return_value = [SimpleNamespace(form_id=1, form_key="DEMOGRAPHICS")]
    sdk.variables.list.return_value = [
        SimpleNamespace(form_id=1, variable_name="study", label="Study")
    ]

    class FakeMapper:
        """Test suite for FakeMapper."""

        def __init__(self, sdk: MagicMock) -> None:
            """Initialize the test object."""
            self._sdk = sdk

        def _build_record_model(
            self, variable_keys: list[str], label_map: dict[str, str]
        ) -> tuple[list[str], dict[str, str]]:
            """Helper function to  build record model."""
            return (variable_keys, label_map)

        def _fetch_records(
            self, study_key: str, extra_filters: dict[str, Any] | None = None
        ) -> list[str]:
            """Helper function to  fetch records."""
            assert extra_filters is not None
            assert "formId" in extra_filters
            return [study_key]

        def _parse_records(
            self,
            records: list[str],
            _record_model: tuple[list[str], dict[str, str]],
        ) -> tuple[list[str], int]:
            """Helper function to  parse records."""
            return records, len(records)

        def _build_dataframe(
            self,
            rows: list[str],
            _variable_keys: list[str],
            _label_map: dict[str, str],
            _use_labels_as_columns: bool,
        ) -> pd.DataFrame:
            """Helper function to  build dataframe."""
            return pd.DataFrame([{"study": rows[0]}])

    monkeypatch.setattr(parquet_mod, "_record_mapper", lambda: FakeMapper)

    parquet_mod.export_to_hive_parquet(sdk, "STUDY_A", str(tmp_path))
    parquet_mod.export_to_hive_parquet(sdk, "STUDY_B", str(tmp_path))

    study_a = _read_partition_dataframe(tmp_path / "study_key=STUDY_A" / "form_key=DEMOGRAPHICS")
    study_b = _read_partition_dataframe(tmp_path / "study_key=STUDY_B" / "form_key=DEMOGRAPHICS")

    assert study_a.to_dict("records") == [{"study": "STUDY_A"}]
    assert study_b.to_dict("records") == [{"study": "STUDY_B"}]


def test_hive_parquet_query_returns_correct_string() -> None:
    """Test that hive parquet query returns correct string."""
    assert (
        parquet_mod.hive_parquet_query("/tmp/lake")
        == "SELECT * FROM read_parquet('/tmp/lake/**/*.parquet', "
        "hive_partitioning = true, union_by_name = true)"
    )


def test_export_to_hive_parquet_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that export to hive parquet import error."""

    def _raise_import_error(module_name: str):
        """Helper function to  raise import error."""
        if module_name == "pyarrow":
            raise ImportError("missing pyarrow")
        return __import__(module_name)

    monkeypatch.setattr(parquet_mod, "import_module", _raise_import_error)

    with pytest.raises(ImportError, match=r"pip install"):
        parquet_mod.export_to_hive_parquet(MagicMock(), "STUDY", "/tmp/lake")


def test_parquet_module_import_and_pruning_handle_missing_pandas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify importing imednet.integrations.parquet works without pandas and pruning raises clear ImportError."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "imednet.integrations._parquet_no_pandas_check",
        parquet_mod.__file__,
    )
    assert spec is not None and spec.loader is not None
    isolated_mod = importlib.util.module_from_spec(spec)
    isolated_mod.__package__ = "imednet.integrations"
    with monkeypatch.context() as mp:
        mp.setitem(__import__("sys").modules, "pandas", None)
        spec.loader.exec_module(isolated_mod)

    def _raise_pandas_import_error(module_name: str) -> Any:
        if module_name == "pandas":
            raise ImportError("missing pandas")
        return __import__(module_name)

    monkeypatch.setattr(parquet_mod, "import_module", _raise_pandas_import_error)
    with pytest.raises(ImportError, match=r"pandas is required.*pip install 'imednet\[export\]'"):
        parquet_mod._prune_hive_partition_record_ids(
            [tmp_path / "part-0.parquet"],
            {"101"},
            pyarrow_module=MagicMock(),
            storage_engine=MagicMock(),
            base_dir=str(tmp_path),
            study_key="STUDY",
            form_key="DEMO",
        )


@pytest.mark.performance
def test_export_to_hive_parquet_50_forms_5000_records_syncs_cache_once_and_masks_phi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify default export_to_hive_parquet syncs CachedRecordsLoader once (<=103 HTTP requests)."""
    pytest.importorskip("pyarrow")
    import httpx
    import respx

    from imednet.sdk import ImednetSDK
    from imednet_workflows import cached_loader as cached_loader_mod

    monkeypatch.setattr(cached_loader_mod, "DEFAULT_CACHE_DIR", tmp_path / "cache")

    forms_payload = [{"formId": i, "formKey": f"FORM_{i:02d}"} for i in range(1, 51)]
    variables_payload = [
        {
            "variableName": "patientName",
            "variableType": "text",
            "label": "Participant Name",
            "formId": i,
            "formKey": f"FORM_{i:02d}",
        }
        for i in range(1, 51)
    ]

    all_records: list[dict[str, Any]] = []
    for rec_idx in range(1, 5001):
        fid = ((rec_idx - 1) % 30) + 1
        all_records.append(
            {
                "recordId": rec_idx,
                "studyKey": "HIVE50",
                "subjectKey": f"SUBJ-{(rec_idx % 100) + 1:03d}",
                "visitId": 1,
                "formId": fid,
                "formKey": f"FORM_{fid:02d}",
                "recordStatus": "Complete",
                "deleted": False,
                "dateModified": "2026-06-01T00:00:00Z",
                "recordData": {"patientName": f"Patient {rec_idx}"},
            }
        )

    sync_calls = 0
    orig_sync = cached_loader_mod.CachedRecordsLoader.sync_records

    def _spy_sync(
        self_loader: cached_loader_mod.CachedRecordsLoader,
        study_key: str,
        *,
        reconcile: bool = True,
    ) -> None:
        nonlocal sync_calls
        sync_calls += 1
        return orig_sync(self_loader, study_key, reconcile=reconcile)

    monkeypatch.setattr(cached_loader_mod.CachedRecordsLoader, "sync_records", _spy_sync)

    http_calls = {"forms": 0, "variables": 0, "records": 0}
    record_filters: list[str] = []

    with respx.mock(assert_all_called=True) as router:

        def forms_handler(_req: httpx.Request) -> httpx.Response:
            http_calls["forms"] += 1
            return httpx.Response(200, json={"data": forms_payload})

        def vars_handler(_req: httpx.Request) -> httpx.Response:
            http_calls["variables"] += 1
            return httpx.Response(200, json={"data": variables_payload})

        def records_handler(req: httpx.Request) -> httpx.Response:
            http_calls["records"] += 1
            flt = req.url.params.get("filter", "")
            record_filters.append(flt)
            page = int(req.url.params.get("page", "0"))
            size = int(req.url.params.get("size", "100"))
            start = page * size
            page_slice = all_records[start : start + size]
            total_pages = (len(all_records) + size - 1) // size
            return httpx.Response(
                200,
                json={
                    "data": page_slice,
                    "pagination": {
                        "currentPage": page,
                        "size": size,
                        "totalPages": total_pages,
                        "totalElements": len(all_records),
                    },
                },
            )

        router.get("https://api.test/api/v1/edc/studies/HIVE50/forms").mock(
            side_effect=forms_handler
        )
        router.get("https://api.test/api/v1/edc/studies/HIVE50/variables").mock(
            side_effect=vars_handler
        )
        router.get("https://api.test/api/v1/edc/studies/HIVE50/records").mock(
            side_effect=records_handler
        )

        sdk = ImednetSDK(api_key="k", security_key="s", base_url="https://api.test")
        lake_masked = tmp_path / "lake_masked"

        parquet_mod.export_to_hive_parquet(
            sdk,
            "HIVE50",
            str(lake_masked),
            use_labels_as_columns=True,
            chunk_size=250,
            max_buffered_records=500,
        )

        assert sync_calls == 1
        assert sum(http_calls.values()) <= 103
        assert all("formId" not in flt for flt in record_filters)

        study_dir = lake_masked / "study_key=HIVE50"
        partition_dirs = sorted(p.name for p in study_dir.iterdir() if p.is_dir())
        assert partition_dirs == [f"form_key=FORM_{i:02d}" for i in range(1, 31)]
        assert not list(lake_masked.rglob("*.tmp*"))

        total_rows = 0
        for i in range(1, 31):
            part_files = sorted((study_dir / f"form_key=FORM_{i:02d}").glob("**/*.parquet"))
            part_df = pd.concat(
                [pd.read_parquet(pf, engine="pyarrow") for pf in part_files], ignore_index=True
            )
            total_rows += len(part_df)
            assert set(part_df["Participant Name"]) == {"***MASKED***"}
        assert total_rows == 5000

        # Opt-out mask_phi=False preserves raw values while still syncing cache at most once per export
        lake_unmasked = tmp_path / "lake_unmasked"
        parquet_mod.export_to_hive_parquet(
            sdk,
            "HIVE50",
            str(lake_unmasked),
            use_labels_as_columns=True,
            form_whitelist=[1],
            mask_phi=False,
        )
        assert sync_calls == 2
        unmasked_df = _read_partition_dataframe(
            lake_unmasked / "study_key=HIVE50" / "form_key=FORM_01"
        )
        assert "Patient 1" in set(unmasked_df["Participant Name"])


def test_export_to_hive_parquet_multi_form_single_pass_syncs_cache_once_and_masks_phi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify default export_to_hive_parquet syncs CachedRecordsLoader once across multiple forms."""
    pytest.importorskip("pyarrow")
    import httpx
    import respx

    from imednet.sdk import ImednetSDK
    from imednet_workflows import cached_loader as cached_loader_mod

    monkeypatch.setattr(cached_loader_mod, "DEFAULT_CACHE_DIR", tmp_path / "cache")

    forms_payload = [{"formId": i, "formKey": f"FORM_{i:02d}"} for i in range(1, 6)]
    variables_payload = [
        {
            "variableName": "patientName",
            "variableType": "text",
            "label": "Participant Name",
            "formId": i,
            "formKey": f"FORM_{i:02d}",
        }
        for i in range(1, 6)
    ]
    all_records = [
        {
            "recordId": idx,
            "studyKey": "HIVE5",
            "subjectKey": f"SUBJ-{(idx % 5) + 1:03d}",
            "visitId": 1,
            "formId": ((idx - 1) % 3) + 1,
            "formKey": f"FORM_{((idx - 1) % 3) + 1:02d}",
            "recordStatus": "Complete",
            "deleted": False,
            "dateModified": "2026-06-01T00:00:00Z",
            "recordData": {"patientName": f"Patient {idx}"},
        }
        for idx in range(1, 16)
    ]

    sync_calls = 0
    orig_sync = cached_loader_mod.CachedRecordsLoader.sync_records

    def _spy_sync(
        self_loader: cached_loader_mod.CachedRecordsLoader,
        study_key: str,
        *,
        reconcile: bool = True,
    ) -> None:
        nonlocal sync_calls
        sync_calls += 1
        return orig_sync(self_loader, study_key, reconcile=reconcile)

    monkeypatch.setattr(cached_loader_mod.CachedRecordsLoader, "sync_records", _spy_sync)
    record_filters: list[str] = []

    with respx.mock(assert_all_called=True) as router:
        router.get("https://api.test/api/v1/edc/studies/HIVE5/forms").mock(
            return_value=httpx.Response(200, json={"data": forms_payload})
        )
        router.get("https://api.test/api/v1/edc/studies/HIVE5/variables").mock(
            return_value=httpx.Response(200, json={"data": variables_payload})
        )

        def records_handler(req: httpx.Request) -> httpx.Response:
            record_filters.append(req.url.params.get("filter", ""))
            return httpx.Response(
                200,
                json={
                    "data": all_records,
                    "pagination": {
                        "currentPage": 0,
                        "size": 100,
                        "totalPages": 1,
                        "totalElements": len(all_records),
                    },
                },
            )

        router.get("https://api.test/api/v1/edc/studies/HIVE5/records").mock(
            side_effect=records_handler
        )

        sdk = ImednetSDK(api_key="k", security_key="s", base_url="https://api.test")
        lake_masked = tmp_path / "lake_masked"
        parquet_mod.export_to_hive_parquet(
            sdk,
            "HIVE5",
            str(lake_masked),
            use_labels_as_columns=True,
            chunk_size=5,
            max_buffered_records=6,
        )
        assert sync_calls == 1
        assert all("formId" not in flt for flt in record_filters)
        study_dir = lake_masked / "study_key=HIVE5"
        partition_dirs = sorted(p.name for p in study_dir.iterdir() if p.is_dir())
        assert partition_dirs == ["form_key=FORM_01", "form_key=FORM_02", "form_key=FORM_03"]
        for i in range(1, 4):
            part_df = _read_partition_dataframe(study_dir / f"form_key=FORM_{i:02d}")
            assert set(part_df["Participant Name"]) == {"***MASKED***"}

        lake_unmasked = tmp_path / "lake_unmasked"
        parquet_mod.export_to_hive_parquet(
            sdk,
            "HIVE5",
            str(lake_unmasked),
            use_labels_as_columns=True,
            form_whitelist=[1],
            mask_phi=False,
        )
        assert sync_calls == 2
        unmasked_df = _read_partition_dataframe(
            lake_unmasked / "study_key=HIVE5" / "form_key=FORM_01"
        )
        assert "Patient 1" in set(unmasked_df["Participant Name"])


def test_export_to_hive_parquet_validates_chunk_and_buffer_bounds(tmp_path: Path) -> None:
    """Verify invalid chunk_size or max_buffered_records fail fast and empty selection skips sync."""
    pytest.importorskip("pyarrow")
    sdk = MagicMock()
    sdk.forms.list.return_value = [SimpleNamespace(form_id=1, form_key="DEMO")]
    sdk.variables.list.return_value = []

    with pytest.raises(ValueError, match="chunk_size must be greater than 0"):
        parquet_mod.export_to_hive_parquet(sdk, "STUDY", str(tmp_path), chunk_size=0)

    with pytest.raises(ValueError, match="max_buffered_records must be a positive integer"):
        parquet_mod.export_to_hive_parquet(sdk, "STUDY", str(tmp_path), max_buffered_records=0)

    parquet_mod.export_to_hive_parquet(sdk, "STUDY", str(tmp_path), form_whitelist=[])
    sdk.records.list.assert_not_called()

    with pytest.raises(TypeError, match="unexpected keyword argument 'unknown_option'"):
        parquet_mod.export_to_hive_parquet(
            sdk,
            "STUDY",
            str(tmp_path),
            unknown_option=True,
        )


def test_export_to_hive_parquet_streaming_checkpoints_incremental_upsert_and_voided_reconcile(
    tmp_path: Path,
) -> None:
    """Verify export_to_hive_parquet supports all 8 streaming checkpoint options with single-pass incremental reconciliation."""
    pytest.importorskip("pyarrow")
    duckdb_mod = pytest.importorskip("duckdb")
    from datetime import UTC, datetime

    from imednet.models.forms import Form
    from imednet.models.records import Record
    from imednet.models.variables import Variable
    from imednet_workflows.state_ledger import FileStateProvider

    sdk = MagicMock()
    forms = [
        Form(form_id=1, form_key="DEMO", form_name="Demographics"),
        Form(form_id=2, form_key="VITALS", form_name="Vitals"),
    ]
    variables = [
        Variable(
            variable_name="AGE", label="Age", variable_type="integer", form_id=1, form_key="DEMO"
        ),
        Variable(
            variable_name="HR",
            label="Heart Rate",
            variable_type="integer",
            form_id=2,
            form_key="VITALS",
        ),
    ]
    sdk.forms.list.side_effect = lambda **_kw: iter(forms)
    sdk.variables.list.side_effect = lambda **_kw: iter(variables)
    sdk.get_variables.side_effect = AssertionError(
        "quality_gate_enabled should reuse pre-fetched study variables without refresh()"
    )

    initial_records = [
        Record(
            record_id=101,
            subject_key="SUBJ-1",
            visit_id=1,
            form_id=1,
            form_key="DEMO",
            date_modified="2026-06-01T00:00:00Z",
            record_data={"AGE": 40},
        ),
        Record(
            record_id=201,
            subject_key="SUBJ-1",
            visit_id=1,
            form_id=2,
            form_key="VITALS",
            date_modified="2026-06-01T00:00:00Z",
            record_data={"HR": 70},
        ),
        Record(
            record_id=202,
            subject_key="SUBJ-2",
            visit_id=1,
            form_id=2,
            form_key="VITALS",
            date_modified="2026-06-02T00:00:00Z",
            record_data={"HR": 82},
        ),
        # Low-readiness record (empty record_data) dropped by quality gate
        Record(
            record_id=299,
            subject_key="SUBJ-9",
            visit_id=1,
            form_id=2,
            form_key="VITALS",
            date_modified="2026-06-02T00:00:00Z",
            record_data={},
        ),
    ]
    sdk.records.list.side_effect = lambda **_kw: iter(initial_records)

    lake_dir = tmp_path / "hive_inc"
    ledger_path = tmp_path / "hive_ledger.json"

    parquet_mod.export_to_hive_parquet(
        sdk,
        "HIVE_INC",
        str(lake_dir),
        incremental=True,
        stream_name="lake",
        ledger_path=str(ledger_path),
        modified_since="2026-05-01T00:00:00Z",
        record_filter={"subjectKey": "SUBJ-1,SUBJ-2,SUBJ-9"},
        record_data_filter=None,
        include_deleted=False,
        quality_gate_enabled=True,
        min_schema_readiness_score=50.0,
        mask_phi=False,
    )

    provider = FileStateProvider(str(ledger_path))
    assert provider.get_last_timestamp("HIVE_INC", "lake_DEMO") == datetime(
        2026, 6, 1, 0, 0, tzinfo=UTC
    )
    assert provider.get_last_timestamp("HIVE_INC", "lake_VITALS") == datetime(
        2026, 6, 2, 0, 0, tzinfo=UTC
    )

    # Run 2: update DEMO record 101 and void VITALS record 201 in a single pass
    delta_records = [
        Record(
            record_id=101,
            subject_key="SUBJ-1",
            visit_id=1,
            form_id=1,
            form_key="DEMO",
            date_modified="2026-06-05T00:00:00Z",
            record_data={"AGE": 41},
        ),
        Record(
            record_id=201,
            subject_key="SUBJ-1",
            visit_id=1,
            form_id=2,
            form_key="VITALS",
            deleted=True,
            date_modified="2026-06-06T00:00:00Z",
            record_data={},
        ),
    ]
    sdk.records.list.reset_mock()
    sdk.records.list.side_effect = lambda **_kw: iter(delta_records)

    parquet_mod.export_to_hive_parquet(
        sdk,
        "HIVE_INC",
        str(lake_dir),
        incremental=True,
        stream_name="lake",
        state_provider=provider,
        include_deleted=False,
        quality_gate_enabled=True,
        min_schema_readiness_score=50.0,
        mask_phi=False,
    )

    assert sdk.records.list.call_count == 1
    assert provider.get_last_timestamp("HIVE_INC", "lake_DEMO") == datetime(
        2026, 6, 5, 0, 0, tzinfo=UTC
    )
    assert provider.get_last_timestamp("HIVE_INC", "lake_VITALS") == datetime(
        2026, 6, 6, 0, 0, tzinfo=UTC
    )

    query = parquet_mod.hive_parquet_query(str(lake_dir))
    with duckdb_mod.connect(":memory:") as dconn:
        rows = dconn.execute(
            f'SELECT "form_key", "recordId", "AGE", "HR" FROM ({query}) ORDER BY "form_key", "recordId"'
        ).fetchall()

    assert rows == [
        ("DEMO", 101, 41, None),
        ("VITALS", 202, None, 82),
    ]
