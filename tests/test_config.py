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
  - schema: sales
    model: "Sales Analytics"
"""


def test_load_valid_config(tmp_path: Path):
    path = write_config(tmp_path, minimal_yaml(str(FIXTURES / "repo_sql"), str(FIXTURES / "repo_pbi")))
    config = Config.load(path)
    assert config.project_name == "Test Estate"
    assert config.repo_root("sql") == (FIXTURES / "repo_sql").resolve()
    assert config.schema_mappings[0].schema_name == "sales"
    assert config.schema_mappings[0].model == "Sales Analytics"
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


def test_non_utf8_config_raises_friendly_config_error(tmp_path: Path):
    # PowerShell 5.1's `>` redirect writes UTF-16LE by default — a plausible
    # first-run experience on Windows. That must surface as a one-line
    # ConfigError naming the file and the likely cause, not a raw
    # UnicodeDecodeError traceback (cli.main only pretty-prints ConfigError).
    path = tmp_path / "coop-data-doc.yml"
    path.write_bytes(minimal_yaml(str(FIXTURES / "repo_sql"), str(FIXTURES / "repo_pbi")).encode("utf-16"))
    with pytest.raises(ConfigError, match=r"coop-data-doc\.yml.*UTF-8"):
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


def test_site_dir_inside_output_dir_rejected(tmp_path: Path):
    # the real crash from the field: site_dir nested in the markdown dir
    body = minimal_yaml(str(FIXTURES / "repo_sql"), str(FIXTURES / "repo_pbi"))
    body += "output:\n  dir: ./docs\n  site_dir: ./docs/site\n"
    path = write_config(tmp_path, body)
    with pytest.raises(ConfigError, match="separate folders"):
        Config.load(path)


def test_output_dir_equal_site_dir_rejected(tmp_path: Path):
    body = minimal_yaml(str(FIXTURES / "repo_sql"), str(FIXTURES / "repo_pbi"))
    body += "output:\n  dir: ./docs\n  site_dir: ./docs\n"
    path = write_config(tmp_path, body)
    with pytest.raises(ConfigError, match="separate folders"):
        Config.load(path)


def test_sibling_output_dirs_accepted(tmp_path: Path):
    body = minimal_yaml(str(FIXTURES / "repo_sql"), str(FIXTURES / "repo_pbi"))
    body += "output:\n  dir: ./docs\n  site_dir: ./docs-site\n"
    path = write_config(tmp_path, body)
    config = Config.load(path)
    assert config.output_dir() == (tmp_path / "docs").resolve()
    assert config.site_dir() == (tmp_path / "docs-site").resolve()


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
    assert config.schema_mappings[0].schema_name == "sales"


def test_scaffold_refuses_overwrite(tmp_path: Path):
    target = tmp_path / "coop-data-doc.yml"
    target.write_text("project_name: keep me\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        Config.scaffold(target)
    assert target.read_text(encoding="utf-8") == "project_name: keep me\n"


def test_include_schemas_render_round_trip(tmp_path: Path):
    # render_config_yaml emits include_schemas when non-empty (and omits it when
    # empty); what it writes parses back to the same value
    from coop_data_doc.config import render_config_yaml

    (tmp_path / "sql-repo").mkdir()
    (tmp_path / "pbi-repo").mkdir()

    def render(include_schemas):
        return render_config_yaml(
            project_name="Estate",
            sql_path="./sql-repo",
            pbi_path="./pbi-repo",
            mappings=[],
            include_schemas=include_schemas,
            output_dir="./docs",
            site_dir="./site",
        )

    path = write_config(tmp_path, render(["bronze", "silver", "erp_fo"]))
    config = Config.load(path)
    assert config.include_schemas == ["bronze", "silver", "erp_fo"]
    assert 'include_schemas: ["bronze", "silver", "erp_fo"]' in path.read_text(encoding="utf-8")

    path = write_config(tmp_path, render([]))
    config = Config.load(path)
    assert config.include_schemas == []  # empty = no restriction
    assert "include_schemas" not in path.read_text(encoding="utf-8")  # key omitted entirely
