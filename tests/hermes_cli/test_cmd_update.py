"""Tests for cmd_update — branch fallback when remote branch doesn't exist."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli.main import cmd_update, PROJECT_ROOT


def _make_run_side_effect(branch="main", verify_ok=True, commit_count="0"):
    """Build a side_effect function for subprocess.run that simulates git commands."""

    def side_effect(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)

        # git rev-parse --abbrev-ref HEAD  (get current branch)
        if "rev-parse" in joined and "--abbrev-ref" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{branch}\n", stderr="")

        # git rev-parse --verify origin/{branch}  (check remote branch exists)
        if "rev-parse" in joined and "--verify" in joined:
            rc = 0 if verify_ok else 128
            return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="")

        # git rev-list HEAD..origin/{branch} --count
        if "rev-list" in joined:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{commit_count}\n", stderr="")

        # Fallback: return a successful CompletedProcess with empty stdout
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    return side_effect


@pytest.fixture
def mock_args():
    return SimpleNamespace(gateway=False, legacy_stock_update=True)


class TestCmdUpdateBranchFallback:
    """cmd_update falls back to main when current branch has no remote counterpart."""

    @patch("hermes_cli.routing_auto_update._render_markdown_report", return_value="report")
    @patch("hermes_cli.routing_auto_update.run_routing_auto_update")
    @patch("hermes_cli.config.is_managed", return_value=False)
    @patch(
        "hermes_cli.routing_auto_update.detect_routing_update_topology",
        return_value={"matches": True, "repo_role": "dev"},
    )
    @patch("hermes_cli.runtime_layout.bootstrap_split_runtime")
    def test_update_redirects_to_routing_updater_by_default(
        self, _mock_bootstrap, _mock_topology, _mock_managed, mock_run_update, _mock_render, capsys
    ):
        mock_run_update.return_value = SimpleNamespace(status="updated", message="ok")

        cmd_update(SimpleNamespace(gateway=False, legacy_stock_update=False))

        mock_run_update.assert_called_once()
        captured = capsys.readouterr()
        assert "Routing-aware fork topology detected" in captured.out

    @patch("hermes_cli.routing_auto_update._render_markdown_report", return_value="report")
    @patch("hermes_cli.runtime_layout.get_profile_home", return_value=Path("/tmp/hermes/profiles/dev"))
    @patch("hermes_cli.runtime_layout.get_admin_root", return_value=Path("/tmp/hermes"))
    @patch("hermes_cli.runtime_layout.bootstrap_split_runtime")
    @patch("subprocess.run")
    @patch("hermes_cli.config.is_managed", return_value=False)
    @patch(
        "hermes_cli.routing_auto_update.detect_routing_update_topology",
        return_value={"matches": True, "repo_role": "live", "dev_repo_root": "/tmp/hermes/hermes-agent-dev"},
    )
    @patch("pathlib.Path.exists", return_value=True)
    def test_update_delegates_live_repo_to_dev_checkout(
        self,
        _mock_exists,
        _mock_topology,
        _mock_managed,
        mock_run,
        _mock_bootstrap,
        _mock_admin_root,
        _mock_profile_home,
        _mock_render,
        capsys,
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            ["python"],
            0,
            stdout=json.dumps(
                {
                    "status": "updated",
                    "started_at": "2026-04-13T01:00:00+00:00",
                    "finished_at": "2026-04-13T01:00:01+00:00",
                    "repo_root": "/tmp/hermes/hermes-agent-dev",
                }
            ),
            stderr="",
        )

        cmd_update(SimpleNamespace(gateway=False, legacy_stock_update=False))

        delegated = mock_run.call_args.args[0]
        assert delegated[:5] == [
            "/tmp/hermes/hermes-agent-dev/venv/bin/python",
            "-m",
            "hermes_cli.main",
            "-p",
            "dev",
        ]
        assert delegated[5:9] == ["routing", "update", "run", "--json"]
        captured = capsys.readouterr()
        assert "report" in captured.out

    @patch("shutil.which", return_value=None)
    @patch("hermes_cli.routing_auto_update.detect_routing_update_topology", return_value={"matches": False})
    @patch("subprocess.run")
    def test_update_falls_back_to_main_when_branch_not_on_remote(
        self, mock_run, _mock_topology, _mock_which, mock_args, capsys
    ):
        mock_run.side_effect = _make_run_side_effect(
            branch="fix/stoicneko", verify_ok=False, commit_count="3"
        )

        cmd_update(mock_args)

        commands = [" ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list]

        # rev-list should use origin/main, not origin/fix/stoicneko
        rev_list_cmds = [c for c in commands if "rev-list" in c]
        assert len(rev_list_cmds) == 1
        assert "origin/main" in rev_list_cmds[0]
        assert "origin/fix/stoicneko" not in rev_list_cmds[0]

        # pull should use main, not fix/stoicneko
        pull_cmds = [c for c in commands if "pull" in c]
        assert len(pull_cmds) == 1
        assert "main" in pull_cmds[0]

    @patch("shutil.which", return_value=None)
    @patch("hermes_cli.routing_auto_update.detect_routing_update_topology", return_value={"matches": False})
    @patch("subprocess.run")
    def test_update_uses_current_branch_when_on_remote(
        self, mock_run, _mock_topology, _mock_which, mock_args, capsys
    ):
        mock_run.side_effect = _make_run_side_effect(
            branch="main", verify_ok=True, commit_count="2"
        )

        cmd_update(mock_args)

        commands = [" ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list]

        rev_list_cmds = [c for c in commands if "rev-list" in c]
        assert len(rev_list_cmds) == 1
        assert "origin/main" in rev_list_cmds[0]

        pull_cmds = [c for c in commands if "pull" in c]
        assert len(pull_cmds) == 1
        assert "main" in pull_cmds[0]

    @patch("shutil.which", return_value=None)
    @patch("hermes_cli.routing_auto_update.detect_routing_update_topology", return_value={"matches": False})
    @patch("subprocess.run")
    def test_update_already_up_to_date(
        self, mock_run, _mock_topology, _mock_which, mock_args, capsys
    ):
        mock_run.side_effect = _make_run_side_effect(
            branch="main", verify_ok=True, commit_count="0"
        )

        cmd_update(mock_args)

        captured = capsys.readouterr()
        assert "Already up to date!" in captured.out

        # Should NOT have called pull
        commands = [" ".join(str(a) for a in c.args[0]) for c in mock_run.call_args_list]
        pull_cmds = [c for c in commands if "pull" in c]
        assert len(pull_cmds) == 0

    def test_update_non_interactive_skips_migration_prompt(self, mock_args, capsys):
        """When stdin/stdout aren't TTYs, config migration prompt is skipped."""
        with patch("shutil.which", return_value=None), patch(
            "subprocess.run"
        ) as mock_run, patch("builtins.input") as mock_input, patch(
            "hermes_cli.routing_auto_update.detect_routing_update_topology", return_value={"matches": False}
        ), patch(
            "hermes_cli.config.get_missing_env_vars", return_value=["MISSING_KEY"]
        ), patch("hermes_cli.config.get_missing_config_fields", return_value=[]), patch(
            "hermes_cli.config.check_config_version", return_value=(1, 2)
        ), patch("hermes_cli.main.sys") as mock_sys:
            mock_sys.stdin.isatty.return_value = False
            mock_sys.stdout.isatty.return_value = False
            mock_run.side_effect = _make_run_side_effect(
                branch="main", verify_ok=True, commit_count="1"
            )

            cmd_update(mock_args)

            mock_input.assert_not_called()
            captured = capsys.readouterr()
            assert "Non-interactive session" in captured.out
