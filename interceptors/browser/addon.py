"""
mitmproxy addon: file upload interceptor for Windows DLP.

Run with:
    mitmdump -s addon.py --listen-port 8080
"""

import base64
import datetime
import json
import logging
import os
import tempfile
import threading
import time
from email.parser import BytesHeaderParser
from urllib.parse import urlparse, parse_qs, unquote

import ctypes

from mitmproxy import http, ctx

import pipe_client
from config import (
    Config,
    ConfigNotFoundError,
    config_from_ctl_payload,
    find_config_yaml,
    load_config,
)
from ctl_pipe_subscriber import CtlPipeSubscriber

log = logging.getLogger(__name__)

# Magic bytes → file extension mapping for Gmail resumable uploads
# (Gmail sends all resumable uploads as application/octet-stream)
_MAGIC_BYTES = [
    (b"%PDF", ".pdf"),
    (b"PK\x03\x04", ".zip"),  # .docx, .xlsx, .pptx are also ZIP-based
    (b"\xd0\xcf\x11\xe0", ".doc"),  # Old Office formats (OLE2)
    (b"\x50\x4b\x03\x04", ".docx"),  # Will be refined by content analysis
]

# ODF files (.odt, .ods, .odp) are also ZIP-based, detected as .zip by magic bytes.
# The DLP engine's extractor will handle them based on content structure.

# Refined detection: ZIP files that contain specific internal files
_ZIP_SIGNATURE = b"PK\x03\x04"
_DOCX_ZIP_MARKERS = [b"word/", b"docProps/", b"[Content_Types].xml"]
_XLSX_ZIP_MARKERS = [b"xl/", b"docProps/", b"[Content_Types].xml"]
_PPTX_ZIP_MARKERS = [b"ppt/", b"docProps/", b"[Content_Types].xml"]
_ODT_ZIP_MARKERS = [b"content.xml", b"meta.xml", b"META-INF/"]

# ODF declares its exact type in an uncompressed "mimetype" zip entry — the
# canonical signal. Without it, .ods/.odp would all collapse to .odt, which
# mislabels the audit log AND routes the orchestrator to the wrong extractor
# (.odt → lxml vs .ods → calamine) — a potential under-extraction / false ALLOW.
_ODF_MIMETYPE_TO_EXT = {
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/vnd.oasis.opendocument.spreadsheet": ".ods",
    "application/vnd.oasis.opendocument.presentation": ".odp",
}


def _detect_extension_from_content(body: bytes, current_filename: str) -> str:
    """Detect file extension from magic bytes when MIME type is application/octet-stream."""
    if not body:
        return current_filename

    # Check magic bytes
    for magic, ext in _MAGIC_BYTES:
        if body.startswith(magic):
            # For ZIP-based formats, try to determine the specific type
            if magic == _ZIP_SIGNATURE or body.startswith(_ZIP_SIGNATURE):
                ext = _detect_office_type_from_zip(body)
            if ext:
                # Replace filename with one that has the detected extension
                base = os.path.splitext(current_filename)[0] if current_filename else "upload"
                return base + ext
            break

    return current_filename


def _detect_office_type_from_zip(body: bytes) -> str:
    """Detect the specific Office/ODF format from ZIP file contents.

    ODF (.odt/.ods/.odp) is identified by its uncompressed ``mimetype`` entry so
    spreadsheets/presentations are not all collapsed to .odt. OOXML
    (.docx/.xlsx/.pptx) is identified by its well-known internal folders. Falls
    back to .odt for an ODF zip with no readable mimetype, else a generic .zip.
    """
    try:
        import zipfile
        import io
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            names = zf.namelist()
            # ODF: the 'mimetype' entry holds the exact type (most reliable).
            if "mimetype" in names:
                try:
                    mt = zf.read("mimetype").decode("ascii", "replace").strip()
                    ext = _ODF_MIMETYPE_TO_EXT.get(mt)
                    if ext:
                        return ext
                except Exception:
                    pass
            # OOXML formats by internal folder.
            if any(n.startswith("word/") for n in names):
                return ".docx"
            if any(n.startswith("xl/") for n in names):
                return ".xlsx"
            if any(n.startswith("ppt/") for n in names):
                return ".pptx"
            # ODF with no/unknown mimetype entry → default to a text document.
            if any("META-INF/" in n or n == "content.xml" for n in names):
                return ".odt"
    except Exception:
        pass
    return ".zip"  # Generic ZIP fallback

_upload_lock = threading.Lock()
# _cfg is hot-reloaded by the ctl-pipe subscriber thread; _cfg_lock guards the
# atomic swap so hooks never observe a half-built Config. Per-attribute reads
# in hooks still go through `_cfg.x` directly (CPython's GIL keeps them atomic).
_cfg_lock = threading.Lock()
_cfg: Config = Config()
_ctl_subscriber: CtlPipeSubscriber | None = None

# Resumable upload tracking:
# Step 1 POST (metadata) → _pending_resumable[id(flow)] = filename
# Step 1 response (Location header) → _resumable_filenames[upload_id] = filename
# Step 2 PUT (file bytes) → look up upload_id in _resumable_filenames
_pending_resumable: dict = {}   # flow id → filename (awaiting upload_id from server)
_resumable_filenames: dict = {} # upload_id → filename
_blocked_url_cache: dict = {}           # url → expiry_time (monotonic float)
_blocked_url_cache_lock = threading.Lock()
_BLOCK_CACHE_TTL = 60.0                 # seconds
# KNOWN LIMITATION (Gmail resumable): Only single-chunk Gmail resumable uploads
# are intercepted (Content-Range: bytes 0-N/N where N+1 equals total). Multi-chunk
# Gmail resumable would require reassembling across flows — not supported there.

# Generic chunked multipart/form-data reassembly (e.g. Zalo): large files arrive
# as several multipart/form-data POSTs to the SAME url (query stripped) carrying
# ?filesize=<total>&offset=<pos>. Analyzing each chunk as a whole file floods the
# log AND breaks context proximity across chunk boundaries (a value and its
# context word can land in different chunks → false ALLOW). We buffer the file
# part of each chunk and analyze only the COMPLETE reassembled file. Keyed by
# scheme://host/path (query stripped — all chunks of one upload share it).
_chunk_lock = threading.Lock()
_chunk_sessions: dict = {}              # session_key → {filesize,parts:{offset:bytes},filename,mime,ts}
_CHUNK_SESSION_TTL = 600.0             # seconds; drop abandoned uploads
_MAX_CHUNK_REASSEMBLY_BYTES = 200 * 1024 * 1024  # OOM guard; orchestrator's max_file_bytes still governs the verdict


def load(loader):
    global _cfg, _ctl_subscriber
    loader.add_option(
        "dlp_config_path", str, "",
        "Absolute path to the central config.yaml. Overrides DLP_CONFIG_PATH.",
    )


