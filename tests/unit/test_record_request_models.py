"""Unit and integration tests for normalized BaseRecordRequest models across Record writes."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError as PydanticValidationError

from imednet import AsyncImednetSDK, DataFrameLike, ImednetSDK, RecordsInput
from imednet.core.endpoint.operations.record_create import RecordCreateOperation
from imednet.errors import ValidationError
from imednet.models import UpdateRecordRequest as ModelsUpdateRecordRequest
from imednet.models.base import ImednetBaseModel
from imednet.models.records import (
    BaseRecordRequest,
    CreateNewRecordRequest,
    Record,
    RecordData,
    RecordStatus,
    RegisterSubjectRequest,
    UpdateRecordRequest,
    UpdateScheduledRecordRequest,
)
from imednet.models.variables import Variable
from imednet.spi.models import UpdateRecordRequest as SpiUpdateRecordRequest
from imednet.utils.tabular import (
    MASKED_SENTINEL,
    ensure_no_masked_sentinel,
    normalize_records_input,
)
from imednet.validation.cache import (
    AsyncSchemaValidator,
    SchemaCache,
    SchemaValidator,
    validate_record_entry,
    validate_records_batch,
)


def _make_populated_schema_cache(study_key: str = "STUDY") -> SchemaCache:
    cache = SchemaCache()
    cache.populate(
        [
            Variable(
                study_key=study_key,
                form_id=301,
                form_key="F_DM",
                variable_id=1,
                variable_name="V_AGE",
                variable_type="integer",
            ),
            Variable(
                study_key=study_key,
                form_id=302,
                form_key="F_VS",
                variable_id=2,
                variable_name="V_WEIGHT",
                variable_type="float",
            ),
        ],
        study_key=study_key,
    )
    return cache


def _make_capturing_sdks() -> tuple[ImednetSDK, AsyncImednetSDK, list[list[dict[str, Any]]]]:
    captured: list[list[dict[str, Any]]] = []
    batch_seq = 0

    def _handle_request(request: httpx.Request) -> httpx.Response:
        nonlocal batch_seq
        path = request.url.path
        if request.method == "POST" and path.endswith("/records"):
            body = json.loads(request.content.decode("utf-8"))
            captured.append(body)
            batch_seq += 1
            return httpx.Response(
                202,
                json={
                    "jobId": f"job-{batch_seq}",
                    "batchId": f"batch-{batch_seq}",
                    "state": "OPEN",
                },
            )
        if request.method == "GET" and "/variables" in path:
            return httpx.Response(
                200,
                json={
                    "metadata": {"status": "OK"},
                    "pagination": {
                        "currentPage": 0,
                        "size": 25,
                        "totalPages": 1,
                        "totalElements": 2,
                    },
                    "data": [
                        {
                            "studyKey": "STUDY",
                            "formId": 301,
                            "formKey": "F_DM",
                            "variableId": 1,
                            "variableName": "V_AGE",
                            "variableType": "integer",
                        },
                        {
                            "studyKey": "STUDY",
                            "formId": 302,
                            "formKey": "F_VS",
                            "variableId": 2,
                            "variableName": "V_WEIGHT",
                            "variableType": "float",
                        },
                    ],
                },
            )
        return httpx.Response(200, json={"data": []})

    sync_sdk = ImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://example.test",
        transport=httpx.MockTransport(_handle_request),
    )
    async_sdk = AsyncImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://example.test",
        transport=httpx.MockTransport(_handle_request),
    )
    return sync_sdk, async_sdk, captured


@pytest.mark.parametrize(
    "req_cls",
    [
        BaseRecordRequest,
        RegisterSubjectRequest,
        CreateNewRecordRequest,
        UpdateScheduledRecordRequest,
        UpdateRecordRequest,
    ],
)
def test_record_request_models_expose_all_14_attributes_camel_dump_and_forbid_extra(
    req_cls: type[BaseRecordRequest],
) -> None:
    """AC 4 & Req 2-5: All 14 REST write attributes serialize as camelCase, omit None, and forbid extra."""
    assert ModelsUpdateRecordRequest is UpdateRecordRequest
    assert SpiUpdateRecordRequest is UpdateRecordRequest
    # Guardrail: ImednetBaseModel retains extra="ignore" for GET response models
    assert ImednetBaseModel.model_config.get("extra") == "ignore"
    rec_response = Record.model_validate({"recordId": 1, "undocumentedUpstreamAttr": "ok"})
    assert rec_response.record_id == 1

    # Default model_dump() omits unset None attributes and emits empty data dict
    empty_req = req_cls()
    assert empty_req.model_dump() == {"data": {}}

    # Blank/whitespace optional string identifiers omit cleanly for server-assigned keys
    blank_req = req_cls(
        formKey="F_DM",
        siteName="   ",
        siteId=101,
        subjectKey="",
        subjectOid="  ",
        intervalName="",
        recordOid=" ",
        recordStatus="   ",
        reasonForChange="",
    )
    assert blank_req.model_dump() == {"formKey": "F_DM", "siteId": 101, "data": {}}

    # Full snake_case construction emits all 14 camelCase attributes + data
    full_snake = req_cls(
        form_key="F_VS",
        form_id=302,
        site_name="SITE-01",
        site_id=10,
        subject_key="SUBJ-01",
        subject_id=201,
        subject_oid="SUBJ-OID-201",
        interval_name="Baseline",
        interval_id=501,
        visit_id=401,
        record_id=602,
        record_oid="REC-OID-602",
        record_status=RecordStatus.RECORD_COMPLETE,
        reason_for_change="Confirmed chart values",
        data={"V_WEIGHT": 71.5},
    )
    expected_camel = {
        "formKey": "F_VS",
        "formId": 302,
        "siteName": "SITE-01",
        "siteId": 10,
        "subjectKey": "SUBJ-01",
        "subjectId": 201,
        "subjectOid": "SUBJ-OID-201",
        "intervalName": "Baseline",
        "intervalId": 501,
        "visitId": 401,
        "recordId": 602,
        "recordOid": "REC-OID-602",
        "recordStatus": "Record Complete",
        "reasonForChange": "Confirmed chart values",
        "data": {"V_WEIGHT": 71.5},
    }
    dumped = full_snake.model_dump()
    assert dumped == expected_camel
    assert type(dumped["recordStatus"]) is str

    # Full camelCase construction produces identical payload
    full_camel = req_cls(**expected_camel)
    assert full_camel.model_dump() == expected_camel

    # Explicit caller overrides on model_dump are honored
    snake_dumped = full_snake.model_dump(by_alias=False)
    assert "form_key" in snake_dumped
    assert "reason_for_change" in snake_dumped
    assert "formKey" not in snake_dumped

    partial = req_cls(recordId=602, reasonForChange="Audit")
    with_nones = partial.model_dump(exclude_none=False)
    assert with_nones["recordId"] == 602
    assert with_nones["formKey"] is None
    assert with_nones["visitId"] is None

    # Undeclared keys are rejected with extra="forbid"
    with pytest.raises(PydanticValidationError):
        req_cls.model_validate({"formKey": "F_DM", "unknownAttribute": "bad"})


@pytest.mark.parametrize(
    ("attr_name", "attr_value"),
    [
        ("formKey", MASKED_SENTINEL),
        ("siteName", MASKED_SENTINEL),
        ("subjectKey", f"prefix-{MASKED_SENTINEL}"),
        ("subjectOid", MASKED_SENTINEL),
        ("intervalName", MASKED_SENTINEL),
        ("recordOid", MASKED_SENTINEL),
        ("recordStatus", MASKED_SENTINEL),
        ("reasonForChange", f"Reason {MASKED_SENTINEL}"),
    ],
)
def test_ensure_no_masked_sentinel_blocks_top_level_and_data_attributes_on_requests(
    attr_name: str,
    attr_value: str,
) -> None:
    """AC 3 & Req 7: ensure_no_masked_sentinel blocks ***MASKED*** across BaseRecordRequest fields."""
    req_top = BaseRecordRequest.model_validate({attr_name: attr_value, "data": {"V_AGE": 40}})
    with pytest.raises(ValidationError, match=r"contains masked PHI sentinel"):
        ensure_no_masked_sentinel([req_top])
    with pytest.raises(ValidationError, match=r"contains masked PHI sentinel"):
        normalize_records_input((req_top,))

    req_data = UpdateRecordRequest(
        recordId=602,
        data=RecordData({"V_WEIGHT": [70.0, MASKED_SENTINEL]}),
    )
    with pytest.raises(ValidationError, match=r"variable 'V_WEIGHT' contains masked PHI sentinel"):
        ensure_no_masked_sentinel([req_data])


def test_normalize_records_input_maps_all_14_snake_case_identifiers_to_camel_case() -> None:
    """Req 7: normalize_records_input maps snake_case _IDENTIFIER_COLUMNS keys to camelCase."""
    raw_snake_mapping = {
        "record_id": 602,
        "record_oid": "REC-602",
        "visit_id": 401,
        "record_status": "Complete",
        "reason_for_change": "Source verification",
        "form_key": "F_VS",
        "form_id": 302,
        "subject_key": "SUB1",
        "subject_oid": "SUB-OID-1",
        "subject_id": 201,
        "site_name": "SITE",
        "site_id": 101,
        "interval_name": "Baseline",
        "interval_id": 501,
        "data": {"V_WEIGHT": 70.5},
    }
    normalized = normalize_records_input([raw_snake_mapping])
    assert normalized == [
        {
            "recordId": 602,
            "recordOid": "REC-602",
            "visitId": 401,
            "recordStatus": "Complete",
            "reasonForChange": "Source verification",
            "formKey": "F_VS",
            "formId": 302,
            "subjectKey": "SUB1",
            "subjectOid": "SUB-OID-1",
            "subjectId": 201,
            "siteName": "SITE",
            "siteId": 101,
            "intervalName": "Baseline",
            "intervalId": 501,
            "data": {"V_WEIGHT": 70.5},
        }
    ]

    with pytest.raises(TypeError, match="BaseRecordRequest or Mapping"):
        normalize_records_input([123])  # type: ignore[list-item]


@pytest.mark.asyncio
async def test_all_12_sync_and_async_record_write_methods_and_validators_accept_request_sequences() -> (
    None
):
    """AC 2 & Req 8: Lists and tuples of all 5 request models succeed across all 12 write methods and validators."""
    schema = _make_populated_schema_cache("STUDY")
    request_items: list[BaseRecordRequest] = [
        BaseRecordRequest(form_key="F_DM", site_name="SITE", data={"V_AGE": 34}),
        RegisterSubjectRequest(formKey="F_DM", siteId=101, data={"V_AGE": 42}),
        CreateNewRecordRequest(
            form_key="F_VS",
            subject_key="SUB1",
            record_status=RecordStatus.INCOMPLETE,
            data={"V_WEIGHT": 68.4},
        ),
        UpdateScheduledRecordRequest(
            formId=302,
            subjectId=201,
            intervalId=501,
            visitId=401,
            data={"V_WEIGHT": 71.2},
        ),
        UpdateRecordRequest(
            record_id=602,
            record_oid="REC-OID-602",
            record_status=RecordStatus.COMPLETE,
            reason_for_change="Verified vitals",
            data={"V_WEIGHT": 72.0},
        ),
    ]
    expected_wire_payload = [item.model_dump() for item in request_items]

    # Direct validation & workflow preflight helpers
    for idx, req in enumerate(request_items):
        assert validate_record_entry(schema, req, study_key="STUDY", record_index=idx) is None
        report = validate_record_entry(
            schema, req, study_key="STUDY", collect_errors=True, record_index=idx
        )
        assert report.is_valid is True

    batch_report = validate_records_batch(schema, tuple(request_items), study_key="STUDY")
    assert batch_report.is_valid is True
    assert batch_report.valid_records == 5

    try:
        import imednet_workflows.record_update as wf_mod
    except ModuleNotFoundError:
        wf_mod = None

    if wf_mod is not None:
        assert wf_mod._extract_record_form_ref(request_items[0]) == ("F_DM", None)
        assert wf_mod._extract_record_form_ref(request_items[3]) == (None, 302)
        assert wf_mod._extract_record_form_ref(request_items[4]) == (None, None)

    sync_sdk, async_sdk, captured = _make_capturing_sdks()
    try:
        # SchemaValidator & AsyncSchemaValidator direct calls
        sync_validator = SchemaValidator(sync_sdk)
        sync_validator.validate_record("STUDY", request_items[0])
        assert sync_validator.validate_batch("STUDY", tuple(request_items), collect_errors=True)

        async_validator = AsyncSchemaValidator(async_sdk)
        await async_validator.validate_record("STUDY", request_items[0])
        assert await async_validator.validate_batch(
            "STUDY", tuple(request_items), collect_errors=True
        )

        # Direct RecordCreateOperation instantiation with BaseRecordRequest tuple
        op: RecordCreateOperation[Any] = RecordCreateOperation(
            "/api/v1/edc/studies/STUDY/records",
            tuple(request_items),
            schema=schema,
            study_key="STUDY",
        )
        assert op.records_data == expected_wire_payload

        sync_wf = wf_mod.RecordUpdateWorkflow(sync_sdk) if wf_mod is not None else None
        async_wf = wf_mod.AsyncRecordUpdateWorkflow(async_sdk) if wf_mod is not None else None
        if sync_wf is not None and async_wf is not None:
            sync_wf._validate_form_key("STUDY", tuple(request_items))
            await async_wf._async_validate_form_key("STUDY", tuple(request_items))

        # Execute all canonical sync & async Record write methods with both list and tuple inputs
        for seq_input in (request_items, tuple(request_items)):
            before_count = len(captured)

            # 1-2: RecordsEndpoint.create & create_batch
            assert sync_sdk.records.create("STUDY", seq_input, schema=schema).batch_id
            assert (
                sync_sdk.records.create_batch("STUDY", seq_input, schema=schema).total_records == 5
            )

            # 3-4: AsyncRecordsEndpoint.create & create_batch
            assert (await async_sdk.records.create("STUDY", seq_input, schema=schema)).batch_id
            assert (
                await async_sdk.records.create_batch("STUDY", seq_input, schema=schema)
            ).total_records == 5

            # 5-6: ImednetSDK.create_record & create_records_batch
            assert sync_sdk.create_record("STUDY", seq_input, schema=schema).batch_id
            assert (
                sync_sdk.create_records_batch("STUDY", seq_input, schema=schema).total_records == 5
            )

            # 7-8: AsyncImednetSDK.create_record & create_records_batch
            assert (await async_sdk.create_record("STUDY", seq_input, schema=schema)).batch_id
            assert (
                await async_sdk.create_records_batch("STUDY", seq_input, schema=schema)
            ).total_records == 5

            expected_calls = 8
            if sync_wf is not None and async_wf is not None:
                # 9-10: RecordUpdateWorkflow.create_or_update_records & create_or_update_records_batch
                assert sync_wf.create_or_update_records("STUDY", seq_input).batch_id
                assert sync_wf.create_or_update_records_batch("STUDY", seq_input).total_records == 5

                # 11-12: AsyncRecordUpdateWorkflow.create_or_update_records & create_or_update_records_batch
                assert (await async_wf.create_or_update_records("STUDY", seq_input)).batch_id
                assert (
                    await async_wf.create_or_update_records_batch("STUDY", seq_input)
                ).total_records == 5
                expected_calls = 12

            new_payloads = captured[before_count:]
            assert len(new_payloads) == expected_calls
            for payload in new_payloads:
                assert payload == expected_wire_payload
    finally:
        sync_sdk.close()
        await async_sdk.aclose()


def test_dataframe_like_protocol_and_strict_mypy_scalar_rejection() -> None:
    """AC 1 & Req 1: DataFrameLike matches tabular objects only and mypy --strict rejects scalars."""

    class _MinimalFrame:
        @property
        def columns(self) -> list[str]:
            return ["formKey"]

        def to_dict(self) -> list[dict[str, Any]]:
            return [{"formKey": "F_DM"}]

    assert isinstance(_MinimalFrame(), DataFrameLike)
    assert not isinstance({"formKey": "F_DM"}, DataFrameLike)
    assert not isinstance(BaseRecordRequest(formKey="F_DM"), DataFrameLike)
    assert not isinstance(123, DataFrameLike)
    assert RecordsInput is not Any

    repo_root = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--strict",
            "--warn-unused-ignores",
            str(repo_root / "tests" / "typing" / "endpoint_contracts.py"),
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"mypy --strict --warn-unused-ignores failed:\n{proc.stdout}\n{proc.stderr}"
    )
