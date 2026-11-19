"""Caching and validation logic for iMednet variable schemas."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    TypeVar,
    cast,
)

from typing_extensions import Protocol, runtime_checkable

from imednet.datadict.logic import LogicTreeEvaluator, extract_variable_constraints
from imednet.datadict.models import DataDictionary
from imednet.errors import UnknownVariableTypeError, ValidationError

from ..models.variables import Variable
from ._base import _extract_form_ref, _ValidatorMixin

if TYPE_CHECKING:
    from ..endpoints.forms import AsyncFormsEndpoint, FormsEndpoint
    from ..endpoints.variables import AsyncVariablesEndpoint, VariablesEndpoint
    from ..spi.facade import AsyncImednetFacade, ImednetFacade

logger = logging.getLogger(__name__)

_TClient = TypeVar("_TClient")
_LOGIC_EVALUATOR = LogicTreeEvaluator()
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


def is_schema_record_prevalidated(schema: Any, record: dict[str, Any]) -> bool:
    """Return True if ``record`` was already validated for ``schema`` in the active task."""
    schema_id = id(schema)
    owner = _current_execution_owner()
    rec_id = id(record)
    for sid, scope_owner, _, rec_ids in _PREVALIDATED_SCOPES.get():
        if sid == schema_id and scope_owner == owner and (rec_ids is None or rec_id in rec_ids):
            return True
    return False


@contextmanager
def prevalidated_records(
    schema: Any, records: Iterable[dict[str, Any]] | None = None
) -> Iterator[None]:
    """Mark ``records`` as pre-validated for ``schema`` in the active task context."""
    records_ref = tuple(records) if records is not None else None
    rec_ids = frozenset(id(rec) for rec in records_ref) if records_ref is not None else None
    owner = _current_execution_owner()
    current = _PREVALIDATED_SCOPES.get()
    token = _PREVALIDATED_SCOPES.set((*current, (id(schema), owner, records_ref, rec_ids)))
    try:
        yield
    finally:
        _PREVALIDATED_SCOPES.reset(token)


@runtime_checkable
class ValidationSchemaProtocol(Protocol):
    """Unified schema protocol implemented by BaseSchemaCache and DataDictionary."""

    def variables_for_form(self, form_key: str) -> Mapping[str, Any]:
        """Return a mapping of variable names to variable definitions for ``form_key``."""
        ...

    def form_key_from_id(self, form_id: int | str) -> str | None:
        """Resolve a ``formKey`` from a ``formId``."""
        ...


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

    def form_key_from_id(self, form_id: int | str) -> str | None:
        """Resolve a form key from a form ID.

        Args:
            form_id: The form ID to look up.

        Returns:
            Optional[str]: The form key if found, else None.
        """
        if isinstance(form_id, bool):
            return None
        if isinstance(form_id, int):
            return self._form_id_to_key.get(form_id)
        if isinstance(form_id, str):
            stripped = form_id.strip()
            if stripped.lstrip("-").isdigit():
                return self._form_id_to_key.get(int(stripped))
        return None

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
    """Validate that the value is an integer (excluding booleans).

    Args:
        value: The value to validate.

    Raises:
        ValidationError: If the value is not an integer or is a boolean.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("Value must be an integer")


def _validate_float(value: Any) -> None:
    """Validate that the value is numeric (int or float, excluding booleans).

    Args:
        value: The value to validate.

    Raises:
        ValidationError: If the value is not numeric or is a boolean.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
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


def _validate_single_choice(value: Any) -> None:
    """Validate that a single-choice categorical value is text or a whole-number code."""
    if isinstance(value, str):
        return
    if isinstance(value, int) and not isinstance(value, bool):
        return
    raise ValidationError("Value must be a string or integer code")


def _is_choice_code(item: Any) -> bool:
    """Return True if ``item`` is a string or non-boolean integer choice code."""
    return isinstance(item, str) or (isinstance(item, int) and not isinstance(item, bool))


def _validate_multi_choice(value: Any) -> None:
    """Validate that a multi-choice value is text, boolean, or a collection of codes."""
    if isinstance(value, (bool, str)):
        return
    if isinstance(value, (list, tuple, set, frozenset)) and all(
        _is_choice_code(item) for item in value
    ):
        return
    raise ValidationError(
        "Value must be a string, boolean, or collection of string or integer codes"
    )


def _validate_calculation(value: Any) -> None:
    """Validate that a computed or range value is non-boolean numeric or text."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValidationError("Value must be numeric or a string")


