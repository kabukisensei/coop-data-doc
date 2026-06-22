import json
import os
import shutil
from pathlib import Path

from click.testing import CliRunner

from coop_data_doc import folders as F
from coop_data_doc.cli import cli
from coop_data_doc.config import Config

FIXTURES = Path(__file__).parent / "fixtures"


def _workspace(tmp_path: Path) -> Path:
    """Fixture repos on disk + a config that already skips the sql 'archive' folder."""
    shutil.copytree(FIXTURES / "repo_sql", tmp_path / "sql-repo")
    shutil.copytree(FIXTURES / "repo_pbi", tmp_path / "pbi-repo")
    config = tmp_path / "coop-data-doc.yml"
    config.write_text(
        """\
project_name: Test Estate
repos:
  sql:
    path: ./sql-repo
    include: ["**/*.sql"]
    exclude: ["**/archive/**"]
  powerbi:
    path: ./pbi-repo
    include: ["**/*.tmdl"]
schema_mappings:
  - schema: sales
    model: Sales
output:
  dir: ./data-docs
  site_dir: ./site
""",
        encoding="utf-8",
    )
    return config


def _run(args, cwd: Path):
    runner = CliRunner()
    old = os.getcwd()
    os.chdir(cwd)
    try:
        return runner.invoke(cli, args, obj={}, catch_exceptions=False)
    finally:
        os.chdir(old)


# --- pure helpers -----------------------------------------------------------


def test_top_level_folders_sorted_skips_hidden(tmp_path: Path):
    for name in ("b", "a", ".hidden"):
        (tmp_path / name).mkdir()
    (tmp_path / "f.txt").write_text("x")
    assert F.top_level_folders(tmp_path) == ["a", "b"]
    assert F.top_level_folders(tmp_path / "missing") == []  # not on disk -> empty


def test_glob_roundtrip_preserves_custom_and_order():
    assert F.excludes_for_skips(["a", "b", "c"], {"b"}, ["**/keep*/**"]) == ["**/keep*/**", "**/b/**"]
    skipped, custom = F.split_excludes(["a", "b"], ["**/b/**", "**/data*/**"])
    assert skipped == {"b"}
    assert custom == ["**/data*/**"]  # real wildcard stays a hand-written pattern


def test_folder_states(tmp_path: Path):
    for name in ("archive", "procs", "views"):
        (tmp_path / name).mkdir()
    states, custom = F.folder_states(tmp_path, ["**/archive/**", "**/x*/**"])
    assert states == [
        {"name": "archive", "documented": False},
        {"name": "procs", "documented": True},
        {"name": "views", "documented": True},
    ]
    assert custom == ["**/x*/**"]


# --- commands ---------------------------------------------------------------


def test_folders_command_json(tmp_path: Path):
    _workspace(tmp_path)
    res = _run(["folders"], tmp_path)
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    sql = next(r for r in data["repos"] if r["repo"] == "sql")
    assert sql["exists"] is True
    assert {f["name"]: f["documented"] for f in sql["folders"]} == {
        "archive": False,
        "procs": True,
        "tables": True,
        "views": True,
    }


def test_set_folders_writes_excludes_and_preserves_config(tmp_path: Path):
    config = _workspace(tmp_path)
    res = _run(["set-folders", "--repo", "sql", "--skip", "archive,tables"], tmp_path)
    assert res.exit_code == 0, res.output
    loaded = Config.load(config)
    assert sorted(loaded.repos["sql"].exclude) == ["**/archive/**", "**/tables/**"]
    # everything else survives the round-trip
    assert loaded.project_name == "Test Estate"
    assert [m.schema_name for m in loaded.schema_mappings] == ["sales"]
    assert loaded.repos["powerbi"].path == "./pbi-repo"
    assert loaded.repos["powerbi"].include == ["**/*.tmdl"]


def test_set_folders_empty_skip_documents_everything(tmp_path: Path):
    config = _workspace(tmp_path)  # starts with archive excluded
    res = _run(["set-folders", "--repo", "sql", "--skip", ""], tmp_path)
    assert res.exit_code == 0, res.output
    assert Config.load(config).repos["sql"].exclude == []


def test_set_folders_rejects_unknown_folder(tmp_path: Path):
    _workspace(tmp_path)
    res = _run(["set-folders", "--repo", "sql", "--skip", "nope"], tmp_path)
    assert res.exit_code != 0
    assert "not top-level folders" in res.output
