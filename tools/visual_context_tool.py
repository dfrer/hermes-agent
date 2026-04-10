"""Read-only visual context helper for UI, web, and image decisions."""

from __future__ import annotations

import json
from typing import Any, Awaitable, Dict, Optional

from tools.registry import registry
from tools.vision_tools import check_vision_requirements, vision_analyze_tool


_MAX_EMBEDDED_TEXT_CHARS = 6000


def _json_or_text(value: str) -> Any:
    text = str(value or "")
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def _truncate_nested_text(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_EMBEDDED_TEXT_CHARS:
        omitted = len(value) - _MAX_EMBEDDED_TEXT_CHARS
        return f"{value[:_MAX_EMBEDDED_TEXT_CHARS]}\n\n[truncated {omitted} chars]"
    if isinstance(value, list):
        return [_truncate_nested_text(item) for item in value]
    if isinstance(value, dict):
        return {key: _truncate_nested_text(item) for key, item in value.items()}
    return value


def _analysis_from_payload(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("analysis") or payload.get("error") or "").strip()
    return str(payload or "").strip()


def _success_from_payload(payload: Any) -> bool:
    return bool(isinstance(payload, dict) and payload.get("success") is True)


def _vision_capability_error() -> Optional[str]:
    try:
        from agent.auxiliary_client import resolve_vision_provider_client
        from agent.models_dev import get_model_info, get_model_info_any_provider

        provider, client, model = resolve_vision_provider_client()
        if client is None or not model:
            return None
        info = get_model_info(provider or "", model) or get_model_info_any_provider(model)
        if info is not None and not info.supports_vision():
            return (
                f"Configured auxiliary vision model '{model}' via '{provider}' is not "
                "marked vision-capable in the model cache. Choose a model with image "
                "input support or leave auxiliary.vision.model empty for the default."
            )
    except Exception:
        return None
    return None


VISUAL_CONTEXT_SCHEMA = {
    "name": "visual_context",
    "description": (
        "Gather read-only visual context for better decisions about UI, web pages, "
        "screenshots, images, layout, design, canvas output, or visual QA. Use this "
        "as the main/local lane's visual scout before planning, routing, or "
        "verification when text/DOM context may miss important visual details. "
        "This tool does not edit files or implement code."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "enum": ["browser", "image"],
                "description": (
                    "Use 'browser' to inspect the current or supplied page in a browser, "
                    "or 'image' to inspect an image URL/local image path."
                ),
            },
            "question": {
                "type": "string",
                "description": "Specific visual question to answer.",
            },
            "url": {
                "type": "string",
                "description": "Optional URL to navigate to before browser visual inspection.",
            },
            "image_url": {
                "type": "string",
                "description": "Image URL or local image path when source is 'image'.",
            },
            "annotate": {
                "type": "boolean",
                "default": True,
                "description": (
                    "For browser source, overlay numbered labels on interactive elements "
                    "so the result can support spatial reasoning."
                ),
            },
            "include_snapshot": {
                "type": "boolean",
                "default": True,
                "description": "For browser source, include the accessibility snapshot.",
            },
            "include_console": {
                "type": "boolean",
                "default": True,
                "description": "For browser source, include current console/errors.",
            },
        },
        "required": ["source", "question"],
    },
}