def running():
    """mitmproxy hook fired after options are bound — resolve config + spin up subscriber.

    We do this here (not in load) because ctx.options.dlp_config_path is only
    populated after option binding completes.
    """
    global _cfg, _ctl_subscriber
    try:
        config_path = _resolve_config_path()
    except ConfigNotFoundError as exc:
        log.error("DLP addon: %s", exc)
        return

    try:
        new_cfg = load_config(config_path)
    except Exception as exc:  # noqa: BLE001
        log.exception("DLP addon: failed to parse %s: %s", config_path, exc)
        return

    with _cfg_lock:
        _cfg = new_cfg

    tmp = _cfg.resolved_temp_dir()
    try:
        os.makedirs(tmp, exist_ok=True)
    except OSError as e:
        log.error("Cannot create temp dir %s: %s", tmp, e)
    log.info(
        "DLP addon loaded | source=%s pipe=%s timeout=%ss fail=%s temp=%s extensions=%s keywords=%s",
        config_path,
        _cfg.pipe_name,
        _cfg.timeout_seconds,
        _cfg.failure_mode,
        tmp,
        _cfg.extensions or "(all)",
        _cfg.upload_url_keywords,
    )

    # Wire the ctl-pipe subscriber. It reads the orchestrator's ctl pipe name
    # from the same yaml the addon just loaded.
    try:
        import yaml as _yaml
        with open(config_path, "r", encoding="utf-8") as f:
            ctl_pipe = (_yaml.safe_load(f) or {}).get("ctl_pipe", "")
    except Exception:  # noqa: BLE001
        ctl_pipe = ""
    if not ctl_pipe:
        log.warning("DLP addon: no ctl_pipe in config — running without hot reload")
        return

    _ctl_subscriber = CtlPipeSubscriber(ctl_pipe, "browser", _apply_ctl_update)
    _ctl_subscriber.start()


def done():
    """mitmproxy shutdown hook — stop the subscriber thread."""
    global _ctl_subscriber
    if _ctl_subscriber is not None:
        _ctl_subscriber.stop()
        _ctl_subscriber = None


def _resolve_config_path() -> str:
    """Mitmproxy option → env var → walk-up."""
    opt = (ctx.options.dlp_config_path or "").strip() if hasattr(ctx, "options") else ""
    if opt:
        if not os.path.exists(opt):
            raise ConfigNotFoundError(f"--set dlp_config_path={opt!r} does not exist")
        return opt
    env = os.environ.get("DLP_CONFIG_PATH", "").strip()
    if env:
        if not os.path.exists(env):
            raise ConfigNotFoundError(f"DLP_CONFIG_PATH={env!r} does not exist")
        return env
    return find_config_yaml()


def _apply_ctl_update(payload: dict) -> None:
    """ctl-pipe subscriber callback. Builds a fresh Config and swaps under lock."""
    global _cfg
    new_cfg = config_from_ctl_payload(payload, current_pipe_name=_cfg.pipe_name)
    with _cfg_lock:
        _cfg = new_cfg
    log.info(
        "ctl: config_update applied | fail=%s timeout=%ss pipe=%s",
        _cfg.failure_mode, _cfg.timeout_seconds, _cfg.pipe_name,
    )


def requestheaders(flow: http.HTTPFlow) -> None:
    """
    Force full body buffering for potential uploads BEFORE the body arrives.
    This must happen in requestheaders — by the time `request` fires it is too late.
    mitmproxy streams large bodies by default (especially over HTTP/2); setting
    flow.request.stream = False here forces it to buffer the whole body first.
    """
    if flow.request.method not in ("POST", "PUT"):
        return

    host = flow.request.pretty_host.lower()
    if any(host == d or host.endswith("." + d) for d in _cfg.domain_blocklist):
        return

    content_type = flow.request.headers.get("content-type", "").lower()

    # Explicit upload formats — always buffer
    if "multipart/form-data" in content_type or "multipart/related" in content_type:
        log.debug("Force-buffering %s %s (content-type: %s)", flow.request.method, flow.request.pretty_url, content_type)
        flow.request.stream = False
        return

    # Gmail attachment uploads — always buffer multipart/form-data to Gmail hosts
    host = flow.request.pretty_host.lower()
    if any(host == h or host.endswith("." + h) for h in _GMAIL_HOSTS):
        if "multipart/form-data" in content_type:
            log.debug("Force-buffering Gmail upload %s %s", flow.request.method, flow.request.pretty_url)
            flow.request.stream = False
            return
        # Gmail resumable upload initiation or file upload to /_/upload
        if "/_/upload" in flow.request.path.lower():
            log.debug("Force-buffering Gmail resumable upload %s %s", flow.request.method, flow.request.pretty_url)
            flow.request.stream = False
            return

    # For other types buffer if URL contains an upload keyword
    if any(kw in flow.request.path.lower() for kw in _cfg.upload_url_keywords):
        log.debug("Force-buffering %s %s (url keyword match)", flow.request.method, flow.request.pretty_url)
        flow.request.stream = False


def request(flow: http.HTTPFlow) -> None:
    # Detect batch PUT resumable initiation (large files, Google Drive step 1).
    # The actual resumable POST is wrapped inside a multipart/mixed PUT to /batch.
    if (flow.request.method == "PUT"
            and "multipart/mixed" in flow.request.headers.get("content-type", "").lower()
            and "batch" in flow.request.path.lower()):
        _track_resumable_initiation_batch(flow)
        return  # Not itself a file upload — just the initiation envelope

    # Gmail resumable upload initiation (metadata POST to /_/upload)
    if _is_gmail_resumable_initiation(flow):
        _track_gmail_resumable_initiation(flow)
        return  # Just metadata, not the actual file

    # Gmail resumable upload (raw file bytes with upload_protocol=resumable)
    if _is_gmail_resumable_upload(flow):
        _handle_gmail_resumable_upload(flow)
        return

    # Gmail multipart/form-data attachment upload
    if _is_gmail_attachment_upload(flow):
        _handle_gmail_attachments(flow)
        return

    if not _is_upload(flow):
        return

    url = flow.request.pretty_url
    if _is_blocked_url(url):
        log.debug("BLOCK (cached) | duplicate suppressed | %s", url[:80])
        flow.response = http.Response.make(
            403,
            b"Upload blocked by DLP policy.",
            {"Content-Type": "text/plain"},
        )
        return

    body = flow.request.content
    content_type = flow.request.headers.get("content-type", "")

    if not body:
        log.warning("Empty body for detected upload %s %s — body was not buffered (streamed?)",
                    flow.request.method, flow.request.pretty_url)
        return

    ct_lower = content_type.lower()

    # Google Drive / API style: multipart/related — metadata part + one file part.
    if "multipart/related" in ct_lower:
        filename, file_body, file_mime = _parse_multipart_related(body, content_type)
        if not filename:
            log.warning("SKIP multipart/related: could not extract filename from %s", flow.request.pretty_url)
            return
        _scan_file_and_apply(flow, filename, file_body, file_mime, url)
        return

    # Generic multipart/form-data (e.g. Zalo): extract EACH file part and scan it.
    # The old behavior wrote the WHOLE multipart envelope as the file, so the
    # analyzer read the boundary/headers instead of the content (a CSV's data
    # collapsed to one column) → no PII detected → false ALLOW (data leak).
    if "multipart/form-data" in ct_lower:
        files = _parse_multipart_form_files(body, content_type)
        if not files:
            log.debug("SKIP multipart/form-data: no file parts | %s", url[:80])
            return
        # Chunked upload (e.g. Zalo): the URL carries ?filesize=&offset= — buffer
        # and analyze the COMPLETE reassembled file instead of each 3 MB chunk.
        q = flow.request.query
        if "filesize" in q and "offset" in q:
            _handle_chunked_upload(flow, files, q, url)
            return
        for fn, fb, fm in files:
            if _scan_file_and_apply(flow, fn, fb, fm, url):
                return  # blocked — 403 response already set, stop scanning parts
        return

    # Anything else: a raw-body upload — the file bytes ARE the request body.
    filename = _extract_filename(flow)
    file_mime = content_type.split(";")[0].strip().lower()
    _scan_file_and_apply(flow, filename, body, file_mime, url)


