from pathlib import Path

import yaml

from coop_data_doc.graph import Column, Edge, EdgeType, LineageGraph, Node, NodeType
from coop_data_doc.render.markdown import INTENT_BEGIN, INTENT_END, render_markdown
from coop_data_doc.render.paths import slug


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


def _ref(target, prop, kind, role="shown", scope="visual"):
    """A report field-ref (the role/kind/scope-aware summary the report page renders from)."""
    return {"target": target, "entity": "", "property": prop, "kind": kind, "role": role, "scope": scope}


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
    view = g.add_node(make_node(NodeType.VIEW, "sales", "dim_customer", source_file="views/dim.sql"))
    model = g.add_node(make_node(NodeType.SEMANTIC_MODEL, "", "sales"))
    pbit = g.add_node(make_node(NodeType.PBI_TABLE, "sales", "dim_customer"))
    vis = g.add_node(make_node(NodeType.VISUAL, "sales", "abc123"))
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
    page = page_path(tmp_path, "view:sales.dim_customer").read_text(encoding="utf-8")
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
    assert data["id"] == "view:sales.dim_customer"
    assert data["path"] == f"view/{slug('view:sales.dim_customer')}.md"
    assert data["upstream_inputs"] == ["gold_table:dbo.fact_sales"]
    assert data["downstream_dependents"] == ["pbi_table:sales.dim_customer"]


def test_contract_table(tmp_path: Path):
    graph = build_graph()
    render_markdown(graph, tmp_path, "Test Estate")
    page = page_path(tmp_path, "gold_table:dbo.fact_sales").read_text(encoding="utf-8")
    assert "| order_id | INT | NOT NULL, PK |  |" in page
    assert "| order_total | DECIMAL(18, 2) | NOT NULL |  |" in page


def test_intent_preserved_across_regeneration(tmp_path: Path):
    graph = build_graph()
    render_markdown(graph, tmp_path, "Test Estate")
    page = page_path(tmp_path, "view:sales.dim_customer")
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
    assert (tmp_path / "manifest.json").is_file()
    assert all(path.is_file() for path in written)


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

    # readable prefix + 8-hex id hash; only [a-z0-9_-] in the readable portion
    s = slug("pbi_table:sales analytics.dim_customer")
    assert s.startswith("sales-analytics-dim_customer-")
    assert re.search(r"-[0-9a-f]{8}$", s)
    assert not re.search(r'[<>:"/\\|?*]', s)
    assert re.fullmatch(r"[a-z0-9_-]+", s)  # nothing outside the URL-safe class


def test_slug_strips_url_hostile_chars():
    import re

    # %, +, parens, &, # are filesystem-legal but URL-hostile: a raw % is an
    # invalid percent-escape, an unbalanced ) breaks a Markdown link target,
    # + flips to a space in query-string contexts (issue #25).
    s = slug("measure:finance.total margin % (aip) + adj & more #1")
    assert re.fullmatch(r"[a-z0-9_-]+", s)  # only URL-safe chars survive
    for ch in "%+()&#":
        assert ch not in s
    assert "--" not in s.rstrip("-")  # runs collapsed, hash suffix intact
    # distinct ids with different hostile chars still never collide
    assert slug("measure:m.a%b") != slug("measure:m.a+b")


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


def test_url_hostile_measure_name_round_trips(tmp_path: Path):
    # a measure named with %, +, and parens must produce a page whose every
    # inbound Markdown link resolves to a real file (issue #25): the model page
    # links to the measure, and that target must exist on disk.
    import re

    g = LineageGraph()
    model = g.add_node(make_node(NodeType.SEMANTIC_MODEL, "", "finance", display_name="Finance"))
    measure = g.add_node(
        make_node(
            NodeType.MEASURE,
            "finance",
            "total margin % (aip) + adj",
            display_name="Total Margin % (AIP) + Adj",
            metadata={"dax": "1"},
        )
    )
    g.add_edge(Edge(source_id=measure.id, target_id=model.id, edge_type=EdgeType.FEEDS))
    render_markdown(g, tmp_path, "Finance")
    measure_page = page_path(tmp_path, measure.id)
    assert measure_page.is_file()  # page written despite the hostile name
    model_page_text = page_path(tmp_path, model.id).read_text(encoding="utf-8")
    # every measure/table link on the model page must point at a file that exists
    for target in re.findall(r"\]\(\.\./([a-z0-9_]+/[a-z0-9_-]+\.md)\)", model_page_text):
        assert (tmp_path / target).is_file(), f"dangling link: {target}"
    # and the link specifically to our hostile-named measure resolves
    assert f"measure/{slug(measure.id)}.md" in model_page_text


def test_orphaned_pages_pruned_on_rerender(tmp_path: Path):
    graph = build_graph()
    render_markdown(graph, tmp_path, "Test Estate")
    view_page = page_path(tmp_path, "view:sales.dim_customer")
    hand_authored = tmp_path / "view" / "notes-from-a-human.txt"
    hand_authored.write_text("keep me", encoding="utf-8")
    assert view_page.is_file()

    # the view disappears from the estate
    del graph.nodes["view:sales.dim_customer"]
    graph.edges = [e for e in graph.edges if "view:sales.dim_customer" not in (e.source_id, e.target_id)]
    render_markdown(graph, tmp_path, "Test Estate")

    assert not view_page.exists()  # orphan pruned
    assert hand_authored.is_file()  # non-.md files untouched


def test_site_nav_grouped_by_layer():
    from coop_data_doc.render.site import _nav_section

    g = LineageGraph()
    g.add_node(make_node(NodeType.BRONZE_TABLE, "d365", "src", metadata={"layer": "bronze"}))
    gold_tbl = make_node(NodeType.GOLD_TABLE, "mart", "fact", metadata={"layer": "gold"})
    g.add_node(gold_tbl)
    g.add_node(make_node(NodeType.VIEW, "sales", "v_fact", source_file="v.sql", metadata={"layer": "gold"}))
    g.add_node(make_node(NodeType.MEASURE, "sales", "total", metadata={"dax": "1"}))
    nav = _nav_section(g)
    assert "- Overview: index.md" in nav
    assert "- Bronze Layer:" in nav
    assert "- Gold Layer:" in nav
    assert "- Semantic Models:" in nav
    # within a layer, grouped by schema, then object type
    gold_idx = nav.index("Gold Layer:")
    assert '- "mart":' in nav  # the gold table's schema, nested under the layer
    assert '- "sales":' in nav  # the gold view's schema
    assert "Tables:" in nav[gold_idx:]
    assert "Views:" in nav[gold_idx:]
    # the table's type subgroup sits under its schema
    mart_idx = nav.index('"mart":')
    assert "Tables:" in nav[mart_idx:]


