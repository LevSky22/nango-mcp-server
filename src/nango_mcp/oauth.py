from __future__ import annotations

import time
from typing import Any

import httpx
from mcp.server.auth.provider import AccessToken, TokenVerifier

from .auth import CallerScope
from .config import OAuthSettings


class OAuthIntrospectionVerifier(TokenVerifier):
    """Validate opaque access tokens with an RFC 7662 introspection endpoint."""

    def __init__(self, settings: OAuthSettings, *, timeout: float = 10.0) -> None:
        self.settings = settings
        self.timeout = timeout

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    self.settings.introspection_url,
                    data={"token": token, "token_type_hint": "access_token"},
                    auth=(self.settings.client_id, self.settings.client_secret),
                    headers={"Accept": "application/json"},
                )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        if not isinstance(payload, dict) or payload.get("active") is not True:
            return None

        expires_at = _integer_claim(payload.get("exp"))
        if expires_at is not None and expires_at <= int(time.time()):
            return None
        scopes = _scope_list(payload.get("scope"))
        if not set(self.settings.required_scopes).issubset(scopes):
            return None
        if not _resource_matches(payload, self.settings.resource_url):
            return None

        client_id = str(payload.get("client_id") or payload.get("azp") or "unknown-client")
        subject = payload.get("sub")
        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=sorted(scopes),
            expires_at=expires_at,
            resource=self.settings.resource_url,
            subject=str(subject) if subject is not None else None,
            claims=payload,
        )


def caller_scope_from_access_token(access_token: AccessToken) -> CallerScope:
    scopes = frozenset(access_token.scopes)
    environments = frozenset(
        scope.removeprefix("nango:env:").lower()
        for scope in scopes
        if scope.startswith("nango:env:") and scope.removeprefix("nango:env:")
    )
    if not environments:
        raise PermissionError("access token does not grant a Nango environment")
    can_read = "nango:read" in scopes
    can_write = "nango:write" in scopes
    can_proxy = "nango:proxy" in scopes
    denied_tools: set[str] = set()
    if not can_read:
        denied_tools.update({
            "describe_connection_convention", "list_environments", "check_environment",
            "list_integrations", "get_integration", "search_provider_templates",
            "list_connections", "get_connection", "get_connection_context",
            "build_connection_convention", "audit_connection_conventions",
            "query_response_artifact",
        })
    if not can_write:
        denied_tools.update({
            "create_integration", "update_integration", "delete_integration",
            "refresh_connection_credentials", "import_connection", "delete_connection",
            "replace_connection_tags", "update_connection_metadata", "update_connection_end_user",
            "create_connect_session",
            "create_standard_connect_session", "create_reconnect_session",
            "apply_connection_convention", "stage_proxy_request_body",
        })
    if not can_proxy:
        denied_tools.update({"proxy_request", "download_provider_file", "stage_proxy_request_body"})
    if not (can_proxy and can_read):
        denied_tools.add("download_provider_file")
    if can_proxy and can_read and can_write:
        allowed_proxy_methods = frozenset({"*"})
    else:
        allowed_methods: set[str] = set()
        if can_proxy and can_read:
            allowed_methods.update({"GET", "HEAD", "OPTIONS"})
        if can_proxy and can_write:
            allowed_methods.update({"POST", "PUT", "PATCH", "DELETE"})
        allowed_proxy_methods = frozenset(allowed_methods)
    return CallerScope(
        label=access_token.subject or access_token.client_id,
        environments=environments,
        denied_tools=frozenset(denied_tools),
        allowed_proxy_methods=allowed_proxy_methods,
    )


def _scope_list(value: Any) -> set[str]:
    if isinstance(value, str):
        return {item for item in value.split() if item}
    if isinstance(value, list):
        return {str(item) for item in value if str(item)}
    return set()


def _integer_claim(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _resource_matches(payload: dict[str, Any], expected: str) -> bool:
    candidates: set[str] = set()
    audience = payload.get("aud")
    if isinstance(audience, str):
        candidates.add(audience)
    elif isinstance(audience, list):
        candidates.update(str(item) for item in audience)
    resource = payload.get("resource")
    if isinstance(resource, str):
        candidates.add(resource)
    elif isinstance(resource, list):
        candidates.update(str(item) for item in resource)
    return expected in candidates
