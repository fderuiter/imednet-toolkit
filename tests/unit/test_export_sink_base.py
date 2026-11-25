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
        assert cfg.mask_phi is True
        assert cfg.extra == {}

    def test_custom_values(self):
        """Test that custom values."""
        cfg = SinkConfig(
            study_key="MY_STUDY",
            batch_size=100,
            max_retries=0,
            idempotent=False,
            mask_phi=False,
        )
        assert cfg.batch_size == 100
        assert cfg.max_retries == 0
        assert cfg.idempotent is False
        assert cfg.mask_phi is False

    def test_rejects_non_boolean_mask_phi(self):
        """Test that non-boolean mask_phi raises ValueError."""
        with pytest.raises(ValueError, match="mask_phi must be a boolean"):
            SinkConfig(study_key="MY_STUDY", mask_phi="false")  # type: ignore[arg-type]


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

    def test_enabled_quality_gate_retains_valid_pydantic_records(self):
        """Assert apply_quality_gate scores Record.record_data and retains valid Record models."""
        from imednet.models.records import Record
        from imednet.models.variables import Variable

        sdk = MagicMock()
        sdk.get_variables.return_value = [
            Variable(
                variable_name="AGE",
                variable_type="int",
                form_id=10,
                form_key="DEMO",
            ),
            Variable(
                variable_name="SEX",
                variable_type="text",
                form_id=10,
                form_key="DEMO",
            ),
        ]

        valid_record = Record(
            record_id=1,
            form_id=10,
            form_key="DEMO",
            record_data={"AGE": 34, "SEX": "F"},
        )
        camel_dict_record = {
            "recordId": 2,
            "formKey": "DEMO",
            "recordData": {"AGE": 41, "SEX": "M"},
        }
        empty_record = Record(
            record_id=3,
            form_id=10,
            form_key="DEMO",
            record_data={},
        )

        cfg = SinkConfig(
            study_key="STUDY", quality_gate_enabled=True, min_schema_readiness_score=50.0
        )
        retained = list(
            sink_base_mod.apply_quality_gate(
                sdk, "STUDY", [valid_record, camel_dict_record, empty_record], cfg
            )
        )
        assert retained == [valid_record, camel_dict_record]

    def test_enabled_quality_gate_scores_clinical_categorical_and_memo_records_at_full_readiness(
        self,
    ):
        """Assert apply_quality_gate scores categorical, memo, and precision date records at 100.0."""
        from imednet.models.records import Record
        from imednet.models.variables import Variable
        from imednet.validation.cache import SchemaCache, calculate_readiness_score

        variables = [
            Variable(variable_name="V_CODED", variable_type="Coded", form_id=301, form_key="F_DM"),
            Variable(variable_name="V_RADIO", variable_type="Radio", form_id=301, form_key="F_DM"),
            Variable(
                variable_name="V_DROP", variable_type="Dropdown", form_id=301, form_key="F_DM"
            ),
            Variable(
                variable_name="V_SINGLE",
                variable_type="Single Select",
                form_id=301,
                form_key="F_DM",
            ),
            Variable(variable_name="V_YESNO", variable_type="Yes/No", form_id=301, form_key="F_DM"),
            Variable(
                variable_name="V_MULTI",
                variable_type="Multi Select",
                form_id=301,
                form_key="F_DM",
            ),
            Variable(
                variable_name="V_CHECK", variable_type="Checkbox", form_id=301, form_key="F_DM"
            ),
            Variable(variable_name="V_MEMO", variable_type="Memo", form_id=301, form_key="F_DM"),
            Variable(
                variable_name="V_DTP",
                variable_type="Date/Time Precision",
                form_id=301,
                form_key="F_DM",
            ),
            Variable(variable_name="V_AGE", variable_type="Number", form_id=301, form_key="F_DM"),
        ]
        sdk = MagicMock()
        sdk.get_variables.return_value = variables

        valid_payload = {
            "V_CODED": "Female",
            "V_RADIO": 1,
            "V_DROP": "2",
            "V_SINGLE": 3,
            "V_YESNO": 1,
            "V_MULTI": ["A", 2],
            "V_CHECK": True,
            "V_MEMO": "Baseline demographics verified.",
            "V_DTP": "2026-01",
            "V_AGE": 45,
        }
        cache = SchemaCache()
        cache.populate(variables)
        score, reasons = calculate_readiness_score(cache, "F_DM", valid_payload)
        assert score == pytest.approx(100.0)
        assert reasons == []

        valid_rec = Record(
            record_id=601,
            form_id=301,
            form_key="F_DM",
            record_data=valid_payload,
        )
        bool_on_num_rec = Record(
            record_id=602,
            form_id=301,
            form_key="F_DM",
            record_data={**valid_payload, "V_AGE": True},
        )
        cfg = SinkConfig(
            study_key="STUDY", quality_gate_enabled=True, min_schema_readiness_score=100.0
        )
        retained = list(
            sink_base_mod.apply_quality_gate(sdk, "STUDY", [valid_rec, bool_on_num_rec], cfg)
        )
        assert retained == [valid_rec]


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

    def test_spi_normalize_sink_value(self):
        """Assert normalize_sink_value dumps Keyword models, Enum values, and tuples while preserving Date/Decimal."""
        from datetime import UTC, date, datetime
        from decimal import Decimal
        from enum import Enum

        import imednet.spi.sinks as spi_sinks
        from imednet.models.records import Keyword, RecordStatus

        class CustomCode(Enum):
            ALPHA = 10

        class LegacyStubModel:
            def model_dump(self):
                return {"stub": ("x", RecordStatus.LOCKED)}

        assert "normalize_sink_value" in spi_sinks.__all__

        kw = Keyword(
            keyword_name="Safety",
            keyword_key="SAFETY",
            keyword_id=7,
            date_added=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        )
        raw = {
            "status": RecordStatus.COMPLETE,
            "code": CustomCode.ALPHA,
            RecordStatus.SIGNED: ("tuple_item", kw, LegacyStubModel()),
            "lab_decimal": Decimal("0.0042"),
            "visit_date": date(2026, 5, 1),
        }

        normalized = spi_sinks.normalize_sink_value(raw)
        assert normalized["status"] == "Complete"
        assert normalized["code"] == 10
        assert normalized["Signed"][0] == "tuple_item"
        assert isinstance(normalized["Signed"], list)
        assert normalized["Signed"][1] == {
            "keyword_name": "Safety",
            "keyword_key": "SAFETY",
            "keyword_id": 7,
            "date_added": "2026-05-01T12:00:00Z",
        }
        assert normalized["Signed"][2] == {"stub": ["x", "Locked"]}
        assert normalized["lab_decimal"] == Decimal("0.0042")
        assert isinstance(normalized["lab_decimal"], Decimal)
        assert normalized["visit_date"] == date(2026, 5, 1)

        aliased = spi_sinks.normalize_sink_value([kw], by_alias=True)
        assert aliased == [
            {
                "keywordName": "Safety",
                "keywordKey": "SAFETY",
                "keywordId": 7,
                "dateAdded": "2026-05-01T12:00:00Z",
            }
        ]


