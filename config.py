import os
import tempfile
from dataclasses import dataclass, field
from typing import List

import yaml

_DEFAULT_UPLOAD_KEYWORDS = ["upload", "attach", "import"]


@dataclass
class Config:
    pipe_name: str = r"\\.\pipe\dlp_upload"
    timeout_seconds: float = 5.0
    fail_behavior: str = "block"
    temp_dir: str = ""
    min_upload_size_bytes: int = 1024
    extensions: List[str] = field(default_factory=list)
    mime_types: List[str] = field(default_factory=list)
    domain_blocklist: List[str] = field(default_factory=list)
    upload_url_keywords: List[str] = field(default_factory=lambda: list(_DEFAULT_UPLOAD_KEYWORDS))
    
    # Chunking configuration
    chunk_size_words: int = 500
    chunk_overlap_words: int = 50

    def resolved_temp_dir(self) -> str:
        return self.temp_dir if self.temp_dir else tempfile.gettempdir()

    def fail_open(self) -> bool:
        return self.fail_behavior.lower() == "allow"

    def has_type_filter(self) -> bool:
        return bool(self.extensions or self.mime_types)


def load_config(path: str = "config.yaml") -> Config:
    if not os.path.exists(path):
        return Config()
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    raw_ext = data.get("extensions") or []
    extensions = [
        e.lower() if e.startswith(".") else f".{e.lower()}"
        for e in raw_ext
    ]
    mime_types = [m.lower() for m in (data.get("mime_types") or [])]
    domain_blocklist = [d.lower() for d in (data.get("domain_blocklist") or [])]
    upload_url_keywords = [k.lower() for k in (data.get("upload_url_keywords") or _DEFAULT_UPLOAD_KEYWORDS)]

    return Config(
        pipe_name=data.get("pipe_name", r"\\.\pipe\dlp_upload"),
        timeout_seconds=float(data.get("timeout_seconds", 5.0)),
        fail_behavior=data.get("fail_behavior", "block"),
        temp_dir=data.get("temp_dir", ""),
        min_upload_size_bytes=int(data.get("min_upload_size_bytes", 1024)),
        extensions=extensions,
        mime_types=mime_types,
        domain_blocklist=domain_blocklist,
        upload_url_keywords=upload_url_keywords,
        chunk_size_words=int(data.get("chunk_size_words", 500)),
        chunk_overlap_words=int(data.get("chunk_overlap_words", 50)),
    )
