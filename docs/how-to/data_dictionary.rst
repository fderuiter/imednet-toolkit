Data Dictionary Workflows and Rule Validation
=============================================

A **Data Dictionary** is the canonical Study metadata specification in ``imednet``, defining Forms, Variables, Choices, numeric and text bounds, required-field policies, and XML ``BusinessLogicRule`` edit checks (see :doc:`/adr/0004-data-dictionary-standardization`).

The :mod:`imednet.datadict` package and ``imednet datadict`` CLI subcommands let Clinical Data Managers and Integration Engineers load Study metadata from four sources, emit deterministic JSON or CDISC ODM 1.3 XML, structurally diff Study revisions, evaluate XML business logic offline, and enforce Form rules before submitting Record payloads to the EDC API.

.. seealso::
   * :doc:`workflows/uat_workflow` — Execute pre-live User Acceptance Testing checks against a ``Study`` sandbox.
   * :doc:`register_subjects` — Register ``Subject`` enrollments after ``Site`` and ``Form`` configuration.
   * :doc:`workflows/bulk_submission` — Validate and submit batch ``Record`` payloads with ``Job`` polling.
   * :doc:`form_builder` — Automate visual layout state dumps to the legacy ``formdez_save.php`` web endpoint using browser session cookies.

Canonical Model Overview
------------------------

Every loader compiles Study metadata into a typed Pydantic v2 hierarchy with deterministic camelCase JSON serialization:

* :class:`~imednet.datadict.DataDictionary`: Root container holding ``metadata``, ``forms``, and ``unassignedBusinessLogic``, while implementing :class:`~imednet.validation.cache.ValidationSchemaProtocol`.
* :class:`~imednet.datadict.Form`: Represents a Study Form (``formKey``, ``formName``, ``formId``, ``formType``) with ordered ``variables`` and Form-scoped ``businessLogic`` rules.
* :class:`~imednet.datadict.Variable`: Defines an individual clinical data point (``variableName``, ``label``, ``variableType``, ``sequence``, ``required``, ``format``, ``precision``, ``choices``).
* :class:`~imednet.datadict.Choice`: Defines a coded categorical option (``choiceValue``, ``choiceText``, ``position``).
* :class:`~imednet.datadict.BusinessLogicRule`: Captures an edit check or workflow rule with both a parsed AST dictionary (``logic``) and optional verbatim XML (``logicRawXml``). ``Form``-scoped rules attach to ``form.businessLogic``, while ``Workflow``, ``Query``, ``Approval``, and ``Required`` rules route to top-level ``unassignedBusinessLogic``.
* :class:`~imednet.datadict.Metadata`: Tracks ``schemaVersion``, ``studyKey``, ``generatedAt``, ``source``, and ``validationWarnings``.

Source CSV or XML attributes without explicit typed properties are preserved losslessly under ``extraAttributes`` using deterministic lexical camelCase keys (via ``to_camel_case``).

Loading a Data Dictionary
-------------------------

From 4-File CSV Exports
~~~~~~~~~~~~~~~~~~~~~~~

iMednet Data Dictionary exports contain four CSV files: ``FORMS.csv``, ``QUESTIONS.csv``, ``CHOICES.csv``, and ``BUSINESS_LOGIC.csv``. Use :func:`~imednet.datadict.load_from_csv` for a directory or ``.zip`` archive, or :func:`~imednet.datadict.load_from_csv_files` for individual paths or text streams. Pass ``strict=True`` to fail fast on unknown columns or broken references, and ``no_raw_xml=True`` to omit verbatim XML strings after parsing the AST:

