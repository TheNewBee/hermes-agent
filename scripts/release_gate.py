"""Pre-start release gate for a Hermes release candidate.

Run before a release is switched in. Exit 0 only when the release tree is clean,
its HEAD commit and tree match a verified evidence record, and every runtime
asset is present. On refusal it prints and appends ONE alert line and exits 1;
the caller must then leave the previous service running. This script never
touches systemd, symlinks, or running processes. Stdlib only, so it runs before
any environment exists.

Usage: python scripts/release_gate.py --release <worktree> --record <record.json> [--alert-log <file>]

Record format: {"commit": <HEAD sha>, "tree": <HEAD^{tree} sha>, "verified": true, "tests_failed": 0}
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REQUIRED_ASSETS = (
    "pyproject.toml",
    "run_agent.py",
    "hermes_cli/__init__.py",
    "gateway/run.py",
    "tools/approval.py",
    "cron/__init__.py",
)
# ponytail: one alert line per refusal, truncated; no dedupe or retry. Add a rate
# window only if refusals start repeating in the alert channel.
MAX_ALERT_CHARS = 500


def _git(release: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(release), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def check(release, record, required=REQUIRED_ASSETS) -> list[str]:
    """Return refusal reasons; an empty list means activation may proceed."""
    release = Path(release)
    try:
        head = _git(release, "rev-parse", "HEAD")
        tree = _git(release, "rev-parse", "HEAD^{tree}")
        dirty = _git(release, "status", "--porcelain", "--untracked-files=all")
    except (OSError, subprocess.CalledProcessError) as exc:
        return [f"not a readable git release tree ({exc})"]

    reasons = []
    if dirty:
        reasons.append(f"release tree dirty ({len(dirty.splitlines())} path(s))")

    try:
        evidence = json.loads(Path(record).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        evidence = None
    if not isinstance(evidence, dict):
        reasons.append("no readable evidence record for this release identity")
    else:
        if evidence.get("commit") != head or evidence.get("tree") != tree:
            reasons.append(f"evidence record does not match release identity {head[:12]}/{tree[:12]}")
        if evidence.get("verified") is not True or evidence.get("tests_failed") != 0:
            reasons.append("evidence record is not verified green")

    missing = [asset for asset in required if not (release / asset).exists()]
    if missing:
        reasons.append("missing runtime asset(s): " + ", ".join(missing))
    return reasons


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Pre-start gate for a Hermes release candidate.")
    parser.add_argument("--release", required=True, help="release worktree path")
    parser.add_argument("--record", required=True, help="verified evidence record (JSON)")
    parser.add_argument("--alert-log", help="file to append the single refusal alert to")
    args = parser.parse_args(argv)

    reasons = check(args.release, args.record)
    if not reasons:
        print("release gate GREEN: activation may proceed")
        return 0

    alert = ("release gate REFUSED, previous service stays active: " + "; ".join(reasons))[:MAX_ALERT_CHARS]
    print(alert, file=sys.stderr)
    if args.alert_log:
        with open(args.alert_log, "a", encoding="utf-8") as fh:
            fh.write(alert + "\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
