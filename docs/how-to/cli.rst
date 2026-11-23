Command Line Interface (CLI)
============================

The package installs an ``imednet`` command that wraps common SDK features. The CLI
reads authentication details from environment variables (or runs completely offline
against synthetic ``SimulationTransport`` fixtures when ``--simulation`` or
``IMEDNET_SIMULATION_MODE=true`` is passed). Every Study-scoped command across core,
``datadict from-api``, ``export``, ``workflows``, and ``tui`` accepts either a
positional ``STUDY`` argument or ``-s`` / ``--study-key STUDY`` (passing conflicting
values exits with status code ``2``). See :doc:`/reference/configuration` for the full
list of environment variables and details on using an ``.env`` file.

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
   imednet tui --study DEMO-STUDY --batch BATCH --batch JOB-1002
   imednet tui DEMO-STUDY --simulation
   imednet tui --high-contrast
   IMEDNET_HIGH_CONTRAST=1 imednet --high-contrast tui

Within ``imednet tui``, use ``Tab`` / ``Shift+Tab`` and arrow keys to move across panels,
``Enter`` to select a Study or Site (re-selecting the active Site clears the Site filter;
``Space`` is not bound on ``StudyList`` or ``SiteList``) or to register validated batch IDs in the
Jobs tab input, ``Enter`` or ``Space`` on the Jobs table to toggle the inline ``#job_detail_panel``
(displaying sanitized ``resultUrl`` and per-``Record`` diagnostics), ``Delete`` or ``Backspace`` on
a highlighted row in the Jobs table to untrack that Job, ``Esc`` to move focus from the Jobs batch
input to the Jobs table, close an open ``#job_detail_panel``, or clear an active Site filter, ``q``
to quit, ``d`` to toggle dark mode, ``h`` to toggle high-contrast mode, ``r`` to refresh active
Study data, and ``a`` to load all pages when a panel is truncated. Switch between the ``Jobs``
(bounded to ``5`` concurrent status polls) and ``Logs`` tabs in the bottom pane to audit redacted
SDK/TUI events (``LogViewer`` masks ``Subject`` PHI fields to ``***MASKED***`` by default); viewports
narrower than ``100`` columns automatically reflow into the 2-column ``compact`` layout. Single-key
shortcuts are suppressed automatically while typing batch IDs in the Jobs tab input. Pass
``--study`` / ``--study-key`` and repeatable ``--batch`` / ``--batch-id`` flags at launch to open a
Study and monitor specific batch Jobs immediately (or pass ``--batch`` without ``--study`` to stage
``QUEUED`` batches until a ``Study`` is selected), and pass ``--force`` only when intentionally
bypassing TTY stream checks.

When ``stdin`` and ``stdout`` are attached to an interactive terminal (with a non-``dumb``
``TERM``), core list commands render results in ``InteractiveDataGrid``, a curses-driven
table with live filtering and a side inspection panel:

* ``Up`` / ``Down`` (or ``k`` / ``j``), ``Page Up`` / ``Page Down``, ``Home`` / ``End``: navigate rows.
* ``Left`` / ``Right`` (or ``h`` / ``l``): scroll columns horizontally across wide tables.
* ``/``: enter live search mode across table columns, record metadata, and form variables (``Enter`` or ``Esc`` exits search input; in normal mode, ``Esc`` closes an open side panel first and then clears the active filter).
* ``a``: load all remaining pages into ``InteractiveDataGrid`` when the initial 100-item TTY view is truncated, re-evaluating any active ``/`` filter.
* ``Space``: toggle the side inspection panel (``[`` / ``]`` scrolls overflowing panel lines and ``{`` / ``}`` scrolls panel pages with ``Lines X-Y of Z`` ``▲`` / ``▼`` cues; ``Esc`` or ``Space`` closes the panel).
* ``q``: exit ``InteractiveDataGrid``.

Set ``IMEDNET_NON_INTERACTIVE=1`` or pass ``--no-interactive`` (or pipe stdout in scripts
and CI) to bypass ``InteractiveDataGrid`` and emit plain ASCII tabular output instead, or
use ``imednet tui --no-interactive`` to block full-screen TUI launches with exit code ``1``:

.. code-block:: bash

   IMEDNET_NON_INTERACTIVE=1 imednet studies list
   imednet tui --no-interactive

Interactive workflow wizards
----------------------------

When ``imednet-workflows`` is installed, workflow commands support ``--interactive``
step-by-step wizard prompts for study keys, filters, and state ledger updates:

.. code-block:: bash

   imednet workflows extract-records --interactive
   imednet workflows state show --interactive
   imednet workflows state set --interactive
   imednet workflows state reset --interactive
   imednet workflows uat build-spec STUDY --subject-count 2 --seed 42 -o uat_spec.json --simulation
   imednet workflows uat run --spec uat_spec.json --schema study_dd.json --dry-run --format json
   imednet workflows uat run --interactive
   imednet workflows sync-worker STUDY --interval 900 --once

