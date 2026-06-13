import shutil
from pathlib import Path

from click.testing import CliRunner

from coop_data_doc.cli import cli

FIXTURES = Path(__file__).parent / "fixtures"


def setup_workspace(tmp_path: Path) -> Path:
    """Copy fixture repos and write a config pointing at them."""
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
  - schema: salespm
    model: SalesPM
output:
  dir: ./data-docs
  site_dir: ./site
""",
        encoding="utf-8",
    )
    return config


def run(args, cwd: Path):
    runner = CliRunner()
    import os

    old = os.getcwd()
    os.chdir(cwd)
    try:
        return runner.invoke(cli, args, obj={}, catch_exceptions=False)
    finally:
        os.chdir(old)


def test_init_and_refuse_overwrite(tmp_path: Path):
    result = run(["init"], tmp_path)
    assert result.exit_code == 0
    assert (tmp_path / "coop-data-doc.yml").is_file()
    result = run(["init"], tmp_path)
    assert result.exit_code != 0
    assert "already exists" in result.output


def test_scan_non_interactive(tmp_path: Path):
    setup_workspace(tmp_path)
    result = run(["scan", "--non-interactive"], tmp_path)
    assert result.exit_code == 0
    assert (tmp_path / "data-docs" / "graph.json").is_file()


def test_scan_strict_fails_on_fixture_warnings(tmp_path: Path):
    setup_workspace(tmp_path)
    # fixtures deliberately contain dynamic SQL and an unresolved partition
    result = run(["scan", "--non-interactive", "--strict"], tmp_path)
    assert result.exit_code == 2


def test_build_skip_html(tmp_path: Path):
    setup_workspace(tmp_path)
    result = run(["build", "--non-interactive", "--skip-html"], tmp_path)
    assert result.exit_code == 0
    docs = tmp_path / "data-docs"
    assert (docs / "index.md").is_file()
    assert (docs / "manifest.json").is_file()
    assert (docs / "view" / "salespm-dim_customer.md").is_file()
    assert (docs / "stored_proc" / "dbo-usp_load_fact_sales.md").is_file()


def test_check_passes_then_detects_staleness(tmp_path: Path):
    setup_workspace(tmp_path)
    assert run(["build", "--non-interactive", "--skip-html"], tmp_path).exit_code == 0

    # check exits 2 on fixture strict failures, so relax: remove the
    # dynamic/dynamic-source fixtures and rebuild for a clean baseline
    (tmp_path / "sql-repo" / "procs" / "usp_dynamic_refresh.sql").unlink()
    (tmp_path / "sql-repo" / "procs" / "usp_cursor_legacy.sql").unlink()
    (tmp_path / "pbi-repo" / "SalesPM.SemanticModel" / "definition" / "tables" / "ext_unresolved.tmdl").unlink()
    # the committed cache answers the one genuinely ambiguous mapping,
    # exactly as a real interactive session would have
    (tmp_path / ".lineage-cache.json").write_text(
        '{\n  "version": 1,\n  "mappings": {\n'
        '    "pbi_table:salespm.fact_sales": {\n'
        '      "target": "gold_table:dbo.fact_sales",\n'
        '      "method": "interactive"\n    }\n  }\n}\n',
        encoding="utf-8",
    )
    shutil.rmtree(tmp_path / "data-docs")
    assert run(["build", "--non-interactive", "--skip-html"], tmp_path).exit_code == 0

    result = run(["check"], tmp_path)
    assert result.exit_code == 0, result.output

    # human edits an intent block -> still up to date (preserved, not stale)
    page = tmp_path / "data-docs" / "view" / "salespm-dim_customer.md"
    page.write_text(
        page.read_text(encoding="utf-8").replace(
            "_Add a short description of what this object is for and who relies on it._",
            "The canonical customer dimension.",
        ),
        encoding="utf-8",
    )
    result = run(["check"], tmp_path)
    assert result.exit_code == 0, result.output

    # a structural hand-edit outside the intent block IS stale
    page.write_text(
        page.read_text(encoding="utf-8").replace("## Lineage", "## Lineage (edited)"),
        encoding="utf-8",
    )
    result = run(["check"], tmp_path)
    assert result.exit_code == 1
    assert "stale" in result.output


def test_version():
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"], obj={})
    assert result.exit_code == 0
    assert "coop-data-doc" in result.output


def test_bare_invocation_without_tty_prints_help():
    runner = CliRunner()
    result = runner.invoke(cli, [], obj={})
    assert result.exit_code == 0
    assert "Commands:" in result.output
    assert "setup" in result.output and "build" in result.output


def test_help_command_group_and_subcommand():
    runner = CliRunner()
    result = runner.invoke(cli, ["help"], obj={})
    assert result.exit_code == 0
    assert "Commands:" in result.output
    result = runner.invoke(cli, ["help", "build"], obj={})
    assert result.exit_code == 0
    assert "--skip-html" in result.output
    result = runner.invoke(cli, ["help", "nonsense"], obj={})
    assert result.exit_code == 2
    assert "unknown command" in result.output


def test_update_is_build_alias(tmp_path: Path):
    setup_workspace(tmp_path)
    result = run(["update", "--non-interactive", "--skip-html"], tmp_path)
    assert result.exit_code == 0
    assert (tmp_path / "data-docs" / "index.md").is_file()
    assert (tmp_path / "data-docs" / "manifest.json").is_file()


def test_interactive_menu_no_config_offers_init(tmp_path: Path, monkeypatch):
    from coop_data_doc import cli as cli_module

    class FakeQuestionary:
        class Choice:
            def __init__(self, title, value):
                self.title = title
                self.value = value

        @staticmethod
        def select(message, choices):
            assert "No coop-data-doc.yml" in message

            class _Result:
                @staticmethod
                def unsafe_ask():
                    return "init"

            return _Result()

    monkeypatch.setattr(cli_module, "questionary", FakeQuestionary)
    monkeypatch.setattr(cli_module, "_stdio_is_interactive", lambda: True)
    result = run([], tmp_path)
    assert result.exit_code == 0
    assert (tmp_path / "coop-data-doc.yml").is_file()


def test_interactive_menu_exit(tmp_path: Path, monkeypatch):
    from coop_data_doc import cli as cli_module

    setup_workspace(tmp_path)

    class FakeQuestionary:
        class Choice:
            def __init__(self, title, value):
                self.title = title
                self.value = value

        @staticmethod
        def select(message, choices):
            assert "Found coop-data-doc.yml" in message
            values = [c.value for c in choices]
            assert values == ["update", "scan", "setup", "check", "upgrade", "exit"]

            class _Result:
                @staticmethod
                def unsafe_ask():
                    return "exit"

            return _Result()

    monkeypatch.setattr(cli_module, "questionary", FakeQuestionary)
    monkeypatch.setattr(cli_module, "_stdio_is_interactive", lambda: True)
    result = run([], tmp_path)
    assert result.exit_code == 0


def test_init_creates_missing_parent_dirs(tmp_path: Path):
    result = run(["init", "nested/dir/conf.yml"], tmp_path)
    assert result.exit_code == 0, result.output
    assert (tmp_path / "nested" / "dir" / "conf.yml").is_file()


def test_check_fails_on_orphaned_committed_page(tmp_path: Path):
    setup_workspace(tmp_path)
    (tmp_path / "sql-repo" / "procs" / "usp_dynamic_refresh.sql").unlink()
    (tmp_path / "sql-repo" / "procs" / "usp_cursor_legacy.sql").unlink()
    (tmp_path / "pbi-repo" / "SalesPM.SemanticModel" / "definition" / "tables" / "ext_unresolved.tmdl").unlink()
    (tmp_path / ".lineage-cache.json").write_text(
        '{\n  "version": 1,\n  "mappings": {\n'
        '    "pbi_table:salespm.fact_sales": {\n'
        '      "target": "gold_table:dbo.fact_sales",\n'
        '      "method": "interactive"\n    }\n  }\n}\n',
        encoding="utf-8",
    )
    assert run(["build", "--non-interactive", "--skip-html"], tmp_path).exit_code == 0
    assert run(["check"], tmp_path).exit_code == 0

    # an orphaned page for an object that no longer exists must be caught
    orphan = tmp_path / "data-docs" / "view" / "dbo-v_ghost.md"
    orphan.write_text("---\nid: view:dbo.v_ghost\n---\n", encoding="utf-8")
    result = run(["check"], tmp_path)
    assert result.exit_code == 1
    assert "v_ghost" in result.output

    # rebuilding prunes it, after which check passes again
    assert run(["build", "--non-interactive", "--skip-html"], tmp_path).exit_code == 0
    assert not orphan.exists()
    assert run(["check"], tmp_path).exit_code == 0


def test_check_lenient_tolerates_risky_parses(tmp_path: Path):
    setup_workspace(tmp_path)
    (tmp_path / ".lineage-cache.json").write_text(
        '{\n  "version": 1,\n  "mappings": {\n'
        '    "pbi_table:salespm.fact_sales": {\n'
        '      "target": "gold_table:dbo.fact_sales",\n'
        '      "method": "interactive"\n    },\n'
        '    "pbi_table:salespm.ext_unresolved": {\n'
        '      "target": null,\n'
        '      "method": "external"\n    }\n  }\n}\n',
        encoding="utf-8",
    )
    assert run(["build", "--non-interactive", "--skip-html"], tmp_path).exit_code == 0
    # fixtures still contain dynamic SQL + cursor fallback -> strict check fails
    assert run(["check"], tmp_path).exit_code == 2
    # lenient tolerates those parse warnings; docs are fresh -> passes
    result = run(["check", "--lenient"], tmp_path)
    assert result.exit_code == 0, result.output
