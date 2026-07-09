"""Incremental SQL parse cache (issue #17).

The NON-NEGOTIABLE property is byte-identity: a warm-cache build must produce
output byte-identical to a cold build. These tests exercise that at three
levels — the whole doc tree (via the CLI), the serialized graph (unit), and the
per-file replay contract — plus the tolerant-load invalidation paths.
"""

import json
from pathlib import Path

import pytest
from test_cli import run, setup_workspace

from coop_data_doc.config import Config, RepoConfig
from coop_data_doc.crawler import FileEntry, FileKind, crawl
from coop_data_doc.graph import LineageGraph, to_json_str
from coop_data_doc.parsers.parse_cache import (
    CACHE_VERSION,
    ParseCache,
    cache_key,
    file_sha256,
)
from coop_data_doc.parsers.sql_objects import parse_sql_objects
from coop_data_doc.parsers.sql_procs import parse_sql_procs, resolve_stub_references

FIXTURES = Path(__file__).parent / "fixtures"
PARSE_CACHE = ".coop-data-doc-parse-cache.json"


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


# -- unit-level: parser wiring + per-file replay ---------------------------------


def _sql_entries(root: Path) -> list[FileEntry]:
    config = Config(
        repos={"sql": RepoConfig(path=str(root), include=["**/*.sql"], exclude=["**/archive/**"])}
    )
    inventory, _ = crawl(config)
    return inventory.by_kind(FileKind.SQL_FILE)


def _build_graph_json(entries: list[FileEntry], parse_cache: ParseCache | None, no_cache: bool) -> str:
    """Run both SQL passes + the cross-file stub pass, return canonical graph JSON."""
    graph = LineageGraph()
    parse_sql_objects(entries, graph, parse_cache=parse_cache, no_parse_cache=no_cache)
    parse_sql_procs(entries, graph, parse_cache=parse_cache, no_parse_cache=no_cache)
    resolve_stub_references(graph)
    return to_json_str(graph)


def test_cold_and_warm_graph_json_byte_identical():
    entries = _sql_entries(FIXTURES / "repo_sql")
    # cold, no cache at all
    cold = _build_graph_json(entries, None, no_cache=False)
    # cold with cache bypassed but still populated
    cache = ParseCache(Path("/tmp/does-not-write.json"))
    forced_cold = _build_graph_json(entries, cache, no_cache=True)
    # warm: same cache object now holds every file's contribution -> replay
    warm = _build_graph_json(entries, cache, no_cache=False)
    assert forced_cold == cold
    assert warm == cold


def test_warm_replay_preserves_source_code():
    """source_code is exclude=True — the cache must round-trip it (HAZARD B)."""
    entries = _sql_entries(FIXTURES / "repo_sql")
    cache = ParseCache(Path("/tmp/unused.json"))
    # populate
    _build_graph_json(entries, cache, no_cache=True)
    # replay into a fresh graph
    graph = LineageGraph()
    parse_sql_objects(entries, graph, parse_cache=cache)
    parse_sql_procs(entries, graph, parse_cache=cache)
    resolve_stub_references(graph)
    view = graph.nodes["view:sales.dim_customer"]
    assert "CREATE OR ALTER VIEW sales.dim_customer" in view.source_code
    proc = graph.nodes["stored_proc:dbo.usp_load_fact_sales"]
    assert "CREATE" in proc.source_code and "PROCEDURE" in proc.source_code.upper()


def test_hit_miss_counters_count_unique_files():
    entries = _sql_entries(FIXTURES / "repo_sql")
    cache = ParseCache(Path("/tmp/unused.json"))
    # first pass: all misses (objects pass owns the counters)
    graph = LineageGraph()
    parse_sql_objects(entries, graph, parse_cache=cache)
    parse_sql_procs(entries, graph, parse_cache=cache)
    assert cache.misses == len(entries)
    assert cache.hits == 0
    # second run against a fresh graph: all hits
    cache.hits = cache.misses = 0
    graph2 = LineageGraph()
    parse_sql_objects(entries, graph2, parse_cache=cache)
    parse_sql_procs(entries, graph2, parse_cache=cache)
    assert cache.hits == len(entries)
    assert cache.misses == 0


