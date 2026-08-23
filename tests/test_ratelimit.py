import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from nango_mcp.nango import NangoClient
from nango_mcp.ratelimit import (
    EnvironmentConcurrencyGate,
    NangoRateLimitError,
    classify_rate_limit,
    current_environment,
    next_delay,
    parse_retry_after,
    should_retry,
)

NANGO_LIMITER_BODY = {"error": {"code": "too_many_request", "method": "GET", "path": "/proxy/x"}}
PROVIDER_BODY = {"err": "rate limit exceeded", "retryAfter": 30}


def _client(handler, **kwargs) -> NangoClient:
    return NangoClient("https://nango.test", transport=httpx.MockTransport(handler), **kwargs)


@pytest.fixture
def no_backoff(monkeypatch):
    """Collapse retry delays. Must not patch asyncio.sleep - other tests need it to yield."""
    monkeypatch.setattr("nango_mcp.nango.next_delay", lambda **_: 0.0)


def test_classify_distinguishes_gateway_from_provider() -> None:
    # The body is the only reliable signal: both paths set X-RateLimit-* headers.
    assert classify_rate_limit(NANGO_LIMITER_BODY) == "nango"
    assert classify_rate_limit(PROVIDER_BODY) == "provider"
    assert classify_rate_limit(None) == "provider"
    assert classify_rate_limit({"error": {"code": "something_else"}}) == "provider"


def test_retry_after_parses_both_forms_and_rejects_garbage() -> None:
    assert parse_retry_after("30") == 30.0
    assert parse_retry_after("0") == 0.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("not-a-date") is None
    # HTTP-date form, which some providers use instead of delta-seconds
    future = parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT")
    assert future is not None and future > 0


def test_zero_retry_after_is_floored_not_honoured() -> None:
    """Nango can legitimately send Retry-After: 0; sleeping that long hot-loops."""
    delay = next_delay(attempt=1, retry_after=0.0, base_seconds=0.0, ceiling_seconds=30.0)
    assert delay >= 1.0


def test_backoff_never_undercuts_the_server() -> None:
    # Additive jitter only, so the delay is never shorter than Retry-After asked for.
    for _ in range(20):
        assert next_delay(attempt=1, retry_after=7.0, base_seconds=1.0, ceiling_seconds=30.0) >= 7.0


def test_ceiling_bounds_backoff_but_never_retry_after() -> None:
    """The ceiling caps our exponential growth; it must not clamp the server's ask.

    Clamping produced a delay shorter than Retry-After, so the retry was guaranteed to
    be rejected again and the wait budget was spent on calls that could not succeed.
    """
    for retry_after in (45.0, 120.0):
        delay = next_delay(
            attempt=1, retry_after=retry_after, base_seconds=1.0, ceiling_seconds=30.0
        )
        assert delay >= retry_after, f"undercut a {retry_after}s Retry-After"

    # With no Retry-After the ceiling still bounds runaway exponential growth.
    capped = next_delay(attempt=10, retry_after=None, base_seconds=1.0, ceiling_seconds=30.0)
    assert 30.0 <= capped <= 30.5


def test_only_idempotent_provider_calls_are_replayed() -> None:
    # A gateway rejection never reached the provider, so replay is safe for any method.
    assert should_retry("nango", "POST") is True
    assert should_retry("nango", "DELETE") is True
    # A provider rejection may have partially applied.
    assert should_retry("provider", "GET") is True
    assert should_retry("provider", "POST") is False


@pytest.mark.asyncio
async def test_gateway_429_is_retried_then_raises_naming_environment_and_provider(no_backoff) -> None:
    attempts = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(429, json=NANGO_LIMITER_BODY, headers={"retry-after": "0"})

    client = _client(handler, rate_limit_max_attempts=3)
    token = current_environment.set("sandbox")
    try:
        # The raising contract, used by _request and download_provider_file.
        # proxy_request converts this into an envelope instead - covered separately.
        with pytest.raises(NangoRateLimitError) as excinfo:
            await client._send_with_retry(
                "sk", "GET", "/proxy/api/v2/task/1", provider_config_key="clickup"
            )
    finally:
        current_environment.reset(token)

    err = excinfo.value
    assert err.scope == "nango"
    assert err.environment == "sandbox"
    assert err.provider_config_key == "clickup"
    assert attempts["n"] == 3, "should retry up to the attempt budget"
    assert "sandbox" in str(err) and "clickup" in str(err)


@pytest.mark.asyncio
async def test_proxy_request_returns_an_envelope_rather_than_raising(no_backoff) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json=NANGO_LIMITER_BODY, headers={"retry-after": "12"})

    client = _client(handler, rate_limit_max_attempts=2)
    token = current_environment.set("sandbox")
    try:
        result = await client.proxy_request("sk", "clickup", "conn", "GET", "/api/v2/task/1")
    finally:
        current_environment.reset(token)

    # The non-raising contract is deliberate: the agent should see the failure.
    assert result["ok"] is False
    assert result["status"] == 429
    assert result["rate_limit"]["scope"] == "nango"
    assert result["rate_limit"]["environment"] == "sandbox"
    assert "sandbox" in result["rate_limit"]["message"]


@pytest.mark.asyncio
async def test_provider_429_is_not_replayed_for_writes(no_backoff) -> None:
    attempts = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(429, json=PROVIDER_BODY)

    client = _client(handler, rate_limit_max_attempts=4)
    result = await client.proxy_request("sk", "clickup", "conn", "POST", "/api/v2/task/1/comment")
    assert result["rate_limit"]["scope"] == "provider"
    assert attempts["n"] == 1, "a write must not be replayed against a provider 429"


@pytest.mark.asyncio
async def test_success_after_a_transient_429(no_backoff) -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json=NANGO_LIMITER_BODY, headers={"retry-after": "0"})
        return httpx.Response(200, json={"ok": True})

    client = _client(handler, rate_limit_max_attempts=3)
    result = await client.proxy_request("sk", "clickup", "conn", "GET", "/api/v2/task/1")
    assert result["ok"] is True
    assert result["status"] == 200
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_gate_caps_one_environment_without_blocking_another() -> None:
    gate = EnvironmentConcurrencyGate(2, 5.0)
    peak = {"a": 0}
    live = {"a": 0}
    started_b = asyncio.Event()

    async def slow_a() -> None:
        async with gate.acquire("environment-a"):
            live["a"] += 1
            peak["a"] = max(peak["a"], live["a"])
            await asyncio.sleep(0)
            live["a"] -= 1

    async def quick_b() -> None:
        async with gate.acquire("environment-b"):
            started_b.set()

    await asyncio.gather(*[slow_a() for _ in range(6)], quick_b())
    assert peak["a"] <= 2
    assert started_b.is_set()
