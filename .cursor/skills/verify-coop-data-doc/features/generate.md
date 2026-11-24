# Generate

Generate crawls the fixture, writes the graph, then writes markdown and the offline HTML portal. A later check and update prove those files stay current.

## Sub-features

- `scan-graph` writes `data-docs/graph.json` and does not write the portal.
- `build-pages` writes markdown, `manifest.json`, and the HTML portal.
- `check-fresh` reports the built docs are up to date.
- `update-same` rebuilds through the `update` alias without changing a byte.

## How to get to it (user POV)

- Run `coop-data-doc scan --non-interactive --jobs 1` in the fixture project.
- Run `coop-data-doc build --non-interactive --jobs 1` in the fixture project.
- After the lineage cache has been applied, run `coop-data-doc build --non-interactive --strict --jobs 1`.
- Run `coop-data-doc check --no-parse-cache`.
- Run `coop-data-doc update --non-interactive --strict --jobs 1`.

## Driving it with verify.sh

Preconditions:

- `bash scripts/verify.sh doctor` prints `DOCTOR: ok`.
- For `scan` and `build`, `.lineage-cache.json` does not exist yet.
- For `check`, `bash scripts/verify.sh drive lineage-cache` has already passed.

- **Scan.** Run `bash scripts/verify.sh drive scan`. Exit code is 0. `data-docs/graph.json` exists. `data-docs/index.md` and `data-docs-site/index.html` do not. The graph's nodes are exactly `gold_table:dbo.customers`, `gold_table:dbo.orders`, `view:sales.dim_customer`, `stored_proc:dbo.usp_load_orders`, `semantic_model:sales`, `pbi_table:sales.dim_customer`, `pbi_table:sales.dim_cust`, `measure:sales.customercount`, and `report:sales`. The view reads `gold_table:dbo.customers`, the proc writes `gold_table:dbo.orders`, and the view feeds `pbi_table:sales.dim_customer`. It does not feed `pbi_table:sales.dim_cust`. Stderr contains `1 unresolved`.
- **Build.** Run `bash scripts/verify.sh drive build`. Exit code is 0. Stderr contains `Markdown docs:` and `HTML portal:`. `index.md` starts with `# Verify Estate`. `manifest.json` matches `graph.json` byte for byte. A page under `data-docs/view/` has id `view:sales.dim_customer` and upstream `gold_table:dbo.customers`. `data-docs-site/index.html` contains `Verify Estate`. `data-docs-site/search/search_index.json` contains `dim_customer`. The model feeds `report:sales`.
- **Check.** Run `bash scripts/verify.sh drive check` after the cache drive. `check --no-parse-cache` exits 0 and prints `docs are up to date`. `findings` is a `coop-data-doc` envelope at schema version 1 with no error-severity item and none of `regex_fallback`, `dynamic_sql`, `unresolved_partition_source`, `ambiguous_visual_binding`, or `unresolved_reference`.
- **Update.** The same check drive runs `build --non-interactive --strict --jobs 1` after the second export, then `update --non-interactive --strict --jobs 1`. The export CSVs live in the docs directory and the portal copies them, so that build publishes the CSVs the update must reproduce. Stderr contains `alias of build`. `data-docs/` and `data-docs-site/` match that build. `status` then prints `freshness: up to date`, shows the lineage cache exists, and prints `unresolved: 0`.
- **Proof.** Evidence holds `scan.txt`, `build.txt`, `check.txt`, `findings.txt`, `update.txt`, `status-after.txt`, `artifacts/graph-scan.json`, `artifacts/index.md`, and `artifacts/index.html`.

## Gotchas

- The first `build` is not `--strict`. One Power BI source is still unresolved, and `--strict` would exit 2. The strict build is the cached rebuild in the lineage-cache drive.
- `--jobs 1` keeps parsing in this process. The product is byte-identical at other job counts; the harness does not leave a worker pool behind.
- `check` compares the published tree to a fresh render. Running it before the cached rebuild fails because the alias is unresolved.
- `update` is an alias of `build` and prints a notice. It does not contact PyPI. `upgrade` does, and this drive does not run `upgrade`.
- `export` writes CSVs into the docs directory. A portal built before that export still has the previous copies. Compare `update` to a build that ran after the export.
