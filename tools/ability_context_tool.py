"""Read-only ability-lane context collector for routing-layer tasks."""

from __future__ import annotations

import csv
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from agent.ability_context import (
    ABILITY_LANES,
    VISUAL_CACHE_TTL_SECONDS,
    build_cache_key,
    detect_ability_requirements,
    make_ability_packet,
    normalize_lanes,
    required_lanes,
)
from agent.routing_guard import (
    clear_ability_cache,
    get_ability_packets,
    get_ability_requirements,
    get_cached_ability_packet,
    record_ability_packet,
)
from tools.registry import registry, tool_result


_MAX_CAPTURE_CHARS = 8000
_ERROR_LINE_RE = re.compile(r"(?i)\b(error|failed|failure|exception|traceback|assert|panic|fatal)\b")
_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|secret|token|password|passwd|credential|private[_-]?key)\s*[:=]\s*['\"]?([A-Za-z0-9_./+=:-]{8,})"
)
_LOCAL_URL_RE = re.compile(r"(?i)^(?:https?://)?(?:localhost|127\.0\.0\.1|\[?::1\]?)(?::\d+)?(?:/|$)")
_HEAVY_VISUAL_RE = re.compile(r"(?i)\b(webgl|canvas|animation|animated|video|p5|three\.?js|game|gpu|fps|frame\s*rate)\b")
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff"}
_LOG_SUFFIXES = {".log", ".txt", ".out", ".err", ".json", ".jsonl", ".csv", ".xml"}
_MEDIA_SUFFIXES = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".mp4", ".mov", ".webm", ".mkv", ".avi"}


ABILITY_CONTEXT_SCHEMA = {
    "name": "ability_context",
    "description": (
        "Collect bounded read-only evidence for routing ability lanes before routed implementation "
        "or after visual/UI fixes. Lanes include visual, runtime, environment, repo_archaeology, "
        "external_docs, security, data_logs, and audio_video. This tool gathers context, caches it "
        "per task, and does not mutate project files."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["auto", "collect", "health", "cache_status", "clear_cache"],
                "default": "auto",
            },
            "lanes": {
                "type": "array",
                "items": {"type": "string", "enum": list(ABILITY_LANES)},
                "description": "Ability lanes to inspect. Omit for mode=auto.",
            },
            "task": {"type": "string", "description": "User task or local question to evaluate."},
            "workdir": {"type": "string", "description": "Project working directory for repo/runtime/environment lanes."},
            "url": {"type": "string", "description": "URL or local page path for visual/environment lanes."},
            "artifact_path": {"type": "string", "description": "Screenshot, log, JSON/CSV/XML, audio, or video artifact path."},
            "query": {"type": "string", "description": "Search/query text for docs, repo archaeology, or log filtering."},
            "phase": {"type": "string", "enum": ["pre", "post"], "default": "pre"},
            "force_refresh": {"type": "boolean", "default": False},
            "cleanup_policy": {"type": "string", "enum": ["auto", "always", "never"], "default": "auto"},
        },
        "required": [],
    },
}


def _json_or_text(value: str) -> Any:
    try:
        return json.loads(value)
    except Exception:
        return value


def _short(text: Any, limit: int = 1200) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return f"{value[:limit]}\n[truncated {len(value) - limit} chars]"


def _run_readonly(command: list[str], *, cwd: str = "", timeout: int = 4) -> dict[str, Any]:
    try:
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout,
            cwd=cwd or None,
        )
        output = (result.stdout or result.stderr or "").strip()
        return {
            "success": result.returncode == 0,
            "exit_code": result.returncode,
            "output": _short(output, _MAX_CAPTURE_CHARS),
        }
    except Exception as exc:
        return {"success": False, "exit_code": -1, "output": "", "error": str(exc)}


def _is_local_visual_source(url_or_path: str) -> bool:
    source = str(url_or_path or "").strip()
    if not source:
        return False
    return source.startswith("file:") or source.startswith("/") or _LOCAL_URL_RE.search(source) is not None


def _is_heavy_visual(task: str, query: str, url: str, artifact_path: str) -> bool:
    text = "\n".join([task, query, url, artifact_path])
    return bool(_HEAVY_VISUAL_RE.search(text))


def _browser_backend_label() -> str:
    try:
        from tools.browser_tool import _get_cloud_provider, _is_local_backend

        if _is_local_backend():
            return "local"
        provider = _get_cloud_provider()
        return f"cloud:{type(provider).__name__}" if provider else "cloud"
    except Exception:
        return "unknown"


