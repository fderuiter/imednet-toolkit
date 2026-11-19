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


def test_oidc_rejects_unsigned_and_forged_admin_tokens():
    import base64
    import json

    # 1. Exact downstream agent verification check: none algorithm with unsigned signature
    payload = json.dumps({"sub": "attacker", "roles": ["admin"]})
    b64_payload = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    unverified_jwt = f"eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.{b64_payload}.unsigned_sig"

    auth = OIDCAuth(unverified_jwt)
    assert "admin" not in auth.get_user_roles(), "Unsigned claims must not grant admin role"
    assert auth.get_user_roles() == []
    assert auth.get_user_id() is None

    # 2. Token with missing signature segment
    empty_sig_jwt = f"eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.{b64_payload}."
    auth_empty = OIDCAuth(empty_sig_jwt)
    assert auth_empty.get_user_roles() == []

    # 3. RS256 token without configured public key or JWKS
    h_rs = (
        base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        .decode()
        .rstrip("=")
    )
    forged_rs_jwt = f"{h_rs}.{b64_payload}.fake_signature"
    auth_forged_rs = OIDCAuth(forged_rs_jwt)
    assert auth_forged_rs.get_user_roles() == []
    assert auth_forged_rs.get_user_id() is None

    # 4. HS256 token with forged/wrong signature
    h_hs = (
        base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        .decode()
        .rstrip("=")
    )
    forged_hs_jwt = f"{h_hs}.{b64_payload}.fake_signature"
    auth_forged_hs = OIDCAuth(forged_hs_jwt, public_key=b"secret-key")
    assert auth_forged_hs.get_user_roles() == []
    assert auth_forged_hs.get_user_id() is None


def test_oidc_enforces_expiration_and_audience():
    import base64
    import hashlib
    import hmac
    import json
    import time

    secret = b"test-shared-secret-1234567890"
    h_b64 = (
        base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        .decode()
        .rstrip("=")
    )

    def _sign_hs256(payload_dict: dict) -> str:
        p_b64 = base64.urlsafe_b64encode(json.dumps(payload_dict).encode()).decode().rstrip("=")
        sig = hmac.new(secret, f"{h_b64}.{p_b64}".encode(), hashlib.sha256).digest()
        s_b64 = base64.urlsafe_b64encode(sig).decode().rstrip("=")
        return f"{h_b64}.{p_b64}.{s_b64}"

    now = time.time()

    # Expired token (exp in past)
    expired_jwt = _sign_hs256({"sub": "expired-user", "roles": ["admin"], "exp": now - 60})
    auth_expired = OIDCAuth(expired_jwt, public_key=secret)
    assert auth_expired.get_user_roles() == []
    assert auth_expired.get_user_id() is None

    # Not-before in future (nbf > now)
    future_nbf_jwt = _sign_hs256({"sub": "future-user", "roles": ["admin"], "nbf": now + 3600})
    auth_future = OIDCAuth(future_nbf_jwt, public_key=secret)
    assert auth_future.get_user_roles() == []
    assert auth_future.get_user_id() is None

    # Audience mismatch
    aud_jwt = _sign_hs256(
        {"sub": "aud-user", "roles": ["admin"], "aud": "expected-audience", "exp": now + 300}
    )
    auth_aud_mismatch = OIDCAuth(aud_jwt, public_key=secret, audience="different-audience")
    assert auth_aud_mismatch.get_user_roles() == []
    assert auth_aud_mismatch.get_user_id() is None

    # Audience match
    auth_aud_match = OIDCAuth(aud_jwt, public_key=secret, audience="expected-audience")
    assert auth_aud_match.get_user_roles() == ["admin"]
    assert auth_aud_match.get_user_id() == "aud-user"


