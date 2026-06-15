CREATE OR ALTER VIEW sales.dim_customer
AS
SELECT
    c.customer_id,
    c.customer_name,
    f.order_total AS latest_order_total
FROM silver.customers AS c
LEFT JOIN dbo.fact_sales AS f
    ON f.customer_id = c.customer_id;
GO
