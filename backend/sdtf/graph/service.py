"""Version-aware SAP business object dependency graph.

Nodes: systems, org units, business object instances, tables. Edges: explicit semantic relationships from
the business object catalog. Persisted relationally (portable, transactional); a property-graph backend is
an adapter concern (see ADR-0004). Traversal supports per-edge-type / per-object-type policies and records
an explanation for every automatically expanded node.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from ..catalog.business_objects import BUSINESS_OBJECTS, RELATIONSHIPS, header_rows, instance_company_codes
from ..catalog.store import RecordStore


def node_id(type_id: str, key: str) -> str:
    return f"{type_id}:{key}"


def split_node(nid: str) -> tuple[str, str]:
    t, _, k = nid.partition(":")
    return t, k


@dataclass
class Graph:
    nodes: dict[str, dict] = field(default_factory=dict)
    out_edges: dict[str, list[dict]] = field(default_factory=lambda: defaultdict(list))
    in_edges: dict[str, list[dict]] = field(default_factory=lambda: defaultdict(list))

    def add_node(self, nid: str, ntype: str, label: str = "", **attrs):
        if nid not in self.nodes:
            self.nodes[nid] = {"id": nid, "type": ntype, "label": label or nid, "attributes": attrs}
        return self.nodes[nid]

    def add_edge(self, frm: str, to: str, etype: str, **attrs):
        e = {"from": frm, "to": to, "type": etype, "attributes": attrs}
        self.out_edges[frm].append(e)
        self.in_edges[to].append(e)
        return e

    def stats(self) -> dict:
        by_type: dict[str, int] = defaultdict(int)
        for n in self.nodes.values():
            by_type[n["type"]] += 1
        edges_by_type: dict[str, int] = defaultdict(int)
        for es in self.out_edges.values():
            for e in es:
                edges_by_type[e["type"]] += 1
        return {"nodes": len(self.nodes), "edges": sum(edges_by_type.values()), "nodes_by_type": dict(by_type), "edges_by_type": dict(edges_by_type)}


def build_graph(store: RecordStore, system_id: str) -> Graph:
    g = Graph()
    g.add_node(f"SYSTEM:{system_id}", "SYSTEM", system_id)
    for bo in BUSINESS_OBJECTS.values():
        for r in header_rows(store, bo):
            key = bo.key_of(r)
            ccs = instance_company_codes(bo, r, store)
            g.add_node(node_id(bo.id, key), bo.id, f"{bo.name} {key}", company_codes=ccs, kind=bo.kind, domain=bo.domain)
    for rel in RELATIONSHIPS:
        bo = BUSINESS_OBJECTS[rel.from_type]
        for r in header_rows(store, bo):
            frm = node_id(bo.id, bo.key_of(r))
            for tkey in rel.resolver(r, store):
                to = node_id(rel.to_type, tkey)
                if to not in g.nodes:
                    g.add_node(to, rel.to_type, f"{rel.to_type} {tkey} (missing)", missing=True)
                g.add_edge(frm, to, rel.edge_type, name=rel.name)
    # cross-company tagging: documents whose company-code set has more than one member
    for n in g.nodes.values():
        ccs = n["attributes"].get("company_codes") or []
        n["attributes"]["cross_company"] = len(ccs) > 1 and BUSINESS_OBJECTS.get(n["type"], None) is not None and BUSINESS_OBJECTS[n["type"]].kind == "TRANSACTIONAL"
        n["attributes"]["shared"] = len(ccs) > 1 and n["type"] in BUSINESS_OBJECTS and BUSINESS_OBJECTS[n["type"]].kind == "MASTER"
    return g


def persist_graph(session: Session, system_id: str, g: Graph) -> dict:
    from .store import get_graph_store

    return get_graph_store(session).persist(system_id, g)


def load_graph(session: Session, system_id: str) -> Graph:
    from .store import get_graph_store

    return get_graph_store(session).load(system_id)


# ------------------------------------------------------------------------------- traversal
POLICIES = ("FOLLOW", "REFERENCE", "STOP", "FLAG")


@dataclass
class TraversalPolicy:
    """Policy per edge type and per target object type. FOLLOW: include and keep expanding. REFERENCE: include as
    reference-only, do not expand further. STOP: do not include. FLAG: include and mark for manual disposition."""

    edge_policies: dict[str, str] = field(default_factory=lambda: {"DOC_FLOW": "FOLLOW", "ACCOUNTING_REF": "FOLLOW", "MASTER_REF": "FOLLOW", "ORG_OWNERSHIP": "REFERENCE", "PARENT_CHILD": "FOLLOW", "CROSS_COMPANY": "FLAG", "SHARED": "FLAG"})
    type_policies: dict[str, str] = field(default_factory=dict)  # overrides by target node type
    max_depth: int = 12
    direction: str = "OUT"  # OUT | BOTH

    def policy_for(self, edge_type: str, target_type: str) -> str:
        return self.type_policies.get(target_type) or self.edge_policies.get(edge_type, "FOLLOW")


@dataclass
class TraversalResult:
    included: dict[str, str] = field(default_factory=dict)  # node -> inclusion (FULL / REFERENCE / FLAGGED)
    traces: list[dict] = field(default_factory=list)
    stopped: list[dict] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


RANK = {"FULL": 3, "FLAGGED": 2, "REFERENCE": 1}
INCLUSION = {"FOLLOW": "FULL", "REFERENCE": "REFERENCE", "FLAG": "FLAGGED"}


def apply_edge_policy(res: TraversalResult, policy: TraversalPolicy, cur: str, tgt: str, tgt_type: str, tgt_missing: bool, edge_type: str, edge_name: str | None, depth: int) -> bool:
    """Apply the traversal policy to one edge (cur -> tgt). Returns True when tgt must be expanded further.
    Shared by the in-process traversal and the server-side frontier traversal so both have identical semantics."""
    if tgt_missing:
        if tgt not in res.missing:
            res.missing.append(tgt)
        return False
    pol = policy.policy_for(edge_type, tgt_type)
    reason = f"Reached via {edge_name or edge_type} from {cur} ({edge_type} policy={pol})"
    if pol == "STOP":
        res.stopped.append({"node": tgt, "from": cur, "edge": edge_type, "reason": reason})
        return False
    inclusion = INCLUSION[pol]
    prev = res.included.get(tgt)
    if prev is not None and RANK[prev] >= RANK[inclusion]:
        return False
    res.included[tgt] = inclusion
    res.traces.append({"node": tgt, "from": cur, "edge": edge_type, "edge_name": edge_name, "policy": pol, "depth": depth + 1, "reason": reason})
    return pol in ("FOLLOW", "FLAG")


def traverse(g: Graph, seeds: list[str], policy: TraversalPolicy | None = None) -> TraversalResult:
    policy = policy or TraversalPolicy()
    res = TraversalResult()
    q: deque[tuple[str, int]] = deque()
    for s in seeds:
        if s in g.nodes:
            res.included[s] = "FULL"
            res.traces.append({"node": s, "from": None, "edge": None, "policy": "SEED", "depth": 0, "reason": "Explicitly selected by scope definition"})
            q.append((s, 0))
    while q:
        cur, depth = q.popleft()
        if depth >= policy.max_depth:
            continue
        edges = list(g.out_edges.get(cur, []))
        if policy.direction == "BOTH":
            edges += [{"from": e["to"], "to": e["from"], "type": e["type"], "attributes": {**e["attributes"], "reverse": True}} for e in g.in_edges.get(cur, [])]
        for e in edges:
            tgt = e["to"]
            tnode = g.nodes.get(tgt)
            if tnode is None:
                continue
            if apply_edge_policy(res, policy, cur, tgt, tnode["type"], bool(tnode["attributes"].get("missing")), e["type"], e["attributes"].get("name"), depth):
                q.append((tgt, depth + 1))
    return res


def neighbourhood(g: Graph, center: str, depth: int = 2, limit: int = 400) -> dict:
    """Sub-graph around a node for the explorer UI."""
    seen = {center}
    frontier = [center]
    for _ in range(depth):
        nxt = []
        for n in frontier:
            # organisational hubs (plants, company codes, G/L accounts) are shown but not expanded through,
            # otherwise every document of the plant would flood the neighbourhood
            if n != center and (n.startswith("CFG.") or n.startswith("FI.GLAccount:")):
                continue
            for e in g.out_edges.get(n, []) + g.in_edges.get(n, []):
                for m in (e["from"], e["to"]):
                    if m not in seen and len(seen) < limit:
                        seen.add(m)
                        nxt.append(m)
        frontier = nxt
    nodes = [g.nodes[n] for n in seen if n in g.nodes]
    edges = [e for n in seen for e in g.out_edges.get(n, []) if e["to"] in seen]
    return {"nodes": nodes, "edges": edges}
