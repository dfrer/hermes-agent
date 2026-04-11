"""Tests for model_tools.py — function call dispatch, agent-loop interception, legacy toolsets."""

import json
import time
from unittest.mock import MagicMock, call, patch

import pytest

from agent.entitlements import QuotaSnapshot
from agent.routing_guard import (
    activate_for_task,
    deactivate_for_task,
    get_verification_attempts,
    record_ability_packet,
    record_routing_decision,
)
from agent.ability_context import make_ability_packet
from model_tools import (
    handle_function_call,
    get_all_tool_names,
    get_tool_definitions,
    get_toolset_for_tool,
    _AGENT_LOOP_TOOLS,
    _LEGACY_TOOLSET_MAP,
    TOOL_TO_TOOLSET_MAP,
)


@pytest.fixture(autouse=True)
def _stable_codex_quota(monkeypatch):
    monkeypatch.setattr(
        "agent.entitlements.observe_codex_quota",
        lambda config=None, now=None: QuotaSnapshot(
            spend_class="openai",
            source="test",
            status="available",
            reason="allowed",
            captured_at=now or time.time(),
            age_seconds=0.0,
            provider="openai-codex",
        ),
    )


# =========================================================================
# handle_function_call
# =========================================================================

class TestHandleFunctionCall:
    def test_agent_loop_tool_returns_error(self):
        for tool_name in _AGENT_LOOP_TOOLS:
            result = json.loads(handle_function_call(tool_name, {}))
            assert "error" in result
            assert "agent loop" in result["error"].lower()

    def test_unknown_tool_returns_error(self):
        result = json.loads(handle_function_call("totally_fake_tool_xyz", {}))
        assert "error" in result
        assert "totally_fake_tool_xyz" in result["error"]

    def test_exception_returns_json_error(self):
        # Even if something goes wrong, should return valid JSON
        result = handle_function_call("terminal", None)  # None args may cause issues
        parsed = json.loads(result)
        assert isinstance(parsed, dict)
        assert "error" in parsed
        assert len(parsed["error"]) > 0
        assert any(
            token in parsed["error"].lower()
            for token in ("error", "failed", "unknown", "invalid")
        )

    def test_tool_hooks_receive_session_and_tool_call_ids(self):
        with (
            patch("model_tools.registry.dispatch", return_value='{"ok":true}'),
            patch("hermes_cli.plugins.invoke_hook") as mock_invoke_hook,
        ):
            result = handle_function_call(
                "web_search",
                {"q": "test"},
                task_id="task-1",
                tool_call_id="call-1",
                session_id="session-1",
            )

        assert result == '{"ok":true}'
        assert mock_invoke_hook.call_args_list == [
            call(
                "pre_tool_call",
                tool_name="web_search",
                args={"q": "test"},
                task_id="task-1",
                session_id="session-1",
                tool_call_id="call-1",
            ),
            call(
                "post_tool_call",
                tool_name="web_search",
                args={"q": "test"},
                result='{"ok":true}',
                task_id="task-1",
                session_id="session-1",
                tool_call_id="call-1",
            ),
        ]

    def test_routed_plan_dispatch_receives_session_id_for_resume(self):
        with (
            patch("model_tools.registry.dispatch", return_value='{"ok":true}') as mock_dispatch,
            patch("hermes_cli.plugins.invoke_hook"),
        ):
            result = handle_function_call(
                "routed_plan",
                {"action": "status"},
                task_id="task-routed-plan-dispatch",
                session_id="session-routed-plan-dispatch",
            )

        assert result == '{"ok":true}'
        assert mock_dispatch.call_args.kwargs["session_id"] == "session-routed-plan-dispatch"

    def test_routing_status_dispatch_receives_session_id(self):
        with (
            patch("model_tools.registry.dispatch", return_value='{"ok":true}') as mock_dispatch,
            patch("hermes_cli.plugins.invoke_hook"),
        ):
            result = handle_function_call(
                "routing_status",
                {},
                task_id="task-routing-status-dispatch",
                session_id="session-routing-status-dispatch",
            )

        assert result == '{"ok":true}'
        assert mock_dispatch.call_args.kwargs["session_id"] == "session-routing-status-dispatch"

    def test_routing_guard_blocks_mutating_tool_without_decision(self):
        task_id = "guarded-task"
        activate_for_task(task_id, session_id="session-guard", skills=["routing-layer"])
        try:
            result = json.loads(
                handle_function_call(
                    "write_file",
                    {"path": "demo.py", "content": "print('x')"},
                    task_id=task_id,
                )
            )
        finally:
            deactivate_for_task(task_id)

        assert "error" in result
        assert "Routing guard blocked `write_file`" in result["error"]

    def test_routing_guard_allows_docs_only_write_without_decision(self):
        task_id = "guarded-task-docs-write"
        activate_for_task(task_id, session_id="session-guard-docs", skills=["routing-layer"])
        with (
            patch("model_tools.registry.dispatch", return_value='{"ok":true}') as mock_dispatch,
            patch("hermes_cli.plugins.invoke_hook"),
        ):
            try:
                result = json.loads(
                    handle_function_call(
                        "write_file",
                        {"path": "/home/hunter/wiki/entities/societies.md", "content": "# Societies\n"},
                        task_id=task_id,
                    )
                )
            finally:
                deactivate_for_task(task_id)

        assert result == {"ok": True}
        assert mock_dispatch.call_count == 1

    def test_routing_guard_blocks_behavior_markdown_write_without_decision(self):
        task_id = "guarded-task-behavior-write"
        activate_for_task(task_id, session_id="session-guard-behavior", skills=["routing-layer"])
        try:
            result = json.loads(
                handle_function_call(
                    "write_file",
                    {"path": "/home/hunter/project/AGENTS.md", "content": "# changed\n"},
                    task_id=task_id,
                )
            )
        finally:
            deactivate_for_task(task_id)

        assert "error" in result
        assert "behavior-changing markdown" in result["error"]

    def test_routing_guard_blocks_config_text_write_without_decision(self):
        task_id = "guarded-task-config-write"
        activate_for_task(task_id, session_id="session-guard-config", skills=["routing-layer"])
        try:
            result = json.loads(
                handle_function_call(
                    "write_file",
                    {"path": "/home/hunter/project/config.yaml", "content": "value: 1\n"},
                    task_id=task_id,
                )
            )
        finally:
            deactivate_for_task(task_id)

        assert "error" in result
        assert "config or executable text" in result["error"]

    def test_routing_guard_blocks_execute_code_before_routing(self):
        task_id = "guarded-task-execute-code-before-route"
        activate_for_task(task_id, session_id="session-guard-exec-before", skills=["routing-layer"])
        try:
            result = json.loads(
                handle_function_call(
                    "execute_code",
                    {"code": "print('hi')"},
                    task_id=task_id,
                )
            )
        finally:
            deactivate_for_task(task_id)

        assert "error" in result
        assert "do not use code execution to bypass routing" in result["error"]

    def test_routing_guard_blocks_native_mutating_tool_after_decision(self):
        task_id = "guarded-task-routed"
        activate_for_task(task_id, session_id="session-guard-2", skills=["routing-layer"])
        record_routing_decision(
            task_id,
            "TIER: 3C | MODEL: Codex CLI (gpt-5.4-mini) | REASON: trivial rename | CONFIDENCE: high",
            session_id="session-guard-2",
        )
        try:
            result = json.loads(
                handle_function_call(
                    "write_file",
                    {"path": "demo.py", "content": "print('x')"},
                    task_id=task_id,
                )
            )
        finally:
            deactivate_for_task(task_id)

        assert "error" in result
        assert "native `write_file`" in result["error"]

    def test_routing_guard_blocks_execute_code_after_routing_decision(self):
        task_id = "guarded-task-execute-code-after-route"
        activate_for_task(task_id, session_id="session-guard-exec-after", skills=["routing-layer"])
        record_routing_decision(
            task_id,
            "TIER: 3B | PATH: marathon | MODEL: Hermes CLI (glm-5.1 via zai) | REASON: medium-scope fix | CONFIDENCE: high",
            session_id="session-guard-exec-after",
        )
        try:
            result = json.loads(
                handle_function_call(
                    "execute_code",
                    {"code": "print('hi')"},
                    task_id=task_id,
                )
            )
        finally:
            deactivate_for_task(task_id)

        assert "error" in result
        assert "stay on the routed model path" in result["error"]

    def test_routed_exec_dispatches_codex_for_tier_3a(self, tmp_path):
        task_id = "guarded-task-codex-routed-exec"
        activate_for_task(task_id, session_id="session-guard-3", skills=["routing-layer"])
        record_routing_decision(
            task_id,
            "TIER: 3A | MODEL: Codex CLI (gpt-5.4) | REASON: multi-file fix | CONFIDENCE: high",
            session_id="session-guard-3",
        )
        with (
            patch(
                "tools.routed_exec_tool.subprocess.run",
                return_value=MagicMock(returncode=0, stdout="done", stderr=""),
            ) as mock_run,
            patch("tools.routed_exec_tool._find_executable", return_value="codex"),
            patch("hermes_cli.plugins.invoke_hook"),
        ):
            try:
                result = json.loads(
                    handle_function_call(
                        "routed_exec",
                        {
                            "task": "Apply the fix",
                            "workdir": str(tmp_path),
                        },
                        task_id=task_id,
                    )
                )
            finally:
                deactivate_for_task(task_id)

        assert result["success"] is True
        assert result["attempts"][0]["kind"] == "codex_gpt54"
        command = mock_run.call_args.args[0]
        assert command[:3] == ["codex", "exec", "--skip-git-repo-check"]
        assert command[-1] == "-"
        assert mock_run.call_args.kwargs["cwd"] == str(tmp_path)

    def test_routed_exec_degrades_3a_to_glm_after_task_scoped_approval(self, tmp_path):
        task_id = "guarded-task-codex-entitlement-downgrade"
        activate_for_task(task_id, session_id="session-guard-entitlement", skills=["routing-layer"])
        record_routing_decision(
            task_id,
            "TIER: 3A | MODEL: Codex CLI (gpt-5.4) | REASON: multi-file fix | CONFIDENCE: high",
            session_id="session-guard-entitlement",
        )
        blocked_codex = QuotaSnapshot(
            spend_class="openai",
            source="test",
            status="locked_paid_only",
            reason="locked_paid_spend",
            captured_at=time.time(),
            age_seconds=0.0,
            provider="openai-codex",
            primary_used_percent=100.0,
            primary_window_minutes=300,
            secondary_used_percent=80.0,
            secondary_window_minutes=10080,
            credits_present=True,
            credits_locked=True,
            credit_balance="123.45",
        )
        with (
            patch("agent.entitlements.observe_codex_quota", return_value=blocked_codex),
            patch("tools.routed_exec_tool.request_blocking_approval", return_value="once"),
            patch(
                "tools.routed_exec_tool.subprocess.run",
                return_value=MagicMock(
                    returncode=0,
                    stdout='HERMES_ROUTED_RESULT: {"status":"success","summary":"done"}',
                    stderr="",
                ),
            ) as mock_run,
            patch("tools.routed_exec_tool._find_executable", side_effect=lambda name: name),
            patch(
                "tools.routed_exec_tool.resolve_api_key_provider_credentials",
                return_value={
                    "base_url": "https://api.z.ai/api/coding/paas/v4",
                    "endpoint_source": "probe",
                    "endpoint_id": "coding-global",
                    "endpoint_label": "Global (Coding Plan)",
                },
            ),
            patch("hermes_cli.plugins.invoke_hook"),
        ):
            try:
                result = json.loads(
                    handle_function_call(
                        "routed_exec",
                        {
                            "task": "Apply the fix",
                            "workdir": str(tmp_path),
                        },
                        task_id=task_id,
                    )
                )
            finally:
                deactivate_for_task(task_id)

        assert result["success"] is True
        assert result["route_path"] == "high-risk"
        assert result["route_model"] == "Codex CLI (gpt-5.4)"
        assert result["attempts"][0]["kind"] == "hermes_glm_zai"
        assert result["selected_route"]["degraded"] is True
        assert result["selected_route"]["entitlement"]["degraded"] is True
        assert result["selected_route"]["effective_targets"][0]["kind"] == "hermes_glm_zai"
        command = mock_run.call_args.args[0]
        assert command[:4] == ["hermes", "chat", "-m", "glm-5.1"]

    def test_routed_exec_includes_ability_and_explicit_evidence(self, tmp_path):
        task_id = "guarded-task-routed-exec-evidence"
        activate_for_task(task_id, session_id="session-guard-evidence", skills=["routing-layer"])
        record_routing_decision(
            task_id,
            "TIER: 3A | MODEL: Codex CLI (gpt-5.4) | REASON: multi-file fix | CONFIDENCE: high",
            session_id="session-guard-evidence",
        )
        record_ability_packet(
            task_id,
            make_ability_packet(
                task_id=task_id,
                lanes=["visual"],
                phase="pre",
                summary="Screenshot shows a clipped button.",
                screenshot_path="/tmp/screen.png",
            ),
        )
        with (
            patch(
                "tools.routed_exec_tool.subprocess.run",
                return_value=MagicMock(returncode=0, stdout="done", stderr=""),
            ) as mock_run,
            patch("tools.routed_exec_tool._find_executable", return_value="codex"),
            patch("hermes_cli.plugins.invoke_hook"),
        ):
            try:
                result = json.loads(
                    handle_function_call(
                        "routed_exec",
                        {
                            "task": "Apply the fix",
                            "workdir": str(tmp_path),
                            "evidence": "Caller saw the same clipping at mobile width.",
                        },
                        task_id=task_id,
                    )
                )
            finally:
                deactivate_for_task(task_id)

        assert result["success"] is True
        assert result["ability_evidence_included"] is True
        prompt = mock_run.call_args.kwargs["input"]
        assert "Ability evidence handoff" in prompt
        assert "clipped button" in prompt
        assert "/tmp/screen.png" in prompt
        assert "Caller saw the same clipping" in prompt

    def test_routed_exec_dispatches_hermes_primary_for_tier_3b(self, tmp_path):
        task_id = "guarded-task-hermes-routed-exec"
        activate_for_task(task_id, session_id="session-guard-hermes", skills=["routing-layer"])
        record_routing_decision(
            task_id,
            "TIER: 3B | MODEL: Hermes CLI (glm-5.1 via zai) | REASON: medium-scope fix | CONFIDENCE: high",
            session_id="session-guard-hermes",
        )
        with (
            patch(
                "tools.routed_exec_tool.subprocess.run",
                return_value=MagicMock(
                    returncode=0,
                    stdout='HERMES_ROUTED_RESULT: {"status":"success","summary":"done"}',
                    stderr="",
                ),
            ) as mock_run,
            patch("tools.routed_exec_tool._find_executable", return_value="hermes"),
            patch(
                "tools.routed_exec_tool.resolve_api_key_provider_credentials",
                return_value={
                    "base_url": "https://api.z.ai/api/coding/paas/v4",
                    "endpoint_source": "probe",
                    "endpoint_id": "coding-global",
                    "endpoint_label": "Global (Coding Plan)",
                },
            ),
            patch("hermes_cli.plugins.invoke_hook"),
        ):
            try:
                result = json.loads(
                    handle_function_call(
                        "routed_exec",
                        {
                            "task": "Apply the fix",
                            "workdir": str(tmp_path),
                        },
                        task_id=task_id,
                    )
                )
            finally:
                deactivate_for_task(task_id)

        assert result["success"] is True
        assert result["attempts"][0]["kind"] == "hermes_glm_zai"
        assert result["resolved_base_url"] == "https://api.z.ai/api/coding/paas/v4"
        assert result["endpoint_source"] == "probe"
        command = mock_run.call_args.args[0]
        env = mock_run.call_args.kwargs["env"]
        assert command[:4] == ["hermes", "chat", "-m", "glm-5.1"]
        assert env["HERMES_DISABLE_DEFAULT_ROUTING_SKILL"] == "1"
        assert env["TERMINAL_CWD"] == str(tmp_path)
        assert "already-routed implementation executor" in env["HERMES_EPHEMERAL_SYSTEM_PROMPT"]
        assert any("HERMES_ROUTED_RESULT:" in str(item) for item in command)
        assert mock_run.call_args.kwargs["timeout"] == 900

    def test_routed_exec_find_executable_falls_back_to_current_venv_bin(self, tmp_path, monkeypatch):
        from tools import routed_exec_tool

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        hermes_bin = bin_dir / "hermes"
        hermes_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        hermes_bin.chmod(0o755)

        monkeypatch.setattr(routed_exec_tool.shutil, "which", lambda _name: None)
        monkeypatch.setattr(routed_exec_tool.sys, "executable", str(bin_dir / "python"))

        assert routed_exec_tool._find_executable("hermes") == str(hermes_bin)

    def test_routed_exec_dispatches_minimax_primary_for_quick_edit(self, tmp_path):
        task_id = "guarded-task-minimax-routed-exec"
        activate_for_task(
            task_id,
            session_id="session-guard-minimax",
            skills=["routing-layer"],
            session_model="MiniMax-M2.7",
            session_provider="minimax",
        )
        record_routing_decision(
            task_id,
            "TIER: 3C | PATH: quick-edit | MODEL: Hermes CLI (MiniMax-M2.7 via minimax) | REASON: simple token-heavy edit loop | CONFIDENCE: high",
            session_id="session-guard-minimax",
        )
        with (
            patch(
                "tools.routed_exec_tool.subprocess.run",
                return_value=MagicMock(returncode=0, stdout="done", stderr=""),
            ) as mock_run,
            patch("tools.routed_exec_tool._find_executable", return_value="hermes"),
            patch("hermes_cli.plugins.invoke_hook"),
        ):
            try:
                result = json.loads(
                    handle_function_call(
                        "routed_exec",
                        {
                            "task": "Apply the quick edit",
                            "workdir": str(tmp_path),
                        },
                        task_id=task_id,
                    )
                )
            finally:
                deactivate_for_task(task_id)

        assert result["success"] is True
        assert result["route_path"] == "quick-edit"
        assert result["session_lane"] == {
            "model": "MiniMax-M2.7",
            "provider": "minimax",
            "label": "MiniMax-M2.7 via minimax",
        }
        assert result["attempts"][0]["kind"] == "hermes_minimax_m27"
        command = mock_run.call_args.args[0]
        assert command[:6] == ["hermes", "chat", "-m", "MiniMax-M2.7", "--provider", "minimax"]
        assert mock_run.call_args.kwargs["timeout"] == 300

    def test_routed_exec_dispatches_mimo_primary_for_long_context(self, tmp_path):
        task_id = "guarded-task-mimo-routed-exec"
        activate_for_task(task_id, session_id="session-guard-mimo", skills=["routing-layer"])
        record_routing_decision(
            task_id,
            "TIER: 3B | PATH: long-context | MODEL: Hermes CLI (xiaomi/mimo-v2-pro via nous) | REASON: massive context analysis | CONFIDENCE: high",
            session_id="session-guard-mimo",
        )
        with (
            patch(
                "tools.routed_exec_tool.subprocess.run",
                return_value=MagicMock(returncode=0, stdout="done", stderr=""),
            ) as mock_run,
            patch("tools.routed_exec_tool._find_executable", return_value="hermes"),
            patch("hermes_cli.plugins.invoke_hook"),
        ):
            try:
                result = json.loads(
                    handle_function_call(
                        "routed_exec",
                        {
                            "task": "Analyze the large codebase",
                            "workdir": str(tmp_path),
                        },
                        task_id=task_id,
                    )
                )
            finally:
                deactivate_for_task(task_id)

        assert result["success"] is True
        assert result["route_path"] == "long-context"
        assert result["attempts"][0]["kind"] == "hermes_nous_mimo_v2_pro"
        command = mock_run.call_args.args[0]
        assert command[:6] == ["hermes", "chat", "-m", "xiaomi/mimo-v2-pro", "--provider", "nous"]

    def test_routing_guard_blocks_native_terminal_mutation_after_route_lock(self):
        task_id = "guarded-task-native-terminal-after-route"
        activate_for_task(task_id, session_id="session-native-terminal-after-route", skills=["routing-layer"])
        record_routing_decision(
            task_id,
            "TIER: 3A | MODEL: Codex CLI (gpt-5.4) | REASON: multi-file fix | CONFIDENCE: high",
            session_id="session-native-terminal-after-route",
        )
        try:
            result = json.loads(
                handle_function_call(
                    "terminal",
                    {
                        "command": "echo hi > zzz.txt",
                    },
                    task_id=task_id,
                )
            )
        finally:
            deactivate_for_task(task_id)

        assert "error" in result
        assert "native `terminal` execution" in result["error"]

    def test_verification_terminal_dispatches_and_records_attempt_after_route_lock(self):
        task_id = "guarded-task-terminal-verification-after-route"
        activate_for_task(task_id, session_id="session-terminal-verification-after-route", skills=["routing-layer"])
        record_routing_decision(
            task_id,
            "TIER: 3A | MODEL: Codex CLI (gpt-5.4) | REASON: multi-file fix | CONFIDENCE: high",
            session_id="session-terminal-verification-after-route",
        )
        with (
            patch(
                "model_tools.registry.dispatch",
                return_value='{"output":"2 passed","exit_code":0,"error":null}',
            ) as mock_dispatch,
            patch("hermes_cli.plugins.invoke_hook"),
        ):
            try:
                result = json.loads(
                    handle_function_call(
                        "terminal",
                        {
                            "command": "timeout 90 python -m pytest tests/test_demo.py -q",
                        },
                        task_id=task_id,
                    )
                )
                attempts = get_verification_attempts(task_id)
            finally:
                deactivate_for_task(task_id)

        assert result["exit_code"] == 0
        assert mock_dispatch.call_count == 1
        assert len(attempts) == 1
        assert attempts[0]["kind"] == "python -m pytest"
        assert attempts[0]["success"] is True
        assert attempts[0]["output_excerpt"] == "2 passed"

    def test_routed_model_terminal_invocation_is_blocked_in_favor_of_routed_exec(self):
        task_id = "guarded-task-terminal-routed-block"
        activate_for_task(task_id, session_id="session-guard-4", skills=["routing-layer"])
        record_routing_decision(
            task_id,
            "TIER: 3A | MODEL: Codex CLI (gpt-5.4) | REASON: multi-file fix | CONFIDENCE: high",
            session_id="session-guard-4",
        )
        try:
            result = json.loads(
                handle_function_call(
                    "terminal",
                    {
                        "command": "codex exec --skip-git-repo-check -C /home/hunter/societies -s workspace-write -m gpt-5.4 -c 'reasoning_effort=\"extra-high\"' 'Apply the fix'",
                    },
                    task_id=task_id,
                )
            )
        finally:
            deactivate_for_task(task_id)

        assert "error" in result
        assert "use `routed_exec`" in result["error"]

    def test_conflicting_route_without_reclassify_blocks_follow_on_tool_calls(self):
        task_id = "guarded-task-route-drift"
        activate_for_task(task_id, session_id="session-route-drift", skills=["routing-layer"])
        record_routing_decision(
            task_id,
            "TIER: 3B | MODEL: Hermes CLI (glm-5.1 via zai) | REASON: medium-scope fix | CONFIDENCE: high",
            session_id="session-route-drift",
        )
        assert (
            record_routing_decision(
                task_id,
                "TIER: 3C | MODEL: Codex CLI (gpt-5.4-mini) | REASON: actually smaller | CONFIDENCE: high",
                session_id="session-route-drift",
            )
            is False
        )
        try:
            result = json.loads(
                handle_function_call(
                    "terminal",
                    {
                        "command": 'hermes chat -m glm-5.1 --provider zai -q "Apply the fix" -t terminal,file -Q',
                    },
                    task_id=task_id,
                )
            )
        finally:
            deactivate_for_task(task_id)

        assert "error" in result
        assert "blocked route drift" in result["error"]

    def test_invalid_route_model_label_blocks_follow_on_tool_calls(self):
        task_id = "guarded-task-invalid-route-model"
        activate_for_task(task_id, session_id="session-invalid-route-model", skills=["routing-layer"])
        assert (
            record_routing_decision(
                task_id,
                "TIER: 3C | MODEL: local execution | REASON: verification only | CONFIDENCE: high",
                session_id="session-invalid-route-model",
            )
            is False
        )
        try:
            result = json.loads(
                handle_function_call(
                    "terminal",
                    {
                        "command": "timeout 90 dotnet test tests/Societies.Core.Tests/Societies.Core.Tests.csproj --filter PrototypePersistence",
                    },
                    task_id=task_id,
                )
            )
        finally:
            deactivate_for_task(task_id)

        assert "error" in result
        assert "invalid routing decision" in result["error"]

    def test_routed_exec_falls_back_to_codex_after_primary_failure(self, tmp_path):
        task_id = "guarded-task-3b-fallback"
        activate_for_task(task_id, session_id="session-guard-5", skills=["routing-layer"])
        record_routing_decision(
            task_id,
            "TIER: 3B | MODEL: Hermes CLI (glm-5.1 via zai) | REASON: medium-scope fix | CONFIDENCE: high",
            session_id="session-guard-5",
        )
        with (
            patch(
                "tools.routed_exec_tool.subprocess.run",
                side_effect=[
                    MagicMock(returncode=1, stdout="provider failed", stderr=""),
                    MagicMock(returncode=0, stdout="backup ok", stderr=""),
                ],
            ) as mock_run,
            patch("tools.routed_exec_tool._find_executable", side_effect=lambda name: name),
            patch("hermes_cli.plugins.invoke_hook"),
        ):
            try:
                result = json.loads(
                    handle_function_call(
                        "routed_exec",
                        {
                            "task": "Apply the fix",
                            "workdir": str(tmp_path),
                        },
                        task_id=task_id,
                    )
                )
            finally:
                deactivate_for_task(task_id)

        assert result["success"] is True
        assert len(result["attempts"]) == 2
        assert result["attempts"][0]["kind"] == "hermes_glm_zai"
        assert result["attempts"][0]["failed"] is True
        assert result["attempts"][1]["kind"] == "codex_gpt54mini"
        assert mock_run.call_count == 2

    def test_routed_exec_sticks_to_codex_backup_after_primary_failure(self, tmp_path):
        task_id = "guarded-task-3b-output-fallback"
        activate_for_task(task_id, session_id="session-guard-5b", skills=["routing-layer"])
        record_routing_decision(
            task_id,
            "TIER: 3B | MODEL: Hermes CLI (glm-5.1 via zai) | REASON: medium-scope fix | CONFIDENCE: high",
            session_id="session-guard-5b",
        )
        with (
            patch(
                "tools.routed_exec_tool.subprocess.run",
                side_effect=[
                    MagicMock(
                        returncode=0,
                        stdout=(
                            "HTTP 429: The service may be temporarily overloaded.\n"
                            'HERMES_ROUTED_RESULT: {"status":"success","summary":"done"}'
                        ),
                        stderr="",
                    ),
                    MagicMock(
                        returncode=-6,
                        stdout=(
                            'HERMES_ROUTED_RESULT: {"status":"success","summary":"verification complete"}\n'
                            "Fatal Python error: _enter_buffered_busy: could not acquire lock for <stdin> at interpreter shutdown"
                        ),
                        stderr="",
                    ),
                ],
            ) as mock_run,
            patch("tools.routed_exec_tool._find_executable", side_effect=lambda name: name),
            patch("hermes_cli.plugins.invoke_hook"),
        ):
            try:
                first_result = json.loads(
                    handle_function_call(
                        "routed_exec",
                        {
                            "task": "Apply the fix",
                            "workdir": str(tmp_path),
                        },
                        task_id=task_id,
                    )
                )
                second_result = json.loads(
                    handle_function_call(
                        "routed_exec",
                        {
                            "task": "Run verification",
                            "workdir": str(tmp_path),
                        },
                        task_id=task_id,
                    )
                )
            finally:
                deactivate_for_task(task_id)

        assert first_result["success"] is True
        assert len(first_result["attempts"]) == 1
        assert first_result["attempts"][0]["warning_kinds"] == ["rate_limited"]
        assert second_result["success"] is True
        assert len(second_result["attempts"]) == 1
        assert second_result["attempts"][0]["kind"] == "hermes_glm_zai"
        assert "executor_shutdown_after_success" in second_result["attempts"][0]["warning_kinds"]
        assert mock_run.call_count == 2

    def test_routed_exec_resolves_nonexistent_workdir_to_existing_parent(self, tmp_path):
        task_id = "guarded-task-routed-nonexistent-workdir"
        activate_for_task(task_id, session_id="session-routed-nonexistent-workdir", skills=["routing-layer"])
        record_routing_decision(
            task_id,
            "TIER: 3A | MODEL: Codex CLI (gpt-5.4) | REASON: multi-file fix | CONFIDENCE: high",
            session_id="session-routed-nonexistent-workdir",
        )
        target = tmp_path / "nested" / "project"
        with (
            patch(
                "tools.routed_exec_tool.subprocess.run",
                return_value=MagicMock(
                    returncode=0,
                    stdout='HERMES_ROUTED_RESULT: {"status":"success","summary":"done"}',
                    stderr="",
                ),
            ) as mock_run,
            patch("tools.routed_exec_tool._find_executable", return_value="codex"),
            patch("hermes_cli.plugins.invoke_hook"),
        ):
            try:
                result = json.loads(
                    handle_function_call(
                        "routed_exec",
                        {
                            "task": "Apply the fix",
                            "workdir": str(target),
                        },
                        task_id=task_id,
                    )
                )
            finally:
                deactivate_for_task(task_id)

        assert result["success"] is True
        assert result["requested_workdir"] == str(target)
        assert result["resolved_workdir"] == str(tmp_path)
        assert mock_run.call_args.kwargs["cwd"] == str(tmp_path)
        assert mock_run.call_args.args[0][4] == str(tmp_path)

    def test_routed_exec_returns_compact_failure_summary_for_timeout_chain(self, tmp_path):
        task_id = "guarded-task-routed-timeout-summary"
        activate_for_task(task_id, session_id="session-routed-timeout-summary", skills=["routing-layer"])
        record_routing_decision(
            task_id,
            "TIER: 3B | PATH: marathon | MODEL: Hermes CLI (glm-5.1 via zai) | REASON: medium-scope fix | CONFIDENCE: high",
            session_id="session-routed-timeout-summary",
        )
        with (
            patch(
                "tools.routed_exec_tool.subprocess.run",
                side_effect=[
                    MagicMock(returncode=124, stdout="timed out while editing files", stderr=""),
                    MagicMock(returncode=124, stdout="timed out while running verification", stderr=""),
                ],
            ),
            patch("tools.routed_exec_tool._find_executable", side_effect=lambda name: name),
            patch("hermes_cli.plugins.invoke_hook"),
        ):
            try:
                result = json.loads(
                    handle_function_call(
                        "routed_exec",
                        {
                            "task": "Apply the fix",
                            "workdir": str(tmp_path),
                        },
                        task_id=task_id,
                    )
                )
            finally:
                deactivate_for_task(task_id)

        assert result["success"] is False
        assert result["timeout_seconds"] == 900
        assert result["timeout_source"] == "route-default"
        assert len(result["attempt_summary"]) == 2
        assert "timed out" in (result["failure_guidance"] or "").lower()
        assert result["attempts"][0]["output_excerpt"]
        assert result["attempts"][0]["output_path"]

    def test_routed_exec_explicit_timeout_overrides_route_default(self, tmp_path):
        task_id = "guarded-task-routed-timeout-override"
        activate_for_task(task_id, session_id="session-routed-timeout-override", skills=["routing-layer"])
        record_routing_decision(
            task_id,
            "TIER: 3A | MODEL: Codex CLI (gpt-5.4) | REASON: high-risk fix | CONFIDENCE: high",
            session_id="session-routed-timeout-override",
        )
        with (
            patch(
                "tools.routed_exec_tool.subprocess.run",
                return_value=MagicMock(returncode=0, stdout="done", stderr=""),
            ) as mock_run,
            patch("tools.routed_exec_tool._find_executable", side_effect=lambda name: name),
            patch("hermes_cli.plugins.invoke_hook"),
        ):
            try:
                result = json.loads(
                    handle_function_call(
                        "routed_exec",
                        {
                            "task": "Apply the fix",
                            "workdir": str(tmp_path),
                            "timeout": 42,
                        },
                        task_id=task_id,
                    )
                )
            finally:
                deactivate_for_task(task_id)

        assert result["success"] is True
        assert result["timeout_seconds"] == 42
        assert result["timeout_source"] == "explicit"
        assert mock_run.call_args.kwargs["timeout"] == 42

    def test_git_commit_requires_explicit_user_permission(self):
        task_id = "guarded-task-git"
        activate_for_task(
            task_id,
            session_id="session-guard-6",
            skills=["routing-layer"],
            user_message="Please implement the fix.",
        )
        try:
            result = json.loads(
                handle_function_call(
                    "terminal",
                    {"command": 'git commit -m "ship it"'},
                    task_id=task_id,
                )
            )
        finally:
            deactivate_for_task(task_id)

        assert "error" in result
        assert "git commit" in result["error"]


