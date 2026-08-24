"""Organic social posting for LinkedIn and TikTok, via Pipedream Connect.

The ads apps cannot do this: a sponsored-account token posts to campaigns, not
to a feed. These tools go through the plain "linkedin" and "tiktok" Pipedream
apps, so the user connects in the browser and this server never stores their
social tokens.

Every posting tool here is a write tool by name (``*_create_post`` /
``*_publish_*``), which is what keeps the agent brain from calling one
directly -- content reaches these only after a human approves the draft.
"""
import json
from typing import Any

from mcp_instance import mcp
from credentials import connect_hint
from permissions import require_editor
from tools.platform_http import (
    _json,
    _parse_json_arg,
    _platform_base,
    list_accounts as _list_pipedream_accounts,
    request as _proxy_request,
)

# LinkedIn versions its REST API by month header; posts moved to /rest/posts.
_LINKEDIN_VERSION = "202405"


def _linkedin_headers() -> dict:
    return {"LinkedIn-Version": _LINKEDIN_VERSION, "X-Restli-Protocol-Version": "2.0.0"}


def _author_urn(author_id: str, author_type: str) -> str:
    value = str(author_id).strip()
    if value.startswith("urn:li:"):
        return value
    kind = "organization" if author_type == "organization" else "person"
    return f"urn:li:{kind}:{value}"


