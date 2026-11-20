Schema Profiler Guide
=====================

:class:`~imednet_workflows.schema_profiler.SchemaProfiler` inspects ``Form`` and
``Variable`` definitions alongside ``Record`` payloads (from an explicit iterable
or :class:`~imednet_workflows.cached_loader.CachedRecordsLoader`) to compute
per-``Form`` completeness and type summaries.

Profiling Form and Variable Completeness
----------------------------------------

:meth:`~imednet_workflows.schema_profiler.SchemaProfiler.profile_records` returns
a ``dict[str, FormProfile]`` keyed by ``form_key``. Each
:class:`~imednet_workflows.schema_profiler.FormProfile` reports ``form_name``,
``record_count``, and a ``fields`` mapping of ``variable_name`` to
:class:`~imednet_workflows.schema_profiler.FieldProfile` (``label``,
``population_rate``, ``inferred_type``, ``unique_count``, and ``unique_values``):

.. testcode::

   from typing import Any, cast
   from imednet import ImednetSDK
   from imednet_workflows import SchemaProfiler

   sdk = ImednetSDK(simulation_mode=True)
   records = list(sdk.records.list(study_key="STUDY"))

   profiler = SchemaProfiler(cast(Any, sdk))
   profiles = profiler.profile_records("STUDY", records=records)

   for form_key, form_profile in sorted(profiles.items()):
       assert form_profile.record_count >= 1
       for var_name, field_profile in sorted(form_profile.fields.items()):
           assert 0.0 <= field_profile.population_rate <= 100.0
           assert field_profile.variable_name == var_name
