from pathlib import Path

from coop_data_doc.config import Config, LayerRule, RepoConfig
from coop_data_doc.crawler import FileKind, crawl
from coop_data_doc.graph import Edge, EdgeType, LineageGraph, Node, NodeType
from coop_data_doc.layering import assign_layers, prune_schemas
from coop_data_doc.parsers.sql_objects import parse_sql_objects


def node(graph, node_type, schema, name, source_file=""):
    return graph.add_node(
        Node(
            id=Node.make_id(node_type, schema, name),
            node_type=node_type,
            name=name,
            schema_name=schema,
            source_file=source_file,
        )
    )


def cfg(layers):
    return Config(repos={"sql": RepoConfig(path=".")}, layers=layers)


def test_layer_by_schema():
    g = LineageGraph()
    node(g, NodeType.GOLD_TABLE, "erp_orders", "customers")  # created table, but bronze schema
    node(g, NodeType.GOLD_TABLE, "mart", "fact_sales")
    # mark both as written so the heuristic wouldn't touch them
    g.add_edge(
        Edge(
            source_id="stored_proc:dbo.p",
            target_id="gold_table:erp_orders.customers",
            edge_type=EdgeType.WRITES,
        )
    )
    g.add_edge(
        Edge(source_id="stored_proc:dbo.p", target_id="gold_table:mart.fact_sales", edge_type=EdgeType.WRITES)
    )
    assign_layers(
        g,
        cfg(
            {
                "bronze": LayerRule(schemas=["erp_orders"]),
                "gold": LayerRule(schemas=["mart"]),
            }
        ),
    )
    assert "bronze_table:erp_orders.customers" in g.nodes
    assert "gold_table:mart.fact_sales" in g.nodes


def test_layer_by_path_glob():
    g = LineageGraph()
    node(g, NodeType.GOLD_TABLE, "dbo", "dim_date", source_file="warehouse/dim/dim_date.sql")
    g.add_edge(
        Edge(source_id="stored_proc:dbo.p", target_id="gold_table:dbo.dim_date", edge_type=EdgeType.WRITES)
    )
    assign_layers(g, cfg({"gold": LayerRule(paths=["**/dim/**"])}))
    assert g.nodes["gold_table:dbo.dim_date"].node_type is NodeType.GOLD_TABLE
    assert g.nodes["gold_table:dbo.dim_date"].metadata["layer"] == "gold"


def test_view_and_proc_get_layer_metadata():
    g = LineageGraph()
    node(g, NodeType.VIEW, "common", "v_shared", source_file="gold/views/v_shared.sql")
    node(g, NodeType.STORED_PROC, "mart", "usp_build", source_file="gold/proc/usp_build.sql")
    assign_layers(g, cfg({"gold": LayerRule(schemas=["common", "mart"])}))
    assert g.nodes["view:common.v_shared"].metadata["layer"] == "gold"
    assert g.nodes["stored_proc:mart.usp_build"].metadata["layer"] == "gold"


def test_precedence_gold_over_bronze():
    g = LineageGraph()
    # schema is in bronze list, but path is a gold folder → gold wins
    node(g, NodeType.GOLD_TABLE, "erp_orders", "x", source_file="gold/dim/x.sql")
    g.add_edge(Edge(source_id="p", target_id="gold_table:erp_orders.x", edge_type=EdgeType.WRITES))
    assign_layers(
        g,
        cfg({"bronze": LayerRule(schemas=["erp_orders"]), "gold": LayerRule(paths=["**/dim/**"])}),
    )
    assert "gold_table:erp_orders.x" in g.nodes
    assert "bronze_table:erp_orders.x" not in g.nodes


def test_heuristic_fallback_when_no_rule_matches():
    g = LineageGraph()
    # read-only source (never written) and no rule → silver by heuristic
    node(g, NodeType.GOLD_TABLE, "mystery", "src")
    g.add_edge(Edge(source_id="view:dbo.v", target_id="gold_table:mystery.src", edge_type=EdgeType.READS))
    warnings = assign_layers(g, cfg({"gold": LayerRule(schemas=["mart"])}))
    assert "silver_table:mystery.src" in g.nodes
    assert any(w.category == "layer_unclassified" for w in warnings)


