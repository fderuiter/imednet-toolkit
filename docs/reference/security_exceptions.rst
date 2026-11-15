Security audit policy
=====================

Run ``uvx hatch run lint:audit`` for the audit used by the CI quality gate.
The scanner environment includes workspace packages and security tools.
Filelock must resolve to version 4.2.1 or newer; no filelock advisory is ignored.
Python 3.11 or newer is required so patched releases can install.

Scanner-only upstream exceptions
--------------------------------

**Owner:** repository maintainer (``fderuiter``).
**Last verified:** November 14, 2026.
**Next review:** November 28, 2026, or the next Semgrep release, whichever is earlier.

Semgrep 1.185.0 pins ``mcp==1.29.0`` and the OpenTelemetry 1.37 family. These
published constraints prevent installing the fixed versions below without
overriding the vendor's requirements. These dependencies belong to the security
scanner, not the shipped SDK or workflow packages.

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
