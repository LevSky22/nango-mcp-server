import time

import httpx
import pytest

from nango_mcp.config import OAuthSettings
from nango_mcp.oauth import OAuthIntrospectionVerifier, caller_scope_from_access_token


class _Client:
    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, *args, **kwargs):
        return httpx.Response(200, json=self.payload, request=httpx.Request("POST", args[0]))


def _settings() -> OAuthSettings:
    return OAuthSettings(
        issuer_url="https://identity.example.test",
        resource_url="https://mcp.example.test/mcp",
        introspection_url="https://identity.example.test/oauth/introspect",
        client_id="resource-server",
        client_secret="test-secret",
    )


@pytest.mark.asyncio
async def test_introspection_validates_resource_scopes_and_maps_environment(monkeypatch) -> None:
    payload = {
        "active": True,
        "client_id": "automation",
        "sub": "operator@example.test",
        "scope": "nango-mcp nango:read nango:write nango:proxy nango:env:sandbox",
        "aud": ["https://mcp.example.test/mcp"],
        "exp": int(time.time()) + 300,
    }
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _Client(payload))
    access_token = await OAuthIntrospectionVerifier(_settings()).verify_token("opaque-token")

    assert access_token is not None
    caller = caller_scope_from_access_token(access_token)
    assert caller.label == "operator@example.test"
    assert caller.environments == frozenset({"sandbox"})
    assert caller.denied_tools == frozenset()


@pytest.mark.asyncio
async def test_introspection_rejects_wrong_audience(monkeypatch) -> None:
    payload = {
        "active": True,
        "client_id": "automation",
        "scope": "nango-mcp nango:env:sandbox",
        "aud": "https://different.example.test/mcp",
    }
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _Client(payload))
    assert await OAuthIntrospectionVerifier(_settings()).verify_token("opaque-token") is None
