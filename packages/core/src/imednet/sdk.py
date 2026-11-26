"""Public entry-point for the iMednet SDK.

This module provides the ImednetSDK class which:
- Manages configuration and authentication
- Exposes all endpoint functionality through a unified interface
- Provides context management for proper resource cleanup
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.metadata import EntryPoint, entry_points
from typing import TYPE_CHECKING, Any, Never, Self, cast

from .auth.credentials import CredentialSource
from .auth.strategy import AuthStrategy
from .config import Config, load_config, merge_config_overrides
from .constants import DEFAULT_BACKOFF_FACTOR, DEFAULT_RETRIES
from .core.context import (
    reset_strict_mode_context,
    set_strict_mode_context,
    strict_mode_context,
    study_context,
)
from .core.factory import ClientFactory
from .core.retry import DefaultRetryPolicy, RetryConfig, RetryPolicy
from .endpoints.registry import ASYNC_ENDPOINT_REGISTRY, ENDPOINT_REGISTRY
from .errors import PluginLoadError
from .plugins import (
    AsyncSinksNamespaceProtocol,
    AsyncWorkflowsNamespaceProtocol,
    PluginProtocol,
    SinksNamespaceProtocol,
    SinksPluginProtocol,
    SyncWorkflowsNamespaceProtocol,
)
from .sdk_convenience import AsyncSDKConvenienceMixin, SyncSDKConvenienceMixin
from .utils.async_bridge import is_async_facade

if TYPE_CHECKING:
    from .core.async_client import AsyncClient
    from .core.client import Client
    from .endpoints.codings import AsyncCodingsEndpoint, CodingsEndpoint
    from .endpoints.forms import AsyncFormsEndpoint, FormsEndpoint
    from .endpoints.intervals import AsyncIntervalsEndpoint, IntervalsEndpoint
    from .endpoints.jobs import AsyncJobsEndpoint, JobsEndpoint
    from .endpoints.queries import AsyncQueriesEndpoint, QueriesEndpoint
    from .endpoints.record_revisions import AsyncRecordRevisionsEndpoint, RecordRevisionsEndpoint
    from .endpoints.records import AsyncRecordsEndpoint, RecordsEndpoint
    from .endpoints.sites import AsyncSitesEndpoint, SitesEndpoint
    from .endpoints.studies import AsyncStudiesEndpoint, StudiesEndpoint
    from .endpoints.subjects import AsyncSubjectsEndpoint, SubjectsEndpoint
    from .endpoints.users import AsyncUsersEndpoint, UsersEndpoint
    from .endpoints.variables import AsyncVariablesEndpoint, VariablesEndpoint
    from .endpoints.visits import AsyncVisitsEndpoint, VisitsEndpoint
    from .spi.facade import AsyncImednetFacade, ImednetFacade


class WorkflowPluginProtocol(PluginProtocol):
    """Alias kept for backwards compatibility; use :class:`~imednet.plugins.PluginProtocol` instead."""


_SYNC_ONLY_WORKFLOW_NAMES: frozenset[str] = frozenset(
    {
        "data_extraction",
        "duckdb_centralizer",
        "query_management",
        "record_mapper",
        "subject_data",
        "uat",
    }
)


def _factory_supports_async(target: Any) -> bool:
    """Return True if a workflow class, factory, or instance supports AsyncImednetSDK."""
    explicit = getattr(target, "supports_async", None)
    if isinstance(explicit, bool):
        return explicit
    cls = target if inspect.isclass(target) else type(target)
    return any(
        attr.startswith("async_") or inspect.iscoroutinefunction(val)
        for attr, val in inspect.getmembers(cls)
        if not attr.startswith("_")
    )


class WorkflowRegistry:
    """Dynamic registry for resolving and instantiating iMednet workflows.

    Lazily loads plugins registered under the 'imednet.workflows' entrypoint group
    and injects the active SDK client instance upon loading.
    """

    def __init__(self, sdk_instance: Any) -> None:
        """Initialize the registry with an SDK instance."""
        self._sdk = sdk_instance
        self._entry_points: dict[str, EntryPoint] = {}
        for ep in entry_points(group="imednet.workflows"):
            if ep.name in self._entry_points:
                raise PluginLoadError(
                    f"Multiple workflows registered under the name '{ep.name}'. "
                    "Please resolve the conflict."
                )
            self._entry_points[ep.name] = ep
        self._async_entry_points: dict[str, EntryPoint] = {}
        for ep in entry_points(group="imednet.async_workflows"):
            if ep.name in self._async_entry_points:
                raise PluginLoadError(
                    f"Multiple async workflows registered under the name '{ep.name}'. "
                    "Please resolve the conflict."
                )
            self._async_entry_points[ep.name] = ep
        self._loaded_workflows: dict[str, Any] = {}

    def _is_async_sdk(self) -> bool:
        """Return True when the bound SDK is asynchronous."""
        return isinstance(self._sdk, AsyncImednetSDK) or is_async_facade(self._sdk)

    def _raise_sync_only_error(self, name: str) -> Never:
        """Raise TypeError with actionable guidance when a sync-only workflow is requested on AsyncImednetSDK."""
        raise TypeError(
            f"Workflow '{name}' is synchronous-only and cannot be used with AsyncImednetSDK. "
            "Use synchronous ImednetSDK(...) for this workflow, or use an async-capable "
            "workflow ('record_update', 'register_subjects', or 'uat_inspector')."
        )

    def __getattr__(self, name: str) -> Any:
        """Dynamically resolve and instantiate a workflow by name."""
        return self._get_workflow(name)

    def __getitem__(self, name: str) -> Any:
        """Dynamically resolve and instantiate a workflow by item access."""
        return self._get_workflow(name)

    def get(self, name: str, default: Any = None) -> Any:
        """Get a workflow by name, returning default if not found."""
        try:
            return self._get_workflow(name)
        except (ImportError, PluginLoadError):
            return default

    def _get_workflow(self, name: str) -> Any:
        """Load and instantiate a workflow plugin entry point."""
        if name in self._loaded_workflows:
            return self._loaded_workflows[name]

        is_async = self._is_async_sdk()
        from_async_group = False
        if is_async and name in self._async_entry_points:
            ep = self._async_entry_points[name]
            from_async_group = True
        elif name in self._entry_points:
            ep = self._entry_points[name]
        else:
            raise ImportError(f"Workflow '{name}' not found. Please install the required package.")

        try:
            workflow_factory = ep.load()
        except (AttributeError, ImportError, ModuleNotFoundError) as error:
            raise PluginLoadError(
                f"Failed to load workflow plugin from entry point '{ep.value}'."
            ) from error

        if not callable(workflow_factory):
            raise PluginLoadError(
                f"The workflows plugin entry point '{ep.value}' must be a callable "
                f"that accepts an SDK instance; got {type(workflow_factory).__name__}."
            )

        if is_async and not from_async_group:
            if (
                name in _SYNC_ONLY_WORKFLOW_NAMES
                or getattr(workflow_factory, "supports_async", None) is False
            ):
                self._raise_sync_only_error(name)
            if inspect.isclass(workflow_factory) and not _factory_supports_async(workflow_factory):
                self._raise_sync_only_error(name)

        try:
            workflow_instance = workflow_factory(self._sdk)
        except TypeError as error:
            raise PluginLoadError(
                "Failed to instantiate workflows from the discovered plugin entry point."
            ) from error

        if is_async and not from_async_group and not _factory_supports_async(workflow_instance):
            self._raise_sync_only_error(name)

        self._loaded_workflows[name] = workflow_instance
        return workflow_instance


class _BaseSDK:
    """Base class for iMednet SDK variants.

    Provides shared logic for configuration management, plugin discovery,
    and workflow/sink initialization.
    """

    config: Config
    _strict_mode_tokens: list[Any]

    def _init_sdk_config(
        self,
        *,
        config: Config | None,
        api_key: str | None,
        security_key: str | None,
        base_url: str | None,
        oidc_token: str | None,
        timeout: float | None,
        strict_mode: bool | None,
        simulation_mode: bool | None,
        auth: AuthStrategy | None,
        credential_source: CredentialSource | None,
        has_injected_client: bool,
        has_injected_transport: bool = False,
    ) -> Config:
        """Resolve SDK configuration once and initialize common SDK state."""
        if has_injected_client and auth is not None:
            raise ValueError("An injected client already owns authentication")
        effective_source = credential_source if auth is None else None
        require_creds = auth is None and not has_injected_client and not has_injected_transport
        if config is not None:
            resolved = merge_config_overrides(
                config,
                api_key=api_key,
                security_key=security_key,
                base_url=base_url,
                oidc_token=oidc_token,
                timeout=timeout,
                strict_mode=strict_mode,
                simulation_mode=simulation_mode,
                credential_source=effective_source,
                require_credentials=require_creds,
                has_transport=has_injected_transport,
            )
        else:
            resolved = load_config(
                api_key=api_key,
                security_key=security_key,
                base_url=base_url,
                oidc_token=oidc_token,
                timeout=timeout,
                strict_mode=strict_mode,
                simulation_mode=simulation_mode,
                credential_source=effective_source,
                require_credentials=require_creds,
                has_transport=has_injected_transport,
            )
        from .constants import DEFAULT_BASE_URL
        from .utils.url import sanitize_base_url

        self.config = resolved
        self._security_key = resolved.security_key
        raw_base_url = (
            resolved.base_url
            if isinstance(resolved.base_url, str) and resolved.base_url.strip()
            else DEFAULT_BASE_URL
        )
        self._base_url = sanitize_base_url(raw_base_url)
        self._strict_mode_tokens = []
        self._governance_tokens: list[tuple[Any, Any]] = []
        return resolved

    def _enter_strict_mode(self) -> None:
        """Push this SDK's strict_mode and governance context onto the active task/thread context."""
        from .core.context import (
            extract_sdk_security_key,
            set_base_url_context,
            set_security_key_context,
        )

        strict_val = getattr(self.config, "strict_mode", None)
        if isinstance(strict_val, bool):
            self._strict_mode_tokens.append(set_strict_mode_context(strict_val))
        token_base = None
        if hasattr(self, "_base_url") and isinstance(self._base_url, str) and self._base_url:
            token_base = set_base_url_context(self._base_url)
        resolved_sec = extract_sdk_security_key(self)
        token_sec = set_security_key_context(resolved_sec) if resolved_sec is not None else None
        if not hasattr(self, "_governance_tokens"):
            self._governance_tokens = []
        self._governance_tokens.append((token_base, token_sec))

    def _exit_strict_mode(self) -> None:
        """Pop the most recent strict_mode and governance tokens set by this SDK."""
        from .core.context import reset_base_url_context, reset_security_key_context

        if getattr(self, "_governance_tokens", None):
            token_base, token_sec = self._governance_tokens.pop()
            if token_sec is not None:
                reset_security_key_context(token_sec)
            if token_base is not None:
                reset_base_url_context(token_base)
        if getattr(self, "_strict_mode_tokens", None):
            reset_strict_mode_context(self._strict_mode_tokens.pop())

    @staticmethod
    def _resolve_retry_config(
        retry_config: RetryConfig | None,
        retries: int | None,
        backoff_factor: float | None,
        retry_policy: RetryPolicy | None,
    ) -> RetryConfig | None:
        """Resolve explicit retry_config or construct one from SDK retry arguments."""
        if retry_config is not None:
            return retry_config
        if any(x is not None for x in (retries, backoff_factor, retry_policy)):
            return RetryConfig(
                retries=retries if retries is not None else DEFAULT_RETRIES,
                backoff_factor=(
                    backoff_factor if backoff_factor is not None else DEFAULT_BACKOFF_FACTOR
                ),
                retry_policy=retry_policy if retry_policy is not None else DefaultRetryPolicy(),
            )
        return None

    def _get_plugin_entry_point(self, name: str) -> EntryPoint | None:
        """Return the configured plugin entry point."""
        plugin_entry_points = list(entry_points(group="imednet.plugins", name=name))

        if not plugin_entry_points:
            return None
        if len(plugin_entry_points) > 1:
            discovered_plugins = ", ".join(
                sorted(entry_point.value for entry_point in plugin_entry_points)
            )
            raise PluginLoadError(
                f"Multiple '{name}' plugins were found in the 'imednet.plugins' entry-point "
                f"group ({discovered_plugins}). Please keep only one {name} plugin installed."
            )
        return plugin_entry_points[0]

    def _init_workflows(self) -> WorkflowRegistry:
        """Initialize and return the dynamic workflow registry."""
        return WorkflowRegistry(self)

    def _init_sinks(self) -> SinksNamespaceProtocol | AsyncSinksNamespaceProtocol | None:
        """Instantiate sinks namespace when optional sinks plugin is available."""
        sinks_entry_point = self._get_plugin_entry_point("sinks")
        if sinks_entry_point is None:
            return None

        try:
            sinks_plugin = sinks_entry_point.load()
        except (AttributeError, ImportError, ModuleNotFoundError) as error:
            raise PluginLoadError(
                f"Failed to load sinks plugin from entry point '{sinks_entry_point.value}'."
            ) from error

        if not callable(sinks_plugin):
            raise PluginLoadError(
                "The sinks plugin entry point "
                f"'{sinks_entry_point.value}' must be a callable that accepts an SDK "
                f"instance; got {type(sinks_plugin).__name__}."
            )

        try:
            sinks_factory = cast(SinksPluginProtocol, sinks_plugin)
            return sinks_factory(cast("ImednetFacade | AsyncImednetFacade", self))
        except TypeError as error:
            raise PluginLoadError(
                "Failed to instantiate sinks from the discovered plugin entry point."
            ) from error

    @contextmanager
    def study_context(self, study_key: str) -> Iterator[Self]:
        """Set a temporary default study key for the current thread/task context."""
        from .core.context import sdk_governance_context

        strict_val = getattr(getattr(self, "config", None), "strict_mode", None)
        with sdk_governance_context(self), study_context(study_key):
            if isinstance(strict_val, bool):
                with strict_mode_context(strict_val):
                    yield self
            else:
                yield self


