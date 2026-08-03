"""Core lineage graph data model (Module 0).

Every other module communicates exclusively through this graph: parsers add
nodes and edges, the linker resolves cross-repo references, and the renderers
read the finished graph.

Determinism contract: all traversals return sorted ids, and serialization
(see serialize.py) emits sorted keys, so identical inputs always produce
byte-identical artifacts.
"""

from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, Field, PrivateAttr


class NodeType(str, Enum):
    """Every kind of object the lineage graph can hold."""

    BRONZE_TABLE = "bronze_table"
    SILVER_TABLE = "silver_table"
    GOLD_TABLE = "gold_table"
    VIEW = "view"
    STORED_PROC = "stored_proc"
    SEMANTIC_MODEL = "semantic_model"
    PBI_TABLE = "pbi_table"
    MEASURE = "measure"
    REPORT = "report"
    REPORT_PAGE = "report_page"
    VISUAL = "visual"


class EdgeType(str, Enum):
    """Relationship kinds; see module docstring for data-flow direction."""

    READS = "reads"
    WRITES = "writes"
    FEEDS = "feeds"
    DEFINES = "defines"
    REFERENCES = "references"
    VISUALIZES = "visualizes"


# Edges are authored in the direction that is natural for the parser
# (e.g. a stored proc *reads* a table: proc -> table). Data does not always
# flow in the authored direction, so traversal normalizes via flow():
#
#   reads       proc/view -> table read        data flows target -> source
#   writes      proc -> table written          data flows source -> target
#   feeds       view -> pbi_table,
#               pbi_table -> semantic_model    data flows source -> target
#   defines     proc -> table it creates       data flows source -> target
#   references  measure -> measure/table,
#               proc -> proc (EXEC)            data flows target -> source
#   visualizes  visual -> pbi_table/measure    data flows target -> source
_REVERSED_FLOW = frozenset({EdgeType.READS, EdgeType.REFERENCES, EdgeType.VISUALIZES})

# Identifier delimiters stripped before matching: SQL brackets/double-quotes/
# backticks and the single quotes Power BI wraps around names containing spaces
# (e.g. a TMDL relationship endpoint `'Fact Sales'.Amount`). Stripping the
# single quote here keeps such endpoints matchable against the table node id,
# which is built from the already-unquoted name.
_IDENT_JUNK = re.compile(r"[\[\]\"`']")


def normalize_identifier(raw: str) -> str:
    """Lowercase an identifier and strip bracket/quote characters.

    ``[dbo].[Fact Sales]`` -> ``dbo.fact sales``;
    ``'Fact Sales'.Amount`` -> ``fact sales.amount``
    """
    return _IDENT_JUNK.sub("", raw).strip().lower()


class Column(BaseModel):
    """A column contract: name, type, nullability, constraints."""

    name: str
    data_type: str = ""
    nullable: bool | None = None
    constraints: list[str] = Field(default_factory=list)
    description: str = ""


class Node(BaseModel):
    """One object in the estate. `schema_name` doubles as the semantic
    model name for Power BI nodes ('schema' shadows a pydantic attribute).
    """

    id: str
    node_type: NodeType
    name: str
    # 'schema' shadows a pydantic BaseModel attribute, hence schema_name.
    # Renderers emit it under the front-matter key 'schema'.
    schema_name: str = ""
    # original-case object name for display (name/id stay normalized for
    # matching). Empty -> fall back to `name`. See `display`.
    display_name: str = ""
    source_file: str = ""
    columns: list[Column] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
    # raw defining SQL (CREATE statement / proc body) for the page's Source
    # section. Excluded from serialization so graph.json / manifest.json stay
    # lean — the rendered Markdown page carries the code for humans and agents.
    source_code: str = Field(default="", exclude=True)

    @property
    def display(self) -> str:
        """Original-case name for rendering; falls back to the normalized name."""
        return self.display_name or self.name

    @property
    def qualified_display(self) -> str:
        """`schema.Name` (original case) for page titles and labels."""
        return f"{self.schema_name}.{self.display}" if self.schema_name else self.display

    @staticmethod
    def make_id(node_type: NodeType, schema: str, name: str) -> str:
        """Stable id: '{type}:{schema}.{name}', lowercased, brackets stripped."""
        qualified = f"{schema}.{name}" if schema else name
        return f"{node_type.value}:{normalize_identifier(qualified)}"