class TestDirectTabularSinkPhiRedaction:
    """Verify direct TabularCSVSink.write_batch and TabularSQLSink.write_batch PHI redaction."""

    def test_tabular_csv_sink_write_batch_masks_dataframe_and_sanitizes_without_mutating_caller(
        self, tmp_path
    ):
        """Direct TabularCSVSink.write_batch masks PHI columns, sanitizes formulas, and preserves caller DataFrame."""
        csv_path = tmp_path / "direct_df.csv"
        cfg = TabularSinkConfig(study_key="STUDY1", sanitize=True)
        input_df = pd.DataFrame(
            [
                {
                    "record_id": 101,
                    "subject_key": "SUBJ-001",
                    "study_key": "STUDY1",
                    "DOB": "1985-04-12",
                    "SSN": "123-45-6789",
                    "patientName": "Jane Doe",
                    "systolic_bp": 118,
                    "notes": "=SUM(1,2)",
                }
            ]
        )

        with TabularCSVSink(str(csv_path), cfg) as sink:
            written = sink.write_batch(input_df, batch_id="STUDY1/csv/0")

        assert written == 1
        # Caller DataFrame must remain unmutated
        assert input_df["DOB"].iloc[0] == "1985-04-12"
        assert input_df["notes"].iloc[0] == "=SUM(1,2)"

        out_df = pd.read_csv(csv_path)
        assert out_df["subject_key"].iloc[0] == "SUBJ-001"
        assert out_df["study_key"].iloc[0] == "STUDY1"
        assert out_df["record_id"].iloc[0] == 101
        assert out_df["systolic_bp"].iloc[0] == 118
        assert out_df["DOB"].iloc[0] == "***MASKED***"
        assert out_df["SSN"].iloc[0] == "***MASKED***"
        assert out_df["patientName"].iloc[0] == "***MASKED***"
        assert out_df["notes"].iloc[0] == "'=SUM(1,2)"

    def test_tabular_csv_sink_write_batch_masks_record_dicts_and_models_and_honors_opt_out(
        self, tmp_path
    ):
        """Direct TabularCSVSink.write_batch redacts flat dicts, nested record_data dicts, and Record models."""
        from imednet.models.records import Record

        flat_csv = tmp_path / "flat_dicts.csv"
        flat_records = [
            {
                "record_id": 1,
                "subject_key": "SUBJ-10",
                "DOB": "1990-01-01",
                "SSN": "123-45-6789",
                "patientName": "John Roe",
                "heart_rate": 72,
            }
        ]
        with TabularCSVSink(str(flat_csv), TabularSinkConfig(study_key="STUDY1")) as sink:
            assert sink.write_batch(flat_records, batch_id="b0") == 1

        flat_out = pd.read_csv(flat_csv)
        assert flat_out["subject_key"].iloc[0] == "SUBJ-10"
        assert flat_out["heart_rate"].iloc[0] == 72
        assert flat_out["DOB"].iloc[0] == "***MASKED***"
        assert flat_out["SSN"].iloc[0] == "***MASKED***"
        assert flat_out["patientName"].iloc[0] == "***MASKED***"

        model_csv = tmp_path / "models.csv"
        rec_model = Record(
            record_id=2,
            study_key="STUDY1",
            subject_key="SUBJ-20",
            visit_id=1,
            form_id=10,
            record_data={
                "DOB": "1979-11-30",
                "SSN": "999-00-1111",
                "patientName": "Alice Smith",
                "heart_rate": 64,
            },
        )
        with TabularCSVSink(str(model_csv), TabularSinkConfig(study_key="STUDY1")) as sink:
            assert sink.write_batch([rec_model], batch_id="b1") == 1

        assert rec_model.record_data["patientName"] == "Alice Smith"
        model_out = pd.read_csv(model_csv)
        assert model_out["subject_key"].iloc[0] == "SUBJ-20"
        assert model_out["heart_rate"].iloc[0] == 64
        assert model_out["DOB"].iloc[0] == "***MASKED***"
        assert model_out["SSN"].iloc[0] == "***MASKED***"
        assert model_out["patientName"].iloc[0] == "***MASKED***"

        opt_out_csv = tmp_path / "opt_out.csv"
        with TabularCSVSink(
            str(opt_out_csv), TabularSinkConfig(study_key="STUDY1", mask_phi=False)
        ) as sink:
            assert sink.write_batch(flat_records, batch_id="b2") == 1
        opt_out_df = pd.read_csv(opt_out_csv)
        assert opt_out_df["patientName"].iloc[0] == "John Roe"
        assert opt_out_df["SSN"].iloc[0] == "123-45-6789"

    def test_tabular_sql_sink_write_batch_masks_dataframe_and_record_sequences(self, tmp_path):
        """Direct TabularSQLSink.write_batch masks PHI for DataFrames, Record dicts, and Record models."""
        from sqlalchemy import create_engine, text

        from imednet.models.records import Record

        db_path = tmp_path / "direct_sink.sqlite"
        engine = create_engine(f"sqlite:///{db_path}")

        df_in = pd.DataFrame(
            [
                {
                    "record_id": 10,
                    "subject_key": "SUBJ-SQL-1",
                    "DOB": "1982-06-15",
                    "SSN": "123-45-6789",
                    "patientName": "Jane SQL",
                    "lab_value": 4.5,
                    "comment": "@malicious",
                }
            ]
        )
        cfg = TabularSinkConfig(study_key="STUDY1", sanitize=True)
        with TabularSQLSink("df_table", engine, cfg) as sink:
            assert sink.write_batch(df_in, batch_id="sql/df/0") == 1

        nested_seq = [
            {
                "record_id": 11,
                "subject_key": "SUBJ-SQL-2",
                "record_data": {
                    "DOB": "1975-03-09",
                    "SSN": "555-66-7777",
                    "patientName": "Bob SQL",
                    "lab_value": 7,
                },
            },
            Record(
                record_id=12,
                subject_key="SUBJ-SQL-3",
                record_data={
                    "DOB": "1995-08-22",
                    "SSN": "888-77-6666",
                    "patientName": "Carol SQL",
                    "lab_value": 9,
                },
            ),
        ]
        with TabularSQLSink("seq_table", engine, TabularSinkConfig(study_key="STUDY1")) as sink:
            assert sink.write_batch(nested_seq, batch_id="sql/seq/0") == 2

        with engine.connect() as conn:
            df_row = (
                conn.execute(
                    text(
                        "SELECT record_id, subject_key, DOB, SSN, patientName, lab_value, comment FROM df_table"
                    )
                )
                .mappings()
                .one()
            )
            seq_rows = (
                conn.execute(
                    text(
                        "SELECT record_id, subject_key, DOB, SSN, patientName, lab_value "
                        "FROM seq_table ORDER BY record_id"
                    )
                )
                .mappings()
                .all()
            )

        assert df_row["record_id"] == 10
        assert df_row["subject_key"] == "SUBJ-SQL-1"
        assert df_row["DOB"] == "***MASKED***"
        assert df_row["SSN"] == "***MASKED***"
        assert df_row["patientName"] == "***MASKED***"
        assert df_row["lab_value"] == pytest.approx(4.5)
        assert df_row["comment"] == "'@malicious"

        assert len(seq_rows) == 2
        assert seq_rows[0]["subject_key"] == "SUBJ-SQL-2"
        assert seq_rows[0]["DOB"] == "***MASKED***"
        assert seq_rows[0]["SSN"] == "***MASKED***"
        assert seq_rows[0]["patientName"] == "***MASKED***"
        assert seq_rows[0]["lab_value"] == 7
        assert seq_rows[1]["subject_key"] == "SUBJ-SQL-3"
        assert seq_rows[1]["patientName"] == "***MASKED***"
        assert seq_rows[1]["lab_value"] == 9
        engine.dispose()


