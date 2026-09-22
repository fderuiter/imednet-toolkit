=================
Verification Loop
=================

Before proposing any solution, execute and pass all CI quality gates locally.

1. Read ``.github/workflows/main.yml`` (the ``quality`` job) to identify the authoritative lint, format, and type-check commands.
2. Run the full gate in order:

   .. code-block:: bash

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
   uv run ruff format --check .
   uv run ruff check .
   uv run mypy packages/core/src/imednet
   uv run mypy packages/plugins-workflows/src/imednet_workflows
   uv run mypy packages/providers-airflow/src/apache_airflow_providers_imednet
   uv run pytest -q --cov=imednet --cov=imednet_workflows --cov=apache_airflow_providers_imednet --cov-fail-under=90
   uv run python scripts/build_docs.py

   # Security vulnerability audit
   uv run --with pip-audit pip-audit -s osv --timeout 60 \
     --ignore-vuln PYSEC-2026-3481 --ignore-vuln CVE-2026-52870 --ignore-vuln GHSA-hvrp-rf83-w775 \
     --ignore-vuln PYSEC-2026-3482 --ignore-vuln GHSA-jpw9-pfvf-9f58 --ignore-vuln CVE-2026-52869 \
     --ignore-vuln PYSEC-2026-3483 --ignore-vuln GHSA-vj7q-gjh5-988w --ignore-vuln CVE-2026-59950
