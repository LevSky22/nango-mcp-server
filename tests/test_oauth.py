import time

import httpx
import pytest
from mcp.server.auth.provider import AccessToken

from nango_mcp.auth import authorize_operation
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


def _access_token(*scopes: str) -> AccessToken:
    return AccessToken(
        token="opaque-token",
        client_id="automation",
        scopes=[*scopes, "nango:env:sandbox"],
    )


def test_oauth_proxy_requires_proxy_plus_read_or_write_by_method() -> None:
    read_scope = caller_scope_from_access_token(_access_token("nango:proxy", "nango:read"))
    authorize_operation(
        read_scope, "proxy_request",
        provider_config_key="sample-integration", method="GET", path="/items",
    )
    authorize_operation(
        read_scope, "download_provider_file",
        provider_config_key="sample-integration", method="GET", path="/files/sample",
    )
    with pytest.raises(PermissionError, match="POST"):
        authorize_operation(
            read_scope, "proxy_request",
            provider_config_key="sample-integration", method="POST", path="/items",
        )
    assert "stage_proxy_request_body" in read_scope.denied_tools

    write_scope = caller_scope_from_access_token(_access_token("nango:proxy", "nango:write"))
    authorize_operation(
        write_scope, "proxy_request",
        provider_config_key="sample-integration", method="DELETE", path="/items/item-123",
    )
    assert "stage_proxy_request_body" not in write_scope.denied_tools
    assert "download_provider_file" in write_scope.denied_tools
    with pytest.raises(PermissionError, match="GET"):
        authorize_operation(
            write_scope, "proxy_request",
            provider_config_key="sample-integration", method="GET", path="/items",
        )


def test_oauth_proxy_scope_alone_grants_no_provider_method() -> None:
    scope = caller_scope_from_access_token(_access_token("nango:proxy"))
    assert "stage_proxy_request_body" in scope.denied_tools
    assert "download_provider_file" in scope.denied_tools
    with pytest.raises(PermissionError):
        authorize_operation(
            scope, "proxy_request",
            provider_config_key="sample-integration", method="GET", path="/items",
        )
    with pytest.raises(PermissionError):
        authorize_operation(
            scope, "proxy_request",
            provider_config_key="sample-integration", method="POST", path="/items",
        )
