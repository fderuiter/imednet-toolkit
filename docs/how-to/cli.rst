Command Line Interface (CLI)
============================

The package installs an ``imednet`` command that wraps common SDK features. The CLI
reads authentication details from environment variables. See :doc:`/reference/configuration`
for the full list and details on using an ``.env`` file.

.. argparse::
   :module: imednet.cli
   :func: get_parser
   :prog: imednet

Interactive terminal interface and data grid
--------------------------------------------

Install ``imednet[tui]`` (or ``imednet-tui``) to launch the interactive Mission Control
dashboard for browsing studies, sites, subjects, and background jobs:

.. code-block:: bash

   pip install "imednet[tui]"
   imednet tui
   imednet tui --high-contrast
   IMEDNET_HIGH_CONTRAST=1 imednet --high-contrast tui

Within ``imednet tui``, use ``Tab`` / ``Shift+Tab`` and arrow keys to move across panels,
``Enter`` to select a study or site, ``q`` to quit, ``d`` to toggle dark mode, ``h`` to
toggle high-contrast mode, and ``r`` to refresh active study data. Pass ``--force`` only
when intentionally bypassing TTY stream checks.

When ``stdin`` and ``stdout`` are attached to an interactive terminal (with a non-``dumb``
``TERM``), core list commands render results in ``InteractiveDataGrid``, a curses-driven
table with live filtering and a side inspection panel:

* ``Up`` / ``Down`` (or ``k`` / ``j``), ``Page Up`` / ``Page Down``, ``Home`` / ``End``: navigate rows.
* ``/``: enter live search mode across table columns, submission metadata, and form variables (``Enter`` or ``Esc`` exits search input; ``Esc`` in normal mode clears the active filter).
* ``Space``: toggle the side inspection panel (``[`` / ``]`` or ``{`` / ``}`` scrolls panel lines).
* ``q``: exit ``InteractiveDataGrid``.

Set ``IMEDNET_NON_INTERACTIVE=1`` (or pipe stdout in scripts and CI) to bypass
``InteractiveDataGrid`` and emit plain ASCII tabular output instead:

.. code-block:: bash

   IMEDNET_NON_INTERACTIVE=1 imednet studies list

Interactive workflow wizards
----------------------------

When ``imednet-workflows`` is installed, workflow commands support ``--interactive``
step-by-step wizard prompts for study keys, filters, and state ledger updates:

.. code-block:: bash

   imednet workflows extract-records --interactive
   imednet workflows state show --interactive
   imednet workflows state set --interactive
   imednet workflows state reset --interactive
   imednet workflows uat run --interactive

Machine-readable core reads
---------------------------

The CLI uses standard-library argparse; install ``imednet[cli]`` only for optional
dotenv/logging helpers. Use the existing nested ``--help`` for command-specific flags.

.. code-block:: bash

   imednet studies list --format json
   imednet subjects list STUDY --filter subject_status=Screened --format json
   imednet users list STUDY --include-inactive --format json
   imednet codings list STUDY -f dictionaryName=MedDRA --format json
   imednet records list STUDY --format json
   imednet jobs status STUDY BATCH --format json
   imednet jobs wait STUDY BATCH --timeout 60 --format json

Supported core list resources are studies, queries, variables, record-revisions,
sites, records, subjects, intervals, visits, codings, and users. Core JSON reads emit exactly one envelope
with ``schemaVersion: "1.0"``, ``resource``, ``status: "complete"``, ``count`` and
``items``. Empty results use an empty items array. Item fields use the SDK models'
JSON aliases; the JSON view includes model fields beyond the human summary table.
Progress and diagnostics use stderr. A retrieval or serialization failure emits
no success response and exits 1; partial iterators are not reported as complete.
Usage failures exit 2, SDK-command interruption exits 130, and success/help exits 0.

``records list --output json`` still writes ``records.json``; it is distinct from
``--format json`` stdout and combining them is an error. Existing ``export``
commands, datadict JSON/schema/diff outputs and optional workflow command formats
retain their own contracts. This is not a global JSON wrapper for plugins.
See :doc:`/adr/0007-cli-framework-and-output-contracts` for the framework decision,
error/partial-output policy and compatibility inventory.
