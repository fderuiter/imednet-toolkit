"""Core CDISC SDTM domain transformation, variable naming, and PHI-safe row mapping."""

from __future__ import annotations

import contextlib
import functools
import hashlib
import hmac
import math
import re
from collections.abc import Iterable, Iterator, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel

from imednet.utils.security import (
    SensitivityRegistry,
    global_sensitivity_registry,
    mask_clinical_phi,
)

try:
    import pandas as pd
except ImportError:
    pd = None  # type: ignore[assignment, unused-ignore]

_MASKED_SENTINEL = "***MASKED***"
_SDTM_RECORDS_SORT = "subjectKey,asc;recordId,asc"
_ISO_YEAR_RE = re.compile(r"^\d{4}$")
_ISO_YEAR_MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
_PARTIAL_UNKNOWN_YEAR_RE = re.compile(r"^(\d{4})-(?:UN|UNK|-)", re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r"[^a-zA-Z0-9]+")
_UNSET_PIPELINE: Any = object()
SDTM_SEQUENCER_METADATA_KEY = "sdtm_sequencer"
_DEFAULT_SDTM_MAX_BUFFERED_RECORDS = 5_000

_EXCLUDED_SDTM_STATUSES: frozenset[str] = frozenset(
    {
        "DELETED",
        "RECORD INCOMPLETE",
        "INCOMPLETE",
        "PARTIAL",
    }
)


def _is_deleted_record(rec: Any) -> bool:
    """Return True if an EDC record or mapping is voided, deleted, or an incomplete draft."""
    if not isinstance(rec, Mapping) and getattr(rec, "is_deleted", None) is True:
        return True

    val = rec.get("deleted") if isinstance(rec, Mapping) else getattr(rec, "deleted", None)
    if isinstance(val, bool) and val:
        return True
    if isinstance(val, (int, float)) and not isinstance(val, bool) and bool(val):
        return True
    if isinstance(val, str) and val.strip().lower() in {"true", "1", "yes", "y"}:
        return True

    if isinstance(rec, Mapping):
        raw_status = (
            rec.get("recordStatus")
            if rec.get("recordStatus") is not None
            else rec.get("record_status")
        )
    else:
        raw_status = getattr(rec, "record_status", None)
        if raw_status is None:
            raw_status = getattr(rec, "recordStatus", None)

    if raw_status is not None and hasattr(raw_status, "value"):
        raw_status = raw_status.value
    if isinstance(raw_status, str):
        norm_status = " ".join(
            raw_status.strip().upper().replace("_", " ").replace("-", " ").split()
        )
        if norm_status in _EXCLUDED_SDTM_STATUSES:
            return True
    return False


def is_active_clinical_record(rec: Any) -> bool:
    """Return True when *rec* is an active clinical record eligible for SDTM/readiness scoring."""
    return not _is_deleted_record(rec)


def _canonical_sdtm_record_id(raw_id: Any) -> str | None:
    """Normalize a non-empty record identifier to string, or None when absent."""
    if raw_id is None or isinstance(raw_id, bool):
        return None
    s = str(raw_id).strip()
    return s if s else None


def _sdtm_digest_key(prefix: str, parts: Sequence[str], *, secret: bytes | None = None) -> str:
    """Compute a deterministic SHA-256 (or HMAC-SHA256) hex digest so PHI never persists."""
    payload = f"{prefix}|" + "|".join(f"{len(part)}:{part}" for part in parts)
    encoded = payload.encode("utf-8")
    if secret is not None:
        return hmac.new(secret, encoded, hashlib.sha256).hexdigest()
    return hashlib.sha256(encoded).hexdigest()


class TransactionalSdtmSequencer:
    """Transactional SDTM sequence tracker scoped to ``(STUDYID, DOMAIN, USUBJID)``.

    Tracks ``(STUDYID, DOMAIN, USUBJID)`` high-water marks and ``(STUDYID, DOMAIN, record_id)``
    bindings using SHA-256 (or HMAC-SHA256) digests so PHI never persists in ledger metadata.
    Supports nested transactional staging via :meth:`begin`, :meth:`commit`, :meth:`rollback`,
    and :meth:`stage`.
    """

    def __init__(
        self,
        *,
        counters: Mapping[str, int] | None = None,
        record_bindings: Mapping[str, int] | None = None,
        secret: bytes | str | None = None,
    ) -> None:
        """Initialize the transactional SDTM sequencer with optional hydrated state."""
        if isinstance(secret, str):
            self._secret: bytes | None = secret.encode("utf-8")
        else:
            self._secret = secret
        self._counters: dict[str, int] = {
            str(k): int(v)
            for k, v in (counters or {}).items()
            if isinstance(v, int) and not isinstance(v, bool) and v > 0
        }
        self._bindings: dict[str, int] = {
            str(k): int(v)
            for k, v in (record_bindings or {}).items()
            if isinstance(v, int) and not isinstance(v, bool) and v > 0
        }
        self._stage_stack: list[tuple[dict[str, int], dict[str, int]]] = []

    @property
    def has_staged_changes(self) -> bool:
        """Return True when one or more uncommitted staging scopes are active."""
        return bool(self._stage_stack)

    @property
    def committed_counters(self) -> dict[str, int]:
        """Return a snapshot of committed ``(STUDYID, DOMAIN, USUBJID)`` digest counters."""
        base = self._stage_stack[0][0] if self._stage_stack else self._counters
        return dict(base)

    @property
    def committed_bindings(self) -> dict[str, int]:
        """Return a snapshot of committed ``(STUDYID, DOMAIN, record_id)`` digest bindings."""
        base = self._stage_stack[0][1] if self._stage_stack else self._bindings
        return dict(base)

    def counter_digest(self, study_id: str, domain: str, usubjid: str) -> str:
        """Return the SHA-256/HMAC digest for ``(STUDYID, DOMAIN, USUBJID)``."""
        return _sdtm_digest_key(
            "seq",
            (
                str(study_id or "").strip(),
                str(domain or "").strip().upper(),
                str(usubjid or "").strip(),
            ),
            secret=self._secret,
        )

    def record_digest(self, study_id: str, domain: str, record_id: Any) -> str | None:
        """Return the SHA-256/HMAC digest for ``(STUDYID, DOMAIN, record_id)``, or None."""
        rec_clean = _canonical_sdtm_record_id(record_id)
        if rec_clean is None:
            return None
        return _sdtm_digest_key(
            "rec",
            (
                str(study_id or "").strip(),
                str(domain or "").strip().upper(),
                rec_clean,
            ),
            secret=self._secret,
        )

    def assign_seq(
        self,
        study_id: str,
        domain: str,
        usubjid: str,
        record_id: Any = None,
    ) -> int:
        """Return prior bound ``<DOM>SEQ`` for *record_id* or allocate the next sequence."""
        cnt_key = self.counter_digest(study_id, domain, usubjid)
        rec_key = self.record_digest(study_id, domain, record_id)
        if rec_key is not None and rec_key in self._bindings:
            bound_seq = self._bindings[rec_key]
            if bound_seq > self._counters.get(cnt_key, 0):
                self._counters[cnt_key] = bound_seq
            return bound_seq

        next_seq = self._counters.get(cnt_key, 0) + 1
        self._counters[cnt_key] = next_seq
        if rec_key is not None:
            self._bindings[rec_key] = next_seq
        return next_seq

    def begin(self) -> None:
        """Open a nested transactional staging scope."""
        self._stage_stack.append((dict(self._counters), dict(self._bindings)))

    def commit(self) -> None:
        """Commit the innermost staging scope."""
        if self._stage_stack:
            self._stage_stack.pop()

    def rollback(self) -> None:
        """Roll back uncommitted sequence increments in the innermost staging scope."""
        if self._stage_stack:
            saved_counters, saved_bindings = self._stage_stack.pop()
            self._counters = saved_counters
            self._bindings = saved_bindings

    @contextlib.contextmanager
    def stage(self) -> Iterator[TransactionalSdtmSequencer]:
        """Context manager that commits sequence deltas on success and rolls back on failure."""
        self.begin()
        try:
            yield self
        except BaseException:
            self.rollback()
            raise
        else:
            self.commit()

    def to_metadata(self) -> dict[str, Any]:
        """Serialize committed digest watermarks and bindings for PHI-safe ledger persistence."""
        return {
            "version": 1,
            "algorithm": "hmac-sha256" if self._secret is not None else "sha256",
            "counters": self.committed_counters,
            "record_bindings": self.committed_bindings,
        }

    def write_to_metadata(self, metadata: MutableMapping[str, Any]) -> None:
        """Persist committed digest state into ``metadata[SDTM_SEQUENCER_METADATA_KEY]``."""
        metadata[SDTM_SEQUENCER_METADATA_KEY] = self.to_metadata()

    def hydrate_from_metadata(self, metadata: Mapping[str, Any] | None) -> None:
        """Merge persisted digest watermarks and bindings into this sequencer."""
        if not isinstance(metadata, Mapping):
            return
        payload = metadata.get(SDTM_SEQUENCER_METADATA_KEY, metadata)
        if not isinstance(payload, Mapping):
            return
        raw_counters = payload.get("counters")
        if isinstance(raw_counters, Mapping):
            for k, v in raw_counters.items():
                if isinstance(v, int) and not isinstance(v, bool) and v > 0:
                    key_str = str(k)
                    if v > self._counters.get(key_str, 0):
                        self._counters[key_str] = v
        raw_bindings = payload.get("record_bindings")
        if isinstance(raw_bindings, Mapping):
            for k, v in raw_bindings.items():
                if isinstance(v, int) and not isinstance(v, bool) and v > 0:
                    self._bindings.setdefault(str(k), v)

    @classmethod
    def from_metadata(
        cls,
        metadata: Mapping[str, Any] | None,
        *,
        secret: bytes | str | None = None,
    ) -> TransactionalSdtmSequencer:
        """Hydrate a new :class:`TransactionalSdtmSequencer` from ledger metadata."""
        seq = cls(secret=secret)
        seq.hydrate_from_metadata(metadata)
        return seq


