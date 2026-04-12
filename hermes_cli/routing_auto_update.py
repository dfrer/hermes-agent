#!/usr/bin/env python3
"""
Deterministic Hermes auto-update orchestration for the routing integration branch.
"""

from __future__ import annotations

import argparse
import importlib.util
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
from hermes_cli.routing_update_git import (
    GitBackend,
    GitBackendProbe,
    _linux_to_windows_path,
    probe_git_backend,
    select_git_backend,
)


LIVE_BRANCH = "codex/routing-integration"
UPSTREAM_REMOTE = "origin"
UPSTREAM_REF = "origin/main"
PUSH_REMOTE = "fork"
PUSH_REF = f"{PUSH_REMOTE}/{LIVE_BRANCH}"
PROMOTION_BRANCH = "main"
MAIN_REF = f"{PUSH_REMOTE}/{PROMOTION_BRANCH}"
EXPECTED_FORK_URL = "https://github.com/dfrer/hermes-agent.git"
EXPECTED_ORIGIN_URL = "https://github.com/NousResearch/hermes-agent.git"
ROUTING_AUTO_UPDATE_JOB_NAME = "routing auto update"
ROUTING_AUTO_UPDATE_SCHEDULE = "0 */4 * * *"
ROUTING_AUTO_UPDATE_TIMEZONE = "America/Vancouver"
ROUTING_AUTO_UPDATE_DELIVERY = "telegram"
REPORT_DIR_NAME = "routing-auto-update"
REPORT_HISTORY_DIR = "history"
ROUTING_BACKUP_DIRNAME = "routing-backups"
DEFAULT_RETENTION_DAYS = 30
UPDATE_BRANCH_PREFIX = "codex/upstream-sync"

ROUTING_CONTRACT_TESTS: tuple[str, ...] = (
    "scripts/test-routing-contract.ps1",
)

TRUST_GATE_PYTEST_ARGS: tuple[str, ...] = (
    "-m",
    "pytest",
    "-o",
    "addopts=",
    "tests/",
    "-q",
    "--ignore=tests/integration",
    "--ignore=tests/e2e",
    "--tb=short",
)

FAST_REPAIR_HINTS: tuple[str, ...] = (
    "rerun the failing verification command first",
    "prefer the smallest repair scoped to the retained worktree",
    "do not mutate dependency manifests, lockfiles, databases, binaries, or out-of-repo policy files",
)

PLAYBOOKS: tuple[dict[str, Any], ...] = (
    {
        "id": "routing-core",
        "label": "routing guard / prompt / tool registration",
        "patterns": (
            "agent/routing_guard.py",
            "agent/prompt_builder.py",
            "model_tools.py",
            "toolsets.py",
            "run_agent.py",
        ),
    },
    {
        "id": "run-agent-provider",
        "label": "run_agent request / extra-body / provider drift",
        "patterns": (
            "run_agent.py",
            "agent/auxiliary_client.py",
            "agent/smart_model_routing.py",
            "gateway/run.py",
        ),
    },
    {
        "id": "browser-gateway-schema",
        "label": "browser / gateway / tool schema drift",
        "patterns": (
            "tools/browser",
            "tools/browser_tool.py",
            "gateway/",
            "tools/",
        ),
    },
    {
        "id": "provider-auth-tests",
        "label": "provider metadata and auth command/test drift",
        "patterns": (
            "hermes_cli/auth_commands.py",
            "hermes_cli/config.py",
            "tests/hermes_cli/",
            "tests/gateway/",
            "tests/agent/",
        ),
    },
)

CRON_PROMPT_TEMPLATE = """You are running the daily Hermes routing-preserving updater.

Do not implement merge, test, or push logic yourself. The deterministic updater is authoritative.

1. Call `hermes routing update run --json --repo-root {repo_root}`.
2. Read `{latest_json}` and `{latest_md}`.
3. If `latest.json` reports `status == "noop"`, return exactly `[SILENT]`.
4. If `status == "updated"`, send a concise summary based on `latest.md` and stop.
5. If `status in {{"repair_required", "verification_failed"}}` and `repair_eligible == true`:
   - emit a `TIER: 3A | PATH: high-risk | ...` routing decision for guarded maintenance repair
   - submit a `routed_plan` over the retained worktree from `latest.json`
   - node 1: inspect `repair_manifest_path` and latest report
   - node 2: apply the smallest repair inside the retained worktree
   - node 3: rerun the targeted failing validation command from the manifest
   - node 4: call `hermes routing update finalize --json --repo-root {repo_root}`
   - node 5: summarize the finalized outcome
6. If repair is ineligible or finalize fails, stop and report the exact retained worktree and manifest paths.
"""


class UpdateError(RuntimeError):
    """Known deterministic updater failure."""


