# ADR-0008: Append-only hash-chained audit log and content-hashed artefacts
**Status:** Accepted

## Decision
Every governance action writes an `audit_events` row whose hash covers the previous hash, timestamp, actor, action,
subject and details. Manifests and rulesets carry sha256 content hashes verified before execution; evidence packages
ship with a hash index.

## Consequences
+ Tamper evidence without external infrastructure; `/audit/verify` runs in O(n).
− Rows must never be updated/deleted; the chain is per database (tenant-level chains and external anchoring are
  future options).
