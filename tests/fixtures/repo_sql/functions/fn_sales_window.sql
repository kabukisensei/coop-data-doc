CREATE FUNCTION dbo.fn_sales_window (@from DATE, @to DATE)
RETURNS TABLE
AS
RETURN
    SELECT
        s.order_id,
        s.order_total,
        c.customer_name
    FROM dbo.fact_sales AS s
    JOIN silver.customers AS c
        ON c.customer_id = s.customer_id
    WHERE s.load_date >= @from
      AND s.load_date < @to;
GO
