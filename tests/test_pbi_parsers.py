import io
import json
import zipfile
from pathlib import Path

from coop_data_doc.config import Config, RepoConfig
from coop_data_doc.crawler import FileEntry, FileKind, crawl
from coop_data_doc.graph import LineageGraph, to_json_str
from coop_data_doc.parsers.dax import extract_refs
from coop_data_doc.parsers.mcode import extract_source
from coop_data_doc.parsers.pbir import link_visual_bindings, parse_legacy_reports, parse_pbir
from coop_data_doc.parsers.pbix import parse_pbix
from coop_data_doc.parsers.tmdl import parse_tmdl

FIXTURES = Path(__file__).parent / "fixtures"


def pbi_inventory():
    config = Config(
        repos={
            "powerbi": RepoConfig(
                path=str(FIXTURES / "repo_pbi"),
                include=[
                    "**/*.tmdl",
                    "**/*.bim",
                    "**/report.json",
                    "**/visual.json",
                    "**/page.json",
                    "**/*.pbix",
                ],
            )
        }
    )
    inventory, _ = crawl(config)
    return inventory


def parse_all() -> tuple[LineageGraph, list]:
    graph = LineageGraph()
    inventory = pbi_inventory()
    warnings = parse_tmdl(inventory.by_kind(FileKind.TMDL), graph)
    warnings += parse_pbir(
        inventory.by_kind(FileKind.PBIR_VISUAL),
        inventory.by_kind(FileKind.PBIR_PAGE),
        graph,
    )
    warnings += parse_legacy_reports(inventory.by_kind(FileKind.REPORT_JSON_LEGACY), graph)
    warnings += link_visual_bindings(graph)
    return graph, warnings


def edge_keys(graph: LineageGraph) -> set[tuple[str, str, str]]:
    return {edge.key() for edge in graph.edges}


# ---- mcode unit tests ------------------------------------------------------


def test_mcode_sql_database():
    ref, sqls = extract_source(
        'let Source = Sql.Database("srv", "gold"), '
        'd = Source{[Schema="sales",Item="dim_customer"]}[Data] in d'
    )
    assert sqls == []
    assert ref.schema_name == "sales"
    assert ref.object_name == "dim_customer"
    assert ref.raw_kind == "sql_database"


def test_mcode_native_query():
    ref, sqls = extract_source(
        'let Source = Sql.Database("srv", "gold"), '
        'q = Value.NativeQuery(Source, "SELECT a FROM sales.v_orders_star") in q'
    )
    assert ref.raw_kind == "native_query"
    assert sqls == ["SELECT a FROM sales.v_orders_star"]


def test_mcode_lakehouse():
    ref, _ = extract_source(
        "let Source = Lakehouse.Contents(), "
        'w = Source{[Name="gold_lakehouse"]}[Data], '
        't = w{[Name="fact_sales"]}[Data] in t'
    )
    assert ref.raw_kind == "lakehouse"
    assert ref.object_name == "fact_sales"
    assert ref.schema_name == "gold_lakehouse"


def test_mcode_unresolved():
    ref, sqls = extract_source('let Source = OData.Feed("https://x.example") in Source')
    assert ref is None and sqls == []


def test_mcode_let_variable_indirection():
    # the real PBIP template: schema/table bound to `let` variables
    ref, sqls = extract_source(
        "let\n"
        '    LocalTable = "Date",\n'
        '    LocalSchema = "common",\n'
        "    Source = Sql.Database(SQLServer, SQLDatabase),\n"
        "    Data = Source{[Schema=LocalSchema,Item=LocalTable]}[Data]\n"
        "in\n    Data"
    )
    assert sqls == []
    assert ref.raw_kind == "sql_database"
    assert ref.schema_name == "common"
    assert ref.object_name == "date"


def test_mcode_static_calculation_table():
    ref, _ = extract_source(
        "let Source = Table.FromRows(Json.Document(Binary.Decompress("
        'Binary.FromText("i45W", BinaryEncoding.Base64), Compression.Deflate))) in Source'
    )
    assert ref is not None and ref.raw_kind == "static"


# ---- dax unit tests --------------------------------------------------------


