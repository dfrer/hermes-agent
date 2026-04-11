"""Entitlement-aware routing and quota helpers.

This module applies spend-policy gates above the canonical routing matrix.
It intentionally stays conservative: locked paid spend classes fail closed
when quota state is unknown, and only routed tasks may grant task-scoped
approvals for downgrade or paid-spend exceptions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import time
from typing import Any, Iterable, Optional


_DEFAULT_ENTITLEMENTS: dict[str, Any] = {
    "enabled": True,
    "locked_spend_classes": ["openai", "anthropic"],
    "unknown_quota_policy": "fail_closed",
    "approval_scope": "task",
    "codex": {
        "quota_source": "local_logs",
        "codex_home": "",
        "max_snapshot_age_seconds": 180,
    },
    "downgrade_policy": {
        "3A/high-risk": {
            "action_when_primary_blocked": "ask_before_downgrade",
            "downgrade_order": ["hermes_glm_zai", "hermes_minimax_m27"],
            "action_when_only_locked_paid_routes_remain": "ask_before_paid_spend",
        }
    },
}


def _deep_merge_dict(base: dict[str, Any], override: Optional[dict[str, Any]]) -> dict[str, Any]:
    merged = json.loads(json.dumps(base))
    if not isinstance(override, dict):
        return merged
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_entitlements_config(config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    if config is None:
        try:
            from hermes_cli.config import read_raw_config

            config = read_raw_config()
        except Exception:
            config = {}
    entitlements = config.get("entitlements") if isinstance(config, dict) else None
    merged = _deep_merge_dict(_DEFAULT_ENTITLEMENTS, entitlements if isinstance(entitlements, dict) else {})
    locked = merged.get("locked_spend_classes")
    if not isinstance(locked, list):
        locked = _DEFAULT_ENTITLEMENTS["locked_spend_classes"]
    merged["locked_spend_classes"] = [str(item or "").strip().lower() for item in locked if str(item or "").strip()]
    codex = merged.get("codex") if isinstance(merged.get("codex"), dict) else {}
    try:
        codex["max_snapshot_age_seconds"] = int(codex.get("max_snapshot_age_seconds", 180))
    except (TypeError, ValueError):
        codex["max_snapshot_age_seconds"] = 180
    merged["codex"] = codex
    return merged


def _iso_to_timestamp(raw: str) -> float:
    text = str(raw or "").strip()
    if not text:
        return 0.0
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def _timestamp_to_local(ts: float) -> str:
    if not ts:
        return "unknown"
    return datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _fmt_age(seconds: float) -> str:
    if seconds is None or not math.isfinite(seconds):
        return "unknown"
    if seconds < 5:
        return "just now"
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        minutes, sec = divmod(int(seconds), 60)
        return f"{minutes}m {sec}s ago" if sec else f"{minutes}m ago"
    hours, remainder = divmod(int(seconds), 3600)
    minutes = remainder // 60
    return f"{hours}h {minutes}m ago" if minutes else f"{hours}h ago"


def _fmt_reset(ts: float, *, now: Optional[float] = None) -> str:
    if not ts:
        return "unknown"
    current = now if now is not None else time.time()
    if ts <= current:
        return "now"
    remaining = int(ts - current)
    if remaining < 60:
        return f"in {remaining}s"
    if remaining < 3600:
        minutes, sec = divmod(remaining, 60)
        return f"in {minutes}m {sec}s" if sec else f"in {minutes}m"
    hours, remainder = divmod(remaining, 3600)
    minutes = remainder // 60
    return f"in {hours}h {minutes}m" if minutes else f"in {hours}h"


def _detect_default_codex_home() -> Path:
    candidates: list[Path] = []
    env_home = os.getenv("CODEX_HOME", "").strip()
    if env_home:
        candidates.append(Path(env_home).expanduser())
    candidates.append(Path.home() / ".codex")
    userprofile = os.getenv("USERPROFILE", "").strip()
    if userprofile:
        candidates.append(Path(userprofile) / ".codex")
    home_drive = os.getenv("HOMEDRIVE", "").strip()
    home_path = os.getenv("HOMEPATH", "").strip()
    if home_drive and home_path:
        candidates.append(Path(f"{home_drive}{home_path}") / ".codex")
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.exists():
            return candidate
    return candidates[0] if candidates else Path.home() / ".codex"


def _iter_recent_codex_logs(codex_home: Path, limit: int = 20) -> Iterable[Path]:
    sessions_root = codex_home / "sessions"
    if not sessions_root.exists():
        return ()
    files = []
    try:
        for path in sessions_root.rglob("rollout-*.jsonl"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            files.append((mtime, path))
    except OSError:
        return ()
    files.sort(key=lambda item: item[0], reverse=True)
    return [path for _mtime, path in files[:limit]]


@dataclass(frozen=True)
class QuotaSnapshot:
    spend_class: str
    source: str
    status: str
    reason: str
    captured_at: float = 0.0
    age_seconds: float = float("inf")
    plan_type: str = ""
    provider: str = ""
    primary_used_percent: Optional[float] = None
    primary_window_minutes: Optional[int] = None
    primary_resets_at: float = 0.0
    secondary_used_percent: Optional[float] = None
    secondary_window_minutes: Optional[int] = None
    secondary_resets_at: float = 0.0
    credits_present: bool = False
    credits_locked: bool = False
    credit_balance: str = ""
    details: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return self.status == "available"

    @property
    def unknown(self) -> bool:
        return self.status == "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "spend_class": self.spend_class,
            "source": self.source,
            "status": self.status,
            "reason": self.reason,
            "captured_at": self.captured_at,
            "age_seconds": self.age_seconds,
            "plan_type": self.plan_type,
            "provider": self.provider,
            "primary": {
                "used_percent": self.primary_used_percent,
                "window_minutes": self.primary_window_minutes,
                "resets_at": self.primary_resets_at,
            },
            "secondary": {
                "used_percent": self.secondary_used_percent,
                "window_minutes": self.secondary_window_minutes,
                "resets_at": self.secondary_resets_at,
            },
            "credits_present": self.credits_present,
            "credits_locked": self.credits_locked,
            "credit_balance": self.credit_balance,
            "details": list(self.details),
        }


@dataclass(frozen=True)
class RouteEligibility:
    target: dict[str, Any]
    spend_class: str
    allowed: bool
    status: str
    reason: str
    requires_approval: bool = False
    approval_kind: str = ""
    quota: Optional[QuotaSnapshot] = None
    degraded_from: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": dict(self.target),
            "spend_class": self.spend_class,
            "allowed": self.allowed,
            "status": self.status,
            "reason": self.reason,
            "requires_approval": self.requires_approval,
            "approval_kind": self.approval_kind,
            "degraded_from": self.degraded_from,
            "quota": self.quota.to_dict() if self.quota else None,
        }


@dataclass(frozen=True)
class EffectiveRoutePlan:
    route_targets: tuple[dict[str, Any], ...]
    evaluations: tuple[RouteEligibility, ...]
    failure_reason: str = ""
    approval_required: bool = False
    approval_kind: str = ""
    approval_key: str = ""
    approval_description: str = ""
    degraded: bool = False
    degraded_from: str = ""

    def to_metadata(self) -> dict[str, Any]:
        return {
            "failure_reason": self.failure_reason or None,
            "approval_required": self.approval_required,
            "approval_kind": self.approval_kind or None,
            "approval_key": self.approval_key or None,
            "approval_description": self.approval_description or None,
            "degraded": self.degraded,
            "degraded_from": self.degraded_from or None,
            "effective_targets": [dict(item) for item in self.route_targets],
            "evaluations": [item.to_dict() for item in self.evaluations],
        }


def classify_spend_class(provider: str = "", base_url: str = "") -> str:
    normalized_provider = str(provider or "").strip().lower()
    normalized_base_url = str(base_url or "").strip().lower()
    if normalized_provider in {"openai-codex", "openai"}:
        return "openai"
    if normalized_provider == "anthropic":
        return "anthropic"
    if "api.openai.com" in normalized_base_url:
        return "openai"
    return normalized_provider or "unknown"


def _quota_unknown(spend_class: str, source: str, reason: str, *, details: Optional[list[str]] = None) -> QuotaSnapshot:
    return QuotaSnapshot(
        spend_class=spend_class,
        source=source,
        status="unknown",
        reason=reason,
        details=tuple(details or ()),
    )


def observe_codex_quota(config: Optional[dict[str, Any]] = None, *, now: Optional[float] = None) -> QuotaSnapshot:
    settings = load_entitlements_config(config)
    codex_cfg = settings.get("codex") if isinstance(settings.get("codex"), dict) else {}
    source = str(codex_cfg.get("quota_source") or "local_logs").strip().lower()
    current = now if now is not None else time.time()
    if source != "local_logs":
        return _quota_unknown("openai", source or "unknown", "quota_unknown", details=["Unsupported Codex quota source."])

    configured_home = str(codex_cfg.get("codex_home") or "").strip()
    codex_home = Path(configured_home).expanduser() if configured_home else _detect_default_codex_home()
    if not codex_home.exists():
        return _quota_unknown(
            "openai",
            "codex_local_logs",
            "quota_unknown",
            details=[f"Codex home not found at {codex_home}."],
        )

    latest_payload: Optional[dict[str, Any]] = None
    latest_ts = 0.0
    for path in _iter_recent_codex_logs(codex_home):
        try:
            with path.open("r", encoding="utf-8") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    event = payload.get("payload") if isinstance(payload, dict) else None
                    if not isinstance(event, dict):
                        continue
                    if event.get("type") != "token_count":
                        continue
                    rate_limits = event.get("rate_limits")
                    if not isinstance(rate_limits, dict):
                        continue
                    if str(rate_limits.get("limit_id") or "").strip().lower() != "codex":
                        continue
                    timestamp = _iso_to_timestamp(str(payload.get("timestamp") or ""))
                    if timestamp >= latest_ts:
                        latest_ts = timestamp
                        latest_payload = {"rate_limits": rate_limits, "path": str(path)}
        except OSError:
            continue

    if not latest_payload or latest_ts <= 0:
        return _quota_unknown(
            "openai",
            "codex_local_logs",
            "quota_unknown",
            details=["No Codex token_count rate-limit snapshots were found."],
        )

    age_seconds = max(0.0, current - latest_ts)
    max_age = int(codex_cfg.get("max_snapshot_age_seconds", 180) or 180)
    rate_limits = latest_payload.get("rate_limits") if isinstance(latest_payload, dict) else {}
    primary = rate_limits.get("primary") if isinstance(rate_limits, dict) else {}
    secondary = rate_limits.get("secondary") if isinstance(rate_limits, dict) else {}
    credits = rate_limits.get("credits") if isinstance(rate_limits, dict) else {}
    primary_pct = None if not isinstance(primary, dict) else float(primary.get("used_percent")) if primary.get("used_percent") is not None else None
    secondary_pct = None if not isinstance(secondary, dict) else float(secondary.get("used_percent")) if secondary.get("used_percent") is not None else None
    if age_seconds > max_age:
        return QuotaSnapshot(
            spend_class="openai",
            source="codex_local_logs",
            status="unknown",
            reason="quota_unknown",
            captured_at=latest_ts,
            age_seconds=age_seconds,
            plan_type=str(rate_limits.get("plan_type") or ""),
            provider="openai-codex",
            primary_used_percent=primary_pct,
            primary_window_minutes=int(primary.get("window_minutes") or 0) if isinstance(primary, dict) and primary.get("window_minutes") is not None else None,
            primary_resets_at=float(primary.get("resets_at") or 0.0) if isinstance(primary, dict) else 0.0,
            secondary_used_percent=secondary_pct,
            secondary_window_minutes=int(secondary.get("window_minutes") or 0) if isinstance(secondary, dict) and secondary.get("window_minutes") is not None else None,
            secondary_resets_at=float(secondary.get("resets_at") or 0.0) if isinstance(secondary, dict) else 0.0,
            credits_present=bool(isinstance(credits, dict) and (credits.get("has_credits") or credits.get("balance") not in (None, ""))),
            credits_locked=True,
            credit_balance=str(credits.get("balance") or "") if isinstance(credits, dict) else "",
            details=(f"Snapshot older than {max_age}s.",),
        )

    credits_present = bool(isinstance(credits, dict) and (credits.get("has_credits") or credits.get("balance") not in (None, "")))
    included_exhausted = bool((primary_pct is not None and primary_pct >= 100.0) or (secondary_pct is not None and secondary_pct >= 100.0))
    if included_exhausted and credits_present:
        status = "locked_paid_only"
        reason = "locked_paid_spend"
    elif included_exhausted:
        status = "exhausted"
        reason = "included_quota_exhausted"
    else:
        status = "available"
        reason = "allowed"

    return QuotaSnapshot(
        spend_class="openai",
        source="codex_local_logs",
        status=status,
        reason=reason,
        captured_at=latest_ts,
        age_seconds=age_seconds,
        plan_type=str(rate_limits.get("plan_type") or ""),
        provider="openai-codex",
        primary_used_percent=primary_pct,
        primary_window_minutes=int(primary.get("window_minutes") or 0) if isinstance(primary, dict) and primary.get("window_minutes") is not None else None,
        primary_resets_at=float(primary.get("resets_at") or 0.0) if isinstance(primary, dict) else 0.0,
        secondary_used_percent=secondary_pct,
        secondary_window_minutes=int(secondary.get("window_minutes") or 0) if isinstance(secondary, dict) and secondary.get("window_minutes") is not None else None,
        secondary_resets_at=float(secondary.get("resets_at") or 0.0) if isinstance(secondary, dict) else 0.0,
        credits_present=credits_present,
        credits_locked=credits_present,
        credit_balance=str(credits.get("balance") or "") if isinstance(credits, dict) else "",
        details=(f"Source: {latest_payload.get('path')}",),
    )


def get_quota_snapshot_for_target(
    provider: str = "",
    *,
    base_url: str = "",
    config: Optional[dict[str, Any]] = None,
    now: Optional[float] = None,
) -> QuotaSnapshot:
    spend_class = classify_spend_class(provider, base_url)
    settings = load_entitlements_config(config)
    locked = set(settings.get("locked_spend_classes", []))
    if spend_class == "openai":
        if str(provider or "").strip().lower() == "openai-codex":
            return observe_codex_quota(settings, now=now)
        if spend_class in locked and str(settings.get("unknown_quota_policy") or "").strip().lower() == "fail_closed":
            return _quota_unknown(
                spend_class,
                "provider_runtime",
                "quota_unknown",
                details=["No supported OpenAI quota observer is available for this route."],
            )
    if spend_class == "anthropic" and spend_class in locked:
        if str(settings.get("unknown_quota_policy") or "").strip().lower() == "fail_closed":
            return _quota_unknown(
                spend_class,
                "provider_runtime",
                "quota_unknown",
                details=["No supported Anthropic quota observer is available for this route."],
            )
    return QuotaSnapshot(
        spend_class=spend_class,
        source="provider_runtime",
        status="available",
        reason="allowed",
        captured_at=now if now is not None else time.time(),
        age_seconds=0.0,
        provider=str(provider or ""),
    )


def evaluate_route_target(
    target: dict[str, Any],
    *,
    config: Optional[dict[str, Any]] = None,
    base_url: str = "",
    now: Optional[float] = None,
) -> RouteEligibility:
    provider = str(target.get("provider") or "").strip()
    quota = get_quota_snapshot_for_target(provider, base_url=base_url, config=config, now=now)
    if quota.status == "available":
        return RouteEligibility(
            target=dict(target),
            spend_class=quota.spend_class,
            allowed=True,
            status="allowed",
            reason="allowed",
            quota=quota,
        )
    if quota.status == "locked_paid_only":
        return RouteEligibility(
            target=dict(target),
            spend_class=quota.spend_class,
            allowed=False,
            status="blocked",
            reason="locked_paid_spend",
            quota=quota,
        )
    return RouteEligibility(
        target=dict(target),
        spend_class=quota.spend_class,
        allowed=False,
        status="blocked",
        reason=quota.reason,
        quota=quota,
    )


def _route_key(decision: dict[str, Any]) -> str:
    tier = str(decision.get("tier") or "").strip().upper()
    path = str(decision.get("path") or "").strip().lower()
    return f"{tier}/{path}" if tier and path else ""


def _approval_key(task_id: str, suffix: str) -> str:
    safe_task = str(task_id or "").strip() or "task"
    return f"entitlement:{safe_task}:{suffix}"


def build_effective_route_plan(
    task_id: str,
    decision: dict[str, Any],
    route_targets: list[dict[str, Any]],
    *,
    config: Optional[dict[str, Any]] = None,
    has_task_approval=None,
    now: Optional[float] = None,
) -> EffectiveRoutePlan:
    settings = load_entitlements_config(config)
    if not settings.get("enabled", True):
        evaluations = tuple(
            RouteEligibility(
                target=dict(target),
                spend_class=classify_spend_class(str(target.get("provider") or "")),
                allowed=True,
                status="allowed",
                reason="allowed",
            )
            for target in route_targets
        )
        return EffectiveRoutePlan(tuple(dict(item) for item in route_targets), evaluations)

    evaluations = tuple(
        evaluate_route_target(target, config=settings, now=now)
        for target in route_targets
    )
    route_key = _route_key(decision)
    if route_key == "3A/high-risk" and evaluations:
        primary = evaluations[0]
        if primary.allowed:
            allowed_targets = [dict(item.target) for item in evaluations if item.allowed]
            return EffectiveRoutePlan(tuple(allowed_targets), evaluations)

        unlocked_downgrades = [item for item in evaluations[1:] if item.allowed]
        if unlocked_downgrades:
            approval_key = _approval_key(task_id, "downgrade:3a-high-risk")
            approved = bool(has_task_approval and has_task_approval(task_id, approval_key))
            ordered = _ordered_downgrades(unlocked_downgrades, settings, route_key)
            if not approved:
                return EffectiveRoutePlan(
                    route_targets=(),
                    evaluations=evaluations,
                    failure_reason="downgrade_approval_required",
                    approval_required=True,
                    approval_kind="downgrade",
                    approval_key=approval_key,
                    approval_description=(
                        "Approve downgrade for this task only: Codex is blocked, "
                        "but unpaid fallback capacity is available."
                    ),
                    degraded=True,
                    degraded_from=str(primary.target.get("label") or ""),
                )
            return EffectiveRoutePlan(
                route_targets=tuple(dict(item.target) for item in ordered),
                evaluations=evaluations,
                degraded=True,
                degraded_from=str(primary.target.get("label") or ""),
            )

        locked_paid = [item for item in evaluations if item.reason == "locked_paid_spend"]
        if locked_paid:
            approval_key = _approval_key(task_id, "paid-spend:3a-high-risk")
            approved = bool(has_task_approval and has_task_approval(task_id, approval_key))
            if not approved:
                return EffectiveRoutePlan(
                    route_targets=(),
                    evaluations=evaluations,
                    failure_reason="locked_paid_spend",
                    approval_required=True,
                    approval_kind="paid_spend",
                    approval_key=approval_key,
                    approval_description=(
                        "Approve locked paid spend for this task only: no unpaid "
                        "route remains for 3A/high-risk."
                    ),
                )
            return EffectiveRoutePlan(
                route_targets=tuple(dict(item.target) for item in locked_paid),
                evaluations=evaluations,
            )

        failure_reason = primary.reason or "included_quota_exhausted"
        return EffectiveRoutePlan(route_targets=(), evaluations=evaluations, failure_reason=failure_reason)

    allowed_targets = [dict(item.target) for item in evaluations if item.allowed]
    if allowed_targets:
        return EffectiveRoutePlan(tuple(allowed_targets), evaluations)

    failure_reason = ""
    for item in evaluations:
        if item.reason:
            failure_reason = item.reason
            break
    return EffectiveRoutePlan(route_targets=(), evaluations=evaluations, failure_reason=failure_reason or "locked_paid_spend")


def _ordered_downgrades(
    downgrades: list[RouteEligibility],
    config: dict[str, Any],
    route_key: str,
) -> list[RouteEligibility]:
    policy = config.get("downgrade_policy") if isinstance(config.get("downgrade_policy"), dict) else {}
    entry = policy.get(route_key) if isinstance(policy, dict) else {}
    order = entry.get("downgrade_order") if isinstance(entry, dict) else None
    if not isinstance(order, list) or not order:
        return downgrades
    rank = {str(kind): index for index, kind in enumerate(order)}
    return sorted(downgrades, key=lambda item: rank.get(str(item.target.get("kind") or ""), len(rank)))


def build_route_availability_summary(config: Optional[dict[str, Any]] = None, *, now: Optional[float] = None) -> list[dict[str, Any]]:
    from agent.routing_policy import load_routing_policy

    policy = load_routing_policy(config)
    rows: list[dict[str, Any]] = []
    for tier, paths in policy.routes.items():
        for path, profile in paths.items():
            targets = [profile.primary.to_dict(), *[item.to_dict() for item in profile.fallbacks]]
            effective = build_effective_route_plan(
                task_id="",
                decision={"tier": tier, "path": path, "model": profile.primary.label},
                route_targets=targets,
                config=config,
                now=now,
            )
            rows.append(
                {
                    "tier": tier,
                    "path": path,
                    "effective_targets": [item.get("label") for item in effective.route_targets],
                    "failure_reason": effective.failure_reason,
                    "approval_required": effective.approval_required,
                    "approval_kind": effective.approval_kind,
                    "degraded": effective.degraded,
                }
            )
    return rows


def format_quota_display(config: Optional[dict[str, Any]] = None, *, now: Optional[float] = None) -> str:
    settings = load_entitlements_config(config)
    snapshot = observe_codex_quota(settings, now=now)
    current = now if now is not None else time.time()
    lines = [
        "Entitlement Quota Status",
        "",
        f"Locked spend classes: {', '.join(settings.get('locked_spend_classes', [])) or 'none'}",
        f"Unknown quota policy: {settings.get('unknown_quota_policy')}",
        "",
        "Codex local quota:",
        f"  Status: {snapshot.status}",
        f"  Reason: {snapshot.reason}",
        f"  Snapshot age: {_fmt_age(snapshot.age_seconds)}",
        f"  Captured at: {_timestamp_to_local(snapshot.captured_at)}",
    ]
    if snapshot.primary_window_minutes:
        lines.append(
            f"  5-hour window: {snapshot.primary_used_percent:.0f}% used "
            f"(resets {_fmt_reset(snapshot.primary_resets_at, now=current)})"
        )
    if snapshot.secondary_window_minutes:
        lines.append(
            f"  Weekly window: {snapshot.secondary_used_percent:.0f}% used "
            f"(resets {_fmt_reset(snapshot.secondary_resets_at, now=current)})"
        )
    if snapshot.plan_type:
        lines.append(f"  Plan: {snapshot.plan_type}")
    if snapshot.credits_present:
        balance = f" ({snapshot.credit_balance})" if snapshot.credit_balance else ""
        suffix = "locked" if snapshot.credits_locked else "available"
        lines.append(f"  Credits: present{balance}, {suffix}")
    else:
        lines.append("  Credits: none reported")
    if snapshot.details:
        for detail in snapshot.details:
            lines.append(f"  Note: {detail}")

    route_rows = build_route_availability_summary(settings, now=now)
    if route_rows:
        lines.extend(["", "Effective route availability:"])
        for row in route_rows:
            route_label = f"{row['tier']}/{row['path']}"
            targets = row.get("effective_targets") or []
            if targets:
                lines.append(f"  {route_label}: {', '.join(targets)}")
            elif row.get("approval_required"):
                lines.append(
                    f"  {route_label}: approval required "
                    f"({row.get('approval_kind') or 'route'})"
                )
            else:
                lines.append(f"  {route_label}: unavailable ({row.get('failure_reason') or 'unknown'})")
    return "\n".join(lines)
