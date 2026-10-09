# ADR-0004: Relational persistence of the dependency graph first; property-graph backend as an adapter
**Status:** Accepted

## Context
The brief suggests a graph database for semantic dependencies.

## Decision
Persist nodes/edges in `graph_nodes`/`graph_edges` and load them into an in-process adjacency structure for
traversal. Keep the `Graph` interface (`add_node`, `add_edge`, `out_edges`, `in_edges`, `traverse`, `neighbourhood`)
backend-agnostic so Neo4j/JanusGraph can be plugged in when instance graphs exceed memory (≈ tens of millions of edges).

## Consequences
+ No extra infrastructure for the first increments; transactional consistency with manifests.
− In-process traversal is bounded by memory; mitigated by building per-system graphs and pruning hub expansion.
