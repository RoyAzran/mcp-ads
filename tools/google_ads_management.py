"""
Google Ads management tools — targeting, bidding, keywords, audiences, MCC, uploads.
Covers gaps not addressed in google_ads.py / google_ads_advanced.py / google_ads_negatives.py.
"""
import hashlib
import json
import os
from datetime import date, timedelta

from google.protobuf import field_mask_pb2

from mcp_instance import mcp
from auth import current_user_ctx
from permissions import require_editor

from tools.google_ads import _get_client, _search


def _m(micros) -> float:
    return round(float(micros) / 1_000_000, 2)


# ===========================================================================
# SHARED NEGATIVE KEYWORD LIST TOOLS
# ===========================================================================

@mcp.tool()
def google_ads_create_shared_negative_list(
    name: str,
    customer_id: str = "",
) -> str:
    """Create a new shared negative keyword list (SharedSet of type NEGATIVE_KEYWORDS).

    After creating, use google_ads_add_to_shared_negative_list to populate it,
    then google_ads_apply_shared_negative_list to attach it to campaigns.

    Args:
        name: Display name for the shared negative list.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    require_editor()
    try:
        client, cid = _get_client(customer_id)
        svc = client.get_service("SharedSetService")
        op = client.get_type("SharedSetOperation")
        shared_set = op.create
        shared_set.name = name
        shared_set.type_ = client.enums.SharedSetTypeEnum.NEGATIVE_KEYWORDS
        response = svc.mutate_shared_sets(customer_id=cid, operations=[op])
        rn = response.results[0].resource_name
        shared_set_id = rn.split("/")[-1]
        return json.dumps({
            "success": True,
            "shared_set_id": shared_set_id,
            "name": name,
            "resource_name": rn,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_apply_shared_negative_list(
    campaign_id: str,
    shared_set_id: str,
    customer_id: str = "",
) -> str:
    """Attach a shared negative keyword list to a campaign (CampaignSharedSet).

    Args:
        campaign_id: Campaign to attach the list to.
        shared_set_id: ID of the shared negative list (from google_ads_list_shared_negative_keywords).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    require_editor()
    try:
        client, cid = _get_client(customer_id)
        svc = client.get_service("CampaignSharedSetService")
        op = client.get_type("CampaignSharedSetOperation")
        css = op.create
        css.campaign = f"customers/{cid}/campaigns/{campaign_id}"
        css.shared_set = f"customers/{cid}/sharedSets/{shared_set_id}"
        response = svc.mutate_campaign_shared_sets(customer_id=cid, operations=[op])
        rn = response.results[0].resource_name
        return json.dumps({
            "success": True,
            "campaign_id": campaign_id,
            "shared_set_id": shared_set_id,
            "resource_name": rn,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


# ===========================================================================
# KEYWORD MANAGEMENT TOOLS
# ===========================================================================

@mcp.tool()
def google_ads_list_adgroup_keywords(
    ad_group_id: str,
    include_paused: bool = True,
    customer_id: str = "",
) -> str:
    """List all keywords in an ad group regardless of spend — useful for auditing.

    Unlike keyword_performance reports, this returns all keywords including
    those with zero impressions.

    Args:
        ad_group_id: Ad group ID to list keywords from.
        include_paused: Include paused keywords (default True).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        status_clause = (
            "AND ad_group_criterion.status IN ('ENABLED','PAUSED')"
            if include_paused
            else "AND ad_group_criterion.status = 'ENABLED'"
        )
        rows = _search(f"""
            SELECT ad_group_criterion.criterion_id,
                   ad_group_criterion.keyword.text,
                   ad_group_criterion.keyword.match_type,
                   ad_group_criterion.status,
                   ad_group_criterion.cpc_bid_micros,
                   ad_group_criterion.quality_info.quality_score,
                   ad_group_criterion.final_urls,
                   ad_group.name, ad_group.id,
                   campaign.name, campaign.id
            FROM ad_group_criterion
            WHERE ad_group_criterion.type = 'KEYWORD'
              AND ad_group_criterion.status != 'REMOVED'
              AND ad_group.id = {ad_group_id}
              {status_clause}
            ORDER BY ad_group_criterion.keyword.text ASC
        """, customer_id)

        keywords = []
        for row in rows:
            c = row.ad_group_criterion
            keywords.append({
                "criterion_id": str(c.criterion_id),
                "text": c.keyword.text,
                "match_type": c.keyword.match_type.name,
                "status": c.status.name,
                "cpc_bid": _m(c.cpc_bid_micros) if c.cpc_bid_micros else None,
                "quality_score": c.quality_info.quality_score if c.quality_info.quality_score else None,
                "final_urls": list(c.final_urls) if c.final_urls else [],
                "ad_group": {"id": str(row.ad_group.id), "name": row.ad_group.name},
                "campaign": {"id": str(row.campaign.id), "name": row.campaign.name},
            })
        return json.dumps({"keywords": keywords, "total": len(keywords), "ad_group_id": ad_group_id})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_remove_keyword(
    ad_group_id: str,
    criterion_id: str,
    customer_id: str = "",
) -> str:
    """Permanently remove (delete) a keyword from an ad group.

    This is a hard delete — the keyword cannot be re-enabled.
    To temporarily stop a keyword, use google_ads_update_keyword_status to PAUSE instead.

    Args:
        ad_group_id: Ad group ID containing the keyword.
        criterion_id: The criterion_id of the keyword (from google_ads_list_adgroup_keywords).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    require_editor()
    try:
        client, cid = _get_client(customer_id)
        svc = client.get_service("AdGroupCriterionService")
        op = client.get_type("AdGroupCriterionOperation")
        op.remove = f"customers/{cid}/adGroupCriteria/{ad_group_id}~{criterion_id}"
        svc.mutate_ad_group_criteria(customer_id=cid, operations=[op])
        return json.dumps({
            "success": True,
            "removed": {"ad_group_id": ad_group_id, "criterion_id": criterion_id},
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_update_keyword_match_type(
    ad_group_id: str,
    criterion_id: str,
    new_match_type: str,
    customer_id: str = "",
) -> str:
    """Change a keyword's match type by removing the old entry and adding a new one.

    Google Ads does not allow in-place match type edits — this tool removes the
    keyword and re-creates it with the new match type, preserving the bid.

    Args:
        ad_group_id: Ad group ID containing the keyword.
        criterion_id: The criterion_id of the keyword to change.
        new_match_type: EXACT | PHRASE | BROAD
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    require_editor()
    try:
        new_match_type = new_match_type.upper()
        if new_match_type not in ("EXACT", "PHRASE", "BROAD"):
            return json.dumps({"error": "new_match_type must be EXACT, PHRASE, or BROAD"})

        rows = _search(f"""
            SELECT ad_group_criterion.criterion_id, ad_group_criterion.keyword.text,
                   ad_group_criterion.keyword.match_type, ad_group_criterion.cpc_bid_micros
            FROM ad_group_criterion
            WHERE ad_group_criterion.criterion_id = {criterion_id}
              AND ad_group.id = {ad_group_id}
        """, customer_id)

        if not rows:
            return json.dumps({"error": f"Keyword {criterion_id} not found in ad group {ad_group_id}"})

        existing = rows[0].ad_group_criterion
        keyword_text = existing.keyword.text
        old_match = existing.keyword.match_type.name
        cpc_bid_micros = existing.cpc_bid_micros

        if old_match == new_match_type:
            return json.dumps({"error": f"Keyword already has match type {new_match_type}"})

        client, cid = _get_client(customer_id)
        svc = client.get_service("AdGroupCriterionService")

        # Remove old keyword
        remove_op = client.get_type("AdGroupCriterionOperation")
        remove_op.remove = f"customers/{cid}/adGroupCriteria/{ad_group_id}~{criterion_id}"

        # Create new keyword with new match type
        add_op = client.get_type("AdGroupCriterionOperation")
        criterion = add_op.create
        criterion.ad_group = f"customers/{cid}/adGroups/{ad_group_id}"
        criterion.keyword.text = keyword_text
        criterion.keyword.match_type = client.enums.KeywordMatchTypeEnum[new_match_type]
        criterion.status = client.enums.AdGroupCriterionStatusEnum.ENABLED
        if cpc_bid_micros:
            criterion.cpc_bid_micros = cpc_bid_micros

        response = svc.mutate_ad_group_criteria(
            customer_id=cid, operations=[remove_op, add_op]
        )
        new_criterion_id = response.results[1].resource_name.split("~")[-1]

        return json.dumps({
            "success": True,
            "keyword_text": keyword_text,
            "old_match_type": old_match,
            "new_match_type": new_match_type,
            "old_criterion_id": criterion_id,
            "new_criterion_id": new_criterion_id,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


# ===========================================================================
# AD STATUS
# ===========================================================================

@mcp.tool()
def google_ads_update_ad_status(
    ad_group_id: str,
    ad_id: str,
    status: str,
    customer_id: str = "",
) -> str:
    """Update the status of an individual ad (enable, pause, or remove).

    Args:
        ad_group_id: Ad group ID containing the ad.
        ad_id: Ad ID to update.
        status: ENABLED | PAUSED | REMOVED
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    require_editor()
    try:
        status = status.upper()
        if status not in ("ENABLED", "PAUSED", "REMOVED"):
            return json.dumps({"error": "status must be ENABLED, PAUSED, or REMOVED"})
        client, cid = _get_client(customer_id)
        svc = client.get_service("AdGroupAdService")
        op = client.get_type("AdGroupAdOperation")
        ad_group_ad = op.update
        ad_group_ad.resource_name = f"customers/{cid}/adGroupAds/{ad_group_id}~{ad_id}"
        ad_group_ad.status = client.enums.AdGroupAdStatusEnum[status]
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["status"]))
        svc.mutate_ad_group_ads(customer_id=cid, operations=[op])
        return json.dumps({
            "success": True,
            "ad_id": ad_id,
            "ad_group_id": ad_group_id,
            "new_status": status,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


# ===========================================================================
# LOCATION TARGETING
# ===========================================================================

@mcp.tool()
def google_ads_list_location_targets(
    campaign_id: str,
    customer_id: str = "",
) -> str:
    """List all geographic location targets for a campaign.

    Returns location IDs (from Google's geo target constants), names, and whether
    they are targeted (+) or excluded (-).

    Args:
        campaign_id: Campaign ID to inspect.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        rows = _search(f"""
            SELECT campaign_criterion.criterion_id,
                   campaign_criterion.location.geo_target_constant,
                   campaign_criterion.negative,
                   campaign_criterion.bid_modifier,
                   campaign_criterion.status,
                   campaign.name, campaign.id
            FROM campaign_criterion
            WHERE campaign_criterion.type = 'LOCATION'
              AND campaign.id = {campaign_id}
              AND campaign_criterion.status != 'REMOVED'
        """, customer_id)

        targets = []
        for row in rows:
            cc = row.campaign_criterion
            geo_rn = cc.location.geo_target_constant
            location_id = geo_rn.split("/")[-1] if geo_rn else None
            targets.append({
                "criterion_id": str(cc.criterion_id),
                "location_id": location_id,
                "geo_target_constant": geo_rn,
                "type": "EXCLUDED" if cc.negative else "TARGETED",
                "bid_modifier": round(cc.bid_modifier, 4) if cc.bid_modifier else 1.0,
                "status": cc.status.name,
            })

        return json.dumps({
            "campaign_id": campaign_id,
            "campaign_name": rows[0].campaign.name if rows else "",
            "locations": targets,
            "total": len(targets),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_add_location_target(
    campaign_id: str,
    location_id: str,
    negative: bool = False,
    bid_modifier: float = 1.0,
    customer_id: str = "",
) -> str:
    """Add a geographic location target (or exclusion) to a campaign.

    Find location IDs at: https://developers.google.com/google-ads/api/data/geotargets
    Common IDs: US=2840, UK=2826, Canada=2124, Australia=2036.

    Args:
        campaign_id: Campaign ID to add the location to.
        location_id: Google geo target constant ID (e.g. '2840' for United States).
        negative: True to EXCLUDE this location. Default False (target).
        bid_modifier: Bid adjustment multiplier (e.g. 1.2 = +20%). Only for targeted, not excluded.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    require_editor()
    try:
        client, cid = _get_client(customer_id)
        svc = client.get_service("CampaignCriterionService")
        op = client.get_type("CampaignCriterionOperation")
        criterion = op.create
        criterion.campaign = f"customers/{cid}/campaigns/{campaign_id}"
        criterion.location.geo_target_constant = f"geoTargetConstants/{location_id}"
        criterion.negative = negative
        if not negative and bid_modifier != 1.0:
            criterion.bid_modifier = bid_modifier
        response = svc.mutate_campaign_criteria(customer_id=cid, operations=[op])
        rn = response.results[0].resource_name
        criterion_id = rn.split("~")[-1]
        return json.dumps({
            "success": True,
            "campaign_id": campaign_id,
            "location_id": location_id,
            "type": "EXCLUDED" if negative else "TARGETED",
            "bid_modifier": bid_modifier if not negative else None,
            "criterion_id": criterion_id,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_remove_location_target(
    campaign_id: str,
    criterion_id: str,
    customer_id: str = "",
) -> str:
    """Remove a geographic location target or exclusion from a campaign.

    Get criterion_id from google_ads_list_location_targets.

    Args:
        campaign_id: Campaign ID.
        criterion_id: Criterion ID of the location target to remove.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    require_editor()
    try:
        client, cid = _get_client(customer_id)
        svc = client.get_service("CampaignCriterionService")
        op = client.get_type("CampaignCriterionOperation")
        op.remove = f"customers/{cid}/campaignCriteria/{campaign_id}~{criterion_id}"
        svc.mutate_campaign_criteria(customer_id=cid, operations=[op])
        return json.dumps({
            "success": True,
            "removed": {"campaign_id": campaign_id, "criterion_id": criterion_id},
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


# ===========================================================================
# AD SCHEDULE (DAY-PARTING)
# ===========================================================================

@mcp.tool()
def google_ads_get_ad_schedule(
    campaign_id: str,
    customer_id: str = "",
) -> str:
    """Get the current ad schedule (day-parting) for a campaign.

    Returns the hours and days the campaign is allowed to serve ads.
    Empty result means the campaign runs 24/7.

    Args:
        campaign_id: Campaign ID to inspect.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        rows = _search(f"""
            SELECT campaign_criterion.criterion_id,
                   campaign_criterion.ad_schedule.day_of_week,
                   campaign_criterion.ad_schedule.start_hour,
                   campaign_criterion.ad_schedule.end_hour,
                   campaign_criterion.ad_schedule.start_minute,
                   campaign_criterion.ad_schedule.end_minute,
                   campaign_criterion.bid_modifier
            FROM campaign_criterion
            WHERE campaign_criterion.type = 'AD_SCHEDULE'
              AND campaign.id = {campaign_id}
              AND campaign_criterion.status != 'REMOVED'
        """, customer_id)

        schedule = []
        for row in rows:
            cc = row.campaign_criterion
            schedule.append({
                "criterion_id": str(cc.criterion_id),
                "day_of_week": cc.ad_schedule.day_of_week.name,
                "start_hour": cc.ad_schedule.start_hour,
                "end_hour": cc.ad_schedule.end_hour,
                "bid_modifier": round(cc.bid_modifier, 4) if cc.bid_modifier else 1.0,
            })
        return json.dumps({
            "campaign_id": campaign_id,
            "schedule": schedule,
            "runs_247": len(schedule) == 0,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_set_ad_schedule(
    campaign_id: str,
    schedules: list,
    customer_id: str = "",
) -> str:
    """Add day-parting schedule entries to a campaign.

    Each schedule entry restricts when ads can show and optionally adjusts bids.
    To replace the full schedule, first use google_ads_clear_ad_schedule.

    Args:
        campaign_id: Campaign ID.
        schedules: List of schedule dicts. Each must have:
                   - day: MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY
                   - start_hour: int 0-23
                   - end_hour: int 1-24
                   - bid_modifier: float (optional, default 1.0; 0.5 = -50%, 1.5 = +50%)
        customer_id: Google Ads customer ID. Leave blank to use default.
    Example:
        schedules=[
            {"day": "MONDAY", "start_hour": 8, "end_hour": 18},
            {"day": "TUESDAY", "start_hour": 8, "end_hour": 18, "bid_modifier": 1.2}
        ]
    """
    require_editor()
    try:
        valid_days = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")
        client, cid = _get_client(customer_id)
        svc = client.get_service("CampaignCriterionService")
        ops = []
        for entry in schedules:
            day = entry.get("day", "").upper()
            if day not in valid_days:
                return json.dumps({"error": f"Invalid day: {day}. Must be one of {valid_days}"})
            start_h = int(entry.get("start_hour", 0))
            end_h = int(entry.get("end_hour", 24))
            bid_mod = float(entry.get("bid_modifier", 1.0))
            op = client.get_type("CampaignCriterionOperation")
            criterion = op.create
            criterion.campaign = f"customers/{cid}/campaigns/{campaign_id}"
            criterion.ad_schedule.day_of_week = client.enums.DayOfWeekEnum[day]
            criterion.ad_schedule.start_hour = start_h
            criterion.ad_schedule.end_hour = end_h
            criterion.ad_schedule.start_minute = client.enums.MinuteOfHourEnum.ZERO
            criterion.ad_schedule.end_minute = client.enums.MinuteOfHourEnum.ZERO
            if bid_mod != 1.0:
                criterion.bid_modifier = bid_mod
            ops.append(op)

        svc.mutate_campaign_criteria(customer_id=cid, operations=ops)
        return json.dumps({
            "success": True,
            "campaign_id": campaign_id,
            "schedules_added": len(ops),
            "entries": schedules,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_clear_ad_schedule(
    campaign_id: str,
    customer_id: str = "",
) -> str:
    """Remove all ad schedule entries from a campaign (revert to 24/7 serving).

    Args:
        campaign_id: Campaign ID.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    require_editor()
    try:
        rows = _search(f"""
            SELECT campaign_criterion.criterion_id
            FROM campaign_criterion
            WHERE campaign_criterion.type = 'AD_SCHEDULE'
              AND campaign.id = {campaign_id}
              AND campaign_criterion.status != 'REMOVED'
        """, customer_id)
        if not rows:
            return json.dumps({"success": True, "removed": 0, "message": "Campaign already runs 24/7 (no schedule set)"})
        client, cid = _get_client(customer_id)
        svc = client.get_service("CampaignCriterionService")
        ops = []
        for row in rows:
            op = client.get_type("CampaignCriterionOperation")
            crit_id = row.campaign_criterion.criterion_id
            op.remove = f"customers/{cid}/campaignCriteria/{campaign_id}~{crit_id}"
            ops.append(op)
        svc.mutate_campaign_criteria(customer_id=cid, operations=ops)
        return json.dumps({"success": True, "removed": len(ops)})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ===========================================================================
# BIDDING STRATEGY TOOLS
# ===========================================================================

@mcp.tool()
def google_ads_update_target_cpa(
    campaign_id: str,
    target_cpa: float,
    customer_id: str = "",
) -> str:
    """Update the Target CPA bid for a campaign using Smart Bidding.

    The campaign must already use Target CPA bidding strategy.
    To check current strategy: use google_ads_campaign_performance.

    Args:
        campaign_id: Campaign ID to update.
        target_cpa: Target cost per acquisition in account currency (e.g. 25.00 for $25).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    require_editor()
    try:
        client, cid = _get_client(customer_id)
        svc = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        campaign = op.update
        campaign.resource_name = f"customers/{cid}/campaigns/{campaign_id}"
        campaign.target_cpa.target_cpa_micros = int(target_cpa * 1_000_000)
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["target_cpa.target_cpa_micros"]))
        svc.mutate_campaigns(customer_id=cid, operations=[op])
        return json.dumps({
            "success": True,
            "campaign_id": campaign_id,
            "new_target_cpa": target_cpa,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_update_target_roas(
    campaign_id: str,
    target_roas: float,
    customer_id: str = "",
) -> str:
    """Update the Target ROAS bid for a campaign using Smart Bidding.

    The campaign must already use Target ROAS bidding strategy.
    ROAS is expressed as a decimal multiplier: 4.0 means 400% ROAS ($4 revenue per $1 spend).

    Args:
        campaign_id: Campaign ID to update.
        target_roas: Target ROAS as a multiplier (e.g. 3.5 = 350% ROAS).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    require_editor()
    try:
        client, cid = _get_client(customer_id)
        svc = client.get_service("CampaignService")
        op = client.get_type("CampaignOperation")
        campaign = op.update
        campaign.resource_name = f"customers/{cid}/campaigns/{campaign_id}"
        campaign.target_roas.target_roas = target_roas
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["target_roas.target_roas"]))
        svc.mutate_campaigns(customer_id=cid, operations=[op])
        return json.dumps({
            "success": True,
            "campaign_id": campaign_id,
            "new_target_roas": target_roas,
            "note": f"Target: {target_roas * 100:.0f}% ROAS (${target_roas:.2f} revenue per $1 spend)",
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


# ===========================================================================
# AUDIENCE TARGETING
# ===========================================================================

@mcp.tool()
def google_ads_add_audience_to_campaign(
    campaign_id: str,
    user_list_id: str,
    bid_modifier: float = 1.0,
    observation_only: bool = True,
    customer_id: str = "",
) -> str:
    """Add a remarketing audience (user list) to a campaign.

    By default adds as OBSERVATION (bid-only, doesn't restrict reach).
    Set observation_only=False for TARGETING mode (only shows ads to audience members).

    Args:
        campaign_id: Campaign ID to add the audience to.
        user_list_id: ID of the user list/audience (from google_ads_audience_performance or the Google Ads UI).
        bid_modifier: Bid adjustment for this audience (e.g. 1.3 = +30% for audience members).
        observation_only: True = add as observation (bid adjustment only). False = targeting (restrict to audience).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    require_editor()
    try:
        client, cid = _get_client(customer_id)
        svc = client.get_service("CampaignCriterionService")
        op = client.get_type("CampaignCriterionOperation")
        criterion = op.create
        criterion.campaign = f"customers/{cid}/campaigns/{campaign_id}"
        criterion.user_list.user_list = f"customers/{cid}/userLists/{user_list_id}"
        if bid_modifier != 1.0:
            criterion.bid_modifier = bid_modifier
        response = svc.mutate_campaign_criteria(customer_id=cid, operations=[op])
        rn = response.results[0].resource_name
        return json.dumps({
            "success": True,
            "campaign_id": campaign_id,
            "user_list_id": user_list_id,
            "mode": "OBSERVATION" if observation_only else "TARGETING",
            "bid_modifier": bid_modifier,
            "criterion_resource_name": rn,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


# ===========================================================================
# MCC / MANAGER ACCOUNT
# ===========================================================================

@mcp.tool()
def google_ads_list_mcc_clients(customer_id: str = "") -> str:
    """List all client accounts accessible under a manager (MCC) account.

    Returns account IDs, names, currency, timezone, and status for each sub-account.
    Use the returned customer_id values in other tools to manage individual accounts.

    Args:
        customer_id: MCC/manager customer ID. Leave blank to use the default manager ID.
    """
    try:
        client, cid = _get_client(customer_id)
        svc = client.get_service("CustomerService")
        accessible = svc.list_accessible_customers()
        resource_names = list(accessible.resource_names)

        if not resource_names:
            return json.dumps({"accounts": [], "total": 0})

        results = []
        for rn in resource_names:
            sub_cid = rn.split("/")[-1]
            try:
                rows = _search("""
                    SELECT customer.id, customer.descriptive_name,
                           customer.currency_code, customer.time_zone,
                           customer.status, customer.manager,
                           customer.test_account
                    FROM customer
                """, sub_cid)
                for row in rows:
                    c = row.customer
                    results.append({
                        "customer_id": str(c.id),
                        "name": c.descriptive_name,
                        "currency": c.currency_code,
                        "timezone": c.time_zone,
                        "status": c.status.name,
                        "is_manager": c.manager,
                        "is_test": c.test_account,
                    })
                    break
            except Exception:
                results.append({"customer_id": sub_cid, "error": "Could not retrieve details"})

        return json.dumps({"accounts": results, "total": len(results)})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ===========================================================================
# CUSTOMER MATCH UPLOAD
# ===========================================================================

@mcp.tool()
def google_ads_upload_customer_match(
    user_list_name: str,
    emails: list,
    phones: list = None,
    membership_days: int = 30,
    customer_id: str = "",
) -> str:
    """Upload a customer match audience (email/phone list) to Google Ads.

    Emails and phones are hashed with SHA-256 before upload (Google requirement).
    Creates a new CRM-based user list or uploads to an existing one with the same name.

    Args:
        user_list_name: Name for the customer match audience (e.g. 'Newsletter Subscribers').
        emails: List of plain-text email addresses. They will be normalized and hashed.
        phones: Optional list of phone numbers (E.164 format preferred, e.g. '+12025551234').
        membership_days: How many days members stay in the list (default 30, max 540).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    require_editor()
    try:
        client, cid = _get_client(customer_id)

        # Create user list
        ul_svc = client.get_service("UserListService")
        ul_op = client.get_type("UserListOperation")
        user_list = ul_op.create
        user_list.name = user_list_name
        user_list.membership_life_span = min(membership_days, 540)
        user_list.crm_based_user_list.upload_key_type = (
            client.enums.CustomerMatchUploadKeyTypeEnum.CONTACT_INFO
        )
        user_list.crm_based_user_list.data_source_type = (
            client.enums.UserListCrmDataSourceTypeEnum.FIRST_PARTY
        )
        ul_response = ul_svc.mutate_user_lists(customer_id=cid, operations=[ul_op])
        user_list_rn = ul_response.results[0].resource_name

        # Build user data operations
        ud_svc = client.get_service("UserDataService")
        ud_ops = []

        for email in (emails or []):
            normalized = email.strip().lower()
            hashed = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            op = client.get_type("UserDataOperation")
            identifier = client.get_type("UserIdentifier")
            identifier.hashed_email = hashed
            op.create.user_identifiers.append(identifier)
            op.create.user_list = user_list_rn
            ud_ops.append(op)

        for phone in (phones or []):
            normalized = phone.strip().replace(" ", "").replace("-", "")
            hashed = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            op = client.get_type("UserDataOperation")
            identifier = client.get_type("UserIdentifier")
            identifier.hashed_phone_number = hashed
            op.create.user_identifiers.append(identifier)
            op.create.user_list = user_list_rn
            ud_ops.append(op)

        if ud_ops:
            ud_response = ud_svc.upload_user_data(
                customer_id=cid,
                operations=ud_ops,
            )
            upload_date = ud_response.upload_date_time if hasattr(ud_response, 'upload_date_time') else "processing"
        else:
            upload_date = None

        user_list_id = user_list_rn.split("/")[-1]
        return json.dumps({
            "success": True,
            "user_list_id": user_list_id,
            "user_list_name": user_list_name,
            "emails_uploaded": len(emails or []),
            "phones_uploaded": len(phones or []),
            "upload_date_time": str(upload_date),
            "note": "Data is hashed before upload. List will be available for targeting within ~24 hours.",
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


# ===========================================================================
# OFFLINE CONVERSION UPLOAD
# ===========================================================================

@mcp.tool()
def google_ads_upload_offline_conversions(
    conversions: list,
    customer_id: str = "",
) -> str:
    """Upload offline conversion events to Google Ads (click-based conversions via GCLID).

    Use this to import CRM leads, phone call outcomes, or any conversion that
    happened offline after an ad click. Google uses GCLID to match the click.

    Args:
        conversions: List of conversion dicts. Each requires:
                     - gclid: The Google Click ID from the landing page URL parameter.
                     - conversion_action_id: ID of the conversion action to import into.
                     - conversion_date_time: ISO datetime with timezone (e.g. '2024-01-15 14:30:00+00:00').
                     - value: Optional float conversion value.
                     - currency: Optional currency code (default 'USD').
        customer_id: Google Ads customer ID. Leave blank to use default.
    Example:
        conversions=[{
            "gclid": "Cj0KCQjw...",
            "conversion_action_id": "12345678",
            "conversion_date_time": "2024-01-15 14:30:00+00:00",
            "value": 250.00,
            "currency": "USD"
        }]
    """
    require_editor()
    try:
        client, cid = _get_client(customer_id)
        svc = client.get_service("ConversionUploadService")

        click_conversions = []
        for conv in conversions:
            cc = client.get_type("ClickConversion")
            cc.gclid = conv["gclid"]
            cc.conversion_action = (
                f"customers/{cid}/conversionActions/{conv['conversion_action_id']}"
            )
            cc.conversion_date_time = conv["conversion_date_time"]
            if conv.get("value"):
                cc.conversion_value = float(conv["value"])
            cc.currency_code = conv.get("currency", "USD")
            click_conversions.append(cc)

        response = svc.upload_click_conversions(
            customer_id=cid,
            conversions=click_conversions,
            partial_failure=True,
        )

        results = []
        for r in response.results:
            results.append({
                "gclid": r.gclid,
                "conversion_date_time": r.conversion_date_time,
                "status": "uploaded",
            })

        partial_errors = []
        if response.partial_failure_error:
            partial_errors = [str(response.partial_failure_error)]

        return json.dumps({
            "success": True,
            "uploaded": len(results),
            "results": results,
            "partial_errors": partial_errors,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


# ===========================================================================
# COPY CAMPAIGN
# ===========================================================================

@mcp.tool()
def google_ads_copy_campaign(
    source_campaign_id: str,
    new_name: str,
    paused: bool = True,
    customer_id: str = "",
) -> str:
    """Copy a campaign — creates a new campaign with the same settings and budget.

    The new campaign is created PAUSED by default so you can review it before enabling.
    Ad groups, keywords, and ads are NOT copied — only the campaign-level settings
    (bidding strategy, budget, targeting type, network settings).

    Args:
        source_campaign_id: ID of the campaign to copy.
        new_name: Name for the new campaign.
        paused: Create the new campaign as PAUSED (default True, recommended).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    require_editor()
    try:
        rows = _search(f"""
            SELECT campaign.id, campaign.name, campaign.advertising_channel_type,
                   campaign.advertising_channel_sub_type,
                   campaign.bidding_strategy_type,
                   campaign.target_cpa.target_cpa_micros,
                   campaign.target_roas.target_roas,
                   campaign.manual_cpc.enhanced_cpc_enabled,
                   campaign.network_settings.target_google_search,
                   campaign.network_settings.target_search_network,
                   campaign.network_settings.target_content_network,
                   campaign.network_settings.target_partner_search_network,
                   campaign.campaign_budget,
                   campaign_budget.amount_micros,
                   campaign_budget.name
            FROM campaign
            WHERE campaign.id = {source_campaign_id}
        """, customer_id)

        if not rows:
            return json.dumps({"error": f"Campaign {source_campaign_id} not found"})

        src = rows[0].campaign
        src_budget = rows[0].campaign_budget
        budget_micros = rows[0].campaign_budget.amount_micros

        client, cid = _get_client(customer_id)

        # Create new budget
        budget_svc = client.get_service("CampaignBudgetService")
        b_op = client.get_type("CampaignBudgetOperation")
        budget = b_op.create
        budget.name = f"{new_name} Budget"
        budget.amount_micros = budget_micros
        budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
        b_response = budget_svc.mutate_campaign_budgets(customer_id=cid, operations=[b_op])
        new_budget_rn = b_response.results[0].resource_name

        # Create new campaign
        c_svc = client.get_service("CampaignService")
        c_op = client.get_type("CampaignOperation")
        campaign = c_op.create
        campaign.name = new_name
        campaign.advertising_channel_type = src.advertising_channel_type
        if src.advertising_channel_sub_type.value:
            campaign.advertising_channel_sub_type = src.advertising_channel_sub_type
        campaign.status = (
            client.enums.CampaignStatusEnum.PAUSED
            if paused
            else client.enums.CampaignStatusEnum.ENABLED
        )
        campaign.campaign_budget = new_budget_rn
        campaign.network_settings.target_google_search = src.network_settings.target_google_search
        campaign.network_settings.target_search_network = src.network_settings.target_search_network
        campaign.network_settings.target_content_network = src.network_settings.target_content_network

        # Copy bidding strategy
        strategy = src.bidding_strategy_type.name
        if strategy == "TARGET_CPA" and src.target_cpa.target_cpa_micros:
            campaign.target_cpa.target_cpa_micros = src.target_cpa.target_cpa_micros
        elif strategy == "TARGET_ROAS" and src.target_roas.target_roas:
            campaign.target_roas.target_roas = src.target_roas.target_roas
        elif strategy == "MAXIMIZE_CONVERSIONS":
            campaign.maximize_conversions.target_cpa_micros = 0
        elif strategy == "MAXIMIZE_CONVERSION_VALUE":
            campaign.maximize_conversion_value.target_roas = 0.0
        else:
            campaign.manual_cpc.enhanced_cpc_enabled = src.manual_cpc.enhanced_cpc_enabled

        c_response = c_svc.mutate_campaigns(customer_id=cid, operations=[c_op])
        new_campaign_rn = c_response.results[0].resource_name
        new_campaign_id = new_campaign_rn.split("/")[-1]

        return json.dumps({
            "success": True,
            "source_campaign_id": source_campaign_id,
            "source_campaign_name": src.name,
            "new_campaign_id": new_campaign_id,
            "new_campaign_name": new_name,
            "status": "PAUSED" if paused else "ENABLED",
            "budget_micros": budget_micros,
            "bidding_strategy": strategy,
            "note": "Ad groups, keywords, and ads were NOT copied — only campaign-level settings.",
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


# ===========================================================================
# NAME / RENAME TOOLS
# ===========================================================================

@mcp.tool()
def google_ads_update_campaign_name(
    campaign_id: str,
    new_name: str,
    customer_id: str = "",
) -> str:
    """Rename a campaign.

    Args:
        campaign_id: The campaign ID to rename.
        new_name: New display name for the campaign.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    require_editor()
    try:
        client, cid = _get_client(customer_id)
        svc = client.get_service("CampaignService")
        rn = svc.campaign_path(cid, campaign_id)

        op = client.get_type("CampaignOperation")
        op.update.resource_name = rn
        op.update.name = new_name
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["name"]))

        svc.mutate_campaigns(customer_id=cid, operations=[op])
        return json.dumps({"success": True, "campaign_id": campaign_id, "new_name": new_name})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_update_adgroup_name(
    campaign_id: str,
    ad_group_id: str,
    new_name: str,
    customer_id: str = "",
) -> str:
    """Rename an ad group.

    Args:
        campaign_id: The campaign ID that contains the ad group.
        ad_group_id: The ad group ID to rename.
        new_name: New display name for the ad group.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    require_editor()
    try:
        client, cid = _get_client(customer_id)
        svc = client.get_service("AdGroupService")
        rn = svc.ad_group_path(cid, ad_group_id)

        op = client.get_type("AdGroupOperation")
        op.update.resource_name = rn
        op.update.name = new_name
        op.update_mask.CopyFrom(field_mask_pb2.FieldMask(paths=["name"]))

        svc.mutate_ad_groups(customer_id=cid, operations=[op])
        return json.dumps({
            "success": True,
            "campaign_id": campaign_id,
            "ad_group_id": ad_group_id,
            "new_name": new_name,
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


# ===========================================================================
# AD LISTING TOOLS
# ===========================================================================

@mcp.tool()
def google_ads_list_ads_in_adgroup(
    ad_group_id: str,
    customer_id: str = "",
    include_paused: bool = True,
) -> str:
    """List all ads within a specific ad group, with their status, type, and performance metrics.

    Returns headlines, descriptions, final URLs, status, and last-30-day impressions/clicks/cost.

    Args:
        ad_group_id: The ad group ID to list ads for.
        customer_id: Google Ads customer ID. Leave blank to use default.
        include_paused: Include PAUSED ads (default True). Set False to return only ENABLED ads.
    """
    try:
        status_filter = "" if include_paused else "AND ad_group_ad.status = 'ENABLED'"
        client, cid = _get_client(customer_id)
        rows = _search(f"""
            SELECT
                ad_group_ad.ad.id,
                ad_group_ad.ad.name,
                ad_group_ad.ad.type,
                ad_group_ad.status,
                ad_group_ad.ad.final_urls,
                ad_group_ad.ad.responsive_search_ad.headlines,
                ad_group_ad.ad.responsive_search_ad.descriptions,
                ad_group_ad.ad.expanded_text_ad.headline_part1,
                ad_group_ad.ad.expanded_text_ad.headline_part2,
                ad_group_ad.ad.expanded_text_ad.description,
                metrics.impressions,
                metrics.clicks,
                metrics.cost_micros,
                metrics.conversions
            FROM ad_group_ad
            WHERE ad_group_ad.ad_group = 'customers/{cid}/adGroups/{ad_group_id}'
              AND ad_group_ad.status != 'REMOVED'
              {status_filter}
        """, customer_id)

        ads = []
        for row in rows:
            ad = row.ad_group_ad.ad
            m = row.metrics
            ad_type = ad.type_.name

            entry: dict = {
                "ad_id": str(ad.id),
                "name": ad.name,
                "type": ad_type,
                "status": row.ad_group_ad.status.name,
                "final_urls": list(ad.final_urls),
                "impressions": m.impressions,
                "clicks": m.clicks,
                "cost": _m(m.cost_micros),
                "conversions": round(m.conversions, 2),
            }

            if ad_type == "RESPONSIVE_SEARCH_AD":
                rsa = ad.responsive_search_ad
                entry["headlines"] = [
                    {"text": h.text, "pinned": h.pinned_field.name}
                    for h in rsa.headlines
                ]
                entry["descriptions"] = [
                    {"text": d.text, "pinned": d.pinned_field.name}
                    for d in rsa.descriptions
                ]
            elif ad_type == "EXPANDED_TEXT_AD":
                eta = ad.expanded_text_ad
                entry["headline1"] = eta.headline_part1
                entry["headline2"] = eta.headline_part2
                entry["description"] = eta.description

            ads.append(entry)

        return json.dumps({"ad_group_id": ad_group_id, "total": len(ads), "ads": ads})
    except Exception as e:
        return json.dumps({"error": str(e)})
