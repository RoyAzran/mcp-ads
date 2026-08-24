"""
RBAC permission definitions.

Roles:
  viewer  — read-only tools on all platforms
  editor  — read + write tools
  admin   — everything + admin REST API

Tool names that require at least editor role (write tools).
Everything NOT in WRITE_TOOLS is implicitly available to all roles.
"""
from enum import Enum


class Role(str, Enum):
    viewer = "viewer"
    editor = "editor"
    admin = "admin"


# ---------------------------------------------------------------------------
# Tools that mutate data — blocked for "viewer" role
# ---------------------------------------------------------------------------
WRITE_TOOLS: set[str] = {
    # ── Google Ads ──────────────────────────────────────────────────────────
    "google_ads_create_budget",
    "google_ads_create_campaign",
    "google_ads_create_adgroup",
    "google_ads_add_keywords",
    "google_ads_add_negative_keywords",
    "google_ads_update_campaign_status",
    "google_ads_update_adgroup_status",
    "google_ads_update_keyword_status",
    "google_ads_update_keyword_bid",
    "google_ads_update_campaign_budget",
    "google_ads_create_responsive_search_ad",
    "google_ads_pause_all_campaigns",
    "google_ads_enable_campaign",
    "google_ads_apply_recommendation",
    "google_ads_dismiss_recommendation",
    "google_ads_upload_image_asset",
    "google_ads_create_responsive_display_ad",
    "google_ads_create_asset_group",
    "google_ads_create_conversion_action",
    "google_ads_update_conversion_action",
    "google_ads_create_sitelink_asset",
    "google_ads_create_callout_asset",
    "google_ads_create_video_ad",
    "google_ads_bulk_create_keywords",
    "google_ads_create_label",
    "google_ads_apply_label",
    # ── Google Ads — Agency / MCC ─────────────────────────────────────────
    "google_ads_create_shared_negative_list",
    "google_ads_apply_shared_negative_list",
    "google_ads_upload_customer_match",
    "google_ads_upload_offline_conversions",
    "google_ads_copy_campaign",
    # ── Meta Ads ────────────────────────────────────────────────────────────
    "meta_ads_update_status",
    "meta_ads_update_budget",
    "meta_ads_update_adset_budget",
    "meta_ads_switch_budget_mode",
    "meta_ads_create_campaign",
    "meta_ads_create_lead_campaign",
    "meta_ads_create_adset",
    "meta_ads_create_lead_adset",
    "meta_ads_create_ad_creative",
    "meta_ads_upload_ad_video",
    "meta_upload_ad_video",
    "meta_ads_upload_video_file_bytes_to_meta",
    "meta_ads_create_video_ad_creative",
    "meta_ads_create_video_ad_from_media",
    "meta_ads_create_video_ad_from_source",
    "meta_create_video_ad",
    "meta_ads_test_media_upload_flow",
    "meta_ads_create_ad",
    "meta_create_ad",
    "meta_ads_duplicate_campaign",
    "meta_ads_duplicate_adset",
    "meta_ads_update_adset_targeting",
    "meta_ads_create_audience",
    "meta_ads_create_lookalike",
    "meta_ads_delete_ad",
    "meta_ads_delete_adset",
    "meta_ads_delete_campaign",
    "meta_ads_create_pixel",
    "meta_ads_send_server_event",
    "meta_ads_create_custom_conversion",
    "meta_ads_upload_customer_list_to_audience",
    "meta_ads_delete_audience",
    "meta_ads_update_audience",
    "meta_ads_create_website_audience",
    "meta_ads_create_engagement_audience",
    "meta_ads_update_ad_creative",
    "meta_ads_upload_image_to_meta",
    "meta_upload_ad_image",
    "meta_ads_upload_image_file_bytes_to_meta",
    "meta_ads_create_image_ad_from_media",
    "meta_ads_create_image_ad_from_source",
    "meta_create_image_ad",
    "meta_ads_create_carousel_ad_creative",
    "meta_ads_create_story_ad_creative",
    "meta_ads_create_collection_ad_creative",
    "meta_ads_bulk_updater",
    "meta_ads_adset_matrix_builder",
    "meta_ads_create_automated_rule",
    "meta_ads_update_automated_rule",
    "meta_ads_delete_automated_rule",
    "meta_ads_create_ab_test",
    "meta_ads_create_adset_with_dayparting",
    "meta_ads_create_advantage_plus_campaign",
    "meta_ads_copy_ad_to_adset",
    "meta_ads_add_ad_account_user",
    "meta_ads_remove_ad_account_user",
    "meta_ads_update_ad",
    "meta_ads_apply_recommendation",
    "meta_ads_create_advantage_plus_creative",
    "meta_ads_boost_existing_post",
    "meta_ads_create_leadgen_form",
    "meta_ads_create_lead_form",
    "meta_ads_create_lead_ad_creative",
    "meta_ads_create_lead_ad_from_form",
    "meta_ads_create_lead_form_and_paused_ad",
    "meta_ads_set_adset_budget_guardrails",
    "meta_ads_create_product_set",
    "meta_ads_create_product_feed",
    "meta_ads_upload_product_feed",
    "meta_ads_update_catalog_product",
    "meta_ads_create_saved_audience",
    "meta_ads_upload_enhanced_conversions",
    "meta_ads_create_system_user",
    "meta_ads_render_report_pdf",
    "meta_ads_render_report_html",
    "meta_google_drive_upload_video_to_meta",
    # LinkedIn Ads via Pipedream
    "linkedin_ads_create_campaign",
    "linkedin_ads_update_campaign",
    "linkedin_ads_create_creative",
    "linkedin_ads_create_campaign_group",
    "linkedin_ads_update_campaign_group",
    "linkedin_ads_update_creative",
    "linkedin_ads_create_conversion",
    "linkedin_ads_update_conversion",
    "linkedin_ads_raw_request",
    # TikTok Ads via Pipedream
    "tiktok_ads_create_campaign",
    "tiktok_ads_update_campaign",
    "tiktok_ads_create_adgroup",
    "tiktok_ads_update_adgroup",
    "tiktok_ads_create_ad",
    "tiktok_ads_update_ad",
    "tiktok_ads_create_pixel",
    "tiktok_ads_update_pixel",
    "tiktok_ads_create_custom_audience",
    "tiktok_ads_update_custom_audience",
    "tiktok_ads_delete_custom_audience",
    "tiktok_ads_create_lookalike_audience",
    "tiktok_ads_upload_image",
    "tiktok_ads_upload_video",
    "tiktok_ads_raw_request",
    # Snapchat Ads via Pipedream
    "snapchat_ads_create_campaign",
    "snapchat_ads_update_campaign_status",
    "snapchat_ads_raw_request",
    # Microsoft Advertising (Bing) via Pipedream
    "microsoft_ads_update_campaign_status",
    "microsoft_ads_update_campaign_budget",
    "microsoft_ads_raw_request",
    # ── Meta Pages ───────────────────────────────────────────────────────────
    "meta_ads_create_page_post",
    "meta_ads_update_page_post",
    "meta_ads_delete_page_post",
    "meta_ads_reply_to_comment",
    "meta_ads_hide_comment",
    "meta_ads_delete_comment",
    "meta_ads_reply_to_page_message",
    "meta_ads_create_page_event",
    "meta_ads_update_page_info",
    "meta_ads_add_page_role",
    "meta_ads_remove_page_role",
    "meta_ads_block_page_user",
    "meta_ads_unblock_page_user",
    "meta_ads_create_instagram_post",
    "meta_ads_publish_page_post_to_instagram",
    "meta_ads_set_page_publish_status",
    "meta_ads_schedule_instagram_post",
    "meta_ads_delete_instagram_post",
    "meta_ads_reply_to_instagram_comment",
    "meta_ads_delete_instagram_comment",
    "meta_ads_create_page_photo_album",
    "meta_ads_pin_post",
    "meta_ads_send_whatsapp_message",
    "meta_ads_create_publisher_block_list",
    "meta_ads_set_page_cta",
    "meta_ads_create_instagram_reel",
    "meta_ads_create_instagram_story",
    "meta_ads_create_live_video",
    "meta_ads_publish_live_video",
    # ── Google Search Console ────────────────────────────────────────────────
    "gsc_submit_sitemap",
    "gsc_delete_sitemap",
    # ── Google Sheets ────────────────────────────────────────────────────────
    "sheets_create_spreadsheet",
    "sheets_delete_spreadsheet",
    "sheets_create_sheet",
    "sheets_delete_sheet",
    "sheets_rename_sheet",
    "sheets_update_cells",
    "sheets_update_multiple_ranges",
    "sheets_append_rows",
    "sheets_clear_range",
    "sheets_copy_range",
    "sheets_move_range",
    "sheets_insert_rows",
    "sheets_delete_rows",
    "sheets_insert_columns",
    "sheets_delete_columns",
    "sheets_format_range",
    "sheets_set_borders",
    "sheets_merge_cells",
    "sheets_unmerge_cells",
    "sheets_clear_formatting",
    "sheets_freeze_panes",
    "sheets_sort_range",
    "sheets_set_basic_filter",
    "sheets_clear_basic_filter",
    "sheets_group_rows",
    "sheets_group_columns",
    "sheets_add_conditional_format_cell_value",
    "sheets_add_conditional_format_color_scale",
    "sheets_delete_conditional_formats",
    "sheets_set_data_validation_list",
    "sheets_set_data_validation_number",
    "sheets_delete_data_validation",
    "sheets_create_named_range",
    "sheets_delete_named_range",
    "sheets_protect_range",
    "sheets_remove_protection",
    "sheets_add_note",
    "sheets_clear_notes",
    "sheets_add_chart",
    "sheets_delete_chart",
    "sheets_find_and_replace",
    "sheets_create_pivot_table",
    "sheets_set_hyperlink",
    "sheets_batch_requests",
    "sheets_share_spreadsheet",
    "sheets_copy_spreadsheet",
    "sheets_move_sheet",
    "sheets_copy_sheet",
    "sheets_hide_sheet",
    "sheets_show_sheet",
    "sheets_set_sheet_tab_color",
    "sheets_set_column_width",
    "sheets_set_row_height",
    "sheets_auto_resize_columns",
    "sheets_hide_rows",
    "sheets_show_rows",
    "sheets_hide_columns",
    "sheets_show_columns",
    # ── Agency / Business Manager ─────────────────────────────────────────
    "meta_business_assign_pixel",
    "meta_business_share_audience",
    "meta_business_add_user",
    "meta_business_grant_account_access",
}


