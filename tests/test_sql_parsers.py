from pathlib import Path

from coop_data_doc.config import Config, RepoConfig
from coop_data_doc.crawler import FileEntry, FileKind, crawl
from coop_data_doc.graph import LineageGraph, to_json_str
from coop_data_doc.layering import assign_layers
from coop_data_doc.parsers.sql_procs import (
    parse_sql_procs,
    resolve_stub_references,
)
from coop_data_doc.parsers.sql_objects import parse_sql_objects

FIXTURES = Path(__file__).parent / "fixtures"


def sql_config() -> Config:
    return Config(
        repos={
            "sql": RepoConfig(
                path=str(FIXTURES / "repo_sql"),
                include=["**/*.sql"],
                exclude=["**/archive/**"],
            )
        }
    )


def sql_entries() -> list[FileEntry]:
    inventory, _ = crawl(sql_config())
    return inventory.by_kind(FileKind.SQL_FILE)


def parse_all() -> tuple[LineageGraph, list]:
    graph = LineageGraph()
    entries = sql_entries()
    warnings = parse_sql_objects(entries, graph)
    warnings += parse_sql_procs(entries, graph)
    resolve_stub_references(graph)
    # the live gold->silver classification lives in layering.assign_layers
    # (pass 2: a table never written/created in the repo becomes a silver
    # source). Run it here (with no layer rules declared) so the fixture
    # mirrors the pipeline instead of the removed classify_silver helper.
    assign_layers(graph, sql_config())
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
        # a CTAS table emits only reads (no WRITES/DEFINES edge), so the
        # layering heuristic treats it as a read-only source -> silver, matching
        # the live pipeline (run_pipeline -> assign_layers pass 2).
        "silver_table:dbo.agg_sales_daily",
        "gold_table:dbo.audit_log",  # written (WRITES edge) but never defined: stays gold
        "view:sales.dim_customer",
        "view:sales.v_orders_star",
        "silver_table:silver.sales_orders",
        "silver_table:silver.customers",
        "silver_table:silver.events",
        "silver_table:silver.invoices",  # read only inside a WITH ... INSERT ... SELECT CTE
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
    # a source read only inside a WITH ... INSERT INTO ... SELECT CTE is traced
    assert (proc, "silver_table:silver.invoices", "reads") in keys
    # temp tables and CTE aliases must never become nodes/edges
    assert not any("staged_orders" in node_id for node_id in graph.nodes)
    assert not any("ranked_customers" in node_id for node_id in graph.nodes)
    assert not any("billable" in node_id for node_id in graph.nodes)  # Insert-CTE name, not a table


def test_view_edges_and_columns():
    graph, _ = parse_all()
    keys = edge_keys(graph)
    view = "view:sales.dim_customer"
    assert (view, "silver_table:silver.customers", "reads") in keys
    assert (view, "gold_table:dbo.fact_sales", "reads") in keys
    names = [column.name for column in graph.nodes[view].columns]
    assert names == ["customer_id", "customer_name", "latest_order_total"]


