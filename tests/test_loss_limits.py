"""Daily loss limits: a buy that could take the day's loss past a market's or the account's limit is never placed."""

import asyncio
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from fifteen_bot.app import LossLimitBody
from fifteen_bot.engine import Engine, Window, _at_risk, trading_day_start
from fifteen_bot.store import Store, now_iso
from fifteen_bot.strategy import Pick, Strategy, would_pass_limit

D = Decimal
CLOSE = datetime.now(timezone.utc) + timedelta(minutes=1)


class FakeClient:
    """Preflight quotes `fee` on every buy; buys are recorded."""

    def __init__(self, fee="0.10"):
        self.fee = D(fee)
        self.preflights = 0
        self.bought = []

    async def preflight_buy_event_contract(self, symbol, quantity, limit_price, account_id):
        self.preflights += 1
        await asyncio.sleep(0)  # a real request: other markets' buys can run meanwhile
        value = quantity * limit_price
        return SimpleNamespace(order_value=value, estimated_cost=value + self.fee)

    async def buy_event_contract(self, symbol, quantity, limit_price, order_id, account_id):
        self.bought.append(symbol)


def setup(account_limit=None, fee="0.10", **markets):
    """Markets by series, each with its own daily loss limit (None for none)."""
    engine = Engine(Store())
    engine.client, engine.account_id, engine.running = FakeClient(fee), "ACC1", True
    for series, limit in markets.items():
        engine.store.add_market(series, series, None)
        engine.store.update_market(series, enabled=True, strategy=Strategy(quantity=D(5), daily_loss_limit=limit))
    if account_limit is not None:
        engine.set_loss_limit(D(account_limit))
    return engine


def settled(engine, series, pnl, when=None, n=[0]):
    n[0] += 1
    engine.store.insert_trade(
        series=series, event_symbol=f"OLD-{n[0]}", side="YES", contract_symbol="C", quantity="5", limit_price="0.80",
        filled_quantity="5", avg_price="0.80", order_status="FILLED", close_time=now_iso(), result="LOST",
        pnl=str(pnl), settled_at=(when or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
    )


def buy(engine, *series, price="0.50"):
    """Each market tries to buy 5 contracts at `price` at once, as a tick does."""

    async def run():
        window = Window(event_symbol="NOW", title="", resolution_time=CLOSE, tradable=True, priced_at=time.monotonic())
        await asyncio.gather(*(
            engine._enter(s, engine.store.get_market(s)["strategy"], window, Pick("YES", f"{s}.Y", D(price)), 60)
            for s in series
        ))

    asyncio.run(run())
    return engine.client.bought


def test_the_worst_case_counts_losses_open_risk_and_the_new_buy():
    assert not would_pass_limit(None, D(-100), D(100), D(100))
    assert not would_pass_limit(D(10), D(-5), D(2), D(3))  # could lose exactly 10
    assert would_pass_limit(D(10), D(-5), D(2), D("3.01"))
    assert not would_pass_limit(D(10), D(4), D(10), D(4))  # today's gains give room


def test_limits_are_whole_cents_or_off():
    assert Strategy(daily_loss_limit=D("12.50")).daily_loss_limit == D("12.50")
    assert Strategy().daily_loss_limit is None
    assert LossLimitBody(daily_loss_limit=None).daily_loss_limit is None
    for bad in ("0", "0.001", "-5", "1000000.01"):
        with pytest.raises(ValidationError):
            Strategy(daily_loss_limit=D(bad))
        with pytest.raises(ValidationError):
            LossLimitBody(daily_loss_limit=D(bad))


def test_rules_saved_before_loss_limits_existed_load_without_one():
    assert Strategy.model_validate({"entry_seconds": 120}).daily_loss_limit is None


def test_the_trading_day_starts_at_midnight_eastern():
    # 11pm Eastern on Oct 28 (daylight saving time) is still Oct 28's trading day.
    assert trading_day_start(datetime(2026, 10, 29, 3, 0, tzinfo=timezone.utc)) == datetime(2026, 10, 28, 4, 0, tzinfo=timezone.utc)
    # In winter, midnight Eastern is 5am UTC.
    assert trading_day_start(datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)) == datetime(2026, 12, 1, 5, 0, tzinfo=timezone.utc)


def test_what_an_open_trade_could_still_lose():
    trade = dict(quantity="5", limit_price="0.80", est_fees="0.10", sold_value="0", sell_fees="0")
    # Order still open: all 5 could fill at the limit, plus all the fees.
    assert _at_risk(dict(trade, order_status="NEW", filled_quantity="2", avg_price="0.78")) == D("4.10")
    # Filled 2 at 0.78 and done: those 2 and their share of the fees.
    assert _at_risk(dict(trade, order_status="CANCELLED", filled_quantity="2", avg_price="0.78")) == D("1.60")
    # Sold some early: what came back is no longer at risk; a locked-in gain isn't negative risk.
    filled = dict(trade, order_status="FILLED", filled_quantity="5", avg_price="0.80")
    assert _at_risk(dict(filled, sold_value="2.00", sell_fees="0.05")) == D("2.15")
    assert _at_risk(dict(filled, sold_value="5.00")) == D(0)


def test_a_buy_within_the_market_limit_is_placed():
    engine = setup(KX="10")
    settled(engine, "KX", "-7.00")
    assert buy(engine, "KX") == ["KX.Y"]  # 7 + 2.50 + 0.10 fees = 9.60


