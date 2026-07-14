"""Stored-procedure DML lineage extraction (Module 2).

T-SQL proc bodies routinely defeat full-batch AST parsing (cursors, WHILE
blocks, TRY/CATCH), so the strategy is uniform per-statement processing:
split the body on ';' (string/comment aware), try sqlglot on each chunk,
and fall back to documented regex patterns for chunks it can't parse.
Dynamic SQL is never guessed at — it produces a warning instead.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

import sqlglot
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
    EXEC_CALL_RE,
    EXEC_RE,
    PROC_HEADER_RE,
    StatementLineage,
    blank_comments_and_strings,
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
from coop_data_doc.parsers.parse_cache import ParseCache, cache_key
from coop_data_doc.parsers.sql_objects import (
    _add_edge,
    _add_node,
    _Contribution,
    _replay_entry,
    columns_from_schema,
    read_sql_file,
    stub_table,
)

_log = logging.getLogger("coop_data_doc")

_AS_RE = re.compile(r"\bAS\b", re.IGNORECASE)
# `EXECUTE AS <context>` (USER/LOGIN/CALLER/OWNER/SELF) is a security-context switch, not a
# proc call. EXEC_RE captures the first identifier after EXECUTE, which is the bare keyword
# `AS` here — reject it (and the context words, defensively) so no phantom proc node appears.
_EXEC_AS_KEYWORDS = {"AS", "USER", "LOGIN", "CALLER", "OWNER", "SELF"}
_BEGIN_END_LINE_RE = re.compile(r"^\s*(?:BEGIN|END)\s*;?\s*$", re.IGNORECASE | re.MULTILINE)

# statement types we trust the AST extraction for; anything else falls
# through to the regex layer
_USABLE = (exp.Insert, exp.Merge, exp.Update, exp.Delete, exp.Select, exp.Create, exp.Drop)


def _find_proc(batch: str) -> tuple[str, str] | None:
    """Return (qualified_name, body) if the batch creates a procedure.

    Header and body-AS detection run on a length-preserving scrub (comments
    and strings blanked to spaces), so a header named in a doc comment can't
    fake a proc and a paren inside a comment or string default can't corrupt
    the depth count; the body is then sliced from the RAW batch at the same
    offset (the scrub preserves offsets 1:1).
    """
    scrubbed = blank_comments_and_strings(batch)
    header = PROC_HEADER_RE.search(scrubbed)
    if not header:
        return None
    # The body-introducing `AS` must be at top level (paren depth 0); an `AS`
    # inside a parenthesized parameter default — e.g. `@D = CAST(GETDATE() AS
    # DATE)` — must not be mistaken for it, which would slice the body
    # mid-parameter-list and corrupt the first statement's lineage.
    as_match = _find_body_as(scrubbed, header.end())
    if not as_match:
        return None
    return header.group(1), batch[as_match:]


def _find_body_as(scrubbed: str, start: int) -> int | None:
    """Index just after the top-level (paren-depth 0) `AS` that introduces the
    proc body, or None. Tracks parenthesis depth so an `AS` inside a
    parenthesized parameter default is skipped. ``scrubbed`` must be
    comment/string-blanked text (blank_comments_and_strings) — counting parens
    on raw text lets `-- 1) rebuild` or N'(' corrupt the depth forever."""
    depth = 0
    for match in _AS_RE.finditer(scrubbed, start):
        depth += scrubbed.count("(", start, match.start()) - scrubbed.count(")", start, match.start())
        start = match.start()
        if depth == 0:
            return match.end()
    return None


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
        # Walk the WHOLE Insert, not just `.expression`: sqlglot hangs a
        # `WITH … INSERT INTO … SELECT` CTE block off the Insert node's `with_`
        # arg, so `.expression` (the inner SELECT) both misses the real base
        # tables inside the CTE bodies and leaks the CTE names as phantom
        # tables (collect_source_tables can only exclude CTE aliases it can see
        # on the node it's given). The write target is removed below via
        # `reads -= writes`, mirroring the Merge branch.
        lineage.reads |= collect_source_tables(statement)

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
        lineage.reads |= collect_source_tables(statement)
        # `DELETE alias FROM table AS alias …`: sqlglot puts the target
        # token(s) in the `tables` arg as bare Table nodes — resolve each
        # through the statement's aliases (mirroring the Update branch) and
        # drop the raw alias tuple from reads so `dbo.o` never becomes a
        # phantom node/edge. Plain `DELETE FROM t` has no `tables` arg and
        # writes `this` as before.
        targets = [t for t in statement.args.get("tables") or [] if isinstance(t, exp.Table)]
        if targets:
            aliases = _alias_map(statement)
            for target in targets:
                raw_target = table_parts(target)
                schema, name = raw_target
                if not target.text("db"):
                    resolved = aliases.get(name)
                    if resolved is not None:
                        schema, name = resolved
                if not is_temp_table(target):
                    lineage.writes.add((schema, name))
                lineage.reads.discard(raw_target)  # the alias itself is not a table
        else:
            target = statement.this
            if isinstance(target, exp.Table) and not is_temp_table(target):
                lineage.writes.add(table_parts(target))

    elif isinstance(statement, exp.Select):
        into = statement.args.get("into")
        if into is not None:
            target = into.this
            if isinstance(target, exp.Table) and not is_temp_table(target):
                lineage.writes.add(table_parts(target))
        lineage.reads |= collect_source_tables(statement)

    lineage.reads -= lineage.writes
    return lineage, creates


def _apply_lineage(
    graph: LineageGraph,
    proc: Node,
    lineage: StatementLineage,
    evidence_file: str,
    contribution: _Contribution | None = None,
) -> None:
    for schema, name in sorted(lineage.writes):
        table = stub_table(graph, schema, name, contribution)
        _add_edge(
            graph,
            Edge(
                source_id=proc.id,
                target_id=table.id,
                edge_type=EdgeType.WRITES,
                evidence=f"{evidence_file}: writes {schema}.{name}",
            ),
            contribution,
        )
    for schema, name in sorted(lineage.reads):
        table = stub_table(graph, schema, name, contribution)
        _add_edge(
            graph,
            Edge(
                source_id=proc.id,
                target_id=table.id,
                edge_type=EdgeType.READS,
                evidence=f"{evidence_file}: reads {schema}.{name}",
            ),
            contribution,
        )


def _parse_procs_entry(
    entry: FileEntry,
    text: str,
    graph: LineageGraph,
    dialect: str,
    warnings: list[ParseWarning],
    contribution: _Contribution | None,
) -> None:
    """Parse ONE SQL file's CREATE PROCEDURE batches into the graph. Split out
    from the entry loop so a cache MISS calls exactly this (recording the file's
    contribution) and a HIT replays the cached contribution instead."""
    for batch in split_batches(text):
        found = _find_proc(batch)
        if found is None:
            header = PROC_HEADER_RE.search(blank_comments_and_strings(batch))
            if header:
                # a real header with no top-level body AS (truncated file,
                # unsupported layout): skipping must be loud, never silent
                warning = ParseWarning(
                    file=entry.path,
                    message=(f"CREATE PROCEDURE {header.group(1)}: no body AS found — batch skipped"),
                    category="proc_body_not_found",
                )
                warnings.append(warning)
                if contribution is not None:
                    contribution.record_warning(warning)
            continue
        raw_name, body = found
        schema, name = qualify(raw_name)
        # Keep a handle on the PRISTINE node as passed to add_node: when an
        # earlier-sorted file already stubbed this proc (an EXEC callee), the
        # object add_node returns is the MERGED node carrying the stub's fields
        # (e.g. its display_name) — which must never enter this file's cache
        # entry (see the re-record at the end of this batch).
        pristine = Node(
            id=Node.make_id(NodeType.STORED_PROC, schema, name),
            node_type=NodeType.STORED_PROC,
            name=name,
            schema_name=schema,
            display_name=original_name(raw_name),
            source_file=entry.path,
            source_code=batch,
            metadata={"parse_quality": "ast"},
        )
        proc = _add_node(graph, pristine, contribution)
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
                warning = ParseWarning(
                    file=entry.path,
                    message=f"{schema}.{name}: dynamic SQL not traced",
                    category="dynamic_sql",
                )
                warnings.append(warning)
                if contribution is not None:
                    contribution.record_warning(warning)
                continue

            # Find EVERY EXEC callee in the chunk, not just one at the chunk start: a
            # conditional `IF <cond> EXEC x` shares its chunk with the IF line, and a
            # semicolon-less body can hold several EXECs. Reject `EXECUTE AS <ctx>`
            # (the captured callee is the bare keyword AS/USER/... — a phantom proc).
            exec_callees: list[str] = []
            non_exec_present = False
            for line in stripped.splitlines():
                if not line.strip():
                    continue
                m = EXEC_RE.match(line)
                if m is not None:
                    # a bare line-start EXEC statement: the callee (unless it's an
                    # EXECUTE AS context keyword) with no non-EXEC content to reparse.
                    if m.group(1).upper() not in _EXEC_AS_KEYWORDS:
                        exec_callees.append(m.group(1))
                    continue
                # Not a line-start EXEC: it may still carry a same-line conditional
                # EXEC (`IF ... EXEC x`, `ELSE EXEC y`, `WHILE ... EXEC z`). Collect
                # those callees; the leading condition/block is real non-EXEC text
                # (it can hold a subquery worth parsing), so this line stays
                # non_exec_present and the chunk still flows to sqlglot below.
                non_exec_present = True
                for match in EXEC_CALL_RE.finditer(line):
                    if match.group(1).upper() not in _EXEC_AS_KEYWORDS:
                        exec_callees.append(match.group(1))
            seen_callees: set[tuple[str, str]] = set()
            for raw_callee in exec_callees:
                callee_schema, callee_name = qualify(raw_callee)
                if (callee_schema, callee_name) in seen_callees:
                    continue
                seen_callees.add((callee_schema, callee_name))
                callee = _add_node(
                    graph,
                    Node(
                        id=Node.make_id(NodeType.STORED_PROC, callee_schema, callee_name),
                        node_type=NodeType.STORED_PROC,
                        name=callee_name,
                        schema_name=callee_schema,
                        display_name=original_name(raw_callee),
                    ),
                    contribution,
                )
                _add_edge(
                    graph,
                    Edge(
                        source_id=proc.id,
                        target_id=callee.id,
                        edge_type=EdgeType.REFERENCES,
                        evidence=f"{entry.path}: EXEC {callee_schema}.{callee_name}",
                    ),
                    contribution,
                )
            # Only skip sqlglot when the chunk was ENTIRELY EXEC statements; otherwise
            # let the non-EXEC text (e.g. an IF-condition subquery) flow on for lineage.
            if exec_callees and not non_exec_present:
                continue

            # RAISE, not IGNORE: a chunk sqlglot can't fully parse (e.g. a
            # legacy semicolon-less multi-statement body, which IGNORE
            # silently mangles into ONE statement, losing most lineage)
            # must yield [] so it flows to the regex fallback below and is
            # honestly flagged via parse_quality/regex_fallback.
            usable = [
                expression
                for expression in parse_batch(chunk, dialect, error_level=sqlglot.ErrorLevel.RAISE)
                if isinstance(expression, _USABLE)
            ]
            if usable:
                for statement in usable:
                    lineage, creates = _extract_statement(statement)
                    _apply_lineage(graph, proc, lineage, entry.path, contribution)
                    for create in creates:
                        target = create.this
                        schema_expr = target if isinstance(target, exp.Schema) else None
                        table_expr = target.this if schema_expr is not None else target
                        t_schema, t_name = table_parts(table_expr)
                        if is_temp_table(table_expr):
                            continue
                        table = _add_node(
                            graph,
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
                            ),
                            contribution,
                        )
                        _add_edge(
                            graph,
                            Edge(
                                source_id=proc.id,
                                target_id=table.id,
                                edge_type=EdgeType.DEFINES,
                                evidence=f"{entry.path}: CREATE TABLE {t_schema}.{t_name}",
                            ),
                            contribution,
                        )
            else:
                lineage = regex_extract(chunk)
                if lineage.writes or lineage.reads:
                    fallback_used = True
                _apply_lineage(graph, proc, lineage, entry.path, contribution)

        if fallback_used:
            proc.metadata["parse_quality"] = "regex_fallback"
            warning = ParseWarning(
                file=entry.path,
                message=f"{schema}.{name}: some statements traced via regex fallback",
                category="regex_fallback",
            )
            warnings.append(warning)
            if contribution is not None:
                contribution.record_warning(warning)

        # Re-record the proc node LAST so its cached dict carries the final
        # in-place metadata (dynamic_sql_untraced / parse_quality=regex_fallback)
        # applied during statement processing. Record the PRISTINE pre-merge
        # node — never `proc`, which may be a MERGED node carrying another
        # file's stub fields (_Contribution's contract: cache what was PASSED
        # to add_node, so replay-after-that-file-is-deleted equals a cold
        # parse). Only the proc's OWN metadata flags (set on the graph node
        # during statement processing) are copied across. The dict key already
        # exists, so this updates the value in place and keeps the proc first
        # in insertion order — the same order a cold parse adds it (before its
        # callees/tables).
        if contribution is not None:
            for flag in ("dynamic_sql_untraced", "parse_quality"):
                if flag in proc.metadata:
                    pristine.metadata[flag] = proc.metadata[flag]
            contribution.record_node(pristine)


def parse_sql_procs(
    entries: list[FileEntry],
    graph: LineageGraph,
    dialect: str = "tsql",
    *,
    on_file: Callable[..., None] | None = None,
    read_cache: dict[str, str] | None = None,
    parse_cache: ParseCache | None = None,
    no_parse_cache: bool = False,
) -> list[ParseWarning]:
    """Extract stored_proc nodes and writes/reads/references/defines edges
    from CREATE PROCEDURE batches, statement by statement.

    ``on_file`` (optional) is called once per entry for progress reporting.
    ``read_cache`` (optional) is shared with parse_sql_objects so each file is
    read + decoded exactly once across both passes (see read_sql_file).
    ``parse_cache``/``no_parse_cache`` (optional) skip re-parsing an unchanged
    file, replaying its cached contribution in sorted file order for a
    byte-identical graph (see parse_sql_objects and parse_cache.py). This pass
    does NOT touch the cache's hit/miss counters — the objects pass counts every
    file once (both passes share the same per-file key), so the CLI's summary
    reflects unique files, not passes.
    """
    warnings: list[ParseWarning] = []
    for entry in entries:
        if on_file:
            on_file(entry.path)
        # encoding problems are warned about once per file by parse_sql_objects
        # (the pipeline runs both parsers over the same entries), so no
        # warnings list is passed here — that would duplicate the diagnostic.
        text = read_sql_file(entry, None, read_cache)
        key = cache_key(dialect, text, "procs", entry.path) if parse_cache is not None else ""
        cached = None if (parse_cache is None or no_parse_cache) else parse_cache.get(key)
        if cached is not None:
            _replay_entry(graph, cached, warnings)
            _log.debug("parse cache hit (procs): %s", entry.path)
            continue
        contribution = _Contribution() if parse_cache is not None else None
        _parse_procs_entry(entry, text, graph, dialect, warnings, contribution)
        if parse_cache is not None:
            parse_cache.put(key, contribution.to_entry())
            _log.debug("parse cache miss (procs): %s", entry.path)
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
    # The in-place endpoint rewrite can create duplicate or self- edges; dedup in ONE O(E)
    # pass (keeping evidence backfill, dropping self-edges), then swap the list + index
    # atomically via replace_edges — the old rebuild-through-add_edge loop was O(E²).
    deduped: dict[tuple[str, str, str], Edge] = {}
    for edge in graph.edges:
        if edge.source_id == edge.target_id:
            continue
        kept = deduped.get(edge.key())
        if kept is None:
            deduped[edge.key()] = edge
        elif not kept.evidence and edge.evidence:
            kept.evidence = edge.evidence
    graph.replace_edges(list(deduped.values()))