Core write commands and batch validation
----------------------------------------

Use ``imednet records create``, ``imednet records update``, ``imednet records import``,
and ``imednet subjects register`` to validate and submit chunked ``Record`` writes or
``Subject`` enrollments from inline ``-d`` / ``--data KEY=VALUE`` pairs, JSON payloads
(``--json`` or ``--file -`` on ``stdin``), or CSV/JSON files:

.. code-block:: bash

   imednet records create STUDY --form-key F_VS --subject-key SUBJ-001 --site-name SITE \
       --interval-name Baseline -d V_WEIGHT=71.4 --schema study_dd.json --dry-run
   imednet records create STUDY --file ./records_batch.json --schema api --chunk-size 100 \
       --wait --timeout 300 --interval 5 --format json
   imednet records update STUDY --record-id 1042 --form-key F_VS -d V_WEIGHT=72.0 \
       --reason-for-change "Source data verification" --schema study_dd.json --dry-run
   imednet records import STUDY ./records_batch.csv --schema study_dd.json --wait --format json
   imednet subjects register STUDY --file ./registrations.json --form-key F_DM \
       --site-name SITE --skip-existing --subject-key-field SUBJID --schema study_dd.json \
       --wait --format json
   imednet subjects register STUDY --form-key F_DM --site-name SITE --subject-key SUBJ-002 \
       -d V_AGE=42 -d V_GENDER=F --dry-run

All four write commands share pre-submission validation and polling controls:

* ``--schema``: Validate payloads before submission against a canonical ``.json`` Data Dictionary,
  CDISC ODM ``.xml``, REDCap ``.csv``, a 4-file CSV export directory, or ``api`` (which refreshes
  live ``Form`` and ``Variable`` definitions via ``SchemaCache``). Payloads containing the
  redacted ``***MASKED***`` sentinel are rejected before any HTTP write occurs.
* ``--dry-run``: Execute local schema, ``Site`` eligibility, and deduplication checks without
  sending HTTP write requests (pairing ``--dry-run`` with a local ``--schema`` file runs completely
  offline without requiring API credentials).
* ``--wait``, ``--timeout`` (default ``300`` seconds), ``--interval`` (default ``5`` seconds),
  ``--chunk-size``, and ``--email-notify``: Split large batches into bounded POST chunks and
  optionally poll submitted background ``Job`` batch IDs to a terminal state.
* ``--skip-existing`` (alias ``--filter-enrolled``) and ``--subject-key-field`` on
  ``subjects register``: Query active ``Study`` ``Subject`` keys/OIDs first, skip already-enrolled
  or duplicate cohort entries (reporting the skipped count in ``skippedCount``), and inspect a
  custom ``Variable`` inside ``data`` (such as ``SUBJID``) when top-level ``subjectKey`` is omitted.
* ``--format json`` (or ``IMEDNET_CLI_OUTPUT_FORMAT=json``): Emit a ``schemaVersion: "1.0"``
  envelope for ``records_create``, ``records_update``, ``records_import``, or ``subjects_register``
  wrapping a ``BatchSubmissionSummary`` item (exiting ``1`` if ``validationReport.is_valid`` is ``false``).

Machine-readable core reads
---------------------------

The CLI uses standard-library argparse; install ``imednet[cli]`` only for optional
dotenv/logging helpers. Use the existing nested ``--help`` for command-specific flags.

.. code-block:: bash

   imednet studies list --simulation --format json
   imednet subjects list STUDY --filter subject_status=Enrolled --format json
   imednet subjects list --study-key STUDY --format json
   imednet users list STUDY --include-inactive --format json
   imednet codings list STUDY -f dictionaryName=MedDRA --format json
   imednet records list STUDY --format json
   imednet jobs status STUDY BATCH --format json
   imednet jobs wait STUDY JOB-1002 --timeout 60 --format json
   imednet workflows extract-records STUDY --format json
   imednet workflows state show --study-key STUDY --format json
   imednet workflows uat run STUDY --dry-run --simulation --format json
   imednet subject-data STUDY SUB1 --format json
   IMEDNET_CLI_OUTPUT_FORMAT=json imednet studies list

