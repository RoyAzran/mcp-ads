"""The credential seam between the tools and wherever credentials actually live.

Everything under tools/ needs the same handful of things: a refresh token for a
Google service, an access token for a Meta ad account, a bearer token for one of
the plain-OAuth ad platforms, a WordPress site's application password, and a
place to stash generated media. Today each tool reaches for those through
`database` and `tools/account_resolution`, which means every tool transitively
depends on SQLAlchemy, a Postgres URL, a Fernet key, and a multi-tenant
workspace/membership model.

That dependency is the only thing standing between this tool library and a
single-user install that needs none of it. So the lookups move behind this
protocol, and the storage moves behind a backend:

    credentials_db.py   multi-tenant, Postgres, workspace-scoped   (hosted)
    credentials_env.py  one user, env vars + a local encrypted file (self-host)

Two rules make this work in practice:

1. No method takes a user_id. The provider reads the principal off
   `current_user_ctx` itself. A signature that threaded user_id through would
   force the self-host backend to invent one, and force every call site to know
   whether it was in a multi-tenant world -- which is exactly the coupling being
   removed.

2. The return shapes are the ones the tools already handle, sentinels included.
   `MANY` in particular is load-bearing: callers switch on it to mean "several
   accounts are connected and you did not say which", and they have merge logic
   behind that branch. Collapsing it to None would silently route every
   discovery call to whichever account happened to connect first.
"""
from __future__ import annotations

import os
from typing import Any, Optional, Protocol, runtime_checkable

# Re-exported so tools import their request context from one place. The public
# build ships an auth.py that defines these with a non-None default; the hosted
# build keeps the JWT-backed one. Both satisfy `from auth import current_user_ctx`.
from auth import current_connection_token_ctx, current_user_ctx  # noqa: F401


# Sentinel: 2+ connections exist for this platform and no account id was given,
# so there is no single right answer. Preserved verbatim from
# tools/account_resolution.MANY -- callers already branch on it.
MANY = "__MANY__"

# Sentinel returned by platform_token() when this deployment reaches the
# upstream API through a broker rather than with a bearer token of its own.
# tools/platform_http.py sees it and hands the call to a transport registered at
# startup. It exists so the hosted build can keep its managed-auth proxy without
# the public build carrying any of that code.
PROXY = "__PROXY__"


@runtime_checkable
class Principal(Protocol):
    """Everything tools/ actually touches on the current user.

    Verified against all 45 tool modules: four members, nothing else. Keeping
    this list honest is what lets the self-host backend hand over a small object
    instead of a SQLAlchemy row.
    """

    # 38 call sites.
    id: str
    # permissions.require_editor().
    role: str
    # Read in tools/wordpress/client.py and sites.py -- and ASSIGNED in
    # sites.py, so a backend must expose this as a settable property, not a
    # plain attribute copied out of storage.
    selected_wordpress_connection_id: Optional[str]

    def get_meta_token(self) -> Optional[str]:
        ...


