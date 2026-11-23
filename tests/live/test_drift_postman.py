"""Unit tests for drift postman."""

import json
import logging
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from imednet.errors import NotFoundError
from imednet.models.contract import to_snake
from imednet.models.engine import ModelEngine, get_contract
from imednet.sdk import ImednetSDK
from tests.utils.verification_reporter import VerificationReporter

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

    with open(collection_path, encoding="utf-8") as f:
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

    ordered_endpoints = sorted(endpoints_to_test)
    checked_endpoints = 0
    skipped_endpoints = 0
    runtime_errors: list[tuple[str, Exception]] = []
    drift_messages: list[str] = []

    with VerificationReporter(
        "drift",
        passed_summary=f"Validated {len(ordered_endpoints)} Postman endpoints with zero schema drift.",
        failed_summary="Detected API contract drift or endpoint failures across Postman endpoints.",
    ) as reporter:
        for endpoint in ordered_endpoints:
            model_name = contract.paths[endpoint]
            model_cls = ModelEngine.get_model(model_name)
            sdk_endpoint = to_snake(endpoint)

            endpoint_obj = getattr(sdk, sdk_endpoint)
            path = endpoint_obj._get_endpoint_path(study_key if sdk_endpoint != "studies" else None)

            try:
                response = sdk._client.get(path)
                payload = response.json()
                if not isinstance(payload, dict):
                    msg = f"{endpoint}: expected JSON object payload, got {type(payload).__name__}"
                    logger.warning(f"Drift detected in {msg}")
                    reporter.add_violation("schema_drift", msg)
                    drift_messages.append(msg)
                    checked_endpoints += 1
                    continue

                items = (
                    payload.get("recordData")
                    if "recordData" in payload
                    else payload.get("data", [])
                )
                if not isinstance(items, list):
                    msg = f"{endpoint}: expected list of items, got {type(items).__name__}"
                    logger.warning(f"Drift detected in {msg}")
                    reporter.add_violation("schema_drift", msg)
                    drift_messages.append(msg)
                    checked_endpoints += 1
                    continue

                checked_endpoints += 1
                for idx, item in enumerate(items):
                    try:
                        model_cls.from_json(item)
                    except ValidationError as exc:
                        msg = f"{endpoint}[{idx}] ({model_name}): {exc}"
                        logger.warning(f"Drift detected in {msg}")
                        reporter.add_violation("schema_drift", msg)
                        drift_messages.append(msg)
            except NotFoundError:
                skipped_endpoints += 1
                logger.info(
                    f"Skipping drift check for {endpoint} as list path is not supported (404 NotFound)"
                )
            except ValidationError as exc:
                checked_endpoints += 1
                msg = f"{endpoint} ({model_name}): {exc}"
                logger.warning(f"Drift detected in {msg}")
                reporter.add_violation("schema_drift", msg)
                drift_messages.append(msg)
            except Exception as exc:
                checked_endpoints += 1
                msg = f"{endpoint}: {exc}"
                logger.warning(f"Drift detected in {msg}")
                reporter.add_violation("endpoint_error", msg)
                drift_messages.append(msg)
                runtime_errors.append((endpoint, exc))

        reporter.add_metric("endpoints_discovered", float(len(ordered_endpoints)), unit="count")
        reporter.add_metric("endpoints_checked", float(checked_endpoints), unit="count")
        reporter.add_metric("endpoints_skipped", float(skipped_endpoints), unit="count")

        if drift_messages:
            reporter.set_summary(
                f"Detected {len(drift_messages)} drift/endpoint violation(s) across "
                f"{checked_endpoints} checked Postman endpoints."
            )
            if runtime_errors:
                first_exc = runtime_errors[0][1]
                combined = "; ".join(f"{ep}: {err}" for ep, err in runtime_errors)
                raise RuntimeError(combined) from first_exc
            raise AssertionError(
                "Postman schema drift detected:\n" + "\n".join(f"- {m}" for m in drift_messages)
            )

        reporter.set_summary(
            f"Validated {checked_endpoints} Postman endpoints ({skipped_endpoints} skipped 404) with zero drift."
        )
