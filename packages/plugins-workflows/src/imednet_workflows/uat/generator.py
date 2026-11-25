"""Synthetic record payload generator for UAT."""

from __future__ import annotations

import logging
import random
import re
from datetime import UTC, date, datetime
from typing import Any

from pydantic import Field

from imednet.spi.models import ImednetBaseModel
from imednet.spi.validation import DataDictionary, extract_variable_constraints

from .inspector import StudySnapshot
from .models import (
    RecordTestType,
    UATFormSpec,
    UATSpecification,
    UATVariableSpec,
    VariableTestStrategy,
)

logger = logging.getLogger(__name__)

_TYPE_SEP_RE = re.compile(r"[\s_-]+")
_SLASH_SPACE_RE = re.compile(r"\s*/\s*")

_INT_TYPES = frozenset({"number", "integer", "int"})
_FLOAT_TYPES = frozenset({"float", "decimal", "double", "numeric", "slider"})
_BOOL_TYPES = frozenset({"boolean", "bool"})
_YES_NO_TYPES = frozenset({"yes/no", "yesno", "yes no", "true/false", "truefalse", "true false"})
_SINGLE_CHOICE_TYPES = frozenset(
    {
        "coded",
        "radio",
        "radiobutton",
        "radio button",
        "single select",
        "singleselect",
        "dropdown",
        "drop down",
        "dropdownlist",
        "dropdown list",
        "select",
        "sql",
    }
)
_MULTI_CHOICE_TYPES = frozenset({"checkbox", "check box", "multi select", "multiselect"})
_TEXT_TYPES = frozenset({"text", "string", "alphanumeric"})
_MEMO_TYPES = frozenset({"textarea", "text area", "memo", "notes", "note"})
_CALC_TYPES = frozenset({"calculation", "calc", "calculated"})
_DATE_TYPES = frozenset(
    {
        "date",
        "date ymd",
        "date mdy",
        "date dmy",
        "date/time precision",
        "datetime precision",
        "date time precision",
    }
)
_DATETIME_TYPES = frozenset(
    {"datetime", "date/time", "date time", "durationdatetime", "intervaldatetime"}
)
_SKIP_EMPTY_TYPES = frozenset(
    {"signature", "file", "upload", "fileupload", "file upload", "descriptive", "label"}
)


def _normalize_var_type(var_type: str | None) -> str:
    """Normalize variable type string for canonical taxonomy matching."""
    if not var_type:
        return ""
    cleaned = _TYPE_SEP_RE.sub(" ", var_type.strip().lower())
    if "/" in cleaned:
        cleaned = _SLASH_SPACE_RE.sub("/", cleaned)
    return cleaned


class GeneratedRecordSet(ImednetBaseModel):
    """Output of the generator for a single form spec."""

    form_key: str
    form_name: str
    test_type: RecordTestType
    payloads: list[dict[str, Any]]  # ready to pass to records.create()
    subject_keys: list[str]  # subject keys that were used/will be used
    warnings: list[str] = Field(default_factory=list)  # non-fatal issues (skipped vars, etc.)


