# MCP Ads

**The open-source marketing MCP server.** Run Google Ads, Meta Ads, GA4, Search
Console, Tag Manager, WordPress and more from Claude, ChatGPT, Cursor, or any
MCP client — read performance, audit wasted spend, build reports, and launch
campaigns (created paused until you say otherwise).

~3,100 registered actions across 12+ platforms, exposed through 17 category
dispatchers so your agent loads ~20 tool definitions instead of thousands.

---

## Choose your path

### ☁️ Cloud — connected in 2 minutes, free tier

Go to **[mcp-ads.com](https://mcp-ads.com)**, sign in, OAuth your ad accounts
in the browser, and paste one URL into Claude. Done.

- No API applications, no developer tokens, no app reviews — the platform
  approvals (Google Ads developer token, Meta App Review, and the rest) are
  already in place
- Free tier: 30 tool calls/month, every platform. Paid plans for unlimited use
- Your credentials stored encrypted; revoke any time from the dashboard

This is the right choice for almost everyone who wants to *use* the tools
rather than *host* them.

### 🔧 Self-hosted — free forever, your keys, your machine

Clone this repo and run it with your own platform credentials. MIT licensed,
no strings, no phone-home. Your tokens never leave your machine.

The honest cost: several of the ad platforms make you apply for API access
(see the table below). If you already have those credentials — you're an
agency with a Google Ads developer token, a developer with a reviewed Meta
app — self-hosting takes minutes. If you don't, budget days to weeks for the
platforms' own approval queues, or just use the cloud.

**Same code, same actions, either way.** The hosted product is this exact
server plus multi-user auth and the approvals already done.

---

## Self-hosted quick start

```bash
git clone https://github.com/RoyAzran/mcp-ads.git
cd mcp-ads
pip install -r requirements.txt
cp .env.example .env      # fill in the platforms you use
python server.py doctor   # shows what's connected
```

Then add it to **Claude Desktop** (`claude_desktop_config.json`) or
**Claude Code** (`.mcp.json`):

```json
{
  "mcpServers": {
    "mcp-ads": {
      "command": "python",
      "args": ["/absolute/path/to/mcp-ads/server.py"]
    }
  }
}
```

That's the whole install. Ask your agent *"audit my Google Ads wasted spend"*
and watch it go.

**Docker instead:**

```bash
cp .env.example .env
docker compose up -d      # HTTP server on 127.0.0.1:8000, MCP endpoint at /mcp
```

**HTTP mode without Docker:** `python server.py serve --transport http`

**OAuth helper** for the bearer-token platforms (opens a browser, stores and
auto-refreshes the token):

```bash
python server.py auth snapchat_ads
```

## What works out of the box, honestly

Self-hosting means holding your own keys to each platform. Some hand them
over in minutes; some make you apply. This is the real picture:

| Platform | Actions | What you need | Realistic effort |
|---|---|---|---|
| **GA4** | 95 | Google OAuth client + refresh token | Minutes–hours |
| **Search Console** | 23 | Same Google token | Minutes |
| **Tag Manager** | 64 | Same Google token | Minutes |
| **Google Sheets** | 69 | Same Google token | Minutes |
| **WordPress** | ~1,800 | Site URL + Application Password | Minutes |
| **Google Ads** | ~755 | Above **plus a developer token** (Google reviews the application) | 1–3 weeks |
| **Meta Ads** | 234 | A Meta app; `ads_management`/`ads_read` need **App Review** (works for the app's own admins/testers before approval) | Days for yourself, weeks for App Review |
| **Snapchat Ads** | 12 | Self-serve OAuth app in Business Manager | Hours |
| **Microsoft Ads** | 11 | Entra OAuth app + developer token (sandbox instant, production reviewed) | Hours–days |
| **TikTok Ads** | 35 | Business API app review | Weeks |
| **LinkedIn Ads** | 26 | **Marketing Developer Platform approval** — routinely refused to individuals | Often unobtainable |
| **Meta Ad Library** | 7 | Token from a Meta-ID-verified identity | Days |
| **Ads Transparency** | 7 | Nothing — but read the note below | Off by default |
| **Creative generation** | 7 | Your OpenAI API key | Minutes |

If that table makes you tired, that's exactly what
**[the cloud version](https://mcp-ads.com)** is for — the approvals are done,
you just connect your accounts.

## How the tool surface works

Your agent sees one dispatcher per platform plus `list_actions`:

```
1. list_actions(category="meta_ads_action", search="budget")
2. meta_ads_action(action="<name from step 1>", params={...})
```

- **Write actions create everything paused** unless explicitly told to launch
- Role gating (`permissions.py`) separates read from write tools by name
- `--surface full` registers every action as its own MCP tool instead —
  useful for programmatic clients, far too many tools for a chat client

## The NPX CLI

No Python on the client machine? The CLI is a ~300-line stdio proxy
([`cli/`](cli/)) that forwards to any MCP Ads server:

```bash
# against your self-hosted server
MCP_ADS_BASE_URL=http://127.0.0.1:8000 npx -y @marketingmcp/cli

# against the cloud (API key from mcp-ads.com/manage)
MCP_ADS_API_KEY=mcp_live_xxx npx -y @marketingmcp/cli
```

## A note on Google Ads Transparency

`tools/google_ads_transparency.py` reads Google's public Ads Transparency
Center by calling the endpoints the website itself uses. Google documents no
API for this data; the endpoints can change or start refusing without notice,
and automated access to them is not something Google has blessed. The family
is **off by default** — set `GOOGLE_ADS_TRANSPARENCY_ENABLED=true` only after
reading that module's docstring. The Meta Ad Library tools use Meta's
official API and carry no such caveat.

## Security posture

- Credentials come from your `.env` and an optional Fernet-encrypted local
  store (`MCP_ADS_SECRET_KEY`). Nothing phones home: telemetry is entirely
  opt-in and off unless you configure your own analytics project.
- HTTP mode is unauthenticated by design (it's you, on loopback). The server
  **refuses to bind a public interface** unless you set
  `MCP_ADS_ALLOW_PUBLIC_BIND=true`, because it holds credentials that can
  spend real money.
- CI runs a boundary gate (`tests/test_no_private_imports.py`) that fails on
  anything shaped like a secret entering the tree.

See [SECURITY.md](SECURITY.md) for reporting.

## Contributing

Issues and PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). The check
scripts in `scripts/` are the review bar: each one pins a failure that
actually happened in production, and
`python scripts/check_tools_execute.py` before a PR catches most of what
review would.

One thing we can't help with: obtaining platform API access. Those approval
queues belong to Google, Meta, LinkedIn, TikTok and Microsoft.

## License

MIT.
