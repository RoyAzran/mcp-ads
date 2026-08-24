"""
Tool registration module.
Importing this file causes all tool modules to register their @mcp.tool() handlers
onto the shared mcp singleton from mcp_instance.py.
Keep imports in this file only — do not import mcp_server from tool files.
"""
import os
import re
from pathlib import Path

from mcp_instance import mcp
from credentials import connect_hint
from permissions import WRITE_TOOLS

import tools.google_ads              # noqa: F401
import tools.google_ads_advanced     # noqa: F401
import tools.google_ads_conversions  # noqa: F401
import tools.google_ads_gads         # noqa: F401
import tools.google_ads_negatives    # noqa: F401
import tools.google_ads_management   # noqa: F401
import tools.meta_ads                # noqa: F401
# Competitor research: public ad archives, read-only, nobody's own account.
import tools.meta_ad_library        # noqa: F401
import tools.google_ads_transparency  # noqa: F401
import tools.pipedream_ads           # noqa: F401
import tools.organic_social          # noqa: F401
import tools.snapchat_ads            # noqa: F401
import tools.microsoft_ads           # noqa: F401
import tools.openai_ads              # noqa: F401
import tools.ga4                     # noqa: F401
import tools.ga4_advanced            # noqa: F401
import tools.ga4_agency              # noqa: F401
import tools.gsc                     # noqa: F401
import tools.gsc_advanced            # noqa: F401
import tools.gtm                     # noqa: F401
import tools.agency                  # noqa: F401
import tools.reports                 # noqa: F401
import tools.creative                # noqa: F401
import tools.media                   # noqa: F401

# WordPress is import-guarded so it can be switched off on a live service with
# `gcloud run services update --update-env-vars WORDPRESS_TOOLS_ENABLED=false`
# -- no rebuild, no revert -- if the added import cost or tool volume causes
# trouble. Defaults to on.
if os.environ.get("WORDPRESS_TOOLS_ENABLED", "true").strip().lower() not in {"0", "false", "no"}:
    import tools.wordpress           # noqa: F401


_SKILL_PATH = Path(__file__).resolve().parent / "static" / "skills" / "mcp-ads-skill.md"


def _read_mcp_ads_skill() -> str:
	try:
		return _SKILL_PATH.read_text(encoding="utf-8")
	except Exception:
		return (
			"# MCP Ads Skill\n\n"
			"Check connected platforms first, read current account state before writes, "
			"keep new campaigns paused unless the user explicitly asks to launch, and "
			f"send users to {connect_hint()} when a platform is not connected."
		)


@mcp.resource(
	"mcp-ads://skill",
	name="mcp_ads_skill",
	title="MCP Ads Skill",
	description="Operational instructions for AI clients using MCP Ads tools.",
	mime_type="text/markdown",
)
def mcp_ads_skill_resource() -> str:
	return _read_mcp_ads_skill()


@mcp.prompt(
	name="mcp_ads_operating_instructions",
	title="MCP Ads Operating Instructions",
	description="Load the recommended workflow and safety instructions before managing ad accounts with MCP Ads.",
)
def mcp_ads_operating_instructions() -> str:
	return _read_mcp_ads_skill()


