"""A viewable link for an image the assistant just generated.

Generated creatives were stored and handed to Meta, and that was the only thing
anyone could do with them: the tools returned a generated_asset_id and nothing
else, so a customer could not see what had been made. On a host that renders no
inline preview -- ChatGPT, for one -- the assistant correctly reported that it
had no way to show the picture, and the only route to a look at it was to push
it into a live ad account and open Ads Manager.

So the bytes get a URL. Signed rather than guessable, and expiring with the
asset it points at: these are a customer's ad creatives, and an id in a URL is
not an access control. The same HMAC scheme the media upload flow already uses,
for the same reason -- the link is handed to a chat client we do not control,
so it has to carry its own authority and its own expiry.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

_TTL_SECONDS = max(int(os.environ.get("GENERATED_IMAGE_STORE_TTL_SECONDS", "7200") or "7200"), 60)


def _secret() -> bytes:
    return (os.environ.get("JWT_SECRET_KEY") or "").encode()


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64u(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def make_token(asset_id: str, user_id: str, ttl: int = _TTL_SECONDS) -> str:
    """A token that says: this asset, this owner, until this moment."""
    claims = {"a": asset_id, "u": user_id, "e": int(time.time()) + ttl}
    payload = _b64u(json.dumps(claims).encode())
    sig = _b64u(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def read_token(token: str) -> dict | None:
    """Verified claims, or None. Never raises -- a bad link is a 404, not a 500."""
    try:
        payload, sig = token.split(".", 1)
        expected = _b64u(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest())
        # compare_digest so a wrong signature cannot be recovered from timing.
        if not hmac.compare_digest(sig, expected):
            return None
        claims = json.loads(_unb64u(payload))
        if int(claims.get("e") or 0) < time.time():
            return None
        return claims
    except Exception:  # noqa: BLE001 - any malformed token is simply not valid
        return None


def preview_url(asset_id: str, user_id: str) -> str:
    """Absolute link the assistant can show, or "" when nothing can be built.

    Empty rather than a relative path: this is handed to a chat client that has
    no idea what host we are on, and a half-formed link is worse than none --
    it looks like a feature that is broken rather than one not configured.
    """
    base = (os.environ.get("APP_URL") or os.environ.get("BASE_URL")
            or os.environ.get("SERVER_BASE_URL") or "").rstrip("/")
    if not base or not asset_id or not _secret():
        return ""
    return f"{base}/generated-media/{asset_id}?token={make_token(asset_id, user_id)}"
