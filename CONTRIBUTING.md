# Contributing

## Module map

| Module | Files | Role |
| --- | --- | --- |
| M0 core graph | `graph/model.py`, `graph/serialize.py` | `Node`/`Edge`/`LineageGraph`; the only data structure modules share |
| M1 config + crawler | `config.py`, `crawler.py` | `coop-data-doc.yml` loading, repo walking, `FileKind` classification |
| M2 SQL parser | `parsers/sql_common.py`, `sql_objects.py`, `sql_procs.py` | sqlglot (tsql) AST lineage with a regex fallback ladder |
| M3 Power BI extractor | `parsers/tmdl.py`, `bim.py`, `mcode.py`, `dax.py`, `pbir.py`, `pbix.py` | semantic models, measures, reports, best-effort pbix |
| M4 linker | `linker/resolver.py`, `cache.py`, `interactive.py` | joins SQL ↔ PBI: cache → exact → config rule → fuzzy → prompt |
| M4½ layering | `layering.py` | medallion layer (bronze/silver/gold) from `config.layers` rules (schema/path), heuristic fallback; object *type* stays parser-detected |
| M4¾ diagnostics | `diagnostics.py` | severity-classified warnings/unresolved → console summary, `diagnostics.json`, and the HTML Diagnostics page |
| M5 renderers | `render/markdown.py`, `paths.py`, `site.py` | agent Markdown + offline MkDocs Material portal; nav grouped by layer→type; `schema.Object` (original-case `display_name`) titles |
| M6 CLI | `cli.py`, `wizard.py`, `upgrade.py`, `progress.py` | interactive menu (bare invocation), `setup` / `init` / `scan` / `build` / `update` / `check` / `help` / `upgrade`; stderr progress bars + spinner |

The original builder briefs live in `tasks/` and double as interface documentation.

## Non-negotiable rules

1. **Deterministic output.** Iterate everything in sorted order; never embed
   timestamps or randomness; pass `newline="\n"` to every `write_text` of a
   generated artifact (Windows would otherwise emit CRLF and break
   cross-platform byte-identity). `tests/test_determinism.py` builds twice
   and byte-compares.
2. **Offline at runtime.** No network, no DB connections, no LLM calls
   anywhere in the documentation pipeline. The built HTML must work over
   `file://` (vendored assets live in `src/coop_data_doc/templates/assets/`).
   The single sanctioned exception is the explicit `upgrade` command
   (`upgrade.py`), which checks PyPI/git for tool and dependency updates —
   nothing in the pipeline may import it.
3. **Parsers are pure.** No printing or exiting outside `cli.py`,
   `wizard.py`, `progress.py`, and `linker/interactive.py`; warnings are
   returned as `ParseWarning` values. Parsers/renderers may accept an optional
   `on_file`/`on_node` callback for progress reporting (the CLI supplies
   it) — that's a reporting hook, not printing; the parser never renders.
4. **Page filenames go through `slug()`** (`render/paths.py`): always
   filesystem-safe (Windows-illegal chars stripped), length-bounded, and
   uniquified with a short id-hash. Never build a page path by hand; agents
   read the `path` front-matter field.
5. **Never guess lineage.** Dynamic SQL, opaque pbix models, and
   unrecognized partition sources produce warnings/unresolved markers, not
   invented edges.

## Adding a parser

Implement `(entries: list[FileEntry], graph: LineageGraph) -> list[ParseWarning]`,
register the file kind in `crawler.py`, wire it into `cli.run_pipeline`, and add
fixtures under `tests/fixtures/` with node/edge-set assertions.

## Developing

```bash
pip install -e ".[dev]"
pytest
ruff check src tests
```

Use Python 3.10–3.13 (3.13 recommended); **never 3.14** — it breaks the
editable-install `.pth` / console-script imports. `make setup` builds a
correct `.venv`; `make test` / `make lint` use it without activation.

## Testing strategy

Two tiers — run the first on every change, the second before releases and
after parser/crawler/linker changes.

**1. Unit suite (pytest).** Fixture-driven: `tests/fixtures/repo_sql` and
`repo_pbi` are miniature real repos, and most tests assert exact node-id /
edge-key sets. The full suite should finish in a few seconds with zero failures:

```bash
.venv/bin/python -m pytest -q     # or: make test
```

CI (`.github/workflows/ci.yml`) runs the same suite on Python 3.10–3.13 ×
ubuntu + windows, plus `ruff check` and `ruff format --check`.

**2. Real-estate end-to-end.** The fixtures are deliberately tiny; before a
release, build against a real estate. Reference estates on Aaron's machine,
cloned side-by-side under one parent directory — `~/Developer` there; the
side-by-side layout is the assumption everywhere cross-repo paths appear
(machine-specific example paths — substitute your own repos elsewhere):

- `~/Developer/fabric-dw` — Fabric warehouse SQL estate, ~452 `.sql` files
  (tables / views / stored procedures across several schemas)
- `~/Developer/fabric` — Power BI estate, ~130 `.tmdl` + ~36 `.pbix` files
  plus PBIR reports

> **This tier is Aaron's-Mac-only.** Both estates live in a private Azure
> DevOps org with interactive-only auth, so a headless machine
> (VPS, CI) **cannot clone or pull them**. On headless machines run only
> tier 1 (the unit suite); if a task requires these corpora, stop and report
> that it needs Aaron's Mac rather than attempting a workaround.

Put a `coop-data-doc.yml` in a folder that is a **sibling of both repos**
(the README's "Worked example: a large multi-schema warehouse" uses exactly
these two repos' paths; keep `output.dir` and `output.site_dir` side-by-side —
validation rejects nesting one inside the other), then build with this repo's
dev install:

```bash
.venv/bin/coop-data-doc build --config path/to/coop-data-doc.yml --non-interactive
```

Expected ballpark against those two estates (regression sanity only — the
estates grow, so treat every number as approximate): **~948 objects,
~2760 edges, ~56 cross-repo links, 0 unresolved, ~40 s**. Investigate if a
rebuild suddenly loses objects/edges, gains unresolved items, or slows
sharply — diff `data-docs/diagnostics.json` against the previous run, and use
`coop-data-doc scan` (no rendering) as the fast feedback loop while narrowing
down.

## Releasing

Releases are human-initiated: cut one only when Aaron explicitly asks for a
release and names the version. Never infer a release from a clean tree, a
merged PR, or a finished task — pushing a `v*` tag publishes to PyPI and
cannot be undone (versions are never reused).

A feature PR must update this repo's own user-facing docs (`README.md`,
`AGENTS.md`) in the same change — docs move with the feature, never in a
later "sync" pass.

Bump the version in `src/coop_data_doc/__init__.py` on every release — that is the
single source of truth. `pyproject.toml` uses dynamic versioning
(`[tool.hatch.version]`) and derives its version from `__version__`, so there is no
version field there to edit. This is mandatory: `coop-data-doc upgrade` is **version-gated** —
`pipx upgrade` only installs when the version number increased — so shipping code
changes under an unchanged version makes users' upgrade silently report "already at
latest" and skip them. PyPI releases are immutable, so a version is never reused.
Use semver: patch for fixes, minor for new commands/features, major for breaking
config/output changes.

Publishing is automated via trusted publishing (`.github/workflows/publish.yml`):
push a matching tag — `git tag vX.Y.Z && git push origin vX.Y.Z` — and the release
builds and uploads to PyPI on its own (no tokens).
