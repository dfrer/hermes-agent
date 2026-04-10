#!/usr/bin/env python3
"""Routed task graph tool for routing-layer controlled work."""

from __future__ import annotations

import json
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import PurePosixPath
from typing import Any, Optional

from agent.routing_guard import (
    get_ability_handoff,
    get_routed_plan_state,
    get_routing_decision,
    get_session_lane_context,
    hydrate_routed_plan_from_persistence,
    mark_routed_plan_node_success,
    clear_routed_plan_state,
    set_routed_plan_state,
)
from agent.routing_plan import (
    DEFAULT_RUN_ALL_MAX_NODES,
    MAX_PLAN_NODES,
    block_dependents,
    dependency_summaries,
    next_runnable_node,
    plan_complete,
    plan_runtime_status,
    public_plan_state,
    ready_nodes,
    validate_and_build_plan,
)
from agent.routing_plan_store import mark_plan_reset, save_plan_snapshot
from agent.routing_policy import normalize_route_model
from tools.registry import registry, tool_error, tool_result
from tools.routed_exec_tool import execute_routed_context


_ACTIONS = {"submit", "status", "run_next", "run_all", "run_parallel", "critique", "reset"}
_MAX_PARALLEL_CONCURRENCY = 4
_GLOB_META_RE = re.compile(r"[*?\[\]{}]")


def _route_targets_for_node(node: dict[str, Any]) -> list[dict[str, Any]]:
    route = node.get("route") if isinstance(node.get("route"), dict) else {}
    profile = route.get("profile") if isinstance(route.get("profile"), dict) else {}
    primary = profile.get("primary") if isinstance(profile.get("primary"), dict) else {}
    fallbacks = [dict(item) for item in profile.get("fallbacks", []) if isinstance(item, dict)]
    if not primary:
        return []

    selected = normalize_route_model(str(node.get("model", "") or ""))
    if selected == normalize_route_model(str(primary.get("label", "") or "")):
        return [dict(primary), *fallbacks]
    for fallback in fallbacks:
        if selected == normalize_route_model(str(fallback.get("label", "") or "")):
            return [dict(fallback)]
    return [dict(primary), *fallbacks]


def _format_dependency_summaries(plan: dict[str, Any], node: dict[str, Any]) -> str:
    summaries = dependency_summaries(plan, node)
    if not summaries:
        return "[]"
    return json.dumps(summaries, ensure_ascii=False, indent=2)


def _node_task_prompt(plan: dict[str, Any], node: dict[str, Any]) -> str:
    write_scope = "\n".join(f"- {item}" for item in node.get("write_scope", []) if str(item or "").strip())
    verification = str(node.get("verification", "") or "").strip() or "Run the narrowest relevant verification."
    return (
        f"{str(node.get('goal', '')).strip()}\n\n"
        "Routed plan node context:\n"
        f"- Parent plan: {str(plan.get('summary', '') or '').strip()}\n"
        f"- Node id: {node.get('id')}\n"
        f"- Route: {node.get('tier')}/{node.get('path')} using {node.get('model')}\n"
        f"- Owned write scope:\n{write_scope}\n"
        "- Do not edit outside the owned write scope unless required to keep the build coherent; "
        "if that is required, report the exact reason and path.\n\n"
        f"Completed dependency summaries:\n{_format_dependency_summaries(plan, node)}\n\n"
        f"Verification expectation:\n{verification}\n"
    )


def _node_evidence(plan: dict[str, Any], node: dict[str, Any]) -> str:
    evidence = {
        "plan_id": plan.get("plan_id", ""),
        "node_id": node.get("id", ""),
        "node_evidence": node.get("evidence", ""),
        "dependency_summaries": dependency_summaries(plan, node),
        "write_scope": node.get("write_scope", []),
    }
    return json.dumps(evidence, ensure_ascii=False)


