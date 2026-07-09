"""Agent-facing Markdown generation (Module 5).

One file per node with strict, fixed-order YAML front-matter that debugging
agents can parse, plus human-readable contract/lineage tables. The Business
Intent block between the intent markers is human-authored and survives
regeneration verbatim.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from pathlib import Path

from coop_data_doc.graph.model import EdgeType, LineageGraph, Node, NodeType, normalize_identifier
from coop_data_doc.graph.serialize import to_json_str
from coop_data_doc.render.paths import doc_relpath, slug

INTENT_BEGIN = "<!-- intent:begin -->"
INTENT_END = "<!-- intent:end -->"
_DEFAULT_INTENT = "_Add a short description of what this object is for and who relies on it._"
_INTENT_RE = re.compile(re.escape(INTENT_BEGIN) + r"(.*?)" + re.escape(INTENT_END), re.S)

# Object types that have a column contract worth a "Structural Contract"
# section. Procs/measures/semantic-models/reports/visuals never carry columns,
# so the section would only ever render the "not resolvable" placeholder —
# noise that reads like a broken page. They're documented via Source/DAX instead.
_CONTRACT_TYPES = frozenset(
    {
        NodeType.BRONZE_TABLE,
        NodeType.SILVER_TABLE,
        NodeType.GOLD_TABLE,
        NodeType.VIEW,
        NodeType.PBI_TABLE,
    }
)

# Power BI storage mode, shown on table pages — the defining trait of a
# composite model (Import cached vs. DirectQuery live vs. Dual).
_STORAGE_LABELS = {"import": "Import", "directquery": "DirectQuery", "dual": "Dual"}

# Relationship-grid cell markers. Literal codepoints (not Material emoji
# shortcodes) so they render over file:// with no icon-font/CDN fetch — same
# offline-first reasoning as the ⚠ used elsewhere in this module.
_REL_ACTIVE = "🟢"  # active relationship
_REL_INACTIVE = "⚪"  # inactive (e.g. role-playing) relationship
_REL_BIDI = "⇅"  # appended when the relationship cross-filters both ways

# Display titles per node type. report_page / visual never reach the renderer
# (collapse_visuals folds them into the report before rendering) but are kept
# here so any intermediate/pre-collapse graph still renders rather than KeyErrors.
_TYPE_TITLES: dict[NodeType, str] = {
    NodeType.BRONZE_TABLE: "Source Tables (Bronze)",
    NodeType.SILVER_TABLE: "Tables (Silver)",
    NodeType.GOLD_TABLE: "Tables (Gold)",
    NodeType.VIEW: "Views",
    NodeType.STORED_PROC: "Stored Procedures",
    NodeType.SEMANTIC_MODEL: "Semantic Models",
    NodeType.PBI_TABLE: "Semantic Model Tables",
    NodeType.MEASURE: "Measures",
    NodeType.REPORT: "Reports",
    NodeType.REPORT_PAGE: "Report Pages",
    NodeType.VISUAL: "Visuals",
}


def _quote(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return '"' + escaped + '"'


def _text(value: str) -> str:
    """Neutralize model-supplied free text (descriptions, object names) before
    it lands in a page: python-markdown/mkdocs pass raw HTML straight through,
    so a `<script>`/`onerror` payload in a TMDL/BIM description or name would
    execute in every reader's browser. Escapes only the HTML-significant
    `& < >` (quote=False) so ordinary prose is untouched."""
    return html.escape(value or "", quote=False)


def _cell(value: str) -> str:
    """Make free text safe inside a Markdown table cell: neutralize raw HTML
    (`_text`), escape pipes and collapse newlines (an unescaped '|' or newline
    breaks the table)."""
    return _text(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _link_text(value: str) -> str:
    """Cell-safe link label: `_cell` escaping plus '[' / ']', which would
    otherwise close a `[text](url)` link early and leak the raw URL as text
    (Power BI object names can legally contain brackets)."""
    return _cell(value).replace("[", "\\[").replace("]", "\\]")


def _attr(value: str) -> str:
    """Safe value for an HTML attribute inside a Markdown table cell:
    HTML-escape (quotes/angle brackets) and escape '|' so the cell can't break."""
    return html.escape(value or "", quote=True).replace("|", "\\|")


