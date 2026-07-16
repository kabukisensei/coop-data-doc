"""CREATE TABLE / CREATE VIEW / inline TVF lineage extraction (Module 2)."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
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
from coop_data_doc.parsers.parse_cache import (
    ParseCache,
    ParseCacheEntry,
    cache_key,
)
from coop_data_doc.parsers.sql_common import (
    _IDENT,
    PROC_HEADER_RE,
    collect_source_tables,
    decode_text,
    original_name,
    parse_batch,
    qualify,
    regex_extract,
    scrub,
    split_batches,
    table_parts,
)

_log = logging.getLogger("coop_data_doc")

# Reuse the bracket-aware _IDENT so a spaced bracketed name like
# `[dbo].[Order Details]` is captured whole, not truncated at the first space.
_TABLE_FALLBACK_RE = re.compile(rf"\bCREATE\s+TABLE\s+({_IDENT})", re.IGNORECASE)
_VIEW_FALLBACK_RE = re.compile(rf"\bCREATE\s+(?:OR\s+ALTER\s+)?VIEW\s+({_IDENT})", re.IGNORECASE)


class _Contribution:
    """Per-file record of exactly what a parser added to the graph, for the
    parse cache. Records the node/edge *as passed to add_node/add_edge* — NOT
    the merged result — so a warm replay calls add_node/add_edge with the
    identical arguments a cold run passed, and the graph's deterministic merge
    logic reproduces byte-identical state. Nodes are keyed by id (later adds of
    the same id overwrite, mirroring the parser calling add_node repeatedly with
    the fuller node); edges are appended and deduped by key, in first-seen order.
    """

    __slots__ = ("nodes", "edges", "_edge_keys", "warnings")

    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []
        self._edge_keys: set[tuple[str, str, str]] = set()
        self.warnings: list[dict] = []

    def record_node(self, node: Node) -> None:
        # Node.source_code is exclude=True, so BOTH model_dump modes strip it —
        # add it back explicitly so the cached page keeps its Source section
        # (HAZARD B); _replay_entry re-validates the dict WITH source_code.
        if node.id in self.nodes:
            if self.nodes[node.id].get("source_file") and not node.source_file:
                # Do not let a read-stub overwrite a real definition from the same file.
                return
        dumped = node.model_dump(mode="python")
        dumped["source_code"] = node.source_code
        self.nodes[node.id] = dumped

    def record_edge(self, edge: Edge) -> None:
        key = edge.key()
        if key in self._edge_keys:
            return
        self._edge_keys.add(key)
        self.edges.append(edge.model_dump(mode="python"))

    def record_warning(self, warning: ParseWarning) -> None:
        self.warnings.append(warning.model_dump())

    def to_entry(self) -> ParseCacheEntry:
        # Sort edges by key so cache order matches the graph.json serialization
        # order; nodes stay in insertion order (deterministic, matches parse).
        edges = sorted(self.edges, key=lambda e: (e["source_id"], e["target_id"], e["edge_type"]))
        return ParseCacheEntry(nodes=dict(self.nodes), edges=edges, warnings=list(self.warnings))


def _add_node(graph: LineageGraph, node: Node, contribution: _Contribution | None) -> Node:
    """add_node, also recording the node in the file's contribution (pre-merge)."""
    if contribution is not None:
        contribution.record_node(node)
    return graph.add_node(node)


def _add_edge(graph: LineageGraph, edge: Edge, contribution: _Contribution | None) -> Edge:
    """add_edge, also recording the edge in the file's contribution."""
    if contribution is not None:
        contribution.record_edge(edge)
    return graph.add_edge(edge)


def _replay_entry(graph: LineageGraph, entry: ParseCacheEntry, warnings: list[ParseWarning]) -> None:
    """Replay a cached file contribution into the graph IN THE SAME ORDER as a
    cold parse: nodes (insertion order) then edges (sorted-by-key order), via the
    same add_node/add_edge the parser used, then append the file's warnings."""
    for node_dict in entry.nodes.values():
        graph.add_node(Node.model_validate(node_dict))
    for edge_dict in entry.edges:
        graph.add_edge(Edge.model_validate(edge_dict))
    for warning_dict in entry.warnings:
        warnings.append(ParseWarning.model_validate(warning_dict))


