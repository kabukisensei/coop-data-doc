import json

from click.testing import CliRunner

from coop_data_doc.cli import cli
from coop_data_doc.config import Config
from coop_data_doc.graph import LineageGraph, Node, NodeType
from coop_data_doc.graph.diff import diff_graphs


def workspace(tmp_path, coverage=""):
    pbi = tmp_path / "pbi" / "Sales.SemanticModel" / "definition" / "tables"
    pbi.mkdir(parents=True)
    (pbi / "Orders.tmdl").write_text("""table Orders
    column id
        dataType: int64
    partition Orders = m
        source =
            let
                Source = Sql.Database("server", "db"),
                Data = Source{[Schema="dbo",Item="orders"]}[Data]
            in Data
""")
    config = tmp_path / "coop-data-doc.yml"
    config.write_text("repos:\n  powerbi:\n    path: pbi\n" + coverage)
    return config


def test_powerbi_only_reports_missing_sql_and_scoped_evidence(tmp_path):
    config = workspace(
        tmp_path, "coverage:\n  powerbi:\n    state: complete\n    scope: client PBIP sources\n"
    )
    result = CliRunner().invoke(cli, ["build", "--config", str(config), "--non-interactive", "--skip-html"])
    assert result.exit_code == 0, result.output
    graph = json.loads((tmp_path / "data-docs/graph.json").read_text())
    assert graph["coverage"]["sources"]["sql"]["availability"] == "missing"
    lineage = CliRunner().invoke(cli, ["lineage", "orders", "--config", str(config)])
    evidence = json.loads(lineage.output)["evidence"]
    assert evidence["impact_scope"] == "observed_graph"
    assert evidence["complete"] is False
    assert evidence["state"] == "unresolved"
    assert not any(n.startswith(("gold_table:", "view:")) for n in graph["nodes"])


def test_legacy_coverage_defaults_unknown(tmp_path):
    config = workspace(tmp_path)
    CliRunner().invoke(cli, ["scan", "--config", str(config), "--non-interactive"])
    graph = json.loads((tmp_path / "data-docs/graph.json").read_text())
    assert graph["coverage"]["sources"]["powerbi"]["declared"] == "unknown"


def test_strict_rejection_preserves_all_caches(tmp_path):
    sql = tmp_path / "sql"
    sql.mkdir()
    (sql / "broken.sql").write_text("CREATE PROCEDURE dbo.broken SELECT 1;")
    config = tmp_path / "coop-data-doc.yml"
    config.write_text("repos:\n  sql:\n    path: sql\n")
    cache = tmp_path / ".coop-data-doc-parse-cache.json"
    cache.write_bytes(b"previous cache")
    result = CliRunner().invoke(
        cli, ["scan", "--config", str(config), "--non-interactive", "--strict", "--jobs", "1"]
    )
    assert result.exit_code == 2, result.output
    assert cache.read_bytes() == b"previous cache"


def test_non_strict_omitted_file_cannot_prune_previous_generation(tmp_path, monkeypatch):
    config = workspace(tmp_path)
    runner = CliRunner()
    assert (
        runner.invoke(cli, ["build", "--config", str(config), "--non-interactive", "--skip-html"]).exit_code
        == 0
    )
    graph_path = tmp_path / "data-docs/graph.json"
    before = graph_path.read_bytes()
    monkeypatch.setattr("coop_data_doc.crawler.MAX_FILE_BYTES", 1)
    result = runner.invoke(cli, ["build", "--config", str(config), "--non-interactive", "--skip-html"])
    assert result.exit_code == 2, result.output
    assert graph_path.read_bytes() == before


def test_semantic_definitions_change_impact():
    old = LineageGraph()
    node = Node(
        id="measure:sales.total",
        node_type=NodeType.MEASURE,
        name="total",
        metadata={"dax": "SUM(Orders[Amount])"},
    )
    old.add_node(node)
    new = old.model_copy(deep=True)
    new.nodes[node.id].metadata["dax"] = "SUM(Orders[Net])"
    assert [n.id for n in diff_graphs(old, new).changed_nodes] == [node.id]


def test_partial_layer_declaration_round_trips_config_set(tmp_path):
    config = workspace(
        tmp_path, "coverage:\n  layer:gold:\n    state: partial\n    scope: mart scripts only\n"
    )
    result = CliRunner().invoke(
        cli, ["config-set", "--config", str(config), "--from-json", "-"], input='{"project_name":"Edited"}'
    )
    assert result.exit_code == 0, result.output
    assert Config.load(config).coverage["layer:gold"].state == "partial"


