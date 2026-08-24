"""
Google Ads — advanced tools.
Extends google_ads.py with:
keyword planner, audience segments, bid modifiers, Performance Max,
ad schedule reports, experiments, smart bidding diagnostics, attribution paths,
shopping product groups, dynamic search targets, brand safety exclusions.
"""
import json
import os
from datetime import date, timedelta
from typing import Optional

from mcp_instance import mcp
from auth import current_user_ctx
from permissions import require_editor

# Re-use helpers from google_ads.py
from tools.google_ads import _get_client, _search


# ---------------------------------------------------------------------------
# Geo target constants
# ---------------------------------------------------------------------------

# Google wants a numeric geo target constant ID, callers speak ISO country
# codes. The table is shared with the Ads Transparency tools, which filter on
# the same IDs -- two copies would drift.
from tools.geo_targets import geo_target_id as _geo_target_id  # noqa: E402


# ---------------------------------------------------------------------------
# Keyword Planner — keyword ideas
# ---------------------------------------------------------------------------

@mcp.tool()
def google_ads_keyword_ideas(
    customer_id: str,
    seed_keywords: str,
    language_id: str = "1000",
    country_code: str = "US",
    limit: int = 50,
) -> dict:
    """
    Generate keyword ideas using Google Ads Keyword Planner.

    Args:
        customer_id: Google Ads customer ID
        seed_keywords: Comma-separated seed keywords (e.g. 'running shoes,athletic footwear')
        language_id: Google language criterion ID (1000=English, 1005=Spanish)
        country_code: Two-letter ISO country code, e.g. 'US', 'IL', 'GB'
            (default: 'US'). A numeric geo target constant ID such as
            '2376' is also accepted.
        limit: Max keyword ideas to return (default: 50)
    """
    require_editor()
    client, cid = _get_client(customer_id)
    svc = client.get_service("KeywordPlanIdeaService")

    request = client.get_type("GenerateKeywordIdeasRequest")
    request.customer_id = cid
    request.language = f"languageConstants/{language_id}"
    request.geo_target_constants.append(
        client.get_service("GeoTargetConstantService").geo_target_constant_path(_geo_target_id(country_code))
    )
    request.include_adult_keywords = False
    request.keyword_seed.keywords.extend([k.strip() for k in seed_keywords.split(",")])

    ideas = []
    for idx, idea in enumerate(svc.generate_keyword_ideas(request=request)):
        if idx >= limit:
            break
        metrics = idea.keyword_idea_metrics
        ideas.append({
            "keyword": idea.text,
            "avg_monthly_searches": metrics.avg_monthly_searches,
            "competition": metrics.competition.name,
            "low_bid": round(metrics.low_top_of_page_bid_micros / 1_000_000, 2) if metrics.low_top_of_page_bid_micros else 0,
            "high_bid": round(metrics.high_top_of_page_bid_micros / 1_000_000, 2) if metrics.high_top_of_page_bid_micros else 0,
        })
    ideas.sort(key=lambda x: x["avg_monthly_searches"], reverse=True)
    return {"keyword_ideas": ideas, "count": len(ideas), "seeds": seed_keywords}


# ---------------------------------------------------------------------------
# Keyword Planner — traffic forecast
# ---------------------------------------------------------------------------

