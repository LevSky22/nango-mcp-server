import pytest

import nango_mcp.server as server
from nango_mcp.config import EnvironmentConfig, Settings
from nango_mcp.server import (
    _integration_update_contains_scope_change,
    _prepare_integration_update_fields,
)


def test_scope_update_moves_top_level_scope_alias_into_credentials() -> None:
    prepared, notes = _prepare_integration_update_fields(
        {"display_name": "Zoho CRM", "oauth_scopes": "scope.one,scope.two"},
        {
            "data": {
                "credentials": {
                    "type": "OAUTH2",
                    "client_id": "client-id",
                    "client_secret": "client-secret",
                    "webhook_secret": None,
                }
            }
        },
    )

    assert prepared == {
        "display_name": "Zoho CRM",
        "credentials": {
            "type": "OAUTH2",
            "client_id": "client-id",
            "client_secret": "client-secret",
            "scopes": "scope.one,scope.two",
        },
    }
    assert any("credentials.scopes" in note for note in notes)
    assert any("must reconnect" in note for note in notes)


def test_scope_update_preserves_nonblank_current_webhook_secret() -> None:
    prepared, _ = _prepare_integration_update_fields(
        {"scopes": "scope.one scope.two"},
        {
            "data": {
                "credentials": {
                    "type": "OAUTH2",
                    "client_id": "client-id",
                    "client_secret": "client-secret",
                    "webhook_secret": "webhook-secret",
                }
            }
        },
    )

    assert prepared["credentials"]["scopes"] == "scope.one,scope.two"
    assert prepared["credentials"]["webhook_secret"] == "webhook-secret"


@pytest.mark.parametrize(
    ("scope_input", "expected"),
    [
        ("openid profile email", "openid,profile,email"),
        (["openid", "profile", "openid"], "openid,profile"),
    ],
)
def test_scope_update_normalizes_supported_input_shapes(scope_input, expected) -> None:
    prepared, _ = _prepare_integration_update_fields(
        {"scopes": scope_input},
        {
            "data": {
                "credentials": {
                    "client_id": "client-id",
                    "client_secret": "client-secret",
                }
            }
        },
    )

    assert prepared["credentials"]["scopes"] == expected


def test_scope_update_rejects_conflicting_scope_values() -> None:
    with pytest.raises(ValueError, match="conflicting scope values"):
        _prepare_integration_update_fields(
            {
                "scopes": "scope.one",
                "credentials": {
                    "client_id": "client-id",
                    "client_secret": "client-secret",
                    "scopes": "scope.two",
                },
            }
        )


def test_scope_update_accepts_docs_shape_without_type() -> None:
    prepared, notes = _prepare_integration_update_fields(
        {
            "credentials": {
                "client_id": "client-id",
                "client_secret": "client-secret",
                "scopes": "scope.one,scope.two",
            }
        }
    )

    assert prepared == {
        "credentials": {
            "client_id": "client-id",
            "client_secret": "client-secret",
            "scopes": "scope.one,scope.two",
        }
    }
    assert any("must reconnect" in note for note in notes)


def test_scope_update_requires_client_credentials() -> None:
    with pytest.raises(ValueError, match="credentials.client_id, credentials.client_secret"):
        _prepare_integration_update_fields(
            {"credentials": {"type": "OAUTH2", "scopes": "scope.one,scope.two"}}
        )


def test_scope_change_detection_covers_top_level_aliases_and_nested_scopes() -> None:
    assert _integration_update_contains_scope_change({"scopes": "scope.one"})
    assert _integration_update_contains_scope_change({"oauth_scopes": "scope.one"})
    assert _integration_update_contains_scope_change({"default_scopes": "scope.one"})
    assert _integration_update_contains_scope_change({"credentials": {"scopes": "scope.one"}})
    assert not _integration_update_contains_scope_change({"display_name": "No scope change"})


@pytest.mark.asyncio
async def test_update_integration_auto_reconnects_single_matching_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeNango:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object, object]] = []

        async def get_integration(self, secret_key, integration_id, include_credentials=False):
            self.calls.append(("get_integration", integration_id, include_credentials))
            return {
                "data": {
                    "credentials": {
                        "client_id": "client-id",
                        "client_secret": "client-secret",
                    }
                }
            }

        async def update_integration(self, secret_key, integration_id, fields):
            self.calls.append(("update_integration", integration_id, fields))
            return {"data": {"unique_key": integration_id}}

        async def list_connections(self, secret_key, filters=None):
            self.calls.append(("list_connections", filters, None))
            return {
                "data": [
                    {"connection_id": "zoho-main", "provider_config_key": "zoho-crm"},
                    {"connection_id": "ringcentral-main", "provider_config_key": "ringcentral"},
                ]
            }

        async def create_reconnect_session(self, secret_key, connection_id, integration_id):
            self.calls.append(("create_reconnect_session", connection_id, integration_id))
            return {"data": {"connect_link": f"https://connect.example.test/{connection_id}"}}

    fake_nango = FakeNango()
    monkeypatch.setattr(
        server,
        "_settings",
        Settings(
            nango_url="https://api.nango.dev",
            environments=(EnvironmentConfig(slug="prod", secret_key="nango-secret"),),
        ),
    )
    monkeypatch.setattr(server, "_resolver", None)
    monkeypatch.setattr(server, "_nango", None)

    async def fake_resolve(environment: str):
        return None, fake_nango, type("Secret", (), {"nango_secret_key": "nango-secret"})()

    monkeypatch.setattr(server, "_resolve", fake_resolve)

    response = await server.update_integration(
        "prod",
        "zoho-crm",
        {"scopes": "scope.one,scope.two"},
    )

    assert ("list_connections", None, None) in fake_nango.calls
    assert ("create_reconnect_session", "zoho-main", "zoho-crm") in fake_nango.calls
    assert response["_nango_mcp_reconnect_sessions"][0]["connection_id"] == "zoho-main"
