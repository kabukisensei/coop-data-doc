from pathlib import Path

import yaml

from coop_data_doc.graph import Column, Edge, EdgeType, LineageGraph, Node, NodeType
from coop_data_doc.render.markdown import INTENT_BEGIN, INTENT_END, render_markdown
from coop_data_doc.render.mermaid import estate_flowchart, local_flowchart, slug


def page_path(out_dir: Path, node_id: str) -> Path:
    """Locate a node's generated page, accounting for the hashed slug."""
    node_type = node_id.split(":", 1)[0]
    return out_dir / node_type / f"{slug(node_id)}.md"


def make_node(node_type, schema, name, **kwargs):
    return Node(
        id=Node.make_id(node_type, schema, name),
        node_type=node_type,
        name=name,
        schema_name=schema,
        **kwargs,
    )


def build_graph() -> LineageGraph:
    """silver -> proc -> gold -> view -> pbi_table -> model, + visual"""
    g = LineageGraph()
    silver = g.add_node(make_node(NodeType.SILVER_TABLE, "silver", "customers"))
    proc = g.add_node(make_node(NodeType.STORED_PROC, "dbo", "usp_load", source_file="procs/usp_load.sql"))
    gold = g.add_node(
        make_node(
            NodeType.GOLD_TABLE,
            "dbo",
            "fact_sales",
            source_file="tables/fact_sales.sql",
            columns=[
                Column(name="order_id", data_type="INT", nullable=False, constraints=["PK"]),
                Column(name="order_total", data_type="DECIMAL(18, 2)", nullable=False),
            ],
        )
    )
    view = g.add_node(make_node(NodeType.VIEW, "salespm", "dim_customer", source_file="views/dim.sql"))
    model = g.add_node(make_node(NodeType.SEMANTIC_MODEL, "", "salespm"))
    pbit = g.add_node(make_node(NodeType.PBI_TABLE, "salespm", "dim_customer"))
    vis = g.add_node(make_node(NodeType.VISUAL, "salespm", "abc123"))
    g.add_edge(
        Edge(
            source_id=proc.id,
            target_id=silver.id,
            edge_type=EdgeType.READS,
            evidence="procs/usp_load.sql: reads silver.customers",
        )
    )
    g.add_edge(
        Edge(
            source_id=proc.id,
            target_id=gold.id,
            edge_type=EdgeType.WRITES,
            evidence="procs/usp_load.sql: writes dbo.fact_sales",
        )
    )
    g.add_edge(
        Edge(
            source_id=view.id,
            target_id=gold.id,
            edge_type=EdgeType.READS,
            evidence="views/dim.sql: FROM dbo.fact_sales",
        )
    )
    g.add_edge(Edge(source_id=view.id, target_id=pbit.id, edge_type=EdgeType.FEEDS, evidence="linker: exact"))
    g.add_edge(Edge(source_id=pbit.id, target_id=model.id, edge_type=EdgeType.FEEDS))
    g.add_edge(Edge(source_id=vis.id, target_id=pbit.id, edge_type=EdgeType.VISUALIZES))
    return g


def test_front_matter_strict_yaml(tmp_path: Path):
    graph = build_graph()
    render_markdown(graph, tmp_path, "Test Estate")
    page = page_path(tmp_path, "view:salespm.dim_customer").read_text(encoding="utf-8")
    front = page.split("---")[1]
    data = yaml.safe_load(front)
    assert list(data) == [
        "id",
        "type",
        "name",
        "schema",
        "layer",
        "source_file",
        "path",
        "upstream_inputs",
        "downstream_dependents",
        "tags",
    ]
    assert data["id"] == "view:salespm.dim_customer"
    assert data["path"] == f"view/{slug('view:salespm.dim_customer')}.md"
    assert data["upstream_inputs"] == ["gold_table:dbo.fact_sales"]
    assert data["downstream_dependents"] == ["pbi_table:salespm.dim_customer"]


def test_contract_table(tmp_path: Path):
    graph = build_graph()
    render_markdown(graph, tmp_path, "Test Estate")
    page = page_path(tmp_path, "gold_table:dbo.fact_sales").read_text(encoding="utf-8")
    assert "| order_id | INT | NOT NULL, PK |  |" in page
    assert "| order_total | DECIMAL(18, 2) | NOT NULL |  |" in page


