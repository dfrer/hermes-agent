from __future__ import annotations

import json

from agent.routing_guard import (
    activate_for_task,
    deactivate_for_task,
    get_active_skill_hints,
    get_routed_execution_plan,
    get_routing_decision,
    get_session_lane_context,
    get_selected_route,
    get_task_class,
    get_verification_attempts,
    has_route_lock,
    pre_tool_call_block_reason,
    record_tool_result,
    record_routing_decision,
)


def _plan_kind_labels(task_id: str) -> list[dict[str, str]]:
    return [
        {"kind": item["kind"], "label": item["label"]}
        for item in get_routed_execution_plan(task_id)
    ]


def test_blocks_file_mutation_before_routing_decision():
    task_id = "task-routing-block"
    activate_for_task(task_id, session_id="session-1", skills=["routing-layer"])
    try:
        reason = pre_tool_call_block_reason("patch", {"path": "demo.py"}, task_id)
        assert reason is not None
        assert "routing decision line" in reason
    finally:
        deactivate_for_task(task_id)


def test_allows_non_code_markdown_write_before_routing_decision():
    task_id = "task-routing-doc-write"
    activate_for_task(task_id, session_id="session-doc-write", skills=["routing-layer"])
    try:
        assert (
            pre_tool_call_block_reason(
                "write_file",
                {"path": "/home/hunter/wiki/entities/societies-game.md", "content": "# Societies\n"},
                task_id,
            )
            is None
        )
    finally:
        deactivate_for_task(task_id)


def test_allows_non_code_markdown_patch_before_routing_decision():
    task_id = "task-routing-doc-patch"
    activate_for_task(task_id, session_id="session-doc-patch", skills=["routing-layer"])
    try:
        assert (
            pre_tool_call_block_reason(
                "patch",
                {
                    "mode": "replace",
                    "path": "/home/hunter/wiki/index.md",
                    "old_string": "old",
                    "new_string": "new",
                },
                task_id,
            )
            is None
        )
    finally:
        deactivate_for_task(task_id)


def test_allows_non_code_v4a_patch_before_routing_decision():
    task_id = "task-routing-doc-v4a"
    activate_for_task(task_id, session_id="session-doc-v4a", skills=["routing-layer"])
    try:
        assert (
            pre_tool_call_block_reason(
                "patch",
                {
                    "mode": "patch",
                    "patch": "*** Begin Patch\n*** Add File: /home/hunter/wiki/concepts/model-routing-system.md\n+test\n*** End Patch\n",
                },
                task_id,
            )
            is None
        )
    finally:
        deactivate_for_task(task_id)


def test_allows_readme_edit_before_routing_decision():
    task_id = "task-routing-readme-write"
    activate_for_task(task_id, session_id="session-readme-write", skills=["routing-layer"])
    try:
        assert (
            pre_tool_call_block_reason(
                "write_file",
                {"path": "/home/hunter/project/README.md", "content": "# Project\n"},
                task_id,
            )
            is None
        )
    finally:
        deactivate_for_task(task_id)


def test_allows_changelog_edit_before_routing_decision():
    task_id = "task-routing-changelog-write"
    activate_for_task(task_id, session_id="session-changelog-write", skills=["routing-layer"])
    try:
        assert (
            pre_tool_call_block_reason(
                "write_file",
                {"path": "/home/hunter/project/CHANGELOG.md", "content": "## 1.2.3\n"},
                task_id,
            )
            is None
        )
    finally:
        deactivate_for_task(task_id)


def test_blocks_behavior_markdown_before_routing_decision():
    task_id = "task-routing-behavior-markdown"
    activate_for_task(task_id, session_id="session-behavior-markdown", skills=["routing-layer"])
    try:
        for path in ("/home/hunter/.hermes/SOUL.md", "/home/hunter/project/AGENTS.md", "/home/hunter/.hermes/skills/foo/SKILL.md"):
            reason = pre_tool_call_block_reason(
                "write_file",
                {"path": path, "content": "changed"},
                task_id,
            )
            assert reason is not None
            assert "behavior-changing markdown" in reason
    finally:
        deactivate_for_task(task_id)


def test_blocks_config_and_executable_text_before_routing_decision():
    task_id = "task-routing-config-text"
    activate_for_task(task_id, session_id="session-config-text", skills=["routing-layer"])
    try:
        for path in (
            "/home/hunter/project/package.json",
            "/home/hunter/project/config.yaml",
            "/home/hunter/project/schema.sql",
            "/home/hunter/project/build.ps1",
            "/home/hunter/project/Directory.Build.props",
            "/home/hunter/project/Societies.csproj",
        ):
            reason = pre_tool_call_block_reason(
                "write_file",
                {"path": path, "content": "changed"},
                task_id,
            )
            assert reason is not None
            assert "config or executable text" in reason
    finally:
        deactivate_for_task(task_id)


