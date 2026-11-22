import cron.scheduler as s
from hermes_cli import plugins
from hermes_cli.plugins import VALID_HOOKS


def test_cron_output_finalized_is_a_valid_hook():
    assert "cron_output_finalized" in VALID_HOOKS


def test_context_session_id_defaults_empty():
    assert s._cron_run_session_id.get() == ""


def test_invoke_cron_output_finalized_dispatches_named_hook(monkeypatch):
    seen = {}

    def fake_invoke(name, **kwargs):
        seen["name"] = name
        seen.update(kwargs)
        return []

    monkeypatch.setattr(plugins, "invoke_hook", fake_invoke)
    s._invoke_cron_output_finalized(
        response_text="r", session_id="cron_x_1", model="",
        artifact_path="/tmp/a.md", delivery_state="PERSISTED_ONLY")
    assert seen["name"] == "cron_output_finalized"
    assert seen["session_id"] == "cron_x_1"
    assert seen["delivery_state"] == "PERSISTED_ONLY"
