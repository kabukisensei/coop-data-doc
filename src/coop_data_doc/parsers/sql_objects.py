"""CREATE TABLE / CREATE VIEW lineage extraction (Module 2)."""

from __future__ import annotations

import re
from pathlib import Path

from sqlglot import exp

from coop_data_doc.config import ParseWarning
from coop_data_doc.crawler import FileEntry
from coop_data_doc.graph.model import (
    Column,
    Edge,
    EdgeType,
    LineageGraph,
    Node,
    NodeType,
    normalize_identifier,
)
from coop_data_doc.parsers.sql_common import (
    PROC_HEADER_RE,
    collect_source_tables,
    parse_batch,
    qualify,
    regex_extract,
    split_batches,
    table_parts,
)

_TABLE_FALLBACK_RE = re.compile(r"\bCREATE\s+TABLE\s+([\w\[\].]+)", re.IGNORECASE)
_VIEW_FALLBACK_RE = re.compile(r"\bCREATE\s+(?:OR\s+ALTER\s+)?VIEW\s+([\w\[\].]+)", re.IGNORECASE)


def read_sql_file(entry: FileEntry) -> str:
    """Read a SQL file tolerantly (BOM-aware, replacement on bad bytes)."""
    return Path(entry.abs_path).read_text(encoding="utf-8-sig", errors="replace")


def stub_table(graph: LineageGraph, schema: str, name: str) -> Node:
    """Get-or-create a table node referenced before (or without) its DDL."""
    return graph.add_node(
        Node(
            id=Node.make_id(NodeType.GOLD_TABLE, schema, name),
            node_type=NodeType.GOLD_TABLE,
            name=name,
            schema_name=schema,
        )
    )


def columns_from_schema(schema_expr: exp.Schema, dialect: str) -> list[Column]:
    """Column contracts from a CREATE TABLE column list.

    Data types are re-rendered through sqlglot, which normalizes exact
    T-SQL synonyms (INT -> INTEGER, DECIMAL -> NUMERIC); precision and
    nullability are always preserved.
    """
    columns: list[Column] = []
    table_level_pk: set[str] = set()
    for item in schema_expr.expressions:
        if isinstance(item, exp.PrimaryKey):
            for col in item.find_all(exp.Column):
                table_level_pk.add(normalize_identifier(col.name))
            for ident in item.find_all(exp.Identifier):
                table_level_pk.add(normalize_identifier(ident.name))
    for item in schema_expr.expressions:
        if not isinstance(item, exp.ColumnDef):
            continue
        name = normalize_identifier(item.name)
        kind = item.args.get("kind")
        data_type = kind.sql(dialect=dialect) if kind is not None else ""
        nullable: bool | None = True
        constraints: list[str] = []
        for constraint in item.args.get("constraints") or []:
            kind_expr = getattr(constraint, "kind", None)
            if isinstance(kind_expr, exp.NotNullColumnConstraint):
                nullable = bool(kind_expr.args.get("allow_null"))
            elif isinstance(kind_expr, exp.PrimaryKeyColumnConstraint):
                constraints.append("PK")
                nullable = False
            elif isinstance(kind_expr, exp.DefaultColumnConstraint):
                try:
                    constraints.append(f"DEFAULT {kind_expr.this.sql(dialect=dialect)}")
                except Exception:
                    constraints.append("DEFAULT")
            elif isinstance(kind_expr, exp.GeneratedAsIdentityColumnConstraint):
                constraints.append("IDENTITY")
            elif isinstance(kind_expr, exp.UniqueColumnConstraint):
                constraints.append("UNIQUE")
            elif kind_expr is not None:
                try:
                    constraints.append(kind_expr.sql(dialect=dialect))
                except Exception:
                    pass
        if name in table_level_pk and "PK" not in constraints:
            constraints.append("PK")
            nullable = False
        columns.append(Column(name=name, data_type=data_type, nullable=nullable, constraints=constraints))
    return columns


def _projection_columns(select: exp.Select) -> tuple[list[Column], bool]:
    """Output columns of a view/CTAS projection; bool = unresolved (star/expr)."""
    columns: list[Column] = []
    unresolved = False
    for item in select.expressions:
        if isinstance(item, exp.Star):
            return [], True
        name = normalize_identifier(item.alias_or_name or "")
        if not name or name == "*":
            unresolved = True
            continue
        columns.append(Column(name=name))
    return columns, unresolved


