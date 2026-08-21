from pathlib import Path

import pytest
import questionary as _questionary

from coop_data_doc import wizard, wizard_io
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
        "Bronze layer — schemas": "erp_orders, erp_finance",
        "Silver layer — schemas": "stg",
        "Gold layer — schemas": "mart, common, silver",
        "FOLDER instead of a schema": True,  # opt into the advanced folder step
        "Gold layer — folder": "**/dim/**, **/fact/**",
    }
    fake = RoutedQuestionary(default_router(answers))
    monkeypatch.setattr(wizard_io, "questionary", fake)
    config_path = tmp_path / "coop-data-doc.yml"

    config = wizard.run_setup(config_path)

    assert config is not None
    assert config.project_name == "My Estate"
    assert config.repos["sql"].include == ["**/*.sql"]
    assert config.repos["sql"].exclude == []
    assert config.repos["powerbi"].exclude == []
    assert config.layers["bronze"].schemas == ["erp_orders", "erp_finance"]
    assert config.layers["silver"].schemas == ["stg"]
    assert config.layers["gold"].schemas == ["mart", "common", "silver"]
    # the allowlist is the union of every schema checked in the layer questions
    assert config.include_schemas == ["erp_orders", "erp_finance", "stg", "mart", "common", "silver"]
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
    monkeypatch.setattr(wizard_io, "questionary", RoutedQuestionary(default_router(answers)))
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
    monkeypatch.setattr(wizard_io, "questionary", fake)
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
        "Power BI — pick the folders": ["Sales.SemanticModel", "Sales.Report"],  # report scope
        "Gold layer — schemas": "mart",
    }
    fake = RoutedQuestionary(default_router(answers))
    monkeypatch.setattr(wizard_io, "questionary", fake)
    config = wizard.run_setup(tmp_path / "coop-data-doc.yml")
    assert config is not None
    inc = config.repos["powerbi"].include
    assert "**/Sales.SemanticModel/**/*.tmdl" in inc
    assert not any("Finance.SemanticModel" in g for g in inc)  # the deselected model is gone
    # the report/pbix globs are scoped to the picked folders, not global
    assert "Sales.Report/**/report.json" in inc and "Sales.Report/**/visual.json" in inc
    assert "Sales.SemanticModel/**/*.pbix" in inc
    assert "**/report.json" not in inc and "**/*.pbix" not in inc


def test_wizard_scoped_config_documents_pbix_only_report(tmp_path: Path, monkeypatch):
    # a report that exists ONLY as a .pbix must still be documented by a
    # wizard-scoped config, matching a default init config (issue #28) — the
    # wizard's own prompt promises "reports are still included". The pbix lives
    # inside the picked folder: the folder pick scopes the pbix glob, so a pbix
    # outside every picked folder would (deliberately) not be crawled.
    from test_pbi_parsers import make_pbix

    from coop_data_doc.cli import run_pipeline

    (tmp_path / "sql-repo").mkdir()
    pbi = tmp_path / "pbi-repo"
    (pbi / "Sales.SemanticModel" / "definition").mkdir(parents=True)
    (pbi / "Sales.SemanticModel" / "definition" / "model.tmdl").write_text(
        "model Sales\n\tculture: en-US\n", encoding="utf-8"
    )
    make_pbix(pbi / "Sales.SemanticModel" / "LoneReport.pbix")  # the only home of this report
    answers = {
        "Project name": "Pbix",
        "SQL repo path": "./sql-repo",
        "Power BI repo path": "./pbi-repo",
        "Markdown output": "./docs",
        "HTML site": "./site",
        "Semantic models to include": ["Sales.SemanticModel"],
    }
    monkeypatch.setattr(wizard_io, "questionary", RoutedQuestionary(default_router(answers)))
    config = wizard.run_setup(tmp_path / "coop-data-doc.yml")
    assert config is not None
    assert "Sales.SemanticModel/**/*.pbix" in config.repos["powerbi"].include  # wizard scoped the pbix in
    graph, _, _ = run_pipeline(config, interactive=False)
    assert "report:lonereport" in graph.nodes  # the pbix-only report got documented


