CREATE TABLE dbo.fact_sales (
    order_id        INT             NOT NULL PRIMARY KEY,
    customer_id     INT             NOT NULL,
    customer_name   NVARCHAR(200)   NULL,
    order_total     DECIMAL(18, 2)  NOT NULL,
    load_date       DATETIME2(3)    NOT NULL DEFAULT SYSUTCDATETIME()
);
GO
