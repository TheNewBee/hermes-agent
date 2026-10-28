"""Provider quota windows park a cron job instead of re-firing into them (#89376;
ai-velho-oy/chairman#3).

Contract (cron/quota_hold.py): a failed run whose cause is a rate-limited ``AuthError`` with a
``retry after <N>s`` hint parks a recurring job's ``next_run_at`` past the window and stamps
``quota_hold_until``; the stale-error re-arm leaves a held job alone; a run that reaches the
model clears the marker. The hint is read only from the AuthError in the cause chain, never from
arbitrary failure text.
"""

from datetime import datetime, timedelta, timezone, UTC
from unittest.mock import MagicMock, patch

import pytest

import cron.scheduler as sched
from cron import quota_hold as qh
from cron.jobs import (
    _job_is_stale_error_recurring, create_job, get_due_jobs, get_job, mark_job_run, update_job,
)
from hermes_cli.auth import CODEX_RATE_LIMITED_CODE, AuthError

QUOTA_MSG = "Codex provider quota exhausted (429); retry after 123518s. Credentials are still valid."


@pytest.fixture
def tmp_cron_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _quota_error() -> AuthError:
    return AuthError(QUOTA_MSG, provider="openai-codex", code=CODEX_RATE_LIMITED_CODE)


def test_hold_seconds_only_from_rate_limited_auth_error_in_cause_chain():
    """The scheduler wraps the resolve failure in a RuntimeError ``from`` the AuthError; the
    hint survives through the cause chain, and text alone (or a re-login AuthError) never
    parks a job."""
    try:
        raise RuntimeError(QUOTA_MSG) from _quota_error()
    except RuntimeError as wrapped:
        assert qh.hold_seconds_from_failure(wrapped) == 123518.0

    assert qh.hold_seconds_from_failure(RuntimeError(QUOTA_MSG)) is None
    assert qh.hold_seconds_from_failure(RuntimeError("HTTP 429: retry after 60s")) is None
    relogin = AuthError(QUOTA_MSG, provider="openai-codex", code="expired", relogin_required=True)
    assert qh.hold_seconds_from_failure(relogin) is None
    structured = AuthError("quota", code=CODEX_RATE_LIMITED_CODE, retry_after=900)
    assert qh.hold_seconds_from_failure(structured) == 900.0


def test_weekly_cron_retries_when_quota_recovers_before_next_occurrence(
    tmp_cron_home, monkeypatch,
):
    """A weekly fire blocked by a shorter quota window retries when the provider reopens;
    it is not silently deferred until the following week's natural occurrence."""
    now = datetime(2026, 9, 18, 12, 1, tzinfo=UTC)
    natural_next = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)
    monkeypatch.setattr(qh, "_hermes_now", lambda: now)
    job = create_job("weekly digest", "0 12 * * 5")
    assert datetime.fromisoformat(job["next_run_at"]) == natural_next

    assert mark_job_run(
        job["id"], False, QUOTA_MSG, quota_hold_seconds=20 * 60 * 60,
        recover_consumed_fire=True,
    )

    held = get_job(job["id"])
    assert held is not None
    retry_at = datetime.fromisoformat(held["next_run_at"])
    assert retry_at == now + timedelta(hours=20, seconds=qh.HOLD_SLACK_SECONDS) < natural_next
    assert qh.is_recovery_fire(held, held["next_run_at"])
    edited = {**held, "schedule": {"kind": "cron", "expr": "0 9 * * *"}}
    assert not qh.is_recovery_fire(edited, edited["next_run_at"])

    monkeypatch.setattr("cron.jobs._hermes_now", lambda: retry_at + timedelta(seconds=1))
    assert job["id"] in {due["id"] for due in get_due_jobs()}