_TYPE_SEP_RE = re.compile(r"[\s_-]+")
_SLASH_SPACE_RE = re.compile(r"\s*/\s*")


def _normalize_variable_type_key(var_type: str | None) -> str:
    """Normalize casing, whitespace, and word separators for variable type lookup."""
    if not var_type:
        return ""
    cleaned = _TYPE_SEP_RE.sub(" ", var_type.strip().lower())
    if "/" in cleaned:
        cleaned = _SLASH_SPACE_RE.sub("/", cleaned)
    return cleaned


# Canonical variable type taxonomy shared across SchemaCache and offline test helpers:
# normalized_key -> (canonical_category, deterministic_sample_value, validator_fn)
_CANONICAL_VARIABLE_TAXONOMY: dict[str, tuple[str, Any, Callable[[Any], None]]] = {
    # Whole-number numeric types
    "int": ("number", 1, _validate_int),
    "integer": ("number", 1, _validate_int),
    "number": ("number", 1, _validate_int),
    # Decimal numeric types
    "float": ("number", 1.0, _validate_float),
    "decimal": ("number", 1.0, _validate_float),
    "double": ("number", 1.0, _validate_float),
    "numeric": ("number", 1.0, _validate_float),
    # Primitive boolean types
    "bool": ("boolean", True, _validate_bool),
    "boolean": ("boolean", True, _validate_bool),
    # Single-choice categorical types
    "coded": ("coded", "1", _validate_single_choice),
    "radio": ("radio", "1", _validate_single_choice),
    "radiobutton": ("radio", "1", _validate_single_choice),
    "radio button": ("radio", "1", _validate_single_choice),
    "dropdown": ("dropdown", "1", _validate_single_choice),
    "drop down": ("dropdown", "1", _validate_single_choice),
    "dropdownlist": ("dropdown", "1", _validate_single_choice),
    "dropdown list": ("dropdown", "1", _validate_single_choice),
    "select": ("single select", "1", _validate_single_choice),
    "single select": ("single select", "1", _validate_single_choice),
    "singleselect": ("single select", "1", _validate_single_choice),
    "sql": ("single select", "1", _validate_single_choice),
    "yes/no": ("yes/no", "1", _validate_single_choice),
    "yesno": ("yes/no", "1", _validate_single_choice),
    "yes no": ("yes/no", "1", _validate_single_choice),
    "true/false": ("yes/no", "1", _validate_single_choice),
    "truefalse": ("yes/no", "1", _validate_single_choice),
    "true false": ("yes/no", "1", _validate_single_choice),
    # Multi-choice categorical types
    "checkbox": ("checkbox", True, _validate_multi_choice),
    "check box": ("checkbox", True, _validate_multi_choice),
    "multi select": ("multi select", "1", _validate_multi_choice),
    "multiselect": ("multi select", "1", _validate_multi_choice),
    # Narrative & free-text types
    "string": ("string", "example", _validate_text),
    "text": ("string", "example", _validate_text),
    "alphanumeric": ("string", "example", _validate_text),
    "memo": ("memo", "example memo", _validate_text),
    "textarea": ("memo", "example memo", _validate_text),
    "text area": ("memo", "example memo", _validate_text),
    "notes": ("memo", "example memo", _validate_text),
    "note": ("memo", "example memo", _validate_text),
    # Temporal & precision date/time types
    "date": ("date", "2024-01-01", _validate_text),
    "date ymd": ("date", "2024-01-01", _validate_text),
    "date mdy": ("date", "2024-01-01", _validate_text),
    "date dmy": ("date", "2024-01-01", _validate_text),
    "datetime": ("datetime", "2024-01-01T00:00:00", _validate_text),
    "date/time": ("datetime", "2024-01-01T00:00:00", _validate_text),
    "date time": ("datetime", "2024-01-01T00:00:00", _validate_text),
    "durationdatetime": ("datetime", "2024-01-01T00:00:00", _validate_text),
    "intervaldatetime": ("datetime", "2024-01-01T00:00:00", _validate_text),
    "date/time precision": ("date/time precision", "2024-01-01", _validate_text),
    "datetime precision": ("date/time precision", "2024-01-01", _validate_text),
    "date time precision": ("date/time precision", "2024-01-01", _validate_text),
    "time": ("time", "12:00:00", _validate_text),
    # Computed & range types
    "calculation": ("calculation", 1, _validate_calculation),
    "calc": ("calculation", 1, _validate_calculation),
    "calculated": ("calculation", 1, _validate_calculation),
    "slider": ("slider", 1, _validate_calculation),
    # Metadata reference types
    "file": ("file", "example.pdf", _validate_text),
    "upload": ("file", "example.pdf", _validate_text),
    "fileupload": ("file", "example.pdf", _validate_text),
    "file upload": ("file", "example.pdf", _validate_text),
    "signature": ("signature", "signed", _validate_text),
    "descriptive": ("descriptive", "example", _validate_text),
    "label": ("descriptive", "example", _validate_text),
}