def test_intent_preserved_across_regeneration(tmp_path: Path):
    graph = build_graph()
    render_markdown(graph, tmp_path, "Test Estate")
    page = page_path(tmp_path, "view:salespm.dim_customer")
    text = page.read_text(encoding="utf-8")
    custom = "Feeds the Sales & PM model. Owned by the analytics coop."
    text = text.replace(
        "_Add a short description of what this object is for and who relies on it._",
        custom,
    )
    page.write_text(text, encoding="utf-8", newline="\n")

    render_markdown(graph, tmp_path, "Test Estate")
    regenerated = page.read_text(encoding="utf-8")
    assert custom in regenerated
    assert INTENT_BEGIN in regenerated and INTENT_END in regenerated


def test_index_and_manifest(tmp_path: Path):
    graph = build_graph()
    written = render_markdown(graph, tmp_path, "Test Estate")
    index = (tmp_path / "index.md").read_text(encoding="utf-8")
    assert "| Views | 1 |" in index
    assert "```mermaid" in index
    assert (tmp_path / "manifest.json").is_file()
    assert all(path.is_file() for path in written)


def test_local_flowchart_deterministic_and_linked():
    graph = build_graph()
    chart = local_flowchart(graph, "view:salespm.dim_customer")
    assert chart == local_flowchart(graph, "view:salespm.dim_customer")
    assert chart.startswith("flowchart LR")
    assert "click" in chart
    assert "stroke-width:3px" in chart
    # the focus node itself must not get a click link
    focus_alias = [line.split()[1] for line in chart.splitlines() if "stroke-width" in line][0]
    assert f"click {focus_alias} " not in chart


def test_mermaid_label_neutralizes_significant_chars():
    g = LineageGraph()
    model = g.add_node(make_node(NodeType.SEMANTIC_MODEL, "", "finance"))
    measure = g.add_node(make_node(NodeType.MEASURE, "finance", 'gross | net < tax > "q1"'))
    g.add_edge(Edge(source_id=measure.id, target_id=model.id, edge_type=EdgeType.FEEDS))
    chart = local_flowchart(g, measure.id)
    # find the node-definition line and extract its quoted label
    node_lines = [ln for ln in chart.splitlines() if "finance.gross" in ln]
    assert node_lines
    label = node_lines[0].split('"')[1]  # text between the first pair of quotes
    assert "finance.gross" in label
    for bad in ("|", "<", ">", '"'):
        assert bad not in label  # mermaid-significant chars neutralized


def test_estate_flowchart_layers():
    chart = estate_flowchart(build_graph())
    assert chart is not None
    assert 'subgraph Silver["Silver"]' in chart
    assert 'subgraph Views["Views"]' in chart


def test_render_determinism(tmp_path: Path):
    graph = build_graph()
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    render_markdown(graph, dir_a, "Test Estate")
    render_markdown(graph, dir_b, "Test Estate")
    files_a = sorted(p.relative_to(dir_a) for p in dir_a.rglob("*") if p.is_file())
    files_b = sorted(p.relative_to(dir_b) for p in dir_b.rglob("*") if p.is_file())
    assert files_a == files_b
    for relative in files_a:
        assert (dir_a / relative).read_bytes() == (dir_b / relative).read_bytes()


def test_slug_is_filesystem_safe():
    import re

    # readable prefix + 8-hex id hash; no filesystem-illegal characters
    s = slug("pbi_table:sales and project management.dim_customer")
    assert s.startswith("sales-and-project-management-dim_customer-")
    assert re.search(r"-[0-9a-f]{8}$", s)
    assert not re.search(r'[<>:"/\\|?*]', s)


def test_slug_handles_windows_illegal_chars_and_is_unique():
    import re

    # the real Windows crash: a DAX measure name with '|' and '+'
    bad = "measure:finance.+ forecast fixed layout | forecast statement"
    s = slug(bad)
    assert not re.search(r'[<>:"/\\|?*]', s)  # safe to write on Windows
    assert "|" not in s and " " not in s
    # distinct ids never collide to the same filename
    assert slug("measure:finance.a") != slug("measure:finance.b")
    # over-long names are bounded
    assert len(slug("view:dbo." + "x" * 500)) <= 90


def test_render_does_not_crash_on_illegal_measure_name(tmp_path: Path):
    g = LineageGraph()
    model = g.add_node(make_node(NodeType.SEMANTIC_MODEL, "", "finance"))
    measure = g.add_node(
        make_node(
            NodeType.MEASURE,
            "finance",
            "+ Forecast Fixed Layout | Forecast Statement",
            metadata={"dax": "SUM(x)"},
        )
    )
    g.add_edge(Edge(source_id=measure.id, target_id=model.id, edge_type=EdgeType.FEEDS))
    written = render_markdown(g, tmp_path, "Finance")  # must not raise OSError
    assert any(p.suffix == ".md" for p in written)
    assert page_path(tmp_path, measure.id).is_file()