def test_recovery_fire_skips_dense_schedules_and_never_re_parks(monkeypatch):
    """Recovery is one attempt for sparse schedules only: an hourly job whose hold ends two
    minutes before :00 keeps its natural :00 (no off-lattice near-duplicate), and a job that is
    already the recovery fire (carries quota_hold_until) failing again is not re-parked."""
    now = datetime(2026, 9, 18, 12, 1, tzinfo=UTC)
    monkeypatch.setattr(qh, "_hermes_now", lambda: now)

    dense = {
        "schedule": {"kind": "cron", "expr": "0 * * * *"},
        "next_run_at": datetime(2026, 9, 18, 13, 0, tzinfo=UTC).isoformat(),
    }
    natural = dense["next_run_at"]
    assert not qh.plan_hold(dense, hold_seconds=57 * 60 - qh.HOLD_SLACK_SECONDS,
                            recover_consumed_fire=True)
    assert dense["next_run_at"] == natural
    assert qh.STATE_KEY not in dense

    held_again = {
        "schedule": {"kind": "cron", "expr": "0 12 * * 5"},
        "next_run_at": (now + timedelta(days=7)).isoformat(),
        qh.STATE_KEY: now.isoformat(),
        qh.SCHEDULE_EXPR_KEY: "0 12 * * 5",
    }
    natural = held_again["next_run_at"]
    assert not qh.plan_hold(held_again, hold_seconds=20 * 60 * 60, recover_consumed_fire=True)
    assert held_again["next_run_at"] == natural
    assert qh.STATE_KEY not in held_again


def _raise_quota(**_kw):
    raise _quota_error()


def _tick(job, home, deliveries, resolve):
    """One real scheduler tick (preflight ON) with the provider resolver replaced by *resolve*."""
    with patch("cron.scheduler._hermes_home", home), \
         patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("hermes_state_registry.acquire", return_value=MagicMock()), \
         patch("tools.mcp_tool_discovery.discover_mcp_tools", return_value=[]), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", side_effect=resolve), \
         patch.object(sched, "_deliver_result",
                      side_effect=lambda jb, content, **kw: deliveries.append(content)), \
         patch("run_agent.AIAgent") as agent_cls:
        agent_cls.return_value.run_conversation.side_effect = RuntimeError("model said no")
        sched.run_one_job(dict(job))


def test_quota_hold_parks_past_window_survives_stale_rearm_and_clears_on_model_reach(tmp_cron_home):
    """A 30-minute job whose provider resolve raises the Codex quota AuthError ('retry after
    123518s') is parked by the real scheduler tick: preflight lets the rate-limited AuthError
    through (it is not a missing credential), the one delivered alert carries the hold notice,
    and the job does not fire again inside the window (not even after the stale-error re-arm's
    cadence+grace). The marker clears once a run reaches the model."""
    job = create_job("portfolio triage", "every 30m", deliver="local")
    job_id = job["id"]
    now = datetime.now(UTC)
    deliveries: list = []

    _tick(get_job(job_id), tmp_cron_home, deliveries, _raise_quota)
    j = get_job(job_id)
    assert j["last_status"] == "error"
    assert len(deliveries) == 1, deliveries
    parked = datetime.fromisoformat(j["next_run_at"])
    assert parked - now >= timedelta(seconds=123518), "next_run_at must land past the window"
    assert j[qh.STATE_KEY] == j["next_run_at"]
    assert "_quota_hold_seconds" not in j

    # Two hours later the job looks like a wedged stale-error record (#62002) — the hold says
    # it is parked on purpose, so it is neither re-armed nor due.
    update_job(job_id, {"last_run_at": (now - timedelta(hours=2)).isoformat()})
    j = get_job(job_id)
    assert not _job_is_stale_error_recurring(j, j["schedule"], now)
    assert all(d["id"] != job_id for d in get_due_jobs())
    assert datetime.fromisoformat(get_job(job_id)["next_run_at"]) == parked

    # A run that reached the model (either outcome) clears the marker.
    assert mark_job_run(job_id, False, "RuntimeError: model said no")
    j = get_job(job_id)
    assert qh.STATE_KEY not in j
    assert datetime.fromisoformat(j["next_run_at"]) - now < timedelta(hours=1)

    # Editing the schedule recomputes next_run_at from the new cadence; the stale marker must
    # not linger on a record that is no longer parked where it says.
    assert mark_job_run(job_id, False, QUOTA_MSG, quota_hold_seconds=123518)
    assert qh.STATE_KEY in get_job(job_id)
    j = update_job(job_id, {"schedule": "every 15m"})
    assert qh.STATE_KEY not in j
    assert datetime.fromisoformat(j["next_run_at"]) - now < timedelta(hours=1)


