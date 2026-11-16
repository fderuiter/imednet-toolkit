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
