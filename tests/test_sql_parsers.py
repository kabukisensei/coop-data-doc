from pathlib import Path

from coop_data_doc.config import Config, RepoConfig
from coop_data_doc.crawler import FileEntry, FileKind, crawl
from coop_data_doc.graph import LineageGraph, to_json_str
from coop_data_doc.layering import assign_layers
from coop_data_doc.parsers.sql_objects import parse_sql_objects
from coop_data_doc.parsers.sql_procs import (
    parse_sql_procs,
    resolve_stub_references,
)

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
        # a CTAS table emits only reads (no WRITES/DEFINES edge), but it has a
        # source_file — it is defined by repo DDL — so the layering heuristic
        # keeps it gold (issue #29), matching the live pipeline
        # (run_pipeline -> assign_layers pass 2).
        "gold_table:dbo.agg_sales_daily",
        "gold_table:dbo.audit_log",  # written (WRITES edge) but never defined: stays gold
        "view:sales.dim_customer",
        "view:sales.v_orders_star",
        "view:dbo.fn_sales_window",  # inline TVF: a view-shaped node (issue #31)
        "view:sales.v_sales_window_recent",  # reads the TVF like a view
        "silver_table:silver.sales_orders",
        "silver_table:silver.customers",
        "silver_table:silver.events",
        "silver_table:silver.invoices",  # read only inside a WITH ... INSERT ... SELECT CTE
        "gold_table:db.base_table",
        "view:db.view_hop_1",
        "view:db.view_hop_2",
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
    # a CTAS table reads its source; despite having no WRITES/DEFINES edge it
    # stays gold because its source_file proves it is defined by repo DDL
    # (issue #29), as in the live pipeline.
    assert (
        "gold_table:dbo.agg_sales_daily",
        "gold_table:dbo.fact_sales",
        "reads",
    ) in edge_keys(graph)
    assert graph.nodes["gold_table:dbo.agg_sales_daily"].source_file


def test_inline_tvf_parsed_as_view_like_node():
    # issue #31: an inline table-valued function is a view-shaped object — a
    # name, a column contract, and reads — tagged object_kind: inline_tvf.
    graph, warnings = parse_all()
    fn = graph.nodes["view:dbo.fn_sales_window"]
    assert fn.metadata["object_kind"] == "inline_tvf"
    assert fn.source_file
    assert [c.name for c in fn.columns] == ["order_id", "order_total", "customer_name"]
    keys = edge_keys(graph)
    assert ("view:dbo.fn_sales_window", "gold_table:dbo.fact_sales", "reads") in keys
    assert ("view:dbo.fn_sales_window", "silver_table:silver.customers", "reads") in keys
    # a cleanly parsed TVF emits no warnings for its file
    assert not any(w.file.endswith("fn_sales_window.sql") for w in warnings)


def test_inline_tvf_caller_gets_reads_edge():
    # issue #31: `FROM dbo.fn_sales_window(...)` in a caller resolves to the
    # TVF's view node (the schema-qualified function-call source is kept and
    # the stub folded by resolve_stub_references).
    graph, _ = parse_all()
    keys = edge_keys(graph)
    assert ("view:sales.v_sales_window_recent", "view:dbo.fn_sales_window", "reads") in keys
    assert "gold_table:dbo.fn_sales_window" not in graph.nodes


def test_unqualified_table_functions_never_become_sources(tmp_path: Path):
    # built-ins (STRING_SPLIT, OPENJSON…) are never schema-qualified — they
    # must stay dropped, never guessed into lineage (hard rule 4).
    graph, _ = _parse_objects_sql(
        tmp_path,
        "CREATE VIEW dbo.v_split AS "
        "SELECT s.value FROM STRING_SPLIT('a,b', ',') AS s JOIN dbo.real_table AS r ON r.k = s.value;",
    )
    assert "view:dbo.v_split" in graph.nodes
    reads = {e.target_id for e in graph.edges if e.edge_type.value == "reads"}
    assert reads == {"gold_table:dbo.real_table"}


