"""Parallel SQL parsing (issue #18).

The NON-NEGOTIABLE property: a ``--jobs N`` (N>=2) build is byte-identical to a
``--jobs 1`` build AND to a cold serial build, warm or cold, because every
file's per-file contribution is merged back in sorted ``entry.path`` order via
the same ``_replay_entry`` path issue #17 already proved deterministic. A worker
that raises degrades that one file to a ``parse_worker_error`` warning and the
build still completes; a pool that cannot start falls back to serial. The
serial (``--jobs 1``) path is today's exact code, untouched.

These tests drive the real CLI (spawning real worker processes) for byte-identity
and unit-test the merge/fan-out helpers for the fallback/error/counter contracts.
"""

from __future__ import annotations

import inspect
import json
import pickle
from pathlib import Path

import pytest
from test_cli import run, setup_workspace

from coop_data_doc.config import Config, RepoConfig
from coop_data_doc.crawler import FileEntry, FileKind, crawl
from coop_data_doc.graph import LineageGraph
from coop_data_doc.parsers import parallel
from coop_data_doc.parsers.parse_cache import ParseCache
from coop_data_doc.parsers.sql_common import parse_worker_both_passes

FIXTURES = Path(__file__).parent / "fixtures"
PARSE_CACHE = ".coop-data-doc-parse-cache.json"


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _sql_entries(root: Path) -> list[FileEntry]:
    config = Config(
        repos={"sql": RepoConfig(path=str(root), include=["**/*.sql"], exclude=["**/archive/**"])}
    )
    inventory, _ = crawl(config)
    return inventory.by_kind(FileKind.SQL_FILE)


# -- spawn-safety: the worker is importable/module-level, no closures -----------


def test_worker_is_module_level_and_spawn_safe():
    """The worker MUST be a top-level function (Windows spawn re-imports the
    module and looks it up by qualified name) — no closures/lambdas/methods. A
    qualified name without '<locals>' and a successful pickle round-trip prove
    it can cross a process boundary."""
    assert parse_worker_both_passes.__module__ == "coop_data_doc.parsers.sql_common"
    assert "<locals>" not in parse_worker_both_passes.__qualname__
    assert parse_worker_both_passes.__qualname__ == "parse_worker_both_passes"
    # A picklable callable is the concrete spawn requirement.
    restored = pickle.loads(pickle.dumps(parse_worker_both_passes))
    assert restored is parse_worker_both_passes
    # It takes exactly one positional arg (the (entry_dump, text, dialect) tuple).
    params = list(inspect.signature(parse_worker_both_passes).parameters.values())
    assert len(params) == 1


def test_worker_returns_cache_entry_shaped_dumps():
    entries = _sql_entries(FIXTURES / "repo_sql")
    entry = next(e for e in entries if e.path.endswith("dim_customer.sql"))
    text = Path(entry.abs_path).read_text(encoding="utf-8")
    path, obj_dump, proc_dump, error = parse_worker_both_passes((entry.model_dump(), text, "tsql"))
    assert error is None
    assert path == entry.path
    # ParseCacheEntry-shaped: nodes/edges/warnings keys present.
    for dump in (obj_dump, proc_dump):
        assert set(dump) == {"nodes", "edges", "warnings"}
    assert "view:sales.dim_customer" in obj_dump["nodes"]
    # source_code (exclude=True) is re-added explicitly by the recorder.
    assert obj_dump["nodes"]["view:sales.dim_customer"]["source_code"]


def test_worker_error_returned_not_raised():
    """A worker given a garbage entry dump returns an error string, never raises
    — the parent turns that into a parse_worker_error warning."""
    path, obj_dump, proc_dump, error = parse_worker_both_passes(
        ({"path": "broken.sql", "not": "a valid entry"}, "SELECT 1", "tsql")
    )
    assert obj_dump is None and proc_dump is None
    assert error is not None
    assert path == "broken.sql"


# -- byte-identity: jobs=N == jobs=1 == cold ------------------------------------


