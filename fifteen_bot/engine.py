"""The trading loop: tracks each selected market's current 15-minute window,
applies its strategy, places orders and settles the results.

Public allows 10 API requests per second, so besides the hard cap in
`ratelimit`, the loop keeps its own traffic low: prices for every market come
from one batched quote request, and open orders from one order search.
"""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable, Optional
from uuid import uuid4

import httpx
import public_api_sdk
from public_api_sdk import (
    ApiKeyAuthConfig,
    APIError,
    AsyncPublicApiClient,
    EventFrequency,
    EventOutcomeState,
    EventSortingMode,
    EventSummary,
    EventSummaryFilters,
    EventSummaryRequest,
    InstrumentType,
    OrderExpirationRequest,
    OrderInstrument,
    OrderRequest,
    OrderSearchRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    QuoteOutcome,
    TimeInForce,
)
from public_api_sdk.models.account import Account

from .ratelimit import MAX_REQUESTS_PER_SECOND, RateLimitedTransport, RateLimiter, urgent
from .store import Store, now_iso
from .strategy import (
    CENT,
    ExitReason,
    Pick,
    Quote,
    Strategy,
    exit_reason,
    in_entry_window,
    pick_contract,
    sell_price,
    settle_pnl,
)

logger = logging.getLogger(__name__)

TICK_SECONDS = 2.0
EVENT_REFRESH_SECONDS = 10.0  # how often to look for a market's next window
AVAILABLE_REFRESH_SECONDS = 30.0
DETAILS_REFRESH_SECONDS = 30.0  # contract symbols, close time and trading state of each window
QUOTE_HOT_SECONDS = 2.0  # price refresh while any market is in (or near) its entry window
QUOTE_COLD_SECONDS = 10.0
MAX_QUOTE_AGE_SECONDS = 5.0  # never trade on prices older than this
QUOTE_BATCH_SIZE = 50  # contracts per quote request
HOT_MARGIN_SECONDS = 10
SETTLE_GRACE_SECONDS = 30  # windows settle a few minutes after they close
SETTLE_POLL_SECONDS = 20.0
ORDER_LOOKUP_GRACE_SECONDS = 30
EXIT_RETRY_SECONDS = 5.0  # a sell not filled by then is cancelled and re-priced at the new bid