def test_sql_no_objects_safety_net(tmp_path: Path):
    # issue #31: a classified .sql file yielding zero nodes and zero warnings
    # (a scalar function, a GRANT script, an empty file) is flagged — never a
    # silent coverage gap. Files that contribute a node or a warning are not.
    from coop_data_doc.parsers.sql_objects import flag_silent_sql_files

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "fn_scalar.sql").write_text(
        "CREATE FUNCTION dbo.fn_add (@a INT) RETURNS INT AS BEGIN RETURN @a + 1; END;",
        encoding="utf-8",
    )
    (repo / "grants.sql").write_text("GRANT SELECT ON SCHEMA::sales TO reporting_role;", encoding="utf-8")
    (repo / "table.sql").write_text("CREATE TABLE dbo.t (id INT);", encoding="utf-8")
    config = Config(repos={"sql": RepoConfig(path=str(repo), include=["**/*.sql"])})
    inventory, _ = crawl(config)
    entries = inventory.by_kind(FileKind.SQL_FILE)
    graph = LineageGraph()
    warnings = parse_sql_objects(entries, graph)
    warnings += parse_sql_procs(entries, graph)
    flagged = flag_silent_sql_files(entries, graph, warnings)
    assert [w.file for w in flagged] == ["fn_scalar.sql", "grants.sql"]
    assert all(w.category == "sql_no_objects" for w in flagged)


def test_no_sql_no_objects_for_contributing_fixture_files():
    # every fixture file contributes a node or a diagnostic, so the safety
    # net stays quiet on the estate (including the inline TVF, which now
    # contributes a node).
    from coop_data_doc.parsers.sql_objects import flag_silent_sql_files

    graph, warnings = parse_all()
    assert flag_silent_sql_files(sql_entries(), graph, warnings) == []


def _parse_objects_sql(tmp_path: Path, sql: str) -> tuple[LineageGraph, list]:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "obj.sql").write_text(sql, encoding="utf-8")
    config = Config(repos={"sql": RepoConfig(path=str(repo), include=["**/*.sql"])})
    inventory, _ = crawl(config)
    graph = LineageGraph()
    warnings = parse_sql_objects(inventory.by_kind(FileKind.SQL_FILE), graph)
    return graph, warnings


def test_openrowset_ctas_no_phantom_source(tmp_path: Path):
    # Fabric review: OPENROWSET(BULK ...) option keywords (FORMAT/DATA_SOURCE) were
    # recovered by sqlglot as phantom source tables (e.g. dbo.format) with a bogus
    # reads-edge and NO warning — silently wrong lineage (hard rule 4). The external
    # file is not a warehouse object, so like COPY INTO it contributes no source node.
    graph, _ = _parse_objects_sql(
        tmp_path,
        "CREATE TABLE dbo.tgt AS SELECT c1 FROM OPENROWSET(BULK 'https://x/y.parquet', FORMAT='PARQUET') AS r;",
    )
    assert sorted(graph.nodes) == ["gold_table:dbo.tgt"]
    assert "gold_table:dbo.format" not in graph.nodes
    assert not any(e.edge_type.value == "reads" for e in graph.edges)


def test_openrowset_with_real_join_keeps_real_source(tmp_path: Path):
    # A genuine table joined alongside an OPENROWSET must still be captured — only the
    # option-keyword phantom is dropped, never a real source.
    graph, _ = _parse_objects_sql(
        tmp_path,
        "CREATE TABLE dbo.tgt AS SELECT a FROM dbo.real x JOIN OPENROWSET(BULK 'u', FORMAT='CSV') AS r ON 1=1;",
    )
    keys = edge_keys(graph)
    assert ("gold_table:dbo.tgt", "gold_table:dbo.real", "reads") in keys
    assert "gold_table:dbo.format" not in graph.nodes


def test_clone_of_captures_source_lineage(tmp_path: Path):
    # Fabric review: `CREATE TABLE tgt AS CLONE OF src` degrades to an opaque Command;
    # the target was created via regex fallback but the clone SOURCE was silently
    # dropped, losing the genuine table->table lineage. It must now be captured.
    graph, warnings = _parse_objects_sql(tmp_path, "CREATE TABLE dbo.tgt AS CLONE OF dbo.src;")
    keys = edge_keys(graph)
    assert ("gold_table:dbo.tgt", "gold_table:dbo.src", "reads") in keys
    assert any(w.category == "regex_fallback" for w in warnings)


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
    assert qualify("db.dbo.t") == ("db.dbo", "t")  # db part of a 3-part name is preserved
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


