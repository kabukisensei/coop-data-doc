CREATE PROCEDURE dbo.usp_cursor_legacy
AS
BEGIN
    DECLARE @event_id INT;

    DECLARE event_cur CURSOR FOR
        SELECT event_id FROM silver.events WHERE processed = 0;

    OPEN event_cur;
    FETCH NEXT FROM event_cur INTO @event_id;

    WHILE @@FETCH_STATUS = 0
    BEGIN
        INSERT INTO dbo.audit_log (event_id, logged_at)
        SELECT e.event_id, e.created_at
        FROM silver.events AS e
        WHERE e.event_id = @event_id;

        FETCH NEXT FROM event_cur INTO @event_id;
    END

    CLOSE event_cur;
    DEALLOCATE event_cur;
END
GO
