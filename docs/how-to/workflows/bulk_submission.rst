Bulk Submission Guide
=====================

The Bulk Record Submission Workflow manages the complexity of uploading large volumes of data by orchestrating a two-phase ingestion process. It ensures that required subject registrations complete before any subsequent data is uploaded.

Two-Phase Process
-----------------

When submitting records in bulk, the operations are automatically separated into two phases:

1. **Registration Phase:** All ``REGISTER_SUBJECT`` test types are batched and submitted. The orchestrator polls until these registrations complete successfully.
2. **Data Phase:** All other record types (e.g., ``CREATE_NEW_RECORD``, ``UPDATE_SCHEDULED_RECORD``) are subsequently submitted, preventing missing subject errors.

Visual Workflow
---------------

The following diagram illustrates the internal two-phase orchestration:

.. mermaid::

    sequenceDiagram
        accTitle: Two-Phase Bulk Record Submission Sequence
        accDescr: Sequence showing Phase 1 Subject registration batch submission and Job polling via ImednetFacade.poll_jobs and JobPoller followed by Phase 2 Record data batch submission
        participant User
        participant Orchestrator as BulkRecordSubmissionWorkflow
        participant API as iMednet API
        participant Facade as ImednetFacade.poll_jobs
        participant Poller as JobPoller

        User->>Orchestrator: submit(Study, RecordSets)
        Note over Orchestrator,API: Phase 1: Registration
        Orchestrator->>API: create_record(Registration Batches)
        API-->>Orchestrator: Job IDs (Registration)
        Orchestrator->>Facade: poll_jobs(Study, Registration Job IDs)
        Facade->>Poller: poll_many(Study, Registration Job IDs)
        Poller-->>Facade: JobStatus Results
        Facade-->>Orchestrator: Completed
        Note over Orchestrator,API: Phase 2: Data Submission
        Orchestrator->>API: create_record(Data Batches)
        API-->>Orchestrator: Job IDs (Data)
        Orchestrator-->>User: SubmissionResult

Usage Example
-------------

The following script sets up a bulk submission instance and illustrates the two-phase workflow:

.. literalinclude:: /../examples/workflow_bulk_submission.py
   :language: python

Error Handling
--------------

If the registration phase fails (i.e. one or more ``Job`` items return a ``FAILED`` state or record-level errors), the orchestrator raises a ``BulkSubmissionError`` immediately, avoiding cascading failures on the dependent ``Record`` data phase.

Direct Chunked Writes with ``create_or_update_records_batch``
-------------------------------------------------------------

When ``Subject`` enrollment is already complete and you want schema-validated, chunked ``Record`` writes without ``GeneratedRecordSet`` wrappers, use :meth:`~imednet_workflows.record_update.RecordUpdateWorkflow.create_or_update_records_batch` (or ``async_create_or_update_records_batch`` for ``AsyncImednetSDK``) via ``sdk.workflows.record_update``:

.. testcode::

   from typing import Any, cast
   from imednet import ImednetSDK
   from imednet.simulation import reset_default_simulation_state
   from imednet_workflows.record_update import RecordUpdateWorkflow

   chunk_sdk = ImednetSDK(simulation_mode=True)
   chunk_updater = RecordUpdateWorkflow(cast(Any, chunk_sdk), batch_size=100)

   chunk_summary = chunk_updater.create_or_update_records_batch(
       study_key="STUDY",
       records_data=[
           {
               "formKey": "F_VS",
               "siteName": "SITE",
               "subjectKey": "SUBJ-001",
               "intervalName": "Baseline",
               "data": {"V_WEIGHT": 71.0},
           }
       ],
       wait_for_completion=True,
       poll_interval=1,
       chunk_size=50,
   )
   print(chunk_summary.batch_ids, chunk_summary.all_successful)
   reset_default_simulation_state()

See :doc:`record_update` and :doc:`/how-to/job_polling` for ``BatchSubmissionSummary`` and partial ``JobStatus`` failure triage details.

Pre-Submission Data Dictionary Validation
-----------------------------------------

Before uploading large ``Record`` batches, pass ``schema=data_dictionary`` to
:class:`~imednet_workflows.record_update.RecordUpdateWorkflow` (or per call on
``create_or_update_records_batch``, ``create_or_update_records``, and
``sdk.records.create_batch``) to validate every chunk locally against ``Form``
required policies, ``Choice`` codelists, numeric/length bounds, and conditional XML
edit checks without extra ``/forms`` or ``/variables`` API round-trips:

.. testcode::

   from imednet import ImednetSDK
   from imednet.datadict import DataDictionary, Form, Metadata, Variable
   from imednet.errors import ValidationError
   from imednet.simulation import SimulationState, SimulationTransport
   from imednet_workflows.record_update import RecordUpdateWorkflow

   vitals_dd = DataDictionary(
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
                       extraAttributes={"minValue": 35.0, "maxValue": 200.0},
                   )
               ],
           )
       ],
   )

   with ImednetSDK(
       simulation_mode=True,
       transport=SimulationTransport(SimulationState()),
   ) as sdk:
       updater = RecordUpdateWorkflow(sdk, batch_size=50, schema=vitals_dd)
       batch_summary = updater.create_or_update_records_batch(
           "STUDY",
           [
               {
                   "formKey": "F_VS",
                   "subjectKey": "SUB1",
                   "intervalName": "Baseline",
                   "data": {"V_WEIGHT": 71.4},
               }
           ],
       )
       assert batch_summary.total_records == 1

       direct_summary = sdk.records.create_batch(
           "STUDY",
           [
               {
                   "formKey": "F_VS",
                   "subjectKey": "SUB1",
                   "intervalName": "Baseline",
                   "data": {"V_WEIGHT": 72.0},
               }
           ],
           chunk_size=50,
           schema=vitals_dd,
       )
       assert direct_summary.total_records == 1

       try:
           updater.create_or_update_records_batch(
               "STUDY",
               [
                   {
                       "formKey": "F_VS",
                       "subjectKey": "SUB1",
                       "intervalName": "Baseline",
                       "data": {"V_WEIGHT": 10.0},
                   }
               ],
           )
       except ValidationError as exc:
           assert "V_WEIGHT" in str(exc)

See :doc:`/how-to/data_dictionary` for loading a ``DataDictionary`` from CSV
exports, CDISC ODM 1.3 XML, REDCap CSV, or the REST API.
