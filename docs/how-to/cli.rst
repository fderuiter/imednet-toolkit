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
   imednet tui --study DEMO-STUDY --batch BATCH-001 --batch BATCH-002
   imednet tui --high-contrast
   IMEDNET_HIGH_CONTRAST=1 imednet --high-contrast tui

Within ``imednet tui``, use ``Tab`` / ``Shift+Tab`` and arrow keys to move across panels,
``Enter`` to select a study or site (re-selecting the active site clears the site filter)
or to register batch IDs in the Jobs tab input, ``Esc`` to move focus from the Jobs batch
input to the Jobs table or clear an active site filter, ``q`` to quit, ``d`` to toggle
dark mode, ``h`` to toggle high-contrast mode, ``r`` to refresh active study data, and
``a`` to load all pages when a panel is truncated. Single-key shortcuts are suppressed
automatically while typing batch IDs in the Jobs tab input. Pass ``--study`` and
repeatable ``--batch`` flags at launch to open a study and monitor specific batch jobs
immediately, and pass ``--force`` only when intentionally bypassing TTY stream checks.

When ``stdin`` and ``stdout`` are attached to an interactive terminal (with a non-``dumb``
``TERM``), core list commands render results in ``InteractiveDataGrid``, a curses-driven
table with live filtering and a side inspection panel:

* ``Up`` / ``Down`` (or ``k`` / ``j``), ``Page Up`` / ``Page Down``, ``Home`` / ``End``: navigate rows; ``Left`` / ``Right`` (or ``h`` / ``l``): scroll wide columns.
* ``/``: enter live search mode across table columns, record metadata, and form variables (``Enter`` or ``Esc`` exits search input; in normal mode, ``Esc`` closes an open side panel first and then clears the active filter).
* ``a``: load all remaining pages into ``InteractiveDataGrid`` when the initial 100-item TTY view is truncated, re-evaluating any active ``/`` filter.
* ``Space``: toggle the side inspection panel (``[`` / ``]`` or ``{`` / ``}`` scrolls overflowing panel lines with ``Lines X-Y of Z`` ``▲`` / ``▼`` cues; ``Esc`` or ``Space`` closes the panel).
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
commands, ``datadict`` JSON/schema/diff outputs (see :doc:`/how-to/data_dictionary`)
and optional workflow command formats retain their own contracts. This is not a
global JSON wrapper for plugins.
See :doc:`/adr/0007-cli-framework-and-output-contracts` for the framework decision,
error/partial-output policy and compatibility inventory.

Data Dictionary commands
------------------------

The ``imednet datadict`` subcommands convert Study metadata across 4-file CSV exports,
CDISC ODM 1.3 XML, REDCap CSV files, and the EDC REST API, validate canonical JSON
files, export the JSON Schema, and compute structural revision diffs (see
:doc:`/how-to/data_dictionary` for Python equivalents and Record validation):

.. code-block:: bash

   imednet datadict from-csv ./datadict_export.zip -o study_dd.json --strict --no-raw-xml
   imednet datadict from-odm ./study_odm.xml --study-key STUDY -o study_dd.json --strict
   imednet datadict to-odm study_dd.json -o study_odm.xml --file-oid ODM.STUDY.001
   imednet datadict from-redcap ./redcap_dictionary.csv --study-key STUDY -o study_dd.json
   imednet datadict from-api --study-key STUDY -o api_dd.json --no-raw-xml
   imednet datadict schema -o data-dictionary.schema.json
   imednet datadict validate study_dd.json --schema data-dictionary.schema.json
   imednet datadict diff baseline_dd.json study_dd.json --format md

Terminal capabilities and non-interactive mode
----------------------------------------------

Multi-item ``list`` commands open an interactive keyboard grid when both ``stdin``
and ``stdout`` are connected to a capable TTY, while single-job commands
(``imednet jobs status`` and ``imednet jobs wait``) always print a static summary
table to ``stdout`` and exit immediately.

Pass ``--no-interactive`` (at the root command or on read/workflow subcommands) or
set ``IMEDNET_NON_INTERACTIVE=1`` to disable full-screen ``curses`` grids, live
``stderr`` spinners, and interactive ``stdin`` prompts. Set ``NO_COLOR=1`` (or any
non-empty value) or ``TERM=dumb`` to suppress ANSI color escape sequences across
tables and diagnostics while preserving ``--high-contrast`` palettes when color
output is permitted. Interactive workflow wizards reject piped or non-TTY ``stdin``
streams immediately with exit code ``1``.

