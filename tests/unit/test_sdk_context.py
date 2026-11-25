"""Unit tests for sdk context."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from imednet.core.async_client import AsyncClient
from imednet.core.client import Client
from imednet.core.context import get_current_study, get_study_context
from imednet.errors.validation import ConfigurationError
from imednet.sdk import AsyncImednetSDK, ImednetSDK


# Mock environment variables to avoid API key requirement issues
@pytest.fixture(autouse=True)
def mock_env(monkeypatch):
    """Helper function to mock env."""
    monkeypatch.setenv("IMEDNET_API_KEY", "test_api_key")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "test_sec_key")


def test_sync_context_manager():
    """Test that sync context manager."""
    client_mock = MagicMock(spec=Client)

    with ImednetSDK(client=client_mock) as sdk:
        assert isinstance(sdk, ImednetSDK)

    client_mock.close.assert_called_once()


@pytest.mark.asyncio
async def test_async_context_manager():
    """Test that async context manager asynchronously."""
    async_client_mock = MagicMock(spec=AsyncClient)
    async_client_mock.aclose = AsyncMock()

    with patch("imednet.sdk.ClientFactory.create_async_client", return_value=async_client_mock):
        async with AsyncImednetSDK() as sdk:
            assert isinstance(sdk, AsyncImednetSDK)

    async_client_mock.aclose.assert_awaited_once()


def test_close_without_async_client():
    """Test that close without async client."""
    client_mock = MagicMock(spec=Client)
    sdk = ImednetSDK(client=client_mock)
    sdk.close()
    client_mock.close.assert_called_once()


def test_sync_sdk_does_not_create_async_client():
    """Test that sync sdk does not create async client."""
    client_mock = MagicMock(spec=Client)
    sdk = ImednetSDK(client=client_mock)
    assert not hasattr(sdk, "_async_client")


def test_async_sdk_close_raises_type_error():
    """AsyncImednetSDK.close() raises TypeError — sync close is forbidden."""
    with patch("imednet.sdk.load_config"):
        with patch("imednet.sdk.ClientFactory.create_client", return_value=MagicMock(spec=Client)):
            with patch(
                "imednet.sdk.ClientFactory.create_async_client",
                return_value=MagicMock(spec=AsyncClient),
            ):
                sdk = AsyncImednetSDK()
                with pytest.raises(TypeError, match="await sdk.aclose"):
                    sdk.close()


def test_async_sdk_sync_context_manager_raises_type_error():
    """Test that async sdk sync context manager raises type error."""
    with patch("imednet.sdk.load_config"):
        with patch("imednet.sdk.ClientFactory.create_client", return_value=MagicMock(spec=Client)):
            with patch(
                "imednet.sdk.ClientFactory.create_async_client",
                return_value=MagicMock(spec=AsyncClient),
            ):
                sdk = AsyncImednetSDK()
                with pytest.raises(TypeError, match="async with AsyncImednetSDK"):
                    with sdk:
                        pass  # pragma: no cover


def test_async_sdk_exit_raises_type_error():
    """__exit__ on AsyncImednetSDK raises TypeError when called directly."""
    with patch("imednet.sdk.load_config"):
        with patch("imednet.sdk.ClientFactory.create_client", return_value=MagicMock(spec=Client)):
            with patch(
                "imednet.sdk.ClientFactory.create_async_client",
                return_value=MagicMock(spec=AsyncClient),
            ):
                sdk = AsyncImednetSDK()
                with pytest.raises(TypeError, match="async with AsyncImednetSDK"):
                    sdk.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_aclose():
    """Test that aclose asynchronously."""
    async_client_mock = MagicMock(spec=AsyncClient)
    async_client_mock.aclose = AsyncMock()

    sdk = AsyncImednetSDK(async_client=async_client_mock)

    await sdk.aclose()

    async_client_mock.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_aclose_without_async_client():
    """Test that aclose without async client asynchronously."""
    client_mock = MagicMock(spec=Client)

    sdk = ImednetSDK(client=client_mock)

    with pytest.raises(TypeError, match="synchronous client"):
        await sdk.aclose()

    client_mock.close.assert_not_called()


def test_study_context_manager_sets_and_resets_context():
    """Test that study context manager sets and resets context."""
    client_mock = MagicMock(spec=Client)
    sdk = ImednetSDK(client=client_mock)

    with sdk.study_context("study-key-123"):
        assert get_current_study() == "study-key-123"

    with pytest.raises(ConfigurationError):
        get_current_study()


def test_study_context_manager_resets_on_exception():
    """Test that study context manager resets on exception."""
    client_mock = MagicMock(spec=Client)
    sdk = ImednetSDK(client=client_mock)

    with pytest.raises(ValueError), sdk.study_context("study-key-123"):
        raise ValueError("boom")

    with pytest.raises(ConfigurationError):
        get_current_study()


def test_default_study_mutation_methods_removed():
    """Test that default study mutation methods removed."""
    client_mock = MagicMock(spec=Client)
    sdk = ImednetSDK(client=client_mock)
    assert not hasattr(sdk, "set_default_study")
    assert not hasattr(sdk, "clear_default_study")


def test_retry_policy_property():
    """Test that retry policy property."""
    async_client_mock = MagicMock(spec=AsyncClient)

    sdk = AsyncImednetSDK(async_client=async_client_mock)

    policy = MagicMock()

    # Test setter
    sdk.retry_policy = policy
    assert async_client_mock.retry_policy == policy

    # Test getter
    async_client_mock.retry_policy = policy
    assert sdk.retry_policy == policy


def test_retry_policy_property_without_async_client():
    """Test that retry policy property without async client."""
    client_mock = MagicMock(spec=Client)
    sdk = ImednetSDK(client=client_mock)

    policy = MagicMock()

    # Test setter when async_client is None
    sdk.retry_policy = policy
    assert client_mock.retry_policy == policy


@pytest.mark.asyncio
async def test_study_context_isolation_on_shared_sdk_instance():
    """Test that study context isolation on shared sdk instance asynchronously."""
    shared_sdk = ImednetSDK(client=MagicMock(spec=Client))

    async def worker(study_key: str, delay: float) -> str:
        """Helper function to worker."""
        with shared_sdk.study_context(study_key):
            await asyncio.sleep(delay)
            return get_current_study()

    results = await asyncio.gather(
        worker("STUDY_A", 0.2),
        worker("STUDY_B", 0.1),
        worker("STUDY_C", 0.3),
    )
    assert results == ["STUDY_A", "STUDY_B", "STUDY_C"]
    assert get_study_context() is None


@pytest.mark.asyncio
async def test_async_sdk_aenter_aexit():
    """Test that async sdk aenter aexit asynchronously."""
    async_client_mock = MagicMock(spec=AsyncClient)
    async_client_mock.aclose = AsyncMock()

    with patch("imednet.sdk.load_config"):
        with patch("imednet.sdk.ClientFactory.create_async_client", return_value=async_client_mock):
            async with AsyncImednetSDK() as sdk:
                assert isinstance(sdk, AsyncImednetSDK)

            async_client_mock.aclose.assert_awaited_once()


def test_async_sdk_sync_init():
    """Test that async sdk sync init."""
    async_client_mock = MagicMock(spec=AsyncClient)

    with patch("imednet.sdk.load_config"):
        with patch("imednet.sdk.ClientFactory.create_async_client", return_value=async_client_mock):
            sdk = AsyncImednetSDK()
            assert sdk._async_client == async_client_mock


@pytest.mark.asyncio
async def test_sync_sdk_rejects_async_context():
    """ImednetSDK.__aenter__ raises TypeError — async with is forbidden."""
    client_mock = MagicMock(spec=Client)
    sdk = ImednetSDK(client=client_mock)
    with pytest.raises(TypeError, match="synchronous client"):
        await sdk.__aenter__()


@pytest.mark.asyncio
async def test_sync_sdk_rejects_async_aexit():
    """ImednetSDK.__aexit__ raises TypeError when called directly."""
    client_mock = MagicMock(spec=Client)
    sdk = ImednetSDK(client=client_mock)
    with pytest.raises(TypeError, match="synchronous client"):
        await sdk.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_sync_sdk_rejects_async_context_via_async_with():
    """'async with ImednetSDK(...)' raises TypeError before entering the block."""
    client_mock = MagicMock(spec=Client)
    sdk = ImednetSDK(client=client_mock)
    with pytest.raises(TypeError, match="synchronous client"):
        async with sdk:
            pass  # pragma: no cover


def test_imednet_sdk_close_and_context_exit_restore_base_url_context():
    """Closing or exiting ImednetSDK restores get_base_url_context() to its prior value."""
    from imednet.core.context import get_base_url_context, set_base_url_context

    assert get_base_url_context() is None

    sdk_direct = ImednetSDK(base_url="https://direct.imednet.test")
    assert get_base_url_context() == "https://direct.imednet.test"
    sdk_direct.close()
    assert get_base_url_context() is None
    # Idempotent repeated close does not fail or corrupt context
    sdk_direct.close()
    assert get_base_url_context() is None

    prior_token = set_base_url_context("https://prior.imednet.test")
    try:
        with ImednetSDK(base_url="https://outer.imednet.test", strict_mode=True):
            assert get_base_url_context() == "https://outer.imednet.test"
            with ImednetSDK(base_url="https://inner.imednet.test", strict_mode=False):
                assert get_base_url_context() == "https://inner.imednet.test"
            assert get_base_url_context() == "https://outer.imednet.test"
        assert get_base_url_context() == "https://prior.imednet.test"
    finally:
        from imednet.core.context import reset_base_url_context

        reset_base_url_context(prior_token)


@pytest.mark.asyncio
async def test_async_imednet_sdk_aclose_and_aexit_restore_base_url_context():
    """Closing or exiting AsyncImednetSDK restores get_base_url_context() to its prior value."""
    from imednet.core.context import (
        get_base_url_context,
        reset_base_url_context,
        set_base_url_context,
    )

    assert get_base_url_context() is None

    sdk_direct = AsyncImednetSDK(base_url="https://async-direct.imednet.test")
    assert get_base_url_context() == "https://async-direct.imednet.test"
    await sdk_direct.aclose()
    assert get_base_url_context() is None

    prior_token = set_base_url_context("https://async-prior.imednet.test")
    try:
        async with AsyncImednetSDK(base_url="https://async-outer.imednet.test"):
            assert get_base_url_context() == "https://async-outer.imednet.test"
            async with AsyncImednetSDK(base_url="https://async-inner.imednet.test"):
                assert get_base_url_context() == "https://async-inner.imednet.test"
            assert get_base_url_context() == "https://async-outer.imednet.test"
        assert get_base_url_context() == "https://async-prior.imednet.test"
    finally:
        reset_base_url_context(prior_token)


def test_sdk_init_failure_restores_base_url_context():
    """If SDK initialization fails after _init_sdk_config, _active_base_url is restored."""
    from imednet.core.context import (
        get_base_url_context,
        reset_base_url_context,
        set_base_url_context,
    )

    token = set_base_url_context("https://before-error.imednet.test")
    try:
        with (
            patch(
                "imednet.sdk.ClientFactory.create_client",
                side_effect=RuntimeError("transport init boom"),
            ),
            pytest.raises(RuntimeError, match="transport init boom"),
        ):
            ImednetSDK(base_url="https://failed-init.imednet.test")
        assert get_base_url_context() == "https://before-error.imednet.test"
    finally:
        reset_base_url_context(token)


def test_dynamic_home_redirects_default_workflow_and_parquet_caches(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Dynamic HOME overrides redirect ConfigVersionStore, CachedRecordsLoader, parquet, and SchemaProfiler."""
    from pathlib import Path

    from imednet.integrations.parquet import _build_record_mapper
    from imednet_workflows.cached_loader import CachedRecordsLoader
    from imednet_workflows.config_version_control import (
        ConfigVersionStore,
        clear_config_version_store_caches,
    )
    from imednet_workflows.schema_profiler import SchemaProfiler
    from imednet_workflows.triage_store import TriageStore

    custom_home = tmp_path / "isolated-home"
    custom_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(custom_home))
    monkeypatch.setenv("USERPROFILE", str(custom_home))
    monkeypatch.delenv("IMEDNET_CONFIG_DB_PATH", raising=False)
    monkeypatch.delenv("IMEDNET_TRIAGE_DB_PATH", raising=False)
    clear_config_version_store_caches()

    with ImednetSDK(base_url="https://cache-check.imednet.test") as sdk:
        loader = CachedRecordsLoader(sdk)
        assert Path(loader.db_path).is_relative_to(custom_home / ".imednet" / "cache")

        mapper = _build_record_mapper(sdk, chunk_size=10)
        assert Path(mapper._loader.db_path).is_relative_to(custom_home / ".imednet" / "cache")

        profiler = SchemaProfiler(sdk)
        sdk.get_records = MagicMock(return_value=[])  # type: ignore[method-assign]
        list(profiler._iter_records("STUDY_ISO"))
        assert profiler._loader is not None
        assert Path(profiler._loader.db_path).is_relative_to(custom_home / ".imednet" / "cache")

        store = ConfigVersionStore()
        assert Path(store.db_path).is_relative_to(custom_home / ".imednet")

        triage = TriageStore()
        assert Path(triage.db_path).is_relative_to(custom_home / ".imednet")


@pytest.mark.asyncio
async def test_injected_client_base_url_context_restores_on_close(
    monkeypatch: pytest.MonkeyPatch,
):
    """Injected sync/async clients with base_url update and restore _active_base_url on close."""
    from imednet.core.async_client import AsyncClient
    from imednet.core.client import Client
    from imednet.core.context import (
        get_base_url_context,
        reset_base_url_context,
        set_base_url_context,
    )

    monkeypatch.delenv("IMEDNET_BASE_URL", raising=False)
    prior = set_base_url_context("https://outer-prior.imednet.test")
    try:
        sync_client = Client("k", "s", base_url="https://injected-sync.imednet.test")
        with ImednetSDK(client=sync_client):
            assert get_base_url_context() == "https://injected-sync.imednet.test"
        assert get_base_url_context() == "https://outer-prior.imednet.test"

        async_client = AsyncClient("k", "s", base_url="https://injected-async.imednet.test")
        async with AsyncImednetSDK(async_client=async_client):
            assert get_base_url_context() == "https://injected-async.imednet.test"
        assert get_base_url_context() == "https://outer-prior.imednet.test"
    finally:
        reset_base_url_context(prior)
