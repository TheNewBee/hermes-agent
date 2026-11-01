"""Six-label output contract at the cron finalization seam (t_b3bb37ea)."""

import json
from pathlib import Path

from cron.output_contract import (
    REPAIR_PROMPT, contract_required, finalize_with_contract, missing_labels,
)

FIXTURES = Path(__file__).parent / "fixtures"
OFF = (FIXTURES / "nightly_0a3da_2026-10-31_00-04-59.response.md").read_text(encoding="utf-8")
ON = (FIXTURES / "nightly_0a3da_2026-10-29_23-18-53.response.md").read_text(encoding="utf-8")
NIGHTLY_PROMPT = "Return six plain-English, one-sentence bullets in order: Done, In progress, Blocking, Fixed, Next, Cause/goal; use None."


def test_real_off_contract_nightly_fails():
    assert missing_labels(OFF) == ["Done", "In progress", "Blocking", "Fixed", "Next", "Cause/goal"]


def test_real_compliant_nightly_passes():
    assert missing_labels(ON) == []


def test_label_shapes():
    assert missing_labels("- **Done:** a\n- **In progress:** b\n- **Blocking:** c\n"
                          "- **Fixed:** d\n- **Next:** e\n- **Cause/goal:** f") == []
    # Heading-style labels on their own line (vault tidy format) and table rows.
    assert missing_labels("**Done**\nx\n## In progress\nx\n**Blocking**:\nx\n"
                          "| Fixed | y |\nNext: z\n5. Cause/goal: w") == []
    # A label word inside prose is not a label.
    assert "Done" in missing_labels("Everything is Done: really.\nI am done.")


def test_contract_opt_in_is_the_job_prompt_sequence():
    assert contract_required(NIGHTLY_PROMPT)
    assert contract_required("5. Status (Done, In progress, Blocking, Fixed, Next, Cause/goal - one line)")
    # VCP outcome review names the labels only to forbid them.
    assert not contract_required(
        "Do not use the newer six-heading format or headings/labels such as `Done`, `In progress`, "
        "`Blocking`, `Fixed`, `Next`, or `Cause/goal` as a replacement")
    assert not contract_required("")


class _Turns:
    """Fake repair runner: returns queued replies and counts calls."""

    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return {"final_response": reply, "prompt_tokens": 100, "completion_tokens": 10}


def _run(tmp_path, response, repair, prompt=NIGHTLY_PROMPT, skip=False):
    verdict_path = tmp_path / "v.json"
    delivered = finalize_with_contract(response, job_prompt=prompt, repair=repair,
                                       verdict_path=verdict_path, skip=skip)
    record = json.loads(verdict_path.read_text()) if verdict_path.exists() else {}
    return delivered, record


def test_pass_records_verdict_without_repair(tmp_path):
    repair = _Turns()
    delivered, record = _run(tmp_path, ON, repair)
    assert delivered == ON and repair.prompts == []
    assert record["status"] == "PASS" and record["repair_attempted"] is False


def test_off_contract_repaired_once(tmp_path):
    repair = _Turns(ON)
    delivered, record = _run(tmp_path, OFF, repair)
    assert repair.prompts == [REPAIR_PROMPT]
    assert delivered == ON
    assert record["status"] == "PASS" and record["delivered"] == "repair"
    assert record["repair_prompt_tokens"] == 100


def test_repair_bound_is_one_and_original_delivered_on_fail(tmp_path):
    # Two off-contract replies queued: only one may be consumed (no loop).
    repair = _Turns(OFF, OFF)
    delivered, record = _run(tmp_path, OFF, repair)
    assert len(repair.prompts) == 1
    assert delivered == OFF  # delivery unchanged by the verdict
    assert record["status"] == "FAIL" and record["delivered"] == "original"
    assert record["missing"] == ["Done", "In progress", "Blocking", "Fixed", "Next", "Cause/goal"]
    assert "still off-contract" in record["reason"]


def test_repair_error_keeps_original(tmp_path):
    delivered, record = _run(tmp_path, OFF, _Turns(RuntimeError("boom")))
    assert delivered == OFF
    assert record["status"] == "FAIL" and "boom" in record["reason"]


def test_not_required_or_silent_or_empty_skips(tmp_path):
    # skip=True is what run_job passes for [SILENT] and the [CRON_FAILURE] first-line marker.
    for response, prompt, skip in ((OFF, "no contract here", False), ("", NIGHTLY_PROMPT, False),
                                   ("[SILENT]", NIGHTLY_PROMPT, True),
                                   ("[CRON_FAILURE]\nlane x unfinished", NIGHTLY_PROMPT, True)):
        repair = _Turns()
        delivered, record = _run(tmp_path, response, repair, prompt, skip)
        assert delivered == response and repair.prompts == []
    assert record["status"] == "SKIP"


def test_verdict_write_failure_never_changes_delivery(tmp_path):
    bad = tmp_path / "file"
    bad.write_text("x")
    assert finalize_with_contract(ON, job_prompt=NIGHTLY_PROMPT, repair=_Turns(),
                                  verdict_path=bad / "sub" / "v.json") == ON
