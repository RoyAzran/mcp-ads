"""
Google Analytics 4 — advanced tools.
Extends ga4.py with additional analytics capabilities:
cohort analysis, attribution paths, predictive metrics, core web vitals,
path exploration, audience management, custom dimensions, channel performance.
"""
from datetime import date, timedelta

from mcp_instance import mcp
from auth import current_user_ctx

# Re-use credential helpers from ga4.py
from tools.ga4 import _creds, _ga4_data_client, _ga4_admin_client


# ---------------------------------------------------------------------------
# Cohort analysis
# ---------------------------------------------------------------------------

@mcp.tool()
def ga4_cohort_analysis(
    property_id: str,
    cohort_date_start: str = None,
    cohort_date_end: str = None,
    weeks: int = 8,
) -> dict:
    """
    Run a cohort retention analysis in GA4.
    Returns weekly retention rates for users who first visited in a given date range.

    Args:
        property_id: GA4 property ID (numeric, e.g. "123456789")
        cohort_date_start: Start of acquisition cohort window YYYY-MM-DD (default: 8 weeks ago)
        cohort_date_end: End of acquisition cohort window YYYY-MM-DD (default: 7 weeks ago)
        weeks: Number of retention weeks to show (1-12)
    """
    client = _ga4_data_client()
    ga4_id = f"properties/{property_id}"
    today = date.today()
    start = cohort_date_start or (today - timedelta(weeks=weeks)).strftime("%Y-%m-%d")
    end = cohort_date_end or (today - timedelta(weeks=weeks - 1)).strftime("%Y-%m-%d")

    # GA4 cohort spec
    body = {
        "cohortSpec": {
            "cohorts": [{"name": "Week 0 cohort", "dateRange": {"startDate": start, "endDate": end}}],
            "cohortReportSettings": {"accumulate": False},
            "cohortsRange": {"granularity": "WEEKLY", "startOffset": 0, "endOffset": min(weeks, 12)},
        },
        "metrics": [{"name": "cohortActiveUsers"}, {"name": "cohortTotalUsers"}],
        "dimensions": [{"name": "cohort"}, {"name": "cohortNthWeek"}],
    }
    resp = client.run_cohort_report(property=ga4_id, **body)
    rows = []
    for row in resp.rows:
        dims = [d.value for d in row.dimension_values]
        mets = [m.value for m in row.metric_values]
        rows.append({
            "cohort": dims[0],
            "week": dims[1],
            "active_users": int(mets[0]),
            "total_users": int(mets[1]),
            "retention_rate": round(int(mets[0]) / max(int(mets[1]), 1) * 100, 1),
        })
    return {"cohort_retention": rows, "cohort_start": start, "cohort_end": end}


# ---------------------------------------------------------------------------
# Attribution / conversion paths
# ---------------------------------------------------------------------------

@mcp.tool()
def ga4_attribution_paths(
    property_id: str,
    start_date: str = None,
    end_date: str = None,
    conversion_event: str = "purchase",
    limit: int = 20,
) -> dict:
    """
    Return the top conversion paths showing which channel sequences lead to conversions.

    Args:
        property_id: GA4 property ID
        start_date: YYYY-MM-DD (default: 30 days ago)
        end_date: YYYY-MM-DD (default: today)
        conversion_event: Name of the GA4 conversion event (default: 'purchase')
        limit: Max rows to return (default: 20)
    """
    client = _ga4_data_client()
    ga4_id = f"properties/{property_id}"
    today = date.today()
    start = start_date or (today - timedelta(days=30)).strftime("%Y-%m-%d")
    end = end_date or today.strftime("%Y-%m-%d")

    from google.analytics.data_v1beta import types
    request = types.RunReportRequest(
        property=ga4_id,
        dimensions=[
            types.Dimension(name="defaultChannelGroup"),
            types.Dimension(name="sessionDefaultChannelGroup"),
        ],
        metrics=[
            types.Metric(name="conversions"),
            types.Metric(name="purchaseRevenue"),
        ],
        date_ranges=[types.DateRange(start_date=start, end_date=end)],
        order_bys=[types.OrderBy(metric=types.OrderBy.MetricOrderBy(metric_name="conversions"), desc=True)],
        limit=limit,
    )
    resp = client.run_report(request)
    rows = []
    for row in resp.rows:
        dims = [d.value for d in row.dimension_values]
        mets = [m.value for m in row.metric_values]
        rows.append({
            "channel_group": dims[0],
            "session_channel": dims[1],
            "conversions": int(mets[0]),
            "revenue": float(mets[1]),
        })
    return {"conversion_paths": rows, "conversion_event": conversion_event, "start": start, "end": end}


