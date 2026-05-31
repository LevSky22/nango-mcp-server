from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import Settings, load_settings
from .conventions import (
    SUGGESTED_OAUTH_APP_OWNERS,
    VALID_OWNER_KINDS,
    VALID_PURPOSES,
    connection_audit_findings,
    convention_metadata,
    convention_tags,
    imported_connection_id,
)
from .nango import NangoClient
from .secrets import ResolvedNangoSecret, SecretResolver, build_secret_resolver


logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

SECRET_KEY_FRAGMENTS = (
    "access_token",
    "refresh_token",
    "id_token",
    "client_secret",
    "secret",
    "password",
    "private_key",
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "token",
)

WRITE_CONFIRMATION = "I understand this changes the Nango environment"
DELETE_CONFIRMATION = "I understand this deletes Nango configuration"

OAUTH_AUTH_MODES = {"OAUTH1", "OAUTH2", "OAUTH2_CC"}
CREDENTIAL_AUTH_MODES = {"API_KEY", "BASIC", "JWT", "SIGNATURE", "APP", "APP_STORE", "CUSTOM", "TWO_STEP"}

FIELD_GUIDE = {
    "environment": "Configured Nango environment alias. Use 'default' for a single-key setup unless you renamed it.",
    "provider_config_key": "Nango integration key, also called integration ID or unique key in Nango docs.",
    "connection_id": "Nango connection ID for the already-authorized account/workspace/mailbox.",
    "tags": "Nango-recommended attribution and routing fields. Prefer end_user_id, end_user_email, organization_id, workspace_id, project_id, and environment.",
    "metadata": "Application/function configuration stored on the connection. Do not store credentials here.",
    "owner_kind": f"Optional MCP convention classification. Suggested values: {', '.join(sorted(VALID_OWNER_KINDS))}.",
    "purpose": f"Optional MCP convention classification. Suggested values: {', '.join(sorted(VALID_PURPOSES))}.",
    "oauth_app_owner": f"Optional metadata hint, not a Nango-native field. Suggested values: {', '.join(sorted(SUGGESTED_OAUTH_APP_OWNERS))}.",
}


mcp = FastMCP(
    name="Nango MCP Server",
    instructions=(
        "Operate one or more Nango environments through the Nango REST API and Proxy. "
        "The default resolver reads Nango secret keys from environment variables or a .env file; "
        "Infisical is optional."
    ),
)

_settings: Settings | None = None
_resolver: SecretResolver | None = None
_nango: NangoClient | None = None


def _runtime() -> tuple[Settings, SecretResolver, NangoClient]:
    global _settings, _resolver, _nango
    if _settings is None:
        _settings = load_settings()
    if _resolver is None:
        _resolver = build_secret_resolver(_settings)
    if _nango is None:
        _nango = NangoClient(_settings.nango_url, timeout=_settings.request_timeout)
    return _settings, _resolver, _nango


async def _resolve(environment: str, *, refresh: bool = False) -> tuple[Settings, NangoClient, ResolvedNangoSecret]:
    settings, resolver, nango = _runtime()
    secret = await resolver.resolve_nango_secret(environment, refresh=refresh)
    return settings, nango, secret


def _assert_confirmation(value: str, expected: str) -> None:
    settings, _, _ = _runtime()
    if settings.read_only:
        raise ValueError("This Nango MCP server is running in read-only mode")
    if settings.require_confirmation and value != expected:
        raise ValueError(f"confirmation must exactly equal: {expected}")


