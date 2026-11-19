UAT Workflow Guide
==================

The UAT (User Acceptance Testing) workflow automates the end-to-end process of inspecting a study, generating a test specification, producing synthetic test records, submitting them to the API, and monitoring the asynchronous jobs until completion.

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

1. **Inspection:** The workflow starts by fetching the current study design (forms, variables, sites) to build a ``StudySnapshot``.
2. **Specification Building:** A specification is created that maps out exactly which tests (e.g., ``REGISTER_SUBJECT``, ``CREATE_NEW_RECORD``) need to be run against which forms.
3. **Generation:** Synthetic data payloads are generated to fulfill the specification.
4. **Submission:** The data is pushed to the API using the bulk submission workflow.
5. **Monitoring:** Finally, the orchestrator polls the job endpoints until all batches are either COMPLETED or FAILED, generating a detailed summary.
