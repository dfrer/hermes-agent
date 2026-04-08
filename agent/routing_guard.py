from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Optional


DEFAULT_ROUTING_SKILL = "routing-layer"

_TASK_STATE_TTL_SECONDS = 2 * 60 * 60
_ROUTING_DECISION_RE = re.compile(
    r"(?im)^\s*(?:RECLASSIFY:\s*)?TIER:\s*(?P<tier>3A|3B|3C)\b\s*\|\s*MODEL:\s*(?P<model>[^|]+?)\s*\|\s*REASON:\s*(?P<reason>[^|]+?)\s*\|\s*CONFIDENCE:\s*(?P<confidence>high|medium|low)\s*$"
)
_RECLASSIFY_MARKER_RE = re.compile(r"(?i)\b(?:reclassify|reclassification|route change|route reclassification|escalate|downgrade)\b")
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_MARKDOWN_LINE_PREFIX_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+\.\s+)?")
_MARKDOWN_WRAP_RE = re.compile(r"^(?:\*\*|__|`+)+|(?:\*\*|__|`+)+$")
_SHELL_SPLIT_RE = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")
_SHELL_CHAIN_RE = re.compile(r"\s*(?:&&|;)\s*")
_SAFE_REDIRECTION_RE = re.compile(r"\b\d>&\d\b")
_UNSAFE_REDIRECTION_RE = re.compile(r"(^|[\s(])(?:\d*>>|\d*>)(?!&)")
_ROUTED_CODEX_WITH_CD_RE = re.compile(
    r"(?is)^\s*(?:cd|set-location|pushd)\b.*?(?:&&|;|\|\|)\s*codex\s+exec\b"
)
_ROUTED_CODEX_HAS_CWD_RE = re.compile(r"(?i)(?:^|\s)-C\s+\S+")
_ROUTED_CODEX_STDIN_RE = re.compile(r"(?is)\|\s*codex\s+exec\b.*(?:^|\s)-\s*$")
_ROUTED_CODEX_POWERSHELL_HOME_RE = re.compile(r"(?i)(?:^|\s)(?:cd|set-location|pushd)\s+~[/\\]")
_LONG_CODEX_INLINE_PROMPT_CHARS = 1200
_HERMES_CHAT_RE = re.compile(r"(?i)\bhermes\s+chat\b")
_HERMES_GLM_MODEL_RE = re.compile(r"(?i)(?:^|\s)-m\s+glm-5\.1\b")
_HERMES_ZAI_PROVIDER_RE = re.compile(r"(?i)(?:^|\s)--provider\s+zai\b")
_HERMES_GLM_BASE_URL_RE = re.compile(r"(?i)\bGLM_BASE_URL=(?P<value>\S+)")
_CODEX_EXEC_RE = re.compile(r"(?i)\bcodex\s+exec\b")
_CODEX_GPT54_MINI_RE = re.compile(r"(?i)(?:^|\s)-m\s+gpt-5\.4-mini\b")
_CODEX_GPT54_RE = re.compile(r"(?i)(?:^|\s)-m\s+gpt-5\.4\b")
_ROUTED_MODEL_OUTPUT_PIPE_RE = re.compile(
    r"(?is)\b(?:hermes\s+chat|codex\s+exec)\b.*\|\s*(?:tail|head|select-object\b)"
)
_CODEX_CAT_SUBSTITUTION_RE = re.compile(r"(?is)\bcodex\s+exec\b.*\$\(\s*cat\b")
_GIT_COMMIT_RE = re.compile(r"(?i)(?:^|[;&|]\s*|\s)git\s+commit\b")
_GIT_PUSH_RE = re.compile(r"(?i)(?:^|[;&|]\s*|\s)git\s+push\b")
_GIT_BRANCH_CREATE_RE = re.compile(r"(?i)(?:^|[;&|]\s*|\s)git\s+(?:checkout\s+-b|switch\s+-c)\b")
_GIT_MUTATION_RE = re.compile(
    r"(?i)(?:^|[;&|]\s*|\s)git\s+(?:checkout(?!\s+-b\b)|switch(?!\s+-c\b)|restore|reset|clean|merge|rebase|cherry-pick|revert)\b"
)
_GIT_COMMIT_REQUEST_RE = re.compile(
    r"(?i)\b(?:git\s+commit|commit(?:\s+it|\s+them|\s+the\s+changes|\s+changes)?|create\s+a\s+commit|make\s+a\s+commit)\b"
)
_GIT_PUSH_REQUEST_RE = re.compile(
    r"(?i)\b(?:git\s+push|push(?:\s+it|\s+them|\s+the\s+branch|\s+the\s+changes)?|publish\s+the\s+branch)\b"
)
_GIT_BRANCH_REQUEST_RE = re.compile(
    r"(?i)\b(?:branch|create\s+a\s+branch|new\s+branch|git\s+checkout\s+-b|git\s+switch\s+-c|switch\s+to\s+a\s+branch)\b"
)
_GIT_MUTATION_REQUEST_RE = re.compile(
    r"(?i)\b(?:git\s+checkout|git\s+restore|git\s+reset|git\s+clean|discard|revert|restore|reset|clean\s+up\s+unrelated\s+changes)\b"
)
_LEADING_ENV_ASSIGNMENTS_RE = re.compile(r"^(?:[a-z_][a-z0-9_]*=\S+\s+)+", re.IGNORECASE)
_TIMEOUT_PREFIX_RE = re.compile(r"^timeout\s+\d+\s+", re.IGNORECASE)
_VERIFICATION_OUTPUT_PIPE_RE = re.compile(r"(?i)\|\s*(?:tail|head|select-object)\b")