class CredentialProvider(Protocol):
    """How tools/ asks for credentials, regardless of where they are kept."""

    # -- Google family ----------------------------------------------------
    def google_token(self, service: str, account_id: str = "") -> Optional[str]:
        """Refresh token for one Google service.

        `service` is one of google_ads, ga4, gsc, sheets, gtm, drive.

        Collapses two lookups the tools currently do in sequence: the
        multi-connection resolution in tools/account_resolution.py and the
        legacy single-token fallback in database.get_google_token_for_service.
        Callers stopped needing to know the difference. May return MANY.
        """

    # -- Meta -------------------------------------------------------------
    def meta_token(self, account_id: str = "", surface: str = "ads") -> Optional[str]:
        """Access token for Meta.

        `surface` is "ads" or "pages". Pages are deliberately a separate login
        from Ads -- bundling the publishing permissions into the Ads consent got
        that dialog rejected outright -- so a Page lookup has to consider both
        connections. May return MANY.
        """

    # -- Plain-bearer ad platforms ----------------------------------------
    def platform_token(self, platform: str, connection_id: str = "") -> str:
        """Bearer token for LinkedIn, TikTok, Snapchat, Microsoft, Meta Pages.

        Returns PROXY when this deployment brokers the call instead. Refreshing
        an expired token happens here rather than at the HTTP layer, so a
        backend is free to refresh, hold a permanent token, or broker, without
        tools/platform_http.py knowing which.
        """

    def api_key(self, platform: str) -> Optional[str]:
        """A user-supplied third-party API key (e.g. OpenAI, for creative)."""

    # -- WordPress --------------------------------------------------------
    def wordpress_connection(self, connection_id: str = "") -> Optional[dict]:
        """{"id", "site_url", "username", "app_password", "label"} or None."""

    def select_wordpress_connection(self, connection_id: str) -> None:
        """Remember which site later calls default to."""

    # -- Discovery --------------------------------------------------------
    def list_connections(self, platform: str) -> list[dict]:
        """Every connection for a platform: {"id", "label", "email", "token"}.

        Discovery calls merge results across all of them, which is why this
        returns tokens and not just labels.
        """

    # -- Writes back into the credential store ----------------------------
    def update_platform_token(
        self,
        platform: str,
        connection_id: str,
        access_token: str,
        refresh_token: str = "",
        expires_at: Optional[float] = None,
    ) -> None:
        """Persist a refreshed token. TikTok's expire in about 24 hours, so this
        is a normal operation, not an administrative one."""

    def legacy_meta_token_for_user(self, user_id: str) -> Optional[str]:
        """The stored Meta token for a user named by id, outside any request.

        Exists for exactly one caller: the media-upload delivery route, which
        runs from a signed URL with no authenticated principal in context and
        must still act as the user who staged the upload. Everything else
        reads the principal off the ContextVar and never needs this.
        """

    # -- Media assets -----------------------------------------------------
    def create_staged_media_asset(self, **kwargs: Any) -> dict: ...

    def create_generated_image_asset(self, **kwargs: Any) -> str:
        """Returns the asset id, "genimg_<id>" -- a string, matching what
        database.create_generated_image_asset has always returned. Callers
        embed it straight into links."""
    def get_generated_image_asset(self, asset_id: str) -> Optional[dict]: ...
    def recent_generated_image_assets(self, limit: int = 12) -> list[dict]: ...


def connect_hint() -> str:
    """Where to send the user when a platform is not connected.

    Baked into two dozen error messages, so it is a function of the deployment:
    the hosted build points at its connections page, a self-host install points
    at its .env, and MCP_ADS_CONNECT_HINT overrides both -- which is also how
    the hosted deploy turns every not-connected error into a link to itself.
    """
    hint = os.environ.get("MCP_ADS_CONNECT_HINT", "").strip()
    if hint:
        return hint
    return "/manage" if _default_backend() == "db" else "your .env file (see the README)"


_provider: Optional[CredentialProvider] = None


def register_provider(instance: CredentialProvider) -> None:
    """Install a backend explicitly.

    The hosted app calls this at startup rather than relying on the env var, so
    that a missing or misspelled MCP_ADS_BACKEND cannot silently downgrade a
    multi-tenant deployment to the single-user backend. That failure would not
    be loud -- it would just serve the wrong person's accounts.
    """
    global _provider
    _provider = instance


def _default_backend() -> str:
    """Pick a backend from what is actually installed, not from configuration.

    An env var would have to be set correctly on every hosted deploy and every
    CI harness, and the failure mode when it is not is the bad one: the server
    starts fine and quietly reads credentials from the wrong place. Nothing
    raises, because both backends are valid.

    Presence is a better signal than configuration. The multi-tenant backend
    only exists in the build that has a database to talk to; the public build
    ships credentials_env.py and no credentials_db.py, so it cannot pick wrong.
    MCP_ADS_BACKEND still overrides, for the one case that needs it: running the
    single-user path inside this repo to reproduce a self-host bug.
    """
    override = (os.environ.get("MCP_ADS_BACKEND") or "").strip().lower()
    if override in ("db", "env"):
        return override

    import importlib.util

    return "db" if importlib.util.find_spec("credentials_db") is not None else "env"


def provider() -> CredentialProvider:
    """The active backend, selected once and cached."""
    global _provider
    if _provider is not None:
        return _provider

    if _default_backend() == "db":
        from credentials_db import DatabaseCredentialProvider

        _provider = DatabaseCredentialProvider()
    else:
        from credentials_env import EnvCredentialProvider

        _provider = EnvCredentialProvider()
    return _provider