def test_blocks_docs_patch_under_code_sensitive_root_before_routing():
    task_id = "task-routing-docs-in-src"
    activate_for_task(task_id, session_id="session-docs-in-src", skills=["routing-layer"])
    try:
        reason = pre_tool_call_block_reason(
            "patch",
            {
                "mode": "replace",
                "path": "/home/hunter/project/src/README.md",
                "old_string": "old",
                "new_string": "new",
            },
            task_id,
        )
        assert reason is not None
        assert "code or code-sensitive project paths" in reason
    finally:
        deactivate_for_task(task_id)


def test_blocks_mixed_docs_and_code_v4a_patch_before_routing():
    task_id = "task-routing-mixed-v4a"
    activate_for_task(task_id, session_id="session-mixed-v4a", skills=["routing-layer"])
    try:
        reason = pre_tool_call_block_reason(
            "patch",
            {
                "mode": "patch",
                "patch": (
                    "*** Begin Patch\n"
                    "*** Add File: /home/hunter/project/docs/notes.md\n"
                    "+doc\n"
                    "*** Add File: /home/hunter/project/src/app.py\n"
                    "+print('x')\n"
                    "*** End Patch\n"
                ),
            },
            task_id,
        )
        assert reason is not None
        assert "mixes docs and code targets" in reason
    finally:
        deactivate_for_task(task_id)


def test_allows_skill_scoped_plan_write_before_routing():
    task_id = "task-routing-plan-skill"
    activate_for_task(
        task_id,
        session_id="session-plan-skill",
        skills=["routing-layer"],
        active_skill_hints=[
            {
                "skill_name": "plan",
                "task_class": "non_coding_authoring",
                "non_code_write_globs": [".hermes/plans/**", "**/.hermes/plans/**"],
            }
        ],
    )
    try:
        assert get_task_class(task_id) == "non_coding_authoring"
        assert (
            pre_tool_call_block_reason(
                "write_file",
                {"path": "/home/hunter/project/.hermes/plans/next-pass.md", "content": "# Plan\n"},
                task_id,
            )
            is None
        )
    finally:
        deactivate_for_task(task_id)


def test_skill_view_updates_active_skill_hints_mid_task():
    task_id = "task-routing-skill-view-update"
    activate_for_task(task_id, session_id="session-skill-view-update", skills=["routing-layer"])
    try:
        record_tool_result(
            task_id,
            "skill_view",
            {"name": "llm-wiki"},
            json.dumps(
                {
                    "success": True,
                    "name": "llm-wiki",
                    "path": "/home/hunter/.hermes/skills/research/llm-wiki/SKILL.md",
                    "metadata": {
                        "hermes": {
                            "routing": {
                                "task_class": "non_coding_authoring",
                                "non_code_write_globs": ["wiki/**"],
                            }
                        }
                    },
                }
            ),
        )
        assert get_task_class(task_id) == "non_coding_authoring"
        assert get_active_skill_hints(task_id) == [
            {
                "skill_name": "llm-wiki",
                "skill_path": "/home/hunter/.hermes/skills/research/llm-wiki/SKILL.md",
                "task_class": "non_coding_authoring",
                "non_code_write_globs": ["wiki/**"],
            }
        ]
    finally:
        deactivate_for_task(task_id)


def test_activate_for_task_records_current_session_lane_identity():
    task_id = "task-routing-session-lane"
    activate_for_task(
        task_id,
        session_id="session-lane",
        skills=["routing-layer"],
        session_model="xiaomi/mimo-v2-pro",
        session_provider="nous",
    )
    try:
        context = get_session_lane_context(task_id)
        assert context == {
            "model": "xiaomi/mimo-v2-pro",
            "provider": "nous",
            "label": "xiaomi/mimo-v2-pro via nous",
        }
    finally:
        deactivate_for_task(task_id)


def test_allows_file_mutation_after_routing_decision():
    task_id = "task-routing-allow"
    activate_for_task(task_id, session_id="session-2", skills=["routing-layer"])
    try:
        recorded = record_routing_decision(
            task_id,
            "TIER: 3B | MODEL: Hermes CLI (glm-5.1 via zai) | REASON: single-file bug fix | CONFIDENCE: high",
            session_id="session-2",
        )
        assert recorded is True
        assert has_route_lock(task_id) is True
        blocked = pre_tool_call_block_reason("write_file", {"path": "demo.py"}, task_id)
        assert blocked is not None
        assert "native `write_file`" in blocked
    finally:
        deactivate_for_task(task_id)


def test_blocks_native_docs_write_after_routing_decision():
    task_id = "task-routing-docs-after-route"
    activate_for_task(task_id, session_id="session-docs-after-route", skills=["routing-layer"])
    try:
        recorded = record_routing_decision(
            task_id,
            "TIER: 3B | MODEL: Hermes CLI (glm-5.1 via zai) | REASON: docs plus implementation | CONFIDENCE: high",
            session_id="session-docs-after-route",
        )
        assert recorded is True
        blocked = pre_tool_call_block_reason(
            "write_file",
            {"path": "/home/hunter/project/README.md", "content": "# still routed\n"},
            task_id,
        )
        assert blocked is not None
        assert "native `write_file`" in blocked
    finally:
        deactivate_for_task(task_id)


