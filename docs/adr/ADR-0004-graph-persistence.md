# ADR-0004: Relational persistence of the dependency graph first; property-graph backend as an adapter
**Status:** Accepted

## Context
The brief suggests a graph database for semantic dependencies.

## Decision
A `GraphStore` contract (`backend/sdtf/graph/store.py`) with two implementations. `RelationalGraphStore` (default)
persists nodes/edges in `graph_nodes`/`graph_edges`. `Neo4jGraphStore` persists a property graph through batched,
parameterised Cypher (label `SdtfNode` plus a label per object type, relationships typed by edge type, JSON
attributes), answers statistics, node search and variable-length neighbourhoods server-side, and loads a system's
graph into the in-process `Graph` for scope traversal. Selected by `SDTF_GRAPH_BACKEND` with `SDTF_NEO4J_URI`,
`SDTF_NEO4J_USER`, `SDTF_NEO4J_PASSWORD`, `SDTF_NEO4J_DATABASE`. The Cypher layer is isolated around a
`run(query, params)` callable so it is testable without a server.

## Consequences
+ No extra infrastructure by default; transactional consistency with manifests; Neo4j available for large
  landscapes and for graph-native exploration by analysts.
+ Traversal is part of the store contract: the relational store traverses in process; the Neo4j store runs a
  server-side *frontier* traversal (one adjacency query per depth level over the current frontier, chunked), applying
  the same policy function as the in-process algorithm, so inclusion ranks, stopped and missing sets are identical
  while only the frontier's edges are transferred. Scope evaluation streams node rows (no edges) for seeds and
  classification and delegates expansion to the store.
− The Neo4j adapter is verified at Cypher-statement level (including traversal parity across policies) and through
  an opt-in integration test, not against a live server in this environment.