Supported core list resources are studies, queries, variables, record-revisions,
sites, forms, records, subjects, intervals, visits, codings, and users, alongside
``workflows extract-records``, ``workflows state show`` (resource ``ledger_state``),
``workflows uat run`` (resource ``uat_run``), and ``subject-data`` (plus write batch summaries for ``records create``, ``records update``,
``records import``, and ``subjects register``). JSON reads emit one envelope
with ``schemaVersion: "1.0"``, ``resource``, ``status: "complete"``, ``count`` and
``items`` (set ``IMEDNET_CLI_OUTPUT_FORMAT=json`` to default ``--format`` commands
and ``datadict diff`` to JSON unless an explicit ``--format`` flag overrides it). Empty list results use an empty items array, whereas ``subject-data`` exits ``1`` on ``stderr`` when the requested Subject does not exist in the Study. Item fields use the SDK models'
JSON aliases; the JSON view includes model fields beyond the human summary table.
Progress and diagnostics use stderr. A retrieval or serialization failure emits
no success response and exits 1; partial iterators are not reported as complete.
Usage failures exit 2, SDK-command interruption exits 130, and success/help exits 0.

Legacy ``records list -o/--output`` accepts ``json``, ``csv``, or an explicit ``.json`` /
``.csv`` destination path (preserving ``./records.json`` and ``./records.csv`` for
``-o json`` and ``-o csv``), flattens ``recordData`` variables, masks ``Subject`` PHI to
``***MASKED***`` by default (``--mask-phi``; opt out with ``--no-mask-phi``), and emits a
``stderr`` deprecation warning (prefer ``--format json`` for stdout or ``imednet export``
for file exports); combining ``-o/--output`` with ``--format json`` is an error. Existing ``export``
commands (which also accept ``--no-interactive`` to suppress live spinners) and ``datadict`` JSON/schema/diff outputs
(see :doc:`/how-to/data_dictionary`) retain their own contracts. This is not a global JSON wrapper for arbitrary third-party plugins.
See :doc:`/adr/0007-cli-framework-and-output-contracts` for the framework decision,
error/partial-output policy and compatibility inventory.

Path segment validation and exit codes
--------------------------------------

Every Study- or resource-scoped CLI command (decorated with ``@with_sdk``, calling
``validate_cli_path_segment``, or dispatched through ``imednet workflows`` subcommands
such as ``extract-records`` and ``uat run``) validates dynamic URL segments
(``study_key``, ``batch_id``, and ``item_id`` across ``Study``, ``Subject``, ``Site``,
``Form``, ``Record``, ``Visit``, ``Interval``, ``Variable``, ``Query``,
``Data Dictionary``, and ``Job`` targets) with
:func:`~imednet.utils.validate_path_segment` and :func:`~imednet.utils.build_safe_path`
before issuing HTTP requests.