def test_site_nav_nests_tables_and_measures_under_each_model():
    from coop_data_doc.render.site import _nav_section

    g = LineageGraph()
    # two models, each with its own table + measure (schema_name == model key)
    g.add_node(make_node(NodeType.SEMANTIC_MODEL, "", "Finance", display_name="Finance"))
    g.add_node(make_node(NodeType.PBI_TABLE, "finance", "Region", display_name="Region"))
    g.add_node(make_node(NodeType.MEASURE, "finance", "Total", display_name="Total", metadata={"dax": "1"}))
    g.add_node(make_node(NodeType.SEMANTIC_MODEL, "", "Resource", display_name="Resource"))
    g.add_node(make_node(NodeType.PBI_TABLE, "resource", "Person", display_name="Person"))
    nav = _nav_section(g)
    # each model is its own subsection with nested Tables / Measures
    assert '- "Finance":' in nav and '- "Resource":' in nav
    fin = nav.index('"Finance":')
    res = nav.index('"Resource":')
    # Finance's table/measure appear under Finance, before Resource
    assert "finance-region" in nav and nav.index("finance-region") > fin
    assert "Tables:" in nav[fin:res] and "Measures:" in nav[fin:res]
    # Resource has no measures -> no Measures subgroup in its block
    assert "Measures:" not in nav[res:]


def test_site_nav_nests_reports_under_their_model():
    from coop_data_doc.render.site import _nav_section

    g = LineageGraph()
    model = g.add_node(make_node(NodeType.SEMANTIC_MODEL, "", "sales", display_name="Sales"))
    rpt = g.add_node(make_node(NodeType.REPORT, "", "dash", display_name="Sales Dashboard"))
    # the model --feeds--> report edge (from link_reports_to_models) drives nesting
    g.add_edge(Edge(source_id=model.id, target_id=rpt.id, edge_type=EdgeType.FEEDS))
    nav = _nav_section(g)
    assert "- Semantic Models:" in nav
    # the report nests under its model in a "Reports" subsection, not a top-level one
    model_idx = nav.index('"Sales":')
    reports_idx = nav.index("- Reports:")
    assert reports_idx > model_idx
    assert '"Sales Dashboard": report/' in nav  # labelled leaf link to the report page
    assert nav.index("dash") > reports_idx


def test_navkey_escapes_control_chars():
    # an interior newline (e.g. a bracketed T-SQL identifier spanning a line)
    # must be escaped so the nav stays valid YAML, not break the mkdocs build
    from coop_data_doc.render.site import _navkey

    key = _navkey("a\nb\tc")
    assert "\\n" in key and "\\t" in key
    assert "\n" not in key and "\t" not in key  # no raw control chars
    yaml.safe_load(f"{key}: x")  # loads without error


def test_upstream_tree_on_model_facing_gold_objects(tmp_path: Path):
    # the view + gold both feed the model, so both get a full back-to-source
    # tree; the upstream silver source (a leaf) does not.
    g = build_graph()
    render_markdown(g, tmp_path, "Test")
    view_page = page_path(tmp_path, "view:sales.dim_customer").read_text(encoding="utf-8")
    assert "## Upstream lineage" in view_page
    # the tree recurses to the source: gold -> proc -> silver, each linked + typed
    assert "[dbo.fact_sales](../gold_table/" in view_page and "`gold_table`" in view_page
    assert "[dbo.usp_load](../stored_proc/" in view_page
    assert "[silver.customers](../silver_table/" in view_page
    # nesting deepens with each hop
    gold_indent = (
        view_page.index("[dbo.fact_sales]")
        - view_page.rindex("\n", 0, view_page.index("[dbo.fact_sales]"))
        - 1
    )
    silver_indent = (
        view_page.index("[silver.customers]")
        - view_page.rindex("\n", 0, view_page.index("[silver.customers]"))
        - 1
    )
    assert silver_indent > gold_indent

    # a pure source (the silver table) is not a measure/gold/view -> no tree
    silver_page = page_path(tmp_path, "silver_table:silver.customers").read_text(encoding="utf-8")
    assert "## Upstream lineage" not in silver_page


def test_unused_measure_detection(tmp_path: Path):
    # a measure nothing references or shows is flagged on its own page and rolled
    # up on the model page; a used measure (referenced by another) is not.
    g = LineageGraph()
    g.add_node(make_node(NodeType.SEMANTIC_MODEL, "", "sales", display_name="Sales"))
    g.add_node(make_node(NodeType.MEASURE, "sales", "total sales", display_name="Total Sales"))
    g.add_node(make_node(NodeType.MEASURE, "sales", "orphan kpi", display_name="Orphan KPI"))
    g.add_node(make_node(NodeType.REPORT, "", "dash", display_name="Dash"))
    for mid in ("measure:sales.total sales", "measure:sales.orphan kpi"):
        g.add_edge(Edge(source_id=mid, target_id="semantic_model:sales", edge_type=EdgeType.FEEDS))
    # Total Sales is shown in a report -> used; Orphan KPI is referenced by nothing
    g.add_edge(
        Edge(source_id="report:dash", target_id="measure:sales.total sales", edge_type=EdgeType.VISUALIZES)
    )
    render_markdown(g, tmp_path, "Test")
    model_page = page_path(tmp_path, "semantic_model:sales").read_text(encoding="utf-8")
    assert "## Unused measures" in model_page
    # isolate just the section (the later Lineage table lists every measure)
    section = model_page.split("## Unused measures", 1)[1].split("\n## ", 1)[0]
    assert "[Orphan KPI](" in section
    assert "[Total Sales](" not in section  # used -> not listed

    orphan_page = page_path(tmp_path, "measure:sales.orphan kpi").read_text(encoding="utf-8")
    assert "⚠ **Unused**" in orphan_page
    used_page = page_path(tmp_path, "measure:sales.total sales").read_text(encoding="utf-8")
    assert "Unused" not in used_page


def test_upstream_tree_handles_cycle_and_diamond():
    # the guard against DAG re-convergence / cycles is the only non-trivial
    # logic in the tree; exercise both shapes directly so it can't regress.
    from coop_data_doc.render.markdown import _direct_upstream, _upstream_tree_text

    # cycle: view a reads b, view b reads a; focus gold c reads a
    g = LineageGraph()
    a = g.add_node(make_node(NodeType.VIEW, "s", "a"))
    b = g.add_node(make_node(NodeType.VIEW, "s", "b"))
    c = g.add_node(make_node(NodeType.GOLD_TABLE, "s", "c"))
    g.add_edge(Edge(source_id=c.id, target_id=a.id, edge_type=EdgeType.READS))
    g.add_edge(Edge(source_id=a.id, target_id=b.id, edge_type=EdgeType.READS))
    g.add_edge(Edge(source_id=b.id, target_id=a.id, edge_type=EdgeType.READS))
    text = "\n".join(_upstream_tree_text(g, c, _direct_upstream(g)))  # must terminate
    assert text.count("s.a") >= 2  # a expanded once, then revisited in the cycle
    assert "_(shown above)_" in text  # the cycle is cut, not looped

    # diamond: two parents share one *non-leaf* node (`mid`, which has its own
    # parent), so the shared subtree is expanded once and noted on the second path
    g2 = LineageGraph()
    base = g2.add_node(make_node(NodeType.SILVER_TABLE, "s", "base"))
    mid = g2.add_node(make_node(NodeType.GOLD_TABLE, "s", "mid"))
    focus = g2.add_node(make_node(NodeType.VIEW, "s", "focus"))
    g2.add_edge(Edge(source_id=mid.id, target_id=base.id, edge_type=EdgeType.READS))
    for p in ("p1", "p2"):
        pnode = g2.add_node(make_node(NodeType.VIEW, "s", p))
        g2.add_edge(Edge(source_id=focus.id, target_id=pnode.id, edge_type=EdgeType.READS))
        g2.add_edge(Edge(source_id=pnode.id, target_id=mid.id, edge_type=EdgeType.READS))
    text2 = "\n".join(_upstream_tree_text(g2, focus, _direct_upstream(g2)))
    assert text2.count("s.mid") == 2  # appears under both p1 and p2
    assert text2.count("_(shown above)_") == 1  # but its subtree is expanded only once
    assert text2.count("s.base") == 1  # so base (under mid) is listed exactly once


