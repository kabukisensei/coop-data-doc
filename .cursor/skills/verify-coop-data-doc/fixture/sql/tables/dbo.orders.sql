CREATE TABLE dbo.orders (
    order_id      INT            NOT NULL PRIMARY KEY,
    customer_id   INT            NOT NULL,
    order_total   DECIMAL(18, 2) NOT NULL
);
GO
