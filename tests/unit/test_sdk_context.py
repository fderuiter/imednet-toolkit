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


def test_sdk_context_scoping_close_nested_and_failed_init():
    """Verify SDK __init__ preserves ambient ContextVars and close/__exit__ scopes governance/strict contexts."""
    from imednet.core.context import (
        get_base_url_context,
        get_security_key_context,
        is_strict_mode,
        reset_base_url_context,
        reset_security_key_context,
        set_base_url_context,
        set_security_key_context,
    )

    prior_url = set_base_url_context("https://prior.example.test")
    prior_sec = set_security_key_context("prior-sec-key")  # pragma: allowlist secret
    try:
        # Failed SDK initialization leaves prior ContextVars untouched
        with pytest.raises(ConfigurationError):
            ImednetSDK(api_key="", security_key="")
        assert get_base_url_context() == "https://prior.example.test"
        assert get_security_key_context() == "prior-sec-key"  # pragma: allowlist secret

        # SDK __init__ never mutates ambient base_url or security_key ContextVars (#2158 invariant)
        outer = ImednetSDK(
            api_key="outer-key",  # pragma: allowlist secret
            security_key="outer-sec",  # pragma: allowlist secret
            base_url="https://outer.example.test",
            strict_mode=True,
        )
        assert get_base_url_context() == "https://prior.example.test"
        assert get_security_key_context() == "prior-sec-key"  # pragma: allowlist secret
        assert is_strict_mode() is False

        with outer:
            assert get_base_url_context() == "https://outer.example.test"
            assert get_security_key_context() == "outer-sec"  # pragma: allowlist secret
            assert is_strict_mode() is True
            # Nested same-instance `with outer:` preserves outer context on inner exit
            with outer:
                assert get_base_url_context() == "https://outer.example.test"
                assert is_strict_mode() is True
            assert get_base_url_context() == "https://outer.example.test"
            assert is_strict_mode() is True

            # Nested cross-instance `with inner:` overrides inside block and restores outer on exit
            with ImednetSDK(
                api_key="inner-key",  # pragma: allowlist secret
                security_key="inner-sec",  # pragma: allowlist secret
                base_url="https://inner.example.test",
                strict_mode=False,
            ):
                assert get_base_url_context() == "https://inner.example.test"
                assert get_security_key_context() == "inner-sec"  # pragma: allowlist secret
                assert is_strict_mode() is False
            assert get_base_url_context() == "https://outer.example.test"
            assert get_security_key_context() == "outer-sec"  # pragma: allowlist secret
            assert is_strict_mode() is True

        assert get_base_url_context() == "https://prior.example.test"
        assert get_security_key_context() == "prior-sec-key"  # pragma: allowlist secret
        assert is_strict_mode() is False

        # Manual __enter__() followed by explicit close() drains active tokens cleanly
        manual_sdk = ImednetSDK(
            api_key="manual-key",  # pragma: allowlist secret
            security_key="manual-sec",  # pragma: allowlist secret
            base_url="https://manual.example.test",
            strict_mode=True,
        )
        manual_sdk.__enter__()
        assert get_base_url_context() == "https://manual.example.test"
        assert is_strict_mode() is True
        manual_sdk.close()
        assert get_base_url_context() == "https://prior.example.test"
        assert is_strict_mode() is False

        # Injected pre-built Client propagates its base_url inside `with sdk:` without mutating on __init__
        injected_client = Client(
            api_key="inj-key",  # pragma: allowlist secret
            security_key="inj-sec",  # pragma: allowlist secret
            base_url="https://injected.example.test",
        )
        sdk_injected = ImednetSDK(client=injected_client)
        assert sdk_injected._base_url == "https://injected.example.test"
        assert get_base_url_context() == "https://prior.example.test"
        with sdk_injected:
            assert get_base_url_context() == "https://injected.example.test"
        assert get_base_url_context() == "https://prior.example.test"
    finally:
        reset_security_key_context(prior_sec)
        reset_base_url_context(prior_url)


