"""
LinkedIn Ads and TikTok Ads tools backed by Pipedream Connect.

These tools use Pipedream's managed auth proxy, so end users connect in the
browser and this server never stores LinkedIn/TikTok access tokens.

The Pipedream plumbing itself lives in tools/pipedream_common.py and is shared
with the other Pipedream-backed ad platforms.
"""
import json
from datetime import date, timedelta
from typing import Any

from mcp_instance import mcp
from permissions import require_editor
from tools.platform_http import (
    _json,
    _parse_json_arg,
    _platform_base,
    list_accounts as _list_pipedream_accounts,
    request as _proxy_request,
)


def _linkedin_urn(account_id: str) -> str:
    value = str(account_id).strip()
    if value.startswith("urn:li:sponsoredAccount:"):
        return value
    return f"urn:li:sponsoredAccount:{value}"


def _date_parts(value: str) -> dict:
    year, month, day = [int(part) for part in value.split("-")]
    return {"year": year, "month": month, "day": day}


def _linkedin_base(path: str) -> str:
    return _platform_base("linkedin_ads", path)


def _tiktok_base(path_or_url: str) -> str:
    return _platform_base("tiktok_ads", path_or_url)


@mcp.tool()
def linkedin_ads_pipedream_accounts() -> str:
    """List connected Pipedream accounts that can be used for LinkedIn Ads proxy calls."""
    try:
        accounts = _list_pipedream_accounts("linkedin_ads")
        return _json({"accounts": accounts, "total": len(accounts)})
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def linkedin_ads_list_accounts(pipedream_account_id: str = "") -> str:
    """List LinkedIn Ads accounts visible to the connected LinkedIn user."""
    try:
        return _json(_proxy_request(
            "linkedin_ads",
            "GET",
            _linkedin_base("/v2/adAccountsV2"),
            pipedream_account_id,
            params={"q": "search", "count": 100},
        ))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def linkedin_ads_list_campaigns(linkedin_account_id: str, pipedream_account_id: str = "", count: int = 100) -> str:
    """List LinkedIn Ads campaigns for a sponsored account."""
    try:
        return _json(_proxy_request(
            "linkedin_ads",
            "GET",
            _linkedin_base("/v2/adCampaignsV2"),
            pipedream_account_id,
            params={
                "q": "search",
                "search.account.values[0]": _linkedin_urn(linkedin_account_id),
                "count": max(1, min(int(count or 100), 500)),
            },
        ))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def linkedin_ads_get_campaign(campaign_id: str, pipedream_account_id: str = "") -> str:
    """Get one LinkedIn Ads campaign by campaign id."""
    try:
        return _json(_proxy_request("linkedin_ads", "GET", _linkedin_base(f"/v2/adCampaignsV2/{campaign_id}"), pipedream_account_id))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def linkedin_ads_campaign_report(
    linkedin_account_id: str,
    start_date: str = "",
    end_date: str = "",
    pivot: str = "CAMPAIGN",
    time_granularity: str = "DAILY",
    pipedream_account_id: str = "",
) -> str:
    """Get LinkedIn Ads analytics by campaign for a date range."""
    try:
        ed = end_date or str(date.today())
        sd = start_date or str(date.today() - timedelta(days=28))
        params = {
            "q": "analytics",
            "pivot": pivot,
            "timeGranularity": time_granularity,
            "accounts[0]": _linkedin_urn(linkedin_account_id),
            "dateRange.start.year": _date_parts(sd)["year"],
            "dateRange.start.month": _date_parts(sd)["month"],
            "dateRange.start.day": _date_parts(sd)["day"],
            "dateRange.end.year": _date_parts(ed)["year"],
            "dateRange.end.month": _date_parts(ed)["month"],
            "dateRange.end.day": _date_parts(ed)["day"],
            "fields": "dateRange,pivot,pivotValue,impressions,clicks,costInLocalCurrency,externalWebsiteConversions,oneClickLeads,likes,shares,comments",
        }
        return _json(_proxy_request("linkedin_ads", "GET", _linkedin_base("/v2/adAnalyticsV2"), pipedream_account_id, params=params))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def linkedin_ads_create_campaign(linkedin_account_id: str, campaign_json: str, pipedream_account_id: str = "") -> str:
    """Create a LinkedIn Ads campaign. Pass the full LinkedIn campaign body as JSON; account is added if omitted."""
    denied = require_editor("linkedin_ads_create_campaign")
    if denied:
        return denied
    try:
        body = _parse_json_arg(campaign_json, "campaign_json", {})
        if not isinstance(body, dict):
            return _json({"error": "campaign_json must be a JSON object."})
        body.setdefault("account", _linkedin_urn(linkedin_account_id))
        return _json(_proxy_request("linkedin_ads", "POST", _linkedin_base("/v2/adCampaignsV2"), pipedream_account_id, body=body))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def linkedin_ads_update_campaign(campaign_id: str, patch_json: str, pipedream_account_id: str = "") -> str:
    """Update a LinkedIn Ads campaign with a Rest.li patch JSON body, for example {"patch":{"$set":{"status":"PAUSED"}}}."""
    denied = require_editor("linkedin_ads_update_campaign")
    if denied:
        return denied
    try:
        body = _parse_json_arg(patch_json, "patch_json", {})
        return _json(_proxy_request(
            "linkedin_ads",
            "POST",
            _linkedin_base(f"/v2/adCampaignsV2/{campaign_id}"),
            pipedream_account_id,
            body=body,
            headers={"X-Restli-Method": "partial_update"},
        ))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def linkedin_ads_create_creative(linkedin_account_id: str, creative_json: str, pipedream_account_id: str = "") -> str:
    """Create a LinkedIn Ads creative. Pass the full LinkedIn creative body as JSON; account is added if omitted."""
    denied = require_editor("linkedin_ads_create_creative")
    if denied:
        return denied
    try:
        body = _parse_json_arg(creative_json, "creative_json", {})
        if not isinstance(body, dict):
            return _json({"error": "creative_json must be a JSON object."})
        body.setdefault("account", _linkedin_urn(linkedin_account_id))
        return _json(_proxy_request("linkedin_ads", "POST", _linkedin_base("/v2/adCreativesV2"), pipedream_account_id, body=body))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def linkedin_ads_raw_request(method: str, url: str, params_json: str = "{}", body_json: str = "{}", pipedream_account_id: str = "") -> str:
    """Run a LinkedIn Ads API request through Pipedream. Editor role required because non-GET requests can mutate ads."""
    denied = require_editor("linkedin_ads_raw_request")
    if denied:
        return denied
    try:
        params = _parse_json_arg(params_json, "params_json", {})
        body = _parse_json_arg(body_json, "body_json", None)
        upstream_url = url if url.startswith("http") else _linkedin_base(url if url.startswith("/") else f"/{url}")
        return _json(_proxy_request("linkedin_ads", method, upstream_url, pipedream_account_id, params=params, body=body))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_ads_pipedream_accounts() -> str:
    """List connected Pipedream accounts that can be used for TikTok Ads proxy calls."""
    try:
        accounts = _list_pipedream_accounts("tiktok_ads")
        return _json({"accounts": accounts, "total": len(accounts)})
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_ads_list_advertisers(pipedream_account_id: str = "") -> str:
    """List TikTok advertiser accounts available to the connected TikTok Business user."""
    try:
        return _json(_proxy_request("tiktok_ads", "GET", _tiktok_base("/oauth2/advertiser/get/"), pipedream_account_id))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_ads_list_campaigns(advertiser_id: str, pipedream_account_id: str = "", page_size: int = 100) -> str:
    """List TikTok Ads campaigns for an advertiser."""
    try:
        return _json(_proxy_request(
            "tiktok_ads",
            "GET",
            _tiktok_base("/campaign/get/"),
            pipedream_account_id,
            params={"advertiser_id": advertiser_id, "page_size": max(1, min(int(page_size or 100), 1000))},
        ))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_ads_get_campaign(advertiser_id: str, campaign_id: str, pipedream_account_id: str = "") -> str:
    """Get one TikTok Ads campaign by campaign id."""
    try:
        filtering = json.dumps({"campaign_ids": [campaign_id]})
        return _json(_proxy_request(
            "tiktok_ads",
            "GET",
            _tiktok_base("/campaign/get/"),
            pipedream_account_id,
            params={"advertiser_id": advertiser_id, "filtering": filtering},
        ))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_ads_campaign_report(
    advertiser_id: str,
    start_date: str = "",
    end_date: str = "",
    dimensions_json: str = '["campaign_id"]',
    metrics_json: str = '["spend","impressions","clicks","ctr","cpc","cpm","conversion","cost_per_conversion"]',
    pipedream_account_id: str = "",
) -> str:
    """Get TikTok Ads campaign performance report for a date range."""
    try:
        ed = end_date or str(date.today())
        sd = start_date or str(date.today() - timedelta(days=28))
        dimensions = _parse_json_arg(dimensions_json, "dimensions_json", ["campaign_id"])
        metrics = _parse_json_arg(metrics_json, "metrics_json", ["spend", "impressions", "clicks"])
        return _json(_proxy_request(
            "tiktok_ads",
            "GET",
            _tiktok_base("/report/integrated/get/"),
            pipedream_account_id,
            params={
                "advertiser_id": advertiser_id,
                "report_type": "BASIC",
                "data_level": "AUCTION_CAMPAIGN",
                "dimensions": json.dumps(dimensions),
                "metrics": json.dumps(metrics),
                "start_date": sd,
                "end_date": ed,
                "page_size": 1000,
            },
        ))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_ads_create_campaign(advertiser_id: str, campaign_json: str, pipedream_account_id: str = "") -> str:
    """Create a TikTok Ads campaign. Pass the TikTok campaign fields as JSON; advertiser_id is added if omitted."""
    denied = require_editor("tiktok_ads_create_campaign")
    if denied:
        return denied
    try:
        body = _parse_json_arg(campaign_json, "campaign_json", {})
        if not isinstance(body, dict):
            return _json({"error": "campaign_json must be a JSON object."})
        body.setdefault("advertiser_id", advertiser_id)
        return _json(_proxy_request("tiktok_ads", "POST", _tiktok_base("/campaign/create/"), pipedream_account_id, body=body))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_ads_update_campaign(advertiser_id: str, campaign_id: str, updates_json: str, pipedream_account_id: str = "") -> str:
    """Update a TikTok Ads campaign. Pass fields like {"operation_status":"DISABLE"} in updates_json."""
    denied = require_editor("tiktok_ads_update_campaign")
    if denied:
        return denied
    try:
        body = _parse_json_arg(updates_json, "updates_json", {})
        if not isinstance(body, dict):
            return _json({"error": "updates_json must be a JSON object."})
        body.setdefault("advertiser_id", advertiser_id)
        body.setdefault("campaign_id", campaign_id)
        return _json(_proxy_request("tiktok_ads", "POST", _tiktok_base("/campaign/update/"), pipedream_account_id, body=body))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_ads_create_adgroup(advertiser_id: str, adgroup_json: str, pipedream_account_id: str = "") -> str:
    """Create a TikTok Ads ad group. Pass the TikTok ad group fields as JSON; advertiser_id is added if omitted."""
    denied = require_editor("tiktok_ads_create_adgroup")
    if denied:
        return denied
    try:
        body = _parse_json_arg(adgroup_json, "adgroup_json", {})
        if not isinstance(body, dict):
            return _json({"error": "adgroup_json must be a JSON object."})
        body.setdefault("advertiser_id", advertiser_id)
        return _json(_proxy_request("tiktok_ads", "POST", _tiktok_base("/adgroup/create/"), pipedream_account_id, body=body))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_ads_create_ad(advertiser_id: str, ad_json: str, pipedream_account_id: str = "") -> str:
    """Create a TikTok Ads ad. Pass the TikTok ad fields as JSON; advertiser_id is added if omitted."""
    denied = require_editor("tiktok_ads_create_ad")
    if denied:
        return denied
    try:
        body = _parse_json_arg(ad_json, "ad_json", {})
        if not isinstance(body, dict):
            return _json({"error": "ad_json must be a JSON object."})
        body.setdefault("advertiser_id", advertiser_id)
        return _json(_proxy_request("tiktok_ads", "POST", _tiktok_base("/ad/create/"), pipedream_account_id, body=body))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_ads_raw_request(method: str, path_or_url: str, params_json: str = "{}", body_json: str = "{}", pipedream_account_id: str = "") -> str:
    """Run a TikTok Business API request through Pipedream. Editor role required because non-GET requests can mutate ads."""
    denied = require_editor("tiktok_ads_raw_request")
    if denied:
        return denied
    try:
        params = _parse_json_arg(params_json, "params_json", {})
        body = _parse_json_arg(body_json, "body_json", None)
        return _json(_proxy_request("tiktok_ads", method, _tiktok_base(path_or_url), pipedream_account_id, params=params, body=body))
    except Exception as exc:
        return _json({"error": str(exc)})


