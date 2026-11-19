"""HTTP client for interacting with the iMednet Form Designer backend."""

from __future__ import annotations

import json
from typing import Any

import httpx
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

from imednet.errors import ApiError, ClientError

from .models import Layout

trace: Any = None
try:
    from opentelemetry import trace as _trace

    trace = _trace
except ImportError:
    pass


def _is_rate_limited_status_error(exc: BaseException) -> bool:
    """Return True only for HTTP 429 Too Many Requests responses."""
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429


class FormDesignerClient:
    """Client for the iMedNet Form Designer endpoint.

    Handles the specific authentication and payload requirements of the legacy
    formdez_save.php endpoint.
    """

    def __init__(self, base_url: str, phpsessid: str, timeout: float = 30.0):
        """Initialize the client.

        Args:
            base_url: The base URL of the iMedNet instance (e.g., ``https://xyz.imednet.com``).
            phpsessid: The active PHP session ID from the browser.
            timeout: Request timeout in seconds.
        """
        self.base_url = base_url.rstrip("/")
        self.phpsessid = phpsessid
        self.timeout = timeout
        self.session = httpx.Client(timeout=timeout)

        if trace is not None:
            self._tracer = trace.get_tracer(__name__)
        else:
            self._tracer = None

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self.session.close()

    def __enter__(self) -> FormDesignerClient:
        """Enter the synchronous context manager."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exit the synchronous context manager and close the HTTP session."""
        self.close()

    def save_form(
        self,
        csrf_key: str,
        form_id: int,
        community_id: int,
        revision: int,
        layout: Layout,
    ) -> str:
        """Submit the form layout to the server.

        Args:
            csrf_key: The CSRF token (scraped from page).
            form_id: The ID of the form being edited.
            community_id: The study ID.
            revision: The NEXT revision number.
            layout: The Form Layout object.

        Returns:
            The raw response text from the server.

        Raises:
            httpx.HTTPStatusError: If the server returns a non-2xx status code.
            ClientError: If validation fails for the provided arguments.
            ApiError: If the server returns an error.
        """
        # --- Validation Logic Migrated from TUI ---
        if not csrf_key or not csrf_key.strip():
            raise ClientError("CSRF Key cannot be empty.")

        if form_id <= 0:
            raise ClientError(f"Invalid form_id: {form_id}. Must be a positive integer.")

        if community_id <= 0:
            raise ClientError(f"Invalid community_id: {community_id}. Must be a positive integer.")

        if revision < 0:
            raise ClientError(f"Invalid revision: {revision}. Must be non-negative.")
        # ------------------------------------------

        url = f"{self.base_url}/app/formdez/formdez_save.php"

        # Critical Headers
        headers = {
            "Cookie": f"PHPSESSID={self.phpsessid}",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "User-Agent": "iMedNet-SDK-FormBuilder/1.0",
        }

        # Serialize Layout
        # mode='json' ensures we get a JSON string compatible output
        # by_alias=True might be needed if we defined aliases,
        # but we used direct names matching schema
        layout_json = layout.model_dump_json(exclude_none=True)

        # Construct Payload
        # Note: We use a dict and let requests url-encode it
        payload = {
            "CSRFKey": csrf_key,
            "form_id": str(form_id),
            "community_id": str(community_id),
            "revision": str(revision),
            "layout": layout_json,
            "resubmit": "0",
            "quick_save": "1",
            "__internal_ajax_request": "1",
        }

        retryer = Retrying(
            stop=stop_after_attempt(4),
            retry=retry_if_exception(_is_rate_limited_status_error),
            wait=wait_exponential(multiplier=0.1),
            reraise=True,
        )

        def _execute_request() -> str:
            """Internal function to execute the POST request with session state.

            Returns:
                str: The raw response text.

            Raises:
                httpx.HTTPStatusError: If the server returns a non-2xx status.
                ApiError: If the server returns an application-level error.
            """
            response = self.session.post(url, data=payload, headers=headers)
            response.raise_for_status()

            # Check for application-level errors (often returned as 200 OK but with error text)
            # However, the requirement says "Signals the backend to return a JSON response"
            # So we should try to parse it.
            try:
                resp_data = response.json()
                # If it's JSON, it usually contains status info.
                # Example success: {"success": true, ...}
                # Example error: {"error": "..."}
                if isinstance(resp_data, dict) and resp_data.get("error"):
                    raise ApiError(
                        f"Server Error: {resp_data['error']}", status_code=response.status_code
                    )
            except json.JSONDecodeError as exc:
                # Fallback if not JSON (legacy endpoints sometimes return HTML on error)
                raise ApiError(response.text, status_code=response.status_code) from exc

            return response.text

        if self._tracer:
            with self._tracer.start_as_current_span(
                "form_designer.save_form",
                attributes={
                    "form_id": form_id,
                    "community_id": community_id,
                    "revision": revision,
                },
            ) as span:
                try:
                    result = retryer(_execute_request)
                    span.set_attribute("success", True)
                    if trace is not None:
                        span.set_status(trace.Status(trace.StatusCode.OK))
                    return result
                except Exception as e:
                    span.record_exception(e)
                    if trace is not None:
                        span.set_status(trace.Status(trace.StatusCode.ERROR))
                    span.set_attribute("success", False)
                    raise
        else:
            return retryer(_execute_request)