class ImednetSDK(_BaseSDK, SyncSDKConvenienceMixin):
    """Public entry-point for library users.

    Provides access to all iMednet API endpoints and maintains configuration.
    """

    codings: CodingsEndpoint
    forms: FormsEndpoint
    intervals: IntervalsEndpoint
    jobs: JobsEndpoint
    queries: QueriesEndpoint
    record_revisions: RecordRevisionsEndpoint
    records: RecordsEndpoint
    sites: SitesEndpoint
    studies: StudiesEndpoint
    subjects: SubjectsEndpoint
    users: UsersEndpoint
    variables: VariablesEndpoint
    visits: VisitsEndpoint
    workflows: SyncWorkflowsNamespaceProtocol
    sinks: SinksNamespaceProtocol | None
    config: Config

    def __init__(
        self,
        api_key: str | None = None,
        security_key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        strict_mode: bool | None = None,
        retry_config: RetryConfig | None = None,
        client: Client | None = None,
        retries: int | None = None,
        backoff_factor: float | None = None,
        retry_policy: RetryPolicy | None = None,
        simulation_mode: bool | None = None,
        transport: Any = None,
        *,
        config: Config | None = None,
        oidc_token: str | None = None,
        auth: AuthStrategy | None = None,
        credential_source: CredentialSource | None = None,
    ) -> None:
        """Initialize the SDK with credentials and configuration.

        Args:
            api_key: iMednet API key.
            security_key: iMednet security key.
            base_url: Base URL for the iMednet API.
            timeout: Default request timeout in seconds.
            strict_mode: Toggle strict mode for data validation.
            retry_config: Centralized configuration for retry behaviors.
            client: Pre-configured client instance.
            retries: Number of retries for failed requests.
            backoff_factor: Backoff factor for retry delays.
            retry_policy: Custom retry policy.
            simulation_mode: Toggle offline simulation mode.
            transport: Custom HTTP transport or SimulationTransport.
            config: Pre-resolved configuration instance.
            oidc_token: Explicit OIDC bearer token.
            auth: Explicit authentication strategy, used instead of default credentials.
            credential_source: Caller-owned credential source for client construction.
        """
        resolved_config = self._init_sdk_config(
            config=config,
            api_key=api_key,
            security_key=security_key,
            base_url=base_url,
            oidc_token=oidc_token,
            timeout=timeout,
            strict_mode=strict_mode,
            simulation_mode=simulation_mode,
            auth=auth,
            credential_source=credential_source,
            has_injected_client=client is not None,
            has_injected_transport=transport is not None,
        )

        if client is not None:
            self._client = client
            injected_url = getattr(client, "base_url", None)
            injected_sim = getattr(client, "simulation_mode", False) is True
            if (
                self.config.base_url is None
                and isinstance(injected_url, str)
                and injected_url.strip()
            ) or (injected_sim and not self.config.simulation_mode):
                from dataclasses import replace

                self.config = replace(
                    self.config,
                    base_url=self.config.base_url or injected_url,
                    simulation_mode=self.config.simulation_mode or injected_sim,
                )
        else:
            retry_config = self._resolve_retry_config(
                retry_config=retry_config,
                retries=retries,
                backoff_factor=backoff_factor,
                retry_policy=retry_policy,
            )

            self._client = ClientFactory.create_client(
                config=resolved_config,
                timeout=resolved_config.timeout,
                retry_config=retry_config,
                transport=transport,
                auth=auth,
            )

        self._init_endpoints()
        self.workflows = cast(SyncWorkflowsNamespaceProtocol, self._init_workflows())
        self.sinks = cast("SinksNamespaceProtocol | None", self._init_sinks())

    @property
    def auth(self) -> AuthStrategy | None:
        """Return the authentication provider used by the client."""
        return cast("AuthStrategy | None", getattr(self._client, "auth", None))

    @property
    def retry_policy(self) -> RetryPolicy:
        """Return the current retry policy."""
        return self._client.retry_policy  # type: ignore[no-any-return]

    @retry_policy.setter
    def retry_policy(self, policy: RetryPolicy) -> None:
        """Set a new retry policy for the client."""
        self._client.retry_policy = policy

    def _init_endpoints(self) -> None:
        """Prepare lazy endpoint instantiation state for this SDK instance."""
        strict_val = getattr(self.config, "strict_mode", None)
        self._endpoint_strict_mode: bool | None = (
            strict_val if isinstance(strict_val, bool) else None
        )
        for attr in ENDPOINT_REGISTRY:
            self.__dict__.pop(attr, None)

    if not TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any:
            """Lazily instantiate synchronous endpoint clients on first access."""
            try:
                endpoint_cls = ENDPOINT_REGISTRY[name]
            except KeyError:
                raise AttributeError(
                    f"'{type(self).__name__}' object has no attribute {name!r}"
                ) from None
            endpoint = endpoint_cls(self._client)
            endpoint._strict_mode = getattr(self, "_endpoint_strict_mode", None)
            setattr(self, name, endpoint)
            return endpoint

        def __dir__(self) -> list[str]:
            """Include lazy endpoint attributes in dir()."""
            return sorted(set(super().__dir__()) | set(ENDPOINT_REGISTRY))

    def __enter__(self) -> ImednetSDK:
        """Support for context manager protocol."""
        self._enter_strict_mode()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Cleanup resources when exiting context."""
        try:
            self.close()
        finally:
            self._exit_strict_mode()

    async def __aenter__(self) -> ImednetSDK:
        """Prevent accidental ``async with`` usage on the synchronous client."""
        raise TypeError(
            "ImednetSDK is a synchronous client. "
            "Use 'with ImednetSDK(...):' instead of 'async with'. "
            "If you require async execution, use AsyncImednetSDK."
        )

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Prevent direct or indirect invocation of the async exit on the sync client."""
        raise TypeError(
            "ImednetSDK is a synchronous client. "
            "Use 'with ImednetSDK(...):' instead of 'async with'. "
            "If you require async execution, use AsyncImednetSDK."
        )

    def close(self) -> None:
        """Close the synchronous client connection and free resources."""
        self._client.close()

    async def aclose(self) -> None:
        """Asynchronous close is not supported on the synchronous SDK."""
        raise TypeError(
            "ImednetSDK is a synchronous client. "
            "Use `sdk.close()` or `with ImednetSDK(...)` instead. "
            "If you require async execution, use AsyncImednetSDK."
        )


