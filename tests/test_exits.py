"""Selling early: take-profit and stop-loss, re-pricing a sell that doesn't fill, and the P/L of a sold trade."""

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from public_api_sdk import OrderSide, OrderStatus

from fifteen_bot.engine import EXIT_RETRY_SECONDS, Engine, Window
from fifteen_bot.store import Store
from fifteen_bot.strategy import Strategy

D = Decimal
EVENT = "KALSHI.KX-W1"
CLOSE = datetime.now(timezone.utc) + timedelta(minutes=1)


@dataclass
class FakeOrder:
    order_id: str
    status: OrderStatus
    filled_quantity: Decimal
    average_price: Optional[Decimal] = None
    reject_reason: Optional[str] = None


class FakeClient:
    def __init__(self):
        self.placed = []
        self.cancelled = []
        self.orders = {}

    async def place_order(self, request, account_id):
        self.placed.append(request)

    async def cancel_order(self, order_id, account_id):
        self.cancelled.append(order_id)

    async def search_orders(self, request, account_id):
        return list(self.orders.values())

    async def close(self):
        pass


def setup(bid="0.95", order_status="FILLED", running=True, **targets):
    """5 YES bought @ 0.87 with 0.05 fees: 0.88 a contract, and an estimated 0.01 a contract to sell."""
    engine = Engine(Store())
    engine.client, engine.account_id, engine.running = FakeClient(), "ACC1", running
    store = engine.store
    store.add_market("KX", "BTC", None)
    targets = targets or {"take_profit": "0.05", "stop_loss": "0.10"}
    store.update_market("KX", enabled=True, strategy=Strategy(**targets))
    window = Window(
        event_symbol=EVENT, title="BTC", resolution_time=CLOSE, yes_bid=D(bid), tradable=True, priced_at=time.monotonic()
    )
    trade_id = store.insert_trade(
        series="KX", event_symbol=EVENT, side="YES", contract_symbol=f"{EVENT}.Y", quantity="5", limit_price="0.87",
        filled_quantity="5", avg_price="0.87", est_fees="0.05", order_id="buy-1", order_status=order_status,
        close_time=CLOSE.isoformat(),
    )
    return engine, store.get_market("KX"), window, trade_id


def manage(engine, market, window):
    asyncio.run(engine._manage_exit(market, window))


def test_take_profit_sells_the_position_at_the_bid():
    engine, market, window, trade_id = setup(bid="0.94")  # 0.94 - 0.01 - 0.88 = +5c
    manage(engine, market, window)

    [order] = engine.client.placed
    assert order.order_side == OrderSide.SELL and order.quantity == D("5") and order.limit_price == D("0.94")
    [exit] = engine.store.exits_for(trade_id)
    assert exit["reason"] == "TAKE_PROFIT" and exit["order_status"] == "NEW" and exit["order_id"] == order.order_id
    assert engine.store.trades()[0]["exit_reason"] == "TAKE_PROFIT"


def test_stop_loss_sells_the_position():
    engine, market, window, trade_id = setup(bid="0.77")  # 0.77 - 0.01 - 0.88 = -12c
    manage(engine, market, window)
    assert engine.store.exits_for(trade_id)[0]["reason"] == "STOP_LOSS"


def test_holds_between_the_targets_without_targets_or_while_stopped():
    for engine, market, window, _ in (
        setup(bid="0.93"),  # +4c: short of the take-profit
        setup(bid="0.99", take_profit=None, stop_loss=None),
        setup(bid="0.99", running=False),
    ):
        manage(engine, market, window)
        assert engine.client.placed == []


def test_open_buy_order_is_cancelled_before_selling():
    engine, market, window, trade_id = setup(order_status="PARTIALLY_FILLED")
    manage(engine, market, window)
    assert engine.client.cancelled == ["buy-1"] and engine.client.placed == []


def test_unfilled_sell_is_cancelled_after_a_few_seconds_then_re_priced():
    engine, market, window, trade_id = setup(bid="0.95")
    manage(engine, market, window)
    [exit] = engine.store.exits_for(trade_id)

    manage(engine, market, window)  # still young: left alone
    assert engine.client.cancelled == [] and len(engine.client.placed) == 1

    old = (datetime.now(timezone.utc) - timedelta(seconds=EXIT_RETRY_SECONDS + 1)).isoformat()
    engine.store.update_exit(exit["id"], placed_at=old)
    manage(engine, market, window)
    assert engine.client.cancelled == [exit["order_id"]]

    engine.store.update_exit(exit["id"], order_status="CANCELLED")  # as order tracking would record
    window.yes_bid = D("0.96")
    manage(engine, market, window)
    assert [o.limit_price for o in engine.client.placed] == [D("0.95"), D("0.96")]


def test_rejected_sell_is_not_retried():
    engine, market, window, trade_id = setup()
    manage(engine, market, window)
    engine.store.update_exit(engine.store.exits_for(trade_id)[0]["id"], order_status="REJECTED")
    manage(engine, market, window)
    assert len(engine.client.placed) == 1


def test_filled_sell_finalises_the_trade_as_sold():
    engine, market, window, trade_id = setup(bid="0.95")
    manage(engine, market, window)
    order_id = engine.client.placed[0].order_id
    engine.client.orders[order_id] = FakeOrder(order_id, OrderStatus.FILLED, D("5"), D("0.95"))

    asyncio.run(engine._track_trades())  # records the fill
    asyncio.run(engine._track_trades())  # sees everything sold: final without waiting for settlement

    trade = engine.store.trades()[0]
    assert D(trade["sold_quantity"]) == D("5")
    # 5 × 0.95 = 4.75, less 0.05 selling fees, less 5 × 0.87 + 0.05 buying cost
    assert trade["result"] == "SOLD" and D(trade["pnl"]) == D("0.30")
    stats = engine.store.stats()["KX"]
    assert (stats["wins"], stats["losses"]) == (1, 0)


def test_session_end_cancels_an_open_sell():
    engine, market, window, trade_id = setup()
    manage(engine, market, window)
    client = engine.client
    asyncio.run(engine.shutdown())
    assert client.cancelled == [client.placed[0].order_id]


def test_a_sell_is_saved_before_it_is_sent():
    engine, market, window, trade_id = setup()
    sent_before_saving = []

    async def checkpoint():
        sent_before_saving.append(bool(engine.client.placed))

    engine.checkpoint = checkpoint
    manage(engine, market, window)
    assert sent_before_saving == [False] and len(engine.client.placed) == 1


def test_a_sell_that_cannot_be_saved_is_not_sent_and_is_retried_later():
    engine, market, window, trade_id = setup()

    async def database_down():
        raise ConnectionError("database unreachable")

    engine.checkpoint = database_down
    manage(engine, market, window)
    assert engine.client.placed == [] and engine.store.exits_for(trade_id)[0]["order_status"] == "NOT_SENT"

    async def database_back():
        pass

    engine.checkpoint = database_back
    manage(engine, market, window)
    assert len(engine.client.placed) == 1
