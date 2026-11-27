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
  backoff schedule and is bounded by ``RetryConfig.max_retry_after``
  (default ``60.0`` seconds).

Timeouts
~~~~~~~~

* ``timeout`` is forwarded directly into ``httpx.Client`` / ``httpx.AsyncClient``.
* You can pass either a float (single timeout applied to all phases) or
  ``httpx.Timeout`` for per-phase timeouts.

Circuit breaker and probe leases
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

* ``HTTPClientBase`` initializes a dedicated ``CircuitBreaker`` per client instance
  (``failure_threshold=5``, ``recovery_timeout=10.0`` seconds,
  ``half_open_max_probes=1``), isolating ``CircuitState`` across independent
  ``Study`` clients.
* ``CircuitState`` progresses through ``CLOSED``, ``OPEN``, and ``HALF_OPEN``:
  five consecutive ``httpx.RequestError`` or HTTP ``5xx`` outcomes via
  ``record_failure()`` open the breaker, causing new requests to fail immediately with
  :class:`imednet.errors.CircuitBreakerError` (subclassing
  :class:`imednet.errors.ImednetError` directly with ``attempt_count=0`` on
  :class:`imednet.errors.DiagnosticContext`).
* After ``recovery_timeout`` elapses, the next request transitions the breaker to
  ``HALF_OPEN`` and acquires a ``ProbeLease`` (enforcing ``half_open_max_probes=1``).
  A ``2xx`` response calls ``record_success()`` and returns the breaker to ``CLOSED``;
  a ``5xx`` or network error re-opens it; and early exits (task cancellation or
  non-429 ``4xx`` responses) invoke ``ProbeLease.release_if_unsettled()`` so in-flight
  probe capacity is never leaked.
* HTTP ``429`` responses invoke ``record_rate_limit()`` rather than
  ``record_failure()``: in ``CLOSED`` state they leave the consecutive failure counter
  untouched so rate-limited ``Subject``, ``Site``, ``Form``, ``Record``, ``Visit``,
  ``Interval``, ``Variable``, ``Query``, ``Data Dictionary``, and ``Job`` traffic is
  paced by ``Retry-After`` without opening the circuit, whereas in ``HALF_OPEN`` state
  an HTTP ``429`` immediately re-opens the breaker.

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

* Transport logs and formatted errors never include raw credential headers.
* URLs logged by request monitoring or attached to :class:`imednet.errors.DiagnosticContext`
  are sanitized through :func:`imednet.utils.url.redact_url_query`, redacting
  sensitive query params such as ``api_key``, ``security_key``, ``token``,
  ``secret``, and ``password``.
* Both retried and unretried transport failures raise :class:`imednet.errors.RequestError`
  with structured :class:`imednet.errors.DiagnosticContext` and chained causes,
  open circuit breakers raise :class:`imednet.errors.CircuitBreakerError`, and
  credential configuration errors raise :class:`imednet.errors.ConfigurationError`
  so callers can catch all SDK failures via :class:`imednet.errors.ImednetError`.

Async Client
------------

:class:`~imednet.core.async_client.AsyncClient` powers a unified **async-first
execution engine** shared across Data Dictionary loaders, study orchestrators,
all nine built-in workflows, and streaming export sinks. The public SDK surface
exposes two strictly typed client facades with **symmetric unprefixed method
names**:

* :class:`~imednet.sdk.ImednetSDK` (:class:`~imednet.spi.facade.ImednetFacade`)
  for synchronous scripts, CLI commands, and interactive notebooks. Synchronous
  wrappers delegate to canonical ``async`` engines via an event-loop-aware bridge
  that detects active ``asyncio`` loops (including Jupyter, ``pytest-asyncio``,
  and single-threaded WebAssembly/Pyodide runtimes) so callers never hit
  ``RuntimeError``.
