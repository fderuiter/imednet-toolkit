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

Stateful Dry Runs, Saved Specifications, and CLI Safeguards
-----------------------------------------------------------

To preview constrained UAT payloads before writing ``Record`` batches to an active ``Site``, pass ``dry_run=True`` to :meth:`~imednet_workflows.uat.UATWorkflow.run` (or ``--dry-run`` on the CLI). Dry-run execution runs ``INSPECTING``, ``BUILDING_SPEC``, and ``GENERATING``, validates every generated payload locally via :func:`~imednet.spi.validation.validate_records_batch` against the supplied ``schema`` / ``data_dictionary`` (or ``StudySnapshot`` variables), transitions ``GENERATING -> COMPLETED`` with zero API writes, and attaches a :class:`~imednet.spi.validation.BatchValidationReport` to :attr:`~imednet_workflows.uat.UATRunResult.validation_report`.

Both Python ``UATWorkflow`` and ``imednet workflows uat`` enforce strict pre-write safeguards:

- Explicit ``site_name`` / ``--site-name`` arguments and custom ``UATSpecification`` ``Site`` entries must exist in the ``Study`` and be enrollment-eligible (no silent fallback to another active ``Site``).
- Explicit ``enabled_form_keys`` / ``--form-key`` filters reject unknown or disabled ``Form`` keys, and zero-Form or zero-Record live runs fail instead of reporting a vacuous pass.
- ``--subject-count`` is validated within ``1..100`` and ``--email-notify`` is normalized and header-checked during CLI preflight (and interactive prompts) before ``StudySchemaInspector`` sends network requests.

.. testcode::

    with ImednetSDK(
        simulation_mode=True,
        transport=SimulationTransport(SimulationState()),
    ) as dry_sdk:
        dry_result = UATWorkflow(
            dry_sdk,
            seed=42,
            data_dictionary=uat_dd,
        ).run("STUDY", subject_count=2, dry_run=True)
        assert dry_result.dry_run is True
        assert dry_result.overall_success is True
        assert dry_result.validation_report.is_valid is True
        assert dry_result.submission_result is None

From the terminal, export a customizable ``UATSpecification`` plan with ``build-spec`` (supporting JSON, CDISC ODM XML, REDCap CSV, and CSV directory Data Dictionaries) and execute dry or live runs with ``imednet workflows uat run``:

.. code-block:: bash

   imednet workflows uat build-spec STUDY --data-dictionary study_dd.json --seed 42 -o uat_spec.json
   imednet workflows uat run STUDY --spec uat_spec.json --data-dictionary study_dd.json --seed 42 --dry-run
   imednet workflows uat run STUDY --schema study_dd.json --dry-run --format json
   imednet workflows uat run STUDY --site-name SITE --form-key F_DM --subject-count 2 --email-notify ops@example.com

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

Path and Partition Validation in UAT and Config Stores
------------------------------------------------------

Workflow engines enforce dynamic URL and filesystem path boundaries via
:func:`~imednet.utils.validate_path_segment`, :func:`~imednet.utils.build_safe_path`,
and :class:`~imednet.errors.PathTraversalValidationError` (``status_code=400``):

* **UAT verification (``UATExecutionEngine``):** ``run_verification(study_key, ...)``
  validates ``study_key`` with :func:`~imednet.utils.validate_path_segment` before
  inspecting ``Form``, ``Subject``, ``Site``, ``Interval``, ``Visit``, ``Variable``,
  ``Query``, or ``Record`` resources. When asynchronous ``Job`` creation responses are
  parsed, ``_extract_job_batch_id`` rejects raw ASCII control characters and validates
  the stripped ``batch_id`` before polling ``/studies/{study_key}/jobs/{batch_id}`` via
  :func:`~imednet.utils.build_safe_path`.
* **Versioned Study configuration store (``ConfigVersionStore``):** When resolving
  Study-scoped SQLite ledgers, ``ConfigVersionStore`` validates active ``Study``
  partition keys (via ``validate_partition_key``) and verifies that the resolved ledger
  directory stays inside the configured base directory (``is_relative_to``), raising
  :class:`~imednet.errors.PathTraversalValidationError` if a context key attempts to
  escape the root directory.

.. testcode::

    from pathlib import Path
    import tempfile
    from imednet import ImednetSDK
    from imednet.core.context import study_context
    from imednet.datadict import DataDictionary, Metadata
    from imednet.errors import PathTraversalValidationError
    from imednet.simulation import SimulationState, SimulationTransport
    from imednet.utils import build_safe_path, validate_path_segment
    from imednet_workflows.config_version_control import ConfigVersionStore
    from imednet_workflows.uat import UATExecutionEngine

    assert (
        build_safe_path("studies", "STUDY", "jobs", "B_1")
        == "studies/STUDY/jobs/B_1"
    )
    assert validate_path_segment("B_1", name="batch_id") == "B_1"

    guard_dd = DataDictionary(metadata=Metadata.create(study_key="STUDY"), forms=[])
    with ImednetSDK(
        simulation_mode=True,
        transport=SimulationTransport(SimulationState()),
    ) as guard_sdk:
        guard_engine = UATExecutionEngine(guard_sdk, guard_dd, poll_interval=0.0)
        try:
            guard_engine.run_verification("../escape")
        except PathTraversalValidationError as study_path_err:
            assert study_path_err.status_code == 400

        for bad_job_payload in ({"batchId": "../escape"}, {"batchId": "batch\n01"}):
            try:
                guard_engine._extract_job_batch_id(bad_job_payload)
            except PathTraversalValidationError as batch_path_err:
                assert batch_path_err.status_code == 400

    with tempfile.TemporaryDirectory() as tmp_ledger_dir:
        base_ledger = Path(tmp_ledger_dir) / "config_versions.sqlite3"
        with study_context("../escape"):
            try:
                ConfigVersionStore._get_dynamic_db_path_no_create(base_ledger)
            except PathTraversalValidationError as store_path_err:
                assert store_path_err.status_code == 400