def _parse_proc_sql(tmp_path: Path, sql: str) -> tuple[LineageGraph, list]:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "proc.sql").write_text(sql, encoding="utf-8")
    config = Config(repos={"sql": RepoConfig(path=str(repo), include=["**/*.sql"])})
    inventory, _ = crawl(config)
    graph = LineageGraph()
    warnings = parse_sql_procs(inventory.by_kind(FileKind.SQL_FILE), graph)
    return graph, warnings


def test_conditional_exec_creates_reference_edge(tmp_path: Path):
    # issue #10: a conditional `IF <cond> EXEC x` shares its chunk with the IF line, so the
    # start-anchored EXEC match missed it and the callee vanished with no warning.
    graph, _ = _parse_proc_sql(
        tmp_path,
        "CREATE PROCEDURE dbo.orchestrator AS\n"
        "BEGIN\n"
        "    IF @full_load = 1\n"
        "        EXEC etl.load_customers;\n"
        "    EXEC etl.load_orders;\n"
        "END\n"
        "GO\n",
    )
    keys = edge_keys(graph)
    src = "stored_proc:dbo.orchestrator"
    assert (src, "stored_proc:etl.load_customers", "references") in keys
    assert (src, "stored_proc:etl.load_orders", "references") in keys


def test_same_line_if_exec_creates_reference_edge(tmp_path: Path):
    # issue #43: `IF <cond> EXEC x` on ONE line (not IF then EXEC on the next).
    # EXEC_RE is anchored to line start, so the callee was silently dropped with
    # parse_quality still "ast" — a proc-orchestration link lost without a hint.
    graph, _ = _parse_proc_sql(
        tmp_path,
        "CREATE PROCEDURE dbo.orchestrator AS\n"
        "BEGIN\n"
        "    IF @debug = 1 EXEC dbo.log_something;\n"
        "    ELSE EXEC dbo.fallback;\n"
        "END\n"
        "GO\n",
    )
    keys = edge_keys(graph)
    src = "stored_proc:dbo.orchestrator"
    assert (src, "stored_proc:dbo.log_something", "references") in keys
    assert (src, "stored_proc:dbo.fallback", "references") in keys


def test_same_line_while_and_begin_exec_reference_edges(tmp_path: Path):
    # `WHILE <cond> EXEC x` and a single-line `IF <cond> BEGIN EXEC y END` are the
    # same class of same-line conditional EXEC and must both link their callee.
    graph, _ = _parse_proc_sql(
        tmp_path,
        "CREATE PROCEDURE dbo.looper AS\n"
        "BEGIN\n"
        "    WHILE @i < 10 EXEC dbo.step;\n"
        "    IF @flag = 1 BEGIN EXEC dbo.finish END\n"
        "END\n"
        "GO\n",
    )
    keys = edge_keys(graph)
    src = "stored_proc:dbo.looper"
    assert (src, "stored_proc:dbo.step", "references") in keys
    assert (src, "stored_proc:dbo.finish", "references") in keys


def test_same_line_execute_as_owner_is_not_a_phantom(tmp_path: Path):
    # `IF ... EXECUTE AS OWNER` must not invent a phantom proc named `as`/`owner`;
    # the same-line detection reuses the _EXEC_AS_KEYWORDS rejection.
    graph, _ = _parse_proc_sql(
        tmp_path,
        "CREATE PROCEDURE dbo.secure AS\n"
        "BEGIN\n"
        "    IF @impersonate = 1 EXECUTE AS OWNER;\n"
        "    EXEC etl.load;\n"
        "END\n"
        "GO\n",
    )
    proc_names = {n.name for n in graph.nodes.values()}
    assert "as" not in proc_names and "owner" not in proc_names
    assert (
        "stored_proc:dbo.secure",
        "stored_proc:etl.load",
        "references",
    ) in edge_keys(graph)


def test_two_execs_in_one_semicolonless_chunk(tmp_path: Path):
    graph, _ = _parse_proc_sql(
        tmp_path,
        "CREATE PROCEDURE dbo.runner AS\nBEGIN\n    EXEC etl.a\n    EXEC etl.b\nEND\nGO\n",
    )
    keys = edge_keys(graph)
    src = "stored_proc:dbo.runner"
    assert (src, "stored_proc:etl.a", "references") in keys
    assert (src, "stored_proc:etl.b", "references") in keys


