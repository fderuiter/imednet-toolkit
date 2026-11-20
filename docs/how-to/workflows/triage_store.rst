Triage Store Guide
==================

:class:`~imednet_workflows.triage_store.TriageStore` provides a thread-safe,
SQLite-backed local review queue for clinical ``Record`` and ``Query`` triage
workflows with validated :class:`~imednet.models.triage.TriageStatus`
transitions (``NEW`` ↔ ``UNDER_REVIEW`` ↔ ``RESOLVED``), reviewer assignments,
annotations, and immutable transition history.

Managing a Local Triage Queue
-----------------------------

* :meth:`~imednet_workflows.triage_store.TriageStore.upsert_item` inserts or
  updates a :class:`~imednet.models.triage.TriageItem` (enforcing valid status
  transitions when updating an existing item).
* :meth:`~imednet_workflows.triage_store.TriageStore.assign_item` assigns a
  queued item to a reviewer ``user_id``.
* :meth:`~imednet_workflows.triage_store.TriageStore.add_annotation` appends a
  timestamped :class:`~imednet.models.triage.TriageAnnotation`.
* :meth:`~imednet_workflows.triage_store.TriageStore.update_status` validates the
  target ``TriageStatus`` transition and records a
  :class:`~imednet.models.triage.TriageHistoryEntry`.
* :meth:`~imednet_workflows.triage_store.TriageStore.get_queue` and
  :meth:`~imednet_workflows.triage_store.TriageStore.get_triage_item` hydrate
  items alongside ordered ``annotations`` and ``history``.

.. testcode::

   import tempfile
   from pathlib import Path
   from imednet.models.triage import TriageItem, TriageStatus
   from imednet_workflows import TriageStore

   with tempfile.TemporaryDirectory() as tmp_dir:
       store = TriageStore(Path(tmp_dir) / "study_triage.sqlite3")
       store.upsert_item(
           TriageItem(
               item_id="REC-101",
               study_key="STUDY",
               status=TriageStatus.NEW,
               severity="HIGH",
           )
       )
       store.assign_item("REC-101", "cdm_reviewer_1")
       store.add_annotation("REC-101", "cdm_reviewer_1", "Reviewing Vital Signs Query")
       store.update_status(
           "REC-101",
           TriageStatus.UNDER_REVIEW,
           user_id="cdm_reviewer_1",
           comment="Opened investigation with Site",
       )
       store.update_status(
           "REC-101",
           TriageStatus.RESOLVED,
           user_id="cdm_reviewer_1",
           comment="Verified updated Record value",
       )

       resolved_queue = store.get_queue("STUDY", status=TriageStatus.RESOLVED)
       assert len(resolved_queue) == 1
       assert len(resolved_queue[0].annotations) == 1
       assert len(resolved_queue[0].history) == 2
       print(resolved_queue[0].item_id, resolved_queue[0].status.value)