def test_select_star_view_flagged():
    graph, warnings = parse_all()
    node = graph.nodes["view:sales.v_orders_star"]
    assert node.metadata.get("columns_unresolved") is True
    assert (
        "view:sales.v_orders_star",
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
    # a CTAS table reads its source and (having no WRITES/DEFINES edge) is
    # classified silver by the layering heuristic, as in the live pipeline.
    assert (
        "silver_table:dbo.agg_sales_daily",
        "gold_table:dbo.fact_sales",
        "reads",
    ) in edge_keys(graph)
    assert graph.nodes["silver_table:dbo.agg_sales_daily"].source_file


def test_cursor_proc_traced():
    graph, _ = parse_all()
    keys = edge_keys(graph)
    proc = "stored_proc:dbo.usp_cursor_legacy"
    assert (proc, "gold_table:dbo.audit_log", "writes") in keys
    assert (proc, "silver_table:silver.events", "reads") in keys
    # the cursor name must not be mistaken for a table
    assert not any("event_cur" in node_id for node_id in graph.nodes)


def test_regex_fallback_resolves_cte_shadowed_and_bracketed_tables():
    """The regex fallback (used for statements sqlglot can't parse) must keep a
    *schema-qualified* table even when a CTE shares its bare name, and must not
    truncate bracketed names that contain spaces. Tested directly because
    forcing a specific statement onto the fallback path via a fixture is
    sqlglot-version-sensitive; the bug lived entirely in regex_extract."""
    from coop_data_doc.parsers.sql_common import regex_extract

    # a CTE named `orders` must not hide the real `dbo.orders` it selects from
    shadowed = regex_extract(
        "WITH orders AS (SELECT * FROM dbo.orders) INSERT INTO dbo.fact SELECT * FROM orders"
    )
    assert ("dbo", "orders") in shadowed.reads
    assert ("dbo", "fact") in shadowed.writes
    assert ("dbo", "orders") not in shadowed.writes  # the CTE alias isn't a write target

    # a CTE named `tgt` must not hide the real `dbo.tgt` it writes to
    write_collision = regex_extract(
        "WITH tgt AS (SELECT * FROM dbo.src) INSERT INTO dbo.tgt SELECT * FROM tgt"
    )
    assert ("dbo", "tgt") in write_collision.writes
    assert ("dbo", "src") in write_collision.reads

    # bracketed identifiers with spaces are captured whole, not truncated
    bracketed = regex_extract(
        "INSERT INTO dbo.fact SELECT * FROM dbo.[Order Details] od JOIN [sales].[Line Items] li ON 1 = 1"
    )
    assert ("dbo", "order details") in bracketed.reads
    assert ("sales", "line items") in bracketed.reads
    assert not any(name in {"my", "order", "line"} for _schema, name in bracketed.reads)

    # an explicit CTE column list `(a, b)` must not leak the alias as a table
    collist = regex_extract("WITH c (a, b) AS (SELECT x, y FROM dbo.src) INSERT INTO dbo.tgt SELECT * FROM c")
    assert collist.reads == {("dbo", "src")}
    assert collist.writes == {("dbo", "tgt")}

    # bracketed table aliases (with spaces) must not leak as phantom tables
    aliased = regex_extract("SELECT * FROM dbo.orders AS [my alias] JOIN dbo.lines [other alias] ON 1 = 1")
    assert ("dbo", "orders") in aliased.reads and ("dbo", "lines") in aliased.reads
    assert not any(name in {"my", "alias", "other"} for _schema, name in aliased.reads)


def test_qualify_respects_bracketed_dots():
    """A literal dot *inside* brackets must not be split into schema.name:
    `[my.proc]` is the object `my.proc` in the default `dbo` schema, and
    `[dbo].[my.weird.table]` keeps the whole bracketed segment as the name."""
    from coop_data_doc.parsers.sql_common import original_name, qualify

    # bracketed names containing a literal dot stay intact
    assert qualify("[my.proc]") == ("dbo", "my.proc")
    assert qualify("[dbo].[my.weird.table]") == ("dbo", "my.weird.table")
    assert original_name("[dbo].[my.weird.table]") == "my.weird.table"

    # ordinary cases still behave
    assert qualify("dbo.t") == ("dbo", "t")
    assert qualify("[dbo].[t]") == ("dbo", "t")
    assert qualify("t") == ("dbo", "t")
    assert qualify("db.dbo.t") == ("dbo", "t")  # db part dropped from 3-part name
    assert original_name("[dim].[Practice]") == "Practice"  # original case preserved


def test_native_query_extracts_sql_with_comma_in_first_arg():
    """Value.NativeQuery(Sql.Database("srv","db"), "SELECT ...") has a top-level
    comma inside its first argument; the SQL extractor must skip past it and
    capture the real query, not mistake the database name for the SQL."""
    from coop_data_doc.parsers.mcode import extract_source

    src, native_sql = extract_source(
        'let S = Value.NativeQuery(Sql.Database("srv","mydb"), '
        '"SELECT a FROM dbo.orders JOIN dbo.cust ON 1=1") in S'
    )
    assert src is not None and src.raw_kind == "native_query"
    assert native_sql == ["SELECT a FROM dbo.orders JOIN dbo.cust ON 1=1"]
    assert "mydb" not in native_sql  # the DB name must not be captured as the SQL

    # the single-identifier first-arg form must still work
    _src2, native_sql2 = extract_source('Value.NativeQuery(Source, "SELECT x FROM dbo.foo")')
    assert native_sql2 == ["SELECT x FROM dbo.foo"]


def test_bracketed_identifier_apostrophe_does_not_swallow_terminator():
    """A `'` inside a [...]-bracketed identifier (e.g. [Owner's Name]) is part
    of the name, not a string-literal start, so it must not make split_statements
    merge across a `;` or make scrub eat the rest of the chunk."""
    from coop_data_doc.parsers.sql_common import regex_extract, scrub, split_statements

    stmts = split_statements("UPDATE dbo.t SET [Owner's Name] = 'x'; DELETE FROM dbo.archive")
    assert len(stmts) == 2  # the ';' terminator is not swallowed

    scrubbed = scrub("INSERT INTO [dbo].[O'Brien] SELECT 1; SELECT * FROM dbo.x", strip_strings=True)
    assert "dbo.x" in scrubbed  # the rest of the chunk survives

    lineage = regex_extract("UPDATE dbo.t SET [Owner's Name] = 'x'; DELETE FROM dbo.archive")
    assert ("dbo", "t") in lineage.writes
    assert ("dbo", "archive") in lineage.writes  # the DELETE write edge is not dropped


def test_proc_and_object_names_with_spaces_captured_whole():
    """The bracket-aware _IDENT pattern must capture spaced bracketed names
    intact in PROC_HEADER_RE and the CREATE TABLE/VIEW regex fallbacks, instead
    of truncating `[dbo].[My Proc]` at the first space."""
    from coop_data_doc.parsers.sql_common import PROC_HEADER_RE, qualify
    from coop_data_doc.parsers.sql_objects import _TABLE_FALLBACK_RE, _VIEW_FALLBACK_RE

    proc = PROC_HEADER_RE.search("CREATE PROCEDURE [dbo].[My Proc] AS SELECT 1")
    assert qualify(proc.group(1)) == ("dbo", "my proc")

    table = _TABLE_FALLBACK_RE.search("CREATE TABLE [dbo].[Order Details] (x int)")
    assert qualify(table.group(1)) == ("dbo", "order details")
    table2 = _TABLE_FALLBACK_RE.search("CREATE TABLE dbo.[Order Details](x int)")
    assert qualify(table2.group(1)) == ("dbo", "order details")

    view = _VIEW_FALLBACK_RE.search("CREATE VIEW [dbo].[My View] AS SELECT 1")
    assert qualify(view.group(1)) == ("dbo", "my view")


def test_exec_return_status_capture_finds_real_callee():
    """EXEC @rc = dbo.RealProc captures a return-status variable first; the
    callee regex must skip the `@var =` and resolve the real procedure, never
    inventing a phantom `dbo.@rc` reference."""
    from coop_data_doc.parsers.sql_common import EXEC_RE, qualify

    match = EXEC_RE.match("EXEC @rc = dbo.RealProc")
    assert match is not None
    assert qualify(match.group(1)) == ("dbo", "realproc")
    assert not match.group(1).startswith("@")

    # plain EXEC (no return-status var) still resolves the callee
    assert qualify(EXEC_RE.match("EXEC dbo.OtherProc").group(1)) == ("dbo", "otherproc")


def test_proc_body_as_after_parenthesized_default():
    """_find_proc must locate the body-introducing AS at top level, not an AS
    inside a parenthesized parameter default (e.g. CAST(GETDATE() AS DATE)),
    which would slice the body mid-parameter-list and corrupt lineage."""
    from coop_data_doc.parsers.sql_procs import _find_proc

    name, body = _find_proc(
        "CREATE PROCEDURE dbo.MyProc @D DATETIME = CAST(GETDATE() AS DATE) "
        "AS INSERT INTO dbo.target SELECT col FROM dbo.src"
    )
    assert name == "dbo.MyProc"
    assert body.strip() == "INSERT INTO dbo.target SELECT col FROM dbo.src"


def test_dynamic_sql_warned_not_guessed():
    graph, warnings = parse_all()
    assert any(w.category == "dynamic_sql" for w in warnings)
    # the table named inside the string literal must NOT appear
    assert not any("refresh_log" in node_id for node_id in graph.nodes)
    # and the gap must be visible to doc consumers, not just the console
    proc = graph.nodes["stored_proc:dbo.usp_dynamic_refresh"]
    assert proc.metadata["dynamic_sql_untraced"] is True


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
    upstream = graph.upstream("view:sales.dim_customer")
    assert "silver_table:silver.sales_orders" in upstream  # via proc -> fact_sales
    assert "stored_proc:dbo.usp_load_fact_sales" in upstream
    downstream = graph.downstream("silver_table:silver.sales_orders")
    assert "view:sales.dim_customer" in downstream


def test_determinism():
    first, _ = parse_all()
    second, _ = parse_all()
    assert to_json_str(first) == to_json_str(second)
