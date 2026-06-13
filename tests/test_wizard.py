from pathlib import Path

import pytest

from coop_data_doc import wizard
from coop_data_doc.config import Config


class RoutedQuestionary:
    """questionary stand-in that answers based on the prompt message.

    Robust to prompt order/count changes: `router(kind, message, kwargs)`
    returns the answer for each prompt. Records every call for assertions.
    """

    def __init__(self, router):
        self.router = router
        self.calls: list[tuple[str, str, dict]] = []

    def _q(self, kind, message, **kwargs):
        self.calls.append((kind, message, kwargs))
        answer = self.router(kind, message, kwargs)

        class _Result:
            @staticmethod
            def ask():
                return answer

            @staticmethod
            def unsafe_ask():
                return answer

        return _Result()

    def text(self, message, **kwargs):
        return self._q("text", message, **kwargs)

    def path(self, message, **kwargs):
        return self._q("path", message, **kwargs)

    def confirm(self, message, **kwargs):
        return self._q("confirm", message, **kwargs)


def make_repos(tmp_path: Path):
    (tmp_path / "sql-repo").mkdir()
    (tmp_path / "pbi-repo").mkdir()


def default_router(answers: dict):
    """Build a router from {substring: answer}; unmatched text->'', confirm->False."""

    def router(kind, message, kwargs):
        for key, value in answers.items():
            if key.lower() in message.lower():
                return value
        return False if kind == "confirm" else ""

    return router


def test_fresh_setup_with_layers(tmp_path: Path, monkeypatch):
    make_repos(tmp_path)
    answers = {
        "Project name": "My Estate",
        "SQL repo path": "./sql-repo",
        "Power BI repo path": "./pbi-repo",
        "Markdown output": "./docs",
        "HTML site": "./site",
        "EXCLUDE": "**/logging/**, **/Deployment/**",
        "Bronze layer — schemas": "d365po, d365fo",
        "Silver layer — schemas": "stg",
        "Gold layer — schemas": "dwm, common",
        "Gold layer — folder": "**/dim/**, **/fact/**",
    }
    fake = RoutedQuestionary(default_router(answers))
    monkeypatch.setattr(wizard, "questionary", fake)
    config_path = tmp_path / "coop-data-doc.yml"

    config = wizard.run_setup(config_path)

    assert config is not None
    assert config.project_name == "My Estate"
    assert config.repos["sql"].exclude == ["**/logging/**", "**/Deployment/**"]
    assert config.layers["bronze"].schemas == ["d365po", "d365fo"]
    assert config.layers["silver"].schemas == ["stg"]
    assert config.layers["gold"].schemas == ["dwm", "common"]
    assert config.layers["gold"].paths == ["**/dim/**", "**/fact/**"]
    assert Config.load(config_path).layers["gold"].paths == ["**/dim/**", "**/fact/**"]


def test_skip_bronze_and_silver(tmp_path: Path, monkeypatch):
    make_repos(tmp_path)
    answers = {
        "Project name": "Gold Only",
        "SQL repo path": "./sql-repo",
        "Power BI repo path": "./pbi-repo",
        "Markdown output": "./docs",
        "HTML site": "./site",
        "Gold layer — schemas": "dwm",
    }
    fake = RoutedQuestionary(default_router(answers))
    monkeypatch.setattr(wizard, "questionary", fake)
    config_path = tmp_path / "coop-data-doc.yml"

    config = wizard.run_setup(config_path)
    assert config is not None
    assert set(config.layers) == {"gold"}  # bronze + silver skipped
    assert config.layers["gold"].schemas == ["dwm"]


def test_rerun_prefills_layers(tmp_path: Path, monkeypatch):
    make_repos(tmp_path)
    config_path = tmp_path / "coop-data-doc.yml"
    answers = {
        "Project name": "Estate",
        "SQL repo path": "./sql-repo",
        "Power BI repo path": "./pbi-repo",
        "Markdown output": "./docs",
        "HTML site": "./site",
        "Bronze layer — schemas": "d365po",
        "Gold layer — schemas": "dwm",
    }
    monkeypatch.setattr(wizard, "questionary", RoutedQuestionary(default_router(answers)))
    assert wizard.run_setup(config_path) is not None

    second = RoutedQuestionary(default_router(answers))
    monkeypatch.setattr(wizard, "questionary", second)
    wizard.run_setup(config_path)
    defaults = {msg: kw.get("default") for _, msg, kw in second.calls}
    bronze_default = next(v for m, v in defaults.items() if "Bronze layer — schemas" in m)
    assert bronze_default == "d365po"


def test_ctrl_c_writes_nothing(tmp_path: Path, monkeypatch):
    make_repos(tmp_path)

    def router(kind, message, kwargs):
        if "Project name" in message:
            return None  # user hits Ctrl-C at the first prompt
        return ""

    monkeypatch.setattr(wizard, "questionary", RoutedQuestionary(router))
    config_path = tmp_path / "coop-data-doc.yml"
    with pytest.raises(KeyboardInterrupt):
        wizard.run_setup(config_path)
    assert not config_path.exists()
