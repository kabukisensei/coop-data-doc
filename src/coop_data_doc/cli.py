"""Command-line interface (Module 6).

Thin wrappers around the pipeline modules — no parsing or rendering logic
lives here. User-facing failures print one friendly line; tracebacks only
with -v.
"""

from __future__ import annotations

import filecmp
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

import click

from coop_data_doc import __version__
from coop_data_doc.config import Config, ConfigError, ParseWarning
from coop_data_doc.crawler import FileKind, crawl
from coop_data_doc.graph.model import LineageGraph
from coop_data_doc.graph.serialize import to_json_file
from coop_data_doc.linker.cache import LineageCache
from coop_data_doc.linker.resolver import ResolutionResult, link_graph
from coop_data_doc.parsers.bim import parse_bim
from coop_data_doc.parsers.pbir import link_visual_bindings, parse_legacy_reports, parse_pbir
from coop_data_doc.parsers.pbix import parse_pbix
from coop_data_doc.parsers.sql_objects import parse_sql_objects
from coop_data_doc.parsers.sql_procs import (
    classify_silver,
    parse_sql_procs,
    resolve_stub_references,
)
from coop_data_doc.parsers.tmdl import parse_tmdl
from coop_data_doc.render.markdown import render_markdown
from coop_data_doc.render.site import build_site, write_mkdocs_config

STRICT_CATEGORIES = ("regex_fallback", "dynamic_sql")
DEFAULT_CONFIG = "coop-data-doc.yml"


def run_pipeline(
    config: Config, interactive: bool
) -> tuple[LineageGraph, ResolutionResult, list[ParseWarning]]:
    """Execute the full crawl -> parse -> link pipeline and return
    (graph, resolution result, warnings). Shared by scan/build/check.
    """
    graph = LineageGraph()
    inventory, warnings = crawl(config)

    sql_entries = inventory.by_kind(FileKind.SQL_FILE)
    warnings += parse_sql_objects(sql_entries, graph, config.sql_dialect)
    warnings += parse_sql_procs(sql_entries, graph, config.sql_dialect)
    resolve_stub_references(graph)

    warnings += parse_tmdl(inventory.by_kind(FileKind.TMDL), graph)
    warnings += parse_bim(inventory.by_kind(FileKind.BIM), graph)
    warnings += parse_pbir(
        inventory.by_kind(FileKind.PBIR_VISUAL),
        inventory.by_kind(FileKind.PBIR_PAGE),
        graph,
    )
    warnings += parse_legacy_reports(inventory.by_kind(FileKind.REPORT_JSON_LEGACY), graph)
    warnings += parse_pbix(inventory.by_kind(FileKind.PBIX), graph)
    warnings += link_visual_bindings(graph)

    classify_silver(graph)

    cache = LineageCache.load(config.base_dir / ".lineage-cache.json")
    result, link_warnings = link_graph(graph, config, cache, interactive)
    warnings += link_warnings
    return graph, result, warnings


def _warning_summary(warnings: list[ParseWarning], quiet: bool) -> None:
    if quiet or not warnings:
        return
    by_category: dict[str, int] = {}
    by_file: dict[str, int] = {}
    for warning in warnings:
        by_category[warning.category] = by_category.get(warning.category, 0) + 1
        by_file[warning.file] = by_file.get(warning.file, 0) + 1
    click.echo("\nWarnings:", err=True)
    for category in sorted(by_category):
        click.echo(f"  {category:30} {by_category[category]}", err=True)
    top = sorted(by_file.items(), key=lambda pair: (-pair[1], pair[0]))[:5]
    if top:
        click.echo("  most affected files:", err=True)
        for file, count in top:
            click.echo(f"    {file} ({count})", err=True)


def _strict_failures(result: ResolutionResult, warnings: list[ParseWarning]) -> list[str]:
    failures = [f"unresolved reference: {key}" for key in result.unresolved]
    failures += [
        f"{warning.category}: {warning.file}"
        for warning in warnings
        if warning.category in STRICT_CATEGORIES
    ]
    return failures


def _scan(config: Config, non_interactive: bool, strict: bool, quiet: bool) -> LineageGraph:
    graph, result, warnings = run_pipeline(config, interactive=not non_interactive)
    out_dir = config.output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    to_json_file(graph, out_dir / "graph.json")
    _warning_summary(warnings, quiet)
    if not quiet:
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
    return graph


def _load_config(config_path: str) -> Config:
    return Config.load(Path(config_path))


