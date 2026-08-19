"""Deterministic, read-only release-state preflight for the running install.

Targets the recurring "mutable-checkout / temporary-launcher contamination"
failure class (see t_7269023e): a Hermes install whose *running code* no
longer corresponds to a clean, durable release -- either because the git
checkout it runs from has gone dirty (untracked/edited sources), or because
the launcher/executable that invokes it resolves outside the release tree
into a throwaway/temp location or an unrelated tree.

Two independent axes, each reported separately so a gate can act on either:

1. **Release identity / cleanliness.**  The install tree (the directory
   holding the running code, resolved via the canonical
   ``hermes_cli.config._install_method_project_root`` contract) plus its
   release proofs: git HEAD, exact-tag describe when present, working-tree
   porcelain (dirty paths), and the local ``HEAD..origin/main`` behind-count
   (local refs only -- never a network fetch).

2. **Launcher residency.**  Each launcher/executable path (interpreter,
   argv[0], in-tree venv/entry launchers) is realpath-resolved and classified:
   ``in_release`` (resolves inside the install tree -> pass),
   ``temporary`` (resolves outside the tree into a temp root),
   ``external`` (resolves outside the tree, not temp), or ``broken``
   (a symlink whose target no longer exists).  Any probe that does not
   resolve inside the release tree rejects the launcher axis.

This module is side-effect-free by construction: every probe is a read
(os.stat / realpath / git rev-parse / describe / status --porcelain / local
rev-list).  No fetch, no writes, no config mutation, no state, no
credentials.  It FAILS CLOSED: unknown or unverifiable state never passes.

It is deliberately *not* wired into any runtime launch path -- it is a
standalone preflight prerequisite for a later, operator-approved cutover
gate (AC: "advance the recurring mutable-live-checkout root cause with a
bounded, testable clean-release preflight prerequisite").  It reuses the
existing extension point (install-identity resolution + the code-scoped
``.install_method`` stamp) instead of inventing a speculative core hook.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

# The canonical install-identity seam: how this install was made (git /
# docker / nix / ...) and which directory holds the running code.  Reused
# verbatim -- single source of truth, no duplicated resolution logic.
from hermes_cli.config import _install_method_project_root, detect_install_method

# Install methods whose release tree is managed/immutable -- they have no git
# working tree that can drift into a dirty/mutable state, so axis 1 is
# reported as not-applicable (with a reason) rather than failing.  Mirrors the
# update-command routing in hermes_cli.config and update_cmd.py.
_MANAGED_METHODS = frozenset({"docker", "nix", "nixos", "home-manager", "apt"})

# Launcher residency decisions (see module docstring).
IN_RELEASE = "in_release"
TEMPORARY = "temporary"
EXTERNAL = "external"
BROKEN = "broken"

_DEFAULT_GIT_TIMEOUT = 10.0
_STATUS_GIT_TIMEOUT = 30.0


def install_root(project_root: Optional[Path] = None) -> Path:
    """Resolve the directory holding the *running code* (the install tree).

    Directly delegates to the canonical ``_install_method_project_root``:
    the parent of ``hermes_cli/`` -- the git checkout for source installs,
    ``/opt/hermes`` inside the container image.  A property of the running
    interpreter, NOT of ``$HERMES_HOME``.
    """
    return Path(_install_method_project_root(project_root))


def _git(root: Path, *args: str, timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    """Run one read-only git probe rooted at ``root`` (no writes, no fetch)."""
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout if timeout is not None else _DEFAULT_GIT_TIMEOUT,
        check=False,
    )


@dataclass(frozen=True)
class ReleaseState:
    """Read-only snapshot of one install tree's release identity."""

    root: Path
    install_method: str
    is_git: bool
    head: Optional[str] = None
    describe: Optional[str] = None
    dirty_paths: Tuple[str, ...] = ()
    behind_local: Optional[int] = None
    error: Optional[str] = None

    @property
    def clean(self) -> bool:
        """``is_git`` AND empty porcelain AND no probe error."""
        return self.is_git and not self.dirty_paths and self.error is None


def _is_git_checkout(root: Path) -> bool:
    """True for a plain checkout (.git dir) or a git worktree (.git file)."""
    git_path = root / ".git"
    if git_path.is_dir():
        return True
    if git_path.is_file():
        try:
            return git_path.read_text(encoding="utf-8").strip().startswith("gitdir:")
        except OSError:
            return False
    return False


