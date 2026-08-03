from pathlib import Path

from coop_data_doc.graph import (
    Column,
    Edge,
    EdgeType,
    LineageGraph,
    Node,
    NodeType,
    from_json_file,
    to_json_file,
    to_json_str,
)


def make_node(node_type, schema, name, **kwargs):
    return Node(
        id=Node.make_id(node_type, schema, name),
        node_type=node_type,
        name=name,
        schema_name=schema,
        **kwargs,
    )


def test_make_id_normalizes_brackets_and_case():
    assert Node.make_id(NodeType.GOLD_TABLE, "[dbo]", "[Fact Sales]") == "gold_table:dbo.fact sales"


def test_normalize_strips_power_bi_single_quotes():
    # Power BI wraps names containing spaces in single quotes; a TMDL
    # relationship endpoint like 'Fact Sales'.Amount must normalize to the
    # same form as the (already-unquoted) table node id so the two match.
    from coop_data_doc.graph.model import normalize_identifier

    assert normalize_identifier("'Fact Sales'.Amount") == "fact sales.amount"
    assert normalize_identifier("'Fact Sales'") == "fact sales"  # literal, not self-referential
    # and that literal is exactly what the table node id is built from (the match it enables)
    assert Node.make_id(NodeType.PBI_TABLE, "sales", "Fact Sales") == "pbi_table:sales.fact sales"


def test_add_node_merges_columns_and_metadata():
    g = LineageGraph()
    g.add_node(
        make_node(
            NodeType.GOLD_TABLE,
            "dbo",
            "t",
            columns=[Column(name="a", data_type="int")],
            metadata={"keep": 1},
        )
    )
    merged = g.add_node(
        make_node(
            NodeType.GOLD_TABLE,
            "dbo",
            "t",
            source_file="x.sql",
            columns=[Column(name="A"), Column(name="b", data_type="varchar(10)")],
            metadata={"keep": 2, "new": 3},
        )
    )
    assert len(g.nodes) == 1
    assert [c.name for c in merged.columns] == ["a", "b"]
    assert merged.columns[0].data_type == "int"  # existing wins
    assert merged.metadata == {"keep": 1, "new": 3}
    assert merged.source_file == "x.sql"  # empty field filled in


def test_add_edge_dedupes():
    g = LineageGraph()
    e = Edge(source_id="a", target_id="b", edge_type=EdgeType.READS)
    g.add_edge(e)
    g.add_edge(Edge(source_id="a", target_id="b", edge_type=EdgeType.READS, evidence="f.sql"))
    assert len(g.edges) == 1
    assert g.edges[0].evidence == "f.sql"  # evidence backfilled
    g.add_edge(Edge(source_id="a", target_id="b", edge_type=EdgeType.WRITES))
    assert len(g.edges) == 2


def build_chain():
    """silver -> proc -> gold -> view -> pbi_table -> visual"""
    g = LineageGraph()
    silver = g.add_node(make_node(NodeType.SILVER_TABLE, "silver", "src"))
    proc = g.add_node(make_node(NodeType.STORED_PROC, "dbo", "usp_load"))
    gold = g.add_node(make_node(NodeType.GOLD_TABLE, "dbo", "fact"))
    view = g.add_node(make_node(NodeType.VIEW, "sales", "v_fact"))
    pbit = g.add_node(make_node(NodeType.PBI_TABLE, "salesmodel", "fact"))
    vis = g.add_node(make_node(NodeType.VISUAL, "report1", "chart1"))
    g.add_edge(Edge(source_id=proc.id, target_id=silver.id, edge_type=EdgeType.READS))
    g.add_edge(Edge(source_id=proc.id, target_id=gold.id, edge_type=EdgeType.WRITES))
    g.add_edge(Edge(source_id=view.id, target_id=gold.id, edge_type=EdgeType.READS))
    g.add_edge(Edge(source_id=view.id, target_id=pbit.id, edge_type=EdgeType.FEEDS))
    g.add_edge(Edge(source_id=vis.id, target_id=pbit.id, edge_type=EdgeType.VISUALIZES))
    return g, silver, proc, gold, view, pbit, vis


def test_flow_direction_end_to_end():
    g, silver, proc, gold, view, pbit, vis = build_chain()
    assert g.upstream(vis.id) == sorted([silver.id, proc.id, gold.id, view.id, pbit.id])
    assert g.downstream(silver.id) == sorted([proc.id, gold.id, view.id, pbit.id, vis.id])
    assert g.upstream(view.id, depth=1) == [gold.id]
    assert g.downstream(view.id, depth=1) == [pbit.id]


