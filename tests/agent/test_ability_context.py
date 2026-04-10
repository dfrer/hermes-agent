from __future__ import annotations

import json
import time

from agent.ability_context import (
    build_cache_key,
    detect_ability_requirements,
    make_ability_packet,
    preflight_missing_lanes,
)
from agent.routing_guard import (
    activate_for_task,
    deactivate_for_task,
    final_response_block_reason,
    get_cached_ability_packet,
    get_ability_handoff,
    get_ability_packets,
    pre_tool_call_block_reason,
    record_ability_packet,
    record_routing_decision,
    record_tool_result,
)


def test_detects_each_ability_lane_from_task_text():
    text = (
        "UI canvas looks wrong, CPU is high, localhost dev server is duplicated, "
        "where is this behavior coming from, check latest API docs, auth token security, "
        "summarize this JSON log, and inspect the MP4 video."
    )

    requirements = detect_ability_requirements(text)

    lanes = requirements["lanes"]
    for lane in (
        "visual",
        "runtime",
        "environment",
        "repo_archaeology",
        "external_docs",
        "security",
        "data_logs",
        "audio_video",
    ):
        assert lanes[lane]["required"] is True
    assert requirements["post_visual_required"] is True


def test_plain_browser_avoidance_does_not_require_visual_lane():
    requirements = detect_ability_requirements(
        "Update hello.py and do not open a browser during this plain file edit."
    )

    assert "visual" not in requirements["lanes"]
    assert requirements["post_visual_required"] is False


def test_ability_cache_hit_and_stale_behavior():
    task_id = "ability-cache-hit"
    activate_for_task(task_id, session_id="s-cache", skills=["routing-layer"])
    try:
        cache_key = build_cache_key(lane="visual", url="http://127.0.0.1:3000", task="canvas", phase="pre")
        packet = make_ability_packet(
            task_id=task_id,
            lanes=["visual"],
            phase="pre",
            summary="Screenshot looks correct.",
            cache_key=cache_key,
            generated_at=time.time(),
        )
        record_ability_packet(task_id, packet)

        cached = get_cached_ability_packet(task_id, cache_key, ttl_seconds=60)
        assert cached is not None
        assert cached["cached"] is True

        record_tool_result(
            task_id,
            "write_file",
            {"path": "/tmp/demo.py", "content": "print('x')"},
            {"success": True},
        )

        assert get_cached_ability_packet(task_id, cache_key, ttl_seconds=60) is None
        assert get_ability_packets(task_id, include_stale=True)[0]["stale"] is True
    finally:
        deactivate_for_task(task_id)


def test_routing_guard_blocks_routed_exec_until_required_preflight_packet():
    task_id = "ability-guard-preflight"
    activate_for_task(
        task_id,
        session_id="s-ability",
        skills=["routing-layer"],
        user_message="Fix the WebGL canvas visual bug.",
    )
    record_routing_decision(
        task_id,
        "TIER: 3C | PATH: quick-edit | MODEL: Hermes CLI (MiniMax-M2.7 via minimax) | REASON: simple visual fix | CONFIDENCE: high",
        session_id="s-ability",
    )
    try:
        reason = pre_tool_call_block_reason("routed_exec", {"task": "fix", "workdir": "/tmp"}, task_id)
        assert reason is not None
        assert "visual" in reason
        assert "ability_context" in reason

        unavailable = make_ability_packet(
            task_id=task_id,
            lanes=["visual"],
            phase="pre",
            status="unavailable",
            summary="Visual verification unavailable.",
            constraints=["browser backend is unavailable"],
        )
        record_ability_packet(task_id, unavailable)

        assert pre_tool_call_block_reason("routed_exec", {"task": "fix", "workdir": "/tmp"}, task_id) is None
    finally:
        deactivate_for_task(task_id)


def test_routed_success_requires_post_visual_verification_before_final_fixed_claim():
    task_id = "ability-final-visual-gate"
    activate_for_task(
        task_id,
        session_id="s-final",
        skills=["routing-layer"],
        user_message="Fix the canvas layout; it looks wrong.",
    )
    record_routing_decision(
        task_id,
        "TIER: 3C | PATH: quick-edit | MODEL: Hermes CLI (MiniMax-M2.7 via minimax) | REASON: simple visual fix | CONFIDENCE: high",
        session_id="s-final",
    )
    record_ability_packet(
        task_id,
        make_ability_packet(task_id=task_id, lanes=["visual"], phase="pre", summary="Broken layout observed."),
    )
    try:
        record_tool_result(
            task_id,
            "routed_exec",
            {"task": "fix", "workdir": "/tmp"},
            {
                "success": True,
                "attempts": [{"kind": "hermes_minimax_m27", "status": "success", "exit_code": 0}],
            },
        )

        reason = final_response_block_reason(task_id, "Fixed.")
        assert reason is not None
        assert "post-fix visual verification" in reason

        post_packet = make_ability_packet(
            task_id=task_id,
            lanes=["visual"],
            phase="post",
            summary="Screenshot confirms layout is fixed.",
        )
        record_ability_packet(task_id, post_packet)
        record_tool_result(
            task_id,
            "terminal",
            {"command": "pytest tests/ui/test_layout.py -q"},
            json.dumps({"status": "success", "output": "1 passed", "exit_code": 0}),
        )
        assert final_response_block_reason(task_id, "Fixed.") is None
    finally:
        deactivate_for_task(task_id)


def test_final_gate_allows_explicit_visual_unavailable_blocker():
    task_id = "ability-final-unavailable"
    activate_for_task(
        task_id,
        session_id="s-final-unavailable",
        skills=["routing-layer"],
        user_message="Fix the UI screenshot bug.",
    )
    record_routing_decision(
        task_id,
        "TIER: 3C | PATH: quick-edit | MODEL: Hermes CLI (MiniMax-M2.7 via minimax) | REASON: simple visual fix | CONFIDENCE: high",
        session_id="s-final-unavailable",
    )
    record_ability_packet(
        task_id,
        make_ability_packet(task_id=task_id, lanes=["visual"], phase="pre", summary="Broken UI observed."),
    )
    try:
        record_tool_result(
            task_id,
            "routed_exec",
            {"task": "fix", "workdir": "/tmp"},
            {
                "success": True,
                "attempts": [{"kind": "hermes_minimax_m27", "status": "success", "exit_code": 0}],
            },
        )
        assert final_response_block_reason(
            task_id,
            "Fixed, but visual verification was unavailable because the browser backend was blocked.",
        ) is None
    finally:
        deactivate_for_task(task_id)


def test_handoff_contains_compact_ability_packet_without_full_artifacts():
    task_id = "ability-handoff"
    activate_for_task(task_id, session_id="s-handoff", skills=["routing-layer"])
    try:
        record_ability_packet(
            task_id,
            make_ability_packet(
                task_id=task_id,
                lanes=["visual"],
                phase="pre",
                summary="The button is clipped.",
                screenshot_path="/tmp/screen.png",
                console_errors=[{"error": "TypeError at app.js:10"}],
            ),
        )
        handoff = get_ability_handoff(task_id)
        assert "button is clipped" in handoff
        assert "/tmp/screen.png" in handoff
        assert "TypeError" in handoff
    finally:
        deactivate_for_task(task_id)


def test_preflight_missing_lanes_helper_accepts_success_packet():
    requirements = detect_ability_requirements("The responsive UI layout looks wrong.")
    packet = make_ability_packet(task_id="x", lanes=["visual"], phase="pre", summary="Collected screenshot.")

    assert preflight_missing_lanes(requirements, [packet]) == []
