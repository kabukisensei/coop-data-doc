@AGENTS.md

# coop-data-doc — agent guide

Offline, deterministic data-lineage doc generator for SQL + Power BI estates.
Start with `ARCHITECTURE.md` (pipeline, data model, design decisions), then
`CONTRIBUTING.md` (rules). The `tasks/` briefs document each module's
interface in depth.

**For the machine-readable agent contract, see `AGENTS.md`** (imported above).

## Quick setup

```bash
python3.13 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q      # verify: ~490 passed in a few seconds
```

**Python 3.10–3.13 only — never 3.14.** 3.14 breaks the editable-install
`.pth` / console-script imports, so `coop-data-doc` (and `python -m
coop_data_doc`) fails to import from a dev install. 3.13 is the recommended
dev interpreter; CI tests 3.10–3.13. `make setup` runs the first two lines
(venv + editable install) and verifies with `python -m coop_data_doc
--version` instead of pytest — run `make test` afterwards for the full check.

## Commands

```bash
.venv/bin/python -m pytest -q          # full suite (fast, ~2s)  [make test]
.venv/bin/ruff check src tests        # lint                     [make lint]
.venv/bin/ruff format --check src tests   # formatting (CI enforces this too)
.venv/bin/coop-data-doc build --non-interactive   # run the tool itself
```

## Release rule

Releases happen only when Aaron explicitly asks for one and names the version —
never create or push a `v*` tag on your own, and never infer a release from a
clean tree or a finished task (the tag push publishes straight to PyPI).

`__version__` in `src/coop_data_doc/__init__.py` is the **single source of
truth** — bump it there on every release. `pyproject.toml` is dynamic
(`[tool.hatch.version]`); there is no version field there to edit. The release
tag must match `__version__` exactly (`__version__ = "0.28.0"` → tag
`v0.28.0`); `.github/workflows/publish.yml` verifies tag == `__version__` and
aborts the PyPI publish on mismatch. Run `make release-check` before tagging
(it checks **local** tags only — `git fetch --tags` first on a stale clone).
Semver policy and rationale: `CONTRIBUTING.md` → "Releasing".

## Hard rules

1. **Determinism** (CI-enforced) — sorted iteration everywhere, no
   timestamps/randomness in output, and `newline="\n"` on every generated
   `write_text` (cross-OS byte-identity). `tests/test_determinism.py`
   byte-compares two full builds. Parallel SQL parsing (`--jobs N`,
   `parsers/parallel.py`) must preserve this: only the SQL parsers fan out
   across processes (the linker/renderers/TMDL/PBIR stay serial), and every
   file's contribution is merged back in sorted `entry.path` order via the
   issue-#17 `_replay_entry` path — so `--jobs N` is byte-identical to
   `--jobs 1` and to a cold serial build. `tests/test_parallel_parse.py`
   proves it. Never parallelize a cross-file pass.
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