* :class:`~imednet.sdk.AsyncImednetSDK`
  (:class:`~imednet.spi.facade.AsyncImednetFacade`) for native ``async``/``await``
  services, exposing symmetric coroutines (for example ``await sdk.get_records(...)``,
  ``await sdk.create_records_batch(...)``, ``await sdk.poll_jobs(...)``,
  ``await async_load_from_api(sdk, ...)``, and
  :class:`~imednet.orchestration.AsyncMultiStudyOrchestrator`). Legacy
  ``async_*`` convenience methods remain available on ``AsyncImednetSDK`` as
  deprecated compatibility aliases.

Manage asynchronous client lifecycles with ``async with AsyncImednetSDK(...) as sdk:``
or ``await sdk.aclose()``.

Endpoints
---------

Each endpoint, such as :class:`~imednet.endpoints.studies.StudiesEndpoint`,
wraps a related set of API operations. Endpoints now use **composition**:
`GenericListGetEndpoint` composes operation classes (for example
``ListOperation`` and ``FilterGetOperation``) rather than inheriting deep
operation mixin chains. This keeps method resolution straightforward, improves
IDE autocomplete, and avoids MRO-related typing ambiguity. Endpoints expose
``list``/``get`` methods that return typed models.

Workflows
---------

Workflows orchestrate several endpoints over shared ``async`` cores to perform
higher-level clinical operations—for example,
:class:`~imednet_workflows.record_update.RecordUpdateWorkflow` and
:class:`~imednet_workflows.record_update.AsyncRecordUpdateWorkflow` validate
record payloads, submit bounded batches, and poll resulting jobs. All nine
built-in workflows expose symmetric unprefixed methods under ``sdk.workflows``
on both :class:`~imednet.sdk.ImednetSDK`
(:class:`~imednet.plugins.SyncWorkflowsNamespaceProtocol`) and
:class:`~imednet.sdk.AsyncImednetSDK`
(:class:`~imednet.plugins.AsyncWorkflowsNamespaceProtocol`), while ``sdk.sinks``
binds :class:`~imednet_sinks.plugin.SinksNamespace` and
:class:`~imednet_sinks.plugin.AsyncSinksNamespace` over native synchronous and
asynchronous sink drivers.

Caching
-------

:doc:`/how-to/caching` describes how schema metadata and local record caches
work. Cached schema metadata can be reloaded by calling ``schema.refresh()`` on
a :class:`~imednet.validation.cache.SchemaCache` instance.

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

* Implement a canonical ``Async*`` workflow against
  :class:`~imednet.spi.facade.AsyncImednetFacade` under
  ``packages/plugins-workflows/src/imednet_workflows`` and wrap synchronous
  :class:`~imednet.spi.facade.ImednetFacade` calls over it.
