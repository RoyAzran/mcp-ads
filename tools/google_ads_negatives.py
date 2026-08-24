"""
Google Ads — Negative Keyword Management Tools.

Covers campaign-level negatives, ad-group-level negatives,
shared negative keyword lists (CRUD + apply/remove), and
one-click search-term-to-negative workflow.

All write operations require editor role.
"""
import json
from typing import Optional

from google.protobuf import field_mask_pb2

from mcp_instance import mcp
from auth import current_user_ctx
from permissions import require_editor
from tools.google_ads import _get_client, _search


# ---------------------------------------------------------------------------
# READ — Campaign-level negative keywords
# ---------------------------------------------------------------------------

@mcp.tool()
def google_ads_list_negative_keywords(
    customer_id: str,
    campaign_id: str = "",
    row_limit: int = 200,
) -> dict:
    """
    List all negative keywords at the campaign level.

    Args:
        customer_id: Google Ads customer ID.
        campaign_id: Filter to a specific campaign ID. Leave blank for all campaigns.
        row_limit: Max rows to return (default: 200).
    """
    try:
        where = f"AND campaign.id = {campaign_id}" if campaign_id else ""
        rows = _search(f"""
            SELECT campaign.id, campaign.name,
                   campaign_criterion.criterion_id,
                   campaign_criterion.keyword.text,
                   campaign_criterion.keyword.match_type,
                   campaign_criterion.negative
            FROM campaign_criterion
            WHERE campaign_criterion.negative = TRUE
              AND campaign_criterion.type = 'KEYWORD'
              AND campaign.status != 'REMOVED'
              {where}
            ORDER BY campaign.name, campaign_criterion.keyword.text
            LIMIT {row_limit}
        """, customer_id)
        results = []
        for row in rows:
            cc = row.campaign_criterion
            results.append({
                "criterion_id": str(cc.criterion_id),
                "campaign_id": str(row.campaign.id),
                "campaign_name": row.campaign.name,
                "keyword": cc.keyword.text,
                "match_type": cc.keyword.match_type.name,
            })
        return {"negative_keywords": results, "total": len(results)}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# READ — Ad-group-level negative keywords
# ---------------------------------------------------------------------------

@mcp.tool()
def google_ads_list_adgroup_negative_keywords(
    customer_id: str,
    adgroup_id: str = "",
    campaign_id: str = "",
    row_limit: int = 200,
) -> dict:
    """
    List all negative keywords at the ad group level.

    Args:
        customer_id: Google Ads customer ID.
        adgroup_id: Filter to a specific ad group ID. Leave blank for all.
        campaign_id: Filter to a specific campaign ID. Leave blank for all.
        row_limit: Max rows to return (default: 200).
    """
    try:
        where_parts = [
            "ad_group_criterion.negative = TRUE",
            "ad_group_criterion.type = 'KEYWORD'",
            "ad_group.status != 'REMOVED'",
        ]
        if adgroup_id:
            where_parts.append(f"ad_group.id = {adgroup_id}")
        if campaign_id:
            where_parts.append(f"campaign.id = {campaign_id}")
        where = " AND ".join(where_parts)
        rows = _search(f"""
            SELECT campaign.name, ad_group.id, ad_group.name,
                   ad_group_criterion.criterion_id,
                   ad_group_criterion.keyword.text,
                   ad_group_criterion.keyword.match_type
            FROM ad_group_criterion
            WHERE {where}
            ORDER BY campaign.name, ad_group.name, ad_group_criterion.keyword.text
            LIMIT {row_limit}
        """, customer_id)
        results = []
        for row in rows:
            c = row.ad_group_criterion
            results.append({
                "criterion_id": str(c.criterion_id),
                "adgroup_id": str(row.ad_group.id),
                "adgroup_name": row.ad_group.name,
                "campaign_name": row.campaign.name,
                "keyword": c.keyword.text,
                "match_type": c.keyword.match_type.name,
            })
        return {"negative_keywords": results, "total": len(results)}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# WRITE — Add ad-group-level negative keywords
# ---------------------------------------------------------------------------