def test_execute_as_context_is_not_a_phantom_proc(tmp_path: Path):
    graph, _ = _parse_proc_sql(
        tmp_path,
        "CREATE PROCEDURE dbo.secure AS\n"
        "BEGIN\n"
        "    EXECUTE AS USER = 'dbo';\n"
        "    EXEC etl.load;\n"
        "    REVERT;\n"
        "END\n"
        "GO\n",
    )
    proc_names = {n.name for n in graph.nodes.values()}
    assert "as" not in proc_names  # EXECUTE AS did not invent a proc named 'as'
    assert (
        "stored_proc:dbo.secure",
        "stored_proc:etl.load",
        "references",
    ) in edge_keys(graph)


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


def test_regex_fallback_ignores_create_in_comments_and_strings(tmp_path: Path):
    # issue #9: an unparseable batch that only MENTIONS CREATE TABLE/VIEW inside a `--`
    # comment, a /* */ comment, or a '...' string literal must NOT create a phantom node
    # (hard rule 4: never guess lineage). The fallback now probes the scrubbed text.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "deploy.sql").write_text(
        "-- NOTE: CREATE TABLE dbo.retired_table was removed in 2024; see wiki.\n"
        "/* also CREATE VIEW dbo.old_view (removed) */\n"
        "PRINT 'CREATE TABLE dbo.string_table';\n"
        ':setvar Environment "prod"\n'
        "EXOTIC NONSQL SYNTAX %%~ GO-AWAY\n",
        encoding="utf-8",
    )
    config = Config(repos={"sql": RepoConfig(path=str(repo), include=["**/*.sql"])})
    inventory, _ = crawl(config)
    graph = LineageGraph()
    parse_sql_objects(inventory.by_kind(FileKind.SQL_FILE), graph)
    assert graph.nodes == {}  # none of the commented/quoted CREATEs became a node


def test_nested_block_comment_hides_archived_insert(tmp_path: Path):
    # issue #42: T-SQL block comments NEST — `/* outer /* inner */ still outer */`
    # is ONE comment. A flat find('*/') stopped at the inner `*/` and un-hid the
    # archived INSERT after it, leaking phantom reads/writes edges alongside the
    # real lineage. The nesting-aware scanner must keep the whole block hidden.
    graph, warnings = _parse_proc_sql(
        tmp_path,
        "CREATE PROCEDURE dbo.loader AS\n"
        "BEGIN\n"
        "    /* archived approach /* v1 note */\n"
        "       INSERT INTO dbo.phantom_target SELECT * FROM dbo.phantom_source */\n"
        "    INSERT INTO dbo.real_target SELECT * FROM dbo.real_source;\n"
        "END\n"
        "GO\n",
    )
    node_ids = set(graph.nodes)
    assert not any("phantom" in node_id for node_id in node_ids)
    keys = edge_keys(graph)
    src = "stored_proc:dbo.loader"
    assert (src, "gold_table:dbo.real_target", "writes") in keys
    assert (src, "gold_table:dbo.real_source", "reads") in keys
    # the real statement parses cleanly (AST), so no regex fallback fires
    assert not any(w.category == "regex_fallback" for w in warnings)
    assert graph.nodes[src].metadata["parse_quality"] == "ast"


def test_nested_block_comment_around_archived_proc_header(tmp_path: Path):
    # Worse failure mode: a nested comment containing an archived CREATE PROCEDURE
    # header used to document the PHANTOM proc as the file's only proc, so the real
    # proc got no node and its lineage was attributed to the phantom.
    graph, warnings = _parse_proc_sql(
        tmp_path,
        "/* legacy design /* superseded 2024 */\n"
        "   CREATE PROCEDURE dbo.phantom_proc AS SELECT * FROM dbo.old_source */\n"
        "CREATE PROCEDURE dbo.real_proc AS\n"
        "BEGIN\n"
        "    INSERT INTO dbo.tgt SELECT * FROM dbo.src;\n"
        "END\n"
        "GO\n",
    )
    assert "stored_proc:dbo.phantom_proc" not in graph.nodes
    assert "stored_proc:dbo.real_proc" in graph.nodes
    assert not any("old_source" in node_id for node_id in graph.nodes)
    keys = edge_keys(graph)
    src = "stored_proc:dbo.real_proc"
    assert (src, "gold_table:dbo.tgt", "writes") in keys
    assert (src, "gold_table:dbo.src", "reads") in keys
    assert not any(w.category == "regex_fallback" for w in warnings)


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