for _base_key, _base_spec in list(_CANONICAL_VARIABLE_TAXONOMY.items()):
    _CANONICAL_VARIABLE_TAXONOMY.setdefault(_base_key.replace(" ", ""), _base_spec)
    _CANONICAL_VARIABLE_TAXONOMY.setdefault(_base_key.replace("/", " "), _base_spec)
    _CANONICAL_VARIABLE_TAXONOMY.setdefault(_base_key.replace(" ", "").replace("/", ""), _base_spec)

_NORMALIZED_TYPE_VALIDATORS: dict[str, Callable[[Any], None]] = {
    key: spec[2] for key, spec in _CANONICAL_VARIABLE_TAXONOMY.items()
}

_MULTI_CHOICE_TYPE_KEYS: frozenset[str] = frozenset(
    key for key, spec in _CANONICAL_VARIABLE_TAXONOMY.items() if spec[2] is _validate_multi_choice
)

# Pre-index common casings and word-separator forms to avoid per-Record string
# allocations during hot-path batch validation.
_TYPE_VALIDATORS: dict[str, Callable[[Any], None]] = dict(_NORMALIZED_TYPE_VALIDATORS)
for _key, _validator in list(_NORMALIZED_TYPE_VALIDATORS.items()):
    _TYPE_VALIDATORS[_key.capitalize()] = _validator
    _TYPE_VALIDATORS[_key.title()] = _validator
    _TYPE_VALIDATORS[_key.upper()] = _validator
    if " " in _key:
        _underscore_key = _key.replace(" ", "_")
        _hyphen_key = _key.replace(" ", "-")
        _pascal_key = "".join(part.capitalize() for part in _key.split(" "))
        _TYPE_VALIDATORS[_underscore_key] = _validator
        _TYPE_VALIDATORS[_underscore_key.upper()] = _validator
        _TYPE_VALIDATORS[_hyphen_key] = _validator
        _TYPE_VALIDATORS[_hyphen_key.upper()] = _validator
        _TYPE_VALIDATORS[_pascal_key] = _validator
    if "/" in _key:
        _slash_title = "/".join(
            " ".join(word.capitalize() for word in seg.split(" ")) for seg in _key.split("/")
        )
        _TYPE_VALIDATORS[_slash_title] = _validator

_TYPE_VALIDATORS.update(
    {
        "TextArea": _validate_text,
        "DateTime": _validate_text,
        "DateTimePrecision": _validate_text,
        "DurationDateTime": _validate_text,
        "IntervalDateTime": _validate_text,
        "RadioButton": _validate_single_choice,
        "DropdownList": _validate_single_choice,
        "SingleSelect": _validate_single_choice,
        "MultiSelect": _validate_multi_choice,
        "YesNo": _validate_single_choice,
        "TrueFalse": _validate_single_choice,
        "FileUpload": _validate_text,
    }
)


@lru_cache(maxsize=256)
def _resolve_type_validator(var_type: str) -> Callable[[Any], None] | None:
    """Resolve and cache a validator for non-standard casing or whitespace."""
    norm = _normalize_variable_type_key(var_type)
    if not norm:
        return None
    validator = _NORMALIZED_TYPE_VALIDATORS.get(norm)
    if validator is None:
        raise UnknownVariableTypeError(var_type)
    return validator


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

    validator = _TYPE_VALIDATORS.get(var_type)
    if validator is None:
        validator = _resolve_type_validator(var_type)
        if validator is None:
            return

    if value == "":
        return

    validator(value)


def _is_empty_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value == ""
    if isinstance(value, (list, tuple, set, frozenset)):
        return len(value) == 0
    return False


