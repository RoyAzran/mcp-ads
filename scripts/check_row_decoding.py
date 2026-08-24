"""A Google Ads row must decode to the fields the query asked for.

_row_to_dict passed `including_default_value_fields=False` to MessageToDict.
protobuf 5.x removed that argument (it is `always_print_fields_with_no_presence`
now), so the call raised TypeError, a bare except swallowed it, and every row
fell through to `{k: str(getattr(row, k)) for k in dir(row)}` -- all 180-odd
resource names the proto declares, stringified, selected or not.

That is not a degraded result, it is a different one. gads_list_budgets returned
31 budgets as 174 KB in which campaign_budget was the string repr of a proto
rather than an object with a name and an amount: past the tool-output limit, and
unusable when it is not. Every read tool in the file goes through here.

Silent because the except was bare and the fallback always succeeds. This check
is the alarm that was missing: it decodes a row built by hand and insists the
result contains what was set and nothing else.
"""
from __future__ import annotations

import json
import os
import sys
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("JWT_SECRET_KEY", "check")
warnings.filterwarnings("ignore")

FAILURES: list[str] = []


def record(label: str, ok: bool, why: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        FAILURES.append(f"{label}{' -- ' + why if why else ''}")


def main() -> int:
    import mcp_server  # noqa: F401
    from tools.google_ads_gads import _row_to_dict
    from google.ads.googleads.v23.resources.types.campaign_budget import CampaignBudget
    from google.ads.googleads.v23.services.types.google_ads_service import GoogleAdsRow

    row = GoogleAdsRow(
        campaign_budget=CampaignBudget(id=123, name="Test budget", amount_micros=50_000_000)
    )
    decoded = _row_to_dict(row)

    print("a row decodes to what was set:")
    record("no decode error", "error" not in decoded, str(decoded)[:200])
    record("the set resource is present", "campaign_budget" in decoded,
           f"got keys {list(decoded)[:8]}")

    budget = decoded.get("campaign_budget")
    record("it is a mapping, not a string repr", isinstance(budget, dict),
           f"got {type(budget).__name__} -- this is the dir() fallback's signature")
    if isinstance(budget, dict):
        record("the selected fields survived",
               str(budget.get("name")) == "Test budget" and str(budget.get("id")) == "123",
               f"got {budget}")
        record("snake_case names are preserved", "amount_micros" in budget,
               "the tools index rows by snake_case; camelCase would read as absent")

    print()
    print("unset resources are not invented:")
    record("only the resource that was set appears", list(decoded.keys()) == ["campaign_budget"],
           f"got {len(decoded)} keys; the fallback produces 180+")
    size = len(json.dumps(decoded))
    print(f"  one row serialises to {size} bytes")
    record("a single row stays small", size < 1000,
           f"{size} bytes/row means the fallback is back; 31 rows hit 174 KB")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("row decoding: rows carry the selected fields, not a proto dump")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
