"""The Postgres backend against a real database. Runs when TEST_DATABASE_URL is set (CI provides one)."""

import asyncio
import os

import pytest
from cryptography.fernet import Fernet

from fifteen_bot.persist import Cipher, Persister, PostgresBackend
from fifteen_bot.sessions import SessionManager
from tests.test_sessions import FakeEngine

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="set TEST_DATABASE_URL to a Postgres database")
KEY = Fernet.generate_key().decode()


async def clean():
    backend = PostgresBackend(URL)
    await backend.open()
    async with backend._pool.connection() as conn:
        await conn.execute("DELETE FROM bot_sessions")
    await backend.close()


async def server():
    FakeEngine.instances = []
    p = Persister(PostgresBackend(URL), Cipher(KEY))
    await p.open()
    return SessionManager(engine_factory=FakeEngine, persister=p)


def test_sessions_survive_a_restart_on_postgres():
    async def run():
        await clean()
        old = await server()
        await old.resume()
        s = await old.connect("key-a")
        s.engine.store.add_market("KX", "BTC", None)
        s.engine.store.set_setting("running", "1")
        await old.suspend_all()
        await old.persister.close()

        new = await server()
        await new.resume()
        resumed = new.get(s.token)
        state = resumed.engine.running, [m["series"] for m in resumed.engine.store.list_markets()]
        await new.end(resumed, "signed out")
        left = await new.persister.load()
        await new.persister.close()
        return state, left

    state, left = asyncio.run(run())
    assert state == (True, ["KX"]) and left == []


def test_the_next_server_waits_for_the_old_one_to_stop():
    async def run():
        await clean()
        old = await server()
        await old.resume()
        new = await server()
        taking_over = asyncio.create_task(new.resume())
        await asyncio.sleep(0.5)
        waited = not taking_over.done() and not new.ready
        await old.suspend_all()
        await old.persister.close()  # releases the lock
        await asyncio.wait_for(taking_over, 10)
        ready = new.ready
        await new.persister.close()
        return waited, ready

    assert asyncio.run(run()) == (True, True)


def test_connections_the_database_drops_are_replaced():
    async def run():
        await clean()
        backend = PostgresBackend(URL)
        await backend.open()
        await backend.wait_for_lock()
        # What the database does on a restart or a trusted-sources change: end every other connection.
        killer = PostgresBackend(URL)
        await killer.open()
        async with killer._pool.connection() as conn:
            await conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid()"
            )
        await killer.close()
        await backend.add_session("s1", "hash", "key", "token")  # a fresh connection, not an error
        lock_held = await backend.lock_held()  # the lock went with its connection
        saved = [s for s, _ in await backend.load()]
        await backend.close()
        return saved, lock_held

    saved, lock_held = asyncio.run(run())
    assert saved == [("s1", "hash", "key", "token")] and lock_held is False
