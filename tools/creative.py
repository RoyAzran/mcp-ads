"""
Creative generation tools powered by OpenAI image models.
"""

from __future__ import annotations

import asyncio
import base64
import os
import uuid
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

from auth import current_user_ctx
from mcp_instance import mcp
from mcp.server.mcpserver import Context
from mcp.types import CallToolResult, ImageContent, TextContent
from tools.generated_image_store import put_generated_image


DEFAULT_IMAGE_MODEL = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-2")
DEFAULT_IMAGE_QUALITY = os.environ.get("OPENAI_IMAGE_QUALITY", "low")
DEFAULT_MULTI_IMAGE_QUALITY = os.environ.get("OPENAI_IMAGE_MULTI_QUALITY", "low")
DEFAULT_AD_VARIANT_COUNT = max(int(os.environ.get("OPENAI_AD_IMAGE_DEFAULT_COUNT", "3") or "3"), 1)
MAX_INLINE_PREVIEWS = max(int(os.environ.get("OPENAI_INLINE_PREVIEWS", "0") or "0"), 0)
_OUTPUT_FORMAT_TO_MIME_TYPE = {
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}
_OUTPUT_FORMAT_TO_EXTENSION = {
    "jpeg": ".jpg",
    "png": ".png",
    "webp": ".webp",
}
_IMAGE_JOB_TTL_SECONDS = max(int(os.environ.get("IMAGE_GENERATION_JOB_TTL_SECONDS", "7200") or "7200"), 300)
_IMAGE_GENERATION_JOBS: dict[str, dict[str, Any]] = {}
_IMAGE_GENERATION_JOB_LOCK = asyncio.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _cleanup_expired_image_jobs() -> None:
    async with _IMAGE_GENERATION_JOB_LOCK:
        now = datetime.now(timezone.utc).timestamp()
        expired = [
            job_id
            for job_id, job in _IMAGE_GENERATION_JOBS.items()
            if now - float(job.get("updated_at_ts") or now) > _IMAGE_JOB_TTL_SECONDS
        ]
        for job_id in expired:
            _IMAGE_GENERATION_JOBS.pop(job_id, None)


async def _update_image_job(job_id: str, **fields: Any) -> dict[str, Any] | None:
    await _cleanup_expired_image_jobs()
    async with _IMAGE_GENERATION_JOB_LOCK:
        job = _IMAGE_GENERATION_JOBS.get(job_id)
        if job is None:
            return None
        job.update(fields)
        job["updated_at"] = _now_iso()
        job["updated_at_ts"] = datetime.now(timezone.utc).timestamp()
        return dict(job)


async def _get_image_job(job_id: str) -> dict[str, Any] | None:
    await _cleanup_expired_image_jobs()
    async with _IMAGE_GENERATION_JOB_LOCK:
        job = _IMAGE_GENERATION_JOBS.get((job_id or "").strip())
        return dict(job) if job else None


def _require_authenticated_user() -> None:
    if current_user_ctx.get(None) is None:
        raise RuntimeError("Not authenticated.")


def _clean_optional(value: str) -> str | None:
    value = (value or "").strip()
    return value or None


def _resolve_quality(requested_quality: str, image_count: int) -> str:
    cleaned = (_clean_optional(requested_quality) or "").lower()
    if cleaned and cleaned != "auto":
        return cleaned
    if image_count > 1:
        return DEFAULT_MULTI_IMAGE_QUALITY
    return DEFAULT_IMAGE_QUALITY


def _supports_response_format(model_name: str) -> bool:
    normalized = (model_name or "").strip().lower()
    return not normalized.startswith("gpt-image")


def _is_gpt_image_model(model_name: str) -> bool:
    normalized = (model_name or "").strip().lower()
    return normalized.startswith("gpt-image")


def _inline_preview_limit(image_count: int) -> int:
    if image_count == 1:
        return 1
    return min(MAX_INLINE_PREVIEWS, max(image_count, 0))


def _mime_type_for_output_format(output_format: str | None) -> str:
    return _OUTPUT_FORMAT_TO_MIME_TYPE.get((output_format or "").strip().lower(), "image/png")