def test_dax_refs():
    measures, tables = extract_refs("DIVIDE([Total Sales], fact_sales[order_total])")
    assert measures == {"Total Sales"}
    assert tables == {"fact_sales"}


def test_dax_quoted_table_and_strings_ignored():
    measures, tables = extract_refs(
        "IF('My Table'[flag] = \"[Not A Ref]\", [Real Measure], 0) // [comment ref]"
    )
    assert measures == {"Real Measure"}
    assert tables == {"my table"}


def test_dax_comment_marker_inside_string_does_not_corrupt_refs():
    """'//' inside a string literal (URLs, doc links) must not be treated as a
    comment: stripping it unbalances the quotes, hides real measure refs, and
    leaks string contents as phantom table candidates."""
    measures, tables = extract_refs(
        'VAR url = "https://contoso.com/help"\n'
        'VAR note = "see [Internal Note] for details"\n'
        "RETURN [Total Sales] + 0"
    )
    assert measures == {"Total Sales"}
    assert "Internal Note" not in measures
    assert "see" not in tables and "return" not in tables


def test_dax_escaped_quotes_and_keyword_before_bracket():
    # a "" escape inside a string must not flip which text counts as code,
    # and a keyword like RETURN before [X] is a measure ref, not table RETURN
    measures, tables = extract_refs('VAR t = "say ""hi"" [Nope]" RETURN [Net Total]')
    assert measures == {"Net Total"}
    assert tables == set()


def test_dax_leading_measure_reference_detected():
    # A [Measure] at the START of the expression (nothing before '[') was dropped because
    # `"" in "_')]"` is True in Python, so a measure used only via `[Ref] * x` looked unused.
    measures, tables = extract_refs("[Total Sales] * 1.1")
    assert measures == {"Total Sales"}
    assert tables == set()
    # a leading measure and a following Table[Column] are still told apart
    measures2, tables2 = extract_refs("[Leading Measure] + fact_sales[order_total]")
    assert measures2 == {"Leading Measure"}
    assert tables2 == {"fact_sales"}


def test_measure_referenced_by_another_measure_is_not_flagged_unused():
    # End-to-end: a measure whose only consumer references it at the start of a DAX
    # expression must count as USED, not shown as a dead measure.
    from coop_data_doc.graph.model import Edge, EdgeType, Node, NodeType
    from coop_data_doc.parsers.dax import link_measures
    from coop_data_doc.render.markdown import _used_measure_ids

    g = LineageGraph()
    g.add_node(Node(id="semantic_model:x", node_type=NodeType.SEMANTIC_MODEL, name="x", schema_name="x"))
    g.add_node(
        Node(
            id="measure:x.base",
            node_type=NodeType.MEASURE,
            name="base",
            schema_name="x",
            metadata={"dax": "SUM(t[c])"},
        )
    )
    g.add_node(
        Node(
            id="measure:x.derived",
            node_type=NodeType.MEASURE,
            name="derived",
            schema_name="x",
            metadata={"dax": "[base] * 1.1"},
        )
    )
    g.add_node(
        Node(
            id="measure:x.shown",
            node_type=NodeType.MEASURE,
            name="shown",
            schema_name="x",
            metadata={"dax": ""},
        )
    )
    g.add_node(Node(id="report:r", node_type=NodeType.REPORT, name="r"))
    g.add_edge(Edge(source_id="report:r", target_id="measure:x.shown", edge_type=EdgeType.VISUALIZES))
    link_measures(g, "x")
    used = _used_measure_ids(g)
    assert ("measure:x.derived", "measure:x.base", "references") in edge_keys(g)
    assert "measure:x.base" in used  # referenced by `derived` -> USED (was wrongly unused)
    assert "measure:x.shown" in used  # shown in a report -> USED
    assert "measure:x.derived" not in used  # nothing references it -> correctly unused


# ---- TMDL model ------------------------------------------------------------


