# Lineage cache

The lineage cache remembers a person's cross-repo decision. On this fixture the Power BI source `sales.dim_cust` is not an exact SQL object, so the first build leaves it unresolved until `resolve-apply` writes `.lineage-cache.json`.

## Sub-features

- `resolve-list` prints the one unresolved alias and its candidates.
- `resolve-apply-write` stores the chosen view and leaves no temp file.
- `rebuild-cache` links that alias from the cache under `--strict`.
- `impact-new-edge` shows the view's new downstream table.

## How to get to it (user POV)

- Run `coop-data-doc build` and notice one unresolved reference.
- Run `coop-data-doc resolve` and read the candidates.
- Run `coop-data-doc resolve-apply --from-json <decisions>` with the candidate `view:sales.dim_customer`.
- Run `coop-data-doc build --non-interactive --strict` again.
- Run `coop-data-doc lineage pbi_table:sales.dim_cust`.
- Run `coop-data-doc impact --baseline <graph from the first build>`.

## Driving it with verify.sh

Preconditions:

- `bash scripts/verify.sh doctor` prints `DOCTOR: ok`.
- `data-docs/graph.json` exists and `.lineage-cache.json` does not.
- `drive build` has passed.

- **List.** Run `bash scripts/verify.sh drive lineage-cache`. `resolve` exits 0. `unresolved` has one item. Its `cache_key` is `pbi_table:sales.dim_cust` and its candidates include `view:sales.dim_customer`.
- **Apply.** The drive writes a decision for that key, that target, and the item's `source_signature`. `resolve-apply` exits 0 and prints `Applied 1 decision`. `.lineage-cache.json` is version 1, method `interactive`, with that target and a source signature. No `.lineage-cache.json.*.tmp` file remains.
- **Rebuild.** `build --non-interactive --strict --jobs 1` exits 0 and stderr contains `0 unresolved`. The cache file is byte-identical to the file `resolve-apply` wrote. The graph has a feeds edge from `view:sales.dim_customer` to `pbi_table:sales.dim_cust` whose evidence contains `linker: cache (sales.dim_cust)`. That table's `source_resolutions` method is `cache`.
- **Read it back.** `lineage pbi_table:sales.dim_cust` exits 0 and lists `view:sales.dim_customer` upstream. A second `export` writes `linker: cache (sales.dim_cust)` into `edges.csv`.
- **Impact.** `impact --baseline` of the pre-cache graph exits 0. The JSON maps `view:sales.dim_customer` to a list that includes `pbi_table:sales.dim_cust`.
- **Proof.** Evidence holds `resolve.txt`, `resolve-apply.txt`, `build-cached.txt`, `lineage-alias.txt`, `export-after.txt`, `impact.txt`, `artifacts/lineage-cache.json`, `artifacts/graph-after-cache.json`, and `artifacts/edges-after-cache.csv`.

## Gotchas

- Exact and auto-fuzzy links are not written to `.lineage-cache.json`. Only `resolve-apply` (or an interactive answer) creates the file. The fixture name `dim_cust` stays in the ask band on purpose. Renaming it so the score reaches the auto-accept threshold makes this drive fail, because `resolve` would no longer list it.
- The harness applies `view:sales.dim_customer` only when `resolve` offered that target. It does not invent a node id.
- A build that finds the same cache mappings does not rewrite the file. A changed cache file after the strict build is a failure.
- The identical-graph impact (`{}`) is part of `drive check`, after `update`. It is not a substitute for the pre-cache impact above.