@mcp.tool()
def google_ads_keyword_forecast(
    customer_id: str,
    keywords: str,
    average_cpc_micros: int = 1_000_000,
    language_id: str = "1000",
    country_code: str = "US",
    forecast_days: int = 30,
) -> dict:
    """
    Forecast clicks, impressions, and cost for a list of keywords using Keyword Planner.

    Args:
        customer_id: Google Ads customer ID
        keywords: Comma-separated keywords to forecast
        average_cpc_micros: Target CPC in micros (1_000_000 = $1.00)
        language_id: Language criterion ID (1000=English)
        country_code: Two-letter ISO country code, e.g. 'US', 'IL', 'GB'
            (default: 'US'). A numeric geo target constant ID such as
            '2376' is also accepted.
        forecast_days: Length of the forecast window in days, starting
            tomorrow (default: 30). Keyword Planner only forecasts future
            periods, up to a year out.
    """
    require_editor()
    client, cid = _get_client(customer_id)
    svc = client.get_service("KeywordPlanIdeaService")

    kw_list = [k.strip() for k in keywords.split(",")]

    request = client.get_type("GenerateKeywordForecastMetricsRequest")
    request.customer_id = cid

    # Keyword Planner rejects a period that starts in the past.
    start = date.today() + timedelta(days=1)
    request.forecast_period.start_date = start.isoformat()
    request.forecast_period.end_date = (start + timedelta(days=max(forecast_days, 1))).isoformat()

    campaign = request.campaign
    campaign.keyword_plan_network = client.enums.KeywordPlanNetworkEnum.GOOGLE_SEARCH
    campaign.language_constants.append(f"languageConstants/{language_id}")
    geo = client.get_type("CriterionBidModifier")
    geo.geo_target_constant = (
        client.get_service("GeoTargetConstantService")
        .geo_target_constant_path(_geo_target_id(country_code))
    )
    campaign.geo_modifiers.append(geo)
    campaign.bidding_strategy.manual_cpc_bidding_strategy.max_cpc_bid_micros = average_cpc_micros

    ad_group = client.get_type("ForecastAdGroup")
    ad_group.max_cpc_bid_micros = average_cpc_micros
    for kw in kw_list:
        biddable = client.get_type("BiddableKeyword")
        biddable.keyword.text = kw
        biddable.keyword.match_type = client.enums.KeywordMatchTypeEnum.BROAD
        biddable.max_cpc_bid_micros = average_cpc_micros
        ad_group.biddable_keywords.append(biddable)
    campaign.ad_groups.append(ad_group)

    try:
        resp = svc.generate_keyword_forecast_metrics(request=request)
        m = resp.campaign_forecast_metrics
        return {
            "keywords": kw_list,
            "forecast_start": request.forecast_period.start_date,
            "forecast_end": request.forecast_period.end_date,
            "forecasted_clicks": round(m.clicks, 1),
            "forecasted_impressions": round(m.impressions, 1),
            "forecasted_cost": round(m.cost_micros / 1_000_000, 2),
            "forecasted_conversions": round(m.conversions, 2),
            "forecasted_ctr": round(m.click_through_rate, 4),
        }
    except Exception as e:
        return {"error": str(e), "keywords": kw_list}


# ---------------------------------------------------------------------------
# Audience segments
# ---------------------------------------------------------------------------

@mcp.tool()
def google_ads_audience_segments(
    customer_id: str,
    type_filter: str = "all",
) -> dict:
    """
    List audience segments available for targeting in this Google Ads account.

    Args:
        customer_id: Google Ads customer ID
        type_filter: 'user_list' | 'in_market' | 'affinity' | 'all' (default: 'all')
    """
    client, cid = _get_client(customer_id)

    gaql = """
        SELECT user_list.id, user_list.name, user_list.description,
               user_list.size_for_search, user_list.size_for_display,
               user_list.type, user_list.membership_status
        FROM user_list
        WHERE user_list.membership_status = 'OPEN'
        ORDER BY user_list.size_for_search DESC
        LIMIT 100
    """
    rows = _search(gaql, customer_id)
    lists = []
    for row in rows:
        ul = row.user_list
        lists.append({
            "id": str(ul.id),
            "name": ul.name,
            "description": ul.description,
            "size_search": ul.size_for_search,
            "size_display": ul.size_for_display,
            "type": ul.type_.name if hasattr(ul.type_, "name") else str(ul.type_),
        })
    return {"audience_segments": lists, "count": len(lists)}


# ---------------------------------------------------------------------------
# Bid modifiers — list
# ---------------------------------------------------------------------------

