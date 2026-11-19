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
   from imednet.errors import RateLimitError, ServerError, NotFoundError

   sdk = ImednetSDK()
   try:
       sdk.studies.list()
   except RateLimitError:
       print("Too many requests, try again later")
   except NotFoundError as exc:
       print(f"Missing resource: {exc}")
   except ServerError as exc:
       print(f"Server error: {exc}")

Custom strategies
-----------------

Retry policies decide if a request should be retried. To retry on
``RateLimitError`` and ``ServerError``:

.. testcode::

   from imednet import ImednetSDK
   from imednet.core.retry import RetryPolicy, RetryState
   from imednet.errors import RateLimitError, ServerError

   class RateLimitServerRetry(RetryPolicy):
       def should_retry(self, state: RetryState) -> bool:
           return isinstance(state.exception, (RateLimitError, ServerError))

   sdk = ImednetSDK(
       api_key="A",
       security_key="B",
       retries=5,
       backoff_factor=0.5,
       retry_policy=RateLimitServerRetry(),
   )

Logging and exponential backoff
-------------------------------

Each attempt is logged with JSON formatting. Configure logging globally via
:func:`imednet.utils.configure_json_logging`. Retries use exponential backoff with delays controlled
by ``retries`` and ``backoff_factor``. With ``retries=5`` and ``backoff_factor=0.5``
the wait times are 0.5 s, 1 s, 2 s, 4 s, and 8 s.

Example script
--------------

The :doc:`/tutorials/examples/custom_retry` script demonstrates these concepts using a mock
transport:

.. code-block:: console

   export IMEDNET_API_KEY=dummy
   export IMEDNET_SECURITY_KEY=dummy
   hatch run python examples/custom_retry.py
