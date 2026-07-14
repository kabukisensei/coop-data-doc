"""Coverage for the legacy model.bim (Tabular Object Model JSON) parser.

The BIM parser mirrors the TMDL parser's contract — same node/edge shapes,
same partition-source attachment, same merged relationship records — sourced
from the legacy JSON format that Windows estates still export. The fixture is
a miniature real model (tests/fixtures/repo_bim) per the repo's
fixture-driven-test rule.
"""

import json
from pathlib import Path

from coop_data_doc.config import Config, RepoConfig
from coop_data_doc.crawler import FileEntry, FileKind, crawl
from coop_data_doc.graph import LineageGraph, to_json_str
from coop_data_doc.parsers.bim import parse_bim

FIXTURES = Path(__file__).parent / "fixtures"

MODEL_KEY = "legacy model"  # normalized from the .bim's "name": "Legacy Model"


def bim_entries() -> list[FileEntry]:
    config = Config(repos={"powerbi": RepoConfig(path=str(FIXTURES / "repo_bim"), include=["**/*.bim"])})
    inventory, warnings = crawl(config)
    assert warnings == []
    entries = inventory.by_kind(FileKind.BIM)
    assert [entry.path for entry in entries] == ["legacy_model.bim"]
    return entries


def parse_fixture() -> tuple[LineageGraph, list]:
    graph = LineageGraph()
    warnings = parse_bim(bim_entries(), graph)
    return graph, warnings


def edge_keys(graph: LineageGraph) -> set[tuple[str, str, str]]:
    return {edge.key() for edge in graph.edges}


def entry_for(path: Path) -> FileEntry:
    return FileEntry(
        path=path.name,
        abs_path=str(path),
        repo_key="powerbi",
        kind=FileKind.BIM,
        size=path.stat().st_size,
    )


def test_bim_model_structure():
    graph, warnings = parse_fixture()
    assert warnings == []
    # model name comes from the top-level "name", not the filename
    assert f"semantic_model:{MODEL_KEY}" in graph.nodes
    keys = edge_keys(graph)
    for table in ("fact_sales", "dim_customer"):
        node_id = f"pbi_table:{MODEL_KEY}.{table}"
        assert node_id in graph.nodes
        assert (node_id, f"semantic_model:{MODEL_KEY}", "feeds") in keys
    fact = graph.nodes[f"pbi_table:{MODEL_KEY}.fact_sales"]
    assert fact.metadata["description"] == "All booked sales orders."
    assert fact.source_file == "legacy_model.bim"


def test_bim_columns():
    graph, _ = parse_fixture()
    fact_cols = {
        c.name: (c.data_type, c.description) for c in graph.nodes[f"pbi_table:{MODEL_KEY}.fact_sales"].columns
    }
    assert fact_cols == {
        "order_id": ("int64", "Surrogate key."),
        "customer_id": ("int64", ""),
        "order_total": ("decimal", ""),
    }
    dim_cols = {c.name: c.data_type for c in graph.nodes[f"pbi_table:{MODEL_KEY}.dim_customer"].columns}
    assert dim_cols == {"customer_id": "int64", "customer_name": "string"}


def test_bim_partition_sources_and_storage_mode():
    graph, _ = parse_fixture()
    fact = graph.nodes[f"pbi_table:{MODEL_KEY}.fact_sales"]
    # list-form M expression (the BIM multi-line style) joined and parsed
    assert fact.metadata["partition_source"] == {
        "schema": "sales",
        "object": "fact_sales",
        "raw_kind": "sql_database",
    }
    assert fact.metadata["storage_mode"] == "import"
    dim = graph.nodes[f"pbi_table:{MODEL_KEY}.dim_customer"]
    # string-form M expression parsed the same way
    assert dim.metadata["partition_source"] == {
        "schema": "sales",
        "object": "dim_customer",
        "raw_kind": "sql_database",
    }


def test_bim_measures():
    graph, _ = parse_fixture()
    keys = edge_keys(graph)
    total = graph.nodes[f"measure:{MODEL_KEY}.total sales"]
    assert total.metadata["dax"] == "SUM(fact_sales[order_total])"
    assert total.metadata["description"] == "Grand total of all orders."
    assert (total.id, f"semantic_model:{MODEL_KEY}", "feeds") in keys
    # list-form DAX expression is joined with newlines; no description key
    count = graph.nodes[f"measure:{MODEL_KEY}.order count"]
    assert count.metadata["dax"] == "COUNTROWS(\n    fact_sales\n)"
    assert "description" not in count.metadata
    # link_measures wires the DAX table reference into the graph
    assert (total.id, f"pbi_table:{MODEL_KEY}.fact_sales", "references") in keys


def test_bim_measure_home_table_marker():
    # a BIM table with measures and only a hidden column is a measure home table
    # (parity with the TMDL parser) — issue #27.
    graph, _ = parse_fixture()
    assert graph.nodes[f"pbi_table:{MODEL_KEY}.calculations"].metadata.get("measure_table") is True
    assert "measure_table" not in graph.nodes[f"pbi_table:{MODEL_KEY}.fact_sales"].metadata
    assert "measure_table" not in graph.nodes[f"pbi_table:{MODEL_KEY}.dim_customer"].metadata


def test_bim_relationships_merged_with_flags():
    graph, _ = parse_fixture()
    relationships = graph.nodes[f"semantic_model:{MODEL_KEY}"].metadata["relationships"]
    # BIM omits isActive when true; crossFilteringBehavior "bothDirections"
    # maps to bidirectional — merged and sorted by (from, to) like TMDL
    assert relationships == [
        {
            "from": "fact_sales.customer_id",
            "to": "dim_customer.customer_id",
            "active": True,
            "bidirectional": True,
        },
        {
            "from": "fact_sales.order_id",
            "to": "dim_customer.customer_id",
            "active": False,
            "bidirectional": False,
        },
    ]


