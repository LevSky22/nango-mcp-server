from __future__ import annotations

import json
import logging
import os
import re
import secrets
import hashlib
import tempfile
import time
from pathlib import Path
from typing import Any, Literal

import uvicorn
from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings
from mcp.server.context import ServerRequestContext
from mcp.server.mcpserver import Context
from mcp.server.request_state import RequestStateSecurity
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import (
    CallToolResult,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    InputRequiredResult,
    ResourceLink,
    TextContent,
)
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from pydantic import BaseModel, Field
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .auth import (
    CallerScope,
    TokenRegistry,
    TokenRegistrySource,
    authenticate,
    authorize_operation,
    permitted_environments,
    proxy_route_matches,
    require_scope,
    reset_scope,
    set_scope,
)
from .binary_resources import BinaryResourceStore
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
from .oauth import OAuthIntrospectionVerifier, caller_scope_from_access_token
from .ratelimit import EnvironmentConcurrencyGate, current_environment
from .response_safety import ArtifactStore, bound_proxy_response
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

OAUTH_AUTH_MODES = {"OAUTH1", "OAUTH2", "OAUTH2_CC"}
CREDENTIAL_AUTH_MODES = {"API_KEY", "BASIC", "JWT", "SIGNATURE", "APP", "APP_STORE", "CUSTOM", "TWO_STEP"}
TOP_LEVEL_SCOPE_FIELDS = ("scopes", "oauth_scopes", "default_scopes")
REQUIRED_SCOPE_CREDENTIAL_FIELDS = ("client_id", "client_secret")

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


_state_keys = [
    key.strip()
    for key in os.getenv("NANGO_MCP_REQUEST_STATE_KEYS", "").split(",")
    if key.strip()
] or [secrets.token_urlsafe(32)]


def _request_state_principal(_: ServerRequestContext[Any, Any]) -> str:
    caller = require_scope()
    policy = {
        "label": caller.label,
        "environments": sorted(caller.environments),
        "mutationApproval": caller.mutation_approval,
    }
    digest = hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return f"{caller.label}:{digest}"


mcp = MCPServer(
    name="Nango MCP Server",
    version="1.0.0",
    instructions=(
        "Operate one or more Nango environments through the Nango REST API and Proxy. "
        "The default resolver reads Nango secret keys from environment variables or a .env file; "
        "Infisical is optional."
    ),
    request_state_security=RequestStateSecurity(
        keys=_state_keys,
        ttl=15 * 60,
        bind_principal=_request_state_principal,
    ),
)

_settings: Settings | None = None
_resolver: SecretResolver | None = None
_nango: NangoClient | None = None
_environment_gate: EnvironmentConcurrencyGate | None = None


def _runtime() -> tuple[Settings, SecretResolver, NangoClient]:
    global _settings, _resolver, _nango, _environment_gate
    if _settings is None:
        _settings = load_settings()
    if _resolver is None:
        _resolver = build_secret_resolver(_settings)
    if _nango is None:
        _nango = NangoClient(
            _settings.nango_url,
            timeout=_settings.request_timeout,
            public_base_url=_settings.public_nango_url,
            rate_limit_max_attempts=_settings.rate_limit_max_attempts,
            rate_limit_max_wait=_settings.rate_limit_max_wait_seconds,
            rate_limit_backoff_base=_settings.rate_limit_backoff_base_seconds,
            rate_limit_ceiling=_settings.rate_limit_retry_ceiling_seconds,
            max_connections=_settings.http_max_connections,
            max_keepalive=_settings.http_max_keepalive,
        )
    if _environment_gate is None:
        _environment_gate = EnvironmentConcurrencyGate(
            _settings.environment_max_concurrency,
            _settings.environment_acquire_timeout_seconds,
        )
    return _settings, _resolver, _nango


async def _resolve(environment: str, *, refresh: bool = False) -> tuple[Settings, NangoClient, ResolvedNangoSecret]:
    settings, resolver, nango = _runtime()
    scope = require_scope()
    configured = frozenset(item.slug for item in settings.environments)
    if environment not in permitted_environments(configured, scope):
        raise PermissionError("environment is outside the caller scope")
    secret = await resolver.resolve_nango_secret(environment, refresh=refresh)
    return settings, nango, secret


def _assert_writable() -> None:
    settings, _, _ = _runtime()
    if settings.read_only:
        raise ValueError("This Nango MCP server is running in read-only mode")


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


def _as_data_dict(payload: Any) -> dict[str, Any]:
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    return data if isinstance(data, dict) else {}


def _field_is_blank(value: Any) -> bool:
    return value is None or value == ""


def _integration_update_contains_scope_change(fields: dict[str, Any]) -> bool:
    if any(key in fields for key in TOP_LEVEL_SCOPE_FIELDS):
        return True
    credentials = fields.get("credentials")
    return isinstance(credentials, dict) and "scopes" in credentials


