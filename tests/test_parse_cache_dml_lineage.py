"""Issue #71: a proc stubbed by an earlier EXEC must keep its DML column
lineage when its file's parse is replayed from the cache (warm == cold)."""

from pathlib import Path

import pytest

from coop_data_doc.config import Config, RepoConfig
from coop_data_doc.crawler import FileKind, crawl
from coop_data_doc.graph import LineageGraph, to_json_str
from coop_data_doc.parsers.parse_cache import ParseCache
from coop_data_doc.parsers.sql_objects import parse_sql_objects
from coop_data_doc.parsers.sql_procs import parse_sql_procs, resolve_stub_references

CALLER = "CREATE PROCEDURE dbo.run_load AS\nEXEC dbo.load_sales;\nGO\n"
LOADER = (
    "CREATE PROCEDURE dbo.load_sales AS\n"
    "INSERT INTO dbo.sales (amount) SELECT s.amount FROM dbo.staging_sales s;\nGO\n"
)
EXPECTED = {"gold_table:dbo.sales": {"amount": ["dbo.staging_sales.amount"]}}


@pytest.mark.parametrize("arrangement", ["caller_first", "definition_first", "no_caller", "same_file"])
def test_warm_cache_preserves_column_lineage(tmp_path: Path, arrangement: str):
    root = tmp_path / "sql"
    root.mkdir()
    if arrangement == "caller_first":
        files = {"a_caller.sql": CALLER, "b_loader.sql": LOADER}
    elif arrangement == "definition_first":
        files = {"a_loader.sql": LOADER, "b_caller.sql": CALLER}
    elif arrangement == "same_file":
        files = {"both.sql": CALLER + LOADER}
    else:
        files = {"loader.sql": LOADER}
    for name, sql in files.items():
        (root / name).write_text(sql, encoding="utf-8")
    inventory, warnings = crawl(Config(repos={"sql": RepoConfig(path=str(root), include=["**/*.sql"])}))
    assert warnings == []
    entries = inventory.by_kind(FileKind.SQL_FILE)
    cache = ParseCache(tmp_path / "parse-cache.json")

    def build(cache):
        graph = LineageGraph()
        warnings = parse_sql_objects(entries, graph, parse_cache=cache)
        warnings += parse_sql_procs(entries, graph, parse_cache=cache)
        resolve_stub_references(graph)
        assert warnings == []
        return graph

    cold = build(cache)
    assert cold.nodes["stored_proc:dbo.load_sales"].metadata["dml_column_lineage"] == EXPECTED
    in_memory_warm = build(cache)
    assert to_json_str(in_memory_warm) == to_json_str(cold)
    cache.write()
    warm = build(ParseCache.load(cache.path))
    assert warm.nodes["stored_proc:dbo.load_sales"].metadata.get("dml_column_lineage") == EXPECTED
    assert to_json_str(warm) == to_json_str(cold)
