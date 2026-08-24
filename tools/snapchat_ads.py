"""
Snapchat Ads tools backed by Pipedream Connect.

Snapchat's Marketing API has been open to all advertisers since 2018 -- there is no
partner-approval tier -- so a user connecting through Pipedream in /manage is all that
is needed. This server never stores Snapchat access tokens; every call goes through
the Pipedream Connect proxy (see tools/pipedream_common.py).

Snapchat's object hierarchy is organization > ad account > campaign > ad squad > ad.
"Ad squad" is Snapchat's name for what Meta calls an ad set.
"""
from datetime import date, timedelta

from mcp_instance import mcp
from permissions import require_editor
from tools.platform_http import (
    _json,
    _parse_json_arg,
    _platform_base,
    list_accounts as _list_pipedream_accounts,
    request as _proxy_request,
)

_PLATFORM = "snapchat_ads"

# Snapchat wants full ISO-8601 instants on day boundaries, not bare dates.
_DAY_START = "T00:00:00Z"


def _snap_base(path_or_url: str) -> str:
    return _platform_base(_PLATFORM, path_or_url)


def _snap_time(value: str, fallback_days_ago: int) -> str:
    """Normalize a YYYY-MM-DD date into the day-boundary instant Snapchat requires."""
    raw = (value or "").strip()
    if not raw:
        raw = str(date.today() - timedelta(days=fallback_days_ago))
    if raw.endswith("Z") or "T" in raw:
        return raw
    return f"{raw}{_DAY_START}"


def _unwrap(payload: dict, key: str) -> dict:
    """Snapchat wraps every result as {"<key>": [{"sub_request_status":..., "<single>":{}}]}.

    Flatten it to a plain list so the model does not have to reason about the envelope,
    while keeping the raw payload available if a sub-request failed.
    """
    if not isinstance(payload, dict) or "error" in payload:
        return payload
    rows = payload.get(key)
    if not isinstance(rows, list):
        return payload
    singular = key[:-1] if key.endswith("s") else key
    items = []
    failed = []
    for row in rows:
        if not isinstance(row, dict):
            items.append(row)
            continue
        if str(row.get("sub_request_status", "SUCCESS")).upper() != "SUCCESS":
            failed.append(row)
            continue
        items.append(row.get(singular, row))
    out = {key: items, "total": len(items)}
    if failed:
        out["failed_sub_requests"] = failed
    if payload.get("connection_id") or payload.get("pipedream_account_id"):
        out["pipedream_account_id"] = payload.get("connection_id") or payload["pipedream_account_id"]
    return out


