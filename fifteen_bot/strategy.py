"""Per-market trading strategy and the pure decision logic behind it.

Nothing here does I/O, so the rules can be tested without the Public API.
"""

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

CENT = Decimal("0.01")

Side = Literal["YES", "NO"]
ExitReason = Literal["TAKE_PROFIT", "STOP_LOSS"]


class Strategy(BaseModel):
    """When and what to buy in each 15-minute window of one market, and when to sell early.

    Prices are what you pay per contract, in dollars, which is also the
    market's implied chance of that side winning (0.85 = 85%). The exit
    targets are profit or loss per contract after fees; None holds the
    position until the window settles.
    """

    entry_seconds: int = Field(120, ge=5, le=900, description="Start looking once this many seconds are left.")
    cutoff_seconds: int = Field(10, ge=0, le=900, description="Stop looking once this many seconds are left.")
    min_price: Decimal = Field(Decimal("0.80"), ge=CENT, le=Decimal("0.99"))
    max_price: Decimal = Field(Decimal("0.90"), ge=CENT, le=Decimal("0.99"))
    side: Literal["EITHER", "YES", "NO"] = "EITHER"
    quantity: Decimal = Field(Decimal("1"), gt=0, le=Decimal("10000"), decimal_places=2)
    take_profit: Optional[Decimal] = Field(None, ge=CENT, le=Decimal("0.98"), description="Sell once up this much.")
    stop_loss: Optional[Decimal] = Field(None, ge=CENT, le=Decimal("0.98"), description="Sell once down this much.")

    @model_validator(mode="after")
    def _check_ranges(self) -> "Strategy":
        if self.min_price > self.max_price:
            raise ValueError("the lowest chance must not be above the highest chance")
        if self.cutoff_seconds >= self.entry_seconds:
            raise ValueError("the stop time must be less than the start time")
        if self.min_price != self.min_price.quantize(CENT) or self.max_price != self.max_price.quantize(CENT):
            raise ValueError("chances must be whole percentages")
        for target in (self.take_profit, self.stop_loss):
            if target is not None and target != target.quantize(CENT):
                raise ValueError("take-profit and stop-loss must be whole cents")
        return self


@dataclass(frozen=True)
class Quote:
    symbol: str
    ask: Optional[Decimal]


@dataclass(frozen=True)
class Pick:
    side: Side
    symbol: str
    price: Decimal  # limit price, whole cents


def in_entry_window(strategy: Strategy, seconds_left: float) -> bool:
    return strategy.cutoff_seconds < seconds_left <= strategy.entry_seconds


def limit_price(ask: Decimal) -> Decimal:
    """Orders are priced in whole cents; round the ask up so the order can fill."""
    return ask.quantize(CENT, rounding=ROUND_CEILING)


def pick_contract(strategy: Strategy, quotes: dict[Side, Quote]) -> Optional[Pick]:
    """The side to buy, or None when no allowed side is priced inside the range.

    If both sides qualify (only possible with a very wide range), the pricier
    one wins: it is the side the market thinks is more likely.
    """
    allowed: tuple[Side, ...] = ("YES", "NO") if strategy.side == "EITHER" else (strategy.side,)
    candidates = []
    for side in allowed:
        quote = quotes.get(side)
        if quote is None or quote.ask is None or quote.ask <= 0:
            continue
        price = limit_price(quote.ask)
        if strategy.min_price <= price <= strategy.max_price and price < 1:
            candidates.append(Pick(side=side, symbol=quote.symbol, price=price))
    return max(candidates, key=lambda p: p.price, default=None)


def sell_price(bid: Decimal) -> Decimal:
    """Orders are priced in whole cents; round the bid down so the sell can fill."""
    return bid.quantize(CENT, rounding=ROUND_FLOOR)


def exit_reason(
    strategy: Strategy, cost_per_contract: Decimal, price: Decimal, sell_fee_per_contract: Decimal
) -> Optional[ExitReason]:
    """Whether selling now at `price` reaches the take-profit or the stop-loss.

    `cost_per_contract` includes its share of the buying fees, so with the selling
    fee taken off too, the profit compared with the targets is what lands in the account.
    """
    profit = price - sell_fee_per_contract - cost_per_contract
    if strategy.take_profit is not None and profit >= strategy.take_profit:
        return "TAKE_PROFIT"
    if strategy.stop_loss is not None and profit <= -strategy.stop_loss:
        return "STOP_LOSS"
    return None


def settle_pnl(
    filled: Decimal,
    avg_price: Decimal,
    fees: Decimal,
    won: bool,
    sold: Decimal = Decimal(0),
    proceeds: Decimal = Decimal(0),
) -> Decimal:
    """Profit of a filled position once its window settles: each winning contract still held pays $1.

    `sold` contracts were sold early for `proceeds` (after selling fees); `fees` are the buying fees.
    """
    payout = filled - sold if won else Decimal(0)
    return payout + proceeds - filled * avg_price - fees
