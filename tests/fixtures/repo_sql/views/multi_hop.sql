CREATE TABLE db.base_table (
    id INT,
    value VARCHAR(50)
);
GO

CREATE VIEW db.view_hop_1 AS
SELECT
    id AS hop_1_id,
    value AS hop_1_val
FROM db.base_table;
GO

CREATE VIEW db.view_hop_2 AS
SELECT
    hop_1_id AS final_id,
    hop_1_val + '_suffix' AS final_val
FROM db.view_hop_1;
GO
