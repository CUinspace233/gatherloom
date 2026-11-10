"""Private attachment storage on the server's local volume."""

import os
from hashlib import sha256
from pathlib import Path
from typing import BinaryIO
from uuid import UUID, uuid4

STORAGE_DIR = Path(os.environ.get("GATHERLOOM_STORAGE_DIR", "var/storage"))
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
CHUNK = 1024 * 1024
IMAGE_SIGNATURES = (
    (b"\xff\xd8\xff", 0, "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", 0, "image/png"),
    (b"ftypheic", 4, "image/heic"),
    (b"ftypheix", 4, "image/heic"),
    (b"ftypmif1", 4, "image/heic"),
)


class TooLarge(Exception):
    pass


def sniff_image(head: bytes) -> str | None:
    for signature, offset, content_type in IMAGE_SIGNATURES:
        if head[offset : offset + len(signature)] == signature:
            return content_type
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def tmp_dir() -> Path:
    return STORAGE_DIR / "tmp"


def attachment_path(owner_id: UUID, attachment_id: UUID) -> Path:
    return STORAGE_DIR / "attachments" / str(owner_id) / str(attachment_id)


def receive(source: BinaryIO) -> tuple[Path, int, str, bytes]:
    """Copy an upload into a private temporary file.

    Returns the temporary path, size, SHA-256 hex digest and leading bytes.
    The caller moves the file into place or deletes it.
    """
    tmp_dir().mkdir(parents=True, exist_ok=True)
    path = tmp_dir() / str(uuid4())
    digest = sha256()
    size = 0
    head = b""
    try:
        with open(path, "xb") as target:
            os.chmod(path, 0o600)
            while chunk := source.read(CHUNK):
                size += len(chunk)
                if size > MAX_ATTACHMENT_BYTES:
                    raise TooLarge
                if len(head) < 16:
                    head = (head + chunk)[:16]
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path, size, digest.hexdigest(), head


def commit(tmp: Path, owner_id: UUID, attachment_id: UUID) -> None:
    final = attachment_path(owner_id, attachment_id)
    final.parent.mkdir(parents=True, exist_ok=True)
    os.replace(tmp, final)


def discard(owner_id: UUID, attachment_id: UUID) -> None:
    attachment_path(owner_id, attachment_id).unlink(missing_ok=True)
