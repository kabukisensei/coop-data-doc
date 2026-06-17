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
    relationships = graph.nodes["semantic_model:sales"].metadata["relationships"]
    assert relationships == [{"from": "fact_sales.customer_id", "to": "dim_customer.customer_id"}]


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
