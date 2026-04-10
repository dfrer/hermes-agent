"""Canonical routing policy for Hermes coding-task execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


ROUTING_POLICY_VERSION = "3.0.0"


@dataclass(frozen=True)
class RouteTarget:
    kind: str
    label: str
    executor: str
    model: str
    provider: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "label": self.label,
            "executor": self.executor,
            "model": self.model,
            "provider": self.provider,
        }


@dataclass(frozen=True)
class RouteProfile:
    tier: str
    path: str
    primary: RouteTarget
    fallbacks: tuple[RouteTarget, ...] = ()
    default_timeout: int = 300

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "path": self.path,
            "primary": self.primary.to_dict(),
            "fallbacks": [target.to_dict() for target in self.fallbacks],
            "default_timeout": self.default_timeout,
        }


@dataclass(frozen=True)
class RoutingPolicy:
    version: str
    routes: dict[str, dict[str, RouteProfile]]
    errors: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors

    def profile(self, tier: str, path: str) -> Optional[RouteProfile]:
        return self.routes.get((tier or "").upper(), {}).get(normalize_route_path(path))


KNOWN_TARGETS: dict[str, RouteTarget] = {
    "codex_gpt54": RouteTarget(
        kind="codex_gpt54",
        label="Codex CLI (gpt-5.4)",
        executor="codex",
        model="gpt-5.4",
    ),
    "codex_gpt54mini": RouteTarget(
        kind="codex_gpt54mini",
        label="Codex CLI (gpt-5.4-mini)",
        executor="codex",
        model="gpt-5.4-mini",
    ),
    "hermes_glm_zai": RouteTarget(
        kind="hermes_glm_zai",
        label="Hermes CLI (glm-5.1 via zai)",
        executor="hermes",
        model="glm-5.1",
        provider="zai",
    ),
    "hermes_minimax_m27": RouteTarget(
        kind="hermes_minimax_m27",
        label="Hermes CLI (MiniMax-M2.7 via minimax)",
        executor="hermes",
        model="MiniMax-M2.7",
        provider="minimax",
    ),
    "hermes_nous_mimo_v2_pro": RouteTarget(
        kind="hermes_nous_mimo_v2_pro",
        label="Hermes CLI (xiaomi/mimo-v2-pro via nous)",
        executor="hermes",
        model="xiaomi/mimo-v2-pro",
        provider="nous",
    ),
}


DEFAULT_ROUTE_SPECS: dict[str, dict[str, dict[str, Any]]] = {
    "3A": {
        "high-risk": {
            "primary": "codex_gpt54",
            "fallbacks": [],
            "default_timeout": 1200,
        }
    },
    "3B": {
        "marathon": {
            "primary": "hermes_glm_zai",
            "fallbacks": ["codex_gpt54mini"],
            "default_timeout": 900,
        },
        "long-context": {
            "primary": "hermes_nous_mimo_v2_pro",
            "fallbacks": ["codex_gpt54mini"],
            "default_timeout": 900,
        },
    },
    "3C": {
        "quick-edit": {
            "primary": "hermes_minimax_m27",
            "fallbacks": ["codex_gpt54mini"],
            "default_timeout": 300,
        }
    },
}


DEFAULT_ROUTE_PATHS = {
    "3A": "high-risk",
    "3B": "marathon",
    "3C": "quick-edit",
}


def normalize_route_model(model: str) -> str:
    return " ".join((model or "").strip().lower().split())


def normalize_route_path(path: str) -> str:
    return "-".join((path or "").strip().lower().split())


def _coerce_timeout(value: Any, default: int, errors: list[str], label: str) -> int:
    if value is None:
        return default
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        errors.append(f"{label}.default_timeout must be an integer")
        return default
    if timeout < 1:
        errors.append(f"{label}.default_timeout must be positive")
        return default
    return timeout


def _coerce_target(value: Any, errors: list[str], label: str) -> Optional[RouteTarget]:
    if isinstance(value, str):
        kind = value.strip()
        target = KNOWN_TARGETS.get(kind)
        if target is None:
            errors.append(f"{label} references unknown routed executor kind '{kind}'")
        return target

    if not isinstance(value, dict):
        errors.append(f"{label} must be a routed executor kind or mapping")
        return None

    kind = str(value.get("kind") or "").strip()
    target = KNOWN_TARGETS.get(kind)
    if target is None:
        errors.append(f"{label}.kind references unknown routed executor kind '{kind}'")
        return None

    label_override = str(value.get("label") or "").strip()
    if label_override and normalize_route_model(label_override) != normalize_route_model(target.label):
        errors.append(
            f"{label}.label must match the canonical label for '{kind}' "
            f"({target.label!r})"
        )
    return target


def _build_profile(tier: str, path: str, raw: Any, errors: list[str]) -> Optional[RouteProfile]:
    label = f"routing.routes.{tier}.{path}"
    if not isinstance(raw, dict):
        errors.append(f"{label} must be a mapping")
        return None

    primary = _coerce_target(raw.get("primary"), errors, f"{label}.primary")
    if primary is None:
        return None

    raw_fallbacks = raw.get("fallbacks", [])
    if raw_fallbacks is None:
        raw_fallbacks = []
    if not isinstance(raw_fallbacks, list):
        errors.append(f"{label}.fallbacks must be a list")
        return None

    fallbacks: list[RouteTarget] = []
    for index, item in enumerate(raw_fallbacks):
        fallback = _coerce_target(item, errors, f"{label}.fallbacks[{index}]")
        if fallback is not None:
            fallbacks.append(fallback)

    default_timeout = _coerce_timeout(
        raw.get("default_timeout"),
        int(DEFAULT_ROUTE_SPECS.get(tier, {}).get(path, {}).get("default_timeout", 300)),
        errors,
        label,
    )
    return RouteProfile(
        tier=tier,
        path=normalize_route_path(path),
        primary=primary,
        fallbacks=tuple(fallbacks),
        default_timeout=default_timeout,
    )


def _build_routes(specs: dict[str, dict[str, dict[str, Any]]], errors: list[str]) -> dict[str, dict[str, RouteProfile]]:
    routes: dict[str, dict[str, RouteProfile]] = {}
    for raw_tier, paths in specs.items():
        tier = str(raw_tier or "").strip().upper()
        if tier not in {"3A", "3B", "3C"}:
            errors.append(f"routing.routes contains unsupported tier '{raw_tier}'")
            continue
        if not isinstance(paths, dict) or not paths:
            errors.append(f"routing.routes.{tier} must define at least one route path")
            continue
        tier_routes: dict[str, RouteProfile] = {}
        for raw_path, raw_profile in paths.items():
            path = normalize_route_path(str(raw_path or ""))
            if not path:
                errors.append(f"routing.routes.{tier} contains an empty route path")
                continue
            profile = _build_profile(tier, path, raw_profile, errors)
            if profile is not None:
                tier_routes[path] = profile
        if tier_routes:
            routes[tier] = tier_routes
    return routes


def _default_policy(errors: tuple[str, ...] = ()) -> RoutingPolicy:
    build_errors: list[str] = []
    routes = _build_routes(DEFAULT_ROUTE_SPECS, build_errors)
    return RoutingPolicy(
        version=ROUTING_POLICY_VERSION,
        routes=routes,
        errors=tuple([*errors, *build_errors]),
    )


def load_routing_policy(config: Optional[dict[str, Any]] = None) -> RoutingPolicy:
    """Load routing policy, applying validated config overrides when present."""
    if config is None:
        try:
            from hermes_cli.config import read_raw_config

            config = read_raw_config()
        except Exception:
            config = {}

    routing = config.get("routing") if isinstance(config, dict) else None
    if not isinstance(routing, dict):
        return _default_policy()

    raw_routes = routing.get("routes")
    if raw_routes in (None, {}):
        return _default_policy()
    if not isinstance(raw_routes, dict):
        return _default_policy(("routing.routes must be a mapping; using default routing policy",))

    errors: list[str] = []
    routes = _build_routes(raw_routes, errors)
    missing_tiers = [tier for tier in DEFAULT_ROUTE_PATHS if tier not in routes]
    for tier in missing_tiers:
        errors.append(f"routing.routes must include tier {tier}")

    for tier, default_path in DEFAULT_ROUTE_PATHS.items():
        if tier in routes and default_path not in routes[tier]:
            errors.append(f"routing.routes.{tier} must include default path '{default_path}'")

    if errors:
        return _default_policy(tuple(errors))

    return RoutingPolicy(
        version=str(routing.get("policy_version") or ROUTING_POLICY_VERSION),
        routes=routes,
        errors=(),
    )


def get_route_matrix(config: Optional[dict[str, Any]] = None) -> dict[str, dict[str, dict[str, Any]]]:
    policy = load_routing_policy(config)
    return {
        tier: {path: profile.to_dict() for path, profile in paths.items()}
        for tier, paths in policy.routes.items()
    }


def get_primary_model_path_by_tier(config: Optional[dict[str, Any]] = None) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for tier, paths in load_routing_policy(config).routes.items():
        result[tier] = {
            normalize_route_model(profile.primary.label): path
            for path, profile in paths.items()
        }
    return result


def get_allowed_route_models(config: Optional[dict[str, Any]] = None) -> dict[str, tuple[str, ...]]:
    result: dict[str, tuple[str, ...]] = {}
    for tier, paths in load_routing_policy(config).routes.items():
        labels: list[str] = []
        for profile in paths.values():
            for target in (profile.primary, *profile.fallbacks):
                if target.label not in labels:
                    labels.append(target.label)
        result[tier] = tuple(labels)
    return result


def resolve_primary_turn_route(primary: dict[str, Any]) -> dict[str, Any]:
    """Return the unmodified runtime route for one turn.

    This replaces the removed cheap-vs-strong smart routing path while keeping
    the existing caller contract stable.
    """
    return {
        "model": primary.get("model"),
        "runtime": {
            "api_key": primary.get("api_key"),
            "base_url": primary.get("base_url"),
            "provider": primary.get("provider"),
            "api_mode": primary.get("api_mode"),
            "command": primary.get("command"),
            "args": list(primary.get("args") or []),
            "credential_pool": primary.get("credential_pool"),
        },
        "label": None,
        "signature": (
            primary.get("model"),
            primary.get("provider"),
            primary.get("base_url"),
            primary.get("api_mode"),
            primary.get("command"),
            tuple(primary.get("args") or ()),
        ),
    }
