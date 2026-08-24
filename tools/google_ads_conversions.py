"""
Google Ads extra tools — RSA updates, conversion setup, and auto-tagging check.
These tools complement tools/google_ads.py and tools/google_ads_advanced.py.
Adapted from google-ads-mcp-server/server.py (extended tools section).
"""
import json
import os
import re
from datetime import date, timedelta

from google.protobuf import field_mask_pb2

from mcp_instance import mcp
from auth import current_user_ctx
from permissions import require_editor

from tools.google_ads import _get_client, _search


def _m(micros) -> float:
    return round(float(micros) / 1_000_000, 2)


# ===========================================================================
# RSA (Responsive Search Ad) UPDATE TOOLS
# ===========================================================================

@mcp.tool()
def google_ads_update_rsa(
    ad_id: str,
    ad_group_id: str,
    headlines: list = None,
    descriptions: list = None,
    customer_id: str = "",
) -> str:
    """Replace headlines and/or descriptions of an existing Responsive Search Ad (RSA).

    Replaces the entire headlines or descriptions list. To add a single asset
    without removing existing ones, use google_ads_add_rsa_asset instead.

    Args:
        ad_id: ID of the ad to update (required).
        ad_group_id: Ad group ID that contains the ad (required).
        headlines: New list of headline strings (3–15 items, max 30 chars each). Replaces all existing.
        descriptions: New list of description strings (2–4 items, max 90 chars each). Replaces all existing.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    require_editor()
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
def google_ads_add_rsa_asset(
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
        text: Text of the new asset. Headlines max 30 chars, descriptions max 90 chars.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    require_editor()
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


# ===========================================================================
# CONVERSION TRACKING TOOLS
# ===========================================================================

@mcp.tool()
def google_ads_get_account_conversion_id(customer_id: str = "") -> str:
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
            })
        return json.dumps({"error": "No conversion tracking ID found for this account"})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_get_conversion_tag_snippet(
    conversion_action_id: str,
    customer_id: str = "",
) -> str:
    """Get the complete gtag snippet for a specific conversion action.

    Returns the global_site_tag (for <head>), event_snippet (fires on conversion),
    and the ready-to-use AW-XXXXXXXX/LABEL send_to string.

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
            })
        return json.dumps({"error": f"Conversion action {conversion_action_id} not found"})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_list_conversion_actions_full(
    include_paused: bool = False,
    customer_id: str = "",
) -> str:
    """List all conversion actions with full details including the AW-XXXXXXXX/LABEL send_to
    string ready for gtag injection.

    Args:
        include_paused: Include PAUSED actions (not just ENABLED). Default False.
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    try:
        status_filter = ("WHERE conversion_action.status != 'REMOVED'"
                         if not include_paused
                         else "WHERE conversion_action.status IN ('ENABLED','PAUSED')")
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
        return json.dumps({"conversion_actions": results, "total": len(results)})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def google_ads_conversion_performance_by_action(
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
def google_ads_conversion_setup_complete(
    name: str,
    category: str = "LEAD",
    value: float = 0.0,
    currency_code: str = "USD",
    customer_id: str = "",
) -> str:
    """One-shot: Create a conversion action and return complete ready-to-use JS snippets.

    Creates the conversion action, fetches its tag snippets, and returns:
    - The AW-XXXXXXXX/LABEL send_to string
    - JS snippet for form submit tracking (replace selector placeholder)
    - JS snippet for thank-you page tracking (replace URL placeholder)

    Args:
        name: Conversion action name (e.g. 'Lead Form Submission').
        category: LEAD | PURCHASE | SIGNUP | PAGE_VIEW | DEFAULT
        value: Optional fixed conversion value. Use 0 for no fixed value.
        currency_code: Currency code (default USD).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    require_editor()
    try:
        client, cid = _get_client(customer_id)

        account_rows = _search("""
            SELECT customer.conversion_tracking_setting.conversion_tracking_id FROM customer
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
        })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def google_ads_create_ecommerce_conversion_actions(
    purchase_name: str = "Purchase",
    add_to_cart_name: str = "Add to Cart",
    begin_checkout_name: str = "Begin Checkout",
    currency_code: str = "USD",
    customer_id: str = "",
) -> str:
    """Create the three standard ecommerce conversion actions in one call:
    Purchase, Add to Cart, Begin Checkout.

    Returns the ID and send_to for each action — ready for tracking implementation.

    Args:
        purchase_name: Name for the purchase conversion action.
        add_to_cart_name: Name for the add-to-cart conversion action.
        begin_checkout_name: Name for the begin-checkout conversion action.
        currency_code: Currency code (default USD).
        customer_id: Google Ads customer ID. Leave blank to use default.
    """
    require_editor()
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
        })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def google_ads_check_auto_tagging(customer_id: str = "") -> str:
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
                "status": ("OK — GA4 will receive GCLID for attribution"
                           if auto_tagging
                           else "WARNING — Auto-tagging is OFF. GA4 cannot attribute sessions to Google Ads."),
                "how_to_fix": (None if auto_tagging
                               else "Go to Google Ads > Admin > Account Settings > Auto-tagging > Enable"),
            })
        return json.dumps({"error": "Could not retrieve account info"})
    except Exception as e:
        return json.dumps({"error": str(e)})
