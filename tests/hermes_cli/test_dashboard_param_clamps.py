"""Dashboard query-param clamps (#39200 + #74778 salvage).

FastAPI Query bounds reject out-of-range values at the validation layer
(422) instead of letting them reach SQL/insights code: an unbounded
``limit`` drags every session row out of SQLite in one hit (multiplied
across every profile's state.db on the fan-out endpoint), and an
unbounded/inverted ``days`` forces full-history InsightsEngine work.
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402
from pathlib import Path  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", "clamp-test-token")
    from hermes_cli import web_server
    import hermes_state

    # Hermetic guard: the dashboard resolves both its session-store path and
    # its auth token at MODULE IMPORT time — hermes_state.DEFAULT_DB_PATH on
    # line 348 and web_server._SESSION_TOKEN on line 478. In a full-suite
    # pytest process those globals are frozen to an EARLIER file's values
    # before this fixture runs: test_dashboard_admin_endpoints.py re-points
    # DEFAULT_DB_PATH directly (and never restores it), and whatever test
    # imports web_server first captures that file's HERMES_DASHBOARD_SESSION_TOKEN
    # (or a freshly-minted token when the var is unset). Setting HERMES_HOME
    # and the token env above is therefore NOT sufficient in isolation: the
    # analytics/session handlers under test would open a stale DB path
    # (intermittent 500 under full-suite load) or reject the Bearer header the
    # fixture sends (401). Re-pin both module globals to this test's isolated
    # values for the test's lifetime; monkeypatch restores them afterwards.
    # Production resolution is unchanged — only this test's environment is
    # pinned.
    monkeypatch.setattr(
        hermes_state, "DEFAULT_DB_PATH", str(Path(tmp_path) / "state.db")
    )
    monkeypatch.setattr(web_server, "_SESSION_TOKEN", "clamp-test-token")

    with TestClient(web_server.app, raise_server_exceptions=False) as c:
        c.headers["Authorization"] = "Bearer clamp-test-token"
        yield c


class TestSessionPaginationClamps:
    def test_oversized_limit_rejected(self, client):
        r = client.get("/api/sessions", params={"limit": 10_000})
        assert r.status_code == 422

    def test_negative_limit_rejected(self, client):
        r = client.get("/api/sessions", params={"limit": -1})
        assert r.status_code == 422

    def test_profile_fanout_limit_clamped(self, client):
        r = client.get("/api/profiles/sessions", params={"limit": 10_000})
        assert r.status_code == 422

    def test_profile_fanout_accepts_real_desktop_maximum(self, client):
        # Desktop callers use limit=200 (ARCHIVED_FETCH_LIMIT, command
        # palette) and electron over-fetches limit+offset — the clamp must
        # sit ABOVE real client maxima, not break them.
        r = client.get("/api/profiles/sessions", params={"limit": 200, "offset": 120})
        assert r.status_code == 200

    def test_in_range_limit_accepted(self, client):
        r = client.get("/api/sessions", params={"limit": 50})
        assert r.status_code == 200


class TestAnalyticsDaysClamps:
    @pytest.mark.parametrize("days", [0, -5, 100_000])
    def test_out_of_range_days_rejected(self, client, days):
        r = client.get("/api/analytics/usage", params={"days": days})
        assert r.status_code == 422

    def test_in_range_days_accepted(self, client):
        r = client.get("/api/analytics/usage", params={"days": 30})
        assert r.status_code == 200
