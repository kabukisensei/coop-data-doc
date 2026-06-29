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
| M5 renderers | `render/markdown.py`, `mermaid.py`, `site.py` | agent Markdown + offline MkDocs Material portal; nav grouped by layer→type; `schema.Object` (original-case `display_name`) titles |
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
4. **Page filenames go through `slug()`** (`render/mermaid.py`): always
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

## Releasing

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
