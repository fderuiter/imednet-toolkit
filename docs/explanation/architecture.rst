Architecture Overview
=====================

The SDK is organized around a core HTTP client layer, endpoint wrappers that model
the iMednet API, workflow helpers that combine multiple endpoint calls, and a CLI
built on top of those pieces.

Components
----------

.. mermaid:: /diagrams/architecture_3.mmd

Core Client
-----------

The synchronous :class:`~imednet.core.client.Client` implements authentication,
retry handling, and JSON serialization for each API request. It inherits from
``HTTPClientBase`` and is shared by all endpoint classes. HTTP transport is
handled by ``httpx``.

HTTP Transport Contract
-----------------------

The transport layer in :mod:`imednet.core.http` provides the following guarantees:

Retry policy
~~~~~~~~~~~~

* Retries are evaluated by :class:`imednet.core.retry.DefaultRetryPolicy`.
* Idempotent methods (``GET``, ``PUT``, ``DELETE``, ``HEAD``, ``OPTIONS``) retry on:

  * ``httpx.RequestError`` network failures
  * HTTP ``5xx`` responses
* All methods (including ``POST``/``PATCH``) retry on HTTP ``429`` rate limits.
* Non-idempotent methods do not retry on network errors or ``5xx`` responses.
* Maximum attempts and backoff are configured via ``retry_config``
  (:class:`~imednet.core.retry.RetryConfig`) on
  :class:`~imednet.core.client.Client` and
  :class:`~imednet.core.async_client.AsyncClient`, or via ``retry_config``,
  ``retries``, and ``backoff_factor`` on :class:`~imednet.sdk.ImednetSDK` and
  :class:`~imednet.sdk.AsyncImednetSDK`.
* Backoff uses Tenacity's randomized exponential wait strategy with
  ``backoff_factor`` as the multiplier.
* Paginators call the requester directly. The HTTP transport is the sole
  retry, circuit-breaker accounting and request-telemetry owner for each page,
  including endpoints returning a raw JSON list. Custom requesters must own
  their own retry policy; paginators do not replay their exceptions.
* If a retriable response includes ``Retry-After``, that delay overrides the
  backoff schedule.

Timeouts
~~~~~~~~

* ``timeout`` is forwarded directly into ``httpx.Client`` / ``httpx.AsyncClient``.
* You can pass either a float (single timeout applied to all phases) or
  ``httpx.Timeout`` for per-phase timeouts.

Status-code error mapping
~~~~~~~~~~~~~~~~~~~~~~~~~

Response errors are mapped via :func:`imednet.errors.get_error_class`:

.. list-table::
   :header-rows: 1

   * - Status code
     - Raised exception
   * - 400
     - :class:`imednet.errors.BadRequestError`
   * - 401
     - :class:`imednet.errors.UnauthorizedError`
   * - 403
     - :class:`imednet.errors.ForbiddenError`
   * - 404
     - :class:`imednet.errors.NotFoundError`
   * - 409
     - :class:`imednet.errors.ConflictError`
   * - 429
     - :class:`imednet.errors.RateLimitError`
   * - 500-599
     - :class:`imednet.errors.ServerError`
   * - other
     - :class:`imednet.errors.ApiError`

Credential redaction
~~~~~~~~~~~~~~~~~~~~

* Transport logs never include raw credential headers.
* URLs logged by request monitoring are sanitized through
  :func:`imednet.utils.url.redact_url_query`, redacting sensitive query params
  such as ``api_key``, ``security_key``, ``token``, ``secret``, and ``password``.
* Retry exhaustion raises :class:`imednet.errors.RequestError` with a generic
  message that does not expose credentials.

Async Client
------------

:class:`~imednet.core.async_client.AsyncClient` provides the same features as the
sync client but leverages ``async``/``await`` for concurrency. The public SDK
surface is split into two client classes:

* :class:`~imednet.sdk.ImednetSDK` for synchronous usage only.
* :class:`~imednet.sdk.AsyncImednetSDK` for asynchronous usage only.

Migration note: if you previously used async behavior from ``ImednetSDK``, move
to :class:`~imednet.sdk.AsyncImednetSDK` and manage lifecycle with
``async with`` or ``await sdk.aclose()``.

Endpoints
---------

