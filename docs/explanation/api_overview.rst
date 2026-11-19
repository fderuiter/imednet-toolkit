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

All requests must include ``x-api-key`` and ``x-imn-security-key`` headers. The
:class:`~imednet.core.base_client.BaseClient` handles shared initialization
while the :class:`~imednet.core.client.Client` and
:class:`~imednet.core.async_client.AsyncClient` rely on
:func:`imednet.config.load_config` to read ``IMEDNET_API_KEY`` and
``IMEDNET_SECURITY_KEY`` from the environment and populate these headers
automatically. See :doc:`/reference/configuration` for more information.

HTTP methods and errors
-----------------------

The API primarily supports ``GET`` and ``POST`` requests. Non‐successful status
codes raise typed exceptions:

* ``400`` – :class:`~imednet.errors.ValidationError`
* ``401`` – :class:`~imednet.errors.AuthenticationError`
* ``403`` – :class:`~imednet.errors.AuthorizationError`
* ``404`` – :class:`~imednet.errors.NotFoundError`
* ``429`` – :class:`~imednet.errors.RateLimitError`
* ``5xx`` – :class:`~imednet.errors.ServerError`

See :doc:`/how-to/retry_policy` for examples of handling these errors and configuring
custom retry logic.

Filtering
---------

List endpoints accept top-level ``page``, ``size`` (or ``limit``), ``sort``, and
``filter`` parameters matching the Mednet API common query specification. When both
a raw ``filter`` string and keyword attribute filters are supplied, the SDK joins
them with ``;`` (``AND``) and validates filter syntax before sending the request.

For clinical form data queries, :class:`~imednet.endpoints.records.RecordsEndpoint`
additionally accepts ``record_data_filter`` (or ``recordDataFilter``) alongside
``filter`` as a separate top-level parameter. Passing clinical data filters to
non-record endpoints raises :class:`~imednet.errors.ConfigurationError`. See
:mod:`imednet.endpoints.records` for usage examples.

List endpoints execute live API requests on each call and do not cache responses
in memory. For validation metadata caching, call
:meth:`~imednet.validation.cache.SchemaCache.refresh`, or use
:class:`~imednet_workflows.cached_loader.CachedRecordsLoader` for persistent
incremental record synchronization.

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
  Validation error. Request contain invalid value.

``9000``
  Unknown error. Please contact Mednet support for assistance.

``9001``
  Unauthorized error. Insufficient permission to retrieve data.

Integrations and analytical exports
-----------------------------------

For DuckDB table exports, Hive-partitioned Parquet layouts, and incremental
bronze/silver ingestion workflows, see :doc:`/how-to/duckdb_integration`.
