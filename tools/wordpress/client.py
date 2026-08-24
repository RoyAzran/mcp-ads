"""WordPress REST client and per-request credential resolution.

Ported from the standalone WordPress MCP server, with three deliberate changes:

1. **No module-level client.** The source cached a `WordPressClient` in a module
   global seeded from `WP_SITE_URL`/`WP_APP_USERNAME`/`WP_APP_PASSWORD` env vars.
   In a multi-tenant server that is a cross-tenant credential leak -- one user's
   site would answer another user's calls. Credentials are resolved per request
   here and never cached across them.

2. **Redirects no longer leak the Authorization header cross-host.** The source
   set `session.rebuild_auth = lambda *_: None` so WordPress trailing-slash 301s
   keep working. That also means a site can 302 to an attacker's host and the
   Application Password goes with it. We keep auth only while the redirect stays
   on the same host.

3. **Site URLs are validated against internal ranges.** Users supply the URL, and
   the server fetches it from inside Cloud Run where the metadata service lives.
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
from contextvars import ContextVar
from typing import Any
from urllib.parse import urlparse

import requests

from auth import current_connection_token_ctx, current_user_ctx
from credentials import connect_hint

# Per-request cache of the resolved credentials. Populated on the first WordPress
# tool call in a request so the rest do not re-hit the database -- Supabase's
# pooler caps total connections across every deploy (see agency_os_api.py).
current_wp_creds_ctx: ContextVar[dict | None] = ContextVar("current_wp_creds", default=None)

_TIMEOUT = 30


# ── Response trimming ────────────────────────────────────────────────────────

_STRIP_KEYS = frozenset({"_links", "_embedded", "guid", "ping_status", "date_gmt", "modified_gmt"})


def _trim(obj: Any) -> Any:
    """Strip WP REST noise fields to reduce tokens returned to the AI."""
    if isinstance(obj, list):
        return [_trim(i) for i in obj]
    if not isinstance(obj, dict):
        return obj
    # Pagination wrapper -- keep totals, trim the data array
    if "data" in obj and ("total" in obj or "pages" in obj):
        return {**obj, "data": _trim(obj["data"])}
    out: dict = {}
    for key, value in obj.items():
        if key in _STRIP_KEYS:
            continue
        # Collapse {rendered, raw, protected} -> just the rendered string
        if isinstance(value, dict) and "rendered" in value:
            out[key] = value["rendered"]
        else:
            out[key] = _trim(value)
    return out


class WordPressBlockedError(RuntimeError):
    """Something in front of the site rejected us before WordPress saw the request.

    Deliberately NOT a subclass of requests.HTTPError. Several tools probe for
    optional features by catching HTTPError and reporting {"supported": false}
    (see `_unsupported_from_http`); a WAF block is not "feature missing", and
    letting it be swallowed there is how it ends up reported as a bad password.
    """

    def __init__(self, message: str, *, status_code: int | None = None,
                 vendor: str | None = None, response: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.vendor = vendor
        self.response = response


def _classify_block(response: Any) -> tuple[str | None, str | None]:
    """Return (vendor, user-facing explanation) if we were blocked, else (None, None).

    WordPress always answers the REST API with JSON, so an HTML body -- or a
    CDN/WAF fingerprint in the headers -- means the request was refused before
    WordPress ever inspected the credentials.

    The edge vendor and the layer that actually did the refusing are two
    different things, and conflating them sends people to the wrong settings
    page. Observed on a real customer site: the 403 carried `CF-Ray` and
    `Server: cloudflare` (so it passed *through* Cloudflare) but the body was a
    plain nginx "403 Forbidden", i.e. the origin host refused it -- telling that
    user to reconfigure Cloudflare would have wasted their time. So only blame
    the edge when the body is recognisably its own block page.
    """
    if response is None or response.status_code not in (401, 403, 406, 429, 503):
        return None, None
    headers = getattr(response, "headers", {}) or {}
    header_names = {k.lower() for k in headers}
    server = str(headers.get("Server", "")).lower()
    content_type = str(headers.get("Content-Type", "")).lower()
    body = (getattr(response, "text", "") or "")[:2000].lower()

    edge = None
    if "cf-ray" in header_names or "cloudflare" in server:
        edge = "Cloudflare"
    elif "x-sucuri-id" in header_names or "sucuri" in server:
        edge = "Sucuri"

    inline = None
    if "wordfence" in body:
        inline = "Wordfence"
    elif edge and any(
        marker in body
        for marker in ("cloudflare", "attention required", "sucuri", "ray id", "cf-error")
    ):
        inline = edge

    looks_like_html = "text/html" in content_type or body.lstrip().startswith(("<html", "<!doctype"))
    if not edge and not inline and not looks_like_html:
        return None, None

    status = response.status_code
    vendor = inline or edge
    if inline:
        cause = f"{inline} blocked it."
    elif edge:
        cause = (
            f"It passed through {edge} but the response is not a {edge} block page, so it was "
            "rejected either there or by the site's own web server -- check host-level rules too."
        )
    else:
        cause = (
            "The site returned an HTML error page instead of JSON, which usually means a "
            "security plugin or the web server rejected it."
        )
    return vendor, (
        f"The request was refused with HTTP {status} before WordPress saw it, so the "
        f"credentials were never checked. {cause} Allow this server's requests to the "
        "WordPress REST API -- on Cloudflare, add a WAF skip rule for the path "
        "/index.php?rest_route=* (or turn off Bot Fight Mode); for a host-level or plugin "
        "block, allowlist the server's IP or that path -- then try again."
    )


def _describe_waf_block(response: Any) -> str | None:
    """Message-only form of `_classify_block`, for callers that just report text."""
    return _classify_block(response)[1]


def _unsupported_from_http(exc: Exception, feature: str) -> dict:
    response = getattr(exc, "response", None)
    return {
        "supported": False,
        "feature": feature,
        "status_code": getattr(response, "status_code", None),
        "reason": str(exc),
    }


# ── URL validation ───────────────────────────────────────────────────────────

_HOSTNAME_RE = re.compile(r"[a-z0-9._-]+|\[[0-9a-f:]+\]")


class SiteUrlError(ValueError):
    """The supplied site URL is unusable or points somewhere we refuse to fetch."""


def normalize_site_url(raw: str) -> str:
    """Reduce a user-supplied site URL to a stable base (scheme://host[:port][/path]).

    Used both to build requests and as the connection's `external_identity`, so
    it must be stable: the same site typed three different ways has to collapse
    to one string or the workspace/platform/identity unique constraint won't
    dedupe and a user ends up with several connections to one site.

    The path is kept, because WordPress is often installed in a subdirectory
    (example.com/blog). Dropping it would silently point every request at the
    domain root -- a different site, or no WordPress at all.
    """
    value = (raw or "").strip()
    if not value:
        raise SiteUrlError("Site URL is required.")
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https"):
        raise SiteUrlError("Site URL must start with http:// or https://")
    if not parsed.hostname:
        raise SiteUrlError("Site URL is missing a hostname.")
    host = parsed.hostname.lower()
    # Reject obvious typos here rather than letting them surface as a confusing
    # DNS failure ("could not resolve my site.com") two steps later.
    if not _HOSTNAME_RE.fullmatch(host):
        raise SiteUrlError(f"{host!r} is not a valid hostname.")
    origin = f"{parsed.scheme}://{host}"
    if parsed.port and not (
        (parsed.scheme == "https" and parsed.port == 443)
        or (parsed.scheme == "http" and parsed.port == 80)
    ):
        origin = f"{origin}:{parsed.port}"
    # Keep a subdirectory install's path, but drop query/fragment and any
    # wp-admin/wp-json suffix a user is likely to paste straight from a browser.
    path = parsed.path.rstrip("/")
    for suffix in ("/wp-admin", "/wp-login.php", "/wp-json", "/index.php"):
        if path.endswith(suffix):
            path = path[: -len(suffix)].rstrip("/")
    return f"{origin}{path}"


def assert_public_url(origin: str) -> None:
    """Refuse URLs that resolve to infrastructure rather than a customer site.

    This is the only endpoint where an authenticated user makes the server issue
    an outbound request to a host of their choosing, from inside Cloud Run --
    where 169.254.169.254 hands out service-account tokens.
    """
    host = urlparse(origin).hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise SiteUrlError(f"Could not resolve {host}.") from exc
    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise SiteUrlError(
                f"{host} resolves to an internal address ({address}). "
                "Enter a publicly reachable WordPress site."
            )


# ── WordPress client ─────────────────────────────────────────────────────────

class WordPressClient:
    """WordPress REST API client -- credentials injected at request time."""

    def __init__(self, url: str, app_username: str, app_password: str, *, allow_redirects: bool = True):
        self._url = (url or "").rstrip("/")
        # .strip() first: a copy-pasted username or password routinely carries a
        # leading/trailing space or a trailing newline (common when pasting from
        # a password manager or the wp-admin page), and Basic Auth treats that as
        # part of the credential -- a byte-for-byte match against a value with
        # invisible whitespace fails with the same generic 401 as a truly wrong
        # password, so this was silently breaking otherwise-correct credentials.
        self._app_username = (app_username or "").strip()
        # WordPress shows Application Passwords in "xxxx xxxx xxxx" form and the
        # spaces are cosmetic; strip all whitespace, not just plain " ", so a
        # pasted tab or newline doesn't end up baked into the password either.
        self._app_password = "".join((app_password or "").split())
        self._allow_redirects = allow_redirects
        if not self._url:
            raise RuntimeError("WordPress site URL is not configured.")
        if not self._app_username or not self._app_password:
            raise RuntimeError("WordPress Application Password credentials are not configured.")

    @property
    def url(self) -> str:
        return self._url

    def _base_url(self) -> str:
        # ?rest_route= works on every site, including ones without pretty
        # permalinks where /wp-json/ 404s.
        return self._url + "/index.php?rest_route="

    def _session(self) -> requests.Session:
        session = requests.Session()
        session.auth = (self._app_username, self._app_password)
        # requests' default User-Agent ("python-requests/x.y") is a well-known
        # bot signature. Sites behind Cloudflare (or any WAF doing UA-based bot
        # filtering) 403 it before the request ever reaches WordPress -- with
        # completely correct credentials never even inspected. Confirmed live:
        # the same call gets a 403 challenge page with the default UA and a 200
        # with a real user object once given a browser-shaped one.
        session.headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        origin_host = urlparse(self._url).hostname or ""

        def _rebuild_auth(prepared_request, response):  # noqa: ANN001 - requests hook signature
            # requests strips the Authorization header on ANY redirect. WordPress
            # legitimately 301s (trailing slash, http->https), so keep auth for
            # same-host hops -- but never hand the password to a different host.
            target_host = urlparse(prepared_request.url).hostname or ""
            if target_host != origin_host:
                prepared_request.headers.pop("Authorization", None)

        session.rebuild_auth = _rebuild_auth
        return session

    def _request(self, method: str, path: str, **kwargs) -> Any:
        trim = kwargs.pop("trim", True)
        response = self._session().request(
            method,
            self._base_url() + path,
            timeout=kwargs.pop("timeout", _TIMEOUT),
            allow_redirects=kwargs.pop("allow_redirects", self._allow_redirects),
            **kwargs,
        )
        # Check for a WAF/CDN refusal before raise_for_status, so every tool gets
        # the real reason instead of a bare "403 Client Error". This used to be
        # checked only in test_connection, which meant a site that connected fine
        # and later got blocked produced unexplained HTTPErrors from 1,800 tools.
        vendor, blocked = _classify_block(response)
        if blocked:
            raise WordPressBlockedError(
                blocked, status_code=response.status_code, vendor=vendor, response=response
            )
        response.raise_for_status()
        payload = response.json()
        total = response.headers.get("X-WP-Total")
        pages = response.headers.get("X-WP-TotalPages")
        if total or pages:
            payload = {"data": payload, "total": int(total or 0), "pages": int(pages or 0)}
        return _trim(payload) if trim else payload

    def test_connection(self) -> dict:
        try:
            info = self.get("/wp/v2/users/me", params={"context": "edit"})
        except WordPressBlockedError as exc:
            # _request already classified this; keep waf_blocked set so
            # connect_wordpress_site does not overwrite it with "wrong password".
            return {"connected": False, "error": str(exc), "waf_blocked": True}
        except requests.HTTPError as exc:
            # A WAF sitting in front of the site (Cloudflare bot protection is the
            # common one) answers 403 with an HTML challenge page before the
            # request ever reaches WordPress. That is indistinguishable from a
            # bad password if you only look at the status code, and reporting it
            # as "credentials rejected" sends the user to regenerate a password
            # that was never the problem. Detect it and say what is actually
            # wrong. Observed live: correct credentials succeed from a residential
            # IP and 403 from a Cloud Run egress IP on the same site.
            blocked = _describe_waf_block(exc.response)
            return {"connected": False, "error": blocked or str(exc), "waf_blocked": bool(blocked)}
        except Exception as exc:  # noqa: BLE001 - reported to the caller verbatim
            return {"connected": False, "error": str(exc)}
        # A 200 alone is not proof we reached WordPress. Anything that answers
        # with JSON qualifies otherwise -- including a host a redirect landed us
        # on -- so require the payload to actually look like a WP user object.
        if not isinstance(info, dict) or not (info.get("id") or info.get("slug")):
            return {"connected": False,
                    "error": "The response did not come from a WordPress REST API."}
        return {"connected": True, "user": info.get("name", ""), "url": self._url}

    def get(self, path: str, params: dict | None = None, **kwargs) -> Any:
        return self._request("GET", path, params=params, **kwargs)

    def post(self, path: str, json: dict | None = None, **kwargs) -> Any:
        return self._request("POST", path, json=json, **kwargs)

    def put(self, path: str, json: dict | None = None, **kwargs) -> Any:
        return self._request("PUT", path, json=json, **kwargs)

    def patch(self, path: str, json: dict | None = None, **kwargs) -> Any:
        return self._request("PATCH", path, json=json, **kwargs)

    def delete(self, path: str, params: dict | None = None, **kwargs) -> Any:
        return self._request("DELETE", path, params=params, **kwargs)

    def upload_media(self, filename: str, data: bytes, mime_type: str) -> Any:
        # Routed through _request so it shares the WAF detection (and anything
        # else added to the one request chokepoint) -- it previously called
        # session.post directly and was the only path that bypassed all of it.
        # trim=False: media objects are returned to callers that read `id` and
        # `source_url`, and _trim would strip `guid` from the payload shape the
        # old direct call produced.
        return self._request(
            "POST",
            "/wp/v2/media",
            data=data,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Type": mime_type,
            },
            timeout=60,
            trim=False,
        )

    # WooCommerce shortcuts
    def woo_get(self, path: str, params: dict | None = None) -> Any:
        return self.get(f"/wc/v3{path}", params=params)

    def woo_post(self, path: str, json: dict | None = None) -> Any:
        return self.post(f"/wc/v3{path}", json=json)

    def woo_put(self, path: str, json: dict | None = None) -> Any:
        return self.put(f"/wc/v3{path}", json=json)

    def woo_delete(self, path: str, params: dict | None = None) -> Any:
        return self.delete(f"/wc/v3{path}", params=params)


# ── Credential resolution ────────────────────────────────────────────────────

def _creds_from_blob(blob: str) -> dict | None:
    """Parse a decrypted connection credential.

    Every platform stores an opaque string in `credential_enc` and reads it from
    the same ContextVar, so a non-WordPress connection can land here. Return None
    rather than raising so the caller can fall through with a clear message.
    """
    try:
        data = json.loads(blob)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("site_url"):
        return None
    return data


def _load_user_wordpress_connections(user_id: str) -> list[dict]:
    """The caller's WordPress connections, in storage spelling.

    The workspace query behind this lives in the credential backend now; this
    wrapper keeps the signature every caller in this package already uses.
    The user_id argument is retained for those callers, but the provider scopes
    to the request principal itself.
    """
    from credentials import provider

    return provider().list_connections("wordpress")


def resolve_wp_creds() -> dict:
    """Resolve the WordPress credentials for the current request.

    Order: per-request cache -> workspace connection -> the user's own
    connections. Raises with an actionable message when it cannot decide.
    """
    cached = current_wp_creds_ctx.get()
    if cached:
        return cached

    blob = current_connection_token_ctx.get(None)
    if blob:
        creds = _creds_from_blob(blob)
        if creds is None:
            raise RuntimeError(
                "The selected connection is not a WordPress connection. "
                "Pick a WordPress site in " + connect_hint() + ", or omit the connection."
            )
        current_wp_creds_ctx.set(creds)
        return creds

    user = current_user_ctx.get(None)
    if user is None:
        raise RuntimeError("No authenticated user in request context.")

    sites = _load_user_wordpress_connections(user.id)
    if not sites:
        raise RuntimeError(
            "No WordPress site is connected. Add one in " + connect_hint() + " "
            "(site URL + Application Password)."
        )
    if len(sites) == 1:
        chosen = sites[0]
    else:
        selected_id = getattr(user, "selected_wordpress_connection_id", None)
        chosen = next((s for s in sites if s["connection_id"] == selected_id), None)
        if chosen is None:
            listing = ", ".join(f'{s["label"]} (id={s["connection_id"]})' for s in sites)
            raise RuntimeError(
                f"{len(sites)} WordPress sites are connected and none is selected. "
                f"Call wordpress_select_site with one of: {listing}"
            )
    current_wp_creds_ctx.set(chosen)
    return chosen


def _wp() -> WordPressClient:
    """Build a client for the current request's WordPress site."""
    creds = resolve_wp_creds()
    return WordPressClient(
        creds["site_url"],
        creds.get("app_username", ""),
        creds.get("app_password", ""),
    )
