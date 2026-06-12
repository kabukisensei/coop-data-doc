from pathlib import Path

from coop_data_doc.config import Config, RepoConfig
from coop_data_doc.crawler import FileEntry, FileKind, crawl
from coop_data_doc.graph import LineageGraph, NodeType, to_json_str
from coop_data_doc.parsers.sql_procs import (
    classify_silver,
    parse_sql_procs,
    resolve_stub_references,
)
from coop_data_doc.parsers.sql_objects import parse_sql_objects

FIXTURES = Path(__file__).parent / "fixtures"


def sql_entries() -> list[FileEntry]:
    config = Config(
        repos={
            "sql": RepoConfig(
                path=str(FIXTURES / "repo_sql"),
                include=["**/*.sql"],
                exclude=["**/archive/**"],
            )
        }
    )
    inventory, _ = crawl(config)
    return inventory.by_kind(FileKind.SQL_FILE)


def parse_all() -> tuple[LineageGraph, list]:
    graph = LineageGraph()
    entries = sql_entries()
    warnings = parse_sql_objects(entries, graph)
    warnings += parse_sql_procs(entries, graph)
    resolve_stub_references(graph)
    classify_silver(graph)
    return graph, warnings


def edge_keys(graph: LineageGraph) -> set[tuple[str, str, str]]:
    return {edge.key() for edge in graph.edges}


def test_expected_nodes():
    graph, _ = parse_all()
    expected = {
        "stored_proc:dbo.usp_load_fact_sales",
        "stored_proc:dbo.usp_cursor_legacy",
        "stored_proc:dbo.usp_dynamic_refresh",
        "stored_proc:dbo.usp_audit_load",  # stub from EXEC
        "gold_table:dbo.fact_sales",
        "gold_table:dbo.agg_sales_daily",
        "gold_table:dbo.audit_log",  # written but never defined: stays gold
        "view:salespm.dim_customer",
        "view:salespm.v_orders_star",
        "silver_table:silver.sales_orders",
        "silver_table:silver.customers",
        "silver_table:silver.events",
    }
    assert set(graph.nodes) == expected


def test_main_proc_edges():
    graph, _ = parse_all()
    keys = edge_keys(graph)
    proc = "stored_proc:dbo.usp_load_fact_sales"
    assert (proc, "silver_table:silver.sales_orders", "reads") in keys
    assert (proc, "silver_table:silver.customers", "reads") in keys
    assert (proc, "gold_table:dbo.fact_sales", "writes") in keys
    assert (proc, "stored_proc:dbo.usp_audit_load", "references") in keys
    # temp tables and CTE aliases must never become nodes/edges
    assert not any("staged_orders" in node_id for node_id in graph.nodes)
    assert not any("ranked_customers" in node_id for node_id in graph.nodes)


def test_view_edges_and_columns():
    graph, _ = parse_all()
    keys = edge_keys(graph)
    view = "view:salespm.dim_customer"
    assert (view, "silver_table:silver.customers", "reads") in keys
    assert (view, "gold_table:dbo.fact_sales", "reads") in keys
    names = [column.name for column in graph.nodes[view].columns]
    assert names == ["customer_id", "customer_name", "latest_order_total"]


def test_select_star_view_flagged():
    graph, warnings = parse_all()
    node = graph.nodes["view:salespm.v_orders_star"]
    assert node.metadata.get("columns_unresolved") is True
    assert (
        "view:salespm.v_orders_star",
        "gold_table:dbo.fact_sales",
        "reads",
    ) in edge_keys(graph)
    assert any(w.category == "select_star_view" for w in warnings)


def test_create_table_columns():
    graph, _ = parse_all()
    columns = {c.name: c for c in graph.nodes["gold_table:dbo.fact_sales"].columns}
    assert set(columns) == {
        "order_id",
        "customer_id",
        "customer_name",
        "order_total",
        "load_date",
    }
    # sqlglot's tsql generator renders INT as INTEGER; both are fine
    assert columns["order_id"].data_type.replace(" ", "") in ("INT", "INTEGER")
    assert "PK" in columns["order_id"].constraints
    assert columns["order_id"].nullable is False
    assert columns["customer_name"].nullable is True
    assert columns["order_total"].data_type.replace(" ", "") in (
        "DECIMAL(18,2)",
        "NUMERIC(18,2)",  # exact T-SQL synonym; sqlglot normalizes
    )
    assert columns["load_date"].data_type.replace(" ", "") == "DATETIME2(3)"
    assert any(c.startswith("DEFAULT") for c in columns["load_date"].constraints)


def test_ctas_reads_source():
    graph, _ = parse_all()
    assert (
        "gold_table:dbo.agg_sales_daily",
        "gold_table:dbo.fact_sales",
        "reads",
    ) in edge_keys(graph)
    assert graph.nodes["gold_table:dbo.agg_sales_daily"].source_file


def test_cursor_proc_traced():
    graph, _ = parse_all()
    keys = edge_keys(graph)
    proc = "stored_proc:dbo.usp_cursor_legacy"
    assert (proc, "gold_table:dbo.audit_log", "writes") in keys
    assert (proc, "silver_table:silver.events", "reads") in keys
    # the cursor name must not be mistaken for a table
    assert not any("event_cur" in node_id for node_id in graph.nodes)


def test_dynamic_sql_warned_not_guessed():
    graph, warnings = parse_all()
    assert any(w.category == "dynamic_sql" for w in warnings)
    # the table named inside the string literal must NOT appear
    assert not any("refresh_log" in node_id for node_id in graph.nodes)


def test_classify_silver():
    graph, _ = parse_all()
    for node_id in (
        "silver_table:silver.sales_orders",
        "silver_table:silver.customers",
        "silver_table:silver.events",
    ):
        assert graph.nodes[node_id].node_type is NodeType.SILVER_TABLE
    # written-but-undefined stays gold; defined tables stay gold
    assert graph.nodes["gold_table:dbo.audit_log"].node_type is NodeType.GOLD_TABLE
    assert graph.nodes["gold_table:dbo.fact_sales"].node_type is NodeType.GOLD_TABLE


def test_view_reading_view_resolves_stub(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "base.sql").write_text(
        "CREATE VIEW dbo.v_base AS SELECT a, b FROM dbo.t_base;\nGO\n", encoding="utf-8"
    )
    (repo / "derived.sql").write_text(
        "CREATE VIEW dbo.v_derived AS SELECT a FROM dbo.v_base;\nGO\n", encoding="utf-8"
    )
    config = Config(repos={"sql": RepoConfig(path=str(repo), include=["**/*.sql"])})
    inventory, _ = crawl(config)
    graph = LineageGraph()
    parse_sql_objects(inventory.by_kind(FileKind.SQL_FILE), graph)
    resolve_stub_references(graph)
    assert "gold_table:dbo.v_base" not in graph.nodes
    assert ("view:dbo.v_derived", "view:dbo.v_base", "reads") in edge_keys(graph)


def test_end_to_end_lineage_chain():
    graph, _ = parse_all()
    upstream = graph.upstream("view:salespm.dim_customer")
    assert "silver_table:silver.sales_orders" in upstream  # via proc -> fact_sales
    assert "stored_proc:dbo.usp_load_fact_sales" in upstream
    downstream = graph.downstream("silver_table:silver.sales_orders")
    assert "view:salespm.dim_customer" in downstream


def test_determinism():
    first, _ = parse_all()
    second, _ = parse_all()
    assert to_json_str(first) == to_json_str(second)