def _front_matter(graph: LineageGraph, node: Node) -> str:
    lines = ["---"]
    lines.append(f"id: {_quote(node.id)}")
    lines.append(f"type: {_quote(node.node_type.value)}")
    lines.append(f"name: {_quote(node.name)}")
    lines.append(f"schema: {_quote(node.schema_name)}")
    lines.append(f"layer: {_quote(node.metadata.get('layer', ''))}")
    lines.append(f"source_file: {_quote(node.source_file)}")
    lines.append(f"path: {_quote(f'{node.node_type.value}/{slug(node.id)}.md')}")
    for key, ids in (
        ("upstream_inputs", graph.upstream(node.id, depth=1)),
        ("downstream_dependents", graph.downstream(node.id, depth=1)),
    ):
        if ids:
            lines.append(f"{key}:")
            lines.extend(f"  - {_quote(i)}" for i in ids)
        else:
            lines.append(f"{key}: []")
    tags = sorted({t for t in (node.schema_name,) if t})
    if tags:
        lines.append("tags:")
        lines.extend(f"  - {_quote(t)}" for t in tags)
    else:
        lines.append("tags: []")
    lines.append("---")
    return "\n".join(lines)


def _contract_section(node: Node) -> str:
    lines = ["## Structural Contract", ""]
    if node.columns:
        lines.append("| Column | Type | Constraints | Description |")
        lines.append("| --- | --- | --- | --- |")
        for column in node.columns:
            nullable = ""
            if column.nullable is False:
                nullable = "NOT NULL"
            elif column.nullable is True:
                nullable = "NULL"
            constraints = ", ".join(part for part in [nullable, *column.constraints] if part)
            lines.append(
                f"| {_cell(column.name)} | {_cell(column.data_type)} "
                f"| {_cell(constraints)} | {_cell(column.description)} |"
            )
    else:
        lines.append("_Columns not statically resolvable for this object._")
    if node.metadata.get("columns_unresolved"):
        lines.append("")
        lines.append("> ⚠ Output columns could not be fully resolved (e.g. `SELECT *`).")
    return "\n".join(lines)


def _lineage_edge_info(graph: LineageGraph) -> tuple[dict, dict]:
    """``(upstream_info, downstream_info)`` where each is ``{node_id: {other_id: (via,
    evidence)}}``, built in ONE pass with first-edge-wins semantics (mirrors what
    _lineage_table did per node per direction — but O(edges) once instead of
    O(nodes×edges×directions)). render_markdown precomputes and threads it in."""
    up: dict[str, dict[str, tuple[str, str]]] = {}
    down: dict[str, dict[str, tuple[str, str]]] = {}
    for edge in graph.edges:
        upstream_id, downstream_id = edge.flow()
        info = (edge.edge_type.value, edge.evidence)
        up.setdefault(downstream_id, {}).setdefault(upstream_id, info)
        down.setdefault(upstream_id, {}).setdefault(downstream_id, info)
    return up, down


def _lineage_table(
    graph: LineageGraph,
    node: Node,
    ids: list[str],
    direction: str,
    edge_info: dict[str, tuple[str, str]] | None = None,
) -> str:
    lines = [f"### {direction}", ""]
    if not ids:
        lines.append(f"_No {direction.lower()} objects._")
        return "\n".join(lines)
    lines.append("| Object | Type | Via | Evidence |")
    lines.append("| --- | --- | --- | --- |")
    if edge_info is None:
        # Standalone fallback: build just this node/direction's map (first-edge-wins).
        edge_info = {}
        for edge in graph.edges:
            upstream_id, downstream_id = edge.flow()
            other = None
            if direction == "Upstream" and downstream_id == node.id:
                other = upstream_id
            elif direction == "Downstream" and upstream_id == node.id:
                other = downstream_id
            if other is not None and other not in edge_info:
                edge_info[other] = (edge.edge_type.value, edge.evidence)
    for other_id in ids:
        other = graph.nodes.get(other_id)
        if other is None:
            continue
        via, evidence = edge_info.get(other_id, ("", ""))
        # On a semantic model's own page, its tables/measures don't need the
        # model-name prefix — the page already establishes the model.
        own_child = (
            node.node_type is NodeType.SEMANTIC_MODEL
            and other.node_type in (NodeType.PBI_TABLE, NodeType.MEASURE)
            and other.schema_name == normalize_identifier(node.name)
        )
        label = other.display if own_child else other.qualified_display
        evidence_file = evidence.split(":", 1)[0] if evidence else ""
        lines.append(
            f"| [{_link_text(label)}]({doc_relpath(other)}) | {other.node_type.value} "
            f"| {_cell(via)} | {_cell(evidence_file)} |"
        )
    return "\n".join(lines)