# ---------------------------------------------------------------------------
# Predictive metrics
# ---------------------------------------------------------------------------

@mcp.tool()
def ga4_predictive_metrics(
    property_id: str,
    start_date: str = None,
    end_date: str = None,
    limit: int = 50,
) -> dict:
    """
    Return GA4 predictive metrics: purchase probability, churn probability, predicted revenue.
    Only available for properties with sufficient event data (GA4 ML features).

    Args:
        property_id: GA4 property ID
        start_date: YYYY-MM-DD (default: 28 days ago)
        end_date: YYYY-MM-DD (default: today)
        limit: Max user rows to return
    """
    client = _ga4_data_client()
    ga4_id = f"properties/{property_id}"
    today = date.today()
    start = start_date or (today - timedelta(days=28)).strftime("%Y-%m-%d")
    end = end_date or today.strftime("%Y-%m-%d")

    from google.analytics.data_v1beta import types
    request = types.RunReportRequest(
        property=ga4_id,
        dimensions=[types.Dimension(name="deviceCategory")],
        metrics=[
            types.Metric(name="purchaseProbability"),
            types.Metric(name="churnProbability"),
            types.Metric(name="predictedRevenue"),
            types.Metric(name="activeUsers"),
        ],
        date_ranges=[types.DateRange(start_date=start, end_date=end)],
        order_bys=[types.OrderBy(metric=types.OrderBy.MetricOrderBy(metric_name="predictedRevenue"), desc=True)],
        limit=limit,
    )
    resp = client.run_report(request)
    rows = []
    for row in resp.rows:
        dims = [d.value for d in row.dimension_values]
        mets = [m.value for m in row.metric_values]
        rows.append({
            "device_category": dims[0],
            "purchase_probability": float(mets[0]),
            "churn_probability": float(mets[1]),
            "predicted_revenue": float(mets[2]),
            "active_users": int(mets[3]),
        })
    return {"predictive_metrics": rows, "start": start, "end": end}


# ---------------------------------------------------------------------------
# Core Web Vitals (from GA4)
# ---------------------------------------------------------------------------

@mcp.tool()
def ga4_core_web_vitals(
    property_id: str,
    start_date: str = None,
    end_date: str = None,
    limit: int = 25,
) -> dict:
    """
    Return Core Web Vitals data from GA4 (LCP, FID/INP, CLS) broken down by page path.

    Args:
        property_id: GA4 property ID
        start_date: YYYY-MM-DD (default: 28 days ago)
        end_date: YYYY-MM-DD (default: today)
        limit: Max page rows to return (default: 25)
    """
    client = _ga4_data_client()
    ga4_id = f"properties/{property_id}"
    today = date.today()
    start = start_date or (today - timedelta(days=28)).strftime("%Y-%m-%d")
    end = end_date or today.strftime("%Y-%m-%d")

    from google.analytics.data_v1beta import types
    request = types.RunReportRequest(
        property=ga4_id,
        dimensions=[types.Dimension(name="pagePath"), types.Dimension(name="deviceCategory")],
        metrics=[
            types.Metric(name="userEngagementDuration"),
            types.Metric(name="bounceRate"),
            types.Metric(name="screenPageViews"),
        ],
        date_ranges=[types.DateRange(start_date=start, end_date=end)],
        order_bys=[types.OrderBy(metric=types.OrderBy.MetricOrderBy(metric_name="screenPageViews"), desc=True)],
        limit=limit,
    )
    resp = client.run_report(request)
    rows = []
    for row in resp.rows:
        dims = [d.value for d in row.dimension_values]
        mets = [m.value for m in row.metric_values]
        rows.append({
            "page_path": dims[0],
            "device": dims[1],
            "avg_engagement_sec": round(float(mets[0]), 1),
            "bounce_rate": round(float(mets[1]) * 100, 1),
            "pageviews": int(mets[2]),
        })
    return {"core_web_vitals": rows, "note": "Use Search Console gsc_core_web_vitals for Lighthouse CWV scores.", "start": start, "end": end}


