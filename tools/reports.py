"""
Composite Report Tools — combine multiple data sources into single actionable reports.

Each tool pulls data from 2+ platforms in a single call, saving time and reducing
the number of AI turns needed to get a complete picture.

Tools:
  report_monthly_executive    — GA4 + Google Ads + Meta + GSC, MoM comparison
  report_seo_full             — GSC queries/pages + GA4 organic traffic
  report_paid_search_full     — Google Ads performance + GA4 paid search channel
  report_paid_social_full     — Meta Ads performance + GA4 paid social channel
  report_ecommerce_full       — Revenue view: GA4 + Google Ads + Meta
  report_new_client_audit     — Check what's connected and what's missing
  report_landing_page_quality — GA4 landing pages + Google Ads quality scores
  report_campaign_health_check— Flag underperforming campaigns across platforms
"""
from __future__ import annotations

import json
import os
from calendar import monthrange
from datetime import date, timedelta
from typing import Any

from auth import current_user_ctx
from mcp_instance import mcp


# ─────────────────────────────────────────────────────────────────────────────
# Credential / client helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ga4_creds():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from credentials import provider
    user = current_user_ctx.get(None)
    if not user:
        raise RuntimeError("Not authenticated.")
    rt = provider().google_token("ga4")
    if not rt:
        raise RuntimeError("GA4 not connected.")
    creds = Credentials(
        token=None,
        refresh_token=rt,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/analytics.readonly"],
    )
    creds.refresh(Request())
    return creds


def _gsc_creds():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from credentials import provider
    user = current_user_ctx.get(None)
    if not user:
        raise RuntimeError("Not authenticated.")
    rt = provider().google_token("gsc")
    if not rt:
        raise RuntimeError("Search Console not connected.")
    creds = Credentials(
        token=None,
        refresh_token=rt,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        # Must match the scope the GSC service token was granted
        # (oauth_google grants plain `webmasters`, not `.readonly`) or the
        # refresh fails with invalid_scope.
        scopes=["https://www.googleapis.com/auth/webmasters"],
    )
    creds.refresh(Request())
    return creds


def _gads_client(customer_id: str):
    from tools.google_ads import _get_client
    client, _ = _get_client(customer_id)
    return client


async def _gather_blocking(*calls) -> list:
    """Run independent blocking API calls at the same time.

    These composite reports are `async def` but every request inside them is a
    blocking requests.get, issued one after another. Three Meta insights calls
    that each take 6-8s cost 26s of a 45-second Action budget, and a Custom GPT
    given no answer inside 45s reports a dead turn to the user with nothing to
    act on. The calls describe the same window from different angles and do not
    feed each other, so there is no reason for them to queue.

    Running them through to_thread also stops them blocking the event loop,
    which they have been doing for every other request on the instance.

    Exceptions are returned rather than raised, so one failing section leaves
    the rest of the report intact -- the callers already guard each block.
    """
    import asyncio

    return await asyncio.gather(
        *(asyncio.to_thread(call) for call in calls), return_exceptions=True
    )


def _unwrap(value, default=None):
    """A gathered result, or the default when that call raised."""
    return default if isinstance(value, BaseException) else value


def _meta_req(path: str, params: dict | None = None) -> dict:
    import requests
    user = current_user_ctx.get(None)
    if not user:
        raise RuntimeError("Not authenticated.")
    token = user.get_meta_token()
    if not token:
        raise RuntimeError("Meta not connected.")
    p = {"access_token": token, **(params or {})}
    resp = requests.get(f"https://graph.facebook.com/v22.0/{path}", params=p, timeout=30)
    if resp.status_code >= 400:
        # Never surface raise_for_status()'s message — it embeds the request URL
        # including the access_token. Use Meta's JSON error body instead.
        try:
            msg = (resp.json().get("error", {}) or {}).get("message") or "request failed"
        except Exception:
            msg = "request failed"
        raise RuntimeError(f"Meta API error {resp.status_code}: {msg}")
    return resp.json()


# ─────────────────────────────────────────────────────────────────────────────
# GA4 REST helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ga4_report(property_id: str, body: dict) -> dict:
    from googleapiclient.discovery import build
    pid = property_id.strip()
    if not pid.startswith("properties/"):
        pid = f"properties/{pid}"
    svc = build("analyticsdata", "v1beta", credentials=_ga4_creds())
    return svc.properties().runReport(property=pid, body=body).execute()


# ─────────────────────────────────────────────────────────────────────────────
# Date helpers
# ─────────────────────────────────────────────────────────────────────────────

def _date_window(days: int) -> tuple[str, str]:
    today = date.today()
    return (today - timedelta(days=days)).isoformat(), (today - timedelta(days=1)).isoformat()


def _month_range(month: str) -> tuple[str, str]:
    """Return (start_iso, end_iso) for a given YYYY-MM string, capped at yesterday."""
    y, m = map(int, month.split("-"))
    last_day = monthrange(y, m)[1]
    end = date(y, m, last_day)
    yesterday = date.today() - timedelta(days=1)
    if end > yesterday:
        end = yesterday
    return date(y, m, 1).isoformat(), end.isoformat()


def _pct_change(new_val: float, old_val: float) -> float:
    if old_val == 0:
        return 0.0
    return round((new_val - old_val) / old_val * 100, 1)