# ---- parser edge-case estate (tests/fixtures/repo_sql_edge) -----------------
# A second miniature repo covering historically silent failure modes: legacy
# semicolon-less proc bodies, parens/keywords inside comments and strings,
# aliased DELETE targets, and cross-database names. Kept separate from
# repo_sql so the crawler/CLI suites' exact-count assertions stay stable.


def edge_config() -> Config:
    return Config(
        repos={
            "sql": RepoConfig(
                path=str(FIXTURES / "repo_sql_edge"),
                include=["**/*.sql"],
            )
        }
    )


def parse_edge_repo() -> tuple[LineageGraph, list]:
    inventory, _ = crawl(edge_config())
    entries = inventory.by_kind(FileKind.SQL_FILE)
    graph = LineageGraph()
    warnings = parse_sql_objects(entries, graph)
    warnings += parse_sql_procs(entries, graph)
    resolve_stub_references(graph)
    return graph, warnings


def test_edge_repo_expected_nodes():
    graph, _ = parse_edge_repo()
    expected = {
        "stored_proc:dbo.usp_refresh_fact_sales",
        "stored_proc:dbo.usp_load_sales",
        "stored_proc:dbo.usp_purge_cancelled",
        "stored_proc:dbo.usp_load_external",
        "gold_table:dbo.dim_customer",
        "gold_table:dbo.customers",
        "gold_table:dbo.load_log",
        "gold_table:dbo.fact_sales",
        "gold_table:dbo.stg_orders",
        "gold_table:dbo.dim_date",
        "gold_table:stg.sales",
        "gold_table:dbo.orders",
        "gold_table:dbo.archive_flags",
        # cross-db / linked-server reads keep their database qualifiers, so
        # they can never be conflated with the local dbo.customers
        "gold_table:otherdb.dbo.customers",
        "gold_table:lnksrv.otherdb.dbo.customers",
    }
    assert set(graph.nodes) == expected


def test_semicolonless_proc_body_lineage_not_lost():
    """A legacy proc body with NO semicolons is one un-parseable chunk; it must
    flow to the regex fallback (all four DML statements traced) and be honestly
    downgraded to regex_fallback quality — never a silent partial 'ast' parse."""
    graph, warnings = parse_edge_repo()
    keys = edge_keys(graph)
    proc = "stored_proc:dbo.usp_refresh_fact_sales"
    assert (proc, "gold_table:dbo.load_log", "writes") in keys
    assert (proc, "gold_table:dbo.fact_sales", "writes") in keys  # UPDATE alias resolved
    assert (proc, "gold_table:dbo.stg_orders", "writes") in keys  # DELETE FROM
    assert (proc, "gold_table:dbo.dim_date", "reads") in keys
    assert graph.nodes[proc].metadata["parse_quality"] == "regex_fallback"
    assert any(w.category == "regex_fallback" for w in warnings)
    # the UPDATE/JOIN aliases must not leak as tables
    assert "gold_table:dbo.fs" not in graph.nodes
    assert "gold_table:dbo.o" not in graph.nodes


def test_proc_header_parens_in_comments_and_strings_do_not_drop_proc():
    """Unbalanced parens inside header comments ('-- fiscal year (see wiki',
    '-- 1) full, 2) incremental') or string defaults (N'(') must not corrupt
    the paren-depth scan that locates the body AS."""
    graph, _ = parse_edge_repo()
    keys = edge_keys(graph)
    proc = "stored_proc:dbo.usp_load_sales"
    assert proc in graph.nodes
    assert (proc, "gold_table:dbo.fact_sales", "writes") in keys
    assert (proc, "gold_table:stg.sales", "reads") in keys
    assert graph.nodes[proc].metadata["parse_quality"] == "ast"


