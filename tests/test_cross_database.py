"""A three-part reference to a database a live source snapshots joins the object
that source documents (cross_database.link). Files only: no database."""

from __future__ import annotations

import json
from pathlib import Path

from coop_data_doc.cli import run_pipeline
from coop_data_doc.config import Config

CONFIG = """\
project_name: Cross database
repos:
  warehouse: {path: ./live/warehouse, include: ["**/*.sql"]}
  lakehouse: {path: ./live/lakehouse, include: ["**/*.sql"]}
live_sources:
  dev_warehouse: {server: wh.example, database: Coop Dev, repo: warehouse, environment: dev}
  dev_lakehouse: {server: lh.example, database: Coop_Dev_LH, repo: lakehouse, environment: dev}
output: {dir: ./data-docs, site_dir: ./data-docs-site}
"""

LH_TABLE = "CREATE TABLE [dbo].[customers] (\n    [customer_id] int NULL\n);\n"
WH_VIEW = "CREATE VIEW sales.v_lh_customers AS SELECT customer_id FROM coop_dev_lh.dbo.customers;\n"


def estate(tmp_path: Path, files: dict[str, str]) -> Config:
    (tmp_path / "coop-data-doc.yml").write_text(CONFIG, encoding="utf-8", newline="\n")
    for folder in ("live/warehouse", "live/lakehouse"):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    return Config.load(tmp_path / "coop-data-doc.yml")


def build(config: Config):
    graph, _, warnings = run_pipeline(config, interactive=False, jobs=1)
    ids = {(n.schema_name, n.name): node_id for node_id, n in graph.nodes.items()}
    flows = {e.flow() for e in graph.edges}
    return graph, ids, flows, warnings


def categories(warnings) -> list[str]:
    return sorted(w.category for w in warnings if w.category.startswith(("cross_", "live_", "catalog_")))


def test_a_warehouse_view_joins_the_lakehouse_table_it_reads(tmp_path: Path):
    deps = {
        "version": 1,
        "dependencies": [
            {
                "object": {"schema": "sales", "name": "v_lh_customers", "type": "view"},
                "references": {"schema": "dbo", "name": "customers", "type": None, "database": "coop_dev_lh"},
            }
        ],
    }
    config = estate(
        tmp_path,
        {
            "live/lakehouse/Tables/dbo.customers.sql": LH_TABLE,
            "live/warehouse/Views/sales.v_lh_customers.sql": WH_VIEW,
            "live/warehouse/Dependencies.json": json.dumps(deps),
        },
    )
    graph, ids, flows, warnings = build(config)

    assert ("coop_dev_lh.dbo", "customers") not in ids  # no placeholder left
    table = ids[("dbo", "customers")]
    assert graph.nodes[table].source_file == "Tables/dbo.customers.sql"
    assert (table, ids[("sales", "v_lh_customers")]) in flows
    # the database's own list names the three-part reference: it counts as linked
    assert categories(warnings) == []


def test_a_database_no_live_source_names_stays_a_placeholder(tmp_path: Path):
    config = estate(
        tmp_path,
        {
            "live/lakehouse/Tables/dbo.customers.sql": LH_TABLE,
            "live/warehouse/Views/sales.v_other.sql": (
                "CREATE VIEW sales.v_other AS SELECT customer_id FROM otherdb.dbo.customers;\n"
            ),
        },
    )
    _, ids, flows, warnings = build(config)
    assert (ids[("otherdb.dbo", "customers")], ids[("sales", "v_other")]) in flows
    assert (ids[("dbo", "customers")], ids[("sales", "v_other")]) not in flows
    assert categories(warnings) == []


def test_an_object_the_live_source_does_not_hold_stays_a_placeholder(tmp_path: Path):
    # dbo.customers exists, but in the Warehouse: the Lakehouse reference is not it
    config = estate(
        tmp_path,
        {
            "live/warehouse/Tables/dbo.customers.sql": LH_TABLE,
            "live/warehouse/Views/sales.v_lh_customers.sql": WH_VIEW,
        },
    )
    _, ids, flows, warnings = build(config)
    assert (ids[("coop_dev_lh.dbo", "customers")], ids[("sales", "v_lh_customers")]) in flows
    assert categories(warnings) == ["cross_database_ambiguous"]


def test_the_same_name_in_both_live_sources_is_reported_and_not_joined(tmp_path: Path):
    config = estate(
        tmp_path,
        {
            "live/lakehouse/Tables/dbo.customers.sql": LH_TABLE,
            "live/warehouse/Tables/dbo.customers.sql": LH_TABLE,
            "live/warehouse/Views/sales.v_lh_customers.sql": WH_VIEW,
        },
    )
    _, ids, _, warnings = build(config)
    assert ("coop_dev_lh.dbo", "customers") in ids
    assert categories(warnings) == ["cross_database_ambiguous", "live_source_name_collision"]
    collision = next(w for w in warnings if w.category == "live_source_name_collision")
    assert "dev_lakehouse, dev_warehouse" in collision.message


def test_a_linked_server_name_is_left_alone(tmp_path: Path):
    config = estate(
        tmp_path,
        {
            "live/lakehouse/Tables/dbo.customers.sql": LH_TABLE,
            "live/warehouse/Views/sales.v_srv.sql": (
                "CREATE VIEW sales.v_srv AS SELECT customer_id FROM srv.coop_dev_lh.dbo.customers;\n"
            ),
        },
    )
    _, ids, _, warnings = build(config)
    assert ("srv.coop_dev_lh.dbo", "customers") in ids
    assert categories(warnings) == []


def test_a_config_without_live_sources_is_untouched(tmp_path: Path):
    config = estate(
        tmp_path,
        {
            "live/lakehouse/Tables/dbo.customers.sql": LH_TABLE,
            "live/warehouse/Views/sales.v_lh_customers.sql": WH_VIEW,
        },
    )
    config.live_sources = {}
    _, ids, _, warnings = build(config)
    assert ("coop_dev_lh.dbo", "customers") in ids
    assert categories(warnings) == []
