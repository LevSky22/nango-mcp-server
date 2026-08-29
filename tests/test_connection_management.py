from types import SimpleNamespace

import pytest

from nango_mcp import server


@pytest.mark.asyncio
async def test_list_connections_uses_documented_nango_filters(monkeypatch) -> None:
    class FakeNango:
        def __init__(self) -> None:
            self.filters = None

        async def list_connections(self, _secret, filters):
            self.filters = filters
            return {"connections": []}

    fake = FakeNango()

    async def fake_resolve(_environment):
        return None, fake, SimpleNamespace(nango_secret_key="fake-secret")

    monkeypatch.setattr(server, "_resolve", fake_resolve)
    await server.list_connections(
        "sandbox",
        connectionId="sample-connection",
        integrationId="sample-integration",
        search="person@example.test",
        endUserId="person-123",
        endUserOrganizationId="organization-123",
        limit=25,
    )
    assert fake.filters == {
        "connectionId": "sample-connection",
        "integrationId": "sample-integration",
        "search": "person@example.test",
        "endUserId": "person-123",
        "endUserOrganizationId": "organization-123",
        "limit": 25,
    }


@pytest.mark.asyncio
async def test_standard_connect_session_returns_generic_finalization(monkeypatch) -> None:
    class FakeNango:
        async def create_connect_session(self, _secret, payload):
            assert payload["tags"]["end_user_id"] == "person@example.test"
            return {"data": {"connect_link": "https://connect.example.test"}}

    async def fake_resolve(_environment):
        return None, FakeNango(), SimpleNamespace(nango_secret_key="fake-secret")

    settings = SimpleNamespace(metadata_namespace="nango_mcp")
    monkeypatch.setattr(server, "_resolve", fake_resolve)
    monkeypatch.setattr(server, "_runtime", lambda: (settings, None, None))
    result = await server.create_standard_connect_session(
        None,
        "staging",
        "example-integration",
        "person@example.test",
        "user",
        "messaging",
        displayName="Example Person",
        email="person@example.test",
        oauthAppOwner="customer",
    )

    finalization = result["post_auth_finalization"]
    assert finalization["oauth_app_owner"] == "customer"
    assert finalization["metadata"]["nango_mcp"]["principal"] == "person@example.test"


@pytest.mark.asyncio
async def test_apply_convention_projects_identity_without_patching_end_user(monkeypatch) -> None:
    class FakeNango:
        def __init__(self) -> None:
            self.tags = None

        async def get_connection(self, *_args, **_kwargs):
            return {
                "tags": {"existing": "keep"},
                "end_user": {
                    "display_name": "Existing Name",
                    "email": "existing@example.test",
                },
            }

        async def replace_connection_tags(self, *_args):
            self.tags = _args[-1]
            return {"success": True}

        async def update_connection_metadata(self, *_args, **_kwargs):
            return {"success": True}

    fake = FakeNango()

    async def fake_resolve(_environment):
        return (
            SimpleNamespace(metadata_namespace="nango_mcp"),
            fake,
            SimpleNamespace(environment="staging", nango_secret_key="fake-secret"),
        )

    monkeypatch.setattr(server, "_resolve", fake_resolve)
    result = await server.apply_connection_convention(
        None,
        "staging",
        "connection-id",
        "example-integration",
        "person@example.test",
        "user",
        "messaging",
    )

    assert fake.tags["existing"] == "keep"
    assert fake.tags["end_user_display_name"] == "Existing Name"
    assert fake.tags["end_user_email"] == "existing@example.test"
    assert result["identity_projection"]["display_name"] == "Existing Name"


@pytest.mark.asyncio
async def test_refresh_connection_credentials_never_returns_tokens(monkeypatch) -> None:
    class FakeNango:
        async def get_connection(self, *_args, **kwargs):
            assert kwargs == {"include_credentials": True, "force_refresh": True}
            return {
                "credentials": {
                    "access_token": "private-access-value",
                    "refresh_token": "private-refresh-value",
                    "raw": {
                        "token_type": "Bearer",
                        "scope": "openid offline_access",
                        "expires_at": "2030-01-01T00:00:00Z",
                    },
                }
            }

    async def fake_resolve(_environment):
        return None, FakeNango(), SimpleNamespace(nango_secret_key="fake-secret")

    monkeypatch.setattr(server, "_resolve", fake_resolve)
    result = await server.refresh_connection_credentials(
        None,
        "staging",
        "connection-id",
        "example-integration",
    )

    summary = result["credential_summary"]
    assert summary["has_access_token"] is True
    assert summary["has_refresh_token"] is True
    assert summary["scope"] == "openid offline_access"
    assert "private-access-value" not in str(result)
    assert "private-refresh-value" not in str(result)
