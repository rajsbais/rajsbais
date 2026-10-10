# ADR-0005: Declarative YAML rule DSL interpreted by a deterministic engine (no embedded code)
**Status:** Accepted

## Decision
Rules are data (YAML), not code: fixed rule types with validated parameters, named lookups, embedded tests.
No user-supplied Python/ABAP expressions.

## Consequences
+ Rules are auditable, diffable, testable, portable across projects; the engine can be re-implemented in another
  language for throughput with identical semantics.
− Exotic transformations need new rule types (added centrally, with tests) rather than ad-hoc scripts.
