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

Alternatively, if running via ``uv`` directly:

.. code-block:: bash

   # Ensure UTF-8 console output (especially on Windows)
   export PYTHONUTF8=1    # or in PowerShell: $env:PYTHONUTF8 = "1"

   # Initialize environment via wizard or manual sync
   bash scripts/wizards/setup-imednet-env.sh
   # OR:
   uv sync --extra dev --extra docs

   # Quality gate
   uvx pre-commit run --all-files
   uv run ruff format --check .
   uv run ruff check .
   uv run mypy packages/core/src/imednet
   uv run mypy packages/plugins-workflows/src/imednet_workflows
   uv run mypy packages/providers-airflow/src/apache_airflow_providers_imednet
   uv run pytest -q --cov=imednet --cov=imednet_workflows --cov=apache_airflow_providers_imednet --cov-fail-under=90
   uv run python scripts/build_docs.py

   # Security vulnerability audit (shared policy with CI)
   uvx hatch run lint:audit

The audit policy and scanner-only upstream exceptions are documented in
:doc:`/reference/security_exceptions`. No filelock advisories are suppressed.
