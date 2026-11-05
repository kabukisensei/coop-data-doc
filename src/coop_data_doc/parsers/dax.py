"""DAX measure dependency extraction (Module 3).

Heuristic by design: bare ``[Name]`` references are matched against the
model's known measure names after everything is loaded. A ``Table[Name]``
reference is a column of that table (a table dependency) unless the table has
no such column and the model has a measure of that name, which it then names. Comments and string literals are
removed before matching so they can never produce references.
"""

from __future__ import annotations

import re

from coop_data_doc.config import ParseWarning
from coop_data_doc.graph.model import (
    Edge,
    EdgeType,
    LineageGraph,
    Node,
    NodeType,
    normalize_identifier,
)

_BRACKET_RE = re.compile(r"\[([^\[\]]+)\]")
_QUOTED_TABLE_RE = re.compile(r"'((?:[^'\n]|'')+)'\s*\[")
_BARE_TABLE_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\[")
# one table-qualified member: 'Quoted Table'[Name] or Table[Name]
_QUALIFIED_RE = re.compile(r"(?:'((?:[^'\n]|'')+)'|\b([A-Za-z_][A-Za-z0-9_]*))\s*\[([^\[\]]+)\]")

# reserved words that can directly precede a bare '[Measure]' reference
# (`RETURN [X]`, `[a] IN [b]`…); they are never table names, so an alnum
# character before '[' must not reclassify the reference as Table[Column]
_DAX_KEYWORDS = frozenset({"return", "var", "in", "not", "and", "or"})


def _clean(dax: str) -> str:
    """Blank string literals (to '""') and remove comments via one character
    scanner. Quoted table names and bracketed names are copied verbatim, so
    '--' or '//' inside an identifier is never read as a comment. Strings are consumed FIRST — a regex pipeline that strips '//'
    before strings would truncate a URL literal at '//', unbalancing the
    quotes and flipping which text counts as code (losing real refs and
    leaking string contents as candidates). Doubled '""' escapes are honored.
    """
    out: list[str] = []
    i, n = 0, len(dax)
    while i < n:
        ch = dax[i]
        if ch == '"':
            j = i + 1
            while j < n:
                if dax[j] == '"':
                    if j + 1 < n and dax[j + 1] == '"':
                        j += 2
                        continue
                    break
                j += 1
            out.append('""')
            i = min(j + 1, n)
        elif ch in "'[":
            # A quoted table name ('North--South', doubled '' escape) or a bracketed
            # name ([a--b], doubled ]] escape) is an identifier, not code: copy it
            # verbatim so comment-like text inside it never starts a comment. An
            # unterminated one is left to the ordinary character path.
            close = "'" if ch == "'" else "]"
            j = i + 1
            while j < n:
                if dax[j] == close:
                    if j + 1 < n and dax[j + 1] == close:
                        j += 2
                        continue
                    break
                if dax[j] == "\n" or (close == "]" and dax[j] == "["):
                    j = n
                    break
                j += 1
            if j < n:
                out.append(dax[i : j + 1])
                i = j + 1
            else:
                out.append(ch)
                i += 1
        elif dax.startswith("//", i) or dax.startswith("--", i):
            j = dax.find("\n", i)
            i = n if j == -1 else j
        elif dax.startswith("/*", i):
            j = dax.find("*/", i)
            i = n if j == -1 else j + 2
            out.append(" ")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _word_before(text: str, end: int) -> str:
    """The identifier word ending at index ``end`` (inclusive), lowercased."""
    k = end
    while k >= 0 and (text[k].isalnum() or text[k] == "_"):
        k -= 1
    return text[k + 1 : end + 1].lower()


def extract_refs(dax: str) -> tuple[set[str], set[str]]:
    """Return (candidate measure names, referenced table names)."""
    text = _clean(dax)
    measures: set[str] = set()
    tables: set[str] = set()
    for match in _BRACKET_RE.finditer(text):
        j = match.start() - 1
        while j >= 0 and text[j] in " \t\r\n":
            j -= 1
        prev = text[j] if j >= 0 else ""
        # An identifier/quote immediately before '[' means Table[Column] — unless that
        # identifier is a DAX keyword (`RETURN [Total]`). ``prev`` must be non-empty for
        # this: at the START of the expression (or after only whitespace) there is nothing
        # before '[', so it is a bare [Measure] — and `"" in "_')]"` is True in Python, so
        # without the `bool(prev)` guard a leading `[Measure] * 1.1` was wrongly dropped as
        # a column ref (its target measure then looked "unused").
        is_column_ref = bool(prev) and (prev.isalnum() or prev in "_')]")
        if not is_column_ref or _word_before(text, j) in _DAX_KEYWORDS:
            measures.add(match.group(1).strip())
    for match in _QUOTED_TABLE_RE.finditer(text):
        tables.add(normalize_identifier(match.group(1).replace("''", "'")))
    for match in _BARE_TABLE_RE.finditer(text):
        name = normalize_identifier(match.group(1))
        if name not in _DAX_KEYWORDS:
            tables.add(name)
    return measures, tables


def extract_qualified_refs(dax: str) -> set[tuple[str, str]]:
    """Every table-qualified ``Table[Name]`` reference as (normalized table
    name, member name as written). DAX allows a measure to be qualified by a
    table (``'Measures'[Revenue]``), so the member is resolved against the
    model by the caller, not assumed to be a column (#94)."""
    text = _clean(dax)
    found: set[tuple[str, str]] = set()
    for match in _QUALIFIED_RE.finditer(text):
        if match.group(1) is not None:
            table = match.group(1).replace("''", "'")
        else:
            table = match.group(2)
            if table.lower() in _DAX_KEYWORDS:
                continue
        found.add((normalize_identifier(table), match.group(3).strip()))
    return found


def _qualified_measure(
    table_key: str,
    member: str,
    measures: dict[str, Node],
    tables: dict[str, Node],
) -> Node | None:
    """The measure a qualified ``Table[Name]`` names, or None when it is (or
    may be) a column. A column of that table wins; otherwise a model measure
    of that name is the member (measure names are model-unique, and DAX lets
    any table qualify one). An unknown table cannot be checked for the column,
    so it is never read as a measure (never guess)."""
    table = tables.get(table_key)
    if table is None:
        return None
    key = normalize_identifier(member)
    if any(normalize_identifier(column.name) == key for column in table.columns):
        return None
    return measures.get(key)


def _model_nodes(graph: LineageGraph, model_key: str) -> tuple[dict[str, Node], dict[str, Node]]:
    """One model's (measures, tables), keyed by normalized name."""
    measures: dict[str, Node] = {}
    tables: dict[str, Node] = {}
    for node in graph.nodes.values():
        if node.schema_name != model_key:
            continue
        if node.node_type is NodeType.MEASURE:
            measures[normalize_identifier(node.name)] = node
        elif node.node_type is NodeType.PBI_TABLE:
            tables[normalize_identifier(node.name)] = node
    return measures, tables


def _resolve_dax_refs(
    graph: LineageGraph,
    node: Node,
    dax: str,
    measures: dict[str, Node],
    tables: dict[str, Node],
) -> None:
    """Wire one node's DAX references into ``references`` edges (heuristic —
    the node gets the ``dax_refs_heuristic`` trust marker, and candidates that
    match no known measure land in ``unmatched_dax_refs``)."""
    node.metadata["dax_refs_heuristic"] = True
    if not dax:
        return
    candidate_measures, candidate_tables = extract_refs(dax)
    # Table-qualified members that name a measure (#94): the reference is to
    # that measure, so its table is a dependency only when another qualified
    # reference to it is a column.
    qualified_measures: list[tuple[str, str, Node]] = []
    column_tables: set[str] = set()
    for table_key, member in sorted(extract_qualified_refs(dax)):
        target = _qualified_measure(table_key, member, measures, tables)
        if target is None:
            column_tables.add(table_key)
        else:
            qualified_measures.append((table_key, member, target))
    measure_only_tables = {table_key for table_key, _, _ in qualified_measures} - column_tables
    candidate_tables = candidate_tables - measure_only_tables
    unmatched: list[str] = []
    for candidate in sorted(candidate_measures, key=str.lower):
        target = measures.get(normalize_identifier(candidate))
        if target is not None and target.id != node.id:
            graph.add_edge(
                Edge(
                    source_id=node.id,
                    target_id=target.id,
                    edge_type=EdgeType.REFERENCES,
                    evidence=f"{node.source_file}: DAX [{candidate}]",
                )
            )
        elif target is None:
            unmatched.append(candidate)
    for table_key, member, target in qualified_measures:
        if target.id != node.id:
            graph.add_edge(
                Edge(
                    source_id=node.id,
                    target_id=target.id,
                    edge_type=EdgeType.REFERENCES,
                    evidence=f"{node.source_file}: DAX {table_key}[{member}]",
                )
            )
    for candidate in sorted(candidate_tables):
        target = tables.get(candidate)
        if target is not None and target.id != node.id:
            graph.add_edge(
                Edge(
                    source_id=node.id,
                    target_id=target.id,
                    edge_type=EdgeType.REFERENCES,
                    evidence=f"{node.source_file}: DAX {candidate}[...]",
                )
            )
    if unmatched:
        node.metadata["unmatched_dax_refs"] = sorted(unmatched)


def link_measures(graph: LineageGraph, model_name: str) -> list[ParseWarning]:
    """Resolve each measure's DAX references within one semantic model."""
    warnings: list[ParseWarning] = []
    model_key = normalize_identifier(model_name)
    measures, tables = _model_nodes(graph, model_key)
    for key in sorted(measures):
        measure = measures[key]
        _resolve_dax_refs(graph, measure, measure.metadata.get("dax", ""), measures, tables)
    return warnings


def link_calculated_tables(graph: LineageGraph, model_name: str) -> None:
    """Resolve each calculated table's DAX references within one semantic model.

    A ``partition X = calculated`` table (TMDL) or ``source.type ==
    "calculated"`` partition (BIM) derives from other tables/measures in the
    same model; its expression goes through the same heuristic as measure DAX
    (issue #30), producing ``references`` edges from the table to what it
    derives from.
    """
    model_key = normalize_identifier(model_name)
    measures, tables = _model_nodes(graph, model_key)
    for key in sorted(tables):
        table = tables[key]
        if not table.metadata.get("partition_calculated"):
            continue
        _resolve_dax_refs(graph, table, table.metadata.get("calculated_dax", ""), measures, tables)