def _normalized_scope_value(value: Any) -> Any:
    """Return the comma-delimited scope representation required by Nango."""
    if isinstance(value, list):
        candidates = [str(item).strip() for item in value]
    elif isinstance(value, str):
        candidates = re.split(r"[\s,]+", value.strip())
    else:
        return value
    return ",".join(dict.fromkeys(item for item in candidates if item))


def _prepare_integration_update_fields(
    fields: dict[str, Any],
    current_integration: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Normalize Nango integration PATCH fields without exposing credential values."""
    normalized = dict(fields)
    notes: list[str] = []

    credentials_value = normalized.get("credentials")
    if credentials_value is not None and not isinstance(credentials_value, dict):
        raise ValueError("credentials must be an object when patching a Nango integration")
    credentials = dict(credentials_value or {})

    top_level_scope_values: list[tuple[str, Any]] = []
    for key in TOP_LEVEL_SCOPE_FIELDS:
        if key in normalized:
            top_level_scope_values.append((key, normalized.pop(key)))

    if top_level_scope_values:
        selected_name, selected_value = top_level_scope_values[0]
        selected_normalized = _normalized_scope_value(selected_value)
        for name, value in top_level_scope_values[1:]:
            if _normalized_scope_value(value) != selected_normalized:
                raise ValueError(
                    "conflicting top-level scope fields supplied: "
                    f"{selected_name} and {name}; pass one value under credentials.scopes"
                )
        if "scopes" in credentials and _normalized_scope_value(credentials["scopes"]) != selected_normalized:
            raise ValueError("conflicting scope values supplied at top level and credentials.scopes")
        credentials["scopes"] = selected_normalized
        notes.append(
            "Moved top-level scope field(s) into credentials.scopes; Nango rejects top-level "
            "scopes/oauth_scopes/default_scopes on integration PATCH."
        )

    if "scopes" in credentials:
        credentials["scopes"] = _normalized_scope_value(credentials["scopes"])
        current = _as_data_dict(current_integration or {})
        current_credentials = current.get("credentials")
        if isinstance(current_credentials, dict):
            merged_credentials = {
                key: value
                for key, value in current_credentials.items()
                if not _field_is_blank(value)
            }
            merged_credentials.update({key: value for key, value in credentials.items() if not _field_is_blank(value)})
            credentials = merged_credentials

        missing = [key for key in REQUIRED_SCOPE_CREDENTIAL_FIELDS if _field_is_blank(credentials.get(key))]
        if missing:
            missing_fields = ", ".join(f"credentials.{key}" for key in missing)
            raise ValueError(
                "scope updates must patch the complete Nango credentials object; "
                f"missing {missing_fields}. Provide those credential fields or use an integration response "
                "that includes credentials so the MCP can preserve them internally."
            )

        normalized["credentials"] = credentials
        notes.append(
            "OAuth scope updates affect the integration config only. Affected connections must reconnect/"
            "reauthorize before their tokens carry the new scopes."
        )
    elif credentials_value is not None:
        normalized["credentials"] = credentials

    return normalized, notes


def _connection_matches_integration(connection: dict[str, Any], integration_id: str) -> bool:
    provider_config_key = (
        connection.get("provider_config_key")
        or connection.get("providerConfigKey")
        or connection.get("integration_id")
        or connection.get("integrationId")
    )
    return provider_config_key == integration_id


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


MUTATION_EFFECTS = {
    "create_integration": "elevated",
    "update_integration": "elevated",
    "delete_integration": "destructive",
    "refresh_connection_credentials": "elevated",
    "import_connection": "elevated",
    "delete_connection": "destructive",
    "patch_connection_tags": "low",
    "set_connection_metadata": "low",
    "create_connect_session": "elevated",
    "create_standard_connect_session": "elevated",
    "create_reconnect_session": "elevated",
    "apply_connection_convention": "low",
}


def _mutation_effect(tool: str, args: tuple[Any, ...]) -> str:
    if tool == "proxy_request":
        return "destructive" if len(args) > 2 and str(args[2]).upper() == "DELETE" else "elevated"
    return MUTATION_EFFECTS[tool]


class MutationApprovalError(PermissionError):
    pass


class LegacyMutationApproval(BaseModel):
    approve: bool = Field(title="Approve this mutation")


def _approval_hash(tool: str, environment: str, args: tuple[Any, ...]) -> str:
    encoded = json.dumps(
        {"tool": tool, "environment": environment, "args": args},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _approval_message(tool: str, environment: str, args: tuple[Any, ...]) -> str:
    target = ""
    if tool in {"update_integration", "delete_integration"} and args:
        target = f" integration={args[0]}"
    elif tool in {
        "refresh_connection_credentials", "delete_connection", "patch_connection_tags",
        "set_connection_metadata", "create_reconnect_session", "apply_connection_convention",
    } and len(args) >= 2:
        target = f" connection={args[0]} integration={args[1]}"
    elif tool == "proxy_request" and len(args) >= 4:
        target = f" method={str(args[2]).upper()} path={args[3]} integration={args[0]} connection={args[1]}"
    return (
        f"Approve Nango mutation: effect={_mutation_effect(tool, args)} "
        f"environment={environment} tool={tool}{target}"
    )


async def _target_snapshot(tool: str, environment: str, args: tuple[Any, ...]) -> str | None:
    target: Any = None
    if tool in {"update_integration", "delete_integration"}:
        _, nango, secret = await _resolve(environment)
        target = await nango.get_integration(secret.nango_secret_key, str(args[0]))
    elif tool in {
        "refresh_connection_credentials", "delete_connection", "patch_connection_tags",
        "set_connection_metadata", "create_reconnect_session", "apply_connection_convention",
    }:
        _, nango, secret = await _resolve(environment)
        target = await nango.get_connection(secret.nango_secret_key, str(args[0]), str(args[1]))
    if target is None:
        return None
    encoded = json.dumps(sanitize_response(target), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _input_required(
    tool: str,
    environment: str,
    args: tuple[Any, ...],
    snapshot: str | None,
) -> InputRequiredResult:
    state = json.dumps(
        {
            "v": 1,
            "tool": tool,
            "environment": environment,
            "effect": _mutation_effect(tool, args),
            "snapshot": snapshot,
            "requestHash": _approval_hash(tool, environment, args),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return InputRequiredResult(
        input_requests={
            "approve": ElicitRequest(
                params=ElicitRequestFormParams(
                    message=_approval_message(tool, environment, args),
                    requestedSchema={
                        "type": "object",
                        "properties": {"approve": {"type": "boolean", "title": "Approve this mutation"}},
                        "required": ["approve"],
                        "additionalProperties": False,
                    },
                )
            )
        },
        request_state=state,
    )


async def _authorize_mutation(
    ctx: Context | None,
    tool: str,
    environment: str,
    args: tuple[Any, ...],
) -> InputRequiredResult | None:
    if ctx is None:
        return None
    _assert_writable()
    caller = require_scope()
    if caller.mutation_approval == "host" and _mutation_effect(tool, args) != "destructive":
        if tool != "proxy_request" or not proxy_route_matches(
            caller.server_approval_proxy_path_patterns,
            provider_config_key=str(args[0]),
            method=str(args[2]),
            path=str(args[3]),
        ):
            return None
    snapshot = await _target_snapshot(tool, environment, args)
    if ctx.protocol_version not in MODERN_PROTOCOL_VERSIONS:
        session = ctx.request_context.session
        capabilities = session.client_capabilities
        if not session.can_send_request or capabilities is None or capabilities.elicitation is None:
            raise MutationApprovalError("MCP client does not support a secure mutation approval flow")
        response = await ctx.elicit(_approval_message(tool, environment, args), LegacyMutationApproval)
        if response.action != "accept" or not response.data.approve:
            raise MutationApprovalError("Nango mutation was not approved")
        if await _target_snapshot(tool, environment, args) != snapshot:
            raise MutationApprovalError("Nango mutation target changed during approval")
        return None
    if not ctx.request_state or not ctx.input_responses:
        return _input_required(tool, environment, args, snapshot)
    try:
        state = json.loads(ctx.request_state)
    except (TypeError, json.JSONDecodeError) as exc:
        raise MutationApprovalError("invalid approval state") from exc
    expected = {
        "v": 1,
        "tool": tool,
        "environment": environment,
        "effect": _mutation_effect(tool, args),
        "requestHash": _approval_hash(tool, environment, args),
    }
    if any(state.get(key) != value for key, value in expected.items()):
        raise MutationApprovalError("approval state does not match this operation")
    if state.get("snapshot") != snapshot:
        return _input_required(tool, environment, args, snapshot)
    response = ctx.input_responses.get("approve")
    if not isinstance(response, ElicitResult) or response.action != "accept" or response.content != {"approve": True}:
        raise MutationApprovalError("Nango mutation was not approved")
    return None


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
            if item.slug in permitted_environments(
                frozenset(environment.slug for environment in settings.environments),
                require_scope(),
            )
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
async def create_integration(ctx: Context, environment: str, payload: dict[str, Any]) -> Any:
    """Create a Nango integration using the Nango API payload shape."""
    approval = await _authorize_mutation(ctx, "create_integration", environment, (payload,))
    if approval:
        return approval
    _, nango, secret = await _resolve(environment)
    return sanitize_response(await nango.create_integration(secret.nango_secret_key, payload))


@mcp.tool()
async def update_integration(
    ctx: Context,
    environment: str,
    integration_id: str,
    fields: dict[str, Any],
    reconnect_connection_ids: list[str] | None = None,
    auto_reconnect_single_matching_connection: bool = True,
) -> Any:
    """Patch a Nango integration."""
    approval = await _authorize_mutation(ctx, "update_integration", environment, (integration_id, fields))
    if approval:
        return approval
    _, nango, secret = await _resolve(environment)
    scope_change = _integration_update_contains_scope_change(fields)
    current_integration = None
    if scope_change:
        current_integration = await nango.get_integration(
            secret.nango_secret_key,
            integration_id,
            include_credentials=True,
        )
    prepared_fields, operator_notes = _prepare_integration_update_fields(fields, current_integration)
    response = sanitize_response(await nango.update_integration(secret.nango_secret_key, integration_id, prepared_fields))
    if scope_change and not reconnect_connection_ids and auto_reconnect_single_matching_connection:
        connections_payload = await nango.list_connections(secret.nango_secret_key)
        matching_connections = [
            connection for connection in _as_data_list(connections_payload)
            if _connection_matches_integration(connection, integration_id)
        ]
        if len(matching_connections) == 1:
            reconnect_connection_ids = [
                matching_connections[0].get("connection_id")
                or matching_connections[0].get("connectionId")
                or ""
            ]
            reconnect_connection_ids = [connection_id for connection_id in reconnect_connection_ids if connection_id]
        elif len(matching_connections) > 1:
            operator_notes.append(
                f"Found {len(matching_connections)} connections for integration {integration_id}; "
                "no reconnect sessions were created automatically. Pass reconnect_connection_ids for the affected accounts."
            )
        else:
            operator_notes.append(
                f"No existing connections were found for integration {integration_id}; no reconnect session was created."
            )

    reconnect_sessions: list[dict[str, Any]] = []
    if scope_change and reconnect_connection_ids:
        for connection_id in reconnect_connection_ids:
            reconnect_sessions.append(
                {
                    "connection_id": connection_id,
                    "response": sanitize_response(
                        await nango.create_reconnect_session(
                            secret.nango_secret_key,
                            connection_id=connection_id,
                            integration_id=integration_id,
                        )
                    ),
                }
            )
        operator_notes.append("Created reconnect session(s) for supplied or inferred affected connection id(s).")
    elif scope_change:
        operator_notes.append(
            "No reconnect session was created. Pass reconnect_connection_ids when you want the MCP to create "
            "reconnect sessions after the scope patch."
        )

    if operator_notes and isinstance(response, dict):
        response["_nango_mcp_operator_notes"] = operator_notes
    if reconnect_sessions and isinstance(response, dict):
        response["_nango_mcp_reconnect_sessions"] = reconnect_sessions
    return response


@mcp.tool()
async def delete_integration(ctx: Context, environment: str, integration_id: str) -> Any:
    """Delete a Nango integration."""
    approval = await _authorize_mutation(ctx, "delete_integration", environment, (integration_id,))
    if approval:
        return approval
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
async def refresh_connection_credentials(
    ctx: Context,
    environment: str,
    connection_id: str,
    provider_config_key: str,
) -> dict[str, Any]:
    """Force an OAuth refresh and return only a non-secret credential summary."""
    approval = await _authorize_mutation(
        ctx, "refresh_connection_credentials", environment, (connection_id, provider_config_key)
    )
    if approval:
        return approval
    _, nango, secret = await _resolve(environment)
    response = await nango.get_connection(
        secret.nango_secret_key,
        connection_id,
        provider_config_key,
        include_credentials=True,
        force_refresh=True,
    )
    credentials = response.get("credentials", {}) if isinstance(response, dict) else {}
    if not isinstance(credentials, dict):
        credentials = {}
    raw_credentials = credentials.get("raw", {})
    if not isinstance(raw_credentials, dict):
        raw_credentials = {}
    return {
        "connection_id": connection_id,
        "provider_config_key": provider_config_key,
        "refreshed": True,
        "credential_summary": {
            "has_access_token": bool(credentials.get("access_token")),
            "has_refresh_token": bool(credentials.get("refresh_token")),
            "token_type": credentials.get("token_type") or raw_credentials.get("token_type"),
            "scope": credentials.get("scope") or raw_credentials.get("scope"),
            "expires_at": credentials.get("expires_at") or raw_credentials.get("expires_at"),
        },
    }


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
async def import_connection(ctx: Context, environment: str, payload: dict[str, Any]) -> Any:
    """Import/create a connection using the Nango API payload shape."""
    approval = await _authorize_mutation(ctx, "import_connection", environment, (payload,))
    if approval:
        return approval
    _, nango, secret = await _resolve(environment)
    return sanitize_response(await nango.import_connection(secret.nango_secret_key, payload))


@mcp.tool()
async def delete_connection(
    ctx: Context,
    environment: str,
    connection_id: str,
    provider_config_key: str,
) -> Any:
    """Delete one Nango connection."""
    approval = await _authorize_mutation(ctx, "delete_connection", environment, (connection_id, provider_config_key))
    if approval:
        return approval
    _, nango, secret = await _resolve(environment)
    return sanitize_response(await nango.delete_connection(secret.nango_secret_key, connection_id, provider_config_key))


@mcp.tool()
async def patch_connection_tags(
    ctx: Context,
    environment: str,
    connection_id: str,
    provider_config_key: str,
    tags: dict[str, str],
) -> Any:
    """Replace a connection's complete tag set. Fetch and merge first when changing one tag."""
    approval = await _authorize_mutation(
        ctx, "patch_connection_tags", environment, (connection_id, provider_config_key, tags)
    )
    if approval:
        return approval
    _, nango, secret = await _resolve(environment)
    return sanitize_response(
        await nango.patch_connection_tags(secret.nango_secret_key, connection_id, provider_config_key, tags)
    )


@mcp.tool()
async def set_connection_metadata(
    ctx: Context,
    environment: str,
    connection_id: str,
    provider_config_key: str,
    metadata: dict[str, Any],
    patch: bool = False,
) -> Any:
    """Set or patch connection metadata. Do not put credentials or required connection config here."""
    approval = await _authorize_mutation(
        ctx, "set_connection_metadata", environment, (connection_id, provider_config_key, metadata, patch)
    )
    if approval:
        return approval
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
    ctx: Context,
    environment: str,
    allowed_integrations: list[str],
    tags: dict[str, str] | None = None,
    integrations_config_defaults: dict[str, Any] | None = None,
) -> Any:
    """Create a Nango Connect session token."""
    approval = await _authorize_mutation(
        ctx, "create_connect_session", environment, (allowed_integrations, tags, integrations_config_defaults)
    )
    if approval:
        return approval
    payload: dict[str, Any] = {"allowed_integrations": allowed_integrations}
    if tags:
        payload["tags"] = tags
    if integrations_config_defaults:
        payload["integrations_config_defaults"] = integrations_config_defaults

    _, nango, secret = await _resolve(environment)
    return sanitize_response(await nango.create_connect_session(secret.nango_secret_key, payload))


