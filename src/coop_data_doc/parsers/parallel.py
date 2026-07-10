"""Deterministic parallel SQL parsing (issue #18).

The two SQL parsers (``parse_sql_objects`` / ``parse_sql_procs``) parse every
file INDEPENDENTLY and only ADD nodes/edges/warnings to the shared graph; all
cross-file resolution (``resolve_stub_references`` and everything downstream)
runs afterwards. That independence is exactly what makes the per-file parse
safe to fan out across processes — as long as the MERGE back into the real
graph happens in the same sorted ``entry.path`` order a serial build uses.

This module owns that fan-out + deterministic merge, composed with issue #17's
per-file parse cache:

- Each file is read + decoded ONCE in the parent (``read_sql_file``), so a
  worker never touches disk and can't see a different encoding than the cache
  key was computed from.
- A cache HIT is replayed in-parent (cheap — no worker) via ``_replay_entry``.
- A cache MISS is dispatched to a ``ProcessPoolExecutor`` running the
  module-level, spawn-safe ``parse_worker_both_passes``; the worker returns the
  SAME ``ParseCacheEntry`` dumps a serial miss would have produced.
- All results — hits and worker results alike — are merged in sorted
  ``entry.path`` order (NOT worker-completion order) through the identical
  ``add_node``/``add_edge`` path, so the merged graph is byte-identical to a
  serial ``--jobs 1`` build. Worker results also populate the cache.
- ``on_file`` ticks once per file per pass (2·N ticks total), from the parent,
  in sorted order, AFTER each file's contribution merges.

Any failure to even start the pool degrades to the serial path (a debug log,
never a crash). A worker that RAISES degrades that ONE file to a
``parse_worker_error`` warning; the build always completes.

The serial path (``--jobs 1``, or a tiny estate where pool startup would cost
more than it saves) is untouched — it is today's exact ``parse_sql_objects`` +
``parse_sql_procs`` loop.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext

from coop_data_doc.config import ParseWarning
from coop_data_doc.crawler import FileEntry
from coop_data_doc.graph.model import LineageGraph
from coop_data_doc.parsers.parse_cache import ParseCache, ParseCacheEntry, cache_key
from coop_data_doc.parsers.sql_common import parse_worker_both_passes
from coop_data_doc.parsers.sql_objects import _replay_entry, parse_sql_objects, read_sql_file
from coop_data_doc.parsers.sql_procs import parse_sql_procs

_log = logging.getLogger("coop_data_doc")

# Below this many SQL files, the ProcessPoolExecutor's spawn/import/pickle
# startup cost outweighs any parse speedup, so we stay serial regardless of
# --jobs. Also the floor at which a pool is worth attempting.
_MIN_FILES_FOR_POOL = 4
# Hard cap on worker processes: more than this rarely helps SQL parsing (the
# merge is serial) and just multiplies spawn cost / memory.
_MAX_WORKERS = 8


def _init_worker_logging(sqlglot_level: int) -> None:
    """ProcessPoolExecutor initializer: silence (or, under --verbose, surface)
    sqlglot in each spawned worker.

    Worker processes start with fresh, unconfigured logging — the parent's
    ``logging.getLogger("sqlglot").setLevel(...)`` (cli.py / wizard.py) does NOT
    propagate to children, so every unsupported-syntax fallback would print a
    raw WARNING to stderr from inside a worker (issue #23). The parent's
    effective sqlglot level is passed in via ``initargs`` so a plain build stays
    quiet (ERROR) and ``--verbose`` / ``--log-file`` (which raise sqlglot to
    DEBUG) still surface the notes. Must stay a module-level function (no
    closure/lambda) so it pickles for the Windows ``spawn`` start method."""
    logging.getLogger("sqlglot").setLevel(sqlglot_level)


def default_jobs() -> int:
    """The default SQL-parse worker count when ``--jobs`` is omitted.

    Normally the CPU count, capped at ``_MAX_WORKERS``. The
    ``COOP_DATA_DOC_JOBS`` environment variable overrides it (still capped, and
    floored at 1) — a way to pin a default (e.g. ``1`` for reproducible,
    pool-free runs in CI or tests) without passing ``--jobs`` every time. An
    explicit ``--jobs N`` on the command line always wins over both. A
    non-integer env value is ignored (falls back to the CPU count) rather than
    crashing the build.
    """
    env = os.environ.get("COOP_DATA_DOC_JOBS")
    if env is not None:
        try:
            return max(1, min(int(env), _MAX_WORKERS))
        except ValueError:
            _log.debug("ignoring non-integer COOP_DATA_DOC_JOBS=%r", env)
    return min(os.cpu_count() or 1, _MAX_WORKERS)


def _replay_pass(
    graph: LineageGraph,
    entry_dump: dict | None,
    warnings: list[ParseWarning],
) -> None:
    """Replay one parser pass's ParseCacheEntry dump into the graph, or nothing
    if the pass produced no entry (a worker error — the caller has already
    emitted the parse_worker_error warning)."""
    if entry_dump is None:
        return
    _replay_entry(graph, ParseCacheEntry.model_validate(entry_dump), warnings)


def parse_sql_parallel(
    entries: list[FileEntry],
    graph: LineageGraph,
    dialect: str,
    *,
    jobs: int,
    on_file: Callable[..., None] | None = None,
    read_cache: dict[str, str] | None = None,
    parse_cache: ParseCache | None = None,
    no_parse_cache: bool = False,
    pool_progress: Callable[[int], AbstractContextManager[Callable[..., None]]] | None = None,
) -> list[ParseWarning]:
    """Run both SQL parser passes over ``entries``, parallelizing cache misses.

    Returns the combined warnings. The resulting ``graph`` state, ``warnings``
    order, ``parse_cache`` contents, and ``on_file`` tick count/order are
    byte-for-byte identical to running ``parse_sql_objects`` then
    ``parse_sql_procs`` serially (``jobs == 1``) — the fan-out only moves the
    per-file parse off the main process; the merge is serial and sorted.

    ``jobs`` is the requested worker count (already validated/capped by the
    caller). ``jobs <= 1``, an empty/tiny estate, or a pool that fails to boot
    all fall back to the serial path.

    ``pool_progress`` (optional, issue #33) is a context-manager factory —
    ``pool_progress(total)`` yields a tick — driven once per POOL result while
    the workers run (the long part of a cold parallel build, during which the
    merge bar would otherwise sit at 0%). It is display-only and never touches
    graph/warnings/cache state; ``on_file`` still ticks exactly 2·N times in
    sorted merge order afterwards. Ticks arrive in ``executor.map`` submission
    (= sorted miss) order.
    """
    # --jobs 1 (or no cache to compose with) => today's exact serial path.
    # Also fall back for a tiny estate: the pool's spawn cost would dominate.
    if jobs <= 1 or parse_cache is None or len(entries) < _MIN_FILES_FOR_POOL:
        return _parse_serial(
            entries,
            graph,
            dialect,
            on_file=on_file,
            read_cache=read_cache,
            parse_cache=parse_cache,
            no_parse_cache=no_parse_cache,
        )

    warnings: list[ParseWarning] = []
    # Pre-read + decode every file ONCE in the parent (shared read_cache), so
    # BOTH parser passes and the worker see identical text and the encoding
    # warning is emitted exactly once (HAZARD: a worker re-reading disk could
    # see a different encoding / a mid-build edit). This mirrors the serial
    # path, which reads via parse_sql_objects (with warnings) then reuses the
    # cache for parse_sql_procs (no warnings).
    texts: dict[str, str] = {}
    for entry in entries:
        texts[entry.path] = read_sql_file(entry, warnings, read_cache)

    # Partition into cache hits (replay in-parent) and misses (dispatch to the
    # pool). Keyed per pass because the two passes have distinct cache keys.
    misses: list[FileEntry] = []
    obj_keys: dict[str, str] = {}
    proc_keys: dict[str, str] = {}
    for entry in entries:
        text = texts[entry.path]
        obj_keys[entry.path] = cache_key(dialect, text, "objects", entry.path)
        proc_keys[entry.path] = cache_key(dialect, text, "procs", entry.path)
        obj_hit = None if no_parse_cache else parse_cache.get(obj_keys[entry.path])
        proc_hit = None if no_parse_cache else parse_cache.get(proc_keys[entry.path])
        # A file is a miss unless BOTH passes are cached (both are stored
        # together on a serial miss, so this is all-or-nothing in practice).
        if obj_hit is None or proc_hit is None:
            misses.append(entry)

    # Dispatch ONLY the misses to the pool; try/except the whole pool lifecycle
    # so any bootstrap failure (no fork/spawn support, restricted sandbox,
    # pickling error) degrades to a serial re-parse of the misses.
    results: dict[str, tuple[dict | None, dict | None, str | None]] = {}
    if misses:
        with pool_progress(len(misses)) if pool_progress is not None else nullcontext() as pool_tick:
            tick = pool_tick if pool_tick is not None else _noop_tick
            pool_results = _run_pool(misses, texts, dialect, jobs, on_result=tick)
            if pool_results is None:
                # Pool never started: re-parse misses serially into throwaway
                # graphs so we still populate the cache + get the entries. Keeps the
                # deterministic sorted merge below unchanged.
                for entry in misses:
                    results[entry.path] = _parse_one_serial(entry, texts[entry.path], dialect)
                    tick(entry.path)
            else:
                results = pool_results

    # DETERMINISTIC MERGE, faithful to the serial path in EVERY observable way:
    #   * graph state — replay each file's contribution in sorted entry.path
    #     order via the same add_node/add_edge (_replay_entry) a warm serial run
    #     uses, so the merged graph is byte-identical;
    #   * warnings order — the serial path emits ALL objects-pass warnings (files
    #     in order) THEN all procs-pass warnings, so we merge the whole objects
    #     pass first, then the whole procs pass;
    #   * on_file ticks — N objects ticks then N procs ticks, matching the serial
    #     two-loop structure exactly (the progress bar total is 2·N);
    #   * cache counters — objects pass owns hits/misses (one count per unique
    #     file), mirroring the serial parsers.
    for entry in entries:
        _merge_pass(
            graph,
            entry,
            warnings,
            results,
            cached_key=obj_keys[entry.path],
            worker_index=0,
            parse_cache=parse_cache,
            count=True,
            on_file=on_file,
        )
    for entry in entries:
        _merge_pass(
            graph,
            entry,
            warnings,
            results,
            cached_key=proc_keys[entry.path],
            worker_index=1,
            parse_cache=parse_cache,
            count=False,
            on_file=on_file,
        )
    return warnings


def _merge_pass(
    graph: LineageGraph,
    entry: FileEntry,
    warnings: list[ParseWarning],
    results: dict[str, tuple[dict | None, dict | None, str | None]],
    *,
    cached_key: str,
    worker_index: int,
    parse_cache: ParseCache,
    count: bool,
    on_file: Callable[..., None] | None,
) -> None:
    """Merge ONE parser pass of ONE file, then tick on_file — the per-pass unit
    the two sorted merge loops call. ``worker_index`` selects the pass in a
    worker result tuple (0=objects, 1=procs); ``count`` is True only on the
    objects pass so the hit/miss counters count each unique file once (matching
    the serial parsers). A worker error degrades the file to a warning on the
    objects pass (emitted once) and contributes nothing on either pass."""
    result = results.get(entry.path)
    if result is not None:
        obj_dump, proc_dump, error = result
        if error is not None:
            if count:
                warnings.append(
                    ParseWarning(
                        file=entry.path,
                        message=f"SQL parse worker failed, object(s) not documented: {error}",
                        category="parse_worker_error",
                    )
                )
                _log.debug("parse worker error for %s: %s", entry.path, error)
                parse_cache.misses += 1
        else:
            pass_dump = (obj_dump, proc_dump)[worker_index]
            _replay_pass(graph, pass_dump, warnings)
            # Populate the cache from the worker's result so the NEXT run is a
            # warm hit — warm+parallel must be byte-identical too.
            parse_cache.put(cached_key, ParseCacheEntry.model_validate(pass_dump))
            if count:
                parse_cache.misses += 1
    else:
        # Cache hit: replay the cached pass in-parent (cheap, no worker).
        _replay_entry(graph, parse_cache.get(cached_key), warnings)
        if count:
            parse_cache.hits += 1
    if on_file:
        on_file(entry.path)


def _noop_tick(*_args: object, **_kwargs: object) -> None:
    """Default pool-progress tick when no ``pool_progress`` factory is given."""


def _run_pool(
    misses: list[FileEntry],
    texts: dict[str, str],
    dialect: str,
    jobs: int,
    on_result: Callable[..., None] = _noop_tick,
) -> dict[str, tuple[dict | None, dict | None, str | None]] | None:
    """Fan the miss files out to a ProcessPoolExecutor, returning
    ``{path: (objects_dump, procs_dump, error)}`` or ``None`` if the pool could
    not even be created (caller then falls back to serial).

    ``on_result`` (display-only) is ticked once per file as each
    ``executor.map`` result arrives — submission (= sorted miss) order.

    ``executor.map`` blocks until every worker finishes (no per-worker timeout —
    a timeout firing mid-parse would abort the whole build; a worker either
    completes or returns an error string it caught itself). A worker is never
    trusted to crash the pool: ``parse_worker_both_passes`` catches its own
    exceptions and returns them as the error slot.
    """
    from concurrent.futures import ProcessPoolExecutor

    workers = min(jobs, _MAX_WORKERS, len(misses))
    payloads = [(entry.model_dump(), texts[entry.path], dialect) for entry in misses]
    # Capture the parent's effective sqlglot level HERE (in the parent process)
    # so the workers inherit it — a fresh worker's logging is unconfigured and
    # would otherwise flood stderr with sqlglot fallback WARNINGs (issue #23).
    sqlglot_level = logging.getLogger("sqlglot").getEffectiveLevel()
    try:
        executor = ProcessPoolExecutor(
            max_workers=workers,
            initializer=_init_worker_logging,
            initargs=(sqlglot_level,),
        )
    except Exception as exc:  # noqa: BLE001 — any bootstrap failure => serial
        _log.debug("parallel SQL parse: pool bootstrap failed, falling back to sequential: %s", exc)
        return None
    try:
        with executor:
            results: dict[str, tuple[dict | None, dict | None, str | None]] = {}
            for path, obj_dump, proc_dump, error in executor.map(parse_worker_both_passes, payloads):
                results[path] = (obj_dump, proc_dump, error)
                on_result(path)
        return results
    except Exception as exc:  # noqa: BLE001 — a pool that dies mid-run => serial
        _log.debug("parallel SQL parse: pool failed during map, falling back to sequential: %s", exc)
        return None


def _parse_one_serial(
    entry: FileEntry,
    text: str,
    dialect: str,
) -> tuple[dict | None, dict | None, str | None]:
    """Serial equivalent of one worker: parse a single file's two passes into a
    throwaway graph and return the ``(objects_dump, procs_dump, error)`` triple
    (the worker's path element is dropped — the caller keys by ``entry.path``).
    Used when the pool couldn't start, so the sorted merge is unchanged. Reuses
    the module-level worker for byte-identical results."""
    _path, obj_dump, proc_dump, error = parse_worker_both_passes((entry.model_dump(), text, dialect))
    return (obj_dump, proc_dump, error)


def _parse_serial(
    entries: list[FileEntry],
    graph: LineageGraph,
    dialect: str,
    *,
    on_file: Callable[..., None] | None,
    read_cache: dict[str, str] | None,
    parse_cache: ParseCache | None,
    no_parse_cache: bool,
) -> list[ParseWarning]:
    """Today's exact serial path: both SQL passes, unchanged. This is the
    ``--jobs 1`` baseline the parallel path must match byte-for-byte."""
    warnings: list[ParseWarning] = []
    warnings += parse_sql_objects(
        entries,
        graph,
        dialect,
        on_file=on_file,
        read_cache=read_cache,
        parse_cache=parse_cache,
        no_parse_cache=no_parse_cache,
    )
    warnings += parse_sql_procs(
        entries,
        graph,
        dialect,
        on_file=on_file,
        read_cache=read_cache,
        parse_cache=parse_cache,
        no_parse_cache=no_parse_cache,
    )
    return warnings