def _relationship_grid(graph: LineageGraph, node: Node) -> str:
    """Render the "Relationship Grid" — a fact × dimension matrix for a
    semantic model, built from its parsed relationships (``metadata["relationships"]``,
    each a ``{"from": "<table>.<col>", "to": "<table>.<col>"}`` pair).

    Power BI authors relationships from the *many* side to the *one* side, so
    each ``from`` table is a fact (a grid column) and each ``to`` table is a
    dimension (a grid row); a green dot marks the cell where the two relate.
    Tables resolve to their page links and original-case display names; a
    table with no node (e.g. unparsed) falls back to its bare name.
    """
    # Client-facing heading is deliberately neutral ("Relationship Grid"); the
    # feature was internally nicknamed "Joel's Relationship Grid".
    lines = ["## Relationship Grid", ""]
    relationships = node.metadata.get("relationships") or []
    if not relationships:
        lines.append("_No relationships defined in this semantic model._")
        return "\n".join(lines)

    facts: set[str] = set()
    dims: set[str] = set()
    by_cell: dict[tuple[str, str], list[dict]] = {}  # (dimension, fact) -> relationships
    for rel in relationships:
        fact = rel["from"].partition(".")[0]
        dim = rel["to"].partition(".")[0]
        facts.add(fact)
        dims.add(dim)
        by_cell.setdefault((dim, fact), []).append(rel)
    fact_cols = sorted(facts)
    dim_rows = sorted(dims)

    model_key = normalize_identifier(node.name)

    def label(table: str) -> str:
        other = graph.nodes.get(Node.make_id(NodeType.PBI_TABLE, model_key, table))
        if other is not None:
            return f"[{_link_text(other.display)}]({doc_relpath(other)})"
        return _link_text(table)

    def cell(dim: str, fact: str) -> str:
        marks = []
        for rel in by_cell.get((dim, fact), ()):
            active = rel.get("active", True)
            glyph = (_REL_ACTIVE if active else _REL_INACTIVE) + (
                _REL_BIDI if rel.get("bidirectional") else ""
            )
            flags = [
                f for f, on in (("inactive", not active), ("bidirectional", rel.get("bidirectional"))) if on
            ]
            tip = f"{rel['from']} → {rel['to']}" + (f" ({', '.join(flags)})" if flags else "")
            marks.append(f'<span title="{_attr(tip)}">{glyph}</span>')
        return " ".join(marks)

    active_count = sum(1 for rel in relationships if rel.get("active", True))
    lines.append(
        f"{len(fact_cols)} fact(s) × {len(dim_rows)} dimension(s), {len(relationships)} "
        f"relationship(s) ({active_count} active). Columns are facts (the *many* side), "
        "rows are dimensions (the *one* side)."
    )
    lines.append("")
    lines.append(
        f"_Legend: {_REL_ACTIVE} active · {_REL_INACTIVE} inactive · {_REL_BIDI} bidirectional "
        "cross-filter. Hover a marker for the joined columns._"
    )
    lines.append("")
    # wrap the matrix so the site can give it real gridlines (md_in_html keeps
    # the inner Markdown table — links and marker <span>s — fully processed)
    lines.append('<div class="rel-grid" markdown="1">')
    lines.append("")
    lines.append("| Dimension \\ Fact | " + " | ".join(label(f) for f in fact_cols) + " |")
    lines.append("| --- | " + " | ".join([":---:"] * len(fact_cols)) + " |")
    for dim in dim_rows:
        cells = (cell(dim, fact) for fact in fact_cols)
        lines.append(f"| {label(dim)} | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("</div>")
    return "\n".join(lines)


def _direct_upstream(graph: LineageGraph) -> dict[str, list[str]]:
    """node id -> sorted ids of its *direct* upstream sources (data-flow
    direction, so READS/REFERENCES/VISUALIZES are already reversed by flow())."""
    up: dict[str, set[str]] = {}
    for edge in graph.edges:
        upstream_id, downstream_id = edge.flow()
        up.setdefault(downstream_id, set()).add(upstream_id)
    return {key: sorted(value) for key, value in up.items()}


_UPSTREAM_TREE_MAX_DEPTH = 60  # backstop against a pathological chain


def _upstream_tree_text(graph: LineageGraph, node: Node, up: dict[str, list[str]]) -> list[str]:
    """Indented, clickable ancestry tree: each object's direct upstream sources
    nested beneath it, recursing to the source roots. A node reachable by
    several paths is expanded once — later occurrences link to it with
    '(shown above)' — so a DAG can't blow up and a cycle can't loop forever.
    ``up`` is the precomputed direct-upstream map (see :func:`_direct_upstream`).
    """
    out: list[str] = []
    expanded: set[str] = set()

    def walk(node_id: str, depth: int) -> None:
        other = graph.nodes.get(node_id)
        if other is None:
            return
        indent = "    " * depth
        link = f"[{_link_text(other.qualified_display)}]({doc_relpath(other)}) `{other.node_type.value}`"
        parents = up.get(node_id, [])
        if parents and (node_id in expanded or depth >= _UPSTREAM_TREE_MAX_DEPTH):
            note = "_(shown above)_" if node_id in expanded else "_(…)_"
            out.append(f"{indent}- {link} {note}")
            return
        out.append(f"{indent}- {link}")
        expanded.add(node_id)
        for parent in parents:
            walk(parent, depth + 1)

    for parent in up.get(node.id, []):
        walk(parent, 0)
    return out


def _wants_upstream_tree(graph: LineageGraph, node: Node) -> bool:
    """Pages that carry the full 'trace back to source' tree: measures (their
    DAX dependency chain) and the gold tables/views that feed a semantic model —
    the objects a functional user reaches working backwards from a report or
    model, where they may not know the SQL that produced them."""
    if node.node_type is NodeType.MEASURE:
        # "Is anything upstream of this measure?" — i.e. does something reference/feed it.
        # Cheap via the cached up-adjacency instead of a full per-measure edge scan.
        return bool(graph.upstream(node.id, depth=1))
    if node.node_type in (NodeType.GOLD_TABLE, NodeType.VIEW):
        return any(
            (dn := graph.nodes.get(d)) is not None
            and dn.node_type in (NodeType.PBI_TABLE, NodeType.SEMANTIC_MODEL)
            for d in graph.downstream(node.id)
        )
    return False


def _upstream_section(graph: LineageGraph, node: Node, up: dict[str, list[str]]) -> str:
    """A 'trace back to source' section: a readable, collapsible text ancestry
    tree to the source roots."""
    tree = _upstream_tree_text(graph, node, up)
    lines = ["## Upstream lineage", ""]
    if not tree:
        lines.append("_No upstream objects — this is a source._")
        return "\n".join(lines)
    lines.append(
        "_Trace this object back to its sources. Each node links to its page; "
        "branches start collapsed in the HTML — expand to drill down._"
    )
    lines.append("")
    lines.extend(tree)
    return "\n".join(lines)


def _used_measure_ids(graph: LineageGraph) -> set[str]:
    """Ids of measures that something depends on: referenced by another measure
    (REFERENCES) or shown in a report (VISUALIZES). A measure's ``feeds`` edge
    to its own model is deliberately ignored — every measure has that, so it
    can't distinguish a used measure from a dead one.
    """
    used: set[str] = set()
    for edge in graph.edges:
        if edge.edge_type in (EdgeType.REFERENCES, EdgeType.VISUALIZES):
            target = graph.nodes.get(edge.target_id)
            if target is not None and target.node_type is NodeType.MEASURE:
                used.add(edge.target_id)
    return used


_UNUSED_MEASURE_CAVEAT = (
    "not referenced by any other measure or shown in any parsed report. "
    "RLS, calculation groups, field parameters, and external/paginated use "
    "aren't tracked, so confirm before deleting."
)


def _unused_measures_section(graph: LineageGraph, node: Node, used: set[str]) -> str:
    """Roll-up on a semantic-model page listing its measures that nothing
    depends on — MeasureLens-style dead-measure detection for cleanup. Returns
    "" (no section) when every measure is used, to keep healthy models clean.
    ``used`` is the precomputed set from :func:`_used_measure_ids`.
    """
    model_key = normalize_identifier(node.name)
    unused = sorted(
        (
            other
            for other in graph.nodes.values()
            if other.node_type is NodeType.MEASURE and other.schema_name == model_key and other.id not in used
        ),
        key=lambda m: m.display.lower(),
    )
    if not unused:
        return ""
    lines = ["## Unused measures", ""]
    lines.append(f"_Defined in this model but {_UNUSED_MEASURE_CAVEAT}_")
    lines.append("")
    lines.extend(f"- [{_link_text(m.display)}]({doc_relpath(m)})" for m in unused)
    return "\n".join(lines)


def _dedup_sorted(nodes: list[Node]) -> list[Node]:
    """Distinct nodes (by id), sorted by display name then id (stable/deterministic)."""
    return sorted({n.id: n for n in nodes}.values(), key=lambda n: (n.display.lower(), n.id))


def _dedup_filters(filters: list[tuple[Node, str, str]]) -> list[tuple[Node, str, str]]:
    """Distinct (target, property, scope) filter fields, sorted for display."""
    seen: dict[tuple[str, str, str], tuple[Node, str, str]] = {}
    for target, prop, scope in filters:
        seen.setdefault((target.id, prop, scope), (target, prop, scope))
    return [seen[key] for key in sorted(seen, key=lambda k: (seen[k][0].display.lower(), k[1], k[2], k[0]))]


def _report_refs(
    graph: LineageGraph, node: Node
) -> tuple[list[Node], dict[str, list[Node]], dict[str, list[Node]], list[tuple[Node, str, str]], set[str]]:
    """``(models, tables_by_model, measures_by_model, filters, measure_home_ids)``
    for a report — the shared read that both :func:`_report_page` and the index
    Reports overview use.

    ``models`` are the SEMANTIC_MODEL nodes the report ``feeds`` from. The
    ``*_by_model`` dicts hold the SHOWN (displayed) tables/measures grouped by
    model key (a child's ``schema_name`` IS its model key; a child whose model
    isn't among the feeding models falls into a ``""`` bucket — never dropped).
    ``filters`` is the distinct (target, property, scope) fields referenced ONLY
    through a filter (issue #26). ``measure_home_ids`` are the shown tables the
    report reached ONLY through a measure binding, or that are structurally
    measure-only — i.e. tables that appear just because a bound measure lives
    there, not because their data is displayed (issue #27).

    The role/kind split comes from ``metadata["field_refs"]`` (built by the pbir
    pipeline). A report without that summary (e.g. an inline test graph) has no
    filter data, so every visualized target is treated as shown.
    """
    models: list[Node] = []
    for edge in graph.edges:
        if edge.edge_type is EdgeType.FEEDS and edge.target_id == node.id:
            src = graph.nodes.get(edge.source_id)
            if src is not None and src.node_type is NodeType.SEMANTIC_MODEL:
                models.append(src)
    models = _dedup_sorted(models)
    model_keys = {m.name for m in models}

    tables: list[Node] = []
    measures: list[Node] = []
    filters: list[tuple[Node, str, str]] = []
    shown_table_kinds: dict[str, set[str]] = {}  # table id -> reach kinds (issue #27)
    field_refs = node.metadata.get("field_refs")
    if field_refs is not None:
        for ref in field_refs:
            target = graph.nodes.get(ref.get("target", ""))
            if target is None:
                continue
            if ref.get("role") == "filter":
                filters.append((target, ref.get("property", ""), ref.get("scope", "")))
            elif target.node_type is NodeType.MEASURE:
                measures.append(target)
            elif target.node_type is NodeType.PBI_TABLE:
                tables.append(target)
                shown_table_kinds.setdefault(target.id, set()).add(ref.get("kind", "unknown"))
    else:
        # no role summary: every visualized target is a shown field
        for edge in graph.edges:
            if edge.edge_type is EdgeType.VISUALIZES and edge.source_id == node.id:
                tgt = graph.nodes.get(edge.target_id)
                if tgt is None:
                    continue
                if tgt.node_type is NodeType.MEASURE:
                    measures.append(tgt)
                elif tgt.node_type is NodeType.PBI_TABLE:
                    tables.append(tgt)

    # A shown table is a "measure home table" for this report when it carries the
    # structural marker, OR the report reached it ONLY through measure bindings
    # (a table reached by any column/filter binding is a real data table).
    measure_home_ids = {
        table.id
        for table in {t.id: t for t in tables}.values()
        if table.metadata.get("measure_table")
        or (table.id in shown_table_kinds and shown_table_kinds[table.id] <= {"measure"})
    }

    def group(children: list[Node]) -> dict[str, list[Node]]:
        by_model: dict[str, list[Node]] = {}
        for child in _dedup_sorted(children):
            key = child.schema_name if child.schema_name in model_keys else ""
            by_model.setdefault(key, []).append(child)
        return by_model

    return models, group(tables), group(measures), _dedup_filters(filters), measure_home_ids


def _report_page(graph: LineageGraph, node: Node, out_path: Path) -> str:
    """Report page grouped by source model: one ``##`` section per model the
    report draws from, its referenced tables and measures listed beneath with
    unprefixed names — the shape that answers "which model does this report use,
    and which tables/measures from it?". Power BI report internals (pages,
    visuals) are folded away by ``collapse_visuals``.
    """
    models, tables_by_model, measures_by_model, filters, measure_home_ids = _report_refs(graph, node)

    def model_link(m: Node) -> str:
        return f"[{_link_text(m.display)}]({doc_relpath(m)})"

    def child_lines(children: list[Node], heading: str, *, prefixed: bool) -> list[str]:
        if not children:
            return []
        label = (lambda n: n.qualified_display) if prefixed else (lambda n: n.display)
        return [
            f"**{heading}**",
            "",
            *[f"- [{_link_text(label(c))}]({doc_relpath(c)})" for c in children],
            "",
        ]

    def table_lines(tbls: list[Node], *, prefixed: bool) -> list[str]:
        # split data tables (data the report shows/filters) from measure home
        # tables (present only because a bound measure lives there) — issue #27
        data = [t for t in tbls if t.id not in measure_home_ids]
        home = [t for t in tbls if t.id in measure_home_ids]
        return child_lines(data, "Tables used", prefixed=prefixed) + child_lines(
            home, "Measure home tables", prefixed=prefixed
        )

    parts = [_front_matter(graph, node), "", f"# {_text(node.qualified_display)}", ""]

    for model in models:
        tbls = tables_by_model.get(model.name, [])
        meas = measures_by_model.get(model.name, [])
        parts += [f"## {model_link(model)}", ""]
        parts += table_lines(tbls, prefixed=False)
        parts += child_lines(meas, "Measures used", prefixed=False)
        if not tbls and not meas:
            parts += ["_No tables or measures statically resolvable from this model._", ""]

    # Fallback: children whose model isn't among the report's feeding models —
    # keep their schema prefix (there's no model heading to establish it) and
    # never drop them (hard rule 4).
    other_tables = tables_by_model.get("", [])
    other_measures = measures_by_model.get("", [])
    if other_tables or other_measures:
        parts += ["## Other referenced objects", ""]
        parts += table_lines(other_tables, prefixed=True)
        parts += child_lines(other_measures, "Measures used", prefixed=True)

    if not models and not other_tables and not other_measures and not filters:
        parts += ["_No model, tables, or measures statically resolvable from this report._", ""]

    # Fields referenced only through a report/page/visual filter — a real
    # dependency the displayed lists deliberately omit (issue #26). One line per
    # distinct filtered field: linked table/measure, the field, and its scope.
    if filters:
        parts += ["## Filters", ""]
        for target, prop, scope in filters:
            field = f" · `{_cell(prop)}`" if prop else ""
            scope_note = f" · _{_cell(scope)} filter_" if scope else ""
            parts += [f"- [{_link_text(target.display)}]({doc_relpath(target)}){field}{scope_note}"]
        parts += [""]

    # Surface any bindings that stayed ambiguous (never silently dropped, hard rule 4).
    unresolved = node.metadata.get("unresolved_bindings")
    if unresolved:
        parts += ["## Unresolved bindings", ""]
        parts += ["_These fields couldn't be matched to a model — verify manually:_", ""]
        parts += [f"- `{_cell(b.get('entity', ''))}.{_cell(b.get('property', ''))}`" for b in unresolved]
        parts += [""]

    parts += ["## Business Intent", "", INTENT_BEGIN, _existing_intent(out_path), INTENT_END, ""]
    return "\n".join(parts)


def _existing_intent(path: Path) -> str:
    if not path.is_file():
        return _DEFAULT_INTENT
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # The intent block is hand-edited, and a Windows ANSI editor saving a
        # smart quote / en-dash writes cp1252 bytes. Never crash the whole
        # build over one docs page: retry cp1252 (covers every 8-bit byte the
        # ANSI case produces), then fall back to a lossy best-effort decode.
        try:
            text = path.read_text(encoding="cp1252")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="replace")
    match = _INTENT_RE.search(text)
    if match is None:
        return _DEFAULT_INTENT
    return match.group(1).strip("\n")