@mcp.tool()
async def create_standard_connect_session(
    ctx: Context,
    environment: str,
    provider_config_key: str,
    principal: str,
    owner_kind: str,
    purpose: str,
    organization_id: str | None = None,
    display_name: str | None = None,
    email: str | None = None,
    integrations_config_defaults: dict[str, Any] | None = None,
    oauth_app_owner: str | None = None,
) -> Any:
    """Create a Connect session and return its post-auth finalization contract."""
    approval = await _authorize_mutation(
        ctx, "create_standard_connect_session", environment, (provider_config_key, principal)
    )
    if approval:
        return approval
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
    settings, _, _ = _runtime()
    return {
        "tags": tags,
        "post_auth_finalization": {
            "provider_config_key": provider_config_key,
            "principal": principal.strip(),
            "owner_kind": owner_kind,
            "purpose": purpose,
            "oauth_app_owner": oauth_app_owner,
            "display_name": display_name.strip() if display_name else principal.strip(),
            "email": email.strip() if email else None,
            "metadata": convention_metadata(
                environment,
                principal,
                owner_kind,
                purpose,
                namespace=settings.metadata_namespace,
                oauth_app_owner=oauth_app_owner,
            ),
        },
        "response": response,
    }


@mcp.tool()
async def create_reconnect_session(
    ctx: Context,
    environment: str,
    connection_id: str,
    provider_config_key: str,
) -> Any:
    """Create a Nango reconnect session for an existing connection."""
    approval = await _authorize_mutation(
        ctx, "create_reconnect_session", environment, (connection_id, provider_config_key)
    )
    if approval:
        return approval
    _, nango, secret = await _resolve(environment)
    return sanitize_response(
        await nango.create_reconnect_session(
            secret.nango_secret_key,
            connection_id=connection_id,
            integration_id=provider_config_key,
        )
    )


