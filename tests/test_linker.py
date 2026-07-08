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


def test_cache_writes_non_ascii_identifiers_literally(tmp_path: Path):
    """Non-ASCII cache keys (e.g. café.ñoño from a Unicode table name) must be
    written literally, not \\uXXXX-escaped, matching serialize.to_json_str's
    ensure_ascii=False so the committed cache stays readable/diffable."""
    cache = cache_at(tmp_path)
    cache.put("café.ñoño", CacheEntry(target="view:café.ñoño", method="interactive"))
    cache.write()
    raw = cache.path.read_text(encoding="utf-8")
    assert "café.ñoño" in raw
    assert "\\u" not in raw  # no escaped Unicode


def test_unknown_cache_version_ignored(tmp_path: Path):
    path = tmp_path / ".lineage-cache.json"
    path.write_text('{"version": 99, "mappings": {"x": {"target": null, "method": "skip"}}}')
    cache = LineageCache.load(path)
    assert cache.mappings == {}
    assert any(w.category == "cache_invalid" for w in cache.warnings)
    assert path.read_text().startswith('{"version": 99')  # file untouched


@pytest.mark.parametrize("payload", ["[1, 2, 3]", "42", '"a string"', "true", "null"])
def test_wrong_shape_cache_degrades_not_crashes(tmp_path: Path, payload: str):
    """A committed .lineage-cache.json that is valid JSON of the WRONG shape (a
    top-level list/number/string/bool/null) must degrade to an empty cache with a
    warning, never AttributeError — `.get()` on a non-dict crashes, and a non-empty
    non-dict is truthy so an `or {}` fallback wouldn't save it."""
    path = tmp_path / ".lineage-cache.json"
    path.write_text(payload, encoding="utf-8")
    cache = LineageCache.load(path)  # must not raise
    assert cache.mappings == {}
    assert any(w.category == "cache_invalid" for w in cache.warnings)


class _RaisingQuestionary(FakeQuestionary):
    """questionary stand-in whose prompt raises — mimics questionary's no-TTY
    OSError / prompt_toolkit's Windows NoConsoleScreenBufferError."""

    def __init__(self, exc):
        super().__init__(None)
        self._exc = exc

    def select(self, message, choices):
        self.calls += 1
        exc = self._exc

        class _Result:
            @staticmethod
            def ask():
                raise exc

        return _Result()


class NoConsoleScreenBufferError(Exception):
    """Name-matches prompt_toolkit's Windows error (which is detected by class name)."""


@pytest.mark.parametrize("exc", [OSError("no tty"), NoConsoleScreenBufferError()])
def test_no_terminal_degrades_instead_of_crashing(tmp_path: Path, monkeypatch, exc):
    # interactive_mode=True, but the only terminal is unavailable (run by the agent /
    # another program): the build must NOT crash — it leaves the ambiguous link
    # unresolved and warns, so the docs still generate.
    monkeypatch.setattr(interactive, "questionary", _RaisingQuestionary(exc))
    graph = build_graph()
    cache = cache_at(tmp_path)

    result, warnings = link_graph(graph, make_config(), cache, interactive_mode=True)

    assert f"pbi_table:{MODEL_KEY}.dcust" in result.unresolved
    assert any(w.category == "interactive_unavailable" for w in warnings)
    assert cache.mappings == {}  # nothing cached — a later terminal run can still resolve it


def test_is_no_terminal_error_detection():
    assert interactive._is_no_terminal_error(OSError())
    assert interactive._is_no_terminal_error(NoConsoleScreenBufferError())
    assert not interactive._is_no_terminal_error(ValueError())
    assert not interactive._is_no_terminal_error(KeyboardInterrupt())


def test_exact_match_resolves_bronze_table(tmp_path: Path):
    # assign_layers retypes tables in user-declared bronze schemas to
    # BRONZE_TABLE before link_graph runs; a PBI table loading such a raw
    # table must still resolve via the exact rung (regression: _SQL_TYPES
    # omitted BRONZE_TABLE, so the item stayed permanently unresolved).
    graph = LineageGraph()
    graph.add_node(make_node(NodeType.BRONZE_TABLE, "erp_orders", "sales_orders"))
    pbi_table(graph, "sales orders", "erp_orders", "sales_orders")
    result, _ = link_graph(graph, make_config(), cache_at(tmp_path), interactive_mode=False)

    assert result.methods == {"exact": 1}
    assert result.unresolved == []
    assert (
        "bronze_table:erp_orders.sales_orders",
        f"pbi_table:{MODEL_KEY}.sales orders",
        "feeds",
    ) in edge_keys(graph)