def test_tmdl_model_structure():
    graph, _ = parse_all()
    assert "semantic_model:sales" in graph.nodes
    for table in ("dim_customer", "fact_sales", "orders_native", "ext_unresolved"):
        node_id = f"pbi_table:sales.{table}"
        assert node_id in graph.nodes
        assert (node_id, "semantic_model:sales", "feeds") in edge_keys(graph)
    columns = {c.name: c.data_type for c in graph.nodes["pbi_table:sales.dim_customer"].columns}
    assert columns == {"customer_id": "int64", "customer_name": "string"}
    # relationships come from two files: the active one from model.tmdl
    # (older style) and the inactive one from a dedicated relationships.tmdl
    # (current Power BI default), merged and sorted by (from, to).
    relationships = graph.nodes["semantic_model:sales"].metadata["relationships"]
    assert relationships == [
        {
            "from": "fact_sales.customer_id",
            "to": "dim_customer.customer_id",
            "active": True,
            "bidirectional": False,
        },
        {
            "from": "fact_sales.order_id",
            "to": "orders_native.order_id",
            "active": False,
            "bidirectional": False,
        },
    ]


def test_tmdl_relationships_without_model_file(tmp_path):
    # an export with relationships.tmdl but NO model.tmdl: relationships are
    # still collected, and the model's source_file falls back to it
    from coop_data_doc.parsers.tmdl import parse_tmdl

    defn = tmp_path / "Solo.SemanticModel" / "definition"
    (defn / "tables").mkdir(parents=True)
    (defn / "relationships.tmdl").write_text(
        "relationship abc\n\tfromColumn: fact.k\n\ttoColumn: dim.k\n", encoding="utf-8"
    )
    (defn / "tables" / "fact.tmdl").write_text("table fact\n\tcolumn k\n", encoding="utf-8")
    config = Config(repos={"pbi": RepoConfig(path=str(tmp_path), include=["**/*.tmdl"])})
    inventory, _ = crawl(config)
    g = LineageGraph()
    parse_tmdl(inventory.by_kind(FileKind.TMDL), g)
    model = g.nodes["semantic_model:solo"]
    assert model.metadata["relationships"] == [
        {"from": "fact.k", "to": "dim.k", "active": True, "bidirectional": False}
    ]
    assert model.source_file.endswith("relationships.tmdl")


def test_tmdl_calculated_column_kept_and_does_not_corrupt_previous(tmp_path):
    # issue #8: `column Name = <DAX>` (a calculated column) must appear in the contract
    # with its OWN dataType, and its property lines must not bleed into the prior column.
    from coop_data_doc.parsers.tmdl import parse_tmdl

    defn = tmp_path / "Calc.SemanticModel" / "definition"
    (defn / "tables").mkdir(parents=True)
    (defn / "tables" / "Sales.tmdl").write_text(
        "table Sales\n"
        "\tcolumn Amount\n"
        "\t\tdataType: decimal\n"
        "\t\tsummarizeBy: sum\n"
        "\n"
        "\tcolumn Margin = [Amount] - [Cost]\n"
        "\t\tdataType: double\n"
        "\n"
        "\tcolumn MultiLine =\n"
        "\t\t\tIF(\n"
        "\t\t\t\t[Amount] > 0,\n"
        "\t\t\t\t[Amount],\n"
        "\t\t\t\t0\n"
        "\t\t\t)\n"
        "\t\tdataType: int64\n",
        encoding="utf-8",
    )
    config = Config(repos={"pbi": RepoConfig(path=str(tmp_path), include=["**/*.tmdl"])})
    inventory, _ = crawl(config)
    g = LineageGraph()
    parse_tmdl(inventory.by_kind(FileKind.TMDL), g)
    table = g.nodes["pbi_table:calc.sales"]
    cols = {c.name: c for c in table.columns}
    assert set(cols) == {"amount", "margin", "multiline"}  # all three present, none dropped
    assert cols["amount"].data_type == "decimal"  # NOT overwritten by Margin's dataType
    assert cols["margin"].data_type == "double"
    assert cols["multiline"].data_type == "int64"  # a multi-line expr didn't corrupt state
    assert "CALCULATED" in cols["margin"].constraints
    assert "CALCULATED" in cols["multiline"].constraints
    assert "CALCULATED" not in cols["amount"].constraints  # a plain column is not tagged


