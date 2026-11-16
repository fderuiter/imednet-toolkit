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
   * - IMEDNET_BASE_URL
     - Optional base URL for private deployments.
     - None
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

If you see an error like ``Error: IMEDNET_API_KEY and IMEDNET_SECURITY_KEY environment variables must be set.`` (CLI) or ``API key and security key are required`` (SDK), or an "Unauthorized" API error, ensure you have set these variables in your shell or in a ``.env`` file in the directory where you run the script. See `Using a .env File`_ for details.

Mapping-name validation
------------------------

``IMEDNET_STRICT_CDASH`` enables the opt-in local mapping-name/prefix heuristic
for MappingRule and StudyConfiguration. It defaults to false and is independent
of SDK response-drift ``IMEDNET_STRICT_MODE``. Per-configuration ``strictCdash``
and its local ``strict``/``strictMode`` flags also enable this heuristic. These
checks do not establish complete CDISC conformance; see
:doc:`/how-to/record_mapping` for transformation and evidence boundaries.
