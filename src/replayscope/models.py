from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


class TraceStatus(StrEnum):
    RECORDING = "recording"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EventKind(StrEnum):
    ENVIRONMENT = "environment"
    MODEL_CALL = "model_call"
    TOOL_CALL = "tool_call"
    WORKSPACE_DELTA = "workspace_delta"
    CHECKPOINT = "checkpoint"
    RUN_FINISHED = "run_finished"


class ReplayMode(StrEnum):
    RECORDED = "recorded"
    FRESH_MODEL = "fresh_model"
    FRESH_ALL = "fresh_all"


class DivergenceKind(StrEnum):
    MISSING = "missing"
    UNEXPECTED = "unexpected"
    TYPE = "type"
    VALUE = "value"
    WORKSPACE = "workspace"
    ERROR = "error"


class ContentRef(BaseModel):
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    size: int = Field(ge=0)
    media_type: str = "application/octet-stream"


class TraceCreate(BaseModel):
    source_run_id: str = Field(min_length=1, max_length=300)
    framework: str = Field(default="generic", min_length=1, max_length=100)
    labels: dict[str, str] = Field(default_factory=dict)


class Trace(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    source_run_id: str
    framework: str
    labels: dict[str, str] = Field(default_factory=dict)
    status: TraceStatus = TraceStatus.RECORDING
    failure_signature: str | None = None
    event_count: int = 0
    chain_head: str = "0" * 64
    created_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None


class TraceEvent(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    trace_id: UUID
    sequence: int = Field(ge=0)
    kind: EventKind
    name: str = Field(min_length=1, max_length=300)
    input_ref: ContentRef | None = None
    output_ref: ContentRef | None = None
    error_ref: ContentRef | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    duration_ms: float | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    previous_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime = Field(default_factory=utcnow)


class FileEntry(BaseModel):
    path: str
    kind: str = Field(pattern=r"^(file|symlink)$")
    content: ContentRef
    mode: int = Field(ge=0)

    @model_validator(mode="after")
    def safe_path(self) -> FileEntry:
        if self.path.startswith("/") or ".." in self.path.split("/"):
            raise ValueError("workspace paths must be relative and cannot traverse")
        return self


class WorkspaceManifest(BaseModel):
    version: int = 1
    files: list[FileEntry] = Field(default_factory=list)


class WorkspaceDelta(BaseModel):
    added: list[FileEntry] = Field(default_factory=list)
    modified: list[FileEntry] = Field(default_factory=list)
    deleted: list[str] = Field(default_factory=list)


class Divergence(BaseModel):
    event_sequence: int
    kind: DivergenceKind
    path: str
    expected: Any = None
    actual: Any = None
    message: str


class ReplayResult(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    trace_id: UUID
    mode: ReplayMode
    status: str
    divergence_count: int
    divergences: list[Divergence] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime = Field(default_factory=utcnow)
