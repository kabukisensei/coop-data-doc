# coop-data-doc — Agent Contract

> **For AI agents and automation scripts.** This document is the canonical
> contract for programmatic interaction with `coop-data-doc`. It is kept
> up-to-date with the code; if it diverges, the code wins and this doc
> should be patched.

## Quick Start (Agent Mode)

```bash
# 1. Check if a project is already configured
coop-data-doc status

# 2. If not, scaffold a config (non-interactive)
coop-data-doc init --force
# ...edit coop-data-doc.yml...

# 3. Build docs headlessly
coop-data-doc build --non-interactive --strict

# 4. Check freshness in CI
coop-data-doc check --lenient
```

> **PATH-independent invocation.** Every command also runs as
> `python -m coop_data_doc <command>` (e.g. `python -m coop_data_doc build
> --non-interactive`). Use this when the `coop-data-doc` console script isn't on
> `PATH` — same entry point, no install-location dependency.

> **Python for dev installs: 3.10–3.13 only — never 3.14.** 3.14 breaks the
> editable-install `.pth` / console-script imports, so both `coop-data-doc`
> and `python -m coop_data_doc` fail on a `pip install -e` install. Setup:
> `python3.13 -m venv .venv && .venv/bin/pip install -e ".[dev]"`
> (or `make setup`). End users installing from PyPI via pipx are unaffected.

## Environment (Working on This Repo)

Linux and macOS are equally supported; every command below is copy-paste on both.

- **Venv** (Python 3.13; 3.10–3.13 only, never 3.14). Rebuild from scratch:
  `rm -rf .venv && python3.13 -m venv .venv && .venv/bin/pip install -e ".[dev]"`
- **Tests:** `.venv/bin/python -m pytest -q` — expect `~310 passed` in a few seconds, zero failures.
- **Lint:** `.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests` — pass looks like `All checks passed!` then `N files already formatted`.
- **Before starting any work:** `git pull --ff-only`. If the tree is dirty or the pull fails: stop and report — do not stash, reset, or commit around it.
- **Secrets:** none exist and none are needed (the pipeline is offline). Never add tokens, connection strings, or credentials to code, config, or fixtures.
- **Releases:** pushing a `v*` tag publishes to PyPI (`.github/workflows/publish.yml`). Never create or push a `v*` tag unless Aaron explicitly requested a release and named the version — a clean tree or a finished task is **not** a release signal.
- **Headless machines (VPS/CI):** run only this repo's unit tests and fixtures. The full-corpus reference estates (`fabric` / `fabric-dw`, see `CONTRIBUTING.md` → Testing strategy) live on Azure DevOps with interactive-only auth and **cannot be cloned or pulled headlessly**; any task needing them (full estate builds, the ~948-object benchmark) is Aaron's-Mac-only — report it back instead of attempting it.

## CLI Contract for Agents

All commands exit with these codes:

| Exit code | Meaning |
|-----------|---------|
| `0` | Success |
| `1` | Stale docs / friendly error / config not found |
| `2` | Unresolved references, risky parses, error-severity diagnostics (corrupt/undecodable files, truncated procs — data is missing), or invalid CLI args |
| `130` | Cancelled with Ctrl+C |

### Global flags (before subcommand)

| Flag | Effect |
|------|--------|
| `--version` | Print version and exit 0 |
| `-v, --verbose` | Debug logging + full tracebacks |
| `-q, --quiet` | Suppress warning summaries and progress |
| `--log-file PATH` | Write a verbose debug log to `PATH` (console stays at warning level) |

`--config PATH` is **not** global — it is a per-subcommand option (see each command below).

### Commands

