from __future__ import annotations

import base64
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

MAX_TEXT_BODY_CHARS = 5000
MAX_ERROR_BODY_CHARS = 1000
MAX_BINARY_BODY_BYTES = 8192

SAFE_RESPONSE_HEADER_NAMES = {
    "request-id",
    "x-request-id",
    "x-correlation-id",
    "x-ms-request-id",
    "retry-after",
}
SAFE_RESPONSE_HEADER_PREFIXES = ("ratelimit", "x-ratelimit")


class NangoClient:
    def __init__(self, base_url: str, timeout: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _parse_response_body(self, response: httpx.Response) -> Any:
        if not response.content:
            return None

        content_type = response.headers.get("content-type", "")
        content_type_lower = content_type.lower()
        if "application/json" in content_type_lower:
            try:
                return response.json()
            except ValueError:
                text = response.text
                return {
                    "body": text[:MAX_TEXT_BODY_CHARS],
                    "truncated": len(text) > MAX_TEXT_BODY_CHARS,
                    "parse_error": "invalid_json",
                }

        if (
            content_type_lower.startswith("text/")
            or "xml" in content_type_lower
            or "html" in content_type_lower
            or "application/x-www-form-urlencoded" in content_type_lower
        ):
            text = response.text
            return {
                "body": text[:MAX_TEXT_BODY_CHARS],
                "truncated": len(text) > MAX_TEXT_BODY_CHARS,
            }

        body = response.content
        clipped = body[:MAX_BINARY_BODY_BYTES]
        return {
            "body_base64": base64.b64encode(clipped).decode("ascii"),
            "encoding": "base64",
            "byte_length": len(body),
            "truncated": len(body) > MAX_BINARY_BODY_BYTES,
        }

    def _safe_response_headers(self, headers: httpx.Headers) -> dict[str, str]:
        safe: dict[str, str] = {}
        for key, value in headers.items():
            lower = key.lower()
            if lower in SAFE_RESPONSE_HEADER_NAMES or lower.startswith(SAFE_RESPONSE_HEADER_PREFIXES):
                safe[key] = value
        return safe

    def _response_envelope(self, response: httpx.Response) -> dict[str, Any]:
        return {
            "ok": response.status_code < 400,
            "status": response.status_code,
            "content_type": response.headers.get("content-type", ""),
            "response_headers": self._safe_response_headers(response.headers),
            "response": self._parse_response_body(response),
        }

    def _with_self_hosted_api_url(self, connect_link: str) -> str:
        parts = urlsplit(connect_link)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query.setdefault("apiURL", self.base_url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    def _normalize_connect_session_response(self, response: Any) -> Any:
        if not isinstance(response, dict):
            return response

        data = response.get("data")
        if not isinstance(data, dict):
            return response

        connect_link = data.get("connect_link")
        if isinstance(connect_link, str) and connect_link:
            response = {**response, "data": {**data, "connect_link": self._with_self_hosted_api_url(connect_link)}}
        return response

    async def _send(
        self,
        secret_key: str,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        request_headers = {"Authorization": f"Bearer {secret_key}", "Accept": "application/json"}
        if headers:
            request_headers.update(headers)

        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
            return await client.request(method, path, params=params, json=body, headers=request_headers)

    async def _request(
        self,
        secret_key: str,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        response = await self._send(secret_key, method, path, params=params, body=body, headers=headers)
        if response.status_code >= 400:
            text = response.text[:MAX_ERROR_BODY_CHARS]
            raise RuntimeError(f"Nango request failed: {method} {path} HTTP {response.status_code}: {text}")
        if not response.content:
            return {"status": response.status_code}
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            return response.json()
        return {"status": response.status_code, "body": response.text[:MAX_TEXT_BODY_CHARS]}

    async def list_integrations(self, secret_key: str) -> Any:
        return await self._request(secret_key, "GET", "/integrations")

    async def list_providers(self, secret_key: str) -> Any:
        return await self._request(secret_key, "GET", "/providers")

    async def get_integration(self, secret_key: str, integration_id: str, include_credentials: bool = False) -> Any:
        return await self._request(
            secret_key,
            "GET",
            f"/integrations/{integration_id}",
            params={"include": "credentials"} if include_credentials else None,
        )

    async def create_integration(self, secret_key: str, payload: dict[str, Any]) -> Any:
        return await self._request(secret_key, "POST", "/integrations", body=payload)

    async def update_integration(self, secret_key: str, integration_id: str, fields: dict[str, Any]) -> Any:
        return await self._request(secret_key, "PATCH", f"/integrations/{integration_id}", body=fields)

    async def delete_integration(self, secret_key: str, integration_id: str) -> Any:
        return await self._request(secret_key, "DELETE", f"/integrations/{integration_id}")

    async def list_connections(self, secret_key: str, filters: dict[str, Any] | None = None) -> Any:
        return await self._request(secret_key, "GET", "/connections", params=filters)

    async def get_connection(
        self,
        secret_key: str,
        connection_id: str,
        provider_config_key: str,
        include_credentials: bool = False,
    ) -> Any:
        params: dict[str, Any] = {"provider_config_key": provider_config_key}
        if include_credentials:
            params["include_credentials"] = "true"
        return await self._request(secret_key, "GET", f"/connections/{connection_id}", params=params)

    async def import_connection(self, secret_key: str, payload: dict[str, Any]) -> Any:
        return await self._request(secret_key, "POST", "/connections", body=payload)

    async def patch_connection_tags(
        self,
        secret_key: str,
        connection_id: str,
        provider_config_key: str,
        tags: dict[str, Any],
    ) -> Any:
        return await self._request(
            secret_key,
            "PATCH",
            f"/connections/{connection_id}",
            params={"provider_config_key": provider_config_key},
            body={"tags": tags},
        )

    async def set_connection_metadata(
        self,
        secret_key: str,
        connection_id: str,
        provider_config_key: str,
        metadata: dict[str, Any],
        *,
        patch: bool = False,
    ) -> Any:
        body = {"connection_id": connection_id, "provider_config_key": provider_config_key, "metadata": metadata}
        method = "PATCH" if patch else "POST"
        return await self._request(secret_key, method, "/connections/metadata", body=body)

    async def delete_connection(self, secret_key: str, connection_id: str, provider_config_key: str) -> Any:
        return await self._request(
            secret_key,
            "DELETE",
            f"/connections/{connection_id}",
            params={"provider_config_key": provider_config_key},
        )

    async def create_connect_session(self, secret_key: str, payload: dict[str, Any]) -> Any:
        response = await self._request(secret_key, "POST", "/connect/sessions", body=payload)
        return self._normalize_connect_session_response(response)

    async def create_reconnect_session(self, secret_key: str, connection_id: str, integration_id: str) -> Any:
        body = {"connection_id": connection_id, "integration_id": integration_id}
        response = await self._request(secret_key, "POST", "/connect/sessions/reconnect", body=body)
        return self._normalize_connect_session_response(response)

    async def search_log_operations(self, secret_key: str, environment: str, body: dict[str, Any]) -> Any:
        return await self._request(
            secret_key,
            "POST",
            "/api/v1/logs/operations",
            params={"env": environment},
            body=body,
        )

    async def get_log_operation(self, secret_key: str, environment: str, operation_id: str) -> Any:
        return await self._request(
            secret_key,
            "GET",
            f"/api/v1/logs/operations/{operation_id}",
            params={"env": environment},
        )

    async def search_log_messages(self, secret_key: str, environment: str, body: dict[str, Any]) -> Any:
        return await self._request(
            secret_key,
            "POST",
            "/api/v1/logs/messages",
            params={"env": environment},
            body=body,
        )

    async def proxy_request(
        self,
        secret_key: str,
        provider_config_key: str,
        connection_id: str,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        base_url_override: str | None = None,
        body: Any | None = None,
    ) -> Any:
        normalized_method = method.upper().strip()
        if normalized_method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError("method must be one of GET, POST, PUT, PATCH, DELETE")

        proxy_headers = {
            "Provider-Config-Key": provider_config_key,
            "Connection-Id": connection_id,
            "nango-proxy-accept": "application/json",
        }
        for key, value in (headers or {}).items():
            lower = key.lower()
            if lower in {"authorization", "provider-config-key", "connection-id"}:
                continue
            header_name = (
                key
                if lower in {"base-url-override"} or lower.startswith("nango-proxy-")
                else f"nango-proxy-{key}"
            )
            proxy_headers[header_name] = value
        if base_url_override:
            proxy_headers["base-url-override"] = base_url_override

        clean_path = path.lstrip("/")
        response = await self._send(
            secret_key,
            normalized_method,
            f"/proxy/{clean_path}",
            params=query,
            body=body,
            headers=proxy_headers,
        )
        return self._response_envelope(response)
