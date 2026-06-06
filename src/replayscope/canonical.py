from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def event_hash(payload: dict[str, Any], previous_hash: str) -> str:
    return hashlib.sha256(bytes.fromhex(previous_hash) + canonical_json(payload)).hexdigest()