def test_blocks_routed_exec_before_routing_decision():
    task_id = "task-routing-routed-exec-block"
    activate_for_task(task_id, session_id="session-routed-exec-block", skills=["routing-layer"])
    try:
        blocked = pre_tool_call_block_reason(
            "routed_exec",
            {"task": "Apply the fix", "workdir": "/home/hunter/societies"},
            task_id,
        )
        assert blocked is not None
        assert "emit a routing decision line" in blocked
    finally:
        deactivate_for_task(task_id)


def test_allows_routed_exec_after_routing_decision():
    task_id = "task-routing-routed-exec-allow"
    activate_for_task(task_id, session_id="session-routed-exec-allow", skills=["routing-layer"])
    try:
        assert record_routing_decision(
            task_id,
            "TIER: 3A | MODEL: Codex CLI (gpt-5.4) | REASON: multi-file fix | CONFIDENCE: high",
            session_id="session-routed-exec-allow",
        )
        assert (
            pre_tool_call_block_reason(
                "routed_exec",
                {"task": "Apply the fix", "workdir": "/home/hunter/societies"},
                task_id,
            )
            is None
        )
    finally:
        deactivate_for_task(task_id)


def test_records_markdown_wrapped_routing_decision():
    task_id = "task-routing-markdown"
    activate_for_task(task_id, session_id="session-markdown", skills=["routing-layer"])
    try:
        recorded = record_routing_decision(
            task_id,
            "**TIER: 3A | MODEL: Codex CLI (gpt-5.4) | REASON: multi-file fix | CONFIDENCE: medium**",
            session_id="session-markdown",
        )
        assert recorded is True
        assert has_route_lock(task_id) is True
    finally:
        deactivate_for_task(task_id)


def test_records_explicit_route_path_for_long_context():
    task_id = "task-routing-path-long-context"
    activate_for_task(task_id, session_id="session-path-long-context", skills=["routing-layer"])
    try:
        recorded = record_routing_decision(
            task_id,
            "TIER: 3B | PATH: long-context | MODEL: Hermes CLI (xiaomi/mimo-v2-pro via nous) | REASON: huge repo/documentation scan | CONFIDENCE: high",
            session_id="session-path-long-context",
        )
        assert recorded is True
        decision = get_routing_decision(task_id)
        assert decision is not None
        assert decision["path"] == "long-context"
        assert _plan_kind_labels(task_id) == [
            {"kind": "hermes_nous_mimo_v2_pro", "label": "Hermes CLI (xiaomi/mimo-v2-pro via nous)"},
            {"kind": "codex_gpt54mini", "label": "Codex CLI (gpt-5.4-mini)"},
        ]
    finally:
        deactivate_for_task(task_id)


def test_records_quick_edit_route_for_minimax_primary():
    task_id = "task-routing-path-quick-edit"
    activate_for_task(task_id, session_id="session-path-quick-edit", skills=["routing-layer"])
    try:
        recorded = record_routing_decision(
            task_id,
            "TIER: 3C | PATH: quick-edit | MODEL: Hermes CLI (MiniMax-M2.7 via minimax) | REASON: straightforward token-heavy edits | CONFIDENCE: high",
            session_id="session-path-quick-edit",
        )
        assert recorded is True
        decision = get_routing_decision(task_id)
        assert decision is not None
        assert decision["path"] == "quick-edit"
        assert _plan_kind_labels(task_id) == [
            {"kind": "hermes_minimax_m27", "label": "Hermes CLI (MiniMax-M2.7 via minimax)"},
            {"kind": "codex_gpt54mini", "label": "Codex CLI (gpt-5.4-mini)"},
        ]
    finally:
        deactivate_for_task(task_id)


def test_blocks_model_path_mismatch_for_long_context_route():
    task_id = "task-routing-path-mismatch"
    activate_for_task(task_id, session_id="session-path-mismatch", skills=["routing-layer"])
    try:
        recorded = record_routing_decision(
            task_id,
            "TIER: 3B | PATH: long-context | MODEL: Hermes CLI (glm-5.1 via zai) | REASON: mismatched model/path | CONFIDENCE: high",
            session_id="session-path-mismatch",
        )
        assert recorded is False
        blocked = pre_tool_call_block_reason(
            "routed_exec",
            {"task": "Do the work", "workdir": "/home/hunter/societies"},
            task_id,
        )
        assert blocked is not None
        assert "does not match the allowed models for path `long-context`" in blocked
    finally:
        deactivate_for_task(task_id)


def test_invalid_quick_edit_tier_gets_corrective_hint():
    task_id = "task-routing-quick-edit-tier-mismatch"
    activate_for_task(task_id, session_id="session-quick-edit-tier-mismatch", skills=["routing-layer"])
    try:
        recorded = record_routing_decision(
            task_id,
            "TIER: 3B | PATH: quick-edit | MODEL: Hermes CLI (MiniMax-M2.7 via minimax) | REASON: small fix | CONFIDENCE: high",
            session_id="session-quick-edit-tier-mismatch",
        )
        assert recorded is False
        blocked = pre_tool_call_block_reason(
            "routed_exec",
            {"task": "Do the work", "workdir": "/home/hunter/societies"},
            task_id,
        )
        assert blocked is not None
        assert "`quick-edit` is not allowed for 3B" in blocked
        assert "`quick-edit` belongs to 3C" in blocked
        assert "TIER: 3C | PATH: quick-edit | MODEL: Hermes CLI (MiniMax-M2.7 via minimax)" in blocked
    finally:
        deactivate_for_task(task_id)


