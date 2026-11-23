Record Mapping Process
======================

:class:`~imednet_workflows.record_mapper.RecordMapper` (registered under the
``record_mapper`` entry point on ``sdk.workflows.record_mapper``) turns raw
Record payloads into a :class:`pandas.DataFrame`. It loads Variable metadata
from the Data Dictionary, builds a dynamic model, parses the Records, and
assembles the final table.

.. mermaid:: /diagrams/record_mapping_13.mmd

Explicit normalization
----------------------

General RecordMapper exports retain source field strings and analytical types.
Use ``CategoricalNormalizer(date_fields={"assessmentDate"}, date_order="MDY")``
for an explicit normalization step. Only the named date fields are converted;
ambiguous slash dates require MDY or DMY, and invalid/partial/unknown dates remain
unchanged. Timezone offsets and string fractional precision are retained.

Pass a study-approved vocabulary to ``CategoricalNormalizer(cdisc_ct=...)`` to
opt into case-insensitive term matching. The normalizer snapshots the vocabulary
and rejects ambiguous case collisions. Explicit terminology lookups take
precedence, including in readiness scoring. Out-of-set values are preserved and
reported without echoing their contents in new terminology diagnostics.

``NORMALIZATION_TERMINOLOGY`` is a project reporting vocabulary, not a dated
CDISC Controlled Terminology release. The compatibility name
``CDISC_CONTROLLED_TERMINOLOGY`` refers to that same project vocabulary. Neither
is enabled by default. Callers own the selected terminology version and its
extensibility policy; full versioned conformance remains tracked in issue #1592.

Existing USUBJID values are preserved. An explicit ``study_key`` argument may
fill an existing empty USUBJID placeholder from study/subject keys, preserving
case. This convenience does not allocate a globally unique subject identity;
use the study's identity mapping for pooled data and repeat participants.

MappingRule/StudyConfiguration ``strictCdash`` (also local ``strict`` or
``strictMode``) and ``IMEDNET_STRICT_CDASH`` enable a local naming/prefix
heuristic. SDK ``IMEDNET_STRICT_MODE`` retains its separate response-drift
meaning. The heuristic accepts reporting aliases and does not certify membership
in any official CDASH/SDTM version.

Evidence and limits: CDISC CDASHIG v2.1 permits partial-date collection;
CDISC SDTM v1.7 defines USUBJID across studies; CDISC terminology publications
are versioned and some codelists are extensible. Source references:

* ``https://www.cdisc.org/standards/foundational/cdashig-v2-1``
* ``https://www.cdisc.org/standards/foundational/sdtm/sdtm-v1-7/html``
* ``https://www.cdisc.org/kb/articles/controlled-terminology-faqs``

Sandboxed ``MappingRule.business_logic`` expressions
----------------------------------------------------

When enriching ``Study``, ``Subject``, ``Site``, ``Form``, ``Record``, ``Visit``,
``Interval``, ``Variable``, ``Query``, ``Data Dictionary``, or ``Job`` outputs via
:class:`~imednet.models.study_config.StudyConfiguration`, each
:class:`~imednet.models.study_config.MappingRule` can supply an optional
``business_logic`` expression. Rather than invoking Python ``eval``, expressions are
parsed with :func:`~imednet.utils.parse_expression` and executed inside
:class:`~imednet.utils.RestrictedAstEvaluator` (via
:func:`~imednet.utils.evaluate_expression`):

* **Safe function and method allowlists:** Calls are restricted to
  ``DEFAULT_SAFE_FUNCTIONS`` (``float``, ``int``, ``str``, ``bool``, ``len``) and
  ``DEFAULT_SAFE_METHODS`` (pure inspection methods on ``str``, ``dict``,
  ``list``/``tuple``, and ``set``).
* **Blocked escape and mutation vectors:** Disallowed syntax and calls raise
  :class:`~imednet.errors.RestrictedEvaluationError` (while runtime arithmetic or type
  failures raise ``EvaluationError``). In particular, ``.format``, ``.format_map``, and
  f-strings (``JoinedStr`` / ``FormattedValue``) are blocked so format-string attribute
  traversal (such as ``"{0.__class__}".format(value)``) cannot inspect internal Python
  objects, private ``_`` attributes are rejected, and mutating container methods (such
  as ``clear``, ``pop``, ``update``, and ``append``) are excluded.