def test_tmdl_partition_sources():
    graph, warnings = parse_all()
    dim = graph.nodes["pbi_table:sales.dim_customer"]
    assert dim.metadata["partition_source"] == {
        "schema": "sales",
        "object": "dim_customer",
        "raw_kind": "sql_database",
    }
    native = graph.nodes["pbi_table:sales.orders_native"]
    assert native.metadata["native_query_tables"] == ["sales.v_orders_star"]
    assert native.metadata["partition_source"]["raw_kind"] == "native_query"
    unresolved = graph.nodes["pbi_table:sales.ext_unresolved"]
    assert unresolved.metadata.get("partition_source_unresolved") is True
    assert any(w.category == "unresolved_partition_source" for w in warnings)


def test_measure_dependencies():
    graph, _ = parse_all()
    keys = edge_keys(graph)
    spc = "measure:sales.sales per customer"
    assert (spc, "measure:sales.total sales", "references") in keys
    assert (spc, "measure:sales.customer count", "references") in keys
    assert (
        "measure:sales.total sales",
        "pbi_table:sales.fact_sales",
        "references",
    ) in keys
    assert graph.nodes[spc].metadata["dax_refs_heuristic"] is True


# ---- reports ---------------------------------------------------------------


def test_pbir_report_structure():
    graph, _ = parse_all()
    keys = edge_keys(graph)
    assert "report:sales" in graph.nodes
    page = "report_page:sales.customer overview"
    visual = "visual:sales.abc123"
    assert (page, "report:sales", "feeds") in keys
    assert (visual, page, "feeds") in keys
    assert graph.nodes[visual].metadata["visual_type"] == "card"
    assert (visual, "pbi_table:sales.dim_customer", "visualizes") in keys
    assert (visual, "measure:sales.customer count", "visualizes") in keys
    assert "pending_model_resolution" not in graph.nodes[visual].metadata


def test_legacy_report_structure():
    graph, _ = parse_all()
    keys = edge_keys(graph)
    assert "report:legacything" in graph.nodes
    visual = "visual:legacything.v1"
    assert (visual, "report_page:legacything.overview", "feeds") in keys
    assert (visual, "pbi_table:sales.dim_customer", "visualizes") in keys
    assert (visual, "measure:sales.customer count", "visualizes") in keys


# ---- pbix ------------------------------------------------------------------


def make_pbix(path: Path, with_layout: bool = True, with_mashup: bool = True) -> None:
    layout = {
        "sections": [
            {
                "name": "s1",
                "displayName": "Main",
                "visualContainers": [
                    {
                        "config": json.dumps(
                            {
                                "name": "vx",
                                "singleVisual": {
                                    "visualType": "table",
                                    "projections": {"Values": [{"queryRef": "orders.order_id"}]},
                                },
                            }
                        )
                    }
                ],
            }
        ]
    }
    section_m = (
        "section Section1;\n"
        'shared orders = let Source = Sql.Database("srv", "gold"), '
        'd = Source{[Schema="dbo",Item="orders"]}[Data] in d;\n'
    )
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr("Formulas/Section1.m", section_m)
    with zipfile.ZipFile(path, "w") as zf:
        if with_layout:
            zf.writestr("Report/Layout", json.dumps(layout).encode("utf-16-le"))
        if with_mashup:
            zf.writestr("DataMashup", b"\x00\x00\x00\x00" + inner.getvalue())
        zf.writestr("DataModel", b"\x01\x02\x03")


def pbix_entry(path: Path) -> FileEntry:
    return FileEntry(
        path=path.name,
        abs_path=str(path),
        repo_key="powerbi",
        kind=FileKind.PBIX,
        size=path.stat().st_size,
    )


def test_pbix_best_effort(tmp_path: Path):
    pbix_path = tmp_path / "Minimal.pbix"
    make_pbix(pbix_path)
    graph = LineageGraph()
    warnings = parse_pbix([pbix_entry(pbix_path)], graph)
    assert "semantic_model:minimal" in graph.nodes
    table = graph.nodes["pbi_table:minimal.orders"]
    assert table.metadata["partition_source"] == {
        "schema": "dbo",
        "object": "orders",
        "raw_kind": "sql_database",
    }
    assert "report:minimal" in graph.nodes
    # mashup recovered the model, so no opaque-model warning
    assert not any(w.category == "pbix_opaque_model" for w in warnings)


