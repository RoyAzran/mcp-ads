"""Site selection tools for WordPress.

A user may connect several WordPress sites. When they drive tools from /manage
the workspace ExecutionContext already pins one, but over plain MCP (Claude
talking straight to /mcp-slim) there is no such context, so the agent needs a
way to see the connected sites and choose one.

Selection is persisted on the user row rather than held in a ContextVar: the
streamable-HTTP transport is stateless and requests are served by any Cloud Run
instance, so an in-memory choice would not survive the next call.
"""
from __future__ import annotations

import json

from auth import current_connection_token_ctx, current_user_ctx
from mcp_instance import mcp

from .client import _load_user_wordpress_connections, _wp, resolve_wp_creds


def _json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _public(site: dict, selected_id: str | None) -> dict:
    """Strip credentials -- only ever return what is safe to show an agent."""
    return {
        "connection_id": site["connection_id"],
        "label": site["label"],
        "site_url": site["site_url"],
        "health": site.get("health", "connected"),
        "is_selected": site["connection_id"] == selected_id,
    }


@mcp.tool()
def wordpress_list_sites() -> str:
    """List the WordPress sites connected to this account, with their ids and which one is selected."""
    user = current_user_ctx.get(None)
    if user is None:
        return _json({"error": "No authenticated user in request context."})
    try:
        sites = _load_user_wordpress_connections(user.id)
    except Exception as exc:  # noqa: BLE001
        return _json({"error": str(exc)})
    selected_id = getattr(user, "selected_wordpress_connection_id", None)
    return _json({
        "sites": [_public(site, selected_id) for site in sites],
        "total": len(sites),
    })


@mcp.tool()
def wordpress_select_site(connection_id: str) -> str:
    """Choose which connected WordPress site later tools act on. Takes a connection_id from wordpress_list_sites."""
    user = current_user_ctx.get(None)
    if user is None:
        return _json({"error": "No authenticated user in request context."})

    # When the caller came in through a workspace connection the site is already
    # fixed by that connection; silently switching underneath them would be wrong.
    if current_connection_token_ctx.get(None):
        creds = resolve_wp_creds()
        return _json({
            "selected": creds.get("site_url"),
            "note": "This session is pinned to a workspace connection; selection is unchanged.",
        })

    target = (connection_id or "").strip()
    sites = _load_user_wordpress_connections(user.id)
    match = next((s for s in sites if s["connection_id"] == target), None)
    if match is None:
        return _json({
            "error": f"No connected WordPress site with id {target!r}.",
            "available": [_public(s, None) for s in sites],
        })

    from credentials import provider

    # Persists the selection and keeps the in-request principal consistent;
    # both halves live in the credential backend.
    provider().select_wordpress_connection(match["connection_id"])
    return _json({"selected": match["site_url"], "connection_id": match["connection_id"]})


@mcp.tool()
def wordpress_current_site() -> str:
    """Show which WordPress site the current session will act on, and why that one."""
    if current_connection_token_ctx.get(None):
        source = "workspace_connection"
    else:
        user = current_user_ctx.get(None)
        if user is None:
            return _json({"error": "No authenticated user in request context."})
        sites = _load_user_wordpress_connections(user.id)
        if not sites:
            source = "none"
        elif len(sites) == 1:
            source = "single_connection"
        else:
            source = "user_selection"
    try:
        creds = resolve_wp_creds()
    except Exception as exc:  # noqa: BLE001
        return _json({"error": str(exc), "source": source})
    return _json({
        "site_url": creds.get("site_url"),
        "connection_id": creds.get("connection_id"),
        "label": creds.get("label"),
        "source": source,
    })


@mcp.tool()
def wordpress_site_info() -> str:
    """Get the connected WordPress site's name, description, and URL."""
    try:
        creds = resolve_wp_creds()
        # The discovery root is unauthenticated and cheap; it also confirms the
        # REST API is actually reachable.
        info = _wp().get("/")
    except Exception as exc:  # noqa: BLE001
        return _json({"error": str(exc)})
    site_url = creds.get("site_url", "")
    name = ""
    description = ""
    if isinstance(info, dict):
        name = str(info.get("name") or "")
        description = str(info.get("description") or "")
    # Shaped as {"sites": [...]} so agency_os_service._parse_discovery_result can
    # turn a connection into exactly one PlatformAccount with no special-casing.
    return _json({
        "sites": [{"id": site_url, "name": name or site_url, "description": description}],
        "site_url": site_url,
        "name": name,
        "description": description,
    })
