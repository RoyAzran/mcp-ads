"""No auto-generated Google Ads stub may be callable.

tools/google_ads_gads.py was generated from one placeholder body, copied under
every tool name: declare the parameters the name implies, read none of them,
run `SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status FROM
ad_group_ad`, return {"success": True}. 101 functions still carry that body.

Most were hidden via _BROKEN_ACTIONS. Thirty were not, and the list-shaped ones
were the bad kind of broken: gads_list_budgets did not fail, it returned ad rows
under the key "budgets" with success set, and an agent asked "what are my
budgets" would act on ad IDs. Only gads_search_geo_targets ever showed up in
telemetry as a failure, and only because it happened to demand a customer_id
first -- the silent ones left no trace at all.

Fourteen have since been implemented against their real resources. The rest are
hidden, because a mutate or a dedicated service is not something a GAQL SELECT
can be rewritten into.

This check fails if a function still carrying the placeholder body is reachable,
whether because a new one was generated or because a hidden one was unhidden
without being implemented.
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("JWT_SECRET_KEY", "check")

PLACEHOLDER = (
    "SELECT ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status "
    "FROM ad_group_ad"
)

# Implemented for real on 2026-08-23. Listed so that if one is ever reverted to
# the placeholder, this check names it instead of silently passing.
IMPLEMENTED = {
    "gads_get_asset_performance",
    "gads_get_budget_pacing",
    "gads_get_carrier_constants",
    "gads_list_assets",
    "gads_list_audiences",
    "gads_list_bidding_strategies",
    "gads_list_budgets",
    "gads_list_conversion_actions",
    "gads_list_experiments",
    "gads_list_recommendations",
    "gads_list_topic_constants",
    "gads_list_user_lists",
    "gads_run_custom_report",
    "gads_search_geo_targets",
}


def stub_names() -> set[str]:
    """Functions whose body is still the generated placeholder."""
    src = (ROOT / "tools" / "google_ads_gads.py").read_text(encoding="utf-8")
    found = set()
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("gads_"):
            continue
        # Tools that genuinely query ad_group_ad are supposed to contain this
        # string; the placeholder is only a tell under an unrelated name.
        if "ad_group_ad" in node.name:
            continue
        if PLACEHOLDER in (ast.get_source_segment(src, node) or ""):
            found.add(node.name)
    return found


def main() -> int:
    import mcp_server  # noqa: F401 - registers every tool
    import tool_registry

    stubs = stub_names()
    hidden = tool_registry._HIDDEN_BY_DEFAULT
    failures: list[str] = []

    print(f"placeholder bodies found: {len(stubs)}")

    reachable = sorted(stubs - hidden)
    if reachable:
        failures.append(
            f"{len(reachable)} stub(s) are callable: {reachable}. "
            "Implement them against their real resource, or add them to "
            "_BROKEN_ACTIONS in tool_registry.py."
        )
    print(f"  {'PASS' if not reachable else 'FAIL'}  every placeholder is hidden")

    regressed = sorted(IMPLEMENTED & stubs)
    if regressed:
        failures.append(
            f"{regressed} went back to the placeholder body after being implemented."
        )
    print(f"  {'PASS' if not regressed else 'FAIL'}  implemented tools stayed implemented")

    # An implemented tool that is also hidden is a tool nobody can reach, which
    # is almost certainly a mistake rather than a decision.
    buried = sorted(IMPLEMENTED & hidden)
    if buried:
        failures.append(f"{buried} are implemented but still hidden by _BROKEN_ACTIONS.")
    print(f"  {'PASS' if not buried else 'FAIL'}  implemented tools are reachable")

    print()
    if failures:
        print(f"{len(failures)} FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("gads stubs: no placeholder-bodied tool is reachable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
