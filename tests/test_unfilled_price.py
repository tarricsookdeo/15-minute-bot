"""Public reports an average price of 0 for an order that hasn't filled; the bot shows its limit price instead."""

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from public_api_sdk import OrderStatus

from fifteen_bot.engine import Engine, _position, _trade_summary
from fifteen_bot.store import Store
from tests.test_exits import FakeClient, FakeOrder

D = Decimal
CLOSE = datetime.now(timezone.utc) + timedelta(minutes=5)


def setup():
    engine = Engine(Store())
    engine.client, engine.account_id = FakeClient(), "ACC1"
    trade_id = engine.store.insert_trade(
        series="KX", event_symbol="W1", side="NO", contract_symbol="W1.N", quantity="2", limit_price="0.86",
        est_fees="0.04", order_id="buy-1", order_status="NEW", close_time=CLOSE.isoformat(),
    )
    return engine, trade_id


def track(engine, status, filled, average):
    engine.client.orders["buy-1"] = FakeOrder("buy-1", status, D(filled), D(average))
    asyncio.run(engine._track_trades())
    return engine.store.trades()[0]


def test_an_unfilled_order_shows_its_limit_price_not_publics_zero():
    engine, _ = setup()
    trade = track(engine, OrderStatus.NEW, "0", "0.00")  # what Public returns while it rests
    assert trade["avg_price"] is None
    assert _trade_summary(trade)["price"] == D("0.86") and _position(trade, None)["avg_price"] == D("0.86")

    trade = track(engine, OrderStatus.CANCELLED, "0", "0.00")  # cancelled when the window closed
    assert trade["avg_price"] is None and trade["result"] == "UNFILLED"


def test_the_fill_price_is_recorded_once_it_fills():
    engine, _ = setup()
    trade = track(engine, OrderStatus.FILLED, "2", "0.84")
    assert trade["avg_price"] == "0.84" and _trade_summary(trade)["price"] == D("0.84")


def test_trades_saved_with_a_zero_price_by_earlier_versions_show_the_limit():
    engine, trade_id = setup()
    engine.store.update_trade(trade_id, avg_price="0.00")
    assert _trade_summary(engine.store.trades()[0])["price"] == D("0.86")
