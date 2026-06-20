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
from coop_data_doc.config import Config, ConfigError, ParseWarning
from coop_data_doc.diagnostics import Diagnostics
from coop_data_doc.crawler import FileKind, crawl
from coop_data_doc.graph.model import LineageGraph
from coop_data_doc.graph.serialize import to_json_file
from coop_data_doc.linker.cache import LineageCache
from coop_data_doc.linker.resolver import ResolutionResult, link_graph
from coop_data_doc.parsers.bim import parse_bim
from coop_data_doc.parsers.pbir import (
    collapse_visuals,
    link_reports_to_models,
    link_visual_bindings,
    parse_legacy_reports,
    parse_pbir,
)
from coop_data_doc.parsers.pbix import parse_pbix
from coop_data_doc.parsers.sql_objects import parse_sql_objects
from coop_data_doc.layering import assign_layers, prune_schemas
from coop_data_doc.parsers.sql_procs import (
    parse_sql_procs,
    resolve_stub_references,
)
from coop_data_doc.parsers.tmdl import link_composite_models, parse_tmdl
from coop_data_doc.progress import Progress, should_enable
from coop_data_doc.render.markdown import render_markdown, write_diagnostics
from coop_data_doc.render.site import build_site, write_mkdocs_config

STRICT_CATEGORIES = ("regex_fallback", "dynamic_sql")
DEFAULT_CONFIG = "coop-data-doc.yml"
_log = logging.getLogger("coop_data_doc")


def run_pipeline(
    config: Config,
    interactive: bool,
    progress: Progress | None = None,
) -> tuple[LineageGraph, ResolutionResult, list[ParseWarning]]:
    """Execute the full crawl -> parse -> link pipeline and return
    (graph, resolution result, warnings). Shared by scan/build/check.

    ``progress`` (optional) drives stderr progress bars; defaults to a
    disabled no-op so callers and tests that don't want output are silent.
    """
    progress = progress or Progress(enabled=False)
    graph = LineageGraph()
    inventory, warnings = crawl(config)
    progress.line(f"Crawling repos… {len(inventory.entries)} files found")
    _log.debug("crawled %d files across %d repos", len(inventory.entries), len(config.repos))

    sql_entries = inventory.by_kind(FileKind.SQL_FILE)
    _log.debug("parsing %d SQL files (dialect=%s)", len(sql_entries), config.sql_dialect)
    with progress.bar("Parsing SQL", total=2 * len(sql_entries)) as tick:
        warnings += parse_sql_objects(sql_entries, graph, config.sql_dialect, on_file=tick)
        warnings += parse_sql_procs(sql_entries, graph, config.sql_dialect, on_file=tick)
    resolve_stub_references(graph)

    tmdl = inventory.by_kind(FileKind.TMDL)
    bim = inventory.by_kind(FileKind.BIM)
    visuals = inventory.by_kind(FileKind.PBIR_VISUAL)
    legacy = inventory.by_kind(FileKind.REPORT_JSON_LEGACY)
    pbix = inventory.by_kind(FileKind.PBIX)
    pbi_total = len(tmdl) + len(bim) + len(visuals) + len(legacy) + len(pbix)
    with progress.bar("Parsing Power BI", total=pbi_total) as tick:
        warnings += parse_tmdl(tmdl, graph, on_file=tick)
        warnings += parse_bim(bim, graph, on_file=tick)
        warnings += parse_pbir(visuals, inventory.by_kind(FileKind.PBIR_PAGE), graph, on_file=tick)
        warnings += parse_legacy_reports(legacy, graph, on_file=tick)
        warnings += parse_pbix(pbix, graph, on_file=tick)
    warnings += link_visual_bindings(graph)
    warnings += link_composite_models(graph)

    dropped = prune_schemas(graph, config.ignore_schemas)
    if dropped:
        progress.line(f"Dropped {dropped} objects in ignored/system schemas")
    warnings += assign_layers(graph, config)

    if dropped:
        _log.debug("pruned %d nodes in system/ignored schemas", dropped)
    progress.line("Linking cross-repo references…")
    cache = LineageCache.load(config.base_dir / ".lineage-cache.json")
    result, link_warnings = link_graph(graph, config, cache, interactive)
    warnings += link_warnings
    # reports become downstream of their models, then visuals fold into pages
    link_reports_to_models(graph)
    collapse_visuals(graph)
    _log.debug(
        "done: %d nodes, %d edges, %d cross-repo links, %d unresolved, %d warnings",
        len(graph.nodes),
        len(graph.edges),
        result.resolved,
        len(result.unresolved),
        len(warnings),
    )
    return graph, result, warnings


