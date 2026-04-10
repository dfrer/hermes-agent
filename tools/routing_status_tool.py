#!/usr/bin/env python3
"""Read-only routing guard status snapshot tool."""

from __future__ import annotations

from agent.routing_guard import (
    get_routing_status_snapshot,
    hydrate_routed_plan_from_persistence,
)
from tools.registry import registry, tool_result


def check_routing_status_requirements() -> bool:
    return True


ROUTING_STATUS_SCHEMA = {
    "name": "routing_status",
    "description": (
        "Inspect the active routing-layer guard state for the current task. "
        "Use this when routing blocked a tool call and you need to see the current route lock, "
        "git permissions, verification history, or recovered routed-plan summary."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "plan_id": {
                "type": "string",
                "description": "Optional persisted routed plan id to recover before reading status.",
            },
        },
        "required": [],
    },
}


def routing_status_tool(*, task_id: str = "", session_id: str = "", plan_id: str = "") -> str:
    hydrated = False
    if task_id:
        hydrated = hydrate_routed_plan_from_persistence(
            task_id,
            session_id=session_id,
            plan_id=plan_id,
        )
    status = get_routing_status_snapshot(task_id)
    status["requested_session_id"] = str(session_id or "")
    status["requested_plan_id"] = str(plan_id or "")
    status["hydrated_from_persistence"] = bool(hydrated)
    return tool_result({"success": True, "status": status})


def _handle_routing_status(args, **kw):
    return routing_status_tool(
        task_id=kw.get("task_id", ""),
        session_id=kw.get("session_id", ""),
        plan_id=args.get("plan_id", ""),
    )


registry.register(
    name="routing_status",
    toolset="routing",
    schema=ROUTING_STATUS_SCHEMA,
    handler=_handle_routing_status,
    check_fn=check_routing_status_requirements,
    emoji="RS",
)