def test_site_nav_report_under_multiple_models():
    # a report drawing from two models nests under BOTH, and is not also
    # emitted as a top-level orphan
    from coop_data_doc.render.site import _nav_section

    g = LineageGraph()
    g.add_node(make_node(NodeType.SEMANTIC_MODEL, "", "sales", display_name="Sales"))
    g.add_node(make_node(NodeType.SEMANTIC_MODEL, "", "finance", display_name="Finance"))
    g.add_node(make_node(NodeType.REPORT, "", "exec", display_name="Exec Summary"))
    g.add_edge(Edge(source_id="semantic_model:sales", target_id="report:exec", edge_type=EdgeType.FEEDS))
    g.add_edge(Edge(source_id="semantic_model:finance", target_id="report:exec", edge_type=EdgeType.FEEDS))
    nav = _nav_section(g)
    assert nav.count(slug("report:exec")) == 2  # nested under both models
    assert "  - Reports:" not in nav.splitlines()  # no top-level orphan Reports header


def test_report_page_no_measures_fallback(tmp_path: Path):
    # a bare report with no edges renders the empty-report placeholder, no model
    # sections, and must not crash
    g = LineageGraph()
    g.add_node(make_node(NodeType.REPORT, "", "bare", display_name="Bare Report"))
    render_markdown(g, tmp_path, "Test")
    page = page_path(tmp_path, "report:bare").read_text(encoding="utf-8")
    assert "# Bare Report" in page
    assert "_No model, tables, or measures statically resolvable from this report._" in page
    assert "**Tables used**" not in page and "**Measures used**" not in page


def test_no_upstream_tree_when_gold_not_feeding_a_model(tmp_path: Path):
    # the trace-to-source tree is only for objects connected to a model; a gold
    # table with no PBI consumer (pure SQL sink) must NOT get the section
    g = LineageGraph()
    proc = g.add_node(make_node(NodeType.STORED_PROC, "dbo", "usp", source_file="p.sql"))
    gold = g.add_node(make_node(NodeType.GOLD_TABLE, "dbo", "audit", source_file="a.sql"))
    silver = g.add_node(make_node(NodeType.SILVER_TABLE, "silver", "src"))
    g.add_edge(Edge(source_id=proc.id, target_id=silver.id, edge_type=EdgeType.READS))
    g.add_edge(Edge(source_id=proc.id, target_id=gold.id, edge_type=EdgeType.WRITES))
    render_markdown(g, tmp_path, "Test")
    gold_page = page_path(tmp_path, "gold_table:dbo.audit").read_text(encoding="utf-8")
    assert "## Upstream lineage" not in gold_page


def test_mkdocs_config_and_assets_deterministic(tmp_path: Path):
    # the HTML-config path (skipped by the markdown determinism test) must also
    # be idempotent: same inputs -> byte-identical config + copied assets
    from coop_data_doc.render.site import write_mkdocs_config

    g = build_graph()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "index.md").write_text("# x", encoding="utf-8")
    site = tmp_path / "site"
    cfg = write_mkdocs_config(docs, site, "Test", g)
    first_cfg = cfg.read_bytes()
    tree = docs / "assets" / "javascripts" / "vendor" / "doc-tree.js"
    first_tree = tree.read_bytes()
    write_mkdocs_config(docs, site, "Test", g)  # rewrite same paths
    assert cfg.read_bytes() == first_cfg
    assert tree.read_bytes() == first_tree


def test_measure_dependency_tree(tmp_path: Path):
    # a measure's DAX dependency chain (MeasureLens-style) renders as the same
    # upstream tree: Sales per Customer -> Total Sales -> fact_sales
    g = LineageGraph()
    g.add_node(make_node(NodeType.SEMANTIC_MODEL, "", "sales"))
    g.add_node(make_node(NodeType.PBI_TABLE, "sales", "fact_sales", display_name="fact_sales"))
    g.add_node(make_node(NodeType.MEASURE, "sales", "total sales", display_name="Total Sales"))
    g.add_node(make_node(NodeType.MEASURE, "sales", "sales per customer", display_name="Sales per Customer"))
    # references: per-customer -> total sales -> fact_sales (REFERENCES reverses to data-flow)
    g.add_edge(
        Edge(
            source_id="measure:sales.sales per customer",
            target_id="measure:sales.total sales",
            edge_type=EdgeType.REFERENCES,
        )
    )
    g.add_edge(
        Edge(
            source_id="measure:sales.total sales",
            target_id="pbi_table:sales.fact_sales",
            edge_type=EdgeType.REFERENCES,
        )
    )
    render_markdown(g, tmp_path, "Test")
    page = page_path(tmp_path, "measure:sales.sales per customer").read_text(encoding="utf-8")
    assert "## Upstream lineage" in page
    assert "[sales.Total Sales](../measure/" in page
    assert "[sales.fact_sales](../pbi_table/" in page


def test_report_page_groups_by_model(tmp_path: Path):
    # a report page groups its referenced tables/measures under one heading per
    # source model, with UNPREFIXED child names (issue #21)
    g = LineageGraph()
    g.add_node(make_node(NodeType.SEMANTIC_MODEL, "", "sales", display_name="Sales"))
    g.add_node(make_node(NodeType.MEASURE, "sales", "total sales", display_name="Total Sales"))
    g.add_node(make_node(NodeType.PBI_TABLE, "sales", "dim_customer", display_name="Dim Customer"))
    g.add_node(make_node(NodeType.REPORT, "", "dash", display_name="Sales Dashboard"))
    # post-collapse shape: model feeds report; report visualizes a measure + table
    g.add_edge(Edge(source_id="semantic_model:sales", target_id="report:dash", edge_type=EdgeType.FEEDS))
    g.add_edge(
        Edge(source_id="report:dash", target_id="measure:sales.total sales", edge_type=EdgeType.VISUALIZES)
    )
    g.add_edge(
        Edge(source_id="report:dash", target_id="pbi_table:sales.dim_customer", edge_type=EdgeType.VISUALIZES)
    )
    render_markdown(g, tmp_path, "Test")
    page = page_path(tmp_path, "report:dash").read_text(encoding="utf-8")
    assert "# Sales Dashboard" in page
    # one model heading, linked; the tables/measures nest under it, unprefixed
    assert "## [Sales](../semantic_model/" in page
    assert "**Tables used**" in page and "**Measures used**" in page
    assert "[Total Sales](" in page and "[sales.Total Sales](" not in page  # no lowercase prefix
    assert "[Dim Customer](" in page and "[sales.Dim Customer](" not in page
    # the messy report internals are gone: no generic lineage tables
    assert "## Lineage" not in page