def _strict_failures(result: ResolutionResult, warnings: list[ParseWarning]) -> list[str]:
    failures = [f"unresolved reference: {key}" for key in result.unresolved]
    failures += [
        f"{warning.category}: {warning.file}" for warning in warnings if warning.category in STRICT_CATEGORIES
    ]
    return failures


def _scan(
    config: Config,
    non_interactive: bool,
    strict: bool,
    quiet: bool,
    progress: Progress | None = None,
) -> tuple[LineageGraph, Diagnostics]:
    graph, result, warnings = run_pipeline(config, interactive=not non_interactive, progress=progress)
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


def _load_config(config_path: str | None = None) -> Config:
    """Load config, with discovery if no explicit path given."""
    if config_path is None:
        found = Config.find()
        if found is None:
            raise ConfigError(
                f"No {DEFAULT_CONFIG} found in this folder or any parent. "
                f"Run `coop-data-doc init` to scaffold one, or `coop-data-doc setup` "
                f"for the interactive wizard."
            )
        config_path = str(found)
    return Config.load(Path(config_path))


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
    # Use discovery to find config, not just cwd
    config_path = Config.find()
    config_exists = config_path is not None
    if config_exists:
        message = f"Found {config_path.name} at {config_path.parent}. What would you like to do?"
        choices = [
            questionary.Choice("Update the docs (scan repos + rebuild everything)", "update"),
            questionary.Choice("Scan only (refresh graph.json, no rendering)", "scan"),
            questionary.Choice("Change settings (re-run the setup wizard)", "setup"),
            questionary.Choice("Check docs freshness (the CI gate)", "check"),
            questionary.Choice("Check for updates & show the upgrade command", "upgrade"),
            questionary.Choice("Exit", "exit"),
        ]
    else:
        message = "No coop-data-doc.yml found in this folder or any parent. What would you like to do?"
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
    if action == "setup":
        ctx.invoke(setup, path=DEFAULT_CONFIG)
    elif action == "init":
        ctx.invoke(init, path=DEFAULT_CONFIG, force=False)
    elif action == "scan":
        ctx.invoke(scan, config_path=DEFAULT_CONFIG, non_interactive=False, strict=False)
    elif action == "check":
        ctx.invoke(check, config_path=DEFAULT_CONFIG)
    elif action == "upgrade":
        ctx.invoke(upgrade)
    elif action == "update":
        _run_build(
            ctx,
            config_path=DEFAULT_CONFIG,
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
    found = Config.find()
    if found is None:
        click.echo("status: no config found")
        click.echo("  Run `coop-data-doc init` to scaffold a starter config.")
        click.echo("  Or `coop-data-doc setup` for the interactive wizard.")
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

    # Check freshness if docs exist
    if out_dir.is_dir():
        try:
            graph, result, warnings = run_pipeline(config, interactive=False)
            with tempfile.TemporaryDirectory() as tmp:
                fresh = Path(tmp) / "docs"
                shutil.copytree(out_dir, fresh)
                render_markdown(graph, fresh, config.project_name)
                to_json_file(graph, fresh / "graph.json")
                stale = _tree_diff(out_dir, fresh)
                if stale:
                    click.echo(f"freshness: stale ({len(stale)} files differ)")
                    click.echo("  Run `coop-data-doc build` to update.")
                else:
                    click.echo("freshness: up to date")
        except Exception as exc:
            click.echo(f"freshness: could not check ({exc})")
    else:
        click.echo("freshness: no docs yet — run `coop-data-doc build`")

    # Summary of unresolved
    if found:
        try:
            graph, result, _ = run_pipeline(config, interactive=False)
            if result.unresolved:
                click.echo(f"unresolved: {len(result.unresolved)} items (run interactively to resolve)")
            else:
                click.echo("unresolved: 0")
        except Exception:
            pass


@cli.command()
@click.argument("path", default=DEFAULT_CONFIG)
@click.pass_context
def setup(ctx: click.Context, path: str) -> None:
    """Interactively create or update coop-data-doc.yml.

    Prompts for every value, prefilled from the existing config when present,
    then saves and re-validates. Ctrl-C before the end writes nothing.
    """
    from coop_data_doc.wizard import run_setup

    try:
        config = run_setup(Path(path))
    except KeyboardInterrupt:
        click.echo("\nSetup cancelled — nothing was written.", err=True)
        sys.exit(130)
    except OSError:
        click.echo(
            "setup needs an interactive terminal. In CI or scripts, edit "
            "coop-data-doc.yml directly or scaffold one with `coop-data-doc init`.",
            err=True,
        )
        sys.exit(1)
    build_cmd = "coop-data-doc build" if path == DEFAULT_CONFIG else f"coop-data-doc build --config {path}"
    if config is None:
        click.echo(f"Saved {path}. Fix the noted problem, then run `{build_cmd}`.")
        return
    click.echo(
        f"Saved {path} — project '{config.project_name}', "
        f"{len(config.repos)} repos, {len(config.schema_mappings)} schema mapping(s)."
    )
    # offer to build right away; either way show the command to run it later
    try:
        build_now = questionary.confirm("Build the docs now?", default=True, auto_enter=False).ask()
    except (KeyboardInterrupt, OSError):
        build_now = None
    if build_now:
        _run_build(ctx, path, non_interactive=False, strict=False, skip_html=False, serve=False)
    else:
        click.echo(f"Build them whenever you're ready with:  {build_cmd}")


@cli.command()
@click.argument("path", default=DEFAULT_CONFIG)
@click.option("--force", is_flag=True, help="Overwrite an existing config.")
def init(path: str, force: bool) -> None:
    """Write a starter coop-data-doc.yml to edit by hand (see also: setup)."""
    target = Path(path)
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


@cli.command()
@click.option(
    "--config", "config_path", default=None, help="Config file path (default: discover in cwd and parents)."
)
@click.option("--non-interactive", is_flag=True, help="Never prompt (CI mode).")
@click.option("--strict", is_flag=True, help="Exit 2 on unresolved refs / risky parses.")
@click.pass_context
def scan(ctx: click.Context, config_path: str | None, non_interactive: bool, strict: bool) -> None:
    """Crawl, parse, and link both repos; write graph.json."""
    config = _load_config(config_path)
    progress = Progress(should_enable(ctx.obj["quiet"]))
    _scan(config, non_interactive, strict, ctx.obj["quiet"], progress=progress)


def _run_build(
    ctx: click.Context,
    config_path: str,
    non_interactive: bool,
    strict: bool,
    skip_html: bool,
    serve: bool,
) -> None:
    """Shared implementation behind `build` and `update`."""
    config = _load_config(config_path)
    progress = Progress(should_enable(ctx.obj["quiet"]))
    graph, diagnostics = _scan(config, non_interactive, strict, ctx.obj["quiet"], progress=progress)
    out_dir = config.output_dir()
    with progress.bar("Rendering pages", total=len(graph.nodes)) as tick:
        render_markdown(graph, out_dir, config.project_name, on_node=tick)
    write_diagnostics(out_dir, diagnostics, config.project_name)
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
    click.echo(f"HTML portal:   file://{index}", err=True)


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
    config_path: str,
    non_interactive: bool,
    strict: bool,
    skip_html: bool,
    serve: bool,
) -> None:
    """Full pipeline: scan + markdown docs + searchable HTML portal."""
    _run_build(ctx, config_path, non_interactive, strict, skip_html, serve)


