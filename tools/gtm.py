"""
Google Tag Manager tools — remote server edition.
Auth pattern: per-user OAuth refresh token via current_user_ctx (same as ga4.py).
~70 tools covering: accounts, containers, workspaces, tags, triggers, variables,
versions, builtin variables, folders, publishing, smart builders, and audit.
"""
import json
import os
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from mcp_instance import mcp
from credentials import connect_hint
from auth import current_user_ctx
from permissions import require_editor

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
GTM_SCOPES = [
    "https://www.googleapis.com/auth/tagmanager.edit.containers",
    "https://www.googleapis.com/auth/tagmanager.readonly",
    "https://www.googleapis.com/auth/tagmanager.manage.accounts",
    "https://www.googleapis.com/auth/tagmanager.publish",
    "https://www.googleapis.com/auth/tagmanager.edit.containerversions",
]


# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------

def _creds() -> Credentials:
    user = current_user_ctx.get(None)
    if user is None:
        raise RuntimeError("Not authenticated.")
    from credentials import provider
    rt = provider().google_token("gtm")
    if not rt:
        raise RuntimeError("GTM account not connected. Connect your Google account via " + connect_hint() + ".")
    creds = Credentials(
        token=None,
        refresh_token=rt,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        scopes=GTM_SCOPES,
    )
    creds.refresh(Request())
    return creds


def _svc():
    return build("tagmanager", "v2", credentials=_creds())


def _handle(request) -> dict:
    try:
        result = request.execute()
        return result if result is not None else {}
    except HttpError as e:
        body = json.loads(e.content.decode())
        raise RuntimeError(body.get("error", {}).get("message", str(e))) from e


# ---------------------------------------------------------------------------
# Parameter helpers for smart builders
# ---------------------------------------------------------------------------

def _pt(key, val): return {"type": "template", "key": key, "value": str(val)}
def _pb(key, val): return {"type": "boolean", "key": key, "value": str(val).lower()}
def _pi(key, val): return {"type": "integer", "key": key, "value": str(val)}
def _pref(key, val): return {"type": "tagReference", "key": key, "value": val}
def _plist(key, items): return {"type": "list", "key": key, "list": items}
def _pmap(pairs): return {"type": "map", "map": [_pt(k, v) for k, v in pairs.items()]}
def _cond(type_, arg0, arg1):
    return {"type": type_, "parameter": [_pt("arg0", arg0), _pt("arg1", arg1)]}


# ===========================================================================
# ACCOUNTS
# ===========================================================================

@mcp.tool()
def gtm_list_accounts() -> str:
    """List all Google Tag Manager accounts accessible to the authenticated user."""
    try:
        result = _handle(_svc().accounts().list())
        accounts = result.get("account", [])
        return json.dumps([{"account_id": a["accountId"], "name": a["name"],
                            "path": a["path"]} for a in accounts])
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_get_account(account_id: str) -> str:
    """Get details of a specific GTM account.

    Args:
        account_id: The GTM account ID.
    """
    try:
        result = _handle(_svc().accounts().get(path=f"accounts/{account_id}"))
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ===========================================================================
# CONTAINERS
# ===========================================================================

@mcp.tool()
def gtm_list_containers(account_id: str) -> str:
    """List all containers in a GTM account.

    Args:
        account_id: The GTM account ID.
    """
    try:
        result = _handle(_svc().accounts().containers().list(parent=f"accounts/{account_id}"))
        containers = result.get("container", [])
        return json.dumps([{
            "container_id": c["containerId"],
            "name": c["name"],
            "public_id": c.get("publicId", ""),
            "usage_context": c.get("usageContext", []),
            "path": c["path"],
        } for c in containers])
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_get_container(account_id: str, container_id: str) -> str:
    """Get details of a specific GTM container.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
    """
    try:
        result = _handle(_svc().accounts().containers().get(
            path=f"accounts/{account_id}/containers/{container_id}"))
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_create_container(account_id: str, name: str, usage_context: str = "web") -> str:
    """Create a new GTM container in an account.

    Args:
        account_id: The GTM account ID.
        name: Display name for the new container.
        usage_context: web | android | ios | amp | server (default: web).
    """
    require_editor()
    try:
        body = {"name": name, "usageContext": [usage_context]}
        result = _handle(_svc().accounts().containers().create(
            parent=f"accounts/{account_id}", body=body))
        return json.dumps({"container_id": result["containerId"], "name": result["name"],
                           "public_id": result.get("publicId", ""), "path": result["path"]})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_get_container_snippet(account_id: str, container_id: str) -> str:
    """Get the GTM installation snippet for a container (the script tag to paste on your site).

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
    """
    try:
        result = _handle(_svc().accounts().containers().snippet(
            path=f"accounts/{account_id}/containers/{container_id}"))
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ===========================================================================
# WORKSPACES
# ===========================================================================

@mcp.tool()
def gtm_list_workspaces(account_id: str, container_id: str) -> str:
    """List all workspaces in a GTM container.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
    """
    try:
        result = _handle(_svc().accounts().containers().workspaces().list(
            parent=f"accounts/{account_id}/containers/{container_id}"))
        workspaces = result.get("workspace", [])
        return json.dumps([{"workspace_id": w["workspaceId"], "name": w["name"],
                            "description": w.get("description", ""),
                            "path": w["path"]} for w in workspaces])
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_get_workspace(account_id: str, container_id: str, workspace_id: str) -> str:
    """Get details of a specific GTM workspace.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
    """
    try:
        result = _handle(_svc().accounts().containers().workspaces().get(
            path=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}"))
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_create_workspace(account_id: str, container_id: str, name: str, description: str = "") -> str:
    """Create a new workspace in a GTM container.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        name: Workspace name.
        description: Optional workspace description.
    """
    require_editor()
    try:
        body = {"name": name, "description": description}
        result = _handle(_svc().accounts().containers().workspaces().create(
            parent=f"accounts/{account_id}/containers/{container_id}", body=body))
        return json.dumps({"workspace_id": result["workspaceId"], "name": result["name"],
                           "path": result["path"]})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_get_workspace_status(account_id: str, container_id: str, workspace_id: str) -> str:
    """Get the status of a workspace — shows changed entities (tags, triggers, variables).

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
    """
    try:
        result = _handle(_svc().accounts().containers().workspaces().getStatus(
            path=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}"))
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_sync_workspace(account_id: str, container_id: str, workspace_id: str) -> str:
    """Sync a workspace with the latest container version. Shows merge conflicts if any.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
    """
    require_editor()
    try:
        result = _handle(_svc().accounts().containers().workspaces().sync(
            path=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}"))
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e)})


# ===========================================================================
# TAGS
# ===========================================================================

