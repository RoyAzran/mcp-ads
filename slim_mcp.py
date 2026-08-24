"""Slim remote MCP surface for Claude.ai and other remote MCP clients.

Exposes only category meta-tools plus `list_actions`, while reusing the same
underlying handlers registered on the full MCP instance.
"""
from __future__ import annotations

import json
from typing import Any

from mcp.types import ToolAnnotations

from auth import current_user_ctx
from mcp_instance import build_mcp, mcp_event_properties, posthog_client
import mcp_server  # noqa: F401 - ensure full tool registry is populated for slim imports
from permissions import WRITE_TOOLS, _looks_like_write_tool
from tool_registry import _get_registry, dispatch, get_categories, list_actions


# One line per category, in the order a user is most likely to want them. Counts
# are filled in from the live registry so they cannot drift.
_CATEGORY_BLURBS: dict[str, str] = {
    "google_ads_action": "campaigns, keywords, budgets, bidding, ads, search terms, negatives, conversions",
    "meta_ads_action": "campaigns, ad sets, ads, creatives, audiences, media upload, lead forms, page posts",
    "meta_ad_library_action": "competitor research: public ads OTHER brands run on Facebook and Instagram",
    "google_ads_transparency_action": "competitor research: public ads OTHER brands run on Google Search, YouTube, Display",
    "linkedin_ads_action": "B2B campaigns, campaign groups, creatives, conversions, targeting, reporting",
    "tiktok_ads_action": "campaigns, ad groups, ads, custom audiences, pixels, creative assets, reporting",
    "microsoft_ads_action": "Bing search campaigns, ad groups, keywords, budgets, performance reports",
    "snapchat_ads_action": "campaigns, ad squads, ads, and performance stats on Snapchat",
    "wordpress_action": "posts, pages, media, WooCommerce, Elementor, SEO plugins, users, site settings",
    "ga4_action": "traffic, conversions, funnels, landing pages, audiences, custom reports",
    "gtm_action": "containers, tags, triggers, variables, versions, publishing",
    "gsc_action": "search analytics, top queries and pages, sitemaps, URL inspection",
    "agency_action": "multi-account audits, budget pacing, wasted spend, weekly reports",
    "reports_action": "composite cross-platform reports (monthly executive, full SEO, full paid)",
    "creative_action": "generate ad creative images with AI",
    "openai_ads_action": "ads shown inside ChatGPT",
    "misc_action": "anything that does not fit the categories above",
}


def _build_instructions() -> str:
    """Session-level briefing sent to the model at connect time.

    Solves a concrete complaint: operators had to re-explain the server's
    capabilities in every new conversation, and the model would insist an action
    was impossible until told otherwise several times. It could not know any
    better -- its tool list is 12 dispatchers, the ~1,900 real actions are only
    reachable through list_actions, and nothing told it that.
    """
    lines: list[str] = []
    counts = {c["category"]: c.get("tool_count", 0) for c in get_categories()}
    for category, blurb in _CATEGORY_BLURBS.items():
        if category not in counts:
            continue  # category absent on this deploy (e.g. WordPress disabled)
        lines.append(f"  {category:<20} ~{counts[category]:<5} {blurb}")
    # Any category that exists but has no hand-written blurb still gets listed.
    for category, n in sorted(counts.items()):
        if category not in _CATEGORY_BLURBS:
            lines.append(f"  {category:<20} ~{n:<5}")
    catalogue = "\n".join(lines)

    return f"""MCP Ads gives you live, authenticated access to this user's own marketing accounts \
-- Google Ads, Meta Ads, Google Analytics 4, Search Console, Google Tag Manager, \
WordPress and ChatGPT Ads -- plus cross-platform reports and agency workflows.

HOW THIS SERVER WORKS
Your tool list shows one dispatcher per platform, not the individual actions. \
There are roughly {sum(counts.values()):,} actions behind those dispatchers. To use one:

  1. list_actions(category="meta_ads_action", search="budget")
  2. meta_ads_action(action="<name returned in step 1>", params={{...}})

CATEGORIES
{catalogue}

RULES THAT MATTER
- Do not tell the user something is impossible until you have searched for it \
with list_actions. You can see 12 tools; the platforms are covered in depth. If \
a first search finds nothing, try a different word before concluding anything.
- You do not need to rediscover everything each time. Within a conversation, \
remember the action names you have already looked up and call them directly.
- Ask which account to use when the user has more than one. A Google Ads \
manager (MCC) account holds no campaign data of its own -- report on the client \
accounts beneath it instead.
- Writes hit live accounts. Anything that creates, updates, pauses, publishes or \
deletes real spend or public content should be stated plainly and confirmed \
before you run it.
- COMPETITOR RESEARCH IS NOT THE USER'S ACCOUNT. meta_ads_action and \
google_ads_action read what the user runs; meta_ad_library_action and \
google_ads_transparency_action read public archives of what EVERYONE ELSE runs. \
Never report a competitor's ads as the user's own performance, and never refuse \
competitor research because no account is connected -- none is needed. Meta's \
archive carries ordinary commercial ads for the EU and UK only, so an empty US \
or Israeli result there is Meta's rule, not a broken connection: say so and use \
google_ads_transparency_action, which covers 180+ countries.
- OFFER THE DROP ZONE THE MOMENT MEDIA COMES UP. Call media_upload_start as soon \
as the user attaches or pastes a picture or video, says they want to upload one, \
or asks for anything needing a creative -- a new image ad, swapping an ad's \
picture, refreshing a tired creative, a carousel, posting to a Page. Do not wait \
to be asked and do not ask them to find a URL first: say something like "drop \
your images or videos in the box above and I will publish them to your ad", and \
call the tool in the same turn so the box is actually there. It takes several \
files at once and video as well as images. You cannot read an attached file -- \
you are shown the picture, never handed the bytes -- so the drop zone is the \
only way media reaches an ad, and never base64 a file into a tool parameter."""


