"""
Category meta-tool registry — collapses the ~1000 individual @mcp.tool()
handlers into ~10 category routers exposed to the CLI.

After `import mcp_server` runs, every tool is registered on the shared FastMCP
instance. We introspect that registry, group tools by their source module, and
expose:

  - get_categories()         → list of {category, tool_count, description}
    - list_actions(category, search?) → recommended action schemas by default,
        or searchable underlying action schemas when a search term is provided
  - dispatch(category, action, params) → run the underlying tool

The CLI registers exactly one MCP tool per category (plus list_actions), so an
agent sees ~11 tool definitions instead of ~1000.
"""
from __future__ import annotations

import re
import json
from dataclasses import dataclass
from typing import Any, Optional

try:
    import sentry_sdk
except ImportError:  # pragma: no cover - the self-host build ships without it
    class _NoopSentry:
        def capture_exception(self, *a, **k): pass
        def capture_message(self, *a, **k): pass
        def set_context(self, *a, **k): pass
        def set_tag(self, *a, **k): pass
    sentry_sdk = _NoopSentry()  # type: ignore[assignment]

from auth import current_user_ctx
from credentials import connect_hint
from mcp_instance import mcp


# Module-prefix → category. Order matters: longest prefix wins (matched first).
# Anything that doesn't match falls into "misc_action".
_MODULE_TO_CATEGORY: list[tuple[str, str]] = [
    ("tools.google_ads", "google_ads_action"),
    # Longer prefix, and it has to be here: tools.google_ads_transparency starts
    # with tools.google_ads, so without this its seven tools file themselves under
    # google_ads_action and competitor research disappears into a 900-tool family.
    ("tools.google_ads_transparency", "google_ads_transparency_action"),
    ("tools.meta_ads", "meta_ads_action"),
    ("tools.meta_ad_library", "meta_ad_library_action"),
    ("tools.openai_ads", "openai_ads_action"),
    ("tools.snapchat_ads", "snapchat_ads_action"),
    ("tools.microsoft_ads", "microsoft_ads_action"),
    ("tools.ga4", "ga4_action"),
    ("tools.gsc", "gsc_action"),
    ("tools.gtm", "gtm_action"),
    ("tools.agency", "agency_action"),
    ("tools.reports", "reports_action"),
    ("tools.creative", "creative_action"),
    # Prefix match, so every tools.wordpress.* submodule lands here too.
    ("tools.wordpress", "wordpress_action"),
]

# Tool-name prefix → category, checked BEFORE the module lookup above.
# tools/pipedream_ads.py holds both LinkedIn and TikTok tools in one module, so the
# module→category map can't split them; without this they all land in misc_action and
# are effectively undiscoverable. Tool names are already consistently prefixed, so
# routing on the name keeps the 1200-line module intact.
_PREFIX_TO_CATEGORY: list[tuple[str, str]] = [
    # Competitor research on public ad archives. Separate from the platform's own
    # family on purpose: meta_ads_action is the user's account, meta_ad_library_action
    # is everyone else's public ads, and an assistant that conflates them reports a
    # competitor's creatives as the user's own performance.
    ("meta_ad_library_", "meta_ad_library_action"),
    ("google_ads_transparency_", "google_ads_transparency_action"),
    ("linkedin_ads_", "linkedin_ads_action"),
    ("tiktok_ads_", "tiktok_ads_action"),
    # Organic posting is a different product surface from ads (different
    # Pipedream app, different API host), so it gets its own category rather
    # than hiding inside the ads ones.
    ("linkedin_organic_", "organic_social_action"),
    ("tiktok_organic_", "organic_social_action"),
    ("meta_pages_", "organic_social_action"),
]

_CATEGORY_DESCRIPTIONS: dict[str, str] = {
    "google_ads_action":
        "Google Ads actions. Start with google_ads_find_account when an account is missing or under an MCC; "
        "otherwise use google_ads_list_customers, google_ads_account_overview, "
        "google_ads_campaign_performance, google_ads_search_terms, google_ads_recommendations. "
        "Write actions should create PAUSED entities unless explicitly requested.",
    "meta_ads_action":
        "Meta Ads actions. Start with meta_ads_list_ad_accounts and meta_ads_find_page. "
        "For lead-gen use this path: meta_ads_create_lead_campaign -> meta_ads_create_lead_adset -> "
        "meta_ads_create_lead_form_and_paused_ad. Prefer meta_ads_create_lead_form_and_paused_ad over "
        "calling meta_ads_create_lead_form directly when you want a usable paused test ad fast. For media, use "
        "meta_ads_upload_image_file_bytes_to_meta, meta_ads_upload_video_file_bytes_to_meta, or meta_ads_test_media_upload_flow. "
        "Create ads PAUSED by default.",
    "meta_ad_library_action":
        "Competitor research on Meta's PUBLIC Ad Library -- ads that OTHER advertisers run on "
        "Facebook and Instagram. This is not the user's own Meta account; that is "
        "meta_ads_action. No ad account, connection or customer id is involved. "
        "Start with meta_ad_library_check_access, then meta_ad_library_find_advertiser to turn "
        "a brand name into a page_id, then meta_ad_library_advertiser_ads or "
        "meta_ad_library_creative_angles. "
        "COVERAGE LIMIT, READ THIS: outside the EU and UK, Meta's API returns only political "
        "and social-issue ads, so a commercial search for the US, Israel, Canada or Australia "
        "comes back empty. That is Meta's rule and not a fault. Pass an EU country or GB in "
        "`countries` for commercial ads, or use google_ads_transparency_action, which covers "
        "180+ countries with no such limit. Never report an empty result here as a broken "
        "connection or as proof the brand does not advertise.",
    "google_ads_transparency_action":
        "Competitor research on Google's Ads Transparency Center -- public ads any verified "
        "advertiser runs on Search, YouTube, Display and Shopping, in 180+ countries. Needs no "
        "API key, no account and no connection, so it works even for a user who has connected "
        "nothing. This is not the user's own Google Ads account; that is google_ads_action. "
        "Start with google_ads_transparency_search_ads for a brand name, or "
        "google_ads_transparency_find_advertiser then google_ads_transparency_advertiser_ads "
        "when several companies share a name. Backed by a public endpoint Google publishes for "
        "browsing but does not document, so a lookup can come back degraded: when it does, say "
        "the source is temporarily unavailable and pass on the manual_url. Never tell the user "
        "to reconnect or re-authenticate anything -- no account is involved.",
    "ga4_action":
        "GA4 actions. Start with ga4_list_properties, ga4_overview, ga4_traffic_sources, "
        "ga4_top_pages, ga4_conversions, and GA preset dashboards.",
    "gsc_action":
        "Search Console actions. Start with gsc_list_sites, then use returned site_url with "
        "gsc_search_analytics, gsc_top_keywords, and gsc_top_pages.",
    "gtm_action":
        "Google Tag Manager actions. Start with gtm_list_accounts, then gtm_list_containers "
        "and gtm_list_workspaces; tags, triggers and variables live inside a workspace, and "
        "changes are only live on the site once a version is created and published.",
    "agency_action":
        "Run agency-workflow actions — multi-account audits, weekly reports, "
        "wasted-spend reports, pacing across all channels.",
    "reports_action":
        "Run composite cross-platform reports — monthly executive, full SEO, "
        "full paid search, full paid social, ecommerce, new client audit, "
        "landing page quality, campaign health.",
    "openai_ads_action":
        "OpenAI Ads actions — manage ads shown inside ChatGPT via the OpenAI Ads API. "
        "Requires the user to connect their OpenAI Ads API key first. Start with "
        "openai_ads_get_account to confirm the key works, then openai_ads_list_campaigns, "
        "openai_ads_list_ad_groups, openai_ads_list_ads, and openai_ads_ad_insights. "
        "Write actions create entities PAUSED unless the user explicitly asks to launch; "
        "budgets and bids are passed in US dollars.",
    "linkedin_ads_action":
        "LinkedIn Ads actions on the connected LinkedIn account — connect it via "
        f"{connect_hint()}. Start with "
        "linkedin_ads_pipedream_accounts to confirm a connection exists, then "
        "linkedin_ads_list_accounts to get the sponsored account id every other action needs. "
        "From there use linkedin_ads_list_campaign_groups, linkedin_ads_list_campaigns and "
        "linkedin_ads_campaign_report. Write actions create entities PAUSED unless the user "
        "explicitly asks to launch.",
    "tiktok_ads_action":
        "TikTok Ads actions on the connected TikTok advertiser account — connect it via "
        f"{connect_hint()}. Start with tiktok_ads_pipedream_accounts to "
        "confirm a connection, then tiktok_ads_list_advertisers — nearly every other action "
        "requires the advertiser_id it returns. From there use tiktok_ads_list_campaigns, "
        "tiktok_ads_list_adgroups and tiktok_ads_campaign_report. Write actions create entities "
        "PAUSED unless the user explicitly asks to launch.",
    "snapchat_ads_action":
        "Snapchat Ads actions on the connected Snapchat account. The hierarchy is organization > "
        "ad account > campaign > ad squad (Snapchat's name for an ad set) > ad. Start with "
        "snapchat_ads_list_organizations, then snapchat_ads_list_accounts to get the "
        "ad_account_id the rest need, then snapchat_ads_list_campaigns and "
        "snapchat_ads_campaign_report. Campaign status is PAUSED or ACTIVE; new campaigns "
        "are created PAUSED unless the user explicitly asks to launch.",
    "microsoft_ads_action":
        "Microsoft Advertising (Bing Ads) actions on the connected Microsoft account. Also requires "
        "MICROSOFT_ADS_DEVELOPER_TOKEN on the server — call microsoft_ads_pipedream_accounts "
        "first, which reports whether it is configured. Start with microsoft_ads_list_accounts "
        "to get the account_id every other action needs, then microsoft_ads_list_campaigns and "
        "microsoft_ads_campaign_performance. Note Bing's API is POST-for-reads and its "
        "reporting is asynchronous; microsoft_ads_campaign_performance hides that behind one call.",
    "creative_action":
        "Generate ad creative images via OpenAI image models.",
    "wordpress_action":
        "WordPress site actions over the site's REST API — posts, pages, media, categories "
        "and tags, users, comments, settings, plugins and themes, plus WooCommerce (woo_*/wc_*), "
        "Elementor, and SEO plugins (yoast_*, rankmath_*). Requires a WordPress site connected "
        f"via {connect_hint()} with an Application Password. Start with wordpress_current_site to see which "
        "site you are acting on; if several are connected, wordpress_list_sites then "
        "wordpress_select_site. This category is large — use list_actions with a search term "
        "(e.g. list_actions('wordpress_action', 'product')) instead of listing everything.",
    "misc_action":
        "Tools that don't fit any other category.",
}


