"""Mermaid flowchart generation (Module 5).

Mermaid node aliases are sequential (n0, n1, ...) assigned over sorted
graph ids, so charts are deterministic. Labels are wrapped in double
quotes and additionally have mermaid-significant characters neutralised
(``"`` ``<`` ``>`` ``|`` and backtick), since object names — especially
DAX measure names — can contain anything.
"""

from __future__ import annotations

import hashlib
import re

from coop_data_doc.graph.model import LineageGraph, Node, NodeType

# Characters illegal in Windows filenames (plus control chars). Names also
# can't end in a dot/space on Windows, and must stay well under MAX_PATH.
_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_COLLAPSE_DASH = re.compile(r"-{2,}")
_SLUG_MAX = 80  # readable portion; a short id-hash is always appended

# (open, close) bracket pair per node type
_SHAPES: dict[NodeType, tuple[str, str]] = {
    NodeType.BRONZE_TABLE: ("[(", ")]"),
    NodeType.SILVER_TABLE: ("[(", ")]"),
    NodeType.GOLD_TABLE: ("[(", ")]"),
    NodeType.VIEW: ("[/", "/]"),
    NodeType.STORED_PROC: ("{{", "}}"),
    NodeType.SEMANTIC_MODEL: ("([", "])"),
    NodeType.PBI_TABLE: ("([", "])"),
    NodeType.MEASURE: (">", "]"),
    NodeType.REPORT: ("[", "]"),
    NodeType.REPORT_PAGE: ("[", "]"),
    NodeType.VISUAL: ("[", "]"),
}

_ESTATE_LAYERS: list[tuple[str, tuple[NodeType, ...]]] = [
    ("Bronze", (NodeType.BRONZE_TABLE,)),
    ("Silver", (NodeType.SILVER_TABLE,)),
    ("Gold", (NodeType.STORED_PROC, NodeType.GOLD_TABLE)),
    ("Views", (NodeType.VIEW,)),
    ("Semantic Models", (NodeType.PBI_TABLE, NodeType.SEMANTIC_MODEL)),
    ("Reports", (NodeType.REPORT,)),
]
_ESTATE_TYPES = {node_type for _, types in _ESTATE_LAYERS for node_type in types}

ESTATE_CHART_NODE_CAP = 150


def slug(node_id: str) -> str:
    """Filesystem-safe, length-bounded, collision-free page name for a node id.

    A readable portion (the id's name, with every filesystem-illegal
    character — ``< > : " / \\ | ? *`` and control chars — plus dots and
    spaces replaced by ``-``) is followed by a short deterministic hash of
    the full id. The hash guarantees uniqueness (two distinct ids never
    collide to one filename) and keeps names safe on Windows, where the
    original crashed on characters like ``|`` in DAX measure names. Pure
    function of the id, so every link generator stays consistent.
    """
    name_part = node_id.split(":", 1)[1] if ":" in node_id else node_id
    safe = _UNSAFE_CHARS.sub("-", name_part)
    safe = safe.replace(".", "-").replace(" ", "-")
    safe = _COLLAPSE_DASH.sub("-", safe).strip("-. ")
    if len(safe) > _SLUG_MAX:
        safe = safe[:_SLUG_MAX].strip("-. ")
    if not safe:
        safe = node_id.split(":", 1)[0]  # fall back to the node type
    digest = hashlib.sha1(node_id.encode("utf-8")).hexdigest()[:8]
    return f"{safe}-{digest}"


def doc_relpath(node: Node) -> str:
    """Path of a node's markdown page relative to another node's page."""
    return f"../{node.node_type.value}/{slug(node.id)}.md"


# mermaid-significant characters → safe visual substitutes (keeps labels
# readable while avoiding any chance of breaking node/edge syntax)
_LABEL_SUBST = str.maketrans({'"': "'", "<": "(", ">": ")", "|": "/", "`": "'"})


def _label(node: Node) -> str:
    return node.qualified_display.translate(_LABEL_SUBST)


def _node_line(alias: str, node: Node) -> str:
    open_b, close_b = _SHAPES.get(node.node_type, ("[", "]"))
    return f'    {alias}{open_b}"{_label(node)}"{close_b}'


def _flow_edges(graph: LineageGraph, ids: set[str]) -> list[tuple[str, str, str]]:
    seen: set[tuple[str, str, str]] = set()
    for edge in graph.edges:
        upstream_id, downstream_id = edge.flow()
        if upstream_id in ids and downstream_id in ids:
            seen.add((upstream_id, downstream_id, edge.edge_type.value))
    return sorted(seen)


def local_flowchart(graph: LineageGraph, node_id: str, up_depth: int = 2, down_depth: int = 2) -> str:
    """Mermaid chart of a node's neighborhood (default 2 hops each way),
    with click-through links and the focus node highlighted.
    """
    ids = (
        {node_id}
        | set(graph.upstream(node_id, depth=up_depth))
        | set(graph.downstream(node_id, depth=down_depth))
    )
    ids &= set(graph.nodes)
    ordered = sorted(ids)
    alias = {nid: f"n{index}" for index, nid in enumerate(ordered)}

    lines = ["flowchart LR"]
    for nid in ordered:
        lines.append(_node_line(alias[nid], graph.nodes[nid]))
    for upstream_id, downstream_id, label in _flow_edges(graph, ids):
        lines.append(f"    {alias[upstream_id]} -->|{label}| {alias[downstream_id]}")
    for nid in ordered:
        if nid != node_id:
            lines.append(f'    click {alias[nid]} "{doc_relpath(graph.nodes[nid])}"')
    lines.append(f"    style {alias[node_id]} stroke-width:3px")
    return "\n".join(lines)


def estate_flowchart(graph: LineageGraph) -> str | None:
    """Whole-estate chart grouped by layer; None when above the node cap."""
    ids = {nid for nid, node in graph.nodes.items() if node.node_type in _ESTATE_TYPES}
    if not ids or len(ids) > ESTATE_CHART_NODE_CAP:
        return None
    ordered = sorted(ids)
    alias = {nid: f"n{index}" for index, nid in enumerate(ordered)}

    lines = ["flowchart LR"]
    for layer_name, types in _ESTATE_LAYERS:
        members = [nid for nid in ordered if graph.nodes[nid].node_type in types]
        if not members:
            continue
        lines.append(f'    subgraph {layer_name.replace(" ", "_")}["{layer_name}"]')
        for nid in members:
            lines.append("    " + _node_line(alias[nid], graph.nodes[nid]).strip())
        lines.append("    end")
    for upstream_id, downstream_id, label in _flow_edges(graph, ids):
        lines.append(f"    {alias[upstream_id]} -->|{label}| {alias[downstream_id]}")
    for nid in ordered:
        node = graph.nodes[nid]
        lines.append(f'    click {alias[nid]} "{node.node_type.value}/{slug(nid)}.md"')
    return "\n".join(lines)
