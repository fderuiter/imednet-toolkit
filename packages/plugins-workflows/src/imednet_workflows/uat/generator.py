"""Synthetic record payload generator for UAT."""

from __future__ import annotations

import logging
import random
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
        self._rng = random.Random(seed)  # noqa: S311
        self._seed = seed
        self._locale = locale
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
        data_dictionary: DataDictionary | None = None,
    ) -> list[GeneratedRecordSet]:
        """Generate all record payloads for the enabled forms in the spec.

        Returns one GeneratedRecordSet per enabled UATFormSpec, in the correct
        submission order: RegisterSubject records first, then scheduled/unscheduled.
        """
        active_dd = data_dictionary if data_dictionary is not None else self._data_dictionary
        results: list[GeneratedRecordSet] = []
        subject_pool = self._get_subject_pool(spec)

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

    def _get_subject_pool(self, spec: UATSpecification) -> list[str]:
        """Return a mocked value."""
        pool = []
        for s_spec in spec.subject_specs:
            for i in range(s_spec.subject_count):
                pool.append(f"{s_spec.subject_key_prefix}{i + 1:03d}")
        if not pool:
            pool = ["UAT-TEST-001"]
        return pool

    def _generate_value(self, var_spec: UATVariableSpec, spec: UATSpecification) -> str | None:
        """Generate a single value for a variable according to its strategy."""
        if var_spec.strategy == VariableTestStrategy.SKIP:
            return None
        if var_spec.strategy == VariableTestStrategy.FIXED:
            return str(var_spec.fixed_value) if var_spec.fixed_value is not None else ""

        return self._synthesize_value(var_spec, spec)

    def _synthesize_value(self, var_spec: UATVariableSpec, spec: UATSpecification) -> str:
        """Return a mocked value appropriate for the canonical variable type."""
        v_type = var_spec.variable_type
        v_norm = (v_type or "").strip().lower()

        if v_norm in ("coded", "radio", "single select"):
            if var_spec.coded_values:
                return self._rng.choice(var_spec.coded_values)
            return ""

        if v_norm in ("checkbox", "multi select"):
            if var_spec.coded_values:
                count = self._rng.randint(1, min(3, len(var_spec.coded_values)))
                choices = self._rng.sample(var_spec.coded_values, count)
                return "`".join(choices)
            return ""

        if v_norm in ("boolean", "bool", "yes/no", "yesno"):
            if var_spec.coded_values:
                return self._rng.choice(var_spec.coded_values)
            return self._rng.choice(["Yes", "No"])

        if v_norm in ("text", "string"):
            if var_spec.coded_values:
                return self._rng.choice(var_spec.coded_values)
            max_len = var_spec.max_length or spec.global_text_length or 10
            return self._lexify("?" * max_len)

        if v_norm in ("number", "integer", "int"):
            if var_spec.coded_values:
                return self._rng.choice(var_spec.coded_values)
            min_i = int(var_spec.min_value) if var_spec.min_value is not None else 1
            max_i = int(var_spec.max_value) if var_spec.max_value is not None else max(100, min_i)
            max_i = max(max_i, min_i)
            return str(self._rng.randint(min_i, max_i))

        if v_norm in ("float", "decimal", "slider"):
            if var_spec.coded_values:
                return self._rng.choice(var_spec.coded_values)
            min_f = var_spec.min_value if var_spec.min_value is not None else 1.0
            max_f = var_spec.max_value if var_spec.max_value is not None else max(100.0, min_f)
            max_f = max(max_f, min_f)
            val = self._rng.uniform(min_f, max_f)
            return f"{val:.2f}"

        if v_norm in ("date", "date/time precision"):
            if spec.global_date_value:
                return spec.global_date_value.isoformat()
            return date.today().isoformat()

        if v_norm == "datetime":
            return datetime.now(UTC).isoformat()

        if v_norm == "time":
            return "12:00:00"

        if v_norm == "textarea":
            text = self._paragraph(nb_sentences=3)
            if var_spec.max_length is not None:
                return text[: var_spec.max_length]
            return text

        if v_norm == "calculation":
            min_c = var_spec.min_value if var_spec.min_value is not None else 1.0
            max_c = var_spec.max_value if var_spec.max_value is not None else max(100.0, min_c)
            max_c = max(max_c, min_c)
            return str(int(self._rng.uniform(min_c, max_c)))

        if v_norm in ("signature", "file", "descriptive"):
            return ""

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
            data_min = {}
            for v in form_spec.variables:
                if v.strategy == VariableTestStrategy.BOUNDARY and v.variable_type in (
                    "Number",
                    "Integer",
                    "Float",
                    "Decimal",
                ):
                    data_min[v.variable_name] = str(v.min_value) if v.min_value is not None else "1"
                else:
                    val = self._generate_value(v, spec)
                    if val is not None:
                        data_min[v.variable_name] = val
            payloads.append(data_min)
            used_subjects.append(subject_pool[0])

            # Max boundary
            data_max = {}
            for v in form_spec.variables:
                if v.strategy == VariableTestStrategy.BOUNDARY and v.variable_type in (
                    "Number",
                    "Integer",
                    "Float",
                    "Decimal",
                ):
                    data_max[v.variable_name] = (
                        str(v.max_value) if v.max_value is not None else "100"
                    )
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

        final_payloads = []
        for data in data_payloads:
            final_payloads.append(
                {"formKey": form_spec.form_key, "siteName": site_name, "data": data}
            )

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
