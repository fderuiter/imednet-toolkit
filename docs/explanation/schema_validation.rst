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

``SchemaCache`` and ``DataDictionary`` enforce different levels of Study metadata checks
because the REST ``/forms`` and ``/variables`` endpoints expose only basic Variable types,
whereas a :doc:`Data Dictionary </how-to/data_dictionary>` carries full Form constraints,
Choice codelists, and conditional XML business logic rules.

When :func:`~imednet.validation.cache.validate_record_data` receives a ``BaseSchemaCache``
instance, it verifies Form existence, rejects unknown Variable names, checks scalar Python
types, and then **returns early**—skipping required-field policies, Choice membership,
numeric/length bounds, and XML edit checks. Passing a ``DataDictionary`` executes the full
constraint and rule pipeline:

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
   * - Scalar & ISO temporal data types (``Number``, ``Integer``, ``Float``, ``Date``, ``DateTime``, ``DurationDateTime``, ``IntervalDateTime``, ``Date/Time Precision``, ``Time``, ``Text``)
     - Enforced
     - Enforced
   * - Required Variable policies (``Yes`` raises error; ``Auto Query`` logs warning; ODM/REDCap conditional exemptions)
     - Skipped
     - Enforced
   * - Choice codelist membership (``Radio``, ``Single Select``, ``Multi Select``, ``Checkbox``, ``Yes/No``)
     - Skipped
     - Enforced
   * - Numeric (``minValue`` / ``maxValue``), ISO temporal (``minDate`` / ``maxDate``), ODM ``RangeCheck`` (``Hard`` / ``Soft``), & text length (``minLength`` / ``maxLength``) bounds
     - Skipped
     - Enforced
   * - Conditional XML, ODM ``ConditionDef`` / ``MethodDef``, & REDCap ``branchingLogic`` AST rules via :class:`~imednet.datadict.logic.LogicTreeEvaluator`
     - Skipped
     - Enforced

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
metadata and sample Records without an API connection. Combine these
functions with ``SchemaCache`` to run type-level validation locally::

    from types import SimpleNamespace
    from imednet.testing import fake_data
    from imednet.validation.cache import SchemaCache

    forms = fake_data.fake_forms_for_cache(1, study_key="S")
    variables = fake_data.fake_variables_for_cache(forms, vars_per_form=2,
                                                   study_key="S")

    forms_ep = SimpleNamespace(list=lambda **_: forms)
    vars_ep = SimpleNamespace(list=lambda form_id=None, **__: [
        v for v in variables if form_id is None or v.form_id == form_id
    ])

    schema = SchemaCache()
    schema.refresh(forms_ep, vars_ep, study_key="S")

    record = fake_data.fake_record(schema)
