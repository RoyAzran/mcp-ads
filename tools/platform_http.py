"""One HTTP door to the plain-bearer ad platforms, whoever holds the key.

Every LinkedIn/TikTok/Snapchat/Microsoft tool builds a real upstream URL and
calls `request()`. What happens next depends on the credential backend:

  * The backend returns a bearer token -> the call goes straight to the
    upstream API with an Authorization header. This is the self-host path:
    the operator's own OAuth app, their own token, no intermediary.

  * The backend returns credentials.PROXY -> the call is handed to a transport
    registered at startup. This is the hosted path: a managed-auth broker that
    deliberately never reveals user tokens, so there is nothing to put in an
    Authorization header and the broker must make the call itself.

The split lives here, below the tools, on purpose: 96 tool bodies were written
against one function signature, and teaching each of them about deployment
modes would fork them. The transport signature is exactly the old
`_proxy_request` signature, so the hosted build registers its existing proxy
body unchanged.

The pure helpers (_json, _parse_json_arg, _platform_base, _append_params)
moved here from the old shared module because every tool module imports them
alongside request(); they contain no transport logic.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable, Optional
from urllib.parse import urlencode

import httpx


# Platform key -> label + upstream API base. The keys name our tools and
# routes; the bases are the real APIs (they were always the real APIs -- the
# old proxy encoded them into its broker URL).
PLATFORMS: dict[str, dict[str, str]] = {
    "linkedin_ads": {
        "label": "LinkedIn Ads",
        "base_url": "https://api.linkedin.com",
    },
    "tiktok_ads": {
        "label": "TikTok Ads",
        "base_url": "https://business-api.tiktok.com/open_api/v1.3",
    },
    "snapchat_ads": {
        "label": "Snapchat Ads",
        "base_url": "https://adsapi.snapchat.com/v1",
    },
    "microsoft_ads": {
        "label": "Microsoft Advertising",
        # Bing Ads v13 splits REST across per-service hosts (campaign./
        # clientcenter./reporting.), so tools/microsoft_ads.py builds absolute
        # URLs per service. This default only covers Campaign Management.
        "base_url": "https://campaign.api.bingads.microsoft.com/CampaignManagement/v13",
    },
    "linkedin_organic": {
        "label": "LinkedIn (organic)",
        "base_url": "https://api.linkedin.com",
    },
    "tiktok_organic": {
        "label": "TikTok (organic)",
        "base_url": "https://open.tiktokapis.com/v2",
    },
    "meta_pages": {
        "label": "Facebook & Instagram pages",
        "base_url": "https://graph.facebook.com/v21.0",
    },
}


# Headers a platform requires on every call. Merged UNDER the caller's headers,
# so a tool that sets its own (X-Restli-Method, say) always wins. The hosted
# broker's managed client added LinkedIn's protocol headers itself, which is
# why the tools never needed to -- direct calls do.
def _default_headers(platform: str) -> dict:
    if platform in ("linkedin_ads", "linkedin_organic"):
        return {
            "X-Restli-Protocol-Version": "2.0.0",
            "LinkedIn-Version": os.environ.get("LINKEDIN_API_VERSION", "").strip() or "202411",
        }
    if platform == "tiktok_ads":
        # TikTok's Marketing API takes the token in its own header, not in
        # Authorization. request() moves it there.
        return {}
    return {}


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


def _parse_json_arg(value: Any, name: str, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        raise ValueError(f"{name} must be valid JSON.")
    try:
        return json.loads(value)
    except Exception as exc:
        raise ValueError(f"{name} must be valid JSON: {exc}") from exc


def _platform_base(platform: str, path_or_url: str) -> str:
    """Join a platform's API base with a path. Absolute URLs pass through untouched."""
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        return path_or_url
    path = path_or_url if path_or_url.startswith("/") else f"/{path_or_url}"
    return f"{PLATFORMS[platform]['base_url']}{path}"


def _append_params(url: str, params: dict | None) -> str:
    clean_params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    if not clean_params:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{urlencode(clean_params, doseq=True)}"


# The hosted build's broker, installed by its credential backend at import
# time. Signatures match the functions the hosted build has always run:
#   transport(platform, method, upstream_url, account_id, params, body, headers) -> dict
#   lister(platform) -> list[dict]
_TRANSPORT: Optional[Callable[..., dict]] = None
_LIST_TRANSPORT: Optional[Callable[[str], list]] = None