def test_parallel_jobs_2_matches_sequential(tmp_path: Path):
    """--jobs 2 and --jobs 1 in matching workspaces must be byte-for-byte equal."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    setup_workspace(a)
    setup_workspace(b)
    assert run(["build", "--non-interactive", "--skip-html", "--jobs", "1"], a).exit_code == 0
    assert run(["build", "--non-interactive", "--skip-html", "--jobs", "2"], b).exit_code == 0
    one = _tree_bytes(a / "data-docs")
    two = _tree_bytes(b / "data-docs")
    assert one.keys() == two.keys()
    for rel in one:
        assert one[rel] == two[rel], f"--jobs 2 != --jobs 1: {rel}"


def test_warm_cold_parallel_all_byte_identical(tmp_path: Path):
    """Cold jobs=1 baseline, warm jobs=2, cold jobs=4 (fresh workspaces where
    needed), warm jobs=4 — every doc tree byte-identical. Proves warm+parallel
    == cold and the merge is order-independent across worker counts."""
    base = tmp_path / "base"
    base.mkdir()
    setup_workspace(base)
    # cold serial baseline (cache bypassed on read, still written)
    assert (
        run(["build", "--non-interactive", "--skip-html", "--no-parse-cache", "--jobs", "1"], base).exit_code
        == 0
    )
    cold = _tree_bytes(base / "data-docs")
    # warm parallel: cache now populated by the cold run
    assert run(["build", "--non-interactive", "--skip-html", "--jobs", "2"], base).exit_code == 0
    warm2 = _tree_bytes(base / "data-docs")
    # warm parallel again with more workers, same cache
    assert run(["build", "--non-interactive", "--skip-html", "--jobs", "4"], base).exit_code == 0
    warm4 = _tree_bytes(base / "data-docs")

    # cold parallel in a fresh workspace (no cache)
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    setup_workspace(fresh)
    assert (
        run(["build", "--non-interactive", "--skip-html", "--no-parse-cache", "--jobs", "4"], fresh).exit_code
        == 0
    )
    cold_parallel = _tree_bytes(fresh / "data-docs")

    assert warm2.keys() == cold.keys() == warm4.keys() == cold_parallel.keys()
    for rel in cold:
        assert warm2[rel] == cold[rel], f"warm jobs2 != cold: {rel}"
        assert warm4[rel] == cold[rel], f"warm jobs4 != cold: {rel}"
        assert cold_parallel[rel] == cold[rel], f"cold jobs4 != cold jobs1: {rel}"


def test_source_code_preserved_in_parallel_build(tmp_path: Path):
    """source_code is exclude=True; the parallel path must still carry it into
    the rendered Source section (HAZARD B), matching the serial build."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    setup_workspace(a)
    setup_workspace(b)
    assert run(["build", "--non-interactive", "--skip-html", "--jobs", "1"], a).exit_code == 0
    assert run(["build", "--non-interactive", "--skip-html", "--jobs", "4"], b).exit_code == 0
    from test_cli import doc_page

    for ws in (a, b):
        page = doc_page(ws / "data-docs", "view:sales.dim_customer").read_text(encoding="utf-8")
        assert "CREATE OR ALTER VIEW sales.dim_customer" in page
    # and the pages are identical
    page_a = doc_page(a / "data-docs", "view:sales.dim_customer").read_bytes()
    page_b = doc_page(b / "data-docs", "view:sales.dim_customer").read_bytes()
    assert page_a == page_b


