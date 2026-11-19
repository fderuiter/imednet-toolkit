"""Operation for creating records.

Encapsulates validation, header construction, and execution logic for record creation.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from imednet.constants import HEADER_EMAIL_NOTIFY
from imednet.core.endpoint.dispatch import Operation
from imednet.core.protocols import AsyncRequesterProtocol, RequesterProtocol
from imednet.utils.security import validate_header_value
from imednet.validation.cache import BaseSchemaCache, validate_record_entry

T = TypeVar("T")


class RecordCreateOperation(Operation[T]):
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
        skip_validation: bool = False,
    ) -> None:
        """Initialize the record creation operation.

        Args:
            path: The full API path for the request.
            records_data: List of record data objects to create.
            email_notify: Email notification setting.
            schema: Optional schema cache for validation.
            skip_validation: If True, skip per-record schema validation because
                the caller has already validated the batch.

        Raises:
            ValidationError: If record data is invalid against the schema.
            ClientError: If email_notify contains invalid characters.
        """
        self.path = path
        self.records_data = records_data
        self.email_notify = email_notify
        self.schema = schema
        self.skip_validation = skip_validation

        self._validate()
        self.headers = self._build_headers()

    def _validate(self) -> None:
        """Validate records against schema if provided and not already validated."""
        if (
            self.skip_validation
            or self.schema is None
            or getattr(self.schema, "is_prevalidated", False) is True
        ):
            return
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

        return (parse_func or Job.from_json)(response.json())  # type: ignore

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