def test_multifile_merge_order_determinism(tmp_path: Path):
    """Two files both contribute columns to the SAME table; the merged node must
    be identical whether both are parsed cold, both replayed warm, or the crawl
    order is reversed. Proves the merge-order guarantee (HAZARD C)."""
    repo = tmp_path / "sql"
    (repo / "a").mkdir(parents=True)
    (repo / "b").mkdir()
    # file A defines the table with [col_a, col_b]; file B (a proc) writes it.
    (repo / "a" / "t.sql").write_text("CREATE TABLE dbo.shared (col_a INT, col_b INT);\n", encoding="utf-8")
    (repo / "b" / "p.sql").write_text(
        "CREATE PROCEDURE dbo.usp_fill AS\nINSERT INTO dbo.shared SELECT * FROM silver.src;\n",
        encoding="utf-8",
    )
    entries = _sql_entries(repo)

    cache = ParseCache(tmp_path / PARSE_CACHE)
    cold = _build_graph_json(entries, cache, no_cache=True)  # populate
    warm = _build_graph_json(entries, cache, no_cache=False)  # replay
    reversed_cold = _build_graph_json(list(reversed(entries)), None, no_cache=False)

    assert warm == cold
    # entry order does not change the merged graph (sorted iteration everywhere)
    assert reversed_cold == cold


def test_edited_file_reparses_others_hit(tmp_path: Path):
    """Editing one file changes only its content hash -> that file misses, the
    rest hit, and the result equals a full cold build of the edited estate."""
    repo = tmp_path / "sql"
    repo.mkdir()
    (repo / "keep.sql").write_text("CREATE TABLE dbo.keep (a INT);\n", encoding="utf-8")
    edit = repo / "edit.sql"
    edit.write_text("CREATE TABLE dbo.edit (a INT);\n", encoding="utf-8")

    cache = ParseCache(tmp_path / PARSE_CACHE)
    entries = _sql_entries(repo)
    _build_graph_json(entries, cache, no_cache=True)  # populate both files
    cache.hits = cache.misses = 0

    # edit one file
    edit.write_text("CREATE TABLE dbo.edit (a INT, b INT);\n", encoding="utf-8")
    entries = _sql_entries(repo)
    graph = LineageGraph()
    parse_sql_objects(entries, graph, parse_cache=cache)
    parse_sql_procs(entries, graph, parse_cache=cache)
    resolve_stub_references(graph)

    assert cache.hits == 1  # keep.sql
    assert cache.misses == 1  # edit.sql
    edited = graph.nodes["gold_table:dbo.edit"]
    assert [c.name for c in edited.columns] == ["a", "b"]


# -- cache file tolerance / invalidation ----------------------------------------


def test_missing_cache_file_is_silent(tmp_path: Path):
    cache = ParseCache.load(tmp_path / PARSE_CACHE)
    assert cache.entries == {}
    assert cache.warnings == []  # a missing derivable cache is not a warning


def test_corrupt_cache_degrades_with_warning(tmp_path: Path):
    path = tmp_path / PARSE_CACHE
    path.write_text("{ not valid json", encoding="utf-8")
    cache = ParseCache.load(path)
    assert cache.entries == {}
    assert len(cache.warnings) == 1
    assert cache.warnings[0].category == "parse_cache_invalid"


def test_version_mismatch_ignored_with_warning(tmp_path: Path):
    path = tmp_path / PARSE_CACHE
    path.write_text(json.dumps({"version": CACHE_VERSION + 999, "cache": {}}), encoding="utf-8")
    cache = ParseCache.load(path)
    assert cache.entries == {}
    assert cache.warnings and cache.warnings[0].category == "parse_cache_invalid"


@pytest.mark.parametrize("payload", ["[1, 2, 3]", "42", '"a string"', "true", "null"])
def test_wrong_shape_cache_degrades_not_crashes(tmp_path: Path, payload: str):
    """A structurally-valid JSON file of the WRONG shape (a top-level list/number/
    string/bool/null instead of an object) must degrade to a cold parse with a
    warning — never AttributeError. A non-empty non-dict is truthy, so an
    `(data or {}).get(...)` guard does NOT save you; the shape must be checked."""
    path = tmp_path / PARSE_CACHE
    path.write_text(payload, encoding="utf-8")
    cache = ParseCache.load(path)  # must not raise
    assert cache.entries == {}
    assert cache.warnings and cache.warnings[0].category == "parse_cache_invalid"


