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

_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT_RE = re.compile(r"(?://|--)[^\n]*")
_STRING_RE = re.compile(r'"[^"]*"')
_BRACKET_RE = re.compile(r"\[([^\[\]]+)\]")
_QUOTED_TABLE_RE = re.compile(r"'([^']+)'\s*\[")
_BARE_TABLE_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\[")


def _clean(dax: str) -> str:
    text = _BLOCK_COMMENT_RE.sub(" ", dax)
    text = _LINE_COMMENT_RE.sub("", text)
    return _STRING_RE.sub('""', text)


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
        # an identifier/quote immediately before '[' means Table[Column]
        if not (prev.isalnum() or prev in "_')]"):
            measures.add(match.group(1).strip())
    for match in _QUOTED_TABLE_RE.finditer(text):
        tables.add(normalize_identifier(match.group(1)))
    for match in _BARE_TABLE_RE.finditer(text):
        tables.add(normalize_identifier(match.group(1)))
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