@mcp.tool()
def google_ads_add_adgroup_negative_keywords(
    customer_id: str,
    adgroup_id: str,
    keywords_json: str,
) -> dict:
    """
    Add negative keywords at the ad group level.

    Args:
        customer_id: Google Ads customer ID.
        adgroup_id: Ad group ID to add negatives to.
        keywords_json: JSON array of objects with 'text' and 'match_type'.
            Example: '[{"text": "free", "match_type": "BROAD"}, {"text": "cheap shoes", "match_type": "EXACT"}]'
    """
    require_editor()
    try:
        keywords = json.loads(keywords_json)
        client, cid = _get_client(customer_id)
        adgroup_rn = f"customers/{cid}/adGroups/{adgroup_id}"
        svc = client.get_service("AdGroupCriterionService")
        ops = []
        for kw in keywords:
            op = client.get_type("AdGroupCriterionOperation")
            c = op.create
            c.ad_group = adgroup_rn
            c.negative = True
            c.keyword.text = kw["text"]
            c.keyword.match_type = client.enums.KeywordMatchTypeEnum[kw.get("match_type", "BROAD")]
            ops.append(op)
        resp = svc.mutate_ad_group_criteria(customer_id=cid, operations=ops)
        return {"success": True, "added": len(resp.results), "adgroup_id": adgroup_id}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# WRITE — Remove campaign-level negative keyword
# ---------------------------------------------------------------------------

@mcp.tool()
def google_ads_remove_campaign_negative_keyword(
    customer_id: str,
    campaign_id: str,
    criterion_id: str,
) -> dict:
    """
    Remove a negative keyword from a campaign by its criterion ID.
    Use google_ads_list_negative_keywords first to get criterion_id values.

    Args:
        customer_id: Google Ads customer ID.
        campaign_id: Campaign ID the negative keyword belongs to.
        criterion_id: The criterion_id of the negative keyword to remove.
    """
    require_editor()
    try:
        client, cid = _get_client(customer_id)
        svc = client.get_service("CampaignCriterionService")
        op = client.get_type("CampaignCriterionOperation")
        op.remove = f"customers/{cid}/campaignCriteria/{campaign_id}~{criterion_id}"
        svc.mutate_campaign_criteria(customer_id=cid, operations=[op])
        return {"success": True, "removed_criterion_id": criterion_id, "campaign_id": campaign_id}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# WRITE — Remove ad-group-level negative keyword
# ---------------------------------------------------------------------------

@mcp.tool()
def google_ads_remove_adgroup_negative_keyword(
    customer_id: str,
    adgroup_id: str,
    criterion_id: str,
) -> dict:
    """
    Remove a negative keyword from an ad group by its criterion ID.
    Use google_ads_list_adgroup_negative_keywords first to get criterion_id values.

    Args:
        customer_id: Google Ads customer ID.
        adgroup_id: Ad group ID the negative keyword belongs to.
        criterion_id: The criterion_id of the negative keyword to remove.
    """
    require_editor()
    try:
        client, cid = _get_client(customer_id)
        svc = client.get_service("AdGroupCriterionService")
        op = client.get_type("AdGroupCriterionOperation")
        op.remove = f"customers/{cid}/adGroupCriteria/{adgroup_id}~{criterion_id}"
        svc.mutate_ad_group_criteria(customer_id=cid, operations=[op])
        return {"success": True, "removed_criterion_id": criterion_id, "adgroup_id": adgroup_id}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# READ — List keywords inside a shared negative list
# ---------------------------------------------------------------------------

