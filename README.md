# MCP Ads: Google Ads MCP server + Meta Ads MCP server

MCP Ads, the ads MCP server by Roy Azran. Hosted at [mcp-ads.com](https://mcp-ads.com/?utm_source=github&utm_medium=readme&utm_campaign=oss_repo), open source here.

[Website](https://mcp-ads.com/?utm_source=github&utm_medium=readme&utm_campaign=oss_repo) · [Docs](https://mcp-ads.com/docs/quickstart?utm_source=github&utm_medium=readme&utm_campaign=oss_repo) · [Pricing](https://mcp-ads.com/pricing?utm_source=github&utm_medium=readme&utm_campaign=oss_repo) · [MCP Registry listing](https://registry.modelcontextprotocol.io/v0/servers?search=com.mcp-ads/mcp-ads) (`com.mcp-ads/mcp-ads`)

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/RoyAzran/mcp-ads/actions/workflows/ci.yml/badge.svg)](https://github.com/RoyAzran/mcp-ads/actions/workflows/ci.yml)

**Open-source (MIT) MCP server for Google Ads, Meta Ads (Facebook & Instagram),
GA4, Search Console, Tag Manager, TikTok, LinkedIn, Microsoft Ads and more.**
Works with Claude, Claude Code, ChatGPT, Cursor, Codex, Gemini CLI, Grok, or any
MCP client. Read performance, audit wasted spend, build reports, **and** create or
change campaigns. Everything it creates starts paused until you say otherwise.

3,363 registered actions across 21 tool families in the hosted build (September
2026), exposed through one dispatcher per family so your agent loads about two
dozen tool definitions instead of thousands. This repo ships 18 of those
families; see [Tool categories](#tool-categories).

## How it compares

Facts as of September 2026, taken from each product's public pages. Plans and
coverage change, so check their site before deciding.

| | Google's Google Ads MCP | Meta's Ads MCP | Pipeboard | Adspirer | MCP Ads |
|---|---|---|---|---|---|
| Platforms | Google Ads | Meta Ads | Meta, Google, TikTok, LinkedIn, Microsoft, Pinterest, Snap, Reddit, plus GA4 and Search Console | Google, Meta, LinkedIn, TikTok, Amazon, ChatGPT Ads | Google, Meta, TikTok, LinkedIn, Microsoft, Snapchat, X, ChatGPT Ads, plus GA4, Search Console, Tag Manager, Merchant Center, WordPress |
| What it does | Reporting and diagnostics (read-only) | Reporting and campaign management | Ads MCP server across the platforms listed | Hosted agent service that carries out ad tasks | Read and write: 755 Google Ads actions, 230 Meta Ads actions |
| Hosting | Self-host, with your own developer token | Hosted by Meta | Hosted; open-source Meta server also available | Hosted | [Hosted](https://mcp-ads.com/?utm_source=github&utm_medium=readme&utm_campaign=oss_repo) or self-host (this repo, MIT) |
| Pricing | Free software; you supply the API access | Check Meta's site | Free tier; paid plans by number of ad-account slots | Per-task pricing with a small free tier | $49/month flat, 3-day free trial ([see pricing](https://mcp-ads.com/pricing?utm_source=github&utm_medium=readme&utm_campaign=oss_repo)); self-hosting is free |

Use Google's or Meta's server if you only need one platform and want the vendor's
own code. Use this one if you want one server across platforms, write access
to Google Ads, or the option to run the whole thing yourself.

---

## Choose your path

### ☁️ Cloud — connected in 2 minutes

Go to **[mcp-ads.com](https://mcp-ads.com?utm_source=github&utm_medium=readme&utm_campaign=oss_repo)**, sign in, OAuth your ad accounts
in the browser, and paste one URL into Claude. Done.

- No API applications, no developer tokens, no app reviews — the platform
  approvals (Google Ads developer token, Meta App Review, and the rest) are
  already in place
- $49/month flat for every platform, with a 3-day free trial
  ([see pricing](https://mcp-ads.com/pricing?utm_source=github&utm_medium=readme&utm_campaign=oss_repo) for the current offer)
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

**Same code either way.** The hosted product is this server plus multi-user
auth, the approvals already done, and three families not in this repo yet
(Merchant Center, X Ads, Bing Webmaster Tools).

---

## Connect to your AI client

The hosted endpoint is the same for every client:

```
https://mcp-ads.com/mcp-slim
```

Sign-in is OAuth in the browser on first use; there is no API key to paste.
For a self-hosted server, use the stdio config under
[Self-hosted quick start](#self-hosted-quick-start) instead; the note under
each client says where it goes.

**Claude.ai and Claude Desktop.** Settings → Connectors → Add custom connector,
paste the URL above, then approve the sign-in window.
([Guide](https://mcp-ads.com/docs/ai-clients/claude?utm_source=github&utm_medium=readme&utm_campaign=oss_repo))
Self-hosted: Claude Desktop takes the `command`/`args` block in
`claude_desktop_config.json`.

**Claude Code.**

```bash
claude mcp add --transport http mcp-ads https://mcp-ads.com/mcp-slim
```

The first tool call opens the browser sign-in. ([Guide](https://mcp-ads.com/docs/ai-clients/claude-code?utm_source=github&utm_medium=readme&utm_campaign=oss_repo))
Self-hosted: `claude mcp add mcp-ads -- python /absolute/path/to/mcp-ads/server.py`,
or the same block in `.mcp.json`.

**ChatGPT.** Custom connectors need Developer mode:

1. Settings → Plugins → Browse plugins, and turn Developer mode on (one time).
2. Settings → Connectors → Add custom connector. Name it "MCP Ads" and paste
   the URL above.
3. Sign in with the Google identity that has access to your ad accounts.
4. In a new chat, open the tools menu and switch the connector on. ChatGPT
   keeps connectors off per chat by default.

([Guide with a recording](https://mcp-ads.com/docs/ai-clients/chatgpt?utm_source=github&utm_medium=readme&utm_campaign=oss_repo))

**Cursor.** Add this to `~/.cursor/mcp.json` (or `.cursor/mcp.json` in a
project) and restart Cursor:

```json
{
  "mcpServers": {
    "mcp-ads": {
      "url": "https://mcp-ads.com/mcp-slim"
    }
  }
}
```

([Guide](https://mcp-ads.com/docs/ai-clients/cursor?utm_source=github&utm_medium=readme&utm_campaign=oss_repo)) Self-hosted: the same file takes the
`command`/`args` block instead of `url`.

**Codex.** Add `[mcp_servers.mcp-ads]` with `url = "https://mcp-ads.com/mcp-slim"`
to `~/.codex/config.toml`. ([Guide](https://mcp-ads.com/docs/ai-clients/codex?utm_source=github&utm_medium=readme&utm_campaign=oss_repo))

**Gemini CLI.** Add an `mcp-ads` entry with the URL to `~/.gemini/settings.json`
and run `/mcp` to check it. ([Guide](https://mcp-ads.com/docs/ai-clients/gemini-cli?utm_source=github&utm_medium=readme&utm_campaign=oss_repo))

**Grok.** On grok.com: + → Connectors → New Connector → Custom, paste the URL
and finish the sign-in. ([Guide](https://mcp-ads.com/docs/ai-clients/grok?utm_source=github&utm_medium=readme&utm_campaign=oss_repo))

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
| **Search Console** | 22 | Same Google token | Minutes |
| **Tag Manager** | 64 | Same Google token | Minutes |
| **WordPress** | 1,843 | Site URL + Application Password | Minutes |
| **Google Ads** | 755 | Above **plus a developer token** (Google reviews the application) | 1–3 weeks |
| **Meta Ads** | 230 | A Meta app; `ads_management`/`ads_read` need **App Review** (works for the app's own admins/testers before approval) | Days for yourself, weeks for App Review |
| **Snapchat Ads** | 12 | Self-serve OAuth app in Business Manager | Hours |
| **Microsoft Ads** | 11 | Entra OAuth app + developer token (sandbox instant, production reviewed) | Hours–days |
| **TikTok Ads** | 35 | Business API app review | Weeks |
| **LinkedIn Ads** | 26 | **Marketing Developer Platform approval** — routinely refused to individuals | Often unobtainable |
| **Meta Ad Library** | 7 | Token from a Meta-ID-verified identity | Days |
| **Ads Transparency** | 7 | Nothing — but read the note below | Off by default |
| **Creative generation** | 7 | Your OpenAI API key | Minutes |

If that table makes you tired, that's exactly what
**[the cloud version](https://mcp-ads.com?utm_source=github&utm_medium=readme&utm_campaign=oss_repo)** is for — the approvals are done,
you just connect your accounts.

## Tool categories

One dispatcher per family. Counts are registered actions in the hosted build
(September 2026); each dispatcher links to its API reference page.

| Dispatcher | Actions | Example actions |
|---|---|---|
| [`google_ads_action`](https://mcp-ads.com/docs/api-reference/google-ads?utm_source=github&utm_medium=readme&utm_campaign=oss_repo) | 755 | `google_ads_search_terms`, `google_ads_add_negative_keywords`, `google_ads_update_campaign_budget` |
| [`meta_ads_action`](https://mcp-ads.com/docs/api-reference/meta-ads?utm_source=github&utm_medium=readme&utm_campaign=oss_repo) | 230 | `meta_ads_campaign_performance`, `meta_ads_frequency`, `meta_ads_update_adset_budget` |
| [`ga4_action`](https://mcp-ads.com/docs/api-reference/ga4?utm_source=github&utm_medium=readme&utm_campaign=oss_repo) | 95 | `ga4_landing_pages`, `ga4_diagnose_missing_conversions`, `ga4_run_custom_report` |
| [`gsc_action`](https://mcp-ads.com/docs/api-reference/gsc?utm_source=github&utm_medium=readme&utm_campaign=oss_repo) | 22 | `gsc_top_keywords`, `gsc_inspect_url`, `gsc_submit_sitemap` |
| [`gtm_action`](https://mcp-ads.com/docs/api-reference/gtm?utm_source=github&utm_medium=readme&utm_campaign=oss_repo) | 64 | `gtm_audit_container`, `gtm_setup_google_ads_conversion`, `gtm_publish_workspace_now` |
| [`tiktok_ads_action`](https://mcp-ads.com/docs/api-reference/tiktok-ads?utm_source=github&utm_medium=readme&utm_campaign=oss_repo) | 35 | `tiktok_ads_campaign_report`, `tiktok_ads_create_adgroup`, `tiktok_ads_upload_video` |
| [`linkedin_ads_action`](https://mcp-ads.com/docs/api-reference/linkedin-ads?utm_source=github&utm_medium=readme&utm_campaign=oss_repo) | 26 | `linkedin_ads_campaign_report`, `linkedin_ads_find_targeting_entities`, `linkedin_ads_update_campaign` |
| [`microsoft_ads_action`](https://mcp-ads.com/docs/api-reference/microsoft-ads?utm_source=github&utm_medium=readme&utm_campaign=oss_repo) | 11 | `microsoft_ads_campaign_performance`, `microsoft_ads_list_keywords`, `microsoft_ads_update_campaign_budget` |
| [`snapchat_ads_action`](https://mcp-ads.com/docs/api-reference/snapchat-ads?utm_source=github&utm_medium=readme&utm_campaign=oss_repo) | 12 | `snapchat_ads_campaign_report`, `snapchat_ads_create_campaign`, `snapchat_ads_update_campaign_status` |
| [`openai_ads_action`](https://mcp-ads.com/docs/api-reference/openai-ads?utm_source=github&utm_medium=readme&utm_campaign=oss_repo) (ChatGPT Ads) | 15 | `openai_ads_ad_insights`, `openai_ads_create_campaign`, `openai_ads_set_ad_status` |
| [`organic_social_action`](https://mcp-ads.com/docs/api-reference/organic-social?utm_source=github&utm_medium=readme&utm_campaign=oss_repo) | 32 | `meta_pages_create_post`, `linkedin_organic_create_post`, `tiktok_organic_publish_video` |
| [`meta_ad_library_action`](https://mcp-ads.com/docs/api-reference/meta-ad-library?utm_source=github&utm_medium=readme&utm_campaign=oss_repo) | 7 | `meta_ad_library_search_ads`, `meta_ad_library_advertiser_ads`, `meta_ad_library_creative_angles` |
| [`google_ads_transparency_action`](https://mcp-ads.com/docs/api-reference/google-ads-transparency?utm_source=github&utm_medium=readme&utm_campaign=oss_repo) | 7 | `google_ads_transparency_find_advertiser`, `google_ads_transparency_advertiser_ads`, `google_ads_transparency_creative_mix` |
| [`creative_action`](https://mcp-ads.com/docs/api-reference/creative?utm_source=github&utm_medium=readme&utm_campaign=oss_repo) | 7 | `creative_generate_ad_images`, `creative_start_image_generation`, `creative_get_image_generation_result` |
| [`reports_action`](https://mcp-ads.com/docs/api-reference/reports?utm_source=github&utm_medium=readme&utm_campaign=oss_repo) | 8 | `report_monthly_executive`, `report_paid_search_full`, `report_new_client_audit` |
| [`agency_action`](https://mcp-ads.com/docs/api-reference/agency?utm_source=github&utm_medium=readme&utm_campaign=oss_repo) | 20 | `agency_wasted_spend_report`, `agency_budget_pacing_all_channels`, `agency_cross_platform_overview` |
| [`wordpress_action`](https://mcp-ads.com/docs/api-reference/wordpress?utm_source=github&utm_medium=readme&utm_campaign=oss_repo) | 1,843 | `a11y_audit_page`, `cf7_list_forms`, `backup_updraft_run` |
| [`misc_action`](https://mcp-ads.com/docs/api-reference/misc?utm_source=github&utm_medium=readme&utm_campaign=oss_repo) | 8 | `competitor_ads_gallery`, `media_upload_start`, `creative_show_images` |
| [`merchant_action`](https://mcp-ads.com/docs/api-reference/merchant?utm_source=github&utm_medium=readme&utm_campaign=oss_repo) (hosted only) | 123 | `merchant_list_products`, `merchant_product_status_summary`, `merchant_account_issues` |
| [`x_ads_action`](https://mcp-ads.com/docs/api-reference/x-ads?utm_source=github&utm_medium=readme&utm_campaign=oss_repo) (hosted only) | 23 | `x_ads_account_stats`, `x_ads_create_campaign`, `x_ads_campaign_reach` |
| [`bing_webmaster_action`](https://mcp-ads.com/docs/api-reference/bing-webmaster?utm_source=github&utm_medium=readme&utm_campaign=oss_repo) (hosted only) | 20 | `bing_webmaster_top_queries`, `bing_webmaster_crawl_issues`, `bing_webmaster_submit_url` |

Total: 3,363. A self-hosted build registers the same families except the three
marked hosted only; organic social has 12 actions here rather than 32, and a
few other counts differ by a handful as actions are added or hidden.
`list_actions` always reports what your build actually has.

## How the tool surface works

Your agent sees one dispatcher per platform plus `list_actions`:

```
1. list_actions(category="meta_ads_action", search="budget")
2. meta_ads_action(action="<name from step 1>", params={...})
```

- **Write actions create everything paused** unless explicitly told to launch
  (see [Safety model](#safety-model))
- `--surface full` registers every action as its own MCP tool instead —
  useful for programmatic clients, far too many tools for a chat client

## Example prompts

- "Which search terms spent over $50 with no conversions last month? Add the
  worst ten as exact-match negatives."
- "Pause ad sets with frequency above 3 in my main Meta account."
- "Compare Meta and Google CPA for last week against GA4 conversions."
- "Which landing pages get paid traffic but convert below the site average?"
- "Build a paused Search campaign for 'emergency plumber' with three ad groups
  and a $50 daily budget."
- "What are my competitors running on Facebook and Instagram right now?"
- "Check my GTM container for paused or unused tags."
- "Write this month's executive report across Google Ads, Meta and GA4."

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

## Safety model

- **Approval before writes.** Each dispatcher carries MCP tool annotations:
  a family with any write action is marked `readOnlyHint: false`,
  `destructiveHint: true`. The check fails closed, so hidden write actions
  count too, and paid image generation is never marked read-only. Clients such
  as Claude and ChatGPT use those hints to ask you before the call runs. The
  server's own instructions also tell the model to state a write plainly and
  get confirmation before it touches live spend or public content.
- **Created paused.** Campaign, ad set, ad group and ad creation default to
  `PAUSED` on Google Ads and Meta. Nothing starts spending until you ask for it
  to be enabled.
- **Read/write role gating.** `permissions.py` lists the write tools in
  `WRITE_TOOLS`, and a name heuristic fails closed for write verbs missing from
  that list. Write tools call `require_editor()`, which refuses the `viewer`
  role; only `editor` and `admin` can write. On the hosted service, a viewer
  seat gets that refusal.
- **Running read-only when self-hosted.** There is no config switch yet. The
  self-host build has one user, `SelfHostPrincipal` in `credentials_env.py`,
  whose role is `admin`. Change that `role` to `"viewer"` and every write tool
  refuses. For a second layer, use read-only credentials where the platform
  offers them: a Meta token with only `ads_read`, and the read-only GA4 and
  Search Console scopes (`analytics.readonly`, `webmasters.readonly`). The
  Google Ads API has no read-only scope, so for Google Ads the role change is
  the control.

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

## FAQ

**Is there a Google Ads MCP server that can make changes?**
Yes, this one. Google's official Google Ads MCP server is read-only (reporting
and diagnostics). MCP Ads has 755 Google Ads actions, including creating
campaigns, ad groups, keywords and responsive search ads, adding negatives, and
changing budgets and bids. New campaigns are created paused.

**Does it work with ChatGPT?**
Yes. Add it as a custom connector in Developer mode with the URL
`https://mcp-ads.com/mcp-slim`; the steps are under
[Connect to your AI client](#connect-to-your-ai-client) and in the
[ChatGPT guide](https://mcp-ads.com/docs/ai-clients/chatgpt?utm_source=github&utm_medium=readme&utm_campaign=oss_repo).

**Do I need a developer token?**
Not for the hosted version: its Google Ads developer token and the other
platform approvals are already in place, and you only sign in. For self-hosted
Google Ads, yes: you need your own Google Ads API developer token, and Google
reviews the application (see
[What works out of the box](#what-works-out-of-the-box-honestly)).

## License

MIT.