def _assign_sdtm_sequence(
    seq_counters: MutableMapping[str, int] | TransactionalSdtmSequencer,
    *,
    study_id: str,
    domain: str,
    usubjid: str,
    record_id: Any = None,
) -> int:
    """Assign or reuse ``<DOM>SEQ`` using either ``TransactionalSdtmSequencer`` or a dict."""
    if isinstance(seq_counters, TransactionalSdtmSequencer):
        return seq_counters.assign_seq(study_id, domain, usubjid, record_id)

    cnt_key = _sdtm_digest_key(
        "seq",
        (
            str(study_id or "").strip(),
            str(domain or "").strip().upper(),
            str(usubjid or "").strip(),
        ),
    )
    rec_clean = _canonical_sdtm_record_id(record_id)
    rec_key = (
        _sdtm_digest_key(
            "rec",
            (
                str(study_id or "").strip(),
                str(domain or "").strip().upper(),
                rec_clean,
            ),
        )
        if rec_clean is not None
        else None
    )
    if rec_key is not None and rec_key in seq_counters:
        bound_seq = int(seq_counters[rec_key])
        if bound_seq > int(seq_counters.get(cnt_key, 0)):
            seq_counters[cnt_key] = bound_seq
        return bound_seq

    next_seq = int(seq_counters.get(cnt_key, 0)) + 1
    seq_counters[cnt_key] = next_seq
    if rec_key is not None:
        seq_counters[rec_key] = next_seq
    return next_seq


def _parse_dtc_string(val_str: str) -> tuple[str, int | None]:
    """Parse a non-empty date/datetime string into (formatted_dtc, year_or_none)."""
    # Check standard date-only pattern YYYY-MM-DD
    if len(val_str) == 10 and val_str[4] == "-" and val_str[7] == "-":
        try:
            d = date.fromisoformat(val_str)
            return d.isoformat(), d.year
        except ValueError:
            pass

    # Try fast ISO datetime parsing (handles YYYY-MM-DDTHH:MM:SS, space-separated, fractional seconds, tz)
    try:
        dt = datetime.fromisoformat(val_str)
        return dt.isoformat(), dt.year
    except ValueError:
        pass

    # Dispatch remaining string formats by delimiter and length
    if "/" in val_str:
        if ":" in val_str and len(val_str) >= 19:
            fmt = None
            if val_str[4] == "/" and val_str[7] == "/":
                fmt = "%Y/%m/%d %H:%M:%S.%f" if "." in val_str else "%Y/%m/%d %H:%M:%S"
            elif val_str[2] == "/" and val_str[5] == "/":
                fmt = "%m/%d/%Y %H:%M:%S.%f" if "." in val_str else "%m/%d/%Y %H:%M:%S"
            if fmt:
                try:
                    parsed_dt = datetime.strptime(val_str, fmt)
                    return parsed_dt.isoformat(), parsed_dt.year
                except ValueError:
                    pass
        elif len(val_str) == 10:
            fmt = None
            if val_str[2] == "/" and val_str[5] == "/":
                fmt = "%m/%d/%Y"
            elif val_str[4] == "/" and val_str[7] == "/":
                fmt = "%Y/%m/%d"
            if fmt:
                try:
                    parsed_dt = datetime.strptime(val_str, fmt)
                    return parsed_dt.strftime("%Y-%m-%d"), parsed_dt.year
                except ValueError:
                    pass
    elif "-" in val_str:
        if len(val_str) == 10 and val_str[2] == "-" and val_str[5] == "-":
            try:
                parsed_dt = datetime.strptime(val_str, "%m-%d-%Y")
                return parsed_dt.strftime("%Y-%m-%d"), parsed_dt.year
            except ValueError:
                pass
        elif len(val_str) == 11 and val_str[2] == "-" and val_str[6] == "-":
            try:
                parsed_dt = datetime.strptime(val_str, "%d-%b-%Y")
                return parsed_dt.strftime("%Y-%m-%d"), parsed_dt.year
            except ValueError:
                pass
        elif val_str.count("-") == 2:
            parts = val_str.split("-")
            if len(parts) == 3 and parts[0].isdigit() and parts[2].isdigit():
                for fmt in ("%d-%b-%Y", "%d-%B-%Y"):
                    try:
                        parsed_dt = datetime.strptime(val_str, fmt)
                        return parsed_dt.strftime("%Y-%m-%d"), parsed_dt.year
                    except ValueError:
                        pass

    if _ISO_YEAR_RE.match(val_str):
        return val_str, int(val_str)
    ym_match = _ISO_YEAR_MONTH_RE.match(val_str)
    if ym_match:
        return val_str, int(ym_match.group(1))
    unk_match = _PARTIAL_UNKNOWN_YEAR_RE.match(val_str)
    if unk_match:
        return val_str, int(unk_match.group(1))

    return val_str, None


def format_iso8601_dtc(
    val: Any,
    *,
    mask_phi: bool = False,
    redact_to_year: bool = False,
) -> str | None:
    """Convert a timestamp/date value to standard CDISC ISO 8601 DTC string.

    Args:
        val: Date, datetime, string, or numeric value to convert.
        mask_phi: When True, redact parsed dates to a four-digit ISO 8601 year or None.
        redact_to_year: Alias for ``mask_phi`` to emit only ``YYYY`` or None.

    Returns:
        Formatted ISO 8601 string (e.g. '2026-01-15T09:30:00', '2026-01-15', or '1985'),
        or None when missing or redacted without a valid year.
    """
    should_redact = mask_phi or redact_to_year
    if val is None:
        return None
    if isinstance(val, str):
        val_str = val.strip()
        if not val_str or val_str == _MASKED_SENTINEL:
            return None
        formatted, parsed_year = _parse_dtc_string(val_str)
        if should_redact:
            return f"{parsed_year:04d}" if parsed_year is not None else None
        return formatted
    if isinstance(val, bool):
        return None if should_redact else str(val)
    if isinstance(val, datetime):
        return f"{val.year:04d}" if should_redact else val.isoformat()
    if isinstance(val, date):
        return f"{val.year:04d}" if should_redact else val.isoformat()
    if isinstance(val, int):
        if should_redact:
            return f"{val:04d}" if 1000 <= val <= 9999 else None
        return str(val)
    if isinstance(val, float) and math.isnan(val):
        return None
    if not isinstance(val, (list, tuple, dict, set)) and pd is not None and bool(pd.isna(val)):
        return None
    return None if should_redact else str(val)


def _format_sdtm_flag(val: Any) -> str | None:
    """Format boolean flag values into CDISC standard 'Y' or 'N'."""
    if val is None:
        return None
    if isinstance(val, bool):
        return "Y" if val else "N"
    if isinstance(val, str):
        v = val.strip().upper()
        if v in ("Y", "YES", "TRUE", "1"):
            return "Y"
        if v in ("N", "NO", "FALSE", "0"):
            return "N"
        return val
    if pd is not None and pd.isna(val):
        return None
    return "Y" if bool(val) else "N"


@functools.lru_cache(maxsize=1024)
def _normalize_key(key: str) -> str:
    """Normalize a key name by removing underscores and converting to lowercase."""
    return key.replace("_", "").lower()


@functools.lru_cache(maxsize=1024)
def _collapse_alnum_key(key: str) -> str:
    """Collapse non-alphanumeric delimiters and lowercase a field key."""
    return _NON_ALNUM_RE.sub("", key).lower()


def _is_birth_dtc_key(key: str) -> bool:
    """Return True when *key* represents a CDISC birth-date DTC variable."""
    return _collapse_alnum_key(key).endswith("brthdtc")


