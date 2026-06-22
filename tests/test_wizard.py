from pathlib import Path

import pytest
import questionary as _questionary

from coop_data_doc import wizard
from coop_data_doc.config import Config, render_config_yaml


class RoutedQuestionary:
    """questionary stand-in that answers based on the prompt message.

    Robust to prompt order/count changes: `router(kind, message, kwargs)`
    returns the answer for each prompt. Records every call for assertions.
    """

    # the wizard builds checkbox options with questionary.Choice — pass the
    # real classes through so `choices` carry value/checked the router reads.
    Choice = _questionary.Choice
    Separator = _questionary.Separator

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

    def checkbox(self, message, **kwargs):
        return self._q("checkbox", message, **kwargs)


def make_repos(tmp_path: Path):
    (tmp_path / "sql-repo").mkdir()
    (tmp_path / "pbi-repo").mkdir()


def default_router(answers: dict):
    """Build a router from {substring: answer}; unmatched text keeps the
    prompt's prefilled default (like pressing Enter), unmatched confirm->False."""

    def router(kind, message, kwargs):
        for key, value in answers.items():
            if key.lower() in message.lower():
                return value
        if kind == "confirm":
            return False
        if kind == "checkbox":
            # unmatched checkbox = "press Enter", i.e. keep everything checked
            return [choice.value for choice in kwargs.get("choices", [])]
        return kwargs.get("default", "")

    return router


def test_fresh_setup_with_layers(tmp_path: Path, monkeypatch):
    make_repos(tmp_path)
    answers = {
        "Project name": "My Estate",
        "SQL repo path": "./sql-repo",
        "Power BI repo path": "./pbi-repo",
        "Markdown output": "./docs",
        "HTML site": "./site",
        "SQL — files/patterns to INCLUDE": "**/*.sql",
        "SQL — folders to SKIP": "**/logging/**, **/Deployment/**",
        "Power BI — folders to SKIP": "**/BACKUP/**",
        "Bronze layer — schemas": "erp_orders, erp_finance",
        "Silver layer — schemas": "stg",
        "Gold layer — schemas": "mart, common, silver",
        "FOLDER instead of a schema": True,  # opt into the advanced folder step
        "Gold layer — folder": "**/dim/**, **/fact/**",
    }
    fake = RoutedQuestionary(default_router(answers))
    monkeypatch.setattr(wizard, "questionary", fake)
    config_path = tmp_path / "coop-data-doc.yml"

    config = wizard.run_setup(config_path)

    assert config is not None
    assert config.project_name == "My Estate"
    assert config.repos["sql"].include == ["**/*.sql"]
    assert config.repos["sql"].exclude == ["**/logging/**", "**/Deployment/**"]
    assert config.repos["powerbi"].exclude == ["**/BACKUP/**"]
    assert config.layers["bronze"].schemas == ["erp_orders", "erp_finance"]
    assert config.layers["silver"].schemas == ["stg"]
    assert config.layers["gold"].schemas == ["mart", "common", "silver"]
    assert config.layers["gold"].paths == ["**/dim/**", "**/fact/**"]
    assert Config.load(config_path).layers["gold"].paths == ["**/dim/**", "**/fact/**"]


def test_folder_layering_skipped_by_default(tmp_path: Path, monkeypatch):
    # without opting into the advanced folder step, layers are schema-only
    make_repos(tmp_path)
    answers = {
        "Project name": "Schemas Only",
        "SQL repo path": "./sql-repo",
        "Power BI repo path": "./pbi-repo",
        "Markdown output": "./docs",
        "HTML site": "./site",
        "Gold layer — schemas": "mart, common, silver",
    }
    monkeypatch.setattr(wizard, "questionary", RoutedQuestionary(default_router(answers)))
    config = wizard.run_setup(tmp_path / "coop-data-doc.yml")
    assert config is not None
    assert config.layers["gold"].schemas == ["mart", "common", "silver"]
    assert config.layers["gold"].paths == []  # no folder prompts were shown