@cli.command()
@_with_build_options
@click.pass_context
def update(
    ctx: click.Context,
    config_path: str,
    non_interactive: bool,
    strict: bool,
    skip_html: bool,
    serve: bool,
) -> None:
    """Re-scan the repos and refresh all documentation (same as build)."""
    _run_build(ctx, config_path, non_interactive, strict, skip_html, serve)


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
    help="Tolerate risky-parse warnings (regex_fallback/dynamic_sql); still "
    "fail on unresolved references and stale docs.",
)
@click.pass_context
def check(ctx: click.Context, config_path: str | None, lenient: bool) -> None:
    """CI gate: fail when committed docs are stale, references are
    unresolved, or (unless --lenient) risky-parse warnings exist
    (regex_fallback / dynamic_sql). Exit 2 for pipeline problems, 1 for
    stale docs."""
    config = _load_config(config_path)
    graph, result, warnings = run_pipeline(config, interactive=False)
    if lenient:
        failures = [f"unresolved reference: {key}" for key in result.unresolved]
    else:
        failures = _strict_failures(result, warnings)
    if failures:
        for failure in failures:
            click.echo(f"check: {failure}", err=True)
        sys.exit(2)
    committed = config.output_dir()
    if not committed.is_dir():
        raise click.ClickException(f"no committed docs at {committed}; run `coop-data-doc build` first")
    with tempfile.TemporaryDirectory() as tmp:
        fresh = Path(tmp) / "docs"
        # start from the committed tree so human-authored Business Intent
        # blocks are preserved in the regenerated pages
        shutil.copytree(committed, fresh)
        render_markdown(graph, fresh, config.project_name)
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
    except ConfigError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)
    except KeyboardInterrupt:
        click.echo("\nInterrupted.", err=True)
        sys.exit(130)


if __name__ == "__main__":
    main()
