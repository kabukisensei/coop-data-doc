"""Report parsing: PBIR folders and legacy report.json (Module 3).

Visual field bindings are collected as structured (entity, property, kind)
tuples; ``link_visual_bindings`` resolves them against loaded semantic
models once everything is parsed. Anything ambiguous is left as
``pending_model_resolution`` metadata for the Module 4 linker.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path, PurePosixPath

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

_KIND_KEYS = {
    "measure": "measure",
    "column": "column",
    "aggregation": "column",
    "hierarchylevel": "column",
}


def _find_entity(obj) -> str | None:
    if isinstance(obj, dict):
        entity = obj.get("Entity") or obj.get("entity")
        if isinstance(entity, str):
            return entity
        for value in obj.values():
            found = _find_entity(value)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_entity(value)
            if found is not None:
                return found
    return None


def _collect_bindings(obj, out: set[tuple[str, str, str]], parent_key: str = "") -> None:
    if isinstance(obj, dict):
        prop = obj.get("Property") or obj.get("property")
        if isinstance(prop, str):
            entity = _find_entity(obj)
            if entity:
                kind = _KIND_KEYS.get(parent_key.lower(), "unknown")
                out.add((normalize_identifier(entity), prop, kind))
        for key, value in obj.items():
            _collect_bindings(value, out, key)
    elif isinstance(obj, list):
        for value in obj:
            _collect_bindings(value, out, parent_key)


def _ensure_report(graph: LineageGraph, report_name: str, source_file: str) -> Node:
    return graph.add_node(
        Node(
            id=Node.make_id(NodeType.REPORT, "", report_name),
            node_type=NodeType.REPORT,
            name=normalize_identifier(report_name),
            source_file=source_file,
        )
    )


def _ensure_page(graph: LineageGraph, report: Node, page_name: str, source_file: str) -> Node:
    page = graph.add_node(
        Node(
            id=Node.make_id(NodeType.REPORT_PAGE, report.name, page_name),
            node_type=NodeType.REPORT_PAGE,
            name=normalize_identifier(page_name),
            schema_name=report.name,
            source_file=source_file,
        )
    )
    graph.add_edge(
        Edge(
            source_id=page.id,
            target_id=report.id,
            edge_type=EdgeType.FEEDS,
            evidence=source_file,
        )
    )
    return page


def _add_visual(
    graph: LineageGraph,
    report: Node,
    page: Node,
    visual_id: str,
    visual_type: str,
    bindings: set[tuple[str, str, str]],
    source_file: str,
) -> Node:
    visual = graph.add_node(
        Node(
            id=Node.make_id(NodeType.VISUAL, report.name, visual_id),
            node_type=NodeType.VISUAL,
            name=normalize_identifier(visual_id),
            schema_name=report.name,
            source_file=source_file,
            metadata={
                "visual_type": visual_type,
                "bindings": [{"entity": e, "property": p, "kind": k} for e, p, k in sorted(bindings)],
            },
        )
    )
    graph.add_edge(
        Edge(
            source_id=visual.id,
            target_id=page.id,
            edge_type=EdgeType.FEEDS,
            evidence=source_file,
        )
    )
    return visual


def report_root(path: str) -> tuple[str, str]:
    """(root_prefix, report_name) for a path inside a PBIR report folder."""
    parts = PurePosixPath(path).parts
    for index, part in enumerate(parts):
        if part.lower().endswith(".report"):
            return "/".join(parts[: index + 1]), part[: -len(".Report")]
    if "definition" in parts:
        index = parts.index("definition")
        if index > 0:
            return "/".join(parts[:index]), parts[index - 1]
    return parts[0] if len(parts) > 1 else "", parts[0]


def parse_pbir(
    visual_entries: list[FileEntry],
    page_entries: list[FileEntry],
    graph: LineageGraph,
    *,
    on_file: Callable[..., None] | None = None,
) -> list[ParseWarning]:
    """Extract report/page/visual nodes and field bindings from PBIR
    visual.json + page.json files.

    ``on_file`` (optional) is called once per visual entry for progress.
    """
    warnings: list[ParseWarning] = []

    page_display: dict[tuple[str, str], str] = {}
    for entry in page_entries:
        try:
            data = json.loads(Path(entry.abs_path).read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(ParseWarning(file=entry.path, message=str(exc), category="pbir_parse"))
            continue
        parts = PurePosixPath(entry.path).parts
        # .../definition/pages/<page_folder>/page.json
        if len(parts) >= 2:
            root, _ = report_root(entry.path)
            page_display[(root, parts[-2])] = data.get("displayName") or parts[-2]

    for entry in sorted(visual_entries, key=lambda e: e.path):
        if on_file:
            on_file(entry.path)
        try:
            data = json.loads(Path(entry.abs_path).read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(ParseWarning(file=entry.path, message=str(exc), category="pbir_parse"))
            continue
        root, report_name = report_root(entry.path)
        parts = PurePosixPath(entry.path).parts
        # .../definition/pages/<page>/visuals/<visual_id>/visual.json
        page_folder = parts[-4] if len(parts) >= 4 else "page"
        visual_id = parts[-2] if len(parts) >= 2 else data.get("name", "visual")

        report = _ensure_report(graph, report_name, entry.path)
        page_name = page_display.get((root, page_folder), page_folder)
        page = _ensure_page(graph, report, page_name, entry.path)

        visual_obj = data.get("visual") or {}
        visual_type = visual_obj.get("visualType") or data.get("visualType") or "unknown"
        bindings: set[tuple[str, str, str]] = set()
        _collect_bindings(data, bindings)
        _add_visual(graph, report, page, visual_id, visual_type, bindings, entry.path)
    return warnings


def parse_layout_json(
    data: dict, report_name: str, source_file: str, graph: LineageGraph
) -> list[ParseWarning]:
    """Legacy report layout: sections[].visualContainers[].config (a JSON
    string embedded in JSON)."""
    warnings: list[ParseWarning] = []
    report = _ensure_report(graph, report_name, source_file)
    for section_index, section in enumerate(data.get("sections") or []):
        page_name = section.get("displayName") or section.get("name") or f"page{section_index}"
        page = _ensure_page(graph, report, page_name, source_file)
        for container_index, container in enumerate(section.get("visualContainers") or []):
            config_raw = container.get("config")
            if not isinstance(config_raw, str):
                continue
            try:
                config = json.loads(config_raw)
            except json.JSONDecodeError:
                warnings.append(
                    ParseWarning(
                        file=source_file,
                        message=f"unparseable visual config on page '{page_name}'",
                        category="report_json_parse",
                    )
                )
                continue
            single = config.get("singleVisual") or {}
            visual_type = single.get("visualType") or "unknown"
            visual_id = config.get("name") or f"{page_name}_{container_index}"
            bindings: set[tuple[str, str, str]] = set()

            for refs in (single.get("projections") or {}).values():
                for ref in refs or []:
                    query_ref = ref.get("queryRef") if isinstance(ref, dict) else None
                    if isinstance(query_ref, str) and "." in query_ref:
                        entity, prop = query_ref.split(".", 1)
                        bindings.add((normalize_identifier(entity), prop, "unknown"))

            prototype = single.get("prototypeQuery") or {}
            alias_map = {
                item.get("Name"): item.get("Entity")
                for item in prototype.get("From") or []
                if isinstance(item, dict)
            }
            for select in prototype.get("Select") or []:
                if not isinstance(select, dict):
                    continue
                for kind_key, payload in select.items():
                    if not isinstance(payload, dict):
                        continue
                    prop = payload.get("Property")
                    source_alias = ((payload.get("Expression") or {}).get("SourceRef") or {}).get("Source")
                    entity = alias_map.get(source_alias)
                    if isinstance(prop, str) and isinstance(entity, str):
                        kind = _KIND_KEYS.get(kind_key.lower(), "unknown")
                        bindings.add((normalize_identifier(entity), prop, kind))

            _add_visual(graph, report, page, visual_id, visual_type, bindings, source_file)
    return warnings


def parse_legacy_reports(
    entries: list[FileEntry],
    graph: LineageGraph,
    *,
    on_file: Callable[..., None] | None = None,
) -> list[ParseWarning]:
    """Extract the same structure from standalone legacy report.json files.

    ``on_file`` (optional) is called once per entry for progress reporting.
    """
    warnings: list[ParseWarning] = []
    for entry in sorted(entries, key=lambda e: e.path):
        if on_file:
            on_file(entry.path)
        try:
            data = json.loads(Path(entry.abs_path).read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(ParseWarning(file=entry.path, message=str(exc), category="report_json_parse"))
            continue
        parent = PurePosixPath(entry.path).parent.name or PurePosixPath(entry.path).stem
        warnings += parse_layout_json(data, parent, entry.path, graph)
    return warnings


def link_visual_bindings(graph: LineageGraph) -> list[ParseWarning]:
    """Resolve visual bindings against loaded semantic models.

    A binding resolves when exactly one model contains a pbi_table with the
    bound entity name; otherwise it is left for the Module 4 linker.
    """
    warnings: list[ParseWarning] = []
    tables_by_name: dict[str, list[Node]] = {}
    measures: dict[tuple[str, str], Node] = {}
    for node in graph.nodes.values():
        if node.node_type is NodeType.PBI_TABLE:
            tables_by_name.setdefault(node.name, []).append(node)
        elif node.node_type is NodeType.MEASURE:
            measures[(node.schema_name, node.name)] = node

    for node_id in sorted(graph.nodes):
        visual = graph.nodes.get(node_id)
        if visual is None or visual.node_type is not NodeType.VISUAL:
            continue
        pending: list[dict[str, str]] = []
        for binding in visual.metadata.get("bindings", []):
            candidates = tables_by_name.get(binding["entity"], [])
            if len(candidates) != 1:
                pending.append(binding)
                continue
            table = candidates[0]
            graph.add_edge(
                Edge(
                    source_id=visual.id,
                    target_id=table.id,
                    edge_type=EdgeType.VISUALIZES,
                    evidence=f"{visual.source_file}: {binding['entity']}.{binding['property']}",
                )
            )
            measure = measures.get((table.schema_name, normalize_identifier(binding["property"])))
            if measure is not None and binding["kind"] in ("measure", "unknown"):
                graph.add_edge(
                    Edge(
                        source_id=visual.id,
                        target_id=measure.id,
                        edge_type=EdgeType.VISUALIZES,
                        evidence=f"{visual.source_file}: [{binding['property']}]",
                    )
                )
        if pending:
            visual.metadata["pending_model_resolution"] = pending
        elif "pending_model_resolution" in visual.metadata:
            del visual.metadata["pending_model_resolution"]
    return warnings
