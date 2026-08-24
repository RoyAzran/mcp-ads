"""Request identity for a single-user server.

The hosted product this codebase grew up in authenticates every request with a
JWT and threads the resulting user through two ContextVars. The tool code kept
the ContextVars and lost interest in where the user came from -- which is what
makes this drop-in shim possible: same module name, same two names exported,
no JWT, no database, no secret key required at import.

The subtlety is that a plain ContextVar with a default is not enough. Nearly
every call site reads `current_user_ctx.get(None)`, and an explicit default
argument overrides the constructed one -- so over stdio, where no request
wrapper ever calls .set(), every one of those reads would come back None and
every tool would refuse with "Not authenticated". The var is therefore wrapped:
.get() falls back to the operator principal no matter which spelling the call
site used, while .set()/.reset() still behave exactly like ContextVar for the
few paths that stage a stand-in identity (media delivery does).
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Optional


class _DefaultPrincipal:
    """Delegates to the credential store's principal, imported lazily.

    Lazy because of the import order: credentials.py imports this module to
    re-export the ContextVars, so importing credentials_env at the top here
    would be a cycle. By the time any attribute is read, everything is loaded.
    """

    def __getattr__(self, name: str) -> Any:
        from credentials_env import PRINCIPAL

        return getattr(PRINCIPAL, name)

    def __setattr__(self, name: str, value: Any) -> None:
        from credentials_env import PRINCIPAL

        setattr(PRINCIPAL, name, value)


_DEFAULT = _DefaultPrincipal()


class _PrincipalVar:
    """ContextVar-shaped, but .get() can never come back empty."""

    def __init__(self) -> None:
        self._var: ContextVar[Any] = ContextVar("current_user", default=None)

    def get(self, *default: Any) -> Any:
        value = self._var.get(None)
        return value if value is not None else _DEFAULT

    def set(self, value: Any):
        return self._var.set(value)

    def reset(self, token) -> None:
        self._var.reset(token)


current_user_ctx = _PrincipalVar()

# A workspace-pinned credential override in the hosted product. Self-host has
# no workspaces, so this stays None -- but the tools consult it before their
# own resolution, so the name must exist and behave like the real thing.
current_connection_token_ctx: ContextVar[Optional[str]] = ContextVar(
    "current_connection_token", default=None
)
