"""Operation for creating records.

Encapsulates validation, header construction, and execution logic for record creation.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Self, TypeVar

from pydantic import ValidationError as PydanticValidationError
from pydantic import field_validator, model_validator

from imednet.constants import HEADER_EMAIL_NOTIFY
from imednet.core.endpoint.dispatch import Operation
from imednet.core.protocols import AsyncRequesterProtocol, RequesterProtocol
from imednet.errors import ValidationError
from imednet.models.records import BaseRecordRequest, RecordRequest
from imednet.utils.security import validate_email_notify
from imednet.validation.cache import BaseSchemaCache, validate_record_entry

T = TypeVar("T")


class _StrictRecordCreateEnvelope(RecordRequest):
    """Strict wire-level request model enforcing formKey/formId presence and strict int IDs."""

    @field_validator("form_id", "site_id", "subject_id", "interval_id", "record_id", mode="before")
    @classmethod
    def _require_strict_int(cls, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError("Identifier must be an integer")
        return v

    @model_validator(mode="after")
    def _require_form_identifier(self) -> Self:  # noqa: N804
        if not self.form_key and self.form_id is None:
            raise ValueError("Either 'formKey' or 'formId' must be provided")
        return self


def _format_pydantic_record_error(idx: int, exc: PydanticValidationError) -> str:
    """Format Pydantic validation errors without echoing raw clinical input values."""
    parts: list[str] = []
    for err in exc.errors(include_input=False, include_url=False):
        loc = ".".join(str(p) for p in err.get("loc", ()))
        msg = err.get("msg", "Invalid value")
        parts.append(f"{loc}: {msg}" if loc else msg)
    detail = "; ".join(parts) or "Invalid record payload"
    return f"Record at index {idx} failed validation: {detail}"


class RecordCreateOperation(Operation[T]):
    """Operation for creating records.

    Handles strict request envelope validation, schema checks, and header
    security validation before executing the creation request.
    """

    def __init__(
        self,
        path: str,
        records_data: list[dict[str, Any]],
        email_notify: str | None = None,
        schema: BaseSchemaCache[Any] | None = None,
    ) -> None:
        """Initialize the record creation operation.

        Args:
            path: The full API path for the request.
            records_data: List of record data objects to create.
            email_notify: Optional email address to notify upon job completion.
            schema: Optional schema cache for validation.

        Raises:
            ValidationError: If record envelope, record data, or email_notify is invalid.
            ClientError: If email_notify contains header-unsafe characters.
        """
        self.path = path
        self.email_notify = email_notify
        self.schema = schema

        self.headers = self._build_headers()
        self.records_data = self._validate_and_serialize(records_data)

    def _validate_and_serialize(self, records_data: Any) -> list[dict[str, Any]]:
        """Validate every item through strict Pydantic models and optional schema."""
        if not isinstance(records_data, list):
            raise ValidationError("records_data must be a list of record dictionaries")

        serialized: list[dict[str, Any]] = []
        for idx, rec in enumerate(records_data):
            if isinstance(rec, BaseRecordRequest):
                raw_payload: Any = rec.model_dump(by_alias=True, exclude_none=True)
            elif isinstance(rec, dict):
                raw_payload = rec
            else:
                raise ValidationError(
                    f"Record at index {idx} failed validation: expected dictionary"
                )

            try:
                strict_model = _StrictRecordCreateEnvelope.model_validate(raw_payload)
            except PydanticValidationError as exc:
                raise ValidationError(_format_pydantic_record_error(idx, exc)) from None

            dumped = strict_model.model_dump(by_alias=True, exclude_none=True)
            normalized = {k: v for k, v in dumped.items() if k == "data" or v != ""}
            if self.schema is not None:
                validate_record_entry(self.schema, normalized)
            serialized.append(normalized)

        return serialized

    def _build_headers(self) -> dict[str, str]:
        """Build headers for record creation request.

        Returns:
            Dictionary of headers.

        Raises:
            ValidationError: If email_notify is boolean, empty, or malformed.
            ClientError: If email_notify contains newlines or control characters.
        """
        headers: dict[str, str] = {}
        if self.email_notify is not None:
            headers[HEADER_EMAIL_NOTIFY] = validate_email_notify(self.email_notify)
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
