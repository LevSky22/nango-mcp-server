from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


DEFAULT_NANGO_URL = "https://api.nango.dev"
DEFAULT_ENVIRONMENT = "default"
DEFAULT_METADATA_NAMESPACE = "nango_mcp"
DEFAULT_INFISICAL_SECRET_PATH_TEMPLATE = "/nango/{environment}"
DEFAULT_INFISICAL_SECRET_NAME = "NANGO_SECRET_KEY"
DEFAULT_REQUEST_STATE_TTL_SECONDS = 15 * 60
MIN_REQUEST_STATE_TTL_SECONDS = 60
MAX_REQUEST_STATE_TTL_SECONDS = 60 * 60


@dataclass(frozen=True)
class EnvironmentConfig:
    slug: str
    secret_key: str | None = None
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class InfisicalSettings:
    url: str
    client_id: str
    client_secret: str
    project_id: str
    environment: str
    secret_path_template: str = DEFAULT_INFISICAL_SECRET_PATH_TEMPLATE
    secret_name: str = DEFAULT_INFISICAL_SECRET_NAME


@dataclass(frozen=True)
class OAuthSettings:
    issuer_url: str
    resource_url: str
    introspection_url: str
    client_id: str
    client_secret: str
    required_scopes: tuple[str, ...] = ("nango-mcp",)


@dataclass(frozen=True)
class Settings:
    nango_url: str
    environments: tuple[EnvironmentConfig, ...]
    public_nango_url: str | None = None
    secret_resolver: str = "direct"
    metadata_namespace: str = DEFAULT_METADATA_NAMESPACE
    request_timeout: float = 20.0
    read_only: bool = False
    require_confirmation: bool = False
    infisical: InfisicalSettings | None = None
    transport: str = "stdio"
    http_host: str = "127.0.0.1"
    http_port: int = 3000
    http_allowed_hosts: tuple[str, ...] = (
        "127.0.0.1:*",
        "localhost:*",
        "[::1]:*",
        "testserver",
    )
    auth_mode: str = "static"
    token_registry_raw: str = ""
    token_registry_file: str = ""
    denied_environments: frozenset[str] = frozenset()
    request_state_keys: tuple[str, ...] = ()
    request_state_ttl_seconds: int = DEFAULT_REQUEST_STATE_TTL_SECONDS
    oauth: OAuthSettings | None = None


def env_key_for_slug(slug: str) -> str:
    key = re.sub(r"[^A-Za-z0-9]+", "_", slug.strip()).strip("_").upper()
    return key or "DEFAULT"