def _warnings_from_execution(execution: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    for key in ("warnings", "warning_kinds"):
        for item in execution.get(key, []) if isinstance(execution.get(key), list) else []:
            text = str(item or "").strip()
            if text and text not in warnings:
                warnings.append(text)
    guidance = str(execution.get("failure_guidance") or "").strip()
    if guidance and guidance not in warnings:
        warnings.append(guidance)
    return warnings


def _clean_optional(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text or text.lower() == "none":
        return None
    return text


def _node_result_from_execution(node: dict[str, Any], execution: dict[str, Any]) -> dict[str, Any]:
    attempts = [item for item in execution.get("attempts", []) if isinstance(item, dict)]
    final_attempt = attempts[-1] if attempts else {}
    failure_kind = (
        str(execution.get("failure_kind") or "").strip()
        or str(final_attempt.get("failure_kind") or "").strip()
    )
    return {
        "id": node.get("id", ""),
        "status": "completed" if execution.get("success") else "failed",
        "tier": node.get("tier", ""),
        "path": node.get("path", ""),
        "model": node.get("model", ""),
        "executors_attempted": list(execution.get("executors_attempted", [])),
        "summary": str(execution.get("summary") or "").strip(),
        "verification": str(execution.get("verification") or "").strip(),
        "warnings": _warnings_from_execution(execution),
        "output_excerpt": str(execution.get("output_excerpt") or "").strip(),
        "output_path": _clean_optional(execution.get("output_path")),
        "failure_kind": failure_kind or None,
    }


def _run_node_execution(plan: dict[str, Any], node: dict[str, Any], task_id: str) -> dict[str, Any]:
    decision = {
        "tier": node.get("tier", ""),
        "path": node.get("path", ""),
        "model": node.get("model", ""),
    }
    return execute_routed_context(
        _node_task_prompt(plan, node),
        str(node.get("workdir", "") or ""),
        decision=decision,
        route_targets=_route_targets_for_node(node),
        selected_route=node.get("route") if isinstance(node.get("route"), dict) else {},
        session_lane=get_session_lane_context(task_id),
        task_id=f"{task_id}-{node.get('id', 'node')}",
        timeout=node.get("timeout"),
        evidence=_node_evidence(plan, node),
        ability_evidence=get_ability_handoff(task_id),
    )


def _apply_node_execution_result(
    plan: dict[str, Any],
    node: dict[str, Any],
    task_id: str,
    execution: dict[str, Any],
) -> dict[str, Any]:
    result = _node_result_from_execution(node, execution)
    node["result"] = result
    node["status"] = result["status"]
    if not execution.get("success"):
        block_dependents(plan, str(node.get("id", "")))
    else:
        mark_routed_plan_node_success(task_id)
    plan["updated_at"] = time.time()
    return result


def _persist_plan(
    *,
    task_id: str,
    session_id: str,
    plan: dict[str, Any],
    parent_decision: dict[str, Any],
    last_error: str = "",
) -> bool:
    return save_plan_snapshot(
        task_id=task_id,
        session_id=session_id,
        plan=plan,
        parent_decision=parent_decision,
        status=plan_runtime_status(plan),
        last_error=last_error,
    )


def _execute_node(
    plan: dict[str, Any],
    node: dict[str, Any],
    task_id: str,
    *,
    session_id: str,
    parent_decision: dict[str, Any],
) -> dict[str, Any]:
    node["status"] = "running"
    node["lease"] = {
        "id": f"seq-{uuid.uuid4().hex[:12]}",
        "started_at": time.time(),
        "mode": "sequential",
    }
    plan["updated_at"] = time.time()
    set_routed_plan_state(task_id, plan)
    _persist_plan(task_id=task_id, session_id=session_id, plan=plan, parent_decision=parent_decision)

    execution = _run_node_execution(plan, node, task_id)
    node.pop("lease", None)
    result = _apply_node_execution_result(plan, node, task_id, execution)
    set_routed_plan_state(task_id, plan)
    _persist_plan(
        task_id=task_id,
        session_id=session_id,
        plan=plan,
        parent_decision=parent_decision,
        last_error=str(result.get("failure_kind") or "") if result.get("status") != "completed" else "",
    )
    return result


def _load_plan_or_error(task_id: str, plan_id: str = "", session_id: str = "") -> tuple[Optional[dict[str, Any]], Optional[str]]:
    expected = str(plan_id or "").strip()
    plan = get_routed_plan_state(task_id)
    if (not plan) or (expected and expected != str(plan.get("plan_id", ""))):
        hydrate_routed_plan_from_persistence(task_id, session_id=session_id, plan_id=expected)
        plan = get_routed_plan_state(task_id)
    if not plan:
        return None, "No active routed plan for this task. Submit a plan first."
    if expected and expected != str(plan.get("plan_id", "")):
        return None, f"Active routed plan id is `{plan.get('plan_id', '')}`, not `{expected}`."
    return plan, None


def _response_with_resume(payload: dict[str, Any], *, session_id: str, plan: Optional[dict[str, Any]], persistent: bool) -> dict[str, Any]:
    result = dict(payload)
    result["persistent"] = bool(persistent)
    result["resume_key"] = {
        "session_id": str(session_id or ""),
        "plan_id": str(plan.get("plan_id", "") if isinstance(plan, dict) else ""),
    }
    return result


def _coerce_limit(value: Optional[int], *, default: int, maximum: int, label: str) -> tuple[Optional[int], Optional[str]]:
    try:
        limit = default if value is None else int(value)
    except (TypeError, ValueError):
        return None, f"`{label}` must be an integer."
    return max(1, min(limit, maximum)), None


def _scope_has_glob(scope: str) -> bool:
    return bool(_GLOB_META_RE.search(str(scope or "")))


def _normalize_scope(scope: str, workdir: str) -> str:
    text = str(scope or "").strip().replace("\\", "/")
    base = str(workdir or "").strip().replace("\\", "/")
    if not text:
        return ""
    if _scope_has_glob(text):
        return text
    drive_path = bool(re.match(r"^[A-Za-z]:/", text))
    if text.startswith("/") or drive_path:
        normalized = text
    elif base:
        normalized = f"{base.rstrip('/')}/{text}"
    else:
        normalized = text
    normalized = str(PurePosixPath(normalized))
    return normalized.rstrip("/") or normalized


def _scope_conflicts(left: str, right: str, left_workdir: str, right_workdir: str) -> bool:
    if _scope_has_glob(left) or _scope_has_glob(right):
        return True
    a = _normalize_scope(left, left_workdir)
    b = _normalize_scope(right, right_workdir)
    if not a or not b:
        return True
    return a == b or a.startswith(f"{b}/") or b.startswith(f"{a}/")


def _write_scopes_conflict(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_workdir = str(left.get("workdir") or "")
    right_workdir = str(right.get("workdir") or "")
    left_scopes = [str(item or "") for item in left.get("write_scope", []) if str(item or "").strip()]
    right_scopes = [str(item or "") for item in right.get("write_scope", []) if str(item or "").strip()]
    if not left_scopes or not right_scopes:
        return True
    return any(_scope_conflicts(a, b, left_workdir, right_workdir) for a in left_scopes for b in right_scopes)


def _select_parallel_wave(plan: dict[str, Any], *, max_nodes: int, max_concurrency: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for node in ready_nodes(plan):
        if len(selected) >= max_nodes or len(selected) >= max_concurrency:
            break
        if any(_write_scopes_conflict(node, existing) for existing in selected):
            continue
        selected.append(node)
    return selected


def _execute_parallel_wave(
    plan: dict[str, Any],
    wave: list[dict[str, Any]],
    task_id: str,
    *,
    session_id: str,
    parent_decision: dict[str, Any],
    max_concurrency: int,
) -> list[dict[str, Any]]:
    lease_id = f"parallel-{uuid.uuid4().hex[:12]}"
    lease_started = time.time()
    for node in wave:
        node["status"] = "running"
        node["lease"] = {
            "id": lease_id,
            "started_at": lease_started,
            "mode": "parallel",
        }
    plan["updated_at"] = time.time()
    set_routed_plan_state(task_id, plan)
    _persist_plan(task_id=task_id, session_id=session_id, plan=plan, parent_decision=parent_decision)

    execution_by_id: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(max_concurrency, len(wave))) as pool:
        future_to_node = {
            pool.submit(_run_node_execution, plan, node, task_id): node
            for node in wave
        }
        for future in as_completed(future_to_node):
            node = future_to_node[future]
            node_id = str(node.get("id", ""))
            try:
                execution_by_id[node_id] = future.result()
            except Exception as exc:
                execution_by_id[node_id] = {
                    "success": False,
                    "executors_attempted": [],
                    "summary": f"node execution raised: {exc}",
                    "verification": "",
                    "warnings": [str(exc)],
                    "attempts": [{"failure_kind": "execution_exception"}],
                    "failure_kind": "execution_exception",
                    "status": "failed",
                }

    results: list[dict[str, Any]] = []
    for node in wave:
        node.pop("lease", None)
        node_id = str(node.get("id", ""))
        result = _apply_node_execution_result(plan, node, task_id, execution_by_id.get(node_id, {"success": False}))
        results.append(result)
    set_routed_plan_state(task_id, plan)
    failure = next((item for item in results if item.get("status") != "completed"), None)
    _persist_plan(
        task_id=task_id,
        session_id=session_id,
        plan=plan,
        parent_decision=parent_decision,
        last_error=str(failure.get("failure_kind") or "") if isinstance(failure, dict) else "",
    )
    return results


def _plan_status_for_response(plan: dict[str, Any], executed: list[dict[str, Any]]) -> str:
    if plan_complete(plan):
        return "complete"
    if any(item.get("status") == "failed" for item in executed):
        return "failed"
    if any(node.get("status") == "blocked" for node in plan.get("nodes", []) if isinstance(node, dict)):
        return "blocked"
    return "partial"


def _ordered_results(plan: dict[str, Any], executed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {str(item.get("id", "")): item for item in executed if isinstance(item, dict)}
    ordered: list[dict[str, Any]] = []
    for node in plan.get("nodes", []):
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id", ""))
        if node_id in by_id:
            ordered.append(by_id[node_id])
    return ordered


def _critique_prompt(plan: dict[str, Any], target: str) -> str:
    public = public_plan_state(plan)
    return (
        "Read-only critique request for a Hermes routed_plan.\n\n"
        f"Target: {target}\n\n"
        "Critique only these items: plan structure, dependency risks, write-scope conflicts, "
        "failure explanations, and verification gaps. Do not propose direct file edits, do not "
        "claim routing compliance, and do not treat this critique as final verification.\n\n"
        f"Plan state:\n{json.dumps(public, ensure_ascii=False, indent=2)}"
    )


def _run_moa_critique(plan: dict[str, Any], target: str) -> dict[str, Any]:
    try:
        from model_tools import _run_async
        from tools.mixture_of_agents_tool import mixture_of_agents_tool

        raw = _run_async(mixture_of_agents_tool(user_prompt=_critique_prompt(plan, target)))
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            payload = {"success": True, "response": str(raw)}
        return {"available": True, "result": payload}
    except Exception as exc:
        return {"available": False, "error": str(exc)}


def _critique_success(critique: dict[str, Any]) -> bool:
    if not critique.get("available"):
        return False
    payload = critique.get("result")
    if isinstance(payload, dict) and payload.get("success") is False:
        return False
    return True


def routed_plan_tool(
    action: str,
    plan: Optional[dict[str, Any]] = None,
    *,
    plan_id: str = "",
    max_nodes: Optional[int] = None,
    max_concurrency: Optional[int] = None,
    target: str = "plan",
    task_id: str = "",
    session_id: str = "",
) -> str:
    normalized_action = str(action or "").strip().lower()
    if normalized_action not in _ACTIONS:
        return tool_error(f"`action` must be one of: {', '.join(sorted(_ACTIONS))}.")

    if normalized_action == "reset":
        loaded_plan, _ = _load_plan_or_error(task_id, plan_id, session_id)
        reset_plan = loaded_plan if isinstance(loaded_plan, dict) else None
        if reset_plan:
            mark_plan_reset(str(reset_plan.get("plan_id", "")), last_error="reset by routed_plan tool")
        clear_routed_plan_state(task_id)
        return tool_result(
            _response_with_resume(
                {"success": True, "status": "reset", "plan": None},
                session_id=session_id,
                plan=reset_plan,
                persistent=bool(reset_plan),
            )
        )

    if normalized_action != "submit":
        hydrate_routed_plan_from_persistence(task_id, session_id=session_id, plan_id=plan_id)
    parent_decision = get_routing_decision(task_id)
    if not parent_decision:
        return tool_error("No active parent routing decision for this task. Emit the routing line first.")

    if normalized_action == "submit":
        built, errors, warnings = validate_and_build_plan(plan or {}, parent_decision)
        if errors or not built:
            return tool_result(
                {
                    "success": False,
                    "status": "invalid",
                    "errors": errors,
                    "blocked": errors,
                    "warnings": warnings,
                    "plan": None,
                }
            )
        built["parent_decision"] = dict(parent_decision)
        set_routed_plan_state(task_id, built)
        persistent = _persist_plan(task_id=task_id, session_id=session_id, plan=built, parent_decision=parent_decision)
        public = public_plan_state(built)
        return tool_result(
            _response_with_resume(
                {
                    "success": True,
                    "status": "submitted",
                    "plan_id": built.get("plan_id", ""),
                    "ordered_nodes": [node.get("id", "") for node in built.get("nodes", [])],
                    "blocked": [],
                    "warnings": warnings,
                    "next_node": public.get("next_node") if isinstance(public, dict) else None,
                    "plan": public,
                },
                session_id=session_id,
                plan=built,
                persistent=persistent,
            )
        )

    loaded_plan, error = _load_plan_or_error(task_id, plan_id, session_id)
    if error or not loaded_plan:
        return tool_error(error or "No active routed plan for this task.")
    parent_decision = get_routing_decision(task_id) or parent_decision
    persistent = _persist_plan(task_id=task_id, session_id=session_id, plan=loaded_plan, parent_decision=parent_decision)

    if normalized_action == "status":
        return tool_result(
            _response_with_resume(
                {"success": True, "status": "status", "plan": public_plan_state(loaded_plan)},
                session_id=session_id,
                plan=loaded_plan,
                persistent=persistent,
            )
        )

    if normalized_action == "critique":
        normalized_target = str(target or "plan").strip().lower()
        if normalized_target not in {"plan", "results"}:
            return tool_error("`target` must be `plan` or `results` for routed_plan critique.")
        critique = _run_moa_critique(loaded_plan, normalized_target)
        critique_ok = _critique_success(critique)
        return tool_result(
            _response_with_resume(
                {
                    "success": critique_ok,
                    "status": "critique" if critique_ok else "unavailable",
                    "target": normalized_target,
                    "critique": critique.get("result"),
                    "error": critique.get("error"),
                    "plan": public_plan_state(loaded_plan),
                },
                session_id=session_id,
                plan=loaded_plan,
                persistent=persistent,
            )
        )

    executed: list[dict[str, Any]] = []
    if normalized_action == "run_next":
        node = next_runnable_node(loaded_plan)
        if not node:
            return tool_result(
                _response_with_resume(
                    {
                        "success": plan_complete(loaded_plan),
                        "status": "complete" if plan_complete(loaded_plan) else "blocked",
                        "executed_nodes": [],
                        "plan": public_plan_state(loaded_plan),
                    },
                    session_id=session_id,
                    plan=loaded_plan,
                    persistent=persistent,
                )
            )
        executed.append(_execute_node(loaded_plan, node, task_id, session_id=session_id, parent_decision=parent_decision))
        node_succeeded = bool(executed[-1].get("status") == "completed")
        complete = plan_complete(loaded_plan)
        return tool_result(
            _response_with_resume(
                {
                    "success": node_succeeded,
                    "status": "complete" if complete else ("partial" if node_succeeded else "failed"),
                    "executed_nodes": executed,
                    "plan": public_plan_state(loaded_plan),
                },
                session_id=session_id,
                plan=loaded_plan,
                persistent=True,
            )
        )

    limit, error_text = _coerce_limit(max_nodes, default=DEFAULT_RUN_ALL_MAX_NODES, maximum=MAX_PLAN_NODES, label="max_nodes")
    if error_text or limit is None:
        return tool_error(error_text or "`max_nodes` must be an integer.")

    if normalized_action == "run_parallel":
        concurrency, concurrency_error = _coerce_limit(
            max_concurrency,
            default=2,
            maximum=_MAX_PARALLEL_CONCURRENCY,
            label="max_concurrency",
        )
        if concurrency_error or concurrency is None:
            return tool_error(concurrency_error or "`max_concurrency` must be an integer.")
        while len(executed) < limit:
            wave = _select_parallel_wave(
                loaded_plan,
                max_nodes=limit - len(executed),
                max_concurrency=concurrency,
            )
            if not wave:
                break
            results = _execute_parallel_wave(
                loaded_plan,
                wave,
                task_id,
                session_id=session_id,
                parent_decision=parent_decision,
                max_concurrency=concurrency,
            )
            executed.extend(results)
            if any(result.get("status") != "completed" for result in results):
                break
        return tool_result(
            _response_with_resume(
                {
                    "success": plan_complete(loaded_plan),
                    "status": _plan_status_for_response(loaded_plan, executed),
                    "executed_nodes": _ordered_results(loaded_plan, executed),
                    "max_nodes": limit,
                    "max_concurrency": concurrency,
                    "plan": public_plan_state(loaded_plan),
                },
                session_id=session_id,
                plan=loaded_plan,
                persistent=True,
            )
        )

    for _ in range(limit):
        node = next_runnable_node(loaded_plan)
        if not node:
            break
        result = _execute_node(loaded_plan, node, task_id, session_id=session_id, parent_decision=parent_decision)
        executed.append(result)
        if result.get("status") != "completed":
            break

    complete = plan_complete(loaded_plan)
    status = "complete" if complete else ("failed" if any(item.get("status") == "failed" for item in executed) else "partial")
    return tool_result(
        _response_with_resume(
            {
                "success": complete,
                "status": status,
                "executed_nodes": executed,
                "max_nodes": limit,
                "plan": public_plan_state(loaded_plan),
            },
            session_id=session_id,
            plan=loaded_plan,
            persistent=True,
        )
    )


def check_routed_plan_requirements() -> bool:
    return True


ROUTED_PLAN_SCHEMA = {
    "name": "routed_plan",
    "description": (
        "Submit, inspect, resume, critique, and run a routed task DAG for the active routing-layer task. "
        "Sequential run_all remains the default; run_parallel is capped and requires disjoint write_scope."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(_ACTIONS),
                "description": "Plan operation to perform.",
            },
            "plan": {
                "type": "object",
                "description": "Plan object required for submit: summary, optional workdir, and nodes.",
                "properties": {
                    "summary": {"type": "string"},
                    "workdir": {"type": "string"},
                    "nodes": {
                        "type": "array",
                        "maxItems": MAX_PLAN_NODES,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "goal": {"type": "string"},
                                "tier": {"type": "string", "enum": ["3A", "3B", "3C"]},
                                "path": {"type": "string"},
                                "model": {"type": "string"},
                                "write_scope": {"type": "array", "items": {"type": "string"}},
                                "workdir": {"type": "string"},
                                "depends_on": {"type": "array", "items": {"type": "string"}},
                                "verification": {"type": "string"},
                                "timeout": {"type": "integer", "minimum": 1},
                                "evidence": {"type": "string"},
                            },
                            "required": ["id", "goal", "tier", "path", "model", "write_scope"],
                        },
                    },
                },
                "required": ["summary", "nodes"],
            },
            "plan_id": {
                "type": "string",
                "description": "Optional guard to ensure run/status targets the active plan id.",
            },
            "max_nodes": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_PLAN_NODES,
                "description": "For run_all/run_parallel, max nodes to execute. Defaults to 3 and is capped at 8.",
            },
            "max_concurrency": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_PARALLEL_CONCURRENCY,
                "description": "For run_parallel, max nodes to launch in a wave. Defaults to 2 and is capped at 4.",
            },
            "target": {
                "type": "string",
                "enum": ["plan", "results"],
                "description": "For critique, whether MoA should critique the plan structure or current results.",
            },
        },
        "required": ["action"],
    },
}


def _handle_routed_plan(args, **kw):
    return routed_plan_tool(
        action=args.get("action", "status"),
        plan=args.get("plan"),
        plan_id=args.get("plan_id", ""),
        max_nodes=args.get("max_nodes"),
        max_concurrency=args.get("max_concurrency"),
        target=args.get("target", "plan"),
        task_id=kw.get("task_id", ""),
        session_id=kw.get("session_id", ""),
    )


registry.register(
    name="routed_plan",
    toolset="routing",
    schema=ROUTED_PLAN_SCHEMA,
    handler=_handle_routed_plan,
    check_fn=check_routed_plan_requirements,
    emoji="RP",
)
