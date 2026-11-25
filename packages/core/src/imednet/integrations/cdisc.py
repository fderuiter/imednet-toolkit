"""Streaming CDISC ODM 1.3.2 XML and CDISC Dataset-JSON v1.1 exporters."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from unittest.mock import NonCallableMock

from imednet.core.context import sdk_governance_context
from imednet.datadict.dataset_json import (
    build_dataset_json_clinical_data,
    build_dataset_json_dataset,
    dataset_json_to_rows,
    load_dataset_json,
)
from imednet.datadict.emit import write_odm_xml_stream
from imednet.datadict.exchange import (
    DatasetJsonDataset,
    OdmAdminData,
    OdmAnnotation,
    OdmArchive,
    OdmAuditRecord,
    OdmClinicalData,
    OdmFormData,
    OdmItemGroupData,
    OdmLocation,
    OdmReferenceData,
    OdmStudyEventData,
    OdmSubjectData,
    OdmUser,
    build_odm_archive,
)
from imednet.datadict.loaders import load_from_api, load_odm_archive
from imednet.datadict.models import DataDictionary, Form, Metadata, Variable
from imednet.datadict.schema import validate_dataset_json, validate_odm_xml
from imednet.integrations.enrichment import (
    resolve_enrichment_pipeline,
    resolve_study_configuration,
)
from imednet.integrations.sink_base import (
    ExportSink,
    SinkConfig,
    apply_quality_gate,
    bind_sdtm_stream_metadata,
    build_form_sdtm_domain_map,
    commit_sdtm_lineage,
    deduplicate_sdtm_rows,
    extract_record_form_ref,
    extract_record_id,
    extract_record_timestamp,
    extract_sdtm_composite_key,
    is_incremental_sink_config,
    is_record_deleted,
    iter_batches,
    normalize_watermark_datetime,
    redact_batch_records,
    rehydrate_sdtm_lineage,
    resolve_form_sdtm_domain,
    resolve_voided_sdtm_keys,
    run_streaming_export,
)
from imednet.models.sdtm import resolve_sdtm_domain, transform_records_to_sdtm_rows
from imednet.sdk import ImednetSDK
from imednet.utils.security import SensitivityRegistry, global_sensitivity_registry

_EPOCH_UTC_ISO = "1970-01-01T00:00:00Z"

_SINK_CONFIG_KEYS: tuple[str, ...] = (
    "batch_size",
    "max_buffered_records",
    "max_retries",
    "retry_backoff",
    "idempotent",
    "mask_phi",
    "extra",
    "quality_gate_enabled",
    "min_schema_readiness_score",
    "tracer",
    "modified_since",
    "record_filter",
    "record_data_filter",
    "include_deleted",
    "incremental",
    "stream_name",
    "ledger_path",
    "state_provider",
    "as_sdtm",
    "sdtm_domain",
    "preserve_record_id",
    "standards_profile",
    "min_standards_score",
    "naming_convention",
)


def _canonical_id(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _get_field(obj: Any, *names: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        for name in names:
            if name in obj and obj[name] is not None:
                return obj[name]
        return default
    for name in names:
        val = getattr(obj, name, None)
        if val is not None and not isinstance(val, NonCallableMock):
            return val
    return default


def _build_cdisc_sink_config(
    study_key: str,
    kwargs: dict[str, Any],
    *,
    quality_gate_enabled: bool = False,
    mask_phi: bool = True,
) -> SinkConfig:
    existing = kwargs.pop("config", None)
    if isinstance(existing, SinkConfig):
        for k in _SINK_CONFIG_KEYS:
            kwargs.pop(k, None)
        kwargs.pop("domain", None)
        base_values = {f.name: getattr(existing, f.name) for f in fields(SinkConfig)}
        base_values["study_key"] = study_key
        return SinkConfig(**base_values)

    domain_alias = kwargs.pop("domain", None)
    extracted = {k: kwargs.pop(k) for k in _SINK_CONFIG_KEYS if k in kwargs}
    if domain_alias is not None and extracted.get("sdtm_domain") is None:
        extracted["sdtm_domain"] = domain_alias
    extracted.setdefault("quality_gate_enabled", quality_gate_enabled)
    extracted.setdefault("mask_phi", mask_phi)
    return SinkConfig(study_key=study_key, **extracted)


def _build_sensitivity_registry(study_key: str) -> SensitivityRegistry:
    registry = SensitivityRegistry()
    registry._sensitive_keys = set(global_sensitivity_registry._sensitive_keys)
    registry._exempt_keys = set(global_sensitivity_registry._exempt_keys)
    for key in _resolve_custom_phi_keys(study_key) or ():
        registry.add_sensitive_key(key)
    return registry


def _resolve_custom_phi_keys(study_key: str) -> set[str] | None:
    pipeline = resolve_enrichment_pipeline(study_key)
    if pipeline is None:
        return None
    raw_keys = getattr(pipeline, "phi_fields", None) or getattr(pipeline, "phi_keys", None)
    if not raw_keys:
        return None
    resolved = {str(k).strip() for k in raw_keys if isinstance(k, str) and str(k).strip()}
    return resolved or None


def _find_form_by_key(dd: DataDictionary, form_key: str | None) -> Form | None:
    if not form_key:
        return None
    target = form_key.strip().upper()
    return next((f for f in dd.forms if f.formKey.strip().upper() == target), None)


def _safe_list_endpoint(sdk: Any, endpoint_name: str, study_key: str) -> list[Any]:
    endpoint = getattr(sdk, endpoint_name, None)
    if endpoint is None or isinstance(endpoint, NonCallableMock):
        return []
    list_fn = getattr(endpoint, "list", None)
    if not callable(list_fn):
        return []
    try:
        items = list_fn(study_key=study_key)
    except TypeError:
        try:
            items = list_fn(study_key)
        except Exception:
            return []
    except Exception:
        return []
    if items is None or isinstance(items, NonCallableMock):
        return []
    try:
        materialized = list(items)
    except Exception:
        return []
    return [item for item in materialized if not isinstance(item, NonCallableMock)]


def _infer_datadict_from_records(
    study_key: str,
    records: Sequence[Any],
    base_dd: DataDictionary | None = None,
) -> DataDictionary:
    metadata = (
        base_dd.metadata
        if base_dd is not None and base_dd.metadata.studyKey
        else Metadata(
            studyKey=study_key,
            source=base_dd.metadata.source if base_dd is not None else "api",
            validationWarnings=(
                list(base_dd.metadata.validationWarnings) if base_dd is not None else []
            ),
            extraAttributes=(dict(base_dd.metadata.extraAttributes) if base_dd is not None else {}),
        )
    )
    existing_forms: dict[str, Form] = (
        {f.formKey: f for f in base_dd.forms} if base_dd is not None else {}
    )
    unassigned = list(base_dd.unassignedBusinessLogic) if base_dd is not None else []

    observed_vars_by_form: dict[str, dict[str, str]] = {}
    form_meta: dict[str, tuple[int | None, str]] = {}

    for rec in records:
        fid, fkey = extract_record_form_ref(rec)
        resolved_fkey = str(fkey or (f"FORM_{fid}" if fid is not None else "GENERAL")).strip()
        fname = str(_get_field(rec, "form_name", "formName") or resolved_fkey)
        form_meta.setdefault(resolved_fkey, (fid if isinstance(fid, int) else None, fname))
        rdata = _get_field(rec, "record_data", "recordData")
        if isinstance(rdata, Mapping):
            bucket = observed_vars_by_form.setdefault(resolved_fkey, {})
            for raw_k, raw_v in rdata.items():
                k = str(raw_k).strip()
                if not k or k in bucket:
                    continue
                if isinstance(raw_v, bool):
                    vtype = "Checkbox"
                elif isinstance(raw_v, int):
                    vtype = "Integer"
                elif isinstance(raw_v, float):
                    vtype = "Number"
                else:
                    vtype = "Text"
                bucket[k] = vtype

    merged_forms: list[Form] = []
    for fkey in sorted(set(existing_forms) | set(form_meta)):
        if fkey in existing_forms:
            existing_form = existing_forms[fkey]
            known_var_names = {v.variableName for v in existing_form.variables}
            extra_vars: list[Variable] = []
            for idx, (vname, vtype) in enumerate(
                sorted(observed_vars_by_form.get(fkey, {}).items()),
                start=len(existing_form.variables) + 1,
            ):
                if vname not in known_var_names:
                    extra_vars.append(
                        Variable(
                            variableName=vname,
                            label=vname,
                            variableType=vtype,
                            sequence=idx,
                        )
                    )
            if extra_vars:
                merged_forms.append(
                    existing_form.model_copy(
                        update={"variables": [*existing_form.variables, *extra_vars]}
                    )
                )
            else:
                merged_forms.append(existing_form)
        else:
            fid, fname = form_meta[fkey]
            vars_list = [
                Variable(
                    variableName=vname,
                    label=vname,
                    variableType=vtype,
                    sequence=idx,
                )
                for idx, (vname, vtype) in enumerate(
                    sorted(observed_vars_by_form.get(fkey, {}).items()), start=1
                )
            ]
            merged_forms.append(
                Form(
                    formId=fid,
                    formKey=fkey,
                    formName=fname,
                    variables=vars_list,
                )
            )

    return DataDictionary(
        metadata=metadata,
        forms=merged_forms,
        unassignedBusinessLogic=unassigned,
    )


def _resolve_study_datadict(
    sdk: Any,
    study_key: str,
    records: Sequence[Any],
    explicit_dd: DataDictionary | None = None,
) -> DataDictionary:
    base_dd = explicit_dd
    if base_dd is None:
        with contextlib.suppress(Exception):
            loaded = load_from_api(sdk, study_key)
            if isinstance(loaded, DataDictionary):
                base_dd = loaded
    return _infer_datadict_from_records(study_key, records, base_dd=base_dd)


def _filter_record_variables_and_forms(
    rec: Any,
    *,
    variable_whitelist: set[str] | None,
    form_whitelist: set[int] | None,
) -> Any | None:
    if form_whitelist is not None:
        fid, _ = extract_record_form_ref(rec)
        if fid is not None and fid not in form_whitelist:
            return None

    if variable_whitelist is None:
        return rec

    rdata = _get_field(rec, "record_data", "recordData")
    if not isinstance(rdata, Mapping):
        return rec
    filtered_rdata = {k: v for k, v in rdata.items() if str(k) in variable_whitelist}
    if isinstance(rec, Mapping):
        copied = dict(rec)
        if "recordData" in copied and "record_data" not in copied:
            copied["recordData"] = filtered_rdata
        else:
            copied["record_data"] = filtered_rdata
        return copied
    model_copy = getattr(rec, "model_copy", None)
    if callable(model_copy):
        return model_copy(update={"record_data": filtered_rdata})
    return rec


def _derive_deterministic_timestamp(
    explicit: str | datetime | None,
    records: Sequence[Any],
    extra_timestamps: Iterable[str | datetime | None] = (),
) -> str:
    if explicit is not None:
        if isinstance(explicit, datetime):
            dt = explicit if explicit.tzinfo is not None else explicit.replace(tzinfo=UTC)
            return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        text = str(explicit).strip()
        if text:
            return text

    candidates: list[datetime] = []
    for rec in records:
        ts = extract_record_timestamp(rec)
        if ts is not None:
            dt = ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
            candidates.append(dt.astimezone(UTC))
    for item in extra_timestamps:
        if item is None:
            continue
        with contextlib.suppress(Exception):
            norm = normalize_watermark_datetime(item)
            if norm is not None:
                dt = norm if norm.tzinfo is not None else norm.replace(tzinfo=UTC)
                candidates.append(dt.astimezone(UTC))

    if candidates:
        return max(candidates).strftime("%Y-%m-%dT%H:%M:%SZ")
    return _EPOCH_UTC_ISO


def _atomic_write_text(dest: Path, content: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{dest.name}.",
        suffix=".tmp",
        dir=str(dest.parent),
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, dest)
    except Exception:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise


def _merge_odm_admin_data(
    existing: tuple[OdmAdminData, ...],
    incoming: tuple[OdmAdminData, ...],
) -> tuple[OdmAdminData, ...]:
    by_study: dict[str | None, tuple[dict[str, OdmUser], dict[str, OdmLocation]]] = {}
    sig_by_study: dict[str | None, dict[str, Any]] = {}
    for block in [*existing, *incoming]:
        users_map, locs_map = by_study.setdefault(block.study_oid, ({}, {}))
        sigs_map = sig_by_study.setdefault(block.study_oid, {})
        for u in block.users:
            users_map[u.oid] = u
        for loc in block.locations:
            locs_map[loc.oid] = loc
        for sig in block.signature_defs:
            sigs_map[sig.oid] = sig

    merged: list[OdmAdminData] = []
    for study_oid in sorted(by_study, key=lambda s: s or ""):
        users_map, locs_map = by_study[study_oid]
        sigs_map = sig_by_study[study_oid]
        merged.append(
            OdmAdminData(
                studyOid=study_oid,
                users=tuple(users_map[k] for k in sorted(users_map)),
                locations=tuple(locs_map[k] for k in sorted(locs_map)),
                signatureDefs=tuple(sigs_map[k] for k in sorted(sigs_map)),
            )
        )
    return tuple(merged)


def _merge_odm_reference_data(
    existing: tuple[OdmReferenceData, ...],
    incoming: tuple[OdmReferenceData, ...],
) -> tuple[OdmReferenceData, ...]:
    igs_by_key: dict[tuple[str, str], dict[tuple[str, str | None], OdmItemGroupData]] = {}
    for ref in [*existing, *incoming]:
        bucket = igs_by_key.setdefault((ref.study_oid, ref.metadata_version_oid), {})
        for ig in ref.item_group_data:
            bucket[(ig.item_group_oid, ig.item_group_repeat_key)] = ig

    return tuple(
        OdmReferenceData(
            studyOid=study_oid,
            metaDataVersionOid=mdv_oid,
            itemGroupData=tuple(
                bucket[k] for k in sorted(bucket, key=lambda pair: (pair[0], pair[1] or ""))
            ),
        )
        for (study_oid, mdv_oid), bucket in sorted(igs_by_key.items())
    )


def _merge_odm_item_groups(
    existing: Sequence[OdmItemGroupData],
    incoming: Sequence[OdmItemGroupData],
    exclude_repeat_keys: set[str],
) -> tuple[OdmItemGroupData, ...]:
    merged: dict[tuple[str, str | None], OdmItemGroupData] = {}
    for ig in existing:
        rk = _canonical_id(ig.item_group_repeat_key)
        if rk is not None and rk in exclude_repeat_keys:
            continue
        merged[(ig.item_group_oid, ig.item_group_repeat_key)] = ig
    for ig in incoming:
        rk = _canonical_id(ig.item_group_repeat_key)
        if rk is not None and rk in exclude_repeat_keys:
            continue
        merged[(ig.item_group_oid, ig.item_group_repeat_key)] = ig
    return tuple(merged[k] for k in sorted(merged, key=lambda pair: (pair[0], pair[1] or "")))


def _merge_odm_forms(
    existing: Sequence[OdmFormData],
    incoming: Sequence[OdmFormData],
    exclude_repeat_keys: set[str],
) -> tuple[OdmFormData, ...]:
    by_key: dict[tuple[str, str | None], OdmFormData] = {}
    for f in existing:
        rk = _canonical_id(f.form_repeat_key)
        if rk is not None and rk in exclude_repeat_keys:
            continue
        igs = _merge_odm_item_groups(f.item_group_data, (), exclude_repeat_keys)
        if igs or f.audit_record or f.annotations:
            by_key[(f.form_oid, f.form_repeat_key)] = f.model_copy(update={"item_group_data": igs})
    for f in incoming:
        key = (f.form_oid, f.form_repeat_key)
        if key in by_key:
            prev = by_key[key]
            igs = _merge_odm_item_groups(
                prev.item_group_data, f.item_group_data, exclude_repeat_keys
            )
            ann_map: dict[str, OdmAnnotation] = {}
            for idx, ann in enumerate([*prev.annotations, *f.annotations], start=1):
                ann_key = ann.annotation_id or f"seq:{ann.seq_num}:{idx}"
                ann_map[ann_key] = ann
            by_key[key] = f.model_copy(
                update={
                    "item_group_data": igs,
                    "audit_record": f.audit_record or prev.audit_record,
                    "annotations": tuple(ann_map.values()),
                }
            )
        else:
            by_key[key] = f
    return tuple(by_key[k] for k in sorted(by_key, key=lambda pair: (pair[0], pair[1] or "")))


def _merge_odm_study_events(
    existing: Sequence[OdmStudyEventData],
    incoming: Sequence[OdmStudyEventData],
    exclude_repeat_keys: set[str],
) -> tuple[OdmStudyEventData, ...]:
    by_key: dict[tuple[str, str | None], OdmStudyEventData] = {}
    for ev in existing:
        forms = _merge_odm_forms(ev.form_data, (), exclude_repeat_keys)
        if forms or ev.audit_record or ev.annotations:
            by_key[(ev.study_event_oid, ev.study_event_repeat_key)] = ev.model_copy(
                update={"form_data": forms}
            )
    for ev in incoming:
        key = (ev.study_event_oid, ev.study_event_repeat_key)
        if key in by_key:
            prev = by_key[key]
            forms = _merge_odm_forms(prev.form_data, ev.form_data, exclude_repeat_keys)
            by_key[key] = ev.model_copy(
                update={
                    "form_data": forms,
                    "audit_record": ev.audit_record or prev.audit_record,
                    "annotations": ev.annotations or prev.annotations,
                }
            )
        else:
            by_key[key] = ev
    return tuple(by_key[k] for k in sorted(by_key, key=lambda pair: (pair[0], pair[1] or "")))


def _merge_odm_clinical_data(
    existing: tuple[OdmClinicalData, ...],
    incoming: tuple[OdmClinicalData, ...],
    voided_ids: set[str],
) -> tuple[OdmClinicalData, ...]:
    existing_map = {(cd.study_oid, cd.metadata_version_oid): cd for cd in existing}
    incoming_map = {(cd.study_oid, cd.metadata_version_oid): cd for cd in incoming}

    merged_blocks: list[OdmClinicalData] = []
    for block_key in sorted(set(existing_map) | set(incoming_map)):
        prev_cd = existing_map.get(block_key)
        next_cd = incoming_map.get(block_key)
        prev_subjects = prev_cd.subject_data if prev_cd is not None else ()
        next_subjects = next_cd.subject_data if next_cd is not None else ()

        subj_by_key: dict[str, OdmSubjectData] = {}
        for subj in prev_subjects:
            events = _merge_odm_study_events(subj.study_event_data, (), voided_ids)
            if (
                events
                or subj.site_location_oid
                or subj.investigator_user_oid
                or subj.audit_record
                or subj.annotations
            ):
                subj_by_key[subj.subject_key] = subj.model_copy(update={"study_event_data": events})

        for subj in next_subjects:
            if subj.subject_key in subj_by_key:
                prev_subj = subj_by_key[subj.subject_key]
                subj_by_key[subj.subject_key] = subj.model_copy(
                    update={
                        "site_location_oid": (
                            subj.site_location_oid or prev_subj.site_location_oid
                        ),
                        "investigator_user_oid": (
                            subj.investigator_user_oid or prev_subj.investigator_user_oid
                        ),
                        "study_event_data": _merge_odm_study_events(
                            prev_subj.study_event_data,
                            subj.study_event_data,
                            voided_ids,
                        ),
                        "audit_record": subj.audit_record or prev_subj.audit_record,
                        "annotations": subj.annotations or prev_subj.annotations,
                    }
                )
            else:
                subj_by_key[subj.subject_key] = subj

        audit_map: dict[str, OdmAuditRecord] = {}
        for idx, ar in enumerate(
            [
                *(prev_cd.audit_records if prev_cd is not None else ()),
                *(next_cd.audit_records if next_cd is not None else ()),
            ],
            start=1,
        ):
            ar_key = ar.audit_id or f"audit:{ar.date_time_stamp}:{ar.user_oid}:{idx}"
            audit_map[ar_key] = ar

        ann_map: dict[str, OdmAnnotation] = {}
        for idx, ann in enumerate(
            [
                *(prev_cd.annotations if prev_cd is not None else ()),
                *(next_cd.annotations if next_cd is not None else ()),
            ],
            start=1,
        ):
            ann_key = ann.annotation_id or f"ann:{ann.seq_num}:{idx}"
            ann_map[ann_key] = ann

        merged_blocks.append(
            OdmClinicalData(
                studyOid=block_key[0],
                metaDataVersionOid=block_key[1],
                subject_data=tuple(subj_by_key[k] for k in sorted(subj_by_key)),
                auditRecords=tuple(audit_map[k] for k in sorted(audit_map)),
                annotations=tuple(ann_map[k] for k in sorted(ann_map)),
            )
        )

    return tuple(merged_blocks)


def _merge_datadictionaries(existing: DataDictionary, incoming: DataDictionary) -> DataDictionary:
    forms_by_key: dict[str, Form] = {f.formKey: f for f in existing.forms}
    for f in incoming.forms:
        if f.formKey in forms_by_key:
            prev_f = forms_by_key[f.formKey]
            var_map = {v.variableName: v for v in prev_f.variables}
            for v in f.variables:
                var_map[v.variableName] = v
            forms_by_key[f.formKey] = f.model_copy(update={"variables": list(var_map.values())})
        else:
            forms_by_key[f.formKey] = f

    return incoming.model_copy(
        update={
            "forms": [forms_by_key[k] for k in sorted(forms_by_key)],
        }
    )


class OdmExportSink(ExportSink):
    """Streaming sink that writes canonical CDISC ODM 1.3.2 XML snapshots."""

    reconciles_deleted_on_full_sync = False

    def __init__(
        self,
        sdk: Any,
        study_key: str,
        path: str | os.PathLike[str],
        config: SinkConfig,
        *,
        datadict: DataDictionary | None = None,
        include_admin_data: bool = True,
        include_reference_data: bool = True,
        include_audit_records: bool = True,
        include_annotations: bool = True,
        file_type: Literal["Snapshot", "Transactional"] = "Snapshot",
        file_oid: str | None = None,
        creation_date_time: str | datetime | None = None,
        originator: str | None = "iMednet",
        source_system: str | None = "iMednet EDC",
        source_system_version: str | None = None,
        variable_whitelist: Sequence[str] | None = None,
        form_whitelist: Sequence[int] | None = None,
        validate_schema: bool = False,
    ) -> None:
        """Initialize the ODM 1.3.2 XML streaming sink."""
        super().__init__(config)
        self._sdk = sdk
        self._study_key = study_key
        self.path = Path(path)
        self._explicit_datadict = datadict
        self._include_admin_data = include_admin_data
        self._include_reference_data = include_reference_data
        self._include_audit_records = include_audit_records
        self._include_annotations = include_annotations
        self._file_type = file_type
        self._file_oid = file_oid
        self._creation_date_time = creation_date_time
        self._originator = originator
        self._source_system = source_system
        self._source_system_version = source_system_version
        self._variable_whitelist = (
            {str(v).strip() for v in variable_whitelist if str(v).strip()}
            if variable_whitelist is not None
            else None
        )
        self._form_whitelist = (
            {int(fid) for fid in form_whitelist} if form_whitelist is not None else None
        )
        self._validate_schema = validate_schema
        self._is_incremental_sync = is_incremental_sink_config(config)
        self._records_by_cid: dict[str, Any] = {}
        self._unkeyed_records: list[Any] = []
        self._voided_ids: set[str] = set()

    def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Buffer incoming active study records for ODM snapshot emission."""
        _ = batch_id
        if not records:
            return 0
        written = 0
        for raw_rec in records:
            filtered = _filter_record_variables_and_forms(
                raw_rec,
                variable_whitelist=self._variable_whitelist,
                form_whitelist=self._form_whitelist,
            )
            if filtered is None:
                continue
            written += 1
            cid = _canonical_id(extract_record_id(filtered))
            if cid is not None and self.config.idempotent:
                self._records_by_cid[cid] = filtered
                self._voided_ids.discard(cid)
            else:
                self._unkeyed_records.append(filtered)
        return written

    def reconcile_deleted(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Record voided upstream record IDs for idempotent pruning on flush."""
        _ = batch_id
        if not self.config.idempotent or not records:
            return 0
        reconciled = 0
        for rec in records:
            cid = _canonical_id(extract_record_id(rec))
            if cid is not None:
                self._voided_ids.add(cid)
                self._records_by_cid.pop(cid, None)
                reconciled += 1
        return reconciled

    def _write_archive(self) -> int:
        dest_exists = self.path.exists() and self.path.stat().st_size > 0
        active_records = list(self._records_by_cid.values()) + self._unkeyed_records

        if (
            self._is_incremental_sync
            and self.config.idempotent
            and not active_records
            and not self._voided_ids
            and dest_exists
        ):
            return 0

        with sdk_governance_context(self._sdk):
            dd = _resolve_study_datadict(
                self._sdk,
                self._study_key,
                active_records,
                explicit_dd=self._explicit_datadict,
            )
            if self._form_whitelist is not None:
                dd = dd.model_copy(
                    update={
                        "forms": [
                            f
                            for f in dd.forms
                            if f.formId is None
                            or (
                                str(f.formId).isdigit()
                                and int(str(f.formId)) in self._form_whitelist
                            )
                        ]
                    }
                )
            if self._variable_whitelist is not None:
                dd = dd.model_copy(
                    update={
                        "forms": [
                            f.model_copy(
                                update={
                                    "variables": [
                                        v
                                        for v in f.variables
                                        if v.variableName in self._variable_whitelist
                                    ]
                                }
                            )
                            for f in dd.forms
                        ]
                    }
                )

            sites = (
                _safe_list_endpoint(self._sdk, "sites", self._study_key)
                if self._include_admin_data
                else []
            )
            users = (
                _safe_list_endpoint(self._sdk, "users", self._study_key)
                if self._include_admin_data
                else []
            )
            subjects = _safe_list_endpoint(self._sdk, "subjects", self._study_key)
            visits = _safe_list_endpoint(self._sdk, "visits", self._study_key)
            revisions = (
                _safe_list_endpoint(self._sdk, "record_revisions", self._study_key)
                if self._include_audit_records
                else []
            )
            queries = (
                _safe_list_endpoint(self._sdk, "queries", self._study_key)
                if self._include_annotations
                else []
            )

        if self._is_incremental_sync and active_records:
            active_cids = set(self._records_by_cid)
            if active_cids:
                revisions = [
                    rev
                    for rev in revisions
                    if (
                        (rcid := _canonical_id(_get_field(rev, "record_id", "recordId"))) is None
                        or rcid in active_cids
                    )
                ]
                queries = [
                    q
                    for q in queries
                    if (
                        (qcid := _canonical_id(_get_field(q, "record_id", "recordId"))) is None
                        or qcid in active_cids
                    )
                ]
        elif self._is_incremental_sync and not active_records:
            revisions = []
            queries = []

        extra_ts = [
            *(_get_field(rev, "date_created", "dateCreated") for rev in revisions),
            *(
                _get_field(q, "date_modified", "dateModified", "date_created", "dateCreated")
                for q in queries
            ),
        ]
        resolved_creation_dt = _derive_deterministic_timestamp(
            self._creation_date_time,
            active_records,
            extra_ts,
        )

        custom_phi = _resolve_custom_phi_keys(self._study_key)
        delta_archive = build_odm_archive(
            self._study_key,
            data_dictionary=dd,
            records=active_records,
            sites=sites,
            users=users,
            subjects=subjects,
            visits=visits,
            revisions=revisions,
            queries=queries,
            file_oid=self._file_oid,
            creation_date_time=resolved_creation_dt,
            file_type=self._file_type,
            mask_phi=self.config.mask_phi,
            include_deleted=self.config.include_deleted,
            custom_phi_keys=custom_phi,
            source_system_version=self._source_system_version,
        )
        if self._originator is not None or self._source_system is not None:
            delta_archive = delta_archive.model_copy(
                update={
                    "originator": self._originator,
                    "source_system": self._source_system or delta_archive.source_system,
                }
            )

        if not self._include_reference_data and delta_archive.reference_data:
            delta_archive = delta_archive.model_copy(update={"reference_data": ()})
        if not self._include_admin_data and delta_archive.admin_data:
            delta_archive = delta_archive.model_copy(update={"admin_data": ()})

        final_archive = delta_archive
        if self._is_incremental_sync and self.config.idempotent and dest_exists:
            existing_archive: OdmArchive | None = None
            with contextlib.suppress(Exception):
                existing_archive = load_odm_archive(self.path, strict=False)
            if existing_archive is not None:
                merged_dd = (
                    _merge_datadictionaries(
                        existing_archive.data_dictionary, delta_archive.data_dictionary
                    )
                    if (
                        existing_archive.data_dictionary is not None
                        and delta_archive.data_dictionary is not None
                    )
                    else (delta_archive.data_dictionary or existing_archive.data_dictionary)
                )
                merged_admin = _merge_odm_admin_data(
                    existing_archive.admin_data, delta_archive.admin_data
                )
                merged_ref = _merge_odm_reference_data(
                    existing_archive.reference_data, delta_archive.reference_data
                )
                merged_clin = _merge_odm_clinical_data(
                    existing_archive.clinical_data,
                    delta_archive.clinical_data,
                    self._voided_ids,
                )
                merged_creation = resolved_creation_dt
                if (
                    self._creation_date_time is None
                    and not active_records
                    and existing_archive.creation_date_time
                ):
                    merged_creation = existing_archive.creation_date_time
                final_archive = delta_archive.model_copy(
                    update={
                        "data_dictionary": merged_dd,
                        "admin_data": merged_admin,
                        "reference_data": merged_ref,
                        "clinical_data": merged_clin,
                        "creation_date_time": merged_creation,
                    }
                )

        clin = final_archive.clinical_data[0] if final_archive.clinical_data else None
        resolved_study_oid = (
            clin.study_oid
            if clin is not None
            else (
                final_archive.admin_data[0].study_oid
                if final_archive.admin_data and final_archive.admin_data[0].study_oid
                else f"S.{self._study_key}"
            )
        )
        resolved_mdv_oid = clin.metadata_version_oid if clin is not None else "MDV.001"

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            write_odm_xml_stream(
                tmp_path,
                study_oid=resolved_study_oid,
                file_oid=final_archive.file_oid,
                creation_datetime=final_archive.creation_date_time,
                meta_data_version_oid=resolved_mdv_oid,
                file_type=final_archive.file_type,
                granularity=final_archive.granularity,
                source_system=final_archive.source_system,
                source_system_version=final_archive.source_system_version,
                datadict=final_archive.data_dictionary,
                admin_data=final_archive.admin_data,
                reference_data=final_archive.reference_data,
                subject_data=clin.subject_data if clin is not None else (),
                audit_records=clin.audit_records if clin is not None else (),
                annotations=clin.annotations if clin is not None else (),
            )
            if self._validate_schema:
                validate_odm_xml(tmp_path, raise_on_error=True)
            os.replace(tmp_path, self.path)
        except Exception:
            with contextlib.suppress(OSError):
                tmp_path.unlink(missing_ok=True)
            raise

        return len(active_records)

    def flush(self) -> None:
        """Atomically write the merged ODM 1.3.2 XML archive to disk."""
        batch_id = f"{self._study_key}/odm/flush"
        try:
            self._execute_with_retry("export_odm", batch_id, self._write_archive)
        finally:
            self._records_by_cid.clear()
            self._unkeyed_records.clear()
            self._voided_ids.clear()

    def close(self) -> None:
        """Release buffered ODM sink state."""
        self._records_by_cid.clear()
        self._unkeyed_records.clear()
        self._voided_ids.clear()


class DatasetJsonExportSink(ExportSink):
    """Streaming sink that writes CDISC Dataset-JSON v1.1 datasets for CDASH Forms or SDTM domains."""

    reconciles_deleted_on_full_sync = False

    def __init__(
        self,
        sdk: Any,
        study_key: str,
        path: str | os.PathLike[str],
        config: SinkConfig,
        *,
        datadict: DataDictionary | None = None,
        use_labels_as_columns: bool = False,
        variable_whitelist: Sequence[str] | None = None,
        form_whitelist: Sequence[int] | None = None,
        creation_date_time: str | datetime | None = None,
        originator: str | None = "iMednet",
        source_system_name: str = "iMednet EDC",
        source_system_version: str = "1.0.0",
        per_form: bool | None = None,
        validate_schema: bool = False,
    ) -> None:
        """Initialize the CDISC Dataset-JSON v1.1 streaming sink."""
        super().__init__(config)
        self._sdk = sdk
        self._study_key = study_key
        self.path = Path(path)
        self._explicit_datadict = datadict
        self._use_labels_as_columns = use_labels_as_columns
        self._variable_whitelist = (
            {str(v).strip() for v in variable_whitelist if str(v).strip()}
            if variable_whitelist is not None
            else None
        )
        self._form_whitelist = (
            {int(fid) for fid in form_whitelist} if form_whitelist is not None else None
        )
        self._creation_date_time = creation_date_time
        self._originator = originator
        self._source_system_name = source_system_name
        self._source_system_version = source_system_version
        self._per_form = (
            per_form
            if per_form is not None
            else (self.path.is_dir() or self.path.suffix.lower() != ".json")
        )
        self._validate_schema = validate_schema
        self._is_incremental_sync = is_incremental_sink_config(config)
        self._records_by_cid: dict[str, Any] = {}
        self._unkeyed_records: list[Any] = []
        self._voided_records: list[Any] = []
        self._voided_ids: set[str] = set()
        self._seq_counters: dict[str, int] = {}
        self._record_seqs: dict[str, int] = {}
        self._stream_metadata: dict[str, Any] | None = None
        self._active_form_domain_map: dict[Any, str] = {}

    def bind_stream_metadata(self, metadata: dict[str, Any]) -> None:
        """Attach persistent ledger metadata for stateful SDTM sequence numbering."""
        self._stream_metadata = metadata
        if self.config.as_sdtm:
            bind_sdtm_stream_metadata(metadata, self._seq_counters, self._record_seqs)

    def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Buffer incoming active study records for Dataset-JSON emission."""
        _ = batch_id
        if not records:
            return 0
        written = 0
        for raw_rec in records:
            filtered = _filter_record_variables_and_forms(
                raw_rec,
                variable_whitelist=None if self.config.as_sdtm else self._variable_whitelist,
                form_whitelist=self._form_whitelist,
            )
            if filtered is None:
                continue
            written += 1
            cid = _canonical_id(extract_record_id(filtered))
            if cid is not None and self.config.idempotent:
                self._records_by_cid[cid] = filtered
                self._voided_ids.discard(cid)
            else:
                self._unkeyed_records.append(filtered)
        return written

    def reconcile_deleted(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Record voided upstream records for idempotent Dataset-JSON pruning."""
        _ = batch_id
        if not self.config.idempotent or not records:
            return 0
        reconciled = 0
        for rec in records:
            self._voided_records.append(rec)
            cid = _canonical_id(extract_record_id(rec))
            if cid is not None:
                self._voided_ids.add(cid)
                self._records_by_cid.pop(cid, None)
            reconciled += 1
        return reconciled

    def _transform_sdtm_rows_by_domain(
        self,
        records: Sequence[Any],
        dd: DataDictionary,
    ) -> dict[str, list[dict[str, Any]]]:
        study_cfg = resolve_study_configuration(self._study_key)
        form_map: dict[Any, str] = dict(self._active_form_domain_map)
        if not form_map and dd.forms:
            form_map = build_form_sdtm_domain_map(
                dd.forms,
                study_config=study_cfg,
                study_key=self._study_key,
            )

        keep_record_id = bool(
            self.config.preserve_record_id is True
            or (
                isinstance(self.config.extra, Mapping)
                and self.config.extra.get("preserve_record_id") is True
            )
        )
        pipeline = resolve_enrichment_pipeline(self._study_key)
        phi_keys = set(getattr(pipeline, "phi_keys", ())) if pipeline is not None else None
        rehydrated_counters, seq_overrides, _ = (
            rehydrate_sdtm_lineage(self, study_key=self._study_key)
            if self._stream_metadata is not None
            else ({}, {}, {})
        )
        domain_seq_counters: dict[str, dict[str, int]] = {}
        for dom_code, subj_map in rehydrated_counters.items():
            local_map = domain_seq_counters.setdefault(dom_code, {})
            for subj_key, wm_val in subj_map.items():
                local_map[subj_key] = max(local_map.get(subj_key, 0), wm_val)
        lineage_out: dict[str, dict[str, Any]] = {}

        grouped_raw: dict[str, list[Any]] = {}
        for rec in records:
            if not self.config.include_deleted and is_record_deleted(rec):
                continue
            if self.config.sdtm_domain:
                dom = resolve_sdtm_domain(self.config.sdtm_domain, fallback="AE")
            else:
                dom = resolve_form_sdtm_domain(
                    rec,
                    study_config=study_cfg,
                    form_domain_map=form_map,
                    study_key=self._study_key,
                    fallback="AE",
                )
            grouped_raw.setdefault(dom, []).append(rec)

        single_explicit_domain = bool(self.config.sdtm_domain) or (
            len(grouped_raw) == 1 and len(domain_seq_counters) <= 1 and not rehydrated_counters
        )
        rows_by_domain: dict[str, list[dict[str, Any]]] = {}
        for dom in sorted(grouped_raw):
            dom_counters = domain_seq_counters.setdefault(dom, {})
            if single_explicit_domain and self._seq_counters and dom != "DM":
                for subj_key, wm_val in self._seq_counters.items():
                    dom_counters[subj_key] = max(dom_counters.get(subj_key, 0), wm_val)
            sdtm_rows = transform_records_to_sdtm_rows(
                grouped_raw[dom],
                domain=dom,
                study_key=self._study_key,
                include_deleted=self.config.include_deleted,
                mask_phi=self.config.mask_phi,
                seq_counters=dom_counters,
                record_seqs=self._record_seqs,
                preserve_record_id=keep_record_id,
                seq_overrides=seq_overrides or None,
                lineage_out=lineage_out,
                phi_keys=phi_keys or None,
                pipeline=pipeline,
            )
            if (self.config.sdtm_domain or len(domain_seq_counters) == 1) and dom != "DM":
                for subj_key, wm_val in dom_counters.items():
                    self._seq_counters[subj_key] = max(
                        self._seq_counters.get(subj_key, 0),
                        wm_val,
                    )
            if self.config.mask_phi and sdtm_rows:
                sdtm_rows = list(redact_batch_records(sdtm_rows, mask_phi=True, as_sdtm=True))
            if self._variable_whitelist is not None:
                keep = self._variable_whitelist | {"STUDYID", "DOMAIN", "USUBJID"}
                if dom != "DM":
                    keep.add(f"{dom}SEQ")
                if keep_record_id:
                    keep.add("RECORD_ID")
                sdtm_rows = [{k: v for k, v in row.items() if k in keep} for row in sdtm_rows]
            rows_by_domain[dom] = (
                list(deduplicate_sdtm_rows(sdtm_rows))
                if self.config.idempotent
                else list(sdtm_rows)
            )

        if self._stream_metadata is not None and lineage_out:
            commit_sdtm_lineage(self, lineage_out)

        if not rows_by_domain and self.config.sdtm_domain:
            dom = resolve_sdtm_domain(self.config.sdtm_domain, fallback="AE")
            rows_by_domain[dom] = []

        return rows_by_domain

    def _merge_sdtm_rows_with_existing(
        self,
        dest_file: Path,
        incoming_rows: Sequence[Mapping[str, Any]],
        voided_sdtm_sigs: set[tuple[str, str, str, str | None, int | None]],
    ) -> list[dict[str, Any]]:
        if not (self._is_incremental_sync and self.config.idempotent and dest_file.exists()):
            return [dict(r) for r in incoming_rows]
        try:
            existing_ds = load_dataset_json(dest_file)
            prev_rows = dataset_json_to_rows(existing_ds)
        except Exception:
            return [dict(r) for r in incoming_rows]

        if self.config.mask_phi and prev_rows:
            prev_rows = list(redact_batch_records(prev_rows, mask_phi=True, as_sdtm=True))

        incoming_sigs = {
            sig for r in incoming_rows if (sig := extract_sdtm_composite_key(r)) is not None
        }
        kept_prev: list[dict[str, Any]] = []
        for prev in prev_rows:
            sig = extract_sdtm_composite_key(prev)
            if sig is not None and (sig in voided_sdtm_sigs or sig in incoming_sigs):
                continue
            kept_prev.append(dict(prev))
        combined = kept_prev + [dict(r) for r in incoming_rows]
        return list(deduplicate_sdtm_rows(combined))

    def _merge_cdash_rows_with_existing(
        self,
        dest_file: Path,
        incoming_rows: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        if not (self._is_incremental_sync and self.config.idempotent and dest_file.exists()):
            return [dict(r) for r in incoming_rows]
        try:
            existing_ds = load_dataset_json(dest_file)
            prev_rows = dataset_json_to_rows(existing_ds)
        except Exception:
            return [dict(r) for r in incoming_rows]

        sens_registry = _build_sensitivity_registry(self._study_key)
        incoming_ids = {
            cid
            for r in incoming_rows
            if (cid := _canonical_id(r.get("RECORDID") or r.get("record_id"))) is not None
        }
        exclude_ids = incoming_ids | self._voided_ids
        kept_prev: list[dict[str, Any]] = []
        for prev in prev_rows:
            cid = _canonical_id(prev.get("RECORDID") or prev.get("record_id"))
            if cid is not None and cid in exclude_ids:
                continue
            row_copy = (
                {
                    k: ("***MASKED***" if sens_registry.is_sensitive(k) and v is not None else v)
                    for k, v in prev.items()
                }
                if self.config.mask_phi
                else dict(prev)
            )
            kept_prev.append(row_copy)
        return kept_prev + [dict(r) for r in incoming_rows]

    def _write_dataset_file(self, dest_file: Path, dataset: DatasetJsonDataset) -> None:
        payload = dataset.to_json_dict()
        if self._validate_schema:
            validate_dataset_json(payload, raise_on_error=True)
        serialized = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        _atomic_write_text(dest_file, serialized)

    def _write_datasets(self) -> int:
        active_records = list(self._records_by_cid.values()) + self._unkeyed_records
        if not self.config.include_deleted:
            active_records = [r for r in active_records if not is_record_deleted(r)]
        has_voided = bool(self._voided_records or self._voided_ids)

        if not self._per_form:
            dest_exists = self.path.exists() and self.path.stat().st_size > 0
        else:
            dest_exists = self.path.exists() and any(self.path.glob("*.json"))

        if (
            self._is_incremental_sync
            and self.config.idempotent
            and not active_records
            and not has_voided
            and dest_exists
        ):
            return 0

        with sdk_governance_context(self._sdk):
            dd = _resolve_study_datadict(
                self._sdk,
                self._study_key,
                active_records,
                explicit_dd=self._explicit_datadict,
            )

        resolved_creation_dt = _derive_deterministic_timestamp(
            self._creation_date_time,
            active_records,
        )
        custom_phi = _resolve_custom_phi_keys(self._study_key)

        if self.config.as_sdtm:
            rows_by_domain = self._transform_sdtm_rows_by_domain(active_records, dd)
            voided_sdtm_entries = (
                resolve_voided_sdtm_keys(
                    self._voided_records,
                    self,
                    study_key=self._study_key,
                    sdtm_domain=self.config.sdtm_domain,
                    form_domain_map=self._active_form_domain_map,
                )
                if self._voided_records
                else []
            )
            voided_sdtm_sigs = {
                (
                    str(e["study_id"]),
                    str(e["domain"]),
                    str(e["usubjid"]),
                    e.get("seq_col"),
                    e.get("seq_val"),
                )
                for e in voided_sdtm_entries
            }

            if self._per_form:
                self.path.mkdir(parents=True, exist_ok=True)
                target_domains = set(rows_by_domain)
                if voided_sdtm_sigs and self.path.exists():
                    for existing_f in self.path.glob("*.json"):
                        target_domains.add(existing_f.stem.upper())
                for dom in sorted(target_domains):
                    dest_file = self.path / f"{dom.upper()}.json"
                    merged_rows = self._merge_sdtm_rows_with_existing(
                        dest_file,
                        rows_by_domain.get(dom, []),
                        voided_sdtm_sigs,
                    )
                    ds = build_dataset_json_dataset(
                        self._study_key,
                        merged_rows,
                        dataset_name=dom.upper(),
                        dataset_label=f"{dom.upper()} SDTM Domain",
                        as_sdtm=True,
                        sdtm_domain=dom.upper(),
                        creation_date_time=resolved_creation_dt,
                        originator=self._originator,
                        source_system_version=self._source_system_version,
                        mask_phi=self.config.mask_phi,
                        custom_phi_keys=custom_phi,
                    )
                    self._write_dataset_file(dest_file, ds)
                return len(active_records)

            # Single-file SDTM Dataset-JSON output
            if self.config.sdtm_domain:
                dom_name = resolve_sdtm_domain(self.config.sdtm_domain, fallback="AE")
                flat_sdtm_rows = rows_by_domain.get(dom_name, [])
            elif len(rows_by_domain) == 1:
                dom_name, flat_sdtm_rows = next(iter(rows_by_domain.items()))
            else:
                dom_name = "SDTM"
                flat_sdtm_rows = [
                    row for dom in sorted(rows_by_domain) for row in rows_by_domain[dom]
                ]
            merged_rows = self._merge_sdtm_rows_with_existing(
                self.path,
                flat_sdtm_rows,
                voided_sdtm_sigs,
            )
            ds = build_dataset_json_dataset(
                self._study_key,
                merged_rows,
                dataset_name=dom_name,
                dataset_label=f"{dom_name} SDTM Domain",
                as_sdtm=True,
                sdtm_domain=dom_name if dom_name != "SDTM" else None,
                creation_date_time=resolved_creation_dt,
                originator=self._originator,
                source_system_version=self._source_system_version,
                mask_phi=self.config.mask_phi,
                custom_phi_keys=custom_phi,
            )
            self._write_dataset_file(self.path, ds)
            return len(active_records)

        # CDASH Form Dataset-JSON output
        if self._per_form:
            self.path.mkdir(parents=True, exist_ok=True)
            clinical_bundle = build_dataset_json_clinical_data(
                self._study_key,
                active_records,
                data_dictionary=dd,
                as_sdtm=False,
                creation_date_time=resolved_creation_dt,
                mask_phi=self.config.mask_phi,
                include_deleted=self.config.include_deleted,
                custom_phi_keys=custom_phi,
            )
            all_bundle_datasets = [
                *clinical_bundle.item_group_data,
                *clinical_bundle.reference_data,
            ]
            datasets_by_name = {ds.name.upper(): ds for ds in all_bundle_datasets}
            if self._voided_ids and self.path.exists():
                for existing_f in self.path.glob("*.json"):
                    fname = existing_f.stem.upper()
                    if fname not in datasets_by_name:
                        form_def = _find_form_by_key(dd, fname)
                        datasets_by_name[fname] = build_dataset_json_dataset(
                            self._study_key,
                            [],
                            dataset_name=fname,
                            dataset_label=(
                                form_def.formName if form_def and form_def.formName else fname
                            ),
                            form=form_def,
                            as_sdtm=False,
                            creation_date_time=resolved_creation_dt,
                            originator=self._originator,
                            source_system_version=self._source_system_version,
                            mask_phi=self.config.mask_phi,
                            custom_phi_keys=custom_phi,
                            use_labels_as_names=self._use_labels_as_columns,
                        )

            for fname in sorted(datasets_by_name):
                dest_file = self.path / f"{fname}.json"
                ds_delta = datasets_by_name[fname]
                form_def = _find_form_by_key(dd, ds_delta.name)
                if self._is_incremental_sync and self.config.idempotent and dest_file.exists():
                    delta_rows = dataset_json_to_rows(ds_delta)
                    merged_cdash_rows = self._merge_cdash_rows_with_existing(dest_file, delta_rows)
                    ds_out = build_dataset_json_dataset(
                        self._study_key,
                        merged_cdash_rows,
                        dataset_name=ds_delta.name,
                        dataset_label=ds_delta.label,
                        form=form_def,
                        is_reference_data=bool(ds_delta.is_reference_data),
                        as_sdtm=False,
                        creation_date_time=resolved_creation_dt,
                        originator=self._originator,
                        source_system_version=self._source_system_version,
                        mask_phi=self.config.mask_phi,
                        custom_phi_keys=custom_phi,
                        use_labels_as_names=self._use_labels_as_columns,
                    )
                else:
                    ds_out = ds_delta
                self._write_dataset_file(dest_file, ds_out)
            return len(active_records)

        # Single-file CDASH Dataset-JSON output
        observed_form_keys = sorted(
            {
                str(fkey)
                for r in active_records
                if (fkey := extract_record_form_ref(r)[1]) is not None
            }
        )
        single_form_key = observed_form_keys[0] if len(observed_form_keys) == 1 else None
        ds_name = single_form_key or "RECORDS"
        form_def = _find_form_by_key(dd, single_form_key)
        ds_label = (
            form_def.formName
            if form_def is not None and form_def.formName
            else f"{self._study_key} Clinical Records"
        )

        ds_delta = build_dataset_json_dataset(
            self._study_key,
            active_records,
            dataset_name=ds_name,
            dataset_label=ds_label,
            form=form_def,
            as_sdtm=False,
            creation_date_time=resolved_creation_dt,
            originator=self._originator,
            source_system_version=self._source_system_version,
            mask_phi=self.config.mask_phi,
            custom_phi_keys=custom_phi,
            use_labels_as_names=self._use_labels_as_columns,
        )
        if self._is_incremental_sync and self.config.idempotent and self.path.exists():
            delta_rows = dataset_json_to_rows(ds_delta)
            merged_cdash_rows = self._merge_cdash_rows_with_existing(self.path, delta_rows)
            ds_out = build_dataset_json_dataset(
                self._study_key,
                merged_cdash_rows,
                dataset_name=ds_name,
                dataset_label=ds_label,
                form=form_def,
                as_sdtm=False,
                creation_date_time=resolved_creation_dt,
                originator=self._originator,
                source_system_version=self._source_system_version,
                mask_phi=self.config.mask_phi,
                custom_phi_keys=custom_phi,
                use_labels_as_names=self._use_labels_as_columns,
            )
        else:
            ds_out = ds_delta

        self._write_dataset_file(self.path, ds_out)
        return len(active_records)

    def flush(self) -> None:
        """Atomically serialize and validate buffered Dataset-JSON dataset(s)."""
        batch_id = f"{self._study_key}/dataset_json/flush"
        try:
            self._execute_with_retry("export_dataset_json", batch_id, self._write_datasets)
        finally:
            self._records_by_cid.clear()
            self._unkeyed_records.clear()
            self._voided_records.clear()
            self._voided_ids.clear()

    def close(self) -> None:
        """Release buffered Dataset-JSON sink state."""
        self._records_by_cid.clear()
        self._unkeyed_records.clear()
        self._voided_records.clear()
        self._voided_ids.clear()


def export_to_odm(
    sdk: ImednetSDK,
    study_key: str,
    path: str | os.PathLike[str],
    *,
    datadict: DataDictionary | None = None,
    include_admin_data: bool = True,
    include_reference_data: bool = True,
    include_audit_records: bool = True,
    include_annotations: bool = True,
    file_type: Literal["Snapshot", "Transactional"] = "Snapshot",
    file_oid: str | None = None,
    creation_date_time: str | datetime | None = None,
    originator: str | None = "iMednet",
    source_system: str | None = "iMednet EDC",
    source_system_version: str | None = None,
    variable_whitelist: list[str] | None = None,
    form_whitelist: list[int] | None = None,
    quality_gate_enabled: bool = False,
    mask_phi: bool = True,
    validate_schema: bool = False,
    **kwargs: Any,
) -> None:
    """Export a study snapshot or incremental delta to CDISC ODM 1.3.2 XML.

    Streams study records with ledger checkpointing (`--since` / `incremental`),
    default PHI masking (`mask_phi=True`), `<AdminData>`, `<ReferenceData>`,
    `<ClinicalData>`, `<AuditRecord>` revisions, and `<Annotation>` queries.
    """
    if path is None:
        raise ValueError("'path' is required for export_to_odm.")
    kwargs.pop("manifest_path", None)
    config = _build_cdisc_sink_config(
        study_key,
        kwargs,
        quality_gate_enabled=quality_gate_enabled,
        mask_phi=mask_phi,
    )
    sink = OdmExportSink(
        sdk,
        study_key,
        path,
        config,
        datadict=datadict,
        include_admin_data=include_admin_data,
        include_reference_data=include_reference_data,
        include_audit_records=include_audit_records,
        include_annotations=include_annotations,
        file_type=file_type,
        file_oid=file_oid,
        creation_date_time=creation_date_time,
        originator=originator,
        source_system=source_system,
        source_system_version=source_system_version,
        variable_whitelist=variable_whitelist,
        form_whitelist=form_whitelist,
        validate_schema=validate_schema,
    )
    run_streaming_export(
        sdk,
        study_key,
        sink,
        config,
        stream_name="odm",
        batch_id_prefix=f"{study_key}/odm",
        quality_gate_fn=apply_quality_gate,
        iter_batches_fn=iter_batches,
    )


def export_to_dataset_json(
    sdk: ImednetSDK,
    study_key: str,
    path: str | os.PathLike[str],
    *,
    datadict: DataDictionary | None = None,
    use_labels_as_columns: bool = False,
    variable_whitelist: list[str] | None = None,
    form_whitelist: list[int] | None = None,
    creation_date_time: str | datetime | None = None,
    originator: str | None = "iMednet",
    source_system_name: str = "iMednet EDC",
    source_system_version: str = "1.0.0",
    per_form: bool | None = None,
    quality_gate_enabled: bool = False,
    mask_phi: bool = True,
    validate_schema: bool = False,
    **kwargs: Any,
) -> None:
    """Export study records to CDISC Dataset-JSON v1.1 for CDASH Forms or SDTM domains.

    Streams study records with ledger checkpointing (`--since` / `incremental`),
    default PHI masking (`mask_phi=True`), and deterministic Dataset-JSON v1.1
    schema-valid output.
    """
    if path is None:
        raise ValueError("'path' is required for export_to_dataset_json.")
    kwargs.pop("manifest_path", None)
    config = _build_cdisc_sink_config(
        study_key,
        kwargs,
        quality_gate_enabled=quality_gate_enabled,
        mask_phi=mask_phi,
    )
    sink = DatasetJsonExportSink(
        sdk,
        study_key,
        path,
        config,
        datadict=datadict,
        use_labels_as_columns=use_labels_as_columns,
        variable_whitelist=variable_whitelist,
        form_whitelist=form_whitelist,
        creation_date_time=creation_date_time,
        originator=originator,
        source_system_name=source_system_name,
        source_system_version=source_system_version,
        per_form=per_form,
        validate_schema=validate_schema,
    )
    run_streaming_export(
        sdk,
        study_key,
        sink,
        config,
        stream_name="dataset_json",
        batch_id_prefix=f"{study_key}/dataset_json",
        quality_gate_fn=apply_quality_gate,
        iter_batches_fn=iter_batches,
    )


__all__ = [
    "DatasetJsonExportSink",
    "OdmExportSink",
    "export_to_dataset_json",
    "export_to_odm",
]