def test_impact_evidence_envelope_never_implies_zero_estate_impact(tmp_path):
    config = workspace(tmp_path)
    runner = CliRunner()
    assert runner.invoke(cli, ["scan", "--config", str(config), "--non-interactive"]).exit_code == 0
    graph_path = tmp_path / "data-docs/graph.json"
    result = runner.invoke(
        cli, ["impact", "--config", str(config), "--baseline", str(graph_path), "--evidence"]
    )
    payload = json.loads(result.output)
    assert payload["impacts"] == {}
    assert payload["schema_version"] == 2
    assert not payload["evidence"]["complete"]
    assert "no observed links" in payload["evidence"]["empty_result_means"]


def test_explicit_layer_gap_prevents_complete_evidence():
    from coop_data_doc.coverage import evidence_summary

    graph = LineageGraph(
        coverage={
            "observed": "scanned",
            "sources": {
                "sql": {"declared": "complete", "availability": "local", "files": 2},
                "powerbi": {"declared": "complete", "availability": "local", "files": 2},
            },
            "layers": {"gold": {"configured": True, "declared": "partial"}},
        }
    )
    assert evidence_summary(graph)["state"] == "partial"
    assert not evidence_summary(graph)["complete"]


def test_explicit_external_decision_remains_visible(tmp_path):
    config = workspace(tmp_path)
    cache = tmp_path / ".lineage-cache.json"
    cache.write_text(
        json.dumps(
            {"version": 1, "mappings": {"pbi_table:sales.orders": {"target": None, "method": "external"}}}
        )
    )
    runner = CliRunner()
    assert (
        runner.invoke(cli, ["build", "--config", str(config), "--non-interactive", "--skip-html"]).exit_code
        == 0
    )
    payload = json.loads(runner.invoke(cli, ["lineage", "orders", "--config", str(config)]).output)
    assert "external" in payload["evidence"]["states"]
    assert payload["upstream"] == []
    assert "zero impact" in (tmp_path / "data-docs/index.md").read_text()


def test_report_cannot_hide_estate_upstream_limitations():
    from coop_data_doc.coverage import evidence_summary

    graph = LineageGraph(
        coverage={
            "observed": "scanned",
            "sources": {
                "sql": {"declared": "complete", "availability": "local", "files": 2},
                "powerbi": {"declared": "complete", "availability": "local", "files": 2},
            },
        }
    )
    report = Node(id="report:sales", name="sales", node_type=NodeType.REPORT)
    graph.add_node(report)
    graph.add_node(
        Node(
            id="pbi_table:sales.orders",
            name="orders",
            node_type=NodeType.PBI_TABLE,
            metadata={"unresolved": True},
        )
    )
    assert not evidence_summary(graph, report)["complete"]
    assert evidence_summary(graph, report)["state"] == "unresolved"


def test_resolution_bookkeeping_is_not_a_change():
    old = LineageGraph()
    node = Node(
        id="pbi_table:sales.orders",
        node_type=NodeType.PBI_TABLE,
        name="orders",
        schema_name="sales",
        metadata={
            "source_resolutions": {"k": {"target": "view:dbo.v", "method": "interactive", "source": "dbo.v"}}
        },
    )
    old.add_node(node)
    new = old.model_copy(deep=True)
    new.nodes[node.id].metadata["source_resolutions"]["k"]["method"] = "cache"
    new.nodes[node.id].metadata["cache_source_unverified"] = True
    assert diff_graphs(old, new).changed_nodes == []
    new.nodes[node.id].metadata["definition_hash"] = "changed"
    assert [n.id for n in diff_graphs(old, new).changed_nodes] == [node.id]


def test_lineage_names_the_tables_that_load_an_undocumented_view(tmp_path):
    """A SQL-less estate: the view the model's partition names is not documented,
    so `lineage dbo.orders` matches no node; it still answers with the Power BI
    tables that load it, by name, flagged as an undocumented source."""
    config = workspace(tmp_path)
    assert CliRunner().invoke(cli, ["scan", "--config", str(config), "--non-interactive"]).exit_code == 0
    res = CliRunner().invoke(cli, ["lineage", "dbo.orders", "--config", str(config)])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["object"] is None
    assert data["undocumented_source"] is True
    assert [hit["table"]["id"] for hit in data["loaded_by"]] == ["pbi_table:sales.orders"]
    assert data["loaded_by"][0] == {
        "table": data["downstream"][0],
        "source": "dbo.orders",
        "linked": False,
    }
    assert data["upstream"] == []
    assert data["evidence"]["complete"] is False
    # the bare name resolves to the pbi_table itself, which still reports the view it loads
    bare = json.loads(CliRunner().invoke(cli, ["lineage", "orders", "--config", str(config)]).output)
    assert bare["object"]["id"] == "pbi_table:sales.orders"
    assert [hit["source"] for hit in bare["loaded_by"]] == ["dbo.orders"]
    assert bare["loaded_by"][0]["linked"] is False
    # a name nothing loads is still an error
    missing = CliRunner().invoke(cli, ["lineage", "dbo.nothing", "--config", str(config)])
    assert missing.exit_code != 0
    assert "no object matching" in missing.output
