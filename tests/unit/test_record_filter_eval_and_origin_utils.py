"""Unit tests for core Record filter evaluation, cache origin normalization, and retry delegation."""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from imednet import ImednetSDK
from imednet.core.retry import DefaultRetryPolicy, RetryConfig, delegates_to_sdk_retry_policy
from imednet.errors import ConfigurationError
from imednet.models.records import Record, RecordStatus
from imednet.spi import utils as spi_utils
from imednet.utils.record_filter_eval import (
    RECORDS_FILTER_ATTRIBUTES,
    compile_record_predicate,
)
from imednet.utils.url import normalize_cache_origin, resolve_sdk_base_url


def _make_record(**overrides: object) -> Record:
    payload: dict[str, object] = {
        "studyKey": "STUDY-1",
        "recordId": 10,
        "formId": 100,
        "formKey": "DEMOG",
        "siteId": 5,
        "visitId": 20,
        "intervalId": 3,
        "subjectId": 55,
        "subjectKey": "SUBJ-01",
        "subjectOid": "OID-01",
        "recordOid": "REC-OID-10",
        "recordType": "SUBJECT",
        "recordStatus": "Record Complete",
        "deleted": False,
        "dateCreated": "2026-01-01T08:00:00Z",
        "dateModified": "2026-01-15T12:30:00Z",
        "lastUpdated": "2026-01-15T12:30:00Z",
        "parentRecordId": None,
        "recordData": {
            "AGE": 45,
            "WEIGHT": 72.5,
            "SMOKER": False,
            "VISIT_DATE": "2026-01-14",
            "SCORE_STR": "120",
            "NOTES": "Mild seasonal allergy; stable",
            "NESTED": {"LAB": {"HGB": 13.8}, "dotted.leaf": "ok"},
            "TAGS": ["a", "b"],
            "EMPTY_VAL": None,
        },
    }
    payload.update(overrides)
    return Record.model_validate(payload)


def test_spi_utils_reexports_origin_retry_and_predicate_symbols() -> None:
    """imednet.spi.utils lazily exposes all origin, retry, and record filter symbols."""
    assert spi_utils.RECORDS_FILTER_ATTRIBUTES is RECORDS_FILTER_ATTRIBUTES
    assert spi_utils.compile_record_predicate is compile_record_predicate
    assert spi_utils.normalize_cache_origin is normalize_cache_origin
    assert spi_utils.resolve_sdk_base_url is resolve_sdk_base_url
    assert spi_utils.DefaultRetryPolicy is DefaultRetryPolicy
    assert spi_utils.delegates_to_sdk_retry_policy is delegates_to_sdk_retry_policy


def test_normalize_cache_origin_and_resolve_sdk_base_url_edge_cases() -> None:
    """normalize_cache_origin and resolve_sdk_base_url handle custom paths, mocks, and non-strings."""
    assert normalize_cache_origin(None) == "https://edc.prod.imednetapi.com"
    assert normalize_cache_origin("   ") == "https://edc.prod.imednetapi.com"
    assert normalize_cache_origin("edc.trn.imednetapi.com/api/v1/edc/") == (
        "https://edc.trn.imednetapi.com"
    )
    assert normalize_cache_origin("https://EDC.PROD.IMEDNETAPI.COM:443/api/v1/edc") == (
        "https://edc.prod.imednetapi.com"
    )
    assert normalize_cache_origin("http://127.0.0.1:80/api/v1/") == "http://127.0.0.1"
    assert (
        normalize_cache_origin("http://127.0.0.1:9000/custom/api") == "http://127.0.0.1:9000/custom"
    )

    # Plain MagicMock does not leak Mock str repr into cache origin
    mock_sdk = MagicMock()
    assert resolve_sdk_base_url(mock_sdk) == "https://edc.prod.imednetapi.com"

    # Explicit _ctx.base_url string on lightweight stub resolves cleanly
    ctx_stub = SimpleNamespace(
        _ctx=SimpleNamespace(base_url="https://edc.trn.imednetapi.com/api/v1/edc")
    )
    assert resolve_sdk_base_url(ctx_stub) == "https://edc.trn.imednetapi.com"

    # Fallback through _client._base_url (both str and httpx.URL)
    import httpx

    client_stub = SimpleNamespace(
        _client=SimpleNamespace(_base_url=httpx.URL("https://stage.example.com:8443/api/v1/edc"))
    )
    assert resolve_sdk_base_url(client_stub) == "https://stage.example.com:8443"

    sdk_priv_stub = SimpleNamespace(_base_url="https://qa.example.com:9443/")
    assert resolve_sdk_base_url(sdk_priv_stub) == "https://qa.example.com:9443"