def test_orphaned_pages_pruned_on_rerender(tmp_path: Path):
    graph = build_graph()
    render_markdown(graph, tmp_path, "Test Estate")
    view_page = page_path(tmp_path, "view:salespm.dim_customer")
    hand_authored = tmp_path / "view" / "notes-from-a-human.txt"
    hand_authored.write_text("keep me", encoding="utf-8")
    assert view_page.is_file()

    # the view disappears from the estate
    del graph.nodes["view:salespm.dim_customer"]
    graph.edges = [e for e in graph.edges if "view:salespm.dim_customer" not in (e.source_id, e.target_id)]
    render_markdown(graph, tmp_path, "Test Estate")

    assert not view_page.exists()  # orphan pruned
    assert hand_authored.is_file()  # non-.md files untouched


def test_site_nav_grouped_by_layer():
    from coop_data_doc.render.site import _nav_section

    g = LineageGraph()
    g.add_node(make_node(NodeType.BRONZE_TABLE, "d365", "src", metadata={"layer": "bronze"}))
    gold_tbl = make_node(NodeType.GOLD_TABLE, "dwm", "fact", metadata={"layer": "gold"})
    g.add_node(gold_tbl)
    g.add_node(make_node(NodeType.VIEW, "salespm", "v_fact", source_file="v.sql", metadata={"layer": "gold"}))
    g.add_node(make_node(NodeType.MEASURE, "salespm", "total", metadata={"dax": "1"}))
    nav = _nav_section(g)
    assert "- Overview: index.md" in nav
    assert "- Bronze Layer:" in nav
    assert "- Gold Layer:" in nav
    assert "- Semantic Models:" in nav
    # within a layer, grouped by object type
    gold_idx = nav.index("Gold Layer:")
    assert "Tables:" in nav[gold_idx:]
    assert "Views:" in nav[gold_idx:]


def test_display_name_schema_qualified_original_case(tmp_path: Path):
    g = LineageGraph()
    # node id/name normalized (lowercase) but display_name keeps original case
    g.add_node(
        Node(
            id=Node.make_id(NodeType.GOLD_TABLE, "dim", "Practice"),
            node_type=NodeType.GOLD_TABLE,
            name="practice",
            schema_name="dim",
            display_name="Practice",
            source_file="dim/Practice.sql",
            metadata={"layer": "gold"},
        )
    )
    render_markdown(g, tmp_path, "Test")
    page = page_path(tmp_path, "gold_table:dim.practice").read_text(encoding="utf-8")
    assert "# dim.Practice\n" in page  # schema-qualified H1, original case
    assert "# dim.Practice `gold_table`" not in page  # type label dropped from the title
    # mermaid label uses the same display
    from coop_data_doc.render.mermaid import local_flowchart

    assert "dim.Practice" in local_flowchart(g, "gold_table:dim.practice")


def test_display_falls_back_to_name_when_absent():
    n = Node(id="view:s.x", node_type=NodeType.VIEW, name="x", schema_name="s")
    assert n.display == "x"
    assert n.qualified_display == "s.x"


def test_branding_logo_and_colors(tmp_path: Path):
    from coop_data_doc.config import Branding
    from coop_data_doc.render.site import write_mkdocs_config

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "index.md").write_text("# x", encoding="utf-8")
    logo = tmp_path / "brand.png"
    logo.write_bytes(b"\x89PNG\r\n\x1a\n fake")
    g = build_graph()
    cfg = write_mkdocs_config(
        docs,
        tmp_path / "site",
        "Test",
        g,
        branding=Branding(logo="brand.png", primary_color="#004060", accent_color="#e04020"),
        config_dir=tmp_path,
    )
    text = cfg.read_text(encoding="utf-8")
    assert "logo: assets/images/logo.png" in text
    assert "favicon: assets/images/favicon.png" in text  # logo reused as favicon
    assert (docs / "assets" / "images" / "logo.png").is_file()
    brand_css = (docs / "assets" / "stylesheets" / "brand.css").read_text(encoding="utf-8")
    assert "--md-primary-fg-color: #004060;" in brand_css
    assert "--md-accent-fg-color: #e04020;" in brand_css


def test_no_branding_is_clean(tmp_path: Path):
    from coop_data_doc.render.site import write_mkdocs_config

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "index.md").write_text("# x", encoding="utf-8")
    cfg = write_mkdocs_config(docs, tmp_path / "site", "Test", build_graph())
    text = cfg.read_text(encoding="utf-8")
    assert "logo:" not in text  # no logo line when unbranded
    assert (docs / "assets" / "stylesheets" / "brand.css").is_file()  # empty brand.css still written
