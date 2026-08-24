"""Shared MCPServer factory and default instance for the full MCP surface."""
import os

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from competitor_ads_app import register_competitor_gallery
from creative_app import register_creative_gallery
from media_app import build_media_apps

# ---------------------------------------------------------------------------
# PostHog MCP analytics — created once at module scope, never per request.
# Guards against missing env vars so the app boots fine without PostHog set.
# ---------------------------------------------------------------------------
_POSTHOG_TOKEN = os.environ.get("POSTHOG_PROJECT_TOKEN", "")
_POSTHOG_HOST = os.environ.get("POSTHOG_HOST", "")

posthog_client = None
if _POSTHOG_TOKEN and _POSTHOG_HOST:
    from posthog import Posthog

    # Tag every event with which deployment produced it. Several deployments
    # can run this same code and report into one analytics project, all
    # sending the same $mcp_server_name -- without this their tool calls are
    # indistinguishable and the dataset collapses into one stream.
    #
    # BRAND_NAME is the same env var a white-label deploy already sets,
    # so this needs no new configuration anywhere.
    posthog_client = Posthog(
        _POSTHOG_TOKEN,
        host=_POSTHOG_HOST,
        super_properties={
            "deployment": os.environ.get("BRAND_NAME", "").strip() or "self-hosted",
            "deployment_url": os.environ.get("APP_URL", "").strip(),
        },
    )
# Unset means analytics off -- deliberate, not an error, so no nag. It would be
# noise on every self-host start, and in stdio transports stderr chatter is one
# bug away from corrupting the protocol stream.

# Enable DNS rebinding protection in production; disable locally for dev
_dns_protection = os.environ.get("DISABLE_DNS_REBINDING_PROTECTION", "").lower() != "true"

# Allowed hostnames for DNS rebinding protection. Loopback by default; a
# deployment behind a domain adds it via ALLOWED_HOSTS.
_allowed_hosts = ["localhost", "127.0.0.1"]
_extra = os.environ.get("ALLOWED_HOSTS", "")
if _extra:
    _allowed_hosts.extend(h.strip() for h in _extra.split(",") if h.strip())


def build_mcp(server_name: str, instructions: str | None = None) -> MCPServer:
    """Build an MCPServer instance.

    `instructions` is returned in the MCP initialize response and injected by
    clients (Claude, ChatGPT) as session-level context. Without it the model
    connects, sees only the category dispatcher tools, and has no idea what sits
    behind them -- which is why it used to claim capabilities did not exist and
    had to be re-taught in every conversation.

    Every surface gets the media-upload App. Extensions are applied at
    construction and then frozen, so this is the only place it can be attached;
    it is what makes the server advertise io.modelcontextprotocol/ui in the
    initialize response, which is the signal a host waits for before it will
    fetch and mount a ui:// resource.
    """
    return MCPServer(
        name=server_name,
        instructions=instructions,
        extensions=[register_competitor_gallery(register_creative_gallery(build_media_apps()))],
    )


def build_http_app(server: MCPServer):
    """The streamable-HTTP ASGI app for a server.

    stateless_http and transport_security moved off the constructor in SDK 2.0,
    so they belong here rather than in build_mcp. Keeping this in one helper
    stops the two surfaces drifting apart on transport settings -- a mismatch
    there shows up as a 421 on one endpoint and not the other.
    """
    return server.streamable_http_app(
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=_dns_protection,
            allowed_hosts=_allowed_hosts,
        ),
    )


mcp = build_mcp("Agency Tools")

def mcp_event_properties(request, extra=None):
    """Record which underlying action ran, not just the category it came from.

    This server publishes ~11 category dispatchers, not its 3,177 tools, so
    every call arrives as tools/call on meta_ads_action or gsc_action with the
    real action buried in the arguments. PostHog therefore recorded
    "meta_ads_action x66" and nothing about which of the 230 Meta tools anyone
    actually used -- which is the only version of the question worth asking.

    The action is lifted out into its own property here. Kept defensive: this
    runs on every captured event, including ones with no arguments at all, and
    an exception in analytics must never surface as a failed tool call.
    """
    try:
        params = (request or {}).get("params") or {}
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return None
        action = str(arguments.get("action") or "").strip()
        if not action:
            return None
        category = str(params.get("name") or "").strip()
        props = {"mcp_action": action}
        if category:
            # Qualified too, because action names are unique per family but the
            # pair is what identifies a call when reading a breakdown.
            props["mcp_category"] = category
            props["mcp_action_full"] = f"{category}:{action}"
        return props
    except Exception:  # noqa: BLE001 - analytics must not break a tool call
        return None


# Instrument the full server with PostHog MCP analytics (idempotent, additive).
mcp_analytics = None
if posthog_client is not None:
    from posthog.mcp import MCPAnalyticsOptions, instrument
    mcp_analytics = instrument(
        mcp, posthog_client,
        MCPAnalyticsOptions(event_properties=mcp_event_properties),
    )
