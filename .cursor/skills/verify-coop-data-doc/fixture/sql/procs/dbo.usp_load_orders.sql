CREATE OR ALTER PROCEDURE dbo.usp_load_orders
AS
BEGIN
    SET NOCOUNT ON;

    INSERT INTO dbo.orders (order_id, customer_id, order_total)
    SELECT
        c.customer_id,
        c.customer_id,
        CAST(0 AS decimal(18, 2))
    FROM dbo.customers AS c
    WHERE c.customer_name IS NOT NULL;
END
GO