_READ_ONLY_TERMINAL_PREFIXES = (
    "cd",
    "pushd",
    "popd",
    "set-location",
    "ls",
    "dir",
    "pwd",
    "date",
    "whoami",
    "uname",
    "echo",
    "rg",
    "grep",
    "find",
    "fd",
    "cat",
    "head",
    "tail",
    "wc",
    "stat",
    "file",
    "git status",
    "git diff",
    "git show",
    "git log",
    "git branch --show-current",
    "git rev-parse",
    "get-childitem",
    "get-content",
    "select-string",
)

_VERIFICATION_TERMINAL_PREFIXES = (
    "pytest",
    "python -m pytest",
    "python -m unittest",
    "dotnet test",
    "dotnet build",
    "dotnet msbuild",
    "cargo test",
    "cargo check",
    "cargo build",
    "go test",
    "go build",
    "npm test",
    "npm run test",
    "npm run lint",
    "npm run build",
    "pnpm test",
    "pnpm lint",
    "pnpm build",
    "pnpm run test",
    "pnpm run lint",
    "pnpm run build",
    "yarn test",
    "yarn lint",
    "yarn build",
    "npx vitest",
    "vitest",
    "ruff check",
    "mypy",
    "eslint",
    "tsc",
    "make test",
    "make lint",
    "make build",
    "just test",
    "just lint",
    "just build",
)

_VERIFICATION_MUTATION_EXCEPTIONS = frozenset(
    {
        "dotnet build",
        "dotnet test",
        "pytest",
        "python ",
    }
)

_TERMINAL_MUTATION_MARKERS = (
    ">",
    ">>",
    "tee",
    "out-file",
    "set-content",
    "add-content",
    "new-item",
    "remove-item",
    "move-item",
    "copy-item",
    "rename-item",
    "mkdir",
    "touch",
    "rm ",
    "mv ",
    "cp ",
    "sed -i",
    "perl -pi",
    "git apply",
    "git am",
    "git checkout",
    "git switch",
    "git cherry-pick",
    "git merge",
    "git commit",
    "npm install",
    "pnpm install",
    "yarn add",
    "pip install",
    "uv add",
    "cargo add",
    "dotnet add",
    "dotnet build",
    "dotnet test",
    "pytest",
    "python ",
)

_IMPLEMENTATION_DELEGATE_KEYWORDS = (
    "implement",
    "implementation",
    "fix",
    "patch",
    "refactor",
    "edit",
    "write",
    "modify",
    "change",
    "code",
    "bug",
    "feature",
    "test",
)
_ZAI_CODING_BASE_URL = "https://api.z.ai/api/coding/paas/v4"
_ROUTED_QUOTA_EXHAUSTED_RE = re.compile(
    r"(?is)\b(?:insufficient balance|no resource package|resource package|quota exhausted|credits? exhausted|please recharge)\b"
)
_ROUTED_FAILURE_OUTPUT_RE = re.compile(
    r"(?is)\b(?:429|rate[- ]limit(?:ed)?|too many requests|insufficient balance|no resource package|resource package|quota exhausted|credits? exhausted|please recharge|remoteprotocolerror|provider dropped|transport failure|http failure|auth failure|authentication failure|model not found|write failure|patch rejection|failed to execute|timed out|timeout)\b"
)
_ALLOWED_ROUTE_MODELS = {
    "3A": ("Codex CLI (gpt-5.4)",),
    "3B": ("Hermes CLI (glm-5.1 via zai)", "Codex CLI (gpt-5.4-mini)"),
    "3C": ("Codex CLI (gpt-5.4-mini)",),
}

_task_state_lock = threading.Lock()
_task_state: dict[str, dict[str, Any]] = {}


def _initial_route_attempts() -> dict[str, Any]:
    return {
        "3b_primary_attempted": False,
        "3b_primary_failed": False,
        "3b_primary_failure_kind": None,
        "last_attempt_kind": None,
        "last_attempt_failed": False,
        "last_attempt_failure_kind": None,
    }


