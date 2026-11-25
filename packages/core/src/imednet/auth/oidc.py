"""OIDC authentication strategy."""

import base64
import hashlib
import hmac
import json
import logging
import math
import os
import time
from types import SimpleNamespace
from typing import Any

from imednet.auth.api_key import ApiKeyAuth
from imednet.errors.api import UnauthorizedError
from imednet.errors.validation import ConfigurationError

_CRYPTOGRAPHY_AVAILABLE: bool | None = None
_CRYPTO_MODULES: SimpleNamespace | None = None


def _get_crypto() -> SimpleNamespace | None:
    """Lazily load cryptography primitives when asymmetric JWT verification is used."""
    global _CRYPTOGRAPHY_AVAILABLE, _CRYPTO_MODULES
    if _CRYPTOGRAPHY_AVAILABLE is False:
        return None
    if _CRYPTO_MODULES is not None:
        return _CRYPTO_MODULES
    try:
        from cryptography import x509
        from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
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

        _CRYPTO_MODULES = SimpleNamespace(
            InvalidSignature=InvalidSignature,
            UnsupportedAlgorithm=UnsupportedAlgorithm,
            hashes=hashes,
            serialization=serialization,
            ec=ec,
            padding=padding,
            SECP256R1=SECP256R1,
            SECP384R1=SECP384R1,
            SECP521R1=SECP521R1,
            EllipticCurvePublicKey=EllipticCurvePublicKey,
            EllipticCurvePublicNumbers=EllipticCurvePublicNumbers,
            Ed25519PublicKey=Ed25519PublicKey,
            RSAPublicKey=RSAPublicKey,
            RSAPublicNumbers=RSAPublicNumbers,
            encode_dss_signature=encode_dss_signature,
            x509=x509,
            asymmetric_key_types=(RSAPublicKey, EllipticCurvePublicKey, Ed25519PublicKey),
        )
        _CRYPTOGRAPHY_AVAILABLE = True
        return _CRYPTO_MODULES
    except ImportError:  # pragma: no cover
        _CRYPTOGRAPHY_AVAILABLE = False
        return None


logger = logging.getLogger(__name__)

_ASYMMETRIC_ALGORITHMS: tuple[str, ...] = (
    "RS256",
    "RS384",
    "RS512",
    "ES256",
    "ES384",
    "ES512",
    "EdDSA",
)
_SYMMETRIC_ALGORITHMS: tuple[str, ...] = (
    "HS256",
    "HS384",
    "HS512",
)
_SSH_KEY_PREFIXES: tuple[bytes, ...] = (
    b"ssh-rsa ",
    b"ssh-ed25519 ",
    b"ssh-dss ",
    b"ecdsa-sha2-",
    b"sk-ssh-",
    b"sk-ecdsa-",
)


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


def _validate_jwk_constraints(jwk: dict[str, Any], alg: str | None = None) -> None:
    """Validate JWK 'use' and 'alg' metadata fields.

    Args:
        jwk: The JSON Web Key dictionary.
        alg: Optional JWS header algorithm to match when JWK specifies 'alg'.

    Raises:
        ValueError: If 'use' is set and not 'sig', or 'alg' is set and mismatches header 'alg'.
    """
    if "use" in jwk and jwk["use"] is not None and jwk["use"] != "sig":
        raise ValueError(f"JWK 'use' must be 'sig' for signature verification, got {jwk['use']!r}.")
    if alg is not None and "alg" in jwk and jwk["alg"] is not None and jwk["alg"] != alg:
        raise ValueError(f"JWK 'alg' ({jwk['alg']!r}) does not match token header 'alg' ({alg!r}).")