@mcp.tool()
def google_ads_bid_modifiers(
    customer_id: str,
    campaign_id: str = "",
) -> dict:
    """
    List all bid modifiers on campaigns (device, location, ad schedule, audience).

    Args:
        customer_id: Google Ads customer ID
        campaign_id: Filter to a specific campaign ID (leave empty for all)
    """
    client, cid = _get_client(customer_id)
    where = f"AND campaign.id = {campaign_id}" if campaign_id else ""
    gaql = f"""
        SELECT campaign_criterion.campaign, campaign_criterion.criterion_id,
               campaign_criterion.bid_modifier, campaign_criterion.type,
               campaign_criterion.device.type, campaign_criterion.location.geo_target_constant,
               campaign_criterion.ad_schedule.day_of_week
        FROM campaign_criterion
        WHERE campaign_criterion.bid_modifier != 1.0
          AND campaign.status != 'REMOVED'
          {where}
        ORDER BY campaign_criterion.bid_modifier DESC
        LIMIT 500
    """
    rows = _search(gaql, customer_id)
    modifiers = []
    for row in rows:
        cc = row.campaign_criterion
        modifiers.append({
            "campaign_id": cc.campaign.split("/")[-1] if cc.campaign else "",
            "criterion_id": cc.criterion_id,
            "bid_modifier": cc.bid_modifier,
            "type": cc.type_.name if hasattr(cc.type_, "name") else str(cc.type_),
        })
    return {"bid_modifiers": modifiers, "count": len(modifiers)}


# ---------------------------------------------------------------------------
# Bid modifiers — update
# ---------------------------------------------------------------------------

@mcp.tool()
def google_ads_update_bid_modifier(
    customer_id: str,
    campaign_id: str,
    criterion_id: str,
    new_bid_modifier: float,
) -> dict:
    """
    Update a bid modifier on a campaign criterion.

    Args:
        customer_id: Google Ads customer ID
        campaign_id: Campaign ID
        criterion_id: Criterion resource ID or numeric ID
        new_bid_modifier: New bid modifier value (0.1 to 10.0; 0 = completely excluded)
    """
    require_editor()
    client, cid = _get_client(customer_id)
    svc = client.get_service("CampaignCriterionService")

    camp_criterion = client.get_type("CampaignCriterion")
    camp_criterion.resource_name = svc.campaign_criterion_path(cid, campaign_id, criterion_id)
    camp_criterion.bid_modifier = float(new_bid_modifier)

    from google.protobuf import field_mask_pb2
    op = client.get_type("CampaignCriterionOperation")
    op.update.CopyFrom(camp_criterion)
    op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["bid_modifier"]))

    resp = svc.mutate_campaign_criteria(customer_id=cid, operations=[op])
    return {"updated": resp.results[0].resource_name, "new_bid_modifier": new_bid_modifier}


# ---------------------------------------------------------------------------
# Performance Max — list asset groups
# ---------------------------------------------------------------------------

@mcp.tool()
def google_ads_performance_max_asset_groups(
    customer_id: str,
    campaign_id: str = "",
) -> dict:
    """
    List asset groups for Performance Max campaigns.

    Args:
        customer_id: Google Ads customer ID
        campaign_id: Optional: filter to a specific P-Max campaign ID
    """
    where = f"AND campaign.id = {campaign_id}" if campaign_id else ""
    gaql = f"""
        SELECT asset_group.id, asset_group.name, asset_group.status,
               asset_group.campaign, asset_group.final_urls,
               asset_group.path1, asset_group.path2
        FROM asset_group
        WHERE campaign.advertising_channel_type = 'PERFORMANCE_MAX'
          AND asset_group.status != 'REMOVED'
          {where}
        LIMIT 100
    """
    rows = _search(gaql, customer_id)
    groups = []
    for row in rows:
        ag = row.asset_group
        groups.append({
            "id": str(ag.id),
            "name": ag.name,
            "status": ag.status.name if hasattr(ag.status, "name") else str(ag.status),
            "campaign_id": ag.campaign.split("/")[-1] if ag.campaign else "",
            "final_urls": list(ag.final_urls),
            "path1": ag.path1,
            "path2": ag.path2,
        })
    return {"asset_groups": groups, "count": len(groups)}


