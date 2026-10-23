"""Unit tests for drift postman."""

import json
import logging
import os
from pathlib import Path

import pytest

from imednet.models.engine import ModelEngine, get_contract
from imednet.sdk import ImednetSDK

logger = logging.getLogger(__name__)


def test_postman_collection_drift(sdk: ImednetSDK, study_key: str, monkeypatch: pytest.MonkeyPatch):
    """Reads the Postman collection, identifies API endpoints,.

    and compares the live API responses against the SDK's internal models.
    """
    monkeypatch.setenv("IMEDNET_STRICT_MODE", "1")

    collection_path = str(
        Path(__file__).resolve().parent.parent.parent / "imednet.postman_collection.json"
    )
    if not os.path.exists(collection_path):
        pytest.skip("Postman collection not found")

    with open(collection_path) as f:
        data = json.load(f)

    # Ensure dynamic contract is built
    contract = get_contract()

    # Walk the collection to find endpoints
    endpoints_to_test = set()

    def walk(items):
        """Helper function to walk."""
        for item in items:
            if "item" in item:
                walk(item["item"])
            elif "request" in item:
                req = item["request"]
                if "url" in req and "path" in req["url"]:
                    path = req["url"]["path"]
                    # Usually paths are like ['api', 'v1', 'studies'] or ['studies']
                    for p in path:
                        if p in contract.paths:
                            endpoints_to_test.add(p)

    walk(data.get("item", []))

    assert len(endpoints_to_test) > 0, "No endpoints found in Postman collection"

    for endpoint in endpoints_to_test:
        model_name = contract.paths[endpoint]
        model_cls = ModelEngine.get_model(model_name)

        # Convert camelCase to snake_case for SDK attribute lookup
        from imednet.models.contract import to_snake

        sdk_endpoint = to_snake(endpoint)

        # Build URL using the SDK's internal path builder
        endpoint_obj = getattr(sdk, sdk_endpoint)
        path = endpoint_obj._get_endpoint_path(study_key if sdk_endpoint != "studies" else None)

        try:
            response = sdk._client.get(path)
            data = response.json()
            items = data.get("recordData") if "recordData" in data else data.get("data", [])
            for item in items:
                # Validates using the internal model
                model_cls.from_json(item)
        except Exception as e:
            from imednet.errors import NotFoundError

            if isinstance(e, NotFoundError):
                logger.info(
                    f"Skipping drift check for {endpoint} as list path is not supported (404 NotFound)"
                )
                continue
            logger.warning(f"Drift detected in {endpoint}: {e}")
            raise
