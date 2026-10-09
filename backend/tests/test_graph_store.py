"""Graph store backends: relational parity and the Neo4j/Cypher adapter (statement-level + opt-in integration)."""
import json
import os

import pytest

from sdtf.catalog.store import RecordStore
from sdtf.graph.service import build_graph, traverse
from sdtf.graph.store import (
    NODE_LABEL,
    CypherGraphStore,
    Neo4jGraphStore,
    RelationalGraphStore,
    get_graph_store,
)
from sdtf.synthetic.ecc_generator import LandscapeSpec, generate_landscape


class FakeCypher:
    """Executes a tiny subset of the Cypher the adapter emits, enough to round-trip persist -> load/stats/search."""

    def __init__(self):
        self.nodes: dict[tuple[str, str], dict] = {}
        self.edges: list[dict] = []

    def __call__(self, query: str, params: dict) -> list[dict]:
        sid = params.get("sid")
        if query.startswith("CREATE CONSTRAINT") or query.startswith("CREATE INDEX"):
            return []
        if "DETACH DELETE" in query:
            self.nodes = {k: v for k, v in self.nodes.items() if k[0] != sid}
            self.edges = [e for e in self.edges if e["sid"] != sid]
            return []
        if query.startswith("UNWIND $rows AS r MERGE (n:"):
            for r in params["rows"]:
                self.nodes[(sid, r["node_id"])] = dict(r)
            return []
        if query.startswith("UNWIND $rows AS r MATCH (a:"):
            etype = query.split("MERGE (a)-[x:", 1)[1].split(" ", 1)[0]
            for r in params["rows"]:
                if (sid, r["from"]) in self.nodes and (sid, r["to"]) in self.nodes:
                    self.edges.append({"sid": sid, "from": r["from"], "to": r["to"], "type": etype, "attributes": r["attributes"]})
            return []
        if "RETURN n.node_id AS id, n.node_type AS type, n.label AS label, n.attributes AS attributes" in query and "WHERE" not in query:
            return [{"id": v["node_id"], "type": v["node_type"], "label": v["label"], "attributes": v["attributes"]} for (s, _), v in self.nodes.items() if s == sid]
        if "RETURN a.node_id AS `from`" in query:
            return [{"from": e["from"], "to": e["to"], "type": e["type"], "attributes": e["attributes"]} for e in self.edges if e["sid"] == sid]
        if "RETURN n.node_type AS type, count(n) AS n" in query:
            c: dict[str, int] = {}
            for (s, _), v in self.nodes.items():
                if s == sid:
                    c[v["node_type"]] = c.get(v["node_type"], 0) + 1
            return [{"type": t, "n": n} for t, n in c.items()]
        if "RETURN type(x) AS type, count(x) AS n" in query:
            c = {}
            for e in self.edges:
                if e["sid"] == sid:
                    c[e["type"]] = c.get(e["type"], 0) + 1
            return [{"type": t, "n": n} for t, n in c.items()]
        if "WHERE" in query and "LIMIT $limit" in query and "RETURN n.node_id AS id" in query:
            out = []
            for (s, _), v in self.nodes.items():
                if s != sid or (params.get("ntype") and v["node_type"] != params["ntype"]) or (params.get("q") and params["q"] not in v["node_id"]):
                    continue
                out.append({"id": v["node_id"], "type": v["node_type"], "label": v["label"], "attributes": v["attributes"]})
                if len(out) >= params["limit"]:
                    break
            return out
        if "RETURN n.node_id AS id LIMIT 1" in query:
            return [{"id": params["nid"]}] if (sid, params["nid"]) in self.nodes else []
        raise AssertionError(f"unexpected cypher: {query[:80]}")


@pytest.fixture(scope="module")
def small_graph():
    tables = generate_landscape(LandscapeSpec(seed=77))
    return build_graph(RecordStore.from_tables("sys", tables), "sys")


