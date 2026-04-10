"""Ability-lane detection and evidence packet helpers for routing tasks."""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Iterable, Optional

from agent.redact import redact_sensitive_text


ABILITY_LANES = (
    "visual",
    "runtime",
    "environment",
    "repo_archaeology",
    "external_docs",
    "security",
    "data_logs",
    "audio_video",
)

VISUAL_CACHE_TTL_SECONDS = 60
MAX_COMPACT_TEXT_CHARS = 900
MAX_HANDOFF_CHARS = 5000

_VISUAL_RE = re.compile(
    r"(?i)\b("
    r"ui|ux|layout|css|html|canvas|webgl|three\.?js|p5\.?js|animation|animated|"
    r"screenshot|responsive|viewport|browser|web\s*page|frontend|front-end|"
    r"visual|visually|looks?\s+wrong|render(?:ing|ed)?|scene|game|sprite|"
    r"button|modal|canvas-heavy"
    r")\b"
)
_RUNTIME_RE = re.compile(
    r"(?i)\b(cpu|gpu|memory|mem(?:ory)?\s*leak|leak|stutter|jank|slow|sluggish|hangs?|build\s+hangs?|"
    r"freeze|perf(?:ormance)?|profile|profiling|fps|frame\s*rate|hot\s*loop)\b"
)
_ENVIRONMENT_RE = re.compile(
    r"(?i)\b(port|ports|localhost|127\.0\.0\.1|dev\s*server|preview\s*server|server\s*lifecycle|"
    r"docker|compose|container|browser\s*backend|ssrf|file://|web\s*server)\b"
)
_REPO_ARCHAEOLOGY_RE = re.compile(
    r"(?i)\b(why\s+is|where\s+is|where.*coming\s+from|origin|history|git\s+history|recent\s+commit|"
    r"archaeolog|blame|introduced|regression)\b"
)
_EXTERNAL_DOCS_RE = re.compile(
    r"(?i)\b(latest|current|docs?|documentation|sdk|framework|library|provider|version|"
    r"spec|standard|release\s+notes|changelog)\b"
)
_SECURITY_RE = re.compile(
    r"(?i)\b(auth|oauth|token|secret|credential|password|api\s*key|permission|sandbox|ssrf|"
    r"supply\s*chain|destructive|reset\s+--hard|rm\s+-rf|vulnerab|security)\b"
)
_DATA_LOGS_RE = re.compile(
    r"(?i)\b(logs?|trace|stack\s*trace|test\s+report|junit|metrics?|dump|"
    r"large\s+output|failing\s+tests?|error\s+log|"
    r"(?:json|csv|xml)\s+(?:artifact|file|dump|report|log|output))\b"
)
_AUDIO_VIDEO_RE = re.compile(
    r"(?i)\b(audio|video|media|mp3|wav|m4a|mp4|mov|webm|transcrib|caption|subtitles?)\b"
)

_LANE_PATTERNS = {
    "visual": _VISUAL_RE,
    "runtime": _RUNTIME_RE,
    "environment": _ENVIRONMENT_RE,
    "repo_archaeology": _REPO_ARCHAEOLOGY_RE,
    "external_docs": _EXTERNAL_DOCS_RE,
    "security": _SECURITY_RE,
    "data_logs": _DATA_LOGS_RE,
    "audio_video": _AUDIO_VIDEO_RE,
}


