"""Offline auth and credential-source acceptance through the real constructors."""

import httpx
import pytest

from imednet.auth import ApiKeyAuth, Credentials, MemoryCredentialStore, OIDCAuth
from imednet.config import load_config
from imednet.core.async_client import AsyncClient
from imednet.core.base_client import BaseClient
from imednet.core.client import Client
from imednet.sdk import AsyncImednetSDK, ImednetSDK


@pytest.fixture(autouse=True)
def clean_auth_environment(monkeypatch):
    for key in (
        "IMEDNET_API_KEY",
        "IMEDNET_SECURITY_KEY",
        "IMEDNET_OIDC_TOKEN",
        "IMEDNET_SIMULATION_MODE",
    ):
        monkeypatch.delenv(key, raising=False)


class SyntheticAuth:
    def __bool__(self):
        return False

    def get_headers(self):
        return {"X-Synthetic-Auth": "offline"}

    def get_user_roles(self):
        return []

    def get_user_id(self):
        return "synthetic"


def transport():
    def respond(request):
        assert request.headers["X-Synthetic-Auth"] == "offline"
        assert "x-api-key" not in request.headers
        return httpx.Response(200, json={"data": [], "totalPages": 1})

    return httpx.MockTransport(respond)


def test_explicit_strategy_initializes_sync_client_and_sdk_without_keys():
    auth = SyntheticAuth()
    with Client(auth=auth, transport=transport()) as client:
        assert isinstance(client, BaseClient)
        assert client.auth is auth
        assert client.get("/studies").json()["data"] == []
    assert client._client.is_closed
    with ImednetSDK(auth=auth, transport=transport()) as sdk:
        assert sdk.auth is auth
        assert list(sdk.studies.list()) == []
    assert sdk._client._client.is_closed


async def test_explicit_strategy_initializes_async_client_and_sdk_without_keys():
    auth = SyntheticAuth()
    async with AsyncClient(auth=auth, transport=transport()) as client:
        assert client.auth is auth
        assert (await client.get("/studies")).json()["data"] == []
    assert client._client.is_closed
    async with AsyncImednetSDK(auth=auth, transport=transport()) as sdk:
        assert sdk.auth is auth
        assert [item async for item in sdk.studies.list()] == []
    assert sdk._async_client._client.is_closed


def test_source_overwrite_clear_and_environment_fallback(monkeypatch):
    monkeypatch.setenv("IMEDNET_API_KEY", "environment-api")
    monkeypatch.setenv("IMEDNET_SECURITY_KEY", "environment-security")
    store = MemoryCredentialStore(Credentials("source-api", "source-security"))
    assert load_config(credential_source=store).api_key == "source-api"
    assert load_config(api_key="explicit", credential_source=store).api_key == "explicit"
    store.set_credentials(Credentials("replacement", "replacement-security"))
    assert load_config(credential_source=store).api_key == "replacement"
    store.clear()
    assert load_config(credential_source=store).api_key == "environment-api"
    with pytest.raises(ValueError, match="API key is required"):
        load_config(api_key="", credential_source=store)


def test_sources_and_auth_representations_are_redacted():
    credentials = Credentials("synthetic-secret-a", "synthetic-secret-b", "synthetic-secret-c")
    for obj in [
        credentials,
        MemoryCredentialStore(credentials),
        ApiKeyAuth("synthetic-secret-a", "synthetic-secret-b"),
        OIDCAuth("synthetic-secret-c"),
    ]:
        assert "synthetic-secret" not in repr(obj)


def test_source_failure_is_generic_and_invalid_snapshot_rejected():
    class BrokenSource:
        def get_credentials(self):
            raise RuntimeError("synthetic-secret-do-not-log")

    with pytest.raises(ValueError, match="Credential source lookup failed") as exc:
        load_config(credential_source=BrokenSource())
    assert "synthetic-secret" not in str(exc.value)
    assert exc.value.__suppress_context__

    class InvalidSource:
        def get_credentials(self):
            return "invalid"

    with pytest.raises(ValueError, match="invalid snapshot"):
        load_config(credential_source=InvalidSource())


def test_strategy_skips_source_lookup_and_failure_closes_transport():
    class BrokenSource:
        def get_credentials(self):
            pytest.fail("Strategy must not consult credential sources")

    with pytest.raises(RuntimeError, match="consumer failed"):
        with ImednetSDK(
            auth=SyntheticAuth(), credential_source=BrokenSource(), transport=transport()
        ) as sdk:
            raise RuntimeError("consumer failed")
    assert sdk._client._client.is_closed


async def test_injected_client_ownership_and_conflicting_strategy():
    client = Client(auth=SyntheticAuth(), transport=transport())
    with pytest.raises(ValueError, match="already owns authentication"):
        ImednetSDK(client=client, auth=SyntheticAuth())
    assert not client._client.is_closed
    with ImednetSDK(client=client):
        pass
    assert client._client.is_closed
    client_async = AsyncClient(auth=SyntheticAuth(), transport=transport())
    async with AsyncImednetSDK(async_client=client_async):
        pass
    assert client_async._client.is_closed


def test_oidc_default_and_source_snapshot_lifetime():
    store = MemoryCredentialStore(Credentials(oidc_token="offline-oidc-token"))
    with ImednetSDK(credential_source=store) as sdk:
        assert isinstance(sdk.auth, OIDCAuth)
        store.clear()
        assert sdk.auth.get_headers() == {"Authorization": "Bearer offline-oidc-token"}
    assert store.get_credentials() is None
    with Client(auth=OIDCAuth("offline-oidc-token")) as client:
        assert isinstance(client.auth, OIDCAuth)
