CREATE TABLE dbo.agg_sales_daily AS
SELECT
    f.load_date AS sales_date,
    SUM(f.order_total) AS total_sales
FROM dbo.fact_sales AS f
GROUP BY f.load_date;
GO
