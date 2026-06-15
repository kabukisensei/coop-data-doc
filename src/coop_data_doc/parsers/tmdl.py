"""TMDL semantic-model parsing (Module 3).

TMDL is an indentation-scoped, line-oriented format. This is deliberately a
tolerant line parser, not a grammar: it tracks table / column / measure /
partition headers and their property lines, and skips anything it doesn't
recognize. Malformed input warns; it never raises.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path, PurePosixPath

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
from coop_data_doc.parsers.dax import link_measures
from coop_data_doc.parsers.mcode import extract_source
from coop_data_doc.parsers.sql_common import collect_source_tables, parse_batch

_TABLE_RE = re.compile(r"^table\s+(.+?)\s*$")
_MEASURE_RE = re.compile(r"^measure\s+('[^']*'|\"[^\"]*\"|\S+)\s*=\s*(.*)$")
_COLUMN_RE = re.compile(r"^column\s+('[^']*'|\"[^\"]*\"|\S+)\s*$")
_DATATYPE_RE = re.compile(r"^dataType\s*:\s*(\S+)")
_PARTITION_RE = re.compile(r"^partition\s+(.+?)\s*=\s*(\w+)\s*$")
_SOURCE_RE = re.compile(r"^source\s*=\s*(.*)$")
_RELATIONSHIP_RE = re.compile(r"^relationship\s+(\S+)")
_FROM_COLUMN_RE = re.compile(r"^fromColumn\s*:\s*(.+?)\s*$")
_TO_COLUMN_RE = re.compile(r"^toColumn\s*:\s*(.+?)\s*$")
_PROPERTY_RE = re.compile(r"^[A-Za-z][\w]*\s*:")


def _unquote(name: str) -> str:
    name = name.strip()
    if len(name) >= 2 and name[0] == name[-1] and name[0] in "'\"":
        return name[1:-1]
    return name


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def model_root(path: str) -> tuple[str, str]:
    """(root_prefix, model_name) for a TMDL file path.

    'SalesPM.SemanticModel/definition/tables/x.tmdl' -> root is the
    .SemanticModel folder, model name 'SalesPM'.
    """
    parts = PurePosixPath(path).parts
    for index, part in enumerate(parts):
        if part.lower().endswith(".semanticmodel"):
            return "/".join(parts[: index + 1]), part[: -len(".SemanticModel")]
    if "definition" in parts:
        index = parts.index("definition")
        if index > 0:
            return "/".join(parts[:index]), parts[index - 1]
    return parts[0] if len(parts) > 1 else "", parts[0].rsplit(".", 1)[0]


def _attach_partition_source(
    table_node: Node, m_text: str, source_file: str, warnings: list[ParseWarning]
) -> None:
    ref, native_sql = extract_source(m_text)
    if native_sql:
        tables: set[tuple[str, str]] = set()
        for sql in native_sql:
            for statement in parse_batch(sql):
                tables |= collect_source_tables(statement)
        table_node.metadata["native_query_tables"] = sorted(f"{schema}.{name}" for schema, name in tables)
        if len(tables) == 1:
            schema, name = next(iter(tables))
            table_node.metadata["partition_source"] = {
                "schema": schema,
                "object": name,
                "raw_kind": "native_query",
            }
            return
    if ref is not None and ref.raw_kind == "static":
        # inline/calculation/parameter table — no database lineage by design
        table_node.metadata["partition_static"] = True
        return
    if ref is not None and ref.raw_kind != "native_query":
        table_node.metadata["partition_source"] = {
            "schema": ref.schema_name,
            "object": ref.object_name,
            "raw_kind": ref.raw_kind,
        }
        return
    if ref is not None and ref.raw_kind == "native_query":
        return  # tables recorded above; multiple sources resolved by linker
    table_node.metadata["partition_source_unresolved"] = True
    warnings.append(
        ParseWarning(
            file=source_file,
            message=f"partition source of {table_node.name} not recognized",
            category="unresolved_partition_source",
        )
    )


def parse_table_file(
    text: str, model_name: str, model_id: str, entry: FileEntry, graph: LineageGraph
) -> list[ParseWarning]:
    """Parse one .tmdl file's table/column/measure/partition blocks into
    graph nodes and feeds edges.
    """
    warnings: list[ParseWarning] = []
    model_key = normalize_identifier(model_name)
    lines = text.splitlines()
    table_node: Node | None = None
    current_column: Column | None = None
    pending_doc: list[str] = []  # accumulates `///` doc-comment lines

    def take_doc() -> str:
        """Cleaned description from the doc-comment lines above an object,
        skipping the 'TODO: Add description' placeholder. Resets the buffer."""
        text_ = " ".join(pending_doc).strip()
        pending_doc.clear()
        return "" if text_.upper().startswith("TODO") else text_

    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        if not stripped:
            pending_doc.clear()  # a blank line breaks doc-comment adjacency
            i += 1
            continue
        if stripped.startswith("///"):
            pending_doc.append(stripped[3:].strip())
            i += 1
            continue
        indent = _indent(raw)

        if indent == 0:
            table_match = _TABLE_RE.match(stripped)
            if table_match:
                name = _unquote(table_match.group(1))
                table_desc = take_doc()
                table_node = graph.add_node(
                    Node(
                        id=Node.make_id(NodeType.PBI_TABLE, model_name, name),
                        node_type=NodeType.PBI_TABLE,
                        name=normalize_identifier(name),
                        schema_name=model_key,
                        display_name=name,
                        source_file=entry.path,
                        metadata={"description": table_desc} if table_desc else {},
                    )
                )
                graph.add_edge(
                    Edge(
                        source_id=table_node.id,
                        target_id=model_id,
                        edge_type=EdgeType.FEEDS,
                        evidence=entry.path,
                    )
                )
                current_column = None
                i += 1
                continue

        if table_node is None:
            i += 1
            continue

        measure_match = _MEASURE_RE.match(stripped)
        if measure_match:
            current_column = None
            measure_name = _unquote(measure_match.group(1))
            measure_desc = take_doc()
            dax_parts = [measure_match.group(2)] if measure_match.group(2) else []
            i += 1
            while i < len(lines):
                nxt = lines[i]
                inner = nxt.strip()
                if inner and (_indent(nxt) <= indent or _PROPERTY_RE.match(inner)):
                    break
                if inner:
                    dax_parts.append(inner)
                i += 1
            measure = graph.add_node(
                Node(
                    id=Node.make_id(NodeType.MEASURE, model_name, measure_name),
                    node_type=NodeType.MEASURE,
                    name=normalize_identifier(measure_name),
                    schema_name=model_key,
                    display_name=measure_name,
                    source_file=entry.path,
                    metadata=(
                        {"dax": "\n".join(dax_parts).strip(), "description": measure_desc}
                        if measure_desc
                        else {"dax": "\n".join(dax_parts).strip()}
                    ),
                )
            )
            graph.add_edge(
                Edge(
                    source_id=measure.id,
                    target_id=model_id,
                    edge_type=EdgeType.FEEDS,
                    evidence=entry.path,
                )
            )
            continue

        column_match = _COLUMN_RE.match(stripped)
        if column_match:
            name = normalize_identifier(_unquote(column_match.group(1)))
            current_column = Column(name=name, description=take_doc())
            known = {c.name for c in table_node.columns}
            if name not in known:
                table_node.columns.append(current_column)
            i += 1
            continue

        datatype_match = _DATATYPE_RE.match(stripped)
        if datatype_match and current_column is not None:
            current_column.data_type = datatype_match.group(1)
            i += 1
            continue

        partition_match = _PARTITION_RE.match(stripped)
        if partition_match:
            current_column = None
            m_parts: list[str] = []
            i += 1
            while i < len(lines):
                nxt = lines[i]
                inner = nxt.strip()
                if inner and _indent(nxt) <= indent:
                    break
                source_match = _SOURCE_RE.match(inner) if inner else None
                if source_match:
                    source_indent = _indent(nxt)
                    if source_match.group(1):
                        m_parts.append(source_match.group(1))
                    i += 1
                    while i < len(lines):
                        deeper = lines[i]
                        deeper_inner = deeper.strip()
                        if deeper_inner and _indent(deeper) <= source_indent:
                            break
                        if deeper_inner:
                            m_parts.append(deeper_inner)
                        i += 1
                    continue
                i += 1
            if partition_match.group(2).lower() == "m":
                _attach_partition_source(table_node, "\n".join(m_parts), entry.path, warnings)
            pending_doc.clear()
            continue

        # any other line (property, hierarchy, level, relationship…) breaks
        # the adjacency between a `///` block and the object it documents
        pending_doc.clear()
        i += 1
    return warnings


def parse_model_file(text: str, model_node: Node) -> None:
    """Collect relationship blocks from model.tmdl onto the model node."""
    relationships: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw in text.splitlines():
        stripped = raw.strip()
        if _RELATIONSHIP_RE.match(stripped) and _indent(raw) == 0:
            current = {}
            relationships.append(current)
            continue
        if current is None:
            continue
        if _indent(raw) == 0 and stripped:
            current = None
            continue
        from_match = _FROM_COLUMN_RE.match(stripped)
        if from_match:
            current["from"] = normalize_identifier(from_match.group(1))
        to_match = _TO_COLUMN_RE.match(stripped)
        if to_match:
            current["to"] = normalize_identifier(to_match.group(1))
    complete = sorted(
        (r for r in relationships if "from" in r and "to" in r),
        key=lambda r: (r["from"], r["to"]),
    )
    if complete:
        existing = model_node.metadata.get("relationships", [])
        merged = {(r["from"], r["to"]) for r in existing} | {(r["from"], r["to"]) for r in complete}
        model_node.metadata["relationships"] = [{"from": pair[0], "to": pair[1]} for pair in sorted(merged)]


def parse_tmdl(
    entries: list[FileEntry],
    graph: LineageGraph,
    *,
    on_file: Callable[..., None] | None = None,
) -> list[ParseWarning]:
    """Group TMDL files by semantic model, parse each, and resolve DAX
    measure dependencies per model.

    ``on_file`` (optional) is called once per entry for progress reporting.
    """
    warnings: list[ParseWarning] = []
    groups: dict[tuple[str, str, str], list[FileEntry]] = {}
    for entry in entries:
        root, model_name = model_root(entry.path)
        groups.setdefault((entry.repo_key, root, model_name), []).append(entry)

    for (_, _, model_name), files in sorted(groups.items()):
        model_node = graph.add_node(
            Node(
                id=Node.make_id(NodeType.SEMANTIC_MODEL, "", model_name),
                node_type=NodeType.SEMANTIC_MODEL,
                name=normalize_identifier(model_name),
                display_name=model_name,
            )
        )
        for entry in sorted(files, key=lambda e: e.path):
            if on_file:
                on_file(entry.path)
            try:
                text = _read(entry)
            except OSError as exc:
                warnings.append(ParseWarning(file=entry.path, message=str(exc), category="tmdl_parse"))
                continue
            basename = PurePosixPath(entry.path).name.lower()
            if basename == "model.tmdl":
                parse_model_file(text, model_node)
                if not model_node.source_file:
                    model_node.source_file = entry.path
            warnings += parse_table_file(text, model_name, model_node.id, entry, graph)
        link_measures(graph, model_name)
    return warnings


def _read(entry: FileEntry) -> str:
    return Path(entry.abs_path).read_text(encoding="utf-8-sig", errors="replace")
