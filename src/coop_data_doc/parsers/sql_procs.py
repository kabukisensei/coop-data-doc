"""Stored-procedure DML lineage extraction (Module 2).

T-SQL proc bodies routinely defeat full-batch AST parsing (cursors, WHILE
blocks, TRY/CATCH), so the strategy is uniform per-statement processing:
split the body on ';' (string/comment aware), try sqlglot on each chunk,
and fall back to documented regex patterns for chunks it can't parse.
Dynamic SQL is never guessed at — it produces a warning instead.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from sqlglot import exp

from coop_data_doc.config import ParseWarning
from coop_data_doc.crawler import FileEntry
from coop_data_doc.graph.model import (
    Edge,
    EdgeType,
    LineageGraph,
    Node,
    NodeType,
    normalize_identifier,
)
from coop_data_doc.parsers.sql_common import (
    DYNAMIC_SQL_RE,
    EXEC_RE,
    PROC_HEADER_RE,
    StatementLineage,
    collect_source_tables,
    is_temp_table,
    original_name,
    parse_batch,
    qualify,
    regex_extract,
    scrub,
    split_batches,
    split_statements,
    table_parts,
)
from coop_data_doc.parsers.sql_objects import (
    columns_from_schema,
    read_sql_file,
    stub_table,
)

_AS_RE = re.compile(r"\bAS\b", re.IGNORECASE)
_BEGIN_END_LINE_RE = re.compile(r"^\s*(?:BEGIN|END)\s*;?\s*$", re.IGNORECASE | re.MULTILINE)

# statement types we trust the AST extraction for; anything else falls
# through to the regex layer
_USABLE = (exp.Insert, exp.Merge, exp.Update, exp.Delete, exp.Select, exp.Create, exp.Drop)


def _find_proc(batch: str) -> tuple[str, str] | None:
    """Return (qualified_name, body) if the batch creates a procedure."""
    header = PROC_HEADER_RE.search(batch)
    if not header:
        return None
    as_match = _AS_RE.search(batch, header.end())
    if not as_match:
        return None
    return header.group(1), batch[as_match.end() :]


def _alias_map(statement: exp.Expression) -> dict[str, tuple[str, str]]:
    aliases: dict[str, tuple[str, str]] = {}
    for table in statement.find_all(exp.Table):
        alias = normalize_identifier(table.alias or "")
        if alias and not is_temp_table(table):
            aliases[alias] = table_parts(table)
    return aliases


def _extract_statement(statement: exp.Expression) -> tuple[StatementLineage, list[exp.Create]]:
    """AST lineage for one parsed statement; also returns CREATE TABLEs."""
    lineage = StatementLineage()
    creates: list[exp.Create] = []

    if isinstance(statement, exp.Drop):
        return lineage, creates

    if isinstance(statement, exp.Create):
        if (statement.kind or "").upper() == "TABLE":
            creates.append(statement)
        return lineage, creates

    if isinstance(statement, exp.Insert):
        target = statement.this
        if isinstance(target, exp.Schema):
            target = target.this
        if isinstance(target, exp.Table) and not is_temp_table(target):
            lineage.writes.add(table_parts(target))
        if statement.expression is not None:
            lineage.reads |= collect_source_tables(statement.expression)

    elif isinstance(statement, exp.Merge):
        target = statement.this
        if isinstance(target, exp.Table) and not is_temp_table(target):
            lineage.writes.add(table_parts(target))
        lineage.reads |= collect_source_tables(statement)

    elif isinstance(statement, exp.Update):
        target = statement.this
        raw_target: tuple[str, str] | None = None
        if isinstance(target, exp.Table):
            raw_target = table_parts(target)
            schema, name = raw_target
            # UPDATE alias ... FROM real_table AS alias
            if not target.text("db"):
                resolved = _alias_map(statement).get(name)
                if resolved is not None:
                    schema, name = resolved
            if not is_temp_table(target):
                lineage.writes.add((schema, name))
        lineage.reads |= collect_source_tables(statement)
        if raw_target is not None:
            lineage.reads.discard(raw_target)  # the alias itself is not a table

    elif isinstance(statement, exp.Delete):
        target = statement.this
        if isinstance(target, exp.Table) and not is_temp_table(target):
            lineage.writes.add(table_parts(target))
        lineage.reads |= collect_source_tables(statement)

    elif isinstance(statement, exp.Select):
        into = statement.args.get("into")
        if into is not None:
            target = into.this
            if isinstance(target, exp.Table) and not is_temp_table(target):
                lineage.writes.add(table_parts(target))
        lineage.reads |= collect_source_tables(statement)

    lineage.reads -= lineage.writes
    return lineage, creates


def _apply_lineage(graph: LineageGraph, proc: Node, lineage: StatementLineage, evidence_file: str) -> None:
    for schema, name in sorted(lineage.writes):
        table = stub_table(graph, schema, name)
        graph.add_edge(
            Edge(
                source_id=proc.id,
                target_id=table.id,
                edge_type=EdgeType.WRITES,
                evidence=f"{evidence_file}: writes {schema}.{name}",
            )
        )
    for schema, name in sorted(lineage.reads):
        table = stub_table(graph, schema, name)
        graph.add_edge(
            Edge(
                source_id=proc.id,
                target_id=table.id,
                edge_type=EdgeType.READS,
                evidence=f"{evidence_file}: reads {schema}.{name}",
            )
        )


def parse_sql_procs(
    entries: list[FileEntry],
    graph: LineageGraph,
    dialect: str = "tsql",
    *,
    on_file: Callable[..., None] | None = None,
) -> list[ParseWarning]:
    """Extract stored_proc nodes and writes/reads/references/defines edges
    from CREATE PROCEDURE batches, statement by statement.

    ``on_file`` (optional) is called once per entry for progress reporting.
    """
    warnings: list[ParseWarning] = []
    for entry in entries:
        if on_file:
            on_file(entry.path)
        text = read_sql_file(entry)
        for batch in split_batches(text):
            found = _find_proc(batch)
            if found is None:
                continue
            raw_name, body = found
            schema, name = qualify(raw_name)
            proc = graph.add_node(
                Node(
                    id=Node.make_id(NodeType.STORED_PROC, schema, name),
                    node_type=NodeType.STORED_PROC,
                    name=name,
                    schema_name=schema,
                    display_name=original_name(raw_name),
                    source_file=entry.path,
                    source_code=batch,
                    metadata={"parse_quality": "ast"},
                )
            )
            if not proc.source_file:
                proc.source_file = entry.path
            fallback_used = False

            for chunk in split_statements(body):
                chunk = _BEGIN_END_LINE_RE.sub("", chunk).strip()
                if not chunk:
                    continue
                stripped = scrub(chunk, strip_strings=True)

                if DYNAMIC_SQL_RE.search(stripped):
                    # persistent marker so doc consumers (agents) can see
                    # this proc's lineage is knowingly incomplete — the
                    # ParseWarning alone only reaches the console
                    proc.metadata["dynamic_sql_untraced"] = True
                    warnings.append(
                        ParseWarning(
                            file=entry.path,
                            message=f"{schema}.{name}: dynamic SQL not traced",
                            category="dynamic_sql",
                        )
                    )
                    continue

                exec_match = EXEC_RE.match(stripped)
                if exec_match:
                    callee_schema, callee_name = qualify(exec_match.group(1))
                    callee = graph.add_node(
                        Node(
                            id=Node.make_id(NodeType.STORED_PROC, callee_schema, callee_name),
                            node_type=NodeType.STORED_PROC,
                            name=callee_name,
                            schema_name=callee_schema,
                            display_name=original_name(exec_match.group(1)),
                        )
                    )
                    graph.add_edge(
                        Edge(
                            source_id=proc.id,
                            target_id=callee.id,
                            edge_type=EdgeType.REFERENCES,
                            evidence=f"{entry.path}: EXEC {callee_schema}.{callee_name}",
                        )
                    )
                    continue

                usable = [
                    expression
                    for expression in parse_batch(chunk, dialect)
                    if isinstance(expression, _USABLE)
                ]
                if usable:
                    for statement in usable:
                        lineage, creates = _extract_statement(statement)
                        _apply_lineage(graph, proc, lineage, entry.path)
                        for create in creates:
                            target = create.this
                            schema_expr = target if isinstance(target, exp.Schema) else None
                            table_expr = target.this if schema_expr is not None else target
                            t_schema, t_name = table_parts(table_expr)
                            if is_temp_table(table_expr):
                                continue
                            table = graph.add_node(
                                Node(
                                    id=Node.make_id(NodeType.GOLD_TABLE, t_schema, t_name),
                                    node_type=NodeType.GOLD_TABLE,
                                    name=t_name,
                                    schema_name=t_schema,
                                    display_name=original_name(table_expr.name),
                                    source_file=entry.path,
                                    columns=(
                                        columns_from_schema(schema_expr, dialect)
                                        if schema_expr is not None
                                        else []
                                    ),
                                )
                            )
                            graph.add_edge(
                                Edge(
                                    source_id=proc.id,
                                    target_id=table.id,
                                    edge_type=EdgeType.DEFINES,
                                    evidence=f"{entry.path}: CREATE TABLE {t_schema}.{t_name}",
                                )
                            )
                else:
                    lineage = regex_extract(chunk)
                    if lineage.writes or lineage.reads:
                        fallback_used = True
                    _apply_lineage(graph, proc, lineage, entry.path)

            if fallback_used:
                proc.metadata["parse_quality"] = "regex_fallback"
                warnings.append(
                    ParseWarning(
                        file=entry.path,
                        message=f"{schema}.{name}: some statements traced via regex fallback",
                        category="regex_fallback",
                    )
                )
    return warnings


def resolve_stub_references(graph: LineageGraph) -> None:
    """Redirect edges pointing at gold_table stubs that are actually views.

    A view that reads another view creates a gold_table stub for it (the
    parser can't know the target's type at parse time); once everything is
    parsed, any definition-less stub whose qualified name matches a real
    view is folded into it.
    """
    for node_id in sorted(graph.nodes):
        node = graph.nodes.get(node_id)
        if node is None or node.node_type is not NodeType.GOLD_TABLE or node.source_file:
            continue
        qualified = node_id.split(":", 1)[1]
        view_id = f"{NodeType.VIEW.value}:{qualified}"
        if view_id not in graph.nodes:
            continue
        for edge in graph.edges:
            if edge.source_id == node_id:
                edge.source_id = view_id
            if edge.target_id == node_id:
                edge.target_id = view_id
        del graph.nodes[node_id]
    # edge rewrites can create duplicates; rebuild with the idempotent adder
    edges = graph.edges
    graph.edges = []
    for edge in edges:
        if edge.source_id != edge.target_id:
            graph.add_edge(edge)


def classify_silver(graph: LineageGraph) -> None:
    """Retype read-only, definition-less tables as silver-layer sources."""
    written = {
        edge.target_id for edge in graph.edges if edge.edge_type in (EdgeType.WRITES, EdgeType.DEFINES)
    }
    for node_id in sorted(graph.nodes):
        node = graph.nodes.get(node_id)
        if (
            node is not None
            and node.node_type is NodeType.GOLD_TABLE
            and not node.source_file
            and node_id not in written
        ):
            graph.retype_node(node_id, NodeType.SILVER_TABLE)
