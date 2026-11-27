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
     - Security key used for authentication.
     - None
   * - IMEDNET_OIDC_TOKEN
     - Optional OIDC bearer token used instead of API/security key pairs (takes precedence over static keys).
     - None
   * - IMEDNET_OIDC_PUBLIC_KEY
     - Optional PEM public key (or symmetric key when a symmetric algorithm is explicitly configured) used by ``OIDCAuth`` to verify JWT signatures.
     - None
   * - IMEDNET_OIDC_JWKS_URL
     - Optional HTTPS JWKS endpoint URL used by ``OIDCAuth`` to fetch asymmetric verification keys.
     - None
   * - IMEDNET_OIDC_AUDIENCE
     - Optional expected JWT ``aud`` claim enforced by ``OIDCAuth``.
     - None
   * - IMEDNET_OIDC_ALGORITHMS
     - Optional comma-separated list of allowed JWS signing algorithms enforced by ``OIDCAuth`` (defaults to asymmetric ``RS*``, ``PS*``, ``ES*``, and ``EdDSA`` algorithms).
     - None
   * - IMEDNET_OIDC_ISSUER
     - Optional expected JWT ``iss`` claim enforced by ``OIDCAuth``.
     - None
   * - IMEDNET_SIMULATION_MODE
     - Set to ``true`` / ``1`` (or pass ``--simulation``) to run offline against ``SimulationTransport``.
     - ``false``
   * - IMEDNET_BASE_URL
     - Optional base URL for private Mednet EDC deployments (also scopes dynamic ``ConfigVersionStore`` host directories).
     - ``https://edc.prod.imednetapi.com``
   * - IMEDNET_TIMEOUT
     - HTTP request timeout in seconds for synchronous and asynchronous SDK clients.
     - ``30.0``
   * - IMEDNET_STRICT_MODE
     - Enable strict SDK response-drift validation when parsing API payloads and untyped primitives.
     - ``false``
   * - IMEDNET_STRICT_CDASH
     - Enable the opt-in local CDASH/SDTM naming-prefix heuristic on ``MappingRule`` and ``StudyConfiguration``.
     - ``false``
   * - IMEDNET_CONFIG_ENCRYPTION_KEY
     - Optional dedicated Fernet key or passphrase used by ``ConfigVersionStore`` to encrypt and decrypt ``StudyConfiguration`` payloads at rest (takes precedence over ``IMEDNET_ENCRYPTION_KEY`` and ``IMEDNET_SECURITY_KEY``).
     - None
   * - IMEDNET_ENCRYPTION_KEY
     - Fernet key or passphrase used by ``ConfigVersionStore`` to encrypt and decrypt ``StudyConfiguration`` payloads at rest (falls back to ``IMEDNET_SECURITY_KEY``).
     - None
   * - IMEDNET_CONFIG_DB_PATH
     - Base SQLite ledger path for ``ConfigVersionStore`` prior to dynamic host and ``Study`` directory scoping.
     - ``~/.imednet/config_versions.sqlite3``
   * - IMEDNET_WORKFLOW_LEDGER_PATH
     - Filesystem path for ``FileStateProvider`` / ``ExtractionStateLedger`` incremental high-water mark checkpoints.
     - ``.imednet_workflow_state.json``
   * - IMEDNET_TRIAGE_DB_PATH
     - SQLite database path for ``TriageStore`` operational ``Query`` triage state in ``imednet-workflows``.
     - ``~/.imednet/triage.sqlite3``
   * - IMEDNET_CLI_OUTPUT_FORMAT
     - Default output format (``table``, ``json``, or ``csv``) for ``imednet`` CLI commands when ``--format`` is omitted.
     - ``table``
   * - IMEDNET_HIGH_CONTRAST
     - Set to ``1`` / ``true`` to enable WCAG AAA high-contrast rendering in CLI tables and the ``imednet-tui`` terminal dashboard.
     - ``false``
   * - IMEDNET_NON_INTERACTIVE
     - Set to ``1`` / ``true`` to suppress interactive prompts and spinners in automated CI/CD shells.
     - ``false``
   * - IMEDNET_OPENAPI_PATH
     - Optional filesystem path to an OpenAPI contract JSON override loaded by ``ModelEngine``.
     - None
   * - IMEDNET_POSTMAN_PATH
     - Optional filesystem path to a Postman collection JSON override loaded by ``ModelEngine``.
     - None
   * - IMEDNET_STUDY_KEY
     - ``Study`` identifier used by CLI defaults, examples, and integration tests.
     - None
   * - IMEDNET_RUN_E2E
     - Set to ``1`` to enable end-to-end tests that hit a live environment.
     - None
   * - IMEDNET_BATCH_ID
     - Batch identifier used by ``Job`` polling tests. Created automatically if unset.
     - None
   * - IMEDNET_FORM_KEY
     - ``Form`` key for ``Record``-creation tests. If unset, the first ``Form`` is used.
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
