Schema Validation Flow
======================

Pre-submission Record validation in the SDK is built around
:class:`~imednet.validation.cache.ValidationSchemaProtocol`, a unified protocol
implemented by both :class:`~imednet.validation.cache.BaseSchemaCache`
(:class:`~imednet.validation.cache.SchemaCache` and
:class:`~imednet.validation.cache.AsyncSchemaCache`) and
:class:`~imednet.datadict.models.DataDictionary`. Passing either implementation via
``schema=...`` validates Record payloads locally and raises
:class:`~imednet.errors.ValidationError` before an invalid payload is sent to the API.

SchemaCache vs. DataDictionary
------------------------------

``SchemaCache`` and ``DataDictionary`` enforce different scopes of Study metadata checks
because the REST ``/forms`` and ``/variables`` endpoints expose Form identifiers, scalar
Variable types, ``required`` policies, and ``deleted`` / ``disabled`` retirement metadata,
whereas a :doc:`Data Dictionary </how-to/data_dictionary>` additionally carries Choice
codelists, numeric and length bounds, CDISC ODM ``<RangeCheck>`` definitions, and conditional
XML business logic rules.

When :func:`~imednet.validation.cache.validate_record_data` receives a
:class:`~imednet.validation.cache.BaseSchemaCache` instance (:class:`~imednet.validation.cache.SchemaCache`
or :class:`~imednet.validation.cache.AsyncSchemaCache`), it verifies Form existence, rejects
unknown Variable names, blocks scalar and nested ``***MASKED***`` PHI sentinels
(``rule_id="masked_phi"``) before type checks, validates scalar Variable types across both
active and retired Variables, and enforces active Required Variable policies (``Yes`` and
``Auto Query``)—skipping only Choice membership, numeric/length and ODM ``<RangeCheck>``
bounds, and XML edit checks. Passing a ``DataDictionary`` executes the full constraint and
rule pipeline:

.. list-table:: Validation Coverage by Schema Implementation
   :header-rows: 1
   :widths: 38 31 31

   * - Check
     - ``SchemaCache`` / ``AsyncSchemaCache``
     - ``DataDictionary``
   * - Form existence & ``formId`` to ``formKey`` resolution
     - Enforced
     - Enforced
   * - Unknown Variable detection on Form
     - Enforced
     - Enforced
   * - Masked PHI sentinel rejection (``***MASKED***``, ``rule_id="masked_phi"``)
     - Enforced
     - Enforced
   * - Scalar Variable data type (``Number``, ``Integer``, ``Float``, ``Date``, ``Text``), including retired (``deleted`` / ``disabled``) Variables
     - Enforced
     - Enforced
   * - Required Variable policies (``Yes`` raises error; ``Auto Query`` logs warning) on active Variables
     - Enforced
     - Enforced
   * - Choice codelist membership (``Radio``, ``Single Select``, ``Multi Select``, ``Checkbox``, ``Yes/No``)
     - Skipped
     - Enforced
   * - Numeric (``minValue`` / ``maxValue``), text length (``minLength`` / ``maxLength``), & CDISC ODM :class:`~imednet.datadict.RangeCheckConstraint` bounds
     - Skipped
     - Enforced
   * - Conditional XML business logic rules via :class:`~imednet.datadict.logic.LogicTreeEvaluator`
     - Skipped
     - Enforced

Masked PHI Sentinels and Retired Variables
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Before running Variable type checks, :func:`~imednet.validation.cache.validate_record_data`
inspects every submitted value (both scalar strings and nested containers such as lists,
tuples, sets, and dictionaries) for the ``***MASKED***`` redaction token. Any matching
Variable fails immediately with ``rule_id="masked_phi"`` (and skips redundant downstream type
errors for that Variable) so redacted export payloads cannot overwrite clinical data in a
Study.

When hydrating :class:`~imednet.validation.cache.BaseSchemaCache`, Variables marked
``deleted`` or ``disabled`` are stored in ``_retired_form_variables`` (while still registering
the Form in ``_known_forms`` and preserving ``formId`` to ``formKey`` links, even when every
Variable on a Form is retired). During validation:

* Omitting a ``deleted`` or ``disabled`` Variable never triggers ``required`` or
  ``required_auto_query`` issues, even if the historical Variable metadata declared
  ``required=True`` or ``required="Yes"``.
* Supplying a value for a retired Variable in a historical Record payload does not fail with
  ``rule_id="unknown_variable"``, and still undergoes ``***MASKED***`` sentinel and scalar
  data type checks (``rule_id="type"``).

API-Backed Type Checks (``SchemaValidator``)
--------------------------------------------

When no offline Data Dictionary is supplied, :class:`~imednet.validation.cache.SchemaValidator`
and :class:`~imednet.validation.cache.AsyncSchemaValidator` manage a ``SchemaCache`` or
``AsyncSchemaCache`` backed by :class:`~imednet.endpoints.forms.FormsEndpoint` and
:class:`~imednet.endpoints.variables.VariablesEndpoint`. If Variable metadata for a Form is
not yet cached, the validator refreshes it from the API on demand.

:class:`~imednet.validation.cache.BaseSchemaCache` partitions cached Variable definitions by
``(study_key, form_key)`` and Form ID resolutions by ``(study_key, form_id)``, so
multi-Study validators and workflows can validate Studies that share identical ``formKey``
or ``formId`` values without cross-Study cache collisions.

The diagram below outlines the API-backed ``SchemaValidator`` flow.

.. mermaid:: /diagrams/schema_validation_7.mmd

Record payloads can also be validated asynchronously. Use
``AsyncSchemaValidator.validate_batch`` with ``AsyncImednetSDK`` before
submitting Records::

    async with AsyncImednetSDK() as sdk:
        validator = AsyncSchemaValidator(sdk)
        await validator.validate_batch(study_key, records)
        await sdk.records.create(study_key, records, schema=validator.schema)

Rule and Constraint Validation (``DataDictionary``)
---------------------------------------------------

To enforce required Variables, Choice codelists, numeric/length bounds, and conditional XML
edit checks before calling ``POST /studies/{studyKey}/records``, pass ``schema=data_dictionary``
to ``sdk.records.create``, ``sdk.records.create_batch``,
:class:`~imednet_workflows.record_update.RecordUpdateWorkflow`, or
:class:`~imednet_workflows.register_subjects.RegisterSubjectsWorkflow`:

.. testcode::

    from imednet import ImednetSDK
    from imednet.datadict import (
        BusinessLogicRule,
        Choice,
        DataDictionary,
        Form,
        Metadata,
        Variable,
        parse_logic_xml,
    )
    from imednet.errors import ValidationError
    from imednet.simulation import SimulationState, SimulationTransport

    logic_xml = (
        "<rule>"
        '<conditions><condition variable="V_AGE" operator="&lt;" value="18"/></conditions>'
        '<actions><action type="error" message="Enrolled Subjects must be at least 18 years old."/></actions>'
        "</rule>"
    )

    dd = DataDictionary(
        metadata=Metadata.create(study_key="STUDY"),
        forms=[
            Form(
                formKey="F_DM",
                formName="Demographics",
                variables=[
                    Variable(
                        variableName="V_AGE",
                        variableType="Integer",
                        required="Yes",
                        extraAttributes={"minValue": 0, "maxValue": 120},
                    ),
                    Variable(
                        variableName="V_GENDER",
                        variableType="Radio",
                        required="Yes",
                        choices=[
                            Choice(choiceValue="Female", choiceText="Female"),
                            Choice(choiceValue="Male", choiceText="Male"),
                        ],
                    ),
                ],
                businessLogic=[
                    BusinessLogicRule(
                        id="101",
                        name="Adult Consent Check",
                        type="Form",
                        status="Active",
                        form="F_DM",
                        logic=parse_logic_xml(logic_xml),
                    )
                ],
            )
        ],
    )

    with ImednetSDK(
        simulation_mode=True,
        transport=SimulationTransport(SimulationState()),
    ) as sdk:
        job = sdk.records.create(
            "STUDY",
            [
                {
                    "formKey": "F_DM",
                    "siteName": "SITE",
                    "data": {"V_AGE": 34, "V_GENDER": "Female"},
                }
            ],
            schema=dd,
        )
        assert job.batch_id is not None

        try:
            sdk.records.create(
                "STUDY",
                [
                    {
                        "formKey": "F_DM",
                        "siteName": "SITE",
                        "data": {"V_AGE": 16, "V_GENDER": "Female"},
                    }
                ],
                schema=dd,
            )
        except ValidationError as exc:
            assert "Adult Consent Check" in str(exc)

