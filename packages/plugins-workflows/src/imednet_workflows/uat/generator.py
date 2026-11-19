"""Synthetic record payload generator for UAT."""

from __future__ import annotations

import logging
import random
from datetime import UTC, date, datetime
from typing import Any

from pydantic import Field

from imednet.spi.models import ImednetBaseModel

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
    ) -> None:
        """Initialize the generator.

        Args:
            seed: Optional seed for reproducibility.
            locale: Ignored.
        """
        self._rng = random.Random(seed)  # noqa: S311
        self._seed = seed
        self._locale = locale

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

    def generate(
        self,
        spec: UATSpecification,
        snapshot: StudySnapshot,
        *,
        subject_keys: list[str] | None = None,
    ) -> list[GeneratedRecordSet]:
        """Generate all record payloads for the enabled forms in the spec.

        Returns one GeneratedRecordSet per enabled UATFormSpec, in the correct
        submission order: RegisterSubject records first, then scheduled/unscheduled.
        """
        results: list[GeneratedRecordSet] = []
        subject_pool = list(subject_keys) if subject_keys else self._get_subject_pool(spec)

        # Phase 1: Enrollment/registration forms
        for form_spec in spec.forms_by_type(RecordTestType.REGISTER_SUBJECT):
            results.append(self._generate_registration_set(spec, form_spec, snapshot, subject_pool))

        # Phase 2: Scheduled forms
        for form_spec in spec.forms_by_type(RecordTestType.UPDATE_SCHEDULED_RECORD):
            results.append(self._generate_scheduled_set(spec, form_spec, snapshot, subject_pool))

        # Phase 3: Unscheduled forms
        for form_spec in spec.forms_by_type(RecordTestType.CREATE_NEW_RECORD):
            results.append(self._generate_unscheduled_set(spec, form_spec, snapshot, subject_pool))

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

    def _generate_value(self, var_spec: UATVariableSpec, spec: UATSpecification) -> str | None:
        """Generate a single value for a variable according to its strategy."""
        if var_spec.strategy == VariableTestStrategy.SKIP:
            return None
        if var_spec.strategy == VariableTestStrategy.FIXED:
            return str(var_spec.fixed_value) if var_spec.fixed_value is not None else ""

        return self._synthesize_value(var_spec, spec)

    def _synthesize_value(self, var_spec: UATVariableSpec, spec: UATSpecification) -> str:
        """Return a mocked value."""
        v_type = var_spec.variable_type

        if v_type == "Text":
            max_len = var_spec.max_length or spec.global_text_length or 10
            return self._lexify("?" * max_len)

        if v_type in ("Number", "Integer"):
            min_v = var_spec.min_value if var_spec.min_value is not None else 1
            max_v = var_spec.max_value if var_spec.max_value is not None else 100
            return str(self._rng.randint(int(min_v), int(max_v)))

        if v_type in ("Float", "Decimal"):
            min_v = var_spec.min_value if var_spec.min_value is not None else 1.0
            max_v = var_spec.max_value if var_spec.max_value is not None else 100.0
            val = self._rng.uniform(min_v, max_v)
            return f"{val:.2f}"

        if v_type == "Date":
            if spec.global_date_value:
                return spec.global_date_value.isoformat()
            return date.today().isoformat()

        if v_type == "DateTime":
            return datetime.now(UTC).isoformat()

        if v_type == "Coded":
            if var_spec.coded_values:
                return self._rng.choice(var_spec.coded_values)
            return ""

        if v_type in ("Boolean", "Yes/No"):
            return self._rng.choice(["Yes", "No"])

        if v_type == "Checkbox":
            if var_spec.coded_values:
                count = self._rng.randint(1, min(3, len(var_spec.coded_values)))
                choices = self._rng.sample(var_spec.coded_values, count)
                return "`".join(choices)
            return ""

        if v_type == "TextArea":
            return self._paragraph(nb_sentences=3)

        if v_type in ("Signature", "File"):
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
