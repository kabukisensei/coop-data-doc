CREATE OR ALTER VIEW sales.dim_customer
AS
SELECT
    c.customer_id,
    c.customer_name
FROM dbo.customers AS c;
GO
