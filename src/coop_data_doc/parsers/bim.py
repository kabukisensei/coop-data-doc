"""model.bim (Tabular Object Model JSON) parsing (Module 3).

Same outputs as the TMDL parser, sourced from the legacy JSON model format.
"""

from __future__ import annotations

import json
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
from coop_data_doc.parsers.tmdl import _attach_partition_source


def _expression_text(expression) -> str:
    if isinstance(expression, list):
        return "\n".join(str(part) for part in expression)
    return str(expression or "")


def parse_bim(
    entries: list[FileEntry],
    graph: LineageGraph,
    *,
    on_file: Callable[..., None] | None = None,
) -> list[ParseWarning]:
    """Extract semantic-model nodes/edges from model.bim JSON files.

    ``on_file`` (optional) is called once per entry for progress reporting.
    """
    warnings: list[ParseWarning] = []
    for entry in entries:
        if on_file:
            on_file(entry.path)
        try:
            data = json.loads(Path(entry.abs_path).read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(ParseWarning(file=entry.path, message=str(exc), category="bim_parse"))
            continue
        model = data.get("model") or {}
        model_name = data.get("name") or PurePosixPath(entry.path).stem
        model_key = normalize_identifier(model_name)
        model_node = graph.add_node(
            Node(
                id=Node.make_id(NodeType.SEMANTIC_MODEL, "", model_name),
                node_type=NodeType.SEMANTIC_MODEL,
                name=model_key,
                source_file=entry.path,
            )
        )

        for table in model.get("tables") or []:
            table_name = table.get("name") or ""
            if not table_name:
                continue
            columns = [
                Column(
                    name=normalize_identifier(column.get("name") or ""),
                    data_type=str(column.get("dataType") or ""),
                )
                for column in table.get("columns") or []
                if column.get("name")
            ]
            table_node = graph.add_node(
                Node(
                    id=Node.make_id(NodeType.PBI_TABLE, model_name, table_name),
                    node_type=NodeType.PBI_TABLE,
                    name=normalize_identifier(table_name),
                    schema_name=model_key,
                    source_file=entry.path,
                    columns=columns,
                )
            )
            graph.add_edge(
                Edge(
                    source_id=table_node.id,
                    target_id=model_node.id,
                    edge_type=EdgeType.FEEDS,
                    evidence=entry.path,
                )
            )
            for partition in table.get("partitions") or []:
                source = partition.get("source") or {}
                expression = _expression_text(source.get("expression"))
                if source.get("type") in (None, "m") and expression:
                    _attach_partition_source(table_node, expression, entry.path, warnings)
            for measure in table.get("measures") or []:
                measure_name = measure.get("name") or ""
                if not measure_name:
                    continue
                measure_node = graph.add_node(
                    Node(
                        id=Node.make_id(NodeType.MEASURE, model_name, measure_name),
                        node_type=NodeType.MEASURE,
                        name=normalize_identifier(measure_name),
                        schema_name=model_key,
                        source_file=entry.path,
                        metadata={"dax": _expression_text(measure.get("expression"))},
                    )
                )
                graph.add_edge(
                    Edge(
                        source_id=measure_node.id,
                        target_id=model_node.id,
                        edge_type=EdgeType.FEEDS,
                        evidence=entry.path,
                    )
                )

        relationships = sorted(
            {
                (
                    normalize_identifier(f"{r.get('fromTable', '')}.{r.get('fromColumn', '')}"),
                    normalize_identifier(f"{r.get('toTable', '')}.{r.get('toColumn', '')}"),
                )
                for r in model.get("relationships") or []
                if r.get("fromTable") and r.get("toTable")
            }
        )
        if relationships:
            existing = {(r["from"], r["to"]) for r in model_node.metadata.get("relationships", [])}
            merged = sorted(existing | set(relationships))
            model_node.metadata["relationships"] = [{"from": pair[0], "to": pair[1]} for pair in merged]
        link_measures(graph, model_name)
    return warnings