def test_allows_read_only_terminal_but_blocks_mutating_terminal():
    task_id = "task-routing-terminal"
    activate_for_task(task_id, session_id="session-3", skills=["routing-layer"])
    try:
        assert (
            pre_tool_call_block_reason(
                "terminal",
                {"command": "git status"},
                task_id,
            )
            is None
        )
        blocked = pre_tool_call_block_reason(
            "terminal",
            {"command": "pytest tests/test_demo.py"},
            task_id,
        )
        assert blocked is not None
        assert "read-only inspection commands" in blocked
    finally:
        deactivate_for_task(task_id)


def test_allows_chained_read_only_terminal_inspection():
    task_id = "task-routing-terminal-chained"
    activate_for_task(task_id, session_id="session-3b", skills=["routing-layer"])
    try:
        assert (
            pre_tool_call_block_reason(
                "terminal",
                {"command": "cd ~/societies && git branch --show-current && git status --short | head -20"},
                task_id,
            )
            is None
        )
    finally:
        deactivate_for_task(task_id)


def test_allows_local_visual_preview_command_before_route_lock():
    task_id = "task-routing-local-preview-before-route"
    activate_for_task(task_id, session_id="session-local-preview-before-route", skills=["routing-layer"])
    try:
        assert (
            pre_tool_call_block_reason(
                "terminal",
                {
                    "command": "cd /home/hunter/matrix-glitch && python3 -m http.server 8765 --bind 127.0.0.1",
                },
                task_id,
            )
            is None
        )
        blocked = pre_tool_call_block_reason(
            "terminal",
            {
                "command": "cd /home/hunter/matrix-glitch && python3 -m http.server 8765 --bind 0.0.0.0",
            },
            task_id,
        )
        assert blocked is not None
        assert "localhost-only visual preview commands" in blocked
    finally:
        deactivate_for_task(task_id)


def test_allows_read_only_git_branch_listing_before_routing():
    task_id = "task-routing-terminal-git-branch-listing"
    activate_for_task(task_id, session_id="session-git-branch-listing", skills=["routing-layer"])
    try:
        assert (
            pre_tool_call_block_reason(
                "terminal",
                {"command": "cd ~/societies && git branch -a && git log --oneline -10 | head -5"},
                task_id,
            )
            is None
        )
    finally:
        deactivate_for_task(task_id)


def test_allows_read_only_terminal_with_quoted_pipe_pattern():
    task_id = "task-routing-terminal-quoted-pipe"
    activate_for_task(task_id, session_id="session-3c", skills=["routing-layer"])
    try:
        assert (
            pre_tool_call_block_reason(
                "terminal",
                {
                    "command": 'cd ~/societies && find . -name "*.cs" | grep -E "(Extended|characterize|runner|report)" | grep -v obj/ | grep -v .godot/'
                },
                task_id,
            )
            is None
        )
    finally:
        deactivate_for_task(task_id)


def test_allows_read_only_terminal_with_null_redirection():
    task_id = "task-routing-terminal-null-redirection"
    activate_for_task(task_id, session_id="session-3d", skills=["routing-layer"])
    try:
        assert (
            pre_tool_call_block_reason(
                "terminal",
                {"command": "cd ~/societies && ls -la characterize/ tests/PathSegmentLogisticsRunner/ 2>/dev/null"},
                task_id,
            )
            is None
        )
        assert (
            pre_tool_call_block_reason(
                "terminal",
                {"command": "cd ~/societies && ls *.md 2>/dev/null"},
                task_id,
            )
            is None
        )
    finally:
        deactivate_for_task(task_id)


def test_blocks_implementation_oriented_delegate_before_routing():
    task_id = "task-routing-delegate"
    activate_for_task(task_id, session_id="session-4", skills=["routing-layer"])
    try:
        blocked = pre_tool_call_block_reason(
            "delegate_task",
            {"goal": "Implement the fix and add tests"},
            task_id,
        )
        assert blocked is not None
        assert "implementation-oriented delegation" in blocked
    finally:
        deactivate_for_task(task_id)


def test_blocks_execute_code_before_routing():
    task_id = "task-routing-execute-code-before-route"
    activate_for_task(task_id, session_id="session-execute-code-before-route", skills=["routing-layer"])
    try:
        blocked = pre_tool_call_block_reason(
            "execute_code",
            {"code": "print('hi')"},
            task_id,
        )
        assert blocked is not None
        assert "do not use code execution to bypass routing" in blocked
    finally:
        deactivate_for_task(task_id)


