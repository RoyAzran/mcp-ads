"""A video ad in a lead-gen ad set must carry its lead form.

Meta's rejection for this is "missing lead form" -- it names neither the
creative nor the link, so it reads as a problem with the form or the Page.
On 2026-08-17 a client spent a day re-verifying a form that was fine the whole
time (active, 67 leads) and re-uploading eight videos that
were also fine, because the actual fault was in the creative: our video tools
could only put a URL in the call-to-action, never a form id.

Two things are pinned here:

  1. the video creative tools accept a lead_gen_form_id and put it in the CTA
     value, so a video lead ad can be built at all; and
  2. the pre-flight guard refuses a lead-gen ad set whose creative's CTA has no
     form, and says which tool to rebuild it with -- so the next person gets a
     sentence naming the real problem instead of Meta's.

Neither is visible from the outside: an ad that is never created leaves nothing
behind to inspect.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import warnings

from cryptography.fernet import Fernet

warnings.filterwarnings("ignore")
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite:///./check_lead_form.db")
os.environ.setdefault("JWT_SECRET_KEY", "ci-check-secret")
os.environ.setdefault("FERNET_KEY", Fernet.generate_key().decode())

FORM = "111222333444555"
FAILURES: list[str] = []


def record(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -> ' + detail) if (detail and not ok) else ''}")
    if not ok:
        FAILURES.append(f"{label}: {detail}")


def main() -> int:
    import tools.meta_ads as m

    def guard(promoted: dict, kind: str, value: dict):
        spec = {"page_id": "1", kind: {"call_to_action": {"type": "SIGN_UP", "value": value}}}

        def fake_get(path, params=None):
            if path == "adset":
                return {"campaign_id": "c", "promoted_object": promoted}
            return {"object_story_spec": spec}

        real, m._get = m._get, fake_get
        try:
            return m._check_creative_adset_compatibility("adset", "creative")
        finally:
            m._get = real

    lead = {"lead_gen_form_id": FORM}

    print("a lead-gen ad set refuses a creative with no form on the CTA:")
    for kind in ("video_data", "link_data"):
        err = (guard(lead, kind, {"link": "https://example.com"}) or {}).get("error", "")
        record(f"{kind} carrying only a link is caught", bool(err),
               "Meta would answer 'missing lead form' and name nothing useful")
        if err:
            record(f"  {kind}: the message names the form id", FORM in err)
            record(f"  {kind}: it says the form is probably fine",
                   "form is almost certainly fine" in err,
                   "without this people re-check the form, which is what happened")
            record(f"  {kind}: it names the tool to rebuild with",
                   ("lead_gen_form_id=" in err) if kind == "video_data" else ("form_id=" in err))

    print("\nwhat should pass, passes:")
    record("a video creative carrying the form",
           guard(lead, "video_data", {"lead_gen_form_id": FORM}) is None,
           "the fixed path must not be blocked")
    record("a non-lead ad set is untouched",
           guard({"pixel_id": "1"}, "video_data", {"link": "https://x.com"}) is None,
           "an ordinary traffic ad would be blocked for no reason")

    print("\nthe video tools can actually attach a form:")
    sent: dict = {}
    m._resolve_meta_account = lambda a="": "act_1"
    m.require_editor = lambda *a, **k: None
    m._attach_video_thumbnail = lambda vd, *a, **k: {"success": True}
    m._post = lambda path, data=None: (sent.update(data=data), {"id": "c1"})[1]

    m.meta_ads_create_video_ad_creative(
        name="n", page_id="P", video_id="V", message="m", lead_gen_form_id=FORM)
    cta = json.loads(sent["data"]["object_story_spec"])["video_data"].get("call_to_action", {})
    record("create_video_ad_creative puts the form in the CTA",
           (cta.get("value") or {}).get("lead_gen_form_id") == FORM,
           f"got {cta}")

    m.meta_ads_create_video_ad_creative(
        name="n", page_id="P", video_id="V", message="m",
        call_to_action_type="LEARN_MORE", call_to_action_link="https://example.com")
    cta = json.loads(sent["data"]["object_story_spec"])["video_data"].get("call_to_action", {})
    record("and still supports a plain link when no form is given",
           (cta.get("value") or {}).get("link") == "https://example.com",
           f"ordinary video ads must be unaffected; got {cta}")

    # Meta refuses a new ad set outright unless targeting_automation states an
    # Advantage Audience choice (error_subcode 1870227). Not sending it is not a
    # neutral default, it is a hard failure -- every ad set creation broke,
    # found only by creating one against the real API on 2026-08-18.
    print("")
    print("every ad set states an Advantage Audience choice:")
    sent: dict = {}
    m._post = lambda path, data=None: (sent.update(data=data or {}), {"id": "as_1"})[1]

    def targeting_of(**kw):
        sent.clear()
        m.meta_ads_create_adset(name="n", campaign_id="c", **kw)
        return json.loads(sent["data"]["targeting"])

    geo = json.dumps({"geo_locations": {"countries": ["IL"]}})
    t1 = targeting_of(targeting=geo)
    record("plain targeting still carries the flag",
           (t1.get("targeting_automation") or {}).get("advantage_audience") == 0,
           "Meta rejects the ad set with 'Advantage Audience Flag Required'")
    record("the geo targeting survives untouched",
           t1.get("geo_locations", {}).get("countries") == ["IL"])
    t2 = targeting_of(targeting=geo, advantage_audience=True)
    record("advantage_audience=True sends 1",
           (t2.get("targeting_automation") or {}).get("advantage_audience") == 1)
    t3 = targeting_of(targeting=json.dumps({"geo_locations": {"countries": ["IL"]},
                                            "targeting_automation": {"advantage_audience": 1}}))
    record("an explicit choice is not overwritten",
           (t3.get("targeting_automation") or {}).get("advantage_audience") == 1,
           "the caller asked for Advantage Audience and we turned it off")
    t4 = targeting_of(targeting="")
    record("blank targeting still carries the flag",
           (t4.get("targeting_automation") or {}).get("advantage_audience") == 0)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("lead ad forms: video lead ads can be built, and a formless one is refused with a reason")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