@mcp.tool()
def snapchat_ads_pipedream_accounts() -> str:
    """List connected Pipedream accounts that can be used for Snapchat Ads proxy calls."""
    try:
        accounts = _list_pipedream_accounts(_PLATFORM)
        return _json({"accounts": accounts, "total": len(accounts)})
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def snapchat_ads_list_organizations(pipedream_account_id: str = "") -> str:
    """List Snapchat organizations for the connected user. Start here — ad accounts live under an organization."""
    try:
        payload = _proxy_request(_PLATFORM, "GET", _snap_base("/me/organizations"), pipedream_account_id)
        return _json(_unwrap(payload, "organizations"))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def snapchat_ads_list_accounts(organization_id: str, pipedream_account_id: str = "") -> str:
    """List Snapchat ad accounts under an organization. Get organization_id from snapchat_ads_list_organizations."""
    try:
        payload = _proxy_request(
            _PLATFORM, "GET", _snap_base(f"/organizations/{organization_id}/adaccounts"), pipedream_account_id
        )
        return _json(_unwrap(payload, "adaccounts"))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def snapchat_ads_list_campaigns(ad_account_id: str, pipedream_account_id: str = "") -> str:
    """List Snapchat Ads campaigns in an ad account."""
    try:
        payload = _proxy_request(
            _PLATFORM, "GET", _snap_base(f"/adaccounts/{ad_account_id}/campaigns"), pipedream_account_id
        )
        return _json(_unwrap(payload, "campaigns"))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def snapchat_ads_get_campaign(campaign_id: str, pipedream_account_id: str = "") -> str:
    """Get one Snapchat Ads campaign by campaign id."""
    try:
        payload = _proxy_request(_PLATFORM, "GET", _snap_base(f"/campaigns/{campaign_id}"), pipedream_account_id)
        return _json(_unwrap(payload, "campaigns"))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def snapchat_ads_list_adsquads(ad_account_id: str, pipedream_account_id: str = "") -> str:
    """List Snapchat ad squads (ad sets) in an ad account."""
    try:
        payload = _proxy_request(
            _PLATFORM, "GET", _snap_base(f"/adaccounts/{ad_account_id}/adsquads"), pipedream_account_id
        )
        return _json(_unwrap(payload, "adsquads"))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def snapchat_ads_list_ads(ad_account_id: str, pipedream_account_id: str = "") -> str:
    """List Snapchat ads in an ad account."""
    try:
        payload = _proxy_request(
            _PLATFORM, "GET", _snap_base(f"/adaccounts/{ad_account_id}/ads"), pipedream_account_id
        )
        return _json(_unwrap(payload, "ads"))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def snapchat_ads_campaign_report(
    campaign_id: str,
    start_date: str = "",
    end_date: str = "",
    granularity: str = "DAY",
    fields: str = "impressions,swipes,spend,conversion_purchases,video_views",
    pipedream_account_id: str = "",
) -> str:
    """Get Snapchat Ads performance stats for one campaign.

    Args:
        campaign_id: Snapchat campaign id.
        start_date: YYYY-MM-DD. Defaults to 28 days ago. Ignored when granularity=TOTAL.
        end_date: YYYY-MM-DD. Defaults to today.
        granularity: TOTAL, DAY, or HOUR. DAY and HOUR require a date range.
        fields: Comma-separated Snapchat metric names.
    """
    try:
        gran = (granularity or "DAY").strip().upper()
        params = {"granularity": gran, "fields": fields}
        if gran != "TOTAL":
            params["start_time"] = _snap_time(start_date, 28)
            params["end_time"] = _snap_time(end_date, 0)
        payload = _proxy_request(
            _PLATFORM, "GET", _snap_base(f"/campaigns/{campaign_id}/stats"), pipedream_account_id, params=params
        )
        return _json(payload)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def snapchat_ads_account_report(
    ad_account_id: str,
    start_date: str = "",
    end_date: str = "",
    granularity: str = "DAY",
    fields: str = "impressions,swipes,spend,conversion_purchases",
    pipedream_account_id: str = "",
) -> str:
    """Get Snapchat Ads performance stats for a whole ad account over a date range."""
    try:
        gran = (granularity or "DAY").strip().upper()
        params = {"granularity": gran, "fields": fields}
        if gran != "TOTAL":
            params["start_time"] = _snap_time(start_date, 28)
            params["end_time"] = _snap_time(end_date, 0)
        payload = _proxy_request(
            _PLATFORM, "GET", _snap_base(f"/adaccounts/{ad_account_id}/stats"), pipedream_account_id, params=params
        )
        return _json(payload)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def snapchat_ads_create_campaign(ad_account_id: str, campaign_json: str, pipedream_account_id: str = "") -> str:
    """Create a Snapchat Ads campaign. Pass the campaign body as JSON.

    Created PAUSED unless the JSON explicitly sets status ACTIVE. Snapchat expects the
    campaign wrapped in a "campaigns" array; that wrapping is added here if omitted.
    """
    denied = require_editor("snapchat_ads_create_campaign")
    if denied:
        return denied
    try:
        body = _parse_json_arg(campaign_json, "campaign_json", {})
        if not isinstance(body, dict):
            return _json({"error": "campaign_json must be a JSON object."})
        body.setdefault("ad_account_id", ad_account_id)
        body.setdefault("status", "PAUSED")
        payload = _proxy_request(
            _PLATFORM,
            "POST",
            _snap_base(f"/adaccounts/{ad_account_id}/campaigns"),
            pipedream_account_id,
            body={"campaigns": [body]},
        )
        return _json(_unwrap(payload, "campaigns"))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def snapchat_ads_update_campaign_status(
    ad_account_id: str, campaign_id: str, status: str, pipedream_account_id: str = ""
) -> str:
    """Pause or activate a Snapchat Ads campaign. status must be PAUSED or ACTIVE.

    Snapchat's update is a full PUT, so this reads the current campaign first and writes
    it back with only the status changed — sending a partial object would blank fields.
    """
    denied = require_editor("snapchat_ads_update_campaign_status")
    if denied:
        return denied
    try:
        new_status = (status or "").strip().upper()
        if new_status not in ("PAUSED", "ACTIVE"):
            return _json({"error": "status must be PAUSED or ACTIVE."})

        current = _proxy_request(_PLATFORM, "GET", _snap_base(f"/campaigns/{campaign_id}"), pipedream_account_id)
        flat = _unwrap(current, "campaigns")
        rows = flat.get("campaigns") if isinstance(flat, dict) else None
        if not rows:
            return _json({"error": f"Could not read campaign {campaign_id} before updating it.", "response": current})

        campaign = dict(rows[0])
        campaign["status"] = new_status
        campaign.setdefault("id", campaign_id)
        campaign.setdefault("ad_account_id", ad_account_id)

        payload = _proxy_request(
            _PLATFORM,
            "PUT",
            _snap_base(f"/adaccounts/{ad_account_id}/campaigns"),
            pipedream_account_id,
            body={"campaigns": [campaign]},
        )
        return _json(_unwrap(payload, "campaigns"))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def snapchat_ads_raw_request(
    method: str, url: str, params_json: str = "{}", body_json: str = "{}", pipedream_account_id: str = ""
) -> str:
    """Call any Snapchat Marketing API endpoint through the Pipedream proxy.

    Escape hatch for endpoints without a dedicated tool. url may be a path
    (e.g. /adaccounts/{id}/media) or a full https://adsapi.snapchat.com URL.
    """
    denied = require_editor("snapchat_ads_raw_request")
    if denied:
        return denied
    try:
        params = _parse_json_arg(params_json, "params_json", {})
        body = _parse_json_arg(body_json, "body_json", {})
        return _json(_proxy_request(
            _PLATFORM, method, _snap_base(url), pipedream_account_id, params=params, body=body or None
        ))
    except Exception as exc:
        return _json({"error": str(exc)})
