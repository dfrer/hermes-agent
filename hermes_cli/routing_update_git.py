#!/usr/bin/env python3
"""Git backend selection for routing updater maintenance operations."""

from __future__ import annotations

import os
import platform
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence


DEFAULT_WINDOWS_GIT_CANDIDATES: tuple[str, ...] = (
    r"/mnt/c/Program Files/Git/cmd/git.exe",
    r"/mnt/c/Program Files/Git/bin/git.exe",
)


def _is_wsl_runtime() -> bool:
    if platform.system() != "Linux":
        return False
    if os.getenv("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8").lower()
    except OSError:
        return False


def _linux_to_windows_path(path: Path | str) -> str:
    resolved = Path(path).expanduser().resolve()
    raw = str(resolved)
    if platform.system() == "Windows":
        return raw
    if raw.startswith("/mnt/") and len(raw) >= 7 and raw[5].isalpha() and raw[6] == "/":
        drive = raw[5].upper()
        tail = raw[7:].replace("/", "\\")
        return f"{drive}:\\{tail}" if tail else f"{drive}:\\"
    if _is_wsl_runtime():
        distro = os.getenv("WSL_DISTRO_NAME") or "Ubuntu"
        unc_tail = raw.replace("/", "\\")
        return f"\\\\wsl.localhost\\{distro}{unc_tail}"
    return raw


def _candidate_windows_git_paths() -> list[str]:
    candidates: list[str] = []
    for candidate in DEFAULT_WINDOWS_GIT_CANDIDATES:
        try:
            if Path(candidate).exists():
                candidates.append(candidate)
        except OSError:
            continue
    return candidates


@dataclass(frozen=True)
class GitBackend:
    kind: str
    executable: str
    repo_root: Path

    def _convert_path(self, path: Path | str) -> str:
        target = Path(path).expanduser().resolve()
        if self.kind == "windows-bridge":
            return _linux_to_windows_path(target)
        return str(target)

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = False,
        capture_output: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        target_cwd = cwd or self.repo_root
        safe_directory = self._convert_path(self.repo_root)
        target_path = self._convert_path(target_cwd)
        command = [
            self.executable,
            "-c",
            f"safe.directory={safe_directory}",
            "-C",
            target_path,
            *args,
        ]
        merged_env = os.environ.copy()
        merged_env.setdefault("GIT_TERMINAL_PROMPT", "0")
        merged_env.setdefault("GCM_INTERACTIVE", "Never")
        if env:
            merged_env.update(env)
        result = subprocess.run(
            command,
            text=True,
            capture_output=capture_output,
            check=False,
            env=merged_env,
        )
        if check and result.returncode != 0:
            stderr = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(stderr or f"git command failed ({result.returncode})")
        return result


@dataclass
class GitBackendProbe:
    backend: str
    fetch_ready: bool
    push_ready: bool
    push_targets: dict[str, bool] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    details: dict[str, dict[str, str | bool]] = field(default_factory=dict)


def _probe_backend(
    backend: GitBackend,
    *,
    upstream_remote: str,
    push_remote: str,
    live_branch: str,
    promotion_branch: str,
    live_source_refs: Sequence[str] | None = None,
    promotion_source_refs: Sequence[str] | None = None,
) -> tuple[bool, bool, dict[str, bool], dict[str, str]]:
    errors: dict[str, str] = {}
    fetch_ready = True
    for remote in (upstream_remote, push_remote):
        result = backend.run(["ls-remote", "--heads", remote], check=False)
        if result.returncode != 0:
            fetch_ready = False
            errors[f"fetch:{remote}"] = (result.stderr or result.stdout or "").strip() or "unknown error"

    push_targets = {
        live_branch: False,
        promotion_branch: False,
    }

    def _resolve_push_source(refs: Sequence[str] | None, branch_name: str) -> str:
        candidates = tuple(
            ref
            for ref in (
                *(refs or ()),
                "HEAD",
                f"refs/remotes/{push_remote}/{branch_name}",
                f"refs/heads/{branch_name}",
            )
            if ref
        )
        for ref in candidates:
            result = backend.run(["rev-parse", "--verify", f"{ref}^{{commit}}"], check=False)
            if result.returncode == 0:
                return (result.stdout or "").strip() or ref
        return "HEAD"

    live_source = _resolve_push_source(live_source_refs, live_branch)
    live_result = backend.run(
        ["push", "--porcelain", "--dry-run", push_remote, f"{live_source}:refs/heads/{live_branch}"],
        check=False,
    )
    if live_result.returncode == 0:
        push_targets[live_branch] = True
    else:
        errors[f"push:{live_branch}"] = (live_result.stderr or live_result.stdout or "").strip() or "unknown error"

    main_source = _resolve_push_source(promotion_source_refs, promotion_branch)
    main_result = backend.run(
        ["push", "--porcelain", "--dry-run", push_remote, f"{main_source}:refs/heads/{promotion_branch}"],
        check=False,
    )
    if main_result.returncode == 0:
        push_targets[promotion_branch] = True
    else:
        errors[f"push:{promotion_branch}"] = (main_result.stderr or main_result.stdout or "").strip() or "unknown error"

    return fetch_ready, all(push_targets.values()), push_targets, errors


