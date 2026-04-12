from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from cron.jobs import create_job, list_jobs
from hermes_cli import routing_auto_update as rau
from hermes_cli.config import load_config
from hermes_cli.routing_update_git import GitBackend, GitBackendProbe, _linux_to_windows_path


@pytest.fixture()
def tmp_hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("cron.jobs.CRON_DIR", home / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", home / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", home / "cron" / "output")
    return home


def _ok_completed(args=None, stdout="", stderr=""):
    return subprocess.CompletedProcess(args or [], 0, stdout=stdout, stderr=stderr)


def _probe(*, backend="native", fetch_ready=True, push_ready=True, errors=None, details=None):
    return GitBackendProbe(
        backend=backend,
        fetch_ready=fetch_ready,
        push_ready=push_ready,
        push_targets={rau.LIVE_BRANCH: push_ready, rau.PROMOTION_BRANCH: push_ready},
        errors=errors or {},
        details=details or {},
    )


def test_to_runtime_posix_path_translates_wsl_unc():
    unc = r"\\wsl.localhost\Ubuntu\home\hunter\.hermes\hermes-agent"
    assert rau._to_runtime_posix_path(unc) == "/home/hunter/.hermes/hermes-agent"


def test_linux_to_windows_path_translates_wsl_mount(monkeypatch):
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    converted = _linux_to_windows_path("/home/hunter/.hermes/hermes-agent")
    assert converted.startswith(r"\\wsl.localhost\Ubuntu\home\hunter\.hermes\hermes-agent")


def test_build_trust_gate_pytest_cmd_skips_xdist_when_unavailable(monkeypatch):
    monkeypatch.setattr(rau, "_trust_gate_supports_xdist", lambda: False)

    cmd = rau._build_trust_gate_pytest_cmd()

    assert cmd[:5] == [rau.sys.executable, "-m", "pytest", "-o", "addopts="]
    assert "-n" not in cmd


def test_run_trust_gate_uses_addopts_override_and_optional_xdist(monkeypatch, tmp_path):
    executed = []

    monkeypatch.setattr(rau, "_trust_gate_supports_xdist", lambda: True)
    monkeypatch.setattr(rau, "_resolve_powershell_command", lambda path: ["pwsh", "-File", str(path)])
    monkeypatch.setattr(rau, "_run_subprocess", lambda cmd, cwd=None: executed.append((cmd, cwd)) or _ok_completed(cmd))

    result = rau._run_trust_gate(tmp_path)

    assert executed[0][0][:5] == [rau.sys.executable, "-m", "pytest", "-o", "addopts="]
    assert executed[0][0][-2:] == ["-n", "auto"]
    assert executed[1][0][0] == "pwsh"
    assert result[0].startswith(f"{rau.sys.executable} -m pytest -o addopts=")