_RECOMMENDED_ACTIONS: set[str] = {
    # Competitor research. Both families are small enough to recommend whole;
    # relying on the "no recommended entries -> show everything" fallback works
    # today and breaks silently the moment an eighth tool is added.
    "meta_ad_library_check_access",
    "meta_ad_library_search_ads",
    "meta_ad_library_find_advertiser",
    "meta_ad_library_advertiser_ads",
    "meta_ad_library_ad_details",
    "meta_ad_library_creative_angles",
    "meta_ad_library_eu_reach",
    "google_ads_transparency_check_access",
    "google_ads_transparency_search_ads",
    "google_ads_transparency_find_advertiser",
    "google_ads_transparency_advertiser_ads",
    "google_ads_transparency_ad_details",
    "google_ads_transparency_creative_mix",
    "google_ads_transparency_regions",
    # The widget tool lives in competitor_ads_app, so it lands in misc_action
    # like creative_show_images does; without this it is invisible there.
    "competitor_ads_gallery",
    "agency_list_platform_accounts",
    "agency_account_health_audit",
    "agency_budget_pacing_all_channels",
    "agency_cross_platform_overview",
    "agency_weekly_report",
    "agency_wasted_spend_report",
    "creative_generate_ad_images",
    "creative_generate_image",
    "ga4_list_properties",
    "ga4_overview",
    "ga4_traffic_sources",
    "ga4_top_pages",
    "ga4_conversions",
    "get_ga_marketing_kpi_dashboard",
    "get_ga_ppc_dashboard",
    "get_ga_seo_dashboard",
    "google_ads_find_account",
    "google_ads_list_customers",
    "google_ads_account_overview",
    "google_ads_campaign_performance",
    "google_ads_ad_performance",
    "google_ads_search_terms",
    "google_ads_recommendations",
    "google_ads_budget_pacing",
    "google_ads_create_budget",
    "google_ads_create_campaign",
    "google_ads_create_adgroup",
    "google_ads_add_keywords",
    "google_ads_create_responsive_search_ad",
    "gsc_list_sites",
    "gsc_search_analytics",
    "gsc_top_keywords",
    "gsc_top_pages",
    # LinkedIn/TikTok are Pipedream-backed: the *_pipedream_accounts probe is the first
    # call because everything else fails with "no connected account" until one exists.
    # The long tail (creatives, conversions, targeting facets, raw_request) stays
    # discoverable via list_actions with a search term.
    "linkedin_ads_pipedream_accounts",
    "linkedin_ads_list_accounts",
    "linkedin_ads_list_campaign_groups",
    "linkedin_ads_list_campaigns",
    "linkedin_ads_campaign_report",
    "linkedin_ads_creative_report",
    "linkedin_ads_create_campaign",
    "linkedin_ads_update_campaign",
    "tiktok_ads_pipedream_accounts",
    "tiktok_ads_list_advertisers",
    "tiktok_ads_list_campaigns",
    "tiktok_ads_list_adgroups",
    "tiktok_ads_campaign_report",
    "tiktok_ads_adgroup_report",
    "tiktok_ads_create_campaign",
    "tiktok_ads_update_campaign",
    "snapchat_ads_pipedream_accounts",
    "snapchat_ads_list_organizations",
    "snapchat_ads_list_accounts",
    "snapchat_ads_list_campaigns",
    "snapchat_ads_campaign_report",
    "snapchat_ads_account_report",
    "snapchat_ads_create_campaign",
    "snapchat_ads_update_campaign_status",
    "microsoft_ads_pipedream_accounts",
    "microsoft_ads_list_accounts",
    "microsoft_ads_list_campaigns",
    "microsoft_ads_list_adgroups",
    "microsoft_ads_list_keywords",
    "microsoft_ads_campaign_performance",
    "microsoft_ads_update_campaign_status",
    "microsoft_ads_update_campaign_budget",
    # GTM is hierarchical -- account > container > workspace -- and nothing is
    # live until a version is published, so the recommended set walks that path
    # rather than exposing 64 tools flat.
    "gtm_list_accounts",
    "gtm_list_containers",
    "gtm_list_workspaces",
    "gtm_list_tags",
    "gtm_list_triggers",
    "gtm_list_variables",
    "gtm_audit_container",
    "gtm_get_container_snippet",
    "gtm_get_live_version",
    "gtm_create_tag",
    "gtm_create_trigger",
    "gtm_setup_ga",
    "gtm_setup_google_ads_conversion",
    "gtm_create_version_from_workspace",
    "gtm_publish_workspace_now",
    "openai_ads_get_account",
    "openai_ads_list_campaigns",
    "openai_ads_get_campaign",
    "openai_ads_create_campaign",
    "openai_ads_set_campaign_status",
    "openai_ads_list_ad_groups",
    "openai_ads_create_ad_group",
    "openai_ads_list_ads",
    "openai_ads_create_chat_card_ad",
    "openai_ads_set_ad_status",
    "openai_ads_ad_insights",
    "meta_ads_list_ad_accounts",
    "meta_ads_find_page",
    "meta_ads_pages",
    "meta_ads_overview",
    "meta_ads_campaigns",
    "meta_ads_adsets",
    "meta_ads_list_ads",
    # The breakdowns an optimisation recommendation actually rests on. All of
    # these existed and were never hidden, but list_actions narrows to the
    # recommended set, so an agent looking at 226 Meta tools never saw them and
    # correctly refused to advise on placements, audiences or devices without
    # the data. "Exists" is not the same as "discoverable".
    "meta_ads_ads",
    "meta_ads_placements",
    "meta_ads_demographics",
    "meta_ads_geo",
    "meta_ads_device_performance",
    "meta_ads_upload_image_file_bytes_to_meta",
    "meta_ads_upload_video_file_bytes_to_meta",
    "meta_ads_test_media_upload_flow",
    "meta_upload_ad_image",
    "meta_create_image_ad",
    "meta_ads_create_image_ad_from_media",
    "meta_ads_create_lead_campaign",
    "meta_ads_create_lead_adset",
    "meta_ads_create_lead_form",
    "meta_ads_create_lead_form_and_paused_ad",
    "meta_ads_update_status",
    "report_campaign_health_check",
    "report_monthly_executive",
    "report_new_client_audit",
    "report_seo_full",
    "report_paid_search_full",
    "report_paid_social_full",
    "report_ecommerce_full",
    "report_landing_page_quality",
    # WordPress. list_actions only narrows to the recommended set when at least
    # one entry exists for the category, so without these a bare
    # list_actions("wordpress_action") would return ~970 schemas in one payload.
    # Every name here passed the audit in scripts/audit_wordpress_tools.py.
    "wordpress_current_site",
    "wordpress_list_sites",
    "wordpress_select_site",
    "wordpress_site_info",
    "wp_list_posts",
    "wp_get_post",
    "wp_create_post",
    "wp_update_post",
    "wp_delete_post",
    "wp_publish_post",
    "wp_search_posts",
    "wp_list_pages",
    "wp_create_page",
    "wp_update_page",
    "wp_list_media",
    "wp_upload_media_from_url",
    "wp_list_categories",
    "wp_list_tags",
    "wp_set_post_terms",
    "wp_list_comments",
    "wp_approve_comment",
    "wp_list_users",
    "wp_get_site_settings",
    "wp_list_plugins",
    "wp_list_themes",
    "woo_list_products",
    "woo_get_product",
    "woo_create_product",
    "woo_update_product",
    "woo_list_orders",
    "woo_get_order",
    "woo_list_customers",
    "woo_sales_report",
}


