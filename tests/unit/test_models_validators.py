"""Unit tests for models validators."""

import datetime

import pytest

from imednet.utils import validators


@pytest.mark.parametrize(
    ("input_val", "expected"),
    [
        # Boolean inputs
        (True, True),
        (False, False),
        # String inputs - True variants (optimized)
        ("true", True),
        ("True", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("y", True),
        ("t", True),
        # String inputs - False variants (optimized)
        ("false", False),
        ("False", False),
        ("FALSE", False),
        ("0", False),
        ("no", False),
        ("n", False),
        ("f", False),
        # String inputs - Whitespace/Case (fallback path)
        (" true ", True),
        (" Yes ", True),
        ("TrUe", True),
        (" FALSE ", False),
        (" No ", False),
        ("FaLsE", False),
        # Numeric inputs
        (1, True),
        (0, False),
        (1.0, True),
        (0.0, False),
        (-1, True),  # Non-zero number is True
        # Edge cases / Invalid
        (None, False),
        ("maybe", False),
        ("", False),
        ([], False),
        ({}, False),
    ],
)
def test_parse_bool_comprehensive(input_val, expected):
    """Test that parse bool comprehensive."""
    assert validators.parse_bool(input_val) is expected


def test_parse_int_or_default_and_str_default():
    """Test that parse int or default and str default."""
    assert validators.parse_int_or_default("5") == 5
    assert validators.parse_int_or_default(None, default=2) == 2
    assert validators.parse_int_or_default("bad") == 0
    with pytest.raises(ValueError):
        validators.parse_int_or_default("bad", strict=True)

    # Float string robustness
    assert validators.parse_int_or_default("5.0") == 5
    assert validators.parse_int_or_default("5.9") == 5
    assert validators.parse_int_or_default(5.9) == 5
    assert validators.parse_int_or_default("inf") == 0
    with pytest.raises(OverflowError):
        validators.parse_int_or_default("inf", strict=True)

    assert validators.parse_str_or_default(None) == ""
    assert validators.parse_str_or_default(123) == "123"


def test_parse_list_and_dict_helpers():
    """Test that parse list and dict helpers."""
    assert validators.parse_list_or_default(None) == []
    assert validators.parse_list_or_default("a") == ["a"]
    assert validators.parse_list_or_default([1, 2]) == [1, 2]
    assert validators.parse_dict_or_default(None) == {}
    assert validators.parse_dict_or_default({"a": 1}) == {"a": 1}
    assert validators.parse_dict_or_default("bad") == {}


def test_parse_datetime_wrapper():
    """Test that parse datetime wrapper."""
    dt = datetime.datetime(2024, 1, 1)
    assert validators.parse_datetime(dt) == dt
    iso = "2024-01-01T00:00:00Z"
    parsed = validators.parse_datetime(iso)
    assert isinstance(parsed, datetime.datetime)
    assert validators.parse_datetime("") is None


def test_parse_datetime_default_date() -> None:
    """Return ``None`` when given an empty or ``None`` value in permissive mode."""
    assert validators.parse_datetime("") is None
    assert validators.parse_datetime(None) is None


def test_parse_datetime_parses_strings() -> None:
    """Parse ISO formatted strings into ``datetime`` objects."""
    iso = "2024-01-01T00:00:00Z"
    parsed = validators.parse_datetime(iso)
    assert parsed == datetime.datetime(2024, 1, 1, 0, 0, tzinfo=datetime.timezone.utc)


def test_parse_datetime_numeric():
    """Test that parse datetime numeric."""
    timestamp = 1704067200  # 2024-01-01 00:00:00 UTC
    dt = validators.parse_datetime(timestamp)
    assert dt == datetime.datetime(2024, 1, 1, 0, 0, tzinfo=datetime.timezone.utc)

    dt_float = validators.parse_datetime(float(timestamp))
    assert dt_float == datetime.datetime(2024, 1, 1, 0, 0, tzinfo=datetime.timezone.utc)


def test_parse_bool_string_float():
    """Test that parse bool string float rejects non-finite floats."""
    assert validators.parse_bool("1.0") is True
    assert validators.parse_bool("0.0") is False
    assert validators.parse_bool("-1") is True
    assert validators.parse_bool("+1") is True
    assert validators.parse_bool(".5") is True
    assert validators.parse_bool("nan") is False
    assert validators.parse_bool("inf") is False
    assert validators.parse_bool("-inf") is False
    assert validators.parse_bool("nope") is False
    assert validators.parse_bool("invalid") is False
    for non_finite in ("nan", "inf", "-inf", float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="non-finite"):
            validators.parse_bool(non_finite, strict=True)


def test_parse_dict_or_default_structural_shift(caplog):
    """Test that parse dict or default structural shift."""
    import logging

    with caplog.at_level(logging.WARNING):
        result = validators.parse_dict_or_default([{"a": 1}, {"b": 2}])
        assert result == {"a": 1}
        assert (
            "Structural shift detected: API returned a list where an object was expected. Coercing by extracting the first item."
            in caplog.text
        )

    result_empty = validators.parse_dict_or_default([])
    assert result_empty == {}


def test_parse_list_or_default_structural_shift(caplog):
    """Test that parse list or default structural shift."""
    import logging

    with caplog.at_level(logging.WARNING):
        result = validators.parse_list_or_default({"a": 1})
        assert result == [{"a": 1}]
        assert (
            "Structural shift detected: API returned an object where a list was expected. Coercing by wrapping in a list."
            in caplog.text
        )


def test_parse_bool_invalid_float():
    """Test that parse bool invalid float."""
    # To cover the 'except (ValueError, TypeError)' block in parse_bool float fallback
    assert validators.parse_bool("1invalid") is False


@pytest.mark.parametrize(
    ("container", "expected_type", "sensitive_markers"),
    [
        (
            [
                {
                    "api_key": "synth-cred-list-901",  # pragma: allowlist secret
                    "patient_name": "Jane Synthetic",
                    "nested": {"ssn": "999-00-1111", "note": "CONFIDENTIAL-LIST-PAYLOAD"},
                }
            ],
            "list",
            ("synth-cred-list-901", "Jane Synthetic", "999-00-1111", "CONFIDENTIAL-LIST-PAYLOAD"),
        ),
        (
            {
                "nested": {
                    "token": "synth-token-dict-902",  # pragma: allowlist secret
                    "patient_name": "John Synthetic",
                    "ssn": "999-00-2222",
                    "note": "CONFIDENTIAL-DICT-PAYLOAD",
                }
            },
            "dict",
            ("synth-token-dict-902", "John Synthetic", "999-00-2222", "CONFIDENTIAL-DICT-PAYLOAD"),
        ),
    ],
)
def test_str_normalizers_guard_against_containers_without_leaking_values(
    caplog, monkeypatch, container, expected_type, sensitive_markers
):
    """Verify parse_str_or_default and _optional_str report only expected/actual types and never disclose nested values."""
    import logging

    from pydantic import Field, ValidationError

    from imednet.models.base import ImednetBaseModel, _drift_reported, _optional_str

    class SampleStrModel(ImednetBaseModel):
        opt_text: str | None = Field(default=None, alias="optText")
        req_text: str = Field(default="fallback", alias="reqText")

    _drift_reported.clear()
    validators._drift_reported.clear()
    expected_fragment = f"Expected str, got {expected_type}"

    # Permissive mode: returns defaults/None, logs type-only drift warning, omits nested values
    monkeypatch.setenv("IMEDNET_STRICT_MODE", "false")
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="imednet.drift"):
        assert validators.parse_str_or_default(container, default="def") == "def"
        assert _optional_str(container) is None
        parsed = SampleStrModel.from_json({"optText": container, "reqText": container})
        assert parsed.opt_text is None
        assert parsed.req_text == ""
        assert validators.parse_bool(container) is False
        assert validators.parse_int_or_default(container, default=7) == 7

    assert expected_fragment in caplog.text
    for marker in sensitive_markers:
        assert marker not in caplog.text

    # Strict mode: raises ValueError / ValidationError with type-only text and omits nested values from logs & exceptions
    caplog.clear()
    _drift_reported.clear()
    with caplog.at_level(logging.WARNING, logger="imednet.drift"):
        with pytest.raises(ValueError, match=expected_fragment) as strict_arg_exc:
            validators.parse_str_or_default(container, strict=True)

        monkeypatch.setenv("IMEDNET_STRICT_MODE", "true")
        with pytest.raises(ValueError, match=expected_fragment) as opt_str_exc:
            _optional_str(container)
        with pytest.raises(ValidationError, match=expected_fragment) as model_opt_exc:
            SampleStrModel.from_json({"optText": container})
        with pytest.raises(ValidationError, match=expected_fragment) as model_req_exc:
            SampleStrModel.from_json({"reqText": container})

    for exc_info in (strict_arg_exc, opt_str_exc, model_opt_exc, model_req_exc):
        exc_text = f"{exc_info.value}\n{exc_info.value!r}"
        for marker in sensitive_markers:
            assert marker not in exc_text
    for marker in sensitive_markers:
        assert marker not in caplog.text