def test_bim_calculated_partition_lineage():
    # issue #30: a BIM "calculated" partition gets references edges to the
    # tables/measures its DAX derives from, plus the trust markers — parity
    # with the TMDL parser.
    graph, warnings = parse_fixture()
    assert warnings == []
    summary = graph.nodes[f"pbi_table:{MODEL_KEY}.sales summary"]
    assert summary.metadata["partition_calculated"] is True
    assert summary.metadata["dax_refs_heuristic"] is True
    keys = edge_keys(graph)
    assert (summary.id, f"pbi_table:{MODEL_KEY}.fact_sales", "references") in keys
    assert (summary.id, f"measure:{MODEL_KEY}.total sales", "references") in keys
    assert "partition_source_unresolved" not in summary.metadata


def test_bim_query_partition_native_sql():
    # issue #30: a legacy "query" partition carries native SQL — same lineage
    # extraction as Value.NativeQuery.
    graph, _ = parse_fixture()
    orders = graph.nodes[f"pbi_table:{MODEL_KEY}.legacy_orders"]
    assert orders.metadata["native_query_tables"] == ["sales.dim_customer"]
    assert orders.metadata["partition_source"] == {
        "schema": "sales",
        "object": "dim_customer",
        "raw_kind": "native_query",
    }


def test_bim_unknown_partition_type_flagged(tmp_path: Path):
    # issue #30: an unrecognized BIM partition source type must be marked
    # unresolved and warned — never silent (hard rule 4).
    path = tmp_path / "weird.bim"
    path.write_text(
        json.dumps(
            {
                "name": "Weird",
                "model": {
                    "tables": [
                        {
                            "name": "mystery",
                            "columns": [{"name": "id", "dataType": "int64"}],
                            "partitions": [
                                {"name": "mystery-p", "mode": "import", "source": {"type": "entity"}}
                            ],
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    graph = LineageGraph()
    warnings = parse_bim([entry_for(path)], graph)
    table = graph.nodes["pbi_table:weird.mystery"]
    assert table.metadata.get("partition_source_unresolved") is True
    assert [w.category for w in warnings] == ["unresolved_partition_source"]
    assert "mystery" in warnings[0].message


def test_bim_model_name_falls_back_to_file_stem(tmp_path: Path):
    path = tmp_path / "Solo Model.bim"
    path.write_text('{"model": {"tables": []}}', encoding="utf-8")
    graph = LineageGraph()
    warnings = parse_bim([entry_for(path)], graph)
    assert warnings == []
    assert "semantic_model:solo model" in graph.nodes
    assert graph.nodes["semantic_model:solo model"].display_name == "Solo Model"


def test_bim_malformed_json_warns_and_skips(tmp_path: Path):
    # a truncated/corrupt export must degrade to a bim_parse warning naming
    # the file — never crash the pipeline, never add half-parsed nodes
    path = tmp_path / "broken.bim"
    path.write_text('{"name": "Broken", "model": {', encoding="utf-8")
    graph = LineageGraph()
    warnings = parse_bim([entry_for(path)], graph)
    assert [w.category for w in warnings] == ["bim_parse"]
    assert warnings[0].file == "broken.bim"
    assert graph.nodes == {}
    assert graph.edges == []


def test_bim_top_level_array_warns_and_skips(tmp_path: Path):
    # a .bim whose top level is a JSON list (valid JSON, wrong shape) must
    # degrade to a bim_parse warning — previously it crashed the pipeline with
    # AttributeError at data.get("model") (hard rule 3, issue #41).
    path = tmp_path / "listy.bim"
    path.write_text(json.dumps([1, 2]), encoding="utf-8")
    graph = LineageGraph()
    warnings = parse_bim([entry_for(path)], graph)  # must not raise
    assert [w.category for w in warnings] == ["bim_parse"]
    assert warnings[0].file == "listy.bim"
    assert graph.nodes == {}
    assert graph.edges == []


def test_bim_non_dict_model_and_entries_degrade_to_warnings(tmp_path: Path):
    # non-object values where TOM objects belong — "model" itself, and
    # tables/partitions/measures/relationships entries — are each skipped with
    # a bim_parse warning instead of raising (hard rule 3, issue #41).
    path = tmp_path / "modelstr.bim"
    path.write_text(json.dumps({"name": "M", "model": "nope"}), encoding="utf-8")
    graph = LineageGraph()
    warnings = parse_bim([entry_for(path)], graph)
    assert [w.category for w in warnings] == ["bim_parse"]
    assert "semantic_model:m" in graph.nodes  # the model node still exists, just empty

    path = tmp_path / "entries.bim"
    path.write_text(
        json.dumps(
            {
                "name": "M2",
                "model": {
                    "tables": [
                        "oops",  # non-dict table entry
                        {
                            "name": "good",
                            "columns": [{"name": "id", "dataType": "int64"}, "oops"],
                            "partitions": ["oops"],
                            "measures": ["oops"],
                        },
                    ],
                    "relationships": ["oops"],
                },
            }
        ),
        encoding="utf-8",
    )
    graph = LineageGraph()
    warnings = parse_bim([entry_for(path)], graph)
    # one warning per malformed table/partition/measure/relationship entry
    # (non-dict columns are skipped quietly — they carry no name to report)
    assert [w.category for w in warnings] == ["bim_parse"] * 4
    good = graph.nodes["pbi_table:m2.good"]
    assert [c.name for c in good.columns] == ["id"]  # the good column survived


def test_bim_determinism():
    first, _ = parse_fixture()
    second, _ = parse_fixture()
    assert to_json_str(first) == to_json_str(second)
