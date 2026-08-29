import json
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import nango_mcp.server as server
from nango_mcp.auth import CallerScope, reset_scope, set_scope
from nango_mcp.server import _as_data_list, _provider_summary


def test_provider_summary_distinguishes_credential_and_oauth_setup() -> None:
    api_key_summary = _provider_summary(
        {
            "name": "stripe-api-key",
            "display_name": "Stripe (API Key)",
            "auth_mode": "API_KEY",
            "docs": "https://nango.dev/docs/api-integrations/stripe-api-key",
        }
    )
    oauth_summary = _provider_summary(
        {
            "name": "stripe",
            "display_name": "Stripe Connect",
            "auth_mode": "OAUTH2",
            "auth": {"default_scopes": ["read_write"]},
        }
    )

    assert api_key_summary["auth_mode"] == "API_KEY"
    assert "Do not ask for OAuth app credentials" in api_key_summary["setup_note"]
    assert oauth_summary["auth_mode"] == "OAUTH2"
    assert oauth_summary["default_scopes"] == ["read_write"]


def test_as_data_list_accepts_wrapped_or_raw_lists() -> None:
    assert _as_data_list({"data": [{"name": "stripe"}, "skip"]}) == [{"name": "stripe"}]
    assert _as_data_list([{"name": "github"}]) == [{"name": "github"}]
    assert _as_data_list({"data": {"name": "github"}}) == []


def test_all_tool_schemas_are_strict_camel_case() -> None:
    expected = {
        "describe_connection_convention": set(),
        "list_environments": {"refresh"},
        "check_environment": {"environment", "refresh"},
        "list_integrations": {"environment", "refreshSecret"},
        "get_integration": {"environment", "integrationId", "includeCredentials"},
        "search_provider_templates": {"environment", "query", "limit", "includeRawTemplates"},
        "create_integration": {"environment", "payload"},
        "update_integration": {
            "environment", "integrationId", "fields", "reconnectConnectionIds",
            "autoReconnectSingleMatchingConnection",
        },
        "delete_integration": {"environment", "integrationId"},
        "list_connections": {
            "environment", "connectionId", "integrationId", "search", "endUserId",
            "endUserOrganizationId", "limit",
        },
        "get_connection": {"environment", "connectionId", "providerConfigKey", "includeCredentials"},
        "refresh_connection_credentials": {"environment", "connectionId", "providerConfigKey"},
        "get_connection_context": {
            "environment", "connectionId", "providerConfigKey", "includeRawProviderTemplate",
        },
        "import_connection": {"environment", "payload"},
        "delete_connection": {"environment", "connectionId", "providerConfigKey"},
        "replace_connection_tags": {"environment", "connectionId", "providerConfigKey", "tags"},
        "update_connection_metadata": {
            "environment", "connectionId", "providerConfigKey", "metadata", "mode",
        },
        "create_connect_session": {"environment", "allowedIntegrations", "tags", "integrationsConfigDefaults"},
        "create_standard_connect_session": {
            "environment", "providerConfigKey", "principal", "ownerKind", "purpose", "organizationId",
            "displayName", "email", "integrationsConfigDefaults", "oauthAppOwner",
        },
        "create_reconnect_session": {"environment", "connectionId", "providerConfigKey"},
        "stage_proxy_request_body": {"environment", "body"},
        "proxy_request": {
            "environment", "providerConfigKey", "connectionId", "method", "path", "query", "headers",
            "baseUrlOverride", "body", "bodyArtifactId", "responseMode", "responsePath", "fields", "filters",
            "pageSize", "cursor",
        },
        "query_response_artifact": {
            "environment", "artifactId", "responsePath", "fields", "filters", "pageSize", "cursor",
            "describe", "objectMode", "textSearch",
        },
        "download_provider_file": {
            "environment", "providerConfigKey", "connectionId", "path", "query", "headers",
            "baseUrlOverride", "suggestedName",
        },
        "build_connection_convention": {
            "environment", "providerConfigKey", "principal", "ownerKind", "purpose", "oauthAppOwner",
        },
        "apply_connection_convention": {
            "environment", "connectionId", "providerConfigKey", "principal", "ownerKind", "purpose",
            "oauthAppOwner", "patchMetadata",
        },
        "audit_connection_conventions": {"environment", "limit"},
    }
    assert set(server.mcp._tool_manager._tools) == set(expected)  # type: ignore[attr-defined]
    for name, properties in expected.items():
        tool = server.mcp._tool_manager.get_tool(name)
        schema = tool.parameters
        assert schema["additionalProperties"] is False, name
        assert set(schema.get("properties", {})) == properties, name
        if name in {"proxy_request", "query_response_artifact", "stage_proxy_request_body"}:
            assert tool.output_schema is not None, name

        def assert_no_null_defaults(value: object) -> None:
            if isinstance(value, dict):
                assert value.get("default", "missing") is not None, name
                for child in value.values():
                    assert_no_null_defaults(child)
            elif isinstance(value, list):
                for child in value:
                    assert_no_null_defaults(child)

        assert_no_null_defaults(schema)


