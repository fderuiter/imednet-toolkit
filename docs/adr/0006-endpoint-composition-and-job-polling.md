# Endpoint composition and shared job polling

**Status:** Accepted (temporary `ctx` constructor compatibility argument retired; `EdcSync*` / `EdcAsync*` bases standardized)

> **Supersession Note:** Operation descriptors (`ListOperation`, `FilterGetOperation`, `PathGetOperation`, `RecordCreateOperation`) and shared Job polling (`JobsEndpoint.poll`, `JobsEndpoint.poll_many`, `AsyncJobsEndpoint.poll`, `AsyncJobsEndpoint.poll_many`, `JobPoller`, `AsyncJobPoller`, and `evaluate_job_state`) remain accepted. Two transitional clauses in the first section below are **superseded**:
> 1. Concrete EDC endpoints now inherit dedicated `EdcSyncListEndpoint`, `EdcAsyncListEndpoint`, `EdcSyncGetEndpoint`, `EdcAsyncGetEndpoint`, `EdcSyncListGetEndpoint`, and `EdcAsyncListGetEndpoint` base classes (`imednet.core.endpoint.edc_mixin`, exported from `imednet.core`) built over `SyncListEndpoint`, `AsyncListEndpoint`, `SyncGetEndpoint`, `AsyncGetEndpoint`, `SyncListGetEndpoint`, and `AsyncListGetEndpoint` (`imednet.core.endpoint.base`).
> 2. The temporary ignored `ctx` compatibility parameter on `GenericEndpoint.__init__(self, client=None, async_client=None)` and concrete `Sync*`/`Async*` endpoint constructors has been removed; study scoping uses `sdk.study_context(...)` (`ContextVar` state) or explicit `study_key` method arguments instead.

## Decision and call trace

Retain the operation descriptors. A sync `records.list` call resolves parameters and
study context, constructs `ListOperation`, then returns its iterator using the sync
requester and paginator. The async descriptor returns an `AsyncIterator` directly;
`get` instead returns an awaitable. The transport alone owns retries (PR #1710).
Deleting descriptors would duplicate this dispatch for each endpoint without a
measured maintenance or type-inference improvement. Specialized path-based `JobsEndpoint.get`
continues to use `PathGetOperation` *(updated: concrete EDC endpoints standardize on
`EdcSync*` and `EdcAsync*` base classes in `imednet.core.endpoint.edc_mixin` rather than
ad-hoc mixin chains)*.

Add typed `JobsEndpoint.poll`/`poll_many` and async equivalents over the existing core
pollers. SDK `poll_job`/`async_poll_job` delegate here, workflows retain their aliases,
and Airflow's nonblocking sensor keeps `evaluate_job_state` from the same core seam.
Airflow owns its own poke scheduling; it must not block inside a full poll loop.
Keep `MultiStudyOrchestrator` and all old poller imports available. *(Superseded constructor
compatibility clause: this decision originally restored an ignored `ctx` compatibility argument
on concrete list/get constructors and `GenericEndpoint` with a deprecation warning while
rejecting the old three-positional constructor. That deprecation window has completed:
`GenericEndpoint(client=None, async_client=None)` and `EdcSync*`/`EdcAsync*` constructors
no longer accept `ctx`, raising `TypeError` if passed.)*

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
on `fail_fast=True`, sync pollers copy `ContextVar` state per worker and wake sleeping
siblings via a cooperative `threading.Event`, while async pollers cancel and await all
pending sibling `asyncio.Task` pollers before raising the triggering `JobFailedError`
or `JobTimeoutError` directly (without cancelling any server-side EDC Job).

## Verification

HTTP MockTransport exercises public sync/async endpoint iteration/get/poll methods,
filter encoding, context restoration, API errors, empty batches and callback failure.
Static type fixtures verify native Iterator/AsyncIterator and JobStatus inference.
Existing endpoint composition, state-machine, workflow and Airflow tests remain.