def normalize_lanes(lanes: Optional[Iterable[Any]]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in lanes or []:
        lane = str(raw or "").strip().lower().replace("-", "_")
        if not lane or lane not in ABILITY_LANES or lane in seen:
            continue
        normalized.append(lane)
        seen.add(lane)
    return normalized


def detect_ability_requirements(
    user_message: str,
    active_skill_hints: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    text_parts = [str(user_message or "")]
    for hint in active_skill_hints or []:
        if isinstance(hint, dict):
            text_parts.append(json.dumps(hint, sort_keys=True, ensure_ascii=False))
        else:
            text_parts.append(str(hint))
    haystack = "\n".join(text_parts)

    lanes: dict[str, dict[str, Any]] = {}
    for lane, pattern in _LANE_PATTERNS.items():
        match = pattern.search(haystack)
        if not match:
            continue
        lanes[lane] = {
            "required": True,
            "reason": f"Task mentions {match.group(0)!r}, which benefits from the {lane} ability lane.",
            "phase": "pre",
        }

    return {
        "lanes": lanes,
        "post_visual_required": "visual" in lanes,
        "detected_at": time.time(),
    }


def lane_required(requirements: dict[str, Any], lane: str) -> bool:
    lanes = requirements.get("lanes") if isinstance(requirements, dict) else {}
    item = lanes.get(lane) if isinstance(lanes, dict) else None
    return bool(isinstance(item, dict) and item.get("required"))


def required_lanes(requirements: dict[str, Any]) -> list[str]:
    lanes = requirements.get("lanes") if isinstance(requirements, dict) else {}
    if not isinstance(lanes, dict):
        return []
    return [lane for lane in ABILITY_LANES if lane_required(requirements, lane)]


def _short_text(value: Any, limit: int = MAX_COMPACT_TEXT_CHARS) -> str:
    text = redact_sensitive_text(str(value or "").strip())
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return f"{text[:limit]}\n[truncated {omitted} chars]"


def _compact_list(values: Any, limit: int = 6) -> list[Any]:
    if not isinstance(values, list):
        return []
    compacted: list[Any] = []
    for item in values[:limit]:
        if isinstance(item, dict):
            compacted.append({str(k): _compact_value(v) for k, v in item.items() if k in {
                "path", "line", "message", "error", "summary", "finding", "url", "title", "status", "constraint",
                "process", "pid", "cpu", "memory", "command", "cwd", "exit_code",
            }})
        else:
            compacted.append(_short_text(item, 300))
    if len(values) > limit:
        compacted.append(f"[{len(values) - limit} more omitted]")
    return compacted


def _compact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _short_text(value, 500)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return _compact_list(value)
    if isinstance(value, dict):
        return {str(k): _compact_value(v) for k, v in list(value.items())[:12]}
    return _short_text(value, 300)


def build_cache_key(
    *,
    lane: str,
    source: str = "",
    task: str = "",
    url: str = "",
    artifact_path: str = "",
    query: str = "",
    phase: str = "pre",
    browser_backend: str = "",
    cleanup_policy: str = "auto",
) -> str:
    material = {
        "lane": lane,
        "source": source,
        "task": task,
        "url": url,
        "artifact_path": artifact_path,
        "query": query,
        "phase": phase,
        "browser_backend": browser_backend,
        "cleanup_policy": cleanup_policy,
    }
    encoded = json.dumps(material, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def make_ability_packet(
    *,
    task_id: str,
    lanes: Iterable[str],
    phase: str = "pre",
    status: str = "success",
    summary: str = "",
    findings: Optional[list[Any]] = None,
    constraints: Optional[list[Any]] = None,
    url_or_path: str = "",
    screenshot_path: str = "",
    console_errors: Optional[list[Any]] = None,
    artifact_paths: Optional[list[str]] = None,
    health: Optional[dict[str, Any]] = None,
    cache_key: str = "",
    cached: bool = False,
    stale: bool = False,
    generated_at: Optional[float] = None,
) -> dict[str, Any]:
    packet = {
        "task_id": str(task_id or ""),
        "lanes": normalize_lanes(lanes),
        "phase": str(phase or "pre"),
        "status": str(status or "success").strip().lower(),
        "summary": _short_text(summary, 1200),
        "findings": _compact_list(findings or [], 10),
        "constraints": _compact_list(constraints or [], 10),
        "url_or_path": str(url_or_path or ""),
        "screenshot_path": str(screenshot_path or ""),
        "console_errors": _compact_list(console_errors or [], 8),
        "artifact_paths": [str(path) for path in (artifact_paths or []) if str(path or "").strip()][:12],
        "health": _compact_value(health or {}),
        "cache_key": str(cache_key or ""),
        "cached": bool(cached),
        "stale": bool(stale),
        "generated_at": float(generated_at if generated_at is not None else time.time()),
    }
    packet["success"] = packet["status"] == "success"
    return packet


def packet_has_concrete_blocker(packet: dict[str, Any]) -> bool:
    if not isinstance(packet, dict):
        return False
    if packet.get("status") not in {"unavailable", "blocked"}:
        return False
    return bool(packet.get("summary") or packet.get("constraints") or packet.get("findings"))


def packet_satisfies_lane(packet: dict[str, Any], lane: str, *, phase: str = "pre") -> bool:
    if not isinstance(packet, dict) or packet.get("stale"):
        return False
    if lane not in normalize_lanes(packet.get("lanes") or []):
        return False
    if str(packet.get("phase") or "pre") != phase:
        return False
    status = str(packet.get("status") or "").lower()
    if status == "success":
        return True
    return packet_has_concrete_blocker(packet)


def preflight_missing_lanes(requirements: dict[str, Any], packets: list[dict[str, Any]]) -> list[str]:
    missing: list[str] = []
    for lane in required_lanes(requirements):
        if any(packet_satisfies_lane(packet, lane, phase="pre") for packet in packets):
            continue
        missing.append(lane)
    return missing


def visual_post_verified(packets: list[dict[str, Any]]) -> bool:
    return any(packet_satisfies_lane(packet, "visual", phase="post") for packet in packets)


def compact_packets_for_handoff(packets: list[dict[str, Any]]) -> str:
    compacted: list[dict[str, Any]] = []
    for packet in packets:
        if not isinstance(packet, dict) or packet.get("stale"):
            continue
        compacted.append(
            {
                "lanes": normalize_lanes(packet.get("lanes") or []),
                "phase": packet.get("phase", "pre"),
                "status": packet.get("status", ""),
                "summary": _short_text(packet.get("summary", ""), 900),
                "findings": _compact_list(packet.get("findings") or [], 6),
                "constraints": _compact_list(packet.get("constraints") or [], 6),
                "url_or_path": packet.get("url_or_path", ""),
                "screenshot_path": packet.get("screenshot_path", ""),
                "console_errors": _compact_list(packet.get("console_errors") or [], 5),
                "artifact_paths": packet.get("artifact_paths", [])[:8],
                "health": _compact_value(packet.get("health") or {}),
            }
        )
    if not compacted:
        return ""
    text = json.dumps(compacted, ensure_ascii=False, indent=2)
    text = redact_sensitive_text(text)
    if len(text) <= MAX_HANDOFF_CHARS:
        return text
    return f"{text[:MAX_HANDOFF_CHARS]}\n[ability evidence truncated {len(text) - MAX_HANDOFF_CHARS} chars]"
