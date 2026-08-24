"""
Google Search Console tools — adapted for remote server.
All 15 tools from mcp-gsc/server.py.
Credentials come from current_user_ctx (per-user Google refresh token) instead of env vars.
Write tools: gsc_submit_sitemap, gsc_delete_sitemap require editor role.
"""
import json
import os
from datetime import date, timedelta

import requests as httpx
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from mcp_instance import mcp
from credentials import connect_hint
from auth import current_connection_token_ctx, current_user_ctx
from permissions import require_editor

# ---------------------------------------------------------------------------
# Scopes — use webmasters (not .readonly) to allow sitemap submit/delete
# ---------------------------------------------------------------------------
GSC_SCOPES = [
    "https://www.googleapis.com/auth/webmasters",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]


# ---------------------------------------------------------------------------
# Credential helpers — NO global cache; always build per-request for multi-user
# ---------------------------------------------------------------------------

def _creds() -> Credentials:
    """Return fresh OAuth2 credentials for the currently authenticated user."""
    user = current_user_ctx.get(None)
    if user is None:
        raise RuntimeError("Not authenticated.")
    from credentials import provider
    rt = current_connection_token_ctx.get(None) or provider().google_token("gsc")
    if not rt:
        raise RuntimeError("Search Console account not connected. Connect your Google account via " + connect_hint() + ".")
    creds = Credentials(
        token=None,
        refresh_token=rt,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        scopes=GSC_SCOPES,
    )
    creds.refresh(Request())
    return creds


def _svc():
    return build("searchconsole", "v1", credentials=_creds())


