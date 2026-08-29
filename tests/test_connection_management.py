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
async def test_update_connection_end_user_preserves_both_tag_layers(monkeypatch) -> None:
    class FakeNango:
        def __init__(self) -> None:
            self.reads = 0
            self.patch = None

        async def get_connection(self, *_args):
            self.reads += 1
            if self.reads == 1:
                return {
                    "connection_id": "sample-connection",
                    "provider_config_key": "sample-integration",
                    "tags": {
                        "source": "dashboard", "end_user_id": "person-old",
                        "end_user_email": "old@example.test", "end_user_display_name": "Old Name",
                    },
                    "end_user": {
                        "id": "person-old", "email": "old@example.test", "display_name": "Old Name",
                        "tags": {"segment": "standard", "end_user_email": "stale@example.test"},
                        "organization": None,
                    },
                }
            return {
                "connection_id": "sample-connection",
                "provider_config_key": "sample-integration",
                "tags": {
                    "source": "dashboard", "end_user_id": "person-new",
                    "end_user_email": "person@example.test", "end_user_display_name": "Example Person",
                },
                "end_user": {
                    "id": "person-new", "email": "person@example.test", "display_name": "Example Person",
                    "tags": {"segment": "standard"}, "organization": None,
                },
            }

        async def patch_connection(self, *_args, **kwargs):
            self.patch = kwargs
            return {"credentials": {"access_token": "must-not-return"}}

        async def list_connections(self, _secret, filters):
            if filters["endUserId"] == "person-old":
                return {"connections": [{
                    "connection_id": "sample-connection",
                    "provider_config_key": "sample-integration",
                }]}
            return {"connections": []}

    fake = FakeNango()

    async def fake_resolve(_environment):
        return None, fake, SimpleNamespace(environment="sandbox", nango_secret_key="fake-secret")

    monkeypatch.setattr(server, "_resolve", fake_resolve)
    call_result = await server.update_connection_end_user(
        None, "sandbox", "sample-connection", "sample-integration",
        id="person-new", email="person@example.test", displayName="Example Person",
    )
    result = call_result.structured_content

    assert fake.patch == {
        "end_user": {
            "id": "person-new", "email": "person@example.test", "display_name": "Example Person",
            "tags": {"segment": "standard"},
        },
        "tags": {"source": "dashboard"},
    }
    assert result == {
        "environment": "sandbox",
        "providerConfigKey": "sample-integration",
        "connectionId": "sample-connection",
        "endUser": {
            "id": "person-new", "email": "person@example.test", "displayName": "Example Person",
        },
        "verified": True,
        "preservedConnectionTagCount": 1,
        "preservedEndUserTagCount": 1,
        "secretMaterialReturned": False,
    }
    assert "must-not-return" not in str(result)


@pytest.mark.asyncio
async def test_update_connection_end_user_blocks_native_organization(monkeypatch) -> None:
    class FakeNango:
        patched = False

        async def get_connection(self, *_args):
            return {"end_user": {"id": "person-1", "organization": {"id": "organization-1"}}}

        async def patch_connection(self, *_args, **_kwargs):
            self.patched = True

    fake = FakeNango()
    monkeypatch.setattr(server, "_resolve", lambda _environment: _async_resolve(fake))
    with pytest.raises(server.ConnectionEndUserUpdateError, match="END_USER_ORGANIZATION_UNSUPPORTED"):
        await server.update_connection_end_user(
            None, "sandbox", "sample-connection", "sample-integration", displayName="New Name"
        )
    assert fake.patched is False


async def _async_resolve(fake):
    return None, fake, SimpleNamespace(environment="sandbox", nango_secret_key="fake-secret")


@pytest.mark.asyncio
async def test_update_connection_end_user_requires_id_when_missing(monkeypatch) -> None:
    class FakeNango:
        async def get_connection(self, *_args):
            return {"tags": {}}

    fake = FakeNango()

    async def fake_resolve(_environment):
        return await _async_resolve(fake)

    monkeypatch.setattr(server, "_resolve", fake_resolve)
    with pytest.raises(server.ConnectionEndUserUpdateError, match="END_USER_ID_REQUIRED"):
        await server.update_connection_end_user(
            None, "sandbox", "sample-connection", "sample-integration", displayName="New Name"
        )