def _preview_url_for(asset_id: str) -> str:
    """Signed, viewable link for a stored generated image (or "" if unavailable)."""
    if not asset_id:
        return ""
    try:
        from auth import current_user_ctx
        from tools.generated_image_links import preview_url

        user = current_user_ctx.get(None)
        return preview_url(asset_id, str(user.id)) if user else ""
    except Exception:  # noqa: BLE001 - a missing link must not fail generation
        return ""


def _filename_for_output_format(index: int, output_format: str | None) -> str:
    ext = _OUTPUT_FORMAT_TO_EXTENSION.get((output_format or "").strip().lower(), ".png")
    return f"generated-image-{index}{ext}"


async def _generate_image_items(
    client: Any,
    request_kwargs: dict[str, Any],
    image_count: int,
    model_name: str,
) -> tuple[list[Any], list[str]]:
    if image_count <= 1 or not _is_gpt_image_model(model_name):
        response = await client.images.generate(**request_kwargs)
        return list(response.data or []), []

    single_image_kwargs = dict(request_kwargs)
    single_image_kwargs["n"] = 1

    async def _generate_one(variant_index: int) -> Any:
        response = await client.images.generate(**single_image_kwargs)
        items = list(response.data or [])
        if not items:
            raise RuntimeError(f"Image variant {variant_index} returned no data.")
        return items[0]

    results = await asyncio.gather(
        *[_generate_one(index) for index in range(1, image_count + 1)],
        return_exceptions=True,
    )

    items: list[Any] = []
    errors: list[str] = []
    for index, result in enumerate(results, start=1):
        if isinstance(result, Exception):
            errors.append(f"Variant {index} failed: {result}")
            continue
        items.append(result)

    return items, errors


async def _image_item_to_dict(
    item: Any,
    include_inline_preview: bool = True,
    default_mime_type: str = "image/png",
    upload_filename: str = "generated-image.png",
) -> dict[str, Any]:
    image: dict[str, Any] = {
        "mime_type": default_mime_type,
        "filename": upload_filename,
    }
    b64_json = getattr(item, "b64_json", None)
    url = getattr(item, "url", None)
    revised_prompt = getattr(item, "revised_prompt", None)
    mime_type = default_mime_type

    if b64_json:
        image["file_bytes_base64"] = b64_json
        if include_inline_preview:
            image["b64_json"] = b64_json
        image["mime_type"] = mime_type
    elif url:
        image["url"] = str(url)
        image["mime_type"] = mime_type
        if include_inline_preview:
            import httpx

            try:
                async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    mime_type = resp.headers.get("Content-Type", mime_type).split(";")[0].strip().lower() or mime_type
                    encoded = base64.b64encode(resp.content).decode()
                    image["b64_json"] = encoded
                    image["file_bytes_base64"] = encoded
                    image["mime_type"] = mime_type
                    image["url"] = str(resp.url or url)
            except Exception as exc:
                image["preview_error"] = str(exc)
    if revised_prompt:
        image["revised_prompt"] = revised_prompt
    return image


async def _report(context: "Context | None", done: int, total: int, message: str) -> None:
    """Report progress if anyone is listening, and never fail the work if not.

    Context.report_progress needs a live MCP request. Tools reached through
    tool_registry.dispatch are handed a deliberately request-free Context (see
    _tool_context there), because dispatch is called from plain HTTP handlers --
    /mcp-slim, the CLI and the GPT Action. Calling report_progress on it raises

      Context is not available outside of a request

    which killed image generation outright on every one of those paths: the
    image was never even requested, because the first progress ping came before
    the OpenAI call. Progress is decoration; losing it must cost nothing.
    """
    if context is None:
        return
    try:
        await context.report_progress(done, total, message)
    except Exception:
        pass