SDTM_DOMAIN_SPECS: dict[str, dict[str, Any]] = {
    "AE": {
        "domain": "AE",
        "variables": [
            "STUDYID",
            "DOMAIN",
            "USUBJID",
            "AESEQ",
            "AETERM",
            "AEDECOD",
            "AESEV",
            "AESER",
            "AESTDTC",
            "AEENDTC",
            "AEOUT",
            "AEREL",
            "AEACN",
        ],
        "aliases": {
            "AETERM": ["aeTerm", "ae_term", "AETERM", "aeterm", "ae_name", "term"],
            "AEDECOD": ["aeDecod", "ae_decod", "AEDECOD", "aedecod", "preferred_term", "pt"],
            "AESEV": ["aeSeverity", "ae_severity", "AESEV", "aesev", "severity"],
            "AESER": ["aeSerious", "ae_serious", "AESER", "aeser", "serious"],
            "AESTDTC": [
                "aeStartDate",
                "ae_start_date",
                "AESTDTC",
                "aestdtc",
                "start_date",
                "startDate",
            ],
            "AEENDTC": ["aeEndDate", "ae_end_date", "AEENDTC", "aeendtc", "end_date", "endDate"],
            "AEOUT": ["aeOutcome", "ae_outcome", "AEOUT", "aeout", "outcome"],
            "AEREL": [
                "aeRelationship",
                "ae_relationship",
                "AEREL",
                "aerel",
                "relationship",
                "causality",
            ],
            "AEACN": ["aeActionTaken", "ae_action_taken", "AEACN", "aeacn", "action_taken"],
        },
        "flags": {"AESER"},
        "dtc": {"AESTDTC", "AEENDTC"},
        "primary_dtc": "AESTDTC",
        "topic_vars": ("AETERM",),
    },
    "DV": {
        "domain": "DV",
        "variables": [
            "STUDYID",
            "DOMAIN",
            "USUBJID",
            "DVSEQ",
            "DVTERM",
            "DVCAT",
            "DVSEV",
            "DVSTDTC",
            "DVSTAT",
        ],
        "aliases": {
            "DVTERM": [
                "dvTerm",
                "dv_term",
                "DVTERM",
                "dvterm",
                "deviation_term",
                "description",
                "term",
            ],
            "DVCAT": ["dvCategory", "dv_category", "DVCAT", "dvcat", "category"],
            "DVSEV": ["dvSeverity", "dv_severity", "DVSEV", "dvsev", "severity"],
            "DVSTDTC": ["dvDate", "dv_date", "DVSTDTC", "dvstdtc", "deviation_date", "date"],
            "DVSTAT": ["dvStatus", "dv_status", "DVSTAT", "dvstat", "status"],
        },
        "flags": set(),
        "dtc": {"DVSTDTC"},
        "primary_dtc": "DVSTDTC",
        "topic_vars": ("DVTERM",),
    },
    "DD": {
        "domain": "DD",
        "variables": [
            "STUDYID",
            "DOMAIN",
            "USUBJID",
            "DDSEQ",
            "DDTERM",
            "DDCAT",
            "DDSER",
            "DDSTDTC",
        ],
        "aliases": {
            "DDTERM": ["ddTerm", "dd_term", "DDTERM", "ddterm", "deficiency_term", "term"],
            "DDCAT": ["ddCategory", "dd_category", "DDCAT", "ddcat", "category"],
            "DDSER": ["ddSerious", "dd_serious", "DDSER", "ddser", "DDSPID", "ddspid", "serious"],
            "DDSTDTC": ["ddDate", "dd_date", "DDSTDTC", "ddstdtc", "deficiency_date", "date"],
        },
        "flags": {"DDSER"},
        "dtc": {"DDSTDTC"},
        "primary_dtc": "DDSTDTC",
        "topic_vars": ("DDTERM",),
    },
    "CM": {
        "domain": "CM",
        "variables": [
            "STUDYID",
            "DOMAIN",
            "USUBJID",
            "CMSEQ",
            "CMTRT",
            "CMDECOD",
            "CMDOSE",
            "CMDOSU",
            "CMROUTE",
            "CMSTDTC",
            "CMENDTC",
            "CMINDC",
        ],
        "aliases": {
            "CMTRT": [
                "cmTerm",
                "cm_term",
                "CMTRT",
                "cmtrt",
                "medication",
                "medicationName",
                "drug_name",
                "cm_trt",
            ],
            "CMDECOD": ["cmDecod", "cm_decod", "CMDECOD", "cmdecod"],
            "CMDOSE": ["cmDose", "cm_dose", "CMDOSE", "cmdose", "dose"],
            "CMDOSU": ["cmDoseUnit", "cm_dose_unit", "CMDOSU", "cmdosu", "dose_unit", "unit"],
            "CMROUTE": ["cmRoute", "cm_route", "CMROUTE", "cmroute", "route"],
            "CMSTDTC": ["cmStartDate", "cm_start_date", "CMSTDTC", "cmstdtc", "start_date"],
            "CMENDTC": ["cmEndDate", "cm_end_date", "CMENDTC", "cmendtc", "end_date"],
            "CMINDC": ["cmIndication", "cm_indication", "CMINDC", "cmindc", "indication"],
        },
        "flags": set(),
        "dtc": {"CMSTDTC", "CMENDTC"},
        "primary_dtc": "CMSTDTC",
        "topic_vars": ("CMTRT",),
    },
    "DM": {
        "domain": "DM",
        "variables": [
            "STUDYID",
            "DOMAIN",
            "USUBJID",
            "SUBJID",
            "BRTHDTC",
            "AGE",
            "AGEU",
            "SEX",
            "RACE",
            "ETHNIC",
            "ARM",
        ],
        "aliases": {
            "SUBJID": ["subjid", "SUBJID", "subject_id", "subjectId", "subjectKey", "subject_key"],
            "BRTHDTC": [
                "brthdat",
                "birthDate",
                "birth_date",
                "BRTHDTC",
                "brthdtc",
                "date_of_birth",
                "dob",
                "DOB",
            ],
            "AGE": ["age", "AGE"],
            "AGEU": ["ageu", "age_unit", "AGEU"],
            "SEX": ["sex", "SEX", "gender"],
            "RACE": ["race", "RACE"],
            "ETHNIC": ["ethnic", "ethnicity", "ETHNIC"],
            "ARM": ["arm", "ARM", "treatment_arm", "armcd"],
        },
        "flags": set(),
        "dtc": {"BRTHDTC"},
        "primary_dtc": "BRTHDTC",
        "topic_vars": ("SUBJID",),
    },
    "VS": {
        "domain": "VS",
        "variables": [
            "STUDYID",
            "DOMAIN",
            "USUBJID",
            "VSSEQ",
            "VSTESTCD",
            "VSTEST",
            "VSORRES",
            "VSORRESU",
            "VSDTC",
        ],
        "aliases": {
            "VSTESTCD": ["vsTestCd", "vs_test_cd", "VSTESTCD", "vstestcd", "test_cd", "testcd"],
            "VSTEST": ["vsTest", "vs_test", "VSTEST", "vstest", "test_name", "test"],
            "VSORRES": ["vsResult", "vs_result", "VSORRES", "vsorres", "result"],
            "VSORRESU": ["vsUnit", "vs_unit", "VSORRESU", "vsorresu", "unit"],
            "VSDTC": ["vsDate", "vs_date", "VSDTC", "vsdtc", "date"],
        },
        "flags": set(),
        "dtc": {"VSDTC"},
        "primary_dtc": "VSDTC",
        "topic_vars": ("VSTESTCD", "VSTEST"),
    },
    "LB": {
        "domain": "LB",
        "variables": [
            "STUDYID",
            "DOMAIN",
            "USUBJID",
            "LBSEQ",
            "LBTESTCD",
            "LBTEST",
            "LBCAT",
            "LBORRES",
            "LBORRESU",
            "LBSTRESC",
            "LBSTRESN",
            "LBSTRESU",
            "LBNRIND",
            "LBDTC",
        ],
        "aliases": {
            "LBTESTCD": ["lbTestCd", "lb_test_cd", "LBTESTCD", "lbtestcd", "test_cd", "testcd"],
            "LBTEST": ["lbTest", "lb_test", "LBTEST", "lbtest", "test_name", "test"],
            "LBCAT": ["lbCategory", "lb_category", "LBCAT", "lbcat", "category"],
            "LBORRES": ["lbResult", "lb_result", "LBORRES", "lborres", "result"],
            "LBORRESU": ["lbUnit", "lb_unit", "LBORRESU", "lborresu", "unit"],
            "LBSTRESC": ["lbStdResultChar", "lb_std_result_char", "LBSTRESC", "lbstresc"],
            "LBSTRESN": ["lbStdResultNum", "lb_std_result_num", "LBSTRESN", "lbstresn"],
            "LBSTRESU": ["lbStdUnit", "lb_std_unit", "LBSTRESU", "lbstresu"],
            "LBNRIND": ["lbNormalRangeInd", "lb_normal_range_ind", "LBNRIND", "lbnrind"],
            "LBDTC": ["lbDate", "lb_date", "LBDTC", "lbdtc", "collection_date", "date"],
        },
        "flags": set(),
        "dtc": {"LBDTC"},
        "primary_dtc": "LBDTC",
        "topic_vars": ("LBTESTCD", "LBTEST"),
    },
    "EX": {
        "domain": "EX",
        "variables": [
            "STUDYID",
            "DOMAIN",
            "USUBJID",
            "EXSEQ",
            "EXTRT",
            "EXDOSE",
            "EXDOSU",
            "EXDOSFRM",
            "EXDOSFRQ",
            "EXROUTE",
            "EXSTDTC",
            "EXENDTC",
        ],
        "aliases": {
            "EXTRT": ["exTrt", "ex_trt", "EXTRT", "extrt", "study_drug", "treatment"],
            "EXDOSE": ["exDose", "ex_dose", "EXDOSE", "exdose", "dose"],
            "EXDOSU": ["exDoseUnit", "ex_dose_unit", "EXDOSU", "exdosu", "dose_unit", "unit"],
            "EXDOSFRM": ["exDoseForm", "ex_dose_form", "EXDOSFRM", "exdosfrm", "dose_form"],
            "EXDOSFRQ": ["exDoseFreq", "ex_dose_freq", "EXDOSFRQ", "exdosfrq", "frequency"],
            "EXROUTE": ["exRoute", "ex_route", "EXROUTE", "exroute", "route"],
            "EXSTDTC": ["exStartDate", "ex_start_date", "EXSTDTC", "exstdtc", "start_date"],
            "EXENDTC": ["exEndDate", "ex_end_date", "EXENDTC", "exendtc", "end_date"],
        },
        "flags": set(),
        "dtc": {"EXSTDTC", "EXENDTC"},
        "primary_dtc": "EXSTDTC",
        "topic_vars": ("EXTRT",),
    },
    "MH": {
        "domain": "MH",
        "variables": [
            "STUDYID",
            "DOMAIN",
            "USUBJID",
            "MHSEQ",
            "MHTERM",
            "MHDECOD",
            "MHCAT",
            "MHPRESP",
            "MHOCCUR",
            "MHSTDTC",
            "MHENDTC",
            "MHENRF",
        ],
        "aliases": {
            "MHTERM": ["mhTerm", "mh_term", "MHTERM", "mhterm", "condition", "term"],
            "MHDECOD": ["mhDecod", "mh_decod", "MHDECOD", "mhdecod"],
            "MHCAT": ["mhCategory", "mh_category", "MHCAT", "mhcat", "category"],
            "MHPRESP": ["mhPresp", "mh_presp", "MHPRESP", "mhpresp"],
            "MHOCCUR": ["mhOccur", "mh_occur", "MHOCCUR", "mhoccur"],
            "MHSTDTC": ["mhStartDate", "mh_start_date", "MHSTDTC", "mhstdtc", "start_date"],
            "MHENDTC": ["mhEndDate", "mh_end_date", "MHENDTC", "mhendtc", "end_date"],
            "MHENRF": ["mhEnrf", "mh_enrf", "MHENRF", "mhenrf", "ongoing"],
        },
        "flags": {"MHPRESP", "MHOCCUR"},
        "dtc": {"MHSTDTC", "MHENDTC"},
        "primary_dtc": "MHSTDTC",
        "topic_vars": ("MHTERM",),
    },
}

_DOMAIN_NAME_ALIASES: dict[str, str] = {
    "AE": "AE",
    "ADVERSEEVENTS": "AE",
    "ADVERSEEVENT": "AE",
    "PD": "DV",
    "DV": "DV",
    "PROTOCOLDEVIATIONS": "DV",
    "PROTOCOLDEVIATION": "DV",
    "DD": "DD",
    "DEVICEDEFICIENCIES": "DD",
    "DEVICEDEFICIENCY": "DD",
    "CM": "CM",
    "CONCOMITANTMEDICATIONS": "CM",
    "CONMEDS": "CM",
    "DM": "DM",
    "DEMOGRAPHICS": "DM",
    "DEMO": "DM",
    "VS": "VS",
    "VITALSIGNS": "VS",
    "VITALS": "VS",
    "LB": "LB",
    "LAB": "LB",
    "LABS": "LB",
    "LABORATORY": "LB",
    "EX": "EX",
    "EXPOSURE": "EX",
    "MH": "MH",
    "MEDICALHISTORY": "MH",
    "MEDHIST": "MH",
}