@mcp.tool()
def gtm_list_tags(account_id: str, container_id: str, workspace_id: str) -> str:
    """List all tags in a GTM workspace.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
    """
    try:
        result = _handle(_svc().accounts().containers().workspaces().tags().list(
            parent=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}"))
        tags = result.get("tag", [])
        return json.dumps([{
            "tag_id": t["tagId"],
            "name": t["name"],
            "type": t.get("type", ""),
            "status": t.get("tagFiringOption", ""),
            "paused": t.get("paused", False),
            "firing_trigger_ids": t.get("firingTriggerId", []),
        } for t in tags])
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_get_tag(account_id: str, container_id: str, workspace_id: str, tag_id: str) -> str:
    """Get full details of a specific GTM tag.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        tag_id: The tag ID.
    """
    try:
        result = _handle(_svc().accounts().containers().workspaces().tags().get(
            path=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}/tags/{tag_id}"))
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_create_tag(account_id: str, container_id: str, workspace_id: str, tag_body: str) -> str:
    """Create a new tag in a GTM workspace.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        tag_body: JSON string with the tag definition. Must include 'name', 'type', and optionally 'parameter', 'firingTriggerId'.
    """
    require_editor()
    try:
        body = json.loads(tag_body)
        result = _handle(_svc().accounts().containers().workspaces().tags().create(
            parent=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}",
            body=body))
        return json.dumps({"tag_id": result["tagId"], "name": result["name"], "path": result["path"]})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_update_tag(account_id: str, container_id: str, workspace_id: str,
                   tag_id: str, tag_body: str) -> str:
    """Update an existing GTM tag.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        tag_id: The tag ID to update.
        tag_body: JSON string with the updated tag definition.
    """
    require_editor()
    try:
        body = json.loads(tag_body)
        result = _handle(_svc().accounts().containers().workspaces().tags().update(
            path=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}/tags/{tag_id}",
            body=body))
        return json.dumps({"success": True, "tag_id": result["tagId"], "name": result["name"]})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_delete_tag(account_id: str, container_id: str, workspace_id: str, tag_id: str) -> str:
    """Delete a tag from a GTM workspace.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        tag_id: The tag ID to delete.
    """
    require_editor()
    try:
        _handle(_svc().accounts().containers().workspaces().tags().delete(
            path=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}/tags/{tag_id}"))
        return json.dumps({"success": True, "deleted_tag_id": tag_id})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_pause_tag(account_id: str, container_id: str, workspace_id: str, tag_id: str) -> str:
    """Pause a GTM tag so it stops firing without deleting it.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        tag_id: The tag ID to pause.
    """
    require_editor()
    try:
        svc = _svc()
        tag_path = f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}/tags/{tag_id}"
        tag = _handle(svc.accounts().containers().workspaces().tags().get(path=tag_path))
        tag["paused"] = True
        result = _handle(svc.accounts().containers().workspaces().tags().update(
            path=tag_path, body=tag))
        return json.dumps({"success": True, "tag_id": tag_id, "name": result["name"], "paused": True})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_unpause_tag(account_id: str, container_id: str, workspace_id: str, tag_id: str) -> str:
    """Resume a paused GTM tag so it starts firing again.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        tag_id: The tag ID to unpause.
    """
    require_editor()
    try:
        svc = _svc()
        tag_path = f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}/tags/{tag_id}"
        tag = _handle(svc.accounts().containers().workspaces().tags().get(path=tag_path))
        tag["paused"] = False
        result = _handle(svc.accounts().containers().workspaces().tags().update(
            path=tag_path, body=tag))
        return json.dumps({"success": True, "tag_id": tag_id, "name": result["name"], "paused": False})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ===========================================================================
# TRIGGERS
# ===========================================================================

