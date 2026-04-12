from __future__ import annotations

from fnmatch import fnmatchcase
import json
from pathlib import Path
import re
import shlex
import threading
import time
from typing import Any, Optional

from agent.routing_policy import (
    ROUTING_POLICY_VERSION,
    DEFAULT_ROUTE_PATHS,
    infer_route_path,
    get_allowed_route_models,
    get_primary_model_path_by_tier,
    get_route_matrix,
    load_routing_policy,
    normalize_route_model,
    normalize_route_path,
    validate_route_choice,
)
from agent.ability_context import (
    VISUAL_CACHE_TTL_SECONDS,
    compact_packets_for_handoff,
    detect_ability_requirements,
    make_ability_packet,
    normalize_lanes,
    preflight_missing_lanes,
    required_lanes,
    visual_post_verified,
)


DEFAULT_ROUTING_SKILL = "routing-layer"

_TASK_STATE_TTL_SECONDS = 2 * 60 * 60
_MAX_CUSTOM_SYSTEM_ISSUES = 12
_ROUTING_DECISION_RE = re.compile(
    r"(?im)^\s*(?:RECLASSIFY:\s*)?TIER:\s*(?P<tier>3A|3B|3C)\b\s*(?:\|\s*PATH:\s*(?P<path>[a-z0-9-]+)\s*)?\|\s*MODEL:\s*(?P<model>[^|]+?)\s*\|\s*REASON:\s*(?P<reason>[^|]+?)\s*\|\s*CONFIDENCE:\s*(?P<confidence>high|medium|low)\s*$"
)
_RECLASSIFY_MARKER_RE = re.compile(r"(?i)\b(?:reclassify|reclassification|route change|route reclassification|escalate|downgrade)\b")
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_MARKDOWN_LINE_PREFIX_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+\.\s+)?")
_MARKDOWN_WRAP_RE = re.compile(r"^(?:\*\*|__|`+)+|(?:\*\*|__|`+)+$")
_PATCH_TARGET_RE = re.compile(r"(?im)^\*\*\*\s+(?:Add|Update|Delete)\s+File:\s+(.+?)\s*$")
_SHELL_SPLIT_RE = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")
_SHELL_CHAIN_RE = re.compile(r"\s*(?:&&|;)\s*")
_SAFE_REDIRECTION_RE = re.compile(r"\b\d>&\d\b|(^|[\s(])(?:\d*>|>)(?:\s*)(?:/dev/null|nul)\b", re.IGNORECASE)
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
_HERMES_MINIMAX_MODEL_RE = re.compile(r"(?i)(?:^|\s)-m\s+minimax-m2\.7\b")
_HERMES_MINIMAX_PROVIDER_RE = re.compile(r"(?i)(?:^|\s)--provider\s+minimax\b")
_HERMES_MIMO_MODEL_RE = re.compile(r"(?i)(?:^|\s)-m\s+(?:xiaomi/)?mimo-v2-pro\b")
_HERMES_NOUS_PROVIDER_RE = re.compile(r"(?i)(?:^|\s)--provider\s+nous\b")
_CODEX_EXEC_RE = re.compile(r"(?i)\bcodex\s+exec\b")
_CODEX_GPT54_MINI_RE = re.compile(r"(?i)(?:^|\s)-m\s+gpt-5\.4-mini\b")
_CODEX_GPT54_RE = re.compile(r"(?i)(?:^|\s)-m\s+gpt-5\.4\b")
_MCPORTER_DISCOVERY_RE = re.compile(
    r"(?i)^(?:npx\s+(?:(?:-y|--yes)\s+)?)?mcporter\s+(?:list|inspect-cli|schema)\b"
)
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
_FIXED_CLAIM_RE = re.compile(r"(?i)\b(?:fixed|resolved|shipped)\b")
_UNVERIFIED_COMPLETION_RE = re.compile(
    r"(?is)\b(?:unverified|not verified|could not verify|unable to verify|verification\b.*\b(?:not run|did not run|failed|blocked|unavailable)|residual risk)\b"
)
_LEADING_ENV_ASSIGNMENTS_RE = re.compile(r"^(?:[a-z_][a-z0-9_]*=\S+\s+)+", re.IGNORECASE)
_TIMEOUT_PREFIX_RE = re.compile(r"^timeout\s+\d+\s+", re.IGNORECASE)
_VERIFICATION_OUTPUT_PIPE_RE = re.compile(r"(?i)\|\s*(?:tail|head|select-object)\b")
_VERIFICATION_OUTPUT_PIPE_SPLIT_RE = re.compile(r"(?i)\|\s*(?:tail|head|select-object)\b.*$")

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
_WORK_CLASS_CODE = "code"
_WORK_CLASS_CONFIG = "config_or_executable_text"
_WORK_CLASS_BEHAVIOR = "behavior_markdown"
_WORK_CLASS_DOCS = "docs_text"
_WORK_CLASS_UNKNOWN = "unknown"
_WORK_CLASS_MIXED = "mixed"
_TASK_CLASS_CODING = "coding"
_TASK_CLASS_NON_CODING = "non_coding_authoring"
_TASK_CLASS_MIXED = "mixed"
_DOC_TEXT_SUFFIXES = frozenset({".md", ".markdown", ".mdx", ".txt", ".rst", ".adoc", ".org"})
_CONFIG_TEXT_SUFFIXES = frozenset(
    {
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".xml",
        ".sql",
        ".csproj",
        ".props",
        ".targets",
    }
)
_EXECUTABLE_TEXT_SUFFIXES = frozenset({".sh", ".bash", ".ps1", ".bat", ".cmd"})
_CODE_FILE_SUFFIXES = frozenset(
    {
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".cs",
        ".java",
        ".go",
        ".rs",
        ".c",
        ".cc",
        ".cpp",
        ".cxx",
        ".h",
        ".hh",
        ".hpp",
        ".swift",
        ".kt",
        ".kts",
        ".rb",
        ".php",
        ".html",
        ".css",
        ".scss",
        ".sass",
    }
)
_BEHAVIOR_MARKDOWN_FILENAMES = frozenset(
    {
        "soul.md",
        "agents.md",
        "hermes.md",
        ".hermes.md",
        "claude.md",
        ".cursorrules",
        "skill.md",
    }
)
_DOCS_BASENAME_PREFIXES = (
    "readme",
    "changelog",
    "release",
    "design",
    "notes",
    "report",
    "summary",
)
_NON_CODE_PATH_SEGMENTS = frozenset({"wiki", "docs", "notes", "plans", "queries", "comparisons"})
_CODE_SENSITIVE_ROOT_PATTERNS = (
    "src/",
    "app/",
    "lib/",
    "packages/",
    "tests/",
    "scripts/",
    ".github/workflows/",
    "infra/",
    "config/",
    "migrations/",
    "database/",
)
_ROUTED_QUOTA_EXHAUSTED_RE = re.compile(
    r"(?is)\b(?:insufficient balance|no resource package|resource package|quota exhausted|credits? exhausted|please recharge)\b"
)
_ROUTED_FAILURE_OUTPUT_RE = re.compile(
    r"(?is)\b(?:429|rate[- ]limit(?:ed)?|too many requests|insufficient balance|no resource package|resource package|quota exhausted|credits? exhausted|please recharge|remoteprotocolerror|provider dropped|transport failure|http failure|auth failure|authentication failure|model not found|write failure|patch rejection|failed to execute|timed out|timeout)\b"
)
_ROUTE_MATRIX: dict[str, dict[str, dict[str, Any]]] = get_route_matrix()
_PRIMARY_MODEL_PATH_BY_TIER: dict[str, dict[str, str]] = get_primary_model_path_by_tier()
_ALLOWED_ROUTE_MODELS = get_allowed_route_models()
_DEFAULT_ROUTE_PATHS = dict(DEFAULT_ROUTE_PATHS)

_task_state_lock = threading.Lock()
_task_state: dict[str, dict[str, Any]] = {}


def _initial_route_attempts() -> dict[str, Any]:
    return {
        "primary_attempted": False,
        "primary_failed": False,
        "primary_failure_kind": None,
        "3b_primary_attempted": False,
        "3b_primary_failed": False,
        "3b_primary_failure_kind": None,
        "last_attempt_kind": None,
        "last_attempt_failed": False,
        "last_attempt_failure_kind": None,
    }


def _split_shell_segments(command: str, separators: tuple[str, ...]) -> list[str]:
    if not command:
        return []
    ordered = sorted(separators, key=len, reverse=True)
    parts: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    i = 0
    while i < len(command):
        char = command[i]
        if char == "'" and not in_double:
            in_single = not in_single
            current.append(char)
            i += 1
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            current.append(char)
            i += 1
            continue
        if not in_single and not in_double:
            matched = False
            for separator in ordered:
                if command.startswith(separator, i):
                    part = "".join(current).strip()
                    if part:
                        parts.append(part)
                    current = []
                    i += len(separator)
                    matched = True
                    break
            if matched:
                continue
        current.append(char)
        i += 1
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def _derive_git_permissions(user_message: str) -> dict[str, bool]:
    text = user_message or ""
    return {
        "commit": bool(_GIT_COMMIT_REQUEST_RE.search(text)),
        "push": bool(_GIT_PUSH_REQUEST_RE.search(text)),
        "branch": bool(_GIT_BRANCH_REQUEST_RE.search(text)),
        "mutate": bool(_GIT_MUTATION_REQUEST_RE.search(text)),
    }


def _format_session_lane_label(model: str = "", provider: str = "") -> str:
    normalized_model = str(model or "").strip()
    normalized_provider = str(provider or "").strip()
    if normalized_model and normalized_provider:
        return f"{normalized_model} via {normalized_provider}"
    return normalized_model or normalized_provider