_GENERIC_ALIASES_EXCLUDED_FROM_GLOBAL_SDTM: frozenset[str] = frozenset(
    {
        "term",
        "severity",
        "serious",
        "start_date",
        "startdate",
        "end_date",
        "enddate",
        "outcome",
        "relationship",
        "causality",
        "action_taken",
        "category",
        "date",
        "status",
        "description",
        "unit",
        "dose",
        "dose_unit",
        "doseunit",
        "dose_form",
        "doseform",
        "frequency",
        "route",
        "indication",
        "test",
        "test_name",
        "testname",
        "test_cd",
        "testcd",
        "result",
        "collection_date",
        "collectiondate",
        "treatment",
        "study_drug",
        "studydrug",
        "condition",
        "ongoing",
    }
)

_METADATA_SDTM_KEYS: dict[str, str] = {
    "study_key": "STUDYID",
    "study_id": "STUDYID",
    "studyid": "STUDYID",
    "subject_key": "USUBJID",
    "usubjid": "USUBJID",
    "site_key": "SITEID",
    "site_id": "SITEID",
    "siteid": "SITEID",
    "visit_id": "VISITNUM",
    "visitnum": "VISITNUM",
    "visit_key": "VISIT",
    "domain": "DOMAIN",
}

_GLOBAL_UNAMBIGUOUS_SDTM_ALIASES: dict[str, str] = {}
for _dom_code, _dom_spec in SDTM_DOMAIN_SPECS.items():
    for _sdtm_var, _aliases in _dom_spec["aliases"].items():
        if _dom_code == "DM" and _sdtm_var == "SUBJID":
            continue
        for _alias in _aliases:
            _norm_alias = _normalize_key(_alias)
            if (
                _norm_alias
                and _norm_alias not in _GENERIC_ALIASES_EXCLUDED_FROM_GLOBAL_SDTM
                and _alias.lower() not in _GENERIC_ALIASES_EXCLUDED_FROM_GLOBAL_SDTM
            ):
                _GLOBAL_UNAMBIGUOUS_SDTM_ALIASES.setdefault(_norm_alias, _sdtm_var)

VALID_NAMING_CONVENTIONS: frozenset[str] = frozenset({"snake_case", "camelCase", "sdtm"})

_VAR_FIRST_CAP_RE = re.compile(r"(.)([A-Z][a-z]+)")
_VAR_ALL_CAP_RE = re.compile(r"([a-z0-9])([A-Z])")
_VAR_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]+")


@functools.lru_cache(maxsize=1024)
def _to_snake_identifier(name: str) -> str:
    """Convert camelCase, PascalCase, or separated variable names into snake_case."""
    cleaned = _VAR_NON_ALNUM_RE.sub("_", name).strip("_")
    if not cleaned:
        return ""
    s1 = _VAR_FIRST_CAP_RE.sub(r"\1_\2", cleaned)
    s2 = _VAR_ALL_CAP_RE.sub(r"\1_\2", s1)
    return re.sub(r"_+", "_", s2).lower()


def normalize_naming_convention(convention: str | None) -> str | None:
    """Validate and canonicalize a SinkConfig naming_convention value."""
    if convention is None:
        return None
    if not isinstance(convention, str):
        raise ValueError("naming_convention must be 'snake_case', 'camelCase', 'sdtm', or None")
    cleaned = convention.strip()
    if not cleaned:
        return None
    lower = cleaned.lower()
    if lower in {"snake_case", "snake"}:
        return "snake_case"
    if lower in {"camelcase", "camel_case", "camel"}:
        return "camelCase"
    if lower == "sdtm":
        return "sdtm"
    raise ValueError(
        f"Unsupported naming_convention '{convention}'. "
        "Expected one of: 'snake_case', 'camelCase', 'sdtm'."
    )


_SDTM_DOMAIN_NON_ALNUM_RE = re.compile(r"[^A-Z0-9]+")


def resolve_sdtm_domain(domain: str | None, *, fallback: str = "AE") -> str:
    """Canonicalize an SDTM domain code or form/domain alias (e.g. 'PD' -> 'DV')."""
    if not domain or not isinstance(domain, str) or not domain.strip():
        return fallback
    upper = domain.strip().upper()
    compact = _SDTM_DOMAIN_NON_ALNUM_RE.sub("", upper)
    if not compact:
        return fallback
    if compact in _DOMAIN_NAME_ALIASES:
        return _DOMAIN_NAME_ALIASES[compact]
    # Prefix heuristics for common EDC form keys (e.g. 'F_AE', 'AE_FORM', 'VS_VITALS')
    tokens = [tok for tok in _SDTM_DOMAIN_NON_ALNUM_RE.split(upper) if tok and tok != "F"]
    for tok in tokens:
        if tok in _DOMAIN_NAME_ALIASES:
            return _DOMAIN_NAME_ALIASES[tok]
    sanitized = compact.lstrip("0123456789")
    return sanitized if sanitized else fallback


def format_variable_name(
    name: str,
    convention: str | None,
    *,
    domain: str | None = None,
    is_metadata: bool = False,
) -> str:
    """Format a metadata or clinical variable key using ``convention``.

    Supported conventions: ``None`` (unchanged), ``"snake_case"``, ``"camelCase"``, ``"sdtm"``.
    """
    canonical_conv = normalize_naming_convention(convention)
    if canonical_conv is None or not name:
        return name

    snake = _to_snake_identifier(name)
    if not snake:
        return name

    if canonical_conv == "snake_case":
        return snake

    if canonical_conv == "camelCase":
        parts = [p for p in snake.split("_") if p]
        if not parts:
            return name
        return parts[0] + "".join(part.capitalize() for part in parts[1:])

    # canonical_conv == "sdtm"
    if is_metadata:
        if snake in _METADATA_SDTM_KEYS:
            return _METADATA_SDTM_KEYS[snake]
        return snake.upper()
    if snake in _METADATA_SDTM_KEYS and name in {
        "studyKey",
        "study_key",
        "STUDYID",
        "subjectKey",
        "subject_key",
        "USUBJID",
    }:
        return _METADATA_SDTM_KEYS[snake]
    compact_norm = _normalize_key(name)
    if domain:
        dom = resolve_sdtm_domain(domain)
        spec = SDTM_DOMAIN_SPECS.get(dom)
        if spec is not None:
            for sdtm_var, aliases in spec["aliases"].items():
                if name == sdtm_var or any(_normalize_key(a) == compact_norm for a in aliases):
                    return str(sdtm_var)
    if compact_norm in _GLOBAL_UNAMBIGUOUS_SDTM_ALIASES:
        return _GLOBAL_UNAMBIGUOUS_SDTM_ALIASES[compact_norm]
    return re.sub(r"[^A-Za-z0-9]", "", snake).upper()


_BASE_SDTM_CONSUMED_KEYS: frozenset[str] = frozenset(
    {
        "USUBJID",
        "subjectKey",
        "subject_key",
        "study_key",
        "studyKey",
        "STUDYID",
        "DOMAIN",
        "domain",
        "recordId",
        "record_id",
        "formId",
        "form_id",
        "visitId",
        "visit_id",
        "recordStatus",
        "record_status",
    }
)


@functools.lru_cache(maxsize=128)
def _get_model_field_aliases(
    model_cls: type[BaseModel],
) -> tuple[dict[str, str], dict[str, set[str]]]:
    """Extract and cache field alias mappings for a Pydantic model class."""
    model_field_to_serialized: dict[str, str] = {}
    all_field_aliases: dict[str, set[str]] = {}

    for fname, finfo in getattr(model_cls, "model_fields", {}).items():
        serialized_key = fname
        if (
            isinstance(getattr(finfo, "serialization_alias", None), str)
            and finfo.serialization_alias
        ):
            serialized_key = finfo.serialization_alias
        elif isinstance(getattr(finfo, "alias", None), str) and finfo.alias:
            serialized_key = finfo.alias

        field_aliases = {fname, serialized_key}
        if isinstance(getattr(finfo, "alias", None), str) and finfo.alias:
            field_aliases.add(finfo.alias)
        if (
            isinstance(getattr(finfo, "serialization_alias", None), str)
            and finfo.serialization_alias
        ):
            field_aliases.add(finfo.serialization_alias)
        if isinstance(getattr(finfo, "validation_alias", None), str) and finfo.validation_alias:
            field_aliases.add(finfo.validation_alias)

        for alias in field_aliases:
            model_field_to_serialized[alias] = serialized_key
            model_field_to_serialized.setdefault(alias.lower(), serialized_key)
            all_field_aliases.setdefault(alias, set()).update(field_aliases)
            all_field_aliases.setdefault(alias.lower(), set()).update(field_aliases)

    return model_field_to_serialized, all_field_aliases


@functools.lru_cache(maxsize=128)
def _get_model_consumed_keys(model_cls: type[BaseModel]) -> frozenset[str]:
    """Precompute base consumed keys for a Pydantic model class."""
    model_field_to_serialized, all_field_aliases = _get_model_field_aliases(model_cls)
    consumed = set(_BASE_SDTM_CONSUMED_KEYS)
    for initial_k in list(consumed):
        if initial_k in model_field_to_serialized:
            serialized_k = model_field_to_serialized[initial_k]
            consumed.add(serialized_k)
            consumed.update(all_field_aliases.get(serialized_k, set()))
        if initial_k.lower() in model_field_to_serialized:
            serialized_k = model_field_to_serialized[initial_k.lower()]
            consumed.add(serialized_k)
            consumed.update(all_field_aliases.get(serialized_k, set()))
    return frozenset(consumed)