def test_fast_path_normalizers_skip_strict_lookup_on_none_and_canonical_types(monkeypatch):
    """Verify canonical primitives and None return before resolving strict mode."""
    import enum

    import imednet.core.context as ctx_mod
    import imednet.models.base as base_mod
    from imednet.models.base import (
        _extract_single_item,
        _optional_bool,
        _optional_datetime,
        _optional_dict,
        _optional_int,
        _optional_list,
        _optional_str,
        _required_int,
    )

    class _SampleIntEnum(enum.IntEnum):
        FIVE = 5

    class _SampleStrEnum(enum.StrEnum):
        ALPHA = "alpha"
        BLANK = "   "

    def _forbid_strict_lookup() -> bool:
        raise AssertionError("is_strict_mode() should not be called on canonical fast paths")

    monkeypatch.setattr(base_mod, "is_strict_mode", _forbid_strict_lookup)
    monkeypatch.setattr(ctx_mod, "is_strict_mode", _forbid_strict_lookup)
    monkeypatch.setattr(validators, "_is_strict_mode_fn", _forbid_strict_lookup)

    now = datetime.datetime(2026, 5, 13, 12, 0, tzinfo=datetime.UTC)

    # _optional_* on None and canonical values
    assert _optional_str(None) is None
    assert _optional_str("") is None
    assert _optional_str("   \t\n") is None
    assert _optional_str("clean") == "clean"
    assert _optional_str(_SampleStrEnum.ALPHA) == "alpha"
    assert _optional_str(_SampleStrEnum.BLANK) is None
    assert _required_int(42) == 42
    assert _optional_int(None) is None
    assert _optional_int(42) == 42
    assert _optional_bool(None) is None
    assert _optional_bool(True) is True
    assert _optional_bool(False) is False
    assert _optional_datetime(None) is None
    assert _optional_datetime(now) is now
    assert _optional_datetime("2026-05-13T12:00:00Z") == now
    assert _optional_list(None) is None
    assert _optional_list([1, 2]) == [1, 2]
    assert _optional_dict(None) is None
    assert _optional_dict({"k": 1}) == {"k": 1}
    assert _extract_single_item({"id": 1}) == {"id": 1}

    # parse_* on canonical values
    assert validators.is_missing_value(None) is True
    assert validators.is_missing_value("") is True
    assert validators.is_missing_value(" \n\t ") is True
    assert validators.is_missing_value("x") is False
    assert validators.parse_str_or_default("abc") == "abc"
    assert validators.parse_str_or_default(None, default="fallback") == "fallback"
    assert validators.parse_int_or_default(7) == 7
    assert validators.parse_int_or_default(_SampleIntEnum.FIVE) == 5
    assert validators.parse_int_or_default(" 42 ") == 42
    assert validators.parse_bool(True) is True
    assert validators.parse_bool(False) is False
    assert validators.parse_bool("true") is True
    assert validators.parse_bool(" False ") is False
    assert validators.parse_datetime(now) is now
    assert validators.parse_datetime("2026-05-13T12:00:00Z") == now
    assert validators.parse_list_or_default(["a"]) == ["a"]
    assert validators.parse_dict_or_default({"a": 1}) == {"a": 1}


