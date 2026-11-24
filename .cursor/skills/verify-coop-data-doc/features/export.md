# Export

Export turns the built graph into four CSV files a person can open in a spreadsheet.

## Sub-features

- `export-files` writes `objects.csv`, `columns.csv`, `measures.csv`, and `edges.csv` into the docs directory.
- `export-contents` includes the view, the customer column, and the `CustomerCount` measure.
- `export-before-cache` does not yet contain the cached SQL link for `dim_cust`.

## How to get to it (user POV)

- Run `coop-data-doc build` first.
- Run `coop-data-doc export` in the fixture project.
- Open `data-docs/objects.csv` and `data-docs/edges.csv`.

## Driving it with verify.sh

Preconditions:

- `bash scripts/verify.sh doctor` prints `DOCTOR: ok`.
- `bash scripts/verify.sh drive build` has passed, so `data-docs/index.md` exists.
- `.lineage-cache.json` does not exist yet.

- **Export.** Run `bash scripts/verify.sh drive export`. Exit code is 0. Stdout contains `Exported objects, columns, measures, and edges`. All four CSV files exist under `data-docs/`.
- **Objects and columns.** `objects.csv` contains `view:sales.dim_customer` and `pbi_table:sales.dim_cust`. `columns.csv` contains `customer_id`.
- **Measure.** `measures.csv` contains `CustomerCount` and `COUNTROWS`.
- **Edges.** `edges.csv` contains `pbi_table:sales.dim_customer`. It does not contain `linker: cache`.
- **Proof.** Evidence holds `export.txt`, `artifacts/objects.csv`, and `artifacts/edges-before-cache.csv`.

## Gotchas

- `export` reads `data-docs/graph.json`. It does not crawl. Without a build it exits with `no built graph`.
- The alias table is already an object, so it appears in `objects.csv` before anyone links it. The SQL feeds evidence `linker: cache (sales.dim_cust)` appears only after `resolve-apply` and another build. The lineage-cache drive exports again and checks that string.
- CSV files are written with a UTF-8 BOM. Assertions search the text; they do not strip the BOM by hand.