def _load_jwk_key(jwk: dict[str, Any]) -> Any:
    """Convert a JWK dictionary into a public key object or HMAC secret bytes.

    Args:
        jwk: The JSON Web Key dictionary.

    Returns:
        A loaded cryptographic public key or secret bytes.

    Raises:
        ValueError: If key cannot be loaded or algorithm is unsupported.
    """
    _validate_jwk_constraints(jwk)
    kty = jwk.get("kty")
    try:
        if kty == "RSA":
            crypto = _get_crypto()
            if crypto is None:
                raise ValueError("Cryptography library is required to load RSA JWK.")
            e = _b64url_to_int(str(jwk["e"]))
            n = _b64url_to_int(str(jwk["n"]))
            return crypto.RSAPublicNumbers(e=e, n=n).public_key()
        if kty == "EC":
            crypto = _get_crypto()
            if crypto is None:
                raise ValueError("Cryptography library is required to load EC JWK.")
            crv_map = {
                "P-256": crypto.SECP256R1(),
                "P-384": crypto.SECP384R1(),
                "P-521": crypto.SECP521R1(),
            }
            crv = str(jwk.get("crv"))
            if crv not in crv_map:
                raise ValueError(f"Unsupported EC curve: {crv}")
            x = _b64url_to_int(str(jwk["x"]))
            y = _b64url_to_int(str(jwk["y"]))
            return crypto.EllipticCurvePublicNumbers(x=x, y=y, curve=crv_map[crv]).public_key()
        if kty == "OKP" and jwk.get("crv") == "Ed25519":
            crypto = _get_crypto()
            if crypto is None:
                raise ValueError("Cryptography library is required to load Ed25519 JWK.")
            raw = _base64url_decode(str(jwk["x"]))
            return crypto.Ed25519PublicKey.from_public_bytes(raw)
        if kty == "oct":
            return _base64url_decode(str(jwk["k"]))
    except KeyError as exc:
        raise ValueError(f"Missing required JWK parameter: {exc.args[0]}") from exc
    raise ValueError(f"Unsupported JWK key type: {kty}")


def _can_parse_as_asymmetric_key(data: bytes) -> bool:
    """Return True if bytes can be parsed as a PEM public key, SSH key, or X.509 certificate."""
    crypto = _get_crypto()
    if crypto is None:
        return False

    for loader in (
        crypto.serialization.load_pem_public_key,
        crypto.serialization.load_ssh_public_key,
        crypto.x509.load_pem_x509_certificate,
    ):
        try:
            loader(data)
            return True
        except (ValueError, TypeError, crypto.UnsupportedAlgorithm):
            continue
    return False


def _is_symmetric_key_input(key_data: Any) -> bool:
    """Return True only when key_data represents a symmetric secret or 'oct' JWK."""
    if isinstance(key_data, dict):
        return key_data.get("kty") == "oct"
    if not isinstance(key_data, (str, bytes)):
        return False

    raw_bytes = key_data.encode("utf-8") if isinstance(key_data, str) else key_data
    stripped_bytes = raw_bytes.strip()
    if not stripped_bytes:
        return False
    if (
        b"-----BEGIN" in stripped_bytes
        or stripped_bytes.startswith(_SSH_KEY_PREFIXES)
        or _can_parse_as_asymmetric_key(stripped_bytes)
    ):
        return False

    try:
        stripped_text = stripped_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return True

    if stripped_text.startswith("{") and stripped_text.endswith("}"):
        try:
            parsed = json.loads(stripped_text)
        except ValueError:
            return False
        return isinstance(parsed, dict) and parsed.get("kty") == "oct"

    return True


