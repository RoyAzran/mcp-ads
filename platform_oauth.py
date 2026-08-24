"""One-shot local OAuth for the plain-bearer ad platforms.

    python server.py auth snapchat_ads

opens the platform's consent page, catches the redirect on 127.0.0.1:8765,
exchanges the code, and writes the tokens into the local credential store --
after which the tools just work, and refresh happens automatically through
credentials_env.

The four flows are structurally identical (authorization-code + refresh), so
each platform is a config row, not a module. What differs per platform is
recorded next to its row: token endpoint quirks, scope spellings, and how hard
the platform makes it to get an app at all -- which for LinkedIn is "very".
"""
from __future__ import annotations

import http.server
import os
import secrets
import threading
import time
import urllib.parse
import webbrowser
from typing import Any, Optional

import httpx

CALLBACK_PORT = 8765
REDIRECT_URI = f"http://127.0.0.1:{CALLBACK_PORT}/callback"


FLOWS: dict[str, dict[str, Any]] = {
    "snapchat_ads": {
        "authorize_url": "https://accounts.snapchat.com/login/oauth2/authorize",
        "token_url": "https://accounts.snapchat.com/login/oauth2/access_token",
        "client_id_env": "SNAPCHAT_CLIENT_ID",
        "client_secret_env": "SNAPCHAT_CLIENT_SECRET",
        "scope": "snapchat-marketing-api",
        # Access tokens last ~30 minutes; the refresh token is the credential.
        "notes": "Create the app under Business Manager -> Business Details -> OAuth Apps.",
    },
    "microsoft_ads": {
        "authorize_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token_url": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "client_id_env": "MICROSOFT_ADS_CLIENT_ID",
        "client_secret_env": "MICROSOFT_ADS_CLIENT_SECRET",
        "scope": "https://ads.microsoft.com/msads.manage offline_access",
        "notes": (
            "Register the app in Microsoft Entra ID. You also need "
            "MICROSOFT_ADS_DEVELOPER_TOKEN -- the sandbox one is instant, "
            "production requires approval."
        ),
    },
    "linkedin_ads": {
        "authorize_url": "https://www.linkedin.com/oauth/v2/authorization",
        "token_url": "https://www.linkedin.com/oauth/v2/accessToken",
        "client_id_env": "LINKEDIN_CLIENT_ID",
        "client_secret_env": "LINKEDIN_CLIENT_SECRET",
        "scope": "r_ads rw_ads r_ads_reporting",
        "notes": (
            "Requires LinkedIn Marketing Developer Platform access on your app, "
            "which LinkedIn reviews and routinely declines for individuals. "
            "The flow below works the moment your app is approved."
        ),
    },
    "tiktok_organic": {
        "authorize_url": "https://www.tiktok.com/v2/auth/authorize/",
        "token_url": "https://open.tiktokapis.com/v2/oauth/token/",
        "client_id_env": "TIKTOK_CLIENT_KEY",
        "client_secret_env": "TIKTOK_CLIENT_SECRET",
        "scope": "user.info.basic,video.publish,video.upload",
        # TikTok spells its client id "client_key" in both requests.
        "client_id_param": "client_key",
        "notes": "TikTok access tokens last ~24h; refresh is automatic afterwards.",
    },
}


