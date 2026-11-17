"""OIDC authentication strategy."""

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, padding
    from cryptography.hazmat.primitives.asymmetric.ec import (
        SECP256R1,
        SECP384R1,
        SECP521R1,
        EllipticCurvePublicKey,
        EllipticCurvePublicNumbers,
    )
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey, RSAPublicNumbers
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

    _CRYPTOGRAPHY_AVAILABLE = True
except ImportError:  # pragma: no cover
    _CRYPTOGRAPHY_AVAILABLE = False

logger = logging.getLogger(__name__)


def _base64url_decode(data: str) -> bytes:
    """Decode a base64url-encoded string with padding correction.

    Args:
        data: Base64url-encoded string.

    Returns:
        Decoded bytes.
    """
    padded = data + "=" * ((4 - len(data) % 4) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _b64url_to_int(data: str) -> int:
    """Convert a base64url-encoded unsigned big-endian integer string to int.

    Args:
        data: Base64url-encoded integer.

    Returns:
        The integer value.
    """
    return int.from_bytes(_base64url_decode(data), byteorder="big")


def _load_jwk_key(jwk: dict[str, Any]) -> Any:
    """Convert a JWK dictionary into a public key object or HMAC secret bytes.

    Args:
        jwk: The JSON Web Key dictionary.

    Returns:
        A loaded cryptographic public key or secret bytes.

    Raises:
        ValueError: If key cannot be loaded or algorithm is unsupported.
    """
    kty = jwk.get("kty")
    if kty == "RSA":
        if not _CRYPTOGRAPHY_AVAILABLE:
            raise ValueError("Cryptography library is required to load RSA JWK.")
        e = _b64url_to_int(str(jwk["e"]))
        n = _b64url_to_int(str(jwk["n"]))
        return RSAPublicNumbers(e=e, n=n).public_key()
    if kty == "EC":
        if not _CRYPTOGRAPHY_AVAILABLE:
            raise ValueError("Cryptography library is required to load EC JWK.")
        crv_map = {
            "P-256": SECP256R1(),
            "P-384": SECP384R1(),
            "P-521": SECP521R1(),
        }
        crv = str(jwk.get("crv"))
        if crv not in crv_map:
            raise ValueError(f"Unsupported EC curve: {crv}")
        x = _b64url_to_int(str(jwk["x"]))
        y = _b64url_to_int(str(jwk["y"]))
        return EllipticCurvePublicNumbers(x=x, y=y, curve=crv_map[crv]).public_key()
    if kty == "OKP" and jwk.get("crv") == "Ed25519":
        if not _CRYPTOGRAPHY_AVAILABLE:
            raise ValueError("Cryptography library is required to load Ed25519 JWK.")
        raw = _base64url_decode(str(jwk["x"]))
        return Ed25519PublicKey.from_public_bytes(raw)
    if kty == "oct":
        return _base64url_decode(str(jwk["k"]))
    raise ValueError(f"Unsupported JWK key type: {kty}")


def _load_public_key_or_secret(key_data: Any, alg: str) -> Any:
    """Load a verification key from string, bytes, dict, or key object.

    Args:
        key_data: Public key PEM, raw bytes, secret string, JWK dict, or key object.
        alg: JWS algorithm string.

    Returns:
        The resolved key object or HMAC secret bytes.
    """
    if alg.startswith("HS"):
        if isinstance(key_data, str):
            return key_data.encode("utf-8")
        if isinstance(key_data, bytes):
            return key_data
        if isinstance(key_data, dict):
            return _load_jwk_key(key_data)
        raise ValueError("HMAC secret must be str, bytes, or oct JWK dict.")

    if not _CRYPTOGRAPHY_AVAILABLE:
        raise ValueError(f"Cryptography library is required for algorithm {alg}")

    if isinstance(key_data, (RSAPublicKey, EllipticCurvePublicKey, Ed25519PublicKey)):
        return key_data

    if isinstance(key_data, dict):
        return _load_jwk_key(key_data)

    if isinstance(key_data, (str, bytes)):
        pem_str = key_data.decode("utf-8") if isinstance(key_data, bytes) else key_data
        pem_stripped = pem_str.strip()
        if pem_stripped.startswith("{") and pem_stripped.endswith("}"):
            return _load_jwk_key(json.loads(pem_stripped))
        pem_bytes = pem_str.encode("utf-8")
        if b"BEGIN CERTIFICATE" in pem_bytes:
            from cryptography import x509

            cert = x509.load_pem_x509_certificate(pem_bytes)
            return cert.public_key()
        return serialization.load_pem_public_key(pem_bytes)

    raise ValueError(f"Unsupported key format: {type(key_data)}")


def _fetch_jwks_from_url(jwks_url: str) -> dict[str, Any]:
    """Fetch and parse JWKS JSON from a remote URL.

    Args:
        jwks_url: The URL to fetch JWKS from.

    Returns:
        JWKS dictionary.
    """
    import httpx

    resp = httpx.get(jwks_url, timeout=10.0)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        return data
    raise ValueError("JWKS response must be a JSON object")


def _verify_token_signature(
    signing_input: bytes,
    signature: bytes,
    alg: str,
    key: Any,
) -> bool:
    """Verify cryptographic signature against key.

    Args:
        signing_input: The ASCII bytes of 'header.payload'.
        signature: Decoded signature bytes.
        alg: JWS algorithm.
        key: The cryptographic key or secret.

    Returns:
        True if signature is valid, False otherwise.
    """
    if alg.startswith("HS"):
        hash_func = {
            "HS256": hashlib.sha256,
            "HS384": hashlib.sha384,
            "HS512": hashlib.sha512,
        }.get(alg)
        if not hash_func or not isinstance(key, bytes):
            return False
        expected = hmac.new(key, signing_input, hash_func).digest()
        return hmac.compare_digest(signature, expected)

    if not _CRYPTOGRAPHY_AVAILABLE:
        return False

    if alg.startswith("RS"):
        hash_algo = {
            "RS256": hashes.SHA256(),
            "RS384": hashes.SHA384(),
            "RS512": hashes.SHA512(),
        }.get(alg)
        if not hash_algo or not isinstance(key, RSAPublicKey):
            return False
        try:
            key.verify(signature, signing_input, padding.PKCS1v15(), hash_algo)
            return True
        except (InvalidSignature, Exception):
            return False

    if alg.startswith("ES"):
        hash_algo = {
            "ES256": hashes.SHA256(),
            "ES384": hashes.SHA384(),
            "ES512": hashes.SHA512(),
        }.get(alg)
        key_size = {
            "ES256": 32,
            "ES384": 48,
            "ES512": 66,
        }.get(alg)
        if not hash_algo or not key_size or not isinstance(key, EllipticCurvePublicKey):
            return False
        try:
            if len(signature) == 2 * key_size:
                r = int.from_bytes(signature[:key_size], "big")
                s = int.from_bytes(signature[key_size:], "big")
                der_sig = encode_dss_signature(r, s)
                key.verify(der_sig, signing_input, ec.ECDSA(hash_algo))
            else:
                key.verify(signature, signing_input, ec.ECDSA(hash_algo))
            return True
        except (InvalidSignature, Exception):
            return False

    if alg == "EdDSA":
        if not isinstance(key, Ed25519PublicKey):
            return False
        try:
            key.verify(signature, signing_input)
            return True
        except (InvalidSignature, Exception):
            return False

    return False


class OIDCAuth:
    """Authentication strategy using OIDC token."""

    def __init__(
        self,
        token: str,
        *,
        public_key: str | bytes | Any | None = None,
        jwks_url: str | None = None,
        jwks: dict[str, Any] | None = None,
        audience: str | list[str] | None = None,
        algorithms: list[str] | None = None,
        issuer: str | None = None,
        leeway: float = 0.0,
    ) -> None:
        """Initialize the OIDC authentication strategy.

        Args:
            token: The OIDC bearer token to use for authentication.
            public_key: Optional cryptographic public key or secret for verification.
            jwks_url: Optional JWKS endpoint URL to fetch verification keys.
            jwks: Optional in-memory JWKS dictionary.
            audience: Optional expected audience or list of audiences.
            algorithms: Optional list of allowed algorithms.
            issuer: Optional expected issuer.
            leeway: Clock skew leeway in seconds for time-based claims.
        """
        self.token = token
        self.public_key = public_key or os.getenv("IMEDNET_OIDC_PUBLIC_KEY")
        self.jwks_url = jwks_url or os.getenv("IMEDNET_OIDC_JWKS_URL")
        self.jwks = jwks
        self.audience = audience or os.getenv("IMEDNET_OIDC_AUDIENCE")
        env_algos = os.getenv("IMEDNET_OIDC_ALGORITHMS")
        self.algorithms = (
            algorithms
            if algorithms is not None
            else ([a.strip() for a in env_algos.split(",")] if env_algos else None)
        )
        self.issuer = issuer or os.getenv("IMEDNET_OIDC_ISSUER")
        self.leeway = leeway
        self._decoded_claims = self._decode_token_claims(token)

    def _decode_token_claims(self, token: str) -> dict[str, Any]:
        """Extract, cryptographically verify, and validate claims from the JWT token.

        Args:
            token: The JWT token string.

        Returns:
            A dictionary containing the verified token claims, or an empty dictionary
            if token verification or claims validation fails.
        """
        parts = token.split(".")
        if len(parts) != 3:
            logger.warning(
                "OIDC token validation failed: Malformed JWT structure (expected 3 parts)."
            )
            return {}

        header_b64, payload_b64, sig_b64 = parts
        if not header_b64 or not payload_b64 or not sig_b64:
            logger.warning(
                "OIDC token validation failed: Missing header, payload, or signature segment."
            )
            return {}

        try:
            header_bytes = _base64url_decode(header_b64)
            header = json.loads(header_bytes.decode("utf-8"))
            if not isinstance(header, dict):
                logger.warning("OIDC token validation failed: Header is not a JSON object.")
                return {}
        except Exception as exc:
            logger.warning("OIDC token validation failed: Failed to decode header: %s", exc)
            return {}

        alg = header.get("alg")
        if not alg or not isinstance(alg, str):
            logger.warning("OIDC token validation failed: Missing or invalid 'alg' in header.")
            return {}

        if alg.lower() == "none":
            logger.warning("OIDC token validation failed: Insecure 'none' algorithm is rejected.")
            return {}

        allowed_algorithms = self.algorithms or [
            "RS256",
            "RS384",
            "RS512",
            "ES256",
            "ES384",
            "ES512",
            "HS256",
            "HS384",
            "HS512",
            "EdDSA",
        ]
        if alg not in allowed_algorithms:
            logger.warning("OIDC token validation failed: Algorithm '%s' is not allowed.", alg)
            return {}

        # Resolve verification key(s)
        keys_to_try: list[Any] = []
        if self.public_key:
            try:
                keys_to_try.append(_load_public_key_or_secret(self.public_key, alg))
            except Exception as exc:
                logger.warning(
                    "OIDC token validation failed: Could not parse configured public key: %s",
                    exc,
                )

        jwks_data = self.jwks
        if not jwks_data and self.jwks_url:
            try:
                jwks_data = _fetch_jwks_from_url(self.jwks_url)
            except Exception as exc:
                logger.warning("OIDC token validation failed: Could not fetch JWKS: %s", exc)

        if jwks_data and isinstance(jwks_data, dict):
            raw_keys = jwks_data.get("keys", [])
            kid = header.get("kid")
            if kid:
                matching = [k for k in raw_keys if isinstance(k, dict) and k.get("kid") == kid]
            else:
                matching = [k for k in raw_keys if isinstance(k, dict)]

            for k in matching:
                try:
                    keys_to_try.append(_load_jwk_key(k))
                except Exception as exc:
                    logger.warning("OIDC token validation failed: Could not load JWK: %s", exc)

        if not keys_to_try:
            logger.warning(
                "OIDC token validation failed: No valid public key or JWKS available to verify signature."
            )
            return {}

        try:
            signature = _base64url_decode(sig_b64)
        except Exception as exc:
            logger.warning("OIDC token validation failed: Invalid base64 in signature: %s", exc)
            return {}

        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        verified = any(
            _verify_token_signature(signing_input, signature, alg, k) for k in keys_to_try
        )
        if not verified:
            logger.warning(
                "OIDC token validation failed: Cryptographic signature verification failed."
            )
            return {}

        try:
            payload_bytes = _base64url_decode(payload_b64)
            claims = json.loads(payload_bytes.decode("utf-8"))
            if not isinstance(claims, dict):
                logger.warning("OIDC token validation failed: Payload is not a JSON object.")
                return {}
        except Exception as exc:
            logger.warning("OIDC token validation failed: Failed to decode payload: %s", exc)
            return {}

        current_time = time.time()
        if "exp" in claims:
            exp_val = claims["exp"]
            if not isinstance(exp_val, (int, float)):
                logger.warning("OIDC token validation failed: Invalid 'exp' claim format.")
                return {}
            if current_time > (exp_val + self.leeway):
                logger.warning("OIDC token validation failed: Token has expired.")
                return {}

        if "nbf" in claims:
            nbf_val = claims["nbf"]
            if not isinstance(nbf_val, (int, float)):
                logger.warning("OIDC token validation failed: Invalid 'nbf' claim format.")
                return {}
            if current_time < (nbf_val - self.leeway):
                logger.warning("OIDC token validation failed: Token is not yet valid (nbf).")
                return {}

        if self.audience:
            token_aud = claims.get("aud")
            if not token_aud:
                logger.warning("OIDC token validation failed: Missing required 'aud' claim.")
                return {}
            expected_auds = (
                [self.audience] if isinstance(self.audience, str) else list(self.audience)
            )
            actual_auds = [token_aud] if isinstance(token_aud, str) else list(token_aud)
            if not any(a in expected_auds for a in actual_auds):
                logger.warning("OIDC token validation failed: Audience mismatch.")
                return {}

        if self.issuer:
            token_iss = claims.get("iss")
            if token_iss != self.issuer:
                logger.warning("OIDC token validation failed: Issuer mismatch.")
                return {}

        return claims

    def get_headers(self) -> dict[str, str]:
        """Return the Authorization header."""
        return {"Authorization": f"Bearer {self.token}"}

    def get_user_id(self) -> str | None:
        """Extract the user identifier from the token claims.

        Returns:
            The user ID (sub or preferred_username) if found, otherwise None.
        """
        val = self._decoded_claims.get("sub") or self._decoded_claims.get("preferred_username")
        return str(val) if val else None

    def get_user_roles(self) -> list[str]:
        """Extract and map user roles from the token claims.

        Returns:
            A list of mapped system roles.
        """
        # Assume groups or roles claim maps to our roles
        # In a real environment, role mapping config would be applied here
        roles = self._decoded_claims.get("roles") or self._decoded_claims.get("groups") or []
        if isinstance(roles, str):
            roles = [roles]

        # Enterprise role mapping to system roles
        mapped_roles = []
        for r in roles:
            r_lower = r.lower()
            if "admin" in r_lower:
                mapped_roles.append("admin")
            elif "manager" in r_lower:
                mapped_roles.append("manager")
            elif "reviewer" in r_lower:
                mapped_roles.append("reviewer")
            else:
                mapped_roles.append("viewer")

        # If no explicit match, default mapping logic if any...
        return mapped_roles or list(roles)

    def __repr__(self) -> str:
        """Return a redacted string representation of the OIDC authentication."""
        return "OIDCAuth(token='********')"

    __str__ = __repr__