_MAX_SOURCE_CHARS = 100_000


def _code_fence(code: str) -> str:
    """A backtick fence longer than any backtick run in `code` (min 3), so a
    stray ``` inside the source can't terminate the block early."""
    longest = run = 0
    for ch in code:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    return "`" * max(3, longest + 1)


def _source_section(node: Node) -> str:
    """The defining SQL (CREATE statement / proc body) as a fenced, copyable
    code block. Material's content.code.copy adds the copy button for free."""
    code = node.source_code
    truncated = len(code) > _MAX_SOURCE_CHARS
    if truncated:
        code = code[:_MAX_SOURCE_CHARS]
    fence = _code_fence(code)
    lines = ["## Source", ""]
    if node.source_file:
        lines += [f"_`{node.source_file}`_", ""]
    lines += [f"{fence}sql", code, fence]
    if truncated:
        lines += ["", f"> ⚠ Source truncated to {_MAX_SOURCE_CHARS:,} characters; see the source file."]
    return "\n".join(lines)


def _dax_section(node: Node) -> str:
    """A measure's defining DAX as `Measure Name = <expression>` (the stored
    metadata["dax"] is the RHS only). Fence sized to the content so a stray
    backtick can't close the block early (mirrors _source_section)."""
    full_dax = f"{node.display} = {node.metadata['dax']}"
    fence = _code_fence(full_dax)
    return f"## DAX\n\n{fence}dax\n{full_dax}\n{fence}"