Each endpoint, such as :class:`~imednet.endpoints.studies.StudiesEndpoint`,
wraps a related set of API operations. Endpoints now use **composition**:
`GenericListGetEndpoint` composes operation classes (for example
``ListOperation`` and ``FilterGetOperation``) rather than inheriting deep
operation mixin chains. This keeps method resolution straightforward, improves
IDE autocomplete, and avoids MRO-related typing ambiguity. Endpoints can cache
responses when called without filters and expose ``list``/``get`` methods that
return typed models.

Workflows
---------

Workflows orchestrate several endpoints to perform higher level tasks. For
example, :class:`~imednet_workflows.record_update.RecordUpdateWorkflow` validates
record payloads, submits them, and polls resulting jobs. Workflows have sync and
async variants and are available under ``sdk.workflows``.

Caching
-------

:doc:`/how-to/caching` describes how endpoint and schema data are cached. Cached values
can be refreshed by passing ``refresh=True`` to endpoint methods or calling
``schema.refresh()`` on a validator.

CLI
---

The :doc:`/how-to/cli` uses standard-library ``argparse`` to expose common workflows on the command line. Each
command creates an :class:`~imednet.sdk.ImednetSDK` instance, invokes a workflow,
and closes the SDK when finished.

Data Flow
---------

.. mermaid:: /diagrams/architecture_4.mmd

Extension Points
----------------

.. mermaid:: /diagrams/architecture_5.mmd

Adding New Endpoints
--------------------

* Subclass :class:`~imednet.core.endpoint.base.GenericEndpoint` or
  :class:`~imednet.core.endpoint.base.GenericListGetEndpoint`.
* Register the class in ``_ENDPOINT_REGISTRY`` within
  :mod:`imednet.sdk` so ``ImednetSDK`` exposes it.
* Document the endpoint in ``docs/endpoints/`` and add tests.

Adding New Workflows
--------------------

* Create a workflow under ``packages/plugins-workflows/src/imednet_workflows`` and provide sync and async
  methods where appropriate.
* Register the workflow factory in the distribution's ``imednet.workflows`` entry points.
* Add CLI commands or examples that demonstrate the workflow.
* Update documentation and tests.

Export Sink Architecture
------------------------

The SDK supports four distinct export paths. Choose the right path based on
the shape of data you want to land at the destination.

Export Path Decision Matrix
~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 16 34 50

   * - Path
     - When to use
     - Key components
   * - **Tabular**
     - Destination is a flat/columnar store (CSV, Excel, SQL, DuckDB,
       Parquet).  All variables for a form can be represented as columns.
     - :class:`~imednet_workflows.record_mapper.RecordMapper` →
       ``pandas.DataFrame`` → sink function in
       :mod:`imednet.integrations.export`
   * - **Document**
     - Destination is a document store (for example MongoDB) and should keep
       nested ``record_data`` payloads with metadata envelope fields.
     - :class:`~imednet_workflows.data_extraction.DataExtractionWorkflow`
       → typed :class:`~imednet.models.Record` list →
       :class:`~imednet_sinks.document.MongoDbExportSink`
   * - **Graph**
     - Destination models relationships natively (for example Neo4j) and the
       hierarchy *Study → Subject → Visit → Record* should be preserved.
     - :class:`~imednet_workflows.data_extraction.DataExtractionWorkflow`
       → typed :class:`~imednet.models.Record` list →
       :class:`~imednet_sinks.graph.Neo4jExportSink`
   * - **Warehouse**
     - Destination is a cloud data warehouse with a native bulk loader.
       Records are staged as Parquet files and then COPY'd in a single
       command for best throughput.
     - :class:`~imednet_workflows.record_mapper.RecordMapper` →
       Parquet staging →
       :class:`~imednet_sinks.warehouse.SnowflakeExportSink`

Data flow diagram
~~~~~~~~~~~~~~~~~

.. mermaid:: /diagrams/architecture_6.mmd

Shared ``ExportSink`` contract
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

All non-tabular sinks subclass
``imednet.spi.sinks.ExportSink`` and must implement
three methods:

``write_batch(records, *, batch_id) -> int``
    Write one batch to the destination.  The ``batch_id`` is a
    caller-supplied idempotency key in the format
    ``"<study_key>/<form_key>/<batch_n>"``.  Returns the number of
    records written.

``flush() -> None``
    Flush any internal buffers.

``close() -> None``
    Release all held resources.  Must be idempotent.

Sinks are used as context managers; ``flush`` is called automatically on
clean exit and ``close`` is always called via ``__exit__``.