def _scan_file_and_apply(flow: http.HTTPFlow, filename: str, file_body: bytes,
                         file_mime: str, url: str) -> bool:
    """Write ONE extracted file to a temp path, consult the policy, and on BLOCK
    set a 403 response + notify the user. Returns True if blocked (so a caller
    looping over multiple parts can stop). Type-filtered files are skipped
    (returns False). Centralizes the write→consult→respond logic so every upload
    shape (multipart/related, multipart/form-data, raw body) scans the REAL file
    bytes the same way."""
    if not _matches_type_filter(filename, file_mime):
        log.debug("SKIP (type filter) | %s | %s", filename, file_mime)
        return False

    try:
        temp_path = _write_temp_file(file_body, filename)
    except OSError as e:
        log.error("Failed to write temp file for '%s': %s → %s", filename, e, _cfg.failure_mode)
        if not _cfg.fail_open():
            _block_response(flow)
            return True
        return False

    payload = {
        "channel": "browser",
        "kind": "file",
        "file_path": temp_path,
        "metadata": {
            "url": flow.request.pretty_url,
            "filename": filename,
            "size_bytes": len(file_body),
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        },
    }

    decision, consumer_received, reason = _consult_policy(payload)

    # Temp file lifecycle: the consumer owns cleanup once it received the path;
    # if it never did (pipe error/timeout), we clean up ourselves.
    if not consumer_received:
        _delete_temp_file(temp_path)

    if decision == "BLOCK":
        _cache_blocked_url(url)
        log.info("BLOCK | %s | %d bytes | %s", filename, len(file_body), url[:80])
        _notify_blocked(filename, reason)
        _block_response(flow)
        return True

    log.info("ALLOW | %s | %d bytes | %s", filename, len(file_body), url[:80])
    return False


def _block_response(flow: http.HTTPFlow) -> None:
    """Set the standard 403 DLP block response on *flow*."""
    flow.response = http.Response.make(
        403, b"Upload blocked by DLP policy.", {"Content-Type": "text/plain"})


# ---------------------------------------------------------------------------
# Chunked multipart/form-data reassembly (Zalo-style ?filesize=&offset=)
# ---------------------------------------------------------------------------

def _session_key(flow: http.HTTPFlow) -> str:
    """scheme://host/path (query stripped) — identifies one chunked upload
    session; every chunk of the same file shares it (the query's offset differs)."""
    u = urlparse(flow.request.pretty_url)
    return f"{u.scheme}://{u.netloc}{u.path}"


def _evict_stale_chunk_sessions() -> None:
    """Drop chunk sessions untouched within the TTL. Caller holds _chunk_lock."""
    now = time.monotonic()
    for key in [k for k, s in _chunk_sessions.items() if now - s["ts"] > _CHUNK_SESSION_TTL]:
        log.debug("evicting stale chunk session %s", key[:80])
        del _chunk_sessions[key]


def _handle_chunked_upload(flow: http.HTTPFlow, files: list, query, url: str) -> None:
    """Buffer one chunk of a multipart/form-data upload (Zalo-style
    ?filesize=&offset=) and analyze ONLY the complete reassembled file.

    Intermediate chunks pass through untouched (no orchestrator call, no audit
    line — so the log isn't flooded). When the buffered bytes reach ``filesize``
    the whole file is scanned once via _scan_file_and_apply; a BLOCK 403s this
    (final) chunk — which fails the whole Zalo upload — and the session is cached
    as blocked so any retried/straggler chunk is 403'd too.
    """
    try:
        filesize = int(query.get("filesize", "0"))
        offset = int(query.get("offset", "0"))
    except (TypeError, ValueError):
        # Malformed chunk params — fall back to scanning this part on its own.
        for fn, fb, fm in files:
            if _scan_file_and_apply(flow, fn, fb, fm, url):
                return
        return

    filename, chunk_bytes, file_mime = files[0]   # one file part per chunk
    key = _session_key(flow)

    with _chunk_lock:
        _evict_stale_chunk_sessions()

        # A session already decided BLOCK (retry/straggler) → 403 immediately.
        if _is_blocked_url(key):
            _block_response(flow)
            return

        # Reject implausible / too-large uploads up front (OOM guard); the
        # orchestrator's max_file_bytes still governs normal-size verdicts.
        if filesize <= 0 or filesize > _MAX_CHUNK_REASSEMBLY_BYTES:
            log.warning("chunked upload filesize=%d unusable → fail %s | %s",
                        filesize, _cfg.failure_mode, url[:80])
            _chunk_sessions.pop(key, None)
            if not _cfg.fail_open():
                _cache_blocked_url(key)
                _block_response(flow)
            return

        sess = _chunk_sessions.get(key)
        if sess is None:
            sess = {"filesize": filesize, "parts": {}, "filename": filename,
                    "mime": file_mime, "ts": time.monotonic()}
            _chunk_sessions[key] = sess
        sess["parts"][offset] = chunk_bytes      # dedups retried offsets
        sess["ts"] = time.monotonic()
        if filename:
            sess["filename"] = filename

        received = sum(len(b) for b in sess["parts"].values())
        if received < filesize:
            log.debug("chunk buffered | %s | offset=%d received=%d/%d",
                      sess["filename"], offset, received, filesize)
            return  # not complete — let this chunk pass through to Zalo

        # Complete: pull the assembled file out under the lock, then scan it.
        nparts = len(sess["parts"])
        full_bytes = b"".join(sess["parts"][o] for o in sorted(sess["parts"]))
        fname, fmime = sess["filename"], sess["mime"]
        _chunk_sessions.pop(key, None)

    log.info("chunked upload complete | %s | %d chunks | %d bytes | %s",
             fname, nparts, len(full_bytes), url[:80])
    if _scan_file_and_apply(flow, fname, full_bytes, fmime, url):
        _cache_blocked_url(key)   # block any retried final chunk too


