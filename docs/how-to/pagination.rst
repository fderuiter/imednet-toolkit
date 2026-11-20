Pagination guarantees
=====================

The SDK pagination helpers live in :mod:`imednet.pagination` and are used by list endpoints.

Cursor model
------------

- The :class:`~imednet.pagination.Paginator` uses a 0-based page cursor sent as the ``page`` query parameter.
- ``size`` is the requested page size.
- :attr:`Paginator.page_size <imednet.pagination.Paginator.page_size>` exposes the active page size (default: ``100``).
- :attr:`Paginator.cursor <imednet.pagination.Paginator.cursor>` is ``None`` before iteration starts, then exposes the next
  page cursor while iterating, and returns to ``None`` once exhausted.
- The SDK does not enforce a maximum page size value; API-side limits still apply.

Iteration and memory behavior
-----------------------------

- Iteration is lazy: pages are fetched only when the iterator is advanced.
- Iteration is bounded-memory: the paginator only keeps the current page payload in memory and does not buffer all pages.

Error handling
--------------

- If pagination metadata is malformed or inconsistent (for example a missing/invalid
  ``totalPages`` cursor when another page is implied), the paginator raises
  :class:`imednet.errors.PaginationError`.