def _candidate_score(fetch_ready: bool, push_targets: dict[str, bool], kind: str) -> tuple[int, int, int]:
    return (
        1 if fetch_ready else 0,
        sum(1 for allowed in push_targets.values() if allowed),
        1 if kind == "native" else 0,
    )


def probe_git_backend(
    repo_root: Path | str,
    *,
    upstream_remote: str,
    push_remote: str,
    live_branch: str,
    promotion_branch: str = "main",
    live_source_refs: Sequence[str] | None = None,
    promotion_source_refs: Sequence[str] | None = None,
) -> GitBackendProbe:
    repo_root = Path(repo_root).expanduser().resolve()
    details: dict[str, dict[str, str | bool]] = {}

    native = GitBackend(kind="native", executable="git", repo_root=repo_root)
    native_fetch_ready, native_push_ready, native_targets, native_errors = _probe_backend(
        native,
        upstream_remote=upstream_remote,
        push_remote=push_remote,
        live_branch=live_branch,
        promotion_branch=promotion_branch,
        live_source_refs=live_source_refs,
        promotion_source_refs=promotion_source_refs,
    )
    details["native"] = {
        "available": True,
        "fetch_ready": native_fetch_ready,
        "push_ready": native_push_ready,
        "errors": "; ".join(f"{key}={value}" for key, value in native_errors.items()),
    }
    candidates: list[tuple[str, bool, bool, dict[str, bool], dict[str, str]]] = [
        ("native", native_fetch_ready, native_push_ready, native_targets, native_errors)
    ]

    windows_candidates = _candidate_windows_git_paths()
    if _is_wsl_runtime() and windows_candidates:
        windows = GitBackend(kind="windows-bridge", executable=windows_candidates[0], repo_root=repo_root)
        bridge_fetch_ready, bridge_push_ready, bridge_targets, bridge_errors = _probe_backend(
            windows,
            upstream_remote=upstream_remote,
            push_remote=push_remote,
            live_branch=live_branch,
            promotion_branch=promotion_branch,
            live_source_refs=live_source_refs,
            promotion_source_refs=promotion_source_refs,
        )
        details["windows-bridge"] = {
            "available": True,
            "fetch_ready": bridge_fetch_ready,
            "push_ready": bridge_push_ready,
            "errors": "; ".join(f"{key}={value}" for key, value in bridge_errors.items()),
            "executable": windows.executable,
        }
        candidates.append(("windows-bridge", bridge_fetch_ready, bridge_push_ready, bridge_targets, bridge_errors))

    if "windows-bridge" not in details:
        details["windows-bridge"] = {
            "available": bool(windows_candidates),
            "fetch_ready": False,
            "push_ready": False,
            "errors": "not running in WSL or git.exe not found",
        }
    kind, fetch_ready, push_ready, push_targets, errors = max(
        candidates,
        key=lambda item: _candidate_score(item[1], item[3], item[0]),
    )
    if not fetch_ready:
        kind = "unavailable"
    return GitBackendProbe(
        backend=kind,
        fetch_ready=fetch_ready,
        push_ready=push_ready,
        push_targets=push_targets,
        errors=errors,
        details=details,
    )


def select_git_backend(
    repo_root: Path | str,
    *,
    upstream_remote: str,
    push_remote: str,
    live_branch: str,
    promotion_branch: str = "main",
    live_source_refs: Sequence[str] | None = None,
    promotion_source_refs: Sequence[str] | None = None,
) -> tuple[GitBackend | None, GitBackendProbe]:
    repo_root = Path(repo_root).expanduser().resolve()
    probe = probe_git_backend(
        repo_root,
        upstream_remote=upstream_remote,
        push_remote=push_remote,
        live_branch=live_branch,
        promotion_branch=promotion_branch,
        live_source_refs=live_source_refs,
        promotion_source_refs=promotion_source_refs,
    )
    if probe.backend == "native":
        return GitBackend(kind="native", executable="git", repo_root=repo_root), probe
    if probe.backend == "windows-bridge":
        candidates = _candidate_windows_git_paths()
        if candidates:
            return GitBackend(kind="windows-bridge", executable=candidates[0], repo_root=repo_root), probe
    return None, probe
