import json
from unittest.mock import AsyncMock, patch

import pytest


def test_visual_context_registered():
    import tools.visual_context_tool  # noqa: F401
    from tools.registry import registry

    entry = registry._tools.get("visual_context")
    assert entry is not None
    assert entry.toolset == "visual_context"
    assert entry.is_async is True
    assert callable(entry.handler)


def test_visual_context_toolset_wiring():
    from toolsets import TOOLSETS, _HERMES_CORE_TOOLS, resolve_toolset

    assert "visual_context" in TOOLSETS
    assert "visual_context" in TOOLSETS["visual_context"]["tools"]
    assert "visual_context" in _HERMES_CORE_TOOLS
    assert "visual_context" in resolve_toolset("hermes-cli")


@pytest.mark.asyncio
async def test_image_source_calls_vision_analyze():
    from tools.visual_context_tool import visual_context_tool

    with patch(
        "tools.visual_context_tool.vision_analyze_tool",
        new_callable=AsyncMock,
    ) as mock_vision, patch(
        "tools.visual_context_tool._vision_capability_error",
        return_value=None,
    ):
        mock_vision.return_value = json.dumps(
            {"success": True, "analysis": "The screenshot has a clipped button."}
        )

        result = json.loads(
            await visual_context_tool(
                "image",
                "Is anything visually broken?",
                image_url="/tmp/screen.png",
            )
        )

    assert result["success"] is True
    assert result["source"] == "image"
    assert result["image_url"] == "/tmp/screen.png"
    assert result["visual_summary"] == "The screenshot has a clipped button."
    mock_vision.assert_awaited_once()
    assert "visual scout" in mock_vision.await_args.args[1]


@pytest.mark.asyncio
async def test_image_source_requires_image_url():
    from tools.visual_context_tool import visual_context_tool

    result = json.loads(await visual_context_tool("image", "Describe it"))

    assert result["success"] is False
    assert "image_url is required" in result["error"]


@pytest.mark.asyncio
async def test_browser_source_collects_visual_snapshot_and_console():
    from tools.visual_context_tool import visual_context_tool

    with (
        patch("tools.visual_context_tool._vision_capability_error", return_value=None),
        patch("tools.browser_tool.check_browser_requirements", return_value=True),
        patch(
            "tools.browser_tool.browser_navigate",
            return_value=json.dumps({"success": True, "url": "http://127.0.0.1:3000"}),
        ) as mock_navigate,
        patch(
            "tools.browser_tool.browser_snapshot",
            return_value=json.dumps({"success": True, "content": "button @e1"}),
        ) as mock_snapshot,
        patch(
            "tools.browser_tool.browser_console",
            return_value=json.dumps({"success": True, "total_errors": 0}),
        ) as mock_console,
        patch(
            "tools.browser_tool.browser_vision",
            return_value=json.dumps(
                {
                    "success": True,
                    "analysis": "The layout is readable.",
                    "screenshot_path": "/tmp/browser.png",
                }
            ),
        ) as mock_browser_vision,
        patch("tools.browser_tool.browser_close") as mock_browser_close,
    ):
        mock_browser_close.return_value = json.dumps({"success": True, "closed": True})
        result = json.loads(
            await visual_context_tool(
                "browser",
                "Does the page look ready?",
                url="http://127.0.0.1:3000",
                annotate=True,
                task_id="task-1",
                user_task="visual QA",
            )
        )

    assert result["success"] is True
    assert result["source"] == "browser"
    assert result["visual_summary"] == "The layout is readable."
    assert result["screenshot_path"] == "/tmp/browser.png"
    assert result["browser"]["snapshot"]["content"] == "button @e1"
    assert result["browser"]["console"]["total_errors"] == 0
    mock_navigate.assert_called_once_with("http://127.0.0.1:3000", task_id="task-1")
    mock_snapshot.assert_called_once_with(full=False, task_id="task-1", user_task="visual QA")
    mock_console.assert_called_once_with(clear=False, task_id="task-1")
    assert mock_browser_vision.call_args.kwargs["annotate"] is True
    mock_browser_close.assert_not_called()


