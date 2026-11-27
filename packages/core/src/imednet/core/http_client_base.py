"""Base class for HTTP clients with shared initialization and configuration."""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import threading
from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import Generator, Mapping, Sequence
from typing import Any, Generic, TypeVar, cast
from urllib.parse import urlsplit

import httpx

from imednet.auth.credentials import CredentialSource
from imednet.auth.strategy import AuthStrategy
from imednet.config import Config
from imednet.constants import (
    CONTENT_TYPE_JSON,
    HEADER_ACCEPT,
    HEADER_API_KEY,
    HEADER_CONTENT_TYPE,
    HEADER_SECURITY_KEY,
)
from imednet.core.http.executor import BaseRequestExecutor
from imednet.core.operations.circuit_breaker import CircuitBreaker, CircuitState
from imednet.errors.client import ClientError

from .base_client import BaseClient, Tracer  # type: ignore[attr-defined]
from .retry import RetryConfig, RetryPolicy

logger = logging.getLogger(__name__)

ClientT = TypeVar("ClientT", bound=httpx.Client | httpx.AsyncClient)
ExecutorT = TypeVar("ExecutorT", bound=BaseRequestExecutor)

_DEFAULT_SCHEME_PORTS: dict[str, int] = {"http": 80, "https": 443}
_CREDENTIAL_HEADER_NAMES: frozenset[str] = frozenset(
    {
        HEADER_API_KEY.lower(),
        HEADER_SECURITY_KEY.lower(),
        "authorization",
    }
)
_BLOCKED_EXTERNAL_HOSTS: frozenset[str] = frozenset(
    {
        "localhost",
        "metadata.google.internal",
        "metadata",
        "instance-data",
    }
)
_OBFUSCATED_IP_LABEL_RE = re.compile(r"^(?:0x[0-9a-f]+|\d+)$", re.IGNORECASE)
_MAX_EXTERNAL_REDIRECTS = 5
_MAX_EXTERNAL_CIRCUIT_BREAKERS = 128
_ORIGINAL_GETADDRINFO = socket.getaddrinfo


class ExternalUrlError(ClientError, ValueError):
    """Raised when an external URL violates scheme, host, SSRF, or redirect rules."""


def _url_origin(url: httpx.URL | str) -> tuple[str, str, int | None]:
    """Return normalized (scheme, host, effective_port) origin tuple for a URL."""
    parsed = httpx.URL(url) if isinstance(url, str) else url
    scheme = parsed.scheme.lower()
    host = parsed.host.lower()
    port = parsed.port if parsed.port is not None else _DEFAULT_SCHEME_PORTS.get(scheme)
    return (scheme, host, port)


class _OriginScopedAuth(httpx.Auth):
    """Attach AuthStrategy credentials only to requests matching the configured base_url origin."""

    def __init__(self, strategy: AuthStrategy, base_url: httpx.URL | str) -> None:
        self._strategy = strategy
        self._origin = _url_origin(base_url)

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        """Inject credentials for same-origin requests and strip them cross-origin."""
        strip_forced = bool(request.extensions.get("strip_credentials"))
        if not strip_forced and _url_origin(request.url) == self._origin:
            auth_headers = self._strategy.get_headers()
            lower_auth_keys = {key.lower() for key in auth_headers}
            for key, value in auth_headers.items():
                request.headers[key] = value
            if "authorization" not in lower_auth_keys:
                for existing in list(request.headers.keys()):
                    if existing.lower() == "authorization":
                        del request.headers[existing]
        else:
            for existing in list(request.headers.keys()):
                if existing.lower() in _CREDENTIAL_HEADER_NAMES:
                    del request.headers[existing]
        yield request


def _validate_ip_address(raw_ip: str) -> None:
    """Reject loopback, link-local, metadata, private, multicast, reserved, or unspecified IPs."""
    addr = ipaddress.ip_address(raw_ip.split("%", 1)[0])
    effective = (
        addr.ipv4_mapped
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None
        else addr
    )
    if (
        not effective.is_global
        or effective.is_loopback
        or effective.is_link_local
        or effective.is_private
        or effective.is_multicast
        or effective.is_reserved
        or effective.is_unspecified
    ):
        raise ExternalUrlError(f"Blocked non-public IP address for external URL: {raw_ip}")


