"""Model tables and columns no parsed report or DAX uses (cleanup aid).

For each semantic model that at least one parsed report reads, a table or
column is *used* when any of these names it:

* a report field (shown or filter) bound to it;
* a relationship endpoint (keeps the key column off the column list, but a
  table only joined, never shown or calculated on, is still reported);
* DAX anywhere in the model: a measure, a calculated table or a calculated
  column. A qualified ``Table[Name]`` marks that column; a bare ``[Name]`` (row
  context) marks a column of that name in every table of the model, and a
  calculated column's bare refs its own table's columns, so a column is never
  reported while any DAX could be reading it;
* for a table, also: any DAX reference edge to it, any column of it in use, or
  any measure it hosts (its measures are reported separately as unused measures).

What is not parsed is not seen: RLS roles, calculation groups, field parameters,
sort-by columns, hierarchies, perspectives, Analyze in Excel, paginated reports
and reports outside the configured repos. The result is a list to confirm, never
proof that a field is dead. A model no parsed report reads is skipped: with no
report evidence everything would look unused.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from coop_data_doc.graph.model import EdgeType, LineageGraph, Node, NodeType, normalize_identifier
from coop_data_doc.parsers.dax import extract_qualified_refs, extract_refs


@dataclass
class UnusedFields:
    """One model's unused tables and columns, each sorted for determinism."""

    tables: list[Node] = field(default_factory=list)
    # (table node, column name, hidden) for columns of tables that are themselves used
    columns: list[tuple[Node, str, bool]] = field(default_factory=list)


def _model_has_report(graph: LineageGraph, model: Node) -> bool:
    return any(
        edge.edge_type is EdgeType.FEEDS
        and edge.source_id == model.id
        and (target := graph.nodes.get(edge.target_id)) is not None
        and target.node_type is NodeType.REPORT
        for edge in graph.edges
    )


def unused_fields(graph: LineageGraph, model: Node) -> UnusedFields | None:
    """The model's unused tables and columns, or None when no parsed report
    reads the model (nothing can be judged)."""
    if model.node_type is not NodeType.SEMANTIC_MODEL or not _model_has_report(graph, model):
        return None
    model_key = normalize_identifier(model.name)
    tables = {
        node.id: node
        for node in graph.nodes.values()
        if node.node_type is NodeType.PBI_TABLE and node.schema_name == model_key
    }
    if not tables:
        return UnusedFields()
    by_name = {node.name: node.id for node in tables.values()}
    used_tables: set[str] = set()
    used_columns: set[tuple[str, str]] = set()  # (table id, normalized column)
    bare_names: set[str] = set()
    key_columns: set[tuple[str, str]] = set()  # relationship endpoints

    def mark_dax(dax: str, own_table: str | None = None) -> None:
        for table_key, member in extract_qualified_refs(dax):
            table_id = by_name.get(table_key)
            if table_id is not None:
                used_columns.add((table_id, normalize_identifier(member)))
        bare, _ = extract_refs(dax)
        for name in bare:
            key = normalize_identifier(name)
            bare_names.add(key)
            if own_table is not None:
                used_columns.add((own_table, key))

    # report fields (shown and filter), already resolved to this model's tables
    for node in graph.nodes.values():
        if node.node_type is not NodeType.REPORT:
            continue
        for ref in node.metadata.get("field_refs", []):
            target = ref.get("target", "")
            if target in tables:
                used_tables.add(target)
                if ref.get("kind") in ("column", "unknown"):
                    used_columns.add((target, normalize_identifier(ref.get("property", ""))))

    # relationship endpoints ("table.column", normalized)
    for rel in model.metadata.get("relationships", []):
        for end in (rel.get("from", ""), rel.get("to", "")):
            table_key, _, column = end.rpartition(".")
            table_id = by_name.get(table_key)
            if table_id is not None:
                key_columns.add((table_id, column))

    # DAX: measures, calculated tables, calculated columns
    for node in graph.nodes.values():
        if node.schema_name != model_key:
            continue
        if node.node_type is NodeType.MEASURE:
            mark_dax(node.metadata.get("dax", ""))
            home = node.metadata.get("home_table")
            if home in tables:
                used_tables.add(home)
        elif node.node_type is NodeType.PBI_TABLE:
            if node.metadata.get("calculated_dax"):
                mark_dax(node.metadata["calculated_dax"])
            for dax in node.metadata.get("calculated_columns", {}).values():
                mark_dax(dax, own_table=node.id)

    # any DAX reference edge into a table (measure or calculated table)
    for edge in graph.edges:
        if edge.edge_type is EdgeType.REFERENCES and edge.target_id in tables:
            used_tables.add(edge.target_id)

    result = UnusedFields()
    for table_id in sorted(tables):
        table = tables[table_id]
        if table.metadata.get("measure_table"):
            continue  # a measure home: its measures are judged on their own
        live = {
            column.name
            for column in table.columns
            if (table_id, column.name) in used_columns or column.name in bare_names
        }
        if table_id not in used_tables and not live:
            result.tables.append(table)
            continue
        hidden = set(table.metadata.get("hidden_columns", []))
        for column in sorted(table.columns, key=lambda c: c.name):
            if column.name not in live and (table_id, column.name) not in key_columns:
                result.columns.append((table, column.name, column.name in hidden))
    return result