_BROKEN_ACTIONS: set[str] = {
    # --- the last of the auto-generated gads_* stubs (hidden 2026-08-23) -----
    # This file was generated with one placeholder body copied across every
    # tool: declare the parameters the name implies, ignore all of them, run
    # `SELECT ad_group_ad.ad.id ... FROM ad_group_ad`, return success. Most
    # were hidden long ago. These sixteen were not, and were reachable.
    #
    # The read-shaped ones from that set have since been implemented properly
    # against their real resources (campaign_budget, user_list, experiment,
    # asset, geo_target_constant and the rest) and are deliberately NOT listed
    # here. What is left needs a mutate or a dedicated service -- an offline
    # conversion upload, an experiment promotion, a user invitation -- which a
    # GAQL SELECT cannot express, so there was nothing to implement in place.
    #
    # Hiding rather than deleting keeps the signatures around as the spec for
    # whoever implements them, and matches how the other stubs are handled.
    # scripts/check_gads_stubs.py fails the build if a new one becomes callable.
    "gads_create_shared_negative_list",
    "gads_end_experiment",
    "gads_forecast_reach",
    "gads_get_ad_strength_forecast",
    "gads_invite_user",
    "gads_link_product_account",
    "gads_promote_experiment",
    "gads_schedule_experiment",
    "gads_subscribe_to_recommendation_type",
    "gads_unsubscribe_from_recommendation_type",
    "gads_upload_enhanced_conversions",
    "gads_upload_image_asset_base64",
    "gads_upload_offline_conversions",
    "gads_upload_store_conversions",
    "gads_upload_text_asset",
    "gads_upload_video_asset",
    # TikTok organic posting was hidden here because it had no auth path:
    # Pipedream publishes exactly one TikTok app (tiktok_ads_manager) and never
    # one for organic, so these four could not authenticate at all.
    #
    # That day arrived. There is now a first-party TikTok app: oauth_tiktok.py
    # holds the Login Kit / Content Posting flow, and tools/tiktok_organic_auth.py
    # hands the stored token to these tools and refreshes it, so they call
    # open.tiktokapis.com directly rather than through a Pipedream app that does
    # not exist. Verified live against the connected account before unhiding --
    # creator_info returns the real creator, its quota and privacy options.
    "ga4_channel_performance",
    "ga4_cohort_analysis",
    "ga4_funnel_analysis",
    "ga4_predictive_metrics",
    # Google does not expose the Auction Insights report through the Ads API at
    # all -- FROM auction_insight is rejected with BAD_RESOURCE_TYPE_IN_FROM_CLAUSE
    # unless the account is allowlisted, so this can never return data. The
    # supported alternative is google_ads_auction_insights, which reports
    # campaign-level impression share instead.
    "gads_auction_insights",
    "gads_count_rsa_assets",
    "gads_forecast_keywords",
    "gads_get_account_budget",
    "gads_get_ad_conversion_breakdown",
    "gads_get_ad_performance_by_date",
    "gads_get_asset_group_metrics",
    "gads_get_auction_insights",
    "gads_get_budget_change_log",
    "gads_get_change_event_log",
    "gads_get_change_history",
    "gads_get_conversion_upload_summary",
    "gads_get_keyword_ideas",
    "gads_get_paid_organic_report",
    "gads_get_per_store_view",
    "gads_get_shopping_performance",
    "gads_get_shopping_product_report",
    "gads_get_user_activity_summary",
    "gads_get_user_interests",
    "gads_list_ad_group_audiences",
    "gads_list_ad_group_demographic_bids",
    "gads_list_ad_group_negative_keywords",
    "gads_list_ad_group_placements",
    "gads_list_ad_group_topics",
    "gads_list_ads_by_campaign",
    "gads_list_ads_by_date_range",
    "gads_list_ads_by_final_url",
    "gads_list_asset_groups",
    "gads_list_asset_groups_by_status",
    "gads_list_assets_by_type",
    "gads_list_billing_setups",
    "gads_list_campaign_criteria",
    "gads_list_campaign_negative_keywords",
    "gads_list_linked_accounts",
    "gads_list_linked_merchant_centers",
    "gads_list_negative_keywords",
    "gads_list_product_groups",
    "gads_list_unused_assets",
    "gads_search_ads_by_headline",
    "gads_suggest_smart_campaign_keyword_themes",
    "google_ads_experiments",
    "google_ads_shopping_product_groups",
    "gsc_core_web_vitals",
    "gsc_index_coverage",
    "gsc_links",
    "gsc_mobile_usability",
    "gsc_rich_results",
    # meta_ads_instagram_media / _insights / _mentions / _stories were hidden
    # here with no reason recorded, unlike every other entry in this set. They
    # are not stubs: each is a real Graph call against the documented endpoint
    # ({ig_id}/media, /insights, /tags, /stories). What they have in common is
    # that all four take an instagram_account_id, which can only come from
    # meta_ads_instagram_account -- and that tool, which takes a page_id
    # instead, was never hidden. An audit calling tools in isolation with no
    # real IG account to hand fails exactly those four and passes that one, so
    # this looks like a fixture ordering artifact rather than a defect. Restored
    # because they are the Instagram analytics half of the organic surface and
    # cannot be demonstrated for Meta App Review while the model cannot see them.
    "meta_google_drive_list_files",
    "meta_google_drive_list_videos",
    # --- 2026-06-29 contract audit: gads_* tools with unimplemented/copy-paste
    # bodies that reference undefined variables (NameError) or call helpers with
    # wrong kwargs. They crash on every invocation regardless of credentials.
    # Core operations here are already covered by the working google_ads_* tools.
    # Hidden from discovery so the connected AI is never offered a broken tool.
    "gads_add_ad_group_income_range",
    "gads_add_ad_group_negative_placement",
    "gads_add_ad_group_negative_topic",
    "gads_add_ad_group_parental_status",
    "gads_add_ad_group_proximity",
    "gads_add_asset_to_asset_group",
    "gads_add_audience_signal_to_asset_group",
    "gads_add_campaign_negative_placement",
    "gads_add_campaign_placement",
    "gads_add_campaign_topic",
    "gads_bulk_apply_label_to_ad_groups",
    "gads_bulk_apply_label_to_ads",
    "gads_bulk_apply_label_to_campaigns",
    "gads_bulk_create_ad_groups",
    "gads_bulk_enable_ad_groups",
    "gads_bulk_enable_asset_groups",
    "gads_bulk_pause_ad_groups",
    "gads_bulk_pause_asset_groups",
    "gads_bulk_remove_ad_groups",
    "gads_bulk_remove_ads",
    "gads_bulk_remove_asset_groups",
    "gads_bulk_remove_label_from_ad_groups",
    "gads_bulk_remove_label_from_ads",
    "gads_bulk_remove_label_from_campaigns",
    "gads_bulk_rename_ads",
    "gads_bulk_update_ad_group_cpc",
    "gads_bulk_update_ad_group_status",
    "gads_bulk_update_ad_status",
    "gads_bulk_update_campaign_status",
    "gads_clear_rda_logos",
    "gads_clear_rda_marketing_images",
    "gads_copy_ad_to_ad_groups",
    "gads_create_ad_group",
    "gads_create_campaign_draft",
    "gads_create_similar_audience",
    "gads_duplicate_ad",
    "gads_duplicate_ad_group",
    "gads_duplicate_asset_group",
    "gads_enable_ad_group",
    "gads_generate_audience_insights",
    "gads_generate_recommendations",
    "gads_get_ad_change_history",
    "gads_get_ad_details",
    "gads_get_ad_group_ads_summary",
    "gads_get_ad_group_details",
    "gads_get_experiment_results",
    "gads_list_ad_group_assets",
    "gads_list_ad_groups_by_label",
    "gads_list_ad_groups_by_status",
    "gads_list_ads_by_ad_strength",
    "gads_list_ads_by_label",
    "gads_list_ads_by_policy_status",
    "gads_list_ads_by_status",
    "gads_list_asset_group_assets",
    "gads_list_asset_group_signals",
    "gads_move_rsa_headline",
    "gads_pause_ad_group",
    "gads_remove_ad_group",
    "gads_remove_asset_from_asset_group",
    "gads_remove_asset_group_listing_filter",
    "gads_remove_asset_group_signal",
    "gads_remove_campaign_criterion",
    "gads_remove_rda_logo",
    "gads_remove_rda_marketing_image",
    "gads_remove_rda_youtube_video",
    "gads_search_ads_by_text",
    "gads_set_ad_device_preference",
    "gads_set_ad_group_audience_targeting",
    "gads_set_ad_group_bid_modifiers",
    "gads_set_ad_group_cpm",
    "gads_set_ad_group_cpv",
    "gads_set_ad_group_demographic_bids",
    "gads_set_ad_group_device_bid_modifier",
    "gads_set_ad_group_rotation",
    "gads_set_ad_group_targeting",
    "gads_set_ad_group_tracking_template",
    "gads_set_ad_group_url_suffix",
    "gads_set_ad_group_url_template",
    "gads_set_asset_group_path1",
    "gads_set_asset_group_path2",
    "gads_update_asset_group",
    # --- 2026-06-30 live write-test: gads_* "write" tools whose body only runs a
    # SELECT and returns rows (no mutate call) -- they report success:True while
    # doing NOTHING on the account. Found by exercising writes against a live
    # account (a fake gads_remove_label "succeeded" but the label persisted).
    # The real write path is the google_ads_* family. Hidden so the AI is never
    # offered a silently-no-op write. (gads_update_label / gads_remove_label were
    # given real implementations and intentionally NOT hidden.)
    "gads_add_ad_group_age_range",
    "gads_add_ad_group_gender",
    "gads_add_ad_group_keyword",
    "gads_add_ad_group_negative_keyword",
    "gads_add_ad_group_placement",
    "gads_add_ad_group_topic",
    "gads_add_campaign_negative_keyword",
    "gads_add_conversion_call_adjustments",
    "gads_add_experiment_arm",
    "gads_add_keywords_to_shared_list",
    "gads_add_rda_description",
    "gads_add_rda_headline",
    "gads_add_rda_logo",
    "gads_add_rda_marketing_image",
    "gads_add_rda_youtube_video",
    "gads_add_rsa_description",
    "gads_add_rsa_headline",
    "gads_apply_label_to_ad",
    "gads_apply_label_to_ad_group",
    "gads_apply_label_to_campaign",
    "gads_attach_asset_to_ad_group",
    "gads_attach_asset_to_campaign",
    "gads_attach_asset_to_customer",
    "gads_attach_audience_to_ad_group",
    "gads_attach_audience_to_campaign",
    "gads_attach_shared_list_to_campaign",
    "gads_bulk_create_rsa",
    "gads_bulk_enable_ads",
    "gads_bulk_enable_campaigns",
    "gads_bulk_enable_keywords",
    "gads_bulk_pause_ads",
    "gads_bulk_pause_campaigns",
    "gads_bulk_pause_keywords",
    "gads_bulk_remove_campaigns",
    "gads_bulk_remove_keywords",
    "gads_bulk_set_ad_final_urls",
    "gads_bulk_update_keyword_bids",
    "gads_create_app_ad",
    "gads_create_app_asset",
    "gads_create_asset_group_listing_filter",
    "gads_create_bidding_data_exclusion",
    "gads_create_bidding_seasonality_adjustment",
    "gads_create_bidding_strategy",
    "gads_create_bumper_ad",
    "gads_create_call_ad",
    "gads_create_call_asset",
    "gads_create_campaign_experiment",
    "gads_create_combined_audience",
    "gads_create_conversion_custom_variable",
    "gads_create_conversion_value_rule",
    "gads_create_custom_audience",
    "gads_create_custom_segment",
    "gads_create_customer_match_list",
    "gads_create_customer_user_access",
    "gads_create_customizer_attribute",
    "gads_create_demand_gen_ad",
    "gads_create_dynamic_search_ad",
    "gads_create_expanded_text_ad",
    "gads_create_experiment",
    "gads_create_gmail_ad",
    "gads_create_hotel_ad",
    "gads_create_html5_ad",
    "gads_create_image_ad",
    "gads_create_image_asset",
    "gads_create_in_feed_video_ad",
    "gads_create_in_stream_shopping_ad",
    "gads_create_lead_form_asset",
    "gads_create_local_ad",
    "gads_create_location_asset",
    "gads_create_masthead_ad",
    "gads_create_non_skippable_in_stream_ad",
    "gads_create_offline_user_data_job",
    "gads_create_outstream_video_ad",
    "gads_create_price_asset",
    "gads_create_product_listing_group",
    "gads_create_promotion_asset",
    "gads_create_remarketing_list",
    "gads_create_responsive_video_ad",
    "gads_create_shared_budget",
    "gads_create_shopping_campaign",
    "gads_create_shopping_product_ad",
    "gads_create_showcase_shopping_ad",
    "gads_create_skippable_in_stream_ad",
    "gads_create_smart_campaign",
    "gads_create_smart_campaign_ad",
    "gads_create_structured_snippet_asset",
    "gads_duplicate_campaign",
    "gads_enable_ad",
    "gads_enable_ad_group_asset",
    "gads_enable_asset_group",
    "gads_enable_asset_group_asset",
    "gads_enable_campaign_asset",
    "gads_generate_keyword_ideas",
    "gads_pause_ad",
    "gads_pause_ad_group_asset",
    "gads_pause_asset_group",
    "gads_pause_asset_group_asset",
    "gads_pause_campaign",
    "gads_pause_campaign_asset",
    "gads_remove_ad",
    "gads_remove_ad_group_criterion",
    "gads_remove_asset_group",
    "gads_remove_campaign",
    "gads_remove_customer_user_access",
    "gads_remove_label_from_ad",
    "gads_remove_label_from_ad_group",
    "gads_remove_label_from_campaign",
    "gads_remove_rsa_description",
    "gads_remove_rsa_headline",
    "gads_rename_ad",
    "gads_set_ad_final_mobile_urls",
    "gads_set_ad_final_urls",
    "gads_set_ad_group_criterion_customizer",
    "gads_set_ad_group_customizer",
    "gads_set_ad_tracking_template",
    "gads_set_ad_url_custom_parameters",
    "gads_set_asset_group_final_urls",
    "gads_set_campaign_bid_strategy",
    "gads_set_campaign_conversion_goals",
    "gads_set_campaign_customizer",
    "gads_set_campaign_device_bids",
    "gads_set_campaign_dsa_settings",
    "gads_set_campaign_frequency_cap",
    "gads_set_campaign_languages",
    "gads_set_campaign_schedule",
    "gads_set_campaign_targeting",
    "gads_set_content_exclusions",
    "gads_set_customer_conversion_goals",
    "gads_set_customer_customizer",
    "gads_set_enhanced_cpc",
    "gads_set_maximize_conversion_value",
    "gads_set_maximize_conversions",
    "gads_set_placement_exclusions",
    "gads_set_rda_call_to_action",
    "gads_set_rda_price_prefix",
    "gads_set_rda_promo_text",
    "gads_set_target_cpa",
    "gads_set_target_impression_share",
    "gads_set_target_roas",
    "gads_update_account",
    "gads_update_ad",
    "gads_update_ad_group",
    "gads_update_app_ad",
    "gads_update_asset",
    "gads_update_bidding_strategy",
    "gads_update_budget",
    "gads_update_call_ad",
    "gads_update_campaign",
    "gads_update_conversion_value_rule",
    "gads_update_customer_user_access",
    "gads_update_demand_gen_ad",
    "gads_update_hotel_ad",
    "gads_update_keyword",
    "gads_update_product_group",
    "gads_update_responsive_display_ad",
    "gads_update_rsa_description",
    "gads_update_rsa_headline",
    "gads_update_rsa_paths",
    "gads_update_smart_campaign_ad",
    "gads_update_smart_campaign_settings",
    "gads_update_user_list",
    "gads_update_video_ad",
}