def read_sql_file(
    entry: FileEntry,
    warnings: list[ParseWarning] | None = None,
    cache: dict[str, str] | None = None,
) -> str:
    """Read a SQL file tolerantly (BOM-aware — UTF-8/16/32 — replacement on
    bad bytes; SSMS's 'Save with Encoding' Unicode default is UTF-16).

    If NULs survive decoding (BOM-less UTF-16, binary), no parser will extract
    anything from the text — so when a ``warnings`` list is given, an
    ``encoding_unreadable`` warning is appended: the file must never just
    silently contribute nothing. Only one caller per file should pass the
    list (the pipeline reads each file in both SQL parsers) to avoid
    duplicate diagnostics.

    ``cache`` (optional, keyed by ``entry.abs_path``) makes the pipeline read +
    decode each file exactly ONCE across both SQL parser passes: a cache HIT skips
    the disk read (and the already-emitted warning). Pass the same dict to both
    ``parse_sql_objects`` and ``parse_sql_procs``.
    """
    if cache is not None and entry.abs_path in cache:
        return cache[entry.abs_path]  # already read + decoded (+ warned) this run
    text = decode_text(Path(entry.abs_path).read_bytes())
    if cache is not None:
        cache[entry.abs_path] = text
    if warnings is not None and "\x00" in text:
        warnings.append(
            ParseWarning(
                file=entry.path,
                message=(
                    "file does not decode as UTF-8/UTF-16 text (NULs remain — likely "
                    "UTF-16 without BOM or binary); re-save as UTF-8, its objects are "
                    "not documented"
                ),
                category="encoding_unreadable",
            )
        )
    return text


def _stub_table_node(schema: str, name: str) -> Node:
    """The gold_table stub Node (unmerged) for a table referenced before/without DDL."""
    return Node(
        id=Node.make_id(NodeType.GOLD_TABLE, schema, name),
        node_type=NodeType.GOLD_TABLE,
        name=name,
        schema_name=schema,
    )


def stub_table(
    graph: LineageGraph, schema: str, name: str, contribution: _Contribution | None = None
) -> Node:
    """Get-or-create a table node referenced before (or without) its DDL."""
    return _add_node(graph, _stub_table_node(schema, name), contribution)


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
    graph: LineageGraph,
    reader_id: str,
    sources: set[tuple[str, str]],
    evidence_file: str,
    contribution: _Contribution | None = None,
) -> None:
    for schema, name in sorted(sources):
        source = _add_node(graph, _stub_table_node(schema, name), contribution)
        _add_edge(
            graph,
            Edge(
                source_id=reader_id,
                target_id=source.id,
                edge_type=EdgeType.READS,
                evidence=f"{evidence_file}: FROM {schema}.{name}",
            ),
            contribution,
        )


def _handle_create_table(
    create: exp.Create,
    graph: LineageGraph,
    entry: FileEntry,
    dialect: str,
    source: str,
    contribution: _Contribution | None = None,
) -> None:
    target = create.this
    schema_expr = None
    if isinstance(target, exp.Schema):
        schema_expr = target
        target = target.this
    schema, name = table_parts(target)
    columns = columns_from_schema(schema_expr, dialect) if schema_expr is not None else []
    # Build THIS file's complete node BEFORE add_node so the parse-cache records
    # a self-contained contribution (no merged-in state from earlier files). The
    # CTAS branch that used to mutate the returned (possibly-merged) node now
    # populates the local node instead; add_node's merge with any earlier node is
    # unchanged (existing columns win, new columns union), so graph state is
    # identical to before.
    node = Node(
        id=Node.make_id(NodeType.GOLD_TABLE, schema, name),
        node_type=NodeType.GOLD_TABLE,
        name=name,
        schema_name=schema,
        display_name=original_name(target.name),
        source_file=entry.path,
        source_code=source,
        columns=columns,
    )
    select = create.expression
    if isinstance(select, exp.Select) and not node.columns:  # CTAS without a column list
        derived, unresolved = _projection_columns(select)
        node.columns.extend(derived)
        if unresolved:
            node.metadata["columns_unresolved"] = True
        else:
            from coop_data_doc.parsers.sql_common import extract_column_lineage
            col_lineage = extract_column_lineage(select, dialect)
            if col_lineage:
                node.metadata["column_lineage"] = col_lineage
    _add_node(graph, node, contribution)
    if isinstance(select, exp.Select):  # CTAS
        _add_reads(graph, node.id, collect_source_tables(select), entry.path, contribution)