# ═════════════════════════════════════════════════════════════════════════════
# 1. MONTHLY EXECUTIVE REPORT
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def report_monthly_executive(
    ga4_property_id: str = "",
    google_ads_customer_id: str = "",
    meta_ad_account_id: str = "",
    gsc_site_url: str = "",
    month: str = "",
) -> dict:
    """
    Full executive monthly report across all connected platforms.
    Compares selected month vs previous month for GA4, Google Ads, Meta Ads, and GSC.
    Pass any combination of platform IDs — platforms without an ID are skipped.

    Args:
        ga4_property_id: GA4 property ID (e.g. '123456789').
        google_ads_customer_id: Google Ads customer ID (e.g. '123-456-7890').
        meta_ad_account_id: Meta ad account ID (e.g. 'act_123456789').
        gsc_site_url: Search Console site URL (e.g. 'https://example.com/').
        month: Month to report in YYYY-MM format. Defaults to last full month.
    """
    if not month:
        today = date.today()
        first = date(today.year, today.month, 1)
        last_month = first - timedelta(days=1)
        month = last_month.strftime("%Y-%m")

    y, m = map(int, month.split("-"))
    this_start, this_end = _month_range(month)
    prev_y, prev_m = (y - 1, 12) if m == 1 else (y, m - 1)
    prev_start, prev_end = _month_range(f"{prev_y:04d}-{prev_m:02d}")

    out: dict[str, Any] = {
        "report": f"{month} Monthly Executive Summary",
        "vs_period": f"{prev_y:04d}-{prev_m:02d}",
        "platforms": {},
        "highlights": [],
        "alerts": [],
    }

    # ── GA4 ──────────────────────────────────────────────────────────────────
    if ga4_property_id:
        try:
            resp = _ga4_report(ga4_property_id, {
                "metrics": [
                    {"name": "sessions"},
                    {"name": "activeUsers"},
                    {"name": "newUsers"},
                    {"name": "conversions"},
                    {"name": "totalRevenue"},
                    {"name": "bounceRate"},
                ],
                "dateRanges": [
                    {"startDate": this_start, "endDate": this_end},
                    {"startDate": prev_start, "endDate": prev_end},
                ],
            })
            rows = resp.get("rows", [])

            def _ga4_vals(idx: int) -> dict:
                if idx >= len(rows):
                    return {}
                mv = rows[idx]["metricValues"]
                return {
                    "sessions": int(mv[0]["value"]),
                    "users": int(mv[1]["value"]),
                    "new_users": int(mv[2]["value"]),
                    "conversions": int(mv[3]["value"]),
                    "revenue": round(float(mv[4]["value"]), 2),
                    "bounce_rate": round(float(mv[5]["value"]) * 100, 1),
                }

            this_ga = _ga4_vals(0)
            prev_ga = _ga4_vals(1)
            changes = {
                "sessions_pct": _pct_change(this_ga.get("sessions", 0), prev_ga.get("sessions", 0)),
                "users_pct": _pct_change(this_ga.get("users", 0), prev_ga.get("users", 0)),
                "conversions_pct": _pct_change(this_ga.get("conversions", 0), prev_ga.get("conversions", 0)),
                "revenue_pct": _pct_change(this_ga.get("revenue", 0), prev_ga.get("revenue", 0)),
            }
            out["platforms"]["ga4"] = {"this_month": this_ga, "prev_month": prev_ga, "changes": changes}
            if changes["sessions_pct"] < -15:
                out["alerts"].append(f"GA4 sessions dropped {abs(changes['sessions_pct'])}% MoM")
            if changes["conversions_pct"] > 20:
                out["highlights"].append(f"GA4 conversions up {changes['conversions_pct']}% MoM")
            if changes["revenue_pct"] > 20:
                out["highlights"].append(f"GA4 revenue up {changes['revenue_pct']}% MoM")
        except Exception as e:
            out["platforms"]["ga4"] = {"error": str(e)}

    # ── Google Ads ────────────────────────────────────────────────────────────
    if google_ads_customer_id:
        try:
            cid = google_ads_customer_id.replace("-", "")
            client = _gads_client(google_ads_customer_id)
            svc = client.get_service("GoogleAdsService")

            def _gads_period(sd: str, ed: str) -> dict:
                rows = list(svc.search(customer_id=cid, query=f"""
                    SELECT metrics.cost_micros, metrics.clicks, metrics.impressions,
                           metrics.conversions, metrics.conversions_value,
                           metrics.ctr, metrics.average_cpc
                    FROM customer
                    WHERE segments.date BETWEEN '{sd}' AND '{ed}'
                """))
                if not rows:
                    return {}
                me = rows[0].metrics
                spend = me.cost_micros / 1_000_000
                return {
                    "spend": round(spend, 2),
                    "clicks": me.clicks,
                    "impressions": me.impressions,
                    "conversions": round(me.conversions, 1),
                    "revenue": round(me.conversions_value, 2),
                    "roas": round(me.conversions_value / spend, 2) if spend > 0 else 0,
                    "ctr": round(me.ctr * 100, 2),
                    "avg_cpc": round(me.average_cpc / 1_000_000, 2),
                }

            this_gads = _gads_period(this_start, this_end)
            prev_gads = _gads_period(prev_start, prev_end)
            changes = {
                "spend_pct": _pct_change(this_gads.get("spend", 0), prev_gads.get("spend", 0)),
                "conversions_pct": _pct_change(this_gads.get("conversions", 0), prev_gads.get("conversions", 0)),
                "roas_pct": _pct_change(this_gads.get("roas", 0), prev_gads.get("roas", 0)),
            }
            out["platforms"]["google_ads"] = {"this_month": this_gads, "prev_month": prev_gads, "changes": changes}
            if changes["roas_pct"] < -20:
                out["alerts"].append(f"Google Ads ROAS dropped {abs(changes['roas_pct'])}% MoM")
            if changes["roas_pct"] > 20:
                out["highlights"].append(f"Google Ads ROAS up {changes['roas_pct']}% MoM")
        except Exception as e:
            out["platforms"]["google_ads"] = {"error": str(e)}

    # ── Meta Ads ──────────────────────────────────────────────────────────────
    if meta_ad_account_id:
        try:
            def _meta_period(sd: str, ed: str) -> dict:
                resp = _meta_req(
                    f"{meta_ad_account_id}/insights",
                    {
                        "fields": "spend,clicks,impressions,ctr,cpc,purchase_roas",
                        "time_range": json.dumps({"since": sd, "until": ed}),
                        "level": "account",
                    },
                )
                d = resp.get("data", [{}])[0] if resp.get("data") else {}
                spend = float(d.get("spend", 0))
                roas_list = d.get("purchase_roas", [])
                roas = float(roas_list[0].get("value", 0)) if roas_list else 0
                return {
                    "spend": round(spend, 2),
                    "clicks": int(d.get("clicks", 0)),
                    "impressions": int(d.get("impressions", 0)),
                    "roas": round(roas, 2),
                    "ctr": round(float(d.get("ctr", 0)) * 100, 2),
                    "cpc": round(float(d.get("cpc", 0)), 2),
                }

            this_meta = _meta_period(this_start, this_end)
            prev_meta = _meta_period(prev_start, prev_end)
            changes = {
                "spend_pct": _pct_change(this_meta.get("spend", 0), prev_meta.get("spend", 0)),
                "roas_pct": _pct_change(this_meta.get("roas", 0), prev_meta.get("roas", 0)),
                "cpc_pct": _pct_change(this_meta.get("cpc", 0), prev_meta.get("cpc", 0)),
            }
            out["platforms"]["meta_ads"] = {"this_month": this_meta, "prev_month": prev_meta, "changes": changes}
            if this_meta.get("roas", 0) < 1.5:
                out["alerts"].append("Meta Ads ROAS below 1.5x this month")
        except Exception as e:
            out["platforms"]["meta_ads"] = {"error": str(e)}

    # ── GSC ───────────────────────────────────────────────────────────────────
    if gsc_site_url:
        try:
            from googleapiclient.discovery import build
            svc = build("searchconsole", "v1", credentials=_gsc_creds())

            def _gsc_period(sd: str, ed: str) -> dict:
                resp = svc.searchanalytics().query(
                    siteUrl=gsc_site_url,
                    body={"startDate": sd, "endDate": ed, "dimensions": [], "rowLimit": 1},
                ).execute()
                r = resp.get("rows", [{}])[0] if resp.get("rows") else {}
                return {
                    "clicks": int(r.get("clicks", 0)),
                    "impressions": int(r.get("impressions", 0)),
                    "ctr": round(r.get("ctr", 0) * 100, 2),
                    "avg_position": round(r.get("position", 0), 1),
                }

            this_gsc = _gsc_period(this_start, this_end)
            prev_gsc = _gsc_period(prev_start, prev_end)
            changes = {
                "clicks_pct": _pct_change(this_gsc.get("clicks", 0), prev_gsc.get("clicks", 0)),
                "impressions_pct": _pct_change(this_gsc.get("impressions", 0), prev_gsc.get("impressions", 0)),
                "position_change": round(this_gsc.get("avg_position", 0) - prev_gsc.get("avg_position", 0), 1),
            }
            out["platforms"]["gsc"] = {"this_month": this_gsc, "prev_month": prev_gsc, "changes": changes}
            if changes["clicks_pct"] < -20:
                out["alerts"].append(f"Organic clicks dropped {abs(changes['clicks_pct'])}% MoM")
            if changes["clicks_pct"] > 20:
                out["highlights"].append(f"Organic clicks up {changes['clicks_pct']}% MoM")
        except Exception as e:
            out["platforms"]["gsc"] = {"error": str(e)}

    # ── Combined paid spend total ─────────────────────────────────────────────
    gads_spend = out.get("platforms", {}).get("google_ads", {}).get("this_month", {}).get("spend", 0)
    meta_spend = out.get("platforms", {}).get("meta_ads", {}).get("this_month", {}).get("spend", 0)
    if gads_spend or meta_spend:
        out["total_paid_spend"] = round(gads_spend + meta_spend, 2)

    return out


