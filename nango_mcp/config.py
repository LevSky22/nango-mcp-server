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
class Settings:
    nango_url: str
    environments: tuple[EnvironmentConfig, ...]
    secret_resolver: str = "direct"
    metadata_namespace: str = DEFAULT_METADATA_NAMESPACE
    request_timeout: float = 20.0
    infisical: InfisicalSettings | None = None


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


def load_settings() -> Settings:
    env_file = os.getenv("NANGO_MCP_ENV_FILE", ".env")
    file_values = _read_env_file(env_file)
    secret_resolver = _value(file_values, "NANGO_MCP_SECRET_RESOLVER", default="direct").lower()
    if secret_resolver not in {"direct", "infisical"}:
        raise RuntimeError("NANGO_MCP_SECRET_RESOLVER must be 'direct' or 'infisical'")

    return Settings(
        nango_url=_value(file_values, "NANGO_BASE_URL", "NANGO_MCP_NANGO_URL", default=DEFAULT_NANGO_URL).rstrip("/"),
        environments=_load_environments(file_values, secret_resolver),
        secret_resolver=secret_resolver,
        metadata_namespace=_value(
            file_values,
            "NANGO_MCP_METADATA_NAMESPACE",
            default=DEFAULT_METADATA_NAMESPACE,
        ),
        request_timeout=_timeout(file_values),
        infisical=_load_infisical(file_values, secret_resolver),
    )