class TestExportSinkBatchRedactionContract:
    """Verify custom ExportSink subclasses inherit default PHI redaction from the shared SPI contract."""

    def test_custom_export_sink_inherits_default_phi_redaction_and_honors_opt_out(self):
        """Custom ExportSink extensions automatically redact batches unless mask_phi=False."""
        from types import SimpleNamespace

        import imednet.spi.sink_base as spi_sink_base
        import imednet.spi.sinks as spi_sinks
        from imednet.models.records import Record

        class CustomAuditSink(spi_sinks.ExportSink):
            def __init__(self, config: spi_sinks.SinkConfig) -> None:
                super().__init__(config)
                self.captured_batches: list[Any] = []

            def write_batch(self, records, *, batch_id: str) -> int:
                self.captured_batches.append((records, batch_id))
                return len(records)

            def flush(self) -> None:
                pass

            def close(self) -> None:
                pass

        clean_record = Record(
            record_id=1,
            subject_key="SUBJ-CLEAN",
            record_data={"heart_rate": 70},
        )
        phi_record = Record(
            record_id=2,
            subject_key="SUBJ-PHI",
            record_data={"patientName": "Jane Doe", "SSN": "123-45-6789", "heart_rate": 75},
        )
        ns_record = SimpleNamespace(
            record_id=3,
            subject_key="SUBJ-NS",
            record_data={"DOB": "1980-01-01", "heart_rate": 80},
        )
        dict_record = {
            "record_id": 4,
            "subject_key": "SUBJ-DICT",
            "DOB": "1991-02-03",
            "heart_rate": 65,
        }

        default_sink = CustomAuditSink(spi_sinks.SinkConfig(study_key="STUDY1"))
        count = default_sink.write_batch(
            (clean_record, phi_record, ns_record, dict_record),
            batch_id="audit/0",
        )
        assert count == 4

        redacted_batch, captured_id = default_sink.captured_batches[0]
        assert captured_id == "audit/0"
        assert isinstance(redacted_batch, tuple)
        # Clean record preserves identity when no PHI fields require masking
        assert redacted_batch[0] is clean_record
        # Caller inputs are not mutated
        assert phi_record.record_data["patientName"] == "Jane Doe"
        assert ns_record.record_data["DOB"] == "1980-01-01"
        assert dict_record["DOB"] == "1991-02-03"

        assert redacted_batch[1].subject_key == "SUBJ-PHI"
        assert redacted_batch[1].record_data == {
            "patientName": "***MASKED***",
            "SSN": "***MASKED***",
            "heart_rate": 75,
        }
        assert redacted_batch[2].subject_key == "SUBJ-NS"
        assert redacted_batch[2].record_data == {"DOB": "***MASKED***", "heart_rate": 80}
        assert redacted_batch[3] == {
            "record_id": 4,
            "subject_key": "SUBJ-DICT",
            "DOB": "***MASKED***",
            "heart_rate": 65,
        }

        # Direct SPI helper also works on DataFrames and honors mask_phi=False
        df_batch = pd.DataFrame([{"subject_key": "SUBJ-DF", "SSN": "123-45-6789", "score": 10}])
        redacted_df = spi_sink_base.redact_batch_records(df_batch)
        assert redacted_df["subject_key"].iloc[0] == "SUBJ-DF"
        assert redacted_df["SSN"].iloc[0] == "***MASKED***"
        assert redacted_df["score"].iloc[0] == 10
        assert df_batch["SSN"].iloc[0] == "123-45-6789"

        opt_out_sink = CustomAuditSink(spi_sinks.SinkConfig(study_key="STUDY1", mask_phi=False))
        opt_out_sink.write_batch([phi_record], batch_id="audit/1")
        assert opt_out_sink.captured_batches[0][0][0].record_data["patientName"] == "Jane Doe"


