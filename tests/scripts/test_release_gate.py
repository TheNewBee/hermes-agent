"""Pre-start release gate: refuse a dirty, unverified, or incomplete release.

Regression guard for t_64781aad / t_7269023e: a release switch must be refused
when the tree is dirty, the identity has no verified evidence, or a runtime
asset is absent, and a refusal emits exactly one bounded alert.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from scripts.release_gate import REQUIRED_ASSETS, check, main

REPO = Path(__file__).resolve().parents[2]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _make_release(tmp_path: Path, assets=REQUIRED_ASSETS) -> tuple[Path, str, str]:
    root = tmp_path / "release"
    root.mkdir()
    _git(root, "init", "-q")
    for asset in assets:
        path = root / asset
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# asset\n")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "release")
    return root, _git(root, "rev-parse", "HEAD"), _git(root, "rev-parse", "HEAD^{tree}")


def _record(tmp_path: Path, commit: str, tree: str, **overrides) -> Path:
    evidence = {"commit": commit, "tree": tree, "verified": True, "tests_failed": 0}
    evidence.update(overrides)
    path = tmp_path / "record.json"
    path.write_text(json.dumps(evidence))
    return path


def test_clean_verified_release_passes(tmp_path, capsys):
    root, commit, tree = _make_release(tmp_path)
    record = _record(tmp_path, commit, tree)
    assert check(root, record) == []
    assert main(["--release", str(root), "--record", str(record)]) == 0
    assert capsys.readouterr().err == ""


def test_untracked_file_refuses_dirty_tree(tmp_path):
    root, commit, tree = _make_release(tmp_path)
    (root / "stray.py").write_text("x = 1\n")
    reasons = check(root, _record(tmp_path, commit, tree))
    assert any("dirty" in r for r in reasons)


def test_modified_tracked_file_refuses_dirty_tree(tmp_path):
    root, commit, tree = _make_release(tmp_path)
    (root / "run_agent.py").write_text("# edited\n")
    reasons = check(root, _record(tmp_path, commit, tree))
    assert any("dirty" in r for r in reasons)


def test_missing_record_refuses_unverified_identity(tmp_path):
    root, _, _ = _make_release(tmp_path)
    reasons = check(root, tmp_path / "absent.json")
    assert any("no readable evidence record" in r for r in reasons)


def test_record_for_other_commit_refused(tmp_path):
    root, _, tree = _make_release(tmp_path)
    reasons = check(root, _record(tmp_path, "0" * 40, tree))
    assert any("does not match" in r for r in reasons)


def test_record_for_other_tree_refused(tmp_path):
    root, commit, _ = _make_release(tmp_path)
    reasons = check(root, _record(tmp_path, commit, "0" * 40))
    assert any("does not match" in r for r in reasons)


def test_unverified_or_failing_record_refused(tmp_path):
    root, commit, tree = _make_release(tmp_path)
    unverified = check(root, _record(tmp_path, commit, tree, verified=False))
    failing = check(root, _record(tmp_path, commit, tree, tests_failed=2))
    assert any("not verified green" in r for r in unverified)
    assert any("not verified green" in r for r in failing)


def test_missing_runtime_asset_refused(tmp_path):
    root, commit, tree = _make_release(tmp_path)
    reasons = check(root, _record(tmp_path, commit, tree), required=("nope/absent.bin",))
    assert any("missing runtime asset" in r and "nope/absent.bin" in r for r in reasons)


def test_refusal_emits_exactly_one_bounded_alert(tmp_path, capsys):
    root, _, _ = _make_release(tmp_path)
    (root / "stray.py").write_text("x = 1\n")
    alert_log = tmp_path / "alerts.log"
    code = main(
        ["--release", str(root), "--record", str(tmp_path / "absent.json"), "--alert-log", str(alert_log)]
    )
    assert code == 1
    lines = alert_log.read_text().splitlines()
    assert len(lines) == 1
    assert len(lines[0]) <= 500
    assert "dirty" in lines[0] and "no readable evidence record" in lines[0]
    assert capsys.readouterr().err.strip() == lines[0]


def test_default_required_assets_exist_in_repo():
    missing = [a for a in REQUIRED_ASSETS if not (REPO / a).exists()]
    assert missing == []