def test_strict_mode_env_cache_tracks_dynamic_mutations_and_context(monkeypatch):
    """Verify IMEDNET_STRICT_MODE caching updates immediately across setenv, delenv, context, and custom mappings."""
    import os

    from pydantic import ValidationError

    import imednet.core.context as ctx_mod
    from imednet.core.context import is_strict_mode, strict_mode_context
    from imednet.models.subjects import Subject

    monkeypatch.delenv("IMEDNET_STRICT_MODE", raising=False)
    assert is_strict_mode() is False
    assert Subject.from_json({"subjectId": "42.9"}).subject_id == 42

    monkeypatch.setenv("IMEDNET_STRICT_MODE", "1")
    assert is_strict_mode() is True
    with pytest.raises(ValidationError):
        Subject.from_json({"subjectId": "42.9"})

    with strict_mode_context(False):
        assert is_strict_mode() is False
        assert Subject.from_json({"subjectId": "42.9"}).subject_id == 42

    assert is_strict_mode() is True

    monkeypatch.setenv("IMEDNET_STRICT_MODE", "false")
    assert is_strict_mode() is False
    with strict_mode_context(True):
        assert is_strict_mode() is True
        with pytest.raises(ValidationError):
            Subject.from_json({"subjectId": "42.9"})

    monkeypatch.delenv("IMEDNET_STRICT_MODE")
    assert is_strict_mode() is False

    # Verify fallback when os.environ itself is replaced with a custom mapping
    for idx in range(36):
        monkeypatch.setenv("IMEDNET_STRICT_MODE", f"env_unknown_{idx}")
        assert is_strict_mode() is False
    assert len(ctx_mod._STRICT_ENV_CACHE) <= 33

    monkeypatch.setattr(os, "environ", {"IMEDNET_STRICT_MODE": "yes"})
    assert is_strict_mode() is True
    os.environ["IMEDNET_STRICT_MODE"] = "no"
    assert is_strict_mode() is False
    del os.environ["IMEDNET_STRICT_MODE"]
    assert is_strict_mode() is False

    # Verify cache bounding stays functional when >32 distinct raw values are seen
    for idx in range(40):
        os.environ["IMEDNET_STRICT_MODE"] = f"unknown_{idx}"
        assert is_strict_mode() is False
    assert len(ctx_mod._STRICT_ENV_CACHE) <= 33