# ═════════════════════════════════════════════════════════════════════════════
# 2. FULL SEO REPORT
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def report_seo_full(
    gsc_site_url: str = "",
    ga4_property_id: str = "",
    days: int = 30,
) -> dict:
    """
    Full SEO report combining Google Search Console and GA4 organic traffic data.
    Returns top queries, top pages, organic traffic metrics, landing pages, and conversions — one call.

    Args:
        gsc_site_url: Search Console site URL (e.g. 'https://example.com/').
        ga4_property_id: GA4 property ID. Optional — adds organic traffic and conversion detail.
        days: Number of days to look back. Default 30.
    """
    start, end = _date_window(days)
    out: dict[str, Any] = {"period": f"{start} to {end}", "gsc": {}, "ga4_organic": {}}

    # ── GSC ───────────────────────────────────────────────────────────────────
    if gsc_site_url:
        try:
            from googleapiclient.discovery import build
            svc = build("searchconsole", "v1", credentials=_gsc_creds())
            base = {"startDate": start, "endDate": end, "rowLimit": 25}
            order = [{"fieldName": "clicks", "sortOrder": "DESCENDING"}]

            totals_resp = svc.searchanalytics().query(
                siteUrl=gsc_site_url, body={**base, "dimensions": [], "rowLimit": 1}
            ).execute()
            tr = totals_resp.get("rows", [{}])[0] if totals_resp.get("rows") else {}

            q_resp = svc.searchanalytics().query(
                siteUrl=gsc_site_url, body={**base, "dimensions": ["query"], "orderBy": order}
            ).execute()

            p_resp = svc.searchanalytics().query(
                siteUrl=gsc_site_url, body={**base, "dimensions": ["page"], "orderBy": order}
            ).execute()

            out["gsc"] = {
                "totals": {
                    "clicks": int(tr.get("clicks", 0)),
                    "impressions": int(tr.get("impressions", 0)),
                    "ctr": round(tr.get("ctr", 0) * 100, 2),
                    "avg_position": round(tr.get("position", 0), 1),
                },
                "top_queries": [
                    {
                        "query": r["keys"][0],
                        "clicks": int(r["clicks"]),
                        "impressions": int(r["impressions"]),
                        "ctr": round(r["ctr"] * 100, 2),
                        "position": round(r["position"], 1),
                    }
                    for r in q_resp.get("rows", [])
                ],
                "top_pages": [
                    {
                        "page": r["keys"][0],
                        "clicks": int(r["clicks"]),
                        "impressions": int(r["impressions"]),
                        "ctr": round(r["ctr"] * 100, 2),
                        "position": round(r["position"], 1),
                    }
                    for r in p_resp.get("rows", [])
                ],
            }
        except Exception as e:
            out["gsc"] = {"error": str(e)}

    # ── GA4 organic ───────────────────────────────────────────────────────────
    if ga4_property_id:
        try:
            organic_filter = {
                "filter": {
                    "fieldName": "sessionDefaultChannelGroup",
                    "stringFilter": {"value": "Organic Search", "matchType": "EXACT"},
                }
            }

            ov = _ga4_report(ga4_property_id, {
                "metrics": [
                    {"name": "sessions"},
                    {"name": "activeUsers"},
                    {"name": "conversions"},
                    {"name": "totalRevenue"},
                    {"name": "bounceRate"},
                    {"name": "averageSessionDuration"},
                ],
                "dateRanges": [{"startDate": start, "endDate": end}],
                "dimensionFilter": organic_filter,
            })
            ov_rows = ov.get("rows", [])
            if ov_rows:
                mv = ov_rows[0]["metricValues"]
                out["ga4_organic"]["overview"] = {
                    "sessions": int(mv[0]["value"]),
                    "users": int(mv[1]["value"]),
                    "conversions": int(mv[2]["value"]),
                    "revenue": round(float(mv[3]["value"]), 2),
                    "bounce_rate": round(float(mv[4]["value"]) * 100, 1),
                    "avg_session_sec": round(float(mv[5]["value"]), 0),
                }

            lp = _ga4_report(ga4_property_id, {
                "dimensions": [{"name": "landingPagePlusQueryString"}],
                "metrics": [
                    {"name": "sessions"},
                    {"name": "conversions"},
                    {"name": "bounceRate"},
                ],
                "dateRanges": [{"startDate": start, "endDate": end}],
                "dimensionFilter": organic_filter,
                "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}],
                "limit": 20,
            })
            out["ga4_organic"]["top_landing_pages"] = [
                {
                    "page": r["dimensionValues"][0]["value"],
                    "sessions": int(r["metricValues"][0]["value"]),
                    "conversions": int(r["metricValues"][1]["value"]),
                    "bounce_rate": round(float(r["metricValues"][2]["value"]) * 100, 1),
                }
                for r in lp.get("rows", [])
            ]
        except Exception as e:
            out["ga4_organic"] = {"error": str(e)}

    return out