_DUPLICATE_ALIASES: set[str] = {
    "gads_account_overview",
    "gads_ad_performance",
    "gads_add_keywords",
    "gads_add_negative_keywords",
    "gads_add_rsa_asset",
    "gads_adgroup_performance",
    "gads_apply_label",
    "gads_apply_recommendation",
    "gads_asset_report",
    "gads_auction_insights",
    "gads_audience_performance",
    "gads_budget_pacing",
    "gads_bulk_create_keywords",
    "gads_call_metrics",
    "gads_campaign_performance",
    "gads_change_history",
    "gads_conversion_actions",
    "gads_conversion_performance_by_action",
    "gads_conversion_setup_complete",
    "gads_create_adgroup",
    "gads_create_asset_group",
    "gads_create_budget",
    "gads_create_callout_asset",
    "gads_create_campaign",
    "gads_create_conversion_action",
    "gads_create_ecommerce_conversion_actions",
    "gads_create_label",
    "gads_create_responsive_display_ad",
    "gads_create_responsive_search_ad",
    "gads_create_shared_negative_list",
    "gads_create_sitelink_asset",
    "gads_create_video_ad",
    "gads_demographic_breakdown",
    "gads_device_breakdown",
    "gads_dismiss_recommendation",
    "gads_display_performance",
    "gads_enable_campaign",
    "gads_extension_performance",
    "gads_geo_performance",
    "gads_get_account_info",
    "gads_get_asset_group_performance",
    "gads_get_conversion_tag_snippet",
    "gads_hourly_breakdown",
    "gads_keyword_performance",
    "gads_landing_page_performance",
    "gads_list_customers",
    "gads_list_labels",
    "gads_list_negative_keywords",
    "gads_pause_all_campaigns",
    "gads_quality_score",
    "gads_recommendations",
    "gads_remove_campaign_negative_keyword",
    "gads_remove_keyword",
    "gads_search_terms",
    "gads_shopping_performance",
    "gads_update_adgroup_status",
    "gads_update_campaign_budget",
    "gads_update_campaign_name",
    "gads_update_campaign_status",
    "gads_update_conversion_action",
    "gads_update_keyword_bid",
    "gads_update_keyword_match_type",
    "gads_update_keyword_status",
    "gads_update_rsa",
    "gads_upload_image_asset",
    "gads_upload_offline_conversions",
    "gads_video_performance",
    "google_ads_check_auto_tagging",
    "google_ads_get_account_conversion_id",
    "google_ads_list_conversion_actions_full",
}