# ---------------------------------------------------------------------------
# Performance Max — create campaign
# ---------------------------------------------------------------------------

@mcp.tool()
def google_ads_create_performance_max(
    customer_id: str,
    campaign_name: str,
    budget_amount_micros: int,
    target_roas: float = None,
    target_cpa_micros: int = None,
    final_url: str = "",
) -> dict:
    """
    Create a new Performance Max campaign with a target ROAS or target CPA bidding strategy.

    Args:
        customer_id: Google Ads customer ID
        campaign_name: Name for the new P-Max campaign
        budget_amount_micros: Daily budget in micros (e.g. 50_000_000 = $50/day)
        target_roas: Target ROAS as decimal (e.g. 3.0 = 300% ROAS). Mutually exclusive with target_cpa_micros.
        target_cpa_micros: Target CPA in micros (e.g. 20_000_000 = $20 CPA). Mutually exclusive with target_roas.
        final_url: Landing page URL for the campaign
    """
    require_editor()
    client, cid = _get_client(customer_id)

    budget_svc = client.get_service("CampaignBudgetService")
    campaign_svc = client.get_service("CampaignService")

    # 1. Create budget
    budget_op = client.get_type("CampaignBudgetOperation")
    budget = budget_op.create
    budget.name = f"{campaign_name} Budget"
    budget.amount_micros = budget_amount_micros
    budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD

    budget_resp = budget_svc.mutate_campaign_budgets(customer_id=cid, operations=[budget_op])
    budget_resource = budget_resp.results[0].resource_name

    # 2. Create campaign
    camp_op = client.get_type("CampaignOperation")
    camp = camp_op.create
    camp.name = campaign_name
    camp.advertising_channel_type = client.enums.AdvertisingChannelTypeEnum.PERFORMANCE_MAX
    camp.status = client.enums.CampaignStatusEnum.PAUSED
    camp.campaign_budget = budget_resource

    if target_roas:
        camp.maximize_conversion_value.target_roas = target_roas
    elif target_cpa_micros:
        camp.maximize_conversions.target_cpa_micros = target_cpa_micros
    else:
        camp.maximize_conversion_value.CopyFrom(client.get_type("MaximizeConversionValue")())

    camp_resp = campaign_svc.mutate_campaigns(customer_id=cid, operations=[camp_op])
    camp_resource = camp_resp.results[0].resource_name

    return {
        "campaign_resource": camp_resource,
        "campaign_name": campaign_name,
        "budget_micros": budget_amount_micros,
        "status": "PAUSED",
        "note": "Campaign created in PAUSED state. Add asset groups, then enable.",
    }


# ---------------------------------------------------------------------------
# Ad schedule performance (day of week × hour)
# ---------------------------------------------------------------------------

@mcp.tool()
def google_ads_ad_schedule_report(
    customer_id: str,
    start_date: str = None,
    end_date: str = None,
    campaign_id: str = "",
) -> dict:
    """
    Return performance metrics broken down by day of week and hour of day.
    Use this to identify optimal ad scheduling windows.

    Args:
        customer_id: Google Ads customer ID
        start_date: YYYY-MM-DD (default: last 30 days)
        end_date: YYYY-MM-DD (default: yesterday)
        campaign_id: Optional campaign ID filter
    """
    today = date.today()
    start = start_date or (today - timedelta(days=30)).strftime("%Y-%m-%d")
    end = end_date or (today - timedelta(days=1)).strftime("%Y-%m-%d")
    where = f"AND campaign.id = {campaign_id}" if campaign_id else ""

    gaql = f"""
        SELECT segments.day_of_week, segments.hour,
               metrics.clicks, metrics.impressions, metrics.cost_micros,
               metrics.conversions, metrics.conversions_value
        FROM campaign
        WHERE segments.date BETWEEN '{start}' AND '{end}'
          AND campaign.status != 'REMOVED'
          {where}
        ORDER BY metrics.cost_micros DESC
        LIMIT 1000
    """
    rows = _search(gaql, customer_id)
    schedule = []
    for row in rows:
        seg = row.segments
        m = row.metrics
        schedule.append({
            "day": seg.day_of_week.name if hasattr(seg.day_of_week, "name") else str(seg.day_of_week),
            "hour": seg.hour,
            "clicks": m.clicks,
            "impressions": m.impressions,
            "cost": round(m.cost_micros / 1_000_000, 2),
            "conversions": round(m.conversions, 2),
            "roas": round(m.conversions_value / max(m.cost_micros / 1_000_000, 0.01), 2),
        })
    return {"ad_schedule_performance": schedule, "start": start, "end": end}


