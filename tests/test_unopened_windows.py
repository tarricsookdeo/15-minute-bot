"""A window Public lists before it can return its details: waited for quietly, mentioned only if it drags on."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from public_api_sdk import APIError

from fifteen_bot.engine import UNOPENED_NOTE, UNOPENED_RETRY_SECONDS, UNOPENED_WARN_SECONDS, Engine, Window
from fifteen_bot.store import Store

EVENT = "KALSHI.KXWTI15M-26OCT281630-EVENT"


class FakeClient:
    def __init__(self, error=None):
        self.error = error or APIError(f"No event found with symbol {EVENT}", status_code=400)
        self.calls = 0

    async def get_event_details(self, symbol):
        self.calls += 1
        if self.error:
            raise self.error
        side = lambda s: SimpleNamespace(symbol=s, ask=None, bid=None)  # noqa: E731
        outcome = SimpleNamespace(yes=side("Y"), no=side("N"), close_time=None, is_tradable=True)
        return SimpleNamespace(outcome=outcome, halted=False)


def setup(**client):
    engine = Engine(Store())
    engine.client = FakeClient(**client)
    window = Window(event_symbol=EVENT, title="WTI", resolution_time=datetime.now(timezone.utc) + timedelta(minutes=14))
    engine.windows["KXWTI15M"] = window
    return engine, window


def refresh(engine, window):
    asyncio.run(engine._refresh_details(window))


def test_a_window_public_has_not_opened_yet_is_waited_for_quietly():
    engine, window = setup()
    refresh(engine, window)
    refresh(engine, window)  # too soon to ask again
    assert engine.client.calls == 1 and list(engine.log) == []
    assert window.note == UNOPENED_NOTE and not window.tradable

    window.details_at -= UNOPENED_RETRY_SECONDS  # a few seconds later Public has it
    engine.client.error = None
    refresh(engine, window)
    assert window.tradable and window.yes_symbol == "Y" and window.note is None and window.unopened_since is None
    assert list(engine.log) == []


def test_it_is_mentioned_once_if_it_drags_on_and_again_when_it_opens():
    engine, window = setup()
    refresh(engine, window)
    window.unopened_since -= UNOPENED_WARN_SECONDS
    for _ in range(3):
        window.details_at -= UNOPENED_RETRY_SECONDS
        refresh(engine, window)
    assert [e["level"] for e in engine.log] == ["warn"] and engine.log[0]["series"] == "KXWTI15M"

    window.details_at -= UNOPENED_RETRY_SECONDS
    engine.client.error = None
    refresh(engine, window)
    assert [e["level"] for e in engine.log] == ["warn", "info"]


def test_other_errors_are_still_errors():
    for error in (APIError("No event found with symbol X", status_code=500), APIError("Bad request", status_code=400)):
        engine, window = setup(error=error)
        with pytest.raises(APIError):
            refresh(engine, window)
        assert window.unopened_since is None
