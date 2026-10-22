"""sqlsc's repo layout: a file whose CREATE declares another object than its file name
(a module renamed after it was scripted) is reported, documented as declared, and never
redirected. Outside an sqlsc repo file names are not read."""

import json
import shutil
from pathlib import Path

from test_cli import run

from coop_data_doc.cli import run_pipeline
from coop_data_doc.config import Config

FIXTURE = Path(__file__).parent / "fixtures" / "repo_sqlsc"
STALE_FILE = "Views/dbo.OrdersSummary_deprecated.sql"


def _workspace(tmp_path: Path, *, sqlsc: bool = True) -> Path:
    shutil.copytree(FIXTURE, tmp_path / "warehouse")
    if not sqlsc:
        (tmp_path / "warehouse" / "sqlsc.yml").unlink()
    config = tmp_path / "coop-data-doc.yml"
    config.write_text(
        "project_name: Fixture\nrepos:\n  sql:\n    path: ./warehouse/database\n"
        '    include: ["**/*.sql"]\noutput:\n  dir: ./data-docs\n  site_dir: ./site\n',
        encoding="utf-8",
    )
    return config


def _mismatches(config: Path):
    graph, _, warnings = run_pipeline(Config.load(config), interactive=False)
    return graph, [(w.file, w.message) for w in warnings if w.category == "sqlsc_declaration_mismatch"]


def test_a_stale_declaration_is_reported_and_not_redirected(tmp_path: Path):
    graph, found = _mismatches(_workspace(tmp_path))
    assert found == [
        (
            STALE_FILE,
            (
                "sqlsc's file for dbo.orderssummary_deprecated declares dbo.orderssummary "
                "(renamed after it was scripted?); documented under the declared name. "
                "Script it again with sqlsc 1.0.4 or later to correct the file"
            ),
        )
    ]
    # documented as declared; the current name is not invented from the file name
    assert "view:dbo.orderssummary" in graph.nodes
    assert "view:dbo.orderssummary_deprecated" not in graph.nodes
    # matching files (bracketed, %2E-escaped dots) raise nothing
    assert "view:dbo.orderscurrent" in graph.nodes
    assert "stored_proc:etl.load.v2" in graph.nodes


def test_a_corrected_capture_documents_the_current_name(tmp_path: Path):
    config = _workspace(tmp_path)
    stale = tmp_path / "warehouse" / "database" / STALE_FILE
    stale.write_text(
        "CREATE VIEW dbo.[OrdersSummary_deprecated] AS SELECT OrderId, Amount FROM dbo.Orders;\nGO\n",
        encoding="utf-8",
    )
    graph, found = _mismatches(config)
    assert found == []
    assert "view:dbo.orderssummary_deprecated" in graph.nodes
    assert "view:dbo.orderssummary" not in graph.nodes
    assert run(["scan", "--non-interactive"], tmp_path).exit_code == 0
    lineage = json.loads(run(["lineage", "dbo.OrdersSummary_deprecated"], tmp_path).output)
    assert lineage["object"]["id"] == "view:dbo.orderssummary_deprecated"


def test_outside_an_sqlsc_repo_file_names_mean_nothing(tmp_path: Path):
    _, found = _mismatches(_workspace(tmp_path, sqlsc=False))
    assert found == []


def test_the_warning_never_gates(tmp_path: Path):
    _workspace(tmp_path)
    assert run(["build", "--non-interactive", "--strict"], tmp_path).exit_code == 0
    assert run(["check"], tmp_path).exit_code == 0
