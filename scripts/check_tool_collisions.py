"""Guard against MCP tools silently overwriting each other.

FastMCP keeps one dict of tools keyed by name. Registering a second tool under
a name that already exists logs `WARNING Tool already exists: <name>` and then
*replaces* the first one -- no error, no failed import. The target already has
132 such collisions today, so the warning alone is clearly not enough to notice.

That matters most when merging a large external tool set (the WordPress port
adds ~1800 names): a single accidental duplicate would silently shadow an
existing Google Ads or Meta tool and the only symptom would be a tool that
quietly does the wrong thing.

This script reports:
  * how many tools are registered vs how many `@mcp.tool()` defs exist on disk
  * the exact names that lost the race (defined more than once)
  * any name registered from a tools.wordpress.* module that is also defined by
    a non-WordPress module (a genuine cross-platform shadow)

Run directly for a report, or via test_agency_os.py for the CI assertion.
"""
from __future__ import annotations

import ast
import pathlib
import sys
from collections import defaultdict

# Committed baseline. Update deliberately -- a change here should be explained
# in the commit message, because it means the tool surface moved.
EXPECTED_REGISTERED = 3176  # 3161 actual on the prior commit, + 15: seven
# meta_ad_library_* tools, seven google_ads_transparency_* tools, and the
# competitor_ads_gallery widget tool. The written-down 3160 was one behind the
# 3161 the tree actually registered, so this also corrects that drift.
# 3157 prior + meta_ads_find_location,
# meta_ads_build_geo_targeting and meta_ads_create_whatsapp_adset -- targeting
# anywhere smaller than a country needed a key lookup that did not exist, and
# WhatsApp ad sets needed the defaults Meta's own editor applies.
# defs-minus-registered. Note this conflates two things: names genuinely
# overwritten by a later registration (6 today, listed in the report) and defs
# in modules nobody imports. It fell 132 -> 68 purely because wiring up
# tools/gtm.py moved its 64 tools from the second bucket to registered. The
# bulk of what remains is tools/sheets.py, which is still not imported --
# deliberately, since its Drive scope is restricted by Google.
# 68 -> 66: the two media tools moved out of tools/ into the MCP Apps
# extension (media_app.py), so they are no longer defined in a scanned
# module and no longer counted here. Tool count itself is unchanged.
# 66 -> 67: media_upload_result came back to tools/media.py as an ordinary
# @mcp.tool(). It must NOT be an Apps tool -- Apps.tool() requires a
# resource_uri, and binding the drop zone to a result that has no upload slot
# left the widget mounted and waiting forever. The def is scanned again while
# the registered count is unchanged, so the difference grows by one.
# 66 -> 64. Two moves, and they are not the same kind, so both are written down.
# The competitor-research families add 14 @mcp.tool() defs under tools/, which
# lands on both sides of defs-minus-registered and cancels; competitor_ads_gallery
# is registered through the MCP Apps extension without a def under tools/, so it
# raises the registered count only and takes the difference down by one. The
# second one is a correction, not a change: the checked-in numbers were already
# one behind reality before this branch (registered was 3161, not 3160), so the
# stale off-by-one is being fixed at the same time rather than carried forward.
EXPECTED_DUPLICATE_DEFS = 64

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _iter_tool_defs() -> list[tuple[str, str]]:
    """Return (tool_name, module_path) for every `@mcp.tool()` def under tools/."""
    found: list[tuple[str, str]] = []
    for path in sorted((_REPO_ROOT / "tools").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        module = path.relative_to(_REPO_ROOT).with_suffix("").as_posix().replace("/", ".")
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                target = decorator.func if isinstance(decorator, ast.Call) else decorator
                # matches both `@mcp.tool()` and `@mcp.tool`
                if isinstance(target, ast.Attribute) and target.attr == "tool":
                    found.append((node.name, module))
                    break
    return found


def collect() -> dict:
    sys.path.insert(0, str(_REPO_ROOT))
    import mcp_server  # noqa: F401  -- import side effect registers every tool
    from mcp_instance import mcp

    registered = mcp._tool_manager._tools
    defs = _iter_tool_defs()

    by_name: dict[str, list[str]] = defaultdict(list)
    for name, module in defs:
        by_name[name].append(module)

    duplicates = {name: mods for name, mods in by_name.items() if len(mods) > 1}

    # A WordPress tool shadowing (or being shadowed by) another platform is the
    # failure this guard exists to prevent.
    cross_platform: dict[str, list[str]] = {}
    for name, mods in duplicates.items():
        wp = [m for m in mods if m.startswith("tools.wordpress")]
        other = [m for m in mods if not m.startswith("tools.wordpress")]
        if wp and other:
            cross_platform[name] = mods

    return {
        "registered": len(registered),
        "defined": len(defs),
        "duplicate_names": duplicates,
        "duplicate_def_count": len(defs) - len(registered),
        "cross_platform": cross_platform,
    }


def main() -> int:
    result = collect()
    print(f"registered tools      : {result['registered']} (expected {EXPECTED_REGISTERED})")
    print(f"@mcp.tool() defs      : {result['defined']}")
    print(f"overwritten by name   : {result['duplicate_def_count']} (expected {EXPECTED_DUPLICATE_DEFS})")

    if result["duplicate_names"]:
        print(f"\nnames defined more than once ({len(result['duplicate_names'])}):")
        for name, mods in sorted(result["duplicate_names"].items()):
            print(f"  {name}: {', '.join(mods)}")

    if result["cross_platform"]:
        print("\nCROSS-PLATFORM SHADOWING -- a WordPress tool collides with another platform:")
        for name, mods in sorted(result["cross_platform"].items()):
            print(f"  {name}: {', '.join(mods)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
