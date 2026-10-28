from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fifteen_bot.engine import Window, _position

D = Decimal
CLOSE = datetime.now(timezone.utc) + timedelta(minutes=1)


def trade(**overrides):
    base = {
        "event_symbol": "KALSHI.KXBTC15M-W1",
        "side": "YES",
        "quantity": "5",
        "filled_quantity": "5",
        "avg_price": "0.87",
        "limit_price": "0.87",
        "est_fees": "0.10",
        "order_status": "FILLED",
    }
    return {**base, **overrides}


def window(**overrides):
    return Window(event_symbol="KALSHI.KXBTC15M-W1", title="BTC", resolution_time=CLOSE, **overrides)


def test_live_pnl_is_value_at_bid_minus_cost_and_fees():
    p = _position(trade(), window(yes_bid=D("0.94"), no_bid=D("0.05")))
    assert p["trading"] and p["bid"] == D("0.94")
    assert p["cost"] == D("4.45")  # 5 × 0.87 + 0.10
    assert p["pnl"] == D("0.25")  # 5 × 0.94 − 4.45
    assert (p["if_win"], p["if_lose"]) == (D("0.55"), D("-4.45"))


def test_no_side_uses_the_no_bid():
    p = _position(trade(side="NO"), window(yes_bid=D("0.94"), no_bid=D("0.05")))
    assert p["pnl"] == D("0.25") - D("4.45")  # 5 × 0.05 − 4.45


def test_partial_fill_counts_only_filled_contracts_and_their_share_of_fees():
    p = _position(trade(filled_quantity="2", order_status="PARTIALLY_FILLED"), window(yes_bid=D("0.90")))
    assert p["cost"] == D("1.78")  # 2 × 0.87 + 0.10 × 2/5
    assert p["pnl"] == D("0.02") and p["order_open"]


def test_closed_window_has_no_live_price_but_keeps_outcomes():
    p = _position(trade(), None)
    assert not p["trading"] and p["pnl"] is None
    assert p["if_win"] == D("0.55")


def test_a_later_window_does_not_price_an_older_trade():
    p = _position(trade(), Window(event_symbol="KALSHI.KXBTC15M-W2", title="BTC", resolution_time=CLOSE, yes_bid=D("0.5")))
    assert not p["trading"] and p["pnl"] is None


def test_missing_bid_gives_no_pnl():
    assert _position(trade(), window(yes_bid=None))["pnl"] is None


def test_contracts_sold_early_count_at_their_sale_price():
    # 5 bought: 2 sold for 1.90 gross (0.04 selling fees), 3 still held and valued at the bid.
    t = trade(sold_quantity="2", sold_value="1.90", sell_fees="0.04", exit_reason="TAKE_PROFIT")
    p = _position(t, window(yes_bid=D("0.94")), selling=True)
    assert p["sold"] == D("2") and p["selling"] and p["exit_reason"] == "TAKE_PROFIT"
    assert p["pnl"] == D("3") * D("0.94") + D("1.86") - D("4.45")
    assert (p["if_win"], p["if_lose"]) == (D("3") + D("1.86") - D("4.45"), D("1.86") - D("4.45"))
