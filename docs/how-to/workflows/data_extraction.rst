Data Extraction Workflow
========================

The :class:`~imednet_workflows.data_extraction.DataExtractionWorkflow` plugin
(registered under ``imednet.workflows`` as ``data_extraction`` and accessible via
``sdk.workflows.data_extraction``) combines filtering across ``Subject``, ``Visit``,
``Record``, and ``RecordRevision`` endpoints into single declarative calls.

Extracting Records Across Subject, Visit, and Record Criteria
-------------------------------------------------------------

:meth:`~imednet_workflows.data_extraction.DataExtractionWorkflow.extract_records_by_criteria`
evaluates ``subject_filter`` against ``SubjectsEndpoint``, ``visit_filter`` against
``VisitsEndpoint``, and ``record_filter`` against ``RecordsEndpoint``, intersecting
matched ``subject_key`` and ``visit_id`` sets automatically. Voided
(``deleted=True``) ``Record`` items are excluded by default unless
``include_deleted=True`` is supplied.

.. testcode::

   from typing import Any, cast
   from imednet import ImednetSDK
   from imednet_workflows.data_extraction import DataExtractionWorkflow

   sdk = ImednetSDK(simulation_mode=True)
   workflow = DataExtractionWorkflow(cast(Any, sdk))

   progress_messages: list[str] = []
   records = workflow.extract_records_by_criteria(
       study_key="STUDY",
       record_filter={"form_key": "F_VS"},
       subject_filter={"site_name": "SITE"},
       visit_filter={"interval_name": "Baseline"},
       on_progress=progress_messages.append,
       include_deleted=False,
   )
   print(len(records), len(progress_messages))

Extracting 21 CFR Part 11 Audit Trails
--------------------------------------

Use :meth:`~imednet_workflows.data_extraction.DataExtractionWorkflow.extract_audit_trail`
to fetch :class:`~imednet.models.record_revisions.RecordRevision` entries bounded
by UTC ``start_date`` and ``end_date`` timestamps alongside optional ``User`` or
``Form`` filters:

.. testcode::

   from typing import Any, cast
   from imednet import ImednetSDK
   from imednet_workflows.data_extraction import DataExtractionWorkflow

   audit_sdk = ImednetSDK(simulation_mode=True)
   audit_workflow = DataExtractionWorkflow(cast(Any, audit_sdk))

   revisions = audit_workflow.extract_audit_trail(
       study_key="STUDY",
       start_date="2025-01-01",
       end_date="2026-12-31",
       user_filter={"form_key": "F_VS"},
   )
   print(len(revisions))

Combining Extraction with ``RecordMapper``
------------------------------------------

To project extracted ``Record`` items into analytical DataFrames or CDISC SDTM
columns, combine ``sdk.workflows.data_extraction`` with
:class:`~imednet_workflows.record_mapper.RecordMapper` (``sdk.workflows.record_mapper``);
see :doc:`/how-to/record_mapping` for column-mapping details.

Canonical ``MappingRule`` Derivations and AST Sandboxing
--------------------------------------------------------

For longitudinal domain extraction across ``Study``, ``Subject``, ``Site``, ``Form``,
``Record``, ``Visit``, ``Interval``, ``Variable``, ``Query``, ``Data Dictionary``, and
``Job`` workflows, :func:`~imednet_workflows.extraction_engine.extract_canonical_records`
groups ``Record`` items per ``Subject`` chronologically and evaluates each
:class:`~imednet.models.study_config.MappingRule`:

* **Sandboxed derivation context:** When ``MappingRule.business_logic`` is present, it
  compiles via :func:`~imednet.utils.parse_expression` and runs inside
  :class:`~imednet.utils.RestrictedAstEvaluator` (:func:`~imednet.utils.evaluate_expression`)
  with ``record``, shallow-copied ``payload`` and ``state`` dictionaries, ``context``
  (``SubjectContext``), ``baseline``, ``subject_key``, and the extracted ``value``.