# ---------------------------------------------------------------------------
# Path exploration (user journey)
# ---------------------------------------------------------------------------

@mcp.tool()
def ga4_path_exploration(
    property_id: str,
    start_date: str = None,
    end_date: str = None,
    starting_page: str = None,
    limit: int = 30,
) -> dict:
    """
    Explore user journeys — what pages/events users visit after a starting point.

    Args:
        property_id: GA4 property ID
        start_date: YYYY-MM-DD (default: 30 days ago)
        end_date: YYYY-MM-DD (default: today)
        starting_page: Optional page path to filter starting point (e.g. '/home')
        limit: Max rows (default: 30)
    """
    client = _ga4_data_client()
    ga4_id = f"properties/{property_id}"
    today = date.today()
    start = start_date or (today - timedelta(days=30)).strftime("%Y-%m-%d")
    end = end_date or today.strftime("%Y-%m-%d")

    from google.analytics.data_v1beta import types
    dims = [types.Dimension(name="pagePath"), types.Dimension(name="pageReferrer")]
    filters = None
    if starting_page:
        filters = types.FilterExpression(
            filter=types.Filter(
                field_name="pageReferrer",
                string_filter=types.Filter.StringFilter(value=starting_page, match_type="CONTAINS"),
            )
        )
    request = types.RunReportRequest(
        property=ga4_id,
        dimensions=dims,
        metrics=[
            types.Metric(name="screenPageViews"),
            types.Metric(name="sessions"),
        ],
        date_ranges=[types.DateRange(start_date=start, end_date=end)],
        dimension_filter=filters,
        order_bys=[types.OrderBy(metric=types.OrderBy.MetricOrderBy(metric_name="screenPageViews"), desc=True)],
        limit=limit,
    )
    resp = client.run_report(request)
    rows = []
    for row in resp.rows:
        dims_v = [d.value for d in row.dimension_values]
        mets = [m.value for m in row.metric_values]
        rows.append({
            "page_path": dims_v[0],
            "referrer": dims_v[1],
            "pageviews": int(mets[0]),
            "sessions": int(mets[1]),
        })
    return {"path_exploration": rows, "starting_page": starting_page, "start": start, "end": end}


# ---------------------------------------------------------------------------
# Create GA4 audience
# ---------------------------------------------------------------------------

@mcp.tool()
def ga4_create_audience(
    property_id: str,
    display_name: str,
    description: str = "",
    membership_duration_days: int = 30,
    event_name: str = "purchase",
) -> dict:
    """
    Create a new GA4 audience based on an event trigger.

    Args:
        property_id: GA4 property ID
        display_name: Human-readable audience name
        description: Optional description
        membership_duration_days: How long users stay in the audience (1-540 days)
        event_name: GA4 event that qualifies users for this audience (e.g. 'purchase', 'sign_up')
    """
    admin = _ga4_admin_client()
    from google.analytics.admin_v1alpha import types as admin_types

    audience = admin_types.Audience(
        display_name=display_name,
        description=description,
        membership_duration_days=min(max(1, membership_duration_days), 540),
        filter_clauses=[
            admin_types.AudienceFilterClause(
                clause_type=admin_types.AudienceFilterClause.AudienceClauseType.INCLUDE,
                simple_filter=admin_types.AudienceSimpleFilter(
                    scope=admin_types.AudienceFilterScope.AUDIENCE_FILTER_SCOPE_ACROSS_ALL_SESSIONS,
                    filter_expression=admin_types.AudienceFilterExpression(
                        and_group=admin_types.AudienceFilterExpressionList(
                            filter_expressions=[
                                admin_types.AudienceFilterExpression(
                                    event_filter=admin_types.AudienceEventFilter(
                                        event_name=event_name
                                    )
                                )
                            ]
                        )
                    ),
                ),
            )
        ],
    )
    result = admin.create_audience(parent=f"properties/{property_id}", audience=audience)
    return {
        "audience_name": result.name,
        "display_name": result.display_name,
        "membership_days": result.membership_duration_days,
        "event_trigger": event_name,
    }


