---
id: "pbi_table:sales.orders"
type: "pbi_table"
name: "orders"
schema: "sales"
layer: ""
source_file: "Sales.SemanticModel/definition/tables/orders.tmdl"
path: "pbi_table/sales-orders-ca4a47cd.md"
upstream_inputs:
  - "view:sales.v_orders_2025"
downstream_dependents:
  - "semantic_model:sales"
tags:
  - "sales"
---

# sales.orders

**Storage mode:** Import

## Structural Contract

| Column | Type | Constraints | Description |
| --- | --- | --- | --- |
| id | int64 |  |  |

## Lineage

### Upstream

| Object | Type | Via | Evidence |
| --- | --- | --- | --- |
| [sales.v_orders_2025](../view/sales-v_orders_2025-80863fab.md) | view | feeds | linker |

### Downstream

| Object | Type | Via | Evidence |
| --- | --- | --- | --- |
| [Sales](../semantic_model/sales-0de5bea3.md) | semantic_model | feeds | Sales.SemanticModel/definition/tables/orders.tmdl |

## Business Intent

<!-- intent:begin -->
Orders combines the 2024 archive and the 2025 live partition. Owner: finance.
<!-- intent:end -->