async def _generate_images_payload(
    *,
    prompt: str,
    n: int,
    size: str,
    quality: str,
    model: str,
    context: Context | None = None,
    progress_callback: Any = None,
) -> dict[str, Any]:
    started_at = perf_counter()
    user = current_user_ctx.get(None)
    if user is None:
        raise RuntimeError("Not authenticated.")
    if not prompt or not prompt.strip():
        raise ValueError("prompt is required.")
    image_count = int(n)
    if image_count < 1:
        raise ValueError("n must be at least 1.")
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY environment variable is not set.")

    resolved_model = _clean_optional(model) or DEFAULT_IMAGE_MODEL
    resolved_quality = _resolve_quality(quality, image_count)

    await _report(context, 10, 100, "Starting image generation...")
    if progress_callback is not None:
        await progress_callback(10, "Starting image generation...")

    from openai import AsyncOpenAI

    client = AsyncOpenAI()
    request_kwargs: dict[str, Any] = {
        "model": resolved_model,
        "prompt": prompt.strip(),
        "n": image_count,
        "size": _clean_optional(size) or "auto",
        "quality": resolved_quality,
    }
    if _supports_response_format(resolved_model):
        request_kwargs["response_format"] = "b64_json"
    if _is_gpt_image_model(resolved_model):
        request_kwargs["output_format"] = "jpeg"
        request_kwargs["output_compression"] = 80

    if image_count > 1 and _is_gpt_image_model(resolved_model):
        await _report(context, 20, 100, f"Generating {image_count} image variants in parallel...")
    if progress_callback is not None:
        await progress_callback(
            20,
            f"Generating {image_count} image variant{'s' if image_count != 1 else ''}...",
        )

    image_items, generation_errors = await _generate_image_items(
        client,
        request_kwargs,
        image_count,
        resolved_model,
    )
    if not image_items:
        raise RuntimeError("; ".join(generation_errors) or "Image generation returned no data.")

    await _report(context, 80, 100, "Preparing image preview...")
    if progress_callback is not None:
        await progress_callback(80, "Preparing image preview...")

    inline_limit = _inline_preview_limit(image_count)
    output_format = request_kwargs.get("output_format")
    default_mime_type = _mime_type_for_output_format(output_format)
    images = [
        await _image_item_to_dict(
            item,
            include_inline_preview=index < inline_limit,
            default_mime_type=default_mime_type,
            upload_filename=_filename_for_output_format(index + 1, output_format),
        )
        for index, item in enumerate(image_items)
    ]
    for image in images:
        file_bytes_base64 = str(image.get("file_bytes_base64") or "")
        if not file_bytes_base64:
            continue
        image["generated_asset_id"] = put_generated_image(
            user_id=str(user.id),
            file_bytes_base64=file_bytes_base64,
            mime_type=str(image.get("mime_type") or "image/png"),
            filename=str(image.get("filename") or "generated-image.png"),
            source_url=str(image.get("url") or ""),
            revised_prompt=str(image.get("revised_prompt") or ""),
        )
    duration_seconds = perf_counter() - started_at

    await _report(context, 100, 100, f"Image ready in {duration_seconds:.1f}s.")
    if progress_callback is not None:
        await progress_callback(100, f"Image ready in {duration_seconds:.1f}s.")

    return {
        "prompt": prompt.strip(),
        "model": resolved_model,
        "count": len(images),
        "generation_seconds": round(duration_seconds, 2),
        "errors": generation_errors,
        "images": images,
    }


async def _run_image_generation_job(
    *,
    job_id: str,
    user: Any,
    prompt: str,
    n: int,
    size: str,
    quality: str,
    model: str,
) -> None:
    token = current_user_ctx.set(user)
    try:
        await _update_image_job(
            job_id,
            status="running",
            started_at=_now_iso(),
            progress_message="Starting image generation...",
            progress_percent=10,
        )
        result = await _generate_images_payload(
            prompt=prompt,
            n=n,
            size=size,
            quality=quality,
            model=model,
            context=None,
            progress_callback=lambda percent, message: _update_image_job(
                job_id,
                progress_percent=percent,
                progress_message=message,
            ),
        )
        await _update_image_job(
            job_id,
            status="completed",
            completed_at=_now_iso(),
            progress_message=f"Image ready in {result['generation_seconds']:.1f}s.",
            progress_percent=100,
            result=result,
            error="",
        )
    except Exception as exc:
        await _update_image_job(
            job_id,
            status="failed",
            completed_at=_now_iso(),
            progress_message="Image generation failed.",
            progress_percent=100,
            error=str(exc),
        )
    finally:
        current_user_ctx.reset(token)