def _visual_health(url: str = "", task: str = "", query: str = "") -> dict[str, Any]:
    health: dict[str, Any] = {}
    try:
        from tools.visual_context_tool import _vision_capability_error

        capability_error = _vision_capability_error()
        health["vision_model_capable"] = capability_error is None
        if capability_error:
            health["vision_model_blocker"] = capability_error
    except Exception as exc:
        health["vision_model_capable"] = False
        health["vision_model_blocker"] = str(exc)

    try:
        from tools.browser_tool import _allow_private_urls, _is_local_backend, check_browser_requirements

        health["browser_available"] = bool(check_browser_requirements())
        health["browser_backend"] = "local" if _is_local_backend() else "cloud"
        health["browser_allow_private_urls"] = bool(_allow_private_urls())
    except Exception as exc:
        health["browser_available"] = False
        health["browser_blocker"] = str(exc)

    health["source_is_local"] = _is_local_visual_source(url)
    health["heavy_page_hint"] = _is_heavy_visual(task, query, url, "")
    return health


async def _collect_visual(
    *,
    task_id: str,
    task: str,
    query: str,
    url: str,
    artifact_path: str,
    phase: str,
    force_refresh: bool,
    cleanup_policy: str,
) -> dict[str, Any]:
    source = "image" if Path(artifact_path).suffix.lower() in _IMAGE_SUFFIXES else "browser"
    source_ref = artifact_path if source == "image" else url
    health = _visual_health(source_ref, task=task, query=query)
    cache_key = build_cache_key(
        lane="visual",
        source=source,
        task=task,
        url=url,
        artifact_path=artifact_path,
        query=query,
        phase=phase,
        browser_backend=str(health.get("browser_backend") or _browser_backend_label()),
        cleanup_policy=cleanup_policy,
    )
    if not force_refresh:
        cached = get_cached_ability_packet(task_id, cache_key, ttl_seconds=VISUAL_CACHE_TTL_SECONDS)
        if cached:
            return cached

    if source == "image" and not artifact_path:
        return make_ability_packet(
            task_id=task_id,
            lanes=["visual"],
            phase=phase,
            status="unavailable",
            summary="Visual image inspection needs an artifact_path or url.",
            constraints=["No image artifact_path was provided."],
            health=health,
            cache_key=cache_key,
        )

    cleanup_after = cleanup_policy == "always" or (
        cleanup_policy == "auto"
        and source == "browser"
        and _is_local_visual_source(url)
        and _is_heavy_visual(task, query, url, artifact_path)
    )
    question = query or task or "Gather visual context for this task."
    try:
        from tools.visual_context_tool import visual_context_tool

        payload = _json_or_text(
            await visual_context_tool(
                source,
                question,
                url=url,
                image_url=artifact_path,
                include_console=True,
                include_snapshot=True,
                cleanup_after=cleanup_after,
                task_id=task_id,
                user_task=task or query,
            )
        )
    except Exception as exc:
        payload = {"success": False, "error": str(exc)}

    console_errors: list[Any] = []
    if isinstance(payload, dict):
        browser = payload.get("browser")
        if isinstance(browser, dict):
            console = browser.get("console")
            if isinstance(console, dict):
                errors = console.get("errors")
                if isinstance(errors, list):
                    console_errors = errors
                elif console.get("total_errors"):
                    console_errors = [console]
        status = "success" if payload.get("success") else "unavailable"
        summary = str(payload.get("visual_summary") or payload.get("error") or "Visual context collected.")
        screenshot_path = str(payload.get("screenshot_path") or "")
        constraints = [payload.get("error")] if payload.get("error") else []
    else:
        status = "unavailable"
        summary = str(payload or "Visual context unavailable.")
        screenshot_path = ""
        constraints = [summary]

    return make_ability_packet(
        task_id=task_id,
        lanes=["visual"],
        phase=phase,
        status=status,
        summary=summary,
        constraints=constraints,
        url_or_path=source_ref,
        screenshot_path=screenshot_path,
        console_errors=console_errors,
        artifact_paths=[screenshot_path] if screenshot_path else [],
        health={**health, "cleanup_after": cleanup_after},
        cache_key=cache_key,
    )