def _derive_git_permissions(user_message: str) -> dict[str, bool]:
    text = user_message or ""
    return {
        "commit": bool(_GIT_COMMIT_REQUEST_RE.search(text)),
        "push": bool(_GIT_PUSH_REQUEST_RE.search(text)),
        "branch": bool(_GIT_BRANCH_REQUEST_RE.search(text)),
        "mutate": bool(_GIT_MUTATION_REQUEST_RE.search(text)),
    }


def _new_task_state(*, session_id: str = "", skills: Optional[list[str]] = None, user_message: str = "") -> dict[str, Any]:
    return {
        "session_id": session_id or "",
        "skills": list(skills or []),
        "enforced": True,
        "routed": False,
        "decision": None,
        "decision_line": None,
        "decision_error": None,
        "route_attempts": _initial_route_attempts(),
        "verification_attempts": [],
        "git_permissions": _derive_git_permissions(user_message),
        "updated_at": time.time(),
    }


def _purge_expired(now: Optional[float] = None) -> None:
    cutoff = (now or time.time()) - _TASK_STATE_TTL_SECONDS
    expired = [task_id for task_id, state in _task_state.items() if state.get("updated_at", 0.0) < cutoff]
    for task_id in expired:
        _task_state.pop(task_id, None)


def activate_for_task(
    task_id: str,
    *,
    session_id: str = "",
    skills: Optional[list[str]] = None,
    user_message: str = "",
) -> None:
    if not task_id:
        return
    with _task_state_lock:
        _purge_expired()
        _task_state[task_id] = _new_task_state(
            session_id=session_id,
            skills=skills,
            user_message=user_message,
        )


def deactivate_for_task(task_id: str) -> None:
    if not task_id:
        return
    with _task_state_lock:
        _task_state.pop(task_id, None)


def is_active_for_task(task_id: str) -> bool:
    if not task_id:
        return False
    with _task_state_lock:
        _purge_expired()
        return bool(_task_state.get(task_id, {}).get("enforced"))


def has_route_lock(task_id: str) -> bool:
    if not task_id:
        return False
    with _task_state_lock:
        _purge_expired()
        return bool(_task_state.get(task_id, {}).get("routed"))


def is_routing_enforced_task(task_id: str) -> bool:
    if not task_id:
        return False
    with _task_state_lock:
        _purge_expired()
        state = _task_state.get(task_id, {})
        skills = state.get("skills") or []
        return bool(state.get("enforced")) and DEFAULT_ROUTING_SKILL in skills


def _strip_think_blocks(text: str) -> str:
    return _THINK_BLOCK_RE.sub("", text or "")


def _normalize_routing_lines(text: str) -> str:
    normalized_lines: list[str] = []
    for raw_line in (text or "").splitlines():
        line = _MARKDOWN_LINE_PREFIX_RE.sub("", raw_line.strip())
        line = _MARKDOWN_WRAP_RE.sub("", line).strip()
        normalized_lines.append(line)
    return "\n".join(normalized_lines)


def get_routing_decision(task_id: str) -> Optional[dict[str, str]]:
    if not task_id:
        return None
    with _task_state_lock:
        _purge_expired()
        decision = _task_state.get(task_id, {}).get("decision")
        if not isinstance(decision, dict):
            return None
        return dict(decision)


def get_route_attempts(task_id: str) -> dict[str, Any]:
    if not task_id:
        return _initial_route_attempts()
    with _task_state_lock:
        _purge_expired()
        attempts = _task_state.get(task_id, {}).get("route_attempts")
        if not isinstance(attempts, dict):
            return _initial_route_attempts()
        merged = _initial_route_attempts()
        merged.update(attempts)
        return merged


def get_routed_execution_plan(task_id: str) -> list[dict[str, str]]:
    decision = get_routing_decision(task_id)
    if not decision:
        return []

    tier = str(decision.get("tier", "")).upper()
    model = _normalize_route_model(str(decision.get("model", "")))
    attempts = get_route_attempts(task_id)

    if tier == "3A":
        return [{"kind": "codex_gpt54", "label": "Codex CLI (gpt-5.4)"}]

    if tier == "3C":
        return [{"kind": "codex_gpt54mini", "label": "Codex CLI (gpt-5.4-mini)"}]

    if tier == "3B":
        if model == _normalize_route_model("Codex CLI (gpt-5.4-mini)"):
            return [{"kind": "codex_gpt54mini", "label": "Codex CLI (gpt-5.4-mini)"}]
        if attempts.get("3b_primary_failed"):
            return [{"kind": "codex_gpt54mini", "label": "Codex CLI (gpt-5.4-mini)"}]
        return [
            {"kind": "hermes_glm_zai", "label": "Hermes CLI (glm-5.1 via zai)"},
            {"kind": "codex_gpt54mini", "label": "Codex CLI (gpt-5.4-mini)"},
        ]

    return []


def get_verification_attempts(task_id: str) -> list[dict[str, Any]]:
    if not task_id:
        return []
    with _task_state_lock:
        _purge_expired()
        attempts = _task_state.get(task_id, {}).get("verification_attempts")
        if not isinstance(attempts, list):
            return []
        return [dict(item) for item in attempts if isinstance(item, dict)]


