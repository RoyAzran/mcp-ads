"""Competitor research on Google's Ads Transparency Center.

Public ads any verified advertiser is running on Search, YouTube, Display and
Shopping, in 180+ countries, with no API key and no account connection. This is
the family that answers "what is my competitor running" for the markets Meta's
Ad Library will not: outside the EU and UK, Meta returns only political ads,
while Google publishes everyone's.

READ ONLY, AND SOMEBODY ELSE'S ADS. Nothing here touches the user's own Google
Ads account -- that is google_ads_action. No connection, customer_id or token is
involved, which also means "the user has not connected Google Ads" is never a
reason for one of these to fail.

The catch, and the reason this module is shaped the way it is: Google publishes
the archive for people to read in a browser and does not document an API for
it. These are the endpoints the site itself calls. They work, they are public,
and they can change or start refusing without notice -- rate limiting begins
after a few dozen rapid calls. So every tool here returns a degraded envelope
rather than raising, every degraded envelope carries a link a human can open,
and every one of them says in plain words that this is not a problem with the
user's account. A research tool that goes quiet is an inconvenience; one that
tells a customer their Google Ads is broken costs a support call.

Request and response shapes were read off the live service rather than guessed,
and both are held as data (_REQUEST_SHAPES, _FIELD_PATHS) so a reshuffle is a
one-line edit and degrades to a missing field instead of a traceback.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, Iterable

import requests

from mcp_instance import mcp
from tools.geo_targets import (
    COUNTRY_NAMES,
    country_for_geo_target_id,
    country_name,
    geo_target_id,
)
from tools.public_cache import cache_key, cached, peek, stats

_RPC_BASE = "https://adstransparency.google.com/anji/_/rpc"
_SITE = "https://adstransparency.google.com"
_TIMEOUT = float(os.environ.get("ADS_TRANSPARENCY_TIMEOUT", "20") or 20)

# The operator's switch. An undocumented upstream will have a bad day
# eventually; flipping an env var on Cloud Run beats shipping a revert.
# Same shape as WORDPRESS_TOOLS_ENABLED in mcp_server.py.
#
# The default follows the deployment: on where the multi-tenant backend is
# installed (that operator has accepted the risk of an undocumented endpoint),
# off for a self-host install, which should read what this family is before
# opting in with GOOGLE_ADS_TRANSPARENCY_ENABLED=true.
def _default_enabled() -> str:
    from credentials import _default_backend

    return "true" if _default_backend() == "db" else "false"


_ENABLED = (os.environ.get("GOOGLE_ADS_TRANSPARENCY_ENABLED", "").strip() or _default_enabled()).lower() \
    not in {"0", "false", "no", "off"}

# Browser-shaped headers because that is what the endpoint answers. Nothing
# user-specific is ever attached: no cookie, no token, no account id.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Referer": f"{_SITE}/",
    "Origin": _SITE,
    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
    "X-Same-Domain": "1",
}

# Fixed viewer context. Field 7.3 is the *viewer's* locale region and does not
# filter anything -- the filter is 3.8. Pinned to the US so results do not
# depend on which Cloud Run region happens to serve the request.
_VIEWER_CONTEXT = {"1": 1, "2": 0, "3": 2840}

_MAX_PAGE = 40          # what the site itself asks for
_MAX_AGGREGATE = 200    # hard ceiling on paginating tools; see _paginate

# Observed format enum. Inferred from the archive's own rendering -- text ads
# are archived as narrow rendered snapshots, image ads as ad-sized ones -- so
# an unknown value is reported as-is rather than forced into a bucket.
_FORMATS = {1: "TEXT", 2: "IMAGE", 3: "VIDEO"}

_IMG_SRC = re.compile(r'src="([^"]+)"')
_IMG_DIM = re.compile(r'(height|width)="(\d+)"')


class _TransparencyError(RuntimeError):
    """Upstream did not answer usefully. Carries a reason for the envelope."""

    def __init__(self, reason: str, kind: str = "upstream"):
        super().__init__(reason)
        self.reason = reason
        self.kind = kind


# ---------------------------------------------------------------------------
# Request shapes — one place to edit when the upstream moves
# ---------------------------------------------------------------------------

def _suggestions_body(query: str, limit: int) -> dict:
    return {"1": query, "2": int(limit)}


def _creatives_body(advertiser_ids: list[str], region_ids: list[int],
                    page_size: int, cursor: str = "", text: str = "") -> dict:
    body: dict[str, Any] = {
        "2": int(page_size),
        "3": {
            "8": [int(r) for r in region_ids],   # region filter (geo target IDs)
            "12": {"1": text, "2": True},        # free-text within the advertiser
            "13": {"1": list(advertiser_ids)},   # advertiser filter
        },
        "7": dict(_VIEWER_CONTEXT),
    }
    if cursor:
        body["4"] = cursor
    return body


def _creative_body(advertiser_id: str, creative_id: str, region_id: int) -> dict:
    return {"1": advertiser_id, "2": creative_id, "5": {"1": 1, "2": int(region_id)}}


_REQUEST_SHAPES: dict[str, Callable[..., dict]] = {
    "SearchSuggestions": _suggestions_body,
    "SearchCreatives": _creatives_body,
    "GetCreativeById": _creative_body,
}

# Output field -> ordered candidate paths into the response. First non-empty
# wins, so a moved field costs one tuple rather than a rewritten parser.
_FIELD_PATHS: dict[str, list[tuple]] = {
    "advertiser_id": [("1",)],
    "creative_id": [("2",)],
    "advertiser": [("12",)],
    "format": [("4",)],
    "first_seen": [("6", "1")],
    "last_seen": [("7", "1")],
    "image_html": [("3", "3", "2")],
    "preview_url": [("3", "1", "4")],
}


# ---------------------------------------------------------------------------
# Tolerant accessors
# ---------------------------------------------------------------------------

def _dig(node: Any, *path: Any) -> Any:
    """Walk a path through the numerically-keyed response.

    The payload is protobuf rendered as JSON, so the same level can be a dict
    with string keys or a list, and a field that is absent is absent rather than
    null. Every step is allowed to fail into None.
    """
    cur = node
    for step in path:
        if cur is None:
            return None
        try:
            if isinstance(cur, dict):
                cur = cur.get(str(step), cur.get(step))
            elif isinstance(cur, (list, tuple)):
                cur = cur[int(step)]
            else:
                return None
        except (KeyError, IndexError, TypeError, ValueError):
            return None
    return cur


def _first(node: Any, paths: Iterable[tuple], default: Any = None) -> Any:
    for path in paths:
        value = _dig(node, *path)
        if value not in (None, "", [], {}):
            return value
    return default


def _field(node: Any, name: str, default: Any = None) -> Any:
    return _first(node, _FIELD_PATHS.get(name, []), default)


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def _rpc(service: str, method: str, body: dict, refresh: bool = False,
         ttl: int = 3600) -> dict:
    """One RPC call, cached. Raises _TransparencyError; never returns junk."""
    if not _ENABLED:
        raise _TransparencyError(
            "Google Ads Transparency lookups are switched off on this server "
            "(GOOGLE_ADS_TRANSPARENCY_ENABLED=false).", kind="disabled")

    key = cache_key("gadt", service, method, json.dumps(body, sort_keys=True))

    def _load() -> dict:
        url = f"{_RPC_BASE}/{service}/{method}?authuser="
        try:
            resp = requests.post(url, headers=_HEADERS,
                                 data={"f.req": json.dumps(body)}, timeout=_TIMEOUT)
        except requests.Timeout:
            raise _TransparencyError(
                f"Google's Ads Transparency Center did not answer within {_TIMEOUT:.0f}s.",
                kind="timeout")
        except requests.RequestException as exc:
            raise _TransparencyError(
                f"Could not reach Google's Ads Transparency Center: {exc}", kind="network")

        if resp.status_code == 429:
            raise _TransparencyError(
                "Google's Ads Transparency Center is rate limiting this server. It "
                "allows only a few dozen rapid lookups before it starts refusing.",
                kind="rate_limited")
        if resp.status_code >= 400:
            raise _TransparencyError(
                f"Google's Ads Transparency Center returned HTTP {resp.status_code}.",
                kind="http_error")

        # A block or challenge page comes back as 200 with HTML. Treat that as a
        # degraded upstream, not as a parse crash.
        text = (resp.text or "").lstrip()
        if not text.startswith(("{", "[")):
            raise _TransparencyError(
                "Google's Ads Transparency Center returned a web page instead of data, "
                "which usually means it is challenging or blocking automated lookups.",
                kind="blocked")
        try:
            payload = resp.json()
        except ValueError:
            raise _TransparencyError(
                "Google's Ads Transparency Center returned a response that could not be read.",
                kind="unparseable")
        if not isinstance(payload, dict):
            raise _TransparencyError(
                "Google's Ads Transparency Center returned an unexpected response shape.",
                kind="unexpected_shape")
        return payload

    return cached(key, ttl, _load, refresh=refresh)


# ---------------------------------------------------------------------------
# Envelopes
# ---------------------------------------------------------------------------

def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


def advertiser_url(advertiser_id: str, region: str = "US") -> str:
    return f"{_SITE}/advertiser/{advertiser_id}?region={region}"


def creative_url(advertiser_id: str, creative_id: str, region: str = "US") -> str:
    """The page for one specific ad.

    Worth having over the advertiser page: a gallery whose every card links to
    the same place gives the reader no way to go from a thumbnail they noticed
    to the ad itself.
    """
    return f"{_SITE}/advertiser/{advertiser_id}/creative/{creative_id}?region={region}"


def _search_url(query: str, region: str = "US") -> str:
    from urllib.parse import quote
    return f"{_SITE}/?region={region}&query={quote(query or '')}"


def _degraded(exc: _TransparencyError, manual_url: str, **context: Any) -> str:
    """The only way this module reports failure.

    Wording matters more than the mechanism here. An assistant reading a bare
    error about a Google endpoint concludes the user's Google Ads connection is
    broken and sends them to reconnect it, which fixes nothing and costs a
    support call. So the envelope says what is true -- a public data source is
    unavailable -- and hands over a link that still works.
    """
    payload = {
        "success": False,
        "degraded": True,
        "error": exc.reason,
        "reason_kind": exc.kind,
        "note": (
            "This reads Google's public Ads Transparency Center, a source Google "
            "publishes for browsing and does not document as an API. It can refuse or "
            "change without notice. This is NOT a problem with the user's account, "
            "connection, permissions or subscription, and nothing else on this server "
            "is affected -- their own Google Ads tools are unaffected."
        ),
        "next_step": (
            "Tell the user plainly that this one competitor lookup is temporarily "
            "unavailable, and give them the manual_url below so they can check it "
            "themselves. Do NOT suggest reconnecting Google Ads, re-authenticating, "
            "upgrading, or checking their permissions -- none of those are involved. "
            "Do not retry more than once. If they need competitor ads right now, "
            "meta_ad_library_search_ads covers Facebook and Instagram instead."
        ),
        "manual_url": manual_url,
    }
    if exc.kind == "rate_limited":
        payload["next_step"] = (
            "Tell the user this lookup is temporarily rate limited by Google and to try "
            "again in a few minutes, and give them the manual_url. Do NOT retry in a "
            "loop, and do NOT suggest reconnecting anything -- no account is involved."
        )
    payload.update(context)
    return _json(payload)


# ---------------------------------------------------------------------------
# Normalisers -> the shared competitor card shape
# ---------------------------------------------------------------------------

def _thumbnail(row: Any) -> tuple[str, int, int]:
    """Pull the archived creative image out of the snippet of HTML Google returns.

    Text ads are archived as rendered images too, so this is the thumbnail for
    most rows regardless of format.
    """
    html = _field(row, "image_html") or ""
    if not isinstance(html, str):
        return "", 0, 0
    src = _IMG_SRC.search(html)
    dims = {k: int(v) for k, v in _IMG_DIM.findall(html)}
    return (src.group(1) if src else ""), dims.get("width", 0), dims.get("height", 0)


def _epoch_to_date(value: Any) -> str:
    """Seconds -> YYYY-MM-DD. Dates are what a human compares; nanoseconds are not."""
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(int(value), tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def normalize_creative(row: Any, region: str = "US") -> dict:
    """One creative -> the card shape shared with the Meta family."""
    advertiser_id = _field(row, "advertiser_id") or ""
    creative_id = _field(row, "creative_id") or ""
    fmt = _field(row, "format")
    thumb, width, height = _thumbnail(row)
    from tools.competitor_media_links import proxy_url

    return {
        "platform": "google",
        "advertiser": _field(row, "advertiser") or "",
        "advertiser_id": advertiser_id,
        "ad_id": creative_id,
        "format": _FORMATS.get(fmt, f"FORMAT_{fmt}" if fmt is not None else "UNKNOWN"),
        "thumbnail": proxy_url(thumb),
        "thumbnail_width": width,
        "thumbnail_height": height,
        # The rendered creative is served as a script that draws itself, so it is
        # a link to follow rather than something the gallery can embed.
        "preview_url": _field(row, "preview_url") or "",
        "headline": "",
        "body": "",
        "cta": "",
        "destination": "",
        "first_seen": _epoch_to_date(_field(row, "first_seen")),
        "last_seen": _epoch_to_date(_field(row, "last_seen")),
        "is_active": None,
        "surfaces": [],
        "regions": [region],
        "detail_url": creative_url(advertiser_id, creative_id, region),
        "advertiser_url": advertiser_url(advertiser_id, region),
    }


def normalize_advertiser(row: Any) -> dict:
    inner = _dig(row, "1") or {}
    count = _dig(inner, "4", "2", "1")
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 0
    return {
        "advertiser_id": _dig(inner, "2") or "",
        "name": _dig(inner, "1") or "",
        "country": _dig(inner, "3") or "",
        "ads_count": count,
        "verified": bool(_dig(inner, "5")),
    }


# ---------------------------------------------------------------------------
# Shared query helpers
# ---------------------------------------------------------------------------

def _region_id(region: str) -> int:
    return int(geo_target_id(region))


def _suggest(query: str, limit: int = 20, refresh: bool = False) -> list[dict]:
    payload = _rpc("SearchService", "SearchSuggestions",
                   _REQUEST_SHAPES["SearchSuggestions"](query, min(max(limit, 1), 50)),
                   refresh=refresh, ttl=86400)
    rows = payload.get("1") or []
    return [normalize_advertiser(r) for r in rows if _dig(r, "1", "2")]


def _rank_advertisers(candidates: list[dict], query: str) -> list[dict]:
    """Best match first.

    Google returns suggestions in name order, so the literal first row for
    "nike" is a one-ad advertiser in Kenya while Nike itself is further down.
    Rank on what actually identifies a brand: an exact name, then verification,
    then how much they advertise.
    """
    wanted = (query or "").strip().lower()

    def score(item: dict) -> tuple:
        name = (item.get("name") or "").strip().lower()
        ads = int(item.get("ads_count") or 0)
        # Order of magnitude, not the raw count. Ranking on the raw number would
        # let 9,001 ads beat 9,000 on noise; bucketing keeps the comparison to
        # "runs vastly more advertising", which is what identifies the real
        # brand. 0 ads -> 0, 1-9 -> 1, 10-99 -> 2, and so on.
        volume = len(str(ads)) if ads > 0 else 0
        return (
            # Relevance is the gate: an advertiser whose name does not contain
            # the query is never the answer, however much it advertises.
            wanted in name,
            # Then volume, and this is the part that was wrong. Exact equality
            # used to outrank everything, so searching "nike" returned a
            # one-ad namesake instead of "Nike, Inc." and its 9,000 -- and
            # "notion" returned "Notion World sas" over "Notion Labs, Inc".
            # Brands register under a legal entity name, which is precisely
            # never an exact match for the bare brand someone types.
            volume,
            name == wanted,
            name.startswith(wanted),
            bool(item.get("verified")),
            ads,
        )

    return sorted(candidates, key=score, reverse=True)


def _paginate(advertiser_id: str, region: str, limit: int,
              refresh: bool = False, text: str = "") -> tuple[list[Any], int, str, bool]:
    """Fetch up to `limit` creatives, following the cursor.

    Capped at _MAX_AGGREGATE and reported as `truncated` rather than silently
    trimmed: an unbounded loop against an undocumented endpoint is how a shared
    egress IP gets itself blocked for everyone.
    """
    region_id = _region_id(region)
    wanted = min(max(int(limit or 0), 1), _MAX_AGGREGATE)
    rows: list[Any] = []
    cursor = ""
    total = 0
    truncated = False

    while len(rows) < wanted:
        body = _REQUEST_SHAPES["SearchCreatives"](
            [advertiser_id], [region_id], min(_MAX_PAGE, wanted - len(rows)), cursor, text)
        payload = _rpc("SearchService", "SearchCreatives", body, refresh=refresh, ttl=3600)
        page = payload.get("1") or []
        try:
            total = max(total, int(payload.get("4") or 0))
        except (TypeError, ValueError):
            pass
        rows.extend(page)
        cursor = payload.get("2") or ""
        if not page or not cursor:
            break
    if len(rows) > wanted:
        rows = rows[:wanted]
    if total and total > len(rows):
        truncated = True
    return rows, total, cursor, truncated


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def google_ads_transparency_check_access(debug: bool = False) -> str:
    """[RECOMMENDED] Check that Google's Ads Transparency Center is answering.

    Run this first when a transparency lookup came back degraded, or before a
    long research session. It probes all three endpoints with a fixed query and
    reports which respond, so "is it Google or is it us" is answerable in one
    call. Needs no account, token or connection of any kind.

    Args:
        debug: Include the raw first record from each endpoint. Use when the
            shape looks like it has changed upstream.
    """
    report: dict[str, Any] = {"enabled": _ENABLED, "cache": stats(), "checks": {}}
    if not _ENABLED:
        report["note"] = ("Switched off on this server via "
                          "GOOGLE_ADS_TRANSPARENCY_ENABLED. This is an operator setting, "
                          "not a problem with the user's account.")
        return _json(report)

    probe_advertiser = ""
    try:
        found = _suggest("booking.com", 5)
        report["checks"]["SearchSuggestions"] = {"ok": True, "results": len(found)}
        ranked = _rank_advertisers(found, "booking.com")
        probe_advertiser = ranked[0]["advertiser_id"] if ranked else ""
        if debug and found:
            report["checks"]["SearchSuggestions"]["sample"] = found[0]
    except _TransparencyError as exc:
        report["checks"]["SearchSuggestions"] = {"ok": False, "error": exc.reason,
                                                 "kind": exc.kind}

    if probe_advertiser:
        try:
            rows, total, _, _ = _paginate(probe_advertiser, "US", 3)
            report["checks"]["SearchCreatives"] = {"ok": True, "results": len(rows),
                                                   "total_reported": total}
            if debug and rows:
                report["checks"]["SearchCreatives"]["sample"] = rows[0]
            if rows:
                creative_id = _field(rows[0], "creative_id")
                detail = _rpc("LookupService", "GetCreativeById",
                              _REQUEST_SHAPES["GetCreativeById"](
                                  probe_advertiser, creative_id, 2840), ttl=86400)
                report["checks"]["GetCreativeById"] = {"ok": bool(detail.get("1"))}
                if debug:
                    report["checks"]["GetCreativeById"]["sample"] = detail
        except _TransparencyError as exc:
            report["checks"]["SearchCreatives"] = {"ok": False, "error": exc.reason,
                                                   "kind": exc.kind}

    report["success"] = all(c.get("ok") for c in report["checks"].values()) or None
    report["note"] = (
        "This is a public archive Google publishes for browsing, with no API key and no "
        "account behind it. If a check failed, that is Google being unavailable or rate "
        "limiting -- it says nothing about the user's Google Ads account or connection."
    )
    return _json(report)


@mcp.tool()
def google_ads_transparency_find_advertiser(query: str, region: str = "US",
                                            limit: int = 20) -> str:
    """[RECOMMENDED] Find an advertiser's ID by brand or company name.

    Use when you need to be sure which advertiser you are looking at before
    pulling their ads -- several unrelated businesses share most brand names.
    Results are ranked best-match-first rather than in Google's own order, which
    is alphabetical and buries the brand you asked for.

    Results are global; `region` only shapes the links returned.

    Args:
        query: Brand, company or domain, e.g. 'nike', 'booking.com'.
        region: Two-letter country code for the links, e.g. 'US', 'IL', 'GB'.
        limit: How many candidates to return. Default 20.
    """
    try:
        candidates = _rank_advertisers(_suggest(query, limit), query)
    except _TransparencyError as exc:
        return _degraded(exc, _search_url(query, region), query=query)
    except ValueError as exc:
        return _json({"error": str(exc)})

    for item in candidates:
        item["detail_url"] = advertiser_url(item["advertiser_id"], region)

    payload = {
        "success": True,
        "query": query,
        "advertisers": candidates,
        "count": len(candidates),
    }
    if not candidates:
        payload["note"] = (
            f"Google's Ads Transparency Center has no verified advertiser matching "
            f"{query!r}. The lookup succeeded -- this is not an error or a connection "
            f"problem. Only advertisers Google has verified appear here, and they are "
            f"listed under their legal entity name, which is often not the brand name."
        )
        payload["next_step"] = (
            "Try the legal entity name (e.g. 'Nike, Inc.' rather than 'nike'), the "
            "bare domain, or a shorter fragment of the name. If the brand advertises "
            "on Facebook or Instagram instead, use meta_ad_library_find_advertiser."
        )
    return _json(payload)


@mcp.tool()
def google_ads_transparency_advertiser_ads(advertiser_id: str, region: str = "US",
                                           format: str = "", limit: int = 40,
                                           refresh: bool = False) -> str:
    """[RECOMMENDED] List the ads one advertiser is running in a country.

    Takes the advertiser_id from google_ads_transparency_find_advertiser. Covers
    Search, YouTube, Display and Shopping. Needs no account or connection.

    Args:
        advertiser_id: e.g. 'AR02934798844673654785'.
        region: Two-letter country code, e.g. 'US', 'IL', 'GB'. Ads are filtered
            to ones shown in that country.
        format: Optional filter — 'TEXT', 'IMAGE' or 'VIDEO'. Blank for all.
        limit: Max ads to return, up to 200. Default 40.
        refresh: Skip the cache and ask Google again.
    """
    if not advertiser_id:
        return _json({"error": "advertiser_id is required.",
                      "next_step": "Call google_ads_transparency_find_advertiser first."})
    try:
        rows, total, _, truncated = _paginate(advertiser_id, region, limit, refresh)
    except _TransparencyError as exc:
        return _degraded(exc, advertiser_url(advertiser_id, region),
                         advertiser_id=advertiser_id, region=region)
    except ValueError as exc:
        return _json({"error": str(exc)})

    ads = [normalize_creative(r, region.upper()) for r in rows]
    wanted = (format or "").strip().upper()
    if wanted:
        ads = [a for a in ads if a["format"] == wanted]

    payload = {
        "success": True,
        "advertiser_id": advertiser_id,
        "advertiser": ads[0]["advertiser"] if ads else "",
        "region": region.upper(),
        "region_name": country_name(region),
        "ads": ads,
        "count": len(ads),
        "total_available": total,
        "truncated": truncated,
        "detail_url": advertiser_url(advertiser_id, region),
        "cached": peek(cache_key("gadt", "SearchService", "SearchCreatives", "")) or None,
    }
    if truncated:
        payload["note"] = (
            f"Showing {len(ads)} of {total} ads Google reports for this advertiser in "
            f"{country_name(region)}. Raise `limit` (max 200) or open detail_url for the "
            f"full archive. Say that you are showing a sample rather than implying this "
            f"is everything they run."
        )
    if not ads and rows:
        payload["note"] = (
            f"This advertiser has ads in {country_name(region)}, but none of format "
            f"{wanted!r}. Re-run without the format filter to see what they do run."
        )
    elif not ads:
        payload["note"] = (
            f"Google's archive has no ads for this advertiser in {country_name(region)}. "
            f"The lookup succeeded. They may advertise only in other countries."
        )
        payload["next_step"] = ("Try another region, or confirm the advertiser_id with "
                                "google_ads_transparency_find_advertiser.")
    return _json(payload)


@mcp.tool()
def google_ads_transparency_search_ads(query: str, region: str = "US",
                                       format: str = "", limit: int = 40,
                                       refresh: bool = False) -> str:
    """[RECOMMENDED] Look up a brand's Google ads in one call.

    The default way in: resolves the brand name to the best-matching verified
    advertiser, then returns their ads. Use
    google_ads_transparency_find_advertiser instead when several businesses
    share the name and you need to pick deliberately.

    Needs no account, connection or API key, and works in 180+ countries — this
    is the tool to reach for when Meta's Ad Library returns nothing because the
    advertiser is outside the EU/UK.

    Args:
        query: Brand, company or domain, e.g. 'nike', 'booking.com'.
        region: Two-letter country code, e.g. 'US', 'IL', 'GB'.
        format: Optional filter — 'TEXT', 'IMAGE' or 'VIDEO'. Blank for all.
        limit: Max ads to return, up to 200. Default 40.
        refresh: Skip the cache and ask Google again.
    """
    try:
        candidates = _rank_advertisers(_suggest(query, 20, refresh), query)
    except _TransparencyError as exc:
        return _degraded(exc, _search_url(query, region), query=query)
    except ValueError as exc:
        return _json({"error": str(exc)})

    if not candidates:
        return _json({
            "success": True, "query": query, "ads": [], "count": 0,
            "note": (f"No verified Google advertiser matches {query!r}. The lookup "
                     f"succeeded — this is not an error and not a connection problem. "
                     f"Google lists advertisers under their verified legal entity name."),
            "next_step": ("Try the legal entity name or the bare domain. If the brand "
                          "advertises on Meta instead, use meta_ad_library_search_ads."),
            "manual_url": _search_url(query, region),
        })

    best = candidates[0]
    result = json.loads(google_ads_transparency_advertiser_ads(
        best["advertiser_id"], region, format, limit, refresh))
    if isinstance(result, dict) and result.get("success"):
        result["query"] = query
        result["matched_advertiser"] = best
        others = candidates[1:5]
        if others:
            result["other_matches"] = others
            result["note"] = (
                (result.get("note", "") + " ").strip() +
                f" Matched {best['name']!r}"
                f"{' (verified)' if best.get('verified') else ''}"
                f" out of {len(candidates)} advertisers with a similar name. If that is "
                f"the wrong company, pick from other_matches and call "
                f"google_ads_transparency_advertiser_ads with its advertiser_id."
            ).strip()
    return _json(result)


@mcp.tool()
def google_ads_transparency_ad_details(advertiser_id: str, creative_id: str,
                                       region: str = "US") -> str:
    """Get the full record for one ad, including every variant Google archived.

    Args:
        advertiser_id: e.g. 'AR02934798844673654785'.
        creative_id: e.g. 'CR14039481954357739521'.
        region: Two-letter country code, e.g. 'US', 'IL', 'GB'.
    """
    if not advertiser_id or not creative_id:
        return _json({"error": "advertiser_id and creative_id are both required."})
    try:
        payload = _rpc("LookupService", "GetCreativeById",
                       _REQUEST_SHAPES["GetCreativeById"](
                           advertiser_id, creative_id, _region_id(region)), ttl=86400)
    except _TransparencyError as exc:
        return _degraded(exc, advertiser_url(advertiser_id, region),
                         advertiser_id=advertiser_id, creative_id=creative_id)
    except ValueError as exc:
        return _json({"error": str(exc)})

    record = payload.get("1") or {}
    if not record:
        return _json({
            "success": True, "ads": [], "count": 0,
            "note": (f"Google has no archived record for creative {creative_id} in "
                     f"{country_name(region)}. The lookup succeeded; the ad may not have "
                     f"run in that country."),
            "manual_url": advertiser_url(advertiser_id, region),
        })

    from tools.competitor_media_links import proxy_url
    variants = []
    for variant in (_dig(record, "5") or []):
        thumb, width, height = _thumbnail(variant)
        variants.append({
            "thumbnail": proxy_url(thumb),
            "thumbnail_width": width,
            "thumbnail_height": height,
            "preview_url": _dig(variant, "1", "4") or "",
        })

    # Google reports where the ad ran as numeric geo IDs and a YYYYMMDD stamp;
    # translate both so the answer reads as countries and dates.
    shown_in = []
    for entry in (_dig(record, "17") or []):
        geo_id = _dig(entry, "1")
        code = country_for_geo_target_id(geo_id)
        stamp = str(_dig(entry, "5") or "")
        shown_in.append({
            "region": code,
            "region_name": country_name(code) if code else "",
            "geo_target_id": geo_id,
            "last_shown": f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}" if len(stamp) == 8 else stamp,
        })

    fmt = _dig(record, "8")
    return _json({
        "success": True,
        "advertiser_id": advertiser_id,
        "creative_id": creative_id,
        "format": _FORMATS.get(fmt, f"FORMAT_{fmt}" if fmt is not None else "UNKNOWN"),
        "last_seen": _epoch_to_date(_dig(record, "4", "1")),
        "variants": variants,
        "variant_count": len(variants),
        "shown_in": shown_in,
        "detail_url": advertiser_url(advertiser_id, region),
    })


@mcp.tool()
def google_ads_transparency_creative_mix(advertiser_id: str, region: str = "US",
                                         limit: int = 100, refresh: bool = False) -> str:
    """Summarise what an advertiser's Google ads look like as a whole.

    The analysis counterpart to listing ads: format split, how long creatives
    stay live, which are longest-running, and when they last refreshed. Use it
    to answer "how heavily are they investing and how often do they change
    angle" rather than showing 100 thumbnails.

    Args:
        advertiser_id: e.g. 'AR02934798844673654785'.
        region: Two-letter country code, e.g. 'US', 'IL', 'GB'.
        limit: How many ads to analyse, up to 200. Default 100.
        refresh: Skip the cache and ask Google again.
    """
    if not advertiser_id:
        return _json({"error": "advertiser_id is required.",
                      "next_step": "Call google_ads_transparency_find_advertiser first."})
    try:
        rows, total, _, truncated = _paginate(advertiser_id, region, limit, refresh)
    except _TransparencyError as exc:
        return _degraded(exc, advertiser_url(advertiser_id, region),
                         advertiser_id=advertiser_id, region=region)
    except ValueError as exc:
        return _json({"error": str(exc)})

    ads = [normalize_creative(r, region.upper()) for r in rows]
    if not ads:
        return _json({
            "success": True, "advertiser_id": advertiser_id, "count": 0,
            "note": (f"No ads archived for this advertiser in {country_name(region)}. "
                     f"The lookup succeeded."),
            "detail_url": advertiser_url(advertiser_id, region),
        })

    by_format: dict[str, int] = {}
    for ad in ads:
        by_format[ad["format"]] = by_format.get(ad["format"], 0) + 1

    def run_days(ad: dict) -> int:
        try:
            from datetime import date
            start = date.fromisoformat(ad["first_seen"])
            end = date.fromisoformat(ad["last_seen"])
            return (end - start).days
        except ValueError:
            return 0

    ranked = sorted(ads, key=run_days, reverse=True)
    first_seen = sorted(a["first_seen"] for a in ads if a["first_seen"])
    last_seen = sorted(a["last_seen"] for a in ads if a["last_seen"])

    payload = {
        "success": True,
        "advertiser_id": advertiser_id,
        "advertiser": ads[0]["advertiser"],
        "region": region.upper(),
        "region_name": country_name(region),
        "analysed": len(ads),
        "total_available": total,
        "truncated": truncated,
        "by_format": by_format,
        "oldest_ad_first_seen": first_seen[0] if first_seen else "",
        "newest_ad_first_seen": first_seen[-1] if first_seen else "",
        "most_recent_activity": last_seen[-1] if last_seen else "",
        "longest_running": [
            {"ad_id": a["ad_id"], "format": a["format"], "first_seen": a["first_seen"],
             "last_seen": a["last_seen"], "days_running": run_days(a),
             "thumbnail": a["thumbnail"], "detail_url": a["detail_url"]}
            for a in ranked[:10]
        ],
        "detail_url": advertiser_url(advertiser_id, region),
    }
    if truncated:
        payload["note"] = (
            f"Based on the {len(ads)} most recent of {total} ads Google reports. Treat "
            f"the split as a sample, not a census, and say so."
        )
    return _json(payload)


@mcp.tool()
def google_ads_transparency_regions(search: str = "") -> str:
    """List the countries Google's Ads Transparency Center can be filtered to.

    No network call. Use it instead of guessing a country code.

    Args:
        search: Optional filter on country name or code, e.g. 'isr', 'united'.
    """
    term = (search or "").strip().lower()
    regions = [
        {"code": code, "name": name, "geo_target_id": geo_target_id(code)}
        for code, name in sorted(COUNTRY_NAMES.items(), key=lambda kv: kv[1])
        if not term or term in name.lower() or term == code.lower()
    ]
    return _json({"success": True, "regions": regions, "count": len(regions),
                  "note": "Pass `code` as the `region` argument on any transparency tool."})