class _CodeCatcher(http.server.BaseHTTPRequestHandler):
    code: Optional[str] = None
    state_expected: str = ""
    error: Optional[str] = None

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if query.get("state", [""])[0] != self.state_expected:
            _CodeCatcher.error = "State mismatch -- possible CSRF, try again."
        elif "error" in query:
            _CodeCatcher.error = query.get("error_description", query["error"])[0]
        else:
            _CodeCatcher.code = query.get("code", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        body = "You can close this tab." if _CodeCatcher.code else f"Failed: {_CodeCatcher.error}"
        self.wfile.write(f"<h2>MCP Ads</h2><p>{body}</p>".encode())

    def log_message(self, *args):  # silence request logging
        pass


def _exchange(flow: dict, code: str) -> dict:
    id_param = flow.get("client_id_param", "client_id")
    data = {
        id_param: os.environ[flow["client_id_env"]],
        "client_secret": os.environ[flow["client_secret_env"]],
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }
    with httpx.Client(timeout=30) as client:
        response = client.post(
            flow["token_url"], data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    response.raise_for_status()
    return response.json()


def refresh_access_token(platform: str, refresh_token: str) -> Optional[dict]:
    """Exchange a refresh token; returns {"access_token", "refresh_token",
    "expires_at"} or None when this platform has no flow configured."""
    flow = FLOWS.get((platform or "").strip().lower())
    if flow is None:
        return None
    client_id = os.environ.get(flow["client_id_env"], "").strip()
    client_secret = os.environ.get(flow["client_secret_env"], "").strip()
    if not (client_id and client_secret):
        return None
    id_param = flow.get("client_id_param", "client_id")
    data = {
        id_param: client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    if platform == "microsoft_ads":
        data["scope"] = flow["scope"]
    with httpx.Client(timeout=30) as client:
        response = client.post(
            flow["token_url"], data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if response.status_code >= 400:
        return None
    payload = response.json()
    # Snapchat and TikTok nest under "data" sometimes; unwrap defensively.
    if isinstance(payload.get("data"), dict) and "access_token" in payload["data"]:
        payload = payload["data"]
    access_token = payload.get("access_token") or ""
    if not access_token:
        return None
    return {
        "access_token": access_token,
        "refresh_token": payload.get("refresh_token") or refresh_token,
        "expires_at": time.time() + int(payload.get("expires_in") or 3600),
    }


def run_auth(platform: str) -> int:
    platform = (platform or "").strip().lower()
    flow = FLOWS.get(platform)
    if flow is None:
        print(f"No local OAuth flow for {platform!r}. Available: {', '.join(sorted(FLOWS))}")
        return 2

    client_id = os.environ.get(flow["client_id_env"], "").strip()
    client_secret = os.environ.get(flow["client_secret_env"], "").strip()
    if not (client_id and client_secret):
        print(
            f"Set {flow['client_id_env']} and {flow['client_secret_env']} in .env first.\n"
            f"({flow['notes']})\n"
            f"Register {REDIRECT_URI} as an allowed redirect URI on the app."
        )
        return 2

    state = secrets.token_urlsafe(16)
    _CodeCatcher.state_expected = state
    id_param = flow.get("client_id_param", "client_id")
    params = {
        id_param: client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": flow["scope"],
        "state": state,
    }
    url = f"{flow['authorize_url']}?{urllib.parse.urlencode(params)}"

    server = http.server.HTTPServer(("127.0.0.1", CALLBACK_PORT), _CodeCatcher)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Opening the {platform} consent page. ({flow['notes']})")
    webbrowser.open(url)

    deadline = time.time() + 300
    while time.time() < deadline and _CodeCatcher.code is None and _CodeCatcher.error is None:
        time.sleep(0.3)
    server.shutdown()

    if _CodeCatcher.error or _CodeCatcher.code is None:
        print(f"Authorization failed: {_CodeCatcher.error or 'timed out after 5 minutes'}")
        return 1

    payload = _exchange(flow, _CodeCatcher.code)
    if isinstance(payload.get("data"), dict) and "access_token" in payload["data"]:
        payload = payload["data"]
    access_token = payload.get("access_token") or ""
    if not access_token:
        print(f"The token endpoint returned no access token: {payload}")
        return 1

    from credentials_env import EnvCredentialProvider

    EnvCredentialProvider().update_platform_token(
        platform,
        "local",
        access_token,
        payload.get("refresh_token") or "",
        time.time() + int(payload.get("expires_in") or 3600),
    )
    print(f"Connected. Token stored; refresh is automatic. Try: python server.py doctor")
    return 0
