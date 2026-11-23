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

Default PHI Masking & ``SensitivityRegistry``
---------------------------------------------

Export mappers, SDTM transformers, and ``ExportSink`` implementations enforce
``mask_phi=True`` by default via :class:`~imednet.utils.security.SensitivityRegistry`
and :func:`~imednet.utils.security.mask_clinical_phi`. Key matching normalizes
camelCase, PascalCase, hyphenated, and upper/lower snake_case ``Variable`` names
before comparison, replacing matching non-null values with ``***MASKED***``.

* **13 Default Sensitive PHI Keys** (redacted to ``***MASKED***`` across case
  and delimiter variants):
  ``patient_name``, ``patient_initials``, ``dob``, ``date_of_birth``, ``ssn``,
  ``phone``, ``email``, ``address``, ``birth_date``, ``brthdtc``, ``brthdat``,
  ``brthyr``, and ``subjinit``.
* **6 Exempt Structural Keys** (preserved so ``Study``, ``Subject``, and
  ``Record`` joins, partitions, and graph edges remain intact even if passed in
  custom key lists):
  ``subject_key``, ``study_key``, ``record_id``, ``usubjid``, ``subjid``, and
  ``studyid``.
* **Study-Specific PHI Extensions**: Additional ``Study``-level ``Variable``
  keys declared in ``StudyConfiguration(phiFields=[...])`` (and governed via
  ``ConfigVersionStore``; see :doc:`/reference/governance`) are merged into the
  active ``SensitivityRegistry`` automatically.
* **SDTM ``BRTHDTC`` Year Redaction**: During CDISC SDTM transformation with
  ``mask_phi=True``, ``BRTHDTC`` values are redacted to 4-digit ISO 8601 birth
  years (``YYYY``) — or ``None`` when no 4-digit year can be parsed — rather
  than ``***MASKED***`` literal text so downstream SDTM ``DM`` date typing
  remains valid without exposing full birth dates.

CDISC SDTM Domain Transformations
---------------------------------

Use :func:`~imednet.spi.models.transform_records_to_sdtm_rows` (or
:func:`~imednet.models.sdtm.transform_records_to_sdtm`) to project clinical
``Record`` payloads into standardized CDISC SDTM domain rows across all 9
domains defined in ``SDTM_DOMAIN_SPECS``:

.. list-table:: Supported CDISC SDTM Domains
   :header-rows: 1
   :widths: 12 30 58

   * - Domain
     - Label
     - Core Standardized Variables & Notes
   * - ``AE``
     - Adverse Events
     - ``STUDYID``, ``DOMAIN``, ``USUBJID``, ``AESEQ``, ``AETERM``, ``AEDECOD``, ``AEBODSYS``, ``AESEV``, ``AESER``, ``AESTDTC``, ``AEENDTC``
   * - ``DV``
     - Protocol Deviations
     - ``STUDYID``, ``DOMAIN``, ``USUBJID``, ``DVSEQ``, ``DVTERM``, ``DVDECOD``, ``DVCAT``, ``DVSCAT``, ``DVSTDTC``, ``DVENDTC``
   * - ``DD``
     - Device Deficiencies
     - ``STUDYID``, ``DOMAIN``, ``USUBJID``, ``DDSEQ``, ``DDTERM``, ``DDDECOD``, ``DDCAT``, ``DDSCAT``, ``DDSTDTC``, ``DDENDTC``
   * - ``CM``
     - Concomitant Medications
     - ``STUDYID``, ``DOMAIN``, ``USUBJID``, ``CMSEQ``, ``CMTRT``, ``CMDECOD``, ``CMCAT``, ``CMDOSE``, ``CMDOSU``, ``CMROUTE``, ``CMSTDTC``, ``CMENDTC``
   * - ``DM``
     - Demographics
     - ``STUDYID``, ``DOMAIN``, ``USUBJID``, ``SUBJID``, ``SITEID``, ``BRTHDTC`` (4-digit ``YYYY`` or ``None`` under ``mask_phi=True``), ``AGE``, ``AGEU``, ``SEX``, ``RACE``, ``ETHNIC``, ``ARMCD``, ``ARM``, ``COUNTRY`` (omits ``DMSEQ`` per CDISC SDTM)
   * - ``VS``
     - Vital Signs
     - ``STUDYID``, ``DOMAIN``, ``USUBJID``, ``VSSEQ``, ``VSTESTCD``, ``VSTEST``, ``VSORRES``, ``VSORRESU``, ``VSSTRESC``, ``VSSTRESN``, ``VSSTRESU``, ``VSPOS``, ``VSDTC``, ``VISIT``, ``VISITNUM``
   * - ``LB``
     - Laboratory Test Results
     - ``STUDYID``, ``DOMAIN``, ``USUBJID``, ``LBSEQ``, ``LBTESTCD``, ``LBTEST``, ``LBCAT``, ``LBORRES``, ``LBORRESU``, ``LBSTRESC``, ``LBSTRESN``, ``LBSTRESU``, ``LBNRIND``, ``LBDTC``, ``VISIT``, ``VISITNUM``
   * - ``EX``
     - Exposure
     - ``STUDYID``, ``DOMAIN``, ``USUBJID``, ``EXSEQ``, ``EXTRT``, ``EXCAT``, ``EXDOSE``, ``EXDOSU``, ``EXDOSFRM``, ``EXDOSFRQ``, ``EXROUTE``, ``EXSTDTC``, ``EXENDTC``, ``VISIT``, ``VISITNUM``
   * - ``MH``
     - Medical History
     - ``STUDYID``, ``DOMAIN``, ``USUBJID``, ``MHSEQ``, ``MHTERM``, ``MHDECOD``, ``MHCAT``, ``MHSCAT``, ``MHBODSYS``, ``MHSTDTC``, ``MHENDTC``, ``MHENRF``