def response(flow: http.HTTPFlow) -> None:
    """For batch resumable upload step-1 responses, extract upload_id from the
    multipart/mixed body (Google Batch API format).
    Also extract upload_id from Gmail resumable initiation responses."""
    # Google Drive batch resumable
    flow_id = id(flow)
    if flow_id in _pending_resumable:
        filename = _pending_resumable.pop(flow_id)
        _extract_upload_id_from_batch_response(flow, filename)

    # Gmail resumable initiation response
    if flow_id in _gmail_pending_resumable:
        _extract_filename_from_gmail_resumable_response(flow)


def _extract_upload_id_from_batch_response(flow: http.HTTPFlow, filename: str) -> None:
    """
    Parse a Google Batch API multipart/mixed response body to find the upload_id
    embedded in the inner HTTP response's Location header.

    Expected body structure:
        --<boundary>
        Content-Type: application/http

        HTTP/1.1 200 OK
        Location: https://...?upload_id=ABC&session_crd=XYZ
        ...
        --<boundary>--
    """
    try:
        resp_ct = flow.response.headers.get("content-type", "")
        if "multipart/mixed" not in resp_ct.lower():
            log.debug("Batch response: not multipart/mixed (ct=%r), filename lost: %r", resp_ct, filename)
            return

        boundary = None
        for segment in resp_ct.split(";"):
            segment = segment.strip()
            if segment.lower().startswith("boundary="):
                boundary = segment[len("boundary="):].strip('"').strip("'")
                break
        if not boundary:
            log.debug("Batch response: no boundary in Content-Type: %s", resp_ct)
            return

        body = flow.response.content
        if not body:
            return

        delimiter = ("--" + boundary).encode()
        for chunk in body.split(delimiter):
            chunk = chunk.lstrip(b"\r\n")
            stripped = chunk.rstrip(b"\r\n")
            if not stripped or stripped == b"--":
                continue

            sep = b"\r\n\r\n" if b"\r\n\r\n" in chunk else b"\n\n"
            header_end = chunk.find(sep)
            if header_end == -1:
                continue
            part_headers = chunk[:header_end].lower()
            part_body = chunk[header_end + len(sep):]

            if b"content-type: application/http" not in part_headers:
                continue

            # part_body is a raw HTTP response; scan its lines for a Location header
            line_sep = b"\r\n" if b"\r\n" in part_body else b"\n"
            for line in part_body.split(line_sep):
                if line.lower().startswith(b"location:"):
                    location = line[len(b"location:"):].strip().decode("utf-8", errors="replace")
                    try:
                        qs = parse_qs(urlparse(location).query)
                        upload_ids = qs.get("upload_id", [])
                        if upload_ids:
                            _resumable_filenames[upload_ids[0]] = filename
                            log.debug("Batch resumable upload tracked: upload_id=%r → %r",
                                      upload_ids[0], filename)
                        else:
                            log.warning("Batch response: no upload_id in inner Location: %s", location)
                    except Exception as e:
                        log.warning("Batch response: failed to parse inner Location: %s", e)
                    return  # Only one inner response expected per initiation batch

        log.debug("Batch response: no application/http part with Location found, filename lost: %r", filename)

    except Exception as e:
        log.warning("Batch response parse failed: %s", e)


def _track_resumable_initiation_batch(flow: http.HTTPFlow) -> None:
    """
    Extract filename from a batch PUT that wraps a resumable upload initiation.

    The body is multipart/mixed; each part is content-type: application/http
    containing a full HTTP request. We find the inner POST, parse its JSON body,
    and store the filename in _pending_resumable so response() can correlate it
    with the upload_id returned in the batch response.
    """
    try:
        content_type = flow.request.headers.get("content-type", "")
        boundary = None
        for segment in content_type.split(";"):
            segment = segment.strip()
            if segment.lower().startswith("boundary="):
                boundary = segment[len("boundary="):].strip('"').strip("'")
                break
        if not boundary:
            log.debug("Batch init: no boundary in Content-Type: %s", content_type)
            return

        body = flow.request.content
        if not body:
            return

        delimiter = ("--" + boundary).encode()
        for chunk in body.split(delimiter):
            chunk = chunk.lstrip(b"\r\n")
            stripped = chunk.rstrip(b"\r\n")
            if not stripped or stripped == b"--":
                continue

            sep = b"\r\n\r\n" if b"\r\n\r\n" in chunk else b"\n\n"
            header_end = chunk.find(sep)
            if header_end == -1:
                continue
            part_headers = chunk[:header_end].lower()
            part_body = chunk[header_end + len(sep):]

            if b"content-type: application/http" not in part_headers:
                continue

            # part_body is a raw HTTP request:
            # POST /upload/... HTTP/1.1\r\nHeaders...\r\n\r\n{"title":"file.pdf",...}
            # Split on the blank line separator to isolate the JSON metadata body
            inner_sep = b"\r\n\r\n" if b"\r\n\r\n" in part_body else b"\n\n"
            inner_split = part_body.find(inner_sep)
            if inner_split == -1:
                continue
            json_bytes = part_body[inner_split + len(inner_sep):].rstrip(b"\r\n")
            if not json_bytes:
                continue

            try:
                metadata = json.loads(json_bytes.decode("utf-8"))
            except Exception as je:
                log.debug("Batch init: JSON parse failed: %s  raw=%r", je, json_bytes[:200])
                continue

            filename = metadata.get("title") or metadata.get("name") or ""
            if filename:
                _pending_resumable[id(flow)] = filename
                log.debug("Batch resumable init: queued filename=%r", filename)
            else:
                log.debug("Batch init: no title/name in metadata: %s", list(metadata.keys()))
            return  # Only one application/http part expected per initiation batch

    except Exception as e:
        log.debug("Batch resumable init parse failed: %s", e)


def _is_blocked_url(url: str) -> bool:
    """Return True if this URL has an active BLOCK cache entry."""
    with _blocked_url_cache_lock:
        expiry = _blocked_url_cache.get(url)
        if expiry is None:
            return False
        now = time.monotonic()
        if now >= expiry:
            del _blocked_url_cache[url]
            return False
        return True


def _cache_blocked_url(url: str) -> None:
    """Record url as blocked for _BLOCK_CACHE_TTL seconds."""
    now = time.monotonic()
    with _blocked_url_cache_lock:
        expired_keys = [k for k, exp in _blocked_url_cache.items() if now >= exp]
        for k in expired_keys:
            del _blocked_url_cache[k]
        _blocked_url_cache[url] = now + _BLOCK_CACHE_TTL


# ---------------------------------------------------------------------------
# Upload detection
# ---------------------------------------------------------------------------