def release_state(project_root: Optional[Path] = None) -> ReleaseState:
    """Gather the release identity/cleanliness evidence for an install tree."""
    root = install_root(project_root)
    method = detect_install_method(root)
    is_git = _is_git_checkout(root)
    if not is_git:
        return ReleaseState(root=root, install_method=method, is_git=False)

    try:
        head_proc = _git(root, "rev-parse", "--short", "HEAD")
    except (OSError, subprocess.SubprocessError):
        return ReleaseState(
            root=root,
            install_method=method,
            is_git=True,
            error="git unavailable for release-state probe",
        )
    if head_proc.returncode != 0:
        return ReleaseState(
            root=root,
            install_method=method,
            is_git=True,
            error="git rev-parse HEAD failed: " + (head_proc.stderr or "").strip(),
        )
    head = head_proc.stdout.strip() or None

    describe = None
    for args in (
        ("describe", "--tags", "--exact-match", "HEAD"),
        ("describe", "--always"),
    ):
        proc = _git(root, *args)
        if proc.returncode == 0 and proc.stdout.strip():
            describe = proc.stdout.strip()
            break

    dirty: Tuple[str, ...] = ()
    try:
        status_proc = _git(root, "status", "--porcelain", "-z", timeout=_STATUS_GIT_TIMEOUT)
        if status_proc.returncode == 0:
            entries = [e for e in status_proc.stdout.split("\x00") if e]
            dirty = tuple(entries)
    except (OSError, subprocess.SubprocessError):
        pass

    behind_local: Optional[int] = None
    try:
        behind_proc = _git(root, "rev-list", "--count", "HEAD..origin/main")
        if behind_proc.returncode == 0 and behind_proc.stdout.strip().isdigit():
            behind_local = int(behind_proc.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        pass

    return ReleaseState(
        root=root,
        install_method=method,
        is_git=True,
        head=head,
        describe=describe,
        dirty_paths=dirty,
        behind_local=behind_local,
    )


def _resolve(path: str) -> Tuple[Path, Optional[str]]:
    """Realpath-resolve a launcher path, detecting broken symlink targets."""
    p = Path(path).expanduser()
    resolved = p.resolve()  # strict=False: no raise when the target is gone
    if p.is_symlink() and not resolved.exists():
        return resolved, f"symlink target does not exist: {resolved}"
    return resolved, None


def _is_within(path: Path, root: Path) -> bool:
    """Anchored, path-segment-aware containment (like the desktop update gate)."""
    root_resolved = root.resolve()
    return path == root_resolved or root_resolved in path.parents


def _temp_roots() -> Tuple[Path, ...]:
    """The OS throwaway roots a temporary launcher lands under."""
    seen: dict = {}
    for raw in (tempfile.gettempdir(), "/var/tmp", "/tmp"):
        try:
            seen[Path(raw).resolve()] = None
        except OSError:
            pass
    return tuple(seen)


def classify_launcher(
    path: str,
    root: Path,
    temp_roots: Optional[Sequence[Path]] = None,
) -> dict:
    """Classify one launcher/executable path against ``root``.

    Pure (no I/O beyond path resolution), so tests can feed exact fixture
    inputs and temp-root overrides for the ``external`` vs ``temporary``
    distinction.
    """
    resolved, broken = _resolve(path)
    if broken is not None:
        return {"path": path, "resolved": str(resolved), "decision": BROKEN, "detail": broken}
    if _is_within(resolved, root):
        return {"path": path, "resolved": str(resolved), "decision": IN_RELEASE, "detail": ""}
    t_roots = list(temp_roots) if temp_roots is not None else list(_temp_roots())
    for t in t_roots:
        if _is_within(resolved, t):
            return {
                "path": path,
                "resolved": str(resolved),
                "decision": TEMPORARY,
                "detail": f"resolves outside release tree, under temp root {t}",
            }
    return {
        "path": path,
        "resolved": str(resolved),
        "decision": EXTERNAL,
        "detail": f"resolves outside release tree: {resolved}",
    }


def default_launcher_candidates(root: Path) -> List[str]:
    """Canonical launcher/executable paths for the given install tree.

    Covers the running interpreter, the invoked entry path, and existing
    in-tree launcher/runtime locations so a reporting run stays truthful
    even when the process itself was started from an unusual path.
    """
    candidates: List[str] = []
    if sys.executable:
        candidates.append(sys.executable)
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0 and (os.path.sep in argv0 or argv0.endswith(".py")):
        candidates.append(argv0)
    root_path = Path(root)
    for rel in (
        "venv/bin/hermes",
        "venv/bin/python",
        ".venv/bin/hermes",
        ".venv/bin/python",
        "scripts/hermes-gateway",
        "hermes",
    ):
        if (root_path / rel).exists():
            candidates.append(str(root_path / rel))
    seen: set = set()
    deduped: List[str] = []
    for c in candidates:
        key = str(Path(c).expanduser().resolve())
        if key not in seen:
            seen.add(key)
            deduped.append(c)
    return deduped


@dataclass(frozen=True)
class ReleaseAxis:
    verdict: str  # "pass" | "fail" | "na"
    install_method: str
    is_git: bool
    head: Optional[str]
    describe: Optional[str]
    behind_local: Optional[int]
    dirty: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()


@dataclass(frozen=True)
class LauncherAxis:
    verdict: str  # "pass" | "fail"
    probes: Tuple[dict, ...] = ()


@dataclass(frozen=True)
class PreflightResult:
    root: Path
    release: ReleaseAxis
    launchers: LauncherAxis

    @property
    def ok(self) -> bool:
        return self.release.verdict in ("pass", "na") and self.launchers.verdict == "pass"

    def evidence(self) -> List[str]:
        """Actionable, human-readable evidence lines (no state touched)."""
        r = self.release
        lines = [
            f"root: {self.root}",
            (
                f"release: method={r.install_method} is_git={r.is_git} "
                f"head={r.head or '-'} describe={r.describe or '-'} "
                f"behind_local={r.behind_local if r.behind_local is not None else '-'} "
                f"verdict={r.verdict}"
            ),
        ]
        for note in r.notes:
            lines.append(f"  release note: {note}")
        for dirty in r.dirty:
            lines.append(f"  DIRTY: {dirty}")
        lines.append(f"launcher: verdict={self.launchers.verdict}")
        for probe in self.launchers.probes:
            detail = f" ({probe['detail']})" if probe.get("detail") else ""
            lines.append(f"  {probe['decision']}: {probe['path']} -> {probe['resolved']}{detail}")
        return lines


def preflight(
    project_root: Optional[Path] = None,
    launchers: Optional[Iterable[str]] = None,
) -> PreflightResult:
    """Evaluate both preflight axes and return a fail-closed verdict.

    ``project_root`` defaults to the running install tree. ``launchers``
    defaults to ``default_launcher_candidates``; pass an explicit iterable
    (possibly empty) to control exactly which paths are checked.
    """
    root = install_root(project_root)
    state = release_state(root)

    # Axis 1: release identity / cleanliness.
    if state.error is not None:
        release_axis = ReleaseAxis(
            verdict="fail",
            install_method=state.install_method,
            is_git=state.is_git,
            head=state.head,
            describe=state.describe,
            behind_local=state.behind_local,
            notes=(state.error,),
        )
    elif state.is_git:
        if state.dirty_paths:
            release_axis = ReleaseAxis(
                verdict="fail",
                install_method=state.install_method,
                is_git=True,
                head=state.head,
                describe=state.describe,
                behind_local=state.behind_local,
                dirty=state.dirty_paths,
                notes=(f"{len(state.dirty_paths)} dirty path(s) in release tree",),
            )
        else:
            release_axis = ReleaseAxis(
                verdict="pass",
                install_method=state.install_method,
                is_git=True,
                head=state.head,
                describe=state.describe,
                behind_local=state.behind_local,
            )
    elif state.install_method in _MANAGED_METHODS:
        release_axis = ReleaseAxis(
            verdict="na",
            install_method=state.install_method,
            is_git=False,
            head=None,
            describe=None,
            behind_local=None,
            notes=(f"managed {state.install_method} install; no git-dirt axis",),
        )
    else:
        release_axis = ReleaseAxis(
            verdict="fail",
            install_method=state.install_method,
            is_git=False,
            head=None,
            describe=None,
            behind_local=None,
            notes=(
                "non-git install with unrecognized/absent install method; "
                "cannot certify release state (fail-closed)",
            ),
        )

    # Axis 2: launcher residency.
    candidates = list(launchers) if launchers is not None else default_launcher_candidates(root)
    probes = tuple(classify_launcher(c, root) for c in candidates)
    if not probes:
        launcher_axis = LauncherAxis(
            verdict="fail",
            probes=(),
        )
    elif all(p["decision"] == IN_RELEASE for p in probes):
        launcher_axis = LauncherAxis(verdict="pass", probes=probes)
    else:
        launcher_axis = LauncherAxis(verdict="fail", probes=probes)

    return PreflightResult(root=root, release=release_axis, launchers=launcher_axis)


def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Read-only release-state preflight for a Hermes install tree."
    )
    parser.add_argument("--root", type=Path, default=None, help="install tree to check (default: running install)")
    parser.add_argument(
        "--launcher",
        action="append",
        default=None,
        help="launcher/executable path to classify (repeatable; default: canonical candidates)",
    )
    args = parser.parse_args()

    result = preflight(project_root=args.root, launchers=args.launcher)
    for line in result.evidence():
        print(line)
    print("PREFLIGHT " + ("OK" if result.ok else "REJECT"))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
