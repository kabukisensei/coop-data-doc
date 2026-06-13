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
from typing import Optional

from pydantic import BaseModel, Field


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

_IDENT_JUNK = re.compile(r'[\[\]"`]')


def normalize_identifier(raw: str) -> str:
    """Lowercase an identifier and strip bracket/quote characters.

    ``[dbo].[Fact Sales]`` -> ``dbo.fact sales``
    """
    return _IDENT_JUNK.sub("", raw).strip().lower()


class Column(BaseModel):
    """A column contract: name, type, nullability, constraints."""

    name: str
    data_type: str = ""
    nullable: Optional[bool] = None
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
    source_file: str = ""
    columns: list[Column] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)

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
        if not existing.source_file:
            existing.source_file = node.source_file
        known = {c.name.lower() for c in existing.columns}
        for col in node.columns:
            if col.name.lower() not in known:
                existing.columns.append(col)
                known.add(col.name.lower())
        existing.metadata = {**node.metadata, **existing.metadata}
        return existing

    def add_edge(self, edge: Edge) -> Edge:
        """Add an edge unless an identical (source, target, type) exists."""
        for existing in self.edges:
            if existing.key() == edge.key():
                if not existing.evidence:
                    existing.evidence = edge.evidence
                return existing
        self.edges.append(edge)
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
        # merge if a node with the new id already existed
        return self.add_node(node).id

    # -- traversal ----------------------------------------------------------

    def _adjacency(self, direction: str) -> dict[str, set[str]]:
        adj: dict[str, set[str]] = {}
        for edge in self.edges:
            up, down = edge.flow()
            if direction == "up":
                adj.setdefault(down, set()).add(up)
            else:
                adj.setdefault(up, set()).add(down)
        return adj

    def _walk(self, node_id: str, direction: str, depth: Optional[int]) -> list[str]:
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

    def upstream(self, node_id: str, depth: Optional[int] = None) -> list[str]:
        """Ids of nodes whose data flows into node_id (cycle-safe BFS)."""
        return self._walk(node_id, "up", depth)

    def downstream(self, node_id: str, depth: Optional[int] = None) -> list[str]:
        """Ids of nodes that consume node_id's data (cycle-safe BFS)."""
        return self._walk(node_id, "down", depth)

    def subgraph(self, ids: set[str]) -> "LineageGraph":
        """A new graph containing the given nodes and edges among them."""
        sub = LineageGraph()
        for node_id in sorted(ids):
            if node_id in self.nodes:
                sub.nodes[node_id] = self.nodes[node_id].model_copy(deep=True)
        for edge in self.edges:
            if edge.source_id in sub.nodes and edge.target_id in sub.nodes:
                sub.edges.append(edge.model_copy(deep=True))
        return sub