# ---------------------------------------------------------------------------
# List custom dimensions
# ---------------------------------------------------------------------------

@mcp.tool()
def ga4_list_custom_dimensions(property_id: str) -> dict:
    """
    List all custom dimensions configured on a GA4 property.

    Args:
        property_id: GA4 property ID
    """
    admin = _ga4_admin_client()
    pager = admin.list_custom_dimensions(parent=f"properties/{property_id}")
    dims = []
    for d in pager:
        dims.append({
            "name": d.name,
            "display_name": d.display_name,
            "description": d.description,
            "scope": d.scope.name if d.scope else "",
            "parameter_name": d.parameter_name,
        })
    return {"custom_dimensions": dims, "count": len(dims)}


# ---------------------------------------------------------------------------
# Compare date ranges
# ---------------------------------------------------------------------------

@mcp.tool()
def ga4_compare_date_ranges(
    property_id: str,
    period1_start: str = None,
    period1_end: str = None,
    period2_start: str = None,
    period2_end: str = None,
    metrics: str = "sessions,newUsers,conversions,purchaseRevenue",
) -> dict:
    """
    Compare key metrics between two date ranges side-by-side.

    Args:
        property_id: GA4 property ID
        period1_start: More recent period start YYYY-MM-DD (default: last 30 days)
        period1_end: More recent period end YYYY-MM-DD (default: yesterday)
        period2_start: Comparison period start (default: 31-60 days ago)
        period2_end: Comparison period end (default: 31 days ago)
        metrics: Comma-separated GA4 metric names (default: sessions,newUsers,conversions,purchaseRevenue)
    """
    client = _ga4_data_client()
    ga4_id = f"properties/{property_id}"
    today = date.today()
    p1s = period1_start or (today - timedelta(days=30)).strftime("%Y-%m-%d")
    p1e = period1_end   or (today - timedelta(days=1)).strftime("%Y-%m-%d")
    p2s = period2_start or (today - timedelta(days=60)).strftime("%Y-%m-%d")
    p2e = period2_end   or (today - timedelta(days=31)).strftime("%Y-%m-%d")

    metric_list = [m.strip() for m in metrics.split(",")]

    from google.analytics.data_v1beta import types

    def _run(start, end):
        req = types.RunReportRequest(
            property=ga4_id,
            dimensions=[],
            metrics=[types.Metric(name=m) for m in metric_list],
            date_ranges=[types.DateRange(start_date=start, end_date=end)],
        )
        r = client.run_report(req)
        if not r.rows:
            return {m: 0.0 for m in metric_list}
        row = r.rows[0]
        return {metric_list[i]: float(row.metric_values[i].value) for i in range(len(metric_list))}

    result = {
        "current": _run(p1s, p1e),
        "comparison": _run(p2s, p2e),
    }

    # Compute deltas
    deltas = {}
    if "current" in result and "comparison" in result:
        for m in metric_list:
            cur = result["current"].get(m, 0)
            prev = result["comparison"].get(m, 0)
            pct = round((cur - prev) / max(prev, 1) * 100, 1) if prev else None
            deltas[m] = {"current": cur, "comparison": prev, "change_pct": pct}

    return {"periods": result, "deltas": deltas, "period1": f"{p1s} to {p1e}", "period2": f"{p2s} to {p2e}"}


# ---------------------------------------------------------------------------
# Channel performance (default channel grouping)
# ---------------------------------------------------------------------------

