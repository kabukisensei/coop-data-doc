CREATE VIEW sales.v_sales_window_recent
AS
SELECT
    w.order_id,
    w.customer_name
FROM dbo.fn_sales_window('2024-01-01', '2024-12-31') AS w
WHERE w.order_total > 0;
GO
