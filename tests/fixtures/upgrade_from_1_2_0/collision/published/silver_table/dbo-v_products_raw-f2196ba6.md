---
id: "silver_table:dbo.v_products_raw"
type: "silver_table"
name: "v_products_raw"
schema: "dbo"
layer: "silver"
source_file: ""
path: "silver_table/dbo-v_products_raw-f2196ba6.md"
upstream_inputs: []
downstream_dependents:
  - "view:sales.v_products"
tags:
  - "dbo"
---

# dbo.v_products_raw

## Structural Contract

_Columns not statically resolvable for this object._

## Lineage

### Upstream

_No upstream objects._

### Downstream

| Object | Type | Via | Evidence |
| --- | --- | --- | --- |
| [sales.v_products](../view/sales-v_products-c0708a19.md) | view | reads | views/v_products.sql |

## Business Intent

<!-- intent:begin -->
_Add a short description of what this object is for and who relies on it._
<!-- intent:end -->