| Command | Agent use | Key flags |
|---------|---------|-----------|
| `coop-data-doc` | Interactive menu (bare, TTY only) | — |
| `coop-data-doc status` | **Check project state** — config exists? docs built? stale? | `--config` |
| `coop-data-doc init [PATH]` | Scaffold a starter config | `--force` |
| `coop-data-doc setup [PATH]` | Interactive wizard (human) | — |
| `coop-data-doc build` | Full pipeline + render | `--non-interactive`, `--strict`, `--skip-html`, `--no-parse-cache`, `--jobs N`, `--config` |
| `coop-data-doc update` | Alias for `build` — prefer `build` in scripts/CI (prints a notice: in the sibling review tools `update` means self-update; the self-update command here is `upgrade`) | Same as `build` |
| `coop-data-doc scan` | Crawl + parse + link only; writes `graph.json` | `--non-interactive`, `--strict`, `--no-parse-cache`, `--jobs N`, `--config` |
| `coop-data-doc check` | CI gate — exits 1 if stale, 2 if problems | `--lenient`, `--no-parse-cache`, `--config` |
| `coop-data-doc folders` | List each repo's top-level folders + documented state (JSON) | `--config` |
| `coop-data-doc set-folders` | Set which top-level folders a repo documents (non-interactive) | `--repo` (required), `--skip` (comma-separated), `--config` |
| `coop-data-doc lineage OBJECT` | Print one object's lineage from the built `graph.json` (JSON) | `--depth`, `--config` |
| `coop-data-doc show-config` | Print the current config as JSON (the `config-set` shape) | `--config` |
| `coop-data-doc config-set` | Apply a JSON patch to the config, non-interactively | `--from-json` (file or `-`), `--config` |
| `coop-data-doc resolve` | List ambiguous cross-repo links + candidates (JSON) | `--config` |
| `coop-data-doc resolve-apply` | Write link decisions to the cache (run `build` separately to apply them) | `--from-json` (file or `-`), `--config` |
| `coop-data-doc upgrade` | Check for a newer release and print the upgrade command (does **not** self-update) | — |
| `coop-data-doc help [CMD]` | Show help | — |

## Config File Discovery

`coop-data-doc` searches for `coop-data-doc.yml` in this order:

1. `--config` argument (explicit)
2. `./coop-data-doc.yml` (current directory)
3. Walk up parent directories until found (like `git` finding `.git`)
4. If not found, exit 1 with a message suggesting `coop-data-doc init`

## Output Artifacts (Machine-Readable)

After a successful `build`/`update`, these files exist. `scan` is rendering-free:
it writes only `data-docs/graph.json` and `data-docs/diagnostics.json` (no
`manifest.json` and no Markdown pages).

### `data-docs/manifest.json`

The entire lineage graph in one JSON file. **Preferred entry point for agents.**
Written by `build`/`update` only (the render step); `scan` does not produce it —
read `graph.json` after a `scan`.

Shape:
```json
{
  "nodes": {
    "view:sales.dim_customer": {
      "node_type": "view",
      "schema_name": "sales",
      "name": "dim_customer",
      "source_file": "views/sales/dim_customer.sql",
      "columns": [...],
      "metadata": {...}
    }
  },
  "edges": [
    {"source_id": "...", "target_id": "...", "edge_type": "feeds", "evidence": "..."}
  ]
}
```

### `data-docs/graph.json`

Byte-identical copy of `manifest.json` (written by every pipeline run). Read `manifest.json`.

### `data-docs/index.md`

Object counts and unresolved items. Human-readable but machine-parseable.

### `data-docs/<type>/<slug>.md`

One page per object. **Read the `path` field in front-matter** to locate; do not compute filenames.

### `data-docs-site/index.html`

Human-facing HTML portal. Works over `file://` with no network.

### `.lineage-cache.json`

Mapping answers from interactive runs. **Commit this file.** It makes subsequent runs fully automatic.

### `.coop-data-doc-parse-cache.json`

Incremental per-file SQL parse cache (content-hashed): unchanged SQL files are replayed
instead of re-parsed on the next build. **Do NOT commit this file** — it is *derivable*
(a cold rebuild reproduces it) and already gitignored. It is transparent (a warm build is
byte-identical to a cold one) and self-healing (a corrupt/stale cache degrades to a cold
parse with a `parse_cache_invalid` warning, which is a warning, never a CI failure). Pass
`--no-parse-cache` to `scan`/`build`/`update`/`check` to bypass it.

## Page Front-Matter Contract

Every generated `.md` page starts with strict YAML:

```yaml
---
id: "view:sales.dim_customer"
type: "view"
name: "dim_customer"
schema: "sales"
layer: "gold"
source_file: "views/sales/dim_customer.sql"
path: "view/sales-dim_customer-<hash>.md"
upstream_inputs:
  - "gold_table:dbo.fact_sales"
downstream_dependents:
  - "pbi_table:sales.dim_customer"
tags:
  - "sales"
---
```

**Rules:**
- `id` format: `"<type>:<schema>.<name>"` (schema omitted for schema-less objects)
- `path` is the source of truth for file location. Do not compute it.
- `upstream_inputs` and `downstream_dependents` are already flow-normalized (data-flow direction).
- Empty `upstream_inputs` does **not** mean "verified no sources" — check `metadata` in `manifest.json`.

## Trust Markers (in `manifest.json` nodes)

