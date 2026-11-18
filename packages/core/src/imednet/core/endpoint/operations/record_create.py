"""Operation for creating records.

Encapsulates validation, header construction, and execution logic for record creation.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Generic, TypeVar

from imednet.constants import HEADER_EMAIL_NOTIFY
from imednet.core.context import get_study_context
from imednet.core.protocols import AsyncRequesterProtocol, RequesterProtocol
from imednet.utils.job_ledger import record_study_job
from imednet.utils.security import validate_header_value
from imednet.validation.cache import BaseSchemaCache, validate_record_entry

T = TypeVar("T")

_STUDY_PATH_RE = re.compile(r"/studies/([^/]+)/records(?:/|$)")


class RecordCreateOperation(Generic[T]):
    """Operation for creating records.

    Handles validation against schema and security checks for headers
    before executing the creation request.
    """

    def __init__(
        self,
        path: str,
        records_data: list[dict[str, Any]],
        email_notify: bool | str | None = None,
        schema: BaseSchemaCache[Any] | None = None,
        *,
        study_key: str | None = None,
    ) -> None:
        """Initialize the record creation operation.

        Args:
            path: The full API path for the request.
            records_data: List of record data objects to create.
            email_notify: Email notification setting.
            schema: Optional schema cache for validation.
            study_key: Optional study key associated with the record batch.

        Raises:
            ValidationError: If record data is invalid against the schema.
            ClientError: If email_notify contains invalid characters.
        """
        self.path = path
        self.records_data = records_data
        self.email_notify = email_notify
        self.schema = schema
        self.study_key = study_key

        self._validate()
        self.headers = self._build_headers()

    def _resolve_study_key(self) -> str | None:
        """Resolve the study key from explicit arg, request path, or study context."""
        if self.study_key:
            return self.study_key
        match = _STUDY_PATH_RE.search(self.path)
        if match:
            return match.group(1)
        return get_study_context()

    def _validate(self) -> None:
        """Validate records against schema if provided."""
        if self.schema is not None:
            for rec in self.records_data:
                validate_record_entry(self.schema, rec)

    def _build_headers(self) -> dict[str, str]:
        """Build headers for record creation request.

        Returns:
            Dictionary of headers.

        Raises:
            ClientError: If email_notify contains newlines.
        """
        headers: dict[str, str] = {}
        if self.email_notify is not None:
            if isinstance(self.email_notify, str):
                validate_header_value(self.email_notify)
                headers[HEADER_EMAIL_NOTIFY] = self.email_notify
            else:
                headers[HEADER_EMAIL_NOTIFY] = str(self.email_notify).lower()
        return headers

    def _process_response(self, response: Any, parse_func: Callable[[Any], T] | None = None) -> T:
        """Process the raw HTTP response."""
        from imednet.models.jobs import Job

        raw = response.json()
        parsed: T = (parse_func or Job.from_json)(raw)  # type: ignore[assignment]
        study_key = self._resolve_study_key()
        if study_key:
            record_study_job(study_key, parsed, job_type="RecordSubmission", source="sdk")
        return parsed

    def execute_sync(
        self,
        client: RequesterProtocol,
        parse_func: Callable[[Any], T] | None = None,
    ) -> T:
        """Execute synchronous creation request.

        Args:
            client: The synchronous HTTP client.
            parse_func: Function to parse the response.

        Returns:
            The parsed response object.
        """
        response = client.post(
            self.path,
            json=self.records_data,
            headers=self.headers,
        )
        return self._process_response(response, parse_func)

    async def execute_async(
        self,
        client: AsyncRequesterProtocol,
        parse_func: Callable[[Any], T] | None = None,
    ) -> T:
        """Execute asynchronous creation request.

        Args:
            client: The asynchronous HTTP client.
            parse_func: Function to parse the response.

        Returns:
            The parsed response object.
        """
        response = await client.post(
            self.path,
            json=self.records_data,
            headers=self.headers,
        )
        return self._process_response(response, parse_func)
