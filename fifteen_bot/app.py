"""Web server: the dashboard, a JSON API and a WebSocket, with one in-memory bot per user.

Every API call carries the caller's session token in the `X-Session` header; the
WebSocket sends it as its first message. Tokens live in the browser's localStorage,
so a returning user rejoins their running bot without the key, and never in cookies
or URLs, so other websites can't act for a user.

With DATABASE_URL set, sessions are saved (see `persist`) and survive restarts: while
a newly started server waits to take over from the old one, the API answers 503
"restarting" and the page keeps retrying.
"""

import asyncio
import logging
import os
import signal
import time
from collections import deque
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, Any, Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from public_api_sdk import APIError, AuthenticationError
from pydantic import BaseModel, Field

from .persist import Cipher, Persister, backend_for
from .sessions import AccountInUseError, Session, SessionLimitError, SessionManager
from .strategy import Strategy

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATE_PUSH_SECONDS = 2.0
CONNECT_ATTEMPTS_PER_MINUTE = 10  # per IP address: slows key guessing and abuse
WS_HELLO_TIMEOUT_SECONDS = 10.0
MAX_MARKETS_PER_SESSION = 50
LOCK_CHECK_SECONDS = 10.0
RESTARTING = "The bot is restarting. It'll be back in a moment."


def _persister() -> Optional[Persister]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        return None
    keys = os.environ.get("SESSION_ENCRYPTION_KEY")
    if not keys:
        raise RuntimeError("DATABASE_URL is set, so SESSION_ENCRYPTION_KEY must be too (see the README)")
    return Persister(backend_for(url), Cipher(keys))


sessions = SessionManager(max_sessions=int(os.environ.get("MAX_SESSIONS", "200")), persister=_persister())


@asynccontextmanager
async def lifespan(_: FastAPI):
    persister = sessions.persister
    tasks = [asyncio.create_task(sessions.run_sweeper())]
    if persister is not None:
        await persister.open()
        # In the background, so the app starts answering (the page shows "restarting") while it
        # waits for any other copy using the same database to stop.
        tasks += [asyncio.create_task(_take_over()), asyncio.create_task(persister.run())]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError, Exception):
                await task
        if persister is not None:
            # Keep every session for the next server; their bots resume there.
            await sessions.suspend_all()
            await persister.close()
        else:
            # Nothing is saved, so a restart ends every session; cancel their unfilled orders on the way out.
            await sessions.end_all("the server restarted")


async def _take_over() -> None:
    """Become the server that runs the bots, then make sure it stays the only one."""
    assert sessions.persister is not None
    try:
        await sessions.resume()
    except Exception:
        # E.g. the database dropped mid-load. Exit, so the platform restarts us and we try again.
        logger.critical("taking over the saved sessions failed; shutting down", exc_info=True)
        os.kill(os.getpid(), signal.SIGTERM)
        return
    while True:
        await asyncio.sleep(LOCK_CHECK_SECONDS)
        if not await sessions.persister.backend.lock_held():
            # Another server may take over now; stop trading here at once and let the platform restart us.
            logger.critical("lost the database lock; shutting down")
            os.kill(os.getpid(), signal.SIGTERM)
            return