# ---------------------------------------------------------------------------
# Experiments (drafts & experiments)
# ---------------------------------------------------------------------------

@mcp.tool()
def google_ads_experiments(
    customer_id: str,
    status_filter: str = "active",
) -> dict:
    """
    List A/B campaign experiments (formerly Campaign Drafts & Experiments).

    Args:
        customer_id: Google Ads customer ID
        status_filter: 'active' | 'ended' | 'all' (default: 'active')
    """
    where_clause = ""
    if status_filter == "active":
        where_clause = "WHERE experiment.status IN ('INITIATED', 'PROMOTED', 'RUNNING')"
    elif status_filter == "ended":
        where_clause = "WHERE experiment.status = 'ENDED'"

    gaql = f"""
        SELECT experiment.id, experiment.name, experiment.description,
               experiment.status, experiment.type, experiment.start_date,
               experiment.end_date, experiment.traffic_split_percent
        FROM experiment
        {where_clause}
        LIMIT 100
    """
    rows = _search(gaql, customer_id)
    experiments = []
    for row in rows:
        exp = row.experiment
        experiments.append({
            "id": str(exp.id),
            "name": exp.name,
            "description": exp.description,
            "status": exp.status.name if hasattr(exp.status, "name") else str(exp.status),
            "type": exp.type_.name if hasattr(exp.type_, "name") else str(exp.type_),
            "start_date": exp.start_date,
            "end_date": exp.end_date,
            "traffic_split_pct": exp.traffic_split_percent,
        })
    return {"experiments": experiments, "count": len(experiments)}


# ---------------------------------------------------------------------------
# Smart bidding diagnostics
# ---------------------------------------------------------------------------