def _normalize_route_model(model: str) -> str:
    return " ".join((model or "").strip().lower().split())


def _format_route_label(tier: str, model: str) -> str:
    return f"TIER: {tier} | MODEL: {model}"


def _format_allowed_route_models(tier: str) -> str:
    labels = _ALLOWED_ROUTE_MODELS.get((tier or "").upper(), ())
    return ", ".join(f"`{label}`" for label in labels)


def _set_decision_error(state: dict[str, Any], message: str) -> None:
    state["decision_error"] = message
    state["updated_at"] = time.time()


def _get_decision_error(task_id: str) -> Optional[str]:
    if not task_id:
        return None
    with _task_state_lock:
        _purge_expired()
        error = _task_state.get(task_id, {}).get("decision_error")
        return str(error) if isinstance(error, str) and error.strip() else None


def record_routing_decision(task_id: str, assistant_content: str, *, session_id: str = "") -> bool:
    if not task_id or not assistant_content:
        return False
    clean = _normalize_routing_lines(_strip_think_blocks(assistant_content))
    match = _ROUTING_DECISION_RE.search(clean)
    if not match:
        return False
    decision = {
        "tier": match.group("tier").upper(),
        "model": match.group("model").strip(),
        "reason": match.group("reason").strip(),
        "confidence": match.group("confidence").lower(),
    }
    raw_line = match.group(0).strip()
    normalized_model = _normalize_route_model(decision["model"])
    with _task_state_lock:
        _purge_expired()
        state = _task_state.setdefault(
            task_id,
            _new_task_state(session_id=session_id),
        )
        allowed_models = _ALLOWED_ROUTE_MODELS.get(decision["tier"], ())
        if normalized_model not in {_normalize_route_model(label) for label in allowed_models}:
            _set_decision_error(
                state,
                (
                    f"Routing guard blocked invalid routing decision for task {task_id}: "
                    f"`{_format_route_label(decision['tier'], decision['model'])}` is not allowed. "
                    f"Allowed model labels for {decision['tier']} are: {_format_allowed_route_models(decision['tier'])}."
                ),
            )
            return False
        current = state.get("decision")
        if isinstance(current, dict):
            current_key = (current.get("tier"), current.get("model"))
            new_key = (decision["tier"], decision["model"])
            if current_key != new_key and not _RECLASSIFY_MARKER_RE.search(clean):
                _set_decision_error(
                    state,
                    (
                        f"Routing guard blocked route drift for task {task_id}: current route is "
                        f"`{_format_route_label(str(current.get('tier', '')), str(current.get('model', '')))}` "
                        f"but the latest assistant output attempted `{_format_route_label(decision['tier'], decision['model'])}` "
                        "without `RECLASSIFY:`. Emit an explicit `RECLASSIFY:` line or stay on the current route."
                    ),
                )
                return False
            if current_key != new_key:
                state["route_attempts"] = _initial_route_attempts()
        state["session_id"] = session_id or state.get("session_id", "")
        state["routed"] = True
        state["decision"] = decision
        state["decision_line"] = raw_line
        state["decision_error"] = None
        state["updated_at"] = time.time()
    return True


def _classify_routed_terminal_command(command: str) -> Optional[str]:
    normalized = " ".join((command or "").strip().lower().split())
    if not normalized:
        return None
    if _HERMES_CHAT_RE.search(normalized):
        if _HERMES_GLM_MODEL_RE.search(normalized) and _HERMES_ZAI_PROVIDER_RE.search(normalized):
            return "hermes_glm_zai"
        return "hermes_chat_other"
    if _CODEX_EXEC_RE.search(normalized):
        if _CODEX_GPT54_MINI_RE.search(normalized):
            return "codex_gpt54mini"
        if _CODEX_GPT54_RE.search(normalized):
            return "codex_gpt54"
        return "codex_other"
    return None