def test_cycle_safety():
    g = LineageGraph()
    g.add_edge(Edge(source_id="a", target_id="b", edge_type=EdgeType.WRITES))
    g.add_edge(Edge(source_id="b", target_id="a", edge_type=EdgeType.WRITES))
    assert g.downstream("a") == ["b"]
    assert g.upstream("a") == ["b"]


def test_retype_node_rewrites_edges():
    _g, _silver, _proc, _gold, _view, _pbit, _vis = build_chain()
    # pretend silver came in misclassified as gold, then got retyped
    g2 = LineageGraph()
    t = g2.add_node(make_node(NodeType.GOLD_TABLE, "silver", "src"))
    p = g2.add_node(make_node(NodeType.STORED_PROC, "dbo", "usp_load"))
    old_id = t.id
    g2.add_edge(Edge(source_id=p.id, target_id=old_id, edge_type=EdgeType.READS))
    new_id = g2.retype_node(old_id, NodeType.SILVER_TABLE)
    assert new_id == "silver_table:silver.src"
    assert new_id in g2.nodes and old_id not in g2.nodes
    assert g2.edges[0].target_id == new_id


def test_retype_node_dedupes_collided_edges():
    # a proc reads two same-named tables in different layers; retyping gold ->
    # silver makes both 'reads' edges point at the same silver id, so they must
    # collapse to ONE edge (with evidence merged) rather than leaving a dup key.
    g = LineageGraph()
    proc = g.add_node(make_node(NodeType.STORED_PROC, "dbo", "usp_load"))
    gold = g.add_node(make_node(NodeType.GOLD_TABLE, "dbo", "t"))
    silver = g.add_node(make_node(NodeType.SILVER_TABLE, "dbo", "t"))
    g.add_edge(Edge(source_id=proc.id, target_id=gold.id, edge_type=EdgeType.READS, evidence="gold.sql"))
    g.add_edge(Edge(source_id=proc.id, target_id=silver.id, edge_type=EdgeType.READS, evidence="silver.sql"))
    new_id = g.retype_node(gold.id, NodeType.SILVER_TABLE)
    assert new_id == silver.id
    reads = [e for e in g.edges if e.edge_type is EdgeType.READS and e.target_id == silver.id]
    assert len(reads) == 1
    assert reads[0].evidence  # surviving edge keeps evidence (merged, not dropped)


def test_subgraph():
    g, _silver, _proc, gold, view, pbit, _vis = build_chain()
    sub = g.subgraph({gold.id, view.id, pbit.id})
    assert sorted(sub.nodes) == sorted([gold.id, view.id, pbit.id])
    assert {e.key() for e in sub.edges} == {
        (view.id, gold.id, "reads"),
        (view.id, pbit.id, "feeds"),
    }


def test_json_round_trip_and_determinism(tmp_path: Path):
    g, *_ = build_chain()
    path = tmp_path / "graph.json"
    to_json_file(g, path)
    loaded = from_json_file(path)
    # serialization canonicalizes edge order, so compare canonical forms
    assert loaded.nodes == g.nodes
    assert {e.key() for e in loaded.edges} == {e.key() for e in g.edges}
    assert to_json_str(loaded) == to_json_str(g)
    # edges serialize sorted regardless of insertion order
    g_reordered = g.model_copy(deep=True)
    g_reordered.edges.reverse()
    assert to_json_str(g_reordered) == to_json_str(g)


def test_add_edge_after_model_validate_load_uses_rebuilt_index():
    # issue #13: a graph loaded from json (LineageGraph.model_validate, as the `lineage`
    # CLI does) must dedup via the index rebuilt on load — add_edge is O(1), not a rescan.
    import json

    g = LineageGraph()
    g.add_node(Node(id="gold_table:s.a", node_type=NodeType.GOLD_TABLE, name="a", schema_name="s"))
    g.add_node(Node(id="gold_table:s.b", node_type=NodeType.GOLD_TABLE, name="b", schema_name="s"))
    g.add_edge(Edge(source_id="gold_table:s.a", target_id="gold_table:s.b", edge_type=EdgeType.FEEDS))

    reloaded = LineageGraph.model_validate(json.loads(g.model_dump_json()))
    assert len(reloaded.edges) == 1
    # a duplicate (same source/target/type) must merge, not append — proving the index exists
    dup = reloaded.add_edge(
        Edge(source_id="gold_table:s.a", target_id="gold_table:s.b", edge_type=EdgeType.FEEDS, evidence="ev")
    )
    assert len(reloaded.edges) == 1
    assert dup.evidence == "ev"  # evidence backfilled onto the existing edge
    # a genuinely new edge appends and indexes
    reloaded.add_edge(
        Edge(source_id="gold_table:s.b", target_id="gold_table:s.a", edge_type=EdgeType.REFERENCES)
    )
    assert len(reloaded.edges) == 2
