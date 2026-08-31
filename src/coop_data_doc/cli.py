"""Command-line interface (Module 6).

Thin wrappers around the pipeline modules — no parsing or rendering logic
lives here. User-facing failures print one friendly line; tracebacks only
with -v.
"""

from __future__ import annotations

import filecmp
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

import click
import questionary

from coop_data_doc import __version__
from coop_data_doc.config import (
    DEFAULT_PBI_INCLUDE,
    DEFAULT_SQL_INCLUDE,
    Config,
    ConfigError,
    ParseWarning,
    render_config_yaml,
)
from coop_data_doc.crawler import FileKind, crawl
from coop_data_doc.diagnostics import Diagnostics, severity_of
from coop_data_doc.folders import (
    base_patterns_from_includes,
    folder_scoped_includes,
    folder_states,
    includes_for_folders,
    split_excludes,
    top_level_folders,
)
from coop_data_doc.graph.model import LineageGraph, normalize_identifier
from coop_data_doc.graph.serialize import to_json_file
from coop_data_doc.layering import assign_layers, prune_schemas
from coop_data_doc.linker.cache import CacheEntry, LineageCache
from coop_data_doc.linker.resolver import ResolutionResult, link_graph
from coop_data_doc.parsers.bim import parse_bim
from coop_data_doc.parsers.parallel import _MAX_WORKERS, default_jobs, parse_sql_parallel
from coop_data_doc.parsers.parse_cache import ParseCache
from coop_data_doc.parsers.pbir import (
    collapse_visuals,
    link_reports_to_models,
    link_visual_bindings,
    parse_legacy_reports,
    parse_pbir,
    parse_pbir_definitions,
)
from coop_data_doc.parsers.pbix import parse_pbix
from coop_data_doc.parsers.sql_objects import flag_silent_sql_files
from coop_data_doc.parsers.sql_procs import resolve_stub_references
from coop_data_doc.parsers.tmdl import link_composite_models, parse_tmdl
from coop_data_doc.progress import Progress, should_enable
from coop_data_doc.render.markdown import render_markdown, write_diagnostics
from coop_data_doc.render.site import SiteBuildError, build_site, write_mkdocs_config
from coop_data_doc.reviews import ReviewEnvelope, join_reviews, load_review_files

# Warning categories that fail `build --strict` and default `check` (tolerated by
# `check --lenient`): risky parses plus the unresolved items the portal itself
# lists — an untraceable partition source and a visual binding no context could
# disambiguate (issue #32: the gate and the site must agree on "clean").
STRICT_CATEGORIES = (
    "regex_fallback",
    "dynamic_sql",
    "unresolved_partition_source",
    "ambiguous_visual_binding",
)
DEFAULT_CONFIG = "coop-data-doc.yml"
_log = logging.getLogger("coop_data_doc")


def build_graph(
    config: Config,
    progress: Progress | None = None,
    no_parse_cache: bool = False,
    jobs: int | None = None,
) -> tuple[LineageGraph, list[ParseWarning]]:
    """Crawl -> parse -> structural links -> prune -> assign layers, returning
    (graph, warnings). This is everything that does NOT depend on
    ``config.schema_mappings``, so the resulting graph can be resolved against
    several mapping sets (see ``resolve_graph``) without re-crawling or
    re-parsing — the expensive part. ``run_pipeline`` chains this into
    ``resolve_graph``; the setup wizard reuses one built graph across its
    suggest + verify passes.

    ``no_parse_cache`` forces a cold SQL parse (bypass the per-file parse cache);
    the cache is still repopulated for the next run. The cache is transparent —
    a warm build is byte-identical to a cold one (see parsers/parse_cache.py).

    ``jobs`` (default ``min(cpu_count, 8)``) parallelizes the per-file SQL parse
    across processes; the deterministic merge (see parsers/parallel.py) keeps a
    ``--jobs N`` build byte-identical to a ``--jobs 1`` build and to a cold
    serial build. Only the SQL parsers fan out; every cross-file pass stays
    serial. ``jobs == 1`` is today's exact sequential path.
    """
    if jobs is None:
        jobs = default_jobs()
    progress = progress or Progress(enabled=False)
    graph = LineageGraph()
    # A spinner (not a silent call) so the crawl — the first, fully blocking
    # stage — visibly shows activity instead of looking frozen on a slow estate.
    with progress.spinner("Crawling repos"):
        inventory, warnings = crawl(config)
    progress.line(f"  {len(inventory.entries)} files found")
    _log.debug("crawled %d files across %d repos", len(inventory.entries), len(config.repos))

    sql_entries = inventory.by_kind(FileKind.SQL_FILE)
    _log.debug("parsing %d SQL files (dialect=%s)", len(sql_entries), config.sql_dialect)
    # Shared across both passes so each SQL file is read + decoded exactly ONCE (the second
    # pass is a cache hit — no disk read, no Windows Defender/OneDrive re-touch tax).
    sql_read_cache: dict[str, str] = {}
    # Per-file parse cache: skip re-parsing an unchanged SQL file (content-hash keyed).
    # Load is tolerant (corrupt/version-mismatch -> cold run + warning); the cache is
    # derivable so it is gitignored. A warm build is byte-identical to a cold one.
    parse_cache = ParseCache.load(config.base_dir / ".coop-data-doc-parse-cache.json")
    with progress.bar("Parsing SQL", total=2 * len(sql_entries)) as tick:
        warnings += parse_sql_parallel(
            sql_entries,
            graph,
            config.sql_dialect,
            jobs=jobs,
            on_file=tick,
            read_cache=sql_read_cache,
            parse_cache=parse_cache,
            no_parse_cache=no_parse_cache,
            # issue #33: a cold parallel build spends its time in the worker
            # pool, during which the merge bar above would sit at 0% — show a
            # dedicated bar ticking as each worker result arrives instead.
            pool_progress=lambda total: progress.bar("Parsing SQL (workers)", total=total),
        )
    parse_cache.write()
    # cache warnings (load: corrupt/version-mismatch; write: could-not-write) surface as
    # diagnostics like any parser warning — collected AFTER write() so both are included.
    warnings += parse_cache.warnings
    if sql_entries:
        progress.line(f"Parsing SQL ({parse_cache.hits} cached, {parse_cache.misses} parsed)")
    _log.debug("parse cache: %d cached, %d parsed", parse_cache.hits, parse_cache.misses)
    # safety net (issue #31): a classified .sql file that contributed nothing —
    # no nodes, no diagnostics — must be flagged, never silently uncovered
    warnings += flag_silent_sql_files(sql_entries, graph, warnings)
    resolve_stub_references(graph)

    tmdl = inventory.by_kind(FileKind.TMDL)
    bim = inventory.by_kind(FileKind.BIM)
    visuals = inventory.by_kind(FileKind.PBIR_VISUAL)
    pbir_reports = inventory.by_kind(FileKind.PBIR_REPORT)
    definitions = inventory.by_kind(FileKind.PBIR_DEFINITION)
    legacy = inventory.by_kind(FileKind.REPORT_JSON_LEGACY)
    pbix = inventory.by_kind(FileKind.PBIX)
    pbi_total = len(tmdl) + len(bim) + len(visuals) + len(definitions) + len(legacy) + len(pbix)
    with progress.bar("Parsing Power BI", total=pbi_total) as tick:
        warnings += parse_tmdl(tmdl, graph, on_file=tick)
        warnings += parse_bim(bim, graph, on_file=tick)
        warnings += parse_pbir(
            visuals, inventory.by_kind(FileKind.PBIR_PAGE), pbir_reports, graph, on_file=tick
        )
        warnings += parse_legacy_reports(legacy, graph, on_file=tick)
        warnings += parse_pbix(pbix, graph, on_file=tick)
        # definition.pbir carries the report's authoritative report->model
        # declaration; parse it AFTER the model parsers (so the model nodes exist
        # to match) and BEFORE link_visual_bindings (so its declared-model scoping
        # is in place).
        warnings += parse_pbir_definitions(definitions, graph, on_file=tick)
    warnings += link_visual_bindings(graph)
    warnings += link_composite_models(graph)

    dropped = prune_schemas(graph, config.ignore_schemas, config.include_schemas)
    if dropped:
        progress.line(f"Dropped {dropped} objects in ignored/system schemas")
        _log.debug("pruned %d nodes in system/ignored schemas", dropped)
    with progress.spinner("Assigning layers"):
        warnings += assign_layers(graph, config)
    return graph, warnings


def resolve_graph(
    graph: LineageGraph,
    config: Config,
    interactive: bool,
    progress: Progress | None = None,
    pending_out: list | None = None,
) -> tuple[ResolutionResult, list[ParseWarning]]:
    """Resolution tail: link Power BI tables to their SQL sources (the only
    stage that depends on ``config.schema_mappings``), then wire reports to
    models and fold visuals into pages. Mutates ``graph`` in place and returns
    (result, warnings). Run this on a *copy* of a built graph when resolving the
    same estate against more than one mapping set.
    """
    progress = progress or Progress(enabled=False)
    cache = LineageCache.load(config.base_dir / ".lineage-cache.json")
    # A spinner (not just a static line) so this stage — the fuzzy cross-repo
    # matcher, previously silent — visibly shows activity on a large estate.
    # Only when non-interactive: an interactive link_graph prompts via
    # questionary, and a spinner thread writing to stderr would corrupt it.
    if interactive:
        progress.line("Linking cross-repo references…")
        result, warnings = link_graph(graph, config, cache, interactive, pending_out=pending_out)
    else:
        with progress.spinner("Linking cross-repo references"):
            result, warnings = link_graph(graph, config, cache, interactive, pending_out=pending_out)
    # reports become downstream of their models, then visuals fold into pages
    link_reports_to_models(graph)
    collapse_visuals(graph)
    return result, warnings


