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
        accDescr: Sequence showing Phase 1 subject registration batch submission and job polling followed by Phase 2 clinical data batch submission
        participant User
        participant Orchestrator as BulkRecordSubmissionWorkflow
        participant API as iMednet API
        participant Poller as PollingManager

        User->>Orchestrator: submit(Study, RecordSets)
        Note over Orchestrator,API: Phase 1: Registration
        Orchestrator->>API: create_record(Registration Batches)
        API-->>Orchestrator: Job IDs (Registration)
        Orchestrator->>Poller: poll(Registration Job IDs)
        Poller-->>Orchestrator: Completed
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

If the registration phase fails (i.e. one or more jobs return a FAILED state), the orchestrator will raise a ``BulkSubmissionError`` immediately, avoiding cascading failures on the dependent data phase.