# ---------------------------------------------------------------------------
# Expanded LinkedIn Ads coverage
# ---------------------------------------------------------------------------


def _linkedin_resource_get(path: str, params: dict | None = None, pipedream_account_id: str = "") -> str:
    return _json(_proxy_request("linkedin_ads", "GET", _linkedin_base(path), pipedream_account_id, params=params))


def _linkedin_resource_post(path: str, body: dict, pipedream_account_id: str = "", headers: dict | None = None) -> str:
    return _json(_proxy_request("linkedin_ads", "POST", _linkedin_base(path), pipedream_account_id, body=body, headers=headers))


@mcp.tool()
def linkedin_ads_get_resource(path: str, params_json: str = "{}", pipedream_account_id: str = "") -> str:
    """GET any LinkedIn Marketing API resource path through Pipedream, for endpoints not covered by a named tool."""
    try:
        params = _parse_json_arg(params_json, "params_json", {})
        path = path if path.startswith("/") else f"/{path}"
        return _linkedin_resource_get(path, params, pipedream_account_id)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def linkedin_ads_get_account(linkedin_account_id: str, pipedream_account_id: str = "") -> str:
    """Get a LinkedIn sponsored ad account by account id."""
    try:
        return _linkedin_resource_get(f"/v2/adAccountsV2/{linkedin_account_id}", pipedream_account_id=pipedream_account_id)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def linkedin_ads_list_campaign_groups(linkedin_account_id: str, pipedream_account_id: str = "", count: int = 100) -> str:
    """List LinkedIn campaign groups for a sponsored account."""
    try:
        return _linkedin_resource_get(
            "/v2/adCampaignGroupsV2",
            {
                "q": "search",
                "search.account.values[0]": _linkedin_urn(linkedin_account_id),
                "count": max(1, min(int(count or 100), 500)),
            },
            pipedream_account_id,
        )
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def linkedin_ads_get_campaign_group(campaign_group_id: str, pipedream_account_id: str = "") -> str:
    """Get one LinkedIn campaign group."""
    try:
        return _linkedin_resource_get(f"/v2/adCampaignGroupsV2/{campaign_group_id}", pipedream_account_id=pipedream_account_id)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def linkedin_ads_create_campaign_group(linkedin_account_id: str, campaign_group_json: str, pipedream_account_id: str = "") -> str:
    """Create a LinkedIn campaign group. Pass full LinkedIn campaign group JSON; account is added if omitted."""
    denied = require_editor("linkedin_ads_create_campaign_group")
    if denied:
        return denied
    try:
        body = _parse_json_arg(campaign_group_json, "campaign_group_json", {})
        if not isinstance(body, dict):
            return _json({"error": "campaign_group_json must be a JSON object."})
        body.setdefault("account", _linkedin_urn(linkedin_account_id))
        return _linkedin_resource_post("/v2/adCampaignGroupsV2", body, pipedream_account_id)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def linkedin_ads_update_campaign_group(campaign_group_id: str, patch_json: str, pipedream_account_id: str = "") -> str:
    """Update a LinkedIn campaign group using a Rest.li patch JSON body."""
    denied = require_editor("linkedin_ads_update_campaign_group")
    if denied:
        return denied
    try:
        body = _parse_json_arg(patch_json, "patch_json", {})
        return _linkedin_resource_post(
            f"/v2/adCampaignGroupsV2/{campaign_group_id}",
            body,
            pipedream_account_id,
            headers={"X-Restli-Method": "partial_update"},
        )
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def linkedin_ads_list_creatives(linkedin_account_id: str, campaign_id: str = "", pipedream_account_id: str = "", count: int = 100) -> str:
    """List LinkedIn ad creatives, optionally filtered by campaign."""
    try:
        params = {
            "q": "search",
            "search.account.values[0]": _linkedin_urn(linkedin_account_id),
            "count": max(1, min(int(count or 100), 500)),
        }
        if campaign_id:
            params["search.campaign.values[0]"] = f"urn:li:sponsoredCampaign:{campaign_id}"
        return _linkedin_resource_get("/v2/adCreativesV2", params, pipedream_account_id)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def linkedin_ads_get_creative(creative_id: str, pipedream_account_id: str = "") -> str:
    """Get one LinkedIn ad creative."""
    try:
        return _linkedin_resource_get(f"/v2/adCreativesV2/{creative_id}", pipedream_account_id=pipedream_account_id)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def linkedin_ads_update_creative(creative_id: str, patch_json: str, pipedream_account_id: str = "") -> str:
    """Update a LinkedIn creative using a Rest.li patch JSON body."""
    denied = require_editor("linkedin_ads_update_creative")
    if denied:
        return denied
    try:
        body = _parse_json_arg(patch_json, "patch_json", {})
        return _linkedin_resource_post(
            f"/v2/adCreativesV2/{creative_id}",
            body,
            pipedream_account_id,
            headers={"X-Restli-Method": "partial_update"},
        )
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def linkedin_ads_list_conversions(linkedin_account_id: str, pipedream_account_id: str = "", count: int = 100) -> str:
    """List LinkedIn conversion rules for a sponsored account."""
    try:
        return _linkedin_resource_get(
            "/v2/conversions",
            {"q": "account", "account": _linkedin_urn(linkedin_account_id), "count": max(1, min(int(count or 100), 500))},
            pipedream_account_id,
        )
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def linkedin_ads_create_conversion(linkedin_account_id: str, conversion_json: str, pipedream_account_id: str = "") -> str:
    """Create a LinkedIn conversion rule. Pass full conversion JSON; account is added if omitted."""
    denied = require_editor("linkedin_ads_create_conversion")
    if denied:
        return denied
    try:
        body = _parse_json_arg(conversion_json, "conversion_json", {})
        if not isinstance(body, dict):
            return _json({"error": "conversion_json must be a JSON object."})
        body.setdefault("account", _linkedin_urn(linkedin_account_id))
        return _linkedin_resource_post("/v2/conversions", body, pipedream_account_id)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def linkedin_ads_update_conversion(conversion_id: str, patch_json: str, pipedream_account_id: str = "") -> str:
    """Update a LinkedIn conversion rule using a Rest.li patch JSON body."""
    denied = require_editor("linkedin_ads_update_conversion")
    if denied:
        return denied
    try:
        body = _parse_json_arg(patch_json, "patch_json", {})
        return _linkedin_resource_post(
            f"/v2/conversions/{conversion_id}",
            body,
            pipedream_account_id,
            headers={"X-Restli-Method": "partial_update"},
        )
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def linkedin_ads_list_targeting_facets(pipedream_account_id: str = "") -> str:
    """List LinkedIn targeting facets available for ad targeting."""
    try:
        return _linkedin_resource_get("/v2/adTargetingFacets", pipedream_account_id=pipedream_account_id)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def linkedin_ads_find_targeting_entities(facet: str, query: str = "", pipedream_account_id: str = "", count: int = 50) -> str:
    """Search LinkedIn targeting entities for a facet such as locations, industries, skills, titles, or companies."""
    try:
        params = {"q": "adTargetingFacet", "facet": facet, "count": max(1, min(int(count or 50), 100))}
        if query:
            params["query"] = query
        return _linkedin_resource_get("/v2/adTargetingEntities", params, pipedream_account_id)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def linkedin_ads_audience_counts(targeting_json: str, pipedream_account_id: str = "") -> str:
    """Estimate LinkedIn audience size for a targeting criteria JSON payload."""
    try:
        params = _parse_json_arg(targeting_json, "targeting_json", {})
        return _linkedin_resource_get("/v2/audienceCountsV2", params, pipedream_account_id)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def linkedin_ads_campaign_group_report(
    linkedin_account_id: str,
    start_date: str = "",
    end_date: str = "",
    time_granularity: str = "DAILY",
    pipedream_account_id: str = "",
) -> str:
    """Get LinkedIn Ads analytics pivoted by campaign group."""
    return linkedin_ads_campaign_report(linkedin_account_id, start_date, end_date, "CAMPAIGN_GROUP", time_granularity, pipedream_account_id)