* **Authoring-time validation via ``parse_expression`` and ``validate_ast``:**
  ``MappingRule`` deserializes ``business_logic`` as a plain ``str | None`` without
  compiling ASTs at model load time. Call :func:`~imednet.utils.parse_expression` (or
  :func:`~imednet.utils.evaluator.validate_ast` on a parsed ``ast.AST``) when authoring
  or reviewing rules to catch syntax or sandbox violations early.
* **Raw-value fallback in enrichment and SDTM pipelines:** ``EnrichmentPipeline`` binds
  ``{"value": value}`` when evaluating ``MappingRule.business_logic``; if
  :class:`~imednet.errors.RestrictedEvaluationError` or
  :class:`~imednet.errors.EvaluationError` occurs, it logs a warning and preserves the
  original ``Variable`` value. Because
  :func:`~imednet.spi.models.transform_records_to_sdtm_rows` applies ``EnrichmentPipeline``
  prior to CDISC SDTM domain projection, blocked or failing expressions likewise retain
  the raw ``Record`` ``Variable`` value instead of aborting the batch.

.. testcode::

   from imednet.errors import EvaluationError, RestrictedEvaluationError
   from imednet.integrations.enrichment import EnrichmentPipeline
   from imednet.models.sdtm import transform_records_to_sdtm_rows
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

   assert set(DEFAULT_SAFE_FUNCTIONS) == {"float", "int", "str", "bool", "len"}
   assert {"strip", "upper", "lower", "get"}.issubset(DEFAULT_SAFE_METHODS)
   assert {"format", "format_map", "clear", "update"}.isdisjoint(DEFAULT_SAFE_METHODS)

   # Pre-check candidate AST syntax before saving StudyConfiguration rules
   compiled_rule = parse_expression("value.strip().upper()")
   validate_ast(compiled_rule)
   rule_evaluator = RestrictedAstEvaluator("value.strip().upper()")
   assert rule_evaluator.evaluate({"value": " mild "}) == "MILD"

   fstring_rejected = False
   try:
       parse_expression("f'{value}'")
   except RestrictedEvaluationError:
       fstring_rejected = True
   assert fstring_rejected

   for unsafe_expr in ("'{0.__class__}'.format(value)", "'{}'.format_map({'a': 1})"):
       rejected = False
       try:
           evaluate_expression(unsafe_expr, {"value": "Headache"}, raise_on_error=True)
       except RestrictedEvaluationError:
           rejected = True
       assert rejected

   coercion_failed = False
   try:
       evaluate_expression("int(value)", {"value": "not-an-int"}, raise_on_error=True)
   except EvaluationError:
       coercion_failed = True
   assert coercion_failed

   mapping_cfg = StudyConfiguration(
       study_key="STUDY",
       mappings=[
           MappingRule(
               domain="AE",
               target_field="aeSeverity",
               source_form_key="F_AE",
               source_variable_name="aeSeverity",
               business_logic="value.strip().upper()",
           ),
           MappingRule(
               domain="AE",
               target_field="aeTerm",
               source_form_key="F_AE",
               source_variable_name="aeTerm",
               business_logic="'{0.__class__}'.format(value)",
           ),
       ],
   )
   enrichment = EnrichmentPipeline(mapping_cfg)
   enriched_payload = enrichment.process(
       {"aeSeverity": " moderate ", "aeTerm": "Headache"},
       mask_phi=False,
   )
   assert enriched_payload["aeSeverity"] == "MODERATE"
   # Blocked .format() expression falls back to the raw Variable value
   assert enriched_payload["aeTerm"] == "Headache"

   sdtm_rows = transform_records_to_sdtm_rows(
       [
           {
               "subjectKey": "SUBJ-001",
               "recordId": 101,
               "record_data": {"aeSeverity": " moderate ", "aeTerm": "Headache"},
           }
       ],
       "AE",
       "STUDY",
       mask_phi=False,
       pipeline=enrichment,
   )
   assert sdtm_rows[0]["AESEV"] == "MODERATE"
   assert sdtm_rows[0]["AETERM"] == "Headache"
