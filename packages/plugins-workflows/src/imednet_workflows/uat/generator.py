"""Synthetic record payload generator for UAT."""

from __future__ import annotations

import calendar
import logging
import math
import random
import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from pydantic import Field

from imednet.spi.models import ImednetBaseModel
from imednet.spi.validation import (
    DataDictionary,
    RangeCheckConstraint,
    VariableConstraints,
    extract_variable_constraints,
)

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

    @staticmethod
    def _matches_numeric_bound(raw: str | None, bound: float | None) -> bool:
        if raw is None or bound is None:
            return False
        try:
            return math.isclose(float(raw), bound, rel_tol=1e-9, abs_tol=1e-12)
        except ValueError:
            return False

    @staticmethod
    def _parse_iso_date_bound(raw: str | None, *, is_upper: bool) -> date | None:
        if not raw or not raw.strip():
            return None
        s = raw.strip()
        if len(s) == 4 and s.isdigit():
            yr = int(s)
            return date(yr, 12, 31) if is_upper else date(yr, 1, 1)
        if len(s) == 7 and s[4] == "-" and s[:4].isdigit() and s[5:7].isdigit():
            yr, mo = int(s[:4]), int(s[5:7])
            if 1 <= mo <= 12:
                last_day = calendar.monthrange(yr, mo)[1]
                return date(yr, mo, last_day) if is_upper else date(yr, mo, 1)
            return None
        date_part = s.split("T", 1)[0].split(" ", 1)[0]
        try:
            return date.fromisoformat(date_part)
        except ValueError:
            return None

    @classmethod
    def _parse_iso_datetime_bound(cls, raw: str | None, *, is_upper: bool) -> datetime | None:
        if not raw or not raw.strip():
            return None
        s = raw.strip()
        if "T" in s or " " in s:
            norm = s.replace(" ", "T", 1)
            if norm.endswith("Z"):
                norm = norm[:-1] + "+00:00"
            try:
                parsed = datetime.fromisoformat(norm)
                if parsed.tzinfo is not None:
                    parsed = parsed.astimezone(UTC).replace(tzinfo=None)
                return parsed
            except ValueError:
                return None
        d_bound = cls._parse_iso_date_bound(s, is_upper=is_upper)
        if d_bound is None:
            return None
        return datetime.combine(
            d_bound,
            time(23, 59, 59) if is_upper else time(0, 0, 0),
        )

    @staticmethod
    def _parse_iso_time_seconds(raw: str | None) -> int | None:
        if not raw or not raw.strip():
            return None
        s = raw.strip()
        if "T" in s:
            s = s.split("T", 1)[1]
        for tz_sep in ("Z", "+", "-"):
            if tz_sep in s:
                s = s.split(tz_sep, 1)[0]
        try:
            t_val = time.fromisoformat(s)
        except ValueError:
            return None
        return t_val.hour * 3600 + t_val.minute * 60 + t_val.second

    @staticmethod
    def _format_time_seconds(total_seconds: int) -> str:
        clamped = max(0, min(86399, total_seconds))
        hh, rem = divmod(clamped, 3600)
        mm, ss = divmod(rem, 60)
        return f"{hh:02d}:{mm:02d}:{ss:02d}"

    @classmethod
    def _apply_range_checks_to_updates(
        cls,
        v: UATVariableSpec,
        constraints: VariableConstraints,
        eff_min: float | None,
        eff_max: float | None,
        updates: dict[str, Any],
    ) -> None:
        in_values: list[str] = []
        excluded: list[str] = list(v.excluded_values)
        range_checks: tuple[RangeCheckConstraint, ...] = constraints.range_checks

        for rc in range_checks:
            comp = (rc.comparator or "").upper()
            if comp == "IN" and rc.check_values:
                for val in rc.check_values:
                    if val not in in_values:
                        in_values.append(val)
            elif comp == "EQ" and rc.check_value is not None:
                if rc.check_value not in in_values:
                    in_values.append(rc.check_value)
            elif comp == "NE" and rc.check_value is not None:
                if rc.check_value not in excluded:
                    excluded.append(rc.check_value)
            elif comp == "NOTIN" and rc.check_values:
                for val in rc.check_values:
                    if val not in excluded:
                        excluded.append(val)
            elif (
                comp == "GT"
                and rc.check_value is not None
                and not v.min_exclusive
                and (
                    cls._matches_numeric_bound(rc.check_value, eff_min)
                    or (
                        constraints.min_date is not None
                        and rc.check_value.strip() == constraints.min_date
                    )
                )
            ):
                updates["min_exclusive"] = True
            elif (
                comp == "LT"
                and rc.check_value is not None
                and not v.max_exclusive
                and (
                    cls._matches_numeric_bound(rc.check_value, eff_max)
                    or (
                        constraints.max_date is not None
                        and rc.check_value.strip() == constraints.max_date
                    )
                )
            ):
                updates["max_exclusive"] = True

        if not v.coded_values:
            if constraints.choices:
                filtered = [c for c in constraints.choices if c not in excluded]
                updates["coded_values"] = filtered or list(constraints.choices)
            elif in_values:
                filtered = [c for c in in_values if c not in excluded]
                updates["coded_values"] = filtered or in_values

        if excluded != v.excluded_values:
            updates["excluded_values"] = excluded

    @classmethod
    def enrich_variable_spec(
        cls, v: UATVariableSpec, constraints: VariableConstraints
    ) -> UATVariableSpec:
        """Return an enriched copy of ``v`` populated from ``VariableConstraints``."""
        updates: dict[str, Any] = {}
        eff_min = (
            constraints.min_value
            if constraints.min_value is not None
            else constraints.soft_min_value
        )
        eff_max = (
            constraints.max_value
            if constraints.max_value is not None
            else constraints.soft_max_value
        )
        if v.min_value is None and eff_min is not None:
            updates["min_value"] = eff_min
        if v.max_value is None and eff_max is not None:
            updates["max_value"] = eff_max
        if v.min_date is None and constraints.min_date is not None:
            updates["min_date"] = constraints.min_date
        if v.max_date is None and constraints.max_date is not None:
            updates["max_date"] = constraints.max_date
        cls._apply_range_checks_to_updates(v, constraints, eff_min, eff_max, updates)
        if v.max_length is None and constraints.max_length is not None:
            updates["max_length"] = constraints.max_length
        if not v.required and constraints.required_policy == "yes":
            updates["required"] = True
        if not updates:
            return v
        return v.model_copy(update=updates)

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
            base_v = v
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
                base_v = v.model_copy(update={"variable_type": dd_type})
            enriched_v = self.enrich_variable_spec(base_v, constraints)
            if enriched_v is not v:
                changed = True
            enriched_vars.append(enriched_v)

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

    def _generate_value(self, var_spec: UATVariableSpec, spec: UATSpecification) -> Any:
        """Generate a single value for a variable according to its strategy."""
        if var_spec.strategy == VariableTestStrategy.SKIP:
            return None
        if var_spec.strategy == VariableTestStrategy.FIXED:
            return self._coerce_fixed_value(var_spec)

        return self._synthesize_value(var_spec, spec)

    @classmethod
    def _effective_int_bounds(cls, var_spec: UATVariableSpec) -> tuple[int, int]:
        lo = math.ceil(var_spec.min_value) if var_spec.min_value is not None else 1
        hi = math.floor(var_spec.max_value) if var_spec.max_value is not None else max(100, lo)
        if (
            var_spec.min_exclusive
            and var_spec.min_value is not None
            and math.isclose(var_spec.min_value, lo)
        ):
            lo += 1
        if (
            var_spec.max_exclusive
            and var_spec.max_value is not None
            and math.isclose(var_spec.max_value, hi)
        ):
            hi -= 1
        hi = max(hi, lo)
        excluded_ints: set[int] = set()
        for ex in var_spec.excluded_values:
            try:
                excluded_ints.add(int(float(ex)))
            except ValueError:
                continue
        while lo in excluded_ints and lo < hi:
            lo += 1
        while hi in excluded_ints and hi > lo:
            hi -= 1
        return lo, hi

    @classmethod
    def _effective_float_bounds(cls, var_spec: UATVariableSpec) -> tuple[float, float]:
        lo = float(var_spec.min_value) if var_spec.min_value is not None else 1.0
        hi = float(var_spec.max_value) if var_spec.max_value is not None else max(100.0, lo)
        if var_spec.min_value is not None and var_spec.max_value is not None:
            span = max(0.0, hi - lo)
            delta = 1.0 if span > 2.0 else (round(span * 0.25, 4) if span > 0 else 0.01)
        else:
            delta = 1.0
        if var_spec.min_exclusive and var_spec.min_value is not None:
            lo = round(lo + delta, 4)
        if var_spec.max_exclusive and var_spec.max_value is not None:
            hi = round(hi - delta, 4)
        hi = max(hi, lo)
        return lo, hi

    @classmethod
    def _effective_date_bounds(
        cls, var_spec: UATVariableSpec, spec: UATSpecification
    ) -> tuple[date, date]:
        lo_d = cls._parse_iso_date_bound(var_spec.min_date, is_upper=False)
        hi_d = cls._parse_iso_date_bound(var_spec.max_date, is_upper=True)
        if lo_d is not None and var_spec.min_exclusive:
            lo_d = lo_d + timedelta(days=1)
        if hi_d is not None and var_spec.max_exclusive:
            hi_d = hi_d - timedelta(days=1)
        anchor = spec.global_date_value or date.today()
        if lo_d is None and hi_d is None:
            return anchor, anchor
        if lo_d is None and hi_d is not None:
            lo_d = min(anchor, hi_d - timedelta(days=30))
        if hi_d is None and lo_d is not None:
            hi_d = max(anchor, lo_d + timedelta(days=30))
        assert lo_d is not None
        assert hi_d is not None
        hi_d = max(hi_d, lo_d)
        return lo_d, hi_d

    @classmethod
    def _effective_datetime_bounds(cls, var_spec: UATVariableSpec) -> tuple[datetime, datetime]:
        lo_dt = cls._parse_iso_datetime_bound(var_spec.min_date, is_upper=False)
        hi_dt = cls._parse_iso_datetime_bound(var_spec.max_date, is_upper=True)
        if lo_dt is not None and var_spec.min_exclusive:
            lo_dt = lo_dt + timedelta(seconds=1)
        if hi_dt is not None and var_spec.max_exclusive:
            hi_dt = hi_dt - timedelta(seconds=1)
        anchor = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
        if lo_dt is None and hi_dt is None:
            return anchor, anchor
        if lo_dt is None and hi_dt is not None:
            lo_dt = min(anchor, hi_dt - timedelta(days=1))
        if hi_dt is None and lo_dt is not None:
            hi_dt = max(anchor, lo_dt + timedelta(days=1))
        assert lo_dt is not None
        assert hi_dt is not None
        hi_dt = max(hi_dt, lo_dt)
        return lo_dt, hi_dt

    @classmethod
    def _effective_time_bounds(cls, var_spec: UATVariableSpec) -> tuple[int, int]:
        lo_sec = cls._parse_iso_time_seconds(var_spec.min_date)
        hi_sec = cls._parse_iso_time_seconds(var_spec.max_date)
        if lo_sec is not None and var_spec.min_exclusive:
            lo_sec += 1
        if hi_sec is not None and var_spec.max_exclusive:
            hi_sec -= 1
        if lo_sec is None and hi_sec is None:
            return 43200, 43200
        if lo_sec is None and hi_sec is not None:
            lo_sec = max(0, hi_sec - 3600)
        if hi_sec is None and lo_sec is not None:
            hi_sec = min(86399, lo_sec + 3600)
        assert lo_sec is not None
        assert hi_sec is not None
        lo_sec = max(0, min(86399, lo_sec))
        hi_sec = max(lo_sec, min(86399, hi_sec))
        return lo_sec, hi_sec

    def _boundary_value(
        self, var_spec: UATVariableSpec, spec: UATSpecification, *, is_upper: bool
    ) -> Any:
        """Compute lower or upper BOUNDARY value for numeric or temporal variables."""
        v_norm = _normalize_var_type(var_spec.variable_type)
        if v_norm in _INT_TYPES or v_norm in _CALC_TYPES:
            if var_spec.coded_values:
                chosen = var_spec.coded_values[-1 if is_upper else 0]
                try:
                    return int(float(chosen))
                except (TypeError, ValueError):
                    return chosen
            lo_i, hi_i = self._effective_int_bounds(var_spec)
            return hi_i if is_upper else lo_i

        if v_norm in _FLOAT_TYPES:
            if var_spec.coded_values:
                chosen_f = var_spec.coded_values[-1 if is_upper else 0]
                try:
                    return float(chosen_f)
                except (TypeError, ValueError):
                    return chosen_f
            lo_f, hi_f = self._effective_float_bounds(var_spec)
            if not is_upper:
                return (
                    float(lo_f)
                    if (var_spec.min_exclusive or var_spec.min_value is None)
                    else float(var_spec.min_value)
                )
            return (
                float(hi_f)
                if (var_spec.max_exclusive or var_spec.max_value is None)
                else float(var_spec.max_value)
            )

        if v_norm in _DATE_TYPES:
            lo_d, hi_d = self._effective_date_bounds(var_spec, spec)
            return (hi_d if is_upper else lo_d).isoformat()

        if v_norm in _DATETIME_TYPES:
            lo_dt, hi_dt = self._effective_datetime_bounds(var_spec)
            return (hi_dt if is_upper else lo_dt).strftime("%Y-%m-%dT%H:%M:%S")

        if v_norm == "time":
            lo_sec, hi_sec = self._effective_time_bounds(var_spec)
            return self._format_time_seconds(hi_sec if is_upper else lo_sec)

        return None

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
            candidate = self._lexify("?" * max_len)
            if var_spec.excluded_values and candidate in var_spec.excluded_values:
                for _ in range(5):
                    candidate = self._lexify("?" * max_len)
                    if candidate not in var_spec.excluded_values:
                        break
            return candidate

        if v_norm in _INT_TYPES:
            if var_spec.coded_values:
                chosen = self._rng.choice(var_spec.coded_values)
                try:
                    return int(float(chosen))
                except (TypeError, ValueError):
                    return chosen
            min_i, max_i = self._effective_int_bounds(var_spec)
            chosen_i = self._rng.randint(min_i, max_i)
            if var_spec.excluded_values and str(chosen_i) in var_spec.excluded_values:
                for cand in range(min_i, max_i + 1):
                    if str(cand) not in var_spec.excluded_values:
                        chosen_i = cand
                        break
            return chosen_i

        if v_norm in _FLOAT_TYPES:
            if var_spec.coded_values:
                chosen_f = self._rng.choice(var_spec.coded_values)
                try:
                    return float(chosen_f)
                except (TypeError, ValueError):
                    return chosen_f
            min_f, max_f = self._effective_float_bounds(var_spec)
            val = round(self._rng.uniform(min_f, max_f), 2)
            clamped = float(min(max(val, min_f), max_f))
            if var_spec.excluded_values and (
                str(clamped) in var_spec.excluded_values
                or f"{clamped:.2f}" in var_spec.excluded_values
            ):
                clamped = float(round((min_f + max_f) / 2.0, 2))
            return clamped

        if v_norm in _DATE_TYPES:
            if var_spec.min_date is not None or var_spec.max_date is not None:
                lo_d, hi_d = self._effective_date_bounds(var_spec, spec)
                if spec.global_date_value and lo_d <= spec.global_date_value <= hi_d:
                    return spec.global_date_value.isoformat()
                span_days = max(0, (hi_d - lo_d).days)
                offset = self._rng.randint(0, span_days)
                return (lo_d + timedelta(days=offset)).isoformat()
            if spec.global_date_value:
                return spec.global_date_value.isoformat()
            return date.today().isoformat()

        if v_norm in _DATETIME_TYPES:
            if var_spec.min_date is not None or var_spec.max_date is not None:
                lo_dt, hi_dt = self._effective_datetime_bounds(var_spec)
                span_sec = max(0, int((hi_dt - lo_dt).total_seconds()))
                offset_sec = self._rng.randint(0, span_sec)
                return (lo_dt + timedelta(seconds=offset_sec)).strftime("%Y-%m-%dT%H:%M:%S")
            return datetime.now(UTC).isoformat()

        if v_norm == "time":
            if var_spec.min_date is not None or var_spec.max_date is not None:
                lo_sec, hi_sec = self._effective_time_bounds(var_spec)
                return self._format_time_seconds(self._rng.randint(lo_sec, hi_sec))
            return "12:00:00"

        if v_norm in _MEMO_TYPES:
            if var_spec.coded_values:
                return self._rng.choice(var_spec.coded_values)
            text = self._paragraph(nb_sentences=3)
            max_memo_len = var_spec.max_length if var_spec.max_length is not None else 200
            return text[:max_memo_len]

        if v_norm in _CALC_TYPES:
            min_c, max_c = self._effective_float_bounds(var_spec)
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
                b_min = (
                    self._boundary_value(v, spec, is_upper=False)
                    if v.strategy == VariableTestStrategy.BOUNDARY
                    else None
                )
                if b_min is not None:
                    data_min[v.variable_name] = b_min
                else:
                    val = self._generate_value(v, spec)
                    if val is not None:
                        data_min[v.variable_name] = val
            payloads.append(data_min)
            used_subjects.append(subject_pool[0])

            # Max boundary
            data_max: dict[str, Any] = {}
            for v in form_spec.variables:
                b_max = (
                    self._boundary_value(v, spec, is_upper=True)
                    if v.strategy == VariableTestStrategy.BOUNDARY
                    else None
                )
                if b_max is not None:
                    data_max[v.variable_name] = b_max
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