slim_mcp = build_mcp("MCP Ads", instructions=_build_instructions())

slim_mcp_analytics = None
if posthog_client is not None:
    from posthog.mcp import MCPAnalyticsOptions, instrument
    slim_mcp_analytics = instrument(
        slim_mcp, posthog_client,
        MCPAnalyticsOptions(event_properties=mcp_event_properties),
    )


# Categories the name-based classifier calls read-only but which are not. These
# are checked BEFORE the automatic derivation and always win.
#
# creative_*: every tool renders an image through the OpenAI API -- that costs
# real money per call and produces a stored asset. permissions._WRITE_NAME_VERBS
# has no "generate" verb (mcp_server._WRITE_VERBS does -- the two lists have
# drifted), so the heuristic reads creative_generate_image as harmless. Marking
# it read-only would let a client auto-approve paid image generation with no
# prompt, which is the exact failure mode annotations are supposed to prevent.
_NEVER_READ_ONLY_CATEGORIES = frozenset({"creative_action"})


def _category_is_read_only(category: str) -> bool:
    """True only when nothing this dispatcher can reach modifies anything.

    A category tool forwards an arbitrary `action` to tool_registry.dispatch(),
    so its annotation has to describe the worst thing reachable through it, not
    the typical case. Two consequences:

      * Hidden actions count. Hiding is discovery-level only -- dispatch() still
        executes hidden tools (only resolve_tool refuses them), so a hidden write
        keeps its category non-read-only.
      * It fails closed. An unknown category, or any doubt, yields False.
    """
    if category in _NEVER_READ_ONLY_CATEGORIES:
        return False
    actions = _get_registry().get(category)
    if not actions:
        return False
    return not any(
        name in WRITE_TOOLS or _looks_like_write_tool(name) for name in actions
    )


def _serialize_tool_result(result: Any) -> Any:
    if hasattr(result, "model_dump"):
        try:
            return result.model_dump(mode="json")
        except Exception:
            pass
    if hasattr(result, "dict"):
        try:
            return result.dict()
        except Exception:
            pass
    return result


def _category_title(category: str) -> str:
    return category.replace("_action", "").replace("_", " ").title()


@slim_mcp.tool(
    name="list_actions",
    title="List Actions",
    description=(
        "List available actions inside a category before calling that category tool. "
        "Returns each action's dispatch name, human title, description, parameter schema, and agent hints for account IDs, safe paused writes, and image/video uploads. "
        "Use `search` when you only know part of the action name. For long-running actions, tell the user which step is running before you call the tool."
    ),
    # Reads the in-process tool registry only: no network call, no external
    # state, same answer every time. The one genuinely read-only tool on this
    # surface, so clients that auto-approve read-only tools can skip prompting
    # for a harmless listing.
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def slim_list_actions(
    category: str,
    search: str = "",
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    return list_actions(category, search, limit, offset)


# media_upload_result is registered here as well as on the full surface. It is
# NOT part of the Apps extension -- binding it to the drop zone made the widget
# mount on a result that has no upload slot, so it waited forever, greyed out.
# Apps.tool() requires a resource_uri, so a tool that should not render the UI
# has to be registered the ordinary way, once per surface.
@slim_mcp.tool(
    name="media_upload_result",
    title="Check A Media Upload",
    description=(
        "Get the public URL of a file the user uploaded via media_upload_start. Returns "
        "status 'waiting' until they drop the file in -- tell them, do not poll in a tight "
        "loop. Pass the returned url straight to the platform media tool."
    ),
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
    ),
)
def slim_media_upload_result(upload_id: str) -> str:
    from media_app import upload_result_payload
    return json.dumps(upload_result_payload(upload_id))


def _register_category_tools() -> None:
    for item in get_categories():
        category = item["category"]
        title = f"{_category_title(category)} Actions"
        description = (
            f"{item.get('description', '').strip()} Call `list_actions` first when you "
            "need to discover the exact action name, human title, parameter schema, or image/video upload path. "
            "For long-running calls, tell the user the current step before invoking the action so the chat does not look stuck."
        ).strip()

        def _make_category_tool(bound_category: str):
            async def category_tool(
                action: str,
                params: dict[str, Any] | None = None,
            ) -> Any:
                if current_user_ctx.get() is None:
                    raise RuntimeError("No authenticated user in request context.")
                payload = params or {}
                if action == "list_actions":
                    return list_actions(
                        bound_category,
                        str(payload.get("search", "") or ""),
                        int(payload.get("limit", 200) or 200),
                        int(payload.get("offset", 0) or 0),
                    )
                result = await dispatch(bound_category, action, payload)
                return _serialize_tool_result(result)

            return category_tool

        read_only = _category_is_read_only(category)
        slim_mcp.tool(
            name=category,
            title=title,
            description=description,
            # Every one of these reaches a live third-party ad/analytics/CMS
            # account, so openWorldHint is always true. destructiveHint is the
            # inverse of read_only rather than a separate judgement: if any
            # reachable action writes, the dispatcher can delete or overwrite
            # through it. idempotentHint is deliberately left unset -- it
            # depends entirely on which action is passed, so any single value
            # would be a lie for most calls.
            annotations=ToolAnnotations(
                readOnlyHint=read_only,
                destructiveHint=not read_only,
                openWorldHint=True,
            ),
        )(_make_category_tool(category))


_register_category_tools()