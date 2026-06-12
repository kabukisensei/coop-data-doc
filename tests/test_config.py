from pathlib import Path

import pytest

from coop_data_doc.config import Config, ConfigError

FIXTURES = Path(__file__).parent / "fixtures"


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "coop-data-doc.yml"
    path.write_text(body, encoding="utf-8")
    return path


def minimal_yaml(sql_path: str, pbi_path: str) -> str:
    return f"""\
project_name: Test Estate
repos:
  sql:
    path: {sql_path}
    include: ["**/*.sql"]
    exclude: ["**/archive/**"]
  powerbi:
    path: {pbi_path}
schema_mappings:
  - schema: salespm
    model: "Sales and Project Management"
"""


def test_load_valid_config(tmp_path: Path):
    path = write_config(
        tmp_path, minimal_yaml(str(FIXTURES / "repo_sql"), str(FIXTURES / "repo_pbi"))
    )
    config = Config.load(path)
    assert config.project_name == "Test Estate"
    assert config.repo_root("sql") == (FIXTURES / "repo_sql").resolve()
    assert config.schema_mappings[0].schema_name == "salespm"
    assert config.schema_mappings[0].model == "Sales and Project Management"
    assert config.sql_dialect == "tsql"
    assert config.output_dir() == (tmp_path / "data-docs").resolve()


def test_relative_paths_resolve_against_config_dir(tmp_path: Path):
    (tmp_path / "sql-repo").mkdir()
    (tmp_path / "pbi-repo").mkdir()
    nested = tmp_path / "docs"
    nested.mkdir()
    path = write_config(nested, minimal_yaml("../sql-repo", "../pbi-repo"))
    config = Config.load(path)
    assert config.repo_root("sql") == (tmp_path / "sql-repo").resolve()


def test_missing_file_error_names_path(tmp_path: Path):
    missing = tmp_path / "nope.yml"
    with pytest.raises(ConfigError, match="nope.yml"):
        Config.load(missing)


def test_invalid_yaml_mentions_line(tmp_path: Path):
    path = write_config(tmp_path, "repos:\n  sql:\n   path: [unclosed\n")
    with pytest.raises(ConfigError, match="Invalid YAML"):
        Config.load(path)


def test_unknown_key_named_in_error(tmp_path: Path):
    body = minimal_yaml(str(FIXTURES / "repo_sql"), str(FIXTURES / "repo_pbi"))
    path = write_config(tmp_path, body + "definitely_not_a_key: 1\n")
    with pytest.raises(ConfigError, match="definitely_not_a_key"):
        Config.load(path)


def test_missing_repo_path_names_repo_key(tmp_path: Path):
    path = write_config(tmp_path, minimal_yaml("./does-not-exist", str(FIXTURES / "repo_pbi")))
    with pytest.raises(ConfigError, match=r"Repo 'sql'"):
        Config.load(path)


def test_scaffold_round_trips(tmp_path: Path):
    (tmp_path / "sql-repo").mkdir()
    (tmp_path / "pbi-repo").mkdir()
    cfg_dir = tmp_path / "docs"
    cfg_dir.mkdir()
    target = cfg_dir / "coop-data-doc.yml"
    Config.scaffold(target)
    config = Config.load(target)
    assert config.project_name == "Coop BI Estate"
    assert set(config.repos) == {"sql", "powerbi"}
    assert config.schema_mappings[0].schema_name == "salespm"


def test_scaffold_refuses_overwrite(tmp_path: Path):
    target = tmp_path / "coop-data-doc.yml"
    target.write_text("project_name: keep me\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        Config.scaffold(target)
    assert target.read_text(encoding="utf-8") == "project_name: keep me\n"
