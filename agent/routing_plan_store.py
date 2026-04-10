"""SQLite persistence adapter for routed_plan resume state."""

from __future__ import annotations

import logging
from typing import Any, Optional

from agent.routing_plan import plan_runtime_status, recover_running_nodes
from hermes_constants import get_hermes_home
from hermes_state import SessionDB

logger = logging.getLogger(__name__)

_session_db: Optional[SessionDB] = None


def get_plan_store_db() -> SessionDB:
    global _session_db
    if _session_db is None:
        _session_db = SessionDB(get_hermes_home() / "state.db")
    return _session_db


def set_plan_store_db(db: Optional[SessionDB]) -> None:
    """Test hook for injecting an isolated SessionDB."""
    global _session_db
    _session_db = db


def save_plan_snapshot(
    *,
    task_id: str,
    session_id: str = "",
    plan: dict[str, Any],
    parent_decision: dict[str, Any],
    status: str = "",
    last_error: str = "",
) -> bool:
    if not isinstance(plan, dict):
        return False
    plan_id = str(plan.get("plan_id", "") or "").strip()
    if not plan_id:
        return False
    try:
        get_plan_store_db().save_routed_plan(
            plan_id=plan_id,
            session_id=session_id,
            task_id=task_id,
            status=str(status or plan_runtime_status(plan)),
            parent_decision=parent_decision,
            plan=plan,
            last_error=last_error,
        )
        return True
    except Exception:
        logger.debug("Failed to persist routed plan %s", plan_id, exc_info=True)
        return False


def load_plan_snapshot(
    *,
    plan_id: str = "",
    task_id: str = "",
    session_id: str = "",
    include_reset: bool = False,
) -> Optional[dict[str, Any]]:
    record: Optional[dict[str, Any]] = None
    db = get_plan_store_db()
    if str(plan_id or "").strip():
        record = db.get_routed_plan(str(plan_id or "").strip())
    if record is None and str(task_id or "").strip():
        record = db.get_active_routed_plan_for_task(str(task_id or "").strip())
    if record is None and str(session_id or "").strip():
        record = db.get_latest_routed_plan_for_session(str(session_id or "").strip())
    if record is None:
        return None
    if record.get("status") == "reset" and not include_reset:
        return None

    recovered, warnings = recover_running_nodes(record.get("plan"))
    if isinstance(recovered, dict):
        record["plan"] = recovered
    if warnings:
        record["status"] = plan_runtime_status(record["plan"])
        record["last_error"] = "; ".join(warnings)
        save_plan_snapshot(
            task_id=str(record.get("task_id") or task_id or ""),
            session_id=str(record.get("session_id") or session_id or ""),
            plan=record["plan"],
            parent_decision=record.get("parent_decision") if isinstance(record.get("parent_decision"), dict) else {},
            status=record["status"],
            last_error=record["last_error"],
        )
    return record


def mark_plan_reset(plan_id: str, *, last_error: str = "") -> bool:
    clean = str(plan_id or "").strip()
    if not clean:
        return False
    try:
        return bool(get_plan_store_db().mark_routed_plan_reset(clean, last_error=last_error))
    except Exception:
        logger.debug("Failed to mark routed plan %s reset", clean, exc_info=True)
        return False