def _validate_terminal_route_command(command: str, task_id: str) -> Optional[str]:
    decision = get_routing_decision(task_id)
    if not decision:
        return None
    route_kind = _classify_routed_terminal_command(command)
    if route_kind is None:
        return None

    tier = decision.get("tier")
    decision_model = _normalize_route_model(str(decision.get("model", "")))
    with _task_state_lock:
        _purge_expired()
        attempts = dict(_task_state.get(task_id, {}).get("route_attempts") or {})

    if tier == "3A":
        if route_kind != "codex_gpt54":
            return (
                "Routing guard blocked routed model mismatch: Tier 3A must execute through "
                "`Codex CLI (gpt-5.4)` unless you explicitly reclassify the route."
            )
        return None

    if tier == "3B":
        if decision_model == _normalize_route_model("Codex CLI (gpt-5.4-mini)"):
            if route_kind != "codex_gpt54mini":
                return (
                    "Routing guard blocked routed model mismatch: the active Tier 3B route is "
                    "`Codex CLI (gpt-5.4-mini)`. Emit `RECLASSIFY:` if you intend to switch routes again."
                )
            return None

        if route_kind == "hermes_glm_zai":
            return None
        if route_kind == "codex_gpt54mini":
            if not attempts.get("3b_primary_failed"):
                return (
                    "Routing guard blocked Tier 3B backup: attempt the primary route "
                    "`Hermes CLI (glm-5.1 via zai)` first and fall back to Codex only after that "
                    "primary attempt fails."
                )
            return None
        if route_kind == "codex_gpt54":
            return (
                "Routing guard blocked routed model mismatch: Tier 3B backup is "
                "`Codex CLI (gpt-5.4-mini)`, not `gpt-5.4`."
            )
        return (
            "Routing guard blocked routed model mismatch: Tier 3B primary must be "
            "`Hermes CLI (glm-5.1 via zai)`."
        )

    if tier == "3C":
        if route_kind != "codex_gpt54mini":
            return (
                "Routing guard blocked routed model mismatch: Tier 3C must execute through "
                "`Codex CLI (gpt-5.4-mini)` unless you explicitly reclassify the route."
            )
    return None


def _validate_git_terminal_command(command: str, task_id: str) -> Optional[str]:
    raw = (command or "").strip()
    if not raw:
        return None

    with _task_state_lock:
        _purge_expired()
        permissions = dict(_task_state.get(task_id, {}).get("git_permissions") or {})

    if _GIT_COMMIT_RE.search(raw) and not permissions.get("commit"):
        return (
            "Routing guard blocked `git commit`: commits require an explicit user request. "
            "Do not grant yourself commit authority."
        )
    if _GIT_PUSH_RE.search(raw) and not permissions.get("push"):
        return (
            "Routing guard blocked `git push`: pushes require an explicit user request."
        )
    if _GIT_BRANCH_CREATE_RE.search(raw) and not permissions.get("branch"):
        return (
            "Routing guard blocked branch creation/switching: creating or switching branches "
            "requires an explicit user request."
        )
    if _GIT_MUTATION_RE.search(raw) and not permissions.get("mutate"):
        return (
            "Routing guard blocked git history/worktree mutation: `git checkout`/`restore`/`reset`/"
            "`clean`/merge-style commands require an explicit user request and must not be used "
            "to clean up unrelated changes."
        )
    return None


def _is_explicitly_permitted_git_terminal_command(command: str, task_id: str) -> bool:
    raw = (command or "").strip()
    if not raw:
        return False

    with _task_state_lock:
        _purge_expired()
        permissions = dict(_task_state.get(task_id, {}).get("git_permissions") or {})

    return bool(
        (_GIT_COMMIT_RE.search(raw) and permissions.get("commit"))
        or (_GIT_PUSH_RE.search(raw) and permissions.get("push"))
        or (_GIT_BRANCH_CREATE_RE.search(raw) and permissions.get("branch"))
        or (_GIT_MUTATION_RE.search(raw) and permissions.get("mutate"))
    )


def record_tool_result(task_id: str, tool_name: str, args: dict[str, Any], result: Any) -> None:
    if (
        tool_name not in {"terminal", "routed_exec"}
        or not task_id
        or not isinstance(args, dict)
        or not is_active_for_task(task_id)
    ):
        return

    if tool_name == "routed_exec":
        try:
            payload = json.loads(result) if isinstance(result, str) else result
        except Exception:
            payload = None
        if not isinstance(payload, dict):
            return
        attempt_entries = payload.get("attempts")
        if not isinstance(attempt_entries, list):
            return
        with _task_state_lock:
            _purge_expired()
            state = _task_state.get(task_id)
            if not state:
                return
            attempts = state.setdefault("route_attempts", _initial_route_attempts())
            for entry in attempt_entries:
                if not isinstance(entry, dict):
                    continue
                route_kind = str(entry.get("kind", "") or "")
                if not route_kind:
                    continue
                output = str(entry.get("output", "") or "")
                failure_kind = str(entry.get("failure_kind") or "") or _classify_routed_failure_kind(output)
                failed = bool(entry.get("failed"))
                if not failed:
                    exit_code = entry.get("exit_code", 0)
                    try:
                        failed = int(exit_code or 0) != 0 or bool(_ROUTED_FAILURE_OUTPUT_RE.search(output))
                    except Exception:
                        failed = True
                attempts["last_attempt_kind"] = route_kind
                attempts["last_attempt_failed"] = failed
                attempts["last_attempt_failure_kind"] = failure_kind if failed else None
                if route_kind == "hermes_glm_zai":
                    attempts["3b_primary_attempted"] = True
                    attempts["3b_primary_failed"] = failed
                    attempts["3b_primary_failure_kind"] = failure_kind if failed else None
            state["updated_at"] = time.time()
        return

    command = str(args.get("command", "") or "")
    route_kind = _classify_routed_terminal_command(command)
    verification_kind = None if route_kind is not None else _classify_verification_command(command)
    if route_kind is None and verification_kind is None:
        return

    failed = True
    failure_kind = None
    output = ""
    error_text = ""
    exit_code = 1
    try:
        payload = json.loads(result) if isinstance(result, str) else result
        output = str(payload.get("output", "") or "")
        error_text = str(payload.get("error", "") or "")
        exit_code = int(payload.get("exit_code", 1) or 0)
        failure_kind = _classify_routed_failure_kind(output)
        failed = (
            bool(error_text)
            or exit_code != 0
            or bool(_ROUTED_FAILURE_OUTPUT_RE.search(output))
        )
    except Exception:
        failed = True

    with _task_state_lock:
        _purge_expired()
        state = _task_state.get(task_id)
        if not state:
            return
        if verification_kind is not None:
            attempts = state.setdefault("verification_attempts", [])
            attempts.append(
                {
                    "kind": verification_kind,
                    "command": command,
                    "success": not failed,
                    "exit_code": exit_code,
                    "error": error_text or None,
                    "output_excerpt": output[:500] if output else "",
                }
            )
            if len(attempts) > 20:
                del attempts[:-20]
            state["updated_at"] = time.time()
            return
        attempts = state.setdefault("route_attempts", _initial_route_attempts())
        attempts["last_attempt_kind"] = route_kind
        attempts["last_attempt_failed"] = failed
        attempts["last_attempt_failure_kind"] = failure_kind if failed else None
        if route_kind == "hermes_glm_zai":
            attempts["3b_primary_attempted"] = True
            attempts["3b_primary_failed"] = failed
            attempts["3b_primary_failure_kind"] = failure_kind if failed else None
        state["updated_at"] = time.time()