# =========================================================================
# Agent loop tools
# =========================================================================

class TestAgentLoopTools:
    def test_expected_tools_in_set(self):
        assert "todo" in _AGENT_LOOP_TOOLS
        assert "memory" in _AGENT_LOOP_TOOLS
        assert "session_search" in _AGENT_LOOP_TOOLS
        assert "delegate_task" in _AGENT_LOOP_TOOLS

    def test_no_regular_tools_in_set(self):
        assert "web_search" not in _AGENT_LOOP_TOOLS
        assert "terminal" not in _AGENT_LOOP_TOOLS


# =========================================================================
# Legacy toolset map
# =========================================================================

class TestLegacyToolsetMap:
    def test_expected_legacy_names(self):
        expected = [
            "web_tools", "terminal_tools", "vision_tools", "moa_tools",
            "image_tools", "skills_tools", "browser_tools", "cronjob_tools",
            "rl_tools", "file_tools", "tts_tools",
        ]
        for name in expected:
            assert name in _LEGACY_TOOLSET_MAP, f"Missing legacy toolset: {name}"

    def test_values_are_lists_of_strings(self):
        for name, tools in _LEGACY_TOOLSET_MAP.items():
            assert isinstance(tools, list), f"{name} is not a list"
            for tool in tools:
                assert isinstance(tool, str), f"{name} contains non-string: {tool}"


