#!/usr/bin/env python3
"""
Deterministic Hermes auto-update orchestration for the routing integration branch.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cron.jobs import compute_next_run, create_job, list_jobs, parse_schedule, pause_job, update_job
from gateway.status import read_runtime_status
from hermes_cli.config import get_hermes_home, load_config, save_config
from hermes_cli.gateway import (
    find_gateway_pids,
    get_launchd_plist_path,
    get_systemd_unit_path,
    is_linux,
    is_macos,
)


LIVE_BRANCH = "codex/routing-integration"
UPSTREAM_REMOTE = "origin"
UPSTREAM_REF = "origin/main"
PUSH_REMOTE = "fork"
PUSH_REF = f"{PUSH_REMOTE}/{LIVE_BRANCH}"
EXPECTED_FORK_URL = "https://github.com/dfrer/hermes-agent.git"
ROUTING_AUTO_UPDATE_JOB_NAME = "routing auto update"
ROUTING_AUTO_UPDATE_SCHEDULE = "0 14 * * *"
ROUTING_AUTO_UPDATE_TIMEZONE = "America/Vancouver"
ROUTING_AUTO_UPDATE_DELIVERY = "telegram"
REPORT_DIR_NAME = "routing-auto-update"
REPORT_HISTORY_DIR = "history"
ROUTING_BACKUP_DIRNAME = "routing-backups"
DEFAULT_RETENTION_DAYS = 30
UPDATE_BRANCH_PREFIX = "codex/upstream-sync"

ROUTING_CONTRACT_TESTS: tuple[str, ...] = (
    "tests/agent/test_routing_guard.py",
    "tests/test_model_tools.py",
    "tests/agent/test_skill_commands.py",
    "tests/cli/test_cli_preloaded_skills.py",
    "tests/hermes_cli/test_api_key_providers.py",
    "tests/hermes_cli/test_auth_commands.py",
)

TARGETED_REGRESSION_TESTS: tuple[str, ...] = (
    "tests/hermes_cli/test_doctor.py",
    "tests/hermes_cli/test_runtime_provider_resolution.py",
    "tests/tools/test_terminal_none_command_guard.py",
)

CRON_PROMPT_TEMPLATE = """You are running the daily Hermes routing-preserving updater.

Do not implement Git update logic yourself. The deterministic script is authoritative.

1. Emit a routing decision for a deterministic repository-maintenance task.
2. Call `routed_exec` once to run:
   `python -m hermes_cli.routing_auto_update run --repo-root {repo_root}`
   Use workdir `{repo_root}`.
3. Read `{latest_json}` and `{latest_md}`.
4. If `latest.json` reports `status == "noop"`, return exactly `[SILENT]`.
5. Otherwise send a concise summary based on `latest.md`.

