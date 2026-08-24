"""An in-chat gallery for creatives the assistant just generated.

The signed preview link solved "the customer cannot see it" everywhere. This
solves it *well* on hosts that can render UI: the pictures appear in the
conversation, side by side, at the moment they are made, rather than as a row of
links someone opens one at a time to compare.

Deliberately additive. The link is the floor and works in every client; this is
the ceiling, and where it cannot render, the tool still returns the same URLs in
its text. That order matters -- the upload drop zone taught us that a widget
which silently fails on ChatGPT reads to the model as "uploads are impossible",
and it told two customers exactly that.

The iframe runs under a deny-by-default CSP: without resource_domains its
img-src is 'none' and every picture is blocked with nothing on the page to say
so. The origin comes from the environment because each white-label deploy
answers on its own hostname.
"""
from __future__ import annotations

import os
from typing import Any

from mcp.server.apps import Apps, ResourceCsp

from auth import current_user_ctx

GALLERY_UI_URI = "ui://creative/gallery"


def base_url() -> str:
    return (os.environ.get("APP_URL") or os.environ.get("BASE_URL")
            or os.environ.get("SERVER_BASE_URL") or "").rstrip("/")


def gallery_html() -> str:
    """The whole widget: one self-contained file the host drops into an iframe,
    which is the only shape that survives every client."""
    return """<!doctype html>
<meta charset="utf-8">
<style>
  :root { color-scheme: light dark; }
  body { margin:0; font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px; padding:12px; }
  figure { margin:0; border:1px solid rgba(128,128,128,.3); border-radius:10px; overflow:hidden; }
  img { width:100%; display:block; background:rgba(128,128,128,.12); aspect-ratio:1/1; object-fit:contain; }
  figcaption { padding:8px 10px; font-size:12px; opacity:.75; }
  .row { display:flex; gap:10px; padding:0 10px 10px; }
  a.btn { flex:1; text-align:center; text-decoration:none; padding:7px 10px; border-radius:7px;
          border:1px solid rgba(128,128,128,.4); font-size:12px; color:inherit; }
  .empty { padding:20px; opacity:.7; }
</style>
<div id="root"><p class="empty">Waiting for images...</p></div>
<script>
  function esc(value) {
    return String(value).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  function render(data) {
    var images = (data && data.images) || [];
    var root = document.getElementById("root");
    if (!images.length) {
      root.innerHTML = '<p class="empty">No images to show.</p>';
      return;
    }
    var cards = images.map(function (img, i) {
      // A revised prompt is model output going straight into the page, so it
      // is escaped rather than trusted to be free of markup.
      var caption = esc(img.revised_prompt || img.filename || ("Image " + (i + 1)));
      var url = esc(img.url || "");
      // Eager, not lazy. A chat widget is short and often scrolled past
      // quickly; lazy loading left the cards below the fold blank, which reads
      // as broken rather than as pending. A dozen small images is not a payload
      // worth deferring.
      return '<figure><img src="' + url + '" alt="' + caption + '">' +
             '<figcaption>' + caption + '</figcaption>' +
             '<div class="row">' +
             '<a class="btn" href="' + url + '" target="_blank" rel="noopener">Open</a>' +
             '<a class="btn" href="' + url + '" download>Download</a>' +
             '</div></figure>';
    });
    root.innerHTML = '<div class="grid">' + cards.join("") + '</div>';
  }

  // Hosts deliver the payload two ways depending on version: a global set
  // before load, and a message after it. A widget that only listens for the
  // message renders nothing on the hosts that use the global.
  if (window.openai && window.openai.toolOutput) render(window.openai.toolOutput);
  window.addEventListener("message", function (event) {
    var data = event.data || {};
    if (data.type === "openai:set_globals" && data.globals && data.globals.toolOutput) {
      render(data.globals.toolOutput);
    } else if (data.toolOutput) {
      render(data.toolOutput);
    }
  });
</script>
"""


def gallery_payload(asset_ids: str = "", limit: int = 12) -> dict[str, Any]:
    """Signed URLs for this user's recent generated images, newest first."""
    from credentials import provider
    from tools.generated_image_links import preview_url

    user = current_user_ctx.get(None)
    if user is None:
        return {"images": [], "count": 0,
                "say_to_user_no_widget": "Sign in first, then generate an image."}

    wanted = {a.strip() for a in (asset_ids or "").replace(",", " ").split() if a.strip()}
    rows = provider().recent_generated_image_assets(limit=max(1, min(int(limit or 12), 24)))
    if wanted:
        rows = [r for r in rows if str(r.get("id")) in wanted] or rows

    images = []
    for row in rows:
        url = preview_url(str(row.get("id") or ""), str(user.id))
        if url:
            images.append({
                "url": url,
                "filename": row.get("filename") or "",
                "revised_prompt": row.get("revised_prompt") or "",
                "generated_asset_id": row.get("id"),
            })

    payload: dict[str, Any] = {"images": images, "count": len(images)}
    if not images:
        payload["say_to_user_no_widget"] = (
            "No generated images are still available. They are kept for about two "
            "hours; generate again and I will show them."
        )
    else:
        # What the model reads where no widget renders. The same links, so a
        # host without UI is no worse off than before this existed.
        payload["say_to_user_no_widget"] = (
            "Here are the generated images:\n"
            + "\n".join(f"- {i.get('filename') or 'image'}: {i['url']}" for i in images)
        )
    return payload


def register_creative_gallery(apps: Apps) -> Apps:
    """Add the gallery to an existing Apps extension.

    Takes one rather than making its own: the MCP server accepts a single
    "io.modelcontextprotocol/ui" extension, and registering a second raises
    "Extension is already registered" at startup -- the container then fails to
    boot on PORT=8080 partway through a deploy, with the tools themselves
    perfectly fine.
    """
    origin = base_url()

    apps.add_html_resource(
        GALLERY_UI_URI,
        gallery_html(),
        name="creative_gallery_widget",
        title="Generated Creatives",
        description="The images just generated, shown in the conversation.",
        # resource_domains, not connect_domains: this widget loads pictures
        # rather than calling an API. Without it img-src is 'none' and every
        # image is blocked, with nothing on the page to explain why.
        csp=ResourceCsp(resource_domains=[origin] if origin else []),
        prefers_border=True,
    )

    @apps.tool(
        resource_uri=GALLERY_UI_URI,
        name="creative_show_images",
        title="Show The Generated Images",
    )
    def creative_show_images(asset_ids: str = "", limit: int = 12) -> dict[str, Any]:
        """[RECOMMENDED] Show the user the images that were just generated.

        CALL THIS RIGHT AFTER GENERATING CREATIVES, without being asked. Someone
        asked for a picture; showing it is the point. The generation tools return
        a generated_asset_id, which is for feeding Meta, not for looking at -- on
        its own it leaves the user unable to see what was made.

        In a host that renders inline UI this puts the images in the conversation
        side by side, which is what makes them comparable. Everywhere else the
        same payload carries the links in `say_to_user_no_widget`; pass those on.
        Never tell the user their creatives cannot be shown.

        Args:
            asset_ids: Optional. Specific generated_asset_id values, space or
                comma separated. Leave blank for the most recent images.
            limit: How many to show. Default 12.
        """
        return gallery_payload(asset_ids, limit)

    return apps
