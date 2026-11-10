"""Run-document ``**Trigger:**`` line (t_433f4c21): ``scheduled`` when the fire's ledger row ran a
schedule slot, ``manual`` for an off-slot fire, no line at all without a ledger row. Real
``run_job`` through the production owner seam (``cron_owner``), AIAgent mocked."""
from unittest.mock import patch

import pytest

import run_agent  # collection-time import; see test_cron_output_contract_run_job.py
from cron import executions
from cron.scheduler import _run_doc_header, run_job

_GOOD = "- Done: a\n- In progress: b\n- Blocking: None.\n- Fixed: c\n- Next: d\n- Cause/goal: e"
_JOB = {"id": "trig-test", "name": "trig test", "prompt": "Report.", "model": None, "provider": None,
        "provider_snapshot": None, "base_url": None, "schedule_display": "15 9 * * *"}


@pytest.fixture(autouse=True)
def _owner(cron_owner):
    return cron_owner


def _run(tmp_path, monkeypatch, execution_id=None):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("model:\n  default: claude-haiku-5-5\n  provider: anthropic\n")
    runtime = {"api_key": "k", "base_url": "https://example.invalid/v1", "provider": "anthropic",
               "api_mode": "chat_completions"}
    with patch("cron.scheduler._hermes_home", tmp_path), \
         patch("cron.scheduler._get_hermes_home", return_value=tmp_path), \
         patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=runtime), \
         patch("run_agent.AIAgent") as agent_cls:
        agent_cls.return_value.run_conversation.return_value = {"final_response": _GOOD, "messages": []}
        success, output, _final, _error = run_job(dict(_JOB), execution_id=execution_id)
    assert success
    return output.split("## Prompt", 1)[0]


def test_scheduled_slot_fire_is_labelled_scheduled(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    row = executions.create_execution(_JOB["id"], source="builtin",
                                      scheduled_instant="2026-11-10T09:15:00-08:00")
    header = _run(tmp_path, monkeypatch, row["id"])
    assert "**Schedule:** 15 9 * * *\n**Trigger:** scheduled\n\n" in header


def test_off_slot_fire_is_labelled_manual(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    row = executions.create_execution(_JOB["id"], source="direct", scheduled_instant=None)
    header = _run(tmp_path, monkeypatch, row["id"])
    assert "**Schedule:** 15 9 * * *\n**Trigger:** manual\n\n" in header


def test_fire_without_ledger_row_writes_no_trigger_line(tmp_path, monkeypatch):
    assert "**Trigger:**" not in _run(tmp_path, monkeypatch)


def test_no_trigger_header_is_byte_identical_to_legacy():
    with patch("cron.scheduler._hermes_now") as now:
        now.return_value.strftime.return_value = "2026-11-09 09:15:02"
        header = _run_doc_header(_JOB, "t", "j1", "P", prompt_stamp="**Prompt Characters:** 1\n")
    assert header == ("# Cron Job: t\n\n**Job ID:** j1\n**Run Time:** 2026-11-09 09:15:02\n"
                      "**Schedule:** 15 9 * * *\n\n**Prompt Characters:** 1\n## Prompt\n\nP\n\n")
