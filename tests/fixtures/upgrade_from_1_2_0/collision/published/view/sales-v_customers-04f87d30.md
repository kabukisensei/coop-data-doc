---
id: "view:sales.v_customers"
type: "view"
name: "v_customers"
schema: "sales"
layer: ""
source_file: "views/v_customers.sql"
path: "view/sales-v_customers-04f87d30.md"
upstream_inputs:
  - "silver_table:dbo.v_customers_raw"
downstream_dependents:
  - "pbi_table:sales.basket"
  - "pbi_table:sales.customer"
  - "pbi_table:sales.legacy_customer"
tags:
  - "sales"
---

# sales.v_customers

## Source

_`views/v_customers.sql`_

```sql
CREATE VIEW sales.v_customers AS SELECT id FROM dbo.v_customers_raw;
```

## Structural Contract

| Column | Type | Constraints | Description | Source Columns |
| --- | --- | --- | --- | --- |
| id |  |  |  | `dbo.v_customers_raw.id` |

## Upstream lineage

_Trace this object back to its sources. Each node links to its page; branches start collapsed in the HTML — expand to drill down._

- [dbo.v_customers_raw](../silver_table/dbo-v_customers_raw-0efbb0b9.md) `silver_table`

## Lineage

### Upstream

| Object | Type | Via | Evidence |
| --- | --- | --- | --- |
| [dbo.v_customers_raw](../silver_table/dbo-v_customers_raw-0efbb0b9.md) | silver_table | reads | views/v_customers.sql |

### Downstream

| Object | Type | Via | Evidence |
| --- | --- | --- | --- |
| [sales.basket](../pbi_table/sales-basket-e47e18bb.md) | pbi_table | feeds | linker |
| [sales.customer](../pbi_table/sales-customer-b08c88bf.md) | pbi_table | feeds | linker |
| [sales.legacy_customer](../pbi_table/sales-legacy_customer-ed447519.md) | pbi_table | feeds | linker |

## Business Intent

<!-- intent:begin -->
_Add a short description of what this object is for and who relies on it._
<!-- intent:end -->