def _add_reads(
    graph: LineageGraph, reader_id: str, sources: set[tuple[str, str]], evidence_file: str
) -> None:
    for schema, name in sorted(sources):
        source = stub_table(graph, schema, name)
        graph.add_edge(
            Edge(
                source_id=reader_id,
                target_id=source.id,
                edge_type=EdgeType.READS,
                evidence=f"{evidence_file}: FROM {schema}.{name}",
            )
        )


def _handle_create_table(create: exp.Create, graph: LineageGraph, entry: FileEntry, dialect: str) -> None:
    target = create.this
    schema_expr = None
    if isinstance(target, exp.Schema):
        schema_expr = target
        target = target.this
    schema, name = table_parts(target)
    columns = columns_from_schema(schema_expr, dialect) if schema_expr is not None else []
    node = graph.add_node(
        Node(
            id=Node.make_id(NodeType.GOLD_TABLE, schema, name),
            node_type=NodeType.GOLD_TABLE,
            name=name,
            schema_name=schema,
            source_file=entry.path,
            columns=columns,
        )
    )
    if not node.source_file:
        node.source_file = entry.path
    select = create.expression
    if isinstance(select, exp.Select):  # CTAS
        if not node.columns:
            derived, unresolved = _projection_columns(select)
            node.columns.extend(derived)
            if unresolved:
                node.metadata["columns_unresolved"] = True
        _add_reads(graph, node.id, collect_source_tables(select), entry.path)


def _handle_create_view(
    create: exp.Create,
    graph: LineageGraph,
    entry: FileEntry,
    dialect: str,
    warnings: list[ParseWarning],
) -> None:
    schema, name = table_parts(create.this)
    node = graph.add_node(
        Node(
            id=Node.make_id(NodeType.VIEW, schema, name),
            node_type=NodeType.VIEW,
            name=name,
            schema_name=schema,
            source_file=entry.path,
        )
    )
    if not node.source_file:
        node.source_file = entry.path
    select = create.expression
    if isinstance(select, exp.Select):
        columns, unresolved = _projection_columns(select)
        if not node.columns:
            node.columns.extend(columns)
        if unresolved:
            node.metadata["columns_unresolved"] = True
            warnings.append(
                ParseWarning(
                    file=entry.path,
                    message=f"view {schema}.{name} uses SELECT * — output columns unresolved",
                    category="select_star_view",
                )
            )
        _add_reads(graph, node.id, collect_source_tables(select), entry.path)


def parse_sql_objects(
    entries: list[FileEntry], graph: LineageGraph, dialect: str = "tsql"
) -> list[ParseWarning]:
    """Extract gold_table/view nodes, column contracts, and reads edges
    from CREATE TABLE / CREATE VIEW batches; regex fallback when sqlglot
    can't parse, recorded as parse_quality metadata plus a warning.
    """
    warnings: list[ParseWarning] = []
    for entry in entries:
        text = read_sql_file(entry)
        for batch in split_batches(text):
            if PROC_HEADER_RE.search(batch):
                continue  # handled by parse_sql_procs
            creates = [
                expression
                for expression in parse_batch(batch, dialect)
                if isinstance(expression, exp.Create) and (expression.kind or "").upper() in ("TABLE", "VIEW")
            ]
            if creates:
                for create in creates:
                    if (create.kind or "").upper() == "TABLE":
                        _handle_create_table(create, graph, entry, dialect)
                    else:
                        _handle_create_view(create, graph, entry, dialect, warnings)
                continue
            # regex fallback for batches sqlglot couldn't parse
            view_match = _VIEW_FALLBACK_RE.search(batch)
            table_match = _TABLE_FALLBACK_RE.search(batch)
            if not view_match and not table_match:
                continue
            lineage = regex_extract(batch)
            if view_match:
                schema, name = qualify(view_match.group(1))
                node_type = NodeType.VIEW
            else:
                schema, name = qualify(table_match.group(1))
                node_type = NodeType.GOLD_TABLE
            node = graph.add_node(
                Node(
                    id=Node.make_id(node_type, schema, name),
                    node_type=node_type,
                    name=name,
                    schema_name=schema,
                    source_file=entry.path,
                    metadata={"parse_quality": "regex_fallback"},
                )
            )
            if not node.source_file:
                node.source_file = entry.path
            lineage.reads.discard((schema, name))
            _add_reads(graph, node.id, lineage.reads, entry.path)
            warnings.append(
                ParseWarning(
                    file=entry.path,
                    message=f"{node_type.value} {schema}.{name} parsed via regex fallback",
                    category="regex_fallback",
                )
            )
    return warnings
