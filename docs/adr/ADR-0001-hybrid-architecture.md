# ADR-0001: Hybrid architecture (SAP-side ABAP agents + external orchestration/AI platform)
**Status:** Accepted · **Date:** 2026-10-09

## Context
Three options were evaluated: SAP-native (all logic in ABAP), external/cloud-native (Python/Java with SAP connectors
only), hybrid (thin ABAP add-on for extraction/CDC + external platform for orchestration, graph, rules, AI, governance).

## Decision
Hybrid. The SAP add-on (planned, contract in `sap-abap/README.md`) owns what only SAP can do safely: consistent
package reads, application-level change capture, SAP authorization enforcement, source throttling. Everything else —
discovery analytics, dependency graph, scope/carve-out intelligence, rule engine, reconciliation, audit, agents,
UI — runs outside SAP in containers.

## Consequences
+ SAP semantics and authorizations are respected; no generic DB log reader.
+ Horizontal scaling, modern UI and AI tooling without ABAP release constraints.
− Two deployment units and an SAP transport to maintain; network path SAP↔platform must be secured (SNC/TLS).
− Until the add-on exists the platform is limited to synthetic/simulated sources (clearly labelled).
