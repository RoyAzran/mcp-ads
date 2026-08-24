"""A competitor's ads, side by side, in the conversation.

Competitor research answers with creatives, and a list of forty archive links is
not an answer -- nobody opens forty tabs to notice that every one of them leads
on the same discount. Put them in a grid and the pattern is the finding.

Two sources, one grid. Google's archive gives a rendered picture of each ad;
Meta's API gives the copy and a link to the archived ad but no embeddable
image, because ad_snapshot_url is a token-bearing HTML page rather than a
picture. So a card renders as an image when there is one and as text when there
is not, and both carry a platform badge -- an unlabelled mix of the two would be
worse than either alone.

Deliberately additive, like the creative gallery it is modelled on. The payload
always carries say_to_user_no_widget with the same information in plain text,
because a widget that silently fails on a host that cannot render it reads to
the model as "this is impossible" -- which is exactly what the upload drop zone
once told two customers.

The iframe runs under a deny-by-default CSP, and the only origin it is given is
this server's own. That is why competitor images are proxied through
/competitor-media rather than loaded from fbcdn and googleusercontent directly:
see tools/competitor_media_links for why that is the right way round.
"""
from __future__ import annotations

import json
import os
from typing import Any

from mcp.server.apps import Apps, ResourceCsp

COMPETITOR_UI_URI = "ui://competitor/ads"


def base_url() -> str:
    return (os.environ.get("APP_URL") or os.environ.get("BASE_URL")
            or os.environ.get("SERVER_BASE_URL") or "").rstrip("/")


def competitor_gallery_html() -> str:
    """The whole widget: one self-contained file the host drops into an iframe."""
    return """<!doctype html>
<meta charset="utf-8">
<style>
  :root { color-scheme: light dark; }
  body { margin:0; font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  .head { padding:12px 12px 0; font-size:13px; opacity:.8; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:12px; padding:12px; }
  figure { margin:0; border:1px solid rgba(128,128,128,.3); border-radius:10px; overflow:hidden;
           display:flex; flex-direction:column; }
  img { width:100%; display:block; background:rgba(128,128,128,.12); object-fit:contain; }
  .copy { padding:10px; font-size:13px; flex:1; }
  .hl { font-weight:600; margin:0 0 4px; }
  .bd { margin:0; opacity:.8; max-height:6.5em; overflow:hidden; }
  .cta { margin:6px 0 0; font-size:12px; opacity:.7; }
  .meta { padding:0 10px 8px; font-size:11px; opacity:.6; display:flex; gap:6px; flex-wrap:wrap; }
  .badge { border:1px solid rgba(128,128,128,.4); border-radius:20px; padding:1px 7px; font-size:11px; }
  .row { display:flex; gap:10px; padding:0 10px 10px; }
  a.btn { flex:1; text-align:center; text-decoration:none; padding:7px 10px; border-radius:7px;
          border:1px solid rgba(128,128,128,.4); font-size:12px; color:inherit; }
  .empty { padding:20px; opacity:.7; }
  .notes { padding:0 12px 12px; font-size:12px; opacity:.7; }
  .notes p { margin:4px 0; }
</style>
<div id="root"><p class="empty">Waiting for competitor ads...</p></div>
<script>
  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  function dates(ad) {
    var first = ad.first_seen ? esc(ad.first_seen) : "";
    var last = ad.last_seen ? esc(ad.last_seen) : "";
    if (first && last) { return first + " to " + last; }
    if (first) { return "since " + first; }
    return last ? "last seen " + last : "";
  }

  function card(ad) {
    var out = ['<figure>'];
    if (ad.thumbnail) {
      out.push('<img src="' + esc(ad.thumbnail) + '" alt="' + esc(ad.advertiser) + ' ad">');
    }
    var hasCopy = ad.headline || ad.body || ad.cta;
    if (hasCopy) {
      out.push('<div class="copy">');
      if (ad.headline) { out.push('<p class="hl">' + esc(ad.headline) + '</p>'); }
      if (ad.body) { out.push('<p class="bd">' + esc(ad.body) + '</p>'); }
      if (ad.cta) { out.push('<p class="cta">' + esc(ad.cta) + '</p>'); }
      out.push('</div>');
    } else if (!ad.thumbnail) {
      out.push('<div class="copy"><p class="bd">No creative preview available for this ad.</p></div>');
    }
    out.push('<div class="meta">');
    out.push('<span class="badge">' + esc(ad.platform === "meta" ? "Meta" : "Google") + '</span>');
    if (ad.format) { out.push('<span class="badge">' + esc(ad.format) + '</span>'); }
    if (ad.advertiser) { out.push('<span>' + esc(ad.advertiser) + '</span>'); }
    var when = dates(ad);
    if (when) { out.push('<span>' + when + '</span>'); }
    out.push('</div>');
    var link = ad.detail_url || ad.preview_url;
    if (link) {
      out.push('<div class="row"><a class="btn" href="' + esc(link) +
               '" target="_blank" rel="noopener noreferrer">Open the original</a></div>');
    }
    out.push('</figure>');
    return out.join("");
  }

  function render(data) {
    var root = document.getElementById("root");
    if (!data) { return; }
    var ads = data.ads || [];
    var html = "";
    if (data.headline) { html += '<p class="head">' + esc(data.headline) + '</p>'; }
    if (ads.length) {
      html += '<div class="grid">' + ads.map(card).join("") + '</div>';
    } else {
      html += '<p class="empty">No competitor ads came back for this search.</p>';
    }
    var notes = data.notes || [];
    if (notes.length) {
      html += '<div class="notes">' + notes.map(function (n) {
        return '<p>' + esc(n) + '</p>';
      }).join("") + '</div>';
    }
    root.innerHTML = html;
  }

  if (window.openai && window.openai.toolOutput) { render(window.openai.toolOutput); }
  window.addEventListener("message", function (event) {
    var data = event && event.data;
    if (!data) { return; }
    if (data.type === "openai:set_globals" && data.globals && data.globals.toolOutput) {
      render(data.globals.toolOutput);
    } else if (data.toolOutput) {
      render(data.toolOutput);
    }
  });
</script>
"""