def test_repo_ddl_table_stays_gold_without_writer(tmp_path):
    # issue #29: a standalone CREATE TABLE DDL file produces a gold_table node
    # with a source_file but NO writes/defines edge. Being defined in the repo
    # is the strongest "created here" evidence, so the heuristic must not
    # demote it to silver — and must not warn about it.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "ext.thing.sql").write_text(
        "CREATE TABLE ext.thing (id INT NOT NULL, label NVARCHAR(50) NULL);\nGO\n",
        encoding="utf-8",
    )
    config = Config(
        repos={"sql": RepoConfig(path=str(repo), include=["**/*.sql"])},
        layers={"gold": LayerRule(schemas=["mart"])},  # no rule covers "ext"
    )
    inventory, _ = crawl(config)
    g = LineageGraph()
    parse_sql_objects(inventory.by_kind(FileKind.SQL_FILE), g)
    assert not any(e.edge_type in (EdgeType.WRITES, EdgeType.DEFINES) for e in g.edges)
    warnings = assign_layers(g, config)
    assert "gold_table:ext.thing" in g.nodes
    assert g.nodes["gold_table:ext.thing"].source_file
    assert not any(w.category == "layer_unclassified" for w in warnings)


def test_heuristic_demotes_only_sourceless_stub_tables():
    # a stub table (referenced by a read, no source_file, no writer) still
    # demotes to silver with the warning; a repo-defined sibling does not.
    g = LineageGraph()
    node(g, NodeType.GOLD_TABLE, "mystery", "src")  # stub: no source_file
    node(g, NodeType.GOLD_TABLE, "ext", "thing", source_file="tables/ext.thing.sql")
    g.add_edge(Edge(source_id="view:dbo.v", target_id="gold_table:mystery.src", edge_type=EdgeType.READS))
    warnings = assign_layers(g, cfg({"gold": LayerRule(schemas=["mart"])}))
    assert "silver_table:mystery.src" in g.nodes
    assert "gold_table:ext.thing" in g.nodes
    unclassified = [w for w in warnings if w.category == "layer_unclassified"]
    assert len(unclassified) == 1
    assert "mystery" in unclassified[0].file or "src" in unclassified[0].file


def test_bronze_only_via_rules_never_heuristic():
    g = LineageGraph()
    node(g, NodeType.GOLD_TABLE, "src", "t")  # read-only source, no bronze rule
    g.add_edge(Edge(source_id="view:dbo.v", target_id="gold_table:src.t", edge_type=EdgeType.READS))
    assign_layers(g, cfg({}))  # no layers at all → pure heuristic, no bronze
    assert "silver_table:src.t" in g.nodes
    assert not any(n.startswith("bronze_table:") for n in g.nodes)


def test_skipping_silver_layer():
    # only bronze + gold declared; a declared bronze source stays bronze
    g = LineageGraph()
    node(g, NodeType.GOLD_TABLE, "erp_finance", "ledger")
    g.add_edge(Edge(source_id="p", target_id="gold_table:erp_finance.ledger", edge_type=EdgeType.READS))
    assign_layers(g, cfg({"bronze": LayerRule(schemas=["erp_finance"]), "gold": LayerRule(schemas=["mart"])}))
    assert "bronze_table:erp_finance.ledger" in g.nodes


def test_prune_system_and_ignored_schemas():
    g = LineageGraph()
    node(g, NodeType.GOLD_TABLE, "dbo", "fact_sales")
    node(g, NodeType.GOLD_TABLE, "sys", "objects")  # system → always dropped
    node(g, NodeType.VIEW, "information_schema", "columns")  # system → dropped
    node(g, NodeType.SILVER_TABLE, "staging", "raw")  # ignored by config
    node(g, NodeType.GOLD_TABLE, "db_owner", "x")  # db_* prefix → dropped
    g.add_edge(Edge(source_id="view:dbo.v", target_id="silver_table:staging.raw", edge_type=EdgeType.READS))
    dropped = prune_schemas(g, ["staging"])
    assert dropped == 4
    assert set(g.nodes) == {"gold_table:dbo.fact_sales"}
    # edges touching dropped nodes are gone too
    assert all("staging" not in e.source_id and "staging" not in e.target_id for e in g.edges)


def test_prune_leaves_pbi_nodes_alone():
    # a semantic model whose name happens to collide with a schema word must
    # not be pruned (pbi nodes carry the model name in schema_name)
    g = LineageGraph()
    node(g, NodeType.PBI_TABLE, "sys", "metrics")  # schema_name == model name "sys"
    node(g, NodeType.SEMANTIC_MODEL, "", "sys")
    dropped = prune_schemas(g, [])
    assert dropped == 0
    assert "pbi_table:sys.metrics" in g.nodes