Server-side filters, sorting, and bounded fetches
-------------------------------------------------

Eleven filter-capable ``list`` commands (``studies``, ``sites``, ``forms``,
``intervals``, ``queries``, ``record-revisions``, ``variables``, ``visits``,
``records``, ``subjects``, and ``codings``) accept repeatable ``-f`` / ``--filter``
arguments using ``=`` or the standard iMednet comparison operators (``<``, ``<=``,
``>``, ``>=``, ``==``, ``!=``) with exclusive ``;`` (AND) or ``,`` (OR) connectors.
Filter attribute names (in ``snake_case`` or ``camelCase``) and scalar values are
validated locally against each endpoint's documented filter allowlist and resource
model before sending HTTP requests: string identifiers preserve leading zeros,
double-quoted strings (including empty ``""``), and non-ASCII text (such as
``subject_key=001``, ``code=0042``, ``reason==""``, or ``value=²``), while integer
and boolean attributes reject invalid values on ``stderr`` with exit code ``1``. Commands that take a positional
``study_key`` reject redundant ``study_key`` / ``studyKey`` attributes in
``--filter`` before issuing requests, whereas ``imednet studies list`` accepts them.
``records list`` additionally exposes ``--record-data-filter`` (supporting ``=~``
regular-expression matching in addition to the six standard operators, plus the
literal ``none`` bypass) to filter Form Variable values on the server. All twelve
``list`` commands—including ``users list``—accept ``--sort``, ``--limit``, and
``--all``.

.. code-block:: bash

   imednet records list STUDY --filter "formKey==AE;recordId>=100" \
       --record-data-filter "AESER==Serious;AGE>=18" --sort dateCreated,desc --limit 50
   imednet queries list STUDY --filter "type==record,type==question" --sort annotationId,desc
   imednet users list STUDY --include-inactive --sort login,asc --limit 25

Interactive TTY table sessions fetch one bounded page of 100 items by default and
display a truncation notice when additional Study items exist; press ``a`` inside
``InteractiveDataGrid`` or pass ``--limit N`` (upstream ``size`` is automatically
capped at 500 per page) or ``--all`` to retrieve larger batches. Non-interactive runs (``--no-interactive``,
``IMEDNET_NON_INTERACTIVE=1``, or piped ``stdout``), headless ``--format json``, and
file exports (``--output json|csv``) continue to fetch all matching pages unless
``--limit`` is explicitly specified.

Export PHI masking, SDTM, and quality-gate controls
---------------------------------------------------

Every ``imednet export`` subcommand (``csv``, ``parquet``, ``excel``, ``json``,
``duckdb``, ``sql``, ``mongodb``, ``neo4j``, and ``snowflake``) masks sensitive
``Subject`` identifiers and committed ``StudyConfiguration`` ``phiFields`` to
``***MASKED***`` by default (``--mask-phi``) before building ``USUBJID`` or
domain outputs, even when ``--use-labels`` or ``--hierarchical`` is active.
Authorized compliance workflows operating inside a restricted enclave can opt
out explicitly with ``--no-mask-phi``.

Exports also support native CDISC SDTM transformation (``--as-sdtm`` and
``--sdtm-domain``), batch-level CDISC standards readiness gating via
``StandardsReadinessValidator`` (``--standards-profile`` and
``--min-standards-score``), raw variable casing alignment
(``--naming-convention snake_case|camelCase|sdtm``), and pre-write ``Record``
schema readiness filtering (``--quality-gate`` and ``--min-readiness-score``):

.. code-block:: bash

   imednet export csv STUDY ./ae_sdtm.csv --sdtm-domain AE --standards-profile drug \
       --min-standards-score 85
   imednet export parquet STUDY ./aligned_study.parquet --naming-convention snake_case
   imednet export json STUDY ./study_tree.json --hierarchical --use-labels
   imednet export neo4j STUDY bolt://localhost:7687 neo4j "$NEO4J_PASSWORD" \
       --quality-gate --min-readiness-score 90 --standards-profile general
   imednet export parquet STUDY /secure/enclave/raw_study.parquet --no-mask-phi
