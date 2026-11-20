Domain Model Lifecycles
=======================

Core clinical models in :mod:`imednet.models` enforce validated status
transitions, soft-delete invariants, and enrollment eligibility checks so local
state mutations cannot violate EDC domain rules.

Record Lifecycle and Soft-Delete Guards
---------------------------------------

:class:`~imednet.models.records.Record` normalizes known status tokens into
:class:`~imednet.models.records.RecordStatus` while preserving custom ``Study``
status strings:

* ``record.can_transition_to(target_status)`` checks whether moving to
  ``target_status`` is permitted without mutating the instance.
* ``record.transition_to(target_status)`` validates and applies the transition,
  automatically setting ``record.deleted = True`` when transitioning to
  ``RecordStatus.DELETED``.
* ``record.is_deleted`` and ``record.effective_status`` resolve ``RecordStatus.DELETED``
  whenever ``deleted=True``. Once soft-deleted, ``RecordStatus.DELETED`` is
  terminal—attempting to clear ``deleted`` or assign an active status raises
  ``ValueError``.

.. testcode::

   from imednet.models.records import Record, RecordStatus

   record = Record.model_validate(
       {
           "recordId": 101,
           "studyKey": "STUDY",
           "formKey": "F_VS",
           "recordStatus": "Record Incomplete",
       }
   )
   assert record.can_transition_to(RecordStatus.VERIFIED) is True
   record.transition_to(RecordStatus.VERIFIED)
   verified_status = record.effective_status
   assert verified_status == RecordStatus.VERIFIED

   record.transition_to(RecordStatus.DELETED)
   deleted_status = record.effective_status
   assert record.is_deleted is True
   assert deleted_status == RecordStatus.DELETED
   assert record.can_transition_to(RecordStatus.COMPLETE) is False

Subject Lifecycle Transitions
-----------------------------

:class:`~imednet.models.subjects.Subject` provides matching lifecycle helpers
backed by :class:`~imednet.models.subjects.SubjectStatus` (``SCREENED``,
``REGISTERED``, ``BASELINE``, ``ENROLLED``, ``ACTIVE``, ``COMPLETED``,
``WITHDRAWN``, ``SCREEN_FAILED``, ``CLOSED``, ``INACTIVE``, and terminal
``DELETED``):

.. testcode::

   from imednet.models.subjects import Subject, SubjectStatus

   subject = Subject.model_validate(
       {
           "subjectId": 1,
           "studyKey": "STUDY",
           "subjectKey": "SUB1",
           "subjectStatus": "Screened",
       }
   )
   assert subject.can_transition_to(SubjectStatus.ENROLLED) is True
   subject.transition_to(SubjectStatus.ENROLLED)
   enrolled_status = subject.effective_status
   assert enrolled_status == SubjectStatus.ENROLLED

   subject.transition_to(SubjectStatus.DELETED)
   deleted_subj_status = subject.effective_status
   assert subject.is_deleted is True
   assert deleted_subj_status == SubjectStatus.DELETED
   assert subject.can_transition_to(SubjectStatus.ACTIVE) is False

Site Enrollment Eligibility
---------------------------

:class:`~imednet.models.sites.Site` normalizes ``site_enrollment_status`` via
:class:`~imednet.models.sites.SiteEnrollmentStatus` and exposes
``site.is_enrollment_open`` (aliased as ``site.is_eligible``) so enrollment
workflows can reject closed or held ``Site`` targets before submitting ``Subject``
registration ``Record`` batches:

.. testcode::

   from imednet.models.sites import Site

   open_site = Site.model_validate(
       {
           "siteId": 10,
           "studyKey": "STUDY",
           "siteName": "SITE",
           "siteEnrollmentStatus": "Enrollment Open",
       }
   )
   closed_site = Site.model_validate(
       {
           "siteId": 11,
           "studyKey": "STUDY",
           "siteName": "SITE-CLOSED",
           "siteEnrollmentStatus": "Enrollment Closed",
       }
   )
   assert open_site.is_enrollment_open is True
   assert closed_site.is_enrollment_open is False

Query State Machine
-------------------

:class:`~imednet.models.queries.Query` enforces directional transitions across
:class:`~imednet.models.queries.QueryStatus` (``OPEN`` → ``ANSWERED`` / ``CLOSED`` /
``CANCELLED``; ``ANSWERED`` → ``OPEN`` / ``CLOSED`` / ``CANCELLED``; ``CLOSED`` →
``OPEN``; ``CANCELLED`` is terminal) on both ``transition_to`` and direct
``query.status`` assignment:

.. testcode::

   from imednet.models.queries import Query, QueryStatus

   query = Query.model_validate(
       {
           "annotationId": 500,
           "studyKey": "STUDY",
           "subjectKey": "SUB1",
           "status": "OPEN",
       }
   )
   assert query.can_transition_to(QueryStatus.ANSWERED) is True
   query.transition_to(QueryStatus.ANSWERED)
   query.transition_to(QueryStatus.CLOSED)
   assert query.can_transition_to(QueryStatus.CANCELLED) is False
