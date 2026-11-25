"""Airflow hook for retrieving an :class:`ImednetSDK` instance."""

from __future__ import annotations

import contextlib
import json
import sys
from collections.abc import Mapping, MutableMapping
from contextlib import closing
from datetime import date, datetime
from typing import Any, TypeAlias, Union, cast
from urllib.parse import quote_plus, urlsplit

from imednet import AsyncImednetSDK, Config, ImednetSDK, load_config
from imednet.spi.utils import (
    is_boolean_token,
    parse_bool,
    redact_sensitive_payload,
    redact_sensitive_text,
    sanitize_base_url,
)

from .._airflow_compat import (
    AirflowNotFoundException,
    AirflowSecretsBackendAccessDenied,
    BaseHook,
)

Primitive = Union[str, int, float, bool, None]  # noqa: UP007
# Primitive-only payload contract for discovery helpers that feed Airflow mapping/XCom.
PrimitiveContainer: TypeAlias = (
    Primitive | list["PrimitiveContainer"] | dict[str, "PrimitiveContainer"]
)
_SENSITIVE_KEYS = {
    "api_key",
    "auth_token",
    "authorization",
    "client_secret",
    "oidc_token",
    "passphrase",
    "password",
    "private_key",
    "private_key_passphrase",
    "secret",
    "secret_key",
    "security_key",
    "token",
    "x-api-key",
    "x-imn-security-key",
}