* **Allowlisted functions and methods:** Only ``DEFAULT_SAFE_FUNCTIONS`` (``float``,
  ``int``, ``str``, ``bool``, ``len``) and read-only ``DEFAULT_SAFE_METHODS`` are
  callable. Format methods (``.format``, ``.format_map``), f-strings, private ``_``
  attributes, and mutating methods (such as ``payload.clear()`` or ``state.update(...)``)
  raise :class:`~imednet.errors.RestrictedEvaluationError` without mutating ``payload``
  or ``context.state``.
* **Graceful fallback on evaluation errors:** ``MappingRule`` keeps ``business_logic``
  as a string at model load time (pre-validate expressions with
  :func:`~imednet.utils.parse_expression` and
  :func:`~imednet.utils.evaluator.validate_ast` if fail-fast authoring checks are
  desired). If a rule raises :class:`~imednet.errors.RestrictedEvaluationError` or
  runtime :class:`~imednet.errors.EvaluationError` during extraction,
  ``extract_canonical_records`` logs a warning and retains the extracted source
  ``Variable`` value (or ``MappingRule.fallback_value`` when the source field is
  missing).

.. testcode::

   from imednet.errors import EvaluationError, RestrictedEvaluationError
   from imednet.models import Record
   from imednet.models.study_config import MappingRule, StudyConfiguration
   from imednet.utils import (
       RestrictedAstEvaluator,
       evaluate_expression,
       parse_expression,
   )
   from imednet.utils.evaluator import (
       DEFAULT_SAFE_FUNCTIONS,
       DEFAULT_SAFE_METHODS,
       validate_ast,
   )
   from imednet_workflows.extraction_engine import extract_canonical_records

   assert "len" in DEFAULT_SAFE_FUNCTIONS
   assert "get" in DEFAULT_SAFE_METHODS
   assert "clear" not in DEFAULT_SAFE_METHODS

   prechecked_ast = parse_expression("value.strip().upper()")
   validate_ast(prechecked_ast)
   severity_evaluator = RestrictedAstEvaluator("value.strip().upper()")
   assert severity_evaluator.evaluate({"value": " mild "}) == "MILD"

   fstring_blocked = False
   try:
       parse_expression("f'{value}'")
   except RestrictedEvaluationError:
       fstring_blocked = True
   assert fstring_blocked

   clear_blocked = False
   try:
       evaluate_expression("payload.clear()", {"payload": {"aeTerm": "Headache"}}, raise_on_error=True)
   except RestrictedEvaluationError:
       clear_blocked = True
   assert clear_blocked

   div_failed = False
   try:
       evaluate_expression("1 / 0", {}, raise_on_error=True)
   except EvaluationError:
       div_failed = True
   assert div_failed

   ae_study_cfg = StudyConfiguration(
       study_key="STUDY",
       mappings=[
           MappingRule(
               domain="AE",
               target_field="subjectKey",
               source_form_key="F_AE",
               source_variable_name="subjectKey",
           ),
           MappingRule(
               domain="AE",
               target_field="aeTerm",
               source_form_key="F_AE",
               source_variable_name="recordData.aeTerm",
               fallback_value="UNSPECIFIED_AE",
               business_logic="payload.clear()",
           ),
           MappingRule(
               domain="AE",
               target_field="aeSeverity",
               source_form_key="F_AE",
               source_variable_name="recordData.aeSeverity",
               business_logic="payload.get('aeTerm', '') + ':' + value.strip().upper()",
           ),
       ],
   )
   raw_ae_records = [
       Record(
           record_id=1,
           study_key="STUDY",
           form_key="F_AE",
           subject_key="SUBJ-001",
           date_created="2026-01-15T10:00:00Z",
           record_data={"aeSeverity": " mild "},
       )
   ]
   canonical_result = extract_canonical_records(raw_ae_records, ae_study_cfg)
   assert len(canonical_result.adverse_events) == 1
   # Blocked payload.clear() left payload intact and fell back to fallback_value
   assert canonical_result.adverse_events[0].ae_term == "UNSPECIFIED_AE"
   assert canonical_result.adverse_events[0].ae_severity == "UNSPECIFIED_AE:MILD"