class AsyncImednetSDK(_BaseSDK, AsyncSDKConvenienceMixin):
    """Async variant of :class:`ImednetSDK` using the async HTTP client.

    Always use this class with ``async with`` or call ``await sdk.aclose()``
    explicitly when done. Using the synchronous context manager (``with``) or
    synchronous ``close()`` on this class will raise a :exc:`TypeError`.
    """

    codings: AsyncCodingsEndpoint
    forms: AsyncFormsEndpoint
    intervals: AsyncIntervalsEndpoint
    jobs: AsyncJobsEndpoint
    queries: AsyncQueriesEndpoint
    record_revisions: AsyncRecordRevisionsEndpoint
    records: AsyncRecordsEndpoint
    sites: AsyncSitesEndpoint
    studies: AsyncStudiesEndpoint
    subjects: AsyncSubjectsEndpoint
    users: AsyncUsersEndpoint
    variables: AsyncVariablesEndpoint
    visits: AsyncVisitsEndpoint
    workflows: AsyncWorkflowsNamespaceProtocol
    sinks: AsyncSinksNamespaceProtocol | None

    def __init__(
        self,
        api_key: str | None = None,
        security_key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        strict_mode: bool | None = None,
        retry_config: RetryConfig | None = None,
        async_client: AsyncClient | None = None,
        retries: int | None = None,
        backoff_factor: float | None = None,
        retry_policy: RetryPolicy | None = None,
        simulation_mode: bool | None = None,
        transport: Any = None,
        *,
        config: Config | None = None,
        oidc_token: str | None = None,
        auth: AuthStrategy | None = None,
        credential_source: CredentialSource | None = None,
    ) -> None:
        """Initialize the asynchronous SDK."""
        resolved_config = self._init_sdk_config(
            config=config,
            api_key=api_key,
            security_key=security_key,
            base_url=base_url,
            oidc_token=oidc_token,
            timeout=timeout,
            strict_mode=strict_mode,
            simulation_mode=simulation_mode,
            auth=auth,
            credential_source=credential_source,
            has_injected_client=async_client is not None,
            has_injected_transport=transport is not None,
        )

        if async_client is not None:
            self._async_client = async_client
            injected_url = getattr(async_client, "base_url", None)
            injected_sim = getattr(async_client, "simulation_mode", False) is True
            if (
                self.config.base_url is None
                and isinstance(injected_url, str)
                and injected_url.strip()
            ) or (injected_sim and not self.config.simulation_mode):
                from dataclasses import replace

                self.config = replace(
                    self.config,
                    base_url=self.config.base_url or injected_url,
                    simulation_mode=self.config.simulation_mode or injected_sim,
                )
        else:
            retry_config = self._resolve_retry_config(
                retry_config=retry_config,
                retries=retries,
                backoff_factor=backoff_factor,
                retry_policy=retry_policy,
            )

            self._async_client = ClientFactory.create_async_client(
                config=resolved_config,
                timeout=resolved_config.timeout,
                retry_config=retry_config,
                transport=transport,
                auth=auth,
            )

        self._init_endpoints()
        self.workflows = cast(AsyncWorkflowsNamespaceProtocol, self._init_workflows())
        self.sinks = cast("AsyncSinksNamespaceProtocol | None", self._init_sinks())

    @property
    def auth(self) -> AuthStrategy | None:
        """Return the authentication provider used by the async client."""
        return cast("AuthStrategy | None", getattr(self._async_client, "auth", None))

    @property
    def retry_policy(self) -> RetryPolicy:
        """Return the current retry policy of the async client."""
        from typing import cast

        return cast('RetryPolicy', self._async_client.retry_policy)

    @retry_policy.setter
    def retry_policy(self, policy: RetryPolicy) -> None:
        """Set a new retry policy for the async client."""
        self._async_client.retry_policy = policy

    def _init_endpoints(self) -> None:
        """Prepare lazy asynchronous endpoint instantiation state for this SDK instance."""
        strict_val = getattr(self.config, "strict_mode", None)
        self._endpoint_strict_mode: bool | None = (
            strict_val if isinstance(strict_val, bool) else None
        )
        for attr in ASYNC_ENDPOINT_REGISTRY:
            self.__dict__.pop(attr, None)

    if not TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any:
            """Lazily instantiate asynchronous endpoint clients on first access."""
            try:
                endpoint_cls = ASYNC_ENDPOINT_REGISTRY[name]
            except KeyError:
                raise AttributeError(
                    f"'{type(self).__name__}' object has no attribute {name!r}"
                ) from None
            endpoint = endpoint_cls(self._async_client)
            endpoint._strict_mode = getattr(self, "_endpoint_strict_mode", None)
            setattr(self, name, endpoint)
            return endpoint

        def __dir__(self) -> list[str]:
            """Include lazy asynchronous endpoint attributes in dir()."""
            return sorted(set(super().__dir__()) | set(ASYNC_ENDPOINT_REGISTRY))

    def close(self) -> None:
        """Raise TypeError as sync close is not supported."""
        raise TypeError(
            "AsyncImednetSDK does not support the synchronous close() method. "
            "Use `await sdk.aclose()` or `async with AsyncImednetSDK(...)` instead."
        )

    def __enter__(self) -> AsyncImednetSDK:
        """Synchronous context manager is not supported."""
        raise TypeError(
            "AsyncImednetSDK does not support the synchronous context manager protocol. "
            "Use `async with AsyncImednetSDK(...)` instead."
        )

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Synchronous context manager is not supported."""
        raise TypeError(
            "AsyncImednetSDK does not support the synchronous context manager protocol. "
            "Use `async with AsyncImednetSDK(...)` instead."
        )

    async def __aenter__(self) -> AsyncImednetSDK:
        """Enter the asynchronous context manager."""
        self._enter_strict_mode()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exit the asynchronous context manager and close the client."""
        try:
            await self.aclose()
        finally:
            self._exit_strict_mode()

    async def aclose(self) -> None:
        """Close the underlying asynchronous client and free resources."""
        await self._async_client.aclose()


__all__ = ["AsyncImednetSDK", "ImednetSDK"]