def test_wizard_rerun_preserves_pbix_glob_and_prechecks_models(tmp_path: Path, monkeypatch):
    # re-running setup over a wizard-scoped config keeps documenting pbix files
    # (now via folder-scoped globs) and pre-checks exactly the .SemanticModel
    # folders the prior include scoped to (the .pbix glob must not confuse
    # _previously_selected_models) — issue #28.
    (tmp_path / "sql-repo").mkdir()
    pbi = tmp_path / "pbi-repo"
    (pbi / "Sales.SemanticModel" / "definition").mkdir(parents=True)
    (pbi / "Finance.SemanticModel" / "definition").mkdir(parents=True)
    config_path = tmp_path / "coop-data-doc.yml"
    config_path.write_text(
        render_config_yaml(
            project_name="Estate",
            sql_path="./sql-repo",
            pbi_path="./pbi-repo",
            mappings=[],
            pbi_include=[
                "**/Sales.SemanticModel/**/*.tmdl",
                "**/Sales.SemanticModel/**/*.bim",
                "**/report.json",
                "**/visual.json",
                "**/page.json",
                "**/*.pbix",
            ],
            output_dir="./docs",
            site_dir="./site",
        ),
        encoding="utf-8",
        newline="\n",
    )
    captured: dict = {}

    def router(kind, message, kwargs):
        if kind == "checkbox" and "Semantic models to include" in message:
            captured["model_choices"] = kwargs["choices"]
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
        if kind == "checkbox":  # any folder/layer checkbox: check everything offered
            return [c.value for c in kwargs.get("choices", [])]
        if kind == "confirm":
            return False
        return kwargs.get("default", "")

    monkeypatch.setattr(wizard_io, "questionary", RoutedQuestionary(router))
    config = wizard.run_setup(config_path)
    assert config is not None
    # the global pbix glob is re-scoped to the picked folders (both were checked)
    assert "Sales.SemanticModel/**/*.pbix" in config.repos["powerbi"].include
    assert "Finance.SemanticModel/**/*.pbix" in config.repos["powerbi"].include
    checked = {c.value: c.checked for c in captured["model_choices"]}
    assert checked["Sales.SemanticModel"] is True  # scoped in the prior include -> pre-checked
    assert checked["Finance.SemanticModel"] is False  # not in prior include -> unchecked


def test_wizard_pbi_folder_checkbox_scopes_only_reports_when_models_found(tmp_path: Path, monkeypatch):
    # with .SemanticModel folders on disk the model picker scopes the MODELS; the
    # top-level-folder checkbox still appears, but only to scope the report/pbix
    # globs — the picked model's tmdl/bim globs are unaffected by the folder pick
    # (issue #22's "don't silently drop the model" concern).
    (tmp_path / "sql-repo").mkdir()
    pbi = tmp_path / "pbi-repo"
    (pbi / "Sales.SemanticModel" / "definition").mkdir(parents=True)
    (pbi / "Documentation").mkdir()  # an extra top-level folder the checkbox offers
    answers = {
        "Project name": "Scoped",
        "SQL repo path": "./sql-repo",
        "Power BI repo path": "./pbi-repo",
        "Markdown output": "./docs",
        "HTML site": "./site",
        "Semantic models to include": ["Sales.SemanticModel"],
        "Power BI — pick the folders": ["Documentation"],  # reports live here, not beside the model
    }
    fake = RoutedQuestionary(default_router(answers))
    monkeypatch.setattr(wizard_io, "questionary", fake)
    config = wizard.run_setup(tmp_path / "coop-data-doc.yml")
    assert config is not None
    # the PBI folder checkbox WAS shown (as the report-scope question)
    assert any(kind == "checkbox" and "Power BI — pick the folders" in msg for kind, msg, _ in fake.calls)
    inc = config.repos["powerbi"].include
    # the picked model is unaffected by the folder pick…
    assert "**/Sales.SemanticModel/**/*.tmdl" in inc
    # …while the report globs are scoped to the picked folder only
    assert "Documentation/**/report.json" in inc
    assert not any(g.startswith("Sales.SemanticModel/**/report.json") for g in inc)
    assert config.repos["powerbi"].exclude == []


def test_wizard_preserves_custom_pbi_excludes_when_models_found(tmp_path: Path, monkeypatch):
    # a hand-written exclude in a loaded config survives a re-run even though the
    # folder-skip checkbox is now skipped on the models-found path (issue #22).
    (tmp_path / "sql-repo").mkdir()
    pbi = tmp_path / "pbi-repo"
    (pbi / "Sales.SemanticModel" / "definition").mkdir(parents=True)
    config_path = tmp_path / "coop-data-doc.yml"
    config_path.write_text(
        render_config_yaml(
            project_name="Estate",
            sql_path="./sql-repo",
            pbi_path="./pbi-repo",
            mappings=[],
            pbi_include=["**/Sales.SemanticModel/**/*.tmdl", "**/report.json", "**/*.pbix"],
            pbi_exclude=["**/BACKUP/**"],
            output_dir="./docs",
            site_dir="./site",
        ),
        encoding="utf-8",
        newline="\n",
    )
    answers = {
        "Project name": "Estate",
        "SQL repo path": "./sql-repo",
        "Power BI repo path": "./pbi-repo",
        "Markdown output": "./docs",
        "HTML site": "./site",
        "Semantic models to include": ["Sales.SemanticModel"],
    }
    monkeypatch.setattr(wizard_io, "questionary", RoutedQuestionary(default_router(answers)))
    config = wizard.run_setup(config_path)
    assert config is not None
    assert config.repos["powerbi"].exclude == ["**/BACKUP/**"]  # custom exclude preserved