def _validate_choice_membership(
    name: str,
    form_key: str,
    var_type: str | None,
    value: Any,
    choices: list[str],
) -> None:
    allowed = set(choices)
    vtype_norm = _normalize_variable_type_key(var_type)

    if vtype_norm in _MULTI_CHOICE_TYPE_KEYS:
        if isinstance(value, bool):
            return
        if isinstance(value, (list, tuple, set, frozenset)):
            tokens = [str(item) for item in value]
        elif isinstance(value, str):
            if value in allowed:
                return
            sep = "`" if "`" in value else ","
            tokens = [part.strip() for part in value.split(sep) if part.strip()]
        else:
            tokens = [str(value)]

        invalid = [tok for tok in tokens if tok not in allowed]
        if invalid:
            raise ValidationError(
                f"Invalid choice(s) {invalid} for variable '{name}' on form '{form_key}'. "
                f"Allowed values: {choices}"
            )
        return

    str_val = str(value)
    if str_val not in allowed:
        raise ValidationError(
            f"Invalid choice '{value}' for variable '{name}' on form '{form_key}'. "
            f"Allowed values: {choices}"
        )


def _validate_numeric_bounds(
    name: str,
    form_key: str,
    value: Any,
    min_value: float | None,
    max_value: float | None,
) -> None:
    if isinstance(value, bool):
        return
    num: float | None = None
    if isinstance(value, (int, float)):
        num = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            num = float(value.strip())
        except ValueError:
            return
    if num is None or not math.isfinite(num):
        return

    if min_value is not None and num < min_value:
        raise ValidationError(
            f"Value {value} for variable '{name}' on form '{form_key}' is below minimum {min_value}"
        )
    if max_value is not None and num > max_value:
        raise ValidationError(
            f"Value {value} for variable '{name}' on form '{form_key}' exceeds maximum {max_value}"
        )


def _validate_text_length(
    name: str,
    form_key: str,
    value: str,
    min_length: int | None,
    max_length: int | None,
) -> None:
    if min_length is not None and len(value) < min_length:
        raise ValidationError(
            f"Value for variable '{name}' on form '{form_key}' "
            f"is shorter than minimum length {min_length}"
        )
    if max_length is not None and len(value) > max_length:
        raise ValidationError(
            f"Value for variable '{name}' on form '{form_key}' exceeds maximum length {max_length}"
        )


def validate_record_data(
    schema: ValidationSchemaProtocol,
    form_key: str,
    data: dict[str, Any],
) -> None:
    """Validate ``data`` for ``form_key`` using the provided validation schema.

    Raises:
        ValidationError: If the form key is not present in the schema or the data
            fails validation checks.
    """
    variables = schema.variables_for_form(form_key)
    if not variables:
        if isinstance(schema, DataDictionary):
            form_obj = schema.form_for_key(form_key)
            if form_obj is None or not schema.rules_for_form(form_key):
                raise ValidationError(f"Unknown form {form_key}")
        else:
            # The cache has no variables for the given form key, so treat it as an
            # unknown form and fail fast.
            raise ValidationError(f"Unknown form {form_key}")

    unknown = [k for k in data if k not in variables]
    if unknown:
        raise ValidationError(f"Unknown variables for form {form_key}: {', '.join(unknown)}")

    for name, value in data.items():
        var_obj = variables[name]
        vtype = getattr(var_obj, "variable_type", getattr(var_obj, "variableType", None))
        _check_type(vtype, value)

    # Fast path for API SchemaCache (no codelists, bounds, required policies, or XML rules)
    if isinstance(schema, BaseSchemaCache):
        return

    for name, var_obj in variables.items():
        constraints = extract_variable_constraints(var_obj)
        is_missing = name not in data or _is_empty_value(data[name])
        if is_missing:
            if constraints.required_policy == "yes":
                raise ValidationError(
                    f"Required variable '{name}' on form '{form_key}' is missing or empty"
                )
            if constraints.required_policy == "auto_query":
                logger.warning(
                    "Auto Query: variable '%s' on form '%s' is missing or empty.",
                    name,
                    form_key,
                )
            continue

        value = data[name]
        vtype = getattr(var_obj, "variable_type", getattr(var_obj, "variableType", None))
        if constraints.choices:
            _validate_choice_membership(name, form_key, vtype, value, constraints.choices)
        if constraints.min_value is not None or constraints.max_value is not None:
            _validate_numeric_bounds(
                name, form_key, value, constraints.min_value, constraints.max_value
            )
        if isinstance(value, str) and (
            constraints.min_length is not None or constraints.max_length is not None
        ):
            _validate_text_length(
                name, form_key, value, constraints.min_length, constraints.max_length
            )

    rules_getter = getattr(schema, "rules_for_form", None)
    if callable(rules_getter):
        rules = rules_getter(form_key)
        if rules:
            report = _LOGIC_EVALUATOR.evaluate_rules(rules, data, form_key=form_key)
            for warn in report.warnings:
                logger.warning(
                    "Query rule '%s' triggered on form '%s': %s",
                    warn.rule_name,
                    form_key,
                    warn.message,
                )
            if report.errors:
                first_err = report.errors[0]
                raise ValidationError(
                    f"Rule '{first_err.rule_name}' failed on form '{form_key}': {first_err.message}"
                )