def _resolve_site(svc, site_url: str) -> str:
    if site_url:
        return site_url
    env_site = os.environ.get("GSC_SITE_URL", "")
    if env_site:
        return env_site
    sites = svc.sites().list().execute().get("siteEntry", [])
    if sites:
        return sites[0]["siteUrl"]
    raise ValueError("No GSC site found. Set GSC_SITE_URL in .env or pass site_url.")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def gsc_list_sites() -> str:
    """List all websites the user has access to in Google Search Console."""
    try:
        svc = _svc()
        sites = svc.sites().list().execute()
        return json.dumps({"sites": [s["siteUrl"] for s in sites.get("siteEntry", [])]})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gsc_search_analytics(
    site_url: str = "",
    start_date: str = "",
    end_date: str = "",
    dimensions: str = "query",
    row_limit: int = 20,
    sort_by: str = "clicks",
) -> str:
    """Get search performance data (clicks, impressions, CTR, position) from Google Search Console.

    Args:
        site_url: Site URL to query. Leave blank to use the default site.
        start_date: Start date YYYY-MM-DD. Defaults to 28 days ago.
        end_date: End date YYYY-MM-DD. Defaults to today.
        dimensions: Comma-separated dimensions, e.g. 'query', 'page', 'country', 'query,page'.
        row_limit: Max rows to return. Default 20.
        sort_by: Sort metric: clicks, impressions, ctr, position. Default clicks.
    """
    try:
        svc = _svc()
        site = _resolve_site(svc, site_url)
        ed = end_date or str(date.today())
        sd = start_date or str(date.today() - timedelta(days=28))
        dims = [d.strip() for d in dimensions.split(",") if d.strip()]
        body = {
            "startDate": sd, "endDate": ed, "dimensions": dims, "rowLimit": row_limit,
            "orderBy": [{"fieldName": sort_by, "sortOrder": "DESCENDING"}],
        }
        rows = svc.searchanalytics().query(siteUrl=site, body=body).execute().get("rows", [])
        results = []
        for row in rows:
            r = {dims[i]: row["keys"][i] for i in range(len(dims))}
            r.update({"clicks": row.get("clicks", 0), "impressions": row.get("impressions", 0), "ctr": f"{row.get('ctr', 0) * 100:.2f}%", "position": round(row.get("position", 0), 1)})
            results.append(r)
        return json.dumps({"site": site, "date_range": f"{sd} to {ed}", "rows": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gsc_top_pages(
    site_url: str = "",
    start_date: str = "",
    end_date: str = "",
    row_limit: int = 20,
) -> str:
    """Get the top performing pages by clicks from Google Search Console.

    Args:
        site_url: Site URL. Leave blank to use the default site.
        start_date: Start date YYYY-MM-DD. Defaults to 28 days ago.
        end_date: End date YYYY-MM-DD. Defaults to today.
        row_limit: Max rows. Default 20.
    """
    try:
        svc = _svc()
        site = _resolve_site(svc, site_url)
        ed = end_date or str(date.today())
        sd = start_date or str(date.today() - timedelta(days=28))
        body = {
            "startDate": sd, "endDate": ed, "dimensions": ["page"], "rowLimit": row_limit,
            "orderBy": [{"fieldName": "clicks", "sortOrder": "DESCENDING"}],
        }
        rows = svc.searchanalytics().query(siteUrl=site, body=body).execute().get("rows", [])
        results = [{"page": row["keys"][0], "clicks": row.get("clicks", 0), "impressions": row.get("impressions", 0), "ctr": f"{row.get('ctr', 0) * 100:.2f}%", "position": round(row.get("position", 0), 1)} for row in rows]
        return json.dumps({"site": site, "date_range": f"{sd} to {ed}", "rows": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gsc_top_keywords(
    site_url: str = "",
    start_date: str = "",
    end_date: str = "",
    row_limit: int = 25,
    sort_by: str = "clicks",
) -> str:
    """Get top keywords driving traffic from Google Search, with clicks, impressions, CTR and average position.

    Args:
        site_url: Site URL. Leave blank to use the default site.
        start_date: Start date YYYY-MM-DD. Defaults to 28 days ago.
        end_date: End date YYYY-MM-DD. Defaults to today.
        row_limit: Max keywords. Default 25.
        sort_by: clicks, impressions, ctr, or position. Default clicks.
    """
    try:
        svc = _svc()
        site = _resolve_site(svc, site_url)
        ed = end_date or str(date.today())
        sd = start_date or str(date.today() - timedelta(days=28))
        body = {
            "startDate": sd, "endDate": ed, "dimensions": ["query"], "rowLimit": row_limit,
            "orderBy": [{"fieldName": sort_by, "sortOrder": "DESCENDING"}],
        }
        rows = svc.searchanalytics().query(siteUrl=site, body=body).execute().get("rows", [])
        results = [{"query": row["keys"][0], "clicks": row.get("clicks", 0), "impressions": row.get("impressions", 0), "ctr": f"{row.get('ctr', 0) * 100:.2f}%", "position": round(row.get("position", 0), 1)} for row in rows]
        return json.dumps({"site": site, "date_range": f"{sd} to {ed}", "rows": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gsc_country_breakdown(
    site_url: str = "",
    start_date: str = "",
    end_date: str = "",
    row_limit: int = 20,
) -> str:
    """Get Google Search Console performance broken down by country.

    Args:
        site_url: Site URL. Leave blank to use the default site.
        start_date: Start date YYYY-MM-DD. Defaults to 28 days ago.
        end_date: End date YYYY-MM-DD. Defaults to today.
        row_limit: Max countries. Default 20.
    """
    try:
        svc = _svc()
        site = _resolve_site(svc, site_url)
        ed = end_date or str(date.today())
        sd = start_date or str(date.today() - timedelta(days=28))
        body = {
            "startDate": sd, "endDate": ed, "dimensions": ["country"], "rowLimit": row_limit,
            "orderBy": [{"fieldName": "clicks", "sortOrder": "DESCENDING"}],
        }
        rows = svc.searchanalytics().query(siteUrl=site, body=body).execute().get("rows", [])
        results = [{"country": row["keys"][0], "clicks": row.get("clicks", 0), "impressions": row.get("impressions", 0), "ctr": f"{row.get('ctr', 0) * 100:.2f}%", "position": round(row.get("position", 0), 1)} for row in rows]
        return json.dumps({"site": site, "date_range": f"{sd} to {ed}", "rows": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gsc_device_breakdown(
    site_url: str = "",
    start_date: str = "",
    end_date: str = "",
) -> str:
    """Get Google Search Console performance broken down by device type: desktop, mobile, tablet.

    Args:
        site_url: Site URL. Leave blank to use the default site.
        start_date: Start date YYYY-MM-DD. Defaults to 28 days ago.
        end_date: End date YYYY-MM-DD. Defaults to today.
    """
    try:
        svc = _svc()
        site = _resolve_site(svc, site_url)
        ed = end_date or str(date.today())
        sd = start_date or str(date.today() - timedelta(days=28))
        body = {
            "startDate": sd, "endDate": ed, "dimensions": ["device"], "rowLimit": 10,
            "orderBy": [{"fieldName": "clicks", "sortOrder": "DESCENDING"}],
        }
        rows = svc.searchanalytics().query(siteUrl=site, body=body).execute().get("rows", [])
        results = [{"device": row["keys"][0], "clicks": row.get("clicks", 0), "impressions": row.get("impressions", 0), "ctr": f"{row.get('ctr', 0) * 100:.2f}%", "position": round(row.get("position", 0), 1)} for row in rows]
        return json.dumps({"site": site, "date_range": f"{sd} to {ed}", "rows": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gsc_page_keywords(
    page_url: str,
    site_url: str = "",
    start_date: str = "",
    end_date: str = "",
    row_limit: int = 20,
) -> str:
    """Get the top keywords/queries driving clicks to a specific page URL in Google Search Console.

    Args:
        page_url: The full URL of the page to analyse (required).
        site_url: GSC property URL. Leave blank to use the default site.
        start_date: Start date YYYY-MM-DD. Defaults to 28 days ago.
        end_date: End date YYYY-MM-DD. Defaults to today.
        row_limit: Max rows. Default 20.
    """
    try:
        svc = _svc()
        site = _resolve_site(svc, site_url)
        ed = end_date or str(date.today())
        sd = start_date or str(date.today() - timedelta(days=28))
        body = {
            "startDate": sd, "endDate": ed, "dimensions": ["query"], "rowLimit": row_limit,
            "orderBy": [{"fieldName": "clicks", "sortOrder": "DESCENDING"}],
            "dimensionFilterGroups": [{"filters": [{"dimension": "page", "operator": "equals", "expression": page_url}]}],
        }
        rows = svc.searchanalytics().query(siteUrl=site, body=body).execute().get("rows", [])
        results = [{"query": row["keys"][0], "clicks": row.get("clicks", 0), "impressions": row.get("impressions", 0), "ctr": f"{row.get('ctr', 0) * 100:.2f}%", "position": round(row.get("position", 0), 1)} for row in rows]
        return json.dumps({"site": page_url, "date_range": f"{sd} to {ed}", "rows": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gsc_inspect_url(url: str, site_url: str = "") -> str:
    """Inspect a specific URL in Google Search Console to check its indexing status and crawl info.

    Args:
        url: The full URL to inspect, e.g. https://example.com/page (required).
        site_url: GSC property URL. Leave blank to use the default site.
    """
    try:
        creds = _creds()
        svc = _svc()
        site = _resolve_site(svc, site_url)
        resp = httpx.post(
            "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect",
            json={"inspectionUrl": url, "siteUrl": site},
            headers={"Authorization": f"Bearer {creds.token}"},
            timeout=20,
        )
        if not resp.ok:
            return json.dumps({"error": resp.text[:400]})
        return json.dumps(resp.json())
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gsc_sitemaps(site_url: str = "") -> str:
    """List all sitemaps submitted to Google Search Console for this site.

    Args:
        site_url: The site URL. Leave blank to use the default site.
    """
    try:
        svc = _svc()
        site = _resolve_site(svc, site_url)
        result = svc.sitemaps().list(siteUrl=site).execute()
        return json.dumps({"site": site, "sitemaps": result.get("sitemap", [])})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gsc_crawl_errors(site_url: str = "") -> str:
    """Get URL coverage and a sample of indexed pages from Google Search Console.

    Args:
        site_url: The site URL. Leave blank to use the default site.
    """
    try:
        svc = _svc()
        site = _resolve_site(svc, site_url)
        end_dt = date.today()
        start_dt = end_dt - timedelta(days=28)
        result = svc.searchanalytics().query(siteUrl=site, body={
            "startDate": str(start_dt), "endDate": str(end_dt),
            "dimensions": ["page"], "rowLimit": 25, "dataState": "all",
        }).execute()
        rows = result.get("rows", [])
        return json.dumps({
            "site": site,
            "note": "GSC API v1 does not expose aggregate crawl errors. Use gsc_inspect_url for individual URL coverage status.",
            "indexed_pages_sample": [{"url": r["keys"][0], "clicks": r.get("clicks", 0), "impressions": r.get("impressions", 0)} for r in rows],
        })
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gsc_submit_sitemap(sitemap_url: str, site_url: str = "") -> str:
    """Submit a sitemap URL to Google Search Console.

    Args:
        sitemap_url: Full sitemap URL, e.g. 'https://example.com/sitemap.xml' (required).
        site_url: GSC property URL. Leave blank to use the default site.
    """
    if err := require_editor("gsc_submit_sitemap"):
        return err
    try:
        svc = _svc()
        site = _resolve_site(svc, site_url)
        svc.sitemaps().submit(siteUrl=site, feedpath=sitemap_url).execute()
        return json.dumps({"success": True, "site_url": site, "submitted_sitemap": sitemap_url})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gsc_delete_sitemap(sitemap_url: str, site_url: str = "") -> str:
    """Delete/remove a sitemap from Google Search Console.

    Args:
        sitemap_url: Full sitemap URL to delete (required).
        site_url: GSC property URL. Leave blank to use the default site.
    """
    if err := require_editor("gsc_delete_sitemap"):
        return err
    try:
        svc = _svc()
        site = _resolve_site(svc, site_url)
        svc.sitemaps().delete(siteUrl=site, feedpath=sitemap_url).execute()
        return json.dumps({"success": True, "site_url": site, "deleted_sitemap": sitemap_url})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gsc_date_trend(site_url: str = "", start_date: str = "", end_date: str = "") -> str:
    """Get day-by-day clicks and impressions trend data from Google Search Console.

    Args:
        site_url: Site URL. Leave blank to use the default site.
        start_date: Start date YYYY-MM-DD. Defaults to 28 days ago.
        end_date: End date YYYY-MM-DD. Defaults to today.
    """
    try:
        svc = _svc()
        site = _resolve_site(svc, site_url)
        ed = end_date or str(date.today())
        sd = start_date or str(date.today() - timedelta(days=28))
        rows = svc.searchanalytics().query(siteUrl=site, body={
            "startDate": sd, "endDate": ed, "dimensions": ["date"], "rowLimit": 500,
            "orderBy": [{"fieldName": "date", "sortOrder": "ASCENDING"}],
        }).execute().get("rows", [])
        trend = [{"date": row["keys"][0], "clicks": row.get("clicks", 0), "impressions": row.get("impressions", 0), "ctr": f"{row.get('ctr', 0) * 100:.2f}%", "position": round(row.get("position", 0), 1)} for row in rows]
        return json.dumps({"site": site, "date_range": f"{sd} to {ed}", "daily_trend": trend})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gsc_search_appearance(site_url: str = "", start_date: str = "", end_date: str = "") -> str:
    """Get GSC performance broken down by search appearance type: Web, AMP, Rich Results, Video, etc.

    Args:
        site_url: Site URL. Leave blank to use the default site.
        start_date: Start date YYYY-MM-DD. Defaults to 28 days ago.
        end_date: End date YYYY-MM-DD. Defaults to today.
    """
    try:
        svc = _svc()
        site = _resolve_site(svc, site_url)
        ed = end_date or str(date.today())
        sd = start_date or str(date.today() - timedelta(days=28))
        rows = svc.searchanalytics().query(siteUrl=site, body={
            "startDate": sd, "endDate": ed, "dimensions": ["searchAppearance"], "rowLimit": 25,
            "orderBy": [{"fieldName": "clicks", "sortOrder": "DESCENDING"}],
        }).execute().get("rows", [])
        results = [{"search_appearance": row["keys"][0], "clicks": row.get("clicks", 0), "impressions": row.get("impressions", 0), "ctr": f"{row.get('ctr', 0) * 100:.2f}%", "position": round(row.get("position", 0), 1)} for row in rows]
        return json.dumps({"site": site, "date_range": f"{sd} to {ed}", "search_appearances": results})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def gsc_compare_periods(
    period1_start: str,
    period1_end: str,
    period2_start: str,
    period2_end: str,
    site_url: str = "",
    dimensions: str = "",
    row_limit: int = 10,
) -> str:
    """Compare Google Search Console performance between two date ranges side by side.

    Args:
        period1_start: Start of first period YYYY-MM-DD (required).
        period1_end: End of first period YYYY-MM-DD (required).
        period2_start: Start of second period YYYY-MM-DD (required).
        period2_end: End of second period YYYY-MM-DD (required).
        site_url: Site URL. Leave blank to use the default site.
        dimensions: Comma-separated dimensions, e.g. 'query' or 'page'. Leave blank for totals only.
        row_limit: Max rows per period. Default 10.
    """
    try:
        svc = _svc()
        site = _resolve_site(svc, site_url)
        dims = [d.strip() for d in dimensions.split(",") if d.strip()]

        def _gsc_period(start, end):
            body: dict = {
                "startDate": start, "endDate": end, "rowLimit": row_limit,
                "orderBy": [{"fieldName": "clicks", "sortOrder": "DESCENDING"}],
            }
            if dims:
                body["dimensions"] = dims
            return svc.searchanalytics().query(siteUrl=site, body=body).execute().get("rows", [])

        def _parse_rows(rows):
            out = []
            for row in rows:
                r = {}
                if dims:
                    r.update({dims[i]: row["keys"][i] for i in range(len(dims))})
                r.update({"clicks": row.get("clicks", 0), "impressions": row.get("impressions", 0), "ctr": f"{row.get('ctr', 0) * 100:.2f}%", "position": round(row.get("position", 0), 1)})
                out.append(r)
            return out

        period1_rows = _parse_rows(_gsc_period(period1_start, period1_end))
        period2_rows = _parse_rows(_gsc_period(period2_start, period2_end))

        def _total(rows, field):
            return sum(r.get(field, 0) for r in rows if isinstance(r.get(field, 0), (int, float)))

        p1_clicks = _total(period1_rows, "clicks")
        p2_clicks = _total(period2_rows, "clicks")
        summary = {
            "period1": {"date_range": f"{period1_start} to {period1_end}", "total_clicks": p1_clicks, "total_impressions": _total(period1_rows, "impressions")},
            "period2": {"date_range": f"{period2_start} to {period2_end}", "total_clicks": p2_clicks, "total_impressions": _total(period2_rows, "impressions")},
            "clicks_change_pct": round((p2_clicks - p1_clicks) / max(p1_clicks, 1) * 100, 1),
        }
        return json.dumps({"site": site, "summary": summary, "period1_rows": period1_rows, "period2_rows": period2_rows})
    except Exception as e:
        return json.dumps({"error": str(e)})