@mcp.tool()
def ga4_channel_performance(
    property_id: str,
    start_date: str = None,
    end_date: str = None,
    compare_to_previous: bool = True,
) -> dict:
    """
    Return performance breakdown by default channel grouping (Organic Search, Paid Search, Direct, etc.).

    Args:
        property_id: GA4 property ID
        start_date: YYYY-MM-DD (default: 30 days ago)
        end_date: YYYY-MM-DD (default: yesterday)
        compare_to_previous: Also fetch previous 30-day period for comparison (default: True)
    """
    client = _ga4_data_client()
    ga4_id = f"properties/{property_id}"
    today = date.today()
    start = start_date or (today - timedelta(days=30)).strftime("%Y-%m-%d")
    end = end_date or (today - timedelta(days=1)).strftime("%Y-%m-%d")

    from google.analytics.data_v1beta import types
    date_ranges = [types.DateRange(start_date=start, end_date=end, name="current")]
    if compare_to_previous:
        prev_days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
        prev_end = date.fromisoformat(start) - timedelta(days=1)
        prev_start = prev_end - timedelta(days=prev_days - 1)
        date_ranges.append(types.DateRange(
            start_date=prev_start.strftime("%Y-%m-%d"),
            end_date=prev_end.strftime("%Y-%m-%d"),
            name="previous",
        ))

    def _run_period(dr):
        req = types.RunReportRequest(
            property=ga4_id,
            dimensions=[types.Dimension(name="defaultChannelGroup")],
            metrics=[
                types.Metric(name="sessions"),
                types.Metric(name="newUsers"),
                types.Metric(name="conversions"),
                types.Metric(name="purchaseRevenue"),
                types.Metric(name="bounceRate"),
            ],
            date_ranges=[dr],
            order_bys=[types.OrderBy(metric=types.OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)],
        )
        return client.run_report(req)

    rows = []
    for dr in date_ranges:
        resp = _run_period(dr)
        for row in resp.rows:
            dims = [d.value for d in row.dimension_values]
            mets = [m.value for m in row.metric_values]
            rows.append({
                "channel": dims[0],
                "period": dr.name or "current",
                "sessions": int(mets[0]),
                "new_users": int(mets[1]),
                "conversions": float(mets[2]),
                "revenue": float(mets[3]),
                "bounce_rate": round(float(mets[4]) * 100, 1),
            })
    return {"channel_performance": rows, "start": start, "end": end}


# ---------------------------------------------------------------------------
# Site search terms report
# ---------------------------------------------------------------------------

@mcp.tool()
def ga4_site_search_terms(
    property_id: str,
    start_date: str = None,
    end_date: str = None,
    limit: int = 50,
) -> dict:
    """
    Return internal site search terms that users searched within your website.
    Requires GA4 enhanced measurement > site search to be enabled.

    Args:
        property_id: GA4 property ID
        start_date: YYYY-MM-DD (default: 30 days ago)
        end_date: YYYY-MM-DD (default: today)
        limit: Max rows (default: 50)
    """
    client = _ga4_data_client()
    ga4_id = f"properties/{property_id}"
    today = date.today()
    start = start_date or (today - timedelta(days=30)).strftime("%Y-%m-%d")
    end = end_date or today.strftime("%Y-%m-%d")

    from google.analytics.data_v1beta import types
    request = types.RunReportRequest(
        property=ga4_id,
        dimensions=[types.Dimension(name="searchTerm")],
        metrics=[
            types.Metric(name="eventCount"),
            types.Metric(name="sessions"),
        ],
        date_ranges=[types.DateRange(start_date=start, end_date=end)],
        dimension_filter=types.FilterExpression(
            filter=types.Filter(
                field_name="eventName",
                string_filter=types.Filter.StringFilter(value="view_search_results", match_type="EXACT"),
            )
        ),
        order_bys=[types.OrderBy(metric=types.OrderBy.MetricOrderBy(metric_name="eventCount"), desc=True)],
        limit=limit,
    )
    resp = client.run_report(request)
    rows = []
    for row in resp.rows:
        dims = [d.value for d in row.dimension_values]
        mets = [m.value for m in row.metric_values]
        rows.append({"search_term": dims[0], "searches": int(mets[0]), "sessions": int(mets[1])})
    return {"site_search_terms": rows, "count": len(rows), "start": start, "end": end}
