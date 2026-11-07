"""Pre-start release gate.

Decides whether a release tree may be activated. It only reads: git state, the
identity record, and file existence. It never touches services, ExecStart,
symlinks, approval files, or cron. A refusal is a non-zero exit that the caller
must treat as "keep the previous service active".

Usage: python -m hermes_cli.release_gate --root <tree> --identity <record.json>
"""

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# ponytail: assets checked by default only; extend via evaluate(required_assets=...).
REQUIRED_ASSETS = ("run_agent.py", "cron/__init__.py", "hermes_cli/main.py", "tools/approval.py")
ALERT_MAX = 240
EXIT_OK = 0
EXIT_REFUSED = 3


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    reasons: tuple
    alert: str | None
    head: str | None = None
    tree: str | None = None


class _GitError(Exception):
    pass


def _git(root: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, env=env, check=False
        )
    except OSError as exc:
        raise _GitError(str(exc)) from exc
    if proc.returncode != 0:
        raise _GitError(proc.stderr.strip() or f"git {args[0]} failed")
    return proc.stdout.strip()


def _load_identity(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def evaluate(root: Path, identity_path: Path, required_assets=REQUIRED_ASSETS) -> GateDecision:
    root = Path(root)
    reasons = []
    head = tree = None
    try:
        toplevel = _git(root, "rev-parse", "--show-toplevel")
        if Path(toplevel).resolve() != root.resolve():
            reasons.append("not a git tree root")
        else:
            head = _git(root, "rev-parse", "--verify", "HEAD")
            tree = _git(root, "rev-parse", "--verify", "HEAD^{tree}")
    except _GitError:
        reasons.append("not a git tree root")

    if head is not None:
        changed = _git(root, "status", "--porcelain", "--untracked-files=all")
        if changed:
            reasons.append(f"dirty tree ({len(changed.splitlines())} changed paths)")

    identity = _load_identity(identity_path)
    if identity is None:
        reasons.append("identity record missing or unreadable")
    elif head is not None:
        if identity.get("commit") != head:
            reasons.append("identity commit mismatch")
        if identity.get("tree") != tree:
            reasons.append("identity tree mismatch")
        record = identity.get("test_record")
        if not (
            isinstance(record, dict)
            and record.get("status") == "passed"
            and record.get("exit_status") == 0
            and record.get("commit") == head
            and record.get("tree") == tree
        ):
            reasons.append("no passing test record for this commit and tree")

    missing = [a for a in required_assets if not (root / a).is_file()]
    if missing:
        reasons.append("missing runtime assets: " + ", ".join(missing))

    if reasons:
        alert = ("RELEASE GATE REFUSED: " + "; ".join(reasons))[:ALERT_MAX]
        return GateDecision(False, tuple(reasons), alert, head, tree)
    return GateDecision(True, (), None, head, tree)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Pre-start release gate (read-only).")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--identity", required=True, type=Path)
    args = parser.parse_args(argv)
    decision = evaluate(args.root, args.identity)
    if decision.allowed:
        print(f"RELEASE GATE PASS: commit {decision.head[:12]} tree {decision.tree[:12]}")
        return EXIT_OK
    print(decision.alert, file=sys.stderr)
    return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