_WRITE_NAME_VERBS = {
    "add",
    "apply",
    "assign",
    "attach",
    "block",
    "boost",
    "bulk",
    "clear",
    "copy",
    "create",
    "delete",
    "dismiss",
    "duplicate",
    "grant",
    "hide",
    "insert",
    "merge",
    "move",
    "pause",
    "protect",
    "publish",
    "remove",
    "rename",
    "reply",
    "resume",
    "schedule",
    "send",
    "set",
    "share",
    "sort",
    "switch",
    "unblock",
    "unhide",
    "unmerge",
    "unlink",
    "update",
    "upload",
    "watch",
}

# WordPress write tools, generated by scripts/audit_wordpress_tools.py from the
# HTTP verb each tool actually issues -- never from its name. Verb-second names
# like woo_product_create read as a "get" to the name heuristic, which would let
# a viewer mutate a live site.
try:
    from wordpress_generated_sets import WORDPRESS_WRITE_TOOLS as _WORDPRESS_WRITE_TOOLS
except ImportError:  # WordPress tools disabled via WORDPRESS_TOOLS_ENABLED
    _WORDPRESS_WRITE_TOOLS = set()

WRITE_TOOLS |= _WORDPRESS_WRITE_TOOLS

_TOOL_PREFIXES = (
    # Both must precede the broader family prefix below them, for the same
    # reason meta_pages_ does: the loop strips the FIRST prefix that matches, and
    # "google_ads_" would leave "transparency_search_ads" whose first word is
    # "transparency". These are all reads either way, but relying on that is
    # relying on an accident -- the next tool named *_export_* would flip.
    "google_ads_transparency_",
    "meta_ad_library_",
    "google_ads_",
    "gads_",
    "meta_ads_",
    # Must precede the bare "meta_": the loop strips the first prefix that
    # matches, and "meta_" would leave "pages_create_post", whose first word
    # is "pages" -- not a write verb. That is how linkedin_organic_create_post
    # came to be classified READ.
    "meta_pages_",
    "meta_",
    "linkedin_ads_",
    "tiktok_ads_",
    # Organic posting. Without these the verb heuristic reads the platform
    # name as the verb ("linkedin" is not a write verb) and classified
    # linkedin_organic_create_post as READ -- which would have let the agent
    # brain post to a client's feed without an approved proposal.
    "linkedin_organic_",
    "tiktok_organic_",
    "snapchat_ads_",
    "microsoft_ads_",
    "openai_ads_",
    "ga4_",
    "gsc_",
    "sheets_",
    "gtm_",
    # WordPress. Only the wordpress_* prefix is listed for now -- the wider
    # families (wp_, woo_, elementor_, ...) arrive with the tool port, and their
    # write classification is generated from the HTTP verb each tool actually
    # issues rather than inferred from the name.
    "wordpress_",
)


