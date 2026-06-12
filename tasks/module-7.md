# MODULE 7 — Tests, Packaging & CI Hardening ✅ IMPLEMENTED

> Status: **done** — kept as interface reference; do not reimplement.

**Files to create/modify:** `tests/test_determinism.py`, `.github/workflows/ci.yml`,
`.github/workflows/publish.yml`, `README.md`, `CONTRIBUTING.md`, `LICENSE`,
`pyproject.toml` (review/extend — it exists).

## 1. End-to-end determinism suite (`tests/test_determinism.py`)
- Run the FULL pipeline (`build --non-interactive --skip-html` via CliRunner) twice into two
  temp dirs against the fixture repos → assert the directory trees are byte-identical
  (walk + hash compare).
- Run once, then again over the same output dir (after injecting a Business Intent edit) →
  assert idempotent except the preserved intent block.
- Cross-platform: no assertions on path separators; use `as_posix()` comparisons.

## 2. `README.md`
Quickstart for coworkers (the primary audience — keep it non-expert friendly):
```
pipx install coop-data-doc        # or: uv tool install coop-data-doc
cd your-docs-folder
coop-data-doc init                # creates coop-data-doc.yml — edit the two repo paths
coop-data-doc build               # answers a few mapping questions the first time
open data-docs-site/index.html
```
Sections: what it documents (the lineage chain diagram), the two-repo setup, the
`.lineage-cache.json` workflow (commit it!), CI usage (`coop-data-doc check`), the .pbix
limitation + "save as PBIP" guidance, troubleshooting table. Note the one-time vendored
`mermaid.min.js` provenance (version + source URL) in a "third-party assets" note.

## 3. `CONTRIBUTING.md`
Module map (M0–M6 with file paths), the determinism rules (sorted iteration, no timestamps),
the pure-parser rule (warnings as data), how to add a new parser (implement
`(entries, graph) -> list[ParseWarning]`, add fixtures + golden expectations), how to run tests.

## 4. CI (`.github/workflows/ci.yml`)
- Trigger: push + PR.
- Jobs: `lint` (ruff check + format --check); `test` matrix
  `python: [3.10, 3.11, 3.12, 3.13]` × `os: [ubuntu-latest, windows-latest]`
  (Windows matters — path handling), `pip install -e .[dev]`, `pytest`.

## 5. Publish (`.github/workflows/publish.yml`)
- Trigger: tag `v*`. Build with `python -m build`, publish via **PyPI trusted publishing**
  (`pypa/gh-action-pypi-publish`, `permissions: id-token: write`), environment `pypi`.
- Sanity job before publish: install the built wheel in a clean venv, run
  `coop-data-doc --version`.

## 6. pyproject review
Confirm: classifiers current, `[project.scripts]` entry, `mkdocs-material` minimum supports the
`offline` plugin (>=9.0), package data includes `templates/` assets
(`[tool.hatch.build.targets.wheel]` force-include if needed).

## Acceptance criteria
- Full matrix green; `pip install -e .[dev] && pytest` works from a clean clone.
- Tag → wheel on PyPI with working console script.
