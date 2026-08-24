# Changelog

## 1.0.0

First public release. The complete MCP Ads server, extracted from the codebase
that runs mcp-ads.com:

- ~3,100 registered actions across Google Ads, Meta Ads, GA4, Search Console,
  Tag Manager, Google Sheets, WordPress (incl. WooCommerce/Elementor/SEO
  plugins), Snapchat Ads, Microsoft Ads, TikTok Ads, LinkedIn Ads, Meta Ad
  Library and OpenAI/ChatGPT Ads, behind 17 category dispatchers.
- Single-user credential backend: env vars plus an optionally-encrypted local
  store. No database required.
- stdio and streamable-HTTP transports; NPX CLI proxy in `cli/`.
- Google Ads Transparency family ships off by default.
