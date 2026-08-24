"""The single-user credential backend: environment variables plus a local file.

This is what a self-hosted install runs. There is exactly one user, so there is
nothing to scope credentials to and no reason to carry a database: tokens the
operator pastes in come from the environment, and tokens the server obtains for
itself (OAuth exchanges, refreshes) go into an encrypted file next to them.

Why a file at all, when env vars would be simpler: three of the platforms hand
out short-lived access tokens. TikTok's last about 24 hours. A connection made
for a demo on Monday is dead by Tuesday, and the failure is a bare 401. Refresh
is therefore a normal runtime operation, and a refreshed token has to be
persisted somewhere the process can write.

The file is Fernet-encrypted when MCP_ADS_SECRET_KEY is set, and plain JSON with
0600 permissions when it is not. Plaintext is the default deliberately: a
self-hoster who has to invent a key before the server will start tends to put
the key in the same .env as everything else, which buys nothing, and the honest
statement is that this file is as sensitive as the .env beside it.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from credentials import current_connection_token_ctx, current_user_ctx  # noqa: F401


# Google issues one refresh token per consent, not one per API. A single-user
# install almost always connects one Google account and expects it to cover Ads,
# Analytics, Search Console, Tag Manager and Sheets, so a shared token is the
# default and per-service overrides exist for the case where it is not.
_GOOGLE_SERVICE_ENV = {
    "google_ads": "GOOGLE_ADS_REFRESH_TOKEN",
    "ga4": "GA4_REFRESH_TOKEN",
    "gsc": "GSC_REFRESH_TOKEN",
    "gtm": "GTM_REFRESH_TOKEN",
    "sheets": "SHEETS_REFRESH_TOKEN",
    "drive": "DRIVE_REFRESH_TOKEN",
}

_PLATFORM_ENV = {
    "linkedin": "LINKEDIN_ACCESS_TOKEN",
    "linkedin_ads": "LINKEDIN_ACCESS_TOKEN",
    "tiktok": "TIKTOK_ADS_ACCESS_TOKEN",
    "tiktok_ads": "TIKTOK_ADS_ACCESS_TOKEN",
    "tiktok_organic": "TIKTOK_ORGANIC_ACCESS_TOKEN",
    "snapchat": "SNAPCHAT_ACCESS_TOKEN",
    "snapchat_ads": "SNAPCHAT_ACCESS_TOKEN",
    "microsoft": "MICROSOFT_ADS_ACCESS_TOKEN",
    "microsoft_ads": "MICROSOFT_ADS_ACCESS_TOKEN",
    "meta_pages": "META_ACCESS_TOKEN",
}


def _home() -> Path:
    return Path(
        os.environ.get("MCP_ADS_HOME") or (Path.home() / ".mcp-ads")
    ).expanduser()


class _Store:
    """A tiny key/value file for tokens the server writes back."""

    def __init__(self) -> None:
        self._path = _home() / "credentials.json"
        self._cache: Optional[dict] = None

    def _fernet(self):
        key = os.environ.get("MCP_ADS_SECRET_KEY", "").strip()
        if not key:
            return None
        from cryptography.fernet import Fernet

        return Fernet(key.encode())

    def _load(self) -> dict:
        if self._cache is not None:
            return self._cache
        if not self._path.exists():
            self._cache = {}
            return self._cache
        raw = self._path.read_bytes()
        fernet = self._fernet()
        if fernet is not None:
            try:
                raw = fernet.decrypt(raw)
            except Exception:  # noqa: BLE001
                # A key that does not match this file is an operator error, and
                # silently starting with an empty store would look like "all my
                # connections vanished" rather than "wrong key".
                raise RuntimeError(
                    f"{self._path} could not be decrypted with MCP_ADS_SECRET_KEY. "
                    "Either the key changed or the file was written without one."
                )
        try:
            self._cache = json.loads(raw.decode() or "{}")
        except ValueError:
            self._cache = {}
        return self._cache

    def _save(self) -> None:
        data = json.dumps(self._cache or {}, indent=2).encode()
        fernet = self._fernet()
        if fernet is not None:
            data = fernet.encrypt(data)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_bytes(data)
        try:
            self._path.chmod(0o600)
        except OSError:
            # Windows and some mounted filesystems do not implement this. The
            # file is still no more exposed than the .env beside it.
            pass

    def get(self, key: str, default: Any = None) -> Any:
        return self._load().get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._load()[key] = value
        self._save()


_store = _Store()


class SelfHostPrincipal:
    """The single user. Duck-types credentials.Principal.

    `selected_wordpress_connection_id` is a property with a setter because
    tools/wordpress/sites.py assigns to it directly; a plain attribute would
    accept the write and lose it at the end of the process.
    """

    id = "self"
    # One user, who owns the machine. require_editor() has nobody to protect
    # them from, so every write is theirs to make.
    role = "admin"

    @property
    def selected_wordpress_connection_id(self) -> Optional[str]:
        return _store.get("wordpress.selected")

    @selected_wordpress_connection_id.setter
    def selected_wordpress_connection_id(self, value: Optional[str]) -> None:
        _store.set("wordpress.selected", value)

    def get_meta_token(self) -> Optional[str]:
        return os.environ.get("META_ACCESS_TOKEN") or _store.get("meta.access_token")


PRINCIPAL = SelfHostPrincipal()


class EnvCredentialProvider:
    """Credentials from the environment and the local store."""

    # -- Google family ----------------------------------------------------
    def google_token(self, service: str, account_id: str = "") -> Optional[str]:
        service = (service or "").strip().lower()
        specific = os.environ.get(_GOOGLE_SERVICE_ENV.get(service, ""), "").strip()
        return (
            specific
            or os.environ.get("GOOGLE_REFRESH_TOKEN", "").strip()
            or _store.get(f"google.{service}")
            or _store.get("google.refresh_token")
            or None
        )

    # -- Meta -------------------------------------------------------------
    def meta_token(self, account_id: str = "", surface: str = "ads") -> Optional[str]:
        if surface == "pages":
            page_token = os.environ.get("META_PAGE_ACCESS_TOKEN", "").strip()
            if page_token:
                return page_token
        return PRINCIPAL.get_meta_token()

    # -- Plain-bearer ad platforms ----------------------------------------
    def platform_token(self, platform: str, connection_id: str = "") -> str:
        platform = (platform or "").strip().lower()

        # A stored token wins over the env var, because a stored one may have
        # been refreshed since the operator pasted theirs in.
        stored = _store.get(f"platform.{platform}") or {}
        if isinstance(stored, dict) and stored.get("access_token"):
            expires_at = stored.get("expires_at")
            if not expires_at or time.time() < float(expires_at) - 120:
                return stored["access_token"]
            refreshed = self._refresh(platform, stored)
            if refreshed:
                return refreshed

        token = os.environ.get(_PLATFORM_ENV.get(platform, ""), "").strip()
        if token:
            return token
        raise RuntimeError(
            f"No credentials for {platform}. Run `mcp-ads auth {platform}`, "
            f"or set {_PLATFORM_ENV.get(platform, platform.upper() + '_ACCESS_TOKEN')}."
        )

    def _refresh(self, platform: str, stored: dict) -> Optional[str]:
        """Exchange a refresh token for a new access token.

        Kept on this side of the seam on purpose: tools/platform_http.py should
        not know which platforms expire, which hold a permanent token, and which
        are brokered. Returns None when this platform has no refresh flow, in
        which case the caller falls through to the environment.
        """
        refresh_token = stored.get("refresh_token")
        if not refresh_token:
            return None

        try:
            from platform_oauth import refresh_access_token
        except ImportError:
            # The per-platform OAuth flows land separately. Until they do, an
            # expired token falls through to the environment rather than
            # raising an ImportError that names a module the operator has never
            # heard of and cannot install.
            return None

        payload = refresh_access_token(platform, refresh_token)
        if not payload:
            return None
        self.update_platform_token(
            platform,
            stored.get("connection_id", ""),
            payload["access_token"],
            payload.get("refresh_token", refresh_token),
            payload.get("expires_at"),
        )
        return payload["access_token"]

    def legacy_meta_token_for_user(self, user_id: str) -> Optional[str]:
        # One user; whoever staged the upload is the operator.
        return PRINCIPAL.get_meta_token()

    def api_key(self, platform: str) -> Optional[str]:
        platform = (platform or "").strip().lower()
        env_name = {
            "openai": "OPENAI_API_KEY",
            "meta_ad_library": "META_AD_LIBRARY_TOKEN",
        }.get(platform, f"{platform.upper()}_API_KEY")
        return os.environ.get(env_name, "").strip() or _store.get(f"apikey.{platform}")

    # -- WordPress --------------------------------------------------------
    def wordpress_connection(self, connection_id: str = "") -> Optional[dict]:
        sites = self.list_connections("wordpress")
        if not sites:
            return None
        target = (connection_id or "").strip() or PRINCIPAL.selected_wordpress_connection_id
        row = next((s for s in sites if s.get("connection_id") == target), None) if target else sites[0]
        if row is None:
            return None
        return {
            "id": row.get("connection_id"),
            "site_url": row.get("site_url"),
            "username": row.get("app_username", ""),
            "app_password": row.get("app_password", ""),
            "label": row.get("label"),
        }

    def select_wordpress_connection(self, connection_id: str) -> None:
        PRINCIPAL.selected_wordpress_connection_id = connection_id

    # -- Discovery --------------------------------------------------------
    def list_connections(self, platform: str) -> list[dict]:
        platform = (platform or "").strip().lower()

        if platform == "wordpress":
            # Storage spelling (connection_id / app_username / app_password /
            # health), because tools/wordpress reads these rows directly --
            # resolve_wp_creds() builds its client from app_username and
            # app_password, and the site tools show label and health.
            site_url = os.environ.get("WORDPRESS_SITE_URL", "").strip()
            if not site_url:
                return list(_store.get("wordpress.sites", []) or [])
            return [
                {
                    "id": "env",
                    "connection_id": "env",
                    "label": os.environ.get("WORDPRESS_LABEL", "").strip() or site_url,
                    "site_url": site_url,
                    "app_username": os.environ.get("WORDPRESS_USERNAME", "").strip(),
                    "app_password": os.environ.get("WORDPRESS_APP_PASSWORD", "").strip(),
                    "health": "connected",
                }
            ]

        if platform == "google_ads":
            token = self.google_token("google_ads")
        elif platform in ("meta_ads", "meta_pages", "meta_organic"):
            token = self.meta_token(surface="pages" if platform != "meta_ads" else "ads")
        else:
            try:
                token = self.platform_token(platform)
            except RuntimeError:
                token = None

        if not token:
            return []
        # One user, one credential per platform: the list exists so discovery
        # code can merge across accounts, and here there is exactly one to merge.
        return [{"id": "env", "label": platform, "email": "", "token": token}]

    # -- Writes back into the credential store ----------------------------
    def update_platform_token(
        self,
        platform: str,
        connection_id: str,
        access_token: str,
        refresh_token: str = "",
        expires_at: Optional[float] = None,
    ) -> None:
        _store.set(
            f"platform.{(platform or '').strip().lower()}",
            {
                "connection_id": connection_id,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": expires_at,
            },
        )

    # -- Media assets -----------------------------------------------------
    # Generated and staged media are keyed by id in the same local store. The
    # hosted build keeps these in Postgres so a second instance can serve the
    # same asset; a single-instance install has no such reader.
    def create_staged_media_asset(self, **kwargs: Any) -> dict:
        return self._put_asset("staged", kwargs)

    def create_generated_image_asset(self, **kwargs: Any) -> str:
        record = self._put_asset("genimg", kwargs)
        return f"genimg_{record['id']}"

    def get_generated_image_asset(self, asset_id: str) -> Optional[dict]:
        key = (asset_id or "").strip()
        key = key[7:] if key.startswith("genimg_") else key
        return (_store.get("assets", {}) or {}).get(f"genimg:{key}")

    def recent_generated_image_assets(self, limit: int = 12) -> list[dict]:
        assets = (_store.get("assets", {}) or {}).values()
        images = [a for a in assets if a.get("kind") == "genimg"]
        images.sort(key=lambda a: a.get("created_at", 0), reverse=True)
        # Without the bytes: the gallery widget needs ids and captions to build
        # links from, not the payloads.
        return [{k: v for k, v in a.items() if k != "file_bytes_base64"} for a in images[:limit]]

    def _put_asset(self, kind: str, payload: dict) -> dict:
        import secrets
        import uuid

        asset_id = uuid.uuid4().hex
        record = {
            **payload,
            "id": asset_id,
            # Callers read "asset_id" and "access_token" (the staged-media path
            # in tools/meta_ads.py builds a public URL from both), so the shape
            # has to match the hosted backend's even though a single-user
            # install has nobody else to keep out.
            "asset_id": asset_id,
            "access_token": secrets.token_urlsafe(24),
            "kind": kind,
            "created_at": time.time(),
        }
        assets = _store.get("assets", {}) or {}
        assets[f"{kind}:{asset_id}"] = record
        _store.set("assets", assets)
        return record
