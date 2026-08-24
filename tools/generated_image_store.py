from __future__ import annotations

import os

from credentials import provider


_TTL_SECONDS = max(int(os.environ.get("GENERATED_IMAGE_STORE_TTL_SECONDS", "7200") or "7200"), 60)


def put_generated_image(
    *,
    user_id: str,
    file_bytes_base64: str,
    mime_type: str,
    filename: str,
    source_url: str = "",
    revised_prompt: str = "",
) -> str:
    # user_id stays in the signature for the callers' sake, but the provider
    # scopes to the request principal itself.
    return provider().create_generated_image_asset(
        file_bytes_base64=file_bytes_base64,
        mime_type=mime_type,
        filename=filename,
        source_url=source_url,
        revised_prompt=revised_prompt,
        ttl_seconds=_TTL_SECONDS,
    )


def get_generated_image(asset_id: str, user_id: str) -> dict | None:
    return provider().get_generated_image_asset(asset_id)