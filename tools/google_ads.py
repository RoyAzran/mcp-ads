"""
Google Ads tools — adapted for remote server.
All 54 tools from google-ads-mcp-server/server.py.
Credentials come from current_user_ctx instead of env vars.
Write tools check require_editor() before executing.
"""
import json
import os
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

def _normalize_customer_id(customer_id: str = "") -> str:
    return str(customer_id or "").strip().replace("-", "")


def _client_config(customer_id: str = ""):
    user = current_user_ctx.get(None)
    if user is None:
        raise RuntimeError("Not authenticated.")
    from credentials import provider
    # The multi-connection resolution, the oldest-connection fallback when no
    # customer_id disambiguates, and the legacy single-token lookup all live in
    # the provider now; the ctx override stays here because it is a fact about
    # this request, not about credential storage.
    refresh_token = current_connection_token_ctx.get(None) or provider().google_token(
        "google_ads", customer_id
    )
    if not refresh_token:
        raise RuntimeError("Google Ads account not connected. Connect your Google account via " + connect_hint() + ".")

    return {
        "developer_token": GOOGLE_ADS_DEVELOPER_TOKEN,
        "client_id":       GOOGLE_CLIENT_ID,
        "client_secret":   GOOGLE_CLIENT_SECRET,
        "refresh_token":   refresh_token,
        "use_proto_plus":  True,
    }


def _get_client(customer_id: str = "", login_customer_id: str = ""):
    config = _client_config(customer_id)
    # Do NOT inject a global login_customer_id — each user accesses their own accounts.
    # login_customer_id is only needed when acting as a manager on a client account.
    login_cid = _normalize_customer_id(login_customer_id)
    cid = _normalize_customer_id(customer_id)
    if not login_cid and cid:
        login_cid = _find_login_customer_id_for_customer(cid)
    if login_cid:
        config["login_customer_id"] = login_cid

    if not cid:
        raise RuntimeError("No customer_id provided. Call google_ads_find_account or google_ads_list_customers first to find your account IDs.")

    return GoogleAdsClient.load_from_dict(config), cid


def _search(gaql: str, customer_id: str = "", login_customer_id: str = "") -> list:
    client, cid = _get_client(customer_id, login_customer_id)
    service = client.get_service("GoogleAdsService")
    try:
        return list(service.search(customer_id=cid, query=gaql))
    except Exception as error:
        if login_customer_id:
            raise
        if "CUSTOMER_NOT_FOUND" not in str(error) and "not found" not in str(error).lower():
            raise
        manager_id = _find_login_customer_id_for_customer(cid)
        if not manager_id:
            raise
        retry_client, retry_cid = _get_client(cid, manager_id)
        retry_service = retry_client.get_service("GoogleAdsService")
        return list(retry_service.search(customer_id=retry_cid, query=gaql))


# Resolving a child account's manager (MCC) requires walking every accessible
# manager and running a customer_client query against each -- slow (multiple
# sequential Google Ads API calls) and identical for a given customer_id every
# time. Cache the result in-process so a page that fires several tool calls
# for the same account (like the Insights dashboard) only pays for it once.
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

    base_client = GoogleAdsClient.load_from_dict(_client_config(target_id))
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
            manager_client, manager_cid = _get_client(manager_id, manager_id)
            service = manager_client.get_service("GoogleAdsService")
            rows = service.search(customer_id=manager_cid, query=gaql)
            for row in rows:
                if str(row.customer_client.id) == target_id:
                    return _cache_and_return(manager_id)
        except Exception:
            continue
    return _cache_and_return("")


def _google_ads_hint(error: Exception, customer_id: str = "") -> dict:
    message = str(error)
    hint = None
    if "CUSTOMER_NOT_FOUND" in message or "not found" in message.lower():
        hint = (
            "If this account is under an MCC, call google_ads_find_account with the target customer_id. "
            "Then retry the Google Ads tool with customer_id set to the client account and "
            "login_customer_id set to the MCC/manager account returned by the diagnostic."
        )
    return {"error": message, "customer_id": _normalize_customer_id(customer_id), "hint": hint}


def _m(micros) -> float:
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
# Analytics Tools (READ — all roles)
# ---------------------------------------------------------------------------

@mcp.tool()
def google_ads_list_customers() -> str:
    """List every Google Ads account accessible to the authenticated user,
    expanding any accessible MCC (manager) account into its client accounts.
    """
    try:
        from credentials import provider

        user = current_user_ctx.get(None)
        configs = [_client_config()] if current_connection_token_ctx.get(None) else None
        connection_email_by_config_index: dict = {}
        if configs is None:
            # With several logins connected, merge discovery across all of
            # them; with one or none (legacy token), _client_config resolves
            # the single identity exactly as before.
            connections = provider().list_connections("google_ads") if user else []
            if len(connections) >= 2:
                configs = []
                for i, conn in enumerate(connections):
                    configs.append({
                        "developer_token": GOOGLE_ADS_DEVELOPER_TOKEN,
                        "client_id": GOOGLE_CLIENT_ID,
                        "client_secret": GOOGLE_CLIENT_SECRET,
                        "refresh_token": conn["token"],
                        "use_proto_plus": True,
                    })
                    connection_email_by_config_index[i] = conn["email"]
            else:
                configs = [_client_config()]

        gaql = """
            SELECT customer_client.client_customer, customer_client.descriptive_name,
                   customer_client.id, customer_client.manager, customer_client.status,
                   customer_client.currency_code, customer_client.time_zone, customer_client.level
            FROM customer_client
        """
        seen_ids: set = set()
        customers = []
        skipped_managers = []
        for config_index, config in enumerate(configs):
            client = GoogleAdsClient.load_from_dict(config)
            customer_service = client.get_service("CustomerService")
            accessible = customer_service.list_accessible_customers()
            top_level_ids = [rn.split("/")[-1] for rn in accessible.resource_names]
            connection_email = connection_email_by_config_index.get(config_index, "")

            for manager_id in top_level_ids:
                try:
                    manager_client = GoogleAdsClient.load_from_dict({**config, "login_customer_id": manager_id})
                    service = manager_client.get_service("GoogleAdsService")
                    rows = list(service.search(customer_id=manager_id, query=gaql))
                except Exception as manager_error:
                    skipped_managers.append({"customer_id": manager_id, "error": str(manager_error)})
                    continue
                for row in rows:
                    account = row.customer_client
                    account_id = str(account.id)
                    if account_id in seen_ids:
                        continue
                    seen_ids.add(account_id)
                    entry = {
                        "id": account_id,
                        "name": account.descriptive_name or account_id,
                        "status": account.status.name,
                        "manager": bool(account.manager),
                        "level": int(account.level),
                        "currency_code": account.currency_code,
                        "timezone": account.time_zone,
                        "manager_account_id": manager_id if account_id != manager_id else "",
                    }
                    if connection_email:
                        entry["connected_as"] = connection_email
                    customers.append(entry)
        return json.dumps({
            "customers": customers,
            "total": len(customers),
            "skipped_managers": skipped_managers,
        })
    except Exception as e:
        return json.dumps(_google_ads_hint(e))