def test_delegates_to_sdk_retry_policy_inspects_real_and_stub_sdks() -> None:
    """delegates_to_sdk_retry_policy returns True only when real SDK client owns DefaultRetryPolicy."""
    with ImednetSDK(api_key="k", security_key="s", simulation_mode=True) as sdk:
        assert delegates_to_sdk_retry_policy(sdk, sdk.records.list) is True
        # Replacing SDK method with a MagicMock disables delegation
        assert delegates_to_sdk_retry_policy(sdk, MagicMock()) is False

    # Custom non-DefaultRetryPolicy on client does not delegate
    class CustomPolicy:
        def should_retry(self, result: object) -> bool:
            return False

    with ImednetSDK(
        api_key="k",
        security_key="s",
        simulation_mode=True,
        retry_config=RetryConfig(retry_policy=CustomPolicy()),
    ) as custom_sdk:
        assert delegates_to_sdk_retry_policy(custom_sdk, custom_sdk.records.list) is False

    # RetryConfig(retries=2) still installs DefaultRetryPolicy
    with ImednetSDK(
        api_key="k",
        security_key="s",
        simulation_mode=True,
        retry_config=RetryConfig(retries=2),
    ) as cfg_sdk:
        assert delegates_to_sdk_retry_policy(cfg_sdk, cfg_sdk.records.list) is True

    assert delegates_to_sdk_retry_policy(MagicMock(), None) is False


def test_compile_record_predicate_metadata_and_data_operators() -> None:
    """compile_record_predicate evaluates scalars, dates, booleans, regex, dotted keys, and projections."""
    rec = _make_record()

    # Empty predicate matches and returns original record unchanged
    empty_pred = compile_record_predicate()
    assert empty_pred.matches(rec) is True
    assert empty_pred.apply(rec) is rec

    # Empty list filter short-circuits to always_empty
    never_pred = compile_record_predicate(extra_filters={"formId": []})
    assert never_pred.always_empty is True
    assert never_pred.matches(rec) is False
    assert never_pred.apply(rec) is None

    # Unknown metadata attribute raises ConfigurationError matching RecordsEndpoint
    with pytest.raises(ConfigurationError, match="Unsupported filter attribute"):
        compile_record_predicate(extra_filters={"filter": "nonExistentAttr==1"})
    with pytest.raises(ConfigurationError, match="Unsupported filter attribute"):
        compile_record_predicate(extra_filters={"unknown_field": "x"})

    # Combined raw filter + structured extra_filters with ';' AND
    combined_pred = compile_record_predicate(
        extra_filters={
            "filter": "siteId>=5;recordStatus==\"Record Complete\"",
            "subject_key": "SUBJ-01",
            "deleted": False,
            "page": 0,
            "size": 100,
            "sort": "recordId,ASC",
        }
    )
    assert combined_pred.matches(rec) is True

    # Comparison operators across numbers, numeric strings, booleans, dates, and lists
    rich_data_pred = compile_record_predicate(
        record_data_filter=(
            "AGE>=45;WEIGHT<80;SMOKER==false;SCORE_STR>100;VISIT_DATE>=2026-01-10;TAGS!=scalar"
        )
    )
    assert rich_data_pred.matches(rec) is True

    # Nested paths are NOT traversed when splitting on dots is disabled
    assert compile_record_predicate(record_data_filter="NESTED.LAB.HGB<=14.0").matches(rec) is False
    assert (
        compile_record_predicate(record_data_filter="NESTED.dotted.leaf==ok").matches(rec) is False
    )

    # Regex and OR clauses
    regex_or_pred = compile_record_predicate(record_data_filter='NOTES=~"seasonal.*stable",AGE<18')
    assert regex_or_pred.matches(rec) is True

    # Null/missing/container edge cases: != returns True on None or omitted values
    assert compile_record_predicate(record_data_filter="EMPTY_VAL==none").matches(rec) is False
    assert compile_record_predicate(record_data_filter="EMPTY_VAL!=none").matches(rec) is True
    assert compile_record_predicate(record_data_filter="MISSING_VAR!=1").matches(rec) is True
    assert compile_record_predicate(record_data_filter="MISSING_VAR==1").matches(rec) is False
    assert compile_record_predicate(record_data_filter="MISSING_VAR>1").matches(rec) is False
    assert (
        compile_record_predicate(extra_filters={"filter": "parentRecordId!=99"}).matches(rec)
        is True
    )
    assert (
        compile_record_predicate(extra_filters={"filter": "parentRecordId==99"}).matches(rec)
        is False
    )
    assert compile_record_predicate(record_data_filter="TAGS=~a").matches(rec) is False
    assert compile_record_predicate(record_data_filter="SMOKER==yes").matches(rec) is False
    assert compile_record_predicate(record_data_filter="SMOKER!=yes").matches(rec) is True
    assert compile_record_predicate(record_data_filter="AGE==notanumber").matches(rec) is False
    assert compile_record_predicate(record_data_filter="AGE!=notanumber").matches(rec) is True
    assert compile_record_predicate(record_data_filter="NOTES>50").matches(rec) is False
    assert compile_record_predicate(record_data_filter="NOTES>2026-01-01").matches(rec) is False

    # Date / datetime Python values in record_data
    dt_rec = _make_record(
        recordData={
            "DT_OBJ": datetime(2026, 2, 1, 12, 0, tzinfo=UTC),
            "DATE_OBJ": date(2026, 2, 1),
        }
    )
    assert compile_record_predicate(record_data_filter="DT_OBJ>=2026-02-01T00:00:00Z").matches(
        dt_rec
    )
    assert compile_record_predicate(record_data_filter="DATE_OBJ==2026-02-01").matches(dt_rec)

    # recordDataFilter="none" strips record_data without mutating original Record
    proj_pred = compile_record_predicate(
        extra_filters={"recordDataFilter": "none", "record_id": 10}
    )
    projected = proj_pred.apply(rec)
    assert projected is not None
    assert projected.record_data is None
    assert rec.record_data["AGE"] == 45
    assert projected.record_status == RecordStatus.RECORD_COMPLETE


