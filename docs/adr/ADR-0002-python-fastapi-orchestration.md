# ADR-0002: Python 3.11+/FastAPI for orchestration, domain services and AI
**Status:** Accepted

## Context
The brief prefers Python/FastAPI for orchestration and AI, Java/Go for performance-sensitive services where justified.

## Decision
One Python service for the API and domain logic now. Performance-sensitive paths (bulk extraction, transformation of
hundreds of millions of rows) are isolated behind interfaces (`Extractor`, `RecordStore`, `transform_record`) so they
can move to a Go/Java worker or a vectorised engine later without touching orchestration.

## Consequences
+ Fastest path to a testable, documented vertical slice; Pydantic contracts double as API docs.
− Python throughput ceiling for transformation (~10–20k rec/s/core measured on synthetic data) → worker pool and
  possibly a compiled transform engine in Phase 4+.