class TestQualityGatePrefetchedVariablesAndBufferRouter:
    """Tests for reusable Study Variables in apply_quality_gate and BoundedFormBufferRouter."""

    def test_apply_quality_gate_reuses_prefetched_variables_without_refresh(self):
        """Assert apply_quality_gate populates schema from prefetched variables without SDK calls."""
        from types import SimpleNamespace

        from imednet.models.records import Record
        from imednet.models.variables import Variable

        sdk = MagicMock()
        sdk.variables.list.side_effect = AssertionError("must not fetch /variables again")
        sdk.get_variables.side_effect = AssertionError("must not fetch /variables again")

        forms = [
            SimpleNamespace(form_id=10, form_key="DEMO"),
            SimpleNamespace(form_id=20, form_key="VITALS"),
        ]
        variables = [
            Variable(variable_name="AGE", variable_type="int", form_id=10, form_key=""),
            SimpleNamespace(variable_name="HR", variable_type="int", form_id=20, form_key=None),
        ]

        valid_demo = Record(
            record_id=1,
            form_id=10,
            form_key="",
            record_data={"AGE": 52},
        )
        invalid_vitals = Record(
            record_id=2,
            form_id=20,
            form_key="VITALS",
            record_data={},
        )
        valid_vitals = Record(
            record_id=3,
            form_id=20,
            form_key="",
            record_data={"HR": 68},
        )

        cfg = SinkConfig(
            study_key="STUDY_REUSE",
            quality_gate_enabled=True,
            min_schema_readiness_score=100.0,
        )
        retained = list(
            sink_base_mod.apply_quality_gate(
                sdk,
                "STUDY_REUSE",
                [valid_demo, invalid_vitals, valid_vitals],
                cfg,
                variables=variables,
                forms=forms,
            )
        )
        assert retained == [valid_demo, valid_vitals]

    def test_apply_quality_gate_prefetched_variables_enforces_required_flag(self):
        """Assert prefetched Variable(required=True) preserves required policy on cloned variables without SDK calls."""
        from types import SimpleNamespace

        from imednet.models.records import Record
        from imednet.models.variables import Variable

        sdk = MagicMock()
        sdk.variables.list.side_effect = AssertionError("must not fetch /variables again")
        sdk.get_variables.side_effect = AssertionError("must not fetch /variables again")

        forms = [SimpleNamespace(form_id=10, form_key="DEMO")]
        variables = [
            Variable(
                variable_name="SUBJ_CODE",
                variable_type="text",
                form_id=10,
                form_key="",
                required=True,
            ),
            Variable(
                variable_name="AGE",
                variable_type="int",
                form_id=10,
                form_key="",
            ),
        ]

        complete_rec = Record(
            record_id=1,
            form_id=10,
            form_key="",
            record_data={"SUBJ_CODE": "S-001", "AGE": 44},
        )
        blank_required_rec = Record(
            record_id=2,
            form_id=10,
            form_key="DEMO",
            record_data={"SUBJ_CODE": "", "AGE": 44},
        )
        none_required_rec = Record(
            record_id=3,
            form_id=10,
            form_key="",
            record_data={"SUBJ_CODE": None, "AGE": 44},
        )

        cfg = SinkConfig(
            study_key="STUDY_REQ",
            quality_gate_enabled=True,
            min_schema_readiness_score=100.0,
        )
        retained = list(
            sink_base_mod.apply_quality_gate(
                sdk,
                "STUDY_REQ",
                [complete_rec, blank_required_rec, none_required_rec],
                cfg,
                variables=variables,
                forms=forms,
            )
        )
        assert retained == [complete_rec]

    def test_bounded_form_buffer_router_and_form_ref_extraction(self):
        """Verify BoundedFormBufferRouter flushes on batch_size and global memory ceiling."""
        from types import SimpleNamespace

        with pytest.raises(ValueError, match="max_buffered_records must be a positive integer"):
            SinkConfig(study_key="S", max_buffered_records=0)
        with pytest.raises(ValueError, match="max_buffered_records must be a positive integer"):
            SinkConfig(study_key="S", max_buffered_records=True)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="batch_size must be greater than 0"):
            sink_base_mod.BoundedFormBufferRouter(
                ["F1"], batch_size=0, on_flush_active=lambda *_: None
            )

        assert sink_base_mod.extract_record_form_ref({"formId": "42", "formKey": " AE "}) == (
            42,
            "AE",
        )
        assert sink_base_mod.extract_record_form_ref({"form_id": True, "form_key": ""}) == (
            None,
            None,
        )
        assert sink_base_mod.extract_record_form_ref(SimpleNamespace(formId=7, formKey="CM")) == (
            7,
            "CM",
        )

        active_flushes: list[tuple[str, list[int], int]] = []
        voided_flushes: list[tuple[str, list[int], int]] = []

        router = sink_base_mod.BoundedFormBufferRouter(
            ["F1", "F2", "F3"],
            batch_size=10,
            max_buffered_records=4,
            on_flush_active=lambda fk, b, idx: active_flushes.append((fk, b, idx)),
            on_flush_voided=lambda fk, b, idx: voided_flushes.append((fk, b, idx)),
        )

        # Add 2 items to F1, 1 to F2, then 1 voided to F3 -> total hits ceiling=4 -> evicts F1 (len 2)
        router.add_active("F1", 101)
        router.add_active("F1", 102)
        router.add_active("F2", 201)
        router.add_voided("F3", 301)

        assert router.peak_buffered == 4
        assert router.total_buffered == 2
        assert active_flushes == [("F1", [101, 102], 0)]

        # Add 2 more voided to F3 -> F3 voided reaches 3 while F2 active is 1 -> total hits 4 -> evicts F3 voided
        router.add_voided("F3", 302)
        router.add_voided("F3", 303)
        assert voided_flushes == [("F3", [301, 302, 303], 0)]
        assert router.total_buffered == 1

        router.flush_all()
        assert active_flushes == [("F1", [101, 102], 0), ("F2", [201], 0)]
        assert router.total_buffered == 0


