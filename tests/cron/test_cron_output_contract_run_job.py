"""run_job seam for the six-label output contract (t_b3bb37ea): one repair turn at most, continued
in-session, verdict recorded, delivery never suppressed. Real ``run_job`` with AIAgent mocked."""
import json
from unittest.mock import MagicMock, patch

from cron.scheduler import run_job

_PROMPT = "Report. Return six bullets in order: Done, In progress, Blocking, Fixed, Next, Cause/goal."
_JOB = {"id": "oc-test", "name": "oc test", "prompt": _PROMPT, "model": None, "provider": None,
        "provider_snapshot": None, "base_url": None}
_GOOD = "- Done: a\n- In progress: b\n- Blocking: None.\n- Fixed: c\n- Next: d\n- Cause/goal: e"
_HISTORY = [{"role": "user", "content": "x"}, {"role": "assistant", "content": "free prose"}]


def _run(tmp_path, *replies):
    (tmp_path / "config.yaml").write_text("model:\n  default: claude-haiku-5-5\n  provider: anthropic\n")
    runtime = {"api_key": "k", "base_url": "https://example.invalid/v1", "provider": "anthropic",
               "api_mode": "chat_completions"}
    with patch("cron.scheduler._hermes_home", tmp_path), \
         patch("cron.scheduler._get_hermes_home", return_value=tmp_path), \
         patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("hermes_state_registry.acquire", return_value=MagicMock()), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=runtime), \
         patch("run_agent.AIAgent") as agent_cls:
        agent_cls.return_value.run_conversation.side_effect = [
            {"final_response": r, "messages": _HISTORY} for r in replies]
        success, _output, final, error = run_job(dict(_JOB))
    calls = agent_cls.return_value.run_conversation.call_args_list
    verdicts = list((tmp_path / "continuation_checker" / "session-verdicts").glob("*.output-contract.json"))
    assert len(verdicts) == 1
    return success, final, error, calls, json.loads(verdicts[0].read_text())


def test_off_contract_final_gets_one_in_session_repair(tmp_path):
    success, final, _e, calls, verdict = _run(tmp_path, "free prose", _GOOD)
    assert success and final == _GOOD
    assert len(calls) == 2 and calls[1].kwargs["conversation_history"] == _HISTORY
    assert verdict["status"] == "PASS" and verdict["delivered"] == "repair"
    assert verdict["session_id"].startswith("cron_oc-test_")


def test_still_off_contract_delivers_original_and_records_fail(tmp_path):
    success, final, error, calls, verdict = _run(tmp_path, "free prose", "still prose", "never used")
    assert (success, error, final) == (True, None, "free prose")
    assert len(calls) == 2  # bound: one repair, no loop
    assert verdict["status"] == "FAIL" and verdict["delivered"] == "original"


def test_compliant_final_runs_one_turn(tmp_path):
    _s, final, _e, calls, verdict = _run(tmp_path, _GOOD)
    assert final == _GOOD and len(calls) == 1 and verdict["status"] == "PASS"