def _load_public_key_or_secret(key_data: Any, alg: str) -> Any:
    """Load a verification key from string, bytes, dict, or key object.

    Args:
        key_data: Public key PEM, raw bytes, secret string, JWK dict, or key object.
        alg: JWS algorithm string.

    Returns:
        The resolved key object or HMAC secret bytes.

    Raises:
        ValueError: If key_data is malformed or incompatible with alg.
    """
    if alg.startswith("HS"):
        if isinstance(key_data, dict):
            _validate_jwk_constraints(key_data, alg)
            loaded_dict_key = _load_jwk_key(key_data)
            if key_data.get("kty") != "oct":
                raise ValueError("Only 'oct' JWK keys are permitted for HMAC verification.")
            return loaded_dict_key
        if isinstance(key_data, (str, bytes)):
            raw_bytes = key_data.encode("utf-8") if isinstance(key_data, str) else key_data
            stripped_bytes = raw_bytes.strip()
            if not stripped_bytes:
                raise ValueError("HMAC secret must not be empty.")
            if (
                b"-----BEGIN" in stripped_bytes
                or stripped_bytes.startswith(_SSH_KEY_PREFIXES)
                or _can_parse_as_asymmetric_key(stripped_bytes)
            ):
                raise ValueError("Asymmetric key material cannot be used as an HMAC secret.")
            try:
                stripped_text: str | None = stripped_bytes.decode("utf-8")
            except UnicodeDecodeError:
                stripped_text = None
            if (
                stripped_text is not None
                and stripped_text.startswith("{")
                and stripped_text.endswith("}")
            ):
                parsed_jwk: dict[str, Any] = json.loads(stripped_text)
                _validate_jwk_constraints(parsed_jwk, alg)
                loaded_str_key = _load_jwk_key(parsed_jwk)
                if parsed_jwk.get("kty") != "oct":
                    raise ValueError("Only 'oct' JWK keys are permitted for HMAC verification.")
                return loaded_str_key
            return raw_bytes
        crypto_hs = _get_crypto()
        if crypto_hs is not None and isinstance(key_data, crypto_hs.asymmetric_key_types):
            raise ValueError("Asymmetric public key object cannot be used as an HMAC secret.")
        raise ValueError("HMAC secret must be str, bytes, or oct JWK dict.")

    crypto = _get_crypto()
    if crypto is None:
        raise ValueError(f"Cryptography library is required for algorithm {alg}")

    if isinstance(key_data, crypto.asymmetric_key_types):
        return key_data

    if isinstance(key_data, dict):
        if key_data.get("kty") == "oct":
            raise ValueError("Symmetric 'oct' JWK cannot be used for asymmetric verification.")
        _validate_jwk_constraints(key_data, alg)
        return _load_jwk_key(key_data)

    if isinstance(key_data, (str, bytes)):
        pem_str = key_data.decode("utf-8") if isinstance(key_data, bytes) else key_data
        pem_stripped = pem_str.strip()
        if pem_stripped.startswith("{") and pem_stripped.endswith("}"):
            parsed_asym_jwk: dict[str, Any] = json.loads(pem_stripped)
            if parsed_asym_jwk.get("kty") == "oct":
                raise ValueError("Symmetric 'oct' JWK cannot be used for asymmetric verification.")
            _validate_jwk_constraints(parsed_asym_jwk, alg)
            return _load_jwk_key(parsed_asym_jwk)
        pem_bytes = pem_stripped.encode("utf-8")
        if b"BEGIN CERTIFICATE" in pem_bytes:
            cert = crypto.x509.load_pem_x509_certificate(pem_bytes)
            return cert.public_key()
        if pem_bytes.startswith(_SSH_KEY_PREFIXES):
            return crypto.serialization.load_ssh_public_key(pem_bytes)
        return crypto.serialization.load_pem_public_key(pem_bytes)

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

    crypto = _get_crypto()
    if crypto is None:
        return False

    if alg.startswith("RS"):
        hash_algo = {
            "RS256": crypto.hashes.SHA256(),
            "RS384": crypto.hashes.SHA384(),
            "RS512": crypto.hashes.SHA512(),
        }.get(alg)
        if not hash_algo or not isinstance(key, crypto.RSAPublicKey):
            return False
        try:
            key.verify(signature, signing_input, crypto.padding.PKCS1v15(), hash_algo)
            return True
        except (crypto.InvalidSignature, Exception):
            return False

    if alg.startswith("ES"):
        hash_algo = {
            "ES256": crypto.hashes.SHA256(),
            "ES384": crypto.hashes.SHA384(),
            "ES512": crypto.hashes.SHA512(),
        }.get(alg)
        key_size = {
            "ES256": 32,
            "ES384": 48,
            "ES512": 66,
        }.get(alg)
        if not hash_algo or not key_size or not isinstance(key, crypto.EllipticCurvePublicKey):
            return False
        try:
            if len(signature) == 2 * key_size:
                r = int.from_bytes(signature[:key_size], "big")
                s = int.from_bytes(signature[key_size:], "big")
                der_sig = crypto.encode_dss_signature(r, s)
                key.verify(der_sig, signing_input, crypto.ec.ECDSA(hash_algo))
            else:
                key.verify(signature, signing_input, crypto.ec.ECDSA(hash_algo))
            return True
        except (crypto.InvalidSignature, Exception):
            return False

    if alg == "EdDSA":
        if not isinstance(key, crypto.Ed25519PublicKey):
            return False
        try:
            key.verify(signature, signing_input)
            return True
        except (crypto.InvalidSignature, Exception):
            return False

    return False