def _collect(platform: str, brand: str, countries: str, limit: int
             ) -> tuple[list[dict], list[str]]:
    """Ads and notes from one source. Never raises -- a dead source is a note."""
    ads: list[dict] = []
    notes: list[str] = []
    try:
        if platform == "google":
            from tools.google_ads_transparency import google_ads_transparency_search_ads
            region = (countries.split(",")[0] or "US").strip() or "US"
            result = json.loads(google_ads_transparency_search_ads(brand, region, "", limit))
            if result.get("degraded"):
                notes.append(f"Google Ads Transparency is unavailable right now: "
                             f"{result.get('error', 'no reason given')}")
            elif result.get("error"):
                notes.append(f"Google Ads Transparency: {result['error']}")
            else:
                ads = result.get("ads") or []
                if not ads and result.get("note"):
                    notes.append(result["note"])
        else:
            from tools.meta_ad_library import meta_ad_library_search_ads
            result = json.loads(meta_ad_library_search_ads(
                brand, countries, "ALL", "ACTIVE", "", "", "", "", limit))
            if result.get("error"):
                notes.append(f"Meta Ad Library: {result['error']}")
            else:
                ads = result.get("ads") or []
                if result.get("note"):
                    notes.append(result["note"])
    except Exception as exc:  # noqa: BLE001 - one dead source must not empty the grid
        notes.append(f"{platform.title()} lookup failed: {exc}")
    return ads, notes


