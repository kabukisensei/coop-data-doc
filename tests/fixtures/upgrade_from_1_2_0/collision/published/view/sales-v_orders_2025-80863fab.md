---
id: "view:sales.v_orders_2025"
type: "view"
name: "v_orders_2025"
schema: "sales"
layer: ""
source_file: "views/v_orders_2025.sql"
path: "view/sales-v_orders_2025-80863fab.md"
upstream_inputs:
  - "silver_table:dbo.v_orders_2025_raw"
downstream_dependents:
  - "pbi_table:sales.orders"
tags:
  - "sales"
---

# sales.v_orders_2025

## Source

_`views/v_orders_2025.sql`_

```sql
CREATE VIEW sales.v_orders_2025 AS SELECT id FROM dbo.v_orders_2025_raw;
```

## Structural Contract

| Column | Type | Constraints | Description | Source Columns |
| --- | --- | --- | --- | --- |
| id |  |  |  | `dbo.v_orders_2025_raw.id` |

## Upstream lineage

_Trace this object back to its sources. Each node links to its page; branches start collapsed in the HTML — expand to drill down._

- [dbo.v_orders_2025_raw](../silver_table/dbo-v_orders_2025_raw-ef321295.md) `silver_table`

## Lineage

### Upstream

| Object | Type | Via | Evidence |
| --- | --- | --- | --- |
| [dbo.v_orders_2025_raw](../silver_table/dbo-v_orders_2025_raw-ef321295.md) | silver_table | reads | views/v_orders_2025.sql |

### Downstream

| Object | Type | Via | Evidence |
| --- | --- | --- | --- |
| [sales.orders](../pbi_table/sales-orders-ca4a47cd.md) | pbi_table | feeds | linker |

## Business Intent

<!-- intent:begin -->
_Add a short description of what this object is for and who relies on it._
<!-- intent:end -->