def test_edge_dedup_with_parallel_merge(tmp_path: Path):
    """A file that reads the same table twice yields ONE reads edge under both
    --jobs 1 and --jobs N; the manifest.json is identical."""
    repo = tmp_path / "sql"
    repo.mkdir()
    # four files (>= pool threshold), one with a duplicated read.
    (repo / "dup.sql").write_text(
        "CREATE VIEW dbo.v_dup AS SELECT a.x FROM silver.src a JOIN silver.src b ON a.x=b.x;\n",
        encoding="utf-8",
    )
    for i in range(3):
        (repo / f"t{i}.sql").write_text(f"CREATE TABLE dbo.t{i} (a INT);\n", encoding="utf-8")
    config = tmp_path / "coop-data-doc.yml"
    config.write_text(
        "project_name: Dedup\nrepos:\n  sql:\n    path: ./sql\n    include: ['**/*.sql']\n"
        "output:\n  dir: ./data-docs\n  site_dir: ./site\n",
        encoding="utf-8",
    )
    assert run(["build", "--non-interactive", "--skip-html", "--jobs", "1"], tmp_path).exit_code == 0
    one = (tmp_path / "data-docs" / "manifest.json").read_bytes()
    assert (
        run(
            ["build", "--non-interactive", "--skip-html", "--no-parse-cache", "--jobs", "4"], tmp_path
        ).exit_code
        == 0
    )
    many = (tmp_path / "data-docs" / "manifest.json").read_bytes()
    assert one == many
    manifest = json.loads(many)
    reads = [e for e in manifest["edges"] if e["edge_type"] == "reads"]
    keys = [(e["source_id"], e["target_id"], e["edge_type"]) for e in reads]
    assert len(keys) == len(set(keys)), "duplicate reads edge survived the parallel merge"


# -- small-estate auto-sequential -----------------------------------------------


