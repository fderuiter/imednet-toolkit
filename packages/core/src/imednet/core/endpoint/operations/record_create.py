"""Operation for creating records.

Encapsulates validation, header construction, and execution logic for record creation.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any, TypeVar
from unittest.mock import NonCallableMock

from imednet.constants import HEADER_EMAIL_NOTIFY
from imednet.core.endpoint.dispatch import Operation
from imednet.core.protocols import AsyncRequesterProtocol, RequesterProtocol
from imednet.utils.security import validate_header_value
from imednet.utils.tabular import ensure_no_masked_sentinel
from imednet.utils.write_pipeline import RawRecordEnvelope, WriteIngestionPipeline
from imednet.validation.cache import (
    BaseSchemaCache,
    ValidationSchemaProtocol,
    is_schema_record_prevalidated,
    validate_record_entry,
)

T = TypeVar("T")
_STUDY_PATH_RE = re.compile(r"/studies/([^/]+)/records(?:/|$)")


def _infer_study_key_from_path(path: str) -> str | None:
    """Extract the study key segment from a ``/studies/<study_key>/records`` path."""
    match = _STUDY_PATH_RE.search(path)
    return match.group(1) if match else None


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
        schema: ValidationSchemaProtocol | None = None,
        *,
        study_key: str | None = None,
        skip_validation: bool = False,
        raw_envelopes: Sequence[RawRecordEnvelope] | None = None,
    ) -> None:
        """Initialize the record creation operation."""
        self.path = path
        self.records_data = records_data
        self.email_notify = email_notify
        self.schema = schema
        self.study_key = study_key if study_key is not None else _infer_study_key_from_path(path)
        self.skip_validation = skip_validation
        self._raw_envelopes: list[RawRecordEnvelope] | None = (
            list(raw_envelopes) if raw_envelopes is not None else None
        )

        self.headers = self._build_headers()
        if self._raw_envelopes is None:
            ensure_no_masked_sentinel(self.records_data)
            self._validate()

    def _bind_deferred_sync(self, client: RequesterProtocol) -> None:
        if self._raw_envelopes is None:
            return
        envelopes = self._raw_envelopes
        self._raw_envelopes = None

        def _refresh(sk: str) -> None:
            if isinstance(self.schema, BaseSchemaCache) and not isinstance(client, NonCallableMock):
                from imednet.endpoints.forms import FormsEndpoint
                from imednet.endpoints.variables import VariablesEndpoint

                if not self.schema._is_async:
                    self.schema.refresh(
                        FormsEndpoint(client=client),
                        VariablesEndpoint(client=client),
                        study_key=sk,
                    )
                else:
                    vars_list = VariablesEndpoint(client=client).list(study_key=sk)
                    self.schema.populate(vars_list, study_key=sk)

        self.records_data = WriteIngestionPipeline.bind(
            envelopes,
            schema=self.schema,
            study_key=self.study_key,
            refresh_sync=None if self.skip_validation else _refresh,
        )
        ensure_no_masked_sentinel(self.records_data)
        self._validate()

    async def _bind_deferred_async(self, client: AsyncRequesterProtocol) -> None:
        if self._raw_envelopes is None:
            return
        envelopes = self._raw_envelopes
        self._raw_envelopes = None

        async def _refresh_async(sk: str) -> None:
            if isinstance(self.schema, BaseSchemaCache) and not isinstance(client, NonCallableMock):
                from imednet.endpoints.forms import AsyncFormsEndpoint
                from imednet.endpoints.variables import AsyncVariablesEndpoint

                if self.schema._is_async:
                    await self.schema.refresh(
                        AsyncFormsEndpoint(async_client=client),
                        AsyncVariablesEndpoint(async_client=client),
                        study_key=sk,
                    )
                else:
                    vars_ep = AsyncVariablesEndpoint(async_client=client)
                    vars_list = [v async for v in vars_ep.list(study_key=sk)]
                    self.schema.populate(vars_list, study_key=sk)

        self.records_data = await WriteIngestionPipeline.bind_async(
            envelopes,
            schema=self.schema,
            study_key=self.study_key,
            refresh_async=None if self.skip_validation else _refresh_async,
        )
        ensure_no_masked_sentinel(self.records_data)
        self._validate()

    def _validate(self) -> None:
        """Validate records against schema if provided and not already validated."""
        if self.skip_validation or self.schema is None:
            return
        is_rec_prevalidated = getattr(self.schema, "is_record_prevalidated", None)
        if callable(is_rec_prevalidated):
            for rec in self.records_data:
                if is_rec_prevalidated(rec) is not True:
                    validate_record_entry(self.schema, rec, study_key=self.study_key)
            return
        if getattr(self.schema, "is_prevalidated", False) is True:
            return
        for rec in self.records_data:
            if not is_schema_record_prevalidated(self.schema, rec):
                validate_record_entry(self.schema, rec, study_key=self.study_key)

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

    def _process_response(
        self,
        response: Any,
        parse_func: Callable[[Any], T] | None = None,
        *,
        client: Any = None,
    ) -> T:
        """Process the raw HTTP response."""
        from imednet.core.context import strict_mode_context
        from imednet.models.jobs import Job

        parser = parse_func or Job.from_json
        payload = response.json()
        client_strict = getattr(client, "strict_mode", None)
        if isinstance(client_strict, bool):
            with strict_mode_context(client_strict):
                return parser(payload)  # type: ignore
        return parser(payload)  # type: ignore

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
        self._bind_deferred_sync(client)
        response = client.post(
            self.path,
            json=self.records_data,
            headers=self.headers,
        )
        return self._process_response(response, parse_func, client=client)

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
        await self._bind_deferred_async(client)
        response = await client.post(
            self.path,
            json=self.records_data,
            headers=self.headers,
        )
        return self._process_response(response, parse_func, client=client)