Standards Readiness Validation & ``SinkConfig`` Controls
--------------------------------------------------------

Before writing to downstream analytical or regulatory destinations
(:doc:`export_destinations`), :class:`~imednet.models.standards.StandardsReadinessValidator`
and :func:`~imednet.spi.sink_base.evaluate_standards_readiness` evaluate
required SDTM identifiers, ISO 8601 ``--DTC`` date formatting, and controlled
terminology conformance across ``"general"``, ``"drug"``, and ``"device"``
:class:`~imednet.models.standards.StandardsProfile` presets.

All export sinks share five :class:`~imednet.spi.sink_base.SinkConfig` controls
for SDTM projection, readiness gating, and field naming:

* ``as_sdtm`` — transform exported ``Record`` items into CDISC SDTM domain rows
  via :func:`~imednet.spi.models.transform_records_to_sdtm_rows` (automatically
  enabled when ``sdtm_domain`` is set).
* ``sdtm_domain`` — target 2-character SDTM domain code (``"AE"``, ``"DV"``,
  ``"DD"``, ``"CM"``, ``"DM"``, ``"VS"``, ``"LB"``, ``"EX"``, or ``"MH"``).
* ``standards_profile`` — ``"general"``, ``"drug"``, ``"device"``, or a custom
  :class:`~imednet.models.standards.StandardsProfile` evaluated before sink
  writes.
* ``min_standards_score`` — minimum readiness score (``0.0``–``100.0``)
  enforced via :func:`~imednet.spi.sink_base.evaluate_standards_readiness`
  (raising ``StandardsValidationError`` when ``report.score < min_standards_score``).
* ``naming_convention`` — raw ``Variable`` and metadata key naming convention
  (``"snake_case"``, ``"camelCase"``, or ``"sdtm"``) applied across tabular,
  document, graph, and warehouse mappers.

.. testcode::

   from imednet.models.records import Record
   from imednet.models.standards import StandardsReadinessValidator
   from imednet.spi.models import transform_records_to_sdtm_rows
   from imednet.spi.sink_base import SinkConfig, evaluate_standards_readiness
   from imednet.utils.security import mask_clinical_phi

   raw_payload = {
       "subject_key": "SUB-101",
       "patient_name": "Jane Doe",
       "brthdtc": "1985-07-19",
   }
   masked_payload = mask_clinical_phi(raw_payload)
   assert masked_payload["subject_key"] == "SUB-101"
   assert masked_payload["patient_name"] == "***MASKED***"

   dm_record = Record.model_validate(
       {
           "studyKey": "STUDY",
           "recordId": 1001,
           "subjectKey": "SUB-101",
           "siteName": "SITE-01",
           "formKey": "DM",
           "deleted": False,
           "recordData": {
               "birthDate": "1985-07-19",
               "sex": "F",
               "age": 41,
               "race": "WHITE",
               "ethnicity": "NOT HISPANIC OR LATINO",
               "ARM": "Treatment A",
               "COUNTRY": "USA",
               "patient_initials": "JD",
           },
       }
   )

   dm_rows = transform_records_to_sdtm_rows([dm_record], domain="DM", study_key="STUDY")
   assert len(dm_rows) == 1
   assert dm_rows[0]["USUBJID"] == "SUB-101"
   assert dm_rows[0]["BRTHDTC"] == "1985"
   assert dm_rows[0]["patient_initials"] == "***MASKED***"
   assert "DMSEQ" not in dm_rows[0]

   validator = StandardsReadinessValidator("drug")
   sex_lookup = {"sex": {"F": "F"}}
   report = validator.score_records(
       records_by_domain={
           "DM": [{"subjectKey": dm_record.subject_key, **dm_record.record_data}]
       },
       terminology_lookups=sex_lookup,
   )
   assert report.score == 100.0 and report.is_ready

   sink_cfg = SinkConfig(
       study_key="STUDY",
       as_sdtm=True,
       sdtm_domain="DM",
       standards_profile="drug",
       min_standards_score=95.0,
       naming_convention="sdtm",
   )
   gate_report = evaluate_standards_readiness(
       [dm_record],
       study_key="STUDY",
       config=sink_cfg,
       terminology_lookups=sex_lookup,
   )
   assert gate_report.score == 100.0 and gate_report.is_ready