def test_report_page_multi_model_two_sections(tmp_path: Path):
    # a report drawing from two models gets a section per model, each with its
    # own tables/measures — no interleaved, prefix-laden single list (issue #21)
    g = LineageGraph()
    g.add_node(make_node(NodeType.SEMANTIC_MODEL, "", "sales", display_name="Sales"))
    g.add_node(make_node(NodeType.SEMANTIC_MODEL, "", "finance", display_name="Finance"))
    g.add_node(make_node(NodeType.PBI_TABLE, "sales", "orders", display_name="Orders"))
    g.add_node(make_node(NodeType.MEASURE, "finance", "margin", display_name="Margin"))
    g.add_node(make_node(NodeType.REPORT, "", "exec", display_name="Exec"))
    for mid in ("semantic_model:sales", "semantic_model:finance"):
        g.add_edge(Edge(source_id=mid, target_id="report:exec", edge_type=EdgeType.FEEDS))
    g.add_edge(
        Edge(source_id="report:exec", target_id="pbi_table:sales.orders", edge_type=EdgeType.VISUALIZES)
    )
    g.add_edge(
        Edge(source_id="report:exec", target_id="measure:finance.margin", edge_type=EdgeType.VISUALIZES)
    )
    render_markdown(g, tmp_path, "Test")
    page = page_path(tmp_path, "report:exec").read_text(encoding="utf-8")
    fin = page.index("## [Finance]")
    sal = page.index("## [Sales]")
    # Orders sits under Sales, Margin under Finance (each in the right block)
    assert page.index("[Orders]") > sal
    assert page.index("[Margin]") > fin
    assert "[sales.Orders]" not in page and "[finance.Margin]" not in page  # unprefixed


def test_measure_home_table_badge(tmp_path: Path):
    # a table flagged measure_table carries a badge on its own page (issue #27)
    g = LineageGraph()
    g.add_node(make_node(NodeType.SEMANTIC_MODEL, "", "sales", display_name="Sales"))
    g.add_node(
        make_node(
            NodeType.PBI_TABLE,
            "sales",
            "ad hoc measures",
            display_name="Ad Hoc Measures",
            metadata={"measure_table": True},
        )
    )
    g.add_node(make_node(NodeType.PBI_TABLE, "sales", "dim_customer", display_name="Dim Customer"))
    render_markdown(g, tmp_path, "Test")
    home = page_path(tmp_path, "pbi_table:sales.ad hoc measures").read_text(encoding="utf-8")
    data = page_path(tmp_path, "pbi_table:sales.dim_customer").read_text(encoding="utf-8")
    assert "**Measure home table**" in home
    assert "Measure home table" not in data  # a normal data table is not badged


def test_report_page_splits_data_and_measure_tables(tmp_path: Path):
    # under a model heading, tables the report reached only via a measure binding
    # (or structurally measure-only) list under "Measure home tables"; tables
    # reached via a column (incl. those reached by BOTH) list under "Tables used".
    g = LineageGraph()
    g.add_node(make_node(NodeType.SEMANTIC_MODEL, "", "sales", display_name="Sales"))
    g.add_node(make_node(NodeType.PBI_TABLE, "sales", "dim_customer", display_name="Dim Customer"))
    g.add_node(make_node(NodeType.PBI_TABLE, "sales", "ad hoc measures", display_name="Ad Hoc Measures"))
    g.add_node(make_node(NodeType.PBI_TABLE, "sales", "fact_sales", display_name="Fact Sales"))
    g.add_node(
        make_node(
            NodeType.REPORT,
            "",
            "dash",
            display_name="Dash",
            metadata={
                "field_refs": [
                    # data table: reached via a column
                    _ref("pbi_table:sales.dim_customer", "customer_name", "column"),
                    # measure home: reached ONLY via a measure binding
                    _ref("pbi_table:sales.ad hoc measures", "KPI Total", "measure"),
                    # reached by BOTH column and measure -> counts as a data table
                    _ref("pbi_table:sales.fact_sales", "order_total", "column"),
                    _ref("pbi_table:sales.fact_sales", "Total Sales", "measure"),
                ]
            },
        )
    )
    g.add_edge(Edge(source_id="semantic_model:sales", target_id="report:dash", edge_type=EdgeType.FEEDS))
    for tid in ("dim_customer", "ad hoc measures", "fact_sales"):
        g.add_edge(
            Edge(source_id="report:dash", target_id=f"pbi_table:sales.{tid}", edge_type=EdgeType.VISUALIZES)
        )
    render_markdown(g, tmp_path, "Test")
    page = page_path(tmp_path, "report:dash").read_text(encoding="utf-8")
    data_block = page.split("**Tables used**", 1)[1].split("**Measure home tables**", 1)[0]
    home_block = page.split("**Measure home tables**", 1)[1]
    assert "[Dim Customer]" in data_block  # column-reached -> data
    assert "[Fact Sales]" in data_block  # both column and measure -> data
    assert "[Ad Hoc Measures]" in home_block  # measure-only -> measure home
    assert "[Ad Hoc Measures]" not in data_block


def test_report_page_filters_section(tmp_path: Path):
    # a table reached ONLY through a filter lands in a ## Filters section (with
    # its field + scope), NOT the shown "Tables used" list; a shown table stays
    # under its model heading (issue #26)
    g = LineageGraph()
    g.add_node(make_node(NodeType.SEMANTIC_MODEL, "", "sales", display_name="Sales"))
    g.add_node(make_node(NodeType.PBI_TABLE, "sales", "dim_customer", display_name="Dim Customer"))
    g.add_node(make_node(NodeType.PBI_TABLE, "sales", "fact_sales", display_name="Fact Sales"))
    g.add_node(
        make_node(
            NodeType.REPORT,
            "",
            "dash",
            display_name="Dash",
            metadata={
                "field_refs": [
                    {
                        "target": "pbi_table:sales.dim_customer",
                        "entity": "dim_customer",
                        "property": "name",
                        "kind": "column",
                        "role": "shown",
                        "scope": "visual",
                    },
                    {
                        "target": "pbi_table:sales.fact_sales",
                        "entity": "fact_sales",
                        "property": "order_date",
                        "kind": "column",
                        "role": "filter",
                        "scope": "page",
                    },
                ]
            },
        )
    )
    g.add_edge(Edge(source_id="semantic_model:sales", target_id="report:dash", edge_type=EdgeType.FEEDS))
    g.add_edge(
        Edge(source_id="report:dash", target_id="pbi_table:sales.dim_customer", edge_type=EdgeType.VISUALIZES)
    )
    g.add_edge(
        Edge(source_id="report:dash", target_id="pbi_table:sales.fact_sales", edge_type=EdgeType.VISUALIZES)
    )
    render_markdown(g, tmp_path, "Test")
    page = page_path(tmp_path, "report:dash").read_text(encoding="utf-8")
    assert "## Filters" in page
    shown_block, filters_block = page.split("## Filters", 1)
    # shown table sits above Filters under its model heading; the filter-only
    # table is in the Filters section with its field + scope — and NOT shown
    assert "[Dim Customer](" in shown_block
    assert "[Fact Sales]" not in shown_block  # a filter is not a displayed table
    assert "[Fact Sales](" in filters_block
    assert "`order_date`" in filters_block and "_page filter_" in filters_block


