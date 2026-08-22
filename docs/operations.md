# Operations

## Retention and recovery

Trace metadata cascades to replays, divergences, trials, and evaluation observations. CAS objects
are immutable and may be shared. Garbage collection must walk event references and nested workspace
manifests before deleting unreachable digests.

PostgreSQL and CAS should be snapshotted together. Restoring CAS from an earlier point can make newer
traces unverifiable, which appears as missing content rather than silent replay corruption.

## Sensitive data

Recorder redaction covers common sensitive key names recursively. Production integrations should
also use allowlists, encrypted volumes, access controls, and retention limits. Trace bundles contain
payload bytes and must be handled as production data.

## Troubleshooting

- `replayscope inspect TRACE_ID` verifies the event chain.
- `ContentCorruption` means bytes no longer match their digest or declared size.
- `inconclusive` reduction means the baseline was unstable or did not reproduce.
- A `$adapter` divergence means the required fresh model or tool adapter was not registered.