@dataclass
class UpdateReport:
    status: str
    started_at: str
    finished_at: str
    schedule_time: str = "every 4 hours"
    timezone: str = ROUTING_AUTO_UPDATE_TIMEZONE
    repo_root: str = ""
    live_branch: str = LIVE_BRANCH
    upstream_ref: str = UPSTREAM_REF
    push_remote: str = PUSH_REMOTE
    promotion_branch: str = PROMOTION_BRANCH
    pre_update_head: str = ""
    upstream_head: str = ""
    post_update_head: str = ""
    promoted_head: str = ""
    last_successful_sync_at: str = ""
    upstream_behind_count: int = 0
    upstream_ahead_count: int = 0
    fork_behind_count: int = 0
    fork_ahead_count: int = 0
    main_behind_count: int = 0
    main_ahead_count: int = 0
    update_branch: str = ""
    update_worktree: str = ""
    backup_dir: str = ""
    tests_run: list[str] = field(default_factory=list)
    validation_tier: str = "trust_gate"
    last_validation_command: str = ""
    push_status: str = "not_attempted"
    integration_push_status: str = "not_attempted"
    main_promotion_status: str = "not_attempted"
    auth_backend: str = "unavailable"
    fetch_auth_ready: bool = False
    push_auth_ready: bool = False
    auth_errors: dict[str, str] = field(default_factory=dict)
    auth_probe_details: dict[str, dict[str, Any]] = field(default_factory=dict)
    delivery_target: str = ROUTING_AUTO_UPDATE_DELIVERY
    message: str = ""
    policy_history_sync: list[dict[str, Any]] = field(default_factory=list)
    retained_failed_worktree: str = ""
    repair_manifest_path: str = ""
    repair_eligible: bool | None = None
    repair_blockers: list[str] = field(default_factory=list)
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
        f"- Upstream drift: behind `{report.upstream_behind_count}`, ahead `{report.upstream_ahead_count}`",
        f"- Fork drift: behind `{report.fork_behind_count}`, ahead `{report.fork_ahead_count}`",
        f"- Fork main drift: behind `{report.main_behind_count}`, ahead `{report.main_ahead_count}`",
        f"- Pre-update HEAD: `{report.pre_update_head or 'n/a'}`",
        f"- Post-update HEAD: `{report.post_update_head or report.pre_update_head or 'n/a'}`",
        f"- Promoted HEAD: `{report.promoted_head or 'n/a'}`",
        f"- Push remote: `{report.push_remote}` ({report.push_status})",
        f"- Integration push: `{report.integration_push_status}`",
        f"- Main promotion: `{report.main_promotion_status}`",
        f"- Auth backend: `{report.auth_backend}`",
        f"- Fetch auth ready: `{report.fetch_auth_ready}`",
        f"- Push auth ready: `{report.push_auth_ready}`",
        f"- Validation tier: `{report.validation_tier}`",
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
    if report.repair_manifest_path:
        lines.append(f"- Repair manifest: `{report.repair_manifest_path}`")
    if report.repair_eligible is not None:
        lines.append(f"- Repair eligible: `{report.repair_eligible}`")
    if report.repair_blockers:
        lines.append("- Repair blockers:")
        lines.extend([f"  - `{item}`" for item in report.repair_blockers])
    if report.last_successful_sync_at:
        lines.append(f"- Last successful sync: `{report.last_successful_sync_at}`")
    if report.last_validation_command:
        lines.append(f"- Last validation command: `{report.last_validation_command}`")
    if report.tests_run:
        lines.append("- Tests run:")
        lines.extend([f"  - `{item}`" for item in report.tests_run])
    if report.policy_history_sync:
        lines.append("- Policy history sync:")
        for item in report.policy_history_sync:
            lines.append(
                f"  - `{item.get('phase', 'unknown')}`: {item.get('status', 'unknown')} ({item.get('head', 'n/a')})"
            )
    if report.auth_errors:
        lines.append("- Auth errors:")
        for key, value in sorted(report.auth_errors.items()):
            lines.append(f"  - `{key}`: {value}")
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


def _ahead_behind(repo_root: Path, left_ref: str, right_ref: str) -> tuple[int, int]:
    result = _git(repo_root, "rev-list", "--left-right", "--count", f"{left_ref}...{right_ref}", check=False)
    if result.returncode != 0:
        return 0, 0
    parts = (result.stdout or "").replace("\t", " ").split()
    if len(parts) < 2:
        return 0, 0
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return 0, 0


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


def _repo_local_setting(repo_root: Path, key: str) -> str:
    result = _git(repo_root, "config", "--local", "--get", key, check=False)
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()


def _ensure_repo_merge_defaults(repo_root: Path) -> None:
    defaults = {
        "rerere.enabled": "true",
        "rerere.autoupdate": "true",
        "merge.conflictstyle": "zdiff3",
    }
    for key, expected in defaults.items():
        if _repo_local_setting(repo_root, key) != expected:
            _git(repo_root, "config", "--local", key, expected)


def _merge_readiness_issues(repo_root: Path) -> list[str]:
    expected = {
        "rerere.enabled": "true",
        "rerere.autoupdate": "true",
        "merge.conflictstyle": "zdiff3",
    }
    issues: list[str] = []
    for key, value in expected.items():
        actual = _repo_local_setting(repo_root, key)
        if actual != value:
            issues.append(f"{key}={actual or '(unset)'} expected {value}")
    return issues


def _is_safe_directory_configured(path: Path) -> bool:
    normalized = str(_normalize_path(path))
    current = _run_subprocess(["git", "config", "--global", "--get-all", "safe.directory"], check=False)
    lines = {(line or "").strip() for line in (current.stdout or "").splitlines() if line.strip()}
    return normalized in lines or "*" in lines


def _origin_url(repo_root: Path) -> str:
    result = _git(repo_root, "remote", "get-url", UPSTREAM_REMOTE, check=False)
    return (result.stdout or "").strip()


def detect_routing_update_topology(repo_root: Path | str | None = None) -> dict[str, Any]:
    repo_root = _normalize_path(repo_root or PROJECT_ROOT)
    current_branch = ""
    try:
        current_branch = _git_output(repo_root, "branch", "--show-current")
    except UpdateError:
        current_branch = ""
    origin_url = _origin_url(repo_root)
    fork_url = ""
    try:
        fork_url = _git_output(repo_root, "remote", "get-url", PUSH_REMOTE)
    except UpdateError:
        fork_url = ""
    return {
        "repo_root": str(repo_root),
        "current_branch": current_branch,
        "live_branch": LIVE_BRANCH,
        "origin_remote": UPSTREAM_REMOTE,
        "origin_url": origin_url,
        "fork_remote": PUSH_REMOTE,
        "fork_url": fork_url,
        "promotion_branch": PROMOTION_BRANCH,
        "matches": (
            _normalize_remote_url(origin_url) == _normalize_remote_url(EXPECTED_ORIGIN_URL)
            and _normalize_remote_url(fork_url) == _normalize_remote_url(EXPECTED_FORK_URL)
            and bool(LIVE_BRANCH)
        ),
    }


def is_routing_update_topology(repo_root: Path | str | None = None) -> bool:
    return bool(detect_routing_update_topology(repo_root).get("matches"))


def _current_ref(repo_root: Path, ref: str) -> str:
    try:
        return _git_output(repo_root, "rev-parse", ref)
    except UpdateError:
        return ""


