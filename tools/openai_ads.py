"""
OpenAI Ads tools — manage advertising shown inside ChatGPT via the
OpenAI Ads API (https://developers.openai.com/ads).

Auth model: each user supplies a bearer API key from their OpenAI Ads
Manager (Settings tab). We store it encrypted (external_api_keys table) and
attach it as `Authorization: Bearer <key>` on every request. No OAuth.

Base URL: https://api.ads.openai.com/v1
Resources: ad_account, campaigns, ad_groups, ads, insights.

Convention (matches the other tool modules): every tool returns a JSON string
and never raises; write tools require the editor role and default new entities
to PAUSED unless the caller explicitly asks to launch.
"""
import json

import requests as httpx

from mcp_instance import mcp
from auth import current_user_ctx
from permissions import require_editor

_BASE = "https://api.ads.openai.com/v1"
_PLATFORM = "openai_ads"
_TIMEOUT = 30

# 1 currency unit = 1,000,000 micros (millionths). $25 -> 25_000_000 micros.
_MICROS = 1_000_000


# ---------------------------------------------------------------------------
# Credential + request helpers
# ---------------------------------------------------------------------------

def _key() -> str:
    user = current_user_ctx.get(None)
    if user is None:
        raise RuntimeError("Not authenticated.")
    from credentials import provider
    key = provider().api_key(_PLATFORM)
    if not key:
        raise RuntimeError(
            "OpenAI Ads is not connected. Add your OpenAI Ads API key from your "
            "dashboard (Connect OpenAI Ads), then retry. Get the key in OpenAI Ads "
            "Manager → Settings."
        )
    return key


def _request(method: str, path: str, params: dict | None = None, body: dict | None = None):
    """Call the OpenAI Ads API. Returns parsed JSON, or an {'error': ...} dict."""
    headers = {"Authorization": f"Bearer {_key()}", "Accept": "application/json"}
    resp = httpx.request(
        method, f"{_BASE}{path}",
        headers=headers,
        params={k: v for k, v in (params or {}).items() if v not in (None, "", [])},
        json=body,
        timeout=_TIMEOUT,
    )
    try:
        data = resp.json()
    except Exception:
        data = {"text": (resp.text or "")[:600]}
    if resp.status_code >= 400:
        return {"error": f"OpenAI Ads API returned {resp.status_code}", "details": data}
    return data


def _usd_to_micros(usd: float) -> int:
    return int(round(float(usd) * _MICROS))


def _ok(payload) -> str:
    return json.dumps(payload, default=str)


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------

@mcp.tool()
def openai_ads_get_account() -> str:
    """Get the connected OpenAI Ads account. Call this first to confirm the API
    key works and to see account-level settings. GET /ad_account."""
    try:
        return _ok(_request("GET", "/ad_account"))
    except Exception as e:
        return _ok({"error": str(e)})


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------

@mcp.tool()
def openai_ads_list_campaigns(limit: int = 20, after: str = "", before: str = "", order: str = "desc") -> str:
    """List OpenAI Ads campaigns.

    Args:
        limit: 1-500 (default 20).
        after / before: pagination cursors returned by a previous call.
        order: 'asc' or 'desc' (default desc).
    """
    try:
        return _ok(_request("GET", "/campaigns", params={"limit": limit, "after": after, "before": before, "order": order}))
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool()
def openai_ads_get_campaign(campaign_id: str) -> str:
    """Retrieve a single OpenAI Ads campaign by ID. GET /campaigns/{id}."""
    try:
        return _ok(_request("GET", f"/campaigns/{campaign_id}"))
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool()
def openai_ads_create_campaign(
    name: str,
    lifetime_budget_usd: float,
    status: str = "paused",
    description: str = "",
) -> str:
    """Create an OpenAI Ads campaign. Defaults to PAUSED — pass status='active' only
    if the user explicitly wants it live.

    Args:
        name: Campaign name (3-1000 chars).
        lifetime_budget_usd: Lifetime spend limit in dollars (min $1).
        status: 'paused' (default) or 'active'.
        description: Optional description.
    """
    err = require_editor("openai_ads_create_campaign")
    if err:
        return err
    try:
        body = {
            "name": name,
            "status": "active" if status == "active" else "paused",
            "budget": {"lifetime_spend_limit_micros": max(_MICROS, _usd_to_micros(lifetime_budget_usd))},
        }
        if description:
            body["description"] = description
        return _ok(_request("POST", "/campaigns", body=body))
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool()
def openai_ads_update_campaign(
    campaign_id: str,
    name: str = "",
    lifetime_budget_usd: float = 0,
    description: str = "",
) -> str:
    """Update a campaign's name, budget, or description. Only non-empty fields are
    sent. POST /campaigns/{id}."""
    err = require_editor("openai_ads_update_campaign")
    if err:
        return err
    try:
        body: dict = {}
        if name:
            body["name"] = name
        if lifetime_budget_usd and lifetime_budget_usd > 0:
            body["budget"] = {"lifetime_spend_limit_micros": max(_MICROS, _usd_to_micros(lifetime_budget_usd))}
        if description:
            body["description"] = description
        if not body:
            return _ok({"error": "Nothing to update — pass name, lifetime_budget_usd, or description."})
        return _ok(_request("POST", f"/campaigns/{campaign_id}", body=body))
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool()
def openai_ads_set_campaign_status(campaign_id: str, action: str) -> str:
    """Activate, pause, or archive a campaign.

    Args:
        campaign_id: The campaign to change.
        action: 'activate', 'pause', or 'archive'.
    """
    err = require_editor("openai_ads_set_campaign_status")
    if err:
        return err
    if action not in ("activate", "pause", "archive"):
        return _ok({"error": "action must be 'activate', 'pause', or 'archive'."})
    try:
        return _ok(_request("POST", f"/campaigns/{campaign_id}/{action}"))
    except Exception as e:
        return _ok({"error": str(e)})