def test_optional_and_primitive_normalizer_edge_branches(monkeypatch):
    """Cover permissive missing-string, non-finite numeric, and multi-field drift branches."""
    from types import SimpleNamespace

    from pydantic import Field, ValidationError

    from imednet.models.base import (
        ImednetBaseModel,
        _optional_bool,
        _optional_datetime,
        _optional_dict,
        _optional_int,
        _optional_list,
        _optional_str,
    )

    monkeypatch.setenv("IMEDNET_STRICT_MODE", "0")
    assert _optional_str(123) == "123"
    assert _optional_int("") is None
    assert _optional_bool("") is None
    assert _optional_datetime("") is None
    assert _optional_list("") is None
    assert _optional_list("item") == ["item"]
    assert _optional_dict("") is None
    assert _optional_dict("not-a-dict") == {}

    assert validators.parse_datetime(True) is None
    with pytest.raises(ValueError, match="Expected datetime, got bool"):
        validators.parse_datetime(True, strict=True)
    assert validators.parse_int_or_default("nan", default=3) == 3
    with pytest.raises(ValueError, match="NaN"):
        validators.parse_int_or_default("nan", strict=True)
    with pytest.raises(ValueError, match="NoneType"):
        validators.parse_int_or_default(None, strict=True)
    assert validators.parse_int_or_default(float("inf"), default=4) == 4

    class _TwoReqModel(ImednetBaseModel):
        first_req: str = Field(..., alias="firstReq")
        second_req: int = Field(..., alias="secondReq")

    assert _TwoReqModel._normalise("raw", SimpleNamespace(field_name=None)) == "raw"
    with pytest.raises(ValidationError):
        _TwoReqModel.model_validate({})