def test_wizard_reprompts_when_site_dir_nested(tmp_path: Path, monkeypatch):
    # entering a site_dir inside the markdown dir must re-ask, not save a broken config
    make_repos(tmp_path)
    site_answers = iter(["./docs/site", "./docs-site"])  # bad first, good second

    def router(kind, message, kwargs):
        if "Project name" in message:
            return "Estate"
        if "SQL repo path" in message:
            return "./sql-repo"
        if "Power BI repo path" in message:
            return "./pbi-repo"
        if "Markdown output" in message:
            return "./docs"
        if "HTML site" in message:
            return next(site_answers)
        if kind == "confirm":
            return False
        return kwargs.get("default", "")

    fake = RoutedQuestionary(router)
    monkeypatch.setattr(wizard, "questionary", fake)
    config = wizard.run_setup(tmp_path / "coop-data-doc.yml")
    assert config is not None
    assert config.site_dir() == (tmp_path / "docs-site").resolve()
    # the site prompt was shown twice (rejected once, accepted once)
    assert sum("HTML site" in msg for _, msg, _ in fake.calls) == 2


def test_wizard_scopes_to_selected_semantic_models(tmp_path: Path, monkeypatch):
    # when the PBI repo has .SemanticModel folders, the wizard lets the user pick
    # which to document and scopes the include globs to those (plus reports)
    (tmp_path / "sql-repo").mkdir()
    pbi = tmp_path / "pbi-repo"
    (pbi / "Sales.SemanticModel" / "definition").mkdir(parents=True)
    (pbi / "Finance.SemanticModel" / "definition").mkdir(parents=True)
    (pbi / "Sales.Report" / "definition").mkdir(parents=True)
    answers = {
        "Project name": "Scoped",
        "SQL repo path": "./sql-repo",
        "Power BI repo path": "./pbi-repo",
        "Markdown output": "./docs",
        "HTML site": "./site",
        "Semantic models to include": ["Sales.SemanticModel"],  # Finance deselected
        "Gold layer — schemas": "mart",
    }
    fake = RoutedQuestionary(default_router(answers))
    monkeypatch.setattr(wizard, "questionary", fake)
    config = wizard.run_setup(tmp_path / "coop-data-doc.yml")
    assert config is not None
    inc = config.repos["powerbi"].include
    assert "**/Sales.SemanticModel/**/*.tmdl" in inc
    assert not any("Finance.SemanticModel" in g for g in inc)  # the deselected model is gone
    assert "**/report.json" in inc and "**/visual.json" in inc  # reports still included
    assert not any(".pbix" in g for g in inc)  # .pbix / loose files excluded


def test_skip_bronze_and_silver(tmp_path: Path, monkeypatch):
    make_repos(tmp_path)
    answers = {
        "Project name": "Gold Only",
        "SQL repo path": "./sql-repo",
        "Power BI repo path": "./pbi-repo",
        "Markdown output": "./docs",
        "HTML site": "./site",
        "Gold layer — schemas": "mart",
    }
    fake = RoutedQuestionary(default_router(answers))
    monkeypatch.setattr(wizard, "questionary", fake)
    config_path = tmp_path / "coop-data-doc.yml"

    config = wizard.run_setup(config_path)
    assert config is not None
    assert set(config.layers) == {"gold"}  # bronze + silver skipped
    assert config.layers["gold"].schemas == ["mart"]


def test_rerun_prefills_layers(tmp_path: Path, monkeypatch):
    make_repos(tmp_path)
    config_path = tmp_path / "coop-data-doc.yml"
    answers = {
        "Project name": "Estate",
        "SQL repo path": "./sql-repo",
        "Power BI repo path": "./pbi-repo",
        "Markdown output": "./docs",
        "HTML site": "./site",
        "Bronze layer — schemas": "erp_orders",
        "Gold layer — schemas": "mart",
    }
    monkeypatch.setattr(wizard, "questionary", RoutedQuestionary(default_router(answers)))
    assert wizard.run_setup(config_path) is not None

    second = RoutedQuestionary(default_router(answers))
    monkeypatch.setattr(wizard, "questionary", second)
    wizard.run_setup(config_path)
    defaults = {msg: kw.get("default") for _, msg, kw in second.calls}
    bronze_default = next(v for m, v in defaults.items() if "Bronze layer — schemas" in m)
    assert bronze_default == "erp_orders"