def _image_result(
    prompt: str,
    model_name: str,
    images: list[dict[str, Any]],
    duration_seconds: float,
    errors: list[str] | None = None,
    include_upload_bytes: bool = False,
) -> CallToolResult:
    inline_limit = _inline_preview_limit(len(images))
    content: list[TextContent | ImageContent] = [
        TextContent(
            type="text",
            text=f"Generated {len(images)} image(s) in {duration_seconds:.1f}s using {model_name}.",
        )
    ]

    content.append(
        TextContent(
            type="text",
            text=(
                "For Meta image upload, pass generated_asset_id directly to meta_upload_ad_image, "
                "meta_create_image_ad, or meta_ads_create_image_ad_from_media. Do not use shell/base64 commands on saved files. "
                "If generated_asset_id is missing, rerun with include_upload_bytes=true and pass the returned "
                "file_bytes_base64 plus filename directly to the Meta tool."
            ),
        )
    )

    if len(images) > inline_limit:
        content.append(
            TextContent(
                type="text",
                text=(
                    f"Embedded {inline_limit} inline preview to keep the MCP response fast. "
                    "All generated variants are still listed below."
                ),
            )
        )
    if errors:
        content.append(TextContent(type="text", text="\n".join(errors)))

    summary_images: list[dict[str, Any]] = []
    for index, image in enumerate(images, start=1):
        image_summary = {
            "index": index,
            "filename": image.get("filename", _filename_for_output_format(index, None)),
            "mime_type": image.get("mime_type", "image/png"),
            "revised_prompt": image.get("revised_prompt", ""),
            "url": image.get("url", ""),
            "generated_asset_id": image.get("generated_asset_id", ""),
            # A link anyone can open. Without it the only way to see a creative
            # was to push it into a live ad account and open Ads Manager --
            # which is what an assistant on a host with no inline preview was
            # correctly, and uselessly, telling customers.
            "preview_url": _preview_url_for(image.get("generated_asset_id", "")),
            "preview_error": image.get("preview_error", ""),
            "has_inline_preview": bool(image.get("b64_json")),
            "upload_ready": bool(image.get("file_bytes_base64")),
        }
        if include_upload_bytes:
            image_summary["file_bytes_base64"] = image.get("file_bytes_base64", "")
        summary_images.append(image_summary)

        b64_json = image.get("b64_json", "")
        if b64_json and index <= inline_limit:
            content.append(
                ImageContent(
                    type="image",
                    data=b64_json,
                    mimeType=image_summary["mime_type"],
                )
            )

        meta_lines: list[str] = []
        if image_summary["revised_prompt"]:
            meta_lines.append(f"Revised prompt: {image_summary['revised_prompt']}")
        if image_summary["url"]:
            meta_lines.append(f"Source URL: {image_summary['url']}")
        if image_summary["preview_url"]:
            # First, and phrased for the assistant to pass straight through:
            # the customer's actual question is "let me see it", and every host
            # can render or open a link even when it cannot show inline images.
            meta_lines.append(
                f"View image {index}: {image_summary['preview_url']} "
                "(share this link with the user so they can see the creative; "
                "it works in any client and expires in about two hours)."
            )
        if image_summary["generated_asset_id"]:
            meta_lines.append(
                "Use generated_asset_id "
                f"{image_summary['generated_asset_id']} in meta_upload_ad_image, meta_create_image_ad, or meta_ads_create_image_ad_from_media."
            )
        if image_summary["preview_error"]:
            meta_lines.append(f"Inline preview unavailable: {image_summary['preview_error']}")
        if not b64_json:
            meta_lines.insert(0, f"Image {index} preview was not embedded inline.")
        elif index > inline_limit:
            meta_lines.insert(0, f"Image {index} preview was skipped inline to avoid MCP timeouts.")
        if meta_lines:
            content.append(TextContent(type="text", text="\n".join(meta_lines)))

    return CallToolResult(
        content=content,
        structuredContent={
            "model": model_name,
            "prompt": prompt,
            "count": len(images),
            "generation_seconds": round(duration_seconds, 2),
            "errors": errors or [],
            "images": summary_images,
        },
    )