def _camelize_owned_envelope(value: dict[str, Any]) -> dict[str, Any]:
    """Camel-case MCP-owned envelope keys while preserving provider JSON verbatim."""
    mapping = {
        "content_type": "contentType",
        "response_headers": "responseHeaders",
        "rate_limit": "rateLimit",
    }
    result = {mapping.get(key, key): child for key, child in value.items()}
    rate_limit = result.get("rateLimit")
    if isinstance(rate_limit, dict):
        result["rateLimit"] = {
            re.sub(r"_([a-z])", lambda match: match.group(1).upper(), str(key)): child
            for key, child in rate_limit.items()
        }
    return result


def _artifact_store(settings: Settings) -> ArtifactStore:
    root = settings.artifact_root
    if not root:
        uid = getattr(os, "getuid", lambda: 0)()
        root = str(Path(tempfile.gettempdir()) / f"nango-mcp-{uid}" / "artifacts")
    key = settings.request_state_keys[0] if settings.request_state_keys else _state_keys[0]
    return ArtifactStore(
        root,
        "nango-mcp://artifact",
        key,
        settings.artifact_ttl_seconds,
        settings.artifact_max_bytes,
    )


def _binary_store(settings: Settings) -> BinaryResourceStore:
    root = settings.artifact_root
    if not root:
        uid = getattr(os, "getuid", lambda: 0)()
        root = str(Path(tempfile.gettempdir()) / f"nango-mcp-{uid}" / "artifacts")
    return BinaryResourceStore(str(Path(root) / "downloads"), settings.artifact_ttl_seconds)