def _load_update_cache_state(hermes_home: Path) -> dict[str, Any]:
    cache_file = hermes_home / ".update_check"
    if not cache_file.exists():
        return {}
    try:
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _resolve_powershell_command(script_path: Path) -> list[str]:
    script = str(script_path)
    if platform.system() == "Windows":
        return ["powershell", "-ExecutionPolicy", "Bypass", "-File", script]
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if pwsh:
        return [pwsh, "-ExecutionPolicy", "Bypass", "-File", script]
    windows_ps = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    if windows_ps.exists():
        return [str(windows_ps), "-ExecutionPolicy", "Bypass", "-File", _linux_to_windows_path(script_path)]
    raise UpdateError("No PowerShell runtime available for routing contract verification.")


def _trust_gate_supports_xdist() -> bool:
    return importlib.util.find_spec("xdist") is not None


def _build_trust_gate_pytest_cmd() -> list[str]:
    cmd = [sys.executable, *TRUST_GATE_PYTEST_ARGS]
    if _trust_gate_supports_xdist():
        cmd.extend(["-n", "auto"])
    return cmd


def _run_trust_gate(worktree: Path) -> list[str]:
    executed: list[str] = []
    pytest_cmd = _build_trust_gate_pytest_cmd()
    _run_subprocess(pytest_cmd, cwd=worktree)
    executed.append(" ".join(pytest_cmd))

    contract_script = worktree / ROUTING_CONTRACT_TESTS[0]
    contract_cmd = _resolve_powershell_command(contract_script)
    _run_subprocess(contract_cmd, cwd=worktree)
    executed.append(" ".join(contract_cmd))
    return executed


def _eligible_text_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    name = Path(normalized).name.lower()
    if any(part in {".git", "__pycache__"} for part in Path(normalized).parts):
        return False
    if name in {"package-lock.json", "pnpm-lock.yaml", "poetry.lock", "uv.lock", "state.db"}:
        return False
    if name.endswith((".db", ".sqlite", ".sqlite3", ".bin", ".exe", ".dll", ".so", ".dylib", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".zip", ".tar", ".gz")):
        return False
    if normalized.startswith("../") or normalized.startswith("..\\"):
        return False
    return name.endswith(
        (
            ".py",
            ".ps1",
            ".sh",
            ".md",
            ".txt",
            ".yaml",
            ".yml",
            ".json",
            ".toml",
            ".ini",
            ".cfg",
            ".service",
            ".plist",
        )
    ) or "/tests/" in normalized or normalized.startswith("tests/")


def _collect_playbooks(paths: Sequence[str]) -> list[dict[str, str]]:
    selected: list[dict[str, str]] = []
    normalized_paths = [path.replace("\\", "/") for path in paths]
    for playbook in PLAYBOOKS:
        if any(any(pattern in path for pattern in playbook["patterns"]) for path in normalized_paths):
            selected.append({"id": playbook["id"], "label": playbook["label"]})
    return selected


def _repair_eligibility(paths: Sequence[str]) -> tuple[bool, list[str]]:
    blockers: list[str] = []
    if not paths:
        blockers.append("no changed paths recorded")
        return False, blockers
    if len(paths) > 40:
        blockers.append(f"changed path count {len(paths)} exceeds conservative repair cap")
    for path in paths:
        if not _eligible_text_path(path):
            blockers.append(f"ineligible repair target: {path}")
    return not blockers, blockers


def _write_repair_manifest(
    report_root: Path,
    report: UpdateReport,
    *,
    failure_kind: str,
    changed_paths: Sequence[str],
) -> tuple[str, bool, list[str]]:
    eligible, blockers = _repair_eligibility(changed_paths)
    manifest = {
        "failure_kind": failure_kind,
        "repo_root": report.repo_root,
        "live_branch": report.live_branch,
        "upstream_ref": report.upstream_ref,
        "upstream_head": report.upstream_head,
        "pre_update_head": report.pre_update_head,
        "update_branch": report.update_branch,
        "update_worktree": report.update_worktree,
        "retained_worktree": report.retained_failed_worktree or report.update_worktree,
        "changed_paths": list(changed_paths),
        "repair_eligible": eligible,
        "repair_blockers": blockers,
        "last_validation_command": report.last_validation_command,
        "trust_gate_commands": report.tests_run,
        "fast_repair_hints": list(FAST_REPAIR_HINTS),
        "playbooks": _collect_playbooks(changed_paths),
    }
    manifest_path = report_root / f"repair-manifest-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    _json_dump(manifest, manifest_path)
    return str(manifest_path), eligible, blockers


def _compute_live_drift(repo_root: Path) -> dict[str, Any]:
    return {
        "current_head": _current_ref(repo_root, "HEAD"),
        "upstream_head": _current_ref(repo_root, UPSTREAM_REF),
        "integration_head": _current_ref(repo_root, PUSH_REF),
        "main_head": _current_ref(repo_root, MAIN_REF),
        "upstream": {
            "behind": _ahead_behind(repo_root, UPSTREAM_REF, "HEAD")[0],
            "ahead": _ahead_behind(repo_root, UPSTREAM_REF, "HEAD")[1],
        },
        "integration": {
            "behind": _ahead_behind(repo_root, PUSH_REF, "HEAD")[0],
            "ahead": _ahead_behind(repo_root, PUSH_REF, "HEAD")[1],
        },
        "main": {
            "behind": _ahead_behind(repo_root, MAIN_REF, "HEAD")[0],
            "ahead": _ahead_behind(repo_root, MAIN_REF, "HEAD")[1],
        },
    }


def _latest_job_state() -> dict[str, Any]:
    jobs = [job for job in list_jobs(include_disabled=True) if job.get("name") == ROUTING_AUTO_UPDATE_JOB_NAME]
    if not jobs:
        return {"installed": False}
    primary = jobs[0]
    return {
        "installed": True,
        "job_id": primary.get("id") or "",
        "state": primary.get("state") or "",
        "next_run_at": primary.get("next_run_at") or "",
        "schedule_display": primary.get("schedule_display") or "",
    }


def _refresh_remote_refs(repo_root: Path, backend: GitBackend | None) -> None:
    if backend is None:
        return
    backend.run(["fetch", UPSTREAM_REMOTE, "--prune"], check=False)
    backend.run(["fetch", PUSH_REMOTE, "--prune"], check=False)


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
    _ensure_repo_merge_defaults(repo_root)
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


