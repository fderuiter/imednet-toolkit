"""Integration test verifying zero cross-Study HTTP requests against a local loopback server."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from apache_airflow_providers_imednet.sensors import ImednetJobSensor
from imednet import ImednetSDK, cli
from imednet.errors import ClientError, PathTraversalValidationError


def test_loopback_server_records_zero_cross_study_requests(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """Verify traversal/truncation proof vectors never reach a loopback HTTP server."""
    captured_requests: list[dict[str, Any]] = []

    class RecordingHandler(BaseHTTPRequestHandler):
        """Record any incoming HTTP request paths."""

        def do_GET(self) -> None:  # noqa: N802
            captured_requests.append({"method": "GET", "path": self.path})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"data": [], "pagination": {"totalPages": 1}}).encode())

        def do_POST(self) -> None:  # noqa: N802
            captured_requests.append({"method": "POST", "path": self.path})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"batchId": "B1"}).encode())

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), RecordingHandler)
    port = server.server_address[1]
    base_url = f"http://127.0.0.1:{port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        monkeypatch.setenv("IMEDNET_API_KEY", "test-key")
        monkeypatch.setenv("IMEDNET_SECURITY_KEY", "test-secret")
        monkeypatch.setenv("IMEDNET_BASE_URL", base_url)
        monkeypatch.setenv(
            "AIRFLOW_CONN_IMEDNET_DEFAULT", f"http://test-key:test-secret@127.0.0.1:{port}"
        )

        sdk = ImednetSDK(api_key="test-key", security_key="test-secret", base_url=base_url)
        try:
            sensor = ImednetJobSensor(
                task_id="verify_job_sensor",
                study_key="ALLOWED_STUDY",
                batch_id="../../CANARY_STUDY/records",
            )
            with pytest.raises(ClientError):
                sensor.poke({})

            with pytest.raises(PathTraversalValidationError):
                sdk.jobs.get("ALLOWED_STUDY", "../../CANARY_STUDY/records")

            with pytest.raises(PathTraversalValidationError):
                sdk.jobs.get("ALLOWED_STUDY", "%2e%2e/%2e%2e/CANARY_STUDY/records")

            with pytest.raises(PathTraversalValidationError):
                list(sdk.sites.list("CANARY_STUDY/records#"))

            with pytest.raises(PathTraversalValidationError):
                list(sdk.sites.list("CANARY_STUDY/users?"))

            with pytest.raises(PathTraversalValidationError):
                sdk.studies.get("../users")

            with pytest.raises(SystemExit) as exc_info:
                cli.app(
                    [
                        "jobs",
                        "status",
                        "ALLOWED_STUDY",
                        "../../CANARY_STUDY/records",
                        "--format",
                        "json",
                    ]
                )
            assert exc_info.value.code == 1
            captured = capfd.readouterr()
            assert captured.out == ""

            assert captured_requests == []
            assert not any(
                r["path"] == "/api/v1/edc/studies/CANARY_STUDY/records" for r in captured_requests
            )
            assert not any(
                r["path"] == "/api/v1/edc/studies/CANARY_STUDY/users" for r in captured_requests
            )
        finally:
            sdk.close()
    finally:
        server.shutdown()
        server.server_close()