def _collect_runtime(task_id: str, task: str, workdir: str, phase: str) -> dict[str, Any]:
    findings: list[Any] = []
    ps = _run_readonly(["ps", "-eo", "pid,ppid,pcpu,pmem,comm,args", "--sort=-pcpu"], timeout=3)
    if ps.get("success"):
        lines = str(ps.get("output") or "").splitlines()
        findings.append({"summary": "Top CPU processes", "message": "\n".join(lines[:8])})
    else:
        findings.append({"summary": "Process snapshot unavailable", "error": ps.get("error") or ps.get("output")})
    ports = _run_readonly(["ss", "-ltnp"], timeout=3)
    if ports.get("success"):
        findings.append({"summary": "Listening TCP ports", "message": "\n".join(str(ports.get("output") or "").splitlines()[:12])})
    return make_ability_packet(
        task_id=task_id,
        lanes=["runtime"],
        phase=phase,
        summary="Collected lightweight runtime/process evidence.",
        findings=findings,
        constraints=["No deep profiler was run by default."],
        url_or_path=workdir,
    )


def _collect_environment(task_id: str, workdir: str, url: str, phase: str) -> dict[str, Any]:
    findings: list[Any] = []
    try:
        from tools.process_registry import process_registry

        sessions = process_registry.list_sessions(task_id=task_id)
        findings.append({"summary": "Managed process registry", "message": json.dumps(sessions[:8], ensure_ascii=False)})
    except Exception as exc:
        findings.append({"summary": "Process registry unavailable", "error": str(exc)})
    ports = _run_readonly(["ss", "-ltnp"], timeout=3)
    if ports.get("success"):
        findings.append({"summary": "Listening TCP ports", "message": "\n".join(str(ports.get("output") or "").splitlines()[:12])})
    constraints = ["Did not start a preview/dev server automatically in this bounded preflight."]
    if url and _is_local_visual_source(url):
        constraints.append("Local URL detected; reuse existing browser/server resources when possible.")
    return make_ability_packet(
        task_id=task_id,
        lanes=["environment"],
        phase=phase,
        summary="Collected dev-server/environment state.",
        findings=findings,
        constraints=constraints,
        url_or_path=url or workdir,
    )


def _collect_repo_archaeology(task_id: str, workdir: str, query: str, phase: str) -> dict[str, Any]:
    cwd = workdir if workdir and Path(workdir).exists() else ""
    findings: list[Any] = []
    for label, command in (
        ("git status", ["git", "status", "--short"]),
        ("recent commits", ["git", "log", "--oneline", "-n", "8"]),
    ):
        result = _run_readonly(command, cwd=cwd, timeout=5)
        findings.append({"summary": label, "message": result.get("output") or result.get("error") or ""})
    if query:
        rg = _run_readonly(["rg", "-n", "-m", "20", "--", query[:120], "."], cwd=cwd, timeout=5)
        findings.append({"summary": "bounded content search", "message": rg.get("output") or rg.get("error") or ""})
    return make_ability_packet(
        task_id=task_id,
        lanes=["repo_archaeology"],
        phase=phase,
        summary="Collected bounded repo archaeology evidence.",
        findings=findings,
        url_or_path=cwd,
    )


def _collect_external_docs(task_id: str, query: str, phase: str) -> dict[str, Any]:
    if not query:
        return make_ability_packet(
            task_id=task_id,
            lanes=["external_docs"],
            phase=phase,
            status="unavailable",
            summary="External docs lane needs a query.",
            constraints=["No docs/current-facts query was provided."],
        )
    try:
        from tools.web_tools import web_search_tool

        payload = _json_or_text(web_search_tool(query, limit=3))
        findings = [{"summary": "web_search results", "message": json.dumps(payload, ensure_ascii=False)}]
        status = "success" if isinstance(payload, dict) and payload.get("success") is True else "unavailable"
        constraints = [] if status == "success" else ["Web search backend returned an unavailable/failed result."]
    except Exception as exc:
        findings = []
        constraints = [str(exc)]
        status = "unavailable"
    return make_ability_packet(
        task_id=task_id,
        lanes=["external_docs"],
        phase=phase,
        status=status,
        summary="Collected external docs/current-facts search evidence." if status == "success" else "External docs evidence unavailable.",
        findings=findings,
        constraints=constraints,
    )


def _collect_security(task_id: str, workdir: str, task: str, phase: str) -> dict[str, Any]:
    cwd = workdir if workdir and Path(workdir).exists() else ""
    findings: list[Any] = []
    diff = _run_readonly(["git", "diff", "--"], cwd=cwd, timeout=5)
    diff_text = str(diff.get("output") or "")
    secret_hits = []
    current_file = ""
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
        if not line.startswith("+") or line.startswith("+++"):
            continue
        match = _SECRET_RE.search(line)
        if match:
            secret_hits.append({"path": current_file, "finding": f"Possible secret-like value assigned to {match.group(1)}"})
    findings.extend(secret_hits[:10])
    if re.search(r"(?i)\b(ssrf|sandbox|credential|token|auth|destructive|supply\s*chain)\b", task):
        findings.append({"summary": "Security-sensitive task language", "message": "Task mentions auth/SSRF/sandbox/credentials/destructive/supply-chain concerns."})
    return make_ability_packet(
        task_id=task_id,
        lanes=["security"],
        phase=phase,
        summary="Ran bounded read-only security preflight.",
        findings=findings,
        constraints=["Secret-like values are reported by key/path only; values are not exposed."],
        url_or_path=cwd,
    )


