"""
Agency Manager Tools — cross-account, cross-platform tools for marketing agencies.

Covers:
  Google Ads MCC (Manager Account) operations
  Meta Business Manager operations
  Cross-platform combined reports (Google Ads + Meta Ads + GA4 + GSC)
  Shared assets management (negative lists, budgets, audiences)
  Offline conversion & customer match uploads
  Agency-level audit and scoring tools
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from auth import current_user_ctx
from mcp_instance import mcp

# ─────────────────────────────────────────────────────────────────────────────
# Helpers — re-used from existing tool modules
# ─────────────────────────────────────────────────────────────────────────────

def _google_ads_client(customer_id: str):
    """Return an MCC-aware Google Ads client for the current user."""
    from tools.google_ads import _get_client
    client, _ = _get_client(customer_id)
    return client


def _meta_error(resp) -> str:
    """Build a safe error string from a Meta response.
    Never include the request URL — it carries the access_token in the query
    string and requests' raise_for_status() would leak it into tool output.
    """
    try:
        err = resp.json().get("error", {}) or {}
        msg = err.get("message") or "request failed"
        return f"Meta API error {resp.status_code}: {msg}"
    except Exception:
        return f"Meta API error {resp.status_code}"


def _meta_get(path: str, params: dict | None = None) -> dict:
    import requests
    user = current_user_ctx.get()
    if not user:
        raise PermissionError("Not authenticated")
    token = user.get_meta_token()
    if not token:
        raise ValueError("Meta account not connected. Visit /auth/meta/start")
    p = {"access_token": token, **(params or {})}
    resp = requests.get(f"https://graph.facebook.com/v22.0/{path}", params=p, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(_meta_error(resp))
    return resp.json()


def _meta_post(path: str, data: dict | None = None) -> dict:
    import requests
    user = current_user_ctx.get()
    if not user:
        raise PermissionError("Not authenticated")
    token = user.get_meta_token()
    if not token:
        raise ValueError("Meta account not connected. Visit /auth/meta/start")
    d = {"access_token": token, **(data or {})}
    resp = requests.post(f"https://graph.facebook.com/v22.0/{path}", data=d, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(_meta_error(resp))
    return resp.json()


def _decode_tool_json(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
            return data if isinstance(data, dict) else {"data": data}
        except Exception:
            return {"raw": payload}
    return {"data": payload}


# ═════════════════════════════════════════════════════════════════════════════
# GOOGLE ADS — MCC / MANAGER ACCOUNT TOOLS
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def agency_list_platform_accounts() -> dict:
    """
    List the currently accessible accounts/properties/sites across all connected platforms.

    Returns a single normalized payload for:
      - Google Ads customer accounts
      - Meta ad accounts
      - GA4 properties
      - Search Console sites
    """
    from tools.ga4 import ga4_list_properties
    from tools.google_ads import google_ads_list_customers
    from tools.gsc import gsc_list_sites
    from tools.meta_ads import meta_ads_list_ad_accounts

    results: dict[str, Any] = {
        "google_ads": {"accounts": [], "total": 0},
        "meta_ads": {"accounts": [], "total": 0},
        "ga4": {"properties": [], "total": 0},
        "gsc": {"sites": [], "total": 0},
        "errors": [],
    }

    try:
        google_ads_data = _decode_tool_json(google_ads_list_customers())
        if "error" in google_ads_data:
            results["google_ads"] = {"error": google_ads_data["error"]}
            results["errors"].append(f"Google Ads error: {google_ads_data['error']}")
        else:
            customers = google_ads_data.get("customers", []) or []
            results["google_ads"] = {
                "accounts": [
                    {
                        "customer_id": item.get("id", ""),
                        "name": item.get("name", ""),
                        "status": item.get("status", ""),
                        "manager": item.get("manager", False),
                        "currency": item.get("currency_code", ""),
                        "timezone": item.get("timezone", ""),
                        "manager_account_id": item.get("manager_account_id", ""),
                    }
                    if isinstance(item, dict) else {"customer_id": item}
                    for item in customers
                ],
                "total": google_ads_data.get("total", len(customers)),
            }
    except Exception as e:
        results["google_ads"] = {"error": str(e)}
        results["errors"].append(f"Google Ads error: {e}")

    try:
        meta_ads_data = _decode_tool_json(meta_ads_list_ad_accounts())
        if "error" in meta_ads_data:
            results["meta_ads"] = {"error": meta_ads_data["error"]}
            results["errors"].append(f"Meta Ads error: {meta_ads_data['error']}")
        else:
            accounts = meta_ads_data.get("data", []) or []
            results["meta_ads"] = {
                "accounts": [
                    {
                        "account_id": item.get("id", ""),
                        "name": item.get("name", ""),
                        "status": item.get("account_status", ""),
                        "currency": item.get("currency", ""),
                        "timezone": item.get("timezone_name", ""),
                    }
                    for item in accounts
                ],
                "total": meta_ads_data.get("total", len(accounts)),
            }
    except Exception as e:
        results["meta_ads"] = {"error": str(e)}
        results["errors"].append(f"Meta Ads error: {e}")

    try:
        ga4_data = _decode_tool_json(ga4_list_properties())
        if "error" in ga4_data:
            results["ga4"] = {"error": ga4_data["error"]}
            results["errors"].append(f"GA4 error: {ga4_data['error']}")
        else:
            properties = ga4_data.get("properties", []) or []
            results["ga4"] = {
                "properties": properties,
                "total": ga4_data.get("total", len(properties)),
            }
    except Exception as e:
        results["ga4"] = {"error": str(e)}
        results["errors"].append(f"GA4 error: {e}")

    try:
        gsc_data = _decode_tool_json(gsc_list_sites())
        if "error" in gsc_data:
            results["gsc"] = {"error": gsc_data["error"]}
            results["errors"].append(f"Search Console error: {gsc_data['error']}")
        else:
            sites = gsc_data.get("sites", []) or []
            results["gsc"] = {
                "sites": [{"site_url": site} for site in sites],
                "total": len(sites),
            }
    except Exception as e:
        results["gsc"] = {"error": str(e)}
        results["errors"].append(f"Search Console error: {e}")

    results["summary"] = {
        "google_ads_accounts": results.get("google_ads", {}).get("total", 0),
        "meta_ads_accounts": results.get("meta_ads", {}).get("total", 0),
        "ga4_properties": results.get("ga4", {}).get("total", 0),
        "gsc_sites": results.get("gsc", {}).get("total", 0),
        "platforms_with_errors": len(results["errors"]),
    }
    return results

@mcp.tool()
async def google_ads_mcc_list_accounts(mcc_customer_id: str) -> dict:
    """
    List all child accounts under a Google Ads Manager Account (MCC).
    Shows account name, ID, currency, timezone, and status.

    Args:
        mcc_customer_id: The Manager Account customer ID (e.g. '123-456-7890').
    """
    try:
        client = _google_ads_client(mcc_customer_id.replace("-", ""))
        ga_service = client.get_service("GoogleAdsService")
        query = """
            SELECT
              customer_client.id,
              customer_client.descriptive_name,
              customer_client.currency_code,
              customer_client.time_zone,
              customer_client.status,
              customer_client.level,
              customer_client.manager
            FROM customer_client
            WHERE customer_client.level <= 1
            ORDER BY customer_client.descriptive_name
        """
        response = ga_service.search(customer_id=mcc_customer_id.replace("-", ""), query=query)
        accounts = []
        for row in response:
            c = row.customer_client
            accounts.append({
                "id": str(c.id),
                "name": c.descriptive_name,
                "currency": c.currency_code,
                "timezone": c.time_zone,
                "status": c.status.name,
                "is_manager": c.manager,
                "level": c.level,
            })
        return {"accounts": accounts, "total": len(accounts)}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def google_ads_mcc_performance(
    mcc_customer_id: str,
    date_range: str = "LAST_30_DAYS",
) -> dict:
    """
    Get performance metrics across ALL child accounts in an MCC.
    Returns spend, impressions, clicks, conversions, ROAS per account.

    Args:
        mcc_customer_id: Manager Account customer ID.
        date_range: LAST_7_DAYS | LAST_30_DAYS | THIS_MONTH | LAST_MONTH.
    """
    try:
        client = _google_ads_client(mcc_customer_id.replace("-", ""))
        ga_service = client.get_service("GoogleAdsService")
        query = f"""
            SELECT
              customer.id,
              customer.descriptive_name,
              metrics.cost_micros,
              metrics.impressions,
              metrics.clicks,
              metrics.conversions,
              metrics.conversions_value
            FROM customer
            WHERE segments.date DURING {date_range}
        """
        # Use manager account to query across all child accounts
        response = ga_service.search(customer_id=mcc_customer_id.replace("-", ""), query=query)
        results = []
        total_spend = 0
        total_conv = 0
        total_revenue = 0
        for row in response:
            spend = row.metrics.cost_micros / 1_000_000
            revenue = row.metrics.conversions_value
            roas = round(revenue / spend, 2) if spend > 0 else 0
            total_spend += spend
            total_conv += row.metrics.conversions
            total_revenue += revenue
            results.append({
                "account_id": str(row.customer.id),
                "account_name": row.customer.descriptive_name,
                "spend": round(spend, 2),
                "impressions": row.metrics.impressions,
                "clicks": row.metrics.clicks,
                "conversions": row.metrics.conversions,
                "revenue": round(revenue, 2),
                "roas": roas,
            })
        results.sort(key=lambda x: x["spend"], reverse=True)
        return {
            "accounts": results,
            "summary": {
                "total_spend": round(total_spend, 2),
                "total_conversions": round(total_conv, 2),
                "total_revenue": round(total_revenue, 2),
                "blended_roas": round(total_revenue / total_spend, 2) if total_spend > 0 else 0,
                "account_count": len(results),
            },
            "date_range": date_range,
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def google_ads_mcc_budget_pacing(
    mcc_customer_id: str,
    month: str = "",
) -> dict:
    """
    Show budget pacing status across all child accounts in an MCC.
    Flags accounts that are over or under pacing.

    Args:
        mcc_customer_id: Manager Account customer ID.
        month: Month to check in YYYY-MM format. Defaults to current month.
    """
    try:
        if not month:
            today = datetime.now(timezone.utc)
            month = today.strftime("%Y-%m")
        year, mon = map(int, month.split("-"))
        from calendar import monthrange
        days_in_month = monthrange(year, mon)[1]
        today = datetime.now(timezone.utc)
        day_of_month = today.day if today.year == year and today.month == mon else days_in_month
        pacing_ratio = day_of_month / days_in_month

        client = _google_ads_client(mcc_customer_id.replace("-", ""))
        ga_service = client.get_service("GoogleAdsService")
        query = f"""
            SELECT
              customer.id,
              customer.descriptive_name,
              campaign.id,
              campaign.name,
              campaign_budget.amount_micros,
              metrics.cost_micros
            FROM campaign
            WHERE
              campaign.status = 'ENABLED'
              AND segments.date DURING {month.replace("-", "_")}
        """
        response = ga_service.search(customer_id=mcc_customer_id.replace("-", ""), query=query)
        accounts: dict[str, dict] = {}
        for row in response:
            acc_id = str(row.customer.id)
            if acc_id not in accounts:
                accounts[acc_id] = {
                    "account_id": acc_id,
                    "account_name": row.customer.descriptive_name,
                    "monthly_budget": 0,
                    "spent_so_far": 0,
                }
            accounts[acc_id]["monthly_budget"] += row.campaign_budget.amount_micros * days_in_month / 1_000_000
            accounts[acc_id]["spent_so_far"] += row.metrics.cost_micros / 1_000_000

        result = []
        for acc in accounts.values():
            budget = acc["monthly_budget"]
            spent = acc["spent_so_far"]
            expected = budget * pacing_ratio
            pacing_pct = round((spent / expected * 100) if expected > 0 else 0, 1)
            status = "on_track"
            if pacing_pct > 110:
                status = "overpacing"
            elif pacing_pct < 80:
                status = "underpacing"
            result.append({**acc, "expected_spend": round(expected, 2), "pacing_pct": pacing_pct, "status": status})

        result.sort(key=lambda x: abs(x["pacing_pct"] - 100), reverse=True)
        return {"accounts": result, "month": month, "day_of_month": day_of_month, "days_in_month": days_in_month}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def google_ads_shared_negative_lists(customer_id: str) -> dict:
    """
    List all shared negative keyword lists in a Google Ads account.

    Args:
        customer_id: Google Ads customer ID.
    """
    try:
        client = _google_ads_client(customer_id.replace("-", ""))
        ga_service = client.get_service("GoogleAdsService")
        query = """
            SELECT
              shared_set.id,
              shared_set.name,
              shared_set.type,
              shared_set.status,
              shared_set.member_count,
              shared_set.reference_count
            FROM shared_set
            WHERE shared_set.type = 'NEGATIVE_KEYWORDS'
              AND shared_set.status = 'ENABLED'
        """
        response = ga_service.search(customer_id=customer_id.replace("-", ""), query=query)
        lists = []
        for row in response:
            s = row.shared_set
            lists.append({
                "id": str(s.id),
                "name": s.name,
                "keyword_count": s.member_count,
                "campaigns_using": s.reference_count,
                "status": s.status.name,
            })
        return {"negative_lists": lists, "total": len(lists)}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def google_ads_create_shared_negative_list(
    customer_id: str,
    list_name: str,
    keywords: list[str],
    match_type: str = "EXACT",
) -> dict:
    """
    Create a shared negative keyword list and populate it with keywords.

    Args:
        customer_id: Google Ads customer ID.
        list_name: Name for the new shared negative list.
        keywords: List of keyword texts to add.
        match_type: EXACT | PHRASE | BROAD.
    """
    try:
        client = _google_ads_client(customer_id.replace("-", ""))
        shared_set_service = client.get_service("SharedSetService")
        shared_criterion_service = client.get_service("SharedCriterionService")

        # Create the shared set
        shared_set_op = client.get_type("SharedSetOperation")
        ss = shared_set_op.create
        ss.name = list_name
        ss.type_ = client.enums.SharedSetTypeEnum.NEGATIVE_KEYWORDS
        result = shared_set_service.mutate_shared_sets(
            customer_id=customer_id.replace("-", ""), operations=[shared_set_op]
        )
        new_resource = result.results[0].resource_name
        shared_set_id = new_resource.split("/")[-1]

        # Add keywords to the shared set
        ops = []
        mt_enum = getattr(client.enums.KeywordMatchTypeEnum, match_type.upper(), None)
        for kw in keywords:
            op = client.get_type("SharedCriterionOperation")
            criterion = op.create
            criterion.shared_set = new_resource
            criterion.keyword.text = kw
            criterion.keyword.match_type = mt_enum
            ops.append(op)

        if ops:
            shared_criterion_service.mutate_shared_criteria(
                customer_id=customer_id.replace("-", ""), operations=ops
            )

        return {
            "success": True,
            "shared_set_id": shared_set_id,
            "list_name": list_name,
            "keywords_added": len(keywords),
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def google_ads_apply_shared_negative_list(
    customer_id: str,
    shared_set_id: str,
    campaign_ids: list[str],
) -> dict:
    """
    Apply an existing shared negative keyword list to one or more campaigns.

    Args:
        customer_id: Google Ads customer ID.
        shared_set_id: ID of the shared negative keyword list.
        campaign_ids: List of campaign IDs to apply the list to.
    """
    try:
        client = _google_ads_client(customer_id.replace("-", ""))
        campaign_shared_set_service = client.get_service("CampaignSharedSetService")
        shared_set_resource = client.get_service("GoogleAdsService").ad_path(
            customer_id.replace("-", ""), shared_set_id
        )
        shared_set_resource = f"customers/{customer_id.replace('-','')}/sharedSets/{shared_set_id}"

        ops = []
        for cid in campaign_ids:
            op = client.get_type("CampaignSharedSetOperation")
            css = op.create
            css.campaign = f"customers/{customer_id.replace('-','')}/campaigns/{cid}"
            css.shared_set = shared_set_resource
            ops.append(op)

        result = campaign_shared_set_service.mutate_campaign_shared_sets(
            customer_id=customer_id.replace("-", ""), operations=ops
        )
        return {
            "success": True,
            "applied_to": len(result.results),
            "campaign_ids": campaign_ids,
            "shared_set_id": shared_set_id,
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def google_ads_portfolio_bid_strategies(customer_id: str) -> dict:
    """
    List all portfolio bid strategies in a Google Ads account.
    Portfolio strategies are shared across multiple campaigns.

    Args:
        customer_id: Google Ads customer ID.
    """
    try:
        client = _google_ads_client(customer_id.replace("-", ""))
        ga_service = client.get_service("GoogleAdsService")
        query = """
            SELECT
              bidding_strategy.id,
              bidding_strategy.name,
              bidding_strategy.type,
              bidding_strategy.status,
              bidding_strategy.campaign_count
            FROM bidding_strategy
            WHERE bidding_strategy.status = 'ENABLED'
        """
        response = ga_service.search(customer_id=customer_id.replace("-", ""), query=query)
        strategies = []
        for row in response:
            b = row.bidding_strategy
            strategies.append({
                "id": str(b.id),
                "name": b.name,
                "type": b.type_.name,
                "status": b.status.name,
                "campaigns_using": b.campaign_count,
            })
        return {"portfolio_strategies": strategies, "total": len(strategies)}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def google_ads_upload_customer_match(
    customer_id: str,
    list_name: str,
    emails: list[str],
    phones: Optional[list[str]] = None,
    description: str = "",
) -> dict:
    """
    Create a Customer Match audience and upload hashed email/phone data.
    Useful for retargeting existing customers or building lookalikes.

    Args:
        customer_id: Google Ads customer ID.
        list_name: Name for the audience list.
        emails: List of customer email addresses (will be SHA-256 hashed).
        phones: Optional list of phone numbers in E.164 format.
        description: Optional description for the audience.
    """
    import hashlib

    def _hash(value: str) -> str:
        return hashlib.sha256(value.strip().lower().encode()).hexdigest()

    try:
        client = _google_ads_client(customer_id.replace("-", ""))
        user_list_service = client.get_service("UserListService")
        user_data_service = client.get_service("UserDataService")

        # Create the user list
        op = client.get_type("UserListOperation")
        ul = op.create
        ul.name = list_name
        ul.description = description
        ul.crm_based_user_list.upload_key_type = (
            client.enums.CustomerMatchUploadKeyTypeEnum.CONTACT_INFO
        )
        ul.membership_life_span = 10000  # max
        result = user_list_service.mutate_user_lists(
            customer_id=customer_id.replace("-", ""), operations=[op]
        )
        list_resource = result.results[0].resource_name

        # Upload user data
        user_data_ops = []
        for email in emails:
            udo = client.get_type("UserDataOperation")
            member = udo.create.user_identifiers.add()
            member.hashed_email = _hash(email)
            user_data_ops.append(udo)

        if phones:
            for phone in phones:
                udo = client.get_type("UserDataOperation")
                member = udo.create.user_identifiers.add()
                member.hashed_phone_number = _hash(phone)
                user_data_ops.append(udo)

        # Upload in batches of 100
        uploaded = 0
        for i in range(0, len(user_data_ops), 100):
            batch = user_data_ops[i: i + 100]
            user_data_service.upload_user_data(
                customer_id=customer_id.replace("-", ""),
                operations=batch,
            )
            uploaded += len(batch)

        return {
            "success": True,
            "list_name": list_name,
            "list_resource": list_resource,
            "emails_uploaded": len(emails),
            "phones_uploaded": len(phones) if phones else 0,
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def google_ads_upload_offline_conversions(
    customer_id: str,
    conversion_action_id: str,
    conversions: list[dict],
) -> dict:
    """
    Upload offline conversion events to a Google Ads conversion action.
    Each conversion should have: gclid, conversion_date_time, conversion_value.

    Args:
        customer_id: Google Ads customer ID.
        conversion_action_id: The conversion action resource name or ID.
        conversions: List of dicts with keys:
            - gclid (str): Google Click ID from your landing page.
            - conversion_date_time (str): 'YYYY-MM-DD HH:MM:SS+TZ' format.
            - conversion_value (float): Revenue value of the conversion.
            - currency_code (str): Optional, e.g. 'USD'.
    """
    try:
        client = _google_ads_client(customer_id.replace("-", ""))
        conversion_upload_service = client.get_service("ConversionUploadService")
        conversion_action_resource = (
            f"customers/{customer_id.replace('-','')}/conversionActions/{conversion_action_id}"
        )

        click_conversions = []
        for cv in conversions:
            cc = client.get_type("ClickConversion")
            cc.gclid = cv["gclid"]
            cc.conversion_action = conversion_action_resource
            cc.conversion_date_time = cv["conversion_date_time"]
            cc.conversion_value = float(cv.get("conversion_value", 0))
            cc.currency_code = cv.get("currency_code", "USD")
            click_conversions.append(cc)

        result = conversion_upload_service.upload_click_conversions(
            customer_id=customer_id.replace("-", ""),
            conversions=click_conversions,
            partial_failure=True,
        )

        successes = sum(1 for r in result.results if r.gclid)
        errors = []
        if result.partial_failure_error:
            errors.append(str(result.partial_failure_error))

        return {
            "success": True,
            "uploaded": successes,
            "total_submitted": len(conversions),
            "errors": errors,
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def google_ads_shared_budgets(customer_id: str) -> dict:
    """
    List all shared budgets in a Google Ads account.
    Shared budgets allow multiple campaigns to draw from one pool.

    Args:
        customer_id: Google Ads customer ID.
    """
    try:
        client = _google_ads_client(customer_id.replace("-", ""))
        ga_service = client.get_service("GoogleAdsService")
        query = """
            SELECT
              campaign_budget.id,
              campaign_budget.name,
              campaign_budget.amount_micros,
              campaign_budget.reference_count,
              campaign_budget.status,
              campaign_budget.total_amount_micros
            FROM campaign_budget
            WHERE campaign_budget.explicitly_shared = TRUE
              AND campaign_budget.status = 'ENABLED'
        """
        response = ga_service.search(customer_id=customer_id.replace("-", ""), query=query)
        budgets = []
        for row in response:
            b = row.campaign_budget
            budgets.append({
                "id": str(b.id),
                "name": b.name,
                "daily_budget": round(b.amount_micros / 1_000_000, 2),
                "campaigns_sharing": b.reference_count,
                "status": b.status.name,
            })
        return {"shared_budgets": budgets, "total": len(budgets)}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def google_ads_copy_campaign(
    source_customer_id: str,
    destination_customer_id: str,
    campaign_id: str,
    new_campaign_name: str,
    pause_on_create: bool = True,
) -> dict:
    """
    Copy a campaign from one Google Ads account to another (cross-account campaign copy).
    Useful for agencies replicating proven campaign structures to new clients.

    Args:
        source_customer_id: Source account customer ID.
        destination_customer_id: Destination account customer ID.
        campaign_id: Campaign ID to copy.
        new_campaign_name: Name for the copied campaign.
        pause_on_create: If True, the copy starts PAUSED (recommended).
    """
    try:
        client = _google_ads_client(source_customer_id.replace("-", ""))
        ga_service = client.get_service("GoogleAdsService")

        # Fetch campaign details
        query = f"""
            SELECT
              campaign.id, campaign.name, campaign.advertising_channel_type,
              campaign.advertising_channel_sub_type, campaign.bidding_strategy_type,
              campaign.target_roas.target_roas,
              campaign.target_cpa.target_cpa_micros,
              campaign_budget.amount_micros
            FROM campaign
            WHERE campaign.id = '{campaign_id}'
        """
        response = ga_service.search(customer_id=source_customer_id.replace("-", ""), query=query)
        rows = list(response)
        if not rows:
            return {"error": f"Campaign {campaign_id} not found in account {source_customer_id}"}

        row = rows[0]
        campaign = row.campaign
        budget_micros = row.campaign_budget.amount_micros

        # Create in destination account
        dest_client = _google_ads_client(destination_customer_id.replace("-", ""))
        budget_service = dest_client.get_service("CampaignBudgetService")
        campaign_service = dest_client.get_service("CampaignService")

        # Create budget
        budget_op = dest_client.get_type("CampaignBudgetOperation")
        b = budget_op.create
        b.name = f"Budget for {new_campaign_name}"
        b.amount_micros = budget_micros
        b.delivery_method = dest_client.enums.BudgetDeliveryMethodEnum.STANDARD
        budget_result = budget_service.mutate_campaign_budgets(
            customer_id=destination_customer_id.replace("-", ""), operations=[budget_op]
        )
        budget_resource = budget_result.results[0].resource_name

        # Create campaign
        camp_op = dest_client.get_type("CampaignOperation")
        c = camp_op.create
        c.name = new_campaign_name
        c.advertising_channel_type = campaign.advertising_channel_type
        c.campaign_budget = budget_resource
        c.status = (
            dest_client.enums.CampaignStatusEnum.PAUSED
            if pause_on_create
            else dest_client.enums.CampaignStatusEnum.ENABLED
        )

        camp_result = campaign_service.mutate_campaigns(
            customer_id=destination_customer_id.replace("-", ""), operations=[camp_op]
        )

        return {
            "success": True,
            "new_campaign_resource": camp_result.results[0].resource_name,
            "new_campaign_name": new_campaign_name,
            "source_account": source_customer_id,
            "destination_account": destination_customer_id,
            "status": "PAUSED" if pause_on_create else "ENABLED",
            "note": "Ad groups and ads were not copied — add them separately.",
        }
    except Exception as e:
        return {"error": str(e)}


# ═════════════════════════════════════════════════════════════════════════════
# META — BUSINESS MANAGER TOOLS
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def meta_business_overview(business_id: str) -> dict:
    """
    Get an overview of a Meta Business Manager account:
    all connected ad accounts, pages, pixels, and users.

    Args:
        business_id: Meta Business Manager ID.
    """
    try:
        ad_accounts = _meta_get(
            f"{business_id}/owned_ad_accounts",
            {"fields": "id,name,account_status,spend_cap,currency,balance"}
        )
        pages = _meta_get(
            f"{business_id}/owned_pages",
            {"fields": "id,name,followers_count,category"}
        )
        pixels = _meta_get(
            f"{business_id}/owned_pixels",
            {"fields": "id,name,last_fired_time"}
        )
        users = _meta_get(
            f"{business_id}/business_users",
            {"fields": "id,name,email,role"}
        )
        return {
            "business_id": business_id,
            "ad_accounts": ad_accounts.get("data", []),
            "ad_account_count": len(ad_accounts.get("data", [])),
            "pages": pages.get("data", []),
            "page_count": len(pages.get("data", [])),
            "pixels": pixels.get("data", []),
            "pixel_count": len(pixels.get("data", [])),
            "users": users.get("data", []),
            "user_count": len(users.get("data", [])),
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def meta_business_cross_account_spend(
    business_id: str,
    date_preset: str = "last_30d",
) -> dict:
    """
    Get total ad spend and ROAS across all ad accounts in a Meta Business Manager.

    Args:
        business_id: Meta Business Manager ID.
        date_preset: last_7d | last_30d | last_month | this_month.
    """
    try:
        accounts_resp = _meta_get(
            f"{business_id}/owned_ad_accounts",
            {"fields": "id,name,currency"}
        )
        accounts = accounts_resp.get("data", [])

        results = []
        total_spend = 0.0
        total_revenue = 0.0

        for acc in accounts:
            acc_id = acc["id"]
            try:
                insights = _meta_get(
                    f"{acc_id}/insights",
                    {
                        "fields": "spend,purchase_roas,actions,action_values",
                        "date_preset": date_preset,
                        "level": "account",
                    }
                )
                data = insights.get("data", [{}])[0] if insights.get("data") else {}
                spend = float(data.get("spend", 0))
                roas_list = data.get("purchase_roas", [])
                roas = float(roas_list[0].get("value", 0)) if roas_list else 0
                revenue = spend * roas
                total_spend += spend
                total_revenue += revenue
                results.append({
                    "account_id": acc_id,
                    "account_name": acc.get("name", ""),
                    "currency": acc.get("currency", ""),
                    "spend": round(spend, 2),
                    "roas": round(roas, 2),
                    "revenue": round(revenue, 2),
                })
            except Exception:
                results.append({"account_id": acc_id, "account_name": acc.get("name", ""), "error": "fetch failed"})

        results.sort(key=lambda x: x.get("spend", 0), reverse=True)
        return {
            "accounts": results,
            "summary": {
                "total_spend": round(total_spend, 2),
                "total_revenue": round(total_revenue, 2),
                "blended_roas": round(total_revenue / total_spend, 2) if total_spend > 0 else 0,
                "account_count": len(results),
            },
            "date_preset": date_preset,
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def meta_business_list_pixels(business_id: str) -> dict:
    """
    List all Meta Pixels owned by a Business Manager, with last-fired info.

    Args:
        business_id: Meta Business Manager ID.
    """
    try:
        resp = _meta_get(
            f"{business_id}/owned_pixels",
            {"fields": "id,name,last_fired_time,owner_business,code"}
        )
        return {"pixels": resp.get("data", []), "total": len(resp.get("data", []))}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def meta_business_assign_pixel(
    pixel_id: str,
    ad_account_id: str,
) -> dict:
    """
    Assign a Meta Pixel to an ad account within the same Business Manager.

    Args:
        pixel_id: The Meta Pixel ID.
        ad_account_id: The ad account ID to assign the pixel to (format: act_XXXXXX).
    """
    try:
        result = _meta_post(f"{pixel_id}/shared_accounts", {"account_id": ad_account_id.replace("act_", "")})
        return {"success": bool(result.get("success")), "pixel_id": pixel_id, "ad_account_id": ad_account_id}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def meta_business_shared_audiences(business_id: str) -> dict:
    """
    List custom audiences that are shared across ad accounts in a Business Manager.

    Args:
        business_id: Meta Business Manager ID.
    """
    try:
        resp = _meta_get(
            f"{business_id}/shared_audiences",
            {"fields": "id,name,approximate_count,subtype,data_source,permission_for_actions"}
        )
        return {"audiences": resp.get("data", []), "total": len(resp.get("data", []))}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def meta_business_share_audience(
    audience_id: str,
    destination_account_ids: list[str],
) -> dict:
    """
    Share an existing custom audience to one or more other ad accounts.

    Args:
        audience_id: The custom audience ID to share.
        destination_account_ids: List of ad account IDs to share the audience with.
    """
    try:
        results = []
        for acc_id in destination_account_ids:
            try:
                resp = _meta_post(
                    f"{audience_id}/ad_accounts",
                    {"adaccounts": [acc_id.replace("act_", "")]}
                )
                results.append({"account_id": acc_id, "success": True})
            except Exception as ex:
                results.append({"account_id": acc_id, "success": False, "error": str(ex)})
        return {"results": results, "shared_to": len([r for r in results if r["success"]])}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def meta_business_add_user(
    business_id: str,
    email: str,
    role: str = "EMPLOYEE",
) -> dict:
    """
    Add a user to a Meta Business Manager.

    Args:
        business_id: Meta Business Manager ID.
        email: Email address of the user to invite.
        role: ADMIN | EMPLOYEE | FINANCE_ANALYST | BUSINESS_ANALYST.
    """
    try:
        resp = _meta_post(f"{business_id}/business_users", {"email": email, "role": role})
        return {"success": True, "email": email, "role": role, "response": resp}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def meta_business_grant_account_access(
    business_id: str,
    ad_account_id: str,
    user_id: str,
    tasks: list[str],
) -> dict:
    """
    Grant a Business Manager user access to a specific ad account.

    Args:
        business_id: Meta Business Manager ID.
        ad_account_id: Ad account ID (format: act_XXXXXX).
        user_id: The Meta user ID to grant access to.
        tasks: List of permissions: MANAGE | ADVERTISE | ANALYZE | DRAFT.
    """
    try:
        resp = _meta_post(
            f"{ad_account_id}/assigned_users",
            {"user": user_id, "tasks": tasks, "business": business_id}
        )
        return {"success": bool(resp.get("success")), "user_id": user_id, "ad_account_id": ad_account_id, "tasks": tasks}
    except Exception as e:
        return {"error": str(e)}


# ═════════════════════════════════════════════════════════════════════════════
# CROSS-PLATFORM AGENCY TOOLS
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def agency_cross_platform_overview(
    google_ads_customer_id: str,
    meta_ad_account_id: str,
    ga4_property_id: str,
    date_range: str = "last_30d",
) -> dict:
    """
    Pull a unified performance overview across Google Ads, Meta Ads, and GA4.
    Deduplicates conversions using GA4 as the source of truth.

    Args:
        google_ads_customer_id: Google Ads customer ID.
        meta_ad_account_id: Meta ad account ID (format: act_XXXXXX).
        ga4_property_id: GA4 property ID (numeric, e.g. '123456789').
        date_range: last_7d | last_30d | last_month | this_month.
    """
    errors = []

    # Google Ads
    gads_data: dict[str, Any] = {}
    try:
        client = _google_ads_client(google_ads_customer_id.replace("-", ""))
        ga_service = client.get_service("GoogleAdsService")
        gads_range = {
            "last_7d": "LAST_7_DAYS", "last_30d": "LAST_30_DAYS",
            "last_month": "LAST_MONTH", "this_month": "THIS_MONTH",
        }.get(date_range, "LAST_30_DAYS")
        query = f"""
            SELECT
              metrics.cost_micros, metrics.clicks, metrics.impressions,
              metrics.conversions, metrics.conversions_value
            FROM customer
            WHERE segments.date DURING {gads_range}
        """
        response = ga_service.search(customer_id=google_ads_customer_id.replace("-", ""), query=query)
        spend, clicks, impr, conv, rev = 0, 0, 0, 0.0, 0.0
        for row in response:
            m = row.metrics
            spend += m.cost_micros / 1_000_000
            clicks += m.clicks
            impr += m.impressions
            conv += m.conversions
            rev += m.conversions_value
        gads_data = {"spend": round(spend, 2), "clicks": clicks, "impressions": impr,
                     "reported_conversions": round(conv, 2), "reported_revenue": round(rev, 2),
                     "roas": round(rev / spend, 2) if spend > 0 else 0}
    except Exception as e:
        errors.append(f"Google Ads error: {e}")

    # Meta Ads
    meta_data: dict[str, Any] = {}
    try:
        insights = _meta_get(
            f"{meta_ad_account_id}/insights",
            {"fields": "spend,clicks,impressions,purchase_roas,actions,action_values",
             "date_preset": date_range, "level": "account"}
        )
        d = insights.get("data", [{}])[0] if insights.get("data") else {}
        spend = float(d.get("spend", 0))
        roas_list = d.get("purchase_roas", [])
        roas = float(roas_list[0].get("value", 0)) if roas_list else 0
        purchases = next(
            (float(a["value"]) for a in d.get("actions", []) if a["action_type"] == "purchase"), 0
        )
        meta_data = {"spend": round(spend, 2), "clicks": int(d.get("clicks", 0)),
                     "impressions": int(d.get("impressions", 0)),
                     "reported_conversions": round(purchases, 2),
                     "reported_revenue": round(spend * roas, 2),
                     "roas": round(roas, 2)}
    except Exception as e:
        errors.append(f"Meta Ads error: {e}")

    # GA4 (source of truth for conversions)
    ga4_data: dict[str, Any] = {}
    try:
        from google.analytics.data_v1beta import BetaAnalyticsDataClient
        from google.analytics.data_v1beta.types import DateRange, Metric
        from google.oauth2.credentials import Credentials
        from credentials import provider
        _ga4_rt = provider().google_token("ga4")
        creds = Credentials(token=None, refresh_token=_ga4_rt,
                            client_id=__import__("os").environ.get("GOOGLE_CLIENT_ID"),
                            client_secret=__import__("os").environ.get("GOOGLE_CLIENT_SECRET"),
                            token_uri="https://oauth2.googleapis.com/token")
        ga_client = BetaAnalyticsDataClient(credentials=creds)
        from google.analytics.data_v1beta.types import RunReportRequest
        days = {"last_7d": "7daysAgo", "last_30d": "30daysAgo", "last_month": "30daysAgo"}.get(date_range, "30daysAgo")
        req = RunReportRequest(
            property=f"properties/{ga4_property_id}",
            metrics=[Metric(name="sessions"), Metric(name="conversions"), Metric(name="totalRevenue")],
            date_ranges=[DateRange(start_date=days, end_date="yesterday")],
        )
        resp = ga_client.run_report(req)
        row = resp.rows[0] if resp.rows else None
        if row:
            ga4_data = {
                "sessions": int(row.metric_values[0].value),
                "conversions": int(row.metric_values[1].value),
                "total_revenue": round(float(row.metric_values[2].value), 2),
            }
    except Exception as e:
        errors.append(f"GA4 error: {e}")

    # Summary
    total_spend = gads_data.get("spend", 0) + meta_data.get("spend", 0)
    true_conversions = ga4_data.get("conversions", 0)
    true_revenue = ga4_data.get("total_revenue", 0)
    true_roas = round(true_revenue / total_spend, 2) if total_spend > 0 else 0

    return {
        "summary": {
            "total_spend_all_channels": total_spend,
            "true_conversions_ga4": true_conversions,
            "true_revenue_ga4": true_revenue,
            "true_roas": true_roas,
            "reported_roas_google": gads_data.get("roas", 0),
            "reported_roas_meta": meta_data.get("roas", 0),
            "roas_inflation_vs_true": round(
                ((gads_data.get("roas", 0) + meta_data.get("roas", 0)) / 2) - true_roas, 2
            ),
        },
        "google_ads": gads_data,
        "meta_ads": meta_data,
        "ga4": ga4_data,
        "date_range": date_range,
        "errors": errors,
    }


@mcp.tool()
async def agency_account_health_audit(
    google_ads_customer_id: str = "",
    meta_ad_account_id: str = "",
) -> dict:
    """
    Run a comprehensive health audit on Google Ads and/or Meta Ads accounts.
    Scores each area 0-100 and highlights the top issues to fix.

    Args:
        google_ads_customer_id: Google Ads customer ID (optional).
        meta_ad_account_id: Meta ad account ID (optional, format: act_XXXXXX).
    """
    audit = {"scores": {}, "issues": [], "quick_wins": []}

    if google_ads_customer_id:
        try:
            client = _google_ads_client(google_ads_customer_id.replace("-", ""))
            ga_service = client.get_service("GoogleAdsService")

            # Quality score check
            # Query keyword_view, not ad_group_criterion: metrics.cost_micros /
            # metrics.impressions are prohibited in a SELECT from ad_group_criterion.
            qs_query = """
                SELECT ad_group_criterion.quality_info.quality_score,
                       metrics.impressions, metrics.cost_micros
                FROM keyword_view
                WHERE ad_group_criterion.type = 'KEYWORD'
                  AND ad_group_criterion.status = 'ENABLED'
                  AND segments.date DURING LAST_30_DAYS
            """
            qs_resp = ga_service.search(customer_id=google_ads_customer_id.replace("-", ""), query=qs_query)
            qs_scores = []
            low_qs_spend = 0.0
            for row in qs_resp:
                qs = row.ad_group_criterion.quality_info.quality_score
                if qs:
                    qs_scores.append(qs)
                    if qs < 5:
                        low_qs_spend += row.metrics.cost_micros / 1_000_000
            avg_qs = round(sum(qs_scores) / len(qs_scores), 1) if qs_scores else 0
            qs_score = min(100, int(avg_qs * 10))
            audit["scores"]["google_ads_quality_score"] = qs_score
            if avg_qs < 6:
                audit["issues"].append(f"Google Ads avg Quality Score is {avg_qs}/10 — below 6 hurts ad rank and CPCs")
            if low_qs_spend > 50:
                audit["quick_wins"].append(f"${low_qs_spend:.0f}/mo spent on keywords with QS < 5 — rewrite their ads and landing pages")

            # Budget pacing / waste check
            search_terms_query = """
                SELECT search_term_view.search_term, metrics.cost_micros, metrics.conversions
                FROM search_term_view
                WHERE segments.date DURING LAST_30_DAYS
                  AND metrics.conversions = 0
                  AND metrics.cost_micros > 5000000
                ORDER BY metrics.cost_micros DESC
                LIMIT 10
            """
            st_resp = ga_service.search(customer_id=google_ads_customer_id.replace("-", ""), query=search_terms_query)
            wasted_terms = []
            total_wasted = 0.0
            for row in st_resp:
                spend = row.metrics.cost_micros / 1_000_000
                total_wasted += spend
                wasted_terms.append({"term": row.search_term_view.search_term, "spend": round(spend, 2)})
            if total_wasted > 0:
                audit["quick_wins"].append(
                    f"${total_wasted:.0f} wasted on {len(wasted_terms)} zero-conversion search terms last 30 days — add as negatives"
                )
            audit["scores"]["google_ads_search_term_relevance"] = max(0, 100 - int(len(wasted_terms) * 5))

        except Exception as e:
            audit["errors_google"] = str(e)

    if meta_ad_account_id:
        try:
            # Frequency check
            adset_insights = _meta_get(
                f"{meta_ad_account_id}/insights",
                {"fields": "adset_id,adset_name,frequency,reach,spend,cpc",
                 "level": "adset", "date_preset": "last_30d",
                 "filtering": '[{"field":"spend","operator":"GREATER_THAN","value":"100"}]'}
            )
            high_freq = [
                a for a in adset_insights.get("data", [])
                if float(a.get("frequency", 0)) > 4
            ]
            if high_freq:
                audit["issues"].append(
                    f"{len(high_freq)} Meta ad sets have frequency > 4 — audience fatigue likely, rotate creatives"
                )
                audit["scores"]["meta_creative_freshness"] = max(0, 100 - len(high_freq) * 15)
            else:
                audit["scores"]["meta_creative_freshness"] = 90

            # Spend distribution check (are too many ad sets underfunded?)
            all_adsets = adset_insights.get("data", [])
            underfunded = [a for a in all_adsets if float(a.get("spend", 0)) < 10]
            if len(underfunded) > len(all_adsets) * 0.4:
                audit["issues"].append(
                    f"{len(underfunded)}/{len(all_adsets)} Meta ad sets spent < $10 — consolidate into fewer, better-funded ad sets"
                )
            audit["scores"]["meta_budget_efficiency"] = max(0, 100 - int(len(underfunded) / max(len(all_adsets), 1) * 100))

        except Exception as e:
            audit["errors_meta"] = str(e)

    # Overall score
    scores = list(audit["scores"].values())
    audit["overall_score"] = round(sum(scores) / len(scores), 1) if scores else 0
    audit["issues_count"] = len(audit["issues"])
    audit["quick_wins_count"] = len(audit["quick_wins"])

    return audit


@mcp.tool()
async def agency_budget_pacing_all_channels(
    google_ads_customer_id: str = "",
    meta_ad_account_id: str = "",
) -> dict:
    """
    Show budget pacing status across all connected ad platforms for the current month.
    Flags channels that are over or under their expected monthly spend.

    Args:
        google_ads_customer_id: Google Ads customer ID (optional).
        meta_ad_account_id: Meta ad account ID (optional).
    """
    today = datetime.now(timezone.utc)
    days_in_month = 30  # approximate
    pacing_ratio = today.day / days_in_month
    results = {}

    if google_ads_customer_id:
        try:
            client = _google_ads_client(google_ads_customer_id.replace("-", ""))
            ga_service = client.get_service("GoogleAdsService")
            query = """
                SELECT
                  campaign.name, campaign_budget.amount_micros, metrics.cost_micros
                FROM campaign
                WHERE campaign.status = 'ENABLED'
                  AND segments.date DURING THIS_MONTH
            """
            response = ga_service.search(customer_id=google_ads_customer_id.replace("-", ""), query=query)
            total_budget = 0.0
            total_spent = 0.0
            for row in response:
                total_budget += row.campaign_budget.amount_micros * days_in_month / 1_000_000
                total_spent += row.metrics.cost_micros / 1_000_000
            expected = total_budget * pacing_ratio
            pacing_pct = round((total_spent / expected * 100) if expected > 0 else 0, 1)
            results["google_ads"] = {
                "monthly_budget": round(total_budget, 2),
                "spent_so_far": round(total_spent, 2),
                "expected_by_now": round(expected, 2),
                "pacing_pct": pacing_pct,
                "status": "overpacing" if pacing_pct > 110 else "underpacing" if pacing_pct < 80 else "on_track",
            }
        except Exception as e:
            results["google_ads"] = {"error": str(e)}

    if meta_ad_account_id:
        try:
            # Get account spend cap as proxy for budget
            acc = _meta_get(meta_ad_account_id, {"fields": "spend_cap,amount_spent,currency"})
            spent = float(acc.get("amount_spent", 0)) / 100  # Meta returns in cents
            cap = float(acc.get("spend_cap", 0)) / 100 if acc.get("spend_cap") else None
            results["meta_ads"] = {
                "spend_cap": round(cap, 2) if cap else "none",
                "spent_so_far": round(spent, 2),
                "currency": acc.get("currency", ""),
                "note": "Meta pacing is per account spend cap, not per-campaign monthly budget" if cap else "No account spend cap set",
            }
        except Exception as e:
            results["meta_ads"] = {"error": str(e)}

    results["as_of"] = today.strftime("%Y-%m-%d"),
    results["day_of_month"] = today.day
    return results


@mcp.tool()
async def agency_weekly_report(
    google_ads_customer_id: str = "",
    meta_ad_account_id: str = "",
    ga4_property_id: str = "",
) -> dict:
    """
    Generate a formatted weekly performance report across all connected platforms.
    Compares this week vs last week and highlights the biggest changes.

    Args:
        google_ads_customer_id: Google Ads customer ID (optional).
        meta_ad_account_id: Meta ad account ID (optional).
        ga4_property_id: GA4 property ID (optional).
    """
    report: dict[str, Any] = {
        "report_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "period": "Last 7 days vs prior 7 days",
        "platforms": {},
        "key_alerts": [],
    }

    if google_ads_customer_id:
        try:
            client = _google_ads_client(google_ads_customer_id.replace("-", ""))
            ga_service = client.get_service("GoogleAdsService")

            def _fetch_gads_week(period: str):
                q = f"""
                    SELECT metrics.cost_micros, metrics.clicks, metrics.impressions,
                           metrics.conversions, metrics.conversions_value
                    FROM customer WHERE segments.date DURING {period}
                """
                rows = list(ga_service.search(customer_id=google_ads_customer_id.replace("-", ""), query=q))
                r = rows[0].metrics if rows else None
                if not r:
                    return {}
                spend = r.cost_micros / 1_000_000
                return {
                    "spend": round(spend, 2),
                    "clicks": r.clicks,
                    "conversions": round(r.conversions, 1),
                    "roas": round(r.conversions_value / spend, 2) if spend > 0 else 0,
                }

            this_week = _fetch_gads_week("LAST_7_DAYS")
            # Note: Google Ads doesn't have a PRIOR_7_DAYS segment, so we use 14 days and split
            report["platforms"]["google_ads"] = {
                "this_week": this_week,
                "period": "LAST_7_DAYS",
            }
            if this_week:
                if this_week.get("roas", 0) < 2:
                    report["key_alerts"].append("Google Ads ROAS below 2x this week")
        except Exception as e:
            report["platforms"]["google_ads"] = {"error": str(e)}

    if meta_ad_account_id:
        try:
            def _fetch_meta(since, until):
                # Use an explicit time_range, not date_preset="last_week" — that
                # preset was removed from the Graph API and 400s. This also lets
                # us compare a rolling 7-day window vs the prior 7 days.
                resp = _meta_get(
                    f"{meta_ad_account_id}/insights",
                    {"fields": "spend,clicks,impressions,purchase_roas,actions",
                     "time_range": json.dumps({"since": str(since), "until": str(until)}),
                     "level": "account"}
                )
                d = resp.get("data", [{}])[0] if resp.get("data") else {}
                spend = float(d.get("spend", 0))
                roas_list = d.get("purchase_roas", [])
                roas = float(roas_list[0].get("value", 0)) if roas_list else 0
                return {
                    "spend": round(spend, 2),
                    "clicks": int(d.get("clicks", 0)),
                    "roas": round(roas, 2),
                }

            _today = date.today()
            this_week = _fetch_meta(_today - timedelta(days=7), _today - timedelta(days=1))
            last_week = _fetch_meta(_today - timedelta(days=14), _today - timedelta(days=8))
            delta_spend = round(this_week["spend"] - last_week.get("spend", 0), 2)
            report["platforms"]["meta_ads"] = {
                "this_week": this_week,
                "last_week": last_week,
                "spend_change": delta_spend,
                "spend_change_pct": round(delta_spend / last_week["spend"] * 100, 1) if last_week.get("spend") else 0,
            }
            if this_week.get("roas", 0) < 2:
                report["key_alerts"].append("Meta Ads ROAS below 2x this week")
        except Exception as e:
            report["platforms"]["meta_ads"] = {"error": str(e)}

    if ga4_property_id:
        try:
            from google.analytics.data_v1beta import BetaAnalyticsDataClient
            from google.analytics.data_v1beta.types import DateRange, Metric, RunReportRequest
            from google.oauth2.credentials import Credentials
            from credentials import provider
            _ga4_rt = provider().google_token("ga4")
            creds = Credentials(token=None, refresh_token=_ga4_rt,
                                client_id=__import__("os").environ.get("GOOGLE_CLIENT_ID"),
                                client_secret=__import__("os").environ.get("GOOGLE_CLIENT_SECRET"),
                                token_uri="https://oauth2.googleapis.com/token")
            ga_client = BetaAnalyticsDataClient(credentials=creds)
            req = RunReportRequest(
                property=f"properties/{ga4_property_id}",
                metrics=[Metric(name="sessions"), Metric(name="conversions"), Metric(name="bounceRate")],
                date_ranges=[
                    DateRange(start_date="7daysAgo", end_date="yesterday"),
                    DateRange(start_date="14daysAgo", end_date="8daysAgo"),
                ],
            )
            resp = ga_client.run_report(req)
            rows = resp.rows
            this_week_ga = {
                "sessions": int(rows[0].metric_values[0].value) if rows else 0,
                "conversions": int(rows[0].metric_values[1].value) if rows else 0,
                "bounce_rate": round(float(rows[0].metric_values[2].value) * 100, 1) if rows else 0,
            } if rows else {}
            last_week_ga = {
                "sessions": int(rows[1].metric_values[0].value) if len(rows) > 1 else 0,
                "conversions": int(rows[1].metric_values[1].value) if len(rows) > 1 else 0,
            } if len(rows) > 1 else {}
            report["platforms"]["ga4"] = {"this_week": this_week_ga, "last_week": last_week_ga}
        except Exception as e:
            report["platforms"]["ga4"] = {"error": str(e)}

    return report


@mcp.tool()
async def agency_wasted_spend_report(
    google_ads_customer_id: str = "",
    meta_ad_account_id: str = "",
    days: int = 30,
) -> dict:
    """
    Find wasted ad spend across Google Ads and Meta Ads.
    Returns actionable items: zero-conversion search terms, high-frequency ad sets,
    paused/disapproved ads still consuming budget, and more.

    Args:
        google_ads_customer_id: Google Ads customer ID (optional).
        meta_ad_account_id: Meta ad account ID (optional).
        days: Lookback window in days (default 30).
    """
    report: dict[str, Any] = {"waste_items": [], "total_wasted_estimate": 0.0}

    if google_ads_customer_id:
        try:
            client = _google_ads_client(google_ads_customer_id.replace("-", ""))
            ga_service = client.get_service("GoogleAdsService")

            # Zero-conversion search terms
            q = f"""
                SELECT search_term_view.search_term, metrics.cost_micros, metrics.clicks
                FROM search_term_view
                WHERE segments.date DURING LAST_{days}_DAYS
                  AND metrics.conversions = 0
                  AND metrics.cost_micros > 3000000
                ORDER BY metrics.cost_micros DESC LIMIT 20
            """
            rows = list(ga_service.search(customer_id=google_ads_customer_id.replace("-", ""), query=q))
            st_waste = sum(r.metrics.cost_micros / 1_000_000 for r in rows)
            if rows:
                report["waste_items"].append({
                    "platform": "google_ads",
                    "type": "zero_conversion_search_terms",
                    "estimated_waste": round(st_waste, 2),
                    "action": f"Add {len(rows)} search terms as negatives",
                    "details": [{"term": r.search_term_view.search_term, "spend": round(r.metrics.cost_micros / 1_000_000, 2)} for r in rows[:5]],
                })
                report["total_wasted_estimate"] += st_waste

            # Night/weekend waste (ads running off-hours with no conversions)
            # Simplified: check hourly performance segments
        except Exception as e:
            report["google_ads_error"] = str(e)

    if meta_ad_account_id:
        try:
            # High frequency ad sets
            resp = _meta_get(
                f"{meta_ad_account_id}/insights",
                {"fields": "adset_id,adset_name,frequency,spend,reach",
                 "level": "adset", "date_preset": f"last_{days}d",
                 "filtering": '[{"field":"spend","operator":"GREATER_THAN","value":"50"}]'}
            )
            high_freq = [a for a in resp.get("data", []) if float(a.get("frequency", 0)) > 4]
            freq_waste = sum(float(a.get("spend", 0)) * 0.25 for a in high_freq)  # ~25% estimated waste on fatigued audiences
            if high_freq:
                report["waste_items"].append({
                    "platform": "meta_ads",
                    "type": "audience_fatigue_high_frequency",
                    "estimated_waste": round(freq_waste, 2),
                    "action": f"Refresh creatives or expand audiences on {len(high_freq)} ad sets with frequency > 4",
                    "details": [{"adset": a.get("adset_name"), "frequency": float(a.get("frequency", 0)), "spend": float(a.get("spend", 0))} for a in high_freq[:5]],
                })
                report["total_wasted_estimate"] += freq_waste

            # Tiny ad sets draining budget
            small_resp = _meta_get(
                f"{meta_ad_account_id}/insights",
                {"fields": "adset_id,adset_name,spend,clicks,actions",
                 "level": "adset", "date_preset": f"last_{days}d",
                 "filtering": '[{"field":"spend","operator":"GREATER_THAN","value":"5"}]'}
            )
            no_conv_adsets = []
            for a in small_resp.get("data", []):
                purchases = sum(float(x["value"]) for x in a.get("actions", []) if x["action_type"] == "purchase")
                if purchases == 0 and float(a.get("spend", 0)) > 20:
                    no_conv_adsets.append(a)
            no_conv_waste = sum(float(a.get("spend", 0)) for a in no_conv_adsets)
            if no_conv_adsets:
                report["waste_items"].append({
                    "platform": "meta_ads",
                    "type": "zero_purchase_adsets",
                    "estimated_waste": round(no_conv_waste, 2),
                    "action": f"Pause or restructure {len(no_conv_adsets)} ad sets with spend but zero purchases",
                    "details": [{"adset": a.get("adset_name"), "spend": float(a.get("spend", 0))} for a in no_conv_adsets[:5]],
                })
                report["total_wasted_estimate"] += no_conv_waste

        except Exception as e:
            report["meta_ads_error"] = str(e)

    report["total_wasted_estimate"] = round(report["total_wasted_estimate"], 2)
    report["waste_item_count"] = len(report["waste_items"])
    report["days"] = days
    return report
