"""media_upload_result — the non-UI half of the media upload pair.

`media_upload_start` lives in `media_app.py` because it carries the drop-zone
App binding, and an extension's tools must exist before the server is built.
This one deliberately does NOT render the widget: it reports a *finished*
upload, so its payload has no `upload_url`, and a drop zone bound to it would
mount and then wait forever for a slot that is never coming. That is exactly
what it did -- a greyed-out, unclickable zone reading "Waiting for the upload
slot..." under a card titled "Media upload result".

`Apps.tool()` requires a resource_uri, so the only way to have a tool without a
UI binding is to register it the ordinary way, here, after the instance exists.
"""

from __future__ import annotations

import json

from media_app import upload_result_payload
from mcp_instance import mcp


@mcp.tool(name="media_upload_result", title="Check A Media Upload")
def media_upload_result(upload_id: str) -> str:
    """Check whether the user has finished uploading, and get the public URL.

    Call after media_upload_start. If status is "waiting" the user has not
    dropped the file in yet -- tell them so and check again rather than polling
    in a tight loop.

    Args:
        upload_id: The upload_id returned by media_upload_start.
    """
    return json.dumps(upload_result_payload(upload_id))