Shared :class:`~imednet.integrations.sink_base.SinkConfig` fields:

.. list-table::
   :header-rows: 1
   :widths: 30 15 55

   * - Field
     - Default
     - Description
   * - ``batch_size``
     - 500
     - Records per :meth:`write_batch` call.
   * - ``max_retries``
     - 3
     - Retry attempts on transient errors.
   * - ``retry_backoff``
     - 1.0
     - Base delay (seconds); grows as ``backoff * 2^attempt``.
   * - ``idempotent``
     - ``True``
     - Use upsert / MERGE semantics.

Error propagation
~~~~~~~~~~~~~~~~~

* Transient errors are retried up to ``max_retries`` times with
  exponential back-off.
* ``max_retries`` counts retries after the first attempt: zero means one
  execution, and two means at most three executions. Sink backoff starts at
  ``retry_backoff`` and doubles for each retry. Negative/non-integral retry
  counts and non-finite/negative backoff values are rejected at configuration.
* Sink configuration and input validation errors are not replayed. Driver
  exception messages are replaced before entering operation telemetry, because
  they may contain connection strings or payloads. Export errors report the
  actual number of destination calls, including zero when a breaker blocks
  execution, and retain ``batch_id`` without copying the driver message.
* After all retries are exhausted, sinks raise
  :class:`~imednet.errors.ExportBatchError` (carries ``batch_id``) so
  that callers can log and resume partial exports.
* Misconfiguration (missing credentials, invalid URIs) raises
  :class:`~imednet.errors.ExportConfigurationError` immediately before
  any data is written.
* Credentials and connection URIs are never written to logs.  Pass URIs
  through :func:`~imednet.integrations.sink_base._redact_uri` before
  logging them.

Optional dependency conventions
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Each sink module calls
``imednet.spi.sinks.require_optional_dep`` at
*connection time* (not at import time).  This means:

* ``import imednet_sinks.graph`` never raises even when
  ``neo4j`` is not installed.
* The error is raised when the first sink instance is created.
* The error message tells the user which ``imednet[<extra>]`` to install.

Extras naming:

.. include:: /_includes/install_extensions.rst

Public API exposure
~~~~~~~~~~~~~~~~~~~

* :mod:`imednet.integrations` exports tabular helpers and shared sink contracts
  (:class:`~imednet.integrations.sink_base.SinkConfig` and
  :class:`~imednet.integrations.sink_base.ExportSink`).
* Document, graph, and warehouse sinks are exposed from :mod:`imednet_sinks`
  (and dynamically via ``sdk.sinks`` when ``imednet-plugins-sinks`` is installed)
  or directly from their submodules:

  * :mod:`imednet_sinks.document`
  * :mod:`imednet_sinks.graph`
  * :mod:`imednet_sinks.warehouse`

* :mod:`apache_airflow_providers_imednet.export` wraps **only** the
  tabular helpers, while
  :class:`~apache_airflow_providers_imednet.operators.export.ImednetExportOperator`
  resolves ``mongodb``, ``neo4j``, and ``snowflake`` destinations dynamically
  via ``sdk.sinks`` at runtime.

Adding New Export Sinks
~~~~~~~~~~~~~~~~~~~~~~~

1. Create a module under ``packages/plugins-sinks/src/imednet_sinks/``.
2. Subclass ``imednet.spi.sinks.ExportSink`` and
   implement ``write_batch``, ``flush``, and ``close``.
3. Call ``imednet.spi.sinks.require_optional_dep``
   inside the constructor (not at module level).
4. Add the optional dependency to ``packages/plugins-sinks/pyproject.toml`` under
   ``project.optional-dependencies``; add a core extra only for a supported install recipe.
5. Expose it through :mod:`imednet_sinks` and its plugin entry points without adding
   static plugin imports to core.
6. Add unit tests; run the canonical verification tasks in :doc:`/how-to/verification`.

Static boundary checks
----------------------

The repository guard scans core, workflows, sinks and Airflow source roots.
It resolves relative imports before checking segment-aware package boundaries;
missing source trees fail explicitly. Type-only references follow the same
layering rules, without executing optional modules. Core's SPI is an intentional
CLI composition root. Dynamic entry points and legacy lazy import shims require
separate runtime tests. New sink implementations use ``imednet.spi.sinks``;
legacy ``imednet.spi.sink_base`` and ``imednet.integrations`` remain compatible.
