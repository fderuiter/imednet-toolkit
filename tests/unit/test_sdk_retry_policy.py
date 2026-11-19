"""Unit tests for sdk retry policy."""

import httpx
import pytest
import respx

from imednet import errors
from imednet.constants import DEFAULT_BACKOFF_FACTOR, DEFAULT_RETRIES
from imednet.core.operations.circuit_breaker import get_global_circuit_breaker
from imednet.core.operations.executor import DefaultOperationRetryPolicy
from imednet.core.retry import DefaultRetryPolicy, RetryConfig, RetryPolicy, RetryState
from imednet.endpoints.registry import ASYNC_ENDPOINT_REGISTRY, ENDPOINT_REGISTRY
from imednet.sdk import AsyncImednetSDK, ImednetSDK


class NamedPolicy(RetryPolicy):
    """Test suite for NamedPolicy."""

    def __init__(self, name: str) -> None:
        """Initialize the test object."""
        self.name = name

    def should_retry(self, state: RetryState) -> bool:  # pragma: no cover - simple
        """Helper function to should retry."""
        return False


@pytest.fixture
def async_sdk() -> AsyncImednetSDK:
    """Helper function to async sdk."""
    return AsyncImednetSDK(
        api_key="key",
        security_key="secret",
        base_url="https://example.com",
    )


@pytest.fixture
def respx_mock_external():
    """Helper function to respx mock external."""
    with respx.mock(base_url="https://example.com") as mock:
        yield mock


def test_initial_retry_policy_propagates_to_async_client() -> None:
    """Test that initial retry policy propagates to async client."""
    policy = NamedPolicy("init")
    sdk = AsyncImednetSDK(
        api_key="key",
        security_key="secret",
        base_url="https://example.com",
        retry_config=RetryConfig(retry_policy=policy),
    )

    assert sdk._async_client.retry_policy is policy


def test_retry_policy_propagates_to_async_client(async_sdk: AsyncImednetSDK) -> None:
    """Test that retry policy propagates to async client."""
    async_policy = NamedPolicy("async")
    async_sdk._async_client.retry_policy = async_policy

    new_policy = NamedPolicy("new")
    async_sdk.retry_policy = new_policy

    assert async_sdk._async_client.retry_policy is new_policy


def test_default_retry_policy_retries_connection_error(respx_mock_external) -> None:
    """Verify that connection errors trigger retries."""
    sdk = ImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://example.com",
        retry_config=RetryConfig(retries=2),
    )
    # Simulate a persistent connection error
    route = respx_mock_external.get("/test").mock(
        side_effect=httpx.ConnectError("Network Error", request=None)
    )

    # Note: RequestExecutor configures tenacity with reraise=False, so the underlying
    # httpx.ConnectError is wrapped in errors.RequestError.
    with pytest.raises(errors.RequestError) as exc_info:
        sdk._client.get("/test")

    assert isinstance(exc_info.value.__cause__, httpx.ConnectError)

    # Should attempt 3 times (default retry_config=RetryConfig(retries=2))
    assert route.call_count == 3


def test_default_retry_policy_retries_on_500(respx_mock_external) -> None:
    """Verify that server errors (500) trigger retries."""
    sdk = ImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://example.com",
        retry_config=RetryConfig(retries=2),
    )
    route = respx_mock_external.get("/test").mock(return_value=httpx.Response(500))

    with pytest.raises(errors.ServerError):
        sdk._client.get("/test")

    # Should attempt 3 times
    assert route.call_count == 3


def test_default_retry_policy_retries_on_429(respx_mock_external) -> None:
    """Verify that rate limit errors (429) trigger retries."""
    sdk = ImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://example.com",
        retry_config=RetryConfig(retries=2),
    )
    route = respx_mock_external.get("/test").mock(return_value=httpx.Response(429))

    with pytest.raises(errors.RateLimitError):
        sdk._client.get("/test")

    # Should attempt 3 times
    assert route.call_count == 3


@pytest.mark.asyncio
async def test_default_retry_policy_async_retries_on_500(respx_mock_external) -> None:
    """Verify that server errors (500) trigger retries in async client."""
    sdk = AsyncImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://example.com",
        retry_config=RetryConfig(retries=2),
    )

    route = respx_mock_external.get("/test").mock(return_value=httpx.Response(500))

    with pytest.raises(errors.ServerError):
        await sdk._async_client.get("/test")

    # Should attempt 3 times
    assert route.call_count == 3


def test_default_retry_policy_post_does_not_retry_on_server_error(respx_mock_external) -> None:
    """Verify that POST requests receiving 5xx do NOT retry (non-idempotent)."""
    sdk = ImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://example.com",
        retry_config=RetryConfig(retries=2),
    )
    route = respx_mock_external.post("/test").mock(return_value=httpx.Response(503))

    with pytest.raises(errors.ServerError):
        sdk._client.post("/test", json={})

    # Must fail fast: only one attempt, no retries
    assert route.call_count == 1