def test_report_page_filter_backtick_in_property_does_not_break_span(tmp_path: Path):
    # a backtick in a filtered field name must not close the inline code span
    # early — the fence widens to survive it (issue #26 hardening).
    g = LineageGraph()
    g.add_node(make_node(NodeType.SEMANTIC_MODEL, "", "sales", display_name="Sales"))
    g.add_node(make_node(NodeType.PBI_TABLE, "sales", "fact", display_name="Fact"))
    g.add_node(
        make_node(
            NodeType.REPORT,
            "",
            "dash",
            display_name="Dash",
            metadata={
                "field_refs": [_ref("pbi_table:sales.fact", "od`er", "column", role="filter", scope="page")]
            },
        )
    )
    g.add_edge(Edge(source_id="semantic_model:sales", target_id="report:dash", edge_type=EdgeType.FEEDS))
    g.add_edge(Edge(source_id="report:dash", target_id="pbi_table:sales.fact", edge_type=EdgeType.VISUALIZES))
    render_markdown(g, tmp_path, "Test")
    page = page_path(tmp_path, "report:dash").read_text(encoding="utf-8")
    assert "``od`er``" in page  # 2-backtick fence wraps the 1-backtick name intact


def test_index_reports_overview_multi_report_sorted(tmp_path: Path):
    # the reports overview lists reports deterministically by display name (#21)
    g = LineageGraph()
    g.add_node(make_node(NodeType.SEMANTIC_MODEL, "", "sales", display_name="Sales"))
    for name, disp in [("charlie", "Charlie"), ("alpha", "Alpha"), ("bravo", "Bravo")]:
        g.add_node(make_node(NodeType.REPORT, "", name, display_name=disp))
        g.add_edge(
            Edge(source_id="semantic_model:sales", target_id=f"report:{name}", edge_type=EdgeType.FEEDS)
        )
    render_markdown(g, tmp_path, "Test")
    index = (tmp_path / "index.md").read_text(encoding="utf-8")
    rows = [ln for ln in index.splitlines() if ln.startswith("| [")]
    order = [r.split("]")[0].lstrip("| [") for r in rows]
    assert order == ["Alpha", "Bravo", "Charlie"]  # sorted, not insertion order


def test_index_reports_overview(tmp_path: Path):
    # index.md carries a reports->models overview table with counts (issue #21)
    g = LineageGraph()
    g.add_node(make_node(NodeType.SEMANTIC_MODEL, "", "sales", display_name="Sales"))
    g.add_node(make_node(NodeType.MEASURE, "sales", "total sales", display_name="Total Sales"))
    g.add_node(make_node(NodeType.PBI_TABLE, "sales", "dim_customer", display_name="Dim Customer"))
    g.add_node(make_node(NodeType.REPORT, "", "dash", display_name="Sales Dashboard"))
    g.add_edge(Edge(source_id="semantic_model:sales", target_id="report:dash", edge_type=EdgeType.FEEDS))
    g.add_edge(
        Edge(source_id="report:dash", target_id="measure:sales.total sales", edge_type=EdgeType.VISUALIZES)
    )
    g.add_edge(
        Edge(source_id="report:dash", target_id="pbi_table:sales.dim_customer", edge_type=EdgeType.VISUALIZES)
    )
    render_markdown(g, tmp_path, "Test")
    index = (tmp_path / "index.md").read_text(encoding="utf-8")
    assert "## Reports overview" in index
    assert "| Report | Draws from | Tables | Measures |" in index
    # one row: Sales Dashboard drawing from Sales, 1 table, 1 measure (root-relative links)
    row = next(ln for ln in index.splitlines() if "Sales Dashboard" in ln)
    assert "(report/" in row and "(semantic_model/" in row
    assert row.rstrip().endswith("| 1 | 1 |")


def test_site_nav_has_no_separate_reports_section():
    # there's no top-level Reports section: a report linked to no model simply
    # doesn't appear in the nav (it still has a page, reachable via search)
    from coop_data_doc.render.site import _nav_section

    g = LineageGraph()
    g.add_node(make_node(NodeType.REPORT, "", "ghost", display_name="Ghost"))
    nav = _nav_section(g)
    assert "- Reports:" not in nav  # no top-level (or any) Reports section for an orphan
    assert "ghost" not in nav


def test_source_section_embeds_sql(tmp_path: Path):
    g = LineageGraph()
    code = "CREATE VIEW sales.dim_customer AS\nSELECT 1 AS x;"
    g.add_node(
        make_node(
            NodeType.VIEW,
            "sales",
            "dim_customer",
            display_name="dim_customer",
            source_file="views/sales/dim_customer.sql",
            source_code=code,
            metadata={"layer": "gold"},
        )
    )
    render_markdown(g, tmp_path, "Test")
    page = page_path(tmp_path, "view:sales.dim_customer").read_text(encoding="utf-8")
    assert "## Source" in page
    assert "```sql" in page  # fenced -> syntax highlight + Material copy button
    assert code in page
    assert "views/sales/dim_customer.sql" in page


def test_no_source_section_when_no_code(tmp_path: Path):
    g = LineageGraph()
    g.add_node(make_node(NodeType.MEASURE, "sales", "total", metadata={"dax": "SUM(x)"}))
    render_markdown(g, tmp_path, "Test")
    page = page_path(tmp_path, "measure:sales.total").read_text(encoding="utf-8")
    assert "## Source" not in page  # no source_code -> no Source section
    assert "## DAX" in page  # measures still show their DAX


def test_dax_includes_measure_name(tmp_path: Path):
    # the DAX block shows the full `Measure Name = <expr>`, not just the expression
    g = LineageGraph()
    g.add_node(
        make_node(
            NodeType.MEASURE,
            "sales",
            "actual sales",
            display_name="Actual Sales",
            metadata={"dax": "SUM(fact_sales[amount])"},
        )
    )
    render_markdown(g, tmp_path, "Test")
    page = page_path(tmp_path, "measure:sales.actual sales").read_text(encoding="utf-8")
    assert "Actual Sales = SUM(fact_sales[amount])" in page


def test_build_site_on_page_ticks_per_page(tmp_path: Path, monkeypatch):
    # build_site streams mkdocs -v output and ticks once per "Building page" line.
    from coop_data_doc.render import site as site_module

    class _FakeProc:
        def __init__(self):
            self.stdout = iter(
                [
                    "INFO    -  Building documentation\n",
                    "DEBUG   -  Building page index.md\n",
                    "DEBUG   -  Building page bronze_table/x.md\n",
                    "DEBUG   -  Running `page_context` event\n",
                    "DEBUG   -  Building page diagnostics.md\n",
                ]
            )

        def wait(self):
            return 0

    monkeypatch.setattr(site_module.subprocess, "Popen", lambda *a, **k: _FakeProc())
    ticks: list[int] = []
    # site_dir has no shim file, so localize_shim is a quick no-op
    site_module.build_site(tmp_path / "cfg.yml", tmp_path / "site", on_page=lambda *a: ticks.append(1))
    assert len(ticks) == 3  # exactly one per page, theme/event lines ignored


