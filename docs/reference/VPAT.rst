===============================================
Voluntary Product Accessibility Template (VPAT)
===============================================

Accessibility Conformance Report
--------------------------------
**Product:** ``imednet-toolkit`` (``imednet``) — Unofficial Open-Source Python SDK, CLI/TUI, and Documentation Portal
**Ownership & Affiliation:** Community-maintained open-source project (`fderuiter/imednet-toolkit <https://github.com/fderuiter/imednet-toolkit>`_); **not** affiliated with, sponsored by, or endorsed by Mednet Solutions / iMednet
**Scope:** Static Sphinx/Furo documentation portal (`https://fderuiter.github.io/imednet-toolkit/`), client-side interactive showcase and Pyodide tutorials, and the ``imednet`` CLI / ``imednet tui`` terminal interfaces (excludes the proprietary iMednet EDC web application)
**Feedback & Issue Reporting:** `GitHub Issues <https://github.com/fderuiter/imednet-toolkit/issues>`_

Summary
^^^^^^^
The ``imednet-toolkit`` documentation portal and terminal tooling target WCAG 2.1 Level AA conformance across primary developer and reviewer workflows.
All interactive controls, Pyodide launch and run buttons, schema tags, status indicators, and Mermaid diagram labels meet or exceed the 4.5:1 contrast threshold in both light and dark themes (and >= 3:1 for diagram strokes), while ``@media (prefers-contrast: more)`` provides >= 7:1 contrast and ``@media (forced-colors: active)`` maps controls and diagrams to CSS system colors.

WCAG 2.1 Report
^^^^^^^^^^^^^^^
- **Principle 1: Perceivable:** Supports (WCAG 2.1 Level AA)
- **Principle 2: Operable:** Supports (WCAG 2.1 Level AA)
- **Principle 3: Understandable:** Supports (WCAG 2.1 Level AA)
- **Principle 4: Robust:** Supports (WCAG 2.1 Level AA)

**Notes:** Theme typography and semantic Furo CSS tokens satisfy WCAG 2.1 Level AA contrast requirements (>= 4.5:1 for normal text and >= 3:1 for graphical strokes). Interactive brand buttons achieve >= 6.4:1 contrast in light mode and >= 7.5:1 in dark mode, status badges maintain >= 4.5:1 contrast across light and dark container surfaces, and ``@media (prefers-contrast: more)`` enforces a minimum contrast ratio of 7:1. All 15 Mermaid architecture and workflow diagrams expose ``<title>`` and ``aria-label`` semantics and scroll horizontally inside keyboard-focusable regions so labels stay >= 12px on 375px mobile viewports. Showcase controls satisfy WCAG 2.1 SC 2.5.3 (Label in Name), and all ``role="tablist"`` widgets implement roving ``tabindex`` keyboard navigation (``ArrowRight``, ``ArrowLeft``, ``ArrowDown``, ``ArrowUp``, ``Home``, ``End``) alongside tabular fallbacks. In the CLI and ``imednet tui`` terminal interface (``ImednetTuiApp``), high-contrast styling (>= 7:1 foreground-and-background status badges and ``#FFFFFF``-on-``#000000`` panels) can be enabled via ``imednet --high-contrast``, ``imednet tui --high-contrast``, or ``IMEDNET_HIGH_CONTRAST=1``. All terminal workflows are fully keyboard-operable without a pointer: ``imednet tui`` binds ``q`` (Quit), ``d`` (Dark Mode), ``h`` (High Contrast), ``r`` (Refresh), ``a`` (Load All), and ``escape`` (Exit Batch Input / Close Job Detail Panel / Clear Filter), plus ``Enter`` and ``Space`` on ``JobTable`` to toggle the focusable split ``#job_detail_panel`` (scrollable via ``Tab`` and ``Up``/``Down``/``PageUp``/``PageDown``/``Home``/``End``) and ``Delete`` / ``Backspace`` to untrack a job, alongside ``Tab`` / ``Shift+Tab`` and arrow-key panel navigation, while ``InteractiveDataGrid`` supports keyboard row paging, live search, and bounded-page expansion (``Up``/``Down``, ``k``/``j``, ``/``, ``a``, ``Esc``, ``q``), horizontal column scrolling (``Left``/``Right`` and ``h``/``l``), and side-panel inspection/scrolling (``Space``, ``[``/``]``, and ``{``/``}``). Headless automation is guarded via ``IMEDNET_NON_INTERACTIVE=1`` and ``--no-interactive`` (including ``imednet tui --no-interactive``, which exits with status code ``1`` without launching a full-screen UI) for plain-text tabular fallback.

**Verification Gate:** Continuously enforced in CI via Sphinx link/HTML validation and headless Chromium Playwright checks in ``tests/unit/test_docs_accessibility.py``.
