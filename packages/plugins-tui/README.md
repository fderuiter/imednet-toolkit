# imednet-tui

Terminal User Interface (TUI) plugin package for the iMednet Python SDK.

## Overview

`imednet-tui` provides an interactive terminal user interface for exploring clinical studies, sites, subjects, and background jobs. It connects to the main `imednet` command-line interface via Python entry points (`imednet.cli_plugins`) without requiring terminal dependencies in headless runtime environments.

## Installation

Install the plugin via pip:

```bash
pip install imednet-tui
```

Or install the core SDK with the `tui` extra:

```bash
pip install 'imednet[tui]'
```

## Usage

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
- **Enter / Space**: Select study or site to inspect corresponding subjects and jobs; press **Enter** in the Jobs batch input to track one or more batch IDs for the active study.
- **q**: Quit application (suppressed while typing in the Jobs batch input).
- **d**: Toggle dark mode (suppressed while typing in the Jobs batch input).
- **h**: Toggle high contrast mode (suppressed while typing in the Jobs batch input).
- **r**: Refresh data for the active study (suppressed while typing in the Jobs batch input).
- **a**: Load all pages when a bounded panel is truncated (suppressed while typing in the Jobs batch input).
