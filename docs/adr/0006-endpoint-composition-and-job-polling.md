# Endpoint composition and shared job polling

## Decision and call trace

Retain the operation descriptors. A sync `records.list` call resolves parameters and
study context, constructs ListOperation, then returns its iterator using the sync
requester and paginator. The async descriptor returns an AsyncIterator directly;
`get` instead returns an awaitable. The transport alone owns retries (PR #1710).
Deleting descriptors would duplicate this dispatch for each endpoint without a
measured maintenance or type-inference improvement. Specialized path-based Jobs.get
continues to use PathGetOperation; no new mixin hierarchy is introduced.

Add typed JobsEndpoint.poll/poll_many and async equivalents over the existing core
pollers. SDK poll_job/async_poll_job delegate here, workflows retain their aliases,
and Airflow's nonblocking sensor keeps evaluate_job_state from the same core seam.
Airflow owns its own poke scheduling; it must not block inside a full poll loop.
Keep MultiStudyOrchestrator and all old poller imports available. Restore the ignored
ctx compatibility argument on concrete list/get constructors with its prior warning.
GenericEndpoint keeps its current positional async_client and accepts ctx by keyword.
The old generic three-positional constructor is not restored because it would silently
reinterpret currently valid calls; use explicit keywords when migrating those callers.

## Lifecycle evidence and limits

PR #1674 and #1715 established the current state enum and monotonic checks. Preserve
the accepted success aliases COMPLETED/COMPLETE/SUCCESS and failure terminals
FAILED/CANCELLED/TIMEOUT for compatibility. These are the repository's current
contract, not a claim of complete vendor lifecycle documentation. Missing state
remains nonterminal and times out; unrecognized states raise validation errors rather
than reporting success. Local timeout raises JobTimeoutError; an observed failure,
cancellation or server TIMEOUT raises JobFailedError with its status. Async task
cancellation propagates; no server-side cancellation is implied. This PR does not
broaden wire coercion or state semantics without vendor evidence (#1683).

Progress callbacks support either a JobStatusEvent argument or the original
(batch_id, status, elapsed). Bind the signature before invoking it; a callback-raised
TypeError propagates exactly once. Ambiguous variadic and opaque callables use the
original signature. Callbacks are synchronous even in async polling; batch sync
callbacks must be thread safe. Partial batch results remain in JobPollSummary;
fail_fast retains existing semantics (sync waits for workers, async raises on failure).
This is not a promise that sibling operations are forcibly cancelled (#1686).

## Verification

HTTP MockTransport exercises public sync/async endpoint iteration/get/poll methods,
filter encoding, context restoration, API errors, empty batches and callback failure.
Static type fixtures verify native Iterator/AsyncIterator and JobStatus inference.
Existing endpoint composition, state-machine, workflow and Airflow tests remain.
