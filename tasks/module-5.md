# MODULE 5 — Markdown & MkDocs HTML Generators ✅ IMPLEMENTED

> Status: **done** — kept as interface reference; do not reimplement.

**Files to create:** `src/coop_data_doc/render/markdown.py`, `render/mermaid.py`,
`render/site.py`, `src/coop_data_doc/templates/` assets, `tests/test_render.py`,
golden files under `tests/golden/`.

**Inputs you can rely on:** a finished, linked `LineageGraph`; `Config.output`
(`dir`, `site_dir`); `coop_data_doc.graph.serialize.to_json_str`.

## 1. `markdown.py` — agent-facing Markdown

```python
def render_markdown(graph: LineageGraph, out_dir: Path, project_name: str) -> list[Path]: ...
```

One file per node at `{out_dir}/{node_type}/{slug}.md`. **As built, `slug()` lives in
`render/mermaid.py`**: it strips every filesystem-illegal character (`< > : " / \ | ? *`
+ control chars), replaces `.`/spaces, bounds the readable part to 80 chars, and appends
`-<8-char sha1 of the id>` for guaranteed uniqueness — so the filename is *not* derivable
from the id by hand. The page's location is published in the `path` front-matter field.
Exact layout (every value double-quoted; `path` sits between `source_file` and
`upstream_inputs`):

```markdown
---
id: "view:sales.dim_customer"
type: "view"
name: "dim_customer"
schema: "sales"
source_file: "views/sales/dim_customer.sql"
path: "view/sales-dim_customer-<hash>.md"
upstream_inputs:
  - "gold_table:dbo.customer"
  - "silver_table:silver.crm_accounts"
downstream_dependents:
  - "pbi_table:sales analytics.dim_customer"
tags:
  - "sales"
---

# dim_customer `view`

## Structural Contract

| Column | Type | Constraints | Description |
| --- | --- | --- | --- |
| customer_id | INT | NOT NULL, PK | |

## Lineage

### Upstream

| Object | Type | Via | Evidence |
| --- | --- | --- | --- |
| [dbo.customer](../gold_table/dbo-customer-<hash>.md) | gold_table | reads | views/sales/dim_customer.sql |

### Downstream

(same table shape)

## Local Flow

```mermaid
<local flowchart, see mermaid.py>
```

## Business Intent

<!-- intent:begin -->
_Add a short description of what this object is for and who relies on it._
<!-- intent:end -->
```

Rules:
- **Front-matter is strict YAML, keys in EXACTLY this order** (`id`, `type`, `name`,
  `schema`, `source_file`, `path`, `upstream_inputs`, `downstream_dependents`, `tags`),
  all values double-quoted, lists sorted; `schema` key maps from `node.schema_name`;
  `path` = `{node_type}/{slug}.md`. `upstream_inputs`/`downstream_dependents` =
  `graph.upstream/downstream(id, depth=1)`. Output must be byte-stable.
- **Business Intent preservation**: before writing, if the target file exists, extract the text
  between `<!-- intent:begin -->`/`<!-- intent:end -->` and carry it forward verbatim. This makes
  regeneration non-destructive — test this explicitly.
- Empty column list → `_Columns not statically resolvable_` plus the metadata flag note.
- Also emit:
  - `{out_dir}/index.md` — project overview: counts by node type (table), unresolved-item list,
    and a global `flowchart LR` grouped with `subgraph` per layer (Silver / Gold & Procs /
    Views / Semantic Models / Reports). If total nodes > 150, replace the global chart with one
    chart per semantic model (its full upstream closure).
  - `{out_dir}/manifest.json` — `to_json_str(graph)`; the machine entrypoint for agents.
- Return the sorted list of written paths. No timestamps anywhere.

## 2. `mermaid.py`

```python
def local_flowchart(graph: LineageGraph, node_id: str, up_depth: int = 2, down_depth: int = 2) -> str: ...
def estate_flowchart(graph: LineageGraph) -> str: ...
```

- `flowchart LR`. Node shapes by type: tables `id1[("name")]`, views `id2[/"name"/]`,
  procs `id3{{"name"}}`, semantic models & pbi tables `id4(["name"])`, measures `id5>"name"]`,
  visuals/reports `id6["name"]`.
- Mermaid node ids: sequential `n0..nN` assigned over **sorted** graph ids (labels carry the
  real name) — avoids mermaid-hostile characters entirely; additionally escape `"` in labels.
- Edges drawn in data-flow direction (`Edge.flow()`), label = edge_type.
- `click nX "../{node_type}/{slug}.md"` lines for cross-linking (relative paths work in mkdocs).
- Current node highlighted: `style nX stroke-width:3px`.

## 3. `site.py`

```python
def write_mkdocs_config(out_dir: Path, project_name: str) -> Path: ...
def build_site(docs_dir: Path, site_dir: Path) -> None: ...   # runs `mkdocs build` via subprocess
```

Synthesized `mkdocs.yml` (emit as a literal template string, values substituted):
- `site_name: {project_name}`, `docs_dir`, `site_dir`, `use_directory_urls: false`
  (**required** for `file://` browsing).
- `theme: material` with two `palette` entries (toggle): default `scheme: slate` (dark) with
  `toggle.icon: material/weather-sunny`, alternate `scheme: default`; `features:`
  `[navigation.sections, navigation.indexes, navigation.top, search.suggest, search.highlight,
  content.code.copy]`.
- `plugins: [search, offline]` — `offline` is **bundled with mkdocs-material**; it makes search
  work over `file://`.
- Mermaid: `markdown_extensions:` → `pymdownx.superfences` with
  `custom_fences: [{name: mermaid, class: mermaid, format: !!python/name:pymdownx.superfences.fence_code_format}]`
  AND vendor `mermaid.min.js` into `{docs_dir}/assets/javascripts/` (ship it inside the package
  under `src/coop_data_doc/templates/assets/` and copy at render time — add a clearly marked
  placeholder download step in the README since the binary blob can't be authored by hand),
  referenced via `extra_javascript`. No CDN URLs anywhere.
- `nav:` generated from node types present, sorted: Overview → Stored Procedures → Tables
  (Gold) → Source Tables (Silver) → Views → Semantic Models → Measures → Reports.
- `build_site` runs `[sys.executable, "-m", "mkdocs", "build", "-f", config, "--strict"-less]`
  via `subprocess.run(capture_output=True)`; non-zero → raise `SiteBuildError` with stderr tail.

## Tests
- Golden-file: render the fixture graph (build it in-test from M0 primitives so this module is
  testable standalone) → byte-compare a representative view/proc/pbi_table/measure page and
  `index.md` against `tests/golden/`.
- Intent preservation: write docs, edit a Business Intent block, re-render, assert preserved.
- Determinism: render twice into two dirs → identical trees.
- Mermaid: snapshot test `local_flowchart` for a mid-chain node; assert click lines and
  highlight present.
- `write_mkdocs_config` output parses as YAML (except the `!!python/name:` tag — use a custom
  loader ignore or string assertion); `build_site` test marked `@pytest.mark.slow` (skipped if
  mkdocs unavailable) asserting `site/index.html` exists and contains no `http://`/`https://`
  asset references.

## Acceptance criteria
- Opening `site/index.html` via `file://` with network disabled: search works, dark toggle
  works, mermaid renders, internal links navigate.
- Front-matter parseable by `yaml.safe_load` with the exact key order specified.
- Re-running over an existing output dir never loses Business Intent content.
