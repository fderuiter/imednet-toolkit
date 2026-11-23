# CLI framework and output contracts

## Decision

Keep the shipped standard-library argparse parser. All core commands, nested help,
required positional arguments, `imednet.cli_plugins` `setup_parser` entry-point discovery
(used by `imednet-workflows` and `imednet-tui`, with graceful fallback stubs when
uninstalled), filter syntax and existing export flags already work through argparse.
Typer/Rich would require replacing registration hooks, rechecking every command and
adding dependencies without a concrete missing capability. The old contributor stack
declaration did not justify that cost. Revisit only for a measured user-facing requirement.

Add opt-in `--format json` to core list reads, core write commands (`records create`,
`records update`, `records import`, `subjects register`), job status/wait, and workflow commands
(`extract-records`, `subject-data`, `state show`, `validate-standards`). Explicit
`--format` flags always take precedence; otherwise setting `IMEDNET_CLI_OUTPUT_FORMAT=json`
defaults `--format`-capable commands (and `datadict diff`) to JSON output while suppressing
full-screen `InteractiveDataGrid` UIs, `stdout` ANSI color sequences, and live `stderr`
spinners. When neither `--format json` nor `IMEDNET_CLI_OUTPUT_FORMAT=json` is active,
human defaults render through curses `InteractiveDataGrid` paging, live search and
side-panel inspection on interactive TTYs, and fall back to plain tabular stdout when
streams are non-interactive, `TERM=dumb`, or `IMEDNET_NON_INTERACTIVE=1` is set; existing
file export flags remain. Study-scoped commands across core, workflows, and TUI share
`imednet.spi.cli` hybrid `study_key` parsing (accepting positional `STUDY` or
`-s`/`--study-key` and rejecting conflicting values with exit code `2`) and offline
`--simulation` mode (`SimulationTransport` via `IMEDNET_SIMULATION_MODE=1`). Datadict JSON
uses its own versioned Data Dictionary/schema/diff contracts and is deliberately not
wrapped in the list envelope. TUI and non-JSON workflow commands (`sync-worker`,
`state set`, `state reset`, `uat run`) own their own documented outputs and interactive
sessions; this change does not claim a global machine-output mode for arbitrary
third-party commands.

## Output and exits

Human progress and diagnostics go to stderr; stdout carries the selected result.
Core list, job, and write machine responses contain schemaVersion `1.0`, resource, status
`complete`, count and items. Empty read results are successful count=0/items=[]. Write
commands (`records_create`, `records_update`, `records_import`, `subjects_register`) emit `count: 1` with a
single `BatchSubmissionSummary` item containing `studyKey`, `dryRun`, `totalRecords`,
`skippedCount`, `chunkCount`, `batchIds`, `allSuccessful`, `jobs` (alias-serialized `Job`
models), and `validationReport`; when `validationReport.is_valid` is `false`, JSON mode
still emits the summary envelope to `stdout` and exits `1`. By contrast,
`imednet workflows validate-standards --format json` emits a standalone readiness report
JSON object (`study_key`, `profile`, `domain`, `score`, `is_ready`, `passed`,
`checked_records`, `checked_fields`, `successfully_validated_fields`, `domain_scores`,
`warnings`, and `violations`) with PHI masked by default (`--mask-phi` / `--no-mask-phi`),
exiting `1` when `score` falls below `--min-score` / `--min-standards-score`. Typed
models use JSON-mode alias serialization; date/enum/false/zero/null values retain
JSON meaning. Unknown objects and nonfinite numbers fail instead of becoming strings.
Serialize the whole document before writing the first byte. This is a CLI convenience
for bounded read/write results, not a streaming/full-study export replacement.

Do not emit a successful partial list: consume the iterator before output, propagate
fetch failures, leave stdout empty, and report a redacted diagnostic on stderr. Users
requiring resumable bulk export should use the export APIs. Legacy `records list -o/--output`
still writes `records.json` / `records.csv` with a `stderr` deprecation warning (prefer
`--format json` or `imednet export`), and cannot be combined with `--format json`. No
implicit file write occurs in machine mode.

Exit 0 means success/empty/help; 1 means command/auth/permission/API/serialization
failure; 2 means argparse usage failure; 130 means an interrupted SDK command. Existing
native datadict nonzero validation/diff statuses remain command-specific, documented
by their help rather than remapped by a new framework. A completed background job does
not certify correct clinical outcomes. Plugin load warnings remain stderr and redact
known environment credentials, including OIDC tokens.

## Verification and compatibility

Real parser invocation with synthetic SDKs and partially failing iterators proves valid
single-document JSON, clean stdout, errors/empty results, filtering, exact file flag
behavior, human help and cleanup. A subprocess simulation run validates the console
entry point without live EDC access. No framework dependency or existing command is removed.
