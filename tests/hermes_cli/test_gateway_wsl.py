"""Tests for WSL detection and WSL-aware gateway behavior."""

import io
import json
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, mock_open

import pytest

import hermes_cli.gateway as gateway
import hermes_constants


# =============================================================================
# is_wsl() in hermes_constants
# =============================================================================

class TestIsWsl:
    def setup_method(self):
        hermes_constants._wsl_detected = None

    def test_detects_wsl2(self):
        fake_content = (
            "Linux version 5.15.146.1-microsoft-standard-WSL2 "
            "(gcc (GCC) 11.2.0) #1 SMP Thu Jan 11 04:09:03 UTC 2024\n"
        )
        with patch("builtins.open", mock_open(read_data=fake_content)):
            assert hermes_constants.is_wsl() is True

    def test_detects_wsl1(self):
        fake_content = (
            "Linux version 4.4.0-19041-Microsoft "
            "(Microsoft@Microsoft.com) (gcc version 5.4.0) #1\n"
        )
        with patch("builtins.open", mock_open(read_data=fake_content)):
            assert hermes_constants.is_wsl() is True

    def test_native_linux(self):
        fake_content = (
            "Linux version 6.5.0-44-generic (buildd@lcy02-amd64-015) "
            "(x86_64-linux-gnu-gcc-12 (Ubuntu 12.3.0-1ubuntu1~22.04) 12.3.0) #44\n"
        )
        with patch("builtins.open", mock_open(read_data=fake_content)):
            assert hermes_constants.is_wsl() is False

    def test_no_proc_version(self):
        with patch("builtins.open", side_effect=FileNotFoundError):
            assert hermes_constants.is_wsl() is False

    def test_result_is_cached(self):
        hermes_constants._wsl_detected = True
        with patch("builtins.open", side_effect=FileNotFoundError):
            assert hermes_constants.is_wsl() is True


# =============================================================================
# _wsl_systemd_operational() in gateway
# =============================================================================

class TestWslSystemdOperational:
    def test_running(self, monkeypatch):
        monkeypatch.setattr(
            gateway.subprocess, "run",
            lambda *a, **kw: SimpleNamespace(
                returncode=0, stdout="running\n", stderr=""
            ),
        )
        assert gateway._wsl_systemd_operational() is True

    def test_degraded(self, monkeypatch):
        monkeypatch.setattr(
            gateway.subprocess, "run",
            lambda *a, **kw: SimpleNamespace(
                returncode=1, stdout="degraded\n", stderr=""
            ),
        )
        assert gateway._wsl_systemd_operational() is True

    def test_starting(self, monkeypatch):
        monkeypatch.setattr(
            gateway.subprocess, "run",
            lambda *a, **kw: SimpleNamespace(
                returncode=1, stdout="starting\n", stderr=""
            ),
        )
        assert gateway._wsl_systemd_operational() is True

    def test_offline_no_systemd(self, monkeypatch):
        monkeypatch.setattr(
            gateway.subprocess, "run",
            lambda *a, **kw: SimpleNamespace(
                returncode=1, stdout="offline\n", stderr=""
            ),
        )
        assert gateway._wsl_systemd_operational() is False

    def test_systemctl_not_found(self, monkeypatch):
        monkeypatch.setattr(
            gateway.subprocess, "run",
            MagicMock(side_effect=FileNotFoundError),
        )
        assert gateway._wsl_systemd_operational() is False

    def test_timeout(self, monkeypatch):
        monkeypatch.setattr(
            gateway.subprocess, "run",
            MagicMock(side_effect=subprocess.TimeoutExpired("systemctl", 5)),
        )
        assert gateway._wsl_systemd_operational() is False


# =============================================================================
# supports_systemd_services() WSL integration
# =============================================================================

class TestSupportsSystemdServicesWSL:
    def test_wsl_with_systemd(self, monkeypatch):
        monkeypatch.setattr(gateway, "is_linux", lambda: True)
        monkeypatch.setattr(gateway, "is_termux", lambda: False)
        monkeypatch.setattr(gateway, "is_wsl", lambda: True)
        monkeypatch.setattr(gateway, "_wsl_systemd_operational", lambda: True)
        assert gateway.supports_systemd_services() is True

    def test_wsl_without_systemd(self, monkeypatch):
        monkeypatch.setattr(gateway, "is_linux", lambda: True)
        monkeypatch.setattr(gateway, "is_termux", lambda: False)
        monkeypatch.setattr(gateway, "is_wsl", lambda: True)
        monkeypatch.setattr(gateway, "_wsl_systemd_operational", lambda: False)
        assert gateway.supports_systemd_services() is False

    def test_native_linux(self, monkeypatch):
        monkeypatch.setattr(gateway, "is_linux", lambda: True)
        monkeypatch.setattr(gateway, "is_termux", lambda: False)
        monkeypatch.setattr(gateway, "is_wsl", lambda: False)
        assert gateway.supports_systemd_services() is True

    def test_termux_still_excluded(self, monkeypatch):
        monkeypatch.setattr(gateway, "is_linux", lambda: True)
        monkeypatch.setattr(gateway, "is_termux", lambda: True)
        assert gateway.supports_systemd_services() is False


