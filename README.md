# coop-data-doc

[![PyPI version](https://img.shields.io/pypi/v/coop-data-doc.svg)](https://pypi.org/project/coop-data-doc/)
[![Python versions](https://img.shields.io/pypi/pyversions/coop-data-doc.svg)](https://pypi.org/project/coop-data-doc/)
[![CI](https://github.com/kabukisensei/coop-data-doc/actions/workflows/ci.yml/badge.svg)](https://github.com/kabukisensei/coop-data-doc/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://opensource.org/licenses/MIT)

> **Install:** `pipx install coop-data-doc` — see [Install](#install) for the one-time pipx setup.

**Automatic documentation for your data estate.** Point this tool at two git repos — your
SQL repo (stored procedures, tables, views) and your Power BI repo (semantic models and
reports) — and it maps how data flows through everything:

```
silver table → stored proc → gold table → view → semantic model → measure → report visual
```

…then writes two kinds of documentation from that map:

- 📄 **Markdown files** with machine-readable headers — built for AI agents and scripts
  (see [For AI agents](#-for-ai-agents) below)
- 🌐 **A searchable website** with dark mode and clickable lineage diagrams — built for
  humans. It works straight off your hard drive: no server, no internet, no login.

Everything runs on your machine by reading files. It never connects to a database, never
calls an AI service, and produces byte-identical output for identical inputs — so the
generated docs can live in git and be checked in CI.

---

## Table of contents

1. [Before you start](#before-you-start)
2. [Install](#install)
3. [First run — about 5 minutes](#first-run--about-5-minutes)
4. [Day-to-day use](#day-to-day-use)
5. [The config file, explained](#the-config-file-explained)
6. [When it asks you questions](#when-it-asks-you-questions)
7. [Adding your own notes to the docs](#adding-your-own-notes-to-the-docs)
8. [Command reference](#command-reference)
9. [Keeping the tool updated](#keeping-the-tool-updated)
10. [Using it in CI](#using-it-in-ci)
11. [🤖 For AI agents](#-for-ai-agents)
12. [Troubleshooting](#troubleshooting)
13. [Notes on .pbix files](#notes-on-pbix-files)

---

## Before you start

You'll need three things:

1. **A terminal.** That's the text window where you type commands.
   - **macOS:** press `Cmd+Space`, type `Terminal`, press Enter.
   - **Windows:** press the Windows key, type `PowerShell`, press Enter.
   - **Linux:** any terminal; the macOS commands apply as-is.
2. **Python 3.10 or newer.** Check by typing `python3 --version` (macOS) or
   `python --version` (Windows) and pressing Enter. If that prints something like
   `Python 3.12.4`, you're set. Otherwise install Python from
   [python.org](https://www.python.org/downloads/) first.
3. **The two repos on your machine** — your SQL repo and your Power BI repo.
   ("Repo" = a folder managed by git; "cloned" = downloaded to your machine with git or
   GitHub Desktop.) You'll need their folder paths — to get a path, drag the folder onto
   the terminal window (macOS), or right-click it in File Explorer and choose
   **Copy as path** (Windows).

> 💡 **Terminal survival kit:** you type a command, press Enter, and read what comes
> back. `cd some/folder` moves you into a folder. Paste with `Cmd+V` (macOS) or
> right-click (Windows). In menus: ↑↓ to move, Enter to select, `Ctrl+C` to safely
> cancel.
>
> ⚠️ **Windows users:** wherever this README shows `python3`, type `python` instead —
> that applies to every command below.

## Install

**Use pipx, not `pip install`.** pipx puts the tool in its own isolated environment, so
it can never upgrade or downgrade packages that other tools (e.g. `ms-fabric-cli`,
`azure-cli`) depend on. Installing into your system Python with plain `pip` works but
will fight those tools over shared dependencies like `pyyaml` — see
[Troubleshooting](#troubleshooting) if you've already hit that. One-time pipx setup:

```bash
python3 -m pip install --user pipx      # Windows: python -m pip install --user pipx
python3 -m pipx ensurepath              # Windows: python -m pipx ensurepath
```

Close and reopen your terminal, then install the tool from PyPI:

```bash
pipx install coop-data-doc
```

That's it. (Advanced — install a specific unreleased commit straight from the source
repo instead: `pipx install git+https://github.com/kabukisensei/coop-data-doc.git`.)

Check it worked:

```bash
coop-data-doc --version
```

You should see `coop-data-doc, version 0.17.0` (or newer). If the terminal says
*"command not found"* (macOS) or *"the term 'coop-data-doc' is not recognized…"*
(Windows), see [Troubleshooting](#troubleshooting).

<details>
<summary>Other ways to install (click to expand)</summary>

```bash
uv tool install /path/to/coop-data-doc     # if you use uv
pip install /path/to/coop-data-doc         # plain pip, into the current environment
pip install -e "/path/to/coop-data-doc[dev]"   # contributors: editable + test deps
```
</details>

## First run — about 5 minutes

**Step 1 — make a home for the docs.** Create a folder for the generated documentation
**next to your two repos** (that makes the wizard's suggested paths like `../sql-repo`
correct). For example, if your repos live in `~/repos` (macOS) or `C:\repos` (Windows):

```bash
cd ~/repos          # Windows: cd C:\repos
mkdir my-data-docs
cd my-data-docs
```

**Step 2 — run the tool with no arguments:**

```bash
coop-data-doc
```

Because there's no configuration here yet, it offers to walk you through setup. Choose
**"Set up interactively"** (↑↓ + Enter) and answer the questions:

- **Project name** — the title shown on the docs website.
- **SQL repo path** and **Power BI repo path** — type or paste the folder paths; the
  wizard checks they exist and lets you re-type a typo.
- **Which folders to document** — once a repo path is set, the wizard lists that repo's
  top-level folders as a checkbox (everything starts checked). Press **Space** to uncheck
  any folder you want to skip — backups, deployment scripts, archives — and **Enter** to
  confirm. No need to type out skip patterns by hand. (If the repo isn't on disk yet, it
  falls back to asking for skip globs as text.) Each unchecked folder becomes a
  `**/Name/**` entry under `repos.<key>.exclude`; nested skip patterns you've added by
  hand are kept as-is on a re-run.
- **Output folders** — press Enter to accept the defaults.
- **"Add a view-schema → semantic-model mapping?"** — this is a hint like *"the
  `sales` schema feeds the Sales Analytics model"*. If you know one,
  add it; **if you're not sure, answer No (type `n`)** — you can always add hints
  later, and the tool will simply ask you about specific tables during the build.

**Step 3 — build the docs.** Run the bare command again and choose
**"Update the docs"** — or run `coop-data-doc update`. (If the tool suggests
`coop-data-doc build`: `build` and `update` are the same command.) You'll see a
summary like:

```
Warnings:
  dynamic_sql                    1
  ...
26 objects, 30 lineage edges (2 cross-repo links; 1 unresolved)
Markdown docs: /Users/you/repos/my-data-docs/data-docs
HTML portal:   file:///Users/you/repos/my-data-docs/data-docs-site/index.html
```

A **Warnings** block is normal and informational — the build succeeded if you see the
object count and the two output paths. (Each warning category is explained in
[Troubleshooting](#troubleshooting).) The first run may also ask a few **mapping
questions** — see [When it asks you questions](#when-it-asks-you-questions).

**Step 4 — open the website.** Copy the `file://…` line into your browser's address
bar — or:

```bash
open data-docs-site/index.html        # macOS
start data-docs-site\index.html       # Windows
xdg-open data-docs-site/index.html    # Linux
```

You'll get a searchable site with a page per table, view, stored procedure, semantic
model, measure, and report — each with its columns, where its data comes from, what
depends on it, and a clickable flowchart.

**Step 5 (recommended) — commit everything.** One command at a time:

```bash
git init
git add -A
git commit -m "Initial data docs"
```

> If this is your first-ever `git commit` on this machine, git may ask you to
> introduce yourself first:
> `git config --global user.name "Your Name"` and
> `git config --global user.email "you@example.com"` — then re-run the commit.

## Day-to-day use

When SQL or Power BI changes land in your repos, refresh the docs:

```bash
cd my-data-docs
coop-data-doc            # choose "Update the docs"  — or —
coop-data-doc update     # the same thing, no menu
```

Pages for new objects appear, changed objects update, and pages for deleted objects
are removed. Notes you've written in [Business Intent](#adding-your-own-notes-to-the-docs)
blocks are preserved. Re-running is always safe.

## The config file, explained

Setup writes a single file, `coop-data-doc.yml`, which you can edit by hand anytime
(or re-run `coop-data-doc setup` — it pre-fills your current answers so you can change
just one thing):

```yaml
project_name: Coop BI Estate        # the title shown on the docs website

repos:
  sql:                              # your SQL repo
    path: ../sql-repo               # relative paths start from THIS file's folder
    include: ["**/*.sql"]           # which files to read
    exclude: ["**/archive/**"]      # which files to skip (wins over include)
  powerbi:                          # your Power BI repo (PBIP/TMDL + PBIR)
    path: ../pbi-repo
    include: ["**/*.tmdl", "**/*.bim", "**/report.json", "**/visual.json", "**/page.json", "**/*.pbix"]
    exclude: []

schema_mappings:                    # hint: which view schema feeds which model
  - schema: sales
    model: "Sales Analytics"

layers:                             # medallion layers (all optional)
  bronze:
    schemas: [erp_orders, erp_finance]   # source schemas
    paths: []
  silver:
    schemas: [stg]
    paths: []
  gold:
    schemas: [mart, common]         # the proc schema + shared/common schema
    paths: ["**/dim/**", "**/fact/**"]   # gold table folders

ignore_schemas: [staging, scratch]  # schemas to drop entirely (never documented)

output:
  dir: ./data-docs                  # the markdown (agents read this)
  site_dir: ./data-docs-site        # the website (humans read this) — must be a
                                    # SEPARATE folder, not inside dir (see note below)

sql_dialect: tsql                   # covers SQL Server, Azure SQL, Fabric warehouse
```

`schema_mappings` matters because view schemas and semantic-model names are often
*similar but not identical* — e.g. the `sales` schema feeds the "Sales Analytics"
model. Each hint you add means fewer questions on the next run.

### Medallion layers (bronze / silver / gold)

The object *type* (table / view / stored proc) is detected automatically from the SQL.
The *layer* can't be — a `CREATE TABLE` doesn't say "I'm silver" — so you declare it.
A table or view is assigned the **first** layer (precedence gold → silver → bronze) whose:

- `schemas` list contains its schema, **or**
- `paths` globs match its file.

In a Fabric or SQL warehouse the folder layout is usually `<Warehouse>/<schema>/<ObjectType>/`,
so **the schema *is* the folder** — listing `schemas` is all you need, and you can ignore
`paths` entirely. `paths` exists only for the less-common case where a layer maps to a
*folder* that isn't its own schema (say a `dim/`/`fact/` convention living under another
schema). You can mix both — a node is assigned the first layer (gold → silver → bronze) hit
by **either** its schema or its path — but most estates only ever fill in `schemas`. The
setup wizard reflects this: it asks for schemas layer-by-layer and only asks about folders
if you opt into the "advanced" question.

Each layer is optional — **omit `bronze` or `silver` entirely to skip it.** Anything no rule
matches falls back to a read/write heuristic (a table that's only ever read → silver; one
that's created here → gold), and the scan warns which objects fell back so you can add rules.
Bronze only ever appears when you declare it.

**Dropping schemas you don't want documented:** list them in `ignore_schemas` (the wizard
asks for these too). System schemas — `sys`, `information_schema`, `tempdb`, `db_*` — are
**always** dropped automatically, since they're catalog references, not real data. Note
that ignoring a schema removes it from lineage entirely, so anything downstream loses that
upstream link.

### Full configuration reference

| Key | Type | What it does |
| --- | --- | --- |
| `project_name` | string | Title shown on the docs site. |
| `repos.<key>.path` | string | Folder to crawl, relative to the config file. The key (`sql`, `powerbi`, …) is just a label; add as many repos as you like. |
| `repos.<key>.include` | list of globs | Only files matching these are read. |
| `repos.<key>.exclude` | list of globs | Files matching these are skipped (**exclude wins** over include). |
| `schema_mappings` | list of `{schema, model}` | Hints linking a SQL view schema to the semantic model it feeds, for cases the names don't match. Often unnecessary — if the Power BI partition's schema equals the SQL schema, it matches automatically. |
| `layers.<bronze\|silver\|gold>.schemas` | list | Schemas assigned to that layer. |
| `layers.<bronze\|silver\|gold>.paths` | list of globs | File paths assigned to that layer. A node matches the first layer (gold → silver → bronze) hit by schema **or** path. |
| `ignore_schemas` | list | Schemas dropped entirely. System schemas are always dropped on top of these. |
| `output.dir` | string | Where the Markdown (agent docs) is written. |
| `output.site_dir` | string | Where the HTML site is built. **Must be a separate folder from `output.dir`** — not the same folder and not nested inside it (each build wipes `site_dir`, which would clobber your Markdown). Side-by-side like `./data-docs` + `./data-docs-site` is the convention. |
| `sql_dialect` | string | sqlglot dialect for the SQL repo (`tsql` covers SQL Server / Azure SQL / Fabric warehouse). |

### include / exclude — choosing what gets crawled

Patterns are matched against each file's path **relative to its repo**, forward-slashed:
`**/Foo/**` = anything under a folder named `Foo`; `**/*.sql` = any `.sql` anywhere;
`SomeDir/**` = everything under `SomeDir`. Two strategies:

- **Allowlist (cleanest for big repos):** include only real object folders, e.g.
  `include: ["**/Tables/*.sql", "**/Views/*.sql", "**/StoredProcedures/*.sql", "**/Functions/*.sql"]`
  — everything else (deployment scripts, role grants, notebooks) is ignored automatically.
- **Denylist:** keep `include: ["**/*.sql"]` and drop noise with
  `exclude: ["**/logging/**", "**/Deployment/**", "**/BACKUP/**"]`.

Use `coop-data-doc scan` (fast, no rendering) as the feedback loop: watch the object count
and the diagnostics summary, adjust, repeat. **Reports need to be present in PBIR format**
(a committed `.Report/definition/pages/.../visual.json` tree) or legacy `report.json` — the
default Power BI `include` already matches them, so once they're in the repo they're picked
up with no config change.

### Worked example: a large multi-schema warehouse

A config for a Fabric warehouse + Power BI estate with medallion schemas, ERP source
schemas, model-named gold schemas, a `common` schema feeding every model, and editor/backup
noise to drop:

```yaml
project_name: Acme BI Estate
repos:
  sql:
    path: ../fabric-dw
    include: ["**/*.sql"]
    exclude: ["**/logging/**", "**/Security/**", "**/Deployment/**"]
  powerbi:
    path: ../fabric
    include: ["**/*.tmdl", "**/*.bim", "**/visual.json", "**/page.json", "**/report.json"]
    exclude: ["**/BACKUP/**", "**/Documentation/**", "**/Editor and Theme Files/**"]
layers:
  bronze:
    schemas: [dbo]                    # lakehouse landing tables
  silver:
    schemas: [erp_orders, erp_finance]   # ERP source schemas
  gold:
    schemas: [silver, mart, common, sales, ops]  # 'silver' schema is gold here!
    paths: ["**/dim/**", "**/fact/**"]
ignore_schemas: [staging, scratch, sandbox, legacy]
```

The standout: a schema *named* `silver` can sit in the **gold** layer — assignment follows
your rule, not the schema's name.

## When it asks you questions

When a Power BI table's source can't be matched to a SQL object automatically, the tool
shows a pick-list: the most likely candidates with similarity scores, plus *"Mark as
external source"* (for data that doesn't live in these repos) and *"Skip for now"*.

Every answer — including skips — is saved instantly to **`.lineage-cache.json`**, which
lives **next to `coop-data-doc.yml`** (it's a hidden file; `git add -A` picks it up even
if Finder/Explorer doesn't show it). **Commit that file.** It's what makes every later
run — yours, a coworker's, CI's — fully automatic.

Two things worth knowing:

- If you cancel mid-way (`Ctrl+C`), the answers you already gave are kept; run again to
  continue from where you stopped.
- *"Skip for now"* is remembered too, so you won't be re-asked on the next run. To be
  asked again (or change any answer), open `.lineage-cache.json` in a text editor and
  delete that entry, then re-run.

## Adding your own notes to the docs

Every generated page has a **Business Intent** section between two marker comments:

```markdown
## Business Intent

<!-- intent:begin -->
Write anything here: what this table is for, who owns it, gotchas.
<!-- intent:end -->
```

Text between the markers survives every rebuild verbatim. Everything *outside* the
markers is regenerated, so put your notes inside them.

## Command reference

| Command | What it does |
| --- | --- |
| `coop-data-doc` | interactive menu (in scripts/CI it prints help instead) |
| `coop-data-doc status` | **show project state** — config found? docs built? stale? |
| `coop-data-doc setup [PATH]` | guided wizard — create or update the config (prefills current values) |
| `coop-data-doc init [PATH] [--force]` | write a commented starter config to edit by hand |
| `coop-data-doc update` | re-scan the repos and refresh all documentation |
| `coop-data-doc build` | identical to `update` — two names for the same command |
| `coop-data-doc scan` | crawl + parse + link only; writes `graph.json`, no rendering |
| `coop-data-doc check [--lenient]` | CI gate — fails on stale docs, unresolved references, or risky parses (`--lenient` tolerates the latter) |
| `coop-data-doc upgrade [--check] [--yes]` | update the **tool itself** + dependency updates |
| `coop-data-doc help [command]` | show help (same as `--help`) |

**Config discovery:** `coop-data-doc` searches for `coop-data-doc.yml` in the current directory and walks up parent directories (like `git` finding `.git`). You can override with `--config PATH` or the `COOP_DATA_DOC_CONFIG` environment variable.

Options for `build`/`update`: `--skip-html` (markdown only), `--serve` (live-preview
 the site). `scan`/`build`/`update` all accept `--non-interactive` (never prompt; for
 CI) and `--strict` (exit code 2 on unresolved references or risky parses). Every
 pipeline command accepts `--config PATH` (default: discover in cwd and parents). Global flags
 go *before* the subcommand: `--version`, `-v` (debug + tracebacks), `-q` (quiet) —
 e.g. `coop-data-doc -q update`.

`scan`/`build`/`update` show progress bars on stderr while they work, but only in an
interactive terminal — they're suppressed by `-q` and absent in CI or piped output, and
they never affect the generated files.

## Keeping the tool updated

```bash
coop-data-doc upgrade --check    # see what's available, change nothing
coop-data-doc upgrade            # apply (asks for confirmation first)
```

After upgrading, `coop-data-doc --version` should report the new version. If it still
shows the old number even though you expected changes, force a clean re-pull:
`pipx reinstall coop-data-doc`.

**On Windows**, the running tool can't replace its own launcher (`coop-data-doc.exe`) —
Windows locks a running executable — so `coop-data-doc upgrade` will tell you to finish
the upgrade from a **new terminal**:

```powershell
pipx upgrade coop-data-doc      # run in a fresh window, where the tool isn't running
```

(On older versions this surfaced as a raw `[WinError 32] … being used by another process`
error — same cause, same fix. See [Troubleshooting](#troubleshooting).)

`upgrade` detects how the tool was installed (pipx / uv / pip / a git checkout) and
updates it — a git checkout gets new commits pulled and reinstalled. For pip and
git-checkout installs it also updates the tool's direct dependencies, **but only within
the same major version**; major-version jumps are listed for a human to review and never
applied automatically. (pipx and uv manage their own isolated environments, so for those
installs `upgrade` delegates dependency handling to them.) This is the single command
that uses the internet; documentation builds are always fully offline.

## Using it in CI

Two useful gates for a pipeline (e.g. GitHub Actions / Azure DevOps):

```bash
coop-data-doc check              # fails if committed docs are stale,
                                 # references are unresolved, or risky
                                 # parses exist (use --lenient to tolerate
                                 # known dynamic-SQL/cursor procs)

coop-data-doc build --non-interactive --strict   # rebuild; exit 2 on problems
```

Exit codes: `0` success · `1` stale docs / friendly error · `2` unresolved references,
risky parses, or an invalid command line (typo'd flag/command) · `130` cancelled with
Ctrl+C.

## 🤖 For AI agents

> **For the full machine-readable contract, see `AGENTS.md`.** It covers the JSON schema, CLI flags, exit codes, config discovery, and Python API.

The Markdown output (`output.dir`, default `data-docs/`) is designed to be read by LLM agents without custom tooling. Here's the quick summary:

**Entry points**

- `data-docs/manifest.json` — the entire lineage graph in one JSON file. Best for
  programmatic traversal and impact analysis. (`data-docs/graph.json` is a byte-identical
  copy written by every pipeline run; read `manifest.json`.)
- `data-docs/<type>/<slug>.md` — one page per object (its exact location is the `path`
  field in each page's front-matter — see **Page paths** below). Best for reading
  context about a specific object. `data-docs/index.md` lists object counts and
  unresolved items.

**Identifiers.** Node ids are stable, lowercase strings: `"<type>:<schema>.<name>"` —
e.g. `view:sales.dim_customer`. Caveats: the `<schema>.` part is **omitted** for
objects that have no schema (`report:sales`, `semantic_model:sales`), and names may
contain spaces (`measure:sales.total sales`). Prefer reading the explicit
`name`/`schema` fields over parsing ids.

**Page paths.** Don't compute these — **read the `path` field** in a node's
front-matter (and resolve ids to pages the same way). The on-disk slug is sanitised for
cross-platform safety (Windows-illegal characters removed) and carries a short hash
suffix to guarantee uniqueness, so it is *not* trivially derivable from the id. Every
page is at `data-docs/<path>`.

**Page front-matter** — strict YAML, fixed key order, all strings double-quoted,
non-empty lists in block style (empty lists render as `[]`):

```yaml
---
id: "view:sales.dim_customer"
type: "view"                              # silver_table | gold_table | view | stored_proc |
                                          # semantic_model | pbi_table | measure | report |
                                          # report_page | visual
name: "dim_customer"
schema: "sales"                         # SQL schema; for pbi_table/measure nodes it's the
                                          # (lowercased) model name; for report_page/visual
                                          # it's the report name; "" for report/semantic_model
source_file: "views/sales/dim_customer.sql"   # repo-relative; cite this as evidence
path: "view/sales-dim_customer-<hash>.md"     # this page's location under data-docs/ (read it, don't compute it)
upstream_inputs:                          # direct (depth-1) data sources, flow-normalized
  - "gold_table:dbo.fact_sales"
downstream_dependents:                    # direct (depth-1) consumers
  - "pbi_table:sales.dim_customer"
tags:
  - "sales"
---
```

To go from an id (e.g. one listed in `upstream_inputs`) to its page, open that node's
page via its own `path` field — or scan `manifest.json` for the node and read its data
directly. Avoid string-building the filename.

**Manifest shape.** `manifest.json` has `nodes` (object keyed by id) and `edges`
(list). Node fields use the internal names `node_type` and `schema_name` (the
front-matter keys `type`/`schema` are renderer aliases for them), plus `name`,
`source_file`, `columns`, and `metadata`. Edge fields: `source_id`, `target_id`,
`edge_type`, `evidence` — edges carry **no metadata**; trust markers live on nodes.

**Traversal rules**

- *"What breaks if X changes?"* → follow `downstream_dependents` page to page.
  *"Where does this number come from?"* → follow `upstream_inputs`.
- In `manifest.json`, edges are stored in authoring direction; convert to data-flow
  direction with this rule: for `edge_type` ∈ {`reads`, `references`, `visualizes`}
  data flows **target → source**; for {`writes`, `feeds`, `defines`} data flows
  **source → target**. (Front-matter lists are already flow-normalized — prefer them
  when reading pages.)
- Column contracts (name, type, nullability, constraints) are in each page's
  **Structural Contract** table; measure DAX is on measure pages under **DAX**.

**Trust markers** — these live in `nodes[<id>].metadata` in `manifest.json` (check the
**endpoint nodes** of an edge; pages don't carry them, and an empty `upstream_inputs`
on a page does *not* mean the object was verified to have no sources):

| marker (on the node) | meaning |
| --- | --- |
| `parse_quality: "regex_fallback"` | lineage came from pattern-matching, not a full parse — verify before high-stakes use |
| `dynamic_sql_untraced: true` | this proc builds SQL in strings; some of its real reads/writes are **knowingly missing** |
| `unresolved: true` / `partition_source_unresolved: true` | a human hasn't mapped this source yet — lineage incomplete |
| `skipped: true` | a human chose "skip for now" — same caution as unresolved |
| `external_source: true` | deliberately marked as living outside these repos — upstream ends here by design |
| `columns_unresolved: true` | column list couldn't be derived (e.g. `SELECT *`) |
| `pbix_model_opaque: true` | a .pbix model couldn't be extracted; lineage behind it is missing |
| `dax_refs_heuristic: true` | present on **every** measure — all DAX dependency extraction is heuristic. The discriminating signal is `unmatched_dax_refs` (bracket references that matched nothing) |

**Editing**: agents may write inside `<!-- intent:begin -->…<!-- intent:end -->` blocks
(those survive rebuilds). Never edit generated content outside the markers — it's
overwritten on the next `update`. To regenerate after source changes, run
`coop-data-doc update --non-interactive` and check the exit code.

> Working on this tool's own source code instead? Read `CLAUDE.md` and
> `ARCHITECTURE.md` in the repo root.

## Troubleshooting

| Symptom | What it means / what to do |
| --- | --- |
| `command not found: coop-data-doc` (macOS) or `the term 'coop-data-doc' is not recognized…` (Windows) | The install location isn't on your PATH. Run `python3 -m pipx ensurepath` (Windows: `python -m pipx ensurepath`), then close and reopen the terminal. |
| `externally-managed-environment` during install (macOS) | Your Python is managed by Homebrew. Run `brew install pipx`, then `pipx ensurepath`, and retry. |
| `coop-data-doc upgrade` fails on Windows with `[WinError 32] … being used by another process` (a `PermissionError` or `OSError` naming `coop-data-doc.exe`) | Windows can't replace the tool's launcher while it's running. The package may have already updated — check `coop-data-doc --version`. If it's still the old version, run `pipx upgrade coop-data-doc` in a **fresh** terminal (where the tool isn't running). v0.17.0+ detects this and prints the exact command instead of the raw error. |
| `dependency conflicts … requires pyyaml==6.0.2, but you have 6.0.3` (or similar) | You installed into a shared system Python with plain `pip`, clashing with another tool. Fix: `pip uninstall -y coop-data-doc`, restore the other tool's pin (e.g. `pip install "pyyaml==6.0.2"`), then reinstall coop-data-doc with **pipx** (isolated): `pipx install coop-data-doc`. |
| `Config file not found` | You're in the wrong folder. `cd` to the folder containing `coop-data-doc.yml`, or pass `--config path/to/coop-data-doc.yml`. |
| `Repo 'sql' path does not exist` | The path in `coop-data-doc.yml` is wrong. Re-run `coop-data-doc setup` and fix it. |
| `output.dir and output.site_dir must be separate folders` / mkdocs `'site_dir' should not be within the 'docs_dir'` | Your HTML folder is the same as — or inside — your Markdown folder. Point `output.site_dir` at a sibling (e.g. `dir: ./data-docs`, `site_dir: ./data-docs-site`), or re-run `coop-data-doc setup` and accept the suggested sibling. |
| `dynamic_sql` warning | A stored proc builds SQL inside strings; lineage can't be traced safely so the tool refuses to guess. Document that proc by hand in its Business Intent block. |
| `regex_fallback` warning | A statement was too gnarly for full parsing; its lineage came from pattern-matching. Usually right — worth a quick eyeball. |
| `unresolved_partition_source` warning | A Power BI table loads from something unrecognized. Run interactively once and map it, or mark it external. |
| `fuzzy_auto` warning | Two names were close enough to auto-match — listed so you can spot a wrong guess. |
| `check` exits 1 | Committed docs are out of date — run `coop-data-doc update` and commit. |
| `check` exits 2 | Unresolved references or risky parses. Resolve interactively, or use `check --lenient` if the risky parses are known and accepted. |
| Diagrams or search don't work in the browser | Make sure you opened `data-docs-site/index.html` (the built site), not a file in `data-docs/`. |
| Want to change a saved mapping answer | Edit `.lineage-cache.json` (next to your config): delete the entry and re-run. |

## Notes on .pbix files

`.pbix` support is best-effort: report layout and Power Query (M) source usually
extract; the compiled data model does not. For full lineage, open the file in Power BI
Desktop and **save as a .pbip project** — the git-friendly format these repos should
hold anyway. The tool tells you when it hits an opaque model.

## Third-party assets

The package vendors `mermaid.min.js` 11.15.0 and `iframe-worker` 1.0.4 (both MIT) so
generated sites render diagrams and search over `file://` with no network. See
`src/coop_data_doc/templates/assets/README.md` for provenance.

## Development

```bash
pip install -e ".[dev]"
pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the module map and design rules, and
[ARCHITECTURE.md](ARCHITECTURE.md) for how it all works.

## License

MIT — see [LICENSE](LICENSE).