def _is_upload(flow: http.HTTPFlow) -> bool:
    if flow.request.method not in ("POST", "PUT"):
        return False

    # 1. Domain blocklist
    host = flow.request.pretty_host.lower()
    if any(host == d or host.endswith("." + d) for d in _cfg.domain_blocklist):
        log.debug("SKIP (domain blocklist) | %s", host)
        return False

    content_type = flow.request.headers.get("content-type", "").lower()

    # 2. Explicit upload formats — no further heuristics needed
    if "multipart/form-data" in content_type or "multipart/related" in content_type:
        return True

    # 3. All other content types: require size + URL keyword + filename signal
    body_len = len(flow.request.content or b"")
    if body_len < _cfg.min_upload_size_bytes:
        return False

    if not _has_upload_url_keyword(flow):
        return False

    if not _has_filename_signal(flow):
        return False

    return True


def _has_upload_url_keyword(flow: http.HTTPFlow) -> bool:
    # flow.request.path already includes the query string in mitmproxy
    url_lower = flow.request.path.lower()
    return any(kw in url_lower for kw in _cfg.upload_url_keywords)


def _has_filename_signal(flow: http.HTTPFlow) -> bool:
    # Content-Disposition header on the request
    cd = flow.request.headers.get("content-disposition", "").lower()
    if "filename" in cd:
        return True

    # 'filename' or 'file_name' query parameter
    query = flow.request.query  # MultiDictView from mitmproxy
    if "filename" in query or "file_name" in query or "file" in query:
        return True

    # Resumable upload PUT: upload_id is present and we tracked its filename from step 1
    upload_id = query.get("upload_id", "")
    if upload_id and upload_id in _resumable_filenames:
        return True

    # File extension in URL path matching our allow-list (or any ext if no filter configured)
    path_segment = flow.request.path.split("?")[0].rsplit("/", 1)[-1]
    ext = os.path.splitext(path_segment)[1].lower()
    if ext:
        if not _cfg.has_type_filter():
            return True
        if ext in _cfg.extensions:
            return True

    return False


# ---------------------------------------------------------------------------
# Type filter
# ---------------------------------------------------------------------------

def _matches_type_filter(filename: str, mime: str) -> bool:
    if not _cfg.has_type_filter():
        return True
    ext = os.path.splitext(filename)[1].lower()
    if ext and ext in _cfg.extensions:
        return True
    if mime and mime in _cfg.mime_types:
        return True
    return False


# ---------------------------------------------------------------------------
# Filename extraction
# ---------------------------------------------------------------------------

def _extract_filename(flow: http.HTTPFlow) -> str:
    # Resumable upload PUT: look up the filename tracked from the initiation POST
    upload_id = flow.request.query.get("upload_id", "")
    if upload_id:
        filename = _resumable_filenames.get(upload_id, "")
        if filename:
            return filename

    content_type = flow.request.headers.get("content-type", "")
    if "multipart/form-data" in content_type.lower():
        name = _filename_from_multipart(flow.request.content, content_type)
        if name:
            return name
    path = flow.request.path.split("?")[0].rstrip("/")
    segment = path.rsplit("/", 1)[-1]
    return segment if segment else "upload"


def _filename_from_multipart(body: bytes, content_type: str) -> str:
    try:
        boundary = None
        for part in content_type.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part[len("boundary="):].strip('"')
                break
        if not boundary:
            return ""
        delimiter = ("--" + boundary).encode()
        for chunk in body.split(delimiter):
            if b"filename=" not in chunk:
                continue
            header_end = chunk.find(b"\r\n\r\n")
            if header_end == -1:
                continue
            raw_headers = chunk[:header_end].lstrip(b"\r\n")
            parser = BytesHeaderParser()
            msg = parser.parsebytes(b"Content-Disposition: " + _extract_cd(raw_headers))
            params = msg.get_params(header="content-disposition")
            for key, val in params:
                if key == "filename":
                    return val
    except Exception:
        pass
    return ""


def _extract_cd(raw_headers: bytes) -> bytes:
    for line in raw_headers.split(b"\r\n"):
        if line.lower().startswith(b"content-disposition:"):
            return line[len(b"content-disposition:"):].strip()
    return b""


def _parse_multipart_related(body: bytes, content_type: str):
    """
    Parse a multipart/related body (Google Drive API format).
    Returns (filename, file_bytes, file_mime_type).

    Part 0: JSON metadata — Drive v2 uses 'title', v3 uses 'name'.
    Part 1: actual file bytes.
    Handles both CRLF and bare-LF line endings.
    """
    try:
        # --- Extract boundary ---
        boundary = None
        for segment in content_type.split(";"):
            segment = segment.strip()
            if segment.lower().startswith("boundary="):
                boundary = segment[len("boundary="):].strip('"').strip("'")
                break
        if not boundary:
            log.warning("multipart/related: no boundary found in Content-Type: %s", content_type)
            return "", body, ""

        delimiter = ("--" + boundary).encode()
        log.debug("multipart/related boundary=%r  body_len=%d  body_prefix=%r",
                  boundary, len(body), body[:120])

        # --- Split into raw chunks ---
        raw_chunks = body.split(delimiter)
        parts = []
        for chunk in raw_chunks:
            # Strip leading CRLF or LF after the boundary line
            chunk = chunk.lstrip(b"\r\n")
            # Skip preamble (empty) and epilogue ("--" suffix)
            stripped = chunk.rstrip(b"\r\n")
            if not stripped or stripped == b"--":
                continue
            # Support both \r\n\r\n and \n\n as the header/body separator
            sep = b"\r\n\r\n" if b"\r\n\r\n" in chunk else b"\n\n"
            header_end = chunk.find(sep)
            if header_end == -1:
                log.debug("multipart/related: no header separator in chunk, skipping")
                continue
            header_bytes = chunk[:header_end]
            part_body = chunk[header_end + len(sep):].rstrip(b"\r\n")
            parts.append((header_bytes, part_body))
            log.debug("multipart/related part %d: headers=%r  body_len=%d  body_prefix=%r",
                      len(parts) - 1, header_bytes[:80], len(part_body), part_body[:60])

        if len(parts) < 2:
            log.warning("multipart/related: expected >=2 parts, got %d (boundary=%r)", len(parts), boundary)
            return "", body, ""

        # --- Part 0: JSON metadata ---
        _, json_body = parts[0]
        try:
            metadata = json.loads(json_body.decode("utf-8"))
        except Exception as je:
            log.warning("multipart/related: failed to parse metadata JSON: %s  raw=%r", je, json_body[:200])
            return "", body, ""

        # Drive v3 → "name", Drive v2 → "title"
        filename = metadata.get("name") or metadata.get("title") or ""
        log.debug("multipart/related metadata keys=%s  filename=%r", list(metadata.keys()), filename)

        # --- Part 1: file content ---
        file_headers, file_body_bytes = parts[1]
        file_mime = ""
        transfer_encoding = ""
        sep_line = b"\r\n" if b"\r\n" in file_headers else b"\n"
        for line in file_headers.split(sep_line):
            ll = line.lower()
            if ll.startswith(b"content-type:"):
                file_mime = line[len(b"content-type:"):].strip().decode("utf-8", errors="replace")
                file_mime = file_mime.split(";")[0].strip().lower()
            elif ll.startswith(b"content-transfer-encoding:"):
                transfer_encoding = line[len(b"content-transfer-encoding:"):].strip().decode("utf-8", errors="replace").strip().lower()

        # Decode transfer encoding so the temp file contains raw bytes
        if transfer_encoding == "base64":
            try:
                file_body_bytes = base64.b64decode(file_body_bytes)
                log.debug("multipart/related: decoded base64 → %d bytes", len(file_body_bytes))
            except Exception as be:
                log.warning("multipart/related: base64 decode failed: %s", be)

        return filename, file_body_bytes, file_mime

    except Exception as e:
        log.warning("Failed to parse multipart/related: %s", e, exc_info=True)
        return "", body, ""