def test_install_routing_auto_update_sets_timezone_and_job(tmp_path, tmp_hermes_home, monkeypatch):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    schedule = {"kind": "cron", "expr": rau.ROUTING_AUTO_UPDATE_SCHEDULE, "display": rau.ROUTING_AUTO_UPDATE_SCHEDULE}

    monkeypatch.setattr(rau, "_ensure_safe_directory", lambda path: None)
    monkeypatch.setattr(rau, "_ensure_repo_merge_defaults", lambda path: None)
    monkeypatch.setattr(rau, "_ensure_fork_remote", lambda repo_root: rau.EXPECTED_FORK_URL)
    monkeypatch.setattr(rau, "_current_gateway_health", lambda: (False, False, False))
    monkeypatch.setattr(rau, "parse_schedule", lambda value: schedule)
    monkeypatch.setattr(rau, "compute_next_run", lambda value: "2099-01-01T22:00:00+00:00")
    monkeypatch.setattr(
        rau,
        "create_job",
        lambda **kwargs: create_job(
            prompt=kwargs["prompt"],
            schedule="every 4h",
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
    assert "hermes routing update run --json" in jobs[0]["prompt"]
    assert "hermes routing update finalize --json" in jobs[0]["prompt"]
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
    monkeypatch.setattr(rau, "_ensure_repo_merge_defaults", lambda path: None)
    monkeypatch.setattr(rau, "_ensure_fork_remote", lambda repo_root: rau.EXPECTED_FORK_URL)
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

    def fake_run_state_machine(repo_root_arg, report_root_arg, report, finalize_from_retained=False):
        report.status = "noop"
        report.message = "No upstream changes to apply and fork promotion is already in sync."
        report.pre_update_head = "abc123"
        report.post_update_head = "abc123"
        report.push_status = "not_needed"
        return report

    monkeypatch.setattr(rau, "_run_state_machine", fake_run_state_machine)
    monkeypatch.setattr(rau, "_current_gateway_health", lambda: (False, False, False))
    monkeypatch.setattr(rau, "_prune_retention", lambda *args, **kwargs: None)

    report = rau.run_routing_auto_update(repo_root, report_root)

    latest = json.loads((report_root / "latest.json").read_text(encoding="utf-8"))
    assert report.status == "noop"
    assert latest["status"] == "noop"
    assert (report_root / "latest.md").exists()


def test_run_state_machine_aborts_dirty_worktree(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    report_root = tmp_path / "reports"
    report = rau.UpdateReport(status="setup_error", started_at=rau._iso(rau._utc_now()), finished_at="")

    outputs = {
        ("branch", "--show-current"): rau.LIVE_BRANCH,
        ("status", "--porcelain"): " M hermes_cli/routing_auto_update.py",
    }

    monkeypatch.setattr(rau, "_ensure_safe_directory", lambda path: None)
    monkeypatch.setattr(rau, "detect_routing_update_topology", lambda repo_root=None: {"matches": True, "current_branch": rau.LIVE_BRANCH})
    monkeypatch.setattr(rau, "_ensure_fork_remote", lambda repo_root: rau.EXPECTED_FORK_URL)
    monkeypatch.setattr(rau, "_git_output", lambda repo, *args, cwd=None: outputs[tuple(args)])

    result = rau._run_state_machine(repo_root, report_root, report)

    assert result.status == "dirty_worktree"
    assert "dirty" in result.message.lower()


def test_run_state_machine_recovers_pending_promotion(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    report_root = tmp_path / "reports"
    report = rau.UpdateReport(status="setup_error", started_at=rau._iso(rau._utc_now()), finished_at="")

    monkeypatch.setattr(rau, "_ensure_safe_directory", lambda path: None)
    monkeypatch.setattr(rau, "detect_routing_update_topology", lambda repo_root=None: {"matches": True, "current_branch": rau.LIVE_BRANCH})
    monkeypatch.setattr(rau, "_ensure_fork_remote", lambda repo_root: rau.EXPECTED_FORK_URL)
    monkeypatch.setattr(rau, "_git_output", lambda repo, *args, cwd=None: {("branch", "--show-current"): rau.LIVE_BRANCH, ("status", "--porcelain"): ""}[tuple(args)])
    monkeypatch.setattr(rau, "_merge_readiness_issues", lambda repo_root: [])
    monkeypatch.setattr(rau, "select_git_backend", lambda *args, **kwargs: (GitBackend("native", "git", repo_root), _probe()))
    monkeypatch.setattr(rau, "_refresh_remote_refs", lambda *args, **kwargs: None)
    monkeypatch.setattr(rau, "_current_ref", lambda repo_root, ref: {
        "HEAD": "abc123",
        rau.UPSTREAM_REF: "abc123",
        rau.PUSH_REF: "old-integration",
        rau.MAIN_REF: "old-main",
    }.get(ref, ""))
    monkeypatch.setattr(
        rau,
        "_compute_live_drift",
        lambda repo_root: {
            "current_head": "abc123",
            "upstream_head": "abc123",
            "integration_head": "old-integration",
            "main_head": "old-main",
            "upstream": {"behind": 0, "ahead": 0},
            "integration": {"behind": 0, "ahead": 1},
            "main": {"behind": 0, "ahead": 1},
        },
    )
    monkeypatch.setattr(
        rau,
        "_git_is_ancestor",
        lambda repo_root, ancestor, descendant: True if (ancestor, descendant) == (rau.UPSTREAM_REF, "HEAD") else False,
    )
    monkeypatch.setattr(
        rau,
        "_push_targets",
        lambda repo_root, backend, report, target_head, **kwargs: (
            setattr(report, "integration_push_status", "ok"),
            setattr(report, "main_promotion_status", "ok"),
            setattr(report, "push_status", "ok"),
            setattr(report, "promoted_head", target_head),
        ),
    )

    result = rau._run_state_machine(repo_root, report_root, report)

    assert result.status == "updated"
    assert result.promoted_head == "abc123"
    assert "Recovered pending fork promotion" in result.message


def test_run_state_machine_reports_main_promotion_failure(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    report_root = tmp_path / "reports"
    report = rau.UpdateReport(status="setup_error", started_at=rau._iso(rau._utc_now()), finished_at="")

    monkeypatch.setattr(rau, "_ensure_safe_directory", lambda path: None)
    monkeypatch.setattr(rau, "detect_routing_update_topology", lambda repo_root=None: {"matches": True, "current_branch": rau.LIVE_BRANCH})
    monkeypatch.setattr(rau, "_ensure_fork_remote", lambda repo_root: rau.EXPECTED_FORK_URL)
    monkeypatch.setattr(rau, "_git_output", lambda repo, *args, cwd=None: {("branch", "--show-current"): rau.LIVE_BRANCH, ("status", "--porcelain"): ""}[tuple(args)])
    monkeypatch.setattr(rau, "_merge_readiness_issues", lambda repo_root: [])
    monkeypatch.setattr(rau, "select_git_backend", lambda *args, **kwargs: (GitBackend("native", "git", repo_root), _probe()))
    monkeypatch.setattr(rau, "_refresh_remote_refs", lambda *args, **kwargs: None)
    monkeypatch.setattr(rau, "_current_ref", lambda repo_root, ref: {
        "HEAD": "abc123",
        rau.UPSTREAM_REF: "abc123",
        rau.PUSH_REF: "old-integration",
        rau.MAIN_REF: "old-main",
    }.get(ref, ""))
    monkeypatch.setattr(
        rau,
        "_compute_live_drift",
        lambda repo_root: {
            "current_head": "abc123",
            "upstream_head": "abc123",
            "integration_head": "old-integration",
            "main_head": "old-main",
            "upstream": {"behind": 0, "ahead": 0},
            "integration": {"behind": 0, "ahead": 1},
            "main": {"behind": 0, "ahead": 1},
        },
    )
    monkeypatch.setattr(
        rau,
        "_git_is_ancestor",
        lambda repo_root, ancestor, descendant: True if (ancestor, descendant) == (rau.UPSTREAM_REF, "HEAD") else False,
    )

    def fake_push_targets(repo_root_arg, backend, report_arg, target_head, **kwargs):
        report_arg.integration_push_status = "ok"
        report_arg.main_promotion_status = "failed"
        report_arg.push_status = "ok"
        report_arg.message = "Updated integration, but promoting fork/main failed: denied"

    monkeypatch.setattr(rau, "_push_targets", fake_push_targets)

    result = rau._run_state_machine(repo_root, report_root, report)

    assert result.status == "push_failed"
    assert result.integration_push_status == "ok"
    assert result.main_promotion_status == "failed"


def test_run_state_machine_realigns_to_promoted_main_and_repairs_integration_branch(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    report_root = tmp_path / "reports"
    report = rau.UpdateReport(status="setup_error", started_at=rau._iso(rau._utc_now()), finished_at="")
    pushed = {}

    monkeypatch.setattr(rau, "_ensure_safe_directory", lambda path: None)
    monkeypatch.setattr(rau, "detect_routing_update_topology", lambda repo_root=None: {"matches": True, "current_branch": rau.LIVE_BRANCH})
    monkeypatch.setattr(rau, "_ensure_fork_remote", lambda repo_root: rau.EXPECTED_FORK_URL)
    monkeypatch.setattr(rau, "_git_output", lambda repo, *args, cwd=None: {("branch", "--show-current"): rau.LIVE_BRANCH, ("status", "--porcelain"): ""}[tuple(args)])
    monkeypatch.setattr(rau, "_merge_readiness_issues", lambda repo_root: [])
    monkeypatch.setattr(
        rau,
        "select_git_backend",
        lambda *args, **kwargs: (
            GitBackend("windows-bridge", "/mnt/c/Program Files/Git/cmd/git.exe", repo_root),
            _probe(backend="windows-bridge", fetch_ready=True, push_ready=False, errors={"push:main": "non-fast-forward"}, details={"windows-bridge": {"available": True}}),
        ),
    )
    monkeypatch.setattr(rau, "_refresh_remote_refs", lambda *args, **kwargs: None)

    heads = {"HEAD": "abc123"}

    def fake_current_ref(repo_root_arg, ref):
        mapping = {
            "HEAD": heads["HEAD"],
            rau.UPSTREAM_REF: "abc123",
            rau.PUSH_REF: "",
            rau.MAIN_REF: "merge-main",
        }
        return mapping.get(ref, "")

    monkeypatch.setattr(rau, "_current_ref", fake_current_ref)

    drift_calls = {"count": 0}

    def fake_compute_live_drift(repo_root_arg):
        drift_calls["count"] += 1
        if drift_calls["count"] == 1:
            return {
                "current_head": "abc123",
                "upstream_head": "abc123",
                "integration_head": "",
                "main_head": "merge-main",
                "upstream": {"behind": 0, "ahead": 0},
                "integration": {"behind": 0, "ahead": 0},
                "main": {"behind": 0, "ahead": 1},
            }
        return {
            "current_head": "merge-main",
            "upstream_head": "abc123",
            "integration_head": "",
            "main_head": "merge-main",
            "upstream": {"behind": 0, "ahead": 1},
            "integration": {"behind": 0, "ahead": 0},
            "main": {"behind": 0, "ahead": 0},
        }

    monkeypatch.setattr(rau, "_compute_live_drift", fake_compute_live_drift)

    def fake_is_ancestor(repo_root_arg, ancestor, descendant):
        if ancestor == rau.UPSTREAM_REF and descendant == "HEAD":
            return True
        if ancestor == "abc123" and descendant == rau.MAIN_REF:
            return True
        return False

    monkeypatch.setattr(rau, "_git_is_ancestor", fake_is_ancestor)

    def fake_git(repo_root_arg, *args, cwd=None, check=True):
        if args == ("merge", "--ff-only", rau.MAIN_REF):
            heads["HEAD"] = "merge-main"
        return _ok_completed(args)

    monkeypatch.setattr(rau, "_git", fake_git)

    def fake_push_targets(repo_root_arg, backend, report_arg, target_head, **kwargs):
        pushed.update(kwargs)
        report_arg.integration_push_status = "ok"
        report_arg.main_promotion_status = "not_needed"
        report_arg.push_status = "ok"
        report_arg.promoted_head = target_head

    monkeypatch.setattr(rau, "_push_targets", fake_push_targets)

    result = rau._run_state_machine(repo_root, report_root, report)

    assert result.status == "updated"
    assert pushed == {"push_integration": True, "push_main": False}
    assert "Fast-forwarded" in result.message
    assert "integration branch" in result.message


def test_run_state_machine_merge_conflict_writes_repair_manifest(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    report_root = tmp_path / "reports"
    update_worktree = tmp_path / "repo-update"
    report = rau.UpdateReport(status="setup_error", started_at=rau._iso(rau._utc_now()), finished_at="")

    monkeypatch.setattr(rau, "_ensure_safe_directory", lambda path: None)
    monkeypatch.setattr(rau, "detect_routing_update_topology", lambda repo_root=None: {"matches": True, "current_branch": rau.LIVE_BRANCH})
    monkeypatch.setattr(rau, "_ensure_fork_remote", lambda repo_root: rau.EXPECTED_FORK_URL)
    monkeypatch.setattr(rau, "_git_output", lambda repo, *args, cwd=None: {("branch", "--show-current"): rau.LIVE_BRANCH, ("status", "--porcelain"): ""}.get(tuple(args), ""))
    monkeypatch.setattr(rau, "_merge_readiness_issues", lambda repo_root: [])
    monkeypatch.setattr(rau, "select_git_backend", lambda *args, **kwargs: (GitBackend("native", "git", repo_root), _probe()))
    monkeypatch.setattr(rau, "_refresh_remote_refs", lambda *args, **kwargs: None)
    monkeypatch.setattr(rau, "_current_ref", lambda repo_root, ref: {"HEAD": "abc123", rau.UPSTREAM_REF: "def456", rau.PUSH_REF: "abc123", rau.MAIN_REF: "abc123"}.get(ref, ""))
    monkeypatch.setattr(
        rau,
        "_compute_live_drift",
        lambda repo_root: {
            "current_head": "abc123",
            "upstream_head": "def456",
            "integration_head": "abc123",
            "main_head": "abc123",
            "upstream": {"behind": 1, "ahead": 0},
            "integration": {"behind": 0, "ahead": 0},
            "main": {"behind": 0, "ahead": 0},
        },
    )
    monkeypatch.setattr(rau, "_git_is_ancestor", lambda repo_root, ancestor, descendant: False)
    monkeypatch.setattr(rau, "_sync_policy_history", lambda hermes_home: {"status": "noop", "head": ""})
    monkeypatch.setattr(rau, "_export_routing_backup", lambda repo_root, hermes_home: {"backup_dir": str(tmp_path / "backup")})
    monkeypatch.setattr(rau, "_unique_worktree_path", lambda repo_root, stamp: update_worktree)

    def fake_git(repo_root_arg, *args, cwd=None, check=True):
        if args[:3] == ("worktree", "add", "-b"):
            update_worktree.mkdir(parents=True, exist_ok=True)
            return _ok_completed(args)
        if args[:3] == ("merge", "--no-ff", rau.UPSTREAM_REF):
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="conflict")
        return _ok_completed(args)

    monkeypatch.setattr(rau, "_git", fake_git)
    monkeypatch.setattr(rau, "_write_repair_manifest", lambda *args, **kwargs: (str(report_root / "manifest.json"), True, []))

    result = rau._run_state_machine(repo_root, report_root, report)

    assert result.status == "repair_required"
    assert result.retained_failed_worktree == str(update_worktree)
    assert result.repair_manifest_path.endswith("manifest.json")
    assert result.repair_eligible is True


def test_finalize_routing_auto_update_refuses_mismatched_retained_worktree(tmp_path, tmp_hermes_home, monkeypatch):
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    report_root = tmp_path / "reports"
    retained = tmp_path / "retained"
    retained.mkdir()

    monkeypatch.setattr(
        rau,
        "read_latest_update_report",
        lambda report_root=None: {
            "status": "verification_failed",
            "upstream_head": "def456",
            "retained_failed_worktree": str(retained),
            "update_branch": "codex/upstream-sync-1",
            "repo_root": str(repo_root),
        },
    )
    monkeypatch.setattr(rau, "_current_gateway_health", lambda: (False, False, False))
    monkeypatch.setattr(rau, "_prune_retention", lambda *args, **kwargs: None)
    monkeypatch.setattr(rau, "_ensure_safe_directory", lambda path: None)
    monkeypatch.setattr(rau, "detect_routing_update_topology", lambda repo_root=None: {"matches": True, "current_branch": rau.LIVE_BRANCH})
    monkeypatch.setattr(rau, "_ensure_fork_remote", lambda repo_root: rau.EXPECTED_FORK_URL)
    monkeypatch.setattr(rau, "_git_output", lambda repo, *args, cwd=None: {("branch", "--show-current"): rau.LIVE_BRANCH, ("status", "--porcelain"): ""}.get(tuple(args), "different-branch"))
    monkeypatch.setattr(rau, "_merge_readiness_issues", lambda repo_root: [])
    monkeypatch.setattr(rau, "select_git_backend", lambda *args, **kwargs: (GitBackend("native", "git", repo_root), _probe()))
    monkeypatch.setattr(rau, "_refresh_remote_refs", lambda *args, **kwargs: None)
    monkeypatch.setattr(rau, "_current_ref", lambda repo_root, ref: {"HEAD": "abc123", rau.UPSTREAM_REF: "def456", rau.PUSH_REF: "abc123", rau.MAIN_REF: "abc123"}.get(ref, ""))
    monkeypatch.setattr(
        rau,
        "_compute_live_drift",
        lambda repo_root: {
            "current_head": "abc123",
            "upstream_head": "def456",
            "integration_head": "abc123",
            "main_head": "abc123",
            "upstream": {"behind": 1, "ahead": 0},
            "integration": {"behind": 0, "ahead": 0},
            "main": {"behind": 0, "ahead": 0},
        },
    )

    report = rau.finalize_routing_auto_update(repo_root, report_root)

    assert report.status == "finalize_failed"
    assert "no longer matches" in report.message.lower()


def test_latest_retained_failure_accepts_stale_report_when_retained_head_contains_upstream(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    retained = tmp_path / "retained"
    retained.mkdir()

    monkeypatch.setattr(
        rau,
        "_git_is_ancestor",
        lambda repo_root_arg, ancestor, descendant: repo_root_arg == retained and ancestor == "new-upstream" and descendant == "HEAD",
    )

    latest = {
        "status": "verification_failed",
        "upstream_head": "old-upstream",
        "retained_failed_worktree": str(retained),
        "update_branch": "codex/upstream-sync-1",
    }

    result = rau._latest_retained_failure(repo_root, latest, "new-upstream")

    assert result is not None
    assert result["upstream_head"] == "new-upstream"
    assert result["retained_failed_worktree"] == str(retained)
    assert result["update_branch"] == "codex/upstream-sync-1"


def test_latest_retained_failure_recovers_from_worktree_list_when_latest_report_was_overwritten(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    retained = tmp_path / "retained"
    retained.mkdir()

    worktree_output = (
        f"worktree {repo_root}\n"
        f"HEAD abc123\n"
        f"branch refs/heads/{rau.LIVE_BRANCH}\n\n"
        f"worktree {retained}\n"
        f"HEAD def456\n"
        f"branch refs/heads/{rau.UPDATE_BRANCH_PREFIX}-123\n"
    )

    monkeypatch.setattr(
        rau,
        "_git_output",
        lambda repo_root_arg, *args, cwd=None: worktree_output if tuple(args) == ("worktree", "list", "--porcelain") else "",
    )
    monkeypatch.setattr(
        rau,
        "_git_is_ancestor",
        lambda repo_root_arg, ancestor, descendant: repo_root_arg == retained and ancestor == "new-upstream" and descendant == "HEAD",
    )

    latest = {
        "status": "finalize_failed",
        "upstream_head": "old-upstream",
        "retained_failed_worktree": "",
        "update_worktree": "",
    }

    result = rau._latest_retained_failure(repo_root, latest, "new-upstream")

    assert result is not None
    assert result["status"] == "verification_failed"
    assert result["upstream_head"] == "new-upstream"
    assert result["retained_failed_worktree"] == str(retained)
    assert result["update_branch"] == f"{rau.UPDATE_BRANCH_PREFIX}-123"


def test_routing_update_status_recomputes_live_drift(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    report_root = tmp_path / "reports"
    report_root.mkdir()
    (report_root / "latest.json").write_text(
        json.dumps(
            {
                "status": "push_failed",
                "message": "main promotion failed",
                "repo_root": str(repo_root),
                "retained_failed_worktree": str(tmp_path / "retained"),
                "finished_at": "2026-04-10T10:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(rau, "_current_gateway_health", lambda: (True, False, False))
    monkeypatch.setattr(rau, "detect_routing_update_topology", lambda repo_root=None: {"matches": True, "current_branch": rau.LIVE_BRANCH, "origin_remote": "origin", "fork_remote": "fork"})
    monkeypatch.setattr(rau, "select_git_backend", lambda *args, **kwargs: (None, _probe(backend="unavailable", fetch_ready=False, push_ready=False, errors={"push:main": "denied"})))
    monkeypatch.setattr(rau, "_git_is_ancestor", lambda repo_root, ancestor, descendant: False)
    monkeypatch.setattr(
        rau,
        "_compute_live_drift",
        lambda repo_root: {
            "current_head": "abc123",
            "upstream_head": "def456",
            "integration_head": "abc123",
            "main_head": "oldmain",
            "upstream": {"behind": 1, "ahead": 46},
            "integration": {"behind": 0, "ahead": 46},
            "main": {"behind": 1, "ahead": 45},
        },
    )

    summary = rau.routing_update_status(report_root, repo_root)

    assert summary["status"] == "push_failed"
    assert summary["branch_drift"]["upstream_behind"] == 1
    assert summary["branch_drift"]["main_behind"] == 1
    assert summary["auth"]["backend"] == "unavailable"
    assert summary["promotion_pending"] is True


def test_routing_update_status_only_refreshes_when_requested(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    refresh_calls = []

    monkeypatch.setattr(rau, "_current_gateway_health", lambda: (True, False, False))
    monkeypatch.setattr(
        rau,
        "detect_routing_update_topology",
        lambda repo_root=None: {"matches": True, "current_branch": rau.LIVE_BRANCH, "origin_remote": "origin", "fork_remote": "fork"},
    )
    monkeypatch.setattr(rau, "select_git_backend", lambda *args, **kwargs: (GitBackend("native", "git", repo_root), _probe()))
    monkeypatch.setattr(rau, "_refresh_remote_refs", lambda *args, **kwargs: refresh_calls.append(True))
    monkeypatch.setattr(rau, "_git_is_ancestor", lambda repo_root, ancestor, descendant: False)
    monkeypatch.setattr(
        rau,
        "_compute_live_drift",
        lambda repo_root: {
            "current_head": "abc123",
            "upstream_head": "def456",
            "integration_head": "abc123",
            "main_head": "oldmain",
            "upstream": {"behind": 1, "ahead": 46},
            "integration": {"behind": 0, "ahead": 46},
            "main": {"behind": 1, "ahead": 45},
        },
    )

    rau.routing_update_status(repo_root=repo_root)
    rau.routing_update_status(repo_root=repo_root, refresh_refs=True)

    assert refresh_calls == [True]


def test_routing_update_doctor_reports_degraded_when_auth_missing(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)

    monkeypatch.setattr(
        rau,
        "routing_update_status",
        lambda report_root=None, repo_root=None: {
            "repo_root": str(repo_root),
            "topology": {"matches": True, "current_branch": rau.LIVE_BRANCH},
            "auth": {"fetch_ready": False, "push_ready": False, "errors": {"push:main": "denied"}},
            "job": {"installed": True},
            "gateway_running": True,
            "telegram_connected": False,
            "retained_worktree": "",
        },
    )
    monkeypatch.setattr(rau, "_merge_readiness_issues", lambda repo_root: [])
    monkeypatch.setattr(rau, "_is_safe_directory_configured", lambda path: True)

    doctor = rau.routing_update_doctor(repo_root=repo_root)

    assert doctor["status"] == "degraded"
    assert any("push:main" in item for item in doctor["issues"])
