Caching Behavior
================

This page explains how the SDK handles data caching.

Endpoint Caching
----------------

The SDK no longer maintains any stateful in-memory cache on endpoint instances.
Every call to ``list()`` performs a fresh API request,
ensuring that concurrent requests from different application contexts cannot
contaminate each other's data.

If your application needs to reduce redundant API calls, implement caching at
the application layer using a library such as `cachetools`_, a request-level
cache middleware (e.g. ``httpx-cache``), or your framework's own cache
primitives (e.g. Django's cache framework or FastAPI dependency injection).

.. _cachetools: https://cachetools.readthedocs.io/

Schema Cache
------------

:class:`~imednet.validation.cache.SchemaCache` (and
:class:`~imednet.validation.cache.AsyncSchemaCache`) stores Variable metadata
partitioned by ``(study_key, form_key)`` and ``(study_key, form_id)`` to validate
Record payloads locally. It can be populated explicitly via ``SchemaCache.refresh()``
using :class:`~imednet.endpoints.forms.FormsEndpoint` and
:class:`~imednet.endpoints.variables.VariablesEndpoint`, or lazily through
:class:`~imednet.validation.cache.SchemaValidator` when validation runs.

What ``SchemaCache`` Validates vs. Skips
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Because ``SchemaCache`` is hydrated from REST ``/forms`` and ``/variables``
endpoints, it performs **structural and scalar type checks only**:

- **Enforced by** ``SchemaCache``: Form existence, ``formId`` to ``formKey``
  resolution, unknown Variable rejection, and scalar Variable type checks
  (``Number``, ``Integer``, ``Float``, ``Date``, ``Text``).
- **Skipped by** ``SchemaCache``: Required Variable policies (``Yes`` and
  ``Auto Query``), Choice codelist membership, numeric (``minValue`` /
  ``maxValue``) and text length (``minLength`` / ``maxLength``) bounds, and
  conditional XML business logic rules.

When you need offline constraint and edit check enforcement without polling
``/forms`` or ``/variables``, load a
:class:`~imednet.datadict.models.DataDictionary` and pass ``schema=data_dictionary``
to ``sdk.records.create``, ``sdk.records.create_batch``,
:class:`~imednet_workflows.record_update.RecordUpdateWorkflow`, or
:class:`~imednet_workflows.register_subjects.RegisterSubjectsWorkflow`. See
:doc:`/how-to/data_dictionary` and :doc:`/explanation/schema_validation` for
side-by-side details.

For lightweight type-check unit tests without a full Data Dictionary,
``imednet.testing.fake_data`` includes helpers to generate Forms, Variables, and
Records that work with ``SchemaCache.refresh`` offline.
