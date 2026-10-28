import asyncio
import time

import httpx

from fifteen_bot.ratelimit import RateLimitedTransport, RateLimiter, urgent

PERIOD = 0.2  # scaled-down second so the tests run fast


def max_in_any_window(times, period):
    return max(sum(1 for u in times if t <= u < t + period) for t in times)


def test_never_exceeds_the_limit_in_any_rolling_window():
    async def run():
        limiter = RateLimiter(4, PERIOD)
        times = []

        async def call():
            await limiter.acquire()
            times.append(time.monotonic())

        started = time.monotonic()
        await asyncio.gather(*(call() for _ in range(20)))
        return times, time.monotonic() - started

    times, elapsed = asyncio.run(run())
    assert len(times) == 20
    assert max_in_any_window(times, PERIOD * 0.999) <= 4
    assert elapsed < PERIOD * 6  # 20 calls at 4 per period: ~4 periods, not serialised further


def test_urgent_requests_jump_the_queue():
    async def run():
        limiter = RateLimiter(1, PERIOD)
        order = []

        async def call(name, is_urgent=False):
            if is_urgent:
                with urgent():
                    await limiter.acquire()
            else:
                await limiter.acquire()
            order.append(name)

        await limiter.acquire()  # use up the only slot so everything below queues
        routine = [asyncio.create_task(call(f"quote{i}")) for i in range(3)]
        await asyncio.sleep(0)
        order_task = asyncio.create_task(call("order", is_urgent=True))
        await asyncio.gather(*routine, order_task)
        return order

    assert asyncio.run(run())[0] == "order"


def test_429_pauses_all_requests_for_retry_after():
    calls = []

    def handler(request):
        calls.append(time.monotonic())
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0.5"})
        return httpx.Response(200, json={})

    async def run():
        transport = RateLimitedTransport(RateLimiter(100, PERIOD), httpx.MockTransport(handler))
        async with httpx.AsyncClient(transport=transport) as client:
            first = await client.get("https://example.test/a")
            await client.get("https://example.test/b")
        return first.status_code

    assert asyncio.run(run()) == 429
    assert calls[1] - calls[0] >= 0.45


def test_tracks_how_long_public_has_refused_the_key():
    statuses = iter([200, 401, 401, 200])

    def handler(request):
        return httpx.Response(next(statuses), json={})

    async def run():
        transport = RateLimitedTransport(RateLimiter(100, PERIOD), httpx.MockTransport(handler))
        seen = []
        async with httpx.AsyncClient(transport=transport) as client:
            for _ in range(4):
                await client.get("https://example.test/a")
                seen.append(transport.refused_since)
        return seen

    ok, first_401, second_401, recovered = asyncio.run(run())
    assert ok is None and first_401 is not None and recovered is None
    assert second_401 == first_401  # counts from the first refusal, not the latest


def test_cancelled_waiter_does_not_use_a_slot():
    async def run():
        limiter = RateLimiter(1, PERIOD)
        await limiter.acquire()
        waiter = asyncio.create_task(limiter.acquire())
        await asyncio.sleep(0)
        waiter.cancel()
        started = time.monotonic()
        await limiter.acquire()
        return time.monotonic() - started

    assert asyncio.run(run()) < PERIOD * 1.5
