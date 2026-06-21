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

from coop_data_doc.graph.model import LineageGraph, Node, NodeType, normalize_identifier
from coop_data_doc.graph.serialize import to_json_str
from coop_data_doc.render.mermaid import (
    doc_relpath,
    estate_flowchart,
    local_flowchart,
    slug,
)

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


def _cell(value: str) -> str:
    """Make free text safe inside a Markdown table cell: escape pipes and
    collapse newlines (an unescaped '|' or newline breaks the table)."""
    return (value or "").replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


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


def _lineage_table(graph: LineageGraph, node: Node, ids: list[str], direction: str) -> str:
    lines = [f"### {direction}", ""]
    if not ids:
        lines.append(f"_No {direction.lower()} objects._")
        return "\n".join(lines)
    lines.append("| Object | Type | Via | Evidence |")
    lines.append("| --- | --- | --- | --- |")
    edge_info: dict[str, tuple[str, str]] = {}
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
    """Render "Joel's Relationship Grid" — a fact × dimension matrix for a
    semantic model, built from its parsed relationships (``metadata["relationships"]``,
    each a ``{"from": "<table>.<col>", "to": "<table>.<col>"}`` pair).

    Power BI authors relationships from the *many* side to the *one* side, so
    each ``from`` table is a fact (a grid column) and each ``to`` table is a
    dimension (a grid row); a green dot marks the cell where the two relate.
    Tables resolve to their page links and original-case display names; a
    table with no node (e.g. unparsed) falls back to its bare name.
    """
    lines = ["## Joel's Relationship Grid", ""]
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
    lines.append("| Dimension \\ Fact | " + " | ".join(label(f) for f in fact_cols) + " |")
    lines.append("| --- | " + " | ".join([":---:"] * len(fact_cols)) + " |")
    for dim in dim_rows:
        cells = (cell(dim, fact) for fact in fact_cols)
        lines.append(f"| {label(dim)} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _existing_intent(path: Path) -> str:
    if not path.is_file():
        return _DEFAULT_INTENT
    match = _INTENT_RE.search(path.read_text(encoding="utf-8"))
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


def render_node_page(graph: LineageGraph, node: Node, out_path: Path) -> str:
    """Full markdown page for one node, carrying forward any existing
    Business Intent block from out_path.
    """
    parts = [
        _front_matter(graph, node),
        "",
        f"# {node.qualified_display}",
        "",
        # description imported from the source model (TMDL/BIM), if any
        *(
            [f"_{' '.join(node.metadata['description'].split())}_", ""]
            if node.metadata.get("description")
            else []
        ),
        *(
            [f"**Storage mode:** {_STORAGE_LABELS.get(mode, mode.title())}", ""]
            if node.node_type is NodeType.PBI_TABLE and (mode := node.metadata.get("storage_mode"))
            else []
        ),
        *([_contract_section(node), ""] if node.node_type in _CONTRACT_TYPES else []),
        # fact × dimension relationship matrix, semantic models only
        *([_relationship_grid(graph, node), ""] if node.node_type is NodeType.SEMANTIC_MODEL else []),
    ]
    # the defining SQL for tables/views/procs (no source_code -> no section)
    if node.source_code:
        parts += [_source_section(node), ""]
    parts += [
        "## Lineage",
        "",
        _lineage_table(graph, node, graph.upstream(node.id, depth=1), "Upstream"),
        "",
        _lineage_table(graph, node, graph.downstream(node.id, depth=1), "Downstream"),
        "",
        "## Local Flow",
        "",
        "```mermaid",
        local_flowchart(graph, node.id),
        "```",
        "",
        "## Business Intent",
        "",
        INTENT_BEGIN,
        _existing_intent(out_path),
        INTENT_END,
        "",
    ]
    if node.node_type is NodeType.MEASURE and node.metadata.get("dax"):
        dax = node.metadata["dax"]
        # size the fence to the content so any stray backticks in the DAX
        # can't terminate the block early (mirrors _source_section)
        fence = _code_fence(dax)
        parts.insert(
            parts.index("## Lineage"),
            f"## DAX\n\n{fence}dax\n{dax}\n{fence}\n",
        )
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
    chart = estate_flowchart(graph)
    if chart is not None:
        lines.append("")
        lines.append("## Data Flow")
        lines.append("")
        lines.append("```mermaid")
        lines.append(chart)
        lines.append("```")
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
    written: list[Path] = []
    for node_id in sorted(graph.nodes):
        node = graph.nodes[node_id]
        if on_node:
            on_node(node_id)
        page_dir = out_dir / node.node_type.value
        page_dir.mkdir(parents=True, exist_ok=True)
        page_path = page_dir / f"{slug(node_id)}.md"
        page_path.write_text(render_node_page(graph, node, page_path), encoding="utf-8", newline="\n")
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
