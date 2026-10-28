"""Per-user sessions: one bot per API key, run from the server's memory.

- A session is created when someone connects with an API key. Connecting again with
  the same key (a second tab, another device, or a later visit) rejoins that session
  instead of starting a second bot, which could double-buy and would split the
  account's rate limit.
- A running bot keeps trading with no tab open, until the user clicks Disconnect.
  A session that is idle (bot stopped, no tab attached) ends after `idle_seconds`,
  so abandoned sessions don't hold keys or capacity forever. So does one whose key
  Public keeps refusing (e.g. the user deleted it).
- Ending a session cancels its unfilled orders and drops everything: key, markets,
  rules and trade history.
- With a database (`persist`), sessions also survive restarts: a server shutting
  down only suspends them, and the next server resumes them, still trading.
  Without one, a restart ends every session.
"""

import asyncio
import hashlib
import logging
import secrets
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Callable, Optional

from public_api_sdk import AuthenticationError

from .engine import Engine
from .persist import Persister, SavedSession
from .store import Store

logger = logging.getLogger(__name__)

IDLE_SECONDS = 24 * 60 * 60.0
KEY_REFUSED_SECONDS = 120.0  # ride out a brief auth hiccup at Public before giving up on the key
SWEEP_SECONDS = 5.0
RESUME_TIMEOUT_SECONDS = 20.0  # per session, so one slow reconnect can't hold up the others
RESUME_RETRY_SECONDS = 30.0


class SessionLimitError(Exception):
    """The server is already running as many sessions as it allows."""


class AccountInUseError(Exception):
    """Another session (connected with a different key) is already trading this account."""


@dataclass(eq=False)
class Session:
    id: str  # random, unrelated to the token; identifies the session in the database
    token: str  # secret; authorises every request for this session
    key_hash: str
    engine: Engine
    task: Optional[asyncio.Task] = None
    sockets: int = 0
    idle_since: Optional[float] = None  # monotonic time; None while a tab is attached or the bot runs
    ended: bool = False
    end_reason: str = ""
    created_at: float = field(default_factory=time.monotonic)

    @property
    def label(self) -> str:
        """Short id for server logs."""
        return self.id[:6]


def _default_engine(label: str) -> Engine:
    return Engine(Store(), label=label)


