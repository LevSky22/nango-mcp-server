import hashlib

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from nango_mcp.auth import CallerScope, require_scope
from nango_mcp.server import BearerScopeMiddleware


def _token() -> str:
    return "nangomcp1_" + "test".ljust(43, "x")


@pytest.mark.asyncio
async def test_health_is_public_but_other_routes_require_bearer(capsys) -> None:
    async def scoped(_: Request) -> JSONResponse:
        return JSONResponse({"caller": require_scope().label})

    token = _token()
    registry = {
        hashlib.sha256(token.encode()).hexdigest(): CallerScope(
            "test-runner",
            frozenset({"sandbox"}),
        )
    }
    app = BearerScopeMiddleware(
        Starlette(routes=[Route("/mcp", scoped), Route("/health", scoped)]),
        registry,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        health = await client.get("/health")
        missing = await client.get("/mcp")
        accepted = await client.get("/mcp", headers={"Authorization": f"Bearer {token}"})

    assert health.json() == {"status": "ok"}
    assert missing.status_code == 401
    assert accepted.json() == {"caller": "test-runner"}
    audit = capsys.readouterr().out
    assert '"outcome":"auth_failure"' in audit
    assert token not in audit