def test_pbix_opaque_model_warns(tmp_path: Path):
    pbix_path = tmp_path / "Opaque.pbix"
    make_pbix(pbix_path, with_mashup=False)
    graph = LineageGraph()
    warnings = parse_pbix([pbix_entry(pbix_path)], graph)
    assert graph.nodes["semantic_model:opaque"].metadata["pbix_model_opaque"] is True
    assert any(w.category == "pbix_opaque_model" for w in warnings)


def test_pbix_garbage_never_raises(tmp_path: Path):
    garbage = tmp_path / "Broken.pbix"
    garbage.write_bytes(b"this is not a zip at all")
    graph = LineageGraph()
    warnings = parse_pbix([pbix_entry(garbage)], graph)
    assert any(w.category == "pbix_unreadable" for w in warnings)
    assert graph.nodes == {}


# ---- determinism -----------------------------------------------------------


def test_determinism():
    first, _ = parse_all()
    second, _ = parse_all()
    assert to_json_str(first) == to_json_str(second)


def test_tmdl_imports_doc_comment_descriptions(tmp_path):
    from coop_data_doc.parsers.tmdl import parse_table_file

    tmdl = (
        "table Sales\n"
        "\t/// All booked sales orders.\n"
        "\tmeasure 'Total' = SUM(Sales[amt])\n"
        "\t/// TODO: Add description\n"
        "\tcolumn amt\n"
        "\t\tdataType: decimal\n"
        "\t/// The customer key.\n"
        "\tcolumn cust_id\n"
        "\t\tdataType: int64\n"
    )
    g = LineageGraph()

    class _E:
        path = "Sales.tmdl"
        repo_key = "powerbi"

    parse_table_file(tmdl, "Model", "semantic_model:model", _E(), g)
    measure = g.nodes["measure:model.total"]
    assert measure.metadata.get("description") == "All booked sales orders."
    cols = {c.name: c.description for c in g.nodes["pbi_table:model.sales"].columns}
    assert cols["amt"] == ""  # 'TODO: Add description' placeholder skipped
    assert cols["cust_id"] == "The customer key."


def test_mcode_analysis_services_database():
    from coop_data_doc.parsers.mcode import extract_source

    m = 'let Source = AnalysisServices.Database("powerbi://x/y", "Finance"), C = Source[Data] in C'
    ref, sql = extract_source(m)
    assert ref is not None and ref.raw_kind == "as_model" and ref.object_name == "Finance"
    assert sql == []


def test_composite_model_directquery_to_model_lineage():
    # an `entity` partition (mode: directQuery) points at a shared expression
    # whose M calls AnalysisServices.Database("…","Finance") — that must become
    # a feeds edge from the Finance model to this composite table.
    from coop_data_doc.graph.model import Node, NodeType
    from coop_data_doc.parsers.tmdl import link_composite_models, parse_table_file

    g = LineageGraph()
    g.add_node(
        Node(
            id="semantic_model:finance",
            node_type=NodeType.SEMANTIC_MODEL,
            name="finance",
            display_name="Finance",
        )
    )
    g.add_node(
        Node(
            id="semantic_model:finance + forecast",
            node_type=NodeType.SEMANTIC_MODEL,
            name="finance + forecast",
            display_name="Finance + Forecast",
        )
    )

    class _E:
        path = "Finance + Forecast.SemanticModel/definition/tables/Sector.tmdl"
        repo_key = "powerbi"

    class _EX:
        path = "Finance + Forecast.SemanticModel/definition/expressions.tmdl"
        repo_key = "powerbi"

    table_tmdl = (
        "table Sector\n"
        "\tcolumn id\n"
        "\t\tdataType: int64\n"
        "\tpartition Sector = entity\n"
        "\t\tmode: directQuery\n"
        "\t\tsource\n"
        "\t\t\tentityName: Sector\n"
        "\t\t\texpressionSource: 'DirectQuery to AS - Finance'\n"
    )
    expr_tmdl = (
        "expression 'DirectQuery to AS - Finance' =\n"
        "\t\tlet\n"
        '\t\t    Source = AnalysisServices.Database("powerbi://api.powerbi.com/v1.0/myorg/X", "Finance"),\n'
        "\t\t    Cube = Source[Data]\n"
        "\t\tin\n"
        "\t\t    Cube\n"
        "\tlineageTag: abc123\n"
    )
    model_id = "semantic_model:finance + forecast"
    parse_table_file(table_tmdl, "Finance + Forecast", model_id, _E(), g)
    parse_table_file(expr_tmdl, "Finance + Forecast", model_id, _EX(), g)

    table = g.nodes["pbi_table:finance + forecast.sector"]
    assert table.metadata.get("storage_mode") == "directquery"
    assert table.metadata["entity_source"]["expression"] == "DirectQuery to AS - Finance"
    assert g.nodes[model_id].metadata["expressions"]  # shared expression captured

    link_composite_models(g)
    keys = {(e.source_id, e.target_id, e.edge_type.value) for e in g.edges}
    assert ("semantic_model:finance", "pbi_table:finance + forecast.sector", "feeds") in keys