def make_repos_with_folders(tmp_path: Path, sql_folders, pbi_folders):
    for name in sql_folders:
        (tmp_path / "sql-repo" / name).mkdir(parents=True)
    for name in pbi_folders:
        (tmp_path / "pbi-repo" / name).mkdir(parents=True)
    if not sql_folders:
        (tmp_path / "sql-repo").mkdir(exist_ok=True)
    if not pbi_folders:
        (tmp_path / "pbi-repo").mkdir(exist_ok=True)


def test_folder_checkbox_unchecked_become_excludes(tmp_path: Path, monkeypatch):
    # repos with real subfolders → the skip step is a checkbox; unchecking a
    # folder writes a **/Name/** exclude for it, checked folders write nothing
    make_repos_with_folders(
        tmp_path,
        sql_folders=["archive", "deployment", "procs", "tables"],
        pbi_folders=["backup", "reports"],
    )

    def router(kind, message, kwargs):
        if kind == "checkbox" and "SQL" in message:
            return ["procs", "tables"]  # keep these two, skip archive + deployment
        if kind == "checkbox" and "Power BI" in message:
            return ["reports"]  # skip backup
        if "Project name" in message:
            return "Estate"
        if "SQL repo path" in message:
            return "./sql-repo"
        if "Power BI repo path" in message:
            return "./pbi-repo"
        if "Markdown output" in message:
            return "./docs"
        if "HTML site" in message:
            return "./site"
        if kind == "confirm":
            return False
        return kwargs.get("default", "")

    monkeypatch.setattr(wizard, "questionary", RoutedQuestionary(router))
    config = wizard.run_setup(tmp_path / "coop-data-doc.yml")
    assert config is not None
    # excludes follow the sorted folder order, only for the unchecked folders
    assert config.repos["sql"].exclude == ["**/archive/**", "**/deployment/**"]
    assert config.repos["powerbi"].exclude == ["**/backup/**"]


def test_folder_checkbox_keep_all_yields_no_excludes(tmp_path: Path, monkeypatch):
    # default_router keeps every box checked (like pressing Enter) → no excludes
    make_repos_with_folders(tmp_path, sql_folders=["procs", "tables"], pbi_folders=["reports"])
    answers = {
        "Project name": "Estate",
        "SQL repo path": "./sql-repo",
        "Power BI repo path": "./pbi-repo",
        "Markdown output": "./docs",
        "HTML site": "./site",
    }
    monkeypatch.setattr(wizard, "questionary", RoutedQuestionary(default_router(answers)))
    config = wizard.run_setup(tmp_path / "coop-data-doc.yml")
    assert config is not None
    assert config.repos["sql"].exclude == []
    assert config.repos["powerbi"].exclude == []


def test_folder_checkbox_rerun_prefills_and_preserves_custom(tmp_path: Path, monkeypatch):
    # a re-run pre-unchecks already-excluded top-level folders and keeps any
    # hand-written nested glob (**/BACKUP/**) that no folder maps to
    make_repos_with_folders(tmp_path, sql_folders=["archive", "procs", "tables"], pbi_folders=[])
    config_path = tmp_path / "coop-data-doc.yml"
    config_path.write_text(
        render_config_yaml(
            project_name="Estate",
            sql_path="./sql-repo",
            pbi_path="./pbi-repo",
            mappings=[],
            sql_exclude=["**/archive/**", "**/BACKUP/**"],
            output_dir="./docs",
            site_dir="./site",
        ),
        encoding="utf-8",
        newline="\n",
    )

    captured: dict = {}

    def router(kind, message, kwargs):
        if kind == "checkbox" and "SQL" in message:
            captured["sql_choices"] = kwargs["choices"]
            return [c.value for c in kwargs["choices"] if c.checked]  # honor prefilled state
        if "Project name" in message:
            return "Estate"
        if "SQL repo path" in message:
            return "./sql-repo"
        if "Power BI repo path" in message:
            return "./pbi-repo"
        if "Markdown output" in message:
            return "./docs"
        if "HTML site" in message:
            return "./site"
        if kind == "confirm":
            return False
        return kwargs.get("default", "")

    monkeypatch.setattr(wizard, "questionary", RoutedQuestionary(router))
    config = wizard.run_setup(config_path)
    assert config is not None
    # archive came in pre-unchecked; procs/tables checked
    checked = {c.value: c.checked for c in captured["sql_choices"]}
    assert checked == {"archive": False, "procs": True, "tables": True}
    # nested custom glob preserved, archive still skipped
    assert config.repos["sql"].exclude == ["**/BACKUP/**", "**/archive/**"]


