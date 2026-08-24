"""Competitor research on Meta's public Ad Library.

Ads that OTHER advertisers are running on Facebook and Instagram, read through
Meta's official ads_archive API. Read-only and public: nothing here touches the
user's own ad account, campaigns or spend -- that is meta_ads_action. No ad
account id is involved, and "the user has not connected Meta Ads" is not by
itself a reason for any of this to fail.

THE COVERAGE LIMIT, WHICH IS THE WHOLE SHAPE OF THIS MODULE. Meta's API is not
the Ad Library website. The website shows every active ad in most countries; the
API returns ordinary commercial ads only where they were delivered to the EU or
the UK. Everywhere else -- the US, Israel, Canada, Australia -- it returns only
ads about politics and social issues, which for competitor research means it
returns nothing at all.

That is Meta's rule and no amount of code changes it, so what this module owes
its caller is an honest empty result. A bare {"data": []} is indistinguishable
from "this brand runs no ads" and from "your connection is broken", and an
assistant handed one will pick an explanation and state it confidently. Every
empty result here therefore says the call succeeded, names the restriction, and
points at either an EU country or google_ads_transparency_action, which has no
such limit and covers 180+ countries.

Access is gated on the *token holder* having completed Meta's identity
verification, not on the advertiser being searched. Practically no customer has
done that, so the token ladder in _token() prefers a single operator-owned
system token that serves everybody.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

import requests

from auth import current_connection_token_ctx, current_user_ctx
from mcp_instance import mcp
from credentials import connect_hint
from tools.public_cache import cache_key, cached, stats

GRAPH_VERSION = os.environ.get("META_GRAPH_VERSION", "v22.0")
ARCHIVE_URL = f"https://graph.facebook.com/{GRAPH_VERSION}/ads_archive"
LIBRARY_SITE = "https://www.facebook.com/ads/library/"
_TIMEOUT = 30

# Where ordinary commercial ads are actually in the archive: the EU, plus the
# UK and the EEA states that adopted the same transparency rules.
_EU_UK = frozenset({
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU",
    "IE", "IS", "IT", "LI", "LT", "LU", "LV", "MT", "NL", "NO", "PL", "PT", "RO",
    "SE", "SI", "SK", "ES", "GB",
})

# Suggested when someone asks for a country the API cannot answer commercially.
_EU_SUGGESTIONS = ("IE", "DE", "FR", "NL", "ES", "GB")

# Always safe to request, in any country, for any ad type.
_BASE_FIELDS = [
    "id", "page_id", "page_name", "ad_creative_bodies", "ad_creative_link_titles",
    "ad_creative_link_captions", "ad_creative_link_descriptions", "ad_snapshot_url",
    "ad_creation_time", "ad_delivery_start_time", "ad_delivery_stop_time",
    "publisher_platforms", "languages", "currency",
]

# EU/UK transparency extras. Asking for these elsewhere is an error, not an
# empty column, which is why _fields_for exists at all.
_EU_FIELDS = ["eu_total_reach", "target_ages", "target_gender", "target_locations",
              "beneficiary_payers"]

# Political and social-issue ads only. The one place spend and impressions live.
_POLITICAL_FIELDS = ["impressions", "spend", "demographic_distribution",
                     "delivery_by_region", "estimated_audience_size", "bylines"]

_POLITICAL_TYPES = {"POLITICAL_AND_ISSUE_ADS"}


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------

def _token() -> tuple[str, str]:
    """(token, source). Prefers a token that is actually allowed to read the archive.

    The env token comes before the user's own Meta login on purpose. Meta gates
    ads_archive on the token holder having completed government-ID verification;
    an ordinary connected user has not, and their token fails with an error that
    reads exactly like a broken connection. One verified operator identity in
    META_AD_LIBRARY_TOKEN serves every customer, the same shape as
    MICROSOFT_ADS_DEVELOPER_TOKEN.
    """
    ctx_token = current_connection_token_ctx.get(None)
    if ctx_token:
        return ctx_token, "context"

    env_token = (os.environ.get("META_AD_LIBRARY_TOKEN") or "").strip()
    if env_token:
        return env_token, "env"

    # Last resort: the user's own Meta token. Reused from meta_ads rather than
    # re-implemented, so multi-connection routing, the legacy token and the
    # organic-pages fallback all keep working and stay in one place.
    try:
        from tools.meta_ads import _token as _meta_token
        user_token = _meta_token()
        if user_token:
            return user_token, "user"
    except RuntimeError:
        pass
    return "", "none"


def _no_token_error() -> str:
    return _json({
        "error": "Meta Ad Library needs an access token whose owner is ID-verified with Meta.",
        "token_source": "none",
        "note": (
            "Meta gates the public Ad Library API on the identity of whoever holds the "
            "token, not on the advertiser being searched. This is NOT a sign that the "
            "user's Meta Ads account is disconnected or broken -- their own account "
            "tools (meta_ads_action) are unaffected by this."
        ),
        "next_step": (
            "Two ways forward. (1) The operator sets META_AD_LIBRARY_TOKEN on the server "
            "from one Meta identity that has completed ID verification at "
            "https://www.facebook.com/ID -- one token then serves every user. (2) The "
            f"user connects Meta at {connect_hint()} and completes Meta's own ID verification. "
            "Until one of those is done this family cannot return data. Meanwhile, "
            "google_ads_transparency_search_ads needs no token at all and covers the "
            "same brands on Google Search, YouTube and Display -- offer that instead."
        ),
        "alternative_action": "google_ads_transparency_search_ads",
    })


# ---------------------------------------------------------------------------
# Query building
# ---------------------------------------------------------------------------

def _countries(value: Any) -> list[str]:
    """Accept 'US', 'US,GB', or a list. Uppercased, de-duplicated, order kept."""
    if isinstance(value, (list, tuple)):
        raw = list(value)
    else:
        raw = str(value or "").replace(";", ",").split(",")
    out: list[str] = []
    for item in raw:
        code = str(item).strip().upper()
        if code and code not in out:
            out.append(code)
    return out


def _has_eu(countries: list[str]) -> bool:
    return any(c in _EU_UK for c in countries)


def _fields_for(countries: list[str], ad_type: str) -> list[str]:
    """Only ask for fields this query is allowed to have.

    Requesting an EU-only or political-only field outside its scope is a hard
    Graph #100, not a null column, so the field list narrows with the query.
    """
    fields = list(_BASE_FIELDS)
    if _has_eu(countries):
        fields += _EU_FIELDS
    if (ad_type or "").upper() in _POLITICAL_TYPES:
        fields += _POLITICAL_FIELDS
    return fields


def _library_url(search: str = "", country: str = "US", page_id: str = "") -> str:
    from urllib.parse import quote
    if page_id:
        return (f"{LIBRARY_SITE}?active_status=all&ad_type=all&country={country}"
                f"&view_all_page_id={page_id}")
    return (f"{LIBRARY_SITE}?active_status=all&ad_type=all&country={country}"
            f"&q={quote(search or '')}")


def _archive_get(params: dict, refresh: bool = False, ttl: int = 3600) -> dict:
    """One archive call, cached, with the reduced-field retry.

    Retries once with only the always-safe fields on Graph #100 -- that error
    means a field is not available for this region or ad type, and returning the
    rows without that column beats returning nothing.
    """
    token, source = _token()
    if not token:
        return {"__no_token__": True}

    def _load() -> dict:
        query = dict(params)
        query["access_token"] = token
        try:
            resp = requests.get(ARCHIVE_URL, params=query, timeout=_TIMEOUT)
        except requests.RequestException as exc:
            return {"error": {"message": f"Could not reach Meta's Ad Library API: {exc}"}}
        try:
            payload = resp.json()
        except ValueError:
            return {"error": {"message": f"Meta returned an unreadable response "
                                         f"(HTTP {resp.status_code})."}}
        if resp.ok:
            return payload
        err = payload.get("error", {}) if isinstance(payload, dict) else {}
        if err.get("code") == 100 and query.get("fields") != ",".join(_BASE_FIELDS):
            retry = dict(query)
            retry["fields"] = ",".join(_BASE_FIELDS)
            try:
                second = requests.get(ARCHIVE_URL, params=retry, timeout=_TIMEOUT)
                if second.ok:
                    out = second.json()
                    out["note_fields"] = (
                        "Some fields are not available for this region or ad type, so they "
                        "were dropped and the rest returned. Reach and targeting detail "
                        "exists only for EU/UK ads; spend and impressions only for "
                        "political and social-issue ads."
                    )
                    return out
            except (requests.RequestException, ValueError):
                pass
        return {"error": err or {"message": f"Meta returned HTTP {resp.status_code}."}}

    # The token never enters the cache key -- see tools/public_cache. The archive
    # is public, so the same query has the same answer for everybody.
    key = cache_key("meta_archive", json.dumps(
        {k: v for k, v in sorted(params.items())}, sort_keys=True))
    result = cached(key, ttl, _load, refresh=refresh)
    if isinstance(result, dict) and "__token_source__" not in result:
        result = dict(result)
        result["__token_source__"] = source
    return result


def _graph_error(payload: dict) -> Optional[str]:
    """Turn a Graph error into something an assistant can act on, or None."""
    err = payload.get("error")
    if not err:
        return None
    if isinstance(err, str):
        return _json({"error": err})
    code = err.get("code")
    message = err.get("message") or "Meta's Ad Library API returned an error."
    out: dict[str, Any] = {"error": message, "meta_error_code": code}
    if code == 190:
        token, source = _token()
        out["note"] = (
            f"The access token used for the Ad Library ({source}) is invalid or expired. "
            f"This is the research token, NOT the user's own Meta Ads connection -- their "
            f"own account tools are unaffected."
        )
        out["next_step"] = (
            "If token_source is 'env', the operator needs to refresh META_AD_LIBRARY_TOKEN. "
            "Do not send the user to reconnect Meta Ads; that is a different credential. "
            "Offer google_ads_transparency_search_ads meanwhile."
        )
    elif code in (4, 17, 32, 613):
        out["note"] = ("Meta is rate limiting the shared Ad Library research token. "
                       "This is not a problem with the user's account.")
        out["next_step"] = ("Tell the user to try again in a few minutes. Do not retry in "
                            "a loop. google_ads_transparency_search_ads is not affected by "
                            "this limit.")
    elif code == 10 or code == 200:
        out["note"] = (
            "Meta refused the Ad Library request for permissions reasons. The usual cause "
            "is that the token holder has not completed Meta's ID verification, which the "
            "Ad Library API requires. This is NOT about the user's ad account permissions."
        )
        out["next_step"] = (
            "See meta_ad_library_check_access for which token is in use. Offer "
            "google_ads_transparency_search_ads, which needs no token."
        )
    return _json(out)


# ---------------------------------------------------------------------------
# The empty-result explanation — the deliverable for non-EU markets
# ---------------------------------------------------------------------------

def _explain_ad_library_gap(payload: dict, params: dict) -> dict:
    """Say why an archive query came back with nothing.

    Modelled on meta_ads._explain_empty_insights and there for the same reason:
    an empty list is the same shape whether the brand runs no ads, the API does
    not carry them in this country, or something is misconfigured. Outside the
    EU/UK the answer is almost always the second one, and that is the case an
    assistant is least able to guess and most likely to misreport.
    """
    if not isinstance(payload, dict) or payload.get("data") != []:
        return payload

    countries = _countries(params.get("__countries") or "")
    term = params.get("search_terms") or params.get("__page_id") or ""
    country_label = ", ".join(countries) or "the requested country"
    ad_type = str(params.get("ad_type") or "ALL").upper()

    if not _has_eu(countries) and ad_type not in _POLITICAL_TYPES:
        payload["note"] = (
            f"Meta returned no ads for {term!r} in {country_label}. The call succeeded -- "
            f"this is not an error, a permission problem, a broken connection or a missing "
            f"token. Meta's Ad Library API returns only political and social-issue ads "
            f"outside the EU and UK. Ordinary commercial ads for {country_label} are "
            f"visible on the Ad Library website but are not in the API at all, so an empty "
            f"result here says nothing about whether this brand is advertising."
        )
        payload["next_step"] = (
            "Do NOT tell the user Meta is disconnected, that access is missing, or that "
            "this brand runs no ads. Do exactly one of these and say which you did: "
            f"(1) re-run with countries set to an EU member state or GB -- for example "
            f"{', '.join(_EU_SUGGESTIONS[:4])} -- where the full commercial archive is "
            f"available, and tell the user the result covers ads delivered in that country "
            f"only; (2) if the brand only advertises outside the EU, call "
            f"google_ads_transparency_search_ads instead -- Google publishes every "
            f"advertiser's ads in 180+ countries with no such restriction; (3) offer the "
            f"user the manual_url below to check by hand."
        )
        payload["coverage"] = "political_only"
        payload["alternative_action"] = "google_ads_transparency_search_ads"
    else:
        payload["note"] = (
            f"Meta returned no archived ads for {term!r} in {country_label}. The call "
            f"succeeded. In the EU and UK the API does cover ordinary commercial ads, so "
            f"this most likely means this advertiser is not currently running ads that "
            f"reached {country_label}."
        )
        payload["next_step"] = (
            "Check, in this order: whether the Page is right -- "
            "meta_ad_library_find_advertiser resolves a brand name to a page_id, and a "
            "keyword search misses brands whose ad copy never contains their own name; "
            "whether ad_active_status='ALL' would surface ads that have since stopped; and "
            "whether the brand targets a different EU country. Report which it was."
        )
        payload["coverage"] = "full_commercial"

    payload["manual_url"] = _library_url(
        str(term), countries[0] if countries else "US",
        str(params.get("__page_id") or ""))
    return payload


# ---------------------------------------------------------------------------
# Normalisation -> the shared competitor card shape
# ---------------------------------------------------------------------------

def _first_text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else ""
    return str(value or "")


def normalize_ad(row: dict, countries: list[str]) -> dict:
    """One archived ad -> the card shape shared with the Google family.

    thumbnail is deliberately empty. ad_snapshot_url is an HTML page that only
    renders with an access token in the query string, and Meta serves it with
    framing restrictions -- it is neither an image nor something that can be
    embedded, so Meta cards are text-first with a link out. Putting the token
    into a chat host's page to make a picture appear is not a trade worth making.
    """
    page_id = str(row.get("page_id") or "")
    return {
        "platform": "meta",
        "advertiser": row.get("page_name") or "",
        "advertiser_id": page_id,
        "ad_id": str(row.get("id") or ""),
        "format": "",
        "thumbnail": "",
        "thumbnail_width": 0,
        "thumbnail_height": 0,
        "preview_url": "",
        "headline": _first_text(row.get("ad_creative_link_titles")),
        "body": _first_text(row.get("ad_creative_bodies")),
        "cta": _first_text(row.get("ad_creative_link_captions")),
        "destination": _first_text(row.get("ad_creative_link_descriptions")),
        "first_seen": str(row.get("ad_delivery_start_time") or "")[:10],
        "last_seen": str(row.get("ad_delivery_stop_time") or "")[:10],
        "is_active": not row.get("ad_delivery_stop_time"),
        "surfaces": list(row.get("publisher_platforms") or []),
        "regions": countries,
        # The archived ad itself. Opens in a browser; not embeddable.
        "detail_url": str(row.get("ad_snapshot_url") or ""),
    }


def _run_query(params: dict, countries: list[str], limit: int,
               refresh: bool, term: str, page_id: str = "") -> dict:
    """Shared body for every search-shaped tool."""
    raw = _archive_get(params, refresh=refresh)
    if raw.get("__no_token__"):
        return {"__no_token__": True}
    err = _graph_error(raw)
    if err:
        return {"__error_json__": err}

    explained = _explain_ad_library_gap(
        dict(raw),
        {**params, "__countries": ",".join(countries), "__page_id": page_id,
         "search_terms": term},
    )
    rows = explained.get("data") or []
    ads = [normalize_ad(r, countries) for r in rows[:limit]]
    out: dict[str, Any] = {
        "success": True,
        "ads": ads,
        "count": len(ads),
        "countries": countries,
        "coverage": explained.get("coverage") or (
            "full_commercial" if _has_eu(countries) else "political_only"),
        "token_source": raw.get("__token_source__", "unknown"),
        "after": ((explained.get("paging") or {}).get("cursors") or {}).get("after", ""),
    }
    for key in ("note", "next_step", "manual_url", "alternative_action", "note_fields"):
        if explained.get(key):
            out[key] = explained[key]
    if ads and not _has_eu(countries):
        out["note"] = (
            f"These are political or social-issue ads. Outside the EU and UK, Meta's API "
            f"carries no other kind, so this is not a full picture of what "
            f"{term or 'this advertiser'} advertises. For ordinary commercial ads use an "
            f"EU country or GB, or google_ads_transparency_search_ads."
        )
    return out


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def meta_ad_library_check_access() -> str:
    """[RECOMMENDED] Check whether Meta's Ad Library can be read, and how.

    Run this first in any Meta competitor-research session, and whenever a
    search comes back empty or refused. It reports which token resolved, whether
    the archive answers, and which countries actually return commercial ads --
    so an empty search can be explained instead of guessed at. Reads nothing
    from the user's own ad account.
    """
    token, source = _token()
    report: dict[str, Any] = {
        "token_source": source,
        "has_token": bool(token),
        "cache": stats(),
        "commercial_coverage_countries": sorted(_EU_UK),
        "note": (
            "Meta's Ad Library API returns ordinary commercial ads only for the EU and UK. "
            "Everywhere else it carries political and social-issue ads only. That is a "
            "Meta rule, not a setting on this server, and it is why a US or Israeli "
            "competitor search can come back empty while the brand is clearly advertising."
        ),
    }
    if not token:
        report["success"] = False
        report["error"] = "No Ad Library token is available."
        report["next_step"] = json.loads(_no_token_error())["next_step"]
        return _json(report)

    probe = _archive_get({
        "search_terms": "insurance",
        "ad_reached_countries": json.dumps(["IE"]),
        "ad_type": "ALL",
        "fields": ",".join(_BASE_FIELDS),
        "limit": 1,
    }, ttl=600)
    err = probe.get("error")
    if err:
        report["success"] = False
        report["error"] = err.get("message") if isinstance(err, dict) else str(err)
        report["meta_error_code"] = err.get("code") if isinstance(err, dict) else None
        report["next_step"] = (
            "If this mentions verification or permissions, the token holder has not "
            "completed Meta's ID verification, which the Ad Library API requires. Tell the "
            "user competitor research on Meta is unavailable on this server and offer "
            "google_ads_transparency_search_ads, which needs no token. Do NOT tell them "
            "their own Meta Ads connection is broken."
        )
        return _json(report)

    report["success"] = True
    report["archive_answers"] = True
    report["probe_rows"] = len(probe.get("data") or [])
    return _json(report)


@mcp.tool()
def meta_ad_library_search_ads(search_terms: str, countries: str = "US",
                               ad_type: str = "ALL", ad_active_status: str = "ACTIVE",
                               media_type: str = "", publisher_platforms: str = "",
                               date_min: str = "", date_max: str = "",
                               limit: int = 25, after: str = "",
                               refresh: bool = False) -> str:
    """[RECOMMENDED] Search Meta's public Ad Library by keyword.

    Searches ad copy across advertisers. Note that a keyword search misses
    brands whose ads never mention their own name -- use
    meta_ad_library_find_advertiser when you want everything one company runs.

    COVERAGE: ordinary commercial ads come back only for EU countries and GB.
    For the US, Israel, Canada and elsewhere Meta's API carries political and
    social-issue ads only, and a commercial search there returns empty. That is
    not an error; read the `note` in the response and follow its `next_step`.

    Args:
        search_terms: What to search for in ad text, e.g. 'house painting'.
        countries: Two-letter country codes, comma separated, e.g. 'IE' or
            'DE,FR'. Use an EU country or 'GB' for commercial ads.
        ad_type: 'ALL' (default), 'POLITICAL_AND_ISSUE_ADS', 'HOUSING_ADS',
            'EMPLOYMENT_ADS', 'FINANCIAL_PRODUCTS_AND_SERVICES_ADS'.
        ad_active_status: 'ACTIVE' (default), 'INACTIVE' or 'ALL'.
        media_type: Optional 'IMAGE', 'VIDEO', 'MEME', 'NONE'.
        publisher_platforms: Optional, comma separated, e.g. 'FACEBOOK,INSTAGRAM'.
        date_min: Optional earliest delivery date, YYYY-MM-DD.
        date_max: Optional latest delivery date, YYYY-MM-DD.
        limit: Max ads to return. Default 25.
        after: Paging cursor from a previous call's `after`.
        refresh: Skip the cache and ask Meta again.
    """
    if not search_terms:
        return _json({"error": "search_terms is required.",
                      "next_step": "To list one advertiser's ads instead, use "
                                   "meta_ad_library_find_advertiser then "
                                   "meta_ad_library_advertiser_ads."})
    codes = _countries(countries) or ["US"]
    params: dict[str, Any] = {
        "search_terms": search_terms,
        "ad_reached_countries": json.dumps(codes),
        "ad_type": (ad_type or "ALL").upper(),
        "ad_active_status": (ad_active_status or "ACTIVE").upper(),
        "fields": ",".join(_fields_for(codes, ad_type)),
        "limit": max(min(int(limit or 25), 100), 1),
    }
    if media_type:
        params["media_type"] = media_type.upper()
    if publisher_platforms:
        params["publisher_platforms"] = json.dumps(
            [p.strip().upper() for p in publisher_platforms.split(",") if p.strip()])
    if date_min:
        params["ad_delivery_date_min"] = date_min
    if date_max:
        params["ad_delivery_date_max"] = date_max
    if after:
        params["after"] = after

    result = _run_query(params, codes, int(limit or 25), refresh, search_terms)
    if result.get("__no_token__"):
        return _no_token_error()
    if result.get("__error_json__"):
        return result["__error_json__"]
    result["search_terms"] = search_terms
    return _json(result)


@mcp.tool()
def meta_ad_library_find_advertiser(name: str, countries: str = "IE",
                                    limit: int = 25, refresh: bool = False) -> str:
    """[RECOMMENDED] Turn a brand name into the Facebook page_id that runs its ads.

    The reliable way in: keyword search misses any brand whose ad copy does not
    contain its own name, and page-scoped lookups do not. Returns the Pages
    found in the archive with how many of their ads matched, most first.

    Defaults to Ireland because the API only carries commercial ads for the
    EU/UK -- searching 'US' for a commercial brand finds nothing to resolve.

    Args:
        name: Brand or company name, e.g. 'Wix', 'Booking.com'.
        countries: Two-letter country codes, comma separated. Default 'IE'.
        limit: How many ads to scan for pages. Default 25.
        refresh: Skip the cache and ask Meta again.
    """
    if not name:
        return _json({"error": "name is required."})
    codes = _countries(countries) or ["IE"]
    params = {
        "search_terms": name,
        "ad_reached_countries": json.dumps(codes),
        "ad_type": "ALL",
        "ad_active_status": "ALL",
        "fields": "id,page_id,page_name",
        "limit": max(min(int(limit or 25) * 4, 100), 1),
    }
    raw = _archive_get(params, refresh=refresh, ttl=86400)
    if raw.get("__no_token__"):
        return _no_token_error()
    err = _graph_error(raw)
    if err:
        return err

    pages: dict[str, dict] = {}
    for row in raw.get("data") or []:
        page_id = str(row.get("page_id") or "")
        if not page_id:
            continue
        entry = pages.setdefault(page_id, {
            "page_id": page_id, "page_name": row.get("page_name") or "",
            "matching_ads": 0,
            "detail_url": _library_url(country=codes[0], page_id=page_id),
        })
        entry["matching_ads"] += 1

    found = sorted(pages.values(), key=lambda p: p["matching_ads"], reverse=True)
    out: dict[str, Any] = {
        "success": True, "name": name, "countries": codes,
        "advertisers": found, "count": len(found),
        "token_source": raw.get("__token_source__", "unknown"),
    }
    if not found:
        explained = _explain_ad_library_gap(
            {"data": []}, {**params, "__countries": ",".join(codes), "search_terms": name})
        out["note"] = explained.get("note", "")
        out["next_step"] = explained.get("next_step", "")
        out["manual_url"] = explained.get("manual_url", "")
        out["coverage"] = explained.get("coverage", "")
    return _json(out)


@mcp.tool()
def meta_ad_library_advertiser_ads(page_id: str, countries: str = "IE",
                                   ad_active_status: str = "ACTIVE",
                                   limit: int = 25, after: str = "",
                                   refresh: bool = False) -> str:
    """[RECOMMENDED] List every archived ad for one Facebook Page.

    Takes the page_id from meta_ad_library_find_advertiser. This is the "what is
    this competitor running right now" call, and unlike a keyword search it does
    not depend on the brand naming itself in its own ad copy.

    Args:
        page_id: Facebook Page ID of the advertiser.
        countries: Two-letter country codes, comma separated. Default 'IE'.
            EU countries and 'GB' return commercial ads; elsewhere only political.
        ad_active_status: 'ACTIVE' (default), 'INACTIVE' or 'ALL'.
        limit: Max ads to return. Default 25.
        after: Paging cursor from a previous call's `after`.
        refresh: Skip the cache and ask Meta again.
    """
    if not page_id:
        return _json({"error": "page_id is required.",
                      "next_step": "Call meta_ad_library_find_advertiser first to turn a "
                                   "brand name into a page_id."})
    codes = _countries(countries) or ["IE"]
    params: dict[str, Any] = {
        "search_page_ids": json.dumps([str(page_id)]),
        "ad_reached_countries": json.dumps(codes),
        "ad_type": "ALL",
        "ad_active_status": (ad_active_status or "ACTIVE").upper(),
        "fields": ",".join(_fields_for(codes, "ALL")),
        "limit": max(min(int(limit or 25), 100), 1),
    }
    if after:
        params["after"] = after

    result = _run_query(params, codes, int(limit or 25), refresh, "", str(page_id))
    if result.get("__no_token__"):
        return _no_token_error()
    if result.get("__error_json__"):
        return result["__error_json__"]
    result["page_id"] = str(page_id)
    result["detail_url"] = _library_url(country=codes[0], page_id=str(page_id))
    return _json(result)


@mcp.tool()
def meta_ad_library_ad_details(ad_archive_id: str, countries: str = "IE") -> str:
    """Get one archived ad with every field its region and type allow.

    Args:
        ad_archive_id: The Ad Library ID, from any of the search tools' `ad_id`.
        countries: Two-letter country codes, comma separated. Default 'IE'.
    """
    if not ad_archive_id:
        return _json({"error": "ad_archive_id is required."})
    codes = _countries(countries) or ["IE"]
    raw = _archive_get({
        "ad_reached_countries": json.dumps(codes),
        "search_terms": "",
        "ad_type": "ALL",
        "ad_active_status": "ALL",
        "fields": ",".join(_fields_for(codes, "ALL") + _POLITICAL_FIELDS),
        "limit": 100,
    }, ttl=3600)
    if raw.get("__no_token__"):
        return _no_token_error()
    err = _graph_error(raw)
    if err:
        return err

    match = next((r for r in (raw.get("data") or [])
                  if str(r.get("id")) == str(ad_archive_id)), None)
    if not match:
        return _json({
            "success": True, "ad": None,
            "note": (f"Ad {ad_archive_id} was not in the archive slice for "
                     f"{', '.join(codes)}. The call succeeded. Ad Library IDs are scoped "
                     f"to the countries an ad reached, so try the country the ad was "
                     f"found in."),
            "manual_url": _library_url(country=codes[0]),
        })
    card = normalize_ad(match, codes)
    card["raw"] = match
    return _json({"success": True, "ad": card,
                  "token_source": raw.get("__token_source__", "unknown")})


@mcp.tool()
def meta_ad_library_creative_angles(page_id: str, countries: str = "IE",
                                    limit: int = 100, refresh: bool = False) -> str:
    """Summarise the angles one advertiser is running on Facebook and Instagram.

    The analysis tool: which headlines and calls to action they repeat, which
    ads have run longest, how their ads split across Facebook and Instagram, and
    how much is still live. Use this to answer "what is their pitch" rather than
    listing fifty near-identical creatives.

    Args:
        page_id: Facebook Page ID, from meta_ad_library_find_advertiser.
        countries: Two-letter country codes, comma separated. Default 'IE'.
        limit: How many ads to analyse, up to 100. Default 100.
        refresh: Skip the cache and ask Meta again.
    """
    if not page_id:
        return _json({"error": "page_id is required.",
                      "next_step": "Call meta_ad_library_find_advertiser first."})
    codes = _countries(countries) or ["IE"]
    params = {
        "search_page_ids": json.dumps([str(page_id)]),
        "ad_reached_countries": json.dumps(codes),
        "ad_type": "ALL",
        "ad_active_status": "ALL",
        "fields": ",".join(_fields_for(codes, "ALL")),
        "limit": max(min(int(limit or 100), 100), 1),
    }
    result = _run_query(params, codes, int(limit or 100), refresh, "", str(page_id))
    if result.get("__no_token__"):
        return _no_token_error()
    if result.get("__error_json__"):
        return result["__error_json__"]

    ads = result.get("ads") or []
    if not ads:
        return _json(result)

    def tally(key: str) -> list[dict]:
        counts: dict[str, int] = {}
        for ad in ads:
            value = (ad.get(key) or "").strip()
            if value:
                counts[value] = counts.get(value, 0) + 1
        return [{"text": t, "ads": n} for t, n in
                sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:10]]

    def run_days(ad: dict) -> int:
        try:
            from datetime import date
            start = date.fromisoformat(ad["first_seen"])
            end = date.fromisoformat(ad["last_seen"]) if ad["last_seen"] else date.today()
            return (end - start).days
        except (ValueError, KeyError):
            return 0

    platforms: dict[str, int] = {}
    for ad in ads:
        for surface in ad.get("surfaces") or []:
            platforms[surface] = platforms.get(surface, 0) + 1

    longest = sorted(ads, key=run_days, reverse=True)[:10]
    return _json({
        "success": True,
        "page_id": str(page_id),
        "advertiser": ads[0]["advertiser"],
        "countries": codes,
        "coverage": result.get("coverage"),
        "analysed": len(ads),
        "still_running": sum(1 for a in ads if a.get("is_active")),
        "top_headlines": tally("headline"),
        "top_calls_to_action": tally("cta"),
        "platform_mix": platforms,
        "longest_running": [
            {"ad_id": a["ad_id"], "headline": a["headline"], "first_seen": a["first_seen"],
             "last_seen": a["last_seen"], "days_running": run_days(a),
             "is_active": a["is_active"], "detail_url": a["detail_url"]}
            for a in longest
        ],
        "token_source": result.get("token_source"),
        "note": result.get("note"),
        "detail_url": _library_url(country=codes[0], page_id=str(page_id)),
    })


@mcp.tool()
def meta_ad_library_eu_reach(page_id: str, countries: str = "IE",
                             limit: int = 50, refresh: bool = False) -> str:
    """EU/UK only: who an advertiser's ads actually reached, and who they targeted.

    Reach totals, age and gender targeting and included/excluded locations are
    published under EU and UK transparency rules and exist nowhere else in the
    archive. Refuses a non-EU country outright rather than returning an empty
    result that looks like a bug.

    Args:
        page_id: Facebook Page ID, from meta_ad_library_find_advertiser.
        countries: EU member state or 'GB'. Default 'IE'.
        limit: Max ads to return. Default 50.
        refresh: Skip the cache and ask Meta again.
    """
    if not page_id:
        return _json({"error": "page_id is required.",
                      "next_step": "Call meta_ad_library_find_advertiser first."})
    codes = _countries(countries) or ["IE"]
    outside = [c for c in codes if c not in _EU_UK]
    if outside or not codes:
        # Refuse before the network call: Meta would answer with a field error or
        # an empty column, and neither says why.
        return _json({
            "error": f"Reach and targeting data does not exist for {', '.join(outside)}.",
            "note": (
                "eu_total_reach, target_ages, target_gender and target_locations are "
                "published under EU and UK transparency rules only. Meta does not collect "
                "them for other countries, so there is nothing to return -- this is not a "
                "permission problem and not a fault on this server."
            ),
            "next_step": (
                f"Re-run with an EU country or GB -- for example "
                f"{', '.join(_EU_SUGGESTIONS[:4])}. For non-EU markets, "
                f"meta_ad_library_advertiser_ads still returns the creatives themselves, "
                f"just without reach or targeting."
            ),
            "eu_uk_countries": sorted(_EU_UK),
        })

    params = {
        "search_page_ids": json.dumps([str(page_id)]),
        "ad_reached_countries": json.dumps(codes),
        "ad_type": "ALL",
        "ad_active_status": "ALL",
        "fields": ",".join(["id", "page_name", "ad_delivery_start_time"] + _EU_FIELDS),
        "limit": max(min(int(limit or 50), 100), 1),
    }
    raw = _archive_get(params, refresh=refresh)
    if raw.get("__no_token__"):
        return _no_token_error()
    err = _graph_error(raw)
    if err:
        return err

    rows = raw.get("data") or []
    total_reach = 0
    for row in rows:
        try:
            total_reach += int(row.get("eu_total_reach") or 0)
        except (TypeError, ValueError):
            pass
    payload = {
        "success": True,
        "page_id": str(page_id),
        "countries": codes,
        "ads": rows,
        "count": len(rows),
        "combined_eu_reach": total_reach,
        "token_source": raw.get("__token_source__", "unknown"),
        "detail_url": _library_url(country=codes[0], page_id=str(page_id)),
    }
    if not rows:
        payload["note"] = (
            f"No ads with EU reach data for this Page in {', '.join(codes)}. The call "
            f"succeeded; the advertiser may not have run ads reaching that country."
        )
    if raw.get("note_fields"):
        payload["note_fields"] = raw["note_fields"]
    return _json(payload)
