"""Signed, same-origin links for competitor ad images.

The gallery shows creatives that live on Meta's and Google's CDNs, and those
URLs cannot go into the widget as they are. Three reasons, in order of how badly
they bite:

Meta's ad_snapshot_url only renders with an access_token in the query string.
Putting that into HTML handed to a chat host would publish an ID-verified Ad
Library token into a page we do not control, permanently, in somebody's
transcript. That one is not a trade-off.

The CDN URLs are signed and short-lived. A gallery built straight on them looks
right when it is made and is a grid of broken images an hour later, which reads
as a broken product rather than as expired content.

And widening the widget's img-src to *.fbcdn.net and *.googleusercontent.com
would have every end user's browser fetch from Meta and Google directly, with
their IP, from inside a chat client, for content they did not ask to load.

So the picture comes back through this server, from an origin the widget's CSP
already allows -- no CSP change at all, since base_url() is the one entry the
creative gallery already has.

The token signs the URL itself rather than an asset id, and is deliberately not
user-scoped: this is public archive data, unlike /generated-media, which is a
customer's own creatives and is per-user for that reason. What the signature
buys is that only a URL this server has already seen in an upstream response can
be fetched -- without it the route would be an open proxy for anyone who can
reach it.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from urllib.parse import quote

# An hour: long enough to scroll back through a conversation, short enough that a
# leaked link stops working. The upstream CDN URLs usually expire first anyway.
_TTL_SECONDS = max(int(os.environ.get("COMPETITOR_MEDIA_TTL_SECONDS", "3600") or 3600), 60)

# Hosts a signed URL is allowed to point at. Suffix-matched against the
# hostname, so "evil.com/.fbcdn.net" and "notfbcdn.net" both miss.
# This is the SSRF boundary; the signature alone is not one, because this
# server signs whatever an upstream response hands it.
ALLOWED_HOST_SUFFIXES: tuple[str, ...] = (
    ".fbcdn.net",
    ".googleusercontent.com",
    ".googlesyndication.com",
    ".2mdn.net",
    ".ggpht.com",
    ".gstatic.com",
)


def _secret() -> bytes:
    return (os.environ.get("JWT_SECRET_KEY") or "").encode()


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64u(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def host_allowed(url: str) -> bool:
    """Whether this URL may be fetched at all. https only, allowlisted host."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower().rstrip(".")
    # Compare against ".suffix" so the check is a domain boundary rather than a
    # substring; also accept the bare apex ("fbcdn.net" itself).
    return any(host.endswith(suffix) or host == suffix.lstrip(".")
               for suffix in ALLOWED_HOST_SUFFIXES)


def make_token(url: str, ttl: int = _TTL_SECONDS) -> str:
    """A token that says: this exact URL, until this moment."""
    claims = {"u": url, "e": int(time.time()) + ttl}
    payload = _b64u(json.dumps(claims, sort_keys=True).encode())
    sig = _b64u(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def read_token(token: str) -> str | None:
    """The verified URL, or None. Never raises -- a bad link is a 404, not a 500."""
    try:
        payload, sig = token.split(".", 1)
        expected = _b64u(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest())
        # compare_digest so a wrong signature cannot be recovered from timing.
        if not hmac.compare_digest(sig, expected):
            return None
        claims = json.loads(_unb64u(payload))
        if int(claims.get("e") or 0) < time.time():
            return None
        url = str(claims.get("u") or "")
        # Re-check the host on the way out as well as in. The allowlist can be
        # tightened after a token was minted, and the newer rule should win.
        return url if url and host_allowed(url) else None
    except Exception:  # noqa: BLE001 - any malformed token is simply not valid
        return None


def proxy_url(url: str) -> str:
    """Same-origin link for a competitor creative, or "" when one cannot be built.

    Empty rather than the raw upstream URL: falling back to the CDN would quietly
    reintroduce every problem this module exists to prevent. A card with no
    picture is a smaller failure than a leaked token.
    """
    base = (os.environ.get("APP_URL") or os.environ.get("BASE_URL")
            or os.environ.get("SERVER_BASE_URL") or "").rstrip("/")
    if not base or not url or not _secret() or not host_allowed(url):
        return ""
    return f"{base}/competitor-media?token={quote(make_token(url), safe='')}"