def test_tmdl_storage_mode_import_partition():
    from coop_data_doc.parsers.tmdl import parse_table_file

    tmdl = (
        "table Fact\n"
        "\tpartition Fact = m\n"
        "\t\tmode: import\n"
        "\t\tsource =\n"
        '\t\t\tlet Source = Sql.Database("s", "db"){[Schema="dbo",Item="fact"]}[Data] in Source\n'
    )
    g = LineageGraph()

    class _E:
        path = "M.SemanticModel/definition/tables/Fact.tmdl"
        repo_key = "powerbi"

    parse_table_file(tmdl, "M", "semantic_model:m", _E(), g)
    assert g.nodes["pbi_table:m.fact"].metadata.get("storage_mode") == "import"


def test_tmdl_multiline_measure_strips_fence_delimiters():
    # TMDL wraps multi-line measure expressions in ``` … ```. Those fence lines
    # must NOT end up in the stored DAX — otherwise the renderer's own ```dax
    # fence is split into two empty code boxes with the DAX leaking as text.
    from coop_data_doc.parsers.tmdl import parse_table_file

    tmdl = (
        "table Finance\n"
        "\tmeasure 'Forecast' = ```\n"
        "\t\t\t\n"
        "\t\t\tVAR x = SELECTEDVALUE('Rows'[Link])\n"
        "\t\t\tRETURN\n"
        "\t\t\tCALCULATE(\n"
        "\t\t\t    [Base],\n"
        "\t\t\t    KEEPFILTERS('Acct'[Id] = x)\n"
        "\t\t\t)\n"
        "\t\t\t\n"
        "\t\t\t```\n"
        "\t\tformatString: #,##0\n"
        "\tmeasure 'Simple' = SUM(Finance[amt])\n"
    )
    g = LineageGraph()

    class _E:
        path = "Finance.tmdl"
        repo_key = "powerbi"

    parse_table_file(tmdl, "Finance", "semantic_model:finance", _E(), g)
    dax = g.nodes["measure:finance.forecast"].metadata["dax"]
    assert "```" not in dax  # no stray fence delimiters
    assert dax.startswith("VAR x =")  # boilerplate indent stripped, body kept
    assert "    [Base]," in dax  # inner indentation preserved (dedented, not flattened)
    assert dax.rstrip().endswith(")")
    # the trailing property and the next measure are not swallowed by the fence
    assert g.nodes["measure:finance.simple"].metadata["dax"] == "SUM(Finance[amt])"


def test_tmdl_doc_comment_does_not_bleed_to_wrong_object(tmp_path):
    from coop_data_doc.parsers.tmdl import parse_table_file

    # a /// above a non-column construct (hierarchy/property) must NOT attach
    # to the next column
    tmdl = (
        "table Sales\n"
        "\t/// Geography drill-down hierarchy\n"
        "\thierarchy Geo\n"
        "\t\tlevel Country\n"
        "\tcolumn region\n"
        "\t\tdataType: string\n"
    )
    g = LineageGraph()

    class _E:
        path = "Sales.tmdl"
        repo_key = "powerbi"

    parse_table_file(tmdl, "Model", "semantic_model:model", _E(), g)
    cols = {c.name: c.description for c in g.nodes["pbi_table:model.sales"].columns}
    assert cols["region"] == ""  # the hierarchy's doc must not bleed onto region