def _target_needs_promotion(repo_root: Path, current_head: str, target_ref: str, target_head: str) -> bool:
    if not current_head:
        return False
    if not target_head:
        return True
    if current_head == target_head:
        return False
    return not _git_is_ancestor(repo_root, current_head, target_ref)


def _promotion_plan(repo_root: Path, current_head: str, integration_head: str, main_head: str) -> dict[str, bool]:
    return {
        "integration": _target_needs_promotion(repo_root, current_head, PUSH_REF, integration_head),
        "main": _target_needs_promotion(repo_root, current_head, MAIN_REF, main_head),
    }


def _realign_live_branch_to_promoted_head(repo_root: Path, current_head: str, integration_head: str, main_head: str) -> str:
    for target_ref, target_head in ((MAIN_REF, main_head), (PUSH_REF, integration_head)):
        if not target_head or target_head == current_head:
            continue
        if not _git_is_ancestor(repo_root, current_head, target_ref):
            continue
        ff_result = _git(repo_root, "merge", "--ff-only", target_ref, check=False)
        if ff_result.returncode != 0:
            stderr = (ff_result.stderr or ff_result.stdout or "").strip() or "unknown error"
            raise UpdateError(f"Could not fast-forward {LIVE_BRANCH} to {target_ref}: {stderr}")
        return target_ref
    return ""


def _push_targets(
    repo_root: Path,
    backend: GitBackend,
    report: UpdateReport,
    target_head: str,
    *,
    push_integration: bool,
    push_main: bool,
) -> None:
    pushed_any = False
    if push_integration:
        integration_push = backend.run(
            ["push", "--porcelain", PUSH_REMOTE, f"HEAD:refs/heads/{LIVE_BRANCH}"],
            check=False,
        )
        if integration_push.returncode != 0:
            report.integration_push_status = "failed"
            report.push_status = "failed"
            if not push_main:
                report.main_promotion_status = "not_needed"
            stderr = (integration_push.stderr or integration_push.stdout or "").strip() or "unknown error"
            report.auth_errors["push:integration"] = stderr
            report.message = f"Failed to push {LIVE_BRANCH} to fork: {stderr}"
            return
        report.integration_push_status = "ok"
        report.push_status = "ok"
        pushed_any = True
    else:
        report.integration_push_status = "not_needed"

    if push_main:
        main_push = backend.run(
            ["push", "--porcelain", PUSH_REMOTE, f"HEAD:refs/heads/{PROMOTION_BRANCH}"],
            check=False,
        )
        if main_push.returncode != 0:
            report.main_promotion_status = "failed"
            if not pushed_any:
                report.push_status = "failed"
            stderr = (main_push.stderr or main_push.stdout or "").strip() or "unknown error"
            report.auth_errors["push:main"] = stderr
            report.message = f"Updated {LIVE_BRANCH} on fork, but promoting fork/main failed: {stderr}"
            return
        report.main_promotion_status = "ok"
        pushed_any = True
    else:
        report.main_promotion_status = "not_needed"

    if pushed_any:
        report.push_status = "ok"
        report.promoted_head = target_head
        report.last_successful_sync_at = _iso(_utc_now())
    else:
        report.push_status = "not_needed"


def _normalize_retained_failure(latest: dict[str, Any], upstream_head: str) -> dict[str, Any] | None:
    if not latest:
        return None
    status = str(latest.get("status") or "")
    if status not in {"repair_required", "verification_failed", "ff_failed", "finalize_failed"}:
        return None

    retained = str(latest.get("retained_failed_worktree") or latest.get("update_worktree") or "")
    if not retained:
        return None
    retained_path = _normalize_path(retained)
    if not retained_path.exists():
        return None

    stored_upstream = str(latest.get("upstream_head") or "")
    if stored_upstream and stored_upstream != upstream_head and not _git_is_ancestor(retained_path, upstream_head, "HEAD"):
        return None

    update_branch = str(latest.get("update_branch") or "").strip()
    if not update_branch:
        try:
            update_branch = _git_output(retained_path, "branch", "--show-current", cwd=retained_path).strip()
        except Exception:
            update_branch = ""
    if update_branch and not update_branch.startswith(UPDATE_BRANCH_PREFIX):
        return None

    normalized = dict(latest)
    normalized["status"] = status if status in {"repair_required", "verification_failed", "ff_failed"} else "verification_failed"
    normalized["upstream_head"] = upstream_head
    normalized["update_worktree"] = str(retained_path)
    normalized["retained_failed_worktree"] = str(retained_path)
    if update_branch:
        normalized["update_branch"] = update_branch
    return normalized


def _discover_retained_failure_from_worktrees(repo_root: Path, upstream_head: str) -> dict[str, Any] | None:
    try:
        raw = _git_output(repo_root, "worktree", "list", "--porcelain")
    except Exception:
        return None
    blocks: list[dict[str, str]] = []
    block: dict[str, str] = {}

    def flush() -> None:
        nonlocal block
        if block:
            blocks.append(block)
        block = {}

    for line in raw.splitlines():
        if not line.strip():
            flush()
            continue
        key, _, value = line.partition(" ")
        if key in {"worktree", "branch", "HEAD"}:
            block[key] = value.strip()
    flush()

    candidates: list[dict[str, Any]] = []
    for entry in blocks:
        worktree_value = entry.get("worktree") or ""
        if not worktree_value:
            continue
        worktree_path = _normalize_path(worktree_value)
        if worktree_path == repo_root or not worktree_path.exists():
            continue

        branch_ref = entry.get("branch") or ""
        branch_name = branch_ref.removeprefix("refs/heads/")
        if not branch_name.startswith(UPDATE_BRANCH_PREFIX):
            continue

        if not _git_is_ancestor(worktree_path, upstream_head, "HEAD"):
            continue

        candidates.append(
            {
                "status": "verification_failed",
                "upstream_head": upstream_head,
                "update_branch": branch_name,
                "update_worktree": str(worktree_path),
                "retained_failed_worktree": str(worktree_path),
            }
        )

    if not candidates:
        return None

    candidates.sort(key=lambda item: str(item.get("update_branch") or ""), reverse=True)
    return candidates[0]


