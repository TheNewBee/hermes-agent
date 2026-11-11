"""Per-job wall-clock cap (``max_runtime_seconds``): a busy agent is still ended at the limit."""

import threading
import time

import pytest

from cron import scheduler


class _BusyAgent:
    """Never goes idle: activity is always fresh, so only the wall clock can end it."""

    def __init__(self):
        self.stop = threading.Event()
        self.interrupts = []

    def run_conversation(self, *args, **kwargs):
        self.stop.wait(30)
        return {"final_response": "should never be delivered"}

    def get_activity_summary(self):
        return {"seconds_since_activity": 0.0, "last_activity_desc": "tool call"}

    def interrupt(self, message=None):
        self.interrupts.append(message)
        self.stop.set()


def test_busy_agent_is_ended_at_limit_with_one_error_line(monkeypatch):
    monkeypatch.setattr(scheduler, "_cron_inactivity_seconds", lambda: 600.0)
    agent = _BusyAgent()
    job = {"id": "wd", "name": "Watchdog", "schedule": {"kind": "interval"}, "max_runtime_seconds": 1}
    t0 = time.monotonic()
    try:
        with pytest.raises(TimeoutError) as exc:
            scheduler._run_agent_with_watchdog(agent, "p", job, "wd", "Watchdog", "t", None)
    finally:
        agent.stop.set()
    elapsed = time.monotonic() - t0
    assert 1.0 <= elapsed < 10.0  # uncapped, the fake agent runs 30 s
    assert agent.interrupts, "agent was not interrupted"
    assert "ended after 1s (limit 1s)" in str(exc.value)

    line = scheduler._summarize_cron_failure_for_delivery(job, f"TimeoutError: {exc.value}")
    assert line == "⚠️ Cron 'Watchdog' ended after 1s (limit 1s)."
    assert "\n" not in line


def test_repair_turn_shares_the_first_turns_budget(monkeypatch):
    monkeypatch.setattr(scheduler, "_cron_inactivity_seconds", lambda: 600.0)
    agent = _BusyAgent()
    job = {"id": "wd", "schedule": {"kind": "interval"}, "max_runtime_seconds": 60}
    state = {"wall_start": time.monotonic() - 60}  # first turn already used the whole budget
    t0 = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match=r"limit 60s"):
            scheduler._run_agent_with_watchdog(agent, "p", job, "wd", "wd", "t", None, worker_state=state)
    finally:
        agent.stop.set()
    assert time.monotonic() - t0 < 5.0


def test_no_agent_script_text_is_not_read_as_wall_limit():
    job = {"id": "s", "name": "S", "no_agent": True}
    line = scheduler._summarize_cron_failure_for_delivery(job, "script said: ended after 5s (limit 5s)")
    assert line.startswith("⚠️ Cron 'S' failed:")


@pytest.mark.parametrize("raw", [None, 0, -5, "abc"])
def test_no_or_invalid_limit_means_no_cap(raw):
    assert scheduler._job_max_runtime_seconds({"max_runtime_seconds": raw}) is None


def test_fast_run_under_limit_returns_result(monkeypatch):
    monkeypatch.setattr(scheduler, "_cron_inactivity_seconds", lambda: 600.0)

    class _Fast(_BusyAgent):
        def run_conversation(self, *args, **kwargs):
            return {"final_response": "ok"}

    job = {"id": "f", "schedule": {"kind": "interval"}, "max_runtime_seconds": 60}
    assert scheduler._run_agent_with_watchdog(_Fast(), "p", job, "f", "f", "t", None)["final_response"] == "ok"
