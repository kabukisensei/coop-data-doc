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
    merge_relationships,
    normalize_identifier,
)
from coop_data_doc.parsers.dax import link_calculated_tables, link_measures
from coop_data_doc.parsers.tmdl import (
    _attach_native_sql,
    _attach_partition_source,
    _mark_partition_unresolved,
)


def _expression_text(expression) -> str:
    if isinstance(expression, list):
        return "\n".join(str(part) for part in expression)
    return str(expression or "")


def _dict_entries(value, label: str, file: str, warnings: list[ParseWarning]) -> list[dict]:
    """The dict entries of a parsed-JSON .bim collection. A malformed shape (a
    non-list collection, a non-dict entry) degrades to a ``bim_parse`` warning
    and is skipped — the parser never raises on malformed input (hard rule 3,
    issue #41)."""
    if not value:
        return []
    if not isinstance(value, list):
        warnings.append(
            ParseWarning(file=file, message=f"{label} is not a JSON array — skipped", category="bim_parse")
        )
        return []
    out: list[dict] = []
    for index, item in enumerate(value):
        if isinstance(item, dict):
            out.append(item)
        else:
            warnings.append(
                ParseWarning(
                    file=file,
                    message=f"{label}[{index}] is not a JSON object — skipped",
                    category="bim_parse",
                )
            )
    return out


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
        # valid JSON but not a TOM object (e.g. a top-level array) — degrade to
        # a warning and skip, never raise (hard rule 3, issue #41)
        if not isinstance(data, dict):
            warnings.append(
                ParseWarning(
                    file=entry.path,
                    message=f"{PurePosixPath(entry.path).name} is not a JSON object",
                    category="bim_parse",
                )
            )
            continue
        model = data.get("model") or {}
        if not isinstance(model, dict):
            warnings.append(
                ParseWarning(
                    file=entry.path, message="'model' is not a JSON object — skipped", category="bim_parse"
                )
            )
            model = {}
        model_name = data.get("name")
        if not isinstance(model_name, str) or not model_name:
            model_name = PurePosixPath(entry.path).stem
        model_key = normalize_identifier(model_name)
        model_node = graph.add_node(
            Node(
                id=Node.make_id(NodeType.SEMANTIC_MODEL, "", model_name),
                node_type=NodeType.SEMANTIC_MODEL,
                name=model_key,
                display_name=model_name,
                source_file=entry.path,
            )
        )

        for table in _dict_entries(model.get("tables"), "model.tables", entry.path, warnings):
            table_name = table.get("name")
            if not table_name or not isinstance(table_name, str):
                continue
            # non-dict column entries are the same malformed-shape class as the
            # collections above; they carry no name to warn about, so skip quietly
            table_columns = [c for c in table.get("columns") or [] if isinstance(c, dict)]
            columns = [
                Column(
                    name=normalize_identifier(column.get("name") or ""),
                    data_type=str(column.get("dataType") or ""),
                    description=str(column.get("description") or ""),
                )
                for column in table_columns
                if column.get("name")
            ]
            table_desc = str(table.get("description") or "")
            table_node = graph.add_node(
                Node(
                    id=Node.make_id(NodeType.PBI_TABLE, model_name, table_name),
                    node_type=NodeType.PBI_TABLE,
                    name=normalize_identifier(table_name),
                    schema_name=model_key,
                    display_name=table_name,
                    source_file=entry.path,
                    columns=columns,
                    metadata={"description": table_desc} if table_desc else {},
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
            for partition in _dict_entries(
                table.get("partitions"), f"table '{table_name}' partitions", entry.path, warnings
            ):
                if partition.get("mode"):
                    table_node.metadata["storage_mode"] = str(partition["mode"]).lower()
                # a non-dict source is unusable: coerce to {} so the partition
                # falls through to _mark_partition_unresolved (which warns)
                source = partition.get("source") or {}
                if not isinstance(source, dict):
                    source = {}
                source_type = str(source.get("type") or "m").lower()  # TOM default is "m"
                expression = _expression_text(source.get("expression"))
                if source_type == "m" and expression:
                    _attach_partition_source(table_node, expression, entry.path, warnings)
                elif source_type == "calculated" and expression:
                    # a DAX calculated table: its references are resolved against
                    # the whole model by link_calculated_tables (issue #30)
                    table_node.metadata["partition_calculated"] = True
                    table_node.metadata["calculated_dax"] = expression
                elif source_type == "query" and (sql := _expression_text(source.get("query")) or expression):
                    # legacy provider partition: the query is native SQL — same
                    # lineage extraction as Value.NativeQuery (issue #30)
                    _attach_native_sql(table_node, [sql], entry.path, warnings)
                elif source_type == "calculationgroup":
                    # a calculation group's partition has no data source by design
                    table_node.metadata["partition_static"] = True
                else:
                    # any other partition flavor (entity, inferred, an empty
                    # expression…) is untraceable — never silently healthy
                    # (hard rule 4, issue #30)
                    _mark_partition_unresolved(
                        table_node, f"source type '{source_type}'", entry.path, warnings
                    )
            table_measures = _dict_entries(
                table.get("measures"), f"table '{table_name}' measures", entry.path, warnings
            )
            # a table hosting measures but with no visible data columns is a
            # "measure home table" (issue #27) — parity with the TMDL parser.
            has_measure = any(m.get("name") for m in table_measures)
            has_data_column = any(c.get("name") and not c.get("isHidden") for c in table_columns)
            if has_measure and not has_data_column:
                table_node.metadata["measure_table"] = True
            for measure in table_measures:
                measure_name = measure.get("name") or ""
                if not measure_name or not isinstance(measure_name, str):
                    continue
                measure_node = graph.add_node(
                    Node(
                        id=Node.make_id(NodeType.MEASURE, model_name, measure_name),
                        node_type=NodeType.MEASURE,
                        name=normalize_identifier(measure_name),
                        schema_name=model_key,
                        display_name=measure_name,
                        source_file=entry.path,
                        metadata=(
                            {
                                "dax": _expression_text(measure.get("expression")),
                                "description": str(measure.get("description") or ""),
                            }
                            if measure.get("description")
                            else {"dax": _expression_text(measure.get("expression"))}
                        ),
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

        relationships = [
            {
                "from": normalize_identifier(f"{r.get('fromTable', '')}.{r.get('fromColumn', '')}"),
                "to": normalize_identifier(f"{r.get('toTable', '')}.{r.get('toColumn', '')}"),
                # BIM omits isActive when true; crossFilteringBehavior is a string
                # ("oneDirection" / "bothDirections" / "automatic").
                "active": r.get("isActive", True) is not False,
                "bidirectional": str(r.get("crossFilteringBehavior", "")).lower() == "bothdirections",
            }
            for r in _dict_entries(model.get("relationships"), "model.relationships", entry.path, warnings)
            if r.get("fromTable") and r.get("toTable")
        ]
        if relationships:
            merge_relationships(model_node, relationships)
        link_measures(graph, model_name)
        link_calculated_tables(graph, model_name)
    return warnings
