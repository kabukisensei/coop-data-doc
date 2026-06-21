# coop-data-doc — agent guide

Offline, deterministic data-lineage doc generator for SQL + Power BI estates.
Start with `ARCHITECTURE.md` (pipeline, data model, design decisions), then
`CONTRIBUTING.md` (rules). The `tasks/` briefs document each module's
interface in depth.

**For the machine-readable agent contract, see `AGENTS.md`.**

## Commands

```bash
.venv/bin/python -m pytest -q          # full suite (fast, <1s)
.venv/bin/ruff check src tests        # lint
.venv/bin/ruff format --check src tests   # formatting (CI enforces this too)
.venv/bin/coop-data-doc build --non-interactive   # run the tool itself
```

If `.venv` is missing: `python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"`.

## Hard rules

1. **Determinism** (CI-enforced) — sorted iteration everywhere, no
   timestamps/randomness in output, and `newline="\n"` on every generated
   `write_text` (cross-OS byte-identity). `tests/test_determinism.py`
   byte-compares two full builds.
2. **Offline pipeline** — no network/DB/LLM anywhere in doc generation;
   built HTML must work over `file://` (vendored assets in
   `src/coop_data_doc/templates/assets/`). Sole exception: the explicit
   `upgrade` command (`upgrade.py`); the pipeline never imports it.
3. **Pure parsers** (convention, reviewed not CI-enforced) — no print/exit
   outside `cli.py`, `wizard.py`, `progress.py`, and `linker/interactive.py`;
   warnings are returned as `ParseWarning` values.
4. **Never guess lineage** — un-provable things become warnings or
   `unresolved` markers, not edges.

## Orientation shortcuts

- Orchestration: `cli.run_pipeline()` — the whole pipeline in one function
  (crawl → SQL parse → PBI parse → prune_schemas → assign_layers → link).
- Data model + edge-direction semantics: `graph/model.py` (read `Edge.flow()`
  before touching traversal — `reads`/`references`/`visualizes` are authored
  opposite to data flow). `id`/`name` are normalized (lowercase) for matching;
  `display_name` keeps original case for rendering (`Node.qualified_display`).
- Layers (bronze/silver/gold) come from `config.layers` rules in `layering.py`,
  not from node type; object type is parser-detected.
- Diagnostics (`diagnostics.py`) classify every warning by severity and render
  the console summary + `diagnostics.json` + the HTML Diagnostics page.
- Tests are fixture-driven: `tests/fixtures/repo_sql` and `repo_pbi` are
  miniature real repos; most tests assert exact node-id/edge-key sets.
- When adding a parser case, extend the fixtures rather than writing inline
  SQL/JSON strings, so the crawler/CLI/determinism suites cover it too.
