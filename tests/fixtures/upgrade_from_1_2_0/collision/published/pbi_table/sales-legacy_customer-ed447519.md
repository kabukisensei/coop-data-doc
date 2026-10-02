---
id: "pbi_table:sales.legacy_customer"
type: "pbi_table"
name: "legacy_customer"
schema: "sales"
layer: ""
source_file: "Archive/Sales.SemanticModel/definition/tables/legacy_customer.tmdl"
path: "pbi_table/sales-legacy_customer-ed447519.md"
upstream_inputs:
  - "view:sales.v_customers"
downstream_dependents:
  - "semantic_model:sales"
tags:
  - "sales"
---

# sales.legacy_customer

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
| [Sales](../semantic_model/sales-0de5bea3.md) | semantic_model | feeds | Archive/Sales.SemanticModel/definition/tables/legacy_customer.tmdl |

## Business Intent

<!-- intent:begin -->
Archived customer list kept for audit. Owner: finance.
<!-- intent:end -->