def test_dax_fence_survives_backticks_in_expression(tmp_path: Path):
    # defense-in-depth: even if a measure's DAX contains a ``` run, the rendered
    # fence must be longer so the block isn't split into two empty code boxes.
    g = LineageGraph()
    g.add_node(make_node(NodeType.MEASURE, "sales", "total", metadata={"dax": "// ```\nSUM(x)"}))
    render_markdown(g, tmp_path, "Test")
    page = page_path(tmp_path, "measure:sales.total").read_text(encoding="utf-8")
    assert "````dax" in page  # 4-backtick fence opens the block
    assert "// ```\nSUM(x)" in page  # the stray ``` stays inside the box


def test_contract_section_only_for_column_bearing_types(tmp_path: Path):
    g = LineageGraph()
    g.add_node(
        make_node(
            NodeType.GOLD_TABLE,
            "dbo",
            "fact",
            columns=[Column(name="id", data_type="INT", nullable=False)],
        )
    )
    g.add_node(make_node(NodeType.MEASURE, "sales", "total", metadata={"dax": "SUM(x)"}))
    g.add_node(make_node(NodeType.STORED_PROC, "dbo", "usp_x", source_file="p.sql", source_code="SELECT 1;"))
    render_markdown(g, tmp_path, "Test")
    table_page = page_path(tmp_path, "gold_table:dbo.fact").read_text(encoding="utf-8")
    measure_page = page_path(tmp_path, "measure:sales.total").read_text(encoding="utf-8")
    proc_page = page_path(tmp_path, "stored_proc:dbo.usp_x").read_text(encoding="utf-8")
    assert "## Structural Contract" in table_page  # tables keep their column contract
    assert "## Structural Contract" not in measure_page  # measures show DAX instead
    assert "## Structural Contract" not in proc_page  # procs show Source instead


def test_pbi_table_page_shows_storage_mode(tmp_path: Path):
    g = LineageGraph()
    g.add_node(
        make_node(
            NodeType.PBI_TABLE,
            "sales",
            "orders",
            display_name="orders",
            metadata={"storage_mode": "directquery"},
        )
    )
    render_markdown(g, tmp_path, "X")
    page = page_path(tmp_path, "pbi_table:sales.orders").read_text(encoding="utf-8")
    assert "**Storage mode:** DirectQuery" in page


def test_reports_downstream_of_models_and_visuals_collapsed():
    from coop_data_doc.parsers.pbir import collapse_visuals, link_reports_to_models

    g = LineageGraph()
    model = g.add_node(make_node(NodeType.SEMANTIC_MODEL, "", "sales", display_name="Sales"))
    pbit = g.add_node(make_node(NodeType.PBI_TABLE, "sales", "orders", display_name="orders"))
    g.add_edge(Edge(source_id=pbit.id, target_id=model.id, edge_type=EdgeType.FEEDS))
    rpt = g.add_node(make_node(NodeType.REPORT, "", "dash", display_name="Dash"))
    pg = g.add_node(make_node(NodeType.REPORT_PAGE, "dash", "p1", display_name="P1"))
    vis = g.add_node(make_node(NodeType.VISUAL, "dash", "v1", display_name="v1"))
    g.add_edge(Edge(source_id=pg.id, target_id=rpt.id, edge_type=EdgeType.FEEDS))
    g.add_edge(Edge(source_id=vis.id, target_id=pg.id, edge_type=EdgeType.FEEDS))
    g.add_edge(Edge(source_id=vis.id, target_id=pbit.id, edge_type=EdgeType.VISUALIZES))

    link_reports_to_models(g)
    collapse_visuals(g)
    keys = {(e.source_id, e.target_id, e.edge_type.value) for e in g.edges}
    assert ("semantic_model:sales", "report:dash", "feeds") in keys  # report downstream of model
    assert "visual:dash.v1" not in g.nodes  # visual folded away
    assert "report_page:dash.p1" not in g.nodes  # page folded into the report too
    # the visualizes edge is rewired all the way up onto the report (one node per report)
    assert ("report:dash", "pbi_table:sales.orders", "visualizes") in keys


def test_semantic_model_page_drops_child_prefix(tmp_path: Path):
    g = LineageGraph()
    model = g.add_node(make_node(NodeType.SEMANTIC_MODEL, "", "Finance", display_name="Finance"))
    measure = g.add_node(
        make_node(
            NodeType.MEASURE, "finance", "Total Sales", display_name="Total Sales", metadata={"dax": "1"}
        )
    )
    g.add_edge(Edge(source_id=measure.id, target_id=model.id, edge_type=EdgeType.FEEDS))
    render_markdown(g, tmp_path, "Test")
    model_page = page_path(tmp_path, "semantic_model:finance").read_text(encoding="utf-8")
    measure_page = page_path(tmp_path, "measure:finance.total sales").read_text(encoding="utf-8")
    # on the model's own page, the measure is listed bare (no "finance." prefix)
    assert "[Total Sales]" in model_page
    assert "[finance.Total Sales]" not in model_page
    # the measure's own page title still carries the qualifier
    assert "# finance.Total Sales" in measure_page


def _parse_grid(page_text: str):
    """Parse the relationship grid markdown table into (fact_columns, rows),
    where rows maps a dimension's first-cell text to {fact_column: cell}."""
    grid_lines = [ln for ln in page_text.splitlines() if ln.startswith("|")]
    sep_idx = next(
        i for i, ln in enumerate(grid_lines) if set(ln.replace("|", "").replace(" ", "")) <= set(":-")
    )
    header = [c.strip() for c in grid_lines[sep_idx - 1].strip("|").split("|")]
    facts = header[1:]
    rows: dict[str, dict[str, str]] = {}
    for ln in grid_lines[sep_idx + 1 :]:
        cells = [c.strip() for c in ln.strip("|").split("|")]
        rows[cells[0]] = dict(zip(facts, cells[1:]))
    return facts, rows


def test_semantic_model_relationship_grid(tmp_path: Path):
    g = LineageGraph()
    g.add_node(make_node(NodeType.SEMANTIC_MODEL, "", "Sales", display_name="Sales"))
    for name, disp in [
        ("fact_sales", "Fact Sales"),
        ("fact_returns", "Fact Returns"),
        ("dim_customer", "Dim Customer"),
        ("dim_date", "Dim Date"),
    ]:
        g.add_node(make_node(NodeType.PBI_TABLE, "sales", name, display_name=disp))
    g.nodes["semantic_model:sales"].metadata["relationships"] = [
        {"from": "fact_sales.customer_id", "to": "dim_customer.customer_id"},
        {"from": "fact_sales.date_id", "to": "dim_date.date_id"},
        {"from": "fact_returns.date_id", "to": "dim_date.date_id"},
    ]
    render_markdown(g, tmp_path, "Test")
    page = page_path(tmp_path, "semantic_model:sales").read_text(encoding="utf-8")
    assert "## Relationship Grid" in page
    assert "Joel" not in page  # client-neutral heading (issue #24)
    # count header + legend
    assert "2 fact(s) × 2 dimension(s), 3 relationship(s) (3 active)" in page
    assert "🟢 active" in page and "⚪ inactive" in page and "⇅ bidirectional" in page
    facts, rows = _parse_grid(page)
    # facts are columns (link text), dims are rows; both sorted by table name
    assert [f.split("]")[0] + "]" for f in facts] == ["[Fact Returns]", "[Fact Sales]"]
    assert [k.split("]")[0] + "]" for k in rows] == ["[Dim Customer]", "[Dim Date]"]
    cust = next(v for k, v in rows.items() if "Dim Customer" in k)
    date = next(v for k, v in rows.items() if "Dim Date" in k)
    fact_sales_col = next(c for c in facts if "Fact Sales" in c)
    fact_returns_col = next(c for c in facts if "Fact Returns" in c)
    # dim_customer relates only to fact_sales; dim_date relates to both
    assert "🟢" in cust[fact_sales_col] and cust[fact_returns_col] == ""
    assert "🟢" in date[fact_sales_col] and "🟢" in date[fact_returns_col]
    # each marker carries a tooltip with the joined columns
    assert 'title="fact_sales.customer_id → dim_customer.customer_id"' in page