# ---------------------------------------------------------------------------
# Gmail attachment handling
# ---------------------------------------------------------------------------

_GMAIL_HOSTS = (
    "mail.google.com",
    "content-gmail.google.com",
    "content-upload.mail.google.com",
)

# Gmail resumable upload tracking (same pattern as Drive batch resumable):
# Initiation POST → _gmail_pending_resumable[id(flow)] = filename
# Initiation response → _gmail_resumable_filenames[upload_id] = filename
# File PUT/POST → look up upload_id in _gmail_resumable_filenames
_gmail_pending_resumable: dict = {}       # flow id → filename
_gmail_resumable_filenames: dict = {}     # upload_id → filename


def _is_gmail_attachment_upload(flow: http.HTTPFlow) -> bool:
    """Detect Gmail compose attachment upload requests (multipart/form-data)."""
    if flow.request.method != "POST":
        return False
    host = flow.request.pretty_host.lower()
    if not any(host == h or host.endswith("." + h) for h in _GMAIL_HOSTS):
        return False
    content_type = flow.request.headers.get("content-type", "").lower()
    if "multipart/form-data" not in content_type:
        return False
    body = flow.request.content
    if not body:
        return False
    return _has_file_parts(body, content_type)


def _is_gmail_resumable_upload(flow: http.HTTPFlow) -> bool:
    """Detect Gmail resumable upload (raw file bytes with upload_protocol=resumable)."""
    if flow.request.method not in ("POST", "PUT"):
        return False
    host = flow.request.pretty_host.lower()
    if not any(host == h or host.endswith("." + h) for h in _GMAIL_HOSTS):
        return False
    query = flow.request.query
    upload_protocol = query.get("upload_protocol", "")
    upload_id = query.get("upload_id", "")
    # Resumable upload: has upload_protocol=resumable OR tracked upload_id
    if upload_protocol.lower() == "resumable":
        return True
    if upload_id and upload_id in _gmail_resumable_filenames:
        return True
    return False


def _is_gmail_resumable_initiation(flow: http.HTTPFlow) -> bool:
    """Detect Gmail resumable upload initiation (metadata POST to /_/upload without upload_protocol)."""
    if flow.request.method != "POST":
        return False
    host = flow.request.pretty_host.lower()
    if not any(host == h or host.endswith("." + h) for h in _GMAIL_HOSTS):
        return False
    path = flow.request.path.lower()
    if "/_/upload" not in path:
        return False
    query = flow.request.query
    # Initiation: has /_/upload but NO upload_protocol=resumable
    return query.get("upload_protocol", "").lower() != "resumable"


def _has_file_parts(body: bytes, content_type: str) -> bool:
    """Quick check: does the multipart body contain any file parts?"""
    try:
        boundary = _extract_boundary(content_type)
        if not boundary:
            return False
        delimiter = ("--" + boundary).encode()
        for chunk in body.split(delimiter):
            if b"filename=" in chunk and b"Content-Type:" in chunk:
                return True
    except Exception:
        pass
    return False


def _handle_gmail_attachments(flow: http.HTTPFlow) -> None:
    """Extract each attachment from a Gmail compose upload and run DLP check."""
    url = flow.request.pretty_url
    if _is_blocked_url(url):
        log.debug("BLOCK (cached) | Gmail attachment | %s", url[:80])
        _block_gmail(flow)
        return

    body = flow.request.content
    content_type = flow.request.headers.get("content-type", "")
    attachments = _parse_multipart_form_files(body, content_type)

    if not attachments:
        log.debug("SKIP Gmail upload: no extractable attachments | %s", url[:80])
        return

    # Check each attachment against DLP policy
    for filename, file_body, file_mime in attachments:
        if not _matches_type_filter(filename, file_mime):
            log.debug("SKIP (type filter) | Gmail | %s | %s", filename, file_mime)
            continue

        try:
            temp_path = _write_temp_file(file_body, filename)
        except OSError as e:
            log.error("Failed to write temp file for Gmail attachment '%s': %s", filename, e)
            if not _cfg.fail_open():
                _block_gmail(flow)
            return

        payload = {
            "channel": "browser",
            "kind": "file",
            "file_path": temp_path,
            "metadata": {
                "url": flow.request.pretty_url,
                "filename": filename,
                "size_bytes": len(file_body),
                "service": "gmail",
                "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
            },
        }

        decision, consumer_received, reason = _consult_policy(payload)

        if not consumer_received:
            _delete_temp_file(temp_path)

        if decision == "BLOCK":
            log.info("BLOCK | Gmail attachment | %s | %d bytes | %s",
                     filename, len(file_body), url[:80])
            _cache_blocked_url(url)
            _notify_blocked(filename, reason)
            _block_gmail(flow)
            return
        else:
            log.info("ALLOW | Gmail attachment | %s | %d bytes | %s",
                     filename, len(file_body), url[:80])


