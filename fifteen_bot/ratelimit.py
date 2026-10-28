"""Client-side rate limiting, so the bot stays under Public's API limit (10 requests/second).

Every request the SDK makes (logins, token refreshes, retries included) goes through
one httpx client; `RateLimitedTransport` makes each of them wait for a slot first.
It also notices when Public starts refusing the key, since it sees every response.
"""

import asyncio
import heapq
import itertools
import logging
import time
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

import httpx

logger = logging.getLogger(__name__)

# Below Public's 10/s so network jitter can't bunch requests over the limit on arrival.
MAX_REQUESTS_PER_SECOND = 8

_urgent: ContextVar[bool] = ContextVar("urgent", default=False)


@contextmanager
def urgent() -> Iterator[None]:
    """Requests made inside this block go ahead of queued routine ones (orders, cancels)."""
    token = _urgent.set(True)
    try:
        yield
    finally:
        _urgent.reset(token)


class RateLimiter:
    """At most `max_requests` in any rolling `period`, spaced evenly; urgent callers go first.

    Even spacing matters: a burst of requests granted at the same instant can be
    delayed by different amounts on the way, then land on the server together
    with the next burst and exceed its limit.
    """

    def __init__(self, max_requests: int, period: float = 1.0) -> None:
        self.max_requests = max_requests
        self.period = period
        self.spacing = period / max_requests
        self._sent: deque[float] = deque()
        self._waiting: list[tuple[int, int, asyncio.Future[None]]] = []
        self._order = itertools.count()
        self._timer: Optional[asyncio.TimerHandle] = None
        self._paused_until = 0.0

    async def acquire(self) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiting, (0 if _urgent.get() else 1, next(self._order), future))
        self._release()
        await future  # a cancelled waiter is skipped by _release

    def pause(self, seconds: float) -> None:
        """Hold every request for `seconds`, e.g. after Public answers 429."""
        self._paused_until = max(self._paused_until, time.monotonic() + seconds)

    def _release(self) -> None:
        now = time.monotonic()
        if now < self._paused_until:
            return self._wake_at(self._paused_until - now)
        while self._sent and now - self._sent[0] >= self.period:
            self._sent.popleft()
        while self._waiting and self._waiting[0][2].done():  # cancelled while queued
            heapq.heappop(self._waiting)
        if not self._waiting:
            return
        wait = 0.0
        if self._sent:
            wait = self.spacing - (now - self._sent[-1])
            if len(self._sent) >= self.max_requests:
                wait = max(wait, self.period - (now - self._sent[0]))
        if wait > 0:
            return self._wake_at(wait)
        *_, future = heapq.heappop(self._waiting)
        future.set_result(None)
        self._sent.append(now)
        if self._waiting:
            self._wake_at(self.spacing)

    def _wake_at(self, delay: float) -> None:
        if self._timer is None:
            self._timer = asyncio.get_running_loop().call_later(delay, self._on_timer)

    def _on_timer(self) -> None:
        self._timer = None
        self._release()


class RateLimitedTransport(httpx.AsyncBaseTransport):
    def __init__(self, limiter: RateLimiter, transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
        self.limiter = limiter
        self._transport = transport or httpx.AsyncHTTPTransport()
        self.refused_since: Optional[float] = None  # monotonic; set while Public answers 401

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await self.limiter.acquire()
        response = await self._transport.handle_async_request(request)
        if response.status_code == 429:
            # The SDK retries this request itself; stop everything else from piling on meanwhile.
            delay = _retry_after(response.headers.get("Retry-After"))
            logger.warning("Public rate limit hit (429); pausing requests for %.1fs", delay)
            self.limiter.pause(delay)
        if response.status_code == 401:
            if self.refused_since is None:
                self.refused_since = time.monotonic()
        elif response.status_code < 400:
            self.refused_since = None
        return response

    async def aclose(self) -> None:
        await self._transport.aclose()


def _retry_after(header: Optional[str]) -> float:
    try:
        return min(max(float(header), 0.5), 60.0) if header else 1.0
    except ValueError:
        return 1.0
