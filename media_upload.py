"""Browser-side media upload: an in-chat widget that puts a file in the bucket.

WHY THIS EXISTS
---------------
A model cannot hand over the bytes of a picture a user pasted into chat. It
receives a rendering of the image, not the file, so any design that routes media
through a tool parameter is broken at the root -- and it fails quietly: a
truncated JPEG still decodes, so Meta returns a healthy-looking image_hash for a
damaged creative. Measured on 2026-08-10: the same 6405-byte JPEG produced two
different image_hashes depending on whether it travelled as base64 or as a URL.

So the model is removed from the byte path. A tool hands back a drop zone, the
browser POSTs the file straight here, and the model only ever sees the resulting
URL.

WHY THE TOKEN IS STATELESS
--------------------------
The upload token is an HMAC over (upload_id, user_id, expiry) keyed with
JWT_SECRET_KEY -- no table, no row to clean up, and nothing held in process
memory. That last part is deliberate: /api/temp-media used to keep images in a
per-instance dict, so on Cloud Run the URL it returned was only valid on the
instance that produced it. Anything stored in RAM here would repeat that bug.
Upload state lives in the bucket itself: the object either exists under the
upload's prefix or it does not.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/media", tags=["media"])

_TTL_SECONDS = int(os.environ.get("MEDIA_UPLOAD_TTL_SECONDS", "1800") or "1800")
# 32 MiB, because that is Cloud Run's own cap on an HTTP/1 request body and it
# is enforced at Google's front end -- the request never reaches this process.
# The old default said 50MB, which meant anything between 32 and 50 was
# accepted by our own validation and then rejected upstream with an HTML 413
# the widget could not parse, so it reported "failed" with no reason. A
# customer lost an afternoon to that on 2026-08-22 trying to upload ad videos.
_EDGE_MAX_BYTES = 32 * 1024 * 1024
_MAX_BYTES = int(os.environ.get("MEDIA_UPLOAD_MAX_BYTES", str(_EDGE_MAX_BYTES)))
# Only what an ad platform will actually take. An allowlist rather than a
# denylist: this endpoint authenticates with a bearer token in the URL, so it
# must never become a general-purpose file host.
_ALLOWED = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
    "image/webp": ".webp", "video/mp4": ".mp4", "video/quicktime": ".mov",
}

# CORS for the app iframe is handled by MediaCORSMiddleware in main.py, not
# here. It cannot be done at the route: the global CORSMiddleware runs with
# allow_credentials=True, so starlette's simple_headers always carries
# "Access-Control-Allow-Credentials: true" and send() does
# headers.update(self.simple_headers) on every response with an Origin. A
# route-set "Access-Control-Allow-Origin: *" would therefore ship next to
# ACAC:true, which browsers reject outright. Removing that header requires a
# layer outboard of the global middleware.


def _secret() -> bytes:
    key = os.environ.get("JWT_SECRET_KEY", "")
    if not key:
        raise RuntimeError("JWT_SECRET_KEY is required to sign media upload tokens.")
    return key.encode()


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def mint_token(user_id: str, upload_id: str, ttl: int = _TTL_SECONDS,
               destination: dict | None = None) -> str:
    claims: dict = {"u": user_id, "i": upload_id, "e": int(time.time()) + ttl}
    if destination:
        # Signed, so the ad this file lands on is fixed at the moment the tool
        # was called. A destination passed alongside the token instead could be
        # swapped by anyone holding the link.
        claims["d"] = destination
    payload = _b64u(json.dumps(claims).encode())
    sig = _b64u(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def read_token(token: str) -> dict:
    """Verify and decode. Raises HTTPException so routes can call it directly."""
    try:
        payload, sig = token.split(".", 1)
        expected = _b64u(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest())
        # compare_digest, not ==, so a wrong signature cannot be recovered byte
        # by byte from response timing.
        if not hmac.compare_digest(sig, expected):
            raise ValueError("bad signature")
        data = json.loads(_unb64u(payload))
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=403, detail="Invalid upload token.")
    if int(data.get("e", 0)) < time.time():
        raise HTTPException(status_code=403, detail="Upload link expired. Ask for a new one.")
    return data


def _bucket() -> str:
    return os.environ.get("GCS_MEDIA_BUCKET", "")


def _prefix(upload_id: str) -> str:
    return f"uploads/{upload_id}/"


def public_url(bucket: str, object_name: str) -> str:
    return f"https://storage.googleapis.com/{bucket}/{object_name}"


def find_uploads(upload_id: str) -> list[dict]:
    """Every file dropped into one slot, oldest first.

    One slot takes several files on purpose: a carousel, a set of variants to
    choose between, or an image and the video of the same creative. Each arrives
    as its own POST and lands under the slot's prefix, so the bucket listing is
    the whole record.
    """
    bucket_name = _bucket()
    if not bucket_name:
        return []
    try:
        from google.cloud import storage as gcs
        client = gcs.Client()
        found: list[dict] = []
        for blob in client.list_blobs(bucket_name, prefix=_prefix(upload_id)):
            meta = blob.metadata or {}
            entry = {
                "url": public_url(bucket_name, blob.name),
                "size_bytes": blob.size,
                "content_type": blob.content_type,
                "filename": blob.name.rsplit("/", 1)[-1],
                "sha256": meta.get("sha256", ""),
                "is_video": str(blob.content_type or "").startswith("video/"),
            }
            if meta.get("delivery"):
                try:
                    entry["delivery"] = json.loads(meta["delivery"])
                except Exception:
                    pass
            found.append(entry)
        found.sort(key=lambda f: f.get("filename") or "")
        return found
    except Exception:
        return []


def find_upload(upload_id: str) -> dict | None:
    """The first file in a slot. Kept for callers that only ever want one."""
    files = find_uploads(upload_id)
    return files[0] if files else None
    return None


@router.post("/u/{token}", include_in_schema=False)
async def receive_upload(token: str, request: Request, file: UploadFile = File(...),
                         confirm: str = Form("")):
    data = read_token(token)
    bucket_name = _bucket()
    if not bucket_name:
        raise HTTPException(status_code=503, detail="GCS_MEDIA_BUCKET is not configured on this deploy.")

    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > _MAX_BYTES + 2 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File is too large.")

    raw = await file.read(_MAX_BYTES + 1)
    if len(raw) > _MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"File is too large (limit {_MAX_BYTES // 1024 // 1024}MB).")
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file.")

    name = Path(file.filename or "upload").name.replace("\r", "").replace("\n", "").strip()[:150] or "upload"
    content_type = (file.content_type or mimetypes.guess_type(name)[0] or "").lower()
    if content_type not in _ALLOWED:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported type {content_type or 'unknown'}. Allowed: {', '.join(sorted(_ALLOWED))}.",
        )
    if not Path(name).suffix:
        name += _ALLOWED[content_type]

    digest = hashlib.sha256(raw).hexdigest()
    object_name = f"{_prefix(data['i'])}{name}"
    try:
        from google.cloud import storage as gcs
        blob = gcs.Client().bucket(bucket_name).blob(object_name)
        # The uploading user is recorded on the object, so a file in the bucket
        # can always be traced back to whoever put it there.
        blob.metadata = {"sha256": digest, "uploaded_by": str(data.get("u", "")), "original_name": name}
        blob.upload_from_string(raw, content_type=content_type)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Bucket write failed: {exc}")

    payload = {
        "ok": True,
        "url": public_url(bucket_name, object_name),
        "upload_id": data["i"],
        "size_bytes": len(raw),
        "sha256": digest,
        "content_type": content_type,
        "filename": name,
    }

    destination = data.get("d")
    if destination:
        confirmed = confirm.strip().lower() in ("1", "true", "yes")
        outcome = await run_in_threadpool(
            _deliver, destination, payload["url"], str(data.get("u", "")), confirmed
        )
        payload["destination"] = outcome
        # Record the outcome on the object itself, so media_upload_result can
        # tell the model what happened to the ad. There is no other store: the
        # bucket is the only state this feature keeps.
        await run_in_threadpool(_record_outcome, blob, outcome)

    return JSONResponse(payload)


def _record_outcome(blob, outcome: dict) -> None:
    try:
        blob.metadata = {**(blob.metadata or {}), "delivery": json.dumps(outcome)[:2000]}
        blob.patch()
    except Exception:
        # A lost annotation must not fail an upload that already succeeded --
        # the response the browser just got is the authoritative answer.
        logger.warning("media upload: could not record delivery outcome", exc_info=True)


def _deliver(destination: dict, url: str, user_id: str, confirmed: bool) -> dict:
    """Hand the freshly-stored file to wherever the tool call said it should go.

    Imported lazily and run off the event loop on purpose. `tools.meta_ads`
    imports `mcp_instance`, which imports `media_app`, which imports this
    module -- taking it at module scope would close that circle. And the Meta
    calls are blocking `requests` calls, so running them inline would stall the
    whole worker for the several seconds an upload-and-attach takes.
    """
    if destination.get("p") != "meta_ad":
        return {"ok": False, "error": f"Unknown destination {destination.get('p')!r}."}

    ad_id = str(destination.get("ad") or "")
    ad_name = destination.get("name") or ad_id
    live = str(destination.get("status") or "").upper() in ("ACTIVE", "CAMPAIGN_PAUSED", "ADSET_PAUSED")

    # The widget asks first, but a guard only in the browser is not a guard --
    # the link works outside the widget too.
    if live and not confirmed:
        return {
            "ok": False,
            "needs_confirmation": True,
            "ad_name": ad_name,
            "error": f"{ad_name!r} is live. Swapping its image sends it back to Meta for review.",
        }

    # current_user_ctx drives which stored OAuth token the Meta calls use. This
    # runs on an unauthenticated route -- the signed token is the credential --
    # so the user has to be put back on the context explicitly.
    from auth import current_user_ctx
    from tools import meta_ads

    class _TokenUser:
        id = user_id

        def get_meta_token(self) -> str | None:
            """The legacy single-token fallback, same as the ORM User exposes.

            meta_ads._token() tries the per-connection tokens first and only
            reaches this when none resolve -- which is precisely the case for an
            account whose Meta connection predates the multi-connection tables.
            Standing in a bare object with only an id meant those users got
            AttributeError: '_TokenUser' object has no attribute
            'get_meta_token' instead of an uploaded image, and because the
            delivery route catches everything to keep the upload from 500ing,
            it surfaced as prose in the tool result rather than in any traceback
            an alert would catch.
            """
            from credentials import provider

            return provider().legacy_meta_token_for_user(user_id)

    ctx = current_user_ctx.set(_TokenUser())
    try:
        result = meta_ads.swap_ad_image(ad_id, url, destination.get("account_id", ""))
    except Exception as exc:
        logger.exception("media upload: delivery to ad %s failed", ad_id)
        result = {"ok": False, "error": f"Could not attach the image to {ad_name}: {exc}"}
    finally:
        current_user_ctx.reset(ctx)

    result.setdefault("ad_name", ad_name)
    return result


# One document serves two hosts: the standalone fallback page at
# GET /media/u/{token}, and the MCP App iframe declared as ui://mcp-ads/media-upload.
# They differ only in where the upload endpoint comes from -- the standalone page
# reads it from its own URL, the app is handed it in the tool result -- so keeping
# one file means the drop zone cannot drift between the two paths.
#
# No external scripts, styles or fonts: the app iframe runs under a deny-by-default
# CSP where anything not declared in _meta.ui.csp is blocked, and adding a bundler
# to this repo to inline assets would be a poor trade for a single page.
_PAGE = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Upload media</title>
<style>
 :root{color-scheme:light dark}
 body{margin:0;font:14px/1.5 Inter,system-ui,-apple-system,'Segoe UI',sans-serif;
      display:grid;place-items:center;min-height:100vh;background:#FBF8F3;color:#16130F}
 @media (prefers-color-scheme:dark){body{background:#16130F;color:#FBF8F3}}
 body.embedded{min-height:0;background:transparent;padding:4px 0}
 .card{width:min(420px,92vw);text-align:center}
 label.drop{display:grid;place-items:center;gap:8px;padding:34px 20px;cursor:pointer;
      border:1.5px dashed rgba(128,116,100,.5);border-radius:10px;transition:border-color .15s,background .15s}
 label.drop:hover,label.drop.over{border-color:#C0532A;background:rgba(192,83,42,.06)}
 label.drop[aria-disabled=true]{opacity:.5;pointer-events:none}
 .hint{font-size:12px;opacity:.65}
 .bar{height:3px;border-radius:2px;background:rgba(128,116,100,.25);margin-top:14px;overflow:hidden;display:none}
 .bar>i{display:block;height:100%;width:0;background:#C0532A;transition:width .2s}
 .out{margin-top:14px;font-size:12px;word-break:break-all;text-align:left;display:none}
 .ok{color:#1F6F52}.err{color:#9B1D20}
 .diag{margin-top:10px;font-size:11px;opacity:.55}
 code{background:rgba(128,116,100,.14);padding:2px 5px;border-radius:4px;font-size:11px}
 input[type=file]{display:none}
 button{font:inherit;font-size:12px;padding:6px 12px;margin-top:8px;border-radius:6px;cursor:pointer;
      border:1px solid rgba(128,116,100,.45);background:transparent;color:inherit}
 button#y{background:#C0532A;border-color:#C0532A;color:#fff}
</style>
<div class="card">
 <label class="drop" id="d" aria-disabled="false">
   <strong id="t">Drop an image or video</strong>
   <span class="hint" id="h">paste with Ctrl+V, drop, or click to choose &middot; JPG, PNG, GIF, WebP, MP4, MOV</span>
   <input type="file" id="f" accept="image/*,video/*" multiple>
 </label>
 <div class="bar" id="b"><i id="p"></i></div>
 <div class="out" id="o"></div>
 <div class="diag" id="g"></div>
</div>
<script>
const D=document.getElementById('d'),F=document.getElementById('f'),B=document.getElementById('b'),
      P=document.getElementById('p'),O=document.getElementById('o'),G=document.getElementById('g');
const EMBEDDED = window.parent && window.parent !== window;
const MAX_BYTES = __MAX_BYTES__;
let ENDPOINT = null;
let DEST = null;      /* {label, live} when the file is going straight onto an ad */
let PENDING = null;   /* the file held back while a live ad awaits confirmation */
let BATCH = null;     /* {total, done, failed} while several files are going up */
let LAST_UPLOAD_ID = null;  /* the slot id, so a batch can report once at the end */

function show(html,cls){O.style.display='block';O.className='out '+(cls||'');O.innerHTML=html}
/* The handshake state is shown on screen deliberately. When a host does not mount
   this the way we expect, the failure is otherwise invisible: a blank box, and no
   way to tell whether the iframe, the CSP or the upload was at fault. */
function diag(text){G.textContent=text}
function ready(on){D.setAttribute('aria-disabled', on?'false':'true')}

/* MCP Apps (SEP-1865) postMessage lifecycle, hand-rolled. The SDK App class is a
   convenience, not a requirement, and pulling an npm build into this repo for one
   page is not worth it. */
let nextId=1; const pending=new Map(); const SEEN=[];
function rpc(method,params){
  const id=nextId++;
  window.parent.postMessage({jsonrpc:"2.0",id,method,params},'*');
  return new Promise((res,rej)=>{
    const t=setTimeout(()=>{pending.delete(id);rej(new Error('timed out: '+method))},15000);
    pending.set(id,{res,rej,t});
  });
}
function notify(method,params){window.parent.postMessage({jsonrpc:"2.0",method,params},'*')}

/* Hunt for the upload slot anywhere in the payload.
   structuredContent is where the spec puts it, but hosts differ in how deeply
   they nest a tool result, and a JSON-encoded text block is a common carrier
   too. Searching rather than reading two fixed paths means a host that wraps it
   one level differently still works, instead of presenting a dead drop zone. */
function findSlot(node, depth){
  if(!node || depth > 6) return null;
  if(typeof node === 'string'){
    if(node.indexOf('/media/u/') > -1 && node.indexOf('http') === 0) return {upload_url:node.trim()};
    if(node.length < 20000 && (node.indexOf('upload_url') > -1)){
      try{ return findSlot(JSON.parse(node), depth+1) }catch(_){ return null }
    }
    return null;
  }
  if(Array.isArray(node)){
    for(const v of node){ const hit = findSlot(v, depth+1); if(hit) return hit }
    return null;
  }
  if(typeof node === 'object'){
    /* Return the whole slot, not just the URL: the destination label and
       live flag sit beside it and drive what the drop zone says. */
    if(typeof node.upload_url === 'string' && node.upload_url) return node;
    for(const k in node){ const hit = findSlot(node[k], depth+1); if(hit) return hit }
  }
  return null;
}

/* A tool call can legitimately come back with no slot at all -- an ad name that
   matched four ads, say. Without this the zone just sits greyed out and blames
   the host, which sends the user looking in the wrong place entirely. */
function findError(node, depth){
  if(!node || depth > 6 || typeof node !== 'object') return null;
  if(typeof node.error === 'string' && node.error) return node.error;
  for(const k in node){ const hit = findError(node[k], depth+1); if(hit) return hit }
  return null;
}

function applyEndpoint(payload){
  const slot = findSlot(payload, 0);
  if(!slot){
    const err = findError(payload, 0);
    if(err){ ready(false); diag(err); return true }
    return false;
  }
  ENDPOINT = slot.upload_url;
  if(slot.destination_label){
    DEST = {label: slot.destination_label, live: !!slot.destination_live};
    document.getElementById('t').textContent = 'Drop or paste the new image for ' + DEST.label;
    document.getElementById('h').textContent = DEST.live
      ? 'This ad is live \\u2014 you will be asked to confirm before it changes'
      : 'paste with Ctrl+V, drop, or click to choose \\u00b7 JPG, PNG, GIF, WebP';
  }
  ready(true); diag('Ready to upload.'); return true;
}

if(EMBEDDED){
  document.body.classList.add('embedded');
  ready(false); diag('Connecting to the chat host\\u2026');
  window.addEventListener('message',ev=>{
    const m=ev.data; if(!m||m.jsonrpc!=='2.0')return;
    if(m.id&&pending.has(m.id)){
      const p=pending.get(m.id); clearTimeout(p.t); pending.delete(m.id);
      m.error?p.rej(new Error(m.error.message)):p.res(m.result); return;
    }
    if(m.method){ SEEN.push(String(m.method).replace('ui/notifications/','')); }
    /* Any inbound message may carry the slot -- tool-result is where it should
       be, but accepting it from tool-input or a host-specific variant costs
       nothing and avoids a dead zone if a host names things differently. */
    if(m.method && m.method.indexOf('ui/notifications/') === 0){ applyEndpoint(m.params); }
    if(m.method==='ui/resource-teardown'){ window.parent.postMessage({jsonrpc:"2.0",id:m.id,result:{}},'*'); }
  });
  rpc('ui/initialize',{
    /* protocolVersion is REQUIRED and is the apps-extension version, not the MCP
       one. Omitting it fails host-side schema validation before the handshake is
       even attempted -- the app mounts, then reports it cannot reach the host,
       which reads like a transport problem rather than a missing field. */
    protocolVersion:'2025-06-18',
    appInfo:{name:'MCP Ads Media Upload',version:'1.0.0'},
    appCapabilities:{availableDisplayModes:['inline']}
  }).then(r=>{
    notify('ui/notifications/initialized',{});
    if(r&&r.hostContext&&r.hostContext.theme==='dark')document.documentElement.style.colorScheme='dark';
    const host=(r&&r.hostInfo&&r.hostInfo.name)||'the host';
    if(!ENDPOINT){
      diag('Connected to '+host+'. Waiting for the upload slot\\u2026');
      /* The slot arrives on ui/notifications/tool-result. If one never does the
         zone would sit greyed out and unclickable forever with no explanation --
         which is exactly how a widget bound to the wrong tool presented. Say so,
         and point at the link, which always works. */
      setTimeout(function(){
        if(!ENDPOINT){
          ready(false);
          diag('No upload slot arrived'+(SEEN.length?' (host sent: '+SEEN.join(', ')+')':' (no notifications received)')+'. Ask the chat to run media_upload_start, or use the link it gave you.');
        }
      },8000);
    }
  }).catch(e=>{ ready(false); diag('Could not reach the chat host ('+e.message+').'); });
}else{
  ENDPOINT=location.pathname.replace(/\\/$/,'');
  ready(true);
}

['dragenter','dragover'].forEach(e=>D.addEventListener(e,ev=>{ev.preventDefault();D.classList.add('over')}));
['dragleave','drop'].forEach(e=>D.addEventListener(e,ev=>{ev.preventDefault();D.classList.remove('over')}));
D.addEventListener('drop',ev=>{sendAll(ev.dataTransfer.files)});
F.addEventListener('change',()=>{sendAll(F.files)});

/* Paste, because that is how people actually have an image in hand: they have
   just copied it, or pasted it into the chat and been told the model cannot
   read it. Ctrl+V here is the shortest path from clipboard to Meta -- no saving
   to disk, no hunting for the file. Bound to the document rather than the drop
   zone: an iframe this small is rarely the focused element, and requiring a
   click first would defeat the point. */
document.addEventListener('paste', ev => {
  const items = (ev.clipboardData && ev.clipboardData.items) || [];
  for (const item of items) {
    if (item.kind !== 'file') continue;
    const file = item.getAsFile();
    if (!file) continue;
    /* A pasted screenshot arrives named "image.png" or with no name at all;
       give it something recognisable in the bucket and on the ad. */
    const named = file.name && file.name !== 'image.png'
      ? file
      : new File([file], 'pasted-' + Date.now() + (file.type === 'image/jpeg' ? '.jpg' : '.png'),
                 {type: file.type});
    ev.preventDefault();
    send(named);
    return;
  }
});

/* Several files go up one at a time rather than at once. Sequential because a
   video can be tens of megabytes and the useful signal is "3 of 5 done", not
   five progress bars racing; and because each file is its own POST, so a
   failure part-way leaves the earlier ones safely stored rather than
   abandoning the batch. */
async function sendAll(list){
  const files = Array.from(list || []);
  if(!files.length) return;
  if(files.length === 1){ send(files[0]); return; }
  BATCH = {total: files.length, done: 0, failed: 0};
  for(const file of files){
    await new Promise(resolve => send(file, false, resolve));
  }
  const {total, done, failed} = BATCH;
  BATCH = null;
  show(failed
    ? done + ' of ' + total + ' uploaded, ' + failed + ' failed.'
    : 'All ' + total + ' files uploaded \\u2713', failed ? 'err' : 'ok');
  if(EMBEDDED && LAST_UPLOAD_ID){
    rpc('tools/call',{name:'media_upload_result',arguments:{upload_id:LAST_UPLOAD_ID}})
      .then(()=>diag('Sent to the chat.'))
      .catch(()=>diag('Uploaded. Tell the chat to continue.'));
  }
}

function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}

/* A live ad is asked about here, but the server refuses an unconfirmed swap on
   its own too -- the upload link works outside this widget, so a check that
   lived only in the browser would not be a check at all. */
function askConfirm(file, label){
  PENDING = file;
  show('<strong>'+esc(label)+'</strong> is running right now.<br>'+
       'Replacing its image sends it back to Meta for review, and its learning phase restarts.'+
       '<br><br><button id="y">Replace it anyway</button> <button id="n">Cancel</button>','');
  document.getElementById('y').onclick=()=>{const f=PENDING;PENDING=null;send(f,true)};
  document.getElementById('n').onclick=()=>{PENDING=null;show('Left unchanged.','');ready(true)};
}

function send(file, confirmed, done){
  /* `done` is how sendAll waits for one file before starting the next; it must
     fire on every exit path or a batch stalls forever. */
  const finish = ok => {
    if(BATCH){ ok ? BATCH.done++ : BATCH.failed++; }
    if(done) done();
  };
  if(!ENDPOINT){show('Not ready yet \\u2014 no upload slot received from the chat host.','err');finish(false);return}
  /* Refuse an oversized file here rather than sending it. Cloud Run rejects
     a body over 32 MiB at Google's front end, before any of our code runs,
     and answers with an HTML page -- so the upload appeared to fail for no
     reason at all. Ad videos are routinely larger than this, which is how a
     customer found it: three videos, three failures, no explanation. */
  if(file.size > MAX_BYTES){
    const mb=(file.size/1048576).toFixed(1), lim=Math.floor(MAX_BYTES/1048576);
    show(esc(file.name)+' is '+mb+' MB, over the '+lim+' MB limit for one upload.'+
         '<br><span class="hint">Compress or trim it, or host it somewhere and give the '+
         'assistant the link instead: the platform tools accept a video by URL.</span>','err');
    finish(false); return
  }
  const fd=new FormData();fd.append('file',file,file.name);
  if(confirmed)fd.append('confirm','1');
  const x=new XMLHttpRequest();x.open('POST',ENDPOINT);
  const label = BATCH ? '('+(BATCH.done+BATCH.failed+1)+' of '+BATCH.total+') ' : '';
  B.style.display='block';show('Uploading '+label+esc(file.name)+'\\u2026');diag('');
  x.upload.onprogress=e=>{if(e.lengthComputable)P.style.width=(e.loaded/e.total*100)+'%'};
  x.onload=()=>{
    P.style.width='100%';
    let r={};try{r=JSON.parse(x.responseText)}catch(_){}
    if(x.status!==200||!r.url){
      /* A 413 comes back from Google's front end as HTML, so r.detail is
         empty and the old message was a bare status number. */
      const why = (r&&r.detail) ? r.detail
        : x.status===413 ? 'the file is too large for one upload (limit '+Math.floor(MAX_BYTES/1048576)+' MB)'
        : x.status===0 ? 'the connection dropped before the upload finished'
        : 'HTTP '+x.status;
      show('Failed: '+esc(why),'err'); finish(false); return }
    LAST_UPLOAD_ID = r.upload_id || LAST_UPLOAD_ID;

    const d = r.destination;
    /* A confirmation prompt is the user's decision, not a finished upload, so a
       batch must not march on behind it. */
    if(d && d.needs_confirmation){ askConfirm(file, d.ad_name||'That ad'); finish(false); return }
    if(BATCH){ BATCH.done++; if(done) done(); return }

    if(d && d.ok){
      /* The whole point of the destination: the job is finished here, so say
         what changed rather than handing back a URL nobody needs to see. */
      show('Done \\u2713 new image is on <strong>'+esc(d.ad_name)+'</strong>'+
           (String(d.ad_status||'').toUpperCase()==='ACTIVE'
             ? '<br><span class="hint">Back in review with Meta \\u2014 usually minutes.</span>' : ''),'ok');
    } else if(d){
      show('Uploaded, but it did not reach the ad: '+esc(d.error||'unknown error'),'err');
    } else {
      show('Uploaded \\u2713 <code>'+esc(r.url)+'</code><br>'+r.size_bytes+' bytes &middot; sha256 '+esc(r.sha256.slice(0,16)),'ok');
    }

    if(EMBEDDED&&r.upload_id){
      /* Let the conversation carry on by itself rather than making the user
         say "done" -- the model needs the outcome, not a nudge. */
      rpc('tools/call',{name:'media_upload_result',arguments:{upload_id:r.upload_id}})
        .then(()=>diag('Sent to the chat.'))
        .catch(()=>diag(d&&d.ok?'':'Uploaded. Tell the chat to continue.'));
    }else if(!EMBEDDED&&!d){
      show(O.innerHTML+'<br><br>Paste this URL back into the chat.','ok');
    }
    if(done) done();
  };
  x.onerror=()=>{show('Network error during upload (CSP or CORS may have blocked it).','err');finish(false)};
  x.send(fd);
}
</script>"""


@router.get("/u/{token}", include_in_schema=False)
async def upload_page(token: str):
    read_token(token)  # 403 before rendering anything, rather than on submit
    # The widget has to know the ceiling to refuse a file before sending it.
    page = _PAGE.replace("__MAX_BYTES__", str(_MAX_BYTES))
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


def widget_html() -> str:
    """The MCP App document. Identical to the standalone page.

    No token is baked in on purpose: the host fetches this resource once and may
    cache or preload it before the tool is ever called, so a token embedded here
    would be stale or shared between invocations. The per-call upload URL arrives
    over ui/notifications/tool-result instead.
    """
    return _PAGE


def new_upload(user_id: str, destination: dict | None = None) -> tuple[str, str]:
    """Return (upload_id, token) for a fresh upload slot.

    `destination` travels inside the signed token rather than as a query
    parameter, so the ad a file lands on cannot be changed by editing the URL.
    """
    upload_id = uuid.uuid4().hex
    return upload_id, mint_token(user_id, upload_id, destination=destination)
