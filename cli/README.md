# @marketingmcp/cli

Local stdio MCP server for [MCP Ads](https://github.com/RoyAzran/mcp-ads). Use
it with Claude Code, Cursor, Cline, Continue, Windsurf, or any MCP-compatible
agent — no Python needed on the client machine.

It works against **either** backend:

## Cloud (mcp-ads.com)

1. **Get an API key** at <https://mcp-ads.com/manage> (CLI API Keys section).
2. **Connect your accounts** (Google Ads, GA4, GSC, Meta Ads, …) on the same
   dashboard — no platform API applications needed on your side.
3. Run:

```bash
MCP_ADS_API_KEY=mcp_live_YOUR_KEY npx -y @marketingmcp/cli
```

## Self-hosted (this repo's server)

Run [the open-source server](https://github.com/RoyAzran/mcp-ads) in HTTP mode
(`python server.py serve --transport http`), then point the CLI at it:

```bash
MCP_ADS_BASE_URL=http://127.0.0.1:8000 npx -y @marketingmcp/cli
```

No API key needed on loopback. If you set `MCP_ADS_API_KEY` on the server,
pass the same value to the CLI.

## How it works

The CLI exposes ~17 **category meta-tools** (one per platform area) plus a
`list_actions` discovery tool. Each tool call is forwarded over HTTP to the
backend's `/api/cli/dispatch`, which resolves the right credentials and runs
the action.

This means:

- Your AI agent loads ~18 tool definitions instead of 1,000+ — dramatically
  less context spent on tool listing.
- Credentials stay on the server — the CLI never sees them.
- Cloud keys revoked at <https://mcp-ads.com/manage> stop working immediately.

## Config

| Source | Variable | Default |
|---|---|---|
| Env | `MCP_ADS_API_KEY` | — (required for cloud; optional self-hosted) |
| Env | `MCP_ADS_BASE_URL` | `https://mcp-ads.com` |
| CLI flag | `--api-key, -k` | (overrides env) |
| CLI flag | `--base-url` | (overrides env) |

## Development

```bash
npm install
npm run build       # produces bin/marketingmcp.js
npm run typecheck
```

To smoke-test against a local backend:

```bash
MCP_ADS_BASE_URL=http://localhost:8000 node bin/marketingmcp.js
```

Or drive it from the [MCP Inspector](https://github.com/modelcontextprotocol/inspector):

```bash
npx @modelcontextprotocol/inspector node bin/marketingmcp.js
```