* Register the synchronous adapter in ``imednet.workflows`` and the asynchronous
  adapter in ``imednet.async_workflows``.
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
     - Destination is a flat/columnar store (CSV, Excel, JSON, SQL, DuckDB,
       Parquet).  All variables for a form or CDISC SDTM domain can be
       represented as columns using ``snake_case``, ``camelCase``, or
       uppercase ``sdtm`` naming conventions.
     - ``imednet.spi.sink_base.SinkConfig`` +
       ``imednet.spi.models.transform_records_to_sdtm_rows`` →
       ``_TabularTransformSink`` / ``pandas.DataFrame`` → sink functions in
       :mod:`imednet.integrations.export`
   * - **Document**
     - Destination is a document store (for example MongoDB) and should keep
       nested ``record_data`` payloads with metadata envelope fields (aligned
       to ``naming_convention``) or emit flat CDISC SDTM domain documents
       when ``as_sdtm=True``. Encodes pure ``date`` scalars as ISO-8601
       strings and ``Decimal`` values as ``bson.Decimal128``, keying ``_id``
       on ``<study_key>/<record_id>`` (falling back to
       ``<STUDYID>/<DOMAIN>/<USUBJID>/<*SEQ>`` when ``as_sdtm=True`` and
       ``record_id`` is absent).
     - ``imednet.spi.sinks.build_sink_mapper`` / ``CentralizedMapper`` →
       :class:`~imednet_sinks.document.MongoDbExportSink` /
       :class:`~imednet_sinks.document.AsyncMongoDbExportSink`
       (native ``pymongo.AsyncMongoClient``)
   * - **Graph**
     - Destination models relationships natively (for example Neo4j),
       preserving *Study → Subject → Visit → Record* for scheduled ``Visit``
       items while normalizing ``visit_id <= 0`` to ``None`` so unscheduled
       ``Record`` nodes link directly via ``(:Subject)-[:HAS_RECORD]->(:Record)``
       under either ``naming_convention`` properties or CDISC SDTM domain
       node properties (``as_sdtm=True``, keyed on
       ``(STUDYID, DOMAIN, USUBJID, *SEQ)`` or ``(STUDYID, DOMAIN, USUBJID)``
       for domains without ``*SEQ`` such as ``DM``).
     - ``imednet.spi.sinks.build_sink_mapper`` / ``CentralizedMapper`` →
       :class:`~imednet_sinks.graph.Neo4jExportSink` /
       :class:`~imednet_sinks.graph.AsyncNeo4jExportSink`
       (native ``neo4j.AsyncGraphDatabase``)
   * - **Warehouse**
     - Destination is a cloud data warehouse with a native bulk loader.
       ``Record`` items are mapped via core SPI (supporting ``snake_case``
       ``(study_key, record_id)``, ``camelCase`` ``(studyKey, recordId)``,
       ``sdtm`` ``(STUDYID, RECORD_ID)``, and ``as_sdtm=True`` SDTM domain
       merge keys ``(STUDYID, DOMAIN, USUBJID, *SEQ)`` or
       ``(STUDYID, DOMAIN, USUBJID)`` for ``DM``), staged as Parquet files,
       and loaded via ``MERGE INTO`` / ``COPY INTO`` for best throughput.
     - ``imednet.spi.sinks.build_sink_mapper`` / ``CentralizedMapper`` →
       Parquet staging →
       :class:`~imednet_sinks.warehouse.SnowflakeExportSink` /
       :class:`~imednet_sinks.warehouse.AsyncSnowflakeExportSink`

Data flow diagram
~~~~~~~~~~~~~~~~~

.. mermaid:: /diagrams/architecture_6.mmd

Shared ``ExportSink`` and ``AsyncExportSink`` contracts
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

All non-tabular synchronous sinks subclass
``imednet.spi.sinks.ExportSink`` (executed via
:func:`~imednet.integrations.sink_base.run_streaming_export` and
:func:`~imednet.integrations.dispatcher.export`), while native asynchronous
sinks subclass ``imednet.spi.sinks.AsyncExportSink`` (executed via
:func:`~imednet.integrations.sink_base.async_run_streaming_export` and
:func:`~imednet.integrations.dispatcher.async_export`) with identical PHI
masking, 21 CFR Part 11 voided-record reconciliation, incremental watermark
checkpointing, and CDISC SDTM lineage guarantees. Each sink implements three
lifecycle methods (coroutines on ``AsyncExportSink``):

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

Shared :class:`~imednet.spi.sink_base.SinkConfig` fields:

.. list-table::
   :header-rows: 1
   :widths: 28 16 56

   * - Field
     - Default
     - Description
   * - ``study_key``
     - *(required)*
     - Non-empty ``Study`` key targeted by the export operation.
   * - ``batch_size``
     - ``500``
     - ``Record`` items per :meth:`write_batch` call.
   * - ``max_buffered_records``
     - ``None``
     - Optional global upper bound on ``Record`` items buffered simultaneously across per-``Form`` streams (defaults to ``max(batch_size, 5000)``).
   * - ``max_retries``
     - ``3``
     - Retry attempts on transient destination errors.
   * - ``retry_backoff``
     - ``1.0``
     - Base delay (seconds); grows exponentially as ``retry_backoff * 2^attempt``.
   * - ``idempotent``
     - ``True``
     - Use upsert / ``MERGE`` semantics keyed on deterministic ``Record`` identifiers (required when ``incremental=True`` or ``state_provider`` is supplied).
   * - ``mask_phi``
     - ``True``
     - Redact registered sensitive ``Subject`` and ``StudyConfiguration`` ``phiFields`` values to ``***MASKED***`` before serialization.
   * - ``extra``
     - ``{}``
     - Destination-specific extension dictionary forwarded to custom sink plugins.
   * - ``quality_gate_enabled``
     - ``False``
     - Run :func:`~imednet.spi.sink_base.apply_quality_gate` before batch writes to filter ``Record`` items below the readiness threshold.
   * - ``min_schema_readiness_score``
     - ``100.0``
     - Minimum readiness score (``0.0``–``100.0``) required when ``quality_gate_enabled=True``.
   * - ``tracer``
     - ``None``
     - Optional OpenTelemetry-compatible tracer used to emit sink batch spans.
   * - ``modified_since``
     - ``None``
     - Lower-bound UTC modification watermark (``datetime`` or ISO-8601 string) applied when no committed checkpoint exists.
   * - ``record_filter``
     - ``None``
     - Optional ``Record`` metadata filter dictionary or filter expression string pushed down to ``RecordsEndpoint``.
   * - ``record_data_filter``
     - ``None``
     - Optional ``Variable`` payload filter expression (``recordDataFilter``).
   * - ``include_deleted``
     - ``False``
     - Include voided (``deleted=True``) ``Record`` items directly in ``write_batch`` payloads instead of routing them to ``reconcile_deleted``.
   * - ``incremental``
     - ``False``
     - Enable transactional high-water mark reads and commits via a ``StateProviderProtocol`` ledger.
   * - ``stream_name``
     - ``None``
     - Logical stream key inside the state ledger (defaults to ``"records"`` or a destination-specific identifier; ``ImednetExportOperator`` derives target-scoped defaults from ``dag_id``, ``task_id``, ``destination``, and sanitized target names while seeding from a matching pre-upgrade checkpoint when state is absent).
   * - ``ledger_path``
     - ``None``
     - Filesystem path passed to file-backed checkpoint providers when ``state_provider`` is not supplied explicitly (always resolving :class:`~imednet_workflows.state_ledger.FileStateProvider` ahead of Airflow ``Variable`` defaults).
   * - ``state_provider``
     - ``None``
     - Pre-configured :class:`~imednet.spi.sink_base.StateProviderProtocol` instance overriding entry-point discovery.
   * - ``as_sdtm``
     - ``False``
     - Transform exported ``Record`` payloads into standardized CDISC SDTM domain rows via :func:`~imednet.spi.models.transform_records_to_sdtm_rows` (automatically enabled when ``sdtm_domain`` is supplied unless ``as_sdtm=False`` is passed explicitly).
   * - ``sdtm_domain``
     - ``None``
     - Optional 2-character CDISC SDTM domain code (for example ``"AE"``, ``"DM"``, or ``"VS"``) selecting the target domain for ``as_sdtm`` transformation or ``naming_convention="sdtm"`` alias resolution.
   * - ``preserve_record_id``
     - ``None``
     - Optional override controlling whether ``RECORD_ID`` is retained on ``as_sdtm=True`` projections (defaults to ``True`` only for incremental exports — when ``incremental=True``, ``modified_since`` is set, or ``state_provider`` is supplied — so delta syncs merge and reconcile voided ``Record`` items deterministically while full baseline SDTM exports omit EDC ``RECORD_ID`` columns by default; set ``True`` or ``False`` to override explicitly).
   * - ``standards_profile``
     - ``None``
     - Optional CDISC standards validation profile name (for example ``"drug"``, ``"device"``, or ``"general"``) or :class:`~imednet.models.standards.StandardsProfile` evaluated before sink writes.
   * - ``min_standards_score``
     - ``None``
     - Optional minimum CDISC standards readiness score (``0.0``–``100.0``) enforced via :func:`~imednet.spi.sink_base.evaluate_standards_readiness` prior to export.
   * - ``naming_convention``
     - ``None``
     - Optional raw variable and metadata key naming convention (``"snake_case"``, ``"camelCase"``, or ``"sdtm"``) applied across tabular, document, graph, and warehouse mappers.