class ImednetHook(BaseHook):
    """Retrieve an :class:`ImednetSDK` instance from an Airflow connection."""

    conn_name_attr = "imednet_conn_id"
    default_conn_name = "imednet_default"
    conn_type = "imednet"
    hook_name = "iMednet"

    def __init__(self, imednet_conn_id: str = default_conn_name) -> None:
        """Initialize the Imednet hook."""
        super().__init__()
        self.imednet_conn_id = imednet_conn_id
        if self.conn_name_attr != "imednet_conn_id":
            setattr(self, self.conn_name_attr, imednet_conn_id)

    @classmethod
    def get_ui_field_behaviour(cls) -> dict[str, Any]:
        """Return Airflow UI connection form customizations for ``imednet``."""
        return {
            "hidden_fields": ["host", "schema", "port", "extra"],
            "relabeling": {
                "login": "API Key",
                "password": "Security Key",  # pragma: allowlist secret
            },
            "placeholders": {
                "login": "iMednet API Key",
                "password": "iMednet Security Key",  # pragma: allowlist secret
                "extra__imednet__base_url": "https://edc.prod.imednetapi.com",
                "extra__imednet__timeout": "30.0",
            },
        }

    @staticmethod
    def _string_or_none(value: object) -> str | None:
        """Return a stripped string or ``None`` for non-string/blank values."""
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        return cleaned or None

    @staticmethod
    def _parsed_extras(value: object) -> MutableMapping[str, object] | None:
        """Return parsed connection extras when value is a dict-like payload."""
        if isinstance(value, Mapping):
            return cast(MutableMapping[str, object], dict(value))
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return None
            if isinstance(parsed, dict):
                return cast(MutableMapping[str, object], parsed)
        return None

    @classmethod
    def _connection_extras(cls, conn: object) -> MutableMapping[str, object]:
        """Resolve extras from Airflow connection objects across API versions."""
        extras = cls._parsed_extras(getattr(conn, "extra_dejson", None))
        if extras is not None:
            return extras

        get_extra = getattr(conn, "get_extra", None)
        if callable(get_extra):
            try:
                raw_extra = get_extra()
            except (AttributeError, TypeError, ValueError):
                raw_extra = None
            extras = cls._parsed_extras(raw_extra)
            if extras is not None:
                return extras

        extras = cls._parsed_extras(getattr(conn, "extra", None))
        if extras is not None:
            return extras
        return {}

    @staticmethod
    def _port_or_none(value: object) -> int | None:
        """Return a positive integer port or ``None``."""
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        if isinstance(value, str) and value.strip().isdigit():
            parsed = int(value.strip())
            return parsed if parsed > 0 else None
        return None

    @staticmethod
    def _float_or_none(value: object) -> float | None:
        """Return a finite numeric value as float or ``None``."""
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, str) and value.strip():
            try:
                return float(value.strip())
            except ValueError:
                return None
        return None

    @staticmethod
    def _bool_or_none(value: object) -> bool | None:
        """Return a parsed boolean value or ``None`` when unset/unrecognized."""
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return bool(value)
        if isinstance(value, str) and is_boolean_token(value):
            return parse_bool(value)
        return None

    @staticmethod
    def _supervisor_comms() -> Any:
        """Return active Airflow Task SDK ``SUPERVISOR_COMMS`` instance when present."""
        try:
            from airflow.sdk.execution_time import task_runner
        except (ImportError, ModuleNotFoundError):
            return None
        return getattr(task_runner, "SUPERVISOR_COMMS", None)

    @staticmethod
    def _orm_get_connection_from_secrets(
        conn_id: str,
        *,
        fallback_exc: BaseException,
    ) -> Any:
        """Load a database/secrets Connection via Airflow ORM when SUPERVISOR_COMMS is absent."""
        try:
            from airflow import settings
            from airflow.models.connection import Connection as OrmConnection
        except (ImportError, ModuleNotFoundError):
            raise fallback_exc from None

        with contextlib.suppress(Exception):
            if getattr(settings, "Session", None) is None:
                configure_orm = getattr(settings, "configure_orm", None)
                if callable(configure_orm):
                    configure_orm()

        return OrmConnection.get_connection_from_secrets(conn_id)

    @classmethod
    def _get_airflow_connection(cls, conn_id: str) -> Any:
        """Retrieve an Airflow connection across SDK, ORM fallback, and compatibility shims."""
        hook_cls: Any
        try:
            from airflow.sdk.bases.hook import BaseHook as SdkBaseHook

            hook_cls = SdkBaseHook
        except (ImportError, ModuleNotFoundError):
            hook_cls = BaseHook

        get_conn_attr = hook_cls.__dict__.get("get_connection")
        get_mod = getattr(getattr(get_conn_attr, "__func__", get_conn_attr), "__module__", "")
        is_stock_get = bool(get_mod and get_mod.startswith("airflow."))

        try:
            return hook_cls.get_connection(conn_id)
        except AirflowSecretsBackendAccessDenied:
            raise
        except AirflowNotFoundException as exc:
            if is_stock_get and cls._supervisor_comms() is None:
                return cls._orm_get_connection_from_secrets(conn_id, fallback_exc=exc)
            raise

    @classmethod
    async def _aget_airflow_connection(cls, conn_id: str) -> Any:
        """Retrieve an Airflow connection asynchronously in triggerer/async contexts."""
        import asyncio

        if "aget_connection" in cls.__dict__:
            custom_aget = cls.__dict__["aget_connection"]
            fn = getattr(custom_aget, "__func__", custom_aget)
            try:
                return (
                    await fn(cls, conn_id)
                    if hasattr(custom_aget, "__func__")
                    else await fn(conn_id)
                )
            except AirflowSecretsBackendAccessDenied:
                raise
            except RuntimeError:
                return await asyncio.to_thread(cls._get_airflow_connection, conn_id)

        if "_get_airflow_connection" in cls.__dict__ and cls is not ImednetHook:
            return await asyncio.to_thread(cls._get_airflow_connection, conn_id)

        hook_cls: Any
        try:
            from airflow.sdk.bases.hook import BaseHook as SdkBaseHook

            hook_cls = SdkBaseHook
        except (ImportError, ModuleNotFoundError):
            hook_cls = BaseHook

        get_conn_attr = hook_cls.__dict__.get("get_connection")
        aget_conn_attr = hook_cls.__dict__.get("aget_connection")
        aget_conn = getattr(hook_cls, "aget_connection", None)
        # Preserve synchronous monkeypatches on BaseHook.get_connection in unit tests
        sync_patched = getattr(getattr(get_conn_attr, "__func__", get_conn_attr), "__module__", "")
        aget_mod = getattr(getattr(aget_conn_attr, "__func__", aget_conn_attr), "__module__", "")
        is_stock_aget = bool(aget_mod and aget_mod.startswith("airflow."))
        if callable(aget_conn) and (not sync_patched or sync_patched.startswith("airflow.")):
            try:
                return await aget_conn(conn_id)
            except AirflowSecretsBackendAccessDenied:
                raise
            except AirflowNotFoundException as exc:
                comms = cls._supervisor_comms()
                if is_stock_aget and comms is None:
                    return await asyncio.to_thread(
                        cls._orm_get_connection_from_secrets,
                        conn_id,
                        fallback_exc=exc,
                    )
                if (
                    is_stock_aget
                    and comms is not None
                    and not callable(getattr(comms, "".join(("a", "send")), None))
                ):
                    return await asyncio.to_thread(hook_cls.get_connection, conn_id)
                raise
            except RuntimeError:
                return await asyncio.to_thread(cls._get_airflow_connection, conn_id)
        return await asyncio.to_thread(cls._get_airflow_connection, conn_id)

    @classmethod
    def resolve_destination_connection(cls, conn_id: str, destination: str) -> dict[str, Any]:
        """Resolve destination sink or SQL connection settings at task runtime."""
        conn = cls._get_airflow_connection(conn_id)
        extras = cls._connection_extras(conn)
        dest = destination.lower().strip()

        if dest == "snowflake":
            candidates: dict[str, Any] = {
                "account": (
                    cls._string_or_none(extras.get("account"))
                    or cls._string_or_none(extras.get("extra__snowflake__account"))
                    or cls._string_or_none(getattr(conn, "host", None))
                ),
                "user": (
                    cls._string_or_none(extras.get("user"))
                    or cls._string_or_none(getattr(conn, "login", None))
                ),
                "password": (
                    cls._string_or_none(extras.get("password"))
                    or cls._string_or_none(getattr(conn, "password", None))
                ),
                "database": (
                    cls._string_or_none(extras.get("database"))
                    or cls._string_or_none(extras.get("extra__snowflake__database"))
                ),
                "schema": (
                    cls._string_or_none(extras.get("schema"))
                    or cls._string_or_none(getattr(conn, "schema", None))
                ),
                "warehouse": (
                    cls._string_or_none(extras.get("warehouse"))
                    or cls._string_or_none(extras.get("extra__snowflake__warehouse"))
                ),
                "stage": cls._string_or_none(extras.get("stage")),
                "table": cls._string_or_none(extras.get("table")),
                "stage_prefix": (
                    cls._string_or_none(extras.get("stage_prefix"))
                    or cls._string_or_none(extras.get("prefix"))
                ),
                "local_staging_dir": cls._string_or_none(extras.get("local_staging_dir")),
                "manifest_path": cls._string_or_none(extras.get("manifest_path")),
                "hard_delete": cls._bool_or_none(extras.get("hard_delete")),
            }
            return {k: v for k, v in candidates.items() if v is not None}

        if dest == "neo4j":
            resolved: dict[str, Any] = {}
            uri = cls._string_or_none(extras.get("uri"))
            if not uri:
                host = cls._string_or_none(getattr(conn, "host", None))
                port = cls._port_or_none(getattr(conn, "port", None))
                conn_type = cls._string_or_none(getattr(conn, "conn_type", None))
                scheme = cls._string_or_none(extras.get("scheme"))
                if not scheme:
                    if conn_type in {"bolt", "bolt+s", "bolt+ssc", "neo4j", "neo4j+s", "neo4j+ssc"}:
                        scheme = conn_type
                    else:
                        scheme = "bolt+s" if extras.get("encrypted") else "bolt"
                if host:
                    uri = f"{scheme}://{host}:{port}" if port else f"{scheme}://{host}"
            if uri:
                resolved["uri"] = uri

            raw_auth = extras.get("auth")
            if isinstance(raw_auth, (list, tuple)) and len(raw_auth) == 2:
                resolved["auth"] = (str(raw_auth[0]), str(raw_auth[1]))
            else:
                user = (
                    cls._string_or_none(extras.get("user"))
                    or cls._string_or_none(getattr(conn, "login", None))
                    or ""
                )
                password = (
                    cls._string_or_none(extras.get("password"))
                    or cls._string_or_none(getattr(conn, "password", None))
                    or ""
                )
                if user or password:
                    resolved["auth"] = (user, password)

            database = cls._string_or_none(extras.get("database")) or cls._string_or_none(
                getattr(conn, "schema", None)
            )
            if database:
                resolved["database"] = database
            return resolved

        if dest == "mongodb":
            resolved = {}
            uri = cls._string_or_none(extras.get("uri"))
            mongo_user = cls._string_or_none(extras.get("user")) or cls._string_or_none(
                getattr(conn, "login", None)
            )
            mongo_password = cls._string_or_none(extras.get("password")) or cls._string_or_none(
                getattr(conn, "password", None)
            )
            if not uri:
                host = cls._string_or_none(getattr(conn, "host", None))
                port = cls._port_or_none(getattr(conn, "port", None))
                conn_type = cls._string_or_none(getattr(conn, "conn_type", None))
                is_srv = bool(extras.get("srv")) or conn_type in {"mongo_srv", "mongodb+srv"}
                scheme = "mongodb+srv" if is_srv else "mongodb"
                if host:
                    userinfo = ""
                    if mongo_user:
                        userinfo = (
                            f"{quote_plus(mongo_user)}:{quote_plus(mongo_password)}@"
                            if mongo_password is not None
                            else f"{quote_plus(mongo_user)}@"
                        )
                    host_part = f"{host}:{port}" if (port and not is_srv) else host
                    auth_source = cls._string_or_none(extras.get("authSource"))
                    query = f"?authSource={quote_plus(auth_source)}" if auth_source else ""
                    uri = f"{scheme}://{userinfo}{host_part}{query}"
            if uri:
                resolved["uri"] = uri
            if mongo_user:
                resolved["user"] = mongo_user
            if mongo_password is not None:
                resolved["password"] = mongo_password
            database = cls._string_or_none(extras.get("database")) or cls._string_or_none(
                getattr(conn, "schema", None)
            )
            collection = cls._string_or_none(extras.get("collection"))
            if database:
                resolved["database"] = database
            if collection:
                resolved["collection"] = collection
            return resolved

        if dest in {"sql", "sql_by_form", "long_sql"}:
            resolved = {}
            conn_str = (
                cls._string_or_none(extras.get("conn_str"))
                or cls._string_or_none(extras.get("sqlalchemy_uri"))
                or cls._string_or_none(extras.get("uri"))
            )
            raw_conn_type = cls._string_or_none(getattr(conn, "conn_type", None))
            if not conn_str and (raw_conn_type or "").lower() == "sqlite":
                host = cls._string_or_none(getattr(conn, "host", None))
                schema = cls._string_or_none(getattr(conn, "schema", None))
                if host and schema:
                    sqlite_target = f"{host.rstrip('/')}/{schema.lstrip('/')}"
                else:
                    sqlite_target = host or schema or ":memory:"
                conn_str = (
                    sqlite_target
                    if sqlite_target.startswith("sqlite:")
                    else f"sqlite:///{sqlite_target}"
                )
            if not conn_str:
                get_uri = getattr(conn, "get_uri", None)
                if callable(get_uri):
                    try:
                        raw_uri = get_uri()
                    except Exception:
                        raw_uri = None
                    if isinstance(raw_uri, str) and raw_uri.strip():
                        conn_str = raw_uri.strip()
            if not conn_str:
                conn_type = raw_conn_type or "postgresql"
                if conn_type == "postgres":
                    conn_type = "postgresql"
                host = cls._string_or_none(getattr(conn, "host", None))
                schema = cls._string_or_none(getattr(conn, "schema", None))
                sql_user = cls._string_or_none(getattr(conn, "login", None))
                sql_password = cls._string_or_none(getattr(conn, "password", None))
                port = cls._port_or_none(getattr(conn, "port", None))
                if host or schema:
                    userinfo = ""
                    if sql_user:
                        userinfo = (
                            f"{quote_plus(sql_user)}:{quote_plus(sql_password)}@"
                            if sql_password is not None
                            else f"{quote_plus(sql_user)}@"
                        )
                    host_part = f"{host or 'localhost'}:{port}" if port else (host or "localhost")
                    db_part = f"/{schema}" if schema else ""
                    conn_str = f"{conn_type}://{userinfo}{host_part}{db_part}"
            if conn_str:
                if conn_str.startswith("postgres://"):
                    conn_str = "postgresql://" + conn_str[len("postgres://") :]
                resolved["conn_str"] = conn_str
            table = cls._string_or_none(extras.get("table"))
            if table:
                resolved["table"] = table
            return resolved

        if dest in {"duckdb", "duckdb_by_form"}:
            resolved = {}
            db_path = (
                cls._string_or_none(extras.get("db_path"))
                or cls._string_or_none(extras.get("path"))
                or cls._string_or_none(getattr(conn, "host", None))
                or cls._string_or_none(getattr(conn, "schema", None))
            )
            if db_path:
                resolved["db_path"] = db_path
            table_name = cls._string_or_none(extras.get("table_name")) or cls._string_or_none(
                extras.get("table")
            )
            if table_name:
                resolved["table_name"] = table_name
            return resolved

        if dest == "hive_parquet":
            resolved = {}
            base_dir = (
                cls._string_or_none(extras.get("base_dir"))
                or cls._string_or_none(extras.get("path"))
                or cls._string_or_none(getattr(conn, "host", None))
                or cls._string_or_none(getattr(conn, "schema", None))
            )
            if base_dir:
                resolved["base_dir"] = base_dir
            return resolved

        return {}

    @classmethod
    def _extra_field(cls, extras_dict: Mapping[str, object], field_name: str) -> object:
        """Read connection extra by unprefixed or Airflow UI-prefixed key."""
        direct = extras_dict.get(field_name)
        if direct is not None and (not isinstance(direct, str) or direct.strip()):
            return direct
        return extras_dict.get(f"extra__imednet__{field_name}")

    @classmethod
    def _resolve_connection_base_url(
        cls,
        conn: object,
        extras: Mapping[str, object],
    ) -> str | None:
        """Resolve an iMednet base URL from explicit extras first, then Connection host/scheme/port."""
        explicit_base_url = cls._string_or_none(cls._extra_field(extras, "base_url"))
        if explicit_base_url is not None:
            return sanitize_base_url(explicit_base_url)

        raw_host = cls._string_or_none(getattr(conn, "host", None))
        if raw_host is None:
            return None

        port = cls._port_or_none(getattr(conn, "port", None))
        raw_schema = cls._string_or_none(getattr(conn, "schema", None))
        schema_subpath = raw_schema.strip("/") if raw_schema else ""

        if raw_host.startswith(("http://", "https://")):
            parsed = urlsplit(raw_host)
            scheme = parsed.scheme
            netloc = parsed.netloc
            if port is not None and parsed.port is None:
                netloc = f"{netloc}:{port}"
            path = parsed.path
        else:
            extra_scheme = (cls._string_or_none(cls._extra_field(extras, "scheme")) or "").lower()
            conn_scheme = (cls._string_or_none(getattr(conn, "conn_type", None)) or "").lower()
            if extra_scheme in {"http", "https"}:
                scheme = extra_scheme
            elif conn_scheme == "http" and schema_subpath.lower() in {"http", "https"}:
                scheme = schema_subpath.lower()
                schema_subpath = ""
            elif conn_scheme in {"http", "https"}:
                scheme = conn_scheme
            else:
                scheme = "https"

            host_only, _, tail = raw_host.partition("/")
            path = f"/{tail}" if tail else ""
            netloc = (
                f"{host_only}:{port}" if port is not None and ":" not in host_only else host_only
            )

        if schema_subpath:
            clean_path = path.rstrip("/")
            target_suffix = f"/{schema_subpath}"
            if clean_path != target_suffix and not clean_path.endswith(target_suffix):
                path = f"{clean_path}{target_suffix}"

        return sanitize_base_url(f"{scheme}://{netloc}{path}")

    def _config_from_connection(self, conn: object) -> Config:
        """Build SDK ``Config`` from a resolved Airflow connection and env fallbacks."""
        extras_dict = self._connection_extras(conn)
        return load_config(
            api_key=self._string_or_none(self._extra_field(extras_dict, "api_key"))
            or self._string_or_none(getattr(conn, "login", None)),
            security_key=self._string_or_none(self._extra_field(extras_dict, "security_key"))
            or self._string_or_none(getattr(conn, "password", None)),
            base_url=self._resolve_connection_base_url(conn, extras_dict),
            oidc_token=self._string_or_none(self._extra_field(extras_dict, "oidc_token")),
            timeout=self._float_or_none(self._extra_field(extras_dict, "timeout")),
            strict_mode=self._bool_or_none(self._extra_field(extras_dict, "strict_mode")),
            simulation_mode=self._bool_or_none(self._extra_field(extras_dict, "simulation_mode")),
        )

    def _resolved_config(self) -> Config:
        """Resolve hook configuration from Airflow connection fields and env fallback."""
        conn = self._get_airflow_connection(self.imednet_conn_id)
        return self._config_from_connection(conn)

    def get_base_url(self) -> str:
        """Return redacted iMednet API base URL for lineage/inspection without requiring secrets."""
        import os

        from imednet.spi.sinks import redact_uri

        default_url = "https://edc.prod.imednetapi.com"
        try:
            conn = self._get_airflow_connection(self.imednet_conn_id)
            extras_dict = self._connection_extras(conn)
            raw_url = self._resolve_connection_base_url(conn, extras_dict)
        except AirflowSecretsBackendAccessDenied:
            raise
        except Exception:
            raw_url = None

        env_url = self._string_or_none(os.environ.get("IMEDNET_BASE_URL"))
        resolved = raw_url or (sanitize_base_url(env_url) if env_url else None) or default_url
        return redact_uri(redact_sensitive_text(resolved)).rstrip("/")

    @classmethod
    def _to_primitive(cls, value: Any) -> PrimitiveContainer:
        """Recursively normalize values to primitive containers with credential redaction.

        Pydantic-style objects are first converted via ``model_dump(mode="json", by_alias=True)``.
        Dictionaries are traversed recursively and sensitive keys are masked. Unknown
        object types fall back to ``str(value)`` so discovery outputs remain serializable.
        """
        if value is None or isinstance(value, (str, int, float, bool)):
            return cast(Primitive, value)
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if hasattr(value, "model_dump"):
            dumped = value.model_dump(mode="json", by_alias=True)
            value = cast(Any, dumped)
        if isinstance(value, Mapping):
            output: dict[str, PrimitiveContainer] = {}
            for key, item in value.items():
                key_str = str(key)
                if key_str.lower() in _SENSITIVE_KEYS:
                    output[key_str] = "***"
                else:
                    output[key_str] = cls._to_primitive(item)
            return output
        if isinstance(value, (list, tuple, set)):
            return [cls._to_primitive(item) for item in value]
        return str(value)

    def get_conn(self) -> ImednetSDK:
        """Return an authenticated ImednetSDK instance adhering to the Airflow BaseHook contract."""
        config = self._resolved_config()
        return ImednetSDK(config=config)

    async def get_async_conn(self) -> AsyncImednetSDK:
        """Return an authenticated :class:`AsyncImednetSDK` for async triggers and tasks."""
        conn = await self._aget_airflow_connection(self.imednet_conn_id)
        config = self._config_from_connection(conn)
        return AsyncImednetSDK(config=config)

    async def redact_error_message(self, raw_message: str) -> str:
        """Redact sensitive patterns, URI credentials, and resolved connection secrets."""
        import contextlib

        from imednet.spi.sinks import redact_uri

        message = str(redact_sensitive_payload(redact_uri(redact_sensitive_text(raw_message))))
        with contextlib.suppress(Exception):
            conn = await self._aget_airflow_connection(self.imednet_conn_id)
            config = self._config_from_connection(conn)
            for secret in (config.api_key, config.security_key, config.oidc_token):
                if isinstance(secret, str) and secret:
                    message = message.replace(secret, "***")
        return message

    def get_sdk_client(self) -> ImednetSDK:
        """Return an SDK client for use within task execution context.

        Direct alias for :meth:`get_conn`.
        """
        return self.get_conn()

    def test_connection(self) -> tuple[bool, str]:
        """Validate connectivity and credentials against the configured iMednet Study endpoint."""
        config: Config | None = None
        try:
            try:
                config = self._resolved_config()
            except AirflowSecretsBackendAccessDenied:
                raise
            except Exception:
                config = None
            with closing(self.get_sdk_client()) as sdk:
                next(iter(sdk.studies.list()), None)
            return True, "Connection successfully tested"
        except AirflowSecretsBackendAccessDenied:
            raise
        except Exception as exc:
            from imednet.spi.sinks import redact_uri

            message = str(redact_sensitive_payload(redact_uri(redact_sensitive_text(str(exc)))))
            if config is not None:
                for secret in (config.api_key, config.security_key, config.oidc_token):
                    if isinstance(secret, str) and secret:
                        message = message.replace(secret, "***")
            return False, message or exc.__class__.__name__

    def describe_connection(self) -> dict[str, PrimitiveContainer]:
        """Return redacted primitive metadata about resolved hook configuration."""
        config = self._resolved_config()
        return {
            "imednet_conn_id": self.imednet_conn_id,
            "base_url": self._to_primitive(config.base_url),
            "api_key": "***",
            "security_key": "***",
            "oidc_token": "***",
            "api_key_configured": bool(config.api_key),
            "security_key_configured": bool(config.security_key),
            "oidc_token_configured": bool(config.oidc_token),
        }

    def list_studies_metadata(self) -> list[dict[str, PrimitiveContainer]]:
        """Return primitive, serialization-safe study metadata for task mapping."""
        metadata: list[dict[str, PrimitiveContainer]] = []
        with closing(self.get_sdk_client()) as sdk:
            for study in sdk.studies.list():
                primitive_study = self._to_primitive(study)
                if isinstance(primitive_study, dict):
                    metadata.append(primitive_study)
        return metadata

    def list_study_keys(self) -> list[str]:
        """Return primitive study keys for mapped Airflow task expansion."""
        keys: list[str] = []
        for study in self.list_studies_metadata():
            study_key = study.get("studyKey") or study.get("study_key")
            if isinstance(study_key, str) and study_key:
                keys.append(study_key)
        return keys


if "airflow.providers.imednet.hooks" in sys.modules:
    sys.modules["airflow.providers.imednet.hooks"] = sys.modules[__name__]

__all__ = ["ImednetHook"]
