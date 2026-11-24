Retry Policy and Error Handling
===============================

:class:`~imednet.core.RetryPolicy` decouples retry decisions from the underlying tenacity logic
configured via :class:`~imednet.core.RetryConfig`.
The :class:`~imednet.core.DefaultRetryPolicy` implementation retries idempotent network and 5xx errors and HTTP 429 rate limits.
You can implement your own strategy by inspecting :class:`imednet.core.retry.RetryState`.

Handling exceptions
-------------------

Use typed exceptions to respond to different failure modes:

.. testcode::

   from imednet import ImednetSDK
   from imednet.errors import NotFoundError, RateLimitError, ServerError

   sdk = ImednetSDK()
   try:
       for study in sdk.studies.list():
           print(study.study_key)
   except RateLimitError:
       print("Too many requests, try again later")
   except NotFoundError as exc:
       print(f"Missing resource: {exc}")
   except ServerError as exc:
       print(f"Server error: {exc}")

Custom strategies
-----------------

Retry policies decide if a request should be retried before the executor converts
non-2xx responses into SDK exceptions. Inspect ``state.result`` for HTTP 429 and
5xx responses (alongside network-level ``state.exception`` failures):

.. testcode::

   from collections.abc import Iterator
   import httpx
   from imednet import ImednetSDK
   from imednet.core.retry import RetryConfig, RetryPolicy, RetryState
   from imednet.errors import RateLimitError, ServerError

   class RateLimitServerRetry(RetryPolicy):
       def should_retry(self, state: RetryState) -> bool:
           if isinstance(state.exception, (RateLimitError, ServerError, httpx.RequestError)):
               return True
           if isinstance(state.result, httpx.Response):
               status = state.result.status_code
               return status == 429 or 500 <= status < 600
           return False

   attempts: list[int] = []
   canned_responses: Iterator[httpx.Response] = iter(
       [
           httpx.Response(429, json={"metadata": {"status": "TOO_MANY_REQUESTS"}}),
           httpx.Response(500, json={"metadata": {"status": "SERVER_ERROR"}}),
           httpx.Response(
               200,
               json={
                   "metadata": {"status": "OK"},
                   "pagination": {
                       "currentPage": 0,
                       "size": 25,
                       "totalPages": 1,
                       "totalElements": 1,
                   },
                   "data": [{"studyKey": "STUDY", "studyName": "Retry Demo Study"}],
               },
           ),
       ]
   )

   def responder(_request: httpx.Request) -> httpx.Response:
       response = next(canned_responses)
       attempts.append(response.status_code)
       return response

   retry_sdk = ImednetSDK(
       api_key="A",
       security_key="B",
       retry_config=RetryConfig(
           retries=5,
           backoff_factor=0.01,
           retry_policy=RateLimitServerRetry(),
       ),
       transport=httpx.MockTransport(responder),
   )
   recovered_studies = list(retry_sdk.studies.list())
   assert attempts == [429, 500, 200]
   assert [s.study_key for s in recovered_studies] == ["STUDY"]

Logging and exponential backoff
-------------------------------

Each attempt is logged with JSON formatting. Configure logging globally via
:func:`imednet.utils.configure_json_logging`. Retries use exponential backoff with delays controlled
by ``retries`` and ``backoff_factor``. With ``retries=5`` and ``backoff_factor=0.5``
the wait times are 0.5 s, 1 s, 2 s, 4 s, and 8 s.

Circuit breakers, probe leases, and rate-limit classification
-------------------------------------------------------------

Every :class:`~imednet.sdk.ImednetSDK` and :class:`~imednet.sdk.AsyncImednetSDK` client owns an
isolated ``CircuitBreaker`` instance (configured on ``HTTPClientBase`` with
``failure_threshold=5``, ``recovery_timeout=10.0`` seconds, and
``half_open_max_probes=1``) so consecutive upstream faults on one ``Study`` client never
trip an independent client instance:

* **State transitions (``CircuitState``):** The breaker starts in ``CircuitState.CLOSED``.
  After ``failure_threshold`` consecutive network transport errors or HTTP ``5xx`` responses
  (``record_failure``), it transitions to ``CircuitState.OPEN`` and blocks new calls
  immediately for ``recovery_timeout`` seconds.
* **Bounded recovery probes (``ProbeLease``):** Once ``recovery_timeout`` elapses, the next
  caller transitions the breaker to ``CircuitState.HALF_OPEN`` and receives a ``ProbeLease``
  (blocking concurrent callers beyond ``half_open_max_probes``). A ``2xx`` response calls
  ``record_success`` and closes the circuit; a transport or ``5xx`` failure re-opens it. If
  a probe exits early (for example via task cancellation or a client ``4xx`` response),
  ``ProbeLease.release_if_unsettled`` releases the in-flight probe slot automatically so
  recovery never deadlocks.