@mcp.tool(
    title="Generate Marketing Image From Prompt",
    description="[WRITE][RECOMMENDED] Step 1 of the normal image-ad flow. Generate one or more marketing images from a freeform prompt, show the preview in chat, then pass generated_asset_id to the Meta image-ad tools. This is the blocking version. If the user wants Claude to keep working while the image renders, use creative_start_image_generation instead. Never use shell commands to base64 local files from this result.",
)
async def creative_generate_image(
    prompt: str,
    n: int = 1,
    size: str = "auto",
    quality: str = DEFAULT_IMAGE_QUALITY,
    model: str = "",
    include_upload_bytes: bool = False,
    context: Context | None = None,

) -> CallToolResult:
    """[WRITE] Generate one or more marketing images from a freeform prompt.

    Use this for direct image generation when the user already knows what visual
    they want. This is the primary freeform image tool for ad creatives, social
    images, landing page visuals, campaign concepts, and other marketing assets.
    Returns inline image previews when Claude supports image blocks.

    Args:
        prompt: Detailed creative brief. Include platform, audience, language, style, brand, offer, and required text.
        n: Number of images to generate. Defaults to 1.
        size: Image size, such as 'auto', '1024x1024', '1024x1536', or '1536x1024'.
        quality: Quality setting. Defaults to medium for single images. If omitted or set to auto on multi-image requests, the tool uses a cheaper low-quality draft mode.
        model: Optional OpenAI image model. Defaults to OPENAI_IMAGE_MODEL or 'gpt-image-2'.
        include_upload_bytes: If true, include file_bytes_base64 in structured output for direct Meta upload. Default false to avoid oversized MCP responses.
    """
    try:
        _require_authenticated_user()
        result = await _generate_images_payload(
            prompt=prompt,
            n=n,
            size=size,
            quality=quality,
            model=model,
            context=context,
        )

        return _image_result(
            prompt=result["prompt"],
            model_name=result["model"],
            images=result["images"],
            duration_seconds=float(result["generation_seconds"]),
            errors=result["errors"],
            include_upload_bytes=include_upload_bytes,
        )
    except Exception as e:
        return CallToolResult(
            content=[TextContent(type="text", text=f"Image generation failed: {e}")],
            structuredContent={"error": str(e)},
            isError=True,
        )


@mcp.tool(
    title="Start Image Generation Job",
    description="[WRITE] Queue image generation and return immediately with a job_id. Use this when you want Claude to keep working, then poll creative_check_image_generation_status until the image is ready.",
)
async def creative_start_image_generation(
    prompt: str,
    n: int = 1,
    size: str = "auto",
    quality: str = DEFAULT_IMAGE_QUALITY,
    model: str = "",
) -> CallToolResult:
    """[WRITE] Start image generation in the background and return a job ID."""
    try:
        _require_authenticated_user()
        user = current_user_ctx.get(None)
        if user is None:
            raise RuntimeError("Not authenticated.")
        job_id = f"imgjob_{uuid.uuid4()}"
        now = _now_iso()
        async with _IMAGE_GENERATION_JOB_LOCK:
            _IMAGE_GENERATION_JOBS[job_id] = {
                "job_id": job_id,
                "status": "queued",
                "prompt": prompt.strip(),
                "n": int(n),
                "size": _clean_optional(size) or "auto",
                "quality": quality,
                "model": _clean_optional(model) or DEFAULT_IMAGE_MODEL,
                "created_at": now,
                "updated_at": now,
                "updated_at_ts": datetime.now(timezone.utc).timestamp(),
                "progress_percent": 0,
                "progress_message": "Queued for image generation.",
                "result": None,
                "error": "",
            }
        asyncio.create_task(
            _run_image_generation_job(
                job_id=job_id,
                user=user,
                prompt=prompt,
                n=int(n),
                size=size,
                quality=quality,
                model=model,
            )
        )
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=(
                        f"Started image generation job {job_id}. Continue with other work, then call "
                        "creative_check_image_generation_status with this job_id until it is completed."
                    ),
                )
            ],
            structuredContent={
                "job_id": job_id,
                "status": "queued",
                "prompt": prompt.strip(),
                "next_step": "Call creative_check_image_generation_status with the returned job_id.",
            },
        )
    except Exception as e:
        return CallToolResult(
            content=[TextContent(type="text", text=f"Could not start image generation job: {e}")],
            structuredContent={"error": str(e)},
            isError=True,
        )


