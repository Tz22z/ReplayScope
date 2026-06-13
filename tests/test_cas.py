from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from replayscope.cas import BlobTooLarge, ContentCorruption, LocalCAS


def test_identical_content_is_stored_once(tmp_path: Path) -> None:
    cas = LocalCAS(tmp_path / "cas")
    first = cas.put_json({"answer": 42})
    second = cas.put_json({"answer": 42})
    assert first == second
    assert len(list((tmp_path / "cas").rglob("*"))) == 3


def test_corruption_is_detected_on_read(tmp_path: Path) -> None:
    cas = LocalCAS(tmp_path / "cas")
    reference = cas.put_bytes(b"original")
    cas._path(reference.digest).write_bytes(b"changed")
    with pytest.raises(ContentCorruption):
        cas.get_bytes(reference)


def test_concurrent_writers_converge_on_one_blob(tmp_path: Path) -> None:
    cas = LocalCAS(tmp_path / "cas")
    with ThreadPoolExecutor(max_workers=16) as pool:
        references = list(pool.map(lambda _: cas.put_bytes(b"same bytes"), range(100)))

    assert len({reference.digest for reference in references}) == 1
    assert cas.get_bytes(references[0]) == b"same bytes"


def test_blob_size_limit_is_enforced(tmp_path: Path) -> None:
    cas = LocalCAS(tmp_path / "cas", max_blob_bytes=3)
    with pytest.raises(BlobTooLarge):
        cas.put_bytes(b"four")
