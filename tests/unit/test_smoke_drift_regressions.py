"""Regressions for the scheduled Coding drift and leaked strict-mode setting."""

import os
from unittest.mock import MagicMock

import pytest
from pydantic import Field, ValidationError
from tests.live.test_drift_postman import test_postman_collection_drift as run_drift_check

from imednet.models.codings import Coding
from imednet.models.engine import ModelEngine


@pytest.mark.parametrize("model", [Coding, ModelEngine.get_model("Coding")])
@pytest.mark.parametrize("timestamp", ["2026-09-24 15:23:49", None])
def test_coding_accepts_last_updated_in_strict_mode(monkeypatch, model, timestamp):
    monkeypatch.setenv("IMEDNET_STRICT_MODE", "1")
    coding = model.from_json({"lastUpdated": timestamp})
    assert coding.last_updated == timestamp
    assert coding.model_dump(by_alias=True)["lastUpdated"] == timestamp


@pytest.mark.parametrize("model", [Coding, ModelEngine.get_model("Coding")])
def test_coding_timestamp_is_optional_without_weakening_drift_detection(monkeypatch, model):
    monkeypatch.setenv("IMEDNET_STRICT_MODE", "1")
    assert model.from_json({}).last_updated is None
    with pytest.raises(ValidationError, match="futureUnknownField"):
        model.from_json({"lastUpdated": None, "futureUnknownField": True})


@pytest.mark.parametrize("previous", [None, "0", "1"])
@pytest.mark.parametrize("fails", [False, True])
def test_drift_check_restores_strict_mode_after_success_or_failure(monkeypatch, previous, fails):
    if previous is None:
        monkeypatch.delenv("IMEDNET_STRICT_MODE", raising=False)
    else:
        monkeypatch.setenv("IMEDNET_STRICT_MODE", previous)
    sdk = MagicMock()
    sdk._client.get.return_value.json.return_value = {"data": []}
    if fails:
        sdk._client.get.side_effect = RuntimeError("Synthetic API failure")

    with pytest.MonkeyPatch.context() as scoped:
        if fails:
            with pytest.raises(RuntimeError, match="Synthetic API failure"):
                run_drift_check(sdk, "SYNTHETIC", scoped)
        else:
            run_drift_check(sdk, "SYNTHETIC", scoped)
        assert os.environ["IMEDNET_STRICT_MODE"] == "1"

    assert os.environ.get("IMEDNET_STRICT_MODE") == previous


def test_field_metadata_cache_and_computed_fields():
    """Verify that field metadata is cached and includes aliases, computed fields, and required fields."""
    from pydantic import computed_field

    from imednet.models.base import _FIELD_METADATA_CACHE, ImednetBaseModel, _get_field_metadata

    class CustomModel(ImednetBaseModel):
        req_field: str = Field(..., alias="reqField")
        opt_field: str | None = Field(default=None, alias="optField")

        @computed_field
        def calc_field(self) -> str:
            return "computed"

    # Before first validation or retrieval, CustomModel should not be in cache
    _FIELD_METADATA_CACHE.pop(CustomModel, None)
    assert CustomModel not in _FIELD_METADATA_CACHE

    defined, required = _get_field_metadata(CustomModel)
    assert CustomModel in _FIELD_METADATA_CACHE
    assert "req_field" in defined
    assert "reqField" in defined
    assert "opt_field" in defined
    assert "optField" in defined
    assert "calc_field" in defined
    assert ("req_field", "reqField") in required
    assert ("opt_field", "optField") not in required

    # Validation should read from cache and succeed
    instance = CustomModel.model_validate({"reqField": "value", "calc_field": "computed"})
    assert instance.req_field == "value"


def test_dynamic_runtime_model_metadata_caching():
    """Verify that dynamically constructed models compute and cache metadata on first instantiation."""
    from pydantic import create_model

    from imednet.models.base import _FIELD_METADATA_CACHE, ImednetBaseModel

    dynamic_model = create_model(
        "DynamicRecord",
        dynamic_id=(str, Field(..., alias="dynamicId")),
        tag=(str, "default"),
        __base__=ImednetBaseModel,
    )

    assert dynamic_model not in _FIELD_METADATA_CACHE

    instance = dynamic_model.model_validate({"dynamicId": "dyn_123"})
    assert dynamic_model in _FIELD_METADATA_CACHE
    assert instance.dynamic_id == "dyn_123"

    defined, required = _FIELD_METADATA_CACHE[dynamic_model]
    assert "dynamic_id" in defined
    assert "dynamicId" in defined
    assert "tag" in defined
    assert ("dynamic_id", "dynamicId") in required


