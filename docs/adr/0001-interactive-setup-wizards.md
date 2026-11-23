# Interactive Setup Wizards via Standalone Bash Scripts

> **Status:** Accepted (CLI framework detail superseded by [ADR 0007](0007-cli-framework-and-output-contracts.md), which standardized the shipped `imednet` CLI and `imednet.cli_plugins` entry points on standard-library `argparse` rather than Typer).

We adopt standalone, self-contained Bash scripts under `scripts/wizards/` for interactive developer onboarding and third-party provisioning (PyPI Trusted Publishing, iMednet EDC credentials, CI live-testing environment). While `imednet-toolkit` is primarily a Python package with a CLI (originally referenced as Typer-based when drafted, now implemented with standard-library `argparse` per ADR 0007), setup wizards must operate across external browser dashboards, write initial `.env` files, and configure GitHub secrets *before* Python virtualenvs or project dependencies are installed, without requiring runtime bootstrapping.
