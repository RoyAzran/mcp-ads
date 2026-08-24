"""First-party TikTok Content Posting credentials for the organic tools.

The organic TikTok tools were written against Pipedream like their LinkedIn
siblings, but Pipedream publishes no TikTok organic app -- pipedream_connect.py
says so explicitly and deliberately offers no button for one. So every one of
those tools could only ever fail to resolve an account: the code existed, the
connect path did not.

This is the other half. oauth_tiktok.py stores a real TikTok token on a
DataSourceConnection(platform="tiktok_organic"), and these helpers hand it to
the posting tools so they call open.tiktokapis.com directly.

Refresh matters more here than elsewhere: TikTok access tokens last about 24
hours, against Meta's 60 days. A connection made for a demo on Monday is dead
by Tuesday, and the failure is a bare 401 from TikTok, so the refresh happens
here rather than being left to a nightly job nobody wrote yet.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

import httpx

TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
API_BASE = "https://open.tiktokapis.com/v2"

# Refresh a little before the token actually dies. A request that starts valid
# and expires in flight fails the same way an expired one does.
_REFRESH_MARGIN_SECONDS = 300


class TikTokNotConnected(RuntimeError):
    """No usable TikTok organic connection for this user."""


def tiktok_access_token(connection_id: str = "") -> str:
    """A live access token for this user's TikTok organic connection.

    The lookup -- and the refresh, when the stored token is at or near its
    24-hour expiry -- lives in the credential provider, so this works the same
    against the hosted database and a single-user local store.
    """
    from credentials import provider

    try:
        return provider().platform_token("tiktok_organic", connection_id)
    except RuntimeError as exc:
        raise TikTokNotConnected(str(exc)) from exc


# TikTok's chunk rules for FILE_UPLOAD. A file under the minimum must go up as
# one chunk equal to its own size -- sending a 5MB chunk_size for a 2MB file is
# rejected at init, before a single byte is uploaded.
_MIN_CHUNK = 5 * 1024 * 1024
_MAX_CHUNK = 64 * 1024 * 1024
_MAX_CHUNKS = 1000

# A video is fetched into memory to be pushed, so this caps what one call can
# pull. TikTok's own ceiling is far higher, but this runs in a request handler
# on a shared container, not a batch job.
_MAX_FETCH_BYTES = 300 * 1024 * 1024


def _chunk_plan(size: int) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) that TikTok will accept for this size."""
    if size <= _MIN_CHUNK:
        return size, 1
    chunk = min(_MAX_CHUNK, max(_MIN_CHUNK, size // _MAX_CHUNKS + 1))
    if chunk < _MIN_CHUNK:
        chunk = _MIN_CHUNK
    count = max(1, size // chunk)
    if count > _MAX_CHUNKS:
        count = _MAX_CHUNKS
        chunk = size // count
    if count == 1:
        # A single chunk IS the whole video. Declaring a chunk_size smaller
        # than what then gets sent is the one case TikTok reads as a mismatch,
        # and it rejects at init rather than on the upload.
        return size, 1
    return chunk, count


def fetch_video_bytes(video_url: str) -> tuple[bytes, str]:
    """Download a video so its bytes can be pushed to TikTok."""
    with httpx.Client(timeout=180, follow_redirects=True) as client:
        with client.stream("GET", video_url) as response:
            if response.status_code >= 400:
                raise RuntimeError(
                    f"Could not download the video ({response.status_code}) from {video_url}"
                )
            mime = (response.headers.get("content-type") or "video/mp4").split(";")[0].strip()
            chunks: list[bytes] = []
            total = 0
            for part in response.iter_bytes():
                total += len(part)
                if total > _MAX_FETCH_BYTES:
                    raise RuntimeError(
                        f"The video is larger than {_MAX_FETCH_BYTES // (1024 * 1024)}MB, "
                        "which is more than this can push in one call."
                    )
                chunks.append(part)
    data = b"".join(chunks)
    if not data:
        raise RuntimeError(f"The video at {video_url} was empty.")
    return data, (mime if mime.startswith("video/") else "video/mp4")


def upload_draft_by_file(video_bytes: bytes, mime_type: str = "video/mp4",
                         connection_id: str = "") -> dict:
    """Send a video to the creator's TikTok drafts rather than publishing it.

    The inbox endpoint, which is what the video.upload scope grants. It exists
    here because direct posting is not available to an unaudited app on a
    public account at all -- TikTok answers
    "unaudited_client_can_only_post_to_private_accounts" and issues no upload
    URL. Drafts have no such restriction, so this is the path that works before
    the Content Posting audit clears.
    """
    return _upload_by_file(
        "/post/publish/inbox/video/init/", None, video_bytes, mime_type, connection_id
    )


def publish_video_by_file(video_bytes: bytes, post_info: dict, mime_type: str = "video/mp4",
                          connection_id: str = "") -> dict:
    """Publish via FILE_UPLOAD: we send the bytes, TikTok never fetches a URL.

    This is the path that needs no domain verification. PULL_FROM_URL asks
    TikTok to download from a host it insists you prove you own; pushing the
    bytes ourselves sidesteps that entirely, at the cost of moving the file
    through this server.
    """
    return _upload_by_file(
        "/post/publish/video/init/", post_info, video_bytes, mime_type, connection_id
    )


def _upload_by_file(init_path: str, post_info: dict | None, video_bytes: bytes,
                    mime_type: str, connection_id: str) -> dict:
    size = len(video_bytes)
    chunk_size, chunk_count = _chunk_plan(size)

    body: dict = {
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": size,
            "chunk_size": chunk_size,
            "total_chunk_count": chunk_count,
        },
    }
    # The inbox (draft) endpoint takes source_info only; sending post_info to it
    # is an error, and omitting it from a direct post is one too.
    if post_info is not None:
        body["post_info"] = post_info

    init = tiktok_request("POST", init_path, body=body, connection_id=connection_id)
    data = init.get("data") or {}
    upload_url = data.get("upload_url") or ""
    publish_id = data.get("publish_id") or ""
    if not upload_url or not publish_id:
        # Hand back TikTok's own error rather than a generic failure: its
        # messages here name the offending field (privacy level, size, quota).
        return {"error": "TikTok did not return an upload URL.", "tiktok_response": init}

    with httpx.Client(timeout=300) as client:
        for index in range(chunk_count):
            start = index * chunk_size
            # The last chunk carries the remainder rather than becoming its own
            # short chunk -- TikTok rejects a final chunk below the minimum.
            end = size - 1 if index == chunk_count - 1 else start + chunk_size - 1
            piece = video_bytes[start:end + 1]
            response = client.put(
                upload_url,
                content=piece,
                headers={
                    "Content-Type": mime_type,
                    "Content-Length": str(len(piece)),
                    "Content-Range": f"bytes {start}-{end}/{size}",
                },
            )
            if response.status_code >= 400:
                return {
                    "error": f"Uploading chunk {index + 1}/{chunk_count} failed ({response.status_code}).",
                    "detail": response.text[:500],
                    "publish_id": publish_id,
                }

    return {
        "data": {"publish_id": publish_id},
        "uploaded_bytes": size,
        "chunks": chunk_count,
        "note": "Uploaded. Poll tiktok_organic_status with this publish_id until it reports a final state.",
    }


def tiktok_request(method: str, path: str, body: dict | None = None,
                   params: dict | None = None, connection_id: str = "") -> dict:
    """Call the TikTok open API with this user's own token."""
    token = tiktok_access_token(connection_id)
    url = path if path.startswith("http") else f"{API_BASE}{path if path.startswith('/') else '/' + path}"
    with httpx.Client(timeout=60) as client:
        response = client.request(
            method.upper(),
            url,
            params=params,
            json=body if body is not None else None,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
        )
    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": response.text[:2000]}
    if response.status_code >= 400:
        payload = dict(payload) if isinstance(payload, dict) else {"response": payload}
        payload["http_status"] = response.status_code
    return payload