@pytest.mark.asyncio
async def test_async_sdk_context_scoping_and_aclose():
    """Verify AsyncImednetSDK context scoping across async with, nested blocks, aclose(), and injected client."""
    from imednet.core.context import (
        get_base_url_context,
        is_strict_mode,
        reset_base_url_context,
        set_base_url_context,
    )

    prior_url = set_base_url_context("https://async-prior.example.test")
    try:
        sdk = AsyncImednetSDK(
            api_key="async-key",  # pragma: allowlist secret
            security_key="async-sec",  # pragma: allowlist secret
            base_url="https://async-sdk.example.test",
            strict_mode=True,
        )
        assert get_base_url_context() == "https://async-prior.example.test"
        assert is_strict_mode() is False

        async with sdk:
            assert get_base_url_context() == "https://async-sdk.example.test"
            assert is_strict_mode() is True
            async with sdk:
                assert get_base_url_context() == "https://async-sdk.example.test"
            assert get_base_url_context() == "https://async-sdk.example.test"
            assert is_strict_mode() is True

        assert get_base_url_context() == "https://async-prior.example.test"
        assert is_strict_mode() is False

        # Manual __aenter__() followed by explicit await aclose() drains tokens
        manual_async = AsyncImednetSDK(
            api_key="async-manual-key",  # pragma: allowlist secret
            security_key="async-manual-sec",  # pragma: allowlist secret
            base_url="https://async-manual.example.test",
            strict_mode=True,
        )
        await manual_async.__aenter__()
        assert get_base_url_context() == "https://async-manual.example.test"
        assert is_strict_mode() is True
        await manual_async.aclose()
        assert get_base_url_context() == "https://async-prior.example.test"
        assert is_strict_mode() is False

        # Injected AsyncClient syncs base_url to AsyncImednetSDK context manager
        injected_async = AsyncClient(
            api_key="async-inj-key",  # pragma: allowlist secret
            security_key="async-inj-sec",  # pragma: allowlist secret
            base_url="https://async-injected.example.test",
        )
        async with AsyncImednetSDK(async_client=injected_async) as wrapped_async:
            assert wrapped_async._base_url == "https://async-injected.example.test"
            assert get_base_url_context() == "https://async-injected.example.test"
        assert get_base_url_context() == "https://async-prior.example.test"
    finally:
        reset_base_url_context(prior_url)


def test_default_stores_follow_dynamic_home_redirection(tmp_path, monkeypatch):
    """Verify all default local SQLite/JSON stores resolve under the active HOME directory."""
    from pathlib import Path

    from imednet.integrations.parquet import _build_record_mapper
    from imednet_workflows.cached_loader import CachedRecordsLoader
    from imednet_workflows.config_version_control import (
        ConfigVersionStore,
        clear_config_version_store_caches,
    )
    from imednet_workflows.schema_profiler import SchemaProfiler
    from imednet_workflows.state_ledger import FileStateProvider, get_state_provider
    from imednet_workflows.triage_store import TriageStore

    custom_home = tmp_path / "custom_home"
    custom_home.mkdir()
    monkeypatch.setenv("HOME", str(custom_home))
    monkeypatch.setenv("USERPROFILE", str(custom_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: custom_home))
    monkeypatch.delenv("AIRFLOW_CTX_TASK_ID", raising=False)
    clear_config_version_store_caches()

    sdk = MagicMock()
    sdk._base_url = "https://home-test.example.test"
    sdk.get_records.return_value = []

    loader = CachedRecordsLoader(sdk)
    assert custom_home in loader.db_path.parents

    mapper = _build_record_mapper(sdk, chunk_size=100)
    assert mapper._loader is not None
    assert custom_home in mapper._loader.db_path.parents

    profiler = SchemaProfiler(sdk)
    assert list(profiler._iter_records("STUDY")) == []
    assert profiler._loader is not None
    assert custom_home in profiler._loader.db_path.parents

    cfg_store = ConfigVersionStore()
    assert custom_home in cfg_store.db_path.parents

    triage = TriageStore()
    assert custom_home in triage.db_path.parents

    ledger = FileStateProvider()
    assert custom_home in ledger.ledger_path.parents

    via_factory = get_state_provider()
    assert isinstance(via_factory, FileStateProvider)
    assert custom_home in via_factory.ledger_path.parents
