"""In-memory routed task graph helpers for routing-layer execution."""

from __future__ import annotations

import copy
import time
import uuid
from typing import Any, Optional

from agent.routing_policy import TIER_RANK, validate_route_choice


MAX_PLAN_NODES = 8
DEFAULT_RUN_ALL_MAX_NODES = 3

PLAN_STATUSES = {"pending", "running", "completed", "failed", "blocked"}


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _clean_id(value: Any) -> str:
    return _clean_text(value)


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = _clean_text(item)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _coerce_timeout(value: Any, errors: list[str], node_id: str) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        errors.append(f"node {node_id}: timeout must be an integer")
        return None
    if timeout < 1:
        errors.append(f"node {node_id}: timeout must be positive")
        return None
    return timeout


def _find_cycle(ids: list[str], deps: dict[str, list[str]]) -> Optional[list[str]]:
    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []

    def visit(node_id: str) -> Optional[list[str]]:
        if node_id in visited:
            return None
        if node_id in visiting:
            if node_id in stack:
                return [*stack[stack.index(node_id) :], node_id]
            return [node_id, node_id]
        visiting.add(node_id)
        stack.append(node_id)
        for dep_id in deps.get(node_id, []):
            cycle = visit(dep_id)
            if cycle:
                return cycle
        stack.pop()
        visiting.remove(node_id)
        visited.add(node_id)
        return None

    for node_id in ids:
        cycle = visit(node_id)
        if cycle:
            return cycle
    return None


def _topological_order(ids: list[str], deps: dict[str, list[str]]) -> list[str]:
    remaining = list(ids)
    emitted: list[str] = []
    emitted_set: set[str] = set()
    while remaining:
        progressed = False
        for node_id in list(remaining):
            if all(dep_id in emitted_set for dep_id in deps.get(node_id, [])):
                emitted.append(node_id)
                emitted_set.add(node_id)
                remaining.remove(node_id)
                progressed = True
        if not progressed:
            return ids
    return emitted


