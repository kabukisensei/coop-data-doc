"""Regressions from the 2026-10-23 audit (issues #76, #77, #78) and the test
suite's config-discovery fence.

- #76: two SQL repos holding a file at the same relative path lost one repo's
  objects under ``--jobs N`` (and the pool-failure fallback), silently.
- #77: a DML target retyped to silver/bronze by layering lost its column
  lineage, because ``dml_column_lineage`` keys still named the gold id.
- #78: a column loaded by two procs reported only the first writer's sources.
- The suite must never discover a ``coop-data-doc.yml`` above its temp folder.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from test_cli import run

from coop_data_doc.cli import run_pipeline
from coop_data_doc.config import Config
from coop_data_doc.graph import to_json_str
from coop_data_doc.parsers import parallel


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


# -- #76 ------------------------------------------------------------------------


def _shared_path_estate(root: Path) -> Path:
    _write(root / "a" / "shared.sql", "CREATE TABLE dbo.first (id INT);\nGO\n")
    _write(root / "a" / "a_extra.sql", "CREATE TABLE dbo.first_extra (id INT);\nGO\n")
    _write(root / "b" / "shared.sql", "CREATE TABLE dbo.second (id INT);\nGO\n")
    _write(root / "b" / "b_extra.sql", "CREATE TABLE dbo.second_extra (id INT);\nGO\n")
    config = root / "coop-data-doc.yml"
    _write(
        config,
        "project_name: Multi\n"
        "repos:\n"
        '  a: {path: ./a, include: ["**/*.sql"]}\n'
        '  b: {path: ./b, include: ["**/*.sql"]}\n'
        "output: {dir: ./docs, site_dir: ./site}\n",
    )
    return config


EXPECTED_NODES = {
    "gold_table:dbo.first",
    "gold_table:dbo.first_extra",
    "gold_table:dbo.second",
    "gold_table:dbo.second_extra",
}


def test_two_repos_sharing_relative_paths_keep_every_object_in_parallel(tmp_path: Path):
    config = _shared_path_estate(tmp_path)
    serial, _, sw = run_pipeline(Config.load(config), interactive=False, jobs=1, no_parse_cache=True)
    parallel_graph, _, pw = run_pipeline(Config.load(config), interactive=False, jobs=2, no_parse_cache=True)
    assert set(serial.nodes) == EXPECTED_NODES
    assert set(parallel_graph.nodes) == EXPECTED_NODES
    assert to_json_str(parallel_graph) == to_json_str(serial)
    assert [w.model_dump() for w in pw] == [w.model_dump() for w in sw]
    # the source text and repo attribution of each shared-path file is its own
    first, second = serial.nodes["gold_table:dbo.first"], serial.nodes["gold_table:dbo.second"]
    assert first.source_file == second.source_file == "shared.sql"


def test_shared_relative_paths_survive_the_pool_fallback_and_a_warm_cache(tmp_path: Path, monkeypatch):
    config = _shared_path_estate(tmp_path)
    # pool-startup failure: the serial fallback inside the parallel path
    monkeypatch.setattr(parallel, "_run_pool", lambda *a, **k: None)
    fallback, _, _ = run_pipeline(Config.load(config), interactive=False, jobs=4, no_parse_cache=True)
    assert set(fallback.nodes) == EXPECTED_NODES
    monkeypatch.undo()
    # warm cache (written by the fallback run), then a mixed hit/miss run
    warm, _, _ = run_pipeline(Config.load(config), interactive=False, jobs=4)
    assert set(warm.nodes) == EXPECTED_NODES
    _write(tmp_path / "b" / "shared.sql", "CREATE TABLE dbo.second (id INT, extra INT);\nGO\n")
    mixed, _, _ = run_pipeline(Config.load(config), interactive=False, jobs=4)
    assert set(mixed.nodes) == EXPECTED_NODES
    assert [c.name for c in mixed.nodes["gold_table:dbo.second"].columns] == ["id", "extra"]
    assert [c.name for c in mixed.nodes["gold_table:dbo.first"].columns] == ["id"], (
        "a's file is untouched by b's edit"
    )


def test_shared_relative_paths_build_byte_identical_trees(tmp_path: Path):
    one, two = tmp_path / "one", tmp_path / "two"
    _shared_path_estate(one)
    _shared_path_estate(two)
    assert run(["build", "--non-interactive", "--skip-html", "--jobs", "1"], one).exit_code == 0
    assert run(["build", "--non-interactive", "--skip-html", "--jobs", "2"], two).exit_code == 0
    assert _tree_bytes(one / "docs") == _tree_bytes(two / "docs")
    manifest = json.loads((two / "docs" / "manifest.json").read_text(encoding="utf-8"))
    assert EXPECTED_NODES <= set(manifest["nodes"])


# -- #77 ------------------------------------------------------------------------


def _column_estate(root: Path, *, layers: str = "", files: dict[str, str]) -> None:
    for name, text in files.items():
        _write(root / "sql" / name, text)
    _write(
        root / "coop-data-doc.yml",
        "project_name: Column Origin Fixture\n"
        "repos:\n"
        "  sql:\n"
        "    path: ./sql\n"
        '    include: ["**/*.sql"]\n'
        "output:\n"
        "  dir: ./docs\n"
        "  site_dir: ./site\n" + layers,
    )


def _sources(root: Path, obj: str, column: str) -> list[str]:
    assert run(["scan", "--non-interactive", "--jobs", "1"], root).exit_code == 0
    result = run(["lineage", obj, "--column", column], root)
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["source_columns"]


LOADER = "CREATE PROCEDURE dbo.load_sales AS\nINSERT INTO stage.sales (amount) SELECT s.amount FROM dbo.source_sales s;\nGO\n"


def test_silver_and_bronze_targets_keep_source_column_lineage(tmp_path: Path):
    for layer in ("silver", "bronze"):
        root = tmp_path / layer
        _column_estate(
            root, layers=f"layers:\n  {layer}:\n    schemas: [stage]\n", files={"load_sales.sql": LOADER}
        )
        assert _sources(root, "stage.sales", "amount") == ["dbo.source_sales.amount"]
        graph = json.loads((root / "docs" / "graph.json").read_text(encoding="utf-8"))
        proc = graph["nodes"]["stored_proc:dbo.load_sales"]
        assert list(proc["metadata"]["dml_column_lineage"]) == [f"{layer}_table:stage.sales"], (
            "every DML target key names a current graph node after layering"
        )
    # the gold control is unchanged
    gold = tmp_path / "gold"
    _column_estate(gold, files={"load_sales.sql": LOADER})
    assert _sources(gold, "stage.sales", "amount") == ["dbo.source_sales.amount"]


# -- #78 ------------------------------------------------------------------------


def test_column_lineage_unions_every_writer(tmp_path: Path):
    _column_estate(
        tmp_path,
        files={
            "sales.sql": "CREATE TABLE dbo.sales (amount INT);\nGO\n",
            "load_a.sql": "CREATE PROCEDURE dbo.load_a AS\nINSERT INTO dbo.sales (amount) SELECT a.amount FROM dbo.source_a a;\nGO\n",
            "load_b.sql": "CREATE PROCEDURE dbo.load_b AS\nINSERT INTO dbo.sales (amount) SELECT b.amount FROM dbo.source_b b;\nGO\n",
            # a trigger is a writer too, and it shares a source with load_b
            "trg.sql": "CREATE TRIGGER dbo.trg_sales ON dbo.sales AFTER INSERT AS\n"
            "INSERT INTO dbo.sales (amount) SELECT b.amount FROM dbo.source_b b;\nGO\n",
        },
    )
    assert _sources(tmp_path, "dbo.sales", "amount") == ["dbo.source_a.amount", "dbo.source_b.amount"]
    # the object page lists the union in its Source Columns cell
    assert run(["build", "--non-interactive", "--skip-html", "--jobs", "1"], tmp_path).exit_code == 0
    pages = list((tmp_path / "docs").rglob("*sales*.md"))
    page = next(p for p in pages if p.parent.name == "gold_table").read_text(encoding="utf-8")
    assert "dbo.source_a.amount" in page and "dbo.source_b.amount" in page


# -- discovery fence --------------------------------------------------------------


def test_config_discovery_stops_at_the_fence(tmp_path: Path, monkeypatch):
    above = tmp_path / "above"
    inside = above / "fence" / "work" / "deep"
    inside.mkdir(parents=True)
    _write(above / "coop-data-doc.yml", "project_name: Above\nrepos: {}\n")
    _write(above / "fence" / "coop-data-doc.yml", "project_name: Fenced\nrepos: {}\n")
    # the suite's own fence (conftest) is set for every test and covers tmp_path
    fence = os.environ.get("COOP_DATA_DOC_DISCOVERY_ROOTS", "")
    assert fence and any(str(tmp_path).startswith(root) for root in fence.split(os.pathsep))
    empty = tmp_path / "empty"
    empty.mkdir()
    result = run(["status"], empty)
    assert result.exit_code == 1 and "no config found" in result.output
    monkeypatch.delenv("COOP_DATA_DOC_DISCOVERY_ROOTS", raising=False)
    assert Config.find(inside) == (above / "fence" / "coop-data-doc.yml").resolve()
    monkeypatch.setenv("COOP_DATA_DOC_DISCOVERY_ROOTS", str(above / "fence"))
    assert Config.find(inside) == (above / "fence" / "coop-data-doc.yml").resolve(), (
        "a root itself is examined"
    )
    monkeypatch.setenv("COOP_DATA_DOC_DISCOVERY_ROOTS", str(above / "fence" / "work"))
    assert Config.find(inside) is None, "nothing above the fence is read"
    monkeypatch.setenv(
        "COOP_DATA_DOC_DISCOVERY_ROOTS", os.pathsep.join([str(tmp_path / "elsewhere"), str(above)])
    )
    assert Config.find(inside) == (above / "fence" / "coop-data-doc.yml").resolve()