def test_strip_m_comments_preserves_url_strings():
    from coop_data_doc.parsers.mcode import strip_m_comments

    out = strip_m_comments('X = "https://a/b", // trailing comment\nY = 1')
    assert '"https://a/b"' in out  # string survives intact
    assert "trailing" not in out  # the real comment is still stripped


def test_mcode_commented_out_as_database_is_not_a_source():
    # a commented-out AnalysisServices.Database line must never win over the
    # real Sql.Database navigation below it
    ref, sqls = extract_source(
        "let\n"
        '    // Source = AnalysisServices.Database("powerbi://api/x", "OldModel"),\n'
        '    Source = Sql.Database("srv", "gold"),\n'
        '    d = Source{[Schema="dbo",Item="orders"]}[Data]\n'
        "in d"
    )
    assert sqls == []
    assert ref is not None and ref.raw_kind == "sql_database"
    assert ref.schema_name == "dbo" and ref.object_name == "orders"


def test_native_query_with_no_extractable_tables_flagged_unresolved():
    """A Value.NativeQuery whose SQL yields zero real tables (ODBC {CALL},
    non-T-SQL passthrough, broken SQL) cannot be traced — the table must be
    marked unresolved and warned about, never left silently healthy-looking."""
    from coop_data_doc.parsers.tmdl import parse_table_file

    class _E:
        path = "M.SemanticModel/definition/tables/proc_fed.tmdl"
        repo_key = "powerbi"

    for native in (
        "{CALL dbo.usp_get_sales(2024)}",
        "definitely not sql at all",
    ):
        g = LineageGraph()
        tmdl = (
            "table proc_fed\n"
            "\tpartition proc_fed = m\n"
            "\t\tmode: import\n"
            "\t\tsource =\n"
            '\t\t\tlet Source = Sql.Database("s", "db"), '
            f'q = Value.NativeQuery(Source, "{native}") in q\n'
        )
        warnings = parse_table_file(tmdl, "M", "semantic_model:m", _E(), g)
        node = g.nodes["pbi_table:m.proc_fed"]
        assert node.metadata["native_query_tables"] == []
        assert node.metadata.get("partition_source_unresolved") is True
        assert any(w.category == "unresolved_partition_source" for w in warnings)


def test_tmdl_utf16_with_bom_parsed(tmp_path):
    import codecs

    from coop_data_doc.parsers.tmdl import parse_tmdl

    defn = tmp_path / "U16.SemanticModel" / "definition" / "tables"
    defn.mkdir(parents=True)
    tmdl = "table fact\n\tcolumn k\n\t\tdataType: int64\n"
    (defn / "fact.tmdl").write_bytes(codecs.BOM_UTF16_LE + tmdl.encode("utf-16-le"))
    config = Config(repos={"pbi": RepoConfig(path=str(tmp_path), include=["**/*.tmdl"])})
    inventory, _ = crawl(config)
    g = LineageGraph()
    warnings = parse_tmdl(inventory.by_kind(FileKind.TMDL), g)
    assert "pbi_table:u16.fact" in g.nodes
    assert [c.name for c in g.nodes["pbi_table:u16.fact"].columns] == ["k"]
    assert not any(w.category == "encoding_unreadable" for w in warnings)


def test_tmdl_undecodable_file_warns_not_silent(tmp_path):
    from coop_data_doc.parsers.tmdl import parse_tmdl

    defn = tmp_path / "U16.SemanticModel" / "definition" / "tables"
    defn.mkdir(parents=True)
    tmdl = "table fact\n\tcolumn k\n"
    (defn / "fact.tmdl").write_bytes(tmdl.encode("utf-16-le"))  # no BOM
    config = Config(repos={"pbi": RepoConfig(path=str(tmp_path), include=["**/*.tmdl"])})
    inventory, _ = crawl(config)
    g = LineageGraph()
    warnings = parse_tmdl(inventory.by_kind(FileKind.TMDL), g)
    encoding = [w for w in warnings if w.category == "encoding_unreadable"]
    assert len(encoding) == 1
    assert "pbi_table:u16.fact" not in g.nodes  # nothing guessed from garbage


