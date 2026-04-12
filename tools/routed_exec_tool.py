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
import sys
import time
from pathlib import Path
from typing import Any, Optional

from agent.entitlements import build_effective_route_plan
from agent.redact import redact_sensitive_text
from agent.routing_guard import (
    _classify_routed_failure_kind,
    record_custom_system_issue,
    get_ability_handoff,
    get_routed_execution_plan,
    get_routing_decision,
    get_selected_route,
    get_session_lane_context,
    has_task_entitlement_approval,
    record_task_entitlement_approval,
    update_selected_route_entitlement,
)
from agent.routing_policy import load_routing_policy
from hermes_cli.auth import resolve_api_key_provider_credentials
from hermes_constants import get_hermes_home
from tools.approval import request_blocking_approval
from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 300
_MAX_OUTPUT_CHARS = 8_000
_MAX_OUTPUT_EXCERPT_CHARS = 600
_WSL_PREFIX_RE = re.compile(r"^\\\\wsl\.localhost\\([^\\]+)\\", re.IGNORECASE)
_BYTES_LITERAL_RE = re.compile(r"""^b(?P<quote>['"]).*(?P=quote)$""", re.DOTALL)
_ROUTED_RESULT_MARKER = "HERMES_ROUTED_RESULT:"
_ROUTED_RESULT_RE = re.compile(rf"(?m)^\s*{re.escape(_ROUTED_RESULT_MARKER)}\s*(?P<payload>\{{.*\}})\s*$")
_EXECUTOR_SHUTDOWN_RE = re.compile(r"(?is)_enter_buffered_busy|fatal python error.*interpreter shutdown")
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


def _candidate_host_paths(workdir: str) -> list[Path]:
    expanded = os.path.expanduser(str(workdir or "").strip())
    if not expanded:
        return []

    candidates = [Path(expanded)]
    if expanded.startswith("/"):
        unc_prefix = _detect_wsl_unc_prefix()
        if unc_prefix:
            candidates.append(Path(unc_prefix + expanded.replace("/", "\\")))
    return candidates


def _nearest_existing_dir(path: Path) -> Optional[Path]:
    current = path
    while True:
        if current.is_dir():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def _resolve_host_workdir(workdir: str) -> Optional[dict[str, str]]:
    raw = str(workdir or "").strip()
    if not raw:
        return None

    for candidate in _candidate_host_paths(raw):
        existing = _nearest_existing_dir(candidate)
        if not existing:
            continue
        return {
            "requested_workdir": os.path.expanduser(raw),
            "resolved_workdir": str(existing),
            "target_exists": "1" if candidate.is_dir() else "",
        }

    return None


def _find_executable(name: str) -> Optional[str]:
    candidates = [name]
    if os.name == "nt":
        candidates.extend([f"{name}.cmd", f"{name}.exe", f"{name}.bat"])
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    local_candidates: list[Path] = []
    executable_dir = Path(sys.executable).resolve().parent
    local_candidates.append(executable_dir / name)
    if os.name == "nt":
        local_candidates.extend(
            [
                executable_dir / f"{name}.cmd",
                executable_dir / f"{name}.exe",
                executable_dir / f"{name}.bat",
            ]
        )
    repo_root = Path(__file__).resolve().parents[1]
    local_candidates.extend(
        [
            repo_root / "venv" / "bin" / name,
            repo_root / "venv" / "Scripts" / name,
            repo_root / "venv" / "Scripts" / f"{name}.cmd",
            repo_root / "venv" / "Scripts" / f"{name}.exe",
        ]
    )
    for candidate in local_candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
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
    profile = load_routing_policy().profile(tier, path)
    if profile is not None:
        return int(profile.default_timeout)
    return _ROUTE_TIMEOUT_SECONDS.get((tier, path), _DEFAULT_TIMEOUT_SECONDS)


def _get_cli_approval_callback():
    try:
        from tools.terminal_tool import get_approval_callback

        return get_approval_callback()
    except Exception:
        return None