def test_precompiled_normalizers_and_forward_ref_rebuild():
    """Verify __pydantic_init_subclass__ precompiles _NORMALIZERS while keeping _FIELD_METADATA_CACHE lazy and resolving forward refs."""
    from pydantic import create_model

    from imednet.models.base import (
        _FIELD_METADATA_CACHE,
        _NORMALIZERS,
        ImednetBaseModel,
        _extract_single_item,
        _optional_int,
        _optional_str,
    )

    class PrecompiledSubModel(ImednetBaseModel):
        item_id: int | None = Field(default=None, alias="itemId")
        label: str | None = Field(default=None, alias="label")

    # _NORMALIZERS is precompiled immediately on subclass creation; _FIELD_METADATA_CACHE stays lazy
    assert PrecompiledSubModel in _NORMALIZERS
    assert _NORMALIZERS[PrecompiledSubModel]["item_id"] is _optional_int
    assert _NORMALIZERS[PrecompiledSubModel]["label"] is _optional_str
    assert PrecompiledSubModel not in _FIELD_METADATA_CACHE

    dyn_cls = create_model(
        "PrecompiledDynamicModel",
        code=(str | None, Field(default=None, alias="code")),
        __base__=ImednetBaseModel,
    )
    assert dyn_cls in _NORMALIZERS
    assert _NORMALIZERS[dyn_cls]["code"] is _optional_str
    assert dyn_cls not in _FIELD_METADATA_CACHE

    # Forward-ref model resolves nested BaseModel normalizer after model_rebuild
    ns: dict[str, object] = {"ImednetBaseModel": ImednetBaseModel}
    exec(
        "from __future__ import annotations\n"
        "class ForwardParent(ImednetBaseModel):\n"
        "    child: ForwardChild\n"
        "class ForwardChild(ImednetBaseModel):\n"
        "    val: int\n",
        ns,
    )
    forward_parent = ns["ForwardParent"]
    assert isinstance(forward_parent, type) and issubclass(forward_parent, ImednetBaseModel)
    assert forward_parent not in _NORMALIZERS
    forward_parent.model_rebuild(_types_namespace=ns)
    parsed = forward_parent.from_json({"child": [{"val": 7}]})
    assert parsed.child.val == 7
    assert _NORMALIZERS[forward_parent]["child"] is _extract_single_item


def test_detect_drift_gates_additive_checks_by_debug_extra_and_strict(caplog, monkeypatch):
    """Verify additive drift checks skip at default log levels on extra='ignore', log deduplicated DEBUG notices when enabled, and warn on extra!='ignore'."""
    import logging

    from pydantic import ConfigDict

    from imednet.models.base import ImednetBaseModel, _drift_reported

    class IgnoredExtraModel(ImednetBaseModel):
        req_id: int = Field(..., alias="reqId")

    class AllowedExtraModel(ImednetBaseModel):
        model_config = ConfigDict(extra="allow")
        req_id: int = Field(default=1, alias="reqId")

    monkeypatch.setenv("IMEDNET_STRICT_MODE", "0")
    drift_logger = logging.getLogger("imednet.drift")
    orig_level = drift_logger.level

    try:
        # 1. Default WARNING level on extra='ignore' skips additive drift notice
        _drift_reported.clear()
        caplog.clear()
        drift_logger.setLevel(logging.WARNING)
        with caplog.at_level(logging.WARNING, logger="imednet.drift"):
            inst = IgnoredExtraModel.model_validate({"reqId": 1, "newApiField": "x"})
            assert inst.req_id == 1
        assert "newApiField" not in caplog.text

        # 2. Enabling DEBUG on imednet.drift logs deduplicated DEBUG additive drift notice
        _drift_reported.clear()
        caplog.clear()
        drift_logger.setLevel(logging.DEBUG)
        with caplog.at_level(logging.DEBUG, logger="imednet.drift"):
            IgnoredExtraModel.model_validate({"reqId": 1, "newApiField": "x"})
            IgnoredExtraModel.model_validate({"reqId": 2, "newApiField": "y"})

        debug_records = [
            r
            for r in caplog.records
            if r.name == "imednet.drift"
            and r.levelno == logging.DEBUG
            and "Drift detected (additive): IgnoredExtraModel received unexpected fields: newApiField"
            in r.message
        ]
        assert len(debug_records) == 1

        # 3. extra != 'ignore' logs deduplicated WARNING even when DEBUG is enabled or disabled
        _drift_reported.clear()
        caplog.clear()
        drift_logger.setLevel(logging.WARNING)
        with caplog.at_level(logging.WARNING, logger="imednet.drift"):
            AllowedExtraModel.model_validate({"reqId": 1, "extraAllowedField": "v1"})
            AllowedExtraModel.model_validate({"reqId": 2, "extraAllowedField": "v2"})

        warn_records = [
            r
            for r in caplog.records
            if r.name == "imednet.drift"
            and r.levelno == logging.WARNING
            and "Drift detected (additive): AllowedExtraModel received unexpected fields: extraAllowedField"
            in r.message
        ]
        assert len(warn_records) == 1

        # 4. Destructive drift still warns when required field/alias is missing from data
        _drift_reported.clear()
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="imednet.drift"):
            with pytest.raises(ValidationError):
                IgnoredExtraModel.model_validate({})
        assert (
            "Drift detected (destructive): IgnoredExtraModel missing required fields: req_id"
            in caplog.text
        )
    finally:
        drift_logger.setLevel(orig_level)