def _handle_create_view(
    create: exp.Create,
    graph: LineageGraph,
    entry: FileEntry,
    dialect: str,
    warnings: list[ParseWarning],
    source: str,
    contribution: _Contribution | None = None,
) -> None:
    schema, name = table_parts(create.this)
    # Fully populate the node (columns + columns_unresolved) BEFORE add_node so the
    # cached contribution is self-contained; see _handle_create_table.
    node = Node(
        id=Node.make_id(NodeType.VIEW, schema, name),
        node_type=NodeType.VIEW,
        name=name,
        schema_name=schema,
        display_name=original_name(create.this.name),
        source_file=entry.path,
        source_code=source,
    )
    select = create.expression
    view_warning: ParseWarning | None = None
    if isinstance(select, exp.Select):
        columns, unresolved = _projection_columns(select)
        node.columns.extend(columns)
        if unresolved:
            node.metadata["columns_unresolved"] = True
            view_warning = ParseWarning(
                file=entry.path,
                message=f"view {schema}.{name} uses SELECT * — output columns unresolved",
                category="select_star_view",
            )
        else:
            from coop_data_doc.parsers.sql_common import extract_column_lineage
            col_lineage = extract_column_lineage(select, dialect)
            if col_lineage:
                node.metadata["column_lineage"] = col_lineage
    _add_node(graph, node, contribution)
    if view_warning is not None:
        warnings.append(view_warning)
        if contribution is not None:
            contribution.record_warning(view_warning)
    if isinstance(select, exp.Select):
        _add_reads(graph, node.id, collect_source_tables(select), entry.path, contribution)


def _handle_create_function(
    create: exp.Create,
    graph: LineageGraph,
    entry: FileEntry,
    dialect: str,
    warnings: list[ParseWarning],
    source: str,
    contribution: _Contribution | None = None,
) -> bool:
    """An inline table-valued function (``CREATE FUNCTION … RETURNS TABLE AS
    RETURN SELECT …``) is a view-shaped object — a name, a column contract, and
    reads — so it becomes a ``view`` node tagged ``object_kind: inline_tvf``
    (issue #31). Returns False for anything else under CREATE FUNCTION (scalar
    functions, multi-statement TVFs — proc-shaped, out of scope); those files
    are caught by the ``sql_no_objects`` safety net instead of vanishing.
    """
    target = create.this
    if isinstance(target, exp.UserDefinedFunction):
        target = target.this
    if not isinstance(target, exp.Table):
        return False
    returned = create.expression
    if create.args.get("begin") or not isinstance(returned, exp.Return):
        return False
    select = returned.this
    if isinstance(select, exp.Subquery):
        select = select.this
    if not isinstance(select, exp.Select):
        return False
    schema, name = table_parts(target)
    node = Node(
        id=Node.make_id(NodeType.VIEW, schema, name),
        node_type=NodeType.VIEW,
        name=name,
        schema_name=schema,
        display_name=original_name(target.name),
        source_file=entry.path,
        source_code=source,
        metadata={"object_kind": "inline_tvf"},
    )
    columns, unresolved = _projection_columns(select)
    node.columns.extend(columns)
    fn_warning: ParseWarning | None = None
    if unresolved:
        node.metadata["columns_unresolved"] = True
        fn_warning = ParseWarning(
            file=entry.path,
            message=f"inline TVF {schema}.{name} uses SELECT * — output columns unresolved",
            category="select_star_view",
        )
    else:
        from coop_data_doc.parsers.sql_common import extract_column_lineage
        col_lineage = extract_column_lineage(select, dialect)
        if col_lineage:
            node.metadata["column_lineage"] = col_lineage
    _add_node(graph, node, contribution)
    if fn_warning is not None:
        warnings.append(fn_warning)
        if contribution is not None:
            contribution.record_warning(fn_warning)
    _add_reads(graph, node.id, collect_source_tables(select), entry.path, contribution)
    return True


def _parse_objects_entry(
    entry: FileEntry,
    text: str,
    graph: LineageGraph,
    dialect: str,
    warnings: list[ParseWarning],
    contribution: _Contribution | None,
) -> None:
    """Parse ONE SQL file's CREATE TABLE/VIEW batches into the graph. Split out
    from the entry loop so a cache MISS calls exactly this (recording the file's
    contribution), and a HIT replays the cached contribution instead."""
    for batch in split_batches(text):
        # Probe headers against comment/string-STRIPPED text so a name merely
        # *mentioned* in a `--`/`/* */` comment or a '...' string literal never
        # creates a phantom object (hard rule 4: never guess lineage). scrub preserves
        # identifiers + casing, so name extraction from the scrubbed match stays exact.
        scrubbed = scrub(batch, strip_strings=True)
        # a doc comment mentioning "CREATE PROCEDURE dbo.x" must not make us drop a
        # CREATE TABLE/VIEW batch as "handled by parse_sql_procs"
        if PROC_HEADER_RE.search(scrubbed):
            continue  # handled by parse_sql_procs
        creates = [
            expression
            for expression in parse_batch(batch, dialect)
            if isinstance(expression, exp.Create)
            and (expression.kind or "").upper() in ("TABLE", "VIEW", "FUNCTION")
        ]
        if creates:
            for create in creates:
                kind = (create.kind or "").upper()
                if kind == "TABLE":
                    _handle_create_table(create, graph, entry, dialect, batch, contribution)
                elif kind == "VIEW":
                    _handle_create_view(create, graph, entry, dialect, warnings, batch, contribution)
                else:  # FUNCTION — only inline TVFs produce a node (issue #31)
                    _handle_create_function(create, graph, entry, dialect, warnings, batch, contribution)
            continue
        # regex fallback for batches sqlglot couldn't parse — on the SCRUBBED text, so
        # a CREATE TABLE/VIEW mentioned only in a comment/string doesn't become a node.
        view_match = _VIEW_FALLBACK_RE.search(scrubbed)
        table_match = _TABLE_FALLBACK_RE.search(scrubbed)
        if not view_match and not table_match:
            continue
        lineage = regex_extract(batch)
        if view_match:
            raw = view_match.group(1)
            node_type = NodeType.VIEW
        else:
            raw = table_match.group(1)
            node_type = NodeType.GOLD_TABLE
        schema, name = qualify(raw)
        _add_node(
            graph,
            Node(
                id=Node.make_id(node_type, schema, name),
                node_type=node_type,
                name=name,
                schema_name=schema,
                display_name=original_name(raw),
                source_file=entry.path,
                metadata={"parse_quality": "regex_fallback"},
            ),
            contribution,
        )
        lineage.reads.discard((schema, name))
        _add_reads(graph, Node.make_id(node_type, schema, name), lineage.reads, entry.path, contribution)
        warning = ParseWarning(
            file=entry.path,
            message=f"{node_type.value} {schema}.{name} parsed via regex fallback",
            category="regex_fallback",
        )
        warnings.append(warning)
        if contribution is not None:
            contribution.record_warning(warning)


