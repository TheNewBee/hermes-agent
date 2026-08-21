# Release-state preflight: mutable-checkout / temporary-launcher contamination

**Driver ticket:** `t_7269023e` (recurring mutable-live-checkout root cause)
**Status:** prerequisite shipped in `5d2a54d3a6` (`feat(hermes_cli): add deterministic release-state preflight validator (t_7269023e)`); **not yet wired into any live gate** — see [Next step](#next-step-bounded-gate-consumption).
**Severity:** P2 — recurrent activation/launcher integrity failures; does not corrupt data but blocks safe activation and silently runs throwaway code paths.

## Summary

A Hermes source install occasionally runs from a release tree that is no longer a clean, durable release: the checked-out runtime (`hermes_cli/…`) has drifted (untracked/edited files) and/or the launcher that invokes it resolves **outside** the release tree into a throwaway location (a recycled `/tmp/<pm-…>` worktree venv, a relinked `~/.local/bin/hermes` trampoline, or a decidedly-different runtime). When the throwaway directory is later reaped, the launcher becomes stale/broken; when the tree is dirty, the running code is not the released code. Both make the active install not correspond to any tagged/released state.

## Existing extension point (reused, not duplicated)

Install identity already lives in `hermes_cli.config`:

- `_install_method_project_root(project_root)` — resolves the directory holding the **running code** ("the parent of `hermes_cli/` … a property of the running interpreter, NOT of `$HERMES_HOME`").
- `detect_install_method(root)` — authoritative install method, code-scoped `.install_method` stamp first (shared-`$HERMES_HOME`-immune), then managed/git fallbacks.

The preflight consumes that contract verbatim rather than re-resolving it, so it cannot drift from the canonical seam. It stays **stdlib + read-only subprocess** (no `main.py`/startup wiring): it is a prerequisite leaf for a later gate, not a runtime hook.

## Preflight contract (`hermes_cli/release_preflight.py`)

Two independent, machine-checkable axes; **fail-closed** (unknown state never passes); every probe is read-only (no fetch/write/config/state/credentials):

1. **Release identity/cleanliness** — `release_state(root)` returns install method, `is_git` (plain checkout *and* worktree `.git`-file), HEAD, exact-tag `describe`, `status --porcelain -z` dirty paths, and `behind_local` (`HEAD..origin/main`, **local refs only** — never a network fetch).
   Verdict: `pass` (git + clean), `fail` (dirty, actionable path list), `na` (managed/immutable install: docker/nix/nixos/home-manager/apt), or fail-closed `fail` (unrecognized non-git install).
2. **Launcher residency** — `classify_launcher(path, root)` realpath-resolves each launcher: `in_release` / `temporary` (outside tree, under a temp root) / `external` (outside tree, non-temp) / `broken` (dangling symlink). Any non-`in_release` probe rejects the axis.

`preflight(project_root=None, launchers=None)` combines both axes; `PreflightResult.ok` is `(pass or na) and all launchers in_release`. `PreflightResult.evidence()` emits actionable lines (dirty paths, per-probe resolved paths/decisions). Runnable as `python -m hermes_cli.release_preflight [--root PATH] [--launcher PATH]…` (exit 0 = OK, 1 = REJECT).

Fixture coverage: `tests/hermes_cli/test_release_preflight.py` (14 tests) — clean release, dirty tree (tracked + untracked), temporary/external/broken launchers, managed-`na`, unknown fail-closed, empty-candidates fail-closed, worktree `.git` recognition, exact-tag `describe`, local-ref `behind_local`, install-root delegation.

## Next step (bounded gate consumption — approval-gated)

The validator is deliberately **not** wired into any runtime path in `5d2a54d3a6`. The later, approval-gated lane should:

1. Invoke, before any activation/cutover and with explicit durable paths:
   `python -m hermes_cli.release_preflight --root <durable root> --launcher <durable>/venv/bin/hermes --launcher <durable>/venv/bin/python`
   (durable root for this workspace: `/home/jy/.hermes/hermes-agent`; live main was clean but **782 commits behind** origin/main by local ref at cutover time).
2. Fail closed on `ok == False`; on rejection, surface `evidence()` (dirty path list / per-probe launcher decisions) as the actionable report.
3. Treat any launcher classified `temporary`/`external`/`broken` as the contaminated-launcher signature: re-point the launcher to the durable venv, then re-run green before proceeding.

## Operator boundary (not agent-owned)

The actual service switch / launcher re-point, and any live-gate wiring or cutover, remain an **explicit operator-approval boundary with rollback requirements** (per the PM brief for `t_7269023e`: "do not claim cutover readiness"). This lane may not modify live launchers, gateway, cron/config/credentials, or Kanban; it only ships the tested prerequisite and this discoverable handoff.
