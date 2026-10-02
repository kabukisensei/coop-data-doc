---
id: "view:sales.v_products"
type: "view"
name: "v_products"
schema: "sales"
layer: ""
source_file: "views/v_products.sql"
path: "view/sales-v_products-c0708a19.md"
upstream_inputs:
  - "silver_table:dbo.v_products_raw"
downstream_dependents: []
tags:
  - "sales"
---

# sales.v_products

## Source

_`views/v_products.sql`_

```sql
CREATE VIEW sales.v_products AS SELECT id FROM dbo.v_products_raw;
```

## Structural Contract

| Column | Type | Constraints | Description | Source Columns |
| --- | --- | --- | --- | --- |
| id |  |  |  | `dbo.v_products_raw.id` |

## Lineage

### Upstream

| Object | Type | Via | Evidence |
| --- | --- | --- | --- |
| [dbo.v_products_raw](../silver_table/dbo-v_products_raw-f2196ba6.md) | silver_table | reads | views/v_products.sql |

### Downstream

_No downstream objects._

## Business Intent

<!-- intent:begin -->
_Add a short description of what this object is for and who relies on it._
<!-- intent:end -->