# ═════════════════════════════════════════════════════════════════════════════
# 3. FULL PAID SEARCH REPORT
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def report_paid_search_full(
    google_ads_customer_id: str = "",
    ga4_property_id: str = "",
    days: int = 30,
) -> dict:
    """
    Full paid search report: Google Ads account totals, top campaigns, top keywords,
    top search terms, and GA4 paid search channel attribution — all in one call.

    Args:
        google_ads_customer_id: Google Ads customer ID.
        ga4_property_id: GA4 property ID. Optional — adds paid channel session and conversion data.
        days: Number of days to report on. Default 30.
    """
    start, end = _date_window(days)
    out: dict[str, Any] = {"period": f"{start} to {end}", "google_ads": {}, "ga4_paid_search": {}}

    if google_ads_customer_id:
        try:
            cid = google_ads_customer_id.replace("-", "")
            client = _gads_client(google_ads_customer_id)
            svc = client.get_service("GoogleAdsService")

            def _q(query: str) -> list:
                return list(svc.search(customer_id=cid, query=query))

            # Account totals
            totals = _q(f"""
                SELECT metrics.cost_micros, metrics.clicks, metrics.impressions,
                       metrics.conversions, metrics.conversions_value,
                       metrics.ctr, metrics.average_cpc, metrics.cost_per_conversion
                FROM customer
                WHERE segments.date BETWEEN '{start}' AND '{end}'
            """)
            if totals:
                me = totals[0].metrics
                spend = me.cost_micros / 1_000_000
                out["google_ads"]["account"] = {
                    "spend": round(spend, 2),
                    "clicks": me.clicks,
                    "impressions": me.impressions,
                    "conversions": round(me.conversions, 1),
                    "revenue": round(me.conversions_value, 2),
                    "roas": round(me.conversions_value / spend, 2) if spend > 0 else 0,
                    "ctr": round(me.ctr * 100, 2),
                    "avg_cpc": round(me.average_cpc / 1_000_000, 2),
                    "cpa": round(me.cost_per_conversion / 1_000_000, 2) if me.conversions > 0 else 0,
                }

            # Top campaigns by spend
            camps = _q(f"""
                SELECT campaign.id, campaign.name,
                       metrics.cost_micros, metrics.clicks, metrics.conversions,
                       metrics.conversions_value, metrics.ctr
                FROM campaign
                WHERE segments.date BETWEEN '{start}' AND '{end}'
                  AND campaign.status = 'ENABLED'
                ORDER BY metrics.cost_micros DESC
                LIMIT 10
            """)
            out["google_ads"]["campaigns"] = [
                {
                    "id": str(r.campaign.id),
                    "name": r.campaign.name,
                    "spend": round(r.metrics.cost_micros / 1_000_000, 2),
                    "clicks": r.metrics.clicks,
                    "conversions": round(r.metrics.conversions, 1),
                    "roas": round(r.metrics.conversions_value / (r.metrics.cost_micros / 1_000_000), 2)
                    if r.metrics.cost_micros > 0 else 0,
                    "ctr": round(r.metrics.ctr * 100, 2),
                }
                for r in camps
            ]

            # Top keywords by spend
            kws = _q(f"""
                SELECT ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type,
                       metrics.cost_micros, metrics.clicks, metrics.conversions, metrics.average_cpc
                FROM keyword_view
                WHERE segments.date BETWEEN '{start}' AND '{end}'
                  AND campaign.status = 'ENABLED'
                  AND ad_group.status = 'ENABLED'
                  AND ad_group_criterion.status = 'ENABLED'
                ORDER BY metrics.cost_micros DESC
                LIMIT 20
            """)
            out["google_ads"]["top_keywords"] = [
                {
                    "keyword": r.ad_group_criterion.keyword.text,
                    "match_type": r.ad_group_criterion.keyword.match_type.name,
                    "spend": round(r.metrics.cost_micros / 1_000_000, 2),
                    "clicks": r.metrics.clicks,
                    "conversions": round(r.metrics.conversions, 1),
                    "avg_cpc": round(r.metrics.average_cpc / 1_000_000, 2),
                }
                for r in kws
            ]

            # Top search terms by impressions
            terms = _q(f"""
                SELECT search_term_view.search_term, search_term_view.status,
                       metrics.cost_micros, metrics.clicks, metrics.impressions,
                       metrics.conversions, metrics.ctr
                FROM search_term_view
                WHERE segments.date BETWEEN '{start}' AND '{end}'
                ORDER BY metrics.impressions DESC
                LIMIT 25
            """)
            out["google_ads"]["top_search_terms"] = [
                {
                    "term": r.search_term_view.search_term,
                    "match_status": r.search_term_view.status.name,
                    "spend": round(r.metrics.cost_micros / 1_000_000, 2),
                    "clicks": r.metrics.clicks,
                    "impressions": r.metrics.impressions,
                    "conversions": round(r.metrics.conversions, 1),
                    "ctr": round(r.metrics.ctr * 100, 2),
                }
                for r in terms
            ]
        except Exception as e:
            out["google_ads"] = {"error": str(e)}

    # ── GA4 paid search channel ───────────────────────────────────────────────
    if ga4_property_id:
        try:
            resp = _ga4_report(ga4_property_id, {
                "metrics": [
                    {"name": "sessions"},
                    {"name": "activeUsers"},
                    {"name": "conversions"},
                    {"name": "totalRevenue"},
                    {"name": "bounceRate"},
                ],
                "dateRanges": [{"startDate": start, "endDate": end}],
                "dimensionFilter": {
                    "filter": {
                        "fieldName": "sessionDefaultChannelGroup",
                        "stringFilter": {"value": "Paid Search", "matchType": "EXACT"},
                    }
                },
            })
            rows = resp.get("rows", [])
            if rows:
                mv = rows[0]["metricValues"]
                out["ga4_paid_search"] = {
                    "sessions": int(mv[0]["value"]),
                    "users": int(mv[1]["value"]),
                    "conversions": int(mv[2]["value"]),
                    "revenue": round(float(mv[3]["value"]), 2),
                    "bounce_rate": round(float(mv[4]["value"]) * 100, 1),
                }
        except Exception as e:
            out["ga4_paid_search"] = {"error": str(e)}

    return out


