"""Focused fixture tests for the release-state preflight (t_7269023e).

Fixture matrix (each decision is deterministic and hermetic -- all git repos
are created under pytest tmp_path, never touching real user state):

  * clean git release + in-tree launcher            -> PASS
  * dirty git tree (tracked edit + untracked)       -> REJECT (dirty paths listed)
  * temporary launcher under a temp root            -> REJECT (temporary)
  * non-temp out-of-tree launcher                   -> REJECT (external)
  * broken-symlink launcher                         -> REJECT (broken)
  * managed non-git install (docker stamp)          -> axis N/A + in-tree launcher -> PASS
  * unknown non-git install                         -> REJECT (fail-closed)
  * no launcher candidates                          -> REJECT (fail-closed)
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import release_preflight as rp


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )


def _make_repo(tmp_path: Path, name: str = "tree") -> Path:
    """Create an initialized git repo that looks like a source install tree."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Fixture Tester")
    _git(repo, "config", "user.email", "fixture@example.com")
    (repo / "hermes_cli").mkdir()
    (repo / "hermes_cli" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "hermes").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    (repo / "README.md").write_text("release\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


def _in_tree_launcher(repo: Path) -> str:
    return str(repo / "hermes")


# ── resolution / conventions ────────────────────────────────────────────────


def test_install_root_delegates_to_canonical_seam() -> None:
    # Default resolver = parent of hermes_cli/ (the install tree of the code
    # being tested), mirroring config._install_method_project_root.
    assert rp.install_root() == Path(rp.__file__).resolve().parent.parent
    assert rp.install_root(Path("/custom/checkout")) == Path("/custom/checkout")


def test_is_git_checkout_recognizes_worktree_gitfile(tmp_path: Path) -> None:
    # A worktree has `.git` as a file containing `gitdir: ...`, not a dir.
    dirpath = tmp_path / "worktree"
    dirpath.mkdir()
    (dirpath / ".git").write_text("gitdir: /some/main/.git/worktrees/x\n", encoding="utf-8")
    assert rp._is_git_checkout(dirpath) is True
    plain = tmp_path / "plain"
    plain.mkdir()
    assert rp._is_git_checkout(plain) is False


def test_detect_install_method_on_fixture_is_git(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    state = rp.release_state(repo)
    assert state.is_git is True
    assert state.install_method == "git"
    assert state.head
    assert state.dirty_paths == ()


# ── axis 1: release cleanliness ─────────────────────────────────────────────


def test_clean_release_passes(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    result = rp.preflight(project_root=repo, launchers=[_in_tree_launcher(repo)])
    assert result.ok is True
    assert result.release.verdict == "pass"
    assert result.launchers.verdict == "pass"
    assert result.release.dirty == ()
    assert result.release.head


def test_release_state_reports_exact_tag_describe(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _git(repo, "tag", "2026.8.19")
    state = rp.release_state(repo)
    assert state.describe == "2026.8.19"


def test_dirty_tree_rejected_with_actionable_paths(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "README.md").write_text("release\nedited\n", encoding="utf-8")  # tracked edit
    (repo / "scratch.txt").write_text("leak\n", encoding="utf-8")  # untracked
    result = rp.preflight(project_root=repo, launchers=[_in_tree_launcher(repo)])
    assert result.ok is False
    assert result.release.verdict == "fail"
    dirty_text = "\n".join(result.release.dirty)
    assert "README.md" in dirty_text  # tracked modification surfaced
    assert "scratch.txt" in dirty_text  # untracked file surfaced
    # Actionable evidence lists the dirty paths.
    assert any("DIRTY:" in line for line in result.evidence())
    # Head identity is preserved so the evidence can say *which* commit drifted.
    assert result.release.head == rp.release_state(repo).head


def test_behind_local_reads_local_ref_only(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    # No origin ref yet -> None (deterministic, no network).
    assert rp.release_state(repo).behind_local is None
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    assert rp.release_state(repo).behind_local == 0


# ── axis 2: launcher residency ──────────────────────────────────────────────


def test_temporary_launcher_rejected(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    # A launcher that resolves outside the release tree, under a temp root
    # (the recurred contamination shape: recycled /tmp venv).
    temp_launcher = tmp_path / "extern" / ".venv" / "bin" / "hermes"
    temp_launcher.parent.mkdir(parents=True)
    temp_launcher.touch()
    result = rp.preflight(
        project_root=repo,
        launchers=[_in_tree_launcher(repo), str(temp_launcher)],
    )
    assert result.ok is False
    assert result.launchers.verdict == "fail"
    decisions = {p["decision"] for p in result.launchers.probes}
    assert rp.IN_RELEASE in decisions  # in-tree probe still classified individually
    assert rp.TEMPORARY in decisions  # temp launcher called out specifically
    assert any(p["decision"] == rp.TEMPORARY for p in result.launchers.probes)
    # The clean tree alone must not accidentally pass while the launcher is bad.
    assert result.release.verdict == "pass"


def test_external_non_temp_launcher_rejected(tmp_path: Path, monkeypatch) -> None:
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(rp, "_temp_roots", lambda: (Path("/definitely-not-a-temp-root"),))
    external = tmp_path / "site" / "bin" / "hermes"
    external.parent.mkdir(parents=True)
    external.touch()
    probe = rp.classify_launcher(str(external), repo)
    assert probe["decision"] == rp.EXTERNAL
    result = rp.preflight(project_root=repo, launchers=[str(external)])
    assert result.ok is False
    assert rp.EXTERNAL in {p["decision"] for p in result.launchers.probes}
    assert result.launchers.verdict == "fail"


def test_broken_symlink_launcher_rejected(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    missing_target = tmp_path / "recycled" / "venv" / "bin" / "python"
    link = tmp_path / "hermes-link"
    os.symlink(str(missing_target), str(link))
    probe = rp.classify_launcher(str(link), repo)
    assert probe["decision"] == rp.BROKEN
    result = rp.preflight(project_root=repo, launchers=[str(link)])
    assert result.ok is False
    assert rp.BROKEN in {p["decision"] for p in result.launchers.probes}


# ── fail-closed / managed installs ──────────────────────────────────────────


def test_managed_non_git_install_passes(tmp_path: Path) -> None:
    # Container-style install: immutable tree, docker stamp, in-tree launcher.
    install = tmp_path / "opt-hermes"
    install.mkdir()
    (install / ".install_method").write_text("docker\n", encoding="utf-8")
    (install / "hermes").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    result = rp.preflight(project_root=install, launchers=[str(install / "hermes")])
    assert result.ok is True
    assert result.release.verdict == "na"
    assert any("managed docker install" in note for note in result.release.notes)


def test_unknown_non_git_install_fails_closed(tmp_path: Path) -> None:
    install = tmp_path / "mystery"
    install.mkdir()
    (install / "hermes").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    result = rp.preflight(project_root=install, launchers=[str(install / "hermes")])
    assert result.ok is False
    assert result.release.verdict == "fail"
    assert any("fail-closed" in note for note in result.release.notes)


def test_no_launcher_candidates_fails_closed(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    result = rp.preflight(project_root=repo, launchers=[])
    assert result.ok is False
    assert result.launchers.verdict == "fail"  # nothing verified -> cannot pass


def test_default_candidates_include_in_tree_launchers(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / "venv" / "bin").mkdir(parents=True)
    (repo / "venv" / "bin" / "hermes").touch()
    (repo / "scripts").mkdir(exist_ok=True)
    (repo / "scripts" / "hermes-gateway").touch()
    cands = rp.default_launcher_candidates(repo)
    assert str(repo / "venv" / "bin" / "hermes") in cands
    assert str(repo / "scripts" / "hermes-gateway") in cands
    # Every *in-tree* candidate resolves inside the release tree. (The
    # running interpreter / argv[0] may legitimately live elsewhere -- those
    # are reported as out-of-tree on purpose.)
    for in_tree in (
        str(repo / "venv" / "bin" / "hermes"),
        str(repo / "scripts" / "hermes-gateway"),
    ):
        assert rp.classify_launcher(in_tree, repo)["decision"] == rp.IN_RELEASE
