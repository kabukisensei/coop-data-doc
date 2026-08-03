"""Estate map SVG generation (Issue #57)."""

from __future__ import annotations

from dataclasses import dataclass

from coop_data_doc.graph.model import LineageGraph, NodeType


@dataclass
class MapNode:
    id: str
    title: str
    column: int
    tables: int = 0
    views: int = 0
    procs: int = 0
    warnings: int = 0


def generate_estate_map_svg(graph: LineageGraph) -> str:
    """Generate a deterministic inline SVG of the estate."""
    # 1. Aggregate nodes
    # Column mapping:
    # 0: Bronze (or unlayered SQL)
    # 1: Silver
    # 2: Gold
    # 3: Semantic Models
    # 4: Reports

    nodes: dict[str, MapNode] = {}

    def _col(layer: str | None, ntype: NodeType) -> int:
        if ntype == NodeType.REPORT:
            return 4
        if ntype == NodeType.SEMANTIC_MODEL:
            return 3
        if layer == "gold":
            return 2
        if layer == "silver":
            return 1
        return 0

    def _layer(node) -> str | None:
        if node.node_type in (NodeType.BRONZE_TABLE, NodeType.SILVER_TABLE, NodeType.GOLD_TABLE):
            return node.node_type.value.split("-")[0]
        return node.metadata.get("layer")

    for nid, node in sorted(graph.nodes.items()):
        if node.node_type == NodeType.REPORT:
            group_id = f"report:{node.name}"
            title = node.display
            col = 4
        elif node.node_type == NodeType.SEMANTIC_MODEL:
            group_id = f"model:{node.name}"
            title = node.display
            col = 3
        elif node.node_type in (NodeType.MEASURE, NodeType.PBI_TABLE):
            group_id = f"model:{node.schema_name}"
            title = node.schema_name
            col = 3
        else:
            layer = _layer(node)
            schema = node.schema_name or "dbo"
            group_id = f"sql:{layer or 'none'}:{schema}"
            title = schema
            col = _col(layer, node.node_type)

        if group_id not in nodes:
            nodes[group_id] = MapNode(id=group_id, title=title, column=col)

        mnode = nodes[group_id]
        if node.node_type in (
            NodeType.BRONZE_TABLE,
            NodeType.SILVER_TABLE,
            NodeType.GOLD_TABLE,
            NodeType.PBI_TABLE,
        ):
            mnode.tables += 1
        elif node.node_type == NodeType.VIEW:
            mnode.views += 1
        elif node.node_type == NodeType.STORED_PROC:
            mnode.procs += 1

        if node.metadata.get("dynamic_sql_untraced") or node.metadata.get("unresolved_source"):
            mnode.warnings += 1

    # 2. Aggregate edges
    node_to_group = {}
    for nid, node in sorted(graph.nodes.items()):
        if node.node_type == NodeType.REPORT:
            group_id = f"report:{node.name}"
        elif node.node_type == NodeType.SEMANTIC_MODEL:
            group_id = f"model:{node.name}"
        elif node.node_type in (NodeType.MEASURE, NodeType.PBI_TABLE):
            group_id = f"model:{node.schema_name}"
        else:
            layer = _layer(node)
            schema = node.schema_name or "dbo"
            group_id = f"sql:{layer or 'none'}:{schema}"
        node_to_group[nid] = group_id

    edges: dict[tuple[str, str], int] = {}
    for edge in sorted(graph.edges, key=lambda e: (e.source_id, e.target_id, e.edge_type.value)):
        if edge.source_id not in node_to_group or edge.target_id not in node_to_group:
            continue
        src = node_to_group[edge.source_id]
        tgt = node_to_group[edge.target_id]
        if src != tgt:
            edges[(src, tgt)] = edges.get((src, tgt), 0) + 1

    # 3. Layout (Fixed Grid)
    cols = {0: [], 1: [], 2: [], 3: [], 4: []}
    for n in nodes.values():
        cols[n.column].append(n)

    for c in cols.values():
        c.sort(key=lambda x: x.title.lower())

    box_w = 160
    box_h = 60
    gap_x = 100
    gap_y = 20

    max_rows = max(len(c) for c in cols.values())
    width = 5 * box_w + 4 * gap_x + 100
    height = max_rows * (box_h + gap_y) + 100

    positions = {}
    for c_idx, clist in cols.items():
        x = 50 + c_idx * (box_w + gap_x)
        y_start = 50 + (max_rows - len(clist)) * (box_h + gap_y) / 2
        for r_idx, n in enumerate(clist):
            y = y_start + r_idx * (box_h + gap_y)
            positions[n.id] = (x, y)

    # 4. Draw SVG
    lines = []
    lines.append(
        f'<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg" class="estate-map-svg">'
    )

    # Draw edges
    for (src, tgt), weight in sorted(edges.items()):
        if src not in positions or tgt not in positions:
            continue
        sx, sy = positions[src]
        tx, ty = positions[tgt]

        sx += box_w
        sy += box_h / 2
        ty += box_h / 2

        # bezier curve
        cx1 = sx + gap_x / 2
        cy1 = sy
        cx2 = tx - gap_x / 2
        cy2 = ty

        lines.append(
            f'<path d="M {sx} {sy} C {cx1} {cy1}, {cx2} {cy2}, {tx} {ty}" fill="none" stroke="#666" stroke-width="{min(weight, 5)}" opacity="0.5"/>'
        )

    # Draw nodes
    colors = {0: "#cd7f32", 1: "#c0c0c0", 2: "#ffd700", 3: "#00acc1", 4: "#e53935"}

    for n in sorted(nodes.values(), key=lambda x: x.id):
        x, y = positions[n.id]
        color = colors[n.column]

        data_attrs = f'data-title="{n.title}" data-tables="{n.tables}" data-views="{n.views}" data-procs="{n.procs}" data-warnings="{n.warnings}"'

        lines.append(f'<g class="estate-node" {data_attrs} transform="translate({x},{y})">')
        lines.append(
            f'<rect width="{box_w}" height="{box_h}" rx="5" fill="{color}" stroke="#fff" stroke-width="2"/>'
        )

        # Truncate title if too long
        display_title = n.title if len(n.title) <= 20 else n.title[:17] + "..."
        lines.append(
            f'<text x="{box_w / 2}" y="{box_h / 2 + 5}" text-anchor="middle" fill="#fff" font-family="sans-serif" font-size="12" font-weight="bold">{display_title}</text>'
        )

        if n.warnings > 0:
            lines.append(f'<circle cx="{box_w}" cy="0" r="10" fill="red" />')
            lines.append(
                f'<text x="{box_w}" y="4" text-anchor="middle" fill="#fff" font-family="sans-serif" font-size="10" font-weight="bold">{n.warnings}</text>'
            )

        lines.append("</g>")

    lines.append("</svg>")

    return "\n".join(lines)


def render_estate_map_page(graph: LineageGraph) -> str:
    svg = generate_estate_map_svg(graph)
    return f"""# Estate Map
    
<div class="estate-map-container">
{svg}
</div>

<div id="estate-tooltip" style="display:none; position:absolute; background:rgba(0,0,0,0.8); color:white; padding:10px; border-radius:5px; pointer-events:none; z-index:1000; font-size:12px;"></div>

<script src="assets/javascripts/estate-map.js"></script>
"""
