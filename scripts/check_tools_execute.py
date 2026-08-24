"""Actually execute tools, rather than just checking they are registered.

Every suite in this repo inspects the registry, the schemas and the routing.
None of them calls a tool. Two production breakages in one day got through that
gap, both invisible until a real request arrived:

  * SDK 2.0 changed Tool.run(arguments) to run(arguments, context), so every
    dispatcher call failed with "missing 1 required positional argument" -- on
    /mcp-slim, the CLI and the GPT Action at once.
  * `json` was used in slim_mcp.py without being imported, so media_upload_result
    raised NameError the moment anyone called it.

Both are call-time failures in code that imports cleanly, which is precisely
what a registry-shaped test cannot see. This runs the real call path with a
stubbed user and asserts the tool was *reached* -- an upstream "not connected"
or "not authenticated" is a pass, because it means the plumbing worked and the
tool's own logic answered.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import warnings

from cryptography.fernet import Fernet

warnings.filterwarnings("ignore")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./check_tools_execute.db")
os.environ.setdefault("JWT_SECRET_KEY", "ci-check-secret")
os.environ.setdefault("FERNET_KEY", Fernet.generate_key().decode())
os.environ.setdefault("BASE_URL", "https://example.invalid")
os.environ.setdefault("SERVER_BASE_URL", "https://example.invalid")
os.environ.setdefault("GCS_MEDIA_BUCKET", "ci-check-bucket")

# A failure that proves the call path is broken, as opposed to the tool politely
# reporting that an account is not connected.
_PLUMBING_ERRORS = (TypeError, NameError, AttributeError, ImportError)

FAILURES: list[str] = []


def record(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -> ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(f"{label}: {detail}")


async def main() -> int:
    import slim_mcp
    from auth import current_user_ctx
    from tool_registry import dispatch

    class _User:
        id = "ci-check-user"

    token = current_user_ctx.set(_User())
    try:
        print("first-class slim tools:")
        # These are hand-registered rather than reached through a dispatcher, so
        # nothing else exercises them.
        for label, call in (
            # Registered by the Apps extension, so there is no module-level
            # function -- call the shared implementation the tool wraps.
            ("media_upload_start", lambda: __import__("media_app").upload_start_payload("ci")),
            ("media_upload_result", lambda: slim_mcp.slim_media_upload_result("nonexistent")),
            ("list_actions", lambda: slim_mcp.slim_list_actions("google_ads_action", "", 5, 0)),
        ):
            try:
                result = call()
                record(label, result is not None, type(result).__name__)
            except _PLUMBING_ERRORS as exc:
                record(label, False, f"{type(exc).__name__}: {exc}")
            except Exception as exc:  # the tool answered; that is what we wanted
                record(label, True, f"{type(exc).__name__} (tool reached)")

        # The Context dispatch hands a tool has NO request behind it, because
        # dispatch is reached from plain HTTP handlers (/mcp-slim, the CLI, the
        # GPT Action). A tool that calls context.report_progress on it dies with
        # "Context is not available outside of a request" -- which is exactly
        # how image generation broke on every one of those paths, before the
        # OpenAI call was even made. Progress reporting must be survivable.
        print("\nprogress reporting against a request-free Context:")
        try:
            from tool_registry import _tool_context
            from tools.creative import _report

            await _report(_tool_context(), 10, 100, "ci check")
            record("_report tolerates a request-free Context", True)
        except _PLUMBING_ERRORS as exc:
            record("_report tolerates a request-free Context", False,
                   f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            record("_report tolerates a request-free Context", False,
                   f"raised {type(exc).__name__}: {exc}")

        print("\ndispatch() to a real tool:")
        # The path /mcp-slim, the CLI and the GPT Action all share. A signature
        # mismatch here breaks every one of them simultaneously.
        for category, action in (
            ("meta_ads_action", "meta_ads_list_ad_accounts"),
            ("google_ads_action", "google_ads_list_customers"),
        ):
            try:
                await dispatch(category, action, {})
                record(f"{category}.{action}", True, "returned")
            except _PLUMBING_ERRORS as exc:
                record(f"{category}.{action}", False, f"{type(exc).__name__}: {exc}")
            except Exception as exc:
                message = str(exc)
                broken = "positional argument" in message or "not defined" in message
                record(f"{category}.{action}", not broken,
                       "tool reached" if not broken else message[:90])
    finally:
        current_user_ctx.reset(token)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("tools execute: the call path is intact")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
