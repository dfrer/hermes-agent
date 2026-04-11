from __future__ import annotations

import json
from pathlib import Path
import time

from agent.entitlements import (
    QuotaSnapshot,
    build_effective_route_plan,
    observe_codex_quota,
)


def _write_rollout(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")


def _token_count_event(
    timestamp: float,
    *,
    primary_pct: float,
    secondary_pct: float,
    credits: dict | None = None,
) -> dict:
    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(timestamp)),
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "rate_limits": {
                "limit_id": "codex",
                "primary": {
                    "used_percent": primary_pct,
                    "window_minutes": 300,
                    "resets_at": int(timestamp + 1200),
                },
                "secondary": {
                    "used_percent": secondary_pct,
                    "window_minutes": 10080,
                    "resets_at": int(timestamp + 86400),
                },
                "credits": credits,
                "plan_type": "plus",
            },
        },
    }


def _quota_config(tmp_path: Path, *, max_age: int = 180) -> dict:
    return {
        "entitlements": {
            "codex": {
                "quota_source": "local_logs",
                "codex_home": str(tmp_path / ".codex"),
                "max_snapshot_age_seconds": max_age,
            }
        }
    }


def test_observe_codex_quota_reads_latest_fresh_snapshot(tmp_path):
    now = time.time()
    rollout = tmp_path / ".codex" / "sessions" / "2026" / "04" / "10" / "rollout-a.jsonl"
    _write_rollout(
        rollout,
        [
            {"timestamp": "2026-04-10T00:00:00.000Z", "type": "event_msg", "payload": {"type": "noop"}},
            _token_count_event(now - 30, primary_pct=42.0, secondary_pct=17.0),
        ],
    )

    snapshot = observe_codex_quota(_quota_config(tmp_path), now=now)

    assert snapshot.status == "available"
    assert snapshot.reason == "allowed"
    assert snapshot.primary_used_percent == 42.0
    assert snapshot.secondary_used_percent == 17.0
    assert snapshot.plan_type == "plus"


def test_observe_codex_quota_marks_stale_snapshot_unknown(tmp_path):
    now = time.time()
    rollout = tmp_path / ".codex" / "sessions" / "2026" / "04" / "10" / "rollout-b.jsonl"
    _write_rollout(
        rollout,
        [_token_count_event(now - 600, primary_pct=10.0, secondary_pct=5.0)],
    )

    snapshot = observe_codex_quota(_quota_config(tmp_path, max_age=180), now=now)

    assert snapshot.status == "unknown"
    assert snapshot.reason == "quota_unknown"


def test_observe_codex_quota_marks_locked_paid_only_when_plan_is_exhausted(tmp_path):
    now = time.time()
    rollout = tmp_path / ".codex" / "sessions" / "2026" / "04" / "10" / "rollout-c.jsonl"
    _write_rollout(
        rollout,
        [
            _token_count_event(
                now - 10,
                primary_pct=100.0,
                secondary_pct=16.0,
                credits={"has_credits": True, "unlimited": False, "balance": "50.0000"},
            )
        ],
    )

    snapshot = observe_codex_quota(_quota_config(tmp_path), now=now)

    assert snapshot.status == "locked_paid_only"
    assert snapshot.reason == "locked_paid_spend"
    assert snapshot.credits_present is True
    assert snapshot.credits_locked is True
    assert snapshot.credit_balance == "50.0000"


def test_observe_codex_quota_handles_malformed_or_missing_events(tmp_path):
    rollout = tmp_path / ".codex" / "sessions" / "2026" / "04" / "10" / "rollout-d.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_text("not-json\n{}\n", encoding="utf-8")

    snapshot = observe_codex_quota(_quota_config(tmp_path), now=time.time())

    assert snapshot.status == "unknown"
    assert snapshot.reason == "quota_unknown"


