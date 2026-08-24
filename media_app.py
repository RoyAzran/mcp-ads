"""The media-upload MCP App (SEP-1865), as an SDK extension.

WHY THIS FILE EXISTS SEPARATELY
-------------------------------
`MCPServer` applies extensions at construction and then freezes them, so every
binding an extension contributes has to be registered *before* the server object
is built. Our tools normally register the other way round -- `@mcp.tool()` at
import time, long after `mcp_instance.mcp` exists -- so the App cannot be
declared from `tools/media.py` without a circular import back into the instance
it is trying to attach to.

Building the extension here, from primitives that depend on nothing but
`media_upload` and `auth`, keeps the dependency arrow pointing one way:
media_upload -> media_app -> mcp_instance -> everything else.

WHY 2.0 OF THE SDK
------------------
mcp 1.27 could not do this at all. Its ServerCapabilities had no `extensions`
field and the string "io.modelcontextprotocol/ui" appeared nowhere in the
package, so a server could declare `_meta.ui.resourceUri` correctly and the host
would still never fetch or mount the resource -- there was no handshake to tell
it the server spoke Apps. That is why the first attempt rendered nothing despite
every declaration being right.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from mcp.server.apps import Apps, ResourceCsp

from auth import current_user_ctx
from media_upload import find_uploads, new_upload, widget_html

UPLOAD_UI_URI = "ui://mcp-ads/media-upload"


def base_url() -> str:
    for key in ("SERVER_BASE_URL", "BASE_URL", "APP_URL"):
        value = (os.environ.get(key) or "").strip().rstrip("/")
        if value:
            return value
    return ""


def _user_id() -> str:
    user = current_user_ctx.get(None)
    return str(getattr(user, "id", "") or "").strip()


def _resolve_destination(ad: str) -> dict:
    """Turn a loose ad reference into a signed destination, or explain why not.

    Resolution happens here, at tool-call time, rather than when the file lands:
    an ambiguous name has to become a question to the user *before* they pick a
    file, and a drop zone that only reveals "which of these 4 ads did you mean?"
    after the upload would be a poor trade for one saved round trip.
    """
    # Imported here, not at module scope: tools.meta_ads imports mcp_instance,
    # which imports this module, so a top-level import would close the circle.
    from tools import meta_ads

    found = meta_ads.find_ad(ad)
    if not found.get("ok"):
        return {"error": found.get("error", "Could not identify that ad."),
                "candidates": found.get("candidates", []),
                "next_step": found.get("next_step",
                                       "Ask the user which ad they mean, then call this again.")}
    target = found["ad"]
    status = str(target.get("effective_status") or target.get("status") or "").upper()
    return {
        "destination": {
            "p": "meta_ad",
            "ad": target.get("id"),
            "name": target.get("name"),
            "status": status,
        },
        "label": target.get("name") or target.get("id"),
        "live": status == "ACTIVE",
    }


def upload_start_payload(purpose: str = "", ad: str = "") -> dict:
    user_id = _user_id()
    if not user_id:
        return {"error": "Not authenticated."}
    if not os.environ.get("GCS_MEDIA_BUCKET"):
        return {
            "error": "This deploy has no GCS_MEDIA_BUCKET configured, so uploads cannot be stored.",
            "next_step": "Ask the user for a public HTTPS URL of the image instead.",
        }

    destination = None
    resolved: dict = {}
    if (ad or "").strip():
        resolved = _resolve_destination(ad.strip())
        if resolved.get("error"):
            return resolved
        destination = resolved["destination"]

    upload_id, token = new_upload(user_id, destination)
    ttl = int(os.environ.get("MEDIA_UPLOAD_TTL_SECONDS", "1800") or "1800")
    payload = {
        "upload_id": upload_id,
        "upload_url": f"{base_url()}/media/u/{token}",
        "purpose": purpose,
        "expires_in_seconds": ttl,
        # Lets a replayed widget (an old conversation reopened) say "expired"
        # rather than failing at the moment of upload.
        "expires_at": int(time.time()) + ttl,
    }

    if destination:
        # The widget reads these two to name the ad on the drop zone and to warn
        # before touching a live one.
        payload["destination_label"] = resolved["label"]
        payload["destination_live"] = resolved["live"]
        payload["next_step"] = (
            f"A drop zone is now visible, addressed to the ad {resolved['label']!r}. When the user "
            "drops a picture it goes straight onto that ad, keeping its existing copy, link and CTA. "
            + ("That ad is live, so they will be asked to confirm first. " if resolved["live"] else "")
            + "Tell them the line in say_to_user (in their language) so the box does not appear "
            "unexplained. "
            + f"Call media_upload_result with upload_id={upload_id} afterwards to see what happened; "
            "there is nothing else for you to do."
        )
        payload["say_to_user"] = (
            f"Drop the new picture in the box above -- or paste it with Ctrl+V -- and I will put it "
            f"straight on {resolved['label']}, keeping its existing text, link and button."
            + (" It is running right now, so I will ask you to confirm first." if resolved["live"] else "")
        )
    else:
        payload["say_to_user"] = (
            "Drop your images or videos in the box above -- or paste them with Ctrl+V -- "
            "and I will upload them to your ad. You can add several at once."
        )
        payload["say_to_user_no_widget"] = (
            f"Open this link and drop your images or videos in: {payload['upload_url']} "
            f"(valid {ttl // 60} minutes). Tell me when you have, and I will put them on your ad. "
            "You can upload several at once."
        )
        # The drop zone is an MCP Apps binding, so it only appears in hosts that
        # render one. Through the ChatGPT Actions surface -- or any plain
        # dispatcher call -- there is no box, and a payload that only talked
        # about "the box above" led one customer's assistant to conclude the
        # upload could not be done at all and to ask the user for a public URL
        # they had no way to produce. The link works everywhere, so it is
        # spelled out rather than kept as a footnote.
        payload["next_step"] = (
            "IF YOU CANNOT RENDER THE DROP ZONE (you are ChatGPT, an API client, or anything "
            "other than an MCP host that shows inline UI), do not tell the user you are unable "
            "to upload and do not ask them for a public URL: give them the line in "
            "say_to_user_no_widget, which is just a link they open in a browser. If a drop zone "
            "did appear above, use say_to_user instead so the box does not appear unexplained. "
            "Either way it takes several files at once, and video as well as images. "
            f"Once they have uploaded, call media_upload_result with upload_id={upload_id} and "
            "pass the returned url(s) to the platform media tool."
        )
    return payload


def upload_result_payload(upload_id: str) -> dict:
    if not _user_id():
        return {"error": "Not authenticated."}
    upload_id = (upload_id or "").strip()
    if not upload_id:
        return {"error": "upload_id is required."}

    files = find_uploads(upload_id)
    if not files:
        return {
            "status": "waiting",
            "upload_id": upload_id,
            "next_step": "The user has not uploaded a file yet. Ask them to drop it in, then check again.",
        }

    if len(files) > 1:
        # One slot takes several files -- a carousel, variants to choose from,
        # or an image and a video of the same creative. Report them all: picking
        # the first silently would drop the rest with no sign anything was lost.
        videos = [f for f in files if f.get("is_video")]
        images = [f for f in files if not f.get("is_video")]
        return {
            "status": "ready",
            "upload_id": upload_id,
            "file_count": len(files),
            "files": files,
            "image_urls": [f["url"] for f in images],
            "video_urls": [f["url"] for f in videos],
            "next_step": (
                f"{len(files)} files were uploaded ({len(images)} image(s), {len(videos)} video(s)). "
                "Pass each `url` to the platform media tool -- meta_ads_upload_image_to_meta for "
                "images, meta_ads_upload_video_to_meta for videos, or the carousel creative tool "
                "when they belong to one ad. Ask the user which they meant if it is ambiguous, "
                "rather than assuming the first."
            ),
        }

    found = files[0]
    delivery = found.get("delivery")
    if delivery:
        # The file was addressed to an ad, so the work is already done and the
        # URL is incidental. Saying "pass this to the upload tool" here would
        # invite a second, duplicate creative.
        if delivery.get("ok"):
            next_step = (
                f"Already done -- the image is on {delivery.get('ad_name')!r} "
                f"(creative {delivery.get('creative_id')}). Tell the user, and mention it is back "
                "in review with Meta if the ad is ACTIVE. Do not upload it again."
            )
        elif delivery.get("needs_confirmation"):
            next_step = (
                f"The user was asked to confirm replacing the image on {delivery.get('ad_name')!r} "
                "because it is live, and has not answered yet. Wait, or ask them."
            )
        else:
            next_step = (
                f"The file uploaded but did not reach the ad: {delivery.get('error')}. "
                "Report this rather than retrying blindly."
            )
        return {"status": "ready", "upload_id": upload_id, **found, "next_step": next_step}

    return {
        "status": "ready",
        "upload_id": upload_id,
        **found,
        "next_step": (
            "Pass `url` to the platform media tool -- e.g. meta_ads_upload_image_to_meta(image_url=url). "
            "Do not download and re-send the bytes."
        ),
    }


def build_media_apps() -> Apps:
    """A configured Apps extension: the drop-zone resource plus its two tools."""
    apps = Apps()

    origin = base_url()
    apps.add_html_resource(
        UPLOAD_UI_URI,
        widget_html(),
        name="media_upload_widget",
        title="Media Upload",
        description="Drop zone for handing a picture or video to the server.",
        # The app iframe runs under a deny-by-default CSP: without connect_domains
        # its connect-src is 'none' and the upload XHR never leaves the page. Read
        # from the environment rather than hard-coded, because each white-label
        # deploy answers on its own hostname and a literal mcp-ads.com would
        # silently break the widget for every client.
        csp=ResourceCsp(connect_domains=[origin] if origin else []),
        prefers_border=True,
    )

    @apps.tool(
        resource_uri=UPLOAD_UI_URI,
        name="media_upload_start",
        title="Upload A Picture Or Video",
    )
    def media_upload_start(purpose: str = "", ad: str = "") -> dict[str, Any]:
        """[RECOMMENDED] Show the user a drop zone to hand over pictures or videos.

        CALL THIS WITHOUT BEING ASKED whenever media enters the conversation:

          * the user attaches or pastes a picture or video
          * they say they want to upload, send or add one
          * they ask for anything that needs a creative -- a new image ad,
            changing an ad's picture, refreshing a tired creative, a carousel,
            a Page or Instagram post

        Say what it is for in the same turn, e.g. "drop your images or videos in
        the box above and I will publish them to your ad", so the box does not
        appear unexplained. Do not ask them to find a public URL first, and do
        not wait for a second request.

        You cannot read an attachment -- you are shown the picture, never handed
        the bytes -- so this is the only way media reaches an ad. Never base64 a
        file into a tool parameter: large parameters are truncated in transit and
        the file arrives corrupt (loudly for PNG, silently for JPEG -- Meta
        returns a valid-looking image_hash for a damaged creative).

        In a host that renders inline UI this puts a drop zone in the
        conversation. Everywhere else -- ChatGPT, API clients -- there is no box
        and the same payload carries a plain upload link in
        `say_to_user_no_widget`. Hand that over rather than telling the user you
        cannot accept files; asking them for a public URL instead is a dead end,
        because this tool is what produces one.

        SEVERAL FILES AT ONCE ARE FINE, and so is video (MP4/MOV alongside
        JPG/PNG/GIF/WebP). One drop zone takes a whole carousel, a set of
        variants to choose between, or an image and a video of the same
        creative. media_upload_result then returns `files`, `image_urls` and
        `video_urls` rather than a single url -- do not assume the first one is
        the one they meant; ask if it is ambiguous.

        PASS `ad` WHENEVER THE USER MEANS A PARTICULAR AD, e.g. "put this picture
        on the retargeting ad". The image then goes straight onto that ad the
        moment they drop it, keeping its existing copy, link, page and CTA, and
        you need no follow-up call at all. Use whatever the conversation has been
        calling the ad -- its name, part of its name, or its ID. If several ads
        match, this returns them so you can ask which one; do not guess, since the
        wrong choice edits the wrong live ad.

        Without `ad`, the file is just stored: call media_upload_result with the
        returned upload_id to get its public URL.

        Args:
            purpose: Optional note shown to the user, e.g. "creative for the
                retargeting ad". Helps them pick the right file.
            ad: Optional. The ad this picture should land on -- name, partial
                name, or ID.
        """
        # A plain dict, not content blocks: the SDK derives both the TextContent
        # the model reads and the structuredContent the widget reads from this
        # one object, and a tool returning content blocks gets
        # structuredContent: None -- which would leave the drop zone waiting
        # forever for an upload slot it never receives.
        return upload_start_payload(purpose, ad)

    return apps