def test_prune_matches_multipart_cross_db_schemas():
    # cross-db reads carry multi-part schemas ("otherdb.sys",
    # "linkedsrv.otherdb.staging"); pruning must key on the final segment
    # (the actual schema) so catalog noise and ignored schemas are dropped
    # wherever the database lives
    g = LineageGraph()
    node(g, NodeType.GOLD_TABLE, "dbo", "fact_sales")  # kept
    node(g, NodeType.GOLD_TABLE, "otherdb.sys", "objects")  # cross-db system → dropped
    node(g, NodeType.SILVER_TABLE, "linkedsrv.otherdb.staging", "raw")  # ignored schema → dropped
    node(g, NodeType.GOLD_TABLE, "otherdb.db_datareader", "x")  # cross-db db_* role → dropped
    node(g, NodeType.GOLD_TABLE, "otherdb.dbo", "customers")  # cross-db user data → kept
    dropped = prune_schemas(g, ["staging"])
    assert dropped == 3
    assert set(g.nodes) == {"gold_table:dbo.fact_sales", "gold_table:otherdb.dbo.customers"}


def test_prune_full_multipart_schema_in_ignore_list():
    # a user can ignore one specific database's schema without ignoring the
    # same-named schema locally
    g = LineageGraph()
    node(g, NodeType.GOLD_TABLE, "otherdb.audit", "log")  # ignored exactly → dropped
    node(g, NodeType.GOLD_TABLE, "audit", "log")  # local audit schema → kept
    dropped = prune_schemas(g, ["otherdb.audit"])
    assert dropped == 1
    assert set(g.nodes) == {"gold_table:audit.log"}


def test_prune_include_schemas_restricts_to_listed():
    # non-empty include_schemas: ONLY the listed schemas survive
    g = LineageGraph()
    node(g, NodeType.GOLD_TABLE, "mart", "fact_sales")  # included → kept
    node(g, NodeType.SILVER_TABLE, "stg", "raw")  # not included → dropped
    node(g, NodeType.VIEW, "common", "v_shared")  # not included → dropped
    g.add_edge(
        Edge(
            source_id="view:common.v_shared", target_id="gold_table:mart.fact_sales", edge_type=EdgeType.READS
        )
    )
    dropped = prune_schemas(g, [], ["mart"])
    assert dropped == 2
    assert set(g.nodes) == {"gold_table:mart.fact_sales"}
    assert all("common" not in e.source_id for e in g.edges)  # edges to dropped nodes go too


def test_prune_include_schemas_empty_is_noop():
    # empty = no restriction (back-compat for hand-written configs)
    g = LineageGraph()
    node(g, NodeType.GOLD_TABLE, "mart", "t")
    node(g, NodeType.SILVER_TABLE, "stg", "raw")
    assert prune_schemas(g, [], []) == 0
    assert prune_schemas(g, []) == 0  # default argument: also no restriction
    assert len(g.nodes) == 2


def test_prune_include_schemas_ignore_wins_on_conflict():
    # a schema in BOTH lists is dropped — ignore beats include
    g = LineageGraph()
    node(g, NodeType.GOLD_TABLE, "mart", "t")
    node(g, NodeType.GOLD_TABLE, "common", "c")
    dropped = prune_schemas(g, ["mart"], ["mart", "common"])
    assert dropped == 1
    assert set(g.nodes) == {"gold_table:common.c"}


def test_prune_include_schemas_matches_multipart_schemas():
    # same rule as ignore_schemas for REAL parsed nodes: the full multi-part
    # schema OR its final segment must be listed for the node to survive
    g = LineageGraph()
    node(g, NodeType.GOLD_TABLE, "otherdb.mart", "t", source_file="x.sql")  # segment listed → kept
    node(g, NodeType.GOLD_TABLE, "linkedsrv.otherdb.mart", "y", source_file="x.sql")  # full form → kept
    node(g, NodeType.GOLD_TABLE, "otherdb.stg", "x", source_file="x.sql")  # neither → dropped
    dropped = prune_schemas(g, [], ["mart", "linkedsrv.otherdb.mart"])
    assert dropped == 1
    assert set(g.nodes) == {"gold_table:otherdb.mart.t", "gold_table:linkedsrv.otherdb.mart.y"}


def test_prune_include_schemas_drops_crossdb_stub_on_segment_match():
    # the live-estate leak: a view reads [lh_db].[bronze].t, creating a stub in
    # ANOTHER database's schema. Selecting the local "bronze" must NOT keep
    # that definition-less stub (and its edges) in the docs.
    g = LineageGraph()
    node(g, NodeType.VIEW, "resource", "v_sales", source_file="v.sql")
    node(g, NodeType.GOLD_TABLE, "lh_db.bronze", "t")  # cross-db stub (no source_file)
    g.add_edge(
        Edge(
            source_id="view:resource.v_sales", target_id="gold_table:lh_db.bronze.t", edge_type=EdgeType.READS
        )
    )
    dropped = prune_schemas(g, [], ["resource", "bronze"])
    assert dropped == 1
    assert set(g.nodes) == {"view:resource.v_sales"}
    assert g.edges == []  # the reference is simply absent, like ignore_schemas


