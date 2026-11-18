"""Unit tests for the export sink architecture (sink_base, graph, document, warehouse)."""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock

import pandas as pd
import pytest

import imednet.integrations.export as export_mod
import imednet.integrations.sink_base as sink_base_mod
from imednet.errors import ExportBatchError, ExportConfigurationError, ExportError
from imednet.integrations.export import TabularCSVSink, TabularSinkConfig, TabularSQLSink
from imednet.integrations.sink_base import (
    ExportSink,
    SinkConfig,
    _redact_uri,
    _require_optional_dep,
    iter_batches,
)

# ---------------------------------------------------------------------------
# _redact_uri
# ---------------------------------------------------------------------------


class TestRedactUri:
    """Test suite for RedactUri."""

    def test_redacts_user_and_password(self):
        """Test that redacts user and password."""
        userpass_uri = "mongodb://" + "user:pass" + "@localhost:27017/db"
        assert _redact_uri(userpass_uri) == "mongodb://***@localhost:27017/db"

    def test_redacts_user_only(self):
        """Test that redacts user only."""
        assert _redact_uri("bolt://neo4j@localhost:7687") == "bolt://***@localhost:7687"

    def test_leaves_uri_without_userinfo_unchanged(self):
        """Test that leaves uri without userinfo unchanged."""
        uri = "neo4j+s://bolt.example.com"
        assert _redact_uri(uri) == uri

    def test_handles_empty_string(self):
        """Test that handles empty string."""
        assert _redact_uri("") == ""


# ---------------------------------------------------------------------------
# _require_optional_dep
# ---------------------------------------------------------------------------


class TestRequireOptionalDep:
    """Test suite for RequireOptionalDep."""

    def test_returns_module_when_installed(self):
        """Test that returns module when installed."""
        mod = _require_optional_dep("sys", "dummy")
        assert mod is sys

    def test_raises_import_error_when_missing(self, monkeypatch):
        """Test that raises import error when missing."""

        def fake_import(name):
            """Helper function to fake import."""
            raise ModuleNotFoundError(name=name)

        monkeypatch.setattr(sink_base_mod, "import_module", fake_import)
        with pytest.raises(ImportError, match="imednet\\[mypkg\\]"):
            _require_optional_dep("mypkg", "mypkg")

    def test_reraises_unrelated_module_not_found_error(self, monkeypatch):
        """Test that reraises unrelated module not found error."""

        def fake_import(name):
            """Helper function to fake import."""
            raise ModuleNotFoundError(name="some_other_missing_lib")

        monkeypatch.setattr(sink_base_mod, "import_module", fake_import)
        with pytest.raises(ModuleNotFoundError):
            _require_optional_dep("mypkg", "mypkg")


# ---------------------------------------------------------------------------
# SinkConfig
# ---------------------------------------------------------------------------


class TestSinkConfig:
    """Test suite for SinkConfig."""

    def test_defaults(self):
        """Test that defaults."""
        cfg = SinkConfig(study_key="MY_STUDY")
        assert cfg.batch_size == 500
        assert cfg.max_retries == 3
        assert cfg.retry_backoff == 1.0
        assert cfg.idempotent is True
        assert cfg.extra == {}

    def test_custom_values(self):
        """Test that custom values."""
        cfg = SinkConfig(study_key="MY_STUDY", batch_size=100, max_retries=0, idempotent=False)
        assert cfg.batch_size == 100
        assert cfg.max_retries == 0
        assert cfg.idempotent is False


class TestIterBatches:
    """Test suite for IterBatches."""

    def test_splits_sequence_by_batch_size(self):
        """Test that splits sequence by batch size."""
        batches = list(iter_batches([1, 2, 3, 4, 5], 2))
        assert batches == [[1, 2], [3, 4], [5]]

    def test_rejects_non_positive_batch_size(self):
        """Test that rejects non positive batch size."""
        with pytest.raises(ValueError, match="batch_size"):
            list(iter_batches([1, 2], 0))


# ---------------------------------------------------------------------------
# ExportSink (concrete stub for testing)
# ---------------------------------------------------------------------------


class _StubSink(ExportSink):
    """Minimal concrete sink for testing the base class."""

    def __init__(self, config=None):
        """Initialize the test object."""
        super().__init__(config)
        self.batches: list[tuple[list, str]] = []
        self.flushed = False
        self.closed = False

    def write_batch(self, records, *, batch_id: str) -> int:
        """Helper function to write batch."""
        self.batches.append((list(records), batch_id))
        return len(records)

    def flush(self) -> None:
        """Helper function to flush."""
        self.flushed = True

    def close(self) -> None:
        """Helper function to close."""
        self.closed = True


