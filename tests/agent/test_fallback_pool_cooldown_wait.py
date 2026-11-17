"""Fallback candidate whose credential pool is fully cooling: wait for a bounded expiry, else skip.

Covers ``chat_completion_helpers._candidate_pool_exhausted``. Time is a fake clock so no test
really sleeps.
"""

from types import SimpleNamespace

import pytest

import agent.chat_completion_helpers as ccH


class FakeClock:
    def __init__(self, now=1_000_000.0):
        self.now = now
        self.slept = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class FakePool:
    """Each entry is available once the clock reaches its ``until``; ``None`` = no expiry info."""

    provider = "openai-codex"

    def __init__(self, clock, untils, always_unavailable=False):
        self.clock = clock
        self.untils = untils
        self.always_unavailable = always_unavailable

    def has_credentials(self):
        return bool(self.untils)

    def has_available(self, *, model=None):
        if self.always_unavailable:
            return False
        return any(u is not None and self.clock.time() >= u for u in self.untils)

    def next_available_at(self, *, model=None):
        if self.has_available(model=model):
            return None
        known = [u for u in self.untils if u is not None]
        return min(known) if known else None


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(ccH, "time", fake)
    monkeypatch.setattr("hermes_cli.config.load_config", dict)
    return fake


def _agent(pool, **extra):
    touches = []
    fields = dict(
        provider="anthropic", model="claude-haiku-5-5", _credential_pool=pool,
        run_budget_seconds=None, _run_budget_started_at=None,
        _touch_activity=lambda desc, **_kw: touches.append(desc),
    )
    fields.update(extra)
    agent = SimpleNamespace(**fields)
    agent._touches = touches
    return agent


def _check(agent):
    return ccH._candidate_pool_exhausted(agent, "openai-codex", "gpt-6-luna")


def test_waits_for_expiry_within_cap_then_uses_entry(clock):
    pool = FakePool(clock, [clock.now + 300])
    agent = _agent(pool)
    assert _check(agent) is False  # not skipped: candidate gets activated
    assert sum(clock.slept) == pytest.approx(300)
    assert pool.has_available(model="gpt-6-luna")


def test_skips_when_expiry_beyond_cap(clock):
    agent = _agent(FakePool(clock, [clock.now + 900]))
    assert _check(agent) is True
    assert clock.slept == []


def test_no_wait_when_an_entry_is_available(clock):
    agent = _agent(FakePool(clock, [clock.now - 1, clock.now + 300]))
    assert _check(agent) is False
    assert clock.slept == []


def test_skips_when_no_expiry_information(clock):
    agent = _agent(FakePool(clock, [None]))
    assert _check(agent) is True
    assert clock.slept == []


def test_cap_is_read_from_fallback_config(clock, monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config",
                        lambda: {"fallback": {"max_pool_wait_seconds": 1000}})
    agent = _agent(FakePool(clock, [clock.now + 900]))
    assert _check(agent) is False
    assert sum(clock.slept) == pytest.approx(900)


def test_run_budget_too_small_for_wait_skips(clock):
    agent = _agent(FakePool(clock, [clock.now + 300]),
                   run_budget_seconds=600.0, _run_budget_started_at=clock.now - 400)
    assert _check(agent) is True
    assert clock.slept == []


def test_still_unavailable_after_wait_is_skipped(clock):
    agent = _agent(FakePool(clock, [clock.now + 300], always_unavailable=True))
    assert _check(agent) is True
    assert sum(clock.slept) == pytest.approx(300)


def test_wait_is_sliced_and_touches_activity(clock):
    agent = _agent(FakePool(clock, [clock.now + 100]))
    assert _check(agent) is False
    assert clock.slept and max(clock.slept) <= 30.0
    assert agent._touches, "waiting must touch activity so the cron inactivity watchdog stays quiet"


def test_user_interrupt_ends_the_wait_early(clock):
    agent = _agent(FakePool(clock, [clock.now + 600]))
    sleep = clock.sleep

    def sleep_then_interrupt(seconds):
        sleep(seconds)
        agent._interrupt_requested = True

    clock.sleep = sleep_then_interrupt
    assert _check(agent) is True  # interrupted: skip, do not activate the candidate
    assert sum(clock.slept) < 5, "/stop must not wait out the whole cap"