@mcp.tool()
def google_ads_list_shared_negative_keywords(
    customer_id: str,
    shared_set_id: str,
    row_limit: int = 500,
) -> dict:
    """
    List all keywords inside a shared negative keyword list.
    Use google_ads_shared_negative_lists (in agency.py) to get shared_set_id values.

    Args:
        customer_id: Google Ads customer ID.
        shared_set_id: ID of the shared negative keyword list.
        row_limit: Max rows (default: 500).
    """
    try:
        rows = _search(f"""
            SELECT shared_criterion.criterion_id,
                   shared_criterion.keyword.text,
                   shared_criterion.keyword.match_type,
                   shared_criterion.type,
                   shared_set.id,
                   shared_set.name
            FROM shared_criterion
            WHERE shared_set.id = {shared_set_id}
              AND shared_criterion.type = 'KEYWORD'
            LIMIT {row_limit}
        """, customer_id)
        keywords = []
        for row in rows:
            sc = row.shared_criterion
            keywords.append({
                "criterion_id": str(sc.criterion_id),
                "keyword": sc.keyword.text,
                "match_type": sc.keyword.match_type.name,
            })
        return {
            "shared_set_id": shared_set_id,
            "keywords": keywords,
            "total": len(keywords),
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# WRITE — Add keywords to an existing shared negative list
# ---------------------------------------------------------------------------

@mcp.tool()
def google_ads_add_to_shared_negative_list(
    customer_id: str,
    shared_set_id: str,
    keywords_json: str,
) -> dict:
    """
    Add keywords to an existing shared negative keyword list.

    Args:
        customer_id: Google Ads customer ID.
        shared_set_id: ID of the shared negative keyword list.
        keywords_json: JSON array of objects with 'text' and 'match_type'.
            Example: '[{"text": "free", "match_type": "EXACT"}, {"text": "cheap", "match_type": "BROAD"}]'
    """
    require_editor()
    try:
        keywords = json.loads(keywords_json)
        client, cid = _get_client(customer_id)
        shared_set_rn = f"customers/{cid}/sharedSets/{shared_set_id}"
        svc = client.get_service("SharedCriterionService")
        ops = []
        for kw in keywords:
            op = client.get_type("SharedCriterionOperation")
            c = op.create
            c.shared_set = shared_set_rn
            c.keyword.text = kw["text"]
            c.keyword.match_type = client.enums.KeywordMatchTypeEnum[kw.get("match_type", "BROAD")]
            ops.append(op)
        resp = svc.mutate_shared_criteria(customer_id=cid, operations=ops)
        return {
            "success": True,
            "added": len(resp.results),
            "shared_set_id": shared_set_id,
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# WRITE — Remove a keyword from a shared negative list
# ---------------------------------------------------------------------------

@mcp.tool()
def google_ads_remove_from_shared_negative_list(
    customer_id: str,
    shared_set_id: str,
    criterion_id: str,
) -> dict:
    """
    Remove a keyword from a shared negative keyword list.
    Use google_ads_list_shared_negative_keywords first to get criterion_id values.

    Args:
        customer_id: Google Ads customer ID.
        shared_set_id: ID of the shared negative keyword list.
        criterion_id: criterion_id of the keyword to remove.
    """
    require_editor()
    try:
        client, cid = _get_client(customer_id)
        svc = client.get_service("SharedCriterionService")
        op = client.get_type("SharedCriterionOperation")
        op.remove = f"customers/{cid}/sharedCriteria/{shared_set_id}~{criterion_id}"
        svc.mutate_shared_criteria(customer_id=cid, operations=[op])
        return {
            "success": True,
            "removed_criterion_id": criterion_id,
            "shared_set_id": shared_set_id,
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# WRITE — Remove a shared negative list from a campaign
# ---------------------------------------------------------------------------

@mcp.tool()
def google_ads_remove_shared_negative_list_from_campaign(
    customer_id: str,
    campaign_id: str,
    shared_set_id: str,
) -> dict:
    """
    Detach a shared negative keyword list from a campaign.
    The shared list itself is not deleted — just unlinked from this campaign.

    Args:
        customer_id: Google Ads customer ID.
        campaign_id: Campaign ID to remove the list from.
        shared_set_id: Shared negative keyword list ID to detach.
    """
    require_editor()
    try:
        client, cid = _get_client(customer_id)
        svc = client.get_service("CampaignSharedSetService")
        op = client.get_type("CampaignSharedSetOperation")
        op.remove = f"customers/{cid}/campaignSharedSets/{campaign_id}~{shared_set_id}"
        svc.mutate_campaign_shared_sets(customer_id=cid, operations=[op])
        return {
            "success": True,
            "campaign_id": campaign_id,
            "shared_set_id": shared_set_id,
            "note": "Shared list detached from campaign. List itself still exists.",
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# READ — Which campaigns have a shared negative list applied?
# ---------------------------------------------------------------------------

@mcp.tool()
def google_ads_shared_list_campaign_assignments(
    customer_id: str,
    shared_set_id: str = "",
) -> dict:
    """
    Show which campaigns each shared negative list is applied to.

    Args:
        customer_id: Google Ads customer ID.
        shared_set_id: Filter to a specific shared list ID. Leave blank for all.
    """
    try:
        where = f"AND shared_set.id = {shared_set_id}" if shared_set_id else ""
        rows = _search(f"""
            SELECT campaign.id, campaign.name, campaign.status,
                   shared_set.id, shared_set.name
            FROM campaign_shared_set
            WHERE campaign.status != 'REMOVED'
              AND shared_set.type = 'NEGATIVE_KEYWORDS'
              {where}
            ORDER BY shared_set.name, campaign.name
            LIMIT 500
        """, customer_id)
        results = []
        for row in rows:
            results.append({
                "campaign_id": str(row.campaign.id),
                "campaign_name": row.campaign.name,
                "campaign_status": row.campaign.status.name,
                "shared_set_id": str(row.shared_set.id),
                "shared_set_name": row.shared_set.name,
            })
        return {"assignments": results, "total": len(results)}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# WORKFLOW — Search term → negative keyword (one-click)
# ---------------------------------------------------------------------------

@mcp.tool()
def google_ads_add_search_term_as_negative(
    customer_id: str,
    search_term: str,
    match_type: str,
    campaign_id: str = "",
    adgroup_id: str = "",
    shared_set_id: str = "",
) -> dict:
    """
    Add a search term directly as a negative keyword — one-click workflow from the search terms report.
    Must specify at least one target: campaign_id, adgroup_id, or shared_set_id.

    Args:
        customer_id: Google Ads customer ID.
        search_term: The search term text to add as a negative (e.g. 'free shipping').
        match_type: Match type for the negative: BROAD, PHRASE, or EXACT.
        campaign_id: Add as campaign-level negative to this campaign.
        adgroup_id: Add as ad-group-level negative to this ad group (requires campaign_id too).
        shared_set_id: Add to this shared negative keyword list instead.
    """
    require_editor()
    if not any([campaign_id, adgroup_id, shared_set_id]):
        return {"error": "Specify at least one of: campaign_id, adgroup_id, shared_set_id."}

    results = {}

    # Add to shared list
    if shared_set_id:
        res = google_ads_add_to_shared_negative_list(
            customer_id=customer_id,
            shared_set_id=shared_set_id,
            keywords_json=json.dumps([{"text": search_term, "match_type": match_type}]),
        )
        results["shared_list"] = res

    # Add as campaign-level negative
    if campaign_id and not adgroup_id:
        try:
            client, cid = _get_client(customer_id)
            svc = client.get_service("CampaignCriterionService")
            op = client.get_type("CampaignCriterionOperation")
            c = op.create
            c.campaign = f"customers/{cid}/campaigns/{campaign_id}"
            c.negative = True
            c.keyword.text = search_term
            c.keyword.match_type = client.enums.KeywordMatchTypeEnum[match_type.upper()]
            resp = svc.mutate_campaign_criteria(customer_id=cid, operations=[op])
            results["campaign_negative"] = {
                "success": True,
                "campaign_id": campaign_id,
                "keyword": search_term,
                "match_type": match_type,
                "resource": resp.results[0].resource_name,
            }
        except Exception as e:
            results["campaign_negative"] = {"error": str(e)}

    # Add as ad-group-level negative
    if adgroup_id:
        res = google_ads_add_adgroup_negative_keywords(
            customer_id=customer_id,
            adgroup_id=adgroup_id,
            keywords_json=json.dumps([{"text": search_term, "match_type": match_type}]),
        )
        results["adgroup_negative"] = res

    return {
        "search_term": search_term,
        "match_type": match_type,
        "results": results,
    }


# ---------------------------------------------------------------------------
# WORKFLOW — Bulk add multiple search terms as negatives
# ---------------------------------------------------------------------------

@mcp.tool()
def google_ads_bulk_add_search_terms_as_negatives(
    customer_id: str,
    search_terms_json: str,
    campaign_id: str = "",
    adgroup_id: str = "",
    shared_set_id: str = "",
) -> dict:
    """
    Bulk-add multiple search terms as negative keywords in one call.
    Use after reviewing the search terms report to block wasteful queries.

    Args:
        customer_id: Google Ads customer ID.
        search_terms_json: JSON array of objects with 'text' and 'match_type'.
            Example: '[{"text": "free", "match_type": "BROAD"}, {"text": "diy shoes", "match_type": "EXACT"}]'
        campaign_id: Add as campaign-level negatives to this campaign.
        adgroup_id: Add as ad-group-level negatives to this ad group.
        shared_set_id: Add to this shared negative keyword list.
    """
    require_editor()
    if not any([campaign_id, adgroup_id, shared_set_id]):
        return {"error": "Specify at least one of: campaign_id, adgroup_id, shared_set_id."}

    try:
        terms = json.loads(search_terms_json)
    except Exception as e:
        return {"error": f"Invalid JSON: {e}"}

    results = {}

    if shared_set_id:
        res = google_ads_add_to_shared_negative_list(
            customer_id=customer_id,
            shared_set_id=shared_set_id,
            keywords_json=search_terms_json,
        )
        results["shared_list"] = res

    if campaign_id and not adgroup_id:
        try:
            client, cid = _get_client(customer_id)
            svc = client.get_service("CampaignCriterionService")
            ops = []
            for kw in terms:
                op = client.get_type("CampaignCriterionOperation")
                c = op.create
                c.campaign = f"customers/{cid}/campaigns/{campaign_id}"
                c.negative = True
                c.keyword.text = kw["text"]
                c.keyword.match_type = client.enums.KeywordMatchTypeEnum[kw.get("match_type", "BROAD")]
                ops.append(op)
            resp = svc.mutate_campaign_criteria(customer_id=cid, operations=ops)
            results["campaign_negatives"] = {"success": True, "added": len(resp.results), "campaign_id": campaign_id}
        except Exception as e:
            results["campaign_negatives"] = {"error": str(e)}

    if adgroup_id:
        res = google_ads_add_adgroup_negative_keywords(
            customer_id=customer_id,
            adgroup_id=adgroup_id,
            keywords_json=search_terms_json,
        )
        results["adgroup_negatives"] = res

    return {
        "total_terms": len(terms),
        "results": results,
    }