Streaming checkpoints and state provider SPI
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Incremental exports decouple destination sinks from checkpoint persistence through
:class:`~imednet.spi.sink_base.StateProviderProtocol` and
:func:`~imednet.spi.sink_base.get_state_provider`:

* ``StateProviderProtocol`` defines the runtime-checkable contract for reading and
  committing per-``Study`` stream watermarks: ``get_last_timestamp(study_key, stream_name)``,
  ``set_last_timestamp(...)``, ``transaction(study_key, stream_name, fallback_timestamp=None)``,
  ``read_state()``, and ``delete_entry(study_key, stream_name=None)``.
* ``get_state_provider(ledger_path=None, *, provider_name=None)`` resolves plugins registered
  under the ``imednet.state_providers`` entry-point group (shipped by ``imednet-workflows``),
  routing explicit ``ledger_path`` arguments directly to ``FileStateProvider`` even under
  ``default`` or ``airflow`` provider settings:

  - ``default`` → :func:`imednet_workflows.state_ledger.get_state_provider` (returns ``FileStateProvider`` for explicit custom paths before checking ``AIRFLOW_CTX_TASK_ID`` or ``USE_AIRFLOW_STATE_PROVIDER``).
  - ``file`` → :class:`imednet_workflows.state_ledger.FileStateProvider` (also exported as the backward-compatible alias :class:`imednet_workflows.ExtractionStateLedger`).
  - ``airflow`` → :class:`imednet_workflows.state_ledger.AirflowStateProvider` (persists consolidated per-``Study`` stream watermarks in Airflow ``Variable`` storage using optimistic SQL compare-and-swap outside ``RuntimeTaskInstance``, ``lease_token`` / ``lease_id`` lease fencing via ``airflow.sdk.Variable`` inside ``RuntimeTaskInstance``, monotonic watermarks, and wildcard-escaped exact ``Study`` deletes).

* :func:`~imednet.spi.sink_base.run_streaming_export` orchestrates end-to-end streaming
  writes across every ``ExportSink``:

  1. Opens ``provider.transaction(...)`` when ``incremental=True`` or ``state_provider`` is set,
     resolving the active watermark from the stored ``last_timestamp`` or ``modified_since`` fallback.
  2. Streams paginated ``Record`` items via :func:`~imednet.spi.sink_base.iter_filtered_sdk_records`,
     combining ``watermark``, ``record_filter``, and ``record_data_filter`` into AND-safe query branches.
  3. Observes ``Record`` modification timestamps while routing voided
     (``deleted=True``) items to ``sink.reconcile_deleted(...)`` on incremental idempotent
     runs (and full idempotent runs for persistent sinks setting
     ``reconciles_deleted_on_full_sync = True``) when ``include_deleted=False`` (with
     multi-``Form`` tabular helpers routing single-pass ``Record`` streams through
     :class:`~imednet.spi.sink_base.BoundedFormBufferRouter`).
  4. Applies :func:`~imednet.spi.sink_base.apply_quality_gate` and streams fixed-size chunks
     into ``sink.write_batch(...)``. When ``quality_gate_enabled=True`` and
     ``apply_quality_gate`` drops low-readiness ``Record`` items, both
     ``run_streaming_export`` and multi-``Form`` tabular streaming cap the candidate
     ``max_ts`` watermark at ``min_rejected_ts`` (selecting the highest exported ``Record``
     timestamp ``<= min_rejected_ts``, or leaving ``max_ts`` as ``None`` when no exported
     timestamp precedes ``min_rejected_ts`` or any rejected ``Record`` lacks a timestamp)
     so rejected ``Record`` items are re-evaluated on subsequent incremental runs.
  5. Flushes and closes the sink before exiting ``provider.transaction(...)``, ensuring the
     capped high-water mark commits **only** after every batch, delete reconciliation, and
     ``sink.flush()`` succeed.