# --- Plain provider 429 (agent exhausted its retries; no AuthError, no retry-after hint) -----
# The reset comes from the job's primary credential pool instead (2026-10-27: Anthropic pool
# reopened 8627s after a 13:36 PT failure).

HTTP_429_MSG = ("API call failed after 3 retries. HTTP 429: This request would exceed your "
                "account's rate limit. Please try again later. | provider=anthropic "
                "model=claude-haiku-5-5")
TUE_1336_PT = datetime(2026, 10, 27, 20, 36, tzinfo=UTC)


class _FakePool:
    def __init__(self, reset_at):
        self.reset_at, self.models = reset_at, []

    def next_available_at(self, *, model=None):
        self.models.append(model)
        return self.reset_at


def _frozen(monkeypatch, now):
    monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)
    monkeypatch.setattr(qh, "_hermes_now", lambda: now)


def test_pool_reset_hold_only_for_plain_429_with_future_pool_reset(monkeypatch):
    """(c)(d): text alone never parks (the pool must also report a future reset); an AuthError
    in the chain belongs to the AuthError path; no reset / a past reset means no hold."""
    now = TUE_1336_PT
    _frozen(monkeypatch, now)
    pool = _FakePool(now.timestamp() + 8627)

    assert qh.hold_seconds_from_pool_reset(RuntimeError(HTTP_429_MSG), pool, "claude-haiku-5-5") \
        == 8627 + qh.POOL_RESET_SLACK_SECONDS - qh.HOLD_SLACK_SECONDS
    assert pool.models == ["claude-haiku-5-5"]
    try:
        raise RuntimeError("agent reported failure") from RuntimeError("Rate limit reached")
    except RuntimeError as wrapped:
        assert qh.hold_seconds_from_pool_reset(wrapped, pool, "m")
    # (c) no 429 text
    assert qh.hold_seconds_from_pool_reset(RuntimeError("model said no"), pool, "m") is None
    # AuthError path untouched: a 429-looking AuthError in the chain is not ours to classify
    try:
        raise RuntimeError(HTTP_429_MSG) from AuthError("expired", code="expired")
    except RuntimeError as wrapped:
        assert qh.hold_seconds_from_pool_reset(wrapped, pool, "m") is None
    # (d) no usable reset
    assert qh.hold_seconds_from_pool_reset(RuntimeError(HTTP_429_MSG), _FakePool(None), "m") is None
    assert qh.hold_seconds_from_pool_reset(
        RuntimeError(HTTP_429_MSG), _FakePool(now.timestamp() - 5), "m") is None
    assert qh.hold_seconds_from_pool_reset(RuntimeError(HTTP_429_MSG), None, "m") is None