# =============================================================================
# WSL messaging in gateway commands
# =============================================================================

class TestGatewayCommandWSLMessages:
    def test_install_wsl_no_systemd(self, monkeypatch, capsys):
        monkeypatch.setattr(gateway, "is_linux", lambda: True)
        monkeypatch.setattr(gateway, "is_termux", lambda: False)
        monkeypatch.setattr(gateway, "is_wsl", lambda: True)
        monkeypatch.setattr(gateway, "supports_systemd_services", lambda: False)
        monkeypatch.setattr(gateway, "is_macos", lambda: False)
        monkeypatch.setattr(gateway, "is_managed", lambda: False)

        args = SimpleNamespace(
            gateway_command="install", force=False, system=False,
            run_as_user=None,
        )
        with pytest.raises(SystemExit) as exc_info:
            gateway.gateway_command(args)
        assert exc_info.value.code == 1

        out = capsys.readouterr().out
        assert "WSL detected" in out
        assert "systemd is not running" in out
        assert "hermes gateway run" in out
        assert "tmux" in out

    def test_start_wsl_no_systemd(self, monkeypatch, capsys):
        monkeypatch.setattr(gateway, "is_linux", lambda: True)
        monkeypatch.setattr(gateway, "is_termux", lambda: False)
        monkeypatch.setattr(gateway, "is_wsl", lambda: True)
        monkeypatch.setattr(gateway, "supports_systemd_services", lambda: False)
        monkeypatch.setattr(gateway, "is_macos", lambda: False)

        args = SimpleNamespace(gateway_command="start", system=False)
        with pytest.raises(SystemExit) as exc_info:
            gateway.gateway_command(args)
        assert exc_info.value.code == 1

        out = capsys.readouterr().out
        assert "WSL detected" in out
        assert "hermes gateway run" in out
        assert "wsl.conf" in out

    def test_install_wsl_with_systemd_warns(self, monkeypatch, capsys):
        monkeypatch.setattr(gateway, "is_linux", lambda: True)
        monkeypatch.setattr(gateway, "is_termux", lambda: False)
        monkeypatch.setattr(gateway, "is_wsl", lambda: True)
        monkeypatch.setattr(gateway, "supports_systemd_services", lambda: True)
        monkeypatch.setattr(gateway, "is_macos", lambda: False)
        monkeypatch.setattr(gateway, "is_managed", lambda: False)

        install_called = []
        monkeypatch.setattr(
            gateway, "systemd_install",
            lambda **kwargs: install_called.append(kwargs),
        )

        args = SimpleNamespace(
            gateway_command="install", force=False, system=False,
            run_as_user=None,
        )
        gateway.gateway_command(args)

        out = capsys.readouterr().out
        assert "WSL detected" in out
        assert "may not survive WSL restarts" in out
        assert len(install_called) == 1  # install still proceeded

    def test_status_wsl_running_manual(self, monkeypatch, capsys):
        monkeypatch.setattr(gateway, "supports_systemd_services", lambda: False)
        monkeypatch.setattr(gateway, "is_macos", lambda: False)
        monkeypatch.setattr(gateway, "is_termux", lambda: False)
        monkeypatch.setattr(gateway, "is_wsl", lambda: True)
        monkeypatch.setattr(gateway, "find_gateway_pids", lambda: [12345])
        monkeypatch.setattr(gateway, "_runtime_health_lines", lambda: [])
        monkeypatch.setattr(
            gateway, "get_systemd_unit_path",
            lambda system=False: SimpleNamespace(exists=lambda: False),
        )
        monkeypatch.setattr(
            gateway, "get_launchd_plist_path",
            lambda: SimpleNamespace(exists=lambda: False),
        )

        args = SimpleNamespace(gateway_command="status", deep=False, system=False)
        gateway.gateway_command(args)

        out = capsys.readouterr().out
        assert "WSL note" in out
        assert "tmux-attach" in out

    def test_status_wsl_not_running(self, monkeypatch, capsys):
        monkeypatch.setattr(gateway, "supports_systemd_services", lambda: False)
        monkeypatch.setattr(gateway, "is_macos", lambda: False)
        monkeypatch.setattr(gateway, "is_termux", lambda: False)
        monkeypatch.setattr(gateway, "is_wsl", lambda: True)
        monkeypatch.setattr(gateway, "find_gateway_pids", lambda: [])
        monkeypatch.setattr(gateway, "_runtime_health_lines", lambda: [])
        monkeypatch.setattr(
            gateway, "get_systemd_unit_path",
            lambda system=False: SimpleNamespace(exists=lambda: False),
        )
        monkeypatch.setattr(
            gateway, "get_launchd_plist_path",
            lambda: SimpleNamespace(exists=lambda: False),
        )

        args = SimpleNamespace(gateway_command="status", deep=False, system=False)
        gateway.gateway_command(args)

        out = capsys.readouterr().out
        assert "hermes gateway run" in out
        assert "tmux" in out