def render_node_page(
    graph: LineageGraph,
    node: Node,
    out_path: Path,
    *,
    used_measures: set[str] | None = None,
    direct_upstream: dict[str, list[str]] | None = None,
    edge_info: tuple[dict, dict] | None = None,
) -> str:
    """Full markdown page for one node, carrying forward any existing
    Business Intent block from out_path.

    ``used_measures`` / ``direct_upstream`` / ``edge_info`` are whole-graph computations
    that :func:`render_markdown` precomputes once and threads in; when omitted (a
    standalone call) they're derived on demand.
    """
    if used_measures is None:
        used_measures = _used_measure_ids(graph)
    if direct_upstream is None:
        direct_upstream = _direct_upstream(graph)
    if edge_info is None:
        edge_info = _lineage_edge_info(graph)
    if node.node_type is NodeType.REPORT:
        return _report_page(graph, node, out_path)
    has_dax = node.node_type is NodeType.MEASURE and node.metadata.get("dax")
    parts = [
        _front_matter(graph, node),
        "",
        f"# {_text(node.qualified_display)}",
        "",
        # description imported from the source model (TMDL/BIM), if any —
        # model-supplied text, so HTML-escaped before it becomes page markup
        *(
            [f"_{_text(' '.join(node.metadata['description'].split()))}_", ""]
            if node.metadata.get("description")
            else []
        ),
        *(
            [f"**Storage mode:** {_STORAGE_LABELS.get(mode, mode.title())}", ""]
            if node.node_type is NodeType.PBI_TABLE and (mode := node.metadata.get("storage_mode"))
            else []
        ),
        # a dedicated measure/calculation "home" table (no data columns) — issue #27
        *(
            ["**Measure home table** — hosts measures, holds no data columns.", ""]
            if node.node_type is NodeType.PBI_TABLE and node.metadata.get("measure_table")
            else []
        ),
        # defining code right under the description: SQL for tables/views/procs,
        # DAX for measures — the first thing a reader wants on the page.
        *([_source_section(node), ""] if node.source_code else []),
        *([_dax_section(node), ""] if has_dax else []),
        # advisory badge on a measure nothing references or shows
        *(
            [f"> ⚠ **Unused** — {_UNUSED_MEASURE_CAVEAT}", ""]
            if node.node_type is NodeType.MEASURE and node.id not in used_measures
            else []
        ),
        *([_contract_section(node), ""] if node.node_type in _CONTRACT_TYPES else []),
        # fact × dimension relationship matrix, semantic models only
        *([_relationship_grid(graph, node), ""] if node.node_type is NodeType.SEMANTIC_MODEL else []),
        # dead-measure roll-up for cleanup, semantic models only ("" when none)
        *(
            [unused_section, ""]
            if node.node_type is NodeType.SEMANTIC_MODEL
            and (unused_section := _unused_measures_section(graph, node, used_measures))
            else []
        ),
        # full "trace back to source" tree on measures + model-facing gold objects
        *([_upstream_section(graph, node, direct_upstream), ""] if _wants_upstream_tree(graph, node) else []),
    ]
    parts += [
        "## Lineage",
        "",
        _lineage_table(
            graph, node, graph.upstream(node.id, depth=1), "Upstream", edge_info[0].get(node.id, {})
        ),
        "",
        _lineage_table(
            graph, node, graph.downstream(node.id, depth=1), "Downstream", edge_info[1].get(node.id, {})
        ),
        "",
        "## Business Intent",
        "",
        INTENT_BEGIN,
        _existing_intent(out_path),
        INTENT_END,
        "",
    ]
    return "\n".join(parts)