def _parse_and_validate_external_url(url: str) -> tuple[str, str | None, int]:
    """Validate external HTTPS URL syntax, host rules, and literal IP safety."""
    if not isinstance(url, str):
        raise ExternalUrlError("External URL must be a string")
    stripped = url.strip()
    if not stripped or any(ord(ch) <= 32 or ord(ch) == 127 for ch in stripped):
        raise ExternalUrlError("External URL must be a non-empty valid URL without whitespace")

    try:
        parts = urlsplit(stripped)
        raw_port = parts.port
    except ValueError as exc:
        raise ExternalUrlError(f"Malformed external URL: {stripped!r}") from exc
    if parts.scheme.lower() != "https":
        raise ExternalUrlError(
            f"External URL must use https:// scheme, got {parts.scheme or 'missing'}://"
        )
    if parts.username is not None or parts.password is not None:
        raise ExternalUrlError("External URL must not include userinfo credentials")
    if not parts.hostname or "%" in parts.netloc:
        raise ExternalUrlError("External URL must include a valid host")

    port = raw_port or 443
    host = parts.hostname.rstrip(".").lower()
    if not host:
        raise ExternalUrlError("External URL must include a valid host")

    if host in _BLOCKED_EXTERNAL_HOSTS or host.endswith((".localhost", ".local", ".internal")):
        raise ExternalUrlError(f"Blocked internal hostname for external URL: {host}")

    try:
        ip_literal = ipaddress.ip_address(host)
    except ValueError:
        ip_literal = None

    if ip_literal is not None:
        _validate_ip_address(host)
        return stripped, None, port

    labels = [lbl for lbl in host.split(".") if lbl]
    if labels and all(_OBFUSCATED_IP_LABEL_RE.match(lbl) for lbl in labels):
        raise ExternalUrlError(f"Blocked obfuscated IP literal for external URL: {host}")

    return stripped, host, port


def _validate_addrinfo_results(infos: Sequence[Any], host: str) -> None:
    """Validate every IP returned by getaddrinfo against SSRF restrictions."""
    if not infos:
        raise ExternalUrlError(f"Unable to resolve external host: {host}")
    for entry in infos:
        sockaddr = entry[4]
        _validate_ip_address(str(sockaddr[0]))


