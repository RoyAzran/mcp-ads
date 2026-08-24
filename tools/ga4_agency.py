"""
GA4 agency tools — curated set of ~55 high-value tools for marketing agencies.

Adds:
  - Date-preset shortcuts (today/yesterday/7d/14d/30d/90d) for traffic, pages, conversions, ecommerce
  - Comparison tools (WoW, MoM, YoY, QoQ)
  - Channel breakdowns (organic, paid, email, social, direct, referral)
  - Per-source breakdowns (Google, Facebook/Instagram, TikTok, etc.)
  - Time-pattern analysis (hour of day, day of week, heatmap)
  - New vs returning, audience engagement, LTV
  - Executive & agency dashboards (KPI, lead-gen, ecom, PPC, SEO)
  - Audit / diagnostic tools
  - Admin tools (create custom dimension, bulk key events, setup wizards)
  - Advanced custom report builder

Credentials reuse tools/ga4.py helpers — no duplication.
"""
import json
from datetime import date, timedelta
from typing import Optional

from mcp_instance import mcp
from auth import current_user_ctx
from permissions import require_editor

# Reuse creds + service builders + report helpers from ga4.py
from tools.ga4 import (
    _creds,
    _data_svc,
    _admin_svc,
    _admin_alpha_svc,
    _resolve_property,
    _run_report,
    _parse_report,
)

# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------

def _dr(sd, ed):
    return [{"startDate": sd, "endDate": ed}]


def _dates(s, e):
    ed = e or str(date.today())
    sd = s or str(date.today() - timedelta(days=28))
    return sd, ed