@mcp.tool()
def linkedin_organic_accounts() -> str:
    """List connected Pipedream accounts usable for organic LinkedIn posting."""
    try:
        accounts = _list_pipedream_accounts("linkedin_organic")
        return _json({"accounts": accounts, "total": len(accounts)})
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def linkedin_organic_list_organizations(pipedream_account_id: str = "") -> str:
    """List LinkedIn organizations (company pages) the connected user can post as."""
    try:
        return _json(_proxy_request(
            "linkedin_organic", "GET",
            _platform_base("linkedin_organic", "/rest/organizationAcls"),
            pipedream_account_id,
            params={"q": "roleAssignee", "role": "ADMINISTRATOR", "state": "APPROVED"},
            headers=_linkedin_headers(),
        ))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def linkedin_organic_create_post(
    author_id: str,
    text: str,
    author_type: str = "organization",
    visibility: str = "PUBLIC",
    article_url: str = "",
    article_title: str = "",
    pipedream_account_id: str = "",
) -> str:
    """Publish an organic post to a LinkedIn feed or company page.

    Args:
        author_id: Organization or person id (bare id or full urn:li:... value).
        text: Post body.
        author_type: organization or person. Default organization.
        visibility: PUBLIC or CONNECTIONS. Default PUBLIC.
        article_url: Optional link to attach to the post.
        article_title: Optional title for the attached link.
        pipedream_account_id: Which connected LinkedIn account to post from.
    """
    require_editor()
    try:
        body: dict[str, Any] = {
            "author": _author_urn(author_id, author_type),
            "commentary": text,
            "visibility": visibility if visibility in ("PUBLIC", "CONNECTIONS") else "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        if article_url:
            body["content"] = {"article": {"source": article_url, "title": article_title or article_url}}
        return _json(_proxy_request(
            "linkedin_organic", "POST",
            _platform_base("linkedin_organic", "/rest/posts"),
            pipedream_account_id, body=body, headers=_linkedin_headers(),
        ))
    except Exception as exc:
        return _json({"error": str(exc)})


def _pages_base(path: str) -> str:
    return _platform_base("meta_pages", path)


def _granted_scopes(pipedream_account_id: str) -> set[str]:
    """What Meta ACTUALLY granted this connection, not what Pipedream advertises.

    A Pipedream account's `authorized_scopes` is the scope profile the connector
    intends to request; Meta may grant far less. Trusting the advertised list is
    what made a read_only connection look fully capable while every Page call
    failed.
    """
    try:
        payload = _proxy_request("meta_pages", "GET",
                                 _pages_base("/me/permissions"), pipedream_account_id)
        return {
            row.get("permission") for row in (payload or {}).get("data") or []
            if row.get("status") == "granted" and row.get("permission")
        }
    except Exception:
        return set()


def _page_token(page_id: str, pipedream_account_id: str) -> str:
    """The page access token for one Page, read through the proxy.

    Posting to a Page is authorized by a page token, not the user token, and
    the only way to obtain one is /me/accounts. Doing that lookup here keeps
    callers from having to pass a token they cannot easily get hold of.
    """
    payload = _proxy_request("meta_pages", "GET", _pages_base("/me/accounts"),
                             pipedream_account_id, params={"fields": "id,name,access_token"})
    for page in (payload or {}).get("data") or []:
        if str(page.get("id")) == str(page_id):
            token = page.get("access_token")
            if token:
                return token
            break
    seen = [str(p.get("id")) for p in (payload or {}).get("data") or []]
    if not seen:
        # Distinguish "no Pages granted" from "this connection cannot do Pages
        # at all", by asking Meta what it actually granted. Pipedream advertises
        # an `admin` scope profile holding pages_manage_posts, but a connection
        # can still run its `read_only` profile -- observed 2026-08-12, where
        # Meta had granted pages_show_list and nothing else, with no declined
        # entries because the rest was never requested. Reconnecting cannot fix
        # that, so saying "reconnect" would send someone round in circles.
        granted = _granted_scopes(pipedream_account_id)
        if granted and "pages_read_engagement" not in granted:
            raise ValueError(
                "This Pipedream connection was granted only: " + ", ".join(sorted(granted)) +
                ". Page reading and posting need pages_read_engagement and pages_manage_posts, "
                "which it never requested -- reconnecting will not change that, because the "
                "scope profile is set in the Pipedream project, not in the consent screen. "
                "Use the native connection instead: /auth/meta/start?surface=organic, which "
                "asks for the publishing scopes directly."
            )
        # Distinguish "that Page is not in the list" from "the list is empty",
        # because the two have completely different fixes and the generic
        # message sent people looking at Page roles when the real problem was
        # the consent screen.
        raise ValueError(
            f"This Facebook connection exposes no Pages at all, so there is no token for "
            f"{page_id}. Meta grants Page access separately from the scopes: reconnect from "
            f"{connect_hint()} and tick the Pages on Meta's consent screen. Or post through the native "
            "Meta connection instead -- meta_ads_pages gives each Page with its access_token, "
            "and meta_ads_create_page_post publishes with it."
        )
    raise ValueError(
        f"Page {page_id} is not among the Pages this connection can reach ({', '.join(seen)}). "
        "Call meta_pages_list to see them; the account may not have a role on that Page."
    )


@mcp.tool()
def meta_pages_accounts() -> str:
    """List connected Pipedream accounts usable for Facebook/Instagram posting."""
    try:
        accounts = _list_pipedream_accounts("meta_pages")
        return _json({"accounts": accounts, "total": len(accounts)})
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def meta_pages_list(pipedream_account_id: str = "") -> str:
    """List the Facebook Pages this connection administers, with their linked
    Instagram Business accounts.

    Args:
        pipedream_account_id: Which connected Facebook account to read.
    """
    try:
        payload = _proxy_request(
            "meta_pages", "GET", _pages_base("/me/accounts"), pipedream_account_id,
            params={"fields": "id,name,category,instagram_business_account{id,username}"},
        )
        if isinstance(payload, dict) and not (payload.get("data") or []):
            # An empty list here is the single most confusing result this tool
            # can give: the connection reports healthy, the scopes are all
            # present, and Meta returns 200 -- so it reads as "this account
            # administers no Pages", which is usually false. What is actually
            # missing is the Page-selection step in Meta's consent screen,
            # which is separate from granting the scopes.
            payload["why_empty"] = (
                "Meta returned no Pages for this connection. The scopes are granted, but Meta "
                "asks separately which Pages an app may access, and none were selected -- so "
                "pages_show_list is held over an empty set."
            )
            payload["fix"] = (
                f"Reconnect Facebook Pages from {connect_hint()} and, on Meta's consent screen, tick the "
                "Pages to include (the 'Opt in to all' option is safest). Then call "
                "meta_pages_accounts and this tool again."
            )
            payload["alternative"] = (
                "The native Meta connection reaches the same Pages without Pipedream: "
                "meta_ads_pages returns each Page with its access_token, and the meta_ads_page_* "
                "tools (meta_ads_create_page_post, meta_ads_page_feed, and the rest) post and "
                "read with it. Prefer that path while this one is empty."
            )
        return _json(payload)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def meta_pages_create_post(
    page_id: str,
    message: str = "",
    link: str = "",
    image_url: str = "",
    pipedream_account_id: str = "",
) -> str:
    """Publish a post to a Facebook Page through Pipedream managed auth.

    Args:
        page_id: Facebook Page ID (required). See meta_pages_list.
        message: Post text.
        link: Optional URL to share as a link post.
        image_url: Optional public image URL; posts as a photo when given.
        pipedream_account_id: Which connected Facebook account to post from.
    """
    require_editor()
    try:
        if not message and not image_url and not link:
            return _json({"error": "Provide message, link, or image_url."})
        token = _page_token(page_id, pipedream_account_id)
        body: dict[str, Any] = {}
        if message:
            body["message"] = message
        if link:
            body["link"] = link
        path = f"/{page_id}/photos" if image_url else f"/{page_id}/feed"
        if image_url:
            body["url"] = image_url
        # The page token goes in the QUERY STRING, not the body. _proxy_request
        # sends the body as JSON, and Meta's Graph API reads access_token from
        # the query string or a form body -- never from a JSON body. Put it in
        # the body and Meta ignores it, falls back to the user token Pipedream's
        # proxy attaches, and refuses the write with
        #   (#283) Requires pages_read_engagement permission to manage the object
        # even though the connection holds that scope. Posting to a Page needs a
        # PAGE token, and this is the only place Meta will look for it.
        return _json(_proxy_request("meta_pages", "POST", _pages_base(path),
                                    pipedream_account_id,
                                    params={"access_token": token}, body=body))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def meta_pages_create_instagram_post(
    instagram_account_id: str,
    image_url: str,
    caption: str = "",
    page_id: str = "",
    pipedream_account_id: str = "",
) -> str:
    """Publish an image post to an Instagram Business account.

    Instagram publishing is two calls: create a media container, then publish
    it. Both are authorized by the token of the Page the Instagram account is
    linked to, which is why page_id matters here.

    Args:
        instagram_account_id: Instagram Business account ID. See meta_pages_list.
        image_url: Public image URL (JPEG/PNG). Required by Instagram -- it
            fetches the image itself, so the URL must be reachable.
        caption: Caption text, including hashtags.
        page_id: The Facebook Page the Instagram account is linked to. Leave
            blank to use the first Page that has this Instagram account.
        pipedream_account_id: Which connected Facebook account to post from.
    """
    require_editor()
    try:
        if not page_id:
            listing = _proxy_request(
                "meta_pages", "GET", _pages_base("/me/accounts"), pipedream_account_id,
                params={"fields": "id,instagram_business_account{id}"},
            )
            for page in (listing or {}).get("data") or []:
                linked = (page.get("instagram_business_account") or {}).get("id")
                if str(linked) == str(instagram_account_id):
                    page_id = str(page.get("id"))
                    break
            if not page_id:
                return _json({"error": f"No connected Page is linked to Instagram account "
                                       f"{instagram_account_id}. Call meta_pages_list."})
        token = _page_token(page_id, pipedream_account_id)
        # access_token in the query string, not the JSON body -- same reason as
        # meta_pages_create_post above: Meta never reads it from a JSON body, so
        # in the body it is silently ignored and the write is attempted with the
        # user token Pipedream attaches, which Instagram refuses.
        container = _proxy_request(
            "meta_pages", "POST", _pages_base(f"/{instagram_account_id}/media"),
            pipedream_account_id,
            params={"access_token": token},
            body={"image_url": image_url, "caption": caption},
        )
        creation_id = (container or {}).get("id")
        if not creation_id:
            return _json({"error": "Instagram did not return a media container", "response": container})
        published = _proxy_request(
            "meta_pages", "POST", _pages_base(f"/{instagram_account_id}/media_publish"),
            pipedream_account_id,
            params={"access_token": token},
            body={"creation_id": creation_id},
        )
        return _json({"container_id": creation_id, "published": published})
    except Exception as exc:
        return _json({"error": str(exc)})


# TikTok organic goes through our own app, not Pipedream: Pipedream publishes
# no TikTok organic app at all (see pipedream_connect.py), so the proxy these
# tools used to call could never resolve an account. oauth_tiktok.py stores the
# token; tools/tiktok_organic_auth.py hands it over and refreshes it.
@mcp.tool()
def tiktok_organic_accounts() -> str:
    """List the TikTok accounts connected for organic posting."""
    try:
        from auth import current_user_ctx
        from credentials import provider

        user = current_user_ctx.get(None)
        if user is None:
            return _json({"error": "Not authenticated."})
        accounts = [
            {
                "connection_id": str(conn["id"]),
                "label": conn.get("label") or "",
                "open_id": conn.get("email") or "",
                "health": conn.get("health", "connected"),
            }
            for conn in provider().list_connections("tiktok_organic")
        ]
        if not accounts:
            return _json({
                "accounts": [], "total": 0,
                "say_to_user": "No TikTok account is connected yet. Connect one at "
                               + connect_hint() + " (TikTok organic).",
            })
        return _json({"accounts": accounts, "total": len(accounts)})
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_organic_creator_info(connection_id: str = "") -> str:
    """Get the connected TikTok creator's posting limits and privacy options.

    TikTok requires querying this before publishing: it returns the privacy
    levels the account is allowed to use and its remaining daily post quota.

    Args:
        connection_id: Which connected TikTok account. Blank uses the first.
    """
    try:
        from tools.tiktok_organic_auth import tiktok_request

        return _json(tiktok_request(
            "POST", "/post/publish/creator_info/query/", body={}, connection_id=connection_id,
        ))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_organic_publish_video(
    video_url: str,
    caption: str = "",
    privacy_level: str = "SELF_ONLY",
    disable_comment: bool = False,
    disable_duet: bool = False,
    disable_stitch: bool = False,
    connection_id: str = "",
    source: str = "file_upload",
) -> str:
    """Publish a video to the connected TikTok account.

    Defaults to file_upload, which downloads the video here and pushes the
    bytes to TikTok. That is the mode that works out of the box: pull_from_url
    makes TikTok fetch the URL itself, and TikTok only fetches from a domain
    you have verified in the developer portal, so it fails on an unverified
    host with an error that does not mention domains at all.

    Args:
        video_url: Reachable video URL. With file_upload it only has to be
            reachable from this server; with pull_from_url its domain must be
            verified with TikTok.
        caption: Post caption, including any hashtags.
        privacy_level: SELF_ONLY, MUTUAL_FOLLOW_FRIENDS, FOLLOWER_OF_CREATOR,
            or PUBLIC_TO_EVERYONE. Defaults to SELF_ONLY, which is the safe
            choice for an unaudited app -- confirm with creator_info first.
            An unaudited app has its posts forced private whatever is asked for.
        disable_comment: Turn comments off.
        disable_duet: Turn duets off.
        disable_stitch: Turn stitches off.
        connection_id: Which connected TikTok account. Blank uses the first.
        source: file_upload (default, no domain verification needed) or
            pull_from_url (requires a verified domain).
    """
    require_editor()
    try:
        from tools.tiktok_organic_auth import (
            fetch_video_bytes,
            publish_video_by_file,
            tiktok_request,
        )

        post_info = {
            "title": caption,
            "privacy_level": privacy_level,
            "disable_comment": bool(disable_comment),
            "disable_duet": bool(disable_duet),
            "disable_stitch": bool(disable_stitch),
        }

        if str(source or "").strip().lower() != "pull_from_url":
            video_bytes, mime_type = fetch_video_bytes(video_url)
            result = publish_video_by_file(
                video_bytes, post_info, mime_type=mime_type, connection_id=connection_id,
            )
            # This one error is worth translating. TikTok's code names neither
            # the fix nor the working alternative, and it is what an unaudited
            # app hits on every public account -- otherwise the model reports
            # "posting failed" and stops, with a draft upload sitting right there.
            if "unaudited_client_can_only_post" in str(result.get("tiktok_response") or ""):
                result["say_to_user"] = (
                    "TikTok will not let this app post directly to a public account "
                    "until its Content Posting review is approved. Use "
                    "tiktok_organic_upload_draft to put the video in the account's "
                    "TikTok drafts instead, or set the TikTok account to private."
                )
            return _json(result)

        return _json(tiktok_request(
            "POST", "/post/publish/video/init/",
            body={"post_info": post_info,
                  "source_info": {"source": "PULL_FROM_URL", "video_url": video_url}},
            connection_id=connection_id,
        ))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_organic_upload_draft(
    video_url: str,
    connection_id: str = "",
) -> str:
    """Send a video to the TikTok account's drafts for the creator to finish.

    Use this when a direct post is refused. An app that has not passed TikTok's
    Content Posting audit cannot post directly to a public account at all --
    TikTok answers "unaudited_client_can_only_post_to_private_accounts" no
    matter which privacy level is asked for. Drafts work regardless, so this is
    the reliable path until the audit clears.

    The video lands in the creator's TikTok inbox; they open the app, review it
    and publish. Tell the user that is where to find it.

    Args:
        video_url: Reachable video URL. Downloaded here and pushed to TikTok,
            so its domain does not need to be verified.
        connection_id: Which connected TikTok account. Blank uses the first.
    """
    require_editor()
    try:
        from tools.tiktok_organic_auth import fetch_video_bytes, upload_draft_by_file

        video_bytes, mime_type = fetch_video_bytes(video_url)
        result = upload_draft_by_file(video_bytes, mime_type=mime_type, connection_id=connection_id)
        if not result.get("error"):
            result["say_to_user"] = (
                "Uploaded to your TikTok drafts. Open TikTok, go to your profile's "
                "drafts/inbox, and publish it from there."
            )
        return _json(result)
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def tiktok_organic_status(publish_id: str, connection_id: str = "") -> str:
    """Check whether a TikTok publish job finished, is still processing, or failed.

    Named without the "publish" verb on purpose: this only reads job state, and
    the write-tool heuristic classifies on the first word after the prefix.

    Args:
        publish_id: The publish_id returned by tiktok_organic_publish_video.
        connection_id: Which connected TikTok account. Blank uses the first.
    """
    try:
        from tools.tiktok_organic_auth import tiktok_request

        return _json(tiktok_request(
            "POST", "/post/publish/status/fetch/",
            body={"publish_id": publish_id}, connection_id=connection_id,
        ))
    except Exception as exc:
        return _json({"error": str(exc)})