_ACTION_GUIDANCE: dict[str, str] = {
    "meta_ads_create_lead_campaign": (
        "Use this first for instant-form lead flows. After campaign creation, call "
        "meta_ads_create_lead_adset, then meta_ads_create_lead_form_and_paused_ad."
    ),
    "meta_ads_create_lead_adset": (
        "Use this after meta_ads_create_lead_campaign. Pass the Facebook page_id in promoted_object via this helper, "
        "then finish with meta_ads_create_lead_form_and_paused_ad."
    ),
    "meta_ads_create_lead_form": (
        "Standalone form creation only. Prefer meta_ads_create_lead_form_and_paused_ad when the goal is a usable test lead ad fast."
    ),
    "meta_ads_create_lead_ad_from_form": (
        "Use only when you already have a valid form_id and adset_id. For the normal test flow, prefer meta_ads_create_lead_form_and_paused_ad."
    ),
    "meta_ads_create_lead_form_and_paused_ad": (
        "Preferred one-call test flow after campaign + adset exist. Creates the form, creative, and paused ad together."
    ),
}


def _wordpress_broken() -> set[str]:
    """Tools the WordPress audit proved contradict their own docstring.

    Generated by scripts/audit_wordpress_tools.py: 240 call a companion plugin
    that does not exist, ~334 resolve only to /wp/v2/posts whatever they claim,
    157 make no HTTP call at all. They stay registered (dispatch still reaches
    them) but are hidden from discovery so the AI is not offered a tool that
    confidently does the wrong thing.
    """
    try:
        from wordpress_generated_sets import BROKEN_WORDPRESS_ACTIONS
    except ImportError:  # WordPress tools disabled via WORDPRESS_TOOLS_ENABLED
        return set()
    return set(BROKEN_WORDPRESS_ACTIONS)


# Broken action -> the supported tool that answers the same question. Surfaced
# by dispatch() when something calls a broken action directly, so the caller is
# redirected instead of just being told no.
_BROKEN_REPLACEMENTS: dict[str, str] = {
    "gads_auction_insights": "google_ads_auction_insights",
    "gads_get_auction_insights": "google_ads_auction_insights",
}

_HIDDEN_BY_DEFAULT: set[str] = _BROKEN_ACTIONS | _DUPLICATE_ALIASES | _wordpress_broken()


def _category_for_module(module_name: str) -> str:
    """Resolve a tool's module name to a category slug. Longest-prefix wins."""
    best: Optional[tuple[str, str]] = None
    for prefix, cat in _MODULE_TO_CATEGORY:
        if module_name == prefix or module_name.startswith(prefix + "_") or module_name.startswith(prefix + "."):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, cat)
    return best[1] if best else "misc_action"


def _category_for_tool(tool_name: str, module_name: str) -> str:
    """Resolve a tool to its category. Tool-name prefix wins over the module map."""
    best: Optional[tuple[str, str]] = None
    for prefix, cat in _PREFIX_TO_CATEGORY:
        if tool_name.startswith(prefix):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, cat)
    if best:
        return best[1]
    return _category_for_module(module_name)


@dataclass
class _Action:
    name: str
    title: str
    description: str
    parameters: dict[str, Any]   # JSON schema (object)
    module: str


# Built lazily on first access so importers can finish registering tools first.
_REGISTRY: Optional[dict[str, dict[str, _Action]]] = None


def _build_registry() -> dict[str, dict[str, _Action]]:
    """Walk FastMCP's tool manager and group tools into categories."""
    registry: dict[str, dict[str, _Action]] = {}
    tool_manager = mcp._tool_manager
    for tool_name, tool_obj in tool_manager._tools.items():
        module = getattr(tool_obj.fn, "__module__", "") or ""
        category = _category_for_tool(tool_name, module)
        registry.setdefault(category, {})
        registry[category][tool_name] = _Action(
            name=tool_name,
            title=getattr(tool_obj, "title", "") or tool_name.replace("_", " ").title(),
            description=tool_obj.description or "",
            parameters=tool_obj.parameters or {"type": "object", "properties": {}},
            module=module,
        )
    return registry


