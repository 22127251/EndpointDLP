"""Unit tests for the browser addon's pure helpers (interceptors/browser/addon.py).

Covers the two browser-interceptor fixes:
  * ODF type detection via the uncompressed ``mimetype`` zip entry so .ods/.odp
    are no longer collapsed to .odt (which mislabels the audit log AND routes the
    orchestrator to the wrong extractor — a potential under-extraction/false ALLOW);
  * the now-hardcoded upload-filter allow-list (incl. Markdown), and that
    load_config IGNORES any stale upload-filter keys left in config.yaml.

The addon is imported directly by putting interceptors/browser on sys.path; it
imports mitmproxy + its sibling modules (config/pipe_client/ctl_pipe_subscriber),
all available in the dev/runtime venv.
"""
from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

_BROWSER_DIR = Path(__file__).resolve().parents[2] / "interceptors" / "browser"
sys.path.insert(0, str(_BROWSER_DIR))

import addon          # noqa: E402
import config         # noqa: E402


# --- ODF / OOXML type detection (B1) ---------------------------------------

def _odf_zip(mimetype: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", mimetype)        # the canonical ODF type signal
        z.writestr("content.xml", "<x/>")
        z.writestr("META-INF/manifest.xml", "<m/>")
    return buf.getvalue()


def _ooxml_zip(folder: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<t/>")
        z.writestr(f"{folder}/document.xml", "<d/>")
        z.writestr("docProps/core.xml", "<c/>")
    return buf.getvalue()


@pytest.mark.parametrize("mimetype,ext", [
    ("application/vnd.oasis.opendocument.text", ".odt"),
    ("application/vnd.oasis.opendocument.spreadsheet", ".ods"),
    ("application/vnd.oasis.opendocument.presentation", ".odp"),
])
def test_odf_detected_by_mimetype(mimetype, ext):
    assert addon._detect_office_type_from_zip(_odf_zip(mimetype)) == ext


@pytest.mark.parametrize("folder,ext", [
    ("word", ".docx"),
    ("xl", ".xlsx"),
    ("ppt", ".pptx"),
])
def test_ooxml_detected_by_folder(folder, ext):
    assert addon._detect_office_type_from_zip(_ooxml_zip(folder)) == ext


def test_odf_without_mimetype_falls_back_to_odt():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("content.xml", "<x/>")
        z.writestr("META-INF/manifest.xml", "<m/>")
    assert addon._detect_office_type_from_zip(buf.getvalue()) == ".odt"


def test_plain_zip_is_generic():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("readme.txt", "hello")
    assert addon._detect_office_type_from_zip(buf.getvalue()) == ".zip"


def test_corrupt_bytes_are_generic_zip():
    assert addon._detect_office_type_from_zip(b"not a zip at all") == ".zip"


# --- generic multipart/form-data file-part extraction (Zalo fix) -----------

def _multipart_form(boundary: str, filename: str, file_bytes: bytes,
                    mime: str = "application/octet-stream") -> bytes:
    """Build a browser-style multipart/form-data body with a single file part
    (matches the captured Zalo upload shape)."""
    return (
        f"--{boundary}\r\n".encode()
        + f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode()
        + f"Content-Type: {mime}\r\n\r\n".encode()
        + file_bytes + b"\r\n"
        + f"--{boundary}--\r\n".encode()
    )


def test_multipart_form_extracts_real_file_not_envelope():
    # The Zalo bug: the addon wrote the WHOLE envelope as the file, so a CSV's
    # rows collapsed to one column → no PII → false ALLOW. The parser must return
    # the actual file bytes (no boundary/headers).
    boundary = "----WebKitFormBoundarybGIOIDNvGHUy4dB4"
    csv = b"\xef\xbb\xbfHo,So CCCD\r\nNguyen,012301234567\r\n"   # BOM + header + data
    body = _multipart_form(boundary, "csv_b1.csv", csv)
    ct = f"multipart/form-data; boundary={boundary}"

    files = addon._parse_multipart_form_files(body, ct)
    assert len(files) == 1
    fn, fb, fm = files[0]
    assert fn == "csv_b1.csv"
    assert b"WebKitFormBoundary" not in fb          # envelope framing stripped
    assert b"012301234567" in fb                    # real PII content preserved
    assert len(fb) < len(body)                      # not the whole envelope


def test_multipart_form_multiple_file_parts():
    boundary = "X"
    p1 = _multipart_form(boundary, "a.csv", b"colA\r\n111\r\n")
    # second file part appended (strip the closing delimiter of p1 first)
    p1_open = p1[: -len(f"--{boundary}--\r\n".encode())]
    body = (p1_open
            + f"--{boundary}\r\n".encode()
            + b'Content-Disposition: form-data; name="file2"; filename="b.txt"\r\n'
            + b"Content-Type: text/plain\r\n\r\n"
            + b"hello\r\n"
            + f"--{boundary}--\r\n".encode())
    files = addon._parse_multipart_form_files(body, f"multipart/form-data; boundary={boundary}")
    names = sorted(f[0] for f in files)
    assert names == ["a.csv", "b.txt"]


# --- chunked multipart/form-data reassembly (Zalo) -------------------------

class _FakeReq:
    def __init__(self, url, query):
        self.pretty_url = url
        self.query = query


class _FakeFlow:
    def __init__(self, url, query):
        self.request = _FakeReq(url, query)
        self.response = None


@pytest.fixture
def captured_scans(monkeypatch):
    """Replace _scan_file_and_apply with a recorder (returns ALLOW) and clear the
    chunk-session buffer so each test starts clean."""
    calls = []

    def _fake(flow, filename, file_body, file_mime, url):
        calls.append((filename, bytes(file_body), file_mime))
        return False   # ALLOW

    addon._chunk_sessions.clear()
    monkeypatch.setattr(addon, "_scan_file_and_apply", _fake)
    return calls


_URL = "https://file-stal-1.dlfl.vn/upc/TOKEN?filesize=250&offset=0"


def _chunk(flow_url, files, filesize, offset):
    flow = _FakeFlow(flow_url, {"filesize": str(filesize), "offset": str(offset)})
    addon._handle_chunked_upload(flow, files, flow.request.query, flow_url)
    return flow


def test_chunked_upload_scans_only_complete_file(captured_scans):
    base = "https://file-stal-1.dlfl.vn/upc/TOKEN"
    a = [("f.csv", b"A" * 100, "application/octet-stream")]
    b = [("f.csv", b"B" * 100, "application/octet-stream")]
    c = [("f.csv", b"C" * 50, "application/octet-stream")]

    f0 = _chunk(base + "?filesize=250&offset=0", a, 250, 0)
    f1 = _chunk(base + "?filesize=250&offset=100", b, 250, 100)
    assert captured_scans == []          # intermediate chunks NOT scanned
    assert f0.response is None and f1.response is None   # and passed through

    f2 = _chunk(base + "?filesize=250&offset=200", c, 250, 200)
    # complete → scanned exactly once with the reassembled bytes IN OFFSET ORDER
    assert len(captured_scans) == 1
    fn, full, _ = captured_scans[0]
    assert fn == "f.csv"
    assert full == b"A" * 100 + b"B" * 100 + b"C" * 50


def test_single_chunk_scans_immediately(captured_scans):
    base = "https://file-stal-2.dlfl.vn/upc/TOK2"
    files = [("x.csv", b"hello", "application/octet-stream")]
    _chunk(base + "?filesize=5&offset=0", files, 5, 0)
    assert len(captured_scans) == 1
    assert captured_scans[0][1] == b"hello"


def test_chunked_oversize_fails_closed_without_scan(captured_scans):
    base = "https://file-stal-3.dlfl.vn/upc/TOK3"
    huge = addon._MAX_CHUNK_REASSEMBLY_BYTES + 1
    files = [("big.csv", b"x" * 10, "application/octet-stream")]
    flow = _chunk(base + f"?filesize={huge}&offset=0", files, huge, 0)
    assert captured_scans == []          # never scanned
    assert flow.response is not None     # fail-closed 403 (default fail_closed)


def test_retried_chunk_offset_is_deduped(captured_scans):
    base = "https://file-stal-4.dlfl.vn/upc/TOK4"
    a = [("f.csv", b"A" * 60, "application/octet-stream")]
    _chunk(base + "?filesize=120&offset=0", a, 120, 0)
    _chunk(base + "?filesize=120&offset=0", a, 120, 0)   # retry same offset
    assert captured_scans == []                          # still incomplete (60/120)
    b = [("f.csv", b"B" * 60, "application/octet-stream")]
    _chunk(base + "?filesize=120&offset=60", b, 120, 60)
    assert len(captured_scans) == 1
    assert captured_scans[0][1] == b"A" * 60 + b"B" * 60


def test_chunked_reassembly_byte_exact_through_parser(captured_scans):
    # File with internal AND trailing newlines — the multipart over-strip hazard.
    # Each chunk goes through the REAL _parse_multipart_form_files, so this fails
    # if part-body stripping eats the file's own trailing \n (breaks completion).
    original = b"col1,col2\r\n0123,4567\r\nlast,row\n"
    cut = len(b"col1,col2\r\n")           # boundary lands right after a newline
    c0, c1 = original[:cut], original[cut:]
    boundary = "----WebKitFormBoundaryZ"
    base = "https://file-stal-9.dlfl.vn/upc/TOK9"

    def feed(chunk_bytes, offset):
        body = _multipart_form(boundary, "f.csv", chunk_bytes)
        files = addon._parse_multipart_form_files(body, f"multipart/form-data; boundary={boundary}")
        flow = _FakeFlow(f"{base}?filesize={len(original)}&offset={offset}",
                         {"filesize": str(len(original)), "offset": str(offset)})
        addon._handle_chunked_upload(flow, files, flow.request.query, flow.request.pretty_url)

    feed(c0, 0)
    feed(c1, len(c0))
    assert len(captured_scans) == 1
    assert captured_scans[0][1] == original     # byte-exact: no lost newlines


def test_multipart_extraction_preserves_trailing_newline():
    boundary = "B"
    f = b"a,b\nc,d\n"                     # file legitimately ends in \n
    body = _multipart_form(boundary, "x.csv", f)
    files = addon._parse_multipart_form_files(body, f"multipart/form-data; boundary={boundary}")
    assert files[0][1] == f              # the \n is content, not framing


# --- hardcoded upload-filter allow-list (A1 / B2) --------------------------

def test_config_hardcodes_markdown_and_office_types():
    c = config.Config()
    assert ".md" in c.extensions               # B2: Markdown now inspected
    assert "text/markdown" in c.mime_types
    for ext in (".odt", ".ods", ".odp", ".csv", ".docx", ".xlsx", ".pdf", ".txt"):
        assert ext in c.extensions
    assert c.has_type_filter()


def test_load_config_ignores_stale_yaml_filter_keys(tmp_path):
    """The 6 upload-filter fields are hardcoded; load_config must read ONLY
    pipe_timeout_ms + failure_mode and ignore any stale filter keys in yaml."""
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({
        "data_pipe": r"\\.\pipe\x",
        "browser": {
            "pipe_timeout_ms": 9000,
            "failure_mode": "fail_open",
            # stale keys that must be IGNORED now:
            "extensions": [".foo"],
            "mime_types": ["application/x-foo"],
            "temp_dir": r"D:\should\be\ignored",
            "min_upload_size_bytes": 999999,
            "domain_blocklist": ["evil.example"],
            "upload_url_keywords": ["zzz"],
        },
    }), encoding="utf-8")

    c = config.load_config(str(p))
    # admin-tunable fields honored
    assert c.timeout_seconds == 9.0
    assert c.failure_mode == "fail_open"
    # hardcoded filters win over the stale yaml keys
    assert ".foo" not in c.extensions and ".md" in c.extensions
    assert "application/x-foo" not in c.mime_types
    assert c.temp_dir == "" and c.resolved_temp_dir()
    assert c.min_upload_size_bytes == config._MIN_UPLOAD_SIZE_BYTES
    assert c.domain_blocklist == config._DOMAIN_BLOCKLIST
    assert c.upload_url_keywords == config._UPLOAD_URL_KEYWORDS