def flag_silent_sql_files(
    entries: list[FileEntry],
    graph: LineageGraph,
    warnings: list[ParseWarning],
) -> list[ParseWarning]:
    """Safety net (issue #31): a classified ``.sql`` file that contributed zero
    nodes and zero warnings across both SQL passes is invisible — unsupported
    DDL (scalar functions, multi-statement TVFs, GRANT scripts…) or an
    accidentally-empty file. Emit one ``sql_no_objects`` warning per such file
    so a coverage gap is never silent (hard rule 4).

    Runs AFTER both passes as a cross-file check (never inside the per-file
    parsers/parse cache — it depends on the whole run's outcome, and a derived
    deterministic post-pass keeps warm/parallel builds byte-identical for free).
    """
    documented = {node.source_file for node in graph.nodes.values() if node.source_file}
    warned = {warning.file for warning in warnings}
    return [
        ParseWarning(
            file=entry.path,
            message=(
                "no documentable objects found (no CREATE TABLE/VIEW/PROCEDURE "
                "or inline TVF, and no diagnostics) — the file contributes nothing"
            ),
            category="sql_no_objects",
        )
        for entry in entries
        if entry.path not in documented and entry.path not in warned
    ]


def parse_sql_objects(
    entries: list[FileEntry],
    graph: LineageGraph,
    dialect: str = "tsql",
    *,
    on_file: Callable[..., None] | None = None,
    read_cache: dict[str, str] | None = None,
    parse_cache: ParseCache | None = None,
    no_parse_cache: bool = False,
) -> list[ParseWarning]:
    """Extract gold_table/view nodes, column contracts, and reads edges
    from CREATE TABLE / CREATE VIEW batches; regex fallback when sqlglot
    can't parse, recorded as parse_quality metadata plus a warning.

    ``on_file`` (optional) is called once per entry for progress reporting.
    ``read_cache`` (optional) is shared with parse_sql_procs so each file is
    read + decoded exactly once across both passes (see read_sql_file).
    ``parse_cache`` (optional) skips re-parsing a file whose (version, dialect,
    content-hash) key already has a stored contribution: the cached nodes/edges/
    warnings are replayed via add_node/add_edge in the SAME sorted file order as
    a cold run, so the merged graph is byte-identical. ``no_parse_cache`` forces
    a cold parse (the cache is still repopulated for the next run). See
    parse_cache.py for the merge-order guarantee.
    """
    warnings: list[ParseWarning] = []
    for entry in entries:
        if on_file:
            on_file(entry.path)
        text = read_sql_file(entry, warnings, read_cache)
        key = cache_key(dialect, text, "objects", entry.path) if parse_cache is not None else ""
        cached = None if (parse_cache is None or no_parse_cache) else parse_cache.get(key)
        if cached is not None:
            _replay_entry(graph, cached, warnings)
            parse_cache.hits += 1
            _log.debug("parse cache hit (objects): %s", entry.path)
            continue
        contribution = _Contribution() if parse_cache is not None else None
        _parse_objects_entry(entry, text, graph, dialect, warnings, contribution)
        if parse_cache is not None:
            parse_cache.put(key, contribution.to_entry())
            parse_cache.misses += 1
            _log.debug("parse cache miss (objects): %s", entry.path)
    return warnings
