# coop-data-doc — agent guide

Offline, deterministic data-lineage doc generator for SQL + Power BI estates.
Start with `ARCHITECTURE.md` (pipeline, data model, design decisions), then
`CONTRIBUTING.md` (rules). The `tasks/` briefs document each module's
interface in depth.

## Commands

```bash
.venv/bin/python -m pytest -q          # full suite (fast, <1s)
.venv/bin/ruff check src tests        # lint
.venv/bin/coop-data-doc build --non-interactive   # run the tool itself
```

If `.venv` is missing: `python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"`.

## Hard rules (CI-enforced)

1. **Determinism** — sorted iteration everywhere, no timestamps/randomness in
   output. `tests/test_determinism.py` byte-compares two full builds.
2. **Offline** — no network/DB/LLM at runtime; built HTML must work over
   `file://` (vendored assets in `src/coop_data_doc/templates/assets/`).
3. **Pure parsers** — no print/exit outside `cli.py`, `wizard.py`, and
   `linker/interactive.py`; warnings are returned as `ParseWarning` values.
4. **Never guess lineage** — un-provable things become warnings or
   `unresolved` markers, not edges.

## Orientation shortcuts

- Orchestration: `cli.run_pipeline()` — the whole pipeline in one function.
- Data model + edge-direction semantics: `graph/model.py` (read `Edge.flow()`
  before touching traversal — `reads`/`references`/`visualizes` are authored
  opposite to data flow).
- Tests are fixture-driven: `tests/fixtures/repo_sql` and `repo_pbi` are
  miniature real repos; most tests assert exact node-id/edge-key sets.
- When adding a parser case, extend the fixtures rather than writing inline
  SQL/JSON strings, so the crawler/CLI/determinism suites cover it too.