class TestStudyConfigurationEnforcementInTabularAndBaseSinks:
    """Regression suite for StudyConfiguration enforcement in Tabular*Sink and ExportSink."""

    @staticmethod
    def _commit_study_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Any, cfg: Any) -> Any:
        from imednet_workflows.config_version_control import ConfigVersionStore

        db_file = tmp_path / "config_store" / "versions.sqlite3"
        monkeypatch.setenv("IMEDNET_CONFIG_DB_PATH", str(db_file))
        monkeypatch.setenv(
            "IMEDNET_ENCRYPTION_KEY", "test-sink-study-cfg-secret"
        )  # pragma: allowlist secret
        monkeypatch.delenv("IMEDNET_SECURITY_KEY", raising=False)

        admin_sdk = MagicMock()
        admin_sdk.auth.get_user_roles.return_value = ["admin"]
        admin_sdk.auth.get_user_id.return_value = "governance-tester"

        store = ConfigVersionStore(db_path=db_file)
        store.commit_config(cfg.study_key, cfg, "Commit test study rules", sdk=admin_sdk)
        return store

    def test_tabular_sinks_enforce_committed_study_config_across_all_input_shapes(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Any,
    ) -> None:
        """All four Tabular*Sink classes enforce phi_fields, terminology, and mappings."""
        import duckdb
        from sqlalchemy import create_engine

        from imednet.integrations.export import TabularDuckDBSink, TabularParquetSink
        from imednet.models.records import Record
        from imednet.models.study_config import MappingRule, StudyConfiguration

        study_cfg = StudyConfiguration(
            study_key="STUDY_TAB_GOV",
            phi_fields=["customChartId", "medicalRecordNumber", "subject_key"],
            terminology_lookups={
                "AESEV": {"1": "MILD", "2": "MODERATE"},
                "vitalStatus": {"1": "ALIVE", "2": "DECEASED"},
            },
            mappings=[
                MappingRule(
                    domain="AE",
                    targetField="normalizedTerm",
                    sourceFormKey="AE",
                    sourceVariableName="rawTerm",
                    businessLogic="str(value).strip().upper()",
                ),
                MappingRule(
                    domain="AE",
                    targetField="doseMg",
                    sourceFormKey="AE",
                    sourceVariableName="doseMg",
                    businessLogic="value * 2",
                ),
                MappingRule(
                    domain="VS",
                    targetField="record_id",
                    sourceFormKey="VS",
                    sourceVariableName="collidingField",
                ),
            ],
        )
        self._commit_study_config(monkeypatch, tmp_path, study_cfg)

        rec_model = Record(
            record_id=101,
            study_key="STUDY_TAB_GOV",
            subject_key="SUBJ-101",
            visit_id=1,
            form_id=10,
            record_data={
                "customChartId": "CH-101",
                "medicalRecordNumber": "MRN-101",
                "SSN": "123-45-6789",
                "AESEV": "1",
                "vitalStatus": "2",
                "rawTerm": "  headache ",
                "doseMg": 25,
                "collidingField": 99999,
            },
        )
        envelope_dict = {
            "record_id": 102,
            "study_key": "STUDY_TAB_GOV",
            "subject_key": "SUBJ-102",
            "visit_id": 2,
            "form_id": 10,
            "recordData": {
                "customChartId": "CH-102",
                "medicalRecordNumber": "MRN-102",
                "SSN": "999-00-1111",
                "AESEV": "2",
                "vitalStatus": "1",
                "rawTerm": " nausea ",
                "doseMg": 10,
                "collidingField": 88888,
            },
        }
        flat_dict = {
            "record_id": 103,
            "study_key": "STUDY_TAB_GOV",
            "subject_key": "SUBJ-103",
            "visit_id": 3,
            "form_id": 10,
            "customChartId": "CH-103",
            "medicalRecordNumber": "MRN-103",
            "SSN": "555-55-5555",
            "AESEV": "1",
            "vitalStatus": "1",
            "rawTerm": "rash",
            "doseMg": 15,
            "collidingField": 77777,
        }
        raw_df = pd.DataFrame([dict(flat_dict, record_id=104, subject_key="SUBJ-104", doseMg=30)])

        cfg = TabularSinkConfig(study_key="STUDY_TAB_GOV")

        csv_rec_path = tmp_path / "out_records.csv"
        with TabularCSVSink(csv_rec_path, cfg) as csv_sink:
            assert csv_sink.write_batch([rec_model, envelope_dict, flat_dict], batch_id="b0") == 3
        csv_df_path = tmp_path / "out_df.csv"
        with TabularCSVSink(csv_df_path, cfg) as csv_df_sink:
            assert csv_df_sink.write_batch(raw_df, batch_id="b1") == 1

        pq_path = tmp_path / "out.parquet"
        with TabularParquetSink(pq_path, cfg) as pq_sink:
            assert pq_sink.write_batch([rec_model, envelope_dict, flat_dict], batch_id="b0") == 3
            assert pq_sink.write_batch(raw_df, batch_id="b1") == 1

        sql_engine = create_engine(f"sqlite:///{tmp_path / 'out.sqlite'}")
        try:
            with TabularSQLSink("ae_out", sql_engine, cfg) as sql_sink:
                assert (
                    sql_sink.write_batch([rec_model, envelope_dict, flat_dict], batch_id="b0") == 3
                )
                assert sql_sink.write_batch(raw_df, batch_id="b1") == 1
            sql_df = pd.read_sql_table("ae_out", sql_engine)
        finally:
            sql_engine.dispose()

        ddb_path = tmp_path / "out.duckdb"
        with TabularDuckDBSink(ddb_path, "ae_out", cfg) as ddb_sink:
            assert ddb_sink.write_batch([rec_model, envelope_dict, flat_dict], batch_id="b0") == 3
            assert ddb_sink.write_batch(raw_df, batch_id="b1") == 1
        with duckdb.connect(str(ddb_path)) as conn:
            ddb_df = conn.execute("SELECT * FROM ae_out ORDER BY record_id").df()

        csv_df = pd.concat(
            [pd.read_csv(csv_rec_path), pd.read_csv(csv_df_path)],
            ignore_index=True,
        )
        pq_df = pd.read_parquet(pq_path)

        for out_df in (csv_df, pq_df, sql_df, ddb_df):
            ordered = out_df.sort_values("record_id").reset_index(drop=True)
            assert ordered["record_id"].tolist() == [101, 102, 103, 104]
            assert ordered["subject_key"].tolist() == [
                "SUBJ-101",
                "SUBJ-102",
                "SUBJ-103",
                "SUBJ-104",
            ]
            assert ordered["study_key"].tolist() == ["STUDY_TAB_GOV"] * 4
            assert ordered["customChartId"].tolist() == ["***MASKED***"] * 4
            assert ordered["medicalRecordNumber"].tolist() == ["***MASKED***"] * 4
            assert ordered["SSN"].tolist() == ["***MASKED***"] * 4
            assert ordered["AESEV"].tolist() == ["MILD", "MODERATE", "MILD", "MILD"]
            assert ordered["vitalStatus"].tolist() == ["DECEASED", "ALIVE", "ALIVE", "ALIVE"]
            assert ordered["normalizedTerm"].tolist() == ["HEADACHE", "NAUSEA", "RASH", "RASH"]
            assert ordered["doseMg"].tolist() == [50, 20, 30, 60]
            assert "rawTerm" not in ordered.columns

        # Caller inputs remain completely unmutated
        assert rec_model.record_data["customChartId"] == "CH-101"
        assert rec_model.record_data["rawTerm"] == "  headache "
        assert envelope_dict["recordData"]["medicalRecordNumber"] == "MRN-102"  # type: ignore[index]
        assert flat_dict["customChartId"] == "CH-103"
        assert raw_df["customChartId"].iloc[0] == "CH-103"
        assert raw_df["doseMg"].iloc[0] == 30
        assert "_imednet_tabular_prepared" not in raw_df.attrs

    def test_tabular_streaming_export_and_mask_phi_false_keeps_enrichment_active(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Any,
    ) -> None:
        """run_streaming_export enforces StudyConfiguration and mask_phi=False keeps mappings/lookups."""
        from imednet.integrations.sink_base import run_streaming_export
        from imednet.models.records import Record
        from imednet.models.study_config import MappingRule, StudyConfiguration

        study_cfg = StudyConfiguration(
            study_key="STUDY_OPT_OUT",
            phi_fields=["customChartId", "medicalRecordNumber"],
            terminology_lookups={"AESEV": {"2": "MODERATE"}},
            mappings=[
                MappingRule(
                    domain="AE",
                    targetField="cleanTerm",
                    sourceFormKey="AE",
                    sourceVariableName="rawTerm",
                    businessLogic="str(value).upper()",
                )
            ],
        )
        self._commit_study_config(monkeypatch, tmp_path, study_cfg)

        stream_records = [
            Record(
                record_id=1,
                study_key="STUDY_OPT_OUT",
                subject_key="SUBJ-1",
                form_id=1,
                record_data={
                    "customChartId": "CH-777",
                    "medicalRecordNumber": "MRN-888",
                    "SSN": "123-45-6789",
                    "AESEV": "2",
                    "rawTerm": "dizziness",
                },
            )
        ]

        sdk = MagicMock()
        masked_csv = tmp_path / "stream_masked.csv"
        masked_cfg = TabularSinkConfig(study_key="STUDY_OPT_OUT", batch_size=1)
        run_streaming_export(
            sdk,
            "STUDY_OPT_OUT",
            TabularCSVSink(masked_csv, masked_cfg),
            masked_cfg,
            stream_name="csv_masked",
            batch_id_prefix="STUDY_OPT_OUT/csv",
            records_source=lambda _wm, _excl: iter(stream_records),
            quality_gate_fn=sink_base_mod.apply_quality_gate,
            iter_batches_fn=iter_batches,
        )
        masked_df = pd.read_csv(masked_csv)
        assert masked_df["customChartId"].tolist() == ["***MASKED***"]
        assert masked_df["medicalRecordNumber"].tolist() == ["***MASKED***"]
        assert masked_df["SSN"].tolist() == ["***MASKED***"]
        assert masked_df["AESEV"].tolist() == ["MODERATE"]
        assert masked_df["cleanTerm"].tolist() == ["DIZZINESS"]

        unmasked_csv = tmp_path / "stream_unmasked.csv"
        unmasked_cfg = TabularSinkConfig(study_key="STUDY_OPT_OUT", mask_phi=False, batch_size=1)
        run_streaming_export(
            sdk,
            "STUDY_OPT_OUT",
            TabularCSVSink(unmasked_csv, unmasked_cfg),
            unmasked_cfg,
            stream_name="csv_unmasked",
            batch_id_prefix="STUDY_OPT_OUT/csv_unmasked",
            records_source=lambda _wm, _excl: iter(stream_records),
            quality_gate_fn=sink_base_mod.apply_quality_gate,
            iter_batches_fn=iter_batches,
        )
        unmasked_df = pd.read_csv(unmasked_csv)
        assert unmasked_df["customChartId"].tolist() == ["CH-777"]
        assert unmasked_df["medicalRecordNumber"].tolist() == ["MRN-888"]
        assert unmasked_df["SSN"].tolist() == ["123-45-6789"]
        assert unmasked_df["AESEV"].tolist() == ["MODERATE"]
        assert unmasked_df["cleanTerm"].tolist() == ["DIZZINESS"]

    def test_unconfigured_study_queries_config_version_store_once_per_sink(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Any,
    ) -> None:
        """Per-sink caching ensures unconfigured studies query resolve_committed_config once."""
        from imednet.models.study_config import StudyConfiguration
        from imednet_workflows.config_version_control import ConfigVersionStore

        # Commit a config for ANOTHER study so the SQLite store file exists on disk
        other_cfg = StudyConfiguration(study_key="OTHER_STUDY", phi_fields=["secretField"])
        self._commit_study_config(monkeypatch, tmp_path, other_cfg)

        calls: list[str] = []
        orig_resolve = ConfigVersionStore.resolve_committed_config

        @classmethod  # type: ignore[misc]
        def _spy_resolve(cls: Any, study_key: str) -> Any:
            calls.append(study_key)
            return orig_resolve(study_key)

        monkeypatch.setattr(ConfigVersionStore, "resolve_committed_config", _spy_resolve)

        csv_sink = TabularCSVSink(
            tmp_path / "unconfigured.csv",
            TabularSinkConfig(study_key="UNCONFIGURED_STUDY"),
        )
        with csv_sink:
            for idx in range(5):
                csv_sink.write_batch(
                    [{"record_id": idx + 1, "subject_key": f"S{idx}", "HR": 70 + idx}],
                    batch_id=f"b/{idx}",
                )
        assert calls == ["UNCONFIGURED_STUDY"]

        calls.clear()
        custom_sink = _StubSink(SinkConfig(study_key="UNCONFIGURED_CUSTOM"))
        for idx in range(5):
            custom_sink.write_batch(
                [{"record_id": idx + 1, "SSN": "123-45-6789", "HR": 65}],
                batch_id=f"c/{idx}",
            )
        assert calls == ["UNCONFIGURED_CUSTOM"]
        assert custom_sink.batches[0][0][0]["SSN"] == "***MASKED***"

    def test_sink_config_extra_overrides_and_base_redact_batch_fail_closed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Any,
    ) -> None:
        """SinkConfig.extra overrides work without store and ExportSink.redact_batch fails closed."""
        from types import SimpleNamespace

        from imednet.integrations.enrichment import (
            EnrichmentPipeline,
            resolve_sink_enrichment_pipeline,
        )
        from imednet.integrations.export import TabularParquetSink
        from imednet.models.records import Record
        from imednet.models.study_config import MappingRule, StudyConfiguration
        from imednet.spi import sink_base as spi_sink_base

        override_cfg = StudyConfiguration(
            study_key="STUDY_EXTRA",
            phi_fields=["customChartId", "record_id"],
            terminology_lookups={"vitalStatus": {"1": "ALIVE"}},
            mappings=[
                MappingRule(
                    domain="DM",
                    targetField="mappedStatus",
                    sourceFormKey="DM",
                    sourceVariableName="vitalStatus",
                )
            ],
        )

        # 1. Extra study_config override on TabularParquetSink
        pq_path = tmp_path / "extra.parquet"
        pq_cfg = TabularSinkConfig(
            study_key="STUDY_EXTRA",
            extra={"study_config": override_cfg.model_dump(by_alias=True)},
        )
        with TabularParquetSink(pq_path, pq_cfg) as pq_sink:
            pq_sink.write_batch(
                [
                    {
                        "record_id": 11,
                        "subject_key": "S11",
                        "customChartId": "C11",
                        "vitalStatus": "1",
                    }
                ],
                batch_id="pq/0",
            )
        pq_df = pd.read_parquet(pq_path)
        assert pq_df["record_id"].tolist() == [11]
        assert pq_df["subject_key"].tolist() == ["S11"]
        assert pq_df["customChartId"].tolist() == ["***MASKED***"]
        assert pq_df["mappedStatus"].tolist() == ["ALIVE"]

        # 2. Extra enrichment_pipeline override on custom ExportSink
        custom_pipeline = EnrichmentPipeline(override_cfg)
        assert (
            resolve_sink_enrichment_pipeline(
                SinkConfig(study_key="STUDY_EXTRA", extra={"enrichment_pipeline": custom_pipeline})
            )
            is custom_pipeline
        )

        clean_rec = Record(record_id=1, subject_key="S1", record_data={"HR": 72})
        phi_rec = Record(
            record_id=2,
            subject_key="S2",
            record_data={"customChartId": "CH-2", "SSN": "123-45-6789", "HR": 80},
        )
        ns_rec = SimpleNamespace(
            record_id=3,
            subject_key="S3",
            record_data={"customChartId": "CH-3", "nested": [{"customChartId": "CH-3B", "ok": 1}]},
        )

        audit_sink = _StubSink(
            SinkConfig(study_key="STUDY_EXTRA", extra={"study_config": override_cfg})
        )
        audit_sink.write_batch([clean_rec, phi_rec, ns_rec], batch_id="a/0")
        out_batch = audit_sink.batches[0][0]
        assert out_batch[0] is clean_rec
        assert out_batch[1].record_id == 2
        assert out_batch[1].subject_key == "S2"
        assert out_batch[1].record_data == {
            "customChartId": "***MASKED***",
            "SSN": "***MASKED***",
            "HR": 80,
        }
        assert out_batch[2].record_data == {
            "customChartId": "***MASKED***",
            "nested": [{"customChartId": "***MASKED***", "ok": 1}],
        }
        # Caller inputs untouched
        assert phi_rec.record_data["customChartId"] == "CH-2"
        assert ns_rec.record_data["customChartId"] == "CH-3"

        # 3. Direct SPI redact_batch_records accepts optional phi_keys across DataFrames & SDTM dicts
        df_in = pd.DataFrame(
            [
                {
                    "study_key": "STUDY_EXTRA",
                    "customChartId": "CH-DF",
                    "payload": {"customChartId": "CH-NESTED"},
                }
            ]
        )
        df_redacted = spi_sink_base.redact_batch_records(df_in, phi_keys=["customChartId"])
        assert df_redacted["study_key"].iloc[0] == "STUDY_EXTRA"
        assert df_redacted["customChartId"].iloc[0] == "***MASKED***"
        assert df_redacted["payload"].iloc[0] == {"customChartId": "***MASKED***"}
        assert df_in["customChartId"].iloc[0] == "CH-DF"

        sdtm_dict = spi_sink_base.redact_batch_records(
            {"USUBJID": "U1", "BRTHDTC": "1984-07-19", "customChartId": "CH-SDTM"},
            as_sdtm=True,
            phi_keys={"customChartId"},
        )
        assert sdtm_dict == {"USUBJID": "U1", "BRTHDTC": "1984", "customChartId": "***MASKED***"}

        # 4. Fail-closed on ExportSink(config=None), missing encryption key, or missing store plugin
        none_cfg_sink = _StubSink(config=None)
        none_cfg_sink.write_batch(
            [{"SSN": "123-45-6789", "customChartId": "CH-RAW"}], batch_id="n/0"
        )
        assert none_cfg_sink.batches[0][0][0] == {
            "SSN": "***MASKED***",
            "customChartId": "CH-RAW",
        }

        self._commit_study_config(monkeypatch, tmp_path, override_cfg)
        monkeypatch.delenv("IMEDNET_ENCRYPTION_KEY", raising=False)
        no_key_sink = _StubSink(SinkConfig(study_key="STUDY_EXTRA"))
        no_key_sink.write_batch(
            [{"SSN": "123-45-6789", "customChartId": "CH-RAW"}], batch_id="nk/0"
        )
        assert no_key_sink.batches[0][0][0] == {
            "SSN": "***MASKED***",
            "customChartId": "CH-RAW",
        }
