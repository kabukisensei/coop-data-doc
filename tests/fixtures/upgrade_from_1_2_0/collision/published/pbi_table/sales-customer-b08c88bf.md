---
id: "pbi_table:sales.customer"
type: "pbi_table"
name: "customer"
schema: "sales"
layer: ""
source_file: "Sales.SemanticModel/definition/tables/customer.tmdl"
path: "pbi_table/sales-customer-b08c88bf.md"
upstream_inputs:
  - "view:sales.v_customers"
downstream_dependents:
  - "semantic_model:sales"
tags:
  - "sales"
---

# sales.customer

**Storage mode:** Import

## Structural Contract

| Column | Type | Constraints | Description |
| --- | --- | --- | --- |
| id | int64 |  |  |

## Lineage

### Upstream

| Object | Type | Via | Evidence |
| --- | --- | --- | --- |
| [sales.v_customers](../view/sales-v_customers-04f87d30.md) | view | feeds | linker |

### Downstream

| Object | Type | Via | Evidence |
| --- | --- | --- | --- |
| [Sales](../semantic_model/sales-0de5bea3.md) | semantic_model | feeds | Sales.SemanticModel/definition/tables/customer.tmdl |

## Business Intent

<!-- intent:begin -->
_Add a short description of what this object is for and who relies on it._
<!-- intent:end -->