def _mask_value_recursive(
    val: Any,
    *,
    registry: SensitivityRegistry,
    collapsed_phi_keys: frozenset[str],
) -> Any:
    """Recursively mask nested dict/list/tuple values against SensitivityRegistry and custom phi_keys."""
    if isinstance(val, dict):
        masked_dict: dict[str, Any] = {}
        for k, v in val.items():
            key_str = str(k)
            if (
                bool(collapsed_phi_keys)
                and not registry.is_exempt(key_str)
                and _collapse_alnum_key(key_str) in collapsed_phi_keys
            ) or registry.is_sensitive(key_str):
                masked_dict[k] = _MASKED_SENTINEL
            else:
                masked_dict[k] = _mask_value_recursive(
                    v,
                    registry=registry,
                    collapsed_phi_keys=collapsed_phi_keys,
                )
        return masked_dict
    if isinstance(val, list):
        return [
            _mask_value_recursive(
                item,
                registry=registry,
                collapsed_phi_keys=collapsed_phi_keys,
            )
            for item in val
        ]
    if isinstance(val, tuple):
        return tuple(
            _mask_value_recursive(
                item,
                registry=registry,
                collapsed_phi_keys=collapsed_phi_keys,
            )
            for item in val
        )
    return mask_clinical_phi(val, registry=registry)


def _record_id_sort_key(raw_id: Any) -> tuple[int, int, str]:
    """Build a deterministic comparable sort key for numeric, string, or missing record IDs."""
    if raw_id is None or raw_id == "":
        return (2, 0, "")
    if isinstance(raw_id, int) and not isinstance(raw_id, bool):
        return (0, raw_id, "")
    if isinstance(raw_id, str):
        stripped = raw_id.strip()
        if not stripped:
            return (2, 0, "")
        try:
            return (0, int(stripped), stripped)
        except ValueError:
            return (1, 0, stripped)
    return (1, 0, str(raw_id))


def _dtc_sort_key(canonical_dtc: str | None) -> tuple[int, str]:
    """Place present canonical ISO 8601 timestamps before missing values."""
    if canonical_dtc:
        return (0, canonical_dtc)
    return (1, "")


def _topic_sort_key(topic_values: tuple[Any, ...]) -> tuple[tuple[int, str], ...]:
    """Build comparable sort tuples for domain topic variable values."""
    return tuple(
        (1, "") if (v is None or (isinstance(v, str) and not v)) else (0, str(v))
        for v in topic_values
    )


def _extract_stream_usubjid_and_id(rec: Any) -> tuple[str, tuple[int, int, str]]:
    """Extract (usubjid, record_id_sort_key) from a raw Record, model, or dict for stream merging."""
    rec_data = getattr(rec, "record_data", None)
    usubjid: Any = None
    raw_id: Any = getattr(rec, "record_id", None)
    if raw_id is None:
        raw_id = getattr(rec, "recordId", None)

    if isinstance(rec_data, dict):
        usubjid = rec_data.get("USUBJID") or rec_data.get("usubjid")
    if isinstance(rec, dict):
        rd = rec.get("record_data") or rec.get("recordData")
        if isinstance(rd, dict) and not usubjid:
            usubjid = rd.get("USUBJID") or rd.get("usubjid")
        if not usubjid:
            usubjid = rec.get("USUBJID") or rec.get("subjectKey") or rec.get("subject_key")
        if raw_id is None:
            raw_id = rec.get("recordId") if "recordId" in rec else rec.get("record_id")
    if not usubjid:
        usubjid = getattr(rec, "subject_key", None) or getattr(rec, "subjectKey", None) or ""
    if raw_id is None:
        raw_id = getattr(rec, "record_oid", None) or (
            rec.get("recordOid") if isinstance(rec, dict) else None
        )
    return str(usubjid), _record_id_sort_key(raw_id)


@dataclass
class _PreparedSdtmRow:
    """Normalized SDTM row prior to final per-subject sequence number assignment."""

    usubjid: str
    sort_key: tuple[tuple[int, str], tuple[tuple[int, str], ...], tuple[int, int, str], int]
    row: dict[str, Any]
    record_id: str | None = None
    source_record: Any = None


