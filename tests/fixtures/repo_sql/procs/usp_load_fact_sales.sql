CREATE OR ALTER PROCEDURE dbo.usp_load_fact_sales
AS
BEGIN
    SET NOCOUNT ON;

    SELECT o.order_id, o.customer_id, o.order_total
    INTO #staged_orders
    FROM silver.sales_orders AS o
    WHERE o.order_status = 'closed';

    WITH ranked_customers AS (
        SELECT
            c.customer_id,
            c.customer_name,
            ROW_NUMBER() OVER (PARTITION BY c.customer_id ORDER BY c.valid_from DESC) AS rn
        FROM silver.customers AS c
    )
    MERGE INTO dbo.fact_sales AS tgt
    USING (
        SELECT s.order_id, s.customer_id, s.order_total, rc.customer_name
        FROM #staged_orders AS s
        INNER JOIN ranked_customers AS rc
            ON rc.customer_id = s.customer_id AND rc.rn = 1
    ) AS src
        ON tgt.order_id = src.order_id
    WHEN MATCHED THEN
        UPDATE SET tgt.order_total = src.order_total,
                   tgt.customer_name = src.customer_name
    WHEN NOT MATCHED THEN
        INSERT (order_id, customer_id, customer_name, order_total, load_date)
        VALUES (src.order_id, src.customer_id, src.customer_name, src.order_total, SYSUTCDATETIME());

    UPDATE f
    SET f.customer_name = c.customer_name
    FROM dbo.fact_sales AS f
    INNER JOIN silver.customers AS c
        ON c.customer_id = f.customer_id
    WHERE f.customer_name IS NULL;

    -- WITH ... INSERT INTO ... SELECT: sqlglot hangs the CTE block off the
    -- Insert node, so silver.invoices (read only inside the CTE) must still be
    -- traced and the CTE name `billable` must NOT leak as a phantom table.
    WITH billable AS (
        SELECT invoice_id, customer_id, order_total
        FROM silver.invoices
        WHERE status = 'billable'
    )
    INSERT INTO dbo.fact_sales (order_id, customer_id, order_total)
    SELECT invoice_id, customer_id, order_total FROM billable;

    DROP TABLE #staged_orders;

    EXEC dbo.usp_audit_load;
END
GO