def competitor_gallery_payload(brand: str = "", platform: str = "both",
                               countries: str = "US", limit: int = 12) -> dict[str, Any]:
    """Ads from both archives, merged into one list of cards."""
    if not brand:
        return {
            "ads": [], "count": 0, "notes": ["No brand was given to look up."],
            "say_to_user_no_widget": "Tell me which competitor to look up.",
        }

    wanted = (platform or "both").strip().lower()
    sources = ["google", "meta"] if wanted not in ("google", "meta") else [wanted]
    per_source = max(int(limit or 12), 1)

    ads: list[dict] = []
    notes: list[str] = []
    for source in sources:
        found, source_notes = _collect(source, brand, countries, per_source)
        ads.extend(found)
        notes.extend(source_notes)

    # Most recently active first: what they are running now is the point.
    ads.sort(key=lambda a: (a.get("last_seen") or "", a.get("first_seen") or ""),
             reverse=True)
    ads = ads[:per_source]

    payload: dict[str, Any] = {
        "brand": brand,
        "headline": f"Ads {brand} is running" + (
            f" in {countries.upper()}" if countries else ""),
        "ads": ads,
        "count": len(ads),
        "platforms": sources,
        "notes": notes,
    }

    # The floor. Every host gets the same facts even with no widget at all.
    if ads:
        lines = [f"{brand} — {len(ads)} ad(s) found:"]
        for ad in ads:
            # Google text ads carry no copy in the archive, only a rendered
            # picture, so fall back to something that still tells one line from
            # the next -- otherwise this reads as the same ad repeated.
            label = (ad.get("headline") or ad.get("body")
                     or " ".join(x for x in (ad.get("format"), ad.get("last_seen")) if x)
                     or "ad")
            where = "Meta" if ad.get("platform") == "meta" else "Google"
            link = ad.get("detail_url") or ad.get("preview_url") or ""
            lines.append(f"- [{where}] {str(label)[:80]}"
                         + (f" — {link}" if link else ""))
        payload["say_to_user_no_widget"] = "\n".join(lines)
    else:
        payload["say_to_user_no_widget"] = "\n".join(
            [f"No ads found for {brand}."] + notes) if notes else f"No ads found for {brand}."
    return payload


def register_competitor_gallery(apps: Apps) -> Apps:
    """Add the competitor gallery to an existing Apps extension.

    Takes one rather than making its own, for the same reason the creative
    gallery does: the MCP server accepts a single "io.modelcontextprotocol/ui"
    extension, and a second raises "Extension is already registered" at startup,
    which fails the container mid-deploy with the tools themselves perfectly fine.
    """
    origin = base_url()

    apps.add_html_resource(
        COMPETITOR_UI_URI,
        competitor_gallery_html(),
        name="competitor_ads_widget",
        title="Competitor Ads",
        description="Ads a competitor is running, shown in the conversation.",
        # Our own origin only. Competitor images come through /competitor-media
        # rather than from fbcdn/googleusercontent, so no third-party host needs
        # to be allowed here -- and Meta's snapshot token never reaches the page.
        csp=ResourceCsp(resource_domains=[origin] if origin else []),
        prefers_border=True,
    )

    @apps.tool(
        resource_uri=COMPETITOR_UI_URI,
        name="competitor_ads_gallery",
        title="Show Competitor Ads",
    )
    def competitor_ads_gallery(brand: str = "", platform: str = "both",
                               countries: str = "US", limit: int = 12) -> dict[str, Any]:
        """[RECOMMENDED] Show the user the ads a competitor is running.

        CALL THIS WHEN SOMEONE ASKS WHAT A COMPETITOR IS ADVERTISING. Seeing the
        creatives side by side is the answer; a list of archive links is not.
        Looks up public ad archives only -- no account, connection or token of
        the user's is involved, so this works even when they have connected
        nothing at all.

        In a host that renders inline UI the ads appear in the conversation as a
        grid. Everywhere else the same payload carries the same ads and links in
        `say_to_user_no_widget`; pass those on. Never tell the user their
        competitor's ads cannot be shown.

        Note on coverage: Google covers 180+ countries. Meta's API only returns
        ordinary commercial ads for the EU and UK, so for a US or Israeli brand
        the Meta half will be empty and the `notes` will say why -- pass that on
        rather than implying the brand does not advertise on Facebook.

        Args:
            brand: The competitor to look up, e.g. 'wix.com' or 'Booking.com'.
            platform: 'both' (default), 'google', or 'meta'.
            countries: Two-letter country codes, comma separated. Default 'US'.
                Use an EU country or 'GB' to get commercial ads from Meta.
            limit: How many ads to show. Default 12.
        """
        return competitor_gallery_payload(brand, platform, countries, limit)

    return apps