# ═════════════════════════════════════════════════════════════════════════════
# 4. FULL PAID SOCIAL REPORT
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def report_paid_social_full(
    meta_ad_account_id: str = "",
    ga4_property_id: str = "",
    days: int = 30,
) -> dict:
    """
    Full paid social report: Meta Ads account overview, top campaigns, demographic breakdown,
    and GA4 paid social channel attribution — all in one call.

    Args:
        meta_ad_account_id: Meta ad account ID (e.g. 'act_123456789').
        ga4_property_id: GA4 property ID. Optional — adds paid social session and conversion data.
        days: Number of days to report on. Default 30.
    """
    start, end = _date_window(days)
    out: dict[str, Any] = {"period": f"{start} to {end}", "meta_ads": {}, "ga4_paid_social": {}}

    if meta_ad_account_id:
        try:
            time_range = json.dumps({"since": start, "until": end})

            # One window, three angles, no dependency between them -- so they
            # go out together. Sequentially these three cost ~26s of a 45s
            # Action budget; concurrently they cost one call.
            _acct_r, _camps_r, _demo_r = await _gather_blocking(
                lambda: _meta_req(
                    f"{meta_ad_account_id}/insights",
                    {
                        "fields": "spend,clicks,impressions,ctr,cpc,purchase_roas,frequency,actions,action_values",
                        "time_range": time_range,
                        "level": "account",
                    },
                ),
                lambda: _meta_req(
                    f"{meta_ad_account_id}/insights",
                    {
                        "fields": "campaign_name,campaign_id,spend,clicks,impressions,ctr,purchase_roas",
                        "time_range": time_range,
                        "level": "campaign",
                        "limit": 10,
                        "sort": '["spend_descending"]',
                    },
                ),
                lambda: _meta_req(
                    f"{meta_ad_account_id}/insights",
                    {
                        "fields": "spend,clicks,impressions",
                        "time_range": time_range,
                        "breakdowns": ["age", "gender"],
                        "level": "account",
                        "limit": 20,
                    },
                ),
            )
            acct = _unwrap(_acct_r, {})
            d = acct.get("data", [{}])[0] if acct.get("data") else {}
            spend = float(d.get("spend", 0))
            roas_list = d.get("purchase_roas", [])
            roas = float(roas_list[0].get("value", 0)) if roas_list else 0
            purchases = sum(
                int(a.get("value", 0)) for a in d.get("actions", []) if a.get("action_type") == "purchase"
            )
            purchase_value = sum(
                float(av.get("value", 0)) for av in d.get("action_values", []) if av.get("action_type") == "purchase"
            )
            out["meta_ads"]["account"] = {
                "spend": round(spend, 2),
                "clicks": int(d.get("clicks", 0)),
                "impressions": int(d.get("impressions", 0)),
                "ctr": round(float(d.get("ctr", 0)) * 100, 2),
                "cpc": round(float(d.get("cpc", 0)), 2),
                "roas": round(roas, 2),
                "purchases": purchases,
                "purchase_value": round(purchase_value, 2),
                "frequency": round(float(d.get("frequency", 0)), 2),
            }

            camps = _unwrap(_camps_r, {})
            out["meta_ads"]["campaigns"] = [
                {
                    "id": c.get("campaign_id"),
                    "name": c.get("campaign_name"),
                    "spend": round(float(c.get("spend", 0)), 2),
                    "clicks": int(c.get("clicks", 0)),
                    "roas": round(float(c.get("purchase_roas", [{}])[0].get("value", 0))
                                  if c.get("purchase_roas") else 0, 2),
                    "ctr": round(float(c.get("ctr", 0)) * 100, 2),
                }
                for c in camps.get("data", [])
            ]

            demo = _unwrap(_demo_r, {})
            out["meta_ads"]["demographics"] = [
                {
                    "age": r.get("age"),
                    "gender": r.get("gender"),
                    "spend": round(float(r.get("spend", 0)), 2),
                    "clicks": int(r.get("clicks", 0)),
                    "impressions": int(r.get("impressions", 0)),
                }
                for r in demo.get("data", [])
            ]
        except Exception as e:
            out["meta_ads"] = {"error": str(e)}

    # ── GA4 paid social channel ───────────────────────────────────────────────
    if ga4_property_id:
        try:
            resp = _ga4_report(ga4_property_id, {
                "metrics": [
                    {"name": "sessions"},
                    {"name": "activeUsers"},
                    {"name": "conversions"},
                    {"name": "totalRevenue"},
                ],
                "dateRanges": [{"startDate": start, "endDate": end}],
                "dimensionFilter": {
                    "filter": {
                        "fieldName": "sessionDefaultChannelGroup",
                        "stringFilter": {"value": "Paid Social", "matchType": "EXACT"},
                    }
                },
            })
            rows = resp.get("rows", [])
            if rows:
                mv = rows[0]["metricValues"]
                out["ga4_paid_social"] = {
                    "sessions": int(mv[0]["value"]),
                    "users": int(mv[1]["value"]),
                    "conversions": int(mv[2]["value"]),
                    "revenue": round(float(mv[3]["value"]), 2),
                }
        except Exception as e:
            out["ga4_paid_social"] = {"error": str(e)}

    return out


# ═════════════════════════════════════════════════════════════════════════════
# 5. FULL ECOMMERCE REPORT
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def report_ecommerce_full(
    ga4_property_id: str = "",
    google_ads_customer_id: str = "",
    meta_ad_account_id: str = "",
    days: int = 30,
) -> dict:
    """
    Full ecommerce revenue report across all connected platforms.
    Combines GA4 revenue/products/channels, Google Ads ROAS, and Meta purchase ROAS in one call.

    Args:
        ga4_property_id: GA4 property ID.
        google_ads_customer_id: Google Ads customer ID. Optional.
        meta_ad_account_id: Meta ad account ID. Optional.
        days: Number of days to report on. Default 30.
    """
    start, end = _date_window(days)
    out: dict[str, Any] = {"period": f"{start} to {end}", "ga4": {}, "google_ads": {}, "meta_ads": {}}

    # ── GA4 ecommerce ─────────────────────────────────────────────────────────
    if ga4_property_id:
        try:
            # Overview metrics
            ov = _ga4_report(ga4_property_id, {
                "metrics": [
                    {"name": "totalRevenue"},
                    {"name": "transactions"},
                    {"name": "averagePurchaseRevenue"},
                    {"name": "ecommercePurchases"},
                    {"name": "sessions"},
                ],
                "dateRanges": [{"startDate": start, "endDate": end}],
            })
            ov_rows = ov.get("rows", [])
            if ov_rows:
                mv = ov_rows[0]["metricValues"]
                txns = int(mv[1]["value"])
                sessions = int(mv[4]["value"])
                out["ga4"]["overview"] = {
                    "revenue": round(float(mv[0]["value"]), 2),
                    "transactions": txns,
                    "avg_order_value": round(float(mv[2]["value"]), 2),
                    "purchases": int(mv[3]["value"]),
                    "conversion_rate": round(txns / sessions * 100, 2) if sessions > 0 else 0,
                }

            # Top products by revenue
            items = _ga4_report(ga4_property_id, {
                "dimensions": [{"name": "itemName"}],
                "metrics": [
                    {"name": "itemRevenue"},
                    {"name": "itemsPurchased"},
                    {"name": "itemsAddedToCart"},
                ],
                "dateRanges": [{"startDate": start, "endDate": end}],
                "orderBys": [{"metric": {"metricName": "itemRevenue"}, "desc": True}],
                "limit": 20,
            })
            out["ga4"]["top_products"] = [
                {
                    "name": r["dimensionValues"][0]["value"],
                    "revenue": round(float(r["metricValues"][0]["value"]), 2),
                    "units_sold": int(r["metricValues"][1]["value"]),
                    "add_to_carts": int(r["metricValues"][2]["value"]),
                }
                for r in items.get("rows", [])
            ]

            # Revenue by acquisition channel
            channels = _ga4_report(ga4_property_id, {
                "dimensions": [{"name": "sessionDefaultChannelGroup"}],
                "metrics": [
                    {"name": "totalRevenue"},
                    {"name": "transactions"},
                    {"name": "sessions"},
                ],
                "dateRanges": [{"startDate": start, "endDate": end}],
                "orderBys": [{"metric": {"metricName": "totalRevenue"}, "desc": True}],
            })
            out["ga4"]["revenue_by_channel"] = [
                {
                    "channel": r["dimensionValues"][0]["value"],
                    "revenue": round(float(r["metricValues"][0]["value"]), 2),
                    "transactions": int(r["metricValues"][1]["value"]),
                    "sessions": int(r["metricValues"][2]["value"]),
                }
                for r in channels.get("rows", [])
            ]
        except Exception as e:
            out["ga4"] = {"error": str(e)}

    # ── Google Ads campaign ROAS ──────────────────────────────────────────────
    if google_ads_customer_id:
        try:
            cid = google_ads_customer_id.replace("-", "")
            client = _gads_client(google_ads_customer_id)
            svc = client.get_service("GoogleAdsService")
            rows = list(svc.search(customer_id=cid, query=f"""
                SELECT campaign.name, campaign.advertising_channel_type,
                       metrics.cost_micros, metrics.conversions_value,
                       metrics.conversions, metrics.clicks
                FROM campaign
                WHERE segments.date BETWEEN '{start}' AND '{end}'
                  AND campaign.status = 'ENABLED'
                  AND metrics.cost_micros > 0
                ORDER BY metrics.cost_micros DESC
                LIMIT 15
            """))
            total_spend = 0.0
            total_revenue = 0.0
            campaigns = []
            for r in rows:
                spend = r.metrics.cost_micros / 1_000_000
                rev = r.metrics.conversions_value
                total_spend += spend
                total_revenue += rev
                campaigns.append({
                    "name": r.campaign.name,
                    "type": r.campaign.advertising_channel_type.name,
                    "spend": round(spend, 2),
                    "revenue": round(rev, 2),
                    "roas": round(rev / spend, 2) if spend > 0 else 0,
                    "conversions": round(r.metrics.conversions, 1),
                })
            out["google_ads"] = {
                "campaigns": campaigns,
                "total_spend": round(total_spend, 2),
                "total_revenue": round(total_revenue, 2),
                "blended_roas": round(total_revenue / total_spend, 2) if total_spend > 0 else 0,
            }
        except Exception as e:
            out["google_ads"] = {"error": str(e)}

    # ── Meta purchase ROAS ────────────────────────────────────────────────────
    if meta_ad_account_id:
        try:
            resp = _meta_req(
                f"{meta_ad_account_id}/insights",
                {
                    "fields": "spend,purchase_roas,actions,action_values",
                    "time_range": json.dumps({"since": start, "until": end}),
                    "level": "account",
                },
            )
            d = resp.get("data", [{}])[0] if resp.get("data") else {}
            spend = float(d.get("spend", 0))
            roas_list = d.get("purchase_roas", [])
            roas = float(roas_list[0].get("value", 0)) if roas_list else 0
            purchases = sum(int(a.get("value", 0)) for a in d.get("actions", []) if a.get("action_type") == "purchase")
            purchase_value = sum(float(av.get("value", 0)) for av in d.get("action_values", []) if av.get("action_type") == "purchase")
            out["meta_ads"] = {
                "spend": round(spend, 2),
                "purchase_roas": round(roas, 2),
                "purchases": purchases,
                "purchase_value": round(purchase_value, 2),
            }
        except Exception as e:
            out["meta_ads"] = {"error": str(e)}

    # ── Blended ROAS across paid channels ─────────────────────────────────────
    ga4_rev = out.get("ga4", {}).get("overview", {}).get("revenue", 0)
    gads_spend = out.get("google_ads", {}).get("total_spend", 0)
    meta_spend = out.get("meta_ads", {}).get("spend", 0)
    total_paid = gads_spend + meta_spend
    if ga4_rev and total_paid:
        out["blended_roas"] = round(ga4_rev / total_paid, 2)
        out["total_paid_spend"] = round(total_paid, 2)

    return out


