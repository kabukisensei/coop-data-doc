---
name: verify-coop-data-doc
description: "Drive the coop-data-doc CLI the way a user does: install this checkout into a temporary venv and replay init, scan, build, export, lineage, and the lineage cache against a small fixture. Use when changing the pipeline, parsers, linker, renderers, or CLI, or when asked to prove generated docs."
---

# Verify coop-data-doc

Prove the documentation generator by installing this checkout and running the real `coop-data-doc` commands against a copied fixture. Unit tests are not this proof. The replay writes markdown, the offline HTML portal, CSV exports, and `.lineage-cache.json`, then reads those files back.

The fixture lives at `.cursor/skills/verify-coop-data-doc/fixture/`. Each run copies it. The committed copy is never built in place.

## When to use

Use this after a change to crawl, SQL or Power BI parsing, linking, layering, markdown, the HTML portal, export, lineage, impact, or the CLI commands those features call. Also use it when a green `make test` is not enough to show that a user can generate and read docs.

Do not use it to publish a release or to upgrade the tool. `upgrade` is the only command that contacts the network, and this harness does not run it.

## Prerequisites

- Bash, `git`, and `diff`.
- Python 3.10, 3.11, 3.12, or 3.13 on `PATH`. The harness prefers 3.13. Python 3.14 cannot import this project from a pip install.
- No API keys and no database. The CLI replay does not use the network. The first install on a machine may download dependencies from PyPI into `.cache/verify/wheels` (gitignored). A later run installs those wheels with `--no-index` and rebuilds this checkout with `--no-build-isolation --no-deps`. No credentials are read or sent.
- `COOP_DATA_DOC_CONFIG` is unset for every command so a parent config cannot be selected. `COOP_DATA_DOC_DISCOVERY_ROOTS` is set to the temp project. `COOP_DATA_DOC_JOBS` is `1`.

## Launch

From the repo root:

```sh
bash scripts/verify.sh launch
```

That creates `/tmp/coop-data-doc-verify.<random>/`, a venv inside it, and a copy of the fixture at `project/`. Ready is:

```text
LAUNCH: ready cli=<venv>/bin/coop-data-doc project=<temp>/project evidence=<repo>/.cache/verify/evidence/<UTC timestamp>-<pid>
```

Nothing is served. Each later command is a new process. A second launch in this checkout refuses while that venv's `coop-data-doc` is still on disk. Two checkouts can run at once: each temp directory and evidence directory is its own.

The full command below launches, drives every feature, and deletes the temp project. Use `launch` only when driving one feature at a time.

## Doctor

Read-only. Run it before every drive, and again after a failed drive before you retry.

```sh
bash scripts/verify.sh doctor
```

Healthy output:

```text
DOCTOR: ok version=<__version__> python=<3.10|3.11|3.12|3.13> evidence=<dir>
```

Doctor checks that `coop-data-doc --version` and `python -m coop_data_doc --version` both contain `__version__` from `src/coop_data_doc/__init__.py`, that the interpreter is 3.10–3.13, that `help build` still says `searchable HTML portal`, and that the copied fixture config is in the temp project. A binary that answers but fails those checks is not this checkout. Do not drive it.

`DOCTOR: no verification project` means launch first. There is no shared machine-wide install.

## Drive

The fixture project is named `Verify Estate`. SQL is under `sql/` (`dbo.customers`, `dbo.orders`, `sales.dim_customer`, `dbo.usp_load_orders`). Power BI is a `Sales` model and a `Sales` report. `sales.dim_customer` matches the view exactly. `sales.dim_cust` does not: it is close enough to list as a candidate and not close enough to auto-link.

```sh
bash scripts/verify.sh drive project
bash scripts/verify.sh drive scan
bash scripts/verify.sh drive build
bash scripts/verify.sh drive export
bash scripts/verify.sh drive lineage
bash scripts/verify.sh drive lineage-cache
bash scripts/verify.sh drive check
```