def _artifact_tool_result(result: dict[str, Any]) -> CallToolResult:
    meta = result.get("responseMeta")
    artifact = meta.get("artifact") if isinstance(meta, dict) else None
    content: list[Any] = [
        TextContent(type="text", text=json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    ]
    if isinstance(artifact, dict) and artifact.get("uri"):
        content.append(
            ResourceLink(
                name=f"Nango response {artifact['id']}",
                uri=artifact["uri"],
                mimeType=artifact["mediaType"],
                size=artifact["byteLength"],
                description="Complete immutable provider response envelope",
            )
        )
    return CallToolResult(
        content=content,
        structuredContent=result,
    )


@mcp.tool(structured_output=False)
async def proxy_request(
    ctx: Context,
    environment: str,
    providerConfigKey: str,
    connectionId: str,
    method: str,
    path: str,
    query: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    baseUrlOverride: str | None = None,
    body: Any | None = None,
    responseMode: str = "auto",
    responsePath: str | None = None,
    fields: list[str] | None = None,
    filters: list[dict[str, Any]] | None = None,
    pageSize: int = 20,
    cursor: str | None = None,
) -> Any:
    """Call a provider API through the Nango Proxy without exposing provider tokens."""
    normalized_method = method.strip().upper()
    caller = require_scope()
    authorize_operation(
        caller,
        "proxy_request",
        provider_config_key=providerConfigKey,
        method=normalized_method,
        path=path,
    )
    mutation_args = (
        providerConfigKey, connectionId, normalized_method, path, query, headers,
        baseUrlOverride, body,
    )
    if normalized_method not in {"GET", "HEAD"}:
        approval = await _authorize_mutation(ctx, "proxy_request", environment, mutation_args)
        if approval:
            return approval
    if responseMode not in {"auto", "inline", "artifact"}:
        raise ValueError("responseMode must be auto, inline, or artifact")
    settings, nango, secret = await _resolve(environment)
    environment_token = current_environment.set(environment)
    try:
        gate = _environment_gate or EnvironmentConcurrencyGate(4, 30.0)
        async with gate.acquire(environment):
            response = await nango.proxy_request(
                secret.nango_secret_key,
                providerConfigKey,
                connectionId,
                normalized_method,
                path,
                query=query,
                headers=headers,
                base_url_override=baseUrlOverride,
                body=body,
            )
    finally:
        current_environment.reset(environment_token)
    public_response = _camelize_owned_envelope(response)
    if public_response.get("rateLimit") is not None:
        return _artifact_tool_result(public_response)
    caller = require_scope()
    key = settings.request_state_keys[0] if settings.request_state_keys else _state_keys[0]
    bounded = bound_proxy_response(
        public_response,
        owner=caller.label,
        environment=environment,
        cursor_key=key,
        store=_artifact_store(settings),
        response_mode="full" if responseMode == "inline" else responseMode,
        response_path=responsePath,
        fields=fields,
        response_filter=filters,
        response_page_size=pageSize,
        response_cursor=cursor,
    )
    return _artifact_tool_result(bounded)


@mcp.resource(
    "nango-mcp://artifact/{artifactId}",
    name="Nango response artifact",
    description="Authenticated complete provider response envelope",
    mime_type="application/json",
)
def read_response_artifact(artifactId: str) -> str:
    settings, _, _ = _runtime()
    caller = require_scope()
    configured = frozenset(item.slug for item in settings.environments)
    permitted = permitted_environments(configured, caller)
    content, _ = _artifact_store(settings).read_authorized(
        artifactId,
        owner=caller.label,
        environments=permitted,
    )
    return content.decode("utf-8")


@mcp.tool(structured_output=False)
async def query_response_artifact(
    environment: str,
    artifactId: str,
    responsePath: str | None = None,
    fields: list[str] | None = None,
    filters: list[dict[str, Any]] | None = None,
    pageSize: int = 20,
    cursor: str | None = None,
    describe: bool = False,
    objectMode: Literal["entries"] | None = None,
    textSearch: dict[str, Any] | None = None,
) -> CallToolResult:
    """Query a stored provider response with bounded, strict camelCase controls."""
    settings, _, _ = await _resolve(environment)
    caller = require_scope()
    store = _artifact_store(settings)
    store.prune()
    result = store.query(
        artifactId,
        owner=caller.label,
        environment=environment,
        response_path=responsePath,
        fields=fields,
        response_filter=filters,
        response_page_size=pageSize,
        cursor=cursor,
        describe=describe,
        object_mode=objectMode,
        text_search=textSearch,
    )
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False, separators=(",", ":")))],
        structuredContent=result,
    )


