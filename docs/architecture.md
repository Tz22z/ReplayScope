# Architecture and invariants

PostgreSQL owns identity, order, lifecycle, and searchable metadata. The content-addressed store owns
payload bytes. A `ContentRef` includes SHA-256, byte length, and media type.

CAS writes go to a temporary file in the destination directory, are flushed and fsynced, then use
atomic rename. Concurrent identical writes converge on the same path. Reads verify both SHA-256 and
declared size, so corruption becomes an explicit error before replay.

## Trace append invariant

Appending an event locks its trace row. The event sequence equals the current `event_count`, its
`previous_hash` equals `chain_head`, and its hash covers the canonical event payload plus the
previous hash. Therefore concurrent appenders cannot allocate the same sequence, insertion and
trace-head advancement commit atomically, and removal, reordering, or modification is detectable.

Blob writes happen before metadata append. A crash may leave an unreferenced CAS object but cannot
leave a committed event referencing bytes that were not durably written.

## Workspace capture

Manifests contain sorted relative paths, entry types, modes, and content references. Directories are
implicit. Deltas contain added, modified, and deleted entries. A recorded replay restores deltas;
fresh-all replay treats them as expected state and compares them with the adapter-mutated workspace.

## PostgreSQL tables

- `traces`, `trace_events`, and `blobs` store immutable observations.
- `replay_runs` and `divergences` store diagnoses.
- `reduction_jobs` and `reduction_trials` store ddmin provenance and caches.
- `evaluation_runs` and `evaluation_observations` retain repeat-level metrics and cost.

