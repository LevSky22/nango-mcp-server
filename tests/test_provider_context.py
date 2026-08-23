import json
from types import SimpleNamespace

import pytest

import nango_mcp.server as server
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
        return None, FakeNango(), SimpleNamespace(environment=environment, nango_secret_key="nango-secret")

    monkeypatch.setattr(server, "_resolve", fake_resolve)

    text = await server.proxy_request(
        "prod",
        "microsoft-entra-id",
        "service",
        "GET",
        "/v1.0/me/messages",
    )

    assert isinstance(text, str)
    assert json.loads(text) == envelope
    assert '"status": 200' in text
    assert '"value": [' in text
    assert calls[0]["kwargs"]["base_url_override"] is None

    mcp_result = await server.mcp.call_tool(
        "proxy_request",
        {
            "environment": "prod",
            "provider_config_key": "microsoft-entra-id",
            "connection_id": "service",
            "method": "GET",
            "path": "/v1.0/me/messages",
            "base_url_override": "https://graph.microsoft.com",
        },
    )
    assert json.loads(mcp_result.content[0].text) == envelope
    assert calls[1]["kwargs"]["base_url_override"] == "https://graph.microsoft.com"