.. testcode::

   import io
   import tempfile
   from pathlib import Path
   from imednet.datadict import load_from_csv, load_from_csv_files

   forms_csv = "Form Key,Form Name,Form ID,Form Type\nF_VS,Vital Signs,302,Subject\n"
   questions_csv = (
       "Form Key,Variable Name,Label,Variable Type,Sequence,Required Field,Format\n"
       "F_VS,V_WEIGHT,Weight (kg),Float,1,Yes,6\n"
       "F_VS,V_POSITION,Body Position,Radio,2,Auto Query,\n"
   )
   choices_csv = (
       "Form Key,Variable Name,Choice Value,Choice Text,Position\n"
       "F_VS,V_POSITION,SIT,Sitting,1\n"
       "F_VS,V_POSITION,SUP,Supine,2\n"
   )
   logic_csv = (
       "ID,Name,Type,Status,Form,Sequence,Logic\n"
       "101,Weight Positive,Form,Active,F_VS,1,"
       '"<rule><conditions><condition variable=""V_WEIGHT"" operator=""&lt;="" value=""0""/></conditions>'
       '<actions><action type=""error"" message=""Weight must exceed 0 kg.""/></actions></rule>"\n'
   )

   csv_dd = load_from_csv_files(
       forms_file=io.StringIO(forms_csv),
       questions_file=io.StringIO(questions_csv),
       choices_file=io.StringIO(choices_csv),
       business_logic_file=io.StringIO(logic_csv),
       study_key="STUDY",
       strict=True,
   )
   assert csv_dd.form_for_key("F_VS") is not None
   assert len(csv_dd.variables_for_form("F_VS")["V_POSITION"].choices) == 2

   with tempfile.TemporaryDirectory() as tmp_dir:
       export_dir = Path(tmp_dir)
       (export_dir / "FORMS.csv").write_text(forms_csv, encoding="utf-8")
       (export_dir / "QUESTIONS.csv").write_text(questions_csv, encoding="utf-8")
       (export_dir / "CHOICES.csv").write_text(choices_csv, encoding="utf-8")
       (export_dir / "BUSINESS_LOGIC.csv").write_text(logic_csv, encoding="utf-8")

       dir_dd = load_from_csv(export_dir, study_key="STUDY", strict=True, no_raw_xml=True)
       assert dir_dd.forms[0].businessLogic[0].logicRawXml is None

From CDISC ODM 1.3 XML
~~~~~~~~~~~~~~~~~~~~~~

Use :func:`~imednet.datadict.to_odm_xml` (or :func:`~imednet.datadict.to_odm_file`) to serialize a :class:`~imednet.datadict.DataDictionary` to CDISC ODM 1.3 XML, and :func:`~imednet.datadict.load_from_cdisc_odm` to parse ODM ``FormDef``, ``ItemGroupDef``, ``ItemDef``, ``CodeList``, ``RangeCheck``, ``ConditionDef``, and ``MethodDef`` definitions into the canonical model:

.. testcode::

   import io
   from imednet.datadict import load_from_cdisc_odm, to_odm_xml

   odm_xml = to_odm_xml(csv_dd, file_oid="ODM.STUDY.001")
   odm_dd = load_from_cdisc_odm(
       io.StringIO(odm_xml),
       study_key="STUDY",
       strict=True,
   )
   assert odm_dd.metadata.source == "cdisc_odm"
   assert "V_WEIGHT" in odm_dd.variables_for_form("F_VS")

CDISC ODM ``RangeCheck`` Constraints and UTC Temporal Normalization
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

When :func:`~imednet.datadict.load_from_cdisc_odm` parses an ``ItemDef`` containing CDISC ODM 1.3.2 ``<RangeCheck>`` elements (which declare a required ``SoftHard`` attribute, an optional ``Comparator``, one or more ``<CheckValue>`` children, and an optional ``<ErrorMessage>``), it preserves the raw definitions under ``Variable.extraAttributes["rangeChecks"]``. Calling :func:`~imednet.datadict.extract_variable_constraints` normalizes them into frozen :class:`~imednet.datadict.RangeCheckConstraint` objects on ``VariableConstraints.range_checks``:

* **Supported ODM comparators**: ``LT``, ``LE``, ``GT``, ``GE``, ``EQ``, ``NE``, ``IN``, and ``NOTIN``. Single-value numeric ``GE`` / ``GT`` and ``LE`` / ``LT`` checks also populate implied ``min_value`` and ``max_value`` bounds on ``VariableConstraints`` when explicit Variable bounds are absent, while ``IN`` and ``NOTIN`` test membership against multi-value ``check_values`` tuples.
* ``Soft`` **vs.** ``Hard`` **severity**: During :func:`~imednet.validation.cache.validate_record_data`, a failed ``Soft`` check (``soft_hard="Soft"``) logs a warning and records a ``RecordValidationIssue`` with ``rule_id="range_check_soft"`` and ``severity="warning"`` in ``report.warnings`` without raising :class:`~imednet.errors.ValidationError`. A failed ``Hard`` check (``soft_hard="Hard"``, the default when ``SoftHard`` is ``"Hard"`` or any non-``"Soft"`` value) raises :class:`~imednet.errors.ValidationError` (or appends an error with ``rule_id="range_check"`` to ``report.errors`` when ``collect_errors=True``).
* **UTC temporal normalization**: When both the Record Variable value and ``check_values`` target parse as temporal values via ``_parse_comparable_temporal``—supporting both iMednet ``MM-DD-YYYY`` and ISO ``YYYY-MM-DD`` dates and timestamps with optional ISO-8601 timezone offsets (``Z``, ``+HH:MM``, ``-HHMM``)—offset-aware timestamps are converted to UTC-normalized chronological instants before evaluating ``RangeCheckConstraint`` or XML condition comparisons.