def test_exact_match_prefers_view_over_bronze_table(tmp_path: Path):
    # BRONZE_TABLE sits LAST in _SQL_TYPES: when a view and a bronze table
    # share schema.name, the view keeps precedence in the exact rung.
    graph = LineageGraph()
    graph.add_node(make_node(NodeType.BRONZE_TABLE, "erp_orders", "sales_orders"))
    graph.add_node(make_node(NodeType.VIEW, "erp_orders", "sales_orders"))
    pbi_table(graph, "sales orders", "erp_orders", "sales_orders")
    result, _ = link_graph(graph, make_config(), cache_at(tmp_path), interactive_mode=False)

    assert result.methods == {"exact": 1}
    assert (
        "view:erp_orders.sales_orders",
        f"pbi_table:{MODEL_KEY}.sales orders",
        "feeds",
    ) in edge_keys(graph)


def _tie_graph() -> LineageGraph:
    """Two SQL candidates scoring an identical fuzzy ratio (a dead tie) for
    one PBI table: dbo.fact_sales_2023 vs {fact_sales_2022, fact_sales_2024}."""
    graph = LineageGraph()
    graph.add_node(make_node(NodeType.GOLD_TABLE, "dbo", "fact_sales_2022"))
    graph.add_node(make_node(NodeType.GOLD_TABLE, "dbo", "fact_sales_2024"))
    pbi_table(graph, "sales", "dbo", "fact_sales_2023")
    return graph


def test_fuzzy_dead_tie_never_auto_accepted(tmp_path: Path):
    # Two candidates at the same >=0.92 score is positive proof the source
    # can't distinguish them — auto-accepting one is a coin flip broken
    # alphabetically, i.e. guessed lineage (hard rule 4). The item must land
    # in the unresolved/ambiguous band instead.
    graph = _tie_graph()
    result, _ = link_graph(graph, make_config(), cache_at(tmp_path), interactive_mode=False)

    assert "fuzzy" not in result.methods
    assert result.unresolved == [f"pbi_table:{MODEL_KEY}.sales"]
    assert graph.nodes[f"pbi_table:{MODEL_KEY}.sales"].metadata["unresolved"] is True
    assert edge_keys(graph) == set()  # no coin-flip feeds edge


def test_fuzzy_dead_tie_routes_to_ambiguous_candidates(tmp_path: Path):
    # In collect mode the tie surfaces as an ambiguous item carrying BOTH
    # tied candidates, so a human/agent makes the call.
    graph = _tie_graph()
    pending: list = []
    result, _ = link_graph(
        graph, make_config(), cache_at(tmp_path), interactive_mode=True, pending_out=pending
    )

    assert "fuzzy" not in result.methods
    assert len(pending) == 1
    targets = {c["target"] for c in pending[0]["candidates"]}
    assert {"gold_table:dbo.fact_sales_2022", "gold_table:dbo.fact_sales_2024"} <= targets


def test_fuzzy_clear_winner_still_auto_accepted(tmp_path: Path):
    # the tie guard must not break the documented >=0.92 auto-accept rung
    # when the best score strictly beats the runner-up
    graph = LineageGraph()
    graph.add_node(make_node(NodeType.GOLD_TABLE, "dbo", "fact_sales_2023"))
    graph.add_node(make_node(NodeType.GOLD_TABLE, "dbo", "fact_sales_2024"))
    pbi_table(graph, "sales", "dbo", "fact_sales_20231")
    result, warnings = link_graph(graph, make_config(), cache_at(tmp_path), interactive_mode=False)

    assert result.methods == {"fuzzy": 1}
    assert any(w.category == "fuzzy_auto" for w in warnings)
    assert (
        "gold_table:dbo.fact_sales_2023",
        f"pbi_table:{MODEL_KEY}.sales",
        "feeds",
    ) in edge_keys(graph)


