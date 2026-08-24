"""Multi-connection credential resolution: the platform-facing half.

The id normalisers, the "can this token actually see this account" probes and
their cache live here; the workspace/connection database queries that used to
sit beside them moved to account_resolution_db.py, which only the hosted
credential backend imports. tools/ must run without a database, and after the
tools moved onto the credentials provider seam, nothing in tools/ needed the
queries anyway -- only the storage backend does.

Callers that used to import resolve_google_ads_token / list_meta_ads_connections
from here now go through credentials.provider(), which routes to whichever
backend is installed.
"""
from __future__ import annotations

MANY = "__MANY__"  # sentinel: 2+ connections exist and no account id was given


def _normalize_google_customer_id(customer_id: str) -> str:
    return str(customer_id or "").strip().replace("-", "")


def _normalize_meta_account_id(account_id: str) -> str:
    value = str(account_id or "").strip()
    return value[4:] if value.startswith("act_") else value


def _account_id_variants(account_id: str) -> list[str]:
    """Both spellings an ad account id is stored in.

    Meta ad accounts are stored WITH the "act_" prefix and arrive normalised
    WITHOUT it; Google Ads customer ids are stored and normalised as plain
    digits. Searching only the form we were handed missed every Meta row.
    """
    if not account_id:
        return []
    if account_id.startswith("act_"):
        return [account_id, account_id[4:]]
    return [account_id, f"act_{account_id}"]


def _google_ads_customer_probe(refresh_token: str, customer_id: str) -> bool:
    """Can this Google login reach this customer? One tiny GAQL read.

    A query rather than list_accessible_customers: that call returns only the
    logins directly attached to the token, so every client account managed
    through an MCC would look inaccessible and route to the wrong connection --
    which is the whole failure this is here to prevent.

    Imported lazily. account_resolution is imported by the Google Ads tools, so
    importing them at module scope would be circular.
    """
    from google.ads.googleads.client import GoogleAdsClient

    from tools.google_ads import (
        GOOGLE_ADS_DEVELOPER_TOKEN,
        GOOGLE_CLIENT_ID,
        GOOGLE_CLIENT_SECRET,
    )

    client = GoogleAdsClient.load_from_dict({
        "developer_token": GOOGLE_ADS_DEVELOPER_TOKEN,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "use_proto_plus": True,
    })
    service = client.get_service("GoogleAdsService")
    response = service.search(
        customer_id=customer_id,
        query="SELECT customer.id FROM customer LIMIT 1",
    )
    # Consuming the iterator is what actually issues the request.
    for _ in response:
        return True
    return True


# (user_id, platform, account_id) -> token that can see it, or "" for "none can".
# Per process and never invalidated on purpose: connections change rarely, and a
# wrong answer here is a failed write rather than stale data.
_PROBE_CACHE: dict = {}


def _meta_account_probe(token: str, account_id: str) -> bool:
    """Can this token see this ad account? One cheap Graph read.

    The id arrives normalised, which strips the "act_" prefix -- and an ad
    account node only exists with it. Without putting it back this asks Meta
    about a different object entirely, gets a negative, and quietly defeats the
    routing it is supposed to fix.
    """
    import requests

    node = account_id if account_id.startswith("act_") else f"act_{account_id}"
    response = requests.get(
        f"https://graph.facebook.com/v22.0/{node}",
        params={"fields": "id", "access_token": token},
        timeout=15,
    )
    return response.ok


# Page and Instagram work is authorized by the organic connection, which is a
# deliberately separate login: bundling its permissions into the Ads consent
# made Facebook reject that dialog outright while they were unapproved. Page
# lookups therefore have to search both, or the Pages that carry posting
# rights are invisible and every publish falls back to an ads token that
# cannot post.
META_PAGE_PLATFORMS = ["meta_ads", "meta_organic"]
