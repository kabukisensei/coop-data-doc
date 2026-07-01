# SHARED CONTEXT — paste this BEFORE every module brief

You are a builder agent implementing one module of **`coop-data-doc`**, an open-source Python CLI
that documents end-to-end data lineage for a Microsoft BI estate.

## What the tool does
It crawls **two git repos** — (1) a SQL repo with stored procedures that build the gold layer from
silver-layer sources, plus tables and views (T-SQL: SQL Server / Azure serverless / Fabric
warehouse & lakehouse); (2) a Power BI repo with semantic models (PBIP/TMDL or .bim) and thin
reports (PBIR folders, legacy report.json, occasional .pbix). It builds one **LineageGraph**
covering: silver table → stored proc → gold table → view → semantic-model table → measure → report
visual. It then renders Markdown docs with strict YAML front-matter (for LLM agents) and a
searchable dark-mode MkDocs HTML portal (for humans). (The portal originally shipped Mermaid
flowcharts; that feature was later removed — mentions of "Mermaid flowcharts" and
`render/mermaid.py` in these briefs are historical.)

## Hard constraints — violating any of these fails review
1. **100% offline & deterministic.** No DB connections, no network, no LLM calls. AST parsing
   (sqlglot), string manipulation, and regex only. Same inputs ⇒ byte-identical outputs: sort all
   dict/list iteration, never embed timestamps or random values.
2. **Python ≥3.10**, full type hints. Allowed deps: `pydantic>=2.5`, `PyYAML`, `sqlglot>=25`,
   `click>=8.1`, `questionary>=2`, `mkdocs>=1.6`, `mkdocs-material>=9.5`. Nothing else.
3. **Pure functions in parsers** — no printing, no sys.exit; return results + a
   `list[ParseWarning]` (a small dataclass/pydantic model: `file`, `message`, `category`).
   Only the CLI layer talks to the terminal.
4. **Pytest tests are part of the deliverable**, including fixtures under `tests/fixtures/`.
5. Match the existing code style: pydantic v2 models, `from __future__ import annotations`,
   module docstrings explaining the module's role.

## The core data model (Module 0 — ALREADY BUILT, import it, do not redefine)
`from coop_data_doc.graph import Node, NodeType, Column, Edge, EdgeType, LineageGraph`

- `NodeType`: `silver_table | gold_table | view | stored_proc | semantic_model | pbi_table |
  measure | report | report_page | visual`
- `EdgeType`: `reads | writes | feeds | defines | references | visualizes`
- `Node(id, node_type, name, schema_name, source_file, columns: list[Column], metadata: dict)`
  - ids via `Node.make_id(node_type, schema, name)` → `"{type}:{schema}.{name}"` lowercased,
    brackets stripped (`[dbo].[Foo]` → `gold_table:dbo.foo`)
  - NOTE: the field is `schema_name` (pydantic shadows `schema`); renderers emit it as `schema`.
- `Column(name, data_type, nullable, constraints: list[str], description)`
- `Edge(source_id, target_id, edge_type, evidence)` — `evidence` is `"<file>: <snippet>"` proving
  the edge. Edges are authored in the parser-natural direction; data-flow direction is handled by
  `Edge.flow()`:
  | edge_type | authored as | data flows |
  |---|---|---|
  | reads | proc/view → table it reads | target → source |
  | writes | proc → table it writes | source → target |
  | feeds | view → pbi_table; pbi_table → semantic_model | source → target |
  | defines | proc → table it creates | source → target |
  | references | measure → measure/table; proc → proc (EXEC) | target → source |
  | visualizes | visual → pbi_table/measure | target → source |
- `LineageGraph`: `add_node` (merge-on-conflict), `add_edge` (idempotent),
  `upstream(id, depth=None)` / `downstream(id, depth=None)` (cycle-safe BFS, sorted ids),
  `retype_node(id, new_type)`, `subgraph(ids)`. JSON round-trip in
  `coop_data_doc.graph.serialize` (`to_json_file`, `from_json_file`, `to_json_str`).

## Package layout (your module's files are marked in the brief)
```
src/coop_data_doc/
├── cli.py                # M6
├── config.py  crawler.py # M1
├── graph/model.py  graph/serialize.py   # M0 (done)
├── parsers/sql_objects.py  sql_procs.py # M2
├── parsers/tmdl.py bim.py mcode.py dax.py pbir.py pbix.py  # M3
├── linker/resolver.py cache.py interactive.py              # M4
└── render/markdown.py paths.py site.py                     # M5  (was mermaid.py; mermaid removed, slug/doc_relpath moved to paths.py)
tests/  (fixtures/repo_sql, fixtures/repo_pbi, golden/)
```

## Domain naming context (real-world)
View schemas in the SQL repo feed semantic models with *similar but not identical* names —
e.g. schema `sales` feeds the "Sales Analytics" semantic model. Never assume
exact name matches across the repo boundary; that resolution is Module 4's job. Your module
should emit unresolved references as structured data, not guesses.

## Deliverable format
Return complete file contents for every file you create or modify (source + tests + fixtures),
each clearly labeled with its repo-relative path. State any assumptions you made at the end.
