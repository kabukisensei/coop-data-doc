from pathlib import Path

import pytest

from coop_data_doc.config import Config, RepoConfig, SchemaMapping
from coop_data_doc.graph import LineageGraph, Node, NodeType
from coop_data_doc.linker import interactive
from coop_data_doc.linker.cache import CacheEntry, LineageCache
from coop_data_doc.linker.resolver import link_graph

MODEL = "Sales Analytics"
MODEL_KEY = "sales analytics"


def make_node(node_type, schema, name, **kwargs):
    return Node(
        id=Node.make_id(node_type, schema, name),
        node_type=node_type,
        name=name,
        schema_name=schema if node_type is not NodeType.PBI_TABLE else MODEL_KEY,
        **kwargs,
    )


def pbi_table(graph, name, source_schema, source_object):
    node = Node(
        id=Node.make_id(NodeType.PBI_TABLE, MODEL, name),
        node_type=NodeType.PBI_TABLE,
        name=name,
        schema_name=MODEL_KEY,
        metadata={
            "partition_source": {
                "schema": source_schema,
                "object": source_object,
                "raw_kind": "sql_database",
            }
        },
    )
    return graph.add_node(node)


def build_graph() -> LineageGraph:
    graph = LineageGraph()
    graph.add_node(make_node(NodeType.VIEW, "sales", "dim_customer"))
    graph.add_node(make_node(NodeType.VIEW, "sales", "fact_sales"))
    graph.add_node(make_node(NodeType.GOLD_TABLE, "dbo", "fact_sales"))
    graph.add_node(make_node(NodeType.SILVER_TABLE, "silver", "events"))
    pbi_table(graph, "dim_customer", "sales", "dim_customer")  # exact
    pbi_table(graph, "fact_sales", "gold", "fact_sales")  # config rule
    pbi_table(graph, "dim_customerz", "sales", "dim_customerz")  # fuzzy auto
    pbi_table(graph, "dcust", "sales", "dcust")  # ambiguous -> prompt
    pbi_table(graph, "mystery", "zzz", "qqq")  # garbage -> unresolved
    return graph


def make_config() -> Config:
    return Config(
        repos={"sql": RepoConfig(path=".")},
        schema_mappings=[SchemaMapping(schema="sales", model=MODEL)],
    )


class FakeQuestionary:
    """questionary stand-in: records calls, returns a fixed answer."""

    class Choice:
        def __init__(self, title, value):
            self.title = title
            self.value = value

    class Separator:
        pass

    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    def select(self, message, choices):
        self.calls += 1
        answer = self.answer

        class _Result:
            @staticmethod
            def ask():
                return answer

        return _Result()


@pytest.fixture
def fake_q(monkeypatch):
    fake = FakeQuestionary("view:sales.dim_customer")
    monkeypatch.setattr(interactive, "questionary", fake)
    return fake


def cache_at(tmp_path: Path) -> LineageCache:
    return LineageCache.load(tmp_path / ".lineage-cache.json")


def edge_keys(graph):
    return {edge.key() for edge in graph.edges}


def test_resolution_ladder(tmp_path: Path, fake_q):
    graph = build_graph()
    cache = cache_at(tmp_path)
    result, warnings = link_graph(graph, make_config(), cache, interactive_mode=True)

    assert result.methods == {"exact": 1, "config_rule": 1, "fuzzy": 1, "interactive": 1}
    assert fake_q.calls == 1
    keys = edge_keys(graph)
    assert ("view:sales.dim_customer", f"pbi_table:{MODEL_KEY}.dim_customer", "feeds") in keys
    assert ("view:sales.fact_sales", f"pbi_table:{MODEL_KEY}.fact_sales", "feeds") in keys
    assert ("view:sales.dim_customer", f"pbi_table:{MODEL_KEY}.dim_customerz", "feeds") in keys
    assert ("view:sales.dim_customer", f"pbi_table:{MODEL_KEY}.dcust", "feeds") in keys
    assert result.unresolved == [f"pbi_table:{MODEL_KEY}.mystery"]
    assert graph.nodes[f"pbi_table:{MODEL_KEY}.mystery"].metadata["unresolved"] is True
    assert any(w.category == "fuzzy_auto" for w in warnings)
    # only the interactive answer is cached
    assert list(cache.mappings) == [f"pbi_table:{MODEL_KEY}.dcust"]


def test_second_run_asks_nothing(tmp_path: Path, fake_q):
    cache = cache_at(tmp_path)
    link_graph(build_graph(), make_config(), cache, interactive_mode=True)
    assert fake_q.calls == 1

    cache2 = cache_at(tmp_path)
    graph2 = build_graph()
    result2, _ = link_graph(graph2, make_config(), cache2, interactive_mode=True)
    assert fake_q.calls == 1  # ZERO additional prompts
    assert result2.methods["cache"] == 1
    assert (
        "view:sales.dim_customer",
        f"pbi_table:{MODEL_KEY}.dcust",
        "feeds",
    ) in edge_keys(graph2)


def test_prune_invalid_reprompts(tmp_path: Path, fake_q):
    cache = cache_at(tmp_path)
    cache.put(
        f"pbi_table:{MODEL_KEY}.dcust",
        CacheEntry(target="view:sales.does_not_exist", method="interactive"),
    )
    result, warnings = link_graph(build_graph(), make_config(), cache, interactive_mode=True)
    assert any(w.category == "cache_pruned" for w in warnings)
    assert fake_q.calls == 1  # re-prompted after prune
    assert cache.get(f"pbi_table:{MODEL_KEY}.dcust").target == "view:sales.dim_customer"


def test_external_choice_cached(tmp_path: Path, monkeypatch):
    fake = FakeQuestionary(interactive.EXTERNAL_CHOICE)
    monkeypatch.setattr(interactive, "questionary", fake)
    cache = cache_at(tmp_path)
    graph = build_graph()
    result, _ = link_graph(graph, make_config(), cache, interactive_mode=True)
    node = graph.nodes[f"pbi_table:{MODEL_KEY}.dcust"]
    assert node.metadata["external_source"] is True
    assert cache.get(f"pbi_table:{MODEL_KEY}.dcust").method == "external"

    graph2 = build_graph()
    link_graph(graph2, make_config(), cache_at(tmp_path), interactive_mode=True)
    assert fake.calls == 1  # cached external answer short-circuits


def test_non_interactive_leaves_unresolved(tmp_path: Path, fake_q):
    graph = build_graph()
    result, _ = link_graph(graph, make_config(), cache_at(tmp_path), interactive_mode=False)
    assert fake_q.calls == 0
    assert result.unresolved == sorted([f"pbi_table:{MODEL_KEY}.dcust", f"pbi_table:{MODEL_KEY}.mystery"])


def test_cache_file_stable(tmp_path: Path):
    cache = cache_at(tmp_path)
    cache.put("b", CacheEntry(target="view:x.y", method="interactive"))
    cache.put("a", CacheEntry(target=None, method="skip"))
    first = cache.path.read_bytes()
    cache.write()
    assert cache.path.read_bytes() == first
    reloaded = LineageCache.load(cache.path)
    assert list(reloaded.mappings) == ["a", "b"]


def test_unknown_cache_version_ignored(tmp_path: Path):
    path = tmp_path / ".lineage-cache.json"
    path.write_text('{"version": 99, "mappings": {"x": {"target": null, "method": "skip"}}}')
    cache = LineageCache.load(path)
    assert cache.mappings == {}
    assert any(w.category == "cache_invalid" for w in cache.warnings)
    assert path.read_text().startswith('{"version": 99')  # file untouched
