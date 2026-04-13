"""Tests for gateway runtime health checks and _current_gateway_health."""

from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from hermes_cli.gateway import _runtime_health_lines


def test_runtime_health_lines_include_fatal_platform_and_startup_reason(monkeypatch):
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "gateway_state": "startup_failed",
            "exit_reason": "telegram conflict",
            "platforms": {
                "telegram": {
                    "state": "fatal",
                    "error_message": "another poller is active",
                }
            },
        },
    )

    lines = _runtime_health_lines()

    assert "⚠ telegram: another poller is active" in lines
    assert "⚠ Last startup issue: telegram conflict" in lines


class TestCurrentGatewayHealth:
    def test_stale_running_state_without_pid_not_treated_as_running(self, monkeypatch):
        monkeypatch.setattr(
            "gateway.status.is_runtime_state_live",
            lambda payload: False,
        )
        monkeypatch.setattr(
            "gateway.status.read_runtime_status",
            lambda: {"gateway_state": "running", "pid": 99999},
        )
        from hermes_cli.routing_auto_update import _current_gateway_health
        monkeypatch.setattr(
            "hermes_cli.routing_auto_update.find_gateway_pids",
            lambda: [],
        )
        monkeypatch.setattr(
            "hermes_cli.routing_auto_update.is_linux",
            lambda: False,
        )
        monkeypatch.setattr(
            "hermes_cli.routing_auto_update.is_macos",
            lambda: False,
        )
        gateway_running, service_installed, telegram_connected = _current_gateway_health()
        assert gateway_running is False

    def test_live_pid_file_treated_as_running(self, monkeypatch):
        monkeypatch.setattr(
            "gateway.status.is_runtime_state_live",
            lambda payload: True,
        )
        monkeypatch.setattr(
            "gateway.status.read_runtime_status",
            lambda: {"gateway_state": "running", "pid": 1234},
        )
        from hermes_cli.routing_auto_update import _current_gateway_health
        monkeypatch.setattr(
            "hermes_cli.routing_auto_update.find_gateway_pids",
            lambda: [],
        )
        monkeypatch.setattr(
            "hermes_cli.routing_auto_update.is_linux",
            lambda: False,
        )
        monkeypatch.setattr(
            "hermes_cli.routing_auto_update.is_macos",
            lambda: False,
        )
        gateway_running, service_installed, telegram_connected = _current_gateway_health()
        assert gateway_running is True