# ---------------------------------------------------------------------------
# Ad groups
# ---------------------------------------------------------------------------

@mcp.tool()
def openai_ads_list_ad_groups(campaign_id: str = "", limit: int = 20, after: str = "", order: str = "desc") -> str:
    """List ad groups, optionally filtered to a campaign. GET /ad_groups."""
    try:
        return _ok(_request("GET", "/ad_groups", params={"campaign_id": campaign_id, "limit": limit, "after": after, "order": order}))
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool()
def openai_ads_get_ad_group(ad_group_id: str) -> str:
    """Retrieve a single ad group by ID. GET /ad_groups/{id}."""
    try:
        return _ok(_request("GET", f"/ad_groups/{ad_group_id}"))
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool()
def openai_ads_create_ad_group(
    campaign_id: str,
    name: str,
    max_bid_usd: float,
    status: str = "paused",
    description: str = "",
) -> str:
    """Create an ad group under a campaign. Defaults to PAUSED.

    Args:
        campaign_id: Parent campaign ID.
        name: Ad group name.
        max_bid_usd: Max bid per impression in dollars (billing_event_type=impression).
        status: 'paused' (default) or 'active'.
        description: Optional.
    """
    err = require_editor("openai_ads_create_ad_group")
    if err:
        return err
    try:
        body = {
            "campaign_id": campaign_id,
            "name": name,
            "status": "active" if status == "active" else "paused",
            "bidding_config": {"billing_event_type": "impression", "max_bid_micros": _usd_to_micros(max_bid_usd)},
        }
        if description:
            body["description"] = description
        return _ok(_request("POST", "/ad_groups", body=body))
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool()
def openai_ads_set_ad_group_status(ad_group_id: str, action: str) -> str:
    """Activate, pause, or archive an ad group. action: 'activate' | 'pause' | 'archive'."""
    err = require_editor("openai_ads_set_ad_group_status")
    if err:
        return err
    if action not in ("activate", "pause", "archive"):
        return _ok({"error": "action must be 'activate', 'pause', or 'archive'."})
    try:
        return _ok(_request("POST", f"/ad_groups/{ad_group_id}/{action}"))
    except Exception as e:
        return _ok({"error": str(e)})


# ---------------------------------------------------------------------------
# Ads
# ---------------------------------------------------------------------------

@mcp.tool()
def openai_ads_list_ads(ad_group_id: str = "", limit: int = 20, after: str = "", order: str = "desc") -> str:
    """List ads, optionally filtered to an ad group. GET /ads."""
    try:
        return _ok(_request("GET", "/ads", params={"ad_group_id": ad_group_id, "limit": limit, "after": after, "order": order}))
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool()
def openai_ads_get_ad(ad_id: str) -> str:
    """Retrieve a single ad by ID, including creative and review_status. GET /ads/{id}."""
    try:
        return _ok(_request("GET", f"/ads/{ad_id}"))
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool()
def openai_ads_create_chat_card_ad(
    ad_group_id: str,
    name: str,
    title: str,
    body: str,
    target_url: str,
    file_id: str,
    status: str = "paused",
) -> str:
    """Create a 'chat_card' ad under an ad group. Defaults to PAUSED.

    Args:
        ad_group_id: Parent ad group ID.
        name: Ad name (3-1000 chars).
        title: Creative title (3-50 chars).
        body: Creative body (max 100 chars).
        target_url: Landing URL the ad links to (required for chat_card).
        file_id: ID of an uploaded image file (required for chat_card).
        status: 'paused' (default) or 'active'.
    """
    err = require_editor("openai_ads_create_chat_card_ad")
    if err:
        return err
    try:
        payload = {
            "ad_group_id": ad_group_id,
            "name": name,
            "status": "active" if status == "active" else "paused",
            "creative": {
                "type": "chat_card",
                "title": title,
                "body": body,
                "target_url": target_url,
                "file_id": file_id,
            },
        }
        return _ok(_request("POST", "/ads", body=payload))
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool()
def openai_ads_set_ad_status(ad_id: str, action: str) -> str:
    """Activate, pause, or archive an ad. action: 'activate' | 'pause' | 'archive'."""
    err = require_editor("openai_ads_set_ad_status")
    if err:
        return err
    if action not in ("activate", "pause", "archive"):
        return _ok({"error": "action must be 'activate', 'pause', or 'archive'."})
    try:
        return _ok(_request("POST", f"/ads/{ad_id}/{action}"))
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool()
def openai_ads_ad_insights(ad_id: str, time_granularity: str = "daily", limit: int = 7) -> str:
    """Get performance insights for an ad (impressions, clicks, spend over time).

    Args:
        ad_id: The ad to report on.
        time_granularity: 'daily' (default) or other granularity the API supports.
        limit: Number of time buckets to return (default 7).
    """
    try:
        return _ok(_request("GET", f"/ads/{ad_id}/insights", params={"time_granularity": time_granularity, "limit": limit}))
    except Exception as e:
        return _ok({"error": str(e)})