def test_invalid_entry_dropped_others_kept(tmp_path: Path):
    path = tmp_path / PARSE_CACHE
    good_key = cache_key("tsql", "CREATE TABLE dbo.x (a INT);\n", "objects", "x.sql")
    path.write_text(
        json.dumps(
            {
                "version": CACHE_VERSION,
                "cache": {
                    good_key: {"nodes": {}, "edges": [], "warnings": []},
                    "bad_key": {"nodes": "not-a-dict", "edges": 42},
                },
            }
        ),
        encoding="utf-8",
    )
    cache = ParseCache.load(path)
    assert good_key in cache.entries
    assert "bad_key" not in cache.entries
    assert any(w.category == "parse_cache_invalid" for w in cache.warnings)


def test_write_roundtrip_stable(tmp_path: Path):
    path = tmp_path / PARSE_CACHE
    entries = _sql_entries(FIXTURES / "repo_sql")
    cache = ParseCache(path)
    graph = LineageGraph()
    parse_sql_objects(entries, graph, parse_cache=cache)
    parse_sql_procs(entries, graph, parse_cache=cache)
    cache.write()
    first = path.read_bytes()
    # reload + rewrite must be byte-stable (sorted keys, LF newline)
    reloaded = ParseCache.load(path)
    reloaded.write()
    assert path.read_bytes() == first
    assert first.endswith(b"\n")
    assert b"\r\n" not in first


def test_key_hashes_content_not_mtime():
    same = "CREATE TABLE dbo.x (a INT);\n"
    assert cache_key("tsql", same, "objects", "x.sql") == cache_key("tsql", same, "objects", "x.sql")
    assert cache_key("tsql", same, "objects", "x.sql") != cache_key("tsql", same, "procs", "x.sql")
    assert cache_key("tsql", same, "objects", "x.sql") != cache_key("mysql", same, "objects", "x.sql")
    # identical content at DIFFERENT paths must not share an entry: contributions
    # embed their file path (source_file/evidence/warning.file), so a shared key
    # would replay the first file's attribution for both
    assert cache_key("tsql", same, "objects", "a/x.sql") != cache_key("tsql", same, "objects", "b/x.sql")
    assert file_sha256("a") != file_sha256("b")


def test_identical_content_files_attribute_warnings_per_path(tmp_path: Path):
    """Two byte-identical files at different paths each get their OWN cache
    entry: cold and warm runs attribute the dynamic-SQL warning to BOTH paths
    (a shared entry used to attribute both files' warnings to the first path,
    making warm builds differ from cold and --jobs N differ from --jobs 1)."""
    repo = tmp_path / "sql"
    (repo / "a").mkdir(parents=True)
    (repo / "b").mkdir()
    body = "CREATE PROCEDURE dbo.usp_dyn AS\nDECLARE @s NVARCHAR(100) = N'SELECT 1';\nEXEC (@s);\n"
    (repo / "a" / "dup.sql").write_text(body, encoding="utf-8")
    (repo / "b" / "dup.sql").write_text(body, encoding="utf-8")
    entries = _sql_entries(repo)

    def run_passes(cache: ParseCache | None, no_cache: bool):
        graph = LineageGraph()
        warnings = parse_sql_objects(entries, graph, parse_cache=cache, no_parse_cache=no_cache)
        warnings += parse_sql_procs(entries, graph, parse_cache=cache, no_parse_cache=no_cache)
        resolve_stub_references(graph)
        return to_json_str(graph), warnings

    cache = ParseCache(tmp_path / PARSE_CACHE)
    cold_json, cold_warnings = run_passes(cache, no_cache=True)  # populates the cache
    warm_json, warm_warnings = run_passes(cache, no_cache=False)  # replays it

    expected_paths = {"a/dup.sql", "b/dup.sql"}
    assert {w.file for w in cold_warnings if w.category == "dynamic_sql"} == expected_paths
    assert {w.file for w in warm_warnings if w.category == "dynamic_sql"} == expected_paths
    assert warm_json == cold_json
    assert [w.model_dump() for w in warm_warnings] == [w.model_dump() for w in cold_warnings]
    # and the two paths really do occupy two distinct entries
    assert cache.hits == len(entries) == 2


# -- end-to-end via the CLI ------------------------------------------------------


