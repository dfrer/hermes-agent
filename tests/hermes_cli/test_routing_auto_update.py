from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from cron.jobs import create_job, list_jobs
from hermes_cli import routing_auto_update as rau
from hermes_cli.config import load_config


@pytest.fixture()
def tmp_hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("cron.jobs.CRON_DIR", home / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", home / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", home / "cron" / "output")
    return home


def _ok_completed(args=None):
    return subprocess.CompletedProcess(args or [], 0, stdout="", stderr="")


def test_to_runtime_posix_path_translates_wsl_unc():
    unc = r"\\wsl.localhost\Ubuntu\home\hunter\.hermes\hermes-agent"
    assert rau._to_runtime_posix_path(unc) == "/home/hunter/.hermes/hermes-agent"


def test_git_push_disables_terminal_prompts(tmp_path, monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        return _ok_completed(args)

    monkeypatch.delenv("GIT_TERMINAL_PROMPT", raising=False)
    monkeypatch.setattr(rau, "_run_subprocess", fake_run)

    rau._git(tmp_path, "push", rau.PUSH_REMOTE, f"{rau.LIVE_BRANCH}:{rau.LIVE_BRANCH}", check=False)

    assert captured["args"] == [
        "git",
        "-C",
        str(tmp_path),
        "push",
        rau.PUSH_REMOTE,
        f"{rau.LIVE_BRANCH}:{rau.LIVE_BRANCH}",
    ]
    assert captured["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_install_routing_auto_update_sets_timezone_and_job(tmp_path, tmp_hermes_home, monkeypatch):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    schedule = {"kind": "cron", "expr": rau.ROUTING_AUTO_UPDATE_SCHEDULE, "display": rau.ROUTING_AUTO_UPDATE_SCHEDULE}

    monkeypatch.setattr(rau, "_ensure_safe_directory", lambda path: None)
    monkeypatch.setattr(rau, "_git_output", lambda repo, *args, cwd=None: "https://github.com/dfrer/hermes-agent.git")
    monkeypatch.setattr(rau, "_current_gateway_health", lambda: (False, False, False))
    monkeypatch.setattr(rau, "parse_schedule", lambda value: schedule)
    monkeypatch.setattr(rau, "compute_next_run", lambda value: "2099-01-01T22:00:00+00:00")
    monkeypatch.setattr(
        rau,
        "create_job",
        lambda **kwargs: create_job(
            prompt=kwargs["prompt"],
            schedule="every 1h",
            name=kwargs["name"],
            deliver=kwargs["deliver"],
            skills=kwargs["skills"],
        ),
    )

    result = rau.install_routing_auto_update(repo_root)

    config = load_config()
    jobs = list_jobs(include_disabled=True)

    assert result.status == "ok"
    assert config["timezone"] == rau.ROUTING_AUTO_UPDATE_TIMEZONE
    assert len(jobs) == 1
    assert jobs[0]["name"] == rau.ROUTING_AUTO_UPDATE_JOB_NAME
    assert jobs[0]["deliver"] == rau.ROUTING_AUTO_UPDATE_DELIVERY
    assert jobs[0]["skills"] == ["routing-layer"]
    assert "python -m hermes_cli.routing_auto_update run --repo-root" in jobs[0]["prompt"]
    assert "[SILENT]" in jobs[0]["prompt"]


def test_install_routing_auto_update_pauses_duplicate_jobs(tmp_path, tmp_hermes_home, monkeypatch):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    schedule = {"kind": "cron", "expr": rau.ROUTING_AUTO_UPDATE_SCHEDULE, "display": rau.ROUTING_AUTO_UPDATE_SCHEDULE}

    first = create_job(
        prompt="old prompt",
        schedule="every 1h",
        name=rau.ROUTING_AUTO_UPDATE_JOB_NAME,
        deliver="local",
        skills=["routing-layer"],
    )
    second = create_job(
        prompt="older prompt",
        schedule="every 1h",
        name=rau.ROUTING_AUTO_UPDATE_JOB_NAME,
        deliver="local",
        skills=["routing-layer"],
    )

    monkeypatch.setattr(rau, "_ensure_safe_directory", lambda path: None)
    monkeypatch.setattr(rau, "_git_output", lambda repo, *args, cwd=None: "https://github.com/dfrer/hermes-agent.git")
    monkeypatch.setattr(rau, "_current_gateway_health", lambda: (False, False, False))
    monkeypatch.setattr(rau, "parse_schedule", lambda value: schedule)
    monkeypatch.setattr(rau, "compute_next_run", lambda value: "2099-01-01T22:00:00+00:00")

    result = rau.install_routing_auto_update(repo_root)
    jobs = {job["id"]: job for job in list_jobs(include_disabled=True)}

    assert result.duplicates_paused == [second["id"]]
    assert jobs[first["id"]]["state"] == "scheduled"
    assert jobs[second["id"]]["state"] == "paused"


def test_run_routing_auto_update_noop_writes_report(tmp_path, tmp_hermes_home, monkeypatch):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    report_root = tmp_hermes_home / "cron" / "output" / rau.REPORT_DIR_NAME

    outputs = {
        ("branch", "--show-current"): rau.LIVE_BRANCH,
        ("status", "--porcelain"): "",
        ("rev-parse", "HEAD"): "abc123",
        ("rev-parse", rau.UPSTREAM_REF): "abc123",
        ("rev-parse", rau.PUSH_REF): "abc123",
    }

    monkeypatch.setattr(rau, "_ensure_safe_directory", lambda path: None)
    monkeypatch.setattr(rau, "_ensure_fork_remote", lambda repo_root: rau.EXPECTED_FORK_URL)
    monkeypatch.setattr(rau, "_current_gateway_health", lambda: (False, False, False))
    monkeypatch.setattr(rau, "_git_output", lambda repo, *args, cwd=None: outputs[tuple(args)])
    monkeypatch.setattr(rau, "_git", lambda repo, *args, cwd=None, check=True: _ok_completed(args))
    monkeypatch.setattr(rau, "_git_branch_exists", lambda repo, ref: True)
    monkeypatch.setattr(rau, "_git_is_ancestor", lambda repo, ancestor, descendant: True)
    monkeypatch.setattr(rau, "_prune_retention", lambda *args, **kwargs: None)

    report = rau.run_routing_auto_update(repo_root, report_root)

    latest = json.loads((report_root / "latest.json").read_text(encoding="utf-8"))
    assert report.status == "noop"
    assert latest["status"] == "noop"
    assert (report_root / "latest.md").exists()


def test_ahead_behind_parses_git_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(
        rau,
        "_git",
        lambda repo, *args, cwd=None, check=True: subprocess.CompletedProcess(args, 0, stdout="4\t31\n", stderr=""),
    )

    assert rau._ahead_behind(tmp_path, rau.UPSTREAM_REF, "HEAD") == (4, 31)


def test_verification_suite_runs_smoke_after_targeted_tests(tmp_path, monkeypatch):
    calls = []

    def fake_pytest(repo_root, tests):
        calls.append(("pytest", tuple(tests)))
        return "pytest " + tests[0]

    def fake_smoke(repo_root):
        calls.append(("smoke", ()))
        return "python -m py_compile"

    monkeypatch.setattr(rau, "_run_pytest", fake_pytest)
    monkeypatch.setattr(rau, "_run_smoke_checks", fake_smoke)

    executed = rau._run_verification_suite(tmp_path)

    assert calls == [
        ("pytest", rau.ROUTING_CONTRACT_TESTS),
        ("pytest", rau.TARGETED_REGRESSION_TESTS),
        ("smoke", ()),
    ]
    assert executed[-1] == "python -m py_compile"


def test_routing_update_status_reads_latest_report(tmp_path):
    report_root = tmp_path / "reports"
    report_root.mkdir()
    (report_root / "latest.json").write_text(
        json.dumps({
            "status": "merge_conflict",
            "finished_at": "2026-04-09T21:00:00+00:00",
            "message": "conflict in routing guard",
            "upstream_behind_count": 2,
            "upstream_ahead_count": 31,
            "fork_behind_count": 0,
            "fork_ahead_count": 1,
            "retained_failed_worktree": "/tmp/hermes-update",
            "gateway_running": True,
            "telegram_connected": False,
        }),
        encoding="utf-8",
    )

    status = rau.routing_update_status(report_root)

    assert status["status"] == "merge_conflict"
    assert status["last_error"] == "conflict in routing guard"
    assert status["branch_drift"]["upstream_behind"] == 2
    assert status["branch_drift"]["upstream_ahead"] == 31
    assert status["retained_worktree"] == "/tmp/hermes-update"
    assert status["gateway_running"] is True
    assert status["telegram_connected"] is False


def test_run_routing_auto_update_dirty_worktree_short_circuits(tmp_path, tmp_hermes_home, monkeypatch):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    outputs = {
        ("branch", "--show-current"): rau.LIVE_BRANCH,
        ("status", "--porcelain"): " M hermes_cli/routing_guard.py",
    }

    monkeypatch.setattr(rau, "_ensure_safe_directory", lambda path: None)
    monkeypatch.setattr(rau, "_ensure_fork_remote", lambda repo_root: rau.EXPECTED_FORK_URL)
    monkeypatch.setattr(rau, "_current_gateway_health", lambda: (False, False, False))
    monkeypatch.setattr(rau, "_git_output", lambda repo, *args, cwd=None: outputs[tuple(args)])
    monkeypatch.setattr(rau, "_prune_retention", lambda *args, **kwargs: None)
    monkeypatch.setattr(rau, "_sync_policy_history", lambda *args, **kwargs: pytest.fail("sync should not run"))
    monkeypatch.setattr(rau, "_export_routing_backup", lambda *args, **kwargs: pytest.fail("backup should not run"))

    report = rau.run_routing_auto_update(repo_root, tmp_hermes_home / "reports")

    assert report.status == "dirty_worktree"
    assert "dirty" in report.message.lower()