def sanitize_response(value: Any) -> Any:
    """Redact credential-like fields from management API responses."""
    if isinstance(value, list):
        return [sanitize_response(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_response(item) for item in value]
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if any(fragment in key_text for fragment in SECRET_KEY_FRAGMENTS):
                sanitized[str(key)] = "[redacted]"
            else:
                sanitized[str(key)] = sanitize_response(item)
        return sanitized
    return value


def json_response_text(value: Any) -> str:
    """Render arbitrary provider data as text for MCP clients that preview tool output."""
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def _collect_scope_context(value: Any, path: str = "", depth: int = 0) -> dict[str, Any]:
    if depth > 8:
        return {}
    found: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            key_lower = key_text.lower()
            item_path = f"{path}.{key_text}" if path else key_text
            if "scope" in key_lower or key_lower in {"permissions", "permission", "resources", "resource"}:
                found[item_path] = sanitize_response(item)
            found.update(_collect_scope_context(item, item_path, depth + 1))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.update(_collect_scope_context(item, f"{path}[{index}]", depth + 1))
    return found


def _first_present(source: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in source:
            return source[key]
    return None


def _as_data_list(payload: Any) -> list[dict[str, Any]]:
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    if isinstance(data, dict) and isinstance(data.get("connections"), list):
        data = data["connections"]
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _provider_summary(provider: dict[str, Any]) -> dict[str, Any]:
    auth_mode = str(provider.get("auth_mode") or provider.get("authMode") or "").upper()
    auth = provider.get("auth") if isinstance(provider.get("auth"), dict) else {}
    default_scopes = provider.get("default_scopes") or auth.get("default_scopes") or auth.get("scope")
    if auth_mode in OAUTH_AUTH_MODES:
        setup_note = "OAuth-style provider. Check provider docs and Nango fields before requesting app credentials."
    elif auth_mode in CREDENTIAL_AUTH_MODES:
        setup_note = "Credential-style provider. Do not ask for OAuth app credentials unless the selected provider docs require them."
    else:
        setup_note = "Check provider docs before requesting credentials."

    return {
        "name": provider.get("name"),
        "display_name": provider.get("display_name") or provider.get("displayName"),
        "auth_mode": auth_mode or None,
        "categories": provider.get("categories"),
        "connection_configuration": provider.get("connection_configuration"),
        "default_scopes": default_scopes,
        "proxy_base_url": (provider.get("proxy") or {}).get("base_url") if isinstance(provider.get("proxy"), dict) else None,
        "docs": provider.get("docs"),
        "docs_connect": provider.get("docs_connect"),
        "setup_note": setup_note,
    }


def _matches_provider(provider: dict[str, Any], query: str) -> bool:
    needle = query.strip().lower()
    if not needle:
        return True
    haystack = " ".join(
        str(value)
        for value in (
            provider.get("name"),
            provider.get("display_name"),
            provider.get("displayName"),
            provider.get("categories"),
            provider.get("docs"),
        )
        if value is not None
    ).lower()
    return needle in haystack


def _provider_sort_key(provider: dict[str, Any], query: str) -> tuple[int, str]:
    needle = query.strip().lower()
    name = str(provider.get("name") or "").lower()
    display = str(provider.get("display_name") or provider.get("displayName") or "").lower()
    rank = 2
    if needle and name == needle:
        rank = 0
    elif needle and (name.startswith(needle) or display.startswith(needle)):
        rank = 1
    return rank, name or display


@mcp.tool()
def describe_connection_convention() -> dict[str, Any]:
    """Explain the optional Nango MCP connection convention helpers."""
    settings, _, _ = _runtime()
    return {
        "principle": (
            "Use Nango tags for attribution, filtering, routing, and webhook reconciliation. "
            "Use Nango metadata for application/function configuration. Do not store credentials in either."
        ),
        "recommended_tags": ["end_user_id", "end_user_email", "organization_id"],
        "optional_routing_tags": ["workspace_id", "project_id", "environment"],
        "metadata_namespace": settings.metadata_namespace,
        "fields": FIELD_GUIDE,
        "avoid": [
            "Do not store raw OAuth tokens or provider secrets in metadata or tags.",
            "Do not put large synced datasets in metadata; use the appropriate data store instead.",
            "Remember that updating tags replaces the full tag object unless you fetch and merge first.",
        ],
    }


@mcp.tool()
def list_environments(refresh: bool = False) -> dict[str, Any]:
    """List configured Nango environments without returning secret material."""
    settings, _, _ = _runtime()
    return {
        "base_url": settings.nango_url,
        "secret_resolver": settings.secret_resolver,
        "environments": [
            {"environment": item.slug, "accepted_aliases": list(item.aliases)}
            for item in settings.environments
        ],
        "secret_material_returned": False,
        "refresh_requested": refresh,
    }


@mcp.tool()
async def check_environment(environment: str, refresh: bool = False) -> dict[str, Any]:
    """Resolve one configured Nango environment without returning its secret key."""
    _, _, secret = await _resolve(environment, refresh=refresh)
    return {
        "environment": secret.environment,
        "ready": True,
        "secret_resolver": secret.resolver,
        "secret_material_returned": False,
    }


@mcp.tool()
async def list_integrations(environment: str, refresh_secret: bool = False) -> Any:
    """List integrations configured in one Nango environment."""
    _, nango, secret = await _resolve(environment, refresh=refresh_secret)
    return sanitize_response(await nango.list_integrations(secret.nango_secret_key))


@mcp.tool()
async def get_integration(environment: str, integration_id: str, include_credentials: bool = False) -> Any:
    """Get one Nango integration. Credential-like response fields are redacted."""
    _, nango, secret = await _resolve(environment)
    return sanitize_response(
        await nango.get_integration(secret.nango_secret_key, integration_id, include_credentials=include_credentials)
    )


@mcp.tool()
async def search_provider_templates(
    environment: str,
    query: str,
    limit: int = 10,
    include_raw_templates: bool = False,
) -> dict[str, Any]:
    """Search Nango provider templates before creating an integration."""
    _, nango, secret = await _resolve(environment)
    providers_payload = await nango.list_providers(secret.nango_secret_key)
    integrations_payload = await nango.list_integrations(secret.nango_secret_key)

    providers = [provider for provider in _as_data_list(providers_payload) if _matches_provider(provider, query)]
    providers.sort(key=lambda provider: _provider_sort_key(provider, query))
    selected_providers = providers[: max(1, min(limit, 50))]
    provider_summaries = [_provider_summary(provider) for provider in selected_providers]

    provider_names = {summary["name"] for summary in provider_summaries if summary.get("name")}
    existing_integrations = []
    for integration in _as_data_list(integrations_payload):
        provider_name = integration.get("provider")
        unique_key = integration.get("unique_key") or integration.get("uniqueKey")
        if provider_name in provider_names or (query.strip().lower() and query.strip().lower() in str(unique_key).lower()):
            existing_integrations.append(
                {
                    "unique_key": unique_key,
                    "display_name": integration.get("display_name") or integration.get("displayName"),
                    "provider": provider_name,
                    "created_at": integration.get("created_at") or integration.get("createdAt"),
                    "updated_at": integration.get("updated_at") or integration.get("updatedAt"),
                }
            )

    return {
        "environment": secret.environment,
        "query": query,
        "template_explanation": {
            "provider_template": "A Nango built-in API definition from the provider catalog.",
            "integration": "A configured provider template. Its unique key is the provider_config_key.",
            "connection": "An authorized account/workspace/mailbox under one integration.",
        },
        "matches": provider_summaries,
        "raw_templates": sanitize_response(selected_providers) if include_raw_templates else None,
        "existing_integrations": existing_integrations,
        "secret_material_returned": False,
    }


@mcp.tool()
async def create_integration(environment: str, payload: dict[str, Any], confirmation: str = "") -> Any:
    """Create a Nango integration using the Nango API payload shape."""
    _assert_confirmation(confirmation, WRITE_CONFIRMATION)
    _, nango, secret = await _resolve(environment)
    return sanitize_response(await nango.create_integration(secret.nango_secret_key, payload))


@mcp.tool()
async def update_integration(environment: str, integration_id: str, fields: dict[str, Any], confirmation: str = "") -> Any:
    """Patch a Nango integration."""
    _assert_confirmation(confirmation, WRITE_CONFIRMATION)
    _, nango, secret = await _resolve(environment)
    return sanitize_response(await nango.update_integration(secret.nango_secret_key, integration_id, fields))


@mcp.tool()
async def delete_integration(environment: str, integration_id: str, confirmation: str = "") -> Any:
    """Delete a Nango integration."""
    _assert_confirmation(confirmation, DELETE_CONFIRMATION)
    _, nango, secret = await _resolve(environment)
    return sanitize_response(await nango.delete_integration(secret.nango_secret_key, integration_id))


@mcp.tool()
async def list_connections(
    environment: str,
    connection_id: str | None = None,
    search: str | None = None,
    tags: dict[str, str] | None = None,
    limit: int | None = None,
) -> Any:
    """List Nango connections. Prefer tag filters such as end_user_id and organization_id."""
    filters: dict[str, Any] = {}
    if connection_id:
        filters["connectionId"] = connection_id
    if search:
        filters["search"] = search
    for key, value in (tags or {}).items():
        filters[f"tags[{key}]"] = value
    if limit:
        filters["limit"] = limit

    _, nango, secret = await _resolve(environment)
    return sanitize_response(await nango.list_connections(secret.nango_secret_key, filters or None))


@mcp.tool()
async def get_connection(
    environment: str,
    connection_id: str,
    provider_config_key: str,
    include_credentials: bool = False,
) -> Any:
    """Get a Nango connection. Credential-like response fields are redacted."""
    _, nango, secret = await _resolve(environment)
    return sanitize_response(
        await nango.get_connection(
            secret.nango_secret_key,
            connection_id,
            provider_config_key,
            include_credentials=include_credentials,
        )
    )


@mcp.tool()
async def get_connection_context(
    environment: str,
    connection_id: str,
    provider_config_key: str,
    include_raw_provider_template: bool = False,
) -> dict[str, Any]:
    """Return a compact, redacted context view for one connection."""
    _, nango, secret = await _resolve(environment)
    connection = sanitize_response(
        await nango.get_connection(secret.nango_secret_key, connection_id, provider_config_key)
    )
    integration = sanitize_response(await nango.get_integration(secret.nango_secret_key, provider_config_key))
    providers_payload = await nango.list_providers(secret.nango_secret_key)

    connection_body = connection if isinstance(connection, dict) else {}
    integration_body = integration if isinstance(integration, dict) else {}
    provider_name = connection_body.get("provider") or integration_body.get("provider")
    provider_template = next(
        (provider for provider in _as_data_list(providers_payload) if provider.get("name") == provider_name),
        None,
    )
    end_user = _first_present(connection_body, ("end_user", "endUser"))
    organization = _first_present(connection_body, ("organization", "end_user_organization", "endUserOrganization"))

    return {
        "environment": secret.environment,
        "connection_id": connection_body.get("connection_id") or connection_body.get("connectionId") or connection_id,
        "provider_config_key": connection_body.get("provider_config_key")
        or connection_body.get("providerConfigKey")
        or provider_config_key,
        "provider": provider_name,
        "provider_template": _provider_summary(provider_template) if provider_template else None,
        "raw_provider_template": sanitize_response(provider_template) if include_raw_provider_template else None,
        "end_user": end_user,
        "organization": organization,
        "tags": connection_body.get("tags") or {},
        "metadata": connection_body.get("metadata") or {},
        "visible_scope_fields": {
            "connection": _collect_scope_context(connection_body),
            "integration": _collect_scope_context(integration_body),
        },
        "secret_material_returned": False,
    }


@mcp.tool()
async def import_connection(environment: str, payload: dict[str, Any], confirmation: str = "") -> Any:
    """Import/create a connection using the Nango API payload shape."""
    _assert_confirmation(confirmation, WRITE_CONFIRMATION)
    _, nango, secret = await _resolve(environment)
    return sanitize_response(await nango.import_connection(secret.nango_secret_key, payload))


@mcp.tool()
async def delete_connection(
    environment: str,
    connection_id: str,
    provider_config_key: str,
    confirmation: str = "",
) -> Any:
    """Delete one Nango connection."""
    _assert_confirmation(confirmation, DELETE_CONFIRMATION)
    _, nango, secret = await _resolve(environment)
    return sanitize_response(await nango.delete_connection(secret.nango_secret_key, connection_id, provider_config_key))


@mcp.tool()
async def patch_connection_tags(
    environment: str,
    connection_id: str,
    provider_config_key: str,
    tags: dict[str, str],
    confirmation: str = "",
) -> Any:
    """Replace a connection's complete tag set. Fetch and merge first when changing one tag."""
    _assert_confirmation(confirmation, WRITE_CONFIRMATION)
    _, nango, secret = await _resolve(environment)
    return sanitize_response(
        await nango.patch_connection_tags(secret.nango_secret_key, connection_id, provider_config_key, tags)
    )


@mcp.tool()
async def set_connection_metadata(
    environment: str,
    connection_id: str,
    provider_config_key: str,
    metadata: dict[str, Any],
    patch: bool = False,
    confirmation: str = "",
) -> Any:
    """Set or patch connection metadata. Do not put credentials or required connection config here."""
    _assert_confirmation(confirmation, WRITE_CONFIRMATION)
    _, nango, secret = await _resolve(environment)
    return sanitize_response(
        await nango.set_connection_metadata(
            secret.nango_secret_key,
            connection_id,
            provider_config_key,
            metadata,
            patch=patch,
        )
    )


@mcp.tool()
async def create_connect_session(
    environment: str,
    allowed_integrations: list[str],
    tags: dict[str, str] | None = None,
    integrations_config_defaults: dict[str, Any] | None = None,
    confirmation: str = "",
) -> Any:
    """Create a Nango Connect session token."""
    _assert_confirmation(confirmation, WRITE_CONFIRMATION)
    payload: dict[str, Any] = {"allowed_integrations": allowed_integrations}
    if tags:
        payload["tags"] = tags
    if integrations_config_defaults:
        payload["integrations_config_defaults"] = integrations_config_defaults

    _, nango, secret = await _resolve(environment)
    return sanitize_response(await nango.create_connect_session(secret.nango_secret_key, payload))


@mcp.tool()
async def create_standard_connect_session(
    environment: str,
    provider_config_key: str,
    principal: str,
    owner_kind: str,
    purpose: str,
    organization_id: str | None = None,
    display_name: str | None = None,
    email: str | None = None,
    integrations_config_defaults: dict[str, Any] | None = None,
    confirmation: str = "",
) -> Any:
    """Create a Connect session with recommended Nango tags plus optional MCP convention tags."""
    _assert_confirmation(confirmation, WRITE_CONFIRMATION)
    tags = convention_tags(
        environment,
        principal,
        owner_kind,
        purpose,
        email=email,
        organization_id=organization_id,
        display_name=display_name,
    )
    payload: dict[str, Any] = {"allowed_integrations": [provider_config_key], "tags": tags}
    if integrations_config_defaults:
        payload["integrations_config_defaults"] = integrations_config_defaults

    _, nango, secret = await _resolve(environment)
    response = sanitize_response(await nango.create_connect_session(secret.nango_secret_key, payload))
    return {"tags": tags, "response": response}


@mcp.tool()
async def create_reconnect_session(
    environment: str,
    connection_id: str,
    provider_config_key: str,
    confirmation: str = "",
) -> Any:
    """Create a Nango reconnect session for an existing connection."""
    _assert_confirmation(confirmation, WRITE_CONFIRMATION)
    _, nango, secret = await _resolve(environment)
    return sanitize_response(
        await nango.create_reconnect_session(
            secret.nango_secret_key,
            connection_id=connection_id,
            integration_id=provider_config_key,
        )
    )


@mcp.tool(structured_output=False)
async def proxy_request(
    environment: str,
    provider_config_key: str,
    connection_id: str,
    method: str,
    path: str,
    query: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    body: Any | None = None,
) -> str:
    """Call a provider API through the Nango Proxy without exposing provider tokens."""
    _, nango, secret = await _resolve(environment)
    response = await nango.proxy_request(
        secret.nango_secret_key,
        provider_config_key,
        connection_id,
        method,
        path,
        query=query,
        headers=headers,
        body=body,
    )
    return json_response_text(response)


@mcp.tool()
def build_connection_convention(
    environment: str,
    provider_config_key: str,
    principal: str,
    owner_kind: str,
    purpose: str,
    oauth_app_owner: str | None = None,
) -> dict[str, Any]:
    """Build a suggested connection_id, tags, and metadata object for a managed connection."""
    settings, _, _ = _runtime()
    return {
        "connection_id": imported_connection_id(environment, provider_config_key, principal),
        "tags": convention_tags(environment, principal, owner_kind, purpose),
        "metadata": convention_metadata(
            environment,
            principal,
            owner_kind,
            purpose,
            namespace=settings.metadata_namespace,
            oauth_app_owner=oauth_app_owner,
        ),
    }


@mcp.tool()
async def apply_connection_convention(
    environment: str,
    connection_id: str,
    provider_config_key: str,
    principal: str,
    owner_kind: str,
    purpose: str,
    oauth_app_owner: str | None = None,
    patch_metadata: bool = True,
    confirmation: str = "",
) -> dict[str, Any]:
    """Apply suggested Nango MCP tags and metadata to an existing connection."""
    _assert_confirmation(confirmation, WRITE_CONFIRMATION)
    settings, nango, secret = await _resolve(environment)
    tags = convention_tags(secret.environment, principal, owner_kind, purpose)
    metadata = convention_metadata(
        secret.environment,
        principal,
        owner_kind,
        purpose,
        namespace=settings.metadata_namespace,
        oauth_app_owner=oauth_app_owner,
    )
    existing = await nango.get_connection(secret.nango_secret_key, connection_id, provider_config_key)
    existing_tags = existing.get("tags") if isinstance(existing, dict) else None
    merged_tags = {**(existing_tags or {}), **tags}
    tag_response = await nango.patch_connection_tags(
        secret.nango_secret_key,
        connection_id,
        provider_config_key,
        merged_tags,
    )
    metadata_response = await nango.set_connection_metadata(
        secret.nango_secret_key,
        connection_id,
        provider_config_key,
        metadata,
        patch=patch_metadata,
    )
    return {
        "tags": merged_tags,
        "metadata": metadata,
        "tag_response": sanitize_response(tag_response),
        "metadata_response": sanitize_response(metadata_response),
    }


@mcp.tool()
async def audit_connection_conventions(environment: str, limit: int = 100) -> dict[str, Any]:
    """Audit Nango connections for suggested MCP tag/metadata conventions."""
    settings, nango, secret = await _resolve(environment)
    payload = await nango.list_connections(secret.nango_secret_key, {"limit": limit})
    connections = payload.get("connections", []) if isinstance(payload, dict) else []
    findings = []
    for connection in connections:
        if not isinstance(connection, dict):
            continue
        audit = connection_audit_findings(
            connection,
            secret.environment,
            metadata_namespace=settings.metadata_namespace,
        )
        required_issues = audit["required_issues"]
        recommendations = audit["recommendations"]
        if required_issues or recommendations:
            findings.append(
                {
                    "connection_id": connection.get("connection_id") or connection.get("id"),
                    "provider_config_key": connection.get("provider_config_key"),
                    "required_issues": required_issues,
                    "recommendations": recommendations,
                }
            )
    return {
        "environment": secret.environment,
        "checked": len(connections),
        "required_issue_count": sum(len(item["required_issues"]) for item in findings),
        "recommendation_count": sum(len(item["recommendations"]) for item in findings),
        "healthy": not any(item["required_issues"] for item in findings),
        "findings": findings,
    }


def main() -> None:
    mcp.run()
