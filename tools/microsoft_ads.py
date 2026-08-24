"""
Microsoft Advertising (Bing Ads) tools backed by Pipedream Connect.

OAuth is handled by Pipedream managed auth, but Microsoft additionally requires a
*developer token* -- a partner-level entitlement key that is not part of OAuth. Set it
once as MICROSOFT_ADS_DEVELOPER_TOKEN; a single universal token works for every user
who connects. Without it every call here fails fast with a clear message rather than a
confusing 401 from Microsoft.

Protocol notes (Bing Ads API v13 REST, which replaces SOAP -- SOAP loses new features
on 2026-10-01 and is fully deprecated 2027-01-31):
  * REST is split across per-service hosts, so URLs are built absolute per service.
  * Read operations are POST with a JSON body, not GET (e.g. Campaigns/QueryByAccountId).
  * Updates are PUT.
  * CustomerId / CustomerAccountId travel as headers on most Campaign Management calls.
  * Reporting is asynchronous: submit -> poll -> download a (zipped) CSV.
"""
import csv
import io
import os
import time
import zipfile
from datetime import date, timedelta

import httpx

from mcp_instance import mcp
from permissions import require_editor
from tools.platform_http import (
    _json,
    _parse_json_arg,
    list_accounts as _list_pipedream_accounts,
    request as _proxy_request,
)

_PLATFORM = "microsoft_ads"

_CAMPAIGN_HOST = "https://campaign.api.bingads.microsoft.com/CampaignManagement/v13"
_CUSTOMER_HOST = "https://clientcenter.api.bingads.microsoft.com/CustomerManagement/v13"
_REPORTING_HOST = "https://reporting.api.bingads.microsoft.com/Reporting/v13"


def _developer_token() -> str:
    token = os.environ.get("MICROSOFT_ADS_DEVELOPER_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "Microsoft Advertising needs a developer token. Set MICROSOFT_ADS_DEVELOPER_TOKEN "
            "(request one from the Microsoft Advertising Developer Portal). OAuth alone is not enough."
        )
    return token


def _ms_headers(customer_id: str = "", account_id: str = "") -> dict:
    """Build the non-OAuth headers Microsoft requires. Pipedream injects Authorization."""
    headers = {"DeveloperToken": _developer_token()}
    if customer_id:
        headers["CustomerId"] = str(customer_id)
    if account_id:
        headers["CustomerAccountId"] = str(account_id)
    return headers


def _ms_date(value: str, fallback_days_ago: int) -> dict:
    """Microsoft wants a {Day, Month, Year} object, not an ISO string."""
    raw = (value or "").strip() or str(date.today() - timedelta(days=fallback_days_ago))
    year, month, day = [int(part) for part in raw.split("-")]
    return {"Day": day, "Month": month, "Year": year}