def test_relationship_grid_marks_inactive_and_bidirectional(tmp_path: Path):
    g = LineageGraph()
    g.add_node(make_node(NodeType.SEMANTIC_MODEL, "", "Sales", display_name="Sales"))
    for name, disp in [("fact_sales", "Fact Sales"), ("dim_date", "Dim Date"), ("dim_geo", "Dim Geo")]:
        g.add_node(make_node(NodeType.PBI_TABLE, "sales", name, display_name=disp))
    g.nodes["semantic_model:sales"].metadata["relationships"] = [
        # role-playing date dimension: one active (order_date) + one inactive (ship_date)
        {"from": "fact_sales.order_date", "to": "dim_date.date_id", "active": True, "bidirectional": False},
        {"from": "fact_sales.ship_date", "to": "dim_date.date_id", "active": False, "bidirectional": False},
        # bidirectional cross-filter to geo
        {"from": "fact_sales.geo_id", "to": "dim_geo.geo_id", "active": True, "bidirectional": True},
    ]
    render_markdown(g, tmp_path, "Test")
    page = page_path(tmp_path, "semantic_model:sales").read_text(encoding="utf-8")
    facts, rows = _parse_grid(page)
    fs = next(c for c in facts if "Fact Sales" in c)
    date = next(v for k, v in rows.items() if "Dim Date" in k)
    geo = next(v for k, v in rows.items() if "Dim Geo" in k)
    # the date cell shows both an active and an inactive marker (role-playing)
    assert "🟢" in date[fs] and "⚪" in date[fs]
    # the geo cell shows an active, bidirectional marker
    assert "🟢" in geo[fs] and "⇅" in geo[fs]
    # tooltips spell out the join columns and the inactive flag
    assert 'title="fact_sales.ship_date → dim_date.date_id (inactive)"' in page
    assert "(bidirectional)" in page
    # 3 relationships, only 2 active
    assert "3 relationship(s) (2 active)" in page


def test_relationship_grid_escapes_brackets_in_table_name(tmp_path: Path):
    # a ']' in a table display name must be escaped so the [text](url) link
    # isn't closed early and the raw URL leaked as visible text
    g = LineageGraph()
    g.add_node(make_node(NodeType.SEMANTIC_MODEL, "", "Sales", display_name="Sales"))
    g.add_node(make_node(NodeType.PBI_TABLE, "sales", "fact_odd", display_name="Fact [Odd]"))
    g.add_node(make_node(NodeType.PBI_TABLE, "sales", "dim_x", display_name="Dim X"))
    g.nodes["semantic_model:sales"].metadata["relationships"] = [
        {"from": "fact_odd.k", "to": "dim_x.k"},
    ]
    render_markdown(g, tmp_path, "Test")
    page = page_path(tmp_path, "semantic_model:sales").read_text(encoding="utf-8")
    assert "[Fact \\[Odd\\]](../pbi_table/" in page  # brackets escaped, link intact
    assert "Fact [Odd]](" not in page  # the unescaped, link-breaking form is gone


def test_relationship_grid_placeholder_when_empty(tmp_path: Path):
    g = LineageGraph()
    g.add_node(make_node(NodeType.SEMANTIC_MODEL, "", "Empty", display_name="Empty"))
    render_markdown(g, tmp_path, "Test")
    page = page_path(tmp_path, "semantic_model:empty").read_text(encoding="utf-8")
    assert "## Relationship Grid" in page  # section always present on a model
    assert "_No relationships defined in this semantic model._" in page
    assert "🟢" not in page


def test_relationship_grid_only_on_semantic_models(tmp_path: Path):
    # build_graph() has a pbi_table and a (relationship-less) semantic model
    render_markdown(build_graph(), tmp_path, "Test")
    pbit = page_path(tmp_path, "pbi_table:sales.dim_customer").read_text(encoding="utf-8")
    model = page_path(tmp_path, "semantic_model:sales").read_text(encoding="utf-8")
    assert "Relationship Grid" not in pbit  # not on table pages
    assert "## Relationship Grid" in model  # present on the model page
    # issue #24: the internal first-name nickname never reaches a rendered page
    assert "Joel" not in pbit and "Joel" not in model


def test_source_fence_survives_backticks_in_code(tmp_path: Path):
    g = LineageGraph()
    code = "-- ``` not a fence\nSELECT 1;"
    g.add_node(
        make_node(
            NodeType.STORED_PROC,
            "dbo",
            "usp_x",
            source_file="p.sql",
            source_code=code,
            metadata={"layer": "gold"},
        )
    )
    render_markdown(g, tmp_path, "Test")
    page = page_path(tmp_path, "stored_proc:dbo.usp_x").read_text(encoding="utf-8")
    assert "````sql" in page  # 4-backtick fence because the code contains ```
    assert code in page


def test_default_branding_is_cooptimize_theme(tmp_path: Path):
    from coop_data_doc.config import DEFAULT_ACCENT_COLOR, DEFAULT_PRIMARY_COLOR, Branding
    from coop_data_doc.render.site import write_mkdocs_config

    assert Branding().primary_color == DEFAULT_PRIMARY_COLOR
    assert Branding().accent_color == DEFAULT_ACCENT_COLOR
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "index.md").write_text("# x", encoding="utf-8")
    write_mkdocs_config(docs, tmp_path / "site", "Test", build_graph(), branding=Branding())
    brand_css = (docs / "assets" / "stylesheets" / "brand.css").read_text(encoding="utf-8")
    assert f"--md-primary-fg-color: {DEFAULT_PRIMARY_COLOR};" in brand_css
    assert f"--md-accent-fg-color: {DEFAULT_ACCENT_COLOR};" in brand_css


def test_doc_tree_asset_copied_and_referenced(tmp_path: Path):
    from coop_data_doc.render.site import write_mkdocs_config

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "index.md").write_text("# x", encoding="utf-8")
    cfg = write_mkdocs_config(docs, tmp_path / "site", "Test", build_graph())
    vendor = docs / "assets" / "javascripts" / "vendor"
    assert (vendor / "doc-tree.js").is_file()
    text = cfg.read_text(encoding="utf-8")
    assert "assets/javascripts/vendor/doc-tree.js" in text


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


