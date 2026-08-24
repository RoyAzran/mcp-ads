"""Location targeting has to be wrong loudly, never quietly.

Meta's geo targeting has two traps that produce a *valid* spec reaching an
audience of exactly zero. No error, no warning, no rejected call -- the ad set
is created, it goes live, and it delivers to nobody while the budget sits there
looking like it is working. Both were measured against Meta's own
delivery_estimate on a real ad account:

  a city with radius 12 km   -> 0-0        (16 km -> 2,600,000-3,100,000)
  exclusions nested inside
  geo_locations              -> 0-0        (as a sibling -> 6,900,000-8,100,000)

Neither is discoverable from the response. This check pins the two behaviours
that prevent them, plus the type->key mapping they both rest on, because a
plausible-looking "simplification" of any of it reintroduces a bug whose only
symptom is an ad that does not run.

It also pins the WhatsApp performance goal. Meta's ad set editor sets
'maximise conversations' the moment WhatsApp is chosen as the destination; our
generic default is LINK_CLICKS, which buys taps that open WhatsApp and go no
further. That one is not silent in the same way -- but it is invisible in the
API response, which is close enough.
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

os.environ.setdefault("DATABASE_URL", "sqlite:///./check_geo.db")
os.environ.setdefault("JWT_SECRET_KEY", "ci-check-secret")
os.environ.setdefault("FERNET_KEY", Fernet.generate_key().decode())

FAILURES: list[str] = []


def record(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -> ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(f"{label}: {detail}")


def main() -> int:
    from tools.meta_ads import meta_ads_build_geo_targeting as build

    print("a radius Meta would silently ignore is refused:")
    for km, kind, should_fail in (
        ("12", "named place", True),
        ("15.9", "named place", True),
        ("16", "named place", False),
        ("80", "named place", False),
        ("200", "named place", True),
    ):
        out = json.loads(build(location_keys="1014712", radius_km=km))
        record(f"city at {km} km {'refused' if should_fail else 'allowed'}",
               bool(out.get("error")) == should_fail,
               out.get("error", "")[:70] or "accepted")

    print("\nbut a pin may be as tight as the user actually wants:")
    out = json.loads(build(custom_pins='[{"lat":32.08,"lng":34.78,"radius_km":3}]'))
    record("3 km pin allowed", not out.get("error"), out.get("error", "")[:70])
    record("the pin carries a unit",
           '"distance_unit": "kilometer"' in (out.get("geo_locations") or ""))

    print("\nexclusions stay a sibling of geo_locations:")
    out = json.loads(build(countries="IL", excluded_location_keys="city:1014712"))
    targeting = json.loads(out["targeting"])
    record("excluded_geo_locations is top-level", "excluded_geo_locations" in targeting)
    record("and is NOT nested inside geo_locations",
           "excluded_geo_locations" not in targeting.get("geo_locations", {}),
           "nested here it matches nobody")

    print("\nplace types map to the right geo_locations key:")
    out = json.loads(build(location_keys="city:1,zip:2,region:3,neighborhood:4"))
    geo = json.loads(out["geo_locations"])
    for singular, plural in (("city", "cities"), ("zip", "zips"),
                             ("region", "regions"), ("neighborhood", "neighborhoods")):
        record(f"{singular} -> {plural}", plural in geo)
    record("a radius is not attached to a region",
           "radius" not in json.loads(build(location_keys="region:3", radius_km="20"))
           .get("geo_locations", ""),
           "Meta rejects a radius on a region")

    print("\nnothing silently targets nowhere:")
    record("no locations is an error", bool(json.loads(build()).get("error")))
    record("an unknown type is reported, not dropped",
           bool(json.loads(build(location_keys="banana:1")).get("warnings")))

    print("\nWhatsApp ad sets get the conversations goal:")
    import inspect

    from tools import meta_ads

    src = inspect.getsource(meta_ads.meta_ads_create_adset)
    record("a messaging destination overrides LINK_CLICKS",
           'optimization_goal = "CONVERSATIONS"' in src and "WHATSAPP" in src)
    record("and says so in the response", 'result["note"] = adjusted' in src)
    sig = inspect.signature(meta_ads.meta_ads_create_whatsapp_adset)
    record("meta_ads_create_whatsapp_adset defaults to CONVERSATIONS",
           sig.parameters["optimization_goal"].default == "CONVERSATIONS")
    record("...and to the WhatsApp destination",
           sig.parameters["destination_type"].default == "WHATSAPP")
    record("...and creates it paused",
           sig.parameters["status"].default == "PAUSED")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("geo targeting: tight radii and nested exclusions refused, WhatsApp goal defaulted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