.. testcode::

   from imednet.datadict import (
       RangeCheckConstraint,
       extract_variable_constraints,
       load_from_cdisc_odm,
   )
   from imednet.validation.cache import validate_record_data

   range_odm_xml = """<?xml version="1.0" encoding="UTF-8"?>
   <ODM xmlns="http://www.cdisc.org/ns/odm/v1.3" FileOID="ODM.RANGE.001"
        FileType="Snapshot" CreationDateTime="2026-03-15T12:00:00Z" ODMVersion="1.3.2">
     <Study OID="STUDY">
       <GlobalVariables>
         <StudyName>Range Study</StudyName>
         <StudyDescription>ODM RangeCheck Demo</StudyDescription>
         <ProtocolName>PROTO-01</ProtocolName>
       </GlobalVariables>
       <MetaDataVersion OID="MDV.001" Name="Version 1">
         <FormDef OID="F_LAB" Name="Laboratory" Repeating="No">
           <ItemGroupRef ItemGroupOID="IG_LAB" Mandatory="Yes"/>
         </FormDef>
         <ItemGroupDef OID="IG_LAB" Name="Lab Results" Repeating="No">
           <ItemRef ItemOID="V_TEMP" OrderNumber="1" Mandatory="Yes"/>
           <ItemRef ItemOID="V_STATUS" OrderNumber="2" Mandatory="No"/>
           <ItemRef ItemOID="V_COLLECT_DT" OrderNumber="3" Mandatory="No"/>
         </ItemGroupDef>
         <ItemDef OID="V_TEMP" Name="Body Temperature" DataType="float">
           <RangeCheck Comparator="GE" SoftHard="Hard">
             <CheckValue>30.0</CheckValue>
             <ErrorMessage><TranslatedText>Temperature below physiological minimum.</TranslatedText></ErrorMessage>
           </RangeCheck>
           <RangeCheck Comparator="LE" SoftHard="Soft">
             <CheckValue>38.0</CheckValue>
             <ErrorMessage><TranslatedText>Confirm febrile temperature above 38.0 C.</TranslatedText></ErrorMessage>
           </RangeCheck>
         </ItemDef>
         <ItemDef OID="V_STATUS" Name="Result Status" DataType="text">
           <RangeCheck Comparator="IN" SoftHard="Hard">
             <CheckValue>NORMAL</CheckValue>
             <CheckValue>ABNORMAL</CheckValue>
           </RangeCheck>
         </ItemDef>
         <ItemDef OID="V_COLLECT_DT" Name="Collection Timestamp" DataType="datetime">
           <RangeCheck Comparator="LE" SoftHard="Hard">
             <CheckValue>2026-03-15T13:00:00Z</CheckValue>
           </RangeCheck>
         </ItemDef>
       </MetaDataVersion>
     </Study>
   </ODM>
   """

   range_dd = load_from_cdisc_odm(io.StringIO(range_odm_xml), study_key="STUDY", strict=True)
   temp_constraints = extract_variable_constraints(range_dd.variables_for_form("F_LAB")["V_TEMP"])
   assert temp_constraints.range_checks == (
       RangeCheckConstraint(
           comparator="GE",
           check_values=("30.0",),
           soft_hard="Hard",
           error_message="Temperature below physiological minimum.",
       ),
       RangeCheckConstraint(
           comparator="LE",
           check_values=("38.0",),
           soft_hard="Soft",
           error_message="Confirm febrile temperature above 38.0 C.",
       ),
   )

   # Soft violation records 'range_check_soft' warning; MM-DD-YYYY -05:00 normalizes to 13:00:00Z UTC
   soft_report = validate_record_data(
       range_dd,
       "F_LAB",
       {
           "V_TEMP": 38.6,
           "V_STATUS": "NORMAL",
           "V_COLLECT_DT": "03-15-2026 08:00:00-05:00",
       },
       collect_errors=True,
   )
   assert soft_report.is_valid is True
   assert [w.rule_id for w in soft_report.warnings] == ["range_check_soft"]

   # Hard violations (numeric GE, IN membership, and UTC timestamp > 13:00:00Z) record 'range_check' errors
   hard_report = validate_record_data(
       range_dd,
       "F_LAB",
       {
           "V_TEMP": 28.5,
           "V_STATUS": "PENDING",
           "V_COLLECT_DT": "2026-03-15T09:30:00-05:00",
       },
       collect_errors=True,
   )
   assert [e.rule_id for e in hard_report.errors] == [
       "range_check",
       "range_check",
       "range_check",
   ]