`drive` with no feature, and `drive all`, run those seven in that order. Each prints `PROJECT: PASS`, `SCAN: PASS`, `BUILD: PASS`, `EXPORT: PASS`, `LINEAGE: PASS`, `LINEAGE-CACHE: PASS`, or `CHECK: PASS`.

`scan` must run before `build`. `export` and `lineage` need the built graph. `lineage-cache` needs that graph and an absent `.lineage-cache.json`. `check` needs the cache decision applied. A missing precondition fails the drive and names the command to run first. Do not report a skipped feature as covered by another command.

The feature map is the source for which entry points exist. Read `.cursor/skills/verify-coop-data-doc/features/README.md` before a partial drive.

## How to read the output

Stage lines on stdout name the feature and `PASS`. Transcripts (the exact argv, exit code, stdout, and stderr) are files in the evidence directory, one `<feature>.txt` per command. Copied outputs are under `evidence/artifacts/`.

A passing summary ends with:

```text
evidence: <repo>/.cache/verify/evidence/<UTC timestamp>-<pid>
RESULT: PASS
```

`RESULT: FAIL` is the last line when any command exits with the wrong code, an expected file or string is missing, a temp cache file is left behind, cleanup deletes evidence, or the checkout's git status changes. The `FAIL:` line above it names the assertion. The matching transcript in the evidence directory is the command and the output that missed.

`PASS` means every driven command exited with the code the feature file names, the asserted files and strings were present, the repeated `update` was byte-identical to the cached build, the temp project was removed, and every path in `keep.txt` was still in the evidence directory. Anything else is `FAIL`.

## Evidence

A full run writes `<repo>/.cache/verify/evidence/<UTC timestamp>-<pid>/`. `.cache/` is gitignored. Cleanup must not delete this directory.

| File | What it proves |
| --- | --- |
| `install.txt` | pip installed this checkout (offline cache, or one dependency download) |
| `doctor.txt` | version string and interpreter |
| `init.txt`, `init-again.txt`, `config-set.txt`, `show-config.txt`, `folders.txt`, `status-before.txt` | project commands and the empty-docs state |
| `scan.txt`, `artifacts/graph-scan.json` | graph with no HTML portal |
| `build.txt`, `artifacts/index.md`, `artifacts/index.html` | markdown and the portal |
| `export.txt`, `artifacts/objects.csv`, `artifacts/edges-before-cache.csv` | CSV export before the alias is linked |
| `lineage-ambiguous.txt`, `lineage-view.txt`, `lineage-column.txt` | a refused ambiguous name, the view's neighborhood, and `customer_id` |
| `resolve.txt`, `resolve-apply.txt`, `artifacts/lineage-cache.json` | the decision that was offered and the file that was written |
| `build-cached.txt`, `artifacts/graph-after-cache.json`, `artifacts/edges-after-cache.csv` | the same alias linked by the cache |
| `check.txt`, `findings.txt`, `impact.txt`, `update.txt`, `status-after.txt` | freshness, diagnostics, downstream impact, and a byte-identical rebuild |

The lineage-cache pair is the action (`resolve` / `resolve-apply`) and the resulting graph, not only the final portal. Impact is a second view of that new edge: `view:sales.dim_customer` lists `pbi_table:sales.dim_cust` downstream of the pre-cache graph.

## Cleanup

```sh
bash scripts/verify.sh cleanup
```

That deletes the temp directory only when its path is `/tmp/coop-data-doc-verify.*`, and deletes `.cache/verify/instance.env`. It does not delete evidence directories. It does not signal any process by name. There is no server and no worker pool: scan, build, and update are run with `--jobs 1`.

After cleanup, every file named in `keep.txt` must still be on disk. A cleanup that removes them is a failed run.

## Helpers

Full run, from the repo root. This is the command to quote:

```sh
bash scripts/verify.sh
```

