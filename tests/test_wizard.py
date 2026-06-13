from pathlib import Path

import pytest

from coop_data_doc import wizard
from coop_data_doc.config import Config


class FakeQuestionary:
    """Queue-driven questionary stand-in; records every prompt's kwargs."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls: list[tuple[str, str, dict]] = []

    def _prompt(self, kind, message, **kwargs):
        self.calls.append((kind, message, kwargs))
        if not self.answers:
            raise AssertionError(f"unexpected extra prompt: {kind} {message!r}")
        value = self.answers.pop(0)

        class _Result:
            @staticmethod
            def ask():
                return value

        return _Result()

    def text(self, message, **kwargs):
        return self._prompt("text", message, **kwargs)

    def path(self, message, **kwargs):
        return self._prompt("path", message, **kwargs)

    def confirm(self, message, **kwargs):
        return self._prompt("confirm", message, **kwargs)


def make_repos(tmp_path: Path) -> tuple[Path, Path]:
    sql = tmp_path / "sql-repo"
    pbi = tmp_path / "pbi-repo"
    sql.mkdir()
    pbi.mkdir()
    return sql, pbi


def test_fresh_setup_writes_loadable_config(tmp_path: Path, monkeypatch):
    make_repos(tmp_path)
    fake = FakeQuestionary(
        [
            "My Estate",  # project name
            "./sql-repo",  # sql path (exists)
            "./pbi-repo",  # pbi path (exists)
            "./docs",  # markdown dir
            "./site",  # site dir
            True,  # add a mapping?
            "salespm",  # schema
            "Sales and PM",  # model
            False,  # add another?
        ]
    )
    monkeypatch.setattr(wizard, "questionary", fake)
    config_path = tmp_path / "coop-data-doc.yml"

    config = wizard.run_setup(config_path)

    assert config is not None
    assert config.project_name == "My Estate"
    assert config.repos["sql"].path == "./sql-repo"
    assert config.schema_mappings[0].schema_name == "salespm"
    assert config.schema_mappings[0].model == "Sales and PM"
    assert config.output.dir == "./docs"
    assert fake.answers == []  # every queued answer consumed
    # file round-trips through the normal loader
    assert Config.load(config_path).project_name == "My Estate"


def test_rerun_prefills_existing_values(tmp_path: Path, monkeypatch):
    make_repos(tmp_path)
    config_path = tmp_path / "coop-data-doc.yml"
    first = FakeQuestionary(
        ["My Estate", "./sql-repo", "./pbi-repo", "./docs", "./site", True, "salespm", "Sales and PM", False]
    )
    monkeypatch.setattr(wizard, "questionary", first)
    assert wizard.run_setup(config_path) is not None

    second = FakeQuestionary(
        [
            "Renamed Estate",  # change the name
            "./sql-repo",
            "./pbi-repo",
            "./docs",
            "./site",
            True,  # keep existing mappings?
            False,  # add another?
        ]
    )
    monkeypatch.setattr(wizard, "questionary", second)
    config = wizard.run_setup(config_path)

    assert config is not None
    assert config.project_name == "Renamed Estate"
    assert config.schema_mappings[0].schema_name == "salespm"  # kept
    # defaults shown to the user were the previous values
    defaults = {message: kwargs.get("default") for _, message, kwargs in second.calls}
    assert defaults["Project name (shown as the docs site title):"] == "My Estate"
    assert defaults["SQL repo path (procs, tables, views):"] == "./sql-repo"
    assert defaults["Markdown output folder:"] == "./docs"


def test_nonexistent_repo_use_anyway(tmp_path: Path, monkeypatch):
    (tmp_path / "pbi-repo").mkdir()
    fake = FakeQuestionary(
        [
            "Estate",
            "./not-cloned-yet",  # sql path: missing
            True,  # use it anyway?
            "./pbi-repo",
            "./docs",
            "./site",
            False,  # add mapping?
        ]
    )
    monkeypatch.setattr(wizard, "questionary", fake)
    config_path = tmp_path / "coop-data-doc.yml"

    config = wizard.run_setup(config_path)

    assert config is None  # saved, but doesn't validate yet
    assert config_path.is_file()
    text = config_path.read_text(encoding="utf-8")
    assert '"./not-cloned-yet"' in text


def test_ctrl_c_writes_nothing(tmp_path: Path, monkeypatch):
    make_repos(tmp_path)
    fake = FakeQuestionary(["Estate", None])  # None = user hit Ctrl-C
    monkeypatch.setattr(wizard, "questionary", fake)
    config_path = tmp_path / "coop-data-doc.yml"

    with pytest.raises(KeyboardInterrupt):
        wizard.run_setup(config_path)
    assert not config_path.exists()


def test_rerun_prefills_even_when_saved_config_not_runnable(tmp_path: Path, monkeypatch):
    (tmp_path / "pbi-repo").mkdir()
    config_path = tmp_path / "coop-data-doc.yml"
    first = FakeQuestionary(
        [
            "Custom Name",
            "./not-cloned-yet",
            True,  # missing repo, use anyway
            "./pbi-repo",
            "./docs",
            "./site",
            True,
            "salespm",
            "Sales and PM",
            False,
        ]
    )
    monkeypatch.setattr(wizard, "questionary", first)
    assert wizard.run_setup(config_path) is None  # saved but not runnable

    # re-running must prefill the saved answers, not start fresh
    second = FakeQuestionary(
        ["Custom Name", "./not-cloned-yet", True, "./pbi-repo", "./docs", "./site", True, False]
    )
    monkeypatch.setattr(wizard, "questionary", second)
    wizard.run_setup(config_path)
    defaults = {message: kwargs.get("default") for _, message, kwargs in second.calls}
    assert defaults["Project name (shown as the docs site title):"] == "Custom Name"
    assert defaults["SQL repo path (procs, tables, views):"] == "./not-cloned-yet"
    # the keep-mappings confirm only appears when mappings were preserved
    assert any("Keep existing schema mappings" in message for _, message, _k in second.calls)
