from pathlib import Path

import pytest

from coop_data_doc import crawler
from coop_data_doc.config import Config, OutputConfig, RepoConfig
from coop_data_doc.crawler import FileKind, crawl

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_config() -> Config:
    return Config(
        repos={
            "sql": RepoConfig(
                path=str(FIXTURES / "repo_sql"),
                include=["**/*.sql"],
                exclude=["**/archive/**"],
            ),
            "powerbi": RepoConfig(
                path=str(FIXTURES / "repo_pbi"),
                include=[
                    "**/*.tmdl",
                    "**/*.bim",
                    "**/report.json",
                    "**/visual.json",
                    "**/page.json",
                    "**/*.pbix",
                ],
            ),
        },
        output=OutputConfig(),
    )


def test_crawl_fixture_repos_classifies_everything():
    inventory, warnings = crawl(fixture_config())
    kinds = {entry.path: entry.kind for entry in inventory.entries}
    assert kinds["procs/usp_load_fact_sales.sql"] == FileKind.SQL_FILE
    assert kinds["tables/dbo.fact_sales.sql"] == FileKind.SQL_FILE
    assert kinds["views/salespm/dim_customer.sql"] == FileKind.SQL_FILE
    assert kinds["SalesPM.SemanticModel/definition/model.tmdl"] == FileKind.TMDL
    assert kinds["SalesPM.SemanticModel/definition/tables/dim_customer.tmdl"] == FileKind.TMDL
    assert kinds["SalesPM.Report/definition/pages/page1/visuals/abc123/visual.json"] == FileKind.PBIR_VISUAL
    assert kinds["SalesPM.Report/definition/pages/page1/page.json"] == FileKind.PBIR_PAGE
    assert kinds["LegacyThing/report.json"] == FileKind.REPORT_JSON_LEGACY
    assert warnings == []


def test_exclude_glob_wins():
    inventory, _ = crawl(fixture_config())
    assert not any("archive" in entry.path for entry in inventory.entries)


def test_inventory_sorted_and_posix():
    inventory, _ = crawl(fixture_config())
    keys = [(entry.repo_key, entry.path) for entry in inventory.entries]
    assert keys == sorted(keys)
    assert not any("\\" in entry.path for entry in inventory.entries)


def test_determinism():
    first, warn1 = crawl(fixture_config())
    second, warn2 = crawl(fixture_config())
    assert first == second
    assert warn1 == warn2


def test_by_kind():
    inventory, _ = crawl(fixture_config())
    sql = inventory.by_kind(FileKind.SQL_FILE)
    assert len(sql) == 7
    assert all(entry.repo_key == "sql" for entry in sql)


def test_unclassified_file_warns(tmp_path: Path):
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "notes.txt").write_text("hi", encoding="utf-8")
    config = Config(repos={"sql": RepoConfig(path=str(tmp_path / "repo"), include=["**/*"])})
    inventory, warnings = crawl(config)
    assert inventory.entries == []
    assert [w.category for w in warnings] == ["unclassified_file"]
    assert warnings[0].file == "notes.txt"


def test_oversize_file_skipped_except_pbix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "big.sql").write_text("SELECT 1;" * 10, encoding="utf-8")
    (repo / "big.pbix").write_bytes(b"PK\x03\x04" + b"\x00" * 100)
    monkeypatch.setattr(crawler, "MAX_FILE_BYTES", 10)
    config = Config(repos={"sql": RepoConfig(path=str(repo), include=["**/*.sql", "**/*.pbix"])})
    inventory, warnings = crawl(config)
    assert [entry.kind for entry in inventory.entries] == [FileKind.PBIX]
    assert [w.category for w in warnings] == ["file_too_large"]
    assert warnings[0].file == "big.sql"


def test_hidden_dirs_skipped(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "junk.sql").write_text("SELECT 1;", encoding="utf-8")
    (repo / "real.sql").write_text("SELECT 1;", encoding="utf-8")
    config = Config(repos={"sql": RepoConfig(path=str(repo), include=["**/*.sql"])})
    inventory, warnings = crawl(config)
    assert [entry.path for entry in inventory.entries] == ["real.sql"]
    assert warnings == []