_FAMILY_PREFIXES = [
	# Before google_ads_/meta_ads_: _tool_family returns the FIRST match, so the
	# broader family would otherwise swallow these and title them
	# "Google Ads: Transparency Find Advertiser".
	("google_ads_transparency_", "Google Ads Transparency"),
	("meta_ad_library_", "Meta Ad Library"),
	("gads_", "Google Ads"),
	("google_ads_", "Google Ads"),
	("meta_ads_", "Meta Ads"),
	# Before the other meta_ prefixes for the same reason permissions.py needs
	# it first: whichever prefix matches decides which word is read as the verb.
	("meta_pages_", "Facebook & Instagram pages"),
	("meta_upload_", "Meta Ads"),
	("meta_create_", "Meta Ads"),
	("linkedin_ads_", "LinkedIn Ads"),
	("tiktok_ads_", "TikTok Ads"),
	# Organic posting. Without these the family lookup falls through to the
	# generic branch, which then reads "linkedin" as the verb -- so
	# linkedin_organic_create_post was published to the model as
	# "[READ] Tool tool." while permissions.py correctly treated it as a write.
	("linkedin_organic_", "LinkedIn (organic)"),
	("tiktok_organic_", "TikTok (organic)"),
	("snapchat_ads_", "Snapchat Ads"),
	("microsoft_ads_", "Microsoft Advertising"),
	("openai_ads_", "OpenAI Ads"),
	("ga4_", "GA4"),
	("get_ga_", "GA4 Preset"),
	("gsc_", "Search Console"),
	("wordpress_", "WordPress"),
	# Without these the WordPress families fall through to the generic branch and
	# render as "Tool: Woo Create Product" / "[WRITE] Tool tool." in the schema
	# the model actually reads.
	("woo_", "WooCommerce"),
	("wc_", "WooCommerce"),
	("wp_", "WordPress"),
	("elementor_", "Elementor"),
	("woodmart_", "WoodMart"),
	("yoast_", "Yoast SEO"),
	("rankmath_", "Rank Math"),
	("aioseo_", "All in One SEO"),
	("aiseo_", "AI SEO"),
	("seopress_", "SEOPress"),
	("acf_", "ACF"),
	("creative_", "Creative"),
	("generate_", "Creative"),
	("agency_", "Agency"),
	("report_", "Composite Report"),
]