@mcp.tool()
def gtm_list_triggers(account_id: str, container_id: str, workspace_id: str) -> str:
    """List all triggers in a GTM workspace.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
    """
    try:
        result = _handle(_svc().accounts().containers().workspaces().triggers().list(
            parent=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}"))
        triggers = result.get("trigger", [])
        return json.dumps([{
            "trigger_id": t["triggerId"],
            "name": t["name"],
            "type": t.get("type", ""),
        } for t in triggers])
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_get_trigger(account_id: str, container_id: str, workspace_id: str, trigger_id: str) -> str:
    """Get full details of a specific GTM trigger.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        trigger_id: The trigger ID.
    """
    try:
        result = _handle(_svc().accounts().containers().workspaces().triggers().get(
            path=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}/triggers/{trigger_id}"))
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_create_trigger(account_id: str, container_id: str, workspace_id: str, trigger_body: str) -> str:
    """Create a new trigger in a GTM workspace.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        trigger_body: JSON string with the trigger definition. Must include 'name' and 'type'.
    """
    require_editor()
    try:
        body = json.loads(trigger_body)
        result = _handle(_svc().accounts().containers().workspaces().triggers().create(
            parent=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}",
            body=body))
        return json.dumps({"trigger_id": result["triggerId"], "name": result["name"],
                           "path": result["path"]})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_update_trigger(account_id: str, container_id: str, workspace_id: str,
                       trigger_id: str, trigger_body: str) -> str:
    """Update an existing GTM trigger.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        trigger_id: The trigger ID to update.
        trigger_body: JSON string with the updated trigger definition.
    """
    require_editor()
    try:
        body = json.loads(trigger_body)
        result = _handle(_svc().accounts().containers().workspaces().triggers().update(
            path=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}/triggers/{trigger_id}",
            body=body))
        return json.dumps({"success": True, "trigger_id": result["triggerId"], "name": result["name"]})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_delete_trigger(account_id: str, container_id: str, workspace_id: str, trigger_id: str) -> str:
    """Delete a trigger from a GTM workspace.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        trigger_id: The trigger ID to delete.
    """
    require_editor()
    try:
        _handle(_svc().accounts().containers().workspaces().triggers().delete(
            path=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}/triggers/{trigger_id}"))
        return json.dumps({"success": True, "deleted_trigger_id": trigger_id})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ===========================================================================
# VARIABLES
# ===========================================================================

@mcp.tool()
def gtm_list_variables(account_id: str, container_id: str, workspace_id: str) -> str:
    """List all user-defined variables in a GTM workspace.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
    """
    try:
        result = _handle(_svc().accounts().containers().workspaces().variables().list(
            parent=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}"))
        variables = result.get("variable", [])
        return json.dumps([{
            "variable_id": v["variableId"],
            "name": v["name"],
            "type": v.get("type", ""),
        } for v in variables])
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_get_variable(account_id: str, container_id: str, workspace_id: str, variable_id: str) -> str:
    """Get full details of a specific GTM variable.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        variable_id: The variable ID.
    """
    try:
        result = _handle(_svc().accounts().containers().workspaces().variables().get(
            path=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}/variables/{variable_id}"))
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_create_variable(account_id: str, container_id: str, workspace_id: str, variable_body: str) -> str:
    """Create a new variable in a GTM workspace.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        variable_body: JSON string with the variable definition. Must include 'name' and 'type'.
    """
    require_editor()
    try:
        body = json.loads(variable_body)
        result = _handle(_svc().accounts().containers().workspaces().variables().create(
            parent=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}",
            body=body))
        return json.dumps({"variable_id": result["variableId"], "name": result["name"],
                           "path": result["path"]})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_update_variable(account_id: str, container_id: str, workspace_id: str,
                        variable_id: str, variable_body: str) -> str:
    """Update an existing GTM variable.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        variable_id: The variable ID to update.
        variable_body: JSON string with the updated variable definition.
    """
    require_editor()
    try:
        body = json.loads(variable_body)
        result = _handle(_svc().accounts().containers().workspaces().variables().update(
            path=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}/variables/{variable_id}",
            body=body))
        return json.dumps({"success": True, "variable_id": result["variableId"], "name": result["name"]})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_delete_variable(account_id: str, container_id: str, workspace_id: str, variable_id: str) -> str:
    """Delete a variable from a GTM workspace.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        variable_id: The variable ID to delete.
    """
    require_editor()
    try:
        _handle(_svc().accounts().containers().workspaces().variables().delete(
            path=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}/variables/{variable_id}"))
        return json.dumps({"success": True, "deleted_variable_id": variable_id})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ===========================================================================
# BUILT-IN VARIABLES
# ===========================================================================

@mcp.tool()
def gtm_list_builtin_variables(account_id: str, container_id: str, workspace_id: str) -> str:
    """List all enabled built-in variables in a GTM workspace (e.g. Click URL, Page Path).

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
    """
    try:
        result = _handle(_svc().accounts().containers().workspaces().built_in_variables().list(
            parent=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}"))
        variables = result.get("builtInVariable", [])
        return json.dumps([{"name": v["name"], "type": v["type"]} for v in variables])
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_enable_builtin_variable(account_id: str, container_id: str, workspace_id: str,
                                 variable_types: str) -> str:
    """Enable one or more built-in variables in a GTM workspace.

    Common types: PAGE_URL, PAGE_PATH, PAGE_HOSTNAME, CLICK_URL, CLICK_TEXT,
    CLICK_ID, CLICK_CLASSES, CLICK_TARGET, CLICK_ELEMENT, FORM_URL, FORM_ID,
    FORM_CLASSES, FORM_TARGET, FORM_ELEMENT, FORM_TEXT, SCROLL_DEPTH_THRESHOLD,
    SCROLL_DEPTH_DIRECTION, SCROLL_DEPTH_UNITS, VIDEO_URL, VIDEO_TITLE,
    VIDEO_DURATION, VIDEO_PERCENT, VIDEO_STATUS.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        variable_types: JSON array of built-in variable type strings to enable.
    """
    require_editor()
    try:
        types = json.loads(variable_types)
        result = _handle(_svc().accounts().containers().workspaces().built_in_variables().create(
            parent=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}",
            type=types))
        return json.dumps({"success": True, "enabled": result.get("builtInVariable", [])})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_disable_builtin_variable(account_id: str, container_id: str, workspace_id: str,
                                  variable_type: str) -> str:
    """Disable a built-in variable in a GTM workspace.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        variable_type: The built-in variable type to disable (e.g. PAGE_URL, CLICK_TEXT).
    """
    require_editor()
    try:
        _handle(_svc().accounts().containers().workspaces().built_in_variables().delete(
            path=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}",
            type=[variable_type]))
        return json.dumps({"success": True, "disabled": variable_type})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ===========================================================================
# FOLDERS
# ===========================================================================

@mcp.tool()
def gtm_list_folders(account_id: str, container_id: str, workspace_id: str) -> str:
    """List all folders in a GTM workspace.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
    """
    try:
        result = _handle(_svc().accounts().containers().workspaces().folders().list(
            parent=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}"))
        folders = result.get("folder", [])
        return json.dumps([{"folder_id": f["folderId"], "name": f["name"]} for f in folders])
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_create_folder(account_id: str, container_id: str, workspace_id: str, name: str) -> str:
    """Create a new folder in a GTM workspace for organizing tags/triggers/variables.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        name: Folder name.
    """
    require_editor()
    try:
        result = _handle(_svc().accounts().containers().workspaces().folders().create(
            parent=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}",
            body={"name": name}))
        return json.dumps({"folder_id": result["folderId"], "name": result["name"],
                           "path": result["path"]})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_list_folder_entities(account_id: str, container_id: str, workspace_id: str,
                              folder_id: str) -> str:
    """List all tags, triggers, and variables inside a specific GTM folder.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        folder_id: The folder ID.
    """
    try:
        result = _handle(_svc().accounts().containers().workspaces().folders().entities(
            path=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}/folders/{folder_id}"))
        return json.dumps({
            "tags": [{"id": t["tagId"], "name": t["name"]} for t in result.get("tag", [])],
            "triggers": [{"id": t["triggerId"], "name": t["name"]} for t in result.get("trigger", [])],
            "variables": [{"id": v["variableId"], "name": v["name"]} for v in result.get("variable", [])],
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


# ===========================================================================
# VERSIONS
# ===========================================================================

@mcp.tool()
def gtm_list_versions(account_id: str, container_id: str) -> str:
    """List all container versions for a GTM container.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
    """
    try:
        # `versions()` has no list method in the GTM API -- only get/publish/
        # live/delete/update/undelete/set_latest. Listing lives on the sibling
        # `version_headers()` resource, which returns lightweight summaries
        # (counts, not full tag/trigger/variable bodies) in the same shape.
        # Confirmed against the live discovery doc after this call raised
        # "'Resource' object has no attribute 'list'" during verification.
        result = _handle(_svc().accounts().containers().version_headers().list(
            parent=f"accounts/{account_id}/containers/{container_id}"))
        versions = result.get("containerVersionHeader", [])
        return json.dumps([{
            "version_id": v.get("containerVersionId", ""),
            "name": v.get("name", ""),
            "description": v.get("description", ""),
            "deleted": v.get("deleted", False),
            "num_tags": v.get("numTags", "0"),
            "num_triggers": v.get("numTriggers", "0"),
            "num_variables": v.get("numVariables", "0"),
        } for v in versions])
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_get_live_version(account_id: str, container_id: str) -> str:
    """Get the currently published (live) version of a GTM container.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
    """
    try:
        result = _handle(_svc().accounts().containers().versions().live(
            parent=f"accounts/{account_id}/containers/{container_id}"))
        return json.dumps({
            "version_id": result.get("containerVersionId", ""),
            "name": result.get("name", ""),
            "description": result.get("description", ""),
            "tag_count": len(result.get("tag", [])),
            "trigger_count": len(result.get("trigger", [])),
            "variable_count": len(result.get("variable", [])),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_create_version_from_workspace(account_id: str, container_id: str,
                                       workspace_id: str, name: str = "",
                                       description: str = "") -> str:
    """Create a new container version from a workspace (snapshot the workspace).

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        name: Optional version name.
        description: Optional version description.
    """
    require_editor()
    try:
        body = {"name": name, "notes": description}
        result = _handle(_svc().accounts().containers().workspaces().create_version(
            path=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}",
            body=body))
        cv = result.get("containerVersion", {})
        return json.dumps({
            "success": True,
            "version_id": cv.get("containerVersionId", ""),
            "name": cv.get("name", ""),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_publish_workspace_now(account_id: str, container_id: str, workspace_id: str,
                               version_name: str = "", version_notes: str = "") -> str:
    """Publish a GTM workspace immediately — creates a version and publishes it live.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        version_name: Optional name for the new version.
        version_notes: Optional description/notes for the version.
    """
    require_editor()
    try:
        body = {"name": version_name, "notes": version_notes}
        result = _handle(_svc().accounts().containers().workspaces().create_version(
            path=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}",
            body=body))
        cv = result.get("containerVersion", {})
        version_id = cv.get("containerVersionId", "")
        pub = _handle(_svc().accounts().containers().versions().publish(
            path=f"accounts/{account_id}/containers/{container_id}/versions/{version_id}"))
        return json.dumps({
            "success": True,
            "version_id": version_id,
            "version_name": cv.get("name", ""),
            "publish_status": pub.get("publishStatus", ""),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_rollback_to_version(account_id: str, container_id: str, version_id: str) -> str:
    """Roll back a GTM container to a specific published version.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        version_id: The version ID to publish (roll back to).
    """
    require_editor()
    try:
        result = _handle(_svc().accounts().containers().versions().publish(
            path=f"accounts/{account_id}/containers/{container_id}/versions/{version_id}"))
        return json.dumps({
            "success": True,
            "version_id": version_id,
            "publish_status": result.get("publishStatus", ""),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


# ===========================================================================
# AUDIT TOOLS
# ===========================================================================

@mcp.tool()
def gtm_find_unused_tags(account_id: str, container_id: str, workspace_id: str) -> str:
    """Find tags in a GTM workspace that have no firing triggers assigned.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
    """
    try:
        result = _handle(_svc().accounts().containers().workspaces().tags().list(
            parent=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}"))
        tags = result.get("tag", [])
        unused = [{"tag_id": t["tagId"], "name": t["name"], "type": t.get("type", "")}
                  for t in tags if not t.get("firingTriggerId")]
        return json.dumps({"unused_tags": unused, "total": len(unused)})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_find_paused_tags(account_id: str, container_id: str, workspace_id: str) -> str:
    """Find all paused tags in a GTM workspace.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
    """
    try:
        result = _handle(_svc().accounts().containers().workspaces().tags().list(
            parent=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}"))
        tags = result.get("tag", [])
        paused = [{"tag_id": t["tagId"], "name": t["name"], "type": t.get("type", "")}
                  for t in tags if t.get("paused")]
        return json.dumps({"paused_tags": paused, "total": len(paused)})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_audit_container(account_id: str, container_id: str, workspace_id: str) -> str:
    """Full container health audit — counts tags/triggers/variables, finds unused/paused tags,
    checks for tags without triggers, and summarizes tag types in use.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
    """
    try:
        svc = _svc()
        parent = f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}"
        tags = _handle(svc.accounts().containers().workspaces().tags().list(parent=parent)).get("tag", [])
        triggers = _handle(svc.accounts().containers().workspaces().triggers().list(parent=parent)).get("trigger", [])
        variables = _handle(svc.accounts().containers().workspaces().variables().list(parent=parent)).get("variable", [])

        used_trigger_ids = set()
        for t in tags:
            for tid in t.get("firingTriggerId", []):
                used_trigger_ids.add(tid)
            for tid in t.get("blockingTriggerId", []):
                used_trigger_ids.add(tid)

        unused_tags = [{"id": t["tagId"], "name": t["name"]} for t in tags if not t.get("firingTriggerId")]
        paused_tags = [{"id": t["tagId"], "name": t["name"]} for t in tags if t.get("paused")]
        unused_triggers = [{"id": t["triggerId"], "name": t["name"]}
                           for t in triggers if t["triggerId"] not in used_trigger_ids]

        tag_types: dict = {}
        for t in tags:
            tag_type = t.get("type", "unknown")
            tag_types[tag_type] = tag_types.get(tag_type, 0) + 1

        return json.dumps({
            "summary": {
                "total_tags": len(tags),
                "total_triggers": len(triggers),
                "total_variables": len(variables),
                "unused_tags": len(unused_tags),
                "paused_tags": len(paused_tags),
                "unused_triggers": len(unused_triggers),
            },
            "tag_types": tag_types,
            "issues": {
                "tags_without_triggers": unused_tags,
                "paused_tags": paused_tags,
                "unused_triggers": unused_triggers,
            },
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_find_tags_by_type(account_id: str, container_id: str, workspace_id: str,
                           tag_type: str) -> str:
    """Find all tags of a specific type in a GTM workspace.

    Common types: googtag (GA4 Config), gaawe (GA4 Event), awct (Google Ads Conversion),
    sp (Google Ads Remarketing), cl (Conversion Linker), html (Custom HTML),
    ua (Universal Analytics), flc (Floodlight Counter), fls (Floodlight Sales).

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        tag_type: The tag type code to filter by.
    """
    try:
        result = _handle(_svc().accounts().containers().workspaces().tags().list(
            parent=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}"))
        tags = result.get("tag", [])
        matching = [{"tag_id": t["tagId"], "name": t["name"], "type": t.get("type", ""),
                     "paused": t.get("paused", False),
                     "firing_trigger_ids": t.get("firingTriggerId", [])}
                    for t in tags if t.get("type", "") == tag_type]
        return json.dumps({"matching_tags": matching, "total": len(matching)})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ===========================================================================
# SMART TAG BUILDERS
# ===========================================================================

@mcp.tool()
def gtm_build_ga4_config_tag(account_id: str, container_id: str, workspace_id: str,
                               measurement_id: str,
                               firing_trigger_ids: str,
                               tag_name: str = "",
                               send_page_view: bool = True,
                               server_container_url: str = "") -> str:
    """Create a GA4 Configuration tag in a GTM workspace.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        measurement_id: GA4 Measurement ID (e.g. G-XXXXXXXXXX).
        firing_trigger_ids: JSON array of trigger IDs to fire this tag on (e.g. ["2147479553"] for All Pages).
        tag_name: Optional custom tag name (default: 'GA4 Config - {measurement_id}').
        send_page_view: Whether to send page_view events (default True).
        server_container_url: Optional server-side GTM container URL.
    """
    require_editor()
    try:
        trigger_ids = json.loads(firing_trigger_ids)
        params = [_pt("tagId", measurement_id), _pb("sendPageView", send_page_view)]
        if server_container_url:
            params.append(_pt("serverContainerUrl", server_container_url))
        body = {
            "name": tag_name or f"GA4 Config - {measurement_id}",
            "type": "googtag",
            "parameter": params,
            "firingTriggerId": trigger_ids,
        }
        result = _handle(_svc().accounts().containers().workspaces().tags().create(
            parent=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}",
            body=body))
        return json.dumps({"success": True, "tag_id": result["tagId"], "name": result["name"]})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_build_ga4_event_tag(account_id: str, container_id: str, workspace_id: str,
                              event_name: str,
                              config_tag_name: str,
                              firing_trigger_ids: str,
                              tag_name: str = "",
                              event_parameters: str = "[]",
                              send_ecommerce: bool = False) -> str:
    """Create a GA4 Event tag in a GTM workspace.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        event_name: GA4 event name (e.g. 'generate_lead', 'purchase').
        config_tag_name: Name of the existing GA4 Config tag to reference.
        firing_trigger_ids: JSON array of trigger IDs.
        tag_name: Optional custom tag name.
        event_parameters: JSON array of {name, value} objects for event parameters.
        send_ecommerce: Whether to send ecommerce dataLayer data with this event.
    """
    require_editor()
    try:
        trigger_ids = json.loads(firing_trigger_ids)
        ep_list = json.loads(event_parameters)
        params = [
            _pref("measurementId", config_tag_name),
            _pt("eventName", event_name),
        ]
        if ep_list:
            params.append(_plist("eventParameters", [
                _pmap({"name": p["name"], "value": p["value"]}) for p in ep_list
            ]))
        if send_ecommerce:
            params.append(_pb("sendEcommerceData", True))
            params.append(_pt("ecommerceMacroData", "{{Ecommerce}}"))
        body = {
            "name": tag_name or f"GA4 - {event_name}",
            "type": "gaawe",
            "parameter": params,
            "firingTriggerId": trigger_ids,
        }
        result = _handle(_svc().accounts().containers().workspaces().tags().create(
            parent=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}",
            body=body))
        return json.dumps({"success": True, "tag_id": result["tagId"], "name": result["name"]})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_build_google_ads_conversion_tag(account_id: str, container_id: str, workspace_id: str,
                                          conversion_id: str, conversion_label: str,
                                          firing_trigger_ids: str,
                                          tag_name: str = "",
                                          conversion_value: str = "",
                                          currency_code: str = "USD",
                                          order_id: str = "") -> str:
    """Create a Google Ads Conversion Tracking tag in a GTM workspace.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        conversion_id: Google Ads conversion ID (numeric, from AW-XXXXXXXXX).
        conversion_label: Conversion label string.
        firing_trigger_ids: JSON array of trigger IDs.
        tag_name: Optional custom tag name.
        conversion_value: Optional conversion value (leave empty for dynamic).
        currency_code: Currency code (default USD).
        order_id: Optional order ID variable (e.g. '{{Transaction ID}}').
    """
    require_editor()
    try:
        trigger_ids = json.loads(firing_trigger_ids)
        body = {
            "name": tag_name or f"GAds - Conversion - {conversion_label}",
            "type": "awct",
            "parameter": [
                _pt("conversionId", conversion_id),
                _pt("conversionLabel", conversion_label),
                _pb("enableConversionLinker", True),
                _pb("remarketingOnly", False),
                _pt("conversionValue", conversion_value),
                _pt("currencyCode", currency_code),
                _pt("orderId", order_id),
            ],
            "firingTriggerId": trigger_ids,
        }
        result = _handle(_svc().accounts().containers().workspaces().tags().create(
            parent=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}",
            body=body))
        return json.dumps({"success": True, "tag_id": result["tagId"], "name": result["name"]})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_build_google_ads_remarketing_tag(account_id: str, container_id: str, workspace_id: str,
                                           conversion_id: str,
                                           firing_trigger_ids: str,
                                           tag_name: str = "") -> str:
    """Create a Google Ads Remarketing tag in a GTM workspace.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        conversion_id: Google Ads conversion ID (numeric).
        firing_trigger_ids: JSON array of trigger IDs.
        tag_name: Optional custom tag name.
    """
    require_editor()
    try:
        trigger_ids = json.loads(firing_trigger_ids)
        body = {
            "name": tag_name or f"GAds - Remarketing - {conversion_id}",
            "type": "sp",
            "parameter": [_pt("conversionId", conversion_id)],
            "firingTriggerId": trigger_ids,
        }
        result = _handle(_svc().accounts().containers().workspaces().tags().create(
            parent=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}",
            body=body))
        return json.dumps({"success": True, "tag_id": result["tagId"], "name": result["name"]})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_build_conversion_linker_tag(account_id: str, container_id: str, workspace_id: str,
                                      firing_trigger_ids: str,
                                      enable_cross_domain: bool = False,
                                      tag_name: str = "Conversion Linker") -> str:
    """Create a Conversion Linker tag in GTM (required for Google Ads conversion tracking).

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        firing_trigger_ids: JSON array of trigger IDs (typically All Pages).
        enable_cross_domain: Enable cross-domain linking.
        tag_name: Tag name (default: 'Conversion Linker').
    """
    require_editor()
    try:
        trigger_ids = json.loads(firing_trigger_ids)
        body = {
            "name": tag_name,
            "type": "cl",
            "parameter": [_pb("enableCrossDomain", enable_cross_domain)],
            "firingTriggerId": trigger_ids,
        }
        result = _handle(_svc().accounts().containers().workspaces().tags().create(
            parent=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}",
            body=body))
        return json.dumps({"success": True, "tag_id": result["tagId"], "name": result["name"]})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_build_custom_html_tag(account_id: str, container_id: str, workspace_id: str,
                               html: str, firing_trigger_ids: str,
                               tag_name: str = "Custom HTML") -> str:
    """Create a Custom HTML tag in GTM.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        html: The HTML/JavaScript code to inject.
        firing_trigger_ids: JSON array of trigger IDs.
        tag_name: Tag name (default: 'Custom HTML').
    """
    require_editor()
    try:
        trigger_ids = json.loads(firing_trigger_ids)
        body = {
            "name": tag_name,
            "type": "html",
            "parameter": [
                _pt("html", html),
                _pb("supportDocumentWrite", False),
            ],
            "firingTriggerId": trigger_ids,
        }
        result = _handle(_svc().accounts().containers().workspaces().tags().create(
            parent=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}",
            body=body))
        return json.dumps({"success": True, "tag_id": result["tagId"], "name": result["name"]})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_build_facebook_pixel_tag(account_id: str, container_id: str, workspace_id: str,
                                   pixel_id: str, firing_trigger_ids: str,
                                   tag_name: str = "") -> str:
    """Create a Facebook/Meta Pixel tag in GTM.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        pixel_id: Facebook Pixel ID.
        firing_trigger_ids: JSON array of trigger IDs (typically All Pages).
        tag_name: Optional custom tag name.
    """
    require_editor()
    try:
        trigger_ids = json.loads(firing_trigger_ids)
        html = f"""<script>
!function(f,b,e,v,n,t,s)
{{if(f.fbq)return;n=f.fbq=function(){{n.callMethod?
n.callMethod.apply(n,arguments):n.queue.push(arguments)}};
if(!f._fbq)f._fbq=n;n.push=n;n.loaded=!0;n.version='2.0';
n.queue=[];t=b.createElement(e);t.async=!0;
t.src=v;s=b.getElementsByTagName(e)[0];
s.parentNode.insertBefore(t,s)}}(window, document,'script',
'https://connect.facebook.net/en_US/fbevents.js');
fbq('init', '{pixel_id}');
fbq('track', 'PageView');
</script>
<noscript><img height="1" width="1" style="display:none"
src="https://www.facebook.com/tr?id={pixel_id}&ev=PageView&noscript=1"/></noscript>"""
        body = {
            "name": tag_name or f"Facebook Pixel - {pixel_id}",
            "type": "html",
            "parameter": [_pt("html", html), _pb("supportDocumentWrite", False)],
            "firingTriggerId": trigger_ids,
        }
        result = _handle(_svc().accounts().containers().workspaces().tags().create(
            parent=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}",
            body=body))
        return json.dumps({"success": True, "tag_id": result["tagId"], "name": result["name"]})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_build_tiktok_pixel_tag(account_id: str, container_id: str, workspace_id: str,
                                 pixel_id: str, firing_trigger_ids: str,
                                 tag_name: str = "") -> str:
    """Create a TikTok Pixel tag in GTM.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        pixel_id: TikTok Pixel ID.
        firing_trigger_ids: JSON array of trigger IDs (typically All Pages).
        tag_name: Optional custom tag name.
    """
    require_editor()
    try:
        trigger_ids = json.loads(firing_trigger_ids)
        html = f"""<script>
!function (w, d, t) {{
  w.TiktokAnalyticsObject=t;var ttq=w[t]=w[t]||[];
  ttq.methods=["page","track","identify","instances","debug","on","off","once","ready","alias","group","enableCookie","disableCookie"],
  ttq.setAndDefer=function(t,e){{t[e]=function(){{t.push([e].concat(Array.prototype.slice.call(arguments,0)))}}}}
  for(var i=0;i<ttq.methods.length;i++)ttq.setAndDefer(ttq,ttq.methods[i]);
  ttq.instance=function(t){{for(var e=ttq._i[t]||[],n=0;n<ttq.methods.length;n++)ttq.setAndDefer(e,ttq.methods[n]);return e}}
  ttq.load=function(e,n){{var i="https://analytics.tiktok.com/i18n/pixel/events.js";
  ttq._i=ttq._i||{{}},ttq._i[e]=[],ttq._i[e]._u=i,ttq._t=ttq._t||{{}},ttq._t[e]=+new Date,ttq._o=ttq._o||{{}},ttq._o[e]=n||{{}};
  var o=document.createElement("script");o.type="text/javascript",o.async=!0,o.src=i+"?sdkid="+e+"&lib="+t;
  var a=document.getElementsByTagName("script")[0];a.parentNode.insertBefore(o,a)}};
  ttq.load('{pixel_id}');
  ttq.page();
}}(window, document, 'ttq');
</script>"""
        body = {
            "name": tag_name or f"TikTok Pixel - {pixel_id}",
            "type": "html",
            "parameter": [_pt("html", html), _pb("supportDocumentWrite", False)],
            "firingTriggerId": trigger_ids,
        }
        result = _handle(_svc().accounts().containers().workspaces().tags().create(
            parent=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}",
            body=body))
        return json.dumps({"success": True, "tag_id": result["tagId"], "name": result["name"]})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_build_linkedin_insight_tag(account_id: str, container_id: str, workspace_id: str,
                                    partner_id: str, firing_trigger_ids: str,
                                    tag_name: str = "") -> str:
    """Create a LinkedIn Insight Tag in GTM.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        partner_id: LinkedIn Partner/Campaign Manager ID.
        firing_trigger_ids: JSON array of trigger IDs (typically All Pages).
        tag_name: Optional custom tag name.
    """
    require_editor()
    try:
        trigger_ids = json.loads(firing_trigger_ids)
        html = f"""<script type="text/javascript">
_linkedin_partner_id = "{partner_id}";
window._linkedin_data_partner_ids = window._linkedin_data_partner_ids || [];
window._linkedin_data_partner_ids.push(_linkedin_partner_id);
(function(l) {{
if (!l){{window.lintrk = function(a,b){{window.lintrk.q.push([a,b])}};
window.lintrk.q=[]}}
var s = document.getElementsByTagName("script")[0];
var b = document.createElement("script");
b.type = "text/javascript";b.async = true;
b.src = "https://snap.licdn.com/li.lms-analytics/insight.min.js";
s.parentNode.insertBefore(b, s);}})(window.lintrk);
</script>
<noscript><img height="1" width="1" style="display:none;" alt="" src="https://px.ads.linkedin.com/collect/?pid={partner_id}&fmt=gif" /></noscript>"""
        body = {
            "name": tag_name or f"LinkedIn Insight - {partner_id}",
            "type": "html",
            "parameter": [_pt("html", html), _pb("supportDocumentWrite", False)],
            "firingTriggerId": trigger_ids,
        }
        result = _handle(_svc().accounts().containers().workspaces().tags().create(
            parent=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}",
            body=body))
        return json.dumps({"success": True, "tag_id": result["tagId"], "name": result["name"]})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_build_microsoft_clarity_tag(account_id: str, container_id: str, workspace_id: str,
                                      project_id: str, firing_trigger_ids: str,
                                      tag_name: str = "") -> str:
    """Create a Microsoft Clarity tag in GTM.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        project_id: Microsoft Clarity project ID.
        firing_trigger_ids: JSON array of trigger IDs (typically All Pages).
        tag_name: Optional custom tag name.
    """
    require_editor()
    try:
        trigger_ids = json.loads(firing_trigger_ids)
        html = f"""<script type="text/javascript">
    (function(c,l,a,r,i,t,y){{
        c[a]=c[a]||function(){{(c[a].q=c[a].q||[]).push(arguments)}};
        t=l.createElement(r);t.async=1;t.src="https://www.clarity.ms/tag/"+i;
        y=l.getElementsByTagName(r)[0];y.parentNode.insertBefore(t,y);
    }})(window, document, "clarity", "script", "{project_id}");
</script>"""
        body = {
            "name": tag_name or f"Microsoft Clarity - {project_id}",
            "type": "html",
            "parameter": [_pt("html", html), _pb("supportDocumentWrite", False)],
            "firingTriggerId": trigger_ids,
        }
        result = _handle(_svc().accounts().containers().workspaces().tags().create(
            parent=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}",
            body=body))
        return json.dumps({"success": True, "tag_id": result["tagId"], "name": result["name"]})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ===========================================================================
# SMART TRIGGER BUILDERS
# ===========================================================================

@mcp.tool()
def gtm_build_pageview_trigger(account_id: str, container_id: str, workspace_id: str,
                                name: str = "All Pages",
                                url_filter: str = "",
                                url_filter_type: str = "contains") -> str:
    """Create a Pageview trigger in GTM. Optionally filter by URL.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        name: Trigger name (default: 'All Pages').
        url_filter: Optional URL string to filter on (leave empty for all pages).
        url_filter_type: Filter type: contains | startsWith | endsWith | equals | matchesRegex.
    """
    require_editor()
    try:
        body: dict[str, Any] = {"name": name, "type": "pageview"}
        if url_filter:
            body["filter"] = [_cond(url_filter_type, "{{Page URL}}", url_filter)]
        result = _handle(_svc().accounts().containers().workspaces().triggers().create(
            parent=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}",
            body=body))
        return json.dumps({"success": True, "trigger_id": result["triggerId"], "name": result["name"]})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_build_custom_event_trigger(account_id: str, container_id: str, workspace_id: str,
                                    event_name: str, name: str = "") -> str:
    """Create a Custom Event trigger in GTM that fires on a specific dataLayer event.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        event_name: The dataLayer event name to listen for (e.g. 'form_submit', 'purchase').
        name: Optional trigger name (default: 'Event - {event_name}').
    """
    require_editor()
    try:
        body = {
            "name": name or f"Event - {event_name}",
            "type": "customEvent",
            "customEventFilter": [_cond("equals", "{{_event}}", event_name)],
        }
        result = _handle(_svc().accounts().containers().workspaces().triggers().create(
            parent=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}",
            body=body))
        return json.dumps({"success": True, "trigger_id": result["triggerId"], "name": result["name"]})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_build_link_click_trigger(account_id: str, container_id: str, workspace_id: str,
                                   name: str = "All Link Clicks",
                                   url_filter: str = "",
                                   url_filter_type: str = "contains",
                                   wait_for_tags: bool = False) -> str:
    """Create a Link Click trigger in GTM. Optionally filter by clicked URL.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        name: Trigger name.
        url_filter: Optional URL string to filter on.
        url_filter_type: Filter type: contains | startsWith | endsWith | equals | matchesRegex.
        wait_for_tags: Wait for other tags before navigating (useful for conversion tracking).
    """
    require_editor()
    try:
        body: dict[str, Any] = {
            "name": name,
            "type": "linkClick",
            "parameter": [
                _pb("waitForTags", wait_for_tags),
                _pb("checkValidation", False),
                _pt("waitForTagsTimeout", "2000"),
            ],
        }
        if url_filter:
            body["autoEventFilter"] = [_cond(url_filter_type, "{{Click URL}}", url_filter)]
        result = _handle(_svc().accounts().containers().workspaces().triggers().create(
            parent=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}",
            body=body))
        return json.dumps({"success": True, "trigger_id": result["triggerId"], "name": result["name"]})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_build_form_submit_trigger(account_id: str, container_id: str, workspace_id: str,
                                   name: str = "Form Submission",
                                   url_filter: str = "",
                                   form_id_filter: str = "",
                                   check_validation: bool = True) -> str:
    """Create a Form Submission trigger in GTM. Optionally filter by page URL or form ID.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        name: Trigger name.
        url_filter: Optional page URL to filter on.
        form_id_filter: Optional form element ID to filter on (uses 'contains' match).
        check_validation: Only fire on valid form submissions.
    """
    require_editor()
    try:
        body: dict[str, Any] = {
            "name": name,
            "type": "formSubmission",
            "parameter": [
                _pb("checkValidation", check_validation),
                _pb("waitForTags", False),
                _pt("waitForTagsTimeout", "2000"),
            ],
        }
        filters = []
        if url_filter:
            filters.append(_cond("contains", "{{Page URL}}", url_filter))
        if form_id_filter:
            filters.append(_cond("contains", "{{Form ID}}", form_id_filter))
        if filters:
            body["autoEventFilter"] = filters
        result = _handle(_svc().accounts().containers().workspaces().triggers().create(
            parent=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}",
            body=body))
        return json.dumps({"success": True, "trigger_id": result["triggerId"], "name": result["name"]})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_build_scroll_depth_trigger(account_id: str, container_id: str, workspace_id: str,
                                    thresholds: str = "[25, 50, 75, 90]",
                                    name: str = "Scroll Depth") -> str:
    """Create a Scroll Depth trigger in GTM that fires at specific scroll percentages.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        thresholds: JSON array of scroll depth percentages (e.g. [25, 50, 75, 90]).
        name: Trigger name.
    """
    require_editor()
    try:
        threshold_list = json.loads(thresholds)
        body = {
            "name": name,
            "type": "scrollDepth",
            "parameter": [
                _pb("verticalThresholdUnits", False),
                _pt("verticalThresholds", ",".join(str(t) for t in threshold_list)),
                _pb("horizontalThresholdUnits", False),
            ],
        }
        result = _handle(_svc().accounts().containers().workspaces().triggers().create(
            parent=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}",
            body=body))
        return json.dumps({"success": True, "trigger_id": result["triggerId"], "name": result["name"]})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ===========================================================================