def _is_valid_numeric_date(value: Any) -> bool:
    """Return True when value is a finite int or float and not a boolean."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _normalize_aud_claim(value: Any) -> list[str] | None:
    """Validate and normalize an 'aud' claim or audience allowlist to non-empty strings."""
    if isinstance(value, str):
        return [value] if value.strip() else None
    if (
        isinstance(value, list)
        and value
        and all(isinstance(item, str) and bool(item.strip()) for item in value)
    ):
        return list(value)
    return None


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
        env_pub_key = os.getenv("IMEDNET_OIDC_PUBLIC_KEY")
        self.public_key = public_key if public_key is not None else (env_pub_key or None)
        env_jwks_url = os.getenv("IMEDNET_OIDC_JWKS_URL")
        self.jwks_url = jwks_url if jwks_url is not None else (env_jwks_url or None)
        self.jwks = jwks
        env_audience = os.getenv("IMEDNET_OIDC_AUDIENCE")
        self.audience = audience if audience is not None else (env_audience or None)
        env_algos = os.getenv("IMEDNET_OIDC_ALGORITHMS")
        self.algorithms = (
            algorithms
            if algorithms is not None
            else (
                [a.strip() for a in env_algos.split(",") if a.strip()]
                if env_algos and env_algos.strip()
                else None
            )
        )
        env_issuer = os.getenv("IMEDNET_OIDC_ISSUER")
        self.issuer = issuer if issuer is not None else (env_issuer or None)
        self.leeway = leeway

        self._verification_configured = any(
            opt is not None
            for opt in (
                self.public_key,
                self.jwks_url,
                self.jwks,
                self.audience,
                self.algorithms,
                self.issuer,
            )
        )
        is_jwt_candidate = isinstance(token, str) and len(token.split(".")) == 3
        has_alg_none_header = _has_alg_none_prefix(token) if isinstance(token, str) else False
        self._requires_verification = (
            is_jwt_candidate or self._verification_configured or has_alg_none_header
        )
        self._token_verified = False
        self._validation_error: ConfigurationError | UnauthorizedError | None = None
        self._decoded_claims: dict[str, Any] = {}
        if not isinstance(token, str) or not token.strip():
            self._validation_error = ConfigurationError(
                "OIDC token validation failed: Token must be a non-empty string."
            )
        elif "\r" in token or "\n" in token:
            self._validation_error = ConfigurationError(
                "OIDC token validation failed: Token must not contain control characters."
            )
        elif has_alg_none_header and not is_jwt_candidate:
            logger.warning("OIDC token validation failed: Insecure 'none' algorithm is rejected.")
            self._validation_error = UnauthorizedError(
                "OIDC token validation failed: Insecure 'none' algorithm is rejected.",
                status_code=401,
            )
        elif self._requires_verification:
            self._decoded_claims = self._decode_token_claims(token)

    def _default_allowed_algorithms(self) -> list[str]:
        """Return default allowed JWS algorithms matched to the configured key type."""
        if self.jwks is not None or self.jwks_url:
            return list(_ASYMMETRIC_ALGORITHMS)
        if self.public_key is not None and _is_symmetric_key_input(self.public_key):
            return list(_SYMMETRIC_ALGORITHMS)
        return list(_ASYMMETRIC_ALGORITHMS)

    def _temporal_claim_error(
        self,
        claims: dict[str, Any],
        *,
        current_time: float | None = None,
    ) -> ConfigurationError | UnauthorizedError | None:
        """Return an error when 'exp', 'nbf', or 'leeway' fails RFC 7519 Section 4.1.4."""
        if not _is_valid_numeric_date(self.leeway) or self.leeway < 0:
            msg = "OIDC token validation failed: Invalid 'leeway' configuration."
            logger.warning(msg)
            return ConfigurationError(msg)

        now = time.time() if current_time is None else current_time
        if "exp" in claims:
            exp_val = claims["exp"]
            if not _is_valid_numeric_date(exp_val):
                msg = "OIDC token validation failed: Invalid 'exp' claim format."
                logger.warning(msg)
                return UnauthorizedError(msg, status_code=401)
            if now >= (exp_val + self.leeway):
                msg = "OIDC token validation failed: Token has expired."
                logger.warning(msg)
                return UnauthorizedError(msg, status_code=401)

        if "nbf" in claims:
            nbf_val = claims["nbf"]
            if not _is_valid_numeric_date(nbf_val):
                msg = "OIDC token validation failed: Invalid 'nbf' claim format."
                logger.warning(msg)
                return UnauthorizedError(msg, status_code=401)
            if now < (nbf_val - self.leeway):
                msg = "OIDC token validation failed: Token is not yet valid (nbf)."
                logger.warning(msg)
                return UnauthorizedError(msg, status_code=401)

        return None

    def _has_active_temporal_claims(
        self,
        claims: dict[str, Any],
        *,
        current_time: float | None = None,
    ) -> bool:
        """Validate 'exp', 'nbf', and 'leeway' against RFC 7519 Section 4.1.4."""
        return self._temporal_claim_error(claims, current_time=current_time) is None

    def _record_validation_error(
        self,
        is_primary_token: bool,
        error: ConfigurationError | UnauthorizedError,
    ) -> dict[str, Any]:
        """Store validation error for primary token and return empty claims."""
        if is_primary_token:
            self._validation_error = error
        return {}

    def _decode_token_claims(self, token: str) -> dict[str, Any]:
        """Extract, cryptographically verify, and validate claims from the JWT token.

        Args:
            token: The JWT token string.

        Returns:
            A dictionary containing the verified token claims, or an empty dictionary
            if token verification or claims validation fails.
        """
        is_primary_token = token == self.token
        if is_primary_token:
            self._token_verified = False
            self._validation_error = None

        if not isinstance(token, str) or not token.strip():
            msg = "OIDC token validation failed: Token must be a non-empty string."
            logger.warning(msg)
            return self._record_validation_error(is_primary_token, ConfigurationError(msg))

        parts = token.split(".")
        if len(parts) != 3:
            msg = "OIDC token validation failed: Malformed JWT structure (expected 3 parts)."
            logger.warning(msg)
            return self._record_validation_error(
                is_primary_token, UnauthorizedError(msg, status_code=401)
            )

        header_b64, payload_b64, sig_b64 = parts
        if not header_b64 or not payload_b64:
            msg = "OIDC token validation failed: Missing header, payload, or signature segment."
            logger.warning(msg)
            return self._record_validation_error(
                is_primary_token, UnauthorizedError(msg, status_code=401)
            )

        try:
            header_bytes = _base64url_decode(header_b64)
            header = json.loads(header_bytes.decode("utf-8"))
            if not isinstance(header, dict):
                msg = "OIDC token validation failed: Header is not a JSON object."
                logger.warning(msg)
                return self._record_validation_error(
                    is_primary_token, UnauthorizedError(msg, status_code=401)
                )
        except Exception as exc:
            logger.warning("OIDC token validation failed: Failed to decode header: %s", exc)
            return self._record_validation_error(
                is_primary_token,
                UnauthorizedError(
                    "OIDC token validation failed: Failed to decode JWT header.",
                    status_code=401,
                ),
            )

        alg = header.get("alg")
        if not alg or not isinstance(alg, str):
            msg = "OIDC token validation failed: Missing or invalid 'alg' in header."
            logger.warning(msg)
            return self._record_validation_error(
                is_primary_token, UnauthorizedError(msg, status_code=401)
            )

        if alg.lower() == "none":
            msg = "OIDC token validation failed: Insecure 'none' algorithm is rejected."
            logger.warning(msg)
            return self._record_validation_error(
                is_primary_token, UnauthorizedError(msg, status_code=401)
            )

        if not sig_b64:
            msg = "OIDC token validation failed: Missing header, payload, or signature segment."
            logger.warning(msg)
            return self._record_validation_error(
                is_primary_token, UnauthorizedError(msg, status_code=401)
            )

        allowed_algorithms = (
            self.algorithms if self.algorithms is not None else self._default_allowed_algorithms()
        )
        if alg not in allowed_algorithms:
            msg = f"OIDC token validation failed: Algorithm '{alg}' is not allowed."
            logger.warning("OIDC token validation failed: Algorithm '%s' is not allowed.", alg)
            return self._record_validation_error(
                is_primary_token, UnauthorizedError(msg, status_code=401)
            )

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
        jwks_from_url = False
        if jwks_data is None and self.jwks_url:
            try:
                jwks_data = _fetch_jwks_from_url(self.jwks_url)
                jwks_from_url = True
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
                if jwks_from_url and k.get("kty") == "oct":
                    logger.warning(
                        "OIDC token validation failed: Symmetric 'oct' JWK from jwks_url is rejected."
                    )
                    continue
                if alg.startswith("HS") and k.get("kty") != "oct":
                    logger.warning(
                        "OIDC token validation failed: Asymmetric JWK cannot verify HMAC algorithm."
                    )
                    continue
                if not alg.startswith("HS") and k.get("kty") == "oct":
                    logger.warning(
                        "OIDC token validation failed: Symmetric 'oct' JWK cannot verify asymmetric algorithm."
                    )
                    continue
                try:
                    _validate_jwk_constraints(k, alg)
                    keys_to_try.append(_load_jwk_key(k))
                except Exception as exc:
                    logger.warning("OIDC token validation failed: Could not load JWK: %s", exc)

        if not keys_to_try:
            msg = (
                "OIDC token validation failed: "
                "No valid public key or JWKS available to verify signature."
            )
            logger.warning(msg)
            return self._record_validation_error(is_primary_token, ConfigurationError(msg))

        try:
            signature = _base64url_decode(sig_b64)
        except Exception as exc:
            logger.warning("OIDC token validation failed: Invalid base64 in signature: %s", exc)
            return self._record_validation_error(
                is_primary_token,
                UnauthorizedError(
                    "OIDC token validation failed: Invalid base64 in signature.",
                    status_code=401,
                ),
            )

        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        verified = any(
            _verify_token_signature(signing_input, signature, alg, k) for k in keys_to_try
        )
        if not verified:
            msg = "OIDC token validation failed: Cryptographic signature verification failed."
            logger.warning(msg)
            return self._record_validation_error(
                is_primary_token, UnauthorizedError(msg, status_code=401)
            )

        try:
            payload_bytes = _base64url_decode(payload_b64)
            claims = json.loads(payload_bytes.decode("utf-8"))
            if not isinstance(claims, dict):
                msg = "OIDC token validation failed: Payload is not a JSON object."
                logger.warning(msg)
                return self._record_validation_error(
                    is_primary_token, UnauthorizedError(msg, status_code=401)
                )
        except Exception as exc:
            logger.warning("OIDC token validation failed: Failed to decode payload: %s", exc)
            return self._record_validation_error(
                is_primary_token,
                UnauthorizedError(
                    "OIDC token validation failed: Failed to decode JWT payload.",
                    status_code=401,
                ),
            )

        temporal_err = self._temporal_claim_error(claims)
        if temporal_err is not None:
            return self._record_validation_error(is_primary_token, temporal_err)

        actual_auds: list[str] | None = None
        if "aud" in claims:
            actual_auds = _normalize_aud_claim(claims["aud"])
            if actual_auds is None:
                msg = "OIDC token validation failed: Invalid 'aud' claim format."
                logger.warning(msg)
                return self._record_validation_error(
                    is_primary_token, UnauthorizedError(msg, status_code=401)
                )

        if self.audience is not None:
            expected_input = (
                list(self.audience) if isinstance(self.audience, (tuple, set)) else self.audience
            )
            expected_auds = _normalize_aud_claim(expected_input)
            if expected_auds is None:
                msg = "OIDC token validation failed: Invalid expected 'audience' configuration."
                logger.warning(msg)
                return self._record_validation_error(is_primary_token, ConfigurationError(msg))
            if actual_auds is None:
                msg = "OIDC token validation failed: Missing required 'aud' claim."
                logger.warning(msg)
                return self._record_validation_error(
                    is_primary_token, UnauthorizedError(msg, status_code=401)
                )
            if not any(a in expected_auds for a in actual_auds):
                msg = "OIDC token validation failed: Audience mismatch."
                logger.warning(msg)
                return self._record_validation_error(
                    is_primary_token, UnauthorizedError(msg, status_code=401)
                )

        if "iss" in claims:
            token_iss = claims["iss"]
            if not isinstance(token_iss, str) or not token_iss.strip():
                msg = "OIDC token validation failed: Invalid 'iss' claim format."
                logger.warning(msg)
                return self._record_validation_error(
                    is_primary_token, UnauthorizedError(msg, status_code=401)
                )

        if self.issuer is not None:
            if not isinstance(self.issuer, str) or not self.issuer.strip():
                msg = "OIDC token validation failed: Invalid expected 'issuer' configuration."
                logger.warning(msg)
                return self._record_validation_error(is_primary_token, ConfigurationError(msg))
            if claims.get("iss") != self.issuer:
                msg = "OIDC token validation failed: Issuer mismatch."
                logger.warning(msg)
                return self._record_validation_error(
                    is_primary_token, UnauthorizedError(msg, status_code=401)
                )

        if is_primary_token:
            self._token_verified = True
        return claims

    def _get_active_claims(self) -> dict[str, Any]:
        """Return verified token claims when currently within the token validity window."""
        if not self._token_verified or not isinstance(self._decoded_claims, dict):
            return {}
        if not self._has_active_temporal_claims(self._decoded_claims):
            return {}
        return dict(self._decoded_claims)

    @property
    def claims(self) -> dict[str, Any]:
        """Return a read-only copy of currently active verified JWT claims."""
        return self._get_active_claims()

    def get_headers(self) -> dict[str, str]:
        """Return the Authorization header or raise an ImednetError on invalid/expired tokens."""
        if not isinstance(self.token, str) or not self.token.strip():
            raise ConfigurationError(
                "OIDC token validation failed: Token must be a non-empty string."
            )
        if "\r" in self.token or "\n" in self.token:
            raise ConfigurationError(
                "OIDC token validation failed: Token must not contain control characters."
            )
        if self._requires_verification:
            if not self._token_verified or not isinstance(self._decoded_claims, dict):
                if self._validation_error is not None:
                    raise self._validation_error
                raise UnauthorizedError(
                    "OIDC token validation failed: Token could not be verified.",
                    status_code=401,
                )
            temporal_err = self._temporal_claim_error(self._decoded_claims)
            if temporal_err is not None:
                raise temporal_err
        return {"Authorization": f"Bearer {self.token}"}

    def get_user_id(self) -> str | None:
        """Extract the user identifier from active verified token claims.

        Returns:
            The user ID (sub or preferred_username) if found and valid, otherwise None.
        """
        active_claims = self._get_active_claims()
        for claim_name in ("sub", "preferred_username"):
            val = active_claims.get(claim_name)
            if val is None or val == "":
                continue
            if isinstance(val, str) and val.strip():
                return val
            logger.warning("OIDC user ID extraction failed: Invalid '%s' claim format.", claim_name)
            return None
        return None

    def get_user_roles(self) -> list[str]:
        """Extract and map user roles from active verified token claims.

        Returns:
            A list of mapped system roles.
        """
        active_claims = self._get_active_claims()
        raw_roles: list[str] = []
        for claim_name in ("roles", "groups"):
            if claim_name not in active_claims or active_claims[claim_name] is None:
                continue
            candidate = active_claims[claim_name]
            if isinstance(candidate, str):
                if not candidate.strip():
                    continue
                raw_roles = [candidate]
                break
            if isinstance(candidate, list):
                if not all(isinstance(item, str) and bool(item.strip()) for item in candidate):
                    logger.warning(
                        "OIDC role extraction failed: '%s' claim contains non-string or blank items.",
                        claim_name,
                    )
                    return []
                if not candidate:
                    continue
                raw_roles = candidate
                break
            logger.warning("OIDC role extraction failed: Invalid '%s' claim format.", claim_name)
            return []

        if not raw_roles:
            return []

        mapped_roles: list[str] = []
        for role in raw_roles:
            r_lower = role.lower()
            if "admin" in r_lower:
                mapped_roles.append("admin")
            elif "manager" in r_lower:
                mapped_roles.append("manager")
            elif "reviewer" in r_lower:
                mapped_roles.append("reviewer")
            else:
                mapped_roles.append("viewer")

        return mapped_roles

    def __repr__(self) -> str:
        """Return a redacted string representation of the OIDC authentication."""
        return "OIDCAuth(token='********')"

    __str__ = __repr__


def _has_alg_none_prefix(token: str) -> bool:
    """Return True when a dotted token segment starts with a JSON header setting alg='none'."""
    if "." not in token:
        return False
    first = token.split(".", 1)[0]
    if not first:
        return False
    try:
        decoded = json.loads(_base64url_decode(first).decode("utf-8"))
    except Exception:
        return False
    return (
        isinstance(decoded, dict)
        and isinstance(decoded.get("alg"), str)
        and decoded["alg"].lower() == "none"
    )


class _ApiKeyOIDCAuth(OIDCAuth):
    """Composite strategy sending static API key headers alongside verified OIDC bearer auth."""

    def __init__(self, token: str, *, api_key_auth: ApiKeyAuth, **kwargs: Any) -> None:
        self._api_key_auth = api_key_auth
        super().__init__(token, **kwargs)

    def get_headers(self) -> dict[str, str]:
        """Validate OIDC bearer token and combine with x-api-key and x-imn-security-key."""
        oidc_headers = super().get_headers()
        return {**self._api_key_auth.get_headers(), **oidc_headers}

    def __repr__(self) -> str:
        """Return a redacted string representation of the composed auth strategy."""
        return "OIDCAuth(api_key='********', security_key='********', token='********')"

    __str__ = __repr__


def build_oidc_auth(
    oidc_token: str,
    *,
    api_key: str | None = None,
    security_key: str | None = None,
) -> OIDCAuth:
    """Create OIDCAuth composed with ApiKeyAuth when both API keys are configured."""
    if (
        isinstance(api_key, str)
        and api_key.strip()
        and isinstance(security_key, str)
        and security_key.strip()
    ):
        return _ApiKeyOIDCAuth(
            oidc_token,
            api_key_auth=ApiKeyAuth(api_key, security_key),
        )
    return OIDCAuth(oidc_token)
