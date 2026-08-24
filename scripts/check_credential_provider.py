"""Both credential backends must implement the whole protocol.

The protocol is structural, so nothing enforces it at import time: a backend
missing a method type-checks fine and fails at the one call site that needed it,
in production, on whichever platform the operator happened to connect. That is a
bad way to find out.

This is cheap to run and catches the drift that matters -- a method added to
CredentialProvider and implemented in only one of the two backends.
"""
from __future__ import annotations

import os
import sys

# The hosted auth module refuses to import without a signing key, and this check
# never signs anything. A placeholder keeps the check runnable on a dev machine
# and in CI without handing either one a real secret.
os.environ.setdefault("JWT_SECRET_KEY", "check-credential-provider-placeholder")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from credentials import CredentialProvider, Principal  # noqa: E402
from credentials_env import EnvCredentialProvider, SelfHostPrincipal  # noqa: E402

# The multi-tenant backend ships with the hosted deployment, not here. Check it
# when present so the one-copy world still validates both; skip it cleanly when
# this is the public tree.
try:
    from credentials_db import DatabaseCredentialProvider  # noqa: E402
except ImportError:
    DatabaseCredentialProvider = None


def _required(protocol) -> set[str]:
    return {
        name
        for name in vars(protocol)
        if not name.startswith("_") and callable(vars(protocol)[name])
    }


def main() -> int:
    failures: list[str] = []

    expected = _required(CredentialProvider)
    if not expected:
        failures.append("CredentialProvider declares no methods -- this check is inert.")

    backends = [b for b in (DatabaseCredentialProvider, EnvCredentialProvider) if b is not None]
    for backend in backends:
        missing = sorted(m for m in expected if not callable(getattr(backend, m, None)))
        if missing:
            failures.append(f"{backend.__name__} is missing: {', '.join(missing)}")

    # Principal is a data protocol, so check the attributes the tools touch
    # rather than only the method.
    principal = SelfHostPrincipal()
    for attr in ("id", "role", "selected_wordpress_connection_id"):
        if not hasattr(principal, attr):
            failures.append(f"SelfHostPrincipal has no {attr!r}")
    if not callable(getattr(principal, "get_meta_token", None)):
        failures.append("SelfHostPrincipal has no get_meta_token()")

    # The one attribute that is written, not just read (tools/wordpress/sites.py
    # assigns to it). A plain class attribute would accept the write silently
    # and lose it, which reads as "selecting a site does nothing".
    try:
        principal.selected_wordpress_connection_id = principal.selected_wordpress_connection_id
    except AttributeError:
        failures.append(
            "SelfHostPrincipal.selected_wordpress_connection_id is not writable; "
            "tools/wordpress/sites.py assigns to it."
        )
    except Exception as exc:  # noqa: BLE001 - a store that cannot be written is a real failure
        failures.append(f"Writing selected_wordpress_connection_id raised: {exc}")

    if not isinstance(principal, Principal):
        failures.append("SelfHostPrincipal does not satisfy the Principal protocol.")

    if failures:
        for line in failures:
            print(f"FAIL: {line}")
        return 1

    print(f"Both credential backends implement all {len(expected)} protocol methods.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
