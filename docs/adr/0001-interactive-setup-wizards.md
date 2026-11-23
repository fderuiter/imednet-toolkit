# Interactive Setup Wizards via Standalone Bash Scripts

**Status:** Accepted (CLI framework reference superseded by [ADR-0007](0007-cli-framework-and-output-contracts.md))

> **Supersession Note:** Standalone Bash setup wizards under `scripts/wizards/` remain accepted. The historical reference below to a "Typer-based CLI" is **superseded by [ADR-0007](0007-cli-framework-and-output-contracts.md)**: `imednet-toolkit` standardizes its CLI on Python's standard-library `argparse` (`imednet.cli`) and shared `imednet.spi.cli` contracts without a `typer` runtime dependency.

## Context and Decision

We adopt standalone, self-contained Bash scripts under `scripts/wizards/` for interactive developer onboarding and third-party provisioning (PyPI Trusted Publishing, iMedNet EDC credentials, CI live-testing environment). While `imednet-toolkit` is primarily a Python package with a command-line interface *(originally referred to here as a Typer-based CLI; superseded by `argparse` in ADR-0007)*, setup wizards must operate across external browser dashboards, write initial `.env` files, and configure GitHub secrets *before* Python virtualenvs or project dependencies are installed, without requiring runtime bootstrapping.
