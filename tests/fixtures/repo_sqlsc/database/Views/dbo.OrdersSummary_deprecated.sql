-- scripted before sqlsc 1.0.4: the view was renamed with sp_rename after it was created
CREATE VIEW dbo.OrdersSummary AS SELECT OrderId, Amount FROM dbo.Orders;
GO
