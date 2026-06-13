# coop-data-doc

Offline, deterministic data-lineage documentation for Microsoft BI estates.

Point it at two git repos — your SQL repo (stored procedures, tables, views) and your Power BI
repo (PBIP/TMDL semantic models, PBIR/legacy reports) — and it maps the full chain:

```
silver table → stored proc → gold table → view → semantic model table → measure → report visual
```

then renders two doc sets from one lineage graph:

- **Markdown with strict YAML front-matter** — designed for LLM agents to reason over
  (`manifest.json` is the machine entrypoint; every page declares `upstream_inputs` /
  `downstream_dependents` and its column contract)
- **A searchable, dark-mode HTML portal** — MkDocs Material, works completely offline over
  `file://` (search, Mermaid lineage flowcharts, fonts: all local, zero CDN)

No database connections, no servers, no LLM calls at runtime — pure AST/regex parsing
(`sqlglot` for T-SQL / Fabric warehouse SQL).

## Installation

Not yet published to PyPI. Until then, install from a git checkout (or a git URL):

```bash
# from a clone
pipx install /path/to/coop-data-doc          # or: uv tool install /path/to/coop-data-doc
# or straight from your git host
pipx install git+https://github.com/<org>/coop-data-doc.git
# or for development
pip install -e ".[dev]"
```

Once published (`git tag v0.1.0 && git push --tags` triggers the included
trusted-publishing workflow), it becomes:

```bash
pipx install coop-data-doc        # or: uv tool install coop-data-doc
```

## Quickstart

```bash
cd your-docs-folder
coop-data-doc                     # bare command = interactive menu: it walks you through
                                  # setup the first time, then offers "update the docs"
open data-docs-site/index.html
```

Or as direct commands: `coop-data-doc setup` then `coop-data-doc update`. Prefer editing
a file by hand? `coop-data-doc init` writes a commented starter config instead. Re-run
`coop-data-doc setup` anytime — it prefills your current values so you can change just
one thing.

## How it works

```
SQL repo ─┐                                          ┌─► data-docs/*.md   (for agents)
          ├─► crawl ─► parse (sqlglot AST + TMDL/M/  ├─► manifest.json    (machine entrypoint)
PBI repo ─┘           DAX/PBIR parsers) ─► link ─────┴─► data-docs-site/  (for humans)
                      (cache → exact → config rule → fuzzy → ask once)
```

Full design documentation: [ARCHITECTURE.md](ARCHITECTURE.md).

## The two-repo setup

```yaml
# coop-data-doc.yml
repos:
  sql:
    path: ../sql-repo             # procs, tables, views
    include: ["**/*.sql"]
    exclude: ["**/archive/**"]
  powerbi:
    path: ../pbi-repo             # PBIP semantic models + PBIR reports
    include: ["**/*.tmdl", "**/*.bim", "**/report.json", "**/visual.json", "**/page.json", "**/*.pbix"]
schema_mappings:                  # view schema -> the semantic model it feeds
  - schema: salespm
    model: "Sales and Project Management"
```

View schemas and semantic-model names are often *similar but not identical* —
`schema_mappings` handles the bulk, and anything still ambiguous triggers a one-time
interactive prompt. Answers are saved to **`.lineage-cache.json` — commit it!** —
so every later run (including CI) is fully automated.

## Commands

Running bare **`coop-data-doc`** in a terminal starts an interactive menu — it detects
whether a config exists and offers setup or update/scan/check accordingly. Everything is
also available as a direct command:

| Command | What it does |
| --- | --- |
| `coop-data-doc` | interactive menu (in scripts/CI it prints help instead) |
| `coop-data-doc setup [PATH]` | interactive wizard — create or update the config (prefills current values) |
| `coop-data-doc init [PATH] [--force]` | scaffold a commented config to edit by hand |
| `coop-data-doc update` | re-scan the repos and refresh all documentation |
| `coop-data-doc build` | same as `update` (`--skip-html`, `--serve` for live preview) |
| `coop-data-doc scan` | crawl + parse + link only; writes `graph.json` and a warning summary |
| `coop-data-doc check [--lenient]` | CI gate: fails on stale docs, unresolved references, or risky parses (`--lenient` tolerates the latter) |
| `coop-data-doc upgrade [--check] [--yes]` | update the **tool itself** + non-breaking dependency updates |
| `coop-data-doc help [command]` | show help (same as `--help`) |

Options: `scan`/`build`/`update` accept `--non-interactive` (never prompt; CI mode) and
`--strict` (exit 2 on unresolved refs or risky parses). Every pipeline command accepts
`--config PATH` (default `./coop-data-doc.yml`). Global: `--version`, `-v` (debug +
tracebacks), `-q` (suppress warning summaries) — global flags go *before* the
subcommand, e.g. `coop-data-doc -q build`.

### Keeping the tool updated

`coop-data-doc upgrade` is the one command that uses the network. It detects how the
tool was installed (pipx / uv tool / pip / a git checkout), updates it — for a git
checkout it pulls new commits and reinstalls — and applies dependency updates **within
the same major version only**. Major-version dependency jumps are reported but never
auto-applied, so nothing breaking lands without a human reviewing it.
`upgrade --check` reports without changing anything; `upgrade --yes` applies without
prompting (for scheduled jobs).

## Editing the docs

Each generated page has a **Business Intent** section between
`<!-- intent:begin -->` / `<!-- intent:end -->` markers. Write whatever you want there —
it survives regeneration verbatim. Everything else is overwritten on each build.

## .pbix files

`.pbix` support is best-effort: the report layout and Power Query (M) source usually
extract; the compiled model does not. For full lineage, open the file in Power BI Desktop
and **save as a .pbip project** (which is the git-friendly format these repos should hold
anyway). The tool tells you when it hits an opaque model.

## Troubleshooting

| Symptom | Meaning / fix |
| --- | --- |
| `dynamic_sql` warning | a proc builds SQL in strings; lineage is never guessed — document it manually in Business Intent |
| `regex_fallback` warning | sqlglot couldn't fully parse a statement; edges came from pattern matching — worth eyeballing |
| `unresolved_partition_source` | a partition's M code wasn't recognized; map it interactively or mark external |
| `check` exits 1 | committed docs are stale — rerun `coop-data-doc build` |
| `check`/`--strict` exits 2 | unresolved references or risky parses present |

## Third-party assets

The wheel vendors `mermaid.min.js` 11.15.0 and `iframe-worker` 1.0.4 (both MIT) so
generated sites render diagrams and search over `file://` with no network. See
`src/coop_data_doc/templates/assets/README.md` for provenance.

## Development

```bash
pip install -e ".[dev]"
pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the module map and design rules.
