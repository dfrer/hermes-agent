"""Deprecated compatibility shim for legacy smart_model_routing imports."""

from __future__ import annotations

from typing import Any, Dict, Optional

from agent.routing_policy import resolve_primary_turn_route


def choose_cheap_model_route(user_message: str, routing_config: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Cheap-vs-strong runtime rerouting has been removed."""
    return None


def resolve_turn_route(user_message: str, routing_config: Optional[Dict[str, Any]], primary: Dict[str, Any]) -> Dict[str, Any]:
    """Return the primary route; retained only for external import compatibility."""
    return resolve_primary_turn_route(primary)
