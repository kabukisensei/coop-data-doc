---
id: "view:sales.v_orders_2024"
type: "view"
name: "v_orders_2024"
schema: "sales"
layer: ""
source_file: "views/v_orders_2024.sql"
path: "view/sales-v_orders_2024-b1ce610f.md"
upstream_inputs:
  - "silver_table:dbo.v_orders_2024_raw"
downstream_dependents: []
tags:
  - "sales"
---

# sales.v_orders_2024

## Source

_`views/v_orders_2024.sql`_

```sql
CREATE VIEW sales.v_orders_2024 AS SELECT id FROM dbo.v_orders_2024_raw;
```

## Structural Contract

| Column | Type | Constraints | Description | Source Columns |
| --- | --- | --- | --- | --- |
| id |  |  |  | `dbo.v_orders_2024_raw.id` |

## Lineage

### Upstream

| Object | Type | Via | Evidence |
| --- | --- | --- | --- |
| [dbo.v_orders_2024_raw](../silver_table/dbo-v_orders_2024_raw-9613b6f9.md) | silver_table | reads | views/v_orders_2024.sql |

### Downstream

_No downstream objects._

## Business Intent

<!-- intent:begin -->
_Add a short description of what this object is for and who relies on it._
<!-- intent:end -->
