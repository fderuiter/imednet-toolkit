Contributing
============

This guide summarizes how to set up your environment, validate changes, and submit
pull requests. Please review our `Code of Conduct <../CODE_OF_CONDUCT.md>`__ and
see `CONTRIBUTING.md <../CONTRIBUTING.md>`__ for complete details.

.. toctree::
   :maxdepth: 1
   :caption: Contributor Guides

   issue_management
   project_standards
   triage_playbook

Developer Environment Setup
---------------------------

We recommend using our interactive setup wizard to configure your environment, credentials, and dependencies:

.. code-block:: bash

   bash scripts/wizards/setup-imednet-env.sh

The wizard configures UTF-8 encoding (preventing Windows console errors), validates your ``uv`` installation, synchronizes all 5 packages in editable mode (``uv sync --extra dev --extra docs``), and configures ``.env`` with EDC credentials and safety flags.

For manual setup:

.. code-block:: bash

   # Configure UTF-8 encoding
   export PYTHONUTF8=1   # in PowerShell: $env:PYTHONUTF8 = "1"

   # Synchronize workspace dependencies
   uv sync --extra dev --extra docs

Public API stability
--------------------

The following table describes the stability contract for each sub-package.  Only
symbols that appear in a module's ``__all__`` list are considered part of the public
API.

.. list-table::
   :header-rows: 1
   :widths: 35 15 50

   * - Package
     - Stability
     - Notes
   * - ``imednet`` (top-level)
     - **Stable**
     - All exports in ``__all__`` follow semantic versioning. Removals require a major version bump and a ``DeprecationWarning`` for at least one minor release.
   * - ``imednet.models``
     - **Stable**
     - Pydantic v2 model schemas. Field additions are non-breaking; field removals or type changes are breaking.
   * - ``imednet.errors``
     - **Stable**
     - Exception hierarchy. New sub-classes are non-breaking.
   * - ``imednet.endpoints``
     - **Stable**
     - Typed resource endpoint classes. Method signatures follow ``FilterValue``/``ItemId`` contracts.
   * - ``imednet.utils``
     - **Stable** (exported symbols only)
     - ``JsonDict``, ``ItemId``, ``FilterValue``, ``FilterScalar``, and utility functions in ``__all__``.
   * - ``imednet.auth``
     - **Stable**
     - Authentication strategy classes.
   * - ``imednet.validation``
     - **Stable**
     - Schema cache and validation helpers.
   * - ``imednet.pagination``
     - **Semi-stable**
     - Paginator re-exports from ``imednet.core.paginator``.  Prefer using endpoint list methods rather than paginators directly.
   * - ``imednet.core``
     - **Internal**
     - Implementation details. May change without notice. Prefer stable public packages.
   * - ``imednet.core.http``
     - **Internal**
     - HTTP execution internals. No stability guarantees.
   * - ``imednet.testing``
     - **Unstable**
     - Test-support utilities. API may change between minor releases.
   * - ``imednet.cli``
     - **Stable** (CLI commands)
     - CLI command-line interface is stable; the Python module internals are not.
   * - ``imednet.integrations``
     - **Semi-stable**
     - Export helpers. Functions in ``__all__`` are stable; internals may change.

Deprecation policy
~~~~~~~~~~~~~~~~~~

Symbols removed from the public API follow this process:

1. Issue a ``DeprecationWarning`` via :func:`warnings.warn` with ``stacklevel=2`` for
   at least one minor release before removal.
2. Document the migration path in ``CHANGELOG.md`` under a **Deprecated** heading.
3. Remove the symbol in the next major version bump only.

Internal modules
~~~~~~~~~~~~~~~~

Modules not listed as *Stable* above should **not** be imported directly in
application code.  Import from the stable namespaces (e.g. ``imednet``,
``imednet.models``, ``imednet.endpoints``) instead.

Type aliases
~~~~~~~~~~~~

The following type aliases are exported from ``imednet`` and ``imednet.utils`` for use
in downstream code:

- ``JsonDict`` – ``Dict[str, Any]``: a generic JSON object.
- ``ItemId`` – ``str | int``: an endpoint item identifier.
- ``FilterScalar`` – ``str | int | float | bool | None``: a single filter value.
- ``FilterValue`` – union of ``FilterScalar``, operator tuples, and lists: the full
  filter value accepted by ``list()`` endpoint methods.

Issue reporting and triage
--------------------------
The project uses a documented issue operating model so epics, work items, and
maintainer triage follow the same rules across the repository.

- Use ``<type>(<area>): <concise outcome>`` issue titles.
- Capture acceptance criteria, test impact, docs impact, and compatibility notes.
- Apply the label taxonomy and lifecycle in ``issue_management``.
- Follow the intake and rewrite workflow in ``triage_playbook``.

HTTP transport mocking
----------------------
Use ``respx`` for any test that exercises ``Client`` or ``AsyncClient`` HTTP behavior.
Do not patch ``Client._client.request``, ``AsyncClient._client.request``, or executor
``send`` callables just to intercept outbound ``httpx`` traffic; mock at the transport
layer instead.

When using ``respx``, prefer strict routers so tests cannot leak live requests and stale
routes fail fast:

.. testcode::

   import httpx
   import respx

   @respx.mock(assert_all_called=True, assert_all_mocked=True)
   def test_retry_and_query_params() -> None:
       calls = {"count": 0}

       def handler(request: httpx.Request) -> httpx.Response:
           calls["count"] += 1
           assert request.url.params["page"] == "2"
           if calls["count"] == 1:
               raise httpx.RequestError("boom", request=request)
           return httpx.Response(200, json={"items": []})

       route = respx.route(
           method="GET",
           url__regex=r"https://api\.test/items(?:\?.*)?$",
       )
       route.mock(side_effect=handler)

This keeps production clients free of test-only wrappers while still validating request
construction, retry behavior, dynamic URLs, and query parameters.