@pytest.mark.asyncio
async def test_browser_source_can_cleanup_after_capture():
    from tools.visual_context_tool import visual_context_tool

    with (
        patch("tools.visual_context_tool._vision_capability_error", return_value=None),
        patch("tools.browser_tool.check_browser_requirements", return_value=True),
        patch(
            "tools.browser_tool.browser_navigate",
            return_value=json.dumps({"success": True, "url": "http://127.0.0.1:3000"}),
        ),
        patch(
            "tools.browser_tool.browser_snapshot",
            return_value=json.dumps({"success": True, "content": "canvas"}),
        ),
        patch(
            "tools.browser_tool.browser_console",
            return_value=json.dumps({"success": True, "total_errors": 0}),
        ),
        patch(
            "tools.browser_tool.browser_vision",
            return_value=json.dumps({"success": True, "analysis": "Looks stable."}),
        ),
        patch("tools.browser_tool.browser_close") as mock_browser_close,
    ):
        mock_browser_close.return_value = json.dumps({"success": True, "closed": True})
        result = json.loads(
            await visual_context_tool(
                "browser",
                "Does the page look ready?",
                url="http://127.0.0.1:3000",
                cleanup_after=True,
                task_id="task-visual-heavy",
            )
        )

    assert result["success"] is True
    assert result["browser"]["cleanup"]["closed"] is True
    mock_browser_close.assert_called_once_with(task_id="task-visual-heavy")


@pytest.mark.asyncio
async def test_browser_source_stops_when_navigation_fails():
    from tools.visual_context_tool import visual_context_tool

    with (
        patch("tools.visual_context_tool._vision_capability_error", return_value=None),
        patch("tools.browser_tool.check_browser_requirements", return_value=True),
        patch(
            "tools.browser_tool.browser_navigate",
            return_value=json.dumps(
                {
                    "success": False,
                    "error": "Blocked by browser SSRF protection: use a local browser backend",
                }
            ),
        ),
        patch("tools.browser_tool.browser_snapshot") as mock_snapshot,
        patch("tools.browser_tool.browser_console") as mock_console,
        patch("tools.browser_tool.browser_vision") as mock_browser_vision,
        patch("tools.browser_tool.browser_close") as mock_browser_close,
    ):
        mock_browser_close.return_value = json.dumps({"success": True, "closed": True})
        result = json.loads(
            await visual_context_tool(
                "browser",
                "Does the page look ready?",
                url="http://127.0.0.1:3000",
                cleanup_after=True,
            )
        )

    assert result["success"] is False
    assert "local browser backend" in result["error"]
    assert result["browser"]["navigation"]["success"] is False
    assert result["browser"]["cleanup"]["closed"] is True
    mock_snapshot.assert_not_called()
    mock_console.assert_not_called()
    mock_browser_vision.assert_not_called()
    mock_browser_close.assert_called_once()


@pytest.mark.asyncio
async def test_known_text_only_vision_model_is_rejected():
    from tools.visual_context_tool import visual_context_tool

    with patch(
        "tools.visual_context_tool._vision_capability_error",
        return_value="configured model is text-only",
    ):
        result = json.loads(
            await visual_context_tool(
                "image",
                "What is visible?",
                image_url="/tmp/screen.png",
            )
        )

    assert result["success"] is False
    assert result["source"] == "image"
    assert "text-only" in result["error"]


@pytest.mark.asyncio
async def test_browser_source_reports_missing_browser_requirements():
    from tools.visual_context_tool import visual_context_tool

    with patch("tools.browser_tool.check_browser_requirements", return_value=False):
        result = json.loads(
            await visual_context_tool("browser", "What is visible?")
        )

    assert result["success"] is False
    assert "browser requirements" in result["error"]


@pytest.mark.asyncio
async def test_invalid_source_is_rejected():
    from tools.visual_context_tool import visual_context_tool

    result = json.loads(await visual_context_tool("video", "Describe it"))

    assert result["success"] is False
    assert "source must be" in result["error"]
