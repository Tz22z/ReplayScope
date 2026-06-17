from __future__ import annotations

import os
import stat
from pathlib import Path, PurePosixPath

from replayscope.cas import LocalCAS
from replayscope.models import ContentRef, FileEntry, WorkspaceDelta, WorkspaceManifest


class UnsafeWorkspacePath(ValueError):
    pass


def capture_workspace(root: Path, cas: LocalCAS) -> WorkspaceManifest:
    root = root.resolve()
    entries: list[FileEntry] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if any(part in {".git", ".replayscope"} for part in PurePosixPath(relative).parts):
            continue
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if path.is_symlink():
            target = os.readlink(path)
            reference = cas.put_bytes(target.encode(), "application/x-symlink")
            entries.append(FileEntry(path=relative, kind="symlink", content=reference, mode=mode))
        elif path.is_file():
            entries.append(
                FileEntry(
                    path=relative, kind="file", content=cas.put_bytes(path.read_bytes()), mode=mode
                )
            )
    return WorkspaceManifest(files=entries)


def diff_manifests(before: WorkspaceManifest, after: WorkspaceManifest) -> WorkspaceDelta:
    old = {entry.path: entry for entry in before.files}
    new = {entry.path: entry for entry in after.files}
    added = [new[path] for path in sorted(new.keys() - old.keys())]
    deleted = sorted(old.keys() - new.keys())
    modified = [
        new[path]
        for path in sorted(old.keys() & new.keys())
        if old[path].content.digest != new[path].content.digest
        or old[path].mode != new[path].mode
        or old[path].kind != new[path].kind
    ]
    return WorkspaceDelta(added=added, modified=modified, deleted=deleted)


def restore_workspace(manifest: WorkspaceManifest, target: Path, cas: LocalCAS) -> None:
    target.mkdir(parents=True, exist_ok=True)
    if any(target.iterdir()):
        raise FileExistsError("restore target must be empty")
    root = target.resolve()
    for entry in manifest.files:
        destination = _safe_destination(root, entry.path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = cas.get_bytes(entry.content)
        if entry.kind == "file":
            destination.write_bytes(data)
            destination.chmod(entry.mode)
        else:
            link_target = data.decode()
            if Path(link_target).is_absolute():
                raise UnsafeWorkspacePath(entry.path)
            resolved = (destination.parent / link_target).resolve()
            if not resolved.is_relative_to(root):
                raise UnsafeWorkspacePath(entry.path)
            destination.symlink_to(link_target)


def apply_delta(delta: WorkspaceDelta, root: Path, cas: LocalCAS) -> None:
    root = root.resolve()
    for relative in delta.deleted:
        destination = _safe_destination(root, relative)
        if destination.is_symlink() or destination.is_file():
            destination.unlink()
    for entry in [*delta.added, *delta.modified]:
        destination = _safe_destination(root, entry.path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink() or destination.exists():
            destination.unlink()
        data = cas.get_bytes(entry.content)
        if entry.kind == "file":
            destination.write_bytes(data)
            destination.chmod(entry.mode)
        else:
            link_target = data.decode()
            if Path(link_target).is_absolute():
                raise UnsafeWorkspacePath(entry.path)
            if not (destination.parent / link_target).resolve().is_relative_to(root):
                raise UnsafeWorkspacePath(entry.path)
            destination.symlink_to(link_target)


def store_manifest(manifest: WorkspaceManifest, cas: LocalCAS) -> ContentRef:
    return cas.put_json(manifest.model_dump(mode="json"))


def load_manifest(reference: ContentRef, cas: LocalCAS) -> WorkspaceManifest:
    return WorkspaceManifest.model_validate(cas.get_json(reference))


def _safe_destination(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts:
        raise UnsafeWorkspacePath(relative)
    destination = root.joinpath(*path.parts)
    if not destination.parent.resolve().is_relative_to(root):
        raise UnsafeWorkspacePath(relative)
    return destination
