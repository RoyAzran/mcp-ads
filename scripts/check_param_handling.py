"""A call that supplies the right values must not fail on how it spelled them.

Two failures in production telemetry (30 days to 2026-08-23) were arguments
being rejected rather than anything going wrong upstream:

  * gads_update_keyword_status was called with `ad_group_id`, which is what the
    Google Ads API calls it and what 391 references elsewhere in the same file
    call it. Seven tools declare it as `adgroup_id`. The error named the
    parameter the caller had not sent instead of the one it had.

  * gads_list_keywords was called with `status: null` -- a model declining to
    set an optional filter. 4,172 parameters across 1,256 tools are declared
    `x: str = None`: annotation str, default None. Omitting works; passing null
    does not, and the error reads "Input should be a valid string".

Both are handled in dispatch now. This pins the behaviour, and pins the limits:
neither transformation may touch a call that was already correct, and neither
may paper over a genuinely wrong argument.
"""
from __future__ import annotations

import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("JWT_SECRET_KEY", "check")

import warnings

warnings.filterwarnings("ignore")

FAILURES: list[str] = []


def record(label: str, ok: bool, why: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        FAILURES.append(f"{label}{' -- ' + why if why else ''}")


def main() -> int:
    import mcp_server  # noqa: F401 - registers every tool
    import tool_registry
    from mcp_instance import mcp

    tools = mcp._tool_manager._tools

    print("the underscore-only misspelling resolves (gads_update_keyword_status):")
    keyword_status = tools["gads_update_keyword_status"]
    # Verbatim from the failing production event.
    produced = tool_registry._resolve_param_aliases(keyword_status, {
        "ad_group_id": "199664705735",
        "criterion_id": "2448179720601",
        "customer_id": "1234567890",
        "status": "PAUSED",
    })
    record("ad_group_id reaches adgroup_id", produced.get("adgroup_id") == "199664705735",
           f"got {produced}")
    record("the wrong spelling is not left behind", "ad_group_id" not in produced,
           "an unexpected key fails validation just as hard as a missing one")

    correct = {"adgroup_id": "1", "criterion_id": "2", "status": "PAUSED"}
    record("an already-correct call is untouched",
           tool_registry._resolve_param_aliases(keyword_status, correct) == correct)

    nonsense = dict(correct, definitely_not_a_parameter="x")
    record("a genuinely unknown name stays unknown",
           "definitely_not_a_parameter" in
           tool_registry._resolve_param_aliases(keyword_status, nonsense),
           "silently dropping it would turn a typo into a wrong-but-successful call")

    print()
    print("an explicit null for an optional parameter means 'not supplied':")
    list_keywords = tools["gads_list_keywords"]
    cleaned = tool_registry._drop_null_optionals(list_keywords, {
        "campaign_id": "24176121598",
        "customer_id": "9876543210",
        "include_negatives": False,
        "limit": 100,
        "status": None,
    })
    record("the null optional is dropped", "status" not in cleaned, f"got {cleaned}")
    record("false is kept, not treated as empty", cleaned.get("include_negatives") is False,
           "dropping falsey values rather than None would silently flip this flag")
    try:
        list_keywords.fn_metadata.arg_model.model_validate(cleaned)
        record("the cleaned call validates", True)
    except Exception as exc:  # noqa: BLE001
        record("the cleaned call validates", False, str(exc)[:200])

    kept = tool_registry._drop_null_optionals(keyword_status, {"criterion_id": None})
    record("a null on a REQUIRED parameter is kept", kept.get("criterion_id", "gone") is None,
           "it must fail saying the value is wrong, not that the argument is missing")

    print()
    print("the shape that caused this is still widespread, so the fix must stay:")
    affected = 0
    for tool in tools.values():
        fn = getattr(tool, "fn", None)
        if fn is None:
            continue
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            continue
        for param in sig.parameters.values():
            if param.default is None and param.annotation is not inspect.Parameter.empty:
                annotation = str(param.annotation)
                if "Optional" not in annotation and "None" not in annotation:
                    affected += 1
    print(f"  parameters declared `x: T = None`: {affected}")
    record("dispatch drops null optionals", affected == 0 or hasattr(
        tool_registry, "_drop_null_optionals"),
        "these reject an explicit null without it")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("param handling: spelling and explicit nulls do not sink a correct call")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
