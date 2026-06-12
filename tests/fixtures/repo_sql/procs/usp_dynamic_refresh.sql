CREATE PROCEDURE dbo.usp_dynamic_refresh
AS
BEGIN
    DECLARE @sql NVARCHAR(MAX);
    SET @sql = N'UPDATE dbo.refresh_log SET refreshed_at = SYSUTCDATETIME()';
    EXEC sp_executesql @sql;
END
GO
