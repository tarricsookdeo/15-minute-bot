"""Public being slow or unreachable: said plainly, rarely, and retried rather than given up on."""

import asyncio
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from public_api_sdk import APIError

from fifteen_bot.engine import CONNECTION_LOG_SECONDS, Engine, Window
from fifteen_bot.store import Store
from fifteen_bot.strategy import Pick, Strategy

D = Decimal


def sdk_error(cause: Exception) -> APIError:
    """How the SDK reports a request that never got an answer: no message, status 0."""
    try:
        try:
            raise cause
        except httpx.HTTPError as exc:
            raise APIError(str(exc), 0, {}) from exc
    except APIError as wrapped:
        return wrapped


async def fails(exc):
    raise exc


def guarded(engine, exc, series=None):
    asyncio.run(engine._guarded(series, fails(exc)))


def test_a_timeout_says_what_happened_once_a_minute():
    engine = Engine(Store())
    for _ in range(5):  # a tick every couple of seconds while Public is slow
        guarded(engine, sdk_error(httpx.ReadTimeout("")))
    assert [(e["level"], e["message"]) for e in engine.log] == [("warn", "Public took too long to answer; retrying")]

    engine._trouble_logged_at -= CONNECTION_LOG_SECONDS  # still going a minute later
    guarded(engine, sdk_error(httpx.ReadTimeout("")))
    assert len(engine.log) == 2


def test_an_unreachable_public_is_named_too():
    engine = Engine(Store())
    guarded(engine, sdk_error(httpx.ConnectError("connection refused")))
    assert engine.log[0]["message"] == "Couldn't reach Public; retrying"


def test_other_errors_are_reported_as_before():
    engine = Engine(Store())
    guarded(engine, APIError("Insufficient buying power", 400), series="KX")
    assert [(e["level"], e["series"], e["message"]) for e in engine.log] == [
        ("error", "KX", "API Error 400: Insufficient buying power")
    ]


class FakeClient:
    def __init__(self, preflight_error=None, buy_error=None):
        self.preflight_error, self.buy_error = preflight_error, buy_error
        self.bought = 0

    async def preflight_buy_event_contract(self, symbol, quantity, limit_price, account_id):
        if self.preflight_error:
            raise self.preflight_error
        return SimpleNamespace(order_value=quantity * limit_price, estimated_cost=None)

    async def buy_event_contract(self, symbol, quantity, limit_price, order_id, account_id):
        if self.buy_error:
            raise self.buy_error
        self.bought += 1


def enter(engine):
    window = Window(
        event_symbol="W1", title="", resolution_time=datetime.now(timezone.utc) + timedelta(minutes=1),
        tradable=True, priced_at=time.monotonic(),
    )
    asyncio.run(engine._enter("KX", Strategy(), window, Pick("YES", "W1.Y", D("0.85")), 60))
    return window


@pytest.fixture
def engine():
    engine = Engine(Store())
    engine.account_id = "ACC1"
    return engine


def test_a_preflight_that_times_out_is_tried_again_next_tick(engine):
    engine.client = FakeClient(preflight_error=sdk_error(httpx.ReadTimeout("")))
    window = enter(engine)
    assert engine.store.trades() == [] and window.note == "Public took too long to answer; trying again"

    engine.client.preflight_error = None
    enter(engine)
    assert engine.client.bought == 1 and engine.store.trades()[0]["order_status"] == "NEW"


def test_a_buy_that_times_out_says_so(engine):
    engine.client = FakeClient(buy_error=sdk_error(httpx.ReadTimeout("")))
    enter(engine)
    [trade] = engine.store.trades()
    assert trade["order_status"] == "SUBMIT_ERROR" and trade["note"] == "Public took too long to answer"
