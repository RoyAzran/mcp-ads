# MCP Ads plugin

An agent plugin that adds the hosted [MCP Ads](https://mcp-ads.com) server and
an **Ads manager** skill. Works in Cursor, Grok Bot, Grok Build and any client
that reads the [agent plugin](https://agent-plugins.org) format.

- `mcp.json` — the server: `https://mcp-ads.com/mcp-slim`, one tool per
  platform, OAuth sign-in on first use.
- `skills/ads-manager/SKILL.md` — how to behave with live ad accounts: discover
  before guessing, read before writing, and every write waits for approval.

## Install

**Cursor** — add from the marketplace, or one click:

```
cursor://anysphere.cursor-deeplink/mcp/install?name=mcp-ads&config=eyJ0eXBlIjoiaHR0cCIsInVybCI6Imh0dHBzOi8vbWNwLWFkcy5jb20vbWNwLXNsaW0ifQ==
```

**Grok Bot** — Settings → Plugins. Grok Bot shares Cursor's plugin catalogue,
so a marketplace install appears there too. If you add the server by hand
instead, its custom-server form takes a URL and headers only: use
`https://mcp-ads.com/mcp-slim` and an `Authorization: Bearer mcp_live_...`
header, with a key from Manage → Connect MCP → API keys in the MCP Ads
dashboard. That is the reliable path while Grok Bot's OAuth for custom servers
is broken (September 2026). Full walkthrough:
https://mcp-ads.com/docs/ai-clients/grok-bot

**Grok Build**

```
grok mcp add mcp-ads https://mcp-ads.com/mcp-slim
```

**Claude Code**

```
claude mcp add --transport http mcp-ads https://mcp-ads.com/mcp-slim
```

## What the server needs

An MCP Ads account (free plan, no card) with at least one ad platform connected
in the dashboard. The plugin carries no credentials; the sign-in or the API
key is yours, and platform grants stay on the server.