class SessionManager:
    def __init__(
        self,
        max_sessions: int = 200,
        idle_seconds: float = IDLE_SECONDS,
        engine_factory: Callable[[str], Engine] = _default_engine,
        persister: Optional[Persister] = None,
    ) -> None:
        self.max_sessions = max_sessions
        self.idle_seconds = idle_seconds
        self.persister = persister
        # With a database, False until this server holds the lock and has resumed the saved sessions.
        self.ready = persister is None
        self._engine_factory = engine_factory
        self._by_token: dict[str, Session] = {}
        self._by_key: dict[str, Session] = {}
        self._connecting: dict[str, asyncio.Future[Session]] = {}

    def __len__(self) -> int:
        return len(self._by_token)

    def get(self, token: Optional[str]) -> Optional[Session]:
        session = self._by_token.get(token) if token else None
        return session if session and not session.ended else None

    async def connect(self, api_key: str) -> Session:
        """The key's session, creating it (and checking the key with Public) if needed.

        Raises the SDK's APIError if Public rejects the key, or SessionLimitError when full.
        """
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()
        existing = self._by_key.get(key_hash)
        if existing and not existing.ended:
            return existing
        # Two tabs connecting with the same key at once share one connection attempt.
        pending = self._connecting.get(key_hash)
        if pending is not None:
            return await asyncio.shield(pending)
        if len(self._by_token) >= self.max_sessions:
            raise SessionLimitError()

        future: asyncio.Future[Session] = asyncio.get_running_loop().create_future()
        self._connecting[key_hash] = future
        try:
            session_id = secrets.token_hex(8)
            engine = self._engine_factory(session_id[:6])
            await engine.connect(api_key)
            session = Session(id=session_id, token=secrets.token_urlsafe(32), key_hash=key_hash, engine=engine)
            if self.persister is not None:
                try:
                    await self.persister.add(session.id, key_hash, api_key, session.token)
                except BaseException:
                    await engine.disconnect()
                    raise
            self._start(session)
            logger.info("session %s started (%d active)", session.label, len(self._by_token))
            future.set_result(session)
            return session
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                future.cancel()
            else:
                future.set_exception(exc)
                future.exception()  # mark retrieved so a lone failure isn't logged as unhandled
            raise
        finally:
            del self._connecting[key_hash]

    def _start(self, session: Session) -> None:
        if self.persister is not None:
            self.persister.watch(session.id, session.engine.store)
            session.engine.checkpoint = self.persister.flush
        session.task = asyncio.create_task(session.engine.run(), name=f"engine-{session.label}")
        self._by_token[session.token] = session
        self._by_key[session.key_hash] = session

    async def resume(self) -> None:
        """With a database: wait until this is the only server running bots, then bring back every saved session.

        Meanwhile `ready` is False, and the app asks browsers to wait.
        """
        assert self.persister is not None
        await self.persister.backend.wait_for_lock()
        saved = await self.persister.load()
        await asyncio.gather(*(self._resume(s) for s in saved))
        self.ready = True
        logger.info("resumed %d of %d saved sessions", len(self._by_token), len(saved))

    async def _resume(self, saved: SavedSession) -> None:
        assert self.persister is not None
        engine = self._engine_factory(saved.id[:6])
        engine.store.load(saved.rows)
        session = Session(id=saved.id, token=saved.token, key_hash=saved.key_hash, engine=engine)
        try:
            await asyncio.wait_for(engine.resume(saved.api_key), RESUME_TIMEOUT_SECONDS)
        except AuthenticationError:
            logger.info("session %s: Public no longer accepts its key; ending it", session.label)
            engine.store.close()
            await self.persister.remove(saved.id)
            return
        except Exception:
            # Public is unreachable or slow: keep the session, and keep trying in the background.
            logger.warning("session %s: couldn't reconnect to Public; retrying", session.label, exc_info=True)
            asyncio.create_task(self._retry_resume(session, saved.api_key), name=f"resume-{session.label}")
        self._start(session)

    async def _retry_resume(self, session: Session, api_key: str) -> None:
        while not session.ended:
            await asyncio.sleep(RESUME_RETRY_SECONDS)
            if session.ended:
                return
            try:
                await asyncio.wait_for(session.engine.resume(api_key), RESUME_TIMEOUT_SECONDS)
                logger.info("session %s: reconnected to Public", session.label)
                return
            except AuthenticationError:
                await self.end(session, "Public stopped accepting the API key")
                return
            except Exception:
                logger.warning("session %s: still can't reconnect to Public", session.label, exc_info=True)

    def check_account(self, session: Session, account_id: str) -> None:
        """Refuse an account another session already trades: two bots could double-buy."""
        for other in self._by_token.values():
            if other is not session and not other.ended and other.engine.account_id == account_id:
                raise AccountInUseError()

    def attach(self, session: Session) -> None:
        session.sockets += 1

    def detach(self, session: Session) -> None:
        session.sockets = max(0, session.sockets - 1)

    async def end(self, session: Session, reason: str) -> None:
        if session.ended:
            return
        session.ended, session.end_reason = True, reason
        self._by_token.pop(session.token, None)
        if self._by_key.get(session.key_hash) is session:
            del self._by_key[session.key_hash]
        if session.task is not None:
            session.task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await session.task
        try:
            await session.engine.shutdown()
        except Exception:
            logger.exception("session %s: shutdown failed", session.label)
        session.engine.store.close()
        if self.persister is not None:
            await self.persister.remove(session.id)
        logger.info("session %s ended: %s (%d active)", session.label, reason, len(self._by_token))

    async def sweep(self) -> None:
        now = time.monotonic()
        for session in list(self._by_token.values()):
            if session.sockets or session.engine.running:
                session.idle_since = None
            elif session.idle_since is None:
                session.idle_since = now
            if session.idle_since is not None and now - session.idle_since >= self.idle_seconds:
                await self.end(session, "it was idle with the bot stopped")
            elif session.engine.key_refused_seconds() >= KEY_REFUSED_SECONDS:
                await self.end(session, "Public stopped accepting the API key")
            elif session.task is not None and session.task.done():
                # The engine loop catches its own errors, so this should never happen.
                await self.end(session, "the trading loop stopped unexpectedly")

    async def run_sweeper(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_SECONDS)
            try:
                await self.sweep()
            except Exception:
                logger.exception("session sweep failed")

    async def end_all(self, reason: str) -> None:
        await asyncio.gather(*(self.end(s, reason) for s in list(self._by_token.values())))

    async def suspend_all(self) -> None:
        """Server shutdown with a database: stop the bots here, keeping their sessions for the next server.

        Unfilled orders stay open at Public; the next server tracks them from the saved records.
        """
        sessions = list(self._by_token.values())
        for session in sessions:
            if session.task is not None:
                session.task.cancel()
        for session in sessions:
            if session.task is not None:
                with suppress(asyncio.CancelledError, Exception):
                    await session.task
        if self.persister is not None:
            try:
                await self.persister.flush()
            except Exception:
                logger.exception("saving sessions at shutdown failed")
        await asyncio.gather(*(s.engine.disconnect() for s in sessions), return_exceptions=True)
        logger.info("suspended %d sessions for the next server", len(sessions))
