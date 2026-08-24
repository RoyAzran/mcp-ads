# Security

This server holds live credentials for advertising accounts — tokens that can
read business data and spend real budgets. Treat a deployment of it like a
password manager, not like a web app.

## Model

- All credentials live in your `.env` and, for tokens the server writes back
  (OAuth refreshes), in `$MCP_ADS_HOME/credentials.json` — Fernet-encrypted
  when `MCP_ADS_SECRET_KEY` is set, plaintext with 0600 permissions otherwise.
- stdio transport has no network surface at all.
- HTTP transport is unauthenticated and loopback-only by default. Binding a
  public interface requires an explicit `MCP_ADS_ALLOW_PUBLIC_BIND=true`, and
  you are expected to put your own authentication in front. `MCP_ADS_API_KEY`
  protects the `/api/cli/*` routes with a shared secret.
- No telemetry unless you configure your own PostHog project.

## Reporting a vulnerability

Open a GitHub security advisory on this repository (Security tab → Report a
vulnerability), or email the maintainer address on the GitHub profile. Please
do not open a public issue for anything that could expose users' ad-account
credentials before a fix ships.
