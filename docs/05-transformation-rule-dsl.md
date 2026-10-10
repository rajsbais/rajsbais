# 05 — Transformation rule DSL specification

Engine: `backend/sdtf/rules/engine.py`. Rules are YAML, versioned per project (`rule_sets.version`), content-hashed,
validated against the table registry, unit-tested in place, dry-run on real samples, and approved four-eyes.

```yaml
ruleset: spinco-5000-rules          # name; versions increment per project
version: 1
description: ...
applies_to: { source: {product: ECC}, target: {product: S4HANA} }
lookups:                            # named reference tables usable by value_map / key_map / lookup_enrich
  coa_map: {"140000": "12100000"}
rules:                              # applied in order, deterministically, per record
  - id: cc-reassign
    type: org_reassign              # company code (or other org field) reassignment
    tables: ["*"]                   # glob patterns over table names
    field: BUKRS
    also_fields: [BUKRS_VF, VBUND]
    map: {"5000": "SP01"}
    on_missing: passthrough         # passthrough | error | default
  - id: coa
    type: value_map                 # generic value mapping, map or lookup
    tables: [BSEG, SKB1]
    fields: [HKONT, SAKNR]
    lookup: coa_map
    on_missing: error
  - id: bp-customer
    type: key_map                   # key conversion: prefix | offset | lookup; emit_field copies result
    tables: [KNA1, KNB1, VBAK, BSEG]
    fields: [KUNNR, KUNRG]
    strategy: prefix
    prefix: BP
    emit_field: PARTNER
  - id: fi-number-range
    type: number_range              # offset / prefix numeric document numbers
    tables: [BKPF, BSEG, BSID, BSIK]
    fields: [BELNR, AUGBL]
    offset: 500000000
  - id: fx
    type: currency_convert          # converts amount fields and sets currency field
    tables: [BSEG]
    fields: [DMBTR]
    currency_field: WAERS
    to: EUR
    rates: {"USD->EUR": 0.92}
  - id: ledger
    type: default                   # set when empty (overwrite: true to force)
    tables: [BKPF]
    set: {RLDNR: "0L"}
  - id: tag-rv
    type: conditional               # when → then.set / then.rules (nested rules)
    tables: [BKPF]
    when: {field: BLART, in: [RV]}
    then: {set: {XREF1: MIG}}
  - id: rename
    type: field_map                 # rename/copy fields (drop_source default true)
    tables: [KNA1]
    map: {NAME1: BU_NAME1}
  - id: enrich-pc
    type: lookup_enrich             # derive a field from a lookup keyed by another field
    tables: [BSEG]
    key_field: KOSTL
    lookup: cc_to_pc
    set_field: PRCTR
  - id: parked
    type: reject                    # exception rule
    tables: [BKPF]
    when: {field: BSTAT, in: [S, V]}
    message: statistical or parked document not migrated
tests:                              # embedded, executed on validate
  - {name: cc, table: BKPF, input: {BUKRS: "5000", BELNR: "1", GJAHR: 2024}, expected: {BUKRS: SP01}}
  - {name: parked, table: BKPF, input: {BUKRS: "5000", BELNR: "1", GJAHR: 2024, BSTAT: V}, expect_reject: true}
```
`when` supports `equals`, `in`, `not_in`, `present`, `prefix`, and `all`/`any` composition.

## Editor, lookup tables and per-rule decisions
The YAML above is what gets hashed, validated and approved; the Rules workbench edits it through a grid
(`backend/sdtf/rules/editor.py`). `POST /projects/{id}/rulesets/compose` turns a structured document (name,
description, applies_to, lookups, rules in order, tests) into the canonical YAML and back, with the validation;
`POST /projects/{id}/rulesets` accepts either `source_yaml` or `document`, and `based_on` names the previous version
so that the decisions of rules whose content is unchanged carry over. `GET /rulesets/{id}/rules` is the grid: every
rule in words (fields, mapping or action, condition), with its decision.

Lookup tables come from two-column CSV exports (`POST /rulesets/lookups/from-csv`, `sdtf lookup-csv`): comma,
semicolon, tab or pipe; the first row is the header when it names the requested columns or reads like one
(source/target, from/to, old/new, key/value); duplicates with the same value are skipped and reported, keys that map
to different values are errors; the result is the `lookups:` fragment.

An approver decides per rule (`POST /rulesets/{id}/rules/{rule_id}/decision`: APPROVED, REJECTED with a comment, or
back to PENDING) while the set is a draft and under four eyes (not its author); the decisions are audit events. The
approval of the set is refused while any rule is rejected, and approves every rule still pending with it, recorded
per rule with the approver and comment, so the lineage of each rule names who accepted it.

## Learned mappings (rule factory v2)
`rules/learning.py`, `POST /projects/{id}/rulesets/generate?manifest_id=…&learn=true`, `GET /projects/{id}/rulesets/learned`,
`sdtf rules-learn --project <id>`, the *learn from approved rule sets* switch of the Rules workbench.

Every approved rule set of the tenant's other projects is a reviewed statement of how a value maps. When a
candidate rule set is generated with learning on, the mappings of `value_map` and `org_reassign` rules (maps and
lookups) are collected per field; values every approved set agrees on become a `learned_<field>` lookup and a
`learned-<field>` rule on the tables that carry the field (on_missing passthrough), whose description names the
provenance (project, rule set, version, approver). Values the approved sets map differently are **conflicts**:
listed for review, never proposed. Organisational fields (company code, plant, sales and purchasing organisation,
controlling area) are not learned unless asked for: a company-code map belongs to its own deal. Fields the
generated rules already cover are left alone. Draft and rejected sets teach nothing; other tenants' sets are
never read. Learned rules start as pending decisions like any other rule, so the approver decides with the
provenance in front of them.

## Guarantees
* **Deterministic**: same ruleset + record ⇒ same output; inputs are never mutated.
* **Lineage**: every change yields `{rule, field, from, to}` persisted on the staged record.
* **Validated**: unknown types/tables/fields/lookups, duplicate ids, missing strategy parameters are errors;
  non-idempotent mappings are warnings. Embedded test failures block approval.
* **Idempotent where applicable**: value maps whose targets are not also sources are idempotent; prefix/offset maps
  are applied exactly once per record (staging keyed by source key, loader upserts by target key).
* **Testable**: embedded tests + `dry_run` with per-rule impact and before/after samples + 10 engine unit tests.