def _is_read_only_terminal_command(command: str) -> bool:
    normalized = " ".join((command or "").strip().lower().split())
    if not normalized:
        return True

    normalized = _SAFE_REDIRECTION_RE.sub("", normalized)
    if _UNSAFE_REDIRECTION_RE.search(normalized):
        return False

    commands = [part.strip() for part in _SHELL_SPLIT_RE.split(normalized) if part.strip()]
    if not commands:
        return True

    for part in commands:
        if any(marker in part for marker in _TERMINAL_MUTATION_MARKERS):
            return False
        if not any(
            part == prefix or part.startswith(f"{prefix} ")
            for prefix in _READ_ONLY_TERMINAL_PREFIXES
        ):
            return False

    return True


def _normalize_verification_segment(segment: str) -> str:
    normalized = " ".join((segment or "").strip().lower().split())
    if not normalized:
        return normalized
    previous = None
    while normalized and previous != normalized:
        previous = normalized
        normalized = _LEADING_ENV_ASSIGNMENTS_RE.sub("", normalized)
        normalized = _TIMEOUT_PREFIX_RE.sub("", normalized)
        normalized = normalized.removeprefix("env ").strip()
    return normalized


def _classify_verification_command(command: str) -> Optional[str]:
    raw = (command or "").strip()
    if not raw:
        return None

    normalized = " ".join(raw.lower().split())
    normalized = _SAFE_REDIRECTION_RE.sub("", normalized)

    if _UNSAFE_REDIRECTION_RE.search(normalized):
        return None
    if _VERIFICATION_OUTPUT_PIPE_RE.search(normalized):
        return None
    if "| codex exec" in normalized or "| hermes chat" in normalized:
        return None
    if _classify_routed_terminal_command(normalized) is not None:
        return None

    parts = [part.strip() for part in _SHELL_CHAIN_RE.split(normalized) if part.strip()]
    if not parts:
        return None

    matched_prefix: Optional[str] = None
    for part in parts:
        if _is_read_only_terminal_command(part):
            continue
        if any(
            marker in part
            for marker in _TERMINAL_MUTATION_MARKERS
            if marker not in _VERIFICATION_MUTATION_EXCEPTIONS
        ):
            return None
        verification = _normalize_verification_segment(part)
        matched = next(
            (
                prefix
                for prefix in _VERIFICATION_TERMINAL_PREFIXES
                if verification == prefix or verification.startswith(f"{prefix} ")
            ),
            None,
        )
        if matched is None:
            return None
        matched_prefix = matched

    return matched_prefix


def _is_verification_terminal_command(command: str) -> bool:
    return _classify_verification_command(command) is not None


