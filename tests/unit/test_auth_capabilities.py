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
        "IMEDNET_OIDC_PUBLIC_KEY",
        "IMEDNET_OIDC_JWKS_URL",
        "IMEDNET_OIDC_AUDIENCE",
        "IMEDNET_OIDC_ALGORITHMS",
        "IMEDNET_OIDC_ISSUER",
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


def test_oidc_rejects_algorithm_confusion_and_blocks_commit_config(monkeypatch, tmp_path):
    import base64
    import datetime as dt
    import hashlib
    import hmac
    import json
    import time

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
    from cryptography.x509.oid import NameOID

    from imednet.auth.oidc import _load_public_key_or_secret
    from imednet.models.study_config import MappingRule, StudyConfiguration, WidgetConfig
    from imednet_workflows.config_version_control import ConfigVersionStore

    def b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")

    def _to_b64u(val: int) -> str:
        return b64url(val.to_bytes((val.bit_length() + 7) // 8, byteorder="big"))

    now = time.time()
    hs_header = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode("utf-8"))
    hs_payload = b64url(
        json.dumps({"sub": "attacker-canary", "roles": ["admin"], "exp": now + 600}).encode("utf-8")
    )

    # Generate RSA, EC, Ed25519 keys, X.509 certificate, OpenSSH keys, and JWK strings
    rsa_priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rsa_pub = rsa_priv.public_key()
    public_pem = rsa_pub.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("utf-8")
    rsa_ssh = rsa_pub.public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
    ).decode("utf-8")

    ec_priv = ec.generate_private_key(ec.SECP256R1())
    ec_pub = ec_priv.public_key()
    ec_pem = ec_pub.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("utf-8")

    ed_priv = ed25519.Ed25519PrivateKey.generate()
    ed_pub = ed_priv.public_key()
    ed_pem = ed_pub.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("utf-8")
    ed_ssh = ed_pub.public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
    ).decode("utf-8")

    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "imednet-oidc.test")])
    now_dt = dt.datetime.now(dt.UTC)
    cert_pem = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(rsa_pub)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now_dt - dt.timedelta(minutes=5))
        .not_valid_after(now_dt + dt.timedelta(hours=1))
        .sign(rsa_priv, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM)
        .decode("utf-8")
    )

    pub_numbers = rsa_pub.public_numbers()
    rsa_jwk_dict = {
        "kty": "RSA",
        "n": _to_b64u(pub_numbers.n),
        "e": _to_b64u(pub_numbers.e),
    }
    rsa_jwk_str = json.dumps(rsa_jwk_dict)

    ec_numbers = ec_pub.public_numbers()
    ec_jwk_str = json.dumps(
        {
            "kty": "EC",
            "crv": "P-256",
            "x": _to_b64u(ec_numbers.x),
            "y": _to_b64u(ec_numbers.y),
        }
    )
    ed_jwk_str = json.dumps(
        {
            "kty": "OKP",
            "crv": "Ed25519",
            "x": b64url(
                ed_pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            ),
        }
    )

    # 1. Direct _load_public_key_or_secret rejection across all asymmetric inputs for HS*
    asymmetric_inputs = [
        rsa_pub,
        ec_pub,
        ed_pub,
        public_pem,
        public_pem.encode("utf-8"),
        f"  \n\t{public_pem}  ",
        ec_pem,
        ed_pem,
        cert_pem,
        cert_pem.encode("utf-8"),
        f"\n  {cert_pem}",
        rsa_ssh,
        rsa_ssh.encode("utf-8"),
        f"   {ed_ssh}\n",
        rsa_jwk_dict,
        rsa_jwk_str,
        rsa_jwk_str.encode("utf-8"),
        f"  \n {rsa_jwk_str} ",
        ec_jwk_str,
        ed_jwk_str,
    ]
    for asym_key in asymmetric_inputs:
        for hs_alg in ("HS256", "HS384", "HS512"):
            with pytest.raises(ValueError):
                _load_public_key_or_secret(asym_key, hs_alg)

    # 2. End-to-end canary check through IMEDNET_OIDC_PUBLIC_KEY, ImednetSDK, and ConfigVersionStore
    store = ConfigVersionStore(db_path=tmp_path / "canary_versions.sqlite3")
    canary_cfg = StudyConfiguration(
        version="1.0.0",
        studyKey="STUDY-CANARY",
        mappings=[
            MappingRule(
                domain="AE",
                targetField="aeTerm",
                sourceFormKey="AE_FORM",
                sourceVariableName="ae_term",
            )
        ],
        widgets=[
            WidgetConfig(
                widgetId="w-canary",
                type="kpi_card",
                title="AE Count",
                domain="AE",
                layoutCols=6,
            )
        ],
    )

    for env_key_material in (
        public_pem,
        f"  \n{public_pem}",
        cert_pem,
        f"  \n{cert_pem}",
        rsa_jwk_str,
        f"  \n{rsa_jwk_str}",
        rsa_ssh,
    ):
        monkeypatch.setenv("IMEDNET_OIDC_PUBLIC_KEY", env_key_material)
        hs_sig = b64url(
            hmac.new(
                env_key_material.encode("utf-8"),
                f"{hs_header}.{hs_payload}".encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        confused_hs_jwt = f"{hs_header}.{hs_payload}.{hs_sig}"
        with ImednetSDK(oidc_token=confused_hs_jwt) as test_sdk:
            assert test_sdk.auth.get_user_roles() == []
            with pytest.raises(PermissionError):
                store.commit_config(
                    "STUDY-CANARY", canary_cfg, desc="canary-confused-commit", sdk=test_sdk
                )

        # Even if caller explicitly opts into HS256 in algorithms, asymmetric key material is rejected
        explicit_hs_auth = OIDCAuth(
            confused_hs_jwt, public_key=env_key_material, algorithms=["HS256", "RS256"]
        )
        assert explicit_hs_auth.get_user_roles() == []
        assert explicit_hs_auth.get_user_id() is None

    # 3. Valid X.509 cert, OpenSSH, and whitespace-padded PEM/JWK still verify asymmetric tokens
    rs_header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode("utf-8"))
    rs_payload = b64url(
        json.dumps({"sub": "valid-admin", "roles": ["admin"], "exp": now + 600}).encode("utf-8")
    )
    rs_sig = b64url(
        rsa_priv.sign(
            f"{rs_header}.{rs_payload}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256()
        )
    )
    valid_rs_jwt = f"{rs_header}.{rs_payload}.{rs_sig}"
    for valid_asym_source in (
        f"  \n{public_pem}",
        f"  \n{cert_pem}",
        f"  {rsa_ssh}",
        f"  \n{rsa_jwk_str}",
    ):
        monkeypatch.setenv("IMEDNET_OIDC_PUBLIC_KEY", valid_asym_source)
        with ImednetSDK(oidc_token=valid_rs_jwt) as valid_sdk:
            assert valid_sdk.auth.get_user_roles() == ["admin"]
            assert valid_sdk.auth.get_user_id() == "valid-admin"

    # Valid EdDSA token with OpenSSH and OKP JWK
    ed_header = b64url(json.dumps({"alg": "EdDSA", "typ": "JWT"}).encode("utf-8"))
    ed_sig = b64url(ed_priv.sign(f"{ed_header}.{rs_payload}".encode("ascii")))
    valid_ed_jwt = f"{ed_header}.{rs_payload}.{ed_sig}"
    assert OIDCAuth(valid_ed_jwt, public_key=ed_ssh).get_user_roles() == ["admin"]
    assert OIDCAuth(valid_ed_jwt, public_key=ed_jwk_str).get_user_roles() == ["admin"]

    # 4. Explicit oct JWK dict and oct JWK JSON string work for HS256 by default
    oct_raw = b"explicit-oct-hmac-secret-bytes"  # pragma: allowlist secret
    oct_jwk = {"kty": "oct", "k": b64url(oct_raw)}
    oct_sig = b64url(
        hmac.new(oct_raw, f"{hs_header}.{hs_payload}".encode("ascii"), hashlib.sha256).digest()
    )
    valid_oct_jwt = f"{hs_header}.{hs_payload}.{oct_sig}"
    assert OIDCAuth(valid_oct_jwt, public_key=oct_jwk).get_user_roles() == ["admin"]
    assert OIDCAuth(valid_oct_jwt, public_key=f"  {json.dumps(oct_jwk)} ").get_user_roles() == [
        "admin"
    ]
    with pytest.raises(ValueError):
        _load_public_key_or_secret(oct_jwk, "RS256")


def test_oidc_jwks_rejects_remote_oct_wrong_use_and_mismatched_alg():
    import base64
    import hashlib
    import hmac
    import json
    import time

    import respx
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    def b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")

    def _to_b64u(val: int) -> str:
        return b64url(val.to_bytes((val.bit_length() + 7) // 8, byteorder="big"))

    now = time.time()
    payload = b64url(
        json.dumps({"sub": "jwks-user", "roles": ["admin"], "exp": now + 600}).encode("utf-8")
    )

    # 1. Symmetric 'oct' JWK from jwks_url is rejected (default algorithms and explicit HS256)
    oct_secret = b"publicly-exposed-jwks-oct-key"  # pragma: allowlist secret
    hs_header = b64url(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "oct-1"}).encode("utf-8"))
    hs_sig = b64url(
        hmac.new(oct_secret, f"{hs_header}.{payload}".encode("ascii"), hashlib.sha256).digest()
    )
    hs_jwt = f"{hs_header}.{payload}.{hs_sig}"
    oct_jwks = {"keys": [{"kty": "oct", "kid": "oct-1", "use": "sig", "k": b64url(oct_secret)}]}

    # Default algorithms on in-memory jwks also disallow HS256
    assert OIDCAuth(hs_jwt, jwks=oct_jwks).get_user_roles() == []

    with respx.mock:
        respx.get("https://auth.imednet.example/oct-jwks.json").respond(200, json=oct_jwks)
        assert (
            OIDCAuth(hs_jwt, jwks_url="https://auth.imednet.example/oct-jwks.json").get_user_roles()
            == []
        )
        assert (
            OIDCAuth(
                hs_jwt,
                jwks_url="https://auth.imednet.example/oct-jwks.json",
                algorithms=["HS256"],
            ).get_user_roles()
            == []
        )

    # 2. JWK 'use' != 'sig' and mismatched 'alg' are rejected
    priv_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub_numbers = priv_key.public_key().public_numbers()
    rs_header = b64url(json.dumps({"alg": "RS256", "typ": "JWT", "kid": "rsa-1"}).encode("utf-8"))
    rs_sig = b64url(
        priv_key.sign(f"{rs_header}.{payload}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    )
    rs_jwt = f"{rs_header}.{payload}.{rs_sig}"

    enc_jwks = {
        "keys": [
            {
                "kty": "RSA",
                "kid": "rsa-1",
                "use": "enc",
                "alg": "RS256",
                "n": _to_b64u(pub_numbers.n),
                "e": _to_b64u(pub_numbers.e),
            }
        ]
    }
    assert OIDCAuth(rs_jwt, jwks=enc_jwks).get_user_roles() == []

    wrong_alg_jwks = {
        "keys": [
            {
                "kty": "RSA",
                "kid": "rsa-1",
                "use": "sig",
                "alg": "RS384",
                "n": _to_b64u(pub_numbers.n),
                "e": _to_b64u(pub_numbers.e),
            }
        ]
    }
    assert OIDCAuth(rs_jwt, jwks=wrong_alg_jwks).get_user_roles() == []

    # Omitted 'use' and omitted 'alg' remain valid (including without kid in header)
    minimal_jwks = {
        "keys": [
            {
                "kty": "RSA",
                "kid": "rsa-1",
                "n": _to_b64u(pub_numbers.n),
                "e": _to_b64u(pub_numbers.e),
            }
        ]
    }
    assert OIDCAuth(rs_jwt, jwks=minimal_jwks).get_user_roles() == ["admin"]

    no_kid_header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode("utf-8"))
    no_kid_sig = b64url(
        priv_key.sign(
            f"{no_kid_header}.{payload}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256()
        )
    )
    no_kid_jwt = f"{no_kid_header}.{payload}.{no_kid_sig}"
    assert OIDCAuth(no_kid_jwt, jwks=minimal_jwks).get_user_roles() == ["admin"]
    assert OIDCAuth(no_kid_jwt, public_key=minimal_jwks["keys"][0]).get_user_roles() == ["admin"]

    # Family mismatches inside JWKS (asymmetric JWK with HS256, oct JWK with RS256) and failed fetch
    matching_kid_rsa_jwk = {**minimal_jwks["keys"][0], "kid": "oct-1"}
    assert (
        OIDCAuth(
            hs_jwt, jwks={"keys": [matching_kid_rsa_jwk]}, algorithms=["HS256"]
        ).get_user_roles()
        == []
    )
    assert OIDCAuth(no_kid_jwt, jwks=oct_jwks).get_user_roles() == []
    with respx.mock:
        respx.get("https://auth.imednet.example/broken-jwks.json").respond(500)
        assert (
            OIDCAuth(
                rs_jwt, jwks_url="https://auth.imednet.example/broken-jwks.json"
            ).get_user_roles()
            == []
        )

    # Binary HMAC secret bytes, malformed JWK strings, and invalid key types
    from cryptography.hazmat.primitives import serialization

    from imednet.auth.oidc import (
        _can_parse_as_asymmetric_key,
        _load_jwk_key,
        _load_public_key_or_secret,
    )

    pub_pem_bytes = priv_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    assert _can_parse_as_asymmetric_key(pub_pem_bytes) is True
    assert _can_parse_as_asymmetric_key(b"plain-secret") is False

    raw_bin_secret = b"\xff\xfe\xfd-binary-hmac-secret"
    bin_sig = b64url(
        hmac.new(raw_bin_secret, f"{hs_header}.{payload}".encode("ascii"), hashlib.sha256).digest()
    )
    assert OIDCAuth(
        f"{hs_header}.{payload}.{bin_sig}", public_key=raw_bin_secret
    ).get_user_roles() == ["admin"]
    assert OIDCAuth(f"{hs_header}.{payload}.{bin_sig}", public_key="   ").get_user_roles() == []
    assert (
        OIDCAuth(f"{hs_header}.{payload}.{bin_sig}", public_key="{not-json}").get_user_roles() == []
    )
    assert OIDCAuth(f"{hs_header}.{payload}.{bin_sig}", public_key=12345).get_user_roles() == []

    for bad_jwk in (
        {"kty": "RSA"},
        {"kty": "EC", "crv": "P-999"},
        {"kty": "unknown"},
    ):
        with pytest.raises(ValueError):
            _load_jwk_key(bad_jwk)
    for bad_key_arg, target_alg in (
        ("   ", "HS256"),
        (12345, "HS256"),
        (12345, "RS256"),
        (json.dumps(oct_jwks["keys"][0]), "RS256"),
    ):
        with pytest.raises(ValueError):
            _load_public_key_or_secret(bad_key_arg, target_alg)


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
    assert auth3.get_headers() == {}


async def test_oidc_fail_closed_headers_same_origin_stripping_and_opaque_contracts(monkeypatch):
    """Rejected or blank tokens emit {} headers and strip residual same-origin Authorization."""
    import base64
    import hashlib
    import hmac
    import json

    secret = b"fail-closed-shared-secret-key"

    def b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")

    h_hs = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode("utf-8"))
    h_none = b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode("utf-8"))
    p_admin = b64url(json.dumps({"sub": "u1", "roles": ["admin"]}).encode("utf-8"))

    def sign_raw_payload(payload_bytes: bytes) -> str:
        p_b64 = b64url(payload_bytes)
        sig = hmac.new(secret, f"{h_hs}.{p_b64}".encode("ascii"), hashlib.sha256).digest()
        return f"{h_hs}.{p_b64}.{b64url(sig)}"

    # 1. Blank, whitespace, newline, and non-string tokens fail closed
    for bad_token in ("", "   ", "\t\n", "tok\r\nX-Injected: 1", None):
        bad_auth = OIDCAuth(bad_token)  # type: ignore[arg-type]
        assert bad_auth.get_headers() == {}
        assert bad_auth.claims == {}
        assert bad_auth.get_user_id() is None
        assert bad_auth.get_user_roles() == []
        assert bad_auth._decode_token_claims(bad_token) == {}  # type: ignore[arg-type]

    # 2. Unconfigured 1-part and 2-part opaque tokens keep Bearer pass-through
    for opaque_tok in ("opaque-bearer-token", "eyJhbGciOiJSUzI1NiJ9.two_segment_synthetic"):
        opaque_auth = OIDCAuth(opaque_tok)
        assert opaque_auth.get_headers() == {"Authorization": f"Bearer {opaque_tok}"}
        assert opaque_auth.claims == {}
        assert opaque_auth.get_user_id() is None
        assert opaque_auth.get_user_roles() == []

    # 3. Opaque tokens are rejected when ANY verification option is configured (kwargs or env)
    for verify_kwargs in (
        {"public_key": secret},
        {"public_key": ""},
        {"jwks": {"keys": []}},
        {"jwks_url": "https://auth.imednet.example/jwks.json"},
        {"audience": "imednet-api"},
        {"issuer": "https://auth.imednet.example"},
        {"algorithms": ["HS256"]},
        {"algorithms": []},
    ):
        rejected_opaque = OIDCAuth("opaque-bearer-token", **verify_kwargs)
        assert rejected_opaque.get_headers() == {}
        assert rejected_opaque.claims == {}

    monkeypatch.setenv("IMEDNET_OIDC_AUDIENCE", "env-audience")
    assert OIDCAuth("opaque-bearer-token").get_headers() == {}
    monkeypatch.delenv("IMEDNET_OIDC_AUDIENCE")

    # 4. Rejected 3-part JWTs (alg=none, missing segment, unconfigured 3-part, forged sig) fail closed
    for rejected_jwt, key_opt in (
        (f"{h_none}.{p_admin}.unsigned", None),
        (f"{h_hs}.{p_admin}.", secret),
        (f"{h_hs}.{p_admin}.unverified_sig", None),
        (f"{h_hs}.{p_admin}.wrong_sig", secret),
    ):
        rej = OIDCAuth(rejected_jwt, public_key=key_opt)
        assert rej.get_headers() == {}
        assert rej.claims == {}
        assert rej.get_user_roles() == []
        assert rej.get_user_id() is None

    # 5. Verified JWTs without an 'exp' claim (both populated and empty '{}' payload) succeed
    no_exp_jwt = sign_raw_payload(json.dumps({"sub": "svc-account", "roles": ["admin"]}).encode())
    no_exp_auth = OIDCAuth(no_exp_jwt, public_key=secret)
    assert no_exp_auth.get_headers() == {"Authorization": f"Bearer {no_exp_jwt}"}
    assert no_exp_auth.claims == {"sub": "svc-account", "roles": ["admin"]}
    assert no_exp_auth.get_user_id() == "svc-account"
    assert no_exp_auth.get_user_roles() == ["admin"]

    empty_claims_jwt = sign_raw_payload(b"{}")
    empty_claims_auth = OIDCAuth(empty_claims_jwt, public_key=secret)
    assert empty_claims_auth.claims == {}
    assert empty_claims_auth.get_headers() == {"Authorization": f"Bearer {empty_claims_jwt}"}

    # 6. _OriginScopedAuth strips residual same-origin Authorization headers when get_headers() == {}
    captured_sync: list[dict[str, str]] = []
    captured_async: list[dict[str, str]] = []

    def sync_handler(request: httpx.Request) -> httpx.Response:
        captured_sync.append({k.lower(): v for k, v in request.headers.items()})
        return httpx.Response(200, json={"data": [], "totalPages": 1})

    class AsyncSpyTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            captured_async.append({k.lower(): v for k, v in request.headers.items()})
            return httpx.Response(200, json={"data": [], "totalPages": 1})

    forged_auth = OIDCAuth(f"{h_hs}.{p_admin}.forged", public_key=secret)
    with Client(
        auth=forged_auth,
        base_url="https://edc.imednet.example",
        transport=httpx.MockTransport(sync_handler),
    ) as sync_client:
        sync_client.get(
            "/studies",
            headers={"Authorization": "Bearer residual-stale-jwt", "X-Trace": "keep"},
        )
    assert len(captured_sync) == 1
    assert "authorization" not in captured_sync[0]
    assert captured_sync[0]["x-trace"] == "keep"

    async with AsyncClient(
        auth=forged_auth,
        base_url="https://edc.imednet.example",
        transport=AsyncSpyTransport(),
    ) as async_client:
        await async_client.get(
            "/studies",
            headers={"authorization": "Bearer residual-async-jwt", "X-Trace": "keep-async"},
        )
    assert len(captured_async) == 1
    assert "authorization" not in captured_async[0]
    assert captured_async[0]["x-trace"] == "keep-async"