From REDCap CSV Data Dictionaries
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Use :func:`~imednet.datadict.load_from_redcap` to convert REDCap instrument CSV files—mapping REDCap field types, ``Choices, Calculations, or Slider Labels``, ``Required Field?``, and ``Text Validation Min`` / ``Max`` bounds into canonical Forms, Variables, and Choices:

.. testcode::

   import io
   from imednet.datadict import extract_variable_constraints, load_from_redcap

   redcap_csv = (
       "Variable / Field Name,Form Name,Field Type,Field Label,"
       '"Choices, Calculations, or Slider Labels",Text Validation Type OR Show Slider Number,'
       "Text Validation Min,Text Validation Max,Required Field?\n"
       "v_age,demographics,text,Subject Age,,integer,18,99,y\n"
       'v_arm,demographics,radio,Treatment Arm,"A, Arm A | B, Arm B",,,,y\n'
   )
   redcap_dd = load_from_redcap(io.StringIO(redcap_csv), study_key="STUDY", strict=True)
   age_var = redcap_dd.variables_for_form("demographics")["v_age"]
   age_constraints = extract_variable_constraints(age_var)
   assert age_constraints.min_value == 18.0
   assert age_constraints.max_value == 99.0
   assert age_constraints.required_policy == "yes"

From the EDC REST API
~~~~~~~~~~~~~~~~~~~~~

Use :func:`~imednet.datadict.load_from_api` with a synchronous :class:`~imednet.ImednetSDK` (:class:`~imednet.spi.facade.ImednetFacade`) or :func:`~imednet.datadict.async_load_from_api` with an asynchronous :class:`~imednet.AsyncImednetSDK` (:class:`~imednet.spi.facade.AsyncImednetFacade`) to extract Study Forms and Variables concurrently from the ``/forms`` and ``/variables`` REST endpoints. Both functions execute the same canonical Data Dictionary builder, and ``load_from_api`` raises :exc:`TypeError` if passed an ``AsyncImednetSDK`` instance:

.. note::
   The standard iMednet REST API exposes Forms and Variables, but does not expose Choice code lists or ``BusinessLogicRule`` XML definitions. ``load_from_api`` and ``async_load_from_api`` record this limitation in ``metadata.validationWarnings``; use a 4-file CSV or CDISC ODM export when enforcing Choice membership or edit check rules.

.. testcode::

   import asyncio
   from imednet import AsyncImednetSDK, ImednetSDK, SimulationState, SimulationTransport
   from imednet.datadict import DataDictionary, async_load_from_api, load_from_api

   state = SimulationState()
   with ImednetSDK(
       simulation_mode=True,
       transport=SimulationTransport(state),
   ) as sdk:
       api_dd = load_from_api(sdk, "STUDY", no_raw_xml=True)

   async def load_async_dd() -> DataDictionary:
       async with AsyncImednetSDK(
           simulation_mode=True,
           transport=SimulationTransport(state),
       ) as async_sdk:
           return await async_load_from_api(async_sdk, "STUDY", no_raw_xml=True)

   async_dd = asyncio.run(load_async_dd())
   assert api_dd.form_for_key("F_VS") is not None
   assert async_dd.forms == api_dd.forms
   assert len(api_dd.metadata.validationWarnings) == 1

JSON Emission, Schema Export, and Validation
--------------------------------------------

:func:`~imednet.datadict.to_json` (and :func:`~imednet.datadict.to_file`) deterministically sorts Forms by ``formKey``, Variables by ``sequence`` then ``variableName``, Choices by ``position``, and rules by ``sequence`` then ``id``. Use :func:`~imednet.datadict.get_schema` to export the Draft 2020-12 JSON Schema, and :func:`~imednet.datadict.validate` to verify a dictionary, JSON string, file path, or :class:`~imednet.datadict.DataDictionary` instance:

.. testcode::

   from imednet.datadict import get_schema, to_json, validate

   canonical_json = to_json(csv_dd, exclude_generated_at=True)
   dd_schema = get_schema()
   assert dd_schema["version"] == "1.0.0"

   validation_errors = validate(canonical_json, schema=dd_schema)
   assert validation_errors == []

Comparing Study Revisions with ``diff``
---------------------------------------

Use :func:`~imednet.datadict.diff` to compare baseline and updated :class:`~imednet.datadict.DataDictionary` revisions while ignoring ``metadata.generatedAt`` timestamps. The returned :class:`~imednet.datadict.DiffReport` formats structural additions, removals, and modifications as plain text, Markdown tables, or JSON:

.. testcode::

   from imednet.datadict import DataDictionary, Variable, diff

   updated_dd = csv_dd.model_copy(deep=True)
   vs_form = updated_dd.form_for_key("F_VS")
   assert vs_form is not None
   vs_form.variables.append(
       Variable(variableName="V_PULSE", label="Pulse (bpm)", variableType="Integer", sequence=3)
   )

   diff_report = diff(csv_dd, updated_dd)
   assert diff_report.has_changes is True
   assert "V_PULSE" in diff_report.to_text()
   assert "V_PULSE" in diff_report.to_markdown()
   assert '"has_changes": true' in diff_report.to_json()

Evaluating XML Business Logic Rules
-----------------------------------

:func:`~imednet.datadict.parse_logic_xml` parses iMednet XML rule expressions into a safe, deterministic AST dictionary, and :class:`~imednet.datadict.LogicTreeEvaluator` evaluates single rules or Form rule sets against Record Variable mappings without ``eval()``:

* ``Form`` and ``Required`` rules classify as ``severity="error"`` (populating ``report.errors`` when triggered).
* ``Query`` and ``Auto Query`` rules classify as ``severity="warning"`` (populating ``report.warnings`` when triggered).
* Inactive rules, UI-only actions (such as ``hide`` or ``disable``), and cross-Form / cross-Visit rules are safely marked ``skipped=True`` with a descriptive ``skip_reason``.

.. testcode::

   from imednet.datadict import BusinessLogicRule, LogicTreeEvaluator, parse_logic_xml

   query_xml = (
       "<rule>"
       '<conditions><condition variable="V_WEIGHT" operator="&gt;" value="200"/></conditions>'
       '<actions><action type="query" message="Confirm weight above 200 kg."/></actions>'
       "</rule>"
   )
   query_rule = BusinessLogicRule(
       id="102",
       name="High Weight Query",
       type="Query",
       status="Active",
       form="F_VS",
       logic=parse_logic_xml(query_xml),
   )

   evaluator = LogicTreeEvaluator()
   rules_to_check = [*csv_dd.rules_for_form("F_VS"), query_rule]

   bad_eval = evaluator.evaluate_rules(rules_to_check, {"V_WEIGHT": -5.0}, form_key="F_VS")
   assert len(bad_eval.errors) == 1
   assert bad_eval.errors[0].rule_name == "Weight Positive"

   warn_eval = evaluator.evaluate_rules(rules_to_check, {"V_WEIGHT": 225.0}, form_key="F_VS")
   assert len(warn_eval.errors) == 0
   assert len(warn_eval.warnings) == 1

Regex Edit Check Safety and Length Caps
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

When authoring ``operator="regex"`` conditions inside ``BusinessLogicRule`` definitions, call :func:`~imednet.datadict.validate_regex_pattern` to verify syntax and ReDoS safety ahead of deployment:

