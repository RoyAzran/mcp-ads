"""Run MCP Ads as your own server.

    python server.py                      # stdio, for Claude Desktop / Claude Code
    python server.py --transport http     # streamable HTTP on 127.0.0.1:8000
    python server.py doctor               # which platforms have credentials
    python server.py list-actions google_ads_action --search budget

Two surfaces exist, and the default is deliberate:

  slim (default)  ~17 dispatcher tools + list_actions. The agent discovers the
                  ~1,900 underlying actions through them. This is the surface
                  the hosted product serves, and the only one that fits inside
                  a chat client's tool budget.
  full            every action registered as its own MCP tool. Thousands of
                  tools; most clients truncate or refuse. For programmatic
                  clients that want raw schemas.

HTTP mode has NO authentication -- it is you talking to your own process, holding
your own ad-account credentials. It therefore binds 127.0.0.1 and refuses a
public interface unless MCP_ADS_ALLOW_PUBLIC_BIND=true, because an open port
that can spend your ad budget is not a bug report anyone wants to write.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv


def _load_env(env_file: str | None) -> None:
    if env_file:
        load_dotenv(env_file, override=False)
        return
    # ./.env for the clone-and-run case, $MCP_ADS_HOME/.env for a tidy install.
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=False)
    home = os.environ.get("MCP_ADS_HOME", "")
    if home:
        load_dotenv(os.path.join(home, ".env"), override=False)


def _server(surface: str):
    """Import triggers registration of every tool module; do it once, late."""
    if surface == "full":
        import mcp_server  # noqa: F401  - registers everything on the full instance
        from mcp_instance import mcp

        return mcp
    from slim_mcp import slim_mcp

    return slim_mcp


def _serve(args) -> int:
    surface = args.surface
    if args.transport == "stdio":
        # Anything printed to stdout corrupts the protocol stream, so make the
        # one likely offender (a stray print in a tool) land on stderr instead.
        server = _server(surface)
        asyncio.run(server.run_stdio_async())
        return 0

    host = args.host
    if host not in ("127.0.0.1", "localhost") and os.environ.get(
        "MCP_ADS_ALLOW_PUBLIC_BIND", ""
    ).strip().lower() not in ("1", "true", "yes"):
        print(
            f"Refusing to bind {host}: HTTP mode is unauthenticated and this server "
            "holds live ad-account credentials. Put it behind your own auth proxy and "
            "set MCP_ADS_ALLOW_PUBLIC_BIND=true if you mean it.",
            file=sys.stderr,
        )
        return 2

    import uvicorn
    from fastapi import FastAPI

    from cli_api import router as cli_router
    from mcp_instance import build_http_app

    mcp_asgi = build_http_app(_server(surface))

    app = FastAPI(title="MCP Ads (self-hosted)", docs_url=None, redoc_url=None)
    app.include_router(cli_router)

    # Locally staged media, so image URLs handed to ad platforms resolve.
    media_dir = os.path.join(
        os.environ.get("MCP_ADS_HOME") or os.path.expanduser("~/.mcp-ads"), "media"
    )
    if os.path.isdir(media_dir):
        from fastapi.staticfiles import StaticFiles

        app.mount("/media", StaticFiles(directory=media_dir), name="media")

    app.mount("/mcp", mcp_asgi)

    uvicorn.run(app, host=host, port=args.port, log_level="info")
    return 0


def _doctor() -> int:
    _load_env(None)
    from credentials_env import _GOOGLE_SERVICE_ENV, _PLATFORM_ENV, EnvCredentialProvider

    provider = EnvCredentialProvider()
    print("MCP Ads credential check (env backend)\n")

    google = {s: bool(provider.google_token(s)) for s in _GOOGLE_SERVICE_ENV}
    for service, ok in google.items():
        print(f"  {'ok ' if ok else '-- '} google/{service}")
    print(f"  {'ok ' if provider.meta_token() else '-- '} meta ads"
          f"   {'ok ' if provider.meta_token(surface='pages') else '-- '} meta pages")
    for platform in ("linkedin_ads", "tiktok_ads", "snapchat_ads", "microsoft_ads"):
        try:
            provider.platform_token(platform)
            print(f"  ok  {platform}")
        except RuntimeError:
            print(f"  --  {platform}")
    print(f"  {'ok ' if provider.api_key('openai') else '-- '} openai (creative)")
    print(f"  {'ok ' if provider.wordpress_connection() else '-- '} wordpress")
    print(f"  {'ok ' if os.environ.get('GOOGLE_ADS_DEVELOPER_TOKEN') else '-- '} google ads developer token")
    print("\n'--' means no credential found. See .env.example for the variable names.")
    return 0


def _list_actions(category: str, search: str) -> int:
    _load_env(None)
    import mcp_server  # noqa: F401
    from tool_registry import list_actions

    import json

    print(json.dumps(list_actions(category, search, 50, 0), indent=2, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcp-ads", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the MCP server (default)")
    serve.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    serve.add_argument("--surface", choices=("slim", "full"), default="slim")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--env-file", default=None)

    sub.add_parser("doctor", help="report which platforms have credentials")

    auth_cmd = sub.add_parser("auth", help="OAuth a platform locally and store the token")
    auth_cmd.add_argument("platform", help="snapchat_ads | microsoft_ads | linkedin_ads | tiktok_organic")

    la = sub.add_parser("list-actions", help="list a category's actions")
    la.add_argument("category")
    la.add_argument("--search", default="")

    args = parser.parse_args(argv)

    if args.command == "doctor":
        return _doctor()
    if args.command == "auth":
        _load_env(None)
        from platform_oauth import run_auth

        return run_auth(args.platform)
    if args.command == "list-actions":
        return _list_actions(args.category, args.search)

    # Bare `python server.py` serves stdio -- the Claude Desktop line stays short.
    if args.command is None:
        args = parser.parse_args(["serve"] + (argv or sys.argv[1:]))
    _load_env(args.env_file)
    return _serve(args)


if __name__ == "__main__":
    raise SystemExit(main())