def register_transport(
    request_fn: Optional[Callable[..., dict]] = None,
    list_accounts_fn: Optional[Callable[[str], list]] = None,
) -> None:
    global _TRANSPORT, _LIST_TRANSPORT
    if request_fn is not None:
        _TRANSPORT = request_fn
    if list_accounts_fn is not None:
        _LIST_TRANSPORT = list_accounts_fn


def request(
    platform: str,
    method: str,
    upstream_url: str,
    connection_id: str = "",
    params: dict | None = None,
    body: Any = None,
    headers: dict | None = None,
) -> dict:
    """Call an upstream ad-platform API as the connected account.

    Returns the upstream JSON on success; on any failure returns
    {"error", "status_code", ...} rather than raising, because every tool body
    was written against that contract and surfaces it verbatim to the agent.
    """
    from credentials import PROXY, provider

    try:
        token = provider().platform_token(platform, connection_id)
    except RuntimeError as exc:
        # "Nothing is connected" arrives here. The old code raised out of
        # account resolution and each tool caught it into {"error": str}, so
        # raising keeps that path -- but going through it would lose the
        # status_code shape some callers check. Return the envelope instead.
        return {"error": str(exc), "status_code": 0, "connection_id": connection_id}

    if token == PROXY:
        if _TRANSPORT is None:
            return {
                "error": (
                    f"This deployment brokers {platform} calls through a managed "
                    "proxy, but no transport is registered. The hosted backend "
                    "registers one at startup; a self-host install should supply "
                    "platform credentials instead."
                ),
                "status_code": 0,
            }
        return _TRANSPORT(platform, method, upstream_url, connection_id, params, body, headers)

    method = method.upper().strip()
    target_url = _append_params(upstream_url, params)

    request_headers = dict(_default_headers(platform))
    for key, value in (headers or {}).items():
        if value in (None, ""):
            continue
        request_headers[str(key)] = str(value)
    if platform in ("tiktok_ads",):
        # TikTok Marketing API authenticates with Access-Token, not a Bearer
        # Authorization header; sending only the latter yields a 401 with an
        # unhelpful body.
        request_headers["Access-Token"] = token
    else:
        request_headers.setdefault("Authorization", f"Bearer {token}")

    with httpx.Client(timeout=30) as client:
        response = client.request(
            method,
            target_url,
            headers=request_headers,
            json=body if body not in (None, "") else None,
        )

    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - upstream returned non-JSON
        payload = {"raw_text": response.text}

    if response.status_code >= 400:
        return {
            "error": payload.get("error", payload) if isinstance(payload, dict) else payload,
            "status_code": response.status_code,
            "connection_id": connection_id,
            # The old proxy injected this key; tool bodies and downstream
            # consumers still read it, so both spellings carry the same value.
            "pipedream_account_id": connection_id,
            "upstream_url": target_url,
        }
    if isinstance(payload, dict):
        payload.setdefault("connection_id", connection_id)
        payload.setdefault("pipedream_account_id", connection_id)
        return payload
    return {
        "data": payload,
        "connection_id": connection_id,
        "pipedream_account_id": connection_id,
    }


def list_accounts(platform: str = "") -> list[dict]:
    """The connected accounts usable for a platform's calls.

    Hosted: the broker's account listing, verbatim. Self-host: the credential
    store's connections, shaped like account rows -- minus the tokens, which
    must never reach tool output.
    """
    from credentials import PROXY, provider

    token = provider().platform_token(platform)
    if token == PROXY:
        if _LIST_TRANSPORT is None:
            raise RuntimeError(
                f"This deployment brokers {platform} through a managed proxy, "
                "but no account-listing transport is registered."
            )
        return _LIST_TRANSPORT(platform)

    label = PLATFORMS.get(platform, {}).get("label", platform)
    rows = provider().list_connections(platform)
    if rows:
        return [
            {"id": str(r.get("id") or ""), "name": r.get("label") or label,
             "app": platform, "healthy": True}
            for r in rows
        ]
    # platform_token() succeeded, so a credential exists even though the store
    # has no row for it (a bare env var). Present it as the one account.
    return [{"id": "env", "name": label, "app": platform, "healthy": True}]
