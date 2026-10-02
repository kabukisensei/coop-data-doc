---
id: "semantic_model:sales"
type: "semantic_model"
name: "sales"
schema: ""
layer: ""
source_file: "Sales.SemanticModel/definition/model.tmdl"
path: "semantic_model/sales-0de5bea3.md"
upstream_inputs:
  - "pbi_table:sales.basket"
  - "pbi_table:sales.customer"
  - "pbi_table:sales.orders"
  - "pbi_table:sales.scratch"
  - "pbi_table:sales.weblog"
downstream_dependents: []
tags: []
---

# Sales

## Relationship Grid

_No relationships defined in this semantic model._

## Lineage

### Upstream

| Object | Type | Via | Evidence |
| --- | --- | --- | --- |
| [basket](../pbi_table/sales-basket-e47e18bb.md) | pbi_table | feeds | Sales.SemanticModel/definition/tables/basket.tmdl |
| [customer](../pbi_table/sales-customer-b08c88bf.md) | pbi_table | feeds | Sales.SemanticModel/definition/tables/customer.tmdl |
| [orders](../pbi_table/sales-orders-ca4a47cd.md) | pbi_table | feeds | Sales.SemanticModel/definition/tables/orders.tmdl |
| [scratch](../pbi_table/sales-scratch-e67957f1.md) | pbi_table | feeds | Sales.SemanticModel/definition/tables/scratch.tmdl |
| [weblog](../pbi_table/sales-weblog-217ade12.md) | pbi_table | feeds | Sales.SemanticModel/definition/tables/weblog.tmdl |

### Downstream

_No downstream objects._

## Business Intent

<!-- intent:begin -->
_Add a short description of what this object is for and who relies on it._
<!-- intent:end -->