# SMART VARIABLE BUILDERS
# ===========================================================================

@mcp.tool()
def gtm_build_datalayer_variable(account_id: str, container_id: str, workspace_id: str,
                                   datalayer_key: str, name: str = "") -> str:
    """Create a Data Layer Variable in GTM.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        datalayer_key: The dataLayer key to read (e.g. 'transaction_id', 'ecommerce.purchase.value').
        name: Optional variable name (default: 'DLV - {datalayer_key}').
    """
    require_editor()
    try:
        body = {
            "name": name or f"DLV - {datalayer_key}",
            "type": "v",
            "parameter": [
                _pt("name", datalayer_key),
                _pt("dataLayerVersion", "2"),
                _pb("setDefaultValue", False),
            ],
        }
        result = _handle(_svc().accounts().containers().workspaces().variables().create(
            parent=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}",
            body=body))
        return json.dumps({"success": True, "variable_id": result["variableId"], "name": result["name"]})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_build_custom_javascript_variable(account_id: str, container_id: str, workspace_id: str,
                                          js_code: str, name: str = "Custom JS Variable") -> str:
    """Create a Custom JavaScript Variable in GTM.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        js_code: JavaScript function body — must be a function() { return ...; } expression.
        name: Variable name.
    """
    require_editor()
    try:
        body = {
            "name": name,
            "type": "jsm",
            "parameter": [_pt("javascript", js_code)],
        }
        result = _handle(_svc().accounts().containers().workspaces().variables().create(
            parent=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}",
            body=body))
        return json.dumps({"success": True, "variable_id": result["variableId"], "name": result["name"]})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_build_constant_variable(account_id: str, container_id: str, workspace_id: str,
                                  value: str, name: str = "") -> str:
    """Create a Constant Variable in GTM.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        value: The constant value to store.
        name: Variable name (default: 'Constant - {value}').
    """
    require_editor()
    try:
        body = {
            "name": name or f"Constant - {value[:30]}",
            "type": "c",
            "parameter": [_pt("value", value)],
        }
        result = _handle(_svc().accounts().containers().workspaces().variables().create(
            parent=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}",
            body=body))
        return json.dumps({"success": True, "variable_id": result["variableId"], "name": result["name"]})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_build_url_variable(account_id: str, container_id: str, workspace_id: str,
                            component_type: str = "PATH", name: str = "") -> str:
    """Create a URL Variable in GTM that extracts a part of the page URL.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        component_type: URL component: PROTOCOL | HOST | PATH | QUERY | FRAGMENT | PORT | FULL_URL.
        name: Variable name (default: 'URL - {component_type}').
    """
    require_editor()
    try:
        body = {
            "name": name or f"URL - {component_type}",
            "type": "u",
            "parameter": [
                _pt("component", component_type),
                _pt("defaultPages", ""),
            ],
        }
        result = _handle(_svc().accounts().containers().workspaces().variables().create(
            parent=f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}",
            body=body))
        return json.dumps({"success": True, "variable_id": result["variableId"], "name": result["name"]})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ===========================================================================