Do not perform manual merge, test, or push steps outside the script.
"""


class UpdateError(RuntimeError):
    """Known deterministic updater failure."""


@dataclass
class UpdateReport:
    status: str
    started_at: str
    finished_at: str
    schedule_time: str = "14:00"
    timezone: str = ROUTING_AUTO_UPDATE_TIMEZONE
    repo_root: str = ""
    live_branch: str = LIVE_BRANCH
    upstream_ref: str = UPSTREAM_REF
    push_remote: str = PUSH_REMOTE
    pre_update_head: str = ""
    upstream_head: str = ""
    post_update_head: str = ""
    update_branch: str = ""
    update_worktree: str = ""
    backup_dir: str = ""
    tests_run: list[str] = field(default_factory=list)
    push_status: str = "not_attempted"
    delivery_target: str = ROUTING_AUTO_UPDATE_DELIVERY
    message: str = ""
    policy_history_sync: list[dict[str, Any]] = field(default_factory=list)
    retained_failed_worktree: str = ""
    gateway_running: bool | None = None
    gateway_service_installed: bool | None = None
    telegram_connected: bool | None = None


@dataclass
class InstallResult:
    status: str
    repo_root: str
    timezone: str
    fork_remote: str
    gateway_running: bool
    gateway_service_installed: bool
    telegram_connected: bool
    job_id: str
    message: str
    duplicates_paused: list[str] = field(default_factory=list)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _normalize_remote_url(url: str) -> str:
    return (url or "").strip().rstrip("/").removesuffix(".git")


def _normalize_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _to_runtime_posix_path(path: str | Path) -> str:
    raw = str(path)
    if raw.startswith("\\\\wsl.localhost\\") or raw.startswith("\\\\wsl$\\"):
        match = re.match(r"^\\\\wsl(?:\.localhost|\$)\\[^\\]+\\(.+)$", raw, flags=re.IGNORECASE)
        if match:
            return "/" + match.group(1).replace("\\", "/")
    return raw.replace("\\", "/") if platform.system() == "Windows" else raw


def _json_dump(data: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2, sort_keys=False), encoding="utf-8")
    temp.replace(path)


def _text_dump(text: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def _default_report_root(hermes_home: Path) -> Path:
    return hermes_home / "cron" / "output" / REPORT_DIR_NAME


def _report_paths(report_root: Path, stamp: str) -> dict[str, Path]:
    history = report_root / REPORT_HISTORY_DIR
    return {
        "latest_json": report_root / "latest.json",
        "latest_md": report_root / "latest.md",
        "history_json": history / f"{stamp}.json",
        "history_md": history / f"{stamp}.md",
    }


def _render_markdown_report(report: UpdateReport) -> str:
    lines = [
        f"# Routing Auto Update: {report.status}",
        "",
        f"- Message: {report.message or '(none)'}",
        f"- Started: {report.started_at}",
        f"- Finished: {report.finished_at}",
        f"- Repo: `{report.repo_root}`",
        f"- Branch: `{report.live_branch}`",
        f"- Upstream: `{report.upstream_ref}` at `{report.upstream_head or 'n/a'}`",
        f"- Pre-update HEAD: `{report.pre_update_head or 'n/a'}`",
        f"- Post-update HEAD: `{report.post_update_head or report.pre_update_head or 'n/a'}`",
        f"- Push remote: `{report.push_remote}` ({report.push_status})",
        f"- Delivery target: `{report.delivery_target}`",
    ]
    if report.update_branch:
        lines.append(f"- Update branch: `{report.update_branch}`")
    if report.update_worktree:
        lines.append(f"- Update worktree: `{report.update_worktree}`")
    if report.backup_dir:
        lines.append(f"- Backup dir: `{report.backup_dir}`")
    if report.retained_failed_worktree:
        lines.append(f"- Retained failed worktree: `{report.retained_failed_worktree}`")
    if report.tests_run:
        lines.append("- Tests run:")
        lines.extend([f"  - `{item}`" for item in report.tests_run])
    if report.policy_history_sync:
        lines.append("- Policy history sync:")
        for item in report.policy_history_sync:
            lines.append(
                f"  - `{item.get('phase', 'unknown')}`: {item.get('status', 'unknown')} ({item.get('head', 'n/a')})"
            )
    if report.gateway_running is not None:
        lines.append(f"- Gateway running: `{report.gateway_running}`")
    if report.gateway_service_installed is not None:
        lines.append(f"- Gateway service installed: `{report.gateway_service_installed}`")
    if report.telegram_connected is not None:
        lines.append(f"- Telegram connected: `{report.telegram_connected}`")
    return "\n".join(lines) + "\n"


def _write_report_files(report_root: Path, report: UpdateReport) -> None:
    stamp = datetime.fromisoformat(report.finished_at).strftime("%Y%m%d-%H%M%S")
    paths = _report_paths(report_root, stamp)
    payload = asdict(report)
    markdown = _render_markdown_report(report)
    _json_dump(payload, paths["history_json"])
    _json_dump(payload, paths["latest_json"])
    _text_dump(markdown, paths["history_md"])
    _text_dump(markdown, paths["latest_md"])


def _run_subprocess(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture_output: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(args),
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=capture_output,
        env=env,
        check=False,
    )
    if check and result.returncode != 0:
        cmd = " ".join(args)
        stderr = (result.stderr or result.stdout or "").strip()
        raise UpdateError(f"Command failed ({result.returncode}): {cmd}\n{stderr}".strip())
    return result


def _git(
    repo_root: Path,
    *args: str,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    base = ["git"]
    if cwd is None:
        base.extend(["-C", str(repo_root)])
    env = None
    if args and args[0] == "push":
        env = os.environ.copy()
        env.setdefault("GIT_TERMINAL_PROMPT", "0")
    return _run_subprocess(base + list(args), cwd=cwd, check=check, env=env)


def _git_output(repo_root: Path, *args: str, cwd: Path | None = None) -> str:
    result = _git(repo_root, *args, cwd=cwd, check=True)
    return (result.stdout or "").strip()


def _ensure_safe_directory(path: Path) -> None:
    path = _normalize_path(path)
    current = _run_subprocess(["git", "config", "--global", "--get-all", "safe.directory"], check=False)
    lines = {(line or "").strip() for line in (current.stdout or "").splitlines() if line.strip()}
    if str(path) not in lines:
        _run_subprocess(["git", "config", "--global", "--add", "safe.directory", str(path)])


def _nearest_existing_parent(path: Path) -> Path:
    candidate = _normalize_path(path)
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            raise UpdateError(f"No existing parent found for path: {path}")
        candidate = parent
    return candidate


def _unique_worktree_path(repo_root: Path, stamp: str) -> Path:
    candidate = repo_root.parent / f"{repo_root.name}-update-{stamp}"
    index = 1
    while candidate.exists():
        candidate = repo_root.parent / f"{repo_root.name}-update-{stamp}-{index}"
        index += 1
    return candidate


def _clear_update_check(hermes_home: Path) -> None:
    try:
        (hermes_home / ".update_check").unlink(missing_ok=True)
    except OSError:
        pass


def _sync_policy_history(hermes_home: Path) -> dict[str, Any]:
    history_repo = hermes_home / "routing-policy-history"
    history_repo.mkdir(parents=True, exist_ok=True)
    skills_dir = history_repo / "skills" / "routing-layer"
    skills_dir.mkdir(parents=True, exist_ok=True)

    soul_path = hermes_home / "SOUL.md"
    skill_path = hermes_home / "skills" / "routing-layer" / "SKILL.md"
    if not soul_path.exists():
        raise UpdateError(f"Missing source file: {soul_path}")
    if not skill_path.exists():
        raise UpdateError(f"Missing source file: {skill_path}")

    if not (history_repo / ".git").exists():
        _run_subprocess(["git", "init", "--initial-branch=main", str(history_repo)])

    _ensure_safe_directory(history_repo)

    readme = """# Routing Policy History

