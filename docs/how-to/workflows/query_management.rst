Query Management Workflow
=========================

The :class:`~imednet_workflows.query_management.QueryManagementWorkflow` plugin
(registered under ``imednet.workflows`` as ``query_management`` and accessible via
``sdk.workflows.query_management``) streamlines ``Query`` monitoring across a
``Study``, ``Site``, or ``Subject``.

Fetching Open Queries and State Counts
--------------------------------------

* :meth:`~imednet_workflows.query_management.QueryManagementWorkflow.get_open_queries`
  returns active ``Query`` items whose normalized ``QueryStatus`` is ``OPEN``
  (including legacy payload evaluation from ``query_comments`` when explicit
  status fields are absent).
* :meth:`~imednet_workflows.query_management.QueryManagementWorkflow.get_query_state_counts`
  aggregates ``Query`` totals into a :class:`~imednet_workflows.query_management.QueryStateCounts`
  mapping supporting case-insensitive string keys (``"open"``, ``"OPEN"``) and
  :class:`~imednet.models.queries.QueryStatus` enum members.

.. testcode::

   from typing import Any, cast
   from imednet import ImednetSDK
   from imednet.models.queries import QueryStatus
   from imednet_workflows.query_management import QueryManagementWorkflow

   sdk = ImednetSDK(simulation_mode=True)
   workflow = QueryManagementWorkflow(cast(Any, sdk))

   open_queries = workflow.get_open_queries("STUDY")
   counts = workflow.get_query_state_counts("STUDY")
   assert len(open_queries) == 2
   assert counts["open"] == counts[QueryStatus.OPEN] == 2
   print(len(open_queries), counts["OPEN"], counts["CLOSED"])

Filtering Queries by Subject or Site
------------------------------------

Use :meth:`~imednet_workflows.query_management.QueryManagementWorkflow.get_queries_for_subject`
to inspect all ``Query`` items attached to a single ``Subject``, or
:meth:`~imednet_workflows.query_management.QueryManagementWorkflow.get_queries_by_site`
to resolve enrolled ``Subject`` keys at a ``Site`` and retrieve every associated
``Query``:

.. testcode::

   from typing import Any, cast
   from imednet import ImednetSDK
   from imednet_workflows.query_management import QueryManagementWorkflow

   scoped_sdk = ImednetSDK(simulation_mode=True)
   scoped_workflow = QueryManagementWorkflow(cast(Any, scoped_sdk))

   subject_queries = scoped_workflow.get_queries_for_subject("STUDY", "SUB1")
   site_queries = scoped_workflow.get_queries_by_site("STUDY", "SITE")
   assert len(subject_queries) == 2
   assert len(site_queries) == 2
   print(len(subject_queries), len(site_queries))

For persistent local review queues, assignment tracking, and transition history,
pair ``QueryManagementWorkflow`` with :doc:`triage_store`.