# ═════════════════════════════════════════════════════════════════════════════
# 6. NEW CLIENT AUDIT
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def report_new_client_audit(
    ga4_property_id: str = "",
    google_ads_customer_id: str = "",
    meta_ad_account_id: str = "",
    gsc_site_url: str = "",
) -> dict:
    """
    Audit a new client's marketing setup — checks what's connected, what's missing, and what needs fixing.
    Run this first when onboarding a new account to get a prioritized checklist of issues.

    Args:
        ga4_property_id: GA4 property ID to audit.
        google_ads_customer_id: Google Ads customer ID to audit.
        meta_ad_account_id: Meta ad account ID to audit.
        gsc_site_url: Search Console site URL to audit.
    """
    out: dict[str, Any] = {
        "audit_date": date.today().isoformat(),
        "platforms": {},
        "issues": [],
        "recommendations": [],
        "score": 0,
        "max_score": 0,
    }
    score = 0
    max_score = 0

    # ── GA4 ──────────────────────────────────────────────────────────────────
    if ga4_property_id:
        max_score += 30
        checks: dict[str, Any] = {}
        try:
            from googleapiclient.discovery import build
            admin = build("analyticsadmin", "v1beta", credentials=_ga4_creds())
            prop_name = f"properties/{ga4_property_id.strip()}"

            try:
                prop = admin.properties().get(name=prop_name).execute()
                checks["property_found"] = True
                checks["property_name"] = prop.get("displayName", "")
                score += 10
            except Exception:
                checks["property_found"] = False
                out["issues"].append("GA4: Property not found or no access")

            try:
                resp = _ga4_report(ga4_property_id, {
                    "metrics": [{"name": "sessions"}],
                    "dateRanges": [{"startDate": "7daysAgo", "endDate": "today"}],
                })
                sessions = int(resp.get("rows", [{}])[0].get("metricValues", [{"value": "0"}])[0].get("value", 0)) \
                    if resp.get("rows") else 0
                checks["has_recent_data"] = sessions > 0
                checks["sessions_last_7d"] = sessions
                if sessions > 0:
                    score += 10
                else:
                    out["issues"].append("GA4: No sessions in last 7 days — tracking may be broken")
                    out["recommendations"].append("GA4: Verify the GA4 tag is firing correctly on all pages")
            except Exception:
                checks["has_recent_data"] = False

            try:
                key_events = admin.properties().keyEvents().list(parent=prop_name).execute()
                events = key_events.get("keyEvents", [])
                checks["conversion_events_count"] = len(events)
                checks["conversion_events"] = [e.get("eventName") for e in events]
                if events:
                    score += 10
                else:
                    out["issues"].append("GA4: No key events (conversions) configured")
                    out["recommendations"].append("GA4: Set up key events for purchases, leads, or form submissions")
            except Exception:
                checks["conversion_events_count"] = 0

        except Exception as e:
            checks["error"] = str(e)
        out["platforms"]["ga4"] = checks

    # ── Google Ads ────────────────────────────────────────────────────────────
    if google_ads_customer_id:
        max_score += 30
        checks = {}
        try:
            cid = google_ads_customer_id.replace("-", "")
            client = _gads_client(google_ads_customer_id)
            svc = client.get_service("GoogleAdsService")

            try:
                info = list(svc.search(customer_id=cid, query="""
                    SELECT customer.descriptive_name, customer.currency_code FROM customer LIMIT 1
                """))
                if info:
                    checks["account_accessible"] = True
                    checks["account_name"] = info[0].customer.descriptive_name
                    checks["currency"] = info[0].customer.currency_code
                    score += 10
            except Exception:
                checks["account_accessible"] = False
                out["issues"].append("Google Ads: Cannot access account — check customer ID and permissions")

            active_camps = list(svc.search(customer_id=cid, query="""
                SELECT campaign.id, campaign.name FROM campaign
                WHERE campaign.status = 'ENABLED' LIMIT 20
            """))
            checks["active_campaigns"] = len(active_camps)
            checks["campaign_names"] = [r.campaign.name for r in active_camps[:5]]
            if active_camps:
                score += 10
            else:
                out["issues"].append("Google Ads: No active campaigns found")

            conv_actions = list(svc.search(customer_id=cid, query="""
                SELECT conversion_action.id, conversion_action.name, conversion_action.status
                FROM conversion_action
                WHERE conversion_action.status = 'ENABLED' LIMIT 20
            """))
            checks["conversion_actions"] = len(conv_actions)
            checks["conversion_action_names"] = [r.conversion_action.name for r in conv_actions]
            if conv_actions:
                score += 10
            else:
                out["issues"].append("Google Ads: No conversion actions set up — campaigns cannot optimize for conversions")
                out["recommendations"].append("Google Ads: Create conversion actions and install the tracking tag")

        except Exception as e:
            checks["error"] = str(e)
        out["platforms"]["google_ads"] = checks

    # ── Meta Ads ──────────────────────────────────────────────────────────────
    if meta_ad_account_id:
        max_score += 20
        checks = {}
        try:
            acct = _meta_req(meta_ad_account_id, {"fields": "id,name,account_status,currency,timezone_name"})
            checks["account_name"] = acct.get("name")
            checks["currency"] = acct.get("currency")
            checks["account_status"] = acct.get("account_status")  # 1 = active
            if acct.get("account_status") == 1:
                score += 10

            pixels = _meta_req(f"{meta_ad_account_id}/adspixels", {"fields": "id,name,last_fired_time"})
            pixel_list = pixels.get("data", [])
            checks["pixels_count"] = len(pixel_list)
            checks["pixels"] = [
                {"id": p["id"], "name": p.get("name"), "last_fired": p.get("last_fired_time")}
                for p in pixel_list
            ]
            if pixel_list:
                score += 10
            else:
                out["issues"].append("Meta: No Pixel found — conversion tracking is not configured")
                out["recommendations"].append("Meta: Install the Meta Pixel and configure standard events (Purchase, Lead, etc.)")

        except Exception as e:
            checks["error"] = str(e)
        out["platforms"]["meta_ads"] = checks

    # ── GSC ───────────────────────────────────────────────────────────────────
    if gsc_site_url:
        max_score += 20
        checks = {}
        try:
            from googleapiclient.discovery import build
            svc = build("searchconsole", "v1", credentials=_gsc_creds())

            sites = svc.sites().list().execute().get("siteEntry", [])
            verified = [s["siteUrl"] for s in sites if s.get("permissionLevel") not in ("siteUnverifiedUser",)]
            checks["site_verified"] = gsc_site_url in verified or any(gsc_site_url in s for s in verified)
            if checks["site_verified"]:
                score += 10
            else:
                out["issues"].append("GSC: Site not verified — verify ownership to access Search Console data")

            try:
                sitemaps = svc.sitemaps().list(siteUrl=gsc_site_url).execute().get("sitemap", [])
                checks["sitemaps_count"] = len(sitemaps)
                checks["sitemaps"] = [{"path": s["path"], "errors": s.get("errors", 0)} for s in sitemaps]
                if sitemaps:
                    score += 10
                else:
                    out["issues"].append("GSC: No sitemap submitted")
                    out["recommendations"].append("GSC: Submit your XML sitemap to improve crawling and indexation")
            except Exception:
                checks["sitemaps_count"] = 0

        except Exception as e:
            checks["error"] = str(e)
        out["platforms"]["gsc"] = checks

    out["score"] = score
    out["max_score"] = max_score
    if max_score > 0:
        out["health_pct"] = round(score / max_score * 100)
    return out


