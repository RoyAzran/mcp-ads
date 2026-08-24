"""A small TTL cache for public, non-user-scoped lookups.

Competitor research repeats itself: one conversation asks about the same three
brands five times, and every repeat is an identical request to an ad archive
that changes daily at best. The archives also push back -- Google's transparency
endpoint starts answering 429 after a few dozen rapid calls -- so not repeating
a call is a correctness measure, not only a speed one.

ONLY PUBLIC DATA GOES IN HERE. The key must never contain a user id, and no
access token, signed URL or anything derived from a user's account may be
stored. What makes this safe is that ad-archive rows are public records keyed
purely on the query -- two different customers asking the same question are
entitled to the same answer. Anything account-scoped belongs in a per-request
lookup, not here.

Per-process by design. Cloud Run runs many instances and recycles them, so a
cold instance is simply a correct instance that has to ask again; this is a
hit-rate optimisation and never a source of truth. That also rules out storing
anything that must be consistent between instances.

Follows the two hand-rolled caches already in the codebase (pipedream_common's
token cache, account_resolution's probe cache), factored once so the third one
is not written from scratch again.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Callable

# expires_at (monotonic) -> value. Monotonic, not wall clock: a clock step
# backwards would otherwise pin an entry as fresh forever.
_STORE: dict[str, tuple[float, Any]] = {}
_MAX_ENTRIES = 500

_HITS = 0
_MISSES = 0

# One dial for the operator. Individual callers pass their own ttl; this is the
# fallback and the value tests reach for.
DEFAULT_TTL = int(os.environ.get("COMPETITOR_CACHE_TTL_SECONDS", "3600") or 3600)

# A shared read has to be cheaper than the call it replaces. The upstream RPC
# takes hundreds of milliseconds to seconds; this is capped well under that, and
# a timeout is simply a miss.
_SHARED_TIMEOUT = float(os.environ.get("PUBLIC_CACHE_SHARED_TIMEOUT", "3") or 3)


def cache_key(*parts: Any) -> str:
    """Build a key from query parts.

    Everything passed here ends up in a process-wide dict, so pass query
    parameters only -- never a user id, token, or email.
    """
    return "|".join("" if p is None else str(p) for p in parts)


def _evict() -> None:
    """Drop expired entries first, then the soonest-to-expire.

    Expiry-ordered rather than insertion-ordered: when the cache is full the
    entry closest to being useless is the cheapest one to lose.
    """
    if len(_STORE) <= _MAX_ENTRIES:
        return
    now = time.monotonic()
    for key in [k for k, (exp, _) in _STORE.items() if exp <= now]:
        _STORE.pop(key, None)
    while len(_STORE) > _MAX_ENTRIES:
        oldest = min(_STORE, key=lambda k: _STORE[k][0])
        _STORE.pop(oldest, None)


# ---------------------------------------------------------------------------
# Shared tier
#
# The per-process cache above is a hit-rate optimisation and says so. That was
# not enough. Google throttles the Ads Transparency endpoint per IP, and every
# Cloud Run instance of every service leaves from the same egress address, so
# the "few dozen rapid lookups" budget is shared by the entire customer base --
# not per user, and not per instance. Twenty test calls exhausted it on
# 2026-08-23 and the whole family degraded for everyone for over twenty
# minutes, which is what a handful of customers researching at once would do.
#
# A cold instance asking again is what spends that budget, so the fix is to let
# instances share their answers. This uses the media bucket that already
# exists, so it needs no new infrastructure, no new credential and no new
# service: one prefix in a bucket the container can already write to.
#
# Every path fails open. No bucket, no credentials, a slow read, an object that
# will not parse -- all of it behaves exactly as the memory-only cache did.
# Competitor research degrading is a bad afternoon; the server failing to boot
# because a cache tier could not reach storage is a much worse one.
# ---------------------------------------------------------------------------

_SHARED_PREFIX = "public-cache/"
_SHARED_DISABLED = os.environ.get("PUBLIC_CACHE_SHARED", "").strip().lower() in {"0", "false", "no", "off"}
_SHARED_HITS = 0
_SHARED_CLIENT: Any = None
_SHARED_CLIENT_TRIED = False


def _shared_bucket() -> str:
    """The bucket to share through, or "" when the tier is off."""
    if _SHARED_DISABLED:
        return ""
    return os.environ.get("GCS_MEDIA_BUCKET", "").strip()


def _shared_blob(key: str):
    """The blob for a key, or None when sharing is unavailable for any reason."""
    global _SHARED_CLIENT, _SHARED_CLIENT_TRIED
    bucket = _shared_bucket()
    if not bucket:
        return None
    if _SHARED_CLIENT is None:
        if _SHARED_CLIENT_TRIED:
            return None
        _SHARED_CLIENT_TRIED = True
        try:
            from google.cloud import storage as gcs
            _SHARED_CLIENT = gcs.Client()
        except Exception:  # noqa: BLE001 - no credentials, no library, no sharing
            return None
    try:
        # Hashed, not the raw key: keys carry brand names and free text, and an
        # object name has length and character rules the key does not respect.
        name = _SHARED_PREFIX + hashlib.sha256(key.encode("utf-8")).hexdigest() + ".json"
        return _SHARED_CLIENT.bucket(bucket).blob(name)
    except Exception:  # noqa: BLE001
        return None


def _shared_load(key: str) -> tuple[bool, Any]:
    """(found, value) from the shared tier. Never raises."""
    blob = _shared_blob(key)
    if blob is None:
        return False, None
    try:
        raw = blob.download_as_bytes(timeout=_SHARED_TIMEOUT)
        payload = json.loads(raw.decode("utf-8"))
        # Expiry travels in the object rather than relying on lifecycle rules,
        # which are eventually-consistent and measured in days.
        if float(payload.get("e") or 0) <= time.time():
            return False, None
        return True, payload.get("v")
    except Exception:  # noqa: BLE001 - a miss is the correct fallback for all of it
        return False, None


def _shared_store(key: str, value: Any, ttl: int) -> None:
    """Publish a value for other instances. Never raises."""
    blob = _shared_blob(key)
    if blob is None:
        return
    try:
        body = json.dumps({"e": time.time() + max(int(ttl or 0), 1), "v": value})
    except (TypeError, ValueError):
        # Not JSON-serialisable, so it cannot be shared. The memory tier still
        # holds it for this process.
        return
    try:
        blob.upload_from_string(body, content_type="application/json", timeout=_SHARED_TIMEOUT)
    except Exception:  # noqa: BLE001
        return


def cached(key: str, ttl: int, loader: Callable[[], Any], refresh: bool = False) -> Any:
    """Return the cached value for `key`, or call `loader()` and store it.

    `refresh=True` skips the read but still writes, which is what a tool's
    `refresh` parameter is for: the user wants today's answer, not a fresh cache
    policy. A loader that raises is not cached -- an outage must not be
    remembered for an hour.
    """
    global _HITS, _MISSES, _SHARED_HITS
    now = time.monotonic()
    if not refresh:
        hit = _STORE.get(key)
        if hit and hit[0] > now:
            _HITS += 1
            return hit[1]
        # Another instance may already have paid for this answer. Checked before
        # the loader, because the whole point is not spending the shared
        # per-IP budget on a question that has already been asked.
        found, shared = _shared_load(key)
        if found:
            _HITS += 1
            _SHARED_HITS += 1
            _STORE[key] = (now + max(int(ttl or 0), 1), shared)
            _evict()
            return shared
    _MISSES += 1
    value = loader()
    _STORE[key] = (now + max(int(ttl or 0), 1), value)
    _evict()
    # Publish after the loader succeeds. A raising loader is not cached here for
    # the same reason it is not cached in memory: an outage must not be
    # remembered for an hour, and certainly not by every other instance.
    _shared_store(key, value, ttl)
    return value


def peek(key: str) -> bool:
    """Whether `key` would be served from cache right now.

    Lets a tool report `"cached": true` without a second lookup changing the
    hit/miss counters it is about to report.
    """
    hit = _STORE.get(key)
    return bool(hit and hit[0] > time.monotonic())


def invalidate(prefix: str = "") -> int:
    """Drop entries whose key starts with `prefix` (everything when blank)."""
    doomed = [k for k in _STORE if k.startswith(prefix)] if prefix else list(_STORE)
    for key in doomed:
        _STORE.pop(key, None)
    return len(doomed)


def stats() -> dict[str, int]:
    """Counters for the *_check_access tools, so "is it cached?" is answerable."""
    return {
        "entries": len(_STORE),
        "hits": _HITS,
        "misses": _MISSES,
        # Of those hits, the ones another instance had already paid for. Zero
        # here with traffic flowing means the shared tier is not reachable.
        "shared_hits": _SHARED_HITS,
        "shared": bool(_shared_bucket()),
    }
