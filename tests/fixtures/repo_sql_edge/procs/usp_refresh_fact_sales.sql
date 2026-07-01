CREATE PROCEDURE dbo.usp_refresh_fact_sales
AS
BEGIN
    INSERT INTO dbo.load_log (run_date) VALUES (GETDATE())

    UPDATE fs
    SET fs.order_total = o.order_total
    FROM dbo.fact_sales fs
    JOIN dbo.stg_orders o ON o.order_id = fs.order_id
    JOIN dbo.dim_date d ON d.date_key = o.date_key

    INSERT INTO dbo.fact_sales (order_id, order_total)
    SELECT order_id, order_total FROM dbo.stg_orders

    DELETE FROM dbo.stg_orders
END
GO