# ═════════════════════════════════════════════════════════════════════════════
# 7. LANDING PAGE QUALITY REPORT
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def report_landing_page_quality(
    ga4_property_id: str = "",
    google_ads_customer_id: str = "",
    days: int = 30,
) -> dict:
    """
    Landing page quality report combining GA4 engagement metrics and Google Ads keyword quality scores.
    Identifies your worst-performing landing pages so you know where to focus CRO efforts.

    Args:
        ga4_property_id: GA4 property ID.
        google_ads_customer_id: Google Ads customer ID. Optional — adds keyword quality scores.
        days: Number of days to report on. Default 30.
    """
    start, end = _date_window(days)
    out: dict[str, Any] = {
        "period": f"{start} to {end}",
        "landing_pages": [],
        "quality_scores": [],
        "poor_performers": [],
    }

    # ── GA4 landing pages ─────────────────────────────────────────────────────
    if ga4_property_id:
        try:
            resp = _ga4_report(ga4_property_id, {
                "dimensions": [{"name": "landingPagePlusQueryString"}],
                "metrics": [
                    {"name": "sessions"},
                    {"name": "bounceRate"},
                    {"name": "conversions"},
                    {"name": "averageSessionDuration"},
                    {"name": "engagementRate"},
                ],
                "dateRanges": [{"startDate": start, "endDate": end}],
                "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}],
                "limit": 30,
            })
            pages = []
            for r in resp.get("rows", []):
                page = r["dimensionValues"][0]["value"]
                mv = r["metricValues"]
                sessions = int(mv[0]["value"])
                bounce = round(float(mv[1]["value"]) * 100, 1)
                convs = int(mv[2]["value"])
                conv_rate = round(convs / sessions * 100, 2) if sessions > 0 else 0
                pages.append({
                    "page": page,
                    "sessions": sessions,
                    "bounce_rate": bounce,
                    "conversions": convs,
                    "conversion_rate": conv_rate,
                    "avg_session_sec": round(float(mv[3]["value"]), 0),
                    "engagement_rate": round(float(mv[4]["value"]) * 100, 1),
                })
                if sessions >= 50 and bounce > 70 and conv_rate < 1:
                    out["poor_performers"].append({
                        "page": page,
                        "sessions": sessions,
                        "bounce_rate": bounce,
                        "conversion_rate": conv_rate,
                        "issue": "High traffic + high bounce + low CVR",
                    })
            out["landing_pages"] = pages
        except Exception as e:
            out["landing_pages"] = {"error": str(e)}

    # ── Google Ads quality scores ─────────────────────────────────────────────
    if google_ads_customer_id:
        try:
            cid = google_ads_customer_id.replace("-", "")
            client = _gads_client(google_ads_customer_id)
            svc = client.get_service("GoogleAdsService")
            rows = list(svc.search(customer_id=cid, query=f"""
                SELECT ad_group_criterion.keyword.text,
                       ad_group_criterion.quality_info.quality_score,
                       ad_group_criterion.quality_info.post_click_quality_score,
                       ad_group_criterion.quality_info.creative_quality_score,
                       ad_group_criterion.quality_info.search_predicted_ctr,
                       ad_group_criterion.final_urls,
                       metrics.cost_micros, metrics.clicks
                FROM keyword_view
                WHERE segments.date BETWEEN '{start}' AND '{end}'
                  AND ad_group_criterion.quality_info.quality_score > 0
                  AND campaign.status = 'ENABLED'
                  AND ad_group.status = 'ENABLED'
                  AND ad_group_criterion.status = 'ENABLED'
                ORDER BY metrics.cost_micros DESC
                LIMIT 30
            """))
            qs_data = []
            for r in rows:
                qi = r.ad_group_criterion.quality_info
                qs = qi.quality_score
                lp_exp = qi.post_click_quality_score.name if qi.post_click_quality_score else "UNKNOWN"
                urls = list(r.ad_group_criterion.final_urls)
                qs_data.append({
                    "keyword": r.ad_group_criterion.keyword.text,
                    "quality_score": qs,
                    "landing_page_experience": lp_exp,
                    "creative_quality": qi.creative_quality_score.name if qi.creative_quality_score else "UNKNOWN",
                    "expected_ctr": qi.search_predicted_ctr.name if qi.search_predicted_ctr else "UNKNOWN",
                    "final_url": urls[0] if urls else "",
                    "spend": round(r.metrics.cost_micros / 1_000_000, 2),
                })
                if qs and qs <= 4 and "BELOW" in lp_exp:
                    out["poor_performers"].append({
                        "keyword": r.ad_group_criterion.keyword.text,
                        "quality_score": qs,
                        "landing_page_experience": lp_exp,
                        "url": urls[0] if urls else "",
                        "issue": f"QS {qs}/10 with below-average landing page experience",
                    })
            out["quality_scores"] = qs_data
        except Exception as e:
            out["quality_scores"] = {"error": str(e)}

    out["poor_performers"].sort(key=lambda x: x.get("sessions", 0), reverse=True)
    return out


