"""Pre-start release gate: refuses activation of an unverified release tree."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli import release_gate as gate


def _git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
        check=True,
        capture_output=True,
        text=True,
        env={**env, "GIT_AUTHOR_NAME": "gate-test", "GIT_AUTHOR_EMAIL": "gate-test@example.invalid",
             "GIT_COMMITTER_NAME": "gate-test", "GIT_COMMITTER_EMAIL": "gate-test@example.invalid"},
    ).stdout.strip()


def _make_release(tmp_path: Path, assets=gate.REQUIRED_ASSETS):
    root = tmp_path / "release"
    root.mkdir()
    _git(root, "init", "-q")
    for rel in assets:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {rel}\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "release")
    return root, _git(root, "rev-parse", "HEAD"), _git(root, "rev-parse", "HEAD^{tree}")


def _write_identity(tmp_path: Path, commit: str, tree: str, *, status="passed", exit_status=0,
                    rec_commit=None, rec_tree=None) -> Path:
    record = {"status": status, "command": "scripts/run_tests.sh", "exit_status": exit_status,
              "commit": rec_commit or commit, "tree": rec_tree or tree}
    path = tmp_path / "identity.json"
    path.write_text(json.dumps({"commit": commit, "tree": tree, "test_record": record}))
    return path


def _snapshot(paths):
    return {str(p): (os.readlink(p) if p.is_symlink() else p.read_bytes()) for p in paths}


def test_clean_verified_release_is_allowed(tmp_path):
    root, head, tree = _make_release(tmp_path)
    decision = gate.evaluate(root, _write_identity(tmp_path, head, tree))
    assert decision.allowed is True
    assert decision.reasons == ()
    assert decision.alert is None
    assert (decision.head, decision.tree) == (head, tree)


def test_dirty_tracked_file_refuses(tmp_path):
    root, head, tree = _make_release(tmp_path)
    (root / "run_agent.py").write_text("# edited\n")
    decision = gate.evaluate(root, _write_identity(tmp_path, head, tree))
    assert decision.allowed is False
    assert any("dirty" in r for r in decision.reasons)


def test_untracked_file_refuses(tmp_path):
    root, head, tree = _make_release(tmp_path)
    (root / "scratch_note.txt").write_text("stray\n")
    decision = gate.evaluate(root, _write_identity(tmp_path, head, tree))
    assert decision.allowed is False
    assert any("dirty" in r for r in decision.reasons)


def test_missing_identity_record_refuses(tmp_path):
    root, _, _ = _make_release(tmp_path)
    decision = gate.evaluate(root, tmp_path / "does-not-exist.json")
    assert decision.allowed is False
    assert any("identity" in r for r in decision.reasons)


def test_identity_pinned_to_other_commit_refuses(tmp_path):
    root, _, tree = _make_release(tmp_path)
    decision = gate.evaluate(root, _write_identity(tmp_path, "0" * 40, tree))
    assert decision.allowed is False
    assert any("commit mismatch" in r for r in decision.reasons)


def test_identity_pinned_to_other_tree_refuses(tmp_path):
    root, head, _ = _make_release(tmp_path)
    decision = gate.evaluate(root, _write_identity(tmp_path, head, "1" * 40))
    assert decision.allowed is False
    assert any("tree mismatch" in r for r in decision.reasons)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"exit_status": 1},
        {"status": "failed"},
        {"rec_commit": "2" * 40},
        {"rec_tree": "3" * 40},
    ],
)
def test_failing_or_foreign_test_record_refuses(tmp_path, kwargs):
    root, head, tree = _make_release(tmp_path)
    decision = gate.evaluate(root, _write_identity(tmp_path, head, tree, **kwargs))
    assert decision.allowed is False
    assert any("test record" in r for r in decision.reasons)


def test_identity_without_test_record_refuses(tmp_path):
    root, head, tree = _make_release(tmp_path)
    ident = tmp_path / "identity.json"
    ident.write_text(json.dumps({"commit": head, "tree": tree}))
    decision = gate.evaluate(root, ident)
    assert decision.allowed is False
    assert any("test record" in r for r in decision.reasons)


def test_missing_runtime_asset_refuses(tmp_path):
    root, head, tree = _make_release(tmp_path, assets=("run_agent.py", "cron/__init__.py"))
    decision = gate.evaluate(root, _write_identity(tmp_path, head, tree))
    assert decision.allowed is False
    assert any("missing runtime assets" in r and "hermes_cli/main.py" in r for r in decision.reasons)


def test_subdirectory_of_another_repo_refuses(tmp_path):
    root, head, tree = _make_release(tmp_path)
    sub = root / "nested"
    sub.mkdir()
    (sub / "marker.txt").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "nested")
    head = _git(root, "rev-parse", "HEAD")
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    decision = gate.evaluate(sub, _write_identity(tmp_path, head, tree))
    assert decision.allowed is False
    assert any("not a git tree root" in r for r in decision.reasons)


def test_refusal_emits_exactly_one_bounded_alert(tmp_path, capsys):
    root, _, _ = _make_release(tmp_path)
    (root / "run_agent.py").write_text("dirty\n")
    rc = gate.main(["--root", str(root), "--identity", str(tmp_path / "missing.json")])
    out, err = capsys.readouterr()
    lines = [line for line in err.splitlines() if line.strip()]
    assert rc == gate.EXIT_REFUSED
    assert out == ""
    assert len(lines) == 1
    assert lines[0].startswith("RELEASE GATE REFUSED")
    assert len(lines[0]) <= gate.ALERT_MAX


def test_allowed_release_exits_zero_with_pass_line(tmp_path, capsys):
    root, head, tree = _make_release(tmp_path)
    rc = gate.main(["--root", str(root), "--identity", str(_write_identity(tmp_path, head, tree))])
    out, err = capsys.readouterr()
    assert rc == 0
    assert out.startswith("RELEASE GATE PASS")
    assert err == ""


def test_refusal_touches_no_service_state_and_runs_only_git(tmp_path, monkeypatch):
    root, head, tree = _make_release(tmp_path)
    unit = tmp_path / "hermes-gateway.service"
    unit.write_text("ExecStart=/old/release/venv/bin/python -m gateway\n")
    approved = tmp_path / "hermes-agent-approved.json"
    approved.write_text('{"approved": "old"}\n')
    current = tmp_path / "current"
    current.symlink_to(tmp_path / "old-release")
    ident = _write_identity(tmp_path, head, tree)
    before = _snapshot([unit, approved, current])

    calls = []
    real_run = gate.subprocess.run

    def recording_run(argv, *args, **kwargs):
        calls.append(list(argv))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(gate.subprocess, "run", recording_run)
    (root / "run_agent.py").write_text("dirty\n")
    decision = gate.evaluate(root, ident)

    assert decision.allowed is False
    assert calls and all(argv[0] == "git" for argv in calls), calls
    assert _snapshot([unit, approved, current]) == before