@mcp.tool()
def google_ads_smart_bidding_report(
    customer_id: str,
    start_date: str = None,
    end_date: str = None,
) -> dict:
    """
    Return smart bidding strategy performance: tROAS, tCPA, and maximize conversion strategies.
    Shows actual vs target ROAS/CPA and learning status.

    Args:
        customer_id: Google Ads customer ID
        start_date: YYYY-MM-DD (default: last 30 days)
        end_date: YYYY-MM-DD (default: yesterday)
    """
    today = date.today()
    start = start_date or (today - timedelta(days=30)).strftime("%Y-%m-%d")
    end = end_date or (today - timedelta(days=1)).strftime("%Y-%m-%d")

    gaql = f"""
        SELECT campaign.id, campaign.name, campaign.bidding_strategy_type,
               campaign.target_roas.target_roas,
               campaign.target_cpa.target_cpa_micros,
               campaign.maximize_conversion_value.target_roas,
               campaign.maximize_conversions.target_cpa_micros,
               metrics.cost_micros, metrics.conversions, metrics.conversions_value,
               metrics.clicks, metrics.impressions
        FROM campaign
        WHERE segments.date BETWEEN '{start}' AND '{end}'
          AND campaign.status = 'ENABLED'
          AND campaign.bidding_strategy_type IN (
            'TARGET_ROAS', 'TARGET_CPA', 'MAXIMIZE_CONVERSION_VALUE', 'MAXIMIZE_CONVERSIONS'
          )
        ORDER BY metrics.cost_micros DESC
        LIMIT 50
    """
    rows = _search(gaql, customer_id)
    campaigns = []
    for row in rows:
        c = row.campaign
        m = row.metrics
        spend = m.cost_micros / 1_000_000
        actual_roas = round(m.conversions_value / max(spend, 0.01), 2)
        actual_cpa = round(spend / max(m.conversions, 0.001), 2)
        strategy = c.bidding_strategy_type.name if hasattr(c.bidding_strategy_type, "name") else str(c.bidding_strategy_type)

        target_roas = None
        target_cpa = None
        if "ROAS" in strategy:
            target_roas = c.target_roas.target_roas or c.maximize_conversion_value.target_roas
        if "CPA" in strategy:
            t = c.target_cpa.target_cpa_micros or c.maximize_conversions.target_cpa_micros
            target_cpa = round(t / 1_000_000, 2) if t else None

        campaigns.append({
            "campaign_id": str(c.id),
            "campaign_name": c.name,
            "strategy": strategy,
            "target_roas": target_roas,
            "actual_roas": actual_roas,
            "target_cpa": target_cpa,
            "actual_cpa": actual_cpa,
            "spend": round(spend, 2),
            "conversions": round(m.conversions, 2),
            "clicks": m.clicks,
        })
    return {"smart_bidding_campaigns": campaigns, "start": start, "end": end}


# ---------------------------------------------------------------------------
# Shopping product groups
# ---------------------------------------------------------------------------

@mcp.tool()
def google_ads_shopping_product_groups(
    customer_id: str,
    campaign_id: str = "",
    start_date: str = None,
    end_date: str = None,
) -> dict:
    """
    Return shopping product groups (listing groups) with performance metrics.

    Args:
        customer_id: Google Ads customer ID
        campaign_id: Optional Shopping campaign ID to filter
        start_date: YYYY-MM-DD (default: 30 days ago)
        end_date: YYYY-MM-DD (default: yesterday)
    """
    today = date.today()
    start = start_date or (today - timedelta(days=30)).strftime("%Y-%m-%d")
    end = end_date or (today - timedelta(days=1)).strftime("%Y-%m-%d")
    where = f"AND campaign.id = {campaign_id}" if campaign_id else ""

    gaql = f"""
        SELECT ad_group_criterion.listing_group.type,
               ad_group_criterion.listing_group.case_value.product_brand.value,
               ad_group_criterion.listing_group.case_value.product_type.value,
               ad_group_criterion.listing_group.case_value.product_category.category_id,
               ad_group_criterion.cpc_bid_micros,
               ad_group_criterion.status,
               campaign.id, campaign.name,
               metrics.clicks, metrics.impressions, metrics.cost_micros,
               metrics.conversions, metrics.conversions_value
        FROM ad_group_criterion
        WHERE segments.date BETWEEN '{start}' AND '{end}'
          AND ad_group_criterion.type = 'LISTING_GROUP'
          AND ad_group_criterion.status != 'REMOVED'
          AND campaign.advertising_channel_type = 'SHOPPING'
          {where}
        ORDER BY metrics.cost_micros DESC
        LIMIT 200
    """
    rows = _search(gaql, customer_id)
    groups = []
    for row in rows:
        crit = row.ad_group_criterion
        lg = crit.listing_group
        m = row.metrics
        spend = m.cost_micros / 1_000_000
        groups.append({
            "type": lg.type_.name if hasattr(lg.type_, "name") else str(lg.type_),
            "brand": lg.case_value.product_brand.value,
            "product_type": lg.case_value.product_type.value,
            "cpc_bid": round(crit.cpc_bid_micros / 1_000_000, 2),
            "campaign": row.campaign.name,
            "clicks": m.clicks,
            "impressions": m.impressions,
            "cost": round(spend, 2),
            "conversions": round(m.conversions, 2),
            "roas": round(m.conversions_value / max(spend, 0.01), 2),
        })
    return {"product_groups": groups, "count": len(groups), "start": start, "end": end}


