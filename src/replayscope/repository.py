from __future__ import annotations

import json
from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import RLock
from typing import Any
from uuid import UUID

from replayscope.canonical import event_hash
from replayscope.models import (
    ContentRef,
    EventKind,
    Trace,
    TraceCreate,
    TraceEvent,
    TraceStatus,
)


class TraceNotFound(KeyError):
    pass


class TraceClosed(RuntimeError):
    pass


class TraceIntegrityError(RuntimeError):
    pass


class TraceRepository(ABC):
    @abstractmethod
    def create_trace(self, request: TraceCreate) -> Trace: ...

    @abstractmethod
    def register_blob(self, reference: ContentRef) -> None: ...

    @abstractmethod
    def append_event(
        self,
        trace_id: UUID,
        kind: EventKind,
        name: str,
        *,
        input_ref: ContentRef | None = None,
        output_ref: ContentRef | None = None,
        error_ref: ContentRef | None = None,
        metadata: dict[str, Any] | None = None,
        duration_ms: float | None = None,
        cost_usd: float | None = None,
    ) -> TraceEvent: ...

    @abstractmethod
    def finish_trace(
        self, trace_id: UUID, status: TraceStatus, failure_signature: str | None = None
    ) -> Trace: ...

    @abstractmethod
    def get_trace(self, trace_id: UUID) -> Trace: ...

    @abstractmethod
    def events(self, trace_id: UUID) -> list[TraceEvent]: ...

    @abstractmethod
    def set_workspace_refs(
        self,
        trace_id: UUID,
        *,
        initial: ContentRef | None = None,
        final: ContentRef | None = None,
        environment: ContentRef | None = None,
    ) -> None: ...


def _event_payload(
    sequence: int,
    kind: EventKind,
    name: str,
    input_ref: ContentRef | None,
    output_ref: ContentRef | None,
    error_ref: ContentRef | None,
    metadata: dict,
    duration_ms: float | None,
    cost_usd: float | None,
) -> dict:
    return {
        "sequence": sequence,
        "kind": kind.value,
        "name": name,
        "input_ref": input_ref.model_dump(mode="json") if input_ref else None,
        "output_ref": output_ref.model_dump(mode="json") if output_ref else None,
        "error_ref": error_ref.model_dump(mode="json") if error_ref else None,
        "metadata": metadata,
        "duration_ms": duration_ms,
        "cost_usd": cost_usd,
    }


@dataclass
class MemoryTraceRepository(TraceRepository):
    _traces: dict[UUID, Trace] = field(default_factory=dict)
    _events: dict[UUID, list[TraceEvent]] = field(default_factory=dict)
    _blobs: dict[str, ContentRef] = field(default_factory=dict)
    _refs: dict[UUID, dict[str, ContentRef]] = field(default_factory=dict)
    _lock: RLock = field(default_factory=RLock)

    def create_trace(self, request: TraceCreate) -> Trace:
        with self._lock:
            trace = Trace(**request.model_dump())
            self._traces[trace.id] = trace
            self._events[trace.id] = []
            self._refs[trace.id] = {}
            return trace.model_copy(deep=True)

    def register_blob(self, reference: ContentRef) -> None:
        with self._lock:
            current = self._blobs.get(reference.digest)
            if current and current.size != reference.size:
                raise TraceIntegrityError(reference.digest)
            self._blobs[reference.digest] = reference

    def append_event(self, trace_id: UUID, kind: EventKind, name: str, **values) -> TraceEvent:
        with self._lock:
            trace = self._require(trace_id)
            if trace.status is not TraceStatus.RECORDING:
                raise TraceClosed(str(trace_id))
            sequence = trace.event_count
            metadata = values.get("metadata") or {}
            payload = _event_payload(
                sequence,
                kind,
                name,
                values.get("input_ref"),
                values.get("output_ref"),
                values.get("error_ref"),
                metadata,
                values.get("duration_ms"),
                values.get("cost_usd"),
            )
            digest = event_hash(payload, trace.chain_head)
            event = TraceEvent(
                trace_id=trace_id,
                previous_hash=trace.chain_head,
                event_hash=digest,
                **payload,
            )
            self._events[trace_id].append(event)
            trace.event_count += 1
            trace.chain_head = digest
            return event.model_copy(deep=True)

    def finish_trace(
        self, trace_id: UUID, status: TraceStatus, failure_signature: str | None = None
    ) -> Trace:
        if status is TraceStatus.RECORDING:
            raise ValueError("terminal status required")
        with self._lock:
            trace = self._require(trace_id)
            if trace.status is not TraceStatus.RECORDING:
                raise TraceClosed(str(trace_id))
            trace.status = status
            trace.failure_signature = failure_signature
            trace.finished_at = datetime.now(UTC)
            return trace.model_copy(deep=True)

    def get_trace(self, trace_id: UUID) -> Trace:
        with self._lock:
            return self._require(trace_id).model_copy(deep=True)

    def events(self, trace_id: UUID) -> list[TraceEvent]:
        with self._lock:
            self._require(trace_id)
            return deepcopy(self._events[trace_id])

    def set_workspace_refs(self, trace_id: UUID, **refs) -> None:
        with self._lock:
            self._require(trace_id)
            self._refs[trace_id].update({key: value for key, value in refs.items() if value})

    def workspace_refs(self, trace_id: UUID) -> dict[str, ContentRef]:
        with self._lock:
            return deepcopy(self._refs[trace_id])

    def _require(self, trace_id: UUID) -> Trace:
        try:
            return self._traces[trace_id]
        except KeyError as exc:
            raise TraceNotFound(str(trace_id)) from exc


