"""Report parsing: PBIR folders and legacy report.json (Module 3).

Visual field bindings are collected as structured (entity, property, kind)
tuples; ``link_visual_bindings`` resolves them against loaded semantic models
once everything is parsed — first by unique name, then by report context. A
binding still ambiguous after that raises an ``ambiguous_visual_binding``
warning and is preserved (never dropped): ``collapse_visuals`` copies the
surviving ``pending_model_resolution`` bindings onto the owning report as
``unresolved_bindings`` metadata.
"""

from __future__ import annotations

import json
import re
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

# Power BI auto date/time machinery: every date column gets a hidden
# LocalDateTable_<guid> (plus one DateTableTemplate_<guid>) and visuals bind
# date hierarchies through them. They are internal, hidden, and never part of
# the documented model - matching them against real tables only produces
# unmatchable-binding noise, so they are skipped up front.
_AUTO_DATETIME_RE = re.compile(r"^(localdatetable_|datetabletemplate_)[0-9a-f\-]{8,}$")

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


def _collect_bindings(
    obj, out: set[tuple[str, str, str, str]], role: str = "shown", parent_key: str = ""
) -> None:
    """Collect ``(entity, property, kind, role)`` field references. ``role`` is
    ``"shown"`` for displayed fields (visual query projections) and ``"filter"``
    for fields referenced only by a filter — the caller walks the visual's query
    and its filterConfig separately so filter fields no longer masquerade as
    displayed ones (issue #26)."""
    if isinstance(obj, dict):
        prop = obj.get("Property") or obj.get("property")
        if isinstance(prop, str):
            entity = _find_entity(obj)
            if entity:
                kind = _KIND_KEYS.get(parent_key.lower(), "unknown")
                out.add((normalize_identifier(entity), prop, kind, role))
        for key, value in obj.items():
            _collect_bindings(value, out, role, key)
    elif isinstance(obj, list):
        for value in obj:
            _collect_bindings(value, out, role, parent_key)


def _filter_fields(obj) -> set[tuple[str, str, str, str]]:
    """``(entity, property, kind, "filter")`` for every field a filterConfig
    references. ``obj`` may be a PBIR ``filterConfig`` dict or a legacy
    JSON-embedded ``filters`` string; a non-parseable string yields nothing."""
    if isinstance(obj, str):
        try:
            obj = json.loads(obj, strict=False)
        except (json.JSONDecodeError, TypeError):
            return set()
    out: set[tuple[str, str, str, str]] = set()
    if obj:
        _collect_bindings(obj, out, role="filter")
    return out