@mcp.tool(
    title="Check Image Generation Status",
    description="[READ] Check whether a background image generation job is queued, running, completed, or failed. Use this after creative_start_image_generation.",
)
async def creative_check_image_generation_status(job_id: str) -> CallToolResult:
    """[READ] Check status for a background image generation job."""
    job = await _get_image_job(job_id)
    if job is None:
        return CallToolResult(
            content=[TextContent(type="text", text="Image generation job was not found or expired.")],
            structuredContent={"error": "job_not_found", "job_id": job_id},
            isError=True,
        )

    result = job.get("result") or {}
    image_count = len(result.get("images") or []) if isinstance(result, dict) else 0
    content_text = (
        f"Job {job['job_id']} is {job['status']}. "
        f"{job.get('progress_message') or ''}".strip()
    )
    if job["status"] == "completed":
        content_text += f" Generated {image_count} image(s). Call creative_get_image_generation_result to fetch previews and generated_asset_id values."
    elif job["status"] == "failed" and job.get("error"):
        content_text += f" Error: {job['error']}"

    return CallToolResult(
        content=[TextContent(type="text", text=content_text)],
        structuredContent={
            "job_id": job["job_id"],
            "status": job["status"],
            "progress_percent": job.get("progress_percent", 0),
            "progress_message": job.get("progress_message", ""),
            "created_at": job.get("created_at", ""),
            "started_at": job.get("started_at", ""),
            "completed_at": job.get("completed_at", ""),
            "error": job.get("error", ""),
            "image_count": image_count,
        },
        isError=job["status"] == "failed",
    )


@mcp.tool(
    title="Get Image Generation Result",
    description="[READ] Fetch the completed result for a background image generation job. Returns previews plus generated_asset_id values when the job is done.",
)
async def creative_get_image_generation_result(
    job_id: str,
    include_upload_bytes: bool = False,
) -> CallToolResult:
    """[READ] Fetch the completed result for a background image generation job."""
    job = await _get_image_job(job_id)
    if job is None:
        return CallToolResult(
            content=[TextContent(type="text", text="Image generation job was not found or expired.")],
            structuredContent={"error": "job_not_found", "job_id": job_id},
            isError=True,
        )
    if job["status"] != "completed":
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=(
                        f"Job {job['job_id']} is {job['status']}. "
                        "Call creative_check_image_generation_status again until it is completed."
                    ),
                )
            ],
            structuredContent={
                "job_id": job["job_id"],
                "status": job["status"],
                "progress_percent": job.get("progress_percent", 0),
                "progress_message": job.get("progress_message", ""),
            },
            isError=job["status"] == "failed",
        )

    result = job.get("result") or {}
    return _image_result(
        prompt=str(result.get("prompt") or job.get("prompt") or ""),
        model_name=str(result.get("model") or job.get("model") or DEFAULT_IMAGE_MODEL),
        images=list(result.get("images") or []),
        duration_seconds=float(result.get("generation_seconds") or 0),
        errors=list(result.get("errors") or []),
        include_upload_bytes=include_upload_bytes,
    )


@mcp.tool(
    title="Legacy Alias: Generate Image",
    description="[WRITE][ALIAS] Legacy short alias for creative_generate_image. Same behavior, same output. Prefer creative_generate_image because its name is clearer for general prompt-to-image generation.",
)
async def generate_image(
    prompt: str,
    n: int = 1,
    size: str = "auto",
    quality: str = DEFAULT_IMAGE_QUALITY,
    model: str = "",
    include_upload_bytes: bool = False,
    context: Context | None = None,

) -> CallToolResult:
    """[WRITE][ALIAS] Legacy short alias for creative_generate_image.

    Same behavior as creative_generate_image. Prefer creative_generate_image when
    you want the clearest primary tool for prompt-to-image generation.
    """
    return await creative_generate_image(
        prompt,
        n=n,
        size=size,
        quality=quality,
        model=model,
        include_upload_bytes=include_upload_bytes,
        context=context,
    )