def _collect_data_logs(task_id: str, artifact_path: str, query: str, phase: str) -> dict[str, Any]:
    path = Path(artifact_path or "")
    if not artifact_path or not path.exists():
        return make_ability_packet(
            task_id=task_id,
            lanes=["data_logs"],
            phase=phase,
            status="unavailable",
            summary="Data/log evidence needs an existing artifact_path.",
            constraints=["No readable log/report/JSON/CSV/XML artifact_path was provided."],
            url_or_path=artifact_path,
        )
    findings: list[Any] = [{"summary": "artifact", "path": str(path), "message": f"{path.stat().st_size} bytes"}]
    suffix = path.suffix.lower()
    try:
        if suffix == ".csv":
            with path.open(newline="", encoding="utf-8", errors="replace") as handle:
                reader = csv.reader(handle)
                rows = [row for _, row in zip(range(8), reader)]
            findings.append({"summary": "csv preview", "message": json.dumps(rows, ensure_ascii=False)})
        elif suffix in {".json", ".jsonl"}:
            text = path.read_text(encoding="utf-8", errors="replace")[:_MAX_CAPTURE_CHARS]
            if suffix == ".json":
                parsed = json.loads(text)
                findings.append({"summary": "json top-level", "message": json.dumps(parsed, ensure_ascii=False)[:2000]})
            else:
                findings.append({"summary": "jsonl preview", "message": "\n".join(text.splitlines()[:12])})
        else:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            error_lines = [line for line in lines if _ERROR_LINE_RE.search(line)]
            if query:
                error_lines.extend([line for line in lines if query.lower() in line.lower()])
            findings.append({"summary": "top error/failure lines", "message": "\n".join(error_lines[:20] or lines[:20])})
    except Exception as exc:
        findings.append({"summary": "parse failed", "error": str(exc)})
    return make_ability_packet(
        task_id=task_id,
        lanes=["data_logs"],
        phase=phase,
        summary="Summarized bounded data/log artifact evidence.",
        findings=findings,
        artifact_paths=[str(path)],
        url_or_path=str(path),
    )