| Marker | Meaning |
|--------|---------|
| `parse_quality: "regex_fallback"` | Lineage from pattern-matching, not full parse. Verify before high-stakes use. |
| `dynamic_sql_untraced: true` | Proc builds SQL in strings; some reads/writes knowingly missing. |
| `unresolved: true` | Human hasn't mapped this source yet. |
| `skipped: true` | Human chose "skip for now". |
| `external_source: true` | Deliberately outside these repos. |
| `columns_unresolved: true` | Column list couldn't be derived (e.g. `SELECT *`). |
| `pbix_model_opaque: true` | `.pbix` model couldn't be extracted. |
| `dax_refs_heuristic: true` | Present on **every** measure. Check `unmatched_dax_refs` for discriminating signal. |
| `measure_table: true` (pbi_table) | A measure/calculation "home" table: hosts measures, has no visible data columns. |
| `declared_model` (report) | The model a report's `definition.pbir` authoritatively binds it to; `declared_model_unresolved: true` when that model isn't in the repos. |

## Traversal Rules

From `manifest.json` edges (authoring direction → data-flow direction):

| `edge_type` | Data flows |
|-------------|------------|
| `reads`, `references`, `visualizes` | **target → source** |
| `writes`, `feeds`, `defines` | **source → target** |

Front-matter `upstream_inputs`/`downstream_dependents` are already flow-normalized — prefer them when reading pages.

> **Reports are collapsed.** The final graph has one node per report (no
> `report_page`/`visual` nodes): a report is downstream of its model(s) via
> `feeds`, and references measures/tables via `report → … visualizes` edges.

## Common Agent Tasks

### "What breaks if I change X?"

```python
# Read manifest.json
# Find node for X by id
# Follow downstream_dependents transitively
```

### "Where does this number come from?"

```python
# Read measure page → upstream_inputs → follow to SQL sources
# DAX is on the measure page under "## DAX"
```

### "Build docs in CI"

```bash
coop-data-doc build --non-interactive --strict
# Exit 0 = success, 2 = problems (unresolved refs or risky parses)
```

### "Check docs freshness"

```bash
coop-data-doc check
# Exit 0 = up to date, 1 = stale, 2 = problems
```

## Determinism Guarantee

Same inputs + same cache → **byte-identical output**. This is CI-enforced via `tests/test_determinism.py`. Agents can rely on:

- Sorted iteration everywhere
- No timestamps or randomness in output
- `newline="\n"` on all generated files
- The incremental parse cache is **transparent**: a warm-cache build is byte-identical to a cold (`--no-parse-cache`) build — the same test suite proves warm == cold.
- Parallel SQL parsing (`--jobs N`) is **transparent** too: each file is parsed in an isolated worker but the per-file contributions are merged back in sorted file order, so `--jobs N` (any N) is byte-identical to `--jobs 1` and to a cold serial build, warm or cold. Only the SQL parsers fan out (all cross-file passes stay serial). The worker is a module-level, spawn-safe function (works on Windows spawn), a failing worker degrades that one file to a `parse_worker_error` diagnostic without aborting the build, and a pool that can't start falls back to serial. Pin the default without the flag via `COOP_DATA_DOC_JOBS`.

## Non-Interactive Mode

`--non-interactive` is the agent's friend. It:
- Never prompts for input
- Treats unresolved items as warnings (not blocking)
- Still writes `.lineage-cache.json` for any fuzzy-auto matches
- Returns exit code 0 if the pipeline completes, even with unresolved items

Use `--strict` to make unresolved items fatal (exit 2).

## Environment Variables

| Variable | Effect |
|----------|--------|
| `COOP_DATA_DOC_CONFIG` | Default config file path (overrides `./coop-data-doc.yml`) |
| `COOP_DATA_DOC_JOBS` | Default SQL-parse worker count when `--jobs` is omitted (capped at 8, floored at 1; a non-integer is ignored). An explicit `--jobs N` always wins. Set `1` for pool-free, fully in-process runs. |

## Python API (Advanced)

```python
from coop_data_doc.config import Config
from coop_data_doc.cli import run_pipeline

config = Config.load("coop-data-doc.yml")
graph, result, warnings = run_pipeline(config, interactive=False)
# graph.nodes, graph.edges, result.resolved, result.unresolved
```

## Version

This contract matches `coop-data-doc` version `0.30.0`.

## Working the backlog (agents)

This repo's work queue is its GitHub issues labeled **`agent:ready`**:
`gh issue list --label agent:ready --state open`. Each issue is self-contained
(Context / Problem / Proposed fix / Acceptance criteria). Rules of engagement:

- Read this file fully first; take ONE issue at a time (oldest first unless one
  blocks another).
- Implement to the acceptance criteria; run the full test suite + lint before
  every commit; commit with `Fixes #N` so the issue closes on push.
- Never push tags, release, or bump versions — Aaron releases (see the release
  rules above).
- An open issue WITHOUT the `agent:ready` label is waiting on a human decision —
  leave it alone.
