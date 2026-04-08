#!/usr/bin/env python3
"""Structured routed execution tool for routing-layer controlled coding work."""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from agent.redact import redact_sensitive_text
from agent.routing_guard import (
    _classify_routed_failure_kind,
    get_routed_execution_plan,
    get_routing_decision,
    get_session_lane_context,
)
from hermes_constants import get_hermes_home
from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 300
_MAX_OUTPUT_CHARS = 8_000
_MAX_OUTPUT_EXCERPT_CHARS = 600
_WSL_PREFIX_RE = re.compile(r"^\\\\wsl\.localhost\\([^\\]+)\\", re.IGNORECASE)
_ZAI_CODING_BASE_URL = "https://api.z.ai/api/coding/paas/v4"
_BYTES_LITERAL_RE = re.compile(r"""^b(?P<quote>['"]).*(?P=quote)$""", re.DOTALL)
_ROUTE_TIMEOUT_SECONDS: dict[tuple[str, str], int] = {
    ("3A", "high-risk"): 1200,
    ("3B", "marathon"): 900,
    ("3B", "long-context"): 900,
    ("3C", "quick-edit"): 300,
}
_INNER_HERMES_EPHEMERAL_PROMPT = (
    "You are running as the already-routed implementation executor for an outer Hermes session. "
    "Do not re-run the routing-layer classification flow inside this child session. "
    "Implement directly using the available tools, then run verification and report the concrete outcome."
)


def _detect_wsl_unc_prefix() -> Optional[str]:
    current = str(Path(__file__).resolve())
    match = _WSL_PREFIX_RE.match(current)
    if not match:
        return None
    return f"\\\\wsl.localhost\\{match.group(1)}"


def _resolve_host_workdir(workdir: str) -> Optional[str]:
    raw = str(workdir or "").strip()
    if not raw:
        return None

    expanded = os.path.expanduser(raw)
    direct = Path(expanded)
    if direct.is_dir():
        return str(direct)

    if expanded.startswith("/"):
        unc_prefix = _detect_wsl_unc_prefix()
        if unc_prefix:
            candidate = Path(unc_prefix + expanded.replace("/", "\\"))
            if candidate.is_dir():
                return str(candidate)

    return None


def _find_executable(name: str) -> Optional[str]:
    candidates = [name]
    if os.name == "nt":
        candidates.extend([f"{name}.cmd", f"{name}.exe", f"{name}.bat"])
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def _normalize_captured_output(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    if _BYTES_LITERAL_RE.match(raw):
        try:
            literal = ast.literal_eval(raw)
            if isinstance(literal, (bytes, bytearray)):
                return bytes(literal).decode("utf-8", errors="replace")
        except Exception:
            pass
    return raw


def _truncate_output(text: str) -> str:
    clean = redact_sensitive_text(_normalize_captured_output(text))
    if len(clean) <= _MAX_OUTPUT_CHARS:
        return clean
    head_chars = int(_MAX_OUTPUT_CHARS * 0.4)
    tail_chars = _MAX_OUTPUT_CHARS - head_chars
    omitted = len(clean) - head_chars - tail_chars
    return (
        f"{clean[:head_chars]}\n\n"
        f"... [OUTPUT TRUNCATED - {omitted} chars omitted out of {len(clean)} total] ...\n\n"
        f"{clean[-tail_chars:]}"
    )


def _combine_output(stdout: str, stderr: str) -> str:
    if stdout and stderr:
        return f"{stdout.rstrip()}\n{stderr.rstrip()}".strip()
    return (stdout or stderr or "").strip()


def _output_excerpt(text: str) -> str:
    clean = _truncate_output(text)
    if len(clean) <= _MAX_OUTPUT_EXCERPT_CHARS:
        return clean
    return f"{clean[:_MAX_OUTPUT_EXCERPT_CHARS]}…"


def _default_timeout_for_route(decision: dict[str, Any]) -> int:
    tier = str(decision.get("tier", "")).upper()
    path = str(decision.get("path", "") or "").strip().lower()
    return _ROUTE_TIMEOUT_SECONDS.get((tier, path), _DEFAULT_TIMEOUT_SECONDS)


def _write_attempt_output(task_id: str, attempt_index: int, kind: str, output: str) -> Optional[str]:
    clean = redact_sensitive_text(_normalize_captured_output(output))
    if not clean:
        return None
    safe_task = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(task_id or "routed-exec")).strip("-") or "routed-exec"
    safe_kind = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(kind or "attempt")).strip("-") or "attempt"
    out_dir = get_hermes_home() / "cache" / "routed_exec"
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{safe_task}_{int(time.time() * 1000)}_{attempt_index:02d}_{safe_kind}.log"
    path = out_dir / filename
    path.write_text(clean, encoding="utf-8")
    return str(path)