Error propagation
~~~~~~~~~~~~~~~~~

* Transient errors are retried up to ``max_retries`` times with
  exponential back-off.
* ``max_retries`` counts retries after the first attempt: zero means one
  execution, and two means at most three executions. Sink backoff starts at
  ``retry_backoff`` and doubles for each retry. Negative/non-integral retry
  counts and non-finite/negative backoff values are rejected at configuration.
* Every :class:`~imednet.spi.sink_base.ExportSink` instance owns an isolated
  per-sink ``CircuitBreaker`` (``failure_threshold=5``,
  ``recovery_timeout=10.0`` seconds, ``half_open_max_probes=1``) passed to
  ``UniversalExecutor`` inside ``ExportSink._execute_with_retry``, ensuring a
  failing warehouse or document target never trips sibling sinks or HTTP API
  clients.
* Sink configuration and input validation errors are not replayed and do not
  poison the sink ``CircuitBreaker``. Driver exception messages are replaced
  before entering operation telemetry, because they may contain connection
  strings or payloads. Export errors report the actual number of destination
  calls—including ``0`` attempts when an open sink ``CircuitBreaker`` blocks
  execution before invoking the driver—and retain ``batch_id`` while chaining
  the sanitized :class:`~imednet.errors.CircuitBreakerError` without copying
  the raw driver message.
* After all retries are exhausted (or when blocked by an open sink breaker),
  sinks raise :class:`~imednet.errors.ExportBatchError` (carries ``batch_id``)
  so that callers can log and resume partial exports.
* Misconfiguration (missing credentials, invalid URIs) raises
  :class:`~imednet.errors.ExportConfigurationError` immediately before
  any data is written.
* Credentials and connection URIs are never written to logs.  Pass URIs
  through :func:`~imednet.spi.sinks.redact_uri` before
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

* :mod:`imednet.integrations` registers all 12 tabular and CDISC exchange helpers on
  :class:`~imednet.integrations.dispatcher.ExportRegistry`, while
  :mod:`imednet.spi.sinks` (and :mod:`imednet.spi.sink_base`) defines shared
  sink contracts (:class:`~imednet.spi.sink_base.SinkConfig`,
  :class:`~imednet.spi.sink_base.ExportSink`, and
  :class:`~imednet.spi.sink_base.BoundedFormBufferRouter`).
* Document, graph, and warehouse sinks are exposed from :mod:`imednet_sinks`
  (and dynamically via ``sdk.sinks`` when ``imednet-plugins-sinks`` is installed)
  or directly from their submodules:

  * :mod:`imednet_sinks.document`
  * :mod:`imednet_sinks.graph`
  * :mod:`imednet_sinks.warehouse`

* :mod:`apache_airflow_providers_imednet.export` wraps all 12 tabular and CDISC exchange helpers
  (``export_to_csv``, ``export_to_dataset_json``, ``export_to_duckdb``,
  ``export_to_duckdb_by_form``, ``export_to_excel``, ``export_to_hive_parquet``,
  ``export_to_json``, ``export_to_long_sql``, ``export_to_odm``, ``export_to_parquet``,
  ``export_to_sql``, and ``export_to_sql_by_form``).
  :class:`~apache_airflow_providers_imednet.operators.export.ImednetExportOperator`
  dispatches those 12 core targets alongside ``mongodb``, ``neo4j``, and
  ``snowflake`` plugin sinks resolved dynamically via ``sdk.sinks`` at runtime
  (keeping ``apache_airflow_providers_imednet`` decoupled from ``imednet_sinks``),
  while :meth:`~apache_airflow_providers_imednet.hooks.ImednetHook.resolve_destination_connection`
  resolves ``destination_conn_id`` settings for ``snowflake``, ``neo4j``,
  ``mongodb``, ``duckdb``, ``duckdb_by_form``, ``hive_parquet``, ``sql``,
  ``sql_by_form``, and ``long_sql``.

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

Restricted AST Business Logic Sandbox
-------------------------------------

