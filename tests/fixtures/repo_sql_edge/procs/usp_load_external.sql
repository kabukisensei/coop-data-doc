CREATE PROCEDURE dbo.usp_load_external
AS
BEGIN
    INSERT INTO dbo.fact_sales (order_id)
    SELECT order_id FROM OtherDb.dbo.customers;

    INSERT INTO dbo.fact_sales (order_id)
    SELECT order_id FROM LNKSRV.OtherDb.dbo.customers;
END
GO