@mcp.tool(structured_output=False)
async def download_provider_file(
    environment: str,
    providerConfigKey: str,
    connectionId: str,
    path: str,
    query: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    baseUrlOverride: str | None = None,
    suggestedName: str | None = None,
) -> CallToolResult:
    """Stream a provider GET response into a protected MCP binary resource."""
    caller = require_scope()
    authorize_operation(
        caller,
        "download_provider_file",
        provider_config_key=providerConfigKey,
        method="GET",
        path=path,
    )
    if suggestedName is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", suggestedName):
        raise ValueError("suggestedName must be a plain 1-120 character filename")
    settings, nango, secret = await _resolve(environment)
    store = _binary_store(settings)
    store.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = store.root / f".incoming-{secrets.token_urlsafe(18)}"
    safe_headers = {
        key: value
        for key, value in (headers or {}).items()
        if key.lower() not in {"accept", "nango-proxy-accept"}
    }
    environment_token = current_environment.set(environment)
    try:
        gate = _environment_gate or EnvironmentConcurrencyGate(4, 30.0)
        async with gate.acquire(environment):
            metadata = await nango.download_provider_file(
                secret.nango_secret_key,
                providerConfigKey,
                connectionId,
                path,
                destination,
                max_bytes=settings.artifact_max_bytes,
                query=query,
                headers=safe_headers,
                base_url_override=baseUrlOverride,
            )
        resource = store.ingest(
            destination,
            owner=caller.label,
            environment=environment,
            content_type=str(metadata["content_type"]),
            byte_length=int(metadata["byte_length"]),
            sha256=str(metadata["sha256"]),
            suggested_name=suggestedName,
        )
    finally:
        current_environment.reset(environment_token)
        destination.unlink(missing_ok=True)
    result = {"ok": True, "status": metadata["status"], "resource": resource}
    return CallToolResult(
        content=[
            TextContent(type="text", text=json.dumps(result, separators=(",", ":"))),
            ResourceLink(
                name=suggestedName or f"Provider download {resource['id']}",
                uri=resource["uri"],
                mimeType=resource["contentType"],
                size=resource["byteLength"],
                description="Complete provider binary response",
            ),
        ],
        structuredContent=result,
    )


