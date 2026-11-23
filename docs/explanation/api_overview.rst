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

Authentication and request header generation are owned by
:class:`~imednet.auth.strategy.AuthStrategy` implementations rather than
hardcoded client headers (see ADR 0005):

* :class:`~imednet.auth.api_key.ApiKeyAuth` – populates ``x-api-key`` and
  ``x-imn-security-key`` headers for static Mednet API/security key pairs.
* :class:`~imednet.auth.oidc.OIDCAuth` – verifies JWT signatures and claims
  (``exp``, ``nbf`` with optional ``leeway``, ``aud``, and ``iss``), emits
  ``Authorization: Bearer <token>``, and extracts ``sub`` /
  ``preferred_username`` user identifiers plus ``roles`` / ``groups`` RBAC
  claims (``admin``, ``manager``, ``reviewer``, ``viewer``). ``OIDCAuth``
  defaults to asymmetric JWS algorithms (``RS*``, ``PS*``, ``ES*``, ``EdDSA``),
  enforces strict key-type separation (rejecting ``alg: "none"`` and symmetric
  ``oct`` JWKs), and reads verification settings from
  ``IMEDNET_OIDC_PUBLIC_KEY``, ``IMEDNET_OIDC_JWKS_URL``,
  ``IMEDNET_OIDC_AUDIENCE``, ``IMEDNET_OIDC_ALGORITHMS``, and
  ``IMEDNET_OIDC_ISSUER``.
* **Custom** :class:`~imednet.auth.strategy.AuthStrategy` – passing an explicit
  ``auth=...`` strategy initializes :class:`~imednet.sdk.ImednetSDK`,
  :class:`~imednet.sdk.AsyncImednetSDK`, :class:`~imednet.core.client.Client`,
  and :class:`~imednet.core.async_client.AsyncClient` directly without requiring
  placeholder API or security keys. Supplying ``auth`` alongside an already
  constructed ``client`` or ``async_client`` raises ``ValueError`` because the
  injected client owns its transport headers.

When ``auth`` is not passed explicitly, :func:`imednet.config.load_config`
resolves each credential field in strict precedence order:

1. Explicit constructor or ``load_config(...)`` keyword arguments (explicit
   empty strings suppress fallback and fail validation with
   :class:`~imednet.errors.ConfigurationError`).
2. A caller-owned :class:`~imednet.auth.credentials.CredentialSource` snapshot
   such as :class:`~imednet.auth.credentials.MemoryCredentialStore` returning
   :class:`~imednet.auth.credentials.Credentials` in memory without touching
   filesystem state. Lookup errors fail closed as ``ConfigurationError`` without
   echoing raw secret text, and ``Config``, ``Credentials``,
   ``MemoryCredentialStore``, ``ApiKeyAuth``, and ``OIDCAuth`` redact secrets in
   ``repr()``.
3. Environment variables (``IMEDNET_OIDC_TOKEN``, ``IMEDNET_API_KEY``, and
   ``IMEDNET_SECURITY_KEY``).

Once resolved, ``oidc_token`` takes precedence over static ``api_key`` and
``security_key`` pairs. Updating or clearing a ``MemoryCredentialStore`` affects
subsequent client initializations without mutating live client instances. See
:doc:`/reference/configuration` for all authentication environment variables.

.. testcode::

   import jwt
   from cryptography.hazmat.primitives import serialization
   from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

   from imednet import ImednetSDK
   from imednet.auth import Credentials, MemoryCredentialStore, OIDCAuth
   from imednet.simulation import SimulationTransport

   signing_key = Ed25519PrivateKey.generate()
   public_pem = signing_key.public_key().public_bytes(
       encoding=serialization.Encoding.PEM,
       format=serialization.PublicFormat.SubjectPublicKeyInfo,
   )
   signed_jwt = jwt.encode(
       {
           "sub": "auditor@study.example",
           "iss": "https://idp.study.example",
           "aud": "imednet-edc",
           "roles": ["admin"],
       },
       signing_key,
       algorithm="EdDSA",
   )

   oidc_auth = OIDCAuth(
       signed_jwt,
       public_key=public_pem,
       issuer="https://idp.study.example",
       audience="imednet-edc",
   )
   assert oidc_auth.get_user_roles() == ["admin"]
   assert "Bearer " in oidc_auth.get_headers()["Authorization"]

   with ImednetSDK(auth=oidc_auth, transport=SimulationTransport()) as sdk:
       studies = list(sdk.studies.list())
       assert len(studies) >= 1

   cred_store = MemoryCredentialStore(
       Credentials(
           api_key="mem-api-key",  # pragma: allowlist secret
           security_key="mem-security-key",  # pragma: allowlist secret
       )
   )
   assert "<redacted>" in repr(cred_store)
   with ImednetSDK(credential_source=cred_store, transport=SimulationTransport()) as sdk:
       assert sdk.auth is not None

HTTP methods and errors
-----------------------

The API primarily supports ``GET`` and ``POST`` requests. Non‐successful status
codes raise typed exceptions:

* ``400`` – :class:`~imednet.errors.BadRequestError`
* ``401`` – :class:`~imednet.errors.UnauthorizedError`
* ``403`` – :class:`~imednet.errors.ForbiddenError`
* ``404`` – :class:`~imednet.errors.NotFoundError`
* ``409`` – :class:`~imednet.errors.ConflictError`
* ``429`` – :class:`~imednet.errors.RateLimitError`
* ``5xx`` – :class:`~imednet.errors.ServerError`

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
  Validation error. Request contain invalid value.

``9000``
  Unknown error. Please contact Mednet support for assistance.

``9001``
  Unauthorized error. Insufficient permission to retrieve data.

Integrations and analytical exports
-----------------------------------

For DuckDB table exports, Hive-partitioned Parquet layouts, and incremental
bronze/silver ingestion workflows, see :doc:`/how-to/duckdb_integration`.
