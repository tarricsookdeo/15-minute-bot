"""Keeps sessions through restarts and deploys: a durable copy of every session in a database.

Each session's working data stays in its in-memory Store, which is fast and what
the engine reads. Every change to it is copied here, so a restarted server loads
the sessions back and their bots carry on trading.

- The API key and the session token are encrypted (Fernet, with SESSION_ENCRYPTION_KEY)
  before they're stored; the database never holds them in the clear.
- Changes are written in batches every half second. The engine calls `flush` before
  it sends an order, so the order is always on record before it can exist at Public:
  a crash can't make the next server place it a second time.
- Only one server may run the bots, or two could trade one account. On Postgres a
  server first waits for an advisory lock, so during a deploy the new server takes
  over only once the old one has stopped.
- Ending a session (Disconnect, idle expiry, a refused key) deletes all of it.

DATABASE_URL picks the database: postgres://… (hosted) or sqlite:///path (local runs, tests).
"""

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from cryptography.fernet import Fernet, MultiFernet

from .store import Store

logger = logging.getLogger(__name__)

FLUSH_SECONDS = 0.5
LOCK_ID = 15_900_015  # any constant; identifies this app's advisory lock

SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_sessions (
    id TEXT PRIMARY KEY,
    key_hash TEXT NOT NULL UNIQUE,
    api_key TEXT NOT NULL,  -- encrypted
    token TEXT NOT NULL,  -- encrypted
    created_at TEXT NOT NULL
);
-- Every row of every session's Store, as JSON.
CREATE TABLE IF NOT EXISTS bot_rows (
    session_id TEXT NOT NULL REFERENCES bot_sessions (id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    row_key TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (session_id, kind, row_key)
);
"""

Row = tuple[str, str, str]  # (session id, Store table, row key)


class Cipher:
    """Encrypts with the first of SESSION_ENCRYPTION_KEY's comma-separated keys and decrypts with
    any of them, so a key can be rotated by putting the new one first."""

    def __init__(self, keys: str) -> None:
        fernets = [Fernet(k.strip()) for k in keys.split(",") if k.strip()]
        if not fernets:
            raise ValueError("SESSION_ENCRYPTION_KEY is empty")
        self._fernet = MultiFernet(fernets)

    def encrypt(self, text: str) -> str:
        return self._fernet.encrypt(text.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self._fernet.decrypt(token.encode()).decode()

    def rotate(self, token: str) -> str:
        """The same secret, encrypted with the first key."""
        return self._fernet.rotate(token.encode()).decode()


@dataclass
class SavedSession:
    id: str
    key_hash: str
    api_key: str
    token: str
    rows: list[tuple[str, dict[str, Any]]]  # (Store table, row), for Store.load


class Backend:
    """The database. Subclasses implement storage; locking is a no-op unless several servers could run."""

    async def open(self) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError

    async def wait_for_lock(self) -> None:
        """Return once this is the only server running the bots."""

    async def lock_held(self) -> bool:
        return True

    async def load(self) -> list[tuple[tuple[str, str, str, str], list[tuple[str, str, str]]]]:
        """Each session as ((id, key_hash, api_key, token), [(kind, row_key, data), ...])."""
        raise NotImplementedError

    async def add_session(self, id: str, key_hash: str, api_key: str, token: str) -> None:
        raise NotImplementedError

    async def delete_session(self, id: str) -> None:
        raise NotImplementedError

    async def rekey(self, id: str, api_key: str, token: str) -> None:
        """Replace a session's encrypted key and token (after rotating the encryption key)."""
        raise NotImplementedError

    async def write(self, upserts: list[tuple[str, str, str, str]], deletes: list[Row]) -> None:
        """Save rows (session id, kind, row key, data) and delete others, all or nothing."""
        raise NotImplementedError


class SqliteBackend(Backend):
    """A local file: sessions survive restarting a local run. One process at a time."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.db: Optional[sqlite3.Connection] = None

    async def open(self) -> None:
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        self.db.commit()

    async def close(self) -> None:
        if self.db is not None:
            self.db.close()
            self.db = None

    async def load(self):
        assert self.db is not None
        sessions = self.db.execute("SELECT id, key_hash, api_key, token FROM bot_sessions").fetchall()
        out = []
        for session in sessions:
            rows = self.db.execute(
                "SELECT kind, row_key, data FROM bot_rows WHERE session_id = ?", (session[0],)
            ).fetchall()
            out.append((tuple(session), [tuple(r) for r in rows]))
        return out

    async def add_session(self, id, key_hash, api_key, token) -> None:
        assert self.db is not None
        with self.db:
            self.db.execute(
                "INSERT INTO bot_sessions (id, key_hash, api_key, token, created_at) VALUES (?, ?, ?, ?, ?)",
                (id, key_hash, api_key, token, _now()),
            )

    async def delete_session(self, id) -> None:
        assert self.db is not None
        with self.db:
            self.db.execute("DELETE FROM bot_sessions WHERE id = ?", (id,))

    async def rekey(self, id, api_key, token) -> None:
        assert self.db is not None
        with self.db:
            self.db.execute("UPDATE bot_sessions SET api_key = ?, token = ? WHERE id = ?", (api_key, token, id))

    async def write(self, upserts, deletes) -> None:
        assert self.db is not None
        with self.db:
            self.db.executemany(
                "INSERT INTO bot_rows (session_id, kind, row_key, data) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (session_id, kind, row_key) DO UPDATE SET data = excluded.data",
                upserts,
            )
            self.db.executemany("DELETE FROM bot_rows WHERE session_id = ? AND kind = ? AND row_key = ?", deletes)


class PostgresBackend(Backend):
    """The hosted database. Holds an advisory lock on its own connection for as long as the server runs."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._pool: Any = None
        self._lock_conn: Any = None

    async def open(self) -> None:
        from psycopg_pool import AsyncConnectionPool

        self._pool = AsyncConnectionPool(self.url, min_size=1, max_size=2, open=False)
        await self._pool.open(wait=True, timeout=30)
        async with self._pool.connection() as conn:
            await conn.execute(SCHEMA)

    async def close(self) -> None:
        if self._lock_conn is not None:
            await self._lock_conn.close()  # releases the lock for the next server
            self._lock_conn = None
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def wait_for_lock(self) -> None:
        import psycopg

        self._lock_conn = await psycopg.AsyncConnection.connect(self.url, autocommit=True)
        await self._lock_conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))

    async def lock_held(self) -> bool:
        try:
            await self._lock_conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    async def load(self):
        async with self._pool.connection() as conn:
            sessions = await (await conn.execute("SELECT id, key_hash, api_key, token FROM bot_sessions")).fetchall()
            rows = await (await conn.execute("SELECT session_id, kind, row_key, data FROM bot_rows")).fetchall()
        by_session: dict[str, list[tuple[str, str, str]]] = {}
        for session_id, kind, row_key, data in rows:
            by_session.setdefault(session_id, []).append((kind, row_key, data))
        return [(tuple(s), by_session.get(s[0], [])) for s in sessions]

    async def add_session(self, id, key_hash, api_key, token) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO bot_sessions (id, key_hash, api_key, token, created_at) VALUES (%s, %s, %s, %s, %s)",
                (id, key_hash, api_key, token, _now()),
            )

    async def delete_session(self, id) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("DELETE FROM bot_sessions WHERE id = %s", (id,))

    async def rekey(self, id, api_key, token) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("UPDATE bot_sessions SET api_key = %s, token = %s WHERE id = %s", (api_key, token, id))

    async def write(self, upserts, deletes) -> None:
        async with self._pool.connection() as conn:  # one transaction
            async with conn.cursor() as cur:
                if upserts:
                    await cur.executemany(
                        "INSERT INTO bot_rows (session_id, kind, row_key, data) VALUES (%s, %s, %s, %s) "
                        "ON CONFLICT (session_id, kind, row_key) DO UPDATE SET data = EXCLUDED.data",
                        upserts,
                    )
                if deletes:
                    await cur.executemany(
                        "DELETE FROM bot_rows WHERE session_id = %s AND kind = %s AND row_key = %s", deletes
                    )