def test_cypher_adapter_round_trips_graph(small_graph):
    fake = FakeCypher()
    store = CypherGraphStore(fake)
    st = store.persist("sysA", small_graph)
    assert st["nodes"] == len(small_graph.nodes) and st["backend"] == "cypher"
    # schema + delete + batched node MERGE per type + batched edge MERGE per type, all parameterised
    kinds = [q.split(" ")[0] for q, _ in store.statements]
    assert kinds[:3] == ["CREATE", "CREATE", "MATCH"]
    assert all("$" in q for q, _ in store.statements[2:]), "no literal values in Cypher"
    assert all(len(p.get("rows", [])) <= CypherGraphStore.BATCH for _, p in store.statements)
    labels = {q.split("MERGE (n:")[1].split(" ")[0] for q, _ in store.statements if "MERGE (n:" in q}
    assert f"{NODE_LABEL}:T_SD_SalesOrder" in labels and f"{NODE_LABEL}:T_FI_AccountingDocument" in labels
    loaded = store.load("sysA")
    assert loaded.stats()["nodes"] == small_graph.stats()["nodes"] and loaded.stats()["edges"] == small_graph.stats()["edges"]
    assert loaded.stats()["edges_by_type"] == small_graph.stats()["edges_by_type"]
    # attributes survive JSON round trip; traversal over the loaded graph equals traversal over the original
    so = next(n for n in small_graph.nodes if n.startswith("SD.SalesOrder:"))
    assert loaded.nodes[so]["attributes"] == small_graph.nodes[so]["attributes"]
    assert set(traverse(loaded, [so]).included) == set(traverse(small_graph, [so]).included)
    stats = store.stats("sysA")
    assert stats["nodes_by_type"] == small_graph.stats()["nodes_by_type"] and stats["backend"] == "cypher"
    found = store.search_nodes("sysA", "SD.SalesOrder", None, 5)
    assert len(found) == 5 and all(n["type"] == "SD.SalesOrder" for n in found)
    assert store.search_nodes("sysA", None, so.split(":")[1], 10)[0]["id"] == so
    assert store.has_node("sysA", so) and not store.has_node("sysA", "SD.SalesOrder:nope")
    # tenant/system isolation: another system's persist does not touch sysA
    store.persist("sysB", small_graph)
    assert store.stats("sysA")["nodes"] == small_graph.stats()["nodes"]


def test_cypher_neighbourhood_query_shape():
    calls = []

    def run(q, p):
        calls.append((q, p))
        return [{"nodes": [{"id": "SD.SalesOrder:1", "type": "SD.SalesOrder", "label": "x", "attributes": json.dumps({"company_codes": ["5000"]})}], "edges": []}]

    out = CypherGraphStore(run).neighbourhood("sys", "SD.SalesOrder:1", depth=9, limit=50)
    q, p = calls[0]
    assert "[*1..4]" in q and p == {"sid": "sys", "center": "SD.SalesOrder:1", "limit": 50}, "depth is clamped, parameters bound"
    assert "NOT x.node_type STARTS WITH 'CFG.'" in q, "organisational hubs are not expanded through"
    assert out["nodes"][0]["attributes"]["company_codes"] == ["5000"]


def test_relational_store_parity(session, slice_result):
    store = RelationalGraphStore(session)
    sid = slice_result["source_id"]
    st = store.stats(sid)
    assert st["backend"] == "relational" and st["nodes"] == slice_result["graph_stats"]["nodes"]
    so = store.search_nodes(sid, "SD.SalesOrder", None, 1)[0]["id"]
    assert store.has_node(sid, so)
    nb = store.neighbourhood(sid, so, 2)
    assert any(n["id"] == so for n in nb["nodes"]) and nb["edges"]
    assert get_graph_store(session).name == "relational"


def test_neo4j_backend_requires_uri(session, monkeypatch):
    from sdtf import config

    monkeypatch.setattr(config, "settings", config.Settings(database_url=config.settings.database_url, graph_backend="neo4j", neo4j_uri=""))
    with pytest.raises(RuntimeError, match="SDTF_NEO4J_URI"):
        get_graph_store(session)


@pytest.mark.skipif(not os.getenv("SDTF_NEO4J_URI"), reason="set SDTF_NEO4J_URI / SDTF_NEO4J_PASSWORD to run against a live Neo4j")
def test_neo4j_integration(small_graph):
    store = Neo4jGraphStore(os.environ["SDTF_NEO4J_URI"], os.getenv("SDTF_NEO4J_USER", "neo4j"), os.environ["SDTF_NEO4J_PASSWORD"], os.getenv("SDTF_NEO4J_DATABASE") or None)
    try:
        store.persist("it-sys", small_graph)
        assert store.stats("it-sys")["nodes"] == len(small_graph.nodes)
        loaded = store.load("it-sys")
        assert loaded.stats()["edges"] == small_graph.stats()["edges"]
        so = next(n for n in small_graph.nodes if n.startswith("SD.SalesOrder:"))
        nb = store.neighbourhood("it-sys", so, 2)
        assert any(n["id"] == so for n in nb["nodes"])
    finally:
        store.close()
