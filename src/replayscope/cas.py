from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from replayscope.canonical import canonical_json, sha256_bytes
from replayscope.models import ContentRef


class ContentCorruption(IOError):
    pass


class BlobTooLarge(ValueError):
    pass


class LocalCAS:
    """Atomic filesystem content-addressed store with read-time verification."""

    def __init__(self, root: Path, max_blob_bytes: int = 64 * 1024 * 1024) -> None:
        self.root = root
        self.max_blob_bytes = max_blob_bytes
        self.root.mkdir(parents=True, exist_ok=True)

    def put_bytes(self, data: bytes, media_type: str = "application/octet-stream") -> ContentRef:
        if len(data) > self.max_blob_bytes:
            raise BlobTooLarge(f"blob is {len(data)} bytes; limit is {self.max_blob_bytes}")
        digest = sha256_bytes(data)
        target = self._path(digest)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".write-", dir=target.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return ContentRef(digest=digest, size=len(data), media_type=media_type)

    def put_json(self, value: Any) -> ContentRef:
        return self.put_bytes(canonical_json(value), "application/json")

    def get_bytes(self, reference: ContentRef | str) -> bytes:
        digest = reference.digest if isinstance(reference, ContentRef) else reference
        data = self._path(digest).read_bytes()
        if sha256_bytes(data) != digest:
            raise ContentCorruption(digest)
        if isinstance(reference, ContentRef) and len(data) != reference.size:
            raise ContentCorruption(f"size mismatch for {digest}")
        return data

    def get_json(self, reference: ContentRef | str) -> Any:
        return json.loads(self.get_bytes(reference))

    def has(self, digest: str) -> bool:
        return self._path(digest).is_file()

    def _path(self, digest: str) -> Path:
        algorithm, hex_digest = digest.split(":", 1)
        if algorithm != "sha256" or len(hex_digest) != 64:
            raise ValueError("unsupported digest")
        return self.root / algorithm / hex_digest[:2] / hex_digest[2:]
