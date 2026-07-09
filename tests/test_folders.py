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
    include: ["**/*.tmdl", "**/*.bim", "**/report.json", "**/visual.json", "**/page.json", "**/*.pbix"]
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


def _run(args, cwd: Path, stdin: str | None = None):
    runner = CliRunner()
    old = os.getcwd()
    os.chdir(cwd)
    try:
        return runner.invoke(cli, args, input=stdin, obj={}, catch_exceptions=False)
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
        "functions": True,
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
    assert loaded.repos["powerbi"].include[0] == "**/*.tmdl"  # untouched repo preserved


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


# --- lineage query ----------------------------------------------------------


def _scan(tmp_path: Path):
    res = _run(["scan", "--non-interactive"], tmp_path)
    assert res.exit_code == 0, res.output


def test_lineage_exact_object(tmp_path: Path):
    _workspace(tmp_path)
    _scan(tmp_path)
    res = _run(["lineage", "dbo.fact_sales"], tmp_path)
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["object"]["name"] == "dbo.fact_sales"
    assert data["object"]["doc"].endswith(".md")
    assert isinstance(data["upstream"], list)
    assert isinstance(data["downstream"], list)


def test_lineage_ambiguous_lists_candidates(tmp_path: Path):
    _workspace(tmp_path)
    _scan(tmp_path)
    res = _run(["lineage", "fact_sales"], tmp_path)  # matches the gold table AND the pbi table
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["ambiguous"] is True
    assert len(data["matches"]) >= 2


def test_lineage_not_found_errors(tmp_path: Path):
    _workspace(tmp_path)
    _scan(tmp_path)
    res = _run(["lineage", "no_such_object_xyz"], tmp_path)
    assert res.exit_code != 0
    assert "no object matching" in res.output


def test_lineage_requires_built_graph(tmp_path: Path):
    _workspace(tmp_path)  # no scan/build → no graph.json yet
    res = _run(["lineage", "anything"], tmp_path)
    assert res.exit_code != 0
    assert "no built graph" in res.output


# --- show-config / config-set (the rest of the setup surface) ---------------


def test_show_config_json(tmp_path: Path):
    _workspace(tmp_path)
    res = _run(["show-config"], tmp_path)
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["exists"] is True
    assert data["project_name"] == "Test Estate"
    assert data["repos"]["sql"]["exclude"] == ["**/archive/**"]
    assert data["schema_mappings"] == [{"schema": "sales", "model": "Sales"}]


def test_show_config_defaults_when_none(tmp_path: Path):
    res = _run(["show-config"], tmp_path)  # empty dir, no config
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["exists"] is False
    assert "sql" in data["repos"] and "powerbi" in data["repos"]


def test_config_set_patch_roundtrip_preserves_rest(tmp_path: Path):
    config = _workspace(tmp_path)
    patch = json.dumps(
        {
            "project_name": "Renamed",
            "layers": {"gold": {"schemas": ["mart"], "paths": []}},
            "schema_mappings": [{"schema": "sales", "model": "Sales"}, {"schema": "ops", "model": "Ops"}],
        }
    )
    res = _run(["config-set"], tmp_path, stdin=patch)
    assert res.exit_code == 0, res.output
    loaded = Config.load(config)
    assert loaded.project_name == "Renamed"
    assert loaded.layers["gold"].schemas == ["mart"]
    assert {m.schema_name for m in loaded.schema_mappings} == {"sales", "ops"}
    # untouched fields survive
    assert loaded.repos["sql"].exclude == ["**/archive/**"]
    assert loaded.repos["sql"].include == ["**/*.sql"]


def test_config_set_partial_repo_preserves_other_fields(tmp_path: Path):
    config = _workspace(tmp_path)
    res = _run(["config-set"], tmp_path, stdin=json.dumps({"repos": {"sql": {"path": "./sql-repo"}}}))
    assert res.exit_code == 0, res.output
    loaded = Config.load(config)
    assert loaded.repos["sql"].include == ["**/*.sql"]  # only path was patched
    assert loaded.repos["sql"].exclude == ["**/archive/**"]


def test_config_set_rejects_non_object(tmp_path: Path):
    _workspace(tmp_path)
    res = _run(["config-set"], tmp_path, stdin="[]")
    assert res.exit_code != 0
    assert "JSON object" in res.output


# --- resolve / resolve-apply (agent link resolution) ------------------------


def test_resolve_lists_ambiguous_then_apply_resolves(tmp_path: Path):
    _workspace(tmp_path)
    # 1. resolve surfaces the ambiguous fact_sales link with scored candidates
    res = _run(["resolve"], tmp_path)
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    item = next(u for u in data["unresolved"] if u["cache_key"] == "pbi_table:sales.fact_sales")
    assert any(c["target"] == "gold_table:dbo.fact_sales" for c in item["candidates"])
    assert all("score" in c and "target" in c for c in item["candidates"])

    # 2. apply a decision mapping it to the gold table
    decision = json.dumps(
        {"decisions": [{"cache_key": "pbi_table:sales.fact_sales", "target": "gold_table:dbo.fact_sales"}]}
    )
    res2 = _run(["resolve-apply"], tmp_path, stdin=decision)
    assert res2.exit_code == 0, res2.output
    assert "Applied 1" in res2.output

    # 3. resolve again: the decision is cached, so it's no longer pending
    again = json.loads(_run(["resolve"], tmp_path).output)
    assert "pbi_table:sales.fact_sales" not in {u["cache_key"] for u in again["unresolved"]}


def test_resolve_apply_external_and_skip(tmp_path: Path):
    _workspace(tmp_path)
    payload = json.dumps(
        {
            "decisions": [
                {"cache_key": "pbi_table:sales.ext_unresolved", "external": True},
                {"cache_key": "pbi_table:sales.orders_native", "skip": True},
            ]
        }
    )
    res = _run(["resolve-apply"], tmp_path, stdin=payload)
    assert res.exit_code == 0, res.output
    assert "Applied 2" in res.output


def test_resolve_apply_rejects_bad_payload(tmp_path: Path):
    _workspace(tmp_path)
    res = _run(["resolve-apply"], tmp_path, stdin='{"nope": 1}')
    assert res.exit_code != 0
    assert "decisions" in res.output
