import json
import pathlib
from test_cli import run, setup_workspace

def test_impact_json(tmp_path: pathlib.Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    setup_workspace(ws)
    
    assert run(["build", "--non-interactive", "--skip-html"], ws).exit_code == 0
    
    baseline = ws / "data-docs/graph.json"
    baseline_copy = tmp_path / "baseline.json"
    baseline_copy.write_bytes(baseline.read_bytes())
    
    # Run impact with --files matching the relative path of the node
    res = run(["impact", "--files", "views/sales/v_orders_star.sql", "--baseline", str(baseline_copy), "--format", "json"], ws)
    assert res.exit_code == 0
    
    impact_json = json.loads(res.output)
    assert "view:sales.v_orders_star" in impact_json
    # Check that it returns its downstream items
    assert len(impact_json["view:sales.v_orders_star"]) > 0

def test_impact_markdown(tmp_path: pathlib.Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    setup_workspace(ws)
    
    assert run(["build", "--non-interactive", "--skip-html"], ws).exit_code == 0
    
    baseline = ws / "data-docs/graph.json"
    baseline_copy = tmp_path / "baseline.json"
    baseline_copy.write_bytes(baseline.read_bytes())
    
    res = run(["impact", "--files", "views/sales/v_orders_star.sql", "--baseline", str(baseline_copy), "--format", "markdown"], ws)
    assert res.exit_code == 0
    assert "### sales.v_orders_star" in res.output