class TestExportSinkContextManager:
    """Test suite for ExportSinkContextManager."""

    def test_flush_and_close_called_on_clean_exit(self):
        """Test that flush and close called on clean exit."""
        sink = _StubSink()
        with sink as s:
            s.write_batch([1, 2, 3], batch_id="b1")
        assert sink.flushed
        assert sink.closed

    def test_close_called_on_exception(self):
        """Test that close called on exception."""
        sink = _StubSink()
        with pytest.raises(ValueError), sink:
            raise ValueError("oops")
        # flush is NOT called on exception; close IS
        assert not sink.flushed
        assert sink.closed

    def test_write_batch_records_returned(self):
        """Test that write batch records returned."""
        sink = _StubSink()
        result = sink.write_batch([10, 20], batch_id="b2")
        assert result == 2
        assert sink.batches[0] == ([10, 20], "b2")


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class TestExportErrors:
    """Test suite for ExportErrors."""

    def test_export_error_is_imednet_error(self):
        """Test that export error is imednet error."""
        from imednet.errors import ImednetError

        assert issubclass(ExportError, ImednetError)

    def test_export_batch_error_carries_batch_id(self):
        """Test that export batch error carries batch id."""
        err = ExportBatchError("failed", batch_id="STUDY/FORM/0")
        assert err.batch_id == "STUDY/FORM/0"
        assert "failed" in str(err)

    def test_export_configuration_error_is_export_error(self):
        """Test that export configuration error is export error."""
        assert issubclass(ExportConfigurationError, ExportError)

    def test_export_batch_error_is_export_error(self):
        """Test that export batch error is export error."""
        assert issubclass(ExportBatchError, ExportError)


# ---------------------------------------------------------------------------
# Neo4jExportSink
# ---------------------------------------------------------------------------


def _fake_neo4j_module(fail_connect: bool = False) -> ModuleType:
    """Helper function to  fake neo4j module."""
    neo4j = ModuleType("neo4j")
    driver = MagicMock()
    if fail_connect:
        driver.verify_connectivity.side_effect = Exception("unreachable")
    session_ctx = MagicMock()
    session_ctx.__enter__ = MagicMock(return_value=MagicMock())
    session_ctx.__exit__ = MagicMock(return_value=False)
    driver.session.return_value = session_ctx
    neo4j.GraphDatabase = MagicMock()
    neo4j.GraphDatabase.driver.return_value = driver
    return neo4j, driver


# ---------------------------------------------------------------------------
# TabularCSVSink & TabularSQLSink Retry & Sanitization
# ---------------------------------------------------------------------------