This repo preserves the live routing policy files that live outside the `hermes-agent` repo:

- `SOUL.md`
- `skills/routing-layer/SKILL.md`
"""
    (history_repo / "README.md").write_text(readme, encoding="utf-8")
    shutil.copy2(soul_path, history_repo / "SOUL.md")
    shutil.copy2(skill_path, skills_dir / "SKILL.md")
    manifest = {
        "synced_at": _iso(_utc_now()),
        "hermes_home": str(hermes_home),
        "source_files": [str(soul_path), str(skill_path)],
    }
    _json_dump(manifest, history_repo / "manifest.json")

    _git(history_repo, "add", ".", cwd=history_repo)
    status = _git_output(history_repo, "status", "--short", cwd=history_repo)
    if not status:
        head = ""
        try:
            head = _git_output(history_repo, "rev-parse", "--short=8", "HEAD", cwd=history_repo)
        except UpdateError:
            head = ""
        return {
            "status": "noop",
            "history_repo": str(history_repo),
            "head": head,
            "message": "Routing policy history already up to date.",
        }

    message = f"Sync routing policy {_utc_now().astimezone().strftime('%Y-%m-%d %H:%M:%S %z')}"
    _git(history_repo, "commit", "-m", message, cwd=history_repo)
    head = _git_output(history_repo, "rev-parse", "--short=8", "HEAD", cwd=history_repo)
    return {
        "status": "updated",
        "history_repo": str(history_repo),
        "head": head,
        "message": message,
    }


def _export_routing_backup(repo_root: Path, hermes_home: Path, base_ref: str = UPSTREAM_REF) -> dict[str, Any]:
    backup_root = hermes_home / ROUTING_BACKUP_DIRNAME
    stamp = _utc_now().strftime("%Y%m%d-%H%M%S")
    dest = backup_root / stamp
    dest.mkdir(parents=True, exist_ok=True)

    branch = _git_output(repo_root, "branch", "--show-current")
    head = _git_output(repo_root, "rev-parse", "HEAD")
    short_head = _git_output(repo_root, "rev-parse", "--short=8", "HEAD")
    commits_raw = _git_output(repo_root, "rev-list", "--reverse", f"{base_ref}..HEAD")
    commit_list = [line for line in commits_raw.splitlines() if line.strip()] if commits_raw else []
    log_content = _git_output(repo_root, "log", "--oneline", f"{base_ref}..HEAD")
    patch_content = _git_output(repo_root, "format-patch", "--stdout", f"{base_ref}..HEAD")

    bundle_path = dest / "routing-integration.bundle"
    patch_path = dest / "routing-stack.patch"
    log_path = dest / "commits.txt"
    restore_path = dest / "RESTORE.md"
    manifest_path = dest / "manifest.json"

    _git(repo_root, "bundle", "create", str(bundle_path), "HEAD")
    patch_path.write_text(patch_content, encoding="utf-8")
    log_path.write_text(log_content, encoding="utf-8")

    soul_path = hermes_home / "SOUL.md"
    skill_path = hermes_home / "skills" / "routing-layer" / "SKILL.md"
    if soul_path.exists():
        shutil.copy2(soul_path, dest / "SOUL.md")
    if skill_path.exists():
        shutil.copy2(skill_path, dest / "routing-layer.SKILL.md")

    policy_history_repo = hermes_home / "routing-policy-history"
    policy_history_head = ""
    if (policy_history_repo / ".git").exists():
        _ensure_safe_directory(policy_history_repo)
        try:
            policy_history_head = _git_output(policy_history_repo, "rev-parse", "--short=8", "HEAD", cwd=policy_history_repo)
        except UpdateError:
            policy_history_head = ""

    restore_text = f"""# Routing Backup Restore