* **Pattern and repeat bounds**: Patterns exceeding ``512`` characters (``_MAX_REGEX_PATTERN_LENGTH``) or quantifiers with minimum or maximum repeat counts above ``256`` (``_MAX_REGEX_REPEAT_COUNT``) raise :exc:`ValueError`.
* **AST-level ReDoS guards**: ``validate_regex_pattern`` parses the regular expression AST and rejects backreferences (such as ``\1``), quantified lookaround assertions, nested repeating quantifiers (such as ``(a+)+``), zero-width quantified subpatterns, and overlapping quantified alternations (such as ``(a|ab)+``).
* **Schema validation vs. runtime evaluation vs. input length cap**:

  1. :func:`~imednet.datadict.validate` inspects static ``operator="regex"`` rules across ``form.businessLogic`` and ``unassignedBusinessLogic`` and returns schema validation error strings (``variable '...' has unsafe or invalid regex: ...``) when ``validate_regex_pattern`` fails.
  2. During :class:`~imednet.datadict.LogicTreeEvaluator` execution, any rule containing an invalid, over-length (``> 512`` characters), or backtracking-prone static or ``compare_variable`` regex pattern is safely marked ``skipped=True`` with ``skip_reason="unsafe_regex"`` and ``triggered=False``.
  3. When a safe ``operator="regex"`` condition is evaluated against a Record Variable value exceeding ``1024`` characters (``_MAX_REGEX_INPUT_LENGTH``), the regex condition leaf returns ``False`` (``triggered=False``, ``skipped=False``) without scanning oversized text.

.. testcode::

   from imednet.datadict import Form, Metadata, validate_regex_pattern

   validate_regex_pattern(r"^SUBJ-[0-9]{4}$")

   for bad_pattern in ("a" * 513, "a{257}", r"(a+)+$"):
       regex_rejected = False
       try:
           validate_regex_pattern(bad_pattern)
       except ValueError:
           regex_rejected = True
       assert regex_rejected is True

   unsafe_rule = BusinessLogicRule(
       id="201",
       name="Unsafe Subject Pattern",
       type="Form",
       status="Active",
       form="F_VS",
       logic=parse_logic_xml(
           "<rule>"
           '<conditions><condition variable="V_CODE" operator="regex" value="(a+)+$"/></conditions>'
           '<actions><action type="error" message="Invalid code format."/></actions>'
           "</rule>"
       ),
   )
   unsafe_dd = DataDictionary(
       metadata=Metadata.create(study_key="STUDY"),
       forms=[
           Form(
               formKey="F_VS",
               variables=[Variable(variableName="V_CODE", variableType="Text")],
               businessLogic=[unsafe_rule],
           )
       ],
   )

   # 1. datadict.validate flags static unsafe regex patterns in BusinessLogicRule definitions
   schema_regex_errors = validate(unsafe_dd)
   assert any("unsafe or invalid regex" in err for err in schema_regex_errors)

   # 2. LogicTreeEvaluator skips rules with unsafe regex conditions via skip_reason="unsafe_regex"
   unsafe_res = evaluator.evaluate_rule(unsafe_rule, {"V_CODE": "aaaa"}, form_key="F_VS")
   assert unsafe_res.skipped is True
   assert unsafe_res.skip_reason == "unsafe_regex"

   # 3. Safe regex conditions return False (not skipped) when Record Variable input exceeds 1024 chars
   safe_regex_rule = BusinessLogicRule(
       id="202",
       name="Code Prefix Check",
       type="Form",
       status="Active",
       form="F_VS",
       logic=parse_logic_xml(
           "<rule>"
           '<conditions><condition variable="V_CODE" operator="regex" value="^ERR-"/></conditions>'
           '<actions><action type="error" message="Error code detected."/></actions>'
           "</rule>"
       ),
   )
   short_res = evaluator.evaluate_rule(safe_regex_rule, {"V_CODE": "ERR-01"}, form_key="F_VS")
   assert short_res.triggered is True and short_res.skipped is False

   oversized_res = evaluator.evaluate_rule(
       safe_regex_rule,
       {"V_CODE": "ERR-" + ("X" * 1021)},
       form_key="F_VS",
   )
   assert oversized_res.triggered is False and oversized_res.skipped is False

Validating Record Submissions with ``schema=data_dictionary``
-------------------------------------------------------------

:class:`~imednet.validation.cache.BaseSchemaCache` (:class:`~imednet.validation.cache.SchemaCache` and :class:`~imednet.validation.cache.AsyncSchemaCache`) enforces Form existence, unknown Variable detection, scalar and nested ``***MASKED***`` PHI sentinel rejection (``rule_id="masked_phi"``), scalar Variable types (including historical type checks on ``deleted`` and ``disabled`` Variables in ``_retired_form_variables``), and active Required Variable policies (``Yes`` raises :class:`~imednet.errors.ValidationError`; ``Auto Query`` logs a warning). Passing a :class:`~imednet.datadict.DataDictionary` via ``schema=data_dictionary`` enforces those checks plus full Study metadata constraints:

1. **Required Variable policies** (``required="Yes"`` raises :class:`~imednet.errors.ValidationError`; ``required="Auto Query"`` logs a warning; skipped on ``deleted`` / ``disabled`` Variables).
2. **Choice membership** for categorical Variables (``Radio``, ``Single Select``, ``Multi Select``).
3. **Numeric, string length, and CDISC ODM** ``RangeCheck`` **bounds** (``minValue`` / ``maxValue``, ``minLength`` / ``maxLength``, and ``Soft`` / ``Hard`` :class:`~imednet.datadict.RangeCheckConstraint` checks).
4. **Active XML edit checks** via :class:`~imednet.datadict.LogicTreeEvaluator` before any HTTP POST is sent.

Pass ``schema=data_dictionary`` to :meth:`~imednet.endpoints.records.RecordsEndpoint.create`, :meth:`~imednet.endpoints.records.RecordsEndpoint.create_batch`, :class:`~imednet_workflows.record_update.RecordUpdateWorkflow`, or :class:`~imednet_workflows.register_subjects.RegisterSubjectsWorkflow` (see :doc:`/explanation/schema_validation`, :doc:`/how-to/register_subjects`, and :doc:`/how-to/workflows/bulk_submission`):

.. testcode::

   from imednet.errors import ValidationError
   from imednet_workflows.record_update import RecordUpdateWorkflow

   with ImednetSDK(
       simulation_mode=True,
       transport=SimulationTransport(SimulationState()),
   ) as wf_sdk:
       # Direct RecordsEndpoint validation
       batch_summary = wf_sdk.records.create_batch(
           "STUDY",
           [{"formKey": "F_VS", "subjectKey": "SUB1", "data": {"V_WEIGHT": 72.5, "V_POSITION": "SIT"}}],
           schema=csv_dd,
       )
       assert batch_summary.total_records == 1

       # RecordUpdateWorkflow configured with an offline DataDictionary
       update_wf = RecordUpdateWorkflow(wf_sdk, schema=csv_dd)
       rejected = False
       try:
           update_wf.create_or_update_records(
               "STUDY",
               [{"formKey": "F_VS", "subjectKey": "SUB1", "data": {"V_WEIGHT": -1.0, "V_POSITION": "SIT"}}],
           )
       except ValidationError:
           rejected = True
       assert rejected is True

For automated UAT synthetic Record generation and negative edit check verification against an EDC environment, pass ``data_dictionary`` to :class:`~imednet_workflows.uat.UATWorkflow` and :class:`~imednet_workflows.uat.UATExecutionEngine` as shown in :doc:`/how-to/workflows/uat_workflow`.

CLI Reference and Recipes
-------------------------

The ``imednet datadict`` command group exposes eight subcommands for Study metadata conversion, schema validation, and revision comparison (see :doc:`/how-to/cli`):

.. code-block:: bash

   # 1. Compile canonical JSON from a 4-file CSV directory or ZIP archive
   imednet datadict from-csv ./datadict_export.zip -o study_dd.json --strict --no-raw-xml

   # 2. Import CDISC ODM 1.3 XML into canonical Data Dictionary JSON
   imednet datadict from-odm ./study_odm.xml --study-key STUDY -o study_dd.json --strict

   # 3. Export canonical Data Dictionary JSON to CDISC ODM 1.3 XML
   imednet datadict to-odm study_dd.json -o study_odm.xml --file-oid ODM.STUDY.001

   # 4. Convert a REDCap CSV Data Dictionary into canonical JSON
   imednet datadict from-redcap ./redcap_dictionary.csv --study-key STUDY -o study_dd.json --strict

   # 5. Extract Study Forms and Variables from the live iMednet REST API
   imednet datadict from-api --study-key STUDY -o api_dd.json --no-raw-xml

   # 6. Export the canonical Draft 2020-12 JSON Schema
   imednet datadict schema -o data-dictionary.schema.json

   # 7. Validate a Data Dictionary JSON file against model and JSON Schema rules
   imednet datadict validate study_dd.json --schema data-dictionary.schema.json
   imednet datadict validate study_dd.json --format json

   # 8. Compare two Study Data Dictionary revisions in text, Markdown, or JSON format
   imednet datadict diff baseline_dd.json study_dd.json --format md