def test_default_retry_policy_post_retries_on_rate_limit(respx_mock_external) -> None:
    """Verify that POST requests receiving 429 DO retry (server rejected before processing)."""
    sdk = ImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://example.com",
        retry_config=RetryConfig(retries=2),
    )
    route = respx_mock_external.post("/test").mock(return_value=httpx.Response(429))

    with pytest.raises(errors.RateLimitError):
        sdk._client.post("/test", json={})

    # Should attempt 3 times (rate limit is safe to retry for any method)
    assert route.call_count == 3


def test_retry_config_normalizes_none_policy_and_keeps_operation_policy() -> None:
    """Verify RetryConfig replaces retry_policy=None while preserving OperationRetryPolicy."""
    cfg_none = RetryConfig(retries=2, backoff_factor=0.5, retry_policy=None)
    assert isinstance(cfg_none.retry_policy, DefaultRetryPolicy)
    assert cfg_none.retries == 2
    assert cfg_none.backoff_factor == 0.5

    op_policy = DefaultOperationRetryPolicy()
    cfg_op = RetryConfig(retry_policy=op_policy)
    assert cfg_op.retry_policy is op_policy


def test_sync_sdk_retries_kwarg_preserves_default_policy_on_200_and_500(
    respx_mock_external,
) -> None:
    """Verify ImednetSDK(retries=2) succeeds on 200 OK and retries 500 without AttributeError."""
    sdk = ImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://example.com",
        retries=2,
        backoff_factor=0.0,
    )
    assert isinstance(sdk.retry_policy, DefaultRetryPolicy)
    assert sdk._client.retry_config.retries == 2
    assert sdk._client.retry_config.backoff_factor == 0.0
    for attr in ENDPOINT_REGISTRY:
        assert getattr(sdk, attr)._client.retry_policy is sdk.retry_policy

    ok_route = respx_mock_external.get("/api/v1/edc/studies").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"studyKey": "STUDY1"}], "pagination": {"totalPages": 1}},
        )
    )
    studies = list(sdk.studies.list())
    assert len(studies) == 1
    assert studies[0].study_key == "STUDY1"
    assert ok_route.call_count == 1
    assert get_global_circuit_breaker()._consecutive_failures == 0

    err_route = respx_mock_external.get("/fail").mock(return_value=httpx.Response(500))
    with pytest.raises(errors.ServerError):
        sdk._client.get("/fail")
    assert err_route.call_count == 3
    sdk.close()


@pytest.mark.asyncio
async def test_async_sdk_retries_kwarg_preserves_default_policy_on_200_and_500(
    respx_mock_external,
) -> None:
    """Verify AsyncImednetSDK(retries=2) succeeds on 200 OK and retries 500."""
    async with AsyncImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://example.com",
        retries=2,
        backoff_factor=0.0,
    ) as sdk:
        assert isinstance(sdk.retry_policy, DefaultRetryPolicy)
        assert sdk._async_client.retry_config.retries == 2
        assert sdk._async_client.retry_config.backoff_factor == 0.0
        for attr in ASYNC_ENDPOINT_REGISTRY:
            assert getattr(sdk, attr)._async_client.retry_policy is sdk.retry_policy

        ok_route = respx_mock_external.get("/api/v1/edc/studies").mock(
            return_value=httpx.Response(
                200,
                json={"data": [{"studyKey": "STUDY1"}], "pagination": {"totalPages": 1}},
            )
        )
        studies = [s async for s in sdk.studies.list()]
        assert len(studies) == 1
        assert studies[0].study_key == "STUDY1"
        assert ok_route.call_count == 1
        assert get_global_circuit_breaker()._consecutive_failures == 0

        err_route = respx_mock_external.get("/fail").mock(return_value=httpx.Response(500))
        with pytest.raises(errors.ServerError):
            await sdk._async_client.get("/fail")
        assert err_route.call_count == 3


def test_sync_sdk_zero_retries_executes_single_attempt(respx_mock_external) -> None:
    """Verify ImednetSDK(retries=0) executes exactly one attempt and preserves DefaultRetryPolicy."""
    with ImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://example.com",
        retries=0,
    ) as sdk:
        assert isinstance(sdk.retry_policy, DefaultRetryPolicy)
        assert sdk._client.retry_config.retries == 0
        assert sdk._client.retry_config.backoff_factor == DEFAULT_BACKOFF_FACTOR

        ok_route = respx_mock_external.get("/ok").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        resp = sdk._client.get("/ok")
        assert resp.status_code == 200
        assert ok_route.call_count == 1

        err_route = respx_mock_external.get("/fail").mock(return_value=httpx.Response(500))
        with pytest.raises(errors.ServerError):
            sdk._client.get("/fail")
        assert err_route.call_count == 1


