"""Column-level lineage: views/CTAS (column_lineage), proc and trigger DML
(dml_column_lineage), and the `lineage OBJECT --column COL` trace that walks them.

Until these tests, every column trace was silently empty: sqlglot 30 made
lineage()'s `schema` argument required, the call raised for every column, and
the broad except swallowed it.
"""

import json
from pathlib import Path

import sqlglot
from test_cli import run, setup_workspace
from test_sql_parsers import parse_all

from coop_data_doc.parsers.sql_common import extract_column_lineage


def _trace(sql: str) -> dict[str, list[str]]:
    return extract_column_lineage(sqlglot.parse_one(sql, read="tsql"), "tsql")


def test_qualified_columns_and_expressions():
    assert _trace(
        "SELECT c.id, c.first_name + ' ' + c.last_name AS full_name, r.name AS region, GETDATE() AS loaded "
        "FROM dbo.customer c JOIN dbo.region r ON r.id = c.region_id"
    ) == {
        "id": ["dbo.customer.id"],
        "full_name": ["dbo.customer.first_name", "dbo.customer.last_name"],
        "region": ["dbo.region.name"],
        # `loaded` reads no column: left out, never guessed
    }


def test_traces_through_ctes_and_subqueries():
    assert _trace(
        "WITH x AS (SELECT id, amount * 2 AS doubled FROM sales.orders) "
        "SELECT x.id, SUM(x.doubled) AS total FROM x GROUP BY x.id"
    ) == {"id": ["sales.orders.id"], "total": ["sales.orders.amount"]}
    assert _trace("SELECT s.id FROM (SELECT o.id FROM dbo.orders o) s") == {"id": ["dbo.orders.id"]}


def test_ambiguous_star_and_temp_sources_are_not_guessed():
    # an unqualified column over two tables could be either: no lineage
    assert _trace("SELECT id FROM dbo.a JOIN dbo.b ON a.k = b.k") == {}
    assert _trace("SELECT * FROM dbo.a") == {}
    assert _trace("SELECT id FROM #staged") == {}


def test_fixture_view_and_proc_column_lineage():
    graph, _ = parse_all()
    assert graph.nodes["view:sales.dim_customer"].metadata["column_lineage"] == {
        "customer_id": ["silver.customers.customer_id"],
        "customer_name": ["silver.customers.customer_name"],
        "latest_order_total": ["dbo.fact_sales.order_total"],
    }
    # WITH billable AS (… FROM silver.invoices) INSERT … SELECT … FROM billable:
    # traced through the CTE to the base table, never to `billable` itself
    proc = graph.nodes["stored_proc:dbo.usp_load_fact_sales"]
    assert proc.metadata["dml_column_lineage"] == {
        "gold_table:dbo.fact_sales": {
            "order_id": ["silver.invoices.invoice_id"],
            "customer_id": ["silver.invoices.customer_id"],
            "order_total": ["silver.invoices.order_total"],
        }
    }


def test_trigger_pseudo_table_columns_are_the_on_table():
    graph, _ = parse_all()
    trigger = graph.nodes["stored_proc:dbo.tr_fact_sales_audit"]
    assert trigger.metadata["dml_column_lineage"] == {
        "gold_table:audit.fact_sales_log": {"sale_id": ["dbo.fact_sales.sale_id"]}
    }


def test_lineage_column_cli_walks_proc_and_view(tmp_path: Path):
    setup_workspace(tmp_path)
    repo = tmp_path / "sql-repo"
    (repo / "views" / "v_invoice_total.sql").write_text(
        "CREATE VIEW dbo.v_invoice_total AS\n"
        "SELECT f.order_id, f.order_total * 1.2 AS gross FROM dbo.fact_sales f\nGO\n",
        encoding="utf-8",
    )
    assert run(["scan", "--non-interactive"], tmp_path).exit_code == 0
    # view column -> fact_sales.order_total -> (proc INSERT) -> silver.invoices.order_total
    result = run(["lineage", "dbo.v_invoice_total", "--column", "gross"], tmp_path)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["source_columns"] == ["silver.invoices.order_total"]