class PostgresTraceRepository(TraceRepository):
    def __init__(self, pool) -> None:
        self.pool = pool

    def create_trace(self, request: TraceCreate) -> Trace:
        trace = Trace(**request.model_dump())
        with self.pool.connection() as conn:
            conn.execute(
                """INSERT INTO traces (id, source_run_id, framework, labels)
                VALUES (%s, %s, %s, %s)""",
                (trace.id, trace.source_run_id, trace.framework, json.dumps(trace.labels)),
            )
        return trace

    def register_blob(self, reference: ContentRef) -> None:
        with self.pool.connection() as conn:
            row = conn.execute(
                """INSERT INTO blobs (digest, size, media_type) VALUES (%s, %s, %s)
                ON CONFLICT (digest) DO UPDATE SET digest = EXCLUDED.digest RETURNING size, media_type""",
                (reference.digest, reference.size, reference.media_type),
            ).fetchone()
            if row["size"] != reference.size:
                raise TraceIntegrityError(reference.digest)

    def append_event(self, trace_id: UUID, kind: EventKind, name: str, **values) -> TraceEvent:
        metadata = values.get("metadata") or {}
        with self.pool.connection() as conn, conn.transaction():
            trace = conn.execute(
                "SELECT * FROM traces WHERE id = %s FOR UPDATE", (trace_id,)
            ).fetchone()
            if trace is None:
                raise TraceNotFound(str(trace_id))
            if trace["status"] != "recording":
                raise TraceClosed(str(trace_id))
            payload = _event_payload(
                trace["event_count"],
                kind,
                name,
                values.get("input_ref"),
                values.get("output_ref"),
                values.get("error_ref"),
                metadata,
                values.get("duration_ms"),
                values.get("cost_usd"),
            )
            digest = event_hash(payload, trace["chain_head"])
            event = TraceEvent(
                trace_id=trace_id,
                previous_hash=trace["chain_head"],
                event_hash=digest,
                **payload,
            )
            conn.execute(
                """INSERT INTO trace_events
                (id, trace_id, sequence, kind, name, input_ref, output_ref, error_ref,
                 metadata, duration_ms, cost_usd, previous_hash, event_hash, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    event.id,
                    trace["id"],
                    event.sequence,
                    event.kind.value,
                    event.name,
                    _json_ref(event.input_ref),
                    _json_ref(event.output_ref),
                    _json_ref(event.error_ref),
                    json.dumps(event.metadata),
                    event.duration_ms,
                    event.cost_usd,
                    event.previous_hash,
                    event.event_hash,
                    event.created_at,
                ),
            )
            conn.execute(
                "UPDATE traces SET event_count = event_count + 1, chain_head = %s WHERE id = %s",
                (digest, trace_id),
            )
            return event

    def finish_trace(
        self, trace_id: UUID, status: TraceStatus, failure_signature: str | None = None
    ) -> Trace:
        if status is TraceStatus.RECORDING:
            raise ValueError("terminal status required")
        with self.pool.connection() as conn:
            row = conn.execute(
                """UPDATE traces SET status = %s, failure_signature = %s, finished_at = now()
                WHERE id = %s AND status = 'recording' RETURNING *""",
                (status.value, failure_signature, trace_id),
            ).fetchone()
            if row is None:
                raise TraceClosed(str(trace_id))
            return _trace(row)

    def get_trace(self, trace_id: UUID) -> Trace:
        with self.pool.connection() as conn:
            row = conn.execute("SELECT * FROM traces WHERE id = %s", (trace_id,)).fetchone()
            if row is None:
                raise TraceNotFound(str(trace_id))
            return _trace(row)

    def events(self, trace_id: UUID) -> list[TraceEvent]:
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM trace_events WHERE trace_id = %s ORDER BY sequence", (trace_id,)
            ).fetchall()
            return [_trace_event(row) for row in rows]

    def set_workspace_refs(self, trace_id: UUID, **refs) -> None:
        fields = {
            "initial": "initial_workspace_ref",
            "final": "final_workspace_ref",
            "environment": "environment_ref",
        }
        with self.pool.connection() as conn:
            for name, value in refs.items():
                if value:
                    conn.execute(
                        f"UPDATE traces SET {fields[name]} = %s WHERE id = %s",
                        (_json_ref(value), trace_id),
                    )


def _json_ref(reference: ContentRef | None) -> str | None:
    return json.dumps(reference.model_dump(mode="json")) if reference else None


def _ref(value) -> ContentRef | None:
    return ContentRef.model_validate(value) if value else None


def _trace(row: dict) -> Trace:
    keys = Trace.model_fields.keys()
    return Trace(**{key: row[key] for key in keys if key in row})


def _trace_event(row: dict) -> TraceEvent:
    values = {key: row[key] for key in TraceEvent.model_fields if key in row}
    for key in ("input_ref", "output_ref", "error_ref"):
        values[key] = _ref(values.get(key))
    return TraceEvent(**values)
