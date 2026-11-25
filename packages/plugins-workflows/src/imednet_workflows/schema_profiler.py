"""Workflow for profiling form and record-data population across cached records."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from imednet.spi.models import Record, Variable
from imednet.spi.utils import is_boolean_token
from imednet.spi.validation import SchemaCache

from .cached_loader import CachedRecordsLoader

if TYPE_CHECKING:
    from imednet.spi.facade import ImednetFacade

_SCHEMA_TYPE_MAP = {
    "bool": "boolean",
    "boolean": "boolean",
    "checkbox": "boolean",
    "date": "date",
    "datetime": "date",
    "decimal": "numeric",
    "float": "numeric",
    "int": "numeric",
    "integer": "numeric",
    "number": "numeric",
}


class FieldProfile(BaseModel):
    """Summary statistics for a single form field."""

    variable_name: str
    label: str
    population_rate: float
    inferred_type: str
    unique_count: int = 0
    unique_values: list[str] = Field(default_factory=list)


class FormProfile(BaseModel):
    """Summary statistics for a single form."""

    form_key: str
    form_name: str
    record_count: int
    fields: dict[str, FieldProfile] = Field(default_factory=dict)


class SchemaProfiler:
    """Profile form and record-data population across cached records."""

    def __init__(self, sdk: ImednetFacade, loader: CachedRecordsLoader | None = None) -> None:
        """Initialize the schema profiler.

        Args:
            sdk: An instance of the iMednet SDK facade.
            loader: Optional cached records loader. If not provided, one will be created.
        """
        self._sdk = sdk
        self._loader = loader

    def profile_records(
        self,
        study_key: str,
        *,
        records: Iterable[Record] | None = None,
    ) -> dict[str, FormProfile]:
        """Return per-form field profiling statistics for ``study_key``."""
        forms = self._sdk.get_forms(study_key=study_key)
        variables = self._sdk.get_variables(study_key=study_key)
        schema = SchemaCache()
        schema.populate(variables)
        form_names: dict[str, str] = {
            form.form_key: form.form_name or form.form_key for form in forms if form.form_key
        }
        records_iterable = records if records is not None else self._iter_records(study_key)

        form_accumulators: dict[str, _FormAccumulator] = {}
        for record in records_iterable:
            resolved_form_key = (
                schema.form_key_from_id(record.form_id) or str(record.form_id)
                if record.form_id is not None
                else None
            )
            form_key = record.form_key or resolved_form_key
            if not form_key:
                continue
            accumulator = form_accumulators.setdefault(form_key, _FormAccumulator())
            accumulator.record_count += 1

            if not isinstance(record.record_data, dict):
                continue
            for field_name, value in record.record_data.items():
                field_accumulator = accumulator.fields.setdefault(field_name, _FieldAccumulator())
                field_accumulator.observe(value)

        profiles: dict[str, FormProfile] = {}
        for form_key, accumulator in form_accumulators.items():
            schema_variables = schema.variables_for_form(form_key)
            field_names = sorted(set(schema_variables).union(accumulator.fields))

            fields = {
                field_name: self._build_field_profile(
                    field_name=field_name,
                    record_count=accumulator.record_count,
                    accumulator=accumulator.fields.get(field_name, _FieldAccumulator()),
                    variable=schema_variables.get(field_name),
                )
                for field_name in field_names
            }
            profiles[form_key] = FormProfile(
                form_key=form_key,
                form_name=form_names.get(form_key, form_key),
                record_count=accumulator.record_count,
                fields=fields,
            )
        return profiles

    def _iter_records(self, study_key: str) -> Iterable[Record]:
        """Internal helper to iterate over records for profiling."""
        if self._loader is None:
            self._loader = CachedRecordsLoader(self._sdk)
        sync_method = getattr(self._loader, "sync_records", None)
        # Check class-level iterator presence to avoid dynamic mock attributes
        # being treated as real chunked iterators.
        iter_method = getattr(type(self._loader), "iter_cached_records", None)
        if callable(iter_method):
            if callable(sync_method):
                sync_method(study_key)
            else:
                self._loader.load_records(study_key)
            return self._loader.iter_cached_records(study_key)
        return self._loader.load_records(study_key)

    def _build_field_profile(
        self,
        *,
        field_name: str,
        record_count: int,
        accumulator: _FieldAccumulator,
        variable: Variable | None,
    ) -> FieldProfile:
        """Internal helper to construct a FieldProfile from accumulated statistics."""
        inferred_type = accumulator.inferred_type(variable)
        population_rate = 0.0
        if record_count:
            population_rate = round((accumulator.populated_count / record_count) * 100, 2)

        label = field_name
        if variable is not None:
            var_label = getattr(variable, 'label', None)
            if not var_label:
                extra = getattr(variable, 'model_extra', None)
                if extra:
                    var_label = extra.get('label', '')
            if var_label:
                label = str(var_label)

        return FieldProfile(
            variable_name=field_name,
            label=label,
            population_rate=population_rate,
            inferred_type=inferred_type,
            unique_count=accumulator.unique_count,
            unique_values=accumulator.sample_values,
        )


@dataclass
class _FormAccumulator:
    """Internal helper to accumulate statistics for a form."""

    record_count: int = 0
    fields: dict[str, _FieldAccumulator] = field(default_factory=dict)


@dataclass
class _FieldAccumulator:
    """Internal helper to accumulate statistics for a form field."""

    populated_count: int = 0
    unique_values: set[str] = field(default_factory=set)
    sample_values: list[str] = field(default_factory=list)
    all_boolean: bool = True
    all_numeric: bool = True
    all_date: bool = True

    @property
    def unique_count(self) -> int:
        """Return the number of unique values observed."""
        return len(self.unique_values)

    def observe(self, value: Any) -> None:
        """Record an observation of a field value."""
        if not _is_populated(value):
            return

        self.populated_count += 1
        rendered_value = _render_value(value)
        if rendered_value not in self.unique_values and len(self.sample_values) < 10:
            self.sample_values.append(rendered_value)
        self.unique_values.add(rendered_value)

        self.all_boolean = self.all_boolean and _is_boolean_value(value)
        self.all_numeric = self.all_numeric and _is_numeric_value(value)
        self.all_date = self.all_date and _is_date_value(value)

    def inferred_type(self, variable: Variable | None) -> str:
        """Infer the data type based on observed values and optional variable definition."""
        if self.populated_count > 0:
            if self.all_boolean:
                return "boolean"
            if self.all_numeric:
                return "numeric"
            if self.all_date:
                return "date"
            return "string"

        if variable is None:
            return "string"
        return _SCHEMA_TYPE_MAP.get((variable.variable_type or "").lower(), "string")


def _is_populated(value: Any) -> bool:
    """Return True if the value is considered 'populated' (not null or empty)."""
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    if isinstance(value, (list, dict)):
        return len(value) > 0
    return True


def _render_value(value: Any) -> str:
    """Render a value to a string representation for statistics."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return str(value)


from imednet.spi.utils import is_boolean_token


def _is_boolean_value(value: Any) -> bool:
    """Return True if the value can be interpreted as a boolean."""
    if isinstance(value, bool):
        return True
    if isinstance(value, str):
        return is_boolean_token(value)
    return False


def _is_numeric_value(value: Any) -> bool:
    """Return True if the value can be interpreted as numeric."""
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        try:
            float(value.strip())
        except ValueError:
            return False
        return True
    return False


def _is_date_value(value: Any) -> bool:
    """Return True if the value can be interpreted as a date or datetime."""
    if isinstance(value, (date, datetime)):
        return True
    if not isinstance(value, str):
        return False

    normalised = value.strip()
    if not normalised:
        return False

    from imednet.spi.utils import parse_iso_datetime

    try:
        parse_iso_datetime(normalised)
        return True
    except ValueError:
        return False