@pytest.mark.asyncio
async def test_async_sdk_zero_retries_executes_single_attempt(respx_mock_external) -> None:
    """Verify AsyncImednetSDK(retries=0) executes one attempt and preserves DefaultRetryPolicy."""
    async with AsyncImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://example.com",
        retries=0,
    ) as sdk:
        assert isinstance(sdk.retry_policy, DefaultRetryPolicy)
        assert sdk._async_client.retry_config.retries == 0
        assert sdk._async_client.retry_config.backoff_factor == DEFAULT_BACKOFF_FACTOR

        ok_route = respx_mock_external.get("/ok").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        resp = await sdk._async_client.get("/ok")
        assert resp.status_code == 200
        assert ok_route.call_count == 1

        err_route = respx_mock_external.get("/fail").mock(return_value=httpx.Response(500))
        with pytest.raises(errors.ServerError):
            await sdk._async_client.get("/fail")
        assert err_route.call_count == 1


@pytest.mark.parametrize("backoff_factor", [0.0, 0.5])
def test_sync_sdk_backoff_factor_only_preserves_default_policy_and_retries(
    respx_mock_external,
    monkeypatch: pytest.MonkeyPatch,
    backoff_factor: float,
) -> None:
    """Verify ImednetSDK(backoff_factor=...) preserves DefaultRetryPolicy and DEFAULT_RETRIES."""
    sleeps: list[float] = []
    monkeypatch.setattr("tenacity.nap.time.sleep", sleeps.append)

    with ImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://example.com",
        backoff_factor=backoff_factor,
    ) as sdk:
        assert isinstance(sdk.retry_policy, DefaultRetryPolicy)
        assert sdk._client.retry_config.retries == DEFAULT_RETRIES
        assert sdk._client.retry_config.backoff_factor == backoff_factor

        route = respx_mock_external.get("/fail").mock(return_value=httpx.Response(503))
        with pytest.raises(errors.ServerError):
            sdk._client.get("/fail")
        assert route.call_count == DEFAULT_RETRIES + 1
        assert len(sleeps) == DEFAULT_RETRIES


@pytest.mark.asyncio
@pytest.mark.parametrize("backoff_factor", [0.0, 0.5])
async def test_async_sdk_backoff_factor_only_preserves_default_policy_and_retries(
    respx_mock_external,
    monkeypatch: pytest.MonkeyPatch,
    backoff_factor: float,
) -> None:
    """Verify AsyncImednetSDK(backoff_factor=...) preserves DefaultRetryPolicy and DEFAULT_RETRIES."""
    sleeps: list[float] = []

    async def fake_async_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("asyncio.sleep", fake_async_sleep)

    async with AsyncImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://example.com",
        backoff_factor=backoff_factor,
    ) as sdk:
        assert isinstance(sdk.retry_policy, DefaultRetryPolicy)
        assert sdk._async_client.retry_config.retries == DEFAULT_RETRIES
        assert sdk._async_client.retry_config.backoff_factor == backoff_factor

        route = respx_mock_external.get("/fail").mock(return_value=httpx.Response(503))
        with pytest.raises(errors.ServerError):
            await sdk._async_client.get("/fail")
        assert route.call_count == DEFAULT_RETRIES + 1
        assert len(sleeps) == DEFAULT_RETRIES


@pytest.mark.asyncio
async def test_explicit_retry_config_takes_precedence_over_sdk_retry_kwargs() -> None:
    """Verify explicit retry_config overrides retries, backoff_factor, and retry_policy kwargs."""
    explicit_policy = NamedPolicy("explicit")
    other_policy = NamedPolicy("ignored")
    explicit_cfg = RetryConfig(retries=1, backoff_factor=0.25, retry_policy=explicit_policy)

    with ImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://example.com",
        retry_config=explicit_cfg,
        retries=5,
        backoff_factor=2.0,
        retry_policy=other_policy,
    ) as sync_sdk:
        assert sync_sdk._client.retry_config is explicit_cfg
        assert sync_sdk.retry_policy is explicit_policy

    async with AsyncImednetSDK(
        api_key="k",
        security_key="s",
        base_url="https://example.com",
        retry_config=explicit_cfg,
        retries=5,
        backoff_factor=2.0,
        retry_policy=other_policy,
    ) as async_sdk:
        assert async_sdk._async_client.retry_config is explicit_cfg
        assert async_sdk.retry_policy is explicit_policy
