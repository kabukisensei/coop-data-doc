import json

import pytest

from coop_data_doc.config import Config
from coop_data_doc.crawler import FileEntry, FileKind
from coop_data_doc.graph import LineageGraph, Node, NodeType
from coop_data_doc.linker.cache import CacheEntry, LineageCache
from coop_data_doc.linker.resolver import link_graph
from coop_data_doc.parsers.pbir import parse_pbir_definitions
from coop_data_doc.parsers.sql_objects import parse_sql_objects
from coop_data_doc.parsers.tmdl import parse_tmdl


def entry(tmp_path, rel, text, kind=FileKind.TMDL, repo="powerbi"):
    path = tmp_path / repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return FileEntry(path=rel, abs_path=str(path), repo_key=repo, kind=kind, size=path.stat().st_size)


def test_multiple_partition_sources_all_link(tmp_path):
    text = """table Orders
    partition old = query
        source = SELECT id FROM dbo.orders_2024
    partition current = query
        source = SELECT id FROM dbo.orders_2025
"""
    graph = LineageGraph()
    parse_tmdl([entry(tmp_path, "Sales.SemanticModel/definition/tables/orders.tmdl", text)], graph)
    for name in ("orders_2024", "orders_2025"):
        graph.add_node(
            Node(id=f"gold_table:dbo.{name}", node_type=NodeType.GOLD_TABLE, name=name, schema_name="dbo")
        )
    link_graph(graph, Config(repos={}), LineageCache(tmp_path / "cache.json"), False)
    assert graph.upstream("pbi_table:sales.orders") == [
        "gold_table:dbo.orders_2024",
        "gold_table:dbo.orders_2025",
    ]


@pytest.mark.parametrize("kind", ["VIEW", "TABLE"])
@pytest.mark.parametrize("operation", ["UNION ALL", "EXCEPT", "INTERSECT"])
def test_set_operations_and_ctas_keep_every_dependency(tmp_path, kind, operation):
    text = f"CREATE {kind} dbo.combined AS SELECT id FROM dbo.first {operation} SELECT id FROM dbo.second;"
    graph = LineageGraph()
    parse_sql_objects([entry(tmp_path, "combined.sql", text, FileKind.SQL_FILE, "sql")], graph)
    nid = ("view" if kind == "VIEW" else "gold_table") + ":dbo.combined"
    assert set(graph.upstream(nid)) == {"gold_table:dbo.first", "gold_table:dbo.second"}


def test_changed_source_signature_does_not_apply_old_target(tmp_path):
    from coop_data_doc.linker.resolver import _collect_items

    graph = LineageGraph()
    for name in ("before", "after"):
        graph.add_node(Node(id=f"view:dbo.{name}", node_type=NodeType.VIEW, name=name, schema_name="dbo"))
    node = Node(
        id="pbi_table:sales.orders",
        node_type=NodeType.PBI_TABLE,
        name="orders",
        schema_name="sales",
        metadata={"partition_source": {"schema": "dbo", "object": "before", "raw_kind": "sql_database"}},
    )
    graph.add_node(node)
    signature = _collect_items(graph)[0].source_signature
    cache = LineageCache(tmp_path / "cache.json")
    cache.put(node.id, CacheEntry(target="view:dbo.before", method="interactive", source_signature=signature))
    old_bytes = cache.path.read_bytes()
    node.metadata["partition_source"]["object"] = "after"
    result, warnings = link_graph(graph, Config(repos={}), cache, False)
    assert "view:dbo.before" not in graph.upstream(node.id)
    assert any(w.category == "cache_source_changed" for w in warnings)
    assert cache.path.read_bytes() == old_bytes
    assert cache.mappings[node.id].target == "view:dbo.before"
    assert result.unresolved  # require review, never silently replace a human decision


