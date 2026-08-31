import json
import pathlib
import subprocess

import pytest
from test_cli import run, setup_workspace

from coop_data_doc.graph.diff import impact_map
from coop_data_doc.graph.model import Edge, EdgeType, LineageGraph, Node, NodeType
from coop_data_doc.graph.serialize import to_json_file


def _node(name: str, source_file: str = "") -> Node:
    return Node(
        id=f"view:dbo.{name}",
        node_type=NodeType.VIEW,
        name=name,
        schema_name="dbo",
        source_file=source_file,
    )


def _graph(nodes: list[Node], edges: list[Edge] | None = None) -> LineageGraph:
    return LineageGraph(nodes={node.id: node for node in nodes}, edges=edges or [])


def _edge(source: str, target: str, edge_type: EdgeType = EdgeType.FEEDS) -> Edge:
    return Edge(source_id=f"view:dbo.{source}", target_id=f"view:dbo.{target}", edge_type=edge_type)


def test_impact_json(tmp_path: pathlib.Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    setup_workspace(ws)

    assert run(["build", "--non-interactive", "--skip-html"], ws).exit_code == 0

    baseline = ws / "data-docs/graph.json"
    baseline_copy = tmp_path / "baseline.json"
    baseline_copy.write_bytes(baseline.read_bytes())

    # Run impact with --files matching the relative path of the node
    res = run(
        [
            "impact",
            "--files",
            "views/sales/v_orders_star.sql",
            "--baseline",
            str(baseline_copy),
            "--format",
            "json",
        ],
        ws,
    )
    assert res.exit_code == 0

    impact_json = json.loads(res.output)
    assert "view:sales.v_orders_star" in impact_json
    # Check that it returns its downstream items
    assert len(impact_json["view:sales.v_orders_star"]) > 0


def test_removed_upstream_node_reports_baseline_consumers():
    a, b = _node("a"), _node("b")
    old = _graph([a, b], [_edge("a", "b")])
    new = _graph([b])

    impacts, _ = impact_map(old, new)

    assert impacts == {a.id: [b.id]}


def test_added_edge_with_unchanged_nodes_reports_current_consumers():
    a, b = _node("a"), _node("b")
    old = _graph([a, b])
    new = _graph([a, b], [_edge("a", "b")])

    impacts, _ = impact_map(old, new)

    assert impacts == {a.id: [b.id]}


def test_impact_cli_reports_added_edge_with_unchanged_nodes(tmp_path: pathlib.Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "data-docs").mkdir()
    (ws / "coop-data-doc.yml").write_text(
        "project_name: test\nrepos: {}\noutput:\n  dir: ./data-docs\n", encoding="utf-8"
    )
    a, b = _node("a"), _node("b")
    to_json_file(_graph([a, b]), ws / "baseline.json")
    to_json_file(_graph([a, b], [_edge("a", "b")]), ws / "data-docs/graph.json")

    result = run(["impact", "--baseline", str(ws / "baseline.json")], ws)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {a.id: [b.id]}


def test_removed_edge_cli_uses_baseline_topology(tmp_path: pathlib.Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "data-docs").mkdir()
    (ws / "coop-data-doc.yml").write_text(
        "project_name: test\nrepos: {}\noutput:\n  dir: ./data-docs\n", encoding="utf-8"
    )
    a, b = _node("a"), _node("b")
    to_json_file(_graph([a, b], [_edge("a", "b")]), ws / "baseline.json")
    to_json_file(_graph([a, b]), ws / "data-docs/graph.json")

    result = run(["impact", "--baseline", str(ws / "baseline.json")], ws)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {a.id: [b.id]}


def test_removed_edge_uses_baseline_topology():
    a, b = _node("a"), _node("b")
    old = _graph([a, b], [_edge("a", "b")])
    new = _graph([a, b])

    impacts, _ = impact_map(old, new)

    assert impacts == {a.id: [b.id]}


def test_added_and_changed_nodes_remain_seeds():
    a, b, c = _node("a"), _node("b"), _node("c")
    changed_b = _node("b")
    changed_b.metadata["trust"] = "verified"
    old = _graph([a, b], [_edge("b", "a")])
    new = _graph([a, changed_b, c], [_edge("b", "a"), _edge("c", "b")])

    impacts, _ = impact_map(old, new)

    assert set(impacts) == {b.id, c.id}
    assert impacts[b.id] == [a.id]
    assert impacts[c.id] == [a.id, b.id]


@pytest.mark.parametrize("edge_type", [EdgeType.READS, EdgeType.REFERENCES, EdgeType.VISUALIZES])
def test_reversed_authored_edge_uses_flow_direction(edge_type: EdgeType):
    upstream, consumer = _node("upstream"), _node("consumer")
    old = _graph([upstream, consumer], [_edge("consumer", "upstream", edge_type)])
    new = _graph([upstream, consumer])

    impacts, _ = impact_map(old, new)

    assert impacts == {upstream.id: [consumer.id]}


def test_multiple_diff_categories_union_impacts_without_duplicates():
    a, b, c = _node("a"), _node("b"), _node("c")
    changed_a = _node("a")
    changed_a.metadata["trust"] = "verified"
    old = _graph([a, b, c], [_edge("a", "b"), _edge("a", "c")])
    new = _graph([changed_a, b, c], [_edge("a", "b")])
    # The removed edge contributes c via baseline; the changed node and the
    # retained edge contribute b via current.

    impacts, _ = impact_map(old, new)

    assert impacts == {a.id: [b.id, c.id]}


def test_impact_results_are_sorted_deterministically():
    z, a, c, b = _node("z"), _node("a"), _node("c"), _node("b")
    old = _graph([z, a, c, b], [_edge("z", "c"), _edge("z", "b"), _edge("a", "c")])
    new = _graph([z, a, c, b])

    impacts, _ = impact_map(old, new)

    assert list(impacts) == [a.id, z.id]
    assert impacts == {a.id: [c.id], z.id: [b.id, c.id]}


def test_impact_markdown_handles_removed_and_baseline_only_nodes(tmp_path: pathlib.Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "data-docs").mkdir()
    (ws / "coop-data-doc.yml").write_text(
        "project_name: test\nrepos: {}\noutput:\n  dir: ./data-docs\n", encoding="utf-8"
    )
    a, b, c = _node("a"), _node("b"), _node("c")
    to_json_file(_graph([a, b, c], [_edge("a", "b"), _edge("b", "c")]), ws / "baseline.json")
    to_json_file(_graph([b]), ws / "data-docs/graph.json")

    result = run(["impact", "--baseline", str(ws / "baseline.json"), "--format", "markdown"], ws)

    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "### dbo.a (view) [removed]",
        "- dbo.b (view)",
        "- dbo.c (view)",
        "",
        "### dbo.b (view)",
        "- dbo.c (view)",
        "",
        "### dbo.c (view) [removed]",
        "",
    ]


