# Benchmarks — protocol and current (simulated) numbers

Measured with `sdtf bench --scales 1,2,3 --out docs/benchmarks.md` (`backend/sdtf/benchmark.py`): the harness runs the
vertical slice at each scale through the simulated add-on and gateway, times every step, and rewrites the measured
section below with the environment it ran on; `docs/benchmarks.json` holds the raw figures. The hand-written table
that follows is the earlier reading kept for comparison.

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

<!-- benchmark:measured:start -->
## Measured on this environment (simulated run, no SAP system)

Python 3.13.16 · Linux-6.18.44-fc-v114-x86_64-with-glibc2.39 · 4 CPUs · database sqlite · staging relational · measured 2026-10-10T18:32:49.014382+00:00

No throughput or downtime guarantee is derived from these figures: the source and the target are the platform's simulators on the synthetic landscape; they validate the measurement plumbing and give the order of magnitude of the engine itself.

| Scale | Rows | Objects | Generate + import | Discovery (store / add-on) | Graph | Scope | Extract (rec/s) | Transform | Load | Reconcile (checks) | End to end |
|---:|---:|---:|---:|---|---:|---:|---|---:|---:|---|---:|
| 1 | 11,319 | 3,052 | 0.27 s | 0.07 s / 0.90 s | 0.16 s | 0.11 s | 0.36 s (5,916) | 0.54 s | 0.66 s | 0.33 s (343) | 3.66 s |
| 2 | 21,772 | 5,705 | 0.73 s | 0.15 s / 3.26 s | 0.38 s | 0.32 s | 0.98 s (3,888) | 1.05 s | 1.28 s | 0.66 s (345) | 9.18 s |
| 3 | 31,820 | 8,306 | 0.98 s | 0.21 s / 6.72 s | 0.53 s | 0.44 s | 1.76 s (3,169) | 1.30 s | 1.82 s | 0.79 s (345) | 15.18 s |
<!-- benchmark:measured:end -->