def calculate_readiness_score(
    schema: ValidationSchemaProtocol,
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
    reasons: list[str] = []

    unknown = [k for k in data if k not in variables]
    if unknown:
        reasons.append(f"Unknown variables: {', '.join(unknown)}")

    is_cache = isinstance(schema, BaseSchemaCache)

    for name, var in variables.items():
        if name not in data:
            reasons.append(f"Missing expected variable: {name}")
            continue

        value = data[name]
        vtype = getattr(var, "variable_type", getattr(var, "variableType", None))
        try:
            _check_type(vtype, value)
            if not is_cache:
                constraints = extract_variable_constraints(var)
                if _is_empty_value(value):
                    if constraints.required_policy == "yes":
                        raise ValidationError("Required variable is empty")
                    if constraints.required_policy == "auto_query":
                        reasons.append(f"Auto Query warning for variable {name}: value is empty")
                else:
                    if constraints.choices:
                        _validate_choice_membership(
                            name, form_key, vtype, value, constraints.choices
                        )
                    if constraints.min_value is not None or constraints.max_value is not None:
                        _validate_numeric_bounds(
                            name, form_key, value, constraints.min_value, constraints.max_value
                        )
                    if isinstance(value, str) and (
                        constraints.min_length is not None or constraints.max_length is not None
                    ):
                        _validate_text_length(
                            name, form_key, value, constraints.min_length, constraints.max_length
                        )
            valid_count += 1
        except Exception as e:
            reasons.append(f"Variable {name} invalid: {e!s}")

    if not is_cache:
        rules_getter = getattr(schema, "rules_for_form", None)
        if callable(rules_getter):
            report = _LOGIC_EVALUATOR.evaluate_rules(
                rules_getter(form_key), data, form_key=form_key
            )
            for err in report.errors:
                reasons.append(f"Rule {err.rule_name} failed: {err.message}")
            for warn in report.warnings:
                reasons.append(f"Rule {warn.rule_name} warning: {warn.message}")

    score = (valid_count / expected_count) * 100.0
    return score, reasons


def validate_record_entry(
    schema: ValidationSchemaProtocol,
    record: dict[str, Any],
) -> None:
    """Validate a single record entry against the schema.

    Resolves the form key from "formKey", "form_key", "formId", or "form_id".

    Args:
        schema: The validation schema to use.
        record: The record data dictionary.

    Raises:
        ValidationError: If the form key or form ID is not present in the schema
            or the data fails validation checks.
    """
    fk, fid = _extract_form_ref(record)
    if not fk and fid is not None:
        if isinstance(fid, bool):
            raise ValidationError(f"Unknown form ID {fid}")
        fk = schema.form_key_from_id(fid)
        if not fk:
            raise ValidationError(f"Unknown form ID {fid}")
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
        if form_key:
            return form_key, not bool(self.schema.variables_for_form(form_key))
        _, fid = _extract_form_ref(record)
        if fid is not None and not isinstance(fid, bool):
            return None, True
        return None, False


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
            if form_key is None:
                form_key = self._resolve_form_key(record)
        if form_key is None:
            _, fid = _extract_form_ref(record)
            if fid is not None:
                raise ValidationError(f"Unknown form ID {fid}")
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
            if form_key is None:
                form_key = self._resolve_form_key(record)
        if form_key is None:
            _, fid = _extract_form_ref(record)
            if fid is not None:
                raise ValidationError(f"Unknown form ID {fid}")
        self._validate_cached(form_key, record.get("recordData", record.get("data", {})))

    async def validate_batch(self, study_key: str, records: list[dict[str, Any]]) -> None:
        """Validate a batch of record payloads asynchronously."""
        for rec in records:
            await self.validate_record(study_key, rec)