@mcp.tool()
def microsoft_ads_pipedream_accounts() -> str:
    """List connected Pipedream accounts that can be used for Microsoft Advertising proxy calls."""
    try:
        accounts = _list_pipedream_accounts(_PLATFORM)
        has_token = bool(os.environ.get("MICROSOFT_ADS_DEVELOPER_TOKEN", "").strip())
        return _json({
            "accounts": accounts,
            "total": len(accounts),
            "developer_token_configured": has_token,
            "note": None if has_token else "MICROSOFT_ADS_DEVELOPER_TOKEN is not set; API calls will fail until it is.",
        })
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def microsoft_ads_list_accounts(customer_id: str = "", only_parent_accounts: bool = False, pipedream_account_id: str = "") -> str:
    """List Microsoft Advertising ad accounts the connected user can access.

    Start here — other actions need the account id (CustomerAccountId) this returns.
    customer_id is the manager account id; leave blank to infer it from the credentials.
    """
    try:
        body = {"OnlyParentAccounts": bool(only_parent_accounts)}
        if customer_id:
            body["CustomerId"] = str(customer_id)
        return _json(_proxy_request(
            _PLATFORM, "POST", f"{_CUSTOMER_HOST}/AccountsInfo/Query", pipedream_account_id,
            body=body, headers=_ms_headers(customer_id=customer_id),
        ))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def microsoft_ads_list_campaigns(
    account_id: str, customer_id: str = "", campaign_type: str = "Search", pipedream_account_id: str = ""
) -> str:
    """List Microsoft Advertising campaigns in an account.

    campaign_type: Search, Shopping, DynamicSearchAds, Audience, PerformanceMax. Space-delimit
    to combine (e.g. "Search Shopping"). Defaults to Search, which is Microsoft's own default.
    """
    try:
        body = {"AccountId": str(account_id)}
        if campaign_type:
            body["CampaignType"] = campaign_type
        return _json(_proxy_request(
            _PLATFORM, "POST", f"{_CAMPAIGN_HOST}/Campaigns/QueryByAccountId", pipedream_account_id,
            body=body, headers=_ms_headers(customer_id=customer_id, account_id=account_id),
        ))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def microsoft_ads_list_adgroups(
    campaign_id: str, account_id: str, customer_id: str = "", pipedream_account_id: str = ""
) -> str:
    """List Microsoft Advertising ad groups in a campaign."""
    try:
        return _json(_proxy_request(
            _PLATFORM, "POST", f"{_CAMPAIGN_HOST}/AdGroups/QueryByCampaignId", pipedream_account_id,
            body={"CampaignId": str(campaign_id)},
            headers=_ms_headers(customer_id=customer_id, account_id=account_id),
        ))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def microsoft_ads_list_keywords(
    ad_group_id: str, account_id: str, customer_id: str = "", pipedream_account_id: str = ""
) -> str:
    """List Microsoft Advertising keywords in an ad group."""
    try:
        return _json(_proxy_request(
            _PLATFORM, "POST", f"{_CAMPAIGN_HOST}/Keywords/QueryByAdGroupId", pipedream_account_id,
            body={"AdGroupId": str(ad_group_id)},
            headers=_ms_headers(customer_id=customer_id, account_id=account_id),
        ))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def microsoft_ads_update_campaign_status(
    campaign_id: str, account_id: str, status: str, customer_id: str = "", pipedream_account_id: str = ""
) -> str:
    """Pause or enable a Microsoft Advertising campaign. status must be Paused or Active.

    Microsoft's update is a partial merge — only Id and Status are sent, so other campaign
    settings are left untouched.
    """
    denied = require_editor("microsoft_ads_update_campaign_status")
    if denied:
        return denied
    try:
        normalized = (status or "").strip().capitalize()
        if normalized not in ("Paused", "Active"):
            return _json({"error": "status must be Paused or Active."})
        return _json(_proxy_request(
            _PLATFORM, "PUT", f"{_CAMPAIGN_HOST}/Campaigns", pipedream_account_id,
            body={"AccountId": str(account_id), "Campaigns": [{"Id": str(campaign_id), "Status": normalized}]},
            headers=_ms_headers(customer_id=customer_id, account_id=account_id),
        ))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def microsoft_ads_update_campaign_budget(
    campaign_id: str, account_id: str, daily_budget: float, customer_id: str = "", pipedream_account_id: str = ""
) -> str:
    """Set the daily budget on a Microsoft Advertising campaign, in account currency."""
    denied = require_editor("microsoft_ads_update_campaign_budget")
    if denied:
        return denied
    try:
        amount = float(daily_budget)
        if amount <= 0:
            return _json({"error": "daily_budget must be greater than zero."})
        return _json(_proxy_request(
            _PLATFORM, "PUT", f"{_CAMPAIGN_HOST}/Campaigns", pipedream_account_id,
            body={"AccountId": str(account_id), "Campaigns": [{"Id": str(campaign_id), "DailyBudget": amount}]},
            headers=_ms_headers(customer_id=customer_id, account_id=account_id),
        ))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def microsoft_ads_submit_report(
    account_id: str,
    report_type: str = "CampaignPerformanceReport",
    columns_json: str = "",
    start_date: str = "",
    end_date: str = "",
    aggregation: str = "Daily",
    customer_id: str = "",
    pipedream_account_id: str = "",
) -> str:
    """Submit a Microsoft Advertising report request. Returns a ReportRequestId to poll.

    Prefer microsoft_ads_campaign_performance for the common case — it submits, polls and
    parses in one call. Use this directly only for report types it does not cover.

    Args:
        report_type: e.g. CampaignPerformanceReport, AdGroupPerformanceReport, KeywordPerformanceReport.
        columns_json: JSON array of column names. Defaults to a sensible campaign set.
        start_date / end_date: YYYY-MM-DD. Default last 28 days.
        aggregation: Daily, Weekly, Monthly, or Summary.
    """
    try:
        columns = _parse_json_arg(columns_json, "columns_json", None) or [
            "TimePeriod", "CampaignId", "CampaignName", "Impressions", "Clicks",
            "Spend", "Conversions", "Ctr", "AverageCpc",
        ]
        body = {
            "ReportRequest": {
                "Type": report_type,
                "Format": "Csv",
                "ReturnOnlyCompleteData": False,
                "Aggregation": aggregation,
                "Columns": columns,
                "Scope": {"AccountIds": [str(account_id)]},
                "Time": {
                    "CustomDateRangeStart": _ms_date(start_date, 28),
                    "CustomDateRangeEnd": _ms_date(end_date, 0),
                },
            }
        }
        return _json(_proxy_request(
            _PLATFORM, "POST", f"{_REPORTING_HOST}/GenerateReport/Submit", pipedream_account_id,
            body=body, headers=_ms_headers(customer_id=customer_id, account_id=account_id),
        ))
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def microsoft_ads_poll_report(
    report_request_id: str, account_id: str, customer_id: str = "", pipedream_account_id: str = ""
) -> str:
    """Check a submitted Microsoft Advertising report. Returns status and, when ready, a download URL."""
    try:
        return _json(_proxy_request(
            _PLATFORM, "POST", f"{_REPORTING_HOST}/GenerateReport/Poll", pipedream_account_id,
            body={"ReportRequestId": str(report_request_id)},
            headers=_ms_headers(customer_id=customer_id, account_id=account_id),
        ))
    except Exception as exc:
        return _json({"error": str(exc)})