_WRITE_VERBS = {
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
	"enable",
	"freeze",
	"format",
	"generate",
	"group",
	"hide",
	"insert",
	"link",
	"merge",
	"move",
	"pause",
	"protect",
	"publish",
	"remove",
	"rename",
	"render",
	"reply",
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

_TOKEN_LABELS = {
	"ab": "A/B",
	"ads": "Ads",
	"ad": "Ad",
	"adset": "Ad Set",
	"adsets": "Ad Sets",
	"api": "API",
	"audience": "Audience",
	"audiences": "Audiences",
	"cta": "CTA",
	"ctr": "CTR",
	"cpc": "CPC",
	"cpm": "CPM",
	"csv": "CSV",
	"ga4": "GA4",
	"gads": "Google Ads",
	"gsc": "GSC",
	"gtm": "GTM",
	"html": "HTML",
	"id": "ID",
	"ids": "IDs",
	"json": "JSON",
	"mcc": "MCC",
	"meta": "Meta",
	"og": "OG",
	"pdf": "PDF",
	"pmax": "PMax",
	"roas": "ROAS",
	"rsa": "RSA",
	"rda": "RDA",
	"seo": "SEO",
	"url": "URL",
	"urls": "URLs",
	"utm": "UTM",
	"video": "Video",
}

_SPECIAL_METADATA = {
	"ga4_list_properties": (
		"GA4: List Accessible Properties",
		"[READ] List every GA4 property the authenticated user can access. Use this to discover valid property_id values before calling report tools.",
	),
	"ga4_list_all_properties": (
		"GA4: List Accessible Properties (Alias)",
		"[READ][ALIAS] Legacy alias for ga4_list_properties. Same behavior. Prefer ga4_list_properties because it is the primary discovery tool for property_id values.",
	),
	"ga4_run_custom_report": (
		"GA4: Run Custom Report",
		"[READ] Run a fully custom GA4 report with your own dimensions and metrics. Use this only when preset GA4 tools do not fit the request. For common asks like overview, traffic sources, top pages, or conversions, prefer the dedicated GA4 tools.",
	),
	"creative_generate_image": (
		"Creative: Generate Image",
		"[WRITE][RECOMMENDED] Step 1 of the normal image-ad flow. Generate an image, show the preview in chat, then pass generated_asset_id to meta_create_image_ad or meta_ads_create_image_ad_from_media. For long runs, tell the user you are generating the image before calling the tool. Never use shell commands to base64 local files from this result.",
	),
	"creative_generate_ad_images": (
		"Creative: Generate Ad Images",
		"[WRITE][RECOMMENDED] Step 1 of the normal image-ad flow when the user gives structured ad inputs. Generate one or more ad images, show previews in chat, then pass generated_asset_id from the chosen image into the Meta image-ad tools. For long runs, tell the user which step is running before calling the tool.",
	),
	"meta_ads_get_upload_url": (
		"Meta Ads: Get Signed Upload URL For Local Files",
		"[WRITE][RECOMMENDED] Use this when the user wants to upload a local file (image or video) to Meta Ads. Returns a signed GCS upload URL. Two-step workflow: (1) call this tool with the filename, (2) use curl/PowerShell to PUT the file to the returned upload_url, (3) pass the returned public_url to meta_upload_ad_image or meta_upload_ad_video as source. This avoids base64 size limits and works with any file size.",
	),
	"meta_upload_ad_image": (
		"Meta Ads: Upload Image Only",
		"[WRITE][PRIMARY][RECOMMENDED][ALIAS] First-choice image upload tool for Meta Ads. Use this when the user says upload an image, attached a photo, pasted a public image URL, or wants an image available in the Meta library. If the user gives a public URL, pass that URL directly to this tool instead of refusing to fetch it yourself. If the user attached a file, pass the attachment as data URI/raw base64 bytes in source. Accepts source as a local file path (e.g. /path/to/ad.png or C:\\Users\\me\\ad.jpg), public URL, data URI/raw base64 bytes, existing image_hash, or generated_asset_id. account_id is the primary parameter; ad_account_id is also accepted as a legacy alias. Returns image_hash for creatives. Does not create a creative or ad.",
	),
	"meta_ads_list_image_assets": (
		"Meta Ads: Browse Image Library",
		"[READ][PRIMARY][RECOMMENDED] First-choice tool when the user asks to show, list, browse, or pick their photos/images from the Meta Ads library or Ads Manager. Use this immediately and present the returned thumbnails visually on the first response, not as file names only. Returns thumbnail_url/preview_url, image_hash, size, and lightweight card data for rendering.",
	),
	"meta_ads_list_video_assets": (
		"Meta Ads: Browse Video Library",
		"[READ][PRIMARY][RECOMMENDED] First-choice tool when the user asks to show, list, browse, or pick their videos from the Meta Ads library or Ads Manager. Use this immediately and present the returned thumbnails visually on the first response. Returns thumbnail_url/preview_url, readiness/status, length, and lightweight card data for rendering.",
	),
	"meta_ads_list_creative_assets": (
		"Meta Ads: Browse Creative Library",
		"[READ][RECOMMENDED] Show existing Meta ad creatives in a gallery-friendly shape. Use this when the user asks to browse, view, or pick existing creatives. Returns thumbnail_url/preview_url, copy fields, type, and lightweight card data for rendering.",
	),
	"meta_ads_media_library": (
		"Meta Ads: Open Media Library",
		"[READ][PRIMARY][RECOMMENDED] First-choice tool when the user asks for their Meta media library, ads library, assets library, or a combined view of photos/videos/creatives. Returns image, video, and creative sections together so clients can render one visual picker instead of separate lists.",
	),
	"meta_ads_upload_image_file_bytes_to_meta": (
		"Meta Ads: Upload Image Bytes",
		"[WRITE][ADVANCED] Raw-bytes-only image uploader. Do not choose this for normal chat requests when meta_upload_ad_image can handle the same job. Use only when you explicitly have raw image bytes and only want image_hash back. Primary params are file_bytes_base64 and account_id. Legacy aliases image_base64 and ad_account_id are also accepted. Do not pass local file paths.",
	),
	"meta_ads_upload_video_file_bytes_to_meta": (
		"Meta Ads: Upload Video Bytes",
		"[WRITE][ADVANCED] Raw-bytes-only video uploader. For normal chat upload requests, prefer meta_upload_ad_video because it accepts public URLs and source values. Use this only when you explicitly have raw video bytes or a data URI and want a video_id in the Meta video library. Pass file_bytes_base64 as raw base64 or data:video/mp4;base64,...; do not pass local file paths. account_id is the primary account parameter; video_base64 and ad_account_id are legacy aliases.",
	),
	"meta_ads_test_media_upload_flow": (
		"Meta Ads: Test Media Upload Flow",
		"[WRITE][RECOMMENDED] End-to-end safe media smoke test. Uploads or reuses image/video assets, creates test creatives, and can create PAUSED test ads. Use this when validating image/video upload wiring across a connected account. Sources can be public URLs, data URI/raw base64 payloads, existing image_hash/video_id values, or generated assets where supported.",
	),
	"google_ads_upload_image_asset": (
		"Google Ads: Upload Image Asset",
		"[WRITE][RECOMMENDED] Upload a public HTTPS image URL into Google Ads as an image asset. Use this before display/PMax/image-ad creation when the ad tool needs an asset_id or asset resource name. Do not pass local file paths; host the image publicly or use a supported generated/upload workflow first.",
	),
	"google_ads_create_video_ad": (
		"Google Ads: Create Video Ad From YouTube Video",
		"[WRITE] Create a video ad from an existing YouTube video ID. This does not upload a raw video file to Google Ads. If the user has a video file, upload it to YouTube or use the platform's supported video asset flow first, then pass youtube_video_id here. Keep the ad/campaign PAUSED unless the user explicitly asks to launch.",
	),
	"meta_create_image_ad": (
		"Meta Ads: Create Image Ad From One Source",
		"[WRITE][RECOMMENDED][ALIAS] Use this when the user wants to create the image ad, not just upload the asset. Uploads or reuses an image, creates an image creative, and optionally creates a PAUSED ad when adset_id is supplied. Prefer generated_asset_id from the creative tools when the image was just created in chat. Source may be a local file path, public URL, data URI/raw base64 bytes, or existing image_hash. Requires page_id, message, and destination link.",
	),
	"meta_ads_create_image_ad_from_media": (
		"Meta Ads: Upload Image And Create Ad",
		"[WRITE][RECOMMENDED] Full image-ad workflow. Use this when the user wants the image uploaded and the creative/ad created in one step. Prefer meta_upload_ad_image when the user only wants the asset in the library. Prefer generated_asset_id from the creative tools when the image was just created in chat. Source accepts local file paths, public URLs, data URI/raw base64, or existing image_hash.",
	),
	"meta_ads_create_lead_form": (
		"Meta Ads: Create Lead Form",
		"[WRITE] Create a lead form on a connected Facebook Page. Requires page_id, name, and privacy_policy_url. questions may be a JSON string or JSON array. If page_id is unknown, call meta_ads_find_page or meta_ads_pages first. For complete lead ads, prefer meta_ads_create_lead_form_and_paused_ad.",
	),
	"meta_ads_create_lead_form_and_paused_ad": (
		"Meta Ads: Create Lead Form And Paused Ad",
		"[WRITE][RECOMMENDED] Complete lead-gen workflow: create lead form, creative, and PAUSED ad. Requires account_id, page_id, adset_id, privacy_policy_url, message, and headline. questions may be a JSON string or JSON array.",
	),
	"meta_create_video_ad": (
		"Meta Ads: Upload Source And Create Video Ad",
		"[WRITE][ALIAS] Create a Meta video ad from a source asset. This is not upload-only: it uploads or reuses media, creates the creative, and can create the ad. Prefer meta_upload_ad_video when the user only wants the asset.",
	),
	"meta_upload_ad_video": (
		"Meta Ads: Upload Video Only",
		"[WRITE][PRIMARY][RECOMMENDED][ALIAS] First-choice video upload tool for Meta Ads. Use this when the user says upload a video, attached a video, or pasted a public video URL. If the user gives a public URL, pass that URL directly to this tool instead of refusing to fetch it yourself. Accepts source as a local file path (e.g. /path/to/ad.mp4 or C:\\Users\\me\\ad.mp4), public URL, data URI/raw base64 video bytes, or existing video_id. account_id is the primary parameter; ad_account_id is also accepted as a legacy alias. Does not create a creative or ad.",
	),
	"meta_create_ad": (
		"Meta Ads: Create Ad From Source",
		"[WRITE][ALIAS] Create a Meta ad from a source asset with auto-detection. This is a creation workflow, not an upload-only tool. Prefer the upload-only aliases when the user only wants image_hash or video_id.",
	),
}

_PRESET_WINDOW_LABELS = {
	"today": "today",
	"yesterday": "yesterday",
	"7d": "the last 7 days",
	"30d": "the last 30 days",
	"90d": "the last 90 days",
	"this_month": "this month to date",
	"last_month": "the last full month",
	"ytd": "year to date",
}


def _tool_family(name: str) -> tuple[str, str, str]:
	for prefix, family in _FAMILY_PREFIXES:
		if name.startswith(prefix):
			return prefix, family, name[len(prefix):]
	return "", "Tool", name


def _is_write_tool(name: str) -> bool:
	if name in WRITE_TOOLS:
		return True
	_, _, remainder = _tool_family(name)
	first = remainder.split("_", 1)[0].lower()
	return first in _WRITE_VERBS


def _humanize_tokens(tokens: list[str]) -> str:
	words: list[str] = []
	for token in tokens:
		low = token.lower()
		if low in _TOKEN_LABELS:
			words.append(_TOKEN_LABELS[low])
		else:
			words.append(low.capitalize())
	return " ".join(words)


def _default_title(name: str) -> str:
	prefix, family, remainder = _tool_family(name)
	match = re.match(r"^get_ga_(?P<subject>.+)_(?P<window>today|yesterday|7d|30d|90d|this_month|last_month|ytd)$", name)
	if match:
		subject = _humanize_tokens(match.group("subject").split("_"))
		window_map = {
			"today": "Today",
			"yesterday": "Yesterday",
			"7d": "Last 7 Days",
			"30d": "Last 30 Days",
			"90d": "Last 90 Days",
			"this_month": "This Month",
			"last_month": "Last Month",
			"ytd": "Year To Date",
		}
		window = window_map.get(match.group("window"), match.group("window"))
		return f"GA4 Preset: {subject} ({window})"

	remainder_tokens = [token for token in remainder.split("_") if token]
	if not remainder_tokens:
		return family

	title = _humanize_tokens(remainder_tokens)
	if prefix in ("gads_", "google_ads_"):
		return f"Google Ads: {title}"
	return f"{family}: {title}"


def _default_description(name: str, existing: str) -> str:
	if existing.lstrip().startswith("["):
		return existing

	prefix, family, _ = _tool_family(name)
	role_tag = "[WRITE]" if _is_write_tool(name) else "[READ]"

	match = re.match(r"^get_ga_(?P<subject>.+)_(?P<window>today|yesterday|7d|30d|90d|this_month|last_month|ytd)$", name)
	if match:
		subject = _humanize_tokens(match.group("subject").split("_"))
		window = _PRESET_WINDOW_LABELS.get(match.group("window"), match.group("window").replace("_", " "))
		return (
			f"{role_tag}[PRESET] Fixed-date GA4 preset for {subject.lower()} over {window}. "
			"Use this when the user explicitly asks for that exact time window. "
			"For flexible date ranges or custom fields, prefer the main ga4_* tools or ga4_run_custom_report()."
		)

	family_note = {
		"gads_": "Google Ads tool (extended/low-level coverage). For common tasks prefer the RECOMMENDED google_ads_* actions; use these only for advanced operations those don't cover.",
		"google_ads_": "Google Ads tool. Prefer the actions marked RECOMMENDED for everyday Google Ads workflows.",
		"meta_ads_": "Meta Ads tool.",
		"linkedin_ads_": "LinkedIn Ads tool backed by Pipedream managed auth.",
		"tiktok_ads_": "TikTok Ads tool backed by Pipedream managed auth.",
		"snapchat_ads_": "Snapchat Ads tool backed by Pipedream managed auth.",
		"microsoft_ads_": "Microsoft Advertising (Bing Ads) tool backed by Pipedream managed auth; also needs a server developer token.",
		"openai_ads_": "OpenAI Ads tool — manages ads shown inside ChatGPT via the OpenAI Ads API.",
		"ga4_": "GA4 analytics tool.",
		"gsc_": "Search Console tool.",
		"agency_": "Agency or multi-platform tool.",
		"report_": "Composite multi-platform report tool.",
	}.get(prefix, f"{family} tool.")

	cleaned = " ".join((existing or "").split())
	if cleaned:
		return f"{role_tag} {family_note} {cleaned}"
	return f"{role_tag} {family_note}"


def _normalize_tool_registry() -> None:
	tools = getattr(mcp, "_tool_manager")._tools
	for name, tool in tools.items():
		if name in _SPECIAL_METADATA:
			title, description = _SPECIAL_METADATA[name]
			tool.title = title
			tool.description = description
			continue

		if not tool.title:
			tool.title = _default_title(name)

		tool.description = _default_description(name, tool.description or "")


_normalize_tool_registry()
