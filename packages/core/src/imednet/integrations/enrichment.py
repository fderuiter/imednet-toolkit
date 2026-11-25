"""Data enrichment and transformation pipeline.

This module provides the :class:`EnrichmentPipeline` for transforming raw API
data using configured mappings, terminology lookups, and PHI masking rules.
"""

import functools
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from importlib.metadata import entry_points
from typing import Any

from imednet.models.engine import ResourceRegistry
from imednet.models.sdtm import (
    format_variable_name,
    normalize_naming_convention,
    resolve_sdtm_domain,
    transform_records_to_sdtm_rows,
)
from imednet.models.study_config import StudyConfiguration
from imednet.utils.evaluator import evaluate_expression
from imednet.utils.security import (
    _normalize_key,
    global_sensitivity_registry,
    mask_clinical_phi,
    validate_partition_key,
)
from imednet.utils.serialization import flatten

logger = logging.getLogger(__name__)

_FIRST_CAP_RE = re.compile(r'([A-Za-z0-9])([A-Z][a-z]+)')
_ALL_CAP_RE = re.compile(r'([a-z0-9])([A-Z])')
_MASKED_VALUE = "***MASKED***"


@functools.lru_cache(maxsize=1024)
def _to_snake_case(name: str) -> str:
    s1 = _FIRST_CAP_RE.sub(r'\1_\2', name)
    return _ALL_CAP_RE.sub(r'\1_\2', s1).lower()


_CACHED_SENS_FS: frozenset[str] = frozenset()
_CACHED_EXEMPT_FS: frozenset[str] = frozenset()
_KEY_SENS_CACHE: dict[str, bool] = {}
_CLEAN_STR_KEYS: set[str] = set()
_CONTAINER_TYPES = (dict, list, tuple)
_BUILTIN_SCALARS = frozenset({str, int, float, bool, type(None)})
_BUILTIN_CONTAINERS = (dict, list, tuple)


def _is_pure_builtin_tree(value: Any) -> bool:
    """Return True iff *value* consists solely of exact builtin containers and scalars."""
    vc = value.__class__
    if vc in _BUILTIN_SCALARS:
        return True
    if vc is dict:
        return all(
            k.__class__ in _BUILTIN_SCALARS and _is_pure_builtin_tree(v) for k, v in value.items()
        )
    if vc is list or vc is tuple:
        return all(_is_pure_builtin_tree(item) for item in value)
    return False


def _get_key_sens_cache() -> dict[str, bool]:
    global _CACHED_SENS_FS, _CACHED_EXEMPT_FS
    sens = global_sensitivity_registry._sensitive_keys
    exempt = global_sensitivity_registry._exempt_keys
    if sens != _CACHED_SENS_FS or exempt != _CACHED_EXEMPT_FS:
        _CACHED_SENS_FS = frozenset(sens)
        _CACHED_EXEMPT_FS = frozenset(exempt)
        _KEY_SENS_CACHE.clear()
        _CLEAN_STR_KEYS.clear()
    elif len(_KEY_SENS_CACHE) > 4096:
        _KEY_SENS_CACHE.clear()
        _CLEAN_STR_KEYS.clear()
    return _KEY_SENS_CACHE


def _mask_default_phi_cow(value: Any, key_cache: dict[str, bool]) -> Any:
    """Mask default registry PHI keys using copy-on-write so clean payloads keep identity."""
    if value.__class__ is dict or isinstance(value, dict):
        mutated_dict: dict[Any, Any] | None = None
        cache_get = key_cache.get
        clean_add = _CLEAN_STR_KEYS.add
        for k, v in value.items():
            if k.__class__ is str or isinstance(k, str):
                is_sens = cache_get(k)
                if is_sens is None:
                    is_sens = global_sensitivity_registry.is_sensitive(k)
                    key_cache[k] = is_sens
                    if not is_sens:
                        clean_add(k)
                if is_sens:
                    if mutated_dict is None:
                        mutated_dict = dict(value)
                    mutated_dict[k] = _MASKED_VALUE
                    continue
            vc = v.__class__
            if vc is dict or vc is list or vc is tuple or isinstance(v, (dict, list, tuple)):
                new_v = _mask_default_phi_cow(v, key_cache)
                if new_v is not v:
                    if mutated_dict is None:
                        mutated_dict = dict(value)
                    mutated_dict[k] = new_v
        return mutated_dict if mutated_dict is not None else value

    if value.__class__ is list or isinstance(value, list):
        mutated_list: list[Any] | None = None
        for idx, item in enumerate(value):
            ic = item.__class__
            if ic is dict or ic is list or ic is tuple or isinstance(item, (dict, list, tuple)):
                new_item = _mask_default_phi_cow(item, key_cache)
                if new_item is not item:
                    if mutated_list is None:
                        mutated_list = list(value)
                    mutated_list[idx] = new_item
        return mutated_list if mutated_list is not None else value

    if value.__class__ is tuple or isinstance(value, tuple):
        mutated_tuple_items: list[Any] | None = None
        for idx, item in enumerate(value):
            ic = item.__class__
            if ic is dict or ic is list or ic is tuple or isinstance(item, (dict, list, tuple)):
                new_item = _mask_default_phi_cow(item, key_cache)
                if new_item is not item:
                    if mutated_tuple_items is None:
                        mutated_tuple_items = list(value)
                    mutated_tuple_items[idx] = new_item
        return tuple(mutated_tuple_items) if mutated_tuple_items is not None else value

    return value


