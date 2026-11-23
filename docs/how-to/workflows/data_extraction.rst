Data Extraction Workflow
========================

The :class:`~imednet_workflows.data_extraction.DataExtractionWorkflow` plugin
(registered under ``imednet.workflows`` as ``data_extraction`` and accessible via
``sdk.workflows.data_extraction``) combines filtering across ``Subject``, ``Visit``,
``Record``, and ``RecordRevision`` endpoints into single declarative calls.

Extracting Records Across Subject, Visit, and Record Criteria
-------------------------------------------------------------

:meth:`~imednet_workflows.data_extraction.DataExtractionWorkflow.extract_records_by_criteria`
evaluates ``subject_filter`` against ``SubjectsEndpoint``, ``visit_filter`` against
``VisitsEndpoint``, and ``record_filter`` against ``RecordsEndpoint``, intersecting
matched ``subject_key`` and ``visit_id`` sets automatically. Voided
(``deleted=True``) ``Record`` items are excluded by default unless
``include_deleted=True`` is supplied.

.. testcode::

   from typing import Any, cast
   from imednet import ImednetSDK
   from imednet_workflows.data_extraction import DataExtractionWorkflow

   sdk = ImednetSDK(simulation_mode=True)
   workflow = DataExtractionWorkflow(cast(Any, sdk))

   progress_messages: list[str] = []
   records = workflow.extract_records_by_criteria(
       study_key="STUDY",
       record_filter={"form_key": "F_VS"},
       subject_filter={"site_name": "SITE"},
       visit_filter={"interval_name": "Baseline"},
       on_progress=progress_messages.append,
       include_deleted=False,
   )
   print(len(records), len(progress_messages))

Extracting 21 CFR Part 11 Audit Trails
--------------------------------------

Use :meth:`~imednet_workflows.data_extraction.DataExtractionWorkflow.extract_audit_trail`
to fetch :class:`~imednet.models.record_revisions.RecordRevision` entries bounded
by UTC ``start_date`` and ``end_date`` timestamps alongside optional ``User`` or
``Form`` filters. ``extract_audit_trail`` sends a single ``dateCreated``
predicate to the iMednet REST API (preserving unique AND-attribute rules) and
evaluates additional UTC bounds—including half-open ``< next_day_00:00:00Z``
bounds for date-only ``end_date`` values so sub-second revisions are retained—in
Python:

.. testcode::

   from typing import Any, cast
   from imednet import ImednetSDK
   from imednet.models.record_revisions import RecordRevision
   from imednet_workflows.data_extraction import DataExtractionWorkflow

   audit_sdk = ImednetSDK(simulation_mode=True)
   audit_workflow = DataExtractionWorkflow(cast(Any, audit_sdk))

   revisions = audit_workflow.extract_audit_trail(
       study_key="STUDY",
       start_date="2025-01-01",
       end_date="2026-12-31",
       user_filter={"form_key": "F_VS"},
   )
   RecordRevision.validate_chain(revisions, allow_partial=True)
   print(len(revisions))

Combining Extraction with ``RecordMapper``
------------------------------------------

To project extracted ``Record`` items into analytical DataFrames or CDISC SDTM
columns, combine ``sdk.workflows.data_extraction`` with
:class:`~imednet_workflows.record_mapper.RecordMapper` (``sdk.workflows.record_mapper``);
see :doc:`/how-to/record_mapping` for column-mapping details.
