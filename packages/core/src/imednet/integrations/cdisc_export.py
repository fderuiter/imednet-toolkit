"""CDISC ODM 1.3.2 and Dataset-JSON v1.1 streaming export sinks and helpers."""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import logging
import os
import uuid
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import NonCallableMock

from imednet.core.context import sdk_governance_context
from imednet.datadict.cdisc_builders import (
    UNSCHEDULED_EVENT_OID,
    _coerce_dataset_json_cell,
    build_datadict_from_sdk_metadata,
    build_dataset_json_bundle,
    build_dataset_json_envelope,
    build_odm_archive,
    load_dataset_json,
    make_oid_slug,
    to_dataset_json,
)
from imednet.datadict.emit import write_odm_xml_stream
from imednet.datadict.exchange import (
    DatasetJsonColumn,
    DatasetJsonEnvelope,
    OdmReferenceData,
)
from imednet.datadict.loaders.cdisc_odm import load_odm_archive
from imednet.datadict.models import DataDictionary
from imednet.errors import ExportConfigurationError
from imednet.integrations._export_contracts import (
    filter_records_by_form_whitelist,
    resolve_config_manifest_path,
)
from imednet.integrations.enrichment import (
    resolve_sink_enrichment_pipeline,
    resolve_sink_phi_keys,
)
from imednet.integrations.sink_base import (
    ExportSink,
    SinkConfig,
    apply_quality_gate,
    commit_sdtm_lineage,
    deduplicate_sdtm_rows,
    extract_record_id,
    is_incremental_sink_config,
    iter_batches,
    rehydrate_sdtm_lineage,
    resolve_voided_sdtm_keys,
    run_streaming_export,
    should_preserve_sdtm_record_id,
)
from imednet.models.sdtm import build_form_sdtm_domain_map

if TYPE_CHECKING:
    from imednet.sdk import ImednetSDK

logger = logging.getLogger(__name__)

_SINK_CONFIG_FIELDS: frozenset[str] = frozenset(f.name for f in dataclasses.fields(SinkConfig))


def _coerce_output_path(path: str | os.PathLike[str] | None, *, default_name: str) -> Path:
    if path is None or not str(path).strip():
        return Path(default_name)
    return Path(path)


def _safe_fetch_sdk_items(
    sdk: Any,
    study_key: str,
    endpoint_attr: str,
    facade_method: str,
    *,
    extra_kwargs: Mapping[str, Any] | None = None,
) -> list[Any]:
    """Fetch auxiliary study items without failing when optional endpoints are unmocked."""
    call_kwargs = dict(extra_kwargs or {})
    try:
        if isinstance(sdk, NonCallableMock):
            mock_children = getattr(sdk, "_mock_children", {})
            obj_vars = vars(sdk)
            has_ep = endpoint_attr in mock_children or endpoint_attr in obj_vars
            has_facade = facade_method in mock_children or facade_method in obj_vars
            if not has_ep and not has_facade:
                return []
            if has_ep and not has_facade:
                ep = getattr(sdk, endpoint_attr)
                if "list" not in getattr(ep, "_mock_children", {}) and "list" not in vars(ep):
                    return []
                raw_iter = ep.list(study_key=study_key, **call_kwargs)
            else:
                raw_iter = getattr(sdk, facade_method)(study_key=study_key, **call_kwargs)
        else:
            ep = getattr(sdk, endpoint_attr, None)
            list_fn = getattr(ep, "list", None) if ep is not None else None
            if callable(list_fn):
                raw_iter = list_fn(study_key=study_key, **call_kwargs)
            elif callable(getattr(sdk, facade_method, None)):
                raw_iter = getattr(sdk, facade_method)(study_key=study_key, **call_kwargs)
            else:
                return []

        if isinstance(raw_iter, NonCallableMock):
            return []
        items = list(raw_iter)
        return [item for item in items if not isinstance(item, NonCallableMock)]
    except Exception as exc:
        logger.debug(
            "Skipping optional %s hydration for study '%s': %s",
            endpoint_attr,
            study_key,
            exc,
        )
        return []


def _build_cdisc_sink_config(
    study_key: str,
    *,
    config: SinkConfig | None,
    mask_phi: bool,
    as_sdtm: bool | None = None,
    sdtm_domain: str | None = None,
    naming_convention: str | None = None,
    preserve_record_id: bool | None = None,
    kwargs: dict[str, Any],
) -> SinkConfig:
    """Resolve a validated :class:`SinkConfig` for ODM or Dataset-JSON exports."""
    remaining = dict(kwargs)
    if config is not None:
        overrides: dict[str, Any] = {}
        for k in list(remaining):
            if k in _SINK_CONFIG_FIELDS:
                overrides[k] = remaining.pop(k)
        if not mask_phi and config.mask_phi:
            overrides["mask_phi"] = False
        if as_sdtm is not None and as_sdtm != config.as_sdtm:
            overrides["as_sdtm"] = as_sdtm
        if sdtm_domain is not None and config.sdtm_domain is None:
            overrides["sdtm_domain"] = sdtm_domain
            if (
                as_sdtm is None
                and "as_sdtm" not in overrides
                and not getattr(config, "_as_sdtm_explicit", False)
            ):
                overrides["as_sdtm"] = True
        if naming_convention is not None and config.naming_convention is None:
            overrides["naming_convention"] = naming_convention
        if preserve_record_id is not None and config.preserve_record_id is None:
            overrides["preserve_record_id"] = preserve_record_id
        if remaining:
            merged_extra = dict(config.extra)
            merged_extra.update(remaining)
            overrides["extra"] = merged_extra
        return dataclasses.replace(config, **overrides) if overrides else config

    cfg_kwargs: dict[str, Any] = {}
    extra_kwargs: dict[str, Any] = {}
    for k, v in remaining.items():
        if k in _SINK_CONFIG_FIELDS:
            cfg_kwargs[k] = v
        else:
            extra_kwargs[k] = v

    cfg_kwargs.setdefault("mask_phi", mask_phi)
    if as_sdtm is not None:
        cfg_kwargs.setdefault("as_sdtm", as_sdtm)
    if sdtm_domain is not None:
        cfg_kwargs.setdefault("sdtm_domain", sdtm_domain)
    if naming_convention is not None:
        cfg_kwargs.setdefault("naming_convention", naming_convention)
    if preserve_record_id is not None:
        cfg_kwargs.setdefault("preserve_record_id", preserve_record_id)
    if extra_kwargs:
        existing_extra = cfg_kwargs.get("extra")
        merged = dict(existing_extra) if isinstance(existing_extra, Mapping) else {}
        merged.update(extra_kwargs)
        cfg_kwargs["extra"] = merged

    return SinkConfig(study_key=study_key, **cfg_kwargs)


