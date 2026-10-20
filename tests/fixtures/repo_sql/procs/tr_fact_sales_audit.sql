-- Audit trigger: every changed fact row is copied to the audit table.
CREATE TRIGGER dbo.tr_fact_sales_audit ON dbo.fact_sales
WITH EXECUTE AS OWNER
AFTER INSERT, UPDATE
AS
BEGIN
    SET NOCOUNT ON;
    INSERT INTO audit.fact_sales_log (sale_id, changed_at)
    SELECT i.sale_id, SYSUTCDATETIME() FROM inserted i;
END
GO