def _summarize_attempts(attempts: list[dict[str, Any]]) -> list[str]:
    summary: list[str] = []
    for index, attempt in enumerate(attempts, start=1):
        executor = str(attempt.get("executor", attempt.get("kind", "attempt")) or "attempt")
        if attempt.get("failed"):
            failure_kind = str(attempt.get("failure_kind") or "failure")
            exit_code = attempt.get("exit_code", -1)
            summary.append(f"{index}. {executor}: failed ({failure_kind}, exit {exit_code})")
        else:
            exit_code = attempt.get("exit_code", 0)
            summary.append(f"{index}. {executor}: succeeded (exit {exit_code})")
    return summary


def _failure_guidance(attempts: list[dict[str, Any]], default_timeout_used: bool, timeout_seconds: int) -> Optional[str]:
    if not attempts or not all(bool(item.get("failed")) for item in attempts):
        return None
    if default_timeout_used and all(str(item.get("failure_kind") or "") == "timeout" for item in attempts):
        return (
            f"All routed attempts timed out after the route default ({timeout_seconds}s per attempt). "
            "Retry with a narrower implementation prompt or a larger explicit `timeout`."
        )
    return None


def _command_preview(kind: str, workdir: str) -> str:
    if kind == "codex_gpt54":
        return (
            f'codex exec --skip-git-repo-check -C {workdir} -s workspace-write '
            '-m gpt-5.4 -c reasoning_effort="extra-high" -'
        )
    if kind == "codex_gpt54mini":
        return (
            f'codex exec --skip-git-repo-check -C {workdir} -s workspace-write '
            '-m gpt-5.4-mini -c reasoning_effort="extra-high" -'
        )
    if kind == "hermes_glm_zai":
        return (
            "GLM_BASE_URL=https://api.z.ai/api/coding/paas/v4 "
            "hermes chat -m glm-5.1 --provider zai -q <prompt> -t terminal,file -Q"
        )
    if kind == "hermes_minimax_m27":
        return "hermes chat -m MiniMax-M2.7 --provider minimax -q <prompt> -t terminal,file -Q"
    if kind == "hermes_nous_mimo_v2_pro":
        return "hermes chat -m xiaomi/mimo-v2-pro --provider nous -q <prompt> -t terminal,file -Q"
    return kind