@mcp.resource(
    "nango-mcp://download/{resourceId}",
    name="Nango provider download",
    description="Authenticated complete provider binary response",
    mime_type="application/octet-stream",
)
def read_provider_download(resourceId: str) -> bytes:
    settings, _, _ = _runtime()
    caller = require_scope()
    configured = frozenset(item.slug for item in settings.environments)
    permitted = permitted_environments(configured, caller)
    content, _ = _binary_store(settings).read_authorized(
        resourceId,
        owner=caller.label,
        environments=permitted,
    )
    return content


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
    ctx: Context,
    environment: str,
    connection_id: str,
    provider_config_key: str,
    principal: str,
    owner_kind: str,
    purpose: str,
    oauth_app_owner: str | None = None,
    patch_metadata: bool = True,
    display_name: str | None = None,
    email: str | None = None,
) -> dict[str, Any]:
    """Apply suggested Nango MCP tags and metadata to an existing connection."""
    approval = await _authorize_mutation(
        ctx, "apply_connection_convention", environment, (connection_id, provider_config_key, principal)
    )
    if approval:
        return approval
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
    existing_end_user = existing.get("end_user") if isinstance(existing, dict) else None
    if not isinstance(existing_end_user, dict):
        existing_end_user = {}
    projected_display_name = display_name.strip() if display_name else existing_end_user.get("display_name")
    projected_email = email.strip() if email else existing_end_user.get("email")
    if projected_display_name:
        merged_tags["end_user_display_name"] = projected_display_name
    if projected_email:
        merged_tags["end_user_email"] = projected_email
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
        "identity_projection": {
            "display_name": projected_display_name,
            "email": projected_email,
        },
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