class _SdtmNormalizer:
    """Reusable per-domain normalizer encapsulating SDTM specs, enrichment, and PHI redaction."""

    def __init__(
        self,
        *,
        domain: str,
        study_key: str,
        include_deleted: bool,
        mask_phi: bool,
        pipeline: Any | None = None,
        phi_keys: Iterable[str] | None = None,
        registry: SensitivityRegistry | None = None,
    ) -> None:
        domain_clean = resolve_sdtm_domain(domain)
        self.domain_clean = domain_clean
        self.study_key = study_key
        self.include_deleted = include_deleted
        self.mask_phi = mask_phi
        self.pipeline = pipeline
        self.registry: SensitivityRegistry = (
            global_sensitivity_registry if registry is None else registry
        )
        self.include_seq = domain_clean != "DM"
        self.seq_var = f"{domain_clean}SEQ"

        spec = SDTM_DOMAIN_SPECS.get(domain_clean)
        if spec:
            self.std_vars: list[str] = list(spec["variables"])
            self.alias_map: dict[str, list[str]] = spec["aliases"]
            self.flag_vars: set[str] = spec["flags"]
            self.dtc_vars: set[str] = spec["dtc"]
            self.primary_dtc: str = spec["primary_dtc"]
            self.topic_vars: tuple[str, ...] = spec["topic_vars"]
        else:
            self.std_vars = (
                ["STUDYID", "DOMAIN", "USUBJID", self.seq_var]
                if self.include_seq
                else ["STUDYID", "DOMAIN", "USUBJID"]
            )
            self.alias_map = {}
            self.flag_vars = set()
            self.dtc_vars = set()
            self.primary_dtc = f"{domain_clean}STDTC"
            self.topic_vars = (
                f"{domain_clean}TERM",
                f"{domain_clean}TRT",
                f"{domain_clean}TESTCD",
                f"{domain_clean}TEST",
                "SUBJID",
            )

        self.has_enrichment_transforms = bool(
            pipeline is not None
            and (getattr(pipeline, "terminology", None) or getattr(pipeline, "mappings", None))
        )
        self.terminology: dict[str, dict[str, str]] = (
            getattr(pipeline, "terminology", {}) if pipeline is not None else {}
        )
        merged_phi_keys: set[str] = set()
        if pipeline is not None and getattr(pipeline, "phi_keys", None):
            merged_phi_keys.update(k for k in pipeline.phi_keys if k)
        if phi_keys is not None:
            merged_phi_keys.update(k for k in phi_keys if k)
        self.pipeline_phi_keys: set[str] = merged_phi_keys
        self.collapsed_phi_keys: frozenset[str] = frozenset(
            _collapse_alnum_key(pk) for pk in self.pipeline_phi_keys
        )
        self._exempt_cache: dict[str, bool] = {}
        self._sens_cache: dict[str, bool] = {}
        self.sensitive_mapped_targets: frozenset[str] = frozenset()
        sensitive_targets: set[str] = set()
        remaps_usubjid = False
        mappings = getattr(pipeline, "mappings", None) if pipeline is not None else None
        if mappings:
            for src_var, rule in mappings.items():
                if rule.target_field and rule.target_field.strip().upper() == "USUBJID":
                    remaps_usubjid = True
                if (
                    rule.target_field
                    and not self._is_exempt_key(rule.target_field)
                    and self._is_sensitive_key(src_var)
                ):
                    sensitive_targets.add(_collapse_alnum_key(rule.target_field))
        self.sensitive_mapped_targets = frozenset(sensitive_targets)
        self._sens_cache.clear()
        self.remaps_usubjid = remaps_usubjid
        self._usubjid_is_sensitive = self._is_sensitive_key("USUBJID")
        self._subject_key_is_sensitive = self._is_sensitive_key(
            "subject_key"
        ) or self._is_sensitive_key("subjectKey")
        self._topic_set = frozenset(self.topic_vars)
        self._compiled_std_vars: tuple[
            tuple[
                str,
                str,
                tuple[tuple[str, str], ...],
                bool,
                bool,
                bool,
                bool,
                bool,
                bool,
            ],
            ...,
        ] = tuple(
            (
                vname,
                vname.lower(),
                tuple((c, c.lower()) for c in self.alias_map.get(vname, [])),
                vname in self.dtc_vars or vname.endswith("DTC"),
                _is_birth_dtc_key(vname),
                vname in self.flag_vars or vname.endswith("SET"),
                self._is_exempt_key(vname),
                self._is_sensitive_key(vname),
                vname in self._topic_set,
            )
            for vname in self.std_vars
            if vname not in ("STUDYID", "DOMAIN", "USUBJID", self.seq_var)
        )

    def _is_exempt_key(self, key: str) -> bool:
        """Cached exemption lookup for *key*."""
        res = self._exempt_cache.get(key)
        if res is None:
            res = self.registry.is_exempt(key)
            self._exempt_cache[key] = res
        return res

    def _is_sensitive_key(self, key: str) -> bool:
        """Return True when *key* is sensitive via active registry, study config, or mapping."""
        res = self._sens_cache.get(key)
        if res is not None:
            return res
        if self._is_exempt_key(key):
            self._sens_cache[key] = False
            return False
        if self.registry.is_sensitive(key):
            self._sens_cache[key] = True
            return True
        if self.collapsed_phi_keys or self.sensitive_mapped_targets:
            collapsed = _collapse_alnum_key(key)
            matched = (
                collapsed in self.collapsed_phi_keys or collapsed in self.sensitive_mapped_targets
            )
            self._sens_cache[key] = matched
            return matched
        self._sens_cache[key] = False
        return False

    def _apply_var_terminology(self, var_name: str, val: Any) -> Any:
        """Apply domain-qualified or variable-keyed StudyConfiguration terminology lookups."""
        if not self.terminology or val is None or isinstance(val, (dict, list, tuple)):
            return val
        str_val = str(val)
        domain_qual = f"{self.domain_clean}.{var_name}"
        if domain_qual in self.terminology and str_val in self.terminology[domain_qual]:
            return self.terminology[domain_qual][str_val]
        if var_name in self.terminology and str_val in self.terminology[var_name]:
            return self.terminology[var_name][str_val]
        return val

    def prepare_record(self, rec: Any, orig_idx: int) -> _PreparedSdtmRow | None:
        """Normalize a single clinical record and compute its Subject-group sort key."""
        if not self.include_deleted and _is_deleted_record(rec):
            return None

        model_field_to_serialized: dict[str, str] = {}
        all_field_aliases: dict[str, set[str]] = {}
        base_consumed: frozenset[str] | set[str] = _BASE_SDTM_CONSUMED_KEYS

        raw_record_id: Any = getattr(rec, "record_id", None)
        if raw_record_id is None:
            raw_record_id = getattr(rec, "recordId", None)

        if hasattr(rec, "record_data") and getattr(rec, "record_data", None) is not None:
            rec_dict = dict(rec.record_data) if isinstance(rec.record_data, dict) else {}
            if getattr(rec, "subject_key", None):
                rec_dict["subject_key"] = rec.subject_key
                rec_dict["subjectKey"] = rec.subject_key
            if getattr(rec, "date_created", None):
                rec_dict["date_created"] = rec.date_created
        elif isinstance(rec, BaseModel):
            rec_dict = rec.model_dump(by_alias=True)
            model_cls = type(rec)
            model_field_to_serialized, all_field_aliases = _get_model_field_aliases(model_cls)
            base_consumed = _get_model_consumed_keys(model_cls)
        elif isinstance(rec, dict):
            rec_dict = dict(rec)
        else:
            rec_dict = dict(getattr(rec, "__dict__", {}))

        if "record_data" in rec_dict and isinstance(rec_dict["record_data"], dict):
            nested_data = dict(rec_dict.pop("record_data"))
            for k, v in nested_data.items():
                if k not in rec_dict:
                    rec_dict[k] = v

        if raw_record_id is None:
            raw_record_id = (
                rec_dict.get("recordId")
                if "recordId" in rec_dict
                else rec_dict.get("record_id") or getattr(rec, "record_oid", None)
            )

        if self.pipeline is not None and (
            self.has_enrichment_transforms or not hasattr(self.pipeline, "terminology")
        ):
            rec_dict = self.pipeline.process(rec_dict, mask_phi=False)

        explicit_usubjid = rec_dict.get("USUBJID") or rec_dict.get("usubjid")
        usubjid_raw = (
            explicit_usubjid
            or rec_dict.get("subjectKey")
            or rec_dict.get("subject_key")
            or (
                rec_dict.get(model_field_to_serialized.get("subject_key", ""))
                if model_field_to_serialized
                else None
            )
            or (
                rec_dict.get(model_field_to_serialized.get("subjectKey", ""))
                if model_field_to_serialized
                else None
            )
            or getattr(rec, "subject_key", None)
            or getattr(rec, "subjectKey", None)
            or ""
        )
        usubjid = str(usubjid_raw)
        if (
            self.mask_phi
            and usubjid
            and (
                self._usubjid_is_sensitive
                or (not explicit_usubjid and self._subject_key_is_sensitive)
            )
        ):
            usubjid = _MASKED_SENTINEL

        row: dict[str, Any] = {
            "STUDYID": self.study_key,
            "DOMAIN": self.domain_clean,
            "USUBJID": usubjid,
        }
        if self.include_seq:
            row[self.seq_var] = 0

        consumed_keys: set[str] = set(base_consumed)
        if not self.include_seq:
            consumed_keys.add("DMSEQ")
            consumed_keys.add("dmseq")

        lower_rec_dict = {k.lower(): (k, v) for k, v in rec_dict.items()}
        canonical_dtc_for_sort: str | None = None
        unmasked_topic_map: dict[str, Any] = {}

        for (
            var_name,
            var_lower,
            cand_pairs,
            is_dtc,
            is_birth_dtc,
            is_flag,
            is_var_exempt,
            is_var_sensitive,
            is_topic,
        ) in self._compiled_std_vars:
            val = None
            found_key = None
            if var_name in rec_dict:
                val = rec_dict[var_name]
                found_key = var_name
            elif var_lower in lower_rec_dict:
                found_key, val = lower_rec_dict[var_lower]
            elif (
                var_name in model_field_to_serialized
                and model_field_to_serialized[var_name] in rec_dict
            ):
                found_key = model_field_to_serialized[var_name]
                val = rec_dict[found_key]
            elif (
                var_lower in model_field_to_serialized
                and model_field_to_serialized[var_lower] in rec_dict
            ):
                found_key = model_field_to_serialized[var_lower]
                val = rec_dict[found_key]
            else:
                for cand, cand_lower in cand_pairs:
                    if cand in rec_dict:
                        val = rec_dict[cand]
                        found_key = cand
                        break
                    if cand_lower in lower_rec_dict:
                        found_key, val = lower_rec_dict[cand_lower]
                        break
                    if (
                        cand in model_field_to_serialized
                        and model_field_to_serialized[cand] in rec_dict
                    ):
                        found_key = model_field_to_serialized[cand]
                        val = rec_dict[found_key]
                        break
                    if (
                        cand_lower in model_field_to_serialized
                        and model_field_to_serialized[cand_lower] in rec_dict
                    ):
                        found_key = model_field_to_serialized[cand_lower]
                        val = rec_dict[found_key]
                        break

            if found_key:
                consumed_keys.add(found_key)
                if all_field_aliases:
                    consumed_keys.update(all_field_aliases.get(found_key, ()))
                    consumed_keys.update(all_field_aliases.get(found_key.lower(), ()))
                for cand, cand_lower in cand_pairs:
                    consumed_keys.add(cand)
                    if cand_lower in lower_rec_dict:
                        consumed_keys.add(lower_rec_dict[cand_lower][0])
                    if model_field_to_serialized:
                        if cand in model_field_to_serialized:
                            serialized_cand = model_field_to_serialized[cand]
                            consumed_keys.add(serialized_cand)
                            consumed_keys.update(all_field_aliases.get(serialized_cand, ()))
                        if cand_lower in model_field_to_serialized:
                            serialized_cand = model_field_to_serialized[cand_lower]
                            consumed_keys.add(serialized_cand)
                            consumed_keys.update(all_field_aliases.get(serialized_cand, ()))
                        consumed_keys.update(all_field_aliases.get(cand, ()))
                        consumed_keys.update(all_field_aliases.get(cand_lower, ()))

            if self.terminology:
                val = self._apply_var_terminology(var_name, val)
            if is_topic:
                unmasked_topic_map[var_name] = val

            should_mask_var = (
                self.mask_phi
                and not is_var_exempt
                and (
                    is_var_sensitive
                    or (found_key is not None and self._is_sensitive_key(found_key))
                )
            )
            if is_dtc:
                full_dtc = format_iso8601_dtc(val, mask_phi=False)
                if var_name == self.primary_dtc or (
                    canonical_dtc_for_sort is None and full_dtc is not None
                ):
                    canonical_dtc_for_sort = full_dtc
                if self.mask_phi and is_birth_dtc:
                    row[var_name] = format_iso8601_dtc(val, mask_phi=True)
                elif should_mask_var:
                    row[var_name] = None
                else:
                    row[var_name] = full_dtc
            elif is_flag:
                formatted_flag = _format_sdtm_flag(val)
                if should_mask_var and formatted_flag is not None:
                    row[var_name] = _MASKED_SENTINEL
                else:
                    row[var_name] = formatted_flag
            elif should_mask_var:
                row[var_name] = (
                    _MASKED_SENTINEL if (val is not None or found_key is not None) else None
                )
            else:
                row[var_name] = val

        normalized_consumed = {_normalize_key(c) for c in consumed_keys}
        for k, raw_v in rec_dict.items():
            if k not in consumed_keys and _normalize_key(k) not in normalized_consumed:
                extra_val = self._apply_var_terminology(k, raw_v) if self.terminology else raw_v
                if k in self._topic_set and k not in unmasked_topic_map:
                    unmasked_topic_map[k] = extra_val
                if self.mask_phi and not self._is_exempt_key(k):
                    if _is_birth_dtc_key(k):
                        if canonical_dtc_for_sort is None:
                            canonical_dtc_for_sort = format_iso8601_dtc(extra_val, mask_phi=False)
                        row[k] = format_iso8601_dtc(extra_val, mask_phi=True)
                        continue
                    if self._is_sensitive_key(k):
                        row[k] = _MASKED_SENTINEL
                        continue
                    if isinstance(extra_val, (dict, list, tuple)):
                        extra_val = _mask_value_recursive(
                            extra_val,
                            registry=self.registry,
                            collapsed_phi_keys=self.collapsed_phi_keys,
                        )
                k_lower = k.lower()
                if "date" in k_lower or "time" in k_lower or "dtc" in k_lower:
                    formatted_extra_dtc = format_iso8601_dtc(extra_val, mask_phi=False)
                    k_upper = k.upper()
                    if k_upper == self.primary_dtc or (
                        canonical_dtc_for_sort is None
                        and k_upper.endswith("DTC")
                        and formatted_extra_dtc is not None
                    ):
                        canonical_dtc_for_sort = formatted_extra_dtc
                    row[k] = formatted_extra_dtc
                else:
                    row[k] = extra_val

        topic_vals = tuple(unmasked_topic_map.get(tv) for tv in self.topic_vars)
        sort_key = (
            _dtc_sort_key(canonical_dtc_for_sort),
            _topic_sort_key(topic_vals),
            _record_id_sort_key(raw_record_id),
            orig_idx,
        )
        return _PreparedSdtmRow(
            usubjid=usubjid,
            sort_key=sort_key,
            row=row,
            record_id=_canonical_sdtm_record_id(raw_record_id),
            source_record=rec,
        )

    @staticmethod
    def _is_non_missing_sdtm_value(val: Any) -> bool:
        if val is None or val == "":
            return False
        return not (isinstance(val, float) and math.isnan(val))

    def finalize_subject_group(
        self,
        group: list[_PreparedSdtmRow],
        seq_counters: MutableMapping[str, int] | TransactionalSdtmSequencer,
    ) -> list[dict[str, Any]]:
        """Sort a Subject's normalized rows, consolidate DM per USUBJID, or stamp <DOM>SEQ."""
        group.sort(key=lambda item: item.sort_key)
        if not self.include_seq:
            merged_by_usubjid: dict[str, dict[str, Any]] = {}
            for item in group:
                item.row.pop("DMSEQ", None)
                item.row.pop("dmseq", None)
                existing = merged_by_usubjid.get(item.usubjid)
                if existing is None:
                    merged_by_usubjid[item.usubjid] = dict(item.row)
                else:
                    for key, val in item.row.items():
                        if key in ("STUDYID", "DOMAIN", "USUBJID"):
                            continue
                        if self._is_non_missing_sdtm_value(val) or key not in existing:
                            existing[key] = val
            return list(merged_by_usubjid.values())

        rows: list[dict[str, Any]] = []
        for item in group:
            next_seq = _assign_sdtm_sequence(
                seq_counters,
                study_id=self.study_key,
                domain=self.domain_clean,
                usubjid=item.usubjid,
                record_id=item.record_id,
            )
            item.row[self.seq_var] = next_seq
            rows.append(item.row)
        return rows

    def rows_to_dataframe(self, rows: list[dict[str, Any]]) -> pd.DataFrame:
        """Build a column-ordered SDTM DataFrame from finalized rows."""
        if not rows:
            return pd.DataFrame(columns=self.std_vars)
        df = pd.DataFrame(rows)
        cols = [c for c in self.std_vars if c in df.columns]
        extra_cols = [c for c in df.columns if c not in self.std_vars]
        return df[cols + extra_cols]


