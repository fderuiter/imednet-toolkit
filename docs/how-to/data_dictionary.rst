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

Validating Record Submissions with ``schema=data_dictionary``
-------------------------------------------------------------

Unlike :class:`~imednet.validation.cache.SchemaCache` (which only checks Form existence, unknown Variables, and basic scalar types), passing a :class:`~imednet.datadict.DataDictionary` via ``schema=data_dictionary`` enforces:

1. **Required Variable policies** (``required="Yes"`` raises :class:`~imednet.errors.ValidationError`; ``required="Auto Query"`` logs a warning).
2. **Choice membership** for categorical Variables (``Radio``, ``Single Select``, ``Multi Select``).
3. **Numeric and string length bounds** (``minValue`` / ``maxValue`` and ``minLength`` / ``maxLength``).
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
