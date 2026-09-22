===========================
Agent and Contributor Rules
===========================

Stack Identification
--------------------

Before executing any task, audit the active dependency stack. Do not assume the presence of any library.

1. Parse ``pyproject.toml`` (``[project.dependencies]`` and ``[project.optional-dependencies]``) and ``uv.lock`` to identify the exact versions in use.
2. Use only libraries already declared in those files. Never introduce a conflicting or unlisted package without an explicit instruction to update dependencies.
3. Apply the following decision matrix when selecting implementation patterns:

.. list-table::
   :header-rows: 1

   * - Concern
     - Source of truth
     - Current library
   * - HTTP client
     - ``pyproject.toml``
     - ``httpx``
   * - Data validation / models
     - ``pyproject.toml``
     - ``pydantic`` v2
   * - CLI framework
     - ``pyproject.toml``
     - ``typer[all]``
   * - Retry logic
     - ``pyproject.toml``
     - ``tenacity``
   * - HTTP mocking in tests
     - ``pyproject.toml`` (dev)
     - ``respx``
   * - Type checking
     - ``pyproject.toml`` (dev)
     - ``mypy``
   * - Linting
     - ``pyproject.toml`` (dev)
     - ``ruff``
   * - Formatting
     - ``pyproject.toml`` (dev)
     - ``ruff``


Immutable Constraints
---------------------

Credential Handling
^^^^^^^^^^^^^^^^^^^
- **Never** log, print, write to disk, or transmit API keys, security keys, tokens, or authorization headers in plaintext under any circumstance.
- Mask credential values in all log output and exception messages.

Testing Boundaries
^^^^^^^^^^^^^^^^^^
.. list-table::
   :header-rows: 1

   * - Test location
     - Network access
     - Required mock library
   * - ``tests/unit/``
     - Forbidden — no live requests
     - ``respx`` for ``httpx``, ``responses`` for ``requests``
   * - ``tests/integration/``
     - Forbidden — use recorded fixtures
     - ``respx`` / ``responses``
   * - ``tests/live/``
     - Permitted — real API calls
     - None (guarded by ``IMEDNET_RUN_E2E`` env var)


- All new or modified code must include or update tests in ``tests/unit/``.
- Coverage must remain ≥ 90%.
- Do not add ``tests/live/`` calls to the default ``pytest`` run.

Commit Messages and Pull Requests
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
- PR titles must follow Conventional Commits. Permitted prefixes: ``feat:``, ``fix:``, ``chore:``, ``docs:``, ``ci:``, ``test:``, ``refactor:``, ``perf:``, ``revert:``. The ``Semantic PR Title`` CI check enforces this.
- Security updates and CVE remediations must use the ``fix:`` prefix (e.g. ``fix(deps):`` or ``fix(security):``), which directly maps to patch releases in ``release-please``. Do not use unpermitted prefixes such as ``security:``.
- Merge to ``main`` via **Squash and merge** so the PR title becomes the commit message.
- Changes that affect the public API, CLI interface, or environment variables must be noted in the PR description for Render deployment review.

Release Process
^^^^^^^^^^^^^^^
- Do **not** manually edit package versions in ``packages/*/pyproject.toml``. Versions are managed automatically by ``release-please``.
- Releases are fully automated. After merging a Release PR, ``release-please`` creates a package-specific Git tag and the ``Pipeline`` workflow publishes the tagged package to PyPI.