def _ensure_report(graph: LineageGraph, report_name: str, source_file: str) -> Node:
    return graph.add_node(
        Node(
            id=Node.make_id(NodeType.REPORT, "", report_name),
            node_type=NodeType.REPORT,
            name=normalize_identifier(report_name),
            # keep the original-case name for rendering (titles/link labels);
            # id/name stay normalized for matching (issue #20).
            display_name=report_name,
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
    bindings: set[tuple[str, str, str, str]],
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
                "bindings": [
                    {"entity": e, "property": p, "kind": k, "role": r} for e, p, k, r in sorted(bindings)
                ],
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


def _append_report_filters(
    graph: LineageGraph,
    report_name: str,
    source_file: str,
    fields: set[tuple[str, str, str, str]],
    scope: str,
) -> None:
    """Attach report/page-scoped filter fields to the report node as raw pending
    filters (resolved later by :func:`link_visual_bindings` against the report's
    models). Deterministic: fields are appended in sorted order."""
    if not fields:
        return
    report = _ensure_report(graph, report_name, source_file)
    pending = report.metadata.setdefault("pending_filters", [])
    for entity, prop, kind, _role in sorted(fields):
        pending.append({"entity": entity, "property": prop, "kind": kind, "scope": scope})


def _dict_or_empty(value) -> dict:
    """``value`` when it is a dict, else ``{}`` — a malformed nested report
    shape (a string/list where an object belongs) degrades to "nothing here"
    instead of raising (hard rule 3)."""
    return value if isinstance(value, dict) else {}


def _load_pbir_json(
    entry: FileEntry, warnings: list[ParseWarning], *, category: str = "pbir_parse"
) -> dict | None:
    """Read + JSON-parse a report file into a dict, or append a warning under
    ``category`` and return ``None``. Valid-but-non-object JSON (a top-level
    array, string, or number) degrades to a warning too — the parser never
    raises on malformed input (hard rule 3)."""
    try:
        data = json.loads(
            Path(entry.abs_path).read_text(encoding="utf-8-sig", errors="replace"), strict=False
        )
    except (OSError, json.JSONDecodeError) as exc:
        warnings.append(ParseWarning(file=entry.path, message=str(exc), category=category))
        return None
    if not isinstance(data, dict):
        warnings.append(
            ParseWarning(
                file=entry.path,
                message=f"{PurePosixPath(entry.path).name} is not a JSON object",
                category=category,
            )
        )
        return None
    return data


def parse_pbir(
    visual_entries: list[FileEntry],
    page_entries: list[FileEntry],
    report_entries: list[FileEntry],
    graph: LineageGraph,
    *,
    on_file: Callable[..., None] | None = None,
) -> list[ParseWarning]:
    """Extract report/page/visual nodes and field bindings from PBIR
    visual.json + page.json + definition/report.json files.

    Shown fields (visual query projections) and filter fields (report/page/visual
    ``filterConfig``) are tracked with distinct roles (issue #26): filter fields
    still create dependency edges but no longer inflate the report's displayed
    tables/measures. ``on_file`` (optional) is called once per visual entry.
    """
    warnings: list[ParseWarning] = []

    # Report-scoped filters: definition/report.json filterConfig.
    for entry in report_entries:
        data = _load_pbir_json(entry, warnings)
        if data is None:
            continue
        _, report_name = report_root(entry.path)
        _append_report_filters(
            graph, report_name, entry.path, _filter_fields(data.get("filterConfig")), "report"
        )

    page_display: dict[tuple[str, str], str] = {}
    for entry in page_entries:
        data = _load_pbir_json(entry, warnings)
        if data is None:
            continue
        parts = PurePosixPath(entry.path).parts
        # .../definition/pages/<page_folder>/page.json
        if len(parts) >= 2:
            root, report_name = report_root(entry.path)
            page_display[(root, parts[-2])] = data.get("displayName") or parts[-2]
            # Page-scoped filters: page.json filterConfig.
            _append_report_filters(
                graph, report_name, entry.path, _filter_fields(data.get("filterConfig")), "page"
            )

    for entry in sorted(visual_entries, key=lambda e: e.path):
        if on_file:
            on_file(entry.path)
        data = _load_pbir_json(entry, warnings)
        if data is None:
            continue
        root, report_name = report_root(entry.path)
        parts = PurePosixPath(entry.path).parts
        # .../definition/pages/<page>/visuals/<visual_id>/visual.json
        page_folder = parts[-4] if len(parts) >= 4 else "page"
        visual_id = parts[-2] if len(parts) >= 2 else data.get("name", "visual")

        report = _ensure_report(graph, report_name, entry.path)
        page_name = page_display.get((root, page_folder), page_folder)
        page = _ensure_page(graph, report, page_name, entry.path)

        # a "visual" value that isn't an object (malformed export) is treated as
        # absent — the visual node still exists, just untyped/unbound
        visual_obj = _dict_or_empty(data.get("visual"))
        visual_type = visual_obj.get("visualType") or data.get("visualType") or "unknown"
        # Walk the displayed query and the visual-level filterConfig SEPARATELY so
        # filter fields carry role="filter" instead of leaking into shown fields.
        bindings: set[tuple[str, str, str, str]] = set()
        _collect_bindings(visual_obj, bindings, role="shown")
        _collect_bindings(data.get("filterConfig") or {}, bindings, role="filter")
        _add_visual(graph, report, page, visual_id, visual_type, bindings, entry.path)
    return warnings


def parse_layout_json(
    data: dict, report_name: str, source_file: str, graph: LineageGraph
) -> list[ParseWarning]:
    """Legacy report layout: sections[].visualContainers[].config (a JSON
    string embedded in JSON)."""
    warnings: list[ParseWarning] = []
    report = _ensure_report(graph, report_name, source_file)
    # Legacy report-level filters (a JSON-embedded string) → report-scope filters.
    _append_report_filters(graph, report_name, source_file, _filter_fields(data.get("filters")), "report")
    for section_index, section in enumerate(data.get("sections") or []):
        if not isinstance(section, dict):
            continue  # malformed section entry — skip, never raise (hard rule 3)
        page_name = section.get("displayName") or section.get("name") or f"page{section_index}"
        page = _ensure_page(graph, report, page_name, source_file)
        # Legacy section-level filters (a JSON-embedded string) → page-scope filters.
        _append_report_filters(
            graph, report_name, source_file, _filter_fields(section.get("filters")), "page"
        )
        for container_index, container in enumerate(section.get("visualContainers") or []):
            if not isinstance(container, dict):
                continue  # malformed container entry — skip, never raise (hard rule 3)
            config_raw = container.get("config")
            if not isinstance(config_raw, str):
                continue
            try:
                config = json.loads(config_raw, strict=False)
            except json.JSONDecodeError:
                config = None
            # unparseable OR valid-but-non-object (a top-level array/string/
            # number): either way the config is unusable — warn and skip
            if not isinstance(config, dict):
                warnings.append(
                    ParseWarning(
                        file=source_file,
                        message=f"unparseable visual config on page '{page_name}'",
                        category="report_json_parse",
                    )
                )
                continue
            single = _dict_or_empty(config.get("singleVisual"))
            visual_type = single.get("visualType") or "unknown"
            visual_id = config.get("name") or f"{page_name}_{container_index}"
            bindings: set[tuple[str, str, str, str]] = set()

            for refs in _dict_or_empty(single.get("projections")).values():
                for ref in refs or []:
                    query_ref = ref.get("queryRef") if isinstance(ref, dict) else None
                    if isinstance(query_ref, str) and "." in query_ref:
                        entity, prop = query_ref.split(".", 1)
                        bindings.add((normalize_identifier(entity), prop, "unknown", "shown"))
            # visual-level legacy filters live on the container itself
            bindings |= _filter_fields(container.get("filters"))

            prototype = _dict_or_empty(single.get("prototypeQuery"))
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
                    source_alias = _dict_or_empty(
                        _dict_or_empty(payload.get("Expression")).get("SourceRef")
                    ).get("Source")
                    entity = alias_map.get(source_alias)
                    if isinstance(prop, str) and isinstance(entity, str):
                        kind = _KIND_KEYS.get(kind_key.lower(), "unknown")
                        bindings.add((normalize_identifier(entity), prop, kind, "shown"))

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
        # report.json is a generic filename other tools also emit (often as a
        # top-level JSON array) — a non-object one degrades to a warning and is
        # skipped, never raised on (hard rule 3, issue #41).
        data = _load_pbir_json(entry, warnings, category="report_json_parse")
        if data is None:
            continue
        # report_root strips a `.Report` folder suffix (and handles the
        # definition/ fallback), so a legacy `<Name>.Report/report.json` yields
        # `report:<name>` — the SAME id scheme as PBIR-parsed reports, instead of
        # the old literal-suffixed `report:<name>.report` (issue #20).
        _, report_name = report_root(entry.path)
        warnings += parse_layout_json(data, report_name, entry.path, graph)
    return warnings


_INITIAL_CATALOG_RE = re.compile(r'initial\s+catalog\s*=\s*"?([^";]+)"?', re.IGNORECASE)


def _declared_model_name(data: dict) -> str | None:
    """The semantic model a ``definition.pbir`` authoritatively declares.

    ``datasetReference.byPath.path`` (``"../Sales.SemanticModel"``) → the folder's
    base name minus a ``.SemanticModel`` suffix (``Sales``); or
    ``datasetReference.byConnection.connectionString`` → its ``initial catalog``
    value. ``None`` when neither is present/parseable (caller falls back to
    name-based inference).
    """
    ref = data.get("datasetReference")
    if not isinstance(ref, dict):
        return None
    by_path = ref.get("byPath")
    if isinstance(by_path, dict) and isinstance(by_path.get("path"), str) and by_path["path"].strip():
        # tolerate Windows-authored backslash paths ("..\\Sales.SemanticModel")
        folder = PurePosixPath(by_path["path"].strip().replace("\\", "/")).name
        if folder.lower().endswith(".semanticmodel"):
            return folder[: -len(".SemanticModel")] or None
        return folder or None
    by_conn = ref.get("byConnection")
    if isinstance(by_conn, dict) and isinstance(by_conn.get("connectionString"), str):
        match = _INITIAL_CATALOG_RE.search(by_conn["connectionString"])
        if match:
            return match.group(1).strip() or None
    return None


def parse_pbir_definitions(
    entries: list[FileEntry],
    graph: LineageGraph,
    *,
    on_file: Callable[..., None] | None = None,
) -> list[ParseWarning]:
    """Parse each report's ``definition.pbir`` — the file that AUTHORITATIVELY
    declares which semantic model the report is bound to (``datasetReference``).

    The declared model is stored on the report node as
    ``metadata["declared_model"]`` (normalized) and, when that model is loaded,
    wired directly as a ``model --feeds--> report`` edge. :func:`link_visual_bindings`
    then scopes the report's binding resolution to the declared model, so a thin
    report whose entity names are ambiguous across two models (a model and its
    fork) resolves instead of collapsing to nothing. A declaration naming a model
    NOT among the loaded repos (e.g. a published model reached ``byConnection``)
    is marked ``declared_model_unresolved`` and warned — never wired to a guessed
    model that merely shares a name (hard rule 4).

    Runs after the semantic-model parsers (so the model nodes exist to match) and
    before :func:`link_visual_bindings`. ``on_file`` (optional) ticks once per entry.
    """
    warnings: list[ParseWarning] = []
    for entry in sorted(entries, key=lambda e: e.path):
        if on_file:
            on_file(entry.path)
        if not any(part.lower().endswith(".report") for part in PurePosixPath(entry.path).parts):
            warnings.append(
                ParseWarning(
                    file=entry.path,
                    message="definition.pbir outside a *.Report folder — skipped (repo debris?)",
                    category="pbir_stray_file",
                )
            )
            continue
        try:
            data = json.loads(
                Path(entry.abs_path).read_text(encoding="utf-8-sig", errors="replace"), strict=False
            )
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(ParseWarning(file=entry.path, message=str(exc), category="pbir_parse"))
            continue
        if not isinstance(data, dict):
            warnings.append(
                ParseWarning(
                    file=entry.path, message="definition.pbir is not a JSON object", category="pbir_parse"
                )
            )
            continue
        declared = _declared_model_name(data)
        if not declared:
            # No parseable datasetReference: fall back to name-based inference
            # silently (this is the normal shape for some bindings, not an error).
            continue
        _, report_name = report_root(entry.path)
        report = _ensure_report(graph, report_name, entry.path)
        model_key = normalize_identifier(declared)
        report.metadata["declared_model"] = model_key
        model_id = Node.make_id(NodeType.SEMANTIC_MODEL, "", declared)
        if model_id in graph.nodes:
            graph.add_edge(
                Edge(
                    source_id=model_id,
                    target_id=report.id,
                    edge_type=EdgeType.FEEDS,
                    evidence=f"{entry.path}: definition.pbir declares this model",
                )
            )
        else:
            # The declaration names a model not among the loaded repos — surface
            # it; never fabricate an edge to a local model that merely shares a
            # name (hard rule 4).
            report.metadata["declared_model_unresolved"] = True
            warnings.append(
                ParseWarning(
                    file=entry.path,
                    message=f"declared model '{declared}' is not among the loaded semantic models",
                    category="pbir_external_model",
                )
            )
    return warnings


def link_visual_bindings(graph: LineageGraph) -> list[ParseWarning]:
    """Resolve visual bindings against loaded semantic models.

    A binding resolves when exactly one model has a pbi_table with the bound entity
    name. When several models share the name (near-universal — every model has a
    ``Date`` dimension), a second pass disambiguates using REPORT CONTEXT: it restricts
    candidates to the models the same report already draws from via its other resolved
    bindings. Bindings still ambiguous after that are NOT dropped silently — they raise
    an ``ambiguous_visual_binding`` warning and stay in ``pending_model_resolution``
    metadata, which :func:`collapse_visuals` propagates onto the report (hard rule 4).
    """
    warnings: list[ParseWarning] = []
    tables_by_name: dict[str, list[Node]] = {}
    measures: dict[tuple[str, str], Node] = {}
    for node in graph.nodes.values():
        if node.node_type is NodeType.PBI_TABLE:
            tables_by_name.setdefault(node.name, []).append(node)
        elif node.node_type is NodeType.MEASURE:
            measures[(node.schema_name, node.name)] = node

    page_of_visual, report_of_page = _page_visual_maps(graph)
    report_of_visual = {
        vid: report_of_page.get(pid) for vid, pid in page_of_visual.items() if report_of_page.get(pid)
    }
    # A report's authoritatively-declared model (from definition.pbir, issue #19).
    # The declared name is normalized and equals the pbi_table.schema_name, so it
    # scopes candidate tables directly. Used to disambiguate name-collisions BEFORE
    # the uniqueness check, so a thin report bound to a model that shares every
    # table name with a fork still resolves.
    declared_by_report = {
        nid: node.metadata["declared_model"]
        for nid, node in graph.nodes.items()
        if node.node_type is NodeType.REPORT and node.metadata.get("declared_model")
    }

    def _resolve(visual: Node, binding: dict[str, str], table: Node) -> None:
        role = binding.get("role", "shown")
        refs = visual.metadata.setdefault("resolved", [])
        graph.add_edge(
            Edge(
                source_id=visual.id,
                target_id=table.id,
                edge_type=EdgeType.VISUALIZES,
                evidence=f"{visual.source_file}: {binding['entity']}.{binding['property']}",
            )
        )
        refs.append(
            {
                "target": table.id,
                "entity": binding["entity"],
                "property": binding["property"],
                "kind": binding["kind"],
                "role": role,
                "scope": "visual",
            }
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
            refs.append(
                {
                    "target": measure.id,
                    "entity": binding["entity"],
                    "property": binding["property"],
                    "kind": "measure",
                    "role": role,
                    "scope": "visual",
                }
            )

    # Pass 1: resolve the unambiguous bindings and record each report's models.
    report_models: dict[str, set[str]] = {}
    visual_pending: dict[str, list[dict[str, str]]] = {}
    for node_id in sorted(graph.nodes):
        visual = graph.nodes.get(node_id)
        if visual is None or visual.node_type is not NodeType.VISUAL:
            continue
        visual.metadata.pop("pending_model_resolution", None)
        report = report_of_visual.get(visual.id)
        declared = declared_by_report.get(report) if report is not None else None
        pending: list[dict[str, str]] = []
        for binding in visual.metadata.get("bindings", []):
            if _AUTO_DATETIME_RE.match(binding["entity"]):
                continue  # auto date/time internals — never documented tables
            candidates = tables_by_name.get(binding["entity"], [])
            if declared:
                # Restrict to the declared model when it actually has the table;
                # a table the declaration doesn't cover falls through to normal
                # inference (so a globally-unique name still resolves).
                scoped = [t for t in candidates if t.schema_name == declared]
                if scoped:
                    candidates = scoped
            if len(candidates) == 1:
                _resolve(visual, binding, candidates[0])
                if report is not None:
                    report_models.setdefault(report, set()).add(candidates[0].schema_name)
            else:
                pending.append(binding)
        if pending:
            visual_pending[node_id] = pending

    # Pass 2: disambiguate each pending binding within the report's resolved models.
    # Unresolved bindings are collected per (report, entity) and emitted as ONE
    # warning each after the loop — a report with 40 visuals binding the same
    # missing table used to produce 40 identical lines. Zero-candidate entities
    # get their own category (they aren't "ambiguous" — nothing matched at all).
    unmatched: dict[tuple[str, str], tuple[str, int]] = {}
    ambiguous: dict[tuple[str, str], tuple[str, set[str], int]] = {}
    for node_id in sorted(visual_pending):
        visual = graph.nodes[node_id]
        report = report_of_visual.get(visual.id) or ""
        models = report_models.get(report, set()) if report else set()
        still_pending: list[dict[str, str]] = []
        for binding in visual_pending[node_id]:
            candidates = tables_by_name.get(binding["entity"], [])
            scoped = [t for t in candidates if t.schema_name in models] if models else candidates
            if len(scoped) == 1:
                _resolve(visual, binding, scoped[0])
                continue
            still_pending.append(binding)
            key = (report, binding["entity"])
            if not candidates:
                file, count = unmatched.get(key, (visual.source_file, 0))
                unmatched[key] = (file, count + 1)
            else:
                file, owners, count = ambiguous.get(key, (visual.source_file, set(), 0))
                owners.update(t.schema_name for t in candidates)
                ambiguous[key] = (file, owners, count + 1)
        if still_pending:
            visual.metadata["pending_model_resolution"] = still_pending

    for (report, entity), (file, count) in sorted(unmatched.items()):
        warnings.append(
            ParseWarning(
                file=file,
                message=(
                    f"visual binding entity '{entity}' matches no documented table in any "
                    f"model ({count} binding(s)) — the visuals may reference a table the "
                    "model no longer has, or a model that isn't documented"
                ),
                category="unmatched_visual_entity",
            )
        )
    for (report, entity), (file, owners, count) in sorted(ambiguous.items()):
        owner_list = ", ".join(sorted(owners))
        warnings.append(
            ParseWarning(
                file=file,
                message=(
                    f"visual binding entity '{entity}' is ambiguous — it exists in "
                    f"{owner_list} but none is uniquely attributable to this report "
                    f"({count} binding(s)) — not linked"
                ),
                category="ambiguous_visual_binding",
            )
        )

    # Report/page-scoped filters (definition/report.json, page.json, legacy
    # top-level/section filters): resolve against the report's models so a field
    # used ONLY in a page/report filter still becomes a dependency edge and lands
    # in the report's Filters section — never guessed (issue #26, hard rule 4).
    model_ids = {n.name for n in graph.nodes.values() if n.node_type is NodeType.SEMANTIC_MODEL}
    for report_id in sorted(graph.nodes):
        report = graph.nodes.get(report_id)
        if report is None or report.node_type is not NodeType.REPORT:
            continue
        pending_filters = report.metadata.pop("pending_filters", None)
        if not pending_filters:
            continue
        models = set(report_models.get(report_id, set()))
        declared = declared_by_report.get(report_id)
        if declared:
            models.add(declared)
        field_refs = report.metadata.setdefault("field_refs", [])
        for flt in pending_filters:
            entity, prop, kind, scope = flt["entity"], flt["property"], flt["kind"], flt["scope"]
            candidates = tables_by_name.get(entity, [])
            scoped = [t for t in candidates if t.schema_name in models] if models else candidates
            if len(scoped) == 1:
                table = scoped[0]
            elif len(candidates) == 1:
                # A single GLOBAL candidate is provable, not a guess: resolve it
                # even when it sits outside the report's shown models (a filter
                # on a model the report shows nothing from — the filter-only-model
                # case #26 exists to capture). Mirrors the shown-binding pass 1,
                # which resolves any globally-unique entity regardless of context.
                table = candidates[0]
            else:
                if len(candidates) > 1:
                    warnings.append(
                        ParseWarning(
                            file=report.source_file,
                            message=(
                                f"{scope} filter {entity}.{prop} is ambiguous across "
                                f"{len(candidates)} models — not linked"
                            ),
                            category="ambiguous_visual_binding",
                        )
                    )
                continue
            graph.add_edge(
                Edge(
                    source_id=report.id,
                    target_id=table.id,
                    edge_type=EdgeType.VISUALIZES,
                    evidence=f"{report.source_file}: {scope} filter {entity}.{prop}",
                )
            )
            if table.schema_name in model_ids:
                # the report depends on the model it filters on, even with nothing
                # shown from it — link_reports_to_models won't (it keys off visuals).
                graph.add_edge(
                    Edge(
                        source_id=Node.make_id(NodeType.SEMANTIC_MODEL, "", table.schema_name),
                        target_id=report.id,
                        edge_type=EdgeType.FEEDS,
                        evidence=f"{report.source_file}: report filters on this model",
                    )
                )
            field_refs.append(
                {
                    "target": table.id,
                    "entity": entity,
                    "property": prop,
                    "kind": kind,
                    "role": "filter",
                    "scope": scope,
                }
            )
            measure = measures.get((table.schema_name, normalize_identifier(prop)))
            if measure is not None and kind in ("measure", "unknown"):
                graph.add_edge(
                    Edge(
                        source_id=report.id,
                        target_id=measure.id,
                        edge_type=EdgeType.VISUALIZES,
                        evidence=f"{report.source_file}: {scope} filter [{prop}]",
                    )
                )
                field_refs.append(
                    {
                        "target": measure.id,
                        "entity": entity,
                        "property": prop,
                        "kind": "measure",
                        "role": "filter",
                        "scope": scope,
                    }
                )
    return warnings


def _page_visual_maps(graph: LineageGraph) -> tuple[dict[str, str], dict[str, str]]:
    """``(page_of_visual, report_of_page)`` from the report→page→visual ``feeds``
    edges. Shared by link_reports_to_models and collapse_visuals so the two stay
    in lockstep on what counts as the report hierarchy.
    """
    page_of_visual: dict[str, str] = {}
    report_of_page: dict[str, str] = {}
    for edge in graph.edges:
        if edge.edge_type is not EdgeType.FEEDS:
            continue
        src, tgt = graph.nodes.get(edge.source_id), graph.nodes.get(edge.target_id)
        if src is None or tgt is None:
            continue
        if src.node_type is NodeType.VISUAL and tgt.node_type is NodeType.REPORT_PAGE:
            page_of_visual[src.id] = tgt.id
        elif src.node_type is NodeType.REPORT_PAGE and tgt.node_type is NodeType.REPORT:
            report_of_page[src.id] = tgt.id
    return page_of_visual, report_of_page


def link_reports_to_models(graph: LineageGraph) -> list[ParseWarning]:
    """Make reports downstream of the semantic model(s) they draw from.

    A report's models are those owning the tables/measures its visuals
    visualize (report <- page <- visual --visualizes--> table/measure -> model).
    Adds a ``model --feeds--> report`` edge per (model, report) pair so reports
    surface on each model's downstream list. Must run while visual edges still
    exist (before :func:`collapse_visuals`).
    """
    page_of_visual, report_of_page = _page_visual_maps(graph)

    model_id_by_key = {
        n.name: nid for nid, n in graph.nodes.items() if n.node_type is NodeType.SEMANTIC_MODEL
    }
    pairs: set[tuple[str, str]] = set()
    for edge in graph.edges:
        if edge.edge_type is not EdgeType.VISUALIZES:
            continue
        visual, target = graph.nodes.get(edge.source_id), graph.nodes.get(edge.target_id)
        if visual is None or visual.node_type is not NodeType.VISUAL:
            continue
        if target is None or target.node_type not in (NodeType.PBI_TABLE, NodeType.MEASURE):
            continue
        page = page_of_visual.get(visual.id)
        report = report_of_page.get(page) if page is not None else None
        model = model_id_by_key.get(target.schema_name)
        if report is not None and model is not None:
            pairs.add((model, report))

    for model_id, report_id in sorted(pairs):
        graph.add_edge(
            Edge(
                source_id=model_id,
                target_id=report_id,
                edge_type=EdgeType.FEEDS,
                evidence=f"{graph.nodes[report_id].source_file}: report consumes this model",
            )
        )
    return []


_FIELD_REF_KEY = ("role", "target", "entity", "property", "kind", "scope")


def _dedupe_field_refs(refs: list[dict]) -> list[dict]:
    """Distinct field-refs, deterministically sorted by (role, target, entity,
    property, kind, scope) — so a report's field_refs are byte-identical
    regardless of visual/merge order."""
    seen: dict[tuple, dict] = {}
    for ref in refs:
        seen.setdefault(tuple(ref.get(key, "") for key in _FIELD_REF_KEY), ref)
    return [seen[key] for key in sorted(seen)]


def collapse_visuals(graph: LineageGraph) -> list[ParseWarning]:
    """Fold a report's visuals *and* pages into the report, leaving one node
    per report.

    Power BI report internals (pages, visuals) get messy fast and add little
    lineage value; what matters is "which measures/tables does this report
    show". Every visual's ``visualizes`` edge is re-pointed from the visual up
    to its owning report, then all visual and report_page nodes (and their
    edges) are removed. The ``model --feeds--> report`` edges from
    :func:`link_reports_to_models` are untouched, so a report stays downstream
    of its model(s). Run AFTER the Module 4 linker and
    :func:`link_reports_to_models`, while binding edges still exist.
    """
    page_of_visual, report_of_page = _page_visual_maps(graph)

    drop_ids = {
        nid for nid, n in graph.nodes.items() if n.node_type in (NodeType.VISUAL, NodeType.REPORT_PAGE)
    }
    if not drop_ids:
        return []

    # report -> target visualizes edges to recreate (deduped, deterministic)
    rewired: set[tuple[str, str, str]] = set()
    for edge in graph.edges:
        if edge.edge_type is EdgeType.VISUALIZES and edge.source_id in drop_ids:
            page = page_of_visual.get(edge.source_id)
            report = report_of_page.get(page) if page is not None else None
            if report is not None and report != edge.target_id:
                rewired.add((report, edge.target_id, edge.evidence))

    # BEFORE the visuals (which hold the metadata) are deleted, roll two things up
    # onto the owning report so they survive the fold (hard rule 4 for the first):
    #   * still-ambiguous bindings -> report.metadata["unresolved_bindings"]
    #   * per-visual resolved field-refs (target/role/kind/scope) -> report.metadata
    #     ["field_refs"], the role-aware summary the report page renders from (issue
    #     #26/#27). Report/page-scoped filter refs are already on the report from
    #     link_visual_bindings; the visual (shown + visual-filter) refs join them here.
    unresolved_by_report: dict[str, list[dict[str, str]]] = {}
    for vid in sorted(drop_ids):
        node = graph.nodes.get(vid)
        if node is None or node.node_type is not NodeType.VISUAL:
            continue
        page = page_of_visual.get(vid)
        report = report_of_page.get(page) if page is not None else None
        if report is None:
            continue
        pend = node.metadata.get("pending_model_resolution")
        if pend:
            unresolved_by_report.setdefault(report, []).extend(pend)
        resolved = node.metadata.get("resolved")
        if resolved:
            graph.nodes[report].metadata.setdefault("field_refs", []).extend(resolved)
    for report_id, pend in unresolved_by_report.items():
        graph.nodes[report_id].metadata["unresolved_bindings"] = sorted(
            pend, key=lambda b: (b.get("entity", ""), b.get("property", ""), b.get("kind", ""))
        )
    # Dedupe + deterministically sort every report's field-refs (order-independent
    # of visual/merge order so warm == cold and --jobs N is byte-identical).
    for node in graph.nodes.values():
        if node.node_type is NodeType.REPORT and node.metadata.get("field_refs"):
            node.metadata["field_refs"] = _dedupe_field_refs(node.metadata["field_refs"])

    # drop every edge that touches a folded node (replace_edges keeps the dedup index in
    # sync so the add_edge calls below don't see stale/dropped keys), then add report edges
    graph.replace_edges(
        [edge for edge in graph.edges if edge.source_id not in drop_ids and edge.target_id not in drop_ids]
    )
    for report, target, evidence in sorted(rewired):
        graph.add_edge(
            Edge(source_id=report, target_id=target, edge_type=EdgeType.VISUALIZES, evidence=evidence)
        )
    for nid in drop_ids:
        graph.nodes.pop(nid, None)
    return []
