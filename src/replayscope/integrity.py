from __future__ import annotations

from replayscope.canonical import event_hash
from replayscope.models import Trace, TraceEvent
from replayscope.repository import TraceIntegrityError, _event_payload


def verify_event_chain(trace: Trace, events: list[TraceEvent]) -> None:
    previous = "0" * 64
    for expected_sequence, event in enumerate(events):
        if event.sequence != expected_sequence:
            raise TraceIntegrityError(
                f"expected sequence {expected_sequence}, got {event.sequence}"
            )
        if event.previous_hash != previous:
            raise TraceIntegrityError(f"broken previous hash at event {event.sequence}")
        payload = _event_payload(
            event.sequence,
            event.kind,
            event.name,
            event.input_ref,
            event.output_ref,
            event.error_ref,
            event.metadata,
            event.duration_ms,
            event.cost_usd,
        )
        calculated = event_hash(payload, previous)
        if calculated != event.event_hash:
            raise TraceIntegrityError(f"invalid hash at event {event.sequence}")
        previous = calculated
    if trace.event_count != len(events) or trace.chain_head != previous:
        raise TraceIntegrityError("trace head does not match event chain")