def backend_for(url: str) -> Backend:
    if url.startswith(("postgres://", "postgresql://")):
        return PostgresBackend(url)
    if url.startswith("sqlite:///"):
        return SqliteBackend(url.removeprefix("sqlite:///"))
    raise ValueError("DATABASE_URL must start with postgresql:// or sqlite:///")


class Persister:
    """Copies sessions and their Store changes to the database, in batches."""

    def __init__(self, backend: Backend, cipher: Cipher) -> None:
        self.backend = backend
        self.cipher = cipher
        self._pending: dict[Row, Optional[str]] = {}  # latest data per row; None deletes it
        self._ended: set[str] = set()  # sessions deleted here: ignore their late changes
        self._undeleted: set[str] = set()  # sessions whose deletion failed, retried on each flush
        self._lock = asyncio.Lock()  # one database operation at a time

    async def open(self) -> None:
        await self.backend.open()

    async def close(self) -> None:
        await self.backend.close()

    async def load(self) -> list[SavedSession]:
        saved = []
        for (id, key_hash, api_key, token), rows in await self.backend.load():
            try:
                rotated = self.cipher.rotate(api_key), self.cipher.rotate(token)
            except Exception:
                # Encrypted with a key that's no longer configured: it can never be resumed.
                logger.error("session %s: can't decrypt its key with SESSION_ENCRYPTION_KEY; deleting it", id[:6])
                await self.backend.delete_session(id)
                continue
            # Re-encrypted with the first (newest) key, so an old key can be retired after one restart.
            await self.backend.rekey(id, *rotated)
            api_key, token = self.cipher.decrypt(rotated[0]), self.cipher.decrypt(rotated[1])
            saved.append(SavedSession(id, key_hash, api_key, token, [(kind, json.loads(data)) for kind, _, data in rows]))
        return saved

    async def add(self, id: str, key_hash: str, api_key: str, token: str) -> None:
        async with self._lock:
            await self.backend.add_session(id, key_hash, self.cipher.encrypt(api_key), self.cipher.encrypt(token))

    def watch(self, id: str, store: Store) -> None:
        """Copy all of `store` now, then every change to it."""

        def changed(table: str, key: str, row: Optional[dict[str, Any]]) -> None:
            if id not in self._ended:
                self._pending[(id, table, key)] = None if row is None else json.dumps(row)

        store.watch(changed)

    async def remove(self, id: str) -> None:
        """Delete a session and everything it stored."""
        self._ended.add(id)
        for row in [r for r in self._pending if r[0] == id]:
            del self._pending[row]
        async with self._lock:
            try:
                await self.backend.delete_session(id)
            except Exception:
                logger.exception("session %s: deleting it failed; will retry", id[:6])
                self._undeleted.add(id)

    async def flush(self) -> None:
        """Write every change made so far. Raises if the database can't be reached."""
        async with self._lock:
            for id in list(self._undeleted):
                await self.backend.delete_session(id)
                self._undeleted.discard(id)
            if not self._pending:
                return
            batch, self._pending = self._pending, {}
            upserts = [(*row, data) for row, data in batch.items() if data is not None]
            deletes = [row for row, data in batch.items() if data is None]
            try:
                await self.backend.write(upserts, deletes)
            except BaseException:
                for row, data in batch.items():  # keep them for the next try; newer changes win
                    if row[0] not in self._ended:
                        self._pending.setdefault(row, data)
                raise

    async def run(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_SECONDS)
            try:
                await self.flush()
            except Exception:
                logger.exception("saving sessions failed; will retry")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