def test_build_effective_route_plan_requires_3a_downgrade_approval():
    route_targets = [
        {"kind": "codex_gpt54", "label": "Codex CLI (gpt-5.4)", "executor": "codex", "model": "gpt-5.4", "provider": "openai-codex"},
        {"kind": "hermes_glm_zai", "label": "Hermes CLI (glm-5.1 via zai)", "executor": "hermes", "model": "glm-5.1", "provider": "zai"},
        {"kind": "hermes_minimax_m27", "label": "Hermes CLI (MiniMax-M2.7 via minimax)", "executor": "hermes", "model": "MiniMax-M2.7", "provider": "minimax"},
    ]
    blocked_codex = QuotaSnapshot(
        spend_class="openai",
        source="test",
        status="locked_paid_only",
        reason="locked_paid_spend",
        captured_at=time.time(),
        age_seconds=0.0,
        provider="openai-codex",
        credits_present=True,
        credits_locked=True,
    )

    def _has_approval(_task_id: str, _approval_key: str) -> bool:
        return False

    from unittest.mock import patch

    with patch("agent.entitlements.observe_codex_quota", return_value=blocked_codex):
        effective = build_effective_route_plan(
            "task-3a",
            {"tier": "3A", "path": "high-risk", "model": "Codex CLI (gpt-5.4)"},
            route_targets,
            has_task_approval=_has_approval,
        )

    assert effective.approval_required is True
    assert effective.approval_kind == "downgrade"
    assert effective.failure_reason == "downgrade_approval_required"
    assert effective.route_targets == ()


def test_build_effective_route_plan_uses_glm_then_minimax_after_approval():
    route_targets = [
        {"kind": "codex_gpt54", "label": "Codex CLI (gpt-5.4)", "executor": "codex", "model": "gpt-5.4", "provider": "openai-codex"},
        {"kind": "hermes_minimax_m27", "label": "Hermes CLI (MiniMax-M2.7 via minimax)", "executor": "hermes", "model": "MiniMax-M2.7", "provider": "minimax"},
        {"kind": "hermes_glm_zai", "label": "Hermes CLI (glm-5.1 via zai)", "executor": "hermes", "model": "glm-5.1", "provider": "zai"},
    ]
    blocked_codex = QuotaSnapshot(
        spend_class="openai",
        source="test",
        status="locked_paid_only",
        reason="locked_paid_spend",
        captured_at=time.time(),
        age_seconds=0.0,
        provider="openai-codex",
        credits_present=True,
        credits_locked=True,
    )

    def _has_approval(_task_id: str, approval_key: str) -> bool:
        return approval_key.endswith("downgrade:3a-high-risk")

    from unittest.mock import patch

    with patch("agent.entitlements.observe_codex_quota", return_value=blocked_codex):
        effective = build_effective_route_plan(
            "task-3a",
            {"tier": "3A", "path": "high-risk", "model": "Codex CLI (gpt-5.4)"},
            route_targets,
            has_task_approval=_has_approval,
        )

    assert [item["kind"] for item in effective.route_targets] == [
        "hermes_glm_zai",
        "hermes_minimax_m27",
    ]
    assert effective.degraded is True
    assert effective.degraded_from == "Codex CLI (gpt-5.4)"


def test_build_effective_route_plan_requires_paid_approval_when_only_locked_route_remains():
    route_targets = [
        {"kind": "codex_gpt54", "label": "Codex CLI (gpt-5.4)", "executor": "codex", "model": "gpt-5.4", "provider": "openai-codex"},
    ]
    blocked_codex = QuotaSnapshot(
        spend_class="openai",
        source="test",
        status="locked_paid_only",
        reason="locked_paid_spend",
        captured_at=time.time(),
        age_seconds=0.0,
        provider="openai-codex",
        credits_present=True,
        credits_locked=True,
    )

    from unittest.mock import patch

    with patch("agent.entitlements.observe_codex_quota", return_value=blocked_codex):
        blocked = build_effective_route_plan(
            "task-3a",
            {"tier": "3A", "path": "high-risk", "model": "Codex CLI (gpt-5.4)"},
            route_targets,
            has_task_approval=lambda *_args: False,
        )
        approved = build_effective_route_plan(
            "task-3a",
            {"tier": "3A", "path": "high-risk", "model": "Codex CLI (gpt-5.4)"},
            route_targets,
            has_task_approval=lambda _task_id, approval_key: approval_key.endswith("paid-spend:3a-high-risk"),
        )

    assert blocked.approval_required is True
    assert blocked.approval_kind == "paid_spend"
    assert blocked.failure_reason == "locked_paid_spend"
    assert [item["kind"] for item in approved.route_targets] == ["codex_gpt54"]
