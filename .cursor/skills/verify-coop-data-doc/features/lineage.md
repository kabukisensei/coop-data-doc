# Lineage

Lineage answers where one documented object comes from and what uses it, using the built graph rather than a new guess.

## Sub-features

- `lineage-ambiguous` refuses a name shared by the view and the Power BI table.
- `lineage-view` shows the view's upstream table and downstream Power BI table.
- `lineage-column` traces `customer_id` to `dbo.customers.customer_id`.

## How to get to it (user POV)

- Run `coop-data-doc build` first.
- Run `coop-data-doc lineage sales.dim_customer`.
- Run `coop-data-doc lineage view:sales.dim_customer --depth 2`.
- Run `coop-data-doc lineage view:sales.dim_customer --column customer_id`.

## Driving it with verify.sh

Preconditions:

- `bash scripts/verify.sh doctor` prints `DOCTOR: ok`.
- `data-docs/graph.json` exists. `drive build` is the usual way to get it.

- **Ambiguous name.** Run `bash scripts/verify.sh drive lineage`. `lineage sales.dim_customer` exits 0. The JSON has `ambiguous: true` and the matches are exactly `view:sales.dim_customer` and `pbi_table:sales.dim_customer`. `loaded_by` includes that Power BI table with `linked: true` and source `sales.dim_customer`.
- **View neighborhood.** `lineage view:sales.dim_customer --depth 2` exits 0. `upstream` includes `gold_table:dbo.customers`. `downstream` includes `pbi_table:sales.dim_customer`. The object has an `evidence` object.
- **Column.** `lineage view:sales.dim_customer --column customer_id` exits 0. `column` is `customer_id` and `source_columns` includes `dbo.customers.customer_id`.
- **Proof.** Evidence holds `lineage-ambiguous.txt`, `lineage-view.txt`, `lineage-column.txt`, `artifacts/lineage-view.json`, and `artifacts/lineage-column.json`.

## Gotchas

- `sales.dim_customer` is the qualified name of both the view and the Power BI table. A pass that prints only one of them is a failed drive, not a simpler reading.
- Querying the node id `view:sales.dim_customer` is the unambiguous entry. `loaded_by` is asserted on the ambiguous query, where the partition name is the query text.
- `lineage` does not rebuild. A stale `graph.json` is what it will read.
