"""The public tree must stay public.

This repo is the open-source half of a larger codebase. The other half -- the
multi-tenant database backend, the managed-auth broker, the hosted domain --
must never leak back in, and neither may anything that smells like a credential.
A human eye ran once, before the first commit; this runs every time.
"""
from __future__ import annotations

import hashlib
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Modules that exist only in the private deployment.
PRIVATE_IMPORTS = re.compile(
    r"^\s*(from|import)\s+(database|agency_os_models|agency_os_service|"
    r"credentials_db|account_resolution_db|pipedream_transport|billing|plans|"
    r"teams|usage|purchases|affiliates|emails|webhooks|datafast|meta_capi)\b",
    re.MULTILINE,
)

# Hosted-deployment identifiers and personal data.
FORBIDDEN_TEXT = [
    re.compile(r"marketingmcp-\d+"),          # the GCP project
    re.compile(r"iam\.gserviceaccount\.com"),
    re.compile(r"[A-Za-z0-9._%+-]+@gmail\.com"),
]

# Real object ids (an ad account, two Google Ads customers, a lead form) were
# found baked into test fixtures before the first release and fuzzed out.
# Object ids are not credentials, but they name a real business -- which is
# also why they live here as hashes: a denylist that spelled them out would
# re-publish the very identifiers it exists to keep out. Every digit run in
# the tree is hashed and compared instead.
FORBIDDEN_ID_HASHES = {
    "42067386ecec5112a2b5553a77b10c2317d55fe9103944a1b402446834a170a0",
    "b86ad728dd522d84a79810282764cc701861d5fccae2d394fa29648c3f348c3d",
    "eb510c481098eff2fd70a3c33e29b4c71e6a348cbcd0c5fa000c4ed68258fcd7",
    "faf9c49b7e1ac879d805199ff47ffff42cfb0d9dbab19953fd747f2f610cb4d3",
}
_DIGIT_RUN = re.compile(r"\d{10,16}")

# Credential shapes. Long base64 blobs (fonts, images) are excluded by
# requiring the token prefix at a word boundary.
SECRET_SHAPES = [
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{30,}"),
    re.compile(r"\bEAA[A-Za-z0-9]{60,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bya29\.[A-Za-z0-9_-]{20,}"),
    re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"),
]

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "_smoke_home", "media"}
TEXT_SUFFIXES = {".py", ".md", ".ts", ".json", ".yml", ".yaml", ".toml", ".txt", ".example"}


def _files():
    for path in ROOT.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_file() and (path.suffix in TEXT_SUFFIXES or path.name == ".env.example"):
            yield path


def test_no_private_imports():
    offenders = []
    # Two named exemptions, both guarded imports that cannot execute in this
    # tree: credentials.py is the seam itself (find_spec-guarded), and the
    # provider check validates the multi-tenant backend when it happens to be
    # installed alongside (try/ImportError). Nothing else gets an exemption.
    exempt = {
        ("credentials.py", "credentials_db"),
        (str(pathlib.Path("scripts") / "check_credential_provider.py"), "credentials_db"),
    }
    for path in _files():
        if path.suffix != ".py" or path == pathlib.Path(__file__).resolve():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in PRIVATE_IMPORTS.finditer(text):
            rel = str(path.relative_to(ROOT))
            if (rel, match.group(2)) in exempt:
                continue
            offenders.append(f"{rel}: {match.group(0).strip()}")
    assert not offenders, "private modules imported:\n" + "\n".join(offenders)


def test_no_hosted_identifiers_or_pii():
    offenders = []
    for path in _files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in FORBIDDEN_TEXT:
            for match in pattern.finditer(text):
                offenders.append(f"{path.relative_to(ROOT)}: {match.group(0)}")
        for match in _DIGIT_RUN.finditer(text):
            if hashlib.sha256(match.group(0).encode()).hexdigest() in FORBIDDEN_ID_HASHES:
                offenders.append(f"{path.relative_to(ROOT)}: a known real object id")
    assert not offenders, "hosted identifiers or PII found:\n" + "\n".join(offenders)


def test_no_secret_shaped_strings():
    offenders = []
    for path in _files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in SECRET_SHAPES:
            for match in pattern.finditer(text):
                offenders.append(f"{path.relative_to(ROOT)}: {match.group(0)[:24]}...")
    assert not offenders, "secret-shaped strings found:\n" + "\n".join(offenders)


def test_env_example_has_no_values():
    """Every assignment in .env.example is empty or a safe literal."""
    allowed = {"false", "true", "v22.0", "202411", "https://example.com"}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        _, value = line.split("=", 1)
        assert value.strip() in allowed or not value.strip(), f"unexpected value: {line}"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"PASS {name}")
