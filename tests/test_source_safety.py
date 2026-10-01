from pathlib import Path
from xml.etree import ElementTree

import pytest

from coop_data_doc.config import Config, ConfigError, ParseWarning
from coop_data_doc.diagnostics import Diagnostics
from coop_data_doc.graph import LineageGraph, Node, NodeType
from coop_data_doc.render.estate_map import generate_estate_map_svg


@pytest.mark.parametrize("output_key", ["dir", "site_dir"])
@pytest.mark.parametrize("location", ["sql", ".", "sql/generated"])
def test_outputs_cannot_overlap_sources(tmp_path, output_key, location):
    root = tmp_path / "sql"
    root.mkdir()
    sentinel = root / "keep.sql"
    sentinel.write_text("CREATE TABLE dbo.keep (id int)", encoding="utf-8")
    config_path = tmp_path / "coop-data-doc.yml"
    config_path.write_text(
        f"repos:\n  sql:\n    path: ./sql\noutput:\n  {output_key}: {location}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="source"):
        Config.load(config_path)
    assert sentinel.read_text() == "CREATE TABLE dbo.keep (id int)"


def test_estate_map_names_are_xml_text():
    title = '"><img src=x onerror=alert(1)> & sales'
    graph = LineageGraph()
    graph.add_node(Node(id="semantic_model:payload", node_type=NodeType.SEMANTIC_MODEL, name=title))
    svg = generate_estate_map_svg(graph)
    tree = ElementTree.fromstring(svg)
    groups = tree.findall("{http://www.w3.org/2000/svg}g")
    assert groups[0].attrib["data-title"] == title
    assert "<img" not in svg


def test_diagnostics_neutralize_raw_html():
    payload = "<img src=x onerror=alert(1)>"
    diagnostics = Diagnostics(warnings=[ParseWarning(file=payload, message=payload, category="bad<script>")])
    page = diagnostics.to_markdown(payload)
    assert "<img" not in page
    assert "<script>" not in page


def test_tooltip_uses_text_nodes():
    script = (
        Path(__file__).parents[1] / "src/coop_data_doc/templates/assets/javascripts/estate-map.js"
    ).read_text()
    assert "innerHTML" not in script


def test_intent_survives_layer_id_change(tmp_path):
    from coop_data_doc.render.markdown import INTENT_BEGIN, render_markdown
    from coop_data_doc.render.paths import slug

    graph = LineageGraph()
    node = Node(
        id="bronze_table:dbo.orders",
        node_type=NodeType.BRONZE_TABLE,
        name="orders",
        schema_name="dbo",
        source_file="tables/orders.sql",
    )
    graph.add_node(node)
    render_markdown(graph, tmp_path, "Test")
    old_path = tmp_path / "bronze_table" / f"{slug(node.id)}.md"
    old_path.write_text(old_path.read_text().replace(INTENT_BEGIN, INTENT_BEGIN + "\nOwned by finance."))
    new_id = graph.retype_node(node.id, NodeType.GOLD_TABLE)
    render_markdown(graph, tmp_path, "Test")
    new_path = tmp_path / "gold_table" / f"{slug(new_id)}.md"
    assert "Owned by finance." in new_path.read_text()


def test_pruning_never_discards_unmatched_authored_intent(tmp_path):
    from coop_data_doc.render.markdown import INTENT_BEGIN, render_markdown
    from coop_data_doc.render.paths import slug

    graph = LineageGraph()
    node = Node(id="view:dbo.orders", node_type=NodeType.VIEW, name="orders", schema_name="dbo")
    graph.add_node(node)
    render_markdown(graph, tmp_path, "Test")
    old_path = tmp_path / "view" / f"{slug(node.id)}.md"
    old_path.write_text(old_path.read_text().replace(INTENT_BEGIN, INTENT_BEGIN + "\nOwned by finance."))
    render_markdown(LineageGraph(), tmp_path, "Test")
    assert old_path.exists()
    assert "Owned by finance." in old_path.read_text()


def test_walk_failure_is_reported(tmp_path, monkeypatch):
    from coop_data_doc.crawler import crawl

    def broken_walk(root, **kwargs):
        kwargs["onerror"](PermissionError(13, "denied", str(root / "private")))
        return iter(())

    monkeypatch.setattr("coop_data_doc.crawler.os.walk", broken_walk)
    root = tmp_path / "sql"
    root.mkdir()
    path = tmp_path / "coop-data-doc.yml"
    path.write_text("repos:\n  sql:\n    path: sql\n")
    _, warnings = crawl(Config.load(path))
    assert any(w.category == "crawl_incomplete" for w in warnings)


@pytest.mark.parametrize("strict", [False, True])
def test_rejected_scan_preserves_previous_artifacts_and_cache(tmp_path, monkeypatch, strict):
    from click.testing import CliRunner

    from coop_data_doc.cli import cli

    source = tmp_path / "sql"
    source.mkdir()
    config = tmp_path / "coop-data-doc.yml"
    config.write_text("repos:\n  sql:\n    path: sql\n")
    output = tmp_path / "data-docs"
    output.mkdir()
    artifacts = [
        output / "graph.json",
        output / "manifest.json",
        output / "diagnostics.json",
        tmp_path / ".lineage-cache.json",
        tmp_path / ".coop-data-doc-parse-cache.json",
    ]
    for artifact in artifacts:
        artifact.write_bytes(b"previous generation")

    def broken_walk(root, **kwargs):
        kwargs["onerror"](PermissionError(13, "denied", str(root)))
        return iter(())

    monkeypatch.setattr("coop_data_doc.crawler.os.walk", broken_walk)
    args = ["build", "--config", str(config), "--non-interactive", "--skip-html"]
    if strict:
        args.append("--strict")
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 2, result.output
    assert "crawl_incomplete" in result.output
    assert all(a.read_bytes() == b"previous generation" for a in artifacts)


def test_strict_scan_does_not_replace_previous_generation(tmp_path):
    from click.testing import CliRunner

    from coop_data_doc.cli import cli

    source = tmp_path / "sql"
    source.mkdir()
    (source / "broken.sql").write_text("CREATE PROCEDURE dbo.broken SELECT 1;")
    config = tmp_path / "coop-data-doc.yml"
    config.write_text("repos:\n  sql:\n    path: sql\n")
    output = tmp_path / "data-docs"
    output.mkdir()
    graph = output / "graph.json"
    graph.write_bytes(b"previous generation")
    result = CliRunner().invoke(
        cli, ["scan", "--config", str(config), "--non-interactive", "--strict", "--jobs", "1"]
    )
    assert result.exit_code == 2, result.output
    assert graph.read_bytes() == b"previous generation"


@pytest.mark.parametrize("output_key", ["dir", "site_dir"])
def test_resolved_output_symlink_cannot_target_source(tmp_path, output_key):
    root = tmp_path / "sql"
    root.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(root, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlink capability is unavailable")
    path = tmp_path / "coop-data-doc.yml"
    path.write_text(f"repos:\n  sql:\n    path: sql\noutput:\n  {output_key}: alias\n")
    with pytest.raises(ConfigError, match="source"):
        Config.load(path)


def test_estate_map_uses_gold_column_for_table_type():
    graph = LineageGraph()
    graph.add_node(
        Node(id="gold_table:dbo.orders", node_type=NodeType.GOLD_TABLE, name="orders", schema_name="dbo")
    )
    tree = ElementTree.fromstring(generate_estate_map_svg(graph))
    rect = tree.find("{http://www.w3.org/2000/svg}g/{http://www.w3.org/2000/svg}rect")
    assert rect.attrib["fill"] == "#ffd700"