def _read_env_file(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    env_path = Path(path)
    if not env_path.exists():
        return {}

    values: dict[str, str] = {}
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def _value(values: dict[str, str], *names: str, default: str = "") -> str:
    for name in names:
        raw = os.getenv(name)
        if raw:
            return raw.strip()
        raw = values.get(name)
        if raw:
            return raw.strip()
    return default


def _csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _timeout(values: dict[str, str]) -> float:
    raw = _value(values, "NANGO_MCP_REQUEST_TIMEOUT", default="20")
    try:
        return float(raw)
    except ValueError:
        return 20.0


def _positive_int(values: dict[str, str], name: str, default: int) -> int:
    raw = _value(values, name, default=str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return value


def _port(values: dict[str, str], name: str, default: int) -> int:
    value = _positive_int(values, name, default)
    if value > 65535:
        raise RuntimeError(f"{name} must be between 1 and 65535")
    return value


def _bool(values: dict[str, str], name: str, *, default: bool = False) -> bool:
    raw = _value(values, name)
    if not raw:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _load_environments(values: dict[str, str], secret_resolver: str) -> tuple[EnvironmentConfig, ...]:
    names = _csv(_value(values, "NANGO_MCP_ENVIRONMENTS"))
    if not names:
        slug = _value(values, "NANGO_ENVIRONMENT", default=DEFAULT_ENVIRONMENT)
        secret_key = _value(values, "NANGO_SECRET_KEY")
        if secret_resolver == "direct" and not secret_key:
            raise RuntimeError("NANGO_SECRET_KEY is required when NANGO_MCP_ENVIRONMENTS is not set")
        return (EnvironmentConfig(slug=slug, secret_key=secret_key or None),)

    environments: list[EnvironmentConfig] = []
    for slug in names:
        key = env_key_for_slug(slug)
        aliases = _csv(_value(values, f"NANGO_MCP_ENVIRONMENT_ALIASES_{key}"))
        secret_key = _value(values, f"NANGO_SECRET_KEY_{key}")
        if secret_resolver == "direct" and not secret_key:
            raise RuntimeError(f"NANGO_SECRET_KEY_{key} is required for environment {slug!r}")
        environments.append(EnvironmentConfig(slug=slug, secret_key=secret_key or None, aliases=aliases))
    return tuple(environments)


def _load_infisical(values: dict[str, str], secret_resolver: str) -> InfisicalSettings | None:
    if secret_resolver != "infisical":
        return None

    settings = InfisicalSettings(
        url=_value(values, "INFISICAL_URL", "INFISICAL_HOST_URL"),
        client_id=_value(values, "INFISICAL_UNIVERSAL_AUTH_CLIENT_ID"),
        client_secret=_value(values, "INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET"),
        project_id=_value(values, "NANGO_MCP_INFISICAL_PROJECT_ID"),
        environment=_value(values, "NANGO_MCP_INFISICAL_ENVIRONMENT", default="prod"),
        secret_path_template=_value(
            values,
            "NANGO_MCP_INFISICAL_SECRET_PATH_TEMPLATE",
            default=DEFAULT_INFISICAL_SECRET_PATH_TEMPLATE,
        ),
        secret_name=_value(values, "NANGO_MCP_INFISICAL_SECRET_NAME", default=DEFAULT_INFISICAL_SECRET_NAME),
    )
    missing = [
        name
        for name, value in {
            "INFISICAL_URL": settings.url,
            "INFISICAL_UNIVERSAL_AUTH_CLIENT_ID": settings.client_id,
            "INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET": settings.client_secret,
            "NANGO_MCP_INFISICAL_PROJECT_ID": settings.project_id,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing Infisical settings for Nango MCP: {', '.join(missing)}")
    return settings


def _load_oauth(values: dict[str, str], transport: str, auth_mode: str) -> OAuthSettings | None:
    if transport != "http" or auth_mode != "oauth":
        return None
    settings = OAuthSettings(
        issuer_url=_value(values, "NANGO_MCP_OAUTH_ISSUER_URL").rstrip("/"),
        resource_url=_value(values, "NANGO_MCP_OAUTH_RESOURCE_URL").rstrip("/"),
        introspection_url=_value(values, "NANGO_MCP_OAUTH_INTROSPECTION_URL"),
        client_id=_value(values, "NANGO_MCP_OAUTH_CLIENT_ID"),
        client_secret=_value(values, "NANGO_MCP_OAUTH_CLIENT_SECRET"),
        required_scopes=_csv(
            _value(values, "NANGO_MCP_OAUTH_REQUIRED_SCOPES", default="nango-mcp")
        ),
    )
    missing = [
        name
        for name, value in {
            "NANGO_MCP_OAUTH_ISSUER_URL": settings.issuer_url,
            "NANGO_MCP_OAUTH_RESOURCE_URL": settings.resource_url,
            "NANGO_MCP_OAUTH_INTROSPECTION_URL": settings.introspection_url,
            "NANGO_MCP_OAUTH_CLIENT_ID": settings.client_id,
            "NANGO_MCP_OAUTH_CLIENT_SECRET": settings.client_secret,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"Missing OAuth resource-server settings: {', '.join(missing)}")
    for name, value in (
        ("NANGO_MCP_OAUTH_ISSUER_URL", settings.issuer_url),
        ("NANGO_MCP_OAUTH_RESOURCE_URL", settings.resource_url),
        ("NANGO_MCP_OAUTH_INTROSPECTION_URL", settings.introspection_url),
    ):
        if not value.startswith("https://") and not value.startswith("http://127.0.0.1") and not value.startswith("http://localhost"):
            raise RuntimeError(f"{name} must use HTTPS except for loopback development")
    return settings


def load_settings() -> Settings:
    env_file = os.getenv("NANGO_MCP_ENV_FILE", ".env")
    file_values = _read_env_file(env_file)
    secret_resolver = _value(file_values, "NANGO_MCP_SECRET_RESOLVER", default="direct").lower()
    if secret_resolver not in {"direct", "infisical"}:
        raise RuntimeError("NANGO_MCP_SECRET_RESOLVER must be 'direct' or 'infisical'")

    transport = _value(file_values, "NANGO_MCP_TRANSPORT", default="stdio").lower()
    if transport not in {"stdio", "http"}:
        raise RuntimeError("NANGO_MCP_TRANSPORT must be 'stdio' or 'http'")
    auth_mode = _value(file_values, "NANGO_MCP_AUTH_MODE", default="static").lower()
    if auth_mode not in {"static", "oauth"}:
        raise RuntimeError("NANGO_MCP_AUTH_MODE must be 'static' or 'oauth'")
    request_state_keys = _csv(_value(file_values, "NANGO_MCP_REQUEST_STATE_KEYS"))
    request_state_ttl = _positive_int(
        file_values,
        "NANGO_MCP_REQUEST_STATE_TTL_SECONDS",
        DEFAULT_REQUEST_STATE_TTL_SECONDS,
    )
    if not MIN_REQUEST_STATE_TTL_SECONDS <= request_state_ttl <= MAX_REQUEST_STATE_TTL_SECONDS:
        raise RuntimeError(
            "NANGO_MCP_REQUEST_STATE_TTL_SECONDS must be between "
            f"{MIN_REQUEST_STATE_TTL_SECONDS} and {MAX_REQUEST_STATE_TTL_SECONDS}"
        )
    token_registry_raw = _value(file_values, "NANGO_MCP_TOKENS")
    token_registry_file = _value(file_values, "NANGO_MCP_TOKEN_REGISTRY_FILE")
    if transport == "http" and auth_mode == "static" and not (token_registry_raw or token_registry_file):
        raise RuntimeError(
            "NANGO_MCP_TOKENS or NANGO_MCP_TOKEN_REGISTRY_FILE is required for static HTTP auth"
        )

    return Settings(
        nango_url=_value(file_values, "NANGO_BASE_URL", "NANGO_MCP_NANGO_URL", default=DEFAULT_NANGO_URL).rstrip("/"),
        public_nango_url=(
            _value(file_values, "NANGO_MCP_PUBLIC_NANGO_URL") or None
        ),
        environments=_load_environments(file_values, secret_resolver),
        secret_resolver=secret_resolver,
        metadata_namespace=_value(
            file_values,
            "NANGO_MCP_METADATA_NAMESPACE",
            default=DEFAULT_METADATA_NAMESPACE,
        ),
        request_timeout=_timeout(file_values),
        read_only=_bool(file_values, "NANGO_MCP_READ_ONLY"),
        require_confirmation=_bool(file_values, "NANGO_MCP_REQUIRE_CONFIRMATION"),
        infisical=_load_infisical(file_values, secret_resolver),
        transport=transport,
        http_host=_value(file_values, "NANGO_MCP_HTTP_HOST", default="127.0.0.1"),
        http_port=_port(file_values, "NANGO_MCP_HTTP_PORT", 3000),
        http_allowed_hosts=_csv(
            _value(
                file_values,
                "NANGO_MCP_HTTP_ALLOWED_HOSTS",
                default="127.0.0.1:*,localhost:*,[::1]:*,testserver",
            )
        ),
        auth_mode=auth_mode,
        token_registry_raw=token_registry_raw,
        token_registry_file=token_registry_file,
        denied_environments=frozenset(
            item.lower() for item in _csv(_value(file_values, "NANGO_MCP_DENY_ENVIRONMENTS"))
        ),
        request_state_keys=request_state_keys,
        request_state_ttl_seconds=request_state_ttl,
        oauth=_load_oauth(file_values, transport, auth_mode),
    )