def test_oidc_live_expiry_rechecks_headers_claims_and_blocks_commit_config(monkeypatch, tmp_path):
    """Post-init expiry invalidates claims, headers, roles, and ConfigVersionStore.commit_config."""
    import base64
    import hashlib
    import hmac
    import json

    from imednet.models.study_config import MappingRule, StudyConfiguration, WidgetConfig
    from imednet_workflows.config_version_control import ConfigVersionStore

    secret = b"live-expiry-hmac-secret-999"
    h_hs = (
        base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )

    t0 = 1_800_000_000.0
    current_clock = [t0]
    monkeypatch.setattr("imednet.auth.oidc.time.time", lambda: current_clock[0])

    payload_dict = {
        "sub": "lead-dm",
        "roles": ["admin"],
        "nbf": t0 - 10.0,
        "exp": t0 + 30.0,
    }
    p_hs = (
        base64.urlsafe_b64encode(json.dumps(payload_dict).encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )
    sig = hmac.new(secret, f"{h_hs}.{p_hs}".encode("ascii"), hashlib.sha256).digest()
    s_hs = base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")
    live_jwt = f"{h_hs}.{p_hs}.{s_hs}"

    captured_headers: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.append({k.lower(): v for k, v in request.headers.items()})
        return httpx.Response(200, json={"data": [], "totalPages": 1})

    monkeypatch.setenv(
        "IMEDNET_ENCRYPTION_KEY", "test-store-encryption-secret"
    )  # pragma: allowlist secret
    auth = OIDCAuth(live_jwt, public_key=secret, leeway=5.0)
    store = ConfigVersionStore(db_path=tmp_path / "live_expiry_versions.sqlite3")

    def _make_cfg(version: str) -> StudyConfiguration:
        return StudyConfiguration(
            version=version,
            studyKey="STUDY-LIVE-EXP",
            mappings=[
                MappingRule(
                    domain="AE",
                    targetField="aeTerm",
                    sourceFormKey="AE_FORM",
                    sourceVariableName="ae_term",
                )
            ],
            widgets=[
                WidgetConfig(
                    widgetId=f"w-{version}",
                    type="kpi_card",
                    title="AE Count",
                    domain="AE",
                    layoutCols=6,
                )
            ],
        )

    # Read-only claims property returns defensive copy and forbids assignment
    snap = auth.claims
    assert snap["sub"] == "lead-dm"
    snap["roles"] = ["viewer"]
    assert auth.get_user_roles() == ["admin"]
    with pytest.raises(AttributeError):
        auth.claims = {}  # type: ignore[misc]

    with ImednetSDK(
        auth=auth,
        base_url="https://edc.imednet.example",
        transport=httpx.MockTransport(handler),
    ) as sdk:
        # 1. Active at t0 and within leeway (t0 + 34.9 < exp + leeway == t0 + 35.0)
        commit_1 = store.commit_config("STUDY-LIVE-EXP", _make_cfg("1.0.0"), desc="v1", sdk=sdk)
        assert commit_1
        current_clock[0] = t0 + 34.9
        list(sdk.studies.list())
        assert captured_headers[-1]["authorization"] == f"Bearer {live_jwt}"
        assert auth.get_user_roles() == ["admin"]
        assert auth.get_user_id() == "lead-dm"

        # 2. Exact RFC 7519 Section 4.1.4 boundary: now == exp + leeway (t0 + 35.0) -> expired
        current_clock[0] = t0 + 35.0
        assert auth.claims == {}
        assert auth.get_headers() == {}
        assert auth.get_user_id() is None
        assert auth.get_user_roles() == []

        with pytest.raises(PermissionError, match="manager or admin role"):
            store.commit_config("STUDY-LIVE-EXP", _make_cfg("2.0.0"), desc="expired-v2", sdk=sdk)

        sdk._client.get("/studies", headers={"Authorization": "Bearer stale-header"})
        assert "authorization" not in captured_headers[-1]

        # 3. Clock skewed before nbf - leeway (t0 - 16.0 < (t0 - 10.0) - 5.0) -> inactive
        current_clock[0] = t0 - 16.0
        assert auth.claims == {}
        assert auth.get_headers() == {}
        assert auth.get_user_roles() == []


def test_oidc_strict_claim_types_and_crypto_decoder_edge_cases():
    """Malformed numeric, aud, iss, sub, and role claims fail closed without unhandled exceptions."""
    import base64
    import hashlib
    import hmac
    import json
    import time

    import respx
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

    from imednet.auth.oidc import _verify_token_signature

    secret = b"strict-claim-validation-secret"

    def b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")

    h_hs = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode("utf-8"))
    now = time.time()

    def sign_claims(claims_obj: object, *, allow_nan: bool = True) -> str:
        raw_payload = json.dumps(claims_obj, allow_nan=allow_nan).encode("utf-8")
        p_b64 = b64url(raw_payload)
        sig = hmac.new(secret, f"{h_hs}.{p_b64}".encode("ascii"), hashlib.sha256).digest()
        return f"{h_hs}.{p_b64}.{b64url(sig)}"

    # 1. Boolean, non-finite, and non-numeric exp / nbf / leeway
    for bad_num in (True, False, float("nan"), float("inf"), float("-inf"), "12345", None, [], {}):
        exp_tok = sign_claims({"sub": "u", "roles": ["admin"], "exp": bad_num})
        exp_auth = OIDCAuth(exp_tok, public_key=secret)
        assert exp_auth.get_headers() == {}
        assert exp_auth.claims == {}
        assert exp_auth.get_user_roles() == []

        nbf_tok = sign_claims({"sub": "u", "roles": ["admin"], "nbf": bad_num})
        nbf_auth = OIDCAuth(nbf_tok, public_key=secret)
        assert nbf_auth.get_headers() == {}
        assert nbf_auth.claims == {}
        assert nbf_auth.get_user_roles() == []

    valid_tok = sign_claims({"sub": "u", "roles": ["admin"], "exp": now + 300})
    for bad_leeway in (True, False, -0.1, float("nan"), float("inf")):
        bad_lw_auth = OIDCAuth(valid_tok, public_key=secret, leeway=bad_leeway)  # type: ignore[arg-type]
        assert bad_lw_auth.get_headers() == {}
        assert bad_lw_auth.claims == {}

    # Exact exp boundary rejection in _decode_token_claims when now >= exp + leeway
    already_at_exp = sign_claims({"sub": "u", "roles": ["admin"], "exp": now - 0.001})
    assert OIDCAuth(already_at_exp, public_key=secret).get_headers() == {}

    # 2. RFC 7519 Section 4.1.3 'aud' validation (string or non-empty list of non-empty strings)
    for bad_aud in (
        123,
        True,
        False,
        {"expected": True},
        [],
        [""],
        ["   "],
        ["expected", 123],
        "",
        "  ",
        None,
    ):
        aud_tok = sign_claims({"sub": "u", "roles": ["admin"], "aud": bad_aud, "exp": now + 300})
        for aud_cfg in (None, "expected"):
            aud_auth = OIDCAuth(aud_tok, public_key=secret, audience=aud_cfg)
            assert aud_auth.get_headers() == {}
            assert aud_auth.claims == {}
            assert aud_auth.get_user_roles() == []

    # Missing 'aud' when audience is configured, and invalid expected audience configurations
    no_aud_tok = sign_claims({"sub": "u", "roles": ["admin"], "exp": now + 300})
    assert OIDCAuth(no_aud_tok, public_key=secret, audience="expected").get_headers() == {}
    for bad_expected_aud in ("", "   ", [], [123], {"expected": 1}):
        assert (
            OIDCAuth(
                sign_claims({"sub": "u", "aud": "expected", "exp": now + 300}),
                public_key=secret,
                audience=bad_expected_aud,  # type: ignore[arg-type]
            ).get_headers()
            == {}
        )

    # Valid string-list 'aud' with tuple/list audience config
    multi_aud_tok = sign_claims(
        {"sub": "u", "roles": ["admin"], "aud": ["other-svc", "imednet-edc"], "exp": now + 300}
    )
    multi_aud_auth = OIDCAuth(
        multi_aud_tok,
        public_key=secret,
        audience=("imednet-edc", "backup-edc"),  # type: ignore[arg-type]
    )
    assert multi_aud_auth.get_user_roles() == ["admin"]
    assert multi_aud_auth.get_headers() == {"Authorization": f"Bearer {multi_aud_tok}"}

    # 3. RFC 7519 Section 4.1.1 'iss' validation (non-empty string)
    for bad_iss in (123, True, False, ["https://idp.example"], {"iss": "x"}, "", "   ", None):
        iss_tok = sign_claims({"sub": "u", "roles": ["admin"], "iss": bad_iss, "exp": now + 300})
        for iss_cfg in (None, "https://idp.example"):
            iss_auth = OIDCAuth(iss_tok, public_key=secret, issuer=iss_cfg)
            assert iss_auth.get_headers() == {}
            assert iss_auth.claims == {}

    valid_iss_tok = sign_claims(
        {"sub": "u", "roles": ["admin"], "iss": "https://idp.example", "exp": now + 300}
    )
    for bad_expected_iss in ("", "   ", 123):
        assert (
            OIDCAuth(
                valid_iss_tok,
                public_key=secret,
                issuer=bad_expected_iss,  # type: ignore[arg-type]
            ).get_headers()
            == {}
        )

    # 4. get_user_id and get_user_roles reject dicts, booleans, and non-string items
    for bad_id in (True, False, 123, ["user"], {"user": "admin"}, "   "):
        sub_tok = sign_claims({"sub": bad_id, "preferred_username": "fallback", "exp": now + 300})
        assert OIDCAuth(sub_tok, public_key=secret).get_user_id() is None
        pref_tok = sign_claims({"preferred_username": bad_id, "exp": now + 300})
        assert OIDCAuth(pref_tok, public_key=secret).get_user_id() is None

    fallback_id_tok = sign_claims({"sub": "", "preferred_username": "alice", "exp": now + 300})
    assert OIDCAuth(fallback_id_tok, public_key=secret).get_user_id() == "alice"

    for bad_roles in (
        {"admin": False},
        {"admin": True},
        True,
        False,
        123,
        [123],
        [True],
        [None],
        [""],
        ["   "],
        ["admin", 123],
    ):
        role_tok = sign_claims({"sub": "u", "roles": bad_roles, "exp": now + 300})
        assert OIDCAuth(role_tok, public_key=secret).get_user_roles() == []
        grp_tok = sign_claims({"sub": "u", "groups": bad_roles, "exp": now + 300})
        assert OIDCAuth(grp_tok, public_key=secret).get_user_roles() == []

    # Valid string role, empty roles fallback to groups, and viewer mapping
    assert OIDCAuth(
        sign_claims({"sub": "u", "roles": "Study-Admin", "exp": now + 300}),
        public_key=secret,
    ).get_user_roles() == ["admin"]
    assert OIDCAuth(
        sign_claims({"sub": "u", "roles": [], "groups": ["clinical-manager"], "exp": now + 300}),
        public_key=secret,
    ).get_user_roles() == ["manager"]
    assert OIDCAuth(
        sign_claims({"sub": "u", "roles": "   ", "groups": "readonly-auditor", "exp": now + 300}),
        public_key=secret,
    ).get_user_roles() == ["viewer"]

    # 5. Header/payload non-dict JSON, invalid alg, invalid signature b64, non-dict JWKS, and crypto paths
    list_h = b64url(b"[1, 2]")
    assert OIDCAuth(f"{list_h}.eyJzdWIiOiIxIn0.c2ln", public_key=secret).get_headers() == {}

    for bad_hdr in ({"typ": "JWT"}, {"alg": 123}, {"alg": ""}):
        bh = b64url(json.dumps(bad_hdr).encode("utf-8"))
        assert OIDCAuth(f"{bh}.eyJzdWIiOiIxIn0.c2ln", public_key=secret).get_headers() == {}

    # 1-char base64 signature triggers binascii.Error after key resolution
    assert OIDCAuth(f"{h_hs}.eyJzdWIiOiIxIn0.a", public_key=secret).get_headers() == {}

    # Validly signed non-dict payload ("[1, 2]") and invalid JSON payload
    assert OIDCAuth(sign_claims([1, 2]), public_key=secret).get_headers() == {}
    non_json_p = b64url(b"not-valid-json")
    non_json_sig = b64url(
        hmac.new(secret, f"{h_hs}.{non_json_p}".encode("ascii"), hashlib.sha256).digest()
    )
    assert OIDCAuth(f"{h_hs}.{non_json_p}.{non_json_sig}", public_key=secret).get_headers() == {}

    # Remote JWKS returning a JSON array rather than object
    rsa_priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ec_priv = ec.generate_private_key(ec.SECP256R1())
    ed_priv = ed25519.Ed25519PrivateKey.generate()
    h_rs = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode("utf-8"))
    with respx.mock:
        respx.get("https://auth.imednet.example/list-jwks.json").respond(200, json=[1, 2, 3])
        assert (
            OIDCAuth(
                f"{h_rs}.eyJzdWIiOiIxIn0.c2ln",
                jwks_url="https://auth.imednet.example/list-jwks.json",
            ).get_headers()
            == {}
        )

    # Direct _verify_token_signature negative and DER-ECDSA branches
    msg = b"header.payload"
    der_ec_sig = ec_priv.sign(msg, ec.ECDSA(hashes.SHA256()))
    assert _verify_token_signature(msg, der_ec_sig, "ES256", ec_priv.public_key()) is True
    assert _verify_token_signature(msg, b"bad-der", "ES256", ec_priv.public_key()) is False
    assert _verify_token_signature(msg, b"bad-sig", "ES256", rsa_priv.public_key()) is False
    assert _verify_token_signature(msg, b"bad-sig", "RS256", rsa_priv.public_key()) is False
    assert _verify_token_signature(msg, b"bad-sig", "RS256", ec_priv.public_key()) is False
    assert _verify_token_signature(msg, b"bad-sig", "EdDSA", ed_priv.public_key()) is False
    assert _verify_token_signature(msg, b"bad-sig", "EdDSA", rsa_priv.public_key()) is False
    assert _verify_token_signature(msg, b"bad-sig", "HS256", "not-bytes") is False
    assert _verify_token_signature(msg, b"bad-sig", "PS999", rsa_priv.public_key()) is False


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