def multi_native_graph() -> LineageGraph:
    """A PBI table backed by a two-table Value.NativeQuery join — the only
    producer of composite '#' cache keys."""
    graph = LineageGraph()
    graph.add_node(make_node(NodeType.GOLD_TABLE, "dbo", "orders"))
    graph.add_node(make_node(NodeType.GOLD_TABLE, "dbo", "order_lines"))
    node = Node(
        id=Node.make_id(NodeType.PBI_TABLE, MODEL, "combo"),
        node_type=NodeType.PBI_TABLE,
        name="combo",
        schema_name=MODEL_KEY,
        metadata={
            "partition_source": {"schema": "", "object": "", "raw_kind": "native_query"},
            "native_query_tables": ["dbo.order_lines", "dbo.orders"],
        },
    )
    graph.add_node(node)
    return graph


def test_multi_table_native_query_resolves_each_table(tmp_path: Path):
    # the composite-key branch: one _Item per table, each resolving exactly,
    # producing one feeds edge per source table onto the same pbi_table
    graph = multi_native_graph()
    result, _ = link_graph(graph, make_config(), cache_at(tmp_path), interactive_mode=False)

    assert result.methods == {"exact": 2}
    assert result.unresolved == []
    keys = edge_keys(graph)
    assert ("gold_table:dbo.orders", f"pbi_table:{MODEL_KEY}.combo", "feeds") in keys
    assert ("gold_table:dbo.order_lines", f"pbi_table:{MODEL_KEY}.combo", "feeds") in keys


def test_multi_table_native_query_composite_cache_keys_round_trip(tmp_path: Path):
    # composite '#' keys flow through the cache: an interactive-style answer
    # stored under a composite key is honored on the next run, and a stale
    # composite key is pruned (not crashed on)
    node_id = f"pbi_table:{MODEL_KEY}.combo"
    cache = cache_at(tmp_path)
    cache.put(f"{node_id}#dbo.orders", CacheEntry(target="gold_table:dbo.orders", method="interactive"))
    cache.put(f"{node_id}#dbo.gone", CacheEntry(target="view:dbo.vanished", method="interactive"))
    cache.write()

    graph = multi_native_graph()
    result, warnings = link_graph(graph, make_config(), cache_at(tmp_path), interactive_mode=False)

    assert result.methods == {"cache": 1, "exact": 1}  # orders from cache, order_lines exact
    assert any(w.category == "cache_pruned" for w in warnings)  # stale composite key dropped
    keys = edge_keys(graph)
    assert ("gold_table:dbo.orders", node_id, "feeds") in keys
    assert ("gold_table:dbo.order_lines", node_id, "feeds") in keys


def test_multi_table_native_query_unresolved_uses_composite_key(tmp_path: Path):
    # when one of the joined tables has no SQL node, the unresolved marker
    # carries the composite key so `resolve`/`resolve-apply` can target it
    graph = multi_native_graph()
    del graph.nodes["gold_table:dbo.order_lines"]
    result, _ = link_graph(graph, make_config(), cache_at(tmp_path), interactive_mode=False)

    assert result.methods == {"exact": 1}
    assert result.unresolved == [f"pbi_table:{MODEL_KEY}.combo#dbo.order_lines"]


def test_collect_mode_records_candidates_without_prompting(tmp_path: Path, monkeypatch):
    # pending_out -> record each ambiguous item + its candidates, leave unresolved,
    # and never prompt (the `resolve` command path).
    monkeypatch.setattr(interactive, "questionary", _RaisingQuestionary(AssertionError("must not prompt")))
    graph = build_graph()
    pending: list = []
    result, _ = link_graph(
        graph, make_config(), cache_at(tmp_path), interactive_mode=True, pending_out=pending
    )

    by_key = {p["cache_key"]: p for p in pending}
    dcust = by_key[f"pbi_table:{MODEL_KEY}.dcust"]
    assert dcust["candidates"]  # carries scored SQL targets
    assert all("target" in c and "score" in c for c in dcust["candidates"])
    assert f"pbi_table:{MODEL_KEY}.dcust" in result.unresolved
