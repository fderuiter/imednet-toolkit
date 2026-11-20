==========================================
Data Dictionary & Form Schema Verification
==========================================

Clinical Data Managers and Integration Engineers use the ``imednet.datadict``
module and the ``imednet datadict`` CLI to compile, inspect, convert, diff, and
validate canonical ``Study`` ``Data Dictionary`` definitions across CSV exports,
CDISC ODM 1.3 XML, REDCap dictionaries, and the live iMednet REST API.

.. seealso::
   * :doc:`workflows/uat_workflow` — Execute pre-live User Acceptance Testing checks against a ``Study`` sandbox.
   * :doc:`register_subjects` — Register ``Subject`` enrollments after ``Site`` and ``Form`` configuration.
   * :doc:`workflows/bulk_submission` — Validate and submit batch ``Record`` payloads with ``Job`` polling.
   * :doc:`form_builder` — Automate visual layout state dumps to the legacy ``formdez_save.php`` web endpoint using browser session cookies.

CLI Reference (``imednet datadict``)
------------------------------------

The ``imednet datadict`` command group provides 8 subcommands for compiling,
converting, validating, and comparing ``Study`` schemas:

* ``from-csv`` — Compile a canonical JSON ``Data Dictionary`` from a 4-file iMednet CSV directory or ZIP archive.
* ``from-odm`` — Import a CDISC ODM 1.3 XML metadata file into the canonical JSON ``Data Dictionary`` format.
* ``to-odm`` — Export a canonical JSON ``Data Dictionary`` to CDISC ODM 1.3 XML.
* ``from-redcap`` — Convert a REDCap data dictionary CSV file into the canonical JSON ``Data Dictionary`` format.
* ``from-api`` — Extract live ``Study`` ``Form`` and ``Variable`` definitions from the iMednet REST API.
* ``validate`` — Validate a ``Data Dictionary`` JSON file against Pydantic model invariants and JSON Schema rules.
* ``diff`` — Compare two ``Data Dictionary`` JSON revisions structurally in ``text``, ``json``, or ``md`` format.
* ``schema`` — Export the canonical Draft 2020-12 JSON Schema for downstream CI and IDE validation.

Compiling & Converting Sources
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Compile a 4-file iMednet CSV export directory or ZIP archive (pass ``--strict``
to fail fast on unknown columns or unreferenced ``Form`` keys, and
``--no-raw-xml`` to omit verbatim XML strings from compiled rules):

.. code-block:: bash

   imednet datadict from-csv ./study_datadict_csv/ -o datadict.json --strict
   imednet datadict from-csv study_datadict.zip -o datadict.json --no-raw-xml

Round-trip metadata between CDISC ODM 1.3 XML, REDCap CSV exports, and the live
REST API:

.. code-block:: bash

   # Import CDISC ODM 1.3 XML and override Study key
   imednet datadict from-odm study_metadata.xml -o datadict.json --study-key STUDY --strict

   # Export canonical JSON back to CDISC ODM 1.3 XML
   imednet datadict to-odm datadict.json -o study_metadata.xml --file-oid OID.STUDY.V1

   # Convert a REDCap data dictionary CSV
   imednet datadict from-redcap redcap_dictionary.csv -o datadict.json --study-key STUDY

   # Snapshot live Form and Variable metadata from the iMednet REST API
   imednet datadict from-api --study-key STUDY -o datadict_live.json

Validating, Diffing, and Exporting JSON Schema
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Validate a compiled dictionary, generate a Markdown structural diff between two
``Study`` build revisions prior to UAT sign-off, or export the canonical JSON
Schema:

.. code-block:: bash

   # Validate model and JSON Schema compliance
   imednet datadict validate datadict.json

   # Compare baseline and candidate Study builds in Markdown format
   imednet datadict diff baseline_v1.json candidate_v2.json --format md

   # Export canonical JSON Schema for CI pipelines or editor autocompletion
   imednet datadict schema -o datadict.schema.json

Canonical ``Data Dictionary`` Models
------------------------------------

Standardized in :doc:`/adr/0004-data-dictionary-standardization`, the
``imednet.datadict`` package represents every ``Study`` build using deterministic
Pydantic v2 models with camelCase JSON serialization:

* :class:`~imednet.datadict.DataDictionary` — Root container holding ``metadata``, ``forms``, and ``unassignedBusinessLogic``.
* :class:`~imednet.datadict.Metadata` — Envelope recording ``schemaVersion``, ``studyKey``, ``generatedAt``, ``source``, and ``validationWarnings``.
* :class:`~imednet.datadict.Form` — Clinical ``Form`` definition (``formKey``, ``formName``, ``formId``, ``formType``, ``variables``, and form-scoped ``businessLogic``).
* :class:`~imednet.datadict.Variable` — ``Form`` data capture ``Variable`` (``variableName``, ``label``, ``variableType``, ``sequence``, ``required``, ``format``, ``precision``, and categorical ``choices``).
* :class:`~imednet.datadict.Choice` — Coded categorical option (``choiceText``, ``choiceValue``, ``position``).
* :class:`~imednet.datadict.BusinessLogicRule` — Edit check, ``Query`` rule, or workflow trigger (``id``, ``name``, ``type``, ``status``, ``form``, ``sequence``, parsed ``logic`` AST, and optional ``logicRawXml``).

