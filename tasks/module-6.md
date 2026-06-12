# MODULE 6 — CLI (`click`) ✅ IMPLEMENTED

> Status: **done** — kept as interface reference; do not reimplement.

**Files to create:** `src/coop_data_doc/cli.py`, `tests/test_cli.py`.

**Inputs you can rely on:** every prior module's public API:
`Config.load/scaffold`, `crawl(config)`, `parse_sql_objects/parse_sql_procs/classify_silver`,
the M3 parser entrypoints, `LineageCache`, `link_graph`, `run_interactive_session`,
`render_markdown`, `write_mkdocs_config`, `build_site`, `graph.serialize`.

## Commands (`coop-data-doc`, console-script already wired in pyproject)

```
coop-data-doc init [--force] [PATH]
coop-data-doc scan  [--config PATH] [--non-interactive] [--strict] [-v|-q]
coop-data-doc build [--config PATH] [--non-interactive] [--strict] [--skip-html] [--serve] [-v|-q]
coop-data-doc check [--config PATH]
coop-data-doc --version
```

- **`init`** — `Config.scaffold("coop-data-doc.yml")`; exists without `--force` → friendly error,
  exit 1. Print next-steps hint ("edit repo paths, then run `coop-data-doc build`").
- **`scan`** — pipeline through Module 4: load config → crawl → SQL parsers → PBI parsers →
  `classify_silver` → cache load/prune → `link_graph` (interactive unless `--non-interactive`)
  → write `graph.json` to `output.dir`. Then print a **warning summary table** to stderr:
  count per warning category, plus the top 5 files by warning count. `--strict`: exit 2 if any
  unresolved references or warnings of category `regex_fallback`/`dynamic_sql` exist.
- **`build`** — everything `scan` does, then `render_markdown` + (unless `--skip-html`)
  `write_mkdocs_config` + `build_site`. `--serve`: after building, `os.execvp` into
  `mkdocs serve -f <config>` for live preview. Final line prints the absolute `file://` URL of
  `site/index.html`.
- **`check`** — CI freshness gate: run the full pipeline non-interactive + strict into a temp
  dir, then compare the regenerated markdown tree against the committed `output.dir`
  (`filecmp.dircmp`, ignoring `manifest.json` ordering is NOT needed — output is deterministic).
  Differences → list them, exit 1. Clean → "docs are up to date", exit 0.

## Cross-cutting requirements
- One shared `_run_pipeline(config, interactive) -> tuple[LineageGraph, ResolutionResult, list[ParseWarning]]`
  helper — commands are thin wrappers; no pipeline logic lives in cli.py beyond sequencing.
- Errors: user-facing failures (bad config, missing repo, mkdocs failure) print ONE friendly
  line to stderr and exit 1 — full traceback only with `-v`. Implement via a try/except in a
  `main()` wrapper around the click group.
- KeyboardInterrupt during interactive linking → "Session saved to .lineage-cache.json — run
  again to continue.", exit 130.
- `-q` silences the warning summary; `-v` sets logging DEBUG.

## Tests (`click.testing.CliRunner`, fixtures from Modules 1–3)
- `init` creates a loadable config; second run without `--force` exits 1.
- `scan --non-interactive` on fixture repos: exit 0, `graph.json` exists and equals golden.
- `scan --non-interactive --strict` with an unresolved fixture: exit 2.
- `build --non-interactive --skip-html`: markdown tree appears.
- `check` passes on freshly built docs; fails (exit 1, names the file) after a doc is hand-edited.
- `--version` prints the package version.

## Acceptance criteria
- No tracebacks reach the user without `-v`.
- All pipeline behavior identical between `scan` and `build` (shared helper).
