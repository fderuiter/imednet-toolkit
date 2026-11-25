=================
Verification Loop
=================

.. note::
   **Contributor Scope:** This guide is for SDK contributors and maintainers
   running pre-PR lint, static type-checking, and test gates locally. For
   clinical ``Study`` schema validation, pre-live UAT checks, or 21 CFR Part 11
   audit verification, see :doc:`data_dictionary`,
   :doc:`workflows/uat_workflow`, and :doc:`audit_compliance`.

Before proposing any code change, execute and pass all CI quality gates locally.

1. Read ``.github/workflows/main.yml`` (the ``quality`` and ``pre-commit`` jobs) to identify the authoritative lint, format, and type-check commands.
2. Run the full gate in order (contributors must execute ``uvx pre-commit run --all-files`` before opening pull requests):

   .. code-block:: bash

      uvx pre-commit run --all-files
      hatch run ruff format --check .
      hatch run ruff check .
      hatch run typos --force-exclude .
      hatch run mypy packages/core/src/imednet
      hatch run mypy packages/plugins-workflows/src/imednet_workflows
      hatch run mypy packages/providers-airflow/src/apache_airflow_providers_imednet
      hatch run mypy packages/plugins-sinks/src/imednet_sinks
      hatch run mypy packages/plugins-tui/src/imednet_tui
      hatch run mypy examples
      hatch run mypy tests/typing/endpoint_contracts.py
      hatch run pytest -q \
        --cov=imednet \
        --cov=imednet_workflows \
        --cov=apache_airflow_providers_imednet \
        --cov=imednet_sinks \
        --cov=imednet_tui \
        --cov-fail-under=90

3. Fix every reported error before marking a task complete. Re-run until the entire sequence exits 0.
4. Build documentation and confirm zero warnings:

   .. code-block:: bash

      hatch run docs

Canonical tasks
---------------

``pyproject.toml`` defines ``hatch run verify-static`` (format, lint,
non-mutating ``typos --force-exclude .`` spell checking, and the
typing targets), ``hatch run verify-tests`` (offline workspace tests and
90% coverage), ``hatch run docs`` (strict documentation checks and tightened
Pyodide browser wheel staging; pass ``--wheel-dir <dir>`` to reuse prebuilt
wheels), and ``hatch run validate-package-consumption`` (isolated clean-venv wheel,
PEP 561 ``py.typed`` / ``mypy --strict`` consumer typing, and
optional-dependency smoke checks across all workspace packages and 21 extras;
pass ``--wheel-dir <dir>`` to reuse prebuilt wheels). Set ``IMEDNET_TEST_WHEELS=1``
after installing built wheels into ``.venv`` to replicate CI wheel-mode ``pytest`` runs.
``hatch run verify`` runs ``verify-static`` and ``verify-tests`` in order; the
Pipeline ``docs`` job runs ``uv run python scripts/build_docs.py --wheel-dir "$RUNNER_TEMP/wheels"``
once on the synced ``.venv`` workspace (staging prebuilt tightened ``dist-*`` wheels
from ``build`` and falling back to ``scripts/tighten_dependencies.py`` rewrite/restore
on local runs or docs-only pull requests), and ``deploy-docs`` requires all ten upstream
jobs (including ``build``, ``validate-package-consumption``, ``integration-sandbox``,
and ``fuzzing`` across all five packages) to succeed on ``main``. The expanded commands
above are useful for diagnostics.

Install with ``uv sync --locked --all-packages --all-extras`` or the setup wizard;
no real credentials or live EDC access are required. Run pre-commit first and
``uvx hatch run quality:all`` as well for the full-workspace formatting, Ruff,
and non-mutating ``typos --force-exclude`` checks alongside the package-scoped
security, dead-code, and similarity gates. The ``global-lint`` job runs the
same pinned Ruff and non-mutating ``typos --force-exclude .`` checks across the
repository; package/Python matrix jobs supplement the full workspace gate. Duplicate global
similarity execution is removed; the canonical ``lint:pylint-sim`` and
``duplication:check`` tasks retain their documented scopes and thresholds.

Fuzzing and performance tracks require their pytest flags and write structured ``VerificationReport`` JSON files under gitignored ``reports/verification/`` (with mutable fuzz corpora under ``.cache/verification/``): run ``pytest tests/core/test_performance.py -m performance`` (pass ``--update-perf-baseline`` when intentionally refreshing ``tests/core/baselines/performance_baseline.json``), ``pytest tests/core/test_fuzz.py -m fuzzing`` (controlled by ``IMEDNET_FUZZ_RUNS``, defaulting to ``25000``), and ``python scripts/aggregate_verification.py --report-dir reports/verification --expect-track <track>`` (which exits ``1`` on missing tracks, malformed files, or ``status: "failed"``). Container integration
requires ``IMEDNET_TEST_CONTAINERS=1`` with ``docker compose up -d`` and
``docker compose down -v`` teardown (see ``CONTRIBUTING.md``). Live tests remain outside the default
suite and require explicit authorization and the live-test charter. Security
exception ownership and review dates are in :doc:`/reference/security_exceptions`.

With uv, run the expanded commands above via ``uv run`` and build documentation
with ``uv run python scripts/build_docs.py``. The Hatch and uv routes use the
same repository configuration and installed package sources; do not skip gates
when using a different environment manager.