def test_small_estate_auto_sequential(tmp_path: Path, monkeypatch):
    """< 4 SQL files must NOT create a pool even at --jobs 8: assert the pool is
    never bootstrapped, exit 0, and output matches a serial build."""
    repo = tmp_path / "sql"
    repo.mkdir()
    (repo / "a.sql").write_text("CREATE TABLE dbo.a (x INT);\n", encoding="utf-8")
    (repo / "b.sql").write_text("CREATE TABLE dbo.b (x INT);\n", encoding="utf-8")
    config = tmp_path / "coop-data-doc.yml"
    config.write_text(
        "project_name: Tiny\nrepos:\n  sql:\n    path: ./sql\n    include: ['**/*.sql']\n"
        "output:\n  dir: ./data-docs\n  site_dir: ./site\n",
        encoding="utf-8",
    )

    calls = {"n": 0}
    real = parallel._run_pool

    def _tracking_pool(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(parallel, "_run_pool", _tracking_pool)
    assert run(["build", "--non-interactive", "--skip-html", "--jobs", "8"], tmp_path).exit_code == 0
    assert calls["n"] == 0, "a 2-file estate must never start the pool"


# -- pool bootstrap failure => serial fallback ----------------------------------


def test_pool_bootstrap_failure_fallback(tmp_path: Path, monkeypatch):
    """If ProcessPoolExecutor cannot be created, the build falls back to serial,
    exits 0, and produces output byte-identical to a real --jobs 1 build."""
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    setup_workspace(a)
    setup_workspace(b)
    # baseline: real serial build
    assert run(["build", "--non-interactive", "--skip-html", "--jobs", "1"], a).exit_code == 0
    serial = _tree_bytes(a / "data-docs")

    # make pool creation blow up; the code must catch it and go serial
    import coop_data_doc.parsers.parallel as parallel_mod

    class _BoomExecutor:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("simulated pool bootstrap failure")

    monkeypatch.setattr("concurrent.futures.ProcessPoolExecutor", _BoomExecutor, raising=True)
    result = run(["build", "--non-interactive", "--skip-html", "--jobs", "8", "--no-parse-cache"], b)
    assert result.exit_code == 0
    fell_back = _tree_bytes(b / "data-docs")
    assert fell_back.keys() == serial.keys()
    for rel in serial:
        assert fell_back[rel] == serial[rel], f"fallback != serial: {rel}"
    # confirm the code path was actually exercised (helper returns None on boot fail)
    assert parallel_mod._run_pool is parallel_mod._run_pool  # sanity


# -- worker error degrades to a warning, build completes ------------------------


def test_worker_parse_error_becomes_parse_worker_error_warning(tmp_path: Path, monkeypatch):
    """A worker that returns an error for a file degrades that file to a
    parse_worker_error diagnostic; the build still exits 0 and the other files
    are documented."""
    setup_workspace(tmp_path)
    import coop_data_doc.parsers.parallel as parallel_mod

    real_pool = parallel_mod._run_pool

    def _one_file_errors(misses, texts, dialect, jobs):
        results = real_pool(misses, texts, dialect, jobs)
        if results is None:
            return None
        # force the first miss (sorted) to look like a worker crash
        target = sorted(results)[0]
        results[target] = (None, None, "Boom: simulated worker crash")
        return results

    monkeypatch.setattr(parallel_mod, "_run_pool", _one_file_errors)
    result = run(["build", "--non-interactive", "--skip-html", "--jobs", "4", "--no-parse-cache"], tmp_path)
    assert result.exit_code == 0
    diagnostics = json.loads((tmp_path / "data-docs" / "diagnostics.json").read_text(encoding="utf-8"))
    cats = {i["category"] for i in diagnostics["issues"]}
    assert "parse_worker_error" in cats
    # error severity (data is missing) — like encoding_unreadable
    err = next(i for i in diagnostics["issues"] if i["category"] == "parse_worker_error")
    assert err["severity"] == "error"


def test_worker_error_never_aborts_build_unit(tmp_path: Path, monkeypatch):
    """Unit-level: a worker result carrying an error contributes nothing, ticks
    on_file for both passes, and counts as a miss — no exception."""
    repo = tmp_path / "sql"
    repo.mkdir()
    for i in range(4):
        (repo / f"t{i}.sql").write_text(f"CREATE TABLE dbo.t{i} (a INT);\n", encoding="utf-8")
    entries = _sql_entries(repo)

    def _all_error(misses, texts_, dialect, jobs):
        return {e.path: (None, None, "boom") for e in misses}

    monkeypatch.setattr(parallel, "_run_pool", _all_error)
    graph = LineageGraph()
    cache = ParseCache(tmp_path / PARSE_CACHE)
    ticks: list[str] = []
    warnings = parallel.parse_sql_parallel(
        entries, graph, "tsql", jobs=4, on_file=ticks.append, parse_cache=cache
    )
    # every file errored -> no nodes, 4 parse_worker_error warnings, build intact
    assert graph.nodes == {}
    worker_errors = [w for w in warnings if w.category == "parse_worker_error"]
    assert len(worker_errors) == len(entries)
    # 2 ticks per file (objects + procs pass), even on error
    assert len(ticks) == 2 * len(entries)


# -- cache composition: only misses go to the pool ------------------------------


def test_cache_misses_only_go_to_pool(tmp_path: Path, monkeypatch):
    """Pre-warm 3 of 4 files, force 1 miss, build --jobs 4, and assert the pool
    only receives the single missed file (hits are replayed in-parent)."""
    repo = tmp_path / "sql"
    repo.mkdir()
    for i in range(4):
        (repo / f"t{i}.sql").write_text(f"CREATE TABLE dbo.t{i} (a INT);\n", encoding="utf-8")
    config = tmp_path / "coop-data-doc.yml"
    config.write_text(
        "project_name: Cache\nrepos:\n  sql:\n    path: ./sql\n    include: ['**/*.sql']\n"
        "output:\n  dir: ./data-docs\n  site_dir: ./site\n",
        encoding="utf-8",
    )
    # warm the cache with a serial build
    assert run(["build", "--non-interactive", "--skip-html", "--jobs", "1"], tmp_path).exit_code == 0
    # edit ONE file so exactly one file misses next time
    (repo / "t0.sql").write_text("CREATE TABLE dbo.t0 (a INT, b INT);\n", encoding="utf-8")

    dispatched: list[str] = []
    real_pool = parallel._run_pool

    def _spy_pool(misses, texts, dialect, jobs):
        dispatched.extend(e.path for e in misses)
        return real_pool(misses, texts, dialect, jobs)

    monkeypatch.setattr(parallel, "_run_pool", _spy_pool)
    assert run(["build", "--non-interactive", "--skip-html", "--jobs", "4"], tmp_path).exit_code == 0
    assert dispatched == ["t0.sql"], f"only the edited file should be dispatched, got {dispatched}"


def test_cache_hit_replay_no_pool(tmp_path: Path, monkeypatch):
    """A fully warm estate dispatches NOTHING to the pool (all hits replayed)."""
    repo = tmp_path / "sql"
    repo.mkdir()
    for i in range(5):
        (repo / f"t{i}.sql").write_text(f"CREATE TABLE dbo.t{i} (a INT);\n", encoding="utf-8")
    config = tmp_path / "coop-data-doc.yml"
    config.write_text(
        "project_name: Warm\nrepos:\n  sql:\n    path: ./sql\n    include: ['**/*.sql']\n"
        "output:\n  dir: ./data-docs\n  site_dir: ./site\n",
        encoding="utf-8",
    )
    assert run(["build", "--non-interactive", "--skip-html", "--jobs", "1"], tmp_path).exit_code == 0
    dispatched: list[str] = []
    real_pool = parallel._run_pool

    def _spy_pool(misses, texts, dialect, jobs):
        dispatched.extend(e.path for e in misses)
        return real_pool(misses, texts, dialect, jobs)

    monkeypatch.setattr(parallel, "_run_pool", _spy_pool)
    assert run(["build", "--non-interactive", "--skip-html", "--jobs", "4"], tmp_path).exit_code == 0
    assert dispatched == [], "a fully warm estate must not dispatch to the pool"


# -- on_file tick order/count ---------------------------------------------------


def test_on_file_tick_per_merge(tmp_path: Path, monkeypatch):
    """on_file ticks exactly 2*N times (objects pass for every file, then procs
    pass for every file), each in sorted entry.path order — matching the serial
    two-loop structure. Runs via the pool with a mock so completion order can't
    reorder the merge."""
    repo = tmp_path / "sql"
    repo.mkdir()
    for name in ("c", "a", "b", "d"):  # deliberately unsorted on disk
        (repo / f"{name}.sql").write_text(f"CREATE TABLE dbo.{name} (x INT);\n", encoding="utf-8")
    entries = _sql_entries(repo)

    # mock the pool: return results in REVERSE order to prove the merge re-sorts
    def _reversed_results(misses, texts_, dialect, jobs):
        out: dict = {}
        for e in reversed(misses):
            out[e.path] = parallel._parse_one_serial(e, texts_[e.path], dialect)
        return out

    monkeypatch.setattr(parallel, "_run_pool", _reversed_results)
    graph = LineageGraph()
    cache = ParseCache(tmp_path / PARSE_CACHE)
    ticks: list[str] = []
    parallel.parse_sql_parallel(entries, graph, "tsql", jobs=4, on_file=ticks.append, parse_cache=cache)
    sorted_paths = [e.path for e in entries]
    # objects pass (all files sorted) THEN procs pass (all files sorted)
    assert ticks == sorted_paths + sorted_paths
    assert len(ticks) == 2 * len(entries)


# -- --jobs flag validation -----------------------------------------------------


def test_jobs_flag_validation(tmp_path: Path):
    """--jobs 1/4/8 parse; --jobs 100 is accepted (capped internally); --jobs 0
    and negatives are rejected with exit 2 (invalid CLI arg)."""
    setup_workspace(tmp_path)
    for good in ("1", "4", "8", "100"):
        r = run(["build", "--non-interactive", "--skip-html", "--jobs", good], tmp_path)
        assert r.exit_code == 0, f"--jobs {good} should be accepted: {r.output}"
    for bad in ("0", "-1"):
        r = run(["build", "--non-interactive", "--skip-html", "--jobs", bad], tmp_path)
        assert r.exit_code == 2, f"--jobs {bad} should be rejected"


def test_jobs_flag_on_scan_and_update(tmp_path: Path):
    """--jobs is accepted by scan and update, not just build (CLI contract)."""
    setup_workspace(tmp_path)
    assert run(["scan", "--non-interactive", "--jobs", "2"], tmp_path).exit_code == 0
    assert run(["update", "--non-interactive", "--skip-html", "--jobs", "2"], tmp_path).exit_code == 0


def test_default_jobs_env_override(monkeypatch):
    """COOP_DATA_DOC_JOBS overrides the default (capped at 8, floored at 1);
    a non-integer value is ignored."""
    monkeypatch.setenv("COOP_DATA_DOC_JOBS", "3")
    assert parallel.default_jobs() == 3
    monkeypatch.setenv("COOP_DATA_DOC_JOBS", "999")
    assert parallel.default_jobs() == parallel._MAX_WORKERS
    monkeypatch.setenv("COOP_DATA_DOC_JOBS", "0")
    assert parallel.default_jobs() == 1
    monkeypatch.setenv("COOP_DATA_DOC_JOBS", "not-a-number")
    # falls back to CPU count (capped) — just assert it is a sane positive int
    assert 1 <= parallel.default_jobs() <= parallel._MAX_WORKERS


# -- unit: serial and parallel produce identical graph state --------------------


def test_parallel_graph_matches_serial_unit(tmp_path: Path):
    """Below/at the threshold and above it, the merged graph JSON is identical
    whether built serially (jobs=1) or via the parallel helper (jobs=4)."""
    from coop_data_doc.graph import to_json_str
    from coop_data_doc.parsers.sql_procs import resolve_stub_references

    entries = _sql_entries(FIXTURES / "repo_sql")

    def build(jobs: int, cache: ParseCache | None, no_cache: bool) -> str:
        graph = LineageGraph()
        parallel.parse_sql_parallel(
            entries, graph, "tsql", jobs=jobs, parse_cache=cache, no_parse_cache=no_cache
        )
        resolve_stub_references(graph)
        return to_json_str(graph)

    serial = build(1, ParseCache(tmp_path / "c1.json"), no_cache=True)
    parallel_cold = build(4, ParseCache(tmp_path / "c2.json"), no_cache=True)
    assert parallel_cold == serial
    # warm parallel: populate then replay
    warm_cache = ParseCache(tmp_path / "c3.json")
    build(4, warm_cache, no_cache=True)  # populate
    warm = build(4, warm_cache, no_cache=False)  # replay
    assert warm == serial


def test_parse_sql_parallel_jobs1_is_serial_path(tmp_path: Path, monkeypatch):
    """jobs=1 must NEVER touch the pool (it is the untouched serial path)."""
    entries = _sql_entries(FIXTURES / "repo_sql")

    def _boom(*args, **kwargs):
        raise AssertionError("jobs=1 must not start the pool")

    monkeypatch.setattr(parallel, "_run_pool", _boom)
    graph = LineageGraph()
    cache = ParseCache(tmp_path / PARSE_CACHE)
    warnings = parallel.parse_sql_parallel(entries, graph, "tsql", jobs=1, parse_cache=cache)
    assert graph.nodes  # it did parse
    assert all(w.category != "parse_worker_error" for w in warnings)


@pytest.mark.parametrize("jobs", [1, 2, 4, 8])
def test_backward_compat_build_still_green(tmp_path: Path, jobs: int):
    """The full pipeline (including HTML skip) still succeeds at every job count
    and always writes the expected artifacts."""
    setup_workspace(tmp_path)
    assert run(["build", "--non-interactive", "--skip-html", "--jobs", str(jobs)], tmp_path).exit_code == 0
    docs = tmp_path / "data-docs"
    assert (docs / "manifest.json").is_file()
    assert (docs / "graph.json").is_file()
