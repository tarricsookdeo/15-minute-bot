"""Saved sessions: encrypted at rest, mirrored as they change, resumed by the next server, deleted when they end."""

import asyncio

import pytest
from cryptography.fernet import Fernet

from fifteen_bot.persist import Cipher, Persister, SqliteBackend
from fifteen_bot.sessions import SessionManager
from fifteen_bot.store import Store
from tests.test_sessions import FakeEngine

KEY = Fernet.generate_key().decode()


def persister(path):
    return Persister(SqliteBackend(str(path)), Cipher(KEY))


async def open_manager(path, **kwargs):
    FakeEngine.instances = []
    p = persister(path)
    await p.open()
    m = SessionManager(engine_factory=FakeEngine, persister=p, **kwargs)
    await m.resume()
    return m


async def restart(m, path):
    """Stop the app the way Ctrl+C does, and start it again on the same database."""
    await m.suspend_all()
    await m.persister.close()
    return await open_manager(path)


def test_cipher_round_trips_and_accepts_rotated_keys():
    old = Cipher(KEY)
    secret = old.encrypt("my-api-key")
    assert secret != "my-api-key" and old.decrypt(secret) == "my-api-key"
    rotated = Cipher(f"{Fernet.generate_key().decode()},{KEY}")  # new key first, old one still accepted
    assert rotated.decrypt(secret) == "my-api-key"
    with pytest.raises(ValueError):
        Cipher(" , ")


def test_restarting_with_a_rotated_key_re_encrypts_saved_sessions(tmp_path):
    db = tmp_path / "bot.db"
    new_key = Fernet.generate_key().decode()

    async def run():
        m = await open_manager(db)
        token = (await m.connect("key-a")).token
        await m.suspend_all()
        await m.persister.close()
        for keys in (f"{new_key},{KEY}", new_key):  # rotate, then retire the old key
            p = Persister(SqliteBackend(str(db)), Cipher(keys))
            await p.open()
            m = SessionManager(engine_factory=FakeEngine, persister=p)
            await m.resume()
            found = m.get(token)
            await m.suspend_all()
            await p.close()
        return found

    assert asyncio.run(run()) is not None


def test_store_load_restores_what_dump_reported():
    store = Store()
    store.set_setting("onboarded", "1")
    store.add_market("KX", "BTC", "Crypto")
    store.insert_trade(
        series="KX", event_symbol="E1", side="YES", contract_symbol="c", quantity="1", limit_price="0.85",
        order_id="o1", order_status="FILLED", close_time="2030-01-01T00:00:00+00:00",
    )
    copy = Store()
    copy.load([(table, row) for table, _, row in store.dump()])
    assert list(copy.dump()) == list(store.dump())


def test_store_reports_each_change_once():
    store = Store()
    seen = []
    store.watch(lambda *change: seen.append(change))
    store.set_setting("running", "1")
    store.set_setting("running", "1")  # unchanged: not reported again
    store.add_market("KX", "BTC", None)
    store.remove_market("KX")
    assert [(table, key, row is None) for table, key, row in seen] == [
        ("settings", "running", False),
        ("markets", "KX", False),
        ("markets", "KX", True),
    ]


def test_a_session_survives_a_restart_with_its_token_settings_and_history(tmp_path):
    db = tmp_path / "bot.db"

    async def run():
        m = await open_manager(db)
        s = await m.connect("key-a")
        store = s.engine.store
        store.set_setting("account_id", "ACC1")
        store.set_setting("running", "1")
        store.add_market("KX", "BTC", None)
        store.insert_trade(
            series="KX", event_symbol="E1", side="YES", contract_symbol="c", quantity="1", limit_price="0.85",
            order_id="o1", order_status="NEW", close_time="2030-01-01T00:00:00+00:00",
        )
        token, id = s.token, s.id

        m2 = await restart(m, db)
        resumed = m2.get(token)
        return m2, resumed, id

    m2, resumed, id = asyncio.run(run())
    assert resumed is not None and resumed.id == id and m2.ready
    engine = resumed.engine
    assert engine.resumed_with == "key-a" and engine.account_id == "ACC1" and engine.running
    assert [m["series"] for m in engine.store.list_markets()] == ["KX"]
    assert engine.store.trades()[0]["order_id"] == "o1"


def test_keys_and_tokens_are_stored_encrypted(tmp_path):
    db = tmp_path / "bot.db"

    async def run():
        m = await open_manager(db)
        s = await m.connect("super-secret-key")
        await m.persister.flush()
        return s.token

    token = asyncio.run(run())
    raw = db.read_bytes()
    assert b"super-secret-key" not in raw and token.encode() not in raw


def test_ending_a_session_deletes_everything_it_saved(tmp_path):
    db = tmp_path / "bot.db"

    async def run():
        m = await open_manager(db)
        s = await m.connect("key-a")
        s.engine.store.add_market("KX", "BTC", None)
        await m.persister.flush()
        await m.end(s, "signed out")
        await m.persister.flush()
        m2 = await restart(m, db)
        return len(m2), m2.persister.backend.db.execute("SELECT COUNT(*) FROM bot_rows").fetchone()[0]

    assert asyncio.run(run()) == (0, 0)


def test_a_saved_session_whose_key_public_now_refuses_is_dropped(tmp_path):
    db = tmp_path / "bot.db"

    async def run():
        p = persister(db)
        await p.open()
        await p.add("deadbeef00000000", "hash", "bad", "some-token")  # e.g. the user deleted the key at Public
        await p.close()
        m = await open_manager(db)
        return len(m), m.ready, await m.persister.load()

    assert asyncio.run(run()) == (0, True, [])


def test_a_saved_session_is_kept_while_public_is_unreachable(tmp_path):
    db = tmp_path / "bot.db"

    async def run():
        p = persister(db)
        await p.open()
        await p.add("deadbeef00000000", "hash", "public-down", "some-token")
        await p.close()
        m = await open_manager(db)
        return m.get("some-token"), m.ready

    session, ready = asyncio.run(run())
    assert ready and session is not None and session.engine.client is None  # retried in the background


def test_a_second_copy_on_the_same_file_waits_for_the_first_to_stop(tmp_path):
    db = tmp_path / "bot.db"

    async def run():
        first = await open_manager(db)
        second = SessionManager(engine_factory=FakeEngine, persister=persister(db))
        await second.persister.open()
        taking_over = asyncio.create_task(second.resume())
        await asyncio.sleep(1.5)
        waited = not taking_over.done() and not second.ready
        await first.suspend_all()
        await first.persister.close()  # releases the lock file
        await asyncio.wait_for(taking_over, 5)
        ready = second.ready
        await second.persister.close()
        return waited, ready

    assert asyncio.run(run()) == (True, True)
