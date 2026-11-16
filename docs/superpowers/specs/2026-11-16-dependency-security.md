# Transitive dependency security remediation

Remediate Dependabot alerts 116, 122, 137, 138, 139, 140 and 141 in the
workspace lockfile. The affected paths are Furo → Beautiful Soup → Soup Sieve,
Airflow → Alembic → Mako, and Airflow → OpenTelemetry. Use supported upstream
releases and preserve the OpenTelemetry family's declared compatibility.

The workspace must reject vulnerable resolutions through uv constraints, without
adding these transitive packages to the SDK's runtime requirements. Soup Sieve's
patched release requires Python >=3.11.5; apply that patch minimum to the workspace
only. Published distributions retain their existing Python metadata and versions.

Acceptance: every locked copy of Soup Sieve >=4.1.1, Mako >=1.4.2 and
OpenTelemetry API >=1.45.2; locked synchronization, dependency consistency, an
unignored audit of exported lock requirements, offline tests with >=90% coverage,
strict docs and the repository's full quality gate pass. Regression checks must
reject affected versions and allow later patched releases.

The detached Semgrep environment is outside uv.lock. As of November 16, 2026,
Semgrep 1.185.0 still requires OpenTelemetry ~=1.37.0 and MCP ==1.29.0. Preserve
its existing narrowly scoped exceptions while these vendor bounds remain; do not
claim they are repaired by the workspace update or add any new exceptions.
Document the difference and verify it against the published package metadata.

Open a new `fix(deps):` PR, with the seven-alert mapping, verification results and
scanner limitation. Do not merge it or dismiss alerts. GitHub closes alerts only
after the fix reaches the default branch and is rescanned.
