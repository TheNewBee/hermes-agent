"""Gateway startup honours ``logging.memory_monitor.enabled`` (the [MEMORY] logger had no call site)."""

from unittest.mock import patch

import pytest

from gateway import memory_monitor as mm
from gateway import run as gateway_run


@pytest.fixture(autouse=True)
def _monitor_stopped():
    mm.stop_memory_monitoring(timeout=1.0)
    yield
    mm.stop_memory_monitoring(timeout=1.0)


@pytest.mark.parametrize("enabled", [True, False])
def test_runner_starts_memory_monitor_only_when_enabled(enabled):
    runner = object.__new__(gateway_run.GatewayRunner)
    cfg = {"logging": {"memory_monitor": {"enabled": enabled, "interval_seconds": 3600}}}
    with patch("hermes_cli.config.load_config", return_value=cfg):
        runner._start_memory_monitor()
    assert mm.is_running() is enabled
