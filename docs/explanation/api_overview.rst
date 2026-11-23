API Overview
============

This page summarizes the core concepts of the Mednet EDC REST API and how the
SDK interacts with it.

The SDK uses ``httpx`` for both synchronous and asynchronous HTTP transport.

Lifecycle Management
--------------------

:class:`~imednet.sdk.ImednetSDK` and :class:`~imednet.sdk.AsyncImednetSDK`
have **distinct, mutually exclusive** lifecycle management methods:

* ``ImednetSDK`` – synchronous only.  Use ``with ImednetSDK(...) as sdk:`` or
  call ``sdk.close()``.  Calling ``close()`` on an instance that has an active
  async client raises :exc:`RuntimeError`; use ``await sdk.aclose()`` in that
  case.

* ``AsyncImednetSDK`` – asynchronous only.  Always use
  ``async with AsyncImednetSDK(...) as sdk:`` or call
  ``await sdk.aclose()`` explicitly.  Using the synchronous context manager
  (``with AsyncImednetSDK(...)``) raises :exc:`TypeError`.

Base URL
--------

The SDK communicates with the production API at
``https://edc.prod.imednetapi.com`` by default. Override this with
``IMEDNET_BASE_URL`` or the ``base_url`` argument to ``load_config`` for a
private deployment. See :doc:`/reference/configuration` for details.

Authentication
--------------

Requests authenticate using either an API/security key pair
(``x-api-key`` and ``x-imn-security-key`` headers) or an OIDC bearer token
(``oidc_token``, sent via ``Authorization: Bearer <token>``).
:class:`~imednet.core.base_client.BaseClient` initializes the configured
:class:`~imednet.auth.strategy.AuthStrategy`, while
:class:`~imednet.core.client.Client` and
:class:`~imednet.core.async_client.AsyncClient` call
:func:`imednet.config.load_config` (and :class:`~imednet.config.Config` /
:class:`~imednet.config.CredentialSource`) to resolve ``IMEDNET_API_KEY`` +
``IMEDNET_SECURITY_KEY`` or ``IMEDNET_OIDC_TOKEN`` from explicit arguments or
environment variables. Supplying both an incomplete key pair and ambiguous
credentials raises :class:`~imednet.errors.ConfigurationError`. See
:doc:`/reference/configuration` for precedence details.

HTTP methods and errors
-----------------------

The API primarily supports ``GET`` and ``POST`` requests. Non-successful
responses are mapped through :func:`imednet.errors.get_error_class` to typed
:class:`~imednet.errors.ApiError` subclasses, inspecting structured
``metadata.error`` codes and ``metadata.status`` strings
(:class:`~imednet.errors.ValidationError`,
:class:`~imednet.errors.AuthenticationError`,
:class:`~imednet.errors.ForbiddenError`, and
:class:`~imednet.errors.ServerError`) before falling back to HTTP status codes:

* ``400`` – :class:`~imednet.errors.BadRequestError` (or :class:`~imednet.errors.ValidationError` on ``metadata.error.code == "1000"`` / ``VALIDATION_ERROR``)
* ``401`` – :class:`~imednet.errors.UnauthorizedError` (or :class:`~imednet.errors.AuthenticationError` on ``metadata.error.code == "9001"`` / ``AUTHENTICATION_FAILED``)
* ``403`` – :class:`~imednet.errors.ForbiddenError` (preserved even when ``metadata.error.code == "9001"``)
* ``404`` – :class:`~imednet.errors.NotFoundError`
* ``409`` – :class:`~imednet.errors.ConflictError`
* ``429`` – :class:`~imednet.errors.RateLimitError`
* ``5xx`` – :class:`~imednet.errors.ServerError`
* other – :class:`~imednet.errors.ApiError`

Every SDK exception inherits from :class:`~imednet.errors.ImednetError` and carries an
optional :class:`~imednet.errors.DiagnosticContext` (with ``method``, query-redacted
``url``, ``retry_after``, ``attempt_count``, and ``cause_type``):

* Network-level transport failures (such as connection drops or read timeouts) raise
  :class:`~imednet.errors.RequestError` after exhausting configured retries, populating
  ``attempt_count`` with the executed wire attempts and ``cause_type`` with the underlying
  ``httpx.RequestError`` class name.
* When a client's ``CircuitBreaker`` is open after consecutive upstream failures, pre-flight
  calls raise :class:`~imednet.errors.CircuitBreakerError`—a direct subclass of
  :class:`~imednet.errors.ImednetError` rather than :class:`~imednet.errors.RequestError`—with
  ``attempt_count == 0`` so callers can separate fast-fail circuit blocks from wire-level
  network errors while still catching :class:`~imednet.errors.ImednetError` as a single root.

See :doc:`/how-to/retry_policy` for examples of handling these errors and configuring
custom retry logic.

Dynamic path segment validation
-------------------------------

Before constructing REST endpoint paths for a ``Study``, ``Subject``, ``Site``, ``Form``,
``Record``, ``Visit``, ``Interval``, ``Variable``, ``Query``, ``Data Dictionary``, or
background ``Job``, the SDK validates every caller-supplied URL segment through
:func:`~imednet.utils.validate_path_segment` and formats paths with
:func:`~imednet.utils.build_safe_path`:

* :func:`~imednet.utils.validate_path_segment` rejects ``None``, ``bool`` values, empty or
  whitespace-only strings, ASCII control characters, path separators (``/`` and ``\``),
  query/fragment delimiters (``?`` and ``#``), dot segments (``.`` and ``..``), and single-
  or multi-stage percent-encoded traversal sequences (such as ``%2e%2e`` or ``%252f``),
  raising :class:`~imednet.errors.PathTraversalValidationError` (a
  :class:`~imednet.errors.ValidationError` subclass with ``status_code=400``) before any
  HTTP request leaves the client.
* :func:`~imednet.utils.build_safe_path` validates ``base_path`` and each positional path
  segment, returning a slash-normalized relative path with reserved characters
  percent-encoded while keeping ``@`` unescaped for Mednet identifier compatibility.

.. testcode::

   from imednet.errors import PathTraversalValidationError
   from imednet.utils import build_safe_path, validate_path_segment

   assert validate_path_segment("STUDY_01", name="study_key") == "STUDY_01"
   safe_job_path = build_safe_path("studies", "STUDY 01", "jobs", "batch@2026")
   assert safe_job_path == "studies/STUDY%2001/jobs/batch@2026"

   for unsafe_study_key in ("../admin", "%2e%2e%2fsecret", "STUDY?admin=1"):
       rejected_seg = False
       try:
           validate_path_segment(unsafe_study_key, name="study_key")
       except PathTraversalValidationError as path_err:
           rejected_seg = True
           assert path_err.status_code == 400
       assert rejected_seg

Filtering
---------

Endpoints accept a ``filter`` parameter that matches the syntax documented in the
Mednet API guide. For clinical data queries, ``recordDataFilter`` can be supplied
alongside ``filter``. See :mod:`imednet.endpoints.records` for usage examples.

Endpoints are stateless and fetch fresh data on every call. For local schema
and record caching helpers, see :doc:`/how-to/caching`.

Dates must use UTC timestamps except where noted. When filtering visits by
``startDate``, ``dueDate``, ``endDate`` or ``visitDate``, use ``YYYY-MM-DD``.
String values containing spaces or special characters must be wrapped in
double quotes. For example: ``siteName=="My Site"``.

Filter helper
-------------

Use :func:`imednet.utils.filters.build_filter_string` to construct filter
expressions from a mapping. Values containing spaces must be wrapped in double
quotes. Example::

   build_filter_string({"site_name": "Bright Test Site"})
   # siteName=="Bright Test Site"

Error responses
---------------

When a request fails, error details are returned in the ``metadata`` section of
the response body. Validation errors include the offending field and value.
Example::

   {
     "metadata": {
       "status": "BAD_REQUEST",
       "path": "/api/v1/edc/studies",
       "timestamp": "2018-10-18 05:46:29",
       "error": {
         "code": "1000",
         "description": "Field raised validation errors",
         "field": {
           "attribute": "page",
           "value": "XX"
         }
       }
     }
   }

Error response fields
~~~~~~~~~~~~~~~~~~~~~

``code``
  Error code

``description``
  Error description message

``field.attribute``
  Origination request attribute which caused the error

``field.value``
  The value of request attribute passed in the request

:class:`~imednet.errors.ApiError` normalizes both single-object ``metadata.error`` payloads
and multi-error lists into :attr:`~imednet.errors.ApiError.errors` while exposing the
primary offending attribute/variable name via :attr:`~imednet.errors.ApiError.field_attribute`
(reading ``field.attribute`` or ``field.variable``) and the redacted offending value via
:attr:`~imednet.errors.ApiError.field_value`:

.. testcode::

   from imednet.errors import BadRequestError, DiagnosticContext

   diag = DiagnosticContext(
       method="GET",
       url="https://edc.prod.imednetapi.com/api/v1/edc/studies/STUDY/records?api_key=secret&page=XX",  # pragma: allowlist secret
       attempt_count=1,
   )
   bad_req = BadRequestError(
       {
           "metadata": {
               "status": "BAD_REQUEST",
               "error": [
                   {
                       "code": "1000",
                       "description": "Field raised validation errors",
                       "field": {"attribute": "page", "value": "XX"},
                   },
                   {
                       "code": "1000",
                       "description": "Unknown Variable filter",
                       "field": {"variable": "V_WEIGHT", "value": "-1"},
                   },
               ],
           }
       },
       status_code=400,
       diagnostic_context=diag,
   )
   assert len(bad_req.errors) == 2
   assert bad_req.field_attribute == "page"
   assert bad_req.field_value == "XX"
   assert bad_req.method == "GET"
   assert "secret" not in (bad_req.url or "")
   assert "page=XX" in (bad_req.url or "")

Error codes
~~~~~~~~~~~

``1000``
  Validation error (:class:`~imednet.errors.ValidationError`). Request contains an invalid value.

``9000``
  Server error (:class:`~imednet.errors.ServerError`). Unknown error; contact Mednet support for assistance.

``9001``
  Authorization or authentication error (:class:`~imednet.errors.ForbiddenError` on HTTP ``403`` / ``FORBIDDEN``, or :class:`~imednet.errors.AuthenticationError` otherwise).

Integrations and analytical exports
-----------------------------------

For DuckDB table exports, Hive-partitioned Parquet layouts, and incremental
bronze/silver ingestion workflows, see :doc:`/how-to/duckdb_integration`.
