---
id: "silver_table:dbo.v_orders_2024_raw"
type: "silver_table"
name: "v_orders_2024_raw"
schema: "dbo"
layer: "silver"
source_file: ""
path: "silver_table/dbo-v_orders_2024_raw-9613b6f9.md"
upstream_inputs: []
downstream_dependents:
  - "view:sales.v_orders_2024"
tags:
  - "dbo"
---

# dbo.v_orders_2024_raw

## Structural Contract

_Columns not statically resolvable for this object._

## Lineage

### Upstream

_No upstream objects._

### Downstream

| Object | Type | Via | Evidence |
| --- | --- | --- | --- |
| [sales.v_orders_2024](../view/sales-v_orders_2024-b1ce610f.md) | view | reads | views/v_orders_2024.sql |

## Business Intent

<!-- intent:begin -->
_Add a short description of what this object is for and who relies on it._
<!-- intent:end -->
