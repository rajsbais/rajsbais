"""Version-aware SAP business object dependency graph.

Nodes: systems, org units, business object instances, tables. Edges: explicit semantic relationships from
the business object catalog. Persisted relationally (portable, transactional); a property-graph backend is
an adapter concern (see ADR-0004). Traversal supports per-edge-type / per-object-type policies and records
an explanation for every automatically expanded node.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..catalog.business_objects import BUSINESS_OBJECTS, RELATIONSHIPS, instance_company_codes
from ..catalog.store import RecordStore
from ..models import GraphEdge, GraphNode


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
        for r in store.rows(bo.header_table):
            key = bo.key_of(r)
            ccs = instance_company_codes(bo, r, store)
            g.add_node(node_id(bo.id, key), bo.id, f"{bo.name} {key}", company_codes=ccs, kind=bo.kind, domain=bo.domain)
    for rel in RELATIONSHIPS:
        bo = BUSINESS_OBJECTS[rel.from_type]
        for r in store.rows(bo.header_table):
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
    session.execute(delete(GraphEdge).where(GraphEdge.system_id == system_id))
    session.execute(delete(GraphNode).where(GraphNode.system_id == system_id))
    nodes = [{"system_id": system_id, "node_id": n["id"], "node_type": n["type"], "label": n["label"][:200], "attributes": n["attributes"]} for n in g.nodes.values()]
    for i in range(0, len(nodes), 2000):
        session.execute(GraphNode.__table__.insert(), nodes[i : i + 2000])
    edges = [{"system_id": system_id, "from_node": e["from"], "to_node": e["to"], "edge_type": e["type"], "attributes": e["attributes"]} for es in g.out_edges.values() for e in es]
    for i in range(0, len(edges), 2000):
        session.execute(GraphEdge.__table__.insert(), edges[i : i + 2000])
    session.flush()
    return g.stats()


def load_graph(session: Session, system_id: str) -> Graph:
    g = Graph()
    for n in session.execute(select(GraphNode.node_id, GraphNode.node_type, GraphNode.label, GraphNode.attributes).where(GraphNode.system_id == system_id)):
        g.nodes[n[0]] = {"id": n[0], "type": n[1], "label": n[2], "attributes": n[3]}
    for e in session.execute(select(GraphEdge.from_node, GraphEdge.to_node, GraphEdge.edge_type, GraphEdge.attributes).where(GraphEdge.system_id == system_id)):
        g.add_edge(e[0], e[1], e[2], **(e[3] or {}))
    return g


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
            if tnode["attributes"].get("missing"):
                if tgt not in res.missing:
                    res.missing.append(tgt)
                continue
            pol = policy.policy_for(e["type"], tnode["type"])
            reason = f"Reached via {e['attributes'].get('name', e['type'])} from {cur} ({e['type']} policy={pol})"
            if pol == "STOP":
                res.stopped.append({"node": tgt, "from": cur, "edge": e["type"], "reason": reason})
                continue
            inclusion = {"FOLLOW": "FULL", "REFERENCE": "REFERENCE", "FLAG": "FLAGGED"}[pol]
            prev = res.included.get(tgt)
            rank = {"FULL": 3, "FLAGGED": 2, "REFERENCE": 1}
            if prev is not None and rank[prev] >= rank[inclusion]:
                continue
            res.included[tgt] = inclusion
            res.traces.append({"node": tgt, "from": cur, "edge": e["type"], "edge_name": e["attributes"].get("name"), "policy": pol, "depth": depth + 1, "reason": reason})
            if pol in ("FOLLOW", "FLAG"):
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
