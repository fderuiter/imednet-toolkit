"""Regressions for the scheduled Coding drift and leaked strict-mode setting."""

import os
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError
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
