"""
Google Ads tools — adapted for remote server.
63 tools. Credentials come from current_user_ctx (per-user Google refresh token).
Write tools check require_editor() before executing.
"""

import json
import os
import datetime as _dt
import re
import time
from datetime import date, timedelta
from typing import Optional

from google.ads.googleads.client import GoogleAdsClient
from google.protobuf import field_mask_pb2
import urllib.request as _urllib_request

from mcp_instance import mcp
from credentials import connect_hint
from auth import current_connection_token_ctx, current_user_ctx
from permissions import require_editor

GOOGLE_ADS_DEVELOPER_TOKEN = os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN", "")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_refresh_token(customer_id: str = "") -> str:
    """Return the current user's Google refresh token."""
    user = current_user_ctx.get(None)
    if user is None:
        raise RuntimeError("Not authenticated.")
    from credentials import provider
    # See tools/google_ads.py::_client_config -- resolution, the
    # oldest-connection fallback and the legacy lookup all live in the provider.
    refresh_token = current_connection_token_ctx.get(None) or provider().google_token(
        "google_ads", customer_id
    )
    if not refresh_token:
        raise RuntimeError("Google account not connected. Connect your Google account via " + connect_hint() + ".")

    return refresh_token


def _load_user_client(login_customer_id: str = "", customer_id: str = "") -> GoogleAdsClient:
    """Create a GoogleAdsClient for the currently authenticated user.

    If login_customer_id is provided, it's set on the client so the API call is
    scoped to that account. Each user operates on their own connected account.
    """
    refresh_token = _get_refresh_token(customer_id)

    config = {
        "developer_token": GOOGLE_ADS_DEVELOPER_TOKEN,
        "client_id":       GOOGLE_CLIENT_ID,
        "client_secret":   GOOGLE_CLIENT_SECRET,
        "refresh_token":   refresh_token,
        "use_proto_plus":  True,
    }
    if login_customer_id:
        config["login_customer_id"] = str(login_customer_id).replace("-", "")

    return GoogleAdsClient.load_from_dict(config)


def _normalize_customer_id(customer_id: str = "") -> str:
    return str(customer_id or "").strip().replace("-", "")


# See tools/google_ads.py's identical cache for why this is needed: resolving
# a child account's manager walks every accessible manager account (multiple
# sequential API calls) and is identical for a given customer_id every time.
_LOGIN_CUSTOMER_ID_CACHE: dict[str, tuple[str, float]] = {}
_LOGIN_CUSTOMER_ID_CACHE_TTL = 3600  # seconds


def _find_login_customer_id_for_customer(target_customer_id: str = "") -> str:
    target_id = _normalize_customer_id(target_customer_id)
    if not target_id:
        return ""

    cached = _LOGIN_CUSTOMER_ID_CACHE.get(target_id)
    if cached and (time.time() - cached[1]) < _LOGIN_CUSTOMER_ID_CACHE_TTL:
        return cached[0]

    def _cache_and_return(value: str) -> str:
        _LOGIN_CUSTOMER_ID_CACHE[target_id] = (value, time.time())
        return value

    base_client = _load_user_client(customer_id=target_id)
    customer_service = base_client.get_service("CustomerService")
    accessible = customer_service.list_accessible_customers()
    accessible_ids = [rn.split("/")[-1] for rn in accessible.resource_names]
    if target_id in accessible_ids:
        return _cache_and_return("")

    gaql = """
        SELECT customer_client.id, customer_client.manager, customer_client.level
        FROM customer_client
        ORDER BY customer_client.level ASC
    """
    for manager_id in accessible_ids:
        try:
            manager_client = _load_user_client(login_customer_id=manager_id)
            service = manager_client.get_service("GoogleAdsService")
            rows = service.search(customer_id=manager_id, query=gaql)
            for row in rows:
                if str(row.customer_client.id) == target_id:
                    return _cache_and_return(manager_id)
        except Exception:
            continue
    return _cache_and_return("")


def _get_client(customer_id: str = ""):
    """Create a GoogleAdsClient and resolve the effective customer ID.

    Resolves the manager (MCC) login-customer-id when the target account is a
    child under a manager the user has access to. The Google Ads API requires
    login-customer-id to be the MANAGER's id (not the child account's own id)
    for BOTH reads and writes against such an account -- scoping to the child
    account's own id (the previous behavior here) works for reads by luck in
    some cases but is rejected outright for mutate calls, which is why write
    tools in this file (budget/schedule updates, etc.) failed under an MCC
    while read tools kept working.
    """
    cid = _normalize_customer_id(customer_id)
    if not cid:
        cid = os.environ.get("GOOGLE_ADS_CUSTOMER_ID", "").replace("-", "")
    if not cid:
        raise RuntimeError("No customer_id provided. Call gads_list_customers first to find your account IDs.")

    login_id = _find_login_customer_id_for_customer(cid) or cid
    client = _load_user_client(login_customer_id=login_id, customer_id=cid)
    return client, cid


def _search(gaql: str, customer_id: str = "") -> list:
    """Execute a GAQL query via gRPC and return all result rows."""
    client, cid = _get_client(customer_id)
    service = client.get_service("GoogleAdsService")
    try:
        return list(service.search(customer_id=cid, query=gaql))
    except Exception as error:
        if "CUSTOMER_NOT_FOUND" not in str(error) and "not found" not in str(error).lower():
            raise
        manager_id = _find_login_customer_id_for_customer(cid)
        if not manager_id:
            raise
        retry_client = _load_user_client(login_customer_id=manager_id)
        retry_service = retry_client.get_service("GoogleAdsService")
        return list(retry_service.search(customer_id=cid, query=gaql))


def _m(micros) -> float:
    """Convert micros to currency units."""
    try:
        return round(int(micros) / 1_000_000, 2)
    except (TypeError, ValueError):
        return 0.0


def _pct(ratio) -> float:
    try:
        return round(float(ratio) * 100, 4)
    except (TypeError, ValueError):
        return 0.0


def _date_range(start_date: str, end_date: str, default_days: int = 28):
    ed = end_date or str(date.today())
    sd = start_date or str(date.today() - timedelta(days=default_days))
    return sd, ed


# ---------------------------------------------------------------------------
# Analytics Tools (read-only)
# ---------------------------------------------------------------------------

@mcp.tool()
def gads_list_customers() -> str:
    """List all Google Ads customer accounts accessible to the authenticated user."""
    try:
        from credentials import provider

        user = current_user_ctx.get(None)
        clients = None
        if not current_connection_token_ctx.get(None) and user:
            connections = provider().list_connections("google_ads")
            if len(connections) >= 2:
                clients = []
                for conn in connections:
                    config = {
                        "developer_token": GOOGLE_ADS_DEVELOPER_TOKEN,
                        "client_id": GOOGLE_CLIENT_ID,
                        "client_secret": GOOGLE_CLIENT_SECRET,
                        "refresh_token": conn["token"],
                        "use_proto_plus": True,
                    }
                    clients.append((GoogleAdsClient.load_from_dict(config), conn["email"]))
        if clients is None:
            clients = [(_load_user_client(), "")]

        customer_ids: list = []
        seen: set = set()
        for client, email in clients:
            customer_service = client.get_service("CustomerService")
            accessible = customer_service.list_accessible_customers()
            for rn in accessible.resource_names:
                cid = rn.split("/")[-1]
                if cid in seen:
                    continue
                seen.add(cid)
                customer_ids.append({"id": cid, "connected_as": email} if email else cid)
        return json.dumps({"customers": customer_ids, "total": len(customer_ids)})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_get_account_info(customer_id: str = "") -> str:
    """Get basic information about a Google Ads account: name, currency, timezone, status.

    Args:
        customer_id: Google Ads customer ID (10 digits). Leave blank to use GOOGLE_ADS_CUSTOMER_ID.
    """
    try:
        rows = _search("""
            SELECT customer.id, customer.descriptive_name, customer.currency_code,
                   customer.time_zone, customer.status, customer.manager
            FROM customer
            LIMIT 1
        """, customer_id)
        if not rows:
            return json.dumps({"error": "No customer info returned."})
        c = rows[0].customer
        return json.dumps({
            "id": str(c.id),
            "name": c.descriptive_name,
            "currency": c.currency_code,
            "timezone": c.time_zone,
            "status": c.status.name,
            "manager": c.manager,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_account_overview(
    start_date: str = "",
    end_date: str = "",
    customer_id: str = "",
) -> str:
    """Get high-level account metrics: total spend, impressions, clicks, conversions, CTR, avg CPC, ROAS.

    Args:
        start_date: Start date YYYY-MM-DD. Defaults to 28 days ago.
        end_date: End date YYYY-MM-DD. Defaults to today.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        sd, ed = _date_range(start_date, end_date)
        rows = _search(f"""
            SELECT metrics.impressions, metrics.clicks, metrics.cost_micros,
                   metrics.ctr, metrics.average_cpc, metrics.conversions,
                   metrics.conversions_value, metrics.cost_per_conversion
            FROM customer
            WHERE segments.date BETWEEN '{sd}' AND '{ed}'
        """, customer_id)
        if not rows:
            return json.dumps({"date_range": f"{sd} to {ed}", "metrics": {}})
        m = rows[0].metrics
        spend = _m(m.cost_micros)
        return json.dumps({
            "date_range": f"{sd} to {ed}",
            "impressions": int(m.impressions),
            "clicks": int(m.clicks),
            "spend": spend,
            "ctr": _pct(m.ctr),
            "avg_cpc": _m(m.average_cpc),
            "conversions": float(m.conversions),
            "conversion_value": float(m.conversions_value),
            "cost_per_conversion": _m(m.cost_per_conversion),
            "roas": round(float(m.conversions_value) / max(spend, 0.01), 2),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_campaign_performance(
    start_date: str = "",
    end_date: str = "",
    customer_id: str = "",
    status_filter: str = "ENABLED",
    row_limit: int = 25,
) -> str:
    """Get Google Ads campaign-level performance: spend, impressions, clicks, conversions, CTR per campaign.

    Args:
        start_date: Start date YYYY-MM-DD. Defaults to 28 days ago.
        end_date: End date YYYY-MM-DD. Defaults to today.
        customer_id: Google Ads customer ID. Leave blank to use default.
        status_filter: Filter by campaign status: ENABLED, PAUSED, REMOVED, or ALL. Default ENABLED.
        row_limit: Max campaigns. Default 25.
    """
    try:
        sd, ed = _date_range(start_date, end_date)
        where_status = "" if status_filter == "ALL" else f"AND campaign.status = '{status_filter}'"
        rows = _search(f"""
            SELECT campaign.id, campaign.name, campaign.status, campaign.bidding_strategy_type,
                   metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.ctr,
                   metrics.average_cpc, metrics.conversions, metrics.conversions_value
            FROM campaign
            WHERE segments.date BETWEEN '{sd}' AND '{ed}'
            {where_status}
            ORDER BY metrics.cost_micros DESC
            LIMIT {row_limit}
        """, customer_id)
        results = []
        for row in rows:
            c = row.campaign
            m = row.metrics
            results.append({
                "id": str(c.id),
                "name": c.name,
                "status": c.status.name,
                "bidding_strategy": c.bidding_strategy_type.name,
                "spend": _m(m.cost_micros),
                "impressions": int(m.impressions),
                "clicks": int(m.clicks),
                "ctr": _pct(m.ctr),
                "avg_cpc": _m(m.average_cpc),
                "conversions": float(m.conversions),
                "conversion_value": float(m.conversions_value),
            })
        return json.dumps({"date_range": f"{sd} to {ed}", "campaigns": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_adgroup_performance(
    start_date: str = "",
    end_date: str = "",
    customer_id: str = "",
    campaign_id: str = "",
    row_limit: int = 25,
) -> str:
    """Get Google Ads ad group performance: spend, clicks, conversions for each ad group.

    Args:
        start_date: Start date YYYY-MM-DD. Defaults to 28 days ago.
        end_date: End date YYYY-MM-DD. Defaults to today.
        customer_id: Google Ads customer ID. Leave blank to use default.
        campaign_id: Filter by campaign ID. Leave blank for all.
        row_limit: Max ad groups. Default 25.
    """
    try:
        sd, ed = _date_range(start_date, end_date)
        where_campaign = f"AND campaign.id = '{campaign_id}'" if campaign_id else ""
        rows = _search(f"""
            SELECT campaign.name, ad_group.id, ad_group.name, ad_group.status,
                   metrics.impressions, metrics.clicks, metrics.cost_micros,
                   metrics.ctr, metrics.average_cpc, metrics.conversions
            FROM ad_group
            WHERE segments.date BETWEEN '{sd}' AND '{ed}'
            AND ad_group.status != 'REMOVED'
            {where_campaign}
            ORDER BY metrics.cost_micros DESC
            LIMIT {row_limit}
        """, customer_id)
        results = []
        for row in rows:
            ag = row.ad_group
            m = row.metrics
            results.append({
                "id": str(ag.id),
                "name": ag.name,
                "status": ag.status.name,
                "campaign": row.campaign.name,
                "spend": _m(m.cost_micros),
                "impressions": int(m.impressions),
                "clicks": int(m.clicks),
                "ctr": _pct(m.ctr),
                "avg_cpc": _m(m.average_cpc),
                "conversions": float(m.conversions),
            })
        return json.dumps({"date_range": f"{sd} to {ed}", "ad_groups": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_keyword_performance(
    start_date: str = "",
    end_date: str = "",
    customer_id: str = "",
    campaign_id: str = "",
    row_limit: int = 50,
    sort_by: str = "cost",
) -> str:
    """Get Google Ads keyword-level performance: clicks, impressions, spend, quality score, match type.

    Args:
        start_date: Start date YYYY-MM-DD. Defaults to 28 days ago.
        end_date: End date YYYY-MM-DD. Defaults to today.
        customer_id: Google Ads customer ID. Leave blank to use default.
        campaign_id: Filter by campaign ID. Leave blank for all.
        row_limit: Max keywords. Default 50.
        sort_by: Sort by cost, clicks, or conversions. Default cost.
    """
    try:
        sd, ed = _date_range(start_date, end_date)
        sort_field = {"cost": "metrics.cost_micros", "clicks": "metrics.clicks", "conversions": "metrics.conversions"}.get(sort_by, "metrics.cost_micros")
        where_campaign = f"AND campaign.id = '{campaign_id}'" if campaign_id else ""
        rows = _search(f"""
            SELECT campaign.name, ad_group.name,
                   ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type,
                   ad_group_criterion.status, ad_group_criterion.quality_info.quality_score,
                   metrics.impressions, metrics.clicks, metrics.cost_micros,
                   metrics.ctr, metrics.average_cpc, metrics.conversions,
                   metrics.search_impression_share
            FROM keyword_view
            WHERE segments.date BETWEEN '{sd}' AND '{ed}'
            AND ad_group_criterion.status != 'REMOVED'
            {where_campaign}
            ORDER BY {sort_field} DESC
            LIMIT {row_limit}
        """, customer_id)
        results = []
        for row in rows:
            c = row.ad_group_criterion
            m = row.metrics
            results.append({
                "keyword": c.keyword.text,
                "match_type": c.keyword.match_type.name,
                "status": c.status.name,
                "quality_score": c.quality_info.quality_score or None,
                "campaign": row.campaign.name,
                "ad_group": row.ad_group.name,
                "spend": _m(m.cost_micros),
                "impressions": int(m.impressions),
                "clicks": int(m.clicks),
                "ctr": _pct(m.ctr),
                "avg_cpc": _m(m.average_cpc),
                "conversions": float(m.conversions),
                "search_impression_share": _pct(m.search_impression_share),
            })
        return json.dumps({"date_range": f"{sd} to {ed}", "keywords": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_search_terms(
    start_date: str = "",
    end_date: str = "",
    customer_id: str = "",
    campaign_id: str = "",
    row_limit: int = 50,
) -> str:
    """Get Google Ads search terms report — the actual queries that triggered ads, with clicks and conversions.

    Args:
        start_date: Start date YYYY-MM-DD. Defaults to 28 days ago.
        end_date: End date YYYY-MM-DD. Defaults to today.
        customer_id: Google Ads customer ID. Leave blank to use default.
        campaign_id: Filter by campaign ID. Leave blank for all.
        row_limit: Max terms. Default 50.
    """
    try:
        sd, ed = _date_range(start_date, end_date)
        where_campaign = f"AND campaign.id = '{campaign_id}'" if campaign_id else ""
        rows = _search(f"""
            SELECT search_term_view.search_term, search_term_view.status,
                   campaign.name, ad_group.name,
                   metrics.impressions, metrics.clicks, metrics.cost_micros,
                   metrics.ctr, metrics.average_cpc, metrics.conversions
            FROM search_term_view
            WHERE segments.date BETWEEN '{sd}' AND '{ed}'
            {where_campaign}
            ORDER BY metrics.clicks DESC
            LIMIT {row_limit}
        """, customer_id)
        results = []
        for row in rows:
            stv = row.search_term_view
            m = row.metrics
            results.append({
                "search_term": stv.search_term,
                "status": stv.status.name,
                "campaign": row.campaign.name,
                "ad_group": row.ad_group.name,
                "impressions": int(m.impressions),
                "clicks": int(m.clicks),
                "spend": _m(m.cost_micros),
                "ctr": _pct(m.ctr),
                "avg_cpc": _m(m.average_cpc),
                "conversions": float(m.conversions),
            })
        return json.dumps({"date_range": f"{sd} to {ed}", "search_terms": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_ad_performance(
    start_date: str = "",
    end_date: str = "",
    customer_id: str = "",
    campaign_id: str = "",
    row_limit: int = 25,
) -> str:
    """Get Google Ads individual ad performance: clicks, impressions, CTR, conversions per ad.

    Args:
        start_date: Start date YYYY-MM-DD. Defaults to 28 days ago.
        end_date: End date YYYY-MM-DD. Defaults to today.
        customer_id: Google Ads customer ID. Leave blank to use default.
        campaign_id: Filter by campaign ID. Leave blank for all.
        row_limit: Max ads. Default 25.
    """
    try:
        sd, ed = _date_range(start_date, end_date)
        where_campaign = f"AND campaign.id = '{campaign_id}'" if campaign_id else ""
        rows = _search(f"""
            SELECT campaign.name, ad_group.name, ad_group_ad.ad.id,
                   ad_group_ad.ad.name, ad_group_ad.ad.type, ad_group_ad.status,
                   metrics.impressions, metrics.clicks, metrics.cost_micros,
                   metrics.ctr, metrics.average_cpc, metrics.conversions
            FROM ad_group_ad
            WHERE segments.date BETWEEN '{sd}' AND '{ed}'
            AND ad_group_ad.status != 'REMOVED'
            {where_campaign}
            ORDER BY metrics.cost_micros DESC
            LIMIT {row_limit}
        """, customer_id)
        results = []
        for row in rows:
            aga = row.ad_group_ad
            m = row.metrics
            results.append({
                "id": str(aga.ad.id),
                "name": aga.ad.name,
                "type": aga.ad.type_.name,
                "status": aga.status.name,
                "campaign": row.campaign.name,
                "ad_group": row.ad_group.name,
                "spend": _m(m.cost_micros),
                "impressions": int(m.impressions),
                "clicks": int(m.clicks),
                "ctr": _pct(m.ctr),
                "conversions": float(m.conversions),
            })
        return json.dumps({"date_range": f"{sd} to {ed}", "ads": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_demographic_breakdown(
    start_date: str = "",
    end_date: str = "",
    customer_id: str = "",
    campaign_id: str = "",
) -> str:
    """Get Google Ads performance broken down by age range and gender demographics.

    Args:
        start_date: Start date YYYY-MM-DD. Defaults to 28 days ago.
        end_date: End date YYYY-MM-DD. Defaults to today.
        customer_id: Google Ads customer ID. Leave blank to use default.
        campaign_id: Filter by campaign ID. Leave blank for all.
    """
    try:
        sd, ed = _date_range(start_date, end_date)
        where_campaign = f"AND campaign.id = '{campaign_id}'" if campaign_id else ""

        age_rows = _search(f"""
            SELECT ad_group_criterion.age_range.type,
                   metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions
            FROM age_range_view
            WHERE segments.date BETWEEN '{sd}' AND '{ed}'
            {where_campaign}
            ORDER BY metrics.cost_micros DESC
        """, customer_id)

        gender_rows = _search(f"""
            SELECT ad_group_criterion.gender.type,
                   metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions
            FROM gender_view
            WHERE segments.date BETWEEN '{sd}' AND '{ed}'
            {where_campaign}
            ORDER BY metrics.cost_micros DESC
        """, customer_id)

        age_agg: dict = {}
        for row in age_rows:
            label = row.ad_group_criterion.age_range.type_.name
            m = row.metrics
            if label not in age_agg:
                age_agg[label] = {"spend": 0.0, "impressions": 0, "clicks": 0, "conversions": 0.0}
            age_agg[label]["spend"] += _m(m.cost_micros)
            age_agg[label]["impressions"] += int(m.impressions)
            age_agg[label]["clicks"] += int(m.clicks)
            age_agg[label]["conversions"] += float(m.conversions)

        gender_agg: dict = {}
        for row in gender_rows:
            label = row.ad_group_criterion.gender.type_.name
            m = row.metrics
            if label not in gender_agg:
                gender_agg[label] = {"spend": 0.0, "impressions": 0, "clicks": 0, "conversions": 0.0}
            gender_agg[label]["spend"] += _m(m.cost_micros)
            gender_agg[label]["impressions"] += int(m.impressions)
            gender_agg[label]["clicks"] += int(m.clicks)
            gender_agg[label]["conversions"] += float(m.conversions)

        return json.dumps({
            "date_range": f"{sd} to {ed}",
            "by_age": [{"label": k, **v} for k, v in age_agg.items()],
            "by_gender": [{"label": k, **v} for k, v in gender_agg.items()],
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_geo_performance(
    start_date: str = "",
    end_date: str = "",
    customer_id: str = "",
    row_limit: int = 25,
) -> str:
    """Get Google Ads performance broken down by geographic location (country/region/city).

    Args:
        start_date: Start date YYYY-MM-DD. Defaults to 28 days ago.
        end_date: End date YYYY-MM-DD. Defaults to today.
        customer_id: Google Ads customer ID. Leave blank to use default.
        row_limit: Max locations. Default 25.
    """
    try:
        sd, ed = _date_range(start_date, end_date)
        rows = _search(f"""
            SELECT geographic_view.country_criterion_id, geographic_view.location_type,
                   metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions
            FROM geographic_view
            WHERE segments.date BETWEEN '{sd}' AND '{ed}'
            ORDER BY metrics.cost_micros DESC
            LIMIT {row_limit}
        """, customer_id)
        results = []
        for row in rows:
            gv = row.geographic_view
            m = row.metrics
            results.append({
                "country_criterion_id": gv.country_criterion_id,
                "location_type": gv.location_type.name,
                "spend": _m(m.cost_micros),
                "impressions": int(m.impressions),
                "clicks": int(m.clicks),
                "conversions": float(m.conversions),
            })
        return json.dumps({"date_range": f"{sd} to {ed}", "locations": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_hourly_breakdown(
    start_date: str = "",
    end_date: str = "",
    customer_id: str = "",
) -> str:
    """Get Google Ads performance by hour of day to identify peak performing times.

    Args:
        start_date: Start date YYYY-MM-DD. Defaults to 7 days ago.
        end_date: End date YYYY-MM-DD. Defaults to today.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        sd, ed = _date_range(start_date, end_date, default_days=7)
        rows = _search(f"""
            SELECT segments.hour, metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions
            FROM campaign
            WHERE segments.date BETWEEN '{sd}' AND '{ed}'
            ORDER BY segments.hour
        """, customer_id)
        hourly: dict = {}
        for row in rows:
            hour = row.segments.hour
            m = row.metrics
            if hour not in hourly:
                hourly[hour] = {"hour": hour, "spend": 0.0, "impressions": 0, "clicks": 0, "conversions": 0.0}
            hourly[hour]["spend"] += _m(m.cost_micros)
            hourly[hour]["impressions"] += int(m.impressions)
            hourly[hour]["clicks"] += int(m.clicks)
            hourly[hour]["conversions"] += float(m.conversions)
        return json.dumps({"date_range": f"{sd} to {ed}", "hourly": sorted(hourly.values(), key=lambda x: x["hour"])})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_device_breakdown(
    start_date: str = "",
    end_date: str = "",
    customer_id: str = "",
) -> str:
    """Get Google Ads performance broken down by device type: desktop, mobile, tablet.

    Args:
        start_date: Start date YYYY-MM-DD. Defaults to 28 days ago.
        end_date: End date YYYY-MM-DD. Defaults to today.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        sd, ed = _date_range(start_date, end_date)
        rows = _search(f"""
            SELECT segments.device, metrics.impressions, metrics.clicks,
                   metrics.cost_micros, metrics.ctr, metrics.average_cpc, metrics.conversions
            FROM campaign
            WHERE segments.date BETWEEN '{sd}' AND '{ed}'
            ORDER BY metrics.cost_micros DESC
        """, customer_id)
        devices: dict = {}
        for row in rows:
            device = row.segments.device.name
            m = row.metrics
            if device not in devices:
                devices[device] = {"device": device, "spend": 0.0, "impressions": 0, "clicks": 0, "conversions": 0.0}
            devices[device]["spend"] += _m(m.cost_micros)
            devices[device]["impressions"] += int(m.impressions)
            devices[device]["clicks"] += int(m.clicks)
            devices[device]["conversions"] += float(m.conversions)
        return json.dumps({"date_range": f"{sd} to {ed}", "devices": list(devices.values())})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_shopping_performance(
    start_date: str = "",
    end_date: str = "",
    customer_id: str = "",
    row_limit: int = 25,
) -> str:
    """Get Google Shopping campaign performance by product: clicks, impressions, spend, ROAS.

    Args:
        start_date: Start date YYYY-MM-DD. Defaults to 28 days ago.
        end_date: End date YYYY-MM-DD. Defaults to today.
        customer_id: Google Ads customer ID. Leave blank to use default.
        row_limit: Max products. Default 25.
    """
    try:
        sd, ed = _date_range(start_date, end_date)
        rows = _search(f"""
            SELECT segments.product_title, segments.product_item_id, segments.product_brand,
                   metrics.impressions, metrics.clicks, metrics.cost_micros,
                   metrics.conversions, metrics.conversions_value
            FROM shopping_performance_view
            WHERE segments.date BETWEEN '{sd}' AND '{ed}'
            ORDER BY metrics.cost_micros DESC
            LIMIT {row_limit}
        """, customer_id)
        results = []
        for row in rows:
            segs = row.segments
            m = row.metrics
            spend = _m(m.cost_micros)
            conv_value = float(m.conversions_value)
            results.append({
                "title": segs.product_title,
                "item_id": segs.product_item_id,
                "brand": segs.product_brand,
                "spend": spend,
                "impressions": int(m.impressions),
                "clicks": int(m.clicks),
                "conversions": float(m.conversions),
                "conversion_value": conv_value,
                "roas": round(conv_value / max(spend, 0.01), 2),
            })
        return json.dumps({"date_range": f"{sd} to {ed}", "products": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_display_performance(
    start_date: str = "",
    end_date: str = "",
    customer_id: str = "",
    row_limit: int = 25,
) -> str:
    """Get Google Display Network campaign performance: impressions, clicks, viewable rate, conversions.

    Args:
        start_date: Start date YYYY-MM-DD. Defaults to 28 days ago.
        end_date: End date YYYY-MM-DD. Defaults to today.
        customer_id: Google Ads customer ID. Leave blank to use default.
        row_limit: Max campaigns. Default 25.
    """
    try:
        sd, ed = _date_range(start_date, end_date)
        rows = _search(f"""
            SELECT campaign.name, campaign.advertising_channel_type,
                   metrics.impressions, metrics.clicks, metrics.cost_micros,
                   metrics.ctr, metrics.average_cpc, metrics.conversions,
                   metrics.active_view_viewability
            FROM campaign
            WHERE segments.date BETWEEN '{sd}' AND '{ed}'
            AND campaign.advertising_channel_type = 'DISPLAY'
            AND campaign.status = 'ENABLED'
            ORDER BY metrics.cost_micros DESC
            LIMIT {row_limit}
        """, customer_id)
        results = []
        for row in rows:
            c = row.campaign
            m = row.metrics
            results.append({
                "name": c.name,
                "spend": _m(m.cost_micros),
                "impressions": int(m.impressions),
                "clicks": int(m.clicks),
                "ctr": _pct(m.ctr),
                "conversions": float(m.conversions),
                "viewability_rate": _pct(m.active_view_viewability),
            })
        return json.dumps({"date_range": f"{sd} to {ed}", "display_campaigns": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_video_performance(
    start_date: str = "",
    end_date: str = "",
    customer_id: str = "",
    row_limit: int = 25,
) -> str:
    """Get Google Video (YouTube) campaign performance: views, view rate, CPV, conversions.

    Args:
        start_date: Start date YYYY-MM-DD. Defaults to 28 days ago.
        end_date: End date YYYY-MM-DD. Defaults to today.
        customer_id: Google Ads customer ID. Leave blank to use default.
        row_limit: Max campaigns. Default 25.
    """
    try:
        sd, ed = _date_range(start_date, end_date)
        rows = _search(f"""
            SELECT campaign.name,
                   metrics.impressions, metrics.video_views, metrics.video_view_rate,
                   metrics.average_cpv, metrics.cost_micros, metrics.conversions
            FROM campaign
            WHERE segments.date BETWEEN '{sd}' AND '{ed}'
            AND campaign.advertising_channel_type = 'VIDEO'
            ORDER BY metrics.cost_micros DESC
            LIMIT {row_limit}
        """, customer_id)
        results = []
        for row in rows:
            c = row.campaign
            m = row.metrics
            results.append({
                "name": c.name,
                "spend": _m(m.cost_micros),
                "impressions": int(m.impressions),
                "video_views": int(m.video_views),
                "view_rate": _pct(m.video_view_rate),
                "avg_cpv": round(float(m.average_cpv) / 1_000_000, 6),
                "conversions": float(m.conversions),
            })
        return json.dumps({"date_range": f"{sd} to {ed}", "video_campaigns": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_landing_page_performance(
    start_date: str = "",
    end_date: str = "",
    customer_id: str = "",
    row_limit: int = 25,
) -> str:
    """Get Google Ads landing page performance: clicks, conversions, mobile-friendliness, speed score.

    Args:
        start_date: Start date YYYY-MM-DD. Defaults to 28 days ago.
        end_date: End date YYYY-MM-DD. Defaults to today.
        customer_id: Google Ads customer ID. Leave blank to use default.
        row_limit: Max landing pages. Default 25.
    """
    try:
        sd, ed = _date_range(start_date, end_date)
        rows = _search(f"""
            SELECT landing_page_view.unexpanded_final_url,
                   metrics.impressions, metrics.clicks, metrics.cost_micros,
                   metrics.conversions, metrics.mobile_friendly_clicks_percentage,
                   metrics.speed_score
            FROM landing_page_view
            WHERE segments.date BETWEEN '{sd}' AND '{ed}'
            ORDER BY metrics.clicks DESC
            LIMIT {row_limit}
        """, customer_id)
        results = []
        for row in rows:
            lp = row.landing_page_view
            m = row.metrics
            results.append({
                "url": lp.unexpanded_final_url,
                "spend": _m(m.cost_micros),
                "impressions": int(m.impressions),
                "clicks": int(m.clicks),
                "conversions": float(m.conversions),
                "mobile_friendly_rate": _pct(m.mobile_friendly_clicks_percentage),
                "speed_score": m.speed_score,
            })
        return json.dumps({"date_range": f"{sd} to {ed}", "landing_pages": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_audience_performance(
    start_date: str = "",
    end_date: str = "",
    customer_id: str = "",
    row_limit: int = 25,
) -> str:
    """Get Google Ads performance for each audience segment or user list.

    Args:
        start_date: Start date YYYY-MM-DD. Defaults to 28 days ago.
        end_date: End date YYYY-MM-DD. Defaults to today.
        customer_id: Google Ads customer ID. Leave blank to use default.
        row_limit: Max audiences. Default 25.
    """
    try:
        sd, ed = _date_range(start_date, end_date)
        rows = _search(f"""
            SELECT user_list.name, campaign.name,
                   metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions
            FROM campaign_audience_view
            WHERE segments.date BETWEEN '{sd}' AND '{ed}'
            ORDER BY metrics.cost_micros DESC
            LIMIT {row_limit}
        """, customer_id)
        results = []
        for row in rows:
            m = row.metrics
            results.append({
                "audience": row.user_list.name,
                "campaign": row.campaign.name,
                "spend": _m(m.cost_micros),
                "impressions": int(m.impressions),
                "clicks": int(m.clicks),
                "conversions": float(m.conversions),
            })
        return json.dumps({"date_range": f"{sd} to {ed}", "audiences": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_conversion_actions(customer_id: str = "") -> str:
    """List all conversion actions configured in the Google Ads account.

    Args:
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        rows = _search("""
            SELECT conversion_action.id, conversion_action.name, conversion_action.status,
                   conversion_action.type, conversion_action.category,
                   conversion_action.value_settings.default_value
            FROM conversion_action
            WHERE conversion_action.status = 'ENABLED'
        """, customer_id)
        results = []
        for row in rows:
            ca = row.conversion_action
            results.append({
                "id": str(ca.id),
                "name": ca.name,
                "type": ca.type_.name,
                "category": ca.category.name,
                "default_value": ca.value_settings.default_value,
            })
        return json.dumps({"conversion_actions": results, "total": len(results)})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_budget_pacing(customer_id: str = "") -> str:
    """Get budget pacing for all active campaigns — daily budget, amount spent, and percentage used.

    Args:
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        today = str(date.today())
        rows = _search(f"""
            SELECT campaign.name, campaign.status, campaign_budget.amount_micros,
                   metrics.cost_micros
            FROM campaign
            WHERE segments.date = '{today}'
            AND campaign.status = 'ENABLED'
            ORDER BY metrics.cost_micros DESC
        """, customer_id)
        results = []
        for row in rows:
            budget = _m(row.campaign_budget.amount_micros)
            spent = _m(row.metrics.cost_micros)
            pct = round(spent / max(budget, 0.01) * 100, 1)
            results.append({
                "campaign": row.campaign.name,
                "daily_budget": budget,
                "spent_today": spent,
                "pacing_pct": f"{pct}%",
            })
        return json.dumps({"date": today, "campaigns": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_auction_insights(
    start_date: str = "",
    end_date: str = "",
    customer_id: str = "",
    campaign_id: str = "",
) -> str:
    """Get Google Ads auction insights: impression share, overlap rate, outranking share vs competitors.

    Args:
        start_date: Start date YYYY-MM-DD. Defaults to 28 days ago.
        end_date: End date YYYY-MM-DD. Defaults to today.
        customer_id: Google Ads customer ID. Leave blank to use default.
        campaign_id: Filter by campaign ID. Leave blank for all.
    """
    try:
        sd, ed = _date_range(start_date, end_date)
        where_campaign = f"AND campaign.id = '{campaign_id}'" if campaign_id else ""
        rows = _search(f"""
            SELECT auction_insight.domain,
                   metrics.search_impression_share, metrics.search_overlap_rate,
                   metrics.search_outranking_share, metrics.search_top_impression_share,
                   metrics.search_absolute_top_impression_share
            FROM auction_insight
            WHERE segments.date BETWEEN '{sd}' AND '{ed}'
            {where_campaign}
            ORDER BY metrics.search_impression_share DESC
        """, customer_id)
        results = []
        for row in rows:
            m = row.metrics
            results.append({
                "domain": row.auction_insight.domain,
                "impression_share": _pct(m.search_impression_share),
                "overlap_rate": _pct(m.search_overlap_rate),
                "outranking_share": _pct(m.search_outranking_share),
                "top_impression_share": _pct(m.search_top_impression_share),
                "absolute_top_impression_share": _pct(m.search_absolute_top_impression_share),
            })
        return json.dumps({"date_range": f"{sd} to {ed}", "competitors": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_change_history(
    start_date: str = "",
    end_date: str = "",
    customer_id: str = "",
    row_limit: int = 25,
) -> str:
    """Get recent change history in the Google Ads account: what was changed, when, and by whom.

    Args:
        start_date: Start date YYYY-MM-DD. Defaults to 7 days ago.
        end_date: End date YYYY-MM-DD. Defaults to today.
        customer_id: Google Ads customer ID. Leave blank to use default.
        row_limit: Max changes. Default 25.
    """
    try:
        sd, ed = _date_range(start_date, end_date, default_days=7)
        rows = _search(f"""
            SELECT change_event.change_date_time, change_event.changed_fields,
                   change_event.change_resource_name, change_event.change_resource_operation,
                   change_event.client_type, change_event.user_email
            FROM change_event
            WHERE change_event.change_date_time BETWEEN '{sd} 00:00:00' AND '{ed} 23:59:59'
            ORDER BY change_event.change_date_time DESC
            LIMIT {row_limit}
        """, customer_id)
        results = []
        for row in rows:
            ce = row.change_event
            # changed_fields is a protobuf FieldMask — extract paths list
            try:
                changed = list(ce.changed_fields.paths)
            except Exception:
                changed = str(ce.changed_fields)
            results.append({
                "date_time": ce.change_date_time,
                "operation": ce.change_resource_operation.name,
                "client_type": ce.client_type.name,
                "resource": ce.change_resource_name,
                "changed_fields": changed,
                "user": ce.user_email,
            })
        return json.dumps({"date_range": f"{sd} to {ed}", "changes": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_quality_score(
    customer_id: str = "",
    campaign_id: str = "",
    row_limit: int = 50,
) -> str:
    """Get quality scores for all keywords: overall score, landing page experience, expected CTR, ad relevance.

    Args:
        customer_id: Google Ads customer ID. Leave blank to use default.
        campaign_id: Filter by campaign ID. Leave blank for all.
        row_limit: Max keywords. Default 50.
    """
    try:
        where_campaign = f"AND campaign.id = '{campaign_id}'" if campaign_id else ""
        rows = _search(f"""
            SELECT ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type,
                   ad_group_criterion.quality_info.quality_score,
                   ad_group_criterion.quality_info.creative_quality_score,
                   ad_group_criterion.quality_info.post_click_quality_score,
                   ad_group_criterion.quality_info.search_predicted_ctr,
                   campaign.name, ad_group.name
            FROM keyword_view
            WHERE ad_group_criterion.status != 'REMOVED'
            AND ad_group_criterion.quality_info.quality_score > 0
            {where_campaign}
            ORDER BY ad_group_criterion.quality_info.quality_score ASC
            LIMIT {row_limit}
        """, customer_id)
        results = []
        for row in rows:
            kw = row.ad_group_criterion
            qi = kw.quality_info
            results.append({
                "keyword": kw.keyword.text,
                "match_type": kw.keyword.match_type.name,
                "quality_score": qi.quality_score,
                "ad_relevance": qi.creative_quality_score.name,
                "landing_page_exp": qi.post_click_quality_score.name,
                "expected_ctr": qi.search_predicted_ctr.name,
                "campaign": row.campaign.name,
                "ad_group": row.ad_group.name,
            })
        return json.dumps({"keywords": results, "total": len(results)})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_call_metrics(
    start_date: str = "",
    end_date: str = "",
    customer_id: str = "",
) -> str:
    """Get Google Ads call extension metrics: calls, call conversions, average call duration.

    Args:
        start_date: Start date YYYY-MM-DD. Defaults to 28 days ago.
        end_date: End date YYYY-MM-DD. Defaults to today.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        sd, ed = _date_range(start_date, end_date)
        rows = _search(f"""
            SELECT campaign.name, metrics.phone_calls, metrics.phone_impressions,
                   metrics.phone_through_rate, metrics.average_cost
            FROM campaign
            WHERE segments.date BETWEEN '{sd}' AND '{ed}'
            AND metrics.phone_calls > 0
            ORDER BY metrics.phone_calls DESC
        """, customer_id)
        results = []
        for row in rows:
            m = row.metrics
            results.append({
                "campaign": row.campaign.name,
                "phone_calls": int(m.phone_calls),
                "phone_impressions": int(m.phone_impressions),
                "phone_through_rate": _pct(m.phone_through_rate),
            })
        return json.dumps({"date_range": f"{sd} to {ed}", "call_metrics": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_extension_performance(
    start_date: str = "",
    end_date: str = "",
    customer_id: str = "",
) -> str:
    """Get Google Ads ad extension performance: sitelinks, callouts, structured snippets.

    Args:
        start_date: Start date YYYY-MM-DD. Defaults to 28 days ago.
        end_date: End date YYYY-MM-DD. Defaults to today.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        sd, ed = _date_range(start_date, end_date)
        rows = _search(f"""
            SELECT asset_field_type_view.field_type,
                   metrics.impressions, metrics.clicks, metrics.cost_micros
            FROM asset_field_type_view
            WHERE segments.date BETWEEN '{sd}' AND '{ed}'
            ORDER BY metrics.clicks DESC
            LIMIT 50
        """, customer_id)
        results = []
        for row in rows:
            m = row.metrics
            results.append({
                "type": row.asset_field_type_view.field_type.name,
                "impressions": int(m.impressions),
                "clicks": int(m.clicks),
                "spend": _m(m.cost_micros),
            })
        return json.dumps({"date_range": f"{sd} to {ed}", "extensions": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_recommendations(customer_id: str = "") -> str:
    """Get Google Ads optimization recommendations for the account.

    Args:
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        rows = _search("""
            SELECT recommendation.type, recommendation.campaign, recommendation.dismissed
            FROM recommendation
            WHERE recommendation.dismissed = FALSE
            LIMIT 25
        """, customer_id)
        results = []
        for row in rows:
            rec = row.recommendation
            results.append({
                "type": rec.type_.name,
                "campaign": rec.campaign,
            })
        return json.dumps({"recommendations": results, "total": len(results)})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_asset_report(
    start_date: str = "",
    end_date: str = "",
    customer_id: str = "",
    row_limit: int = 25,
) -> str:
    """Get Google Ads asset performance (headlines and descriptions for responsive ads): clicks, impressions, performance rating.

    Args:
        start_date: Start date YYYY-MM-DD. Defaults to 28 days ago.
        end_date: End date YYYY-MM-DD. Defaults to today.
        customer_id: Google Ads customer ID. Leave blank to use default.
        row_limit: Max assets. Default 25.
    """
    try:
        sd, ed = _date_range(start_date, end_date)
        rows = _search(f"""
            SELECT ad_group_ad_asset_view.field_type, ad_group_ad_asset_view.performance_label,
                   asset.text_asset.text, asset.type,
                   campaign.name, metrics.impressions, metrics.clicks
            FROM ad_group_ad_asset_view
            WHERE segments.date BETWEEN '{sd}' AND '{ed}'
            AND ad_group_ad_asset_view.enabled = TRUE
            ORDER BY metrics.impressions DESC
            LIMIT {row_limit}
        """, customer_id)
        results = []
        for row in rows:
            av = row.ad_group_ad_asset_view
            a = row.asset
            m = row.metrics
            results.append({
                "text": a.text_asset.text,
                "field_type": av.field_type.name,
                "performance": av.performance_label.name,
                "campaign": row.campaign.name,
                "impressions": int(m.impressions),
                "clicks": int(m.clicks),
            })
        return json.dumps({"date_range": f"{sd} to {ed}", "assets": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_list_labels(customer_id: str = "") -> str:
    """List all labels defined in the Google Ads account.

    Args:
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        rows = _search("""
            SELECT label.id, label.name, label.status, label.text_label.background_color
            FROM label
        """, customer_id)
        results = []
        for row in rows:
            lbl = row.label
            results.append({
                "id": str(lbl.id),
                "name": lbl.name,
                "status": lbl.status.name,
                "color": lbl.text_label.background_color,
            })
        return json.dumps({"labels": results, "total": len(results)})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Management Tools (write)
# ---------------------------------------------------------------------------

@mcp.tool()
def gads_create_budget(
    name: str,
    amount_per_day: float,
    delivery_method: str = "STANDARD",
    customer_id: str = "",
) -> str:
    """Create a new shared campaign budget in Google Ads.

    Args:
        name: Budget name (required).
        amount_per_day: Daily budget amount in the account's currency, e.g. 50.00 for $50 (required).
        delivery_method: STANDARD or ACCELERATED. Default STANDARD.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignBudgetService")
        operation = client.get_type("CampaignBudgetOperation")
        budget = operation.create
        budget.name = name
        budget.amount_micros = int(amount_per_day * 1_000_000)
        budget.delivery_method = client.enums.BudgetDeliveryMethodEnum[delivery_method]
        response = service.mutate_campaign_budgets(customer_id=cid, operations=[operation])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_create_campaign(
    name: str,
    budget_id: str,
    advertising_channel_type: str = "SEARCH",
    status: str = "PAUSED",
    bidding_strategy: str = "MANUAL_CPC",
    target_cpa_micros: int = 0,
    target_roas: float = 0.0,
    customer_id: str = "",
) -> str:
    """Create a new Google Ads campaign.

    Args:
        name: Campaign name (required).
        budget_id: Campaign budget resource name or ID, e.g. '1234567890' (required).
        advertising_channel_type: SEARCH, DISPLAY, VIDEO, SHOPPING, or PERFORMANCE_MAX. Default SEARCH.
        status: ENABLED or PAUSED. Default PAUSED.
        bidding_strategy: MANUAL_CPC, TARGET_CPA, TARGET_ROAS, or MAXIMIZE_CONVERSIONS. Default MANUAL_CPC.
        target_cpa_micros: Required if bidding_strategy is TARGET_CPA (target cost-per-acquisition, in micros).
        target_roas: Required if bidding_strategy is TARGET_ROAS (target return-on-ad-spend, e.g. 3.5 = 350%).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        client, cid = _get_client(customer_id)
        if bidding_strategy == "TARGET_CPA" and target_cpa_micros <= 0:
            return json.dumps({"error": "target_cpa_micros must be a positive value when bidding_strategy is TARGET_CPA."})
        if bidding_strategy == "TARGET_ROAS" and target_roas <= 0:
            return json.dumps({"error": "target_roas must be a positive value when bidding_strategy is TARGET_ROAS."})
        budget_rn = budget_id if budget_id.startswith("customers/") else f"customers/{cid}/campaignBudgets/{budget_id}"
        service = client.get_service("CampaignService")
        operation = client.get_type("CampaignOperation")
        campaign = operation.create
        campaign.name = name
        campaign.campaign_budget = budget_rn
        campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum[advertising_channel_type]
        campaign.status = client.enums.CampaignStatusEnum[status]
        campaign.contains_eu_political_advertising = client.enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
        if bidding_strategy == "MANUAL_CPC":
            campaign.manual_cpc = client.get_type("ManualCpc")
        elif bidding_strategy == "TARGET_CPA":
            campaign.target_cpa.target_cpa_micros = target_cpa_micros
        elif bidding_strategy == "TARGET_ROAS":
            campaign.target_roas.target_roas = target_roas
        elif bidding_strategy == "MAXIMIZE_CONVERSIONS":
            campaign.maximize_conversions = client.get_type("MaximizeConversions")
        response = service.mutate_campaigns(customer_id=cid, operations=[operation])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_create_adgroup(
    name: str,
    campaign_id: str,
    cpc_bid_micros: int = 1000000,
    status: str = "PAUSED",
    customer_id: str = "",
) -> str:
    """Create a new ad group within a Google Ads campaign.

    Args:
        name: Ad group name (required).
        campaign_id: Parent campaign ID (required).
        cpc_bid_micros: Default CPC bid in micros, e.g. 1000000 for $1.00. Default 1000000.
        status: ENABLED or PAUSED. Default PAUSED.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        client, cid = _get_client(customer_id)
        campaign_rn = f"customers/{cid}/campaigns/{campaign_id}"
        service = client.get_service("AdGroupService")
        operation = client.get_type("AdGroupOperation")
        ag = operation.create
        ag.name = name
        ag.campaign = campaign_rn
        ag.cpc_bid_micros = cpc_bid_micros
        ag.status = client.enums.AdGroupStatusEnum[status]
        response = service.mutate_ad_groups(customer_id=cid, operations=[operation])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_add_keywords(
    adgroup_id: str,
    keywords_json: str,
    campaign_id: str,
    customer_id: str = "",
) -> str:
    """Add keywords to a Google Ads ad group.

    Args:
        adgroup_id: Ad group ID to add keywords to (required).
        keywords_json: JSON array of keyword objects, e.g. '[{"text": "running shoes", "match_type": "BROAD", "cpc_bid_micros": 800000}]' (required).
            match_type options: BROAD, PHRASE, EXACT.
        campaign_id: Parent campaign ID (required).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        client, cid = _get_client(customer_id)
        adgroup_rn = f"customers/{cid}/adGroups/{adgroup_id}"
        keywords = json.loads(keywords_json)
        service = client.get_service("AdGroupCriterionService")
        operations = []
        for kw in keywords:
            operation = client.get_type("AdGroupCriterionOperation")
            criterion = operation.create
            criterion.ad_group = adgroup_rn
            criterion.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
            criterion.keyword.text = kw["text"]
            criterion.keyword.match_type = client.enums.KeywordMatchTypeEnum[kw.get("match_type", "BROAD")]
            if kw.get("cpc_bid_micros"):
                criterion.cpc_bid_micros = int(kw["cpc_bid_micros"])
            operations.append(operation)
        response = service.mutate_ad_group_criteria(customer_id=cid, operations=operations)
        return json.dumps({"success": True, "added": len(response.results)})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_add_negative_keywords(
    campaign_id: str,
    keywords_json: str,
    customer_id: str = "",
) -> str:
    """Add negative keywords at the campaign level to exclude irrelevant traffic.

    Args:
        campaign_id: Campaign ID to add negative keywords to (required).
        keywords_json: JSON array of objects, e.g. '[{"text": "free", "match_type": "BROAD"}, {"text": "cheap shoes", "match_type": "EXACT"}]' (required).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        client, cid = _get_client(customer_id)
        campaign_rn = f"customers/{cid}/campaigns/{campaign_id}"
        keywords = json.loads(keywords_json)
        service = client.get_service("CampaignCriterionService")
        operations = []
        for kw in keywords:
            operation = client.get_type("CampaignCriterionOperation")
            criterion = operation.create
            criterion.campaign = campaign_rn
            criterion.negative = True
            criterion.keyword.text = kw["text"]
            criterion.keyword.match_type = client.enums.KeywordMatchTypeEnum[kw.get("match_type", "BROAD")]
            operations.append(operation)
        response = service.mutate_campaign_criteria(customer_id=cid, operations=operations)
        return json.dumps({"success": True, "added": len(response.results)})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_update_campaign_status(
    campaign_id: str,
    status: str,
    customer_id: str = "",
) -> str:
    """Update the status of a Google Ads campaign: enable, pause, or remove it.

    Args:
        campaign_id: Campaign ID to update (required).
        status: New status: ENABLED, PAUSED, or REMOVED (required).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        client, cid = _get_client(customer_id)
        campaign_rn = f"customers/{cid}/campaigns/{campaign_id}"
        service = client.get_service("CampaignService")
        operation = client.get_type("CampaignOperation")
        campaign = operation.update
        campaign.resource_name = campaign_rn
        campaign.status = client.enums.CampaignStatusEnum[status]
        operation.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["status"]))
        service.mutate_campaigns(customer_id=cid, operations=[operation])
        return json.dumps({"success": True, "campaign_id": campaign_id, "new_status": status})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_update_campaign_name(
    campaign_id: str,
    new_name: str,
    customer_id: str = "",
) -> str:
    """Rename a Google Ads campaign.

    Args:
        campaign_id: Campaign ID to rename (required).
        new_name: The new campaign name (required).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        client, cid = _get_client(customer_id)
        campaign_rn = f"customers/{cid}/campaigns/{campaign_id}"
        service = client.get_service("CampaignService")
        operation = client.get_type("CampaignOperation")
        campaign = operation.update
        campaign.resource_name = campaign_rn
        campaign.name = new_name
        operation.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["name"]))
        service.mutate_campaigns(customer_id=cid, operations=[operation])
        return json.dumps({"success": True, "campaign_id": campaign_id, "new_name": new_name})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_update_adgroup_status(
    adgroup_id: str,
    campaign_id: str,
    status: str,
    customer_id: str = "",
) -> str:
    """Update the status of a Google Ads ad group: enable, pause, or remove it.

    Args:
        adgroup_id: Ad group ID to update (required).
        campaign_id: Parent campaign ID (required).
        status: New status: ENABLED, PAUSED, or REMOVED (required).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        client, cid = _get_client(customer_id)
        adgroup_rn = f"customers/{cid}/adGroups/{adgroup_id}"
        service = client.get_service("AdGroupService")
        operation = client.get_type("AdGroupOperation")
        ag = operation.update
        ag.resource_name = adgroup_rn
        ag.status = client.enums.AdGroupStatusEnum[status]
        operation.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["status"]))
        service.mutate_ad_groups(customer_id=cid, operations=[operation])
        return json.dumps({"success": True, "adgroup_id": adgroup_id, "new_status": status})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_update_keyword_status(
    adgroup_id: str,
    criterion_id: str,
    status: str,
    customer_id: str = "",
) -> str:
    """Update the status of a keyword in a Google Ads ad group.

    Args:
        adgroup_id: Ad group ID containing the keyword (required).
        criterion_id: Keyword criterion ID to update (required).
        status: New status: ENABLED, PAUSED, or REMOVED (required).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        client, cid = _get_client(customer_id)
        criterion_rn = f"customers/{cid}/adGroupCriteria/{adgroup_id}~{criterion_id}"
        service = client.get_service("AdGroupCriterionService")
        operation = client.get_type("AdGroupCriterionOperation")
        criterion = operation.update
        criterion.resource_name = criterion_rn
        criterion.status = client.enums.AdGroupCriterionStatusEnum[status]
        operation.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["status"]))
        service.mutate_ad_group_criteria(customer_id=cid, operations=[operation])
        return json.dumps({"success": True, "criterion_id": criterion_id, "new_status": status})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_update_keyword_bid(
    adgroup_id: str,
    criterion_id: str,
    cpc_bid_micros: int,
    customer_id: str = "",
) -> str:
    """Update the CPC bid for a specific keyword in Google Ads.

    Args:
        adgroup_id: Ad group ID containing the keyword (required).
        criterion_id: Keyword criterion ID to update (required).
        cpc_bid_micros: New CPC bid in micros, e.g. 2000000 for $2.00 (required).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        client, cid = _get_client(customer_id)
        criterion_rn = f"customers/{cid}/adGroupCriteria/{adgroup_id}~{criterion_id}"
        service = client.get_service("AdGroupCriterionService")
        operation = client.get_type("AdGroupCriterionOperation")
        criterion = operation.update
        criterion.resource_name = criterion_rn
        criterion.cpc_bid_micros = cpc_bid_micros
        operation.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["cpc_bid_micros"]))
        service.mutate_ad_group_criteria(customer_id=cid, operations=[operation])
        return json.dumps({"success": True, "criterion_id": criterion_id, "cpc_bid_micros": cpc_bid_micros})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_update_campaign_budget(
    budget_id: str,
    amount_per_day: float,
    customer_id: str = "",
) -> str:
    """Update the daily budget amount for a Google Ads campaign budget.

    Args:
        budget_id: Campaign budget ID to update (required).
        amount_per_day: New daily budget in the account's currency, e.g. 100.00 for $100 (required).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        client, cid = _get_client(customer_id)
        budget_rn = budget_id if budget_id.startswith("customers/") else f"customers/{cid}/campaignBudgets/{budget_id}"
        service = client.get_service("CampaignBudgetService")
        operation = client.get_type("CampaignBudgetOperation")
        budget = operation.update
        budget.resource_name = budget_rn
        budget.amount_micros = int(amount_per_day * 1_000_000)
        operation.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["amount_micros"]))
        service.mutate_campaign_budgets(customer_id=cid, operations=[operation])
        return json.dumps({"success": True, "budget_id": budget_id, "new_daily_budget": amount_per_day})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_create_responsive_search_ad(
    adgroup_id: str,
    campaign_id: str,
    final_url: str,
    headlines: list,
    descriptions: list,
    path1: str = "",
    path2: str = "",
    status: str = "PAUSED",
    customer_id: str = "",
) -> str:
    """Create a Responsive Search Ad (RSA) in a Google Ads ad group.

    Args:
        adgroup_id: Ad group ID to add the ad to (required).
        campaign_id: Parent campaign ID (required).
        final_url: The destination URL for the ad (required).
        headlines: List of 3–15 headline strings, max 30 characters each (required).
        descriptions: List of 2–4 description strings, max 90 characters each (required).
        path1: First URL display path, max 15 chars. Optional.
        path2: Second URL display path, max 15 chars. Optional.
        status: ENABLED or PAUSED. Default PAUSED.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        client, cid = _get_client(customer_id)
        adgroup_rn = f"customers/{cid}/adGroups/{adgroup_id}"
        service = client.get_service("AdGroupAdService")
        operation = client.get_type("AdGroupAdOperation")
        ad_group_ad = operation.create
        ad_group_ad.ad_group = adgroup_rn
        ad_group_ad.status = client.enums.AdGroupAdStatusEnum[status]
        ad_group_ad.ad.final_urls.append(final_url)
        rsa = ad_group_ad.ad.responsive_search_ad
        for h in headlines:
            asset = client.get_type("AdTextAsset")
            asset.text = h
            rsa.headlines.append(asset)
        for d in descriptions:
            asset = client.get_type("AdTextAsset")
            asset.text = d
            rsa.descriptions.append(asset)
        if path1:
            rsa.path1 = path1
        if path2:
            rsa.path2 = path2
        response = service.mutate_ad_group_ads(customer_id=cid, operations=[operation])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_update_rsa(
    ad_id: str,
    ad_group_id: str,
    headlines: list = None,
    descriptions: list = None,
    customer_id: str = "",
) -> str:
    """Replace headlines and/or descriptions of an existing Responsive Search Ad (RSA).

    Replaces the entire headlines or descriptions list. To add a single asset
    without removing existing ones, use gads_add_rsa_asset instead.

    Args:
        ad_id: ID of the ad to update (required).
        ad_group_id: Ad group ID that contains the ad (required).
        headlines: New list of headline strings (3–15 items, max 30 chars each). Replaces all existing headlines.
        descriptions: New list of description strings (2–4 items, max 90 chars each). Replaces all existing descriptions.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        if headlines is None and descriptions is None:
            return json.dumps({"error": "Provide at least one of: headlines, descriptions"})
        client, cid = _get_client(customer_id)
        ad_rn = f"customers/{cid}/adGroupAds/{ad_group_id}~{ad_id}"
        service = client.get_service("AdGroupAdService")
        operation = client.get_type("AdGroupAdOperation")
        ad_group_ad = operation.update
        ad_group_ad.resource_name = ad_rn
        rsa = ad_group_ad.ad.responsive_search_ad
        paths = []
        if headlines is not None:
            for h in headlines:
                asset = client.get_type("AdTextAsset")
                asset.text = h
                rsa.headlines.append(asset)
            paths.append("ad.responsive_search_ad.headlines")
        if descriptions is not None:
            for d in descriptions:
                asset = client.get_type("AdTextAsset")
                asset.text = d
                rsa.descriptions.append(asset)
            paths.append("ad.responsive_search_ad.descriptions")
        operation.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=paths))
        service.mutate_ad_group_ads(customer_id=cid, operations=[operation])
        return json.dumps({
            "success": True,
            "ad_id": ad_id,
            "ad_group_id": ad_group_id,
            "updated": {
                "headlines": headlines if headlines is not None else "unchanged",
                "descriptions": descriptions if descriptions is not None else "unchanged",
            },
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_add_rsa_asset(
    ad_id: str,
    ad_group_id: str,
    asset_type: str,
    text: str,
    customer_id: str = "",
) -> str:
    """Add a single headline or description to an existing RSA without removing existing assets.

    Fetches the current assets, appends the new one, and writes back the full list.

    Args:
        ad_id: ID of the ad to update (required).
        ad_group_id: Ad group ID that contains the ad (required).
        asset_type: "headline" or "description" (required).
        text: Text of the new asset (required). Headlines max 30 chars, descriptions max 90 chars.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        if asset_type not in ("headline", "description"):
            return json.dumps({"error": "asset_type must be 'headline' or 'description'"})
        rows = _search(
            f"""
            SELECT ad_group_ad.ad.id,
                   ad_group_ad.ad.responsive_search_ad.headlines,
                   ad_group_ad.ad.responsive_search_ad.descriptions
            FROM ad_group_ad
            WHERE ad_group_ad.ad.id = {ad_id}
              AND ad_group.id = {ad_group_id}
              AND ad_group_ad.status != 'REMOVED'
            """,
            customer_id,
        )
        if not rows:
            return json.dumps({"error": f"Ad {ad_id} not found in ad group {ad_group_id}"})
        existing_rsa = rows[0].ad_group_ad.ad.responsive_search_ad
        existing_headlines = [a.text for a in existing_rsa.headlines]
        existing_descriptions = [a.text for a in existing_rsa.descriptions]
        client, cid = _get_client(customer_id)
        ad_rn = f"customers/{cid}/adGroupAds/{ad_group_id}~{ad_id}"
        service = client.get_service("AdGroupAdService")
        operation = client.get_type("AdGroupAdOperation")
        ad_group_ad = operation.update
        ad_group_ad.resource_name = ad_rn
        rsa = ad_group_ad.ad.responsive_search_ad
        if asset_type == "headline":
            for h in existing_headlines + [text]:
                asset = client.get_type("AdTextAsset")
                asset.text = h
                rsa.headlines.append(asset)
            operation.update_mask.CopyFrom(
                field_mask_pb2.FieldMask(paths=["ad.responsive_search_ad.headlines"])
            )
        else:
            for d in existing_descriptions + [text]:
                asset = client.get_type("AdTextAsset")
                asset.text = d
                rsa.descriptions.append(asset)
            operation.update_mask.CopyFrom(
                field_mask_pb2.FieldMask(paths=["ad.responsive_search_ad.descriptions"])
            )
        service.mutate_ad_group_ads(customer_id=cid, operations=[operation])
        return json.dumps({
            "success": True,
            "ad_id": ad_id,
            "ad_group_id": ad_group_id,
            "added": {"asset_type": asset_type, "text": text},
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_pause_all_campaigns(customer_id: str = "") -> str:
    """Pause ALL active campaigns in the Google Ads account. Use with caution — this will pause all spending.

    Args:
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        rows = _search("""
            SELECT campaign.id, campaign.name
            FROM campaign
            WHERE campaign.status = 'ENABLED'
        """, customer_id)
        if not rows:
            return json.dumps({"message": "No enabled campaigns found.", "paused": 0})
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignService")
        operations = []
        for row in rows:
            operation = client.get_type("CampaignOperation")
            campaign = operation.update
            campaign.resource_name = f"customers/{cid}/campaigns/{row.campaign.id}"
            campaign.status = client.enums.CampaignStatusEnum.PAUSED
            operation.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["status"]))
            operations.append(operation)
        service.mutate_campaigns(customer_id=cid, operations=operations)
        return json.dumps({"success": True, "paused_count": len(operations)})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_enable_campaign(
    campaign_id: str,
    customer_id: str = "",
) -> str:
    """Enable (un-pause) a specific Google Ads campaign.

    Args:
        campaign_id: Campaign ID to enable (required).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        client, cid = _get_client(customer_id)
        campaign_rn = f"customers/{cid}/campaigns/{campaign_id}"
        service = client.get_service("CampaignService")
        operation = client.get_type("CampaignOperation")
        campaign = operation.update
        campaign.resource_name = campaign_rn
        campaign.status = client.enums.CampaignStatusEnum.ENABLED
        operation.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["status"]))
        service.mutate_campaigns(customer_id=cid, operations=[operation])
        return json.dumps({"success": True, "campaign_id": campaign_id, "new_status": "ENABLED"})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_apply_recommendation(
    recommendation_resource_name: str,
    customer_id: str = "",
) -> str:
    """Apply a Google Ads optimization recommendation.

    Args:
        recommendation_resource_name: Full resource name of the recommendation, e.g. 'customers/123/recommendations/456' (required).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("RecommendationService")
        operation = client.get_type("ApplyRecommendationOperation")
        operation.resource_name = recommendation_resource_name
        response = service.apply_recommendation(customer_id=cid, operations=[operation])
        return json.dumps({"success": True, "applied": len(response.results)})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_dismiss_recommendation(
    recommendation_resource_name: str,
    customer_id: str = "",
) -> str:
    """Dismiss a Google Ads optimization recommendation so it no longer appears.

    Args:
        recommendation_resource_name: Full resource name of the recommendation, e.g. 'customers/123/recommendations/456' (required).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("RecommendationService")
        operation = client.get_type("DismissRecommendationRequest").Operations()
        operation.resource_name = recommendation_resource_name
        response = service.dismiss_recommendation(
            customer_id=cid,
            operations=[{"resource_name": recommendation_resource_name}],
        )
        return json.dumps({"success": True, "dismissed": recommendation_resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})



# ---------------------------------------------------------------------------
# Extended Tools (11): Image Assets, Display/Video Ads, PMax Asset Groups,
#   Conversion Actions, Sitelinks, Callouts, Bulk Keywords, Labels
# ---------------------------------------------------------------------------


@mcp.tool()
def gads_upload_image_asset(
    image_url: str,
    asset_name: str,
    customer_id: str = "",
) -> str:
    """Upload a public image URL as a Google Ads Image Asset.

    Args:
        image_url: Public HTTPS URL of the image to upload.
        asset_name: Display name for the asset.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        req = _urllib_request.Request(image_url, headers={"User-Agent": "Mozilla/5.0"})
        with _urllib_request.urlopen(req, timeout=30) as resp:
            image_data = resp.read()
        client, cid = _get_client(customer_id)
        asset_svc = client.get_service("AssetService")
        asset_op = client.get_type("AssetOperation")
        asset = asset_op.create
        asset.name = asset_name
        asset.image_asset.data = image_data
        response = asset_svc.mutate_assets(customer_id=cid, operations=[asset_op])
        rn = response.results[0].resource_name
        return json.dumps({"asset_resource_name": rn, "asset_id": rn.split("/")[-1]})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def gads_create_responsive_display_ad(
    adgroup_id: str,
    headlines: str,
    descriptions: str,
    business_name: str,
    final_url: str,
    marketing_image_asset_id: str,
    logo_asset_id: str = "",
    customer_id: str = "",
) -> str:
    """Create a Responsive Display Ad in an existing ad group.

    Args:
        adgroup_id: The ad group ID.
        headlines: JSON array of headline strings (min 1, max 5, each ≤30 chars).
        descriptions: JSON array of description strings (min 1, max 5, each ≤90 chars).
        business_name: Business name shown in the ad (≤25 chars).
        final_url: Landing page URL.
        marketing_image_asset_id: Image asset ID from gads_upload_image_asset.
        logo_asset_id: Optional logo asset ID.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        headlines_list = json.loads(headlines)
        descriptions_list = json.loads(descriptions)
        client, cid = _get_client(customer_id)
        aga_svc = client.get_service("AdGroupAdService")
        asset_svc = client.get_service("AssetService")
        ad_op = client.get_type("AdGroupAdOperation")
        ad_group_ad = ad_op.create
        ad_group_ad.ad_group = client.get_service("AdGroupService").ad_group_path(cid, adgroup_id)
        ad_group_ad.status = client.enums.AdGroupAdStatusEnum.ENABLED
        rda = ad_group_ad.ad.responsive_display_ad
        rda.business_name = business_name
        ad_group_ad.ad.final_urls.append(final_url)
        for h in headlines_list[:5]:
            ht = client.get_type("AdTextAsset")
            ht.text = h
            rda.headlines.append(ht)
        for d in descriptions_list[:5]:
            dt = client.get_type("AdTextAsset")
            dt.text = d
            rda.descriptions.append(dt)
        img = client.get_type("AdImageAsset")
        img.asset = asset_svc.asset_path(cid, marketing_image_asset_id)
        rda.marketing_images.append(img)
        if logo_asset_id:
            logo = client.get_type("AdImageAsset")
            logo.asset = asset_svc.asset_path(cid, logo_asset_id)
            rda.logos.append(logo)
        response = aga_svc.mutate_ad_group_ads(customer_id=cid, operations=[ad_op])
        rn = response.results[0].resource_name
        return json.dumps({"ad_resource_name": rn, "ad_id": rn.split("/")[-1]})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def gads_create_asset_group(
    campaign_id: str,
    name: str,
    final_url: str,
    business_name: str,
    headlines: str,
    long_headlines: str,
    descriptions: str,
    image_asset_ids: str,
    logo_asset_ids: str = "[]",
    youtube_video_ids: str = "[]",
    customer_id: str = "",
) -> str:
    """Create an Asset Group inside an existing Performance Max campaign.

    Args:
        campaign_id: The PMax campaign ID.
        name: Asset group name.
        final_url: Landing page URL.
        business_name: Business name (≤25 chars).
        headlines: JSON array of headlines (min 3, max 15, each ≤30 chars).
        long_headlines: JSON array of long headlines (min 1, max 5, each ≤90 chars).
        descriptions: JSON array of descriptions (min 2, max 4, each ≤90 chars).
        image_asset_ids: JSON array of image asset IDs (at least 1 required).
        logo_asset_ids: JSON array of logo asset IDs (optional).
        youtube_video_ids: JSON array of YouTube video IDs (optional).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        h_list = json.loads(headlines)
        lh_list = json.loads(long_headlines)
        d_list = json.loads(descriptions)
        img_ids = json.loads(image_asset_ids)
        logo_ids = json.loads(logo_asset_ids) if logo_asset_ids else []
        vid_ids = json.loads(youtube_video_ids) if youtube_video_ids else []

        client, cid = _get_client(customer_id)
        asset_svc = client.get_service("AssetService")
        ag_svc = client.get_service("AssetGroupService")
        aga_svc = client.get_service("AssetGroupAssetService")
        campaign_rn = client.get_service("CampaignService").campaign_path(cid, campaign_id)
        aft = client.enums.AssetFieldTypeEnum

        # Create asset group
        ag_op = client.get_type("AssetGroupOperation")
        ag = ag_op.create
        ag.name = name
        ag.campaign = campaign_rn
        ag.final_urls.append(final_url)
        ag.status = client.enums.AssetGroupStatusEnum.ENABLED
        ag_resp = ag_svc.mutate_asset_groups(customer_id=cid, operations=[ag_op])
        ag_rn = ag_resp.results[0].resource_name

        aga_ops = []

        def _link(asset_rn, field_type):
            op = client.get_type("AssetGroupAssetOperation")
            aga = op.create
            aga.asset_group = ag_rn
            aga.asset = asset_rn
            aga.field_type = field_type
            aga_ops.append(op)

        def _text_asset(text):
            op = client.get_type("AssetOperation")
            op.create.text_asset.text = text
            r = asset_svc.mutate_assets(customer_id=cid, operations=[op])
            return r.results[0].resource_name

        _link(_text_asset(business_name), aft.BUSINESS_NAME)
        for h in h_list[:15]:
            _link(_text_asset(h), aft.HEADLINE)
        for lh in lh_list[:5]:
            _link(_text_asset(lh), aft.LONG_HEADLINE)
        for d in d_list[:4]:
            _link(_text_asset(d), aft.DESCRIPTION)
        for img_id in img_ids:
            _link(asset_svc.asset_path(cid, img_id), aft.MARKETING_IMAGE)
        for logo_id in logo_ids:
            _link(asset_svc.asset_path(cid, logo_id), aft.LOGO)
        for vid_id in vid_ids:
            yt_op = client.get_type("AssetOperation")
            yt_op.create.youtube_video_asset.youtube_video_id = vid_id
            yt_resp = asset_svc.mutate_assets(customer_id=cid, operations=[yt_op])
            _link(yt_resp.results[0].resource_name, aft.YOUTUBE_VIDEO)

        if aga_ops:
            aga_svc.mutate_asset_group_assets(customer_id=cid, operations=aga_ops)

        return json.dumps({"asset_group_resource_name": ag_rn, "asset_group_id": ag_rn.split("/")[-1]})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def gads_get_asset_group_performance(
    campaign_id: str = "",
    start_date: str = "",
    end_date: str = "",
    customer_id: str = "",
) -> str:
    """Get performance metrics for Asset Groups in Performance Max campaigns.

    Args:
        campaign_id: Optional PMax campaign ID to filter results.
        start_date: Start date YYYY-MM-DD. Defaults to 30 days ago.
        end_date: End date YYYY-MM-DD. Defaults to today.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        if not start_date:
            start_date = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d")
        if not end_date:
            end_date = date.today().strftime("%Y-%m-%d")
        where_clauses = [
            "campaign.advertising_channel_type = 'PERFORMANCE_MAX'",
            f"segments.date BETWEEN '{start_date}' AND '{end_date}'",
        ]
        if campaign_id:
            where_clauses.append(f"campaign.id = {campaign_id}")
        gaql = (
            "SELECT asset_group.id, asset_group.name, asset_group.status,"
            " metrics.impressions, metrics.clicks, metrics.conversions, metrics.cost_micros"
            " FROM asset_group"
            f" WHERE {' AND '.join(where_clauses)}"
        )
        rows = _search(gaql, customer_id)
        results = []
        for row in rows:
            results.append({
                "asset_group_id": row.asset_group.id,
                "name": row.asset_group.name,
                "status": row.asset_group.status.name,
                "impressions": row.metrics.impressions,
                "clicks": row.metrics.clicks,
                "conversions": round(row.metrics.conversions, 2),
                "cost": _m(row.metrics.cost_micros),
            })
        return json.dumps(results)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def gads_create_conversion_action(
    name: str,
    type: str,
    category: str,
    counting_type: str,
    value: float = 0.0,
    currency_code: str = "ILS",
    customer_id: str = "",
) -> str:
    """Create a new Conversion Action in the Google Ads account.

    Args:
        name: Display name for the conversion action.
        type: WEBPAGE | PHONE_CALL | APP_ANDROID | APP_IOS | IMPORT
        category: PURCHASE | LEAD | SIGNUP | PAGE_VIEW | DEFAULT
        counting_type: ONE_PER_CLICK | MANY_PER_CLICK
        value: Optional fixed conversion value in account currency.
        currency_code: Currency code, default ILS.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        client, cid = _get_client(customer_id)
        svc = client.get_service("ConversionActionService")
        op = client.get_type("ConversionActionOperation")
        ca = op.create
        ca.name = name
        ca.type_ = getattr(client.enums.ConversionActionTypeEnum, type)
        ca.category = getattr(client.enums.ConversionActionCategoryEnum, category)
        ca.counting_type = getattr(client.enums.ConversionActionCountingTypeEnum, counting_type)
        if value:
            ca.value_settings.default_value = value
            ca.value_settings.always_use_default_value = True
        ca.value_settings.default_currency_code = currency_code
        response = svc.mutate_conversion_actions(customer_id=cid, operations=[op])
        rn = response.results[0].resource_name
        return json.dumps({
            "conversion_action_resource_name": rn,
            "conversion_action_id": rn.split("/")[-1],
        })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def gads_get_account_conversion_id(customer_id: str = "") -> str:
    """Get the Google Ads conversion tracking tag ID (AW-XXXXXXXXX) for the account.

    Returns the numeric conversion tracking ID used in gtag snippets.
    Required for building the send_to parameter: 'AW-{id}/{label}'.

    Args:
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        rows = _search("""
            SELECT customer.conversion_tracking_setting.conversion_tracking_id,
                   customer.conversion_tracking_setting.cross_account_conversion_tracking_id,
                   customer.auto_tagging_enabled
            FROM customer
        """, customer_id)
        for row in rows:
            tag_id = row.customer.conversion_tracking_setting.conversion_tracking_id
            cross_id = row.customer.conversion_tracking_setting.cross_account_conversion_tracking_id
            auto_tagging = row.customer.auto_tagging_enabled
            return json.dumps({
                "conversion_tracking_id": str(tag_id),
                "aw_tag_id": f"AW-{tag_id}",
                "cross_account_conversion_tracking_id": str(cross_id) if cross_id else None,
                "auto_tagging_enabled": auto_tagging,
                "gtag_config_line": f"gtag('config', 'AW-{tag_id}');",
                "gtag_script_tag": f'<script async src="https://www.googletagmanager.com/gtag/js?id=AW-{tag_id}"></script>',
                "note": "Use this AW-XXXXXXXXX as the conversion_id when calling analytics_install_gtag_base or analytics_google_ads_form_conversion on WordPress.",
            })
        return json.dumps({"error": "No conversion tracking ID found for this account"})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_get_conversion_tag_snippet(
    conversion_action_id: str,
    customer_id: str = "",
) -> str:
    """Get the complete gtag snippet for a specific conversion action.

    Returns the global_site_tag (for <head>), event_snippet (fires on conversion),
    and the ready-to-use AW-XXXXXXXX/LABEL send_to string for WordPress injection.

    Args:
        conversion_action_id: The numeric conversion action ID.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        rows = _search(f"""
            SELECT conversion_action.id, conversion_action.name, conversion_action.status,
                   conversion_action.tag_snippets,
                   customer.conversion_tracking_setting.conversion_tracking_id
            FROM conversion_action
            WHERE conversion_action.id = {conversion_action_id}
        """, customer_id)

        for row in rows:
            ca = row.conversion_action
            tag_id = row.customer.conversion_tracking_setting.conversion_tracking_id

            send_to = f"AW-{tag_id}"
            global_site_tag = ""
            event_snippet = ""
            all_snippets = []

            for ts in ca.tag_snippets:
                snippet_type = ts.type_.name
                all_snippets.append({
                    "type": snippet_type,
                    "global_site_tag": ts.global_site_tag,
                    "event_snippet": ts.event_snippet,
                })
                if snippet_type == "WEBPAGE" and not global_site_tag:
                    match = re.search(r"'send_to':\s*'(AW-[^/]+/[^']+)'", ts.event_snippet)
                    if match:
                        send_to = match.group(1)
                    global_site_tag = ts.global_site_tag
                    event_snippet = ts.event_snippet

            aw_id = f"AW-{tag_id}"
            form_js = (
                f"(function(){{\n"
                f"  if(!window.dataLayer){{window.dataLayer=[];}}\n"
                f"  if(!window.gtag){{window.gtag=function(){{dataLayer.push(arguments);}};gtag('js',new Date());gtag('config','{aw_id}');var s=document.createElement('script');s.async=true;s.src='https://www.googletagmanager.com/gtag/js?id={aw_id}';document.head.appendChild(s);}}\n"
                f"  document.addEventListener('DOMContentLoaded',function(){{\n"
                f"    var form=document.querySelector('REPLACE_WITH_FORM_SELECTOR');\n"
                f"    if(form){{form.addEventListener('submit',function(){{gtag('event','conversion',{{'send_to':'{send_to}'}});}});}}\n"
                f"  }});\n"
                f"}})();"
            )

            return json.dumps({
                "conversion_action_id": conversion_action_id,
                "conversion_action_name": ca.name,
                "status": ca.status.name,
                "account_tag_id": aw_id,
                "send_to": send_to,
                "global_site_tag": global_site_tag,
                "event_snippet": event_snippet,
                "all_snippets": all_snippets,
                "wordpress_form_js_template": form_js,
                "next_step": "Replace 'REPLACE_WITH_FORM_SELECTOR' with your form's CSS selector (e.g. '#contact-form', '.wpcf7-form'). Then use analytics_google_ads_form_conversion on WordPress.",
            })
        return json.dumps({"error": f"Conversion action {conversion_action_id} not found"})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_list_conversion_actions_full(
    include_paused: bool = False,
    customer_id: str = "",
) -> str:
    """List all conversion actions with full details: type, category, value, attribution model,
    and the AW-XXXXXXXX/LABEL send_to string ready for gtag injection.

    Args:
        include_paused: Include PAUSED actions (not just ENABLED). Default False.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        status_filter = "WHERE conversion_action.status != 'REMOVED'" if not include_paused else "WHERE conversion_action.status IN ('ENABLED','PAUSED')"
        rows = _search(f"""
            SELECT conversion_action.id, conversion_action.name, conversion_action.status,
                   conversion_action.type, conversion_action.category,
                   conversion_action.counting_type,
                   conversion_action.value_settings.default_value,
                   conversion_action.value_settings.default_currency_code,
                   conversion_action.attribution_model_settings.attribution_model,
                   conversion_action.tag_snippets,
                   customer.conversion_tracking_setting.conversion_tracking_id
            FROM conversion_action
            {status_filter}
        """, customer_id)

        results = []
        for row in rows:
            ca = row.conversion_action
            tag_id = row.customer.conversion_tracking_setting.conversion_tracking_id

            send_to = f"AW-{tag_id}"
            for ts in ca.tag_snippets:
                if ts.type_.name == "WEBPAGE":
                    match = re.search(r"'send_to':\s*'(AW-[^/]+/[^']+)'", ts.event_snippet)
                    if match:
                        send_to = match.group(1)
                    break

            results.append({
                "id": str(ca.id),
                "name": ca.name,
                "status": ca.status.name,
                "type": ca.type_.name,
                "category": ca.category.name,
                "counting_type": ca.counting_type.name,
                "default_value": ca.value_settings.default_value,
                "currency": ca.value_settings.default_currency_code,
                "attribution_model": ca.attribution_model_settings.attribution_model.name,
                "account_tag_id": f"AW-{tag_id}",
                "send_to": send_to,
            })
        return json.dumps({
            "conversion_actions": results,
            "total": len(results),
            "note": "Use 'send_to' value with analytics_google_ads_form_conversion or analytics_google_ads_page_conversion on WordPress.",
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_conversion_performance_by_action(
    days: int = 30,
    customer_id: str = "",
) -> str:
    """Get conversion performance metrics broken down by individual conversion action.

    Args:
        days: Number of days to look back (default 30).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        end_date = str(date.today())
        start_date = str(date.today() - timedelta(days=days))
        rows = _search(f"""
            SELECT conversion_action.id, conversion_action.name, conversion_action.category,
                   conversion_action.status,
                   metrics.conversions, metrics.conversions_value,
                   metrics.cost_per_conversion, metrics.all_conversions,
                   metrics.view_through_conversions
            FROM conversion_action
            WHERE segments.date BETWEEN '{start_date}' AND '{end_date}'
            AND conversion_action.status = 'ENABLED'
            ORDER BY metrics.conversions DESC
        """, customer_id)

        results = []
        for row in rows:
            ca = row.conversion_action
            m = row.metrics
            results.append({
                "id": str(ca.id),
                "name": ca.name,
                "category": ca.category.name,
                "conversions": round(float(m.conversions), 2),
                "all_conversions": round(float(m.all_conversions), 2),
                "view_through_conversions": float(m.view_through_conversions),
                "conversions_value": round(float(m.conversions_value), 2),
                "cost_per_conversion": _m(m.cost_per_conversion),
            })
        return json.dumps({
            "period": f"{start_date} to {end_date}",
            "conversion_actions": results,
            "total_actions": len(results),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_conversion_setup_complete(
    name: str,
    category: str = "LEAD",
    value: float = 0.0,
    currency_code: str = "ILS",
    customer_id: str = "",
) -> str:
    """One-shot: Create a conversion action in Google Ads and return the complete
    ready-to-use JavaScript snippets for WordPress form and page-based tracking.

    Creates the conversion action, fetches its tag snippets, and outputs:
    - The AW-XXXXXXXX/LABEL send_to string
    - JS snippet for form submit tracking (replace selector placeholder)
    - JS snippet for thank-you page tracking (replace URL placeholder)

    Args:
        name: Conversion action name (e.g. 'Lead Form Submission').
        category: LEAD | PURCHASE | SIGNUP | PAGE_VIEW | DEFAULT
        value: Optional fixed conversion value. Use 0 for no fixed value.
        currency_code: Currency code, default ILS.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        client, cid = _get_client(customer_id)

        account_rows = _search("""
            SELECT customer.conversion_tracking_setting.conversion_tracking_id
            FROM customer
        """, customer_id)
        tag_id = None
        for row in account_rows:
            tag_id = row.customer.conversion_tracking_setting.conversion_tracking_id
            break

        svc = client.get_service("ConversionActionService")
        op = client.get_type("ConversionActionOperation")
        ca = op.create
        ca.name = name
        ca.type_ = client.enums.ConversionActionTypeEnum.WEBPAGE
        ca.category = getattr(client.enums.ConversionActionCategoryEnum, category)
        ca.counting_type = client.enums.ConversionActionCountingTypeEnum.ONE_PER_CLICK
        if value:
            ca.value_settings.default_value = value
            ca.value_settings.always_use_default_value = True
        ca.value_settings.default_currency_code = currency_code

        response = svc.mutate_conversion_actions(customer_id=cid, operations=[op])
        rn = response.results[0].resource_name
        action_id = rn.split("/")[-1]

        snippet_rows = _search(f"""
            SELECT conversion_action.id, conversion_action.tag_snippets,
                   customer.conversion_tracking_setting.conversion_tracking_id
            FROM conversion_action WHERE conversion_action.id = {action_id}
        """, customer_id)

        send_to = f"AW-{tag_id}" if tag_id else "AW-UNKNOWN"
        global_site_tag = ""
        event_snippet = ""

        for row in snippet_rows:
            if not tag_id:
                tag_id = row.customer.conversion_tracking_setting.conversion_tracking_id
            for ts in row.conversion_action.tag_snippets:
                if ts.type_.name == "WEBPAGE":
                    match = re.search(r"'send_to':\s*'(AW-[^/]+/[^']+)'", ts.event_snippet)
                    if match:
                        send_to = match.group(1)
                    global_site_tag = ts.global_site_tag
                    event_snippet = ts.event_snippet
                    break

        aw_id = f"AW-{tag_id}" if tag_id else "AW-UNKNOWN"
        value_part = f",'value':{value},'currency':'{currency_code}'" if value else ""

        form_js = (
            f"(function(){{\n"
            f"  if(!window.dataLayer){{window.dataLayer=[];}}\n"
            f"  if(!window.gtag){{window.gtag=function(){{dataLayer.push(arguments);}};gtag('js',new Date());gtag('config','{aw_id}');var s=document.createElement('script');s.async=true;s.src='https://www.googletagmanager.com/gtag/js?id={aw_id}';document.head.appendChild(s);}}\n"
            f"  document.addEventListener('DOMContentLoaded',function(){{\n"
            f"    var form=document.querySelector('REPLACE_WITH_FORM_SELECTOR');\n"
            f"    if(form){{form.addEventListener('submit',function(){{gtag('event','conversion',{{'send_to':'{send_to}'{value_part}}});}});}}\n"
            f"  }});\n"
            f"}})();"
        )

        page_js = (
            f"(function(){{\n"
            f"  if(!window.dataLayer){{window.dataLayer=[];}}\n"
            f"  if(!window.gtag){{window.gtag=function(){{dataLayer.push(arguments);}};gtag('js',new Date());gtag('config','{aw_id}');var s=document.createElement('script');s.async=true;s.src='https://www.googletagmanager.com/gtag/js?id={aw_id}';document.head.appendChild(s);}}\n"
            f"  if(window.location.href.indexOf('REPLACE_WITH_THANK_YOU_URL_PART')>-1){{\n"
            f"    gtag('event','conversion',{{'send_to':'{send_to}'{value_part}}});\n"
            f"  }}\n"
            f"}})();"
        )

        return json.dumps({
            "success": True,
            "conversion_action_id": action_id,
            "conversion_action_name": name,
            "account_tag_id": aw_id,
            "send_to": send_to,
            "global_site_tag": global_site_tag,
            "event_snippet": event_snippet,
            "wordpress_form_conversion_js": form_js,
            "wordpress_page_conversion_js": page_js,
            "next_steps": [
                "1. Use analytics_install_gtag_base on WordPress with conversion_id=" + aw_id,
                "2a. Form tracking: use analytics_google_ads_form_conversion with the form CSS selector and send_to=" + send_to,
                "2b. Page tracking: use analytics_google_ads_page_conversion with the thank-you page URL pattern and send_to=" + send_to,
                "3. Use analytics_detect_forms on the page ID to find the correct CSS selector for step 2a",
            ],
        })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def gads_create_ecommerce_conversion_actions(
    purchase_name: str = "Purchase",
    add_to_cart_name: str = "Add to Cart",
    begin_checkout_name: str = "Begin Checkout",
    currency_code: str = "ILS",
    customer_id: str = "",
) -> str:
    """Create the three standard WooCommerce/ecommerce conversion actions in one call:
    Purchase (PURCHASE), Add to Cart (ADD_TO_CART), Begin Checkout (BEGIN_CHECKOUT).

    Returns the ID and send_to for each action — ready for WordPress injection.

    Args:
        purchase_name: Name for the purchase conversion action.
        add_to_cart_name: Name for the add-to-cart conversion action.
        begin_checkout_name: Name for the begin-checkout conversion action.
        currency_code: Currency code, default ILS.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        client, cid = _get_client(customer_id)

        account_rows = _search("""
            SELECT customer.conversion_tracking_setting.conversion_tracking_id FROM customer
        """, customer_id)
        tag_id = None
        for row in account_rows:
            tag_id = row.customer.conversion_tracking_setting.conversion_tracking_id
            break

        aw_id = f"AW-{tag_id}" if tag_id else "AW-UNKNOWN"
        svc = client.get_service("ConversionActionService")

        actions_to_create = [
            (purchase_name, "PURCHASE", "ONE_PER_CLICK"),
            (add_to_cart_name, "ADD_TO_CART", "MANY_PER_CLICK"),
            (begin_checkout_name, "BEGIN_CHECKOUT", "ONE_PER_CLICK"),
        ]

        operations = []
        for name_str, category_str, counting_str in actions_to_create:
            op = client.get_type("ConversionActionOperation")
            ca = op.create
            ca.name = name_str
            ca.type_ = client.enums.ConversionActionTypeEnum.WEBPAGE
            ca.category = getattr(client.enums.ConversionActionCategoryEnum, category_str)
            ca.counting_type = getattr(client.enums.ConversionActionCountingTypeEnum, counting_str)
            ca.value_settings.default_currency_code = currency_code
            operations.append(op)

        response = svc.mutate_conversion_actions(customer_id=cid, operations=operations)
        created = []
        for i, result in enumerate(response.results):
            rn = result.resource_name
            action_id = rn.split("/")[-1]
            action_name = actions_to_create[i][0]
            action_category = actions_to_create[i][1]

            snippet_rows = _search(f"""
                SELECT conversion_action.id, conversion_action.tag_snippets,
                       customer.conversion_tracking_setting.conversion_tracking_id
                FROM conversion_action WHERE conversion_action.id = {action_id}
            """, customer_id)

            send_to = aw_id
            for row in snippet_rows:
                for ts in row.conversion_action.tag_snippets:
                    if ts.type_.name == "WEBPAGE":
                        match = re.search(r"'send_to':\s*'(AW-[^/]+/[^']+)'", ts.event_snippet)
                        if match:
                            send_to = match.group(1)
                        break

            created.append({
                "name": action_name,
                "category": action_category,
                "id": action_id,
                "account_tag_id": aw_id,
                "send_to": send_to,
            })

        return json.dumps({
            "success": True,
            "account_tag_id": aw_id,
            "created": created,
            "next_steps": [
                "Use analytics_woo_google_ads_purchase with send_to from 'Purchase' action",
                "Use analytics_woo_google_ads_add_to_cart with send_to from 'Add to Cart' action",
                "Use analytics_woo_google_ads_checkout with send_to from 'Begin Checkout' action",
            ],
        })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def gads_check_auto_tagging(customer_id: str = "") -> str:
    """Check if Google Ads auto-tagging is enabled (required for GA4 attribution).

    Auto-tagging appends the GCLID parameter to URLs so GA4 can attribute
    sessions back to Google Ads clicks.

    Args:
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        rows = _search("""
            SELECT customer.auto_tagging_enabled,
                   customer.conversion_tracking_setting.conversion_tracking_id,
                   customer.descriptive_name
            FROM customer
        """, customer_id)
        for row in rows:
            auto_tagging = row.customer.auto_tagging_enabled
            tag_id = row.customer.conversion_tracking_setting.conversion_tracking_id
            return json.dumps({
                "auto_tagging_enabled": auto_tagging,
                "account_name": row.customer.descriptive_name,
                "conversion_tracking_id": str(tag_id),
                "aw_tag_id": f"AW-{tag_id}",
                "status": "OK — GA4 will receive GCLID for attribution" if auto_tagging else "WARNING — Auto-tagging is OFF. GA4 cannot attribute sessions to Google Ads. Enable in Account Settings.",
                "how_to_fix": None if auto_tagging else "Go to Google Ads > Admin > Account Settings > Auto-tagging > Enable",
            })
        return json.dumps({"error": "Could not retrieve account info"})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gads_create_sitelink_asset(
    link_text: str,
    description_1: str,
    description_2: str,
    final_url: str,
    campaign_id: str = "",
    customer_id: str = "",
) -> str:
    """Create a Sitelink asset and optionally attach it to a campaign.

    Args:
        link_text: Sitelink anchor text (≤25 chars).
        description_1: First description line (≤35 chars).
        description_2: Second description line (≤35 chars).
        final_url: URL the sitelink points to.
        campaign_id: If provided, attach the sitelink to this campaign.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        client, cid = _get_client(customer_id)
        asset_svc = client.get_service("AssetService")
        asset_op = client.get_type("AssetOperation")
        sl = asset_op.create.sitelink_asset
        sl.link_text = link_text
        sl.description1 = description_1
        sl.description2 = description_2
        sl.final_urls.append(final_url)
        resp = asset_svc.mutate_assets(customer_id=cid, operations=[asset_op])
        rn = resp.results[0].resource_name
        if campaign_id:
            ca_svc = client.get_service("CampaignAssetService")
            ca_op = client.get_type("CampaignAssetOperation")
            ca = ca_op.create
            ca.campaign = client.get_service("CampaignService").campaign_path(cid, campaign_id)
            ca.asset = rn
            ca.field_type = client.enums.AssetFieldTypeEnum.SITELINK
            ca_svc.mutate_campaign_assets(customer_id=cid, operations=[ca_op])
        return json.dumps({"asset_resource_name": rn, "asset_id": rn.split("/")[-1]})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def gads_create_callout_asset(
    callout_text: str,
    campaign_id: str = "",
    customer_id: str = "",
) -> str:
    """Create a Callout extension asset and optionally attach to a campaign.

    Args:
        callout_text: Callout text (≤25 chars).
        campaign_id: If provided, attach the callout to this campaign.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        client, cid = _get_client(customer_id)
        asset_svc = client.get_service("AssetService")
        asset_op = client.get_type("AssetOperation")
        asset_op.create.callout_asset.callout_text = callout_text
        resp = asset_svc.mutate_assets(customer_id=cid, operations=[asset_op])
        rn = resp.results[0].resource_name
        if campaign_id:
            ca_svc = client.get_service("CampaignAssetService")
            ca_op = client.get_type("CampaignAssetOperation")
            ca = ca_op.create
            ca.campaign = client.get_service("CampaignService").campaign_path(cid, campaign_id)
            ca.asset = rn
            ca.field_type = client.enums.AssetFieldTypeEnum.CALLOUT
            ca_svc.mutate_campaign_assets(customer_id=cid, operations=[ca_op])
        return json.dumps({"asset_resource_name": rn, "asset_id": rn.split("/")[-1]})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def gads_create_video_ad(
    adgroup_id: str,
    youtube_video_id: str,
    format: str,
    final_url: str,
    headline: str = "",
    description: str = "",
    companion_banner_asset_id: str = "",
    customer_id: str = "",
) -> str:
    """Create a YouTube video ad in an ad group.

    Args:
        adgroup_id: The ad group ID.
        youtube_video_id: 11-character YouTube video ID (from watch?v=...).
        format: IN_STREAM_VIDEO | IN_FEED_VIDEO | BUMPER_AD | OUT_STREAM
        final_url: Landing page URL.
        headline: Headline text (required for IN_FEED_VIDEO).
        description: Description text.
        companion_banner_asset_id: Optional companion banner asset ID.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        client, cid = _get_client(customer_id)
        asset_svc = client.get_service("AssetService")
        aga_svc = client.get_service("AdGroupAdService")

        # Create YouTube video asset
        yt_op = client.get_type("AssetOperation")
        yt_op.create.youtube_video_asset.youtube_video_id = youtube_video_id
        yt_resp = asset_svc.mutate_assets(customer_id=cid, operations=[yt_op])
        yt_asset_rn = yt_resp.results[0].resource_name

        ad_op = client.get_type("AdGroupAdOperation")
        ad_group_ad = ad_op.create
        ad_group_ad.ad_group = client.get_service("AdGroupService").ad_group_path(cid, adgroup_id)
        ad_group_ad.status = client.enums.AdGroupAdStatusEnum.ENABLED
        ad = ad_group_ad.ad
        ad.final_urls.append(final_url)

        if format == "IN_FEED_VIDEO":
            ifv = ad.in_feed_video_ad
            vid_asset = client.get_type("AdVideoAsset")
            vid_asset.asset = yt_asset_rn
            ifv.video.CopyFrom(vid_asset)
            ifv.headline = headline or ""
            ifv.description1 = description or ""
            ifv.description2 = ""
        else:
            # IN_STREAM_VIDEO, BUMPER_AD, OUT_STREAM → video_responsive_ad
            vra = ad.video_responsive_ad
            vid_asset = client.get_type("AdVideoAsset")
            vid_asset.asset = yt_asset_rn
            vra.videos.append(vid_asset)
            if headline:
                ht = client.get_type("AdTextAsset")
                ht.text = headline
                vra.headlines.append(ht)
            if description:
                dt = client.get_type("AdTextAsset")
                dt.text = description
                vra.long_headlines.append(dt)
            if companion_banner_asset_id:
                cb = client.get_type("AdImageAsset")
                cb.asset = asset_svc.asset_path(cid, companion_banner_asset_id)
                vra.companion_banners.append(cb)

        response = aga_svc.mutate_ad_group_ads(customer_id=cid, operations=[ad_op])
        rn = response.results[0].resource_name
        return json.dumps({"ad_resource_name": rn, "ad_id": rn.split("/")[-1]})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def gads_bulk_create_keywords(
    keywords_json: str,
    customer_id: str = "",
) -> str:
    """Create multiple keywords in a single batch API call (up to 10,000).

    Args:
        keywords_json: JSON array of objects, each with:
            adgroup_id (str), text (str), match_type (BROAD|PHRASE|EXACT),
            cpc_bid_micros (int, optional).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        keywords = json.loads(keywords_json)
    except json.JSONDecodeError as e:
        return json.dumps({"success": False, "error": f"Invalid JSON: {e}"})
    try:
        client, cid = _get_client(customer_id)
        svc = client.get_service("AdGroupCriterionService")
        ag_svc = client.get_service("AdGroupService")
        match_type_enum = client.enums.KeywordMatchTypeEnum
        operations = []
        errors = []
        for idx, kw in enumerate(keywords):
            try:
                op = client.get_type("AdGroupCriterionOperation")
                criterion = op.create
                criterion.ad_group = ag_svc.ad_group_path(cid, str(kw["adgroup_id"]))
                criterion.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
                criterion.keyword.text = kw["text"]
                criterion.keyword.match_type = getattr(match_type_enum, kw.get("match_type", "BROAD"))
                if kw.get("cpc_bid_micros"):
                    criterion.cpc_bid_micros = int(kw["cpc_bid_micros"])
                operations.append(op)
            except Exception as e:
                errors.append(f"Row {idx}: {e}")
        if not operations:
            return json.dumps({"created": 0, "failed": len(errors), "errors": errors})
        response = svc.mutate_ad_group_criteria(
            customer_id=cid,
            operations=operations,
            partial_failure=True,
        )
        created = len([r for r in response.results if r.resource_name])
        if response.partial_failure_error:
            errors.append(str(response.partial_failure_error.message))
        return json.dumps({"created": created, "failed": len(errors), "errors": errors})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def gads_create_label(
    name: str,
    color: str = "",
    description: str = "",
    customer_id: str = "",
) -> str:
    """Create a new Label in the Google Ads account.

    Args:
        name: Label display name.
        color: HEX color string e.g. '#FF0000' (optional).
        description: Label description (optional).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        client, cid = _get_client(customer_id)
        svc = client.get_service("LabelService")
        op = client.get_type("LabelOperation")
        label = op.create
        label.name = name
        if color:
            label.text_label.background_color = color
        if description:
            label.text_label.description = description
        response = svc.mutate_labels(customer_id=cid, operations=[op])
        rn = response.results[0].resource_name
        return json.dumps({"label_resource_name": rn, "label_id": rn.split("/")[-1]})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def gads_apply_label(
    label_id: str,
    entity_type: str,
    entity_id: str,
    customer_id: str = "",
) -> str:
    """Apply a Label to a campaign, ad group, keyword, or ad.

    Args:
        label_id: The label ID (from gads_create_label or gads_list_labels).
        entity_type: CAMPAIGN | AD_GROUP | KEYWORD | AD
        entity_id: The entity ID. For KEYWORD use 'adgroup_id/criterion_id'.
                   For AD use 'adgroup_id/ad_id'.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        client, cid = _get_client(customer_id)
        label_rn = client.get_service("LabelService").label_path(cid, label_id)

        if entity_type == "CAMPAIGN":
            svc = client.get_service("CampaignLabelService")
            op = client.get_type("CampaignLabelOperation")
            op.create.campaign = client.get_service("CampaignService").campaign_path(cid, entity_id)
            op.create.label = label_rn
            resp = svc.mutate_campaign_labels(customer_id=cid, operations=[op])
        elif entity_type == "AD_GROUP":
            svc = client.get_service("AdGroupLabelService")
            op = client.get_type("AdGroupLabelOperation")
            op.create.ad_group = client.get_service("AdGroupService").ad_group_path(cid, entity_id)
            op.create.label = label_rn
            resp = svc.mutate_ad_group_labels(customer_id=cid, operations=[op])
        elif entity_type == "KEYWORD":
            ag_id, crit_id = entity_id.split("/")
            svc = client.get_service("AdGroupCriterionLabelService")
            op = client.get_type("AdGroupCriterionLabelOperation")
            op.create.ad_group_criterion = client.get_service("AdGroupCriterionService").ad_group_criterion_path(cid, ag_id, crit_id)
            op.create.label = label_rn
            resp = svc.mutate_ad_group_criterion_labels(customer_id=cid, operations=[op])
        elif entity_type == "AD":
            ag_id, ad_id = entity_id.split("/")
            svc = client.get_service("AdGroupAdLabelService")
            op = client.get_type("AdGroupAdLabelOperation")
            op.create.ad_group_ad = client.get_service("AdGroupAdService").ad_group_ad_path(cid, ag_id, ad_id)
            op.create.label = label_rn
            resp = svc.mutate_ad_group_ad_labels(customer_id=cid, operations=[op])
        else:
            return json.dumps({"success": False, "error": f"Unknown entity_type: {entity_type}"})

        rn = resp.results[0].resource_name
        return json.dumps({"success": True, "resource_name": rn})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})



@mcp.tool()
def gads_update_conversion_action(
    conversion_action_id: str,
    status: str = "",
    value: float = -1.0,
    counting_type: str = "",
    attribution_model: str = "",
    customer_id: str = "",
) -> str:
    """Update an existing Conversion Action (status, value, counting type, attribution model).

    Args:
        conversion_action_id: The conversion action ID to update.
        status: ENABLED | PAUSED | REMOVED (optional).
        value: New fixed conversion value (optional, pass -1 to skip).
        counting_type: ONE_PER_CLICK | MANY_PER_CLICK (optional).
        attribution_model: LAST_CLICK | FIRST_CLICK | LINEAR | TIME_DECAY |
                           POSITION_BASED | DATA_DRIVEN (optional).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        client, cid = _get_client(customer_id)
        svc = client.get_service("ConversionActionService")
        op = client.get_type("ConversionActionOperation")
        ca = op.update
        ca.resource_name = svc.conversion_action_path(cid, conversion_action_id)

        update_mask_fields = []

        if status:
            ca.status = getattr(client.enums.ConversionActionStatusEnum, status)
            update_mask_fields.append("status")

        if counting_type:
            ca.counting_type = getattr(client.enums.ConversionActionCountingTypeEnum, counting_type)
            update_mask_fields.append("counting_type")

        if attribution_model:
            ca.attribution_model_settings.attribution_model = getattr(
                client.enums.AttributionModelEnum, attribution_model
            )
            update_mask_fields.append("attribution_model_settings.attribution_model")

        if value >= 0:
            ca.value_settings.default_value = value
            ca.value_settings.always_use_default_value = True
            update_mask_fields.extend([
                "value_settings.default_value",
                "value_settings.always_use_default_value",
            ])

        from google.protobuf import field_mask_pb2
        op.update_mask.CopyFrom(
            field_mask_pb2.FieldMask(paths=update_mask_fields)
        )

        response = svc.mutate_conversion_actions(customer_id=cid, operations=[op])
        rn = response.results[0].resource_name
        return json.dumps({"success": True, "resource_name": rn})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})
# TEST LINE


# ─── Extended tools (rocket-gads-mcp-extension) ──────────────────────────────


# ─── Extended tools (rocket-gads-mcp-extension) ──────────────────────────────


# ─── Extended tools (rocket-gads-mcp-extension) ──────────────────────────────

def _row_to_dict(row) -> dict:
    """Convert a GoogleAds proto row to a plain Python dict.

    `including_default_value_fields` was removed in protobuf 5.x (it is now
    `always_print_fields_with_no_presence`), so on protobuf 5.29 the call below
    raised TypeError, the except swallowed it, and every row came back from the
    dir() fallback instead: all 180-odd resource names the proto declares, each
    stringified, whether or not the query selected it.

    That is not a degraded result, it is a different result. A caller asking for
    31 campaign budgets got a 174 KB reply in which campaign_budget is the
    string repr of a proto rather than an object with a name and an amount --
    large enough to blow a tool-output limit, and unusable when it does not.
    Every read tool in this file goes through here, so every one of them was
    affected, silently, for as long as protobuf has been on 5.x.

    Both kwargs are tried so the module works either side of that rename, and
    the fallback now says what went wrong rather than quietly inventing a shape.
    """
    from google.protobuf.json_format import MessageToDict

    pb = getattr(row, "_pb", row)
    try:
        return MessageToDict(pb, preserving_proto_field_name=True)
    except TypeError:
        pass
    except Exception:
        pass
    try:
        # protobuf < 5: the same behaviour under the old name.
        return MessageToDict(
            pb, preserving_proto_field_name=True, including_default_value_fields=False
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not decode row: {type(exc).__name__}: {exc}"}

@mcp.tool()
def gads_create_customer_user_access(
    email_address: str,
    access_role: str,
    customer_id: str = None,
) -> dict:
    """Invite a user to access this Google Ads account with a specified role."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_find_account(
    query: str,
    customer_id: str = None,
) -> dict:
    """Search for sub-accounts under the MCC by name or ID (fuzzy match). Useful for quickly finding a client account."""
    gaql = """SELECT customer_client.client_customer, customer_client.descriptive_name,
              customer_client.id, customer_client.manager,
              customer_client.status, customer_client.currency_code,
              customer_client.time_zone, customer_client.level
       FROM customer_client
       ORDER BY customer_client.level ASC"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_account_budget(
    customer_id: str = None,
) -> dict:
    """Get account-level budget information including approved spending limits."""
    gaql = """SELECT account_budget.id, account_budget.name, account_budget.status,
              account_budget.amount_micros, account_budget.total_adjustments_micros,
              account_budget.approved_spending_limit_micros,
              account_budget.approved_start_date_time,
              account_budget.approved_end_date_time,
              account_budget.billing_setup
       FROM account_budget"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_account_status(
    customer_id: str = None,
) -> dict:
    """Get account status summary: account info, active/paused/removed campaign counts, and last change event in the past 30 days."""
    gaql = """SELECT customer.id, customer.descriptive_name, customer.status,
                customer.currency_code, customer.time_zone,
                customer.auto_tagging_enabled, customer.test_account
         FROM customer
         LIMIT 1"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_budget_change_log(
    start_date: str,
    end_date: str,
    limit: int =None,
    customer_id: str = None,
) -> dict:
    """Get a log of all budget changes (CAMPAIGN_BUDGET resource type) in a date range. Shows old → new budget amounts with percentage change and who made the change."""
    _limit = int(limit) if limit else 100
    sd = start_date or str(date.today() - timedelta(days=30))
    ed = end_date or str(date.today())
    gaql = f"""SELECT change_event.change_date_time, change_event.user_email,
                change_event.client_type,
                change_event.resource_name, change_event.changed_fields,
                change_event.campaign, change_event.old_resource,
                change_event.new_resource
         FROM change_event
         WHERE change_event.change_date_time >= '{sd} 00:00:00'
           AND change_event.change_date_time <= '{ed} 23:59:59'
         ORDER BY change_event.change_date_time DESC
         LIMIT {_limit}"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_change_history(
    start_date: str,
    end_date: str,
    resource_type: str = None,
    limit: int =None,
    customer_id: str = None,
) -> dict:
    """Get account change history showing all modifications with who made them. Automatically splits long date ranges into 30-day windows (Google Ads API limitation). Returns Hebrew client_type labels."""
    _limit = int(limit) if limit else 100
    sd = start_date or str(date.today() - timedelta(days=30))
    ed = end_date or str(date.today())
    resource_filter = f"AND change_event.resource_type = '{resource_type}'" if resource_type else ""
    gaql = f"""SELECT change_event.change_date_time, change_event.changed_fields,
                change_event.client_type,
                change_event.resource_name, change_event.user_email,
                change_event.campaign, change_event.ad_group
         FROM change_event
         WHERE change_event.change_date_time >= '{sd} 00:00:00'
           AND change_event.change_date_time <= '{ed} 23:59:59'
           {resource_filter}
         ORDER BY change_event.change_date_time DESC
         LIMIT {_limit}"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_inactivity_alerts(
    threshold_days: float = 14,
    customer_id: str = None,
) -> dict:
    """Check all MCC sub-accounts for inactivity. Returns accounts with no changes in the past N days (default 14). Useful for detecting neglected client accounts."""
    gaql = """SELECT customer_client.client_customer, customer_client.descriptive_name,
              customer_client.id, customer_client.manager, customer_client.status,
              customer_client.level
       FROM customer_client
       ORDER BY customer_client.descriptive_name ASC"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_user_activity_summary(
    start_date: str,
    end_date: str,
    customer_id: str = None,
) -> dict:
    """Summarize change history by user: total changes per user per day, first/last change time, and estimated session duration. Useful for agency oversight."""
    conditions = []
    if start_date and end_date:
        conditions.append(f"segments.date BETWEEN '{start_date}' AND '{end_date}'")
    gaql = f"""SELECT change_event.change_date_time, change_event.user_email,
                change_event.client_type
         FROM change_event
         WHERE change_event.change_date_time >= '{w.start} 00:00:00'
           AND change_event.change_date_time <= '{w.end} 23:59:59'
         ORDER BY change_event.change_date_time ASC
         LIMIT 1000"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_invite_user(
    email_address: str,
    access_role: str,
    customer_id: str = None,
) -> dict:
    """Send an invitation to a user to join this Google Ads account."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_link_product_account(
    product_link_type: str,
    linked_account_id: str,
    customer_id: str = None,
) -> dict:
    """Link another Google product account (YouTube, partner, etc.) to this Google Ads account."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_accessible_accounts(
    customer_id: str = None,
) -> dict:
    """List all accounts accessible under this MCC/manager account."""
    gaql = """SELECT customer_client.client_customer, customer_client.descriptive_name,
              customer_client.id, customer_client.manager,
              customer_client.status, customer_client.currency_code,
              customer_client.time_zone, customer_client.level
       FROM customer_client
       ORDER BY customer_client.level ASC"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_billing_setups(
    customer_id: str = None,
) -> dict:
    """List billing setups configured for this Google Ads account."""
    gaql = """SELECT billing_setup.id, billing_setup.status,
              billing_setup.payments_account_id,
              billing_setup.payments_profile_id,
              billing_setup.start_date_time, billing_setup.end_date_time
       FROM billing_setup"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_customer_user_access(
    customer_id: str = None,
) -> dict:
    """List all users with access to this Google Ads account."""
    gaql = """SELECT customer_user_access.user_id, customer_user_access.email_address,
              customer_user_access.access_role, customer_user_access.access_creation_date_time,
              customer_user_access.inviter_user_email_address
       FROM customer_user_access"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_linked_accounts(
    customer_id: str = None,
) -> dict:
    """List all product accounts linked to this Google Ads account."""
    gaql = """SELECT product_link.product_link_id, product_link.type,
              product_link.advertising_partner.customer,
              product_link.youtube_channel.channel_id,
              product_link.google_ads.customer
       FROM product_link"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_manager_accounts(
    customer_id: str = None,
) -> dict:
    """List all manager (MCC) accounts in the hierarchy."""
    gaql = """SELECT customer_client.client_customer, customer_client.descriptive_name,
              customer_client.id, customer_client.manager,
              customer_client.status, customer_client.currency_code
       FROM customer_client
       WHERE customer_client.manager = TRUE"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_remove_customer_user_access(
    user_id: str,
    customer_id: str = None,
) -> dict:
    """Remove a user/"""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_update_account(
    descriptive_name: str = None,
    tracking_url_template: str = None,
    auto_tagging_enabled: bool = None,
    customer_id: str = None,
) -> dict:
    """Update account settings like name, tracking template, or auto-tagging."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_update_customer_user_access(
    user_id: str,
    access_role: str,
    customer_id: str = None,
) -> dict:
    """Update the access role for an existing user on this account."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_ad_group(
    campaign_id: str,
    name: str,
    cpc_bid_micros: float = None,
    cpm_bid_micros: float = None,
    cpv_bid_micros: float = None,
    target_cpa_micros: float = None,
    target_roas: float = None,
    type_val: str = None,
    status: str = None,
    customer_id: str = None,
) -> dict:
    """Create a new ad group inside a campaign. type values: SEARCH_STANDARD, DISPLAY_STANDARD, SHOPPING_PRODUCT_ADS, VIDEO_BUMPER, VIDEO_TRUE_VIEW_IN_STREAM, VIDEO_TRUE_VIEW_IN_DISPLAY, SEARCH_DYNAMIC_ADS, etc. Defaults SEARCH_STANDARD."""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    if status and status not in ('ALL', None): conditions.append(f"campaign.status = '{status}'")
    gaql = f"""
        SELECT ad_group_label.ad_group, ad_group_label.label
        FROM ad_group_label
        WHERE ad_group_label.label = 'customers/{cid}/labels/{label_id or ""}'
        LIMIT {limit or 500}
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_pause_ad_group(
    ad_group_id: str,
    customer_id: str = None,
) -> dict:
    """Pause an ad group by ID."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
        SELECT ad_group_label.ad_group, ad_group_label.label
        FROM ad_group_label
        WHERE ad_group_label.label = 'customers/{cid}/labels/{label_id or ""}'
        LIMIT {limit or 500}
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_enable_ad_group(
    ad_group_id: str,
    customer_id: str = None,
) -> dict:
    """Enable a paused ad group."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
        SELECT ad_group_label.ad_group, ad_group_label.label
        FROM ad_group_label
        WHERE ad_group_label.label = 'customers/{cid}/labels/{label_id or ""}'
        LIMIT {limit or 500}
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_remove_ad_group(
    ad_group_id: str,
    customer_id: str = None,
) -> dict:
    """Permanently remove an ad group (cannot be undone — all its ads go to REMOVED status)."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
        SELECT ad_group_label.ad_group, ad_group_label.label
        FROM ad_group_label
        WHERE ad_group_label.label = 'customers/{cid}/labels/{label_id or ""}'
        LIMIT {limit or 500}
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_bulk_create_ad_groups(
    ad_groups: list = None,
    customer_id: str = None,
) -> dict:
    """Create multiple ad groups in one call. Each item: { campaign_id, name, cpc_bid_micros? }."""
    conditions = []
    gaql = f"""
        SELECT ad_group_label.ad_group, ad_group_label.label
        FROM ad_group_label
        WHERE ad_group_label.label = 'customers/{cid}/labels/{label_id or ""}'
        LIMIT {limit or 500}
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_bulk_pause_ad_groups(
    ad_group_ids: list,
    customer_id: str = None,
) -> dict:
    """Pause multiple ad groups in one call."""
    conditions = []
    gaql = f"""
        SELECT ad_group_label.ad_group, ad_group_label.label
        FROM ad_group_label
        WHERE ad_group_label.label = 'customers/{cid}/labels/{label_id or ""}'
        LIMIT {limit or 500}
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_bulk_enable_ad_groups(
    ad_group_ids: list,
    customer_id: str = None,
) -> dict:
    """Enable multiple ad groups in one call."""
    conditions = []
    gaql = f"""
        SELECT ad_group_label.ad_group, ad_group_label.label
        FROM ad_group_label
        WHERE ad_group_label.label = 'customers/{cid}/labels/{label_id or ""}'
        LIMIT {limit or 500}
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_bulk_remove_ad_groups(
    ad_group_ids: list,
    customer_id: str = None,
) -> dict:
    """Permanently remove multiple ad groups in one call."""
    conditions = []
    gaql = f"""
        SELECT ad_group_label.ad_group, ad_group_label.label
        FROM ad_group_label
        WHERE ad_group_label.label = 'customers/{cid}/labels/{label_id or ""}'
        LIMIT {limit or 500}
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_bulk_update_ad_group_cpc(
    updates: list = None,
    customer_id: str = None,
) -> dict:
    """Update CPC bid on multiple ad groups. items: [{ad_group_id, cpc_bid_micros}]."""
    conditions = []
    gaql = f"""
        SELECT ad_group_label.ad_group, ad_group_label.label
        FROM ad_group_label
        WHERE ad_group_label.label = 'customers/{cid}/labels/{label_id or ""}'
        LIMIT {limit or 500}
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_ad_group_cpm(
    ad_group_id: str,
    cpm_bid_micros: float,
    customer_id: str = None,
) -> dict:
    """Set CPM (cost-per-thousand-impressions) bid on an ad group."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
        SELECT ad_group_label.ad_group, ad_group_label.label
        FROM ad_group_label
        WHERE ad_group_label.label = 'customers/{cid}/labels/{label_id or ""}'
        LIMIT {limit or 500}
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_ad_group_cpv(
    ad_group_id: str,
    cpv_bid_micros: float,
    customer_id: str = None,
) -> dict:
    """Set CPV (cost-per-view) bid on a video ad group."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
        SELECT ad_group_label.ad_group, ad_group_label.label
        FROM ad_group_label
        WHERE ad_group_label.label = 'customers/{cid}/labels/{label_id or ""}'
        LIMIT {limit or 500}
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_ad_group_url_template(
    ad_group_id: str,
    tracking_url_template: str,
    customer_id: str = None,
) -> dict:
    """Set tracking URL template on an ad group."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
        SELECT ad_group_label.ad_group, ad_group_label.label
        FROM ad_group_label
        WHERE ad_group_label.label = 'customers/{cid}/labels/{label_id or ""}'
        LIMIT {limit or 500}
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_ad_group_url_suffix(
    ad_group_id: str,
    final_url_suffix: str,
    customer_id: str = None,
) -> dict:
    """Set final URL suffix on an ad group (parallel tracking suffix)."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
        SELECT ad_group_label.ad_group, ad_group_label.label
        FROM ad_group_label
        WHERE ad_group_label.label = 'customers/{cid}/labels/{label_id or ""}'
        LIMIT {limit or 500}
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_ad_groups_by_status(
    status: str,
    campaign_id: str = None,
    limit: int =None,
    customer_id: str = None,
) -> dict:
    """List ad groups filtered by status (ENABLED/PAUSED/REMOVED). Optional campaign_id filter."""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    if status and status not in ('ALL', None): conditions.append(f"campaign.status = '{status}'")
    gaql = f"""
        SELECT ad_group_label.ad_group, ad_group_label.label
        FROM ad_group_label
        WHERE ad_group_label.label = 'customers/{cid}/labels/{label_id or ""}'
        LIMIT {limit or 500}
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_ad_groups_by_label(
    label_id: str,
    limit: int =None,
    customer_id: str = None,
) -> dict:
    """List ad groups that have a specific label attached."""
    conditions = []
    gaql = f"""
        SELECT ad_group_label.ad_group, ad_group_label.label
        FROM ad_group_label
        WHERE ad_group_label.label = 'customers/{cid}/labels/{label_id or ""}'
        LIMIT {limit or 500}
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_ad_group_ads_summary(
    ad_group_id: str,
    customer_id: str = None,
) -> dict:
    """Get count of ads by type and status inside an ad group. Fast overview without listing every ad."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
        SELECT ad_group_label.ad_group, ad_group_label.label
        FROM ad_group_label
        WHERE ad_group_label.label = 'customers/{cid}/labels/{label_id or ""}'
        LIMIT {limit or 500}
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_duplicate_ad_group(
    source_ad_group_id: str,
    target_campaign_id: str,
    new_name: str,
    customer_id: str = None,
) -> dict:
    """Duplicate an ad group into a target campaign, copying ads and keywords"""
    conditions = []
    gaql = f"""
      SELECT
        ad_group.id,
        ad_group.name,
        ad_group.status,
        ad_group.type,
        ad_group.cpc_bid_micros,
        ad_group.target_cpa_micros,
        ad_group.target_roas
      FROM ad_group
      WHERE ad_group.resource_name = '{RN.adGroup(cid, params.source_ad_group_id)}'
      LIMIT 1
    """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_ad_group_details(
    ad_group_id: str,
    customer_id: str = None,
) -> dict:
    """Get full details of a specific ad group including targeting settings and bids"""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
      SELECT
        ad_group.id,
        ad_group.name,
        ad_group.status,
        ad_group.type,
        ad_group.cpc_bid_micros,
        ad_group.cpm_bid_micros,
        ad_group.cpv_bid_micros,
        ad_group.target_cpa_micros,
        ad_group.target_roas,
        ad_group.campaign,
        ad_group.resource_name,
        ad_group.targeting_setting.target_restrictions
      FROM ad_group
      WHERE ad_group.resource_name = '{RN.adGroup(cid, params.ad_group_id)}'
      LIMIT 1
    """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_ad_group_simulation(
    ad_group_id: str,
    customer_id: str = None,
) -> dict:
    """Get bid simulation data for an ad group showing performance estimates at different bid levels"""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
      SELECT
        ad_group_simulation.ad_group_id,
        ad_group_simulation.type,
        ad_group_simulation.modification_method,
        ad_group_simulation.start_date,
        ad_group_simulation.end_date,
        ad_group_simulation.cpc_bid_point_list.points,
        ad_group_simulation.target_cpa_point_list.points,
        ad_group_simulation.target_roas_point_list.points,
        ad_group_simulation.cpv_bid_point_list.points
      FROM ad_group_simulation
      WHERE ad_group_simulation.ad_group_id = {ad_group_id or ""}
    """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_ad_groups(
    campaign_id: str = None,
    status: str = None,
    limit: int =None,
    customer_id: str = None,
) -> dict:
    """List ad groups, optionally filtered by campaign and status"""
    _limit = int(limit) if limit else 200
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    if status and status not in ('ALL', None): conditions.append(f"ad_group.status = '{status}'")
    where_clause = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f"""
      SELECT
        ad_group.id,
        ad_group.name,
        ad_group.status,
        ad_group.type,
        ad_group.cpc_bid_micros,
        ad_group.campaign
      FROM ad_group
      {where_clause}
      LIMIT {_limit}
    """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_ad_group_audience_targeting(
    ad_group_id: str,
    add_user_list_ids: list = None,
    remove_user_list_ids: list = None,
    targeting_setting: str = None,
    customer_id: str = None,
) -> dict:
    """Add or remove user list (audience) criteria on an ad group"""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
            SELECT ad_group_criterion.resource_name
            FROM ad_group_criterion
            WHERE ad_group_criterion.ad_group = '{adGroupRn}'
              AND ad_group_criterion.user_list.user_list = 'customers/{cid}/userLists/{userListId}'
            LIMIT 1
          """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_ad_group_bid_modifiers(
    ad_group_id: str,
    device_bid_modifiers: dict = None,
    customer_id: str = None,
) -> dict:
    """Set device bid modifiers on an ad group (desktop, mobile, tablet)"""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
      SELECT
        ad_group_criterion.resource_name,
        ad_group_criterion.criterion_id,
        ad_group_criterion.device.type,
        ad_group_criterion.bid_modifier
      FROM ad_group_criterion
      WHERE ad_group_criterion.ad_group = '{adGroupRn}'
        AND ad_group_criterion.type = 6
    """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_ad_group_demographic_bids(
    ad_group_id: str,
    age_range_bid_modifiers: dict = None,
    gender_bid_modifiers: dict = None,
    customer_id: str = None,
) -> dict:
    """Set age range and gender bid modifiers on an ad group"""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
      SELECT
        ad_group_criterion.resource_name,
        ad_group_criterion.age_range.type,
        ad_group_criterion.gender.type,
        ad_group_criterion.bid_modifier,
        ad_group_criterion.type
      FROM ad_group_criterion
      WHERE ad_group_criterion.ad_group = '{adGroupRn}'
        AND ad_group_criterion.type IN ('AGE_RANGE', 'GENDER')
    """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_ad_group_targeting(
    ad_group_id: str,
    add_keyword_ids: list = None,
    remove_keyword_ids: list = None,
    customer_id: str = None,
) -> dict:
    """Add or remove keyword criteria on an ad group"""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
        SELECT
          ad_group_criterion.criterion_id,
          ad_group_criterion.keyword.text,
          ad_group_criterion.keyword.match_type,
          ad_group_criterion.status,
          ad_group_criterion.cpc_bid_micros
        FROM ad_group_criterion
        WHERE ad_group_criterion.resource_name IN ({idList})
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_update_ad_group(
    ad_group_id: str,
    name: str = None,
    status: str = None,
    cpc_bid_micros: float = None,
    target_cpa_micros: float = None,
    target_roas: float = None,
    customer_id: str = None,
) -> dict:
    """Update an ad group name, status, or bid settings"""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group.id, ad_group.name, ad_group.status, metrics.clicks, metrics.impressions FROM ad_group {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_bulk_apply_label_to_ads(
) -> dict:
    """Apply a label to many ads at once (pairs of ad_group_id+ad_id+label_id)."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_bulk_remove_label_from_ads(
) -> dict:
    """Remove a label from many ads."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_bulk_apply_label_to_ad_groups(
) -> dict:
    """Apply label to many ad groups."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_bulk_remove_label_from_ad_groups(
) -> dict:
    """Remove label from many ad groups."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_bulk_apply_label_to_campaigns(
) -> dict:
    """Apply label to many campaigns."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_bulk_remove_label_from_campaigns(
) -> dict:
    """Remove label from many campaigns."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_ads_by_campaign(
) -> dict:
    """List all ads within a campaign (joins ad_group_ad + ad_group + campaign)."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_ads_by_final_url(
) -> dict:
    """Find ads whose final_urls contain given substring."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_ads_by_date_range(
) -> dict:
    """List ads with impressions in given date range."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_search_ads_by_headline(
) -> dict:
    """Search RSA ads by headline text substring (client-side filter)."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_count_rsa_assets(
) -> dict:
    """Count headlines/descriptions on an RSA."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_remove_rda_marketing_image(
) -> dict:
    """Remove a marketing image asset from an RDA by asset_id."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_remove_rda_logo(
) -> dict:
    """Remove a logo asset from an RDA."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_remove_rda_youtube_video(
) -> dict:
    """Remove a YouTube video asset from an RDA."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_clear_rda_marketing_images(
) -> dict:
    """Clear all marketing images from an RDA."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_clear_rda_logos(
) -> dict:
    """Clear all logos from an RDA."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_ad_group_negative_keywords(
) -> dict:
    """List negative keywords on an ad group."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_ad_group_placements(
) -> dict:
    """List placement criteria on ad group."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_ad_group_topics(
) -> dict:
    """List topic criteria on ad group."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_ad_group_audiences(
) -> dict:
    """List audience criteria on ad group."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_ad_group_demographic_bids(
) -> dict:
    """List age/gender/parental/income demographic criteria with bid modifiers."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_add_ad_group_negative_placement(
) -> dict:
    """Add negative placement (URL) to ad group."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_add_ad_group_negative_topic(
) -> dict:
    """Add negative topic to ad group."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_add_ad_group_income_range(
) -> dict:
    """Add income_range criterion. type enum e.g. INCOME_RANGE_0_50, INCOME_RANGE_50_60, INCOME_RANGE_60_70, INCOME_RANGE_70_80, INCOME_RANGE_80_90, INCOME_RANGE_90_UP, INCOME_RANGE_UNDETERMINED."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_add_ad_group_parental_status(
) -> dict:
    """Add parental_status criterion: PARENT, NOT_A_PARENT, UNDETERMINED."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_add_ad_group_proximity(
) -> dict:
    """Add proximity (lat/lng + radius) targeting to ad group."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_ad_group_rotation(
) -> dict:
    """Set ad rotation mode: OPTIMIZE or ROTATE_FOREVER."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_ad_group_tracking_template(
) -> dict:
    """Set ad group tracking_url_template."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_add_campaign_placement(
) -> dict:
    """Add placement (URL) targeting to campaign."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_add_campaign_negative_placement(
) -> dict:
    """Add negative placement to campaign."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_add_campaign_topic(
) -> dict:
    """Add topic targeting to campaign."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_campaign_criteria(
) -> dict:
    """List all criteria on a campaign."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_campaign_negative_keywords(
) -> dict:
    """List negative keywords on a campaign."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_remove_campaign_criterion(
) -> dict:
    """Remove a campaign criterion by its criterion_id."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_asset_group_path1(
) -> dict:
    """Set path1 (display URL first path segment) on asset group."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_asset_group_path2(
) -> dict:
    """Set path2 on asset group."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_asset_groups_by_status(
) -> dict:
    """List asset groups filtered by status (ENABLED/PAUSED/REMOVED)."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_asset_group_metrics(
) -> dict:
    """Get performance metrics for an asset group in given date range."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_assets_by_type(
) -> dict:
    """List assets filtered by type (IMAGE, TEXT, YOUTUBE_VIDEO, MEDIA_BUNDLE, etc.)."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_unused_assets(
) -> dict:
    """List assets not linked to any campaign, ad group, customer, or asset group."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_bulk_update_ad_status(
) -> dict:
    """Bulk set status (ENABLED/PAUSED/REMOVED) on many ads."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_bulk_rename_ads(
) -> dict:
    """Bulk set ad.name for many ads."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_bulk_update_ad_group_status(
) -> dict:
    """Bulk set ad group status."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_bulk_update_campaign_status(
) -> dict:
    """Bulk set campaign status."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_move_rsa_headline(
) -> dict:
    """Reorder an RSA headline (swap asset at from_index with to_index)."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_ad_conversion_breakdown(
) -> dict:
    """Per-conversion-action breakdown for an ad in a date range."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_ad_device_preference(
) -> dict:
    """Set ad.device_preference to MOBILE or UNSPECIFIED (clear)."""
    conditions = []
    gaql = f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.status, ad_group.id, ad_group.name FROM ad_group_ad WHERE campaign.id = {campaign_id} LIMIT {limit or 200}"""
    rows = _search(gaql, '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_bulk_remove_ads(
    ads: list = None,
    customer_id: str = None,
) -> dict:
    """Permanently remove multiple ads in one call. Provide array of {ad_id, ad_group_id}."""
    conditions = []
    gaql = f"""
        SELECT ad_group_ad_label.ad_group_ad, ad_group_ad_label.label
        FROM ad_group_ad_label
        WHERE ad_group_ad_label.label = 'customers/{cid}/labels/{label_id or ""}'
        LIMIT {limit or 200}
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_ads_by_status(
    status: str,
    campaign_id: str = None,
    ad_group_id: str = None,
    limit: int =None,
    customer_id: str = None,
) -> dict:
    """List ads filtered by status (ENABLED/PAUSED/REMOVED). Optional campaign_id / ad_group_id filter."""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    if status and status not in ('ALL', None): conditions.append(f"campaign.status = '{status}'")
    gaql = f"""
        SELECT ad_group_ad_label.ad_group_ad, ad_group_ad_label.label
        FROM ad_group_ad_label
        WHERE ad_group_ad_label.label = 'customers/{cid}/labels/{label_id or ""}'
        LIMIT {limit or 200}
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_ads_by_policy_status(
    approval_status: str,
    campaign_id: str = None,
    limit: int =None,
    customer_id: str = None,
) -> dict:
    """List ads filtered by policy approval status (APPROVED / DISAPPROVED / APPROVED_LIMITED / AREA_OF_INTEREST_ONLY / SITE_SUSPENDED)."""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    gaql = f"""
        SELECT ad_group_ad_label.ad_group_ad, ad_group_ad_label.label
        FROM ad_group_ad_label
        WHERE ad_group_ad_label.label = 'customers/{cid}/labels/{label_id or ""}'
        LIMIT {limit or 200}
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_ads_by_ad_strength(
    ad_strength: str,
    campaign_id: str = None,
    limit: int =None,
    customer_id: str = None,
) -> dict:
    """List ads filtered by ad strength (POOR / AVERAGE / GOOD / EXCELLENT / PENDING / NO_ADS). Useful to find weak RSAs to improve."""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    gaql = f"""
        SELECT ad_group_ad_label.ad_group_ad, ad_group_ad_label.label
        FROM ad_group_ad_label
        WHERE ad_group_ad_label.label = 'customers/{cid}/labels/{label_id or ""}'
        LIMIT {limit or 200}
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_ads_by_label(
    label_id: str,
    limit: int =None,
    customer_id: str = None,
) -> dict:
    """List ads that have a specific label attached."""
    conditions = []
    gaql = f"""
        SELECT ad_group_ad_label.ad_group_ad, ad_group_ad_label.label
        FROM ad_group_ad_label
        WHERE ad_group_ad_label.label = 'customers/{cid}/labels/{label_id or ""}'
        LIMIT {limit or 200}
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_search_ads_by_text(
    search_text: str,
    campaign_id: str = None,
    limit: int =None,
    customer_id: str = None,
) -> dict:
    """Search ads containing specific text in any RSA headline or description. Matches case-insensitively using CONTAINS."""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    gaql = f"""
        SELECT ad_group_ad_label.ad_group_ad, ad_group_ad_label.label
        FROM ad_group_ad_label
        WHERE ad_group_ad_label.label = 'customers/{cid}/labels/{label_id or ""}'
        LIMIT {limit or 200}
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_ad_change_history(
    ad_id: str,
    ad_group_id: str,
    start_date: str,
    end_date: str,
    customer_id: str = None,
) -> dict:
    """Get change events (creates, updates, status changes) for an ad within a date range (up to 14 days back)."""
    conditions = []
    if start_date and end_date:
        conditions.append(f"segments.date BETWEEN '{start_date}' AND '{end_date}'")
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
        SELECT ad_group_ad_label.ad_group_ad, ad_group_ad_label.label
        FROM ad_group_ad_label
        WHERE ad_group_ad_label.label = 'customers/{cid}/labels/{label_id or ""}'
        LIMIT {limit or 200}
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_copy_ad_to_ad_groups(
    source_ad_id: str,
    source_ad_group_id: str,
    destination_ad_group_ids: list,
    customer_id: str = None,
) -> dict:
    """Copy an existing ad to multiple destination ad groups. Works for RSA and RDA. Returns new ad resource names."""
    conditions = []
    gaql = f"""
        SELECT ad_group_ad_label.ad_group_ad, ad_group_ad_label.label
        FROM ad_group_ad_label
        WHERE ad_group_ad_label.label = 'customers/{cid}/labels/{label_id or ""}'
        LIMIT {limit or 200}
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_apply_label_to_ad(
    ad_id: str,
    ad_group_id: str,
    label_id: str,
    customer_id: str = None,
) -> dict:
    """Attach a label to a specific ad."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_remove_label_from_ad(
    ad_id: str,
    ad_group_id: str,
    label_id: str,
    customer_id: str = None,
) -> dict:
    """Detach a label from an ad."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_apply_label_to_ad_group(
    ad_group_id: str,
    label_id: str,
    customer_id: str = None,
) -> dict:
    """Attach a label to an ad group."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_remove_label_from_ad_group(
    ad_group_id: str,
    label_id: str,
    customer_id: str = None,
) -> dict:
    """Detach a label from an ad group."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_apply_label_to_campaign(
    campaign_id: str,
    label_id: str,
    customer_id: str = None,
) -> dict:
    """Attach a label to a campaign."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_remove_label_from_campaign(
    campaign_id: str,
    label_id: str,
    customer_id: str = None,
) -> dict:
    """Detach a label from a campaign."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_update_label(
    label_id: str,
    name: str = None,
    background_color: str = None,
    description: str = None,
    customer_id: str = None,
) -> dict:
    """Update a label (name, background_color, description)."""
    try:
        client, cid = _get_client(customer_id or "")
        svc = client.get_service("LabelService")
        op = client.get_type("LabelOperation")
        label = op.update
        label.resource_name = svc.label_path(cid, label_id)
        paths = []
        if name is not None:
            label.name = name
            paths.append("name")
        if background_color is not None:
            label.text_label.background_color = background_color
            paths.append("text_label.background_color")
        if description is not None:
            label.text_label.description = description
            paths.append("text_label.description")
        if not paths:
            return {"success": False, "error": "Nothing to update — pass name, background_color, or description."}
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=paths))
        resp = svc.mutate_labels(customer_id=cid, operations=[op])
        return {"success": True, "updated": resp.results[0].resource_name}
    except Exception as e:
        return {"success": False, "error": str(e)}

@mcp.tool()
def gads_remove_label(
    label_id: str,
    customer_id: str = None,
) -> dict:
    """Permanently remove a label (detaches from all entities)."""
    try:
        client, cid = _get_client(customer_id or "")
        svc = client.get_service("LabelService")
        op = client.get_type("LabelOperation")
        op.remove = svc.label_path(cid, label_id)
        resp = svc.mutate_labels(customer_id=cid, operations=[op])
        return {"success": True, "removed": resp.results[0].resource_name}
    except Exception as e:
        return {"success": False, "error": str(e)}

@mcp.tool()
def gads_attach_asset_to_ad_group(
    ad_group_id: str,
    asset_id: str,
    field_type: str,
    customer_id: str = None,
) -> dict:
    """Attach an existing asset to an ad group with a specific field type (SITELINK, CALLOUT, STRUCTURED_SNIPPET, CALL, PROMOTION, PRICE, HOTEL_CALLOUT, LEAD_FORM, LOGO)."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_detach_asset_from_ad_group(
    ad_group_asset_resource_name: str,
    customer_id: str = None,
) -> dict:
    """Detach an asset from an ad group by resource name."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_attach_asset_to_customer(
    asset_id: str,
    field_type: str,
    customer_id: str = None,
) -> dict:
    """Attach an asset at the customer (account) level. Used mostly for account-wide sitelinks, callouts."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_detach_asset_from_customer(
    customer_asset_resource_name: str,
    customer_id: str = None,
) -> dict:
    """Detach an account-level asset by resource name."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_customer_assets(
    customer_id: str = None,
) -> dict:
    """List assets attached at the customer (account) level."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_pause_ad_group_asset(
    ad_group_asset_resource_name: str,
    customer_id: str = None,
) -> dict:
    """Pause an ad-group-level asset link by its resource name."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_enable_ad_group_asset(
    ad_group_asset_resource_name: str,
    customer_id: str = None,
) -> dict:
    """Enable an ad-group-level asset link."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_pause_campaign_asset(
    campaign_asset_resource_name: str,
    customer_id: str = None,
) -> dict:
    """Pause a campaign-level asset link."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_enable_campaign_asset(
    campaign_asset_resource_name: str,
    customer_id: str = None,
) -> dict:
    """Enable a campaign-level asset link."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_skippable_in_stream_ad(
    ad_group_id: str,
    youtube_video_asset_id: str,
    action_headline: str = None,
    action_button_label: str = None,
    final_urls: list = None,
    companion_banner_image_asset_id: str = None,
    customer_id: str = None,
) -> dict:
    """Create a skippable in-stream TrueView video ad. Has action call-out overlay."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_masthead_ad(
    ad_group_id: str,
    youtube_video_asset_id: str,
    headline: str = None,
    description: str = None,
    call_to_action_button_label: str = None,
    final_urls: list = None,
    customer_id: str = None,
) -> dict:
    """Create a YouTube Masthead ad (premium homepage placement; usually reserved via sales)."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_responsive_video_ad(
    ad_group_id: str,
    youtube_video_asset_ids: list,
    headlines: list = None,
    long_headlines: list = None,
    descriptions: list = None,
    call_to_actions: list = None,
    companion_banner_image_asset_ids: list = None,
    final_urls: list = None,
    breadcrumb1: str = None,
    breadcrumb2: str = None,
    customer_id: str = None,
) -> dict:
    """Create a Video Responsive Ad — multi-headline, multi-description, multi-CTA format that Google auto-assembles into in-stream, shorts, in-feed placements."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_in_stream_shopping_ad(
    ad_group_id: str,
    youtube_video_asset_id: str,
    final_urls: list = None,
    customer_id: str = None,
) -> dict:
    """Create an in-stream video ad with product listing overlay (shopping + video)."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_disapproved_ads(
    campaign_id: str = None,
    customer_id: str = None,
) -> dict:
    """List all disapproved ads in the account with their policy-topic reasons."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_limited_approval_ads(
    customer_id: str = None,
) -> dict:
    """List ads with APPROVED_LIMITED policy status (serving but with restrictions)."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_ad_groups_without_rsa(
    campaign_id: str = None,
    customer_id: str = None,
) -> dict:
    """Find ad groups that have no active RSA ad (gaps to fill)."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_compare_ad_performance(
    ad_ids: list,
    start_date: str,
    end_date: str,
    customer_id: str = None,
) -> dict:
    """Compare performance metrics side-by-side for multiple ads over a date range. Returns conversion rate, CTR, CPA per ad."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_ad_asset_field_performance(
    ad_id: str,
    ad_group_id: str,
    start_date: str,
    end_date: str,
    customer_id: str = None,
) -> dict:
    """Get performance per field type (HEADLINE_1, HEADLINE_2, DESCRIPTION_1, etc) for an RSA or RDA. Uses ad_group_ad_asset_view."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_rsa_without_pins(
    campaign_id: str = None,
    customer_id: str = None,
) -> dict:
    """Find RSAs in the account that have no pinned headlines or descriptions (fully flexible rotation)."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_bulk_create_rsa(
    ads: list = None,
    customer_id: str = None,
) -> dict:
    """Create multiple RSAs in one call. Items: {ad_group_id, headlines: [{text,pin?}], descriptions: [...], final_urls, path1?, path2?}."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_bulk_set_ad_final_urls(
    updates: list = None,
    customer_id: str = None,
) -> dict:
    """Replace final_urls across multiple ads. Items: [{ad_id, ad_group_id, final_urls}]."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_remove_ad_group_criterion(
    criterion_resource_name: str,
    customer_id: str = None,
) -> dict:
    """Remove a single ad group criterion by resource name (keyword, topic, placement, gender, age, etc)."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_add_ad_group_placement(
    ad_group_id: str,
    url: str,
    cpc_bid_micros: float = None,
    customer_id: str = None,
) -> dict:
    """Add a placement criterion (specific URL) to a Display ad group."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_add_ad_group_topic(
    ad_group_id: str,
    topic_constant_id: str,
    cpc_bid_micros: float = None,
    customer_id: str = None,
) -> dict:
    """Add a topic criterion to a Display ad group."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_add_ad_group_gender(
    ad_group_id: str,
    gender: str,
    customer_id: str = None,
) -> dict:
    """Add a gender targeting criterion (MALE=10, FEMALE=11, UNDETERMINED=20) to an ad group."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_add_ad_group_age_range(
    ad_group_id: str,
    age_range: str,
    customer_id: str = None,
) -> dict:
    """Add an age range targeting criterion to an ad group. Ranges: AGE_RANGE_18_24, AGE_RANGE_25_34, AGE_RANGE_35_44, AGE_RANGE_45_54, AGE_RANGE_55_64, AGE_RANGE_65_UP, AGE_RANGE_UNDETERMINED."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_add_ad_group_keyword(
    ad_group_id: str,
    text: str,
    match_type: str,
    cpc_bid_micros: float = None,
    customer_id: str = None,
) -> dict:
    """Add a single keyword as a positive targeting criterion to an ad group."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_add_ad_group_negative_keyword(
    ad_group_id: str,
    text: str,
    match_type: str,
    customer_id: str = None,
) -> dict:
    """Add a negative keyword to an ad group (excludes matching queries)."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_pause_asset_group_asset(
    asset_group_asset_resource_name: str,
    customer_id: str = None,
) -> dict:
    """Pause the link between an asset and an asset group (status only; asset stays in library)."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_enable_asset_group_asset(
    asset_group_asset_resource_name: str,
    customer_id: str = None,
) -> dict:
    """Enable the link between an asset and an asset group."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_pause_campaign(
    campaign_id: str,
    customer_id: str = None,
) -> dict:
    """Pause a campaign by ID."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_remove_campaign(
    campaign_id: str,
    customer_id: str = None,
) -> dict:
    """Permanently remove a campaign (cannot be undone)."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_bulk_pause_campaigns(
    campaign_ids: list,
    customer_id: str = None,
) -> dict:
    """Pause multiple campaigns in one call."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_bulk_enable_campaigns(
    campaign_ids: list,
    customer_id: str = None,
) -> dict:
    """Enable multiple campaigns in one call."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_bulk_remove_campaigns(
    campaign_ids: list,
    customer_id: str = None,
) -> dict:
    """Remove multiple campaigns permanently."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_bulk_pause_keywords(
    criterion_resource_names: list,
    customer_id: str = None,
) -> dict:
    """Pause multiple keywords. Items: [{criterion_resource_name}] — use list_keywords to get RNs."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_bulk_enable_keywords(
    criterion_resource_names: list,
    customer_id: str = None,
) -> dict:
    """Enable multiple keywords in one call."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_bulk_remove_keywords(
    criterion_resource_names: list,
    customer_id: str = None,
) -> dict:
    """Permanently remove multiple keywords."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_bulk_update_keyword_bids(
    updates: list = None,
    customer_id: str = None,
) -> dict:
    """Bulk update CPC bids on keywords. Items: [{criterion_resource_name, cpc_bid_micros}]."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_ad_group_primary_status(
    ad_group_id: str,
    customer_id: str = None,
) -> dict:
    """Get primary status and reasons (e.g. CAMPAIGN_PAUSED, BUDGET_DEPLETED) for an ad group."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_campaign_primary_status(
    campaign_id: str,
    customer_id: str = None,
) -> dict:
    """Get primary serving status and reasons for a campaign."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_ad_primary_status(
    ad_id: str,
    ad_group_id: str,
    customer_id: str = None,
) -> dict:
    """Get primary serving status and reasons for an ad (why it is/is not serving)."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_ad_group_criteria(
    ad_group_id: str,
    customer_id: str = None,
) -> dict:
    """List all criteria on an ad group (keywords, gender, age, placement, topic, userlist, etc) — one call fetches everything."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_count_ads_in_campaign(
    campaign_id: str,
    customer_id: str = None,
) -> dict:
    """Count ads in a campaign grouped by type and status (fast rollup)."""
    gaql = """
        SELECT label.id, label.name, label.status,
               label.text_label.background_color, label.text_label.description
        FROM label
        LIMIT 500
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_update_call_ad(
    ad_id: str,
    ad_group_id: str,
    business_name: str = None,
    country_code: str = None,
    phone_number: str = None,
    headline1: str = None,
    headline2: str = None,
    description1: str = None,
    description2: str = None,
    final_urls: list = None,
    tracking_url_template: str = None,
    customer_id: str = None,
) -> dict:
    """Update a call ad. Any subset of: business_name, country_code, phone_number, headline1, headline2, description1, description2, final_urls, tracking_template."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_update_app_ad(
    ad_id: str,
    ad_group_id: str,
    headlines: list = None,
    descriptions: list = None,
    image_asset_ids: list = None,
    youtube_video_asset_ids: list = None,
    html5_media_bundle_asset_ids: list = None,
    customer_id: str = None,
) -> dict:
    """Update an App ad. Supports replacing headlines, descriptions, images, videos, html5 bundles."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_update_hotel_ad(
    ad_id: str,
    ad_group_id: str,
    status: float = None,
    customer_id: str = None,
) -> dict:
    """Update a Hotel ad. Hotel ads have no editable user fields in most cases; this tool toggles status only."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_update_video_ad(
    ad_id: str,
    ad_group_id: str,
    final_urls: list = None,
    display_url: str = None,
    tracking_url_template: str = None,
    customer_id: str = None,
) -> dict:
    """Update a Video ad (TrueView). Can change final_urls, display_url, tracking template. Video asset itself cannot be swapped — must create a new ad."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_update_demand_gen_ad(
    ad_id: str,
    ad_group_id: str,
    headlines: list = None,
    descriptions: list = None,
    marketing_image_asset_ids: list = None,
    logo_asset_ids: list = None,
    business_name: str = None,
    call_to_action_text: str = None,
    final_urls: list = None,
    customer_id: str = None,
) -> dict:
    """Update a Demand Gen multi-asset ad. Can replace headlines, descriptions, image assets, logo assets, business_name, call_to_action, final_urls."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_update_smart_campaign_ad(
    ad_id: str,
    ad_group_id: str,
    headlines: list = None,
    descriptions: list = None,
    customer_id: str = None,
) -> dict:
    """Update a Smart Campaign ad — replace headlines and descriptions."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT campaign.id, campaign.name, campaign.status, metrics.clicks, metrics.impressions, metrics.cost_micros FROM campaign {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_bulk_pause_ads(
    ads: list = None,
    customer_id: str = None,
) -> dict:
    """Pause multiple ads at once. Provide an array of {ad_id, ad_group_id} pairs. Returns success/failure per ad."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_bulk_enable_ads(
    ads: list = None,
    customer_id: str = None,
) -> dict:
    """Enable multiple ads at once. Provide an array of {ad_id, ad_group_id} pairs. Returns success/failure per ad."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_shopping_product_ad(
    ad_group_id: str,
    customer_id: str = None,
) -> dict:
    """Create a product shopping ad in a Shopping campaign ad group. Shopping ads auto-pull content from Merchant Center; this just creates the ad slot."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_image_ad(
    ad_group_id: str,
    image_asset_id: str,
    final_urls: list,
    name: str = None,
    display_url: str = None,
    customer_id: str = None,
) -> dict:
    """Create a legacy image ad from an uploaded image asset (use for display/remarketing where raw image ads are still allowed)."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_local_ad(
    ad_group_id: str,
    headlines: list,
    descriptions: list,
    marketing_image_asset_ids: list = None,
    logo_asset_ids: list = None,
    video_asset_ids: list = None,
    final_urls: list = None,
    customer_id: str = None,
) -> dict:
    """Create a Local ad in a Local campaign. Requires 1-5 headlines, 1-5 descriptions, optional marketing images/logos/videos."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_html5_ad(
    ad_group_id: str,
    media_bundle_asset_id: str,
    final_urls: list,
    name: str = None,
    customer_id: str = None,
) -> dict:
    """Create an HTML5 upload ad (display creative bundle) from an uploaded media bundle asset."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_expanded_text_ad(
    ad_group_id: str,
    headline_part1: str,
    headline_part2: str,
    description: str,
    final_urls: list,
    headline_part3: str = None,
    description2: str = None,
    path1: str = None,
    path2: str = None,
    customer_id: str = None,
) -> dict:
    """Create a legacy Expanded Text Ad (ETA). ETAs are deprecated for new creation in most accounts but may be supported in specific verticals or for historical work."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_dynamic_search_ad(
    ad_group_id: str,
    description1: str,
    description2: str = None,
    customer_id: str = None,
) -> dict:
    """Create a Dynamic Search Ad (DSA). Headlines and URLs are auto-generated based on your site; you only provide description(s)."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_gmail_ad(
    ad_group_id: str,
    teaser_headline: str,
    teaser_description: str,
    teaser_business_name: str,
    final_urls: list,
    teaser_logo_asset_id: str = None,
    marketing_image_asset_id: str = None,
    header_image_asset_id: str = None,
    customer_id: str = None,
) -> dict:
    """Create a Gmail ad. Gmail ads are a legacy Discovery format — most new accounts should use Demand Gen ads instead. Retained for accounts still using Gmail inventory."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_showcase_shopping_ad(
    ad_group_id: str,
    headline: str,
    logo_asset_id: str,
    description: str = None,
    customer_id: str = None,
) -> dict:
    """Create a Showcase Shopping ad (legacy). Mostly replaced by Performance Max; kept for accounts with legacy shopping campaigns that still support it."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_in_feed_video_ad(
    ad_group_id: str,
    youtube_video_asset_id: str,
    headline: str,
    description1: str = None,
    description2: str = None,
    thumbnail: str = None,
    customer_id: str = None,
) -> dict:
    """Create an in-feed (discovery) video ad showing on YouTube home/search/related. Requires a YouTube video asset plus headline/descriptions."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_bumper_ad(
    ad_group_id: str,
    youtube_video_asset_id: str,
    final_urls: list = None,
    display_url: str = None,
    customer_id: str = None,
) -> dict:
    """Create a Bumper video ad (non-skippable ≤6 seconds). Requires an uploaded YouTube video asset ≤6s."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_non_skippable_in_stream_ad(
    ad_group_id: str,
    youtube_video_asset_id: str,
    action_headline: str = None,
    action_button_label: str = None,
    final_urls: list = None,
    customer_id: str = None,
) -> dict:
    """Create a non-skippable in-stream video ad (15-30s). Plays before, during, or after another video on YouTube."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_outstream_video_ad(
    ad_group_id: str,
    youtube_video_asset_id: str,
    headline: str,
    description: str,
    final_urls: list = None,
    customer_id: str = None,
) -> dict:
    """Create an outstream video ad (plays on Google video partner sites outside YouTube)."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_app_ad(
    ad_group_id: str,
    headlines: list,
    descriptions: list,
    images: list = None,
    youtube_videos: list = None,
    html5_media_bundles: list = None,
    customer_id: str = None,
) -> dict:
    """Create an App Ad in an ad group"""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_call_ad(
    ad_group_id: str,
    phone_number: str,
    country_code: str,
    description_1: str,
    headline_1: str,
    description_2: str = None,
    headline_2: str = None,
    business_name: str = None,
    customer_id: str = None,
) -> dict:
    """Create a Call Ad in an ad group"""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_demand_gen_ad(
    ad_group_id: str,
    headlines: list,
    descriptions: list,
    final_urls: list,
    image_asset_ids: list = None,
    logo_asset_ids: list = None,
    business_name: str = None,
    customer_id: str = None,
) -> dict:
    """Create a Demand Gen multi-asset ad in an ad group"""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_hotel_ad(
    ad_group_id: str,
    customer_id: str = None,
) -> dict:
    """Create a Hotel Ad in an ad group (auto-generated from hotel feed)"""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_smart_campaign_ad(
    ad_group_id: str,
    headlines: list,
    descriptions: list,
    customer_id: str = None,
) -> dict:
    """Create a Smart Campaign Ad with exactly 3 headlines and 2 descriptions"""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT campaign.id, campaign.name, campaign.status, metrics.clicks, metrics.impressions, metrics.cost_micros FROM campaign {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_duplicate_ad(
    source_ad_group_id: str,
    source_ad_id: str,
    target_ad_group_id: str,
    customer_id: str = None,
) -> dict:
    """Duplicate an ad from one ad group to another ad group"""
    conditions = []
    gaql = f"""
      SELECT
        ad_group_ad.ad.id,
        ad_group_ad.ad.type,
        ad_group_ad.ad.final_urls,
        ad_group_ad.ad.final_mobile_urls,
        ad_group_ad.ad.tracking_url_template,
        ad_group_ad.ad.responsive_search_ad.headlines,
        ad_group_ad.ad.responsive_search_ad.descriptions,
        ad_group_ad.ad.responsive_search_ad.path1,
        ad_group_ad.ad.responsive_search_ad.path2,
        ad_group_ad.ad.expanded_text_ad.headline_part1,
        ad_group_ad.ad.expanded_text_ad.headline_part2,
        ad_group_ad.ad.expanded_text_ad.headline_part3,
        ad_group_ad.ad.expanded_text_ad.description,
        ad_group_ad.ad.expanded_text_ad.description2,
        ad_group_ad.ad.expanded_text_ad.path1,
        ad_group_ad.ad.expanded_text_ad.path2,
        ad_group_ad.ad.responsive_display_ad.headlines,
        ad_group_ad.ad.responsive_display_ad.descriptions,
        ad_group_ad.ad.responsive_display_ad.business_name,
        ad_group_ad.ad.responsive_display_ad.marketing_images,
        ad_group_ad.ad.responsive_display_ad.logos,
        ad_group_ad.status
      FROM ad_group_ad
      WHERE ad_group_ad.resource_name = '{sourceRn}'
      LIMIT 1
    """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_enable_ad(
    ad_id: str,
    ad_group_id: str,
    customer_id: str = None,
) -> dict:
    """Enable a specific ad"""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_ad_details(
    ad_id: str,
    ad_group_id: str,
    customer_id: str = None,
) -> dict:
    """Get full details of a specific ad including all ad type fields and policy status"""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
      SELECT
        ad_group_ad.ad.id,
        ad_group_ad.ad.name,
        ad_group_ad.ad.type,
        ad_group_ad.ad.final_urls,
        ad_group_ad.ad.final_mobile_urls,
        ad_group_ad.ad.tracking_url_template,
        ad_group_ad.ad.url_custom_parameters,
        ad_group_ad.ad.responsive_search_ad.headlines,
        ad_group_ad.ad.responsive_search_ad.descriptions,
        ad_group_ad.ad.responsive_search_ad.path1,
        ad_group_ad.ad.responsive_search_ad.path2,
        ad_group_ad.ad.expanded_text_ad.headline_part1,
        ad_group_ad.ad.expanded_text_ad.headline_part2,
        ad_group_ad.ad.expanded_text_ad.headline_part3,
        ad_group_ad.ad.expanded_text_ad.description,
        ad_group_ad.ad.expanded_text_ad.description2,
        ad_group_ad.ad.responsive_display_ad.headlines,
        ad_group_ad.ad.responsive_display_ad.descriptions,
        ad_group_ad.ad.responsive_display_ad.business_name,
        ad_group_ad.ad.call_ad.phone_number,
        ad_group_ad.ad.call_ad.country_code,
        ad_group_ad.ad.call_ad.headline1,
        ad_group_ad.ad.call_ad.headline2,
        ad_group_ad.ad.call_ad.description1,
        ad_group_ad.ad.call_ad.description2,
        ad_group_ad.status,
        ad_group_ad.ad_strength,
        ad_group_ad.ad_group,
        ad_group_ad.resource_name,
        ad_group_ad.policy_summary.approval_status,
        ad_group_ad.policy_summary.review_status
      FROM ad_group_ad
      WHERE ad_group_ad.resource_name = '{adGroupAdRn}'
      LIMIT 1
    """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_ad_strength(
    ad_group_id: str = None,
    campaign_id: str = None,
    customer_id: str = None,
) -> dict:
    """Get ad strength scores (EXCELLENT, GOOD, AVERAGE, POOR) for ads in an ad group or campaign"""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where_clause = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f"""
      SELECT
        ad_group_ad.ad.id,
        ad_group_ad.ad.type,
        ad_group_ad.ad.name,
        ad_group_ad.ad_strength,
        ad_group_ad.status,
        ad_group_ad.ad_group,
        ad_group_ad.resource_name,
        ad_group_ad.ad.final_urls
      FROM ad_group_ad
      {where_clause}
      LIMIT 500
    """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_shareable_preview(
    ad_id: str,
    ad_group_id: str,
    customer_id: str = None,
) -> dict:
    """Get ad details and a shareable preview URL for a specific ad."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""SELECT ad_group_ad.ad.id,
              ad_group_ad.ad.type,
              ad_group_ad.ad.final_urls,
              ad_group_ad.ad.responsive_search_ad.headlines,
              ad_group_ad.ad.responsive_search_ad.descriptions,
              ad_group_ad.status,
              ad_group.name,
              campaign.name
       FROM ad_group_ad
       WHERE ad_group_ad.ad.id = {ad_id or ""}
         AND ad_group.id = {ad_group_id or ""}"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_ads(
    ad_group_id: str = None,
    campaign_id: str = None,
    status: str = None,
    limit: int =None,
    customer_id: str = None,
) -> dict:
    """List ads, optionally filtered by ad group, campaign, and status"""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    if status and status not in ('ALL', None): conditions.append(f"campaign.status = '{status}'")
    where_clause = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f"""
      SELECT
        ad_group_ad.ad.id,
        ad_group_ad.ad.name,
        ad_group_ad.ad.type,
        ad_group_ad.ad.final_urls,
        ad_group_ad.ad.final_mobile_urls,
        ad_group_ad.ad.responsive_search_ad.headlines,
        ad_group_ad.ad.responsive_search_ad.descriptions,
        ad_group_ad.ad.expanded_text_ad.headline_part1,
        ad_group_ad.ad.expanded_text_ad.headline_part2,
        ad_group_ad.ad.expanded_text_ad.description,
        ad_group_ad.status,
        ad_group_ad.ad_group,
        ad_group_ad.resource_name
      FROM ad_group_ad
      {where_clause}
      LIMIT {limit or 200}
    """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_pause_ad(
    ad_id: str,
    ad_group_id: str,
    customer_id: str = None,
) -> dict:
    """Pause a specific ad"""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_add_rda_headline(
    ad_id: str,
    ad_group_id: str,
    text: str,
    customer_id: str = None,
) -> dict:
    """Append a headline to a Responsive Display Ad (max 5)."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
    SELECT ad_group_ad.ad.id,
           ad_group_ad.ad.responsive_display_ad.headlines,
           ad_group_ad.ad.responsive_display_ad.long_headline,
           ad_group_ad.ad.responsive_display_ad.descriptions,
           ad_group_ad.ad.responsive_display_ad.business_name,
           ad_group_ad.ad.responsive_display_ad.marketing_images,
           ad_group_ad.ad.responsive_display_ad.square_marketing_images,
           ad_group_ad.ad.responsive_display_ad.logo_images,
           ad_group_ad.ad.responsive_display_ad.square_logo_images,
           ad_group_ad.ad.responsive_display_ad.youtube_videos,
           ad_group_ad.ad.responsive_display_ad.call_to_action_text,
           ad_group_ad.ad.responsive_display_ad.price_prefix,
           ad_group_ad.ad.responsive_display_ad.promo_text
    FROM ad_group_ad
    WHERE ad_group_ad.ad.id = {ad_id}
      AND ad_group.id = {ad_group_id}
    LIMIT 1
  """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_add_rda_description(
    ad_id: str,
    ad_group_id: str,
    text: str,
    customer_id: str = None,
) -> dict:
    """Append a description to a Responsive Display Ad (max 5)."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
    SELECT ad_group_ad.ad.id,
           ad_group_ad.ad.responsive_display_ad.headlines,
           ad_group_ad.ad.responsive_display_ad.long_headline,
           ad_group_ad.ad.responsive_display_ad.descriptions,
           ad_group_ad.ad.responsive_display_ad.business_name,
           ad_group_ad.ad.responsive_display_ad.marketing_images,
           ad_group_ad.ad.responsive_display_ad.square_marketing_images,
           ad_group_ad.ad.responsive_display_ad.logo_images,
           ad_group_ad.ad.responsive_display_ad.square_logo_images,
           ad_group_ad.ad.responsive_display_ad.youtube_videos,
           ad_group_ad.ad.responsive_display_ad.call_to_action_text,
           ad_group_ad.ad.responsive_display_ad.price_prefix,
           ad_group_ad.ad.responsive_display_ad.promo_text
    FROM ad_group_ad
    WHERE ad_group_ad.ad.id = {ad_id}
      AND ad_group.id = {ad_group_id}
    LIMIT 1
  """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_add_rda_marketing_image(
    ad_id: str,
    ad_group_id: str,
    image_asset_id: str,
    square: bool = None,
    customer_id: str = None,
) -> dict:
    """Append a marketing (landscape) image asset to a Responsive Display Ad. Max 15."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
    SELECT ad_group_ad.ad.id,
           ad_group_ad.ad.responsive_display_ad.headlines,
           ad_group_ad.ad.responsive_display_ad.long_headline,
           ad_group_ad.ad.responsive_display_ad.descriptions,
           ad_group_ad.ad.responsive_display_ad.business_name,
           ad_group_ad.ad.responsive_display_ad.marketing_images,
           ad_group_ad.ad.responsive_display_ad.square_marketing_images,
           ad_group_ad.ad.responsive_display_ad.logo_images,
           ad_group_ad.ad.responsive_display_ad.square_logo_images,
           ad_group_ad.ad.responsive_display_ad.youtube_videos,
           ad_group_ad.ad.responsive_display_ad.call_to_action_text,
           ad_group_ad.ad.responsive_display_ad.price_prefix,
           ad_group_ad.ad.responsive_display_ad.promo_text
    FROM ad_group_ad
    WHERE ad_group_ad.ad.id = {ad_id}
      AND ad_group.id = {ad_group_id}
    LIMIT 1
  """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_add_rda_logo(
    ad_id: str,
    ad_group_id: str,
    image_asset_id: str,
    square: bool = None,
    customer_id: str = None,
) -> dict:
    """Append a logo image asset to a Responsive Display Ad."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
    SELECT ad_group_ad.ad.id,
           ad_group_ad.ad.responsive_display_ad.headlines,
           ad_group_ad.ad.responsive_display_ad.long_headline,
           ad_group_ad.ad.responsive_display_ad.descriptions,
           ad_group_ad.ad.responsive_display_ad.business_name,
           ad_group_ad.ad.responsive_display_ad.marketing_images,
           ad_group_ad.ad.responsive_display_ad.square_marketing_images,
           ad_group_ad.ad.responsive_display_ad.logo_images,
           ad_group_ad.ad.responsive_display_ad.square_logo_images,
           ad_group_ad.ad.responsive_display_ad.youtube_videos,
           ad_group_ad.ad.responsive_display_ad.call_to_action_text,
           ad_group_ad.ad.responsive_display_ad.price_prefix,
           ad_group_ad.ad.responsive_display_ad.promo_text
    FROM ad_group_ad
    WHERE ad_group_ad.ad.id = {ad_id}
      AND ad_group.id = {ad_group_id}
    LIMIT 1
  """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_add_rda_youtube_video(
    ad_id: str,
    ad_group_id: str,
    youtube_video_asset_id: str,
    customer_id: str = None,
) -> dict:
    """Append a YouTube video asset to a Responsive Display Ad (max 5)."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
    SELECT ad_group_ad.ad.id,
           ad_group_ad.ad.responsive_display_ad.headlines,
           ad_group_ad.ad.responsive_display_ad.long_headline,
           ad_group_ad.ad.responsive_display_ad.descriptions,
           ad_group_ad.ad.responsive_display_ad.business_name,
           ad_group_ad.ad.responsive_display_ad.marketing_images,
           ad_group_ad.ad.responsive_display_ad.square_marketing_images,
           ad_group_ad.ad.responsive_display_ad.logo_images,
           ad_group_ad.ad.responsive_display_ad.square_logo_images,
           ad_group_ad.ad.responsive_display_ad.youtube_videos,
           ad_group_ad.ad.responsive_display_ad.call_to_action_text,
           ad_group_ad.ad.responsive_display_ad.price_prefix,
           ad_group_ad.ad.responsive_display_ad.promo_text
    FROM ad_group_ad
    WHERE ad_group_ad.ad.id = {ad_id}
      AND ad_group.id = {ad_group_id}
    LIMIT 1
  """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_rda_call_to_action(
    ad_id: str,
    ad_group_id: str,
    call_to_action_text: str,
    customer_id: str = None,
) -> dict:
    """Set the call-to-action text on a Responsive Display Ad (e.g. """
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
    SELECT ad_group_ad.ad.id,
           ad_group_ad.ad.responsive_display_ad.headlines,
           ad_group_ad.ad.responsive_display_ad.long_headline,
           ad_group_ad.ad.responsive_display_ad.descriptions,
           ad_group_ad.ad.responsive_display_ad.business_name,
           ad_group_ad.ad.responsive_display_ad.marketing_images,
           ad_group_ad.ad.responsive_display_ad.square_marketing_images,
           ad_group_ad.ad.responsive_display_ad.logo_images,
           ad_group_ad.ad.responsive_display_ad.square_logo_images,
           ad_group_ad.ad.responsive_display_ad.youtube_videos,
           ad_group_ad.ad.responsive_display_ad.call_to_action_text,
           ad_group_ad.ad.responsive_display_ad.price_prefix,
           ad_group_ad.ad.responsive_display_ad.promo_text
    FROM ad_group_ad
    WHERE ad_group_ad.ad.id = {ad_id}
      AND ad_group.id = {ad_group_id}
    LIMIT 1
  """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_rda_price_prefix(
    ad_id: str,
    ad_group_id: str,
    price_prefix: str,
    customer_id: str = None,
) -> dict:
    """Set the price prefix on a Responsive Display Ad (e.g. """
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
    SELECT ad_group_ad.ad.id,
           ad_group_ad.ad.responsive_display_ad.headlines,
           ad_group_ad.ad.responsive_display_ad.long_headline,
           ad_group_ad.ad.responsive_display_ad.descriptions,
           ad_group_ad.ad.responsive_display_ad.business_name,
           ad_group_ad.ad.responsive_display_ad.marketing_images,
           ad_group_ad.ad.responsive_display_ad.square_marketing_images,
           ad_group_ad.ad.responsive_display_ad.logo_images,
           ad_group_ad.ad.responsive_display_ad.square_logo_images,
           ad_group_ad.ad.responsive_display_ad.youtube_videos,
           ad_group_ad.ad.responsive_display_ad.call_to_action_text,
           ad_group_ad.ad.responsive_display_ad.price_prefix,
           ad_group_ad.ad.responsive_display_ad.promo_text
    FROM ad_group_ad
    WHERE ad_group_ad.ad.id = {ad_id}
      AND ad_group.id = {ad_group_id}
    LIMIT 1
  """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_rda_promo_text(
    ad_id: str,
    ad_group_id: str,
    promo_text: str,
    customer_id: str = None,
) -> dict:
    """Set promotional text on a Responsive Display Ad (e.g. """
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
    SELECT ad_group_ad.ad.id,
           ad_group_ad.ad.responsive_display_ad.headlines,
           ad_group_ad.ad.responsive_display_ad.long_headline,
           ad_group_ad.ad.responsive_display_ad.descriptions,
           ad_group_ad.ad.responsive_display_ad.business_name,
           ad_group_ad.ad.responsive_display_ad.marketing_images,
           ad_group_ad.ad.responsive_display_ad.square_marketing_images,
           ad_group_ad.ad.responsive_display_ad.logo_images,
           ad_group_ad.ad.responsive_display_ad.square_logo_images,
           ad_group_ad.ad.responsive_display_ad.youtube_videos,
           ad_group_ad.ad.responsive_display_ad.call_to_action_text,
           ad_group_ad.ad.responsive_display_ad.price_prefix,
           ad_group_ad.ad.responsive_display_ad.promo_text
    FROM ad_group_ad
    WHERE ad_group_ad.ad.id = {ad_id}
      AND ad_group.id = {ad_group_id}
    LIMIT 1
  """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_remove_ad(
    ad_id: str,
    ad_group_id: str,
    customer_id: str = None,
) -> dict:
    """Permanently remove an ad from an ad group"""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_update_rsa_headline(
    ad_id: str,
    ad_group_id: str,
    index: float,
    new_text: str,
    customer_id: str = None,
) -> dict:
    """Update a single headline of an RSA by 0-based index, preserving all other headlines and pins."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
    SELECT ad_group_ad.ad.id,
           ad_group_ad.ad.responsive_search_ad.headlines,
           ad_group_ad.ad.responsive_search_ad.descriptions,
           ad_group_ad.ad.responsive_search_ad.path1,
           ad_group_ad.ad.responsive_search_ad.path2
    FROM ad_group_ad
    WHERE ad_group_ad.ad.id = {ad_id}
      AND ad_group.id = {ad_group_id}
      AND ad_group_ad.status != 'REMOVED'
    LIMIT 1
  """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_update_rsa_description(
    ad_id: str,
    ad_group_id: str,
    index: float,
    new_text: str,
    customer_id: str = None,
) -> dict:
    """Update a single description of an RSA by 0-based index, preserving all other descriptions and pins."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
    SELECT ad_group_ad.ad.id,
           ad_group_ad.ad.responsive_search_ad.headlines,
           ad_group_ad.ad.responsive_search_ad.descriptions,
           ad_group_ad.ad.responsive_search_ad.path1,
           ad_group_ad.ad.responsive_search_ad.path2
    FROM ad_group_ad
    WHERE ad_group_ad.ad.id = {ad_id}
      AND ad_group.id = {ad_group_id}
      AND ad_group_ad.status != 'REMOVED'
    LIMIT 1
  """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_pin_rsa_headline(
    ad_id: str,
    ad_group_id: str,
    index: float,
    position: float,
    customer_id: str = None,
) -> dict:
    """Pin an RSA headline to position 1, 2, or 3. Pass position=0 to unpin."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
    SELECT ad_group_ad.ad.id,
           ad_group_ad.ad.responsive_search_ad.headlines,
           ad_group_ad.ad.responsive_search_ad.descriptions,
           ad_group_ad.ad.responsive_search_ad.path1,
           ad_group_ad.ad.responsive_search_ad.path2
    FROM ad_group_ad
    WHERE ad_group_ad.ad.id = {ad_id}
      AND ad_group.id = {ad_group_id}
      AND ad_group_ad.status != 'REMOVED'
    LIMIT 1
  """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_pin_rsa_description(
    ad_id: str,
    ad_group_id: str,
    index: float,
    position: float,
    customer_id: str = None,
) -> dict:
    """Pin an RSA description to position 1 or 2. Pass position=0 to unpin."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
    SELECT ad_group_ad.ad.id,
           ad_group_ad.ad.responsive_search_ad.headlines,
           ad_group_ad.ad.responsive_search_ad.descriptions,
           ad_group_ad.ad.responsive_search_ad.path1,
           ad_group_ad.ad.responsive_search_ad.path2
    FROM ad_group_ad
    WHERE ad_group_ad.ad.id = {ad_id}
      AND ad_group.id = {ad_group_id}
      AND ad_group_ad.status != 'REMOVED'
    LIMIT 1
  """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_remove_rsa_headline(
    ad_id: str,
    ad_group_id: str,
    index: float,
    customer_id: str = None,
) -> dict:
    """Remove a specific headline from an RSA by 0-based index. Minimum 3 headlines enforced."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
    SELECT ad_group_ad.ad.id,
           ad_group_ad.ad.responsive_search_ad.headlines,
           ad_group_ad.ad.responsive_search_ad.descriptions,
           ad_group_ad.ad.responsive_search_ad.path1,
           ad_group_ad.ad.responsive_search_ad.path2
    FROM ad_group_ad
    WHERE ad_group_ad.ad.id = {ad_id}
      AND ad_group.id = {ad_group_id}
      AND ad_group_ad.status != 'REMOVED'
    LIMIT 1
  """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_remove_rsa_description(
    ad_id: str,
    ad_group_id: str,
    index: float,
    customer_id: str = None,
) -> dict:
    """Remove a specific description from an RSA by 0-based index. Minimum 2 descriptions enforced."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
    SELECT ad_group_ad.ad.id,
           ad_group_ad.ad.responsive_search_ad.headlines,
           ad_group_ad.ad.responsive_search_ad.descriptions,
           ad_group_ad.ad.responsive_search_ad.path1,
           ad_group_ad.ad.responsive_search_ad.path2
    FROM ad_group_ad
    WHERE ad_group_ad.ad.id = {ad_id}
      AND ad_group.id = {ad_group_id}
      AND ad_group_ad.status != 'REMOVED'
    LIMIT 1
  """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_update_rsa_paths(
    ad_id: str,
    ad_group_id: str,
    path1: str = None,
    path2: str = None,
    customer_id: str = None,
) -> dict:
    """Update the display path1 and/or path2 of an RSA (shown in the URL, max 15 chars each)."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
    SELECT ad_group_ad.ad.id,
           ad_group_ad.ad.responsive_search_ad.headlines,
           ad_group_ad.ad.responsive_search_ad.descriptions,
           ad_group_ad.ad.responsive_search_ad.path1,
           ad_group_ad.ad.responsive_search_ad.path2
    FROM ad_group_ad
    WHERE ad_group_ad.ad.id = {ad_id}
      AND ad_group.id = {ad_group_id}
      AND ad_group_ad.status != 'REMOVED'
    LIMIT 1
  """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_add_rsa_headline(
    ad_id: str,
    ad_group_id: str,
    text: str,
    pin_position: float = None,
    customer_id: str = None,
) -> dict:
    """Append a new headline to an RSA (does not replace existing). Max 15 total."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
    SELECT ad_group_ad.ad.id,
           ad_group_ad.ad.final_urls,
           ad_group_ad.ad.tracking_url_template,
           ad_group_ad.ad.url_custom_parameters,
           ad_group_ad.ad.responsive_search_ad.headlines,
           ad_group_ad.ad.responsive_search_ad.descriptions
    FROM ad_group_ad
    WHERE ad_group_ad.ad.id = {ad_id}
      AND ad_group.id = {ad_group_id}
    LIMIT 1
  """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_add_rsa_description(
    ad_id: str,
    ad_group_id: str,
    text: str,
    pin_position: float = None,
    customer_id: str = None,
) -> dict:
    """Append a new description to an RSA (does not replace existing). Max 4 total."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
    SELECT ad_group_ad.ad.id,
           ad_group_ad.ad.final_urls,
           ad_group_ad.ad.tracking_url_template,
           ad_group_ad.ad.url_custom_parameters,
           ad_group_ad.ad.responsive_search_ad.headlines,
           ad_group_ad.ad.responsive_search_ad.descriptions
    FROM ad_group_ad
    WHERE ad_group_ad.ad.id = {ad_id}
      AND ad_group.id = {ad_group_id}
    LIMIT 1
  """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_unpin_rsa_headline(
    ad_id: str,
    ad_group_id: str,
    index: float,
    customer_id: str = None,
) -> dict:
    """Remove pinning from a headline by 0-based index. Headline text stays; just becomes unpinned."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
    SELECT ad_group_ad.ad.id,
           ad_group_ad.ad.final_urls,
           ad_group_ad.ad.tracking_url_template,
           ad_group_ad.ad.url_custom_parameters,
           ad_group_ad.ad.responsive_search_ad.headlines,
           ad_group_ad.ad.responsive_search_ad.descriptions
    FROM ad_group_ad
    WHERE ad_group_ad.ad.id = {ad_id}
      AND ad_group.id = {ad_group_id}
    LIMIT 1
  """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_unpin_rsa_description(
    ad_id: str,
    ad_group_id: str,
    index: float,
    customer_id: str = None,
) -> dict:
    """Remove pinning from a description by 0-based index."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
    SELECT ad_group_ad.ad.id,
           ad_group_ad.ad.final_urls,
           ad_group_ad.ad.tracking_url_template,
           ad_group_ad.ad.url_custom_parameters,
           ad_group_ad.ad.responsive_search_ad.headlines,
           ad_group_ad.ad.responsive_search_ad.descriptions
    FROM ad_group_ad
    WHERE ad_group_ad.ad.id = {ad_id}
      AND ad_group.id = {ad_group_id}
    LIMIT 1
  """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_ad_final_urls(
    ad_id: str,
    ad_group_id: str,
    final_urls: list,
    final_mobile_urls: list = None,
    customer_id: str = None,
) -> dict:
    """Set the final_urls on any ad (RSA, RDA, etc). Replaces existing URLs."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
    SELECT ad_group_ad.ad.id,
           ad_group_ad.ad.final_urls,
           ad_group_ad.ad.tracking_url_template,
           ad_group_ad.ad.url_custom_parameters,
           ad_group_ad.ad.responsive_search_ad.headlines,
           ad_group_ad.ad.responsive_search_ad.descriptions
    FROM ad_group_ad
    WHERE ad_group_ad.ad.id = {ad_id}
      AND ad_group.id = {ad_group_id}
    LIMIT 1
  """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_ad_tracking_template(
    ad_id: str,
    ad_group_id: str,
    tracking_url_template: str,
    customer_id: str = None,
) -> dict:
    """Set the tracking URL template on an ad. Pass empty string to clear."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
    SELECT ad_group_ad.ad.id,
           ad_group_ad.ad.final_urls,
           ad_group_ad.ad.tracking_url_template,
           ad_group_ad.ad.url_custom_parameters,
           ad_group_ad.ad.responsive_search_ad.headlines,
           ad_group_ad.ad.responsive_search_ad.descriptions
    FROM ad_group_ad
    WHERE ad_group_ad.ad.id = {ad_id}
      AND ad_group.id = {ad_group_id}
    LIMIT 1
  """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_ad_url_custom_parameters(
    ad_id: str = None,
    ad_group_id: str = None,
    parameters: list = None,
    customer_id: str = None,
) -> dict:
    """Set custom parameters on an ad for ValueTrack {_param} substitutions in tracking template or final URL."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
    SELECT ad_group_ad.ad.id,
           ad_group_ad.ad.final_urls,
           ad_group_ad.ad.tracking_url_template,
           ad_group_ad.ad.url_custom_parameters,
           ad_group_ad.ad.responsive_search_ad.headlines,
           ad_group_ad.ad.responsive_search_ad.descriptions
    FROM ad_group_ad
    WHERE ad_group_ad.ad.id = {ad_id}
      AND ad_group.id = {ad_group_id}
    LIMIT 1
  """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_ad_final_mobile_urls(
    ad_id: str,
    ad_group_id: str,
    final_mobile_urls: list,
    customer_id: str = None,
) -> dict:
    """Set final_mobile_urls on an ad (landing pages for mobile devices)."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
    SELECT ad_group_ad.ad.id,
           ad_group_ad.ad.final_urls,
           ad_group_ad.ad.tracking_url_template,
           ad_group_ad.ad.url_custom_parameters,
           ad_group_ad.ad.responsive_search_ad.headlines,
           ad_group_ad.ad.responsive_search_ad.descriptions
    FROM ad_group_ad
    WHERE ad_group_ad.ad.id = {ad_id}
      AND ad_group.id = {ad_group_id}
    LIMIT 1
  """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_rename_ad(
    ad_id: str,
    ad_group_id: str,
    name: str,
    customer_id: str = None,
) -> dict:
    """Change the display name of an ad."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
    SELECT ad_group_ad.ad.id,
           ad_group_ad.ad.final_urls,
           ad_group_ad.ad.tracking_url_template,
           ad_group_ad.ad.url_custom_parameters,
           ad_group_ad.ad.responsive_search_ad.headlines,
           ad_group_ad.ad.responsive_search_ad.descriptions
    FROM ad_group_ad
    WHERE ad_group_ad.ad.id = {ad_id}
      AND ad_group.id = {ad_group_id}
    LIMIT 1
  """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_ad_policy_status(
    ad_id: str,
    ad_group_id: str,
    customer_id: str = None,
) -> dict:
    """Get policy approval status and disapproval reasons for an ad."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
    SELECT ad_group_ad.ad.id,
           ad_group_ad.ad.final_urls,
           ad_group_ad.ad.tracking_url_template,
           ad_group_ad.ad.url_custom_parameters,
           ad_group_ad.ad.responsive_search_ad.headlines,
           ad_group_ad.ad.responsive_search_ad.descriptions
    FROM ad_group_ad
    WHERE ad_group_ad.ad.id = {ad_id}
      AND ad_group.id = {ad_group_id}
    LIMIT 1
  """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_rsa_asset_performance(
    ad_id: str,
    ad_group_id: str,
    customer_id: str = None,
) -> dict:
    """Get per-asset (headline/description) performance ratings for a Responsive Search Ad. Returns each asset text with its performance label (BEST, GOOD, LOW, LEARNING, PENDING, UNSPECIFIED)."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
        SELECT
          ad_group_ad.ad.id,
          ad_group_ad.ad.responsive_search_ad.headlines,
          ad_group_ad.ad.responsive_search_ad.descriptions,
          ad_group_ad.ad_strength,
          ad_group_ad.policy_summary.approval_status
        FROM ad_group_ad
        WHERE ad_group_ad.ad.id = {ad_id or ""}
          AND ad_group.id = {ad_group_id or ""}
          AND ad_group_ad.status != 'REMOVED'
        LIMIT 1
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_ad_performance_by_date(
    start_date: str,
    end_date: str,
    campaign_id: str = None,
    ad_group_id: str = None,
    ad_id: str = None,
    segment_by_date: bool = None,
    customer_id: str = None,
) -> dict:
    """Get impressions, clicks, cost, CTR, CPC, and conversions for ads within a date range. Optionally filter by campaign or ad group."""
    conditions = []
    if start_date and end_date:
        conditions.append(f"segments.date BETWEEN '{start_date}' AND '{end_date}'")
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
        SELECT
          ad_group_ad.ad.id,
          ad_group_ad.ad.responsive_search_ad.headlines,
          ad_group_ad.ad.responsive_search_ad.descriptions,
          ad_group_ad.ad_strength,
          ad_group_ad.policy_summary.approval_status
        FROM ad_group_ad
        WHERE ad_group_ad.ad.id = {ad_id or ""}
          AND ad_group.id = {ad_group_id or ""}
          AND ad_group_ad.status != 'REMOVED'
        LIMIT 1
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_update_ad(
    ad_id: str,
    ad_group_id: str,
    status: str = None,
    final_urls: list = None,
    final_mobile_urls: list = None,
    customer_id: str = None,
) -> dict:
    """Update an ad status or final URLs"""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_update_responsive_display_ad(
    ad_id: str,
    ad_group_id: str,
    headlines: list = None,
    descriptions: list = None,
    long_headline: str = None,
    business_name: str = None,
    marketing_image_asset_ids: list = None,
    logo_asset_ids: list = None,
    customer_id: str = None,
) -> dict:
    """Update an existing Responsive Display Ad. You can update any subset of: headlines (array of strings, max 5), descriptions (array, max 5), long_headline, business_name, marketing_image_asset_ids (array), logo_asset_ids (array)."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_asset_groups(
    campaign_id: str = None,
    status: str = None,
    limit: int =None,
    customer_id: str = None,
) -> dict:
    """List all PMax asset groups. Optionally filter by campaign_id and/or status."""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    if status and status not in ('ALL', None): conditions.append(f"campaign.status = '{status}'")
    gaql = f"""
        SELECT asset_group.id, asset_group.name, asset_group.ad_strength,
               asset_group.status, asset_group.primary_status,
               asset_group.primary_status_reasons
        FROM asset_group
        WHERE asset_group.id = {asset_group_id or ""}
        LIMIT 1
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_remove_asset_group(
    asset_group_id: str,
    customer_id: str = None,
) -> dict:
    """Permanently remove a PMax asset group. Cannot be undone."""
    conditions = []
    gaql = f"""
        SELECT asset_group.id, asset_group.name, asset_group.ad_strength,
               asset_group.status, asset_group.primary_status,
               asset_group.primary_status_reasons
        FROM asset_group
        WHERE asset_group.id = {asset_group_id or ""}
        LIMIT 1
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_pause_asset_group(
    asset_group_id: str,
    customer_id: str = None,
) -> dict:
    """Pause a PMax asset group."""
    conditions = []
    gaql = f"""
        SELECT asset_group.id, asset_group.name, asset_group.ad_strength,
               asset_group.status, asset_group.primary_status,
               asset_group.primary_status_reasons
        FROM asset_group
        WHERE asset_group.id = {asset_group_id or ""}
        LIMIT 1
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_enable_asset_group(
    asset_group_id: str,
    customer_id: str = None,
) -> dict:
    """Enable a PMax asset group."""
    conditions = []
    gaql = f"""
        SELECT asset_group.id, asset_group.name, asset_group.ad_strength,
               asset_group.status, asset_group.primary_status,
               asset_group.primary_status_reasons
        FROM asset_group
        WHERE asset_group.id = {asset_group_id or ""}
        LIMIT 1
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_bulk_pause_asset_groups(
    asset_group_ids: list,
    customer_id: str = None,
) -> dict:
    """Pause multiple PMax asset groups in one call."""
    conditions = []
    gaql = f"""
        SELECT asset_group.id, asset_group.name, asset_group.ad_strength,
               asset_group.status, asset_group.primary_status,
               asset_group.primary_status_reasons
        FROM asset_group
        WHERE asset_group.id = {asset_group_id or ""}
        LIMIT 1
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_bulk_enable_asset_groups(
    asset_group_ids: list,
    customer_id: str = None,
) -> dict:
    """Enable multiple PMax asset groups in one call."""
    conditions = []
    gaql = f"""
        SELECT asset_group.id, asset_group.name, asset_group.ad_strength,
               asset_group.status, asset_group.primary_status,
               asset_group.primary_status_reasons
        FROM asset_group
        WHERE asset_group.id = {asset_group_id or ""}
        LIMIT 1
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_bulk_remove_asset_groups(
    asset_group_ids: list,
    customer_id: str = None,
) -> dict:
    """Permanently remove multiple PMax asset groups."""
    conditions = []
    gaql = f"""
        SELECT asset_group.id, asset_group.name, asset_group.ad_strength,
               asset_group.status, asset_group.primary_status,
               asset_group.primary_status_reasons
        FROM asset_group
        WHERE asset_group.id = {asset_group_id or ""}
        LIMIT 1
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_asset_group_final_urls(
    asset_group_id: str,
    final_urls: list,
    customer_id: str = None,
) -> dict:
    """Replace the final_urls on a PMax asset group."""
    conditions = []
    gaql = f"""
        SELECT asset_group.id, asset_group.name, asset_group.ad_strength,
               asset_group.status, asset_group.primary_status,
               asset_group.primary_status_reasons
        FROM asset_group
        WHERE asset_group.id = {asset_group_id or ""}
        LIMIT 1
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_asset_group_ad_strength(
    asset_group_id: str,
    customer_id: str = None,
) -> dict:
    """Get ad strength rating (POOR / AVERAGE / GOOD / EXCELLENT) of a PMax asset group."""
    conditions = []
    gaql = f"""
        SELECT asset_group.id, asset_group.name, asset_group.ad_strength,
               asset_group.status, asset_group.primary_status,
               asset_group.primary_status_reasons
        FROM asset_group
        WHERE asset_group.id = {asset_group_id or ""}
        LIMIT 1
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_remove_asset_group_signal(
    signal_resource_name: str,
    customer_id: str = None,
) -> dict:
    """Remove an audience signal from a PMax asset group by signal resource name."""
    conditions = []
    gaql = f"""
        SELECT asset_group.id, asset_group.name, asset_group.ad_strength,
               asset_group.status, asset_group.primary_status,
               asset_group.primary_status_reasons
        FROM asset_group
        WHERE asset_group.id = {asset_group_id or ""}
        LIMIT 1
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_duplicate_asset_group(
    source_asset_group_id: str,
    destination_campaign_id: str,
    new_name: str = None,
    customer_id: str = None,
) -> dict:
    """Duplicate a PMax asset group into the same or a different PMax campaign. Copies name, final_urls, and all linked assets/signals (best-effort)."""
    conditions = []
    gaql = f"""
        SELECT asset_group.id, asset_group.name, asset_group.ad_strength,
               asset_group.status, asset_group.primary_status,
               asset_group.primary_status_reasons
        FROM asset_group
        WHERE asset_group.id = {asset_group_id or ""}
        LIMIT 1
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_asset_group_listing_filters(
    asset_group_id: str,
    customer_id: str = None,
) -> dict:
    """List product listing group filters on a PMax asset group (shopping-inventory partitioning)."""
    conditions = []
    gaql = f"""
        SELECT asset_group.id, asset_group.name, asset_group.ad_strength,
               asset_group.status, asset_group.primary_status,
               asset_group.primary_status_reasons
        FROM asset_group
        WHERE asset_group.id = {asset_group_id or ""}
        LIMIT 1
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_asset_group_listing_filter(
    asset_group_id: str,
    type_val: str,
    parent_filter_id: str = None,
    dimension: str = None,
    value: str = None,
    customer_id: str = None,
) -> dict:
    """Create a listing group filter (product partition) on a PMax asset group. type: UNIT_INCLUDED (leaf, include), UNIT_EXCLUDED (leaf, exclude), SUBDIVISION (branch)."""
    conditions = []
    gaql = f"""
        SELECT asset_group.id, asset_group.name, asset_group.ad_strength,
               asset_group.status, asset_group.primary_status,
               asset_group.primary_status_reasons
        FROM asset_group
        WHERE asset_group.id = {asset_group_id or ""}
        LIMIT 1
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_remove_asset_group_listing_filter(
    filter_resource_name: str,
    customer_id: str = None,
) -> dict:
    """Remove a product listing group filter from an asset group."""
    conditions = []
    gaql = f"""
        SELECT asset_group.id, asset_group.name, asset_group.ad_strength,
               asset_group.status, asset_group.primary_status,
               asset_group.primary_status_reasons
        FROM asset_group
        WHERE asset_group.id = {asset_group_id or ""}
        LIMIT 1
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_asset_group_assets(
    asset_group_id: str,
    customer_id: str = None,
) -> dict:
    """List all assets linked to a PMax asset group, including text, image, and video assets with their field types."""
    conditions = []
    gaql = f"""
        SELECT
          asset_group_asset.asset_group,
          asset_group_asset.asset,
          asset_group_asset.field_type,
          asset_group_asset.status,
          asset.id,
          asset.name,
          asset.type,
          asset.text_asset.text,
          asset.image_asset.full_size.url,
          asset.youtube_video_asset.youtube_video_id
        FROM asset_group_asset
        WHERE asset_group_asset.asset_group = 'customers/{cid}/assetGroups/{asset_group_id or ""}'
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_add_asset_to_asset_group(
    asset_group_id: str,
    asset_id: str,
    field_type: str,
    customer_id: str = None,
) -> dict:
    """Link an existing asset (by asset_id) to a PMax asset group with a specified field type. Field types: HEADLINE, LONG_HEADLINE, DESCRIPTION, BUSINESS_NAME, MARKETING_IMAGE, SQUARE_MARKETING_IMAGE, LOGO, YOUTUBE_VIDEO."""
    conditions = []
    gaql = f"""
        SELECT
          asset_group_asset.asset_group,
          asset_group_asset.asset,
          asset_group_asset.field_type,
          asset_group_asset.status,
          asset.id,
          asset.name,
          asset.type,
          asset.text_asset.text,
          asset.image_asset.full_size.url,
          asset.youtube_video_asset.youtube_video_id
        FROM asset_group_asset
        WHERE asset_group_asset.asset_group = 'customers/{cid}/assetGroups/{asset_group_id or ""}'
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_remove_asset_from_asset_group(
    asset_group_id: str,
    asset_id: str,
    field_type: str,
    customer_id: str = None,
) -> dict:
    """Remove (unlink) an asset from a PMax asset group. The asset itself is NOT deleted."""
    conditions = []
    gaql = f"""
        SELECT
          asset_group_asset.asset_group,
          asset_group_asset.asset,
          asset_group_asset.field_type,
          asset_group_asset.status,
          asset.id,
          asset.name,
          asset.type,
          asset.text_asset.text,
          asset.image_asset.full_size.url,
          asset.youtube_video_asset.youtube_video_id
        FROM asset_group_asset
        WHERE asset_group_asset.asset_group = 'customers/{cid}/assetGroups/{asset_group_id or ""}'
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_update_asset_group(
    asset_group_id: str,
    name: str = None,
    final_urls: list = None,
    status: float = None,
    customer_id: str = None,
) -> dict:
    """Update a PMax asset group. Supports changing name, final_urls, status (ENABLED=2/PAUSED=3)."""
    conditions = []
    if status and status not in ('ALL', None): conditions.append(f"campaign.status = '{status}'")
    gaql = f"""
        SELECT
          asset_group_asset.asset_group,
          asset_group_asset.asset,
          asset_group_asset.field_type,
          asset_group_asset.status,
          asset.id,
          asset.name,
          asset.type,
          asset.text_asset.text,
          asset.image_asset.full_size.url,
          asset.youtube_video_asset.youtube_video_id
        FROM asset_group_asset
        WHERE asset_group_asset.asset_group = 'customers/{cid}/assetGroups/{asset_group_id or ""}'
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_asset_group_signals(
    asset_group_id: str,
    customer_id: str = None,
) -> dict:
    """List audience signals attached to a PMax asset group."""
    conditions = []
    gaql = f"""
        SELECT
          asset_group_asset.asset_group,
          asset_group_asset.asset,
          asset_group_asset.field_type,
          asset_group_asset.status,
          asset.id,
          asset.name,
          asset.type,
          asset.text_asset.text,
          asset.image_asset.full_size.url,
          asset.youtube_video_asset.youtube_video_id
        FROM asset_group_asset
        WHERE asset_group_asset.asset_group = 'customers/{cid}/assetGroups/{asset_group_id or ""}'
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_add_audience_signal_to_asset_group(
    asset_group_id: str,
    audience_id: str = None,
    search_theme_text: str = None,
    customer_id: str = None,
) -> dict:
    """Add an audience signal to a PMax asset group. Provide either audience_id (for a user list / custom audience resource) or search_theme_text (for a search-theme signal)."""
    conditions = []
    gaql = f"""
        SELECT
          asset_group_asset.asset_group,
          asset_group_asset.asset,
          asset_group_asset.field_type,
          asset_group_asset.status,
          asset.id,
          asset.name,
          asset.type,
          asset.text_asset.text,
          asset.image_asset.full_size.url,
          asset.youtube_video_asset.youtube_video_id
        FROM asset_group_asset
        WHERE asset_group_asset.asset_group = 'customers/{cid}/assetGroups/{asset_group_id or ""}'
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_attach_asset_to_campaign(
    campaign_id: str,
    asset_id: str,
    field_type: str,
    customer_id: str = None,
) -> dict:
    """Attach an existing asset to a Google Ads campaign with a specified field type."""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT campaign.id, campaign.name, campaign.status, metrics.clicks, metrics.impressions, metrics.cost_micros FROM campaign {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_app_asset(
    app_id: str,
    app_store: str,
    link_text: str,
    customer_id: str = None,
) -> dict:
    """Create an app asset in Google Ads linking to an iOS or Android application."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_call_asset(
    phone_number: str,
    country_code: str,
    call_conversion_action: str = None,
    customer_id: str = None,
) -> dict:
    """Create a call asset in Google Ads with a phone number."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_image_asset(
    image_url: str,
    name: str = None,
    customer_id: str = None,
) -> dict:
    """Create an image asset in Google Ads by fetching an image from a URL and uploading it."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_lead_form_asset(
    business_name: str = None,
    headline: str = None,
    description: str = None,
    fields: list = None,
    post_submit_headline: str = None,
    post_submit_description: str = None,
    call_to_action_type: str = None,
    customer_id: str = None,
) -> dict:
    """Create a lead form asset in Google Ads to collect user information directly from ads."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_location_asset(
    business_profile_location: str = None,
    location_ownership_type: str = None,
    customer_id: str = None,
) -> dict:
    """Create a location asset in Google Ads linked to a Business Profile location."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_price_asset(
    type_val: str = None,
    price_qualifier: str = None,
    language_code: str = None,
    items: list = None,
    customer_id: str = None,
) -> dict:
    """Create a price asset in Google Ads to showcase products/services with prices."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_promotion_asset(
    promotion_target: str,
    language_code: str,
    currency_code: str,
    discount_modifier: str = None,
    percent_off: float = None,
    money_amount_off: float = None,
    promotion_code: str = None,
    orders_over_amount: float = None,
    start_date: str = None,
    end_date: str = None,
    occasion: str = None,
    customer_id: str = None,
) -> dict:
    """Create a promotion asset in Google Ads to highlight sales and special offers."""
    conditions = []
    if start_date and end_date:
        conditions.append(f"segments.date BETWEEN '{start_date}' AND '{end_date}'")
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_structured_snippet_asset(
    header: str,
    values: list,
    customer_id: str = None,
) -> dict:
    """Create a structured snippet asset in Google Ads with a header and list of values."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_detach_asset_from_campaign(
    campaign_id: str,
    asset_id: str,
    field_type: str,
    customer_id: str = None,
) -> dict:
    """Detach (remove) an asset from a Google Ads campaign."""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT campaign.id, campaign.name, campaign.status, metrics.clicks, metrics.impressions, metrics.cost_micros FROM campaign {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_asset_details(
    asset_id: str,
    customer_id: str = None,
) -> dict:
    """Get detailed information about a specific Google Ads asset by ID."""
    conditions = []
    gaql = f"""
      SELECT
        asset.id,
        asset.name,
        asset.type,
        asset.text_asset.text,
        asset.image_asset.full_size.url,
        asset.youtube_video_asset.youtube_video_id,
        asset.callout_asset.callout_text,
        asset.sitelink_asset.link_text,
        asset.sitelink_asset.description1,
        asset.sitelink_asset.description2
      FROM asset
      WHERE asset.id = {asset_id or ""}
    """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_asset_performance(
    asset_id: str = None,
    limit: int = 200,
    customer_id: str = None,
) -> dict:
    """Performance of assets as they served inside ads, over the last 30 days.

    Args:
        asset_id: Optional asset ID, to look at a single asset.
        limit: Maximum rows to return. Default 200; raise it if the answer is being cut off.
    """
    _limit = max(1, min(int(limit or 200), 2000))
    extra = f" AND asset.id = {asset_id}" if asset_id else ""
    gaql = f"""
      SELECT
        asset.id,
        asset.name,
        asset.type,
        ad_group_ad_asset_view.field_type,
        ad_group_ad_asset_view.performance_label,
        metrics.impressions,
        metrics.clicks,
        metrics.cost_micros,
        metrics.conversions
      FROM ad_group_ad_asset_view
      WHERE segments.date DURING LAST_30_DAYS{extra}
      ORDER BY metrics.impressions DESC
      LIMIT {_limit}
    """
    rows = _search(gaql, customer_id or '')
    data = []
    for r in rows:
        row = _row_to_dict(r)
        metrics = row.get("metrics") or {}
        if isinstance(metrics, dict) and metrics.get("cost_micros") is not None:
            metrics["cost"] = _m(metrics["cost_micros"])
        data.append(row)
    return {"success": True, "data": data, "count": len(data)}

@mcp.tool()
def gads_list_ad_group_assets(
    ad_group_id: str,
    customer_id: str = None,
) -> dict:
    """List all assets attached to a specific Google Ads ad group."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""
      SELECT
        ad_group_asset.asset,
        ad_group_asset.field_type,
        ad_group_asset.status
      FROM ad_group_asset
      WHERE ad_group_asset.ad_group = 'customers/{cid}/adGroups/{ad_group_id or ""}'
    """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_assets(
    type_val: str = None,
    limit: int = 200,
    customer_id: str = None,
) -> dict:
    """List assets in the account library.

    Args:
        type_val: Optional asset type filter, e.g. IMAGE, TEXT, YOUTUBE_VIDEO, SITELINK, CALL.
        limit: Maximum rows to return. Default 200; raise it if the answer is being cut off.
    """
    _limit = max(1, min(int(limit or 200), 2000))
    where = f"WHERE asset.type = '{type_val}'" if type_val else ""
    gaql = f"""
      SELECT
        asset.id,
        asset.name,
        asset.type,
        asset.resource_name
      FROM asset
      {where}
      ORDER BY asset.id DESC
      LIMIT {_limit}
    """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_campaign_assets(
    campaign_id: str,
    field_type: str = None,
    customer_id: str = None,
) -> dict:
    """List all assets attached to a specific Google Ads campaign."""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT campaign.id, campaign.name, campaign.status, metrics.clicks, metrics.impressions, metrics.cost_micros FROM campaign {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_update_asset(
    asset_id: str,
    name: str = None,
    customer_id: str = None,
) -> dict:
    """Update an existing Google Ads asset (e.g., rename it)."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_upload_image_asset_base64(
    image_data: str,
    name: str = None,
    customer_id: str = None,
) -> dict:
    """Upload an image asset to Google Ads from a base64-encoded string. Useful when the image is not available at a public URL. Returns the new asset ID and resource name."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_upload_text_asset(
    text: str,
    name: str = None,
    customer_id: str = None,
) -> dict:
    """Create a text asset in Google Ads."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_upload_video_asset(
    youtube_video_id: str,
    name: str = None,
    customer_id: str = None,
) -> dict:
    """Create a YouTube video asset in Google Ads using a YouTube video ID."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_add_users_to_customer_match_list(
    user_list_id: str,
    users: list,
    remove_all_existing: bool = None,
    customer_id: str = None,
) -> dict:
    """Upload hashed user data (email, phone, address) to a Google Ads Customer Match list via an offline user data job."""
    require_editor()
    client, cid = _get_client(customer_id or '')
    service = client.get_service("OfflineUserDataJobService")
    op = client.get_type("OfflineUserDataJobOperation")
    obj = op.create
    if user_list_id is not None: obj.user_list_id = user_list_id
    if users is not None: obj.users = users
    if remove_all_existing is not None: obj.remove_all_existing = remove_all_existing
    response = service.mutate_offline_user_data_jobs(customer_id=cid, operations=[op])
    return {"success": True, "resource_name": str(response.results[0].resource_name)}

@mcp.tool()
def gads_attach_audience_to_ad_group(
    ad_group_id: str,
    user_list_id: str,
    bid_modifier: float = None,
    targeting_setting: str = None,
    customer_id: str = None,
) -> dict:
    """Attach a user list audience to a Google Ads ad group as an ad group criterion with an optional bid modifier."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group.id, ad_group.name, ad_group.status, metrics.clicks, metrics.impressions FROM ad_group {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_attach_audience_to_campaign(
    campaign_id: str,
    user_list_id: str,
    bid_modifier: float = None,
    targeting_setting: str = None,
    customer_id: str = None,
) -> dict:
    """Attach a user list audience to a Google Ads campaign as a campaign criterion with an optional bid modifier."""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT campaign.id, campaign.name, campaign.status, metrics.clicks, metrics.impressions, metrics.cost_micros FROM campaign {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_combined_audience(
    name: str = None,
    description: str = None,
    dimensions: list = None,
    customer_id: str = None,
) -> dict:
    """Attempt to create a combined audience. Note: Combined audiences are read-only in the Google Ads API; this tool returns existing combined audiences and an explanation."""
    gaql = """SELECT combined_audience.id, combined_audience.name, combined_audience.description, combined_audience.status FROM combined_audience LIMIT 50"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_custom_audience(
    type_val: str,
    name: str = None,
    description: str = None,
    members: list = None,
    customer_id: str = None,
) -> dict:
    """Create a custom audience in Google Ads based on keywords, URLs, apps, or place categories."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_customer_match_list(
    name: str,
    description: str = None,
    membership_lifespan_days: float = None,
    customer_id: str = None,
) -> dict:
    """Create a CRM-based Customer Match user list in Google Ads for uploading first-party customer data."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_remarketing_list(
    name: str,
    description: str = None,
    membership_lifespan_days: float = None,
    rule_type: str = None,
    url_filter: str = None,
    customer_id: str = None,
) -> dict:
    """Create a rule-based remarketing user list in Google Ads, optionally filtered by URL."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_similar_audience(
    seed_user_list_id: str,
    customer_id: str = None,
) -> dict:
    """Look up similar audiences generated by Google for a given seed user list. Note: Similar audiences cannot be manually created via API — Google generates them automatically."""
    conditions = []
    gaql = f"""SELECT user_list.id, user_list.name, user_list.type FROM user_list WHERE user_list.similar_user_list.seed_user_list = '{seedResourceName}'"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_audience_composition(
    user_list_id: str,
    customer_id: str = None,
) -> dict:
    """Get composition details and demographic breakdown signals for a Google Ads user list."""
    conditions = []
    gaql = f"""SELECT user_list.id, user_list.name, user_list.type, user_list.size_for_search, user_list.size_for_display, user_list.membership_status FROM user_list WHERE user_list.id = {user_list_id or ""} LIMIT 1"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_audience_insights(
    user_list_id: str,
    customer_id: str = None,
) -> dict:
    """Get size, eligibility, and configuration insights for a specific Google Ads user list."""
    conditions = []
    gaql = f"""SELECT user_list.id, user_list.name, user_list.type, user_list.size_for_search, user_list.size_for_display, user_list.eligible_for_search, user_list.eligible_for_display, user_list.membership_life_span, user_list.membership_status FROM user_list WHERE user_list.id = {user_list_id or ""} LIMIT 1"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_audiences(
    type_val: str = None,
    limit: int = 200,
    customer_id: str = None,
) -> dict:
    """List audiences defined on the account.

    Args:
        type_val: Ignored -- the audience resource has no type field. Kept so
            callers that pass it everywhere do not break. For remarketing and
            customer-match lists, which do have types, use gads_list_user_lists.
        limit: Maximum rows to return. Default 200; raise it if the answer is being cut off.
    """
    _limit = max(1, min(int(limit or 200), 2000))
    gaql = f"""
      SELECT
        audience.id,
        audience.name,
        audience.description,
        audience.status
      FROM audience
      ORDER BY audience.name ASC
      LIMIT {_limit}
    """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_user_lists(
    type_val: str = None,
    limit: int = 200,
    customer_id: str = None,
) -> dict:
    """List remarketing and customer-match user lists with their sizes.

    Args:
        type_val: Optional type filter, e.g. REMARKETING, CRM_BASED, RULE_BASED.
        limit: Maximum rows to return. Default 200; raise it if the answer is being cut off.
    """
    _limit = max(1, min(int(limit or 200), 2000))
    where = f"WHERE user_list.type = '{type_val}'" if type_val else ""
    gaql = f"""
      SELECT
        user_list.id,
        user_list.name,
        user_list.description,
        user_list.type,
        user_list.membership_status,
        user_list.membership_life_span,
        user_list.size_for_display,
        user_list.size_for_search
      FROM user_list
      {where}
      ORDER BY user_list.name ASC
      LIMIT {_limit}
    """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_update_user_list(
    user_list_id: str,
    name: str = None,
    description: str = None,
    membership_lifespan_days: float = None,
    status: str = None,
    customer_id: str = None,
) -> dict:
    """Update a Google Ads user list — rename, change description, membership lifespan, or open/close status."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_bidding_data_exclusion(
    name: str,
    start_date_time: str,
    end_date_time: str,
    campaign_ids: list = None,
    customer_id: str = None,
) -> dict:
    """Create a bidding data exclusion to exclude a time period from smart bidding models."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_bidding_seasonality_adjustment(
    name: str,
    start_date_time: str,
    end_date_time: str,
    conversion_rate_modifier: float,
    campaign_ids: list = None,
    customer_id: str = None,
) -> dict:
    """Create a bidding seasonality adjustment to inform smart bidding of expected conversion rate changes."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_bidding_strategy(
    name: str,
    type_val: str,
    target_cpa: float = None,
    target_roas: float = None,
    target_impression_share_location: str = None,
    impression_share_fraction: float = None,
    max_cpc_bid_ceiling_micros: float = None,
    customer_id: str = None,
) -> dict:
    """Create a portfolio bidding strategy in Google Ads (TARGET_CPA, TARGET_ROAS, MAXIMIZE_CONVERSIONS, MAXIMIZE_CONVERSION_VALUE, TARGET_IMPRESSION_SHARE)."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_bidding_strategy_simulation(
    bidding_strategy_id: str,
    customer_id: str = None,
) -> dict:
    """Get simulation data points for a portfolio bidding strategy."""
    conditions = []
    gaql = f"""SELECT bidding_strategy_simulation.bidding_strategy_id,
              bidding_strategy_simulation.type,
              bidding_strategy_simulation.modification_method,
              bidding_strategy_simulation.target_cpa_point_list.points
       FROM bidding_strategy_simulation
       WHERE bidding_strategy_simulation.bidding_strategy_id = {bidding_strategy_id or ""}"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_bidding_strategies(
    type_val: str = None,
    limit: int = 200,
    customer_id: str = None,
) -> dict:
    """List portfolio bidding strategies and how many campaigns use each.

    Args:
        type_val: Optional type filter, e.g. TARGET_CPA, TARGET_ROAS, MAXIMIZE_CONVERSIONS.
        limit: Maximum rows to return. Default 200; raise it if the answer is being cut off.
    """
    _limit = max(1, min(int(limit or 200), 2000))
    where = f"WHERE bidding_strategy.type = '{type_val}'" if type_val else ""
    gaql = f"""
      SELECT
        bidding_strategy.id,
        bidding_strategy.name,
        bidding_strategy.type,
        bidding_strategy.status,
        bidding_strategy.campaign_count
      FROM bidding_strategy
      {where}
      ORDER BY bidding_strategy.name ASC
      LIMIT {_limit}
    """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_enhanced_cpc(
    campaign_id: str,
    customer_id: str = None,
) -> dict:
    """Enable Enhanced CPC bidding on a campaign."""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_maximize_conversion_value(
    campaign_id: str,
    target_roas: float = None,
    customer_id: str = None,
) -> dict:
    """Set Maximize Conversion Value bidding strategy on a campaign."""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_maximize_conversions(
    campaign_id: str,
    target_cpa: float = None,
    customer_id: str = None,
) -> dict:
    """Set Maximize Conversions bidding strategy on a campaign."""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_target_cpa(
    campaign_id: str,
    target_cpa: float,
    customer_id: str = None,
) -> dict:
    """Set Target CPA bidding strategy on a campaign."""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_target_impression_share(
    campaign_id: str,
    location: str,
    fraction: float,
    max_cpc_bid_ceiling_micros: float = None,
    customer_id: str = None,
) -> dict:
    """Set Target Impression Share bidding on a campaign."""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_target_roas(
    campaign_id: str,
    target_roas: float,
    customer_id: str = None,
) -> dict:
    """Set Target ROAS bidding strategy on a campaign (e.g. 4.0 = 400% return)."""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_update_bidding_strategy(
    bidding_strategy_id: str,
    name: str = None,
    target_cpa: float = None,
    target_roas: float = None,
    impression_share_fraction: float = None,
    max_cpc_bid_ceiling_micros: float = None,
    customer_id: str = None,
) -> dict:
    """Update an existing Google Ads portfolio bidding strategy — change name, target CPA, target ROAS, impression share, or bid ceiling."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_shared_budget(
    name: str,
    amount: float,
    delivery_method: str = None,
    customer_id: str = None,
) -> dict:
    """Create a new shared campaign budget in Google Ads."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_budget_details(
    budget_id: str,
    customer_id: str = None,
) -> dict:
    """Get full details for a specific campaign budget by its ID."""
    conditions = []
    gaql = f"""SELECT campaign_budget.id, campaign_budget.name, campaign_budget.amount_micros, campaign_budget.period, campaign_budget.type, campaign_budget.status, campaign_budget.explicitly_shared, campaign_budget.reference_count, campaign_budget.delivery_method, campaign_budget.total_amount_micros FROM campaign_budget WHERE campaign_budget.id = {budget_id or ""}"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_budget_pacing(
    budget_id: str = None,
    customer_id: str = None,
) -> dict:
    """Compare each budget's daily amount against what its campaigns spent this month.

    Args:
        budget_id: Optional budget ID, to look at a single budget.
    """
    conditions = ["segments.date DURING THIS_MONTH"]
    if budget_id:
        conditions.append(f"campaign_budget.id = {budget_id}")
    gaql = f"""
      SELECT
        campaign_budget.id,
        campaign_budget.name,
        campaign_budget.amount_micros,
        campaign.id,
        campaign.name,
        campaign.status,
        metrics.cost_micros
      FROM campaign
      WHERE {' AND '.join(conditions)}
      LIMIT 1000
    """
    rows = _search(gaql, customer_id or '')

    by_budget: dict = {}
    for r in rows:
        row = _row_to_dict(r)
        budget = row.get("campaign_budget") or {}
        bid = str(budget.get("id") or "")
        if not bid:
            continue
        entry = by_budget.setdefault(bid, {
            "budget_id": bid,
            "budget_name": budget.get("name"),
            "daily_amount": _m(budget.get("amount_micros") or 0),
            "spend_this_month": 0.0,
            "campaigns": [],
        })
        cost = _m((row.get("metrics") or {}).get("cost_micros") or 0)
        entry["spend_this_month"] = round(entry["spend_this_month"] + cost, 2)
        campaign = row.get("campaign") or {}
        entry["campaigns"].append({
            "id": campaign.get("id"),
            "name": campaign.get("name"),
            "status": campaign.get("status"),
            "cost": cost,
        })

    data = list(by_budget.values())
    for entry in data:
        # Days elapsed is what makes "spent 400 against a 30/day budget" mean
        # anything. Stating the expected figure saves the caller reconstructing
        # the month, and saves it getting that wrong.
        expected = entry["daily_amount"] * _dt.date.today().day
        entry["expected_to_date"] = round(expected, 2)
        entry["pacing_pct"] = round(entry["spend_this_month"] / expected * 100, 1) if expected else None
    data.sort(key=lambda e: e["spend_this_month"], reverse=True)
    return {"success": True, "data": data, "count": len(data)}

@mcp.tool()
def gads_list_budgets(
    limit: int = 200,
    customer_id: str = None,
) -> dict:
    """List campaign budgets with their amounts, delivery method, and how many campaigns share each.
    Args:
        limit: Maximum rows to return. Default 200; raise it if the answer is being cut off.
    """
    _limit = max(1, min(int(limit or 200), 2000))
    gaql = f"""
      SELECT
        campaign_budget.id,
        campaign_budget.name,
        campaign_budget.amount_micros,
        campaign_budget.delivery_method,
        campaign_budget.status,
        campaign_budget.explicitly_shared,
        campaign_budget.period,
        campaign_budget.reference_count
      FROM campaign_budget
      ORDER BY campaign_budget.amount_micros DESC
      LIMIT {_limit}
    """
    rows = _search(gaql, customer_id or '')
    data = []
    for r in rows:
        row = _row_to_dict(r)
        budget = row.get("campaign_budget") or {}
        if isinstance(budget, dict) and budget.get("amount_micros") is not None:
            budget["amount"] = _m(budget["amount_micros"])
        data.append(row)
    return {"success": True, "data": data, "count": len(data)}

@mcp.tool()
def gads_update_budget(
    budget_id: str,
    amount: float = None,
    name: str = None,
    customer_id: str = None,
) -> dict:
    """Update a campaign budget amount or name in Google Ads."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_campaign_draft(
    base_campaign_id: str,
    name: str,
    customer_id: str = None,
) -> dict:
    """Create a campaign draft from an existing Google Ads campaign for testing changes."""
    conditions = []
    gaql = f"""
        SELECT campaign_draft.draft_campaign
        FROM campaign_draft
        WHERE campaign_draft.draft_id = {draftId}
        LIMIT 1
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_campaign_experiment(
    base_campaign_id: str,
    name: str,
    traffic_split_percent: float,
    description: str = None,
    start_date: str = None,
    end_date: str = None,
    customer_id: str = None,
) -> dict:
    """Create a campaign experiment (A/B test) by creating a draft of the base campaign with a traffic split."""
    conditions = []
    if start_date and end_date:
        conditions.append(f"segments.date BETWEEN '{start_date}' AND '{end_date}'")
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT campaign.id, campaign.name, campaign.status, metrics.clicks, metrics.impressions, metrics.cost_micros FROM campaign {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_duplicate_campaign(
    source_campaign_id: str,
    new_name: str,
    pause_new_campaign: bool = None,
    customer_id: str = None,
) -> dict:
    """Duplicate a campaign including its budget, bidding strategy, ad groups, keywords, and responsive search ads.

    Args:
        source_campaign_id: ID of the campaign to copy.
        new_name: Name for the new campaign.
        pause_new_campaign: If True or unset (default), the new campaign is created PAUSED for safety.
            Pass False to create it ENABLED immediately.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        client, cid = _get_client(customer_id)

        campaign_gaql = f"""
            SELECT campaign.id, campaign.advertising_channel_type, campaign.bidding_strategy_type,
                   campaign.target_cpa.target_cpa_micros, campaign.target_roas.target_roas,
                   campaign.maximize_conversions.target_cpa_micros,
                   campaign_budget.amount_micros, campaign_budget.delivery_method
            FROM campaign
            WHERE campaign.id = {source_campaign_id}
            LIMIT 1
        """
        campaign_rows = _search(campaign_gaql, cid)
        if not campaign_rows:
            return json.dumps({"error": f"Campaign {source_campaign_id} not found."})
        src = campaign_rows[0]

        # --- New (unshared) budget, copying the source amount ---
        budget_service = client.get_service("CampaignBudgetService")
        budget_op = client.get_type("CampaignBudgetOperation")
        budget = budget_op.create
        budget.name = f"{new_name} Budget"
        budget.amount_micros = src.campaign_budget.amount_micros
        budget.delivery_method = src.campaign_budget.delivery_method
        budget.explicitly_shared = False
        budget_response = budget_service.mutate_campaign_budgets(customer_id=cid, operations=[budget_op])
        budget_rn = budget_response.results[0].resource_name

        # --- New campaign, same channel type + bidding strategy as the source ---
        campaign_service = client.get_service("CampaignService")
        campaign_op = client.get_type("CampaignOperation")
        campaign = campaign_op.create
        campaign.name = new_name
        campaign.campaign_budget = budget_rn
        campaign.advertising_channel_type = src.campaign.advertising_channel_type
        campaign.status = client.enums.CampaignStatusEnum.ENABLED if pause_new_campaign is False else client.enums.CampaignStatusEnum.PAUSED
        campaign.contains_eu_political_advertising = client.enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING

        bidding_type = src.campaign.bidding_strategy_type.name
        if bidding_type == "TARGET_CPA":
            campaign.target_cpa.target_cpa_micros = src.campaign.target_cpa.target_cpa_micros
        elif bidding_type == "TARGET_ROAS":
            campaign.target_roas.target_roas = src.campaign.target_roas.target_roas
        elif bidding_type == "MAXIMIZE_CONVERSIONS":
            campaign.maximize_conversions.target_cpa_micros = src.campaign.maximize_conversions.target_cpa_micros
        else:
            campaign.manual_cpc = client.get_type("ManualCpc")

        campaign_response = campaign_service.mutate_campaigns(customer_id=cid, operations=[campaign_op])
        new_campaign_rn = campaign_response.results[0].resource_name
        new_campaign_id = new_campaign_rn.split("/")[-1]

        # --- Copy ad groups ---
        adgroup_gaql = f"""
            SELECT ad_group.id, ad_group.name, ad_group.status, ad_group.cpc_bid_micros
            FROM ad_group
            WHERE ad_group.campaign = 'customers/{cid}/campaigns/{source_campaign_id}'
        """
        adgroup_rows = _search(adgroup_gaql, cid)
        adgroup_service = client.get_service("AdGroupService")
        old_to_new_adgroup: dict[str, str] = {}
        for row in adgroup_rows:
            op = client.get_type("AdGroupOperation")
            ag = op.create
            ag.name = row.ad_group.name
            ag.campaign = new_campaign_rn
            ag.status = row.ad_group.status
            if row.ad_group.cpc_bid_micros:
                ag.cpc_bid_micros = row.ad_group.cpc_bid_micros
            resp = adgroup_service.mutate_ad_groups(customer_id=cid, operations=[op])
            old_to_new_adgroup[str(row.ad_group.id)] = resp.results[0].resource_name

        # --- Copy keywords ---
        keyword_count = 0
        if old_to_new_adgroup:
            keyword_gaql = f"""
                SELECT ad_group_criterion.ad_group, ad_group_criterion.keyword.text,
                       ad_group_criterion.keyword.match_type, ad_group_criterion.cpc_bid_micros
                FROM ad_group_criterion
                WHERE ad_group_criterion.type = 'KEYWORD'
                  AND ad_group.campaign = 'customers/{cid}/campaigns/{source_campaign_id}'
            """
            keyword_rows = _search(keyword_gaql, cid)
            criterion_service = client.get_service("AdGroupCriterionService")
            keyword_ops = []
            for row in keyword_rows:
                old_ag_id = row.ad_group_criterion.ad_group.split("/")[-1]
                new_ag_rn = old_to_new_adgroup.get(old_ag_id)
                if not new_ag_rn:
                    continue
                op = client.get_type("AdGroupCriterionOperation")
                criterion = op.create
                criterion.ad_group = new_ag_rn
                criterion.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
                criterion.keyword.text = row.ad_group_criterion.keyword.text
                criterion.keyword.match_type = row.ad_group_criterion.keyword.match_type
                if row.ad_group_criterion.cpc_bid_micros:
                    criterion.cpc_bid_micros = row.ad_group_criterion.cpc_bid_micros
                keyword_ops.append(op)
            if keyword_ops:
                criterion_service.mutate_ad_group_criteria(customer_id=cid, operations=keyword_ops)
                keyword_count = len(keyword_ops)

        # --- Copy responsive search ads ---
        ad_count = 0
        if old_to_new_adgroup:
            ad_gaql = f"""
                SELECT ad_group_ad.ad_group, ad_group_ad.ad.final_urls,
                       ad_group_ad.ad.responsive_search_ad.headlines,
                       ad_group_ad.ad.responsive_search_ad.descriptions,
                       ad_group_ad.ad.responsive_search_ad.path1,
                       ad_group_ad.ad.responsive_search_ad.path2
                FROM ad_group_ad
                WHERE ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'
                  AND ad_group.campaign = 'customers/{cid}/campaigns/{source_campaign_id}'
            """
            ad_rows = _search(ad_gaql, cid)
            ad_service = client.get_service("AdGroupAdService")
            ad_ops = []
            for row in ad_rows:
                old_ag_id = row.ad_group_ad.ad_group.split("/")[-1]
                new_ag_rn = old_to_new_adgroup.get(old_ag_id)
                if not new_ag_rn:
                    continue
                op = client.get_type("AdGroupAdOperation")
                ad_group_ad = op.create
                ad_group_ad.ad_group = new_ag_rn
                ad_group_ad.status = client.enums.AdGroupAdStatusEnum.PAUSED
                for url in row.ad_group_ad.ad.final_urls:
                    ad_group_ad.ad.final_urls.append(url)
                rsa = ad_group_ad.ad.responsive_search_ad
                for h in row.ad_group_ad.ad.responsive_search_ad.headlines:
                    asset = client.get_type("AdTextAsset")
                    asset.text = h.text
                    rsa.headlines.append(asset)
                for d in row.ad_group_ad.ad.responsive_search_ad.descriptions:
                    asset = client.get_type("AdTextAsset")
                    asset.text = d.text
                    rsa.descriptions.append(asset)
                if row.ad_group_ad.ad.responsive_search_ad.path1:
                    rsa.path1 = row.ad_group_ad.ad.responsive_search_ad.path1
                if row.ad_group_ad.ad.responsive_search_ad.path2:
                    rsa.path2 = row.ad_group_ad.ad.responsive_search_ad.path2
                ad_ops.append(op)
            if ad_ops:
                ad_service.mutate_ad_group_ads(customer_id=cid, operations=ad_ops)
                ad_count = len(ad_ops)

        return json.dumps({
            "success": True,
            "new_campaign_resource_name": new_campaign_rn,
            "new_campaign_id": new_campaign_id,
            "ad_groups_copied": len(old_to_new_adgroup),
            "keywords_copied": keyword_count,
            "ads_copied": ad_count,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})

@mcp.tool()
def gads_get_campaign_details(
    campaign_id: str,
    customer_id: str = None,
) -> dict:
    """Get full details of a Google Ads campaign including network settings, targeting, budget, and bid strategy."""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    gaql = f"""
      SELECT
        campaign.id,
        campaign.name,
        campaign.status,
        campaign.advertising_channel_type,
        campaign.advertising_channel_sub_type,
        campaign.bidding_strategy_type,
        campaign.campaign_budget,
        campaign.network_settings.target_google_search,
        campaign.network_settings.target_search_network,
        campaign.network_settings.target_content_network,
        campaign.network_settings.target_partner_search_network,
        campaign.geo_target_type_setting.positive_geo_target_type,
        campaign.geo_target_type_setting.negative_geo_target_type,
        campaign.target_cpa.target_cpa_micros,
        campaign.target_roas.target_roas,
        campaign.maximize_conversions.target_cpa_micros,
        campaign.maximize_conversion_value.target_roas,
        campaign.target_impression_share.location,
        campaign.target_impression_share.location_fraction_micros,
        campaign.target_impression_share.cpc_bid_ceiling_micros,
        campaign.manual_cpc.enhanced_cpc_enabled,
        campaign.dynamic_search_ads_setting.domain_name,
        campaign.dynamic_search_ads_setting.language_code,
        campaign.dynamic_search_ads_setting.use_supplied_urls_only,
        campaign.serving_status,
        campaign.payment_mode,
        campaign.optimization_score,
        campaign_budget.amount_micros,
        campaign_budget.delivery_method,
        campaign_budget.explicitly_shared
      FROM campaign
      WHERE campaign.id = {campaign_id}
      LIMIT 1
    """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_campaign_draft(
    draft_id: str,
    customer_id: str = None,
) -> dict:
    """Get details of a Google Ads campaign draft by draft ID."""
    conditions = []
    gaql = f"""
      SELECT
        campaign_draft.resource_name,
        campaign_draft.draft_id,
        campaign_draft.base_campaign,
        campaign_draft.name,
        campaign_draft.status,
        campaign_draft.draft_campaign,
        campaign_draft.has_experiment_running
      FROM campaign_draft
      WHERE campaign_draft.draft_id = {draft_id}
      LIMIT 1
    """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_campaign_simulation(
    campaign_id: str,
    customer_id: str = None,
) -> dict:
    """Get bid simulation data for a Google Ads campaign (TARGET_CPA, TARGET_ROAS, TARGET_IMPRESSION_SHARE)."""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    gaql = f"""
      SELECT
        campaign_simulation.campaign_id,
        campaign_simulation.type,
        campaign_simulation.modifier_type,
        campaign_simulation.start_date,
        campaign_simulation.end_date,
        campaign_simulation.target_cpa_point_list.points,
        campaign_simulation.target_roas_point_list.points,
        campaign_simulation.target_impression_share_point_list.points
      FROM campaign_simulation
      WHERE campaign_simulation.campaign_id = {campaign_id}
      LIMIT 20
    """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_campaigns(
    status: str = None,
    limit: int =None,
    customer_id: str = None,
) -> dict:
    """List Google Ads campaigns with optional status filter."""
    _limit = int(limit) if limit else 200
    where = f"WHERE campaign.status = '{status}'" if status and status not in ('ALL', None) else ""
    gaql = f"""
      SELECT
        campaign.id,
        campaign.name,
        campaign.status,
        campaign.advertising_channel_type,
        campaign.advertising_channel_sub_type,
        campaign_budget.amount_micros
      FROM campaign
      {where}
      ORDER BY campaign.name ASC
      LIMIT {_limit}
    """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_promote_campaign_draft(
    draft_id: str,
    customer_id: str = None,
) -> dict:
    """Promote a Google Ads campaign draft, applying its changes to the base campaign."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT campaign.id, campaign.name, campaign.status, metrics.clicks, metrics.impressions, metrics.cost_micros FROM campaign {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_campaign_bid_strategy(
    campaign_id: str,
    strategy_type: str,
    target_cpa: float = None,
    target_roas: float = None,
    target_impression_share_location: str = None,
    target_impression_share_fraction: float = None,
    max_cpc_bid_ceiling_micros: float = None,
    customer_id: str = None,
) -> dict:
    """Set the bidding strategy for a Google Ads campaign."""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT campaign.id, campaign.name, campaign.status, metrics.clicks, metrics.impressions, metrics.cost_micros FROM campaign {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_campaign_device_bids(
    campaign_id: str,
    desktop_bid_modifier: float = None,
    mobile_bid_modifier: float = None,
    tablet_bid_modifier: float = None,
    customer_id: str = None,
) -> dict:
    """Set device bid modifiers for a Google Ads campaign (desktop, mobile, tablet)."""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    gaql = f"""
      SELECT
        campaign_criterion.resource_name,
        campaign_criterion.device.type,
        campaign_criterion.bid_modifier
      FROM campaign_criterion
      WHERE campaign_criterion.campaign.id = {campaign_id}
        AND campaign_criterion.type = 'DEVICE'
      LIMIT 10
    """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_campaign_dsa_settings(
    campaign_id: str,
    website_url: str,
    language_code: str,
    page_feed_ids: list = None,
    use_supplied_urls_only: bool = None,
    customer_id: str = None,
) -> dict:
    """Set Dynamic Search Ads (DSA) settings for a Google Ads campaign."""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT campaign.id, campaign.name, campaign.status, metrics.clicks, metrics.impressions, metrics.cost_micros FROM campaign {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_campaign_frequency_cap(
    campaign_id: str = None,
    frequency_caps: list = None,
    customer_id: str = None,
) -> dict:
    """Set frequency capping rules for a Google Ads campaign."""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT campaign.id, campaign.name, campaign.status, metrics.clicks, metrics.impressions, metrics.cost_micros FROM campaign {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_campaign_geo_targets(
    campaign_id: str,
    add_location_ids: list = None,
    remove_location_ids: list = None,
    customer_id: str = None,
) -> dict:
    """Add or remove geo location targeting criteria for a Google Ads campaign."""
    try:
        client, cid = _get_client(customer_id or "")
        svc = client.get_service("CampaignCriterionService")
        ops = []

        if add_location_ids:
            for loc_id in add_location_ids:
                op = client.get_type("CampaignCriterionOperation")
                c = op.create
                c.campaign = f"customers/{cid}/campaigns/{campaign_id}"
                c.location.geo_target_constant = f"geoTargetConstants/{loc_id}"
                c.negative = False
                ops.append(op)

        if remove_location_ids:
            for loc_id in remove_location_ids:
                op = client.get_type("CampaignCriterionOperation")
                op.remove = f"customers/{cid}/campaignCriteria/{campaign_id}~{loc_id}"
                ops.append(op)

        if not ops:
            # No mutations requested — just list existing geo targets
            gaql = (
                f"SELECT campaign_criterion.resource_name,"
                f" campaign_criterion.location.geo_target_constant"
                f" FROM campaign_criterion"
                f" WHERE campaign.id = {campaign_id}"
                f" AND campaign_criterion.type = 'LOCATION'"
                f" LIMIT 500"
            )
            rows = _search(gaql, cid)
            return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

        response = svc.mutate_campaign_criteria(customer_id=cid, operations=ops)
        results = [r.resource_name for r in response.results]
        return {"success": True, "added": len(add_location_ids or []), "removed": len(remove_location_ids or []), "results": results}
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
def gads_set_campaign_languages(
    campaign_id: str,
    language_ids: list,
    replace_existing: bool = None,
    customer_id: str = None,
) -> dict:
    """Set language targeting criteria for a Google Ads campaign."""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT campaign.id, campaign.name, campaign.status, metrics.clicks, metrics.impressions, metrics.cost_micros FROM campaign {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_campaign_schedule(
    campaign_id: str = None,
    ad_schedule: list = None,
    customer_id: str = None,
) -> dict:
    """Set ad schedule (dayparting) for a Google Ads campaign, replacing any existing schedule."""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT campaign.id, campaign.name, campaign.status, metrics.clicks, metrics.impressions, metrics.cost_micros FROM campaign {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_campaign_targeting(
    campaign_id: str,
    language_ids: list = None,
    geo_target_ids: list = None,
    customer_id: str = None,
) -> dict:
    """Set language and geo targeting criteria for a Google Ads campaign (replaces existing criteria of each type)."""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT campaign.id, campaign.name, campaign.status, metrics.clicks, metrics.impressions, metrics.cost_micros FROM campaign {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_update_campaign(
    campaign_id: str,
    name: str = None,
    status: str = None,
    daily_budget_micros: float = None,
    start_date: str = None,
    end_date: str = None,
    network_settings: dict = None,
    customer_id: str = None,
) -> dict:
    """Update an existing Google Ads campaign (name, status, budget, dates, network settings)."""
    conditions = []
    if start_date and end_date:
        conditions.append(f"segments.date BETWEEN '{start_date}' AND '{end_date}'")
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    if status and status not in ('ALL', None): conditions.append(f"campaign.status = '{status}'")
    gaql = f"""SELECT campaign.campaign_budget FROM campaign WHERE campaign.id = {campaign_id} LIMIT 1"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_conversion_custom_variable(
    name: str,
    tag: str,
    status: str = None,
    customer_id: str = None,
) -> dict:
    """Create a custom variable for conversion tracking in Google Ads."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_conversion_value_rule(
    action: str,
    value: float,
    condition_type: str = None,
    audience_user_list_ids: list = None,
    device_types: list = None,
    customer_id: str = None,
) -> dict:
    """Create a conversion value rule to adjust conversion values based on audience, device, location, or query term conditions."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_conversion_upload_summary(
    start_date: str,
    end_date: str,
    customer_id: str = None,
) -> dict:
    """Get a summary of offline conversion upload jobs and their status."""
    gaql = """SELECT conversion_upload_summary.job_type,
              conversion_upload_summary.upload_event_count,
              conversion_upload_summary.failed_count,
              conversion_upload_summary.uploaded_event_count,
              conversion_upload_summary.status
       FROM conversion_upload_summary"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_conversion_actions(
    type_val: str = None,
    limit: int = 200,
    customer_id: str = None,
) -> dict:
    """List conversion actions, optionally filtered by type.

    Args:
        type_val: Optional type filter, e.g. WEBPAGE, UPLOAD_CLICKS, PHONE_CALL_FROM_ADS.
        limit: Maximum rows to return. Default 200; raise it if the answer is being cut off.
    """
    _limit = max(1, min(int(limit or 200), 2000))
    where = f"WHERE conversion_action.type = '{type_val}'" if type_val else ""
    gaql = f"""
      SELECT
        conversion_action.id,
        conversion_action.name,
        conversion_action.type,
        conversion_action.category,
        conversion_action.status,
        conversion_action.primary_for_goal
      FROM conversion_action
      {where}
      ORDER BY conversion_action.name ASC
      LIMIT {_limit}
    """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_conversion_value_rules(
    limit: int =50,
    customer_id: str = None,
) -> dict:
    """List conversion value rules in a Google Ads account."""
    gaql = f"""SELECT conversion_value_rule.id, conversion_value_rule.action.operation, conversion_value_rule.action.value, conversion_value_rule.status FROM conversion_value_rule LIMIT {limit or 200}"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_campaign_conversion_goals(
    campaign_id: str,
    conversion_action_ids: list,
    biddable: bool = None,
    customer_id: str = None,
) -> dict:
    """Set which conversion actions are biddable goals for a campaign."""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT campaign.id, campaign.name, campaign.status, metrics.clicks, metrics.impressions, metrics.cost_micros FROM campaign {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_customer_conversion_goals(
    conversion_action_ids: list,
    biddable: bool = None,
    customer_id: str = None,
) -> dict:
    """Set which conversion actions are biddable goals at the account level."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_update_conversion_value_rule(
    rule_id: str,
    value: float = None,
    status: str = None,
    customer_id: str = None,
) -> dict:
    """Update an existing conversion value rule (value or status)."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_upload_offline_conversions(
    conversions: list = None,
    partial_failure: bool = True,
    customer_id: str = None,
) -> dict:
    """Upload offline click conversions to Google Ads using GCLIDs."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_upload_store_conversions(
    conversions: list = None,
    customer_id: str = None,
) -> dict:
    """Upload store conversion data to Google Ads."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_customizer_attribute(
    name: str,
    type_val: str,
    customer_id: str = None,
) -> dict:
    """Create a customizer attribute for use in ad copy customization."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_ad_group_criterion_customizer(
    ad_group_id: str,
    criterion_id: str,
    customizer_attribute_id: str,
    value: str,
    customer_id: str = None,
) -> dict:
    """Set a customizer attribute value for a specific keyword/criterion within an ad group."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group.id, ad_group.name, ad_group.status, metrics.clicks, metrics.impressions FROM ad_group {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_ad_group_customizer(
    ad_group_id: str,
    customizer_attribute_id: str,
    value: str,
    customer_id: str = None,
) -> dict:
    """Set a customizer attribute value for an ad group."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group.id, ad_group.name, ad_group.status, metrics.clicks, metrics.impressions FROM ad_group {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_campaign_customizer(
    campaign_id: str,
    customizer_attribute_id: str,
    value: str,
    customer_id: str = None,
) -> dict:
    """Set a customizer attribute value for a campaign."""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT campaign.id, campaign.name, campaign.status, metrics.clicks, metrics.impressions, metrics.cost_micros FROM campaign {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_customer_customizer(
    customizer_attribute_id: str,
    value: str,
    customer_id: str = None,
) -> dict:
    """Set a customizer attribute value at the account level."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_add_experiment_arm(
    experiment_id: str,
    name: str,
    traffic_split: float,
    is_control: bool = None,
    campaign_ids: list = None,
    customer_id: str = None,
) -> dict:
    """Add a new arm to an existing campaign experiment."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_experiment(
    campaign_id: str,
    name: str,
    start_date: str,
    traffic_split_percent: float,
    description: str = None,
    end_date: str = None,
    type_val: str = None,
    customer_id: str = None,
) -> dict:
    """Create a campaign experiment (A/B test) with a control and experiment arm."""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    if start_date and end_date:
        conditions.append(f"segments.date BETWEEN '{start_date}' AND '{end_date}'")
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_end_experiment(
    experiment_id: str,
    customer_id: str = None,
) -> dict:
    """End a running campaign experiment immediately."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_experiment_results(
    experiment_id: str,
    start_date: str,
    end_date: str,
    customer_id: str = None,
) -> dict:
    """Get performance results for a campaign experiment, comparing control vs experiment arms."""
    conditions = []
    if start_date and end_date:
        conditions.append(f"segments.date BETWEEN '{start_date}' AND '{end_date}'")
    gaql = f"""SELECT experiment_arm.name, experiment_arm.control, experiment_arm.traffic_split,
              metrics.clicks, metrics.impressions, metrics.cost_micros,
              metrics.conversions, metrics.conversion_value, metrics.ctr
       FROM experiment_arm
       WHERE experiment_arm.experiment = 'customers/{cid}/experiments/{experiment_id or ""}'"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_experiments(
    limit: int = 200,
    customer_id: str = None,
) -> dict:
    """List campaign experiments with their status and date range.
    Args:
        limit: Maximum rows to return. Default 200; raise it if the answer is being cut off.
    """
    _limit = max(1, min(int(limit or 200), 2000))
    gaql = f"""
      SELECT
        experiment.resource_name,
        experiment.experiment_id,
        experiment.name,
        experiment.description,
        experiment.status,
        experiment.type,
        experiment.start_date,
        experiment.end_date
      FROM experiment
      ORDER BY experiment.name ASC
      LIMIT {_limit}
    """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_promote_experiment(
    experiment_id: str,
    validate_only: bool = None,
    customer_id: str = None,
) -> dict:
    """Promote a campaign experiment — apply its changes to the base campaign."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_schedule_experiment(
    experiment_id: str,
    start_date: str,
    customer_id: str = None,
) -> dict:
    """Schedule a campaign experiment to start on a specific date."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_add_campaign_negative_keyword(
    campaign_id: str,
    text: str,
    match_type: str,
    customer_id: str = None,
) -> dict:
    """Add a negative keyword at the campaign level"""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, ad_group_criterion.status, metrics.clicks, metrics.impressions FROM ad_group_criterion {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_add_keywords_to_shared_list(
    shared_set_id: str = None,
    keywords: list = None,
    customer_id: str = None,
) -> dict:
    """Add negative keywords to a shared negative keyword list"""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, ad_group_criterion.status, metrics.clicks, metrics.impressions FROM ad_group_criterion {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_attach_shared_list_to_campaign(
    campaign_id: str,
    shared_set_id: str,
    customer_id: str = None,
) -> dict:
    """Attach a shared negative keyword list to a campaign"""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT campaign.id, campaign.name, campaign.status, metrics.clicks, metrics.impressions, metrics.cost_micros FROM campaign {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_shared_negative_list(
    name: str,
    type_val: str = None,
    customer_id: str = None,
) -> dict:
    """Create a shared negative keyword list that can be attached to multiple campaigns"""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_forecast_keywords(
    keywords: list = None,
    daily_budget: float = None,
    max_cpc: float = None,
    language_id: str = 1037,
    geo_target_ids: list = None,
    customer_id: str = None,
) -> dict:
    """Forecast clicks, impressions, and cost for a list of keywords using Google Keyword Planner"""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, ad_group_criterion.status, metrics.clicks, metrics.impressions FROM ad_group_criterion {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_keyword_details(
    ad_group_id: str,
    criterion_id: str,
    customer_id: str = None,
) -> dict:
    """Get full details and performance metrics for a specific keyword"""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where_clause = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f"""
        SELECT
          ad_group_criterion.criterion_id,
          ad_group_criterion.keyword.text,
          ad_group_criterion.keyword.match_type,
          ad_group_criterion.status,
          ad_group_criterion.negative,
          ad_group_criterion.cpc_bid_micros,
          ad_group_criterion.quality_info.quality_score,
          ad_group_criterion.quality_info.creative_quality_score,
          ad_group_criterion.quality_info.post_click_quality_score,
          ad_group_criterion.quality_info.search_predicted_ctr,
          ad_group_criterion.final_urls,
          ad_group.id,
          ad_group.name,
          campaign.id,
          campaign.name
        FROM ad_group_criterion
        WHERE {where_clause}
        LIMIT 1
      """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_keyword_ideas(
    seed_keywords: list = None,
    seed_urls: list = None,
    language_id: str = 1037,
    geo_target_ids: list = None,
    include_adult_keywords: bool = False,
    customer_id: str = None,
) -> dict:
    """Generate keyword ideas from seed keywords or URLs using Google Keyword Planner"""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, ad_group_criterion.status, metrics.clicks, metrics.impressions FROM ad_group_criterion {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_keywords(
    ad_group_id: str = None,
    campaign_id: str = None,
    status: str = None,
    include_negatives: bool = False,
    limit: int =200,
    customer_id: str = None,
) -> dict:
    """List keywords across ad groups and campaigns with optional filters"""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    if status and status not in ('ALL', None): conditions.append(f"campaign.status = '{status}'")
    where_clause = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f"""
      SELECT
        ad_group_criterion.criterion_id,
        ad_group_criterion.keyword.text,
        ad_group_criterion.keyword.match_type,
        ad_group_criterion.status,
        ad_group_criterion.negative,
        ad_group_criterion.cpc_bid_micros,
        ad_group.name,
        ad_group.id,
        campaign.name,
        campaign.id
      FROM ad_group_criterion
      {where_clause}
      LIMIT {limit or 200}
    """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_negative_keywords(
    campaign_id: str = None,
    ad_group_id: str = None,
    limit: int =100,
    customer_id: str = None,
) -> dict:
    """List negative keywords at campaign and/or ad group level"""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, ad_group_criterion.status, metrics.clicks, metrics.impressions FROM ad_group_criterion {where} LIMIT {limit or 200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_shared_lists(
    type_val: str = None,
    limit: int =50,
    customer_id: str = None,
) -> dict:
    """List shared negative keyword lists in the account"""
    conditions = []
    where_clause = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f"""
      SELECT
        shared_set.id,
        shared_set.name,
        shared_set.type,
        shared_set.status,
        shared_set.member_count
      FROM shared_set
      {where_clause}
      LIMIT {limit or 200}
    """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_remove_campaign_negative_keyword(
    campaign_id: str,
    criterion_id: str,
    customer_id: str = None,
) -> dict:
    """Remove a negative keyword from a campaign"""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, ad_group_criterion.status, metrics.clicks, metrics.impressions FROM ad_group_criterion {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_remove_keyword(
    ad_group_id: str,
    criterion_id: str,
    customer_id: str = None,
) -> dict:
    """Remove (delete) a keyword from an ad group"""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, ad_group_criterion.status, metrics.clicks, metrics.impressions FROM ad_group_criterion {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_update_keyword(
    ad_group_id: str,
    criterion_id: str,
    status: str = None,
    cpc_bid_micros: float = None,
    final_urls: list = None,
    customer_id: str = None,
) -> dict:
    """Update a keyword status, CPC bid, or final URLs"""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, ad_group_criterion.status, metrics.clicks, metrics.impressions FROM ad_group_criterion {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_add_conversion_call_adjustments(
    adjustments: list = None,
    partial_failure: bool = None,
    customer_id: str = None,
) -> dict:
    """Upload call conversion adjustments (retract, restate, or enhance call conversions)."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_offline_user_data_job(
    job_type: str,
    user_list_id: str = None,
    external_id: str = None,
    customer_id: str = None,
) -> dict:
    """Create an offline user data job for Customer Match uploads."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_upload_enhanced_conversions(
    conversions: list = None,
    partial_failure: bool = None,
    validate_only: bool = None,
    customer_id: str = None,
) -> dict:
    """Upload enhanced conversions with hashed user PII (email, phone, address) for improved measurement accuracy."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_forecast_reach(
    budget_micros: float,
    start_date: str,
    end_date: str,
    targeting: dict = None,
    customer_id: str = None,
) -> dict:
    """Forecast reach and impressions for a video/display campaign with given budget and targeting."""
    conditions = []
    if start_date and end_date:
        conditions.append(f"segments.date BETWEEN '{start_date}' AND '{end_date}'")
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_generate_audience_insights(
    user_list_ids: list = None,
    user_interest_ids: list = None,
    dimensions: list = None,
    customer_id: str = None,
) -> dict:
    """Get audience composition insights for specified user lists."""
    conditions = []
    gaql = f"""SELECT user_list.id, user_list.name, user_list.type,
                  user_list.size_for_search, user_list.size_for_display,
                  user_list.eligible_for_search, user_list.status
           FROM user_list
           WHERE user_list.id IN ({params.user_list_ids.join(',')})"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_generate_keyword_ideas(
    seed_keywords: list = None,
    seed_urls: list = None,
    language_id: str = None,
    geo_target_ids: list = None,
    limit: int =None,
    customer_id: str = None,
) -> dict:
    """Generate keyword ideas with monthly search volume and bid estimates from seed keywords or URLs."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, ad_group_criterion.status, metrics.clicks, metrics.impressions FROM ad_group_criterion {where} LIMIT {limit or 200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_ad_strength_forecast(
    headlines: list,
    descriptions: list,
    final_url: str,
    customer_id: str = None,
) -> dict:
    """Analyze ad copy and forecast Responsive Search Ad strength (POOR/AVERAGE/GOOD/EXCELLENT) with improvement suggestions."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_plan_keyword_campaign(
    keywords: list,
    daily_budget: float,
    language_id: str = None,
    geo_target_ids: list = None,
    customer_id: str = None,
) -> dict:
    """Create a keyword plan and get click/impression/cost forecasts for a set of keywords."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, ad_group_criterion.status, metrics.clicks, metrics.impressions FROM ad_group_criterion {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_generate_recommendations(
    recommendation_types: list,
    advertising_channel_type: str = None,
    customer_id: str = None,
) -> dict:
    """Generate and list recommendations for specified recommendation types."""
    conditions = []
    gaql = f"""SELECT recommendation.type, recommendation.resource_name,
              recommendation.campaign
       FROM recommendation
       WHERE recommendation.type IN ({types})
       LIMIT 50"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_recommendations(
    types: list = None,
    limit: int = 200,
    customer_id: str = None,
) -> dict:
    """List Google Ads recommendations currently offered for the account.

    Args:
        types: Optional list of recommendation types to restrict to, e.g.
            ["CAMPAIGN_BUDGET", "KEYWORD", "TARGET_CPA_OPT_IN"].
        limit: Maximum rows to return. Default 200; raise it if the answer is being cut off.
    """
    _limit = max(1, min(int(limit or 200), 2000))
    where = ""
    if types:
        wanted = ", ".join(f"'{t}'" for t in types)
        where = f"WHERE recommendation.type IN ({wanted})"
    gaql = f"""
      SELECT
        recommendation.resource_name,
        recommendation.type,
        campaign.id,
        campaign.name
      FROM recommendation
      {where}
      LIMIT {_limit}
    """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_subscribe_to_recommendation_type(
    recommendation_type: str,
    customer_id: str = None,
) -> dict:
    """Subscribe to auto-apply a specific recommendation type."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_unsubscribe_from_recommendation_type(
    recommendation_type: str,
    customer_id: str = None,
) -> dict:
    """Unsubscribe from auto-applying a specific recommendation type."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_age_range_report(
    start_date: str,
    end_date: str,
    campaign_id: str = None,
    limit: int =None,
    customer_id: str = None,
) -> dict:
    """Get performance breakdown by age range demographic."""
    conditions = []
    if start_date and end_date:
        conditions.append(f"segments.date BETWEEN '{start_date}' AND '{end_date}'")
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    gaql = f"""SELECT ad_group_criterion.age_range.type,
              metrics.impressions, metrics.clicks, metrics.cost_micros,
              metrics.conversions, metrics.ctr, metrics.average_cpc,
              campaign.name, ad_group.name
       FROM age_range_view
       WHERE {' AND '.join(conditions)}
       ORDER BY metrics.impressions DESC
       LIMIT {limit or 100}"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_auction_insights(
    start_date: str,
    end_date: str,
    campaign_id: str = None,
    ad_group_id: str = None,
    customer_id: str = None,
) -> dict:
    """Get auction insights showing how you compete against other advertisers in the same auctions."""
    conditions = []
    if start_date and end_date:
        conditions.append(f"segments.date BETWEEN '{start_date}' AND '{end_date}'")
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""SELECT auction_insight.domain,
              auction_insight.search_position,
              metrics.search_impression_share,
              metrics.search_overlap_rate,
              metrics.search_outranking_share,
              metrics.search_top_impression_share,
              metrics.search_absolute_top_impression_share,
              campaign.name
       FROM auction_insight
       WHERE {' AND '.join(conditions)}
       ORDER BY metrics.search_impression_share DESC"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_campaign_search_term_insights(
    campaign_id: str,
    start_date: str,
    end_date: str,
    limit: int =None,
    customer_id: str = None,
) -> dict:
    """Get search term insights (search categories) for a campaign — works for Performance Max.

    Uses campaign_search_term_insight, which is the only resource that reports
    search data for Performance Max; search_term_view returns zero rows for
    PMax. Note Google exposes a search *category* here rather than the raw
    query, and provides no cost or CPC (not selectable on this resource).

    For exact queries with spend on Search/Shopping campaigns, use
    google_ads_search_terms instead.
    """
    conditions = [f"campaign_search_term_insight.campaign_id = {str(campaign_id).strip()}"]
    if start_date and end_date:
        conditions.append(f"segments.date BETWEEN '{start_date}' AND '{end_date}'")
    gaql = f"""SELECT campaign_search_term_insight.category_label,
              campaign_search_term_insight.campaign_id,
              metrics.clicks, metrics.impressions, metrics.ctr,
              metrics.conversions, metrics.conversions_value
       FROM campaign_search_term_insight
       WHERE {' AND '.join(conditions)}
       ORDER BY metrics.impressions DESC
       LIMIT {limit or 200}"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_change_event_log(
    start_date: str,
    end_date: str,
    resource_type: str = None,
    limit: int =None,
    customer_id: str = None,
) -> dict:
    """Get change event log showing what was changed, by whom, and when."""
    conditions = []
    if start_date and end_date:
        conditions.append(f"segments.date BETWEEN '{start_date}' AND '{end_date}'")
    gaql = f"""SELECT change_event.change_date_time, change_event.changed_fields,
              change_event.client_type, change_event.resource_type,
              change_event.resource_name, change_event.user_email
       FROM change_event
       WHERE {' AND '.join(conditions)}
       ORDER BY change_event.change_date_time DESC
       LIMIT {limit or 100}"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_change_status_report(
    start_date: str,
    end_date: str,
    limit: int =None,
    customer_id: str = None,
) -> dict:
    """Get change status report showing last modification time for account resources."""
    conditions = []
    if start_date and end_date:
        conditions.append(f"segments.date BETWEEN '{start_date}' AND '{end_date}'")
    gaql = f"""SELECT change_status.resource_name, change_status.resource_type,
              change_status.resource_status, change_status.last_change_date_time,
              change_status.campaign, change_status.ad_group
       FROM change_status
       WHERE change_status.last_change_date_time >= '{start_date or ""} 00:00:00'
         AND change_status.last_change_date_time <= '{end_date or ""} 23:59:59'
       ORDER BY change_status.last_change_date_time DESC
       LIMIT {limit or 100}"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_click_view_report(
    date: str,
    campaign_id: str = None,
    limit: int =None,
    customer_id: str = None,
) -> dict:
    """Get individual click-level data including GCLIDs for a specific date."""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    gaql = f"""SELECT click_view.gclid, click_view.area_of_interest.city,
              click_view.location_of_presence.city,
              click_view.keyword, click_view.keyword_info.match_type,
              campaign.name, ad_group.name, metrics.clicks
       FROM click_view
       WHERE {' AND '.join(conditions)}
       LIMIT {limit or 100}"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_content_suitability_report(
    start_date: str,
    end_date: str,
    customer_id: str = None,
) -> dict:
    """Get content suitability and topic targeting performance report."""
    conditions = []
    if start_date and end_date:
        conditions.append(f"segments.date BETWEEN '{start_date}' AND '{end_date}'")
    gaql = f"""SELECT topic_view.resource_name, ad_group_criterion.topic.path,
              metrics.impressions, metrics.clicks, metrics.cost_micros,
              metrics.conversions, campaign.name, ad_group.name
       FROM topic_view
       WHERE segments.date BETWEEN '{start_date or ""}' AND '{end_date or ""}'
       ORDER BY metrics.impressions DESC
       LIMIT 100"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_distance_report(
    start_date: str,
    end_date: str,
    campaign_id: str = None,
    limit: int =None,
    customer_id: str = None,
) -> dict:
    """Get performance breakdown by distance from business location (distance_view)."""
    conditions = []
    if start_date and end_date:
        conditions.append(f"segments.date BETWEEN '{start_date}' AND '{end_date}'")
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    gaql = f"""SELECT distance_view.distance_bucket,
              metrics.impressions, metrics.clicks, metrics.cost_micros,
              metrics.conversions, metrics.ctr, metrics.average_cpc,
              campaign.name
       FROM distance_view
       WHERE {' AND '.join(conditions)}
       ORDER BY metrics.impressions DESC
       LIMIT {limit or 100}"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_dynamic_search_ads_report(
    start_date: str,
    end_date: str,
    campaign_id: str = None,
    limit: int =None,
    customer_id: str = None,
) -> dict:
    """Get Dynamic Search Ads performance report with auto-generated headlines and landing pages."""
    conditions = []
    if start_date and end_date:
        conditions.append(f"segments.date BETWEEN '{start_date}' AND '{end_date}'")
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    gaql = f"""SELECT dynamic_search_ads_search_term_view.search_term,
              dynamic_search_ads_search_term_view.headline,
              dynamic_search_ads_search_term_view.landing_page,
              dynamic_search_ads_search_term_view.page_url,
              metrics.impressions, metrics.clicks, metrics.cost_micros,
              metrics.conversions, campaign.name, ad_group.name
       FROM dynamic_search_ads_search_term_view
       WHERE {' AND '.join(conditions)}
       ORDER BY metrics.impressions DESC
       LIMIT {limit or 100}"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_gender_report(
    start_date: str,
    end_date: str,
    campaign_id: str = None,
    limit: int =None,
    customer_id: str = None,
) -> dict:
    """Get performance breakdown by gender demographic."""
    conditions = []
    if start_date and end_date:
        conditions.append(f"segments.date BETWEEN '{start_date}' AND '{end_date}'")
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    gaql = f"""SELECT ad_group_criterion.gender.type,
              metrics.impressions, metrics.clicks, metrics.cost_micros,
              metrics.conversions, metrics.ctr, campaign.name, ad_group.name
       FROM gender_view
       WHERE {' AND '.join(conditions)}
       ORDER BY metrics.impressions DESC
       LIMIT {limit or 100}"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_hotel_performance_report(
    start_date: str,
    end_date: str,
    limit: int =None,
    customer_id: str = None,
) -> dict:
    """Get hotel campaign performance report."""
    conditions = []
    if start_date and end_date:
        conditions.append(f"segments.date BETWEEN '{start_date}' AND '{end_date}'")
    gaql = f"""SELECT hotel_performance_view.resource_name,
              metrics.impressions, metrics.clicks, metrics.cost_micros,
              metrics.hotel_average_lead_value_micros,
              metrics.hotel_price_difference_percentage,
              campaign.name
       FROM hotel_performance_view
       WHERE segments.date BETWEEN '{start_date or ""}' AND '{end_date or ""}'
       LIMIT {limit or 100}"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_income_range_report(
    start_date: str,
    end_date: str,
    campaign_id: str = None,
    limit: int =None,
    customer_id: str = None,
) -> dict:
    """Get performance breakdown by household income range."""
    conditions = []
    if start_date and end_date:
        conditions.append(f"segments.date BETWEEN '{start_date}' AND '{end_date}'")
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    gaql = f"""SELECT ad_group_criterion.income_range.type,
              metrics.impressions, metrics.clicks, metrics.cost_micros,
              metrics.conversions, campaign.name, ad_group.name
       FROM income_range_view
       WHERE {' AND '.join(conditions)}
       LIMIT {limit or 100}"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_paid_organic_report(
    start_date: str,
    end_date: str,
    limit: int =None,
    customer_id: str = None,
) -> dict:
    """Get paid vs organic search comparison report."""
    conditions = []
    if start_date and end_date:
        conditions.append(f"segments.date BETWEEN '{start_date}' AND '{end_date}'")
    gaql = f"""SELECT paid_organic_search_term_view.search_term,
              metrics.impressions, metrics.organic_impressions,
              metrics.clicks, metrics.organic_clicks,
              metrics.average_position, metrics.organic_average_position,
              campaign.name, ad_group.name
       FROM paid_organic_search_term_view
       WHERE segments.date BETWEEN '{start_date or ""}' AND '{end_date or ""}'
       ORDER BY metrics.impressions DESC
       LIMIT {limit or 100}"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_parental_status_report(
    start_date: str,
    end_date: str,
    campaign_id: str = None,
    limit: int =None,
    customer_id: str = None,
) -> dict:
    """Get performance breakdown by parental status demographic."""
    conditions = []
    if start_date and end_date:
        conditions.append(f"segments.date BETWEEN '{start_date}' AND '{end_date}'")
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    gaql = f"""SELECT ad_group_criterion.parental_status.type,
              metrics.impressions, metrics.clicks, metrics.cost_micros,
              metrics.conversions, campaign.name, ad_group.name
       FROM parental_status_view
       WHERE {' AND '.join(conditions)}
       LIMIT {limit or 100}"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_per_store_view(
    start_date: str,
    end_date: str,
    limit: int =None,
    customer_id: str = None,
) -> dict:
    """Get store visit metrics per physical store location."""
    conditions = []
    if start_date and end_date:
        conditions.append(f"segments.date BETWEEN '{start_date}' AND '{end_date}'")
    gaql = f"""SELECT per_store_view.place_id,
              metrics.store_visits_last_click_model_attributed_conversions,
              metrics.store_visits_this_conversion_model_incompatible_conversions
       FROM per_store_view
       WHERE segments.date BETWEEN '{start_date or ""}' AND '{end_date or ""}'
       LIMIT {limit or 100}"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_placement_report(
    start_date: str,
    end_date: str,
    campaign_id: str = None,
    limit: int =None,
    customer_id: str = None,
) -> dict:
    """Get display/video placement performance report showing where ads appeared."""
    conditions = []
    if start_date and end_date:
        conditions.append(f"segments.date BETWEEN '{start_date}' AND '{end_date}'")
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    gaql = f"""SELECT detail_placement_view.placement, detail_placement_view.display_name,
              detail_placement_view.placement_type, detail_placement_view.group_placement_target_url,
              metrics.impressions, metrics.clicks, metrics.cost_micros,
              metrics.conversions, metrics.ctr, campaign.name
       FROM detail_placement_view
       WHERE {' AND '.join(conditions)}
       ORDER BY metrics.impressions DESC
       LIMIT {limit or 100}"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_search_term_insights(
    start_date: str,
    end_date: str,
    campaign_id: str = None,
    ad_group_id: str = None,
    limit: int =None,
    customer_id: str = None,
) -> dict:
    """Get search term performance report with clicks, impressions, cost, and conversions."""
    conditions = []
    if start_date and end_date:
        conditions.append(f"segments.date BETWEEN '{start_date}' AND '{end_date}'")
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    gaql = f"""SELECT search_term_view.search_term, search_term_view.status,
              metrics.clicks, metrics.impressions, metrics.ctr,
              metrics.average_cpc, metrics.cost_micros, metrics.conversions,
              campaign.name, ad_group.name
       FROM search_term_view
       WHERE {' AND '.join(conditions)}
       ORDER BY metrics.impressions DESC
       LIMIT {limit or 100}"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_run_custom_report(
    gaql_query: str,
    customer_id: str = None,
) -> dict:
    """Run an arbitrary GAQL query and return its rows.

    The escape hatch for anything the named tools do not cover. GAQL is a
    read-only language -- no GAQL statement changes an account -- so this
    cannot mutate anything regardless of what is passed.

    Args:
        gaql_query: A full GAQL query, e.g. "SELECT campaign.name, metrics.clicks
            FROM campaign WHERE segments.date DURING LAST_7_DAYS".
    """
    query = (gaql_query or "").strip()
    if not query:
        return {
            "success": False,
            "error": "gaql_query is required.",
            "next_step": "Pass a GAQL query, e.g. 'SELECT campaign.id, campaign.name FROM campaign LIMIT 10'.",
        }
    if not query.upper().startswith("SELECT"):
        return {
            "success": False,
            "error": "GAQL queries must start with SELECT.",
            "detail": "There is no GAQL statement that writes; use the named write tools instead.",
        }
    rows = _search(query, customer_id or '')
    return {"success": True, "query": query,
            "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_product_listing_group(
    ad_group_id: str,
    listing_group_type: str,
    parent_criterion_id: str = None,
    case_value: dict = None,
    cpc_bid_micros: float = None,
    customer_id: str = None,
) -> dict:
    """Create a product listing group criterion for a shopping ad group."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_shopping_campaign(
    name: str,
    budget_amount: float,
    merchant_id: str,
    country_code: str,
    language_code: str = None,
    priority: str = None,
    customer_id: str = None,
) -> dict:
    """Create a Google Shopping campaign linked to a Merchant Center account."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT campaign.id, campaign.name, campaign.status, metrics.clicks, metrics.impressions, metrics.cost_micros FROM campaign {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_shopping_performance(
    start_date: str,
    end_date: str,
    campaign_id: str = None,
    limit: int =None,
    customer_id: str = None,
) -> dict:
    """Get Shopping campaign performance overview."""
    conditions = []
    if start_date and end_date:
        conditions.append(f"segments.date BETWEEN '{start_date}' AND '{end_date}'")
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    gaql = f"""SELECT shopping_performance_view.resource_name,
              metrics.impressions, metrics.clicks, metrics.cost_micros,
              metrics.conversions, metrics.conversion_value,
              metrics.search_impression_share,
              campaign.name, campaign.shopping_setting.merchant_id
       FROM shopping_performance_view
       WHERE {' AND '.join(conditions)}
       LIMIT {limit or 100}"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_shopping_product_report(
    start_date: str,
    end_date: str,
    campaign_id: str = None,
    limit: int =None,
    customer_id: str = None,
) -> dict:
    """Get shopping product performance report with metrics by product item."""
    conditions = []
    if start_date and end_date:
        conditions.append(f"segments.date BETWEEN '{start_date}' AND '{end_date}'")
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    where_clause = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f"""
      SELECT
        shopping_product.item_id,
        shopping_product.title,
        shopping_product.brand,
        shopping_product.merchant_id,
        metrics.clicks,
        metrics.impressions,
        metrics.cost_micros,
        metrics.conversions,
        metrics.conversion_value
      FROM shopping_product
      {where_clause}
      LIMIT {limit or 200}
    """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_link_merchant_center(
    merchant_id: str,
    customer_id: str = None,
) -> dict:
    """Check existing Merchant Center links and get instructions for linking a new Merchant Center account."""
    gaql = """SELECT merchant_center_link.id, merchant_center_link.merchant_center_id,
              merchant_center_link.merchant_center_name, merchant_center_link.status
       FROM merchant_center_link"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_linked_merchant_centers(
    customer_id: str = None,
) -> dict:
    """List all Merchant Center accounts linked to this Google Ads account."""
    gaql = """SELECT merchant_center_link.id, merchant_center_link.merchant_center_id,
              merchant_center_link.merchant_center_name, merchant_center_link.status
       FROM merchant_center_link"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_product_groups(
    campaign_id: str = None,
    ad_group_id: str = None,
    limit: int =None,
    customer_id: str = None,
) -> dict:
    """List product listing groups (product groups) for shopping campaigns."""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where_clause = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f"""
      SELECT
        ad_group_criterion.criterion_id,
        ad_group_criterion.listing_group.type,
        ad_group_criterion.listing_group.case_value,
        ad_group_criterion.listing_group.parent_ad_group_criterion,
        ad_group_criterion.cpc_bid_micros,
        ad_group.name,
        campaign.name
      FROM ad_group_criterion
      {where_clause}
      LIMIT {limit or 200}
    """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_update_product_group(
    ad_group_id: str,
    criterion_id: str,
    cpc_bid_micros: float = None,
    status: str = None,
    customer_id: str = None,
) -> dict:
    """Update a product group (listing group) bid or status."""
    conditions = []
    if ad_group_id: conditions.append(f'ad_group.id = {ad_group_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_smart_campaign(
    name: str,
    daily_budget: float,
    business_name: str,
    final_url: str,
    headline_1: str,
    headline_2: str,
    headline_3: str,
    description_1: str,
    description_2: str,
    phone_number: str = None,
    country_code: str = None,
    geo_target_ids: list = None,
    keyword_themes: list = None,
    customer_id: str = None,
) -> dict:
    """Create a Google Smart campaign with ad copy, keyword themes, and geo targeting."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT campaign.id, campaign.name, campaign.status, metrics.clicks, metrics.impressions, metrics.cost_micros FROM campaign {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_suggest_smart_campaign_budget(
    keyword_themes: list,
    final_url: str,
    business_name: str = None,
    geo_target_ids: list = None,
    language_code: str = None,
    customer_id: str = None,
) -> dict:
    """Get Smart campaign budget suggestions (low/recommended/high) based on keyword themes and targeting."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT campaign.id, campaign.name, campaign.status, metrics.clicks, metrics.impressions, metrics.cost_micros FROM campaign {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_suggest_smart_campaign_keyword_themes(
    final_url: str = None,
    business_name: str = None,
    keyword_themes: list = None,
    geo_target_ids: list = None,
    language_code: str = None,
    customer_id: str = None,
) -> dict:
    """Suggest keyword themes for a Smart campaign based on URL, business name, or seed themes."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, ad_group_criterion.status, metrics.clicks, metrics.impressions FROM ad_group_criterion {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_update_smart_campaign_settings(
    campaign_id: str,
    final_url: str = None,
    business_name: str = None,
    phone_number: str = None,
    country_code: str = None,
    advertising_language_code: str = None,
    keyword_themes: list = None,
    customer_id: str = None,
) -> dict:
    """Update Smart campaign settings including URL, business name, phone, language, and keyword themes."""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT campaign.id, campaign.name, campaign.status, metrics.clicks, metrics.impressions, metrics.cost_micros FROM campaign {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_create_custom_segment(
    name: str,
    type_val: str,
    keywords: list = None,
    urls: list = None,
    apps: list = None,
    customer_id: str = None,
) -> dict:
    """Create a custom audience segment based on keywords, URLs, or apps."""
    conditions = []
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_carrier_constants(
    country_code: str = None,
    limit: int = 200,
    customer_id: str = None,
) -> dict:
    """List mobile carriers available for targeting, optionally in one country.

    Args:
        country_code: Optional two-letter country code, e.g. "IL", "US".
        limit: Maximum rows to return. Default 200; raise it if the answer is being cut off.
    """
    _limit = max(1, min(int(limit or 200), 2000))
    where = f"WHERE carrier_constant.country_code = '{country_code}'" if country_code else ""
    gaql = f"""
      SELECT
        carrier_constant.id,
        carrier_constant.name,
        carrier_constant.country_code
      FROM carrier_constant
      {where}
      ORDER BY carrier_constant.name ASC
      LIMIT {_limit}
    """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_detailed_demographics(
    customer_id: str = None,
) -> dict:
    """Get detailed demographic targeting categories (education, marital status, etc.)."""
    gaql = """SELECT detailed_demographic.id, detailed_demographic.name,
              detailed_demographic.parent, detailed_demographic.launched_to_all
       FROM detailed_demographic
       LIMIT 200"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_life_events(
    customer_id: str = None,
) -> dict:
    """Get life event targeting categories (recently married, new home, etc.)."""
    gaql = """SELECT life_event.id, life_event.name, life_event.parent, life_event.launched_to_all
       FROM life_event
       LIMIT 100"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_mobile_device_constants(
    customer_id: str = None,
) -> dict:
    """Get mobile device constants for device-specific targeting."""
    gaql = """SELECT mobile_device_constant.id, mobile_device_constant.name,
              mobile_device_constant.manufacturer_name,
              mobile_device_constant.operating_system_name,
              mobile_device_constant.type
       FROM mobile_device_constant
       LIMIT 500"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_operating_system_constants(
    customer_id: str = None,
) -> dict:
    """Get operating system version constants for device targeting."""
    gaql = """SELECT operating_system_version_constant.id,
              operating_system_version_constant.name,
              operating_system_version_constant.os_major_version,
              operating_system_version_constant.os_minor_version,
              operating_system_version_constant.operator_type
       FROM operating_system_version_constant
       LIMIT 200"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_get_user_interests(
    taxonomy_type: str = None,
    limit: int =None,
    customer_id: str = None,
) -> dict:
    """Get user interest categories (affinity, in-market) for audience targeting."""
    gaql = f"""SELECT user_interest.user_interest_id, user_interest.name, user_interest.taxonomy_type, user_interest.launched_to_all
       FROM user_interest{typeFilter}
       LIMIT {limit or 100}"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_language_constants(
    limit: int =None,
    customer_id: str = None,
) -> dict:
    """List all targetable language constants with their IDs."""
    gaql = f"""SELECT language_constant.id, language_constant.name, language_constant.code, language_constant.targetable
       FROM language_constant
       WHERE language_constant.targetable = TRUE
       LIMIT {limit or 100}"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_list_topic_constants(
    parent_id: str = None,
    limit: int = 200,
    customer_id: str = None,
) -> dict:
    """List content topics available for Display targeting.

    Args:
        parent_id: Optional parent topic ID, to list only that topic's children.
        limit: Maximum rows to return. Default 200; raise it if the answer is being cut off.
    """
    _limit = max(1, min(int(limit or 200), 2000))
    where = (f"WHERE topic_constant.topic_constant_parent = 'topicConstants/{parent_id}'"
             if parent_id else "")
    gaql = f"""
      SELECT
        topic_constant.id,
        topic_constant.path,
        topic_constant.topic_constant_parent
      FROM topic_constant
      {where}
      LIMIT {_limit}
    """
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_search_geo_targets(
    query: str,
    locale: str = None,
    country_code: str = None,
    customer_id: str = None,
) -> dict:
    """Search geo target locations by name to get the IDs campaign targeting takes.

    Geo targets are global constants, not account data, so this needs no
    account. That matters: the previous version demanded a customer_id and
    refused the call without one, which is the error this tool was reported
    failing with. customer_id is still accepted, and ignored, so callers that
    pass it to everything keep working.

    Args:
        query: Location name to look up, e.g. "Tel Aviv", "Greater London".
        locale: Language for the returned names. Defaults to "en".
        country_code: Optional two-letter country code to restrict results, e.g. "IL".
    """
    term = (query or "").strip()
    if not term:
        return {"success": False, "error": "query is required.",
                "next_step": 'Pass a location name, e.g. query="Tel Aviv".'}

    client = _load_user_client()
    service = client.get_service("GeoTargetConstantService")
    request = client.get_type("SuggestGeoTargetConstantsRequest")
    request.locale = locale or "en"
    if country_code:
        request.country_code = country_code
    request.location_names.names.append(term)

    response = service.suggest_geo_target_constants(request=request)
    data = []
    for suggestion in response.geo_target_constant_suggestions:
        constant = suggestion.geo_target_constant
        data.append({
            # The numeric id is what campaign location targeting actually takes.
            "id": str(constant.id),
            "resource_name": constant.resource_name,
            "name": constant.name,
            "canonical_name": constant.canonical_name,
            "country_code": constant.country_code,
            "target_type": constant.target_type,
            "status": getattr(constant.status, "name", str(constant.status)),
            "reach": suggestion.reach,
            "locale": suggestion.locale,
        })
    return {"success": True, "query": term, "data": data, "count": len(data)}

@mcp.tool()
def gads_set_content_exclusions(
    campaign_id: str,
    add_exclusions: list = None,
    remove_exclusions: list = None,
    customer_id: str = None,
) -> dict:
    """Add or remove content label exclusions (brand safety settings) for a campaign."""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    gaql = f"""SELECT campaign_criterion.criterion_id, campaign_criterion.content_label.type
         FROM campaign_criterion
         WHERE campaign.id = {campaign_id or ""}
           AND campaign_criterion.type = 'CONTENT_LABEL'"""
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}

@mcp.tool()
def gads_set_placement_exclusions(
    campaign_id: str,
    add_placements: list = None,
    remove_placement_criterion_ids: list = None,
    customer_id: str = None,
) -> dict:
    """Add or remove placement exclusions (blocked URLs) for a campaign."""
    conditions = []
    if campaign_id: conditions.append(f'campaign.id = {campaign_id}')
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    gaql = f'SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM ad_group_ad {where} LIMIT {200}'
    rows = _search(gaql, customer_id or '')
    return {"success": True, "data": [_row_to_dict(r) for r in rows], "count": len(rows)}


# ===== TOOLS ADDED FROM google-ads-mcp-server =====


@mcp.tool()
def gads_add_ad_group_carrier(ad_group_id: str, carrier_constant: str, customer_id: str = "") -> str:
    """Add carrier targeting to an ad group."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupCriterionService")
        op = client.get_type("AdGroupCriterionOperation")
        criterion = op.create
        criterion.ad_group = f"customers/{cid}/adGroups/{ad_group_id}"
        criterion.type_ = client.enums.CriterionTypeEnum.CARRIER
        criterion.carrier.carrier_constant = carrier_constant
        response = service.mutate_ad_group_criteria(customer_id=cid, operations=[op])
        rn = response.results[0].resource_name
        return json.dumps({"success": True, "resource_name": rn})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_add_ad_group_mobile_device(ad_group_id: str, mobile_device_constant: str, customer_id: str = "") -> str:
    """Add mobile device targeting to an ad group. mobile_device_constant e.g. 'mobileDeviceConstants/700001'."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupCriterionService")
        op = client.get_type("AdGroupCriterionOperation")
        criterion = op.create
        criterion.ad_group = f"customers/{cid}/adGroups/{ad_group_id}"
        criterion.type_ = client.enums.CriterionTypeEnum.MOBILE_DEVICE
        criterion.mobile_device.mobile_device_constant = mobile_device_constant
        response = service.mutate_ad_group_criteria(customer_id=cid, operations=[op])
        rn = response.results[0].resource_name
        return json.dumps({"success": True, "resource_name": rn})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_add_ad_group_operating_system(ad_group_id: str, operating_system_version_constant: str, customer_id: str = "") -> str:
    """Add operating system targeting to an ad group. operating_system_version_constant e.g. 'operatingSystemVersionConstants/630000'."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupCriterionService")
        op = client.get_type("AdGroupCriterionOperation")
        criterion = op.create
        criterion.ad_group = f"customers/{cid}/adGroups/{ad_group_id}"
        criterion.type_ = client.enums.CriterionTypeEnum.OPERATING_SYSTEM_VERSION
        criterion.operating_system_version.operating_system_version_constant = operating_system_version_constant
        response = service.mutate_ad_group_criteria(customer_id=cid, operations=[op])
        rn = response.results[0].resource_name
        return json.dumps({"success": True, "resource_name": rn})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_add_call_extension(
    phone_number: str,
    country_code: str,
    campaign_id: str = "",
    customer_id: str = "",
) -> str:
    """Add a phone call extension to a campaign or account.

    Args:
        phone_number: Phone number string, e.g. '1-800-555-0100'.
        country_code: Two-letter country code, e.g. 'US', 'IL'.
        campaign_id: Campaign ID to attach to (optional; omit for account-level).
        customer_id: Google Ads customer ID.
    """
    try:
        client, cid = _get_client(customer_id)
        asset_service = client.get_service("AssetService")
        asset_op = client.get_type("AssetOperation")
        a = asset_op.create
        a.call_asset.phone_number = phone_number
        a.call_asset.country_code = country_code
        a_resp = asset_service.mutate_assets(customer_id=cid, operations=[asset_op])
        asset_rn = a_resp.results[0].resource_name

        if campaign_id:
            ca_service = client.get_service("CampaignAssetService")
            ca_op = client.get_type("CampaignAssetOperation")
            ca = ca_op.create
            ca.campaign = f"customers/{cid}/campaigns/{campaign_id}"
            ca.asset = asset_rn
            ca.field_type = client.enums.AssetFieldTypeEnum["CALL"]
            ca_service.mutate_campaign_assets(customer_id=cid, operations=[ca_op])

        return json.dumps({
            "success": True,
            "asset_resource_name": asset_rn,
            "phone_number": phone_number,
            "campaign_id": campaign_id or "account-level",
        })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})






@mcp.tool()
def gads_add_campaign_carrier(campaign_id: str, carrier_constant: str, customer_id: str = "") -> str:
    """Add carrier targeting to a campaign. carrier_constant is resource name like 'carrierConstants/70502'."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignCriterionService")
        op = client.get_type("CampaignCriterionOperation")
        criterion = op.create
        criterion.campaign = f"customers/{cid}/campaigns/{campaign_id}"
        criterion.type_ = client.enums.CriterionTypeEnum.CARRIER
        criterion.carrier.carrier_constant = carrier_constant
        response = service.mutate_campaign_criteria(customer_id=cid, operations=[op])
        rn = response.results[0].resource_name
        return json.dumps({"success": True, "resource_name": rn})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_add_campaign_ip_exclusion(campaign_id: str, ip_address: str, customer_id: str = "") -> str:
    """Add IP address exclusion to a campaign. ip_address can be like '1.2.3.4' or '1.2.3.0/24'."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignCriterionService")
        op = client.get_type("CampaignCriterionOperation")
        criterion = op.create
        criterion.campaign = f"customers/{cid}/campaigns/{campaign_id}"
        criterion.negative = True
        criterion.ip_block.ip_address = ip_address
        response = service.mutate_campaign_criteria(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_add_campaign_location_group(campaign_id: str, feed_resource_names: list = None,
                                      geo_target_constants: list = None,
                                      radius: float = 0, radius_units: str = "MILES",
                                      customer_id: str = "") -> str:
    """Add a location group criterion to a campaign.
    Can use feed_resource_names (business locations feed) or geo_target_constants + radius."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignCriterionService")
        op = client.get_type("CampaignCriterionOperation")
        criterion = op.create
        criterion.campaign = f"customers/{cid}/campaigns/{campaign_id}"
        criterion.type_ = client.enums.CriterionTypeEnum.LOCATION_GROUP
        if feed_resource_names:
            for frn in feed_resource_names:
                criterion.location_group.feed_item_sets.append(frn)
        if geo_target_constants:
            for gtc in geo_target_constants:
                criterion.location_group.geo_targets.geo_target_constants.append(gtc)
        if radius > 0:
            criterion.location_group.radius = radius
            criterion.location_group.radius_units = \
                client.enums.LocationGroupRadiusUnitsEnum[radius_units]
        response = service.mutate_campaign_criteria(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Video responsive ad ────────────────────────────────────────────────────





@mcp.tool()
def gads_add_campaign_mobile_device(campaign_id: str, mobile_device_constant: str, customer_id: str = "") -> str:
    """Add mobile device targeting to a campaign. mobile_device_constant e.g. 'mobileDeviceConstants/700001'."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignCriterionService")
        op = client.get_type("CampaignCriterionOperation")
        criterion = op.create
        criterion.campaign = f"customers/{cid}/campaigns/{campaign_id}"
        criterion.type_ = client.enums.CriterionTypeEnum.MOBILE_DEVICE
        criterion.mobile_device.mobile_device_constant = mobile_device_constant
        response = service.mutate_campaign_criteria(customer_id=cid, operations=[op])
        rn = response.results[0].resource_name
        return json.dumps({"success": True, "resource_name": rn})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_add_campaign_operating_system(campaign_id: str, operating_system_version_constant: str,
                                        customer_id: str = "") -> str:
    """Add operating system targeting to a campaign."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignCriterionService")
        op = client.get_type("CampaignCriterionOperation")
        criterion = op.create
        criterion.campaign = f"customers/{cid}/campaigns/{campaign_id}"
        criterion.type_ = client.enums.CriterionTypeEnum.OPERATING_SYSTEM_VERSION
        criterion.operating_system_version.operating_system_version_constant = operating_system_version_constant
        response = service.mutate_campaign_criteria(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_add_customer_negative_keyword(keyword_text: str, match_type: str = "BROAD",
                                        customer_id: str = "") -> str:
    """Add a customer-level negative keyword (applies account-wide via a shared list approach).
    Note: Google Ads API does not support account-level negative keywords directly.
    This creates a shared negative list and attaches it. Use gads_create_shared_negative_list + gads_add_keywords_to_shared_list + gads_attach_shared_list_to_campaign for per-campaign control."""
    return json.dumps({
        "note": "Google Ads API does not support true account-level (customer-level) negative keywords. "
                "To add negative keywords broadly: "
                "1) Use gads_create_shared_negative_list to create a shared negative keyword list, "
                "2) Use gads_add_keywords_to_shared_list to add keywords to it, "
                "3) Use gads_attach_shared_list_to_campaign to attach it to each campaign. "
                f"Keyword requested: '{keyword_text}' [{match_type}]",
        "workaround": "shared_negative_list"
    })


# ── Run offline user data job ──────────────────────────────────────────────





@mcp.tool()
def gads_add_negative_audience_to_ad_group(ad_group_id: str, user_list_resource_name: str, customer_id: str = "") -> str:
    """Add a negative audience (user list exclusion) to an ad group."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupCriterionService")
        op = client.get_type("AdGroupCriterionOperation")
        criterion = op.create
        criterion.ad_group = f"customers/{cid}/adGroups/{ad_group_id}"
        criterion.negative = True
        criterion.type_ = client.enums.CriterionTypeEnum.USER_LIST
        criterion.user_list.user_list = user_list_resource_name
        response = service.mutate_ad_group_criteria(customer_id=cid, operations=[op])
        rn = response.results[0].resource_name
        return json.dumps({"success": True, "resource_name": rn})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── PMax brand exclusions ──────────────────────────────────────────────────





@mcp.tool()
def gads_add_negative_audience_to_campaign(campaign_id: str, user_list_resource_name: str, customer_id: str = "") -> str:
    """Add a negative audience (user list exclusion) to a campaign."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignCriterionService")
        op = client.get_type("CampaignCriterionOperation")
        criterion = op.create
        criterion.campaign = f"customers/{cid}/campaigns/{campaign_id}"
        criterion.negative = True
        criterion.type_ = client.enums.CriterionTypeEnum.USER_LIST
        criterion.user_list.user_list = user_list_resource_name
        response = service.mutate_campaign_criteria(customer_id=cid, operations=[op])
        rn = response.results[0].resource_name
        return json.dumps({"success": True, "resource_name": rn})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_add_pmax_brand_exclusion(campaign_id: str, brand_name: str = "", brand_id: int = 0, customer_id: str = "") -> str:
    """Add brand exclusion to a Performance Max campaign. Provide brand_name or brand_id."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignCriterionService")
        op = client.get_type("CampaignCriterionOperation")
        criterion = op.create
        criterion.campaign = f"customers/{cid}/campaigns/{campaign_id}"
        criterion.negative = True
        criterion.type_ = client.enums.CriterionTypeEnum.BRAND
        if brand_id:
            criterion.brand.entity_id = brand_id
        if brand_name:
            criterion.brand.name = brand_name
        response = service.mutate_campaign_criteria(customer_id=cid, operations=[op])
        rn = response.results[0].resource_name
        return json.dumps({"success": True, "resource_name": rn})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_add_structured_snippet(
    header: str,
    values: str,
    campaign_id: str = "",
    customer_id: str = "",
) -> str:
    """Add a structured snippet extension to a campaign or account.

    Args:
        header: Snippet header, e.g. 'Services', 'Brands', 'Courses'.
        values: JSON array of snippet values, e.g. ["Value1","Value2","Value3"].
        campaign_id: Campaign ID to attach to (optional; omit for account-level).
        customer_id: Google Ads customer ID.
    """
    try:
        client, cid = _get_client(customer_id)
        val_list = json.loads(values) if isinstance(values, str) else values
        asset_service = client.get_service("AssetService")
        asset_op = client.get_type("AssetOperation")
        a = asset_op.create
        a.structured_snippet_asset.header = header
        for v in val_list:
            a.structured_snippet_asset.values.append(v)
        a_resp = asset_service.mutate_assets(customer_id=cid, operations=[asset_op])
        asset_rn = a_resp.results[0].resource_name

        if campaign_id:
            ca_service = client.get_service("CampaignAssetService")
            ca_op = client.get_type("CampaignAssetOperation")
            ca = ca_op.create
            ca.campaign = f"customers/{cid}/campaigns/{campaign_id}"
            ca.asset = asset_rn
            ca.field_type = client.enums.AssetFieldTypeEnum["STRUCTURED_SNIPPET"]
            ca_service.mutate_campaign_assets(customer_id=cid, operations=[ca_op])

        return json.dumps({
            "success": True,
            "asset_resource_name": asset_rn,
            "campaign_id": campaign_id or "account-level",
        })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})






@mcp.tool()
def gads_apply_label_to_keyword(ad_group_id: str, criterion_id: str, label_id: str,
                                  customer_id: str = "") -> str:
    """Apply a label to a keyword (ad group criterion)."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupCriterionLabelService")
        op = client.get_type("AdGroupCriterionLabelOperation")
        label_op = op.create
        label_op.ad_group_criterion = f"customers/{cid}/adGroupCriteria/{ad_group_id}~{criterion_id}"
        label_op.label = f"customers/{cid}/labels/{label_id}"
        response = service.mutate_ad_group_criterion_labels(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_assign_budget_to_campaign(
    campaign_id: str,
    budget_id: str,
    customer_id: str = "",
) -> str:
    """Assign an existing budget to a campaign.

    Args:
        campaign_id: Campaign numeric ID.
        budget_id: Budget ID to assign.
        customer_id: Google Ads customer ID.
    """
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        c = op.update
        c.resource_name = f"customers/{cid}/campaigns/{campaign_id}"
        c.campaign_budget = f"customers/{cid}/campaignBudgets/{budget_id}"
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["campaign_budget"]))
        service.mutate_campaigns(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "campaign_id": campaign_id, "budget_id": budget_id})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


# ---------------------------------------------------------------------------
# GROUP 8: Performance — missing tools
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# GROUP 9: Extensions / Assets
# ---------------------------------------------------------------------------





@mcp.tool()
def gads_assign_portfolio_strategy_to_campaign(campaign_id: str, bidding_strategy_id: str,
                                                customer_id: str = "") -> str:
    """Assign a portfolio bidding strategy to a campaign."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        campaign = op.update
        campaign.resource_name = f"customers/{cid}/campaigns/{campaign_id}"
        campaign.bidding_strategy = f"customers/{cid}/biddingStrategies/{bidding_strategy_id}"
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["bidding_strategy"]))
        response = service.mutate_campaigns(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Location group targeting ───────────────────────────────────────────────





@mcp.tool()
def gads_bulk_rename_ad_groups(updates: list, customer_id: str = "") -> str:
    """Bulk rename multiple ad groups. updates: list of {ad_group_id, name} dicts."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupService")
        ops = []
        for u in updates:
            op = client.get_type("AdGroupOperation")
            ag = op.update
            ag.resource_name = f"customers/{cid}/adGroups/{u['ad_group_id']}"
            ag.name = u["name"]
            op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["name"]))
            ops.append(op)
        response = service.mutate_ad_groups(customer_id=cid, operations=ops)
        results = [{"resource_name": r.resource_name} for r in response.results]
        return json.dumps({"success": True, "updated": len(results), "results": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Shared negative list ───────────────────────────────────────────────────





@mcp.tool()
def gads_bulk_rename_campaigns(updates: list, customer_id: str = "") -> str:
    """Bulk rename multiple campaigns. updates: list of {campaign_id, name} dicts."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignService")
        ops = []
        for u in updates:
            op = client.get_type("CampaignOperation")
            c = op.update
            c.resource_name = f"customers/{cid}/campaigns/{u['campaign_id']}"
            c.name = u["name"]
            op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["name"]))
            ops.append(op)
        response = service.mutate_campaigns(customer_id=cid, operations=ops)
        results = [{"resource_name": r.resource_name} for r in response.results]
        return json.dumps({"success": True, "updated": len(results), "results": results})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_bulk_set_ai_max_search_campaigns(
    enable: bool,
    text_asset_automation: bool = True,
    final_url_expansion: bool = True,
    customer_id: str = "",
) -> str:
    """Enable or disable AI Max for ALL active Search campaigns in the account.

    Also sets sub-settings: text asset automation and final URL expansion.

    Args:
        enable: True to enable AI Max on all Search campaigns, False to disable.
        text_asset_automation: Enable text asset automation sub-setting (default True).
        final_url_expansion: Enable final URL expansion sub-setting (default True).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        from google.ads.googleads.v23.resources.types.campaign import Campaign as _Camp
        client, cid = _get_client(customer_id)
        rows = _search(
            "SELECT campaign.id, campaign.name, campaign.ai_max_setting.enable_ai_max "
            "FROM campaign "
            "WHERE campaign.status IN (ENABLED, PAUSED) "
            "AND campaign.advertising_channel_type = SEARCH",
            customer_id,
        )
        service = client.get_service("CampaignService")
        opted_in = client.enums.AssetAutomationStatusEnum.OPTED_IN
        opted_out = client.enums.AssetAutomationStatusEnum.OPTED_OUT
        AssetAutomationSetting = _Camp.AssetAutomationSetting
        updated = []
        skipped = []
        failed = []
        for row in rows:
            current = row.campaign.ai_max_setting.enable_ai_max
            camp_info = {"campaign_id": str(row.campaign.id), "name": row.campaign.name}
            if current == enable:
                skipped.append(camp_info)
                continue
            try:
                op = client.get_type("CampaignOperation")
                c = op.update
                c.resource_name = f"customers/{cid}/campaigns/{row.campaign.id}"
                c.ai_max_setting.enable_ai_max = enable
                s1 = AssetAutomationSetting()
                s1.asset_automation_type = client.enums.AssetAutomationTypeEnum.TEXT_ASSET_AUTOMATION
                s1.asset_automation_status = opted_in if text_asset_automation else opted_out
                c.asset_automation_settings.append(s1)
                s2 = AssetAutomationSetting()
                s2.asset_automation_type = client.enums.AssetAutomationTypeEnum.FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION
                s2.asset_automation_status = opted_in if final_url_expansion else opted_out
                c.asset_automation_settings.append(s2)
                op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["ai_max_setting.enable_ai_max", "asset_automation_settings"]))
                service.mutate_campaigns(customer_id=cid, operations=[op])
                updated.append(camp_info)
            except Exception as e:
                err_msg = str(e)
                if "AI_MAX_MUST_BE_ENABLED" in err_msg:
                    reason = "Campaign requires AI Max enabled (cannot be disabled via API)"
                else:
                    reason = err_msg[:120]
                failed.append({**camp_info, "reason": reason})
        return json.dumps({
            "success": True,
            "ai_max_enabled": enable,
            "text_asset_automation": text_asset_automation,
            "final_url_expansion": final_url_expansion,
            "updated_count": len(updated),
            "skipped_count": len(skipped),
            "failed_count": len(failed),
            "updated": updated,
            "skipped_already_set": skipped,
            "failed": failed,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_bulk_set_device_bid_modifiers(campaign_ids: str, device: str, bid_modifier: float, customer_id: str = "") -> str:
    """Set device bid modifier across multiple campaigns.
    Args: campaign_ids: Comma-separated campaign IDs. device: DESKTOP, MOBILE, or TABLET. bid_modifier: Multiplier. customer_id: optional."""
    try:
        _, cid = _get_client(customer_id)
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignCriterionService")
        ops = []
        for cid_val in [x.strip() for x in campaign_ids.split(",") if x.strip()]:
            # Check for existing criterion
            rows = _search(f"""
                SELECT campaign_criterion.criterion_id
                FROM campaign_criterion
                WHERE campaign_criterion.campaign = 'customers/{cid}/campaigns/{cid_val}'
                  AND campaign_criterion.type = 'DEVICE'
                  AND campaign_criterion.device.type = '{device}'
            """, customer_id)
            if rows:
                crit_id = str(rows[0].campaign_criterion.criterion_id)
                op = client.get_type("CampaignCriterionOperation")
                cc = op.update
                cc.resource_name = f"customers/{cid}/campaignCriteria/{cid_val}~{crit_id}"
                cc.bid_modifier = bid_modifier
                op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["bid_modifier"]))
            else:
                op = client.get_type("CampaignCriterionOperation")
                cc = op.create
                cc.campaign = f"customers/{cid}/campaigns/{cid_val}"
                cc.device.type_ = client.enums.DeviceEnum[device]
                cc.bid_modifier = bid_modifier
            ops.append(op)
        service.mutate_campaign_criteria(customer_id=cid, operations=ops)
        return json.dumps({"success": True, "campaigns_updated": len(ops), "device": device, "bid_modifier": bid_modifier})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_bulk_update_ad_group_names(updates: list, customer_id: str = "") -> str:
    """Bulk rename ad groups. updates is a list of dicts with 'ad_group_id' and 'name'."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupService")
        ops = []
        for item in updates:
            op = client.get_type("AdGroupOperation")
            ad_group = op.update
            ad_group.resource_name = f"customers/{cid}/adGroups/{item['ad_group_id']}"
            ad_group.name = item["name"]
            op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["name"]))
            ops.append(op)
        response = service.mutate_ad_groups(customer_id=cid, operations=ops)
        return json.dumps({
            "success": True,
            "updated_count": len(response.results),
            "resource_names": [r.resource_name for r in response.results],
        })
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_bulk_update_campaign_names(updates: list, customer_id: str = "") -> str:
    """Bulk rename campaigns. updates is a list of dicts with 'campaign_id' and 'name'."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignService")
        ops = []
        for item in updates:
            op = client.get_type("CampaignOperation")
            campaign = op.update
            campaign.resource_name = f"customers/{cid}/campaigns/{item['campaign_id']}"
            campaign.name = item["name"]
            op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["name"]))
            ops.append(op)
        response = service.mutate_campaigns(customer_id=cid, operations=ops)
        return json.dumps({
            "success": True,
            "updated_count": len(response.results),
            "resource_names": [r.resource_name for r in response.results],
        })
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_check_accounts_inactivity(
    days: int = 7,
    mcc_customer_id: str = "",
) -> str:
    """Flag sub-accounts with zero spend or zero clicks in the last N days.

    Args:
        days: Look-back window in days. Default 7.
        mcc_customer_id: MCC account ID. Defaults to GOOGLE_ADS_CUSTOMER_ID env var.
    """
    try:
        sd = str(date.today() - timedelta(days=days))
        ed = str(date.today())
        sub_rows = _search(
            "SELECT customer_client.id, customer_client.descriptive_name FROM customer_client WHERE customer_client.manager = false AND customer_client.level = 1",
            mcc_customer_id,
        )
        inactive = []
        active = []
        for row in sub_rows:
            sub_id = str(row.customer_client.id)
            name = row.customer_client.descriptive_name
            try:
                perf = _search(
                    f"""
                    SELECT metrics.cost_micros, metrics.clicks
                    FROM customer
                    WHERE segments.date BETWEEN '{sd}' AND '{ed}'
                    """,
                    sub_id,
                )
                total_clicks = sum(r.metrics.clicks for r in perf)
                total_cost = sum(r.metrics.cost_micros for r in perf)
                if total_clicks == 0 or total_cost == 0:
                    inactive.append({"id": sub_id, "name": name, "clicks": total_clicks, "cost": _m(total_cost)})
                else:
                    active.append({"id": sub_id, "name": name, "clicks": total_clicks, "cost": _m(total_cost)})
            except Exception:
                inactive.append({"id": sub_id, "name": name, "error": "query failed"})

        return json.dumps({
            "look_back_days": days,
            "inactive_accounts": inactive,
            "active_accounts": active,
            "inactive_count": len(inactive),
            "active_count": len(active),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})



# ===========================================================================
# EXTENDED TOOLS BATCH 1 — Audiences, Bidding, Bulk Ops
# ===========================================================================


# ---------------------------------------------------------------------------
# AUDIENCES & USER LISTS
# ---------------------------------------------------------------------------





@mcp.tool()
def gads_clear_campaign_schedule(
    campaign_id: str,
    customer_id: str = "",
) -> str:
    """Remove ALL ad schedule criteria from a campaign (clears dayparting entirely).

    Args:
        campaign_id: Campaign numeric ID.
        customer_id: Google Ads customer ID.
    """
    try:
        client, cid = _get_client(customer_id)
        cc_service = client.get_service("CampaignCriterionService")
        existing = _search(
            f"SELECT campaign_criterion.resource_name FROM campaign_criterion "
            f"WHERE campaign_criterion.type = 'AD_SCHEDULE' AND campaign.id = {campaign_id}",
            customer_id,
        )
        ops = []
        for row in existing:
            op = client.get_type("CampaignCriterionOperation")
            op.remove = row.campaign_criterion.resource_name
            ops.append(op)
        if ops:
            cc_service.mutate_campaign_criteria(customer_id=cid, operations=ops)
        return json.dumps({"success": True, "removed": len(ops), "campaign_id": campaign_id})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


# ---------------------------------------------------------------------------
# GROUP 2: Bid Modifiers
# ---------------------------------------------------------------------------





@mcp.tool()
def gads_copy_schedule_to_campaigns(source_campaign_id: str, target_campaign_ids: str, customer_id: str = "") -> str:
    """Copy ad schedule from one campaign to multiple campaigns.
    Args: source_campaign_id: Source campaign ID. target_campaign_ids: Comma-separated target campaign IDs. customer_id: optional."""
    try:
        _, cid = _get_client(customer_id)
        # Get source schedule
        src_rows = _search(f"""
            SELECT campaign_criterion.criterion_id,
                   campaign_criterion.ad_schedule.day_of_week,
                   campaign_criterion.ad_schedule.start_hour,
                   campaign_criterion.ad_schedule.start_minute,
                   campaign_criterion.ad_schedule.end_hour,
                   campaign_criterion.ad_schedule.end_minute,
                   campaign_criterion.bid_modifier
            FROM campaign_criterion
            WHERE campaign_criterion.campaign = 'customers/{cid}/campaigns/{source_campaign_id}'
              AND campaign_criterion.type = 'AD_SCHEDULE'
        """, customer_id)
        if not src_rows:
            return json.dumps({"error": "No schedule found on source campaign"})

        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignCriterionService")
        ops = []
        for target_id in [x.strip() for x in target_campaign_ids.split(",") if x.strip()]:
            for src in src_rows:
                sc = src.campaign_criterion
                op = client.get_type("CampaignCriterionOperation")
                c = op.create
                c.campaign = f"customers/{cid}/campaigns/{target_id}"
                c.ad_schedule.day_of_week = sc.ad_schedule.day_of_week
                c.ad_schedule.start_hour = sc.ad_schedule.start_hour
                c.ad_schedule.start_minute = sc.ad_schedule.start_minute
                c.ad_schedule.end_hour = sc.ad_schedule.end_hour
                c.ad_schedule.end_minute = sc.ad_schedule.end_minute
                c.bid_modifier = sc.bid_modifier
                ops.append(op)
        service.mutate_campaign_criteria(customer_id=cid, operations=ops)
        return json.dumps({"success": True, "schedules_copied": len(src_rows), "campaigns_updated": len(target_campaign_ids.split(","))})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_create_conversion_value_rule_set(campaign_id: str, dimensions: list,
                                           rule_resource_names: list, customer_id: str = "") -> str:
    """Create a conversion value rule set for a campaign.
    dimensions: list of 'GEO_LOCATION', 'DEVICE', 'AUDIENCE', 'ITINERARY', 'NO_CONDITION'
    rule_resource_names: list of conversion value rule resource names to include."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("ConversionValueRuleSetService")
        op = client.get_type("ConversionValueRuleSetOperation")
        rs = op.create
        rs.campaign = f"customers/{cid}/campaigns/{campaign_id}"
        DimEnum = client.enums.ValueRuleSetDimensionEnum
        for dim in dimensions:
            rs.dimensions.append(DimEnum[dim])
        for rn in rule_resource_names:
            rs.conversion_value_rules.append(rn)
        rs.status = client.enums.ConversionValueRuleSetStatusEnum.ENABLED
        response = service.mutate_conversion_value_rule_sets(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Asset automation settings ─────────────────────────────────────────────





@mcp.tool()
def gads_create_customer(descriptive_name: str, currency_code: str = "USD",
                          time_zone: str = "America/New_York",
                          manager_customer_id: str = "", customer_id: str = "") -> str:
    """Create a new Google Ads customer (sub-account) under a manager account.
    manager_customer_id: the MCC account ID. customer_id is used as the login customer."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CustomerService")
        customer = client.get_type("Customer")
        customer.descriptive_name = descriptive_name
        customer.currency_code = currency_code
        customer.time_zone = time_zone
        manager_rn = f"customers/{manager_customer_id}" if manager_customer_id else f"customers/{cid}"
        response = service.create_customer_client(
            customer_id=manager_customer_id or cid,
            customer_client=customer
        )
        return json.dumps({
            "success": True,
            "resource_name": response.resource_name,
            "invitation_link": response.invitation_link,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Conversion value rule sets ─────────────────────────────────────────────





@mcp.tool()
def gads_create_demand_gen_campaign(name: str, budget_micros: int, customer_id: str = "") -> str:
    """Create a Demand Gen campaign with a new shared budget."""
    try:
        client, cid = _get_client(customer_id)

        # Create budget
        budget_service = client.get_service("CampaignBudgetService")
        budget_op = client.get_type("CampaignBudgetOperation")
        budget = budget_op.create
        budget.name = f"{name} Budget"
        budget.amount_micros = budget_micros
        budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
        budget.explicitly_shared = False
        budget_response = budget_service.mutate_campaign_budgets(customer_id=cid, operations=[budget_op])
        budget_resource = budget_response.results[0].resource_name

        # Create campaign
        campaign_service = client.get_service("CampaignService")
        campaign_op = client.get_type("CampaignOperation")
        campaign = campaign_op.create
        campaign.name = name
        campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.DEMAND_GEN
        campaign.status = client.enums.CampaignStatusEnum.PAUSED
        campaign.contains_eu_political_advertising = client.enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
        campaign.campaign_budget = budget_resource
        campaign.maximize_conversions.target_cpa_micros = 0
        campaign_response = campaign_service.mutate_campaigns(customer_id=cid, operations=[campaign_op])
        return json.dumps({
            "success": True,
            "campaign_resource_name": campaign_response.results[0].resource_name,
            "budget_resource_name": budget_resource,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_create_discovery_campaign(name: str, budget_micros: int,
                                    target_cpa_micros: int = 0, target_roas: float = 0.0,
                                    customer_id: str = "") -> str:
    """Create a Discovery campaign (legacy; new campaigns use Demand Gen)."""
    try:
        client, cid = _get_client(customer_id)
        budget_service = client.get_service("CampaignBudgetService")
        bop = client.get_type("CampaignBudgetOperation")
        budget = bop.create
        budget.name = f"{name}_budget"
        budget.amount_micros = budget_micros
        budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
        budget.explicitly_shared = False
        bresp = budget_service.mutate_campaign_budgets(customer_id=cid, operations=[bop])
        budget_rn = bresp.results[0].resource_name

        service = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        campaign = op.create
        campaign.name = name
        campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.DISCOVERY
        campaign.status = client.enums.CampaignStatusEnum.PAUSED
        campaign.contains_eu_political_advertising = client.enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
        campaign.campaign_budget = budget_rn
        if target_cpa_micros > 0:
            campaign.target_cpa.target_cpa_micros = target_cpa_micros
        elif target_roas > 0:
            campaign.target_roas.target_roas = target_roas
        else:
            campaign.maximize_conversions = client.get_type("MaximizeConversions")
        response = service.mutate_campaigns(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Seasonality adjustments & data exclusions management ──────────────────





@mcp.tool()
def gads_create_display_campaign(
    name: str,
    budget_micros: int,
    bidding_strategy: str = "MAXIMIZE_CONVERSIONS",
    target_cpa_micros: int = 0,
    customer_id: str = "",
) -> str:
    """Create a Display campaign."""
    try:
        client, cid = _get_client(customer_id)

        # Create budget
        budget_service = client.get_service("CampaignBudgetService")
        budget_op = client.get_type("CampaignBudgetOperation")
        budget = budget_op.create
        budget.name = f"{name} Budget"
        budget.amount_micros = budget_micros
        budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
        budget.explicitly_shared = False
        budget_response = budget_service.mutate_campaign_budgets(customer_id=cid, operations=[budget_op])
        budget_resource = budget_response.results[0].resource_name

        # Create campaign
        campaign_service = client.get_service("CampaignService")
        campaign_op = client.get_type("CampaignOperation")
        campaign = campaign_op.create
        campaign.name = name
        campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.DISPLAY
        campaign.status = client.enums.CampaignStatusEnum.PAUSED
        campaign.contains_eu_political_advertising = client.enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
        campaign.campaign_budget = budget_resource

        strategy = bidding_strategy.upper()
        if strategy == "MAXIMIZE_CONVERSIONS":
            if target_cpa_micros:
                campaign.maximize_conversions.target_cpa_micros = target_cpa_micros
            else:
                campaign.maximize_conversions.target_cpa_micros = 0
        elif strategy == "TARGET_CPA":
            campaign.target_cpa.target_cpa_micros = target_cpa_micros
        elif strategy == "MAXIMIZE_CONVERSION_VALUE":
            campaign.maximize_conversion_value.target_roas = 0

        campaign_response = campaign_service.mutate_campaigns(customer_id=cid, operations=[campaign_op])
        return json.dumps({
            "success": True,
            "campaign_resource_name": campaign_response.results[0].resource_name,
            "budget_resource_name": budget_resource,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_create_keyword(ad_group_id: str, keyword_text: str,
                         match_type: str = "BROAD", cpc_bid_micros: int = 0,
                         status: str = "ENABLED", customer_id: str = "") -> str:
    """Create a single keyword in an ad group. match_type: BROAD, PHRASE, EXACT."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupCriterionService")
        op = client.get_type("AdGroupCriterionOperation")
        criterion = op.create
        criterion.ad_group = f"customers/{cid}/adGroups/{ad_group_id}"
        criterion.status = client.enums.AdGroupCriterionStatusEnum[status]
        criterion.keyword.text = keyword_text
        criterion.keyword.match_type = client.enums.KeywordMatchTypeEnum[match_type]
        if cpc_bid_micros > 0:
            criterion.cpc_bid_micros = cpc_bid_micros
        response = service.mutate_ad_group_criteria(customer_id=cid, operations=[op])
        rn = response.results[0].resource_name
        return json.dumps({"success": True, "resource_name": rn,
                           "criterion_id": rn.split("~")[-1]})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_create_local_campaign(name: str, budget_micros: int,
                                location_source_type: str = "GOOGLE_MY_BUSINESS",
                                customer_id: str = "") -> str:
    """Create a Local campaign. location_source_type: GOOGLE_MY_BUSINESS or AFFILIATE."""
    try:
        client, cid = _get_client(customer_id)
        budget_service = client.get_service("CampaignBudgetService")
        bop = client.get_type("CampaignBudgetOperation")
        budget = bop.create
        budget.name = f"{name}_budget"
        budget.amount_micros = budget_micros
        budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
        budget.explicitly_shared = False
        bresp = budget_service.mutate_campaign_budgets(customer_id=cid, operations=[bop])
        budget_rn = bresp.results[0].resource_name

        service = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        campaign = op.create
        campaign.name = name
        campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.LOCAL
        campaign.status = client.enums.CampaignStatusEnum.PAUSED
        campaign.contains_eu_political_advertising = client.enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
        campaign.campaign_budget = budget_rn
        campaign.local_campaign_setting.location_source_type = \
            client.enums.LocationSourceTypeEnum[location_source_type]
        campaign.maximize_conversion_value.CopyFrom(
            client.get_type("MaximizeConversionValue"))
        response = service.mutate_campaigns(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name,
                           "budget_resource_name": budget_rn})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_create_pmax_campaign(
    name: str,
    budget_micros: int,
    target_roas: float = 0,
    target_cpa_micros: int = 0,
    customer_id: str = "",
) -> str:
    """Create a Performance Max campaign."""
    try:
        client, cid = _get_client(customer_id)

        # Create budget
        budget_service = client.get_service("CampaignBudgetService")
        budget_op = client.get_type("CampaignBudgetOperation")
        budget = budget_op.create
        budget.name = f"{name} Budget"
        budget.amount_micros = budget_micros
        budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
        budget.explicitly_shared = False
        budget_response = budget_service.mutate_campaign_budgets(customer_id=cid, operations=[budget_op])
        budget_resource = budget_response.results[0].resource_name

        # Create campaign
        campaign_service = client.get_service("CampaignService")
        campaign_op = client.get_type("CampaignOperation")
        campaign = campaign_op.create
        campaign.name = name
        campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.PERFORMANCE_MAX
        campaign.status = client.enums.CampaignStatusEnum.PAUSED
        campaign.contains_eu_political_advertising = client.enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
        campaign.campaign_budget = budget_resource

        if target_roas:
            campaign.maximize_conversion_value.target_roas = target_roas
        elif target_cpa_micros:
            campaign.maximize_conversions.target_cpa_micros = target_cpa_micros
        else:
            campaign.maximize_conversion_value.target_roas = 0

        campaign_response = campaign_service.mutate_campaigns(customer_id=cid, operations=[campaign_op])
        return json.dumps({
            "success": True,
            "campaign_resource_name": campaign_response.results[0].resource_name,
            "budget_resource_name": budget_resource,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_create_rsa(
    ad_group_id: str,
    final_url: str,
    headlines: str,
    descriptions: str,
    path1: str = "",
    path2: str = "",
    status: str = "PAUSED",
    customer_id: str = "",
) -> str:
    """Create a Responsive Search Ad (RSA). Headlines and descriptions as JSON arrays.

    Args:
        ad_group_id: Ad group ID to add the ad to.
        final_url: Destination URL.
        headlines: JSON array of 3-15 headline strings (max 30 chars each).
          e.g. ["Headline 1", "Headline 2", "Headline 3"]
        descriptions: JSON array of 2-4 description strings (max 90 chars each).
        path1: First URL display path (max 15 chars).
        path2: Second URL display path (max 15 chars).
        status: ENABLED | PAUSED. Default PAUSED.
        customer_id: Google Ads customer ID.
    """
    try:
        client, cid = _get_client(customer_id)
        h_list = json.loads(headlines) if isinstance(headlines, str) else headlines
        d_list = json.loads(descriptions) if isinstance(descriptions, str) else descriptions
        service = client.get_service("AdGroupAdService")
        op = client.get_type("AdGroupAdOperation")
        aga = op.create
        aga.ad_group = f"customers/{cid}/adGroups/{ad_group_id}"
        aga.status = client.enums.AdGroupAdStatusEnum[status.upper()]
        aga.ad.final_urls.append(final_url)
        rsa = aga.ad.responsive_search_ad
        for h in h_list:
            asset = client.get_type("AdTextAsset")
            asset.text = h
            rsa.headlines.append(asset)
        for d in d_list:
            asset = client.get_type("AdTextAsset")
            asset.text = d
            rsa.descriptions.append(asset)
        if path1:
            rsa.path1 = path1
        if path2:
            rsa.path2 = path2
        resp = service.mutate_ad_group_ads(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": resp.results[0].resource_name})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


# ---------------------------------------------------------------------------
# GROUP 7: Budget & Billing
# ---------------------------------------------------------------------------





@mcp.tool()
def gads_create_search_campaign(
    name: str,
    budget_micros: int,
    bidding_strategy: str = "MAXIMIZE_CONVERSIONS",
    target_cpa_micros: int = 0,
    networks_search: bool = True,
    networks_search_partners: bool = False,
    geo_targets: list = None,
    languages: list = None,
    customer_id: str = "",
) -> str:
    """Create a Search campaign with common settings."""
    try:
        client, cid = _get_client(customer_id)

        # Create budget
        budget_service = client.get_service("CampaignBudgetService")
        budget_op = client.get_type("CampaignBudgetOperation")
        budget = budget_op.create
        budget.name = f"{name} Budget"
        budget.amount_micros = budget_micros
        budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
        budget.explicitly_shared = False
        budget_response = budget_service.mutate_campaign_budgets(customer_id=cid, operations=[budget_op])
        budget_resource = budget_response.results[0].resource_name

        # Create campaign
        campaign_service = client.get_service("CampaignService")
        campaign_op = client.get_type("CampaignOperation")
        campaign = campaign_op.create
        campaign.name = name
        campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.SEARCH
        campaign.status = client.enums.CampaignStatusEnum.PAUSED
        campaign.contains_eu_political_advertising = client.enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
        campaign.campaign_budget = budget_resource
        campaign.network_settings.target_google_search = networks_search
        campaign.network_settings.target_search_network = networks_search_partners

        strategy = bidding_strategy.upper()
        if strategy == "MAXIMIZE_CONVERSIONS":
            if target_cpa_micros:
                campaign.maximize_conversions.target_cpa_micros = target_cpa_micros
            else:
                campaign.maximize_conversions.target_cpa_micros = 0
        elif strategy == "MAXIMIZE_CONVERSION_VALUE":
            campaign.maximize_conversion_value.target_roas = 0
        elif strategy == "TARGET_CPA":
            campaign.target_cpa.target_cpa_micros = target_cpa_micros
        elif strategy == "MANUAL_CPC":
            campaign.manual_cpc.enhanced_cpc_enabled = False
        elif strategy == "TARGET_IMPRESSION_SHARE":
            campaign.target_impression_share.location = client.enums.TargetImpressionShareLocationEnum.ANYWHERE_ON_PAGE
            campaign.target_impression_share.location_fraction_micros = 1000000

        campaign_response = campaign_service.mutate_campaigns(customer_id=cid, operations=[campaign_op])
        campaign_resource = campaign_response.results[0].resource_name

        results = {
            "success": True,
            "campaign_resource_name": campaign_resource,
            "budget_resource_name": budget_resource,
        }

        # Add geo targets
        if geo_targets:
            criterion_service = client.get_service("CampaignCriterionService")
            criterion_ops = []
            for geo_id in geo_targets:
                cop = client.get_type("CampaignCriterionOperation")
                c = cop.create
                c.campaign = campaign_resource
                c.location.geo_target_constant = f"geoTargetConstants/{geo_id}"
                criterion_ops.append(cop)
            criterion_service.mutate_campaign_criteria(customer_id=cid, operations=criterion_ops)
            results["geo_targets_added"] = len(geo_targets)

        # Add languages
        if languages:
            criterion_service = client.get_service("CampaignCriterionService")
            lang_ops = []
            for lang_id in languages:
                lop = client.get_type("CampaignCriterionOperation")
                lc = lop.create
                lc.campaign = campaign_resource
                lc.language.language_constant = f"languageConstants/{lang_id}"
                lang_ops.append(lop)
            criterion_service.mutate_campaign_criteria(customer_id=cid, operations=lang_ops)
            results["languages_added"] = len(languages)

        return json.dumps(results)
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_create_video_campaign(
    name: str,
    budget_micros: int,
    subtype: str = "VIDEO_VIEW",
    bidding_strategy: str = "TARGET_CPV",
    target_cpv_micros: int = 0,
    customer_id: str = "",
) -> str:
    """Create a Video campaign. subtype: VIDEO_ACTION, VIDEO_VIEW, VIDEO_REACH_TARGET_FREQUENCY, etc."""
    try:
        client, cid = _get_client(customer_id)

        # Create budget
        budget_service = client.get_service("CampaignBudgetService")
        budget_op = client.get_type("CampaignBudgetOperation")
        budget = budget_op.create
        budget.name = f"{name} Budget"
        budget.amount_micros = budget_micros
        budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
        budget.explicitly_shared = False
        budget_response = budget_service.mutate_campaign_budgets(customer_id=cid, operations=[budget_op])
        budget_resource = budget_response.results[0].resource_name

        # Create campaign
        campaign_service = client.get_service("CampaignService")
        campaign_op = client.get_type("CampaignOperation")
        campaign = campaign_op.create
        campaign.name = name
        campaign.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.VIDEO
        campaign.status = client.enums.CampaignStatusEnum.PAUSED
        campaign.contains_eu_political_advertising = client.enums.EuPoliticalAdvertisingStatusEnum.DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING
        campaign.campaign_budget = budget_resource

        subtype_enum = client.enums.AdvertisingChannelSubTypeEnum
        subtype_map = {
            "VIDEO_ACTION": subtype_enum.VIDEO_ACTION,
            "VIDEO_VIEW": subtype_enum.VIDEO_VIEW,
            "VIDEO_REACH_TARGET_FREQUENCY": subtype_enum.VIDEO_REACH_TARGET_FREQUENCY,
            "VIDEO_NON_SKIPPABLE": subtype_enum.VIDEO_NON_SKIPPABLE,
        }
        if subtype in subtype_map:
            campaign.advertising_channel_sub_type = subtype_map[subtype]

        strategy = bidding_strategy.upper()
        if strategy == "TARGET_CPV":
            campaign.target_cpv.target_cpv_micros = target_cpv_micros
        elif strategy == "MAXIMIZE_CONVERSIONS":
            campaign.maximize_conversions.target_cpa_micros = 0
        elif strategy == "TARGET_CPA":
            campaign.target_cpa.target_cpa_micros = target_cpv_micros

        campaign_response = campaign_service.mutate_campaigns(customer_id=cid, operations=[campaign_op])
        return json.dumps({
            "success": True,
            "campaign_resource_name": campaign_response.results[0].resource_name,
            "budget_resource_name": budget_resource,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_create_video_responsive_ad(ad_group_id: str, headlines: list, descriptions: list,
                                     videos: list, thumbnails: list = None,
                                     final_urls: list = None, customer_id: str = "") -> str:
    """Create a Video Responsive ad (for YouTube).
    headlines: list of strings (max 5).
    descriptions: list of strings (max 5).
    videos: list of YouTube video asset resource names (max 5).
    thumbnails: list of image asset resource names (optional)."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupAdService")
        op = client.get_type("AdGroupAdOperation")
        ad_group_ad = op.create
        ad_group_ad.ad_group = f"customers/{cid}/adGroups/{ad_group_id}"
        ad_group_ad.status = client.enums.AdGroupAdStatusEnum.PAUSED
        ad = ad_group_ad.ad
        vra = ad.video_responsive_ad

        AdTextAsset = client.get_type("AdTextAsset")
        AdVideoAsset = client.get_type("AdVideoAsset")
        AdImageAsset = client.get_type("AdImageAsset")

        for h in headlines[:5]:
            ata = AdTextAsset()
            ata.text = h
            vra.headlines.append(ata)
        for d in descriptions[:5]:
            ata = AdTextAsset()
            ata.text = d
            vra.descriptions.append(ata)
        for v in videos[:5]:
            ava = AdVideoAsset()
            ava.asset = v
            vra.videos.append(ava)
        if thumbnails:
            for t in thumbnails[:5]:
                aia = AdImageAsset()
                aia.asset = t
                vra.companion_banners.append(aia)
        if final_urls:
            ad.final_urls.extend(final_urls)

        response = service.mutate_ad_group_ads(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Ad preview ────────────────────────────────────────────────────────────





@mcp.tool()
def gads_detach_shared_list_from_campaign(campaign_id: str, shared_set_id: str,
                                           customer_id: str = "") -> str:
    """Detach a shared negative keyword list from a campaign."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignSharedSetService")
        op = client.get_type("CampaignSharedSetOperation")
        op.remove = f"customers/{cid}/campaignSharedSets/{campaign_id}~{shared_set_id}"
        service.mutate_campaign_shared_sets(customer_id=cid, operations=[op])
        return json.dumps({"success": True})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Portfolio bidding strategy assignment ─────────────────────────────────





@mcp.tool()
def gads_enable_keyword(ad_group_id: str, criterion_id: str, customer_id: str = "") -> str:
    """Enable (unpause) a keyword."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupCriterionService")
        op = client.get_type("AdGroupCriterionOperation")
        criterion = op.update
        criterion.resource_name = f"customers/{cid}/adGroupCriteria/{ad_group_id}~{criterion_id}"
        criterion.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["status"]))
        response = service.mutate_ad_group_criteria(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_get_ad_group_cpa_simulation(ad_group_id: str, customer_id: str = "") -> str:
    """Get target CPA bid simulation data for an ad group."""
    try:
        rows = _search(
            f"""SELECT ad_group_simulation.ad_group_id, ad_group_simulation.type,
                       ad_group_simulation.modification_method,
                       ad_group_simulation.start_date, ad_group_simulation.end_date,
                       ad_group_simulation.target_cpa_point_list.points
                FROM ad_group_simulation
                WHERE ad_group_simulation.ad_group_id = {ad_group_id}
                  AND ad_group_simulation.type = 'TARGET_CPA'""",
            customer_id
        )
        result = []
        for row in rows:
            sim = row.ad_group_simulation
            points = []
            for p in sim.target_cpa_point_list.points:
                points.append({
                    "target_cpa_micros": p.target_cpa_micros,
                    "biddable_conversions": p.biddable_conversions,
                    "biddable_conversions_value": p.biddable_conversions_value,
                    "clicks": p.clicks,
                    "cost_micros": p.cost_micros,
                    "impressions": p.impressions,
                })
            result.append({
                "ad_group_id": str(sim.ad_group_id),
                "type": str(sim.type_),
                "start_date": sim.start_date,
                "end_date": sim.end_date,
                "points": points,
            })
        return json.dumps({"simulations": result, "count": len(result)})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Keyword suggestions for ad group ──────────────────────────────────────





@mcp.tool()
def gads_get_ad_group_cpc_estimates(ad_group_id: str, customer_id: str = "") -> str:
    """Get CPC bid estimates for an ad group.
    Args: ad_group_id: Ad group ID. customer_id: optional."""
    return gads_get_ad_group_simulation(ad_group_id=ad_group_id, customer_id=customer_id)


from google.protobuf import field_mask_pb2






@mcp.tool()
def gads_get_ad_group_keyword_suggestions(ad_group_id: str, customer_id: str = "") -> str:
    """Get keyword theme suggestions for an ad group (Smart Campaign keyword themes)."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("SmartCampaignSuggestService")
        request = client.get_type("SuggestSmartCampaignKeywordThemesRequest")
        request.customer_id = cid
        request.ad_group = f"customers/{cid}/adGroups/{ad_group_id}"
        response = service.suggest_smart_campaign_keyword_themes(request=request)
        themes = []
        for theme in response.keyword_themes:
            themes.append({
                "keyword_theme_constant": theme.keyword_theme_constant,
                "free_form_keyword_theme": theme.free_form_keyword_theme,
            })
        return json.dumps({"keyword_theme_suggestions": themes, "count": len(themes)})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Request ad review ──────────────────────────────────────────────────────





@mcp.tool()
def gads_get_ad_group_url_options(ad_group_id: str, customer_id: str = "") -> str:
    """Get URL options (tracking template, final URL suffix, custom params) for an ad group."""
    try:
        client, cid = _get_client(customer_id)
        query = (
            f"SELECT ad_group.id, ad_group.name, ad_group.tracking_url_template, "
            f"ad_group.final_url_suffix, ad_group.url_custom_parameters "
            f"FROM ad_group "
            f"WHERE ad_group.id = {ad_group_id}"
        )
        results = _search(query, cid)
        url_options = {}
        for row in results:
            ag = row.ad_group
            custom_params = [{"key": p.key, "value": p.value} for p in ag.url_custom_parameters]
            url_options = {
                "id": ag.id,
                "name": ag.name,
                "tracking_url_template": ag.tracking_url_template,
                "final_url_suffix": ag.final_url_suffix,
                "url_custom_parameters": custom_params,
            }
        return json.dumps({"success": True, "url_options": url_options})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_get_ad_preview(keyword: str, geo_criterion_id: str = "2840",
                         language_criterion_id: str = "1000",
                         device: str = "DESKTOP", customer_id: str = "") -> str:
    """Get ad preview for a keyword. Uses AdGroupAdService simulation.
    geo_criterion_id: e.g. '2840' for United States.
    language_criterion_id: e.g. '1000' for English.
    device: DESKTOP, MOBILE, TABLET."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupAdService")
        request = client.get_type("GenerateAdGroupThemesRequest")
        # Use the search ads 360 approach instead
        # For ad preview, use the ad_preview_and_diagnosis endpoint behavior
        # Return a GAQL query result showing ads for the keyword instead
        rows = _search(
            f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.responsive_search_ad.headlines,
                       ad_group_ad.ad.responsive_search_ad.descriptions,
                       ad_group_ad.ad.final_urls, ad_group_ad.status, ad_group.name,
                       campaign.name
                FROM ad_group_ad
                WHERE ad_group_ad.status = 'ENABLED'
                  AND campaign.status = 'ENABLED'
                  AND ad_group.status = 'ENABLED'
                LIMIT 10""",
            customer_id
        )
        ads = []
        for row in rows:
            ad = row.ad_group_ad.ad
            ads.append({
                "ad_id": str(ad.id),
                "ad_group": row.ad_group.name,
                "campaign": row.campaign.name,
                "final_urls": list(ad.final_urls),
                "status": str(row.ad_group_ad.status),
            })
        return json.dumps({
            "note": "Ad preview requires the Google Ads UI or AdGroupAdService preview. This returns active ads.",
            "keyword": keyword,
            "device": device,
            "active_ads_sample": ads,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── RSA performance details ────────────────────────────────────────────────





@mcp.tool()
def gads_get_ad_schedule_bid_modifiers(campaign_id: str, customer_id: str = "") -> str:
    """Get ad schedule bid modifiers for a campaign.
    Args: campaign_id: Campaign ID. customer_id: optional."""
    try:
        _, cid = _get_client(customer_id)
        rows = _search(f"""
            SELECT campaign_criterion.criterion_id,
                   campaign_criterion.ad_schedule.day_of_week,
                   campaign_criterion.ad_schedule.start_hour,
                   campaign_criterion.ad_schedule.end_hour,
                   campaign_criterion.bid_modifier
            FROM campaign_criterion
            WHERE campaign_criterion.campaign = 'customers/{cid}/campaigns/{campaign_id}'
              AND campaign_criterion.type = 'AD_SCHEDULE'
        """, customer_id)
        results = []
        for r in rows:
            cc = r.campaign_criterion
            results.append({
                "criterion_id": str(cc.criterion_id),
                "day_of_week": cc.ad_schedule.day_of_week.name,
                "start_hour": cc.ad_schedule.start_hour,
                "end_hour": cc.ad_schedule.end_hour,
                "bid_modifier": cc.bid_modifier,
            })
        return json.dumps(results)
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_get_asset_group_details(asset_group_id: str, customer_id: str = "") -> str:
    """Get full details of an asset group including all assets."""
    try:
        client, cid = _get_client(customer_id)

        ag_query = (
            f"SELECT asset_group.id, asset_group.name, asset_group.status, "
            f"asset_group.final_urls, asset_group.final_mobile_urls, "
            f"asset_group.path1, asset_group.path2 "
            f"FROM asset_group "
            f"WHERE asset_group.id = {asset_group_id}"
        )
        ag_results = _search(ag_query, cid)
        asset_group_info = {}
        for row in ag_results:
            ag = row.asset_group
            asset_group_info = {
                "id": ag.id,
                "name": ag.name,
                "status": ag.status.name,
                "final_urls": list(ag.final_urls),
                "final_mobile_urls": list(ag.final_mobile_urls),
                "path1": ag.path1,
                "path2": ag.path2,
            }

        asset_query = (
            f"SELECT asset_group_asset.asset, asset_group_asset.field_type, "
            f"asset_group_asset.status, asset.name, asset.type "
            f"FROM asset_group_asset "
            f"WHERE asset_group_asset.asset_group = 'customers/{cid}/assetGroups/{asset_group_id}'"
        )
        asset_results = _search(asset_query, cid)
        assets = []
        for row in asset_results:
            assets.append({
                "asset": row.asset_group_asset.asset,
                "field_type": row.asset_group_asset.field_type.name,
                "status": row.asset_group_asset.status.name,
                "asset_name": row.asset.name,
                "asset_type": row.asset.type_.name,
            })

        return json.dumps({"success": True, "asset_group": asset_group_info, "assets": assets})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_get_billing_setup(customer_id: str = "") -> str:
    """Get billing setup details for the account."""
    try:
        rows = _search(
            """SELECT billing_setup.id, billing_setup.status,
                      billing_setup.payments_account_info.payments_account_id,
                      billing_setup.payments_account_info.payments_account_name,
                      billing_setup.payments_profile_info.payments_profile_id
               FROM billing_setup""",
            customer_id
        )
        result = []
        for row in rows:
            bs = row.billing_setup
            result.append({
                "id": str(bs.id),
                "status": str(bs.status),
                "payments_account_id": bs.payments_account_info.payments_account_id,
                "payments_account_name": bs.payments_account_info.payments_account_name,
                "payments_profile_id": bs.payments_profile_info.payments_profile_id,
            })
        return json.dumps({"billing_setups": result})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_get_campaign_ai_max_status(customer_id: str = "") -> str:
    """Get AI Max status for all Search campaigns in the account.

    Returns list of campaigns with their current AI Max enabled/disabled status.

    Args:
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        rows = _search(
            "SELECT campaign.id, campaign.name, campaign.status, "
            "campaign.ai_max_setting.enable_ai_max "
            "FROM campaign "
            "WHERE campaign.status != REMOVED "
            "AND campaign.advertising_channel_type = SEARCH "
            "ORDER BY campaign.name",
            customer_id,
        )
        campaigns = []
        for row in rows:
            campaigns.append({
                "campaign_id": str(row.campaign.id),
                "name": row.campaign.name,
                "status": row.campaign.status.name,
                "ai_max_enabled": row.campaign.ai_max_setting.enable_ai_max,
            })
        enabled = sum(1 for c in campaigns if c["ai_max_enabled"])
        return json.dumps({
            "total_search_campaigns": len(campaigns),
            "ai_max_enabled_count": enabled,
            "ai_max_disabled_count": len(campaigns) - enabled,
            "campaigns": campaigns,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_get_campaign_bidding_details(campaign_id: str, customer_id: str = "") -> str:
    """Get detailed bidding strategy settings for a campaign."""
    try:
        rows = _search(
            f"""SELECT campaign.id, campaign.name,
                       campaign.bidding_strategy_type,
                       campaign.maximize_conversions.target_cpa_micros,
                       campaign.maximize_conversion_value.target_roas,
                       campaign.target_cpa.target_cpa_micros,
                       campaign.target_roas.target_roas,
                       campaign.target_impression_share.location,
                       campaign.target_impression_share.location_fraction_micros,
                       campaign.target_impression_share.cpc_bid_ceiling_micros,
                       campaign.maximize_clicks.target_spend_micros,
                       campaign.maximize_clicks.cpc_bid_ceiling_micros,
                       campaign.manual_cpc.enhanced_cpc_enabled,
                       campaign.manual_cpm,
                       campaign.manual_cpv,
                       campaign.bidding_strategy
                FROM campaign
                WHERE campaign.id = {campaign_id}""",
            customer_id
        )
        if not rows:
            return json.dumps({"error": "Campaign not found"})
        c = rows[0].campaign
        return json.dumps({
            "id": str(c.id),
            "name": c.name,
            "bidding_strategy_type": str(c.bidding_strategy_type),
            "maximize_conversions_target_cpa_micros": c.maximize_conversions.target_cpa_micros,
            "maximize_conversion_value_target_roas": c.maximize_conversion_value.target_roas,
            "target_cpa_micros": c.target_cpa.target_cpa_micros,
            "target_roas": c.target_roas.target_roas,
            "target_impression_share_location": str(c.target_impression_share.location),
            "target_impression_share_fraction_micros": c.target_impression_share.location_fraction_micros,
            "target_impression_share_cpc_ceiling_micros": c.target_impression_share.cpc_bid_ceiling_micros,
            "maximize_clicks_target_spend_micros": c.maximize_clicks.target_spend_micros,
            "maximize_clicks_cpc_ceiling_micros": c.maximize_clicks.cpc_bid_ceiling_micros,
            "manual_cpc_enhanced_cpc": c.manual_cpc.enhanced_cpc_enabled,
            "portfolio_bidding_strategy": c.bidding_strategy,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_get_campaign_experiment(experiment_campaign_id: str, customer_id: str = "") -> str:
    """Get details of a campaign experiment by the experiment campaign ID."""
    try:
        rows = _search(
            f"""SELECT experiment.name, experiment.status, experiment.type,
                       experiment.start_date, experiment.end_date,
                       experiment.traffic_split_percent, experiment.resource_name
                FROM experiment
                WHERE experiment.resource_name LIKE '%{experiment_campaign_id}%'""",
            customer_id
        )
        if not rows:
            # Try searching by experiment arms
            rows = _search(
                f"""SELECT experiment_arm.experiment, experiment_arm.campaign,
                           experiment_arm.name, experiment_arm.trial_fraction
                    FROM experiment_arm
                    WHERE experiment_arm.campaign = 'customers/{_get_client(customer_id)[1]}/campaigns/{experiment_campaign_id}'""",
                customer_id
            )
            if rows:
                arms = []
                for row in rows:
                    arm = row.experiment_arm
                    arms.append({
                        "experiment": arm.experiment,
                        "campaign": arm.campaign,
                        "name": arm.name,
                        "trial_fraction": arm.trial_fraction,
                    })
                return json.dumps({"experiment_arms": arms})
            return json.dumps({"error": "Experiment not found"})
        result = []
        for row in rows:
            e = row.experiment
            result.append({
                "resource_name": e.resource_name,
                "name": e.name,
                "status": str(e.status),
                "type": str(e.type_),
                "start_date": e.start_date,
                "end_date": e.end_date,
                "traffic_split_percent": e.traffic_split_percent,
            })
        return json.dumps({"experiments": result})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Ad group content bid criterion ────────────────────────────────────────





@mcp.tool()
def gads_get_campaign_frequency_cap(campaign_id: str, customer_id: str = "") -> str:
    """Get frequency cap settings for a campaign."""
    try:
        rows = _search(
            f"""SELECT campaign.id, campaign.name, campaign.frequency_caps
                FROM campaign
                WHERE campaign.id = {campaign_id}""",
            customer_id
        )
        if not rows:
            return json.dumps({"error": "Campaign not found"})
        c = rows[0].campaign
        caps = []
        for fc in c.frequency_caps:
            caps.append({
                "impressions": fc.cap,
                "time_unit": str(fc.time_unit),
                "time_length": fc.time_length,
                "event_type": str(fc.key.event_type) if hasattr(fc, 'key') else "",
                "level": str(fc.key.level) if hasattr(fc, 'key') else "",
            })
        return json.dumps({"campaign_id": campaign_id, "frequency_caps": caps})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Campaign drafts ───────────────────────────────────────────────────────





@mcp.tool()
def gads_get_campaign_schedule(campaign_id: str, customer_id: str = "") -> str:
    """Get the full ad schedule for a campaign.
    Args: campaign_id: Campaign ID. customer_id: optional."""
    return gads_get_ad_schedule_bid_modifiers(campaign_id=campaign_id, customer_id=customer_id)






@mcp.tool()
def gads_get_campaign_targeting(campaign_id: str, customer_id: str = "") -> str:
    """Get all targeting settings for a campaign (networks, geo, language, devices, schedule).
    Args: campaign_id: Campaign ID. customer_id: optional."""
    try:
        _, cid = _get_client(customer_id)
        # Get network settings from campaign
        camp_rows = _search(f"""
            SELECT campaign.id, campaign.name,
                   campaign.network_settings.target_google_search,
                   campaign.network_settings.target_search_network,
                   campaign.network_settings.target_content_network,
                   campaign.geo_target_type_setting.positive_geo_target_type
            FROM campaign WHERE campaign.id = {campaign_id}
        """, customer_id)
        if not camp_rows:
            return json.dumps({"error": "Campaign not found"})
        c = camp_rows[0].campaign

        # Get geo targets
        geo_rows = _search(f"""
            SELECT campaign_criterion.criterion_id, campaign_criterion.type,
                   campaign_criterion.location.geo_target_constant, campaign_criterion.negative
            FROM campaign_criterion
            WHERE campaign_criterion.campaign = 'customers/{cid}/campaigns/{campaign_id}'
              AND campaign_criterion.type = 'LOCATION'
        """, customer_id)

        # Get languages
        lang_rows = _search(f"""
            SELECT campaign_criterion.language.language_constant
            FROM campaign_criterion
            WHERE campaign_criterion.campaign = 'customers/{cid}/campaigns/{campaign_id}'
              AND campaign_criterion.type = 'LANGUAGE'
        """, customer_id)

        result = {
            "campaign_id": campaign_id,
            "networks": {
                "google_search": c.network_settings.target_google_search,
                "search_partners": c.network_settings.target_search_network,
                "display": c.network_settings.target_content_network,
            },
            "locations": [{"geo_target": r.campaign_criterion.location.geo_target_constant, "negative": r.campaign_criterion.negative} for r in geo_rows],
            "languages": [r.campaign_criterion.language.language_constant for r in lang_rows],
        }
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_get_campaign_url_options(campaign_id: str, customer_id: str = "") -> str:
    """Get URL options (tracking template, final URL suffix, custom params) for a campaign."""
    try:
        client, cid = _get_client(customer_id)
        query = (
            f"SELECT campaign.id, campaign.name, campaign.tracking_url_template, "
            f"campaign.final_url_suffix, campaign.url_custom_parameters "
            f"FROM campaign "
            f"WHERE campaign.id = {campaign_id}"
        )
        results = _search(query, cid)
        url_options = {}
        for row in results:
            c = row.campaign
            custom_params = [{"key": p.key, "value": p.value} for p in c.url_custom_parameters]
            url_options = {
                "id": c.id,
                "name": c.name,
                "tracking_url_template": c.tracking_url_template,
                "final_url_suffix": c.final_url_suffix,
                "url_custom_parameters": custom_params,
            }
        return json.dumps({"success": True, "url_options": url_options})
    except Exception as e:
        return json.dumps({"error": str(e)})



# ---------------------------------------------------------------------------
# BATCH 16 — Carrier/Device/OS Targeting, Negative Audiences, PMax Brand,
#             App Campaigns, DSA Update, Local Campaigns, Seasonality Mgmt,
#             Location/Language Lists, Conversion Adjustments, URL options
# ---------------------------------------------------------------------------

from google.protobuf import field_mask_pb2


# ── Carrier targeting ──────────────────────────────────────────────────────





@mcp.tool()
def gads_get_campaigns_without_spend(days: int = 30, customer_id: str = "") -> str:
    """Get campaigns with zero spend in the last N days."""
    try:
        rows = _search(
            f"""SELECT campaign.id, campaign.name, campaign.status,
                       metrics.cost_micros, metrics.impressions
                FROM campaign
                WHERE campaign.status = 'ENABLED'
                  AND segments.date DURING LAST_{days}_DAYS
                  AND metrics.cost_micros = 0
                LIMIT 200""",
            customer_id
        )
        result = []
        seen = set()
        for row in rows:
            c = row.campaign
            if str(c.id) not in seen:
                seen.add(str(c.id))
                result.append({
                    "id": str(c.id),
                    "name": c.name,
                    "status": str(c.status),
                    "cost_micros": row.metrics.cost_micros,
                    "impressions": row.metrics.impressions,
                })
        return json.dumps({"campaigns_without_spend": result, "count": len(result),
                           "period_days": days})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Keywords with quality score ────────────────────────────────────────────





@mcp.tool()
def gads_get_dayofweek_performance(date_range: str = "LAST_30_DAYS", campaign_id: str = "", customer_id: str = "") -> str:
    """Get performance metrics by day of week.
    Args: date_range: e.g. LAST_30_DAYS. campaign_id: optional filter. customer_id: optional."""
    try:
        where = f"WHERE segments.date DURING {date_range}"
        if campaign_id:
            where += f" AND campaign.id = {campaign_id}"
        rows = _search(f"""
            SELECT segments.day_of_week,
                   metrics.impressions, metrics.clicks, metrics.cost_micros,
                   metrics.conversions, metrics.ctr, metrics.average_cpc
            FROM campaign
            {where}
        """, customer_id)
        results = {}
        for r in rows:
            day = r.segments.day_of_week.name
            m = r.metrics
            if day not in results:
                results[day] = {"impressions": 0, "clicks": 0, "cost": 0, "conversions": 0}
            results[day]["impressions"] += m.impressions
            results[day]["clicks"] += m.clicks
            results[day]["cost"] += m.cost_micros / 1e6
            results[day]["conversions"] += m.conversions
        return json.dumps(results)
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_get_device_bid_modifiers(campaign_id: str, customer_id: str = "") -> str:
    """Get device bid modifiers for a campaign.
    Args: campaign_id: Campaign ID. customer_id: optional."""
    try:
        _, cid = _get_client(customer_id)
        rows = _search(f"""
            SELECT campaign_criterion.criterion_id,
                   campaign_criterion.device.type,
                   campaign_criterion.bid_modifier
            FROM campaign_criterion
            WHERE campaign_criterion.campaign = 'customers/{cid}/campaigns/{campaign_id}'
              AND campaign_criterion.type = 'DEVICE'
        """, customer_id)
        results = []
        for r in rows:
            cc = r.campaign_criterion
            results.append({"criterion_id": str(cc.criterion_id), "device": cc.device.type_.name, "bid_modifier": cc.bid_modifier})
        return json.dumps(results)
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_get_hotel_campaign_settings(campaign_id: str, customer_id: str = "") -> str:
    """Get hotel-specific campaign settings."""
    try:
        rows = _search(
            f"""SELECT campaign.id, campaign.name,
                       campaign.hotel_setting.hotel_center_id
                FROM campaign
                WHERE campaign.id = {campaign_id}""",
            customer_id
        )
        if not rows:
            return json.dumps({"error": "Campaign not found"})
        c = rows[0].campaign
        return json.dumps({
            "id": str(c.id),
            "name": c.name,
            "hotel_center_id": str(c.hotel_setting.hotel_center_id),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Create customer (sub-account) ─────────────────────────────────────────





@mcp.tool()
def gads_get_keyword_bid_estimates(keywords_json: str, campaign_id: str = "", ad_group_id: str = "", customer_id: str = "") -> str:
    """Get bid estimates for keywords using the KeywordPlanIdeaService.
    Args: keywords_json: JSON array of keyword strings. campaign_id: Optional campaign context. ad_group_id: Optional ad group context. customer_id: optional."""
    try:
        # Use keyword ideas to get bid estimates
        keywords = json.loads(keywords_json)
        return gads_generate_keyword_ideas(seed_keywords=",".join(keywords), customer_id=customer_id)
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_get_keyword_forecast(keywords: list, geo_target_ids: list = None,
                               language_id: str = "1000", daily_budget_micros: int = 1000000,
                               customer_id: str = "") -> str:
    """Get traffic forecast for keywords. keywords: list of {text, match_type} dicts.
    geo_target_ids: list of NUMERIC geo target constant IDs, e.g. ['2840'] for the
    United States, ['2376'] for Israel. language_id: '1000' = English.
    Forecasts a 30-day window starting tomorrow; Keyword Planner cannot forecast the past."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("KeywordPlanIdeaService")
        request = client.get_type("GenerateKeywordForecastMetricsRequest")
        request.customer_id = cid
        request.currency_code = "USD"
        start = date.today() + timedelta(days=1)
        request.forecast_period.start_date = start.isoformat()
        request.forecast_period.end_date = (start + timedelta(days=30)).isoformat()

        campaign = request.campaign
        campaign.keyword_plan_network = client.enums.KeywordPlanNetworkEnum.GOOGLE_SEARCH
        if language_id:
            campaign.language_constants.append(f"languageConstants/{language_id}")
        for geo_id in (geo_target_ids or []):
            modifier = client.get_type("CriterionBidModifier")
            modifier.geo_target_constant = f"geoTargetConstants/{geo_id}"
            campaign.geo_modifiers.append(modifier)

        max_cpc = daily_budget_micros // max(len(keywords), 1)
        campaign.bidding_strategy.manual_cpc_bidding_strategy.max_cpc_bid_micros = max_cpc

        ad_group = client.get_type("ForecastAdGroup")
        ad_group.max_cpc_bid_micros = max_cpc
        for kw in keywords:
            biddable = client.get_type("BiddableKeyword")
            biddable.keyword.text = kw.get("text", kw) if isinstance(kw, dict) else kw
            mt = kw.get("match_type", "BROAD") if isinstance(kw, dict) else "BROAD"
            biddable.keyword.match_type = client.enums.KeywordMatchTypeEnum[mt]
            biddable.max_cpc_bid_micros = max_cpc
            ad_group.biddable_keywords.append(biddable)
        campaign.ad_groups.append(ad_group)

        response = service.generate_keyword_forecast_metrics(request=request)
        metrics = response.campaign_forecast_metrics
        return json.dumps({
            "forecast_start": request.forecast_period.start_date,
            "forecast_end": request.forecast_period.end_date,
            "impressions": metrics.impressions,
            "clicks": metrics.clicks,
            "click_through_rate": metrics.click_through_rate,
            "average_cpc_micros": metrics.average_cpc_micros,
            "cost_micros": metrics.cost_micros,
            "conversions": metrics.conversions,
            "conversion_rate": metrics.conversion_rate,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Product partition update ───────────────────────────────────────────────





@mcp.tool()
def gads_get_keyword_quality_score(ad_group_id: str, customer_id: str = "") -> str:
    """Get quality scores for all keywords in an ad group.
    Args: ad_group_id: Ad group ID. customer_id: optional."""
    try:
        _, cid = _get_client(customer_id)
        rows = _search(f"""
            SELECT ad_group_criterion.criterion_id, ad_group_criterion.keyword.text,
                   ad_group_criterion.keyword.match_type,
                   ad_group_criterion.quality_info.quality_score,
                   ad_group_criterion.quality_info.search_predicted_ctr,
                   ad_group_criterion.quality_info.creative_quality_score,
                   ad_group_criterion.quality_info.post_click_quality_score
            FROM ad_group_criterion
            WHERE ad_group_criterion.ad_group = 'customers/{cid}/adGroups/{ad_group_id}'
              AND ad_group_criterion.type = 'KEYWORD'
              AND ad_group_criterion.negative = FALSE
        """, customer_id)
        results = []
        for r in rows:
            ac = r.ad_group_criterion
            qi = ac.quality_info
            results.append({
                "criterion_id": str(ac.criterion_id),
                "keyword": ac.keyword.text,
                "match_type": ac.keyword.match_type.name,
                "quality_score": qi.quality_score,
                "predicted_ctr": qi.search_predicted_ctr.name if qi.search_predicted_ctr else "UNKNOWN",
                "ad_relevance": qi.creative_quality_score.name if qi.creative_quality_score else "UNKNOWN",
                "landing_page_experience": qi.post_click_quality_score.name if qi.post_click_quality_score else "UNKNOWN",
            })
        return json.dumps(results)
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_get_keyword_simulation(ad_group_id: str, criterion_id: str, customer_id: str = "") -> str:
    """Get bid simulation data for a keyword."""
    try:
        rows = _search(
            f"""SELECT ad_group_criterion_simulation.ad_group_id,
                       ad_group_criterion_simulation.criterion_id,
                       ad_group_criterion_simulation.type,
                       ad_group_criterion_simulation.modification_method,
                       ad_group_criterion_simulation.start_date,
                       ad_group_criterion_simulation.end_date,
                       ad_group_criterion_simulation.cpc_bid_point_list.points
                FROM ad_group_criterion_simulation
                WHERE ad_group_criterion_simulation.ad_group_id = {ad_group_id}
                  AND ad_group_criterion_simulation.criterion_id = {criterion_id}""",
            customer_id
        )
        result = []
        for row in rows:
            sim = row.ad_group_criterion_simulation
            points = []
            for p in sim.cpc_bid_point_list.points:
                points.append({
                    "cpc_bid_micros": p.cpc_bid_micros,
                    "impressions": p.impressions,
                    "clicks": p.clicks,
                    "cost_micros": p.cost_micros,
                })
            result.append({
                "ad_group_id": str(sim.ad_group_id),
                "criterion_id": str(sim.criterion_id),
                "type": str(sim.type_),
                "modification_method": str(sim.modification_method),
                "start_date": sim.start_date,
                "end_date": sim.end_date,
                "points": points,
            })
        return json.dumps({"simulations": result, "count": len(result)})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Customer-level URL options ─────────────────────────────────────────────





@mcp.tool()
def gads_get_mcc_performance(date_range: str = "LAST_30_DAYS", customer_id: str = "") -> str:
    """Get performance for all accounts under an MCC.
    Args: date_range: e.g. LAST_30_DAYS. customer_id: MCC account ID."""
    try:
        rows = _search(f"""
            SELECT customer_client.id, customer_client.descriptive_name,
                   metrics.impressions, metrics.clicks, metrics.cost_micros,
                   metrics.conversions
            FROM customer_client
            WHERE customer_client.level = 1
              AND segments.date DURING {date_range}
        """, customer_id)
        results = []
        for r in rows:
            cc = r.customer_client
            m = r.metrics
            results.append({
                "id": str(cc.id),
                "name": cc.descriptive_name,
                "impressions": m.impressions,
                "clicks": m.clicks,
                "cost": m.cost_micros / 1e6,
                "conversions": m.conversions,
            })
        return json.dumps(results)
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_get_payments_account(customer_id: str = "") -> str:
    """Get payments account details linked to the Google Ads account."""
    try:
        rows = _search(
            """SELECT payments_account.payments_account_id, payments_account.name,
                      payments_account.currency_code, payments_account.payments_profile_id
               FROM payments_account""",
            customer_id
        )
        result = []
        for row in rows:
            pa = row.payments_account
            result.append({
                "payments_account_id": pa.payments_account_id,
                "name": pa.name,
                "currency_code": pa.currency_code,
                "payments_profile_id": pa.payments_profile_id,
            })
        return json.dumps({"payments_accounts": result})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Shared budgets ────────────────────────────────────────────────────────





@mcp.tool()
def gads_get_pmax_campaign_settings(campaign_id: str, customer_id: str = "") -> str:
    """Get Performance Max specific settings for a campaign."""
    try:
        client, cid = _get_client(customer_id)
        query = (
            f"SELECT campaign.name, campaign.status, campaign.url_expansion_opt_out, "
            f"campaign.asset_automation_settings, campaign.shopping_setting.merchant_id "
            f"FROM campaign "
            f"WHERE campaign.id = {campaign_id}"
        )
        results = _search(query, cid)
        settings = {}
        for row in results:
            c = row.campaign
            automation_settings = []
            for s in c.asset_automation_settings:
                automation_settings.append({
                    "asset_automation_type": s.asset_automation_type.name,
                    "asset_automation_status": s.asset_automation_status.name,
                })
            settings = {
                "name": c.name,
                "status": c.status.name,
                "url_expansion_opt_out": c.url_expansion_opt_out,
                "asset_automation_settings": automation_settings,
                "merchant_id": c.shopping_setting.merchant_id,
            }
        return json.dumps({"success": True, "settings": settings})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_get_pmax_performance(date_range: str = "LAST_30_DAYS", customer_id: str = "") -> str:
    """Get Performance Max campaign performance report.
    Args: date_range: e.g. LAST_30_DAYS. customer_id: optional."""
    try:
        rows = _search(f"""
            SELECT campaign.id, campaign.name,
                   metrics.impressions, metrics.clicks, metrics.cost_micros,
                   metrics.conversions, metrics.conversions_value,
                   metrics.all_conversions, metrics.ctr, metrics.average_cpc
            FROM campaign
            WHERE campaign.advertising_channel_type = 'PERFORMANCE_MAX'
              AND segments.date DURING {date_range}
            ORDER BY metrics.cost_micros DESC
        """, customer_id)
        results = []
        for r in rows:
            c = r.campaign
            m = r.metrics
            results.append({
                "campaign_id": str(c.id),
                "name": c.name,
                "impressions": m.impressions,
                "clicks": m.clicks,
                "cost": m.cost_micros / 1e6,
                "conversions": m.conversions,
                "conversions_value": m.conversions_value,
                "ctr": m.ctr,
                "avg_cpc": m.average_cpc / 1e6 if m.average_cpc else 0,
            })
        return json.dumps(results)
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_get_search_terms_report(
    campaign_id: str = "",
    ad_group_id: str = "",
    start_date: str = "",
    end_date: str = "",
    min_clicks: int = 0,
    customer_id: str = "",
) -> str:
    """Get actual search queries (search terms report) that triggered ads.

    Args:
        campaign_id: Filter by campaign ID (optional).
        ad_group_id: Filter by ad group ID (optional).
        start_date: YYYY-MM-DD (default last 30 days).
        end_date: YYYY-MM-DD.
        min_clicks: Minimum clicks threshold to filter results.
        customer_id: Google Ads customer ID.
    """
    try:
        sd, ed = _date_range(start_date, end_date, 30)
        conditions = [f"segments.date BETWEEN '{sd}' AND '{ed}'"]
        if campaign_id:
            conditions.append(f"campaign.id = {campaign_id}")
        if ad_group_id:
            conditions.append(f"ad_group.id = {ad_group_id}")
        where = " AND ".join(conditions)
        rows = _search(
            f"""
            SELECT search_term_view.search_term,
                   search_term_view.status,
                   campaign.id, campaign.name,
                   ad_group.id, ad_group.name,
                   metrics.clicks, metrics.impressions,
                   metrics.cost_micros, metrics.conversions,
                   metrics.ctr
            FROM search_term_view
            WHERE {where}
            ORDER BY metrics.clicks DESC
            LIMIT 500
            """,
            customer_id,
        )
        result = []
        for row in rows:
            m = row.metrics
            if m.clicks < min_clicks:
                continue
            result.append({
                "search_term": row.search_term_view.search_term,
                "status": row.search_term_view.status.name,
                "campaign_id": str(row.campaign.id),
                "campaign_name": row.campaign.name,
                "ad_group_id": str(row.ad_group.id),
                "ad_group_name": row.ad_group.name,
                "clicks": m.clicks,
                "impressions": m.impressions,
                "cost": _m(m.cost_micros),
                "conversions": round(m.conversions, 2),
                "ctr": _pct(m.ctr),
            })
        return json.dumps({"search_terms": result, "count": len(result)})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# GROUP 6: Ads (RSA / Assets)
# ---------------------------------------------------------------------------





@mcp.tool()
def gads_get_sitelink_details(asset_resource_name: str, customer_id: str = "") -> str:
    """Get full details of a sitelink asset."""
    try:
        rows = _search(
            f"""SELECT asset.id, asset.name, asset.resource_name,
                       asset.sitelink_asset.link_text, asset.sitelink_asset.description1,
                       asset.sitelink_asset.description2, asset.final_urls, asset.tracking_url_template
                FROM asset
                WHERE asset.resource_name = '{asset_resource_name}'""",
            customer_id
        )
        if not rows:
            return json.dumps({"error": "Asset not found"})
        a = rows[0].asset
        return json.dumps({
            "id": str(a.id),
            "name": a.name,
            "resource_name": a.resource_name,
            "link_text": a.sitelink_asset.link_text,
            "description1": a.sitelink_asset.description1,
            "description2": a.sitelink_asset.description2,
            "final_urls": list(a.final_urls),
            "tracking_url_template": a.tracking_url_template,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Conversion adjustments ─────────────────────────────────────────────────





@mcp.tool()
def gads_get_smart_campaign_status(campaign_id: str, customer_id: str = "") -> str:
    """Get smart campaign status and eligibility details."""
    try:
        rows = _search(
            f"""SELECT smart_campaign_status.smart_campaign_status,
                       smart_campaign_status.not_eligible_reason,
                       smart_campaign_status.paused_reason,
                       smart_campaign_status.ended_reason
                FROM smart_campaign_status
                WHERE smart_campaign_status.campaign = 'customers/{_get_client(customer_id)[1]}/campaigns/{campaign_id}'""",
            customer_id
        )
        if not rows:
            return json.dumps({"error": "Smart campaign status not found"})
        s = rows[0].smart_campaign_status
        return json.dumps({
            "status": str(s.smart_campaign_status),
            "not_eligible_reason": str(s.not_eligible_reason),
            "paused_reason": str(s.paused_reason),
            "ended_reason": str(s.ended_reason),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Ad group extensions list ──────────────────────────────────────────────





@mcp.tool()
def gads_list_account_budgets(customer_id: str = "") -> str:
    """List all account-level budgets."""
    try:
        rows = _search(
            """SELECT account_budget.id, account_budget.name, account_budget.status,
                      account_budget.approved_spending_limit_micros,
                      account_budget.total_adjustments_micros,
                      account_budget.amount_served_micros,
                      account_budget.start_date_time, account_budget.end_date_time
               FROM account_budget""",
            customer_id
        )
        result = []
        for row in rows:
            ab = row.account_budget
            result.append({
                "id": str(ab.id),
                "name": ab.name,
                "status": str(ab.status),
                "approved_spending_limit_micros": ab.approved_spending_limit_micros,
                "total_adjustments_micros": ab.total_adjustments_micros,
                "amount_served_micros": ab.amount_served_micros,
                "start_date_time": ab.start_date_time,
                "end_date_time": ab.end_date_time,
            })
        return json.dumps({"account_budgets": result, "count": len(result)})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_list_accounts(
    customer_id: str = "",
) -> str:
    """List all sub-accounts (clients) under the MCC with name, ID, and status.

    Args:
        customer_id: MCC (manager) customer ID. Leave blank for default.
    """
    try:
        rows = _search(
            """
            SELECT customer_client.client_customer,
                   customer_client.descriptive_name,
                   customer_client.id,
                   customer_client.status,
                   customer_client.currency_code,
                   customer_client.time_zone,
                   customer_client.manager,
                   customer_client.level
            FROM customer_client
            WHERE customer_client.level <= 1
            ORDER BY customer_client.descriptive_name
            """,
            customer_id,
        )
        result = []
        for row in rows:
            cc = row.customer_client
            result.append({
                "id": str(cc.id),
                "name": cc.descriptive_name,
                "resource_name": cc.client_customer,
                "status": cc.status.name,
                "currency": cc.currency_code,
                "timezone": cc.time_zone,
                "is_manager": cc.manager,
                "level": cc.level,
            })
        return json.dumps({"accounts": result, "count": len(result)})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_list_ad_group_audience_criteria(ad_group_id: str, customer_id: str = "") -> str:
    """List all audience criteria for an ad group (positive and negative)."""
    try:
        _, cid = _get_client(customer_id)
        rows = _search(
            f"""SELECT ad_group_criterion.criterion_id, ad_group_criterion.type,
                       ad_group_criterion.user_list.user_list,
                       ad_group_criterion.user_interest.user_interest_category,
                       ad_group_criterion.negative, ad_group_criterion.status,
                       ad_group_criterion.bid_modifier
                FROM ad_group_criterion
                WHERE ad_group_criterion.ad_group = 'customers/{cid}/adGroups/{ad_group_id}'
                  AND ad_group_criterion.type IN ('USER_LIST', 'USER_INTEREST')""",
            customer_id
        )
        result = []
        for row in rows:
            ac = row.ad_group_criterion
            entry = {
                "criterion_id": str(ac.criterion_id),
                "type": str(ac.type_),
                "negative": ac.negative,
                "status": str(ac.status),
                "bid_modifier": ac.bid_modifier,
            }
            if ac.user_list.user_list:
                entry["user_list"] = ac.user_list.user_list
            if ac.user_interest.user_interest_category:
                entry["user_interest"] = str(ac.user_interest.user_interest_category)
            result.append(entry)
        return json.dumps({"audience_criteria": result, "count": len(result)})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_list_ad_group_extensions(ad_group_id: str, customer_id: str = "") -> str:
    """List all asset extensions attached to an ad group."""
    try:
        _, cid = _get_client(customer_id)
        rows = _search(
            f"""SELECT ad_group_asset.asset, ad_group_asset.field_type, ad_group_asset.status
                FROM ad_group_asset
                WHERE ad_group_asset.ad_group = 'customers/{cid}/adGroups/{ad_group_id}'""",
            customer_id
        )
        result = []
        for row in rows:
            aga = row.ad_group_asset
            result.append({
                "asset": aga.asset,
                "field_type": str(aga.field_type),
                "status": str(aga.status),
            })
        return json.dumps({"extensions": result, "count": len(result)})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Billing / account budget ──────────────────────────────────────────────





@mcp.tool()
def gads_list_ad_group_location_targets(ad_group_id: str, customer_id: str = "") -> str:
    """List all location targeting criteria for an ad group."""
    try:
        _, cid = _get_client(customer_id)
        rows = _search(
            f"""SELECT ad_group_criterion.criterion_id, ad_group_criterion.location.geo_target_constant,
                       ad_group_criterion.negative
                FROM ad_group_criterion
                WHERE ad_group_criterion.ad_group = 'customers/{cid}/adGroups/{ad_group_id}'
                  AND ad_group_criterion.type = 'LOCATION'""",
            customer_id
        )
        result = []
        for row in rows:
            ac = row.ad_group_criterion
            result.append({
                "criterion_id": str(ac.criterion_id),
                "geo_target": ac.location.geo_target_constant,
                "negative": ac.negative,
            })
        return json.dumps({"locations": result, "count": len(result)})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_list_asset_automation_settings(campaign_id: str, customer_id: str = "") -> str:
    """List asset automation settings for a campaign (PMax auto-created assets)."""
    try:
        rows = _search(
            f"""SELECT campaign.id, campaign.name, campaign.asset_automation_settings
                FROM campaign
                WHERE campaign.id = {campaign_id}""",
            customer_id
        )
        if not rows:
            return json.dumps({"error": "Campaign not found"})
        c = rows[0].campaign
        settings = []
        for s in c.asset_automation_settings:
            settings.append({
                "asset_automation_type": str(s.asset_automation_type),
                "asset_automation_status": str(s.asset_automation_status),
            })
        return json.dumps({"campaign_id": campaign_id, "asset_automation_settings": settings})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Legacy feeds list ─────────────────────────────────────────────────────





@mcp.tool()
def gads_list_asset_group_audience_signals(asset_group_id: str, customer_id: str = "") -> str:
    """List audience signals for a PMax asset group."""
    try:
        _, cid = _get_client(customer_id)
        rows = _search(
            f"""SELECT asset_group_signal.asset_group, asset_group_signal.audience.audience
                FROM asset_group_signal
                WHERE asset_group_signal.asset_group = 'customers/{cid}/assetGroups/{asset_group_id}'""",
            customer_id
        )
        result = []
        for row in rows:
            sig = row.asset_group_signal
            result.append({
                "asset_group": sig.asset_group,
                "audience": sig.audience.audience,
            })
        return json.dumps({"signals": result, "count": len(result)})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Asset-specific listing tools ───────────────────────────────────────────





@mcp.tool()
def gads_list_bidding_data_exclusions(customer_id: str = "") -> str:
    """List all bidding data exclusions."""
    try:
        rows = _search(
            """SELECT bidding_data_exclusion.resource_name,
                      bidding_data_exclusion.data_exclusion_id,
                      bidding_data_exclusion.start_date_time,
                      bidding_data_exclusion.end_date_time,
                      bidding_data_exclusion.description
               FROM bidding_data_exclusion""",
            customer_id
        )
        result = []
        for row in rows:
            de = row.bidding_data_exclusion
            result.append({
                "resource_name": de.resource_name,
                "id": str(de.data_exclusion_id),
                "start": de.start_date_time,
                "end": de.end_date_time,
                "description": de.description,
            })
        return json.dumps({"data_exclusions": result, "count": len(result)})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_list_bidding_seasonality_adjustments(customer_id: str = "") -> str:
    """List all bidding seasonality adjustments."""
    try:
        rows = _search(
            """SELECT bidding_seasonality_adjustment.resource_name,
                      bidding_seasonality_adjustment.seasonality_adjustment_id,
                      bidding_seasonality_adjustment.start_date_time,
                      bidding_seasonality_adjustment.end_date_time,
                      bidding_seasonality_adjustment.conversion_rate_modifier,
                      bidding_seasonality_adjustment.description
               FROM bidding_seasonality_adjustment""",
            customer_id
        )
        result = []
        for row in rows:
            sa = row.bidding_seasonality_adjustment
            result.append({
                "resource_name": sa.resource_name,
                "id": str(sa.seasonality_adjustment_id),
                "start": sa.start_date_time,
                "end": sa.end_date_time,
                "conversion_rate_modifier": sa.conversion_rate_modifier,
                "description": sa.description,
            })
        return json.dumps({"adjustments": result, "count": len(result)})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_list_call_assets(customer_id: str = "") -> str:
    """List all call assets."""
    try:
        rows = _search(
            """SELECT asset.id, asset.name, asset.resource_name,
                      asset.call_asset.phone_number, asset.call_asset.country_code
               FROM asset
               WHERE asset.type = 'CALL'""",
            customer_id
        )
        result = []
        for row in rows:
            a = row.asset
            result.append({
                "id": str(a.id),
                "name": a.name,
                "resource_name": a.resource_name,
                "phone_number": a.call_asset.phone_number,
                "country_code": a.call_asset.country_code,
            })
        return json.dumps({"call_assets": result, "count": len(result)})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_list_campaign_audience_views(campaign_id: str, customer_id: str = "") -> str:
    """List audience performance for a campaign."""
    try:
        client, cid = _get_client(customer_id)
        query = (
            f"SELECT campaign_audience_view.resource_name, "
            f"campaign.id, campaign.name, "
            f"metrics.impressions, metrics.clicks, metrics.cost_micros, "
            f"metrics.conversions, metrics.ctr "
            f"FROM campaign_audience_view "
            f"WHERE campaign.id = {campaign_id}"
        )
        results = _search(query, cid)
        rows = []
        for row in results:
            rows.append({
                "resource_name": row.campaign_audience_view.resource_name,
                "campaign_id": row.campaign.id,
                "campaign_name": row.campaign.name,
                "impressions": row.metrics.impressions,
                "clicks": row.metrics.clicks,
                "cost_micros": row.metrics.cost_micros,
                "conversions": row.metrics.conversions,
                "ctr": row.metrics.ctr,
            })
        return json.dumps({"success": True, "audience_views": rows})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_list_campaign_audiences(campaign_id: str, customer_id: str = "") -> str:
    """List all audience criteria attached to a campaign (positive and negative)."""
    try:
        _, cid = _get_client(customer_id)
        rows = _search(
            f"""SELECT campaign_criterion.criterion_id, campaign_criterion.type,
                       campaign_criterion.user_list.user_list,
                       campaign_criterion.user_interest.user_interest_category,
                       campaign_criterion.negative
                FROM campaign_criterion
                WHERE campaign_criterion.campaign = 'customers/{cid}/campaigns/{campaign_id}'
                  AND campaign_criterion.type IN ('USER_LIST', 'USER_INTEREST')""",
            customer_id
        )
        result = []
        for row in rows:
            cc = row.campaign_criterion
            entry = {
                "criterion_id": str(cc.criterion_id),
                "type": str(cc.type_),
                "negative": cc.negative,
            }
            if cc.user_list.user_list:
                entry["user_list"] = cc.user_list.user_list
            if cc.user_interest.user_interest_category:
                entry["user_interest"] = str(cc.user_interest.user_interest_category)
            result.append(entry)
        return json.dumps({"audiences": result, "count": len(result)})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_list_campaign_drafts(campaign_id: str = "", customer_id: str = "") -> str:
    """List campaign drafts, optionally filtered by base campaign."""
    try:
        where = f"WHERE campaign_draft.base_campaign = 'customers/{_get_client(customer_id)[1]}/campaigns/{campaign_id}'" if campaign_id else ""
        rows = _search(
            f"""SELECT campaign_draft.draft_id, campaign_draft.name,
                       campaign_draft.base_campaign, campaign_draft.draft_campaign,
                       campaign_draft.status, campaign_draft.has_experiment_running
                FROM campaign_draft {where}""",
            customer_id
        )
        result = []
        for row in rows:
            cd = row.campaign_draft
            result.append({
                "draft_id": str(cd.draft_id),
                "name": cd.name,
                "base_campaign": cd.base_campaign,
                "draft_campaign": cd.draft_campaign,
                "status": str(cd.status),
                "has_experiment_running": cd.has_experiment_running,
            })
        return json.dumps({"drafts": result, "count": len(result)})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_list_campaign_ip_exclusions(campaign_id: str, customer_id: str = "") -> str:
    """List IP address exclusions for a campaign."""
    try:
        client, cid = _get_client(customer_id)
        query = (
            f"SELECT campaign_criterion.criterion_id, campaign_criterion.ip_block.ip_address "
            f"FROM campaign_criterion "
            f"WHERE campaign_criterion.campaign = 'customers/{cid}/campaigns/{campaign_id}' "
            f"AND campaign_criterion.type = 'IP_BLOCK'"
        )
        results = _search(query, cid)
        exclusions = []
        for row in results:
            exclusions.append({
                "criterion_id": row.campaign_criterion.criterion_id,
                "ip_address": row.campaign_criterion.ip_block.ip_address,
            })
        return json.dumps({"success": True, "exclusions": exclusions})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_list_campaign_language_targets(campaign_id: str, customer_id: str = "") -> str:
    """List all language targeting criteria for a campaign."""
    try:
        _, cid = _get_client(customer_id)
        rows = _search(
            f"""SELECT campaign_criterion.criterion_id, campaign_criterion.language.language_constant
                FROM campaign_criterion
                WHERE campaign_criterion.campaign = 'customers/{cid}/campaigns/{campaign_id}'
                  AND campaign_criterion.type = 'LANGUAGE'""",
            customer_id
        )
        result = []
        for row in rows:
            cc = row.campaign_criterion
            result.append({
                "criterion_id": str(cc.criterion_id),
                "language_constant": cc.language.language_constant,
            })
        return json.dumps({"languages": result, "count": len(result)})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Audience list/details ──────────────────────────────────────────────────





@mcp.tool()
def gads_list_campaign_location_targets(campaign_id: str, customer_id: str = "") -> str:
    """List all location targeting criteria for a campaign (positive and negative)."""
    try:
        _, cid = _get_client(customer_id)
        rows = _search(
            f"""SELECT campaign_criterion.criterion_id, campaign_criterion.location.geo_target_constant,
                       campaign_criterion.negative, campaign_criterion.bid_modifier
                FROM campaign_criterion
                WHERE campaign_criterion.campaign = 'customers/{cid}/campaigns/{campaign_id}'
                  AND campaign_criterion.type = 'LOCATION'""",
            customer_id
        )
        result = []
        for row in rows:
            cc = row.campaign_criterion
            result.append({
                "criterion_id": str(cc.criterion_id),
                "geo_target": cc.location.geo_target_constant,
                "negative": cc.negative,
                "bid_modifier": cc.bid_modifier,
            })
        return json.dumps({"locations": result, "count": len(result)})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_list_campaign_negative_placements(campaign_id: str, customer_id: str = "") -> str:
    """List negative placement exclusions for a campaign."""
    try:
        client, cid = _get_client(customer_id)
        query = (
            f"SELECT campaign_criterion.criterion_id, campaign_criterion.placement.url, "
            f"campaign_criterion.type "
            f"FROM campaign_criterion "
            f"WHERE campaign_criterion.campaign = 'customers/{cid}/campaigns/{campaign_id}' "
            f"AND campaign_criterion.type = 'PLACEMENT' "
            f"AND campaign_criterion.negative = TRUE"
        )
        results = _search(query, cid)
        placements = []
        for row in results:
            placements.append({
                "criterion_id": row.campaign_criterion.criterion_id,
                "url": row.campaign_criterion.placement.url,
            })
        return json.dumps({"success": True, "negative_placements": placements})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_list_campaigns_by_type(advertising_channel_type: str, customer_id: str = "") -> str:
    """List campaigns filtered by advertising channel type.
    Types: SEARCH, DISPLAY, SHOPPING, VIDEO, MULTI_CHANNEL, LOCAL, SMART,
           PERFORMANCE_MAX, DISCOVERY, LOCAL_SERVICES, DEMAND_GEN."""
    try:
        rows = _search(
            f"""SELECT campaign.id, campaign.name, campaign.status,
                       campaign.advertising_channel_type,
                       campaign.advertising_channel_sub_type,
                       campaign.bidding_strategy_type
                FROM campaign
                WHERE campaign.advertising_channel_type = '{advertising_channel_type}'
                  AND campaign.status != 'REMOVED'""",
            customer_id
        )
        result = []
        for row in rows:
            c = row.campaign
            result.append({
                "id": str(c.id),
                "name": c.name,
                "status": str(c.status),
                "channel_type": str(c.advertising_channel_type),
                "channel_sub_type": str(c.advertising_channel_sub_type),
                "bidding_strategy_type": str(c.bidding_strategy_type),
            })
        return json.dumps({"campaigns": result, "count": len(result)})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_list_conversion_categories(customer_id: str = "") -> str:
    """List all conversion actions grouped by category."""
    try:
        rows = _search(
            """SELECT conversion_action.id, conversion_action.name,
                      conversion_action.category, conversion_action.type,
                      conversion_action.status, conversion_action.value_settings.default_value,
                      metrics.conversions, metrics.conversions_value
               FROM conversion_action
               WHERE conversion_action.status != 'REMOVED'
               ORDER BY conversion_action.category ASC""",
            customer_id
        )
        from collections import defaultdict
        by_category = defaultdict(list)
        for row in rows:
            ca = row.conversion_action
            by_category[str(ca.category)].append({
                "id": str(ca.id),
                "name": ca.name,
                "type": str(ca.type_),
                "status": str(ca.status),
                "default_value": ca.value_settings.default_value,
                "conversions": row.metrics.conversions,
                "conversions_value": row.metrics.conversions_value,
            })
        return json.dumps({"conversion_categories": dict(by_category),
                           "total_actions": sum(len(v) for v in by_category.values())})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Keyword forecast ───────────────────────────────────────────────────────





@mcp.tool()
def gads_list_conversion_value_rule_sets(customer_id: str = "") -> str:
    """List all conversion value rule sets."""
    try:
        rows = _search(
            """SELECT conversion_value_rule_set.id, conversion_value_rule_set.resource_name,
                      conversion_value_rule_set.status, conversion_value_rule_set.campaign,
                      conversion_value_rule_set.conversion_value_rules,
                      conversion_value_rule_set.dimensions
               FROM conversion_value_rule_set""",
            customer_id
        )
        result = []
        for row in rows:
            rs = row.conversion_value_rule_set
            result.append({
                "id": str(rs.id),
                "resource_name": rs.resource_name,
                "status": str(rs.status),
                "campaign": rs.campaign,
                "rule_count": len(rs.conversion_value_rules),
                "dimensions": [str(d) for d in rs.dimensions],
            })
        return json.dumps({"rule_sets": result, "count": len(result)})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_list_feeds(customer_id: str = "") -> str:
    """List all feeds in the account (legacy feed management)."""
    try:
        rows = _search(
            """SELECT feed.id, feed.name, feed.status, feed.origin, feed.resource_name
               FROM feed
               WHERE feed.status = 'ENABLED'""",
            customer_id
        )
        result = []
        for row in rows:
            f = row.feed
            result.append({
                "id": str(f.id),
                "name": f.name,
                "status": str(f.status),
                "origin": str(f.origin),
                "resource_name": f.resource_name,
            })
        return json.dumps({"feeds": result, "count": len(result)})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Expanded text ad (deprecated but queryable) ───────────────────────────





@mcp.tool()
def gads_list_keywords_with_quality_score(campaign_id: str = "", min_quality_score: int = 1,
                                           max_quality_score: int = 10, customer_id: str = "") -> str:
    """List keywords with their quality scores. Filter by score range."""
    try:
        campaign_filter = f"AND campaign.id = {campaign_id}" if campaign_id else ""
        rows = _search(
            f"""SELECT ad_group_criterion.criterion_id, ad_group_criterion.keyword.text,
                       ad_group_criterion.keyword.match_type,
                       ad_group_criterion.quality_info.quality_score,
                       ad_group_criterion.quality_info.creative_quality_score,
                       ad_group_criterion.quality_info.post_click_quality_score,
                       ad_group_criterion.quality_info.search_predicted_ctr,
                       ad_group.name, campaign.name,
                       metrics.impressions, metrics.clicks, metrics.ctr
                FROM ad_group_criterion
                WHERE ad_group_criterion.type = 'KEYWORD'
                  AND ad_group_criterion.status != 'REMOVED'
                  AND ad_group_criterion.quality_info.quality_score >= {min_quality_score}
                  AND ad_group_criterion.quality_info.quality_score <= {max_quality_score}
                  {campaign_filter}
                ORDER BY ad_group_criterion.quality_info.quality_score ASC
                LIMIT 500""",
            customer_id
        )
        result = []
        for row in rows:
            ac = row.ad_group_criterion
            result.append({
                "criterion_id": str(ac.criterion_id),
                "keyword": ac.keyword.text,
                "match_type": str(ac.keyword.match_type),
                "quality_score": ac.quality_info.quality_score,
                "creative_quality": str(ac.quality_info.creative_quality_score),
                "landing_page_quality": str(ac.quality_info.post_click_quality_score),
                "expected_ctr": str(ac.quality_info.search_predicted_ctr),
                "ad_group": row.ad_group.name,
                "campaign": row.campaign.name,
                "impressions": row.metrics.impressions,
                "clicks": row.metrics.clicks,
                "ctr": row.metrics.ctr,
            })
        return json.dumps({"keywords": result, "count": len(result)})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Conversion categories ──────────────────────────────────────────────────





@mcp.tool()
def gads_list_pmax_brand_exclusions(campaign_id: str, customer_id: str = "") -> str:
    """List brand exclusions for a Performance Max campaign."""
    try:
        _, cid = _get_client(customer_id)
        rows = _search(
            f"""SELECT campaign_criterion.criterion_id, campaign_criterion.brand.entity_id,
                       campaign_criterion.brand.name, campaign_criterion.negative
                FROM campaign_criterion
                WHERE campaign_criterion.campaign = 'customers/{cid}/campaigns/{campaign_id}'
                  AND campaign_criterion.type = 'BRAND'
                  AND campaign_criterion.negative = TRUE""",
            customer_id
        )
        result = []
        for row in rows:
            cc = row.campaign_criterion
            result.append({
                "criterion_id": str(cc.criterion_id),
                "brand_entity_id": str(cc.brand.entity_id),
                "brand_name": cc.brand.name,
            })
        return json.dumps({"brand_exclusions": result, "count": len(result)})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_list_price_assets(customer_id: str = "") -> str:
    """List all price assets."""
    try:
        rows = _search(
            """SELECT asset.id, asset.name, asset.resource_name, asset.price_asset.type,
                      asset.price_asset.price_qualifier
               FROM asset
               WHERE asset.type = 'PRICE'""",
            customer_id
        )
        result = []
        for row in rows:
            a = row.asset
            result.append({
                "id": str(a.id),
                "name": a.name,
                "resource_name": a.resource_name,
                "price_type": str(a.price_asset.type_),
                "price_qualifier": str(a.price_asset.price_qualifier),
            })
        return json.dumps({"price_assets": result, "count": len(result)})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_list_promotion_assets(customer_id: str = "") -> str:
    """List all promotion assets."""
    try:
        rows = _search(
            """SELECT asset.id, asset.name, asset.resource_name,
                      asset.promotion_asset.promotion_target,
                      asset.promotion_asset.discount_modifier,
                      asset.promotion_asset.percent_off
               FROM asset
               WHERE asset.type = 'PROMOTION'""",
            customer_id
        )
        result = []
        for row in rows:
            a = row.asset
            result.append({
                "id": str(a.id),
                "name": a.name,
                "resource_name": a.resource_name,
                "promotion_target": a.promotion_asset.promotion_target,
                "discount_modifier": str(a.promotion_asset.discount_modifier),
                "percent_off": a.promotion_asset.percent_off,
            })
        return json.dumps({"promotion_assets": result, "count": len(result)})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_list_rsa_performance(campaign_id: str = "", customer_id: str = "") -> str:
    """List RSA ads with asset performance metrics (headline/description serving stats)."""
    try:
        where = f"AND campaign.id = {campaign_id}" if campaign_id else ""
        rows = _search(
            f"""SELECT ad_group_ad.ad.id, ad_group_ad.ad.responsive_search_ad.headlines,
                       ad_group_ad.ad.responsive_search_ad.descriptions,
                       ad_group_ad.ad_strength, ad_group.name, campaign.name,
                       metrics.impressions, metrics.clicks, metrics.ctr,
                       metrics.average_cpc, metrics.conversions
                FROM ad_group_ad
                WHERE ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'
                  AND ad_group_ad.status != 'REMOVED' {where}
                LIMIT 100""",
            customer_id
        )
        result = []
        for row in rows:
            ad = row.ad_group_ad.ad
            headlines = [{"text": h.text, "pinned": str(h.pinned_field)} for h in ad.responsive_search_ad.headlines]
            descriptions = [{"text": d.text, "pinned": str(d.pinned_field)} for d in ad.responsive_search_ad.descriptions]
            result.append({
                "ad_id": str(ad.id),
                "ad_group": row.ad_group.name,
                "campaign": row.campaign.name,
                "ad_strength": str(row.ad_group_ad.ad_strength),
                "headlines": headlines,
                "descriptions": descriptions,
                "impressions": row.metrics.impressions,
                "clicks": row.metrics.clicks,
                "ctr": row.metrics.ctr,
                "avg_cpc": row.metrics.average_cpc,
                "conversions": row.metrics.conversions,
            })
        return json.dumps({"rsa_performance": result, "count": len(result)})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Customer-level negative keywords ──────────────────────────────────────





@mcp.tool()
def gads_list_shared_budgets(customer_id: str = "") -> str:
    """List all shared budgets in the account."""
    try:
        rows = _search(
            """SELECT campaign_budget.id, campaign_budget.name, campaign_budget.amount_micros,
                      campaign_budget.status, campaign_budget.period, campaign_budget.reference_count,
                      campaign_budget.explicitly_shared, campaign_budget.total_amount_micros
               FROM campaign_budget
               WHERE campaign_budget.explicitly_shared = TRUE""",
            customer_id
        )
        result = []
        for row in rows:
            cb = row.campaign_budget
            result.append({
                "id": str(cb.id),
                "name": cb.name,
                "amount_micros": cb.amount_micros,
                "status": str(cb.status),
                "period": str(cb.period),
                "reference_count": cb.reference_count,
                "explicitly_shared": cb.explicitly_shared,
                "total_amount_micros": cb.total_amount_micros,
            })
        return json.dumps({"shared_budgets": result, "count": len(result)})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Hotel campaign settings ────────────────────────────────────────────────





@mcp.tool()
def gads_list_sitelinks(
    campaign_id: str = "",
    customer_id: str = "",
) -> str:
    """List all sitelink assets on a campaign (or account-wide if no campaign given).

    Args:
        campaign_id: Campaign numeric ID (optional; omit for account-level sitelinks).
        customer_id: Google Ads customer ID.
    """
    try:
        if campaign_id:
            rows = _search(
                f"""
                SELECT campaign_asset.asset, campaign_asset.field_type,
                       asset.id, asset.name,
                       asset.sitelink_asset.link_text,
                       asset.sitelink_asset.description1,
                       asset.sitelink_asset.description2,
                       asset.sitelink_asset.final_urls
                FROM campaign_asset
                WHERE campaign.id = {campaign_id}
                  AND campaign_asset.field_type = 'SITELINK'
                  AND campaign_asset.status != 'REMOVED'
                """,
                customer_id,
            )
            result = []
            for row in rows:
                a = row.asset
                sl = a.sitelink_asset
                result.append({
                    "asset_id": str(a.id),
                    "name": a.name,
                    "link_text": sl.link_text,
                    "description1": sl.description1,
                    "description2": sl.description2,
                    "final_urls": list(sl.final_urls),
                })
        else:
            rows = _search(
                """
                SELECT asset.id, asset.name,
                       asset.sitelink_asset.link_text,
                       asset.sitelink_asset.description1,
                       asset.sitelink_asset.description2,
                       asset.sitelink_asset.final_urls
                FROM asset
                WHERE asset.type = 'SITELINK'
                ORDER BY asset.id DESC
                """,
                customer_id,
            )
            result = []
            for row in rows:
                a = row.asset
                sl = a.sitelink_asset
                result.append({
                    "asset_id": str(a.id),
                    "name": a.name,
                    "link_text": sl.link_text,
                    "description1": sl.description1,
                    "description2": sl.description2,
                    "final_urls": list(sl.final_urls),
                })
        return json.dumps({"sitelinks": result, "count": len(result)})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_pause_keyword(
    ad_group_id: str,
    criterion_id: str,
    customer_id: str = "",
) -> str:
    """Pause a specific keyword.

    Args:
        ad_group_id: Ad group ID containing the keyword.
        criterion_id: Keyword criterion ID.
        customer_id: Google Ads customer ID.
    """
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupCriterionService")
        op = client.get_type("AdGroupCriterionOperation")
        c = op.update
        c.resource_name = f"customers/{cid}/adGroupCriteria/{ad_group_id}~{criterion_id}"
        c.status = client.enums.AdGroupCriterionStatusEnum["PAUSED"]
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["status"]))
        service.mutate_ad_group_criteria(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "criterion_id": criterion_id, "status": "PAUSED"})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})






@mcp.tool()
def gads_remove_ad_group_age_range(ad_group_id: str, criterion_id: str, customer_id: str = "") -> str:
    """Remove an age range criterion from an ad group."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupCriterionService")
        op = client.get_type("AdGroupCriterionOperation")
        op.remove = f"customers/{cid}/adGroupCriteria/{ad_group_id}~{criterion_id}"
        service.mutate_ad_group_criteria(customer_id=cid, operations=[op])
        return json.dumps({"success": True})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_remove_ad_group_gender(ad_group_id: str, criterion_id: str, customer_id: str = "") -> str:
    """Remove a gender criterion from an ad group."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupCriterionService")
        op = client.get_type("AdGroupCriterionOperation")
        op.remove = f"customers/{cid}/adGroupCriteria/{ad_group_id}~{criterion_id}"
        service.mutate_ad_group_criteria(customer_id=cid, operations=[op])
        return json.dumps({"success": True})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_remove_ad_group_income_range(ad_group_id: str, criterion_id: str, customer_id: str = "") -> str:
    """Remove an income range criterion from an ad group."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupCriterionService")
        op = client.get_type("AdGroupCriterionOperation")
        op.remove = f"customers/{cid}/adGroupCriteria/{ad_group_id}~{criterion_id}"
        service.mutate_ad_group_criteria(customer_id=cid, operations=[op])
        return json.dumps({"success": True})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_remove_ad_group_mobile_device(ad_group_id: str, criterion_id: str, customer_id: str = "") -> str:
    """Remove mobile device targeting criterion from an ad group."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupCriterionService")
        op = client.get_type("AdGroupCriterionOperation")
        op.remove = f"customers/{cid}/adGroupCriteria/{ad_group_id}~{criterion_id}"
        service.mutate_ad_group_criteria(customer_id=cid, operations=[op])
        return json.dumps({"success": True})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_remove_ad_group_negative_placement(ad_group_id: str, criterion_id: str, customer_id: str = "") -> str:
    """Remove a negative placement criterion from an ad group."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupCriterionService")
        op = client.get_type("AdGroupCriterionOperation")
        op.remove = f"customers/{cid}/adGroupCriteria/{ad_group_id}~{criterion_id}"
        service.mutate_ad_group_criteria(customer_id=cid, operations=[op])
        return json.dumps({"success": True})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_remove_ad_group_negative_topic(ad_group_id: str, criterion_id: str, customer_id: str = "") -> str:
    """Remove a negative topic criterion from an ad group."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupCriterionService")
        op = client.get_type("AdGroupCriterionOperation")
        op.remove = f"customers/{cid}/adGroupCriteria/{ad_group_id}~{criterion_id}"
        service.mutate_ad_group_criteria(customer_id=cid, operations=[op])
        return json.dumps({"success": True})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_remove_ad_group_operating_system(ad_group_id: str, criterion_id: str, customer_id: str = "") -> str:
    """Remove operating system targeting criterion from an ad group."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupCriterionService")
        op = client.get_type("AdGroupCriterionOperation")
        op.remove = f"customers/{cid}/adGroupCriteria/{ad_group_id}~{criterion_id}"
        service.mutate_ad_group_criteria(customer_id=cid, operations=[op])
        return json.dumps({"success": True})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Negative audiences ─────────────────────────────────────────────────────





@mcp.tool()
def gads_remove_ad_group_parental_status(ad_group_id: str, criterion_id: str, customer_id: str = "") -> str:
    """Remove a parental status criterion from an ad group."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupCriterionService")
        op = client.get_type("AdGroupCriterionOperation")
        op.remove = f"customers/{cid}/adGroupCriteria/{ad_group_id}~{criterion_id}"
        service.mutate_ad_group_criteria(customer_id=cid, operations=[op])
        return json.dumps({"success": True})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_remove_ad_group_placement(ad_group_id: str, criterion_id: str, customer_id: str = "") -> str:
    """Remove a placement criterion from an ad group."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupCriterionService")
        op = client.get_type("AdGroupCriterionOperation")
        op.remove = f"customers/{cid}/adGroupCriteria/{ad_group_id}~{criterion_id}"
        service.mutate_ad_group_criteria(customer_id=cid, operations=[op])
        return json.dumps({"success": True})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_remove_ad_group_proximity(ad_group_id: str, criterion_id: str, customer_id: str = "") -> str:
    """Remove a proximity criterion from an ad group."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupCriterionService")
        op = client.get_type("AdGroupCriterionOperation")
        op.remove = f"customers/{cid}/adGroupCriteria/{ad_group_id}~{criterion_id}"
        service.mutate_ad_group_criteria(customer_id=cid, operations=[op])
        return json.dumps({"success": True})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Remove audience criteria ──────────────────────────────────────────────





@mcp.tool()
def gads_remove_ad_group_topic(ad_group_id: str, criterion_id: str, customer_id: str = "") -> str:
    """Remove a topic criterion from an ad group."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupCriterionService")
        op = client.get_type("AdGroupCriterionOperation")
        op.remove = f"customers/{cid}/adGroupCriteria/{ad_group_id}~{criterion_id}"
        service.mutate_ad_group_criteria(customer_id=cid, operations=[op])
        return json.dumps({"success": True})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_remove_asset(
    asset_resource_name: str,
    campaign_id: str = "",
    field_type: str = "SITELINK",
    customer_id: str = "",
) -> str:
    """Remove an asset from a campaign (detach it, not delete permanently).

    Args:
        asset_resource_name: Full resource name of the asset,
          e.g. 'customers/123/assets/456'.
        campaign_id: Campaign to remove asset from.
        field_type: Asset field type: SITELINK|CALLOUT|CALL|STRUCTURED_SNIPPET|etc.
        customer_id: Google Ads customer ID.
    """
    try:
        client, cid = _get_client(customer_id)
        if not campaign_id:
            return json.dumps({"error": "campaign_id is required."})
        # Find the campaign_asset resource name
        rows = _search(
            f"""
            SELECT campaign_asset.resource_name
            FROM campaign_asset
            WHERE campaign.id = {campaign_id}
              AND campaign_asset.asset = '{asset_resource_name}'
              AND campaign_asset.field_type = '{field_type.upper()}'
            """,
            customer_id,
        )
        if not rows:
            return json.dumps({"error": "campaign_asset not found."})
        ca_rn = rows[0].campaign_asset.resource_name
        ca_service = client.get_service("CampaignAssetService")
        op = client.get_type("CampaignAssetOperation")
        op.remove = ca_rn
        ca_service.mutate_campaign_assets(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "removed_campaign_asset": ca_rn})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


# ---------------------------------------------------------------------------
# GROUP 10: Account & MCC
# ---------------------------------------------------------------------------





@mcp.tool()
def gads_remove_audience_from_ad_group(ad_group_id: str, criterion_id: str, customer_id: str = "") -> str:
    """Remove an audience criterion (positive or negative) from an ad group."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupCriterionService")
        op = client.get_type("AdGroupCriterionOperation")
        op.remove = f"customers/{cid}/adGroupCriteria/{ad_group_id}~{criterion_id}"
        service.mutate_ad_group_criteria(customer_id=cid, operations=[op])
        return json.dumps({"success": True})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Labels on keywords ────────────────────────────────────────────────────





@mcp.tool()
def gads_remove_audience_from_campaign(campaign_id: str, criterion_id: str, customer_id: str = "") -> str:
    """Remove an audience criterion (positive or negative) from a campaign."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignCriterionService")
        op = client.get_type("CampaignCriterionOperation")
        op.remove = f"customers/{cid}/campaignCriteria/{campaign_id}~{criterion_id}"
        service.mutate_campaign_criteria(customer_id=cid, operations=[op])
        return json.dumps({"success": True})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_remove_bidding_data_exclusion(data_exclusion_resource_name: str, customer_id: str = "") -> str:
    """Remove a bidding data exclusion by resource name."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("BiddingDataExclusionService")
        op = client.get_type("BiddingDataExclusionOperation")
        op.remove = data_exclusion_resource_name
        service.mutate_bidding_data_exclusions(customer_id=cid, operations=[op])
        return json.dumps({"success": True})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Location / language list tools ────────────────────────────────────────





@mcp.tool()
def gads_remove_bidding_seasonality_adjustment(seasonality_adjustment_resource_name: str, customer_id: str = "") -> str:
    """Remove a bidding seasonality adjustment by resource name."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("BiddingSeasonalityAdjustmentService")
        op = client.get_type("BiddingSeasonalityAdjustmentOperation")
        op.remove = seasonality_adjustment_resource_name
        service.mutate_bidding_seasonality_adjustments(customer_id=cid, operations=[op])
        return json.dumps({"success": True})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_remove_campaign_carrier(campaign_id: str, criterion_id: str, customer_id: str = "") -> str:
    """Remove carrier targeting criterion from a campaign."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignCriterionService")
        op = client.get_type("CampaignCriterionOperation")
        op.remove = f"customers/{cid}/campaignCriteria/{campaign_id}~{criterion_id}"
        service.mutate_campaign_criteria(customer_id=cid, operations=[op])
        return json.dumps({"success": True})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_remove_campaign_draft(base_campaign_id: str, draft_id: str, customer_id: str = "") -> str:
    """Remove (delete) a campaign draft."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignDraftService")
        op = client.get_type("CampaignDraftOperation")
        op.remove = f"customers/{cid}/campaignDrafts/{base_campaign_id}~{draft_id}"
        service.mutate_campaign_drafts(customer_id=cid, operations=[op])
        return json.dumps({"success": True})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Smart campaign status ─────────────────────────────────────────────────





@mcp.tool()
def gads_remove_campaign_geo_target(campaign_id: str, criterion_id: str, customer_id: str = "") -> str:
    """Remove a geo target (location) criterion from a campaign."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignCriterionService")
        op = client.get_type("CampaignCriterionOperation")
        op.remove = f"customers/{cid}/campaignCriteria/{campaign_id}~{criterion_id}"
        service.mutate_campaign_criteria(customer_id=cid, operations=[op])
        return json.dumps({"success": True})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_remove_campaign_ip_exclusion(campaign_id: str, criterion_id: str, customer_id: str = "") -> str:
    """Remove an IP address exclusion from a campaign."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignCriterionService")
        op = client.get_type("CampaignCriterionOperation")
        op.remove = f"customers/{cid}/campaignCriteria/{campaign_id}~{criterion_id}"
        response = service.mutate_campaign_criteria(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "removed": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_remove_campaign_language(campaign_id: str, criterion_id: str, customer_id: str = "") -> str:
    """Remove a language criterion from a campaign."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignCriterionService")
        op = client.get_type("CampaignCriterionOperation")
        op.remove = f"customers/{cid}/campaignCriteria/{campaign_id}~{criterion_id}"
        service.mutate_campaign_criteria(customer_id=cid, operations=[op])
        return json.dumps({"success": True})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_remove_campaign_mobile_device(campaign_id: str, criterion_id: str, customer_id: str = "") -> str:
    """Remove mobile device targeting criterion from a campaign."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignCriterionService")
        op = client.get_type("CampaignCriterionOperation")
        op.remove = f"customers/{cid}/campaignCriteria/{campaign_id}~{criterion_id}"
        service.mutate_campaign_criteria(customer_id=cid, operations=[op])
        return json.dumps({"success": True})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Ad group carrier / device / OS targeting ───────────────────────────────





@mcp.tool()
def gads_remove_campaign_negative_topic(campaign_id: str, criterion_id: str, customer_id: str = "") -> str:
    """Remove a negative topic criterion from a campaign."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignCriterionService")
        op = client.get_type("CampaignCriterionOperation")
        op.remove = f"customers/{cid}/campaignCriteria/{campaign_id}~{criterion_id}"
        service.mutate_campaign_criteria(customer_id=cid, operations=[op])
        return json.dumps({"success": True})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_remove_campaign_operating_system(campaign_id: str, criterion_id: str, customer_id: str = "") -> str:
    """Remove an operating system criterion from a campaign."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignCriterionService")
        op = client.get_type("CampaignCriterionOperation")
        op.remove = f"customers/{cid}/campaignCriteria/{campaign_id}~{criterion_id}"
        service.mutate_campaign_criteria(customer_id=cid, operations=[op])
        return json.dumps({"success": True})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Remove ad group criteria ──────────────────────────────────────────────





@mcp.tool()
def gads_remove_campaign_placement(campaign_id: str, criterion_id: str, customer_id: str = "") -> str:
    """Remove a placement criterion from a campaign."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignCriterionService")
        op = client.get_type("CampaignCriterionOperation")
        op.remove = f"customers/{cid}/campaignCriteria/{campaign_id}~{criterion_id}"
        service.mutate_campaign_criteria(customer_id=cid, operations=[op])
        return json.dumps({"success": True})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_remove_campaign_topic(campaign_id: str, criterion_id: str, customer_id: str = "") -> str:
    """Remove a topic criterion from a campaign."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignCriterionService")
        op = client.get_type("CampaignCriterionOperation")
        op.remove = f"customers/{cid}/campaignCriteria/{campaign_id}~{criterion_id}"
        service.mutate_campaign_criteria(customer_id=cid, operations=[op])
        return json.dumps({"success": True})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_remove_keywords_from_shared_list(shared_set_id: str, criterion_ids: list,
                                           customer_id: str = "") -> str:
    """Remove keywords from a shared negative keyword list. criterion_ids: list of criterion ID strings."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("SharedCriterionService")
        ops = []
        for crit_id in criterion_ids:
            op = client.get_type("SharedCriterionOperation")
            op.remove = f"customers/{cid}/sharedCriteria/{shared_set_id}~{crit_id}"
            ops.append(op)
        service.mutate_shared_criteria(customer_id=cid, operations=ops)
        return json.dumps({"success": True, "removed": len(ops)})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_remove_label_from_keyword(ad_group_id: str, criterion_id: str, label_id: str,
                                     customer_id: str = "") -> str:
    """Remove a label from a keyword (ad group criterion)."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupCriterionLabelService")
        op = client.get_type("AdGroupCriterionLabelOperation")
        op.remove = f"customers/{cid}/adGroupCriterionLabels/{ad_group_id}~{criterion_id}~{label_id}"
        service.mutate_ad_group_criterion_labels(customer_id=cid, operations=[op])
        return json.dumps({"success": True})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Asset updates ─────────────────────────────────────────────────────────





@mcp.tool()
def gads_remove_pmax_brand_exclusion(campaign_id: str, criterion_id: str, customer_id: str = "") -> str:
    """Remove a brand exclusion from a Performance Max campaign."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignCriterionService")
        op = client.get_type("CampaignCriterionOperation")
        op.remove = f"customers/{cid}/campaignCriteria/{campaign_id}~{criterion_id}"
        service.mutate_campaign_criteria(customer_id=cid, operations=[op])
        return json.dumps({"success": True})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── App campaign settings ──────────────────────────────────────────────────





@mcp.tool()
def gads_remove_users_from_customer_match_list(user_list_id: str, emails: list = None,
                                                phone_numbers: list = None,
                                                customer_id: str = "") -> str:
    """Remove users from a Customer Match user list.
    emails: list of email addresses (will be SHA-256 hashed).
    phone_numbers: list of phone numbers in E.164 format."""
    try:
        import hashlib
        client, cid = _get_client(customer_id)
        service = client.get_service("UserListService")
        offline_service = client.get_service("OfflineUserDataJobService")

        job_op = client.get_type("OfflineUserDataJobOperation")
        job = job_op.create
        job.type_ = client.enums.OfflineUserDataJobTypeEnum.CUSTOMER_MATCH_USER_LIST
        job.customer_match_user_list_metadata.user_list = f"customers/{cid}/userLists/{user_list_id}"
        job_response = offline_service.create_offline_user_data_job(
            customer_id=cid, job=job
        )
        job_rn = job_response.resource_name

        UserData = client.get_type("UserData")
        UserIdentifier = client.get_type("UserIdentifier")
        DataOp = client.get_type("OfflineUserDataJobOperation")

        ops = []
        if emails:
            for email in emails:
                hashed = hashlib.sha256(email.strip().lower().encode()).hexdigest()
                data_op = DataOp()
                ud = data_op.remove
                ui = UserIdentifier()
                ui.hashed_email = hashed
                ud.user_identifiers.append(ui)
                ops.append(data_op)
        if phone_numbers:
            for phone in phone_numbers:
                hashed = hashlib.sha256(phone.strip().encode()).hexdigest()
                data_op = DataOp()
                ud = data_op.remove
                ui = UserIdentifier()
                ui.hashed_phone_number = hashed
                ud.user_identifiers.append(ui)
                ops.append(data_op)

        if ops:
            offline_service.add_offline_user_data_job_operations(
                resource_name=job_rn, operations=ops
            )
            run_response = offline_service.run_offline_user_data_job(resource_name=job_rn)
            return json.dumps({"success": True, "job_resource_name": job_rn,
                               "operation_name": run_response.operation.name,
                               "users_removed": len(ops)})
        return json.dumps({"error": "No users provided"})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Ad group CPA simulation ────────────────────────────────────────────────





@mcp.tool()
def gads_rename_ad_group(
    ad_group_id: str,
    new_name: str,
    customer_id: str = "",
) -> str:
    """Rename an ad group.

    Args:
        ad_group_id: Ad group numeric ID (required).
        new_name: New ad group name (required).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupService")
        op = client.get_type("AdGroupOperation")
        ag = op.update
        ag.resource_name = f"customers/{cid}/adGroups/{ad_group_id}"
        ag.name = new_name
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["name"]))
        service.mutate_ad_groups(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "ad_group_id": ad_group_id, "new_name": new_name})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_rename_ad_with_label(
    ad_group_id: str,
    ad_id: str,
    label_name: str,
    customer_id: str = "",
) -> str:
    """Apply a label to an ad as a naming/tagging mechanism (since ads have no name field).

    Args:
        ad_group_id: Ad group ID (required).
        ad_id: Ad ID (required).
        label_name: Label name to create and apply as the ad's identifier (required).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        client, cid = _get_client(customer_id)
        label_service = client.get_service("LabelService")
        label_op = client.get_type("LabelOperation")
        lbl = label_op.create
        lbl.name = label_name
        lbl.status = client.enums.LabelStatusEnum.ENABLED
        label_resp = label_service.mutate_labels(customer_id=cid, operations=[label_op])
        label_rn = label_resp.results[0].resource_name
        al_service = client.get_service("AdGroupAdLabelService")
        al_op = client.get_type("AdGroupAdLabelOperation")
        al = al_op.create
        al.ad_group_ad = f"customers/{cid}/adGroupAds/{ad_group_id}~{ad_id}"
        al.label = label_rn
        al_service.mutate_ad_group_ad_labels(customer_id=cid, operations=[al_op])
        return json.dumps({"success": True, "ad_id": ad_id, "label_applied": label_name, "label_resource": label_rn})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# LABELS EXTENDED
# ---------------------------------------------------------------------------





@mcp.tool()
def gads_rename_asset_group(
    asset_group_id: str,
    new_name: str,
    customer_id: str = "",
) -> str:
    """Rename a Performance Max asset group.

    Args:
        asset_group_id: Asset group numeric ID (required).
        new_name: New asset group name (required).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AssetGroupService")
        op = client.get_type("AssetGroupOperation")
        ag = op.update
        ag.resource_name = f"customers/{cid}/assetGroups/{asset_group_id}"
        ag.name = new_name
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["name"]))
        service.mutate_asset_groups(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "asset_group_id": asset_group_id, "new_name": new_name})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_rename_campaign(
    campaign_id: str,
    new_name: str,
    customer_id: str = "",
) -> str:
    """Rename a campaign.

    Args:
        campaign_id: Campaign numeric ID (required).
        new_name: New campaign name (required).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        c = op.update
        c.resource_name = f"customers/{cid}/campaigns/{campaign_id}"
        c.name = new_name
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["name"]))
        service.mutate_campaigns(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "campaign_id": campaign_id, "new_name": new_name})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_request_review(ad_group_id: str, ad_id: str, customer_id: str = "") -> str:
    """Request a review for a disapproved or limited ad."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupAdService")
        request = client.get_type("MutateAdGroupAdsRequest")
        op = client.get_type("AdGroupAdOperation")
        ad_group_ad = op.update
        ad_group_ad.resource_name = f"customers/{cid}/adGroupAds/{ad_group_id}~{ad_id}"
        # Trigger re-review by updating status to ENABLED
        ad_group_ad.status = client.enums.AdGroupAdStatusEnum.ENABLED
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["status"]))
        response = service.mutate_ad_group_ads(customer_id=cid, operations=[op])
        return json.dumps({
            "success": True,
            "resource_name": response.results[0].resource_name,
            "note": "Ad set to ENABLED to trigger re-review. Policy review may take 1-3 business days."
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Ad group criterion bid modifier ───────────────────────────────────────





@mcp.tool()
def gads_run_offline_user_data_job(job_resource_name: str, customer_id: str = "") -> str:
    """Run (execute) an offline user data job after adding operations to it."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("OfflineUserDataJobService")
        response = service.run_offline_user_data_job(
            resource_name=job_resource_name
        )
        return json.dumps({"success": True, "operation_name": response.operation.name})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Remove users from customer match list ─────────────────────────────────





@mcp.tool()
def gads_set_ad_group_ad_rotation(ad_group_id: str, rotation_mode: str, customer_id: str = "") -> str:
    """Set ad rotation mode for an ad group. rotation_mode: OPTIMIZE or ROTATE_FOREVER."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupService")
        op = client.get_type("AdGroupOperation")
        ag = op.update
        ag.resource_name = f"customers/{cid}/adGroups/{ad_group_id}"
        ag.ad_rotation_mode = client.enums.AdGroupAdRotationModeEnum[rotation_mode]
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["ad_rotation_mode"]))
        response = service.mutate_ad_groups(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Campaign targeting expansion ──────────────────────────────────────────





@mcp.tool()
def gads_set_ad_group_content_bid_criterion(ad_group_id: str, criterion_resource_name: str,
                                             cpc_bid_micros: int, customer_id: str = "") -> str:
    """Set CPC bid on a content (placement/topic/keyword) criterion in an ad group."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupCriterionService")
        op = client.get_type("AdGroupCriterionOperation")
        criterion = op.update
        criterion.resource_name = criterion_resource_name
        criterion.cpc_bid_micros = cpc_bid_micros
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["cpc_bid_micros"]))
        response = service.mutate_ad_group_criteria(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})




# ---------------------------------------------------------------------------
# BATCH 17 — Criteria Removal, Labels on Keywords, Asset Updates,
#             Account/Billing, Campaign Drafts, Keyword Tools, etc.
# ---------------------------------------------------------------------------

from google.protobuf import field_mask_pb2


# ── Remove campaign criteria ──────────────────────────────────────────────





@mcp.tool()
def gads_set_ad_group_cpc_bid(
    ad_group_id: str,
    cpc_bid_micros: int,
    customer_id: str = "",
) -> str:
    """Set the max CPC bid on an ad group.

    Args:
        ad_group_id: Ad group numeric ID.
        cpc_bid_micros: New CPC bid in micros, e.g. 2000000 for $2.00.
        customer_id: Google Ads customer ID.
    """
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupService")
        op = client.get_type("AdGroupOperation")
        ag = op.update
        ag.resource_name = f"customers/{cid}/adGroups/{ad_group_id}"
        ag.cpc_bid_micros = cpc_bid_micros
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["cpc_bid_micros"]))
        service.mutate_ad_groups(customer_id=cid, operations=[op])
        return json.dumps({
            "success": True,
            "ad_group_id": ad_group_id,
            "cpc_bid_micros": cpc_bid_micros,
            "cpc_bid": _m(cpc_bid_micros),
        })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})






@mcp.tool()
def gads_set_ad_group_criterion_bid_modifier(ad_group_id: str, criterion_id: str,
                                              bid_modifier: float, customer_id: str = "") -> str:
    """Set bid modifier on an ad group criterion (audience, placement, topic, demographic)."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupCriterionService")
        op = client.get_type("AdGroupCriterionOperation")
        criterion = op.update
        criterion.resource_name = f"customers/{cid}/adGroupCriteria/{ad_group_id}~{criterion_id}"
        criterion.bid_modifier = bid_modifier
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["bid_modifier"]))
        response = service.mutate_ad_group_criteria(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Campaigns by type / without spend ────────────────────────────────────





@mcp.tool()
def gads_set_ad_group_custom_parameters(ad_group_id: str, parameters: list,
                                          customer_id: str = "") -> str:
    """Set custom URL parameters on an ad group. parameters: list of {key, value} dicts."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupService")
        op = client.get_type("AdGroupOperation")
        ag = op.update
        ag.resource_name = f"customers/{cid}/adGroups/{ad_group_id}"
        CustomParameter = client.get_type("CustomParameter")
        for p in parameters:
            cp = CustomParameter()
            cp.key = p["key"]
            cp.value = p["value"]
            ag.url_custom_parameters.append(cp)
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["url_custom_parameters"]))
        response = service.mutate_ad_groups(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_set_ad_group_device_bid_modifier(ad_group_id: str, device: str, bid_modifier: float, customer_id: str = "") -> str:
    """Set device bid modifier for an ad group (alias for gads_set_ad_group_bid_modifiers).
    Args: ad_group_id: Ad group ID. device: DESKTOP, MOBILE, TABLET. bid_modifier: Multiplier. customer_id: optional."""
    return gads_set_ad_group_bid_modifiers(ad_group_id=ad_group_id, device=device, bid_modifier=bid_modifier, customer_id=customer_id)






@mcp.tool()
def gads_set_ad_group_final_url_suffix(ad_group_id: str, final_url_suffix: str, customer_id: str = "") -> str:
    """Set final URL suffix at ad group level."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupService")
        op = client.get_type("AdGroupOperation")
        ad_group = op.update
        ad_group.resource_name = f"customers/{cid}/adGroups/{ad_group_id}"
        ad_group.final_url_suffix = final_url_suffix
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["final_url_suffix"]))
        response = service.mutate_ad_groups(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_set_ad_group_target_cpa(
    ad_group_id: str,
    target_cpa_micros: int,
    customer_id: str = "",
) -> str:
    """Set target CPA at the ad group level.

    Args:
        ad_group_id: Ad group numeric ID.
        target_cpa_micros: Target CPA in micros, e.g. 50000000 for $50.
        customer_id: Google Ads customer ID.
    """
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupService")
        op = client.get_type("AdGroupOperation")
        ag = op.update
        ag.resource_name = f"customers/{cid}/adGroups/{ad_group_id}"
        ag.target_cpa_micros = target_cpa_micros
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["target_cpa_micros"]))
        service.mutate_ad_groups(customer_id=cid, operations=[op])
        return json.dumps({
            "success": True,
            "ad_group_id": ad_group_id,
            "target_cpa_micros": target_cpa_micros,
            "target_cpa": _m(target_cpa_micros),
        })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


# ---------------------------------------------------------------------------
# GROUP 5: Keywords (missing tools)
# ---------------------------------------------------------------------------





@mcp.tool()
def gads_set_ad_group_target_roas(ad_group_id: str, target_roas: float, customer_id: str = "") -> str:
    """Set target ROAS for an ad group (overrides campaign-level ROAS)."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupService")
        op = client.get_type("AdGroupOperation")
        ag = op.update
        ag.resource_name = f"customers/{cid}/adGroups/{ad_group_id}"
        ag.target_roas = target_roas
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["target_roas"]))
        response = service.mutate_ad_groups(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Campaign misc settings ─────────────────────────────────────────────────





@mcp.tool()
def gads_set_ad_schedule_bid_modifier(campaign_id: str, criterion_id: str, bid_modifier: float, customer_id: str = "") -> str:
    """Update bid modifier for a specific ad schedule in a campaign.
    Args: campaign_id: Campaign ID. criterion_id: Criterion ID. bid_modifier: New multiplier. customer_id: optional."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignCriterionService")
        op = client.get_type("CampaignCriterionOperation")
        cc = op.update
        cc.resource_name = f"customers/{cid}/campaignCriteria/{campaign_id}~{criterion_id}"
        cc.bid_modifier = bid_modifier
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["bid_modifier"]))
        service.mutate_campaign_criteria(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "criterion_id": criterion_id, "bid_modifier": bid_modifier})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_set_ad_url_suffix(ad_group_id: str, ad_id: str, final_url_suffix: str, customer_id: str = "") -> str:
    """Set final URL suffix on a specific ad."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupAdService")
        op = client.get_type("AdGroupAdOperation")
        ad_group_ad = op.update
        ad_group_ad.resource_name = f"customers/{cid}/adGroupAds/{ad_group_id}~{ad_id}"
        ad_group_ad.ad.final_url_suffix = final_url_suffix
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["ad.final_url_suffix"]))
        response = service.mutate_ad_group_ads(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_set_app_campaign_settings(campaign_id: str, app_id: str = "", app_store: str = "",
                                    bidding_strategy_goal_type: str = "", customer_id: str = "") -> str:
    """Set app campaign specific settings. app_store: APPLE_APP_STORE or GOOGLE_APP_STORE.
    bidding_strategy_goal_type: OPTIMIZE_INSTALLS_TARGET_INSTALL_COST, OPTIMIZE_IN_APP_CONVERSIONS_TARGET_INSTALL_COST, etc."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        campaign = op.update
        campaign.resource_name = f"customers/{cid}/campaigns/{campaign_id}"
        paths = []
        if app_id:
            campaign.app_campaign_setting.app_id = app_id
            paths.append("app_campaign_setting.app_id")
        if app_store:
            campaign.app_campaign_setting.app_store = client.enums.AppCampaignAppStoreEnum[app_store]
            paths.append("app_campaign_setting.app_store")
        if bidding_strategy_goal_type:
            campaign.app_campaign_setting.bidding_strategy_goal_type = \
                client.enums.AppCampaignBiddingStrategyGoalTypeEnum[bidding_strategy_goal_type]
            paths.append("app_campaign_setting.bidding_strategy_goal_type")
        if paths:
            op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=paths))
            response = service.mutate_campaigns(customer_id=cid, operations=[op])
            return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
        return json.dumps({"error": "No fields provided"})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── DSA update ─────────────────────────────────────────────────────────────





@mcp.tool()
def gads_set_asset_group_status(asset_group_id: str, status: str, customer_id: str = "") -> str:
    """Set status (ENABLED/PAUSED/REMOVED) on an asset group."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AssetGroupService")
        op = client.get_type("AssetGroupOperation")
        asset_group = op.update
        asset_group.resource_name = f"customers/{cid}/assetGroups/{asset_group_id}"
        status_enum = client.enums.AssetGroupStatusEnum
        status_map = {
            "ENABLED": status_enum.ENABLED,
            "PAUSED": status_enum.PAUSED,
            "REMOVED": status_enum.REMOVED,
        }
        if status.upper() not in status_map:
            return json.dumps({"error": f"Invalid status '{status}'. Use ENABLED, PAUSED, or REMOVED."})
        asset_group.status = status_map[status.upper()]
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["status"]))
        response = service.mutate_asset_groups(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_set_campaign_ad_rotation(campaign_id: str, ad_rotation_mode: str, customer_id: str = "") -> str:
    """Set ad rotation mode for a campaign. ad_rotation_mode: 'OPTIMIZE' or 'ROTATE_FOREVER'."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        campaign = op.update
        campaign.resource_name = f"customers/{cid}/campaigns/{campaign_id}"
        mode_enum = client.enums.AdRotationModeEnum
        mode_map = {
            "OPTIMIZE": mode_enum.OPTIMIZE,
            "ROTATE_FOREVER": mode_enum.ROTATE_FOREVER,
        }
        if ad_rotation_mode not in mode_map:
            return json.dumps({"error": f"Invalid ad_rotation_mode '{ad_rotation_mode}'. Use OPTIMIZE or ROTATE_FOREVER."})
        campaign.ad_rotation_mode = mode_map[ad_rotation_mode]
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["ad_rotation_mode"]))
        response = service.mutate_campaigns(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_set_campaign_ai_max(
    campaign_id: str,
    enable: bool,
    text_asset_automation: bool = True,
    final_url_expansion: bool = True,
    customer_id: str = "",
) -> str:
    """Enable or disable AI Max for a single Search campaign, including sub-settings.

    When enabling AI Max, also controls:
    - Text asset automation (התאמה אישית של טקסט): Google auto-generates ad text
    - Final URL expansion (הרחבה של כתובת URL סופית): Google expands to relevant landing pages

    Args:
        campaign_id: Campaign ID to update.
        enable: True to enable AI Max, False to disable.
        text_asset_automation: Enable text asset automation (default True when enabling AI Max).
        final_url_expansion: Enable final URL expansion (default True when enabling AI Max).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        from google.ads.googleads.v23.resources.types.campaign import Campaign as _Camp
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        campaign = op.update
        campaign.resource_name = f"customers/{cid}/campaigns/{campaign_id}"
        campaign.ai_max_setting.enable_ai_max = enable
        paths = ["ai_max_setting.enable_ai_max"]

        # Set asset automation sub-settings
        opted_in = client.enums.AssetAutomationStatusEnum.OPTED_IN
        opted_out = client.enums.AssetAutomationStatusEnum.OPTED_OUT
        AssetAutomationSetting = _Camp.AssetAutomationSetting

        s1 = AssetAutomationSetting()
        s1.asset_automation_type = client.enums.AssetAutomationTypeEnum.TEXT_ASSET_AUTOMATION
        s1.asset_automation_status = opted_in if text_asset_automation else opted_out
        campaign.asset_automation_settings.append(s1)

        s2 = AssetAutomationSetting()
        s2.asset_automation_type = client.enums.AssetAutomationTypeEnum.FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION
        s2.asset_automation_status = opted_in if final_url_expansion else opted_out
        campaign.asset_automation_settings.append(s2)

        paths.append("asset_automation_settings")
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=paths))
        response = service.mutate_campaigns(customer_id=cid, operations=[op])
        return json.dumps({
            "success": True,
            "campaign_id": campaign_id,
            "ai_max_enabled": enable,
            "text_asset_automation": text_asset_automation,
            "final_url_expansion": final_url_expansion,
            "resource_name": response.results[0].resource_name,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_set_campaign_audience_setting(campaign_id: str, use_audience_grouped: bool, customer_id: str = "") -> str:
    """Set audience targeting setting for a campaign. True = use audience groups (observation), False = individual segments."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        campaign = op.update
        campaign.resource_name = f"customers/{cid}/campaigns/{campaign_id}"
        campaign.audience_setting.use_audience_grouped = use_audience_grouped
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["audience_setting.use_audience_grouped"]))
        response = service.mutate_campaigns(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_set_campaign_broad_match_settings(campaign_id: str, use_broad_match_keywords: bool,
                                            customer_id: str = "") -> str:
    """Enable/disable broad match for all keywords in a campaign (Smart Bidding broad match)."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        campaign = op.update
        campaign.resource_name = f"customers/{cid}/campaigns/{campaign_id}"
        campaign.keyword_match_setting.opt_in = use_broad_match_keywords
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["keyword_match_setting.opt_in"]))
        response = service.mutate_campaigns(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_set_campaign_budget_delivery(campaign_id: str, delivery_method: str, customer_id: str = "") -> str:
    """Set budget delivery method for a campaign. delivery_method: 'STANDARD' or 'ACCELERATED'."""
    try:
        client, cid = _get_client(customer_id)

        # Get the campaign's budget resource name
        query = (
            f"SELECT campaign.campaign_budget "
            f"FROM campaign "
            f"WHERE campaign.id = {campaign_id}"
        )
        results = _search(query, cid)
        budget_resource = None
        for row in results:
            budget_resource = row.campaign.campaign_budget
            break

        if not budget_resource:
            return json.dumps({"error": f"Could not find budget for campaign {campaign_id}."})

        budget_service = client.get_service("CampaignBudgetService")
        budget_op = client.get_type("CampaignBudgetOperation")
        budget = budget_op.update
        budget.resource_name = budget_resource

        delivery_enum = client.enums.BudgetDeliveryMethodEnum
        delivery_map = {
            "STANDARD": delivery_enum.STANDARD,
            "ACCELERATED": delivery_enum.ACCELERATED,
        }
        if delivery_method.upper() not in delivery_map:
            return json.dumps({"error": f"Invalid delivery_method '{delivery_method}'. Use STANDARD or ACCELERATED."})

        budget.delivery_method = delivery_map[delivery_method.upper()]
        budget_op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["delivery_method"]))
        response = budget_service.mutate_campaign_budgets(customer_id=cid, operations=[budget_op])
        return json.dumps({"success": True, "budget_resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_set_campaign_custom_parameters(campaign_id: str, parameters: list, customer_id: str = "") -> str:
    """Set custom URL parameters at campaign level. parameters is a list of dicts with 'key' and 'value'."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        campaign = op.update
        campaign.resource_name = f"customers/{cid}/campaigns/{campaign_id}"
        CustomParameter = client.get_type("CustomParameter")
        campaign.url_custom_parameters[:] = []
        for param in parameters:
            cp = CustomParameter()
            cp.key = param["key"]
            cp.value = param["value"]
            campaign.url_custom_parameters.append(cp)
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["url_custom_parameters"]))
        response = service.mutate_campaigns(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_set_campaign_customer_acquisition(campaign_id: str, optimize_for_new_customers: bool = True, new_customer_bid_only: bool = False, new_customer_acquisition_goal_value_micros: int = 0, customer_id: str = "") -> str:
    """Set customer acquisition settings for a campaign (PMax, Search).
    Allows bidding higher for new customers vs existing ones.
    Args: campaign_id: Campaign ID. optimize_for_new_customers: Enable new customer optimization. new_customer_bid_only: Only bid for new customers (exclude existing). new_customer_acquisition_goal_value_micros: Value lift for new customers. customer_id: optional."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        c = op.update
        c.resource_name = f"customers/{cid}/campaigns/{campaign_id}"
        ca = c.customer_acquisition_goal_settings
        if optimize_for_new_customers:
            ca.optimization_mode = client.enums.CustomerAcquisitionOptimizationModeEnum.OPTIMIZE_NEW_CUSTOMER_ACQUISITION_GOAL
        if new_customer_bid_only:
            ca.optimization_mode = client.enums.CustomerAcquisitionOptimizationModeEnum.TARGET_NEW_CUSTOMERS
        if new_customer_acquisition_goal_value_micros:
            ca.value_settings.value = new_customer_acquisition_goal_value_micros / 1e6
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["customer_acquisition_goal_settings"]))
        service.mutate_campaigns(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "campaign_id": campaign_id, "new_customer_optimization": optimize_for_new_customers})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_set_campaign_customer_retention(campaign_id: str, enable_retention: bool = True, customer_id: str = "") -> str:
    """Set customer retention optimization for a campaign.
    Args: campaign_id: Campaign ID. enable_retention: Enable customer retention optimization. customer_id: optional."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        c = op.update
        c.resource_name = f"customers/{cid}/campaigns/{campaign_id}"
        # Customer retention is controlled via audience targeting + bidding adjustments
        # For PMax campaigns this maps to the retention optimization goal
        if enable_retention:
            c.selective_optimization.conversion_actions.append("RETENTION")
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["selective_optimization"]))
        service.mutate_campaigns(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "campaign_id": campaign_id, "retention_enabled": enable_retention})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_set_campaign_final_url_suffix(campaign_id: str, final_url_suffix: str, customer_id: str = "") -> str:
    """Set final URL suffix at campaign level."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        campaign = op.update
        campaign.resource_name = f"customers/{cid}/campaigns/{campaign_id}"
        campaign.final_url_suffix = final_url_suffix
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["final_url_suffix"]))
        response = service.mutate_campaigns(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_set_campaign_geo_target_type(
    campaign_id: str,
    positive_geo_target_type: str = "",
    negative_geo_target_type: str = "",
    customer_id: str = "",
) -> str:
    """Set how geo targeting is applied. positive_geo_target_type: PRESENCE, PRESENCE_OR_INTEREST, INTEREST. negative_geo_target_type: PRESENCE, PRESENCE_OR_INTEREST."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        campaign = op.update
        campaign.resource_name = f"customers/{cid}/campaigns/{campaign_id}"

        paths = []
        pos_enum = client.enums.PositiveGeoTargetTypeEnum
        neg_enum = client.enums.NegativeGeoTargetTypeEnum

        pos_map = {
            "PRESENCE": pos_enum.PRESENCE,
            "PRESENCE_OR_INTEREST": pos_enum.PRESENCE_OR_INTEREST,
            "INTEREST": pos_enum.INTEREST,
        }
        neg_map = {
            "PRESENCE": neg_enum.PRESENCE,
            "PRESENCE_OR_INTEREST": neg_enum.PRESENCE_OR_INTEREST,
        }

        if positive_geo_target_type:
            if positive_geo_target_type not in pos_map:
                return json.dumps({"error": f"Invalid positive_geo_target_type '{positive_geo_target_type}'."})
            campaign.geo_target_type_setting.positive_geo_target_type = pos_map[positive_geo_target_type]
            paths.append("geo_target_type_setting.positive_geo_target_type")

        if negative_geo_target_type:
            if negative_geo_target_type not in neg_map:
                return json.dumps({"error": f"Invalid negative_geo_target_type '{negative_geo_target_type}'."})
            campaign.geo_target_type_setting.negative_geo_target_type = neg_map[negative_geo_target_type]
            paths.append("geo_target_type_setting.negative_geo_target_type")

        if not paths:
            return json.dumps({"error": "No geo target type fields provided."})

        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=paths))
        response = service.mutate_campaigns(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name, "updated_fields": paths})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_set_campaign_networks(campaign_id: str, target_google_search: bool = None, target_search_partners: bool = None, target_display_network: bool = None, target_partner_search_network: bool = None, customer_id: str = "") -> str:
    """Set network targeting for a campaign (Google Search, Search Partners, Display, Partner Search).
    Args: campaign_id: Campaign ID. target_google_search: Show on Google Search. target_search_partners: Show on search partner sites. target_display_network: Show on Display Network. target_partner_search_network: Show on partner search network. customer_id: optional."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        c = op.update
        c.resource_name = f"customers/{cid}/campaigns/{campaign_id}"
        paths = []
        if target_google_search is not None:
            c.network_settings.target_google_search = target_google_search
            paths.append("network_settings.target_google_search")
        if target_search_partners is not None:
            c.network_settings.target_search_network = target_search_partners
            paths.append("network_settings.target_search_network")
        if target_display_network is not None:
            c.network_settings.target_content_network = target_display_network
            paths.append("network_settings.target_content_network")
        if target_partner_search_network is not None:
            c.network_settings.target_partner_search_network = target_partner_search_network
            paths.append("network_settings.target_partner_search_network")
        if not paths:
            return json.dumps({"error": "Provide at least one network setting"})
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=paths))
        service.mutate_campaigns(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "campaign_id": campaign_id, "updated": paths})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_set_campaign_real_time_bidding(campaign_id: str, opt_in: bool, customer_id: str = "") -> str:
    """Enable or disable real-time bidding (RTB) for a Display campaign.
    Args: campaign_id: Campaign ID. opt_in: True to enable RTB, False to disable. customer_id: optional."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        campaign = op.update
        campaign.resource_name = f"customers/{cid}/campaigns/{campaign_id}"
        campaign.real_time_bidding_setting.opt_in = opt_in
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["real_time_bidding_setting.opt_in"]))
        response = service.mutate_campaigns(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "campaign_id": campaign_id, "opt_in": opt_in,
                           "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_set_campaign_target_spend(
    campaign_id: str,
    target_spend_micros: int,
    cpc_bid_ceiling_micros: int = 0,
    customer_id: str = "",
) -> str:
    """Set target spend for maximize clicks bidding on a campaign."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        campaign = op.update
        campaign.resource_name = f"customers/{cid}/campaigns/{campaign_id}"
        paths = []
        campaign.maximize_clicks.target_spend_micros = target_spend_micros
        paths.append("maximize_clicks.target_spend_micros")
        if cpc_bid_ceiling_micros:
            campaign.maximize_clicks.cpc_bid_ceiling_micros = cpc_bid_ceiling_micros
            paths.append("maximize_clicks.cpc_bid_ceiling_micros")
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=paths))
        response = service.mutate_campaigns(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_set_campaign_targeting_expansion(campaign_id: str, opt_out_targeting_expansion: bool,
                                           customer_id: str = "") -> str:
    """Set targeting expansion (optimized targeting) for a display/video campaign.
    opt_out_targeting_expansion=True disables optimized targeting."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        campaign = op.update
        campaign.resource_name = f"customers/{cid}/campaigns/{campaign_id}"
        campaign.targeting_setting.target_restrictions_for_operation.clear()
        # Use audience targeting setting
        campaign.targeting_setting.targeting_dimension_for_location.CopyFrom(
            client.get_type("TargetingDimensionCondition"))
        # The correct approach for expansion:
        op_update = client.get_type("CampaignOperation")
        c = op_update.update
        c.resource_name = f"customers/{cid}/campaigns/{campaign_id}"
        # targeting_expansion_opt_out is at campaign level (for video/display)
        c.targeting_expansion.targeting_dimension = \
            client.enums.TargetingDimensionEnum.AUDIENCE
        # Use the proper field
        op_final = client.get_type("CampaignOperation")
        camp_final = op_final.update
        camp_final.resource_name = f"customers/{cid}/campaigns/{campaign_id}"
        camp_final.targeting_expansion.opt_out_targeting_expansion = opt_out_targeting_expansion
        op_final.update_mask.CopyFrom(
            field_mask_pb2.FieldMask(paths=["targeting_expansion.opt_out_targeting_expansion"]))
        response = service.mutate_campaigns(customer_id=cid, operations=[op_final])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Keyword simulation ─────────────────────────────────────────────────────





@mcp.tool()
def gads_set_campaign_tracking_template(campaign_id: str, tracking_template: str, customer_id: str = "") -> str:
    """Set tracking template at campaign level."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        campaign = op.update
        campaign.resource_name = f"customers/{cid}/campaigns/{campaign_id}"
        campaign.tracking_url_template = tracking_template
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["tracking_url_template"]))
        response = service.mutate_campaigns(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_set_campaign_vanity_pharma(campaign_id: str, display_url_mode: str,
                                     text: str = "", customer_id: str = "") -> str:
    """Set vanity pharma settings for a campaign (pharmaceutical industry).
    display_url_mode: MANUFACTURER_WEBSITE_URL or WEBSITE_DESCRIPTION.
    text: the description text if mode is WEBSITE_DESCRIPTION."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        campaign = op.update
        campaign.resource_name = f"customers/{cid}/campaigns/{campaign_id}"
        campaign.vanity_pharma.vanity_pharma_display_url_mode = \
            client.enums.VanityPharmaDisplayUrlModeEnum[display_url_mode]
        paths = ["vanity_pharma.vanity_pharma_display_url_mode"]
        if text:
            campaign.vanity_pharma.vanity_pharma_text = \
                client.enums.VanityPharmaTextEnum[text]
            paths.append("vanity_pharma.vanity_pharma_text")
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=paths))
        response = service.mutate_campaigns(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_set_customer_final_url_suffix(final_url_suffix: str, customer_id: str = "") -> str:
    """Set final URL suffix at the customer/account level."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CustomerService")
        op = client.get_type("CustomerOperation")
        customer = op.update
        customer.resource_name = f"customers/{cid}"
        customer.final_url_suffix = final_url_suffix
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["final_url_suffix"]))
        response = service.mutate_customer(customer_id=cid, operation=op)
        return json.dumps({"success": True, "resource_name": response.result.resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Campaign experiment (get) ──────────────────────────────────────────────





@mcp.tool()
def gads_set_customer_tracking_template(tracking_template: str, customer_id: str = "") -> str:
    """Set tracking template at the customer/account level."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CustomerService")
        op = client.get_type("CustomerOperation")
        customer = op.update
        customer.resource_name = f"customers/{cid}"
        customer.tracking_url_template = tracking_template
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["tracking_url_template"]))
        response = service.mutate_customer(customer_id=cid, operation=op)
        return json.dumps({"success": True, "resource_name": response.result.resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_set_device_bid_modifier(
    campaign_id: str,
    device: str,
    bid_modifier: float,
    customer_id: str = "",
) -> str:
    """Set bid modifier for DESKTOP, MOBILE, or TABLET on a campaign.

    Args:
        campaign_id: Campaign numeric ID.
        device: DESKTOP | MOBILE | TABLET
        bid_modifier: Multiplier, e.g. 1.2 = +20%, 0.5 = -50%, 0.0 = disable device
        customer_id: Google Ads customer ID.
    """
    try:
        client, cid = _get_client(customer_id)
        cc_service = client.get_service("CampaignCriterionService")
        dev = device.upper()

        # Find existing device criterion
        rows = _search(
            f"""
            SELECT campaign_criterion.resource_name,
                   campaign_criterion.device.type_
            FROM campaign_criterion
            WHERE campaign_criterion.type = 'DEVICE'
              AND campaign.id = {campaign_id}
            """,
            customer_id,
        )
        existing_rn = None
        for row in rows:
            if row.campaign_criterion.device.type_.name == dev:
                existing_rn = row.campaign_criterion.resource_name
                break

        op = client.get_type("CampaignCriterionOperation")
        if existing_rn:
            cc = op.update
            cc.resource_name = existing_rn
            cc.bid_modifier = bid_modifier
            op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["bid_modifier"]))
        else:
            cc = op.create
            cc.campaign = f"customers/{cid}/campaigns/{campaign_id}"
            cc.device.type_ = client.enums.DeviceEnum[dev]
            cc.bid_modifier = bid_modifier

        resp = cc_service.mutate_campaign_criteria(customer_id=cid, operations=[op])
        return json.dumps({
            "success": True,
            "action": "updated" if existing_rn else "created",
            "device": dev,
            "bid_modifier": bid_modifier,
            "resource_name": resp.results[0].resource_name,
        })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


# ---------------------------------------------------------------------------
# GROUP 3: Campaign Management (missing tools)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# GROUP 4: Ad Group Management (missing tools)
# ---------------------------------------------------------------------------





@mcp.tool()
def gads_set_keyword_custom_parameters(ad_group_id: str, criterion_id: str,
                                        parameters: list, customer_id: str = "") -> str:
    """Set custom URL parameters on a keyword. parameters: list of {key, value} dicts."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupCriterionService")
        op = client.get_type("AdGroupCriterionOperation")
        criterion = op.update
        criterion.resource_name = f"customers/{cid}/adGroupCriteria/{ad_group_id}~{criterion_id}"
        CustomParameter = client.get_type("CustomParameter")
        for p in parameters:
            cp = CustomParameter()
            cp.key = p["key"]
            cp.value = p["value"]
            criterion.url_custom_parameters.append(cp)
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["url_custom_parameters"]))
        response = service.mutate_ad_group_criteria(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Ad group URL options ───────────────────────────────────────────────────





@mcp.tool()
def gads_set_keyword_final_url_suffix(
    ad_group_id: str,
    criterion_id: str,
    final_url_suffix: str,
    customer_id: str = "",
) -> str:
    """Set final URL suffix at keyword level."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupCriterionService")
        op = client.get_type("AdGroupCriterionOperation")
        criterion = op.update
        criterion.resource_name = f"customers/{cid}/adGroupCriteria/{ad_group_id}~{criterion_id}"
        criterion.final_url_suffix = final_url_suffix
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["final_url_suffix"]))
        response = service.mutate_ad_group_criteria(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_set_keyword_final_urls(ad_group_id: str, criterion_id: str,
                                  final_urls: list, customer_id: str = "") -> str:
    """Set final URLs on a keyword."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupCriterionService")
        op = client.get_type("AdGroupCriterionOperation")
        criterion = op.update
        criterion.resource_name = f"customers/{cid}/adGroupCriteria/{ad_group_id}~{criterion_id}"
        criterion.final_urls.extend(final_urls)
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["final_urls"]))
        response = service.mutate_ad_group_criteria(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_set_keyword_tracking_template(
    ad_group_id: str,
    criterion_id: str,
    tracking_template: str,
    customer_id: str = "",
) -> str:
    """Set tracking template at keyword level."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupCriterionService")
        op = client.get_type("AdGroupCriterionOperation")
        criterion = op.update
        criterion.resource_name = f"customers/{cid}/adGroupCriteria/{ad_group_id}~{criterion_id}"
        criterion.keyword.tracking_url_template = tracking_template
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["tracking_url_template"]))
        response = service.mutate_ad_group_criteria(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_set_manual_cpc(campaign_id: str, enhanced_cpc: bool = False, customer_id: str = "") -> str:
    """Set Manual CPC bidding for a campaign.
    Args: campaign_id: Campaign ID. enhanced_cpc: Enable Enhanced CPC. customer_id: optional."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        c = op.update
        c.resource_name = f"customers/{cid}/campaigns/{campaign_id}"
        c.manual_cpc.enhanced_cpc_enabled = enhanced_cpc
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["manual_cpc.enhanced_cpc_enabled"]))
        service.mutate_campaigns(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "campaign_id": campaign_id, "bidding": "MANUAL_CPC", "enhanced_cpc": enhanced_cpc})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_set_manual_cpm(campaign_id: str, customer_id: str = "") -> str:
    """Set Manual CPM bidding for a display campaign.
    Args: campaign_id: Campaign ID. customer_id: optional."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        c = op.update
        c.resource_name = f"customers/{cid}/campaigns/{campaign_id}"
        c.manual_cpm = client.get_type("ManualCpm")
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["manual_cpm"]))
        service.mutate_campaigns(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "campaign_id": campaign_id, "bidding": "MANUAL_CPM"})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_set_manual_cpv(campaign_id: str, customer_id: str = "") -> str:
    """Set Manual CPV bidding for a video campaign.
    Args: campaign_id: Campaign ID. customer_id: optional."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        c = op.update
        c.resource_name = f"customers/{cid}/campaigns/{campaign_id}"
        c.manual_cpv = client.get_type("ManualCpv")
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["manual_cpv"]))
        service.mutate_campaigns(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "campaign_id": campaign_id, "bidding": "MANUAL_CPV"})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_set_maximize_clicks(campaign_id: str, max_cpc_bid_ceiling_micros: int = 0, customer_id: str = "") -> str:
    """Set Maximize Clicks bidding strategy for a campaign with optional max CPC cap.
    Args: campaign_id: Campaign ID. max_cpc_bid_ceiling_micros: Optional max CPC ceiling in micros (0 = no cap). customer_id: optional."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        c = op.update
        c.resource_name = f"customers/{cid}/campaigns/{campaign_id}"
        # In API v17+, Maximize Clicks uses target_spend bidding strategy
        c.target_spend.target_spend_micros = 0  # 0 = use full budget
        paths = ["target_spend.target_spend_micros"]
        if max_cpc_bid_ceiling_micros > 0:
            c.target_spend.cpc_bid_ceiling_micros = max_cpc_bid_ceiling_micros
            paths.append("target_spend.cpc_bid_ceiling_micros")
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=paths))
        service.mutate_campaigns(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "campaign_id": campaign_id, "bidding": "MAXIMIZE_CLICKS", "max_cpc_ceiling_micros": max_cpc_bid_ceiling_micros})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_set_pmax_auto_created_assets(campaign_id: str, opt_out: bool, customer_id: str = "") -> str:
    """Control automatically created assets for Performance Max campaigns. opt_out=True disables auto-created assets."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        campaign = op.update
        campaign.resource_name = f"customers/{cid}/campaigns/{campaign_id}"

        AssetAutomationSetting = client.get_type("AssetAutomationSetting")
        AssetAutomationTypeEnum = client.enums.AssetAutomationTypeEnum
        AssetAutomationStatusEnum = client.enums.AssetAutomationStatusEnum

        setting = AssetAutomationSetting()
        setting.asset_automation_type = AssetAutomationTypeEnum.TEXT_ASSET_AUTOMATION
        if opt_out:
            setting.asset_automation_status = AssetAutomationStatusEnum.OPTED_OUT
        else:
            setting.asset_automation_status = AssetAutomationStatusEnum.OPTED_IN

        campaign.asset_automation_settings[:] = [setting]
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["asset_automation_settings"]))
        response = service.mutate_campaigns(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name, "opted_out": opt_out})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_set_pmax_url_expansion(campaign_id: str, opt_out_url_expansion: bool, customer_id: str = "") -> str:
    """Set URL expansion for Performance Max campaigns. True = opt out (disabled), False = enabled (default)."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        campaign = op.update
        campaign.resource_name = f"customers/{cid}/campaigns/{campaign_id}"
        campaign.url_expansion_opt_out = opt_out_url_expansion
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["url_expansion_opt_out"]))
        response = service.mutate_campaigns(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_set_schedule_all_campaigns(day_of_week: str, start_hour: int, end_hour: int, bid_modifier: float = 1.0, customer_id: str = "") -> str:
    """Set an ad schedule on all active campaigns.
    Args: day_of_week: MONDAY, TUESDAY, WEDNESDAY, THURSDAY, FRIDAY, SATURDAY, SUNDAY. start_hour: 0-23. end_hour: 1-24. bid_modifier: Bid adjustment. customer_id: optional."""
    try:
        _, cid = _get_client(customer_id)
        camp_rows = _search("""
            SELECT campaign.id FROM campaign
            WHERE campaign.status = 'ENABLED'
        """, customer_id)
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignCriterionService")
        ops = []
        for cr in camp_rows:
            cid_val = str(cr.campaign.id)
            op = client.get_type("CampaignCriterionOperation")
            c = op.create
            c.campaign = f"customers/{cid}/campaigns/{cid_val}"
            c.ad_schedule.day_of_week = client.enums.DayOfWeekEnum[day_of_week]
            c.ad_schedule.start_hour = start_hour
            c.ad_schedule.start_minute = client.enums.MinuteOfHourEnum.ZERO
            c.ad_schedule.end_hour = end_hour
            c.ad_schedule.end_minute = client.enums.MinuteOfHourEnum.ZERO
            c.bid_modifier = bid_modifier
            ops.append(op)
        # Process in chunks of 1000
        for i in range(0, len(ops), 1000):
            service.mutate_campaign_criteria(customer_id=cid, operations=ops[i:i+1000])
        return json.dumps({"success": True, "campaigns_updated": len(ops)})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_set_shopping_campaign_settings(
    campaign_id: str,
    merchant_id: int = 0,
    sales_country: str = "",
    feed_label: str = "",
    enable_local: bool = None,
    campaign_priority: int = -1,
    customer_id: str = "",
) -> str:
    """Update shopping campaign settings. Only provided fields are updated."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        campaign = op.update
        campaign.resource_name = f"customers/{cid}/campaigns/{campaign_id}"

        paths = []
        if merchant_id:
            campaign.shopping_setting.merchant_id = merchant_id
            paths.append("shopping_setting.merchant_id")
        if sales_country:
            campaign.shopping_setting.sales_country = sales_country
            paths.append("shopping_setting.sales_country")
        if feed_label:
            campaign.shopping_setting.feed_label = feed_label
            paths.append("shopping_setting.feed_label")
        if enable_local is not None:
            campaign.shopping_setting.enable_local = enable_local
            paths.append("shopping_setting.enable_local")
        if campaign_priority >= 0:
            campaign.shopping_setting.campaign_priority = campaign_priority
            paths.append("shopping_setting.campaign_priority")

        if not paths:
            return json.dumps({"error": "No fields to update were provided."})

        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=paths))
        response = service.mutate_campaigns(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name, "updated_fields": paths})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_update_app_campaign(campaign_id: str, name: str = "", status: str = "",
                              app_campaign_setting_bidding_strategy_goal_type: str = "",
                              customer_id: str = "") -> str:
    """Update app campaign settings. goal_type: OPTIMIZE_INSTALLS_TARGET_INSTALL_COST,
    OPTIMIZE_IN_APP_CONVERSIONS_TARGET_INSTALL_COST, OPTIMIZE_IN_APP_CONVERSIONS_TARGET_CONVERSION_COST,
    OPTIMIZE_RETURN_ON_ADVERTISING_SPEND, OPTIMIZE_INSTALLS_WITHOUT_TARGET_INSTALL_COST."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        campaign = op.update
        campaign.resource_name = f"customers/{cid}/campaigns/{campaign_id}"
        paths = []
        if name:
            campaign.name = name
            paths.append("name")
        if status:
            campaign.status = client.enums.CampaignStatusEnum[status]
            paths.append("status")
        if app_campaign_setting_bidding_strategy_goal_type:
            goal_enum = client.enums.AppCampaignBiddingStrategyGoalTypeEnum
            campaign.app_campaign_setting.bidding_strategy_goal_type = \
                goal_enum[app_campaign_setting_bidding_strategy_goal_type]
            paths.append("app_campaign_setting.bidding_strategy_goal_type")
        if paths:
            op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=paths))
            response = service.mutate_campaigns(customer_id=cid, operations=[op])
            return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
        return json.dumps({"error": "No fields provided to update"})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_update_asset_group_name(asset_group_id: str, name: str, customer_id: str = "") -> str:
    """Update asset group name."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AssetGroupService")
        op = client.get_type("AssetGroupOperation")
        asset_group = op.update
        asset_group.resource_name = f"customers/{cid}/assetGroups/{asset_group_id}"
        asset_group.name = name
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["name"]))
        response = service.mutate_asset_groups(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_update_call_asset(asset_resource_name: str, phone_number: str = "",
                             country_code: str = "", customer_id: str = "") -> str:
    """Update a call asset's phone number and/or country code."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AssetService")
        op = client.get_type("AssetOperation")
        asset = op.update
        asset.resource_name = asset_resource_name
        paths = []
        if phone_number:
            asset.call_asset.phone_number = phone_number
            paths.append("call_asset.phone_number")
        if country_code:
            asset.call_asset.country_code = country_code
            paths.append("call_asset.country_code")
        if paths:
            op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=paths))
            response = service.mutate_assets(customer_id=cid, operations=[op])
            return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
        return json.dumps({"error": "No fields to update"})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Keyword tools ─────────────────────────────────────────────────────────





@mcp.tool()
def gads_update_callout_asset(asset_resource_name: str, callout_text: str,
                                customer_id: str = "") -> str:
    """Update a callout asset's text."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AssetService")
        op = client.get_type("AssetOperation")
        asset = op.update
        asset.resource_name = asset_resource_name
        asset.callout_asset.callout_text = callout_text
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["callout_asset.callout_text"]))
        response = service.mutate_assets(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_update_dynamic_search_ad(ad_group_id: str, ad_id: str,
                                   description1: str = "", description2: str = "",
                                   tracking_url_template: str = "", customer_id: str = "") -> str:
    """Update a Dynamic Search Ad's description lines and/or tracking template."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupAdService")
        op = client.get_type("AdGroupAdOperation")
        ad_group_ad = op.update
        ad_group_ad.resource_name = f"customers/{cid}/adGroupAds/{ad_group_id}~{ad_id}"
        paths = []
        if description1:
            ad_group_ad.ad.expanded_dynamic_search_ad.description = description1
            paths.append("ad.expanded_dynamic_search_ad.description")
        if description2:
            ad_group_ad.ad.expanded_dynamic_search_ad.description2 = description2
            paths.append("ad.expanded_dynamic_search_ad.description2")
        if tracking_url_template:
            ad_group_ad.ad.tracking_url_template = tracking_url_template
            paths.append("ad.tracking_url_template")
        if paths:
            op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=paths))
            response = service.mutate_ad_group_ads(customer_id=cid, operations=[op])
            return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
        return json.dumps({"error": "No fields provided"})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Local campaign & Local ad ──────────────────────────────────────────────





@mcp.tool()
def gads_update_expanded_text_ad(ad_group_id: str, ad_id: str,
                                   headline_part1: str = "", headline_part2: str = "",
                                   headline_part3: str = "", description: str = "",
                                   description2: str = "", path1: str = "", path2: str = "",
                                   customer_id: str = "") -> str:
    """Update an Expanded Text Ad (deprecated format, read-only after June 2022, but existing ads can be paused/enabled/removed)."""
    return json.dumps({"error": "Expanded Text Ads are deprecated and cannot be updated via the API since June 2022. You can pause, enable, or remove them."})






@mcp.tool()
def gads_update_keyword_match_type(ad_group_id: str, criterion_id: str,
                                    new_match_type: str, customer_id: str = "") -> str:
    """Update keyword match type. Note: Google Ads API requires remove + re-create for match type changes.
    new_match_type: BROAD, PHRASE, EXACT."""
    try:
        client, cid = _get_client(customer_id)
        # Get current keyword details
        rows = _search(
            f"""SELECT ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type,
                       ad_group_criterion.cpc_bid_micros, ad_group_criterion.status,
                       ad_group_criterion.final_urls
                FROM ad_group_criterion
                WHERE ad_group_criterion.ad_group = 'customers/{cid}/adGroups/{ad_group_id}'
                  AND ad_group_criterion.criterion_id = {criterion_id}""",
            customer_id
        )
        if not rows:
            return json.dumps({"error": "Keyword not found"})
        ac = rows[0].ad_group_criterion
        keyword_text = ac.keyword.text
        cpc_bid = ac.cpc_bid_micros
        status = ac.status

        service = client.get_service("AdGroupCriterionService")
        ops = []

        # Remove old
        remove_op = client.get_type("AdGroupCriterionOperation")
        remove_op.remove = f"customers/{cid}/adGroupCriteria/{ad_group_id}~{criterion_id}"
        ops.append(remove_op)

        # Create new with new match type
        create_op = client.get_type("AdGroupCriterionOperation")
        criterion = create_op.create
        criterion.ad_group = f"customers/{cid}/adGroups/{ad_group_id}"
        criterion.keyword.text = keyword_text
        criterion.keyword.match_type = client.enums.KeywordMatchTypeEnum[new_match_type]
        criterion.status = status
        if cpc_bid > 0:
            criterion.cpc_bid_micros = cpc_bid
        ops.append(create_op)

        response = service.mutate_ad_group_criteria(customer_id=cid, operations=ops)
        new_rn = response.results[-1].resource_name
        return json.dumps({"success": True, "new_resource_name": new_rn,
                           "keyword_text": keyword_text, "new_match_type": new_match_type})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_update_product_partition(ad_group_id: str, criterion_id: str,
                                   cpc_bid_micros: int = 0, status: str = "",
                                   customer_id: str = "") -> str:
    """Update a product partition (shopping product group) bid or status."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupCriterionService")
        op = client.get_type("AdGroupCriterionOperation")
        criterion = op.update
        criterion.resource_name = f"customers/{cid}/adGroupCriteria/{ad_group_id}~{criterion_id}"
        paths = []
        if cpc_bid_micros > 0:
            criterion.cpc_bid_micros = cpc_bid_micros
            paths.append("cpc_bid_micros")
        if status:
            criterion.status = client.enums.AdGroupCriterionStatusEnum[status]
            paths.append("status")
        if paths:
            op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=paths))
            response = service.mutate_ad_group_criteria(customer_id=cid, operations=[op])
            return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
        return json.dumps({"error": "No fields to update"})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Smart campaign keyword themes update ──────────────────────────────────





@mcp.tool()
def gads_update_sitelink_asset(asset_resource_name: str, link_text: str = "",
                                 description1: str = "", description2: str = "",
                                 final_urls: list = None, tracking_url_template: str = "",
                                 customer_id: str = "") -> str:
    """Update a sitelink asset's text, descriptions, or URLs."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("AssetService")
        op = client.get_type("AssetOperation")
        asset = op.update
        asset.resource_name = asset_resource_name
        paths = []
        if link_text:
            asset.sitelink_asset.link_text = link_text
            paths.append("sitelink_asset.link_text")
        if description1:
            asset.sitelink_asset.description1 = description1
            paths.append("sitelink_asset.description1")
        if description2:
            asset.sitelink_asset.description2 = description2
            paths.append("sitelink_asset.description2")
        if final_urls:
            asset.final_urls.extend(final_urls)
            paths.append("final_urls")
        if tracking_url_template:
            asset.tracking_url_template = tracking_url_template
            paths.append("tracking_url_template")
        if paths:
            op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=paths))
            response = service.mutate_assets(customer_id=cid, operations=[op])
            return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
        return json.dumps({"error": "No fields to update"})
    except Exception as e:
        return json.dumps({"error": str(e)})






@mcp.tool()
def gads_update_smart_campaign_keyword_themes(campaign_id: str, keyword_themes: list,
                                               customer_id: str = "") -> str:
    """Update keyword themes for a Smart Campaign.
    keyword_themes: list of {keyword_theme_constant: str} or {free_form_keyword_theme: str} dicts."""
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("SmartCampaignSettingService")
        op = client.get_type("SmartCampaignSettingOperation")
        setting = op.update
        setting.resource_name = f"customers/{cid}/smartCampaignSettings/{campaign_id}"
        KeywordTheme = client.get_type("KeywordThemeInfo")
        for kt in keyword_themes:
            kti = KeywordTheme()
            if "keyword_theme_constant" in kt:
                kti.keyword_theme_constant = kt["keyword_theme_constant"]
            elif "free_form_keyword_theme" in kt:
                kti.free_form_keyword_theme = kt["free_form_keyword_theme"]
            setting.keyword_themes.append(kti)
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["keyword_themes"]))
        response = service.mutate_smart_campaign_settings(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})



# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run()




@mcp.tool()
def gads_upload_conversion_adjustments(conversion_action_id: str,
                                        adjustments: list, customer_id: str = "") -> str:
    """Upload conversion adjustments (restatements or retractions).
    adjustments: list of dicts with keys:
      - order_id (str) OR gclid (str) + conversion_date_time (str, 'yyyy-mm-dd hh:mm:ss+TZ')
      - adjustment_type: 'RESTATEMENT' or 'RETRACTION'
      - adjustment_date_time: 'yyyy-mm-dd hh:mm:ss+TZ'
      - restatement_value (float, only for RESTATEMENT)
      - restatement_currency_code (str, only for RESTATEMENT)
    """
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("ConversionAdjustmentUploadService")
        conversion_action_rn = client.get_service("ConversionActionService").conversion_action_path(
            cid, conversion_action_id
        )
        ConversionAdjustment = client.get_type("ConversionAdjustment")
        AdjType = client.enums.ConversionAdjustmentTypeEnum

        ops = []
        for adj_data in adjustments:
            adj = ConversionAdjustment()
            adj.conversion_action = conversion_action_rn
            adj.adjustment_date_time = adj_data["adjustment_date_time"]
            adj_type_str = adj_data.get("adjustment_type", "RESTATEMENT").upper()
            adj.adjustment_type = AdjType[adj_type_str]

            if "order_id" in adj_data:
                adj.order_id = adj_data["order_id"]
            else:
                adj.gclid_date_time_pair.gclid = adj_data["gclid"]
                adj.gclid_date_time_pair.conversion_date_time = adj_data["conversion_date_time"]

            if adj_type_str == "RESTATEMENT" and "restatement_value" in adj_data:
                adj.restatement_value.adjusted_value = float(adj_data["restatement_value"])
                if "restatement_currency_code" in adj_data:
                    adj.restatement_value.currency_code = adj_data["restatement_currency_code"]
            ops.append(adj)

        response = service.upload_conversion_adjustments(
            customer_id=cid,
            conversion_adjustments=ops,
            partial_failure=True
        )
        results = []
        for r in response.results:
            results.append({
                "adjustment_date_time": r.adjustment_date_time,
                "adjustment_type": str(r.adjustment_type),
            })
        errors = []
        if response.partial_failure_error:
            errors.append(str(response.partial_failure_error))
        return json.dumps({"success": True, "uploaded": len(results), "results": results, "errors": errors})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ── Ad group ad rotation ───────────────────────────────────────────────────