See :doc:`/how-to/data_dictionary` for loading a ``DataDictionary`` from CSV, CDISC ODM XML,
REDCap CSV, or the REST API.

Offline SchemaCache Example
---------------------------

``imednet.testing.fake_data`` provides helpers for generating Form
metadata and sample Records without an API connection, and ``SchemaCache.populate``
lets unit tests verify Required Variable policies (``Yes`` and ``Auto Query``),
``***MASKED***`` sentinel blocking, and retired Variable type checks offline:

.. testcode::

    from types import SimpleNamespace
    from typing import Any, cast
    from imednet.models.variables import Variable as ApiVariable
    from imednet.testing import fake_data
    from imednet.validation.cache import SchemaCache, validate_record_data

    forms = fake_data.fake_forms_for_cache(1, study_key="S")
    variables = fake_data.fake_variables_for_cache(
        forms, vars_per_form=2, study_key="S"
    )

    forms_ep = cast(Any, SimpleNamespace(list=lambda **_: forms))
    vars_ep = cast(
        Any,
        SimpleNamespace(
            list=lambda form_id=None, **__: [
                v for v in variables if form_id is None or v.form_id == form_id
            ]
        ),
    )

    schema = SchemaCache()
    schema.refresh(forms_ep, vars_ep, study_key="S")
    sample_record = fake_data.fake_record(schema)
    assert "recordData" in sample_record

    policy_cache = SchemaCache()
    policy_cache.populate(
        [
            ApiVariable.model_validate(
                {
                    "variableName": "V_SUBJ_AGE",
                    "variableType": "Integer",
                    "formId": 501,
                    "formKey": "F_SCREEN",
                    "required": "Yes",
                }
            ),
            ApiVariable.model_validate(
                {
                    "variableName": "V_NOTES",
                    "variableType": "Multi Select",
                    "formId": 501,
                    "formKey": "F_SCREEN",
                    "required": "Auto Query",
                }
            ),
            ApiVariable.model_validate(
                {
                    "variableName": "V_LEGACY_SCORE",
                    "variableType": "Integer",
                    "formId": 501,
                    "formKey": "F_SCREEN",
                    "deleted": True,
                    "required": True,
                }
            ),
        ],
        study_key="STUDY",
    )
    assert "V_LEGACY_SCORE" in policy_cache._retired_form_variables[("STUDY", "F_SCREEN")]

    # Missing active 'Yes' raises required error; 'Auto Query' records a warning; retired Variable is not required
    missing_report = validate_record_data(
        policy_cache,
        "F_SCREEN",
        {},
        study_key="STUDY",
        collect_errors=True,
    )
    assert [e.rule_id for e in missing_report.errors] == ["required"]
    assert [w.rule_id for w in missing_report.warnings] == ["required_auto_query"]

    # Scalar/nested '***MASKED***' sentinels fail with 'masked_phi'; retired Variables are still type-checked
    masked_and_retired = validate_record_data(
        policy_cache,
        "F_SCREEN",
        {
            "V_SUBJ_AGE": "***MASKED***",
            "V_NOTES": ["***MASKED***"],
            "V_LEGACY_SCORE": "not-an-int",
        },
        study_key="STUDY",
        collect_errors=True,
    )
    assert [e.rule_id for e in masked_and_retired.errors] == [
        "masked_phi",
        "masked_phi",
        "type",
    ]