* **HTTP ``429`` ``record_rate_limit`` vs ``5xx`` ``record_failure``:** While the breaker is
  ``CircuitState.CLOSED``, HTTP ``429`` responses invoke ``record_rate_limit()`` rather than
  ``record_failure()``, allowing ``Retry-After`` backoff to pace ``Subject``, ``Site``,
  ``Form``, ``Record``, ``Visit``, ``Interval``, ``Variable``, ``Query``, ``Data Dictionary``,
  and ``Job`` polling calls without opening a healthy circuit. If an HTTP ``429`` arrives
  during a ``CircuitState.HALF_OPEN`` recovery probe, ``record_rate_limit()`` immediately
  re-opens the breaker because the upstream endpoint is still saturated.

Diagnostic context, ``CircuitBreakerError``, and ``ApiError`` fields
--------------------------------------------------------------------

Transport and API exceptions attach a redacted :class:`~imednet.errors.DiagnosticContext`
(exposing ``method``, query-sanitized ``url``, ``retry_after``, ``attempt_count``, and
``cause_type``):

* Wire transport failures raise :class:`~imednet.errors.RequestError` with
  ``attempt_count >= 1`` and ``cause_type`` naming the underlying ``httpx.RequestError``.
* Open-circuit pre-flight blocks raise :class:`~imednet.errors.CircuitBreakerError`, which
  subclasses :class:`~imednet.errors.ImednetError` directly—**not**
  :class:`~imednet.errors.RequestError`—with ``attempt_count == 0``. Catch
  :class:`~imednet.errors.RequestError` when handling wire failures separately from open
  circuits, or catch :class:`~imednet.errors.ImednetError` to handle both uniformly.
* Non-2xx JSON error payloads raise :class:`~imednet.errors.ApiError` subclasses (such as
  :class:`~imednet.errors.BadRequestError`), normalizing single-object or multi-item
  ``metadata.error`` payloads into :attr:`~imednet.errors.ApiError.errors` while exposing
  the primary offending parameter via :attr:`~imednet.errors.ApiError.field_attribute` and
  redacted :attr:`~imednet.errors.ApiError.field_value`.

.. testcode::

   import httpx
   from imednet import ImednetSDK
   from imednet.core.retry import RetryConfig
   from imednet.errors import (
       ApiError,
       CircuitBreakerError,
       DiagnosticContext,
       ImednetError,
       RateLimitError,
       RequestError,
   )

   wire_calls: list[int] = []

   def failing_wire(request: httpx.Request) -> httpx.Response:
       wire_calls.append(1)
       raise httpx.ConnectError("upstream unreachable", request=request)

   def healthy_wire(_request: httpx.Request) -> httpx.Response:
       return httpx.Response(
           200,
           json={
               "metadata": {"status": "OK"},
               "pagination": {
                   "currentPage": 0,
                   "size": 25,
                   "totalPages": 1,
                   "totalElements": 1,
               },
               "data": [{"studyKey": "ISOLATED", "studyName": "Healthy Study"}],
           },
       )

   tripped_client = ImednetSDK(
       api_key="A",
       security_key="B",
       retry_config=RetryConfig(retries=0, backoff_factor=0.0),
       transport=httpx.MockTransport(failing_wire),
   )
   healthy_client = ImednetSDK(
       api_key="A",
       security_key="B",
       retry_config=RetryConfig(retries=0, backoff_factor=0.0),
       transport=httpx.MockTransport(healthy_wire),
   )

   for _ in range(5):
       try:
           list(tripped_client.studies.list())
       except RequestError as req_err:
           assert req_err.attempt_count == 1
           assert req_err.cause_type == "ConnectError"
           assert isinstance(req_err.diagnostic_context, DiagnosticContext)

   try:
       list(tripped_client.studies.list())
   except CircuitBreakerError as cb_err:
       assert isinstance(cb_err, ImednetError)
       assert not isinstance(cb_err, RequestError)
       assert cb_err.attempt_count == 0
       assert len(wire_calls) == 5

   # Per-client isolation: a separate SDK client remains CLOSED and operational
   assert [s.study_key for s in healthy_client.studies.list()] == ["ISOLATED"]

   # HTTP 429 in CLOSED state uses record_rate_limit() without opening the circuit
   rl_statuses = iter([429] * 6 + [200])

   def rate_limited_wire(_request: httpx.Request) -> httpx.Response:
       code = next(rl_statuses)
       if code == 429:
           return httpx.Response(429, json={"metadata": {"status": "TOO_MANY_REQUESTS"}})
       return httpx.Response(
           200,
           json={
               "metadata": {"status": "OK"},
               "pagination": {
                   "currentPage": 0,
                   "size": 25,
                   "totalPages": 1,
                   "totalElements": 1,
               },
               "data": [{"studyKey": "PACED", "studyName": "Paced Study"}],
           },
       )

   paced_client = ImednetSDK(
       api_key="A",
       security_key="B",
       retry_config=RetryConfig(retries=0, backoff_factor=0.0),
       transport=httpx.MockTransport(rate_limited_wire),
   )
   for _ in range(6):
       try:
           list(paced_client.studies.list())
       except RateLimitError:
           pass
   assert [s.study_key for s in paced_client.studies.list()] == ["PACED"]

   # ApiError normalizes metadata.error entries and exposes field_attribute / field_value
   def bad_request_wire(_request: httpx.Request) -> httpx.Response:
       return httpx.Response(
           400,
           json={
               "metadata": {
                   "status": "BAD_REQUEST",
                   "error": [
                       {
                           "code": "1000",
                           "description": "Invalid Variable filter",
                           "field": {"attribute": "variableName", "value": "BAD_VAR"},
                       },
                       {
                           "code": "1001",
                           "description": "Invalid page size",
                           "field": {"attribute": "size", "value": "9999"},
                       },
                   ],
               }
           },
       )

   validation_client = ImednetSDK(
       api_key="A",
       security_key="B",
       retry_config=RetryConfig(retries=0, backoff_factor=0.0),
       transport=httpx.MockTransport(bad_request_wire),
   )
   try:
       list(validation_client.variables.list("STUDY"))
   except ApiError as api_err:
       assert len(api_err.errors) == 2
       assert api_err.field_attribute == "variableName"
       assert api_err.field_value == "BAD_VAR"

