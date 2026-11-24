# imednet-tui

Terminal User Interface (TUI) plugin package for the iMednet Python SDK.

## Overview

`imednet-tui` provides an interactive terminal user interface for exploring clinical studies, sites, subjects, and background jobs. It connects to the main `imednet` command-line interface via Python entry points (`imednet.cli_plugins`) without requiring terminal dependencies in headless runtime environments.

## Installation

### Source Checkout (Pre-Release Package & Extra)

`imednet-tui` and the `imednet[tui]` extra are not yet published on PyPI (awaiting the first `imednet-tui-v*` release tag). Install them from a repository source checkout:

```bash
git clone https://github.com/fderuiter/imednet-toolkit.git
cd imednet-toolkit
pip install -e ./packages/plugins-tui -e "./packages/core[tui]"
```

For full monorepo development with `uv`:

```bash
uv sync --locked --all-packages --all-extras
```

## Programmatic Initialization

Construct `ImednetTuiApp` with an `ImednetSDK` client in offline simulation mode or with `api_key` and `security_key` credentials:

```python
import os

from imednet import ImednetSDK
from imednet_tui.app import ImednetTuiApp

# Offline simulation mode
sim_sdk = ImednetSDK(simulation_mode=True)
sim_app = ImednetTuiApp(sim_sdk, study_key="STUDY")
sim_sdk.close()

# Authenticated mode (requires both api_key and security_key)
live_sdk = ImednetSDK(
    api_key=os.environ["IMEDNET_API_KEY"],
    security_key=os.environ["IMEDNET_SECURITY_KEY"],
)
live_app = ImednetTuiApp(live_sdk, study_key="STUDY", high_contrast=True)
live_sdk.close()
```

## CLI Usage

Launch the terminal interface using the `imednet` CLI:

```bash
imednet tui
imednet tui --study STUDY-KEY --batch BATCH-001 --batch BATCH-002
```

For accessibility and high-contrast environments:

```bash
imednet tui --high-contrast
# or set the environment variable:
export IMEDNET_HIGH_CONTRAST=1
imednet tui
```

## Navigation & Controls

- **Arrow keys / Tab / Shift-Tab**: Move focus between panels, the Jobs batch input, and the Jobs table.
- **Enter / Space**: Select study or site to inspect corresponding subjects and jobs (re-selecting the active site clears the site filter); press **Enter** in the Jobs batch input to track one or more batch IDs for the active study.
- **Escape**: Move focus from the Jobs batch input to the Jobs table, or clear the active site filter and reload all study subjects.
- **q**: Quit application (suppressed while typing in the Jobs batch input).
- **d**: Toggle dark mode (suppressed while typing in the Jobs batch input).
- **h**: Toggle high contrast mode (suppressed while typing in the Jobs batch input).
- **r**: Refresh data for the active study (suppressed while typing in the Jobs batch input).
- **a**: Load all pages when a bounded panel is truncated (suppressed while typing in the Jobs batch input).

## Links

- CLI & TUI Guide: <https://fderuiter.github.io/imednet-toolkit/how-to/cli.html>
- Documentation: <https://fderuiter.github.io/imednet-toolkit/>
- Repository: <https://github.com/fderuiter/imednet-toolkit>
- License: [MIT](https://github.com/fderuiter/imednet-toolkit/blob/main/LICENSE)