def _iter_sorted_sdtm_frames(
    records: Iterable[Any],
    normalizer: _SdtmNormalizer,
    *,
    chunk_size: int,
    seq_counters: MutableMapping[str, int] | TransactionalSdtmSequencer,
    source_is_ordered: bool = False,
    max_buffered_records: int = _DEFAULT_SDTM_MAX_BUFFERED_RECORDS,
) -> Iterator[pd.DataFrame]:
    """Yield bounded SDTM DataFrames grouped by USUBJID with continuous sequence counters."""
    effective_cap = max(1, chunk_size, max_buffered_records)

    if isinstance(records, (list, tuple)):
        grouped: dict[str, list[_PreparedSdtmRow]] = {}
        for idx, rec in enumerate(records):
            prepared = normalizer.prepare_record(rec, idx)
            if prepared is not None:
                grouped.setdefault(prepared.usubjid, []).append(prepared)
        emit_rows: list[dict[str, Any]] = []
        for usubjid in sorted(grouped):
            emit_rows.extend(normalizer.finalize_subject_group(grouped[usubjid], seq_counters))
            while len(emit_rows) >= chunk_size:
                batch = emit_rows[:chunk_size]
                emit_rows = emit_rows[chunk_size:]
                yield normalizer.rows_to_dataframe(batch)
        if emit_rows:
            yield normalizer.rows_to_dataframe(emit_rows)
        return

    staged_groups: dict[str, list[_PreparedSdtmRow]] = {}
    group_order: list[str] = []
    last_usubjid: str | None = None
    staged_total_rows = 0
    yielded_any = False
    orig_idx = 0
    rec_iter = iter(records)
    emit_buffer: list[dict[str, Any]] = []

    def _flush_oldest_group() -> Iterator[pd.DataFrame]:
        nonlocal staged_total_rows, yielded_any, emit_buffer
        if not staged_groups:
            return
        target_subj = max(staged_groups, key=lambda s: len(staged_groups[s]))
        flushed_group = staged_groups.pop(target_subj)
        if target_subj in group_order:
            group_order.remove(target_subj)
        staged_total_rows -= len(flushed_group)
        emit_buffer.extend(normalizer.finalize_subject_group(flushed_group, seq_counters))
        while len(emit_buffer) >= chunk_size:
            batch = emit_buffer[:chunk_size]
            emit_buffer = emit_buffer[chunk_size:]
            yielded_any = True
            yield normalizer.rows_to_dataframe(batch)

    for rec in rec_iter:
        prepared = normalizer.prepare_record(rec, orig_idx)
        orig_idx += 1
        if prepared is None:
            continue

        if not yielded_any:
            if (
                last_usubjid is not None
                and prepared.usubjid != last_usubjid
                and (prepared.usubjid < last_usubjid or prepared.usubjid in staged_groups)
            ):
                # Ungrouped input detected: stage within max_buffered_records ceiling.
                staged_groups.setdefault(prepared.usubjid, []).append(prepared)
                staged_total_rows += 1
                while staged_total_rows >= effective_cap:
                    yield from _flush_oldest_group()
                for tail_rec in rec_iter:
                    tail_prepared = normalizer.prepare_record(tail_rec, orig_idx)
                    orig_idx += 1
                    if tail_prepared is None:
                        continue
                    staged_groups.setdefault(tail_prepared.usubjid, []).append(tail_prepared)
                    staged_total_rows += 1
                    while staged_total_rows >= effective_cap:
                        yield from _flush_oldest_group()
                for usubjid in sorted(staged_groups):
                    emit_buffer.extend(
                        normalizer.finalize_subject_group(staged_groups[usubjid], seq_counters)
                    )
                    while len(emit_buffer) >= chunk_size:
                        batch = emit_buffer[:chunk_size]
                        emit_buffer = emit_buffer[chunk_size:]
                        yield normalizer.rows_to_dataframe(batch)
                if emit_buffer:
                    yield normalizer.rows_to_dataframe(emit_buffer)
                return

            if prepared.usubjid not in staged_groups:
                staged_groups[prepared.usubjid] = []
                group_order.append(prepared.usubjid)
            staged_groups[prepared.usubjid].append(prepared)
            staged_total_rows += 1
            last_usubjid = prepared.usubjid

            completed_groups = len(group_order) - 1
            completed_rows = staged_total_rows - len(staged_groups[last_usubjid])
            min_completed_groups = 1 if source_is_ordered else (4 if chunk_size == 1 else 2)
            if completed_groups >= min_completed_groups and completed_rows >= chunk_size:
                for done_subj in group_order[:-1]:
                    done_group = staged_groups.pop(done_subj)
                    emit_buffer.extend(normalizer.finalize_subject_group(done_group, seq_counters))
                group_order = [last_usubjid]
                staged_total_rows = len(staged_groups[last_usubjid])
                while len(emit_buffer) >= chunk_size:
                    batch = emit_buffer[:chunk_size]
                    emit_buffer = emit_buffer[chunk_size:]
                    yielded_any = True
                    yield normalizer.rows_to_dataframe(batch)
            elif staged_total_rows >= effective_cap:
                yield from _flush_oldest_group()
                last_usubjid = group_order[-1] if group_order else None
            continue

        # Already streaming ordered Subject groups
        if last_usubjid is not None and prepared.usubjid != last_usubjid:
            if last_usubjid in staged_groups:
                finished_group = staged_groups.pop(last_usubjid)
                staged_total_rows -= len(finished_group)
                emit_buffer.extend(normalizer.finalize_subject_group(finished_group, seq_counters))
            staged_groups[prepared.usubjid] = [prepared]
            staged_total_rows += 1
            last_usubjid = prepared.usubjid
            while len(emit_buffer) >= chunk_size:
                batch = emit_buffer[:chunk_size]
                emit_buffer = emit_buffer[chunk_size:]
                yield normalizer.rows_to_dataframe(batch)
        else:
            last_usubjid = prepared.usubjid
            staged_groups.setdefault(last_usubjid, []).append(prepared)
            staged_total_rows += 1
            if staged_total_rows >= effective_cap:
                yield from _flush_oldest_group()
                last_usubjid = next(iter(staged_groups), None)

    for rem_subj in sorted(staged_groups) if not yielded_any else list(staged_groups):
        emit_buffer.extend(normalizer.finalize_subject_group(staged_groups[rem_subj], seq_counters))
    while len(emit_buffer) >= chunk_size:
        batch = emit_buffer[:chunk_size]
        emit_buffer = emit_buffer[chunk_size:]
        yield normalizer.rows_to_dataframe(batch)
    if emit_buffer:
        yield normalizer.rows_to_dataframe(emit_buffer)


def _consolidate_dm_source_records(group: list[_PreparedSdtmRow]) -> Any:
    """Consolidate sorted DM source records for one USUBJID into a single Record or dict."""
    if len(group) == 1:
        return group[0].source_record

    merged_data: dict[str, Any] = {}
    latest_rec = group[-1].source_record
    for item in group:
        src = item.source_record
        if isinstance(src, Mapping):
            nested = src.get("record_data") or src.get("recordData")
            if isinstance(nested, Mapping):
                for k, v in nested.items():
                    if k.upper() == "DMSEQ":
                        continue
                    if _SdtmNormalizer._is_non_missing_sdtm_value(v) or k not in merged_data:
                        merged_data[k] = v
            else:
                for k, v in src.items():
                    if k in ("record_id", "recordId", "DMSEQ", "dmseq"):
                        continue
                    if _SdtmNormalizer._is_non_missing_sdtm_value(v) or k not in merged_data:
                        merged_data[k] = v
        else:
            rd = getattr(src, "record_data", None)
            if isinstance(rd, Mapping):
                for k, v in rd.items():
                    if k.upper() == "DMSEQ":
                        continue
                    if _SdtmNormalizer._is_non_missing_sdtm_value(v) or k not in merged_data:
                        merged_data[k] = v

    model_copy = getattr(latest_rec, "model_copy", None)
    if callable(model_copy) and hasattr(latest_rec, "record_data"):
        return model_copy(update={"record_data": merged_data})
    if isinstance(latest_rec, Mapping):
        cloned = dict(latest_rec)
        cloned.pop("DMSEQ", None)
        cloned.pop("dmseq", None)
        if "record_data" in cloned and isinstance(cloned["record_data"], Mapping):
            cloned["record_data"] = merged_data
        elif "recordData" in cloned and isinstance(cloned["recordData"], Mapping):
            cloned["recordData"] = merged_data
        else:
            cloned.update(merged_data)
        return cloned
    return latest_rec