def run_pipeline(
    config: Config,
    interactive: bool,
    progress: Progress | None = None,
    pending_out: list | None = None,
    no_parse_cache: bool = False,
    jobs: int | None = None,
) -> tuple[LineageGraph, ResolutionResult, list[ParseWarning]]:
    """Execute the full crawl -> parse -> link pipeline and return
    (graph, resolution result, warnings). Shared by scan/build/check.

    ``progress`` (optional) drives stderr progress bars; defaults to a
    disabled no-op so callers and tests that don't want output are silent.
    ``no_parse_cache`` forces a cold SQL parse (see build_graph). ``jobs``
    (default ``min(cpu_count, 8)``) sets the SQL-parse worker count; the merge
    is deterministic so ``--jobs N`` == ``--jobs 1`` == cold (see build_graph).
    """
    graph, warnings = build_graph(config, progress, no_parse_cache=no_parse_cache, jobs=jobs)
    result, link_warnings = resolve_graph(graph, config, interactive, progress, pending_out)
    warnings += link_warnings
    _log.debug(
        "done: %d nodes, %d edges, %d cross-repo links, %d unresolved, %d warnings",
        len(graph.nodes),
        len(graph.edges),
        result.resolved,
        len(result.unresolved),
        len(warnings),
    )
    return graph, result, warnings


def _error_failures(warnings: list[ParseWarning]) -> list[str]:
    """Failure lines for error-SEVERITY diagnostics (corrupt/undecodable files, truncated
    procs, parse failures) — these mean whole objects are silently missing from the docs, so
    they fail even `check --lenient` (a corrupt file is never "known and accepted")."""
    return [f"{w.category}: {w.file}" for w in warnings if severity_of(w.category) == "error"]


def _strict_failures(result: ResolutionResult, warnings: list[ParseWarning]) -> list[str]:
    failures = [f"unresolved reference: {key}" for key in result.unresolved]
    # risky/unresolved warnings (STRICT_CATEGORIES) fail strict but are tolerated by
    # --lenient; error-severity diagnostics fail BOTH (missing data is never acceptable).
    failures += [f"{w.category}: {w.file}" for w in warnings if w.category in STRICT_CATEGORIES]
    failures += _error_failures(warnings)
    return failures


def _review_inputs(config: Config, flag_paths: tuple[str, ...]) -> list[tuple[Path, str]] | None:
    """The review files to compose, as ``(resolved_path, display)`` pairs, or
    ``None`` when the feature is off (no config ``reviews:`` list, no flags).

    Config-listed paths resolve against the config file's folder (like every
    other config path) and degrade to a ``reviews_unreadable`` warning when
    missing; a ``--reviews`` flag path that doesn't exist is a usage error
    (exit 2). The flag EXTENDS the config list; a file supplied twice is read
    once (first occurrence wins).
    """
    if not config.reviews and not flag_paths:
        return None
    inputs: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for raw in config.reviews:
        resolved = (config.base_dir / Path(raw).expanduser()).resolve()
        if resolved not in seen:
            seen.add(resolved)
            inputs.append((resolved, raw))
    for raw in flag_paths:
        resolved = Path(raw).expanduser().resolve()
        if not resolved.is_file():
            raise click.UsageError(f"--reviews file not found: {raw}")
        if resolved not in seen:
            seen.add(resolved)
            inputs.append((resolved, raw))
    return inputs


def _load_reviews(
    config: Config, flag_paths: tuple[str, ...]
) -> tuple[list[tuple[Path, str]] | None, list[ReviewEnvelope], list[ParseWarning]]:
    """Resolve + load the review files once: ``(inputs, envelopes, warnings)``.
    ``inputs`` is ``None`` when no review files were supplied (feature off)."""
    inputs = _review_inputs(config, flag_paths)
    if inputs is None:
        return None, [], []
    envelopes, warnings = load_review_files(inputs)
    return inputs, envelopes, warnings


