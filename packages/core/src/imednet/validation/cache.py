"""Caching and validation logic for iMednet variable schemas."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    TypeVar,
    cast,
)

from imednet.errors import UnknownVariableTypeError, ValidationError

from ..models.variables import Variable
from ._base import _ValidatorMixin

if TYPE_CHECKING:
    from ..endpoints.forms import AsyncFormsEndpoint, FormsEndpoint
    from ..endpoints.variables import AsyncVariablesEndpoint, VariablesEndpoint
    from ..spi.facade import AsyncImednetFacade, ImednetFacade

_TClient = TypeVar("_TClient")
_ExecutionOwner = tuple[int, int | None]
_PrevalidatedScope = tuple[
    int,
    _ExecutionOwner,
    tuple[dict[str, Any], ...] | None,
    frozenset[int] | None,
]
_PREVALIDATED_SCOPES: ContextVar[tuple[_PrevalidatedScope, ...]] = ContextVar(
    "imednet_prevalidated_scopes",
    default=(),
)


def _current_execution_owner() -> _ExecutionOwner:
    """Return ``(thread_id, task_id)`` for the currently executing thread and asyncio task."""
    thread_id = threading.get_ident()
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return (thread_id, id(task) if task is not None else None)


class BaseSchemaCache(Generic[_TClient]):
    """Cache of variables by form key with optional async refresh."""

    def __init__(self, is_async: bool) -> None:
        """Initialize the schema cache.

        Args:
            is_async: Whether this cache is used in an asynchronous context.
        """
        self._is_async = is_async
        self._form_variables: dict[str, dict[str, Variable]] = {}
        self._form_id_to_key: dict[int, str] = {}

    @property
    def is_prevalidated(self) -> bool:
        """Return True while inside a ``prevalidated()`` scope owned by the active task."""
        cache_id = id(self)
        owner = _current_execution_owner()
        return any(
            cid == cache_id and scope_owner == owner
            for cid, scope_owner, _, _ in _PREVALIDATED_SCOPES.get()
        )

    def is_record_prevalidated(self, record: dict[str, Any]) -> bool:
        """Return True if ``record`` was already validated for this cache in the active task."""
        cache_id = id(self)
        owner = _current_execution_owner()
        rec_id = id(record)
        for cid, scope_owner, _, rec_ids in _PREVALIDATED_SCOPES.get():
            if cid == cache_id and scope_owner == owner and (rec_ids is None or rec_id in rec_ids):
                return True
        return False

    @contextmanager
    def prevalidated(self, records: Iterable[dict[str, Any]] | None = None) -> Iterator[None]:
        """Mark ``records`` (or the active block) as validated in the current task context."""
        records_ref = tuple(records) if records is not None else None
        rec_ids = frozenset(id(rec) for rec in records_ref) if records_ref is not None else None
        owner = _current_execution_owner()
        current = _PREVALIDATED_SCOPES.get()
        token = _PREVALIDATED_SCOPES.set((*current, (id(self), owner, records_ref, rec_ids)))
        try:
            yield
        finally:
            _PREVALIDATED_SCOPES.reset(token)

    def populate(self, variables: Iterable[Variable]) -> None:
        """Populate the cache with the given variables."""
        self._form_variables.clear()
        self._form_id_to_key.clear()
        for var in variables:
            if var.form_id is not None and var.form_key is not None:
                self._form_id_to_key[var.form_id] = var.form_key
                if var.variable_name is not None:
                    self._form_variables.setdefault(var.form_key, {})[var.variable_name] = var

    async def _refresh_async(
        self,
        forms: AsyncFormsEndpoint,
        variables: AsyncVariablesEndpoint,
        study_key: str | None = None,
    ) -> None:
        """Refresh the cache asynchronously by fetching all variables for a study.

        Args:
            forms: The asynchronous forms endpoint.
            variables: The asynchronous variables endpoint.
            study_key: The study key to refresh variables for.
        """
        vars_list = [v async for v in variables.list(study_key=study_key)]
        self.populate(vars_list)

    def _refresh_sync(
        self,
        forms: FormsEndpoint,
        variables: VariablesEndpoint,
        study_key: str | None = None,
    ) -> None:
        """Refresh the cache synchronously by fetching all variables for a study.

        Args:
            forms: The synchronous forms endpoint.
            variables: The synchronous variables endpoint.
            study_key: The study key to refresh variables for.
        """
        vars_list = variables.list(study_key=study_key)
        self.populate(vars_list)

    def refresh(
        self,
        forms: FormsEndpoint | AsyncFormsEndpoint,
        variables: VariablesEndpoint | AsyncVariablesEndpoint,
        study_key: str | None = None,
    ) -> Any:
        """Refresh the cache, using the appropriate sync or async implementation.

        Args:
            forms: The forms endpoint (sync or async).
            variables: The variables endpoint (sync or async).
            study_key: The study key to refresh variables for.

        Returns:
            Any: An awaitable if async, else None.
        """
        if self._is_async:
            return self._refresh_async(
                cast("AsyncFormsEndpoint", forms),
                cast("AsyncVariablesEndpoint", variables),
                study_key,
            )
        return self._refresh_sync(
            cast("FormsEndpoint", forms),
            cast("VariablesEndpoint", variables),
            study_key,
        )

    def variables_for_form(self, form_key: str) -> dict[str, Variable]:
        """Return the variables associated with the given form key.

        Args:
            form_key: The form key to look up.

        Returns:
            Dict[str, Variable]: A mapping of variable names to Variable objects.
        """
        return self._form_variables.get(form_key, {})

    def form_key_from_id(self, form_id: int) -> str | None:
        """Resolve a form key from a form ID.

        Args:
            form_id: The form ID to look up.

        Returns:
            Optional[str]: The form key if found, else None.
        """
        return self._form_id_to_key.get(form_id)

    @property
    def forms(self) -> dict[str, dict[str, Variable]]:
        """Return cached variables grouped by form key."""
        return self._form_variables


class SchemaCache(BaseSchemaCache["ImednetFacade"]):
    """Synchronous implementation of the schema cache."""

    def __init__(self) -> None:
        """Initialize the synchronous schema cache."""
        super().__init__(is_async=False)


class AsyncSchemaCache(BaseSchemaCache["AsyncImednetFacade"]):
    """Asynchronous implementation of the schema cache."""

    def __init__(self) -> None:
        """Initialize the asynchronous schema cache."""
        super().__init__(is_async=True)


def _validate_int(value: Any) -> None:
    """Validate that the value is an integer.

    Args:
        value: The value to validate.

    Raises:
        ValidationError: If the value is not an integer.
    """
    if not isinstance(value, int):
        raise ValidationError("Value must be an integer")


def _validate_float(value: Any) -> None:
    """Validate that the value is numeric (int or float).

    Args:
        value: The value to validate.

    Raises:
        ValidationError: If the value is not numeric.
    """
    if not isinstance(value, (int, float)):
        raise ValidationError("Value must be numeric")


def _validate_bool(value: Any) -> None:
    """Validate that the value is a boolean.

    Args:
        value: The value to validate.

    Raises:
        ValidationError: If the value is not a boolean.
    """
    if not isinstance(value, bool):
        raise ValidationError("Value must be boolean")


def _validate_text(value: Any) -> None:
    """Validate that the value is a string.

    Args:
        value: The value to validate.

    Raises:
        ValidationError: If the value is not a string.
    """
    if not isinstance(value, str):
        raise ValidationError("Value must be a string")


_TYPE_VALIDATORS: dict[str, Callable[[Any], None]] = {
    "int": _validate_int,
    "integer": _validate_int,
    "number": _validate_int,
    "float": _validate_float,
    "decimal": _validate_float,
    "bool": _validate_bool,
    "boolean": _validate_bool,
    "text": _validate_text,
    "string": _validate_text,
    "date": _validate_text,
    "datetime": _validate_text,
    "time": _validate_text,
}

# Bolt Optimization: Expand validators to include common casings
# to avoid .lower() allocation in hot paths.
for key, validator in list(_TYPE_VALIDATORS.items()):
    _TYPE_VALIDATORS[key.capitalize()] = validator
    _TYPE_VALIDATORS[key.upper()] = validator


def _check_type(var_type: str | None, value: Any) -> None:
    """Check the type of a value against an iMednet variable type.

    Args:
        var_type: The expected iMednet variable type (e.g., 'int', 'text').
        value: The value to check.

    Raises:
        UnknownVariableTypeError: If the variable type is not recognized.
        ValidationError: If the value does not match the expected type.
    """
    if value is None or not var_type:
        return

    # Bolt Optimization: Try direct lookup first to avoid string manipulation
    try:
        validator = _TYPE_VALIDATORS[var_type]
    except KeyError:
        try:
            validator = _TYPE_VALIDATORS[var_type.lower()]
        except KeyError:
            raise UnknownVariableTypeError(var_type) from None

    validator(value)


def validate_record_data(
    schema: BaseSchemaCache[Any],
    form_key: str,
    data: dict[str, Any],
) -> None:
    """Validate ``data`` for ``form_key`` using the provided schema cache.

    Raises:
        ValidationError: If the form key is not present in the schema or the data
            fails validation checks.
    """
    variables = schema.variables_for_form(form_key)
    if not variables:
        # The cache has no variables for the given form key, so treat it as an
        # unknown form and fail fast.
        raise ValidationError(f"Unknown form {form_key}")
    unknown = [k for k in data if k not in variables]
    if unknown:
        raise ValidationError(f"Unknown variables for form {form_key}: {', '.join(unknown)}")
    # Bolt Optimization: Removed dead code iterating over all variables for 'required' check.
    # The Variable model does not have a 'required' field, so this loop was O(N) for no-op.
    for name, value in data.items():
        _check_type(variables[name].variable_type, value)


def calculate_readiness_score(
    schema: BaseSchemaCache[Any],
    form_key: str,
    data: dict[str, Any],
) -> tuple[float, list[str]]:
    """Calculate the schema readiness score for a record's data.

    Returns:
        A tuple of (score, list of validation failure reasons).
    """
    variables = schema.variables_for_form(form_key)
    if not variables:
        return 0.0, [f"Unknown form {form_key}"]

    expected_count = len(variables)
    if expected_count == 0:
        return 100.0, []

    valid_count = 0
    reasons = []

    unknown = [k for k in data if k not in variables]
    if unknown:
        reasons.append(f"Unknown variables: {', '.join(unknown)}")

    for name, var in variables.items():
        if name not in data:
            reasons.append(f"Missing expected variable: {name}")
            continue

        value = data[name]
        try:
            _check_type(var.variable_type, value)
            valid_count += 1
        except Exception as e:
            reasons.append(f"Variable {name} invalid: {e!s}")

    score = (valid_count / expected_count) * 100.0
    return score, reasons


def validate_record_entry(
    schema: BaseSchemaCache[Any],
    record: dict[str, Any],
) -> None:
    """Validate a single record entry against the schema.

    Resolves the form key from "formKey", "form_key", "formId", or "form_id".

    Args:
        schema: The schema cache to use for validation.
        record: The record data dictionary.

    Raises:
        ValidationError: If the form key is not present in the schema or the data
            fails validation checks.
    """
    fk = record.get("formKey") or record.get("form_key")
    if not fk:
        fid = record.get("formId") or record.get("form_id") or 0
        fk = schema.form_key_from_id(fid)
    if fk:
        validate_record_data(schema, fk, record.get("recordData", record.get("data", {})))


class BaseSchemaValidator(_ValidatorMixin, Generic[_TClient]):
    """Base validator sharing logic between sync and async implementations."""

    schema: BaseSchemaCache[_TClient]

    def _refresh_common(self, variables: Iterable[Variable]) -> None:
        """Common logic to populate the schema cache.

        Args:
            variables: An iterable of variables to populate the cache with.
        """
        self.schema.populate(variables)

    def _needs_refresh(self, record: dict[str, Any]) -> tuple[str | None, bool]:
        """Determine if the schema cache needs refreshing for the given record.

        Returns:
            A tuple of (form_key, needs_refresh).
        """
        form_key = self._resolve_form_key(record)
        needs_refresh = bool(form_key and not self.schema.variables_for_form(form_key))
        return form_key, needs_refresh


class SchemaValidator(BaseSchemaValidator["ImednetFacade"]):
    """Validate record payloads using variable metadata from the API (Synchronous)."""

    def __init__(self, sdk: ImednetFacade) -> None:
        """Initialize the synchronous schema validator."""
        self._sdk = sdk
        self.schema = SchemaCache()

    def refresh(self, study_key: str) -> None:
        """Populate the schema cache for ``study_key`` from the Variables endpoint.

        This method never raises :class:`~imednet.errors.ValidationError`;
        any API errors bubble up as :class:`~imednet.errors.ApiError`.
        """
        variables = self._sdk.get_variables(study_key=study_key)
        self._refresh_common(variables)

    def validate_record(self, study_key: str, record: dict[str, Any]) -> None:
        """Validate a single record payload."""
        form_key, needs_refresh = self._needs_refresh(record)
        if needs_refresh:
            self.refresh(study_key)
        self._validate_cached(form_key, record.get("recordData", record.get("data", {})))

    def validate_batch(self, study_key: str, records: list[dict[str, Any]]) -> None:
        """Validate a batch of record payloads."""
        for rec in records:
            self.validate_record(study_key, rec)


class AsyncSchemaValidator(BaseSchemaValidator["AsyncImednetFacade"]):
    """Validate record payloads using variable metadata from the API (Asynchronous)."""

    def __init__(self, sdk: AsyncImednetFacade) -> None:
        """Initialize the asynchronous schema validator."""
        self._sdk = sdk
        self.schema = AsyncSchemaCache()

    async def refresh(self, study_key: str) -> None:
        """Populate the schema cache for ``study_key`` from the Variables endpoint.

        This method never raises :class:`~imednet.errors.ValidationError`;
        any API errors bubble up as :class:`~imednet.errors.ApiError`.
        """
        variables = await self._sdk.async_get_variables(study_key=study_key)
        self._refresh_common(variables)

    async def validate_record(self, study_key: str, record: dict[str, Any]) -> None:
        """Validate a single record payload asynchronously."""
        form_key, needs_refresh = self._needs_refresh(record)
        if needs_refresh:
            await self.refresh(study_key)
        self._validate_cached(form_key, record.get("recordData", record.get("data", {})))

    async def validate_batch(self, study_key: str, records: list[dict[str, Any]]) -> None:
        """Validate a batch of record payloads asynchronously."""
        for rec in records:
            await self.validate_record(study_key, rec)
