UAT Workflow Guide
==================

The UAT (User Acceptance Testing) workflow (:class:`~imednet_workflows.uat.UATWorkflow`, also registered under ``imednet.workflows`` as ``uat`` via ``sdk.workflows.uat``) automates the end-to-end process of inspecting a ``Study``, generating a test specification, producing synthetic test ``Record`` payloads, submitting them to the API, and monitoring asynchronous ``Job`` batches until completion.

Visual Workflow
---------------

The following diagram illustrates the internal orchestration logic of the UAT workflow:

.. mermaid::

    sequenceDiagram
        accTitle: End-to-End UAT Workflow Orchestration Sequence
        accDescr: Sequence across User, UATWorkflow, StudySchemaInspector, UATSpecificationBuilder, SyntheticRecordGenerator, BulkRecordSubmissionWorkflow, and PollingManager
        participant User
        participant Orchestrator as UATWorkflow
        participant Inspector as StudySchemaInspector
        participant Builder as UATSpecificationBuilder
        participant Generator as SyntheticRecordGenerator
        participant Submitter as BulkRecordSubmissionWorkflow
        participant Poller as PollingManager

        User->>Orchestrator: run("STUDY_KEY")
        Orchestrator->>Inspector: inspect("STUDY_KEY")
        Inspector-->>Orchestrator: StudySnapshot
        Orchestrator->>Builder: build(StudySnapshot)
        Builder-->>Orchestrator: UATSpecification
        Orchestrator->>Generator: generate(UATSpecification, StudySnapshot)
        Generator-->>Orchestrator: List[GeneratedRecordSet]
        Orchestrator->>Submitter: submit("STUDY_KEY", List[GeneratedRecordSet])
        Submitter-->>Orchestrator: SubmissionResult (with Job IDs)
        Orchestrator->>Poller: monitor(Job IDs)
        Poller-->>Orchestrator: UATRunResult (Pass/Fail)
        Orchestrator-->>User: UATRunResult

Usage Example
-------------

The following code demonstrates how to use the ``UATWorkflow`` to automate test validation:

.. literalinclude:: /../examples/workflow_uat.py
   :language: python

Detailed Steps
--------------

1. **Inspection:** The workflow starts by fetching the current Study design (Forms, Variables, Sites, and Intervals) to build a ``StudySnapshot``.
2. **Specification Building:** :class:`~imednet_workflows.uat.UATSpecificationBuilder` creates a :class:`~imednet_workflows.uat.UATSpecification` mapping which tests (``REGISTER_SUBJECT``, ``UPDATE_SCHEDULED_RECORD``, ``CREATE_NEW_RECORD``) run against each Form.
3. **Generation:** :class:`~imednet_workflows.uat.SyntheticRecordGenerator` synthesizes deterministic Record payloads to fulfill the specification.
4. **Submission:** :class:`~imednet_workflows.uat.BulkRecordSubmissionWorkflow` pushes Subject registrations first, resolves enrolled Subject keys, and submits clinical Form batches.
5. **Monitoring:** Finally, the orchestrator polls Job endpoints until all batches reach ``COMPLETED`` or ``FAILED``, producing a :class:`~imednet_workflows.uat.UATRunResult` summary.

Enriching Synthetic Data with a Data Dictionary
-----------------------------------------------

REST ``/variables`` metadata exposes basic Variable types (such as ``Number``, ``Float``, and ``Coded``) without Choice codelists, numeric ranges, text length limits, or required-field policies. Passing a :class:`~imednet.datadict.models.DataDictionary` via ``data_dictionary=...`` to :class:`~imednet_workflows.uat.UATWorkflow`, :class:`~imednet_workflows.uat.UATSpecificationBuilder`, or :class:`~imednet_workflows.uat.SyntheticRecordGenerator` enriches each :class:`~imednet_workflows.uat.UATVariableSpec` with:

- ``coded_values`` from :class:`~imednet.datadict.models.Choice` definitions and ODM ``IN`` / ``EQ`` ``RangeCheck`` constraints (while filtering ``NE`` / ``NOTIN`` ``excluded_values``),
- ``min_value`` and ``max_value`` numeric bounds (including ``Soft`` warning bounds and strict ``GT`` / ``LT`` exclusivity for ``BOUNDARY`` and ``SYNTHETIC`` strategies),
- ``min_date`` and ``max_date`` ISO temporal bounds for ``Date``, ``DateTime``, ``Date/Time Precision``, and ``Time`` Variables,
- ``max_length`` text length caps, and
- ``required=True`` when the Variable's required policy is ``Yes``.