def test_wizard_reprompts_when_no_model_selected(tmp_path: Path, monkeypatch):
    # unchecking every model re-prompts instead of silently selecting all (#22).
    (tmp_path / "sql-repo").mkdir()
    pbi = tmp_path / "pbi-repo"
    (pbi / "Sales.SemanticModel" / "definition").mkdir(parents=True)
    model_answers = iter([[], ["Sales.SemanticModel"]])  # empty first -> re-ask

    def router(kind, message, kwargs):
        if kind == "checkbox" and "Semantic models to include" in message:
            return next(model_answers)
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
        if kind == "checkbox":
            return [c.value for c in kwargs.get("choices", [])]
        if kind == "confirm":
            return False
        return kwargs.get("default", "")

    fake = RoutedQuestionary(router)
    monkeypatch.setattr(wizard_io, "questionary", fake)
    config = wizard.run_setup(tmp_path / "coop-data-doc.yml")
    assert config is not None
    # the model picker was shown twice: the empty selection re-prompted
    assert sum("Semantic models to include" in msg for _, msg, _ in fake.calls) == 2
    assert "**/Sales.SemanticModel/**/*.tmdl" in config.repos["powerbi"].include


def test_wizard_autosuggests_schema_mapping_from_dry_run(tmp_path: Path, monkeypatch):
    # a model whose M-code reads schema "sales" but whose object actually lives
    # in SQL schema "mart" is unresolved; the wizard dry-run derives "mart" from
    # where the name lives and the user confirms — no blind typing.
    sql = tmp_path / "sql-repo"
    sql.mkdir()
    (sql / "fact.sql").write_text(
        "CREATE VIEW mart.fact_sales AS SELECT a FROM mart.base;\nGO\n", encoding="utf-8"
    )
    defn = tmp_path / "pbi-repo" / "X.SemanticModel" / "definition"
    (defn / "tables").mkdir(parents=True)
    (defn / "model.tmdl").write_text("model Model\n\tculture: en-US\n", encoding="utf-8")
    (defn / "tables" / "fact_sales.tmdl").write_text(
        "table fact_sales\n"
        "\tcolumn a\n"
        "\t\tdataType: int64\n"
        "\tpartition fact_sales = m\n"
        "\t\tmode: import\n"
        "\t\tsource =\n"
        "\t\t\t\tlet\n"
        '\t\t\t\t    Source = Sql.Database("srv", "wh"),\n'
        '\t\t\t\t    d = Source{[Schema="sales",Item="fact_sales"]}[Data]\n'
        "\t\t\t\tin\n"
        "\t\t\t\t    d\n",
        encoding="utf-8",
    )

    def router(kind, message, kwargs):
        m = message.lower()
        if "project name" in m:
            return "MapTest"
        if "sql repo path" in m:
            return "./sql-repo"
        if "power bi repo path" in m:
            return "./pbi-repo"
        if "markdown output" in m:
            return "./docs"
        if "html site" in m:
            return "./site"
        if "map x" in m and kind == "confirm":  # "Map X → mart?"
            return True
        if kind == "confirm":
            return False
        if kind == "checkbox":
            return [c.value for c in kwargs.get("choices", [])]
        return kwargs.get("default", "")

    monkeypatch.setattr(wizard_io, "questionary", RoutedQuestionary(router))
    config = wizard.run_setup(tmp_path / "coop-data-doc.yml")
    assert config is not None
    rules = {(m.schema_name, m.model) for m in config.schema_mappings}
    assert ("mart", "X") in rules  # derived + confirmed, not typed