def _looks_like_write_tool(tool_name: str) -> bool:
    """Fail closed for obvious write tools missing from WRITE_TOOLS."""
    remainder = tool_name
    for prefix in _TOOL_PREFIXES:
        if remainder.startswith(prefix):
            remainder = remainder[len(prefix):]
            break

    first = remainder.split("_", 1)[0].lower()
    if first in _WRITE_NAME_VERBS:
        return True

    # Common alias pattern, e.g. meta_create_image_ad.
    for prefix in ("meta_",):
        if tool_name.startswith(prefix):
            alias_first = tool_name[len(prefix):].split("_", 1)[0].lower()
            return alias_first in _WRITE_NAME_VERBS

    return False


def check_permission(tool_name: str, role: str) -> bool:
    """Return True if the role is allowed to call this tool."""
    if role in (Role.editor, Role.admin):
        return True
    # viewer: only read tools
    if role == Role.viewer:
        return tool_name not in WRITE_TOOLS and not _looks_like_write_tool(tool_name)
    return False


def require_editor(tool_name: str = "") -> str | None:
    """
    Check current user from context and return an error JSON string if denied.
    Returns None if the call is allowed.
    """
    from auth import current_user_ctx
    user = current_user_ctx.get(None)
    if user is None:
        import json
        return json.dumps({"error": "Not authenticated. Connect to the MCP server using a valid JWT."})
    if not check_permission(tool_name, user.role):
        import json
        return json.dumps({"error": f"Access denied: '{user.role}' role cannot call '{tool_name}'. Requires editor or admin role."})
    return None