def _download_report_rows(url: str, row_limit: int) -> dict:
    """Fetch a completed report. Microsoft returns a ZIP containing one CSV.

    The download URL is pre-signed, so this is a direct fetch rather than a proxy call.
    """
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        response = client.get(url)
    if response.status_code >= 400:
        return {"error": f"Report download failed: HTTP {response.status_code}"}

    raw = response.content
    text = ""
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = [n for n in archive.namelist() if n.lower().endswith(".csv")] or archive.namelist()
            if not names:
                return {"error": "Report archive was empty."}
            text = archive.read(names[0]).decode("utf-8-sig", errors="replace")
    else:
        text = raw.decode("utf-8-sig", errors="replace")

    # Microsoft pads the CSV with report metadata above the header and a footer below it.
    # The real header is the first line that looks like a column row.
    lines = [ln for ln in text.splitlines() if ln.strip()]
    start = 0
    for index, line in enumerate(lines):
        if "," in line and not line.startswith('"Report Name"') and not line.startswith('"Report Time"'):
            start = index
            break
    reader = csv.DictReader(lines[start:])
    rows = []
    for row in reader:
        if row.get(reader.fieldnames[0]) in (None, "", "©2026 Microsoft Corporation"):
            continue
        rows.append(row)
        if len(rows) >= row_limit:
            break
    return {"rows": rows, "row_count": len(rows), "columns": reader.fieldnames or []}


