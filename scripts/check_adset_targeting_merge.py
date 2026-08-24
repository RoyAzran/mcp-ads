"""Check that a partial ad set update cannot wipe targeting it did not mention.

Meta's `targeting` field is replace-not-merge: whatever is POSTed becomes the
whole spec. meta_ads_update_adset_targeting used to build that object out of
only the supplied fields, so:

  * passing advantage_audience with no targeting sent
    {"targeting_automation": {...}} as the entire spec. Reproduced on
    2026-08-11 against ad set 52542305410210 -- Meta answered "A location is
    missing", blame_field_specs [["targeting"]].
  * passing any targeting subset dropped everything outside it. That one Meta
    *accepts*, so a live ad set silently loses its locales, interests,
    exclusions and custom audiences and nothing anywhere says so.

The second is the dangerous one and it leaves no trace, which is the whole
reason for this file. Everything here is local -- the Graph calls are stubbed,
so what is under test is the payload the tool would have sent.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import warnings

from cryptography.fernet import Fernet

warnings.filterwarnings("ignore")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./check_adset_targeting.db")
os.environ.setdefault("JWT_SECRET_KEY", "ci-check-secret")
os.environ.setdefault("FERNET_KEY", Fernet.generate_key().decode())
os.environ.setdefault("META_AD_ACCOUNT_ID", "act_120330000000000001")

FAILURES: list[str] = []

# A realistic live ad set: geo, locales, interests, an exclusion, and
# Advantage+ already switched on.
CURRENT = {
    "geo_locations": {"countries": ["IL"], "cities": [{"key": "2588", "name": "Tel Aviv"}]},
    "locales": [6, 12],
    "age_min": 30,
    "age_max": 55,
    "flexible_spec": [{"interests": [{"id": "6003107902433", "name": "Marketing"}]}],
    "excluded_custom_audiences": [{"id": "77777"}],
    "targeting_automation": {"advantage_audience": 1, "individual_setting": {"age": 1}},
}


def record(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -> ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(f"{label}: {detail}")


def main() -> int:
    from tools import meta_ads

    sent: dict = {}
    reads: list = []

    def fake_get(path, params=None, access_token=""):
        reads.append(path)
        return {"id": path, "targeting": json.loads(json.dumps(CURRENT))}

    def fake_post(path, data=None):
        sent.clear()
        sent.update(data or {})
        return {"success": True, "id": path}

    meta_ads._get = fake_get
    meta_ads._post = fake_post
    meta_ads.require_editor = lambda *a, **k: None
    meta_ads._resolve_meta_account = lambda account_id="": "act_120330000000000001"

    call = meta_ads.mcp._tool_manager._tools["meta_ads_update_adset_targeting"].fn

    def targeting_sent() -> dict:
        return json.loads(sent["targeting"]) if "targeting" in sent else {}

    # The exact reproduction: no targeting, only unrelated fields.
    print("the reported call (frequency_control_specs + advantage_audience, no targeting):")
    sent.clear()
    call(adset_id="52542305410210",
         frequency_control_specs='[{"event":"IMPRESSIONS","interval_days":7,"max_frequency":3}]',
         advantage_audience=True)
    t = targeting_sent()
    record("geo_locations survives", t.get("geo_locations") == CURRENT["geo_locations"],
           json.dumps(t.get("geo_locations")))
    record("locales survive", t.get("locales") == [6, 12], str(t.get("locales")))
    record("interests survive", t.get("flexible_spec") == CURRENT["flexible_spec"])
    record("exclusions survive", t.get("excluded_custom_audiences") == [{"id": "77777"}])
    record("advantage_audience applied",
           t.get("targeting_automation", {}).get("advantage_audience") == 1)
    record("sibling automation flags survive",
           t.get("targeting_automation", {}).get("individual_setting") == {"age": 1})
    record("the unrelated field still went", "frequency_control_specs" in sent)

    # The silent case: a partial targeting spec Meta would happily accept.
    print("\na partial targeting spec (geo_locations only):")
    sent.clear()
    call(adset_id="52542305410210", targeting='{"geo_locations":{"countries":["US"]}}')
    t = targeting_sent()
    record("geo replaced outright, so narrowing works",
           t.get("geo_locations") == {"countries": ["US"]}, json.dumps(t.get("geo_locations")))
    record("locales NOT wiped", t.get("locales") == [6, 12], str(t.get("locales")))
    record("interests NOT wiped", t.get("flexible_spec") == CURRENT["flexible_spec"])
    record("age NOT wiped", t.get("age_min") == 30 and t.get("age_max") == 55)
    record("advantage_audience untouched",
           t.get("targeting_automation", {}).get("advantage_audience") == 1)

    # advantage_audience is tri-state: unset must not turn a live setting off.
    print("\nadvantage_audience is tri-state:")
    sent.clear()
    call(adset_id="52542305410210", name="Renamed")
    record("unset sends no targeting at all", "targeting" not in sent, json.dumps(list(sent)))
    record("the rename still went", sent.get("name") == "Renamed")

    sent.clear()
    call(adset_id="52542305410210", status="PAUSED", advantage_audience=None)
    record("explicit None sends no targeting", "targeting" not in sent)

    sent.clear()
    call(adset_id="52542305410210", advantage_audience=False)
    record("explicit False does disable it",
           targeting_sent().get("targeting_automation", {}).get("advantage_audience") == 0)
    record("...without wiping geo", targeting_sent().get("geo_locations") == CURRENT["geo_locations"])

    # A failed read must not fall back to the destructive path.
    print("\nwhen the current targeting cannot be read:")
    meta_ads._get = lambda path, params=None, access_token="": {"error": {"message": "nope"}}
    sent.clear()
    out = json.loads(call(adset_id="52542305410210", advantage_audience=True))
    record("nothing is sent", not sent, json.dumps(list(sent)))
    record("and it says why", "error" in out, str(out.get("error"))[:70])
    meta_ads._get = fake_get

    # The red herring: a targeting failure on a CBO campaign must not be
    # explained as a budget problem.
    print("\nthe CBO hint is gated on the error being about a budget:")

    def get_for_explain(path, params=None, access_token=""):
        if path == "234":
            return {"id": "234", "name": "Sales", "daily_budget": "50000"}
        return {"id": path, "name": "Ad set", "campaign_id": "234", "effective_status": "ACTIVE"}

    meta_ads._get = get_for_explain
    targeting_err = {"error": {"message": "Invalid parameter", "code": 100,
                               "error_subcode": 1885364,
                               "error_user_title": "A location is missing",
                               "error_user_msg": "Add at least one location or choose a custom audience.",
                               "blame_field_specs": [["targeting"]]}}

    out = meta_ads._explain_adset_edit("6712", json.loads(json.dumps(targeting_err)),
                                       written=["targeting", "frequency_control_specs"])
    why = " ".join(out.get("why", []))
    record("no budget red herring", "budget optimisation" not in why, why[:90])
    record("Meta's own message is surfaced", "A location is missing" in why)
    record("the blamed field is named", "targeting" in why)

    budget_err = {"error": {"message": "Invalid parameter", "code": 100,
                            "blame_field_specs": [["daily_budget"]]}}
    out = meta_ads._explain_adset_edit("6712", json.loads(json.dumps(budget_err)),
                                       written=["daily_budget"])
    why = " ".join(out.get("why", []))
    record("budget failure still gets the CBO hint", "budget optimisation" in why, why[:90])

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("adset targeting: partial updates preserve what they did not mention")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
