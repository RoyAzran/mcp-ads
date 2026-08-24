"""The REST surface the NPX CLI talks to, single-user edition.

Same three routes as the hosted product -- /api/cli/manifest, list_actions,
dispatch -- so `npx @marketingmcp/cli --base-url http://127.0.0.1:8000` works
against this server unmodified. What changed is everything around the routes:
no key table, no plans, no usage metering. Auth is one shared secret from the
environment, and it is optional because the default bind is loopback -- the
secret exists for the operator who reverse-proxies this out to their own team.
"""
from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from auth import current_user_ctx

logger = logging.getLogger(__name__)

router = APIRouter(tags=["cli"])


def _check_key(request: Request) -> None:
    import os

    expected = os.environ.get("MCP_ADS_API_KEY", "").strip()
    if not expected:
        return  # loopback deployment, nothing configured -- open by design
    supplied = request.headers.get("Authorization", "")
    supplied = supplied[7:] if supplied.startswith("Bearer ") else supplied
    if not secrets.compare_digest(supplied.strip(), expected):
        raise HTTPException(status_code=401, detail="Invalid API key.")


@router.get("/api/cli/manifest")
def cli_manifest(request: Request) -> dict:
    _check_key(request)
    from tool_registry import get_categories

    return {
        "version": 1,
        "user": {"id": "self", "email": ""},
        "categories": get_categories(),
        "discovery_tool": {
            "name": "list_actions",
            "description": (
                "List actions available within a category. Call this BEFORE "
                "<category>_action when you don't know the exact action name "
                "or its parameters. Returns dispatch name, human title, description, "
                "parameter schema, and hints for IDs, safe paused writes, and "
                "image/video uploads. Use the optional `search` arg to narrow the "
                "result (e.g. search='budget' inside google_ads_action)."
            ),
        },
    }


class ListActionsBody(BaseModel):
    category: str
    search: str = ""
    limit: int = 200
    offset: int = 0


@router.post("/api/cli/list_actions")
def cli_list_actions(body: ListActionsBody, request: Request) -> dict:
    _check_key(request)
    from tool_registry import list_actions

    try:
        return list_actions(body.category, body.search, body.limit, body.offset)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


class DispatchBody(BaseModel):
    category: str = Field(..., description="e.g. 'google_ads_action'")
    action: str = Field(..., description="Underlying tool name, e.g. 'google_ads_list_customers'")
    params: dict[str, Any] = Field(default_factory=dict)


@router.post("/api/cli/dispatch")
async def cli_dispatch(body: DispatchBody, request: Request) -> dict:
    _check_key(request)
    from tool_registry import dispatch, find_action

    try:
        find_action(body.category, body.action)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # current_user_ctx's default is already the operator; nothing to set.
    try:
        result = await dispatch(body.category, body.action, body.params)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - surface tool failures as clean 500s
        logger.exception("CLI dispatch failed: %s.%s", body.category, body.action)
        raise HTTPException(status_code=500, detail=str(exc)[:500])

    return {"action": body.action, "category": body.category, "result": _serialize(result)}


def _serialize(result: Any) -> Any:
    """FastMCP Tool.run returns a CallToolResult-like object; surface its JSON
    content directly so the Node CLI passes it through to the agent."""
    content = getattr(result, "content", None)
    if content is None:
        return result
    parts = []
    for item in content:
        text = getattr(item, "text", None)
        parts.append(text if text is not None else str(item))
    return parts[0] if len(parts) == 1 else parts
