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
responses are mapped through :func:`imednet.errors.get_error_class`, which
inspects structured ``metadata.error`` codes and ``metadata.status`` strings
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

See :doc:`/how-to/retry_policy` for examples of handling these errors and configuring
custom retry logic.

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
