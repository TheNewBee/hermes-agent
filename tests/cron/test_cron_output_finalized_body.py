"""Body-level tests for the cron_output_finalized seam (t_ebc78db4).

The seam lives in _run_one_job_body. These tests drive run_one_job end to end
with the execute/persist/deliver/mark primitives patched, and assert that the
finalize hook fires only after a successful persisted run that recorded a
session id, and never leaks a stale session id into the next run.
"""
import cron.scheduler as s
from hermes_cli import plugins


def _patch(monkeypatch, *, success=True, set_session=True, session_id="cron_job_1_1"):
    finalized = []

    def fake_run_job(job, *, defer_agent_teardown=None, **kw):
        if set_session:
            s._cron_run_session_id.set(session_id)
        return (success, "out", "final response", None)

    monkeypatch.setattr(s, "run_job", fake_run_job)
    monkeypatch.setattr(s, "save_job_output", lambda jid, out: f"/tmp/{jid}.txt")
    monkeypatch.setattr("cron.delivery_queue.enqueue", lambda *a, **k: {"status": "pending"})
    monkeypatch.setattr(s, "mark_job_run", lambda *a, **k: None)

    def fake_invoke(name, **kwargs):
        # Other hooks (e.g. transform_api_error_classification) fire on failures too;
        # only the finalize seam is under test.
        if name == "cron_output_finalized":
            finalized.append((name, kwargs))
        return []

    monkeypatch.setattr(plugins, "invoke_hook", fake_invoke)
    return finalized


def test_finalize_fires_once_after_successful_persisted_run(monkeypatch):
    finalized = _patch(monkeypatch)

    assert s.run_one_job({"id": "fin-ok", "name": "t", "deliver": "telegram"}) is True

    assert [name for name, _ in finalized] == ["cron_output_finalized"]
    kwargs = finalized[0][1]
    assert kwargs["session_id"] == "cron_job_1_1"
    assert kwargs["delivery_state"] in {"PERSISTED_ONLY", "DELIVERY_ATTEMPTED", "DELIVERY_FAILED"}


def test_finalize_does_not_fire_for_failed_run(monkeypatch):
    finalized = _patch(monkeypatch, success=False)

    s.run_one_job({"id": "fin-fail", "name": "t", "deliver": "telegram"})

    assert finalized == []


def test_finalize_does_not_fire_without_recorded_session(monkeypatch):
    finalized = _patch(monkeypatch, set_session=False)

    s.run_one_job({"id": "fin-nosess", "name": "t", "deliver": "telegram"})

    assert finalized == []


def test_stale_session_id_does_not_leak_into_next_run(monkeypatch):
    finalized = _patch(monkeypatch, session_id="cron_stale_9")
    s.run_one_job({"id": "fin-leak-1", "name": "t", "deliver": "telegram"})
    assert len(finalized) == 1

    # Second run records no session id: the wrapper must clear the context.
    finalized.clear()
    monkeypatch.setattr(s, "run_job", lambda job, **kw: (True, "out", "final", None))
    s.run_one_job({"id": "fin-leak-2", "name": "t", "deliver": "telegram"})

    assert finalized == []
    assert s._cron_run_session_id.get() == ""
