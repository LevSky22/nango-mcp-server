from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from .ratelimit import (
    NangoAPIError,
    NangoRateLimitError,
    classify_rate_limit,
    current_environment,
    next_delay,
    parse_retry_after,
    should_retry,
)

logger = logging.getLogger(__name__)

_NANGO_LIMITER_MARKER = b'"too_many_request"'

MAX_TEXT_BODY_CHARS = 5000
MAX_ERROR_BODY_CHARS = 1000
MAX_BINARY_BODY_BYTES = 8192
MAX_PROXY_BODY_BYTES = 50 * 1024 * 1024

SAFE_RESPONSE_HEADER_NAMES = {
    "request-id",
    "x-request-id",
    "x-correlation-id",
    "x-ms-request-id",
    "retry-after",
}
SAFE_RESPONSE_HEADER_PREFIXES = ("ratelimit", "x-ratelimit")


class NangoClient:
    def __init__(
        self,
        base_url: str,
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        public_base_url: str | None = None,
        rate_limit_max_attempts: int = 4,
        rate_limit_max_wait: float = 45.0,
        rate_limit_backoff_base: float = 1.0,
        rate_limit_ceiling: float = 30.0,
        max_connections: int = 20,
        max_keepalive: int = 10,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.public_base_url = (public_base_url or base_url).rstrip("/")
        self.timeout = timeout
        self.transport = transport
        self.rate_limit_max_attempts = rate_limit_max_attempts
        self.rate_limit_max_wait = rate_limit_max_wait
        self.rate_limit_backoff_base = rate_limit_backoff_base
        self.rate_limit_ceiling = rate_limit_ceiling
        self.max_connections = max_connections
        self.max_keepalive = max_keepalive
        self._http: httpx.AsyncClient | None = None

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
        query["apiURL"] = self.public_base_url
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

        client = await self._client()
        return await client.request(method, path, params=params, json=body, headers=request_headers)

    def _classify_429(self, response: httpx.Response) -> str:
        # Avoid parsing a possibly huge provider error body just to read one code.
        if _NANGO_LIMITER_MARKER not in response.content[:512]:
            return "provider"
        return classify_rate_limit(self._parse_response_body(response))

    async def _client(self) -> httpx.AsyncClient:
        """
        One pooled client for the process. Building a fresh AsyncClient per call meant a
        new TLS handshake on every request and no pool to exert backpressure; with a pool,
        saturation surfaces as PoolTimeout instead of unbounded socket creation.
        """
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                transport=self.transport,
                limits=httpx.Limits(
                    max_connections=self.max_connections,
                    max_keepalive_connections=self.max_keepalive,
                ),
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _send_with_retry(
        self,
        secret_key: str,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any | None = None,
        headers: dict[str, str] | None = None,
        provider_config_key: str | None = None,
    ) -> httpx.Response:
        """
        Send, retrying only on 429; any other status is returned untouched.
        Raises NangoRateLimitError once the attempt or wait budget is spent.
        """
        started = time.monotonic()
        attempt = 0

        while True:
            attempt += 1
            response = await self._send(secret_key, method, path, params=params, body=body, headers=headers)
            if response.status_code != 429:
                return response

            await self._backoff_or_raise(
                response,
                method=method,
                path=path,
                attempt=attempt,
                started=started,
                provider_config_key=provider_config_key,
            )

    async def _backoff_or_raise(
        self,
        response: httpx.Response,
        *,
        method: str,
        path: str,
        attempt: int,
        started: float,
        provider_config_key: str | None,
    ) -> None:
        """
        Sleep before retrying a 429, or raise NangoRateLimitError if the budget is spent.

        Shared by the request and download paths so both honour the same budget; the
        download path previously had no retry at all.
        """
        scope = self._classify_429(response)
        retry_after = parse_retry_after(response.headers.get("retry-after"))
        delay = next_delay(
            attempt=attempt,
            retry_after=retry_after,
            base_seconds=self.rate_limit_backoff_base,
            ceiling_seconds=self.rate_limit_ceiling,
        )
        waited = time.monotonic() - started
        # Budget checked before sleeping, so we abort rather than overrun the tool timeout.
        if (
            should_retry(scope, method)
            and attempt < self.rate_limit_max_attempts
            and waited + delay <= self.rate_limit_max_wait
        ):
            logger.info(
                "nango rate limited; backing off",
                extra={"scope": scope, "attempt": attempt, "delay_s": round(delay, 2), "path": path},
            )
            await asyncio.sleep(delay)
            return

        raise NangoRateLimitError(
            scope=scope,
            environment=current_environment.get(),
            provider_config_key=provider_config_key,
            status=response.status_code,
            headers=self._safe_response_headers(response.headers),
            retry_after_seconds=retry_after,
            attempts=attempt,
            waited_seconds=waited,
        )

    def _proxy_headers(
        self,
        provider_config_key: str,
        connection_id: str,
        *,
        accept: str,
        headers: dict[str, str] | None,
        base_url_override: str | None,
    ) -> dict[str, str]:
        proxy_headers = {
            "Provider-Config-Key": provider_config_key,
            "Connection-Id": connection_id,
            "nango-proxy-accept": accept,
        }
        for key, value in (headers or {}).items():
            lower = key.lower()
            if lower in {"authorization", "provider-config-key", "connection-id"}:
                continue
            header_name = (
                key
                if lower == "base-url-override" or lower.startswith("nango-proxy-")
                else f"nango-proxy-{key}"
            )
            proxy_headers[header_name] = value
        if base_url_override:
            proxy_headers["base-url-override"] = base_url_override
        return proxy_headers

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
        response = await self._send_with_retry(secret_key, method, path, params=params, body=body, headers=headers)
        if response.status_code >= 400:
            text = response.text[:MAX_ERROR_BODY_CHARS]
            raise NangoAPIError(
                f"Nango request failed: {method} {path} HTTP {response.status_code}: {text}",
                status=response.status_code,
                headers=self._safe_response_headers(response.headers),
                body=text,
            )
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
        *,
        force_refresh: bool = False,
    ) -> Any:
        params: dict[str, Any] = {"provider_config_key": provider_config_key}
        if include_credentials:
            params["refresh_token"] = "true"
        if force_refresh:
            params["force_refresh"] = "true"
        return await self._request(secret_key, "GET", f"/connections/{connection_id}", params=params)

    async def patch_connection(
        self,
        secret_key: str,
        connection_id: str,
        provider_config_key: str,
        *,
        end_user: dict[str, Any] | None = None,
        tags: dict[str, Any] | None = None,
        webhook_url_override: str | None = None,
    ) -> Any:
        body: dict[str, Any] = {}
        if end_user is not None:
            body["end_user"] = end_user
        if tags is not None:
            body["tags"] = tags
        if webhook_url_override is not None:
            body["webhook_url_override"] = webhook_url_override
        return await self._request(
            secret_key,
            "PATCH",
            f"/connections/{connection_id}",
            params={"provider_config_key": provider_config_key},
            body=body,
        )

    async def import_connection(self, secret_key: str, payload: dict[str, Any]) -> Any:
        return await self._request(secret_key, "POST", "/connections", body=payload)

    async def patch_connection_tags(
        self,
        secret_key: str,
        connection_id: str,
        provider_config_key: str,
        tags: dict[str, Any],
    ) -> Any:
        return await self.patch_connection(secret_key, connection_id, provider_config_key, tags=tags)

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

        proxy_headers = self._proxy_headers(
            provider_config_key,
            connection_id,
            accept="application/json",
            headers=headers,
            base_url_override=base_url_override,
        )

        clean_path = path.lstrip("/")
        try:
            response = await self._send_with_retry(
                secret_key,
                normalized_method,
                f"/proxy/{clean_path}",
                params=query,
                body=body,
                headers=proxy_headers,
                provider_config_key=provider_config_key,
            )
        except NangoRateLimitError as exc:
            # proxy_request deliberately does not raise on upstream failure - the agent
            # should see and reason about the response. Keep that contract and attach the
            # rate-limit detail; bound_proxy_response spreads the envelope, so it survives.
            return {
                "ok": False,
                "status": exc.status,
                "content_type": "application/json",
                "response_headers": exc.headers,
                "response": None,
                "rate_limit": exc.detail,
            }
        if len(response.content) > MAX_PROXY_BODY_BYTES:
            raise ValueError(f"provider response exceeds the {MAX_PROXY_BODY_BYTES}-byte proxy limit")
        return self._response_envelope(response)

    async def download_provider_file(
        self,
        secret_key: str,
        provider_config_key: str,
        connection_id: str,
        path: str,
        destination: Path,
        *,
        max_bytes: int,
        query: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        base_url_override: str | None = None,
    ) -> dict[str, Any]:
        """Stream a provider GET response to an atomic local file."""
        proxy_headers = self._proxy_headers(
            provider_config_key,
            connection_id,
            # A generic binary Accept value is not universally valid. For
            # example, RingCentral recording content rejects
            # application/octet-stream with HTTP 406 and returns audio/mpeg.
            # Let the provider negotiate its concrete attachment media type.
            accept="*/*",
            headers=headers,
            base_url_override=base_url_override,
        )
        request_headers = {"Authorization": f"Bearer {secret_key}", **proxy_headers}
        clean_path = path.lstrip("/")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.part")
        started = time.monotonic()
        attempt = 0
        try:
            while True:
                attempt += 1
                # Reset per attempt so a retry cannot inherit a partial hash or count.
                total = 0
                digest = hashlib.sha256()
                client = await self._client()
                async with client.stream(
                    "GET",
                    f"/proxy/{clean_path}",
                    params=query,
                    headers=request_headers,
                ) as response:
                    # 429 is detected before any byte reaches disk, so replaying the
                    # stream is clean. GET, so provider 429s are retryable too.
                    if response.status_code == 429:
                        await response.aread()
                        await self._backoff_or_raise(
                            response,
                            method="GET",
                            path=path,
                            attempt=attempt,
                            started=started,
                            provider_config_key=provider_config_key,
                        )
                        continue
                    if response.status_code >= 400:
                        error = (await response.aread())[:MAX_ERROR_BODY_CHARS].decode("utf-8", "replace")
                        # Carry the headers: a 429 here previously lost Retry-After entirely.
                        raise NangoAPIError(
                            f"Nango proxy download failed: GET {path} HTTP {response.status_code}: {error}",
                            status=response.status_code,
                            headers=self._safe_response_headers(response.headers),
                            body=error,
                        )
                    content_length = response.headers.get("content-length")
                    if content_length:
                        try:
                            declared_length = int(content_length)
                        except ValueError:
                            declared_length = 0
                        if declared_length > max_bytes:
                            raise ValueError(f"provider file exceeds the {max_bytes}-byte download limit")
                    with temporary.open("xb") as output:
                        os.chmod(temporary, 0o600)
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > max_bytes:
                                raise ValueError(f"provider file exceeds the {max_bytes}-byte download limit")
                            output.write(chunk)
                            digest.update(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    if total == 0:
                        raise RuntimeError("provider returned an empty file")
                    os.replace(temporary, destination)
                    return {
                        "status": response.status_code,
                        "content_type": response.headers.get("content-type", "application/octet-stream"),
                        "byte_length": total,
                        "sha256": digest.hexdigest(),
                    }
        finally:
            temporary.unlink(missing_ok=True)