class TestTabularCSVSinkRetryAndSanitization:
    """Test suite for TabularCSVSink retries, backoff, and error suppression."""

    @pytest.mark.parametrize("retries,expected_sleeps", [(0, []), (2, [0.25, 0.5])])
    def test_tabular_csv_sink_retries_and_backoff(self, monkeypatch, retries, expected_sleeps):
        """Assert configured retry attempts and exponential backoff on transient failure."""
        sleeps = []
        monkeypatch.setattr("tenacity.nap.time.sleep", sleeps.append)
        cfg = TabularSinkConfig(study_key="STUDY", max_retries=retries, retry_backoff=0.25)
        sink = TabularCSVSink("/fake/path.csv", cfg)

        call_count = 0

        def failing_open(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise OSError("disk IO error")

        monkeypatch.setattr(export_mod, "open", failing_open, raising=False)

        with pytest.raises(ExportBatchError, match=f"after {retries + 1} attempts") as caught:
            sink.write_batch([{"col": "val"}], batch_id="csv_batch_1")

        assert call_count == retries + 1
        assert sleeps == expected_sleeps
        assert caught.value.batch_id == "csv_batch_1"
        assert sink._is_first_batch is True

    def test_tabular_csv_sink_success_after_transient_error(self, monkeypatch):
        """Assert that successful execution stops retries and flips _is_first_batch."""
        sleeps = []
        monkeypatch.setattr("tenacity.nap.time.sleep", sleeps.append)
        cfg = TabularSinkConfig(study_key="STUDY", max_retries=3, retry_backoff=0.25)
        sink = TabularCSVSink("/fake/path.csv", cfg)

        mock_file = MagicMock()
        mock_ctx = MagicMock()
        mock_ctx.__enter__.return_value = mock_file
        mock_ctx.__exit__.return_value = False

        attempts = 0

        def flaky_open(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("temporary disk glitch")
            return mock_ctx

        monkeypatch.setattr(export_mod, "open", flaky_open, raising=False)

        loaded = sink.write_batch([{"col": "val"}], batch_id="csv_batch_success")
        assert loaded == 1
        assert attempts == 2
        assert sleeps == [0.25]
        assert sink._is_first_batch is False

    @pytest.mark.parametrize("error_type", [ExportConfigurationError, ValueError, TypeError])
    def test_tabular_csv_sink_permanent_failures_fail_immediately_and_redact(
        self, monkeypatch, caplog, error_type
    ):
        """Verify non-transient errors fail on first attempt without leaking driver messages."""
        sleeps = []
        monkeypatch.setattr("tenacity.nap.time.sleep", sleeps.append)
        tracer = MagicMock()
        sentinel = "secret-csv-credential-token"
        cfg = TabularSinkConfig(study_key="STUDY", max_retries=2, tracer=tracer)
        sink = TabularCSVSink("/fake/path.csv", cfg)

        call_count = 0

        def failing_open(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise error_type(sentinel)

        monkeypatch.setattr(export_mod, "open", failing_open, raising=False)

        with pytest.raises(ExportBatchError, match="after 1 attempts") as caught:
            sink.write_batch([{"col": "val"}], batch_id="csv_perm_fail")

        assert call_count == 1
        assert sleeps == []
        assert sentinel not in str(caught.value)
        assert sentinel not in caplog.text
        assert sentinel not in str(tracer.mock_calls)

    def test_tabular_csv_sink_transient_failure_redacted_before_tracing(self, monkeypatch, caplog):
        """Assert transient driver messages are redacted from error, log, and tracer."""
        monkeypatch.setattr("tenacity.nap.time.sleep", lambda _: None)
        tracer = MagicMock()
        sentinel = "secret-csv-transient-token"
        cfg = TabularSinkConfig(study_key="STUDY", max_retries=0, tracer=tracer)
        sink = TabularCSVSink("/fake/path.csv", cfg)

        def failing_open(*args, **kwargs):
            raise RuntimeError(sentinel)

        monkeypatch.setattr(export_mod, "open", failing_open, raising=False)

        with pytest.raises(ExportBatchError) as caught:
            sink.write_batch([{"col": "val"}], batch_id="csv_transient_redact")

        assert sentinel not in str(caught.value)
        assert sentinel not in caplog.text
        assert sentinel not in str(tracer.mock_calls)

    def test_tabular_csv_sink_empty_batch_returns_zero(self, monkeypatch):
        """Assert empty batches return 0 immediately without invoking retry or open."""
        cfg = TabularSinkConfig(study_key="STUDY")
        sink = TabularCSVSink("/fake/path.csv", cfg)
        failing_open = MagicMock(side_effect=AssertionError("open should not be called"))
        monkeypatch.setattr(export_mod, "open", failing_open, raising=False)

        assert sink.write_batch([], batch_id="b_empty_list") == 0
        assert sink.write_batch(pd.DataFrame(), batch_id="b_empty_df") == 0
        assert failing_open.call_count == 0


class TestTabularSQLSinkRetryAndSanitization:
    """Test suite for TabularSQLSink retries, backoff, and error suppression."""

    @pytest.mark.parametrize("retries,expected_sleeps", [(0, []), (2, [0.25, 0.5])])
    def test_tabular_sql_sink_retries_and_backoff(self, monkeypatch, retries, expected_sleeps):
        """Assert configured retry attempts and exponential backoff on transient failure."""
        sleeps = []
        monkeypatch.setattr("tenacity.nap.time.sleep", sleeps.append)
        cfg = TabularSinkConfig(study_key="STUDY", max_retries=retries, retry_backoff=0.25)
        sink = TabularSQLSink("tbl", MagicMock(), cfg)

        call_count = 0

        def failing_to_sql(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("database connection lost")

        monkeypatch.setattr(export_mod, "_to_sql_with_chunking", failing_to_sql)

        with pytest.raises(ExportBatchError, match=f"after {retries + 1} attempts") as caught:
            sink.write_batch([{"col": 1}], batch_id="sql_batch_1")

        assert call_count == retries + 1
        assert sleeps == expected_sleeps
        assert caught.value.batch_id == "sql_batch_1"
        assert sink._is_first_batch is True

    def test_tabular_sql_sink_success_after_transient_error(self, monkeypatch):
        """Assert that successful execution stops retries and flips _is_first_batch."""
        sleeps = []
        monkeypatch.setattr("tenacity.nap.time.sleep", sleeps.append)
        cfg = TabularSinkConfig(study_key="STUDY", max_retries=3, retry_backoff=0.25)
        sink = TabularSQLSink("tbl", MagicMock(), cfg)

        attempts = 0

        def flaky_to_sql(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary deadlock")

        monkeypatch.setattr(export_mod, "_to_sql_with_chunking", flaky_to_sql)

        loaded = sink.write_batch([{"col": 1}], batch_id="sql_batch_success")
        assert loaded == 1
        assert attempts == 2
        assert sleeps == [0.25]
        assert sink._is_first_batch is False

    @pytest.mark.parametrize("error_type", [ExportConfigurationError, ValueError, TypeError])
    def test_tabular_sql_sink_permanent_failures_fail_immediately_and_redact(
        self, monkeypatch, caplog, error_type
    ):
        """Verify non-transient errors fail on first attempt without leaking driver messages."""
        sleeps = []
        monkeypatch.setattr("tenacity.nap.time.sleep", sleeps.append)
        tracer = MagicMock()
        sentinel = "postgresql://user:password_secret@localhost:5432/db"
        cfg = TabularSinkConfig(study_key="STUDY", max_retries=2, tracer=tracer)
        sink = TabularSQLSink("tbl", MagicMock(), cfg)

        call_count = 0

        def failing_to_sql(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise error_type(sentinel)

        monkeypatch.setattr(export_mod, "_to_sql_with_chunking", failing_to_sql)

        with pytest.raises(ExportBatchError, match="after 1 attempts") as caught:
            sink.write_batch([{"col": 1}], batch_id="sql_perm_fail")

        assert call_count == 1
        assert sleeps == []
        assert sentinel not in str(caught.value)
        assert sentinel not in caplog.text
        assert sentinel not in str(tracer.mock_calls)

    def test_tabular_sql_sink_transient_failure_redacted_before_tracing(self, monkeypatch, caplog):
        """Assert transient driver messages are redacted from error, log, and tracer."""
        monkeypatch.setattr("tenacity.nap.time.sleep", lambda _: None)
        tracer = MagicMock()
        sentinel = "mysql://root:secret_mysql_pass@localhost/clinical"
        cfg = TabularSinkConfig(study_key="STUDY", max_retries=0, tracer=tracer)
        sink = TabularSQLSink("tbl", MagicMock(), cfg)

        def failing_to_sql(*args, **kwargs):
            raise RuntimeError(sentinel)

        monkeypatch.setattr(export_mod, "_to_sql_with_chunking", failing_to_sql)

        with pytest.raises(ExportBatchError) as caught:
            sink.write_batch([{"col": 1}], batch_id="sql_transient_redact")

        assert sentinel not in str(caught.value)
        assert sentinel not in caplog.text
        assert sentinel not in str(tracer.mock_calls)

    def test_tabular_sql_sink_empty_batch_returns_zero(self, monkeypatch):
        """Assert empty batches return 0 immediately without invoking retry or sql."""
        cfg = TabularSinkConfig(study_key="STUDY")
        sink = TabularSQLSink("tbl", MagicMock(), cfg)
        failing_to_sql = MagicMock(
            side_effect=AssertionError("_to_sql_with_chunking should not be called")
        )
        monkeypatch.setattr(export_mod, "_to_sql_with_chunking", failing_to_sql)

        assert sink.write_batch([], batch_id="sql_empty_list") == 0
        assert sink.write_batch(pd.DataFrame(), batch_id="sql_empty_df") == 0
        assert failing_to_sql.call_count == 0


# ---------------------------------------------------------------------------
# apply_quality_gate and spi.sinks
# ---------------------------------------------------------------------------


class TestApplyQualityGate:
    """Test suite for apply_quality_gate in sink_base."""

    def test_disabled_quality_gate_yields_all(self):
        """Assert disabled quality gate yields all records without filtering."""
        records = [{"record_id": "r1"}, {"record_id": "r2"}]
        cfg = SinkConfig(study_key="STUDY", quality_gate_enabled=False)
        result = list(sink_base_mod.apply_quality_gate(MagicMock(), "STUDY", records, cfg))
        assert result == records

    def test_enabled_quality_gate_filtering(self, monkeypatch, caplog):
        """Assert enabled quality gate filters records based on schema readiness score."""
        import logging

        caplog.set_level(logging.INFO)
        mock_validator_cls = MagicMock()
        mock_validator = MagicMock()
        mock_validator_cls.return_value = mock_validator
        mock_schema = MagicMock()
        mock_validator.schema = mock_schema

        def form_key_lookup(fid):
            if fid == 10:
                return "form_10"
            return None

        mock_schema.form_key_from_id.side_effect = form_key_lookup

        def mock_calc_readiness(schema, fk, data):
            if fk == "f_pass":
                return 100.0, []
            elif fk == "f_fail":
                return 40.0, ["Missing mandatory field"]
            elif fk == "form_10":
                return 95.0, []
            return 0.0, ["Unknown"]

        monkeypatch.setattr("imednet.validation.cache.SchemaValidator", mock_validator_cls)
        monkeypatch.setattr(
            "imednet.validation.cache.calculate_readiness_score", mock_calc_readiness
        )

        class ModelDumpRecord:
            def model_dump(self):
                return {"formKey": "f_pass", "data": {"a": 1}, "record_id": "rec_dump"}

        class AttrRecord:
            def __init__(self):
                self.form_key = "f_fail"
                self.form_id = None
                self.record_data = {"b": 2}
                self.record_id = "rec_attr"

        rec1 = ModelDumpRecord()
        rec2 = AttrRecord()
        rec3 = {"formId": 10, "data": None, "record_id": "rec_fid"}
        rec4 = {"data": {"c": 3}, "record_id": "rec_no_form"}
        rec5 = "not_a_dict_or_object"

        cfg = SinkConfig(
            study_key="STUDY", quality_gate_enabled=True, min_schema_readiness_score=80.0
        )
        res = list(
            sink_base_mod.apply_quality_gate(
                MagicMock(), "STUDY", [rec1, rec2, rec3, rec4, rec5], cfg
            )
        )

        assert res == [rec1, rec3]
        mock_validator.refresh.assert_called_once_with("STUDY")
        assert "Quality gate dropped 3 records in total." in caplog.text

    def test_quality_gate_accepts_ecrf_types_and_blocks_invalid_values(self):
        """Quality gate accepts RADIO, DROPDOWN, MEMO, CHECKBOX, DATE, and decimal NUMBER values."""
        from imednet.models.records import Record
        from imednet.models.variables import Variable

        sdk = MagicMock()
        sdk.get_variables.return_value = [
            Variable(variable_name="radio_v", variable_type="RADIO", form_id=1, form_key="F_ECRF"),
            Variable(
                variable_name="drop_v", variable_type="DROPDOWN", form_id=1, form_key="F_ECRF"
            ),
            Variable(variable_name="memo_v", variable_type="MEMO", form_id=1, form_key="F_ECRF"),
            Variable(variable_name="chk_v", variable_type="CHECKBOX", form_id=1, form_key="F_ECRF"),
            Variable(variable_name="date_v", variable_type="DATE", form_id=1, form_key="F_ECRF"),
            Variable(variable_name="num_v", variable_type="NUMBER", form_id=1, form_key="F_ECRF"),
        ]
        good = {
            "record_id": "r_good",
            "formKey": "F_ECRF",
            "recordData": {
                "radio_v": "A",
                "drop_v": "B",
                "memo_v": "Notes",
                "chk_v": False,
                "date_v": "2026-05-01",
                "num_v": 36.7,
            },
        }
        good_model = Record(
            record_id=2,
            form_key="F_ECRF",
            record_data={
                "radio_v": "A",
                "drop_v": "B",
                "memo_v": "Notes",
                "chk_v": True,
                "date_v": "2026-05-02",
                "num_v": 98.6,
            },
        )
        bad = {
            "record_id": "r_bad",
            "formKey": "F_ECRF",
            "recordData": {
                "radio_v": "A",
                "drop_v": "B",
                "memo_v": "Notes",
                "chk_v": "not-bool",
                "date_v": "05/01/2026",
                "num_v": True,
            },
        }
        cfg = SinkConfig(
            study_key="STUDY", quality_gate_enabled=True, min_schema_readiness_score=100.0
        )
        passed = list(sink_base_mod.apply_quality_gate(sdk, "STUDY", [good, good_model, bad], cfg))
        assert passed == [good, good_model]


class TestSpiSinks:
    """Test suite for imednet.spi.sinks re-exports and namespace modules."""

    def test_spi_redact_uri(self):
        """Assert spi redact_uri correctly redacts credentials."""
        import imednet.spi.sinks as spi_sinks

        assert spi_sinks.redact_uri("mongodb://u:p@localhost/db") == "mongodb://***@localhost/db"

    def test_spi_require_optional_dep(self):
        """Assert spi require_optional_dep resolves installed module."""
        import sys

        import imednet.spi.sinks as spi_sinks

        assert spi_sinks.require_optional_dep("sys", "core") is sys

    def test_http_namespace_import(self):
        """Assert imednet.http module is importable."""
        import imednet.http

        assert imednet.http.__all__ == []