def test_find_proc_ignores_parens_in_comments_and_strings():
    from coop_data_doc.parsers.sql_procs import _find_proc

    found = _find_proc(
        "CREATE PROCEDURE dbo.usp_LoadSales\n"
        "    @Year INT = 2024, -- fiscal year (see wiki\n"
        "    @Prefix NVARCHAR(5) = N'('\n"
        "AS\n"
        "INSERT INTO dbo.fact_sales SELECT col FROM stg.sales"
    )
    assert found is not None
    name, body = found
    assert name == "dbo.usp_LoadSales"
    assert "INSERT INTO dbo.fact_sales" in body


def test_proc_header_without_body_as_warns_not_silent():
    """When a CREATE PROCEDURE header exists but no top-level body AS is found,
    the batch is skipped with a warning — a proc must never vanish silently."""
    _, warnings = parse_edge_repo()
    broken = [w for w in warnings if w.category == "proc_body_not_found"]
    assert len(broken) == 1
    assert "usp_broken_no_as" in broken[0].message


def test_comment_mentioning_create_procedure_does_not_drop_table_batch():
    """A doc comment naming the loading proc must not make parse_sql_objects
    skip the batch as 'a proc' (and parse_sql_procs drop it for lack of AS)."""
    graph, _ = parse_edge_repo()
    table = graph.nodes["gold_table:dbo.dim_customer"]
    names = [column.name for column in table.columns]
    assert names == ["customer_id", "customer_name"]
    # and the proc named only inside the comment must not become a node
    assert not any("usp_load_dim_customer" in node_id for node_id in graph.nodes)


def test_delete_alias_resolves_to_real_table():
    """DELETE alias FROM table AS alias must write to the real table; the alias
    itself must never become a phantom node or reads edge."""
    graph, _ = parse_edge_repo()
    keys = edge_keys(graph)
    proc = "stored_proc:dbo.usp_purge_cancelled"
    assert (proc, "gold_table:dbo.orders", "writes") in keys
    assert (proc, "gold_table:dbo.archive_flags", "reads") in keys
    assert "gold_table:dbo.o" not in graph.nodes
    assert (proc, "gold_table:dbo.o", "reads") not in keys


def test_cross_db_reads_not_conflated_with_local_tables():
    """3-part (db.schema.object) and 4-part (server.db.schema.object) names keep
    their qualifiers: a read of OtherDb.dbo.customers must not attach to the
    local dbo.customers node."""
    graph, _ = parse_edge_repo()
    keys = edge_keys(graph)
    proc = "stored_proc:dbo.usp_load_external"
    assert (proc, "gold_table:otherdb.dbo.customers", "reads") in keys
    assert (proc, "gold_table:lnksrv.otherdb.dbo.customers", "reads") in keys
    assert (proc, "gold_table:dbo.customers", "reads") not in keys


def test_cross_db_names_preserved_in_helpers():
    import sqlglot

    from coop_data_doc.parsers.sql_common import collect_source_tables, qualify

    assert qualify("OtherDb.dbo.customers") == ("otherdb.dbo", "customers")
    assert qualify("LNKSRV.OtherDb.dbo.customers") == ("lnksrv.otherdb.dbo", "customers")

    three = sqlglot.parse_one("SELECT * FROM OtherDb.dbo.customers", read="tsql")
    assert collect_source_tables(three) == {("otherdb.dbo", "customers")}
    four = sqlglot.parse_one("SELECT * FROM LNKSRV.OtherDb.dbo.customers", read="tsql")
    assert collect_source_tables(four) == {("lnksrv.otherdb.dbo", "customers")}


def test_utf16_sql_files_parsed_via_bom(tmp_path: Path):
    """SSMS 'Save with Encoding' Unicode (UTF-16, BOM) files must parse like
    UTF-8 ones instead of decoding to NUL-interleaved garbage."""
    import codecs

    script = (
        "CREATE TABLE dbo.orders (order_id INT NOT NULL PRIMARY KEY);\n"
        "GO\n"
        "CREATE VIEW dbo.v_sales AS SELECT order_id FROM dbo.orders;\n"
        "GO\n"
    )
    (tmp_path / "utf16le.sql").write_bytes(codecs.BOM_UTF16_LE + script.encode("utf-16-le"))
    (tmp_path / "utf16be.sql").write_bytes(codecs.BOM_UTF16_BE + script.encode("utf-16-be"))
    config = Config(repos={"sql": RepoConfig(path=str(tmp_path), include=["**/*.sql"])})
    inventory, _ = crawl(config)
    graph = LineageGraph()
    warnings = parse_sql_objects(inventory.by_kind(FileKind.SQL_FILE), graph)
    assert "gold_table:dbo.orders" in graph.nodes
    assert "view:dbo.v_sales" in graph.nodes
    assert not any(w.category == "encoding_unreadable" for w in warnings)


