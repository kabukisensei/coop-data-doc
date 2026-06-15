# Architecture

How `coop-data-doc` turns two git repos into lineage documentation. Read this
before changing code; `CONTRIBUTING.md` has the rules, this file has the *why*
and the *how*.

## The pipeline

```
coop-data-doc.yml ──► [crawler] ──► FileInventory (classified files)
                           │
             ┌─────────────┴───────────────┐
             ▼                             ▼
   [SQL parsers (M2)]            [Power BI parsers (M3)]
   sql_objects: CREATE           tmdl: tables/columns/measures/partitions
     TABLE/VIEW + columns        bim:  same, from model.bim JSON
   sql_procs: proc DML           mcode: partition M ──► SourceRef
     (INSERT/MERGE/UPDATE/       dax:  measure ──► measure/table refs
      SELECT INTO/EXEC)          pbir: report/page/visual + field bindings
                                       (PBIR folders AND legacy report.json)
                                 pbix: best-effort zip extraction
             │                             │
             ▼                             ▼
   resolve_stub_references()      link_visual_bindings()
             └──────────────┬───────────────┘
                 prune_schemas() — drop system/ignored schemas
                 assign_layers() — bronze/silver/gold (rules + heuristic)
                            │
                     [linker (M4)]
                cache → exact → config rule → fuzzy → interactive prompt
                answers persist to .lineage-cache.json (commit it)
                            │
                            ▼
                  LineageGraph  ── graph.json + diagnostics.json (artifacts)
                            │
             ┌──────────────┴──────────────┐
             ▼                             ▼
  [render/markdown (M5)]         [render/site (M5)]
  per-node .md + index.md        MkDocs Material, dark default,
  + manifest.json (for agents)   offline search + vendored mermaid
```

`cli.run_pipeline()` (`src/coop_data_doc/cli.py`) is the executable version of
this diagram — read it first when tracing behavior.

## The data model (everything flows through one graph)

`src/coop_data_doc/graph/model.py`:

- **Node** — `id`, `node_type`, `name`, `schema_name`, `source_file`,
  `columns`, free-form `metadata`. Ids are stable slugs:
  `"{type}:{schema}.{name}"`, lowercased, brackets stripped
  (`[dbo].[Fact Sales]` → `gold_table:dbo.fact sales`). For Power BI nodes,
  `schema_name` holds the normalized semantic-model name.
- **NodeType** — `silver_table, gold_table, view, stored_proc,
  semantic_model, pbi_table, measure, report, report_page, visual`.
- **Edge** — `source_id`, `target_id`, `edge_type`, `evidence`
  (a `"file: snippet"` string proving the edge — every edge is auditable).
- **Edge direction.** Edges are *authored* in the parser-natural direction,
  which is not always the data-flow direction. `Edge.flow()` normalizes:

  | edge_type | authored as | data flows |
  | --- | --- | --- |
  | `reads` | proc/view → table it reads | target → source |
  | `writes` | proc → table it writes | source → target |
  | `feeds` | view → pbi_table; pbi_table → model; visual → page → report | source → target |
  | `defines` | proc → table it CREATEs | source → target |
  | `references` | measure → measure/table; proc → proc (EXEC) | target → source |
  | `visualizes` | visual → pbi_table/measure | target → source |

  All traversal (`upstream()` / `downstream()`) uses `flow()`, so callers
  never think about authoring direction.

## Key design decisions

**Deterministic by construction.** Every iteration is sorted, ids are
normalized, serialization sorts keys and edges, and nothing embeds a
timestamp. Same inputs + same cache ⇒ byte-identical output
(`tests/test_determinism.py` enforces this). This is what makes
`coop-data-doc check` a valid CI freshness gate.

**Never guess lineage.** Anything not statically provable becomes a warning
or an `unresolved` marker, never an invented edge: dynamic SQL
(`sp_executesql`), opaque .pbix models, unrecognized M partitions.

**AST first, regex fallback second (SQL).** T-SQL proc bodies routinely
defeat sqlglot (cursors, WHILE, TRY/CATCH). `sql_procs` therefore processes
*statement by statement*: split the body on `;` (string/comment-aware
scanner), try sqlglot per chunk, and only for unparseable chunks apply
documented regex patterns — marking the proc
`metadata.parse_quality = "regex_fallback"` plus a warning so humans know to
eyeball it. Two sqlglot subtleties are handled centrally: temp tables lose
their `#` in the AST (flagged `temporary=True` instead — see
`is_temp_table`), and `UPDATE alias ... FROM table AS alias` needs alias
resolution for the write target.