When an argument contains path traversal sequences (``..``, ``%2e%2e``), path
separators (``/`` or ``\``), query/fragment delimiters (``?`` or ``#``), or control
characters, :class:`~imednet.errors.PathTraversalValidationError` is caught at the CLI
boundary: a redacted diagnostic is written to ``stderr``, ``stdout`` remains empty (no
partial JSON envelope or table rows), and the process exits with status code ``1``:

.. testcode::

   import contextlib
   import io
   from imednet.cli import app

   for bad_argv in (
       ["records", "list", "../secret", "--simulation", "--format", "json"],
       ["workflows", "extract-records", "../secret", "--simulation", "--format", "json"],
   ):
       out_buf = io.StringIO()
       err_buf = io.StringIO()
       exit_code: int | str | None = None
       with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
           try:
               app(bad_argv)
           except SystemExit as cli_exit:
               exit_code = cli_exit.code
       assert exit_code == 1
       assert out_buf.getvalue() == ""
       assert "Error:" in err_buf.getvalue()

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
   imednet datadict from-api STUDY --simulation -o sim_dd.json
   imednet datadict schema -o data-dictionary.schema.json
   imednet datadict validate study_dd.json --schema data-dictionary.schema.json
   imednet datadict diff baseline_dd.json study_dd.json --format md

Terminal capabilities and non-interactive mode
----------------------------------------------

Multi-item ``list`` commands open an interactive keyboard grid when both ``stdin``
and ``stdout`` are connected to a capable TTY, while single-job commands
(``imednet jobs status`` and ``imednet jobs wait``) always print a static summary
table to ``stdout`` and exit immediately.

Pass ``--no-interactive`` (at the root command or on ``imednet tui``, read, write, export,
and workflow subcommands) or set ``IMEDNET_NON_INTERACTIVE=1`` to disable full-screen ``curses`` grids,
live ``stderr`` spinners, and interactive ``stdin`` prompts. Passing
``imednet tui --no-interactive`` (or invoking ``imednet tui`` while
``IMEDNET_NON_INTERACTIVE`` is enabled) exits immediately with status code ``1``, even
when ``--force`` is supplied. Set ``NO_COLOR=1`` (or any non-empty value) or
``TERM=dumb`` to suppress ANSI color escape sequences across tables and diagnostics
while preserving ``--high-contrast`` palettes when color output is permitted.
Interactive workflow wizards reject piped or non-TTY ``stdin`` streams immediately with
exit code ``1``.

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

   imednet records list STUDY --filter "formKey==F_DM;recordId>=600" \
       --record-data-filter "V_GENDER==Female;V_AGE>=18" --sort dateCreated,desc --limit 50
   imednet queries list STUDY --filter "type==record,type==question" --sort annotationId,desc
   imednet users list STUDY --include-inactive --sort login,asc --limit 25

Interactive TTY table sessions fetch one bounded page of 100 items by default and
display a truncation notice when additional Study items exist; press ``a`` inside
``InteractiveDataGrid`` or pass ``--limit N`` (upstream ``size`` is automatically
capped at 500 per page) or ``--all`` to retrieve larger batches. Non-interactive runs (``--no-interactive``,
``IMEDNET_NON_INTERACTIVE=1``, or piped ``stdout``), headless ``--format json``, and
file exports (``--output json|csv``) continue to fetch all matching pages unless
``--limit`` is explicitly specified.

CDISC standards validation and export controls
----------------------------------------------

When ``imednet-workflows`` is installed, ``imednet workflows validate-standards`` audits
live or simulated ``Study`` ``Form``, ``Variable``, and non-deleted ``Record`` payloads
against CDISC CDASH variable naming heuristics and SDTM readiness rules without writing
export files. Pass ``--profile`` / ``--standards-profile`` (``general``, ``drug``,
``biologic``, ``vaccine``, or ``device``), optional ``--domain`` / ``--sdtm-domain``,
``--min-score`` / ``--min-standards-score`` (exiting ``1`` when the ``Study`` score is
below threshold), ``--mask-phi`` / ``--no-mask-phi``, and ``--format json``:

.. code-block:: bash

   imednet workflows validate-standards STUDY --profile drug --domain AE --min-score 85
   imednet workflows validate-standards --study-key STUDY --standards-profile device \
       --format json --simulation
   imednet workflows validate-standards STUDY --no-mask-phi --format json

Every ``imednet export`` subcommand (``csv``, ``parquet``, ``excel``, ``json``,
``duckdb``, ``sql``, ``mongodb``, ``neo4j``, and ``snowflake``) masks sensitive
``Subject`` identifiers and committed ``StudyConfiguration`` ``phiFields`` to
``***MASKED***`` by default (``--mask-phi``) before building ``USUBJID`` or
domain outputs, even when ``--use-labels`` or ``--hierarchical`` is active.
Authorized compliance workflows operating inside a restricted enclave can opt
out explicitly with ``--no-mask-phi``.

All nine ``imednet export`` subcommands also share incremental checkpointing,
watermark filtering, CDISC SDTM transformation, and opt-in quality-gate flags:

* ``--since``: Export ``Record`` items modified on or after an ISO-8601 timestamp or date.
* ``--incremental``, ``--stream-name``, and ``--ledger-path``: Read and commit
  transactional high-water mark checkpoints via ``imednet.state_providers`` (defaulting to
  ``IMEDNET_WORKFLOW_LEDGER_PATH`` or ``~/.imednet/pipeline_ledger.json`` for file ledgers).
* ``--include-deleted``: Include voided (``deleted=True``) ``Record`` items in export batches.
* ``--quality-gate``, ``--no-quality-gate``, and ``--min-readiness-score``: Opt-in pre-write
  ``Record`` schema readiness filtering (disabled by default across all nine subcommands, including
  SQLite per-``Form``, ``--single-table``, and ``--long-format`` SQL exports; enable explicitly with
  ``--quality-gate`` and optional ``0-100`` ``--min-readiness-score``, or force off with ``--no-quality-gate``).
* ``--as-sdtm``, ``--sdtm-domain`` (alias ``--domain``), ``--standards-profile``
  (``general``, ``drug``, or ``device``), ``--min-standards-score``, and
  ``--naming-convention snake_case|camelCase|sdtm``: Transform exports into CDISC SDTM
  domains, enforce batch-level ``StandardsReadinessValidator`` thresholds, or normalize raw
  ``Variable`` column naming:

.. code-block:: bash

   imednet export csv STUDY ./ae_sdtm.csv --sdtm-domain AE --standards-profile drug \
       --min-standards-score 85
   imednet export parquet STUDY ./incremental_records.parquet --incremental \
       --since 2026-01-01T00:00:00Z --stream-name vitals_parquet \
       --ledger-path ./export_ledger.json --include-deleted
   imednet export duckdb STUDY clinical_records ./study.duckdb --incremental \
       --stream-name duckdb_sync --no-quality-gate
   imednet export parquet STUDY ./aligned_study.parquet --naming-convention snake_case
   imednet export json STUDY ./study_tree.json --hierarchical --use-labels
   imednet export neo4j STUDY bolt://localhost:7687 neo4j "$NEO4J_PASSWORD" \
       --quality-gate --min-readiness-score 90 --standards-profile general
   imednet export parquet STUDY /secure/enclave/raw_study.parquet --no-mask-phi