def _latest_retained_failure(repo_root: Path, latest: dict[str, Any], upstream_head: str) -> dict[str, Any] | None:
    retained = _normalize_retained_failure(latest, upstream_head)
    if retained:
        return retained
    return _discover_retained_failure_from_worktrees(repo_root, upstream_head)


def _preflight_backend(repo_root: Path, report: UpdateReport) -> tuple[GitBackend | None, GitBackendProbe]:
    backend, probe = select_git_backend(
        repo_root,
        upstream_remote=UPSTREAM_REMOTE,
        push_remote=PUSH_REMOTE,
        live_branch=LIVE_BRANCH,
        promotion_branch=PROMOTION_BRANCH,
    )
    report.auth_backend = probe.backend
    report.fetch_auth_ready = probe.fetch_ready
    report.push_auth_ready = probe.push_ready
    report.auth_errors = dict(probe.errors)
    report.auth_probe_details = probe.details
    return backend, probe


def _run_state_machine(
    repo_root: Path,
    report_root: Path,
    report: UpdateReport,
    *,
    finalize_from_retained: bool = False,
) -> UpdateReport:
    hermes_home = _normalize_path(get_hermes_home())
    latest_report = read_latest_update_report(report_root)
    update_worktree: Path | None = None
    update_branch: str | None = None

    _ensure_safe_directory(repo_root)
    topology = detect_routing_update_topology(repo_root)
    if not topology.get("matches"):
        raise UpdateError(
            f"Routing updater requires {EXPECTED_ORIGIN_URL} as '{UPSTREAM_REMOTE}' and {EXPECTED_FORK_URL} as '{PUSH_REMOTE}'."
        )
    _ensure_fork_remote(repo_root)

    current_branch = _git_output(repo_root, "branch", "--show-current")
    if current_branch != LIVE_BRANCH:
        raise UpdateError(f"Live repo is on '{current_branch}', expected '{LIVE_BRANCH}'.")

    dirty = _git_output(repo_root, "status", "--porcelain")
    if dirty:
        report.status = "dirty_worktree"
        report.message = "Live worktree is dirty; routing auto-update aborted before any mutation."
        return report

    merge_issues = _merge_readiness_issues(repo_root)
    if merge_issues:
        raise UpdateError("Repo merge defaults are not ready: " + "; ".join(merge_issues))

    backend, probe = _preflight_backend(repo_root, report)
    if backend is None or not probe.fetch_ready:
        report.status = "auth_failed"
        details = [f"{key}={value}" for key, value in sorted(probe.errors.items())]
        report.message = "Git auth preflight failed. " + ("; ".join(details) if details else "No usable backend.")
        return report

    _refresh_remote_refs(repo_root, backend)

    report.pre_update_head = _current_ref(repo_root, "HEAD")
    report.upstream_head = _current_ref(repo_root, UPSTREAM_REF)
    if not report.upstream_head:
        raise UpdateError(f"Could not resolve {UPSTREAM_REF}.")

    drift = _compute_live_drift(repo_root)
    report.upstream_behind_count = int(drift["upstream"]["behind"])
    report.upstream_ahead_count = int(drift["upstream"]["ahead"])
    report.fork_behind_count = int(drift["integration"]["behind"])
    report.fork_ahead_count = int(drift["integration"]["ahead"])
    report.main_behind_count = int(drift["main"]["behind"])
    report.main_ahead_count = int(drift["main"]["ahead"])

    retained = _latest_retained_failure(repo_root, latest_report, report.upstream_head)
    if retained and not finalize_from_retained:
        report.status = str(retained.get("status") or "repair_required")
        report.message = "A retained maintenance worktree for the current upstream head is still pending repair or finalize."
        report.update_branch = str(retained.get("update_branch") or "")
        report.update_worktree = str(retained.get("update_worktree") or "")
        report.retained_failed_worktree = str(retained.get("retained_failed_worktree") or "")
        report.repair_manifest_path = str(retained.get("repair_manifest_path") or "")
        report.repair_eligible = retained.get("repair_eligible")
        report.repair_blockers = list(retained.get("repair_blockers") or [])
        return report

    realigned_to = _realign_live_branch_to_promoted_head(
        repo_root,
        report.pre_update_head,
        drift.get("integration_head") or "",
        drift.get("main_head") or "",
    )
    if realigned_to:
        report.pre_update_head = _current_ref(repo_root, "HEAD")
        drift = _compute_live_drift(repo_root)
        report.upstream_behind_count = int(drift["upstream"]["behind"])
        report.upstream_ahead_count = int(drift["upstream"]["ahead"])
        report.fork_behind_count = int(drift["integration"]["behind"])
        report.fork_ahead_count = int(drift["integration"]["ahead"])
        report.main_behind_count = int(drift["main"]["behind"])
        report.main_ahead_count = int(drift["main"]["ahead"])

    upstream_missing = not _git_is_ancestor(repo_root, UPSTREAM_REF, "HEAD")
    promotion_plan = _promotion_plan(
        repo_root,
        report.pre_update_head,
        drift.get("integration_head") or "",
        drift.get("main_head") or "",
    )
    pending_promotion = any(promotion_plan.values())

    if not upstream_missing and not finalize_from_retained:
        report.post_update_head = report.pre_update_head
        if pending_promotion:
            _push_targets(
                repo_root,
                backend,
                report,
                report.pre_update_head,
                push_integration=promotion_plan["integration"],
                push_main=promotion_plan["main"],
            )
            promotion_ok = (
                report.integration_push_status in {"ok", "not_needed"}
                and report.main_promotion_status in {"ok", "not_needed"}
            )
            report.status = "updated" if promotion_ok else "push_failed"
            if report.status == "updated":
                if realigned_to:
                    if promotion_plan["integration"] and not promotion_plan["main"]:
                        report.message = (
                            f"Fast-forwarded {LIVE_BRANCH} to {realigned_to} and restored the fork integration branch."
                        )
                    elif promotion_plan["main"] and not promotion_plan["integration"]:
                        report.message = (
                            f"Fast-forwarded {LIVE_BRANCH} to {realigned_to} and restored fork/main promotion."
                        )
                    else:
                        report.message = f"Fast-forwarded {LIVE_BRANCH} to {realigned_to} and restored fork promotion."
                elif promotion_plan["integration"] and not promotion_plan["main"]:
                    report.message = "Recovered pending fork integration branch without creating a new update worktree."
                elif promotion_plan["main"] and not promotion_plan["integration"]:
                    report.message = "Recovered pending fork/main promotion without creating a new update worktree."
                else:
                    report.message = "Recovered pending fork promotion without creating a new update worktree."
            return report

        report.status = "noop"
        report.push_status = "not_needed"
        report.integration_push_status = "not_needed"
        report.main_promotion_status = "not_needed"
        if realigned_to:
            report.message = f"No upstream changes to apply; fast-forwarded {LIVE_BRANCH} to {realigned_to}."
        else:
            report.message = "No upstream changes to apply and fork promotion is already in sync."
        return report

    if finalize_from_retained:
        if not retained:
            raise UpdateError("No retained routed-maintenance worktree is available to finalize.")
        update_worktree = _normalize_path(str(retained.get("retained_failed_worktree") or ""))
        update_branch = str(retained.get("update_branch") or "")
        if not update_worktree.exists():
            raise UpdateError(f"Retained worktree is missing: {update_worktree}")
        if not update_branch:
            raise UpdateError("Latest retained repair report is missing update_branch.")
        if _git_output(update_worktree, "branch", "--show-current", cwd=update_worktree) != update_branch:
            raise UpdateError("Retained worktree no longer matches the prepared update branch.")
        report.update_branch = update_branch
        report.update_worktree = str(update_worktree)
        report.retained_failed_worktree = str(update_worktree)
    else:
        pre_sync = _sync_policy_history(hermes_home)
        pre_sync["phase"] = "pre-update"
        report.policy_history_sync.append(pre_sync)

        backup = _export_routing_backup(repo_root, hermes_home)
        report.backup_dir = backup["backup_dir"]

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
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
            report.status = "repair_required"
            stderr = (merge_result.stderr or merge_result.stdout or "").strip() or "manual resolution required"
            report.message = f"Upstream merge produced conflicts in the retained update worktree: {stderr}"
            report.retained_failed_worktree = str(update_worktree)
            conflicted = _git_output(update_worktree, "diff", "--name-only", "--diff-filter=U", cwd=update_worktree).splitlines()
            report.repair_manifest_path, report.repair_eligible, report.repair_blockers = _write_repair_manifest(
                report_root,
                report,
                failure_kind="merge_conflict",
                changed_paths=[path for path in conflicted if path.strip()],
            )
            return report

    try:
        report.tests_run = _run_trust_gate(update_worktree)
        report.last_validation_command = report.tests_run[-1] if report.tests_run else ""
    except UpdateError as exc:
        report.status = "verification_failed"
        report.message = str(exc)
        report.retained_failed_worktree = str(update_worktree)
        changed = _git_output(update_worktree, "diff", "--name-only", f"{LIVE_BRANCH}...HEAD", cwd=update_worktree).splitlines()
        report.repair_manifest_path, report.repair_eligible, report.repair_blockers = _write_repair_manifest(
            report_root,
            report,
            failure_kind="verification_failed",
            changed_paths=[path for path in changed if path.strip()],
        )
        return report

    ff_result = _git(repo_root, "merge", "--ff-only", update_branch, check=False)
    if ff_result.returncode != 0:
        report.status = "ff_failed"
        stderr = (ff_result.stderr or ff_result.stdout or "").strip() or "unknown error"
        report.message = f"Fast-forward of {LIVE_BRANCH} failed: {stderr}"
        report.retained_failed_worktree = str(update_worktree)
        changed = _git_output(update_worktree, "diff", "--name-only", f"{LIVE_BRANCH}...HEAD", cwd=update_worktree).splitlines()
        report.repair_manifest_path, report.repair_eligible, report.repair_blockers = _write_repair_manifest(
            report_root,
            report,
            failure_kind="fast_forward_failed",
            changed_paths=[path for path in changed if path.strip()],
        )
        return report

    _clear_update_check(hermes_home)
    report.post_update_head = _current_ref(repo_root, "HEAD")

    post_sync = _sync_policy_history(hermes_home)
    post_sync["phase"] = "post-update"
    report.policy_history_sync.append(post_sync)

    _push_targets(
        repo_root,
        backend,
        report,
        report.post_update_head,
        push_integration=True,
        push_main=True,
    )
    if report.integration_push_status == "ok" and report.main_promotion_status == "ok":
        report.status = "updated"
        report.message = "Applied upstream changes, passed the trust gate, and promoted fork integration + main."
    else:
        report.status = "push_failed"

    _remove_worktree_and_branch(repo_root, update_worktree, update_branch)
    report.retained_failed_worktree = ""
    report.repair_manifest_path = ""
    report.repair_eligible = None
    report.repair_blockers = []
    return report

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
    try:
        return _run_state_machine(repo_root, report_root, report)
    except UpdateError as exc:
        report.status = "setup_error"
        report.message = str(exc)
        return report
    except Exception as exc:  # pragma: no cover
        report.status = "setup_error"
        report.message = f"Unexpected error: {exc}"
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