def test_blocks_native_delegate_after_routing_decision():
    task_id = "task-routing-delegate-routed"
    activate_for_task(task_id, session_id="session-5", skills=["routing-layer"])
    try:
        record_routing_decision(
            task_id,
            "TIER: 3A | MODEL: Codex CLI (gpt-5.4) | REASON: multi-file change | CONFIDENCE: high",
            session_id="session-5",
        )
        blocked = pre_tool_call_block_reason(
            "delegate_task",
            {"goal": "Implement the fix and add tests"},
            task_id,
        )
        assert blocked is not None
        assert "native `delegate_task`" in blocked
    finally:
        deactivate_for_task(task_id)


def test_blocks_execute_code_after_routing_decision():
    task_id = "task-routing-execute-code-after-route"
    activate_for_task(task_id, session_id="session-execute-code-after-route", skills=["routing-layer"])
    try:
        record_routing_decision(
            task_id,
            "TIER: 3B | MODEL: Hermes CLI (glm-5.1 via zai) | REASON: medium-scope change | CONFIDENCE: high",
            session_id="session-execute-code-after-route",
        )
        blocked = pre_tool_call_block_reason(
            "execute_code",
            {"code": "print('hi')"},
            task_id,
        )
        assert blocked is not None
        assert "stay on the routed model path" in blocked
    finally:
        deactivate_for_task(task_id)


def test_blocks_native_terminal_mutation_after_routing_decision():
    task_id = "task-routing-terminal-native-mutation"
    activate_for_task(task_id, session_id="session-terminal-native-mutation", skills=["routing-layer"])
    try:
        record_routing_decision(
            task_id,
            "TIER: 3A | MODEL: Codex CLI (gpt-5.4) | REASON: multi-file change | CONFIDENCE: high",
            session_id="session-terminal-native-mutation",
        )
        blocked = pre_tool_call_block_reason(
            "terminal",
            {"command": "python -c \"open('zzz.txt','w').write('hi')\""},
            task_id,
        )
        assert blocked is not None
        assert "native `terminal` execution" in blocked
    finally:
        deactivate_for_task(task_id)


def test_allows_read_only_terminal_inspection_after_routing_decision():
    task_id = "task-routing-terminal-readonly-after-route"
    activate_for_task(task_id, session_id="session-terminal-readonly-after-route", skills=["routing-layer"])
    try:
        record_routing_decision(
            task_id,
            "TIER: 3B | MODEL: Hermes CLI (glm-5.1 via zai) | REASON: medium-scope fix | CONFIDENCE: high",
            session_id="session-terminal-readonly-after-route",
        )
        assert (
            pre_tool_call_block_reason(
                "terminal",
                {"command": "git status --short"},
                task_id,
            )
            is None
        )
    finally:
        deactivate_for_task(task_id)


def test_allows_local_verification_terminal_after_routing_decision():
    task_id = "task-routing-terminal-verification-after-route"
    activate_for_task(task_id, session_id="session-terminal-verification-after-route", skills=["routing-layer"])
    try:
        record_routing_decision(
            task_id,
            "TIER: 3B | MODEL: Hermes CLI (glm-5.1 via zai) | REASON: medium-scope fix | CONFIDENCE: high",
            session_id="session-terminal-verification-after-route",
        )
        assert (
            pre_tool_call_block_reason(
                "terminal",
                {"command": "timeout 90 python -m pytest tests/test_demo.py -q"},
                task_id,
            )
            is None
        )
    finally:
        deactivate_for_task(task_id)


def test_blocks_verification_output_pipe_with_specific_guidance():
    task_id = "task-routing-terminal-verification-pipe"
    activate_for_task(task_id, session_id="session-terminal-verification-pipe", skills=["routing-layer"])
    try:
        record_routing_decision(
            task_id,
            "TIER: 3B | MODEL: Hermes CLI (glm-5.1 via zai) | REASON: medium-scope fix | CONFIDENCE: high",
            session_id="session-terminal-verification-pipe",
        )
        blocked = pre_tool_call_block_reason(
            "terminal",
            {"command": "cd ~/societies && dotnet build src/societies/Societies.csproj 2>&1 | tail -5"},
            task_id,
        )
        assert blocked is not None
        assert "blocked verification through `terminal`" in blocked
        assert "Run the `dotnet build` command directly." in blocked
    finally:
        deactivate_for_task(task_id)


def test_records_local_verification_attempts():
    task_id = "task-routing-terminal-verification-record"
    activate_for_task(task_id, session_id="session-terminal-verification-record", skills=["routing-layer"])
    try:
        record_routing_decision(
            task_id,
            "TIER: 3C | MODEL: Codex CLI (gpt-5.4-mini) | REASON: small fix | CONFIDENCE: high",
            session_id="session-terminal-verification-record",
        )
        record_tool_result(
            task_id,
            "terminal",
            {"command": "timeout 120 dotnet test tests/Societies.Core.Tests/Societies.Core.Tests.csproj --filter PrototypePersistence"},
            json.dumps({"output": "Passed!", "exit_code": 0, "error": None}),
        )
        attempts = get_verification_attempts(task_id)
        assert len(attempts) == 1
        assert attempts[0]["kind"] == "dotnet test"
        assert attempts[0]["success"] is True
        assert attempts[0]["exit_code"] == 0
        assert attempts[0]["output_excerpt"] == "Passed!"
    finally:
        deactivate_for_task(task_id)


