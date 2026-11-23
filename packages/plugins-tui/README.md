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

Launch the terminal interface using the `imednet` CLI (accepts positional `<STUDY_KEY>` or `-s` / `--study` / `--study-key`, repeatable or comma-separated `-b` / `--batch` / `--batch-id`, offline `--simulation`, `--force`, and `--no-interactive`):

```bash
imednet tui
imednet tui --study STUDY-KEY --batch BATCH-001 --batch BATCH-002
imednet tui --study-key STUDY-KEY --batch-id BATCH-001,BATCH-002 --simulation
imednet tui STUDY-KEY --simulation --force
imednet tui --no-interactive
```

Passing `--no-interactive` (or setting `IMEDNET_NON_INTERACTIVE=1`) blocks full-screen TUI launches immediately with exit code `1`, even when `--force` is supplied. Pass `--force` only when intentionally bypassing non-TTY stream detection.

For accessibility and high-contrast environments:

```bash
imednet tui --high-contrast
# or set the environment variable:
export IMEDNET_HIGH_CONTRAST=1
imednet tui
```

## Navigation, Layout & Controls

- **Arrow keys / Tab / Shift-Tab**: Move focus between `StudyList`, `SiteList`, `SubjectTable`, the bottom `Jobs` / `Logs` tabs, the Jobs batch input, and the Jobs table.
- **Enter**: Select a Study in `StudyList` or a Site in `SiteList` to inspect corresponding Subjects and Jobs (re-selecting the active Site clears the Site filter; `StudyList` and `SiteList` use **Enter**-only selection and do not bind Space). Press **Enter** in the Jobs batch input to register one or more batch IDs for the active Study.
- **Delete / Backspace**: Untrack the currently highlighted Job row in the `Jobs` table (`JobTable`).
- **Escape**: Move focus from the Jobs batch input to the Jobs table, or clear the active Site filter and reload all Study Subjects.
- **q**: Quit application (suppressed while typing in the Jobs batch input).
- **d**: Toggle dark mode (suppressed while typing in the Jobs batch input).
- **h**: Toggle high contrast mode (suppressed while typing in the Jobs batch input).
- **r**: Refresh data for the active Study and re-poll placeholder Jobs (suppressed while typing in the Jobs batch input).
- **a**: Load all pages when a bounded Study, Site, or Subject panel is truncated (suppressed while typing in the Jobs batch input).
- **Jobs & Logs Bottom Tabs**: `JobMonitor` polls active non-terminal Study Jobs in the background with a 5-poll concurrency cap (`MAX_CONCURRENT_POLLS = 5`) and pauses polling automatically once all tracked Jobs reach terminal states. Switch to the `Logs` tab (`LogViewer`) to inspect credential-redacted `imednet` and `imednet_tui` events.
- **Responsive `compact` Layout**: Viewports narrower than `100` columns automatically switch `DashboardScreen` into the 2-column `compact` grid layout so `Subjects` and the `Jobs` / `Logs` pane span full width.