def test_wizard_dry_run_leaves_lineage_cache_untouched(tmp_path: Path, monkeypatch):
    # The dry-run scans with a candidate (often narrower) config; a committed
    # answer whose target isn't in that scan must survive byte-identically —
    # the wizard's "Ctrl-C writes nothing" promise extends to the cache file.
    sql = tmp_path / "sql-repo"
    sql.mkdir()
    (sql / "fact.sql").write_text(
        "CREATE VIEW mart.fact_sales AS SELECT a FROM mart.base;\nGO\n", encoding="utf-8"
    )
    defn = tmp_path / "pbi-repo" / "X.SemanticModel" / "definition"
    (defn / "tables").mkdir(parents=True)
    (defn / "model.tmdl").write_text("model Model\n\tculture: en-US\n", encoding="utf-8")
    (defn / "tables" / "fact_sales.tmdl").write_text(
        "table fact_sales\n"
        "\tcolumn a\n"
        "\t\tdataType: int64\n"
        "\tpartition fact_sales = m\n"
        "\t\tmode: import\n"
        "\t\tsource =\n"
        "\t\t\t\tlet\n"
        '\t\t\t\t    Source = Sql.Database("srv", "wh"),\n'
        '\t\t\t\t    d = Source{[Schema="sales",Item="fact_sales"]}[Data]\n'
        "\t\t\t\tin\n"
        "\t\t\t\t    d\n",
        encoding="utf-8",
    )
    cache_file = tmp_path / ".lineage-cache.json"
    cache_file.write_text(
        '{\n  "version": 1,\n  "mappings": {\n'
        '    "pbi_table:x.answered_elsewhere": {\n'
        '      "target": "view:sales.only_on_other_branch",\n'
        '      "method": "interactive"\n    }\n  }\n}\n',
        encoding="utf-8",
        newline="\n",
    )
    before = cache_file.read_bytes()

    def router(kind, message, kwargs):
        m = message.lower()
        if "project name" in m:
            return "MapTest"
        if "sql repo path" in m:
            return "./sql-repo"
        if "power bi repo path" in m:
            return "./pbi-repo"
        if "markdown output" in m:
            return "./docs"
        if "html site" in m:
            return "./site"
        if "map x" in m and kind == "confirm":
            return True
        if kind == "confirm":
            return False
        if kind == "checkbox":
            return [c.value for c in kwargs.get("choices", [])]
        return kwargs.get("default", "")

    monkeypatch.setattr(wizard_io, "questionary", RoutedQuestionary(router))
    config = wizard.run_setup(tmp_path / "coop-data-doc.yml")
    assert config is not None
    assert cache_file.read_bytes() == before


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
    monkeypatch.setattr(wizard_io, "questionary", fake)
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
    monkeypatch.setattr(wizard_io, "questionary", RoutedQuestionary(default_router(answers)))
    assert wizard.run_setup(config_path) is not None

    second = RoutedQuestionary(default_router(answers))
    monkeypatch.setattr(wizard_io, "questionary", second)
    wizard.run_setup(config_path)
    defaults = {msg: kw.get("default") for _, msg, kw in second.calls}
    bronze_default = next(v for m, v in defaults.items() if "Bronze layer — schemas" in m)
    assert bronze_default == "erp_orders"


def test_rerun_preserves_reviews(tmp_path: Path, monkeypatch):
    # issue #40: re-running setup over a config with a reviews: list must
    # carry it through verbatim (setup never prompts for it)
    make_repos(tmp_path)
    config_path = tmp_path / "coop-data-doc.yml"
    config_path.write_text(
        render_config_yaml(
            project_name="Estate",
            sql_path="./sql-repo",
            pbi_path="./pbi-repo",
            mappings=[],
            reviews=["reviews/sql.json", "reviews/dax.json"],
        ),
        encoding="utf-8",
        newline="\n",
    )
    answers = {
        "Project name": "Estate",
        "SQL repo path": "./sql-repo",
        "Power BI repo path": "./pbi-repo",
        "Markdown output": "./docs",
        "HTML site": "./site",
        "Gold layer — schemas": "mart",
    }
    monkeypatch.setattr(wizard_io, "questionary", RoutedQuestionary(default_router(answers)))
    config = wizard.run_setup(config_path)
    assert config is not None
    assert config.reviews == ["reviews/sql.json", "reviews/dax.json"]
    text = config_path.read_text(encoding="utf-8")
    assert '  - "reviews/sql.json"\n  - "reviews/dax.json"' in text


def test_fresh_setup_emits_no_reviews_key(tmp_path: Path, monkeypatch):
    # a fresh setup (no existing config) must not write a reviews: block
    make_repos(tmp_path)
    answers = {
        "Project name": "Estate",
        "SQL repo path": "./sql-repo",
        "Power BI repo path": "./pbi-repo",
        "Markdown output": "./docs",
        "HTML site": "./site",
        "Gold layer — schemas": "mart",
    }
    monkeypatch.setattr(wizard_io, "questionary", RoutedQuestionary(default_router(answers)))
    config_path = tmp_path / "coop-data-doc.yml"
    config = wizard.run_setup(config_path)
    assert config is not None
    assert config.reviews == []
    assert "reviews:" not in config_path.read_text(encoding="utf-8")


def make_repos_with_folders(tmp_path: Path, sql_folders, pbi_folders):
    for name in sql_folders:
        (tmp_path / "sql-repo" / name).mkdir(parents=True)
    for name in pbi_folders:
        (tmp_path / "pbi-repo" / name).mkdir(parents=True)
    if not sql_folders:
        (tmp_path / "sql-repo").mkdir(exist_ok=True)
    if not pbi_folders:
        (tmp_path / "pbi-repo").mkdir(exist_ok=True)