def test_files_reports_source_file_deleted_since_baseline(tmp_path: pathlib.Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "data-docs").mkdir()
    (ws / "coop-data-doc.yml").write_text(
        "project_name: test\nrepos: {}\noutput:\n  dir: ./data-docs\n", encoding="utf-8"
    )
    a = _node("a", "views/a.sql")
    b = _node("b", "views/b.sql")
    to_json_file(_graph([a, b], [_edge("a", "b")]), ws / "baseline.json")
    to_json_file(_graph([b]), ws / "data-docs/graph.json")

    result = run(["impact", "--files", "views/a.sql", "--baseline", str(ws / "baseline.json")], ws)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {a.id: [b.id]}


def test_files_reports_object_removed_from_file_that_still_exists(tmp_path: pathlib.Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "data-docs").mkdir()
    (ws / "coop-data-doc.yml").write_text(
        "project_name: test\nrepos: {}\noutput:\n  dir: ./data-docs\n", encoding="utf-8"
    )
    removed = _node("removed", "views/shared.sql")
    retained = _node("retained", "views/shared.sql")
    consumer = _node("consumer", "views/consumer.sql")
    to_json_file(
        _graph([removed, retained, consumer], [_edge("removed", "consumer")]),
        ws / "baseline.json",
    )
    to_json_file(_graph([retained, consumer]), ws / "data-docs/graph.json")

    result = run(["impact", "--files", "views/shared.sql", "--baseline", str(ws / "baseline.json")], ws)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        removed.id: [consumer.id],
        retained.id: [],
    }


def test_impact_git_baseline_path(tmp_path: pathlib.Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "data-docs").mkdir()
    (ws / "coop-data-doc.yml").write_text(
        "project_name: test\nrepos: {}\noutput:\n  dir: ./data-docs\n", encoding="utf-8"
    )
    a, b = _node("a"), _node("b")
    to_json_file(_graph([a, b], [_edge("a", "b")]), ws / "data-docs/graph.json")
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=ws, check=True)
    subprocess.run(["git", "add", "coop-data-doc.yml", "data-docs/graph.json"], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=ws, check=True)
    to_json_file(_graph([b]), ws / "data-docs/graph.json")

    result = run(["impact", "--git", "HEAD"], ws)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {a.id: [b.id]}


def test_impact_markdown(tmp_path: pathlib.Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    setup_workspace(ws)

    assert run(["build", "--non-interactive", "--skip-html"], ws).exit_code == 0

    baseline = ws / "data-docs/graph.json"
    baseline_copy = tmp_path / "baseline.json"
    baseline_copy.write_bytes(baseline.read_bytes())

    res = run(
        [
            "impact",
            "--files",
            "views/sales/v_orders_star.sql",
            "--baseline",
            str(baseline_copy),
            "--format",
            "markdown",
        ],
        ws,
    )
    assert res.exit_code == 0
    assert "### sales.v_orders_star" in res.output
