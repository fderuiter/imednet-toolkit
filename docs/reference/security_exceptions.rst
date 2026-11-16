Security audit policy
=====================

Run ``uvx hatch run lint:audit`` for the audit used by the CI quality gate.
The scanner environment includes workspace packages and security tools.
Filelock must resolve to version 4.2.1 or newer; no filelock advisory is ignored.
Workspace development and documentation require Python >=3.11.5 so patched
Soup Sieve releases can install. Published package Python metadata is unchanged.

Patched workspace dependencies
------------------------------

The workspace's ``tool.uv.constraint-dependencies`` prevents vulnerable fresh
resolutions as well as protecting the checked-in ``uv.lock``. These constraints
do not add runtime dependencies to the published SDK. The OpenTelemetry API,
SDK and exporters resolve together using their upstream requirements.

.. list-table:: November 16, 2026 remediation
   :header-rows: 1
   :widths: 18 20 20 42

   * - Dependabot alerts
     - Dependency / locked version
     - Security floor
     - Dependency path
   * - 137, 138, 139, 140
     - Soup Sieve 4.1.1
     - >=4.1.1
     - Furo → Beautiful Soup → Soup Sieve (documentation)
   * - 116
     - Mako 1.4.3
     - >=1.4.2
     - Airflow → Alembic → Mako
   * - 122, 141
     - OpenTelemetry API 1.45.2
     - >=1.45.2
     - Airflow and its telemetry SDK/exporters

Soup Sieve 4.0 fixes the reported descendant-combinator issue, but later
selector denial-of-service advisories also affect 4.0.1 and 4.1. Use 4.1.1 or
newer, retaining the upstream default selector protections. Mako 1.4.2 fixes
Windows drive-letter traversal. OpenTelemetry API 1.45.1 fixes tracestate parsing;
1.45.2 also fixes traceparent parsing.

Audit the lock independently of the scanner environment, including every
workspace member and optional extra, with no ignored advisories:

.. code-block:: bash

   uv lock --check
   uv export --locked --all-packages --all-extras --no-emit-workspace \
     --no-hashes --format requirements-txt --output-file /tmp/imednet-locked.txt
   uvx pip-audit -s osv --timeout 60 --disable-pip --no-deps \
     -r /tmp/imednet-locked.txt

The export contains exact external pins and platform markers; workspace source
packages are omitted because they are not registry dependencies. ``--no-deps``
audits the already resolved graph without re-resolving it. The auditor evaluates
markers for the current host; inspect the universal lock and run supported
platform checks as well. Unit regressions check every locked copy of the three
affected packages, including copies for other platforms.
The Pipeline quality job runs this same independent audit before the detached
scanner gate. It cannot inherit the scanner-only advisory exceptions.

Maintainer advisories:

* `Soup Sieve descendant combinator <https://github.com/facelessuser/soupsieve/security/advisories/GHSA-mmhr-3c5m-f64f>`_
* `Soup Sieve selector compilation <https://github.com/facelessuser/soupsieve/security/advisories/GHSA-6655-6qjp-jx7x>`_
* `Soup Sieve parser recursion <https://github.com/facelessuser/soupsieve/security/advisories/GHSA-7g44-g9rc-x53w>`_
* `Soup Sieve sibling matching <https://github.com/facelessuser/soupsieve/security/advisories/GHSA-4j63-6c4x-wfg4>`_
* `Soup Sieve attribute matching follow-up <https://github.com/facelessuser/soupsieve/security/advisories/GHSA-f3mv-fm3p-593v>`_
* `Soup Sieve nested matching follow-up <https://github.com/facelessuser/soupsieve/security/advisories/GHSA-98qm-p6jx-5wj2>`_
* `Mako drive-letter traversal <https://github.com/sqlalchemy/mako/security/advisories/GHSA-5639-2j2p-m4mx>`_
* `OpenTelemetry tracestate parsing <https://github.com/open-telemetry/opentelemetry-python/security/advisories/GHSA-xcq7-p3pf-p88h>`_
* `OpenTelemetry traceparent parsing <https://github.com/open-telemetry/opentelemetry-python/security/advisories/GHSA-mf5v-w3w3-f6cg>`_

Scanner-only upstream exceptions
--------------------------------

**Owner:** repository maintainer (``fderuiter``).
**Last verified:** November 16, 2026.
**Next review:** November 28, 2026, or the next Semgrep release, whichever is earlier.

Semgrep 1.185.0 pins ``mcp==1.29.0`` and the OpenTelemetry 1.37 family. These
published constraints prevent installing the fixed versions below without
overriding the vendor's requirements. These dependencies belong to the security
scanner, not the shipped SDK or workflow packages.
The patched OpenTelemetry family in ``uv.lock`` is a separate resolution;
the exceptions below do not apply to its independent audit. The latest published
Semgrep release remains 1.185.0 as of the verification date.
The detached ``lint`` environment sets ``UV_NO_CONFIG=true`` so its uv installer
does not apply the incompatible workspace constraints to Semgrep's vendor pins.
This setting is scoped to that environment; ordinary workspace resolution and
the independent exported-lock audit continue to enforce the patched floors.

.. list-table:: Existing upstream constraints
   :header-rows: 1
   :widths: 30 45 25

   * - Scanner dependency
     - Advisory IDs
     - Reported fixed versions
   * - ``mcp==1.29.0``
     - CVE-2026-116684, CVE-2026-116685, CVE-2026-116686
     - 1.30.0
   * - ``mcp==1.29.0``
     - CVE-2026-59951
     - 2.2.0
   * - ``opentelemetry-api==1.37.0``
     - CVE-2026-114603, CVE-2026-126360
     - 1.45.1, 1.45.2 respectively

The six corresponding audit exceptions apply only while these constraints
remain. Verification invokes ``semgrep scan`` with metrics disabled; it does
not start an MCP server or enable affected server features. This limits exposure
and does not claim the vulnerable dependencies are fixed. Do not use the tooling
environment to host services or deploy application code. An audit success means
no findings outside the documented exceptions, not absence of all advisories.

Upgrade Semgrep when supported constraints admit the patches, remove the
matching exceptions, and rerun the complete quality gate. Do not force incompatible
transitive versions or add blanket exceptions to conceal findings.

Upstream evidence:

* `Semgrep dependencies <https://github.com/semgrep/semgrep/blob/v1.185.0/cli/pyproject.toml>`_
* `Semgrep MCP update tracking <https://github.com/semgrep/semgrep/issues/11992>`_
* `OpenTelemetry releases <https://github.com/open-telemetry/opentelemetry-python/blob/main/CHANGELOG.md>`_
* `Filelock fixes <https://github.com/tox-dev/filelock/releases/tag/4.2.1>`_