def test_folder_checkbox_checked_become_include_globs(tmp_path: Path, monkeypatch):
    # repos with real subfolders → the folder step is an ALLOWLIST checkbox;
    # checking a folder writes Folder-scoped include globs for it, unchecked
    # folders are simply not crawled (no excludes are written)
    make_repos_with_folders(
        tmp_path,
        sql_folders=["archive", "deployment", "procs", "tables"],
        pbi_folders=["backup", "reports"],
    )

    def router(kind, message, kwargs):
        if kind == "checkbox" and "SQL" in message:
            return ["procs", "tables"]  # document these two, leave archive + deployment out
        if kind == "checkbox" and "Power BI" in message:
            return ["reports"]  # leave backup out
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

    monkeypatch.setattr(wizard_io, "questionary", RoutedQuestionary(router))
    config = wizard.run_setup(tmp_path / "coop-data-doc.yml")
    assert config is not None
    assert config.repos["sql"].include == ["procs/**/*.sql", "tables/**/*.sql"]
    assert config.repos["sql"].exclude == []
    # no .SemanticModel folders → the PBI folder pick scopes the full PBI file-type set
    assert "reports/**/*.tmdl" in config.repos["powerbi"].include
    assert not any(g.startswith("backup/") for g in config.repos["powerbi"].include)


def test_folder_checkbox_first_run_nothing_prechecked(tmp_path: Path, monkeypatch):
    # first run: the allowlist checkbox starts with NOTHING checked — checking
    # everything documents every folder via scoped include globs
    make_repos_with_folders(tmp_path, sql_folders=["procs", "tables"], pbi_folders=["reports"])
    captured: dict = {}

    def router(kind, message, kwargs):
        if kind == "checkbox" and "SQL" in message:
            captured["sql_choices"] = kwargs["choices"]
            return [c.value for c in kwargs["choices"]]  # check everything
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
        if kind == "checkbox":
            return [c.value for c in kwargs.get("choices", [])]
        if kind == "confirm":
            return False
        return kwargs.get("default", "")

    monkeypatch.setattr(wizard_io, "questionary", RoutedQuestionary(router))
    config = wizard.run_setup(tmp_path / "coop-data-doc.yml")
    assert config is not None
    assert all(not c.checked for c in captured["sql_choices"])  # opt-in: nothing pre-checked
    assert config.repos["sql"].include == ["procs/**/*.sql", "tables/**/*.sql"]
    assert config.repos["sql"].exclude == []


def test_folder_checkbox_empty_selection_rejected(tmp_path: Path, monkeypatch):
    # an empty folder selection would silently document nothing — it is rejected
    # and re-asked instead (same pattern as the semantic-model picker)
    make_repos_with_folders(tmp_path, sql_folders=["procs", "tables"], pbi_folders=["reports"])
    sql_answers = iter([[], ["procs"]])  # empty first -> re-ask

    def router(kind, message, kwargs):
        if kind == "checkbox" and "SQL" in message:
            return next(sql_answers)
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
        if kind == "checkbox":
            return [c.value for c in kwargs.get("choices", [])]
        if kind == "confirm":
            return False
        return kwargs.get("default", "")

    fake = RoutedQuestionary(router)
    monkeypatch.setattr(wizard_io, "questionary", fake)
    config = wizard.run_setup(tmp_path / "coop-data-doc.yml")
    assert config is not None
    # the SQL folder checkbox was shown twice: the empty selection re-prompted
    assert sum(kind == "checkbox" and "SQL — pick the folders" in msg for kind, msg, _ in fake.calls) == 2
    assert config.repos["sql"].include == ["procs/**/*.sql"]