def _handle_gmail_resumable_upload(flow: http.HTTPFlow) -> None:
    """Handle Gmail resumable upload (raw file bytes)."""
    url = flow.request.pretty_url
    if _is_blocked_url(url):
        log.debug("BLOCK (cached) | Gmail resumable upload | %s", url[:80])
        _block_gmail(flow)
        return

    # Get filename from tracked resumable or from URL
    query = flow.request.query
    upload_id = query.get("upload_id", "")
    filename = _gmail_resumable_filenames.get(upload_id, "")
    if not filename:
        # Fallback: try to extract from URL path
        path_segment = flow.request.path.split("?")[0].rsplit("/", 1)[-1]
        filename = path_segment if path_segment else "gmail_attachment"

    body = flow.request.content
    if not body:
        log.warning("Empty body for Gmail resumable upload | %s", url[:80])
        return

    file_mime = flow.request.headers.get("content-type", "").split(";")[0].strip().lower()

    # Gmail resumable uploads always use application/octet-stream regardless of
    # actual file type. The URL pattern (/_/upload + upload_protocol=resumable)
    # is already a strong upload signal — skip type filter and let DLP engine
    # analyze the actual content.
    if not file_mime.startswith("application/octet-stream"):
        if not _matches_type_filter(filename, file_mime):
            log.debug("SKIP (type filter) | Gmail resumable | %s | %s", filename, file_mime)
            return

    # Detect actual file extension from magic bytes (Gmail sends everything as octet-stream)
    original_filename = filename
    filename = _detect_extension_from_content(body, filename)
    if filename != original_filename:
        log.debug("Detected file type from content: %s → %s", original_filename, filename)

    try:
        temp_path = _write_temp_file(body, filename)
    except OSError as e:
        log.error("Failed to write temp file for Gmail resumable '%s': %s", filename, e)
        if not _cfg.fail_open():
            _block_gmail(flow)
        return

    payload = {
        "channel": "browser",
        "kind": "file",
        "file_path": temp_path,
        "metadata": {
            "url": flow.request.pretty_url,
            "filename": filename,
            "size_bytes": len(body),
            "service": "gmail",
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        },
    }

    decision, consumer_received, reason = _consult_policy(payload)

    if not consumer_received:
        _delete_temp_file(temp_path)

    if decision == "BLOCK":
        log.info("BLOCK | Gmail resumable upload | %s | %d bytes | %s",
                 filename, len(body), url[:80])
        _cache_blocked_url(url)
        _notify_blocked(filename, reason)
        _block_gmail(flow)
    else:
        log.info("ALLOW | Gmail resumable upload | %s | %d bytes | %s",
                 filename, len(body), url[:80])


def _track_gmail_resumable_initiation(flow: http.HTTPFlow) -> None:
    """Extract filename from Gmail resumable upload initiation POST."""
    body = flow.request.content
    if not body:
        return

    content_type = flow.request.headers.get("content-type", "").lower()

    # Try JSON body first
    if "application/json" in content_type:
        try:
            metadata = json.loads(body.decode("utf-8"))
            filename = metadata.get("filename") or metadata.get("name") or metadata.get("title") or ""
            if filename:
                _gmail_pending_resumable[id(flow)] = filename
                log.debug("Gmail resumable init (JSON): queued filename=%r", filename)
            return
        except Exception:
            pass

    # Try multipart/form-data body
    if "multipart/form-data" in content_type:
        boundary = _extract_boundary(content_type)
        if boundary:
            delimiter = ("--" + boundary).encode()
            for chunk in body.split(delimiter):
                if b"filename=" not in chunk:
                    continue
                filename = _extract_filename_from_multipart_chunk(chunk)
                if filename:
                    _gmail_pending_resumable[id(flow)] = filename
                    log.debug("Gmail resumable init (multipart): queued filename=%r", filename)
                    return


def _extract_filename_from_gmail_resumable_response(flow: http.HTTPFlow) -> None:
    """Extract upload_id from Gmail resumable initiation response and correlate with filename."""
    flow_id = id(flow)
    if flow_id not in _gmail_pending_resumable:
        return

    filename = _gmail_pending_resumable.pop(flow_id)

    # Try to get upload_id from response headers (X-GUploader-UploadID or Location)
    upload_id = (
        flow.response.headers.get("x-guploader-uploadid", "")
        or flow.response.headers.get("x-gupload-uploadid", "")
    )

    # Try to extract from response body if it contains upload_id
    if not upload_id and flow.response.content:
        try:
            body = flow.response.content.decode("utf-8", errors="replace")
            # Gmail may return upload_id in various formats
            for key in ("upload_id", "uploadId", "X-GUploader-UploadID"):
                if key in body:
                    # Try to extract value after the key
                    idx = body.index(key)
                    rest = body[idx + len(key):]
                    for sep in ('"', "'", ":", "=", ">"):
                        if sep in rest:
                            val = rest.split(sep, 1)[1]
                            for end_sep in ('"', "'", "&", "<", " ", "\n", "\r"):
                                if end_sep in val:
                                    upload_id = val.split(end_sep, 1)[0].strip()
                                    break
                            if upload_id:
                                break
                    if upload_id:
                        break
        except Exception:
            pass

    # If no upload_id found, use flow ID as fallback key
    if not upload_id:
        upload_id = f"gmail_flow_{flow_id}"
        log.debug("Gmail resumable: no upload_id in response, using fallback key for %r", filename)

    _gmail_resumable_filenames[upload_id] = filename
    log.debug("Gmail resumable tracked: upload_id=%r → %r", upload_id, filename)


def _block_gmail(flow: http.HTTPFlow) -> None:
    """Return a 403 response for a Gmail upload request."""
    flow.response = http.Response.make(
        403,
        b"Attachment blocked by DLP policy.",
        {"Content-Type": "text/plain"},
    )


def _notify_blocked(filename: str, reason: str) -> None:
    """Show a block notice to the interactive user (runs in a thread so it never
    blocks the proxy).

    Uses WTSSendMessageW to render the box on the active console session's desktop
    rather than user32.MessageBoxW. Reason: under the DLPAgent service mitmdump
    runs in Session 0, whose desktop the user never sees (and Interactive Services
    Detection was removed in Win10 1803+), so a plain MessageBox would be invisible.
    In --foreground the active console session is the user's, so this works there too.
    """
    def _show():
        try:
            title = "Upload Blocked by DLP"
            # The upload was 403'd. Google Drive (and similar) surface that 403 as
            # a generic "network error" and keep retrying, so the user must stop
            # the upload themselves and reload the page — tell them explicitly.
            guidance = ("Hành động: Vui lòng TẢI LẠI (refresh) trang web và NGỪNG/HỦY "
                        "tải tệp này. Tệp bị chặn có thể khiến trình duyệt báo lỗi mạng.")
            header = f"File: {filename}" if not reason else f"File: {filename}\nReason: {reason}"
            msg = f"{header}\n\n{guidance}"
            kernel32 = ctypes.windll.kernel32
            kernel32.WTSGetActiveConsoleSessionId.restype = ctypes.c_uint
            session_id = kernel32.WTSGetActiveConsoleSessionId()
            if session_id == 0xFFFFFFFF:   # no active console session
                return
            response = ctypes.c_uint(0)
            ctypes.windll.wtsapi32.WTSSendMessageW(
                0,                       # WTS_CURRENT_SERVER_HANDLE
                session_id,
                title, len(title) * 2,   # pTitle + byte length (wide chars)
                msg, len(msg) * 2,       # pMessage + byte length
                0x40 | 0x1000,           # MB_ICONINFORMATION | MB_TOPMOST
                0,                       # no auto-dismiss timeout
                ctypes.byref(response),
                False,                   # bWait=False — don't block on the user
            )
        except Exception:
            pass
    threading.Thread(target=_show, daemon=True).start()


