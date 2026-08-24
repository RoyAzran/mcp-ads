"""The public-lookup cache must survive its own failures, and must share.

Two separate things are pinned here, and both were learned the hard way.

Sharing. Google throttles the Ads Transparency endpoint per IP, and every
instance of a Cloud Run service leaves from the same egress address, so the
"few dozen rapid lookups" budget belongs to the whole customer base rather than
to a user or an instance. On 2026-08-23 roughly twenty test calls exhausted it
and the entire family degraded for everyone for over twenty minutes. A
process-local cache cannot help with that: instances are cold, recycled, and
numerous, so the same question gets asked again and again by different
processes. The shared tier is what stops that, and a regression to
memory-only would be invisible until the next outage.

Failing open. The tier writes to a GCS bucket, so it inherits every way storage
can be unavailable: no bucket configured, no credentials, a slow read, an
object that will not parse, a value that is not JSON. None of those may raise.
Competitor research degrading is a bad afternoon; a cache tier taking the
server down is a much worse one, and it would take down tools that have nothing
to do with competitor research.
"""
from __future__ import annotations

import importlib
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []


def record(label: str, ok: bool, detail: str = "") -> None:
    suffix = f"  -> {detail}" if (detail and not ok) else ""
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{suffix}")
    if not ok:
        FAILURES.append(label)


def fresh(**env):
    """Reimport the module with a given environment."""
    for key in ("GCS_MEDIA_BUCKET", "PUBLIC_CACHE_SHARED"):
        os.environ.pop(key, None)
    os.environ.update({k: v for k, v in env.items() if v is not None})
    import tools.public_cache as pc

    return importlib.reload(pc)


def main() -> int:
    print("with no bucket configured it behaves exactly as a memory cache:")
    pc = fresh()
    calls: list[int] = []
    value = pc.cached("k", 60, lambda: (calls.append(1), {"n": len(calls)})[1])
    again = pc.cached("k", 60, lambda: (calls.append(1), {"n": len(calls)})[1])
    record("a value is returned", value == {"n": 1})
    record("a second read is served from memory", again == {"n": 1} and len(calls) == 1)
    record("stats report sharing as off", pc.stats().get("shared") is False)

    print("\nnothing raises when storage is unavailable:")
    pc = fresh(GCS_MEDIA_BUCKET="a-bucket-that-does-not-exist-9d2f1")
    try:
        got = pc.cached("k2", 60, lambda: {"ok": True})
        record("an unreachable bucket still returns the value", got == {"ok": True})
    except Exception as exc:  # noqa: BLE001
        record("an unreachable bucket still returns the value", False, f"raised {exc!r}")
    try:
        found, _ = pc._shared_load("k2")
        record("a failed shared read reports a miss", found is False)
    except Exception as exc:  # noqa: BLE001
        record("a failed shared read reports a miss", False, f"raised {exc!r}")

    print("\na value that cannot be serialised is still usable locally:")
    class NotJson:
        pass
    try:
        got = pc.cached("k3", 60, NotJson)
        record("an unserialisable value does not raise", isinstance(got, NotJson))
    except Exception as exc:  # noqa: BLE001
        record("an unserialisable value does not raise", False, f"raised {exc!r}")

    print("\nan outage is never cached, locally or shared:")
    def boom():
        raise RuntimeError("upstream down")
    try:
        pc.cached("k4", 60, boom)
        record("a raising loader propagates", False, "the error was swallowed")
    except RuntimeError:
        record("a raising loader propagates", True)
    record("and its key is not stored", "k4" not in pc._STORE,
           "an outage would be remembered for the full TTL")

    print("\nthe switch turns it off without turning the cache off:")
    pc = fresh(GCS_MEDIA_BUCKET="mcp-ads-media", PUBLIC_CACHE_SHARED="0")
    record("PUBLIC_CACHE_SHARED=0 disables sharing", pc._shared_bucket() == "")
    record("and no blob is built", pc._shared_blob("x") is None)
    record("while the memory cache still works",
           pc.cached("k5", 60, lambda: 7) == 7 and pc.cached("k5", 60, lambda: 8) == 7)

    print("\nthe key never leaks into the object name:")
    pc = fresh(GCS_MEDIA_BUCKET="mcp-ads-media")
    import hashlib
    expected = hashlib.sha256(b"brand|US|secret-looking-text").hexdigest()
    record("object names are hashed, not the raw key", expected in
           (pc._SHARED_PREFIX + expected + ".json"),
           "query text would otherwise appear in bucket listings")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("public cache: shares between instances, and fails open when it cannot")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