async def visual_context_tool(
    source: str,
    question: str,
    *,
    url: str = "",
    image_url: str = "",
    annotate: bool = True,
    include_snapshot: bool = True,
    include_console: bool = True,
    task_id: Optional[str] = None,
    user_task: Optional[str] = None,
) -> str:
    """Collect a structured visual context packet without mutating the workspace."""
    normalized_source = str(source or "").strip().lower()
    clean_question = str(question or "").strip()
    warnings: list[str] = []

    if normalized_source not in {"browser", "image"}:
        return json.dumps(
            {
                "success": False,
                "source": normalized_source or source,
                "error": "source must be 'browser' or 'image'",
            },
            ensure_ascii=False,
        )
    if not clean_question:
        return json.dumps(
            {
                "success": False,
                "source": normalized_source,
                "error": "question is required",
            },
            ensure_ascii=False,
        )

    if normalized_source == "image":
        clean_image_url = str(image_url or url or "").strip()
        if not clean_image_url:
            return json.dumps(
                {
                    "success": False,
                    "source": "image",
                    "error": "image_url is required when source is 'image'",
                },
                ensure_ascii=False,
            )

        capability_error = _vision_capability_error()
        if capability_error:
            return json.dumps(
                {
                    "success": False,
                    "source": "image",
                    "error": capability_error,
                },
                ensure_ascii=False,
            )

        prompt = (
            "You are a visual scout helping the main agent make a better decision. "
            "Fully describe the image, then answer this specific question:\n\n"
            f"{clean_question}"
        )
        payload = _json_or_text(await vision_analyze_tool(clean_image_url, prompt))
        payload = _truncate_nested_text(payload)
        return json.dumps(
            {
                "success": _success_from_payload(payload),
                "source": "image",
                "question": clean_question,
                "image_url": clean_image_url,
                "visual_summary": _analysis_from_payload(payload),
                "image": payload,
                "warnings": warnings,
            },
            ensure_ascii=False,
        )

    from tools.browser_tool import (
        browser_console,
        browser_navigate,
        browser_snapshot,
        browser_vision,
        check_browser_requirements,
    )

    if not check_browser_requirements():
        return json.dumps(
            {
                "success": False,
                "source": "browser",
                "error": "browser requirements are not available",
            },
            ensure_ascii=False,
        )

    capability_error = _vision_capability_error()
    if capability_error:
        return json.dumps(
            {
                "success": False,
                "source": "browser",
                "error": capability_error,
            },
            ensure_ascii=False,
        )

    browser_details: dict[str, Any] = {}
    clean_url = str(url or "").strip()
    if clean_url:
        navigation_payload = _json_or_text(
            browser_navigate(clean_url, task_id=task_id)
        )
        browser_details["navigation"] = navigation_payload
        if isinstance(navigation_payload, dict) and navigation_payload.get("success") is False:
            return json.dumps(
                {
                    "success": False,
                    "source": "browser",
                    "question": clean_question,
                    "url": clean_url,
                    "error": str(navigation_payload.get("error") or "browser navigation failed"),
                    "browser": _truncate_nested_text(browser_details),
                    "warnings": warnings,
                },
                ensure_ascii=False,
            )

    if include_snapshot:
        browser_details["snapshot"] = _json_or_text(
            browser_snapshot(full=False, task_id=task_id, user_task=user_task)
        )

    if include_console:
        browser_details["console"] = _json_or_text(
            browser_console(clear=False, task_id=task_id)
        )

    browser_prompt = (
        "You are a visual scout helping the main agent make a better decision. "
        "Inspect the browser screenshot and answer the question directly. "
        "Call out layout, visual quality, broken elements, occlusion, responsive "
        "issues, or suspicious UI state when relevant.\n\n"
        f"Question: {clean_question}"
    )
    vision_payload = _json_or_text(
        browser_vision(browser_prompt, annotate=bool(annotate), task_id=task_id)
    )
    vision_payload = _truncate_nested_text(vision_payload)
    browser_details = _truncate_nested_text(browser_details)

    result: dict[str, Any] = {
        "success": _success_from_payload(vision_payload),
        "source": "browser",
        "question": clean_question,
        "url": clean_url,
        "annotated": bool(annotate),
        "visual_summary": _analysis_from_payload(vision_payload),
        "browser": browser_details,
        "vision": vision_payload,
        "warnings": warnings,
    }
    if isinstance(vision_payload, dict) and vision_payload.get("screenshot_path"):
        result["screenshot_path"] = vision_payload["screenshot_path"]
    return json.dumps(result, ensure_ascii=False)


def _handle_visual_context(args: Dict[str, Any], **kw: Any) -> Awaitable[str]:
    return visual_context_tool(
        args.get("source", ""),
        args.get("question", ""),
        url=args.get("url", ""),
        image_url=args.get("image_url", ""),
        annotate=args.get("annotate", True),
        include_snapshot=args.get("include_snapshot", True),
        include_console=args.get("include_console", True),
        task_id=kw.get("task_id"),
        user_task=kw.get("user_task"),
    )


registry.register(
    name="visual_context",
    toolset="visual_context",
    schema=VISUAL_CONTEXT_SCHEMA,
    handler=_handle_visual_context,
    check_fn=check_vision_requirements,
    is_async=True,
    emoji="👁️",
)