def test_oidc_valid_signed_tokens():
    import base64
    import hashlib
    import hmac
    import json
    import time

    import respx
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    now = time.time()

    # 1. Valid HS256 signed token
    secret = "hmac-secret-key-abcdef"  # pragma: allowlist secret
    h_hs = (
        base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        .decode()
        .rstrip("=")
    )
    p_hs = (
        base64.urlsafe_b64encode(
            json.dumps({"sub": "admin-1", "roles": ["admin"], "exp": now + 600}).encode()
        )
        .decode()
        .rstrip("=")
    )
    sig_hs = hmac.new(secret.encode("utf-8"), f"{h_hs}.{p_hs}".encode(), hashlib.sha256).digest()
    s_hs = base64.urlsafe_b64encode(sig_hs).decode().rstrip("=")
    jwt_hs = f"{h_hs}.{p_hs}.{s_hs}"

    auth_hs = OIDCAuth(jwt_hs, public_key=secret)
    assert auth_hs.get_user_roles() == ["admin"]
    assert auth_hs.get_user_id() == "admin-1"

    # 2. Valid RS256 signed token with PEM public key
    priv_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub_key = priv_key.public_key()
    pub_pem = pub_key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("utf-8")

    h_rs = (
        base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        .decode()
        .rstrip("=")
    )
    p_rs = (
        base64.urlsafe_b64encode(
            json.dumps({"sub": "manager-1", "roles": ["manager"], "exp": now + 600}).encode()
        )
        .decode()
        .rstrip("=")
    )
    sig_rs = priv_key.sign(f"{h_rs}.{p_rs}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    s_rs = base64.urlsafe_b64encode(sig_rs).decode().rstrip("=")
    jwt_rs = f"{h_rs}.{p_rs}.{s_rs}"

    auth_rs = OIDCAuth(jwt_rs, public_key=pub_pem)
    assert auth_rs.get_user_roles() == ["manager"]
    assert auth_rs.get_user_id() == "manager-1"

    # 3. Valid RS256 token verified via JWKS (in-memory and remote URL)
    pub_numbers = pub_key.public_numbers()

    def _to_b64u(val: int) -> str:
        b = val.to_bytes((val.bit_length() + 7) // 8, byteorder="big")
        return base64.urlsafe_b64encode(b).decode().rstrip("=")

    jwks_data = {
        "keys": [
            {
                "kty": "RSA",
                "kid": "key-test-1",
                "use": "sig",
                "alg": "RS256",
                "n": _to_b64u(pub_numbers.n),
                "e": _to_b64u(pub_numbers.e),
            }
        ]
    }
    h_jwk = (
        base64.urlsafe_b64encode(
            json.dumps({"alg": "RS256", "typ": "JWT", "kid": "key-test-1"}).encode()
        )
        .decode()
        .rstrip("=")
    )
    sig_jwk = priv_key.sign(f"{h_jwk}.{p_rs}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    s_jwk = base64.urlsafe_b64encode(sig_jwk).decode().rstrip("=")
    jwt_jwk = f"{h_jwk}.{p_rs}.{s_jwk}"

    # In-memory JWKS
    auth_jwks_mem = OIDCAuth(jwt_jwk, jwks=jwks_data)
    assert auth_jwks_mem.get_user_roles() == ["manager"]
    assert auth_jwks_mem.get_user_id() == "manager-1"

    # Remote JWKS URL
    with respx.mock:
        respx.get("https://auth.imednet.example/jwks.json").respond(200, json=jwks_data)
        auth_jwks_remote = OIDCAuth(jwt_jwk, jwks_url="https://auth.imednet.example/jwks.json")
        assert auth_jwks_remote.get_user_roles() == ["manager"]
        assert auth_jwks_remote.get_user_id() == "manager-1"


def test_oidc_es256_and_issuer_and_env_configuration(monkeypatch):
    import base64
    import json
    import time

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, utils

    priv_ec = ec.generate_private_key(ec.SECP256R1())
    pub_ec = priv_ec.public_key()

    now = time.time()
    h = (
        base64.urlsafe_b64encode(json.dumps({"alg": "ES256", "typ": "JWT"}).encode())
        .decode()
        .rstrip("=")
    )
    p = (
        base64.urlsafe_b64encode(
            json.dumps(
                {
                    "preferred_username": "reviewer-user",
                    "roles": ["reviewer", "viewer"],
                    "iss": "https://auth.example.com",
                    "exp": now + 300,
                }
            ).encode()
        )
        .decode()
        .rstrip("=")
    )

    der_sig = priv_ec.sign(f"{h}.{p}".encode("ascii"), ec.ECDSA(hashes.SHA256()))
    r, s = utils.decode_dss_signature(der_sig)
    raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    s_b64 = base64.urlsafe_b64encode(raw_sig).decode().rstrip("=")
    jwt_tok = f"{h}.{p}.{s_b64}"

    # Match issuer and verify EC
    auth = OIDCAuth(jwt_tok, public_key=pub_ec, issuer="https://auth.example.com")
    assert auth.get_user_roles() == ["reviewer", "viewer"]
    assert auth.get_user_id() == "reviewer-user"

    # Mismatch issuer
    auth_bad_iss = OIDCAuth(jwt_tok, public_key=pub_ec, issuer="https://wrong-issuer.com")
    assert auth_bad_iss.get_user_roles() == []
    assert auth_bad_iss.get_user_id() is None

    # Algorithm not allowed
    auth_bad_algo = OIDCAuth(jwt_tok, public_key=pub_ec, algorithms=["RS256"])
    assert auth_bad_algo.get_user_roles() == []

    # Environment variable configuration fallback
    monkeypatch.setenv("IMEDNET_OIDC_ISSUER", "https://auth.example.com")
    auth_env = OIDCAuth(jwt_tok, public_key=pub_ec)
    assert auth_env.get_user_roles() == ["reviewer", "viewer"]


def test_oidc_edge_cases_malformed_tokens():
    # 1. Non-JSON header
    bad_h = "bm90LWpzb24"  # "not-json"
    bad_jwt1 = f"{bad_h}.eyJzdWIiOiIxIn0.c2ln"
    auth1 = OIDCAuth(bad_jwt1, public_key=b"secret")
    assert auth1.get_user_roles() == []

    # 2. Non-JSON payload
    h = "eyJhbGciOiJIUzI1NiJ9"
    bad_jwt2 = f"{h}.bm90LWpzb24.c2ln"
    auth2 = OIDCAuth(bad_jwt2, public_key=b"secret")
    assert auth2.get_user_roles() == []

    # 3. Invalid base64 in signature
    bad_jwt3 = f"{h}.eyJzdWIiOiIxIn0.???invalid_b64???"
    auth3 = OIDCAuth(bad_jwt3, public_key=b"secret")
    assert auth3.get_user_roles() == []


async def test_single_load_config_and_pre_resolved_config_and_oidc_precedence(monkeypatch):
    """Constructors call load_config once, accept Config/oidc_token, and honor ADR 0005 precedence."""
    import os
    from unittest.mock import patch

    import imednet.config as config_mod
    from imednet.config import Config
    from imednet.core.factory import ClientFactory

    monkeypatch.delenv("IMEDNET_STRICT_MODE", raising=False)
    calls: list[dict[str, object]] = []
    real_load = config_mod.load_config

    def counting_load(*args, **kwargs):
        calls.append(dict(kwargs))
        return real_load(*args, **kwargs)

    with (
        patch("imednet.config.load_config", side_effect=counting_load),
        patch("imednet.sdk.load_config", side_effect=counting_load),
        patch("imednet.core.base_client.load_config", side_effect=counting_load),
    ):
        # 1. Sync SDK with explicit oidc_token + strict_mode + timeout -> 1 load_config call
        with ImednetSDK(
            oidc_token="explicit-sync-oidc",
            base_url="https://sync.imednet.example",
            timeout=44.0,
            strict_mode=True,
        ) as sdk:
            assert len(calls) == 1
            assert isinstance(sdk.auth, OIDCAuth)
            assert sdk.config.oidc_token == "explicit-sync-oidc"
            assert sdk.config.timeout == 44.0
            assert sdk.config.strict_mode is True
            assert sdk._client.timeout == httpx.Timeout(44.0)
            assert "IMEDNET_STRICT_MODE" not in os.environ

        # 2. Async SDK with explicit oidc_token -> 1 load_config call
        calls.clear()
        async with AsyncImednetSDK(
            oidc_token="explicit-async-oidc",
            timeout=48.0,
            strict_mode=True,
        ) as async_sdk:
            assert len(calls) == 1
            assert isinstance(async_sdk.auth, OIDCAuth)
            assert async_sdk.config.oidc_token == "explicit-async-oidc"
            assert async_sdk.config.timeout == 48.0
            assert async_sdk.config.strict_mode is True
            assert "IMEDNET_STRICT_MODE" not in os.environ

        # 3. Direct Client and AsyncClient with oidc_token -> 1 load_config call each
        calls.clear()
        with Client(oidc_token="direct-client-oidc", timeout=35.0) as direct_client:
            assert len(calls) == 1
            assert isinstance(direct_client.auth, OIDCAuth)
            assert direct_client.timeout == httpx.Timeout(35.0)

        calls.clear()
        async with AsyncClient(oidc_token="direct-async-oidc", timeout=36.0) as direct_async:
            assert len(calls) == 1
            assert isinstance(direct_async.auth, OIDCAuth)
            assert direct_async.timeout == httpx.Timeout(36.0)

        # 4. Pre-resolved Config passed into SDK, AsyncSDK, Client, ClientFactory -> 0 extra calls
        calls.clear()
        resolved = Config(
            api_key="fallback-key",  # pragma: allowlist secret
            security_key="fallback-sec",  # pragma: allowlist secret
            base_url="https://pre-resolved.imednet.example",
            oidc_token="preferred-oidc-token",
            timeout=61.0,
            strict_mode=True,
            simulation_mode=False,
        )
        with ImednetSDK(config=resolved) as pre_sdk:
            assert len(calls) == 0
            assert pre_sdk.config is resolved
            assert isinstance(pre_sdk.auth, OIDCAuth)
            assert pre_sdk.auth.get_headers() == {"Authorization": "Bearer preferred-oidc-token"}
            assert pre_sdk._client.timeout == httpx.Timeout(61.0)

        async with AsyncImednetSDK(config=resolved) as pre_async_sdk:
            assert len(calls) == 0
            assert pre_async_sdk.config is resolved
            assert isinstance(pre_async_sdk.auth, OIDCAuth)
            assert pre_async_sdk._async_client.timeout == httpx.Timeout(61.0)

        with ClientFactory.create_client(resolved) as factory_client:
            assert len(calls) == 0
            assert factory_client.config is resolved
            assert factory_client.timeout == httpx.Timeout(61.0)

        async with ClientFactory.create_async_client(resolved) as factory_async:
            assert len(calls) == 0
            assert factory_async.config is resolved
            assert factory_async.timeout == httpx.Timeout(61.0)

        # 5. ADR 0005 precedence: explicit oidc_token > credential_source > env
        monkeypatch.setenv("IMEDNET_OIDC_TOKEN", "env-oidc")
        store = MemoryCredentialStore(Credentials(oidc_token="source-oidc"))
        calls.clear()
        with ImednetSDK(oidc_token="explicit-wins", credential_source=store) as prec_sdk:
            assert len(calls) == 1
            assert prec_sdk.auth.get_headers() == {"Authorization": "Bearer explicit-wins"}


async def test_strict_mode_isolated_per_sdk_without_env_mutation(monkeypatch):
    """strict_mode=True enforces drift validation per SDK without mutating os.environ."""
    import asyncio
    import os

    monkeypatch.delenv("IMEDNET_STRICT_MODE", raising=False)

    drifted_payload = {
        "data": [
            {
                "studyKey": "S-STRICT",
                "studyName": "Strict Study",
                "unexpectedAdditiveField": "surprise",
            }
        ],
        "pagination": {"currentPage": 0, "size": 100, "totalPages": 1, "totalElements": 1},
    }

    def sync_respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=drifted_payload)

    class AsyncMockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0)
            return httpx.Response(200, json=drifted_payload)

    strict_sdk = ImednetSDK(
        api_key="k",
        security_key="s",
        strict_mode=True,
        transport=httpx.MockTransport(sync_respond),
    )
    lenient_sdk = ImednetSDK(
        api_key="k",
        security_key="s",
        strict_mode=False,
        transport=httpx.MockTransport(sync_respond),
    )
    try:
        assert "IMEDNET_STRICT_MODE" not in os.environ

        with pytest.raises(ValueError, match="unexpectedAdditiveField"):
            list(strict_sdk.studies.list())

        # Lenient SDK remains unaffected even inside strict_sdk's context block
        with strict_sdk:
            studies = list(lenient_sdk.studies.list())
            assert len(studies) == 1
            assert studies[0].study_key == "S-STRICT"

        assert "IMEDNET_STRICT_MODE" not in os.environ
    finally:
        strict_sdk.close()
        lenient_sdk.close()

    async with (
        AsyncImednetSDK(
            api_key="k",
            security_key="s",
            strict_mode=True,
            transport=AsyncMockTransport(),
        ) as async_strict,
        AsyncImednetSDK(
            api_key="k",
            security_key="s",
            strict_mode=False,
            transport=AsyncMockTransport(),
        ) as async_lenient,
    ):

        async def _consume_strict() -> None:
            with pytest.raises(ValueError, match="unexpectedAdditiveField"):
                _ = [item async for item in async_strict.studies.list()]

        async def _consume_lenient() -> list:
            return [item async for item in async_lenient.studies.list()]

        _, lenient_items = await asyncio.gather(_consume_strict(), _consume_lenient())
        assert len(lenient_items) == 1
        assert lenient_items[0].study_key == "S-STRICT"
        assert "IMEDNET_STRICT_MODE" not in os.environ
