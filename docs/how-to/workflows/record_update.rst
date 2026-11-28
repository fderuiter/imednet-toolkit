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
(or :meth:`~imednet_workflows.record_update.AsyncRecordUpdateWorkflow.create_or_update_records_batch`
on ``AsyncImednetSDK``) to validate a sequence of typed
:class:`~imednet.models.records.BaseRecordRequest` models (such as
:class:`~imednet.models.records.RegisterSubjectRequest`,
:class:`~imednet.models.records.CreateNewRecordRequest`,
:class:`~imednet.models.records.UpdateScheduledRecordRequest`, and
:class:`~imednet.models.records.UpdateRecordRequest`), ``Record`` dictionaries,
a ``DataFrame``, or a CSV path once against the cached ``Study`` schema, split
them into bounded chunks via :class:`~imednet.utils.batch_coordinator.BatchCoordinator`,
and return a :class:`~imednet.utils.batch_coordinator.BatchSubmissionSummary`.

Every :class:`~imednet.models.records.BaseRecordRequest` subclass accepts both
``snake_case`` and ``camelCase`` keyword arguments for all 14 iMednet
``POST /records`` write attributes (``formKey``, ``formId``, ``siteName``,
``siteId``, ``subjectKey``, ``subjectId``, ``subjectOid``, ``intervalName``,
``intervalId``, ``visitId``, ``recordId``, ``recordOid``, ``recordStatus``, and
``reasonForChange``) plus ``data``, rejects unknown keys (``extra="forbid"``),
and omits ``None`` defaults when serialized:

.. testcode::

   import asyncio
   from imednet import AsyncImednetSDK, ImednetSDK
   from imednet.models.records import (
       CreateNewRecordRequest,
       RecordStatus,
       UpdateRecordRequest,
       UpdateScheduledRecordRequest,
   )
   from imednet.simulation import reset_default_simulation_state

   sdk = ImednetSDK(simulation_mode=True)
   workflow = sdk.workflows.record_update

   summary = workflow.create_or_update_records_batch(
       study_key="STUDY",
       records_data=[
           UpdateScheduledRecordRequest(
               form_key="F_VS",
               site_name="SITE",
               subject_key="SUB1",
               interval_name="Baseline",
               data={"V_WEIGHT": 71.5},
           ),
           UpdateRecordRequest(
               record_id=602,
               visit_id=401,
               form_id=302,
               record_status=RecordStatus.COMPLETE,
               reason_for_change="Confirmed vital signs",
               data={"V_WEIGHT": 72.0},
           ),
       ],
       wait_for_completion=True,
       poll_interval=1,
       chunk_size=1,
   )
   assert summary.chunk_count == 2
   assert summary.all_successful is True

   async def run_async_batch() -> None:
       async with AsyncImednetSDK(simulation_mode=True) as async_sdk:
           async_summary = await async_sdk.workflows.record_update.create_or_update_records_batch(
               "STUDY",
               (
                   CreateNewRecordRequest(
                       formKey="F_VS",
                       subjectKey="SUB1",
                       data={"V_WEIGHT": 73.0},
                   ),
               ),
               wait_for_completion=True,
               poll_interval=1,
           )
           assert async_summary.all_successful is True

   asyncio.run(run_async_batch())
   reset_default_simulation_state()

Single-Record Convenience Helpers
---------------------------------

:meth:`~imednet_workflows.record_update.RecordUpdateWorkflow.create_or_update_records`
(and ``async_create_or_update_records``) enforces a strict single-``Job`` contract:
passing multi-chunk payloads, ``dry_run=True``, empty ``Record`` lists, or invalid
``DataDictionary`` inputs raises :class:`~imednet.errors.ValidationError` instead of
truncating outcomes or fabricating synthetic ``COMPLETED`` states. Use
:meth:`~imednet_workflows.record_update.RecordUpdateWorkflow.create_or_update_records_batch`
for multi-chunk writes or dry-run validation reports.

For targeted operational scripts, ``RecordUpdateWorkflow`` also provides typed
builders that construct properly aliased ``Record`` dictionaries and return the
submitted ``Job`` (along with ``async_*`` counterparts for ``AsyncImednetSDK``):

* :meth:`~imednet_workflows.record_update.RecordUpdateWorkflow.register_subject` /
  :meth:`~imednet_workflows.record_update.RecordUpdateWorkflow.async_register_subject`:
  Submits an enrollment ``Form`` ``Record`` at an open ``Site``.
* :meth:`~imednet_workflows.record_update.RecordUpdateWorkflow.update_scheduled_record` /
  :meth:`~imednet_workflows.record_update.RecordUpdateWorkflow.async_update_scheduled_record`:
  Updates a ``Form`` ``Record`` bound to a scheduled ``Interval`` for an enrolled
  ``Subject``.
* :meth:`~imednet_workflows.record_update.RecordUpdateWorkflow.create_new_record` /
  :meth:`~imednet_workflows.record_update.RecordUpdateWorkflow.async_create_new_record`:
  Creates an unscheduled ``Form`` ``Record`` for an enrolled ``Subject``.

.. testcode::

   import asyncio
   from imednet import AsyncImednetSDK, ImednetSDK
   from imednet.simulation import reset_default_simulation_state

   helper_sdk = ImednetSDK(simulation_mode=True)
   helper_workflow = helper_sdk.workflows.record_update

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
       subject_identifier="SUB1",
       interval_identifier="Baseline",
       data={"V_WEIGHT": 69.5},
       wait_for_completion=True,
       poll_interval=1,
   )
   unsched_job = helper_workflow.create_new_record(
       study_key="STUDY",
       form_identifier="F_VS",
       subject_identifier="SUB1",
       data={"V_WEIGHT": 70.0},
       wait_for_completion=True,
       poll_interval=1,
   )
   assert reg_job.batch_id and sched_job.batch_id and unsched_job.batch_id

   async def run_async_helpers() -> None:
       async with AsyncImednetSDK(simulation_mode=True) as async_sdk:
           async_reg_job = await async_sdk.workflows.record_update.async_register_subject(
               study_key="STUDY",
               form_identifier="F_DM",
               site_identifier="SITE",
               data={"V_AGE": 41, "V_GENDER": "M"},
               wait_for_completion=True,
               poll_interval=1,
           )
           assert async_reg_job.batch_id

   asyncio.run(run_async_helpers())
   print(reg_job.batch_id, sched_job.batch_id, unsched_job.batch_id)
   reset_default_simulation_state()
