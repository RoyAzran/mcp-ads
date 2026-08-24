"""The competitor gallery widget: one extension, escaped ad copy, no dead ends.

Same three failure modes as the creative gallery, plus two this one adds.

The shared ones: a second Apps() stops the server booting at all; text that
comes from outside goes into the page and must be escaped; loading="lazy"
leaves cards below the fold blank, which in a chat reads as broken.

The two that are specific here. First, the text is worse than a revised prompt
-- it is ad copy written by a competitor, arriving from Meta and Google, and it
is the most obviously attacker-controlled string anywhere in this product.
Second, this widget merges two sources, and either can be down: if one dead
source empties the grid, the answer becomes "this competitor has no ads", which
is a wrong answer rather than a missing one. The payload has to carry the other
source's ads and say what happened to the first.
"""
from __future__ import annotations

import os
import pathlib
import sys
import warnings

warnings.filterwarnings("ignore")
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("JWT_SECRET_KEY", "ci-check-secret")
os.environ.setdefault("DATABASE_URL", "sqlite:///./check_competitor.db")

FAILURES: list[str] = []


def record(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -> ' + detail) if (detail and not ok) else ''}")
    if not ok:
        FAILURES.append(f"{label}: {detail}")


def main() -> int:
    import competitor_ads_app

    html = competitor_ads_app.competitor_gallery_html()
    source = (ROOT / "competitor_ads_app.py").read_text(encoding="utf-8")

    print("only one UI extension is ever registered:")
    record("the gallery extends an Apps it is handed",
           "def register_competitor_gallery(apps: Apps)" in source,
           "building a second Apps() stops the server booting at all")
    record("it does not construct its own", "Apps()" not in source,
           "'Extension is already registered' -- the container never starts")
    instance = (ROOT / "mcp_instance.py").read_text(encoding="utf-8")
    record("the server threads it through the one extension",
           "register_competitor_gallery(register_creative_gallery(build_media_apps()))" in instance,
           "both galleries and the drop zone share a single Apps")

    print("")
    print("a competitor cannot inject markup into the page:")
    record("text goes through an escape function", "function esc(" in html)
    record("it escapes the four that matter",
           all(s in html for s in ('"&amp;"', '"&lt;"', '"&gt;"', '"&quot;"')))
    for field in ("ad.headline", "ad.body", "ad.cta", "ad.advertiser",
                  "ad.thumbnail", "ad.format"):
        record(f"{field} is escaped where it is used", f"esc({field})" in html)
    record("the outbound link is escaped too", "esc(link)" in html)
    record("notes from upstream are escaped", "esc(n)" in html)

    print("")
    print("the ads actually appear:")
    record("nothing is lazy-loaded", 'loading="lazy"' not in html,
           "cards below the fold stay blank, which reads as broken in a chat")
    record("both payload shapes are handled",
           "window.openai" in html and "openai:set_globals" in html,
           "hosts differ; handling one renders nothing on the other")

    print("")
    print("a host with no widget is no worse off than before:")
    record("the payload carries the ads as text",
           "say_to_user_no_widget" in source)
    record("the tool tells the model to pass them on",
           "pass those on" in source or "Pass those on" in source)

    print("")
    print("one dead source does not become a wrong answer:")
    payload = competitor_ads_app.competitor_gallery_payload("", "both", "US", 5)
    record("a missing brand is explained, not empty",
           bool(payload.get("notes")) and bool(payload.get("say_to_user_no_widget")))
    record("each source is collected separately", "def _collect(" in source)
    record("a source that raises becomes a note, not a traceback",
           "except Exception as exc:" in source and "notes.append" in source,
           "one dead archive would otherwise empty the whole grid")
    record("the widget renders those notes", 'data.notes' in html)

    print("")
    print("the coverage limit reaches the model:")
    record("the tool docstring names the EU/UK restriction",
           "EU and UK" in source and "notes" in source,
           "otherwise an empty Meta half reads as 'they do not advertise'")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("competitor gallery: one extension, escaped ad copy, no dead ends")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