def iter_staged_sdtm_records(
    records: Iterable[Any],
    *,
    study_key: str | None = None,
    sdtm_domain: str | None = None,
    include_deleted: bool = False,
    mask_phi: bool = True,
    max_buffered_records: int = _DEFAULT_SDTM_MAX_BUFFERED_RECORDS,
    source_is_ordered: bool = False,
    phi_keys: Iterable[str] | None = None,
    registry: SensitivityRegistry | None = None,
    pipeline: Any = _UNSET_PIPELINE,
) -> Iterator[Any]:
    """Stage clinical records by ``(DOMAIN, USUBJID)`` in chronological ``--DTC`` order.

    Honors ``max_buffered_records`` when draining ungrouped streams, drops deleted/draft
    records when ``include_deleted=False``, and consolidates ``DM`` revisions per ``USUBJID``
    prior to batch chunking so streaming sinks emit identical sequences to ``RecordMapper``.
    """
    effective_cap = max(1, max_buffered_records)
    resolved_study_key = study_key or ""
    resolved_pipeline = _resolve_sdtm_pipeline(resolved_study_key, pipeline, _UNSET_PIPELINE)
    normalizers: dict[str, _SdtmNormalizer] = {}

    def _get_normalizer(dom_code: str) -> _SdtmNormalizer:
        norm = normalizers.get(dom_code)
        if norm is None:
            norm = _SdtmNormalizer(
                domain=dom_code,
                study_key=resolved_study_key,
                include_deleted=include_deleted,
                mask_phi=mask_phi,
                pipeline=resolved_pipeline,
                phi_keys=phi_keys,
                registry=registry,
            )
            normalizers[dom_code] = norm
        return norm

    def _resolve_rec_domain(rec: Any) -> str:
        if sdtm_domain:
            return resolve_sdtm_domain(sdtm_domain, fallback="AE")
        if isinstance(rec, Mapping):
            raw_dom = (
                rec.get("DOMAIN") or rec.get("domain") or rec.get("form_key") or rec.get("formKey")
            )
        else:
            raw_dom = getattr(rec, "form_key", None) or getattr(rec, "formKey", None)
        return resolve_sdtm_domain(raw_dom, fallback="AE")

    def _emit_group(dom_code: str, group: list[_PreparedSdtmRow]) -> Iterator[Any]:
        group.sort(key=lambda item: item.sort_key)
        if dom_code == "DM":
            yield _consolidate_dm_source_records(group)
        else:
            for item in group:
                yield item.source_record

    staged: dict[tuple[str, str], list[_PreparedSdtmRow]] = {}
    subj_order: list[str] = []
    last_usubjid: str | None = None
    buffered_count = 0
    ungrouped = not source_is_ordered

    def _flush_subject(target_usubjid: str) -> Iterator[Any]:
        nonlocal buffered_count
        keys_for_subj = sorted(k for k in staged if k[1] == target_usubjid)
        for key in keys_for_subj:
            grp = staged.pop(key)
            buffered_count -= len(grp)
            yield from _emit_group(key[0], grp)
        if target_usubjid in subj_order:
            subj_order.remove(target_usubjid)

    for idx, rec in enumerate(records):
        dom_code = _resolve_rec_domain(rec)
        norm = _get_normalizer(dom_code)
        prepared = norm.prepare_record(rec, idx)
        if prepared is None:
            continue

        usubjid = prepared.usubjid
        if not ungrouped and last_usubjid is not None and usubjid != last_usubjid:
            if usubjid < last_usubjid or usubjid in subj_order:
                ungrouped = True
            else:
                yield from _flush_subject(last_usubjid)

        if usubjid not in subj_order:
            subj_order.append(usubjid)
        staged.setdefault((dom_code, usubjid), []).append(prepared)
        buffered_count += 1
        last_usubjid = usubjid

        while buffered_count >= effective_cap and staged:
            # Prefer evicting completed subjects before splitting the active subject group.
            candidate_subjs = [
                s for s in subj_order if s != last_usubjid and any(k[1] == s for k in staged)
            ]
            if candidate_subjs:
                evict_subj = candidate_subjs[0]
            else:
                counts_by_subj: dict[str, int] = {}
                for (_, s_id), grp in staged.items():
                    counts_by_subj[s_id] = counts_by_subj.get(s_id, 0) + len(grp)
                evict_subj = max(counts_by_subj, key=lambda s: counts_by_subj[s])
            yield from _flush_subject(evict_subj)
            last_usubjid = subj_order[-1] if subj_order else None

    remaining_subjects = sorted({s for (_, s) in staged}) if ungrouped else list(subj_order)
    for rem_subj in remaining_subjects:
        yield from _flush_subject(rem_subj)


def _resolve_sdtm_pipeline(
    study_key: str,
    pipeline: Any = _UNSET_PIPELINE,
    enrichment_pipeline: Any = _UNSET_PIPELINE,
) -> Any | None:
    """Resolve an EnrichmentPipeline from explicit args or StudyConfiguration."""
    if enrichment_pipeline is not _UNSET_PIPELINE:
        return enrichment_pipeline
    if pipeline is not _UNSET_PIPELINE:
        return pipeline
    from imednet.integrations.enrichment import resolve_enrichment_pipeline

    return resolve_enrichment_pipeline(study_key)


def get_sdtm_domain_variables(domain: str) -> list[str]:
    """Return standard uppercase SDTM variable list for ``domain``."""
    domain_clean = resolve_sdtm_domain(domain)
    spec = SDTM_DOMAIN_SPECS.get(domain_clean)
    if spec is not None:
        return list(spec["variables"])
    if domain_clean == "DM":
        return ["STUDYID", "DOMAIN", "USUBJID"]
    return ["STUDYID", "DOMAIN", "USUBJID", f"{domain_clean}SEQ"]


def transform_records_to_sdtm_rows(
    records: Iterable[Any],
    domain: str,
    study_key: str,
    *,
    include_deleted: bool = False,
    mask_phi: bool = True,
    seq_counters: MutableMapping[str, int] | TransactionalSdtmSequencer | None = None,
    phi_keys: Iterable[str] | None = None,
    registry: SensitivityRegistry | None = None,
    pipeline: Any = _UNSET_PIPELINE,
    enrichment_pipeline: Any = _UNSET_PIPELINE,
) -> list[dict[str, Any]]:
    """Transform clinical records into normalized CDISC SDTM row dictionaries.

    Applies ``SensitivityRegistry`` and custom ``phi_keys`` / ``EnrichmentPipeline``
    masking *before* constructing ``USUBJID`` or mapping SDTM domain variables,
    redacts masked ``BRTHDTC`` values to 4-digit ISO 8601 years or ``None``, sorts
    records per subject by ``(--DTC, topic_vars, recordId)``, consolidates ``DM``
    to one row per ``USUBJID`` without ``DMSEQ``, and maintains per-``(STUDYID, DOMAIN, USUBJID)``
    sequence counters across streaming batches when ``seq_counters`` is supplied.
    """
    resolved_pipeline = _resolve_sdtm_pipeline(study_key, pipeline, enrichment_pipeline)
    normalizer = _SdtmNormalizer(
        domain=domain,
        study_key=study_key,
        include_deleted=include_deleted,
        mask_phi=mask_phi,
        pipeline=resolved_pipeline,
        phi_keys=phi_keys,
        registry=registry,
    )
    counters: MutableMapping[str, int] | TransactionalSdtmSequencer = (
        seq_counters if seq_counters is not None else {}
    )

    grouped: dict[str, list[_PreparedSdtmRow]] = {}
    for idx, rec in enumerate(records):
        prepared = normalizer.prepare_record(rec, idx)
        if prepared is not None:
            grouped.setdefault(prepared.usubjid, []).append(prepared)

    rows: list[dict[str, Any]] = []
    for usubjid in sorted(grouped):
        rows.extend(normalizer.finalize_subject_group(grouped[usubjid], counters))
    return rows


def transform_records_to_sdtm(
    records: Iterable[Any],
    domain: str,
    study_key: str,
    *,
    include_deleted: bool = False,
    mask_phi: bool = True,
    seq_counters: MutableMapping[str, int] | TransactionalSdtmSequencer | None = None,
    phi_keys: Iterable[str] | None = None,
    registry: SensitivityRegistry | None = None,
    pipeline: Any = _UNSET_PIPELINE,
    enrichment_pipeline: Any = _UNSET_PIPELINE,
) -> pd.DataFrame:
    """Transform clinical records into a normalized CDISC SDTM domain DataFrame.

    Maps subject keys to USUBJID, study keys to STUDYID, sorts records per
    subject by canonical ``--DTC`` timestamp, domain topic variable, and record
    identifier, sequences records per ``(STUDYID, DOMAIN, USUBJID)`` (consolidating
    ``DM`` without ``DMSEQ``), enforces PHI redaction and ``StudyConfiguration``
    rules, and converts event timestamps to standard ISO 8601 DTC string formats.
    """
    if pd is None:
        raise ImportError(
            "pandas is required for SDTM transformations. Install with "
            "'pip install \"imednet[export]\"'."
        )

    resolved_pipeline = _resolve_sdtm_pipeline(study_key, pipeline, enrichment_pipeline)
    normalizer = _SdtmNormalizer(
        domain=domain,
        study_key=study_key,
        include_deleted=include_deleted,
        mask_phi=mask_phi,
        pipeline=resolved_pipeline,
        phi_keys=phi_keys,
        registry=registry,
    )
    counters: MutableMapping[str, int] | TransactionalSdtmSequencer = (
        seq_counters if seq_counters is not None else {}
    )

    grouped: dict[str, list[_PreparedSdtmRow]] = {}
    for idx, rec in enumerate(records):
        prepared = normalizer.prepare_record(rec, idx)
        if prepared is not None:
            grouped.setdefault(prepared.usubjid, []).append(prepared)

    rows: list[dict[str, Any]] = []
    for usubjid in sorted(grouped):
        rows.extend(normalizer.finalize_subject_group(grouped[usubjid], counters))

    return normalizer.rows_to_dataframe(rows)


__all__ = [
    "SDTM_DOMAIN_SPECS",
    "SDTM_SEQUENCER_METADATA_KEY",
    "VALID_NAMING_CONVENTIONS",
    "_SDTM_RECORDS_SORT",
    "_UNSET_PIPELINE",
    "TransactionalSdtmSequencer",
    "_SdtmNormalizer",
    "_collapse_alnum_key",
    "_extract_stream_usubjid_and_id",
    "_iter_sorted_sdtm_frames",
    "format_iso8601_dtc",
    "format_variable_name",
    "get_sdtm_domain_variables",
    "is_active_clinical_record",
    "iter_staged_sdtm_records",
    "normalize_naming_convention",
    "resolve_sdtm_domain",
    "transform_records_to_sdtm",
    "transform_records_to_sdtm_rows",
]
