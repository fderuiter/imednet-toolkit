Subject Data Workflow
=====================

The :class:`~imednet_workflows.subject_data.SubjectDataWorkflow` plugin
(registered under ``imednet.workflows`` as ``subject_data`` and accessible via
``sdk.workflows.subject_data``) retrieves a complete clinical dossier for a
single ``Subject`` in one call.

Assembling a ``SubjectComprehensiveData`` Dossier
-------------------------------------------------

:meth:`~imednet_workflows.subject_data.SubjectDataWorkflow.get_all_subject_data`
queries ``SubjectsEndpoint``, ``VisitsEndpoint``, ``RecordsEndpoint``, and
``QueriesEndpoint`` for ``subject_key`` and returns a typed
:class:`~imednet_workflows.subject_data.SubjectComprehensiveData` Pydantic model
with four fields:

* ``subject_details`` (:class:`~imednet.models.subjects.Subject` | ``None``):
  Enrollment status, ``Site`` assignment, and ``Subject`` metadata.
* ``visits`` (``list[Visit]``): Scheduled and unscheduled ``Visit`` instances.
* ``records`` (``list[Record]``): ``Form`` ``Record`` payloads (excluding voided
  ``deleted=True`` items by default unless ``include_deleted=True`` is passed).
* ``queries`` (``list[Query]``): Open and historical ``Query`` items linked to the
  ``Subject``.

.. testcode::

   from typing import Any, cast
   from imednet import ImednetSDK
   from imednet_workflows.subject_data import SubjectDataWorkflow

   sdk = ImednetSDK(simulation_mode=True)
   workflow = SubjectDataWorkflow(cast(Any, sdk))

   dossier = workflow.get_all_subject_data(
       study_key="STUDY",
       subject_key="SUB1",
       include_deleted=False,
   )
   assert dossier.subject_details is not None
   assert dossier.subject_details.subject_key == "SUB1"