@mcp.tool(
    title="Generate Ad Image Variants From Marketing Brief",
    description="[WRITE] Generate multiple ad-image variants from structured marketing inputs like product, audience, platform, offer, and required text. Use this when the request is specifically about ad creative concepts rather than a single open-ended image prompt.",
)
async def creative_generate_ad_images(
    product_or_service: str,
    audience: str = "",
    platform: str = "Facebook Ads",
    language: str = "Hebrew",
    offer: str = "",
    style: str = "modern high-converting paid social creative",
    required_text: str = "",
    n: int = DEFAULT_AD_VARIANT_COUNT,
    size: str = "auto",
    quality: str = DEFAULT_MULTI_IMAGE_QUALITY,
    include_upload_bytes: bool = False,
    context: Context | None = None,

) -> CallToolResult:
    """[WRITE] Generate ad image variants from a structured marketing brief.

    Use this when the user wants performance-marketing creative variants and can
    describe product/service, audience, platform, offer, and required text as
    separate inputs. Prefer this over creative_generate_image for ad-specific
    multi-variant generation.

    Args:
        product_or_service: What the company sells or promotes.
        audience: Target audience for the ads.
        platform: Ad platform or placement, e.g. Facebook Ads, Instagram Feed, LinkedIn.
        language: Language for any text in the creative.
        offer: Main offer, promotion, or campaign angle.
        style: Visual style or creative direction.
        required_text: Exact words that should appear in the creative, if any.
        n: Number of image variants to generate. Defaults to 3.
        size: Image size, such as 'auto', '1024x1024', '1024x1536', or '1536x1024'.
        quality: Quality setting. Defaults to low for cheaper draft variants. Use medium or high only for shortlisted finals.
        include_upload_bytes: If true, include file_bytes_base64 for direct Meta upload. Default false to avoid oversized MCP responses.
    """
    brief_parts = [
        f"Create {n} distinct ad creative image variant(s) for {platform}.",
        f"Product or service: {product_or_service}.",
        f"Language for visible text: {language}.",
        f"Visual style: {style}.",
    ]
    if audience.strip():
        brief_parts.append(f"Target audience: {audience.strip()}.")
    if offer.strip():
        brief_parts.append(f"Main offer or angle: {offer.strip()}.")
    if required_text.strip():
        brief_parts.append(f"Use this visible text exactly where possible: {required_text.strip()}.")
    brief_parts.append("Make each variant feel like a polished performance marketing asset, with clear hierarchy and no clutter.")

    return await creative_generate_image(
        prompt="\n".join(brief_parts),
        n=n,
        size=size,
        quality=quality,
        include_upload_bytes=include_upload_bytes,
        context=context,
    )


@mcp.tool(
    title="Legacy Alias: Generate Ad Creative",
    description="[WRITE][ALIAS] Legacy short alias for creative_generate_ad_images. Same behavior. Prefer creative_generate_ad_images because it clearly signals structured ad-brief inputs and multi-variant output.",
)
async def generate_ad_creative(
    product_or_service: str,
    audience: str = "",
    platform: str = "Facebook Ads",
    language: str = "Hebrew",
    offer: str = "",
    style: str = "modern high-converting paid social creative",
    required_text: str = "",
    n: int = DEFAULT_AD_VARIANT_COUNT,
    size: str = "auto",
    quality: str = DEFAULT_MULTI_IMAGE_QUALITY,
    include_upload_bytes: bool = False,
    context: Context | None = None,

) -> CallToolResult:
    """[WRITE][ALIAS] Legacy short alias for creative_generate_ad_images.

    Same behavior as creative_generate_ad_images. Prefer the primary tool because
    its name makes the structured ad-brief workflow clearer to the model.
    """
    return await creative_generate_ad_images(
        product_or_service=product_or_service,
        audience=audience,
        platform=platform,
        language=language,
        offer=offer,
        style=style,
        required_text=required_text,
        n=n,
        size=size,
        quality=quality,
        include_upload_bytes=include_upload_bytes,
        context=context,
    )