def _get_registry() -> dict[str, dict[str, _Action]]:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _build_registry()
    return _REGISTRY


def reset_cache() -> None:
    """Force the registry to be rebuilt on next access (used in tests)."""
    global _REGISTRY
    _REGISTRY = None


def _is_hidden(action: _Action) -> bool:
    return action.name in _HIDDEN_BY_DEFAULT


def _description_for(action: _Action) -> str:
    description = action.description or ""
    labels: list[str] = []
    existing_label_text = description[:80].upper()
    if action.name in _RECOMMENDED_ACTIONS and "RECOMMENDED" not in existing_label_text:
        labels.append("RECOMMENDED")
    if action.name in _BROKEN_ACTIONS and "HIDDEN" not in existing_label_text:
        labels.append("HIDDEN: failing audit")
    elif action.name in _DUPLICATE_ALIASES and "HIDDEN" not in existing_label_text:
        labels.append("HIDDEN: duplicate alias")
    guidance = _ACTION_GUIDANCE.get(action.name, "")
    if guidance:
        description = f"{description} Guidance: {guidance}".strip()
    inferred = _inferred_guidance(action)
    if inferred:
        description = f"{description} Agent hint: {inferred}".strip()
    if labels:
        return f"[{'/'.join(labels)}] {description}"
    return description


def _parameter_names(action: _Action) -> set[str]:
    props = action.parameters.get("properties", {}) if isinstance(action.parameters, dict) else {}
    return {str(name).lower() for name in props.keys()}


def _inferred_guidance(action: _Action) -> str:
    name = action.name.lower()
    params = _parameter_names(action)

    # Competitor research short-circuits every heuristic below, and has to.
    # is_media_related fires on "creative", and is_media_write fires on "create"
    # -- which "creative" contains -- so meta_ad_library_creative_angles would be
    # published telling the model to "prefer meta_upload_ad_image for image-only
    # uploads", and google_ads_transparency_creative_mix to "upload the asset
    # first": write instructions bolted onto read-only lookups of somebody
    # else's public ads. The account_id hint further down is wrong here too.
    if name.startswith(("meta_ad_library_", "google_ads_transparency_")):
        return ("These are PUBLIC ads by OTHER advertisers, not the user's own account, "
                "campaigns or media library. No connection, account id or token is needed, "
                "so a failure here never means the user should reconnect anything. Never "
                "route from here to meta_ads_media_library, media_upload_start, or any "
                "action that writes to the user's account.")
    hints: list[str] = []
    is_media_related = any(token in name for token in ("upload", "image", "video", "media", "asset", "creative"))
    is_media_write = any(token in name for token in ("upload", "create", "add", "attach", "generate")) and is_media_related
    is_media_browse = any(token in name for token in ("list", "library", "browse", "get")) and is_media_related

    if is_media_related:
        if "meta" in name:
            if is_media_write:
                hints.append(
                    "For Meta media writes, prefer meta_upload_ad_image for image-only uploads, "
                    "meta_upload_ad_video for video-only uploads, and meta_ads_create_image_ad_from_media when the user wants an ad created."
                )
            elif is_media_browse:
                hints.append("For Meta media browsing, prefer meta_ads_media_library for the combined image/video/creative picker.")
        elif "google_ads" in name or name.startswith("gads_"):
            if is_media_write:
                hints.append(
                    "For Google Ads media writes, upload the asset first when needed, then reference the returned asset ID/resource name in ad creation."
                )

        if is_media_write and any(param in params for param in ("source", "image_url", "video_url", "file_bytes_base64", "image_base64", "video_bytes_base64")):
            hints.append(
                "MEDIA MUST ARRIVE AS A URL, NOT AS BYTES. Pass a public HTTPS URL "
                "(a storage.googleapis.com staging URL is ideal), a generated_asset_id, "
                "or a file path that exists on THIS server. Do NOT pass base64 in "
                "file_bytes_base64/image_base64/video_bytes_base64: large tool "
                "parameters are truncated in transit, so the file arrives corrupt. "
                "It fails loudly for PNG and SILENTLY for JPEG -- a clipped JPEG still "
                "decodes, so Meta returns a valid-looking image_hash for a damaged "
                "creative. Verified: the same 6405-byte JPEG produced two different "
                "image_hashes via the two routes. "
                "If the user pasted or attached a picture in chat, you do not have a "
                "URL for it, and you cannot read the attachment off disk either -- a "
                "chat attachment is not a path on this server, so passing its filename "
                "returns 'File not found'. Do not invent a URL, do not base64 it, and "
                "do NOT tell them to upload it in a dashboard: there is no dashboard "
                "uploader, and that answer has stranded real customers. "
                "Call media_upload_start (in misc_action) instead. It mints an upload "
                "slot and returns both a drop zone, in hosts that render one, and a "
                "plain link that works anywhere else. When they have uploaded, "
                "media_upload_result hands you the storage.googleapis.com URL to pass "
                "in here. That tool is the only way to turn an attachment into a URL."
            )

    if name.startswith("meta_ads_create_") and "status" not in params:
        hints.append("Meta creation helpers should leave ads/campaigns paused when the helper supports it or when the user did not explicitly ask to launch.")
    elif "status" in params and any(verb in name for verb in ("create", "update", "enable")):
        hints.append("For write actions, keep status PAUSED unless the user explicitly asks to publish or enable.")

    if "account_id" in params and any(prefix in name for prefix in ("meta_ads", "meta_")):
        hints.append("If account_id is unknown, call meta_ads_list_ad_accounts first.")
    if "customer_id" in params and ("google_ads" in name or name.startswith("gads_")):
        hints.append("If customer_id is unknown, call google_ads_find_account or google_ads_list_customers first.")
    if "property_id" in params and (name.startswith("ga4_") or name.startswith("get_ga_")):
        hints.append("If property_id is unknown, call ga4_list_properties first.")
    if "site_url" in params and name.startswith("gsc_"):
        hints.append("If site_url is unknown, call gsc_list_sites first and pass the exact returned URL.")

    # --- When to stop and ask the customer ---
    # Ambiguous target: if more than one account/property/site is connected,
    # don't guess which one to act on.
    if any(p in params for p in ("account_id", "customer_id", "property_id", "site_url")):
        hints.append("If the customer has more than one account/property/site connected, ask which one to use before proceeding instead of guessing.")

    # Money/publish/destructive writes: get explicit approval first.
    destructive = any(v in name for v in ("delete", "remove", "archive", "pause_all"))
    spend_or_publish = any(v in name for v in (
        "create", "launch", "enable", "activate", "publish", "boost", "duplicate",
        "update_budget", "set_budget", "update_status", "set_status",
        "upload_offline", "upload_customer", "apply_recommendation",
    ))
    # WordPress tools act on a website, not an ad account -- describing a post
    # edit as affecting "spend" is wrong and teaches the model the wrong model
    # of what it is touching.
    is_wordpress = name.startswith((
        "wp_", "woo_", "wc_", "wordpress_", "elementor_", "yoast_", "rankmath_",
        "aiseo_", "aioseo_", "seopress_", "acf_", "woodmart_",
    ))
    if destructive:
        target = "their live WordPress site" if is_wordpress else "their live account"
        hints.append(f"ASK THE CUSTOMER TO CONFIRM before running — this permanently removes or alters something on {target}. State exactly what will be affected and wait for a clear yes.")
    elif spend_or_publish:
        if is_wordpress:
            hints.append("This writes to the customer's live WordPress site (can change published content, settings, or plugins). Tell the customer exactly what you are about to do and get an explicit go-ahead before running it.")
        else:
            hints.append("This writes to the customer's live ad account (can affect spend, status, or what is published). Tell the customer exactly what you are about to do and get an explicit go-ahead before running it.")

    return " ".join(hints)


def _sort_key(action: _Action) -> tuple[int, str]:
    return (0 if action.name in _RECOMMENDED_ACTIONS else 1, action.name)