@pytest.mark.asyncio
async def test_management_tool_rejects_legacy_snake_case_before_execution() -> None:
    with pytest.raises(ToolError, match="connection_id"):
        await server.mcp.call_tool(
            "get_connection",
            {
                "environment": "sandbox",
                "connection_id": "sample-connection",
                "provider_config_key": "sample-integration",
            },
        )


@pytest.mark.asyncio
async def test_proxy_request_returns_provider_payload_as_json_text(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"value": [{"subject": "Quote request", "from": {"emailAddress": {"address": "lead@example.test"}}}]}
    envelope = {"ok": True, "status": 200, "content_type": "application/json", "response_headers": {}, "response": payload}
    calls = []

    class FakeNango:
        async def proxy_request(self, *args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return envelope

    async def fake_resolve(environment: str):
        settings = SimpleNamespace(
            request_state_keys=(),
            artifact_root="",
            artifact_ttl_seconds=86400,
            artifact_max_bytes=50 * 1024 * 1024,
        )
        return settings, FakeNango(), SimpleNamespace(environment=environment, nango_secret_key="nango-secret")

    monkeypatch.setattr(server, "_resolve", fake_resolve)

    scope_token = set_scope(CallerScope("test", frozenset({"prod"})))
    try:
        result = await server.proxy_request(
            None,
            "prod",
            "microsoft-entra-id",
            "service",
            "GET",
            "/v1.0/me/messages",
        )

        structured = result.structured_content
        assert structured["status"] == 200
        assert structured["contentType"] == "application/json"
        assert structured["response"] == payload
        assert calls[0]["kwargs"]["base_url_override"] is None

        mcp_result = await server.mcp.call_tool(
            "proxy_request",
            {
                "environment": "prod",
                "providerConfigKey": "microsoft-entra-id",
                "connectionId": "service",
                "method": "GET",
                "path": "/v1.0/me/messages",
                "baseUrlOverride": "https://graph.microsoft.com",
            },
        )
        assert mcp_result.structured_content["response"] == payload
        assert calls[1]["kwargs"]["base_url_override"] == "https://graph.microsoft.com"
    finally:
        reset_scope(scope_token)


@pytest.mark.asyncio
async def test_proxy_provider_error_precedes_invalid_response_controls(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeNango:
        async def proxy_request(self, *_args, **_kwargs):
            return {
                "ok": False,
                "status": 404,
                "content_type": "application/json",
                "response_headers": {"x-request-id": "request-123"},
                "response": {"error": {"message": "Not found"}},
            }

    async def fake_resolve(environment: str):
        settings = SimpleNamespace(
            request_state_keys=(), artifact_root="", artifact_ttl_seconds=86400,
            artifact_max_bytes=50 * 1024 * 1024,
        )
        return settings, FakeNango(), SimpleNamespace(environment=environment, nango_secret_key="fake-secret")

    monkeypatch.setattr(server, "_resolve", fake_resolve)
    scope_token = set_scope(CallerScope("test", frozenset({"sandbox"})))
    try:
        result = await server.proxy_request(
            None, "sandbox", "sample-integration", "sample-connection", "GET", "/items",
            responsePath="/doesNotExist", fields=["id"],
        )
    finally:
        reset_scope(scope_token)

    structured = result.structured_content
    assert structured["ok"] is False
    assert structured["status"] == 404
    assert structured["response"] == {"error": {"message": "Not found"}}
    assert "were not applied" in structured["responseMeta"]["warning"]


@pytest.mark.asyncio
async def test_json_artifact_resource_is_descriptor_only_and_not_advertised(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    store = server.ArtifactStore(str(tmp_path), "nango-mcp://artifact", "cursor-key", 3600, 1024 * 1024)
    artifact = store.write(
        {"ok": True, "response": {"secretValue": "stored-only"}},
        owner="test",
        environment="sandbox",
    )
    settings = SimpleNamespace(environments=[SimpleNamespace(slug="sandbox")])
    monkeypatch.setattr(server, "_runtime", lambda: (settings, None, None))
    monkeypatch.setattr(server, "_artifact_store", lambda _settings: store)

    scope_token = set_scope(CallerScope("test", frozenset({"sandbox"})))
    try:
        contents = await server.mcp.read_resource(artifact["uri"])
        templates = await server.mcp.list_resource_templates()
    finally:
        reset_scope(scope_token)

    descriptor = json.loads(list(contents)[0].content)
    template_uris = {str(item.uri_template) for item in templates}
    assert descriptor["rawReadable"] is False
    assert descriptor["queryTool"] == "query_response_artifact"
    assert "stored-only" not in json.dumps(descriptor)
    assert "nango-mcp://artifact/{artifactId}" not in template_uris
    assert "nango-mcp://download/{resourceId}" in template_uris