def finalize_routing_auto_update(repo_root: Path | None = None, report_root: Path | None = None) -> UpdateReport:
    started = _utc_now()
    repo_root = _normalize_path(repo_root or PROJECT_ROOT)
    hermes_home = _normalize_path(get_hermes_home())
    report_root = _normalize_path(report_root or _default_report_root(hermes_home))

    report = UpdateReport(
        status="finalize_failed",
        started_at=_iso(started),
        finished_at=_iso(started),
        repo_root=str(repo_root),
    )

    gateway_running, service_installed, telegram_connected = _current_gateway_health()
    report.gateway_running = gateway_running
    report.gateway_service_installed = service_installed
    report.telegram_connected = telegram_connected

    try:
        return _run_state_machine(repo_root, report_root, report, finalize_from_retained=True)
    except UpdateError as exc:
        report.status = "finalize_failed"
        report.message = str(exc)
        return report
    except Exception as exc:  # pragma: no cover
        report.status = "finalize_failed"
        report.message = f"Unexpected error: {exc}"
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


def read_latest_update_report(report_root: Path | str | None = None) -> dict[str, Any]:
    hermes_home = _normalize_path(get_hermes_home())
    root = _normalize_path(report_root or _default_report_root(hermes_home))
    latest = root / "latest.json"
    if not latest.exists():
        return {}
    try:
        payload = json.loads(latest.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _status_missing_payload(repo_root: Path, gateway_running: bool, service_installed: bool, telegram_connected: bool) -> dict[str, Any]:
    return {
        "status": "missing",
        "message": "No routing auto-update report has been written yet.",
        "last_run": "",
        "last_successful_sync_at": "",
        "repo_root": str(repo_root),
        "live_branch": LIVE_BRANCH,
        "upstream_ref": UPSTREAM_REF,
        "branch_drift": {},
        "current_drift": {},
        "last_error": "",
        "retained_worktree": "",
        "repair_manifest_path": "",
        "repair_eligible": None,
        "repair_blockers": [],
        "gateway_running": gateway_running,
        "gateway_service_installed": service_installed,
        "telegram_connected": telegram_connected,
        "push_status": "not_attempted",
        "integration_push_status": "not_attempted",
        "main_promotion_status": "not_attempted",
        "auth": {
            "backend": "unavailable",
            "fetch_ready": False,
            "push_ready": False,
            "errors": {},
            "details": {},
        },
        "job": _latest_job_state(),
        "topology": detect_routing_update_topology(repo_root),
    }


def routing_update_status(
    report_root: Path | str | None = None,
    repo_root: Path | str | None = None,
    *,
    probe_auth: bool = True,
    refresh_refs: bool = False,
) -> dict[str, Any]:
    hermes_home = _normalize_path(get_hermes_home())
    latest = read_latest_update_report(report_root)
    effective_repo = _normalize_path(repo_root or latest.get("repo_root") or PROJECT_ROOT)
    gateway_running, service_installed, telegram_connected = _current_gateway_health()
    summary = _status_missing_payload(effective_repo, gateway_running, service_installed, telegram_connected)

    if latest:
        status = str(latest.get("status") or "")
        message = str(latest.get("message") or "")
        summary.update(
            {
                "status": status,
                "message": message,
                "last_run": latest.get("finished_at") or latest.get("started_at") or "",
                "last_successful_sync_at": latest.get("last_successful_sync_at") or "",
                "repo_root": latest.get("repo_root") or str(effective_repo),
                "live_branch": latest.get("live_branch") or LIVE_BRANCH,
                "upstream_ref": latest.get("upstream_ref") or UPSTREAM_REF,
                "last_error": "" if status in {"noop", "updated"} else message,
                "retained_worktree": latest.get("retained_failed_worktree") or "",
                "repair_manifest_path": latest.get("repair_manifest_path") or "",
                "repair_eligible": latest.get("repair_eligible"),
                "repair_blockers": list(latest.get("repair_blockers") or []),
                "push_status": latest.get("push_status") or "not_attempted",
                "integration_push_status": latest.get("integration_push_status") or "not_attempted",
                "main_promotion_status": latest.get("main_promotion_status") or "not_attempted",
            }
        )

    topology = detect_routing_update_topology(effective_repo)
    summary["topology"] = topology
    job = _latest_job_state()
    summary["job"] = job

    auth_backend = "unavailable"
    fetch_ready = False
    push_ready = False
    auth_errors: dict[str, str] = {}
    auth_details: dict[str, dict[str, Any]] = {}
    if (effective_repo / ".git").exists():
        if probe_auth:
            backend, probe = select_git_backend(
                effective_repo,
                upstream_remote=UPSTREAM_REMOTE,
                push_remote=PUSH_REMOTE,
                live_branch=LIVE_BRANCH,
                promotion_branch=PROMOTION_BRANCH,
            )
            auth_backend = probe.backend
            fetch_ready = probe.fetch_ready
            push_ready = probe.push_ready
            auth_errors = dict(probe.errors)
            auth_details = dict(probe.details)
            if backend is not None and probe.fetch_ready and refresh_refs:
                _refresh_remote_refs(effective_repo, backend)
        live_drift = _compute_live_drift(effective_repo)
        summary["current_drift"] = live_drift
        summary["branch_drift"] = {
            "upstream_behind": int(live_drift["upstream"]["behind"]),
            "upstream_ahead": int(live_drift["upstream"]["ahead"]),
            "fork_behind": int(live_drift["integration"]["behind"]),
            "fork_ahead": int(live_drift["integration"]["ahead"]),
            "main_behind": int(live_drift["main"]["behind"]),
            "main_ahead": int(live_drift["main"]["ahead"]),
        }
        summary["promotion_pending"] = any(
            _promotion_plan(
                effective_repo,
                live_drift.get("current_head") or "",
                live_drift.get("integration_head") or "",
                live_drift.get("main_head") or "",
            ).values()
        )

    summary["auth"] = {
        "backend": auth_backend,
        "fetch_ready": fetch_ready,
        "push_ready": push_ready,
        "errors": auth_errors,
        "details": auth_details,
    }
    summary["gateway_running"] = gateway_running
    summary["gateway_service_installed"] = service_installed
    summary["telegram_connected"] = telegram_connected
    summary["update_cache_state"] = _load_update_cache_state(hermes_home)
    return summary


def routing_update_doctor(
    report_root: Path | str | None = None,
    repo_root: Path | str | None = None,
) -> dict[str, Any]:
    summary = routing_update_status(report_root, repo_root)
    effective_repo = _normalize_path(summary.get("repo_root") or repo_root or PROJECT_ROOT)

    issues: list[str] = []
    checks = {
        "topology": bool(summary.get("topology", {}).get("matches")),
        "live_branch": summary.get("topology", {}).get("current_branch") == LIVE_BRANCH,
        "safe_directory": _is_safe_directory_configured(effective_repo) if (effective_repo / ".git").exists() else False,
        "merge_defaults": not _merge_readiness_issues(effective_repo) if (effective_repo / ".git").exists() else False,
        "fetch_auth": bool(summary.get("auth", {}).get("fetch_ready")),
        "push_auth": bool(summary.get("auth", {}).get("push_ready")),
        "job_installed": bool(summary.get("job", {}).get("installed")),
        "gateway_running": bool(summary.get("gateway_running")),
        "telegram_connected": bool(summary.get("telegram_connected")),
        "update_cache_hygiene": True,
        "retained_worktree_present": False,
    }

    retained = str(summary.get("retained_worktree") or "")
    if retained:
        retained_path = _normalize_path(retained)
        checks["retained_worktree_present"] = retained_path.exists()
        if not retained_path.exists():
            issues.append(f"Retained failed worktree is missing: {retained}")
    if not checks["topology"]:
        issues.append("Repo remotes/branch do not match the routing-maintenance fork topology.")
    if not checks["safe_directory"]:
        issues.append("git safe.directory is not configured for the live repo.")
    merge_issues = _merge_readiness_issues(effective_repo) if (effective_repo / ".git").exists() else ["repo missing .git"]
    if merge_issues:
        issues.extend([f"merge readiness: {item}" for item in merge_issues])
    if not checks["gateway_running"]:
        issues.append("gateway delivery path is not running")
    if not checks["telegram_connected"]:
        issues.append("telegram delivery path is not connected")
    auth_errors = summary.get("auth", {}).get("errors") or {}
    for key, value in sorted(auth_errors.items()):
        issues.append(f"{key}: {value}")

    status = "ready" if not issues else "degraded"
    message = (
        "Routing auto-update readiness looks healthy."
        if status == "ready"
        else "Routing auto-update has readiness issues that should be fixed before trusting unattended promotion."
    )
    return {
        "status": status,
        "message": message,
        "repo_root": str(effective_repo),
        "checks": checks,
        "issues": issues,
        "summary": summary,
    }


def routing_status_command(args) -> None:
    summary = routing_update_status(
        getattr(args, "report_root", "") or None,
        getattr(args, "repo_root", "") or None,
    )
    if getattr(args, "json", False):
        print(json.dumps(summary, indent=2))
        return

    drift = summary.get("branch_drift") or {}
    print(f"Routing update status: {summary.get('status')}")
    if summary.get("last_run"):
        print(f"Last run: {summary['last_run']}")
    if summary.get("message"):
        print(f"Message: {summary['message']}")
    print(
        "Branch drift: "
        f"upstream behind {drift.get('upstream_behind', 0)}, "
        f"upstream ahead {drift.get('upstream_ahead', 0)}, "
        f"fork behind {drift.get('fork_behind', 0)}, "
        f"fork ahead {drift.get('fork_ahead', 0)}, "
        f"main behind {drift.get('main_behind', 0)}, "
        f"main ahead {drift.get('main_ahead', 0)}"
    )
    auth = summary.get("auth") or {}
    print(
        "Auth backend: "
        f"{auth.get('backend', 'unavailable')} "
        f"(fetch={auth.get('fetch_ready')}, push={auth.get('push_ready')})"
    )
    job = summary.get("job") or {}
    if job.get("installed"):
        print(f"Job: installed ({job.get('state') or 'unknown'}) next={job.get('next_run_at') or 'unknown'}")
    else:
        print("Job: not installed")
    if summary.get("last_error"):
        print(f"Last error: {summary['last_error']}")
    if summary.get("retained_worktree"):
        print(f"Retained worktree: {summary['retained_worktree']}")
    if summary.get("repair_manifest_path"):
        print(f"Repair manifest: {summary['repair_manifest_path']}")
    print(f"Gateway running: {summary.get('gateway_running')}")
    print(f"Telegram connected: {summary.get('telegram_connected')}")


def routing_doctor_command(args) -> None:
    doctor = routing_update_doctor(
        getattr(args, "report_root", "") or None,
        getattr(args, "repo_root", "") or None,
    )
    if getattr(args, "json", False):
        print(json.dumps(doctor, indent=2))
        return

    print(f"Routing update doctor: {doctor['status']}")
    print(f"Message: {doctor['message']}")
    for key, value in doctor.get("checks", {}).items():
        print(f"- {key}: {value}")
    if doctor.get("issues"):
        print("Issues:")
        for item in doctor["issues"]:
            print(f"  - {item}")


def _install_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    install_parser = subparsers.add_parser("install", help="Install or update the routing auto-update cron job")
    install_parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    install_parser.add_argument("--json", action="store_true", help="Emit structured JSON")

    run_parser = subparsers.add_parser("run", help="Run the routing-preserving auto-update flow once")
    run_parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    run_parser.add_argument("--report-root", default="")
    run_parser.add_argument("--json", action="store_true", help="Emit structured JSON")

    status_parser = subparsers.add_parser("status", help="Summarize the latest routing auto-update report")
    status_parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    status_parser.add_argument("--report-root", default="")
    status_parser.add_argument("--json", action="store_true", help="Emit structured JSON")

    doctor_parser = subparsers.add_parser("doctor", help="Check routing auto-update readiness")
    doctor_parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    doctor_parser.add_argument("--report-root", default="")
    doctor_parser.add_argument("--json", action="store_true", help="Emit structured JSON")

    finalize_parser = subparsers.add_parser("finalize", help=argparse.SUPPRESS)
    finalize_parser.add_argument("--repo-root", default=str(PROJECT_ROOT))
    finalize_parser.add_argument("--report-root", default="")
    finalize_parser.add_argument("--json", action="store_true", help="Emit structured JSON")


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

    if args.command == "status":
        routing_status_command(args)
        return 0

    if args.command == "doctor":
        routing_doctor_command(args)
        return 0

    if args.command == "finalize":
        report = finalize_routing_auto_update(args.repo_root, args.report_root or None)
    else:
        report = run_routing_auto_update(args.repo_root, args.report_root or None)
    if args.json:
        print(json.dumps(asdict(report), indent=2))
    else:
        print(_render_markdown_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
