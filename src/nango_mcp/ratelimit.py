"""
Rate-limit handling for Nango proxy traffic.

Two distinct throttles reach this client and they need different treatment:

* Nango's own gateway limiter rejects the request *before* it ever runs, so retrying
  is free of side effects regardless of HTTP method.
* A provider's limiter rejects a request Nango already forwarded, so a non-idempotent
  method may have partially applied and must not be replayed.

They are told apart by response body, never by headers: Nango's limiter emits exactly
``{"error": {"code": "too_many_request", ...}}`` while a forwarded provider 429 carries
the provider's raw body. Both paths set ``X-RateLimit-*``, so header inspection would
misclassify.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from contextvars import ContextVar
from email.utils import parsedate_to_datetime
from typing import Any, AsyncIterator

NANGO_LIMITER_ERROR_CODE = "too_many_request"

# Nango floors Retry-After and runs with blockDuration 0, so it can legitimately send
# "0". Sleeping for that long is a hot loop, hence a floor rather than trusting the wire.
RETRY_AFTER_FLOOR_SECONDS = 1.0

# Methods safe to replay when the *provider* throttled us. A provider 429 does not
# promise the request had no effect.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Set per tool call so errors can name the environment without threading an argument
# through every method on NangoClient.
current_environment: ContextVar[str | None] = ContextVar("current_environment", default=None)


class NangoAPIError(RuntimeError):
    """
    A non-2xx from Nango on a raising code path.

    Carries the response headers, which the previous bare RuntimeError discarded - so a
    429 on the download path lost its Retry-After entirely.
    """

    def __init__(self, message: str, *, status: int, headers: dict[str, str], body: str) -> None:
        self.status = status
        self.headers = headers
        self.body = body
        super().__init__(message)


class EnvironmentQueueTimeoutError(RuntimeError):
    """An environment's concurrency slots stayed full past the acquire timeout."""

    def __init__(self, environment: str, limit: int, timeout: float) -> None:
        self.environment = environment
        self.limit = limit
        self.timeout = timeout
        super().__init__(
            f"environment '{environment}' has {limit} calls already in flight and did not free a slot "
            f"within {timeout:.0f}s; retry shortly"
        )


class NangoRateLimitError(NangoAPIError):
    """Raised when retries are exhausted against a rate limit."""

    def __init__(
        self,
        *,
        scope: str,
        environment: str | None,
        provider_config_key: str | None,
        status: int,
        headers: dict[str, str] | None = None,
        retry_after_seconds: float | None,
        attempts: int,
        waited_seconds: float,
    ) -> None:
        self.scope = scope
        self.environment = environment
        self.provider_config_key = provider_config_key
        self.status = status
        self.retry_after_seconds = retry_after_seconds
        self.attempts = attempts
        self.waited_seconds = waited_seconds
        super().__init__(self._message(), status=status, headers=headers or {}, body="")

    def _message(self) -> str:
        who = f"environment '{self.environment}'" if self.environment else "this caller"
        where = (
            f"Rate limited by the Nango gateway for {who}"
            if self.scope == "nango"
            else f"Rate limited by provider '{self.provider_config_key or 'unknown'}' for {who}"
        )
        if self.scope == "nango" and self.provider_config_key:
            where += f" (integration '{self.provider_config_key}')"
        retry = (
            f" Retry after ~{self.retry_after_seconds:.0f}s."
            if self.retry_after_seconds is not None
            else " Retry shortly."
        )
        return f"{where}: {self.attempts} attempts over {self.waited_seconds:.1f}s.{retry}"

    @property
    def detail(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "environment": self.environment,
            "provider_config_key": self.provider_config_key,
            "status": self.status,
            "retry_after_seconds": self.retry_after_seconds,
            "attempts": self.attempts,
            "waited_seconds": round(self.waited_seconds, 3),
            "message": str(self),
        }


def classify_rate_limit(body: Any) -> str:
    """``'nango'`` when our own gateway rejected the call, otherwise ``'provider'``."""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("code") == NANGO_LIMITER_ERROR_CODE:
            return "nango"
    return "provider"


def parse_retry_after(value: str | None, *, now: float | None = None) -> float | None:
    """Seconds to wait, from either delta-seconds or an HTTP-date. None if unusable."""
    if not value:
        return None
    text = value.strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    reference = time.time() if now is None else now
    return max(0.0, parsed.timestamp() - reference)


def next_delay(
    *,
    attempt: int,
    retry_after: float | None,
    base_seconds: float,
    ceiling_seconds: float,
) -> float:
    """
    Backoff for `attempt` (1-based).

    Jitter is additive rather than multiplicative so the delay never falls below what
    the server asked for - full jitter would sleep 3s when told 7s and be rejected again.
    """
    # The ceiling bounds our own exponential growth, never Retry-After: clamping the
    # server's instruction sleeps less than we were told and earns a second rejection.
    # When the server asks for longer than the caller's budget, _send_with_retry's
    # `waited + delay <= rate_limit_max_wait` check aborts rather than sleeping short.
    backoff = min(base_seconds * (2 ** (attempt - 1)), ceiling_seconds)
    delay = max(retry_after or 0.0, backoff, RETRY_AFTER_FLOOR_SECONDS)
    return delay + random.uniform(0.0, 0.5)


def should_retry(scope: str, method: str) -> bool:
    # A Nango-limiter rejection never reached the provider, so replaying it is safe
    # even for writes. A provider rejection may have partially applied.
    if scope == "nango":
        return True
    return method.upper() in IDEMPOTENT_METHODS


class EnvironmentConcurrencyGate:
    """
    Caps in-flight calls per environment.

    This is an isolation device, not a rate limiter: at the default cap and typical
    latency it still permits far more throughput than any Nango bucket allows. Its job
    is to stop one environment's fan-out from monopolising the single event loop and the
    shared connection pool while other environments wait.
    """

    def __init__(self, limit: int, acquire_timeout: float) -> None:
        self._limit = max(1, limit)
        self._acquire_timeout = acquire_timeout
        self._gates: dict[str, asyncio.Semaphore] = {}

    def _gate(self, slug: str) -> asyncio.Semaphore:
        # Safe without a lock: creation is synchronous on a single event loop.
        gate = self._gates.get(slug)
        if gate is None:
            gate = self._gates[slug] = asyncio.Semaphore(self._limit)
        return gate

    @contextlib.asynccontextmanager
    async def acquire(self, slug: str | None) -> AsyncIterator[float]:
        if not slug:
            yield 0.0
            return
        gate = self._gate(slug)
        started = time.monotonic()
        try:
            await asyncio.wait_for(gate.acquire(), timeout=self._acquire_timeout)
        except asyncio.TimeoutError as exc:
            raise EnvironmentQueueTimeoutError(slug, self._limit, self._acquire_timeout) from exc
        waited_ms = (time.monotonic() - started) * 1000.0
        try:
            yield waited_ms
        finally:
            gate.release()