@pytest.mark.parametrize("separate_repos", [False, True])
def test_same_model_name_collision_is_diagnostic_not_merged(tmp_path, separate_repos):
    graph = LineageGraph()
    entries = [entry(tmp_path, "A/Sales.SemanticModel/definition/tables/one.tmdl", "table One")]
    entries.append(
        entry(
            tmp_path,
            ("Sales" if separate_repos else "B/Sales") + ".SemanticModel/definition/tables/two.tmdl",
            "table Two",
            repo="other" if separate_repos else "powerbi",
        )
    )
    warnings = parse_tmdl(entries, graph)
    assert any(w.category == "identity_collision" for w in warnings)
    assert not ("pbi_table:sales.one" in graph.nodes and "pbi_table:sales.two" in graph.nodes)


def test_report_path_binding_does_not_link_same_basename_elsewhere(tmp_path):
    graph = LineageGraph()
    parse_tmdl(
        [entry(tmp_path, "Other/Sales.SemanticModel/definition/tables/orders.tmdl", "table Orders")], graph
    )
    report = entry(
        tmp_path,
        "Client/Sales.Report/definition.pbir",
        json.dumps({"datasetReference": {"byPath": {"path": "../Sales.SemanticModel"}}}),
        FileKind.PBIR_DEFINITION,
    )
    warnings = parse_pbir_definitions([report], graph)
    assert not graph.upstream("report:sales")
    assert graph.nodes["report:sales"].metadata["declared_model_unresolved"]
    assert any(w.category == "pbir_external_model" for w in warnings)


def test_standalone_bim_same_name_is_not_merged(tmp_path):
    from coop_data_doc.parsers.bim import parse_bim

    graph = LineageGraph()
    entries = [
        entry(
            tmp_path,
            f"{name}.bim",
            json.dumps({"name": "Sales", "model": {"tables": [{"name": name}]}}),
            FileKind.BIM,
        )
        for name in ("one", "two")
    ]
    warnings = parse_bim(entries, graph)
    assert any(w.category == "identity_collision" for w in warnings)
    assert not ("pbi_table:sales.one" in graph.nodes and "pbi_table:sales.two" in graph.nodes)


def test_changed_cache_decision_is_exposed_for_review(tmp_path):
    from coop_data_doc.linker.resolver import _collect_items

    graph = LineageGraph()
    graph.add_node(Node(id="view:dbo.new", node_type=NodeType.VIEW, name="new", schema_name="dbo"))
    table = graph.add_node(
        Node(
            id="pbi_table:sales.orders",
            node_type=NodeType.PBI_TABLE,
            name="orders",
            schema_name="sales",
            metadata={"partition_source": {"schema": "dbo", "object": "new"}},
        )
    )
    cache = LineageCache(tmp_path / "cache.json")
    cache.put(table.id, CacheEntry(target="view:dbo.new", method="interactive", source_signature="old"))
    previous = cache.path.read_bytes()
    pending = []
    result, _ = link_graph(graph, Config(repos={}), cache, True, pending_out=pending)
    assert result.unresolved == [table.id]
    assert pending[0]["source_signature"] == _collect_items(graph)[0].source_signature
    assert pending[0]["candidates"][0]["target"] == "view:dbo.new"
    assert cache.path.read_bytes() == previous


@pytest.mark.parametrize("method", ["external", "skip"])
def test_cache_null_target_roundtrip_preserves_decision(tmp_path, method):
    cache = LineageCache(tmp_path / "cache.json")
    cache.put("table", CacheEntry(target=None, method=method))
    loaded = LineageCache.load(cache.path)
    assert loaded.get("table").method == method
    assert not loaded.warnings


def test_sql_expression_change_is_visible_to_impact(tmp_path):
    from coop_data_doc.graph.diff import diff_graphs

    before, after = LineageGraph(), LineageGraph()
    parse_sql_objects(
        [
            entry(
                tmp_path,
                "first.sql",
                "CREATE VIEW dbo.result AS SELECT id + 1 AS value FROM dbo.input",
                FileKind.SQL_FILE,
                "sql",
            )
        ],
        before,
    )
    parse_sql_objects(
        [
            entry(
                tmp_path,
                "first.sql",
                "CREATE VIEW dbo.result AS SELECT id + 2 AS value FROM dbo.input",
                FileKind.SQL_FILE,
                "sql",
            )
        ],
        after,
    )
    assert "view:dbo.result" in [node.id for node in diff_graphs(before, after).changed_nodes]
