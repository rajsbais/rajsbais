# Benchmarks — protocol and current (simulated) numbers

No throughput or downtime guarantee is made. Numbers below are from the synthetic, in-memory, single-process
run on the development container and exist only to validate the measurement plumbing.

| Metric | Where measured | Scale 1 (synthetic) |
|---|---|---|
| Extraction records/s | `run_stages.metrics.records_per_second` (EXTRACT) | ≈ 20,000–23,000 |
| Transform duration | TRANSFORM stage | ≈ 0.13 s for 1,793 records |
| Load duration | LOAD stage | ≈ 0.11 s |
| Reconciliation duration | RECONCILE stage (233 checks) | ≈ 0.09 s |
| End-to-end vertical slice (generate + discover + graph + scope + run) | CLI `demo` | ≈ 3–4 s |
| Scale 3 (≈33k rows, 1,596 objects, 4,702 records): extract 1.26 s @ 6.5k rec/s, transform 0.65 s, load 0.92 s, reconcile 0.38 s | `tests/test_scale.py` | 7.5 s total |

## Protocol for real measurements (required before Phase 6 claims)
1. Source impact: CPU, IO, work-process utilisation on the SAP source during extraction at package sizes 10k/50k/200k.
2. Throughput per adapter and table class (cluster vs transparent, wide vs narrow rows), with 1/4/8/16 workers.
3. Delta lag: p50/p95 time from source commit to target apply under a defined transaction mix.
4. Backlog drain rate after a 1-hour freeze; final delta duration.
5. Recovery: time to resume after worker loss at 25/50/75% progress.
6. Window fit: Migration Performance Agent output vs actual rehearsal duration.
Report template: `docs/benchmarks.md` sections per environment (source release, DB, sizing, network).