# WORKFLOW: GA4 QUICK SETUP
# ===========================================================================

@mcp.tool()
def gtm_setup_ga4_basic(account_id: str, container_id: str, workspace_id: str,
                         measurement_id: str) -> str:
    """One-shot: Set up GA4 basic tracking in GTM.
    Creates: All Pages pageview trigger + GA4 Config tag with page_view.
    Returns the IDs of everything created.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        measurement_id: GA4 Measurement ID (G-XXXXXXXXXX).
    """
    require_editor()
    try:
        svc = _svc()
        parent = f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}"

        trigger_body = {"name": "All Pages", "type": "pageview"}
        trigger_result = _handle(svc.accounts().containers().workspaces().triggers().create(
            parent=parent, body=trigger_body))
        trigger_id = trigger_result["triggerId"]

        tag_body = {
            "name": f"GA4 Config - {measurement_id}",
            "type": "googtag",
            "parameter": [_pt("tagId", measurement_id), _pb("sendPageView", True)],
            "firingTriggerId": [trigger_id],
        }
        tag_result = _handle(svc.accounts().containers().workspaces().tags().create(
            parent=parent, body=tag_body))

        return json.dumps({
            "success": True,
            "created": {
                "trigger": {"id": trigger_id, "name": "All Pages"},
                "tag": {"id": tag_result["tagId"], "name": tag_result["name"]},
            },
            "next_step": "Call gtm_publish_workspace_now to go live.",
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gtm_setup_google_ads_conversion(account_id: str, container_id: str, workspace_id: str,
                                     conversion_id: str, conversion_label: str,
                                     thank_you_url_contains: str,
                                     conversion_value: str = "",
                                     currency_code: str = "USD") -> str:
    """One-shot: Set up Google Ads conversion tracking in GTM.
    Creates: Thank-you page URL trigger + Conversion Linker tag + Google Ads Conversion tag.

    Args:
        account_id: The GTM account ID.
        container_id: The GTM container ID.
        workspace_id: The GTM workspace ID.
        conversion_id: Google Ads conversion ID (numeric, from AW-XXXXXXXXX).
        conversion_label: Conversion label string.
        thank_you_url_contains: String that appears in the thank-you page URL.
        conversion_value: Optional fixed conversion value.
        currency_code: Currency code (default USD).
    """
    require_editor()
    try:
        svc = _svc()
        parent = f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}"

        all_pages_trigger = _handle(svc.accounts().containers().workspaces().triggers().create(
            parent=parent, body={"name": "All Pages", "type": "pageview"}))
        all_pages_id = all_pages_trigger["triggerId"]

        ty_trigger = _handle(svc.accounts().containers().workspaces().triggers().create(
            parent=parent, body={
                "name": f"Thank You - {thank_you_url_contains}",
                "type": "pageview",
                "filter": [_cond("contains", "{{Page URL}}", thank_you_url_contains)],
            }))
        ty_id = ty_trigger["triggerId"]

        linker_tag = _handle(svc.accounts().containers().workspaces().tags().create(
            parent=parent, body={
                "name": "Conversion Linker",
                "type": "cl",
                "parameter": [_pb("enableCrossDomain", False)],
                "firingTriggerId": [all_pages_id],
            }))

        conv_tag = _handle(svc.accounts().containers().workspaces().tags().create(
            parent=parent, body={
                "name": f"GAds - Conversion - {conversion_label}",
                "type": "awct",
                "parameter": [
                    _pt("conversionId", conversion_id),
                    _pt("conversionLabel", conversion_label),
                    _pb("enableConversionLinker", True),
                    _pb("remarketingOnly", False),
                    _pt("conversionValue", conversion_value),
                    _pt("currencyCode", currency_code),
                ],
                "firingTriggerId": [ty_id],
            }))

        return json.dumps({
            "success": True,
            "created": {
                "all_pages_trigger": {"id": all_pages_id, "name": "All Pages"},
                "thank_you_trigger": {"id": ty_id, "name": ty_trigger["name"]},
                "conversion_linker_tag": {"id": linker_tag["tagId"], "name": linker_tag["name"]},
                "conversion_tag": {"id": conv_tag["tagId"], "name": conv_tag["name"]},
            },
            "next_step": "Call gtm_publish_workspace_now to go live.",
        })
    except Exception as e:
        return json.dumps({"error": str(e)})