def get_categories() -> list[dict[str, Any]]:
    """Return [{category, tool_count, description}] for every category present."""
    reg = _get_registry()
    out: list[dict[str, Any]] = []
    for cat, actions in sorted(reg.items()):
        visible_count = sum(1 for action in actions.values() if not _is_hidden(action))
        out.append({
            "category": cat,
            "tool_count": visible_count,
            "hidden_count": len(actions) - visible_count,
            "description": _CATEGORY_DESCRIPTIONS.get(cat, ""),
        })
    return out


def _resolve_category(category: str, reg: dict) -> str:
    """Accept the obvious near-misses for a category name.

    Every category is named <platform>_action and is reached through a tool of
    the same name, so "meta_ads" is what a caller reaches for when it means
    meta_ads_action -- and list_actions raised on it, which is a hard stop on
    the one tool whose whole job is to get an unstuck caller unstuck. Telemetry
    to 2026-08-23 has list_actions itself as the single most-failed tool.

    Only exact, unambiguous rewrites: the suffix, or its absence. Anything
    vaguer still raises with the full list, because guessing wrong here sends
    the caller into a different platform's tools without telling it.
    """
    if category in reg:
        return category
    for candidate in (f"{category}_action", category.removesuffix("_action")):
        if candidate in reg:
            return candidate
    return category


