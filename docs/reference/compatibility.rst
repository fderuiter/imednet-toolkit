Package compatibility and release policy
========================================

The four distributions ship independently. Release-please owns their versions,
package tags and release notes. A workspace checkout is a development environment,
not an installable substitute for any published extension. Current 1.x contracts
use semantic versioning even while the core classifier remains Beta.

.. list-table:: Supported combinations
   :header-rows: 1

   * - Distribution
     - Python / core
     - Dependency authority
   * - ``imednet``
     - 3.11, 3.12, 3.13 / 1.x
     - Pydantic 2, HTTPX 0.28, Tenacity 9 within manifest bounds
   * - ``imednet-workflows``
     - 3.11, 3.12, 3.13 / >=1.0,<2
     - Pandas 2, filelock >=4.2.1,<5, cryptography per manifest
   * - ``imednet-plugins-sinks``
     - 3.11, 3.12, 3.13 / >=1.0,<2
     - Optional destination drivers per selected extra
   * - ``apache-airflow-providers-imednet``
     - 3.11, 3.12, 3.13 / >=1.0,<2
     - Airflow >=3.2,<4; additionally obey Airflow's Python constraints

Newer Python versions allowed by metadata are not claimed tested until added to
the CI matrix. Supported combinations are the intersection of this table and all
installed dependency requirements. ``uv.lock`` records the current development
resolution; it is not a lower-bound certification. The Python matrix runs current
resolved dependencies; minimum dependency combinations still require independent
verification before being advertised. Do not override vendor bounds to force a
combination. Retire a Python line or raise required dependency floors in a major
release, with a documented migration and security exception approved by the maintainer.

Install core with ``pip install imednet``. Use ``imednet[cli]`` for optional dotenv
and logging helpers, ``imednet[export]`` for tabular workflows, or
``imednet[mongodb]``, ``imednet[neo4j]``, ``imednet[snowflake]`` for sinks.
Install the Airflow distribution separately. Extras are dependency bundles, not
separate versions or guarantees that external services are configured.

Public surfaces and deprecation
-------------------------------

Documented SDK constructors, endpoint methods, SPI namespaces, CLI commands,
flags, exit codes, configuration variables and versioned Data Dictionary schemas
are compatibility surfaces. Preserve them throughout a major line. Introduce a
replacement first, emit a documented warning where practical, retain the old
surface for at least one minor release and remove it only in the next major.
Include before/after examples, exception/return semantics and a migration note.
New optional capabilities must preserve old defaults. Private underscore members
are not extension contracts; use the SPI. Response schema additions must not be
silently presented as complete vendor schema coverage.

The earlier ``ctx`` constructor removal in PR #1670 has already merged. It is a
known compatibility deviation, not a precedent for removing further parameters.
Use ``sdk.study_context(study_key)`` for scoped study selection; context tokens
are restored on exit. Track restoration or a versioned migration before advertising
compatibility to historical callers. Sync and async SDKs have distinct lifecycles;
use ``with`` and ``async with`` respectively.

Release acceptance checklist
----------------------------

The repository maintainer owns acceptance and any documented exception. Before
merging a release PR, review each package's changelog, dependency bounds, install
recipes and compatibility notes; run the verification and package-consumption
gates. Preserve OIDC trusted publishing, pinned Actions and existing permissions.
Release-please remains the sole version authority; never manually change versions
or publish from a feature PR.

For each tagged package, record tag/commit, distribution name/version, exact
wheel and sdist SHA-256, checks run and provenance/attestation verification. Test
those exact files in clean environments outside the source checkout, inspect
``py.typed`` and resources, and validate rebuilt sdist behavior. The current
package-consumption harness is supporting evidence; it does not prove exact
release-artifact fan-in. Issue #1678 owns that remaining enforcement. Rebuilding
a different wheel is not acceptance of the published bytes. A missing artifact,
failed check or unverifiable provenance blocks acceptance unless the maintainer
records a specific, dated exception and remediation owner.