def test_plain_429_retries_same_day_at_pool_reset_then_success_clears(tmp_cron_home, monkeypatch):
    """(a)(e): reset+300s lands before tomorrow's natural fire -> one-off retry at reset+300s,
    marker set, cron expression untouched; a later successful run clears the marker."""
    now = TUE_1336_PT
    _frozen(monkeypatch, now)
    job = create_job("paper portfolio review", "15 20 * * 1-5")
    natural_next = datetime(2026, 10, 28, 20, 15, tzinfo=UTC)
    assert datetime.fromisoformat(job["next_run_at"]) == natural_next

    hold_s = qh.hold_seconds_from_pool_reset(
        RuntimeError(HTTP_429_MSG), _FakePool(now.timestamp() + 8627), "claude-haiku-5-5")
    assert mark_job_run(job["id"], False, HTTP_429_MSG, quota_hold_seconds=hold_s,
                        recover_consumed_fire=True)

    held = get_job(job["id"])
    assert datetime.fromisoformat(held["next_run_at"]) == now + timedelta(seconds=8627 + 300)
    assert held[qh.STATE_KEY] == held["next_run_at"]
    assert held["schedule"]["expr"] == "15 20 * * 1-5"
    assert qh.is_recovery_fire(held, held["next_run_at"])

    assert mark_job_run(job["id"], True)
    done = get_job(job["id"])
    assert qh.STATE_KEY not in done and qh.SCHEDULE_EXPR_KEY not in done
    assert datetime.fromisoformat(done["next_run_at"]) == natural_next


def test_plain_429_reset_after_natural_fire_parks_on_lattice(tmp_cron_home, monkeypatch):
    """(b): the 21:00 fire falls inside the closed window -> existing behaviour (first legal
    occurrence after the window), no off-lattice one-off."""
    now = TUE_1336_PT
    _frozen(monkeypatch, now)
    job = create_job("evening scan", "0 21 * * *")
    hold_s = qh.hold_seconds_from_pool_reset(
        RuntimeError(HTTP_429_MSG), _FakePool(now.timestamp() + 8627), "m")
    assert mark_job_run(job["id"], False, HTTP_429_MSG, quota_hold_seconds=hold_s,
                        recover_consumed_fire=True)

    held = get_job(job["id"])
    assert datetime.fromisoformat(held["next_run_at"]) == datetime(2026, 10, 28, 21, 0, tzinfo=UTC)
    assert qh.SCHEDULE_EXPR_KEY not in held
    assert not qh.is_recovery_fire(held, held["next_run_at"])


def test_scheduler_tick_parks_plain_429_using_job_primary_pool(tmp_cron_home):
    """Wiring: run_one_job feeds the run's primary pool + resolved model into the hold."""
    import time

    job = create_job("portfolio triage", "every 30m", deliver="local", model="claude-haiku-5-5")
    pool = _FakePool(time.time() + 8627)
    deliveries: list = []
    runtime = {"provider": "anthropic", "api_key": "fake", "base_url": "https://example.invalid",
               "api_mode": "anthropic_messages"}
    with patch.object(sched, "_load_credential_pool", return_value=pool):
        with patch("run_agent.AIAgent") as agent_cls:
            agent_cls.return_value.run_conversation.return_value = {
                "failed": True, "completed": False, "error": HTTP_429_MSG, "final_response": ""}
            with patch("cron.scheduler._hermes_home", tmp_cron_home), \
                 patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
                 patch("hermes_cli.env_loader.load_hermes_dotenv"), \
                 patch("hermes_cli.env_loader.reset_secret_source_cache"), \
                 patch("hermes_state_registry.acquire", return_value=MagicMock()), \
                 patch("tools.mcp_tool_discovery.discover_mcp_tools", return_value=[]), \
                 patch("hermes_cli.runtime_provider.resolve_runtime_provider",
                       return_value=dict(runtime)), \
                 patch.object(sched, "_deliver_result",
                              side_effect=lambda jb, content, **kw: deliveries.append(content)):
                sched.run_one_job(dict(get_job(job["id"])))

    j = get_job(job["id"])
    assert j["last_status"] == "error"
    assert pool.models == ["claude-haiku-5-5"]
    parked_in = datetime.fromisoformat(j["next_run_at"]).timestamp() - time.time()
    assert 8627 + 300 - 30 < parked_in <= 8627 + 300
    assert j[qh.STATE_KEY] == j["next_run_at"]
