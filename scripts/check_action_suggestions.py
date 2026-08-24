"""A wrong tool name must come back with the right one attached.

The dispatcher takes an action name and looks it up. When the name does not
exist the KeyError is the model's only feedback, so the suggestion in it is
what decides whether the next attempt succeeds or the run stalls.

The original rule was `action.lower() in name.lower()`: a suggestion appeared
only when the guess was a strict substring of a real name. Wrong guesses are
almost never substrings -- they are longer, or reordered, or add a verb the
real name omits. meta_ads_get_adset wants meta_ads_adsets; meta_ads_list_pages
wants meta_ads_pages; neither contains the other. So the branch never fired and
the error said only "not found".

Two weeks of MCP telemetry (to 2026-08-23) showed 11 of 19 distinct failing
tool names were inventions of exactly this shape, every one with an obvious
real counterpart the caller never got told about.

The names below are those 11, verbatim from telemetry. Each must produce a
suggestion, and the intended tool must be in the top three -- a list of five
near-misses that omits the right answer is worse than useless, because it reads
as confirmation that the tool does not exist.
"""
from __future__ import annotations

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


# (guessed name, the tool it was reaching for)
GUESSES = [
    ("meta_ads_get_adset", "meta_ads_adsets"),
    ("meta_ads_get_adset_details", "meta_ads_adsets"),
    ("meta_ads_get_account_overview", "meta_ads_overview"),
    ("meta_ads_create_creative", "meta_ads_create_ad_creative"),
    ("meta_ads_update_campaign", "meta_ads_campaigns"),
    ("meta_ads_create_whatsapp_ad", "meta_ads_create_whatsapp_adset"),
    ("meta_ads_get_ad_preview", "meta_ads_preview_ad_creative"),
    ("meta_ads_get_creative_details", "meta_ads_ad_creatives"),
    ("meta_ads_get_creative", "meta_ads_ad_creative"),
    ("meta_ads_list_pages", "meta_ads_pages"),
    ("meta_ads_get_ad_creative", "meta_ads_ad_creative"),
]


def main() -> int:
    import mcp_server  # noqa: F401 - registers every tool
    import tool_registry

    actions = tool_registry._get_registry()["meta_ads_action"]

    print("every name the model actually invented gets pointed somewhere real:")
    for guess, wanted in GUESSES:
        assert guess not in actions, f"{guess} exists now -- retire this row"
        assert wanted in actions, f"{wanted} no longer exists -- update this row"
        top = tool_registry._suggest_actions(guess, actions, 3)
        record(f"{guess} -> {wanted}", wanted in top,
               f"got {top or 'nothing at all'}")

    print()
    print("the suggestion reaches the caller, not just the ranking function:")
    try:
        tool_registry.find_action("meta_ads_action", "meta_ads_list_pages")
        record("unknown action raises", False, "it returned instead")
    except KeyError as exc:
        message = str(exc)
        record("the error carries a suggestion", "Did you mean" in message)
        record("and names the real tool", "meta_ads_pages" in message)
        record("and still points at list_actions", "list_actions" in message,
               "the suggestion is a shortcut, not a replacement for discovery")

    print()
    print("the category name tolerates the suffix being dropped:")
    bare = tool_registry.list_actions("meta_ads", search="pages")
    record("list_actions('meta_ads') resolves", bare["category"] == "meta_ads_action",
           "list_actions is the tool a stuck caller reaches for, and it raised "
           "on the most natural way to name the category")
    record("find_action('meta_ads', ...) resolves",
           tool_registry.find_action("meta_ads", "meta_ads_pages").name == "meta_ads_pages")

    for wrong in ("meta", "ads", "facebook"):
        try:
            tool_registry.list_actions(wrong)
            record(f"'{wrong}' is not silently accepted", False,
                   "a vague name must not be routed into some platform's tools")
        except KeyError:
            record(f"'{wrong}' still raises", True)

    print()
    print("a name with nothing in common stays quiet:")
    record("no suggestion for unrelated input",
           not tool_registry._suggest_actions("zzzz_qqqq", actions),
           "five arbitrary tools would read as five real options")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("action suggestions: invented tool names resolve to the real ones")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