# =============================================================================
# find_gateway_pids PID-file fallback
# =============================================================================

class TestFindGatewayPidsFallback:
    def test_falls_back_to_pid_file_when_ps_empty(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(gateway, "is_windows", lambda: False)
        monkeypatch.setattr(gateway, "_get_service_pids", lambda: set())

        def fake_run(cmd, **kwargs):
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway.subprocess, "run", fake_run)

        fake_pid = 48888
        pid_path = tmp_path / "gateway.pid"
        pid_path.write_text(json.dumps({
            "pid": fake_pid,
            "kind": "hermes-gateway",
            "argv": ["python", "-m", "hermes_cli.main", "gateway"],
            "start_time": 123,
        }))
        monkeypatch.setattr(gateway.os, "kill", lambda pid, sig: None)

        import gateway.status as gs
        monkeypatch.setattr(gs, "_get_process_start_time", lambda pid: 123)
        monkeypatch.setattr(gs, "_read_process_cmdline", lambda pid: "python -m hermes_cli.main gateway run")

        pids = gateway.find_gateway_pids()
        assert fake_pid in pids

    def test_no_fallback_when_all_profiles_true(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(gateway, "is_windows", lambda: False)
        monkeypatch.setattr(gateway, "_get_service_pids", lambda: set())

        def fake_run(cmd, **kwargs):
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(gateway.subprocess, "run", fake_run)

        pid_path = tmp_path / "gateway.pid"
        pid_path.write_text(json.dumps({
            "pid": os.getpid(),
            "kind": "hermes-gateway",
            "argv": ["python", "-m", "hermes_cli.main", "gateway"],
            "start_time": 123,
        }))
        monkeypatch.setattr(gateway.os, "kill", lambda pid, sig: None)

        import gateway.status as gs
        monkeypatch.setattr(gs, "_get_process_start_time", lambda pid: 123)
        monkeypatch.setattr(gs, "_read_process_cmdline", lambda pid: None)

        pids = gateway.find_gateway_pids(all_profiles=True)
        assert os.getpid() not in pids
        assert pids == []

    def test_no_fallback_when_ps_discovers_pids(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(gateway, "is_windows", lambda: False)
        monkeypatch.setattr(gateway, "_get_service_pids", lambda: set())

        fake_ps_output = "12345 python -m hermes_cli.main gateway run\n"
        monkeypatch.setattr(
            gateway.subprocess, "run",
            lambda cmd, **kw: SimpleNamespace(returncode=0, stdout=fake_ps_output, stderr=""),
        )

        pids = gateway.find_gateway_pids()
        assert 12345 in pids


# =============================================================================
# tmux gateway helpers
# =============================================================================

class TestTmuxSessionName:
    def test_default_profile(self, monkeypatch):
        monkeypatch.setattr(gateway, "_profile_suffix", lambda: "")
        assert gateway._tmux_session_name() == "hermes-gateway"

    def test_dev_profile(self, monkeypatch):
        monkeypatch.setattr(gateway, "_profile_suffix", lambda: "dev")
        assert gateway._tmux_session_name() == "hermes-dev-gateway"


class TestTmuxStart:
    def test_idempotent_when_already_running(self, monkeypatch, capsys):
        monkeypatch.setattr(gateway, "is_linux", lambda: True)
        monkeypatch.setattr(gateway, "is_wsl", lambda: True)
        monkeypatch.setattr(gateway, "_tmux_bin", lambda: "/usr/bin/tmux")
        monkeypatch.setattr(gateway, "_tmux_session_name", lambda: "hermes-dev-gateway")
        monkeypatch.setattr(gateway, "find_gateway_pids", lambda: [4954])
        monkeypatch.setattr(gateway, "_tmux_session_exists", lambda name: True)

        gateway.tmux_start()

        out = capsys.readouterr().out
        assert "Already running" in out
        assert "4954" in out

    def test_starts_when_not_running(self, monkeypatch, capsys):
        monkeypatch.setattr(gateway, "is_linux", lambda: True)
        monkeypatch.setattr(gateway, "is_wsl", lambda: False)
        monkeypatch.setattr(gateway, "_tmux_bin", lambda: "/usr/bin/tmux")
        monkeypatch.setattr(gateway, "_tmux_session_name", lambda: "hermes-gateway")
        monkeypatch.setattr(gateway, "find_gateway_pids", lambda: [])
        monkeypatch.setattr(gateway, "_tmux_session_exists", lambda name: False)
        monkeypatch.setattr(gateway, "_tmux_gateway_command", lambda: ["python", "-m", "hermes_cli.main", "gateway", "run"])

        run_calls = []
        monkeypatch.setattr(
            gateway.subprocess, "run",
            lambda *args, **kwargs: run_calls.append(args) or SimpleNamespace(returncode=0),
        )

        gateway.tmux_start()

        out = capsys.readouterr().out
        assert "Started gateway in tmux" in out
        assert len(run_calls) == 1

    def test_unsupported_platform(self, monkeypatch):
        monkeypatch.setattr(gateway, "is_linux", lambda: False)
        monkeypatch.setattr(gateway, "is_wsl", lambda: False)
        with pytest.raises(SystemExit):
            gateway.tmux_start()


class TestTmuxStatus:
    def test_reports_session_and_pid(self, monkeypatch, capsys):
        monkeypatch.setattr(gateway, "is_linux", lambda: True)
        monkeypatch.setattr(gateway, "is_wsl", lambda: True)
        monkeypatch.setattr(gateway, "_tmux_session_name", lambda: "hermes-dev-gateway")
        monkeypatch.setattr(gateway, "_tmux_session_exists", lambda name: True)
        monkeypatch.setattr(gateway, "find_gateway_pids", lambda: [4954])

        import gateway.status as gs
        monkeypatch.setattr(gs, "is_runtime_state_live", lambda state: True)
        monkeypatch.setattr(gs, "read_runtime_status", lambda: {"gateway_state": "running"})

        gateway.tmux_status()

        out = capsys.readouterr().out
        assert "hermes-dev-gateway" in out
        assert "4954" in out
        assert "Session exists: True" in out
        assert "healthy" in out

    def test_reports_not_running(self, monkeypatch, capsys):
        monkeypatch.setattr(gateway, "is_linux", lambda: True)
        monkeypatch.setattr(gateway, "is_wsl", lambda: True)
        monkeypatch.setattr(gateway, "_tmux_session_name", lambda: "hermes-gateway")
        monkeypatch.setattr(gateway, "_tmux_session_exists", lambda name: False)
        monkeypatch.setattr(gateway, "find_gateway_pids", lambda: [])

        gateway.tmux_status()

        out = capsys.readouterr().out
        assert "not running" in out.lower()


class TestGatewayStatusRecommendsTmuxOnWsl:
    def test_status_running_recommends_tmux_attach(self, monkeypatch, capsys):
        monkeypatch.setattr(gateway, "supports_systemd_services", lambda: False)
        monkeypatch.setattr(gateway, "is_macos", lambda: False)
        monkeypatch.setattr(gateway, "is_termux", lambda: False)
        monkeypatch.setattr(gateway, "is_wsl", lambda: True)
        monkeypatch.setattr(gateway, "find_gateway_pids", lambda: [12345])
        monkeypatch.setattr(gateway, "_runtime_health_lines", lambda: [])
        monkeypatch.setattr(
            gateway, "get_systemd_unit_path",
            lambda system=False: SimpleNamespace(exists=lambda: False),
        )
        monkeypatch.setattr(
            gateway, "get_launchd_plist_path",
            lambda: SimpleNamespace(exists=lambda: False),
        )

        args = SimpleNamespace(gateway_command="status", deep=False, system=False)
        gateway.gateway_command(args)

        out = capsys.readouterr().out
        assert "tmux-attach" in out
        assert "tmux-status" in out

    def test_status_not_running_recommends_tmux_start(self, monkeypatch, capsys):
        monkeypatch.setattr(gateway, "supports_systemd_services", lambda: False)
        monkeypatch.setattr(gateway, "is_macos", lambda: False)
        monkeypatch.setattr(gateway, "is_termux", lambda: False)
        monkeypatch.setattr(gateway, "is_wsl", lambda: True)
        monkeypatch.setattr(gateway, "find_gateway_pids", lambda: [])
        monkeypatch.setattr(gateway, "_runtime_health_lines", lambda: [])
        monkeypatch.setattr(
            gateway, "get_systemd_unit_path",
            lambda system=False: SimpleNamespace(exists=lambda: False),
        )
        monkeypatch.setattr(
            gateway, "get_launchd_plist_path",
            lambda: SimpleNamespace(exists=lambda: False),
        )

        args = SimpleNamespace(gateway_command="status", deep=False, system=False)
        gateway.gateway_command(args)

        out = capsys.readouterr().out
        assert "tmux-start" in out