Source CSV or XML attributes without explicit typed properties are preserved
losslessly under ``extraAttributes`` using deterministic lexical camelCase keys
(via ``to_camel_case``). Rules scoped to a single ``Form`` attach to
``form.businessLogic``, while cross-form ``Workflow``, ``Query``, ``Approval``,
or ``Required`` rules route to ``datadict.unassignedBusinessLogic``.

.. testcode::

   from imednet.datadict import (
       BusinessLogicRule,
       Choice,
       DataDictionary,
       Form,
       Metadata,
       Variable,
       diff,
       get_schema,
   )

   baseline = DataDictionary(
       metadata=Metadata(studyKey="STUDY", source="odm"),
       forms=[
           Form(
               formKey="F_VS",
               formName="Vital Signs",
               formId=302,
               formType="Subject",
               variables=[
                   Variable(
                       variableName="V_SYSBP",
                       label="Systolic Blood Pressure (mmHg)",
                       variableType="Integer",
                       sequence=1,
                       required="Yes",
                   ),
                   Variable(
                       variableName="V_POS",
                       label="Measurement Position",
                       variableType="Radio",
                       sequence=2,
                       required="No",
                       choices=[
                           Choice(choiceText="Sitting", choiceValue="SIT", position=1),
                           Choice(choiceText="Standing", choiceValue="STAND", position=2),
                       ],
                   ),
               ],
               businessLogic=[
                   BusinessLogicRule(
                       id="BL-VS-001",
                       name="Systolic Out of Range",
                       type="Query",
                       status="Active",
                       form="F_VS",
                       sequence=1,
                       logic={
                           "operator": "OR",
                           "conditions": [
                               {"variable": "V_SYSBP", "operator": "<", "value": 70},
                               {"variable": "V_SYSBP", "operator": ">", "value": 240},
                           ],
                       },
                   )
               ],
           )
       ],
   )

   candidate = baseline.model_copy(deep=True)
   candidate.forms[0].variables[0].label = "Seated Systolic Blood Pressure (mmHg)"

   report = diff(baseline, candidate)
   schema = get_schema()
   assert schema["title"] == "iMednet Data Dictionary Schema"
   assert len(report.items) == 1 and report.items[0].action == "modified"
   print(f"Schema: {schema['title']}, Changes: {len(report.items)}, Modified: {report.items[0].action}")

Evaluating Edit Checks with ``LogicTreeEvaluator``
--------------------------------------------------

Before running live ``Record`` UAT or bulk imports, use
:class:`~imednet.datadict.LogicTreeEvaluator` and ``parse_logic_xml`` to parse
and evaluate ``BusinessLogicRule`` trees deterministically in memory against
candidate ``Record`` dictionaries:

.. testcode::

   from imednet.datadict import BusinessLogicRule, LogicTreeEvaluator, parse_logic_xml

   xml_rule = """
   <rule>
     <conditions operator="OR">
       <condition><variable>V_SYSBP</variable><operator>&lt;</operator><value>70</value></condition>
       <condition><variable>V_SYSBP</variable><operator>&gt;</operator><value>240</value></condition>
     </conditions>
     <action type="auto_query">
       <message>Verify systolic blood pressure measurement.</message>
     </action>
   </rule>
   """

   rule = BusinessLogicRule(
       id="BL-VS-001",
       name="Systolic Range Check",
       type="Query",
       status="Active",
       form="F_VS",
       logic=parse_logic_xml(xml_rule),
   )

   evaluator = LogicTreeEvaluator()
   normal_report = evaluator.evaluate_rules([rule], {"V_SYSBP": 122}, form_key="F_VS")
   alert_report = evaluator.evaluate_rules([rule], {"V_SYSBP": 255}, form_key="F_VS")
   assert not normal_report.results[0].triggered
   assert alert_report.results[0].triggered and alert_report.results[0].severity == "warning"

   print(f"122 mmHg triggered: {normal_report.results[0].triggered}")
   print(
       f"255 mmHg triggered: {alert_report.results[0].triggered} "
       f"({alert_report.results[0].severity}: {alert_report.results[0].message})"
   )

Inactive rules (``status="Inactive"``), UI-only actions (such as ``hide``,
``show``, or ``disable``), and cross-visit rules are automatically classified
and marked ``skipped=True`` with a structured ``skip_reason`` so single-record
validation pipelines remain deterministic.
