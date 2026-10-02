---
id: "pbi_table:sales.scratch"
type: "pbi_table"
name: "scratch"
schema: "sales"
layer: ""
source_file: "Sales.SemanticModel/definition/tables/scratch.tmdl"
path: "pbi_table/sales-scratch-e67957f1.md"
upstream_inputs: []
downstream_dependents:
  - "semantic_model:sales"
tags:
  - "sales"
---

# sales.scratch

**Storage mode:** Import

## Structural Contract

| Column | Type | Constraints | Description |
| --- | --- | --- | --- |
| id | int64 |  |  |

## Lineage

### Upstream

_No upstream objects._

### Downstream

| Object | Type | Via | Evidence |
| --- | --- | --- | --- |
| [Sales](../semantic_model/sales-0de5bea3.md) | semantic_model | feeds | Sales.SemanticModel/definition/tables/scratch.tmdl |

## Business Intent

<!-- intent:begin -->
_Add a short description of what this object is for and who relies on it._
<!-- intent:end -->