def test_mcode_navigation_anchored_not_poisoned_by_let_step():
    from coop_data_doc.parsers.mcode import extract_source

    # a let step literally named Schema/Item must not poison the real source
    ref, _ = extract_source(
        "let\n"
        '    Schema = "junk",\n'
        '    Item = "junk",\n'
        "    Source = Sql.Database(S, D),\n"
        '    Data = Source{[Schema="sales", Item="dim_customer"]}[Data]\n'
        "in Data"
    )
    assert ref is not None and ref.schema_name == "sales" and ref.object_name == "dim_customer"


def _binding(entity, prop, kind="column"):
    return {"entity": entity, "property": prop, "kind": kind}


def _two_model_report_graph(bindings):
    """Two models (a, b) each with a `date` table; model a also has `sales`. One
    report -> page -> visual whose bindings are the given list."""
    from coop_data_doc.graph.model import Edge, EdgeType, Node, NodeType

    g = LineageGraph()
    for mid in ("a", "b"):
        g.add_node(
            Node(id=f"semantic_model:{mid}", node_type=NodeType.SEMANTIC_MODEL, name=mid, schema_name=mid)
        )
        g.add_node(
            Node(id=f"pbi_table:{mid}.date", node_type=NodeType.PBI_TABLE, name="date", schema_name=mid)
        )
    g.add_node(Node(id="pbi_table:a.sales", node_type=NodeType.PBI_TABLE, name="sales", schema_name="a"))
    g.add_node(Node(id="report:r1", node_type=NodeType.REPORT, name="r1", source_file="Report.pbip"))
    g.add_node(Node(id="report_page:p1", node_type=NodeType.REPORT_PAGE, name="p1"))
    g.add_node(
        Node(
            id="visual:v1",
            node_type=NodeType.VISUAL,
            name="v1",
            source_file="Report.pbip",
            metadata={"bindings": bindings},
        )
    )
    g.add_edge(Edge(source_id="visual:v1", target_id="report_page:p1", edge_type=EdgeType.FEEDS))
    g.add_edge(Edge(source_id="report_page:p1", target_id="report:r1", edge_type=EdgeType.FEEDS))
    return g


def test_ambiguous_binding_resolved_by_report_context():
    # issue #11: `date` is shared by both models, but the report also binds model a's
    # unique `sales` table — that context resolves the `date` binding to model a.
    g = _two_model_report_graph([_binding("sales", "amount"), _binding("date", "year")])
    warnings = link_visual_bindings(g)
    from coop_data_doc.parsers.pbir import collapse_visuals, link_reports_to_models

    link_reports_to_models(g)
    collapse_visuals(g)
    keys = edge_keys(g)
    assert ("report:r1", "pbi_table:a.date", "visualizes") in keys  # resolved to model a
    assert ("report:r1", "pbi_table:b.date", "visualizes") not in keys  # NOT model b
    assert ("report:r1", "pbi_table:a.sales", "visualizes") in keys
    assert ("semantic_model:a", "report:r1", "feeds") in keys
    assert ("semantic_model:b", "report:r1", "feeds") not in keys  # linked to model a only
    assert not any(w.category == "ambiguous_visual_binding" for w in warnings)


def test_fully_ambiguous_binding_warns_and_marks_report():
    # A report whose ONLY binding is ambiguous with no disambiguating context: no guessed
    # edge, a warning is raised, and the report node carries unresolved_bindings after collapse.
    g = _two_model_report_graph([_binding("date", "year")])
    warnings = link_visual_bindings(g)
    from coop_data_doc.parsers.pbir import collapse_visuals, link_reports_to_models

    assert any(w.category == "ambiguous_visual_binding" for w in warnings)
    link_reports_to_models(g)
    collapse_visuals(g)
    keys = edge_keys(g)
    assert ("report:r1", "pbi_table:a.date", "visualizes") not in keys
    assert ("report:r1", "pbi_table:b.date", "visualizes") not in keys
    report = g.nodes["report:r1"]
    assert report.metadata.get("unresolved_bindings") == [_binding("date", "year")]
