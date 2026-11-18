Project Standards
=================

This document summarizes the engineering standards that guide changes in the
repository. It is a contributor-facing companion to ``AGENTS.md``.

Dependency policy
-----------------

Use the dependency stack already declared in ``pyproject.toml`` and ``uv.lock``.
The current source of truth includes:

- ``httpx`` for HTTP transport
- ``pydantic`` v2 for models and validation
- ``argparse`` for the CLI
- ``tenacity`` for retry behavior
- ``respx`` for HTTP mocking in tests
- ``mypy`` for type checking
- ``ruff`` for linting and formatting

Do not introduce new runtime or development dependencies without explicit approval.

Verification loop
-----------------

Before proposing or merging changes, run the same checks enforced by repository
policy:

.. code-block:: bash

   hatch run lint:all
   hatch run test
   hatch run docs

Changes are not complete until these checks pass.

Architecture boundaries
-----------------------

The repository is split into clear layers and responsibilities.

Data models
^^^^^^^^^^^

Define schemas in ``packages/core/src/imednet/models/`` using Pydantic v2 models.
Keep these modules focused on deserialization and stable model definitions.

Application layer
^^^^^^^^^^^^^^^^^

- ``packages/core/src/imednet/http/`` handles transport concerns such as retries,
  timeouts, rate limits, and status-code-to-error mapping.
- ``packages/core/src/imednet/auth/`` manages credentials. Never log or expose
  secrets.
- ``packages/core/src/imednet/errors/`` defines the typed exception hierarchy.
- ``packages/core/src/imednet/pagination/`` contains lazy and bounded-memory
  iteration helpers.
- ``packages/core/src/imednet/utils/`` contains pure helpers with no network I/O.
- ``packages/plugins-workflows/src/imednet_workflows/`` contains orchestration,
  batching, and transforms on top of injected clients.

Presentation layer
^^^^^^^^^^^^^^^^^^

CLI modules under ``packages/core/src/imednet/cli/`` should handle argument
parsing, terminal output, and exit behavior only. Business logic and HTTP
behavior belong in the SDK layers.

Credential handling
-------------------

- Never log, print, persist, or expose API keys, security keys, tokens, or
  authorization headers in plaintext.
- Mask credential values in logs, exceptions, and debug output.
- Surface authentication and transport failures with typed errors instead of
  silent fallbacks.

Testing standards
-----------------

- Update or add ``tests/unit/`` coverage for new or modified behavior.
- Do not add live network access to default test runs.
- Use ``respx`` for ``httpx`` traffic and strict routers when testing transport
  behavior.
- Keep total coverage at or above 90%.

Docs standards
--------------

- Update public API or CLI documentation with any public-facing change.
- Keep docstrings in Google style so Sphinx and Napoleon render them correctly.
- Use generated API docs for module references and reserve hand-written docs for
  guides, architecture notes, and tutorials.
- Treat documentation build warnings as errors by keeping ``hatch run docs`` clean.

PR and release standards
------------------------

- PR titles must use Conventional Commits: ``feat:``, ``fix:``, ``chore:``,
  ``docs:``, ``ci:``, ``test:``, ``refactor:``, ``perf:``, or ``revert:``.
- Merge to ``main`` using **Squash and merge**.
- Do not manually edit package versions in ``packages/*/pyproject.toml``.
- Public API, CLI, or environment variable changes should be called out clearly
  in the PR description.

Issue metadata expectations
---------------------------

To keep execution aligned with these standards, issues should identify:

- affected package or component
- API surface impact: internal, public SDK, CLI, or docs-only
- compatibility impact: patch, minor, or major
- required verification steps

This keeps triage, implementation, and review aligned from intake through merge.