The script is executable. It installs the checkout into the temp venv with `pip install` of a copy of `pyproject.toml`, `README.md`, `LICENSE`, and `src/` (the repo tree is not the install target, so the install does not write metadata into the checkout). It then runs `coop-data-doc` from that venv.

## Feature map

Each row is a user-facing command. The harness column is what a passing `bash scripts/verify.sh` actually checks. Commands the harness does not run stay listed so a partial drive is not mistaken for them.

| Command | What a pass proves |
| --- | --- |
| `--version`, `help build` | The temp install is this checkout and still describes the HTML portal. Doctor. |
| `init` | A scratch directory gets `coop-data-doc.yml` with project name `Coop BI Estate`. A second `init` without `--force` exits 1 and leaves the file. |
| `config-set` | A JSON patch renames that scratch project to `Scratch Estate`. The scaffold's repo paths are not on disk, so the write reports `saved, not runnable yet`. |
| `show-config` | The fixture reports `Verify Estate`, `exists: true`, and the temp config path. |
| `folders` | SQL is an allowlist of `tables`, `views`, and `procs`, all documented. Both Power BI folders exist and are documented. |
| `status` | Before a build: docs and `.lineage-cache.json` are missing, and one reference is unresolved. After the cached build: docs are up to date, the cache exists, unresolved is 0. |
| `scan` | `data-docs/graph.json` contains the nine fixture objects, the view reads `dbo.customers`, the proc writes `dbo.orders`, and `sales.dim_customer` feeds the exact table. No `index.md` and no `index.html`. The alias is unresolved. |
| `build` | Adds `index.md`, `manifest.json` (byte-identical to `graph.json`), a view page whose upstream is `gold_table:dbo.customers`, and `data-docs-site/index.html` plus `search/search_index.json` containing the project name and `dim_customer`. |
| `export` | Writes `objects.csv`, `columns.csv`, `measures.csv`, and `edges.csv`. The measure CSV contains `CustomerCount` and `COUNTROWS`. The cached SQL link is absent until `resolve-apply`. |
| `lineage` | `sales.dim_customer` is ambiguous between the view and the Power BI table. `view:sales.dim_customer` has `dbo.customers` upstream and the exact table downstream. `--column customer_id` reports `dbo.customers.customer_id`. |
| `resolve` | Lists one unresolved item, `pbi_table:sales.dim_cust`, whose candidates include `view:sales.dim_customer`. |
| `resolve-apply` | Writes `.lineage-cache.json` (`version` 1, method `interactive`, that target and source signature) and leaves no `.lineage-cache.json.*.tmp` file. |
| `build` after the cache | `--strict` exits 0, unresolved is 0, the cache file is unchanged, and the new feeds edge evidence is `linker: cache (sales.dim_cust)`. |
| `check` | Prints `docs are up to date` (cold parse, `--no-parse-cache`). |
| `findings` | Envelope `tool` `coop-data-doc`, schema version 1, no error severity, and none of the strict warning rules. |
| `impact` | Against the pre-cache graph, `view:sales.dim_customer` downstream includes `pbi_table:sales.dim_cust`. Against the post-cache graph, a later identical build returns `{}`. |
| `update` | Rebuilds with the alias-of-build notice. `data-docs/` and `data-docs-site/` are byte-identical to the strict build that ran after export. |
| `setup` | Not driven. It is the interactive wizard (or the JSONL transport). `init` and `config-set` cover non-interactive config authoring. |
| `set-folders` | Not driven. It rewrites include globs. `folders` covers the read side on the fixture allowlist. |
| `upgrade` | Not driven. It is the only command that uses the network. |

## Result

Run `bash scripts/verify.sh` from the repo root. The last line of its stdout is the verdict. Stop there and quote the summary block.

```text
RESULT: PASS
```

or

```text
RESULT: FAIL
```

When the CLI or the fixture changes, update the feature map and re-run. `/maintain-verification-skill` is the upkeep loop.
