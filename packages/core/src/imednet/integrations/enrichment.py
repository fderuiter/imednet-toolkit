"""Data enrichment and transformation pipeline.

This module provides the :class:`EnrichmentPipeline` for transforming raw API
data using configured mappings, terminology lookups, and PHI masking rules.
"""

import functools
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from imednet.models.engine import ResourceRegistry
from imednet.models.study_config import StudyConfiguration
from imednet.utils.evaluator import evaluate_expression
from imednet.utils.serialization import flatten

logger = logging.getLogger(__name__)

_FIRST_CAP_RE = re.compile(r'(.)([A-Z][a-z]+)')
_ALL_CAP_RE = re.compile(r'([a-z0-9])([A-Z])')


@functools.lru_cache(maxsize=1024)
def _to_snake_case(name: str) -> str:
    s1 = _FIRST_CAP_RE.sub(r'\1_\2', name)
    return _ALL_CAP_RE.sub(r'\1_\2', s1).lower()


class CentralizedMapper:
    """Unified Mapper with Enrichment Engine for data sinks."""

    _cached_fields: list[tuple[str, str]] | None = None
    _cached_field_pairs: list[tuple[str, str]] | None = None
    _DEFAULT_PIPELINE: Any = None

    def __init__(
        self,
        mode: str = "document",
        post_processor: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ):
        """Initialize the mapper.

        Args:
            mode: "tabular" for flattened structures, "document" for nested structures.
            post_processor: Optional function to apply destination-specific formatting.
        """
        self.mode = mode
        self.post_processor = post_processor
        self._pipeline_cache: dict[str, Any] = {}
        if CentralizedMapper._cached_fields is None:
            pairs = [(f, _to_snake_case(f)) for f in ResourceRegistry.get_fields("Record")]
            CentralizedMapper._cached_fields = pairs
            CentralizedMapper._cached_field_pairs = pairs

    def map_record(self, record: Any, study_key: str | None = None) -> dict[str, Any]:
        """Map a clinical record to the unified destination format."""
        fields = self._cached_fields
        if fields is None:
            fields = [(f, _to_snake_case(f)) for f in ResourceRegistry.get_fields("Record")]
            CentralizedMapper._cached_fields = fields
            CentralizedMapper._cached_field_pairs = fields

        mapped = {}
        for f, snake_key in fields:
            val = getattr(record, f, None)

            if f == "record_data":
                val = dict(val or {})
                if self.mode == "tabular":
                    val = flatten(val)
                val = {_to_snake_case(k): v for k, v in val.items()}

            if val is not None or snake_key not in mapped:
                mapped[snake_key] = val

        # Mandatory Metadata Injection
        mapped['deleted'] = getattr(record, 'deleted', False)
        if mapped['deleted'] is None:
            mapped['deleted'] = False

        date_mod = getattr(record, 'date_modified', None)
        if date_mod is None:
            date_mod = datetime.now(tz=UTC).isoformat()
        mapped['date_modified'] = date_mod

        if study_key is not None:
            mapped['study_key'] = study_key
        elif 'study_key' not in mapped or not mapped['study_key']:
            mapped['study_key'] = getattr(record, 'study_key', None)

        sk = mapped.get('study_key') or "UNKNOWN"
        if sk not in self._pipeline_cache:
            self._pipeline_cache[sk] = self._DEFAULT_PIPELINE

        mapped = self._pipeline_cache[sk].process(mapped)

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

        self.phi_keys = set(getattr(config, 'phi_fields', []))

        self.terminology = config.terminology_lookups or {}

        self.mappings = {}
        for mapping in config.mappings:
            if mapping.source_variable_name:
                self.mappings[mapping.source_variable_name] = mapping

        self._has_rules = bool(self.phi_keys or self.terminology or self.mappings)

    def process(self, data: Any) -> Any:
        """Process the input data through the enrichment pipeline.

        Args:
            data: The raw data to be enriched.

        Returns:
            The enriched and transformed data.
        """
        if not self._has_rules:
            return data
        return self._transform_recursive(data, depth=0)

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

    def _transform_recursive(self, data: Any, depth: int) -> Any:
        """Recursively transform data structures.

        Args:
            data: The data structure to transform.
            depth: The current recursion depth.

        Returns:
            The transformed data structure.
        """
        if isinstance(data, dict):
            new_dict = {}
            for k, v in data.items():
                new_key = k
                new_val = (
                    self._transform_recursive(v, depth + 1)
                    if isinstance(v, (dict, list, tuple))
                    else v
                )

                # If value is not a complex structure, apply rules
                if not isinstance(v, (dict, list, tuple)):
                    # Terminology mapping
                    # The terminology lookups are usually keyed by variable name or domain.variable
                    if k in self.terminology and str(new_val) in self.terminology[k]:
                        new_val = self.terminology[k][str(new_val)]

                    # Mapping rules (business logic and key renaming)
                    if k in self.mappings:
                        rule = self.mappings[k]
                        if rule.business_logic:
                            new_val = self._evaluate_business_logic(rule.business_logic, new_val)
                        if rule.target_field and rule.target_field != k:
                            new_key = rule.target_field

                    # PHI Masking
                    if k in self.phi_keys:
                        new_val = "[MASKED]"

                new_dict[new_key] = new_val
            return new_dict

        if isinstance(data, list):
            return [self._transform_recursive(item, depth + 1) for item in data]
        if isinstance(data, tuple):
            return tuple(self._transform_recursive(item, depth + 1) for item in data)
        return data


_DEFAULT_STUDY_CONFIG = StudyConfiguration(
    studyKey="UNKNOWN",
    version="1.0",
    reportingProfile="general",
)
_DEFAULT_PIPELINE = EnrichmentPipeline(_DEFAULT_STUDY_CONFIG)
CentralizedMapper._DEFAULT_PIPELINE = _DEFAULT_PIPELINE
