"""One user's markets, settings and trades, in an in-memory SQLite database.

Each session gets its own Store, which the engine reads and writes. On its own it
disappears with the session; with a database configured, `persist` copies every
change through `listener`, and `load` restores a session after a restart. All
access happens on the asyncio event-loop thread, so one connection is enough.
"""

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Iterable, Iterator, Optional

from .strategy import Strategy

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS markets (
    series TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT,
    enabled INTEGER NOT NULL DEFAULT 0,
    strategy TEXT NOT NULL,
    added_at TEXT NOT NULL,
    -- set when buying stopped because the market's daily loss limit could be passed;
    -- cleared by switching the market back on
    halted_at TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    series TEXT NOT NULL,
    event_symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    contract_symbol TEXT NOT NULL,
    quantity TEXT NOT NULL,
    limit_price TEXT NOT NULL,
    filled_quantity TEXT NOT NULL DEFAULT '0',
    avg_price TEXT,
    est_fees TEXT NOT NULL DEFAULT '0',
    order_id TEXT,
    order_status TEXT NOT NULL,
    seconds_left INTEGER,
    close_time TEXT NOT NULL,
    placed_at TEXT NOT NULL,
    result TEXT NOT NULL DEFAULT 'PENDING',
    pnl TEXT,
    settled_at TEXT,
    note TEXT,
    -- totals of the sell orders in `exits`, kept here for the dashboard and P/L
    exit_reason TEXT,
    sold_quantity TEXT NOT NULL DEFAULT '0',
    sold_value TEXT NOT NULL DEFAULT '0',
    sell_fees TEXT NOT NULL DEFAULT '0',
    -- one entry per market per 15-minute window
    UNIQUE (series, event_symbol)
);
-- Sell orders that close a trade's position early (take-profit or stop-loss). One trade
-- can have several: a sell that doesn't fill quickly is cancelled and re-priced.
CREATE TABLE IF NOT EXISTS exits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER NOT NULL REFERENCES trades (id),
    reason TEXT NOT NULL,
    quantity TEXT NOT NULL,
    limit_price TEXT NOT NULL,
    filled_quantity TEXT NOT NULL DEFAULT '0',
    avg_price TEXT,
    est_fees TEXT NOT NULL DEFAULT '0',
    order_id TEXT NOT NULL,
    order_status TEXT NOT NULL,
    placed_at TEXT NOT NULL,
    note TEXT
);
"""

# Each table's primary key, which identifies a row to the listener.
TABLE_KEYS = {"settings": "key", "markets": "series", "trades": "id", "exits": "id"}

# Called with (table, row key, row) after a row changes; the row is None once deleted.
Listener = Callable[[str, str, Optional[dict[str, Any]]], None]

# Results that are final; everything else is still being tracked.
FINAL_RESULTS = ("WON", "LOST", "SOLD", "VOID", "UNFILLED", "REJECTED", "ERROR")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self) -> None:
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.listener: Optional[Listener] = None
        self._reported: dict[tuple[str, str], Optional[dict[str, Any]]] = {}

    def close(self) -> None:
        self.db.close()

    def _changed(self, table: str, key: Any) -> None:
        """Tell the listener about a row's new state, unless it's the same as last time."""
        if self.listener is None:
            return
        row = self.db.execute(f"SELECT * FROM {table} WHERE {TABLE_KEYS[table]} = ?", (key,)).fetchone()
        data = dict(row) if row else None
        if self._reported.get((table, str(key))) != data:
            self._reported[(table, str(key))] = data
            self.listener(table, str(key), data)

    def watch(self, listener: Listener) -> None:
        """Report every row now, then each change from here on."""
        self.listener = listener
        for table, key, row in self.dump():
            self._reported[(table, key)] = row
            listener(table, key, row)

    def dump(self) -> Iterator[tuple[str, str, dict[str, Any]]]:
        """Every row, as (table, row key, row)."""
        for table, key in TABLE_KEYS.items():
            for row in self.db.execute(f"SELECT * FROM {table}").fetchall():
                yield table, str(row[key]), dict(row)

    def load(self, rows: Iterable[tuple[str, dict[str, Any]]]) -> None:
        """Restore rows from `dump` or the listener, e.g. after a restart.

        Columns this version doesn't have are dropped, and ones it added take their defaults.
        """
        columns = {t: {c["name"] for c in self.db.execute(f"PRAGMA table_info({t})")} for t in TABLE_KEYS}
        for table, row in rows:
            if table not in TABLE_KEYS:
                continue
            fields = {name: value for name, value in row.items() if name in columns[table]}
            names = ", ".join(fields)
            marks = ", ".join("?" for _ in fields)
            self.db.execute(f"INSERT OR REPLACE INTO {table} ({names}) VALUES ({marks})", list(fields.values()))
        self.db.commit()

    # --- settings ---------------------------------------------------------

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: Optional[str]) -> None:
        if value is None:
            self.db.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            self.db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        self.db.commit()
        self._changed("settings", key)

    # --- markets ----------------------------------------------------------

    def list_markets(self) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM markets ORDER BY added_at").fetchall()
        return [self._market(row) for row in rows]

    def get_market(self, series: str) -> Optional[dict[str, Any]]:
        row = self.db.execute("SELECT * FROM markets WHERE series = ?", (series,)).fetchone()
        return self._market(row) if row else None

    def add_market(self, series: str, name: str, category: Optional[str]) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO markets (series, name, category, enabled, strategy, added_at) "
            "VALUES (?, ?, ?, 0, ?, ?)",
            (series, name, category, Strategy().model_dump_json(), now_iso()),
        )
        self.db.commit()
        self._changed("markets", series)

    def remove_market(self, series: str) -> None:
        self.db.execute("DELETE FROM markets WHERE series = ?", (series,))
        self.db.commit()
        self._changed("markets", series)

    def update_market(
        self, series: str, *, enabled: Optional[bool] = None, strategy: Optional[Strategy] = None
    ) -> None:
        if enabled is not None:
            self.db.execute("UPDATE markets SET enabled = ? WHERE series = ?", (int(enabled), series))
            if enabled:
                self.db.execute("UPDATE markets SET halted_at = NULL WHERE series = ?", (series,))
        if strategy is not None:
            self.db.execute(
                "UPDATE markets SET strategy = ? WHERE series = ?", (strategy.model_dump_json(), series)
            )
        self.db.commit()
        self._changed("markets", series)

    def halt_market(self, series: str) -> None:
        self.db.execute("UPDATE markets SET halted_at = ? WHERE series = ?", (now_iso(), series))
        self.db.commit()
        self._changed("markets", series)

    @staticmethod
    def _market(row: sqlite3.Row) -> dict[str, Any]:
        market = dict(row)
        market["enabled"] = bool(market["enabled"])
        market["strategy"] = Strategy.model_validate(json.loads(market["strategy"]))
        return market

    # --- trades -----------------------------------------------------------

    def has_trade(self, series: str, event_symbol: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM trades WHERE series = ? AND event_symbol = ?", (series, event_symbol)
        ).fetchone()
        return row is not None

    def insert_trade(self, **fields: Any) -> int:
        fields.setdefault("placed_at", now_iso())
        columns = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cursor = self.db.execute(
            f"INSERT INTO trades ({columns}) VALUES ({marks})", [_db(v) for v in fields.values()]
        )
        self.db.commit()
        self._changed("trades", cursor.lastrowid)
        return int(cursor.lastrowid)

    def update_trade(self, trade_id: int, **fields: Any) -> None:
        assignments = ", ".join(f"{name} = ?" for name in fields)
        self.db.execute(
            f"UPDATE trades SET {assignments} WHERE id = ?", [*(_db(v) for v in fields.values()), trade_id]
        )
        self.db.commit()
        self._changed("trades", trade_id)

    # --- exits ------------------------------------------------------------

    def insert_exit(self, **fields: Any) -> int:
        fields.setdefault("placed_at", now_iso())
        columns = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cursor = self.db.execute(
            f"INSERT INTO exits ({columns}) VALUES ({marks})", [_db(v) for v in fields.values()]
        )
        self.db.commit()
        self._changed("exits", cursor.lastrowid)
        return int(cursor.lastrowid)

    def update_exit(self, exit_id: int, **fields: Any) -> None:
        assignments = ", ".join(f"{name} = ?" for name in fields)
        self.db.execute(
            f"UPDATE exits SET {assignments} WHERE id = ?", [*(_db(v) for v in fields.values()), exit_id]
        )
        self.db.commit()
        self._changed("exits", exit_id)

    def exits_for(self, trade_id: int) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM exits WHERE trade_id = ? ORDER BY id", (trade_id,))
        return [dict(row) for row in rows.fetchall()]

    # --- trade history ----------------------------------------------------

    def trades(self, series: Optional[str] = None, limit: int = 200) -> list[dict[str, Any]]:
        sql = "SELECT * FROM trades"
        params: list[Any] = []
        if series:
            sql += " WHERE series = ?"
            params.append(series)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [dict(row) for row in self.db.execute(sql, params).fetchall()]

    def trade_for(self, series: str, event_symbol: str) -> Optional[dict[str, Any]]:
        """The market's trade in one window (there is at most one)."""
        row = self.db.execute(
            "SELECT * FROM trades WHERE series = ? AND event_symbol = ?", (series, event_symbol)
        ).fetchone()
        return dict(row) if row else None

    def open_trade(self, series: str) -> Optional[dict[str, Any]]:
        """The market's most recent trade that hasn't settled yet, if any."""
        row = self.db.execute(
            "SELECT * FROM trades WHERE series = ? AND result = 'PENDING' ORDER BY id DESC LIMIT 1", (series,)
        ).fetchone()
        return dict(row) if row else None

    def trades_to_track(self) -> list[dict[str, Any]]:
        """Trades whose order or settlement is still outstanding."""
        marks = ", ".join("?" for _ in FINAL_RESULTS)
        rows = self.db.execute(f"SELECT * FROM trades WHERE result NOT IN ({marks})", FINAL_RESULTS)
        return [dict(row) for row in rows.fetchall()]

    def pending_trades(self) -> list[dict[str, Any]]:
        """Trades whose result isn't known yet: their money is still at risk."""
        return [dict(row) for row in self.db.execute("SELECT * FROM trades WHERE result = 'PENDING'").fetchall()]

    def pnl_since(self, since: str) -> dict[str, Decimal]:
        """Net P&L per market of the trades that settled (or sold out) at or after `since`."""
        out: dict[str, Decimal] = {}
        rows = self.db.execute("SELECT series, pnl FROM trades WHERE settled_at >= ? AND pnl IS NOT NULL", (since,))
        for row in rows.fetchall():
            out[row["series"]] = out.get(row["series"], Decimal(0)) + Decimal(row["pnl"])
        return out

    def stats(self) -> dict[str, dict[str, Any]]:
        """Per-market performance, keyed by series."""
        out: dict[str, dict[str, Any]] = {}
        rows = self.db.execute("SELECT series, result, pnl, filled_quantity, avg_price FROM trades")
        for row in rows.fetchall():
            s = out.setdefault(
                row["series"],
                {"trades": 0, "wins": 0, "losses": 0, "pending": 0, "pnl": Decimal(0), "cost": Decimal(0)},
            )
            result = row["result"]
            if result in ("UNFILLED", "REJECTED", "ERROR"):
                continue
            s["trades"] += 1
            if result == "WON" or (result == "SOLD" and Decimal(row["pnl"]) > 0):
                s["wins"] += 1
            elif result in ("LOST", "SOLD"):
                s["losses"] += 1
            elif result == "PENDING":
                s["pending"] += 1
            if row["pnl"] is not None:
                s["pnl"] += Decimal(row["pnl"])
            if row["avg_price"] is not None:
                s["cost"] += Decimal(row["filled_quantity"]) * Decimal(row["avg_price"])
        return out


def _db(value: Any) -> Any:
    return str(value) if isinstance(value, Decimal) else value