def test_blocks_routed_codex_exec_with_powershell_cd_and_andand():
    task_id = "task-routing-codex-shell"
    activate_for_task(task_id, session_id="session-6", skills=["routing-layer"])
    try:
        record_routing_decision(
            task_id,
            "TIER: 3A | MODEL: Codex CLI (gpt-5.4) | REASON: multi-file fix | CONFIDENCE: high",
            session_id="session-6",
        )
        blocked = pre_tool_call_block_reason(
            "terminal",
            {
                "command": "cd ~/societies && codex exec --skip-git-repo-check -C /home/hunter/societies -s workspace-write -m gpt-5.4 -c 'reasoning_effort=\"extra-high\"' 'Implement the fix'",
            },
            task_id,
        )
        assert blocked is not None
        assert "use `routed_exec`" in blocked
    finally:
        deactivate_for_task(task_id)


def test_blocks_long_inline_routed_codex_prompt_without_stdin():
    task_id = "task-routing-codex-stdin"
    activate_for_task(task_id, session_id="session-7", skills=["routing-layer"])
    try:
        record_routing_decision(
            task_id,
            "TIER: 3A | MODEL: Codex CLI (gpt-5.4) | REASON: multi-file fix | CONFIDENCE: high",
            session_id="session-7",
        )
        blocked = pre_tool_call_block_reason(
            "terminal",
            {
                "command": f"codex exec --skip-git-repo-check -C /home/hunter/societies -s workspace-write -m gpt-5.4 -c 'reasoning_effort=\"extra-high\"' '{'A' * 1400}'",
            },
            task_id,
        )
        assert blocked is not None
        assert "use `routed_exec`" in blocked
    finally:
        deactivate_for_task(task_id)


def test_blocks_routed_codex_prompt_via_terminal_even_when_stdin_shaped():
    task_id = "task-routing-codex-stdin-block"
    activate_for_task(task_id, session_id="session-8", skills=["routing-layer"])
    try:
        record_routing_decision(
            task_id,
            "TIER: 3A | MODEL: Codex CLI (gpt-5.4) | REASON: multi-file fix | CONFIDENCE: high",
            session_id="session-8",
        )
        blocked = pre_tool_call_block_reason(
            "terminal",
            {
                "command": "@'\nImplement the fix\n'@ | codex exec --skip-git-repo-check -C /home/hunter/societies -s workspace-write -m gpt-5.4 -c 'reasoning_effort=\"extra-high\"' -",
            },
            task_id,
        )
        assert blocked is not None
        assert "use `routed_exec`" in blocked
    finally:
        deactivate_for_task(task_id)


def test_route_stays_frozen_without_explicit_reclassification():
    task_id = "task-routing-freeze"
    activate_for_task(task_id, session_id="session-freeze", skills=["routing-layer"])
    try:
        assert record_routing_decision(
            task_id,
            "TIER: 3A | MODEL: Codex CLI (gpt-5.4) | REASON: high-risk change | CONFIDENCE: high",
            session_id="session-freeze",
        )
        assert (
            record_routing_decision(
                task_id,
                "TIER: 3B | MODEL: Hermes CLI (glm-5.1 via zai) | REASON: actually smaller | CONFIDENCE: high",
                session_id="session-freeze",
            )
            is False
        )
        decision = get_routing_decision(task_id)
        assert decision is not None
        assert decision["tier"] == "3A"
        assert decision["model"] == "Codex CLI (gpt-5.4)"
        selected = get_selected_route(task_id)
        assert selected["policy_version"] == "3.0.0"
        assert selected["tier"] == "3A"
        assert selected["path"] == "high-risk"
        blocked = pre_tool_call_block_reason(
            "terminal",
            {
                "command": "codex exec --skip-git-repo-check -C /home/hunter/societies -s workspace-write -m gpt-5.4 -c 'reasoning_effort=\"extra-high\"' 'Apply the fix'",
            },
            task_id,
        )
        assert blocked is not None
        assert "blocked route drift" in blocked
    finally:
        deactivate_for_task(task_id)


def test_explicit_reclassification_updates_route():
    task_id = "task-routing-reclassify"
    activate_for_task(task_id, session_id="session-reclassify", skills=["routing-layer"])
    try:
        assert record_routing_decision(
            task_id,
            "TIER: 3A | MODEL: Codex CLI (gpt-5.4) | REASON: high-risk change | CONFIDENCE: high",
            session_id="session-reclassify",
        )
        assert record_routing_decision(
            task_id,
            "RECLASSIFY: TIER: 3B | MODEL: Hermes CLI (glm-5.1 via zai) | REASON: scope narrowed to one subsystem | CONFIDENCE: high",
            session_id="session-reclassify",
        )
        decision = get_routing_decision(task_id)
        assert decision is not None
        assert decision["tier"] == "3B"
        assert decision["model"] == "Hermes CLI (glm-5.1 via zai)"
        selected = get_selected_route(task_id)
        assert selected["tier"] == "3B"
        assert selected["path"] == "marathon"
    finally:
        deactivate_for_task(task_id)