# =========================================================================
# Backward-compat wrappers
# =========================================================================

class TestBackwardCompat:
    def test_get_all_tool_names_returns_list(self):
        names = get_all_tool_names()
        assert isinstance(names, list)
        assert len(names) > 0
        # Should contain well-known tools
        assert "terminal" in names
        assert "routed_plan" in names

    def test_get_toolset_for_tool(self):
        result = get_toolset_for_tool("terminal")
        assert result is not None
        assert isinstance(result, str)

    def test_get_toolset_for_routed_plan(self):
        assert get_toolset_for_tool("routed_plan") == "routing"

    def test_get_toolset_for_routing_status(self):
        assert get_toolset_for_tool("routing_status") == "routing"

    def test_get_toolset_for_unknown_tool(self):
        result = get_toolset_for_tool("totally_nonexistent_tool")
        assert result is None

    def test_tool_to_toolset_map(self):
        assert isinstance(TOOL_TO_TOOLSET_MAP, dict)
        assert len(TOOL_TO_TOOLSET_MAP) > 0

    def test_hermes_cli_toolset_includes_routed_exec(self):
        defs = get_tool_definitions(enabled_toolsets=["hermes-cli"], quiet_mode=True)
        names = {item["function"]["name"] for item in defs}
        assert "routed_exec" in names
        assert "routed_plan" in names
        assert "routing_status" in names

    def test_routing_toolset_includes_routed_plan(self):
        defs = get_tool_definitions(enabled_toolsets=["routing"], quiet_mode=True)
        names = {item["function"]["name"] for item in defs}
        assert {"routed_exec", "routed_plan", "routing_status"} <= names
