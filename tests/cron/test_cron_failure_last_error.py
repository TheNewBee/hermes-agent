"""What a failed cron run persists as ``last_error`` (and the execution ledger ``error``).

An agent-declared ``[CRON_FAILURE]`` run used to persist its whole report: the evidence was
everything after the marker, so a multi-paragraph report landed verbatim in ``jobs.json``
``last_error``/``last_failure``. The persisted record now carries one bounded evidence line.
The saved run output and the delivered notice keep the full report.
"""
import cron.scheduler as s

from tests.cron.test_run_one_job import _patch_pipeline

REPORT_LEDE = "The daily PM crew has launched no work because every worker model is unavailable."
REPORT_BODY = (
    "\n\n- **Done:** checked the morning run's pre-launch record and re-read provider status.\n"
    "- **In progress:** route decision still awaiting a reply.\n"
    "- **Blocking:** two provider-quota incidents remain open.\n"
)


def _capture_record(monkeypatch):
    """Record the ``error`` handed to mark_job_run and finish_execution."""
    seen = {}

    def fake_mark(jid, ok, err=None, delivery_error=None, **_kw):
        seen["mark_error"] = err

    def fake_finish(execution_id, success=None, error=None, **_kw):
        seen["ledger_error"] = error

    monkeypatch.setattr(s, "mark_job_run", fake_mark)
    monkeypatch.setattr(s, "finish_execution", fake_finish)
    return seen


def test_agent_declared_failure_persists_bounded_evidence_not_report(monkeypatch):
    _patch_pipeline(monkeypatch, final=f"[CRON_FAILURE]\n{REPORT_LEDE}{REPORT_BODY}")
    seen = _capture_record(monkeypatch)

    s.run_one_job({"id": "declared", "name": "pm review", "deliver": "telegram"})

    err = seen["mark_error"]
    assert err is not None
    assert err.startswith("agent_declared_failure:")
    assert len(err) <= 200
    assert REPORT_LEDE[:60] in err
    assert "**Done:**" not in err and "Blocking" not in err
    assert seen["ledger_error"] == err


def test_declared_failure_keeps_full_report_in_delivery(monkeypatch):
    """Persisted record is bounded; the operator still receives the agent's evidence."""
    _patch_pipeline(monkeypatch, final=f"[CRON_FAILURE]\n{REPORT_LEDE}{REPORT_BODY}")
    delivered = []
    seen = _capture_record(monkeypatch)
    monkeypatch.setattr(
        s, "_deliver_result", lambda job, content, **kw: delivered.append(content))

    s.run_one_job({"id": "declared-deliver", "name": "pm review", "deliver": "telegram"})

    assert len(delivered) == 1
    assert REPORT_LEDE.rstrip(".") in delivered[0]
    assert "two provider-quota incidents" in delivered[0]
    assert "two provider-quota incidents" not in seen["mark_error"]


def test_prose_without_marker_or_raise_writes_no_last_error(monkeypatch):
    """A run that returns prose without raising and without the marker is healthy."""
    _patch_pipeline(monkeypatch, final=f"{REPORT_LEDE}{REPORT_BODY}")
    seen = _capture_record(monkeypatch)

    s.run_one_job({"id": "prose-ok", "name": "pm review", "deliver": "telegram"})

    assert seen["mark_error"] is None


def test_raised_exception_writes_class_and_message(monkeypatch):
    _patch_pipeline(monkeypatch, success=False, final="", error="ValueError: bad route id")
    seen = _capture_record(monkeypatch)

    s.run_one_job({"id": "raised", "name": "pm review", "deliver": "telegram"})

    assert seen["mark_error"] == "ValueError: bad route id"
    assert seen["ledger_error"] == "ValueError: bad route id"
