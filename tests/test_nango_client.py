import httpx
import pytest

from nango_mcp.nango import NangoClient


class CapturingNangoClient(NangoClient):
    def __init__(
        self,
        base_url: str = "https://nango.example.test",
        public_base_url: str | None = None,
    ) -> None:
        super().__init__(base_url, public_base_url=public_base_url)
        self.calls = []

    async def _send(self, secret_key, method, path, *, params=None, body=None, headers=None):
        self.calls.append(
            {
                "secret_key": secret_key,
                "method": method,
                "path": path,
                "params": params,
                "body": body,
                "headers": headers,
            }
        )
        return httpx.Response(
            200,
            json={"ok": True},
            headers={"content-type": "application/json", "x-request-id": "req_123"},
            request=httpx.Request(method, f"{self.base_url}{path}"),
        )


class ConnectSessionNangoClient(CapturingNangoClient):
    async def _send(self, secret_key, method, path, *, params=None, body=None, headers=None):
        self.calls.append(
            {
                "secret_key": secret_key,
                "method": method,
                "path": path,
                "params": params,
                "body": body,
                "headers": headers,
            }
        )
        return httpx.Response(
            201,
            json={
                "data": {
                    "token": "nango_connect_session_test",
                    "connect_link": "https://connect.example.test/?session_token=nango_connect_session_test",
                    "expires_at": "2026-05-29T22:01:14.114Z",
                }
            },
            headers={"content-type": "application/json"},
            request=httpx.Request(method, f"{self.base_url}{path}"),
        )


@pytest.mark.asyncio
async def test_get_integration_includes_credentials_with_current_nango_query_shape() -> None:
    client = CapturingNangoClient()

    await client.get_integration("nango-secret", "zoho-crm", include_credentials=True)

    assert client.calls == [
        {
            "secret_key": "nango-secret",
            "method": "GET",
            "path": "/integrations/zoho-crm",
            "params": {"include": "credentials"},
            "body": None,
            "headers": None,
        }
    ]


@pytest.mark.asyncio
async def test_set_connection_metadata_uses_documented_body_shape() -> None:
    client = CapturingNangoClient()

    await client.set_connection_metadata(
        "nango-secret",
        "prod__microsoft__service",
        "microsoft",
        {"nango_mcp": {"purpose": "mailbox_readwrite"}},
        patch=True,
    )

    assert client.calls == [
        {
            "secret_key": "nango-secret",
            "method": "PATCH",
            "path": "/connections/metadata",
            "params": None,
            "body": {
                "connection_id": "prod__microsoft__service",
                "provider_config_key": "microsoft",
                "metadata": {"nango_mcp": {"purpose": "mailbox_readwrite"}},
            },
            "headers": None,
        }
    ]


@pytest.mark.asyncio
async def test_create_connect_session_adds_self_hosted_api_url_to_connect_link() -> None:
    client = ConnectSessionNangoClient()

    response = await client.create_connect_session(
        "nango-secret",
        {"allowed_integrations": ["microsoft"]},
    )

    assert (
        response["data"]["connect_link"]
        == "https://connect.example.test/?session_token=nango_connect_session_test&apiURL=https%3A%2F%2Fnango.example.test"
    )


@pytest.mark.asyncio
async def test_connect_link_replaces_internal_api_url_with_public_url() -> None:
    client = ConnectSessionNangoClient(
        base_url="http://nango-service:3003",
        public_base_url="https://nango.example.test",
    )

    link = client._with_self_hosted_api_url(
        "https://connect.example.test/?session_token=test&apiURL=http%3A%2F%2Fnango-service%3A3003"
    )

    assert link.endswith("apiURL=https%3A%2F%2Fnango.example.test")


@pytest.mark.asyncio
async def test_get_connection_uses_supported_refresh_query_parameters() -> None:
    client = CapturingNangoClient()

    await client.get_connection(
        "nango-secret",
        "connection-id",
        "example-integration",
        include_credentials=True,
        force_refresh=True,
    )

    assert client.calls[0]["params"] == {
        "provider_config_key": "example-integration",
        "refresh_token": "true",
        "force_refresh": "true",
    }