def _resolve_effective_route_plan(
    task_id: str,
    decision: dict[str, Any],
    plan: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    effective = build_effective_route_plan(
        task_id,
        decision,
        plan,
        has_task_approval=has_task_entitlement_approval,
    )
    if effective.approval_required and effective.approval_key:
        route_label = f"{decision.get('tier')}/{decision.get('path')}"
        command = (
            f"routed_exec entitlement approval for {route_label}: "
            f"{effective.approval_kind or 'route'}"
        )
        description = (
            f"{effective.approval_description} This approval applies only to the current task."
        )
        choice = request_blocking_approval(
            command,
            description,
            session_key="",
            approval_callback=_get_cli_approval_callback(),
            allow_permanent=False,
        )
        if choice != "deny":
            record_task_entitlement_approval(task_id, effective.approval_key)
            effective = build_effective_route_plan(
                task_id,
                decision,
                plan,
                has_task_approval=has_task_entitlement_approval,
            )
        else:
            update_selected_route_entitlement(
                task_id,
                entitlement=effective.to_metadata(),
                effective_targets=[],
                degraded=effective.degraded,
                failure_reason=effective.failure_reason or "approval_required",
            )
            return [], effective.to_metadata(), (
                "Entitlement approval denied for this task. "
                "The routed executor will not spend locked quota or downgrade without approval."
            )

    metadata = effective.to_metadata()
    update_selected_route_entitlement(
        task_id,
        entitlement=metadata,
        effective_targets=list(effective.route_targets),
        degraded=effective.degraded,
        failure_reason=effective.failure_reason,
    )
    return [dict(item) for item in effective.route_targets], metadata, ""


def _compact_explicit_evidence(evidence: str) -> str:
    clean = redact_sensitive_text(str(evidence or "").strip())
    if len(clean) <= 3000:
        return clean
    return f"{clean[:3000]}\n[explicit evidence truncated {len(clean) - 3000} chars]"


def _build_routed_prompt(
    task: str,
    *,
    requested_workdir: str,
    resolved_workdir: str,
    ability_evidence: str = "",
    explicit_evidence: str = "",
) -> str:
    requested = str(requested_workdir or "").strip() or resolved_workdir
    resolved = str(resolved_workdir or "").strip() or requested
    evidence_sections: list[str] = []
    if ability_evidence:
        evidence_sections.append(f"Stored ability evidence packet:\n{ability_evidence}")
    if explicit_evidence:
        evidence_sections.append(f"Caller-provided evidence notes:\n{_compact_explicit_evidence(explicit_evidence)}")
    evidence_text = ""
    if evidence_sections:
        evidence_text = "\nAbility evidence handoff:\n" + "\n\n".join(evidence_sections) + "\n"
    return (
        f"{str(task or '').strip()}\n\n"
        "Execution context:\n"
        f"- Requested target workdir: {requested}\n"
        f"- Existing launch workdir: {resolved}\n"
        "- If the requested target workdir does not exist yet, create it first and then work there.\n\n"
        f"{evidence_text}"
        "Completion contract:\n"
        f"- Emit exactly one final line beginning with `{_ROUTED_RESULT_MARKER}` followed by compact JSON.\n"
        '- JSON schema: {"status":"success|failed|timeout","summary":"...","verification":"...","warnings":["..."]}\n'
        "- Set `status` to `success` only if the requested task is actually complete.\n"
        "- Do not wrap the final JSON line in Markdown fences.\n"
    )


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
            if attempt.get("warning_kinds"):
                warnings = ", ".join(str(item) for item in attempt.get("warning_kinds", []))
                summary.append(f"{index}. {executor}: succeeded with warnings ({warnings}, exit {exit_code})")
            else:
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


def _extract_structured_routed_result(output: str) -> Optional[dict[str, Any]]:
    matches = list(_ROUTED_RESULT_RE.finditer(str(output or "")))
    if not matches:
        return None
    try:
        payload = json.loads(matches[-1].group("payload"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    status = str(payload.get("status", "") or "").strip().lower()
    if status not in {"success", "failed", "timeout"}:
        return None
    warnings = payload.get("warnings")
    normalized_warnings = [str(item).strip() for item in warnings] if isinstance(warnings, list) else []
    return {
        "status": status,
        "summary": str(payload.get("summary", "") or "").strip(),
        "verification": str(payload.get("verification", "") or "").strip(),
        "warnings": [item for item in normalized_warnings if item],
        "failure_kind": str(payload.get("failure_kind", "") or "").strip(),
    }


def _classify_warning_kinds(output: str, *, structured_status: Optional[str]) -> list[str]:
    warning_kinds: list[str] = []
    classified = _classify_routed_failure_kind(output)
    if structured_status == "success" and classified:
        warning_kinds.append(classified)
    if structured_status == "success" and _EXECUTOR_SHUTDOWN_RE.search(str(output or "")):
        warning_kinds.append("executor_shutdown_after_success")
    seen: set[str] = set()
    deduped: list[str] = []
    for item in warning_kinds:
        if item and item not in seen:
            deduped.append(item)
            seen.add(item)
    return deduped


def _finalize_attempt(
    *,
    kind: str,
    executor: str,
    command_preview: str,
    raw_output: str,
    exit_code: int,
    timed_out: bool,
    error: str = "",
    resolved_base_url: str = "",
    endpoint_source: str = "",
    endpoint_id: str = "",
    endpoint_label: str = "",
) -> dict[str, Any]:
    structured = _extract_structured_routed_result(raw_output)
    warning_kinds = _classify_warning_kinds(raw_output, structured_status=structured.get("status") if structured else None)
    output = _truncate_output(raw_output)

    status = "failed"
    failed = True
    failure_kind = ""
    if structured and str(structured.get("status", "") or "") == "success":
        status = "success"
        failed = False
        if timed_out:
            warning_kinds.append("timeout_after_success")
    elif timed_out:
        status = "timeout"
        failure_kind = "timeout"
    elif structured:
        status = str(structured.get("status", "") or "failed")
        failed = status != "success"
        if failed:
            failure_kind = str(structured.get("failure_kind", "") or _classify_routed_failure_kind(raw_output) or "execution_failure")
    elif error:
        status = "failed"
        failure_kind = "execution_failure"
    elif int(exit_code or 0) != 0:
        status = "failed"
        failure_kind = str(_classify_routed_failure_kind(raw_output) or "execution_failure")
    else:
        status = "success"
        failed = False

    if status == "success":
        failed = False
        failure_kind = ""

    attempt = {
        "kind": kind,
        "executor": executor,
        "command_preview": command_preview,
        "output": output,
        "exit_code": int(exit_code),
        "failed": failed,
        "failure_kind": failure_kind if failed else None,
        "timed_out": timed_out,
        "status": status,
        "summary": str(structured.get("summary", "") if structured else ""),
        "verification": str(structured.get("verification", "") if structured else ""),
        "warning_kinds": warning_kinds,
    }
    if error:
        attempt["error"] = error
    if structured and structured.get("warnings"):
        attempt["warnings"] = list(structured["warnings"])
    if resolved_base_url:
        attempt["resolved_base_url"] = resolved_base_url
    if endpoint_source:
        attempt["endpoint_source"] = endpoint_source
    if endpoint_id:
        attempt["endpoint_id"] = endpoint_id
    if endpoint_label:
        attempt["endpoint_label"] = endpoint_label
    return attempt


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
        return "hermes chat -m glm-5.1 --provider zai -q <prompt> -t terminal,file -Q"
    if kind == "hermes_minimax_m27":
        return "hermes chat -m MiniMax-M2.7 --provider minimax -q <prompt> -t terminal,file -Q"
    if kind == "hermes_nous_mimo_v2_pro":
        return "hermes chat -m xiaomi/mimo-v2-pro --provider nous -q <prompt> -t terminal,file -Q"
    return kind


def _run_codex(
    *,
    executable: str,
    model: str,
    workdir: str,
    host_cwd: str,
    prompt: str,
    timeout: int,
    kind: str = "",
    label: str = "",
) -> dict[str, Any]:
    target_kind = kind or ("codex_gpt54" if model == "gpt-5.4" else "codex_gpt54mini")
    executor_label = label or f"Codex CLI ({model})"
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
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            cwd=host_cwd,
        )
        return _finalize_attempt(
            kind=target_kind,
            executor=executor_label,
            command_preview=_command_preview(target_kind, workdir),
            raw_output=_combine_output(result.stdout, result.stderr),
            exit_code=int(result.returncode),
            timed_out=False,
        )
    except subprocess.TimeoutExpired as exc:
        return _finalize_attempt(
            kind=target_kind,
            executor=executor_label,
            command_preview=_command_preview(target_kind, workdir),
            raw_output=_combine_output(exc.stdout or "", exc.stderr or ""),
            exit_code=124,
            timed_out=True,
        )
    except FileNotFoundError:
        return _finalize_attempt(
            kind=target_kind,
            executor=executor_label,
            command_preview=_command_preview(target_kind, workdir),
            raw_output="",
            exit_code=-1,
            timed_out=False,
            error=f"Executable not found: {executable}",
        )
    except Exception as exc:
        return _finalize_attempt(
            kind=target_kind,
            executor=executor_label,
            command_preview=_command_preview(target_kind, workdir),
            raw_output="",
            exit_code=-1,
            timed_out=False,
            error=str(exc),
        )


def _run_hermes(
    *,
    executable: str,
    host_cwd: str,
    prompt: str,
    timeout: int,
    kind: str,
    model: str,
    provider: str,
    label: str = "",
) -> dict[str, Any]:
    env = os.environ.copy()
    env.setdefault("HERMES_DISABLE_DEFAULT_ROUTING_SKILL", "1")
    env["TERMINAL_CWD"] = host_cwd
    env["HERMES_EPHEMERAL_SYSTEM_PROMPT"] = (
        f"{env.get('HERMES_EPHEMERAL_SYSTEM_PROMPT', '').strip()}\n\n{_INNER_HERMES_EPHEMERAL_PROMPT}"
    ).strip()
    resolved_base_url = ""
    endpoint_source = ""
    endpoint_id = ""
    endpoint_label = ""
    if provider == "zai":
        try:
            creds = resolve_api_key_provider_credentials("zai")
            resolved_base_url = str(creds.get("base_url", "") or "")
            endpoint_source = str(creds.get("endpoint_source", "") or "")
            endpoint_id = str(creds.get("endpoint_id", "") or "")
            endpoint_label = str(creds.get("endpoint_label", "") or "")
        except Exception:
            logger.debug("Failed to resolve Z.AI endpoint metadata for routed exec", exc_info=True)
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
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            cwd=host_cwd,
            env=env,
        )
        return _finalize_attempt(
            kind=kind,
            executor=label or {
                "hermes_glm_zai": "Hermes CLI (glm-5.1 via zai)",
                "hermes_minimax_m27": "Hermes CLI (MiniMax-M2.7 via minimax)",
                "hermes_nous_mimo_v2_pro": "Hermes CLI (xiaomi/mimo-v2-pro via nous)",
            }.get(kind, f"Hermes CLI ({model} via {provider})"),
            command_preview=_command_preview(kind, host_cwd),
            raw_output=_combine_output(result.stdout, result.stderr),
            exit_code=int(result.returncode),
            timed_out=False,
            resolved_base_url=resolved_base_url,
            endpoint_source=endpoint_source,
            endpoint_id=endpoint_id,
            endpoint_label=endpoint_label,
        )
    except subprocess.TimeoutExpired as exc:
        return _finalize_attempt(
            kind=kind,
            executor=label or {
                "hermes_glm_zai": "Hermes CLI (glm-5.1 via zai)",
                "hermes_minimax_m27": "Hermes CLI (MiniMax-M2.7 via minimax)",
                "hermes_nous_mimo_v2_pro": "Hermes CLI (xiaomi/mimo-v2-pro via nous)",
            }.get(kind, f"Hermes CLI ({model} via {provider})"),
            command_preview=_command_preview(kind, host_cwd),
            raw_output=_combine_output(exc.stdout or "", exc.stderr or ""),
            exit_code=124,
            timed_out=True,
            resolved_base_url=resolved_base_url,
            endpoint_source=endpoint_source,
            endpoint_id=endpoint_id,
            endpoint_label=endpoint_label,
        )
    except FileNotFoundError:
        return _finalize_attempt(
            kind=kind,
            executor=label or {
                "hermes_glm_zai": "Hermes CLI (glm-5.1 via zai)",
                "hermes_minimax_m27": "Hermes CLI (MiniMax-M2.7 via minimax)",
                "hermes_nous_mimo_v2_pro": "Hermes CLI (xiaomi/mimo-v2-pro via nous)",
            }.get(kind, f"Hermes CLI ({model} via {provider})"),
            command_preview=_command_preview(kind, host_cwd),
            raw_output="",
            exit_code=-1,
            timed_out=False,
            error=f"Executable not found: {executable}",
            resolved_base_url=resolved_base_url,
            endpoint_source=endpoint_source,
            endpoint_id=endpoint_id,
            endpoint_label=endpoint_label,
        )
    except Exception as exc:
        return _finalize_attempt(
            kind=kind,
            executor=label or {
                "hermes_glm_zai": "Hermes CLI (glm-5.1 via zai)",
                "hermes_minimax_m27": "Hermes CLI (MiniMax-M2.7 via minimax)",
                "hermes_nous_mimo_v2_pro": "Hermes CLI (xiaomi/mimo-v2-pro via nous)",
            }.get(kind, f"Hermes CLI ({model} via {provider})"),
            command_preview=_command_preview(kind, host_cwd),
            raw_output="",
            exit_code=-1,
            timed_out=False,
            error=str(exc),
            resolved_base_url=resolved_base_url,
            endpoint_source=endpoint_source,
            endpoint_id=endpoint_id,
            endpoint_label=endpoint_label,
        )


def execute_routed_context(
    task: str,
    workdir: str,
    *,
    decision: dict[str, Any],
    route_targets: list[dict[str, Any]],
    selected_route: Optional[dict[str, Any]] = None,
    session_lane: Optional[dict[str, str]] = None,
    task_id: str = "",
    timeout: Optional[int] = None,
    evidence: str = "",
    ability_evidence: str = "",
) -> dict[str, Any]:
    prompt = str(task or "").strip()
    if not prompt:
        return {"success": False, "error": "`task` is required for routed_exec."}

    plan = [dict(item) for item in route_targets if isinstance(item, dict)]
    if not plan:
        return {"success": False, "error": "No routed execution plan is available for the current task."}
    effective_plan, entitlement_metadata, entitlement_error = _resolve_effective_route_plan(
        task_id,
        decision,
        plan,
    )
    if entitlement_error:
        if task_id:
            record_custom_system_issue(
                task_id,
                component="routed_exec",
                code="entitlement_blocked",
                summary="Routed execution was blocked by entitlement policy before any executor was launched.",
                detail=str(entitlement_metadata.get("failure_reason") or "approval_required"),
                severity="warning",
            )
        selected_route = get_selected_route(task_id) if task_id else dict(selected_route or {})
        return {
            "success": False,
            "tier": decision.get("tier"),
            "route_path": decision.get("path"),
            "route_model": decision.get("model"),
            "selected_route": dict(selected_route or {}),
            "session_lane": dict(session_lane or {}),
            "workdir": workdir,
            "requested_workdir": workdir,
            "resolved_workdir": "",
            "timeout_seconds": int(timeout or _default_timeout_for_route(decision)),
            "timeout_source": "route-default" if timeout is None else "explicit",
            "executors_attempted": [],
            "attempt_summary": "",
            "attempts": [],
            "summary": "",
            "verification": "",
            "warnings": [],
            "output_excerpt": "",
            "output_path": "",
            "failure_kind": "",
            "failure_reason": str(entitlement_metadata.get("failure_reason") or "approval_required"),
            "verification_expectations": (
                "Child executor must report concrete verification in the final "
                f"{_ROUTED_RESULT_MARKER} JSON line."
            ),
            "ability_evidence_included": bool(str(ability_evidence or "").strip() or str(evidence or "").strip()),
            "fallback_exhausted": False,
            "output": "",
            "exit_code": -1,
            "status": "blocked",
            "warning_kinds": [],
            "resolved_base_url": "",
            "endpoint_source": "",
            "error": entitlement_error,
        }
    plan = effective_plan
    if not plan:
        if task_id:
            record_custom_system_issue(
                task_id,
                component="routed_exec",
                code="no_entitlement_approved_target",
                summary="Routed execution had no entitlement-approved route target to run.",
                detail=str(entitlement_metadata.get("failure_reason") or "locked_paid_spend"),
                severity="warning",
            )
        selected_route = get_selected_route(task_id) if task_id else dict(selected_route or {})
        failure_reason = str(entitlement_metadata.get("failure_reason") or "locked_paid_spend")
        return {
            "success": False,
            "tier": decision.get("tier"),
            "route_path": decision.get("path"),
            "route_model": decision.get("model"),
            "selected_route": dict(selected_route or {}),
            "session_lane": dict(session_lane or {}),
            "workdir": workdir,
            "requested_workdir": workdir,
            "resolved_workdir": "",
            "timeout_seconds": int(timeout or _default_timeout_for_route(decision)),
            "timeout_source": "route-default" if timeout is None else "explicit",
            "executors_attempted": [],
            "attempt_summary": "",
            "attempts": [],
            "summary": "",
            "verification": "",
            "warnings": [],
            "output_excerpt": "",
            "output_path": "",
            "failure_kind": "",
            "failure_reason": failure_reason,
            "verification_expectations": (
                "Child executor must report concrete verification in the final "
                f"{_ROUTED_RESULT_MARKER} JSON line."
            ),
            "ability_evidence_included": bool(str(ability_evidence or "").strip() or str(evidence or "").strip()),
            "fallback_exhausted": False,
            "output": "",
            "exit_code": -1,
            "status": "blocked",
            "warning_kinds": [],
            "resolved_base_url": "",
            "endpoint_source": "",
            "error": f"No entitlement-approved route target is available ({failure_reason}).",
        }

    workdir_info = _resolve_host_workdir(workdir)
    if not workdir_info:
        if task_id:
            record_custom_system_issue(
                task_id,
                component="routed_exec",
                code="workdir_resolution_failed",
                summary="Routed execution could not resolve the requested workdir on the current host.",
                detail=str(workdir or ""),
                severity="warning",
            )
        return {
            "success": False,
            "error": (
                f"Could not resolve routed_exec workdir `{workdir}` on this host. "
                "Use an absolute path whose parent directory already exists."
            ),
        }
    requested_workdir = str(workdir_info.get("requested_workdir", workdir) or workdir)
    resolved_workdir = str(workdir_info.get("resolved_workdir", "") or "")
    routed_prompt = _build_routed_prompt(
        prompt,
        requested_workdir=requested_workdir,
        resolved_workdir=resolved_workdir,
        ability_evidence=ability_evidence,
        explicit_evidence=str(evidence or ""),
    )

    default_timeout = _default_timeout_for_route(decision)
    effective_timeout = int(timeout or default_timeout)
    default_timeout_used = timeout is None
    attempts: list[dict[str, Any]] = []

    codex_executable = _find_executable("codex")
    hermes_executable = _find_executable("hermes")

    for attempt_index, target in enumerate(plan, start=1):
        kind = str(target.get("kind", "") or "")
        executor = str(target.get("executor", "") or "")
        model = str(target.get("model", "") or "")
        provider = str(target.get("provider", "") or "")
        label = str(target.get("label", "") or "")
        if executor == "hermes":
            attempt = _run_hermes(
                executable=hermes_executable or "hermes",
                host_cwd=resolved_workdir,
                prompt=routed_prompt,
                timeout=effective_timeout,
                kind=kind,
                model=model,
                provider=provider,
                label=label,
            )
        elif executor == "codex":
            attempt = _run_codex(
                executable=codex_executable or "codex",
                model=model,
                workdir=resolved_workdir,
                host_cwd=resolved_workdir,
                prompt=routed_prompt,
                timeout=effective_timeout,
                kind=kind,
                label=label,
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
    selected_route = get_selected_route(task_id) if task_id else dict(selected_route or {})
    success = bool(final_attempt) and not bool(final_attempt.get("failed"))
    attempt_summary = _summarize_attempts(attempts)
    failure_guidance = _failure_guidance(attempts, default_timeout_used, effective_timeout)
    fallback_exhausted = (not success) and bool(attempts) and len(attempts) >= len(plan)

    warnings: list[str] = []
    if final_attempt:
        warnings.extend(str(item) for item in final_attempt.get("warning_kinds", []) if str(item or "").strip())
        warnings.extend(str(item) for item in final_attempt.get("warnings", []) if str(item or "").strip())

    final_output_path = final_attempt.get("output_path") if final_attempt else ""
    final_failure_kind = final_attempt.get("failure_kind") if final_attempt else ""
    failure_reason = str(entitlement_metadata.get("failure_reason") or "")
    if task_id and not success:
        record_custom_system_issue(
            task_id,
            component="routed_exec",
            code="execution_failed",
            summary=(
                f"All routed execution attempts failed for "
                f"{decision.get('tier')}/{decision.get('path')}."
            ),
            detail=(
                failure_reason
                or str(final_failure_kind or "")
                or ", ".join(str(item.get("kind") or "") for item in attempts if isinstance(item, dict))
            ),
            severity="warning",
        )
    return {
        "success": success,
        "tier": decision.get("tier"),
        "route_path": decision.get("path"),
        "route_model": decision.get("model"),
        "selected_route": dict(selected_route or {}),
        "session_lane": dict(session_lane or {}),
        "workdir": requested_workdir,
        "requested_workdir": requested_workdir,
        "resolved_workdir": resolved_workdir,
        "timeout_seconds": effective_timeout,
        "timeout_source": "route-default" if default_timeout_used else "explicit",
        "executors_attempted": [str(item.get("executor") or item.get("kind") or "") for item in attempts],
        "attempt_summary": attempt_summary,
        "attempts": attempts,
        "summary": str(final_attempt.get("summary", "") if final_attempt else ""),
        "verification": str(final_attempt.get("verification", "") if final_attempt else ""),
        "warnings": warnings,
        "output_excerpt": str(final_attempt.get("output_excerpt", "") if final_attempt else ""),
        "output_path": str(final_output_path or ""),
        "failure_kind": str(final_failure_kind or ""),
        "failure_reason": failure_reason or str(final_failure_kind or ""),
        "verification_expectations": (
            "Child executor must report concrete verification in the final "
            f"{_ROUTED_RESULT_MARKER} JSON line."
        ),
        "ability_evidence_included": bool(str(ability_evidence or "").strip() or str(evidence or "").strip()),
        "fallback_exhausted": fallback_exhausted,
        "output": str(final_attempt.get("output", "") if final_attempt else ""),
        "exit_code": int(final_attempt.get("exit_code", -1) if final_attempt else -1),
        "status": str(final_attempt.get("status", "failed") if final_attempt else "failed"),
        "warning_kinds": list(final_attempt.get("warning_kinds", []) if final_attempt else []),
        "resolved_base_url": str(final_attempt.get("resolved_base_url", "") if final_attempt else ""),
        "endpoint_source": str(final_attempt.get("endpoint_source", "") if final_attempt else ""),
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


def routed_exec_tool(
    task: str,
    workdir: str,
    timeout: Optional[int] = None,
    *,
    task_id: str = "",
    evidence: str = "",
) -> str:
    prompt = str(task or "").strip()
    if not prompt:
        return tool_error("`task` is required for routed_exec.")

    decision = get_routing_decision(task_id)
    if not decision:
        if task_id:
            record_custom_system_issue(
                task_id,
                component="routed_exec",
                code="missing_routing_decision",
                summary="routed_exec was called before an active routing decision was recorded for the task.",
                severity="warning",
            )
        return tool_error("No active routing decision for this task. Emit the routing line first.")

    plan = get_routed_execution_plan(task_id)
    if not plan:
        record_custom_system_issue(
            task_id,
            component="routed_exec",
            code="missing_execution_plan",
            summary="routed_exec was called but no routed execution plan was available for the current task.",
            severity="warning",
        )
        return tool_error("No routed execution plan is available for the current task.")

    result = execute_routed_context(
        prompt,
        workdir,
        decision=decision,
        route_targets=plan,
        selected_route=get_selected_route(task_id),
        session_lane=get_session_lane_context(task_id),
        task_id=task_id,
        timeout=timeout,
        evidence=evidence,
        ability_evidence=get_ability_handoff(task_id),
    )
    if not result.get("success") and result.get("error") and not result.get("attempts"):
        return tool_error(str(result["error"]))
    return tool_result(result)


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
                "description": "Absolute project working directory or target directory for the routed task. If the target does not exist yet, routed_exec will launch from the nearest existing parent and instruct the child executor to create it.",
            },
            "timeout": {
                "type": "integer",
                "description": "Per-attempt timeout in seconds. Defaults to a route-aware value (3B marathon/long-context 900, 3A high-risk 1200, 3C quick-edit 300).",
                "minimum": 1,
            },
            "evidence": {
                "type": "string",
                "description": "Optional compact caller-provided evidence notes to include in the routed child prompt. Do not inline full logs or screenshots.",
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
        evidence=args.get("evidence", ""),
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
