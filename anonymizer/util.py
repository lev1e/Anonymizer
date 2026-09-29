from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
from pathlib import Path


# Working copies Office and editors leave behind. They duplicate content that is already
# covered by the real file, so they are skipped — and reported, never silently dropped.
SKIPPED_PREFIXES = ("~$", ".~lock.")
SKIPPED_SUFFIXES = (".tmp", ".bak", ".swp", ".part", ".crdownload")


def opaque_id(nbytes: int = 10) -> str:
    import base64
    return base64.b32encode(secrets.token_bytes(nbytes)).decode("ascii").rstrip("=")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".anon-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def safe_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".anon-copy-", dir=dst.parent)
    os.close(fd)
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def sniff_encoding(raw: bytes) -> str:
    """Единственная точка определения кодировки: и чтение, и обратная запись берут её отсюда."""
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    for encoding in ("utf-8", "cp1251"):
        try:
            raw.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            pass
    return "utf-8"


def decode_text(raw: bytes) -> str:
    return raw.decode(sniff_encoding(raw), errors="replace")


def finding_id(*parts: object) -> str:
    """Стабильный идентификатор находки: одни и те же координаты дают тот же id на любом проходе."""
    payload = "\0".join(str(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24].upper()


def json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
