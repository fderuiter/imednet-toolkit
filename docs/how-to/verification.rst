=================
Verification Loop
=================

Before proposing any solution, execute and pass all CI quality gates locally.

1. Read ``.github/workflows/main.yml`` (the ``quality`` and ``pre-commit`` jobs) to identify the authoritative lint, format, and type-check commands.
2. Run the full gate in order (contributors must execute ``uvx pre-commit run --all-files`` before opening pull requests):

   .. code-block:: bash

      uvx pre-commit run --all-files
      hatch run ruff format --check .
      hatch run ruff check .
      hatch run mypy packages/core/src/imednet
      hatch run mypy packages/plugins-workflows/src/imednet_workflows
      hatch run mypy packages/providers-airflow/src/apache_airflow_providers_imednet
      hatch run pytest -q \
        --cov=imednet \
        --cov=imednet_workflows \
        --cov=apache_airflow_providers_imednet \
        --cov-fail-under=90

3. Fix every reported error before marking a task complete. Re-run until the entire sequence exits 0.
4. Build documentation and confirm zero warnings:

   .. code-block:: bash

      hatch run docs

Canonical tasks
---------------

``pyproject.toml`` defines ``hatch run verify-static`` (format, lint and the
three typing targets), ``hatch run verify-tests`` (offline workspace tests and
90% coverage), and ``hatch run docs`` (strict documentation checks).
``hatch run verify`` runs these same tasks in order; the Pipeline quality job
invokes it directly. The expanded commands above are useful for diagnostics.

Install with ``uv sync --locked --extra dev --extra docs`` or the setup wizard;
no real credentials or live EDC access are required. Run pre-commit first and
``uvx hatch run quality:all packages`` as well for the existing security, dead-code,
spelling and similarity gates. The fast global Ruff check is an early subset;
package/Python matrix jobs supplement the full workspace gate. Duplicate global
similarity execution is removed; the canonical ``lint:pylint-sim`` and
``duplication:check`` tasks retain their documented scopes and thresholds.

Fuzzing/performance tracks require their pytest flags; container integration
requires ``IMEDNET_TEST_CONTAINERS=1``. Live tests remain outside the default
suite and require explicit authorization and the live-test charter. Security
exception ownership and review dates are in :doc:`/reference/security_exceptions`.

With uv, run the expanded commands above via ``uv run`` and build documentation
with ``uv run python scripts/build_docs.py``. The Hatch and uv routes use the
same repository configuration and installed package sources; do not skip gates
when using a different environment manager.