Bundle file:
- routing-integration.bundle

Patch file:
- routing-stack.patch

Restore options:

1. Restore the exact backed-up branch into a fresh clone:

   git clone <upstream hermes repo> hermes-agent-restored
   cd hermes-agent-restored
   git fetch "{bundle_path}" HEAD:codex/routing-restored
   git switch codex/routing-restored

2. Reapply only the routing delta on top of a newer upstream checkout:

   git am --3way < routing-stack.patch

Reference files copied with this backup:
- SOUL.md
- routing-layer.SKILL.md
"""
    restore_path.write_text(restore_text, encoding="utf-8")

    manifest = {
        "created_at": _iso(_utc_now()),
        "repo_root": str(repo_root),
        "branch": branch,
        "head": head,
        "short_head": short_head,
        "base_ref": base_ref,
        "commit_count": len(commit_list),
        "commits": commit_list,
        "policy_history_repo": str(policy_history_repo),
        "policy_history_head": policy_history_head,
        "files": [
            "routing-integration.bundle",
            "routing-stack.patch",
            "commits.txt",
            "RESTORE.md",
        ],
    }
    _json_dump(manifest, manifest_path)
    return {
        "backup_dir": str(dest),
        "manifest_path": str(manifest_path),
        "branch": branch,
        "head": head,
        "short_head": short_head,
        "base_ref": base_ref,
        "commit_count": len(commit_list),
        "policy_history_repo": str(policy_history_repo),
        "policy_history_head": policy_history_head,
    }


def _run_pytest(repo_root: Path, tests: Sequence[str]) -> str:
    if not tests:
        return ""
    cmd = [sys.executable, "-m", "pytest", "-o", "addopts="] + list(tests) + ["-q"]
    _run_subprocess(cmd, cwd=repo_root)
    return " ".join(cmd)


def _run_verification_suite(worktree: Path) -> list[str]:
    executed: list[str] = []
    executed.append(_run_pytest(worktree, ROUTING_CONTRACT_TESTS))
    executed.append(_run_pytest(worktree, TARGETED_REGRESSION_TESTS))
    return [item for item in executed if item]


def _git_branch_exists(repo_root: Path, ref: str) -> bool:
    result = _git(repo_root, "rev-parse", "--verify", "--quiet", ref, check=False)
    return result.returncode == 0


def _git_is_ancestor(repo_root: Path, ancestor: str, descendant: str) -> bool:
    result = _git(repo_root, "merge-base", "--is-ancestor", ancestor, descendant, check=False)
    return result.returncode == 0


def _remove_worktree_and_branch(repo_root: Path, worktree: Path | None, branch: str | None) -> None:
    if worktree and worktree.exists():
        _git(repo_root, "worktree", "remove", "--force", str(worktree), check=False)
    if branch and _git_branch_exists(repo_root, branch):
        _git(repo_root, "branch", "-D", branch, check=False)


def _cleanup_old_dirs(paths: Iterable[Path], *, keep: set[Path], older_than: timedelta) -> None:
    cutoff = _utc_now() - older_than
    for path in paths:
        if not path.exists() or path in keep:
            continue
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            continue


def _prune_retention(hermes_home: Path, repo_root: Path, *, keep_failed_worktree: Path | None = None) -> None:
    retention = timedelta(days=DEFAULT_RETENTION_DAYS)

    backup_root = hermes_home / ROUTING_BACKUP_DIRNAME
    backups = sorted([p for p in backup_root.iterdir()] if backup_root.exists() else [], key=lambda item: item.stat().st_mtime, reverse=True)
    keep_backups = {backups[0]} if backups else set()
    _cleanup_old_dirs(backups, keep=keep_backups, older_than=retention)

    report_root = _default_report_root(hermes_home)
    history_root = report_root / REPORT_HISTORY_DIR
    history_entries = sorted([p for p in history_root.iterdir()] if history_root.exists() else [], key=lambda item: item.stat().st_mtime, reverse=True)
    _cleanup_old_dirs(history_entries, keep=set(), older_than=retention)

    update_dirs = sorted([p for p in repo_root.parent.glob(f"{repo_root.name}-update-*") if p.is_dir()], key=lambda item: item.stat().st_mtime, reverse=True)
    keep_failed = {keep_failed_worktree} if keep_failed_worktree else set()
    if update_dirs and not keep_failed:
        keep_failed = {update_dirs[0]}
    _cleanup_old_dirs(update_dirs, keep=keep_failed, older_than=retention)


def _current_gateway_health() -> tuple[bool, bool, bool]:
    runtime = read_runtime_status() or {}
    service_installed = False
    if is_linux():
        service_installed = get_systemd_unit_path(system=False).exists() or get_systemd_unit_path(system=True).exists()
    elif is_macos():
        service_installed = get_launchd_plist_path().exists()
    gateway_running = bool(find_gateway_pids()) or runtime.get("gateway_state") == "running"
    telegram_state = ((runtime.get("platforms") or {}).get("telegram") or {}).get("state", "")
    telegram_connected = telegram_state == "connected"
    return gateway_running, service_installed, telegram_connected


def _build_cron_prompt(repo_root: Path, hermes_home: Path) -> str:
    runtime_repo_root = _to_runtime_posix_path(repo_root)
    latest_json = _to_runtime_posix_path(_default_report_root(hermes_home) / "latest.json")
    latest_md = _to_runtime_posix_path(_default_report_root(hermes_home) / "latest.md")
    return CRON_PROMPT_TEMPLATE.format(
        repo_root=runtime_repo_root,
        latest_json=latest_json,
        latest_md=latest_md,
    )


def _ensure_fork_remote(repo_root: Path, expected_url: str = EXPECTED_FORK_URL) -> str:
    actual = _git_output(repo_root, "remote", "get-url", PUSH_REMOTE)
    if _normalize_remote_url(actual) != _normalize_remote_url(expected_url):
        raise UpdateError(
            f"Remote '{PUSH_REMOTE}' points to '{actual}', expected '{expected_url}'."
        )
    return actual


def _upsert_cron_job(repo_root: Path, hermes_home: Path) -> tuple[str, list[str]]:
    schedule = parse_schedule(ROUTING_AUTO_UPDATE_SCHEDULE)
    prompt = _build_cron_prompt(repo_root, hermes_home)
    jobs = list_jobs(include_disabled=True)
    matching = [job for job in jobs if job.get("name") == ROUTING_AUTO_UPDATE_JOB_NAME]
    duplicates_paused: list[str] = []

    primary = matching[0] if matching else None
    for duplicate in matching[1:]:
        pause_job(duplicate["id"], reason="Superseded by deterministic routing auto update job")
        duplicates_paused.append(duplicate["id"])

    next_run = compute_next_run(schedule)
    updates = {
        "name": ROUTING_AUTO_UPDATE_JOB_NAME,
        "prompt": prompt,
        "schedule": schedule,
        "schedule_display": schedule.get("display", ROUTING_AUTO_UPDATE_SCHEDULE),
        "deliver": ROUTING_AUTO_UPDATE_DELIVERY,
        "skills": ["routing-layer"],
        "enabled": True,
        "state": "scheduled",
        "paused_at": None,
        "paused_reason": None,
        "next_run_at": next_run,
        "repeat": {"times": None, "completed": 0},
    }

    if primary:
        updated = update_job(primary["id"], updates)
        if not updated:
            raise UpdateError(f"Failed to update cron job '{primary['id']}'.")
        return updated["id"], duplicates_paused

    created = create_job(
        prompt=prompt,
        schedule=ROUTING_AUTO_UPDATE_SCHEDULE,
        name=ROUTING_AUTO_UPDATE_JOB_NAME,
        deliver=ROUTING_AUTO_UPDATE_DELIVERY,
        skills=["routing-layer"],
    )
    return created["id"], duplicates_paused


def install_routing_auto_update(repo_root: Path | None = None) -> InstallResult:
    repo_root = _normalize_path(repo_root or PROJECT_ROOT)
    hermes_home = _normalize_path(get_hermes_home())
    _ensure_safe_directory(repo_root)
    fork_remote = _ensure_fork_remote(repo_root)

    config = load_config()
    if config.get("timezone") != ROUTING_AUTO_UPDATE_TIMEZONE:
        config["timezone"] = ROUTING_AUTO_UPDATE_TIMEZONE
        save_config(config)

    job_id, duplicates_paused = _upsert_cron_job(repo_root, hermes_home)
    gateway_running, service_installed, telegram_connected = _current_gateway_health()

    if gateway_running and telegram_connected:
        message = "Cron job installed and gateway delivery path looks healthy."
    elif gateway_running:
        message = (
            "Cron job installed, but Telegram is not connected. Local reports will still be written; "
            "chat delivery is currently degraded."
        )
    else:
        message = (
            "Cron job installed, but the Hermes gateway is not running. Hermes cron will only fire when "
            "the gateway is active; missed daily runs follow Hermes catch-up/skip behavior."
        )

    return InstallResult(
        status="ok",
        repo_root=str(repo_root),
        timezone=ROUTING_AUTO_UPDATE_TIMEZONE,
        fork_remote=fork_remote,
        gateway_running=gateway_running,
        gateway_service_installed=service_installed,
        telegram_connected=telegram_connected,
        job_id=job_id,
        message=message,
        duplicates_paused=duplicates_paused,
    )


def run_routing_auto_update(repo_root: Path | None = None, report_root: Path | None = None) -> UpdateReport:
    started = _utc_now()
    repo_root = _normalize_path(repo_root or PROJECT_ROOT)
    hermes_home = _normalize_path(get_hermes_home())
    report_root = _normalize_path(report_root or _default_report_root(hermes_home))

    report = UpdateReport(
        status="setup_error",
        started_at=_iso(started),
        finished_at=_iso(started),
        repo_root=str(repo_root),
    )

    gateway_running, service_installed, telegram_connected = _current_gateway_health()
    report.gateway_running = gateway_running
    report.gateway_service_installed = service_installed
    report.telegram_connected = telegram_connected

    update_worktree: Path | None = None
    update_branch: str | None = None

    try:
        _ensure_safe_directory(repo_root)
        _ensure_fork_remote(repo_root)

        current_branch = _git_output(repo_root, "branch", "--show-current")
        if current_branch != LIVE_BRANCH:
            raise UpdateError(f"Live repo is on '{current_branch}', expected '{LIVE_BRANCH}'.")

        dirty = _git_output(repo_root, "status", "--porcelain")
        if dirty:
            report.status = "dirty_worktree"
            report.message = "Live worktree is dirty; auto-update aborted without mutating the branch."
            return report

        _git(repo_root, "fetch", UPSTREAM_REMOTE, "--prune")
        _git(repo_root, "fetch", PUSH_REMOTE, "--prune", check=False)

        report.pre_update_head = _git_output(repo_root, "rev-parse", "HEAD")
        report.upstream_head = _git_output(repo_root, "rev-parse", UPSTREAM_REF)

        upstream_missing = not _git_is_ancestor(repo_root, UPSTREAM_REF, "HEAD")
        push_needed = True
        if _git_branch_exists(repo_root, PUSH_REF):
            remote_head = _git_output(repo_root, "rev-parse", PUSH_REF)
            push_needed = remote_head != report.pre_update_head

        if not upstream_missing:
            report.post_update_head = report.pre_update_head
            if push_needed:
                push_result = _git(repo_root, "push", PUSH_REMOTE, f"{LIVE_BRANCH}:{LIVE_BRANCH}", check=False)
                if push_result.returncode == 0:
                    report.status = "updated"
                    report.push_status = "ok"
                    report.message = "Recovered a pending push to fork; upstream was already merged locally."
                else:
                    report.status = "push_failed"
                    report.push_status = "failed"
                    stderr = (push_result.stderr or push_result.stdout or "").strip()
                    report.message = f"Fork push failed while retrying a previously-updated local branch: {stderr or 'unknown error'}"
                return report

            report.status = "noop"
            report.push_status = "not_needed"
            report.message = "No upstream changes to apply and fork is already in sync."
            return report

        pre_sync = _sync_policy_history(hermes_home)
        pre_sync["phase"] = "pre-update"
        report.policy_history_sync.append(pre_sync)

        backup = _export_routing_backup(repo_root, hermes_home)
        report.backup_dir = backup["backup_dir"]

        stamp = started.strftime("%Y%m%d-%H%M%S")
        update_branch = f"{UPDATE_BRANCH_PREFIX}-{stamp}"
        update_worktree = _unique_worktree_path(repo_root, stamp)
        report.update_branch = update_branch
        report.update_worktree = str(update_worktree)

        host_cwd = _nearest_existing_parent(update_worktree)
        _ensure_safe_directory(host_cwd)
        _git(repo_root, "worktree", "add", "-b", update_branch, str(update_worktree), LIVE_BRANCH)
        _ensure_safe_directory(update_worktree)

        merge_result = _git(update_worktree, "merge", "--no-ff", UPSTREAM_REF, cwd=update_worktree, check=False)
        if merge_result.returncode != 0:
            report.status = "merge_conflict"
            stderr = (merge_result.stderr or merge_result.stdout or "").strip()
            report.message = f"Upstream merge produced conflicts in disposable worktree: {stderr or 'manual resolution required'}"
            report.retained_failed_worktree = str(update_worktree)
            return report

        try:
            report.tests_run = _run_verification_suite(update_worktree)
        except UpdateError as exc:
            report.status = "verification_failed"
            report.message = str(exc)
            report.retained_failed_worktree = str(update_worktree)
            return report

        ff_result = _git(repo_root, "merge", "--ff-only", update_branch, check=False)
        if ff_result.returncode != 0:
            report.status = "ff_failed"
            stderr = (ff_result.stderr or ff_result.stdout or "").strip()
            report.message = f"Fast-forward of live branch failed: {stderr or 'unknown error'}"
            report.retained_failed_worktree = str(update_worktree)
            return report

        _clear_update_check(hermes_home)
        report.post_update_head = _git_output(repo_root, "rev-parse", "HEAD")

        post_sync = _sync_policy_history(hermes_home)
        post_sync["phase"] = "post-update"
        report.policy_history_sync.append(post_sync)

        push_result = _git(repo_root, "push", PUSH_REMOTE, f"{LIVE_BRANCH}:{LIVE_BRANCH}", check=False)
        if push_result.returncode == 0:
            report.status = "updated"
            report.push_status = "ok"
            report.message = "Applied upstream Hermes changes, verified the routing stack, and pushed the integration branch to fork."
        else:
            report.status = "push_failed"
            report.push_status = "failed"
            stderr = (push_result.stderr or push_result.stdout or "").strip()
            report.message = (
                "Applied and verified the update locally, but pushing to fork failed. "
                f"Next scheduled run will retry the push. Details: {stderr or 'unknown error'}"
            )

        _remove_worktree_and_branch(repo_root, update_worktree, update_branch)
        update_worktree = None
        update_branch = None
        return report

    except UpdateError as exc:
        report.status = "setup_error"
        report.message = str(exc)
        if update_worktree and update_worktree.exists():
            report.retained_failed_worktree = str(update_worktree)
        return report
    except Exception as exc:  # pragma: no cover
        report.status = "setup_error"
        report.message = f"Unexpected error: {exc}"
        if update_worktree and update_worktree.exists():
            report.retained_failed_worktree = str(update_worktree)
        return report
    finally:
        report.finished_at = _iso(_utc_now())
        if not report.post_update_head:
            report.post_update_head = report.pre_update_head
        _write_report_files(report_root, report)
        _prune_retention(
            hermes_home,
            repo_root,
            keep_failed_worktree=_normalize_path(report.retained_failed_worktree) if report.retained_failed_worktree else None,
        )


def _install_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    install_parser = subparsers.add_parser("install", help="Install or update the routing auto-update cron job")
    install_parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    install_parser.add_argument("--json", action="store_true", help="Emit structured JSON")

    run_parser = subparsers.add_parser("run", help="Run the routing-preserving auto-update flow once")
    run_parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    run_parser.add_argument("--report-root", default="")
    run_parser.add_argument("--json", action="store_true", help="Emit structured JSON")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hermes routing-preserving auto-update orchestration")
    subparsers = parser.add_subparsers(dest="command", required=True)
    _install_parser(subparsers)
    args = parser.parse_args(argv)

    if args.command == "install":
        result = install_routing_auto_update(args.repo_root)
        if args.json:
            print(json.dumps(asdict(result), indent=2))
        else:
            print(result.message)
            print(f"job_id={result.job_id}")
        return 0

    report = run_routing_auto_update(args.repo_root, args.report_root or None)
    if args.json:
        print(json.dumps(asdict(report), indent=2))
    else:
        print(_render_markdown_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
