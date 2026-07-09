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
                    "**/definition.pbir",
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
    assert kinds["views/sales/dim_customer.sql"] == FileKind.SQL_FILE
    assert kinds["Sales.SemanticModel/definition/model.tmdl"] == FileKind.TMDL
    assert kinds["Sales.SemanticModel/definition/tables/dim_customer.tmdl"] == FileKind.TMDL
    assert kinds["Sales.Report/definition/pages/page1/visuals/abc123/visual.json"] == FileKind.PBIR_VISUAL
    assert kinds["Sales.Report/definition/pages/page1/page.json"] == FileKind.PBIR_PAGE
    assert kinds["Sales.Report/definition.pbir"] == FileKind.PBIR_DEFINITION
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


def test_unreadable_file_degrades_to_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A locked / permission-denied / mid-walk-deleted file (e.g. a .pbix held by
    Power BI Desktop on Windows) raises OSError from stat(); the crawler must
    degrade it to a warning and keep going, never abort the whole build."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "locked.sql").write_text("SELECT 1;", encoding="utf-8")
    (repo / "ok.sql").write_text("SELECT 2;", encoding="utf-8")

    real_stat = Path.stat

    def fake_stat(self, *args, **kwargs):
        if self.name == "locked.sql":
            raise PermissionError("[WinError 32] file in use")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fake_stat)
    config = Config(repos={"sql": RepoConfig(path=str(repo), include=["**/*.sql"])})
    inventory, warnings = crawl(config)
    assert [entry.path for entry in inventory.entries] == ["ok.sql"]  # build did not abort
    assert any(w.category == "file_unreadable" and w.file == "locked.sql" for w in warnings)


def test_hidden_dirs_skipped(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "junk.sql").write_text("SELECT 1;", encoding="utf-8")
    (repo / "real.sql").write_text("SELECT 1;", encoding="utf-8")
    config = Config(repos={"sql": RepoConfig(path=str(repo), include=["**/*.sql"])})
    inventory, warnings = crawl(config)
    assert [entry.path for entry in inventory.entries] == ["real.sql"]
    assert warnings == []


def test_hidden_and_excluded_dirs_are_not_descended(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Pruning happens at the directory level: files under a hidden or a
    subtree-excluded directory must never be stat'd/opened (the whole point of
    the walk-pruning is to avoid touching .git / node_modules on Windows). We
    prove non-descent by making any stat() under a pruned dir blow up — the
    crawl must still succeed and only see the real, top-level file."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "objects.sql").write_text("SELECT 1;", encoding="utf-8")
    (repo / "node_modules" / "pkg").mkdir(parents=True)
    (repo / "node_modules" / "pkg" / "bundled.sql").write_text("SELECT 1;", encoding="utf-8")
    (repo / "real.sql").write_text("SELECT 1;", encoding="utf-8")

    real_stat = Path.stat

    def boom_under_pruned(self, *args, **kwargs):
        if ".git" in self.parts or "node_modules" in self.parts:
            raise AssertionError(f"descended into a pruned dir: {self}")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", boom_under_pruned)
    config = Config(
        repos={"sql": RepoConfig(path=str(repo), include=["**/*.sql"], exclude=["**/node_modules/**"])}
    )
    inventory, warnings = crawl(config)
    assert [entry.path for entry in inventory.entries] == ["real.sql"]
    assert warnings == []


def test_glob_matching_is_case_insensitive_on_every_platform():
    """fnmatch.fnmatch is case-insensitive on Windows (normcase folds) but
    case-sensitive on POSIX, so the crawled file set — and therefore the docs —
    differed per OS. The policy is uniform case-insensitivity everywhere:
    _matches casefolds both sides itself, so the result below holds regardless
    of the platform the suite runs on."""
    assert crawler._matches("REPORT.SQL", ["**/*.sql"])  # uppercase file, lowercase glob
    assert crawler._matches("report.sql", ["**/*.SQL"])  # lowercase file, uppercase glob
    assert crawler._matches("Archive/old.sql", ["**/archive/**"])  # TitleCase folder
    assert not crawler._matches("report.txt", ["**/*.sql"])  # still a real filter


def test_crawl_uppercase_extension_and_titlecase_exclude_dir(tmp_path: Path):
    # Windows-authored repos routinely carry REPORT.SQL and Archive/: the
    # include must catch the former and the exclude must prune the latter,
    # identically on every OS (regression: both flipped per-platform).
    repo = tmp_path / "repo"
    (repo / "Archive").mkdir(parents=True)
    (repo / "Archive" / "old.sql").write_text("SELECT 1;", encoding="utf-8")
    (repo / "REPORT.SQL").write_text("SELECT 1;", encoding="utf-8")
    config = Config(
        repos={"sql": RepoConfig(path=str(repo), include=["**/*.sql"], exclude=["**/archive/**"])}
    )
    inventory, warnings = crawl(config)
    assert [entry.path for entry in inventory.entries] == ["REPORT.SQL"]
    assert [entry.kind for entry in inventory.entries] == [FileKind.SQL_FILE]
    assert warnings == []


def test_narrow_exclude_still_crawls_dir_and_keeps_other_files(tmp_path: Path):
    """A narrow exclude (not a whole-subtree pattern) must NOT prune the dir:
    the excluded file is dropped per-file, siblings are still crawled."""
    repo = tmp_path / "repo"
    (repo / "logs").mkdir(parents=True)
    (repo / "logs" / "debug.sql").write_text("SELECT 1;", encoding="utf-8")
    (repo / "logs" / "keep.sql").write_text("SELECT 1;", encoding="utf-8")
    config = Config(repos={"sql": RepoConfig(path=str(repo), include=["**/*.sql"], exclude=["**/debug.sql"])})
    inventory, _ = crawl(config)
    assert [entry.path for entry in inventory.entries] == ["logs/keep.sql"]