def _collect_audio_video(task_id: str, artifact_path: str, phase: str) -> dict[str, Any]:
    path = Path(artifact_path or "")
    if not artifact_path or not path.exists():
        return make_ability_packet(
            task_id=task_id,
            lanes=["audio_video"],
            phase=phase,
            status="unavailable",
            summary="Audio/video lane needs an existing artifact_path.",
            constraints=["No media artifact_path was provided."],
            url_or_path=artifact_path,
        )
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return make_ability_packet(
            task_id=task_id,
            lanes=["audio_video"],
            phase=phase,
            status="unavailable",
            summary="Audio/video metadata unavailable because ffprobe was not found.",
            constraints=["ffprobe is required for bounded media metadata in V1."],
            artifact_paths=[str(path)],
            url_or_path=str(path),
        )
    result = _run_readonly(
        [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        timeout=8,
    )
    status = "success" if result.get("success") else "unavailable"
    return make_ability_packet(
        task_id=task_id,
        lanes=["audio_video"],
        phase=phase,
        status=status,
        summary="Collected bounded audio/video metadata." if status == "success" else "Audio/video metadata unavailable.",
        findings=[{"summary": "ffprobe metadata", "message": result.get("output") or result.get("error") or ""}],
        constraints=["No large media transcription was run automatically."],
        artifact_paths=[str(path)],
        url_or_path=str(path),
    )


async def ability_context_tool(
    *,
    mode: str = "auto",
    lanes: Optional[list[str]] = None,
    task: str = "",
    workdir: str = "",
    url: str = "",
    artifact_path: str = "",
    query: str = "",
    phase: str = "pre",
    force_refresh: bool = False,
    cleanup_policy: str = "auto",
    task_id: str = "",
    user_task: Optional[str] = None,
) -> str:
    clean_mode = str(mode or "auto").strip().lower()
    clean_phase = str(phase or "pre").strip().lower()
    if clean_phase not in {"pre", "post"}:
        clean_phase = "pre"
    clean_cleanup = str(cleanup_policy or "auto").strip().lower()
    if clean_cleanup not in {"auto", "always", "never"}:
        clean_cleanup = "auto"

    if clean_mode == "clear_cache":
        clear_ability_cache(task_id)
        return tool_result({"success": True, "mode": clean_mode, "task_id": task_id, "packets": [], "cleared": True})

    if clean_mode == "cache_status":
        packets = get_ability_packets(task_id, include_stale=True)
        return tool_result({"success": True, "mode": clean_mode, "task_id": task_id, "packets": packets})

    requested_lanes = normalize_lanes(lanes or [])
    if clean_mode == "auto" and not requested_lanes:
        requirements = get_ability_requirements(task_id)
        requested_lanes = required_lanes(requirements)
        if not requested_lanes:
            detected = detect_ability_requirements(" ".join([task, query, user_task or ""]))
            requested_lanes = required_lanes(detected)
    if clean_mode == "health" and not requested_lanes:
        requested_lanes = list(ABILITY_LANES)
    if clean_mode == "collect" and not requested_lanes:
        requested_lanes = required_lanes(detect_ability_requirements(" ".join([task, query, url, artifact_path])))

    if not requested_lanes:
        packet = make_ability_packet(
            task_id=task_id,
            lanes=[],
            phase=clean_phase,
            summary="No ability lanes were required or requested.",
            findings=[],
        )
        return tool_result({"success": True, "mode": clean_mode, "task_id": task_id, "packets": [packet]})

    packets: list[dict[str, Any]] = []
    for lane in requested_lanes:
        if clean_mode == "health":
            health = _visual_health(url, task=task, query=query) if lane == "visual" else {"available": True}
            packet = make_ability_packet(
                task_id=task_id,
                lanes=[lane],
                phase=clean_phase,
                status="success",
                summary=f"{lane} ability lane health checked.",
                health=health,
                url_or_path=url or artifact_path or workdir,
            )
        elif lane == "visual":
            packet = await _collect_visual(
                task_id=task_id,
                task=task or user_task or "",
                query=query,
                url=url,
                artifact_path=artifact_path,
                phase=clean_phase,
                force_refresh=force_refresh,
                cleanup_policy=clean_cleanup,
            )
        elif lane == "runtime":
            packet = _collect_runtime(task_id, task or user_task or "", workdir, clean_phase)
        elif lane == "environment":
            packet = _collect_environment(task_id, workdir, url, clean_phase)
        elif lane == "repo_archaeology":
            packet = _collect_repo_archaeology(task_id, workdir, query or task or "", clean_phase)
        elif lane == "external_docs":
            packet = _collect_external_docs(task_id, query or task or "", clean_phase)
        elif lane == "security":
            packet = _collect_security(task_id, workdir, task or user_task or query, clean_phase)
        elif lane == "data_logs":
            packet = _collect_data_logs(task_id, artifact_path, query, clean_phase)
        elif lane == "audio_video":
            packet = _collect_audio_video(task_id, artifact_path, clean_phase)
        else:
            packet = make_ability_packet(
                task_id=task_id,
                lanes=[lane],
                phase=clean_phase,
                status="unavailable",
                summary=f"Unknown ability lane: {lane}",
                constraints=["Lane is not supported."],
            )
        record_ability_packet(task_id, packet)
        packets.append(packet)

    return tool_result(
        {
            "success": any(packet.get("status") == "success" for packet in packets),
            "mode": clean_mode,
            "task_id": task_id,
            "lanes": requested_lanes,
            "phase": clean_phase,
            "packets": packets,
        }
    )


def _handle_ability_context(args: Dict[str, Any], **kw: Any):
    return ability_context_tool(
        mode=args.get("mode", "auto"),
        lanes=args.get("lanes") or [],
        task=args.get("task", ""),
        workdir=args.get("workdir", ""),
        url=args.get("url", ""),
        artifact_path=args.get("artifact_path", ""),
        query=args.get("query", ""),
        phase=args.get("phase", "pre"),
        force_refresh=bool(args.get("force_refresh", False)),
        cleanup_policy=args.get("cleanup_policy", "auto"),
        task_id=kw.get("task_id", ""),
        user_task=kw.get("user_task"),
    )


registry.register(
    name="ability_context",
    toolset="ability_context",
    schema=ABILITY_CONTEXT_SCHEMA,
    handler=_handle_ability_context,
    is_async=True,
    emoji="🧭",
)
