# Benchmarks — the performance gate

Required, repository-level performance gates. Each benchmark measures one
real user path's wall-clock and asserts the median of 5 samples stays under
a calibrated budget. A red bench blocks the PR — that is what makes it a
gate rather than a decoration.

## What is measured (one directory entry per user path, not per package)

| Benchmark | User path | Endpoint |
|---|---|---|
| `test_sidecar_boot.py` | First-message latency | spawn → `__SIDECAR_READY__` marker |
| `test_coreloop_round.py` | Per-turn framework tax | one round over a 50-turn transcript, instant provider |
| `test_compaction.py` | Long-session stall | pressure compaction firing mid-run |

## Budget discipline

- Budgets are **reviewed source constants** (`REFERENCE_MS` per file) —
  environment variables must not override them.
- The reference machine is an arm64 Mac. CI budgets derive via
  `calibration.py`: `ceil(REFERENCE_MS × CI_TIME_SCALE × HEADROOM)`.
- `CI_TIME_SCALE` is the measured CI-runner/reference ratio (initially 2.0,
  following dsh). Recalibrate from a CI run's reported medians: if every
  benchmark's CI median lands at roughly `k × REFERENCE_MS`, set
  `CI_TIME_SCALE = k` and record the evidence in the PR.
- On failure the assertion prints the full sample distribution
  (min/median/max) so the verdict is explainable from the log alone.

## Running

```sh
uv run pytest benchmarks/ -q
```

The lane is deliberately NOT in `testpaths`: benchmarks run as their own CI
job (`bench` in `ci.yml`, pull_request only) on an otherwise idle runner —
wall-clock budgets are meaningless under a concurrent gate aggregate.

## Adding a benchmark

1. Pick a user path, not a function. If you cannot name the user-visible
   stall it prevents, do not add it.
2. Providers and externals are instant fakes — the measurement must isolate
   framework overhead, never network.
3. Fresh process/state per sample; bound every child, await exit, reap.
4. Measure locally (median of 5+), set `REFERENCE_MS` near the measurement
   with headroom for legitimate growth, and record the measurement in the
   PR description.