def test_folder_checkbox_rerun_prefills_from_scoped_includes(tmp_path: Path, monkeypatch):
    # a re-run pre-checks the folders already scoped in the include list and
    # keeps any hand-written nested exclude glob (**/BACKUP/**) untouched
    make_repos_with_folders(tmp_path, sql_folders=["archive", "procs", "tables"], pbi_folders=[])
    config_path = tmp_path / "coop-data-doc.yml"
    config_path.write_text(
        render_config_yaml(
            project_name="Estate",
            sql_path="./sql-repo",
            pbi_path="./pbi-repo",
            mappings=[],
            sql_include=["procs/**/*.sql", "tables/**/*.sql"],
            sql_exclude=["**/BACKUP/**"],
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

    monkeypatch.setattr(wizard_io, "questionary", RoutedQuestionary(router))
    config = wizard.run_setup(config_path)
    assert config is not None
    # procs/tables came in pre-checked; archive unchecked
    checked = {c.value: c.checked for c in captured["sql_choices"]}
    assert checked == {"archive": False, "procs": True, "tables": True}
    # honoring the prefill round-trips the include list untouched…
    assert config.repos["sql"].include == ["procs/**/*.sql", "tables/**/*.sql"]
    # …and the nested custom exclude glob survives
    assert config.repos["sql"].exclude == ["**/BACKUP/**"]


def test_folder_checkbox_legacy_excludes_read_as_unchecked(tmp_path: Path, monkeypatch):
    # a legacy denylist config (broad include + **/Name/** excludes) pre-checks
    # NOTHING — the excluded folder and its siblings all read as unchecked,
    # and the first selection converts the repo to allowlist mode
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

    captured: dict = {}

    def router(kind, message, kwargs):
        if kind == "checkbox" and "SQL" in message:
            captured["sql_choices"] = kwargs["choices"]
            return ["procs", "tables"]  # archive stays out, now via the allowlist
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

    monkeypatch.setattr(wizard_io, "questionary", RoutedQuestionary(router))
    config = wizard.run_setup(config_path)
    assert config is not None
    assert all(not c.checked for c in captured["sql_choices"])  # legacy config: nothing pre-checked
    assert config.repos["sql"].include == ["procs/**/*.sql", "tables/**/*.sql"]
    assert config.repos["sql"].exclude == []  # the **/archive/** exclude is superseded


def test_folder_include_falls_back_to_text_when_repo_absent(tmp_path: Path, monkeypatch):
    # repo path the user hasn't cloned yet → no folders to list → type globs
    (tmp_path / "pbi-repo").mkdir()
    answers = {
        "Project name": "Estate",
        "Power BI repo path": "./pbi-repo",
        "Markdown output": "./docs",
        "HTML site": "./site",
        "SQL — files/patterns to INCLUDE": "warehouse/**/*.sql",
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
    monkeypatch.setattr(wizard_io, "questionary", fake)
    config_path = tmp_path / "coop-data-doc.yml"
    config = wizard.run_setup(config_path)
    assert config is None  # saved but not runnable (repo missing) — still wrote the file
    # the SQL folder step degraded to the comma-separated text prompt
    assert any(kind == "text" and "SQL — files/patterns to INCLUDE" in msg for kind, msg, _ in fake.calls)
    assert not any(kind == "checkbox" and "SQL" in msg for kind, msg, _ in fake.calls)
    assert '"warehouse/**/*.sql"' in config_path.read_text(encoding="utf-8")  # typed glob was written


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


def test_folder_checkbox_escapes_metachar_names_so_crawler_includes_correctly(tmp_path: Path, monkeypatch):
    # a folder whose name contains fnmatch metacharacters must still be included
    # literally — and must NOT bleed into a similarly-named sibling
    from coop_data_doc.crawler import crawl

    (tmp_path / "sql-repo" / "back[up]").mkdir(parents=True)
    (tmp_path / "sql-repo" / "backu").mkdir(parents=True)
    (tmp_path / "sql-repo" / "back[up]" / "old.sql").write_text("select 1", encoding="utf-8")
    (tmp_path / "sql-repo" / "backu" / "live.sql").write_text("select 1", encoding="utf-8")
    (tmp_path / "pbi-repo").mkdir()

    def router(kind, message, kwargs):
        if kind == "checkbox" and "SQL" in message:
            return ["back[up]"]  # document the metacharacter folder, leave its sibling out
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

    monkeypatch.setattr(wizard_io, "questionary", RoutedQuestionary(router))
    config = wizard.run_setup(tmp_path / "coop-data-doc.yml")
    assert config is not None
    assert config.repos["sql"].include == ["back[[]up]/**/*.sql"]  # escaped, not raw

    inventory, _ = crawl(config)
    paths = {entry.path for entry in inventory.entries}
    assert "back[up]/old.sql" in paths  # checked metachar folder really crawled
    assert "backu/live.sql" not in paths  # unchecked sibling NOT swept in by the glob


def test_folder_checkbox_rerun_preserves_nested_custom_glob(tmp_path: Path, monkeypatch):
    # a genuinely nested skip pattern (**/a/b/**) isn't a folder toggle; it must
    # survive a re-run untouched alongside the allowlist include globs
    make_repos_with_folders(tmp_path, sql_folders=["archive", "procs", "tables"], pbi_folders=[])
    config_path = tmp_path / "coop-data-doc.yml"
    config_path.write_text(
        render_config_yaml(
            project_name="Estate",
            sql_path="./sql-repo",
            pbi_path="./pbi-repo",
            mappings=[],
            sql_include=["procs/**/*.sql"],
            sql_exclude=["**/a/b/**"],
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

    monkeypatch.setattr(wizard_io, "questionary", RoutedQuestionary(router))
    config = wizard.run_setup(config_path)
    assert config is not None
    # **/a/b/** kept verbatim; procs still the only documented folder
    assert config.repos["sql"].include == ["procs/**/*.sql"]
    assert config.repos["sql"].exclude == ["**/a/b/**"]


def test_folder_checkbox_unchecking_drops_its_scoped_globs(tmp_path: Path, monkeypatch):
    # re-running and unchecking a previously-documented folder removes its globs
    make_repos_with_folders(tmp_path, sql_folders=["archive", "procs", "tables"], pbi_folders=[])
    config_path = tmp_path / "coop-data-doc.yml"
    config_path.write_text(
        render_config_yaml(
            project_name="Estate",
            sql_path="./sql-repo",
            pbi_path="./pbi-repo",
            mappings=[],
            sql_include=["procs/**/*.sql", "tables/**/*.sql"],
            output_dir="./docs",
            site_dir="./site",
        ),
        encoding="utf-8",
        newline="\n",
    )

    def router(kind, message, kwargs):
        if kind == "checkbox" and "SQL" in message:
            return ["procs"]  # uncheck tables
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

    monkeypatch.setattr(wizard_io, "questionary", RoutedQuestionary(router))
    config = wizard.run_setup(config_path)
    assert config is not None
    assert config.repos["sql"].include == ["procs/**/*.sql"]  # tables unchecked → globs dropped


def make_sql_estate(tmp_path: Path):
    """Repos with real SQL in three schemas, so the wizard's scan discovers
    schemas to offer as layer checkboxes (issue #35)."""
    sql = tmp_path / "sql-repo"
    sql.mkdir()
    (sql / "marts.sql").write_text(
        "CREATE VIEW mart.v_sales AS SELECT a FROM stg.raw_orders;\nGO\n", encoding="utf-8"
    )
    (sql / "erp.sql").write_text("CREATE TABLE erp.customers (id INT);\nGO\n", encoding="utf-8")
    (tmp_path / "pbi-repo").mkdir()


def test_layer_checkboxes_from_scanned_schemas(tmp_path: Path, monkeypatch):
    # issue #35: with repos on disk the layer prompts are checkboxes over the
    # scanned schemas — confirm, don't type — and each schema is offered once
    # (a bronze pick disappears from the silver/gold choices). A checked schema
    # is both included AND layered; an unchecked one is excluded from the docs.
    make_sql_estate(tmp_path)
    captured: dict = {}

    def router(kind, message, kwargs):
        if kind == "checkbox" and "Bronze layer" in message:
            captured["bronze"] = kwargs["choices"]
            return ["erp"]
        if kind == "checkbox" and "Silver layer" in message:
            return []  # skip silver
        if kind == "checkbox" and "Gold layer" in message:
            captured["gold"] = kwargs["choices"]
            return ["mart"]
        if kind == "checkbox" and "Include WITHOUT a layer" in message:
            return []  # stg stays out entirely
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
        if kind == "checkbox":
            return [c.value for c in kwargs.get("choices", [])]
        if kind == "confirm":
            return False
        return kwargs.get("default", "")

    monkeypatch.setattr(wizard_io, "questionary", RoutedQuestionary(router))
    config = wizard.run_setup(tmp_path / "coop-data-doc.yml")
    assert config is not None
    # checkbox picks land in the config exactly like typed answers did
    assert config.layers["bronze"].schemas == ["erp"]
    assert config.layers["gold"].schemas == ["mart"]
    assert "silver" not in config.layers
    # the allowlist is the union of the checked schemas; stg was never checked
    assert config.include_schemas == ["erp", "mart"]
    # bronze offered every discovered schema (+ the typed escape hatch)
    bronze_values = [c.value for c in captured["bronze"]]
    assert bronze_values == ["erp", "mart", "stg", "__manual__"]
    # gold no longer offers the schema bronze took
    gold_values = [c.value for c in captured["gold"]]
    assert "erp" not in gold_values
    assert "mart" in gold_values and "stg" in gold_values


def test_layer_checkbox_manual_escape_hatch(tmp_path: Path, monkeypatch):
    # picking the "(add schemas by typing them next)" entry appends typed
    # schemas — for schemas not in the repo yet — without duplicates.
    make_sql_estate(tmp_path)

    def router(kind, message, kwargs):
        if kind == "checkbox" and "Gold layer" in message:
            return ["mart", "__manual__"]
        if "Gold layer — additional schemas" in message:
            return "future_gold, mart"  # mart already picked -> deduped
        if kind == "checkbox" and "layer" in message:
            return []  # skip bronze/silver
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
        if kind == "checkbox":
            return [c.value for c in kwargs.get("choices", [])]
        if kind == "confirm":
            return False
        return kwargs.get("default", "")

    monkeypatch.setattr(wizard_io, "questionary", RoutedQuestionary(router))
    config = wizard.run_setup(tmp_path / "coop-data-doc.yml")
    assert config is not None
    assert config.layers["gold"].schemas == ["mart", "future_gold"]
    # the typed schemas join the allowlist too (erp/stg were never checked)
    assert config.include_schemas == ["mart", "future_gold"]


def test_layer_checkbox_no_layer_catchall_includes_without_layer(tmp_path: Path, monkeypatch):
    # the catch-all checkbox documents a schema without forcing a layer on it:
    # it lands in include_schemas but in no layers.*.schemas rule (it gets the
    # read/write heuristic at build time)
    make_sql_estate(tmp_path)

    def router(kind, message, kwargs):
        if kind == "checkbox" and "Gold layer" in message:
            return ["mart"]
        if kind == "checkbox" and "Include WITHOUT a layer" in message:
            return ["stg"]  # document stg, layer it automatically
        if kind == "checkbox" and "layer" in message:
            return []  # skip bronze/silver
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
        if kind == "checkbox":
            return [c.value for c in kwargs.get("choices", [])]
        if kind == "confirm":
            return False
        return kwargs.get("default", "")

    monkeypatch.setattr(wizard_io, "questionary", RoutedQuestionary(router))
    config = wizard.run_setup(tmp_path / "coop-data-doc.yml")
    assert config is not None
    assert config.layers["gold"].schemas == ["mart"]
    assert all("stg" not in rule.schemas for rule in config.layers.values())  # no forced layer
    assert config.include_schemas == ["mart", "stg"]  # but stg IS documented


def test_layer_checkbox_empty_total_selection_rejected(tmp_path: Path, monkeypatch):
    # checking NOTHING at any layer (and nothing in the catch-all) would silently
    # document nothing — it is rejected and re-asked instead
    make_sql_estate(tmp_path)
    gold_answers = iter([[], ["mart"]])  # empty first -> re-ask

    def router(kind, message, kwargs):
        if kind == "checkbox" and "Gold layer" in message:
            return next(gold_answers)
        if kind == "checkbox" and "layer" in message:
            return []  # bronze/silver/catch-all: nothing
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
        if kind == "checkbox":
            return [c.value for c in kwargs.get("choices", [])]
        if kind == "confirm":
            return False
        return kwargs.get("default", "")

    fake = RoutedQuestionary(router)
    monkeypatch.setattr(wizard_io, "questionary", fake)
    config = wizard.run_setup(tmp_path / "coop-data-doc.yml")
    assert config is not None
    # the gold layer checkbox was shown twice: the empty total re-prompted
    assert sum(kind == "checkbox" and "Gold layer" in msg for kind, msg, _ in fake.calls) == 2
    assert config.layers["gold"].schemas == ["mart"]
    assert config.include_schemas == ["mart"]


def test_layer_checkbox_rerun_prechecks_saved_schemas(tmp_path: Path, monkeypatch):
    # re-running setup pre-checks the schemas already in each layer rule
    # (checkbox parity with test_rerun_prefills_layers), and keeps schemas
    # saved in a rule but no longer discovered (offered checked). A schema in
    # include_schemas but no layer rule pre-checks in the catch-all instead.
    make_sql_estate(tmp_path)
    config_path = tmp_path / "coop-data-doc.yml"
    config_path.write_text(
        render_config_yaml(
            project_name="Estate",
            sql_path="./sql-repo",
            pbi_path="./pbi-repo",
            mappings=[],
            layers={"gold": {"schemas": ["mart", "not_scanned_yet"], "paths": []}},
            include_schemas=["mart", "not_scanned_yet", "stg"],  # stg: included, not layered
            output_dir="./docs",
            site_dir="./site",
        ),
        encoding="utf-8",
        newline="\n",
    )
    captured: dict = {}

    def router(kind, message, kwargs):
        if kind == "checkbox" and "Gold layer" in message:
            captured["gold"] = kwargs["choices"]
            return [c.value for c in kwargs["choices"] if getattr(c, "checked", False)]  # honor prefill
        if kind == "checkbox" and "Include WITHOUT a layer" in message:
            captured["catchall"] = kwargs["choices"]
            return [c.value for c in kwargs["choices"] if getattr(c, "checked", False)]  # honor prefill
        if kind == "checkbox" and "layer" in message:
            return []
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
        if kind == "checkbox":
            return [c.value for c in kwargs.get("choices", [])]
        if kind == "confirm":
            return False
        return kwargs.get("default", "")

    monkeypatch.setattr(wizard_io, "questionary", RoutedQuestionary(router))
    config = wizard.run_setup(config_path)
    assert config is not None
    checked = {c.value: c.checked for c in captured["gold"] if c.value != "__manual__"}
    assert checked["mart"] is True  # saved in the rule -> pre-checked
    assert checked["not_scanned_yet"] is True  # saved but undiscovered -> still offered, checked
    assert checked["erp"] is False and checked["stg"] is False
    catchall_checked = {c.value: c.checked for c in captured["catchall"]}
    assert catchall_checked == {"erp": False, "stg": True}  # stg was included without a layer
    assert config.layers["gold"].schemas == ["mart", "not_scanned_yet"]  # round-trips untouched
    assert config.include_schemas == ["mart", "not_scanned_yet", "stg"]  # allowlist round-trips too


def test_ctrl_c_writes_nothing(tmp_path: Path, monkeypatch):
    make_repos(tmp_path)

    def router(kind, message, kwargs):
        if "Project name" in message:
            return None  # user hits Ctrl-C at the first prompt
        return ""

    monkeypatch.setattr(wizard_io, "questionary", RoutedQuestionary(router))
    config_path = tmp_path / "coop-data-doc.yml"
    with pytest.raises(KeyboardInterrupt):
        wizard.run_setup(config_path)
    assert not config_path.exists()