def _preset_dates(preset: str):
    t = date.today()
    p = (preset or "last_28d").lower()
    if p == "today":
        return str(t), str(t)
    if p in ("yesterday", "yest"):
        y = t - timedelta(days=1)
        return str(y), str(y)
    if p == "last_7d":
        return str(t - timedelta(days=7)), str(t - timedelta(days=1))
    if p == "last_14d":
        return str(t - timedelta(days=14)), str(t - timedelta(days=1))
    if p == "last_28d":
        return str(t - timedelta(days=28)), str(t - timedelta(days=1))
    if p == "last_30d":
        return str(t - timedelta(days=30)), str(t - timedelta(days=1))
    if p == "last_60d":
        return str(t - timedelta(days=60)), str(t - timedelta(days=1))
    if p == "last_90d":
        return str(t - timedelta(days=90)), str(t - timedelta(days=1))
    if p == "last_180d":
        return str(t - timedelta(days=180)), str(t - timedelta(days=1))
    if p == "last_365d":
        return str(t - timedelta(days=365)), str(t - timedelta(days=1))
    if p == "ytd":
        return f"{t.year}-01-01", str(t)
    if p == "this_week":
        s = t - timedelta(days=t.weekday())
        return str(s), str(t)
    if p == "this_month":
        return str(t.replace(day=1)), str(t)
    if p == "last_month":
        e = t.replace(day=1) - timedelta(days=1)
        s = e.replace(day=1)
        return str(s), str(e)
    if p == "this_quarter":
        q = ((t.month - 1) // 3) * 3 + 1
        return str(t.replace(month=q, day=1)), str(t)
    if p == "last_quarter":
        q_start_month = ((t.month - 1) // 3) * 3 + 1
        q_end = t.replace(month=q_start_month, day=1) - timedelta(days=1)
        lq_start = ((q_end.month - 1) // 3) * 3 + 1
        return str(q_end.replace(month=lq_start, day=1)), str(q_end)
    # fallback
    return str(t - timedelta(days=28)), str(t - timedelta(days=1))


def _simple_report(pid, sd, ed, dims, mets, sort_by="", limit=20, filt=None):
    body = {
        "dateRanges": _dr(sd, ed),
        "dimensions": [{"name": d} for d in dims],
        "metrics": [{"name": m} for m in mets],
        "limit": limit,
    }
    if sort_by:
        body["orderBys"] = [{"metric": {"metricName": sort_by}, "desc": True}]
    if filt:
        body["dimensionFilter"] = filt
    return _parse_report(_run_report(pid, body))


def _gen_report(pid, sd, ed, dims, mets, sort_by="", limit=20, filt=None):
    body = {
        "dateRanges": [{"startDate": sd, "endDate": ed}],
        "dimensions": [{"name": d} for d in dims],
        "metrics": [{"name": m} for m in mets],
        "limit": limit,
    }
    if sort_by:
        body["orderBys"] = [{"metric": {"metricName": sort_by}, "desc": True}]
    if filt:
        body["dimensionFilter"] = filt
    return _parse_report(_run_report(pid, body))


def _compare_two(pid, dims, mets, p1_sd, p1_ed, p2_sd, p2_ed, sort_by="", limit=20):
    body = {
        "dateRanges": [
            {"startDate": p1_sd, "endDate": p1_ed, "name": "current"},
            {"startDate": p2_sd, "endDate": p2_ed, "name": "previous"},
        ],
        "dimensions": [{"name": d} for d in dims],
        "metrics": [{"name": m} for m in mets],
        "limit": limit,
    }
    if sort_by:
        body["orderBys"] = [{"metric": {"metricName": sort_by}, "desc": True}]
    return _parse_report(_run_report(pid, body))


def _str_filt(field, value, match="EXACT", case_sensitive=False):
    return {"filter": {"fieldName": field,
                       "stringFilter": {"matchType": match, "value": value, "caseSensitive": case_sensitive}}}


# ===========================================================================
# DATE-PRESET TRAFFIC REPORTS
# ===========================================================================

@mcp.tool()
def get_ga_traffic_today(property_id: str = "") -> str:
    """GA4 traffic sources — today."""
    sd, ed = _preset_dates("today")
    return json.dumps({"date_range": f"{sd} to {ed}", "channels": _gen_report(property_id, sd, ed, ["sessionDefaultChannelGroup"], ["sessions", "activeUsers", "conversions"], "sessions", 15)})


@mcp.tool()
def get_ga_traffic_yesterday(property_id: str = "") -> str:
    """GA4 traffic sources — yesterday."""
    sd, ed = _preset_dates("yesterday")
    return json.dumps({"date_range": f"{sd} to {ed}", "channels": _gen_report(property_id, sd, ed, ["sessionDefaultChannelGroup"], ["sessions", "activeUsers", "conversions"], "sessions", 15)})


@mcp.tool()
def get_ga_traffic_7d(property_id: str = "") -> str:
    """GA4 traffic sources — last 7 days."""
    sd, ed = _preset_dates("last_7d")
    return json.dumps({"date_range": f"{sd} to {ed}", "channels": _gen_report(property_id, sd, ed, ["sessionDefaultChannelGroup"], ["sessions", "activeUsers", "conversions", "totalRevenue"], "sessions", 15)})


@mcp.tool()
def get_ga_traffic_30d(property_id: str = "") -> str:
    """GA4 traffic sources — last 30 days."""
    sd, ed = _preset_dates("last_30d")
    return json.dumps({"date_range": f"{sd} to {ed}", "channels": _gen_report(property_id, sd, ed, ["sessionDefaultChannelGroup"], ["sessions", "activeUsers", "conversions", "totalRevenue"], "sessions", 15)})


@mcp.tool()
def get_ga_traffic_90d(property_id: str = "") -> str:
    """GA4 traffic sources — last 90 days."""
    sd, ed = _preset_dates("last_90d")
    return json.dumps({"date_range": f"{sd} to {ed}", "channels": _gen_report(property_id, sd, ed, ["sessionDefaultChannelGroup"], ["sessions", "activeUsers", "conversions", "totalRevenue"], "sessions", 15)})


@mcp.tool()
def get_ga_traffic_this_month(property_id: str = "") -> str:
    """GA4 traffic sources — current month to date."""
    sd, ed = _preset_dates("this_month")
    return json.dumps({"date_range": f"{sd} to {ed}", "channels": _gen_report(property_id, sd, ed, ["sessionDefaultChannelGroup"], ["sessions", "activeUsers", "conversions", "totalRevenue"], "sessions", 15)})


@mcp.tool()
def get_ga_traffic_last_month(property_id: str = "") -> str:
    """GA4 traffic sources — last complete month."""
    sd, ed = _preset_dates("last_month")
    return json.dumps({"date_range": f"{sd} to {ed}", "channels": _gen_report(property_id, sd, ed, ["sessionDefaultChannelGroup"], ["sessions", "activeUsers", "conversions", "totalRevenue"], "sessions", 15)})


@mcp.tool()
def get_ga_traffic_ytd(property_id: str = "") -> str:
    """GA4 traffic sources — year to date."""
    sd, ed = _preset_dates("ytd")
    return json.dumps({"date_range": f"{sd} to {ed}", "channels": _gen_report(property_id, sd, ed, ["sessionDefaultChannelGroup"], ["sessions", "activeUsers", "conversions", "totalRevenue"], "sessions", 15)})


# ===========================================================================
# DATE-PRESET TOP PAGES
# ===========================================================================

@mcp.tool()
def get_ga_top_pages_today(property_id: str = "", row_limit: int = 20) -> str:
    """GA4 top pages — today."""
    sd, ed = _preset_dates("today")
    return json.dumps({"date_range": f"{sd} to {ed}", "pages": _gen_report(property_id, sd, ed, ["pagePath", "pageTitle"], ["screenPageViews", "activeUsers", "bounceRate"], "screenPageViews", row_limit)})


@mcp.tool()
def get_ga_top_pages_yesterday(property_id: str = "", row_limit: int = 20) -> str:
    """GA4 top pages — yesterday."""
    sd, ed = _preset_dates("yesterday")
    return json.dumps({"date_range": f"{sd} to {ed}", "pages": _gen_report(property_id, sd, ed, ["pagePath", "pageTitle"], ["screenPageViews", "activeUsers", "bounceRate"], "screenPageViews", row_limit)})


@mcp.tool()
def get_ga_top_pages_7d(property_id: str = "", row_limit: int = 25) -> str:
    """GA4 top pages — last 7 days."""
    sd, ed = _preset_dates("last_7d")
    return json.dumps({"date_range": f"{sd} to {ed}", "pages": _gen_report(property_id, sd, ed, ["pagePath", "pageTitle"], ["screenPageViews", "activeUsers", "bounceRate", "conversions"], "screenPageViews", row_limit)})


@mcp.tool()
def get_ga_top_pages_30d(property_id: str = "", row_limit: int = 25) -> str:
    """GA4 top pages — last 30 days."""
    sd, ed = _preset_dates("last_30d")
    return json.dumps({"date_range": f"{sd} to {ed}", "pages": _gen_report(property_id, sd, ed, ["pagePath", "pageTitle"], ["screenPageViews", "activeUsers", "bounceRate", "conversions"], "screenPageViews", row_limit)})


@mcp.tool()
def get_ga_top_pages_90d(property_id: str = "", row_limit: int = 25) -> str:
    """GA4 top pages — last 90 days."""
    sd, ed = _preset_dates("last_90d")
    return json.dumps({"date_range": f"{sd} to {ed}", "pages": _gen_report(property_id, sd, ed, ["pagePath", "pageTitle"], ["screenPageViews", "activeUsers", "bounceRate", "conversions"], "screenPageViews", row_limit)})


# ===========================================================================
# DATE-PRESET CONVERSIONS
# ===========================================================================

@mcp.tool()
def get_ga_conversions_today(property_id: str = "") -> str:
    """GA4 conversions — today."""
    sd, ed = _preset_dates("today")
    return json.dumps({"date_range": f"{sd} to {ed}", "conversions": _gen_report(property_id, sd, ed, ["eventName"], ["eventCount", "totalUsers"], "eventCount", 20, {"filter": {"fieldName": "eventName", "inListFilter": {"values": ["purchase", "generate_lead", "form_submit", "sign_up", "contact", "phone_click"]}}})})


@mcp.tool()
def get_ga_conversions_yesterday(property_id: str = "") -> str:
    """GA4 conversions — yesterday."""
    sd, ed = _preset_dates("yesterday")
    return json.dumps({"date_range": f"{sd} to {ed}", "conversions": _gen_report(property_id, sd, ed, ["eventName", "sessionDefaultChannelGroup"], ["eventCount", "totalUsers"], "eventCount", 25)})


@mcp.tool()
def get_ga_conversions_7d(property_id: str = "") -> str:
    """GA4 conversions by event — last 7 days."""
    sd, ed = _preset_dates("last_7d")
    return json.dumps({"date_range": f"{sd} to {ed}", "conversions": _gen_report(property_id, sd, ed, ["eventName", "sessionDefaultChannelGroup"], ["eventCount", "totalUsers"], "eventCount", 30)})


@mcp.tool()
def get_ga_conversions_30d(property_id: str = "") -> str:
    """GA4 conversions by event — last 30 days."""
    sd, ed = _preset_dates("last_30d")
    return json.dumps({"date_range": f"{sd} to {ed}", "conversions": _gen_report(property_id, sd, ed, ["eventName", "sessionDefaultChannelGroup"], ["eventCount", "totalUsers"], "eventCount", 30)})


@mcp.tool()
def get_ga_conversions_this_month(property_id: str = "") -> str:
    """GA4 conversions — current month to date."""
    sd, ed = _preset_dates("this_month")
    return json.dumps({"date_range": f"{sd} to {ed}", "conversions": _gen_report(property_id, sd, ed, ["eventName", "sessionDefaultChannelGroup"], ["eventCount", "totalUsers"], "eventCount", 30)})


# ===========================================================================
# DATE-PRESET ECOMMERCE
# ===========================================================================

@mcp.tool()
def get_ga_ecommerce_today(property_id: str = "") -> str:
    """GA4 ecommerce summary — today."""
    sd, ed = _preset_dates("today")
    return json.dumps({"date_range": f"{sd} to {ed}", "summary": _gen_report(property_id, sd, ed, [], ["totalRevenue", "transactions", "averagePurchaseRevenue", "ecommercePurchases"], "", 1), "top_items": _gen_report(property_id, sd, ed, ["itemName"], ["itemRevenue", "itemsPurchased"], "itemRevenue", 10)})


@mcp.tool()
def get_ga_ecommerce_7d(property_id: str = "") -> str:
    """GA4 ecommerce — last 7 days."""
    sd, ed = _preset_dates("last_7d")
    return json.dumps({"date_range": f"{sd} to {ed}", "summary": _gen_report(property_id, sd, ed, [], ["totalRevenue", "transactions", "averagePurchaseRevenue"], "", 1), "by_channel": _gen_report(property_id, sd, ed, ["sessionDefaultChannelGroup"], ["totalRevenue", "transactions"], "totalRevenue", 10), "top_items": _gen_report(property_id, sd, ed, ["itemName"], ["itemRevenue", "itemsPurchased"], "itemRevenue", 15)})


@mcp.tool()
def get_ga_ecommerce_30d(property_id: str = "") -> str:
    """GA4 ecommerce — last 30 days."""
    sd, ed = _preset_dates("last_30d")
    return json.dumps({"date_range": f"{sd} to {ed}", "summary": _gen_report(property_id, sd, ed, [], ["totalRevenue", "transactions", "averagePurchaseRevenue", "purchaseRevenue"], "", 1), "by_channel": _gen_report(property_id, sd, ed, ["sessionDefaultChannelGroup"], ["totalRevenue", "transactions", "averagePurchaseRevenue"], "totalRevenue", 10), "top_items": _gen_report(property_id, sd, ed, ["itemName"], ["itemRevenue", "itemsPurchased"], "itemRevenue", 20), "top_categories": _gen_report(property_id, sd, ed, ["itemCategory"], ["itemRevenue", "itemsPurchased"], "itemRevenue", 10)})


@mcp.tool()
def get_ga_ecommerce_this_month(property_id: str = "") -> str:
    """GA4 ecommerce — current month to date."""
    sd, ed = _preset_dates("this_month")
    return json.dumps({"date_range": f"{sd} to {ed}", "summary": _gen_report(property_id, sd, ed, [], ["totalRevenue", "transactions", "averagePurchaseRevenue"], "", 1), "by_channel": _gen_report(property_id, sd, ed, ["sessionDefaultChannelGroup"], ["totalRevenue", "transactions"], "totalRevenue", 10), "top_items": _gen_report(property_id, sd, ed, ["itemName"], ["itemRevenue", "itemsPurchased"], "itemRevenue", 15)})


# ===========================================================================
# COMPARISON TOOLS (WoW / MoM / YoY / QoQ)
# ===========================================================================

@mcp.tool()
def get_ga_compare_wow(property_id: str = "") -> str:
    """Week-over-week comparison — this week vs previous 7 days."""
    t = date.today()
    p1s, p1e = str(t - timedelta(days=7)), str(t - timedelta(days=1))
    p2s, p2e = str(t - timedelta(days=14)), str(t - timedelta(days=8))
    try:
        return json.dumps({"current_period": f"{p1s} to {p1e}", "previous_period": f"{p2s} to {p2e}", "channels": _compare_two(property_id, ["sessionDefaultChannelGroup"], ["sessions", "activeUsers", "conversions", "totalRevenue"], p1s, p1e, p2s, p2e, "sessions")})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_ga_compare_mom(property_id: str = "") -> str:
    """Month-over-month comparison — last 30 days vs prior 30 days."""
    t = date.today()
    p1s, p1e = str(t - timedelta(days=30)), str(t - timedelta(days=1))
    p2s, p2e = str(t - timedelta(days=60)), str(t - timedelta(days=31))
    try:
        return json.dumps({"current_period": f"{p1s} to {p1e}", "previous_period": f"{p2s} to {p2e}", "channels": _compare_two(property_id, ["sessionDefaultChannelGroup"], ["sessions", "activeUsers", "conversions", "totalRevenue"], p1s, p1e, p2s, p2e, "sessions")})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_ga_compare_yoy(property_id: str = "") -> str:
    """Year-over-year comparison — last 30 days vs same 30 days one year ago."""
    t = date.today()
    p1s, p1e = str(t - timedelta(days=30)), str(t - timedelta(days=1))
    p2s = str(t - timedelta(days=30 + 365))
    p2e = str(t - timedelta(days=365))
    try:
        return json.dumps({"current_period": f"{p1s} to {p1e}", "prior_year_period": f"{p2s} to {p2e}", "channels": _compare_two(property_id, ["sessionDefaultChannelGroup"], ["sessions", "activeUsers", "conversions", "totalRevenue"], p1s, p1e, p2s, p2e, "sessions")})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_ga_compare_qoq(property_id: str = "") -> str:
    """Quarter-over-quarter comparison — last 90 days vs prior 90 days."""
    t = date.today()
    p1s, p1e = str(t - timedelta(days=90)), str(t - timedelta(days=1))
    p2s, p2e = str(t - timedelta(days=180)), str(t - timedelta(days=91))
    try:
        return json.dumps({"current_period": f"{p1s} to {p1e}", "previous_period": f"{p2s} to {p2e}", "channels": _compare_two(property_id, ["sessionDefaultChannelGroup"], ["sessions", "activeUsers", "conversions", "totalRevenue"], p1s, p1e, p2s, p2e, "sessions")})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ===========================================================================
# CHANNEL BREAKDOWN TOOLS
# ===========================================================================

@mcp.tool()
def get_ga_organic_search(property_id: str = "", start_date: str = "", end_date: str = "", row_limit: int = 20) -> str:
    """GA4 organic search traffic — sources, keywords, landing pages."""
    sd, ed = _dates(start_date, end_date)
    filt = _str_filt("sessionMedium", "organic")
    try:
        return json.dumps({"date_range": f"{sd} to {ed}", "sources": _simple_report(property_id, sd, ed, ["sessionSource"], ["sessions", "engagedSessions", "conversions"], "sessions", row_limit, filt), "landing_pages": _simple_report(property_id, sd, ed, ["landingPage"], ["sessions", "conversions"], "sessions", row_limit, filt)})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_ga_paid_search(property_id: str = "", start_date: str = "", end_date: str = "", row_limit: int = 20) -> str:
    """GA4 paid search traffic (cpc/ppc) — campaigns, sources, landing pages."""
    sd, ed = _dates(start_date, end_date)
    filt = {"orGroup": {"expressions": [_str_filt("sessionMedium", "cpc")["filter"], _str_filt("sessionMedium", "ppc")["filter"]]}}
    try:
        return json.dumps({"date_range": f"{sd} to {ed}", "by_campaign": _simple_report(property_id, sd, ed, ["sessionCampaignName", "sessionSource"], ["sessions", "conversions", "totalRevenue"], "sessions", row_limit, filt), "by_landing_page": _simple_report(property_id, sd, ed, ["landingPage"], ["sessions", "conversions"], "sessions", row_limit, filt)})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_ga_organic_social(property_id: str = "", start_date: str = "", end_date: str = "", row_limit: int = 20) -> str:
    """GA4 organic social traffic."""
    sd, ed = _dates(start_date, end_date)
    filt = _str_filt("sessionDefaultChannelGroup", "Organic Social")
    try:
        return json.dumps({"date_range": f"{sd} to {ed}", "sources": _simple_report(property_id, sd, ed, ["sessionSource", "sessionMedium"], ["sessions", "activeUsers", "conversions"], "sessions", row_limit, filt)})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_ga_paid_social(property_id: str = "", start_date: str = "", end_date: str = "", row_limit: int = 20) -> str:
    """GA4 paid social traffic."""
    sd, ed = _dates(start_date, end_date)
    filt = _str_filt("sessionDefaultChannelGroup", "Paid Social")
    try:
        return json.dumps({"date_range": f"{sd} to {ed}", "sources": _simple_report(property_id, sd, ed, ["sessionSource", "sessionCampaignName"], ["sessions", "conversions", "totalRevenue"], "sessions", row_limit, filt)})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_ga_email_channel(property_id: str = "", start_date: str = "", end_date: str = "", row_limit: int = 20) -> str:
    """GA4 email channel traffic."""
    sd, ed = _dates(start_date, end_date)
    filt = _str_filt("sessionMedium", "email")
    try:
        return json.dumps({"date_range": f"{sd} to {ed}", "campaigns": _simple_report(property_id, sd, ed, ["sessionCampaignName", "sessionSource"], ["sessions", "conversions", "totalRevenue"], "sessions", row_limit, filt)})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_ga_direct_channel(property_id: str = "", start_date: str = "", end_date: str = "") -> str:
    """GA4 direct traffic."""
    sd, ed = _dates(start_date, end_date)
    filt = _str_filt("sessionDefaultChannelGroup", "Direct")
    try:
        return json.dumps({"date_range": f"{sd} to {ed}", "pages": _simple_report(property_id, sd, ed, ["landingPage"], ["sessions", "activeUsers", "conversions"], "sessions", 20, filt)})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_ga_referral_channel(property_id: str = "", start_date: str = "", end_date: str = "", row_limit: int = 25) -> str:
    """GA4 referral traffic — top referring sites."""
    sd, ed = _dates(start_date, end_date)
    filt = _str_filt("sessionDefaultChannelGroup", "Referral")
    try:
        return json.dumps({"date_range": f"{sd} to {ed}", "referrers": _simple_report(property_id, sd, ed, ["sessionSource"], ["sessions", "activeUsers", "conversions"], "sessions", row_limit, filt)})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ===========================================================================
# PER-SOURCE BREAKDOWNS
# ===========================================================================

@mcp.tool()
def get_ga_google_traffic(property_id: str = "", start_date: str = "", end_date: str = "") -> str:
    """GA4 traffic from Google (all channels)."""
    sd, ed = _dates(start_date, end_date)
    filt = _str_filt("sessionSource", "google")
    try:
        return json.dumps({"date_range": f"{sd} to {ed}", "by_medium": _simple_report(property_id, sd, ed, ["sessionMedium", "sessionCampaignName"], ["sessions", "activeUsers", "conversions", "totalRevenue"], "sessions", 20, filt)})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_ga_facebook_traffic(property_id: str = "", start_date: str = "", end_date: str = "") -> str:
    """GA4 traffic from Facebook / Meta."""
    sd, ed = _dates(start_date, end_date)
    filt = {"orGroup": {"expressions": [_str_filt("sessionSource", "facebook")["filter"], _str_filt("sessionSource", "instagram")["filter"]]}}
    try:
        return json.dumps({"date_range": f"{sd} to {ed}", "by_medium": _simple_report(property_id, sd, ed, ["sessionSource", "sessionMedium", "sessionCampaignName"], ["sessions", "conversions", "totalRevenue"], "sessions", 25, filt)})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_ga_tiktok_traffic(property_id: str = "", start_date: str = "", end_date: str = "") -> str:
    """GA4 traffic from TikTok."""
    sd, ed = _dates(start_date, end_date)
    filt = _str_filt("sessionSource", "tiktok", match="CONTAINS")
    try:
        return json.dumps({"date_range": f"{sd} to {ed}", "by_campaign": _simple_report(property_id, sd, ed, ["sessionCampaignName", "sessionMedium"], ["sessions", "conversions", "totalRevenue"], "sessions", 20, filt)})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ===========================================================================
# TIME PATTERN ANALYSIS
# ===========================================================================

@mcp.tool()
def get_ga_hour_of_day(property_id: str = "", start_date: str = "", end_date: str = "") -> str:
    """GA4 traffic by hour of day — useful for scheduling ads and content."""
    sd = start_date or _preset_dates("last_30d")[0]
    ed = end_date or _preset_dates("last_30d")[1]
    try:
        return json.dumps({"date_range": f"{sd} to {ed}", "hours": _gen_report(property_id, sd, ed, ["hour"], ["sessions", "conversions", "totalRevenue"], "sessions", 24)})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_ga_day_of_week(property_id: str = "", start_date: str = "", end_date: str = "") -> str:
    """GA4 traffic by day of week — useful for scheduling posts and campaigns."""
    sd = start_date or _preset_dates("last_30d")[0]
    ed = end_date or _preset_dates("last_30d")[1]
    try:
        return json.dumps({"date_range": f"{sd} to {ed}", "weekdays": _gen_report(property_id, sd, ed, ["dayOfWeekName"], ["sessions", "conversions", "totalRevenue"], "sessions", 7)})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_ga_day_hour_heatmap(property_id: str = "", start_date: str = "", end_date: str = "") -> str:
    """GA4 day × hour heatmap — when are users most active? Grid data for all 7 days × 24 hours."""
    sd = start_date or _preset_dates("last_30d")[0]
    ed = end_date or _preset_dates("last_30d")[1]
    try:
        return json.dumps({"date_range": f"{sd} to {ed}", "heatmap": _gen_report(property_id, sd, ed, ["dayOfWeek", "hour"], ["sessions"], "", 200)})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ===========================================================================
# USER BEHAVIOR TOOLS
# ===========================================================================

@mcp.tool()
def get_ga_new_vs_returning(property_id: str = "", start_date: str = "", end_date: str = "") -> str:
    """GA4 new vs returning users — sessions, conversions, and revenue split."""
    sd, ed = _dates(start_date, end_date)
    try:
        return json.dumps({"date_range": f"{sd} to {ed}", "users": _simple_report(property_id, sd, ed, ["newVsReturning"], ["activeUsers", "sessions", "conversions", "totalRevenue", "engagementRate"], "activeUsers", 5)})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_ga_user_lifetime_value(property_id: str = "", start_date: str = "", end_date: str = "") -> str:
    """GA4 user lifetime value summary — average revenue per user."""
    sd, ed = _dates(start_date, end_date)
    try:
        return json.dumps({"date_range": f"{sd} to {ed}", "ltv": _simple_report(property_id, sd, ed, [], ["totalUsers", "averageRevenuePerUser", "totalRevenue", "purchaseRevenue"], "", 1)})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_ga_audience_engagement(property_id: str = "", start_date: str = "", end_date: str = "") -> str:
    """GA4 engagement metrics by audience segment."""
    sd, ed = _dates(start_date, end_date)
    try:
        return json.dumps({"date_range": f"{sd} to {ed}", "audiences": _simple_report(property_id, sd, ed, ["audienceName"], ["activeUsers", "sessions", "conversions", "totalRevenue"], "activeUsers", 30)})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ===========================================================================
# EXECUTIVE & AGENCY DASHBOARDS
# ===========================================================================

@mcp.tool()
def get_ga_executive_dashboard(property_id: str = "", days: int = 30) -> str:
    """Executive summary dashboard — sessions, conversions, revenue, top channels, and daily trend."""
    t = date.today()
    sd, ed = str(t - timedelta(days=days)), str(t - timedelta(days=1))
    try:
        return json.dumps({
            "period": f"{sd} to {ed}",
            "summary": _simple_report(property_id, sd, ed, [], ["sessions", "activeUsers", "newUsers", "conversions", "totalRevenue", "engagementRate"], "", 1),
            "top_channels": _simple_report(property_id, sd, ed, ["sessionDefaultChannelGroup"], ["sessions", "conversions", "totalRevenue"], "sessions", 8),
            "daily_trend": _gen_report(property_id, sd, ed, ["date"], ["sessions", "conversions", "totalRevenue"], "date", days),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_ga_marketing_kpi_dashboard(property_id: str = "") -> str:
    """Full marketing KPI dashboard — acquisition, engagement, monetization, retention."""
    sd, ed = _preset_dates("last_30d")
    try:
        return json.dumps({
            "date_range": f"{sd} to {ed}",
            "acquisition": _gen_report(property_id, sd, ed, ["firstUserDefaultChannelGroup"], ["newUsers", "conversions"], "newUsers", 10),
            "engagement": _gen_report(property_id, sd, ed, ["sessionDefaultChannelGroup"], ["engagementRate", "engagedSessions", "averageSessionDuration"], "engagedSessions", 10),
            "monetization": _gen_report(property_id, sd, ed, ["sessionDefaultChannelGroup"], ["totalRevenue", "transactions", "averagePurchaseRevenue"], "totalRevenue", 10),
            "retention": _gen_report(property_id, sd, ed, ["newVsReturning"], ["activeUsers", "sessions", "conversions"], "activeUsers", 5),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_ga_lead_gen_dashboard(property_id: str = "") -> str:
    """Lead generation dashboard — lead events, sources, and top converting pages."""
    sd, ed = _preset_dates("last_30d")
    lead_filt = {"filter": {"fieldName": "eventName", "inListFilter": {"values": ["generate_lead", "form_submit", "sign_up", "contact", "phone_click"]}}}
    try:
        return json.dumps({
            "date_range": f"{sd} to {ed}",
            "lead_events": _gen_report(property_id, sd, ed, ["eventName"], ["eventCount", "totalUsers"], "eventCount", 20, lead_filt),
            "lead_sources": _gen_report(property_id, sd, ed, ["sessionSourceMedium"], ["conversions", "sessions"], "conversions", 25),
            "converting_pages": _gen_report(property_id, sd, ed, ["landingPage"], ["sessions", "conversions"], "conversions", 25),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_ga_ecommerce_dashboard(property_id: str = "") -> str:
    """Ecommerce dashboard — revenue by channel, top products, top categories, top countries."""
    sd, ed = _preset_dates("last_30d")
    try:
        return json.dumps({
            "date_range": f"{sd} to {ed}",
            "revenue_by_channel": _gen_report(property_id, sd, ed, ["sessionDefaultChannelGroup"], ["totalRevenue", "transactions", "averagePurchaseRevenue"], "totalRevenue", 15),
            "top_products": _gen_report(property_id, sd, ed, ["itemName"], ["itemRevenue", "itemsPurchased"], "itemRevenue", 20),
            "top_categories": _gen_report(property_id, sd, ed, ["itemCategory"], ["itemRevenue", "itemsPurchased"], "itemRevenue", 10),
            "top_countries": _gen_report(property_id, sd, ed, ["country"], ["totalRevenue", "transactions"], "totalRevenue", 10),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_ga_ppc_dashboard(property_id: str = "") -> str:
    """PPC performance dashboard — paid search campaigns, keywords, and landing pages."""
    sd, ed = _preset_dates("last_30d")
    filt = _str_filt("sessionDefaultChannelGroup", "Paid Search")
    try:
        return json.dumps({
            "date_range": f"{sd} to {ed}",
            "by_campaign": _gen_report(property_id, sd, ed, ["sessionCampaignName"], ["sessions", "conversions", "totalRevenue"], "sessions", 25, filt),
            "by_keyword": _gen_report(property_id, sd, ed, ["sessionManualTerm"], ["sessions", "conversions"], "sessions", 25, filt),
            "by_landing_page": _gen_report(property_id, sd, ed, ["landingPage"], ["sessions", "conversions"], "conversions", 20, filt),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_ga_seo_dashboard(property_id: str = "") -> str:
    """SEO dashboard — organic search traffic, landing pages, and engagement."""
    sd, ed = _preset_dates("last_30d")
    filt = _str_filt("sessionMedium", "organic")
    try:
        return json.dumps({
            "date_range": f"{sd} to {ed}",
            "top_landing_pages": _gen_report(property_id, sd, ed, ["landingPage"], ["sessions", "activeUsers", "bounceRate", "conversions"], "sessions", 25, filt),
            "by_source": _gen_report(property_id, sd, ed, ["sessionSource"], ["sessions", "activeUsers", "conversions"], "sessions", 15, filt),
            "devices": _gen_report(property_id, sd, ed, ["deviceCategory"], ["sessions", "bounceRate", "conversions"], "sessions", 5, filt),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_ga_social_dashboard(property_id: str = "") -> str:
    """Social media dashboard — organic and paid social traffic."""
    sd, ed = _preset_dates("last_30d")
    social_filt = {"orGroup": {"expressions": [_str_filt("sessionDefaultChannelGroup", "Organic Social")["filter"], _str_filt("sessionDefaultChannelGroup", "Paid Social")["filter"]]}}
    try:
        return json.dumps({
            "date_range": f"{sd} to {ed}",
            "by_source": _gen_report(property_id, sd, ed, ["sessionSource", "sessionDefaultChannelGroup"], ["sessions", "conversions", "totalRevenue"], "sessions", 20, social_filt),
            "by_campaign": _gen_report(property_id, sd, ed, ["sessionCampaignName"], ["sessions", "conversions"], "sessions", 20, social_filt),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_ga_full_360_report(property_id: str = "", days: int = 30) -> str:
    """Comprehensive 360 site report — all major dimensions in one call."""
    t = date.today()
    sd, ed = str(t - timedelta(days=days)), str(t - timedelta(days=1))
    try:
        return json.dumps({
            "date_range": f"{sd} to {ed}",
            "summary": _simple_report(property_id, sd, ed, [], ["sessions", "activeUsers", "newUsers", "conversions", "totalRevenue", "engagementRate", "averageSessionDuration"], "", 1),
            "channels": _simple_report(property_id, sd, ed, ["sessionDefaultChannelGroup"], ["sessions", "conversions", "totalRevenue"], "sessions", 10),
            "top_pages": _simple_report(property_id, sd, ed, ["pagePath"], ["screenPageViews", "activeUsers", "bounceRate"], "screenPageViews", 10),
            "devices": _simple_report(property_id, sd, ed, ["deviceCategory"], ["sessions", "conversions"], "sessions", 5),
            "top_countries": _simple_report(property_id, sd, ed, ["country"], ["sessions", "conversions"], "sessions", 10),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


# ===========================================================================
# AUDIT & DIAGNOSTIC TOOLS
# ===========================================================================

@mcp.tool()
def ga4_check_data_quality(property_id: str = "", days: int = 7) -> str:
    """Check for common GA4 data quality issues — missing events, no conversions, no sessions."""
    t = date.today()
    sd, ed = str(t - timedelta(days=days)), str(t - timedelta(days=1))
    issues = []
    try:
        ev = _simple_report(property_id, sd, ed, ["eventName"], ["eventCount"], "eventCount", 50)
        ev_names = [r.get("eventName") for r in ev]
        if "page_view" not in ev_names:
            issues.append("CRITICAL: Missing page_view events — GA4 tag may not be firing")
        if "session_start" not in ev_names:
            issues.append("WARNING: Missing session_start events")
        if not any(e in ev_names for e in ["purchase", "generate_lead", "form_submit", "sign_up"]):
            issues.append("WARNING: No conversion-type events detected in last " + str(days) + " days")
        sessions = _simple_report(property_id, sd, ed, [], ["sessions"], "", 1)
        if not sessions or int(sessions[0].get("sessions", 0)) < 1:
            issues.append("CRITICAL: No sessions found — check GA4 installation")
        return json.dumps({
            "date_range": f"{sd} to {ed}",
            "total_events_seen": len(ev_names),
            "events_firing": ev_names[:30],
            "issues": issues if issues else ["No issues detected"],
            "health": "OK" if not issues else ("CRITICAL" if any("CRITICAL" in i for i in issues) else "WARNING"),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def ga4_audit_property_settings(property_id: str = "") -> str:
    """Audit property setup — streams, key events, Google Ads links, retention, custom dims."""
    try:
        prop = _resolve_property(property_id)
        admin = _admin_svc()
        return json.dumps({
            "property": prop,
            "streams": admin.properties().dataStreams().list(parent=prop).execute().get("dataStreams", []),
            "key_events": admin.properties().keyEvents().list(parent=prop).execute().get("keyEvents", []),
            "key_events_count": len(admin.properties().keyEvents().list(parent=prop).execute().get("keyEvents", [])),
            "google_ads_links": admin.properties().googleAdsLinks().list(parent=prop).execute().get("googleAdsLinks", []),
            "custom_dimensions_count": len(admin.properties().customDimensions().list(parent=prop).execute().get("customDimensions", [])),
            "custom_metrics_count": len(admin.properties().customMetrics().list(parent=prop).execute().get("customMetrics", [])),
            "audiences_count": len(_admin_alpha_svc().properties().audiences().list(parent=prop).execute().get("audiences", [])),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def ga4_diagnose_missing_conversions(property_id: str = "", days: int = 7) -> str:
    """Diagnose why conversions may be missing — compares key events marked vs events actually firing."""
    t = date.today()
    sd, ed = str(t - timedelta(days=days)), str(t - timedelta(days=1))
    try:
        prop = _resolve_property(property_id)
        key_events = _admin_svc().properties().keyEvents().list(parent=prop).execute().get("keyEvents", [])
        ev_names_marked = [k.get("eventName") for k in key_events]
        ev_seen = _simple_report(property_id, sd, ed, ["eventName"], ["eventCount"], "eventCount", 100)
        ev_names_seen = [r.get("eventName") for r in ev_seen]
        marked_but_not_firing = [e for e in ev_names_marked if e not in ev_names_seen]
        should_be_marked = [e for e in ev_names_seen if e not in ev_names_marked and e in ["purchase", "generate_lead", "form_submit", "phone_click", "sign_up", "contact"]]
        return json.dumps({
            "date_range": f"{sd} to {ed}",
            "key_events_marked": ev_names_marked,
            "events_firing_in_period": ev_names_seen[:40],
            "marked_but_no_data": marked_but_not_firing,
            "firing_but_not_marked_as_key_events": should_be_marked,
            "recommendation": ("Mark these as key events: " + ", ".join(should_be_marked)) if should_be_marked else "Key event setup looks good",
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def ga4_check_google_ads_integration(property_id: str = "") -> str:
    """Check if Google Ads is linked to this GA4 property (required for imported conversions)."""
    try:
        prop = _resolve_property(property_id)
        links = _admin_svc().properties().googleAdsLinks().list(parent=prop).execute().get("googleAdsLinks", [])
        return json.dumps({
            "linked": len(links) > 0,
            "link_count": len(links),
            "links": links,
            "recommendation": ("Google Ads is linked. Make sure auto-tagging is enabled in Google Ads." if links else "No Google Ads link found. Go to GA4 > Admin > Google Ads Links to connect your account."),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def ga4_export_property_config(property_id: str = "") -> str:
    """Export full property configuration as JSON — useful for backups and onboarding new clients."""
    try:
        prop = _resolve_property(property_id)
        admin = _admin_svc()
        return json.dumps({
            "property": admin.properties().get(name=prop).execute(),
            "streams": admin.properties().dataStreams().list(parent=prop).execute().get("dataStreams", []),
            "key_events": admin.properties().keyEvents().list(parent=prop).execute().get("keyEvents", []),
            "custom_dimensions": admin.properties().customDimensions().list(parent=prop).execute().get("customDimensions", []),
            "custom_metrics": admin.properties().customMetrics().list(parent=prop).execute().get("customMetrics", []),
            "audiences": _admin_alpha_svc().properties().audiences().list(parent=prop).execute().get("audiences", []),
            "google_ads_links": admin.properties().googleAdsLinks().list(parent=prop).execute().get("googleAdsLinks", []),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


# ===========================================================================
# ADMIN TOOLS
# ===========================================================================

@mcp.tool()
def ga4_create_custom_dimension(
    parameter_name: str,
    display_name: str,
    scope: str = "EVENT",
    description: str = "",
    property_id: str = "",
) -> str:
    """Create a new custom dimension in GA4.

    Args:
        parameter_name: The event parameter name to track (e.g. 'product_category').
        display_name: Human-readable name shown in the GA4 UI.
        scope: EVENT | USER | ITEM. Default EVENT.
        description: Optional description.
        property_id: GA4 property ID. Leave blank to use default.
    """
    require_editor()
    try:
        prop = _resolve_property(property_id)
        body = {"parameterName": parameter_name, "displayName": display_name, "scope": scope}
        if description:
            body["description"] = description
        resp = _admin_svc().properties().customDimensions().create(parent=prop, body=body).execute()
        return json.dumps({"success": True, "custom_dimension": resp})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def ga4_bulk_create_key_events(
    event_names_csv: str,
    counting_method: str = "ONCE_PER_EVENT",
    property_id: str = "",
) -> str:
    """Create multiple GA4 key events (conversions) in one call.

    Args:
        event_names_csv: Comma-separated event names to mark as key events (e.g. 'generate_lead,form_submit,phone_click').
        counting_method: ONCE_PER_EVENT | ONCE_PER_SESSION. Default ONCE_PER_EVENT.
        property_id: GA4 property ID. Leave blank to use default.
    """
    require_editor()
    try:
        prop = _resolve_property(property_id)
        admin = _admin_svc()
        results = []
        for ev in [e.strip() for e in event_names_csv.split(",") if e.strip()]:
            try:
                admin.properties().keyEvents().create(
                    parent=prop,
                    body={"eventName": ev, "countingMethod": counting_method},
                ).execute()
                results.append({"event": ev, "status": "created"})
            except Exception as e:
                results.append({
                    "event": ev,
                    "status": "already_exists" if "already" in str(e).lower() else f"error: {e}",
                })
        return json.dumps({"results": results, "created": sum(1 for r in results if r["status"] == "created")})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def ga4_setup_lead_gen_property(
    property_id: str = "",
    google_ads_customer_id: str = "",
) -> str:
    """One-shot lead gen property setup — creates standard key events and a lead audience.

    Sets up: generate_lead, form_submit, phone_click, email_click, sign_up as key events.
    Creates a 'Lead Submitters' audience with 30-day membership.

    Args:
        property_id: GA4 property ID. Leave blank to use default.
        google_ads_customer_id: Optional. If provided, creates a Google Ads link.
    """
    require_editor()
    try:
        prop = _resolve_property(property_id)
        admin = _admin_svc()
        results = {"property": prop}

        # Key events
        events_results = []
        for ev in ["generate_lead", "form_submit", "phone_click", "email_click", "sign_up"]:
            try:
                admin.properties().keyEvents().create(
                    parent=prop,
                    body={"eventName": ev, "countingMethod": "ONCE_PER_EVENT"},
                ).execute()
                events_results.append({"event": ev, "status": "created"})
            except Exception as e:
                events_results.append({"event": ev, "status": "already_exists" if "already" in str(e).lower() else f"error: {e}"})
        results["key_events"] = events_results

        # Lead audience
        try:
            audience_body = {
                "displayName": "Lead Submitters",
                "membershipDurationDays": 30,
                "filterClauses": [{"clauseType": "INCLUDE", "simpleFilter": {"scope": "AUDIENCE_FILTER_SCOPE_ACROSS_ALL_SESSIONS", "filterExpression": {"orGroup": {"filterExpressions": [{"dimensionOrMetricFilter": {"fieldName": "eventName", "stringFilter": {"value": "generate_lead"}}}, {"dimensionOrMetricFilter": {"fieldName": "eventName", "stringFilter": {"value": "form_submit"}}}]}}}}],
            }
            audience_resp = _admin_alpha_svc().properties().audiences().create(parent=prop, body=audience_body).execute()
            results["lead_audience"] = {"status": "created", "name": audience_resp.get("displayName")}
        except Exception as e:
            results["lead_audience"] = {"status": f"error: {e}"}

        return json.dumps({"success": True, "setup": results})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def ga4_setup_ecommerce_property(property_id: str = "") -> str:
    """One-shot ecommerce property setup — creates standard ecommerce key events.

    Sets up: purchase, add_to_cart, begin_checkout, view_item as key events.

    Args:
        property_id: GA4 property ID. Leave blank to use default.
    """
    require_editor()
    try:
        prop = _resolve_property(property_id)
        admin = _admin_svc()
        results = []
        for ev in ["purchase", "add_to_cart", "begin_checkout", "view_item", "view_item_list"]:
            try:
                admin.properties().keyEvents().create(
                    parent=prop,
                    body={"eventName": ev, "countingMethod": "ONCE_PER_EVENT"},
                ).execute()
                results.append({"event": ev, "status": "created"})
            except Exception as e:
                results.append({"event": ev, "status": "already_exists" if "already" in str(e).lower() else f"error: {e}"})
        return json.dumps({"success": True, "key_events": results})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


@mcp.tool()
def ga4_link_google_ads(
    google_ads_customer_id: str,
    property_id: str = "",
) -> str:
    """Link a Google Ads account to a GA4 property for conversion imports and attribution.

    Args:
        google_ads_customer_id: Google Ads customer ID (numbers only, e.g. '1234567890').
        property_id: GA4 property ID. Leave blank to use default.
    """
    require_editor()
    try:
        prop = _resolve_property(property_id)
        cid = google_ads_customer_id.replace("-", "").strip()
        body = {"customerIds": [cid], "canManageClients": False}
        resp = _admin_svc().properties().googleAdsLinks().create(parent=prop, body=body).execute()
        return json.dumps({"success": True, "link": resp})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


# ===========================================================================
# ADVANCED CUSTOM REPORTS
# ===========================================================================

@mcp.tool()
def ga4_compare_two_dates(
    property_id: str = "",
    dimensions: list = None,
    metrics: list = None,
    period1_start: str = "",
    period1_end: str = "",
    period2_start: str = "",
    period2_end: str = "",
    sort_metric: str = "",
    row_limit: int = 20,
) -> str:
    """Compare two custom date ranges side by side for any dimensions and metrics.

    Args:
        property_id: GA4 property ID. Leave blank to use default.
        dimensions: List of GA4 dimension names (e.g. ['sessionDefaultChannelGroup']).
        metrics: List of GA4 metric names (e.g. ['sessions', 'conversions']).
        period1_start: Period 1 start date YYYY-MM-DD.
        period1_end: Period 1 end date YYYY-MM-DD.
        period2_start: Period 2 start date YYYY-MM-DD.
        period2_end: Period 2 end date YYYY-MM-DD.
        sort_metric: Metric to sort by (descending).
        row_limit: Max rows (default 20).
    """
    try:
        t = date.today()
        p1s = period1_start or str(t - timedelta(days=30))
        p1e = period1_end or str(t - timedelta(days=1))
        p2s = period2_start or str(t - timedelta(days=60))
        p2e = period2_end or str(t - timedelta(days=31))
        return json.dumps({
            "period1": f"{p1s} to {p1e}",
            "period2": f"{p2s} to {p2e}",
            "rows": _compare_two(property_id, dimensions or ["sessionDefaultChannelGroup"], metrics or ["sessions", "conversions"], p1s, p1e, p2s, p2e, sort_metric, row_limit),
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def ga4_quick_metric(
    metric: str,
    property_id: str = "",
    days: int = 30,
) -> str:
    """Get a single metric value quickly — e.g. 'sessions', 'conversions', 'totalRevenue'.

    Args:
        metric: GA4 metric name (e.g. 'sessions', 'activeUsers', 'conversions', 'totalRevenue').
        property_id: GA4 property ID. Leave blank to use default.
        days: Number of days to look back (default 30).
    """
    try:
        t = date.today()
        sd, ed = str(t - timedelta(days=days)), str(t - timedelta(days=1))
        rows = _simple_report(property_id, sd, ed, [], [metric], "", 1)
        value = rows[0].get(metric) if rows else "0"
        return json.dumps({"metric": metric, "value": value, "date_range": f"{sd} to {ed}", "days": days})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def ga4_top_n(
    dimension: str,
    metric: str = "sessions",
    n: int = 10,
    property_id: str = "",
    days: int = 30,
) -> str:
    """Get the top N rows for any dimension ranked by a metric.

    Args:
        dimension: GA4 dimension to group by (e.g. 'pagePath', 'country', 'sessionCampaignName').
        metric: Metric to rank by (default 'sessions').
        n: Number of rows to return (default 10).
        property_id: GA4 property ID. Leave blank to use default.
        days: Number of days to look back (default 30).
    """
    try:
        t = date.today()
        sd, ed = str(t - timedelta(days=days)), str(t - timedelta(days=1))
        rows = _gen_report(property_id, sd, ed, [dimension], [metric, "activeUsers", "conversions"], metric, n)
        return json.dumps({"dimension": dimension, "metric": metric, "date_range": f"{sd} to {ed}", "rows": rows})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def ga4_metric_trend(
    metric: str = "sessions",
    property_id: str = "",
    days: int = 30,
) -> str:
    """Get daily trend data for a metric — useful for spotting anomalies or growth trends.

    Args:
        metric: GA4 metric name (e.g. 'sessions', 'conversions', 'totalRevenue').
        property_id: GA4 property ID. Leave blank to use default.
        days: Number of days of daily data to return (default 30).
    """
    try:
        t = date.today()
        sd, ed = str(t - timedelta(days=days)), str(t - timedelta(days=1))
        rows = _gen_report(property_id, sd, ed, ["date"], [metric], "", days)
        rows.sort(key=lambda r: r.get("date", ""))
        return json.dumps({"metric": metric, "date_range": f"{sd} to {ed}", "daily": rows})
    except Exception as e:
        return json.dumps({"error": str(e)})
