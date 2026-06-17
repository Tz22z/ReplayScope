from pathlib import Path

import pytest

from replayscope.cas import LocalCAS
from replayscope.models import FileEntry, WorkspaceManifest
from replayscope.workspace import (
    UnsafeWorkspacePath,
    capture_workspace,
    diff_manifests,
    restore_workspace,
)


def test_capture_diff_and_restore(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("before")
    (source / "script.sh").write_text("echo hi")
    (source / "script.sh").chmod(0o755)
    cas = LocalCAS(tmp_path / "cas")
    before = capture_workspace(source, cas)
    (source / "a.txt").write_text("after")
    (source / "new.txt").write_text("new")
    (source / "script.sh").unlink()
    after = capture_workspace(source, cas)
    delta = diff_manifests(before, after)
    assert [entry.path for entry in delta.added] == ["new.txt"]
    assert [entry.path for entry in delta.modified] == ["a.txt"]
    assert delta.deleted == ["script.sh"]
    restored = tmp_path / "restored"
    restore_workspace(before, restored, cas)
    assert (restored / "a.txt").read_text() == "before"
    assert (restored / "script.sh").stat().st_mode & 0o777 == 0o755


def test_restore_rejects_escaping_symlink(tmp_path: Path) -> None:
    cas = LocalCAS(tmp_path / "cas")
    reference = cas.put_bytes(b"../../outside", "application/x-symlink")
    manifest = WorkspaceManifest(
        files=[FileEntry(path="dir/link", kind="symlink", content=reference, mode=0o777)]
    )
    with pytest.raises(UnsafeWorkspacePath):
        restore_workspace(manifest, tmp_path / "restore", cas)