**Layer assignment is a post-pass** (`layering.assign_layers`). Object *type*
comes from the SQL; the medallion *layer* (bronze/silver/gold) is assigned
from `config.layers` rules — by schema and/or source-path glob, precedence
gold → silver → bronze — with a read/write heuristic fallback (a table only
ever read → silver source; one created here → gold). `display_name` carries
the original-case name for rendering while ids stay normalized.
`prune_schemas` first drops system schemas (`sys`/`information_schema`/
`tempdb`/`db_*`) and any `ignore_schemas`, which would otherwise appear as
phantom nodes from catalog references.

**Name gaps are a first-class problem.** View schemas and semantic-model
names are similar but not identical (e.g. schema `sales` feeds the
"Sales Analytics" model). The linker ladder
(`linker/resolver.py`) goes: cache → exact id match → `schema_mappings`
config rule → fuzzy (`difflib`, auto-accept ≥ 0.92, prompt 0.60–0.92) →
interactive `questionary` prompt. Every interactive answer is written to
`.lineage-cache.json` immediately (crash-safe), so the second run asks
nothing.

**Two renderers, one graph.** `render/markdown.py` emits strict fixed-order
YAML front-matter (`id`, `type`, `name`, `schema`, `source_file`, `path`,
`upstream_inputs`, `downstream_dependents`, `tags`) so agents can parse pages
without heuristics; `manifest.json` is the whole serialized graph for
programmatic consumers. Page filenames come from `slug()` (filesystem-safe,
length-bounded, hash-suffixed for uniqueness — not derivable from the id), so
the `path` field is the source of truth for where a node's page lives. `render/site.py` synthesizes a Material config and
post-processes the built HTML so the portal works over `file://` with zero
network: vendored `mermaid.min.js` (Material skips its CDN fetch when
`window.mermaid` exists), vendored iframe-worker shim (URL rewritten in the
HTML), `font: false`, `use_directory_urls: false`.

**Human content survives regeneration.** Each page has a Business Intent
block between `<!-- intent:begin/end -->` markers; the renderer carries the
existing block forward verbatim. `check` copies the committed tree before
re-rendering for the same reason.

## For agents: answering questions from the output

- *"What breaks if I drop column X from view Y?"* — open the view's page
  (find it via the `path` field, not by computing a filename), read
  `downstream_dependents`, follow each page's front-matter transitively
  (or walk `manifest.json` edges with the
  flow table above).
- *"Where does this report number come from?"* — visual page →
  `visualizes` → measure (DAX shown on the measure page) → `references` →
  pbi_table → `feeds` → view → `reads` → gold table → `writes` ← proc →
  `reads` → silver sources.
- Trust levels: edges carry `evidence`; nodes parsed via fallback carry
  `metadata.parse_quality = "regex_fallback"`; DAX/measure edges are
  heuristic (`dax_refs_heuristic`).

## Repo layout

```
src/coop_data_doc/
├── cli.py            entrypoints + run_pipeline (the orchestration) + interactive menu
├── config.py         coop-data-doc.yml model (repos/layers/ignore_schemas) + ParseWarning
├── crawler.py        repo walk + FileKind classification
├── graph/            model.py (Node/Edge/LineageGraph, display_name), serialize.py
├── parsers/          sql_common/sql_objects/sql_procs, tmdl/bim/mcode/dax/pbir/pbix
├── layering.py       medallion layer assignment + system/ignored-schema pruning
├── linker/           resolver.py (ladder), cache.py, interactive.py
├── diagnostics.py    severity-classified warnings → console / JSON / HTML page
├── progress.py       stderr progress bars + spinner (TTY-only)
├── wizard.py         interactive `setup` (repos, layers, ignore, mappings)
├── upgrade.py        `upgrade` — the only networked command (PyPI/git)
├── render/           markdown.py, mermaid.py, site.py (layer-grouped nav)
└── templates/assets/ vendored mermaid + iframe-worker + custom.css
tasks/                original builder briefs — double as interface docs
tests/                fixtures/repo_sql + fixtures/repo_pbi drive everything
```
