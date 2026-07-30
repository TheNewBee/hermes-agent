"""Characterization + unit tests for the `run_one_job` shared helper (Phase 4A).

`tick`'s per-job body (`_process_job`) is the execute → save → deliver → mark
sequence that fires ONE due job. Phase 4A extracts it into a module-level
`run_one_job(job, *, adapters=None, loop=None, verbose=False)` so the external
Chronos provider's `fire_due` can reuse the IDENTICAL body — no duplicated
correctness.

The first test characterizes the sequence as driven through `tick()` (proving
the extraction didn't change `tick`'s behavior); the rest unit-test the
extracted helper directly.
"""
import cron.scheduler as s


def test_cron_output_finalized_hook_is_supported():
    from hermes_cli.plugins import VALID_HOOKS

    assert "cron_output_finalized" in VALID_HOOKS


def _patch_pipeline(monkeypatch, *, success=True, output="out", final="final response",
                    error=None, silent_marker_in=None):
    """Patch the job pipeline primitives and record the call order."""
    calls = []

    def fake_run_job(job, *, defer_agent_teardown=None):
        calls.append(("run_job", job["id"]))
        fr = final if silent_marker_in is None else silent_marker_in
        return (success, output, fr, error)

    def fake_save(jid, out):
        calls.append(("save", jid))
        return f"/tmp/{jid}.txt"

    def fake_deliver(job, content, adapters=None, loop=None):
        calls.append(("deliver", job["id"]))
        return None

    def fake_mark(jid, ok, err=None, delivery_error=None):
        calls.append(("mark", jid, ok))

    monkeypatch.setattr(s, "run_job", fake_run_job)
    monkeypatch.setattr(s, "save_job_output", fake_save)
    monkeypatch.setattr(s, "_deliver_result", fake_deliver)
    monkeypatch.setattr(s, "mark_job_run", fake_mark)
    return calls


def test_tick_process_job_sequence(monkeypatch):
    """Characterization: a single due job driven through tick() runs the
    sequence run_job → save → deliver → mark, in that order."""
    calls = _patch_pipeline(monkeypatch)
    monkeypatch.setattr(s, "get_due_jobs", lambda: [{"id": "j1", "name": "t"}])
    monkeypatch.setattr(s, "advance_next_run", lambda jid: True)

    s.tick(verbose=False, sync=True)

    assert [c[0] for c in calls] == ["run_job", "save", "deliver", "mark"]
    assert calls[-1] == ("mark", "j1", True)


def test_tick_does_not_propagate_delegated_child_context_to_cron_parent(monkeypatch):
    """A scheduler-owned cron run must retain Kanban authority.

    ``tick`` uses ``copy_context`` to bridge request-local state into a worker
    thread. A caller can itself be a delegated child, but a scheduled job is a
    scheduler parent, not that child's descendant. Propagating the child marker
    made every Kanban operation fail closed inside PM cron jobs.
    """
    from agent.delegation_context import (
        delegated_child_context,
        is_delegated_child_process_context,
    )

    # The test runner itself may be a delegated child. The regression is about
    # copied *ContextVar* state, so remove any inherited process marker before
    # establishing the test's child scope.
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)

    seen = []
    monkeypatch.setattr(s, "get_due_jobs", lambda: [{"id": "j-context", "name": "t"}])
    monkeypatch.setattr(s, "advance_next_run", lambda _: True)
    monkeypatch.setattr(
        s,
        "run_one_job",
        lambda *args, **kwargs: seen.append(is_delegated_child_process_context()) or True,
    )

    with delegated_child_context():
        s.tick(verbose=False, sync=True)

    assert seen == [False]


def test_run_one_job_success_sequence(monkeypatch):
    """The extracted helper runs the same execute→save→deliver→mark sequence
    for a successful job."""
    calls = _patch_pipeline(monkeypatch)

    ok = s.run_one_job({"id": "j2", "name": "t"})

    assert ok is True
    assert [c[0] for c in calls] == ["run_job", "save", "deliver", "mark"]
    assert calls[-1] == ("mark", "j2", True)


def test_run_one_job_finalizes_persisted_llm_output_before_delivery(monkeypatch):
    """A post-persistence observer receives the exact saved artifact before delivery."""
    order = []
    observed = {}

    def fake_run_job(job, *, defer_agent_teardown=None):
        order.append("run_job")
        job["_hermes_final_cron_session_id"] = "cron_j2_20260727_010000"
        return True, "persisted report", "human report", None

    def fake_save(job_id, output):
        order.append("save")
        assert (job_id, output) == ("j2", "persisted report")
        return "/tmp/j2.md"

    def fake_finalize(name, **kwargs):
        order.append("finalize")
        observed.update(name=name, **kwargs)
        return "[SILENT]"

    def fake_deliver(*_args, **_kwargs):
        order.append("deliver")
        return None

    monkeypatch.setattr(s, "run_job", fake_run_job)
    monkeypatch.setattr(s, "save_job_output", fake_save)
    monkeypatch.setattr(s, "_deliver_result", fake_deliver)
    monkeypatch.setattr(s, "mark_job_run", lambda *_args, **_kwargs: order.append("mark"))
    import hermes_cli.plugins as plugins
    monkeypatch.setattr(plugins, "invoke_hook", fake_finalize)

    assert s.run_one_job({"id": "j2", "name": "t"}) is True
    assert order == ["run_job", "save", "finalize", "deliver", "mark"]
    assert observed == {
        "name": "cron_output_finalized",
        "response_text": "persisted report",
        "session_id": "cron_j2_20260727_010000",
        "platform": "cron",
        "artifact_path": "/tmp/j2.md",
        "delivery_state": "PERSISTED_ONLY",
    }


def test_run_one_job_installs_secret_scope_under_multiplex(monkeypatch, tmp_path):
    """Regression: under profile isolation (multiplex active), run_one_job must
    execute run_job inside a profile secret scope so credential reads
    (resolve_runtime_provider -> get_secret) don't fail-close with
    UnscopedSecretError, and must tear the scope down afterward.

    Behavior contract: a scope is present during run_job and absent after,
    regardless of the concrete secret values.
    """
    from agent import secret_scope as ss

    # Point cron's home resolution at a profile whose .env carries a secret.
    (tmp_path / ".env").write_text("OPENROUTER_BASE_URL=https://openrouter.ai/api/v1\n")
    monkeypatch.setattr(s, "_get_hermes_home", lambda: tmp_path)

    scope_during_run = {}

    def fake_run_job(job, *, defer_agent_teardown=None):
        # This is where resolve_runtime_provider() would read a secret. Prove a
        # scope is installed and the profile's secret resolves without raising.
        scope_during_run["scope"] = ss.current_secret_scope()
        scope_during_run["base_url"] = ss.get_secret("OPENROUTER_BASE_URL")
        return (True, "out", "final", None)

    monkeypatch.setattr(s, "run_job", fake_run_job)
    monkeypatch.setattr(s, "save_job_output", lambda jid, out: f"/tmp/{jid}.txt")
    monkeypatch.setattr(s, "_deliver_result", lambda *a, **k: None)
    monkeypatch.setattr(s, "mark_job_run", lambda *a, **k: None)

    ss.set_multiplex_active(True)
    try:
        ok = s.run_one_job({"id": "j7", "name": "t"})
    finally:
        ss.set_multiplex_active(False)

    assert ok is True
    # Scope was installed during run_job and the profile secret resolved.
    assert scope_during_run["scope"] is not None
    assert scope_during_run["base_url"] == "https://openrouter.ai/api/v1"
    # And it was torn down after run_one_job returned (no leak).
    assert ss.current_secret_scope() is None