@pytest.mark.asyncio
async def test_create_reconnect_session_uses_nango_reconnect_body_shape() -> None:
    client = CapturingNangoClient()

    await client.create_reconnect_session(
        "nango-secret",
        connection_id="prod__microsoft__service",
        integration_id="microsoft",
    )

    assert client.calls == [
        {
            "secret_key": "nango-secret",
            "method": "POST",
            "path": "/connect/sessions/reconnect",
            "params": None,
            "body": {
                "connection_id": "prod__microsoft__service",
                "integration_id": "microsoft",
            },
            "headers": None,
        }
    ]


@pytest.mark.asyncio
async def test_create_reconnect_session_adds_self_hosted_api_url_to_connect_link() -> None:
    client = ConnectSessionNangoClient()

    response = await client.create_reconnect_session(
        "nango-secret",
        connection_id="prod__microsoft__service",
        integration_id="microsoft",
    )

    assert client.calls[0]["body"] == {
        "connection_id": "prod__microsoft__service",
        "integration_id": "microsoft",
    }
    assert (
        response["data"]["connect_link"]
        == "https://connect.example.test/?session_token=nango_connect_session_test&apiURL=https%3A%2F%2Fnango.example.test"
    )


@pytest.mark.asyncio
async def test_proxy_request_protects_nango_headers_and_prefixes_provider_headers() -> None:
    client = CapturingNangoClient()

    await client.proxy_request(
        "nango-secret",
        "microsoft",
        "service",
        "GET",
        "/v1.0/me",
        headers={"Authorization": "Bearer wrong", "Prefer": "outlook.body-content-type=text"},
    )

    assert client.calls[0]["headers"] == {
        "Provider-Config-Key": "microsoft",
        "Connection-Id": "service",
        "nango-proxy-accept": "application/json",
        "nango-proxy-Prefer": "outlook.body-content-type=text",
    }
    assert client.calls[0]["path"] == "/proxy/v1.0/me"


@pytest.mark.asyncio
async def test_proxy_request_forwards_base_url_override_as_raw_proxy_header() -> None:
    client = CapturingNangoClient()

    await client.proxy_request(
        "nango-secret",
        "google",
        "service",
        "GET",
        "/v4/spreadsheets/sheet-id/values/A1",
        headers={"base-url-override": "https://ignored.example.test", "Accept": "application/json"},
        base_url_override="https://sheets.googleapis.com",
    )

    assert client.calls[0]["headers"] == {
        "Provider-Config-Key": "google",
        "Connection-Id": "service",
        "nango-proxy-accept": "application/json",
        "base-url-override": "https://sheets.googleapis.com",
        "nango-proxy-Accept": "application/json",
    }


@pytest.mark.asyncio
async def test_proxy_request_returns_http_envelope() -> None:
    client = CapturingNangoClient()

    response = await client.proxy_request(
        "nango-secret",
        "microsoft",
        "service",
        "GET",
        "/v1.0/me",
    )

    assert response == {
        "ok": True,
        "status": 200,
        "content_type": "application/json",
        "response_headers": {"x-request-id": "req_123"},
        "response": {"ok": True},
    }


@pytest.mark.asyncio
async def test_proxy_request_returns_provider_error_envelope() -> None:
    class ErrorNangoClient(CapturingNangoClient):
        async def _send(self, secret_key, method, path, *, params=None, body=None, headers=None):
            self.calls.append({"secret_key": secret_key, "method": method, "path": path})
            return httpx.Response(
                403,
                json={"error": {"code": "Forbidden", "message": "Missing scope"}},
                headers={"content-type": "application/json", "x-ms-request-id": "ms_req_123"},
                request=httpx.Request(method, f"{self.base_url}{path}"),
            )

    client = ErrorNangoClient()

    response = await client.proxy_request(
        "nango-secret",
        "microsoft",
        "service",
        "GET",
        "/v1.0/me",
    )

    assert response == {
        "ok": False,
        "status": 403,
        "content_type": "application/json",
        "response_headers": {"x-ms-request-id": "ms_req_123"},
        "response": {"error": {"code": "Forbidden", "message": "Missing scope"}},
    }