User-authored :class:`~imednet.models.study_config.MappingRule` ``business_logic``
expressions run inside :class:`~imednet.utils.RestrictedAstEvaluator` (via
:func:`~imednet.utils.parse_expression`, :func:`~imednet.utils.evaluator.validate_ast`,
and :func:`~imednet.utils.evaluate_expression`) rather than Python ``eval``:

* **Explicit AST node allowlist:** Only pure scalar, arithmetic, boolean, comparison,
  ternary ``IfExp``, container literal, subscript/slice, public attribute read, and
  allowlisted call nodes are permitted; statements, lambdas, comprehensions, ``await``,
  and f-strings (``JoinedStr`` / ``FormattedValue``) raise
  :class:`~imednet.errors.RestrictedEvaluationError`.
* **Bounded call surface:** Free function calls are restricted to
  ``DEFAULT_SAFE_FUNCTIONS`` (``float``, ``int``, ``str``, ``bool``, ``len``), and
  method calls are restricted to ``DEFAULT_SAFE_METHODS`` (read-only string, ``dict``,
  ``list``/``tuple``, and ``set`` inspection helpers). Private ``_`` attributes,
  ``.format`` / ``.format_map`` (which could otherwise traverse object attributes via
  format strings), and mutating methods (such as ``clear``, ``pop``, ``update``, or
  ``append``) are rejected before execution so rule evaluation cannot escape the
  sandbox or mutate shared ``Record`` payloads.
* **Non-fatal pipeline fallback:** ``EnrichmentPipeline``,
  :func:`~imednet.spi.models.transform_records_to_sdtm_rows`, and
  ``extract_canonical_records`` catch :class:`~imednet.errors.RestrictedEvaluationError`
  (and runtime :class:`~imednet.errors.EvaluationError`) per rule, log a warning, and
  preserve the raw or configured fallback ``Variable`` value.

.. testcode::

   from imednet.errors import EvaluationError, RestrictedEvaluationError
   from imednet.utils import (
       RestrictedAstEvaluator,
       evaluate_expression,
       parse_expression,
   )
   from imednet.utils.evaluator import (
       DEFAULT_SAFE_FUNCTIONS,
       DEFAULT_SAFE_METHODS,
       validate_ast,
   )

   assert set(DEFAULT_SAFE_FUNCTIONS) == {"float", "int", "str", "bool", "len"}
   assert "format" not in DEFAULT_SAFE_METHODS
   assert "format_map" not in DEFAULT_SAFE_METHODS
   assert "clear" not in DEFAULT_SAFE_METHODS

   parsed_tree = parse_expression("float(value.strip()) * 2.2")
   validate_ast(parsed_tree)
   evaluator = RestrictedAstEvaluator("float(value.strip()) * 2.2")
   assert round(evaluator.evaluate({"value": " 70.0 "}), 1) == 154.0
   assert evaluate_expression("payload.get('UNIT', 'kg').upper()", {"payload": {"UNIT": "kg"}}) == "KG"

   for blocked_expr in ("'{0.__class__}'.format(value)", "f'{value}'", "payload.clear()"):
       blocked = False
       try:
           evaluate_expression(
               blocked_expr,
               {"value": "70", "payload": {"UNIT": "kg"}},
               raise_on_error=True,
           )
       except RestrictedEvaluationError:
           blocked = True
       assert blocked

   runtime_failed = False
   try:
       evaluate_expression("int(value)", {"value": "bad"}, raise_on_error=True)
   except EvaluationError:
       runtime_failed = True
   assert runtime_failed

Static boundary checks
----------------------

The repository guard scans core, workflows, sinks and Airflow source roots.
It resolves relative imports before checking segment-aware package boundaries;
missing source trees fail explicitly. Type-only references follow the same
layering rules, without executing optional modules. Core's SPI is an intentional
CLI composition root. Dynamic entry points and legacy lazy import shims require
separate runtime tests. New sink implementations use ``imednet.spi.sinks``;
legacy ``imednet.spi.sink_base`` and ``imednet.integrations`` remain compatible.