def test_record_batch_validation_call_counts_and_subclass_validator_order(monkeypatch):
    """Verify 10,000 Record validations execute <= 10,000 strict checks and zero os.getenv/parse_bool calls, while preserving subclass validator order."""
    import os

    import imednet.core.context as ctx_mod
    import imednet.models.base as base_mod
    from imednet.core.context import strict_mode_context
    from imednet.models.jobs import JobStatus
    from imednet.models.records import Record, RecordStatus
    from imednet.models.standards import StandardsReadinessReport
    from imednet.models.users import Role
    from imednet.testing.fake_data import fake_record
    from imednet.validation.cache import SchemaCache

    cache = SchemaCache()
    payloads = [fake_record(cache) for _ in range(10000)]

    monkeypatch.setenv("IMEDNET_STRICT_MODE", "false")
    # Prime initial env cache once before steady-state measurement
    assert ctx_mod.is_strict_mode() is False

    counts = {"strict": 0, "getenv": 0, "parse_bool": 0}
    orig_strict = ctx_mod.is_strict_mode
    orig_getenv = os.getenv
    orig_parse_bool = validators.parse_bool

    def _counted_strict() -> bool:
        counts["strict"] += 1
        return orig_strict()

    def _counted_getenv(*args, **kwargs):
        counts["getenv"] += 1
        return orig_getenv(*args, **kwargs)

    def _counted_parse_bool(*args, **kwargs):
        counts["parse_bool"] += 1
        return orig_parse_bool(*args, **kwargs)

    monkeypatch.setattr(ctx_mod, "is_strict_mode", _counted_strict)
    monkeypatch.setattr(base_mod, "is_strict_mode", _counted_strict)
    monkeypatch.setattr(validators, "_is_strict_mode_fn", _counted_strict)
    monkeypatch.setattr(os, "getenv", _counted_getenv)
    monkeypatch.setattr(ctx_mod, "parse_bool", _counted_parse_bool)
    monkeypatch.setattr(base_mod, "parse_bool", _counted_parse_bool)

    for payload in payloads:
        Record.model_validate(payload)

    assert counts["strict"] <= 10000
    assert counts["getenv"] == 0
    assert counts["parse_bool"] == 0

    # Verify subclass validator execution order across Role, Record, JobStatus, StandardsReadinessReport
    with strict_mode_context(True):
        role = Role.from_json(
            {
                "roleId": "R-1",
                "dateCreated": [2026, 5, 13, 14, 6, 50, 612000000],
                "dateModified": [2026, 5, 14, 9, 30, 0, 0],
            }
        )
        assert role.date_created == datetime.datetime(
            2026, 5, 13, 14, 6, 50, 612000, tzinfo=datetime.UTC
        )
        rec = Record.from_json({"recordId": 1, "recordStatus": "record_complete", "deleted": False})
        assert rec.record_status == "record_complete"
        canonical_rec = Record.from_json(
            {"recordId": 2, "recordStatus": "Record Complete", "deleted": False}
        )
        assert canonical_rec.record_status is RecordStatus.RECORD_COMPLETE
        job = JobStatus.from_json(
            {"batchId": "B-1", "progress": "80", "totalRecords": 10, "successfulRecords": 8}
        )
        assert job.progress == 80
        assert job.total_records == 10
        py_job = JobStatus(batchId="B-1", progress="80", total_records=10, successful_records=8)
        assert py_job.total_records == 10
        assert py_job.successful_records == 8

    report = StandardsReadinessReport.model_validate(
        {
            "score": "not-a-float",
            "successfully_validated_fields": 4,
            "total_expected_fields": 5,
            "domain_scores": {"DM": "95.5", "AE": "bad"},
        }
    )
    assert report.score == 0.0
    assert report.domain_scores == {"DM": 95.5, "AE": 0.0}
