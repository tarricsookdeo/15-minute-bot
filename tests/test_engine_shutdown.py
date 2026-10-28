import asyncio
from types import SimpleNamespace

from fifteen_bot.engine import Engine
from fifteen_bot.store import Store


class FakeClient:
    def __init__(self):
        self.cancelled = []
        self.closed = False

    async def cancel_order(self, order_id, account_id):
        self.cancelled.append((order_id, account_id))

    async def close(self):
        self.closed = True


def add_trade(store, series, order_status, order_id):
    store.insert_trade(
        series=series, event_symbol=f"KALSHI.{series}-W1", side="YES", contract_symbol="c", quantity="1",
        limit_price="0.85", order_id=order_id, order_status=order_status, close_time="2030-01-01T00:00:00+00:00",
    )


def test_session_end_cancels_only_unfilled_orders_then_disconnects():
    engine = Engine(Store())
    client = FakeClient()
    engine.client, engine.account_id = client, "ACC1"
    add_trade(engine.store, "A", "NEW", "open-1")
    add_trade(engine.store, "B", "PARTIALLY_FILLED", "open-2")
    add_trade(engine.store, "C", "FILLED", "done-1")

    asyncio.run(engine.shutdown())

    assert sorted(client.cancelled) == [("open-1", "ACC1"), ("open-2", "ACC1")]
    assert client.closed and engine.client is None and engine.account_id is None


def test_resume_restores_the_account_and_running_state():
    engine = Engine(Store())
    engine.store.set_setting("account_id", "ACC2")
    engine.store.set_setting("running", "1")

    async def connect(api_key):
        engine.client = FakeClient()
        engine.accounts = [SimpleNamespace(account_id="ACC1"), SimpleNamespace(account_id="ACC2")]

    engine.connect = connect
    asyncio.run(engine.resume("key"))
    assert engine.account_id == "ACC2" and engine.running
    assert "Resumed after a server restart, still trading" in engine.log[-1]["message"]


def test_resume_stays_stopped_if_it_was_stopped():
    engine = Engine(Store())
    engine.store.set_setting("account_id", "ACC1")
    engine.store.set_setting("running", "0")

    async def connect(api_key):
        engine.client = FakeClient()
        engine.accounts = [SimpleNamespace(account_id="ACC1")]

    engine.connect = connect
    asyncio.run(engine.resume("key"))
    assert engine.account_id == "ACC1" and not engine.running
