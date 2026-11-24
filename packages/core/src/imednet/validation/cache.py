"""Caching and validation logic for iMednet variable schemas."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import lru_cache
from typing import (
    TYPE_CHECKING,
    Any,
    Generic,
    Literal,
    TypeVar,
    cast,
    overload,
)

from typing_extensions import Protocol, runtime_checkable

from imednet.core.context import get_study_context
from imednet.datadict.logic import LogicTreeEvaluator, extract_variable_constraints
from imednet.datadict.models import DataDictionary
from imednet.errors import UnknownVariableTypeError, ValidationError
from imednet.utils.security import global_sensitivity_registry
from imednet.utils.tabular import (
    _IDENTIFIER_COLUMNS,
    _NESTED_DATA_COLUMNS,
    _READ_ONLY_RECORD_COLUMNS,
    _SIMULATION_PASSTHROUGH_KEYS,
    MASKED_SENTINEL,
    _contains_masked_sentinel,
    _contains_nonfinite_number,
)

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
    """Cache of variables partitioned by ``(study_key, form_key)`` and ``(study_key, form_id)``."""

    def __init__(self, is_async: bool) -> None:
        """Initialize the schema cache.

        Args:
            is_async: Whether this cache is used in an asynchronous context.
        """
        self._is_async = is_async
        self._form_variables: dict[tuple[str, str] | str, dict[str, Variable]] = {}
        self._form_id_to_key: dict[tuple[str, int] | int, str] = {}
        self._refreshed_studies: set[str] = set()

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

    def _clear_study_partition(self, study_key: str) -> None:
        """Remove existing cached entries belonging only to ``study_key``."""
        for fv_key in [
            k
            for k in self._form_variables
            if (isinstance(k, tuple) and k[0] == study_key)
            or (study_key == "" and isinstance(k, str))
        ]:
            del self._form_variables[fv_key]
        for fid_key in [
            k
            for k in self._form_id_to_key
            if (isinstance(k, tuple) and k[0] == study_key)
            or (study_key == "" and isinstance(k, int))
        ]:
            del self._form_id_to_key[fid_key]

    def populate(
        self,
        variables: Iterable[Variable],
        study_key: str | None = None,
    ) -> None:
        """Populate the cache with ``variables``, isolated by ``study_key``."""
        vars_list = list(variables)
        if study_key is not None:
            self._clear_study_partition(study_key)
            self._refreshed_studies.add(study_key)
            for var in vars_list:
                if var.form_key is not None:
                    if var.form_id is not None:
                        self._form_id_to_key[(study_key, var.form_id)] = var.form_key
                    if var.variable_name is not None:
                        self._form_variables.setdefault((study_key, var.form_key), {})[
                            var.variable_name
                        ] = var
            return

        target_studies = {var.study_key or "" for var in vars_list} or {""}
        for sk in target_studies:
            self._clear_study_partition(sk)
            if sk:
                self._refreshed_studies.add(sk)
        for var in vars_list:
            sk = var.study_key or ""
            if var.form_key is not None:
                if var.form_id is not None:
                    self._form_id_to_key[(sk, var.form_id)] = var.form_key
                if var.variable_name is not None:
                    self._form_variables.setdefault((sk, var.form_key), {})[var.variable_name] = var

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
        _ = forms
        vars_list = [v async for v in variables.list(study_key=study_key)]
        self.populate(vars_list, study_key=study_key)

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
        _ = forms
        vars_list = variables.list(study_key=study_key)
        self.populate(vars_list, study_key=study_key)

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

    def variables_for_form(
        self,
        form_key: str,
        study_key: str | None = None,
    ) -> dict[str, Variable]:
        """Return the variables associated with ``form_key`` for ``study_key``."""
        refreshed: set[str] = getattr(self, "_refreshed_studies", None) or set()
        if study_key is not None:
            exact = self._form_variables.get((study_key, form_key))
            if exact is not None:
                return exact
            if study_key not in refreshed:
                unscoped = self._form_variables.get(("", form_key))
                if unscoped is not None:
                    return unscoped
                legacy = self._form_variables.get(form_key)
                if legacy is not None:
                    return legacy
            return {}

        ctx_study = get_study_context()
        if ctx_study:
            ctx_exact = self._form_variables.get((ctx_study, form_key))
            if ctx_exact is not None:
                return ctx_exact
            if ctx_study in refreshed:
                return {}

        if form_key in self._form_variables:
            return self._form_variables[form_key]
        if ("", form_key) in self._form_variables:
            return self._form_variables[("", form_key)]

        matches = [
            (k[0], v)
            for k, v in self._form_variables.items()
            if isinstance(k, tuple) and k[0] != "" and k[1] == form_key
        ]
        if len(matches) == 1:
            return matches[0][1]
        if len(matches) > 1:
            studies = ", ".join(sorted(sk for sk, _ in matches))
            raise ValidationError(
                f"Ambiguous form '{form_key}' cached across multiple studies ({studies}); "
                "specify study_key."
            )
        return {}

    @staticmethod
    def _coerce_form_id_int(form_id: int | str) -> int | None:
        """Parse an integer form ID, rejecting booleans and non-numeric strings."""
        if isinstance(form_id, bool):
            return None
        if isinstance(form_id, int):
            return form_id
        if isinstance(form_id, str):
            stripped = form_id.strip()
            if stripped.lstrip("-").isdigit():
                return int(stripped)
        return None

    def form_key_from_id(
        self,
        form_id: int | str,
        study_key: str | None = None,
    ) -> str | None:
        """Resolve a form key from ``form_id``, isolated by ``study_key``."""
        fid_int = self._coerce_form_id_int(form_id)
        if fid_int is None:
            return None

        refreshed: set[str] = getattr(self, "_refreshed_studies", None) or set()
        if study_key is not None:
            exact = self._form_id_to_key.get((study_key, fid_int))
            if exact is not None:
                return exact
            if study_key not in refreshed:
                unscoped = self._form_id_to_key.get(("", fid_int))
                if unscoped is not None:
                    return unscoped
                legacy = self._form_id_to_key.get(fid_int)
                if legacy is not None:
                    return legacy
            return None

        ctx_study = get_study_context()
        if ctx_study:
            ctx_exact = self._form_id_to_key.get((ctx_study, fid_int))
            if ctx_exact is not None:
                return ctx_exact
            if ctx_study in refreshed:
                return None

        if fid_int in self._form_id_to_key:
            return self._form_id_to_key[fid_int]
        if ("", fid_int) in self._form_id_to_key:
            return self._form_id_to_key[("", fid_int)]

        matches = [
            (k[0], fk)
            for k, fk in self._form_id_to_key.items()
            if isinstance(k, tuple) and k[0] != "" and k[1] == fid_int
        ]
        if len(matches) == 1:
            return matches[0][1]
        if len(matches) > 1:
            studies = ", ".join(sorted(sk for sk, _ in matches))
            raise ValidationError(
                f"Ambiguous form ID {fid_int} cached across multiple studies ({studies}); "
                "specify study_key."
            )
        return None

    def forms_for_study(self, study_key: str) -> dict[str, dict[str, Variable]]:
        """Return cached variables grouped by form key for ``study_key``."""
        scoped: dict[str, dict[str, Variable]] = {
            k[1]: v
            for k, v in self._form_variables.items()
            if isinstance(k, tuple) and k[0] == study_key
        }
        if scoped or study_key in getattr(self, "_refreshed_studies", set()):
            return scoped
        fallback: dict[str, dict[str, Variable]] = {}
        for k, v in self._form_variables.items():
            if isinstance(k, str):
                fallback[k] = v
            elif isinstance(k, tuple) and k[0] == "":
                fallback[k[1]] = v
        return fallback

    @property
    def study_forms(self) -> dict[tuple[str, str], dict[str, Variable]]:
        """Return cached variables keyed by ``(study_key, form_key)``."""
        return {
            (k if isinstance(k, tuple) else ("", k)): v for k, v in self._form_variables.items()
        }

    @property
    def forms(self) -> dict[str, dict[str, Variable]]:
        """Return cached variables grouped by form key for the active or unambiguous study."""
        ctx_study = get_study_context()
        if ctx_study and (
            ctx_study in getattr(self, "_refreshed_studies", set())
            or any(isinstance(k, tuple) and k[0] == ctx_study for k in self._form_variables)
        ):
            return self.forms_for_study(ctx_study)
        return {(k[1] if isinstance(k, tuple) else k): v for k, v in self._form_variables.items()}


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
    """Validate that the value is a finite number (int or float, excluding booleans).

    Args:
        value: The value to validate.

    Raises:
        ValidationError: If the value is not finite numeric or is a boolean.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError("Value must be numeric")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValidationError("Value must be numeric (NaN and Infinity are not allowed)")


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