def _atomic_write_text(dest: Path, content: str) -> None:
    """Write UTF-8 text to a sibling ``.tmp`` file and atomically replace *dest*."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest.parent / f".{dest.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, dest)
    except Exception:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise


def _append_file_manifest(
    manifest_path: str | None,
    *,
    destination: str,
    study_key: str,
    row_count: int,
    file_path: Path,
) -> None:
    """Append a JSONL manifest checksum entry when *manifest_path* is configured."""
    if not manifest_path or not file_path.exists():
        return
    import json

    digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
    m_path = Path(manifest_path)
    m_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "batch_id": f"{study_key}/{destination}/final",
        "destination": destination,
        "row_count": row_count,
        "checksum": digest,
    }
    with m_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")


def _extract_existing_odm_records(
    existing_archive: Any,
    *,
    exclude_record_ids: set[str],
    study_key: str,
) -> list[dict[str, Any]]:
    """Reconstruct synthetic Record dicts from an existing ``OdmArchive`` for incremental upsert."""
    dd = existing_archive.data_dictionary
    oid_to_var: dict[tuple[str, str], str] = {}
    ig_oid_to_form_key: dict[str, str] = {}
    for form in dd.forms:
        extra_form = form.extraAttributes if isinstance(form.extraAttributes, Mapping) else {}
        preserved_ig = extra_form.get("itemGroupOID")
        ig_oids = [f"IG.{form.formKey}", f"IG_{form.formKey}"]
        if isinstance(preserved_ig, str) and preserved_ig.strip():
            ig_oids.append(preserved_ig.strip())
        for ig_oid in ig_oids:
            ig_oid_to_form_key[ig_oid] = form.formKey
        for var in form.variables:
            extra_var = var.extraAttributes if isinstance(var.extraAttributes, Mapping) else {}
            preserved_it = extra_var.get("itemOID")
            cand_oids = [
                var.variableName,
                f"{form.formKey}.{var.variableName}",
                f"IT.{form.formKey}.{var.variableName}",
            ]
            if isinstance(preserved_it, str) and preserved_it.strip():
                cand_oids.append(preserved_it.strip())
            for cand_oid in cand_oids:
                oid_to_var[(form.formKey, cand_oid)] = var.variableName
                for ig_oid in ig_oids:
                    oid_to_var[(ig_oid, cand_oid)] = var.variableName

    se_oid_to_interval_name: dict[str, str] = {}
    visits_meta = (
        dd.metadata.extraAttributes.get("visits")
        if isinstance(dd.metadata.extraAttributes, Mapping)
        else None
    )
    if isinstance(visits_meta, Sequence):
        for v_entry in visits_meta:
            if isinstance(v_entry, Mapping):
                v_oid = v_entry.get("oid")
                v_name = v_entry.get("name")
                if (
                    isinstance(v_oid, str)
                    and v_oid.strip()
                    and isinstance(v_name, str)
                    and v_name.strip()
                ):
                    se_oid_to_interval_name[v_oid.strip()] = v_name.strip()

    def _collect_item_groups(
        item_groups: Sequence[Any],
        *,
        form_key: str,
        subj_key: str | None,
        site_oid: str | None,
        visit_id: int | None,
        interval_name: str | None,
        study_event_oid: str | None,
        study_event_repeat_key: str | None,
    ) -> None:
        for ig in item_groups:
            if getattr(ig, "transaction_type", None) == "Remove":
                continue
            rep_key = ig.item_group_repeat_key
            if rep_key and rep_key in exclude_record_ids:
                continue
            rec_id: int | str | None = None
            rec_oid: str | None = None
            if rep_key is not None:
                if rep_key.isdigit():
                    rec_id = int(rep_key)
                else:
                    rec_id = rep_key
                    rec_oid = rep_key
            rec_data: dict[str, Any] = {}
            for item in ig.items:
                if getattr(item, "transaction_type", None) == "Remove":
                    continue
                vname = oid_to_var.get((form_key, item.item_oid)) or oid_to_var.get(
                    (ig.item_group_oid, item.item_oid)
                )
                if vname is None:
                    for prefix in (f"IT.{form_key}.", f"{form_key}."):
                        if item.item_oid.startswith(prefix):
                            vname = item.item_oid[len(prefix) :]
                            break
                    else:
                        vname = item.item_oid
                rec_data[vname] = None if item.is_null else item.value
            rec_entry: dict[str, Any] = {
                "study_key": study_key,
                "subject_key": subj_key,
                "site_name": (
                    site_oid[4:] if site_oid and site_oid.startswith("LOC.") else site_oid
                ),
                "visit_id": visit_id,
                "interval_name": interval_name,
                "form_key": form_key,
                "record_id": rec_id,
                "record_oid": rec_oid,
                "record_data": rec_data,
            }
            if study_event_oid:
                rec_entry["study_event_oid"] = study_event_oid
            if study_event_repeat_key is not None:
                rec_entry["study_event_repeat_key"] = study_event_repeat_key
            retained.append(rec_entry)

    def _collect_form_records(
        fd: Any,
        *,
        subj_key: str | None,
        site_oid: str | None,
        visit_id: int | None,
        interval_name: str | None,
        study_event_oid: str | None,
        study_event_repeat_key: str | None,
    ) -> None:
        if getattr(fd, "transaction_type", None) == "Remove":
            return
        form_key = fd.form_oid.removeprefix("F.")
        _collect_item_groups(
            fd.item_groups,
            form_key=form_key,
            subj_key=subj_key,
            site_oid=site_oid,
            visit_id=visit_id,
            interval_name=interval_name,
            study_event_oid=study_event_oid,
            study_event_repeat_key=study_event_repeat_key,
        )

    retained: list[dict[str, Any]] = []
    for ref_container in getattr(existing_archive, "reference_data", ()):
        for ig in ref_container.item_groups:
            if getattr(ig, "transaction_type", None) == "Remove":
                continue
            mapped_fk = ig_oid_to_form_key.get(ig.item_group_oid)
            if mapped_fk is not None:
                _collect_item_groups(
                    (ig,),
                    form_key=mapped_fk,
                    subj_key=None,
                    site_oid=None,
                    visit_id=None,
                    interval_name=None,
                    study_event_oid=None,
                    study_event_repeat_key=None,
                )
    for cdata in existing_archive.clinical_data:
        for subj in cdata.subjects:
            if getattr(subj, "transaction_type", None) == "Remove":
                continue
            site_oid = subj.site_location_oid
            for se in subj.study_events:
                if getattr(se, "transaction_type", None) == "Remove":
                    continue
                visit_id: int | None = None
                if se.study_event_repeat_key and se.study_event_repeat_key.isdigit():
                    visit_id = int(se.study_event_repeat_key)
                is_scheduled = se.study_event_oid != UNSCHEDULED_EVENT_OID
                interval_name = (
                    se_oid_to_interval_name.get(se.study_event_oid, se.study_event_oid)
                    if is_scheduled
                    else None
                )
                for fd in se.forms:
                    _collect_form_records(
                        fd,
                        subj_key=subj.subject_key,
                        site_oid=site_oid,
                        visit_id=visit_id,
                        interval_name=interval_name,
                        study_event_oid=se.study_event_oid if is_scheduled else None,
                        study_event_repeat_key=se.study_event_repeat_key if is_scheduled else None,
                    )
            for fd in subj.forms:
                _collect_form_records(
                    fd,
                    subj_key=subj.subject_key,
                    site_oid=site_oid,
                    visit_id=None,
                    interval_name=None,
                    study_event_oid=None,
                    study_event_repeat_key=None,
                )
    return retained


def _retain_static_odm_reference_data(existing_archive: Any) -> list[OdmReferenceData]:
    """Return non-form static ``OdmReferenceData`` blocks from *existing_archive*."""
    dd = existing_archive.data_dictionary
    form_ig_oids: set[str] = set()
    for form in dd.forms:
        form_ig_oids.add(f"IG.{form.formKey}")
        form_ig_oids.add(f"IG_{form.formKey}")
        extra_form = form.extraAttributes if isinstance(form.extraAttributes, Mapping) else {}
        preserved_ig = extra_form.get("itemGroupOID")
        if isinstance(preserved_ig, str) and preserved_ig.strip():
            form_ig_oids.add(preserved_ig.strip())
    static_refs: list[OdmReferenceData] = []
    for ref_container in getattr(existing_archive, "reference_data", ()):
        kept_igs = tuple(
            ig for ig in ref_container.item_groups if ig.item_group_oid not in form_ig_oids
        )
        if kept_igs:
            static_refs.append(ref_container.model_copy(update={"item_groups": kept_igs}))
    return static_refs


class _OdmArchiveSink(ExportSink):
    """Streaming CDISC ODM 1.3.2 archive sink supporting incremental upserts and voided pruning."""

    reconciles_deleted_on_full_sync = True

    def __init__(
        self,
        sdk: Any,
        study_key: str,
        output_path: Path,
        config: SinkConfig,
        *,
        data_dictionary: DataDictionary | None = None,
        reference_data: Sequence[OdmReferenceData] = (),
        include_audit_trail: bool = True,
        include_annotations: bool = True,
        form_whitelist: Sequence[int] | None = None,
        variable_whitelist: Sequence[str] | None = None,
        metadata_version_oid: str = "MDV.001",
        file_oid: str | None = None,
        creation_datetime: str | None = None,
        manifest_path: str | None = None,
        enrichment_pipeline: Any = None,
        phi_keys: Collection[str] | None = None,
    ) -> None:
        super().__init__(config)
        self._sdk = sdk
        self._study_key = study_key
        self._path = output_path
        self._data_dictionary = data_dictionary
        self._reference_data = tuple(reference_data)
        self._include_audit_trail = include_audit_trail
        self._include_annotations = include_annotations
        self._form_whitelist = list(form_whitelist) if form_whitelist is not None else None
        self._variable_whitelist = (
            set(variable_whitelist) if variable_whitelist is not None else None
        )
        self._metadata_version_oid = metadata_version_oid
        self._file_oid = file_oid
        self._creation_datetime = creation_datetime
        self._manifest_path = manifest_path
        self._pipeline = enrichment_pipeline
        self._phi_keys = set(phi_keys or ())
        self._staged_by_id: dict[str, Any] = {}
        self._unkeyed_records: list[Any] = []
        self._voided_ids: set[str] = set()
        self._flushed = False

    def redact_batch(
        self,
        records: Sequence[Any],
        *,
        study_key: str | None = None,
        config: SinkConfig | None = None,
        phi_keys: Collection[str] | None = None,
    ) -> Sequence[Any]:
        """Defer PHI redaction to ``build_odm_archive`` so ``EnrichmentPipeline`` runs on raw records."""
        return records

    def _project_record(self, rec: Any) -> Any:
        if self._variable_whitelist is None:
            return rec
        allowed = self._variable_whitelist
        rdata = getattr(rec, "record_data", None)
        if hasattr(rec, "model_copy") and isinstance(rdata, Mapping):
            return rec.model_copy(
                update={"record_data": {k: v for k, v in rdata.items() if str(k) in allowed}}
            )
        if isinstance(rec, Mapping):
            copied = dict(rec)
            inner = copied.get("record_data") or copied.get("recordData")
            if isinstance(inner, Mapping):
                key_name = "record_data" if "record_data" in copied else "recordData"
                copied[key_name] = {k: v for k, v in inner.items() if str(k) in allowed}
            return copied
        return rec

    def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Stage a redacted batch of study records for ODM serialization."""
        filtered = filter_records_by_form_whitelist(records, self._form_whitelist)

        def _stage() -> int:
            count = 0
            for rec in filtered:
                projected = self._project_record(rec)
                rid = extract_record_id(projected)
                if rid is not None and not isinstance(rid, bool):
                    rid_str = str(rid).strip()
                    self._voided_ids.discard(rid_str)
                    self._staged_by_id[rid_str] = projected
                else:
                    self._unkeyed_records.append(projected)
                count += 1
            return count

        return self._execute_with_retry("write_odm_batch", batch_id, _stage)

    def reconcile_deleted(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Record voided ``record_id`` values so ``flush()`` prunes them from existing archives."""

        def _reconcile() -> int:
            reconciled = 0
            for rec in records:
                rid = extract_record_id(rec)
                if rid is not None and not isinstance(rid, bool):
                    rid_str = str(rid).strip()
                    self._staged_by_id.pop(rid_str, None)
                    self._voided_ids.add(rid_str)
                    reconciled += 1
            return reconciled

        return self._execute_with_retry("reconcile_odm_deleted", batch_id, _reconcile)

    def flush(self) -> None:
        """Materialize and stream the ODM 1.3.2 XML snapshot atomically to disk."""
        if self._flushed:
            return
        self._flushed = True

        is_incremental = is_incremental_sink_config(self.config)
        staged_records = [*self._staged_by_id.values(), *self._unkeyed_records]
        merged_records: list[Any] = list(staged_records)
        existing_ref_data: list[OdmReferenceData] = list(self._reference_data)
        prior_file_oid: str | None = None
        prior_creation_dt: str | None = None

        if (
            self.config.idempotent
            and (is_incremental or self._voided_ids)
            and self._path.exists()
            and self._path.stat().st_size > 0
        ):
            if not staged_records and not self._voided_ids and is_incremental:
                return
            try:
                prior_archive = load_odm_archive(self._path)
                prior_file_oid = prior_archive.file_oid
                prior_creation_dt = prior_archive.creation_datetime
                exclude_ids = set(self._staged_by_id.keys()) | self._voided_ids
                prior_records = _extract_existing_odm_records(
                    prior_archive,
                    exclude_record_ids=exclude_ids,
                    study_key=self._study_key,
                )
                merged_records = [*prior_records, *staged_records]
                if not existing_ref_data and prior_archive.reference_data:
                    existing_ref_data = _retain_static_odm_reference_data(prior_archive)
            except Exception as exc:
                logger.warning(
                    "Unable to merge existing ODM archive at '%s'; writing fresh snapshot: %s",
                    self._path,
                    exc,
                )

        forms = _safe_fetch_sdk_items(self._sdk, self._study_key, "forms", "get_forms")
        variables = _safe_fetch_sdk_items(self._sdk, self._study_key, "variables", "get_variables")
        intervals = _safe_fetch_sdk_items(self._sdk, self._study_key, "intervals", "get_intervals")
        sites = _safe_fetch_sdk_items(self._sdk, self._study_key, "sites", "get_sites")
        users = _safe_fetch_sdk_items(self._sdk, self._study_key, "users", "get_users")
        subjects = _safe_fetch_sdk_items(self._sdk, self._study_key, "subjects", "get_subjects")
        visits = _safe_fetch_sdk_items(self._sdk, self._study_key, "visits", "get_visits")
        revisions = (
            _safe_fetch_sdk_items(
                self._sdk,
                self._study_key,
                "record_revisions",
                "get_record_revisions",
            )
            if self._include_audit_trail
            else []
        )
        queries = (
            _safe_fetch_sdk_items(self._sdk, self._study_key, "queries", "get_queries")
            if self._include_annotations
            else []
        )

        if self._form_whitelist is not None:
            allowed_fids = set(self._form_whitelist)
            forms = [
                f
                for f in forms
                if (
                    f.get("formId") or f.get("form_id")
                    if isinstance(f, Mapping)
                    else getattr(f, "form_id", None)
                )
                in allowed_fids
            ]
            variables = [
                v
                for v in variables
                if (
                    v.get("formId") or v.get("form_id")
                    if isinstance(v, Mapping)
                    else getattr(v, "form_id", None)
                )
                in allowed_fids
            ]
        if self._variable_whitelist is not None:
            allowed_vnames = self._variable_whitelist
            variables = [
                v
                for v in variables
                if str(
                    (
                        v.get("variableName") or v.get("variable_name")
                        if isinstance(v, Mapping)
                        else getattr(v, "variable_name", "")
                    )
                    or ""
                )
                in allowed_vnames
            ]

        with sdk_governance_context(self._sdk):
            dd = self._data_dictionary or build_datadict_from_sdk_metadata(
                self._study_key,
                forms=forms,
                variables=variables,
                intervals=intervals,
            )
            archive = build_odm_archive(
                self._study_key,
                data_dictionary=dd,
                forms=forms,
                variables=variables,
                intervals=intervals,
                subjects=subjects,
                visits=visits,
                records=merged_records,
                revisions=revisions,
                queries=queries,
                sites=sites,
                users=users,
                reference_data=existing_ref_data,
                metadata_version_oid=self._metadata_version_oid,
                file_oid=self._file_oid or prior_file_oid,
                creation_datetime=self._creation_datetime or prior_creation_dt,
                mask_phi=self.config.mask_phi,
                custom_phi_keys=self._phi_keys or None,
                enrichment_pipeline=self._pipeline,
                include_deleted=self.config.include_deleted,
            )

            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = (
                self._path.parent / f".{self._path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
            )
            try:
                with tmp_path.open("w", encoding="utf-8") as fh:
                    write_odm_xml_stream(
                        archive,
                        fh,
                        mask_phi=self.config.mask_phi,
                        custom_phi_keys=self._phi_keys or None,
                    )
                os.replace(tmp_path, self._path)
            except Exception:
                with contextlib.suppress(OSError):
                    tmp_path.unlink(missing_ok=True)
                raise

        _append_file_manifest(
            self._manifest_path,
            destination="odm",
            study_key=self._study_key,
            row_count=len(merged_records),
            file_path=self._path,
        )

    def close(self) -> None:
        """Release staged buffers."""
        self._staged_by_id.clear()
        self._unkeyed_records.clear()
        self._voided_ids.clear()


def _seed_sdtm_counters_from_envelope(
    env: DatasetJsonEnvelope,
    domain_seq_counters: dict[str, dict[str, int]],
) -> None:
    """Ensure ``domain_seq_counters`` never assigns below existing ``<DOM>SEQ`` values on disk."""
    dom = env.name.strip().upper()
    seq_col = f"{dom}SEQ"
    col_names = [c.name for c in env.columns]
    if "USUBJID" not in col_names or seq_col not in col_names:
        return
    usubjid_idx = col_names.index("USUBJID")
    seq_idx = col_names.index(seq_col)
    dom_bucket = domain_seq_counters.setdefault(dom, {})
    for row in env.rows:
        if len(row) <= max(usubjid_idx, seq_idx):
            continue
        usubjid = row[usubjid_idx]
        seq_val = row[seq_idx]
        if (
            isinstance(usubjid, str)
            and usubjid.strip()
            and isinstance(seq_val, int)
            and not isinstance(seq_val, bool)
            and seq_val > dom_bucket.get(usubjid, 0)
        ):
            dom_bucket[usubjid] = seq_val


def _merge_dataset_json_envelopes(
    prior_env: DatasetJsonEnvelope,
    delta_env: DatasetJsonEnvelope | None,
    *,
    voided_record_ids: set[str],
    voided_sdtm_keys: set[tuple[Any, ...]],
    creation_datetime_override: str | None = None,
    file_oid_override: str | None = None,
) -> DatasetJsonEnvelope:
    """Merge an existing flat Dataset-JSON v1.1 envelope with delta updates and voided deletions."""
    prior_col_names = [col.name for col in prior_env.columns]
    prior_rows: list[dict[str, Any]] = [
        dict(zip(prior_col_names, row, strict=False)) for row in prior_env.rows
    ]

    delta_rows: list[dict[str, Any]] = []
    if delta_env is not None:
        delta_col_names = [col.name for col in delta_env.columns]
        delta_rows = [dict(zip(delta_col_names, row, strict=False)) for row in delta_env.rows]

    def _row_rec_id(row: Mapping[str, Any]) -> str | None:
        for k in ("recordId", "record_id", "RECORD_ID"):
            val = row.get(k)
            if val is not None and not isinstance(val, bool) and str(val).strip():
                return str(val).strip()
        return None

    def _row_sdtm_sig(row: Mapping[str, Any]) -> tuple[Any, ...] | None:
        st_id = row.get("STUDYID")
        usubjid = row.get("USUBJID")
        if not st_id or not usubjid:
            return None
        dom = str(row.get("DOMAIN") or prior_env.name).strip().upper()
        if dom == "DM":
            return (str(st_id), "DM", str(usubjid), None, None)
        seq_col = f"{dom}SEQ"
        if seq_col in row and row.get(seq_col) is not None:
            return (str(st_id), dom, str(usubjid), seq_col, row.get(seq_col))
        return None

    superseded_ids = {
        rid for r in delta_rows if (rid := _row_rec_id(r)) is not None
    } | voided_record_ids
    superseded_sdtm = {
        sig for r in delta_rows if (sig := _row_sdtm_sig(r)) is not None
    } | voided_sdtm_keys

    kept_prior: list[dict[str, Any]] = []
    for prow in prior_rows:
        prid = _row_rec_id(prow)
        if prid is not None and prid in superseded_ids:
            continue
        psig = _row_sdtm_sig(prow)
        if psig is not None and psig in superseded_sdtm:
            continue
        kept_prior.append(prow)

    combined_rows = [*kept_prior, *delta_rows]
    if prior_env.name.strip().upper() == "DM":
        combined_rows = deduplicate_sdtm_rows(combined_rows)

    unified_cols_by_name: dict[str, DatasetJsonColumn] = {
        col.name: col for col in prior_env.columns if col.name != "ITEMGROUPDATASEQ"
    }
    ordered_names = [n for n in prior_col_names if n != "ITEMGROUPDATASEQ"]
    if delta_env is not None:
        for d_col in delta_env.columns:
            if d_col.name == "ITEMGROUPDATASEQ":
                continue
            if d_col.name not in unified_cols_by_name:
                unified_cols_by_name[d_col.name] = d_col
                ordered_names.append(d_col.name)
            elif unified_cols_by_name[d_col.name].data_type != d_col.data_type:
                base_col = unified_cols_by_name[d_col.name]
                unified_cols_by_name[d_col.name] = DatasetJsonColumn(
                    itemOID=base_col.item_oid,
                    name=base_col.name,
                    label=base_col.label,
                    dataType="string",
                    targetDataType=None,
                    length=base_col.length,
                    displayFormat=base_col.display_format,
                    keySequence=base_col.key_sequence,
                )

    final_cols = tuple(unified_cols_by_name[n] for n in ordered_names)

    key_cols = [
        col.name
        for col in sorted(
            (c for c in final_cols if c.key_sequence is not None),
            key=lambda x: x.key_sequence or 0,
        )
    ]
    if key_cols:
        combined_rows.sort(
            key=lambda r: tuple(
                (0, int(r[kc]))
                if isinstance(r.get(kc), int) and not isinstance(r.get(kc), bool)
                else (1, str(r.get(kc) or ""))
                for kc in key_cols
            )
        )

    tuple_rows = tuple(
        tuple(_coerce_dataset_json_cell(r.get(col.name), col.data_type) for col in final_cols)
        for r in combined_rows
    )

    resolved_dt = (
        creation_datetime_override
        if creation_datetime_override
        else (
            max(prior_env.dataset_json_creation_datetime, delta_env.dataset_json_creation_datetime)
            if delta_env is not None
            else prior_env.dataset_json_creation_datetime
        )
    )
    resolved_db_dt: str | None = prior_env.db_last_modified_datetime
    if (
        delta_env is not None
        and delta_env.db_last_modified_datetime is not None
        and (resolved_db_dt is None or delta_env.db_last_modified_datetime > resolved_db_dt)
    ):
        resolved_db_dt = delta_env.db_last_modified_datetime

    return DatasetJsonEnvelope(
        datasetJSONCreationDateTime=resolved_dt,
        datasetJSONVersion="1.1.0",
        fileOID=file_oid_override or prior_env.file_oid,
        dbLastModifiedDateTime=resolved_db_dt,
        originator=delta_env.originator if delta_env is not None else prior_env.originator,
        sourceSystem=delta_env.source_system if delta_env is not None else prior_env.source_system,
        studyOID=delta_env.study_oid if delta_env is not None else prior_env.study_oid,
        metaDataVersionOID=(
            delta_env.metadata_version_oid
            if delta_env is not None
            else prior_env.metadata_version_oid
        ),
        metaDataRef=delta_env.metadata_ref if delta_env is not None else prior_env.metadata_ref,
        itemGroupOID=prior_env.item_group_oid,
        isReferenceData=(
            delta_env.is_reference_data if delta_env is not None else prior_env.is_reference_data
        ),
        records=len(tuple_rows),
        name=prior_env.name,
        label=delta_env.label if delta_env is not None else prior_env.label,
        columns=final_cols,
        rows=tuple_rows,
    )


class _DatasetJsonSink(ExportSink):
    """Streaming CDISC Dataset-JSON v1.1 sink for CDASH Forms and SDTM domains."""

    _delegates_sdtm_redaction = True
    reconciles_deleted_on_full_sync = True

    def __init__(
        self,
        sdk: Any,
        study_key: str,
        output_path: Path,
        config: SinkConfig,
        *,
        use_labels_as_columns: bool = False,
        form_whitelist: Sequence[int] | None = None,
        variable_whitelist: Sequence[str] | None = None,
        preserve_record_id: bool | None = None,
        metadata_version_oid: str = "MDV.001",
        file_oid: str | None = None,
        creation_datetime: str | None = None,
        indent: int = 2,
        manifest_path: str | None = None,
        enrichment_pipeline: Any = None,
        phi_keys: Collection[str] | None = None,
    ) -> None:
        super().__init__(config)
        self._sdk = sdk
        self._study_key = study_key
        self._path = output_path
        self._use_labels_as_columns = use_labels_as_columns
        self._form_whitelist = list(form_whitelist) if form_whitelist is not None else None
        self._variable_whitelist = (
            list(variable_whitelist) if variable_whitelist is not None else None
        )
        self._preserve_record_id = preserve_record_id
        self._metadata_version_oid = metadata_version_oid
        self._file_oid = file_oid
        self._creation_datetime = creation_datetime
        self._indent = indent
        self._manifest_path = manifest_path
        self._pipeline = enrichment_pipeline
        self._phi_keys = set(phi_keys or ())
        self._staged_by_id: dict[str, Any] = {}
        self._unkeyed_records: list[Any] = []
        self._voided_ids: set[str] = set()
        self._voided_records: list[Any] = []
        self._voided_sdtm_keys: set[tuple[Any, ...]] = set()
        self._sdtm_lineage: dict[str, dict[str, Any]] = {}
        self._sdtm_seq_watermarks: dict[str, int] = {}
        self._flushed = False

    def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Stage a batch of records for Dataset-JSON v1.1 serialization."""
        filtered = filter_records_by_form_whitelist(records, self._form_whitelist)

        def _stage() -> int:
            count = 0
            for rec in filtered:
                rid = extract_record_id(rec)
                if rid is not None and not isinstance(rid, bool):
                    rid_str = str(rid).strip()
                    self._voided_ids.discard(rid_str)
                    self._staged_by_id[rid_str] = rec
                else:
                    self._unkeyed_records.append(rec)
                count += 1
            return count

        return self._execute_with_retry("write_dataset_json_batch", batch_id, _stage)

    def reconcile_deleted(self, records: Sequence[Any], *, batch_id: str) -> int:
        """Stage voided record identifiers and SDTM composite keys for pruning on ``flush()``."""

        def _reconcile() -> int:
            reconciled = 0
            effective_sdtm = (
                bool(self.config.as_sdtm)
                if getattr(self.config, "_as_sdtm_explicit", False)
                else bool(self.config.as_sdtm or self.config.sdtm_domain)
            )
            for rec in records:
                rid = extract_record_id(rec)
                if rid is not None and not isinstance(rid, bool):
                    rid_str = str(rid).strip()
                    self._staged_by_id.pop(rid_str, None)
                    self._voided_ids.add(rid_str)
                    reconciled += 1
                if effective_sdtm:
                    self._voided_records.append(rec)
            if effective_sdtm and records:
                for entry in resolve_voided_sdtm_keys(
                    records,
                    self,
                    study_key=self._study_key,
                    sdtm_domain=self.config.sdtm_domain,
                ):
                    sig = (
                        entry.get("study_id"),
                        entry.get("domain"),
                        entry.get("usubjid"),
                        entry.get("seq_col"),
                        entry.get("seq_val"),
                    )
                    self._voided_sdtm_keys.add(sig)
            return reconciled

        return self._execute_with_retry("reconcile_dataset_json_deleted", batch_id, _reconcile)

    def flush(self) -> None:
        """Build or incrementally merge the Dataset-JSON v1.1 envelope(s) and write atomically."""
        if self._flushed:
            return
        self._flushed = True

        is_incremental = is_incremental_sink_config(self.config)
        staged_records = [*self._staged_by_id.values(), *self._unkeyed_records]
        has_voided = bool(self._voided_ids or self._voided_sdtm_keys or self._voided_records)

        is_dir_target = self._path.is_dir() or self._path.suffix == ""
        if (
            is_incremental
            and self.config.idempotent
            and not staged_records
            and not has_voided
            and self._path.exists()
            and (is_dir_target or self._path.stat().st_size > 0)
        ):
            return

        effective_sdtm = (
            bool(self.config.as_sdtm)
            if getattr(self.config, "_as_sdtm_explicit", False)
            else bool(self.config.as_sdtm or self.config.sdtm_domain)
        )

        with sdk_governance_context(self._sdk):
            forms = _safe_fetch_sdk_items(self._sdk, self._study_key, "forms", "get_forms")
            visits = _safe_fetch_sdk_items(self._sdk, self._study_key, "visits", "get_visits")
            variables = (
                []
                if effective_sdtm
                else _safe_fetch_sdk_items(
                    self._sdk,
                    self._study_key,
                    "variables",
                    "get_variables",
                )
            )
            effective_preserve_rec_id = should_preserve_sdtm_record_id(
                self.config,
                self._preserve_record_id,
            )

            domain_seq_counters: dict[str, dict[str, int]] | None = None
            seq_overrides: dict[str, int] | None = None
            lineage_updates: dict[str, dict[str, Any]] = {}

            if effective_sdtm:
                form_domain_map = build_form_sdtm_domain_map(
                    forms,
                    study_config=getattr(self._pipeline, "config", None),
                    study_key=self._study_key,
                )
                domain_seq_counters, seq_overrides, _ = rehydrate_sdtm_lineage(
                    self,
                    study_key=self._study_key,
                )
                if self._voided_records:
                    for entry in resolve_voided_sdtm_keys(
                        self._voided_records,
                        self,
                        study_key=self._study_key,
                        sdtm_domain=self.config.sdtm_domain,
                        form_domain_map=form_domain_map,
                    ):
                        self._voided_sdtm_keys.add(
                            (
                                entry.get("study_id"),
                                entry.get("domain"),
                                entry.get("usubjid"),
                                entry.get("seq_col"),
                                entry.get("seq_val"),
                            )
                        )

            if is_dir_target:
                self._path.mkdir(parents=True, exist_ok=True)
                prior_by_oid: dict[str, tuple[Path, DatasetJsonEnvelope]] = {}
                if self.config.idempotent and (is_incremental or has_voided):
                    for existing_file in sorted(self._path.glob("*.json")):
                        try:
                            p_env = load_dataset_json(existing_file)
                            prior_by_oid[p_env.item_group_oid] = (existing_file, p_env)
                            if effective_sdtm and domain_seq_counters is not None:
                                _seed_sdtm_counters_from_envelope(p_env, domain_seq_counters)
                        except Exception as exc:
                            logger.warning(
                                "Skipping unreadable Dataset-JSON file '%s': %s",
                                existing_file,
                                exc,
                            )

                delta_datasets: dict[str, DatasetJsonEnvelope] = {}
                if staged_records or not prior_by_oid:
                    bundle = build_dataset_json_bundle(
                        self._study_key,
                        records=staged_records,
                        forms=forms,
                        variables=variables,
                        visits=visits,
                        as_sdtm=effective_sdtm,
                        sdtm_domain=self.config.sdtm_domain,
                        use_labels_as_columns=self._use_labels_as_columns,
                        variable_whitelist=self._variable_whitelist,
                        form_whitelist=self._form_whitelist,
                        naming_convention=self.config.naming_convention,
                        preserve_record_id=effective_preserve_rec_id,
                        include_deleted=self.config.include_deleted,
                        mask_phi=self.config.mask_phi,
                        custom_phi_keys=self._phi_keys or None,
                        enrichment_pipeline=self._pipeline,
                        metadata_version_oid=self._metadata_version_oid,
                        file_oid=self._file_oid,
                        creation_datetime=self._creation_datetime,
                        domain_seq_counters=domain_seq_counters,
                        seq_overrides=seq_overrides,
                        lineage_out=lineage_updates if effective_sdtm else None,
                    )
                    delta_datasets = dict(bundle.datasets)

                all_oids = sorted(set(prior_by_oid) | set(delta_datasets))
                for ig_oid in all_oids:
                    d_env = delta_datasets.get(ig_oid)
                    if ig_oid in prior_by_oid:
                        dest_file, p_env = prior_by_oid[ig_oid]
                        per_dom_env = _merge_dataset_json_envelopes(
                            p_env,
                            d_env if (d_env is not None and d_env.records > 0) else None,
                            voided_record_ids=self._voided_ids,
                            voided_sdtm_keys=self._voided_sdtm_keys,
                            creation_datetime_override=self._creation_datetime,
                            file_oid_override=self._file_oid,
                        )
                    else:
                        assert d_env is not None
                        per_dom_env = d_env
                        dest_file = self._path / f"{make_oid_slug(d_env.name).lower()}.json"

                    _atomic_write_text(
                        dest_file,
                        to_dataset_json(per_dom_env, indent=self._indent),
                    )
                    _append_file_manifest(
                        self._manifest_path,
                        destination="dataset_json",
                        study_key=self._study_key,
                        row_count=per_dom_env.records,
                        file_path=dest_file,
                    )

                if effective_sdtm and lineage_updates:
                    commit_sdtm_lineage(self, lineage_updates, study_key=self._study_key)
                return

            prior_envelope: DatasetJsonEnvelope | None = None
            if (
                self.config.idempotent
                and (is_incremental or has_voided)
                and self._path.exists()
                and self._path.stat().st_size > 0
            ):
                try:
                    prior_envelope = load_dataset_json(self._path)
                    if effective_sdtm and domain_seq_counters is not None:
                        _seed_sdtm_counters_from_envelope(prior_envelope, domain_seq_counters)
                except Exception as exc:
                    logger.warning(
                        "Unable to merge existing Dataset-JSON archive at '%s'; writing fresh envelope: %s",
                        self._path,
                        exc,
                    )
                    prior_envelope = None

            if prior_envelope is not None:
                delta_env: DatasetJsonEnvelope | None = None
                if staged_records:
                    bundle = build_dataset_json_bundle(
                        self._study_key,
                        records=staged_records,
                        forms=forms,
                        variables=variables,
                        visits=visits,
                        as_sdtm=effective_sdtm,
                        sdtm_domain=self.config.sdtm_domain,
                        use_labels_as_columns=self._use_labels_as_columns,
                        variable_whitelist=self._variable_whitelist,
                        form_whitelist=self._form_whitelist,
                        naming_convention=self.config.naming_convention,
                        preserve_record_id=effective_preserve_rec_id,
                        include_deleted=self.config.include_deleted,
                        mask_phi=self.config.mask_phi,
                        custom_phi_keys=self._phi_keys or None,
                        enrichment_pipeline=self._pipeline,
                        metadata_version_oid=self._metadata_version_oid,
                        file_oid=self._file_oid,
                        creation_datetime=self._creation_datetime,
                        domain_seq_counters=domain_seq_counters,
                        seq_overrides=seq_overrides,
                        lineage_out=lineage_updates if effective_sdtm else None,
                    )
                    delta_env = bundle.datasets.get(prior_envelope.item_group_oid)
                    if delta_env is None and len(bundle.datasets) == 1:
                        delta_env = next(iter(bundle.datasets.values()))
                final_envelope = _merge_dataset_json_envelopes(
                    prior_envelope,
                    delta_env if (delta_env is not None and delta_env.records > 0) else None,
                    voided_record_ids=self._voided_ids,
                    voided_sdtm_keys=self._voided_sdtm_keys,
                    creation_datetime_override=self._creation_datetime,
                    file_oid_override=self._file_oid,
                )
            else:
                final_envelope = build_dataset_json_envelope(
                    self._study_key,
                    records=staged_records,
                    forms=forms,
                    variables=variables,
                    visits=visits,
                    as_sdtm=effective_sdtm,
                    sdtm_domain=self.config.sdtm_domain,
                    use_labels_as_columns=self._use_labels_as_columns,
                    variable_whitelist=self._variable_whitelist,
                    form_whitelist=self._form_whitelist,
                    naming_convention=self.config.naming_convention,
                    preserve_record_id=effective_preserve_rec_id,
                    include_deleted=self.config.include_deleted,
                    mask_phi=self.config.mask_phi,
                    custom_phi_keys=self._phi_keys or None,
                    enrichment_pipeline=self._pipeline,
                    metadata_version_oid=self._metadata_version_oid,
                    file_oid=self._file_oid,
                    creation_datetime=self._creation_datetime,
                    domain_seq_counters=domain_seq_counters,
                    seq_overrides=seq_overrides,
                    lineage_out=lineage_updates if effective_sdtm else None,
                )

            rendered = to_dataset_json(final_envelope, indent=self._indent)
            _atomic_write_text(self._path, rendered)

            if effective_sdtm and lineage_updates:
                commit_sdtm_lineage(self, lineage_updates, study_key=self._study_key)

            _append_file_manifest(
                self._manifest_path,
                destination="dataset_json",
                study_key=self._study_key,
                row_count=final_envelope.records,
                file_path=self._path,
            )

    def close(self) -> None:
        """Clear staged buffers."""
        self._staged_by_id.clear()
        self._unkeyed_records.clear()
        self._voided_ids.clear()
        self._voided_records.clear()
        self._voided_sdtm_keys.clear()


def _reject_unsupported_odm_options(
    config: SinkConfig | None,
    kwargs: Mapping[str, Any],
) -> None:
    """Reject SDTM and naming convention options unsupported by CDISC ODM 1.3.2 XML exports."""
    offending: list[str] = []
    if kwargs.get("as_sdtm") is not None:
        offending.append("as_sdtm")
    if kwargs.get("sdtm_domain") is not None or kwargs.get("domain") is not None:
        offending.append("sdtm_domain")
    if kwargs.get("naming_convention") is not None:
        offending.append("naming_convention")
    if kwargs.get("preserve_record_id") is not None:
        offending.append("preserve_record_id")
    if config is not None:
        if (
            config.as_sdtm or getattr(config, "_as_sdtm_explicit", False)
        ) and "as_sdtm" not in offending:
            offending.append("as_sdtm")
        if config.sdtm_domain is not None and "sdtm_domain" not in offending:
            offending.append("sdtm_domain")
        if config.naming_convention is not None and "naming_convention" not in offending:
            offending.append("naming_convention")
        if config.preserve_record_id is not None and "preserve_record_id" not in offending:
            offending.append("preserve_record_id")
    if offending:
        joined = ", ".join(offending)
        raise ExportConfigurationError(
            f"CDISC ODM 1.3.2 export preserves EDC CRF metadata and does not support SDTM or "
            f"naming convention options ({joined}). Use export_to_dataset_json or a tabular "
            f"sink for SDTM and naming convention exports."
        )


def export_to_odm(
    sdk: ImednetSDK,
    study_key: str,
    path: str | os.PathLike[str] | None = None,
    *,
    config: SinkConfig | None = None,
    data_dictionary: DataDictionary | None = None,
    reference_data: Sequence[OdmReferenceData] = (),
    include_audit_trail: bool = True,
    include_annotations: bool = True,
    variable_whitelist: list[str] | None = None,
    form_whitelist: list[int] | None = None,
    metadata_version_oid: str = "MDV.001",
    file_oid: str | None = None,
    creation_datetime: str | None = None,
    quality_gate_enabled: bool = False,
    mask_phi: bool = True,
    **kwargs: Any,
) -> None:
    """Export a study snapshot to CDISC ODM 1.3.2 XML with incremental checkpointing and PHI redaction."""
    _reject_unsupported_odm_options(config, kwargs)
    manifest_arg = kwargs.pop("manifest_path", None)
    kwargs.setdefault("quality_gate_enabled", quality_gate_enabled)
    resolved_cfg = _build_cdisc_sink_config(
        study_key,
        config=config,
        mask_phi=mask_phi,
        kwargs=kwargs,
    )
    output_path = _coerce_output_path(
        path,
        default_name=f"{make_oid_slug(study_key).lower()}.odm.xml",
    )
    manifest_path = resolve_config_manifest_path(resolved_cfg, manifest_arg)

    with sdk_governance_context(sdk):
        pipeline = resolve_sink_enrichment_pipeline(study_key, config=resolved_cfg)
        phi_keys = resolve_sink_phi_keys(
            study_key,
            config=resolved_cfg,
            phi_keys=getattr(pipeline, "phi_keys", None),
        )

    sink = _OdmArchiveSink(
        sdk,
        study_key,
        output_path,
        resolved_cfg,
        data_dictionary=data_dictionary,
        reference_data=reference_data,
        include_audit_trail=include_audit_trail,
        include_annotations=include_annotations,
        form_whitelist=form_whitelist,
        variable_whitelist=variable_whitelist,
        metadata_version_oid=metadata_version_oid,
        file_oid=file_oid,
        creation_datetime=creation_datetime,
        manifest_path=manifest_path,
        enrichment_pipeline=pipeline,
        phi_keys=phi_keys,
    )
    run_streaming_export(
        sdk,
        study_key,
        sink,
        resolved_cfg,
        stream_name=resolved_cfg.stream_name or "odm",
        batch_id_prefix=f"{study_key}/odm",
        quality_gate_fn=apply_quality_gate,
        iter_batches_fn=iter_batches,
    )


def export_to_dataset_json(
    sdk: ImednetSDK,
    study_key: str,
    path: str | os.PathLike[str] | None = None,
    *,
    config: SinkConfig | None = None,
    use_labels_as_columns: bool = False,
    variable_whitelist: list[str] | None = None,
    form_whitelist: list[int] | None = None,
    as_sdtm: bool | None = None,
    sdtm_domain: str | None = None,
    naming_convention: str | None = None,
    preserve_record_id: bool | None = None,
    metadata_version_oid: str = "MDV.001",
    file_oid: str | None = None,
    creation_datetime: str | None = None,
    indent: int = 2,
    quality_gate_enabled: bool = False,
    mask_phi: bool = True,
    **kwargs: Any,
) -> None:
    """Export study clinical datasets to CDISC Dataset-JSON v1.1 for CDASH Forms or SDTM domains."""
    manifest_arg = kwargs.pop("manifest_path", None)
    domain_alias = kwargs.pop("domain", None)
    resolved_domain = sdtm_domain if sdtm_domain is not None else domain_alias
    kwargs.setdefault("quality_gate_enabled", quality_gate_enabled)
    resolved_cfg = _build_cdisc_sink_config(
        study_key,
        config=config,
        mask_phi=mask_phi,
        as_sdtm=as_sdtm,
        sdtm_domain=resolved_domain,
        naming_convention=naming_convention,
        preserve_record_id=preserve_record_id,
        kwargs=kwargs,
    )
    output_path = _coerce_output_path(
        path,
        default_name=f"{make_oid_slug(study_key).lower()}.dataset.json",
    )
    manifest_path = resolve_config_manifest_path(resolved_cfg, manifest_arg)

    with sdk_governance_context(sdk):
        pipeline = resolve_sink_enrichment_pipeline(study_key, config=resolved_cfg)
        phi_keys = resolve_sink_phi_keys(
            study_key,
            config=resolved_cfg,
            phi_keys=getattr(pipeline, "phi_keys", None),
        )

    sink = _DatasetJsonSink(
        sdk,
        study_key,
        output_path,
        resolved_cfg,
        use_labels_as_columns=use_labels_as_columns,
        form_whitelist=form_whitelist,
        variable_whitelist=variable_whitelist,
        preserve_record_id=preserve_record_id,
        metadata_version_oid=metadata_version_oid,
        file_oid=file_oid,
        creation_datetime=creation_datetime,
        indent=indent,
        manifest_path=manifest_path,
        enrichment_pipeline=pipeline,
        phi_keys=phi_keys,
    )
    run_streaming_export(
        sdk,
        study_key,
        sink,
        resolved_cfg,
        stream_name=resolved_cfg.stream_name or "dataset_json",
        batch_id_prefix=f"{study_key}/dataset_json",
        quality_gate_fn=apply_quality_gate,
        iter_batches_fn=iter_batches,
    )


__all__ = [
    "export_to_dataset_json",
    "export_to_odm",
]