@mcp.tool()
def google_ads_find_account(target_customer_id: str = "", manager_customer_id: str = "") -> str:
    """Find a Google Ads account across directly accessible accounts and MCC children.

    Use this first when a client account is missing or an error says CUSTOMER_NOT_FOUND.

    Args:
        target_customer_id: Optional client account ID to find, with or without dashes.
        manager_customer_id: Optional MCC/manager ID to search first, with or without dashes.
    """
    try:
        target_id = _normalize_customer_id(target_customer_id)
        preferred_manager = _normalize_customer_id(manager_customer_id)

        base_client = GoogleAdsClient.load_from_dict(_client_config(target_id))
        customer_service = base_client.get_service("CustomerService")
        accessible = customer_service.list_accessible_customers()
        accessible_ids = [rn.split("/")[-1] for rn in accessible.resource_names]

        managers_to_check = []
        if preferred_manager:
            managers_to_check.append(preferred_manager)
        managers_to_check.extend([cid for cid in accessible_ids if cid not in managers_to_check])

        matches = []
        searched_managers = []
        direct_match = target_id and target_id in accessible_ids
        if direct_match:
            matches.append({
                "customer_id": target_id,
                "login_customer_id": "",
                "relationship": "direct_access",
                "next_call_params": {"customer_id": target_id},
            })

        gaql = """
            SELECT customer_client.client_customer, customer_client.descriptive_name,
                   customer_client.id, customer_client.manager, customer_client.status,
                   customer_client.currency_code, customer_client.time_zone, customer_client.level
            FROM customer_client
            ORDER BY customer_client.level ASC, customer_client.descriptive_name ASC
        """
        for manager_id in managers_to_check:
            try:
                rows = _search(gaql, manager_id, manager_id)
            except Exception as manager_error:
                searched_managers.append({"manager_customer_id": manager_id, "error": str(manager_error)})
                continue

            searched_managers.append({"manager_customer_id": manager_id, "accounts_checked": len(rows)})
            for row in rows:
                account = row.customer_client
                account_id = str(account.id)
                if target_id and account_id != target_id:
                    continue
                matches.append({
                    "customer_id": account_id,
                    "name": account.descriptive_name,
                    "status": account.status.name,
                    "manager": bool(account.manager),
                    "level": int(account.level),
                    "currency": account.currency_code,
                    "timezone": account.time_zone,
                    "login_customer_id": manager_id if account_id != manager_id else "",
                    "relationship": "mcc_child" if account_id != manager_id else "manager_self",
                    "next_call_params": {"customer_id": account_id, "login_customer_id": manager_id} if account_id != manager_id else {"customer_id": account_id},
                })

        found = bool(matches) if target_id else None
        return json.dumps({
            "target_customer_id": target_id,
            "found": found,
            "matches": matches,
            "directly_accessible_customers": accessible_ids,
            "searched_managers": searched_managers,
            "recommendation": (
                "Use the next_call_params from the matching account on Google Ads tools. "
                "If found is false, the connected Google user does not appear to have access to this account through the listed MCCs."
            ),
        })
    except Exception as e:
        return json.dumps(_google_ads_hint(e, target_customer_id))


