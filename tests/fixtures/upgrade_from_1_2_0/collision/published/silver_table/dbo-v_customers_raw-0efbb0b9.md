---
id: "silver_table:dbo.v_customers_raw"
type: "silver_table"
name: "v_customers_raw"
schema: "dbo"
layer: "silver"
source_file: ""
path: "silver_table/dbo-v_customers_raw-0efbb0b9.md"
upstream_inputs: []
downstream_dependents:
  - "view:sales.v_customers"
tags:
  - "dbo"
---

# dbo.v_customers_raw

## Structural Contract

_Columns not statically resolvable for this object._

## Lineage

### Upstream

_No upstream objects._

### Downstream

| Object | Type | Via | Evidence |
| --- | --- | --- | --- |
| [sales.v_customers](../view/sales-v_customers-04f87d30.md) | view | reads | views/v_customers.sql |

## Business Intent

<!-- intent:begin -->
_Add a short description of what this object is for and who relies on it._
<!-- intent:end -->