def _index_page(graph: LineageGraph, project_name: str) -> str:
    lines = [f"# {project_name}", ""]
    lines.append("End-to-end data lineage documentation, generated by `coop-data-doc`.")
    lines.append("")
    lines.append("## Estate Overview")
    lines.append("")
    lines.append("| Object Type | Count |")
    lines.append("| --- | --- |")
    counts: dict[NodeType, int] = {}
    for node in graph.nodes.values():
        counts[node.node_type] = counts.get(node.node_type, 0) + 1
    for node_type in NodeType:
        if node_type in counts:
            lines.append(f"| {_TYPE_TITLES[node_type]} | {counts[node_type]} |")
    # Reports overview: which reports use which model(s), answerable at a glance
    # without opening every report page. Links are root-relative (this is
    # index.md at the docs root, not a node-type subdir).
    reports = sorted(
        (n for n in graph.nodes.values() if n.node_type is NodeType.REPORT),
        key=lambda n: (n.display.lower(), n.id),
    )
    if reports:
        lines.append("")
        lines.append("## Reports overview")
        lines.append("")
        lines.append("| Report | Draws from | Tables | Measures |")
        lines.append("| --- | --- | --- | --- |")
        for report in reports:
            models, tables_by_model, measures_by_model, _filters, _mh = _report_refs(graph, report)
            model_links = (
                ", ".join(f"[{_link_text(m.display)}](semantic_model/{slug(m.id)}.md)" for m in models)
                or "_none_"
            )
            n_tables = sum(len(v) for v in tables_by_model.values())
            n_measures = sum(len(v) for v in measures_by_model.values())
            report_link = f"[{_link_text(report.display)}](report/{slug(report.id)}.md)"
            lines.append(f"| {report_link} | {model_links} | {n_tables} | {n_measures} |")
    unresolved = sorted(
        node_id
        for node_id, node in graph.nodes.items()
        if node.metadata.get("unresolved") or node.metadata.get("partition_source_unresolved")
    )
    if unresolved:
        lines.append("")
        lines.append("## Unresolved References")
        lines.append("")
        for node_id in unresolved:
            node = graph.nodes[node_id]
            lines.append(f"- [{node_id}]({node.node_type.value}/{slug(node_id)}.md)")
    lines.extend(
        [
            "",
            "## Adding your own notes",
            "",
            "Each object page has two kinds of description:",
            "",
            "- **Description** (italic, under the title) is imported automatically from the "
            "Power BI model — the `///` doc comment on a TMDL table/column/measure (or the "
            "`description` field in a `.bim`). Fill those in inside Power BI and they appear "
            "here on the next build.",
            "- **Business Intent** (the section at the bottom of every page) is for *your* "
            "notes — ownership, gotchas, why an object exists. Edit the text between the "
            "`<!-- intent:begin -->` / `<!-- intent:end -->` markers in the matching "
            "`.md` file under the docs folder; it is preserved verbatim across rebuilds.",
            "",
            "After editing, rerun `coop-data-doc build` (or `update`) to regenerate the site.",
        ]
    )
    lines.append("")
    return "\n".join(lines)