def test_default_theme_bundles_logo(tmp_path: Path):
    # a default config (Branding()) ships the bundled Cooptimize logo + favicon
    from coop_data_doc.config import Branding
    from coop_data_doc.render.site import write_mkdocs_config

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "index.md").write_text("# x", encoding="utf-8")
    cfg = write_mkdocs_config(docs, tmp_path / "site", "Test", build_graph(), branding=Branding())
    text = cfg.read_text(encoding="utf-8")
    assert "logo: assets/images/logo.png" in text
    assert "favicon: assets/images/favicon.png" in text
    assert (docs / "assets" / "images" / "logo.png").is_file()
    assert (docs / "assets" / "images" / "favicon.png").is_file()


def test_no_branding_is_clean(tmp_path: Path):
    from coop_data_doc.render.site import write_mkdocs_config

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "index.md").write_text("# x", encoding="utf-8")
    cfg = write_mkdocs_config(docs, tmp_path / "site", "Test", build_graph())
    text = cfg.read_text(encoding="utf-8")
    assert "logo:" not in text  # no logo line when unbranded
    assert (docs / "assets" / "stylesheets" / "brand.css").is_file()  # empty brand.css still written


def test_mkdocs_config_quotes_weird_project_name(tmp_path: Path):
    import yaml as _yaml

    from coop_data_doc.render.site import write_mkdocs_config

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "index.md").write_text("# x", encoding="utf-8")
    cfg = write_mkdocs_config(docs, tmp_path / "site", "Sales: FY26 # report", build_graph())
    data = _yaml.safe_load(cfg.read_text(encoding="utf-8").replace("!!python/name:", "tag:"))
    assert data["site_name"] == "Sales: FY26 # report"  # colon/# survived as a quoted scalar


def test_model_description_script_tag_rendered_inert(tmp_path: Path):
    # descriptions come from the semi-trusted estate being documented; raw
    # HTML passes straight through python-markdown/mkdocs, so a <script> in a
    # TMDL/BIM description would execute in every colleague's browser. It must
    # be escaped to &lt;script&gt; at render time.
    payload = "legit blurb <script>alert(1)</script>"
    g = LineageGraph()
    g.add_node(
        make_node(
            NodeType.PBI_TABLE,
            "sales",
            "orders",
            display_name="orders",
            metadata={"description": payload},
        )
    )
    render_markdown(g, tmp_path, "Test")
    page = page_path(tmp_path, "pbi_table:sales.orders").read_text(encoding="utf-8")
    assert "<script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "legit blurb" in page  # plain text survives untouched


def test_column_description_html_rendered_inert(tmp_path: Path):
    # _cell keeps its pipe escaping AND neutralizes HTML in column descriptions
    g = LineageGraph()
    g.add_node(
        make_node(
            NodeType.GOLD_TABLE,
            "dbo",
            "fact",
            columns=[Column(name="c", data_type="int", description="<img src=x onerror=alert(1)> a | b")],
        )
    )
    render_markdown(g, tmp_path, "Test")
    page = page_path(tmp_path, "gold_table:dbo.fact").read_text(encoding="utf-8")
    assert "<img" not in page
    assert "&lt;img src=x onerror=alert(1)&gt; a \\| b" in page  # escaped, pipe still escaped


def test_h1_object_name_html_rendered_inert(tmp_path: Path):
    # object names are model-supplied too; an <i>…</i> (or worse) in a display
    # name must render literally in the H1, not as live markup
    g = LineageGraph()
    g.add_node(
        Node(
            id=Node.make_id(NodeType.PBI_TABLE, "m", "evil"),
            node_type=NodeType.PBI_TABLE,
            name="evil",
            schema_name="m",
            display_name="<img src=x onerror=alert(1)>",
        )
    )
    g.add_node(
        Node(
            id=Node.make_id(NodeType.REPORT, "", "rpt"),
            node_type=NodeType.REPORT,
            name="rpt",
            schema_name="",
            display_name="Dash <script>x</script>",
        )
    )
    render_markdown(g, tmp_path, "Test")
    table_page = page_path(tmp_path, "pbi_table:m.evil").read_text(encoding="utf-8")
    assert "# m.&lt;img src=x onerror=alert(1)&gt;" in table_page
    report_page = page_path(tmp_path, "report:rpt").read_text(encoding="utf-8")
    assert "# Dash &lt;script&gt;x&lt;/script&gt;" in report_page
    assert "<script>" not in report_page


def test_intent_block_saved_as_cp1252_does_not_crash_build(tmp_path: Path):
    # the Business Intent block is hand-edited; a Windows ANSI editor saving a
    # smart quote writes cp1252 bytes. The next build must carry the note
    # forward (decoded via the cp1252 fallback), never crash with
    # UnicodeDecodeError.
    graph = build_graph()
    render_markdown(graph, tmp_path, "Test Estate")
    page = page_path(tmp_path, "view:sales.dim_customer")
    text = page.read_text(encoding="utf-8")
    custom = "Bob’s finance view – don’t drop"
    text = text.replace(
        "_Add a short description of what this object is for and who relies on it._",
        custom,
    )
    page.write_bytes(text.encode("cp1252"))  # smart quote/en-dash as ANSI bytes

    render_markdown(graph, tmp_path, "Test Estate")  # must not raise
    regenerated = page.read_text(encoding="utf-8")
    assert custom in regenerated  # note preserved, correctly decoded


def test_intent_block_with_binary_garbage_does_not_crash_build(tmp_path: Path):
    # bytes invalid in BOTH utf-8 and cp1252 (0x81/0x90 are unmapped in
    # cp1252): the build must still regenerate the page best-effort
    graph = build_graph()
    render_markdown(graph, tmp_path, "Test Estate")
    page = page_path(tmp_path, "view:sales.dim_customer")
    text = page.read_text(encoding="utf-8")
    head, _, tail = text.partition("_Add a short description")
    garbage = head.encode("utf-8") + b"\x81\x90\xfe\xff" + ("_Add a short description" + tail).encode("utf-8")
    page.write_bytes(garbage)

    written = render_markdown(graph, tmp_path, "Test Estate")  # must not raise
    assert page in written
    regenerated = page.read_text(encoding="utf-8")
    assert INTENT_BEGIN in regenerated and INTENT_END in regenerated


def test_description_with_pipe_does_not_break_contract_table(tmp_path: Path):
    g = LineageGraph()
    g.add_node(
        Node(
            id=Node.make_id(NodeType.PBI_TABLE, "m", "t"),
            node_type=NodeType.PBI_TABLE,
            name="t",
            schema_name="m",
            columns=[Column(name="c", data_type="int", description="a | b\nsecond line")],
        )
    )
    render_markdown(g, tmp_path, "X")
    page = page_path(tmp_path, "pbi_table:m.t").read_text(encoding="utf-8")
    row = [ln for ln in page.splitlines() if ln.startswith("| c ")][0]
    assert "a \\| b second line" in row  # pipe escaped, newline collapsed to a space
    assert row.count(" | ") == 3  # exactly 4 cells (3 interior separators)
