Configuration
=============

The SDK and CLI read settings from environment variables. They can be set in the
shell or stored in a ``.env`` file that the CLI loads automatically via
:func:`dotenv.load_dotenv`.

Environment Variables
---------------------

.. list-table::
   :header-rows: 1
   :widths: 25 50 15

   * - Variable
     - Description
     - Default
   * - IMEDNET_API_KEY
     - API key used for authentication.
     - None
   * - IMEDNET_SECURITY_KEY
     - Security key used for authentication (also used as fallback key material for ``ConfigVersionStore`` when ``IMEDNET_ENCRYPTION_KEY`` is unset).
     - None
   * - IMEDNET_OIDC_TOKEN
     - Optional OIDC bearer token used instead of API/security key pairs.
     - None
   * - IMEDNET_OIDC_PUBLIC_KEY
     - Optional PEM public key or shared secret used by ``OIDCAuth`` to verify JWT signatures locally.
     - None
   * - IMEDNET_OIDC_JWKS_URL
     - Optional JWKS endpoint URL queried by ``OIDCAuth`` to resolve signing keys dynamically.
     - None
   * - IMEDNET_OIDC_AUDIENCE
     - Optional expected ``aud`` claim enforced by ``OIDCAuth`` during JWT validation.
     - None
   * - IMEDNET_OIDC_ALGORITHMS
     - Optional comma-separated allowlist of JWS signing algorithms (for example ``RS256,ES256``) accepted by ``OIDCAuth``.
     - None (key-matched defaults)
   * - IMEDNET_OIDC_ISSUER
     - Optional expected ``iss`` claim enforced by ``OIDCAuth`` during JWT validation.
     - None
   * - IMEDNET_BASE_URL
     - Optional base URL override for private or regional deployments (client defaults to ``https://edc.prod.imednetapi.com`` when unset).
     - None
   * - IMEDNET_TIMEOUT
     - Default HTTP request timeout in seconds loaded by :func:`imednet.config.load_config`.
     - ``30.0``
   * - IMEDNET_SIMULATION_MODE
     - Set to ``true`` / ``1`` (or pass ``--simulation``) to run offline against ``SimulationTransport``.
     - ``false``
   * - IMEDNET_STRICT_MODE
     - Set to ``true`` / ``1`` to raise validation errors on unexpected upstream response drift or untyped scalar coercion.
     - ``false``
   * - IMEDNET_STRICT_CDASH
     - Set to ``true`` / ``1`` to enable opt-in CDASH Variable naming/prefix heuristics on ``MappingRule`` and ``StudyConfiguration``.
     - ``false``
   * - IMEDNET_NON_INTERACTIVE
     - Set to ``1`` / ``true`` / ``yes`` (or pass ``--no-interactive``) to disable full-screen curses grids, ``imednet tui``, live ``stderr`` spinners, and interactive ``stdin`` prompts.
     - ``false``
   * - IMEDNET_CLI_OUTPUT_FORMAT
     - Set to ``json`` to default ``--format``-capable CLI commands and ``datadict diff`` to JSON output unless an explicit ``--format`` flag overrides it.
     - ``table``
   * - IMEDNET_HIGH_CONTRAST
     - Set to ``1`` (or pass ``--high-contrast``) to enable high-contrast palettes across CLI tables, ``InteractiveDataGrid``, and ``imednet tui``.
     - ``0`` (disabled)
   * - IMEDNET_OPENAPI_PATH
     - Optional filesystem path to an OpenAPI specification file overriding bundled contract models in ``imednet.models.engine``.
     - None
   * - IMEDNET_POSTMAN_PATH
     - Optional filesystem path to a Postman collection JSON file overriding bundled contract models when ``IMEDNET_OPENAPI_PATH`` is unset.
     - None
   * - IMEDNET_CONFIG_DB_PATH
     - SQLite database file path storing append-only ``StudyConfiguration`` history for ``ConfigVersionStore``.
     - ``~/.imednet/config_versions.sqlite3``
   * - IMEDNET_ENCRYPTION_KEY
     - Optional AES-256 Fernet key or passphrase encrypting ``ConfigVersionStore`` payloads (falls back to ``IMEDNET_SECURITY_KEY`` when unset).
     - None
   * - IMEDNET_WORKFLOW_LEDGER_PATH
     - Default JSON file path storing incremental high-water mark checkpoints for ``FileStateProvider`` / ``ExtractionStateLedger``.
     - ``~/.imednet/pipeline_ledger.json``
   * - IMEDNET_TRIAGE_DB_PATH
     - Default SQLite database file path storing ``Record`` triage items and history for ``TriageStore``.
     - ``~/.imednet/triage.sqlite3``
   * - IMEDNET_STUDY_KEY
     - Study identifier used by examples and some tests.
     - None
   * - IMEDNET_RUN_E2E
     - Set to ``1`` to enable end-to-end tests that hit a live environment.
     - None
   * - IMEDNET_BATCH_ID
     - Batch identifier used by job polling tests. Created automatically if unset.
     - None
   * - IMEDNET_FORM_KEY
     - Form key for record-creation tests. If unset, the first form is used.
     - None
   * - IMEDNET_ALLOW_MUTATION
     - Set to ``1`` to allow workflow tests that submit data.
     - None

Using a .env File
-----------------

You can create a ``.env`` file in the project root to store these variables.
A template is provided in ``.env.example``::

    cp .env.example .env

Edit the file to add your keys::

    IMEDNET_API_KEY=your_api_key
    IMEDNET_SECURITY_KEY=your_security_key
    IMEDNET_BASE_URL=https://edc.prod.imednetapi.com

The CLI loads this file automatically. Other scripts can call
``dotenv.load_dotenv()`` to mimic this behaviour.

Alternatively, run the interactive setup wizard to configure your ``.env`` file:

.. code-block:: bash

    bash scripts/wizards/setup-imednet-env.sh


Parsing Values
--------------

When reading environment variables or other untyped values, use helpers from
:mod:`imednet.utils.validators` such as
:func:`imednet.utils.validators.parse_bool`,
:func:`imednet.utils.validators.parse_datetime`,
:func:`imednet.utils.validators.parse_int_or_default`,
:func:`imednet.utils.validators.parse_str_or_default`,
:func:`imednet.utils.validators.parse_list_or_default`, and
:func:`imednet.utils.validators.parse_dict_or_default`.

.. testcode::

   from imednet.utils.validators import (
       parse_bool,
       parse_datetime,
       parse_int_or_default,
       parse_str_or_default,
       parse_list_or_default,
       parse_dict_or_default,
   )
   print(parse_bool("yes"))
   print(parse_datetime("2020-01-01T00:00:00Z").year)
   print(parse_int_or_default("bad", default=5))
   print(repr(parse_str_or_default(1.23)))
   print(parse_list_or_default("x"))
   print(parse_dict_or_default(None))

.. testoutput::

   True
   2020
   5
   '1.23'
   ['x']
   {}

Troubleshooting
---------------

**Missing required environment variable(s)**

If you see an error like ``Error: IMEDNET_API_KEY and IMEDNET_SECURITY_KEY environment variables must be set.`` (or ``Error: IMEDNET_SECURITY_KEY environment variable must be set`` / ``Error: IMEDNET_API_KEY environment variable must be set`` when only one key of the pair is configured) or an "Unauthorized" API error, ensure you have set these variables (or ``IMEDNET_OIDC_TOKEN``) in your shell or in a ``.env`` file in the directory where you run the script, or pass ``--simulation`` to run against offline synthetic data. See `Using a .env File`_ for details.

Mapping-name validation
------------------------

``IMEDNET_STRICT_CDASH`` enables the opt-in local mapping-name/prefix heuristic
for MappingRule and StudyConfiguration. It defaults to false and is independent
of SDK response-drift ``IMEDNET_STRICT_MODE``. Per-configuration ``strictCdash``
and its local ``strict``/``strictMode`` flags also enable this heuristic. These
checks do not establish complete CDISC conformance; see
:doc:`/how-to/record_mapping` for transformation and evidence boundaries.