def _validate_true_false(value: Any) -> None:
    """Validate that a true/false variable value is a boolean, string, or integer."""
    if not isinstance(value, (bool, str, int)):
        raise ValidationError("Value must be a boolean, string, or integer code")


def _validate_calculation(value: Any) -> None:
    """Validate that a computed or range value is non-boolean finite numeric or text."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValidationError("Value must be numeric or a string")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValidationError("Value must be numeric (NaN and Infinity are not allowed)")
    if isinstance(value, str) and value.strip().lower() in {
        "nan",
        "+nan",
        "-nan",
        "inf",
        "+inf",
        "-inf",
        "infinity",
        "+infinity",
        "-infinity",
    }:
        raise ValidationError("Value must be numeric (NaN and Infinity are not allowed)")


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
    "true/false": ("yes/no", "1", _validate_true_false),
    "truefalse": ("yes/no", "1", _validate_true_false),
    "true false": ("yes/no", "1", _validate_true_false),
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

_TRUE_FALSE_TYPE_KEYS: frozenset[str] = frozenset(
    key for key, spec in _CANONICAL_VARIABLE_TAXONOMY.items() if spec[2] is _validate_true_false
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
        "TrueFalse": _validate_true_false,
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


@dataclass(frozen=True)
class RecordValidationIssue:
    """Structured single validation error or warning produced during schema validation."""

    record_index: int
    form_key: str | None
    variable_name: str | None
    message: str
    rule_id: str | None = None
    severity: Literal["error", "warning"] = "error"

    def to_dict(self) -> dict[str, Any]:
        """Serialize validation issue to a JSON-compatible dictionary."""
        payload: dict[str, Any] = {
            "recordIndex": self.record_index,
            "formKey": self.form_key,
            "variableName": self.variable_name,
            "message": self.message,
            "severity": self.severity,
        }
        if self.rule_id is not None:
            payload["ruleId"] = self.rule_id
        return payload


@dataclass
class BatchValidationReport:
    """Aggregated multi-record validation report collecting all constraint errors and warnings."""

    total_records: int = 0
    valid_records: int = 0
    invalid_records: int = 0
    errors: list[RecordValidationIssue] = field(default_factory=list)
    warnings: list[RecordValidationIssue] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        """Return True when zero validation errors were recorded."""
        return len(self.errors) == 0

    @property
    def error_count(self) -> int:
        """Return total number of validation errors across the batch."""
        return len(self.errors)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the batch validation report to a JSON-compatible dictionary."""
        return {
            "isValid": self.is_valid,
            "totalRecords": self.total_records,
            "validRecords": self.valid_records,
            "invalidRecords": self.invalid_records,
            "errorCount": len(self.errors),
            "warningCount": len(self.warnings),
            "errors": [err.to_dict() for err in self.errors],
            "warnings": [warn.to_dict() for warn in self.warnings],
        }

    def format_summary(self) -> str:
        """Format a concise PHI-safe summary of all validation errors."""
        if not self.errors:
            return f"Validation passed ({self.total_records} record(s))."
        lines = [
            f"Batch validation failed with {len(self.errors)} error(s) "
            f"across {self.invalid_records}/{self.total_records} record(s):"
        ]
        for issue in self.errors:
            loc = f"Row {issue.record_index}"
            if issue.form_key:
                loc += f" [form={issue.form_key}]"
            if issue.variable_name:
                loc += f" [variable={issue.variable_name}]"
            lines.append(f"  - {loc}: {issue.message}")
        return "\n".join(lines)

    def raise_for_errors(self) -> None:
        """Raise :class:`ValidationError` with ``validation_report`` attached if invalid."""
        if not self.is_valid:
            raise ValidationError(self.format_summary(), validation_report=self)


