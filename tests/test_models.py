import pytest
from pydantic import ValidationError

from replayscope.canonical import canonical_json, event_hash, sha256_bytes
from replayscope.models import ContentRef, FileEntry


def test_canonical_json_is_order_independent() -> None:
    assert canonical_json({"b": 2, "a": 1}) == canonical_json({"a": 1, "b": 2})


def test_content_digest_is_validated() -> None:
    digest = sha256_bytes(b"hello")
    assert ContentRef(digest=digest, size=5).digest == digest
    with pytest.raises(ValidationError):
        ContentRef(digest="sha256:nope", size=0)


def test_file_entry_rejects_path_traversal() -> None:
    ref = ContentRef(digest=sha256_bytes(b"x"), size=1)
    with pytest.raises(ValidationError):
        FileEntry(path="../secret", kind="file", content=ref, mode=0o644)


def test_event_hash_chains_previous_event() -> None:
    first = event_hash({"sequence": 0}, "0" * 64)
    second = event_hash({"sequence": 1}, first)
    assert first != second