class HTTPClientBase(BaseClient, ABC, Generic[ClientT, ExecutorT]):
    """Shared logic for synchronous and asynchronous HTTP clients."""

    _client: ClientT
    _executor: ExecutorT
    _circuit_breaker: CircuitBreaker
    _external_executor: ExecutorT
    _external_breakers_lock: threading.RLock
    _max_external_circuit_breakers: int
    _external_circuit_breakers: OrderedDict[tuple[str, str, int | None], CircuitBreaker]

    @abstractmethod
    def _get_client_class(self) -> type[ClientT]:
        """Return the underlying httpx client class."""

    @abstractmethod
    def _create_executor(
        self, client: ClientT, retry_config: RetryConfig | None = None
    ) -> ExecutorT:
        """Create the request executor."""

    def __init__(
        self,
        api_key: str | None = None,
        security_key: str | None = None,
        base_url: str | None = None,
        timeout: float | httpx.Timeout | None = None,
        tracer: Tracer | None = None,
        retry_config: RetryConfig | None = None,
        auth: AuthStrategy | None = None,
        transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None,
        simulation_mode: bool | None = None,
        *,
        config: Config | None = None,
        oidc_token: str | None = None,
        strict_mode: bool | None = None,
        credential_source: CredentialSource | None = None,
    ) -> None:
        """Initialize the HTTP client and request executor."""
        super().__init__(
            api_key=api_key,
            security_key=security_key,
            base_url=base_url,
            timeout=timeout,
            tracer=tracer,
            retry_config=retry_config,
            auth=auth,
            transport=transport,
            simulation_mode=simulation_mode,
            config=config,
            oidc_token=oidc_token,
            strict_mode=strict_mode,
            credential_source=credential_source,
        )

        self._circuit_breaker = CircuitBreaker()
        self._executor = self._create_executor(self._client, self.retry_config)
        self._executor.circuit_breaker = self._circuit_breaker

        self._external_breakers_lock = threading.RLock()
        self._max_external_circuit_breakers = _MAX_EXTERNAL_CIRCUIT_BREAKERS
        self._external_circuit_breakers = OrderedDict()
        self._external_executor = self._create_executor(self._client, self.retry_config)

    def _evict_excess_external_breakers(self) -> None:
        """Bound external origin circuit breakers using LRU eviction under lock."""
        max_entries = max(1, self._max_external_circuit_breakers)
        while len(self._external_circuit_breakers) > max_entries:
            self._external_circuit_breakers.popitem(last=False)

    def _acquire_external_origin_breaker(
        self, url: httpx.URL | str
    ) -> tuple[tuple[str, str, int | None], CircuitBreaker, bool]:
        """Return ``(origin, breaker, existed)`` without caching untrusted redirect hops yet."""
        origin = _url_origin(url)
        with self._external_breakers_lock:
            existing = self._external_circuit_breakers.get(origin)
            if existing is not None:
                self._external_circuit_breakers.move_to_end(origin)
                return origin, existing, True
        return origin, CircuitBreaker(), False

    def _store_external_origin_breaker(
        self,
        origin: tuple[str, str, int | None],
        breaker: CircuitBreaker,
    ) -> CircuitBreaker:
        """Persist a terminal hop's origin circuit breaker in the LRU-bounded map."""
        with self._external_breakers_lock:
            existing = self._external_circuit_breakers.get(origin)
            if existing is not None:
                self._external_circuit_breakers.move_to_end(origin)
                if existing is not breaker:
                    if breaker.state == CircuitState.OPEN:
                        while existing.state == CircuitState.CLOSED:
                            existing.record_failure()
                    elif breaker.consecutive_failures > 0:
                        for _ in range(breaker.consecutive_failures):
                            existing.record_failure()
                return existing
            self._external_circuit_breakers[origin] = breaker
            self._evict_excess_external_breakers()
            return breaker

    def _get_external_circuit_breaker(self, url: httpx.URL | str) -> CircuitBreaker:
        """Return or create the LRU-tracked ``CircuitBreaker`` scoped to *url*'s origin."""
        origin = _url_origin(url)
        with self._external_breakers_lock:
            existing = self._external_circuit_breakers.get(origin)
            if existing is not None:
                self._external_circuit_breakers.move_to_end(origin)
                return existing
            breaker = CircuitBreaker()
            self._external_circuit_breakers[origin] = breaker
            self._evict_excess_external_breakers()
            return breaker

    def _create_client(self, auth: AuthStrategy) -> ClientT:
        """Create the underlying httpx client with origin-scoped authentication."""
        headers = {
            HEADER_ACCEPT: CONTENT_TYPE_JSON,
            HEADER_CONTENT_TYPE: CONTENT_TYPE_JSON,
        }

        client_cls = self._get_client_class()
        client_kwargs: dict[str, Any] = {
            "base_url": self._base_url,
            "headers": headers,
            "timeout": self.timeout,
            "auth": _OriginScopedAuth(auth, self._base_url),
        }
        if getattr(self, "_transport", None) is not None:
            client_kwargs["transport"] = self._transport

        client: Any = client_cls(**client_kwargs)
        return cast(ClientT, client)

    def _should_resolve_external_dns(self) -> bool:
        """Return True when live DNS resolution should run for external hostnames."""
        if socket.getaddrinfo is not _ORIGINAL_GETADDRINFO:
            return True
        if getattr(self, "_transport", None) is not None:
            return False
        raw_transport = getattr(self._client, "_transport", None)
        if not isinstance(raw_transport, (httpx.HTTPTransport, httpx.AsyncHTTPTransport)):
            return False
        is_async = isinstance(raw_transport, httpx.AsyncHTTPTransport)
        handler_attr = "handle_async_request" if is_async else "handle_request"
        handle_fn = getattr(raw_transport, handler_attr, None)
        if getattr(handle_fn, "__module__", "").startswith(("respx", "unittest.mock")):
            return False
        pool = getattr(raw_transport, "_pool", None)
        pool_fn = getattr(pool, handler_attr, None)
        return not getattr(pool_fn, "__module__", "").startswith(("respx", "unittest.mock"))

    def _prepare_external_kwargs(self, target_url: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Prepare request kwargs with redirects disabled and all credentials stripped."""
        _ = target_url
        prepared = dict(kwargs)
        prepared["follow_redirects"] = False
        raw_ext = prepared.get("extensions")
        ext_dict = dict(raw_ext) if isinstance(raw_ext, Mapping) else {}
        ext_dict["strip_credentials"] = True
        prepared["extensions"] = ext_dict
        raw_headers = prepared.get("headers")
        if isinstance(raw_headers, Mapping):
            prepared["headers"] = {
                k: v
                for k, v in raw_headers.items()
                if str(k).lower() not in _CREDENTIAL_HEADER_NAMES
            }
        return prepared

    @staticmethod
    def _resolve_external_redirect(
        current_url: str, response: httpx.Response, hop: int
    ) -> str | None:
        """Resolve 3xx redirect target URLs for per-hop HTTPS and SSRF validation."""
        if not (300 <= response.status_code < 400) or response.status_code == 304:
            return None
        if hop >= _MAX_EXTERNAL_REDIRECTS:
            raise ExternalUrlError("Maximum redirects exceeded for external URL")
        location = response.headers.get("Location")
        if not location or not location.strip():
            raise ExternalUrlError(
                f"Redirect response {response.status_code} missing Location header"
            )
        try:
            return str(httpx.URL(current_url).join(location.strip()))
        except httpx.InvalidURL as exc:
            raise ExternalUrlError(f"Malformed redirect Location header: {location!r}") from exc

    @property
    def retry_policy(self) -> RetryPolicy:
        """Return the current retry policy of the executor."""
        return cast(RetryPolicy, self._executor.retry_config.retry_policy)

    @retry_policy.setter
    def retry_policy(self, policy: RetryPolicy) -> None:
        """Set a new retry policy for the executor."""
        self._executor.retry_config.retry_policy = policy
        self._external_executor.retry_config.retry_policy = policy

    @abstractmethod
    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Make an HTTP request. Return type varies by async/sync client."""
