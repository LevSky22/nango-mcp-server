from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx

from .config import EnvironmentConfig, InfisicalSettings, Settings
from .discovery import resolve_environment


@dataclass(frozen=True)
class ResolvedNangoSecret:
    environment: str
    nango_secret_key: str
    resolver: str
    secret_material_returned: bool = False


class SecretResolver(Protocol):
    async def resolve_nango_secret(self, environment: str, refresh: bool = False) -> ResolvedNangoSecret:
        ...


class DirectSecretResolver:
    def __init__(self, environments: tuple[EnvironmentConfig, ...]) -> None:
        self.environments = environments

    async def resolve_nango_secret(self, environment: str, refresh: bool = False) -> ResolvedNangoSecret:
        resolved = resolve_environment(environment, self.environments)
        if not resolved.secret_key:
            raise RuntimeError(f"NANGO_SECRET_KEY is not configured for environment {resolved.slug!r}")
        return ResolvedNangoSecret(
            environment=resolved.slug,
            nango_secret_key=resolved.secret_key,
            resolver="direct",
        )


class InfisicalSecretResolver:
    def __init__(
        self,
        environments: tuple[EnvironmentConfig, ...],
        settings: InfisicalSettings,
        *,
        timeout: float = 20.0,
    ) -> None:
        self.environments = environments
        self.settings = settings
        self.timeout = timeout
        self._cache: dict[str, ResolvedNangoSecret] = {}

    async def _request_json(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        token: str | None = None,
        json_body: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = await client.request(method, path, headers=headers, json=json_body, params=params)
        if response.status_code >= 400:
            raise RuntimeError(f"Infisical request failed: {method} {path} HTTP {response.status_code}")
        return response.json() if response.content else {}

    async def _login(self, client: httpx.AsyncClient) -> str:
        payload = await self._request_json(
            client,
            "POST",
            "/api/v1/auth/universal-auth/login",
            json_body={"clientId": self.settings.client_id, "clientSecret": self.settings.client_secret},
        )
        token = str(payload.get("accessToken") or "")
        if not token:
            raise RuntimeError("Infisical login returned no access token")
        return token

    async def resolve_nango_secret(self, environment: str, refresh: bool = False) -> ResolvedNangoSecret:
        resolved = resolve_environment(environment, self.environments)
        if not refresh and resolved.slug in self._cache:
            return self._cache[resolved.slug]

        secret_path = self.settings.secret_path_template.format(environment=resolved.slug)
        async with httpx.AsyncClient(base_url=self.settings.url, timeout=self.timeout) as client:
            token = await self._login(client)
            payload = await self._request_json(
                client,
                "GET",
                f"/api/v4/secrets/{self.settings.secret_name}",
                token=token,
                params={
                    "projectId": self.settings.project_id,
                    "environment": self.settings.environment,
                    "secretPath": secret_path,
                    "viewSecretValue": "true",
                    "includeImports": "false",
                },
            )

        secret_key = str((payload.get("secret") or {}).get("secretValue") or "")
        if not secret_key:
            raise RuntimeError(f"Infisical secret {self.settings.secret_name} is empty or missing")
        result = ResolvedNangoSecret(
            environment=resolved.slug,
            nango_secret_key=secret_key,
            resolver="infisical",
        )
        self._cache[resolved.slug] = result
        return result


def build_secret_resolver(settings: Settings) -> SecretResolver:
    if settings.secret_resolver == "infisical":
        if settings.infisical is None:
            raise RuntimeError("Infisical settings are required when NANGO_MCP_SECRET_RESOLVER=infisical")
        return InfisicalSecretResolver(settings.environments, settings.infisical, timeout=settings.request_timeout)
    return DirectSecretResolver(settings.environments)
