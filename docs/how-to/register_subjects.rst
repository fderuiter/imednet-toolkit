Register Subjects Workflow
==========================

The :class:`~imednet_workflows.register_subjects.RegisterSubjectsWorkflow`
(available directly or via ``sdk.workflows.register_subjects``) validates that
target ``Site`` names exist and are open for enrollment before submitting
``Subject`` registration ``Record`` items.

Single-Submission Example
-------------------------

For small cohorts, :meth:`~imednet_workflows.register_subjects.RegisterSubjectsWorkflow.register_subjects`
submits registration payloads and returns the final ``Job``:

.. testcode::

   from typing import Any, cast
   from imednet import ImednetSDK
   from imednet.models.records import RecordData, RegisterSubjectRequest
   from imednet_workflows.register_subjects import RegisterSubjectsWorkflow

   sdk = ImednetSDK(api_key="API_KEY", security_key="SECURITY_KEY")
   workflow = RegisterSubjectsWorkflow(cast(Any, sdk))
   subjects = [RegisterSubjectRequest(formKey="F_DM", siteName="SITE", data=RecordData({}))]
   job = workflow.register_subjects(study_key="STUDY", subjects=subjects)
   print(job)

Chunked Batch Enrollment with ``register_subjects_batch``
---------------------------------------------------------

When enrolling larger cohorts or re-running idempotent enrollment loads, use
:meth:`~imednet_workflows.register_subjects.RegisterSubjectsWorkflow.register_subjects_batch`.
It validates ``Site`` eligibility once across the entire input sequence, splits
submissions into bounded chunks via :class:`~imednet.utils.batch_coordinator.BatchCoordinator`,
and returns a :class:`~imednet.utils.batch_coordinator.BatchSubmissionSummary`:

* ``skip_existing`` (``bool``) and ``filter_enrolled`` (``bool | None``): Query active
  non-deleted ``Subject`` items in the ``Study`` first and skip already enrolled
  identifiers as well as duplicate keys within the same batch (recording the count on
  ``summary.skipped_count``).
* ``subject_key_field`` (``str | None``): Inspect a custom ``Variable`` key inside
  ``data`` (for example ``"SUBJID"`` or ``"SCREENING_ID"``) when deduplicating raw
  registration dictionaries that omit top-level ``subjectKey``.
* ``chunk_size`` (``int | None``): Override the per-POST ``Record`` chunk size.
* ``wait_for_completion`` (``bool``): Poll all submitted ``Job`` batch IDs to a terminal
  state and attach ``summary.poll_summary``.

.. testcode::

   from typing import Any, cast
   from imednet import ImednetSDK
   from imednet.simulation import reset_default_simulation_state
   from imednet_workflows.register_subjects import RegisterSubjectsWorkflow

   batch_sdk = ImednetSDK(simulation_mode=True)
   batch_workflow = RegisterSubjectsWorkflow(cast(Any, batch_sdk), batch_size=50)

   raw_registrations = [
       {
           "formKey": "F_DM",
           "siteName": "SITE",
           "data": {"SUBJID": "SUBJ-001", "V_AGE": 41, "V_GENDER": "F"},
       },
       {
           "formKey": "F_DM",
           "siteName": "SITE",
           "data": {"SUBJID": "SUBJ-NEW-201", "V_AGE": 52, "V_GENDER": "M"},
       },
   ]

   batch_summary = batch_workflow.register_subjects_batch(
       study_key="STUDY",
       subjects=raw_registrations,
       wait_for_completion=True,
       poll_interval=1,
       chunk_size=25,
       skip_existing=True,
       filter_enrolled=True,
       subject_key_field="SUBJID",
   )
   print(batch_summary.total_records, batch_summary.skipped_count, batch_summary.all_successful)
   reset_default_simulation_state()
