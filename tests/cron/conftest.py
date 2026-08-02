"""Cron-test fixtures.

Provides a default ``HERMES_MODEL`` for cron run_job tests so each one
doesn't have to spell out a model. The global conftest blanks
HERMES_MODEL hermetically; without this autouse fixture every cron test
that exercises ``run_job`` would hit the fail-fast guard added in
``cron/scheduler.py`` (see issue #23979) and have to be rewritten.

Tests that specifically need ``HERMES_MODEL`` unset — model-resolution
edge cases — call ``monkeypatch.delenv("HERMES_MODEL", raising=False)``
inside the test, which overrides this fixture's value for that scope.
"""

import pytest


@pytest.fixture(autouse=True)
def _default_cron_test_model(monkeypatch):
    """Pin a default HERMES_MODEL so cron run_job tests have a resolvable model."""
    monkeypatch.setenv("HERMES_MODEL", "test-cron-default-model")
    yield


@pytest.fixture(autouse=True)
def _default_cron_skill_integrity_pass(monkeypatch):
    """Keep unrelated scheduler tests focused on their own execution seam.

    Dependency-integrity behavior is covered explicitly in
    ``TestRunJobDependencyIntegrity``; ordinary cron tests should not require a
    live canonical skills checkout in their temporary HERMES_HOME.
    """
    monkeypatch.setattr("cron.scheduler._validate_cron_skill_dependencies", lambda: [])
    yield
