Study Schema Inspector
======================

The :class:`~imednet_workflows.uat.StudySchemaInspector` plugin (registered under
``imednet.workflows`` as ``uat_inspector`` and accessible via
``sdk.workflows.uat_inspector``) fetches and indexes ``Form``, ``Variable``,
``Interval``, and ``Site`` metadata into an immutable-style
:class:`~imednet_workflows.uat.StudySnapshot`.

Capturing a ``StudySnapshot``
-----------------------------

``StudySchemaInspector`` supports both synchronous (``inspect``) and concurrent
asynchronous (``async_inspect`` via ``asyncio.gather``) execution, caching
snapshots in memory per ``study_key`` until ``force_refresh=True`` or
``clear_cache()`` is called:

* ``snapshot.enrollment_forms()``: ``Form`` definitions with ``form_type`` in
  ``("Enrollment", "Registration")``.
* ``snapshot.scheduled_forms()``: Visit-bound ``Form`` definitions excluding
  enrollment and unscheduled forms.
* ``snapshot.unscheduled_forms()``: Episodic ``Form`` definitions where
  ``unscheduled_visit`` is ``True``.
* ``snapshot.active_sites()``: ``Site`` entries eligible for ``Subject`` enrollment.
* ``snapshot.forms_by_key``, ``snapshot.variables_by_form``, and
  ``snapshot.intervals_by_name``: Pre-indexed lookup dictionaries.

.. testcode::

   from imednet import AsyncImednetSDK, ImednetSDK
   from imednet_workflows.uat import StudySchemaInspector

   sdk = ImednetSDK(simulation_mode=True)
   inspector = StudySchemaInspector(sdk)

   snapshot = inspector.inspect("STUDY")
   print(
       len(snapshot.enrollment_forms()),
       len(snapshot.scheduled_forms()),
       len(snapshot.unscheduled_forms()),
       len(snapshot.active_sites()),
   )
   inspector.clear_cache("STUDY")

   async def inspect_concurrently() -> None:
       async with AsyncImednetSDK(simulation_mode=True) as async_sdk:
           async_inspector = StudySchemaInspector(async_sdk)
           snap = await async_inspector.async_inspect("STUDY", force_refresh=True)
           print(list(snap.forms_by_key))

Pairing with ``UATWorkflow``
----------------------------

``StudySchemaInspector`` powers the inspection stage of
:class:`~imednet_workflows.uat.UATWorkflow` (``sdk.workflows.uat``); see
:doc:`uat_workflow` for end-to-end synthetic ``Record`` generation and submission.
