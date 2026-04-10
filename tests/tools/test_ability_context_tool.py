from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from agent.routing_guard import (
    activate_for_task,
    deactivate_for_task,
)


def test_ability_context_registered_and_toolset_wired():
    import tools.ability_context_tool  # noqa: F401
    from tools.registry import registry
    from toolsets import TOOLSETS, _HERMES_CORE_TOOLS, resolve_toolset

    entry = registry._tools.get("ability_context")
    assert entry is not None
    assert entry.toolset == "ability_context"
    assert entry.is_async is True
    assert "ability_context" in TOOLSETS
    assert "ability_context" in _HERMES_CORE_TOOLS
    assert "ability_context" in resolve_toolset("hermes-cli")


@pytest.mark.asyncio
async def test_auto_visual_lane_uses_cache_on_second_call():
    from tools.ability_context_tool import ability_context_tool

    task_id = "ability-tool-cache"
    activate_for_task(
        task_id,
        session_id="s-ability-tool-cache",
        skills=["routing-layer"],
        user_message="The WebGL canvas looks wrong.",
    )
    try:
        with (
            patch("tools.ability_context_tool._visual_health", return_value={"browser_backend": "local"}),
            patch(
                "tools.visual_context_tool.visual_context_tool",
                new_callable=AsyncMock,
            ) as mock_visual,
        ):
            mock_visual.return_value = json.dumps(
                {
                    "success": True,
                    "visual_summary": "The canvas is washed out.",
                    "screenshot_path": "/tmp/canvas.png",
                    "browser": {"console": {"total_errors": 0}},
                }
            )

            first = json.loads(
                await ability_context_tool(
                    mode="auto",
                    task="The WebGL canvas looks wrong.",
                    url="http://127.0.0.1:3000",
                    phase="pre",
                    task_id=task_id,
                )
            )
            second = json.loads(
                await ability_context_tool(
                    mode="auto",
                    task="The WebGL canvas looks wrong.",
                    url="http://127.0.0.1:3000",
                    phase="pre",
                    task_id=task_id,
                )
            )

        assert first["packets"][0]["summary"] == "The canvas is washed out."
        assert second["packets"][0]["cached"] is True
        assert mock_visual.await_count == 1
    finally:
        deactivate_for_task(task_id)


@pytest.mark.asyncio
async def test_visual_lane_auto_cleanup_for_local_heavy_page():
    from tools.ability_context_tool import ability_context_tool

    task_id = "ability-tool-cleanup"
    activate_for_task(task_id, session_id="s-ability-tool-cleanup", skills=["routing-layer"])
    try:
        with (
            patch("tools.ability_context_tool._visual_health", return_value={"browser_backend": "local"}),
            patch(
                "tools.visual_context_tool.visual_context_tool",
                new_callable=AsyncMock,
            ) as mock_visual,
        ):
            mock_visual.return_value = json.dumps({"success": True, "visual_summary": "Ready."})
            result = json.loads(
                await ability_context_tool(
                    mode="collect",
                    lanes=["visual"],
                    task="Check the WebGL animation.",
                    url="http://127.0.0.1:3000",
                    cleanup_policy="auto",
                    task_id=task_id,
                )
            )

        assert result["packets"][0]["health"]["cleanup_after"] is True
        assert mock_visual.await_args.kwargs["cleanup_after"] is True
    finally:
        deactivate_for_task(task_id)


@pytest.mark.asyncio
async def test_data_logs_lane_requires_existing_artifact():
    from tools.ability_context_tool import ability_context_tool

    result = json.loads(
        await ability_context_tool(
            mode="collect",
            lanes=["data_logs"],
            artifact_path="/tmp/does-not-exist.log",
            task_id="ability-tool-data",
        )
    )

    assert result["packets"][0]["status"] == "unavailable"
    assert "artifact_path" in result["packets"][0]["summary"]


@pytest.mark.asyncio
async def test_external_docs_lane_extracts_official_candidates():
    from tools.ability_context_tool import ability_context_tool

    with (
        patch(
            "tools.web_tools.web_search_tool",
            return_value=json.dumps(
                {
                    "success": True,
                    "data": {
                        "web": [
                            {"title": "Unofficial", "url": "https://example.com/blog"},
                            {"title": "Official docs", "url": "https://docs.example.dev/api"},
                        ]
                    },
                }
            ),
        ) as mock_search,
        patch(
            "tools.web_tools.web_extract_tool",
            new_callable=AsyncMock,
        ) as mock_extract,
    ):
        mock_extract.return_value = json.dumps({"success": True, "content": "Official API details"})
        result = json.loads(
            await ability_context_tool(
                mode="collect",
                lanes=["external_docs"],
                query="example api docs",
                task_id="ability-tool-docs",
            )
        )

    assert result["packets"][0]["status"] == "success"
    assert mock_search.call_args.args[0] == "example api docs"
    assert mock_extract.await_args.args[0][0] == "https://docs.example.dev/api"
    assert "official-doc extract" in result["packets"][0]["findings"][1]["summary"]


@pytest.mark.asyncio
async def test_environment_lane_reports_reusable_managed_server_matches():
    from tools.ability_context_tool import ability_context_tool

    sessions = [
        {
            "session_id": "proc_dev",
            "status": "running",
            "command": "npm run dev -- --host 127.0.0.1 --port 5173",
            "cwd": "/tmp/demo",
        },
        {
            "session_id": "proc_other",
            "status": "running",
            "command": "npm run dev -- --host 127.0.0.1 --port 3000",
            "cwd": "/tmp/other",
        },
    ]
    with patch("tools.process_registry.process_registry.list_sessions", return_value=sessions):
        result = json.loads(
            await ability_context_tool(
                mode="collect",
                lanes=["environment"],
                workdir="/tmp/demo",
                url="http://127.0.0.1:5173",
                task_id="ability-tool-env",
            )
        )

    packet = result["packets"][0]
    assert packet["status"] == "success"
    registry_finding = packet["findings"][0]["message"]
    assert "proc_dev" in registry_finding
    assert "proc_other" not in registry_finding
