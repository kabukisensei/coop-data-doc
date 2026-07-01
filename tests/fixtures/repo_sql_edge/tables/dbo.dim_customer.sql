-- Table refreshed nightly by CREATE PROCEDURE dbo.usp_load_dim_customer
CREATE TABLE dbo.dim_customer (
    customer_id     INT             NOT NULL PRIMARY KEY,
    customer_name   NVARCHAR(200)   NULL
);
GO
