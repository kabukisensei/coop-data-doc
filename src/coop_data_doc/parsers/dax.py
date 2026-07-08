"""DAX measure dependency extraction (Module 3).

Heuristic by design: bare ``[Name]`` references are matched against the
model's known measure names after everything is loaded; ``Table[Column]``
references become table dependencies. Comments and string literals are
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
_QUOTED_TABLE_RE = re.compile(r"'([^']+)'\s*\[")
_BARE_TABLE_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\[")

# reserved words that can directly precede a bare '[Measure]' reference
# (`RETURN [X]`, `[a] IN [b]`…); they are never table names, so an alnum
# character before '[' must not reclassify the reference as Table[Column]
_DAX_KEYWORDS = frozenset({"return", "var", "in", "not", "and", "or"})


def _clean(dax: str) -> str:
    """Blank string literals (to '""') and remove comments via one character
    scanner. Strings are consumed FIRST — a regex pipeline that strips '//'
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
        tables.add(normalize_identifier(match.group(1)))
    for match in _BARE_TABLE_RE.finditer(text):
        name = normalize_identifier(match.group(1))
        if name not in _DAX_KEYWORDS:
            tables.add(name)
    return measures, tables


def link_measures(graph: LineageGraph, model_name: str) -> list[ParseWarning]:
    """Resolve each measure's DAX references within one semantic model."""
    warnings: list[ParseWarning] = []
    model_key = normalize_identifier(model_name)
    measures: dict[str, Node] = {}
    tables: dict[str, Node] = {}
    for node in graph.nodes.values():
        if node.schema_name != model_key:
            continue
        if node.node_type is NodeType.MEASURE:
            measures[normalize_identifier(node.name)] = node
        elif node.node_type is NodeType.PBI_TABLE:
            tables[normalize_identifier(node.name)] = node

    for key in sorted(measures):
        measure = measures[key]
        dax = measure.metadata.get("dax", "")
        measure.metadata["dax_refs_heuristic"] = True
        if not dax:
            continue
        candidate_measures, candidate_tables = extract_refs(dax)
        unmatched: list[str] = []
        for candidate in sorted(candidate_measures, key=str.lower):
            target = measures.get(normalize_identifier(candidate))
            if target is not None and target.id != measure.id:
                graph.add_edge(
                    Edge(
                        source_id=measure.id,
                        target_id=target.id,
                        edge_type=EdgeType.REFERENCES,
                        evidence=f"{measure.source_file}: DAX [{candidate}]",
                    )
                )
            elif target is None:
                unmatched.append(candidate)
        for candidate in sorted(candidate_tables):
            target = tables.get(candidate)
            if target is not None:
                graph.add_edge(
                    Edge(
                        source_id=measure.id,
                        target_id=target.id,
                        edge_type=EdgeType.REFERENCES,
                        evidence=f"{measure.source_file}: DAX {candidate}[...]",
                    )
                )
        if unmatched:
            measure.metadata["unmatched_dax_refs"] = sorted(unmatched)
    return warnings