def test_folder_skip_falls_back_to_text_when_repo_absent(tmp_path: Path, monkeypatch):
    # repo path the user hasn't cloned yet → no folders to list → type globs
    (tmp_path / "pbi-repo").mkdir()
    answers = {
        "Project name": "Estate",
        "Power BI repo path": "./pbi-repo",
        "Markdown output": "./docs",
        "HTML site": "./site",
        "SQL — folders to SKIP": "**/archive/**",
    }

    def router(kind, message, kwargs):
        if "SQL repo path" in message:
            return "./not-cloned-yet"  # doesn't exist
        if "doesn't exist" in message:  # "use it anyway?" confirm
            return True
        for key, value in answers.items():
            if key.lower() in message.lower():
                return value
        if kind == "confirm":
            return False
        if kind == "checkbox":
            return [c.value for c in kwargs.get("choices", [])]
        return kwargs.get("default", "")

    fake = RoutedQuestionary(router)
    monkeypatch.setattr(wizard, "questionary", fake)
    config = wizard.run_setup(tmp_path / "coop-data-doc.yml")
    assert config is None  # saved but not runnable (repo missing) — still wrote the file
    # the SQL skip step degraded to the comma-separated text prompt
    assert any(kind == "text" and "SQL — folders to SKIP" in msg for kind, msg, _ in fake.calls)
    assert not any(kind == "checkbox" and "SQL" in msg for kind, msg, _ in fake.calls)


def test_folder_name_from_glob_contract():
    # the parser that lets a re-run map a saved exclude back to a checkbox
    from coop_data_doc.folders import folder_name_from_glob as f

    assert f("**/archive/**") == "archive"
    assert f("archive/**") == "archive"  # legacy prefix-less form
    assert f("archive/*") == "archive"  # legacy single-star form
    assert f("**/Editor and Theme Files/**") == "Editor and Theme Files"
    assert f("**/back[[]up]/**") == "back[up]"  # escaped metachars round-trip
    assert f("**/a/b/**") is None  # nested path → preserved as custom
    assert f("**/data*/**") is None  # real wildcard pattern → custom
    assert f("**/*.sql") is None  # file glob → custom
    assert f("Name") is None  # no recognized suffix → custom