def _scan(
    config: Config,
    non_interactive: bool,
    strict: bool,
    quiet: bool,
    progress: Progress | None = None,
    no_parse_cache: bool = False,
    jobs: int | None = None,
    extra_warnings: list[ParseWarning] | None = None,
) -> tuple[LineageGraph, Diagnostics]:
    # Only prompt when stdin/stdout are a real terminal. Run as a subprocess (e.g.
    # by the coop agent or another program), questionary/prompt_toolkit can't open a
    # console and would otherwise crash the build — so fall back to non-interactive
    # there, build everything that resolves automatically, and tell the user how to
    # finish the ambiguous links (and pick folders) from a terminal.
    interactive = not non_interactive and _stdio_is_interactive()
    if not non_interactive and not interactive and not quiet:
        click.echo(
            "Not a terminal — building everything that resolves automatically. To map "
            "ambiguous cross-repo links or pick which folders to document, run "
            "`coop-data-doc setup` (or `build`) in a terminal.",
            err=True,
        )
    graph, result, warnings = run_pipeline(
        config, interactive=interactive, progress=progress, no_parse_cache=no_parse_cache, jobs=jobs
    )
    if extra_warnings:
        # e.g. review-file load problems (issue #38) — surfaced through the same
        # diagnostics channel as parser warnings, advisory (never a strict failure)
        warnings = warnings + extra_warnings
    out_dir = config.output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    to_json_file(graph, out_dir / "graph.json")

    diagnostics = Diagnostics(warnings=warnings, unresolved=list(result.unresolved))
    (out_dir / "diagnostics.json").write_text(
        json.dumps(diagnostics.to_json(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    if not quiet:
        click.echo("", err=True)
        for line in diagnostics.console_lines():
            click.echo(line, err=True)
        click.echo(
            f"\n{len(graph.nodes)} objects, {len(graph.edges)} lineage edges "
            f"({result.resolved} cross-repo links; {len(result.unresolved)} unresolved)",
            err=True,
        )
    if strict:
        failures = _strict_failures(result, warnings)
        if failures:
            for failure in failures:
                click.echo(f"strict: {failure}", err=True)
            sys.exit(2)
    return graph, diagnostics


def _selected_config_path(config_path: str | None = None) -> Path | None:
    """Select and normalize an explicit or discovered config path."""
    try:
        return Config.resolve_path(config_path) if config_path is not None else Config.find()
    except ConfigError as exc:
        # An invalid path expansion is still an authoritative selection; keep
        # it user-facing and never let Path/expanduser leak a traceback.
        raise click.ClickException(str(exc)) from exc


def _load_config(config_path: str | None = None) -> Config:
    """Load config, with discovery if no explicit path given."""
    selected = _selected_config_path(config_path)
    if selected is None:
        raise ConfigError(
            f"No {DEFAULT_CONFIG} found in this folder or any parent. "
            f"Run `coop-data-doc init` to scaffold one, or `coop-data-doc setup` "
            f"for the interactive wizard."
        )
    try:
        return Config.load(selected)
    except ConfigError as exc:
        # Commands should report one actionable line, including for an
        # authoritative environment path, instead of leaking a traceback.
        raise click.ClickException(str(exc)) from exc


def _stdio_is_interactive() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


@click.group(invoke_without_command=True)
@click.version_option(version=__version__, prog_name="coop-data-doc")
@click.option("-v", "--verbose", is_flag=True, help="Debug logging and full tracebacks.")
@click.option("-q", "--quiet", is_flag=True, help="Suppress warning summaries.")
@click.option("--log-file", type=click.Path(), default=None, help="Write a verbose debug log to this file.")
@click.pass_context
def cli(ctx: click.Context, verbose: bool, quiet: bool, log_file: str | None) -> None:
    """Offline data-lineage documentation for SQL + Power BI estates.

    Run with no arguments in a terminal to get an interactive menu.
    """
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    ctx.obj["quiet"] = quiet
    logging.basicConfig(level=logging.DEBUG if verbose else logging.WARNING)
    if not verbose:
        # sqlglot logs every unsupported-syntax fallback; already surfaced
        # (deduplicated) via the diagnostics summary
        logging.getLogger("sqlglot").setLevel(logging.ERROR)
    if log_file:
        try:
            handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
        except OSError as exc:
            raise click.ClickException(f"could not open log file {log_file}: {exc}") from exc
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root = logging.getLogger()
        # keep the console at WARNING so the file (not the terminal) gets the
        # DEBUG flood; raise only the file-bound loggers to DEBUG
        for existing in root.handlers:
            if not isinstance(existing, logging.FileHandler) and existing.level < logging.WARNING:
                existing.setLevel(logging.WARNING)
                existing.addFilter(lambda r: not r.name.startswith("sqlglot"))
        root.setLevel(logging.DEBUG)
        root.addHandler(handler)
        logging.getLogger("coop_data_doc").setLevel(logging.DEBUG)
        logging.getLogger("sqlglot").setLevel(logging.DEBUG)
    if ctx.invoked_subcommand is None:
        if _stdio_is_interactive():
            _interactive_home(ctx)
        else:
            click.echo(ctx.get_help())


def _interactive_home(ctx: click.Context) -> None:
    """The menu shown when `coop-data-doc` is run bare in a terminal."""
    click.echo(f"coop-data-doc {__version__} — offline lineage docs for SQL + Power BI\n")
    # Preserve an authoritative environment candidate even when it does not
    # exist; never let the menu advertise or use a discovered fallback.
    config_path = _selected_config_path()
    config_exists = config_path is not None and config_path.is_file()
    if config_exists:
        message = f"Found {config_path.name} at {config_path.parent}. What would you like to do?"
        choices = [
            questionary.Choice("Update the docs (scan repos + rebuild everything)", "update"),
            questionary.Choice("Show project status (config, docs, freshness)", "status"),
            questionary.Choice("Scan only (refresh graph.json, no rendering)", "scan"),
            questionary.Choice("Map ambiguous links now (interactive build)", "map"),
            questionary.Choice("Change settings (re-run the setup wizard)", "setup"),
            questionary.Choice("Check docs freshness (the CI gate)", "check"),
            questionary.Choice("Check for updates & show the upgrade command", "upgrade"),
            questionary.Choice("Exit", "exit"),
        ]
    else:
        message = (
            f"Configured path is not a config file: {config_path}. What would you like to do?"
            if config_path is not None
            else "No coop-data-doc.yml found in this folder or any parent. What would you like to do?"
        )
        choices = [
            questionary.Choice("Set up interactively (recommended)", "setup"),
            questionary.Choice("Write a starter config to edit by hand", "init"),
            questionary.Choice("Exit", "exit"),
        ]
    try:
        # unsafe_ask: let Ctrl-C propagate so it exits 130, distinct from "Exit"
        action = questionary.select(message, choices=choices).unsafe_ask()
    except OSError:
        click.echo(ctx.get_help())
        return
    if action in (None, "exit"):
        return
    # Actions that read an existing config must use the DISCOVERED path (which may be in a
    # parent dir), not the bare default filename — otherwise running the menu from a
    # subdirectory advertises the parent config but fails to load it. The "Change settings"
    # setup action is only offered because a config WAS found, so it must edit THAT config,
    # not write a new nested one in cwd that would shadow it. `init` (offered only in the
    # no-config branch) intentionally targets cwd.
    discovered = str(config_path) if config_path is not None else DEFAULT_CONFIG
    if action == "setup":
        ctx.invoke(setup, path=discovered)
    elif action == "init":
        # With an authoritative environment candidate, even the scaffold
        # action must target that candidate rather than creating a local
        # fallback config. In the ordinary no-config case `discovered` is the
        # default filename in cwd.
        ctx.invoke(init, path=discovered, force=False)
    elif action == "scan":
        ctx.invoke(scan, config_path=discovered, non_interactive=False, strict=False)
    elif action == "status":
        ctx.invoke(status, config_path=discovered)
    elif action == "check":
        ctx.invoke(check, config_path=discovered)
    elif action == "upgrade":
        ctx.invoke(upgrade)
    elif action in ("update", "map"):
        # "map" is the same interactive build as "update" — surfaced separately
        # because it is the one action that NEEDS a human at a terminal (the
        # ambiguous-link prompts); both run against the discovered config.
        _run_build(
            ctx,
            config_path=discovered,
            non_interactive=False,
            strict=False,
            skip_html=False,
            serve=False,
        )


@cli.command(name="help")
@click.argument("command_name", required=False)
@click.pass_context
def help_cmd(ctx: click.Context, command_name: str | None) -> None:
    """Show help for coop-data-doc or a specific command."""
    if command_name is None:
        click.echo(ctx.parent.get_help())
        return
    command = cli.get_command(ctx, command_name)
    if command is None:
        # UsageError -> exit 2, same as `coop-data-doc <unknown>` itself
        raise click.UsageError(
            f"unknown command '{command_name}' — try `coop-data-doc help`",
            ctx=ctx.parent,
        )
    sub_ctx = click.Context(command, info_name=command_name, parent=ctx.parent)
    click.echo(command.get_help(sub_ctx))


@cli.command()
@click.option(
    "--config", "config_path", default=None, help="Config file path (default: discover in cwd and parents)."
)
@click.pass_context
def status(ctx: click.Context, config_path: str | None) -> None:
    """Show project status: config found? docs built? stale?"""
    # offline by design — to check PyPI for a newer release, run `upgrade`
    click.echo(f"version:   {__version__} (run `coop-data-doc upgrade` to check for updates)")
    # honor an explicit --config; otherwise discover in cwd and parents. An
    # environment-selected missing/directory path is still authoritative, so
    # report it rather than treating it as a normal discovery miss.
    found = _selected_config_path(config_path)
    if found is None:
        click.echo("status: no config found")
        click.echo("  Run `coop-data-doc init` to scaffold a starter config.")
        click.echo("  Or `coop-data-doc setup` for the interactive wizard.")
        sys.exit(1)
    if not found.exists():
        click.echo(f"status: config not found at {found}")
        sys.exit(1)

    click.echo(f"config:    {found}")
    try:
        config = Config.load(found)
    except ConfigError as exc:
        click.echo(f"status:    config exists but is invalid: {exc}")
        sys.exit(1)

    out_dir = config.output_dir()
    site_dir = config.site_dir()
    click.echo(f"markdown:  {out_dir} {'(exists)' if out_dir.is_dir() else '(missing)'}")
    click.echo(f"html site: {site_dir} {'(exists)' if site_dir.is_dir() else '(missing)'}")

    cache_path = config.base_dir / ".lineage-cache.json"
    click.echo(f"cache:     {cache_path} {'(exists)' if cache_path.is_file() else '(missing)'}")

    # Run the pipeline ONCE (it is the expensive part) and reuse it for both
    # the freshness check and the unresolved summary. Progress goes to stderr
    # only when a human is watching (TTY, not --quiet) — issue #33: on a large
    # estate a silent status looks hung for the whole pipeline run.
    progress = Progress(should_enable(ctx.obj["quiet"]))
    # config-listed review files only (status has no --reviews flag); a portal
    # built with EXTRA --reviews flags will legitimately read as stale here.
    review_inputs, envelopes, review_warnings = _load_reviews(config, ())
    try:
        graph, result, warnings = run_pipeline(config, interactive=False, progress=progress)
    except Exception as exc:  # noqa: BLE001 — status degrades to a message, never crashes
        click.echo(f"status:    could not analyze repos ({exc})")
        return
    warnings += review_warnings

    if out_dir.is_dir():
        try:
            # the spinner wraps only the silent work; the result line prints
            # after it so the spinner's \r rewrites never garble real output
            with progress.spinner("Checking docs freshness"), tempfile.TemporaryDirectory() as tmp:
                fresh = Path(tmp) / "docs"
                # copy the committed tree first so human-authored Business
                # Intent blocks survive, then regenerate the SAME artifacts
                # `check` compares (markdown, graph.json, diagnostics.json/.md)
                # so the two commands can never disagree about staleness.
                shutil.copytree(out_dir, fresh)
                review_join = join_reviews(graph, envelopes) if review_inputs is not None else None
                render_markdown(graph, fresh, config.project_name, reviews=review_join)
                diagnostics = Diagnostics(warnings=warnings, unresolved=list(result.unresolved))
                write_diagnostics(fresh, diagnostics, config.project_name)
                (fresh / "diagnostics.json").write_text(
                    json.dumps(diagnostics.to_json(), indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
                to_json_file(graph, fresh / "graph.json")
                stale = _tree_diff(out_dir, fresh)
            if stale:
                click.echo(f"freshness: stale ({len(stale)} files differ)")
                click.echo("  Run `coop-data-doc build` to update.")
            else:
                click.echo("freshness: up to date")
        except Exception as exc:  # noqa: BLE001 — a failed freshness check degrades to a message
            click.echo(f"freshness: could not check ({exc})")
    else:
        click.echo("freshness: no docs yet — run `coop-data-doc build`")

    # Unresolved summary, from the single pipeline run above.
    if result.unresolved:
        click.echo(f"unresolved: {len(result.unresolved)} items (run interactively to resolve)")
    else:
        click.echo("unresolved: 0")


@cli.command()
@click.argument("path", default=None)
@click.option(
    "--transport",
    type=click.Choice(["terminal", "jsonl"], case_sensitive=False),
    default="terminal",
    help="How to interact with the wizard: terminal (questionary) or jsonl (one prompt per line).",
)
@click.pass_context
def setup(ctx: click.Context, path: str | None, transport: str) -> None:
    """Interactively create or update coop-data-doc.yml.

    Prompts for every value, prefilled from the existing config when present,
    then saves and re-validates. Ctrl-C before the end writes nothing.
    """
    from coop_data_doc.wizard import run_setup
    from coop_data_doc.wizard_io import JsonlWizardIO, QuestionaryWizardIO, WizardProtocolError

    # An omitted setup path follows the same discovery policy as every other
    # command. In particular, an environment-selected missing path is the
    # authoring target rather than a local fallback.
    if path is None:
        found = _selected_config_path()
        # Keep the historical relative name for a config in cwd (the JSONL
        # protocol and displayed commands use it), while retaining discovered
        # parent paths and authoritative environment paths exactly.
        cwd_config = (Path.cwd() / DEFAULT_CONFIG).resolve()
        path = DEFAULT_CONFIG if found == cwd_config else str(found or Path(DEFAULT_CONFIG))
    elif path != DEFAULT_CONFIG:
        path = str(_selected_config_path(path))

    transport = transport.lower()
    if transport == "jsonl":
        io = JsonlWizardIO(sys.stdin, sys.stdout)
    else:
        io = QuestionaryWizardIO()

    try:
        config = run_setup(Path(path), io=io)
    except KeyboardInterrupt:
        if transport == "jsonl":
            io.cancelled()
        else:
            click.echo("\nSetup cancelled — nothing was written.", err=True)
        sys.exit(130)
    except WizardProtocolError as exc:
        if transport == "jsonl":
            io.error(str(exc))
        sys.exit(2)
    except ConfigError as exc:
        if transport == "jsonl":
            io.error(str(exc))
            sys.exit(2)
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:
        from coop_data_doc.linker.interactive import _is_no_terminal_error

        if transport == "jsonl":
            io.error(str(exc))
            sys.exit(2)
        if not _is_no_terminal_error(exc):
            raise
        click.echo(
            "setup needs an interactive terminal (no console available here). Run "
            "`coop-data-doc setup` directly in a terminal, or scaffold a config to edit "
            "by hand with `coop-data-doc init`.",
            err=True,
        )
        sys.exit(1)
    build_cmd = "coop-data-doc build" if path == DEFAULT_CONFIG else f"coop-data-doc build --config {path}"
    if config is None:
        if transport == "jsonl":
            io.notice(f"Saved {path}. Fix the noted problem, then run `{build_cmd}`.")
            io.complete("Setup saved with validation notices.", {"config": path})
        else:
            click.echo(f"Saved {path}. Fix the noted problem, then run `{build_cmd}`.")
        return
    saved = (
        f"Saved {path} — project '{config.project_name}', "
        f"{len(config.repos)} repos, {len(config.schema_mappings)} schema mapping(s)."
    )
    if transport == "jsonl":
        # JSONL stdout must stay line-delimited JSON — the summary goes out as a
        # notice event, never raw text, or the bridge's parser would choke.
        io.notice(saved)
        io.complete("Setup complete.", {"config": path})
        return
    click.echo(saved)
    # offer to build right away; either way show the command to run it later
    try:
        build_now = questionary.confirm("Build the docs now?", default=True, auto_enter=False).ask()
    except (KeyboardInterrupt, OSError):
        build_now = None
    if build_now:
        _run_build(ctx, path, non_interactive=False, strict=False, skip_html=False, serve=False)
        _first_run_tour(config, path)
    else:
        click.echo(f"Build them whenever you're ready with:  {build_cmd}")


def _first_run_tour(config: Config, path: str) -> None:
    """The closing tour after a setup-invoked build (issue #37): the four facts
    that decide whether a team adopts the tool, surfaced at the moment of
    success. Setup flow only — plain `build` runs (and CI logs) never grow a
    tour. Plain echo, no prompt, TTY-agnostic."""
    suffix = "" if path == DEFAULT_CONFIG else f" --config {path}"
    index = config.site_dir() / "index.html"
    portal = (
        index.resolve().as_uri()
        if index.is_file()
        else "(HTML site not built — run `coop-data-doc build` without --skip-html)"
    )
    to_commit = [config.output.dir]
    if (config.base_dir / ".lineage-cache.json").is_file():
        # the mapping-answer cache makes future runs fully automatic — but only
        # mention it when this run actually produced one
        to_commit.insert(0, ".lineage-cache.json")
    click.echo("\n✓ Docs built.")
    click.echo(f"\n  Open the portal:      {portal}")
    click.echo(f"  Commit these files:   {'  '.join(to_commit)}")
    click.echo(f"  Gate CI with:         coop-data-doc check{suffix}")
    click.echo(f"  Rebuild any time:     coop-data-doc build{suffix}")


@cli.command()
@click.argument("path", required=False, default=None)
@click.option("--force", is_flag=True, help="Overwrite an existing config.")
def init(path: str | None, force: bool) -> None:
    """Write a starter coop-data-doc.yml to edit by hand (see also: setup)."""
    if path is None:
        # An environment-selected target is authoritative even for the
        # authoring command that has historically defaulted to cwd.
        target = _selected_config_path() if os.environ.get("COOP_DATA_DOC_CONFIG") else Path(DEFAULT_CONFIG)
        if target is None:  # defensive: the env branch always returns a path
            target = Path(DEFAULT_CONFIG)
    else:
        target = _selected_config_path(path)
        if target is None:  # defensive: an explicit path always resolves
            raise click.ClickException(f"could not resolve config path {path}")
    if target.exists() and not force:
        # Check if it's a valid config — if so, suggest setup instead
        try:
            Config.load(target)
            click.echo(f"{target} already exists and is valid.")
            click.echo("  Run `coop-data-doc setup` to edit it interactively,")
            click.echo("  or `coop-data-doc init --force` to overwrite.")
            sys.exit(1)
        except ConfigError:
            pass
        raise click.ClickException(f"{target} already exists (use --force to overwrite)")
    try:
        if target.exists():
            target.unlink()
        if target.parent != Path(""):
            target.parent.mkdir(parents=True, exist_ok=True)
        Config.scaffold(target)
    except OSError as exc:
        raise click.ClickException(f"could not write {target}: {exc}") from exc
    click.echo(f"Wrote {target}.")
    click.echo("Next: edit the two repo paths, then run `coop-data-doc build`.")


def _render_kwargs_from_config(config: Config) -> dict:
    """The ``render_config_yaml(**kwargs)`` that reproduces ``config`` — so a single
    field can be changed and the file re-rendered the same way the wizard saves it
    (deterministic, comments intact). Modeled fields only; unknown YAML keys aren't
    preserved (same as re-running setup)."""
    sql = config.repos.get("sql")
    pbi = config.repos.get("powerbi")
    branding: dict[str, str] = {}
    brand = config.branding
    if brand:
        for key, val in (
            ("logo", brand.logo),
            ("favicon", brand.favicon),
            ("primary_color", brand.primary_color),
            ("accent_color", brand.accent_color),
        ):
            if val:
                branding[key] = val
    return {
        "project_name": config.project_name,
        "sql_path": sql.path if sql else "../sql-repo",
        "pbi_path": pbi.path if pbi else "../pbi-repo",
        "mappings": [(m.schema_name, m.model) for m in config.schema_mappings],
        "layers": {k: {"schemas": list(v.schemas), "paths": list(v.paths)} for k, v in config.layers.items()},
        "ignore_schemas": list(config.ignore_schemas),
        "include_schemas": list(config.include_schemas),
        "branding": branding,
        "sql_include": list(sql.include) if sql else None,
        "sql_exclude": list(sql.exclude) if sql else None,
        "pbi_include": list(pbi.include) if pbi else None,
        "pbi_exclude": list(pbi.exclude) if pbi else None,
        "output_dir": config.output.dir,
        "site_dir": config.output.site_dir,
        "sql_dialect": config.sql_dialect,
        "reviews": list(config.reviews),
    }


@cli.command()
@click.option("--config", "config_path", default=None, help="Config file path (default: discover).")
def folders(config_path: str | None) -> None:
    """List each repo's top-level folders and whether they're documented (JSON).

    For agents / scripts: drive folder selection without the interactive checkbox,
    then apply a choice with `set-folders`. Output is sorted/deterministic.
    """
    config = _load_config(config_path)
    repos = []
    for key in sorted(config.repos):
        repo = config.repos[key]
        repo_abs = (config.base_dir / Path(repo.path).expanduser()).resolve()
        states, custom_excludes, custom_includes = folder_states(
            repo_abs, list(repo.exclude), list(repo.include)
        )
        repos.append(
            {
                "repo": key,
                "path": repo.path,
                "exists": repo_abs.is_dir(),
                "mode": "allowlist" if folder_scoped_includes(list(repo.include)) else "legacy",
                "include": list(repo.include),
                "folders": states,
                "custom_excludes": custom_excludes,
                "custom_includes": custom_includes,
            }
        )
    click.echo(json.dumps({"repos": repos}, indent=2, sort_keys=True))


@cli.command("set-folders")
@click.option("--config", "config_path", default=None, help="Config file path (default: discover).")
@click.option("--repo", required=True, help="Repo key to update: 'sql' or 'powerbi'.")
@click.option(
    "--include",
    "include_csv",
    default="",
    help="Comma-separated top-level folder names to DOCUMENT; every other folder is left out. "
    "Empty (the default) documents nothing for this repo.",
)
def set_folders(config_path: str | None, repo: str, include_csv: str) -> None:
    """Set which top-level folders a repo documents, non-interactively (agent/CI).

    Writes folder-scoped include globs for each selected folder (derived from the
    repo's current file-type patterns), drops any ``**/Name/**`` folder excludes
    the selection supersedes, preserves hand-written exclude patterns, then
    re-validates. The non-interactive twin of the setup wizard's folder checkbox.
    """
    path = _selected_config_path(config_path)
    if path is None:
        raise click.ClickException(
            f"no {DEFAULT_CONFIG} found here or above — run `coop-data-doc init` or `setup` first."
        )
    try:
        config = Config.load(path)
    except ConfigError as exc:
        raise click.ClickException(
            f"config didn't validate ({exc}); fix it (or run `coop-data-doc setup`), then retry."
        ) from exc
    if repo not in config.repos:
        raise click.ClickException(f"no repo '{repo}' in config (have: {', '.join(sorted(config.repos))}).")
    if repo not in ("sql", "powerbi"):
        raise click.ClickException(f"set-folders supports the 'sql' and 'powerbi' repos (got '{repo}').")

    repo_cfg = config.repos[repo]
    repo_abs = (config.base_dir / Path(repo_cfg.path).expanduser()).resolve()
    available = top_level_folders(repo_abs)
    if not available:
        raise click.ClickException(
            f"'{repo}' path {repo_abs} has no top-level folders on disk — nothing to pick. "
            "Clone the repo there, or edit the include globs by hand."
        )
    selected = {s.strip() for s in include_csv.split(",") if s.strip()}
    unknown = sorted(selected - set(available))
    if unknown:
        raise click.ClickException(
            f"not top-level folders of '{repo}': {', '.join(unknown)} (available: {', '.join(available)})."
        )

    base_patterns = base_patterns_from_includes(list(repo_cfg.include)) or (
        DEFAULT_SQL_INCLUDE if repo == "sql" else DEFAULT_PBI_INCLUDE
    )
    new_include = includes_for_folders(sorted(selected), base_patterns)
    # folder excludes the allowlist supersedes are dropped; hand-written
    # (non-folder) excludes are preserved
    _, custom_excludes = split_excludes(available, list(repo_cfg.exclude))
    kwargs = _render_kwargs_from_config(config)
    kwargs["sql_include" if repo == "sql" else "pbi_include"] = new_include
    kwargs["sql_exclude" if repo == "sql" else "pbi_exclude"] = custom_excludes
    rendered = render_config_yaml(**kwargs)
    try:
        path.write_text(rendered, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise click.ClickException(f"could not write {path}: {exc}") from exc
    Config.load(path)  # re-validate what we wrote
    click.echo(
        f"{repo}: documenting {len(selected)} folder(s) "
        f"({', '.join(sorted(selected)) or 'none'}). Wrote {path}."
    )


def _doc_path(node) -> str:
    """The object's generated Markdown page, relative to the docs root."""
    from coop_data_doc.render.paths import slug

    return f"{node.node_type.value}/{slug(node.id)}.md"


def _node_ref(graph: LineageGraph, node_id: str) -> dict:
    node = graph.nodes[node_id]
    return {
        "id": node.id,
        "name": node.qualified_display,
        "type": node.node_type.value,
        "doc": _doc_path(node),
    }


def _match_nodes(graph: LineageGraph, query: str) -> list[str]:
    """Node ids matching ``query`` — exact id, then exact name, then substring. Sorted."""
    q = query.strip().lower()
    if q in graph.nodes:
        return [q]
    exact = sorted(
        nid
        for nid, n in graph.nodes.items()
        if q in {n.name.lower(), n.qualified_display.lower(), f"{n.schema_name}.{n.name}".lower()}
    )
    if exact:
        return exact
    return sorted(
        nid for nid, n in graph.nodes.items() if q in n.name.lower() or q in n.qualified_display.lower()
    )


@cli.command()
@click.argument("object_name")
@click.option("--column", "column_name", default=None, help="Trace a specific column's lineage.")
@click.option("--config", "config_path", default=None, help="Config file path (default: discover).")
@click.option("--depth", default=1, type=int, help="Lineage hops up- and downstream (default 1).")
def lineage(object_name: str, column_name: str | None, config_path: str | None, depth: int) -> None:
    """Print one object's lineage (upstream/downstream + relationships) as JSON.

    Reads the BUILT graph.json so the agent can ground a change in an object's
    immediate lineage without re-parsing the repos. Ambiguous names list the
    candidates instead of guessing.
    """
    config = _load_config(config_path)
    graph_path = config.output_dir() / "graph.json"
    if not graph_path.is_file():
        raise click.ClickException(f"no built graph at {graph_path} — run `coop-data-doc build` first.")
    graph = LineageGraph.model_validate(json.loads(graph_path.read_text(encoding="utf-8")))
    matches = _match_nodes(graph, object_name)
    if not matches:
        raise click.ClickException(f"no object matching '{object_name}' in the docs.")
    if len(matches) > 1:
        click.echo(
            json.dumps(
                {
                    "query": object_name,
                    "ambiguous": True,
                    "matches": [_node_ref(graph, n) for n in matches],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    nid = matches[0]
    node = graph.nodes[nid]

    if column_name:
        col_name_norm = normalize_identifier(column_name)
        visited_cols = set()
        frontier = [(nid, col_name_norm)]
        traced_sources = set()

        while frontier:
            curr_nid, curr_col = frontier.pop(0)
            if (curr_nid, curr_col) in visited_cols:
                continue
            visited_cols.add((curr_nid, curr_col))

            curr_node = graph.nodes.get(curr_nid)
            if not curr_node:
                continue

            col_lineage = curr_node.metadata.get("column_lineage", {})
            sources = col_lineage.get(curr_col)

            if not sources:
                # Check if an upstream stored procedure populates this column
                upstreams = graph.upstream(curr_nid, depth=1)
                for up_id in upstreams:
                    up_node = graph.nodes.get(up_id)
                    if up_node and "dml_column_lineage" in up_node.metadata:
                        dml_lineage = up_node.metadata["dml_column_lineage"]
                        if curr_nid in dml_lineage and curr_col in dml_lineage[curr_nid]:
                            sources = dml_lineage[curr_nid][curr_col]
                            break

            if sources:
                upstreams = graph.upstream(curr_nid, depth=2)
                for source_str in sources:
                    parts = source_str.rsplit(".", 1)
                    if len(parts) != 2:
                        continue
                    src_obj, src_col = parts

                    matched_up = None
                    for up_id in upstreams:
                        up_node = graph.nodes.get(up_id)
                        if not up_node:
                            continue
                        if up_node.qualified_display.lower() == src_obj or up_node.name.lower() == src_obj:
                            matched_up = up_id
                            break

                    if matched_up:
                        frontier.append((matched_up, src_col))
                    else:
                        traced_sources.add(f"{src_obj}.{src_col} (unresolved node)")
            else:
                traced_sources.add(f"{curr_node.qualified_display}.{curr_col}")

        click.echo(
            json.dumps(
                {
                    "object": _node_ref(graph, nid),
                    "column": column_name,
                    "source_columns": sorted(traced_sources),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return

    click.echo(
        json.dumps(
            {
                "object": _node_ref(graph, nid),
                "schema": node.schema_name,
                "layer": node.metadata.get("layer", ""),
                "source_file": node.source_file,
                "upstream": [_node_ref(graph, x) for x in graph.upstream(nid, depth=depth)],
                "downstream": [_node_ref(graph, x) for x in graph.downstream(nid, depth=depth)],
                "relationships": node.metadata.get("relationships", []),
            },
            indent=2,
            sort_keys=True,
        )
    )


_DEFAULT_CONFIG_DICT = {
    "project_name": "Data Estate",
    "repos": {
        "sql": {"path": "../sql-repo", "include": ["**/*.sql"], "exclude": []},
        "powerbi": {"path": "../pbi-repo", "include": ["**/*"], "exclude": []},
    },
    "output": {"dir": "./data-docs", "site_dir": "./data-docs-site"},
    "layers": {},
    "schema_mappings": [],
    "ignore_schemas": [],
    "include_schemas": [],
    "branding": {},
    "sql_dialect": "tsql",
    "reviews": [],
}


def _default_render_kwargs() -> dict:
    """render_config_yaml kwargs for a brand-new config (no file yet)."""
    return {
        "project_name": "Data Estate",
        "sql_path": "../sql-repo",
        "pbi_path": "../pbi-repo",
        "mappings": [],
        "layers": {},
        "ignore_schemas": [],
        "include_schemas": [],
        "branding": {},
        "sql_include": None,
        "sql_exclude": None,
        "pbi_include": None,
        "pbi_exclude": None,
        "output_dir": "./data-docs",
        "site_dir": "./data-docs-site",
        "sql_dialect": "tsql",
        "reviews": [],
    }


def _config_to_dict(config: Config) -> dict:
    """A config as the JSON shape `show-config` prints / `config-set` accepts."""
    branding: dict[str, str] = {}
    brand = config.branding
    if brand:
        for key, val in (
            ("logo", brand.logo),
            ("favicon", brand.favicon),
            ("primary_color", brand.primary_color),
            ("accent_color", brand.accent_color),
        ):
            if val:
                branding[key] = val
    return {
        "project_name": config.project_name,
        "repos": {
            k: {"path": r.path, "include": list(r.include), "exclude": list(r.exclude)}
            for k, r in sorted(config.repos.items())
        },
        "output": {"dir": config.output.dir, "site_dir": config.output.site_dir},
        "layers": {k: {"schemas": list(v.schemas), "paths": list(v.paths)} for k, v in config.layers.items()},
        "schema_mappings": [{"schema": m.schema_name, "model": m.model} for m in config.schema_mappings],
        "ignore_schemas": list(config.ignore_schemas),
        "include_schemas": list(config.include_schemas),
        "branding": branding,
        "sql_dialect": config.sql_dialect,
        "reviews": list(config.reviews),
    }


def _load_config_lenient(path: Path) -> Config:
    """Load a config without the repo-path-existence check (so we can read/patch a
    config whose repos aren't cloned yet — the 'saved but not runnable' state).

    Syntax, decoding, and schema errors remain user-facing ConfigErrors naming
    this path; only missing configured repo directories are tolerated.
    """
    try:
        return Config.load(path)
    except ConfigError as original:
        # Config.load's only intentionally lenient failure is a configured repo
        # directory that has not been cloned yet. Preserve all other errors.
        if not str(original).startswith("Repo '"):
            raise
        import yaml

        try:
            return Config.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001 - normalize all read/validation failures
            raise ConfigError(f"Invalid config in {path}: {exc}") from original


def _apply_config_patch(kwargs: dict, patch: dict) -> None:
    """Override render kwargs with the provided patch keys (partial update)."""
    if "project_name" in patch:
        kwargs["project_name"] = patch["project_name"]
    for repo_key, prefix in (("sql", "sql"), ("powerbi", "pbi")):
        repo_patch = patch.get("repos", {}).get(repo_key)
        if not repo_patch:
            continue
        if "path" in repo_patch:
            kwargs[f"{prefix}_path"] = repo_patch["path"]
        if "include" in repo_patch:
            kwargs[f"{prefix}_include"] = list(repo_patch["include"])
        if "exclude" in repo_patch:
            kwargs[f"{prefix}_exclude"] = list(repo_patch["exclude"])
    output = patch.get("output", {})
    if "dir" in output:
        kwargs["output_dir"] = output["dir"]
    if "site_dir" in output:
        kwargs["site_dir"] = output["site_dir"]
    if "layers" in patch:
        kwargs["layers"] = {
            k: {"schemas": list(v.get("schemas", [])), "paths": list(v.get("paths", []))}
            for k, v in patch["layers"].items()
        }
    if "schema_mappings" in patch:
        kwargs["mappings"] = [(m["schema"], m["model"]) for m in patch["schema_mappings"]]
    if "ignore_schemas" in patch:
        kwargs["ignore_schemas"] = list(patch["ignore_schemas"])
    if "include_schemas" in patch:
        kwargs["include_schemas"] = list(patch["include_schemas"])
    if "branding" in patch:
        kwargs["branding"] = dict(patch["branding"])
    if "sql_dialect" in patch:
        kwargs["sql_dialect"] = patch["sql_dialect"]
    if "reviews" in patch:
        kwargs["reviews"] = [str(p) for p in patch["reviews"]]


@cli.command("show-config")
@click.option("--config", "config_path", default=None, help="Config file path (default: discover).")
def show_config(config_path: str | None) -> None:
    """Print the current config as JSON (defaults if none) — the shape `config-set` takes."""
    path = _selected_config_path(config_path)
    if path and path.exists() and not path.is_file():
        raise click.ClickException(f"Config path is not a file: {path}")
    if path and path.is_file():
        try:
            out = _config_to_dict(_load_config_lenient(path))
        except ConfigError as exc:
            raise click.ClickException(str(exc)) from exc
        out["exists"] = True
        out["path"] = str(path)
    else:
        out = dict(_DEFAULT_CONFIG_DICT)
        out["exists"] = False
        out["path"] = str(path) if path else DEFAULT_CONFIG
    click.echo(json.dumps(out, indent=2, sort_keys=True))


@cli.command("config-set")
@click.option(
    "--config",
    "config_path",
    default=None,
    help="Config file to write (default: discover or ./coop-data-doc.yml).",
)
@click.option(
    "--from-json", "json_src", type=click.File("r"), default="-", help="JSON patch (file, or '-' for stdin)."
)
def config_set(config_path: str | None, json_src) -> None:
    """Apply a JSON patch to coop-data-doc.yml non-interactively (agent/CI).

    The patch shape matches `show-config`; only the keys you pass change, the rest are
    preserved. Re-validated after writing. Lets the agent drive the whole setup
    (repos, output, layers, schema mappings) via collected answers.
    """
    try:
        patch = json.load(json_src)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"invalid JSON: {exc}") from exc
    if not isinstance(patch, dict):
        raise click.ClickException("config-set expects a JSON object.")
    path = _selected_config_path(config_path) or Path(DEFAULT_CONFIG)
    if path.is_dir():
        raise click.ClickException(f"Config path is not a file: {path}")
    try:
        kwargs = (
            _render_kwargs_from_config(_load_config_lenient(path))
            if path.is_file()
            else _default_render_kwargs()
        )
    except ConfigError as exc:
        raise click.ClickException(f"could not read {path}: {exc}") from exc
    try:
        _apply_config_patch(kwargs, patch)
        rendered = render_config_yaml(**kwargs)
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise click.ClickException(f"patch produced an invalid config: {exc}") from exc
    try:
        if path.parent != Path(""):
            path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise click.ClickException(f"could not write {path}: {exc}") from exc
    try:
        Config.load(path)
        status = "validated"
    except ConfigError as exc:
        status = f"saved, not runnable yet ({exc})"
    click.echo(f"Wrote {path} ({status}).")


@cli.command()
@click.option("--config", "config_path", default=None, help="Config file path (default: discover).")
def resolve(config_path: str | None) -> None:
    """List ambiguous cross-repo links + their candidates as JSON (for agent mapping).

    The interactive resolution step, exposed non-interactively: each item carries its
    candidate SQL targets (with fuzzy scores). Present them to the user, then feed the
    decisions to `resolve-apply`. Already-resolved/cached links don't appear.
    """
    config = _load_config(config_path)
    pending: list[dict] = []
    run_pipeline(config, interactive=True, pending_out=pending)
    click.echo(json.dumps({"unresolved": pending}, indent=2, sort_keys=True))


@cli.command("resolve-apply")
@click.option("--config", "config_path", default=None, help="Config file path (default: discover).")
@click.option(
    "--from-json", "json_src", type=click.File("r"), default="-", help="Decisions JSON (file or '-')."
)
def resolve_apply(config_path: str | None, json_src) -> None:
    """Write link decisions to the lineage cache (agent/CI); run `build` separately to use them.

    Input: ``{"decisions": [{"cache_key": "...", "target": "view:sales.dim_customer"},
    {"cache_key": "...", "external": true}, {"cache_key": "...", "skip": true}]}``.
    A decision with neither target/external is treated as skip. The keys + targets
    come from `resolve`.
    """
    try:
        payload = json.load(json_src)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"invalid JSON: {exc}") from exc
    decisions = payload.get("decisions") if isinstance(payload, dict) else payload
    if not isinstance(decisions, list):
        raise click.ClickException('expected {"decisions": [...]} (or a JSON list of decisions).')
    config = _load_config(config_path)
    cache = LineageCache.load(config.base_dir / ".lineage-cache.json")
    applied = 0
    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        key = decision.get("cache_key")
        if not key:
            continue
        if decision.get("target"):
            entry = CacheEntry(target=decision["target"], method="interactive")
        elif decision.get("external"):
            entry = CacheEntry(target=None, method="external")
        else:
            entry = CacheEntry(target=None, method="skip")
        cache.put(key, entry)
        applied += 1
    # resolve-apply's whole job is to persist these human decisions to disk;
    # unlike the interactive loop there is no later write to self-heal a lock, so
    # a swallowed write failure would print "Applied N" while nothing reached the
    # file — silently breaking the agent contract. Fail loud (exit 1) instead.
    if not cache.write():
        message = next(
            (w.message for w in cache.warnings if w.category == "cache_write_failed"),
            "could not write lineage cache",
        )
        raise click.ClickException(f"{message} — no decisions were saved")
    click.echo(f"Applied {applied} decision(s) to {cache.path}. Run `coop-data-doc build` to use them.")


_JOBS_OPTION = click.option(
    "--jobs",
    "jobs",
    type=click.IntRange(min=1),
    default=None,
    help="Parallel SQL-parse workers (default: CPU count, capped at 8). "
    "--jobs 1 is the exact sequential path; the parallel merge is deterministic "
    "so --jobs N is byte-identical to --jobs 1.",
)


def _resolve_jobs(jobs: int | None) -> int:
    """A user-supplied ``--jobs`` value, capped at the worker ceiling; ``None``
    (flag omitted) resolves to the default (CPU count, already capped). Values
    below 1 are rejected by click's IntRange before reaching here."""
    if jobs is None:
        return default_jobs()
    return min(jobs, _MAX_WORKERS)


@cli.command()
@click.option(
    "--config", "config_path", default=None, help="Config file path (default: discover in cwd and parents)."
)
@click.option("--non-interactive", is_flag=True, help="Never prompt (CI mode).")
@click.option("--strict", is_flag=True, help="Exit 2 on unresolved refs / risky parses.")
@click.option(
    "--no-parse-cache",
    is_flag=True,
    help="Force a cold SQL parse (bypass the incremental per-file parse cache).",
)
@_JOBS_OPTION
@click.pass_context
def scan(
    ctx: click.Context,
    config_path: str | None,
    non_interactive: bool,
    strict: bool,
    no_parse_cache: bool,
    jobs: int | None,
) -> None:
    """Crawl, parse, and link both repos; write graph.json."""
    config = _load_config(config_path)
    progress = Progress(should_enable(ctx.obj["quiet"]))
    _scan(
        config,
        non_interactive,
        strict,
        ctx.obj["quiet"],
        progress=progress,
        no_parse_cache=no_parse_cache,
        jobs=_resolve_jobs(jobs),
    )


def _run_build(
    ctx: click.Context,
    config_path: str | None,
    non_interactive: bool,
    strict: bool,
    skip_html: bool,
    serve: bool,
    no_parse_cache: bool = False,
    jobs: int | None = None,
    reviews: tuple[str, ...] = (),
) -> None:
    """Shared implementation behind `build` and `update`."""
    config = _load_config(config_path)
    review_inputs, envelopes, review_warnings = _load_reviews(config, reviews)
    progress = Progress(should_enable(ctx.obj["quiet"]))
    graph, diagnostics = _scan(
        config,
        non_interactive,
        strict,
        ctx.obj["quiet"],
        progress=progress,
        no_parse_cache=no_parse_cache,
        jobs=_resolve_jobs(jobs),
        extra_warnings=review_warnings,
    )
    # renderer-layer join (issue #38): graph.json/manifest.json are untouched
    review_join = join_reviews(graph, envelopes) if review_inputs is not None else None
    out_dir = config.output_dir()
    with progress.bar("Rendering pages", total=len(graph.nodes)) as tick:
        render_markdown(graph, out_dir, config.project_name, reviews=review_join, on_node=tick)
    write_diagnostics(out_dir, diagnostics, config.project_name)
    # The ONE place stale lineage-cache answers are actually deleted: an explicit
    # build that got this far succeeded against the full configured estate, so an
    # entry whose target still isn't in the graph is genuinely dead. Reload from
    # disk (interactive answers were written during linking) and prune for real —
    # read-only commands (check/status/resolve/scan, wizard dry-runs) only ever
    # ignore such entries for the run (see LineageCache.prune_invalid).
    prune_cache = LineageCache.load(config.base_dir / ".lineage-cache.json")
    pruned = prune_cache.prune_invalid(graph, persist=True)
    if pruned:
        _log.debug("pruned %d stale lineage-cache entr%s", len(pruned), "y" if len(pruned) == 1 else "ies")
    # prune_invalid writes only when it dropped entries, and write() now records
    # a cache_write_failed warning (never raises) if the file is locked/read-only
    # — surface it here since diagnostics were already emitted upstream. The docs
    # are fully rendered; only the on-disk prune didn't stick (harmless: those
    # dead entries are re-ignored next run and dropped on the next writable build).
    for warning in prune_cache.warnings:
        if warning.category == "cache_write_failed":
            click.echo(f"warning: {warning.message}", err=True)
    click.echo(f"Markdown docs: {out_dir}", err=True)
    if skip_html:
        return
    mkdocs_config = write_mkdocs_config(
        out_dir,
        config.site_dir(),
        config.project_name,
        graph,
        branding=config.branding,
        config_dir=config.base_dir,
    )
    if serve:
        os.execvp(sys.executable, [sys.executable, "-m", "mkdocs", "serve", "-f", str(mkdocs_config)])
    # one page per node, plus index.md + diagnostics.md
    page_total = len(graph.nodes) + 2
    if progress.enabled:
        with progress.bar(f"Building HTML site ({len(graph.nodes)} pages)", total=page_total) as tick:
            build_site(mkdocs_config, config.site_dir(), on_page=tick)
    else:
        build_site(mkdocs_config, config.site_dir())
    index = config.site_dir() / "index.html"
    # as_uri() renders a valid file URL on every OS — file:///C:/... on Windows
    # (an f"file://{path}" would keep backslashes and drop the third slash).
    click.echo(f"HTML portal:   {index.resolve().as_uri()}", err=True)


_REVIEWS_OPTION = click.option(
    "--reviews",
    "reviews",
    multiple=True,
    type=click.Path(),
    help="A coop-sql-review / coop-dax-review `--format json` report to compose "
    "into the portal (repeatable; extends the config's `reviews:` list). "
    "Advisory: findings never affect exit codes.",
)

_BUILD_OPTIONS = [
    click.option(
        "--config",
        "config_path",
        default=None,
        help="Config file path (default: discover in cwd and parents).",
    ),
    click.option("--non-interactive", is_flag=True, help="Never prompt (CI mode)."),
    click.option("--strict", is_flag=True, help="Exit 2 on unresolved refs / risky parses."),
    click.option("--skip-html", is_flag=True, help="Markdown only; skip the mkdocs site."),
    click.option("--serve", is_flag=True, help="Start `mkdocs serve` after building."),
    click.option(
        "--no-parse-cache",
        is_flag=True,
        help="Force a cold SQL parse (bypass the incremental per-file parse cache).",
    ),
    _JOBS_OPTION,
    _REVIEWS_OPTION,
]


def _with_build_options(func):
    for option in reversed(_BUILD_OPTIONS):
        func = option(func)
    return func


@cli.command()
@_with_build_options
@click.pass_context
def build(
    ctx: click.Context,
    config_path: str | None,
    non_interactive: bool,
    strict: bool,
    skip_html: bool,
    serve: bool,
    no_parse_cache: bool,
    jobs: int | None,
    reviews: tuple[str, ...],
) -> None:
    """Full pipeline: scan + markdown docs + searchable HTML portal."""
    _run_build(ctx, config_path, non_interactive, strict, skip_html, serve, no_parse_cache, jobs, reviews)


@cli.command()
@_with_build_options
@click.pass_context
def update(
    ctx: click.Context,
    config_path: str | None,
    non_interactive: bool,
    strict: bool,
    skip_html: bool,
    serve: bool,
    no_parse_cache: bool,
    jobs: int | None,
    reviews: tuple[str, ...],
) -> None:
    """Alias of build — re-scan the repos and refresh all documentation.

    Prefer `build` in scripts/CI. To upgrade the TOOL itself, run
    `coop-data-doc upgrade` (in the sibling review tools `update` means a
    self-update check, so a notice keeps the verbs unambiguous here).
    """
    if not ctx.obj["quiet"]:
        print(
            "note: 'update' rebuilds the docs (alias of build) — to upgrade the tool "
            "itself, run: coop-data-doc upgrade",
            file=sys.stderr,
        )
    _run_build(ctx, config_path, non_interactive, strict, skip_html, serve, no_parse_cache, jobs, reviews)


@cli.command()
@click.pass_context
def upgrade(ctx: click.Context) -> None:
    """Check for a newer release and print the command to upgrade.

    The ONLY command that uses the network (PyPI metadata / git fetch). It
    does not self-update: replacing the tool while it's running is flaky
    (and impossible on Windows), so instead it prints the exact command for
    how this copy was installed — e.g. `pipx upgrade coop-data-doc` — to run
    from a normal shell.
    """
    from coop_data_doc.upgrade import build_plan, manual_upgrade_command

    progress = Progress(should_enable(ctx.obj["quiet"]))
    with progress.spinner("Checking for updates"):
        plan = build_plan()
    click.echo(f"\ncoop-data-doc {plan.tool_installed} ({plan.install_method}) — {plan.tool_note}")
    if plan.dependencies:
        click.echo("\nDependencies:")
        for dep in plan.dependencies:
            latest = dep.latest or "?"
            label = {
                "current": "up to date",
                "safe": f"update available → {latest}",
                "major": f"MAJOR update available → {latest} (review before upgrading)",
                "unknown": "could not check (offline?)",
            }[dep.kind]
            click.echo(f"  {dep.name:20} {dep.installed:12} {label}")
    click.echo("\nTo upgrade, run this in a regular terminal:\n")
    for line in manual_upgrade_command(plan).splitlines():
        click.echo(f"    {line}")
    click.echo("\nThen confirm with:  coop-data-doc --version")


@cli.command()
@click.option(
    "--config", "config_path", default=None, help="Config file path (default: discover in cwd and parents)."
)
@click.option(
    "--lenient",
    is_flag=True,
    help="Tolerate risky/unresolved warnings (regex_fallback/dynamic_sql/"
    "unresolved_partition_source/ambiguous_visual_binding); still fail on "
    "unresolved references, error-severity diagnostics (corrupt/undecodable files), and "
    "stale docs.",
)
@click.option(
    "--no-parse-cache",
    is_flag=True,
    help="Force a cold SQL parse (bypass the incremental per-file parse cache).",
)
@_REVIEWS_OPTION
@click.pass_context
def check(
    ctx: click.Context,
    config_path: str | None,
    lenient: bool,
    no_parse_cache: bool,
    reviews: tuple[str, ...],
) -> None:
    """CI gate: fail when committed docs are stale, references are unresolved,
    an error-severity diagnostic means data is missing (corrupt/undecodable file,
    truncated proc), or (unless --lenient) risky/unresolved warnings exist
    (regex_fallback / dynamic_sql / unresolved_partition_source /
    ambiguous_visual_binding). Exit 2 for pipeline problems, 1 for stale docs.

    Pass the SAME --reviews arguments the build used (the config's `reviews:`
    list is picked up automatically) — review findings are an explicit build
    input, so the trees legitimately differ otherwise. Findings themselves
    never affect the exit code (the linters are advisory)."""
    config = _load_config(config_path)
    review_inputs, envelopes, review_warnings = _load_reviews(config, reviews)
    graph, result, warnings = run_pipeline(config, interactive=False, no_parse_cache=no_parse_cache)
    warnings += review_warnings
    if lenient:
        # --lenient forgives risky-parse warnings, but a corrupt/undecodable file (error
        # severity) is never "known and accepted" — it still fails.
        failures = [f"unresolved reference: {key}" for key in result.unresolved]
        failures += _error_failures(warnings)
    else:
        failures = _strict_failures(result, warnings)
    if failures:
        for failure in failures:
            click.echo(f"check: {failure}", err=True)
        sys.exit(2)
    committed = config.output_dir()
    if not committed.is_dir():
        raise click.ClickException(f"no committed docs at {committed}; run `coop-data-doc build` first")
    review_join = join_reviews(graph, envelopes) if review_inputs is not None else None
    with tempfile.TemporaryDirectory() as tmp:
        fresh = Path(tmp) / "docs"
        # start from the committed tree so human-authored Business Intent
        # blocks are preserved in the regenerated pages
        shutil.copytree(committed, fresh)
        render_markdown(graph, fresh, config.project_name, reviews=review_join)
        diagnostics = Diagnostics(warnings=warnings, unresolved=list(result.unresolved))
        write_diagnostics(fresh, diagnostics, config.project_name)
        (fresh / "diagnostics.json").write_text(
            json.dumps(diagnostics.to_json(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        to_json_file(graph, fresh / "graph.json")
        stale = _tree_diff(committed, fresh)
        if stale:
            for path in stale:
                click.echo(f"stale: {path}", err=True)
            click.echo("docs are out of date — run `coop-data-doc build`", err=True)
            sys.exit(1)
    click.echo("docs are up to date")


def _tree_diff(committed: Path, fresh: Path) -> list[str]:
    """Generated files that differ between the two doc trees, both ways:
    changed/missing in committed AND committed files the fresh render no
    longer produces (orphaned pages of deleted objects)."""

    def generated_files(root: Path) -> set[Path]:
        files = {p.relative_to(root) for p in root.rglob("*.md") if p.is_file()}
        for name in ("graph.json", "manifest.json", "diagnostics.json"):
            if (root / name).is_file():
                files.add(Path(name))
        return files

    stale: set[str] = set()
    fresh_files = generated_files(fresh)
    committed_files = generated_files(committed)
    for relative in fresh_files | committed_files:
        committed_file = committed / relative
        fresh_file = fresh / relative
        if not (
            committed_file.is_file()
            and fresh_file.is_file()
            and filecmp.cmp(committed_file, fresh_file, shallow=False)
        ):
            stale.add(str(relative))
    return sorted(stale)


def main() -> None:
    """Console-script entrypoint: friendly one-line errors, exit 130 on Ctrl-C.

    standalone_mode=False so click doesn't swallow KeyboardInterrupt into a
    bare "Aborted!" — interactive sessions (linker prompts, the menu) need
    the cache-preservation message and the conventional 130 exit code.
    """
    try:
        cli(obj={}, standalone_mode=False)
    except click.exceptions.Abort:
        # click converts EOFError/KeyboardInterrupt inside commands to Abort
        click.echo(
            "\nInterrupted — any answers you gave are saved in .lineage-cache.json; run again to continue.",
            err=True,
        )
        sys.exit(130)
    except click.exceptions.Exit as exc:  # e.g. --help / --version
        sys.exit(exc.exit_code)
    except click.ClickException as exc:
        exc.show()
        sys.exit(exc.exit_code)
    except (ConfigError, SiteBuildError) as exc:
        # SiteBuildError (mkdocs failed) is a routine user-facing failure: print
        # one friendly line, reserving the full traceback for -v, to match the
        # module docstring's contract and the other handled error types.
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)
    except KeyboardInterrupt:
        click.echo("\nInterrupted.", err=True)
        sys.exit(130)


if __name__ == "__main__":
    main()


@cli.command()
@click.option("--config", "config_path", default=None, help="Config file path (default: discover).")
@click.option(
    "--out", "out_dir_str", default=None, help="Output directory for CSVs (defaults to output.dir)."
)
def export(config_path: str | None, out_dir_str: str | None) -> None:
    """Export the built graph as deterministic CSV files (client deliverable)."""
    from coop_data_doc.render.export import export_csvs

    config = _load_config(config_path)
    graph_path = config.output_dir() / "graph.json"
    if not graph_path.is_file():
        raise click.ClickException(f"no built graph at {graph_path} — run `coop-data-doc build` first.")

    graph = LineageGraph.model_validate(json.loads(graph_path.read_text(encoding="utf-8")))
    out_dir = Path(out_dir_str) if out_dir_str else config.output_dir()

    export_csvs(graph, out_dir)
    click.echo(f"Exported objects, columns, measures, and edges to {out_dir}/")


@cli.command()
@click.option("--config", "config_path", default=None, help="Config file path (default: discover).")
@click.option(
    "--out", "out_file", type=click.File("w"), default="-", help="Output JSON file (default stdout)."
)
@click.option("--no-parse-cache", is_flag=True, help="Force a cold parse.")
def findings(config_path: str | None, out_file, no_parse_cache: bool) -> None:
    """Emit data-doc diagnostics as a standard review-findings envelope."""
    config = _load_config(config_path)
    _graph, result, warnings = run_pipeline(config, interactive=False, no_parse_cache=no_parse_cache)
    diagnostics = Diagnostics(warnings=warnings, unresolved=list(result.unresolved))
    envelope = diagnostics.to_envelope()
    out_file.write(json.dumps(envelope, indent=2, sort_keys=True) + "\n")


@cli.command()
@click.option("--config", "config_path", default=None, help="Config file path (default: discover).")
@click.option(
    "--baseline",
    "baseline_path",
    type=click.Path(exists=True, dir_okay=False),
    help="Path to baseline graph.json.",
)
@click.option("--git", "git_ref", help="Git ref to read graph.json from (e.g. main).")
@click.option("--files", "files_list", multiple=True, help="Seed from these changed source files.")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["json", "markdown"]),
    default="json",
    help="Output format (default json).",
)
def impact(
    config_path: str | None,
    baseline_path: str | None,
    git_ref: str | None,
    files_list: tuple[str, ...],
    fmt: str,
) -> None:
    """Change-impact diff against a baseline graph.json (e.g., git main)."""
    import subprocess

    from coop_data_doc.graph.diff import impact_map

    config = _load_config(config_path)

    current_graph_path = config.output_dir() / "graph.json"
    if not current_graph_path.is_file():
        raise click.ClickException(
            f"no built graph at {current_graph_path} — run `coop-data-doc build` first."
        )
    current_graph = LineageGraph.model_validate(json.loads(current_graph_path.read_text(encoding="utf-8")))

    old_graph = None
    if baseline_path:
        old_graph = LineageGraph.model_validate(json.loads(Path(baseline_path).read_text(encoding="utf-8")))
    elif git_ref:
        rel_path = current_graph_path.relative_to(config.base_dir).as_posix()
        try:
            old_json_bytes = subprocess.check_output(
                ["git", "show", f"{git_ref}:{rel_path}"], cwd=config.base_dir, stderr=subprocess.DEVNULL
            )
            old_graph = LineageGraph.model_validate(json.loads(old_json_bytes.decode("utf-8")))
        except subprocess.CalledProcessError:
            raise click.ClickException(f"Could not read {rel_path} from git ref {git_ref}")
    else:
        raise click.ClickException("Must provide --baseline or --git")

    impacts, seed_graphs = impact_map(old_graph, current_graph, files_list)

    if fmt == "json":
        click.echo(json.dumps(impacts, indent=2))
    else:
        for nid, down in impacts.items():
            graph = seed_graphs[nid]
            node = graph.nodes.get(nid) or current_graph.nodes.get(nid) or old_graph.nodes.get(nid)
            if node is None:
                # Dangling edge endpoints are unusual but valid graph data;
                # retain their JSON impact entry instead of failing to render.
                click.echo(f"### {nid}")
            else:
                removed = nid not in current_graph.nodes
                trust = " ⚠️ " + node.metadata.get("trust") if node.metadata.get("trust") else ""
                removed_marker = " [removed]" if removed else ""
                click.echo(f"### {node.qualified_display} ({node.node_type.value}){removed_marker}{trust}")
            for d_id in down:
                d_node = graph.nodes.get(d_id) or current_graph.nodes.get(d_id) or old_graph.nodes.get(d_id)
                if d_node is None:
                    click.echo(f"- {d_id}")
                else:
                    click.echo(f"- {d_node.qualified_display} ({d_node.node_type.value})")
            click.echo()