def validate_and_build_plan(raw_plan: dict[str, Any], parent_decision: dict[str, Any]) -> tuple[Optional[dict[str, Any]], list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(raw_plan, dict):
        return None, ["plan must be an object"], warnings

    raw_nodes = raw_plan.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        return None, ["plan.nodes must be a non-empty list"], warnings
    if len(raw_nodes) > MAX_PLAN_NODES:
        errors.append(f"plan.nodes must contain at most {MAX_PLAN_NODES} nodes")

    summary = _clean_text(raw_plan.get("summary"))
    default_workdir = _clean_text(raw_plan.get("workdir") or raw_plan.get("default_workdir"))
    parent_tier = _clean_text(parent_decision.get("tier")).upper()
    parent_rank = TIER_RANK.get(parent_tier, 0)

    nodes_by_id: dict[str, dict[str, Any]] = {}
    input_ids: list[str] = []
    deps_by_id: dict[str, list[str]] = {}

    for index, raw_node in enumerate(raw_nodes, start=1):
        if not isinstance(raw_node, dict):
            errors.append(f"node {index}: must be an object")
            continue

        node_id = _clean_id(raw_node.get("id"))
        label = node_id or str(index)
        if not node_id:
            errors.append(f"node {index}: id is required")
            continue
        if node_id in nodes_by_id:
            errors.append(f"node {node_id}: duplicate id")
            continue

        goal = _clean_text(raw_node.get("goal"))
        if not goal:
            errors.append(f"node {node_id}: goal is required")

        tier = _clean_text(raw_node.get("tier")).upper()
        path = _clean_text(raw_node.get("path"))
        model = _clean_text(raw_node.get("model"))
        route = validate_route_choice(tier, path, model)
        if not route.ok:
            errors.extend(f"node {label}: {error}" for error in route.errors)
        if parent_rank and TIER_RANK.get(route.tier, 0) > parent_rank:
            errors.append(f"node {node_id}: route {route.tier}/{route.path} exceeds parent route tier {parent_tier}")

        workdir = _clean_text(raw_node.get("workdir")) or default_workdir
        if not workdir:
            errors.append(f"node {node_id}: workdir is required or plan.workdir must be set")

        write_scope = _string_list(raw_node.get("write_scope"))
        if not write_scope:
            errors.append(f"node {node_id}: write_scope must name at least one owned path or glob")

        depends_on = _string_list(raw_node.get("depends_on"))
        timeout = _coerce_timeout(raw_node.get("timeout"), errors, node_id)
        node = {
            "id": node_id,
            "goal": goal,
            "tier": route.tier,
            "path": route.path,
            "model": model,
            "workdir": workdir,
            "write_scope": write_scope,
            "depends_on": depends_on,
            "verification": _clean_text(raw_node.get("verification")),
            "timeout": timeout,
            "evidence": _clean_text(raw_node.get("evidence")),
            "status": "pending",
            "result": None,
            "route": {
                "policy_version": route.policy_version,
                "tier": route.tier,
                "path": route.path,
                "model": model,
                "profile": route.profile,
                "selected_target": route.selected_target,
            },
        }
        nodes_by_id[node_id] = node
        input_ids.append(node_id)
        deps_by_id[node_id] = depends_on

    known_ids = set(nodes_by_id)
    for node_id, deps in deps_by_id.items():
        missing = [dep_id for dep_id in deps if dep_id not in known_ids]
        if missing:
            errors.append(f"node {node_id}: missing dependency id(s): {', '.join(missing)}")

    if not errors:
        cycle = _find_cycle(input_ids, deps_by_id)
        if cycle:
            errors.append(f"plan dependency cycle: {' -> '.join(cycle)}")

    if errors:
        return None, errors, warnings

    ordered_ids = _topological_order(input_ids, deps_by_id)
    plan_id = _clean_text(raw_plan.get("plan_id")) or f"routed-plan-{uuid.uuid4().hex[:12]}"
    nodes = [nodes_by_id[node_id] for node_id in ordered_ids]
    return {
        "plan_id": plan_id,
        "summary": summary,
        "workdir": default_workdir,
        "nodes": nodes,
        "created_at": time.time(),
        "updated_at": time.time(),
    }, errors, warnings


def next_runnable_node(plan: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not isinstance(plan, dict):
        return None
    nodes = [node for node in plan.get("nodes", []) if isinstance(node, dict)]
    completed = {str(node.get("id")) for node in nodes if node.get("status") == "completed"}
    for node in nodes:
        if node.get("status") != "pending":
            continue
        if all(dep_id in completed for dep_id in _string_list(node.get("depends_on"))):
            return node
    return None


def ready_nodes(plan: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return pending nodes whose dependencies are all completed, in plan order."""
    if not isinstance(plan, dict):
        return []
    nodes = [node for node in plan.get("nodes", []) if isinstance(node, dict)]
    completed = {str(node.get("id")) for node in nodes if node.get("status") == "completed"}
    ready: list[dict[str, Any]] = []
    for node in nodes:
        if node.get("status") != "pending":
            continue
        if all(dep_id in completed for dep_id in _string_list(node.get("depends_on"))):
            ready.append(node)
    return ready


def block_dependents(plan: dict[str, Any], failed_node_id: str) -> None:
    nodes = [node for node in plan.get("nodes", []) if isinstance(node, dict)]
    blocked = {failed_node_id}
    changed = True
    while changed:
        changed = False
        for node in nodes:
            if node.get("status") != "pending":
                continue
            if any(dep_id in blocked for dep_id in _string_list(node.get("depends_on"))):
                node["status"] = "blocked"
                node["blocked_by"] = sorted(dep_id for dep_id in _string_list(node.get("depends_on")) if dep_id in blocked)
                blocked.add(str(node.get("id")))
                changed = True


def plan_complete(plan: Optional[dict[str, Any]]) -> bool:
    if not isinstance(plan, dict):
        return False
    nodes = [node for node in plan.get("nodes", []) if isinstance(node, dict)]
    return bool(nodes) and all(node.get("status") == "completed" for node in nodes)


def plan_runtime_status(plan: Optional[dict[str, Any]]) -> str:
    """Compact persisted status for the whole routed plan."""
    if not isinstance(plan, dict):
        return "missing"
    nodes = [node for node in plan.get("nodes", []) if isinstance(node, dict)]
    if not nodes:
        return "missing"
    if all(node.get("status") == "completed" for node in nodes):
        return "completed"
    if any(node.get("status") == "failed" for node in nodes):
        return "failed"
    if any(node.get("status") == "running" for node in nodes):
        return "running"
    if any(node.get("status") == "blocked" for node in nodes):
        return "blocked"
    if any(node.get("status") == "completed" for node in nodes):
        return "partial"
    return "submitted"


def clone_plan(plan: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not isinstance(plan, dict):
        return None
    return copy.deepcopy(plan)


def recover_running_nodes(plan: Optional[dict[str, Any]]) -> tuple[Optional[dict[str, Any]], list[str]]:
    """Return a plan clone with interrupted running nodes moved back to pending."""
    cloned = clone_plan(plan)
    if not isinstance(cloned, dict):
        return None, []
    warnings: list[str] = []
    for node in cloned.get("nodes", []):
        if not isinstance(node, dict) or node.get("status") != "running":
            continue
        node_id = str(node.get("id") or "")
        node["status"] = "pending"
        node.pop("lease", None)
        node.pop("lease_id", None)
        node.pop("lease_started_at", None)
        recovery = "recovered_after_interrupted_run"
        existing = node.get("recovery_warnings") if isinstance(node.get("recovery_warnings"), list) else []
        if recovery not in existing:
            node["recovery_warnings"] = [*existing, recovery]
        warnings.append(f"node {node_id}: {recovery}" if node_id else recovery)
    if warnings:
        cloned["updated_at"] = time.time()
        existing = cloned.get("recovery_warnings") if isinstance(cloned.get("recovery_warnings"), list) else []
        for warning in warnings:
            if warning not in existing:
                existing.append(warning)
        cloned["recovery_warnings"] = existing
    return cloned, warnings


def compact_node_result(node: dict[str, Any]) -> dict[str, Any]:
    result = node.get("result") if isinstance(node.get("result"), dict) else {}
    output_path = result.get("output_path") or ""
    failure_kind = result.get("failure_kind") or ""
    warnings = list(result.get("warnings", [])) if isinstance(result, dict) else []
    for warning in node.get("recovery_warnings", []) if isinstance(node.get("recovery_warnings"), list) else []:
        text = str(warning or "").strip()
        if text and text not in warnings:
            warnings.append(text)
    return {
        "id": node.get("id", ""),
        "status": node.get("status", "pending"),
        "tier": node.get("tier", ""),
        "path": node.get("path", ""),
        "model": node.get("model", ""),
        "executors_attempted": list(result.get("executors_attempted", [])) if isinstance(result, dict) else [],
        "summary": str(result.get("summary", "") if isinstance(result, dict) else ""),
        "verification": str(result.get("verification", "") if isinstance(result, dict) else ""),
        "warnings": warnings,
        "output_excerpt": str(result.get("output_excerpt", "") if isinstance(result, dict) else ""),
        "output_path": str(output_path),
        "failure_kind": str(failure_kind),
    }


def public_plan_state(plan: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not isinstance(plan, dict):
        return None
    nodes = [node for node in plan.get("nodes", []) if isinstance(node, dict)]
    next_node = next_runnable_node(plan)
    return {
        "plan_id": plan.get("plan_id", ""),
        "summary": plan.get("summary", ""),
        "workdir": plan.get("workdir", ""),
        "status": plan_runtime_status(plan),
        "complete": plan_complete(plan),
        "next_node": compact_node_result(next_node) if isinstance(next_node, dict) else None,
        "recovery_warnings": list(plan.get("recovery_warnings", [])) if isinstance(plan.get("recovery_warnings"), list) else [],
        "nodes": [compact_node_result(node) for node in nodes],
    }


def dependency_summaries(plan: dict[str, Any], node: dict[str, Any]) -> list[dict[str, str]]:
    by_id = {str(item.get("id")): item for item in plan.get("nodes", []) if isinstance(item, dict)}
    summaries: list[dict[str, str]] = []
    for dep_id in _string_list(node.get("depends_on")):
        dep = by_id.get(dep_id)
        if not dep:
            continue
        result = dep.get("result") if isinstance(dep.get("result"), dict) else {}
        summaries.append(
            {
                "id": dep_id,
                "status": str(dep.get("status", "")),
                "summary": str(result.get("summary", "") if isinstance(result, dict) else ""),
                "verification": str(result.get("verification", "") if isinstance(result, dict) else ""),
            }
        )
    return summaries