def test_folder_checkbox_escapes_metachar_names_so_crawler_excludes_correctly(tmp_path: Path, monkeypatch):
    # a folder whose name contains fnmatch metacharacters must still be skipped
    # literally — and must NOT bleed into a similarly-named sibling
    from coop_data_doc.crawler import crawl

    (tmp_path / "sql-repo" / "back[up]").mkdir(parents=True)
    (tmp_path / "sql-repo" / "backu").mkdir(parents=True)
    (tmp_path / "sql-repo" / "back[up]" / "old.sql").write_text("select 1", encoding="utf-8")
    (tmp_path / "sql-repo" / "backu" / "live.sql").write_text("select 1", encoding="utf-8")
    (tmp_path / "pbi-repo").mkdir()

    def router(kind, message, kwargs):
        if kind == "checkbox" and "SQL" in message:
            return ["backu"]  # uncheck the metacharacter folder, keep its sibling
        if "Project name" in message:
            return "Estate"
        if "SQL repo path" in message:
            return "./sql-repo"
        if "Power BI repo path" in message:
            return "./pbi-repo"
        if "Markdown output" in message:
            return "./docs"
        if "HTML site" in message:
            return "./site"
        if kind == "confirm":
            return False
        return kwargs.get("default", "")

    monkeypatch.setattr(wizard, "questionary", RoutedQuestionary(router))
    config = wizard.run_setup(tmp_path / "coop-data-doc.yml")
    assert config is not None
    assert config.repos["sql"].exclude == ["**/back[[]up]/**"]  # escaped, not raw

    inventory, _ = crawl(config)
    paths = {entry.path for entry in inventory.entries}
    assert "backu/live.sql" in paths  # checked sibling kept
    assert "back[up]/old.sql" not in paths  # unchecked metachar folder really skipped


def test_folder_checkbox_rerun_preserves_nested_custom_glob(tmp_path: Path, monkeypatch):
    # a genuinely nested skip pattern (**/a/b/**) isn't a folder toggle; it must
    # survive a re-run untouched alongside the folder-checkbox excludes
    make_repos_with_folders(tmp_path, sql_folders=["archive", "procs", "tables"], pbi_folders=[])
    config_path = tmp_path / "coop-data-doc.yml"
    config_path.write_text(
        render_config_yaml(
            project_name="Estate",
            sql_path="./sql-repo",
            pbi_path="./pbi-repo",
            mappings=[],
            sql_exclude=["**/a/b/**", "**/archive/**"],
            output_dir="./docs",
            site_dir="./site",
        ),
        encoding="utf-8",
        newline="\n",
    )

    def router(kind, message, kwargs):
        if kind == "checkbox" and "SQL" in message:
            return [c.value for c in kwargs["choices"] if c.checked]  # honor prefill
        if "Project name" in message:
            return "Estate"
        if "SQL repo path" in message:
            return "./sql-repo"
        if "Power BI repo path" in message:
            return "./pbi-repo"
        if "Markdown output" in message:
            return "./docs"
        if "HTML site" in message:
            return "./site"
        if kind == "confirm":
            return False
        return kwargs.get("default", "")

    monkeypatch.setattr(wizard, "questionary", RoutedQuestionary(router))
    config = wizard.run_setup(config_path)
    assert config is not None
    # **/a/b/** kept verbatim; archive (a real top-level folder) stays skipped
    assert config.repos["sql"].exclude == ["**/a/b/**", "**/archive/**"]


def test_folder_checkbox_rechecking_drops_its_exclude(tmp_path: Path, monkeypatch):
    # re-running and re-checking a previously-skipped folder removes its glob
    make_repos_with_folders(tmp_path, sql_folders=["archive", "procs", "tables"], pbi_folders=[])
    config_path = tmp_path / "coop-data-doc.yml"
    config_path.write_text(
        render_config_yaml(
            project_name="Estate",
            sql_path="./sql-repo",
            pbi_path="./pbi-repo",
            mappings=[],
            sql_exclude=["**/archive/**"],
            output_dir="./docs",
            site_dir="./site",
        ),
        encoding="utf-8",
        newline="\n",
    )

    def router(kind, message, kwargs):
        if kind == "checkbox" and "SQL" in message:
            return [c.value for c in kwargs["choices"]]  # re-check EVERYTHING incl. archive
        if "Project name" in message:
            return "Estate"
        if "SQL repo path" in message:
            return "./sql-repo"
        if "Power BI repo path" in message:
            return "./pbi-repo"
        if "Markdown output" in message:
            return "./docs"
        if "HTML site" in message:
            return "./site"
        if kind == "confirm":
            return False
        return kwargs.get("default", "")

    monkeypatch.setattr(wizard, "questionary", RoutedQuestionary(router))
    config = wizard.run_setup(config_path)
    assert config is not None
    assert config.repos["sql"].exclude == []  # archive re-checked → exclude dropped


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
