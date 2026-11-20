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

Validating Enrollment Data with a Data Dictionary
-------------------------------------------------

Both :meth:`~imednet_workflows.register_subjects.RegisterSubjectsWorkflow.register_subjects`
and :meth:`~imednet_workflows.register_subjects.RegisterSubjectsWorkflow.register_subjects_batch`
accept ``schema=data_dictionary`` (any
:class:`~imednet.validation.cache.ValidationSchemaProtocol`, such as a
:class:`~imednet.datadict.models.DataDictionary`). Passing a ``DataDictionary``
validates every enrollment ``Record`` against required ``Variable`` policies,
``Choice`` codelists, numeric/text bounds, and XML business logic rules *after*
``Site`` eligibility checks and *before* submitting chunked registration requests:

.. testcode::

   from imednet import ImednetSDK
   from imednet.datadict import Choice, DataDictionary, Form, Metadata, Variable
   from imednet.errors import ValidationError
   from imednet.models.records import RecordData, RegisterSubjectRequest
   from imednet.simulation import SimulationState, SimulationTransport
   from imednet_workflows.register_subjects import RegisterSubjectsWorkflow

   enrollment_dd = DataDictionary(
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
                       extraAttributes={"minValue": 18, "maxValue": 85},
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
           )
       ],
   )

   with ImednetSDK(
       simulation_mode=True,
       transport=SimulationTransport(SimulationState()),
   ) as sim_sdk:
       reg_workflow = RegisterSubjectsWorkflow(sim_sdk, filter_enrolled=True)
       summary = reg_workflow.register_subjects_batch(
           study_key="STUDY",
           subjects=[
               RegisterSubjectRequest(
                   formKey="F_DM",
                   siteName="SITE",
                   data=RecordData({"V_AGE": 42, "V_GENDER": "Female"}),
               )
           ],
           schema=enrollment_dd,
       )
       assert summary.total_records == 1

       try:
           reg_workflow.register_subjects_batch(
               study_key="STUDY",
               subjects=[
                   RegisterSubjectRequest(
                       formKey="F_DM",
                       siteName="SITE",
                       data=RecordData({"V_AGE": 15, "V_GENDER": "Unknown"}),
                   )
               ],
               schema=enrollment_dd,
           )
       except ValidationError as exc:
           assert "V_AGE" in str(exc) or "V_GENDER" in str(exc)

See :doc:`/how-to/data_dictionary` and :doc:`/explanation/schema_validation` for
details on loading Study Data Dictionaries and XML edit checks.