def test_compile_record_predicate_strict_rest_contract_parity() -> None:
    """Verify exact case-sensitive keys, strict booleans, literal quotes, and record.deleted."""
    # Requirement 1: Exact case-sensitive membership in recordData without dotted splitting
    rec_keys = _make_record(
        recordData={
            "AESER": "Serious",
            "aeTerm": "Headache",
            "A": {"B": "val"},
            "FLAT.KEY": "val",
            "DOSE": 10,
            "VISIT_CODE": "2026-02-01",
        }
    )
    assert compile_record_predicate(record_data_filter="aeser==Serious").matches(rec_keys) is False
    assert compile_record_predicate(record_data_filter="AESER==Serious").matches(rec_keys) is True
    assert (
        compile_record_predicate(record_data_filter="ae_term==Headache").matches(rec_keys) is False
    )
    assert compile_record_predicate(record_data_filter="aeTerm==Headache").matches(rec_keys) is True
    assert compile_record_predicate(record_data_filter="A.B==val").matches(rec_keys) is False
    assert compile_record_predicate(record_data_filter="FLAT.KEY==val").matches(rec_keys) is True

    # Requirement 2: Strict lowercase unquoted booleans and literal quoted strings
    assert (
        compile_record_predicate(extra_filters={"filter": "deleted==FALSE"}).matches(rec_keys)
        is False
    )
    assert (
        compile_record_predicate(extra_filters={"filter": 'deleted=="false"'}).matches(rec_keys)
        is False
    )
    assert (
        compile_record_predicate(extra_filters={"filter": "deleted==false"}).matches(rec_keys)
        is True
    )
    assert compile_record_predicate(record_data_filter='DOSE=="010.00"').matches(rec_keys) is False
    assert compile_record_predicate(record_data_filter="DOSE==010.00").matches(rec_keys) is True
    assert (
        compile_record_predicate(record_data_filter='VISIT_CODE=="2026-02-01T00:00:00Z"').matches(
            rec_keys
        )
        is False
    )

    # Requirement 4 & 5: Direct record.deleted lookup and verbatim recordStatus matching
    active_with_deleted_status = _make_record(deleted=False, recordStatus="deleted")
    assert (
        compile_record_predicate(extra_filters={"filter": "deleted==false"}).matches(
            active_with_deleted_status
        )
        is True
    )
    assert (
        compile_record_predicate(extra_filters={"filter": "deleted==true"}).matches(
            active_with_deleted_status
        )
        is False
    )

    unset_deleted_rec = _make_record(deleted=None, recordStatus="COMPLETE")
    assert (
        compile_record_predicate(extra_filters={"filter": "deleted==false"}).matches(
            unset_deleted_rec
        )
        is False
    )
    assert (
        compile_record_predicate(extra_filters={"filter": "deleted!=false"}).matches(
            unset_deleted_rec
        )
        is True
    )
    assert (
        compile_record_predicate(extra_filters={"filter": "recordStatus==COMPLETE"}).matches(
            unset_deleted_rec
        )
        is True
    )
    assert (
        compile_record_predicate(
            extra_filters={"filter": 'recordStatus=="Record Complete"'}
        ).matches(unset_deleted_rec)
        is False
    )


@pytest.mark.parametrize(
    ("extra_filters", "record_data_filter"),
    [
        ({"filter": "siteId==1,formId==10;subjectKey==S1"}, None),
        ({"filter": 'subjectKey=="unclosed'}, None),
        ({"subjectKey": ("=~", "SUBJ")}, None),
        (None, "AGE==1;WEIGHT==2,SMOKER==false"),
        (None, "NOTES=~[unclosed"),
        (None, "NESTED..BAD==1"),
    ],
)
def test_compile_record_predicate_rejects_invalid_expressions(
    extra_filters: dict[str, object] | None,
    record_data_filter: str | None,
) -> None:
    """Invalid syntax, unclosed quotes, mixed connectors, or bad regex raise ValueError."""
    with pytest.raises(ValueError):
        compile_record_predicate(
            extra_filters=extra_filters,
            record_data_filter=record_data_filter,
        )