Per-sink circuit breakers and zero-attempt ``ExportBatchError``
---------------------------------------------------------------

Destination sinks subclassing :class:`~imednet.spi.sink_base.ExportSink` maintain their own
per-sink ``CircuitBreaker`` instance inside ``ExportSink._execute_with_retry``. Transient
destination failures increment that sink's breaker until ``failure_threshold=5`` is reached,
without affecting other sink instances or HTTP API clients. Once a sink breaker is open,
subsequent batches fail immediately before calling the destination driver and raise
:class:`~imednet.errors.ExportBatchError` reporting ``0 attempts`` (preserving ``batch_id``
and chaining the sanitized :class:`~imednet.errors.CircuitBreakerError` as ``__cause__``):

.. testcode::

   from collections.abc import Sequence
   from typing import Any
   from imednet.errors import CircuitBreakerError, ExportBatchError
   from imednet.spi.sink_base import ExportSink, SinkConfig

   class DemoMemorySink(ExportSink):
       def __init__(self, config: SinkConfig, *, fail_writes: bool = False) -> None:
           super().__init__(config)
           self.fail_writes = fail_writes
           self.driver_calls = 0

       def write_batch(self, records: Sequence[Any], *, batch_id: str) -> int:
           def _write() -> int:
               self.driver_calls += 1
               if self.fail_writes:
                   raise ConnectionError("destination unavailable")
               return len(records)

           return self._execute_with_retry("write_batch", batch_id, _write)

       def flush(self) -> None:
           return None

       def close(self) -> None:
           return None

   unhealthy_sink = DemoMemorySink(
       SinkConfig(study_key="STUDY", max_retries=0, retry_backoff=0.0),
       fail_writes=True,
   )
   isolated_sink = DemoMemorySink(
       SinkConfig(study_key="STUDY", max_retries=0, retry_backoff=0.0),
       fail_writes=False,
   )

   for batch_idx in range(5):
       try:
           unhealthy_sink.write_batch([{"recordId": batch_idx}], batch_id=f"STUDY/F_VS/{batch_idx}")
       except ExportBatchError as attempt_err:
           assert "after 1 attempts" in str(attempt_err)

   try:
       unhealthy_sink.write_batch([{"recordId": 99}], batch_id="STUDY/F_VS/5")
   except ExportBatchError as blocked_batch_err:
       assert blocked_batch_err.batch_id == "STUDY/F_VS/5"
       assert "after 0 attempts" in str(blocked_batch_err)
       assert isinstance(blocked_batch_err.__cause__, CircuitBreakerError)
       assert unhealthy_sink.driver_calls == 5

   assert isolated_sink.write_batch([{"recordId": 1}], batch_id="STUDY/F_VS/0") == 1

Example script
--------------

The :doc:`/tutorials/examples/custom_retry` script demonstrates these concepts using a mock
transport:

.. code-block:: console

   export IMEDNET_API_KEY=dummy
   export IMEDNET_SECURITY_KEY=dummy
   hatch run python examples/custom_retry.py