def _validate_routed_codex_terminal_command(command: str) -> Optional[str]:
    raw = (command or "").strip()
    if not raw:
        return None

    normalized = " ".join(raw.lower().split())
    if "codex exec" not in normalized:
        return None

    if _ROUTED_MODEL_OUTPUT_PIPE_RE.search(raw):
        return (
            "Routing guard blocked routed model invocation: do not pipe `codex exec` output through "
            "`tail`/`head`/`Select-Object` because that can mask the true exit status. Run the routed "
            "model command directly."
        )

    if "&&" in raw:
        return (
            "Routing guard blocked routed `codex exec`: `&&` is not valid in PowerShell 5.1. "
            "Use `codex exec -C ...` directly instead of chaining with `cd ... &&`."
        )

    if _ROUTED_CODEX_WITH_CD_RE.search(raw) and _ROUTED_CODEX_HAS_CWD_RE.search(raw):
        return (
            "Routing guard blocked routed `codex exec`: do not prefix Codex with `cd`/`Set-Location` "
            "when `-C` is already provided. Use `-C` as the working-directory control."
        )

    if _ROUTED_CODEX_POWERSHELL_HOME_RE.search(raw):
        return (
            "Routing guard blocked routed `codex exec`: `~/...` resolves to the Windows home in PowerShell. "
            "Use `-C /home/...` or a `\\\\wsl.localhost\\...` path instead."
        )

    if _CODEX_CAT_SUBSTITUTION_RE.search(raw):
        return (
            "Routing guard blocked routed `codex exec`: do not pass the prompt via `$(cat file)` shell "
            "substitution. Use stdin directly, for example `cat file | codex exec ... -`."
        )

    return None


def _validate_routed_hermes_terminal_command(command: str) -> Optional[str]:
    raw = (command or "").strip()
    if not raw:
        return None

    normalized = " ".join(raw.lower().split())
    if "hermes chat" not in normalized:
        return None

    if _ROUTED_MODEL_OUTPUT_PIPE_RE.search(raw):
        return (
            "Routing guard blocked routed model invocation: do not pipe `hermes chat` output through "
            "`tail`/`head`/`Select-Object` because that can mask the true exit status. Run the routed "
            "model command directly."
        )

    return None


def _classify_routed_failure_kind(output: str) -> Optional[str]:
    text = str(output or "")
    if not text:
        return None
    if _ROUTED_QUOTA_EXHAUSTED_RE.search(text):
        return "quota_exhausted"
    if re.search(r"(?is)\b(?:429|rate[- ]limit(?:ed)?|too many requests)\b", text):
        return "rate_limited"
    if re.search(r"(?is)\b(?:remoteprotocolerror|provider dropped|transport failure|http failure)\b", text):
        return "transport_failure"
    if re.search(r"(?is)\b(?:auth failure|authentication failure)\b", text):
        return "authentication_failure"
    if re.search(r"(?is)\bmodel not found\b", text):
        return "model_not_found"
    if re.search(r"(?is)\b(?:write failure|patch rejection|failed to execute)\b", text):
        return "execution_failure"
    if re.search(r"(?is)\b(?:timed out|timeout)\b", text):
        return "timeout"
    return None


def _extract_final_shell_string_arg(command: str) -> tuple[str, str] | None:
    trimmed = (command or "").rstrip()
    if not trimmed or trimmed[-1] not in {"'", '"'}:
        return None

    quote = trimmed[-1]
    end = len(trimmed) - 1
    start = None
    idx = end - 1
    while idx >= 0:
        if trimmed[idx] == quote:
            if quote == "'" and idx > 0 and trimmed[idx - 1] == quote:
                idx -= 2
                continue
            start = idx
            break
        idx -= 1

    if start is None:
        return None

    prefix_raw = trimmed[:start]
    if prefix_raw and not prefix_raw[-1].isspace():
        return None
    prefix = prefix_raw.rstrip()

    prompt = trimmed[start + 1:end]
    if quote == "'":
        prompt = prompt.replace("''", "'")
    return prefix, prompt


def _build_powershell_herestring(prompt: str) -> str:
    safe_prompt = (prompt or "").replace("'@", "'`@")
    return f"@'\n{safe_prompt}\n'@"


def _pin_routed_hermes_glm_base_url(command: str) -> str:
    raw = (command or "").strip()
    if _classify_routed_terminal_command(raw) != "hermes_glm_zai":
        return raw

    if _HERMES_GLM_BASE_URL_RE.search(raw):
        return _HERMES_GLM_BASE_URL_RE.sub(
            f"GLM_BASE_URL={_ZAI_CODING_BASE_URL}",
            raw,
            count=1,
        )

    hermes_idx = raw.lower().find("hermes chat")
    if hermes_idx < 0:
        return raw
    return f"{raw[:hermes_idx]}GLM_BASE_URL={_ZAI_CODING_BASE_URL} {raw[hermes_idx:]}"


