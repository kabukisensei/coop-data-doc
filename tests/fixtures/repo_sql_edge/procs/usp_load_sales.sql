CREATE PROCEDURE dbo.usp_load_sales
    @Year INT = 2024,      -- fiscal year (see wiki
    @Rebuild BIT = 0,      -- 1 = full rebuild
    @Prefix NVARCHAR(5) = N'('
AS
BEGIN
    INSERT INTO dbo.fact_sales (order_id)
    SELECT order_id FROM stg.sales;
END
GO