TERMINAL_ORDER_STATUSES = {
    OrderStatus.FILLED.value,
    OrderStatus.CANCELLED.value,
    OrderStatus.QUEUED_CANCELLED.value,
    OrderStatus.REJECTED.value,
    OrderStatus.EXPIRED.value,
    OrderStatus.REPLACED.value,
}
# A sell that Public rejected or never created is done too; the position is then held to settlement.
# NOT_SENT: it couldn't be saved first, so it was never sent; a later tick can try again.
EXIT_DONE_STATUSES = TERMINAL_ORDER_STATUSES | {"NOT_FOUND", "NOT_SENT"}
EXIT_LABELS = {"TAKE_PROFIT": "take-profit", "STOP_LOSS": "stop-loss"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def market_name(title: Optional[str], series: str) -> str:
    """"BTC 15 min · $80,520.00 target" -> "BTC 15 min"; the target changes every window."""
    return (title or series).split(" · ")[0].strip()


@dataclass
class Window:
    """The current 15-minute window of one market."""

    event_symbol: str
    title: str
    resolution_time: datetime
    close_time: Optional[datetime] = None
    yes_symbol: Optional[str] = None
    no_symbol: Optional[str] = None
    yes_ask: Optional[Decimal] = None
    yes_bid: Optional[Decimal] = None
    no_ask: Optional[Decimal] = None
    no_bid: Optional[Decimal] = None
    tradable: bool = False
    multi_outcome: bool = False
    details_at: float = float("-inf")
    priced_at: float = float("-inf")
    note: Optional[str] = None

    @property
    def closes_at(self) -> datetime:
        return self.close_time or self.resolution_time

    def seconds_left(self) -> float:
        return (self.closes_at - utcnow()).total_seconds()

    def to_json(self) -> dict[str, Any]:
        return {
            "event_symbol": self.event_symbol,
            "title": self.title,
            "close_time": self.closes_at.isoformat(),
            "yes_ask": self.yes_ask,
            "yes_bid": self.yes_bid,
            "no_ask": self.no_ask,
            "no_bid": self.no_bid,
            "tradable": self.tradable,
            "multi_outcome": self.multi_outcome,
            "note": self.note,
        }


class Engine:
    def __init__(self, store: Store, label: str = "") -> None:
        self.store = store
        self.label = label  # identifies the session in server logs
        self.client: Optional[AsyncPublicApiClient] = None
        self.accounts: list[Account] = []
        self.account_id: Optional[str] = None
        self.running = False
        self.windows: dict[str, Window] = {}
        self.available: list[dict[str, Any]] = []
        self.log: deque[dict[str, Any]] = deque(maxlen=300)
        self._http: Optional[httpx.AsyncClient] = None
        self._transport: Optional[RateLimitedTransport] = None
        self._events_at = float("-inf")
        self._quotes_at = float("-inf")
        self._settle_checked: dict[int, float] = {}
        self._cancel_sent: set[int] = set()  # trade ids whose buy order was cancelled
        self._exit_cancel_sent: set[int] = set()  # exit ids whose sell order was cancelled
        # Awaited after recording an order and before sending it; with a database, this saves
        # the record first, so a crash can never leave an order at Public that we don't know about.
        self.checkpoint: Callable[[], Awaitable[None]] = _nothing

    # --- session ----------------------------------------------------------

    async def connect(self, api_key: str) -> list[Account]:
        """Validate the key by listing accounts; raises APIError if Public rejects it."""
        transport = RateLimitedTransport(RateLimiter(MAX_REQUESTS_PER_SECOND))
        http = _rate_limited_http_client(transport)
        client = AsyncPublicApiClient(
            ApiKeyAuthConfig(api_secret_key=api_key, validity_minutes=60), http_client=http
        )
        try:
            accounts = (await client.get_accounts()).accounts
        except BaseException:
            await client.close()
            await http.aclose()
            raise
        await self.disconnect()
        self.client, self._http, self._transport, self.accounts = client, http, transport, accounts
        if len(accounts) == 1:
            self.select_account(accounts[0].account_id)
        self._log("info", None, f"Connected to Public ({len(accounts)} account(s))")
        return accounts

    async def resume(self, api_key: str) -> None:
        """Reconnect a session saved before a server restart, to the account and running state it had."""
        await self.connect(api_key)
        saved = self.store.get_setting("account_id")
        if saved and any(a.account_id == saved for a in self.accounts):
            self.account_id = saved
        self.running = self.account_id is not None and self.store.get_setting("running") == "1"
        self._log("info", None, f"Resumed after a server restart{', still trading' if self.running else ''}")

    def select_account(self, account_id: str) -> None:
        if not any(a.account_id == account_id for a in self.accounts):
            raise ValueError("Unknown account")
        self.account_id = account_id
        self.store.set_setting("account_id", account_id)

    async def disconnect(self) -> None:
        self.running = False
        client, self.client, self.accounts, self.account_id = self.client, None, [], None
        http, self._http, self._transport = self._http, None, None
        self.windows.clear()
        self.available = []
        self._events_at = self._quotes_at = float("-inf")
        try:
            if client is not None:
                await client.close()
            if http is not None:
                await http.aclose()
        except Exception:
            logger.exception("%sclosing the Public client failed", self._prefix)

    async def shutdown(self) -> None:
        """End of a session: cancel orders still waiting to fill, then disconnect.

        Contracts that already filled stay in the account and settle at Public as normal.
        """
        if self.client is not None and self.account_id is not None:
            for trade in self.store.trades_to_track():
                open_orders = [trade] if trade["order_status"] not in TERMINAL_ORDER_STATUSES else []
                open_orders += [e for e in self.store.exits_for(trade["id"]) if e["order_status"] not in EXIT_DONE_STATUSES]
                for order in open_orders:
                    if not order["order_id"]:
                        continue
                    try:
                        with urgent():
                            await self.client.cancel_order(order["order_id"], self.account_id)
                        self._log("info", trade["series"], f"Cancelled unfilled order for trade #{trade['id']} at session end")
                    except Exception:
                        logger.exception("%scancelling order %s at shutdown failed", self._prefix, order["order_id"])
        await self.disconnect()

    def key_refused_seconds(self) -> float:
        """How long Public has been refusing this session's key; 0 while it's accepted."""
        since = self._transport.refused_since if self._transport else None
        return time.monotonic() - since if since is not None else 0.0

    def set_running(self, running: bool) -> None:
        if running and (self.client is None or self.account_id is None):
            raise ValueError("Connect and choose an account first")
        if running != self.running:
            self.running = running
            self._log("info", None, f"Engine {'started' if running else 'stopped'}")
        self.store.set_setting("running", "1" if running else "0")

    # --- loop -------------------------------------------------------------

    async def run(self) -> None:
        while True:
            started = time.monotonic()
            try:
                await self._tick()
            except Exception as exc:  # keep the loop alive whatever happens
                logger.exception("%stick failed", self._prefix)
                self._log("error", None, f"Engine error: {exc}")
            await asyncio.sleep(max(0.5, TICK_SECONDS - (time.monotonic() - started)))

    async def _tick(self) -> None:
        if self.client is None:
            return
        markets = self.store.list_markets()
        now = utcnow()
        stale = any(
            (w := self.windows.get(m["series"])) is None or w.closes_at <= now for m in markets
        )
        if stale and time.monotonic() - self._events_at >= EVENT_REFRESH_SECONDS:
            await self.refresh_events()

        live = [(m, w) for m in markets if (w := self.windows.get(m["series"])) and w.closes_at > utcnow()]
        await asyncio.gather(*(self._guarded(m["series"], self._refresh_details(w)) for m, w in live))
        await self._guarded(None, self._refresh_quotes(live))
        # Concurrent so markets entering together don't wait on each other; the rate limiter paces them.
        await asyncio.gather(*(self._guarded(m["series"], self._decide(m, w)) for m, w in live))
        await asyncio.gather(*(self._guarded(m["series"], self._manage_exit(m, w)) for m, w in live))
        await self._track_trades()

    async def _guarded(self, series: Optional[str], step) -> None:
        try:
            await step
        except Exception as exc:
            logger.exception("%sstep failed for %s", self._prefix, series)
            self._log("error", series, str(exc))

    # --- discovery --------------------------------------------------------

    async def available_markets(self) -> list[dict[str, Any]]:
        if time.monotonic() - self._events_at >= AVAILABLE_REFRESH_SECONDS:
            await self.refresh_events()
        return self.available

    async def refresh_events(self) -> None:
        """List every open 15-minute event and track each market's current window."""
        assert self.client is not None
        request = EventSummaryRequest(
            sorting_mode=EventSortingMode.EXPIRATION,
            display_resolved_events=False,
            filters=EventSummaryFilters(frequencies=[EventFrequency.FIFTEEN_MINUTES]),
        )
        events: list[EventSummary] = []
        while True:
            page = await self.client.get_event_summary(request)
            events.extend(page.content)
            if not page.next_token:
                break
            request.next_token = page.next_token
        self._events_at = time.monotonic()

        now = utcnow()
        current: dict[str, EventSummary] = {}
        latest: dict[str, EventSummary] = {}
        for event in events:
            series = event.series
            if series is None or event.resolution_time is None:
                continue
            latest.setdefault(series, event)
            # The API keeps listing a window as unresolved for a few minutes after it closes.
            if event.resolved or as_utc(event.resolution_time) <= now:
                continue
            best = current.get(series)
            if best is None or as_utc(event.resolution_time) < as_utc(best.resolution_time):  # type: ignore[arg-type]
                current[series] = event

        for series, event in current.items():
            window = self.windows.get(series)
            if window is None or window.event_symbol != event.event_symbol:
                self.windows[series] = Window(
                    event_symbol=event.event_symbol,
                    title=event.title or series,
                    resolution_time=as_utc(event.resolution_time),  # type: ignore[arg-type]
                )

        available = []
        for series, event in latest.items():
            shown = current.get(series, event)
            available.append(
                {
                    "series": series,
                    "name": market_name(shown.title, series),
                    "category": shown.category,
                    "subcategories": shown.subcategories,
                    "title": shown.title,
                    "close_time": as_utc(shown.resolution_time).isoformat() if series in current else None,  # type: ignore[arg-type]
                    "volume": shown.volume,
                }
            )
        self.available = sorted(available, key=lambda m: (m["category"] or "", m["name"]))

    # --- prices -------------------------------------------------------------

    async def _refresh_details(self, window: Window) -> None:
        """Contract symbols, close time and trading state: once per window, then occasionally."""
        assert self.client is not None
        if window.multi_outcome or time.monotonic() - window.details_at < DETAILS_REFRESH_SECONDS:
            return
        details = await self.client.get_event_details(window.event_symbol)
        window.details_at = time.monotonic()
        outcome = details.outcome
        if outcome is None:
            window.multi_outcome = True
            window.note = "Multi-outcome market — not supported yet"
            return
        yes, no = outcome.yes, outcome.no
        window.yes_symbol = yes.symbol if yes else None
        window.no_symbol = no.symbol if no else None
        if window.yes_ask is None and yes and no:  # show prices before the first quote arrives
            window.yes_ask, window.yes_bid, window.no_ask, window.no_bid = yes.ask, yes.bid, no.ask, no.bid
        if outcome.close_time is not None:
            window.close_time = as_utc(outcome.close_time)
        window.tradable = outcome.is_tradable and not details.halted

    async def _refresh_quotes(self, live: list[tuple[dict[str, Any], Window]]) -> None:
        """Prices for every market's YES and NO contracts in one request (per 50 contracts)."""
        assert self.client is not None
        hot = any(
            m["enabled"] and w.seconds_left() <= m["strategy"].entry_seconds + HOT_MARGIN_SECONDS
            for m, w in live
        )
        if time.monotonic() - self._quotes_at < (QUOTE_HOT_SECONDS if hot else QUOTE_COLD_SECONDS):
            return
        by_symbol: dict[str, tuple[Window, str]] = {}
        for _, window in live:
            if window.yes_symbol and window.no_symbol:
                by_symbol[window.yes_symbol] = (window, "yes")
                by_symbol[window.no_symbol] = (window, "no")
        if not by_symbol or self.account_id is None:
            return
        self._quotes_at = time.monotonic()
        symbols = list(by_symbol)
        for start in range(0, len(symbols), QUOTE_BATCH_SIZE):
            instruments = [
                OrderInstrument(symbol=s, type=InstrumentType.EVENTCONTRACT)
                for s in symbols[start : start + QUOTE_BATCH_SIZE]
            ]
            quotes = await self.client.get_quotes(instruments, self.account_id)
            received = time.monotonic()
            for quote in quotes:
                match = by_symbol.get(quote.instrument.symbol)
                if match is None or quote.outcome != QuoteOutcome.SUCCESS:
                    continue
                window, side = match
                setattr(window, f"{side}_ask", quote.ask)
                setattr(window, f"{side}_bid", quote.bid)
                if side == "no":  # both sides arrive in the same batch
                    window.priced_at = received

    # --- trading ----------------------------------------------------------

    async def _decide(self, market: dict[str, Any], window: Window) -> None:
        series: str = market["series"]
        strategy: Strategy = market["strategy"]
        if not (self.running and market["enabled"]) or window.multi_outcome:
            return
        if self.store.has_trade(series, window.event_symbol):
            return
        seconds_left = window.seconds_left()
        if not in_entry_window(strategy, seconds_left):
            return
        if not window.tradable or window.yes_symbol is None or window.no_symbol is None:
            window.note = "Window is not tradable right now"
            return
        if time.monotonic() - window.priced_at > MAX_QUOTE_AGE_SECONDS:
            window.note = "Waiting for fresh prices"
            return
        pick = pick_contract(
            strategy,
            {"YES": Quote(window.yes_symbol, window.yes_ask), "NO": Quote(window.no_symbol, window.no_ask)},
        )
        if pick is None:
            window.note = f"Waiting for a side priced {_cents(strategy.min_price)}–{_cents(strategy.max_price)}"
            return
        with urgent():
            await self._enter(series, strategy, window, pick, seconds_left)

    async def _enter(self, series: str, strategy: Strategy, window: Window, pick: Pick, seconds_left: float) -> None:
        assert self.client is not None
        quantity = strategy.quantity
        trade = {
            "series": series,
            "event_symbol": window.event_symbol,
            "side": pick.side,
            "contract_symbol": pick.symbol,
            "quantity": quantity,
            "limit_price": pick.price,
            "seconds_left": int(seconds_left),
            "close_time": window.closes_at.isoformat(),
        }
        summary = f"{pick.side} x{quantity} @ {_cents(pick.price)} with {int(seconds_left)}s left"

        # Preflight checks the order with Public without placing it, and gives the fee estimate.
        try:
            preflight = await self.client.preflight_buy_event_contract(
                pick.symbol, quantity, limit_price=pick.price, account_id=self.account_id
            )
            if preflight.estimated_cost is not None:
                trade["est_fees"] = max(Decimal(0), preflight.estimated_cost - preflight.order_value)
        except APIError as exc:
            self.store.insert_trade(**trade, order_status="PREFLIGHT_FAILED", result="REJECTED", note=str(exc))
            self._log("error", series, f"Preflight rejected {summary}: {exc}")
            return

        # Record the order before sending it, so a crash or timeout can never lead to a second order.
        order_id = str(uuid4())
        trade_id = self.store.insert_trade(**trade, order_id=order_id, order_status="SUBMITTING")
        try:
            await self.checkpoint()
        except Exception as exc:
            self.store.update_trade(trade_id, order_status="NOT_SENT", result="ERROR", note="Couldn't save the order")
            self._log("error", series, f"Didn't send {summary}: couldn't save it first ({exc})")
            return
        try:
            await self.client.buy_event_contract(
                pick.symbol, quantity, limit_price=pick.price, order_id=order_id, account_id=self.account_id
            )
        except APIError as exc:
            # The request may still have reached Public; order tracking looks it up and settles the row.
            self.store.update_trade(trade_id, order_status="SUBMIT_ERROR", note=str(exc))
            self._log("error", series, f"Order failed {summary}: {exc}")
            return
        self.store.update_trade(trade_id, order_status=OrderStatus.NEW.value)
        window.note = f"Ordered {summary}"
        self._log("trade", series, f"Ordered {summary}")

    # --- early exits ------------------------------------------------------

    async def _manage_exit(self, market: dict[str, Any], window: Window) -> None:
        """Sell this window's position early once its take-profit or stop-loss is reached."""
        strategy: Strategy = market["strategy"]
        if not (self.running and market["enabled"]) or (strategy.take_profit is None and strategy.stop_loss is None):
            return
        trade = self.store.trade_for(market["series"], window.event_symbol)
        if trade is None or trade["result"] != "PENDING":
            return
        assert self.client is not None and self.account_id is not None
        filled = Decimal(trade["filled_quantity"])
        held = filled - Decimal(trade["sold_quantity"])
        if held <= 0:
            return
        exits = self.store.exits_for(trade["id"])
        if any(e["order_status"] in ("REJECTED", "NOT_FOUND") for e in exits):
            return  # Public refused a sell; already logged, and retrying would just repeat it
        pending = [e for e in exits if e["order_status"] not in EXIT_DONE_STATUSES]
        if pending:
            for e in pending:
                age = (utcnow() - datetime.fromisoformat(e["placed_at"])).total_seconds()
                sent = e["order_status"] not in ("SUBMITTING", "SUBMIT_ERROR")
                if sent and age >= EXIT_RETRY_SECONDS and e["id"] not in self._exit_cancel_sent:
                    # The bid moved away; once Public confirms the cancel, a later tick re-prices.
                    await self._cancel_exit(e)
            return

        if not window.tradable or time.monotonic() - window.priced_at > MAX_QUOTE_AGE_SECONDS:
            return
        bid = window.yes_bid if trade["side"] == "YES" else window.no_bid
        if bid is None or (price := sell_price(bid)) < CENT:
            return
        quantity = Decimal(trade["quantity"])
        buy_fees = Decimal(trade["est_fees"]) * filled / quantity
        cost = Decimal(trade["avg_price"] or trade["limit_price"]) + buy_fees / filled
        # Selling costs about what buying did; Public quotes no fee for a sell before it's placed.
        sell_fee = Decimal(trade["est_fees"]) / quantity
        reason = exit_reason(strategy, cost, price, sell_fee)
        if reason is None:
            return
        if trade["order_status"] not in TERMINAL_ORDER_STATUSES:
            # Stop the rest of the buy first so nothing fills after the sell; then sell what filled.
            if trade["id"] not in self._cancel_sent:
                with urgent():
                    await self.client.cancel_order(trade["order_id"], self.account_id)
                self._cancel_sent.add(trade["id"])
            return
        with urgent():
            await self._sell(trade, held, price, reason, sell_fee)

    async def _sell(self, trade: dict[str, Any], quantity: Decimal, price: Decimal, reason: ExitReason, fee: Decimal) -> None:
        assert self.client is not None
        series = trade["series"]
        summary = f"{trade['side']} x{quantity} @ {_cents(price)} ({EXIT_LABELS[reason]})"
        # Recorded before sending, like buys, so a timeout can never lead to a second sell.
        order_id = str(uuid4())
        exit_id = self.store.insert_exit(
            trade_id=trade["id"], reason=reason, quantity=quantity, limit_price=price,
            est_fees=fee * quantity, order_id=order_id, order_status="SUBMITTING",
        )
        if trade["exit_reason"] is None:
            self.store.update_trade(trade["id"], exit_reason=reason)
        try:
            await self.checkpoint()
        except Exception as exc:
            self.store.update_exit(exit_id, order_status="NOT_SENT", note="Couldn't save the order")
            self._log("error", series, f"Didn't send sell {summary}: couldn't save it first ({exc})")
            return
        try:
            await self.client.place_order(_sell_order(trade["contract_symbol"], quantity, price, order_id), self.account_id)
        except APIError as exc:
            # The request may still have reached Public; order tracking looks it up either way.
            self.store.update_exit(exit_id, order_status="SUBMIT_ERROR", note=str(exc))
            self._log("error", series, f"Sell failed {summary}: {exc}")
            return
        self.store.update_exit(exit_id, order_status=OrderStatus.NEW.value)
        self._log("trade", series, f"Selling {summary}")

    async def _cancel_exit(self, exit: dict[str, Any]) -> None:
        assert self.client is not None
        self._exit_cancel_sent.add(exit["id"])
        try:
            with urgent():
                await self.client.cancel_order(exit["order_id"], self.account_id)
        except APIError as exc:
            # Usually the sell filled in the meantime; order tracking records whatever happened.
            logger.info("%scancelling sell %s: %s", self._prefix, exit["order_id"], exc)

    def _sync_sold(self, trade_id: int) -> None:
        """Total up a trade's sell orders onto the trade, for P/L and the dashboard."""
        sold = value = fees = Decimal(0)
        for e in self.store.exits_for(trade_id):
            filled = Decimal(e["filled_quantity"])
            if filled > 0:
                sold += filled
                value += filled * Decimal(e["avg_price"] or e["limit_price"])
                fees += Decimal(e["est_fees"]) * filled / Decimal(e["quantity"])
        self.store.update_trade(trade_id, sold_quantity=sold, sold_value=value, sell_fees=fees)

    # --- order tracking and settlement -----------------------------------

    async def _track_trades(self) -> None:
        trades = self.store.trades_to_track()
        open_orders = [t for t in trades if t["order_status"] not in TERMINAL_ORDER_STATUSES]
        open_exits = [
            (e, t) for t in trades for e in self.store.exits_for(t["id"]) if e["order_status"] not in EXIT_DONE_STATUSES
        ]
        if open_orders or open_exits:
            await self._guarded(None, self._track_orders(open_orders, open_exits))
        for trade in trades:
            if trade["order_status"] in TERMINAL_ORDER_STATUSES:
                await self._guarded(trade["series"], self._check_settlement(trade))

    async def _track_orders(
        self, trades: list[dict[str, Any]], exits: list[tuple[dict[str, Any], dict[str, Any]]]
    ) -> None:
        """Look up every open order (buys and sells) with one search rather than one request per order."""
        assert self.client is not None and self.account_id is not None
        placed = [t["placed_at"] for t in trades] + [e["placed_at"] for e, _ in exits]
        since = min(datetime.fromisoformat(p) for p in placed) - timedelta(minutes=1)
        found = await self.client.search_orders(
            OrderSearchRequest(created_after=since, security_type=InstrumentType.EVENTCONTRACT), self.account_id
        )
        orders = {order.order_id: order for order in found}
        for exit, trade in exits:
            order = orders.get(exit["order_id"])
            self._track_exit(exit, trade, order)
            if (
                order is not None
                and order.status.value not in EXIT_DONE_STATUSES
                and exit["id"] not in self._exit_cancel_sent
                and utcnow() >= datetime.fromisoformat(trade["close_time"])
            ):
                # The window is over; Public settles whatever is still held.
                await self._cancel_exit(exit)
        for trade in trades:
            trade_id = trade["id"]
            order = orders.get(trade["order_id"])
            if order is None:
                placed = datetime.fromisoformat(trade["placed_at"])
                if utcnow() - placed > timedelta(seconds=ORDER_LOOKUP_GRACE_SECONDS):
                    self.store.update_trade(trade_id, order_status="NOT_FOUND", result="ERROR")
                    self._log("error", trade["series"], f"Order for trade #{trade_id} was never created")
                continue

            status = order.status.value
            filled = order.filled_quantity or Decimal(0)
            fields: dict[str, Any] = {"order_status": status, "filled_quantity": filled}
            if order.average_price is not None:
                fields["avg_price"] = order.average_price
            if status in TERMINAL_ORDER_STATUSES:
                if filled == 0:
                    fields["result"] = "REJECTED" if status == OrderStatus.REJECTED.value else "UNFILLED"
                    fields["note"] = order.reject_reason or trade["note"]
            elif utcnow() >= datetime.fromisoformat(trade["close_time"]) and trade_id not in self._cancel_sent:
                # The window is over; don't leave the rest of the order resting.
                with urgent():
                    await self.client.cancel_order(trade["order_id"], self.account_id)
                self._cancel_sent.add(trade_id)
            self.store.update_trade(trade_id, **fields)
            if status != trade["order_status"]:
                self._log("info", trade["series"], f"Order #{trade_id} {status.lower()} ({filled} filled)")

    def _track_exit(self, exit: dict[str, Any], trade: dict[str, Any], order: Any) -> None:
        series = trade["series"]
        if order is None:
            placed = datetime.fromisoformat(exit["placed_at"])
            if utcnow() - placed > timedelta(seconds=ORDER_LOOKUP_GRACE_SECONDS):
                self.store.update_exit(exit["id"], order_status="NOT_FOUND")
                self._log("error", series, f"Sell for trade #{trade['id']} was never created; holding to settlement")
            return
        status = order.status.value
        filled = order.filled_quantity or Decimal(0)
        fields: dict[str, Any] = {"order_status": status, "filled_quantity": filled}
        if order.average_price is not None:
            fields["avg_price"] = order.average_price
        if order.reject_reason:
            fields["note"] = order.reject_reason
        self.store.update_exit(exit["id"], **fields)
        self._sync_sold(trade["id"])
        if status == exit["order_status"]:
            return
        if status == OrderStatus.REJECTED.value:
            self._log("error", series, f"Sell for trade #{trade['id']} rejected ({order.reject_reason or 'no reason given'}); holding to settlement")
        elif status == OrderStatus.FILLED.value:
            self._log("trade", series, f"Sold {trade['side']} x{filled} @ {_cents(order.average_price or Decimal(exit['limit_price']))}")
        elif status in TERMINAL_ORDER_STATUSES and filled > 0:
            self._log("info", series, f"Sell for trade #{trade['id']} {status.lower()} with {filled} sold")

    async def _check_settlement(self, trade: dict[str, Any]) -> None:
        assert self.client is not None
        filled = Decimal(trade["filled_quantity"])
        sold = Decimal(trade["sold_quantity"])
        buy_fees = Decimal(trade["est_fees"]) * filled / Decimal(trade["quantity"])
        proceeds = Decimal(trade["sold_value"]) - Decimal(trade["sell_fees"])
        if filled > 0 and sold >= filled:
            # Sold out of the whole position early: final now, no need to wait for the window to settle.
            pnl = settle_pnl(filled, Decimal(trade["avg_price"]), buy_fees, False, sold, proceeds).quantize(Decimal("0.0001"))
            self.store.update_trade(trade["id"], result="SOLD", pnl=pnl, settled_at=now_iso())
            self._log("result", trade["series"], f"{trade['side']} sold early: {'+' if pnl >= 0 else '-'}${abs(pnl):.2f}")
            return
        close_time = datetime.fromisoformat(trade["close_time"])
        if utcnow() < close_time + timedelta(seconds=SETTLE_GRACE_SECONDS):
            return
        if time.monotonic() - self._settle_checked.get(trade["id"], float("-inf")) < SETTLE_POLL_SECONDS:
            return
        self._settle_checked[trade["id"]] = time.monotonic()

        if filled == 0:
            self.store.update_trade(trade["id"], result="UNFILLED", pnl=Decimal(0), settled_at=now_iso())
            return
        if any(e["order_status"] not in EXIT_DONE_STATUSES for e in self.store.exits_for(trade["id"])):
            return  # a sell is still being cancelled; settle once its final fill is known
        outcome = (await self.client.get_event_details(trade["event_symbol"])).outcome
        if outcome is None or outcome.state != EventOutcomeState.STATE_SETTLED:
            return

        won = outcome.won(trade["contract_symbol"])
        if won is None:
            self.store.update_trade(
                trade["id"], result="VOID", settled_at=now_iso(), note=f"Settled {outcome.settled_outcome}"
            )
            self._log("warn", trade["series"], f"Trade #{trade['id']} settled without a YES/NO winner")
            return
        pnl = settle_pnl(filled, Decimal(trade["avg_price"]), buy_fees, won, sold, proceeds).quantize(Decimal("0.0001"))
        self.store.update_trade(trade["id"], result="WON" if won else "LOST", pnl=pnl, settled_at=now_iso())
        self._settle_checked.pop(trade["id"], None)
        verb = "won" if won else "lost"
        self._log("result", trade["series"], f"{trade['side']} {verb}: {'+' if pnl >= 0 else '-'}${abs(pnl):.2f}")

    # --- reporting --------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        stats = self.store.stats()
        empty = {"trades": 0, "wins": 0, "losses": 0, "pending": 0, "pnl": Decimal(0), "cost": Decimal(0)}
        markets = []
        open_pnl: Optional[Decimal] = None
        for market in self.store.list_markets():
            window = self.windows.get(market["series"])
            if window and window.closes_at <= utcnow():
                window = None
            trade = self.store.open_trade(market["series"])
            position = None
            if trade:
                selling = any(e["order_status"] not in EXIT_DONE_STATUSES for e in self.store.exits_for(trade["id"]))
                position = _position(trade, window, selling)
            if position and position["pnl"] is not None:
                open_pnl = (open_pnl or Decimal(0)) + position["pnl"]
            this_window = self.store.trade_for(market["series"], window.event_symbol) if window else None
            markets.append(
                {
                    **market,
                    "window": window.to_json() if window else None,
                    "window_trade": _trade_summary(this_window) if this_window else None,
                    "position": position,
                    "stats": stats.get(market["series"], empty),
                }
            )
        totals: dict[str, Any] = dict(empty)
        for s in stats.values():
            for key in totals:
                totals[key] += s[key]
        totals["open_pnl"] = open_pnl
        return {
            "connected": self.client is not None,
            "accounts": [{"id": a.account_id, "type": a.account_type.value} for a in self.accounts],
            "account_id": self.account_id,
            "running": self.running,
            "markets": markets,
            "totals": totals,
            "log": list(self.log)[::-1][:100],
            "server_time": utcnow().isoformat(),
        }

    @property
    def _prefix(self) -> str:
        return f"<{self.label}> " if self.label else ""

    def _log(self, level: str, series: Optional[str], message: str) -> None:
        log = logger.warning if level in ("warn", "error") else logger.info
        log("%s%s%s", self._prefix, f"[{series}] " if series else "", message)
        self.log.append({"time": now_iso(), "level": level, "series": series, "message": message})


def _position(trade: dict[str, Any], window: Optional[Window], selling: bool = False) -> dict[str, Any]:
    """A market's unsettled trade, valued at the current bid while its window is still trading.

    The bid is what the contracts still held could be sold for right now, so `pnl`
    is what you'd make or lose by selling them (before any selling fee), plus what
    any contracts already sold early brought in. Once the window closes there's no
    market to price against until Public settles it.
    """
    filled = Decimal(trade["filled_quantity"])
    quantity = Decimal(trade["quantity"])
    sold = Decimal(trade.get("sold_quantity") or 0)
    held = filled - sold
    proceeds = Decimal(trade.get("sold_value") or 0) - Decimal(trade.get("sell_fees") or 0)
    avg_price = Decimal(trade["avg_price"] or trade["limit_price"])
    fees = Decimal(trade["est_fees"]) * filled / quantity
    cost = filled * avg_price + fees
    trading = window is not None and window.event_symbol == trade["event_symbol"]
    bid = (window.yes_bid if trade["side"] == "YES" else window.no_bid) if trading else None  # type: ignore[union-attr]
    value = held * bid + proceeds if bid is not None and filled > 0 else None
    return {
        "side": trade["side"],
        "quantity": quantity,
        "filled": filled,
        "sold": sold,
        "selling": selling,
        "exit_reason": trade.get("exit_reason"),
        "avg_price": avg_price,
        "order_open": trade["order_status"] not in TERMINAL_ORDER_STATUSES,
        "trading": trading,
        "cost": cost,
        "bid": bid,
        "value": value,
        "pnl": value - cost if value is not None else None,
        "if_win": held + proceeds - cost,
        "if_lose": proceeds - cost,
    }


def _trade_summary(trade: dict[str, Any]) -> dict[str, Any]:
    """What a market card needs to say about its trade in the current window."""
    return {
        "side": trade["side"],
        "quantity": Decimal(trade["quantity"]),
        "filled": Decimal(trade["filled_quantity"]),
        "price": Decimal(trade["avg_price"] or trade["limit_price"]),
        "sold": Decimal(trade["sold_quantity"]),
        "exit_reason": trade["exit_reason"],
        "result": trade["result"],
        "note": trade["note"],
    }


async def _nothing() -> None:
    pass


def _sell_order(symbol: str, quantity: Decimal, price: Decimal, order_id: str) -> OrderRequest:
    """A DAY limit sell of contracts already held (the SDK only has a helper for buying)."""
    return OrderRequest(
        order_id=order_id,
        instrument=OrderInstrument(symbol=symbol, type=InstrumentType.EVENTCONTRACT),
        order_side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        limit_price=price,
        quantity=quantity,
        expiration=OrderExpirationRequest(time_in_force=TimeInForce.DAY),
    )


def _rate_limited_http_client(transport: RateLimitedTransport) -> httpx.AsyncClient:
    """The HTTP client the SDK would build itself, with every request passing the rate limiter."""
    version = getattr(public_api_sdk, "__version__", "0.1.0")
    return httpx.AsyncClient(
        headers={
            "Content-Type": "application/json",
            "User-Agent": f"public-python-api-sdk-{version}",
            "X-App-Version": f"public-python-api-sdk-{version}",
        },
        # Short enough that one slow request can't make the bot miss a buy window.
        timeout=10.0,
        transport=transport,
    )


def _cents(price: Decimal) -> str:
    return f"{(price / CENT):.0f}¢"