def rewrite_routed_tool_args(tool_name: str, args: dict[str, Any], task_id: str) -> dict[str, Any]:
    if (
        tool_name != "terminal"
        or not isinstance(args, dict)
        or not task_id
        or not has_route_lock(task_id)
        or not is_routing_enforced_task(task_id)
    ):
        return args

    command = str(args.get("command", "") or "")
    raw = command.strip()
    if not raw:
        return args

    rewritten = raw
    route_kind = _classify_routed_terminal_command(raw)

    if route_kind == "hermes_glm_zai":
        rewritten = _pin_routed_hermes_glm_base_url(rewritten)

    if "codex exec" not in raw.lower():
        if rewritten == raw:
            return args
        new_args = dict(args)
        new_args["command"] = rewritten
        return new_args

    if _ROUTED_CODEX_WITH_CD_RE.search(rewritten):
        codex_idx = rewritten.lower().find("codex exec")
        if codex_idx >= 0:
            rewritten = rewritten[codex_idx:].lstrip()

    if _ROUTED_CODEX_POWERSHELL_HOME_RE.search(command):
        codex_idx = rewritten.lower().find("codex exec")
        if codex_idx >= 0:
            rewritten = rewritten[codex_idx:].lstrip()

    uses_stdin = rewritten.rstrip().endswith(" -") or bool(_ROUTED_CODEX_STDIN_RE.search(rewritten))
    if not uses_stdin and len(rewritten) > _LONG_CODEX_INLINE_PROMPT_CHARS:
        parsed = _extract_final_shell_string_arg(rewritten)
        if parsed is not None:
            prefix, prompt = parsed
            rewritten = f"{_build_powershell_herestring(prompt)} | {prefix} -"

    if rewritten == raw:
        return args

    new_args = dict(args)
    new_args["command"] = rewritten
    return new_args


def _is_implementation_delegate(args: dict[str, Any]) -> bool:
    text_parts: list[str] = []
    goal = args.get("goal")
    context = args.get("context")
    tasks = args.get("tasks")
    if isinstance(goal, str):
        text_parts.append(goal)
    if isinstance(context, str):
        text_parts.append(context)
    if isinstance(tasks, list):
        for task in tasks:
            if isinstance(task, dict):
                text_parts.append(json.dumps(task, ensure_ascii=False))
            else:
                text_parts.append(str(task))
    combined = " ".join(text_parts).lower()
    return any(keyword in combined for keyword in _IMPLEMENTATION_DELEGATE_KEYWORDS)


def pre_tool_call_block_reason(tool_name: str, args: dict[str, Any], task_id: str) -> Optional[str]:
    if not task_id or not is_active_for_task(task_id):
        return None
    routed = has_route_lock(task_id)
    routing_task = is_routing_enforced_task(task_id)
    decision_error = _get_decision_error(task_id)

    if decision_error and tool_name in {"patch", "write_file", "terminal", "delegate_task", "routed_exec"}:
        return decision_error

    if tool_name in {"patch", "write_file"}:
        if routed and routing_task:
            return (
                f"Routing guard blocked native `{tool_name}` for task {task_id}: "
                "stay on the routed model path and do not fall back to native file mutation."
            )
        return (
            f"Routing guard blocked `{tool_name}` for task {task_id}: "
            "emit a routing decision line before mutating files."
        )

    if tool_name == "routed_exec":
        if not routing_task:
            return (
                "Routing guard blocked `routed_exec`: this tool is reserved for routing-layer controlled "
                "coding tasks."
            )
        if routed:
            return None
        return (
            f"Routing guard blocked `routed_exec` for task {task_id}: "
            "emit a routing decision line before starting routed execution."
        )

    if tool_name == "terminal":
        command = ""
        if isinstance(args, dict):
            command = str(args.get("command", "") or "")
        git_issue = _validate_git_terminal_command(command, task_id)
        if git_issue:
            return git_issue
        if routed and routing_task:
            if _is_explicitly_permitted_git_terminal_command(command, task_id):
                return None
            route_kind = _classify_routed_terminal_command(command)
            if route_kind is not None:
                return (
                    f"Routing guard blocked routed model execution through `terminal` for task {task_id}: "
                    "use `routed_exec` for routed Codex/Hermes execution. "
                    "`terminal` remains available only for read-only inspection and explicitly permitted git actions."
                )
            if _is_verification_terminal_command(command):
                return None
            if _is_read_only_terminal_command(command):
                return None
            return (
                f"Routing guard blocked native `terminal` execution for task {task_id}: "
                "after a routing decision, non-read-only shell work must stay on the routed model path. "
                "Only routed model execution via `routed_exec`, approved verification commands, and read-only inspection commands are allowed."
            )
        if routed:
            return None
        if _is_read_only_terminal_command(command):
            return None
        return (
            f"Routing guard blocked `terminal` for task {task_id}: "
            "only read-only inspection commands are allowed before a routing decision."
        )

    if tool_name == "delegate_task":
        if routed and routing_task and _is_implementation_delegate(args if isinstance(args, dict) else {}):
            return (
                f"Routing guard blocked native `delegate_task` for task {task_id}: "
                "stay on the routed model path instead of falling back to ordinary delegation."
            )
        if not isinstance(args, dict) or not _is_implementation_delegate(args):
            return None
        return (
            f"Routing guard blocked `delegate_task` for task {task_id}: "
            "implementation-oriented delegation requires a routing decision first."
        )

    return None
