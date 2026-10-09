"""Graph store backends for the dependency graph (ADR-0004).

* RelationalGraphStore - nodes/edges in the metadata database (default); traversal in process.
* Neo4jGraphStore      - property graph in Neo4j via the official driver: batched MERGE/UNWIND persistence,
                         server-side neighbourhood and statistics, in-process traversal over a loaded `Graph`.
The Cypher generation is isolated in CypherGraphStore around a `run(query, params) -> rows` callable so it can be
verified without a server; Neo4jGraphStore binds it to a driver session. Configuration: SDTF_GRAPH_BACKEND
(relational | neo4j), SDTF_NEO4J_URI, SDTF_NEO4J_USER, SDTF_NEO4J_PASSWORD, SDTF_NEO4J_DATABASE.
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Callable, Protocol

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .. import observability as obs
from ..models import GraphEdge, GraphNode
from .service import Graph
from .service import neighbourhood as _inproc_neighbourhood

NODE_LABEL = "SdtfNode"


class GraphStore(Protocol):
    name: str

    def persist(self, system_id: str, g: Graph) -> dict: ...
    def load(self, system_id: str) -> Graph: ...
    def stats(self, system_id: str) -> dict: ...
    def search_nodes(self, system_id: str, node_type: str | None, q: str | None, limit: int) -> list[dict]: ...
    def neighbourhood(self, system_id: str, center: str, depth: int, limit: int = 400) -> dict: ...
    def has_node(self, system_id: str, node_id: str) -> bool: ...


# ---------------------------------------------------------------------------------------------- relational
class RelationalGraphStore:
    name = "relational"

    def __init__(self, session: Session):
        self.session = session

    def persist(self, system_id: str, g: Graph) -> dict:
        s = self.session
        s.execute(delete(GraphEdge).where(GraphEdge.system_id == system_id))
        s.execute(delete(GraphNode).where(GraphNode.system_id == system_id))
        nodes = [{"system_id": system_id, "node_id": n["id"], "node_type": n["type"], "label": n["label"][:200], "attributes": n["attributes"]} for n in g.nodes.values()]
        for i in range(0, len(nodes), 2000):
            s.execute(GraphNode.__table__.insert(), nodes[i : i + 2000])
        edges = [{"system_id": system_id, "from_node": e["from"], "to_node": e["to"], "edge_type": e["type"], "attributes": e["attributes"]} for es in g.out_edges.values() for e in es]
        for i in range(0, len(edges), 2000):
            s.execute(GraphEdge.__table__.insert(), edges[i : i + 2000])
        s.flush()
        return {**g.stats(), "backend": self.name}

    def load(self, system_id: str) -> Graph:
        g = Graph()
        for n in self.session.execute(select(GraphNode.node_id, GraphNode.node_type, GraphNode.label, GraphNode.attributes).where(GraphNode.system_id == system_id)):
            g.nodes[n[0]] = {"id": n[0], "type": n[1], "label": n[2], "attributes": n[3]}
        for e in self.session.execute(select(GraphEdge.from_node, GraphEdge.to_node, GraphEdge.edge_type, GraphEdge.attributes).where(GraphEdge.system_id == system_id)):
            g.add_edge(e[0], e[1], e[2], **(e[3] or {}))
        return g

    def stats(self, system_id: str) -> dict:
        nodes = self.session.execute(select(GraphNode.node_type, func.count()).where(GraphNode.system_id == system_id).group_by(GraphNode.node_type)).all()
        edges = self.session.execute(select(GraphEdge.edge_type, func.count()).where(GraphEdge.system_id == system_id).group_by(GraphEdge.edge_type)).all()
        return {"nodes": sum(n for _, n in nodes), "edges": sum(n for _, n in edges), "nodes_by_type": dict(nodes), "edges_by_type": dict(edges), "backend": self.name}

    def search_nodes(self, system_id: str, node_type: str | None, q: str | None, limit: int) -> list[dict]:
        stmt = select(GraphNode).where(GraphNode.system_id == system_id)
        if node_type:
            stmt = stmt.where(GraphNode.node_type == node_type)
        if q:
            stmt = stmt.where(GraphNode.node_id.like(f"%{q}%"))
        return [{"id": n.node_id, "type": n.node_type, "label": n.label, "attributes": n.attributes} for n in self.session.execute(stmt.limit(limit)).scalars()]

    def neighbourhood(self, system_id: str, center: str, depth: int, limit: int = 400) -> dict:
        return _inproc_neighbourhood(self.load(system_id), center, depth, limit)

    def has_node(self, system_id: str, node_id: str) -> bool:
        return self.session.execute(select(GraphNode.id).where(GraphNode.system_id == system_id, GraphNode.node_id == node_id)).first() is not None


# ---------------------------------------------------------------------------------------------- cypher
class CypherGraphStore:
    """Property-graph persistence through Cypher. Nodes carry label SdtfNode plus a label per object type
    (dots replaced by underscores), properties system_id, node_id, node_type, label and JSON attributes; relationships
    are typed by edge type with `name` and `system_id` properties. All statements are parameterised and batched."""

    name = "cypher"
    BATCH = 1000

    def __init__(self, run: Callable[[str, dict], list[dict]]):
        self.run = run
        self.statements: list[tuple[str, dict]] = []

    def _run(self, query: str, params: dict | None = None) -> list[dict]:
        params = params or {}
        self.statements.append((query, params))
        return self.run(query, params)

    @staticmethod
    def type_label(node_type: str) -> str:
        return "T_" + "".join(c if c.isalnum() else "_" for c in node_type)

    def ensure_schema(self) -> None:
        self._run(f"CREATE CONSTRAINT sdtf_node_key IF NOT EXISTS FOR (n:{NODE_LABEL}) REQUIRE (n.system_id, n.node_id) IS UNIQUE")
        self._run(f"CREATE INDEX sdtf_node_type IF NOT EXISTS FOR (n:{NODE_LABEL}) ON (n.system_id, n.node_type)")

    def persist(self, system_id: str, g: Graph) -> dict:
        with obs.timed("sdtf.graph.persist", system_id=system_id, backend=self.name, nodes=len(g.nodes)):
            self.ensure_schema()
            self._run(f"MATCH (n:{NODE_LABEL} {{system_id: $sid}}) DETACH DELETE n", {"sid": system_id})
            by_type: dict[str, list[dict]] = defaultdict(list)
            for n in g.nodes.values():
                by_type[n["type"]].append({"node_id": n["id"], "node_type": n["type"], "label": n["label"][:200], "attributes": json.dumps(n["attributes"], default=str), "company_codes": list(n["attributes"].get("company_codes") or []), "cross_company": bool(n["attributes"].get("cross_company")), "shared": bool(n["attributes"].get("shared")), "missing": bool(n["attributes"].get("missing"))})
            for ntype, rows in by_type.items():
                for i in range(0, len(rows), self.BATCH):
                    self._run(f"UNWIND $rows AS r MERGE (n:{NODE_LABEL}:{self.type_label(ntype)} {{system_id: $sid, node_id: r.node_id}}) SET n.node_type = r.node_type, n.label = r.label, n.attributes = r.attributes, n.company_codes = r.company_codes, n.cross_company = r.cross_company, n.shared = r.shared, n.missing = r.missing", {"sid": system_id, "rows": rows[i : i + self.BATCH]})
            by_etype: dict[str, list[dict]] = defaultdict(list)
            for es in g.out_edges.values():
                for e in es:
                    by_etype[e["type"]].append({"from": e["from"], "to": e["to"], "name": e["attributes"].get("name", ""), "attributes": json.dumps(e["attributes"], default=str)})
            for etype, rows in by_etype.items():
                for i in range(0, len(rows), self.BATCH):
                    self._run(f"UNWIND $rows AS r MATCH (a:{NODE_LABEL} {{system_id: $sid, node_id: r.from}}), (b:{NODE_LABEL} {{system_id: $sid, node_id: r.to}}) MERGE (a)-[x:{etype} {{system_id: $sid, name: r.name}}]->(b) SET x.attributes = r.attributes", {"sid": system_id, "rows": rows[i : i + self.BATCH]})
            return {**g.stats(), "backend": self.name}

    def load(self, system_id: str) -> Graph:
        g = Graph()
        for row in self._run(f"MATCH (n:{NODE_LABEL} {{system_id: $sid}}) RETURN n.node_id AS id, n.node_type AS type, n.label AS label, n.attributes AS attributes", {"sid": system_id}):
            g.nodes[row["id"]] = {"id": row["id"], "type": row["type"], "label": row["label"], "attributes": json.loads(row["attributes"] or "{}")}
        for row in self._run(f"MATCH (a:{NODE_LABEL} {{system_id: $sid}})-[x {{system_id: $sid}}]->(b:{NODE_LABEL}) RETURN a.node_id AS `from`, b.node_id AS `to`, type(x) AS type, x.attributes AS attributes", {"sid": system_id}):
            g.add_edge(row["from"], row["to"], row["type"], **json.loads(row["attributes"] or "{}"))
        return g

    def stats(self, system_id: str) -> dict:
        nodes = {r["type"]: r["n"] for r in self._run(f"MATCH (n:{NODE_LABEL} {{system_id: $sid}}) RETURN n.node_type AS type, count(n) AS n", {"sid": system_id})}
        edges = {r["type"]: r["n"] for r in self._run(f"MATCH (:{NODE_LABEL} {{system_id: $sid}})-[x {{system_id: $sid}}]->() RETURN type(x) AS type, count(x) AS n", {"sid": system_id})}
        return {"nodes": sum(nodes.values()), "edges": sum(edges.values()), "nodes_by_type": nodes, "edges_by_type": edges, "backend": self.name}

    def search_nodes(self, system_id: str, node_type: str | None, q: str | None, limit: int) -> list[dict]:
        where = ["n.system_id = $sid"]
        params: dict = {"sid": system_id, "limit": int(limit)}
        if node_type:
            where.append("n.node_type = $ntype")
            params["ntype"] = node_type
        if q:
            where.append("n.node_id CONTAINS $q")
            params["q"] = q
        rows = self._run(f"MATCH (n:{NODE_LABEL}) WHERE {' AND '.join(where)} RETURN n.node_id AS id, n.node_type AS type, n.label AS label, n.attributes AS attributes LIMIT $limit", params)
        return [{"id": r["id"], "type": r["type"], "label": r["label"], "attributes": json.loads(r["attributes"] or "{}")} for r in rows]

    def neighbourhood(self, system_id: str, center: str, depth: int, limit: int = 400) -> dict:
        """Server-side variable-length expansion. Organisational hubs are not expanded through (same rule as the
        in-process explorer), implemented by excluding CFG_* / FI_GLAccount labels from intermediate hops."""
        depth = max(1, min(int(depth), 4))
        rows = self._run(
            f"MATCH (c:{NODE_LABEL} {{system_id: $sid, node_id: $center}}) "
            f"CALL {{ WITH c MATCH p = (c)-[*1..{depth}]-(m:{NODE_LABEL}) "
            f"WHERE ALL(x IN nodes(p)[1..-1] WHERE NOT x.node_type STARTS WITH 'CFG.' AND x.node_type <> 'FI.GLAccount') "
            f"RETURN DISTINCT m LIMIT $limit }} "
            "WITH collect(m) + c AS ns UNWIND ns AS n "
            "WITH collect(DISTINCT n) AS ns "
            "UNWIND ns AS a MATCH (a)-[x]->(b) WHERE b IN ns "
            "RETURN [n IN ns | {id: n.node_id, type: n.node_type, label: n.label, attributes: n.attributes}] AS nodes, collect({from: a.node_id, to: b.node_id, type: type(x), attributes: x.attributes}) AS edges",
            {"sid": system_id, "center": center, "limit": int(limit)},
        )
        if not rows:
            return {"nodes": [], "edges": []}
        nodes = [{**n, "attributes": json.loads(n["attributes"] or "{}")} for n in rows[0]["nodes"]]
        edges = [{**e, "attributes": json.loads(e["attributes"] or "{}")} for e in rows[0]["edges"]]
        return {"nodes": nodes, "edges": edges}

    def has_node(self, system_id: str, node_id: str) -> bool:
        return bool(self._run(f"MATCH (n:{NODE_LABEL} {{system_id: $sid, node_id: $nid}}) RETURN n.node_id AS id LIMIT 1", {"sid": system_id, "nid": node_id}))


class Neo4jGraphStore(CypherGraphStore):
    name = "neo4j"

    def __init__(self, uri: str, user: str, password: str, database: str | None = None):
        try:
            from neo4j import GraphDatabase
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("neo4j graph backend requires the neo4j driver (pip install 'sdtf[neo4j]')") from e
        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        self.database = database
        super().__init__(self._execute)

    def _execute(self, query: str, params: dict) -> list[dict]:
        with self.driver.session(database=self.database) as s:
            return [r.data() for r in s.run(query, **params)]

    def close(self) -> None:
        self.driver.close()


def get_graph_store(session: Session, backend: str | None = None) -> GraphStore:
    from .. import config

    name = backend or config.settings.graph_backend
    if name == "relational":
        return RelationalGraphStore(session)
    if name == "neo4j":
        c = config.settings
        if not c.neo4j_uri:
            raise RuntimeError("SDTF_GRAPH_BACKEND=neo4j requires SDTF_NEO4J_URI")
        return Neo4jGraphStore(c.neo4j_uri, c.neo4j_user, c.neo4j_password, c.neo4j_database or None)
    raise ValueError(f"unknown graph backend {name!r}")