@pytest.mark.asyncio
async def test_update_connection_end_user_blocks_shared_and_conflicting_ids(monkeypatch) -> None:
    class FakeNango:
        patched = False

        async def get_connection(self, *_args):
            return {"tags": {}, "end_user": {"id": "shared-id", "tags": {}, "organization": None}}

        async def list_connections(self, _secret, filters):
            if filters["endUserId"] == "shared-id":
                return {"connections": [
                    {"connection_id": "sample-connection", "provider_config_key": "sample-integration"},
                    {"connection_id": "other-connection", "provider_config_key": "other-integration"},
                ]}
            return {"connections": [{
                "connection_id": "other-connection", "provider_config_key": "other-integration",
            }]}

        async def patch_connection(self, *_args, **_kwargs):
            self.patched = True

    fake = FakeNango()

    async def fake_resolve(_environment):
        return await _async_resolve(fake)

    monkeypatch.setattr(server, "_resolve", fake_resolve)
    with pytest.raises(server.ConnectionEndUserUpdateError, match="END_USER_SHARED_CONNECTION_UNSUPPORTED"):
        await server.update_connection_end_user(
            None, "sandbox", "sample-connection", "sample-integration", displayName="New Name"
        )
    assert fake.patched is False

    async def only_target(_secret, filters):
        if filters["endUserId"] == "shared-id":
            return {"connections": [{
                "connection_id": "sample-connection", "provider_config_key": "sample-integration",
            }]}
        return {"connections": [{
            "connection_id": "other-connection", "provider_config_key": "other-integration",
        }]}

    fake.list_connections = only_target
    with pytest.raises(server.ConnectionEndUserUpdateError, match="END_USER_ID_CONFLICT"):
        await server.update_connection_end_user(
            None, "sandbox", "sample-connection", "sample-integration", id="conflicting-id"
        )
    assert fake.patched is False


@pytest.mark.asyncio
async def test_update_connection_end_user_preflights_connection_tag_capacity(monkeypatch) -> None:
    class FakeNango:
        patched = False

        async def get_connection(self, *_args):
            return {
                "tags": {**{f"tag_{index}": "value" for index in range(8)}, "end_user_id": "person-1"},
                "end_user": {"id": "person-1", "tags": {}, "organization": None},
            }

        async def list_connections(self, *_args):
            return {"connections": [{
                "connection_id": "sample-connection", "provider_config_key": "sample-integration",
            }]}

        async def patch_connection(self, *_args, **_kwargs):
            self.patched = True

    fake = FakeNango()

    async def fake_resolve(_environment):
        return await _async_resolve(fake)

    monkeypatch.setattr(server, "_resolve", fake_resolve)
    with pytest.raises(server.ConnectionEndUserUpdateError, match="END_USER_TAG_CAPACITY_EXCEEDED"):
        await server.update_connection_end_user(
            None, "sandbox", "sample-connection", "sample-integration",
            email="person@example.test", displayName="Example Person",
        )
    assert fake.patched is False


@pytest.mark.asyncio
async def test_update_connection_end_user_rejects_nango_tag_drop_before_patch(monkeypatch) -> None:
    class FakeNango:
        patched = False

        async def get_connection(self, *_args):
            return {
                "tags": {"end_user_id": "person-1"},
                "end_user": {
                    "id": "person-1", "email": None, "display_name": None,
                    "tags": {f"custom_{index}": "value" for index in range(9)},
                    "organization": None,
                },
            }

        async def list_connections(self, *_args):
            return {"connections": [{
                "connection_id": "sample-connection", "provider_config_key": "sample-integration",
            }]}

        async def patch_connection(self, *_args, **_kwargs):
            self.patched = True

    fake = FakeNango()

    async def fake_resolve(_environment):
        return await _async_resolve(fake)

    monkeypatch.setattr(server, "_resolve", fake_resolve)
    with pytest.raises(server.ConnectionEndUserUpdateError, match="cause Nango to drop"):
        await server.update_connection_end_user(
            None, "sandbox", "sample-connection", "sample-integration",
            email="person@example.test", displayName="Example Person",
        )
    assert fake.patched is False


@pytest.mark.asyncio
async def test_update_connection_end_user_fails_closed_on_readback_mismatch(monkeypatch) -> None:
    class FakeNango:
        async def get_connection(self, *_args):
            return {
                "tags": {"source": "dashboard", "end_user_id": "person-1"},
                "end_user": {
                    "id": "person-1", "display_name": "Old Name", "email": None,
                    "tags": {}, "organization": None,
                },
            }

        async def list_connections(self, *_args):
            return {"connections": [{
                "connection_id": "sample-connection", "provider_config_key": "sample-integration",
            }]}

        async def patch_connection(self, *_args, **_kwargs):
            return {"success": True}

    fake = FakeNango()

    async def fake_resolve(_environment):
        return await _async_resolve(fake)

    monkeypatch.setattr(server, "_resolve", fake_resolve)
    with pytest.raises(server.ConnectionEndUserUpdateError, match="END_USER_VERIFICATION_FAILED"):
        await server.update_connection_end_user(
            None, "sandbox", "sample-connection", "sample-integration", displayName="New Name"
        )


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