def _normalize_skill_routing_hint(hint: Any) -> Optional[dict[str, Any]]:
    if not isinstance(hint, dict):
        return None

    raw_task_class = str(hint.get("task_class", "") or "").strip().lower()
    task_class = (
        raw_task_class
        if raw_task_class in {_TASK_CLASS_CODING, _TASK_CLASS_NON_CODING, _TASK_CLASS_MIXED}
        else _TASK_CLASS_CODING
    )

    raw_globs = hint.get("non_code_write_globs") or []
    if isinstance(raw_globs, str):
        raw_globs = [raw_globs]
    if not isinstance(raw_globs, list):
        raw_globs = []

    globs: list[str] = []
    seen: set[str] = set()
    for entry in raw_globs:
        value = str(entry or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        globs.append(value)

    return {
        "skill_name": str(hint.get("skill_name", "") or "").strip(),
        "skill_path": str(hint.get("skill_path", "") or "").strip(),
        "task_class": task_class,
        "non_code_write_globs": globs,
    }


def _normalize_active_skill_hints(hints: Optional[list[Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for raw in hints or []:
        hint = _normalize_skill_routing_hint(raw)
        if not isinstance(hint, dict):
            continue
        key = (
            hint.get("skill_name", ""),
            hint.get("skill_path", ""),
            hint.get("task_class", _TASK_CLASS_CODING),
            tuple(hint.get("non_code_write_globs", [])),
        )
        if key in seen:
            continue
        seen.add(key)
        normalized.append(hint)
    return normalized


def _derive_task_class(active_skill_hints: Optional[list[dict[str, Any]]]) -> str:
    classes = {
        str(item.get("task_class", "") or "").strip().lower()
        for item in (active_skill_hints or [])
        if isinstance(item, dict)
    }
    if _TASK_CLASS_MIXED in classes:
        return _TASK_CLASS_MIXED
    if _TASK_CLASS_CODING in classes and _TASK_CLASS_NON_CODING in classes:
        return _TASK_CLASS_MIXED
    if _TASK_CLASS_NON_CODING in classes:
        return _TASK_CLASS_NON_CODING
    return _TASK_CLASS_CODING


def _new_task_state(
    *,
    session_id: str = "",
    skills: Optional[list[str]] = None,
    active_skill_hints: Optional[list[dict[str, Any]]] = None,
    user_message: str = "",
    session_model: str = "",
    session_provider: str = "",
) -> dict[str, Any]:
    normalized_hints = _normalize_active_skill_hints(active_skill_hints)
    active_skills = list(skills or [])
    if DEFAULT_ROUTING_SKILL in active_skills:
        ability_requirements = detect_ability_requirements(user_message, normalized_hints)
    else:
        ability_requirements = {"lanes": {}, "post_visual_required": False, "detected_at": time.time()}
    return {
        "session_id": session_id or "",
        "session_model": str(session_model or "").strip(),
        "session_provider": str(session_provider or "").strip(),
        "session_lane_label": _format_session_lane_label(session_model, session_provider),
        "user_message": str(user_message or ""),
        "latest_user_message": str(user_message or ""),
        "skills": active_skills,
        "active_skill_hints": normalized_hints,
        "task_class": _derive_task_class(normalized_hints),
        "ability_requirements": ability_requirements,
        "ability_packets": [],
        "ability_cache": {},
        "ability_last_mutation_at": None,
        "visual_verification_required": bool(ability_requirements.get("post_visual_required")),
        "visual_verification_pending": False,
        "final_response_guard_attempts": 0,
        "completion_guard_attempts": 0,
        "last_mutation_class": None,
        "routed_mutation_succeeded": False,
        "enforced": True,
        "routed": False,
        "decision": None,
        "decision_line": None,
        "decision_error": None,
        "routed_plan": None,
        "route_attempts": _initial_route_attempts(),
        "verification_attempts": [],
        "entitlement_approvals": [],
        "custom_system_issues": [],
        "git_permissions": _derive_git_permissions(user_message),
        "blocked_tool_attempts": {},
        "last_blocked_tool": None,
        "last_block_reason": None,
        "updated_at": time.time(),
    }


def _deep_copy_jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _normalize_custom_system_issue_text(text: str, *, limit: int = 280) -> str:
    clean = " ".join(str(text or "").strip().split())
    if len(clean) <= limit:
        return clean
    return f"{clean[: limit - 3].rstrip()}..."


def _merge_issue_severity(current: str, incoming: str) -> str:
    rank = {"info": 0, "warning": 1, "error": 2}
    normalized_current = str(current or "warning").strip().lower()
    normalized_incoming = str(incoming or "warning").strip().lower()
    if normalized_current not in rank:
        normalized_current = "warning"
    if normalized_incoming not in rank:
        normalized_incoming = "warning"
    return normalized_incoming if rank[normalized_incoming] >= rank[normalized_current] else normalized_current


def _record_custom_system_issue_locked(
    state: dict[str, Any],
    *,
    component: str,
    summary: str,
    code: str = "",
    detail: str = "",
    severity: str = "warning",
) -> None:
    normalized_summary = _normalize_custom_system_issue_text(summary)
    if not normalized_summary:
        return

    issues = state.setdefault("custom_system_issues", [])
    if not isinstance(issues, list):
        issues = []
        state["custom_system_issues"] = issues

    normalized_component = _normalize_custom_system_issue_text(component or "routing", limit=64) or "routing"
    normalized_code = _normalize_custom_system_issue_text(code, limit=64).lower()
    normalized_detail = _normalize_custom_system_issue_text(detail, limit=320)
    normalized_severity = str(severity or "warning").strip().lower()
    if normalized_severity not in {"info", "warning", "error"}:
        normalized_severity = "warning"

    issue_key = (normalized_component.lower(), normalized_code, normalized_summary.lower())
    now = time.time()

    for item in issues:
        if not isinstance(item, dict):
            continue
        existing_key = (
            str(item.get("component", "")).strip().lower(),
            str(item.get("code", "")).strip().lower(),
            str(item.get("summary", "")).strip().lower(),
        )
        if existing_key != issue_key:
            continue
        item["count"] = int(item.get("count", 1) or 1) + 1
        item["severity"] = _merge_issue_severity(str(item.get("severity", "warning")), normalized_severity)
        if normalized_detail:
            item["detail"] = normalized_detail
        item["last_seen_at"] = now
        state["updated_at"] = now
        return

    issues.append(
        {
            "component": normalized_component,
            "code": normalized_code,
            "severity": normalized_severity,
            "summary": normalized_summary,
            "detail": normalized_detail or None,
            "count": 1,
            "first_seen_at": now,
            "last_seen_at": now,
        }
    )
    if len(issues) > _MAX_CUSTOM_SYSTEM_ISSUES:
        del issues[:-_MAX_CUSTOM_SYSTEM_ISSUES]
    state["updated_at"] = now


def _refresh_task_state(
    existing: Optional[dict[str, Any]],
    *,
    session_id: str = "",
    skills: Optional[list[str]] = None,
    active_skill_hints: Optional[list[dict[str, Any]]] = None,
    user_message: str = "",
    session_model: str = "",
    session_provider: str = "",
) -> dict[str, Any]:
    refreshed = _new_task_state(
        session_id=session_id,
        skills=skills,
        active_skill_hints=active_skill_hints,
        user_message=user_message,
        session_model=session_model,
        session_provider=session_provider,
    )
    if not isinstance(existing, dict):
        return refreshed

    preserve_live_route = (
        (bool(existing.get("routed")) or isinstance(existing.get("routed_plan"), dict))
        and (existing.get("session_id") == session_id)
    )
    if not preserve_live_route:
        return refreshed

    for key in (
        "ability_packets",
        "ability_cache",
        "ability_last_mutation_at",
        "visual_verification_pending",
        "last_mutation_class",
        "routed",
        "decision",
        "decision_line",
        "decision_error",
        "routed_plan",
        "route_attempts",
        "verification_attempts",
        "policy_version",
        "selected_route",
        "entitlement_approvals",
        "custom_system_issues",
        "routed_mutation_succeeded",
    ):
        if key in existing:
            refreshed[key] = _deep_copy_jsonable(existing.get(key))
    refreshed["visual_verification_required"] = bool(
        existing.get("visual_verification_required") or refreshed.get("visual_verification_required")
    )
    refreshed["final_response_guard_attempts"] = 0
    refreshed["completion_guard_attempts"] = 0
    refreshed["blocked_tool_attempts"] = {}
    refreshed["last_blocked_tool"] = None
    refreshed["last_block_reason"] = None
    refreshed["updated_at"] = time.time()
    return refreshed


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
    active_skill_hints: Optional[list[dict[str, Any]]] = None,
    user_message: str = "",
    session_model: str = "",
    session_provider: str = "",
) -> None:
    if not task_id:
        return
    with _task_state_lock:
        _purge_expired()
        existing = _task_state.get(task_id)
        _task_state[task_id] = _refresh_task_state(
            existing,
            session_id=session_id,
            skills=skills,
            active_skill_hints=active_skill_hints,
            user_message=user_message,
            session_model=session_model,
            session_provider=session_provider,
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


def _normalize_path_value(path: str) -> str:
    raw = str(path or "").strip().replace("\\", "/")
    raw = re.sub(r"/{2,}", "/", raw)
    return raw


def _lower_wrapped_path(path: str) -> str:
    normalized = _normalize_path_value(path).strip("/")
    return f"/{normalized.lower()}/" if normalized else "/"


def _path_parts_lower(path: str) -> list[str]:
    normalized = _normalize_path_value(path).strip("/")
    if not normalized:
        return []
    return [part.lower() for part in normalized.split("/") if part]


def _is_behavior_markdown_path(path: str) -> bool:
    normalized = _normalize_path_value(path)
    if not normalized:
        return False
    basename = Path(normalized).name.lower()
    if basename in _BEHAVIOR_MARKDOWN_FILENAMES:
        return True
    if basename.endswith(".mdc") and "/.cursor/rules/" in _lower_wrapped_path(normalized):
        return True
    return False


def _is_config_or_executable_text_path(path: str) -> bool:
    normalized = _normalize_path_value(path)
    if not normalized:
        return False
    basename = Path(normalized).name.lower()
    suffix = Path(normalized).suffix.lower()
    if suffix in _CONFIG_TEXT_SUFFIXES or suffix in _EXECUTABLE_TEXT_SUFFIXES:
        return True
    if basename.startswith(".env"):
        return True
    if basename.startswith("dockerfile"):
        return True
    if basename.startswith("compose"):
        return True
    if basename.startswith("docker-compose"):
        return True
    return False


def _is_code_sensitive_path(path: str) -> bool:
    wrapped = _lower_wrapped_path(path)
    return any(f"/{pattern.strip('/').lower()}/" in wrapped for pattern in _CODE_SENSITIVE_ROOT_PATTERNS)


def _is_docs_text_path(path: str) -> bool:
    normalized = _normalize_path_value(path)
    if not normalized:
        return False
    basename = Path(normalized).name.lower()
    suffix = Path(normalized).suffix.lower()
    if suffix in _DOC_TEXT_SUFFIXES:
        return True
    return basename.startswith(_DOCS_BASENAME_PREFIXES)


def _classify_path_work_class(path: str) -> str:
    normalized = _normalize_path_value(path)
    if not normalized:
        return _WORK_CLASS_UNKNOWN
    if _is_behavior_markdown_path(normalized):
        return _WORK_CLASS_BEHAVIOR
    if _is_config_or_executable_text_path(normalized):
        return _WORK_CLASS_CONFIG
    if _is_code_sensitive_path(normalized):
        return _WORK_CLASS_CODE
    if _is_docs_text_path(normalized):
        return _WORK_CLASS_DOCS
    if Path(normalized).suffix.lower() in _CODE_FILE_SUFFIXES:
        return _WORK_CLASS_CODE
    return _WORK_CLASS_UNKNOWN


def _collect_mutation_target_paths(tool_name: str, args: dict[str, Any]) -> list[str]:
    if tool_name == "write_file":
        path = _normalize_path_value(str(args.get("path", "") or ""))
        return [path] if path else []

    if tool_name != "patch":
        return []

    mode = str(args.get("mode", "replace") or "replace").strip().lower()
    if mode == "replace":
        path = _normalize_path_value(str(args.get("path", "") or ""))
        return [path] if path else []

    if mode != "patch":
        return []

    patch_text = str(args.get("patch", "") or "")
    return [
        _normalize_path_value(match.strip())
        for match in _PATCH_TARGET_RE.findall(patch_text)
        if _normalize_path_value(match.strip())
    ]


def _classify_file_mutation(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    targets = _collect_mutation_target_paths(tool_name, args)
    if not targets:
        return {"class": _WORK_CLASS_UNKNOWN, "targets": [], "target_classes": []}

    target_classes = [_classify_path_work_class(path) for path in targets]
    unique = set(target_classes)
    if _WORK_CLASS_BEHAVIOR in unique:
        mutation_class = _WORK_CLASS_BEHAVIOR if unique == {_WORK_CLASS_BEHAVIOR} else _WORK_CLASS_MIXED
    elif _WORK_CLASS_CODE in unique:
        mutation_class = _WORK_CLASS_CODE if unique == {_WORK_CLASS_CODE} else _WORK_CLASS_MIXED
    elif _WORK_CLASS_CONFIG in unique:
        mutation_class = _WORK_CLASS_CONFIG if unique == {_WORK_CLASS_CONFIG} else _WORK_CLASS_MIXED
    elif unique == {_WORK_CLASS_DOCS}:
        mutation_class = _WORK_CLASS_DOCS
    elif len(unique) == 1:
        mutation_class = next(iter(unique))
    else:
        mutation_class = _WORK_CLASS_MIXED

    return {
        "class": mutation_class,
        "targets": targets,
        "target_classes": target_classes,
    }


def _path_glob_candidates(path: str) -> list[str]:
    normalized = _normalize_path_value(path).strip("/")
    if not normalized:
        return []
    parts = [part for part in normalized.split("/") if part]
    candidates: list[str] = []
    for index in range(len(parts)):
        candidates.append("/".join(parts[index:]))
    return candidates


def _matches_non_code_globs(path: str, globs: list[str]) -> bool:
    candidates = [candidate.lower() for candidate in _path_glob_candidates(path)]
    for pattern in globs:
        normalized_pattern = str(pattern or "").strip().replace("\\", "/").strip("/").lower()
        if not normalized_pattern:
            continue
        for candidate in candidates:
            if fnmatchcase(candidate, normalized_pattern):
                return True
    return False


def _is_default_docs_authoring_path(path: str) -> bool:
    normalized = _normalize_path_value(path)
    if not normalized:
        return False
    basename = Path(normalized).name.lower()
    if basename.startswith(_DOCS_BASENAME_PREFIXES):
        return True
    return any(segment in _NON_CODE_PATH_SEGMENTS for segment in _path_parts_lower(normalized))


def _get_active_skill_hints(task_id: str) -> list[dict[str, Any]]:
    if not task_id:
        return []
    with _task_state_lock:
        _purge_expired()
        hints = _task_state.get(task_id, {}).get("active_skill_hints")
        return [dict(item) for item in hints if isinstance(item, dict)] if isinstance(hints, list) else []


def _set_last_mutation_class(task_id: str, mutation_class: str) -> None:
    if not task_id:
        return
    with _task_state_lock:
        _purge_expired()
        state = _task_state.get(task_id)
        if not state:
            return
        state["last_mutation_class"] = mutation_class
        state["updated_at"] = time.time()


def _is_allowed_docs_text_mutation(mutation: dict[str, Any], task_id: str) -> bool:
    if mutation.get("class") != _WORK_CLASS_DOCS:
        return False
    targets = mutation.get("targets") or []
    if not targets:
        return False
    for path in targets:
        # Large documentation packages should stay local as long as the write
        # remains plain docs text and is not under a code-sensitive root.
        if _is_code_sensitive_path(path):
            return False
    return True


def _describe_mutation_block_reason(mutation: dict[str, Any]) -> str:
    mutation_class = str(mutation.get("class", "") or _WORK_CLASS_UNKNOWN)
    target_classes = set(mutation.get("target_classes") or [])
    if mutation_class == _WORK_CLASS_BEHAVIOR:
        return "blocked because this is behavior-changing markdown"
    if mutation_class == _WORK_CLASS_CONFIG:
        return "blocked because this is config or executable text that can change runtime behavior"
    if mutation_class == _WORK_CLASS_CODE:
        return "blocked because this targets code or code-sensitive project paths"
    if mutation_class == _WORK_CLASS_DOCS:
        return "blocked because this docs/text write is outside the allowed non-code authoring scope"
    if mutation_class == _WORK_CLASS_MIXED:
        if _WORK_CLASS_BEHAVIOR in target_classes:
            return "blocked because this patch mixes behavior-changing markdown with other routed targets"
        if _WORK_CLASS_CODE in target_classes and _WORK_CLASS_DOCS in target_classes:
            return "blocked because this patch mixes docs and code targets"
        if _WORK_CLASS_CONFIG in target_classes and _WORK_CLASS_DOCS in target_classes:
            return "blocked because this patch mixes docs and config/executable-text targets"
        return "blocked because this patch mixes multiple work classes and defaults to routing"
    return "blocked because the target files could not be classified confidently"


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


def get_selected_route(task_id: str) -> dict[str, Any]:
    if not task_id:
        return {}
    with _task_state_lock:
        _purge_expired()
        selected = _task_state.get(task_id, {}).get("selected_route")
        if not isinstance(selected, dict):
            return {}
        return dict(selected)


def record_custom_system_issue(
    task_id: str,
    *,
    component: str,
    summary: str,
    code: str = "",
    detail: str = "",
    severity: str = "warning",
) -> None:
    if not task_id:
        return
    with _task_state_lock:
        _purge_expired()
        state = _task_state.get(task_id)
        if not isinstance(state, dict):
            return
        _record_custom_system_issue_locked(
            state,
            component=component,
            summary=summary,
            code=code,
            detail=detail,
            severity=severity,
        )


def get_custom_system_issues(task_id: str) -> list[dict[str, Any]]:
    if not task_id:
        return []
    with _task_state_lock:
        _purge_expired()
        issues = _task_state.get(task_id, {}).get("custom_system_issues")
        if not isinstance(issues, list):
            return []
        return [dict(item) for item in issues if isinstance(item, dict)]


def build_custom_system_issue_report(task_id: str, *, max_items: int = 5) -> str:
    issues = get_custom_system_issues(task_id)
    if not issues:
        return ""

    lines = ["Custom system notes:"]
    for item in issues[-max_items:]:
        component = str(item.get("component", "routing") or "routing")
        summary = str(item.get("summary", "") or "").strip()
        detail = str(item.get("detail", "") or "").strip()
        count = int(item.get("count", 1) or 1)
        prefix = f"- `{component}`"
        if count > 1:
            prefix += f" ({count}x)"
        line = f"{prefix}: {summary}"
        if detail and detail != summary:
            line += f"; {detail}"
        lines.append(line)
    return "\n".join(lines)


def has_task_entitlement_approval(task_id: str, approval_key: str) -> bool:
    if not task_id or not approval_key:
        return False
    with _task_state_lock:
        _purge_expired()
        approvals = _task_state.get(task_id, {}).get("entitlement_approvals") or []
        return str(approval_key) in {str(item) for item in approvals}


def record_task_entitlement_approval(task_id: str, approval_key: str) -> None:
    if not task_id or not approval_key:
        return
    with _task_state_lock:
        _purge_expired()
        state = _task_state.get(task_id)
        if not state:
            return
        approvals = [str(item) for item in (state.get("entitlement_approvals") or []) if str(item or "").strip()]
        if approval_key not in approvals:
            approvals.append(approval_key)
        state["entitlement_approvals"] = approvals
        state["updated_at"] = time.time()


def update_selected_route_entitlement(
    task_id: str,
    *,
    entitlement: Optional[dict[str, Any]] = None,
    effective_targets: Optional[list[dict[str, Any]]] = None,
    degraded: Optional[bool] = None,
    failure_reason: str = "",
) -> None:
    if not task_id:
        return
    with _task_state_lock:
        _purge_expired()
        state = _task_state.get(task_id)
        if not state:
            return
        selected = state.get("selected_route")
        if not isinstance(selected, dict):
            selected = {}
            state["selected_route"] = selected
        if entitlement is not None:
            selected["entitlement"] = _deep_copy_jsonable(entitlement)
        if effective_targets is not None:
            selected["effective_targets"] = _deep_copy_jsonable(effective_targets)
        if degraded is not None:
            selected["degraded"] = bool(degraded)
        if failure_reason:
            selected["failure_reason"] = str(failure_reason)
        elif failure_reason == "":
            selected.pop("failure_reason", None)
        state["updated_at"] = time.time()


def get_routed_plan_state(task_id: str) -> Optional[dict[str, Any]]:
    if not task_id:
        return None
    with _task_state_lock:
        _purge_expired()
        plan = _task_state.get(task_id, {}).get("routed_plan")
        if not isinstance(plan, dict):
            return None
        return json.loads(json.dumps(plan, ensure_ascii=False))


def set_routed_plan_state(task_id: str, plan: Optional[dict[str, Any]]) -> None:
    if not task_id:
        return
    with _task_state_lock:
        _purge_expired()
        state = _task_state.get(task_id)
        if not state:
            return
        state["routed_plan"] = json.loads(json.dumps(plan, ensure_ascii=False)) if isinstance(plan, dict) else None
        state["updated_at"] = time.time()


def clear_routed_plan_state(task_id: str) -> None:
    set_routed_plan_state(task_id, None)


def hydrate_routed_plan_from_persistence(task_id: str, *, session_id: str = "", plan_id: str = "") -> bool:
    """Restore route lock + routed_plan state from state.db when routing-layer is active."""
    if not task_id:
        return False
    with _task_state_lock:
        _purge_expired()
        state = _task_state.get(task_id)
        if not state or DEFAULT_ROUTING_SKILL not in (state.get("skills") or []):
            return False

    try:
        from agent.routing_plan_store import load_plan_snapshot

        record = load_plan_snapshot(plan_id=plan_id, task_id=task_id, session_id=session_id)
    except Exception:
        return False
    if not isinstance(record, dict):
        return False

    parent_decision = record.get("parent_decision") if isinstance(record.get("parent_decision"), dict) else {}
    plan = record.get("plan") if isinstance(record.get("plan"), dict) else None
    if not parent_decision or not plan:
        return False
    route_validation = validate_route_choice(
        str(parent_decision.get("tier", "")),
        str(parent_decision.get("path", "")),
        str(parent_decision.get("model", "")),
    )
    if not route_validation.ok or not route_validation.profile:
        return False

    policy = load_routing_policy()
    with _task_state_lock:
        _purge_expired()
        state = _task_state.get(task_id)
        if not state or DEFAULT_ROUTING_SKILL not in (state.get("skills") or []):
            return False
        state["session_id"] = session_id or str(record.get("session_id") or state.get("session_id") or "")
        state["routed"] = True
        state["decision"] = dict(parent_decision)
        state["policy_version"] = policy.version or ROUTING_POLICY_VERSION
        state["selected_route"] = {
            "policy_version": policy.version or ROUTING_POLICY_VERSION,
            "tier": route_validation.tier,
            "path": route_validation.path,
            "model": str(parent_decision.get("model", "")),
            "profile": route_validation.profile,
            "entitlement": None,
            "effective_targets": [],
            "degraded": False,
        }
        state["decision_error"] = None
        state["routed_plan"] = json.loads(json.dumps(plan, ensure_ascii=False))
        state["updated_at"] = time.time()
    return True


def get_session_lane_context(task_id: str) -> dict[str, str]:
    if not task_id:
        return {"model": "", "provider": "", "label": ""}
    with _task_state_lock:
        _purge_expired()
        state = _task_state.get(task_id, {})
        return {
            "model": str(state.get("session_model", "") or ""),
            "provider": str(state.get("session_provider", "") or ""),
            "label": str(state.get("session_lane_label", "") or ""),
        }


def get_task_class(task_id: str) -> str:
    if not task_id:
        return _TASK_CLASS_CODING
    with _task_state_lock:
        _purge_expired()
        return str(_task_state.get(task_id, {}).get("task_class", _TASK_CLASS_CODING) or _TASK_CLASS_CODING)


def get_active_skill_hints(task_id: str) -> list[dict[str, Any]]:
    return _get_active_skill_hints(task_id)


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
    path = _normalize_route_path(str(decision.get("path", "") or ""))
    attempts = get_route_attempts(task_id)
    profile = _get_route_profile(tier, path, model)
    if not profile:
        return []

    primary = dict(profile["primary"])
    fallbacks = [dict(item) for item in profile.get("fallbacks", [])]
    if model == _normalize_route_model(primary["label"]):
        if attempts.get("primary_failed") and fallbacks:
            return fallbacks
        return [primary, *fallbacks]

    for fallback in fallbacks:
        if model == _normalize_route_model(fallback["label"]):
            return [fallback]

    return [primary, *fallbacks]


def get_verification_attempts(task_id: str) -> list[dict[str, Any]]:
    if not task_id:
        return []
    with _task_state_lock:
        _purge_expired()
        attempts = _task_state.get(task_id, {}).get("verification_attempts")
        if not isinstance(attempts, list):
            return []
        return [dict(item) for item in attempts if isinstance(item, dict)]


def get_routing_status_snapshot(task_id: str) -> dict[str, Any]:
    if not task_id:
        return {
            "task_id": "",
            "active": False,
            "route_locked": False,
            "decision": None,
            "decision_error": None,
            "git_permissions": {},
            "route_attempts": _initial_route_attempts(),
            "verification_attempts": [],
            "routed_plan": None,
            "selected_route": {},
            "custom_system_issues": [],
        }

    with _task_state_lock:
        _purge_expired()
        state = _task_state.get(task_id)
        if not isinstance(state, dict):
            return {
                "task_id": task_id,
                "active": False,
                "route_locked": False,
                "decision": None,
                "decision_error": None,
                "git_permissions": {},
                "route_attempts": _initial_route_attempts(),
                "verification_attempts": [],
                "routed_plan": None,
                "selected_route": {},
                "custom_system_issues": [],
            }
        snapshot = {
            "task_id": task_id,
            "active": bool(state.get("enforced")),
            "enforced": bool(state.get("enforced")),
            "session_id": str(state.get("session_id", "") or ""),
            "session_lane": {
                "model": str(state.get("session_model", "") or ""),
                "provider": str(state.get("session_provider", "") or ""),
                "label": str(state.get("session_lane_label", "") or ""),
            },
            "latest_user_message": str(state.get("latest_user_message", "") or state.get("user_message", "") or ""),
            "route_locked": bool(state.get("routed")),
            "decision": _deep_copy_jsonable(state.get("decision")) if isinstance(state.get("decision"), dict) else None,
            "selected_route": _deep_copy_jsonable(state.get("selected_route")) if isinstance(state.get("selected_route"), dict) else {},
            "decision_error": str(state.get("decision_error", "") or "") or None,
            "git_permissions": dict(state.get("git_permissions") or {}),
            "blocked_tool_attempts": dict(state.get("blocked_tool_attempts") or {}),
            "last_blocked_tool": str(state.get("last_blocked_tool", "") or "") or None,
            "last_block_reason": str(state.get("last_block_reason", "") or "") or None,
            "route_attempts": _deep_copy_jsonable(state.get("route_attempts") or _initial_route_attempts()),
            "verification_attempts": [
                dict(item) for item in state.get("verification_attempts", []) if isinstance(item, dict)
            ],
            "entitlement_approvals": list(state.get("entitlement_approvals") or []),
            "custom_system_issues": [
                dict(item) for item in state.get("custom_system_issues", []) if isinstance(item, dict)
            ],
            "visual_verification_required": bool(state.get("visual_verification_required")),
            "visual_verification_pending": bool(state.get("visual_verification_pending")),
            "routed_mutation_succeeded": bool(state.get("routed_mutation_succeeded")),
            "expires_at": float(state.get("updated_at") or 0.0) + _TASK_STATE_TTL_SECONDS,
        }
        plan = state.get("routed_plan")
    if isinstance(plan, dict):
        try:
            from agent.routing_plan import public_plan_state

            public = public_plan_state(plan)
            snapshot["routed_plan"] = {
                "plan_id": public.get("plan_id"),
                "status": public.get("status"),
                "next_node": public.get("next_node"),
                "node_statuses": public.get("node_statuses"),
            }
        except Exception:
            snapshot["routed_plan"] = {"plan_id": plan.get("plan_id"), "status": plan.get("status")}
    else:
        snapshot["routed_plan"] = None
    snapshot["seconds_until_expiry"] = max(0.0, round(float(snapshot["expires_at"]) - time.time(), 3))
    return snapshot


def get_ability_requirements(task_id: str) -> dict[str, Any]:
    if not task_id:
        return {"lanes": {}, "post_visual_required": False}
    with _task_state_lock:
        _purge_expired()
        requirements = _task_state.get(task_id, {}).get("ability_requirements")
        if not isinstance(requirements, dict):
            return {"lanes": {}, "post_visual_required": False}
        return json.loads(json.dumps(requirements, ensure_ascii=False))


def get_ability_packets(task_id: str, *, include_stale: bool = False) -> list[dict[str, Any]]:
    if not task_id:
        return []
    with _task_state_lock:
        _purge_expired()
        packets = _task_state.get(task_id, {}).get("ability_packets")
        if not isinstance(packets, list):
            return []
        result = [dict(item) for item in packets if isinstance(item, dict)]
        if include_stale:
            return result
        return [item for item in result if not item.get("stale")]


def get_cached_ability_packet(
    task_id: str,
    cache_key: str,
    *,
    ttl_seconds: int = VISUAL_CACHE_TTL_SECONDS,
) -> Optional[dict[str, Any]]:
    if not task_id or not cache_key:
        return None
    with _task_state_lock:
        _purge_expired()
        state = _task_state.get(task_id)
        if not state:
            return None
        cache = state.get("ability_cache")
        if not isinstance(cache, dict):
            return None
        packet = cache.get(cache_key)
        if not isinstance(packet, dict) or packet.get("stale"):
            return None
        generated_at = float(packet.get("generated_at") or 0.0)
        if ttl_seconds > 0 and generated_at and (time.time() - generated_at) > ttl_seconds:
            packet["stale"] = True
            return None
        cached = dict(packet)
        cached["cached"] = True
        return cached


def record_ability_packet(task_id: str, packet: dict[str, Any]) -> None:
    if not task_id or not isinstance(packet, dict):
        return
    normalized = dict(packet)
    normalized.setdefault("task_id", task_id)
    normalized["lanes"] = normalize_lanes(normalized.get("lanes") or [])
    normalized.setdefault("phase", "pre")
    normalized.setdefault("status", "success" if normalized.get("success") else "unavailable")
    normalized.setdefault("generated_at", time.time())
    normalized.setdefault("stale", False)
    cache_key = str(normalized.get("cache_key") or "")
    with _task_state_lock:
        _purge_expired()
        state = _task_state.get(task_id)
        if not state:
            return
        packets = state.setdefault("ability_packets", [])
        if cache_key:
            packets[:] = [
                item for item in packets
                if not (isinstance(item, dict) and str(item.get("cache_key") or "") == cache_key)
            ]
        packets.append(normalized)
        if len(packets) > 30:
            del packets[:-30]
        if cache_key:
            cache = state.setdefault("ability_cache", {})
            if isinstance(cache, dict):
                cache[cache_key] = dict(normalized)
        state["updated_at"] = time.time()


def clear_ability_cache(task_id: str) -> None:
    if not task_id:
        return
    with _task_state_lock:
        _purge_expired()
        state = _task_state.get(task_id)
        if not state:
            return
        state["ability_packets"] = []
        state["ability_cache"] = {}
        state["updated_at"] = time.time()


def mark_ability_evidence_stale(task_id: str, *, reason: str = "") -> None:
    if not task_id:
        return
    with _task_state_lock:
        _purge_expired()
        state = _task_state.get(task_id)
        if not state:
            return
        state["ability_last_mutation_at"] = time.time()
        for bucket_name in ("ability_packets",):
            bucket = state.get(bucket_name)
            if not isinstance(bucket, list):
                continue
            for packet in bucket:
                if not isinstance(packet, dict):
                    continue
                if "visual" in normalize_lanes(packet.get("lanes") or []):
                    packet["stale"] = True
                    if reason:
                        packet["stale_reason"] = reason
        cache = state.get("ability_cache")
        if isinstance(cache, dict):
            for packet in cache.values():
                if isinstance(packet, dict) and "visual" in normalize_lanes(packet.get("lanes") or []):
                    packet["stale"] = True
                    if reason:
                        packet["stale_reason"] = reason
        state["updated_at"] = time.time()


def _mark_visual_verification_pending_locked(state: dict[str, Any], *, reason: str) -> None:
    if not state.get("visual_verification_required"):
        return
    state["visual_verification_pending"] = True
    state["final_response_guard_attempts"] = 0
    for packet in state.get("ability_packets", []):
        if isinstance(packet, dict) and "visual" in normalize_lanes(packet.get("lanes") or []):
            packet["stale"] = True
            packet["stale_reason"] = reason
    cache = state.get("ability_cache")
    if isinstance(cache, dict):
        for packet in cache.values():
            if isinstance(packet, dict) and "visual" in normalize_lanes(packet.get("lanes") or []):
                packet["stale"] = True
                packet["stale_reason"] = reason


def mark_routed_plan_node_success(task_id: str) -> None:
    if not task_id:
        return
    with _task_state_lock:
        _purge_expired()
        state = _task_state.get(task_id)
        if not state:
            return
        _mark_visual_verification_pending_locked(state, reason="routed_plan node succeeded")
        state["routed_mutation_succeeded"] = True
        state["updated_at"] = time.time()


def get_ability_handoff(task_id: str) -> str:
    return compact_packets_for_handoff(get_ability_packets(task_id))


def _record_ability_tool_payload(task_id: str, payload: dict[str, Any], *, fallback_lane: str = "") -> None:
    packets = payload.get("packets") if isinstance(payload, dict) else None
    if isinstance(packets, list):
        for packet in packets:
            if isinstance(packet, dict):
                record_ability_packet(task_id, packet)
        return
    if fallback_lane:
        console_errors: list[Any] = []
        browser_payload = payload.get("browser") if isinstance(payload, dict) else None
        if isinstance(browser_payload, dict):
            console = browser_payload.get("console")
            if isinstance(console, dict):
                errors = console.get("errors") or console.get("messages") or []
                if isinstance(errors, list):
                    console_errors = errors
                elif console.get("total_errors"):
                    console_errors = [console]
        packet = make_ability_packet(
            task_id=task_id,
            lanes=[fallback_lane],
            phase=str(payload.get("phase") or "pre"),
            status="success" if payload.get("success") else "unavailable",
            summary=str(payload.get("visual_summary") or payload.get("summary") or payload.get("error") or ""),
            findings=[],
            constraints=[payload.get("error")] if payload.get("error") else [],
            url_or_path=str(payload.get("url") or payload.get("image_url") or ""),
            screenshot_path=str(payload.get("screenshot_path") or ""),
            console_errors=console_errors,
            artifact_paths=[str(payload.get("screenshot_path"))] if payload.get("screenshot_path") else [],
            health={},
            cache_key=str(payload.get("cache_key") or ""),
            cached=bool(payload.get("cached")),
            stale=bool(payload.get("stale")),
            generated_at=time.time(),
        )
        record_ability_packet(task_id, packet)


def final_response_block_reason(task_id: str, assistant_text: str) -> Optional[str]:
    if not task_id or not is_active_for_task(task_id):
        return None
    text = str(assistant_text or "")
    with _task_state_lock:
        _purge_expired()
        state = _task_state.get(task_id)
        if not state:
            return None
        if state.get("visual_verification_pending"):
            packets = [dict(item) for item in state.get("ability_packets", []) if isinstance(item, dict)]
            if visual_post_verified(packets):
                state["visual_verification_pending"] = False
                state["updated_at"] = time.time()
            else:
                explicit_unavailable = re.search(r"(?is)\bvisual verification\b.*\b(unavailable|blocked|could not|unable)\b", text)
                cites_blocker = re.search(r"(?is)\b(browser|vision|screenshot|webgl|local|ssrf|expensive|cpu|gpu|unavailable|blocked)\b", text)
                if explicit_unavailable and cites_blocker:
                    state["visual_verification_pending"] = False
                    state["updated_at"] = time.time()
                else:
                    attempts = int(state.get("final_response_guard_attempts") or 0)
                    if attempts < 2:
                        state["final_response_guard_attempts"] = attempts + 1
                        _record_custom_system_issue_locked(
                            state,
                            component="routing_guard",
                            code="visual_verification_gate",
                            summary="Final response was held until a post-fix visual verification packet or explicit visual blocker was provided.",
                            severity="warning",
                        )
                        return (
                            "Routing guard requires a post-fix visual verification packet before a fixed/complete final answer. "
                            "Call `ability_context` with `phase=\"post\"` for the visual lane, or explicitly state that visual "
                            "verification was unavailable and cite the concrete blocker."
                        )

        if not state.get("routed_mutation_succeeded"):
            return None
        attempts = [dict(item) for item in state.get("verification_attempts", []) if isinstance(item, dict)]
        if any(bool(item.get("success")) for item in attempts):
            state["completion_guard_attempts"] = 0
            state["updated_at"] = time.time()
            return None
        if not _FIXED_CLAIM_RE.search(text):
            return None
        if _UNVERIFIED_COMPLETION_RE.search(text):
            state["completion_guard_attempts"] = 0
            state["updated_at"] = time.time()
            return None
        completion_attempts = int(state.get("completion_guard_attempts") or 0)
        if completion_attempts >= 2:
            return None
        state["completion_guard_attempts"] = completion_attempts + 1
        _record_custom_system_issue_locked(
            state,
            component="routing_guard",
            code="completion_verification_gate",
            summary="Final response was held until local verification succeeded or the response explicitly described the verification blocker and residual risk.",
            severity="warning",
        )
        return (
            "Routing guard requires successful local verification before you claim the task is fixed/resolved/shipped. "
            "Run an approved local verification command, or explicitly state that verification could not be completed and describe the residual risk."
        )


def _normalize_route_model(model: str) -> str:
    return normalize_route_model(model)


def _normalize_route_path(path: str) -> str:
    return normalize_route_path(path)


def _infer_route_path(tier: str, normalized_model: str) -> str:
    return infer_route_path(tier, normalized_model)


def _get_route_profile(tier: str, path: str, normalized_model: str = "") -> Optional[dict[str, Any]]:
    normalized_tier = (tier or "").upper()
    normalized_path = _normalize_route_path(path)
    if not normalized_path:
        normalized_path = _infer_route_path(normalized_tier, normalized_model)
    return get_route_matrix().get(normalized_tier, {}).get(normalized_path)


def _format_route_label(tier: str, model: str) -> str:
    return f"TIER: {tier} | MODEL: {model}"


def _format_route_label_with_path(tier: str, path: str, model: str) -> str:
    normalized_path = _normalize_route_path(path)
    if normalized_path:
        return f"TIER: {tier} | PATH: {normalized_path} | MODEL: {model}"
    return _format_route_label(tier, model)


def _format_allowed_route_models(tier: str) -> str:
    labels = get_allowed_route_models().get((tier or "").upper(), ())
    return ", ".join(f"`{label}`" for label in labels)


def _format_allowed_route_paths(tier: str) -> str:
    labels = tuple(get_route_matrix().get((tier or "").upper(), {}).keys())
    return ", ".join(f"`{label}`" for label in labels)


def _format_route_correction_hint(tier: str, path: str, normalized_model: str) -> str:
    route_matrix = get_route_matrix()
    hints: list[str] = []
    normalized_path = _normalize_route_path(path)
    normalized_tier = (tier or "").upper()

    if normalized_path:
        for candidate_tier, paths in route_matrix.items():
            profile = paths.get(normalized_path)
            if not profile or candidate_tier == normalized_tier:
                continue
            primary_label = str(profile.get("primary", {}).get("label", "") or "")
            if primary_label:
                hints.append(
                    f"`{normalized_path}` belongs to {candidate_tier}; use "
                    f"`{_format_route_label_with_path(candidate_tier, normalized_path, primary_label)}`."
                )

    if normalized_model:
        for candidate_tier, paths in route_matrix.items():
            for candidate_path, profile in paths.items():
                targets = [profile.get("primary", {}), *list(profile.get("fallbacks", []) or [])]
                for target in targets:
                    label = str(target.get("label", "") or "")
                    if normalized_model != _normalize_route_model(label):
                        continue
                    hints.append(
                        f"`{label}` is valid on {candidate_tier}/{candidate_path}; use "
                        f"`{_format_route_label_with_path(candidate_tier, candidate_path, label)}`."
                    )

    deduped: list[str] = []
    seen: set[str] = set()
    for hint in hints:
        if hint in seen:
            continue
        seen.add(hint)
        deduped.append(hint)
    return f" Correction: {' '.join(deduped)}" if deduped else ""


def _set_decision_error(state: dict[str, Any], message: str) -> None:
    state["decision_error"] = message
    _record_custom_system_issue_locked(
        state,
        component="routing_guard",
        code="routing_decision_error",
        summary=message,
        severity="warning",
    )


def _codex_reclassify_block_reason(
    task_id: str,
    state: dict[str, Any],
    *,
    decision: dict[str, str],
    profile: Optional[dict[str, Any]],
) -> Optional[str]:
    current = state.get("decision") if isinstance(state.get("decision"), dict) else None
    if not isinstance(current, dict):
        return None
    current_key = (current.get("tier"), current.get("path"), current.get("model"))
    new_key = (decision.get("tier"), decision.get("path"), decision.get("model"))
    if current_key == new_key:
        return None
    if not isinstance(profile, dict):
        return None
    primary = profile.get("primary") if isinstance(profile.get("primary"), dict) else {}
    if str(primary.get("executor", "") or "").strip().lower() != "codex":
        return None

    selected_route = state.get("selected_route") if isinstance(state.get("selected_route"), dict) else {}
    entitlement = selected_route.get("entitlement") if isinstance(selected_route.get("entitlement"), dict) else {}
    evaluations = entitlement.get("evaluations") if isinstance(entitlement.get("evaluations"), list) else []
    blocked_reason = ""
    for item in evaluations:
        if not isinstance(item, dict):
            continue
        target = item.get("target") if isinstance(item.get("target"), dict) else {}
        provider = str(target.get("provider", "") or "").strip().lower()
        spend_class = str(item.get("spend_class", "") or "").strip().lower()
        reason = str(item.get("reason", "") or "").strip().lower()
        if provider != "openai-codex" and spend_class != "openai":
            continue
        if reason in {"quota_unknown", "locked_paid_spend", "included_quota_exhausted"}:
            blocked_reason = reason
            break
    if not blocked_reason:
        return None

    return (
        f"Routing guard blocked Codex reclassification for task "
        f"{task_id}: the current route already observed "
        f"Codex entitlement state `{blocked_reason}`. Do not switch to a Codex-primary route "
        "hoping a different Codex model or tier will bypass the same quota gate. "
        "Report fallback chain exhausted or wait for a refreshed Codex quota snapshot."
    )


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
        "path": _normalize_route_path(match.group("path") or ""),
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
        current = state.get("decision")
        if not decision["path"]:
            if (
                isinstance(current, dict)
                and str(current.get("tier", "")).upper() == decision["tier"]
                and current.get("path")
            ):
                decision["path"] = _normalize_route_path(str(current.get("path", "")))
            else:
                decision["path"] = _infer_route_path(decision["tier"], normalized_model)
        route_validation = validate_route_choice(decision["tier"], decision["path"], decision["model"])
        policy = load_routing_policy()
        route_matrix = get_route_matrix()
        profile = route_validation.profile
        if decision["path"] not in route_matrix.get(decision["tier"], {}):
            correction = _format_route_correction_hint(decision["tier"], decision["path"], normalized_model)
            _set_decision_error(
                state,
                (
                    f"Routing guard blocked invalid routing path for task {task_id}: "
                    f"`{decision['path']}` is not allowed for {decision['tier']}. "
                    f"Allowed paths for {decision['tier']} are: {_format_allowed_route_paths(decision['tier'])}."
                    f"{correction}"
                ),
            )
            return False
        allowed_models = get_allowed_route_models().get(decision["tier"], ())
        if normalized_model not in {_normalize_route_model(label) for label in allowed_models}:
            correction = _format_route_correction_hint(decision["tier"], decision["path"], normalized_model)
            _set_decision_error(
                state,
                (
                    f"Routing guard blocked invalid routing decision for task {task_id}: "
                    f"`{_format_route_label(decision['tier'], decision['model'])}` is not allowed. "
                    f"Allowed model labels for {decision['tier']} are: {_format_allowed_route_models(decision['tier'])}."
                    f"{correction}"
                ),
            )
            return False
        if not profile:
            _set_decision_error(
                state,
                (
                    f"Routing guard blocked invalid routing decision for task {task_id}: "
                    f"`{_format_route_label_with_path(decision['tier'], decision['path'], decision['model'])}` "
                    "does not map to a known route profile."
                ),
            )
            return False
        profile_models = {
            _normalize_route_model(profile["primary"]["label"]),
            *(_normalize_route_model(item["label"]) for item in profile.get("fallbacks", [])),
        }
        if normalized_model not in profile_models:
            correction = _format_route_correction_hint(decision["tier"], decision["path"], normalized_model)
            _set_decision_error(
                state,
                (
                    f"Routing guard blocked invalid routing decision for task {task_id}: "
                    f"`{_format_route_label_with_path(decision['tier'], decision['path'], decision['model'])}` "
                    f"does not match the allowed models for path `{decision['path']}`."
                    f"{correction}"
                ),
            )
            return False
        if isinstance(current, dict):
            current_key = (current.get("tier"), current.get("path"), current.get("model"))
            new_key = (decision["tier"], decision["path"], decision["model"])
            if current_key != new_key and not _RECLASSIFY_MARKER_RE.search(clean):
                _set_decision_error(
                    state,
                    (
                        f"Routing guard blocked route drift for task {task_id}: current route is "
                        f"`{_format_route_label_with_path(str(current.get('tier', '')), str(current.get('path', '')), str(current.get('model', '')))}` "
                        f"but the latest assistant output attempted `{_format_route_label_with_path(decision['tier'], decision['path'], decision['model'])}` "
                        "without `RECLASSIFY:`. Emit an explicit `RECLASSIFY:` line or stay on the current route."
                    ),
                )
                return False
            if current_key != new_key:
                codex_block = _codex_reclassify_block_reason(
                    task_id,
                    state,
                    decision=decision,
                    profile=profile,
                )
                if codex_block:
                    _set_decision_error(state, codex_block)
                    return False
                state["route_attempts"] = _initial_route_attempts()
        state["session_id"] = session_id or state.get("session_id", "")
        state["routed"] = True
        state["decision"] = decision
        state["policy_version"] = policy.version or ROUTING_POLICY_VERSION
        state["selected_route"] = {
            "policy_version": policy.version or ROUTING_POLICY_VERSION,
            "tier": decision["tier"],
            "path": decision["path"],
            "model": decision["model"],
            "profile": profile,
            "entitlement": None,
            "effective_targets": [],
            "degraded": False,
        }
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
        if _HERMES_MINIMAX_MODEL_RE.search(normalized) and _HERMES_MINIMAX_PROVIDER_RE.search(normalized):
            return "hermes_minimax_m27"
        if _HERMES_MIMO_MODEL_RE.search(normalized) and _HERMES_NOUS_PROVIDER_RE.search(normalized):
            return "hermes_nous_mimo_v2_pro"
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
    path = _normalize_route_path(str(decision.get("path", "") or ""))
    with _task_state_lock:
        _purge_expired()
        attempts = dict(_task_state.get(task_id, {}).get("route_attempts") or {})
    profile = _get_route_profile(str(tier or ""), path, _normalize_route_model(str(decision.get("model", ""))))
    if not profile:
        return None

    primary_kind = str(profile["primary"]["kind"])
    fallback_kinds = [str(item["kind"]) for item in profile.get("fallbacks", [])]
    decision_model = _normalize_route_model(str(decision.get("model", "")))

    if decision_model == _normalize_route_model(profile["primary"]["label"]):
        if route_kind == primary_kind:
            return None
        if route_kind in fallback_kinds:
            if not attempts.get("primary_failed"):
                return (
                    f"Routing guard blocked {decision.get('tier')} backup on path `{path}`: attempt the primary route "
                    f"`{profile['primary']['label']}` first and fall back only after that primary attempt fails."
                )
            return None
        return (
            f"Routing guard blocked routed model mismatch: active path `{path}` must execute through "
            f"`{profile['primary']['label']}` or its defined fallback chain."
        )

    for fallback in profile.get("fallbacks", []):
        if decision_model == _normalize_route_model(fallback["label"]):
            if route_kind != fallback["kind"]:
                return (
                    f"Routing guard blocked routed model mismatch: the active route is "
                    f"`{fallback['label']}` on path `{path}`. Emit `RECLASSIFY:` if you intend to switch routes again."
                )
            return None

    return None


def _record_blocked_tool(task_id: str, tool_name: str, reason: str) -> str:
    if not task_id or not reason:
        return reason
    count = 1
    with _task_state_lock:
        _purge_expired()
        state = _task_state.get(task_id)
        if not isinstance(state, dict):
            return reason
        attempts = state.setdefault("blocked_tool_attempts", {})
        if not isinstance(attempts, dict):
            attempts = {}
            state["blocked_tool_attempts"] = attempts
        count = int(attempts.get(tool_name, 0) or 0) + 1
        attempts[tool_name] = count
        state["last_blocked_tool"] = str(tool_name or "")
        state["last_block_reason"] = str(reason or "")
        _record_custom_system_issue_locked(
            state,
            component="routing_guard",
            code=f"blocked_{str(tool_name or 'tool').strip().lower() or 'tool'}",
            summary=f"{tool_name}: {reason}",
            severity="warning",
        )

    if count < 2:
        return reason

    next_step = "follow the routing guard guidance in the blocker."
    if tool_name in {"patch", "write_file", "delegate_task"}:
        next_step = "use `routed_exec` or `routed_plan` for implementation work."
    elif tool_name in {"routed_exec", "routed_plan"} and "emit a routing decision" in reason.lower():
        next_step = "emit the routing decision line before routed execution."
    elif tool_name == "terminal":
        lowered = reason.lower()
        if "git " in lowered:
            next_step = "inspect `routing_status` and obtain an explicit user request for the blocked git action."
        elif "before a routing decision" in lowered:
            next_step = "stay read-only or emit the routing decision line."
        else:
            next_step = "use `routed_exec`/`routed_plan` for implementation, or run an approved local verification command."
    elif tool_name == "execute_code":
        next_step = "stay on the routed path: use `routed_exec` for implementation and approved local `terminal` commands for verification."

    return (
        f"{reason} Repeated block ({count}x) on `{tool_name}`. "
        f"Stop retrying the same path. Next valid action: {next_step}"
    )


def _format_git_permission_summary(permissions: dict[str, Any]) -> str:
    labels = {
        "commit": "commit",
        "push": "push",
        "branch": "branch-create/switch",
        "mutate": "history/worktree-mutate",
    }
    allowed = [label for key, label in labels.items() if permissions.get(key)]
    if allowed:
        return "Allowed git actions for this task: " + ", ".join(allowed) + "."
    return "No mutating git actions are currently allowed for this task."


def _validate_git_terminal_command(command: str, task_id: str, *, session_id: str = "") -> Optional[str]:
    raw = (command or "").strip()
    if not raw:
        return None

    with _task_state_lock:
        _purge_expired()
        state = _task_state.get(task_id, {})
        permissions = dict(state.get("git_permissions") or {})
        recorded_session_id = str(state.get("session_id", "") or "")
        latest_user_message = str(state.get("latest_user_message", "") or state.get("user_message", "") or "")

    stale_state = bool(session_id and recorded_session_id and recorded_session_id != session_id)
    source_note = (
        f"Block source: stale task state recorded for session `{recorded_session_id}` while the current session is `{session_id}`."
        if stale_state
        else "Block source: the latest user turn did not explicitly authorize that git action."
    )
    permission_summary = _format_git_permission_summary(permissions)
    latest_message_hint = ""
    if latest_user_message:
        latest_message_hint = f" Latest recorded user turn: {latest_user_message[:160]!r}."

    if _GIT_COMMIT_RE.search(raw) and not permissions.get("commit"):
        return (
            "Routing guard blocked `git commit`: commits require an explicit user request. "
            f"{permission_summary} {source_note}{latest_message_hint} "
            "Inspect `routing_status` if this looks wrong."
        )
    if _GIT_PUSH_RE.search(raw) and not permissions.get("push"):
        return (
            "Routing guard blocked `git push`: pushes require an explicit user request. "
            f"{permission_summary} {source_note}{latest_message_hint} "
            "Inspect `routing_status` if this looks wrong."
        )
    if _GIT_BRANCH_CREATE_RE.search(raw) and not permissions.get("branch"):
        return (
            "Routing guard blocked branch creation/switching: creating or switching branches "
            f"requires an explicit user request. {permission_summary} {source_note}{latest_message_hint} "
            "Inspect `routing_status` if this looks wrong."
        )
    if _GIT_MUTATION_RE.search(raw) and not permissions.get("mutate"):
        return (
            "Routing guard blocked git history/worktree mutation: `git checkout`/`restore`/`reset`/"
            "`clean`/merge-style commands require an explicit user request and must not be used "
            f"to clean up unrelated changes. {permission_summary} {source_note}{latest_message_hint} "
            "Inspect `routing_status` if this looks wrong."
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


def _normalize_recorded_attempt(entry: dict[str, Any]) -> tuple[bool, Optional[str], int]:
    output = str(entry.get("output", "") or "")
    failure_kind = str(entry.get("failure_kind", "") or "").strip()
    status = str(entry.get("status", "") or "").strip().lower()
    try:
        exit_code = int(entry.get("exit_code", 0) or 0)
    except Exception:
        exit_code = 0

    if status in {"success", "failed", "timeout"}:
        failed = status != "success"
        if failed and not failure_kind:
            if status == "timeout":
                failure_kind = "timeout"
            else:
                failure_kind = str(_classify_routed_failure_kind(output) or "execution_failure")
        return failed, failure_kind or None, exit_code

    failed = bool(entry.get("failed"))
    if not failed:
        failed = exit_code != 0 or bool(_ROUTED_FAILURE_OUTPUT_RE.search(output))
    if failed and not failure_kind:
        failure_kind = str(_classify_routed_failure_kind(output) or ("timeout" if exit_code == 124 else "execution_failure"))
    return failed, failure_kind or None, exit_code


def record_tool_result(task_id: str, tool_name: str, args: dict[str, Any], result: Any) -> None:
    if (
        tool_name not in {"terminal", "routed_exec", "skill_view", "visual_context", "patch", "write_file"}
        or not task_id
        or not isinstance(args, dict)
        or not is_active_for_task(task_id)
    ):
        return

    if tool_name == "visual_context":
        try:
            payload = json.loads(result) if isinstance(result, str) else result
        except Exception:
            payload = None
        if isinstance(payload, dict):
            _record_ability_tool_payload(
                task_id,
                payload,
                fallback_lane="visual",
            )
        return

    if tool_name in {"patch", "write_file"}:
        try:
            payload = json.loads(result) if isinstance(result, str) else result
        except Exception:
            payload = None
        if isinstance(payload, dict) and payload.get("error"):
            return
        mark_ability_evidence_stale(task_id, reason=f"{tool_name} mutation completed")
        return

    if tool_name == "skill_view":
        try:
            payload = json.loads(result) if isinstance(result, str) else result
        except Exception:
            payload = None
        if not isinstance(payload, dict) or not payload.get("success"):
            return

        metadata = payload.get("metadata")
        hermes_meta = metadata.get("hermes") if isinstance(metadata, dict) else None
        routing_meta = hermes_meta.get("routing") if isinstance(hermes_meta, dict) else None
        if not isinstance(routing_meta, dict):
            return

        hint = _normalize_skill_routing_hint(
            {
                "skill_name": payload.get("name", ""),
                "skill_path": payload.get("path", ""),
                "task_class": routing_meta.get("task_class", _TASK_CLASS_CODING),
                "non_code_write_globs": routing_meta.get("non_code_write_globs", []),
            }
        )
        if not isinstance(hint, dict):
            return

        with _task_state_lock:
            _purge_expired()
            state = _task_state.get(task_id)
            if not state:
                return
            merged = _normalize_active_skill_hints([*(state.get("active_skill_hints") or []), hint])
            state["active_skill_hints"] = merged
            state["task_class"] = _derive_task_class(merged)
            if DEFAULT_ROUTING_SKILL in (state.get("skills") or []):
                state["ability_requirements"] = detect_ability_requirements(
                    str(state.get("user_message", "") or ""),
                    merged,
                )
                state["visual_verification_required"] = bool(
                    state["ability_requirements"].get("post_visual_required")
                )
            state["updated_at"] = time.time()
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
            decision = state.get("decision") if isinstance(state.get("decision"), dict) else {}
            profile = _get_route_profile(
                str(decision.get("tier", "")),
                str(decision.get("path", "")),
                _normalize_route_model(str(decision.get("model", ""))),
            )
            primary_kind = str(profile["primary"]["kind"]) if profile else ""
            for entry in attempt_entries:
                if not isinstance(entry, dict):
                    continue
                route_kind = str(entry.get("kind", "") or "")
                if not route_kind:
                    continue
                failed, failure_kind, _ = _normalize_recorded_attempt(entry)
                attempts["last_attempt_kind"] = route_kind
                attempts["last_attempt_failed"] = failed
                attempts["last_attempt_failure_kind"] = failure_kind if failed else None
                if route_kind == primary_kind:
                    attempts["primary_attempted"] = True
                    attempts["primary_failed"] = failed
                    attempts["primary_failure_kind"] = failure_kind if failed else None
                if route_kind == "hermes_glm_zai":
                    attempts["3b_primary_attempted"] = True
                    attempts["3b_primary_failed"] = failed
                    attempts["3b_primary_failure_kind"] = failure_kind if failed else None
                if not failed:
                    _mark_visual_verification_pending_locked(state, reason="routed_exec mutation succeeded")
                    state["routed_mutation_succeeded"] = True
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
        failed, failure_kind, exit_code = _normalize_recorded_attempt(payload)
        failed = bool(error_text) or failed
        if failed and not failure_kind:
            failure_kind = _classify_routed_failure_kind(output) or "execution_failure"
    except Exception:
        failed = True

    with _task_state_lock:
        _purge_expired()
        state = _task_state.get(task_id)
        if not state:
            return
        decision = state.get("decision") if isinstance(state.get("decision"), dict) else {}
        profile = _get_route_profile(
            str(decision.get("tier", "")),
            str(decision.get("path", "")),
            _normalize_route_model(str(decision.get("model", ""))),
        )
        primary_kind = str(profile["primary"]["kind"]) if profile else ""
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
            state["completion_guard_attempts"] = 0
            state["updated_at"] = time.time()
            return
        attempts = state.setdefault("route_attempts", _initial_route_attempts())
        attempts["last_attempt_kind"] = route_kind
        attempts["last_attempt_failed"] = failed
        attempts["last_attempt_failure_kind"] = failure_kind if failed else None
        if route_kind == primary_kind:
            attempts["primary_attempted"] = True
            attempts["primary_failed"] = failed
            attempts["primary_failure_kind"] = failure_kind if failed else None
        if route_kind == "hermes_glm_zai":
            attempts["3b_primary_attempted"] = True
            attempts["3b_primary_failed"] = failed
            attempts["3b_primary_failure_kind"] = failure_kind if failed else None
        if not failed:
            state["routed_mutation_succeeded"] = True
        state["updated_at"] = time.time()


def _is_read_only_terminal_command(command: str) -> bool:
    normalized = " ".join((command or "").strip().lower().split())
    if not normalized:
        return True

    normalized = _SAFE_REDIRECTION_RE.sub(" ", normalized)
    if _UNSAFE_REDIRECTION_RE.search(normalized):
        return False

    commands = _split_shell_segments(normalized, ("&&", "||", ";", "|"))
    if not commands:
        return True

    for part in commands:
        if any(marker in part for marker in _TERMINAL_MUTATION_MARKERS):
            return False
        if _is_safe_git_branch_inspection_command(part):
            continue
        if not any(
            part == prefix or part.startswith(f"{prefix} ")
            for prefix in _READ_ONLY_TERMINAL_PREFIXES
        ):
            return False

    return True


def _is_non_coding_discovery_terminal_command(command: str) -> bool:
    normalized = " ".join((command or "").strip().lower().split())
    if not normalized:
        return False

    normalized = _SAFE_REDIRECTION_RE.sub(" ", normalized)
    if _UNSAFE_REDIRECTION_RE.search(normalized):
        return False

    commands = _split_shell_segments(normalized, ("&&", "||", ";", "|"))
    if len(commands) != 1:
        return False

    part = commands[0]
    if any(marker in part for marker in _TERMINAL_MUTATION_MARKERS):
        return False
    if _classify_routed_terminal_command(part) is not None:
        return False
    return bool(_MCPORTER_DISCOVERY_RE.match(part))


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


def _is_safe_git_branch_inspection_command(command: str) -> bool:
    normalized = " ".join((command or "").strip().lower().split())
    if not normalized.startswith("git branch"):
        return False

    tokens = normalized.split()
    if len(tokens) < 2 or tokens[:2] != ["git", "branch"]:
        return False

    branch_tokens = tokens[2:]
    if not branch_tokens:
        return True

    safe_flags = {
        "-a",
        "--all",
        "-r",
        "--remotes",
        "--show-current",
        "--list",
        "--color",
        "--no-color",
        "-v",
        "-vv",
        "--verbose",
        "--column",
        "--sort",
        "--ignore-case",
        "--omit-empty",
        "--format",
        "--contains",
        "--no-contains",
        "--merged",
        "--no-merged",
        "--points-at",
    }
    value_flags = {
        "--column",
        "--sort",
        "--format",
        "--contains",
        "--no-contains",
        "--merged",
        "--no-merged",
        "--points-at",
    }

    idx = 0
    saw_list = False
    while idx < len(branch_tokens):
        token = branch_tokens[idx]
        if token in {"-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move", "--copy", "--set-upstream-to", "-u", "--unset-upstream", "--track", "--no-track", "--edit-description"}:
            return False
        if token in safe_flags:
            if token == "--list":
                saw_list = True
            if token in value_flags:
                idx += 1
                if idx >= len(branch_tokens):
                    return False
            idx += 1
            continue
        if token.startswith("-"):
            return False
        if saw_list:
            idx += 1
            continue
        return False

    return True


def _classify_verification_command(command: str, *, allow_output_pipe: bool = False) -> Optional[str]:
    raw = (command or "").strip()
    if not raw:
        return None

    normalized = " ".join(raw.lower().split())
    normalized = _SAFE_REDIRECTION_RE.sub(" ", normalized)

    if _UNSAFE_REDIRECTION_RE.search(normalized):
        return None
    if _VERIFICATION_OUTPUT_PIPE_RE.search(normalized):
        if not allow_output_pipe:
            return None
        normalized = _VERIFICATION_OUTPUT_PIPE_SPLIT_RE.sub("", normalized).strip()
    if not normalized:
        return None
    if "| codex exec" in normalized or "| hermes chat" in normalized:
        return None
    if _classify_routed_terminal_command(normalized) is not None:
        return None

    parts = _split_shell_segments(normalized, ("&&", ";"))
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


def _verification_terminal_block_reason(command: str) -> Optional[str]:
    normalized = " ".join((command or "").strip().lower().split())
    normalized = _SAFE_REDIRECTION_RE.sub(" ", normalized)
    if not _VERIFICATION_OUTPUT_PIPE_RE.search(normalized):
        return None
    verification_kind = _classify_verification_command(command, allow_output_pipe=True)
    if verification_kind is None:
        return None
    return (
        "Routing guard blocked verification through `terminal`: do not pipe build/test/lint output through "
        "`tail`/`head`/`Select-Object` because that can mask the true exit status. "
        f"Run the `{verification_kind}` command directly."
    )


def _visual_verification_terminal_block_reason(command: str) -> Optional[str]:
    raw = (command or "").strip()
    if not raw:
        return None

    normalized = " ".join(raw.lower().split())
    normalized = _SAFE_REDIRECTION_RE.sub(" ", normalized)
    if _UNSAFE_REDIRECTION_RE.search(normalized) or _VERIFICATION_OUTPUT_PIPE_RE.search(normalized):
        return None
    if _classify_visual_verification_command(command) is not None:
        return None

    parts = _split_shell_segments(normalized, ("&&", ";"))
    if not parts:
        return None

    for part in parts:
        if _is_read_only_terminal_command(part):
            continue
        candidate = _normalize_verification_segment(part)
        try:
            tokens = shlex.split(candidate)
        except ValueError:
            return None

        if len(tokens) >= 4 and tokens[:3] in (["python", "-m", "http.server"], ["python3", "-m", "http.server"]):
            if "--directory" in tokens or "-d" in tokens:
                return (
                    "Routing guard blocked visual preview through `terminal`: local preview servers must stay scoped "
                    "to the current working tree and bind to localhost only. Use "
                    "`cd /path/to/project && python3 -m http.server 8765 --bind 127.0.0.1`, or use "
                    "`browser_navigate` with a `file://` URL for static files."
                )
            if "--bind" not in tokens:
                return (
                    "Routing guard blocked visual preview through `terminal`: local preview servers must bind to "
                    "localhost only. Use `python3 -m http.server 8765 --bind 127.0.0.1`, or use "
                    "`browser_navigate` with a `file://` URL for static files."
                )
            bind_index = tokens.index("--bind")
            host = tokens[bind_index + 1] if bind_index + 1 < len(tokens) else ""
            if host not in {"127.0.0.1", "localhost", "::1"}:
                return (
                    "Routing guard blocked visual preview through `terminal`: local preview servers must bind to "
                    "localhost only. Use `python3 -m http.server 8765 --bind 127.0.0.1`, or use "
                    "`browser_navigate` with a `file://` URL for static files."
                )
            continue

        if len(tokens) >= 2 and tokens[0] in {"http-server", "npx"}:
            token_text = " ".join(tokens)
            if token_text.startswith("npx http-server") or token_text.startswith("http-server"):
                if "--host" in tokens:
                    host_index = tokens.index("--host")
                    host = tokens[host_index + 1] if host_index + 1 < len(tokens) else ""
                elif "-a" in tokens:
                    host_index = tokens.index("-a")
                    host = tokens[host_index + 1] if host_index + 1 < len(tokens) else ""
                else:
                    host = ""
                if host not in {"127.0.0.1", "localhost", "::1"}:
                    return (
                        "Routing guard blocked visual preview through `terminal`: local preview servers must bind to "
                        "localhost only. Use `npx http-server -a 127.0.0.1`, or use "
                        "`browser_navigate` with a `file://` URL for static files."
                    )
                continue

        return None

    return None


def _classify_visual_verification_command(command: str) -> Optional[str]:
    raw = (command or "").strip()
    if not raw:
        return None

    normalized = " ".join(raw.lower().split())
    normalized = _SAFE_REDIRECTION_RE.sub(" ", normalized)
    if _UNSAFE_REDIRECTION_RE.search(normalized) or _VERIFICATION_OUTPUT_PIPE_RE.search(normalized):
        return None
    if _classify_routed_terminal_command(normalized) is not None:
        return None

    parts = _split_shell_segments(normalized, ("&&", ";"))
    if not parts:
        return None

    saw_preview = False
    for part in parts:
        if _is_read_only_terminal_command(part):
            continue
        candidate = _normalize_verification_segment(part)
        try:
            tokens = shlex.split(candidate)
        except ValueError:
            return None
        if len(tokens) >= 5 and tokens[:3] in (["python", "-m", "http.server"], ["python3", "-m", "http.server"]):
            if "--directory" in tokens or "-d" in tokens:
                return None
            if "--bind" not in tokens:
                return None
            bind_index = tokens.index("--bind")
            if bind_index + 1 >= len(tokens) or tokens[bind_index + 1] not in {"127.0.0.1", "localhost", "::1"}:
                return None
            saw_preview = True
            continue
        if len(tokens) >= 2 and tokens[0] in {"http-server", "npx"}:
            token_text = " ".join(tokens)
            if token_text.startswith("npx http-server") or token_text.startswith("http-server"):
                if "--host" in tokens:
                    host_index = tokens.index("--host")
                    host = tokens[host_index + 1] if host_index + 1 < len(tokens) else ""
                elif "-a" in tokens:
                    host_index = tokens.index("-a")
                    host = tokens[host_index + 1] if host_index + 1 < len(tokens) else ""
                else:
                    host = ""
                if host not in {"127.0.0.1", "localhost", "::1"}:
                    return None
                saw_preview = True
                continue
        return None

    return "local-preview" if saw_preview else None


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


def pre_tool_call_block_reason(tool_name: str, args: dict[str, Any], task_id: str, session_id: str = "") -> Optional[str]:
    if not task_id or not is_active_for_task(task_id):
        return None
    routed = has_route_lock(task_id)
    routing_task = is_routing_enforced_task(task_id)
    decision_error = _get_decision_error(task_id)

    def _block(reason: str) -> str:
        return _record_blocked_tool(task_id, tool_name, reason)

    if decision_error and tool_name in {"patch", "write_file", "terminal", "delegate_task", "routed_exec", "routed_plan", "execute_code"}:
        return _block(decision_error)

    if tool_name == "execute_code":
        if not routing_task:
            return None
        if routed:
            return _block(
                f"Routing guard blocked `execute_code` for task {task_id}: "
                "stay on the routed model path for implementation. Use `routed_exec` for routed coding work, "
                "and use approved local verification commands through `terminal` when you need tests/build/lint checks."
            )
        return _block(
            f"Routing guard blocked `execute_code` for task {task_id}: "
            "do not use code execution to bypass routing. Before route lock, only read-only inspection tools are allowed. "
            "Emit the routing decision line first."
        )

    if tool_name in {"patch", "write_file"}:
        mutation = _classify_file_mutation(tool_name, args if isinstance(args, dict) else {})
        _set_last_mutation_class(task_id, str(mutation.get("class", _WORK_CLASS_UNKNOWN)))
        if not routed and _is_allowed_docs_text_mutation(mutation, task_id):
            return None
        if routed and routing_task:
            return _block(
                f"Routing guard blocked native `{tool_name}` for task {task_id}: "
                "stay on the routed model path and do not fall back to native file mutation."
            )
        detail = _describe_mutation_block_reason(mutation)
        return _block(
            f"Routing guard blocked `{tool_name}` for task {task_id}: "
            f"{detail}. Emit a routing decision line before mutating files."
        )

    if tool_name == "routed_exec":
        if not routing_task:
            return _block(
                "Routing guard blocked `routed_exec`: this tool is reserved for routing-layer controlled "
                "coding tasks."
            )
        if routed:
            requirements = get_ability_requirements(task_id)
            missing = preflight_missing_lanes(requirements, get_ability_packets(task_id))
            if missing:
                lanes = ", ".join(missing)
                return _block(
                    f"Routing guard blocked `routed_exec` for task {task_id}: "
                    f"required ability preflight lane(s) missing: {lanes}. "
                    "Call `ability_context` with `mode=\"auto\"` or `mode=\"collect\"` and `phase=\"pre\"`; "
                    "if a lane is unavailable, record an unavailable packet with the concrete blocker."
                )
            return None
        return _block(
            f"Routing guard blocked `routed_exec` for task {task_id}: "
            "emit a routing decision line before starting routed execution."
        )

    if tool_name == "routed_plan":
        if not routing_task:
            return _block(
                "Routing guard blocked `routed_plan`: this tool is reserved for routing-layer controlled "
                "coding tasks."
            )
        if not routed:
            requested_plan_id = ""
            if isinstance(args, dict):
                requested_plan_id = str(args.get("plan_id", "") or "").strip()
            routed = hydrate_routed_plan_from_persistence(
                task_id,
                session_id=session_id,
                plan_id=requested_plan_id,
            ) or has_route_lock(task_id)
        if routed:
            requirements = get_ability_requirements(task_id)
            missing = preflight_missing_lanes(requirements, get_ability_packets(task_id))
            if missing:
                lanes = ", ".join(missing)
                return _block(
                    f"Routing guard blocked `routed_plan` for task {task_id}: "
                    f"required ability preflight lane(s) missing: {lanes}. "
                    "Call `ability_context` with `mode=\"auto\"` or `mode=\"collect\"` and `phase=\"pre\"`; "
                    "if a lane is unavailable, record an unavailable packet with the concrete blocker."
                )
            return None
        return _block(
            f"Routing guard blocked `routed_plan` for task {task_id}: "
            "emit a routing decision line before submitting or running a routed plan."
        )

    if tool_name == "terminal":
        command = ""
        if isinstance(args, dict):
            command = str(args.get("command", "") or "")
        git_issue = _validate_git_terminal_command(command, task_id, session_id=session_id)
        if git_issue:
            return _block(git_issue)
        if routed and routing_task:
            if _is_explicitly_permitted_git_terminal_command(command, task_id):
                return None
            route_kind = _classify_routed_terminal_command(command)
            if route_kind is not None:
                return _block(
                    f"Routing guard blocked routed model execution through `terminal` for task {task_id}: "
                    "use `routed_exec` for routed Codex/Hermes execution. "
                    "`terminal` remains available only for approved verification commands, read-only inspection, "
                    "and explicitly permitted git actions."
                )
            verification_issue = _verification_terminal_block_reason(command)
            if verification_issue:
                return _block(verification_issue)
            preview_issue = _visual_verification_terminal_block_reason(command)
            if preview_issue:
                return _block(preview_issue)
            if _is_verification_terminal_command(command):
                return None
            if _classify_visual_verification_command(command) is not None:
                return None
            if _is_read_only_terminal_command(command):
                return None
            if (
                get_task_class(task_id) == _TASK_CLASS_NON_CODING
                and _is_non_coding_discovery_terminal_command(command)
            ):
                return None
            return _block(
                f"Routing guard blocked native `terminal` execution for task {task_id}: "
                "after a routing decision, non-read-only shell work must stay on the routed model path. "
                "Only routed model execution via `routed_exec`, approved verification commands, localhost-only visual preview commands, and read-only inspection commands are allowed."
            )
        if routed:
            return None
        preview_issue = _visual_verification_terminal_block_reason(command)
        if preview_issue:
            return _block(preview_issue)
        if _classify_visual_verification_command(command) is not None:
            return None
        if _is_read_only_terminal_command(command):
            return None
        if (
            get_task_class(task_id) == _TASK_CLASS_NON_CODING
            and _is_non_coding_discovery_terminal_command(command)
        ):
            return None
        return _block(
            f"Routing guard blocked `terminal` for task {task_id}: "
            "only read-only inspection commands and localhost-only visual preview commands are allowed before a routing decision. "
            "Emit the routing decision line before non-read-only terminal work."
        )

    if tool_name == "delegate_task":
        if routed and routing_task and _is_implementation_delegate(args if isinstance(args, dict) else {}):
            return _block(
                f"Routing guard blocked native `delegate_task` for task {task_id}: "
                "stay on the routed model path instead of falling back to ordinary delegation."
            )
        if not isinstance(args, dict) or not _is_implementation_delegate(args):
            return None
        return _block(
            f"Routing guard blocked `delegate_task` for task {task_id}: "
            "implementation-oriented delegation requires a routing decision first."
        )

    return None
