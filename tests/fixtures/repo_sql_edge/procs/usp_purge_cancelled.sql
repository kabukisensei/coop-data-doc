CREATE PROCEDURE dbo.usp_purge_cancelled
AS
BEGIN
    DELETE o
    FROM dbo.orders AS o
    JOIN dbo.archive_flags AS f ON f.order_id = o.order_id
    WHERE f.archived = 1;
END
GO