def _run_codex(*, executable: str, model: str, workdir: str, host_cwd: str, prompt: str, timeout: int) -> dict[str, Any]:
    command = [
        executable,
        "exec",
        "--skip-git-repo-check",
        "-C",
        workdir,
        "-s",
        "workspace-write",
        "-m",
        model,
        "-c",
        'reasoning_effort="extra-high"',
        "-",
    ]
    try:
        result = subprocess.run(
            command,
            input=prompt,
            text=True,
            capture_output=True,
            timeout=timeout,
            cwd=host_cwd,
        )
        output = _truncate_output(_combine_output(result.stdout, result.stderr))
        failure_kind = _classify_routed_failure_kind(output)
        failed = bool(result.returncode != 0 or failure_kind)
        return {
            "kind": "codex_gpt54" if model == "gpt-5.4" else "codex_gpt54mini",
            "executor": f"Codex CLI ({model})",
            "command_preview": _command_preview(
                "codex_gpt54" if model == "gpt-5.4" else "codex_gpt54mini",
                workdir,
            ),
            "output": output,
            "exit_code": int(result.returncode),
            "failed": failed,
            "failure_kind": failure_kind if failed else None,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        output = _truncate_output(_combine_output(exc.stdout or "", exc.stderr or ""))
        return {
            "kind": "codex_gpt54" if model == "gpt-5.4" else "codex_gpt54mini",
            "executor": f"Codex CLI ({model})",
            "command_preview": _command_preview(
                "codex_gpt54" if model == "gpt-5.4" else "codex_gpt54mini",
                workdir,
            ),
            "output": output,
            "exit_code": 124,
            "failed": True,
            "failure_kind": "timeout",
            "timed_out": True,
        }
    except FileNotFoundError:
        return {
            "kind": "codex_gpt54" if model == "gpt-5.4" else "codex_gpt54mini",
            "executor": f"Codex CLI ({model})",
            "command_preview": _command_preview(
                "codex_gpt54" if model == "gpt-5.4" else "codex_gpt54mini",
                workdir,
            ),
            "output": "",
            "exit_code": -1,
            "failed": True,
            "failure_kind": "execution_failure",
            "timed_out": False,
            "error": f"Executable not found: {executable}",
        }
    except Exception as exc:
        return {
            "kind": "codex_gpt54" if model == "gpt-5.4" else "codex_gpt54mini",
            "executor": f"Codex CLI ({model})",
            "command_preview": _command_preview(
                "codex_gpt54" if model == "gpt-5.4" else "codex_gpt54mini",
                workdir,
            ),
            "output": "",
            "exit_code": -1,
            "failed": True,
            "failure_kind": "execution_failure",
            "timed_out": False,
            "error": str(exc),
        }


def _run_hermes(
    *,
    executable: str,
    host_cwd: str,
    prompt: str,
    timeout: int,
    kind: str,
    model: str,
    provider: str,
    env_overrides: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    env.setdefault("HERMES_DISABLE_DEFAULT_ROUTING_SKILL", "1")
    env["HERMES_EPHEMERAL_SYSTEM_PROMPT"] = (
        f"{env.get('HERMES_EPHEMERAL_SYSTEM_PROMPT', '').strip()}\n\n{_INNER_HERMES_EPHEMERAL_PROMPT}"
    ).strip()
    command = [
        executable,
        "chat",
        "-m",
        model,
        "--provider",
        provider,
        "-q",
        prompt,
        "-t",
        "terminal,file",
        "-Q",
    ]
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout,
            cwd=host_cwd,
            env=env,
        )
        output = _truncate_output(_combine_output(result.stdout, result.stderr))
        failure_kind = _classify_routed_failure_kind(output)
        failed = bool(result.returncode != 0 or failure_kind)
        return {
            "kind": kind,
            "executor": {
                "hermes_glm_zai": "Hermes CLI (glm-5.1 via zai)",
                "hermes_minimax_m27": "Hermes CLI (MiniMax-M2.7 via minimax)",
                "hermes_nous_mimo_v2_pro": "Hermes CLI (xiaomi/mimo-v2-pro via nous)",
            }.get(kind, f"Hermes CLI ({model} via {provider})"),
            "command_preview": _command_preview(kind, host_cwd),
            "output": output,
            "exit_code": int(result.returncode),
            "failed": failed,
            "failure_kind": failure_kind if failed else None,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        output = _truncate_output(_combine_output(exc.stdout or "", exc.stderr or ""))
        return {
            "kind": kind,
            "executor": {
                "hermes_glm_zai": "Hermes CLI (glm-5.1 via zai)",
                "hermes_minimax_m27": "Hermes CLI (MiniMax-M2.7 via minimax)",
                "hermes_nous_mimo_v2_pro": "Hermes CLI (xiaomi/mimo-v2-pro via nous)",
            }.get(kind, f"Hermes CLI ({model} via {provider})"),
            "command_preview": _command_preview(kind, host_cwd),
            "output": output,
            "exit_code": 124,
            "failed": True,
            "failure_kind": "timeout",
            "timed_out": True,
        }
    except FileNotFoundError:
        return {
            "kind": kind,
            "executor": {
                "hermes_glm_zai": "Hermes CLI (glm-5.1 via zai)",
                "hermes_minimax_m27": "Hermes CLI (MiniMax-M2.7 via minimax)",
                "hermes_nous_mimo_v2_pro": "Hermes CLI (xiaomi/mimo-v2-pro via nous)",
            }.get(kind, f"Hermes CLI ({model} via {provider})"),
            "command_preview": _command_preview(kind, host_cwd),
            "output": "",
            "exit_code": -1,
            "failed": True,
            "failure_kind": "execution_failure",
            "timed_out": False,
            "error": f"Executable not found: {executable}",
        }
    except Exception as exc:
        return {
            "kind": kind,
            "executor": {
                "hermes_glm_zai": "Hermes CLI (glm-5.1 via zai)",
                "hermes_minimax_m27": "Hermes CLI (MiniMax-M2.7 via minimax)",
                "hermes_nous_mimo_v2_pro": "Hermes CLI (xiaomi/mimo-v2-pro via nous)",
            }.get(kind, f"Hermes CLI ({model} via {provider})"),
            "command_preview": _command_preview(kind, host_cwd),
            "output": "",
            "exit_code": -1,
            "failed": True,
            "failure_kind": "execution_failure",
            "timed_out": False,
            "error": str(exc),
        }


def routed_exec_tool(task: str, workdir: str, timeout: Optional[int] = None, *, task_id: str = "") -> str:
    prompt = str(task or "").strip()
    if not prompt:
        return tool_error("`task` is required for routed_exec.")

    decision = get_routing_decision(task_id)
    if not decision:
        return tool_error("No active routing decision for this task. Emit the routing line first.")

    plan = get_routed_execution_plan(task_id)
    if not plan:
        return tool_error("No routed execution plan is available for the current task.")

    host_cwd = _resolve_host_workdir(workdir)
    if not host_cwd:
        return tool_error(
            f"Could not resolve routed_exec workdir `{workdir}` on this host. "
            "Use an existing absolute path."
        )

    default_timeout = _default_timeout_for_route(decision)
    effective_timeout = int(timeout or default_timeout)
    default_timeout_used = timeout is None
    attempts: list[dict[str, Any]] = []

    codex_executable = _find_executable("codex")
    hermes_executable = _find_executable("hermes")

    for attempt_index, target in enumerate(plan, start=1):
        kind = target["kind"]
        if kind == "hermes_glm_zai":
            attempt = _run_hermes(
                executable=hermes_executable or "hermes",
                host_cwd=host_cwd,
                prompt=prompt,
                timeout=effective_timeout,
                kind="hermes_glm_zai",
                model="glm-5.1",
                provider="zai",
                env_overrides={"GLM_BASE_URL": _ZAI_CODING_BASE_URL},
            )
        elif kind == "hermes_minimax_m27":
            attempt = _run_hermes(
                executable=hermes_executable or "hermes",
                host_cwd=host_cwd,
                prompt=prompt,
                timeout=effective_timeout,
                kind="hermes_minimax_m27",
                model="MiniMax-M2.7",
                provider="minimax",
            )
        elif kind == "hermes_nous_mimo_v2_pro":
            attempt = _run_hermes(
                executable=hermes_executable or "hermes",
                host_cwd=host_cwd,
                prompt=prompt,
                timeout=effective_timeout,
                kind="hermes_nous_mimo_v2_pro",
                model="xiaomi/mimo-v2-pro",
                provider="nous",
            )
        elif kind == "codex_gpt54":
            attempt = _run_codex(
                executable=codex_executable or "codex",
                model="gpt-5.4",
                workdir=workdir,
                host_cwd=host_cwd,
                prompt=prompt,
                timeout=effective_timeout,
            )
        elif kind == "codex_gpt54mini":
            attempt = _run_codex(
                executable=codex_executable or "codex",
                model="gpt-5.4-mini",
                workdir=workdir,
                host_cwd=host_cwd,
                prompt=prompt,
                timeout=effective_timeout,
            )
        else:
            attempt = {
                "kind": kind,
                "executor": target.get("label", kind),
                "command_preview": kind,
                "output": "",
                "exit_code": -1,
                "failed": True,
                "failure_kind": "execution_failure",
                "timed_out": False,
                "error": f"Unsupported routed executor kind: {kind}",
            }
        raw_attempt_output = str(attempt.get("output", "") or "")
        attempt["output_excerpt"] = _output_excerpt(raw_attempt_output)
        attempt["output_path"] = None
        if raw_attempt_output and (attempt.get("failed") or len(raw_attempt_output) > _MAX_OUTPUT_EXCERPT_CHARS):
            attempt["output_path"] = _write_attempt_output(task_id, attempt_index, kind, raw_attempt_output)
        attempts.append(attempt)
        if not attempt.get("failed"):
            break

    final_attempt = attempts[-1] if attempts else None
    success = bool(final_attempt) and not bool(final_attempt.get("failed"))
    attempt_summary = _summarize_attempts(attempts)
    failure_guidance = _failure_guidance(attempts, default_timeout_used, effective_timeout)

    return tool_result(
        {
            "success": success,
            "tier": decision.get("tier"),
            "route_path": decision.get("path"),
            "route_model": decision.get("model"),
            "session_lane": get_session_lane_context(task_id),
            "workdir": workdir,
            "timeout_seconds": effective_timeout,
            "timeout_source": "route-default" if default_timeout_used else "explicit",
            "attempt_summary": attempt_summary,
            "attempts": attempts,
            "output": str(final_attempt.get("output", "") if final_attempt else ""),
            "exit_code": int(final_attempt.get("exit_code", -1) if final_attempt else -1),
            "failure_guidance": None if success else failure_guidance,
            "error": None if success else (
                str(final_attempt.get("error", "")).strip()
                or (
                    f"All routed execution attempts failed for {decision.get('tier')}."
                    if final_attempt
                    else "No routed execution attempts were made."
                )
            ),
        }
    )


def check_routed_exec_requirements() -> bool:
    """Routed execution remains available even if the CLIs are missing.

    The tool surfaces explicit executable-not-found errors at runtime so the
    routing layer can report them cleanly.
    """
    return True


ROUTED_EXEC_SCHEMA = {
    "name": "routed_exec",
        "description": (
            "Execute the active routing-layer coding task through the structured routed executor. "
            "Use this after emitting the routing decision line for any routed Codex/Hermes work. "
            "Do not construct raw `codex exec` or `hermes chat` terminal commands yourself; this tool "
            "selects the correct executor for the active route archetype and handles the defined fallback chain."
        ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "Exact implementation or verification task to execute on the routed model.",
            },
            "workdir": {
                "type": "string",
                "description": "Absolute project working directory for the routed task.",
            },
            "timeout": {
                "type": "integer",
                "description": "Per-attempt timeout in seconds. Defaults to a route-aware value (3B marathon/long-context 900, 3A high-risk 1200, 3C quick-edit 300).",
                "minimum": 1,
            },
        },
        "required": ["task", "workdir"],
    },
}


def _handle_routed_exec(args, **kw):
    return routed_exec_tool(
        task=args.get("task", ""),
        workdir=args.get("workdir", ""),
        timeout=args.get("timeout"),
        task_id=kw.get("task_id", ""),
    )


registry.register(
    name="routed_exec",
    toolset="routing",
    schema=ROUTED_EXEC_SCHEMA,
    handler=_handle_routed_exec,
    check_fn=check_routed_exec_requirements,
    emoji="🧭",
)
