import csv
from pathlib import Path

from coop_data_doc.graph.model import LineageGraph, NodeType
from coop_data_doc.render.markdown import _used_measure_ids


def _write_row(writer, values) -> None:
    # CSV quoting protects delimiters, not spreadsheet formula execution.
    cells = [str(value) for value in values]
    writer.writerow(
        [
            "'" + cell
            if cell.lstrip().startswith(("=", "+", "-", "@")) or cell.startswith(("\t", "\r", "\n"))
            else cell
            for cell in cells
        ]
    )


def export_csvs(graph: LineageGraph, out_dir: Path) -> None:
    """Export the graph as a set of deterministic, stakeholder-ready CSV files."""
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. objects.csv
    with open(out_dir / "objects.csv", "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        _write_row(writer, ["id", "type", "schema", "name", "layer", "source_file", "trust"])
        for node in sorted(graph.nodes.values(), key=lambda n: n.id):
            layer = node.metadata.get("layer", "")
            trust = node.metadata.get("trust", "")
            _write_row(
                writer,
                [
                    node.id,
                    node.node_type.value,
                    node.schema_name,
                    node.display,
                    layer,
                    node.source_file,
                    trust,
                ],
            )

    # 2. columns.csv
    with open(out_dir / "columns.csv", "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        _write_row(writer, ["object_id", "column", "type", "nullable", "constraints"])
        for node in sorted(graph.nodes.values(), key=lambda n: n.id):
            for col in node.columns:
                nullable_str = str(col.nullable) if col.nullable is not None else ""
                constraints_str = ", ".join(col.constraints)
                _write_row(
                    writer,
                    [
                        node.id,
                        col.name,
                        col.data_type,
                        nullable_str,
                        constraints_str,
                    ],
                )

    # 3. measures.csv
    used = _used_measure_ids(graph)
    with open(out_dir / "measures.csv", "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        _write_row(writer, ["model", "measure", "dax", "unused"])
        for node in sorted(graph.nodes.values(), key=lambda n: n.id):
            if node.node_type is NodeType.MEASURE:
                is_unused = str(node.id not in used)
                dax = node.metadata.get("dax", "")
                _write_row(writer, [node.schema_name, node.display, dax, is_unused])

    # 4. edges.csv
    with open(out_dir / "edges.csv", "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        _write_row(writer, ["source", "target", "edge_type", "evidence"])
        for edge in sorted(graph.edges, key=lambda e: e.key()):
            _write_row(
                writer,
                [
                    edge.source_id,
                    edge.target_id,
                    edge.edge_type.value,
                    edge.evidence,
                ],
            )
