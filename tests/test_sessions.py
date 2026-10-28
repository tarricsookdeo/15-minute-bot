import asyncio

import pytest
from public_api_sdk import AuthenticationError

from fifteen_bot.sessions import AccountInUseError, SessionLimitError, SessionManager
from fifteen_bot.store import Store


class FakeEngine:
    instances: list["FakeEngine"] = []

    def __init__(self, label):
        self.label = label
        self.store = Store()
        self.client = None
        self.account_id = None
        self.accounts = []
        self.running = False
        self.refused_for = 0.0
        self.shut_down = False
        FakeEngine.instances.append(self)

    async def connect(self, api_key):
        await asyncio.sleep(0.01)  # a real connect waits on Public
        if api_key == "bad":
            raise AuthenticationError("bad key", 401)
        if api_key == "public-down":
            raise ConnectionError("can't reach Public")
        self.client = object()

    async def resume(self, api_key):
        await self.connect(api_key)
        self.resumed_with = api_key
        self.account_id = self.store.get_setting("account_id")
        self.running = self.store.get_setting("running") == "1"

    async def disconnect(self):
        self.client = None

    async def run(self):
        await asyncio.Event().wait()

    def key_refused_seconds(self):
        return self.refused_for

    async def shutdown(self):
        self.shut_down = True


def manager(**kwargs):
    FakeEngine.instances = []
    return SessionManager(engine_factory=FakeEngine, **kwargs)


def test_same_key_rejoins_the_same_session_and_other_keys_get_their_own():
    async def run():
        m = manager()
        a = await m.connect("key-a")
        again = await m.connect("key-a")
        b = await m.connect("key-b")
        return a, again, b, len(FakeEngine.instances)

    a, again, b, engines = asyncio.run(run())
    assert a is again and a is not b
    assert engines == 2


def test_simultaneous_connects_with_one_key_share_one_bot():
    async def run():
        m = manager()
        sessions = await asyncio.gather(*(m.connect("key-a") for _ in range(5)))
        return {id(s) for s in sessions}, len(FakeEngine.instances)

    ids, engines = asyncio.run(run())
    assert len(ids) == 1 and engines == 1


def test_rejected_key_creates_nothing():
    async def run():
        m = manager()
        with pytest.raises(AuthenticationError):
            await m.connect("bad")
        return len(m)

    assert asyncio.run(run()) == 0


def test_capacity_limit():
    async def run():
        m = manager(max_sessions=1)
        await m.connect("key-a")
        with pytest.raises(SessionLimitError):
            await m.connect("key-b")
        return await m.connect("key-a")  # rejoining still works when full

    assert asyncio.run(run()) is not None


async def sweep_after(m, seconds):
    await m.sweep()  # starts the idle clock, as the periodic sweeper would
    await asyncio.sleep(seconds)
    await m.sweep()


def test_running_bot_keeps_trading_with_no_tab_open():
    async def run():
        m = manager(idle_seconds=0.05)
        s = await m.connect("key-a")
        s.engine.running = True
        m.attach(s)
        m.detach(s)  # the user closed their last tab
        await sweep_after(m, 0.1)
        return m.get(s.token) is s and not s.engine.shut_down

    assert asyncio.run(run())


def test_stopped_bot_with_no_tab_ends_once_idle_long_enough():
    async def run():
        m = manager(idle_seconds=0.05)
        s = await m.connect("key-a")
        m.attach(s)
        await sweep_after(m, 0.1)
        attached_survives = m.get(s.token) is s
        m.detach(s)
        await sweep_after(m, 0.1)
        return attached_survives, m.get(s.token), s

    attached_survives, after, s = asyncio.run(run())
    assert attached_survives
    assert after is None and s.ended and s.engine.shut_down and s.task.cancelled()


def test_activity_restarts_the_idle_clock():
    async def run():
        m = manager(idle_seconds=0.2)
        s = await m.connect("key-a")
        await sweep_after(m, 0.15)
        s.engine.running = True  # started, then stopped again, from another device
        await m.sweep()
        s.engine.running = False
        await sweep_after(m, 0.15)
        return m.get(s.token) is s

    assert asyncio.run(run())


def test_session_ends_once_public_keeps_refusing_the_key(monkeypatch):
    monkeypatch.setattr("fifteen_bot.sessions.KEY_REFUSED_SECONDS", 60)

    async def run():
        m = manager()
        s = await m.connect("key-a")
        s.engine.running = True
        s.engine.refused_for = 5  # a brief hiccup is ridden out
        await m.sweep()
        survived_hiccup = m.get(s.token) is s
        s.engine.refused_for = 61  # e.g. the user deleted the key at Public
        await m.sweep()
        return survived_hiccup, s

    survived_hiccup, s = asyncio.run(run())
    assert survived_hiccup
    assert s.ended and "API key" in s.end_reason and s.engine.shut_down


def test_ending_frees_the_key_for_a_fresh_session():
    async def run():
        m = manager()
        first = await m.connect("key-a")
        await m.end(first, "signed out")
        second = await m.connect("key-a")
        return first, second, m.get(first.token)

    first, second, stale = asyncio.run(run())
    assert first is not second and stale is None


def test_one_account_cannot_be_traded_by_two_sessions():
    async def run():
        m = manager()
        a = await m.connect("key-a")
        b = await m.connect("key-b")
        a.engine.account_id = "ACC1"
        m.check_account(a, "ACC1")  # its own account is fine
        with pytest.raises(AccountInUseError):
            m.check_account(b, "ACC1")
        await m.end(a, "signed out")
        m.check_account(b, "ACC1")  # free again once the other session ends

    asyncio.run(run())


def test_end_all_shuts_every_engine_down():
    async def run():
        m = manager()
        sessions = [await m.connect(f"key-{i}") for i in range(3)]
        await m.end_all("the server restarted")
        return sessions, len(m)

    sessions, remaining = asyncio.run(run())
    assert remaining == 0 and all(s.engine.shut_down for s in sessions)