.. testcode::

    from imednet import ImednetSDK
    from imednet.datadict import Choice, DataDictionary, Form, Metadata, Variable
    from imednet.simulation import SimulationState, SimulationTransport
    from imednet_workflows.uat import (
        StudySchemaInspector,
        SyntheticRecordGenerator,
        UATSpecificationBuilder,
        UATWorkflow,
    )

    uat_dd = DataDictionary(
        metadata=Metadata.create(study_key="STUDY"),
        forms=[
            Form(
                formKey="F_DM",
                formName="Demographics",
                formType="Enrollment",
                variables=[
                    Variable(
                        variableName="V_AGE",
                        variableType="Integer",
                        required="Yes",
                        extraAttributes={"minValue": 21, "maxValue": 65},
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
            ),
            Form(
                formKey="F_VS",
                formName="Vital Signs",
                formType="Standard",
                variables=[
                    Variable(
                        variableName="V_WEIGHT",
                        variableType="Float",
                        required="Yes",
                        extraAttributes={"minValue": 45.0, "maxValue": 120.0},
                    )
                ],
            ),
        ],
    )

    with ImednetSDK(
        simulation_mode=True,
        transport=SimulationTransport(SimulationState()),
    ) as sdk:
        snapshot = StudySchemaInspector(sdk).inspect("STUDY")
        spec = UATSpecificationBuilder(data_dictionary=uat_dd).build(
            snapshot,
            subject_count=2,
        )
        record_sets = SyntheticRecordGenerator(
            seed=7,
            data_dictionary=uat_dd,
        ).generate(spec, snapshot)
        dm_payload = record_sets[0].payloads[0]["data"]
        assert 21 <= int(dm_payload["V_AGE"]) <= 65
        assert dm_payload["V_GENDER"] in {"Female", "Male"}

        uat_result = UATWorkflow(
            sdk,
            poll_interval=0.0,
            seed=7,
            data_dictionary=uat_dd,
        ).run("STUDY", subject_count=2)
        assert uat_result.overall_success is True

Automated Edit Check Verification (``UATExecutionEngine``)
----------------------------------------------------------

While ``UATWorkflow`` verifies positive Record submission across Study Forms, :class:`~imednet_workflows.uat.UATExecutionEngine` verifies active :class:`~imednet.datadict.models.BusinessLogicRule` definitions by synthesizing **negative** Record payloads designed to trigger server-side edit checks:

1. Parses active ``BusinessLogicRule`` definitions and XML ``<logic>`` condition trees via :class:`~imednet.datadict.logic.LogicTreeEvaluator` (skipping UI-only visibility rules and cross-Form rules).
2. Calls ``generate_negative_test_case(rule)`` to construct a Form payload that violates the target rule condition while populating required companion Variables with valid defaults.
3. Submits each negative payload via ``sdk.create_record`` and polls all resulting Job batches.
4. Classifies each rule into :class:`~imednet_workflows.uat.EditCheckVerificationReport` with :class:`~imednet_workflows.uat.EditCheckResultStatus`:

   - ``EditCheckResultStatus.PASS`` (``actual_response == "Rejected"``): The EDC server or batch Job rejected the invalid payload as expected.
   - ``EditCheckResultStatus.FAIL`` (``actual_response == "Accepted"``): The negative payload was accepted without triggering a server-side rejection.
   - ``EditCheckResultStatus.SKIPPED``: Rule lacked a target Variable for negative data synthesis.
   - ``EditCheckResultStatus.ERROR``: Server ``5xx`` error or Job polling timeout.

.. note::

   In offline ``SimulationTransport`` mode, the simulated transport accepts syntactically valid ``202`` Record batches without evaluating server-side EDC edit checks, so ``actual_response`` is ``"Accepted"``. Against a live UAT Study environment with active server edit checks, rejected batches report ``EditCheckResultStatus.PASS``.

.. testcode::

    from imednet import ImednetSDK
    from imednet.datadict import (
        BusinessLogicRule,
        DataDictionary,
        Form,
        Metadata,
        Variable,
        parse_logic_xml,
    )
    from imednet.simulation import SimulationState, SimulationTransport
    from imednet_workflows.uat import UATExecutionEngine

    rule_xml = (
        "<rule>"
        '<conditions><condition variable="V_WEIGHT" operator="&lt;" value="35"/></conditions>'
        '<actions><action type="error" message="Weight must be at least 35 kg."/></actions>'
        "</rule>"
    )

    verification_dd = DataDictionary(
        metadata=Metadata.create(study_key="STUDY"),
        forms=[
            Form(
                formKey="F_VS",
                formName="Vital Signs",
                variables=[
                    Variable(
                        variableName="V_WEIGHT",
                        variableType="Float",
                        required="Yes",
                        extraAttributes={"minValue": 35.0, "maxValue": 180.0},
                    )
                ],
                businessLogic=[
                    BusinessLogicRule(
                        id="201",
                        name="Weight Lower Bound Rule",
                        type="Form",
                        status="Active",
                        form="F_VS",
                        logic=parse_logic_xml(rule_xml),
                    )
                ],
            )
        ],
    )

    with ImednetSDK(
        simulation_mode=True,
        transport=SimulationTransport(SimulationState()),
    ) as verify_sdk:
        engine = UATExecutionEngine(verify_sdk, verification_dd, poll_interval=0.0)
        verification_report = engine.run_verification("STUDY", form_scope="F_VS")
        assert verification_report.total_rules == 1
        assert verification_report.results[0]["rule_name"] == "Weight Lower Bound Rule"
        assert float(verification_report.results[0]["test_data"]["V_WEIGHT"]) < 35.0

See :doc:`/how-to/data_dictionary` for loading Study Data Dictionaries and evaluating XML business logic offline before UAT runs.