@click.group()
@click.version_option(version=__version__, prog_name="coop-data-doc")
@click.option("-v", "--verbose", is_flag=True, help="Debug logging and full tracebacks.")
@click.option("-q", "--quiet", is_flag=True, help="Suppress warning summaries.")
@click.pass_context
def cli(ctx: click.Context, verbose: bool, quiet: bool) -> None:
    """Offline data-lineage documentation for SQL + Power BI estates."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    ctx.obj["quiet"] = quiet
    logging.basicConfig(level=logging.DEBUG if verbose else logging.WARNING)


@cli.command()
@click.argument("path", default=DEFAULT_CONFIG)
@click.option("--force", is_flag=True, help="Overwrite an existing config.")
def init(path: str, force: bool) -> None:
    """Create a starter coop-data-doc.yml."""
    target = Path(path)
    if target.exists() and not force:
        raise click.ClickException(f"{target} already exists (use --force to overwrite)")
    if target.exists():
        target.unlink()
    Config.scaffold(target)
    click.echo(f"Wrote {target}.")
    click.echo("Next: edit the two repo paths, then run `coop-data-doc build`.")


@cli.command()
@click.option("--config", "config_path", default=DEFAULT_CONFIG, show_default=True)
@click.option("--non-interactive", is_flag=True, help="Never prompt (CI mode).")
@click.option("--strict", is_flag=True, help="Exit 2 on unresolved refs / risky parses.")
@click.pass_context
def scan(ctx: click.Context, config_path: str, non_interactive: bool, strict: bool) -> None:
    """Crawl, parse, and link both repos; write graph.json."""
    config = _load_config(config_path)
    _scan(config, non_interactive, strict, ctx.obj["quiet"])


@cli.command()
@click.option("--config", "config_path", default=DEFAULT_CONFIG, show_default=True)
@click.option("--non-interactive", is_flag=True, help="Never prompt (CI mode).")
@click.option("--strict", is_flag=True, help="Exit 2 on unresolved refs / risky parses.")
@click.option("--skip-html", is_flag=True, help="Markdown only; skip the mkdocs site.")
@click.option("--serve", is_flag=True, help="Start `mkdocs serve` after building.")
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
    config = _load_config(config_path)
    graph = _scan(config, non_interactive, strict, ctx.obj["quiet"])
    out_dir = config.output_dir()
    render_markdown(graph, out_dir, config.project_name)
    click.echo(f"Markdown docs: {out_dir}", err=True)
    if skip_html:
        return
    mkdocs_config = write_mkdocs_config(out_dir, config.site_dir(), config.project_name, graph)
    if serve:
        os.execvp(sys.executable, [sys.executable, "-m", "mkdocs", "serve", "-f", str(mkdocs_config)])
    build_site(mkdocs_config, config.site_dir())
    index = config.site_dir() / "index.html"
    click.echo(f"HTML portal:   file://{index}", err=True)


@cli.command()
@click.option("--config", "config_path", default=DEFAULT_CONFIG, show_default=True)
@click.pass_context
def check(ctx: click.Context, config_path: str) -> None:
    """CI gate: fail if committed docs are stale or refs are unresolved."""
    config = _load_config(config_path)
    graph, result, warnings = run_pipeline(config, interactive=False)
    failures = _strict_failures(result, warnings)
    if failures:
        for failure in failures:
            click.echo(f"check: {failure}", err=True)
        sys.exit(2)
    committed = config.output_dir()
    if not committed.is_dir():
        raise click.ClickException(
            f"no committed docs at {committed}; run `coop-data-doc build` first"
        )
    with tempfile.TemporaryDirectory() as tmp:
        fresh = Path(tmp) / "docs"
        # start from the committed tree so human-authored Business Intent
        # blocks are preserved in the regenerated pages
        shutil.copytree(committed, fresh)
        render_markdown(graph, fresh, config.project_name)
        to_json_file(graph, fresh / "graph.json")
        stale = _tree_diff(committed, fresh)
        if stale:
            for path in stale:
                click.echo(f"stale: {path}", err=True)
            click.echo("docs are out of date — run `coop-data-doc build`", err=True)
            sys.exit(1)
    click.echo("docs are up to date")


def _tree_diff(committed: Path, fresh: Path) -> list[str]:
    """Names of generated files that differ between the two doc trees."""
    stale: list[str] = []
    for fresh_file in sorted(fresh.rglob("*.md")) + [fresh / "graph.json", fresh / "manifest.json"]:
        if not fresh_file.is_file():
            continue
        relative = fresh_file.relative_to(fresh)
        committed_file = committed / relative
        if not committed_file.is_file() or not filecmp.cmp(
            committed_file, fresh_file, shallow=False
        ):
            stale.append(str(relative))
    return stale


def main() -> None:
    """Console-script entrypoint: friendly one-line errors, exit 130 on Ctrl-C."""
    try:
        cli(obj={})
    except ConfigError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)
    except KeyboardInterrupt:
        click.echo(
            "\nInterrupted — answers so far are saved in .lineage-cache.json; "
            "run again to continue.",
            err=True,
        )
        sys.exit(130)


if __name__ == "__main__":
    main()