def test_prune_include_schemas_keeps_crossdb_stub_when_full_schema_listed():
    # a user can include one specific database's schema exactly (mirrors the
    # ignore_schemas multipart rule)
    g = LineageGraph()
    node(g, NodeType.GOLD_TABLE, "lh_db.bronze", "t")  # full multi-part form listed → kept
    node(g, NodeType.GOLD_TABLE, "otherdb.bronze", "u")  # only the segment matches → dropped
    dropped = prune_schemas(g, [], ["lh_db.bronze"])
    assert dropped == 1
    assert set(g.nodes) == {"gold_table:lh_db.bronze.t"}


def test_prune_include_schemas_keeps_same_db_stub():
    # a same-db reference to an uncrawled object in a SELECTED schema keeps its
    # stub (single-part schema, final-segment match) — unchanged behavior
    g = LineageGraph()
    node(g, NodeType.VIEW, "resource", "v_sales", source_file="v.sql")
    node(g, NodeType.GOLD_TABLE, "bronze", "not_crawled")  # stub in a selected schema → kept
    dropped = prune_schemas(g, [], ["resource", "bronze"])
    assert dropped == 0
    assert "gold_table:bronze.not_crawled" in g.nodes


def _crossdb_read_estate(tmp_path: Path, include_schemas: list[str]) -> Config:
    """A view doing a three-part cross-warehouse read, with a schema allowlist."""
    sql = tmp_path / "sql-repo"
    sql.mkdir()
    (tmp_path / "pbi-repo").mkdir()
    (sql / "v.sql").write_text(
        "CREATE VIEW resource.v_sales AS SELECT a FROM [lh_db].[bronze].erp_projperiodempl;\nGO\n",
        encoding="utf-8",
    )
    return Config(
        repos={
            "sql": RepoConfig(path=str(sql), include=["**/*.sql"]),
            "powerbi": RepoConfig(path=str(tmp_path / "pbi-repo")),
        },
        include_schemas=include_schemas,
    )


def test_include_schemas_crossdb_stub_never_reaches_graph(tmp_path):
    # end-to-end regression for the live-estate leak: with include_schemas set,
    # the three-part read's target is in a schema the user did NOT select
    # (lh_db.bronze ≠ the local bronze) — no stub node, no edge, no page
    from coop_data_doc.cli import run_pipeline

    config = _crossdb_read_estate(tmp_path, ["resource", "bronze"])
    graph, _, _ = run_pipeline(config, interactive=False)
    assert set(graph.nodes) == {"view:resource.v_sales"}
    assert graph.edges == []  # the reference is simply absent


def test_include_schemas_empty_keeps_crossdb_stub(tmp_path):
    # with no allowlist the stub is created exactly as before
    from coop_data_doc.cli import run_pipeline

    config = _crossdb_read_estate(tmp_path, [])
    graph, _, _ = run_pipeline(config, interactive=False)
    assert "silver_table:lh_db.bronze.erp_projperiodempl" in graph.nodes
    assert any(e.target_id == "silver_table:lh_db.bronze.erp_projperiodempl" for e in graph.edges)


def test_prune_include_schemas_leaves_pbi_nodes_alone():
    g = LineageGraph()
    node(g, NodeType.PBI_TABLE, "sales", "metrics")  # schema_name == model name
    dropped = prune_schemas(g, [], ["mart"])
    assert dropped == 0
    assert "pbi_table:sales.metrics" in g.nodes


def test_layer_path_glob_case_insensitive_all_platforms():
    # deterministic cross-OS policy (mirrors crawler._matches): an uppercase
    # source path still matches a lowercase layer glob on every platform
    g = LineageGraph()
    node(g, NodeType.GOLD_TABLE, "dbo", "dim_date", source_file="Warehouse/DIM/Dim_Date.SQL")
    g.add_edge(
        Edge(source_id="stored_proc:dbo.p", target_id="gold_table:dbo.dim_date", edge_type=EdgeType.WRITES)
    )
    assign_layers(g, cfg({"gold": LayerRule(paths=["**/dim/**"])}))
    assert g.nodes["gold_table:dbo.dim_date"].metadata["layer"] == "gold"