def write_diagnostics(out_dir: Path, diagnostics, project_name: str) -> Path:
    """Write the human-readable diagnostics page (diagnostics.md) for the
    HTML portal. The machine-readable diagnostics.json is written by the CLI.
    """
    path = Path(out_dir) / "diagnostics.md"
    path.write_text(diagnostics.to_markdown(project_name), encoding="utf-8", newline="\n")
    return path


def render_markdown(
    graph: LineageGraph,
    out_dir: Path,
    project_name: str,
    *,
    on_node: Callable[..., None] | None = None,
) -> list[Path]:
    """Write one page per node plus index.md and manifest.json; returns
    the sorted list of written paths. Safe to re-run over existing output.

    ``on_node`` (optional) is called once per node for progress reporting.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # whole-graph scans hoisted out of the per-node loop (computed once, not per page)
    used_measures = _used_measure_ids(graph)
    direct_upstream = _direct_upstream(graph)
    edge_info = _lineage_edge_info(graph)  # one pass; kills the per-node full-edge scan
    written: list[Path] = []
    for node_id in sorted(graph.nodes):
        node = graph.nodes[node_id]
        if on_node:
            on_node(node_id)
        page_dir = out_dir / node.node_type.value
        page_dir.mkdir(parents=True, exist_ok=True)
        page_path = page_dir / f"{slug(node_id)}.md"
        page_path.write_text(
            render_node_page(
                graph,
                node,
                page_path,
                used_measures=used_measures,
                direct_upstream=direct_upstream,
                edge_info=edge_info,
            ),
            encoding="utf-8",
            newline="\n",
        )
        written.append(page_path)

    index_path = out_dir / "index.md"
    index_path.write_text(_index_page(graph, project_name), encoding="utf-8", newline="\n")
    written.append(index_path)

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(to_json_str(graph), encoding="utf-8", newline="\n")
    written.append(manifest_path)

    # prune pages of objects that no longer exist (e.g. a dropped view) —
    # only inside the node-type directories this renderer manages, so any
    # hand-authored files elsewhere in the docs tree are never touched
    written_set = set(written)
    managed = {node_type.value for node_type in NodeType}
    for subdir in sorted(out_dir.iterdir()):
        if not subdir.is_dir() or subdir.name not in managed:
            continue
        for page in sorted(subdir.glob("*.md")):
            if page not in written_set:
                page.unlink()
        if not any(subdir.iterdir()):
            subdir.rmdir()
    return sorted(written)