def _parse_multipart_form_files(body: bytes, content_type: str) -> list[tuple[str, bytes, str]]:
    """
    Parse ANY multipart/form-data body and extract all file parts (parts with a
    ``filename=`` in their Content-Disposition). Used for Gmail compose uploads
    AND generic uploads from other hosts (e.g. Zalo). Decodes base64
    Content-Transfer-Encoding so the temp file holds raw bytes.
    Returns list of (filename, file_bytes, mime_type).
    """
    attachments: list[tuple[str, bytes, str]] = []
    try:
        boundary = _extract_boundary(content_type)
        if not boundary:
            return attachments

        delimiter = ("--" + boundary).encode()
        for chunk in body.split(delimiter):
            chunk = chunk.lstrip(b"\r\n")
            stripped = chunk.rstrip(b"\r\n")
            if not stripped or stripped == b"--" or stripped == b"--\r\n":
                continue

            sep = b"\r\n\r\n" if b"\r\n\r\n" in chunk else b"\n\n"
            header_end = chunk.find(sep)
            if header_end == -1:
                continue

            header_bytes = chunk[:header_end]
            part_body = chunk[header_end + len(sep):]
            # Strip ONLY the single CRLF that precedes the boundary delimiter —
            # a blind rstrip(b"\r\n") would also eat the file's own trailing
            # newlines, corrupting the byte count the chunk-reassembly completion
            # check (received == filesize) relies on (CSV chunks end in \n).
            if part_body.endswith(b"\r\n"):
                part_body = part_body[:-2]
            elif part_body.endswith(b"\n"):
                part_body = part_body[:-1]

            # Check for filename in Content-Disposition
            cd = _extract_cd_from_headers(header_bytes)
            if not cd or b"filename=" not in cd.lower():
                continue

            filename = _extract_filename_from_cd(cd)
            if not filename:
                continue

            # Extract content type of the part
            part_mime = ""
            transfer_encoding = ""
            sep_line = b"\r\n" if b"\r\n" in header_bytes else b"\n"
            for line in header_bytes.split(sep_line):
                ll = line.lower()
                if ll.startswith(b"content-type:") and b"multipart" not in ll:
                    part_mime = line[len(b"content-type:"):].strip().decode("utf-8", errors="replace")
                    part_mime = part_mime.split(";")[0].strip().lower()
                elif ll.startswith(b"content-transfer-encoding:"):
                    transfer_encoding = line[len(b"content-transfer-encoding:"):].strip().decode("utf-8", errors="replace").strip().lower()

            file_bytes = part_body
            if transfer_encoding == "base64":
                try:
                    file_bytes = base64.b64decode(file_bytes)
                except Exception:
                    pass

            attachments.append((filename, file_bytes, part_mime))

    except Exception as e:
        log.warning("Failed to parse Gmail attachments: %s", e, exc_info=True)

    return attachments


def _extract_boundary(content_type: str) -> str | None:
    """Extract boundary string from a multipart Content-Type header."""
    for segment in content_type.split(";"):
        segment = segment.strip()
        if segment.lower().startswith("boundary="):
            return segment[len("boundary="):].strip('"').strip("'")
    return None


def _extract_cd_from_headers(header_bytes: bytes) -> bytes:
    """Extract Content-Disposition value from raw header bytes."""
    sep_line = b"\r\n" if b"\r\n" in header_bytes else b"\n"
    for line in header_bytes.split(sep_line):
        if line.lower().startswith(b"content-disposition:"):
            return line[len(b"content-disposition:"):].strip()
    return b""


def _extract_filename_from_multipart_chunk(chunk: bytes) -> str:
    """Extract filename from a multipart/form-data chunk's headers."""
    sep = b"\r\n\r\n" if b"\r\n\r\n" in chunk else b"\n\n"
    header_end = chunk.find(sep)
    if header_end == -1:
        return ""
    raw_headers = chunk[:header_end].lstrip(b"\r\n")
    cd = _extract_cd_from_headers(raw_headers)
    if not cd:
        return ""
    return _extract_filename_from_cd(cd)


def _extract_filename_from_cd(cd: bytes) -> str:
    """Extract filename from a Content-Disposition header value."""
    # Try filename*=UTF-8''... first (RFC 5987)
    try:
        idx = cd.lower().index(b"filename*=")
        val = cd[idx + len(b"filename*="):]
        # Format: UTF-8''encoded-text or charset="UTF-8"''text
        if b"''" in val:
            val = val.split(b"''", 1)[1]
        filename = unquote(val.decode("utf-8", errors="replace"))
        if filename:
            return filename
    except (ValueError, IndexError):
        pass

    # Try filename="..." or filename=...
    try:
        idx = cd.lower().index(b"filename=")
        val = cd[idx + len(b"filename="):].strip()
        if val.startswith(b'"'):
            end = val.index(b'"', 1)
            return val[1:end].decode("utf-8", errors="replace")
        else:
            end = val.index(b";") if b";" in val else len(val)
            return val[:end].strip().decode("utf-8", errors="replace")
    except (ValueError, IndexError):
        pass

    return ""


# ---------------------------------------------------------------------------
# Temp file
# ---------------------------------------------------------------------------

def _write_temp_file(body: bytes, filename: str) -> str:
    """Write body to temp dir using the original filename.

    If a file with that name already exists (e.g. concurrent upload of the
    same filename), a numeric suffix is inserted before the extension.
    """
    tmp_dir = _cfg.resolved_temp_dir()
    base, ext = os.path.splitext(filename) if filename else ("upload", "")
    dest = os.path.join(tmp_dir, filename if filename else "upload")
    counter = 0
    while os.path.exists(dest):
        counter += 1
        dest = os.path.join(tmp_dir, f"{base}_{counter}{ext}")
    with open(dest, "wb") as f:
        f.write(body)
    return dest


def _delete_temp_file(path: str) -> None:
    try:
        os.unlink(path)
    except OSError as e:
        log.warning("Could not delete temp file %s: %s", path, e)


# ---------------------------------------------------------------------------
# Policy pipe
# ---------------------------------------------------------------------------

def _consult_policy(payload: dict) -> tuple:
    """
    Returns (decision: str, consumer_received: bool, reason: str).
    consumer_received=True means the consumer got the temp_path and owns cleanup.
    """
    with _upload_lock:
        try:
            decision, reason = pipe_client.send_and_receive(
                payload,
                _cfg.pipe_name,
                _cfg.timeout_seconds,
            )
            return decision, True, reason
        except TimeoutError as e:
            log.warning("Pipe timeout: %s → %s", e, _cfg.failure_mode)
        except OSError as e:
            log.warning("Pipe error: %s → %s", e, _cfg.failure_mode)
        except Exception as e:
            log.error("Unexpected pipe error: %s → %s", e, _cfg.failure_mode)

    return ("ALLOW" if _cfg.fail_open() else "BLOCK"), False, "Pipe communication error"
