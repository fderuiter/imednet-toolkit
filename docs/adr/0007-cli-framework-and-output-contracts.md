# CLI framework and output contracts

## Decision

Keep the shipped standard-library argparse parser. All core commands, nested help,
required positional arguments, plugin setup_parser entry points, filter syntax and
existing export flags already work through argparse. Typer/Rich would require replacing
registration hooks, rechecking every command and adding dependencies without a concrete
missing capability. The old contributor stack declaration did not justify that cost.
Revisit only for a measured user-facing requirement.

Add opt-in `--format json` to core list reads and job status/wait, using one versioned
response envelope. Human defaults and existing file export flags remain. Datadict JSON
uses its own versioned Data Dictionary/schema/diff contracts and is deliberately not
wrapped in the list envelope. Workflow plugins own their own documented outputs; this
change does not claim a global machine-output mode for arbitrary third-party commands.

## Output and exits

Human progress and diagnostics go to stderr; stdout carries the selected result.
Core list and job machine responses contain schemaVersion `1.0`, resource, status
`complete`, count and items. Empty results are successful count=0/items=[]. Typed
models use JSON-mode alias serialization; date/enum/false/zero/null values retain
JSON meaning. Unknown objects and nonfinite numbers fail instead of becoming strings.
Serialize the whole envelope before writing the first byte. This is a CLI convenience
for bounded read results, not a streaming/full-study export replacement.

Do not emit a successful partial list: consume the iterator before output, propagate
fetch failures, leave stdout empty, and report a redacted diagnostic on stderr. Users
requiring resumable bulk export should use the export APIs. `--output json/csv` still
writes files and cannot be combined with `--format json` on records. No implicit file
write occurs in machine mode.

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