def test_crlf_sql_files_normalized_to_lf(tmp_path: Path):
    """git autocrlf on Windows checks sources out as CRLF; source_code must be
    LF regardless, or rendered pages embed CRLF inside code fences and `check`
    reports docs built on another OS as stale (the cross-OS byte-identity
    guarantee)."""
    from coop_data_doc.parsers.sql_common import decode_text

    script = "CREATE VIEW dbo.v_sales AS\r\nSELECT order_id\r\nFROM dbo.orders;\r\n"
    (tmp_path / "crlf.sql").write_bytes(script.encode("utf-8"))
    config = Config(repos={"sql": RepoConfig(path=str(tmp_path), include=["**/*.sql"])})
    inventory, _ = crawl(config)
    graph = LineageGraph()
    parse_sql_objects(inventory.by_kind(FileKind.SQL_FILE), graph)
    node = graph.nodes["view:dbo.v_sales"]
    assert "\r" not in node.source_code
    assert "CREATE VIEW dbo.v_sales AS\nSELECT order_id" in node.source_code
    # the BOM'd UTF-16 decode path (SSMS Unicode save) normalizes too
    assert decode_text("x\r\ny\rz".encode("utf-16")) == "x\ny\nz"


def test_undecodable_sql_file_warns_not_silent(tmp_path: Path):
    """A BOM-less UTF-16 (or binary) file can't be parsed — but it must produce
    an encoding warning, never silently contribute nothing. The warning is
    emitted once (by parse_sql_objects), not duplicated by parse_sql_procs."""
    script = "CREATE TABLE dbo.orders (order_id INT NOT NULL PRIMARY KEY);\nGO\n"
    (tmp_path / "nobom.sql").write_bytes(script.encode("utf-16-le"))  # no BOM
    config = Config(repos={"sql": RepoConfig(path=str(tmp_path), include=["**/*.sql"])})
    inventory, _ = crawl(config)
    entries = inventory.by_kind(FileKind.SQL_FILE)
    graph = LineageGraph()
    warnings = parse_sql_objects(entries, graph)
    warnings += parse_sql_procs(entries, graph)
    encoding = [w for w in warnings if w.category == "encoding_unreadable"]
    assert len(encoding) == 1
    assert encoding[0].file == "nobom.sql"


def test_edge_repo_determinism():
    first, _ = parse_edge_repo()
    second, _ = parse_edge_repo()
    assert to_json_str(first) == to_json_str(second)


def test_sql_file_read_exactly_once_across_both_parsers(tmp_path: Path, monkeypatch):
    # issue #15: a shared read_cache makes each SQL file read + decoded ONCE across the
    # object AND proc passes (the second pass is a cache hit — no re-read, no Defender tax).
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.sql").write_text(
        "CREATE TABLE dbo.t (a int);\nGO\nCREATE PROCEDURE dbo.p AS SELECT 1 FROM dbo.t;\n",
        encoding="utf-8",
    )
    config = Config(repos={"sql": RepoConfig(path=str(repo), include=["**/*.sql"])})
    inventory, _ = crawl(config)  # crawl before patching so only the parser reads are counted
    entries = inventory.by_kind(FileKind.SQL_FILE)

    counts: dict[str, int] = {}
    real_read = Path.read_bytes

    def counting_read(self):
        counts[str(self)] = counts.get(str(self), 0) + 1
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", counting_read)
    graph = LineageGraph()
    cache: dict[str, str] = {}
    parse_sql_objects(entries, graph, read_cache=cache)
    parse_sql_procs(entries, graph, read_cache=cache)
    sql_reads = [n for p, n in counts.items() if p.endswith("a.sql")]
    assert sql_reads == [1], counts  # one file, two parser passes, ONE read