class SyntheticRecordGenerator:
    """Generate synthetic record payloads from a UATSpecification and StudySnapshot.

    Parameters
    ----------
    seed : Optional[int]
        Random seed for reproducible generation. None means non-deterministic.
    locale : str
        Locale (no longer used, kept for backwards compatibility).
    """

    def __init__(
        self,
        seed: int | None = None,
        locale: str = "en_US",
        data_dictionary: DataDictionary | None = None,
    ) -> None:
        """Initialize the generator.

        Args:
            seed: Optional seed for reproducibility.
            locale: Ignored.
            data_dictionary: Optional DataDictionary used to enrich variable choices and bounds.
        """
        _ = locale
        self._rng = random.Random(seed)  # noqa: S311
        self._seeded = seed is not None
        self._data_dictionary = data_dictionary

    def _lexify(self, text: str) -> str:
        """Return a mocked value."""
        out = []
        for c in text:
            if c == '?':
                out.append(self._rng.choice('abcdefghijklmnopqrstuvwxyz'))
            else:
                out.append(c)
        return "".join(out)

    def _paragraph(self, nb_sentences: int = 3) -> str:
        """Return a mocked value."""
        words = ["test", "mock", "fake", "sample", "data", "example"]
        sentences = []
        for _ in range(nb_sentences):
            sentence_words = [self._rng.choice(words) for _ in range(3)]
            sentence_words[0] = sentence_words[0].title()
            sentences.append(" ".join(sentence_words) + ".")
        return " ".join(sentences)

    def _enrich_form_spec(
        self, form_spec: UATFormSpec, data_dictionary: DataDictionary | None
    ) -> UATFormSpec:
        """Enrich variable specs with DataDictionary choices/bounds without mutating caller spec."""
        if data_dictionary is None:
            return form_spec
        dd_vars = data_dictionary.variables_for_form(form_spec.form_key)
        if not dd_vars:
            return form_spec

        enriched_vars: list[UATVariableSpec] = []
        changed = False
        for v in form_spec.variables:
            dd_var = dd_vars.get(v.variable_name)
            if dd_var is None:
                enriched_vars.append(v)
                continue
            constraints = extract_variable_constraints(dd_var)
            updates: dict[str, Any] = {}
            dd_type = getattr(dd_var, "variable_type", getattr(dd_var, "variableType", None))
            snap_norm = _normalize_var_type(v.variable_type)
            dd_norm = _normalize_var_type(dd_type)
            if dd_type and (
                not snap_norm
                or (
                    snap_norm == "coded"
                    and (
                        dd_norm in _SINGLE_CHOICE_TYPES
                        or dd_norm in _MULTI_CHOICE_TYPES
                        or dd_norm in _YES_NO_TYPES
                    )
                )
                or (snap_norm == "text" and dd_norm != "text")
            ):
                updates["variable_type"] = dd_type
            if not v.coded_values and constraints.choices:
                updates["coded_values"] = list(constraints.choices)
            if v.min_value is None and constraints.min_value is not None:
                updates["min_value"] = constraints.min_value
            if v.max_value is None and constraints.max_value is not None:
                updates["max_value"] = constraints.max_value
            if v.max_length is None and constraints.max_length is not None:
                updates["max_length"] = constraints.max_length
            if not v.required and constraints.required_policy == "yes":
                updates["required"] = True
            if updates:
                changed = True
                enriched_vars.append(v.model_copy(update=updates))
            else:
                enriched_vars.append(v)

        if not changed:
            return form_spec
        return form_spec.model_copy(update={"variables": enriched_vars})

    def generate(
        self,
        spec: UATSpecification,
        snapshot: StudySnapshot,
        *,
        subject_keys: list[str] | None = None,
        data_dictionary: DataDictionary | None = None,
    ) -> list[GeneratedRecordSet]:
        """Generate all record payloads for the enabled forms in the spec.

        Returns one GeneratedRecordSet per enabled UATFormSpec, in the correct
        submission order: RegisterSubject records first, then scheduled/unscheduled.
        """
        active_dd = data_dictionary if data_dictionary is not None else self._data_dictionary
        results: list[GeneratedRecordSet] = []
        subject_pool = list(subject_keys) if subject_keys else self._get_subject_pool(spec)

        # Phase 1: Enrollment/registration forms
        for form_spec in spec.forms_by_type(RecordTestType.REGISTER_SUBJECT):
            enriched = self._enrich_form_spec(form_spec, active_dd)
            results.append(self._generate_registration_set(spec, enriched, snapshot, subject_pool))

        # Phase 2: Scheduled forms
        for form_spec in spec.forms_by_type(RecordTestType.UPDATE_SCHEDULED_RECORD):
            enriched = self._enrich_form_spec(form_spec, active_dd)
            results.append(self._generate_scheduled_set(spec, enriched, snapshot, subject_pool))

        # Phase 3: Unscheduled forms
        for form_spec in spec.forms_by_type(RecordTestType.CREATE_NEW_RECORD):
            enriched = self._enrich_form_spec(form_spec, active_dd)
            results.append(self._generate_unscheduled_set(spec, enriched, snapshot, subject_pool))

        return results

    @staticmethod
    def resolve_subject_keys(
        record_sets: list[GeneratedRecordSet],
        enrolled_subject_keys: list[str],
        *,
        placeholder_keys: list[str | None] | list[str] | None = None,
        preserved_keys: set[str] | None = None,
    ) -> dict[str, str]:
        """Bind server-assigned Phase 1 Subject keys into Phase 2 GeneratedRecordSet payloads."""
        clean_enrolled = [k for k in enrolled_subject_keys if isinstance(k, str) and k]
        if not clean_enrolled:
            return {}

        preserved = preserved_keys or set()
        key_map: dict[str, str] = {pk: pk for pk in preserved}
        if placeholder_keys is not None and len(placeholder_keys) == len(clean_enrolled):
            for placeholder, enrolled in zip(placeholder_keys, clean_enrolled, strict=False):
                if placeholder and placeholder not in preserved:
                    key_map.setdefault(placeholder, enrolled)
        else:
            candidates: list[str] = []
            seen: set[str] = set()
            if placeholder_keys:
                for sk in placeholder_keys:
                    if sk and sk not in preserved and sk not in seen:
                        seen.add(sk)
                        candidates.append(sk)
            else:
                for rs in record_sets:
                    for sk in rs.subject_keys:
                        if sk and sk not in preserved and sk not in seen:
                            seen.add(sk)
                            candidates.append(sk)
            for idx, placeholder in enumerate(candidates):
                key_map[placeholder] = clean_enrolled[idx % len(clean_enrolled)]

        for rs in record_sets:
            if rs.test_type == RecordTestType.REGISTER_SUBJECT:
                rs.subject_keys = [key_map.get(sk, sk) for sk in rs.subject_keys]
                for payload in rs.payloads:
                    payload.pop("subjectKey", None)
                    payload.pop("subject_key", None)
                continue

            new_subject_keys: list[str] = []
            for idx, payload in enumerate(rs.payloads):
                orig_key = (
                    rs.subject_keys[idx]
                    if idx < len(rs.subject_keys)
                    else payload.get("subjectKey")
                )
                mapped = key_map.get(orig_key) if isinstance(orig_key, str) else None
                resolved_key: str = (
                    mapped if mapped is not None else clean_enrolled[idx % len(clean_enrolled)]
                )
                payload["subjectKey"] = resolved_key
                new_subject_keys.append(resolved_key)

            if len(rs.subject_keys) > len(new_subject_keys):
                for extra_key in rs.subject_keys[len(new_subject_keys) :]:
                    new_subject_keys.append(key_map.get(extra_key, extra_key))
            rs.subject_keys = new_subject_keys

        return key_map

    def _get_subject_pool(self, spec: UATSpecification) -> list[str]:
        """Return a mocked value."""
        pool = []
        for s_spec in spec.subject_specs:
            for i in range(s_spec.subject_count):
                pool.append(f"{s_spec.subject_key_prefix}{i + 1:03d}")
        if not pool:
            pool = ["UAT-TEST-001"]
        return pool

    @staticmethod
    def _coerce_fixed_value(var_spec: UATVariableSpec) -> Any:
        """Coerce a FIXED strategy value to match the variable's canonical scalar type."""
        raw = var_spec.fixed_value
        if raw is None:
            return ""
        v_norm = _normalize_var_type(var_spec.variable_type)
        if v_norm in _INT_TYPES:
            if isinstance(raw, bool):
                return int(raw)
            if isinstance(raw, int):
                return raw
            try:
                return int(float(raw))
            except (TypeError, ValueError):
                return raw
        if v_norm in _FLOAT_TYPES:
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                return float(raw)
            try:
                return float(raw)
            except (TypeError, ValueError):
                return raw
        if v_norm in _BOOL_TYPES:
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, (int, float)):
                return bool(raw)
            if isinstance(raw, str):
                low = raw.strip().lower()
                if low in {"true", "1", "yes"}:
                    return True
                if low in {"false", "0", "no"}:
                    return False
            return raw
        if isinstance(raw, (int, float, bool)) and v_norm in _CALC_TYPES:
            return int(raw) if isinstance(raw, int) and not isinstance(raw, bool) else float(raw)
        return str(raw)

    @staticmethod
    def _boundary_scalar(var_spec: UATVariableSpec, *, is_max: bool) -> int | float:
        """Compute a typed min or max boundary scalar for numeric variables."""
        v_norm = _normalize_var_type(var_spec.variable_type)
        if v_norm in _INT_TYPES or v_norm in _CALC_TYPES:
            min_i = int(var_spec.min_value) if var_spec.min_value is not None else 1
            if not is_max:
                return min_i
            return int(var_spec.max_value) if var_spec.max_value is not None else max(100, min_i)
        min_f = float(var_spec.min_value) if var_spec.min_value is not None else 1.0
        if not is_max:
            return min_f
        return float(var_spec.max_value) if var_spec.max_value is not None else max(100.0, min_f)

    def _generate_value(self, var_spec: UATVariableSpec, spec: UATSpecification) -> Any:
        """Generate a single value for a variable according to its strategy."""
        if var_spec.strategy == VariableTestStrategy.SKIP:
            return None
        if var_spec.strategy == VariableTestStrategy.FIXED:
            return self._coerce_fixed_value(var_spec)

        return self._synthesize_value(var_spec, spec)

    def _synthesize_value(self, var_spec: UATVariableSpec, spec: UATSpecification) -> Any:
        """Return a mocked value appropriate for the canonical variable type."""
        v_type = var_spec.variable_type
        v_norm = _normalize_var_type(v_type)

        if v_norm in _SINGLE_CHOICE_TYPES:
            if var_spec.coded_values:
                return self._rng.choice(var_spec.coded_values)
            return "1" if var_spec.required else ""

        if v_norm in _MULTI_CHOICE_TYPES:
            if var_spec.coded_values:
                count = self._rng.randint(1, min(3, len(var_spec.coded_values)))
                choices = self._rng.sample(var_spec.coded_values, count)
                return "`".join(choices)
            return True if var_spec.required else ""

        if v_norm in _BOOL_TYPES:
            return self._rng.choice([True, False])

        if v_norm in _YES_NO_TYPES:
            if var_spec.coded_values:
                return self._rng.choice(var_spec.coded_values)
            return self._rng.choice(["Yes", "No"])

        if v_norm in _TEXT_TYPES:
            if var_spec.coded_values:
                return self._rng.choice(var_spec.coded_values)
            max_len = var_spec.max_length or spec.global_text_length or 10
            return self._lexify("?" * max_len)

        if v_norm in _INT_TYPES:
            if var_spec.coded_values:
                chosen = self._rng.choice(var_spec.coded_values)
                try:
                    return int(float(chosen))
                except (TypeError, ValueError):
                    return chosen
            min_i = int(var_spec.min_value) if var_spec.min_value is not None else 1
            max_i = int(var_spec.max_value) if var_spec.max_value is not None else max(100, min_i)
            max_i = max(max_i, min_i)
            return self._rng.randint(min_i, max_i)

        if v_norm in _FLOAT_TYPES:
            if var_spec.coded_values:
                chosen_f = self._rng.choice(var_spec.coded_values)
                try:
                    return float(chosen_f)
                except (TypeError, ValueError):
                    return chosen_f
            min_f = float(var_spec.min_value) if var_spec.min_value is not None else 1.0
            max_f = (
                float(var_spec.max_value) if var_spec.max_value is not None else max(100.0, min_f)
            )
            max_f = max(max_f, min_f)
            val = round(self._rng.uniform(min_f, max_f), 2)
            return float(min(max(val, min_f), max_f))

        if v_norm in _DATE_TYPES:
            if spec.global_date_value:
                return spec.global_date_value.isoformat()
            return date.today().isoformat()

        if v_norm in _DATETIME_TYPES:
            if spec.global_date_value:
                return f"{spec.global_date_value.isoformat()}T12:00:00+00:00"
            if self._seeded or spec.seed is not None:
                return f"{date.today().isoformat()}T12:00:00+00:00"
            return datetime.now(UTC).isoformat()

        if v_norm == "time":
            return "12:00:00"

        if v_norm in _MEMO_TYPES:
            if var_spec.coded_values:
                return self._rng.choice(var_spec.coded_values)
            text = self._paragraph(nb_sentences=3)
            max_memo_len = var_spec.max_length if var_spec.max_length is not None else 200
            return text[:max_memo_len]

        if v_norm in _CALC_TYPES:
            min_c = float(var_spec.min_value) if var_spec.min_value is not None else 1.0
            max_c = (
                float(var_spec.max_value) if var_spec.max_value is not None else max(100.0, min_c)
            )
            max_c = max(max_c, min_c)
            min_ci = int(min_c) if min_c.is_integer() else int(min_c) + 1
            max_ci = int(max_c)
            if min_ci <= max_ci:
                return self._rng.randint(min_ci, max_ci)
            val_c = round(self._rng.uniform(min_c, max_c), 2)
            return float(min(max(val_c, min_c), max_c))

        if v_norm in _SKIP_EMPTY_TYPES:
            return "example" if var_spec.required else ""

        logger.warning(
            f"Unrecognized variable type '{v_type}' for variable '{var_spec.variable_name}'. "
            "Defaulting to short text."
        )
        return self._lexify("???????")

    def _generate_data_payloads(
        self, spec: UATSpecification, form_spec: UATFormSpec, subject_pool: list[str]
    ) -> tuple[list[dict[str, Any]], list[str], list[str]]:
        warnings = []
        coded_all_vars = [
            v for v in form_spec.variables if v.strategy == VariableTestStrategy.CODED_ALL
        ]

        if len(coded_all_vars) > 1:
            warnings.append(
                f"Multiple CODED_ALL variables found in form {form_spec.form_key}. "
                "Falling back to one payload per subject."
            )
            coded_all_vars = []

        if coded_all_vars:
            var_spec = coded_all_vars[0]
            payloads = []
            used_subjects = []
            for i, code in enumerate(var_spec.coded_values):
                subj_key = subject_pool[i % len(subject_pool)]
                used_subjects.append(subj_key)
                data = {}
                for v in form_spec.variables:
                    if v.variable_key == var_spec.variable_key:
                        data[v.variable_name] = code
                    else:
                        val = self._generate_value(v, spec)
                        if val is not None:
                            data[v.variable_name] = val
                payloads.append(data)
            return payloads, warnings, used_subjects

        if any(v.strategy == VariableTestStrategy.BOUNDARY for v in form_spec.variables):
            payloads = []
            used_subjects = []
            # Min boundary
            data_min: dict[str, Any] = {}
            for v in form_spec.variables:
                v_norm = _normalize_var_type(v.variable_type)
                if v.strategy == VariableTestStrategy.BOUNDARY and (
                    v_norm in _INT_TYPES or v_norm in _FLOAT_TYPES or v_norm in _CALC_TYPES
                ):
                    data_min[v.variable_name] = self._boundary_scalar(v, is_max=False)
                else:
                    val = self._generate_value(v, spec)
                    if val is not None:
                        data_min[v.variable_name] = val
            payloads.append(data_min)
            used_subjects.append(subject_pool[0])

            # Max boundary
            data_max: dict[str, Any] = {}
            for v in form_spec.variables:
                v_norm = _normalize_var_type(v.variable_type)
                if v.strategy == VariableTestStrategy.BOUNDARY and (
                    v_norm in _INT_TYPES or v_norm in _FLOAT_TYPES or v_norm in _CALC_TYPES
                ):
                    data_max[v.variable_name] = self._boundary_scalar(v, is_max=True)
                else:
                    val = self._generate_value(v, spec)
                    if val is not None:
                        data_max[v.variable_name] = val
            payloads.append(data_max)
            used_subjects.append(subject_pool[1 % len(subject_pool)])
            return payloads, warnings, used_subjects

        payloads = []
        used_subjects = []
        for i in range(form_spec.subject_count):
            subj_key = subject_pool[i % len(subject_pool)]
            used_subjects.append(subj_key)
            data = {}
            for v in form_spec.variables:
                val = self._generate_value(v, spec)
                if val is not None:
                    data[v.variable_name] = val
            payloads.append(data)
        return payloads, warnings, used_subjects

    def _generate_registration_set(
        self,
        spec: UATSpecification,
        form_spec: UATFormSpec,
        snapshot: StudySnapshot,
        subject_pool: list[str],
    ) -> GeneratedRecordSet:
        data_payloads, warnings, subject_keys = self._generate_data_payloads(
            spec, form_spec, subject_pool
        )

        subj_spec = spec.subject_specs[0] if spec.subject_specs else None
        site_name = (
            subj_spec.site_name
            if subj_spec
            else (
                snapshot.active_sites()[0].site_name if snapshot.active_sites() else "Default Site"
            )
        )
        keyword_tag = (subj_spec.keyword_tag if subj_spec is not None else "UAT").strip()

        final_payloads = []
        for data in data_payloads:
            reg_payload: dict[str, Any] = {
                "formKey": form_spec.form_key,
                "siteName": site_name,
                "data": data,
            }
            if keyword_tag:
                reg_payload["keywords"] = [{"keywordName": keyword_tag, "keywordKey": keyword_tag}]
            final_payloads.append(reg_payload)

        return GeneratedRecordSet(
            form_key=form_spec.form_key,
            form_name=form_spec.form_name,
            test_type=form_spec.test_type,
            payloads=final_payloads,
            subject_keys=subject_keys,
            warnings=warnings,
        )

    def _generate_scheduled_set(
        self,
        spec: UATSpecification,
        form_spec: UATFormSpec,
        snapshot: StudySnapshot,
        subject_pool: list[str],
    ) -> GeneratedRecordSet:
        data_payloads, warnings, subject_keys = self._generate_data_payloads(
            spec, form_spec, subject_pool
        )

        final_payloads = []
        for i, data in enumerate(data_payloads):
            final_payloads.append(
                {
                    "formKey": form_spec.form_key,
                    "subjectKey": subject_keys[i],
                    "intervalName": form_spec.interval_name or "Default Interval",
                    "data": data,
                }
            )

        return GeneratedRecordSet(
            form_key=form_spec.form_key,
            form_name=form_spec.form_name,
            test_type=form_spec.test_type,
            payloads=final_payloads,
            subject_keys=subject_keys,
            warnings=warnings,
        )

    def _generate_unscheduled_set(
        self,
        spec: UATSpecification,
        form_spec: UATFormSpec,
        snapshot: StudySnapshot,
        subject_pool: list[str],
    ) -> GeneratedRecordSet:
        data_payloads, warnings, subject_keys = self._generate_data_payloads(
            spec, form_spec, subject_pool
        )

        final_payloads = []
        for i, data in enumerate(data_payloads):
            final_payloads.append(
                {"formKey": form_spec.form_key, "subjectKey": subject_keys[i], "data": data}
            )

        return GeneratedRecordSet(
            form_key=form_spec.form_key,
            form_name=form_spec.form_name,
            test_type=form_spec.test_type,
            payloads=final_payloads,
            subject_keys=subject_keys,
            warnings=warnings,
        )
