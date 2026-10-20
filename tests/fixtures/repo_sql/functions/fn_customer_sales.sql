-- Scalar function: one customer's total sales.
CREATE FUNCTION dbo.fn_customer_sales (@customer_id INT)
RETURNS DECIMAL(18, 2)
AS
BEGIN
    RETURN (SELECT SUM(amount) FROM dbo.fact_sales WHERE customer_id = @customer_id);
END
GO