def list_actions(
    category: str,
    search: str = "",
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    """
    Return action schemas for a category, optionally filtered by a substring
    match on action name or description (case-insensitive). Paginated to keep
    payloads bounded — callers can page through with `offset` if needed.

    Returns: {category, total, returned, offset, actions: [{name, description, parameters}]}
    """
    reg = _get_registry()
    category = _resolve_category(category, reg)
    if category not in reg:
        raise KeyError(f"Unknown category: {category}. Available: {sorted(reg.keys())}")

    curated_default = not search.strip()
    include_hidden = False
    hidden_only = False
    if search.startswith("all:"):
        include_hidden = True
        curated_default = False
        search = search[4:]
    elif search.startswith("hidden:"):
        include_hidden = True
        hidden_only = True
        curated_default = False
        search = search[7:]
    elif search.startswith("visible:"):
        curated_default = False
        search = search[8:]

    actions = list(reg[category].values())
    if hidden_only:
        actions = [action for action in actions if _is_hidden(action)]
    elif not include_hidden:
        actions = [action for action in actions if not _is_hidden(action)]

    if curated_default:
        recommended = [action for action in actions if action.name in _RECOMMENDED_ACTIONS]
        if recommended:
            actions = recommended

    if search:
        needle = search.lower()
        actions = [
            a for a in actions
            if needle in a.name.lower() or needle in (a.description or "").lower()
        ]

    actions.sort(key=_sort_key)
    total = len(actions)
    page = actions[offset:offset + max(1, min(limit, 500))]

    hidden_in_category = len([a for a in reg[category].values() if _is_hidden(a)])
    visible_in_category = len([a for a in reg[category].values() if not _is_hidden(a)])
    return {
        "category": category,
        "total": total,
        "returned": len(page),
        "offset": offset,
        "discovery_mode": "recommended" if curated_default else "search",
        "visible_in_category": visible_in_category,
        "actions": [
            {"name": a.name, "title": a.title, "description": _description_for(a), "parameters": a.parameters}
            for a in page
        ],
        "hidden_by_default": hidden_in_category,
        "tips": [
            "With no search term, only recommended workflow/core actions are returned.",
            "Use search='<term>' to search all visible actions in this category.",
            "Every action includes name, title, description, and parameters. Use title for human intent and name for dispatch.",
            "Prefer composite helpers when available (for example actions named like create_*_and_* or explicitly described as one-call flows) before stitching low-level tools together manually.",
            "For image/video uploads, pass a public HTTPS URL, a generated_asset_id, or a path on this server. Never pass base64: large tool parameters truncate in transit and the media arrives corrupt -- loudly for PNG, silently for JPEG. If the user attached or pasted a picture in chat you have no URL for it and cannot read it off disk -- call media_upload_start (misc_action), which returns an upload link that works in any client, then media_upload_result for the storage.googleapis.com URL. There is no dashboard uploader; never send them looking for one.",
            "Use search='visible:' to list every non-hidden action, search='all:<term>' to include hidden actions, or search='hidden:<term>' to inspect only hidden actions.",
        ],
    }


# Verb prefixes a model reaches for when it is inventing a name rather than
# recalling one. Stripped before matching so get_adset can find adsets.
_GUESS_VERBS = {"get", "list", "fetch", "show", "read", "retrieve", "find", "view"}


def _name_tokens(name: str) -> set:
    """Tokens of an action name, minus filler, with plurals folded together."""
    out = set()
    for token in name.lower().split("_"):
        if not token or token in _GUESS_VERBS:
            continue
        # adsets/adset, pages/page, creatives/creative -- a guess and the real
        # name routinely disagree only on the s.
        if len(token) > 3 and token.endswith("s"):
            token = token[:-1]
        out.add(token)
    return out


def _suggest_actions(action: str, names, limit: int = 5) -> list:
    """Rank real action names against a name that does not exist.

    The old rule was `action.lower() in name.lower()`, which only ever fired
    when the guess was a strict substring of a real name. Every realistic wrong
    guess is longer than, or reorders, the name it wants -- meta_ads_get_adset
    for meta_ads_adsets, meta_ads_list_pages for meta_ads_pages -- so the rule
    matched nothing and the error carried no suggestion at all. In two weeks of
    telemetry, 11 of 19 distinct failing tool names were inventions of exactly
    this shape, each of which had an obvious real counterpart.

    Score on shared tokens first, since that is what survives a paraphrase, and
    use difflib only to order names that tie.
    """
    import difflib

    wanted = _name_tokens(action)
    scored = []
    for name in names:
        tokens = _name_tokens(name)
        if not tokens or not wanted:
            continue
        shared = len(wanted & tokens)
        if not shared:
            continue
        # Jaccard, so a name that is all of the guess plus nothing else wins
        # over one that merely contains it among many other tokens.
        overlap = shared / len(wanted | tokens)
        ratio = difflib.SequenceMatcher(None, action.lower(), name.lower()).ratio()
        scored.append((overlap, ratio, name))
    scored.sort(key=lambda row: (-row[0], -row[1], row[2]))
    return [name for _, _, name in scored[:limit]]


def find_action(category: str, action: str) -> _Action:
    """Look up a specific action; raises KeyError with a helpful message."""
    reg = _get_registry()
    category = _resolve_category(category, reg)
    if category not in reg:
        raise KeyError(
            f"Unknown category '{category}'. Available: {sorted(reg.keys())}"
        )
    if action not in reg[category]:
        candidates = _suggest_actions(action, reg[category])
        suggestion = f" Did you mean: {candidates}?" if candidates else ""
        raise KeyError(
            f"Action '{action}' not found in '{category}'.{suggestion} "
            f"Use list_actions(category='{category}', search=...) to discover actions."
        )
    return reg[category][action]


def _error_text_from_result(result: Any) -> str:
    if isinstance(result, dict):
        if result.get("error"):
            return str(result.get("error"))
        if result.get("success") is False:
            return str(result.get("message") or result.get("detail") or result)
    if isinstance(result, str):
        text = result.strip()
        if not text:
            return ""
        try:
            parsed = json.loads(text)
        except Exception:
            return ""
        return _error_text_from_result(parsed)
    return ""


def _capture_tool_failure(
    category: str,
    action: str,
    params: dict[str, Any],
    *,
    exc: Exception | None = None,
    result: Any = None,
) -> None:
    user = current_user_ctx.get(None)
    with sentry_sdk.push_scope() as scope:
        scope.set_tag("mcp.category", category)
        scope.set_tag("mcp.action", action)
        scope.set_tag("mcp.surface", "tool_registry.dispatch")
        if user is not None:
            scope.set_user({"id": user.id})
        scope.set_extra("param_keys", sorted((params or {}).keys()))
        if exc is not None:
            sentry_sdk.capture_exception(exc)
            return
        error_text = _error_text_from_result(result)
        if error_text:
            scope.set_extra("tool_error", error_text[:1000])
            sentry_sdk.capture_message("MCP tool returned error", level="warning")
    _capture_failure_analytics(category, action, exc=exc, result=result)


def _capture_failure_analytics(
    category: str,
    action: str,
    *,
    exc: Exception | None = None,
    result: Any = None,
) -> None:
    """Send the failure to PostHog as well as Sentry, carrying its message.

    The two answer different questions and only Sentry was being told. Sentry
    groups an error by stack trace, which is what you want once you know
    something is wrong; PostHog is where the per-tool breakdown lives, and it is
    where "which tools are failing, and for whom" actually gets asked.

    It could not answer the second half. The MCP instrumentation records a call
    as errored but leaves $mcp_response null on exactly those events, so a
    fortnight of failures showed 27 errored calls across four deployments and
    not one line of error text -- enough to know something was broken, never
    enough to know what. Every diagnosis had to be reconstructed by reading the
    tool's source and guessing.

    The message is truncated and no parameters are attached: params carry ad
    account ids, page ids and occasionally a token, and this leaves the process
    to a third party.
    """
    try:
        from mcp_instance import posthog_client

        if posthog_client is None:
            return
        message = str(exc) if exc is not None else _error_text_from_result(result)
        if not message:
            return
        user = current_user_ctx.get(None)
        posthog_client.capture(
            distinct_id=getattr(user, "id", None) or "anonymous",
            event="mcp_tool_failed",
            properties={
                "mcp_category": category,
                "mcp_action": action,
                "mcp_action_full": f"{category}:{action}",
                "error_message": message[:500],
                "error_type": type(exc).__name__ if exc is not None else "tool_error",
            },
        )
    except Exception:  # noqa: BLE001 - analytics must never break a tool call
        return


def _tool_context():
    """A Context for calling a tool outside an MCP request.

    SDK 2.0 changed Tool.run to require one: `run(arguments, context)`. Under
    1.27 the single-argument form was fine, so this broke every dispatcher path
    at once -- /mcp-slim, the CLI and the GPT Action all reach tools through
    dispatch() -- with "Tool.run() missing 1 required positional argument".
    Nothing caught it because no test exercises a real tool call.

    Built without a request_context on purpose: dispatch is reached from HTTP
    handlers that are not inside an MCP request, and the SDK constructs the same
    context-free Context for its own out-of-band calls. The user identity tools
    actually rely on travels through auth.current_user_ctx, not through this.
    """
    from mcp.server.mcpserver.context import Context

    return Context(mcp_server=mcp, subscriptions=getattr(mcp, "_subscriptions", None))


def _resolve_param_aliases(tool_obj: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Match a supplied parameter to a declared one that differs only in underscores.

    Seven Google Ads tools declare `adgroup_id` while 391 references elsewhere
    in the same file -- and the Google Ads API itself, and every neighbouring
    tool -- call it `ad_group_id`. A caller that has just read
    gads_list_keywords, where it is `ad_group_id`, sends `ad_group_id` to
    gads_update_keyword_status and gets "invalid or missing arguments:
    adgroup_id" for an argument it did in fact supply. That is what
    gads_update_keyword_status failed with in production, and it is unguessable
    from the error, which names the parameter the caller did not send rather
    than the one it did.

    Matching on the name with underscores stripped fixes that whole class
    without renaming published parameters. Deliberately narrow:

      * only names that are not already valid are considered, so a correct call
        is never rewritten;
      * the normalised form must match exactly one declared parameter, so an
        ambiguous name is left alone to fail as before;
      * a declared parameter that already has a value is never overwritten.
    """
    schema = getattr(tool_obj, "parameters", None)
    if not isinstance(schema, dict) or not params:
        return params
    declared = schema.get("properties")
    if not isinstance(declared, dict):
        return params

    unknown = [name for name in params if name not in declared]
    if not unknown:
        return params

    by_normal: dict[str, list[str]] = {}
    for name in declared:
        by_normal.setdefault(name.replace("_", ""), []).append(name)

    resolved = dict(params)
    for name in unknown:
        matches = by_normal.get(name.replace("_", ""), [])
        if len(matches) != 1:
            continue
        target = matches[0]
        if resolved.get(target) not in (None, ""):
            continue  # the real name was supplied too; it wins
        resolved[target] = resolved.pop(name)
    return resolved


def _drop_null_optionals(tool_obj: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Treat an explicit null for an optional parameter as "not supplied".

    4,172 parameters across 1,256 tools are declared `status: str = None` --
    annotation str, default None. Pydantic reads that as a required-type field
    with a None default: omitting it works, passing null for it does not, and
    the call fails with "Input should be a valid string" for a parameter the
    caller was explicitly declining to set.

    Models fill optional arguments with null routinely. gads_list_keywords was
    called in production with {"campaign_id": ..., "status": null} and rejected,
    which reads as the tool being broken rather than as a value that should
    simply have been left out.

    Dropping the key is exactly equivalent to omitting it -- the parameter then
    takes the same None default it was already declared with, so no tool sees a
    value it would not otherwise have seen.

    Required parameters are left alone: a null there is a real mistake, and it
    should fail saying so rather than turn into "missing argument".
    """
    schema = getattr(tool_obj, "parameters", None)
    if not isinstance(schema, dict) or not params:
        return params
    declared = schema.get("properties")
    if not isinstance(declared, dict):
        return params
    required = set(schema.get("required") or ())

    return {
        name: value for name, value in params.items()
        if value is not None or name in required or name not in declared
    }


def _coerce_list_params(tool_obj: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Let a caller pass "a,b" where the tool declares a list.

    121 parameters across the registry are annotated `list`, and every one of
    them is documented to a model as a comma-separated string -- which pydantic
    then rejects, so the call fails validation before it runs. ga4_compare_two_dates
    is the one a customer hit: it wants dimensions and metrics as lists while
    every neighbouring GA4 tool takes them as strings, and the failure surfaces
    as "invalid or missing arguments: dimensions, metrics" for arguments that
    were in fact supplied.

    Only strings aimed at array-typed parameters are touched, so an MCP client
    sending a proper array is unaffected. A JSON array in a string is parsed as
    one rather than split on its punctuation.
    """
    schema = getattr(tool_obj, "parameters", None)
    if not isinstance(schema, dict) or not params:
        return params
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return params

    out = dict(params)
    for key, value in params.items():
        if not isinstance(value, str):
            continue
        spec = properties.get(key)
        if not isinstance(spec, dict):
            continue
        types = {spec.get("type")} | {
            v.get("type") for v in spec.get("anyOf", []) if isinstance(v, dict)
        }
        if "array" not in types:
            continue
        text = value.strip()
        if text.startswith("["):
            try:
                decoded = json.loads(text)
                if isinstance(decoded, list):
                    out[key] = decoded
                    continue
            except ValueError:
                pass
        out[key] = [part.strip() for part in text.split(",") if part.strip()]
    return out


async def dispatch(category: str, action: str, params: dict[str, Any]) -> Any:
    """
    Run an underlying tool by (category, action). The caller MUST have already
    set current_user_ctx — exactly the same as the streamable HTTP MCP path.
    """
    find_action(category, action)  # raises with helpful error if missing
    # Hiding a broken action only removes it from discovery -- a model that
    # already knows the name (from an earlier session, a skill, or a doc) can
    # still call it and get the raw upstream failure, which reads as a flaky
    # connector rather than a permanently unavailable feature. Refuse here, and
    # name the working alternative when there is one.
    if action in _BROKEN_ACTIONS:
        replacement = _BROKEN_REPLACEMENTS.get(action)
        detail = f" Use {replacement} instead." if replacement else ""
        raise ValueError(
            f"{action} is not available: the upstream API does not support it.{detail}"
        )
    tool_obj = mcp._tool_manager._tools[action]
    params = _resolve_param_aliases(tool_obj, params or {})
    params = _drop_null_optionals(tool_obj, params)
    params = _coerce_list_params(tool_obj, params)
    # Tool.run validates params via fn_metadata.arg_model and invokes the function.
    try:
        result = await tool_obj.run(params or {}, _tool_context())
    except Exception as exc:
        _capture_tool_failure(category, action, params or {}, exc=exc)
        raise
    _capture_tool_failure(category, action, params or {}, result=result)
    return result