@mcp.tool()
def linkedin_ads_creative_report(
    linkedin_account_id: str,
    start_date: str = "",
    end_date: str = "",
    time_granularity: str = "DAILY",
    pipedream_account_id: str = "",
) -> str:
    """Get LinkedIn Ads analytics pivoted by creative."""
    return linkedin_ads_campaign_report(linkedin_account_id, start_date, end_date, "CREATIVE", time_granularity, pipedream_account_id)


# ---------------------------------------------------------------------------
# Expanded TikTok Ads coverage
# ---------------------------------------------------------------------------


def _tiktok_get(path: str, params: dict | None = None, pipedream_account_id: str = "") -> str:
    return _json(_proxy_request("tiktok_ads", "GET", _tiktok_base(path), pipedream_account_id, params=params))


def _tiktok_post(path: str, body: dict, pipedream_account_id: str = "") -> str:
    return _json(_proxy_request("tiktok_ads", "POST", _tiktok_base(path), pipedream_account_id, body=body))


@mcp.tool()
def tiktok_ads_get_resource(path_or_url: str, params_json: str = "{}", pipedream_account_id: str = "") -> str:
    """GET any TikTok Business API resource through Pipedream, for endpoints not covered by a named tool."""
    try:
        params = _parse_json_arg(params_json, "params_json", {})
        return _tiktok_get(path_or_url, params, pipedream_account_id)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_ads_advertiser_info(advertiser_id: str, pipedream_account_id: str = "") -> str:
    """Get TikTok advertiser account info."""
    try:
        return _tiktok_get("/advertiser/info/", {"advertiser_ids": json.dumps([advertiser_id])}, pipedream_account_id)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_ads_account_balance(advertiser_id: str, pipedream_account_id: str = "") -> str:
    """Get TikTok advertiser account balance."""
    try:
        return _tiktok_get("/advertiser/balance/get/", {"advertiser_id": advertiser_id}, pipedream_account_id)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_ads_list_adgroups(advertiser_id: str, campaign_id: str = "", pipedream_account_id: str = "", page_size: int = 100) -> str:
    """List TikTok ad groups, optionally filtered by campaign."""
    try:
        filtering: dict[str, Any] = {}
        if campaign_id:
            filtering["campaign_ids"] = [campaign_id]
        return _tiktok_get(
            "/adgroup/get/",
            {
                "advertiser_id": advertiser_id,
                "filtering": json.dumps(filtering) if filtering else "",
                "page_size": max(1, min(int(page_size or 100), 1000)),
            },
            pipedream_account_id,
        )
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_ads_get_adgroup(advertiser_id: str, adgroup_id: str, pipedream_account_id: str = "") -> str:
    """Get one TikTok ad group."""
    try:
        return _tiktok_get(
            "/adgroup/get/",
            {"advertiser_id": advertiser_id, "filtering": json.dumps({"adgroup_ids": [adgroup_id]})},
            pipedream_account_id,
        )
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_ads_update_adgroup(advertiser_id: str, adgroup_id: str, updates_json: str, pipedream_account_id: str = "") -> str:
    """Update a TikTok ad group."""
    denied = require_editor("tiktok_ads_update_adgroup")
    if denied:
        return denied
    try:
        body = _parse_json_arg(updates_json, "updates_json", {})
        if not isinstance(body, dict):
            return _json({"error": "updates_json must be a JSON object."})
        body.setdefault("advertiser_id", advertiser_id)
        body.setdefault("adgroup_id", adgroup_id)
        return _tiktok_post("/adgroup/update/", body, pipedream_account_id)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_ads_list_ads(advertiser_id: str, adgroup_id: str = "", campaign_id: str = "", pipedream_account_id: str = "", page_size: int = 100) -> str:
    """List TikTok ads, optionally filtered by campaign or ad group."""
    try:
        filtering: dict[str, Any] = {}
        if adgroup_id:
            filtering["adgroup_ids"] = [adgroup_id]
        if campaign_id:
            filtering["campaign_ids"] = [campaign_id]
        return _tiktok_get(
            "/ad/get/",
            {
                "advertiser_id": advertiser_id,
                "filtering": json.dumps(filtering) if filtering else "",
                "page_size": max(1, min(int(page_size or 100), 1000)),
            },
            pipedream_account_id,
        )
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_ads_get_ad(advertiser_id: str, ad_id: str, pipedream_account_id: str = "") -> str:
    """Get one TikTok ad."""
    try:
        return _tiktok_get(
            "/ad/get/",
            {"advertiser_id": advertiser_id, "filtering": json.dumps({"ad_ids": [ad_id]})},
            pipedream_account_id,
        )
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_ads_update_ad(advertiser_id: str, ad_id: str, updates_json: str, pipedream_account_id: str = "") -> str:
    """Update a TikTok ad."""
    denied = require_editor("tiktok_ads_update_ad")
    if denied:
        return denied
    try:
        body = _parse_json_arg(updates_json, "updates_json", {})
        if not isinstance(body, dict):
            return _json({"error": "updates_json must be a JSON object."})
        body.setdefault("advertiser_id", advertiser_id)
        body.setdefault("ad_id", ad_id)
        return _tiktok_post("/ad/update/", body, pipedream_account_id)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_ads_adgroup_report(
    advertiser_id: str,
    start_date: str = "",
    end_date: str = "",
    dimensions_json: str = '["adgroup_id"]',
    metrics_json: str = '["spend","impressions","clicks","ctr","cpc","cpm","conversion","cost_per_conversion"]',
    pipedream_account_id: str = "",
) -> str:
    """Get TikTok ad-group-level report."""
    try:
        ed = end_date or str(date.today())
        sd = start_date or str(date.today() - timedelta(days=28))
        return _tiktok_get(
            "/report/integrated/get/",
            {
                "advertiser_id": advertiser_id,
                "report_type": "BASIC",
                "data_level": "AUCTION_ADGROUP",
                "dimensions": json.dumps(_parse_json_arg(dimensions_json, "dimensions_json", ["adgroup_id"])),
                "metrics": json.dumps(_parse_json_arg(metrics_json, "metrics_json", ["spend", "impressions", "clicks"])),
                "start_date": sd,
                "end_date": ed,
                "page_size": 1000,
            },
            pipedream_account_id,
        )
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_ads_ad_report(
    advertiser_id: str,
    start_date: str = "",
    end_date: str = "",
    dimensions_json: str = '["ad_id"]',
    metrics_json: str = '["spend","impressions","clicks","ctr","cpc","cpm","conversion","cost_per_conversion"]',
    pipedream_account_id: str = "",
) -> str:
    """Get TikTok ad-level report."""
    try:
        ed = end_date or str(date.today())
        sd = start_date or str(date.today() - timedelta(days=28))
        return _tiktok_get(
            "/report/integrated/get/",
            {
                "advertiser_id": advertiser_id,
                "report_type": "BASIC",
                "data_level": "AUCTION_AD",
                "dimensions": json.dumps(_parse_json_arg(dimensions_json, "dimensions_json", ["ad_id"])),
                "metrics": json.dumps(_parse_json_arg(metrics_json, "metrics_json", ["spend", "impressions", "clicks"])),
                "start_date": sd,
                "end_date": ed,
                "page_size": 1000,
            },
            pipedream_account_id,
        )
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_ads_custom_report(advertiser_id: str, report_json: str, pipedream_account_id: str = "") -> str:
    """Run a custom TikTok integrated report. Pass any TikTok report params as JSON."""
    try:
        params = _parse_json_arg(report_json, "report_json", {})
        if not isinstance(params, dict):
            return _json({"error": "report_json must be a JSON object."})
        params.setdefault("advertiser_id", advertiser_id)
        return _tiktok_get("/report/integrated/get/", params, pipedream_account_id)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_ads_list_pixels(advertiser_id: str, pipedream_account_id: str = "") -> str:
    """List TikTok pixels for an advertiser."""
    try:
        return _tiktok_get("/pixel/list/", {"advertiser_id": advertiser_id}, pipedream_account_id)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_ads_create_pixel(advertiser_id: str, pixel_json: str, pipedream_account_id: str = "") -> str:
    """Create a TikTok pixel. Pass pixel fields as JSON; advertiser_id is added if omitted."""
    denied = require_editor("tiktok_ads_create_pixel")
    if denied:
        return denied
    try:
        body = _parse_json_arg(pixel_json, "pixel_json", {})
        if not isinstance(body, dict):
            return _json({"error": "pixel_json must be a JSON object."})
        body.setdefault("advertiser_id", advertiser_id)
        return _tiktok_post("/pixel/create/", body, pipedream_account_id)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_ads_update_pixel(advertiser_id: str, pixel_id: str, updates_json: str, pipedream_account_id: str = "") -> str:
    """Update a TikTok pixel."""
    denied = require_editor("tiktok_ads_update_pixel")
    if denied:
        return denied
    try:
        body = _parse_json_arg(updates_json, "updates_json", {})
        if not isinstance(body, dict):
            return _json({"error": "updates_json must be a JSON object."})
        body.setdefault("advertiser_id", advertiser_id)
        body.setdefault("pixel_id", pixel_id)
        return _tiktok_post("/pixel/update/", body, pipedream_account_id)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_ads_list_custom_audiences(advertiser_id: str, pipedream_account_id: str = "", page_size: int = 100) -> str:
    """List TikTok custom audiences."""
    try:
        return _tiktok_get(
            "/dmp/custom_audience/list/",
            {"advertiser_id": advertiser_id, "page_size": max(1, min(int(page_size or 100), 1000))},
            pipedream_account_id,
        )
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_ads_create_custom_audience(advertiser_id: str, audience_json: str, pipedream_account_id: str = "") -> str:
    """Create a TikTok custom audience. Pass TikTok audience fields as JSON; advertiser_id is added if omitted."""
    denied = require_editor("tiktok_ads_create_custom_audience")
    if denied:
        return denied
    try:
        body = _parse_json_arg(audience_json, "audience_json", {})
        if not isinstance(body, dict):
            return _json({"error": "audience_json must be a JSON object."})
        body.setdefault("advertiser_id", advertiser_id)
        return _tiktok_post("/dmp/custom_audience/create/", body, pipedream_account_id)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_ads_update_custom_audience(advertiser_id: str, custom_audience_id: str, updates_json: str, pipedream_account_id: str = "") -> str:
    """Update a TikTok custom audience."""
    denied = require_editor("tiktok_ads_update_custom_audience")
    if denied:
        return denied
    try:
        body = _parse_json_arg(updates_json, "updates_json", {})
        if not isinstance(body, dict):
            return _json({"error": "updates_json must be a JSON object."})
        body.setdefault("advertiser_id", advertiser_id)
        body.setdefault("custom_audience_id", custom_audience_id)
        return _tiktok_post("/dmp/custom_audience/update/", body, pipedream_account_id)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_ads_delete_custom_audience(advertiser_id: str, custom_audience_id: str, pipedream_account_id: str = "") -> str:
    """Delete a TikTok custom audience."""
    denied = require_editor("tiktok_ads_delete_custom_audience")
    if denied:
        return denied
    try:
        return _tiktok_post(
            "/dmp/custom_audience/delete/",
            {"advertiser_id": advertiser_id, "custom_audience_ids": [custom_audience_id]},
            pipedream_account_id,
        )
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_ads_create_lookalike_audience(advertiser_id: str, audience_json: str, pipedream_account_id: str = "") -> str:
    """Create a TikTok lookalike audience. Pass TikTok lookalike fields as JSON; advertiser_id is added if omitted."""
    denied = require_editor("tiktok_ads_create_lookalike_audience")
    if denied:
        return denied
    try:
        body = _parse_json_arg(audience_json, "audience_json", {})
        if not isinstance(body, dict):
            return _json({"error": "audience_json must be a JSON object."})
        body.setdefault("advertiser_id", advertiser_id)
        return _tiktok_post("/dmp/lookalike/create/", body, pipedream_account_id)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_ads_list_images(advertiser_id: str, pipedream_account_id: str = "", page_size: int = 100) -> str:
    """List TikTok image assets."""
    try:
        return _tiktok_get("/file/image/ad/search/", {"advertiser_id": advertiser_id, "page_size": max(1, min(int(page_size or 100), 1000))}, pipedream_account_id)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_ads_upload_image(advertiser_id: str, image_json: str, pipedream_account_id: str = "") -> str:
    """Upload or register a TikTok ad image. Pass TikTok image upload fields as JSON; advertiser_id is added if omitted."""
    denied = require_editor("tiktok_ads_upload_image")
    if denied:
        return denied
    try:
        body = _parse_json_arg(image_json, "image_json", {})
        if not isinstance(body, dict):
            return _json({"error": "image_json must be a JSON object."})
        body.setdefault("advertiser_id", advertiser_id)
        return _tiktok_post("/file/image/ad/upload/", body, pipedream_account_id)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_ads_list_videos(advertiser_id: str, pipedream_account_id: str = "", page_size: int = 100) -> str:
    """List TikTok video assets."""
    try:
        return _tiktok_get("/file/video/ad/search/", {"advertiser_id": advertiser_id, "page_size": max(1, min(int(page_size or 100), 1000))}, pipedream_account_id)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_ads_upload_video(advertiser_id: str, video_json: str, pipedream_account_id: str = "") -> str:
    """Upload or register a TikTok ad video. Pass TikTok video upload fields as JSON; advertiser_id is added if omitted."""
    denied = require_editor("tiktok_ads_upload_video")
    if denied:
        return denied
    try:
        body = _parse_json_arg(video_json, "video_json", {})
        if not isinstance(body, dict):
            return _json({"error": "video_json must be a JSON object."})
        body.setdefault("advertiser_id", advertiser_id)
        return _tiktok_post("/file/video/ad/upload/", body, pipedream_account_id)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_ads_targeting_options(advertiser_id: str, option_path: str, params_json: str = "{}", pipedream_account_id: str = "") -> str:
    """Query TikTok targeting/tool endpoints, e.g. /tool/region/ or /tool/interest_keyword/recommend/."""
    try:
        params = _parse_json_arg(params_json, "params_json", {})
        if not isinstance(params, dict):
            return _json({"error": "params_json must be a JSON object."})
        params.setdefault("advertiser_id", advertiser_id)
        return _tiktok_get(option_path, params, pipedream_account_id)
    except Exception as exc:
        return _json({"error": str(exc)})