# ---------------------------------------------------------------------------
# Dynamic search ads targets
# ---------------------------------------------------------------------------

@mcp.tool()
def google_ads_dynamic_search_targets(
    customer_id: str,
    campaign_id: str = "",
) -> dict:
    """
    List dynamic search ad (DSA) targets — webpage conditions targeting specific page categories or URLs.

    Args:
        customer_id: Google Ads customer ID
        campaign_id: Optional DSA campaign ID to filter
    """
    where = f"AND campaign.id = {campaign_id}" if campaign_id else ""
    gaql = f"""
        SELECT ad_group_criterion.criterion_id,
               ad_group_criterion.webpage.criterion_name,
               ad_group_criterion.webpage.conditions,
               ad_group_criterion.cpc_bid_micros,
               ad_group_criterion.status,
               ad_group.id, ad_group.name, campaign.name
        FROM ad_group_criterion
        WHERE ad_group_criterion.type = 'WEBPAGE'
          AND ad_group_criterion.status != 'REMOVED'
          {where}
        LIMIT 200
    """
    rows = _search(gaql, customer_id)
    targets = []
    for row in rows:
        crit = row.ad_group_criterion
        wp = crit.webpage
        targets.append({
            "criterion_id": str(crit.criterion_id),
            "name": wp.criterion_name,
            "conditions": [
                {
                    "operand": str(c.operand),
                    "argument": c.argument,
                }
                for c in wp.conditions
            ],
            "cpc_bid": round(crit.cpc_bid_micros / 1_000_000, 2),
            "status": crit.status.name if hasattr(crit.status, "name") else str(crit.status),
            "ad_group": row.ad_group.name,
            "campaign": row.campaign.name,
        })
    return {"dsa_targets": targets, "count": len(targets)}


# ---------------------------------------------------------------------------
# Brand safety exclusions (placement exclusions)
# ---------------------------------------------------------------------------

@mcp.tool()
def google_ads_brand_safety_exclusions(
    customer_id: str,
    level: str = "account",
) -> dict:
    """
    List brand safety placement exclusions at account or campaign level.

    Args:
        customer_id: Google Ads customer ID
        level: 'account' (customer-level exclusions) or 'campaign' (campaign-level)
    """
    if level == "account":
        gaql = """
            SELECT customer_negative_criterion.id,
                   customer_negative_criterion.type,
                   customer_negative_criterion.placement.url,
                   customer_negative_criterion.youtube_channel.channel_id,
                   customer_negative_criterion.youtube_video.video_id
            FROM customer_negative_criterion
            WHERE customer_negative_criterion.type IN ('PLACEMENT', 'YOUTUBE_CHANNEL', 'YOUTUBE_VIDEO')
            LIMIT 500
        """
        rows = _search(gaql, customer_id)
        exclusions = []
        for row in rows:
            nc = row.customer_negative_criterion
            exc_type = nc.type_.name if hasattr(nc.type_, "name") else str(nc.type_)
            exclusions.append({
                "id": str(nc.id),
                "type": exc_type,
                "placement_url": nc.placement.url,
                "youtube_channel": nc.youtube_channel.channel_id,
                "youtube_video": nc.youtube_video.video_id,
            })
    else:
        gaql = """
            SELECT campaign_criterion.criterion_id,
                   campaign_criterion.type,
                   campaign_criterion.placement.url,
                   campaign.name
            FROM campaign_criterion
            WHERE campaign_criterion.negative = true
              AND campaign_criterion.type = 'PLACEMENT'
              AND campaign.status != 'REMOVED'
            LIMIT 500
        """
        rows = _search(gaql, customer_id)
        exclusions = []
        for row in rows:
            cc = row.campaign_criterion
            exclusions.append({
                "criterion_id": str(cc.criterion_id),
                "type": "PLACEMENT",
                "placement_url": cc.placement.url,
                "campaign": row.campaign.name,
            })
    return {"brand_safety_exclusions": exclusions, "count": len(exclusions), "level": level}