class Edge(BaseModel):
    """A typed link between two nodes with human-readable evidence."""

    source_id: str
    target_id: str
    edge_type: EdgeType
    evidence: str = ""

    def key(self) -> tuple[str, str, str]:
        """Identity triple used for deduplication."""
        return (self.source_id, self.target_id, self.edge_type.value)

    def flow(self) -> tuple[str, str]:
        """Return (upstream_id, downstream_id) in data-flow direction."""
        if self.edge_type in _REVERSED_FLOW:
            return (self.target_id, self.source_id)
        return (self.source_id, self.target_id)


class LineageGraph(BaseModel):
    """The single shared data structure: nodes by id plus typed edges,
    with merge-on-conflict adds and cycle-safe traversal.
    """

    nodes: dict[str, Node] = Field(default_factory=dict)
    edges: list[Edge] = Field(default_factory=list)
    # O(1) dedup index over `edges`, keyed by (source, target, type). Not serialized —
    # rebuilt from `edges` on load (model_post_init, so model_validate of a graph.json
    # works) and whenever the edge list is replaced wholesale (replace_edges). Keeps graph
    # construction O(E) instead of the O(E²) full scan add_edge used to do per insert.
    _edge_index: dict[tuple[str, str, str], Edge] = PrivateAttr(default_factory=dict)
    # Cached flow adjacency per direction ("up"/"down"), lazily built by _adjacency and
    # invalidated on any edge mutation. Render is read-only over the graph, so this turns
    # the per-node upstream()/downstream() calls (which rebuilt adjacency EVERY call) into
    # a single rebuild reused across the whole render — O(nodes×edges) -> O(nodes+edges).
    _adj_cache: dict[str, dict[str, set[str]]] = PrivateAttr(default_factory=dict)

    def model_post_init(self, context: object, /) -> None:
        self._reindex()

    def _reindex(self) -> None:
        """Rebuild the edge index from the current `edges` list and drop the adjacency cache."""
        self._edge_index = {edge.key(): edge for edge in self.edges}
        self._adj_cache = {}

    def replace_edges(self, edges: list[Edge]) -> None:
        """Swap in a new edge list and rebuild the index in one O(E) pass. Every site that
        rewrites/filters `edges` wholesale must use this, or the indexes go stale."""
        self.edges = edges
        self._reindex()

    # -- construction -------------------------------------------------------

    def add_node(self, node: Node) -> Node:
        """Add a node, merging into an existing node with the same id.

        Merge policy: existing scalar fields win unless empty; columns are
        unioned by case-insensitive name; metadata keys from the existing
        node win on conflict.
        """
        existing = self.nodes.get(node.id)
        if existing is None:
            self.nodes[node.id] = node
            return node
        if not existing.name:
            existing.name = node.name
        if not existing.schema_name:
            existing.schema_name = node.schema_name
        if not existing.display_name:
            existing.display_name = node.display_name
        if not existing.source_file:
            existing.source_file = node.source_file
        if not existing.source_code:
            existing.source_code = node.source_code
        known = {c.name.lower() for c in existing.columns}
        for col in node.columns:
            if col.name.lower() not in known:
                existing.columns.append(col)
                known.add(col.name.lower())
        existing.metadata = {**node.metadata, **existing.metadata}
        return existing

    def add_edge(self, edge: Edge) -> Edge:
        """Add an edge unless an identical (source, target, type) exists — O(1) via the
        index, with the same evidence-backfill semantics as before."""
        key = edge.key()
        existing = self._edge_index.get(key)
        if existing is not None:
            if not existing.evidence:
                existing.evidence = edge.evidence
            return existing
        self.edges.append(edge)
        self._edge_index[key] = edge
        self._adj_cache = {}  # a new edge changes adjacency; drop the cache
        return edge

    def retype_node(self, node_id: str, new_type: NodeType) -> str:
        """Change a node's type, rewriting its id and all referencing edges.

        Used by the silver-classification post-pass. Returns the new id.
        """
        node = self.nodes.pop(node_id)
        new_id = Node.make_id(new_type, node.schema_name, node.name)
        node.id = new_id
        node.node_type = new_type
        for edge in self.edges:
            if edge.source_id == node_id:
                edge.source_id = new_id
            if edge.target_id == node_id:
                edge.target_id = new_id
        # the in-place rewrite can collide a rewritten edge with an existing
        # identical (source, target, type) edge; dedup so duplicates merge (and
        # self-edges drop), mirroring resolve_stub_references. Done in a single
        # O(E) pass via a key->edge map (NOT a rebuild through add_edge, which
        # rescans all edges per insert and made this O(E²) — the pass is called
        # once per retyped table, so assign_layers was O(tables × edges²) and
        # could stall for minutes on a real estate).
        deduped: dict[tuple[str, str, str], Edge] = {}
        for edge in self.edges:
            if edge.source_id == edge.target_id:
                continue  # self-edge created by the rewrite: drop it
            kept = deduped.get(edge.key())
            if kept is None:
                deduped[edge.key()] = edge
            elif not kept.evidence and edge.evidence:
                kept.evidence = edge.evidence  # backfill evidence, like add_edge
        # `deduped` is already the (rewritten-key -> edge) index; set both so the private
        # index doesn't go stale after the in-place endpoint rewrite changed edge keys.
        self.edges = list(deduped.values())
        self._edge_index = deduped
        self._adj_cache = {}  # endpoints were rewritten; drop the adjacency cache
        # merge if a node with the new id already existed
        return self.add_node(node).id

    # -- traversal ----------------------------------------------------------

    def _adjacency(self, direction: str) -> dict[str, set[str]]:
        cached = self._adj_cache.get(direction)
        if cached is not None:
            return cached
        adj: dict[str, set[str]] = {}
        for edge in self.edges:
            up, down = edge.flow()
            if direction == "up":
                adj.setdefault(down, set()).add(up)
            else:
                adj.setdefault(up, set()).add(down)
        self._adj_cache[direction] = adj
        return adj

    def _walk(self, node_id: str, direction: str, depth: int | None) -> list[str]:
        adj = self._adjacency(direction)
        visited: set[str] = set()
        frontier = [node_id]
        level = 0
        while frontier and (depth is None or level < depth):
            next_frontier: list[str] = []
            for current in frontier:
                for neighbor in adj.get(current, ()):
                    if neighbor != node_id and neighbor not in visited:
                        visited.add(neighbor)
                        next_frontier.append(neighbor)
            frontier = next_frontier
            level += 1
        return sorted(visited)

    def upstream(self, node_id: str, depth: int | None = None) -> list[str]:
        """Ids of nodes whose data flows into node_id (cycle-safe BFS)."""
        return self._walk(node_id, "up", depth)

    def downstream(self, node_id: str, depth: int | None = None) -> list[str]:
        """Ids of nodes that consume node_id's data (cycle-safe BFS)."""
        return self._walk(node_id, "down", depth)

    def subgraph(self, ids: set[str]) -> LineageGraph:
        """A new graph containing the given nodes and edges among them."""
        sub = LineageGraph()
        for node_id in sorted(ids):
            if node_id in self.nodes:
                sub.nodes[node_id] = self.nodes[node_id].model_copy(deep=True)
        sub.replace_edges(
            [
                edge.model_copy(deep=True)
                for edge in self.edges
                if edge.source_id in sub.nodes and edge.target_id in sub.nodes
            ]
        )
        return sub


def merge_relationships(model_node: Node, new_rels: list[dict]) -> None:
    """Merge Power BI relationship records onto a semantic-model node's
    ``metadata["relationships"]``, deduped by (from, to) column endpoints and
    deterministically sorted. Shared by the TMDL and BIM parsers so both emit
    the identical shape: each record is
    ``{"from", "to", "active": bool, "bidirectional": bool}`` (``from`` is the
    many/fact side, ``to`` the one/dimension side). Later records win on
    conflict, so a re-parse refreshes the flags rather than duplicating edges.
    """
    by_key: dict[tuple[str, str], dict] = {}
    for rel in model_node.metadata.get("relationships", []):
        by_key[(rel["from"], rel["to"])] = rel
    for rel in new_rels:
        by_key[(rel["from"], rel["to"])] = {
            "from": rel["from"],
            "to": rel["to"],
            "active": bool(rel.get("active", True)),
            "bidirectional": bool(rel.get("bidirectional", False)),
        }
    model_node.metadata["relationships"] = [by_key[key] for key in sorted(by_key)]