@mcp.tool()
def google_ads_get_account_info(customer_id: str = "") -> str:
    """Get basic information about a Google Ads account: name, currency, timezone, status.

    Args:
        customer_id: Google Ads customer ID (10 digits). Leave blank to use default.
    """
    try:
        rows = _search("""
            SELECT customer.id, customer.descriptive_name, customer.currency_code,
                   customer.time_zone, customer.status, customer.manager
            FROM customer LIMIT 1
        """, customer_id)
        if not rows:
            return json.dumps({"error": "No customer info returned."})
        c = rows[0].customer
        return json.dumps({
            "id": str(c.id), "name": c.descriptive_name, "currency": c.currency_code,
            "timezone": c.time_zone, "status": c.status.name, "manager": c.manager,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_account_overview(start_date: str = "", end_date: str = "", customer_id: str = "") -> str:
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
            "impressions": int(m.impressions), "clicks": int(m.clicks), "spend": spend,
            "ctr": _pct(m.ctr), "avg_cpc": _m(m.average_cpc),
            "conversions": float(m.conversions), "conversion_value": float(m.conversions_value),
            "cost_per_conversion": _m(m.cost_per_conversion),
            "roas": round(float(m.conversions_value) / max(spend, 0.01), 2),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_daily_trend(start_date: str = "", end_date: str = "", customer_id: str = "") -> str:
    """Get day-by-day account performance: spend, impressions, clicks, conversions per day.

    Args:
        start_date: Start date YYYY-MM-DD. Defaults to 28 days ago.
        end_date: End date YYYY-MM-DD. Defaults to today.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        sd, ed = _date_range(start_date, end_date)
        rows = _search(f"""
            SELECT segments.date, metrics.impressions, metrics.clicks, metrics.cost_micros,
                   metrics.conversions, metrics.conversions_value
            FROM customer
            WHERE segments.date BETWEEN '{sd}' AND '{ed}'
            ORDER BY segments.date ASC
        """, customer_id)
        daily = [{
            "date": row.segments.date,
            "spend": _m(row.metrics.cost_micros),
            "impressions": int(row.metrics.impressions),
            "clicks": int(row.metrics.clicks),
            "conversions": float(row.metrics.conversions),
            "conversion_value": float(row.metrics.conversions_value),
        } for row in rows]
        return json.dumps({"date_range": f"{sd} to {ed}", "daily": daily})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_campaign_performance(start_date: str = "", end_date: str = "", customer_id: str = "", status_filter: str = "ENABLED", row_limit: int = 25) -> str:
    """Get Google Ads campaign-level performance: spend, impressions, clicks, conversions, CTR per campaign.

    Args:
        start_date: Start date YYYY-MM-DD. Defaults to 28 days ago.
        end_date: End date YYYY-MM-DD. Defaults to today.
        customer_id: Google Ads customer ID. Leave blank to use default.
        status_filter: ENABLED, PAUSED, REMOVED, or ALL. Default ENABLED.
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
            ORDER BY metrics.cost_micros DESC LIMIT {row_limit}
        """, customer_id)
        results = []
        for row in rows:
            c, m = row.campaign, row.metrics
            results.append({
                "id": str(c.id), "name": c.name, "status": c.status.name,
                "bidding_strategy": c.bidding_strategy_type.name,
                "spend": _m(m.cost_micros), "impressions": int(m.impressions),
                "clicks": int(m.clicks), "ctr": _pct(m.ctr), "avg_cpc": _m(m.average_cpc),
                "conversions": float(m.conversions), "conversion_value": float(m.conversions_value),
            })
        return json.dumps({"date_range": f"{sd} to {ed}", "campaigns": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_adgroup_performance(start_date: str = "", end_date: str = "", customer_id: str = "", campaign_id: str = "", row_limit: int = 25) -> str:
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
            AND ad_group.status != 'REMOVED' {where_campaign}
            ORDER BY metrics.cost_micros DESC LIMIT {row_limit}
        """, customer_id)
        results = []
        for row in rows:
            ag, m = row.ad_group, row.metrics
            results.append({
                "id": str(ag.id), "name": ag.name, "status": ag.status.name,
                "campaign": row.campaign.name, "spend": _m(m.cost_micros),
                "impressions": int(m.impressions), "clicks": int(m.clicks),
                "ctr": _pct(m.ctr), "avg_cpc": _m(m.average_cpc), "conversions": float(m.conversions),
            })
        return json.dumps({"date_range": f"{sd} to {ed}", "ad_groups": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_keyword_performance(start_date: str = "", end_date: str = "", customer_id: str = "", campaign_id: str = "", row_limit: int = 50, sort_by: str = "cost") -> str:
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
            AND ad_group_criterion.status != 'REMOVED' {where_campaign}
            ORDER BY {sort_field} DESC LIMIT {row_limit}
        """, customer_id)
        results = []
        for row in rows:
            c, m = row.ad_group_criterion, row.metrics
            results.append({
                "keyword": c.keyword.text, "match_type": c.keyword.match_type.name,
                "status": c.status.name, "quality_score": c.quality_info.quality_score or None,
                "campaign": row.campaign.name, "ad_group": row.ad_group.name,
                "spend": _m(m.cost_micros), "impressions": int(m.impressions), "clicks": int(m.clicks),
                "ctr": _pct(m.ctr), "avg_cpc": _m(m.average_cpc), "conversions": float(m.conversions),
                "search_impression_share": _pct(m.search_impression_share),
            })
        return json.dumps({"date_range": f"{sd} to {ed}", "keywords": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


def _pmax_search_term_insights(sd: str, ed: str, customer_id: str, campaign_id: str, row_limit: int) -> list[dict]:
    """Search-term insights for Performance Max campaigns.

    PMax is invisible to search_term_view -- Google returns zero rows for it, so
    a report built only on that resource silently looks "empty" for a PMax
    campaign rather than erroring. The data lives on the search-term-insight
    resources instead, with four constraints proven against the live API (v22):

      * They report a *category* (`category_label`), not the raw query. One row
        per scope comes back with an empty label -- that is Google's
        "uncategorized / other" bucket, not a bug.
      * metrics.cost_micros and metrics.average_cpc are NOT selectable here, so
        there is no spend or CPC per term. ctr/impressions/clicks/conversions
        are.
      * segments.date is filterable in WHERE but NOT selectable in SELECT.
      * campaign_search_term_insight rejects any query that does not filter to a
        single campaign (REQUIRES_FILTER_BY_SINGLE_RESOURCE), so the
        account-wide case must use customer_search_term_insight instead. Using
        the campaign resource for both is what made the account-level call fail.
    """
    if campaign_id:
        resource, field = "campaign_search_term_insight", "campaign_search_term_insight"
        where = [
            f"campaign_search_term_insight.campaign_id = {str(campaign_id).strip()}",
            f"segments.date BETWEEN '{sd}' AND '{ed}'",
        ]
        extra_select = ", campaign_search_term_insight.campaign_id"
    else:
        resource, field = "customer_search_term_insight", "customer_search_term_insight"
        where = [f"segments.date BETWEEN '{sd}' AND '{ed}'"]
        extra_select = ""

    rows = _search(f"""
        SELECT {field}.category_label{extra_select},
               metrics.impressions, metrics.clicks, metrics.ctr,
               metrics.conversions, metrics.conversions_value
        FROM {resource}
        WHERE {' AND '.join(where)}
        ORDER BY metrics.impressions DESC LIMIT {row_limit}
    """, customer_id)
    out = []
    for row in rows:
        insight = getattr(row, field)
        m = row.metrics
        item = {
            "search_category": insight.category_label or "(uncategorized)",
            "impressions": int(m.impressions), "clicks": int(m.clicks),
            "ctr": _pct(m.ctr), "conversions": float(m.conversions),
            "conversions_value": float(m.conversions_value),
        }
        if campaign_id:
            item["campaign_id"] = str(insight.campaign_id)
        out.append(item)
    return out


@mcp.tool()
def google_ads_search_terms(start_date: str = "", end_date: str = "", customer_id: str = "", campaign_id: str = "", row_limit: int = 50) -> str:
    """Get Google Ads search terms — the actual queries that triggered ads.

    Covers Search/Shopping campaigns (exact queries, with spend) AND Performance
    Max campaigns (search *categories*, no spend — a Google API limitation).

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
            WHERE segments.date BETWEEN '{sd}' AND '{ed}' {where_campaign}
            ORDER BY metrics.clicks DESC LIMIT {row_limit}
        """, customer_id)
        results = []
        for row in rows:
            stv, m = row.search_term_view, row.metrics
            results.append({
                "search_term": stv.search_term, "status": stv.status.name,
                "campaign": row.campaign.name, "ad_group": row.ad_group.name,
                "impressions": int(m.impressions), "clicks": int(m.clicks),
                "spend": _m(m.cost_micros), "ctr": _pct(m.ctr),
                "avg_cpc": _m(m.average_cpc), "conversions": float(m.conversions),
            })

        payload = {"date_range": f"{sd} to {ed}", "search_terms": results}

        # Always attempt PMax too. Reporting only search_term_view meant a PMax
        # campaign returned an empty list with no explanation, which reads as
        # "no search terms" rather than "wrong resource for this campaign type".
        try:
            pmax = _pmax_search_term_insights(sd, ed, customer_id, campaign_id, row_limit)
        except Exception as pmax_error:  # never let PMax break the Search report
            payload["pmax_search_categories_error"] = str(pmax_error)[:300]
        else:
            if pmax:
                payload["pmax_search_categories"] = pmax
                payload["pmax_note"] = (
                    "Performance Max does not expose raw search terms. Google reports "
                    "search *categories* instead, and provides no spend or CPC for them "
                    "-- impressions, clicks, CTR and conversions only. "
                    "'(uncategorized)' is Google's catch-all bucket."
                )
            elif not results:
                payload["note"] = (
                    "No search terms found. Search/Shopping campaigns report exact queries; "
                    "Performance Max reports search categories and returned none for this "
                    "period, which usually means too little volume for Google to categorize."
                )
        return json.dumps(payload)
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_ad_performance(start_date: str = "", end_date: str = "", customer_id: str = "", campaign_id: str = "", row_limit: int = 25) -> str:
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
            AND ad_group_ad.status != 'REMOVED' {where_campaign}
            ORDER BY metrics.cost_micros DESC LIMIT {row_limit}
        """, customer_id)
        results = []
        for row in rows:
            aga, m = row.ad_group_ad, row.metrics
            results.append({
                "id": str(aga.ad.id), "name": aga.ad.name, "type": aga.ad.type_.name,
                "status": aga.status.name, "campaign": row.campaign.name,
                "ad_group": row.ad_group.name, "spend": _m(m.cost_micros),
                "impressions": int(m.impressions), "clicks": int(m.clicks),
                "ctr": _pct(m.ctr), "conversions": float(m.conversions),
            })
        return json.dumps({"date_range": f"{sd} to {ed}", "ads": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_demographic_breakdown(start_date: str = "", end_date: str = "", customer_id: str = "", campaign_id: str = "") -> str:
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
            WHERE segments.date BETWEEN '{sd}' AND '{ed}' {where_campaign}
            ORDER BY metrics.cost_micros DESC
        """, customer_id)
        gender_rows = _search(f"""
            SELECT ad_group_criterion.gender.type,
                   metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions
            FROM gender_view
            WHERE segments.date BETWEEN '{sd}' AND '{ed}' {where_campaign}
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
def google_ads_geo_performance(start_date: str = "", end_date: str = "", customer_id: str = "", row_limit: int = 25) -> str:
    """Get Google Ads performance broken down by geographic location.

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
            ORDER BY metrics.cost_micros DESC LIMIT {row_limit}
        """, customer_id)
        results = []
        for row in rows:
            m = row.metrics
            gv = row.geographic_view
            results.append({
                "country_criterion_id": gv.country_criterion_id,
                "location_type": gv.location_type.name if hasattr(gv.location_type, "name") else str(gv.location_type),
                "spend": _m(m.cost_micros),
                "impressions": int(m.impressions), "clicks": int(m.clicks), "conversions": float(m.conversions),
            })
        return json.dumps({"date_range": f"{sd} to {ed}", "locations": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_hourly_breakdown(start_date: str = "", end_date: str = "", customer_id: str = "") -> str:
    """Get Google Ads performance by hour of day to identify peak performing times.

    Args:
        start_date: Start date YYYY-MM-DD. Defaults to 7 days ago.
        end_date: End date YYYY-MM-DD. Defaults to today.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        sd, ed = _date_range(start_date, end_date, default_days=7)
        rows = _search(f"""
            SELECT segments.hour, metrics.impressions, metrics.clicks,
                   metrics.cost_micros, metrics.conversions
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
def google_ads_device_breakdown(start_date: str = "", end_date: str = "", customer_id: str = "") -> str:
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
            device, m = row.segments.device.name, row.metrics
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
def google_ads_shopping_performance(start_date: str = "", end_date: str = "", customer_id: str = "", row_limit: int = 25) -> str:
    """Get Google Shopping campaign performance by product.

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
            ORDER BY metrics.cost_micros DESC LIMIT {row_limit}
        """, customer_id)
        results = []
        for row in rows:
            segs, m = row.segments, row.metrics
            spend = _m(m.cost_micros)
            conv_value = float(m.conversions_value)
            results.append({
                "title": segs.product_title, "item_id": segs.product_item_id,
                "brand": segs.product_brand, "spend": spend,
                "impressions": int(m.impressions), "clicks": int(m.clicks),
                "conversions": float(m.conversions), "conversion_value": conv_value,
                "roas": round(conv_value / max(spend, 0.01), 2),
            })
        return json.dumps({"date_range": f"{sd} to {ed}", "products": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_display_performance(start_date: str = "", end_date: str = "", customer_id: str = "", row_limit: int = 25) -> str:
    """Get Google Display Network campaign performance.

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
            AND campaign.advertising_channel_type = 'DISPLAY' AND campaign.status = 'ENABLED'
            ORDER BY metrics.cost_micros DESC LIMIT {row_limit}
        """, customer_id)
        results = []
        for row in rows:
            c, m = row.campaign, row.metrics
            results.append({
                "name": c.name, "spend": _m(m.cost_micros), "impressions": int(m.impressions),
                "clicks": int(m.clicks), "ctr": _pct(m.ctr), "conversions": float(m.conversions),
                "viewability_rate": _pct(m.active_view_viewability),
            })
        return json.dumps({"date_range": f"{sd} to {ed}", "display_campaigns": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_video_performance(start_date: str = "", end_date: str = "", customer_id: str = "", row_limit: int = 25) -> str:
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
            ORDER BY metrics.cost_micros DESC LIMIT {row_limit}
        """, customer_id)
        results = []
        for row in rows:
            c, m = row.campaign, row.metrics
            results.append({
                "name": c.name, "spend": _m(m.cost_micros), "impressions": int(m.impressions),
                "video_views": int(m.video_views), "view_rate": _pct(m.video_view_rate),
                "avg_cpv": round(float(m.average_cpv) / 1_000_000, 6), "conversions": float(m.conversions),
            })
        return json.dumps({"date_range": f"{sd} to {ed}", "video_campaigns": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_landing_page_performance(start_date: str = "", end_date: str = "", customer_id: str = "", row_limit: int = 25) -> str:
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
                   metrics.conversions, metrics.mobile_friendly_clicks_percentage, metrics.speed_score
            FROM landing_page_view
            WHERE segments.date BETWEEN '{sd}' AND '{ed}'
            ORDER BY metrics.clicks DESC LIMIT {row_limit}
        """, customer_id)
        results = []
        for row in rows:
            lp, m = row.landing_page_view, row.metrics
            results.append({
                "url": lp.unexpanded_final_url, "spend": _m(m.cost_micros),
                "impressions": int(m.impressions), "clicks": int(m.clicks),
                "conversions": float(m.conversions),
                "mobile_friendly_rate": _pct(m.mobile_friendly_clicks_percentage),
                "speed_score": m.speed_score,
            })
        return json.dumps({"date_range": f"{sd} to {ed}", "landing_pages": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_audience_performance(start_date: str = "", end_date: str = "", customer_id: str = "", row_limit: int = 25) -> str:
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
            ORDER BY metrics.cost_micros DESC LIMIT {row_limit}
        """, customer_id)
        results = []
        for row in rows:
            m = row.metrics
            results.append({
                "audience": row.user_list.name, "campaign": row.campaign.name,
                "spend": _m(m.cost_micros), "impressions": int(m.impressions),
                "clicks": int(m.clicks), "conversions": float(m.conversions),
            })
        return json.dumps({"date_range": f"{sd} to {ed}", "audiences": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_conversion_actions(customer_id: str = "") -> str:
    """List all conversion actions configured in the Google Ads account.

    Args:
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        rows = _search("""
            SELECT conversion_action.id, conversion_action.name, conversion_action.status,
                   conversion_action.type, conversion_action.category,
                   conversion_action.value_settings.default_value
            FROM conversion_action WHERE conversion_action.status = 'ENABLED'
        """, customer_id)
        results = []
        for row in rows:
            ca = row.conversion_action
            results.append({
                "id": str(ca.id), "name": ca.name, "type": ca.type_.name,
                "category": ca.category.name, "default_value": ca.value_settings.default_value,
            })
        return json.dumps({"conversion_actions": results, "total": len(results)})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_budget_pacing(customer_id: str = "") -> str:
    """Get budget pacing for all active campaigns — daily budget, amount spent, and percentage used.

    Args:
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        today = str(date.today())
        rows = _search(f"""
            SELECT campaign.name, campaign.status, campaign_budget.amount_micros, metrics.cost_micros
            FROM campaign
            WHERE segments.date = '{today}' AND campaign.status = 'ENABLED'
            ORDER BY metrics.cost_micros DESC
        """, customer_id)
        results = []
        for row in rows:
            budget = _m(row.campaign_budget.amount_micros)
            spent = _m(row.metrics.cost_micros)
            pct = round(spent / max(budget, 0.01) * 100, 1)
            results.append({"campaign": row.campaign.name, "daily_budget": budget, "spent_today": spent, "pacing_pct": f"{pct}%"})
        return json.dumps({"date": today, "campaigns": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_auction_insights(start_date: str = "", end_date: str = "", customer_id: str = "", campaign_id: str = "") -> str:
    """Get competitive pressure per campaign: impression share, and share lost to budget vs rank.

    Note: Google does not expose the Auction Insights report (competitor domains,
    overlap rate, outranking share) through the Ads API -- that data is available
    only in the Google Ads UI. This returns the impression-share metrics the API
    does provide, which answer the same underlying question: how much of the
    available auction you are winning, and whether you lose it to budget or rank.

    Args:
        start_date: Start date YYYY-MM-DD. Defaults to 28 days ago.
        end_date: End date YYYY-MM-DD. Defaults to today.
        customer_id: Google Ads customer ID. Leave blank to use default.
        campaign_id: Filter by campaign ID. Leave blank for all.
    """
    try:
        sd, ed = _date_range(start_date, end_date)
        where_campaign = f"AND campaign.id = '{campaign_id}'" if campaign_id else ""
        # Every field here lives on `campaign`. The old query used FROM
        # auction_insight with metrics.search_overlap_rate/search_outranking_share,
        # which the API rejects outright (BAD_RESOURCE_TYPE_IN_FROM_CLAUSE) for
        # anyone not allowlisted -- so the tool could never return data at all.
        rows = _search(f"""
            SELECT campaign.id, campaign.name, campaign.status,
                   metrics.search_impression_share,
                   metrics.search_top_impression_share,
                   metrics.search_absolute_top_impression_share,
                   metrics.search_budget_lost_impression_share,
                   metrics.search_rank_lost_impression_share,
                   metrics.impressions, metrics.clicks, metrics.cost_micros
            FROM campaign
            WHERE segments.date BETWEEN '{sd}' AND '{ed}' {where_campaign}
              AND campaign.status != 'REMOVED'
            ORDER BY metrics.impressions DESC
        """, customer_id)
        results = []
        for row in rows:
            m = row.metrics
            results.append({
                "campaign_id": str(row.campaign.id),
                "campaign_name": row.campaign.name,
                "status": row.campaign.status.name,
                "impression_share": _pct(m.search_impression_share),
                "top_impression_share": _pct(m.search_top_impression_share),
                "absolute_top_impression_share": _pct(m.search_absolute_top_impression_share),
                "lost_to_budget": _pct(m.search_budget_lost_impression_share),
                "lost_to_rank": _pct(m.search_rank_lost_impression_share),
                "impressions": m.impressions,
                "clicks": m.clicks,
                "cost": _m(m.cost_micros),
            })
        return json.dumps({
            "date_range": f"{sd} to {ed}",
            "campaigns": results,
            "note": (
                "Competitor domains, overlap rate and outranking share are not available "
                "through the Google Ads API -- view them in the Google Ads UI under "
                "Auction Insights. lost_to_budget vs lost_to_rank tells you whether to "
                "raise budget or improve Ad Rank."
            ),
        })
    except Exception as e:
        return json.dumps(_google_ads_hint(e, customer_id))


@mcp.tool()
def google_ads_change_history(start_date: str = "", end_date: str = "", customer_id: str = "", row_limit: int = 25) -> str:
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
            ORDER BY change_event.change_date_time DESC LIMIT {row_limit}
        """, customer_id)
        results = []
        for row in rows:
            ce = row.change_event
            try:
                changed = list(ce.changed_fields.paths)
            except Exception:
                changed = str(ce.changed_fields)
            results.append({
                "date_time": ce.change_date_time, "operation": ce.change_resource_operation.name,
                "client_type": ce.client_type.name, "resource": ce.change_resource_name,
                "changed_fields": changed, "user": ce.user_email,
            })
        return json.dumps({"date_range": f"{sd} to {ed}", "changes": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_quality_score(customer_id: str = "", campaign_id: str = "", row_limit: int = 50) -> str:
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
            AND ad_group_criterion.quality_info.quality_score > 0 {where_campaign}
            ORDER BY ad_group_criterion.quality_info.quality_score ASC LIMIT {row_limit}
        """, customer_id)
        results = []
        for row in rows:
            kw, qi = row.ad_group_criterion, row.ad_group_criterion.quality_info
            results.append({
                "keyword": kw.keyword.text, "match_type": kw.keyword.match_type.name,
                "quality_score": qi.quality_score, "ad_relevance": qi.creative_quality_score.name,
                "landing_page_exp": qi.post_click_quality_score.name,
                "expected_ctr": qi.search_predicted_ctr.name,
                "campaign": row.campaign.name, "ad_group": row.ad_group.name,
            })
        return json.dumps({"keywords": results, "total": len(results)})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_call_metrics(start_date: str = "", end_date: str = "", customer_id: str = "") -> str:
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
                "campaign": row.campaign.name, "phone_calls": int(m.phone_calls),
                "phone_impressions": int(m.phone_impressions),
                "phone_through_rate": _pct(m.phone_through_rate),
            })
        return json.dumps({"date_range": f"{sd} to {ed}", "call_metrics": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_extension_performance(start_date: str = "", end_date: str = "", customer_id: str = "") -> str:
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
            ORDER BY metrics.clicks DESC LIMIT 50
        """, customer_id)
        results = []
        for row in rows:
            m = row.metrics
            results.append({
                "type": row.asset_field_type_view.field_type.name,
                "impressions": int(m.impressions), "clicks": int(m.clicks), "spend": _m(m.cost_micros),
            })
        return json.dumps({"date_range": f"{sd} to {ed}", "extensions": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_recommendations(customer_id: str = "") -> str:
    """Get Google Ads optimization recommendations for the account.

    Args:
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        rows = _search("""
            SELECT recommendation.type, recommendation.campaign, recommendation.dismissed
            FROM recommendation WHERE recommendation.dismissed = FALSE LIMIT 25
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
def google_ads_asset_report(start_date: str = "", end_date: str = "", customer_id: str = "", row_limit: int = 25) -> str:
    """Get Google Ads asset performance: clicks, impressions, performance rating for headlines/descriptions.

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
            ORDER BY metrics.impressions DESC LIMIT {row_limit}
        """, customer_id)
        results = []
        for row in rows:
            av, a, m = row.ad_group_ad_asset_view, row.asset, row.metrics
            results.append({
                "text": a.text_asset.text, "field_type": av.field_type.name,
                "performance": av.performance_label.name, "campaign": row.campaign.name,
                "impressions": int(m.impressions), "clicks": int(m.clicks),
            })
        return json.dumps({"date_range": f"{sd} to {ed}", "assets": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_list_labels(customer_id: str = "") -> str:
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
                "id": str(lbl.id), "name": lbl.name, "status": lbl.status.name,
                "color": lbl.text_label.background_color,
            })
        return json.dumps({"labels": results, "total": len(results)})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_get_asset_group_performance(start_date: str = "", end_date: str = "", customer_id: str = "", campaign_id: str = "") -> str:
    """Get performance for Performance Max asset groups.

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
            SELECT asset_group.id, asset_group.name, asset_group.status,
                   campaign.name, metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions
            FROM asset_group
            WHERE segments.date BETWEEN '{sd}' AND '{ed}' {where_campaign}
            ORDER BY metrics.cost_micros DESC LIMIT 25
        """, customer_id)
        results = []
        for row in rows:
            ag, m = row.asset_group, row.metrics
            results.append({
                "id": str(ag.id), "name": ag.name, "status": ag.status.name,
                "campaign": row.campaign.name, "spend": _m(m.cost_micros),
                "impressions": int(m.impressions), "clicks": int(m.clicks), "conversions": float(m.conversions),
            })
        return json.dumps({"date_range": f"{sd} to {ed}", "asset_groups": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Management Tools (WRITE — editor/admin only)
# ---------------------------------------------------------------------------

@mcp.tool()
def google_ads_create_budget(name: str, amount_per_day: float, delivery_method: str = "STANDARD", customer_id: str = "") -> str:
    """Create a new shared campaign budget in Google Ads.

    Args:
        name: Budget name (required).
        amount_per_day: Daily budget amount in the account's currency, e.g. 50.00 (required).
        delivery_method: STANDARD or ACCELERATED. Default STANDARD.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    if err := require_editor("google_ads_create_budget"):
        return err
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
def google_ads_create_campaign(name: str, budget_id: str, advertising_channel_type: str = "SEARCH", status: str = "PAUSED", bidding_strategy: str = "MANUAL_CPC", target_cpa_micros: int = 0, target_roas: float = 0.0, customer_id: str = "") -> str:
    """Create a new Google Ads campaign.

    Args:
        name: Campaign name (required).
        budget_id: Campaign budget resource name or ID (required).
        advertising_channel_type: SEARCH, DISPLAY, VIDEO, SHOPPING, or PERFORMANCE_MAX. Default SEARCH.
        status: ENABLED or PAUSED. Default PAUSED.
        bidding_strategy: MANUAL_CPC, TARGET_CPA, TARGET_ROAS, or MAXIMIZE_CONVERSIONS. Default MANUAL_CPC.
        target_cpa_micros: Required if bidding_strategy is TARGET_CPA (target cost-per-acquisition, in micros).
        target_roas: Required if bidding_strategy is TARGET_ROAS (target return-on-ad-spend, e.g. 3.5 = 350%).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    if err := require_editor("google_ads_create_campaign"):
        return err
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
def google_ads_create_adgroup(name: str, campaign_id: str, cpc_bid_micros: int = 1000000, status: str = "PAUSED", customer_id: str = "") -> str:
    """Create a new ad group within a Google Ads campaign.

    Args:
        name: Ad group name (required).
        campaign_id: Parent campaign ID (required).
        cpc_bid_micros: Default CPC bid in micros, e.g. 1000000 for $1.00. Default 1000000.
        status: ENABLED or PAUSED. Default PAUSED.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    if err := require_editor("google_ads_create_adgroup"):
        return err
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
def google_ads_add_keywords(adgroup_id: str, keywords_json: str, campaign_id: str, customer_id: str = "") -> str:
    """Add keywords to a Google Ads ad group.

    Args:
        adgroup_id: Ad group ID to add keywords to (required).
        keywords_json: JSON array of keyword objects, e.g. '[{"text": "running shoes", "match_type": "BROAD", "cpc_bid_micros": 800000}]' (required).
        campaign_id: Parent campaign ID (required).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    if err := require_editor("google_ads_add_keywords"):
        return err
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
def google_ads_add_negative_keywords(campaign_id: str, keywords_json: str, customer_id: str = "") -> str:
    """Add negative keywords at the campaign level.

    Args:
        campaign_id: Campaign ID to add negative keywords to (required).
        keywords_json: JSON array, e.g. '[{"text": "free", "match_type": "BROAD"}]' (required).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    if err := require_editor("google_ads_add_negative_keywords"):
        return err
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
def google_ads_update_campaign_status(campaign_id: str, status: str, customer_id: str = "") -> str:
    """Update the status of a Google Ads campaign: enable, pause, or remove it.

    Args:
        campaign_id: Campaign ID to update (required).
        status: New status: ENABLED, PAUSED, or REMOVED (required).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    if err := require_editor("google_ads_update_campaign_status"):
        return err
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
def google_ads_update_adgroup_status(adgroup_id: str, campaign_id: str, status: str, customer_id: str = "") -> str:
    """Update the status of a Google Ads ad group.

    Args:
        adgroup_id: Ad group ID to update (required).
        campaign_id: Parent campaign ID (required).
        status: New status: ENABLED, PAUSED, or REMOVED (required).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    if err := require_editor("google_ads_update_adgroup_status"):
        return err
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
def google_ads_update_keyword_status(adgroup_id: str, criterion_id: str, status: str, customer_id: str = "") -> str:
    """Update the status of a keyword in a Google Ads ad group.

    Args:
        adgroup_id: Ad group ID containing the keyword (required).
        criterion_id: Keyword criterion ID to update (required).
        status: New status: ENABLED, PAUSED, or REMOVED (required).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    if err := require_editor("google_ads_update_keyword_status"):
        return err
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
def google_ads_update_keyword_bid(adgroup_id: str, criterion_id: str, cpc_bid_micros: int, customer_id: str = "") -> str:
    """Update the CPC bid for a specific keyword in Google Ads.

    Args:
        adgroup_id: Ad group ID containing the keyword (required).
        criterion_id: Keyword criterion ID to update (required).
        cpc_bid_micros: New CPC bid in micros, e.g. 2000000 for $2.00 (required).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    if err := require_editor("google_ads_update_keyword_bid"):
        return err
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
def google_ads_update_campaign_budget(budget_id: str, amount_per_day: float, customer_id: str = "") -> str:
    """Update the daily budget amount for a Google Ads campaign budget.

    Args:
        budget_id: Campaign budget ID to update (required).
        amount_per_day: New daily budget in the account's currency, e.g. 100.00 for $100 (required).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    if err := require_editor("google_ads_update_campaign_budget"):
        return err
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
def google_ads_create_responsive_search_ad(adgroup_id: str, campaign_id: str, final_url: str, headlines: list, descriptions: list, path1: str = "", path2: str = "", status: str = "PAUSED", customer_id: str = "") -> str:
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
    if err := require_editor("google_ads_create_responsive_search_ad"):
        return err
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
def google_ads_pause_all_campaigns(customer_id: str = "") -> str:
    """Pause ALL active campaigns in the Google Ads account. Use with caution.

    Args:
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    if err := require_editor("google_ads_pause_all_campaigns"):
        return err
    try:
        rows = _search("""
            SELECT campaign.id, campaign.name FROM campaign WHERE campaign.status = 'ENABLED'
        """, customer_id)
        if not rows:
            return json.dumps({"message": "No enabled campaigns found.", "paused": 0})
        client, cid = _get_client(customer_id)
        service = client.get_service("CampaignService")
        operations = []
        for row in rows:
            op = client.get_type("CampaignOperation")
            campaign = op.update
            campaign.resource_name = f"customers/{cid}/campaigns/{row.campaign.id}"
            campaign.status = client.enums.CampaignStatusEnum.PAUSED
            op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["status"]))
            operations.append(op)
        service.mutate_campaigns(customer_id=cid, operations=operations)
        return json.dumps({"success": True, "paused_count": len(operations)})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_enable_campaign(campaign_id: str, customer_id: str = "") -> str:
    """Enable (un-pause) a specific Google Ads campaign.

    Args:
        campaign_id: Campaign ID to enable (required).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    if err := require_editor("google_ads_enable_campaign"):
        return err
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
def google_ads_apply_recommendation(recommendation_resource_name: str, customer_id: str = "") -> str:
    """Apply a Google Ads optimization recommendation.

    Args:
        recommendation_resource_name: Full resource name of the recommendation (required).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    if err := require_editor("google_ads_apply_recommendation"):
        return err
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
def google_ads_dismiss_recommendation(recommendation_resource_name: str, customer_id: str = "") -> str:
    """Dismiss a Google Ads optimization recommendation.

    Args:
        recommendation_resource_name: Full resource name of the recommendation (required).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    if err := require_editor("google_ads_dismiss_recommendation"):
        return err
    try:
        client, cid = _get_client(customer_id)
        service = client.get_service("RecommendationService")
        response = service.dismiss_recommendation(
            customer_id=cid,
            operations=[{"resource_name": recommendation_resource_name}],
        )
        return json.dumps({"success": True, "dismissed": recommendation_resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_upload_image_asset(image_url: str, asset_name: str, customer_id: str = "") -> str:
    """Upload a public image URL as a Google Ads Image Asset.

    Args:
        image_url: Public HTTPS URL of the image to upload (required).
        asset_name: Display name for the asset (required).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    if err := require_editor("google_ads_upload_image_asset"):
        return err
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
def google_ads_create_conversion_action(name: str, type: str, category: str, counting_type: str = "ONE_PER_CLICK", value: float = 0.0, currency_code: str = "USD", customer_id: str = "") -> str:
    """Create a new conversion action in Google Ads.

    Args:
        name: Conversion action name (required).
        type: Type such as WEBPAGE, PHONE_CALL, APP_INSTALL (required).
        category: Category such as PURCHASE, LEAD, SIGNUP (required).
        counting_type: ONE_PER_CLICK or MANY_PER_CLICK. Default ONE_PER_CLICK.
        value: Default conversion value. Default 0.0.
        currency_code: Currency code, e.g. USD. Default USD.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    if err := require_editor("google_ads_create_conversion_action"):
        return err
    try:
        client, cid = _get_client(customer_id)
        svc = client.get_service("ConversionActionService")
        op = client.get_type("ConversionActionOperation")
        ca = op.create
        ca.name = name
        ca.type_ = client.enums.ConversionActionTypeEnum[type]
        ca.category = client.enums.ConversionActionCategoryEnum[category]
        ca.counting_type = client.enums.ConversionActionCountingTypeEnum[counting_type]
        ca.value_settings.default_value = value
        ca.value_settings.default_currency_code = currency_code
        response = svc.mutate_conversion_actions(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_update_conversion_action(conversion_action_id: str, status: str = "", value: float = -1, counting_type: str = "", attribution_model: str = "", customer_id: str = "") -> str:
    """Update an existing conversion action in Google Ads.

    Args:
        conversion_action_id: The conversion action ID to update (required).
        status: New status: ENABLED, REMOVED, or HIDDEN. Leave blank to keep unchanged.
        value: New default conversion value. Pass -1 to keep unchanged.
        counting_type: ONE_PER_CLICK or MANY_PER_CLICK. Leave blank to keep unchanged.
        attribution_model: Attribution model name. Leave blank to keep unchanged.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    if err := require_editor("google_ads_update_conversion_action"):
        return err
    try:
        client, cid = _get_client(customer_id)
        rn = f"customers/{cid}/conversionActions/{conversion_action_id}"
        svc = client.get_service("ConversionActionService")
        op = client.get_type("ConversionActionOperation")
        ca = op.update
        ca.resource_name = rn
        paths = []
        if status:
            ca.status = client.enums.ConversionActionStatusEnum[status]
            paths.append("status")
        if value >= 0:
            ca.value_settings.default_value = value
            paths.append("value_settings.default_value")
        if counting_type:
            ca.counting_type = client.enums.ConversionActionCountingTypeEnum[counting_type]
            paths.append("counting_type")
        if not paths:
            return json.dumps({"error": "No fields to update."})
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=paths))
        svc.mutate_conversion_actions(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "conversion_action_id": conversion_action_id})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_create_sitelink_asset(link_text: str, description_1: str, description_2: str, final_url: str, campaign_id: str = "", customer_id: str = "") -> str:
    """Create a sitelink asset in Google Ads and optionally link it to a campaign.

    Args:
        link_text: Sitelink link text, max 25 chars (required).
        description_1: First description line, max 35 chars (required).
        description_2: Second description line, max 35 chars (required).
        final_url: Destination URL (required).
        campaign_id: Campaign to link the sitelink to. Leave blank for account-level.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    if err := require_editor("google_ads_create_sitelink_asset"):
        return err
    try:
        client, cid = _get_client(customer_id)
        asset_svc = client.get_service("AssetService")
        asset_op = client.get_type("AssetOperation")
        asset = asset_op.create
        sl = asset.sitelink_asset
        sl.link_text = link_text
        sl.description1 = description_1
        sl.description2 = description_2
        asset.final_urls.append(final_url)
        asset_resp = asset_svc.mutate_assets(customer_id=cid, operations=[asset_op])
        asset_rn = asset_resp.results[0].resource_name
        result = {"asset_resource_name": asset_rn}
        if campaign_id:
            caa_svc = client.get_service("CampaignAssetService")
            caa_op = client.get_type("CampaignAssetOperation")
            caa = caa_op.create
            caa.campaign = f"customers/{cid}/campaigns/{campaign_id}"
            caa.asset = asset_rn
            caa.field_type = client.enums.AssetFieldTypeEnum.SITELINK
            caa_svc.mutate_campaign_assets(customer_id=cid, operations=[caa_op])
            result["linked_to_campaign"] = campaign_id
        return json.dumps({"success": True, **result})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_create_callout_asset(callout_text: str, campaign_id: str = "", customer_id: str = "") -> str:
    """Create a callout asset and optionally link it to a specific campaign.

    Args:
        callout_text: Callout text, max 25 chars (required).
        campaign_id: Campaign to link the callout to. Leave blank for account-level.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    if err := require_editor("google_ads_create_callout_asset"):
        return err
    try:
        client, cid = _get_client(customer_id)
        asset_svc = client.get_service("AssetService")
        asset_op = client.get_type("AssetOperation")
        asset = asset_op.create
        asset.callout_asset.callout_text = callout_text
        asset_resp = asset_svc.mutate_assets(customer_id=cid, operations=[asset_op])
        asset_rn = asset_resp.results[0].resource_name
        result: dict = {"asset_resource_name": asset_rn}
        if campaign_id:
            caa_svc = client.get_service("CampaignAssetService")
            caa_op = client.get_type("CampaignAssetOperation")
            caa = caa_op.create
            caa.campaign = f"customers/{cid}/campaigns/{campaign_id}"
            caa.asset = asset_rn
            caa.field_type = client.enums.AssetFieldTypeEnum.CALLOUT
            caa_svc.mutate_campaign_assets(customer_id=cid, operations=[caa_op])
            result["linked_to_campaign"] = campaign_id
        return json.dumps({"success": True, **result})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_create_video_ad(adgroup_id: str, youtube_video_id: str, format: str, final_url: str, headline: str = "", description: str = "", companion_banner_asset_id: str = "", customer_id: str = "") -> str:
    """Create a video ad in an existing ad group linked to a YouTube video.

    Args:
        adgroup_id: The ad group ID to create the video ad in (required).
        youtube_video_id: YouTube video ID, e.g. 'dQw4w9WgXcQ' (required).
        format: Video ad format: IN_STREAM, BUMPER, DISCOVERY, OUT_STREAM (required).
        final_url: Landing page URL (required for IN_STREAM).
        headline: Ad headline for IN_STREAM / DISCOVERY ads.
        description: Ad description for DISCOVERY ads.
        companion_banner_asset_id: Asset ID of the companion banner for IN_STREAM.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    if err := require_editor("google_ads_create_video_ad"):
        return err
    try:
        client, cid = _get_client(customer_id)
        adgroup_rn = f"customers/{cid}/adGroups/{adgroup_id}"
        asset_svc = client.get_service("AssetService")
        aga_svc = client.get_service("AdGroupAdService")
        op = client.get_type("AdGroupAdOperation")
        ad_group_ad = op.create
        ad_group_ad.ad_group = adgroup_rn
        ad_group_ad.status = client.enums.AdGroupAdStatusEnum.ENABLED
        if final_url:
            ad_group_ad.ad.final_urls.append(final_url)
        video_asset_op = client.get_type("AssetOperation")
        video_asset = video_asset_op.create
        video_asset.youtube_video_asset.youtube_video_id = youtube_video_id
        video_resp = asset_svc.mutate_assets(customer_id=cid, operations=[video_asset_op])
        video_rn = video_resp.results[0].resource_name
        if format == "IN_STREAM":
            v = ad_group_ad.ad.video_true_view_in_stream_ad
            v.action_headline = headline
            v.in_stream_ad_info.video_asset = client.get_type("AdVideoAsset")
            v.in_stream_ad_info.video_asset.asset = video_rn
        elif format == "BUMPER":
            v = ad_group_ad.ad.video_bumper_ad
            v.companion_banner.asset = companion_banner_asset_id if companion_banner_asset_id else ""
        elif format == "DISCOVERY":
            v = ad_group_ad.ad.video_responsive_ad
            ht = client.get_type("AdTextAsset")
            ht.text = headline
            v.headlines.append(ht)
            dt = client.get_type("AdTextAsset")
            dt.text = description
            v.descriptions.append(dt)
            va = client.get_type("AdVideoAsset")
            va.asset = video_rn
            v.videos.append(va)
        resp = aga_svc.mutate_ad_group_ads(customer_id=cid, operations=[op])
        rn = resp.results[0].resource_name
        return json.dumps({"success": True, "ad_resource_name": rn})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_bulk_create_keywords(keywords_json: str, customer_id: str = "") -> str:
    """Bulk create keywords across multiple ad groups in a single API call.

    Args:
        keywords_json: JSON array of objects with adgroup_id, text, match_type, and optional cpc_bid_micros.
            Example: '[{"adgroup_id": "123", "text": "shoes", "match_type": "BROAD", "cpc_bid_micros": 500000}]' (required).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    if err := require_editor("google_ads_bulk_create_keywords"):
        return err
    try:
        keywords = json.loads(keywords_json)
        client, cid = _get_client(customer_id)
        service = client.get_service("AdGroupCriterionService")
        operations = []
        for kw in keywords:
            operation = client.get_type("AdGroupCriterionOperation")
            criterion = operation.create
            criterion.ad_group = f"customers/{cid}/adGroups/{kw['adgroup_id']}"
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
def google_ads_create_label(name: str, color: str = "#000000", description: str = "", customer_id: str = "") -> str:
    """Create a new label in Google Ads for organizing campaigns, ad groups, ads or keywords.

    Args:
        name: Label name (required).
        color: Background color as hex code, e.g. '#FF0000'. Default #000000.
        description: Optional label description.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    if err := require_editor("google_ads_create_label"):
        return err
    try:
        client, cid = _get_client(customer_id)
        svc = client.get_service("LabelService")
        op = client.get_type("LabelOperation")
        label = op.create
        label.name = name
        label.text_label.background_color = color
        if description:
            label.text_label.description = description
        response = svc.mutate_labels(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "resource_name": response.results[0].resource_name})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_apply_label(label_id: str, entity_type: str, entity_id: str, customer_id: str = "") -> str:
    """Apply an existing label to a campaign, ad group, ad, or keyword.

    Args:
        label_id: The label ID to apply (required).
        entity_type: One of 'campaign', 'ad_group', 'ad', or 'keyword' (required).
        entity_id: The ID of the entity to label (required).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    if err := require_editor("google_ads_apply_label"):
        return err
    try:
        client, cid = _get_client(customer_id)
        label_rn = f"customers/{cid}/labels/{label_id}"
        type_map = {
            "campaign": ("CampaignLabelService", "CampaignLabelOperation", f"customers/{cid}/campaigns/{entity_id}"),
            "ad_group": ("AdGroupLabelService", "AdGroupLabelOperation", f"customers/{cid}/adGroups/{entity_id}"),
            "ad": ("AdGroupAdLabelService", "AdGroupAdLabelOperation", entity_id),
            "keyword": ("AdGroupCriterionLabelService", "AdGroupCriterionLabelOperation", entity_id),
        }
        if entity_type not in type_map:
            return json.dumps({"error": f"entity_type must be one of: {list(type_map.keys())}"})
        svc_name, op_name, entity_rn = type_map[entity_type]
        svc = client.get_service(svc_name)
        op = client.get_type(op_name)
        label_link = op.create
        label_link.label = label_rn
        if entity_type == "campaign":
            label_link.campaign = entity_rn
            svc.mutate_campaign_labels(customer_id=cid, operations=[op])
        elif entity_type == "ad_group":
            label_link.ad_group = entity_rn
            svc.mutate_ad_group_labels(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "label_id": label_id, "entity": entity_id})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_create_responsive_display_ad(adgroup_id: str, headlines: str, descriptions: str, business_name: str, final_url: str, marketing_image_asset_id: str, logo_asset_id: str = "", customer_id: str = "") -> str:
    """Create a Responsive Display Ad in an existing ad group.

    Args:
        adgroup_id: The ad group ID (required).
        headlines: JSON array of headline strings, e.g. '["Headline 1", "Headline 2"]' (required).
        descriptions: JSON array of description strings (required).
        business_name: Business name shown in the ad, max 25 chars (required).
        final_url: Landing page URL (required).
        marketing_image_asset_id: Image asset ID from google_ads_upload_image_asset (required).
        logo_asset_id: Optional logo asset ID.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    if err := require_editor("google_ads_create_responsive_display_ad"):
        return err
    try:
        headlines_list = json.loads(headlines)
        descriptions_list = json.loads(descriptions)
        client, cid = _get_client(customer_id)
        aga_svc = client.get_service("AdGroupAdService")
        asset_svc = client.get_service("AssetService")
        ad_op = client.get_type("AdGroupAdOperation")
        ad_group_ad = ad_op.create
        ad_group_ad.ad_group = f"customers/{cid}/adGroups/{adgroup_id}"
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
def google_ads_create_asset_group(campaign_id: str, name: str, final_url: str, business_name: str, headlines: str, long_headlines: str, descriptions: str, image_asset_ids: str, logo_asset_ids: str = "[]", youtube_video_ids: str = "[]", customer_id: str = "") -> str:
    """Create an Asset Group inside an existing Performance Max campaign.

    Args:
        campaign_id: The PMax campaign ID (required).
        name: Asset group name (required).
        final_url: Landing page URL (required).
        business_name: Business name, max 25 chars (required).
        headlines: JSON array of headlines, min 3, max 15, each max 30 chars (required).
        long_headlines: JSON array of long headlines, min 1, max 5, each max 90 chars (required).
        descriptions: JSON array of descriptions, min 2, max 4, each max 90 chars (required).
        image_asset_ids: JSON array of image asset IDs (required).
        logo_asset_ids: JSON array of logo asset IDs.
        youtube_video_ids: JSON array of YouTube video IDs.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    if err := require_editor("google_ads_create_asset_group"):
        return err
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

        for h in h_list[:15]:
            _link(_text_asset(h), aft.HEADLINE)
        for lh in lh_list[:5]:
            _link(_text_asset(lh), aft.LONG_HEADLINE)
        for d in d_list[:4]:
            _link(_text_asset(d), aft.DESCRIPTION)
        _link(_text_asset(business_name), aft.BUSINESS_NAME)
        for img_id in img_ids:
            _link(asset_svc.asset_path(cid, img_id), aft.MARKETING_IMAGE)
        for logo_id in logo_ids:
            _link(asset_svc.asset_path(cid, logo_id), aft.LOGO)
        for vid_id in vid_ids:
            vid_asset_op = client.get_type("AssetOperation")
            vid_asset_op.create.youtube_video_asset.youtube_video_id = vid_id
            var = asset_svc.mutate_assets(customer_id=cid, operations=[vid_asset_op])
            _link(var.results[0].resource_name, aft.YOUTUBE_VIDEO)
        if aga_ops:
            aga_svc.mutate_asset_group_assets(customer_id=cid, operations=aga_ops)
        return json.dumps({"success": True, "asset_group_resource_name": ag_rn})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})
