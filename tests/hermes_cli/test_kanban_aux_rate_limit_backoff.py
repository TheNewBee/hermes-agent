"""Kanban specify/decompose aux calls back off after a 429 instead of retrying every tick."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent.auxiliary_health import fallback_candidate_quarantine_ttl
from hermes_cli import kanban_specify as spec

NOW = 1_794_600_000.0


class RateLimitError(Exception):  # matched by class name in _is_rate_limit_error
    status_code = 429

    def __init__(self, body: dict):
        super().__init__(f"Error code: 429 - {body}")
        self.body = body


def _usage_limit(resets_at: float) -> RateLimitError:
    return RateLimitError({"error": {"type": "usage_limit_reached", "resets_at": int(resets_at)}})


def _plain_429() -> RateLimitError:
    return RateLimitError({"type": "error", "error": {"type": "rate_limit_error", "message": "try later"}})


@pytest.fixture
def clock(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    now = [NOW]
    monkeypatch.setattr("time.time", lambda: now[0])
    yield now
    spec._aux_backoff.clear()


def _call(monkeypatch, side_effect) -> MagicMock:
    llm = MagicMock(side_effect=side_effect)
    monkeypatch.setattr("agent.auxiliary_client.call_llm", llm)
    spec._call_aux("decompose", "t_x", aux_task="kanban_decomposer", system="s", user="u",
                   max_tokens=10, timeout=5)
    return llm


def test_usage_limit_reset_is_honoured_not_60s(clock, monkeypatch):
    reset_at = NOW + 299_342
    assert fallback_candidate_quarantine_ttl("rate limit", _usage_limit(reset_at)) == pytest.approx(299_342)
    assert fallback_candidate_quarantine_ttl("rate limit", _plain_429()) == 60.0  # no hint: transient hold
    assert fallback_candidate_quarantine_ttl("rate limit") == 60.0
    bodyless = Exception("Error code: 429 - {'error': {'type': 'usage_limit_reached', 'resets_in_seconds': 299342}}")
    assert fallback_candidate_quarantine_ttl("rate limit", bodyless) == pytest.approx(299_342)

    assert _call(monkeypatch, _usage_limit(reset_at)).call_count == 1
    for t in (NOW + 61, NOW + 3601, reset_at - 1):
        clock[0] = t
        assert _call(monkeypatch, AssertionError("retried before reset")).call_count == 0
    clock[0] = reset_at + 1
    assert _call(monkeypatch, _plain_429()).call_count == 1


def test_plain_429_backs_off_exponentially_with_cap(clock, monkeypatch):
    waits = []
    for _ in range(8):
        assert _call(monkeypatch, _plain_429()).call_count == 1
        not_before, _n = spec._aux_backoff[spec._backoff_key("kanban_decomposer")]
        waits.append(not_before - clock[0])
        clock[0] = not_before - 1
        assert _call(monkeypatch, AssertionError("retried inside backoff")).call_count == 0
        clock[0] = not_before
    assert waits == [60, 120, 240, 480, 960, 1920, 3600, 3600]


def test_success_clears_backoff(clock, monkeypatch):
    _call(monkeypatch, _plain_429())
    clock[0] += 60
    resp = MagicMock()
    resp.choices[0].message.content = "ok"
    _call(monkeypatch, [resp])
    assert spec._backoff_key("kanban_decomposer") not in spec._aux_backoff