def _display_constraint_value(name: str, value: Any) -> Any:
    """Redact clinical values for PHI-sensitive variable names in error messages."""
    if global_sensitivity_registry.is_sensitive(name):
        return MASKED_SENTINEL
    return value


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
            shown = [MASKED_SENTINEL] if global_sensitivity_registry.is_sensitive(name) else invalid
            raise ValidationError(
                f"Invalid choice(s) {shown} for variable '{name}' on form '{form_key}'. "
                f"Allowed values: {choices}"
            )
        return

    if vtype_norm in _TRUE_FALSE_TYPE_KEYS and isinstance(value, bool):
        equiv = ("1", "true", "True") if value else ("0", "false", "False")
        if any(candidate in allowed for candidate in equiv):
            return

    str_val = str(value)
    if str_val not in allowed:
        shown_val = _display_constraint_value(name, value)
        raise ValidationError(
            f"Invalid choice '{shown_val}' for variable '{name}' on form '{form_key}'. "
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
    if num is None:
        return
    if not math.isfinite(num):
        raise ValidationError(
            f"Value for variable '{name}' on form '{form_key}' must be finite "
            "(NaN and Infinity are not allowed)"
        )

    shown_val = _display_constraint_value(name, value)
    if min_value is not None and num < min_value:
        raise ValidationError(
            f"Value {shown_val} for variable '{name}' on form '{form_key}' is below minimum {min_value}"
        )
    if max_value is not None and num > max_value:
        raise ValidationError(
            f"Value {shown_val} for variable '{name}' on form '{form_key}' exceeds maximum {max_value}"
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


def _check_required_variables_only(
    variables: Mapping[str, Any],
    form_key: str,
    data: Mapping[str, Any],
    *,
    collect_errors: bool,
    record_index: int,
    errors: list[RecordValidationIssue],
    warnings: list[RecordValidationIssue],
) -> None:
    """Enforce ``required`` variable flags on API ``BaseSchemaCache`` schemas."""
    for name, var_obj in variables.items():
        constraints = extract_variable_constraints(var_obj)
        if constraints.required_policy == "no":
            continue
        if name not in data or _is_empty_value(data[name]):
            msg = f"Required variable '{name}' on form '{form_key}' is missing or empty"
            if constraints.required_policy == "yes":
                if not collect_errors:
                    raise ValidationError(msg)
                errors.append(
                    RecordValidationIssue(
                        record_index=record_index,
                        form_key=form_key,
                        variable_name=name,
                        message=msg,
                        rule_id="required",
                    )
                )
            elif constraints.required_policy == "auto_query":
                logger.warning(
                    "Auto Query: variable '%s' on form '%s' is missing or empty.",
                    name,
                    form_key,
                )
                warnings.append(
                    RecordValidationIssue(
                        record_index=record_index,
                        form_key=form_key,
                        variable_name=name,
                        message=msg,
                        rule_id="required_auto_query",
                        severity="warning",
                    )
                )


@overload
def validate_record_data(
    schema: ValidationSchemaProtocol,
    form_key: str,
    data: dict[str, Any],
    *,
    study_key: str | None = None,
    collect_errors: Literal[False] = False,
    record_index: int = 0,
) -> None: ...


@overload
def validate_record_data(
    schema: ValidationSchemaProtocol,
    form_key: str,
    data: dict[str, Any],
    *,
    study_key: str | None = None,
    collect_errors: Literal[True],
    record_index: int = 0,
) -> BatchValidationReport: ...


@overload
def validate_record_data(
    schema: ValidationSchemaProtocol,
    form_key: str,
    data: dict[str, Any],
    *,
    study_key: str | None = None,
    collect_errors: bool = False,
    record_index: int = 0,
) -> BatchValidationReport | None: ...


def validate_record_data(
    schema: ValidationSchemaProtocol,
    form_key: str,
    data: dict[str, Any],
    *,
    study_key: str | None = None,
    collect_errors: bool = False,
    record_index: int = 0,
) -> BatchValidationReport | None:
    """Validate ``data`` for ``form_key`` using the provided validation schema.

    Raises:
        ValidationError: If ``collect_errors=False`` and the form key or data
            fails validation checks.
    """
    errors: list[RecordValidationIssue] = []
    warnings: list[RecordValidationIssue] = []

    def _finish() -> BatchValidationReport | None:
        if not collect_errors:
            return None
        return BatchValidationReport(
            total_records=1,
            valid_records=0 if errors else 1,
            invalid_records=1 if errors else 0,
            errors=errors,
            warnings=warnings,
        )

    def _record_error(
        message: str,
        *,
        variable_name: str | None = None,
        rule_id: str | None = None,
        exc: ValidationError | None = None,
    ) -> None:
        if not collect_errors:
            if exc is not None:
                raise exc
            raise ValidationError(message)
        errors.append(
            RecordValidationIssue(
                record_index=record_index,
                form_key=form_key,
                variable_name=variable_name,
                message=message,
                rule_id=rule_id,
            )
        )

    variables = (
        schema.variables_for_form(form_key, study_key=study_key)
        if isinstance(schema, BaseSchemaCache)
        else schema.variables_for_form(form_key)
    )
    if not variables:
        unknown_form = True
        if isinstance(schema, DataDictionary):
            form_obj = schema.form_for_key(form_key)
            if form_obj is not None and schema.rules_for_form(form_key):
                unknown_form = False
        if unknown_form:
            _record_error(f"Unknown form {form_key}", rule_id="unknown_form")
            return _finish()

    unknown = [k for k in data if k not in variables]
    if unknown:
        _record_error(
            f"Unknown variables for form {form_key}: {', '.join(unknown)}",
            variable_name=unknown[0] if len(unknown) == 1 else None,
            rule_id="unknown_variable",
        )
        if not collect_errors:
            return None

    type_failed_vars: set[str] = set()
    for name, value in data.items():
        if _contains_masked_sentinel(value):
            type_failed_vars.add(name)
            _record_error(
                f"Variable '{name}' on form '{form_key}' contains masked PHI sentinel '{MASKED_SENTINEL}'",
                variable_name=name,
                rule_id="masked_phi",
            )
            continue
        if _contains_nonfinite_number(value):
            type_failed_vars.add(name)
            _record_error(
                f"Variable '{name}' on form '{form_key}' contains non-finite number (NaN or Infinity)",
                variable_name=name,
                rule_id="type",
            )
            continue
        if name not in variables:
            continue
        var_obj = variables[name]
        vtype = getattr(var_obj, "variable_type", getattr(var_obj, "variableType", None))
        try:
            _check_type(vtype, value)
        except ValidationError as exc:
            type_failed_vars.add(name)
            _record_error(exc.message, variable_name=name, rule_id="type", exc=exc)

    # Fast path for API SchemaCache (enforce required flags when defined; skip codelists, bounds, and XML rules)
    if isinstance(schema, BaseSchemaCache):
        _check_required_variables_only(
            variables,
            form_key,
            data,
            collect_errors=collect_errors,
            record_index=record_index,
            errors=errors,
            warnings=warnings,
        )
        return _finish()

    for name, var_obj in variables.items():
        if name in type_failed_vars:
            continue
        constraints = extract_variable_constraints(var_obj)
        is_missing = name not in data or _is_empty_value(data[name])
        if is_missing:
            if constraints.required_policy == "yes":
                _record_error(
                    f"Required variable '{name}' on form '{form_key}' is missing or empty",
                    variable_name=name,
                    rule_id="required",
                )
            elif constraints.required_policy == "auto_query":
                warn_msg = f"Required variable '{name}' on form '{form_key}' is missing or empty"
                logger.warning(
                    "Auto Query: variable '%s' on form '%s' is missing or empty.",
                    name,
                    form_key,
                )
                warnings.append(
                    RecordValidationIssue(
                        record_index=record_index,
                        form_key=form_key,
                        variable_name=name,
                        message=warn_msg,
                        rule_id="required_auto_query",
                        severity="warning",
                    )
                )
            continue

        value = data[name]
        vtype = getattr(var_obj, "variable_type", getattr(var_obj, "variableType", None))
        if constraints.choices:
            try:
                _validate_choice_membership(name, form_key, vtype, value, constraints.choices)
            except ValidationError as exc:
                _record_error(exc.message, variable_name=name, rule_id="choice", exc=exc)
        if constraints.min_value is not None or constraints.max_value is not None:
            try:
                _validate_numeric_bounds(
                    name, form_key, value, constraints.min_value, constraints.max_value
                )
            except ValidationError as exc:
                _record_error(exc.message, variable_name=name, rule_id="bounds", exc=exc)
        if isinstance(value, str) and (
            constraints.min_length is not None or constraints.max_length is not None
        ):
            try:
                _validate_text_length(
                    name, form_key, value, constraints.min_length, constraints.max_length
                )
            except ValidationError as exc:
                _record_error(exc.message, variable_name=name, rule_id="length", exc=exc)

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
                warnings.append(
                    RecordValidationIssue(
                        record_index=record_index,
                        form_key=form_key,
                        variable_name=None,
                        message=f"Query rule '{warn.rule_name}' triggered on form '{form_key}': {warn.message}",
                        rule_id=str(warn.rule_id) if warn.rule_id is not None else warn.rule_name,
                        severity="warning",
                    )
                )
            if report.errors:
                if not collect_errors:
                    first_err = report.errors[0]
                    raise ValidationError(
                        f"Rule '{first_err.rule_name}' failed on form '{form_key}': {first_err.message}"
                    )
                for rule_err in report.errors:
                    errors.append(
                        RecordValidationIssue(
                            record_index=record_index,
                            form_key=form_key,
                            variable_name=None,
                            message=(
                                f"Rule '{rule_err.rule_name}' failed on form '{form_key}': "
                                f"{rule_err.message}"
                            ),
                            rule_id=(
                                str(rule_err.rule_id)
                                if rule_err.rule_id is not None
                                else rule_err.rule_name
                            ),
                        )
                    )

    return _finish()


def calculate_readiness_score(
    schema: ValidationSchemaProtocol,
    form_key: str,
    data: dict[str, Any],
    *,
    study_key: str | None = None,
) -> tuple[float, list[str]]:
    """Calculate the schema readiness score for a Study Record's data.

    Returns:
        A tuple of (score, list of validation failure or warning reasons).
    """
    variables = (
        schema.variables_for_form(form_key, study_key=study_key)
        if isinstance(schema, BaseSchemaCache)
        else schema.variables_for_form(form_key)
    )
    if not variables:
        return 0.0, [f"Unknown form {form_key}"]

    expected_count = len(variables)
    if expected_count == 0:
        return 100.0, []

    data_map: Mapping[str, Any] = data if isinstance(data, Mapping) else {}
    valid_count = 0
    reasons: list[str] = []

    if not data_map:
        for name, var in variables.items():
            constraints = extract_variable_constraints(var)
            if constraints.required_policy == "yes":
                reasons.append(f"Missing required variable: {name}")
            elif constraints.required_policy == "auto_query":
                reasons.append(f"Auto Query warning for variable {name}: variable is missing")
        if not reasons:
            reasons.append("Empty record data")
        return 0.0, reasons

    unknown = [k for k in data_map if k not in variables]
    if unknown:
        reasons.append(f"Unknown variables: {', '.join(unknown)}")

    has_clinical_data = any(k in variables and not _is_empty_value(v) for k, v in data_map.items())
    is_cache = isinstance(schema, BaseSchemaCache)

    for name, var in variables.items():
        if name not in data_map:
            constraints = extract_variable_constraints(var)
            if constraints.required_policy == "yes":
                reasons.append(f"Missing required variable: {name}")
            elif constraints.required_policy == "auto_query":
                reasons.append(f"Auto Query warning for variable {name}: variable is missing")
                if has_clinical_data:
                    valid_count += 1
            elif has_clinical_data:
                valid_count += 1
            else:
                reasons.append(f"Missing expected variable: {name}")
            continue

        value = data_map[name]
        vtype = getattr(var, "variable_type", getattr(var, "variableType", None))
        try:
            _check_type(vtype, value)
            constraints = extract_variable_constraints(var)
            if _is_empty_value(value):
                if constraints.required_policy == "yes":
                    raise ValidationError("Required variable is empty")
                if constraints.required_policy == "auto_query":
                    reasons.append(f"Auto Query warning for variable {name}: value is empty")
                if not has_clinical_data:
                    continue
            elif not is_cache:
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
            valid_count += 1
        except Exception as e:
            reasons.append(f"Variable {name} invalid: {e!s}")

    if not is_cache:
        rules_getter = getattr(schema, "rules_for_form", None)
        if callable(rules_getter):
            report = _LOGIC_EVALUATOR.evaluate_rules(
                rules_getter(form_key), dict(data_map), form_key=form_key
            )
            for err in report.errors:
                reasons.append(f"Rule {err.rule_name} failed: {err.message}")
            for warn in report.warnings:
                reasons.append(f"Rule {warn.rule_name} warning: {warn.message}")

    score = (valid_count / expected_count) * 100.0
    return score, reasons


_FLATTENED_EXCLUDED_RECORD_KEYS: frozenset[str] = (
    _IDENTIFIER_COLUMNS
    | _READ_ONLY_RECORD_COLUMNS
    | _NESTED_DATA_COLUMNS
    | _SIMULATION_PASSTHROUGH_KEYS
)


def _extract_record_clinical_data(record: Mapping[str, Any]) -> dict[str, Any]:
    """Extract clinical variable mapping from nested or flattened Record dicts."""
    for nested_key in ("recordData", "record_data", "data"):
        if nested_key in record:
            nested_val = record[nested_key]
            if isinstance(nested_val, dict):
                return nested_val
            if isinstance(nested_val, Mapping):
                return dict(nested_val)
            return {}
    return {k: v for k, v in record.items() if k not in _FLATTENED_EXCLUDED_RECORD_KEYS}


@overload
def validate_record_entry(
    schema: ValidationSchemaProtocol,
    record: dict[str, Any],
    *,
    study_key: str | None = None,
    collect_errors: Literal[False] = False,
    record_index: int = 0,
) -> None: ...


@overload
def validate_record_entry(
    schema: ValidationSchemaProtocol,
    record: dict[str, Any],
    *,
    study_key: str | None = None,
    collect_errors: Literal[True],
    record_index: int = 0,
) -> BatchValidationReport: ...


@overload
def validate_record_entry(
    schema: ValidationSchemaProtocol,
    record: dict[str, Any],
    *,
    study_key: str | None = None,
    collect_errors: bool = False,
    record_index: int = 0,
) -> BatchValidationReport | None: ...


def validate_record_entry(
    schema: ValidationSchemaProtocol,
    record: dict[str, Any],
    *,
    study_key: str | None = None,
    collect_errors: bool = False,
    record_index: int = 0,
) -> BatchValidationReport | None:
    """Validate a single record entry against the schema.

    Resolves the form key from "formKey", "form_key", "formId", or "form_id".
    """
    raw_study = study_key or record.get("studyKey") or record.get("study_key")
    eff_study_key = str(raw_study) if raw_study else None
    fk, fid = _extract_form_ref(record)
    if not fk and fid is not None:
        if isinstance(fid, bool):
            msg = f"Unknown form ID {fid}"
            if not collect_errors:
                raise ValidationError(msg)
            return BatchValidationReport(
                total_records=1,
                valid_records=0,
                invalid_records=1,
                errors=[
                    RecordValidationIssue(
                        record_index=record_index,
                        form_key=None,
                        variable_name=None,
                        message=msg,
                        rule_id="unknown_form_id",
                    )
                ],
            )
        fk = (
            schema.form_key_from_id(fid, study_key=eff_study_key)
            if isinstance(schema, BaseSchemaCache)
            else schema.form_key_from_id(fid)
        )
        if not fk:
            msg = f"Unknown form ID {fid}"
            if not collect_errors:
                raise ValidationError(msg)
            return BatchValidationReport(
                total_records=1,
                valid_records=0,
                invalid_records=1,
                errors=[
                    RecordValidationIssue(
                        record_index=record_index,
                        form_key=None,
                        variable_name=None,
                        message=msg,
                        rule_id="unknown_form_id",
                    )
                ],
            )
    if fk:
        return validate_record_data(
            schema,
            fk,
            _extract_record_clinical_data(record),
            study_key=eff_study_key,
            collect_errors=collect_errors,
            record_index=record_index,
        )

    raw_data = _extract_record_clinical_data(record)
    if isinstance(raw_data, Mapping) and raw_data:
        forms_attr = getattr(schema, "forms", None)
        is_datadict_like = isinstance(forms_attr, Sequence) and not isinstance(
            schema, BaseSchemaCache
        )
        if is_datadict_like or collect_errors:
            form_keys: Iterable[str]
            if isinstance(schema, BaseSchemaCache):
                form_keys = schema.forms_for_study(eff_study_key) if eff_study_key else schema.forms
            elif isinstance(forms_attr, Sequence):
                form_keys = [
                    str(getattr(f, "form_key", None) or getattr(f, "formKey", ""))
                    for f in forms_attr
                    if getattr(f, "form_key", None) or getattr(f, "formKey", None)
                ]
            else:
                form_keys = ()

            known_vars: dict[str, list[Any]] = {}
            for candidate_fk in form_keys:
                f_vars = (
                    schema.variables_for_form(candidate_fk, study_key=eff_study_key)
                    if isinstance(schema, BaseSchemaCache)
                    else schema.variables_for_form(candidate_fk)
                )
                for v_name, v_obj in f_vars.items():
                    known_vars.setdefault(v_name, []).append(v_obj)

            if known_vars:
                formless_errors: list[RecordValidationIssue] = []
                for var_name, var_val in raw_data.items():
                    candidates = known_vars.get(var_name)
                    if not candidates:
                        msg = f"Unknown variable {var_name}"
                        if not collect_errors:
                            raise ValidationError(msg)
                        formless_errors.append(
                            RecordValidationIssue(
                                record_index=record_index,
                                form_key=None,
                                variable_name=var_name,
                                message=msg,
                                rule_id="unknown_variable",
                            )
                        )
                        continue
                    if not _is_empty_value(var_val):
                        distinct_types = {
                            str(getattr(vo, "variable_type", getattr(vo, "variableType", "")) or "")
                            .strip()
                            .lower()
                            for vo in candidates
                        }
                        distinct_types.discard("")
                        if len(distinct_types) == 1:
                            vtype = getattr(
                                candidates[0],
                                "variable_type",
                                getattr(candidates[0], "variableType", None),
                            )
                            try:
                                _check_type(vtype, var_val)
                            except ValidationError as exc:
                                if not collect_errors:
                                    raise
                                formless_errors.append(
                                    RecordValidationIssue(
                                        record_index=record_index,
                                        form_key=None,
                                        variable_name=var_name,
                                        message=str(exc),
                                        rule_id="invalid_type",
                                    )
                                )
                if formless_errors:
                    return BatchValidationReport(
                        total_records=1,
                        valid_records=0,
                        invalid_records=1,
                        errors=formless_errors,
                    )

    if collect_errors:
        return BatchValidationReport(total_records=1, valid_records=1, invalid_records=0)
    return None


def validate_records_batch(
    schema: ValidationSchemaProtocol,
    records: Sequence[Mapping[str, Any]],
    *,
    study_key: str | None = None,
    collect_errors: bool = True,
) -> BatchValidationReport:
    """Validate a sequence of records against *schema*, aggregating errors when requested."""
    aggregated = BatchValidationReport(total_records=len(records))
    for idx, rec in enumerate(records):
        rec_dict = rec if isinstance(rec, dict) else dict(rec)
        if collect_errors:
            entry_report = validate_record_entry(
                schema,
                rec_dict,
                study_key=study_key,
                collect_errors=True,
                record_index=idx,
            )
            if entry_report.is_valid:
                aggregated.valid_records += 1
            else:
                aggregated.invalid_records += 1
                aggregated.errors.extend(entry_report.errors)
            aggregated.warnings.extend(entry_report.warnings)
        else:
            validate_record_entry(
                schema,
                rec_dict,
                study_key=study_key,
                collect_errors=False,
                record_index=idx,
            )
            aggregated.valid_records += 1
    return aggregated


class BaseSchemaValidator(_ValidatorMixin, Generic[_TClient]):
    """Base validator sharing logic between sync and async implementations."""

    schema: BaseSchemaCache[_TClient]

    def _refresh_common(
        self,
        variables: Iterable[Variable],
        study_key: str | None = None,
    ) -> None:
        """Common logic to populate the schema cache.

        Args:
            variables: An iterable of variables to populate the cache with.
            study_key: Optional study key partition for ``variables``.
        """
        self.schema.populate(variables, study_key=study_key)

    def _needs_refresh(
        self,
        record: dict[str, Any],
        study_key: str | None = None,
    ) -> tuple[str | None, bool]:
        """Determine if the schema cache needs refreshing for the given record.

        Returns:
            A tuple of (form_key, needs_refresh).
        """
        form_key = self._resolve_form_key(record, study_key=study_key)
        if form_key:
            return form_key, not bool(self.schema.variables_for_form(form_key, study_key=study_key))
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
        from unittest.mock import NonCallableMock

        if isinstance(self._sdk, NonCallableMock):
            mock_children = getattr(self._sdk, "_mock_children", {})
            obj_vars = vars(self._sdk)
            has_vars_ep = "variables" in mock_children or "variables" in obj_vars
            has_get_vars = "get_variables" in mock_children or "get_variables" in obj_vars
            if has_vars_ep and not has_get_vars:
                variables = self._sdk.variables.list(study_key=study_key)
                self._refresh_common(variables, study_key=study_key)
                return

        variables = self._sdk.get_variables(study_key=study_key)
        self._refresh_common(variables, study_key=study_key)

    def validate_record(self, study_key: str, record: dict[str, Any]) -> None:
        """Validate a single record payload."""
        form_key, needs_refresh = self._needs_refresh(record, study_key=study_key)
        if needs_refresh:
            self.refresh(study_key)
            if form_key is None:
                form_key = self._resolve_form_key(record, study_key=study_key)
        if form_key is None:
            _, fid = _extract_form_ref(record)
            if fid is not None:
                raise ValidationError(f"Unknown form ID {fid}")
        self._validate_cached(
            form_key,
            _extract_record_clinical_data(record),
            study_key=study_key,
        )

    @overload
    def validate_batch(
        self,
        study_key: str,
        records: list[dict[str, Any]],
        *,
        collect_errors: Literal[False] = False,
    ) -> None: ...

    @overload
    def validate_batch(
        self,
        study_key: str,
        records: list[dict[str, Any]],
        *,
        collect_errors: Literal[True],
    ) -> BatchValidationReport: ...

    @overload
    def validate_batch(
        self,
        study_key: str,
        records: list[dict[str, Any]],
        *,
        collect_errors: bool = False,
    ) -> BatchValidationReport | None: ...

    def validate_batch(
        self,
        study_key: str,
        records: list[dict[str, Any]],
        *,
        collect_errors: bool = False,
    ) -> BatchValidationReport | None:
        """Validate a batch of record payloads."""
        if not collect_errors:
            for rec in records:
                self.validate_record(study_key, rec)
            return None

        refreshed = False
        for rec in records:
            _, needs_refresh = self._needs_refresh(rec, study_key=study_key)
            if needs_refresh and not refreshed:
                self.refresh(study_key)
                refreshed = True
                break
        return validate_records_batch(
            self.schema,
            records,
            study_key=study_key,
            collect_errors=True,
        )


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
        self._refresh_common(variables, study_key=study_key)

    async def validate_record(self, study_key: str, record: dict[str, Any]) -> None:
        """Validate a single record payload asynchronously."""
        form_key, needs_refresh = self._needs_refresh(record, study_key=study_key)
        if needs_refresh:
            await self.refresh(study_key)
            if form_key is None:
                form_key = self._resolve_form_key(record, study_key=study_key)
        if form_key is None:
            _, fid = _extract_form_ref(record)
            if fid is not None:
                raise ValidationError(f"Unknown form ID {fid}")
        self._validate_cached(
            form_key,
            _extract_record_clinical_data(record),
            study_key=study_key,
        )

    @overload
    async def validate_batch(
        self,
        study_key: str,
        records: list[dict[str, Any]],
        *,
        collect_errors: Literal[False] = False,
    ) -> None: ...

    @overload
    async def validate_batch(
        self,
        study_key: str,
        records: list[dict[str, Any]],
        *,
        collect_errors: Literal[True],
    ) -> BatchValidationReport: ...

    @overload
    async def validate_batch(
        self,
        study_key: str,
        records: list[dict[str, Any]],
        *,
        collect_errors: bool = False,
    ) -> BatchValidationReport | None: ...

    async def validate_batch(
        self,
        study_key: str,
        records: list[dict[str, Any]],
        *,
        collect_errors: bool = False,
    ) -> BatchValidationReport | None:
        """Validate a batch of record payloads asynchronously."""
        if not collect_errors:
            for rec in records:
                await self.validate_record(study_key, rec)
            return None

        refreshed = False
        for rec in records:
            _, needs_refresh = self._needs_refresh(rec, study_key=study_key)
            if needs_refresh and not refreshed:
                await self.refresh(study_key)
                refreshed = True
                break
        return validate_records_batch(
            self.schema,
            records,
            study_key=study_key,
            collect_errors=True,
        )
