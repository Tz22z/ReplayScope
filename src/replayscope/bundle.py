from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from typing import Any
from uuid import UUID

from replayscope.canonical import canonical_json, sha256_bytes
from replayscope.cas import LocalCAS
from replayscope.models import ContentRef, Trace, TraceEvent
from replayscope.reduction import ReductionResult
from replayscope.repository import TraceIntegrityError, TraceRepository


def export_trace(
    trace_id: UUID, repository: TraceRepository, cas: LocalCAS, destination: Path
) -> None:
    trace = repository.get_trace(trace_id)
    events = repository.events(trace_id)
    references = _references(events, cas)
    bundle = {
        "format": "replayscope.bundle.v1",
        "trace": trace.model_dump(mode="json"),
        "events": [event.model_dump(mode="json") for event in events],
        "blobs": [reference.model_dump(mode="json") for reference in references.values()],
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(destination, "w:gz") as archive:
        _add(archive, "manifest.json", canonical_json(bundle))
        for digest, reference in sorted(references.items()):
            _add(archive, f"blobs/{digest.removeprefix('sha256:')}", cas.get_bytes(reference))


def inspect_bundle(source: Path) -> tuple[Trace, list[TraceEvent], dict[str, bytes]]:
    with tarfile.open(source, "r:gz") as archive:
        names = archive.getnames()
        if "manifest.json" not in names or any(
            name.startswith("/") or ".." in Path(name).parts for name in names
        ):
            raise TraceIntegrityError("unsafe or incomplete bundle")
        manifest_stream = archive.extractfile("manifest.json")
        if manifest_stream is None:
            raise TraceIntegrityError("missing bundle manifest")
        manifest = json.load(manifest_stream)
        if manifest.get("format") != "replayscope.bundle.v1":
            raise TraceIntegrityError("unsupported bundle format")
        blobs: dict[str, bytes] = {}
        for declared in manifest["blobs"]:
            reference = ContentRef.model_validate(declared)
            member = f"blobs/{reference.digest.removeprefix('sha256:')}"
            stream = archive.extractfile(member)
            if stream is None:
                raise TraceIntegrityError(f"missing {reference.digest}")
            data = stream.read()
            if sha256_bytes(data) != reference.digest or len(data) != reference.size:
                raise TraceIntegrityError(f"corrupt {reference.digest}")
            blobs[reference.digest] = data
    return (
        Trace.model_validate(manifest["trace"]),
        [TraceEvent.model_validate(event) for event in manifest["events"]],
        blobs,
    )


def export_reduction(result: ReductionResult, cas: LocalCAS, destination: Path) -> None:
    if result.status != "succeeded" or result.package is None:
        raise ValueError("only successful reductions can be exported")
    references = {entry.content.digest: entry.content for entry in result.package.files}
    manifest = {
        "format": "replayscope.reduction.v1",
        "signature": result.signature,
        "original_size": result.original_size,
        "minimal_size": result.minimal_size,
        "package": result.package.model_dump(mode="json"),
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(destination, "w:gz") as archive:
        _add(archive, "reproduction.json", canonical_json(manifest))
        for digest, reference in sorted(references.items()):
            _add(archive, f"blobs/{digest.removeprefix('sha256:')}", cas.get_bytes(reference))


def _references(events: list[TraceEvent], cas: LocalCAS) -> dict[str, ContentRef]:
    references = {}
    for event in events:
        for reference in (event.input_ref, event.output_ref, event.error_ref):
            if reference:
                references[reference.digest] = reference
    pending = list(references.values())
    while pending:
        reference = pending.pop()
        if reference.media_type != "application/json":
            continue
        for nested in _nested_refs(cas.get_json(reference)):
            if nested.digest not in references:
                references[nested.digest] = nested
                pending.append(nested)
    return references


def _nested_refs(value: Any) -> list[ContentRef]:
    found = []
    if isinstance(value, dict):
        if {"digest", "size"}.issubset(value):
            try:
                found.append(ContentRef.model_validate(value))
            except ValueError:
                pass
        for item in value.values():
            found.extend(_nested_refs(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_nested_refs(item))
    return found


def _add(archive: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o644
    info.mtime = 0
    archive.addfile(info, io.BytesIO(data))
