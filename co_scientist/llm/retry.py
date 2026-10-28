"""Retry semantics for both kinds of backend.

There are two failure domains in this project and they want opposite backoff:

**HTTP** (`anthropic_client.py`, `openai_client.py`) — failures arrive as SDK
exceptions carrying a status code and often a `Retry-After` header. A metered
API rate-limits per minute, so seconds of backoff suffice. `RetryPolicy` +
`with_retry` cover this half; 4xx other than 429 never retries.

**Subprocess** (`cli_backend/`) — there is no HTTP client in the request path,
so failures are classified from exit codes and message text. A subscription
rate-limits over a multi-hour window, so retrying in seconds just burns
attempts: `CliRetryPolicy` + `backoff_seconds` start rate-limit backoff in the
tens of seconds and let it grow into minutes.

The Anthropic SDK is imported defensively so a CLI-only install (no
`anthropic`, no metered key anywhere) can still import this module. When the
SDK is absent the HTTP exception names alias to a class that is never raised,
which makes `with_retry` a transparent passthrough — correct, because in that
environment nothing puts an HTTP call in the path to begin with.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import TypeVar

from ..obs.activity import retry_sleep

try:
    import httpx
    from anthropic import (
        APIConnectionError,
        APIStatusError,
        APITimeoutError,
        AuthenticationError,
        BadRequestError,
        InternalServerError,
        PermissionDeniedError,
        RateLimitError,
    )
except ImportError:                                      # pragma: no cover
    class _NeverRaised(Exception):
        """Placeholder so the `except` clauses below stay well-formed."""

    APIConnectionError = APIStatusError = APITimeoutError = _NeverRaised
    AuthenticationError = BadRequestError = InternalServerError = _NeverRaised
    PermissionDeniedError = RateLimitError = _NeverRaised

    class _HttpxShim:
        TimeoutException = NetworkError = _NeverRaised

    httpx = _HttpxShim()                                  # type: ignore[assignment]

T = TypeVar("T")


# --------------------------------------------------------------------------- #
# HTTP backends: status-coded SDK exceptions
#
# - 429: respect Retry-After, exp backoff
# - 529 (overloaded): respect Retry-After, longer backoff
# - 5xx, timeouts: standard exp backoff with jitter
# - 4xx (except 429): never retry — propagate


@dataclass
class RetryPolicy:
    max_attempts_429: int = 6
    max_attempts_529: int = 8
    max_attempts_5xx: int = 5
    max_attempts_timeout: int = 3
    # Total cap across all error classes. Without this, a flapping connection
    # that cycles 429 → 529 → 5xx → timeout can retry up to
    # (6+8+5+3) = 22 times before any per-class counter trips.
    max_attempts_total: int = 12
    base_ms: int = 1000
    cap_ms: int = 60_000


class RetryExhausted(RuntimeError):
    def __init__(self, last_error: BaseException, attempts: int):
        super().__init__(f"retry exhausted after {attempts} attempts: {last_error!r}")
        self.last_error = last_error
        self.attempts = attempts


def _retry_after_seconds(err: APIStatusError) -> float | None:
    headers = getattr(getattr(err, "response", None), "headers", None)
    if not headers:
        return None
    ra = headers.get("retry-after") or headers.get("Retry-After")
    if ra is None:
        return None
    try:
        return float(ra)
    except (TypeError, ValueError):
        pass
    # RFC 7231 also allows HTTP-date format.
    try:
        from datetime import UTC, datetime
        when = parsedate_to_datetime(ra)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        delta = (when - datetime.now(UTC)).total_seconds()
        return max(0.0, delta) if delta < 3600 else None
    except (TypeError, ValueError):
        return None


def _backoff_ms(base_ms: int, cap_ms: int, attempt: int, *, full_jitter: bool = True) -> int:
    exp = min(cap_ms, base_ms * (2**attempt))
    return random.randint(0, exp) if full_jitter else exp


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy,
) -> T:
    """Run `fn` with the configured retry policy. Never retries 4xx (except 429)."""

    attempt_429 = 0
    attempt_529 = 0
    attempt_5xx = 0
    attempt_timeout = 0
    attempt_total = 0

    while True:
        try:
            return await fn()

        # 4xx auth / bad request: never retry
        except (AuthenticationError, PermissionDeniedError, BadRequestError):
            raise

        except RateLimitError as e:
            attempt_429 += 1
            attempt_total += 1
            if attempt_429 >= policy.max_attempts_429 or attempt_total >= policy.max_attempts_total:
                raise RetryExhausted(e, attempt_total) from e
            ra = _retry_after_seconds(e)
            delay_s = ra if ra is not None else _backoff_ms(policy.base_ms, policy.cap_ms, attempt_429) / 1000
            await retry_sleep(delay_s, attempt_total, e, sleep=asyncio.sleep)

        except APIStatusError as e:
            status = getattr(e, "status_code", None) or getattr(
                getattr(e, "response", None), "status_code", None
            )
            if status == 529:
                attempt_529 += 1
                attempt_total += 1
                if attempt_529 >= policy.max_attempts_529 or attempt_total >= policy.max_attempts_total:
                    raise RetryExhausted(e, attempt_total) from e
                ra = _retry_after_seconds(e)
                delay_s = (
                    ra if ra is not None else _backoff_ms(policy.base_ms * 2, policy.cap_ms * 2, attempt_529) / 1000
                )
                await retry_sleep(delay_s, attempt_total, e, sleep=asyncio.sleep)
            elif status is not None and 500 <= status < 600:
                attempt_5xx += 1
                attempt_total += 1
                if attempt_5xx >= policy.max_attempts_5xx or attempt_total >= policy.max_attempts_total:
                    raise RetryExhausted(e, attempt_total) from e
                delay_s = _backoff_ms(policy.base_ms // 2 or 250, policy.cap_ms // 2, attempt_5xx) / 1000
                await retry_sleep(delay_s, attempt_total, e, sleep=asyncio.sleep)
            else:
                # 4xx other than 429: do not retry
                raise

        except InternalServerError as e:
            attempt_5xx += 1
            attempt_total += 1
            if attempt_5xx >= policy.max_attempts_5xx or attempt_total >= policy.max_attempts_total:
                raise RetryExhausted(e, attempt_total) from e
            delay_s = _backoff_ms(policy.base_ms // 2 or 250, policy.cap_ms // 2, attempt_5xx) / 1000
            await retry_sleep(delay_s, attempt_total, e, sleep=asyncio.sleep)

        except (APITimeoutError, APIConnectionError, httpx.TimeoutException, httpx.NetworkError) as e:
            attempt_timeout += 1
            attempt_total += 1
            if attempt_timeout >= policy.max_attempts_timeout or attempt_total >= policy.max_attempts_total:
                raise RetryExhausted(e, attempt_total) from e
            delay_s = _backoff_ms(policy.base_ms, policy.cap_ms // 4, attempt_timeout) / 1000
            await retry_sleep(delay_s, attempt_total, e, sleep=asyncio.sleep)


# --------------------------------------------------------------------------- #
# CLI backends: exit codes and message text


# Message fragments meaning "back off and try again", not "this was malformed".
RATE_LIMIT_MARKERS: tuple[str, ...] = (
    "rate limit",
    "rate_limit",
    "usage limit",
    "quota exceeded",
    "too many requests",
    "overloaded",
    "429",
    "529",
)

TRANSIENT_MARKERS: tuple[str, ...] = (
    "connection error",
    "network error",
    "socket hang up",
    "econnreset",
    "etimedout",
    "fetch failed",
    "internal server error",
    "timed out",
    "503",
    "502",
)

# Floor/ceiling applied to rate-limit backoff, overriding the ordinary knobs.
RATE_LIMIT_BASE_MS = 30_000
RATE_LIMIT_CAP_MS = 600_000


class CliBackendError(RuntimeError):
    """The CLI ran but could not produce a usable answer."""


class CliRetryableError(CliBackendError):
    """Transient failure — worth retrying after a backoff."""


@dataclass
class CliRetryPolicy:
    """Retry knobs for subprocess backends, sourced from `[retry]` in config.

    Deliberately flatter than `RetryPolicy`: a subprocess failure has no status
    code to classify by, so there are no per-class attempt counters to keep.
    """

    max_attempts: int = 6
    base_ms: int = 1_000
    cap_ms: int = 60_000

    @classmethod
    def from_config(cls, cfg) -> CliRetryPolicy:
        return cls(
            max_attempts=max(1, cfg.retry.max_attempts_429),
            base_ms=cfg.retry.base_ms,
            cap_ms=cfg.retry.cap_ms,
        )


def is_rate_limit(message: str) -> bool:
    return _matches(message, RATE_LIMIT_MARKERS)


def is_transient(message: str) -> bool:
    return _matches(message, RATE_LIMIT_MARKERS) or _matches(message, TRANSIENT_MARKERS)


def classify_failure(message: str) -> CliBackendError:
    """Map an error string onto retryable vs terminal."""
    if is_transient(message):
        return CliRetryableError(message)
    return CliBackendError(message)


def backoff_seconds(
    attempt: int, message: str, policy: CliRetryPolicy, *, jitter: float | None = None
) -> float:
    """Exponential backoff with jitter, widened for subscription rate limits.

    `attempt` is 1-based. `jitter` is injectable so tests can be deterministic.
    """
    base_ms, cap_ms = policy.base_ms, policy.cap_ms
    if is_rate_limit(message):
        base_ms = max(base_ms, RATE_LIMIT_BASE_MS)
        cap_ms = max(cap_ms, RATE_LIMIT_CAP_MS)
    delay_ms = min(cap_ms, base_ms * (2 ** max(0, attempt - 1)))
    factor = jitter if jitter is not None else (0.5 + random.random())
    return (delay_ms / 1000.0) * factor


def _matches(text: str, markers: tuple[str, ...]) -> bool:
    low = text.lower()
    return any(m in low for m in markers)