# ═════════════════════════════════════════════════════════════════════════════
# 8. CAMPAIGN HEALTH CHECK
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def report_campaign_health_check(
    google_ads_customer_id: str = "",
    meta_ad_account_id: str = "",
    days: int = 14,
) -> dict:
    """
    Campaign health check — flags underperforming and problematic campaigns across Google Ads and Meta.
    Checks for: zero conversions with high spend, below-average CTR, high CPA, audience fatigue, low ROAS.

    Args:
        google_ads_customer_id: Google Ads customer ID. Optional.
        meta_ad_account_id: Meta ad account ID. Optional.
        days: Number of days to evaluate. Default 14.
    """
    start, end = _date_window(days)
    out: dict[str, Any] = {
        "period": f"{start} to {end}",
        "google_ads_issues": [],
        "meta_issues": [],
        "summary": {"total_issues": 0, "critical": 0, "warnings": 0},
    }

    # ── Google Ads ────────────────────────────────────────────────────────────
    if google_ads_customer_id:
        try:
            cid = google_ads_customer_id.replace("-", "")
            client = _gads_client(google_ads_customer_id)
            svc = client.get_service("GoogleAdsService")
            rows = list(svc.search(customer_id=cid, query=f"""
                SELECT campaign.id, campaign.name,
                       metrics.cost_micros, metrics.clicks, metrics.impressions,
                       metrics.conversions, metrics.conversions_value,
                       metrics.ctr, metrics.cost_per_conversion
                FROM campaign
                WHERE segments.date BETWEEN '{start}' AND '{end}'
                  AND campaign.status = 'ENABLED'
                  AND metrics.impressions > 100
                ORDER BY metrics.cost_micros DESC
                LIMIT 50
            """))

            all_ctrs = [r.metrics.ctr for r in rows if r.metrics.impressions > 100]
            avg_ctr = sum(all_ctrs) / len(all_ctrs) if all_ctrs else 0

            for r in rows:
                me = r.metrics
                spend = me.cost_micros / 1_000_000
                if spend == 0:
                    continue
                issues = []
                severity = "warning"

                if me.conversions == 0 and spend > 50:
                    issues.append(f"No conversions despite ${spend:.0f} spend")
                    severity = "critical"
                if me.ctr < avg_ctr * 0.5 and me.impressions > 500:
                    issues.append(f"CTR {round(me.ctr*100,2)}% is 50%+ below account avg {round(avg_ctr*100,2)}%")
                if me.clicks == 0 and me.impressions > 1000:
                    issues.append("1000+ impressions but zero clicks — check ad copy or targeting")
                    severity = "critical"
                if me.conversions > 0 and me.cost_per_conversion > 0:
                    cpa = me.cost_per_conversion / 1_000_000
                    if cpa > 500:
                        issues.append(f"Very high CPA: ${cpa:.0f}")

                if issues:
                    out["google_ads_issues"].append({
                        "campaign": r.campaign.name,
                        "campaign_id": str(r.campaign.id),
                        "severity": severity,
                        "issues": issues,
                        "spend": round(spend, 2),
                        "conversions": round(me.conversions, 1),
                        "ctr": round(me.ctr * 100, 2),
                    })
        except Exception as e:
            out["google_ads_issues"] = [{"error": str(e)}]

    # ── Meta ──────────────────────────────────────────────────────────────────
    if meta_ad_account_id:
        try:
            resp = _meta_req(
                f"{meta_ad_account_id}/insights",
                {
                    "fields": "campaign_name,campaign_id,spend,clicks,impressions,ctr,purchase_roas,frequency,actions",
                    "time_range": json.dumps({"since": start, "until": end}),
                    "level": "campaign",
                    "limit": 50,
                },
            )
            for c in resp.get("data", []):
                spend = float(c.get("spend", 0))
                if spend == 0:
                    continue
                freq = float(c.get("frequency", 0))
                ctr = float(c.get("ctr", 0)) * 100
                roas_list = c.get("purchase_roas", [])
                roas = float(roas_list[0].get("value", 0)) if roas_list else 0
                purchases = sum(int(a.get("value", 0)) for a in c.get("actions", []) if a.get("action_type") == "purchase")

                issues = []
                severity = "warning"

                if freq > 5:
                    issues.append(f"Frequency {round(freq,1)}x — audience may be fatigued, refresh creatives")
                    severity = "critical"
                if ctr < 0.5:
                    issues.append(f"Low CTR {round(ctr,2)}% — ad creative may need refreshing")
                if 0 < roas < 1:
                    issues.append(f"ROAS {round(roas,2)}x below breakeven — campaign is losing money")
                    severity = "critical"
                if purchases == 0 and spend > 100:
                    issues.append(f"No purchases despite ${spend:.0f} spend")
                    severity = "critical"

                if issues:
                    out["meta_issues"].append({
                        "campaign": c.get("campaign_name"),
                        "campaign_id": c.get("campaign_id"),
                        "severity": severity,
                        "issues": issues,
                        "spend": round(spend, 2),
                        "roas": round(roas, 2),
                        "frequency": round(freq, 1),
                        "ctr": round(ctr, 2),
                    })
        except Exception as e:
            out["meta_issues"] = [{"error": str(e)}]

    # ── Summary ───────────────────────────────────────────────────────────────
    all_issues = [i for i in out["google_ads_issues"] + out["meta_issues"] if isinstance(i, dict) and "issues" in i]
    out["summary"]["total_issues"] = len(all_issues)
    out["summary"]["critical"] = sum(1 for i in all_issues if i.get("severity") == "critical")
    out["summary"]["warnings"] = sum(1 for i in all_issues if i.get("severity") == "warning")
    out["google_ads_issues"].sort(key=lambda x: 0 if isinstance(x, dict) and x.get("severity") == "critical" else 1)
    out["meta_issues"].sort(key=lambda x: 0 if isinstance(x, dict) and x.get("severity") == "critical" else 1)

    return out
