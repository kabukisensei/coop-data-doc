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

## CLI Contract for Agents

All commands exit with these codes:

| Exit code | Meaning |
|-----------|---------|
| `0` | Success |
| `1` | Stale docs / friendly error / config not found |
| `2` | Unresolved references, risky parses, or invalid CLI args |
| `130` | Cancelled with Ctrl+C |

### Global flags (before subcommand)

| Flag | Effect |
|------|--------|
| `--version` | Print version and exit 0 |
| `-v, --verbose` | Debug logging + full tracebacks |
| `-q, --quiet` | Suppress warning summaries and progress |
| `--config PATH` | Use this config file instead of `./coop-data-doc.yml` |

### Commands

| Command | Agent use | Key flags |
|---------|---------|-----------|
| `coop-data-doc` | Interactive menu (bare, TTY only) | — |
| `coop-data-doc status` | **Check project state** — config exists? docs built? stale? | `--config` |
| `coop-data-doc init [PATH]` | Scaffold a starter config | `--force` |
| `coop-data-doc setup [PATH]` | Interactive wizard (human) | — |
| `coop-data-doc build` | Full pipeline + render | `--non-interactive`, `--strict`, `--skip-html`, `--config` |
| `coop-data-doc update` | Alias for `build` | Same as `build` |
| `coop-data-doc scan` | Crawl + parse + link only; writes `graph.json` | `--non-interactive`, `--strict`, `--config` |
| `coop-data-doc check` | CI gate — exits 1 if stale, 2 if problems | `--lenient`, `--config` |
| `coop-data-doc upgrade` | Update the tool itself | `--check`, `--yes` |
| `coop-data-doc help [CMD]` | Show help | — |

## Config File Discovery

`coop-data-doc` searches for `coop-data-doc.yml` in this order:

1. `--config` argument (explicit)
2. `./coop-data-doc.yml` (current directory)
3. Walk up parent directories until found (like `git` finding `.git`)
4. If not found, exit 1 with a message suggesting `coop-data-doc init`

## Output Artifacts (Machine-Readable)

After a successful `build`/`update`/`scan`, these files exist:

### `data-docs/manifest.json`

The entire lineage graph in one JSON file. **Preferred entry point for agents.**

Shape:
```json
{
  "nodes": {
    "view:salespm.dim_customer": {
      "node_type": "view",
      "schema_name": "salespm",
      "name": "dim_customer",
      "source_file": "views/salespm/dim_customer.sql",
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

## Page Front-Matter Contract

Every generated `.md` page starts with strict YAML:

```yaml
---
id: "view:salespm.dim_customer"
type: "view"
name: "dim_customer"
schema: "salespm"
source_file: "views/salespm/dim_customer.sql"
path: "view/salespm-dim_customer-<hash>.md"
upstream_inputs:
  - "gold_table:dbo.fact_sales"
downstream_dependents:
  - "pbi_table:salespm.dim_customer"
tags:
  - "salespm"
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
| `COOP_DATA_DOC_QUIET` | If set, equivalent to `-q` |

## Python API (Advanced)

```python
from coop_data_doc.config import Config
from coop_data_doc.cli import run_pipeline

config = Config.load("coop-data-doc.yml")
graph, result, warnings = run_pipeline(config, interactive=False)
# graph.nodes, graph.edges, result.resolved, result.unresolved
```

## Version

This contract matches `coop-data-doc` version `0.26.1`.