def _matches_custom_phi_key(key: str, phi_keys: set[str]) -> bool:
    """Return True if *key* matches *phi_keys* in normalized or collapsed form."""
    if not phi_keys or global_sensitivity_registry.is_exempt(key):
        return False
    norm = _normalize_key(key)
    if not norm:
        return False
    return norm in phi_keys or norm.replace("_", "") in phi_keys


def _mask_custom_phi_recursive(value: Any, phi_keys: set[str]) -> Any:
    """Recursively mask custom normalized study PHI keys prior to flattening."""
    if not phi_keys:
        return value
    if isinstance(value, dict):
        return {
            k: (
                _MASKED_VALUE
                if isinstance(k, str) and _matches_custom_phi_key(k, phi_keys)
                else (
                    _mask_custom_phi_recursive(v, phi_keys)
                    if isinstance(v, (dict, list, tuple))
                    else v
                )
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [
            _mask_custom_phi_recursive(v, phi_keys) if isinstance(v, (dict, list, tuple)) else v
            for v in value
        ]
    if isinstance(value, tuple):
        return tuple(
            _mask_custom_phi_recursive(v, phi_keys) if isinstance(v, (dict, list, tuple)) else v
            for v in value
        )
    return value


mask_custom_phi_recursive = _mask_custom_phi_recursive


_ORIG_ENTRY_POINTS = entry_points
_CACHED_STORE_CLS: Any = None
_STORE_CLS_RESOLVED = False


def _get_config_store_cls() -> Any:
    global _CACHED_STORE_CLS, _STORE_CLS_RESOLVED
    if entry_points is not _ORIG_ENTRY_POINTS:
        stores = list(entry_points(group="imednet.stores", name="ConfigVersionStore"))
        return stores[0].load() if stores else None
    if not _STORE_CLS_RESOLVED:
        stores = list(entry_points(group="imednet.stores", name="ConfigVersionStore"))
        _CACHED_STORE_CLS = stores[0].load() if stores else None
        _STORE_CLS_RESOLVED = True
    return _CACHED_STORE_CLS


def _coerce_resolved_study_config(candidate: Any) -> StudyConfiguration | None:
    """Coerce a resolved store payload into a validated :class:`StudyConfiguration`."""
    if isinstance(candidate, StudyConfiguration):
        return candidate
    if isinstance(candidate, Mapping) and candidate:
        return StudyConfiguration.model_validate(candidate)
    return None


def _resolve_from_store_instance(store: Any, study_key: str) -> StudyConfiguration | None:
    """Resolve latest committed :class:`StudyConfiguration` via ``get_history``/``rollback_config``."""
    history = store.get_history(study_key)
    if not history or not isinstance(history, list):
        return None
    latest_entry = history[-1]
    if not isinstance(latest_entry, Mapping) or "commit_id" not in latest_entry:
        return None
    latest_commit = latest_entry["commit_id"]
    return _coerce_resolved_study_config(store.rollback_config(study_key, latest_commit))


def resolve_study_configuration(study_key: str | None) -> StudyConfiguration | None:
    """Resolve the latest committed :class:`StudyConfiguration` for *study_key* via entry points.

    Fails closed by returning ``None`` whenever no store plugin is installed, the study has no
    committed configuration history, or decryption/storage errors occur so callers still enforce
    default :data:`global_sensitivity_registry` PHI masking.
    """
    if not study_key or not isinstance(study_key, str) or study_key == "UNKNOWN":
        return None
    try:
        validate_partition_key(study_key)
        store_cls = _get_config_store_cls()
        if store_cls is None:
            return None
        fast_resolve = getattr(store_cls, "resolve_committed_config", None)
        if callable(fast_resolve):
            return _coerce_resolved_study_config(fast_resolve(study_key))
        from imednet.core.context import study_context

        store = store_cls()
        resolved = _resolve_from_store_instance(store, study_key)
        if resolved is not None:
            return resolved
        with study_context(study_key):
            legacy_store = store_cls()
            return _resolve_from_store_instance(legacy_store, study_key)
    except Exception as exc:
        logger.debug("Could not resolve StudyConfiguration for %r: %s", study_key, exc)
    return None


def resolve_enrichment_pipeline(study_key: str | None) -> "EnrichmentPipeline | None":
    """Return an :class:`EnrichmentPipeline` for *study_key* when committed rules exist."""
    study_cfg = resolve_study_configuration(study_key)
    if study_cfg is None:
        return None
    pipeline = EnrichmentPipeline(study_cfg)
    return pipeline if pipeline._has_rules else None


class CentralizedMapper:
    """Unified Mapper with Enrichment Engine for data sinks."""

    _cached_fields: list[tuple[str, str]] | None = None
    _cached_field_pairs: list[tuple[str, str]] | None = None
    _DEFAULT_PIPELINE: Any = None

    def __init__(
        self,
        mode: str = "document",
        post_processor: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        *,
        mask_phi: bool = True,
        study_config: StudyConfiguration | None = None,
        naming_convention: str | None = None,
        as_sdtm: bool = False,
        sdtm_domain: str | None = None,
        seq_counters: dict[str, int] | None = None,
        domain_seq_counters: dict[str, dict[str, int]] | None = None,
        form_domain_map: Mapping[Any, str] | None = None,
        seq_overrides: Mapping[str, int] | None = None,
        lineage_out: dict[str, dict[str, Any]] | None = None,
        record_seqs: dict[str, int] | None = None,
        preserve_record_id: bool = False,
    ):
        """Initialize the mapper.

        Args:
            mode: "tabular" for flattened structures, "document" for nested structures.
            post_processor: Optional function to apply destination-specific formatting.
            mask_phi: Whether to mask sensitive PHI fields by default.
            study_config: Optional study configuration containing custom enrichment rules.
            naming_convention: Optional key convention ('snake_case', 'camelCase', 'sdtm').
            as_sdtm: When True, transform records directly into CDISC SDTM domain rows.
            sdtm_domain: Optional SDTM domain code (e.g. 'AE', 'VS', 'DM').
            seq_counters: Optional per-USUBJID sequence counter map shared across batches.
            domain_seq_counters: Optional per-domain sequence counter map for multi-Form SDTM.
            form_domain_map: Optional lookup from form_id / form_key to SDTM domain code.
            seq_overrides: Optional rehydrated record_id -> <DOM>SEQ overrides.
            lineage_out: Optional dictionary populated with record_id -> SDTM lineage metadata.
            record_seqs: Optional per-Record sequence map for idempotent incremental reruns.
            preserve_record_id: When True, retain RECORD_ID in transformed SDTM rows.
        """
        self.mode = mode
        self.post_processor = post_processor
        self.mask_phi = mask_phi
        self.naming_convention = normalize_naming_convention(naming_convention)
        self.sdtm_domain = (
            resolve_sdtm_domain(sdtm_domain)
            if isinstance(sdtm_domain, str) and sdtm_domain.strip()
            else None
        )
        self.as_sdtm = bool(as_sdtm or self.sdtm_domain is not None)
        self._explicit_seq_counters = seq_counters is not None
        self._seq_counters: dict[str, int] = seq_counters if seq_counters is not None else {}
        self._domain_seq_counters: dict[str, dict[str, int]] = (
            domain_seq_counters if domain_seq_counters is not None else {}
        )
        if self.sdtm_domain is not None and seq_counters is not None:
            existing = self._domain_seq_counters.get(self.sdtm_domain)
            if existing and existing is not seq_counters:
                for subj, seq_val in existing.items():
                    seq_counters[subj] = max(seq_counters.get(subj, 0), seq_val)
            self._domain_seq_counters[self.sdtm_domain] = seq_counters
        self._form_domain_map: Mapping[Any, str] = (
            form_domain_map if form_domain_map is not None else {}
        )
        self._seq_overrides: Mapping[str, int] | None = seq_overrides
        self._lineage_out: dict[str, dict[str, Any]] | None = lineage_out
        self._record_seqs: dict[str, int] | None = record_seqs
        self._preserve_record_id = bool(preserve_record_id)
        self._pipeline_cache: dict[str, Any] = {}
        self._sens_snapshot: frozenset[str] = frozenset()
        self._exempt_snapshot: frozenset[str] = frozenset()
        self._masked_meta_cache: dict[int, frozenset[str]] = {}
        if study_config is not None:
            custom_pipeline = EnrichmentPipeline(study_config)
            self._default_pipeline: Any = custom_pipeline
            if study_config.study_key:
                self._pipeline_cache[study_config.study_key] = custom_pipeline
        else:
            self._default_pipeline = None
        if CentralizedMapper._cached_fields is None:
            pairs = [(f, _to_snake_case(f)) for f in ResourceRegistry.get_fields("Record")]
            CentralizedMapper._cached_fields = pairs
            CentralizedMapper._cached_field_pairs = pairs

    def register_pipeline(self, study_key: str, pipeline: Any) -> None:
        """Register an explicit enrichment pipeline for *study_key*."""
        self._pipeline_cache[study_key] = pipeline

    def _resolve_masked_meta_fields(
        self,
        active_pipeline: Any,
        fields: list[tuple[str, str]],
        pipeline_phi_keys: set[str],
    ) -> frozenset[str]:
        """Return canonical metadata snake_keys that must be PHI-masked for *active_pipeline*."""
        sens = global_sensitivity_registry._sensitive_keys
        exempt = global_sensitivity_registry._exempt_keys
        if sens != self._sens_snapshot or exempt != self._exempt_snapshot:
            self._sens_snapshot = frozenset(sens)
            self._exempt_snapshot = frozenset(exempt)
            self._masked_meta_cache.clear()
        cache_key = id(active_pipeline)
        cached = self._masked_meta_cache.get(cache_key)
        if cached is None:
            masked: set[str] = set()
            for f, snake_key in fields:
                if f == "record_data":
                    continue
                if (
                    pipeline_phi_keys and _matches_custom_phi_key(snake_key, pipeline_phi_keys)
                ) or global_sensitivity_registry.is_sensitive(snake_key):
                    masked.add(snake_key)
            cached = frozenset(masked)
            self._masked_meta_cache[cache_key] = cached
        return cached

    def _resolve_pipeline_for_study(self, sk: str) -> Any:
        """Resolve and cache the active ``EnrichmentPipeline`` for *sk*."""
        if sk not in self._pipeline_cache:
            if self._default_pipeline is not None:
                self._pipeline_cache[sk] = self._default_pipeline
            else:
                sdk_obj = getattr(self, "sdk", None)
                if sdk_obj is not None:
                    from imednet.core.context import sdk_governance_context

                    with sdk_governance_context(sdk_obj):
                        resolved_cfg = resolve_study_configuration(sk)
                else:
                    resolved_cfg = resolve_study_configuration(sk)
                self._pipeline_cache[sk] = (
                    EnrichmentPipeline(resolved_cfg)
                    if resolved_cfg is not None
                    else self._DEFAULT_PIPELINE
                )
        return self._pipeline_cache[sk]

    def _resolve_record_sdtm_domain(
        self,
        record: Any,
        *,
        study_key: str | None = None,
        study_config: Any | None = None,
    ) -> str:
        """Resolve the target SDTM domain for *record* using explicit domain, Form metadata, or StudyConfiguration."""
        if self.sdtm_domain:
            return self.sdtm_domain
        from imednet.models.sdtm import resolve_form_sdtm_domain

        resolved_cfg = study_config
        if resolved_cfg is None and self._default_pipeline is not None:
            resolved_cfg = getattr(self._default_pipeline, "config", None)
        if resolved_cfg is None and study_key:
            active_pipe = self._resolve_pipeline_for_study(study_key)
            if active_pipe is not None and active_pipe is not self._DEFAULT_PIPELINE:
                resolved_cfg = getattr(active_pipe, "config", None)

        dom = resolve_form_sdtm_domain(
            record,
            study_config=resolved_cfg,
            form_domain_map=self._form_domain_map,
            fallback="",
        )
        if dom:
            return dom
        is_mapping = isinstance(record, Mapping)
        raw_fk = (
            record.get("form_key") or record.get("formKey")
            if is_mapping
            else getattr(record, "form_key", None) or getattr(record, "formKey", None)
        )
        return resolve_sdtm_domain(raw_fk, fallback="AE")

    def _counters_for_domain(self, domain: str) -> dict[str, int]:
        """Return the per-USUBJID sequence counter map scoped to ``domain``."""
        if self.sdtm_domain is not None and self._explicit_seq_counters:
            return self._seq_counters
        return self._domain_seq_counters.setdefault(domain, {})

    def map_batch(
        self, records: Sequence[Any], study_key: str | None = None
    ) -> list[dict[str, Any]]:
        """Map a batch of records, sorting and sequencing SDTM batches per domain and USUBJID."""
        if not self.as_sdtm:
            return [self.map_record(r, study_key=study_key) for r in records]
        return [doc for _src, doc in self.map_batch_with_sources(records, study_key=study_key)]

    def map_batch_with_sources(
        self,
        records: Sequence[Any],
        study_key: str | None = None,
    ) -> list[tuple[Any, dict[str, Any]]]:
        """Map an SDTM batch returning ``(source_record, sdtm_row)`` pairs in finalized order."""
        if not records:
            return []
        if not self.as_sdtm:
            return [(r, self.map_record(r, study_key=study_key)) for r in records]

        from imednet.models.sdtm import _PreparedSdtmRow, _SdtmNormalizer

        # Partition records by (study_key, target_domain) while preserving first-seen partition order
        partitions: dict[tuple[str, str], list[tuple[int, Any]]] = {}
        partition_order: list[tuple[str, str]] = []
        for idx, rec in enumerate(records):
            is_mapping = isinstance(rec, Mapping)
            resolved_sk = (
                study_key
                if study_key is not None
                else (
                    rec.get("study_key") or rec.get("studyKey") or rec.get("STUDYID")
                    if is_mapping
                    else getattr(rec, "study_key", None)
                )
            ) or "UNKNOWN"
            dom = self._resolve_record_sdtm_domain(rec, study_key=str(resolved_sk))
            key = (str(resolved_sk), dom)
            if key not in partitions:
                partitions[key] = []
                partition_order.append(key)
            partitions[key].append((idx, rec))

        results: list[tuple[Any, dict[str, Any]]] = []
        for sk, dom in partition_order:
            active_pipeline = self._resolve_pipeline_for_study(sk)
            pipeline_phi_keys: set[str] = getattr(active_pipeline, "phi_keys", set())
            has_active_rules = bool(
                active_pipeline is not None
                and not (
                    active_pipeline.__class__ is EnrichmentPipeline
                    and not active_pipeline._has_rules
                )
            )
            normalizer = _SdtmNormalizer(
                domain=dom,
                study_key=sk,
                include_deleted=True,
                mask_phi=self.mask_phi,
                preserve_record_id=self._preserve_record_id,
                pipeline=active_pipeline if has_active_rules else None,
                phi_keys=pipeline_phi_keys or None,
            )
            counters = self._counters_for_domain(dom)
            grouped: dict[str, list[_PreparedSdtmRow]] = {}
            source_by_row_id: dict[int, Any] = {}
            for orig_idx, src_rec in partitions[(sk, dom)]:
                prepared = normalizer.prepare_record(src_rec, orig_idx)
                if prepared is not None:
                    source_by_row_id[id(prepared.row)] = src_rec
                    grouped.setdefault(prepared.usubjid, []).append(prepared)
            for usubjid in sorted(grouped):
                finalized_rows = normalizer.finalize_subject_group(
                    grouped[usubjid],
                    counters,
                    record_seqs=self._record_seqs,
                    seq_overrides=self._seq_overrides,
                    lineage_out=self._lineage_out,
                )
                for row_dict in finalized_rows:
                    src = source_by_row_id.get(id(row_dict))
                    out_row = self.post_processor(row_dict) if self.post_processor else row_dict
                    results.append((src, out_row))
        return results

    def map_record(self, record: Any, study_key: str | None = None) -> dict[str, Any]:
        """Map a clinical record to the unified destination format."""
        fields = self._cached_fields
        if fields is None:
            fields = [(f, _to_snake_case(f)) for f in ResourceRegistry.get_fields("Record")]
            CentralizedMapper._cached_fields = fields
            CentralizedMapper._cached_field_pairs = fields

        is_mapping = isinstance(record, Mapping)
        resolved_study_key = (
            study_key
            if study_key is not None
            else (
                record.get("study_key") or record.get("studyKey")
                if is_mapping
                else getattr(record, "study_key", None)
            )
        )
        sk = resolved_study_key or "UNKNOWN"
        active_pipeline = self._resolve_pipeline_for_study(sk)
        pipeline_phi_keys: set[str] = getattr(active_pipeline, "phi_keys", set())
        has_active_rules = bool(
            active_pipeline is not None
            and not (
                active_pipeline.__class__ is EnrichmentPipeline and not active_pipeline._has_rules
            )
        )
        masked_meta_fields: frozenset[str] | tuple[()] = (
            self._resolve_masked_meta_fields(active_pipeline, fields, pipeline_phi_keys)
            if self.mask_phi
            else ()
        )

        if self.as_sdtm:
            target_domain = self._resolve_record_sdtm_domain(record, study_key=sk)
            domain_counters = self._counters_for_domain(target_domain)
            sdtm_rows = transform_records_to_sdtm_rows(
                [record],
                domain=target_domain,
                study_key=sk,
                include_deleted=True,
                mask_phi=self.mask_phi,
                seq_counters=domain_counters,
                record_seqs=self._record_seqs,
                preserve_record_id=self._preserve_record_id,
                seq_overrides=self._seq_overrides,
                lineage_out=self._lineage_out,
                phi_keys=pipeline_phi_keys or None,
                pipeline=active_pipeline if has_active_rules else None,
            )
            mapped_sdtm = (
                sdtm_rows[0]
                if sdtm_rows
                else {
                    "STUDYID": sk,
                    "DOMAIN": target_domain,
                    "USUBJID": "",
                }
            )
            if self.post_processor:
                mapped_sdtm = self.post_processor(mapped_sdtm)
            return mapped_sdtm

        active_conv = self.naming_convention
        mapped: dict[str, Any] = {}
        for f, snake_key in fields:
            if is_mapping:
                val = record.get(f)
                if val is None and snake_key != f:
                    val = record.get(snake_key)
                if val is None:
                    camel_f = format_variable_name(snake_key, "camelCase")
                    if camel_f not in {f, snake_key}:
                        val = record.get(camel_f)
                if val is None and active_conv == "sdtm":
                    sdtm_f = format_variable_name(
                        snake_key,
                        "sdtm",
                        domain=self.sdtm_domain,
                        is_metadata=True,
                    )
                    if sdtm_f not in {f, snake_key}:
                        val = record.get(sdtm_f)
            else:
                val = getattr(record, f, None)

            if f == "record_data":
                val = dict(val or {})
                if self.mask_phi and val:
                    val = mask_clinical_phi(val)
                if has_active_rules:
                    rec_fk = (
                        record.get("form_key") or record.get("formKey")
                        if is_mapping
                        else getattr(record, "form_key", None) or getattr(record, "formKey", None)
                    )
                    rec_dom = self.sdtm_domain if self.sdtm_domain else None
                    if self.mask_phi:
                        try:
                            val = active_pipeline.process(
                                val,
                                _skip_default_phi=False,
                                domain=rec_dom,
                                form_key=str(rec_fk) if rec_fk else None,
                            )
                        except TypeError:
                            try:
                                val = active_pipeline.process(val, _skip_default_phi=False)
                            except TypeError:
                                val = active_pipeline.process(val)
                    else:
                        try:
                            val = active_pipeline.process(
                                val,
                                mask_phi=False,
                                domain=rec_dom,
                                form_key=str(rec_fk) if rec_fk else None,
                            )
                        except TypeError:
                            try:
                                val = active_pipeline.process(val, mask_phi=False)
                            except TypeError:
                                val = active_pipeline.process(val)
                if self.mode == "tabular":
                    val = flatten(val)
                if active_conv is None or active_conv == "snake_case":
                    val = {_to_snake_case(k): v for k, v in val.items()}
                else:
                    val = {
                        format_variable_name(k, active_conv, domain=self.sdtm_domain): v
                        for k, v in val.items()
                    }
            elif masked_meta_fields and val is not None and snake_key in masked_meta_fields:
                val = _MASKED_VALUE

            if val is not None or snake_key not in mapped:
                mapped[snake_key] = val

        # Mandatory Metadata Injection
        mapped['deleted'] = (
            record.get('deleted', False) if is_mapping else getattr(record, 'deleted', False)
        )
        if mapped['deleted'] is None:
            mapped['deleted'] = False

        date_mod = (
            record.get('date_modified') or record.get('dateModified')
            if is_mapping
            else getattr(record, 'date_modified', None)
        )
        if date_mod is None:
            date_mod = datetime.now(tz=UTC).isoformat()
        mapped['date_modified'] = date_mod

        if study_key is not None:
            mapped['study_key'] = study_key
        elif 'study_key' not in mapped or not mapped['study_key']:
            mapped['study_key'] = resolved_study_key

        if active_conv is not None and active_conv != "snake_case":
            converted: dict[str, Any] = {}
            for mk, mv in mapped.items():
                target_mk = format_variable_name(
                    mk,
                    active_conv,
                    domain=self.sdtm_domain,
                    is_metadata=True,
                )
                if mv is not None or target_mk not in converted:
                    converted[target_mk] = mv
            mapped = converted

        if self.post_processor:
            mapped = self.post_processor(mapped)

        return mapped


class EnrichmentPipeline:
    """Pipeline for transforming and enriching study data.

    Applies a set of rules defined in a :class:`StudyConfiguration` to data
    objects, including PHI masking, terminology mapping, and custom business logic.
    """

    def __init__(self, config: StudyConfiguration):
        """Initialize the enrichment pipeline.

        Args:
            config: The study configuration containing enrichment rules.
        """
        self.config = config

        raw_phi_fields = getattr(config, "phi_fields", []) or []
        self.phi_keys: set[str] = set()
        for raw_key in raw_phi_fields:
            if global_sensitivity_registry.is_exempt(raw_key):
                continue
            norm_key = _normalize_key(raw_key)
            if norm_key:
                self.phi_keys.add(norm_key)
                self.phi_keys.add(norm_key.replace("_", ""))
            snake_norm = _normalize_key(_to_snake_case(str(raw_key)))
            if snake_norm:
                self.phi_keys.add(snake_norm)
                self.phi_keys.add(snake_norm.replace("_", ""))

        self.terminology = config.terminology_lookups or {}
        self._snake_terminology: dict[str, dict[str, str]] = {}
        for term_key, lookup in self.terminology.items():
            snake_term_key = _to_snake_case(str(term_key))
            if snake_term_key and snake_term_key not in self._snake_terminology:
                self._snake_terminology[snake_term_key] = lookup

        self.mappings: dict[str, Any] = {}
        self._snake_mappings: dict[str, Any] = {}
        self._var_rule_counts: dict[str, int] = {}
        self._snake_rule_counts: dict[str, int] = {}
        self.all_mappings: list[Any] = []
        self.indexed_mappings: dict[tuple[str, str, str], Any] = {}
        self._indexed_snake_mappings: dict[tuple[str, str, str], Any] = {}
        self._domain_var_mappings: dict[tuple[str, str], Any] = {}
        self._domain_snake_mappings: dict[tuple[str, str], Any] = {}
        self._form_var_mappings: dict[tuple[str, str], Any] = {}
        self._form_snake_mappings: dict[tuple[str, str], Any] = {}

        for mapping in config.mappings:
            if mapping.source_variable_name:
                src_var = mapping.source_variable_name
                snake_src = _to_snake_case(src_var)
                norm_dom = (
                    resolve_sdtm_domain(mapping.domain, fallback="") if mapping.domain else ""
                )
                raw_fk = mapping.source_form_key or ""
                norm_fk = _normalize_key(raw_fk).replace("_", "") if raw_fk else ""

                self.all_mappings.append(mapping)
                self.mappings.setdefault(src_var, mapping)
                self._var_rule_counts[src_var] = self._var_rule_counts.get(src_var, 0) + 1
                if snake_src:
                    self._snake_mappings.setdefault(snake_src, mapping)
                    self._snake_rule_counts[snake_src] = (
                        self._snake_rule_counts.get(snake_src, 0) + 1
                    )

                self.indexed_mappings[(norm_dom, norm_fk, src_var)] = mapping
                if raw_fk and raw_fk != norm_fk:
                    self.indexed_mappings.setdefault((norm_dom, raw_fk, src_var), mapping)
                if snake_src:
                    self._indexed_snake_mappings.setdefault((norm_dom, norm_fk, snake_src), mapping)

                if norm_dom:
                    self._domain_var_mappings.setdefault((norm_dom, src_var), mapping)
                    if snake_src:
                        self._domain_snake_mappings.setdefault((norm_dom, snake_src), mapping)
                if norm_fk:
                    self._form_var_mappings.setdefault((norm_fk, src_var), mapping)
                    if snake_src:
                        self._form_snake_mappings.setdefault((norm_fk, snake_src), mapping)

        self._has_rules = bool(self.phi_keys or self.terminology or self.all_mappings)
        self._last_clean_repr: str | None = None

    @staticmethod
    def _is_empty_input(val: Any) -> bool:
        """Return True when *val* is ``None`` or an empty/whitespace-only string."""
        return val is None or (isinstance(val, str) and not val.strip())

    def _resolve_rule_for_key(
        self,
        key: str,
        snake_key: str | None,
        *,
        norm_domain: str | None,
        norm_form_key: str | None,
    ) -> Any | None:
        """Look up a ``MappingRule`` scoped by ``(domain, source_form_key, source_variable_name)``."""
        if norm_domain and norm_form_key:
            rule = self.indexed_mappings.get((norm_domain, norm_form_key, key))
            if rule is None and snake_key:
                rule = self._indexed_snake_mappings.get((norm_domain, norm_form_key, snake_key))
            if rule is not None:
                return rule
            # Fall back to domain-scoped rule when form_key is a generic/study form alias for norm_domain
            # and no conflicting form-specific rule matched.
            dom_rule = self._domain_var_mappings.get((norm_domain, key))
            if dom_rule is None and snake_key:
                dom_rule = self._domain_snake_mappings.get((norm_domain, snake_key))
            if dom_rule is not None:
                rule_fk = getattr(dom_rule, "source_form_key", "") or ""
                rule_fk_norm = _normalize_key(rule_fk).replace("_", "") if rule_fk else ""
                if not rule_fk_norm or resolve_sdtm_domain(rule_fk, fallback="") == norm_domain:
                    return dom_rule
            return None

        if norm_domain:
            rule = self._domain_var_mappings.get((norm_domain, key))
            if rule is None and snake_key:
                rule = self._domain_snake_mappings.get((norm_domain, snake_key))
            return rule

        if norm_form_key:
            rule = self._form_var_mappings.get((norm_form_key, key))
            if rule is None and snake_key:
                rule = self._form_snake_mappings.get((norm_form_key, snake_key))
            if rule is not None:
                return rule
            from imednet.models.sdtm import resolve_form_sdtm_domain

            inferred_dom = resolve_form_sdtm_domain(
                norm_form_key, study_config=self.config, fallback=""
            )
            if inferred_dom:
                dom_rule = self._domain_var_mappings.get((inferred_dom, key))
                if dom_rule is None and snake_key:
                    dom_rule = self._domain_snake_mappings.get((inferred_dom, snake_key))
                if dom_rule is not None:
                    rule_fk = getattr(dom_rule, "source_form_key", "") or ""
                    rule_fk_norm = _normalize_key(rule_fk).replace("_", "") if rule_fk else ""
                    if (
                        not rule_fk_norm
                        or rule_fk_norm == norm_form_key
                        or resolve_sdtm_domain(rule_fk, fallback="") == inferred_dom
                    ):
                        return dom_rule
            # In non-SDTM mode (norm_domain is None), allow unambiguous single-variable rules
            if self._var_rule_counts.get(key, 0) == 1:
                return self.mappings.get(key)
            if snake_key and self._snake_rule_counts.get(snake_key, 0) == 1:
                return self._snake_mappings.get(snake_key)
            return None

        rule = self.mappings.get(key)
        if rule is None and snake_key is not None:
            rule = self._snake_mappings.get(snake_key)
        return rule

    def _applicable_fallback_rules(
        self,
        *,
        norm_domain: str | None,
        norm_form_key: str | None,
    ) -> list[Any]:
        """Return rules with ``fallback_value`` applicable to ``(norm_domain, norm_form_key)``."""
        candidates: list[Any] = []
        for rule in self.all_mappings:
            if rule.fallback_value is None:
                continue
            r_dom = resolve_sdtm_domain(rule.domain, fallback="") if rule.domain else ""
            r_fk_raw = rule.source_form_key or ""
            r_fk = _normalize_key(r_fk_raw).replace("_", "") if r_fk_raw else ""

            if norm_domain and r_dom and r_dom != norm_domain:
                continue
            if (
                norm_form_key
                and r_fk
                and r_fk != norm_form_key
                and not (norm_domain and resolve_sdtm_domain(r_fk_raw, fallback="") == norm_domain)
            ):
                continue
            candidates.append(rule)
        return candidates

    def process(
        self,
        data: Any,
        *,
        mask_phi: bool = True,
        _skip_default_phi: bool = False,
        domain: str | None = None,
        form_key: str | None = None,
    ) -> Any:
        """Process the input data through the enrichment pipeline.

        Args:
            data: The raw data to be enriched.
            mask_phi: Whether default and configured PHI rules should be applied.
            domain: Optional SDTM domain scoping ``MappingRule`` resolution.
            form_key: Optional EDC Form key scoping ``MappingRule`` resolution.

        Returns:
            The enriched and transformed data.
        """
        if not self._has_rules:
            if not mask_phi or _skip_default_phi or not isinstance(data, _CONTAINER_TYPES):
                return data
            if (
                self._last_clean_repr is not None
                and repr(data) == self._last_clean_repr
                and global_sensitivity_registry._sensitive_keys == _CACHED_SENS_FS
                and global_sensitivity_registry._exempt_keys == _CACHED_EXEMPT_FS
            ):
                return data
            res = _mask_default_phi_cow(data, _get_key_sens_cache())
            if res is data and _is_pure_builtin_tree(data):
                self._last_clean_repr = repr(data)
            return res
        if not mask_phi and not (self.terminology or self.all_mappings):
            return data

        raw_dom = domain
        raw_fk = form_key
        if isinstance(data, Mapping):
            if not raw_dom:
                cand_dom = data.get("DOMAIN") or data.get("domain")
                if isinstance(cand_dom, str) and cand_dom.strip():
                    raw_dom = cand_dom
            if not raw_fk:
                cand_fk = data.get("form_key") or data.get("formKey")
                if isinstance(cand_fk, str) and cand_fk.strip():
                    raw_fk = cand_fk

        norm_dom = (
            resolve_sdtm_domain(raw_dom, fallback="")
            if isinstance(raw_dom, str) and raw_dom.strip()
            else None
        ) or None
        norm_fk = (
            _normalize_key(raw_fk).replace("_", "")
            if isinstance(raw_fk, str) and raw_fk.strip()
            else None
        ) or None

        return self._transform_recursive(
            data,
            depth=0,
            mask_phi=mask_phi,
            _skip_default_phi=_skip_default_phi,
            norm_domain=norm_dom,
            norm_form_key=norm_fk,
        )

    def _evaluate_business_logic(self, expr: str, value: Any) -> Any:
        """Evaluate a business logic expression on a value.

        Args:
            expr: The python expression to evaluate.
            value: The current value.

        Returns:
            The result of the evaluated expression, or the original value if evaluation fails.
        """
        try:
            env = {"value": value}
            return evaluate_expression(expr, env, default=value, raise_on_error=False)
        except Exception as e:
            logger.warning(
                f"Business logic evaluation failed for expr '{expr}' with value '{value}': {e}"
            )
            return value

    def _transform_recursive(
        self,
        data: Any,
        depth: int,
        *,
        mask_phi: bool = True,
        _skip_default_phi: bool = False,
        norm_domain: str | None = None,
        norm_form_key: str | None = None,
    ) -> Any:
        """Recursively transform data structures."""
        if isinstance(data, dict):
            new_dict: dict[str, Any] = {}
            for k, v in data.items():
                new_key = k
                is_phi_key = False
                if mask_phi and isinstance(k, str) and not global_sensitivity_registry.is_exempt(k):
                    is_phi_key = _matches_custom_phi_key(k, self.phi_keys) or (
                        not _skip_default_phi and global_sensitivity_registry.is_sensitive(k)
                    )
                if is_phi_key and isinstance(v, (dict, list, tuple)):
                    new_dict[new_key] = _MASKED_VALUE
                    continue

                new_val = (
                    self._transform_recursive(
                        v,
                        depth + 1,
                        mask_phi=mask_phi,
                        _skip_default_phi=_skip_default_phi,
                        norm_domain=norm_domain,
                        norm_form_key=norm_form_key,
                    )
                    if isinstance(v, (dict, list, tuple))
                    else v
                )

                # If value is not a complex structure, apply rules
                if not isinstance(v, (dict, list, tuple)):
                    snake_k = _to_snake_case(k) if isinstance(k, str) else None
                    rule = (
                        self._resolve_rule_for_key(
                            k,
                            snake_k,
                            norm_domain=norm_domain,
                            norm_form_key=norm_form_key,
                        )
                        if isinstance(k, str)
                        else None
                    )
                    used_fallback = False
                    if (
                        rule is not None
                        and rule.fallback_value is not None
                        and self._is_empty_input(new_val)
                    ):
                        new_val = rule.fallback_value
                        used_fallback = True

                    # Terminology mapping: domain.target/source first, then exact sourceVariableName, then snake_case
                    term_lookup = None
                    if isinstance(k, str):
                        if norm_domain and f"{norm_domain}.{k}" in self.terminology:
                            term_lookup = self.terminology[f"{norm_domain}.{k}"]
                        else:
                            term_lookup = self.terminology.get(k)
                        if term_lookup is None and snake_k is not None:
                            term_lookup = self._snake_terminology.get(snake_k)
                    if not is_phi_key and term_lookup is not None and str(new_val) in term_lookup:
                        new_val = term_lookup[str(new_val)]

                    # Mapping rules (business logic and key renaming)
                    if rule is not None:
                        if not is_phi_key and rule.business_logic and not used_fallback:
                            new_val = self._evaluate_business_logic(rule.business_logic, new_val)
                            if rule.fallback_value is not None and self._is_empty_input(new_val):
                                new_val = rule.fallback_value
                        if rule.target_field and rule.target_field != k:
                            new_key = rule.target_field

                    # PHI Masking
                    target_is_phi = False
                    if (
                        mask_phi
                        and isinstance(new_key, str)
                        and not global_sensitivity_registry.is_exempt(new_key)
                    ):
                        target_is_phi = _matches_custom_phi_key(new_key, self.phi_keys) or (
                            not _skip_default_phi
                            and global_sensitivity_registry.is_sensitive(new_key)
                        )
                    if is_phi_key or target_is_phi:
                        new_val = _MASKED_VALUE

                new_dict[new_key] = new_val

            if depth == 0 and self.all_mappings:
                for fb_rule in self._applicable_fallback_rules(
                    norm_domain=norm_domain,
                    norm_form_key=norm_form_key,
                ):
                    target_k = fb_rule.target_field or fb_rule.source_variable_name
                    if not target_k:
                        continue
                    if target_k not in new_dict or self._is_empty_input(new_dict.get(target_k)):
                        fb_val = fb_rule.fallback_value
                        if (
                            mask_phi
                            and isinstance(target_k, str)
                            and not global_sensitivity_registry.is_exempt(target_k)
                            and (
                                _matches_custom_phi_key(target_k, self.phi_keys)
                                or (
                                    not _skip_default_phi
                                    and global_sensitivity_registry.is_sensitive(target_k)
                                )
                            )
                        ):
                            fb_val = _MASKED_VALUE
                        new_dict[target_k] = fb_val
            return new_dict

        if isinstance(data, list):
            return [
                self._transform_recursive(
                    item,
                    depth + 1,
                    mask_phi=mask_phi,
                    _skip_default_phi=_skip_default_phi,
                    norm_domain=norm_domain,
                    norm_form_key=norm_form_key,
                )
                for item in data
            ]
        if isinstance(data, tuple):
            return tuple(
                self._transform_recursive(
                    item,
                    depth + 1,
                    mask_phi=mask_phi,
                    _skip_default_phi=_skip_default_phi,
                    norm_domain=norm_domain,
                    norm_form_key=norm_form_key,
                )
                for item in data
            )
        return data


_DEFAULT_STUDY_CONFIG = StudyConfiguration(
    studyKey="UNKNOWN",
    version="1.0",
    reportingProfile="general",
)
_DEFAULT_PIPELINE = EnrichmentPipeline(_DEFAULT_STUDY_CONFIG)
CentralizedMapper._DEFAULT_PIPELINE = _DEFAULT_PIPELINE