def test_cli_warmcold_docs_byte_identical(tmp_path: Path):
    """Cold x2 + warm through the real CLI: every generated file byte-identical."""
    setup_workspace(tmp_path)
    # build 1: cold (--no-parse-cache), capture the doc tree
    assert run(["build", "--non-interactive", "--skip-html", "--no-parse-cache"], tmp_path).exit_code == 0
    cold = _tree_bytes(tmp_path / "data-docs")
    # build 2: warm (cache populated by build 1's write path... but --no-parse-cache
    # still populates the cache, so this run is a hit)
    assert run(["build", "--non-interactive", "--skip-html"], tmp_path).exit_code == 0
    warm = _tree_bytes(tmp_path / "data-docs")
    # build 3: cold again to confirm stability
    assert run(["build", "--non-interactive", "--skip-html", "--no-parse-cache"], tmp_path).exit_code == 0
    cold2 = _tree_bytes(tmp_path / "data-docs")
    assert warm.keys() == cold.keys() == cold2.keys()
    for rel in cold:
        assert warm[rel] == cold[rel], f"warm != cold: {rel}"
        assert cold2[rel] == cold[rel], f"cold != cold: {rel}"


def test_cli_cache_file_written_and_gitignorable(tmp_path: Path):
    setup_workspace(tmp_path)
    assert run(["build", "--non-interactive", "--skip-html"], tmp_path).exit_code == 0
    cache_path = tmp_path / PARSE_CACHE
    assert cache_path.is_file()
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    assert data["version"] == CACHE_VERSION
    assert data["cache"]  # non-empty


def test_cli_no_parse_cache_flag_all_reparse(tmp_path: Path):
    """--no-parse-cache always re-parses, but stays byte-identical."""
    setup_workspace(tmp_path)
    assert run(["build", "--non-interactive", "--skip-html"], tmp_path).exit_code == 0
    warm = _tree_bytes(tmp_path / "data-docs")
    assert run(["build", "--non-interactive", "--skip-html", "--no-parse-cache"], tmp_path).exit_code == 0
    cold = _tree_bytes(tmp_path / "data-docs")
    assert warm == cold


def test_cli_corrupt_cache_still_builds(tmp_path: Path):
    setup_workspace(tmp_path)
    (tmp_path / PARSE_CACHE).write_text("{ corrupt", encoding="utf-8")
    result = run(["build", "--non-interactive", "--skip-html"], tmp_path)
    assert result.exit_code == 0
    diagnostics = json.loads((tmp_path / "data-docs" / "diagnostics.json").read_text(encoding="utf-8"))
    cats = {i["category"] for i in diagnostics["issues"]}
    assert "parse_cache_invalid" in cats
    # the cache file was rewritten valid for the next run
    data = json.loads((tmp_path / PARSE_CACHE).read_text(encoding="utf-8"))
    assert data["version"] == CACHE_VERSION


def test_cli_edited_sql_partial_reparse_matches_cold(tmp_path: Path):
    setup_workspace(tmp_path)
    assert run(["build", "--non-interactive", "--skip-html"], tmp_path).exit_code == 0
    # edit one fixture SQL file (append a harmless comment)
    tbl = tmp_path / "sql-repo" / "tables" / "dbo.fact_sales.sql"
    tbl.write_text(tbl.read_text(encoding="utf-8") + "\n-- edited\n", encoding="utf-8")
    # warm partial re-parse
    assert run(["build", "--non-interactive", "--skip-html"], tmp_path).exit_code == 0
    warm = _tree_bytes(tmp_path / "data-docs")
    # cold rebuild of the edited estate
    assert run(["build", "--non-interactive", "--skip-html", "--no-parse-cache"], tmp_path).exit_code == 0
    cold = _tree_bytes(tmp_path / "data-docs")
    assert warm == cold


def test_cli_no_parse_cache_flag_on_scan_and_check(tmp_path: Path):
    """The flag is accepted by scan/build/update/check (CLI contract)."""
    setup_workspace(tmp_path)
    assert run(["scan", "--non-interactive", "--no-parse-cache"], tmp_path).exit_code == 0
    assert run(["build", "--non-interactive", "--skip-html"], tmp_path).exit_code == 0
    # check exits 0 (up to date) or 1/2 (unresolved) — the point is the flag parses
    check = run(["check", "--lenient", "--no-parse-cache"], tmp_path)
    assert check.exit_code in (0, 1, 2)
    update = run(["update", "--non-interactive", "--skip-html", "--no-parse-cache"], tmp_path)
    assert update.exit_code == 0
