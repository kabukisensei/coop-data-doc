from coop_data_doc.config import Config, LayerRule, RepoConfig
from coop_data_doc.graph import Edge, EdgeType, LineageGraph, Node, NodeType
from coop_data_doc.layering import assign_layers, prune_schemas


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
    node(g, NodeType.GOLD_TABLE, "d365po", "customers")  # created table, but bronze schema
    node(g, NodeType.GOLD_TABLE, "dwm", "fact_sales")
    # mark both as written so the heuristic wouldn't touch them
    g.add_edge(
        Edge(
            source_id="stored_proc:dbo.p", target_id="gold_table:d365po.customers", edge_type=EdgeType.WRITES
        )
    )
    g.add_edge(
        Edge(source_id="stored_proc:dbo.p", target_id="gold_table:dwm.fact_sales", edge_type=EdgeType.WRITES)
    )
    assign_layers(
        g,
        cfg(
            {
                "bronze": LayerRule(schemas=["d365po"]),
                "gold": LayerRule(schemas=["dwm"]),
            }
        ),
    )
    assert "bronze_table:d365po.customers" in g.nodes
    assert "gold_table:dwm.fact_sales" in g.nodes


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
    node(g, NodeType.STORED_PROC, "dwm", "usp_build", source_file="gold/proc/usp_build.sql")
    assign_layers(g, cfg({"gold": LayerRule(schemas=["common", "dwm"])}))
    assert g.nodes["view:common.v_shared"].metadata["layer"] == "gold"
    assert g.nodes["stored_proc:dwm.usp_build"].metadata["layer"] == "gold"


def test_precedence_gold_over_bronze():
    g = LineageGraph()
    # schema is in bronze list, but path is a gold folder → gold wins
    node(g, NodeType.GOLD_TABLE, "d365po", "x", source_file="gold/dim/x.sql")
    g.add_edge(Edge(source_id="p", target_id="gold_table:d365po.x", edge_type=EdgeType.WRITES))
    assign_layers(
        g,
        cfg({"bronze": LayerRule(schemas=["d365po"]), "gold": LayerRule(paths=["**/dim/**"])}),
    )
    assert "gold_table:d365po.x" in g.nodes
    assert "bronze_table:d365po.x" not in g.nodes


def test_heuristic_fallback_when_no_rule_matches():
    g = LineageGraph()
    # read-only source (never written) and no rule → silver by heuristic
    node(g, NodeType.GOLD_TABLE, "mystery", "src")
    g.add_edge(Edge(source_id="view:dbo.v", target_id="gold_table:mystery.src", edge_type=EdgeType.READS))
    warnings = assign_layers(g, cfg({"gold": LayerRule(schemas=["dwm"])}))
    assert "silver_table:mystery.src" in g.nodes
    assert any(w.category == "layer_unclassified" for w in warnings)


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
    node(g, NodeType.GOLD_TABLE, "d365fo", "ledger")
    g.add_edge(Edge(source_id="p", target_id="gold_table:d365fo.ledger", edge_type=EdgeType.READS))
    assign_layers(g, cfg({"bronze": LayerRule(schemas=["d365fo"]), "gold": LayerRule(schemas=["dwm"])}))
    assert "bronze_table:d365fo.ledger" in g.nodes


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