def test_invalid_route_model_label_blocks_follow_on_tool_use():
    task_id = "task-routing-invalid-model"
    activate_for_task(task_id, session_id="session-invalid-model", skills=["routing-layer"])
    try:
        assert record_routing_decision(
            task_id,
            "TIER: 3C | MODEL: local execution | REASON: quick verification step | CONFIDENCE: high",
            session_id="session-invalid-model",
        ) is False
        blocked = pre_tool_call_block_reason(
            "terminal",
            {
                "command": "timeout 90 dotnet test tests/Societies.Core.Tests/Societies.Core.Tests.csproj --filter PrototypePersistence",
            },
            task_id,
        )
        assert blocked is not None
        assert "invalid routing decision" in blocked
        assert "`Codex CLI (gpt-5.4-mini)`" in blocked
    finally:
        deactivate_for_task(task_id)


def test_tier_3b_reclassified_to_codex_backup_requires_codex_route():
    task_id = "task-routing-3b-reclassified-backup"
    activate_for_task(task_id, session_id="session-3b-reclassified-backup", skills=["routing-layer"])
    try:
        assert record_routing_decision(
            task_id,
            "TIER: 3B | MODEL: Hermes CLI (glm-5.1 via zai) | REASON: medium-scope fix | CONFIDENCE: high",
            session_id="session-3b-reclassified-backup",
        )
        record_tool_result(
            task_id,
            "terminal",
            {
                "command": 'hermes chat -m glm-5.1 --provider zai -q "Apply the fix" -t terminal,file -Q',
            },
            '{"output":"provider failed","exit_code":1,"error":null}',
        )
        assert record_routing_decision(
            task_id,
            "RECLASSIFY: TIER: 3B | MODEL: Codex CLI (gpt-5.4-mini) | REASON: primary route failed; using backup | CONFIDENCE: high",
            session_id="session-3b-reclassified-backup",
        )
        assert _plan_kind_labels(task_id) == [
            {"kind": "codex_gpt54mini", "label": "Codex CLI (gpt-5.4-mini)"}
        ]
    finally:
        deactivate_for_task(task_id)


def test_tier_3b_requires_primary_before_codex_backup():
    task_id = "task-routing-3b-primary"
    activate_for_task(task_id, session_id="session-3b-primary", skills=["routing-layer"])
    try:
        record_routing_decision(
            task_id,
            "TIER: 3B | MODEL: Hermes CLI (glm-5.1 via zai) | REASON: medium-scope fix | CONFIDENCE: high",
            session_id="session-3b-primary",
        )
        assert _plan_kind_labels(task_id) == [
            {"kind": "hermes_glm_zai", "label": "Hermes CLI (glm-5.1 via zai)"},
            {"kind": "codex_gpt54mini", "label": "Codex CLI (gpt-5.4-mini)"},
        ]

        record_tool_result(
            task_id,
            "routed_exec",
            {
                "task": "Apply the fix",
                "workdir": "/home/hunter/societies",
            },
            json.dumps(
                {
                    "attempts": [
                        {
                            "kind": "hermes_glm_zai",
                            "executor": "Hermes CLI (glm-5.1 via zai)",
                            "output": "provider failed",
                            "exit_code": 1,
                            "failed": True,
                            "failure_kind": "transport_failure",
                        }
                    ]
                }
            ),
        )

        assert _plan_kind_labels(task_id) == [
            {"kind": "codex_gpt54mini", "label": "Codex CLI (gpt-5.4-mini)"}
        ]
    finally:
        deactivate_for_task(task_id)


def test_tier_3b_does_not_unlock_backup_when_primary_reports_structured_success():
    task_id = "task-routing-3b-primary-structured-success"
    activate_for_task(task_id, session_id="session-3b-output-failure", skills=["routing-layer"])
    try:
        record_routing_decision(
            task_id,
            "TIER: 3B | MODEL: Hermes CLI (glm-5.1 via zai) | REASON: medium-scope fix | CONFIDENCE: high",
            session_id="session-3b-output-failure",
        )
        record_tool_result(
            task_id,
            "routed_exec",
            {
                "task": "Apply the fix",
                "workdir": "/home/hunter/societies",
            },
            json.dumps(
                {
                    "attempts": [
                        {
                            "kind": "hermes_glm_zai",
                            "executor": "Hermes CLI (glm-5.1 via zai)",
                            "output": "HTTP 429: Insufficient balance or no resource package. Please recharge.",
                            "exit_code": 0,
                            "status": "success",
                            "warning_kinds": ["quota_exhausted"],
                        }
                    ]
                }
            ),
        )
        assert _plan_kind_labels(task_id) == [
            {"kind": "hermes_glm_zai", "label": "Hermes CLI (glm-5.1 via zai)"},
            {"kind": "codex_gpt54mini", "label": "Codex CLI (gpt-5.4-mini)"}
        ]
    finally:
        deactivate_for_task(task_id)