def test_a_buy_that_could_pass_the_market_limit_stops_that_market_only():
    engine = setup(KX="10", ETH=None)
    settled(engine, "KX", "-8.00")
    assert buy(engine, "KX", "ETH") == ["ETH.Y"]  # KX: 8 + 2.50 = 10.50
    assert engine.store.get_market("KX")["halted_at"] and not engine.store.get_market("ETH")["halted_at"]
    assert engine.store.get_setting("halted_at") is None
    assert engine.client.preflights == 1  # KX was stopped before asking Public


def test_fees_count_too():
    engine = setup(KX="10", fee="0.10")
    settled(engine, "KX", "-7.45")  # 7.45 + 2.50 = 9.95 is fine, but with 0.10 of fees it's 10.05
    assert buy(engine, "KX") == [] and engine.client.preflights == 1
    assert engine.store.get_market("KX")["halted_at"]


def test_open_positions_count_as_lost():
    engine = setup(KX="5")
    engine.store.insert_trade(
        series="KX", event_symbol="EARLIER", side="YES", contract_symbol="C", quantity="5", limit_price="0.50",
        filled_quantity="5", avg_price="0.50", order_status="FILLED", close_time=now_iso(),
    )
    assert buy(engine, "KX") == []  # 2.50 open + 2.50 + fees


def test_losses_before_today_and_gains_today():
    engine = setup(KX="5")
    settled(engine, "KX", "-50.00", when=trading_day_start() - timedelta(minutes=1))
    settled(engine, "KX", "-6.00")
    settled(engine, "KX", "+4.00")
    assert buy(engine, "KX") == ["KX.Y"]  # today: -2 net, + 2.60 = 4.60


def test_the_account_limit_stops_every_market():
    engine = setup(account_limit="10", BTC=None, ETH=None)
    settled(engine, "BTC", "-4.00")
    settled(engine, "ETH", "-4.00")
    assert buy(engine, "ETH") == []  # 8 + 2.60
    assert engine.store.get_setting("halted_at") is not None

    engine.store.update_trade(1, pnl="0")  # even with room again, nothing buys until resumed
    asyncio.run(engine._decide(engine.store.get_market("BTC"), _window_in_range()))
    assert engine.client.bought == []


def test_markets_buying_at_once_cannot_pass_the_account_limit_together():
    engine = setup(account_limit="5", BTC=None, ETH=None)
    assert len(buy(engine, "BTC", "ETH")) == 1  # 2.60 each: only one fits
    assert engine.client.preflights == 2  # both asked Public before either was recorded


def test_resuming_clears_the_stop():
    engine = setup(account_limit="10", KX="10")
    engine.store.halt_market("KX")
    engine.store.set_setting("halted_at", now_iso())
    engine.set_running(True)
    engine.store.update_market("KX", enabled=True)  # switching the market back on
    assert engine.store.get_setting("halted_at") is None and engine.store.get_market("KX")["halted_at"] is None
    engine.store.halt_market("KX")
    engine.store.update_market("KX", strategy=Strategy())  # editing the rule doesn't resume it
    assert engine.store.get_market("KX")["halted_at"]


def test_the_dashboard_shows_today_and_the_limits():
    engine = setup(account_limit="20", KX="10")
    settled(engine, "KX", "-3.00")
    snapshot = engine.snapshot()
    assert snapshot["loss_limit"] == {"limit": D(20), "halted_at": None, "today_pnl": D(-3), "at_risk": D(0)}
    assert snapshot["markets"][0]["today"] == {"pnl": D(-3), "at_risk": D(0)}


def _window_in_range():
    return Window(
        event_symbol="NOW", title="", resolution_time=CLOSE, tradable=True, priced_at=time.monotonic(),
        yes_symbol="Y", no_symbol="N", yes_ask=D("0.85"), no_ask=D("0.16"),
    )


def test_stops_survive_a_restart_and_older_saves_load():
    engine = setup(account_limit="10", KX="5")
    engine.store.halt_market("KX")
    engine.store.set_setting("halted_at", now_iso())
    restored = Store()
    restored.load((table, row) for table, _, row in engine.store.dump())
    assert restored.get_market("KX")["halted_at"] and restored.get_setting("halted_at")

    older = Store()  # a market saved before loss limits existed has no stop marker
    row = {k: v for k, v in next(r for t, _, r in engine.store.dump() if t == "markets").items() if k != "halted_at"}
    older.load([("markets", row)])
    assert older.get_market("KX")["halted_at"] is None


def test_nothing_buys_after_another_market_stopped_every_market():
    engine = setup(account_limit="5", BTC=None, ETH=None)
    settled(engine, "BTC", "-2.00")

    async def run():  # ETH asks Public about its buy; meanwhile BTC's buy would pass the account limit
        window = Window(event_symbol="NOW", title="", resolution_time=CLOSE, tradable=True, priced_at=time.monotonic())
        eth = asyncio.create_task(engine._enter("ETH", Strategy(quantity=D(5)), window, Pick("YES", "ETH.Y", D("0.50")), 60))
        await asyncio.sleep(0)
        await engine._enter("BTC", Strategy(quantity=D(5)), window, Pick("YES", "BTC.Y", D("0.80")), 60)
        await eth

    asyncio.run(run())
    assert engine.store.get_setting("halted_at") is not None and engine.client.bought == []
