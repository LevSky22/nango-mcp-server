from types import SimpleNamespace

import pytest

from nango_mcp import server
from nango_mcp.auth import CallerScope, reset_scope, set_scope


def _settings(tmp_path):
    return SimpleNamespace(
        artifact_root=str(tmp_path),
        artifact_ttl_seconds=3600,
        artifact_max_bytes=1024 * 1024,
        request_state_keys=("request-state-key",),
    )


def test_stdio_staging_uses_process_fallback_key(tmp_path) -> None:
    settings = _settings(tmp_path)
    settings.request_state_keys = ()

    assert server._mutation_body_store(settings).key == server._state_keys[0]


@pytest.mark.asyncio
async def test_staged_body_executes_exact_payload_and_has_no_reader(monkeypatch, tmp_path) -> None:
    settings = _settings(tmp_path)

    class FakeNango:
        def __init__(self) -> None:
            self.bodies = []

        async def proxy_request(self, *_args, **kwargs):
            self.bodies.append(kwargs["body"])
            return {
                "ok": True, "status": 200, "content_type": "application/json",
                "response_headers": {}, "response": {"updated": True},
            }

    fake = FakeNango()

    async def fake_resolve(environment: str):
        return settings, fake, SimpleNamespace(environment=environment, nango_secret_key="fake-secret")

    monkeypatch.setattr(server, "_resolve", fake_resolve)
    monkeypatch.setattr(server, "_runtime", lambda: (settings, None, fake))
    scope_token = set_scope(CallerScope("operator", frozenset({"sandbox"})))
    body = {"items": [{"id": index, "value": f"value-{index}"} for index in range(60)]}
    try:
        staged = await server.mcp.call_tool(
            "stage_proxy_request_body", {"environment": "sandbox", "body": body}
        )
        descriptor = staged.structured_content
        approval_message = server._approval_message(
            "proxy_request",
            "sandbox",
            (
                "sample-integration", "sample-connection", "PUT", "/items/item-123",
                None, None, None, None, descriptor["id"],
            ),
        )
        result = await server.proxy_request(
            None, "sandbox", "sample-integration", "sample-connection", "PUT", "/items/item-123",
            bodyArtifactId=descriptor["id"],
        )
        templates = await server.mcp.list_resource_templates()
    finally:
        reset_scope(scope_token)

    assert result.structured_content["response"] == {"updated": True}
    assert fake.bodies == [body]
    assert descriptor["rawReadable"] is False
    assert descriptor["queryable"] is False
    assert "value-1" not in str(descriptor)
    assert f"bodyArtifactId={descriptor['id']}" in approval_message
    assert "bodySha256=" in approval_message
    assert "value-1" not in approval_message
    assert all("mutation" not in str(template.uri_template) for template in templates)


@pytest.mark.asyncio
async def test_oversized_inline_body_rejected_before_resolution_or_provider(monkeypatch) -> None:
    calls = []

    async def unexpected_resolve(_environment: str):
        calls.append("resolve")
        raise AssertionError("resolution must not run")

    monkeypatch.setattr(server, "_resolve", unexpected_resolve)
    scope_token = set_scope(CallerScope("operator", frozenset({"sandbox"})))
    try:
        result = await server.proxy_request(
            None, "sandbox", "sample-integration", "sample-connection", "PUT", "/items/item-123",
            body={"items": [{"id": index} for index in range(50)]},
        )
    finally:
        reset_scope(scope_token)

    assert result.is_error
    assert result.structured_content["response"]["error"]["code"] == "INLINE_BODY_REQUIRES_STAGING"
    assert "stage_proxy_request_body" in result.content[0].text
    assert calls == []


@pytest.mark.asyncio
async def test_body_and_body_artifact_are_mutually_exclusive_before_resolution(monkeypatch) -> None:
    calls = []

    async def unexpected_resolve(_environment: str):
        calls.append("resolve")
        raise AssertionError("resolution must not run")

    monkeypatch.setattr(server, "_resolve", unexpected_resolve)
    scope_token = set_scope(CallerScope("operator", frozenset({"sandbox"})))
    try:
        result = await server.proxy_request(
            None, "sandbox", "sample-integration", "sample-connection", "PATCH", "/items/item-123",
            body={"name": "Example"}, bodyArtifactId="staged-id",
        )
    finally:
        reset_scope(scope_token)

    assert result.is_error
    assert "mutually exclusive" in result.content[0].text
    assert calls == []


@pytest.mark.parametrize(
    "path",
    [
        "/api/items/123456",
        "/api/items/550e8400-e29b-41d4-a716-446655440000",
        "/api/items/item_123abc",
    ],
)
def test_exact_target_delete_can_delegate_to_trusted_host(path: str) -> None:
    args = ("sample-integration", "sample-connection", "DELETE", path, None, None, None, None, None)
    token = set_scope(CallerScope("operator", frozenset({"sandbox"}), mutation_approval="host"))
    try:
        assert server._is_exact_target_proxy_delete(args) is True
        assert server._requires_server_mutation_approval("proxy_request", args) is False
    finally:
        reset_scope(token)


@pytest.mark.parametrize(
    "path,query,body,artifact_id",
    [
        ("/api/notifications", None, None, None),
        ("/v1/subscriptions", None, None, None),
        ("/api/items/all", None, None, None),
        ("/api/items/*", None, None, None),
        ("/api/items/{id}", None, None, None),
        ("/api/items/item-123", {"all": True}, None, None),
        ("/api/items/item-123", None, {"cascade": True}, None),
        ("/api/items/item-123", None, None, "body-artifact"),
    ],
)
def test_broad_or_ambiguous_delete_stays_server_approved(path, query, body, artifact_id) -> None:
    args = (
        "sample-integration", "sample-connection", "DELETE", path,
        query, None, None, body, artifact_id,
    )
    token = set_scope(CallerScope("operator", frozenset({"sandbox"}), mutation_approval="host"))
    try:
        assert server._is_exact_target_proxy_delete(args) is False
        assert server._requires_server_mutation_approval("proxy_request", args) is True
    finally:
        reset_scope(token)


def test_server_approval_route_override_wins_for_exact_delete() -> None:
    args = (
        "sample-integration", "sample-connection", "DELETE", "/api/items/item-123",
        None, None, None, None, None,
    )
    token = set_scope(CallerScope(
        "operator", frozenset({"sandbox"}), mutation_approval="host",
        server_approval_proxy_path_patterns=(r"DELETE:sample-integration:/api/items/",),
    ))
    try:
        assert server._requires_server_mutation_approval("proxy_request", args) is True
    finally:
        reset_scope(token)


@pytest.mark.asyncio
async def test_policy_middleware_defers_method_check_until_arguments_exist() -> None:
    middleware = server.ToolPolicyMiddleware()
    ctx = SimpleNamespace(method="tools/call", params=SimpleNamespace(name="proxy_request"))
    token = set_scope(CallerScope(
        "operator", frozenset({"sandbox"}), allowed_proxy_methods=frozenset({"GET"}),
    ))
    async def call_next(_ctx):
        return "continued"

    try:
        assert await middleware(ctx, call_next) == "continued"
        with pytest.raises(PermissionError, match="DELETE"):
            server.authorize_operation(
                server.require_scope(), "proxy_request",
                provider_config_key="sample-integration", method="DELETE", path="/items/item-123",
            )
    finally:
        reset_scope(token)