# ---------------------------------------------------------------------------
# Add brand safety exclusion
# ---------------------------------------------------------------------------

@mcp.tool()
def google_ads_add_placement_exclusion(
    customer_id: str,
    placement_url: str,
    campaign_id: str = "",
) -> dict:
    """
    Add a placement URL exclusion for brand safety (at account or campaign level).

    Args:
        customer_id: Google Ads customer ID
        placement_url: URL to exclude (e.g. 'www.example.com')
        campaign_id: If provided, adds exclusion to that campaign only. If empty, adds to account level.
    """
    require_editor()
    client, cid = _get_client(customer_id)

    if campaign_id:
        svc = client.get_service("CampaignCriterionService")
        op = client.get_type("CampaignCriterionOperation")
        criterion = op.create
        criterion.campaign = client.get_service("CampaignService").campaign_path(cid, campaign_id)
        criterion.negative = True
        criterion.placement.url = placement_url
        resp = svc.mutate_campaign_criteria(customer_id=cid, operations=[op])
        return {"added": "campaign_level", "campaign_id": campaign_id, "url": placement_url, "resource": resp.results[0].resource_name}
    else:
        svc = client.get_service("CustomerNegativeCriterionService")
        op = client.get_type("CustomerNegativeCriterionOperation")
        criterion = op.create
        criterion.placement.url = placement_url
        resp = svc.mutate_customer_negative_criteria(customer_id=cid, operations=[op])
        return {"added": "account_level", "url": placement_url, "resource": resp.results[0].resource_name}


# ---------------------------------------------------------------------------
# Attribution paths (from conversion path report)
# ---------------------------------------------------------------------------

@mcp.tool()
def google_ads_attribution_paths(
    customer_id: str,
    start_date: str = None,
    end_date: str = None,
    limit: int = 50,
) -> dict:
    """
    Return top-of-funnel and cross-channel attribution paths showing which campaigns
    assist vs. close conversions.

    Args:
        customer_id: Google Ads customer ID
        start_date: YYYY-MM-DD (default: 30 days ago)
        end_date: YYYY-MM-DD (default: yesterday)
        limit: Max path rows (default: 50)
    """
    today = date.today()
    start = start_date or (today - timedelta(days=30)).strftime("%Y-%m-%d")
    end = end_date or (today - timedelta(days=1)).strftime("%Y-%m-%d")

    # Use click_view report to approximate attribution paths
    gaql = f"""
        SELECT campaign.id, campaign.name, campaign.advertising_channel_type,
               metrics.cost_micros, metrics.conversions, metrics.conversions_value,
               metrics.all_conversions, metrics.view_through_conversions
        FROM campaign
        WHERE segments.date BETWEEN '{start}' AND '{end}'
          AND campaign.status != 'REMOVED'
        ORDER BY metrics.all_conversions DESC
        LIMIT {limit or 50}
    """
    rows = _search(gaql, customer_id)
    paths = []
    for row in rows:
        c = row.campaign
        m = row.metrics
        spend = m.cost_micros / 1_000_000
        channel = c.advertising_channel_type.name if hasattr(c.advertising_channel_type, "name") else str(c.advertising_channel_type)
        paths.append({
            "campaign_id": str(c.id),
            "campaign_name": c.name,
            "channel_type": channel,
            "last_click_conversions": round(m.conversions, 2),
            "all_conversions": round(m.all_conversions, 2),
            "view_through_conversions": round(m.view_through_conversions, 2),
            "assist_ratio": round((m.all_conversions - m.conversions) / max(m.conversions, 0.01), 2),
            "revenue": round(m.conversions_value, 2),
            "spend": round(spend, 2),
        })
    return {"attribution_paths": paths, "start": start, "end": end}
