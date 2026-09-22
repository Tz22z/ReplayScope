# ReplayScope formal validation

Deterministic, ground-truth-injected validation; no external model calls.

| Experiment | Result |
| --- | --- |
| Replay drift detection | 240 traces; 15 injected; precision 100.0%; recall 100.0% |
| Failure reduction | 26/30 stable faults reproduced; 83.33% mean reduction; 0 false results |
| Regression aggregation | 100 cases x 5 repeats; 82.00% solve rate; 2.28-point stddev |

## Integrity

- Source tree SHA-256: `52190c675ae39eef99eb45d5eab99b9cd1487a79d534c9dcfb7c2c2456707f96`
- Runner SHA-256: `4c65e90385b98e715f06cdbaac3b858b250cc586849addb0cae5d720a91f92d6`
- All assertions passed: `True`

## Limitations

- The suite validates replay, reduction, and aggregation mechanics with injected ground truth.
- It does not estimate natural production drift or live-model quality.
- Timing is local-machine diagnostic data, not a service-level objective.
