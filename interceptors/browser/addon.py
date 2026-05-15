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

from mitmproxy import http

import pipe_client
from config import Config, load_config

log = logging.getLogger(__name__)

_upload_lock = threading.Lock()
_cfg: Config = Config()

# Resumable upload tracking:
# Step 1 POST (metadata) → _pending_resumable[id(flow)] = filename
# Step 1 response (Location header) → _resumable_filenames[upload_id] = filename
# Step 2 PUT (file bytes) → look up upload_id in _resumable_filenames
_pending_resumable: dict = {}   # flow id → filename (awaiting upload_id from server)
_resumable_filenames: dict = {} # upload_id → filename
_blocked_url_cache: dict = {}           # url → expiry_time (monotonic float)
_blocked_url_cache_lock = threading.Lock()
_BLOCK_CACHE_TTL = 60.0                 # seconds
# KNOWN LIMITATION: Only single-chunk uploads are intercepted
# (Content-Range: bytes 0-N/N where N+1 equals total). Multi-chunk chunked
# uploads would require reassembling chunks across flows, which is not supported.


def load(loader):
    global _cfg
    _cfg = load_config(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml"))
    tmp = _cfg.resolved_temp_dir()
    try:
        os.makedirs(tmp, exist_ok=True)
    except OSError as e:
        log.error("Cannot create temp dir %s: %s", tmp, e)
    log.info(
        "DLP addon loaded | pipe=%s timeout=%ss fail=%s temp=%s extensions=%s keywords=%s",
        _cfg.pipe_name,
        _cfg.timeout_seconds,
        _cfg.fail_behavior,
        tmp,
        _cfg.extensions or "(all)",
        _cfg.upload_url_keywords,
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

    # Google Drive / API style: multipart/related — extract actual file bytes and name
    if "multipart/related" in content_type.lower():
        filename, file_body, file_mime = _parse_multipart_related(body, content_type)
        if not filename:
            log.warning("SKIP multipart/related: could not extract filename from %s", flow.request.pretty_url)
            return
    else:
        filename = _extract_filename(flow)
        file_body = body
        file_mime = content_type.split(";")[0].strip().lower()

    if not _matches_type_filter(filename, file_mime):
        log.debug("SKIP (type filter) | %s | %s", filename, file_mime)
        return

    try:
        temp_path = _write_temp_file(file_body, filename)
    except OSError as e:
        log.error("Failed to write temp file for '%s': %s → fail_%s", filename, e, _cfg.fail_behavior)
        if not _cfg.fail_open():
            flow.kill()
        return

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

    decision, consumer_received = _consult_policy(payload)

    # Temp file lifecycle: consumer is responsible for cleanup when it received the path.
    # If the consumer never received it (pipe error/timeout), we clean up ourselves.
    if not consumer_received:
        _delete_temp_file(temp_path)

    if decision == "BLOCK":
        _cache_blocked_url(flow.request.pretty_url)
        log.info("BLOCK | %s | %d bytes | %s", filename, len(file_body), flow.request.pretty_url)
        flow.response = http.Response.make(
            403,
            b"Upload blocked by DLP policy.",
            {"Content-Type": "text/plain"},
        )
    else:
        log.info("ALLOW | %s | %d bytes | %s", filename, len(file_body), flow.request.pretty_url)


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
    attachments = _parse_gmail_attachments(body, content_type)

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

        decision, consumer_received = _consult_policy(payload)

        if not consumer_received:
            _delete_temp_file(temp_path)

        if decision == "BLOCK":
            log.info("BLOCK | Gmail attachment | %s | %d bytes | %s",
                     filename, len(file_body), url[:80])
            _cache_blocked_url(url)
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

    if not _matches_type_filter(filename, file_mime):
        log.debug("SKIP (type filter) | Gmail resumable | %s | %s", filename, file_mime)
        return

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

    decision, consumer_received = _consult_policy(payload)

    if not consumer_received:
        _delete_temp_file(temp_path)

    if decision == "BLOCK":
        log.info("BLOCK | Gmail resumable upload | %s | %d bytes | %s",
                 filename, len(body), url[:80])
        _cache_blocked_url(url)
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


def _parse_gmail_attachments(body: bytes, content_type: str) -> list[tuple[str, bytes, str]]:
    """
    Parse a multipart/form-data Gmail compose body and extract all file attachments.
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
            part_body = chunk[header_end + len(sep):].rstrip(b"\r\n")

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
    Returns (decision: str, consumer_received: bool).
    consumer_received=True means the consumer got the temp_path and owns cleanup.
    """
    with _upload_lock:
        try:
            decision = pipe_client.send_and_receive(
                payload,
                _cfg.pipe_name,
                _cfg.timeout_seconds,
            )
            return decision, True
        except TimeoutError as e:
            log.warning("Pipe timeout: %s → fail_%s", e, _cfg.fail_behavior)
        except OSError as e:
            log.warning("Pipe error: %s → fail_%s", e, _cfg.fail_behavior)
        except Exception as e:
            log.error("Unexpected pipe error: %s → fail_%s", e, _cfg.fail_behavior)

    return ("ALLOW" if _cfg.fail_open() else "BLOCK"), False
