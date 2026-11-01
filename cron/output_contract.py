"""Six-label output contract for cron final responses (t_b3bb37ea).

A job opts in by its own prompt naming the six labels in order ("Done, In progress, Blocking,
Fixed, Next, Cause/goal"). On an off-contract final, at most ONE in-session repair turn asks the
agent to restate its answer; if that still misses a label the original response is delivered
unchanged. Every verdict is written to ``continuation_checker/session-verdicts/<session>.output-contract.json``.
The verdict never suppresses delivery.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

LABELS = ("Done", "In progress", "Blocking", "Fixed", "Next", "Cause/goal")
# Same label shapes as scripts/nightly_review_packet.py CONTRACT_LINE, plus heading-only lines
# ("**Done**"), numbered items and table rows: bullet/number/pipe, optional #, optional bold,
# label, optional bold, then ':' or '|' or end of line.
_LABEL_LINE = {
    label: re.compile(
        r"^[ \t]*(?:[-*+][ \t]+|\d+[.)][ \t]+|\|[ \t]*)?(?:#{1,6}[ \t]+)?(?:\*\*|__)?[ \t]*"
        + re.escape(label) + r"[ \t]*(?:\*\*|__)?[ \t]*(?::|\||$)",
        re.MULTILINE | re.IGNORECASE)
    for label in LABELS
}
_OPT_IN = re.compile(r"Done,\s+In progress,\s+Blocking,\s+Fixed,\s+Next,\s+(?:and\s+)?Cause/goal", re.IGNORECASE)

REPAIR_PROMPT = (
    "[Output contract check] Your final answer is missing one or more of the required labels: "
    + ", ".join(LABELS) + ". Do not call any tools and do not do new work. Restate your complete "
    "final report for this run, keeping everything the job instructions require (verdict line, "
    "numbered sections, continuation ledger, ticket IDs), and make sure each of the six labels "
    "appears as its own labelled line, e.g. '- Done: ...'. Use 'None.' for an empty label."
)


def missing_labels(text: str) -> list[str]:
    return [label for label in LABELS if _LABEL_LINE[label].search(text or "") is None]


def contract_required(job_prompt: str) -> bool:
    return bool(_OPT_IN.search(job_prompt or ""))


def verdict_path_for(hermes_home: Path, session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", session_id).strip("-")
    return Path(hermes_home) / "continuation_checker" / "session-verdicts" / f"{safe}.output-contract.json"


def _write(path: Path, record: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except Exception:  # recording must never change delivery
        logger.warning("output contract verdict write failed: %s", path, exc_info=True)


def finalize_with_contract(
    response: str, *, job_prompt: str, repair: Callable[[str], dict], verdict_path: Path,
    skip: bool = False, extra: Optional[dict] = None,
) -> str:
    """Return the response to deliver. ``repair(prompt)`` runs one more agent turn and returns
    the run_conversation result dict; it is called at most once."""
    record = {"timestamp": int(time.time()), "record_kind": "output_contract", "labels": list(LABELS),
              "repair_attempted": False, "delivered": "original", **(extra or {})}
    if skip or not (response or "").strip() or not contract_required(job_prompt):
        record.update(status="SKIP", reason="silent, failure marker, empty, or job prompt has no six-label contract")
        _write(verdict_path, record)
        return response
    missing = missing_labels(response)
    if not missing:
        record.update(status="PASS", missing=[])
        _write(verdict_path, record)
        return response

    record.update(repair_attempted=True, original_missing=missing)
    try:
        result = repair(REPAIR_PROMPT) or {}
        repaired = str(result.get("final_response") or "")
        record.update(repair_prompt_tokens=result.get("prompt_tokens"),
                      repair_completion_tokens=result.get("completion_tokens"))
    except Exception as exc:
        repaired = ""
        record["repair_error"] = f"{type(exc).__name__}: {exc}"[:300]
    if repaired.strip() and not missing_labels(repaired):
        record.update(status="PASS", missing=[], delivered="repair")
        _write(verdict_path, record)
        return repaired
    # ponytail: if the repair turn ran, the continuation-ledger plugin's pending record now
    # reflects the repair text, not the delivered original; see repair_attempted in this record.
    reason = record.get("repair_error") or (
        "repair turn still off-contract, missing " + ", ".join(missing_labels(repaired))
        if repaired.strip() else "repair turn returned no text")
    record.update(status="FAIL", missing=missing, reason=f"original missing {', '.join(missing)}; {reason}")
    _write(verdict_path, record)
    return response