def _enforce_strict_tool_arguments(*names: str) -> None:
    for name in names:
        tool = mcp._tool_manager.get_tool(name)  # type: ignore[attr-defined]
        if tool is None:
            raise RuntimeError(f"cannot harden unregistered MCP tool {name}")
        argument_model = tool.fn_metadata.arg_model
        argument_model.model_config["extra"] = "forbid"
        argument_model.model_rebuild(force=True)
        tool.parameters = argument_model.model_json_schema(by_alias=True)


_enforce_strict_tool_arguments(
    "proxy_request",
    "query_response_artifact",
    "download_provider_file",
)


def _audit_event(*, caller: str, outcome: str, duration_ms: int, tool: str = "unknown") -> None:
    print(
        json.dumps(
            {
                "event": "mcp_request",
                "caller": caller,
                "tool": tool,
                "outcome": outcome,
                "durationMs": duration_ms,
            },
            separators=(",", ":"),
            sort_keys=True,
        ),
        flush=True,
    )


class BearerScopeMiddleware:
    """Authenticate HTTP requests without logging credentials or payloads."""

    def __init__(self, app: ASGIApp, registry: TokenRegistry | TokenRegistrySource) -> None:
        self.app = app
        self.registry = registry

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("path") == "/health":
            await JSONResponse({"status": "ok"})(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        started = time.monotonic()
        try:
            registry = self.registry.current() if isinstance(self.registry, TokenRegistrySource) else self.registry
            caller = authenticate(headers.get("authorization"), registry)
        except PermissionError:
            _audit_event(
                caller="unauthenticated",
                outcome="auth_failure",
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            await Response(status_code=401)(scope, receive, send)
            return
        except (OSError, RuntimeError):
            _audit_event(
                caller="unauthenticated",
                outcome="policy_failure",
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            await Response(status_code=503)(scope, receive, send)
            return

        status_code = 200

        async def capture_status(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        scope_token = set_scope(caller)
        try:
            await self.app(scope, receive, capture_status)
        finally:
            reset_scope(scope_token)
        if status_code >= 400:
            _audit_event(
                caller=caller.label,
                tool=headers.get("mcp-name", "unknown"),
                outcome="request_failure" if status_code < 500 else "transport_failure",
                duration_ms=round((time.monotonic() - started) * 1000),
            )


class OAuthScopeMiddleware:
    """Bind validated OAuth claims to the same environment policy as static auth."""

    def __init__(self, app: ASGIApp, verifier: OAuthIntrospectionVerifier) -> None:
        self.app = app
        self.verifier = verifier

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") == "/health":
            await self.app(scope, receive, send)
            return
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        authorization = headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            await Response(status_code=401)(scope, receive, send)
            return
        access_token = await self.verifier.verify_token(authorization[7:].strip())
        if access_token is None:
            await Response(status_code=401)(scope, receive, send)
            return
        try:
            caller = caller_scope_from_access_token(access_token)
        except PermissionError:
            await Response(status_code=403)(scope, receive, send)
            return
        scope_token = set_scope(caller)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_scope(scope_token)


def create_http_app(
    settings: Settings,
    registry: TokenRegistry | TokenRegistrySource | None = None,
) -> ASGIApp:
    oauth_verifier: OAuthIntrospectionVerifier | None = None
    if settings.auth_mode == "oauth":
        if settings.oauth is None:
            raise RuntimeError("OAuth settings are unavailable")
        oauth_verifier = OAuthIntrospectionVerifier(settings.oauth)
        mcp.settings.auth = AuthSettings(
            issuer_url=settings.oauth.issuer_url,
            resource_server_url=settings.oauth.resource_url,
            required_scopes=list(settings.oauth.required_scopes),
        )
        mcp._token_verifier = oauth_verifier  # type: ignore[attr-defined]
    else:
        resolved_registry = registry or TokenRegistrySource(
            settings.token_registry_raw,
            settings.denied_environments,
            settings.token_registry_file,
        )

    @mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=False,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(settings.http_allowed_hosts),
            allowed_origins=[],
        ),
        host=settings.http_host,
    )
    if oauth_verifier is not None:
        return OAuthScopeMiddleware(app, oauth_verifier)
    return BearerScopeMiddleware(app, resolved_registry)


def main() -> None:
    settings = load_settings()
    if settings.transport == "http":
        uvicorn.run(create_http_app(settings), host=settings.http_host, port=settings.http_port)
        return
    implicit = CallerScope(
        label="local-stdio",
        environments=frozenset(item.slug for item in settings.environments),
    )
    scope_token = set_scope(implicit)
    try:
        mcp.run("stdio")
    finally:
        reset_scope(scope_token)