@mcp.tool()
def microsoft_ads_campaign_performance(
    account_id: str,
    start_date: str = "",
    end_date: str = "",
    aggregation: str = "Daily",
    row_limit: int = 200,
    customer_id: str = "",
    pipedream_account_id: str = "",
) -> str:
    """Get Microsoft Advertising campaign performance for a date range.

    Submits the report, polls until it is ready, then downloads and parses the CSV — the
    model sees one call. Defaults to the last 28 days. Polling is bounded; if the report
    is not ready in time the ReportRequestId is returned so it can be polled separately.
    """
    try:
        submitted = _proxy_request(
            _PLATFORM, "POST", f"{_REPORTING_HOST}/GenerateReport/Submit", pipedream_account_id,
            body={
                "ReportRequest": {
                    "Type": "CampaignPerformanceReport",
                    "Format": "Csv",
                    "ReturnOnlyCompleteData": False,
                    "Aggregation": aggregation,
                    "Columns": [
                        "TimePeriod", "CampaignId", "CampaignName", "Impressions", "Clicks",
                        "Spend", "Conversions", "Ctr", "AverageCpc",
                    ],
                    "Scope": {"AccountIds": [str(account_id)]},
                    "Time": {
                        "CustomDateRangeStart": _ms_date(start_date, 28),
                        "CustomDateRangeEnd": _ms_date(end_date, 0),
                    },
                }
            },
            headers=_ms_headers(customer_id=customer_id, account_id=account_id),
        )
        if isinstance(submitted, dict) and submitted.get("error"):
            return _json(submitted)
        request_id = (submitted or {}).get("ReportRequestId")
        if not request_id:
            return _json({"error": "Microsoft did not return a ReportRequestId.", "response": submitted})

        headers = _ms_headers(customer_id=customer_id, account_id=account_id)
        status = {}
        for _attempt in range(10):
            time.sleep(3)
            status = _proxy_request(
                _PLATFORM, "POST", f"{_REPORTING_HOST}/GenerateReport/Poll", pipedream_account_id,
                body={"ReportRequestId": str(request_id)}, headers=headers,
            )
            if isinstance(status, dict) and status.get("error"):
                return _json(status)
            info = (status or {}).get("ReportRequestStatus") or {}
            state = str(info.get("Status") or "").strip()
            if state == "Success":
                url = info.get("ReportDownloadUrl")
                if not url:
                    # Success with no URL means the report matched zero rows.
                    return _json({"rows": [], "row_count": 0, "report_request_id": request_id,
                                  "note": "Report completed with no data for this date range."})
                result = _download_report_rows(url, max(1, min(int(row_limit or 200), 5000)))
                result["report_request_id"] = request_id
                return _json(result)
            if state == "Error":
                return _json({"error": "Microsoft reported an error generating the report.", "response": status})

        return _json({
            "error": "Report was not ready in time.",
            "report_request_id": request_id,
            "next_step": "Call microsoft_ads_poll_report with this report_request_id.",
            "last_status": status,
        })
    except Exception as exc:
        return _json({"error": str(exc)})


@mcp.tool()
def microsoft_ads_raw_request(
    method: str,
    url: str,
    body_json: str = "{}",
    account_id: str = "",
    customer_id: str = "",
    pipedream_account_id: str = "",
) -> str:
    """Call any Microsoft Advertising v13 REST endpoint through the Pipedream proxy.

    Escape hatch for operations without a dedicated tool. url must be absolute, e.g.
    https://campaign.api.bingads.microsoft.com/CampaignManagement/v13/Ads/QueryByAdGroupId
    Remember most read operations are POST with a JSON body, and updates are PUT.
    """
    denied = require_editor("microsoft_ads_raw_request")
    if denied:
        return denied
    try:
        body = _parse_json_arg(body_json, "body_json", {})
        return _json(_proxy_request(
            _PLATFORM, method, url, pipedream_account_id, body=body or None,
            headers=_ms_headers(customer_id=customer_id, account_id=account_id),
        ))
    except Exception as exc:
        return _json({"error": str(exc)})
