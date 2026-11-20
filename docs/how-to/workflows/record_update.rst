Record Update Workflow
======================

The :class:`~imednet_workflows.record_update.RecordUpdateWorkflow` plugin
(registered under ``imednet.workflows`` as ``record_update`` and accessible via
``sdk.workflows.record_update``) validates ``Form`` and ``Variable`` schemas once
before submitting single or chunked ``Record`` payloads and polling resulting
``Job`` executions.

Chunked Batch Writes (``create_or_update_records_batch``)
---------------------------------------------------------

Use :meth:`~imednet_workflows.record_update.RecordUpdateWorkflow.create_or_update_records_batch`
(or ``async_create_or_update_records_batch`` on ``AsyncImednetSDK``) to validate a
list of ``Record`` dictionaries once against the cached ``Study`` schema, split
them into bounded chunks via :class:`~imednet.utils.batch_coordinator.BatchCoordinator`,
and return a :class:`~imednet.utils.batch_coordinator.BatchSubmissionSummary`:

.. testcode::

   from typing import Any, cast
   from imednet import AsyncImednetSDK, ImednetSDK
   from imednet.simulation import reset_default_simulation_state
   from imednet_workflows.record_update import RecordUpdateWorkflow

   sdk = ImednetSDK(simulation_mode=True)
   workflow = RecordUpdateWorkflow(cast(Any, sdk), batch_size=100)

   summary = workflow.create_or_update_records_batch(
       study_key="STUDY",
       records_data=[
           {
               "formKey": "F_VS",
               "siteName": "SITE",
               "subjectKey": "SUBJ-001",
               "intervalName": "Baseline",
               "data": {"V_WEIGHT": 71.5},
           },
           {
               "formKey": "F_VS",
               "siteName": "SITE",
               "subjectKey": "SUBJ-001",
               "intervalName": "Baseline",
               "data": {"V_WEIGHT": 72.0},
           },
       ],
       wait_for_completion=True,
       poll_interval=1,
       chunk_size=1,
   )
   assert summary.chunk_count == 2
   assert summary.all_successful is True

   async def run_async_batch() -> None:
       async with AsyncImednetSDK(simulation_mode=True) as async_sdk:
           async_workflow = RecordUpdateWorkflow(cast(Any, async_sdk))
           await async_workflow.async_create_or_update_records_batch(
               "STUDY",
               [
                   {
                       "formKey": "F_VS",
                       "siteName": "SITE",
                       "subjectKey": "SUBJ-001",
                       "intervalName": "Baseline",
                       "data": {"V_WEIGHT": 73.0},
                   }
               ],
               wait_for_completion=True,
               poll_interval=1,
           )

   reset_default_simulation_state()

Single-Record Convenience Helpers
---------------------------------

For targeted operational scripts, ``RecordUpdateWorkflow`` also provides typed
builders that construct properly aliased ``Record`` dictionaries and return the
submitted ``Job``:

* :meth:`~imednet_workflows.record_update.RecordUpdateWorkflow.register_subject`:
  Submits an enrollment ``Form`` ``Record`` at an open ``Site``.
* :meth:`~imednet_workflows.record_update.RecordUpdateWorkflow.update_scheduled_record`:
  Updates a ``Form`` ``Record`` bound to a scheduled ``Interval`` for an enrolled
  ``Subject``.
* :meth:`~imednet_workflows.record_update.RecordUpdateWorkflow.create_new_record`:
  Creates an unscheduled ``Form`` ``Record`` for an enrolled ``Subject``.

.. testcode::

   from typing import Any, cast
   from imednet import ImednetSDK
   from imednet.simulation import reset_default_simulation_state
   from imednet_workflows.record_update import RecordUpdateWorkflow

   helper_sdk = ImednetSDK(simulation_mode=True)
   helper_workflow = RecordUpdateWorkflow(cast(Any, helper_sdk))

   reg_job = helper_workflow.register_subject(
       study_key="STUDY",
       form_identifier="F_DM",
       site_identifier="SITE",
       data={"V_AGE": 38, "V_GENDER": "F"},
       wait_for_completion=True,
       poll_interval=1,
   )
   sched_job = helper_workflow.update_scheduled_record(
       study_key="STUDY",
       form_identifier="F_VS",
       subject_identifier="SUBJ-001",
       interval_identifier="Baseline",
       data={"V_WEIGHT": 69.5},
       wait_for_completion=True,
       poll_interval=1,
   )
   unsched_job = helper_workflow.create_new_record(
       study_key="STUDY",
       form_identifier="F_VS",
       subject_identifier="SUBJ-001",
       data={"V_WEIGHT": 70.0},
       wait_for_completion=True,
       poll_interval=1,
   )
   print(reg_job.batch_id, sched_job.batch_id, unsched_job.batch_id)
   reset_default_simulation_state()
