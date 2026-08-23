import json
from types import SimpleNamespace

import pytest

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


def test_proxy_schema_is_strict_camel_case() -> None:
    tool = server.mcp._tool_manager.get_tool("proxy_request")
    assert tool.parameters["additionalProperties"] is False
    properties = tool.parameters["properties"]
    assert "providerConfigKey" in properties
    assert "connectionId" in properties
    assert "baseUrlOverride" in properties
    assert "provider_config_key" not in properties

    query_tool = server.mcp._tool_manager.get_tool("query_response_artifact")
    assert query_tool.parameters["additionalProperties"] is False
    query_properties = query_tool.parameters["properties"]
    assert {"artifactId", "responsePath", "pageSize", "objectMode", "textSearch"} <= set(query_properties)
    assert "artifact_id" not in query_properties

    download_tool = server.mcp._tool_manager.get_tool("download_provider_file")
    assert download_tool.parameters["additionalProperties"] is False
    download_properties = download_tool.parameters["properties"]
    assert {"providerConfigKey", "connectionId", "baseUrlOverride", "suggestedName"} <= set(download_properties)


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