def test_rewrites_routed_hermes_glm_command_to_coding_endpoint():
    task_id = "task-routing-hermes-zai-endpoint"
    activate_for_task(task_id, session_id="session-hermes-zai-endpoint", skills=["routing-layer"])
    try:
        record_routing_decision(
            task_id,
            "TIER: 3B | MODEL: Hermes CLI (glm-5.1 via zai) | REASON: medium-scope fix | CONFIDENCE: high",
            session_id="session-hermes-zai-endpoint",
        )
        blocked = pre_tool_call_block_reason(
            "terminal",
            {
                "command": 'cd /home/hunter/societies && hermes chat -m glm-5.1 --provider zai -q "Apply the fix" -t terminal,file -Q',
            },
            task_id,
        )
        assert blocked is not None
        assert "use `routed_exec`" in blocked
    finally:
        deactivate_for_task(task_id)


def test_blocks_routed_hermes_output_truncation_pipe():
    task_id = "task-routing-hermes-tail"
    activate_for_task(task_id, session_id="session-hermes-tail", skills=["routing-layer"])
    try:
        record_routing_decision(
            task_id,
            "TIER: 3B | MODEL: Hermes CLI (glm-5.1 via zai) | REASON: medium-scope fix | CONFIDENCE: high",
            session_id="session-hermes-tail",
        )
        blocked = pre_tool_call_block_reason(
            "terminal",
            {
                "command": 'hermes chat -m glm-5.1 --provider zai -q "Apply the fix" -t terminal,file -Q 2>&1 | tail -40',
            },
            task_id,
        )
        assert blocked is not None
        assert "use `routed_exec`" in blocked
    finally:
        deactivate_for_task(task_id)


def test_allows_local_visual_preview_command_after_route_lock():
    task_id = "task-routing-local-preview"
    activate_for_task(task_id, session_id="session-local-preview", skills=["routing-layer"])
    try:
        record_routing_decision(
            task_id,
            "TIER: 3B | MODEL: Hermes CLI (glm-5.1 via zai) | REASON: medium-scope fix | CONFIDENCE: high",
            session_id="session-local-preview",
        )
        assert (
            pre_tool_call_block_reason(
                "terminal",
                {
                    "command": "cd /home/hunter/societies && python3 -m http.server 8765 --bind 127.0.0.1",
                },
                task_id,
            )
            is None
        )
    finally:
        deactivate_for_task(task_id)


def test_blocks_routed_codex_cat_substitution_prompt_shape():
    task_id = "task-routing-codex-cat-substitution"
    activate_for_task(task_id, session_id="session-codex-cat", skills=["routing-layer"])
    try:
        record_routing_decision(
            task_id,
            "TIER: 3A | MODEL: Codex CLI (gpt-5.4) | REASON: multi-file fix | CONFIDENCE: high",
            session_id="session-codex-cat",
        )
        blocked = pre_tool_call_block_reason(
            "terminal",
            {
                "command": 'codex exec --skip-git-repo-check -C /home/hunter/societies -s workspace-write -m gpt-5.4 -c \'reasoning_effort="extra-high"\' "$(cat /tmp/frontier_fix_prompt.txt)"',
            },
            task_id,
        )
        assert blocked is not None
        assert "use `routed_exec`" in blocked
    finally:
        deactivate_for_task(task_id)


def test_blocks_git_commit_push_and_cleanup_without_explicit_request():
    task_id = "task-routing-git-guard"
    activate_for_task(
        task_id,
        session_id="session-git-guard",
        skills=["routing-layer"],
        user_message="Please implement the fix and add tests.",
    )
    try:
        assert "git commit" in pre_tool_call_block_reason(
            "terminal",
            {"command": "git commit -m \"ship it\""},
            task_id,
        )
        assert "git push" in pre_tool_call_block_reason(
            "terminal",
            {"command": "git push"},
            task_id,
        )
        cleanup_reason = pre_tool_call_block_reason(
            "terminal",
            {"command": "git checkout AGENTS.md src/societies/Societies.csproj"},
            task_id,
        )
        assert cleanup_reason is not None
        assert "must not be used to clean up unrelated changes" in cleanup_reason
    finally:
        deactivate_for_task(task_id)


def test_allows_git_commit_and_push_when_user_explicitly_requests_it():
    task_id = "task-routing-git-allowed"
    activate_for_task(
        task_id,
        session_id="session-git-allowed",
        skills=["routing-layer"],
        user_message="Please make a commit and push the branch when you're done.",
    )
    try:
        record_routing_decision(
            task_id,
            "TIER: 3C | MODEL: Codex CLI (gpt-5.4-mini) | REASON: small fix | CONFIDENCE: high",
            session_id="session-git-allowed",
        )
        assert pre_tool_call_block_reason(
            "terminal",
            {"command": "git commit -m \"ship it\""},
            task_id,
        ) is None
        assert pre_tool_call_block_reason(
            "terminal",
            {"command": "git push"},
            task_id,
        ) is None
    finally:
        deactivate_for_task(task_id)