app = FastAPI(title="15-Minute Bot", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    else:
        # Revalidate the page's files on every load, so after an update the browser never
        # mixes a cached old index.html with a new app.js.
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/healthz")
async def healthz():
    return {"ok": True, "ready": sessions.ready, "sessions": len(sessions)}


@app.get("/api/info")
async def info():
    """What the page should tell users about how their key is kept."""
    return {"saves_sessions": sessions.persister is not None}


def _require_ready() -> None:
    if not sessions.ready:
        raise HTTPException(503, RESTARTING, headers={"Retry-After": "2"})


# --- sessions ----------------------------------------------------------------

_connect_attempts: dict[str, deque[float]] = {}


def _check_connect_rate(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    attempts = _connect_attempts.setdefault(ip, deque())
    while attempts and now - attempts[0] > 60:
        attempts.popleft()
    if len(attempts) >= CONNECT_ATTEMPTS_PER_MINUTE:
        raise HTTPException(429, "Too many connection attempts. Wait a minute and try again.")
    attempts.append(now)
    if len(_connect_attempts) > 10_000:  # forget idle addresses so this can't grow forever
        for key in [k for k, v in _connect_attempts.items() if not v or now - v[-1] > 60]:
            del _connect_attempts[key]


def current_session(x_session: Annotated[Optional[str], Header()] = None) -> Session:
    _require_ready()
    session = sessions.get(x_session)
    if session is None:
        raise HTTPException(401, "Your session has ended. Connect again to continue.")
    return session


CurrentSession = Annotated[Session, Depends(current_session)]


def session_info(session: Session) -> dict[str, Any]:
    engine = session.engine
    return {
        "connected": engine.client is not None,
        "account_id": engine.account_id,
        "accounts": [{"id": a.account_id, "type": a.account_type.value} for a in engine.accounts],
        "onboarded": engine.store.get_setting("onboarded") == "1",
    }


class ConnectBody(BaseModel):
    api_key: str = Field(min_length=1, max_length=512)


@app.post("/api/connect")
async def connect(body: ConnectBody, request: Request):
    _require_ready()
    _check_connect_rate(request)
    try:
        session = await sessions.connect(body.api_key.strip())
    except AuthenticationError:
        raise HTTPException(
            401,
            "Public didn't accept that key. Check that you copied the whole key "
            "and that it hasn't been deleted in Public's API settings.",
        )
    except SessionLimitError:
        raise HTTPException(503, "The bot is at capacity right now. Please try again later.")
    except APIError as exc:
        raise HTTPException(502, f"Public returned an error ({exc}). Please try again in a moment.")
    except httpx.TransportError:
        raise HTTPException(502, "Couldn't reach Public. Please try again in a moment.")
    return {"token": session.token, **session_info(session)}


@app.get("/api/session")
async def get_session(session: CurrentSession):
    return session_info(session)


@app.delete("/api/session")
async def end_session(session: CurrentSession):
    await sessions.end(session, "signed out")
    return {"ended": True}


class AccountBody(BaseModel):
    account_id: str = Field(max_length=64)


@app.post("/api/account")
async def select_account(body: AccountBody, session: CurrentSession):
    if not any(a.account_id == body.account_id for a in session.engine.accounts):
        # Checked before the in-use check, so nobody learns whether someone else's account is active here.
        raise HTTPException(400, "Unknown account")
    try:
        sessions.check_account(session, body.account_id)
        session.engine.select_account(body.account_id)
    except AccountInUseError:
        raise HTTPException(409, "This account is already being traded in another session. Close that one first.")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return session_info(session)


@app.websocket("/ws")
async def live(websocket: WebSocket):
    """Pushes the dashboard state every couple of seconds; an open socket keeps an idle session alive.

    No cookies are involved, so another site's page can't use this socket: it would
    need the session token, which only the user's own tab has.
    """
    await websocket.accept()
    try:
        hello = await asyncio.wait_for(websocket.receive_json(), WS_HELLO_TIMEOUT_SECONDS)
    except (asyncio.TimeoutError, WebSocketDisconnect, ValueError):
        await websocket.close(code=1008)
        return
    if not sessions.ready:
        await websocket.send_json({"type": "restarting", "reason": RESTARTING})
        await websocket.close()
        return
    session = sessions.get(hello.get("session") if isinstance(hello, dict) else None)
    if session is None:
        await websocket.send_json({"type": "ended", "reason": "Your session has ended. Connect again to continue."})
        await websocket.close()
        return

    sessions.attach(session)
    listener = asyncio.create_task(_until_closed(websocket))
    try:
        while not session.ended and not listener.done():
            state = jsonable_encoder(session.engine.snapshot())
            await websocket.send_json({"type": "state", "state": state})
            await asyncio.wait({listener}, timeout=STATE_PUSH_SECONDS)
        if session.ended and not listener.done():
            await websocket.send_json(
                {"type": "ended", "reason": f"Your session ended ({session.end_reason}). Connect again to continue."}
            )
            await websocket.close()
    except (WebSocketDisconnect, RuntimeError):
        pass  # the tab went away mid-send
    finally:
        listener.cancel()
        sessions.detach(session)


async def _until_closed(websocket: WebSocket) -> None:
    with suppress(WebSocketDisconnect):
        while True:
            await websocket.receive_text()


# --- engine ------------------------------------------------------------------


class EngineBody(BaseModel):
    running: bool


@app.post("/api/engine")
async def set_engine(body: EngineBody, session: CurrentSession):
    try:
        session.engine.set_running(body.running)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return session.engine.snapshot()


@app.get("/api/state")
async def state(session: CurrentSession):
    return session.engine.snapshot()


@app.get("/api/trades")
async def trades(session: CurrentSession, series: Optional[str] = None, limit: int = 200):
    return session.engine.store.trades(series, min(max(limit, 1), 1000))


# --- markets -----------------------------------------------------------------


async def _known_markets(session: Session) -> dict[str, dict[str, Any]]:
    """Public's current 15-minute markets: names and categories come from Public, not the browser."""
    if session.engine.client is None:
        raise HTTPException(409, "Not connected to Public")
    try:
        return {m["series"]: m for m in await session.engine.available_markets()}
    except APIError as exc:
        raise HTTPException(502, f"Public API error: {exc}")


def _add_markets(session: Session, series_list: list[str], known: dict[str, dict[str, Any]]) -> None:
    store = session.engine.store
    unknown = [s for s in series_list if s not in known]
    if unknown:
        raise HTTPException(400, f"Unknown market: {', '.join(unknown)}")
    if len({m["series"] for m in store.list_markets()} | set(series_list)) > MAX_MARKETS_PER_SESSION:
        raise HTTPException(400, f"You can trade at most {MAX_MARKETS_PER_SESSION} markets at once.")
    for series in series_list:
        store.add_market(series, known[series]["name"], known[series]["category"])


@app.get("/api/markets/available")
async def available_markets(session: CurrentSession):
    return list((await _known_markets(session)).values())


class AddMarketBody(BaseModel):
    series: str = Field(max_length=64)


@app.post("/api/markets")
async def add_market(body: AddMarketBody, session: CurrentSession):
    _add_markets(session, [body.series], await _known_markets(session))
    return session.engine.snapshot()


class UpdateMarketBody(BaseModel):
    enabled: Optional[bool] = None
    strategy: Optional[Strategy] = None


@app.put("/api/markets/{series}")
async def update_market(series: str, body: UpdateMarketBody, session: CurrentSession):
    store = session.engine.store
    if store.get_market(series) is None:
        raise HTTPException(404, "Market not selected")
    store.update_market(series, enabled=body.enabled, strategy=body.strategy)
    return session.engine.snapshot()


class BulkStrategyBody(BaseModel):
    series: list[str] = Field(max_length=MAX_MARKETS_PER_SESSION)
    strategy: Strategy


@app.post("/api/markets/strategy")
async def apply_strategy(body: BulkStrategyBody, session: CurrentSession):
    for series in body.series:
        session.engine.store.update_market(series, strategy=body.strategy)
    return session.engine.snapshot()


class OnboardingBody(BaseModel):
    series: list[str] = Field(min_length=1, max_length=MAX_MARKETS_PER_SESSION)
    strategy: Strategy
    start: bool = False


@app.post("/api/onboarding")
async def finish_onboarding(body: OnboardingBody, session: CurrentSession):
    """Save the setup wizard's choices in one go."""
    engine = session.engine
    if engine.account_id is None:
        raise HTTPException(400, "Choose an account first")
    _add_markets(session, body.series, await _known_markets(session))
    for series in body.series:
        engine.store.update_market(series, enabled=True, strategy=body.strategy)
    engine.store.set_setting("onboarded", "1")
    if body.start:
        engine.set_running(True)
    return engine.snapshot()


@app.delete("/api/markets/{series}")
async def remove_market(series: str, session: CurrentSession):
    session.engine.store.remove_market(series)
    session.engine.windows.pop(series, None)
    return session.engine.snapshot()


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    # Log the details server-side; never send internals (or anything key-related) to the browser.
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse({"detail": "Something went wrong. Please try again."}, status_code=500)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
