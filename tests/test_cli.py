import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from coop_data_doc import __version__
from coop_data_doc.cli import cli
from coop_data_doc.render.paths import slug

FIXTURES = Path(__file__).parent / "fixtures"


def doc_page(docs: Path, node_id: str) -> Path:
    """A node's generated page path (slug includes a hash suffix)."""
    return docs / node_id.split(":", 1)[0] / f"{slug(node_id)}.md"


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
    include: ["**/*.tmdl", "**/*.bim", "**/report.json", "**/visual.json", "**/page.json", "**/definition.pbir", "**/*.pbix"]
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


def run(args, cwd: Path, input: str | None = None):
    runner = CliRunner()
    import os

    old = os.getcwd()
    os.chdir(cwd)
    try:
        return runner.invoke(cli, args, input=input, obj={}, catch_exceptions=False)
    finally:
        os.chdir(old)


def test_init_and_refuse_overwrite(tmp_path: Path):
    result = run(["init"], tmp_path)
    assert result.exit_code == 0
    assert (tmp_path / "coop-data-doc.yml").is_file()
    result = run(["init"], tmp_path)
    assert result.exit_code != 0
    assert "already exists" in result.output


def test_init_without_path_targets_authoritative_env_candidate(tmp_path: Path, monkeypatch):
    local = setup_workspace(tmp_path)
    selected = tmp_path / "created" / "selected.yml"
    monkeypatch.setenv("COOP_DATA_DOC_CONFIG", str(selected))

    result = run(["init"], tmp_path)

    assert result.exit_code == 0, result.output
    assert selected.is_file()
    assert local.read_text(encoding="utf-8").startswith("project_name: Test Estate")


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
    # issue #32: an untraceable partition source is an unresolved item the
    # portal lists — it must reach the strict gate, not just the site
    assert "unresolved_partition_source" in result.output


def _two_model_ambiguous_workspace(tmp_path: Path) -> None:
    """A workspace whose only problem is a fully ambiguous visual binding:
    two models share the entity name and nothing disambiguates the report."""
    pbi = tmp_path / "pbi-repo"
    for model in ("Alpha", "Beta"):
        tables = pbi / f"{model}.SemanticModel" / "definition" / "tables"
        tables.mkdir(parents=True)
        (tables / "date.tmdl").write_text(
            "table date\n\tcolumn year\n\t\tdataType: int64\n", encoding="utf-8"
        )
    report = pbi / "LegacyDash"
    report.mkdir()
    (report / "report.json").write_text(
        '{"sections": [{"name": "s1", "displayName": "Main", "visualContainers": [{'
        '"config": "{\\"name\\":\\"v1\\",\\"singleVisual\\":{\\"visualType\\":\\"columnChart\\",'
        '\\"projections\\":{\\"Y\\":[{\\"queryRef\\":\\"date.year\\"}]}}}"}]}]}',
        encoding="utf-8",
    )
    (tmp_path / "coop-data-doc.yml").write_text(
        """\
project_name: Ambiguous Estate
repos:
  powerbi:
    path: ./pbi-repo
    include: ["**/*.tmdl", "**/report.json"]
output:
  dir: ./data-docs
  site_dir: ./site
""",
        encoding="utf-8",
    )


def test_ambiguous_visual_binding_fails_strict_gate(tmp_path: Path):
    # issue #32: a visual binding no context can disambiguate is pending on the
    # report node and shown by the portal — strict/check must fail on it too,
    # while --lenient keeps tolerating it as a known-and-accepted warning.
    _two_model_ambiguous_workspace(tmp_path)
    result = run(["build", "--non-interactive", "--strict", "--skip-html"], tmp_path)
    assert result.exit_code == 2, result.output
    assert "ambiguous_visual_binding" in result.output

    # a non-strict build succeeds and writes docs; default check then fails on
    # the same category, and --lenient tolerates it
    assert run(["build", "--non-interactive", "--skip-html"], tmp_path).exit_code == 0
    chk = run(["check"], tmp_path)
    assert chk.exit_code == 2, chk.output
    assert "ambiguous_visual_binding" in chk.output
    assert run(["check", "--lenient"], tmp_path).exit_code == 0


def _unmatched_entity_workspace(tmp_path: Path) -> None:
    """A workspace whose only diagnostic is unmatched_visual_entity: one model,
    one table, and a report visual binding a 'ghost' entity no table matches."""
    pbi = tmp_path / "pbi-repo"
    tables = pbi / "Alpha.SemanticModel" / "definition" / "tables"
    tables.mkdir(parents=True)
    (tables / "date.tmdl").write_text("table date\n\tcolumn year\n\t\tdataType: int64\n", encoding="utf-8")
    report = pbi / "GhostDash"
    report.mkdir()
    (report / "report.json").write_text(
        '{"sections": [{"name": "s1", "displayName": "Main", "visualContainers": [{'
        '"config": "{\\"name\\":\\"v1\\",\\"singleVisual\\":{\\"visualType\\":\\"columnChart\\",'
        '\\"projections\\":{\\"Y\\":[{\\"queryRef\\":\\"ghost.value\\"}]}}}"}]}]}',
        encoding="utf-8",
    )
    (tmp_path / "coop-data-doc.yml").write_text(
        """\
project_name: Ghost Estate
repos:
  powerbi:
    path: ./pbi-repo
    include: ["**/*.tmdl", "**/report.json"]
output:
  dir: ./data-docs
  site_dir: ./site
""",
        encoding="utf-8",
    )


def test_unmatched_visual_entity_is_tolerated_by_strict_gate(tmp_path: Path):
    # issue #45: an entity that matches NO documented table is a heads-up, never
    # a missing edge to a known object ("never guess lineage") — so unlike
    # ambiguous_visual_binding it does NOT gate: build --strict AND check pass.
    _unmatched_entity_workspace(tmp_path)
    strict = run(["build", "--non-interactive", "--strict", "--skip-html"], tmp_path)
    assert strict.exit_code == 0, strict.output
    assert "unmatched_visual_entity" in strict.output  # still reported, just not fatal
    chk = run(["check"], tmp_path)
    assert chk.exit_code == 0, chk.output


def test_build_skip_html(tmp_path: Path):
    setup_workspace(tmp_path)
    result = run(["build", "--non-interactive", "--skip-html"], tmp_path)
    assert result.exit_code == 0
    docs = tmp_path / "data-docs"
    assert (docs / "index.md").is_file()
    assert (docs / "manifest.json").is_file()
    assert doc_page(docs, "view:sales.dim_customer").is_file()
    assert doc_page(docs, "stored_proc:dbo.usp_load_fact_sales").is_file()


def test_check_passes_then_detects_staleness(tmp_path: Path):
    setup_workspace(tmp_path)
    assert run(["build", "--non-interactive", "--skip-html"], tmp_path).exit_code == 0

    # check exits 2 on fixture strict failures, so relax: remove the
    # dynamic/dynamic-source fixtures and rebuild for a clean baseline
    (tmp_path / "sql-repo" / "procs" / "usp_dynamic_refresh.sql").unlink()
    (tmp_path / "sql-repo" / "procs" / "usp_cursor_legacy.sql").unlink()
    (tmp_path / "pbi-repo" / "Sales.SemanticModel" / "definition" / "tables" / "ext_unresolved.tmdl").unlink()
    # the committed cache answers the one genuinely ambiguous mapping,
    # exactly as a real interactive session would have
    (tmp_path / ".lineage-cache.json").write_text(
        '{\n  "version": 1,\n  "mappings": {\n'
        '    "pbi_table:sales.fact_sales": {\n'
        '      "target": "gold_table:dbo.fact_sales",\n'
        '      "method": "interactive"\n    }\n  }\n}\n',
        encoding="utf-8",
    )
    shutil.rmtree(tmp_path / "data-docs")
    assert run(["build", "--non-interactive", "--skip-html"], tmp_path).exit_code == 0

    result = run(["check"], tmp_path)
    assert result.exit_code == 0, result.output

    # human edits an intent block -> still up to date (preserved, not stale)
    page = doc_page(tmp_path / "data-docs", "view:sales.dim_customer")
    page.write_text(  # newline="\n": mimic an editor that preserves LF
        page.read_text(encoding="utf-8").replace(
            "_Add a short description of what this object is for and who relies on it._",
            "The canonical customer dimension.",
        ),
        encoding="utf-8",
        newline="\n",
    )
    result = run(["check"], tmp_path)
    assert result.exit_code == 0, result.output

    # a structural hand-edit outside the intent block IS stale
    page.write_text(  # newline="\n": mimic an editor that preserves LF
        page.read_text(encoding="utf-8").replace("## Lineage", "## Lineage (edited)"),
        encoding="utf-8",
        newline="\n",
    )
    result = run(["check"], tmp_path)
    assert result.exit_code == 1
    assert "stale" in result.output


def seed_stale_cache(tmp_path: Path) -> bytes:
    """Write a .lineage-cache.json holding a committed human answer whose target
    isn't in the current graph (e.g. answered on another branch or a wider
    scope); return the file bytes for byte-identity assertions."""
    cache = tmp_path / ".lineage-cache.json"
    cache.write_text(
        '{\n  "version": 1,\n  "mappings": {\n'
        '    "pbi_table:sales.answered_elsewhere": {\n'
        '      "target": "view:sales.only_on_other_branch",\n'
        '      "method": "interactive"\n    }\n  }\n}\n',
        encoding="utf-8",
        newline="\n",
    )
    return cache.read_bytes()


def test_check_leaves_lineage_cache_untouched(tmp_path: Path):
    # CI `check` must never mutate the working tree: a committed answer whose
    # target isn't in this build is ignored for the run, not deleted.
    setup_workspace(tmp_path)
    assert run(["build", "--non-interactive", "--skip-html"], tmp_path).exit_code == 0
    before = seed_stale_cache(tmp_path)
    run(["check", "--lenient"], tmp_path)  # exit code irrelevant: fixtures have warnings
    assert (tmp_path / ".lineage-cache.json").read_bytes() == before


def test_status_leaves_lineage_cache_untouched(tmp_path: Path):
    setup_workspace(tmp_path)
    before = seed_stale_cache(tmp_path)
    result = run(["status"], tmp_path)
    assert result.exit_code == 0, result.output
    assert (tmp_path / ".lineage-cache.json").read_bytes() == before


def test_build_prunes_stale_lineage_cache_entries(tmp_path: Path):
    # an explicit successful build is the ONE place stale answers are deleted
    setup_workspace(tmp_path)
    seed_stale_cache(tmp_path)
    assert run(["build", "--non-interactive", "--skip-html"], tmp_path).exit_code == 0
    import json as _json

    mappings = _json.loads((tmp_path / ".lineage-cache.json").read_text(encoding="utf-8"))["mappings"]
    assert "pbi_table:sales.answered_elsewhere" not in mappings


def test_version():
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"], obj={})
    assert result.exit_code == 0
    assert "coop-data-doc" in result.output


def test_python_m_invocation_works():
    # `python -m coop_data_doc` is the documented "command not found" fallback —
    # it must run the same CLI without depending on the console script / PATH.
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "coop_data_doc", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "coop-data-doc" in result.stdout


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
    # the verb-disambiguation notice (update means self-update in the sibling
    # review tools) prints on every non-quiet update run
    assert "alias of build" in result.output


def test_update_notice_suppressed_by_quiet_and_absent_from_build(tmp_path: Path):
    setup_workspace(tmp_path)
    quiet = run(["-q", "update", "--non-interactive", "--skip-html"], tmp_path)
    assert quiet.exit_code == 0
    assert "alias of build" not in quiet.output
    build = run(["build", "--non-interactive", "--skip-html"], tmp_path)
    assert build.exit_code == 0
    assert "alias of build" not in build.output


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


def test_interactive_menu_names_invalid_authoritative_env_target(tmp_path: Path, monkeypatch):
    from coop_data_doc import cli as cli_module

    selected = tmp_path / "configured" / "missing.yml"
    monkeypatch.setenv("COOP_DATA_DOC_CONFIG", str(selected))

    class FakeQuestionary:
        class Choice:
            def __init__(self, title, value):
                self.title = title
                self.value = value

        @staticmethod
        def select(message, choices):
            assert str(selected) in message
            assert "not a config file" in message

            class _Result:
                @staticmethod
                def unsafe_ask():
                    return "exit"

            return _Result()

    monkeypatch.setattr(cli_module, "questionary", FakeQuestionary)
    monkeypatch.setattr(cli_module, "_stdio_is_interactive", lambda: True)
    result = run([], tmp_path)
    assert result.exit_code == 0, result.output


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
            assert values == ["update", "status", "scan", "map", "setup", "check", "upgrade", "exit"]

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
    (tmp_path / "pbi-repo" / "Sales.SemanticModel" / "definition" / "tables" / "ext_unresolved.tmdl").unlink()
    (tmp_path / ".lineage-cache.json").write_text(
        '{\n  "version": 1,\n  "mappings": {\n'
        '    "pbi_table:sales.fact_sales": {\n'
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
        '    "pbi_table:sales.fact_sales": {\n'
        '      "target": "gold_table:dbo.fact_sales",\n'
        '      "method": "interactive"\n    },\n'
        '    "pbi_table:sales.ext_unresolved": {\n'
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


# --- Tests for config discovery and status command ---


def test_config_find_in_cwd(tmp_path: Path):
    """Config.find() locates config in current directory."""
    from coop_data_doc.config import DEFAULT_CONFIG, Config

    config = tmp_path / DEFAULT_CONFIG
    config.write_text("project_name: Test\nrepos:\n  sql:\n    path: ./sql\n  powerbi:\n    path: ./pbi\n")
    found = Config.find(start_dir=tmp_path)
    assert found is not None
    assert found.name == DEFAULT_CONFIG
    assert found.parent == tmp_path


def test_config_find_walks_up_parents(tmp_path: Path):
    """Config.find() walks up parent directories to find config."""
    from coop_data_doc.config import DEFAULT_CONFIG, Config

    config = tmp_path / DEFAULT_CONFIG
    config.write_text("project_name: Test\nrepos:\n  sql:\n    path: ./sql\n  powerbi:\n    path: ./pbi\n")
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    found = Config.find(start_dir=nested)
    assert found is not None
    assert found.parent == tmp_path


def test_config_find_prefers_env_var(tmp_path: Path, monkeypatch):
    """COOP_DATA_DOC_CONFIG environment variable overrides discovery."""
    from coop_data_doc.config import Config

    env_config = tmp_path / "custom-config.yml"
    env_config.write_text(
        "project_name: EnvTest\nrepos:\n  sql:\n    path: ./sql\n  powerbi:\n    path: ./pbi\n"
    )
    monkeypatch.setenv("COOP_DATA_DOC_CONFIG", str(env_config))
    found = Config.find(start_dir=tmp_path)
    assert found == env_config


def test_config_find_env_missing_is_authoritative(tmp_path: Path, monkeypatch):
    """A missing environment target is returned as-is, never replaced by discovery."""
    from coop_data_doc.config import Config

    local = tmp_path / "coop-data-doc.yml"
    local.write_text("project_name: fallback\nrepos: {}\n", encoding="utf-8")
    missing = tmp_path / "missing.yml"
    monkeypatch.setenv("COOP_DATA_DOC_CONFIG", str(missing))
    assert Config.find(start_dir=tmp_path) == missing.resolve()


def test_config_find_empty_env_allows_normal_discovery(tmp_path: Path, monkeypatch):
    from coop_data_doc.config import Config

    config = tmp_path / "coop-data-doc.yml"
    config.write_text("project_name: Test\nrepos: {}\n", encoding="utf-8")
    for value in (None, ""):
        if value is None:
            monkeypatch.delenv("COOP_DATA_DOC_CONFIG", raising=False)
        else:
            monkeypatch.setenv("COOP_DATA_DOC_CONFIG", value)
        assert Config.find(start_dir=tmp_path) == config


def test_config_find_relative_env_resolves_from_cwd(tmp_path: Path, monkeypatch):
    from coop_data_doc.config import Config

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COOP_DATA_DOC_CONFIG", "nested/custom.yml")
    assert Config.find() == (tmp_path / "nested/custom.yml").resolve()


def test_unresolvable_env_path_is_a_friendly_error(tmp_path: Path, monkeypatch):
    selected = "~__coop_data_doc_missing_user__/selected.yml"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COOP_DATA_DOC_CONFIG", selected)

    result = run(["status"], tmp_path)

    assert result.exit_code == 1
    assert selected in result.output
    assert "Traceback" not in result.output


def test_resolve_path_rejects_a_synthetic_missing_named_home(tmp_path: Path, monkeypatch):
    """Windows-style expanduser guesses must not make unknown users resolvable."""
    from coop_data_doc.config import Config, ConfigError

    original_expanduser = Path.expanduser
    missing_home = tmp_path / "Users" / "__coop_data_doc_missing_user__"

    def windows_style_expanduser(path: Path) -> Path:
        raw = str(path)
        if raw == "~__coop_data_doc_missing_user__":
            return missing_home
        if raw.startswith("~__coop_data_doc_missing_user__/"):
            return missing_home / raw.split("/", 1)[1]
        return original_expanduser(path)

    monkeypatch.setattr(Path, "expanduser", windows_style_expanduser)

    with pytest.raises(ConfigError, match="~__coop_data_doc_missing_user__"):
        Config.resolve_path("~__coop_data_doc_missing_user__/selected.yml")


def test_status_shows_version(tmp_path: Path):
    """status prints the installed version (offline) with an upgrade hint."""
    result = run(["status"], tmp_path)  # no config -> exit 1, but version prints first
    assert __version__ in result.output
    assert "upgrade" in result.output


def test_status_no_config(tmp_path: Path):
    """status exits 1 with friendly message when no config found."""
    result = run(["status"], tmp_path)
    assert result.exit_code == 1
    assert "no config found" in result.output
    assert "coop-data-doc init" in result.output
    assert "coop-data-doc setup" in result.output


def test_authoritative_env_missing_rejects_all_read_commands(tmp_path: Path, monkeypatch):
    """Read commands fail on the selected env path instead of using local config."""
    setup_workspace(tmp_path)
    selected = tmp_path / "missing" / "selected.yml"
    baseline = tmp_path / "baseline.json"
    baseline.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("COOP_DATA_DOC_CONFIG", str(selected))
    for args in (
        ["build", "--non-interactive", "--skip-html"],
        ["scan", "--non-interactive"],
        ["check"],
        ["impact", "--baseline", str(baseline)],
    ):
        result = run(args, tmp_path)
        assert result.exit_code == 1, (args, result.output)
        assert str(selected) in result.output
        assert "Traceback" not in result.output
    # The valid local config was never used to create output.
    assert not (tmp_path / "data-docs").exists()


def test_authoritative_env_missing_status_and_parent_do_not_fallback(tmp_path: Path, monkeypatch):
    """A missing env path wins over both a local and an ancestor config."""
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    (parent / "coop-data-doc.yml").write_text("project_name: parent\nrepos: {}\n", encoding="utf-8")
    (child / "coop-data-doc.yml").write_text("project_name: local\nrepos: {}\n", encoding="utf-8")
    selected = parent / "selected.yml"
    monkeypatch.setenv("COOP_DATA_DOC_CONFIG", str(selected))
    result = run(["status"], child)
    assert result.exit_code == 1
    assert f"config not found at {selected}" in result.output
    assert "config:" not in result.output


def test_authoritative_env_directory_rejects_all_read_commands(tmp_path: Path, monkeypatch):
    setup_workspace(tmp_path)
    selected = tmp_path / "selected-dir"
    selected.mkdir()
    baseline = tmp_path / "baseline.json"
    baseline.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("COOP_DATA_DOC_CONFIG", str(selected))

    for args in (
        ["build", "--non-interactive", "--skip-html"],
        ["scan", "--non-interactive"],
        ["check"],
        ["impact", "--baseline", str(baseline)],
        ["status"],
    ):
        result = run(args, tmp_path)
        assert result.exit_code == 1, (args, result.output)
        assert str(selected) in result.output
        assert "Traceback" not in result.output
        if args == ["status"]:
            assert "directory" in result.output


def test_valid_env_config_wins_over_local_and_parent(tmp_path: Path, monkeypatch):
    local = setup_workspace(tmp_path)
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "coop-data-doc.yml").write_text("project_name: Parent Estate\nrepos: {}\n", encoding="utf-8")
    selected = tmp_path / "config" / "selected.yml"
    selected.parent.mkdir()
    selected.write_text(
        local.read_text(encoding="utf-8")
        .replace("Test Estate", "Environment Estate")
        .replace("./sql-repo", "../sql-repo")
        .replace("./pbi-repo", "../pbi-repo"),
        encoding="utf-8",
    )
    monkeypatch.setenv("COOP_DATA_DOC_CONFIG", str(selected))
    result = run(["show-config"], tmp_path)
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["project_name"] == "Environment Estate"
    assert data["path"] == str(selected)


def test_explicit_config_path_beats_environment_override(tmp_path: Path, monkeypatch):
    selected = tmp_path / "selected.yml"
    explicit = tmp_path / "nested" / "explicit.yml"
    selected.write_text("project_name: Environment Estate\nrepos: {}\n", encoding="utf-8")
    explicit.parent.mkdir()
    explicit.write_text("project_name: Explicit Estate\nrepos: {}\n", encoding="utf-8")
    monkeypatch.setenv("COOP_DATA_DOC_CONFIG", str(selected))

    result = run(["show-config", "--config", "nested/explicit.yml"], tmp_path)

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["project_name"] == "Explicit Estate"
    assert data["path"] == str(explicit.resolve())


def test_setup_without_path_targets_authoritative_env_candidate(tmp_path: Path, monkeypatch):
    from coop_data_doc import wizard as wizard_module

    setup_workspace(tmp_path)
    selected = tmp_path / "setup" / "selected.yml"
    monkeypatch.setenv("COOP_DATA_DOC_CONFIG", str(selected))
    captured = {}

    def fake_run_setup(path, io=None):
        captured["path"] = Path(path)

    monkeypatch.setattr(wizard_module, "run_setup", fake_run_setup)
    result = run(["setup", "--transport", "jsonl"], tmp_path, input="")
    assert result.exit_code == 0, result.output
    assert captured["path"] == selected.resolve()
    assert not selected.exists()
    assert "project_name: Test Estate" in (tmp_path / "coop-data-doc.yml").read_text(encoding="utf-8")


def test_setup_explicit_unresolvable_path_is_friendly(tmp_path: Path):
    selected = "~__coop_data_doc_missing_user__/selected.yml"

    result = run(["setup", selected], tmp_path)

    assert result.exit_code == 1
    assert selected in result.output
    assert "Traceback" not in result.output


def test_show_config_reports_missing_authoritative_env_path(tmp_path: Path, monkeypatch):
    setup_workspace(tmp_path)
    selected = tmp_path / "new" / "selected.yml"
    monkeypatch.setenv("COOP_DATA_DOC_CONFIG", str(selected))
    result = run(["show-config"], tmp_path)
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["exists"] is False
    assert data["path"] == str(selected)
    assert data["project_name"] == "Data Estate"  # no local fallback


def test_show_config_rejects_authoritative_directory(tmp_path: Path, monkeypatch):
    setup_workspace(tmp_path)
    selected = tmp_path / "selected-dir"
    selected.mkdir()
    monkeypatch.setenv("COOP_DATA_DOC_CONFIG", str(selected))

    result = run(["show-config"], tmp_path)

    assert result.exit_code == 1
    assert str(selected) in result.output
    assert "Data Estate" not in result.output
    assert "Traceback" not in result.output


def test_config_set_creates_missing_authoritative_env_path(tmp_path: Path, monkeypatch):
    from coop_data_doc.config import Config

    local = setup_workspace(tmp_path)
    selected = tmp_path / "created" / "selected.yml"
    monkeypatch.setenv("COOP_DATA_DOC_CONFIG", str(selected))
    result = run(["config-set"], tmp_path, input=json.dumps({"project_name": "Created"}))
    assert result.exit_code == 0, result.output
    assert selected.is_file()
    assert Config.load(selected).project_name == "Created"
    assert local.read_text(encoding="utf-8").startswith("project_name: Test Estate")


def test_authoritative_env_directory_fails_cleanly_for_authoring(tmp_path: Path, monkeypatch):
    setup_workspace(tmp_path)
    selected = tmp_path / "selected-dir"
    selected.mkdir()
    monkeypatch.setenv("COOP_DATA_DOC_CONFIG", str(selected))
    for args in (
        ["config-set"],
        ["set-folders", "--repo", "sql", "--include", "procs"],
    ):
        result = run(args, tmp_path, input="{}" if args[0] == "config-set" else None)
        assert result.exit_code == 1
        assert str(selected) in result.output
        assert "Traceback" not in result.output


def test_authoritative_env_missing_rejects_set_folders(tmp_path: Path, monkeypatch):
    setup_workspace(tmp_path)
    selected = tmp_path / "missing" / "selected.yml"
    monkeypatch.setenv("COOP_DATA_DOC_CONFIG", str(selected))

    result = run(["set-folders", "--repo", "sql", "--include", "procs"], tmp_path)

    assert result.exit_code == 1
    assert str(selected) in result.output
    assert "Traceback" not in result.output


def test_setup_directory_error_is_friendly(tmp_path: Path, monkeypatch):
    from coop_data_doc import wizard as wizard_module
    from coop_data_doc.config import ConfigError

    selected = tmp_path / "selected-dir"
    selected.mkdir()
    monkeypatch.setenv("COOP_DATA_DOC_CONFIG", str(selected))

    def fail_setup(path, io=None):
        raise ConfigError(f"Config path is a directory: {path}")

    monkeypatch.setattr(wizard_module, "run_setup", fail_setup)
    result = run(["setup"], tmp_path)

    assert result.exit_code == 1
    assert str(selected) in result.output
    assert "Traceback" not in result.output


def test_status_invalid_authoritative_env_names_path(tmp_path: Path, monkeypatch):
    setup_workspace(tmp_path)
    selected = tmp_path / "invalid.yml"
    selected.write_text("not: [valid", encoding="utf-8")
    monkeypatch.setenv("COOP_DATA_DOC_CONFIG", str(selected))
    result = run(["status"], tmp_path)
    assert result.exit_code == 1
    assert str(selected) in result.output
    assert "Traceback" not in result.output


def test_status_unreadable_authoritative_env_names_path(tmp_path: Path, monkeypatch):
    setup_workspace(tmp_path)
    selected = tmp_path / "unreadable.yml"
    selected.write_text("project_name: selected\nrepos: {}\n", encoding="utf-8")
    monkeypatch.setenv("COOP_DATA_DOC_CONFIG", str(selected))
    original_read_text = Path.read_text

    def deny_selected(path, *args, **kwargs):
        if path == selected:
            raise PermissionError("permission denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_selected)
    result = run(["status"], tmp_path)

    assert result.exit_code == 1
    assert str(selected) in result.output
    assert "Traceback" not in result.output


def test_status_with_valid_config(tmp_path: Path):
    """status shows project state when config is valid."""
    setup_workspace(tmp_path)
    # Build first so docs exist
    run(["build", "--non-interactive", "--skip-html"], tmp_path)
    result = run(["status"], tmp_path)
    assert result.exit_code == 0
    assert "config:" in result.output
    assert "data-docs" in result.output
    assert "freshness:" in result.output


def test_build_graph_is_reusable_across_resolutions(tmp_path: Path):
    """build_graph parses once; resolve_graph re-links on a copy without
    re-parsing. Two resolves of the same parsed graph must match run_pipeline,
    and the built graph must be left pristine (so the wizard can reuse it for
    its suggest + verify passes). This is what makes the wizard scan ~2x faster.
    """
    import copy as _copy

    from coop_data_doc.cli import build_graph, resolve_graph, run_pipeline
    from coop_data_doc.config import Config

    config_path = setup_workspace(tmp_path)
    config = Config.load(config_path)

    # baseline: the full pipeline in one shot
    _, whole, _ = run_pipeline(config, interactive=False)

    # split: parse once, then resolve twice on throwaway copies
    parsed, _ = build_graph(config)
    before_nodes, before_edges = len(parsed.nodes), len(parsed.edges)

    g1 = parsed.model_copy(deep=True)
    r1, _ = resolve_graph(g1, config, interactive=False)
    g2 = parsed.model_copy(deep=True)
    r2, _ = resolve_graph(g2, config, interactive=False)

    # the split reproduces the monolithic pipeline's resolution exactly
    assert (r1.resolved, r1.unresolved) == (whole.resolved, whole.unresolved)
    # re-linking is deterministic across copies
    assert (r1.resolved, r1.unresolved) == (r2.resolved, r2.unresolved)
    # resolve_graph mutates only its argument copy, never the parsed base graph
    assert (len(parsed.nodes), len(parsed.edges)) == (before_nodes, before_edges)
    # build_graph is mapping-independent: dropping the schema_mappings changes
    # only the resolution, not the parsed structure
    no_map = _copy.deepcopy(config)
    no_map.schema_mappings = []
    g3 = parsed.model_copy(deep=True)
    r3, _ = resolve_graph(g3, no_map, interactive=False)
    assert r3.resolved <= whole.resolved  # fewer/equal links without the mapping hint


def test_status_with_invalid_config(tmp_path: Path):
    """status exits 1 when config exists but is invalid."""
    config = tmp_path / "coop-data-doc.yml"
    config.write_text("invalid: yaml: [", encoding="utf-8")
    result = run(["status"], tmp_path)
    assert result.exit_code == 1
    assert "config exists but is invalid" in result.output


def test_scan_uses_config_discovery(tmp_path: Path):
    """scan without --config finds config in parent directory."""
    setup_workspace(tmp_path)
    nested = tmp_path / "sub" / "sub2"
    nested.mkdir(parents=True)
    result = run(["scan", "--non-interactive"], nested)
    assert result.exit_code == 0
    assert (tmp_path / "data-docs" / "graph.json").is_file()


def test_build_uses_config_discovery(tmp_path: Path):
    """build without --config finds config in parent directory."""
    setup_workspace(tmp_path)
    nested = tmp_path / "sub"
    nested.mkdir(parents=True)
    result = run(["build", "--non-interactive", "--skip-html"], nested)
    assert result.exit_code == 0
    assert (tmp_path / "data-docs" / "index.md").is_file()


def test_init_suggests_setup_on_existing_valid_config(tmp_path: Path):
    """init on existing valid config suggests setup instead of error."""
    setup_workspace(tmp_path)
    result = run(["init"], tmp_path)
    assert result.exit_code == 1
    assert "already exists and is valid" in result.output
    assert "coop-data-doc setup" in result.output


def test_init_force_overwrites_valid_config(tmp_path: Path):
    """init --force overwrites existing valid config."""
    setup_workspace(tmp_path)
    result = run(["init", "--force"], tmp_path)
    assert result.exit_code == 0
    assert "Wrote" in result.output


def test_config_set_malformed_patch_friendly_error(tmp_path: Path):
    """A valid-JSON-but-wrong-shape patch (e.g. {"output": null}) must produce a
    friendly one-line ClickException, not a leaked TypeError/AttributeError/
    KeyError traceback. catch_exceptions=False means a leaked non-Click exception
    would fail this test."""
    import os

    runner = CliRunner()
    old = os.getcwd()
    os.chdir(tmp_path)
    try:
        for patch in (
            '{"output": null}',
            '{"repos": []}',
            '{"layers": []}',
            '{"schema_mappings": [{"schema": "x"}]}',
        ):
            result = runner.invoke(
                cli, ["config-set", "--from-json", "-"], input=patch, obj={}, catch_exceptions=False
            )
            assert result.exit_code != 0
            assert "patch produced an invalid config" in result.output
    finally:
        os.chdir(old)


# a lineage cache that fully resolves the fixtures' ambiguous cross-repo refs,
# so interactive runs (interactive=True) never stop to prompt in tests
_FULL_CACHE = (
    '{\n  "version": 1,\n  "mappings": {\n'
    '    "pbi_table:sales.fact_sales": {\n'
    '      "target": "gold_table:dbo.fact_sales",\n'
    '      "method": "interactive"\n    },\n'
    '    "pbi_table:sales.ext_unresolved": {\n'
    '      "target": null,\n'
    '      "method": "external"\n    }\n  }\n}\n'
)


def test_status_honors_explicit_config(tmp_path: Path):
    """status --config <path> uses the given config, not discovery."""
    setup_workspace(tmp_path)
    # rename so nothing is discoverable — only the explicit --config can find it
    explicit = tmp_path / "custom.yml"
    (tmp_path / "coop-data-doc.yml").rename(explicit)
    result = run(["status", "--config", str(explicit)], tmp_path)
    assert result.exit_code == 0, result.output
    assert str(explicit) in result.output  # reported the explicit config
    assert "no config found" not in result.output


def test_status_explicit_config_missing(tmp_path: Path):
    """status --config to a nonexistent path fails clearly (no silent discovery)."""
    result = run(["status", "--config", str(tmp_path / "nope.yml")], tmp_path)
    assert result.exit_code == 1
    assert "config not found" in result.output


def test_interactive_menu_from_subdir_uses_discovered_config(tmp_path: Path, monkeypatch):
    """Running the menu from a SUBDIRECTORY must drive actions with the
    discovered (parent) config, not the bare default filename."""
    from coop_data_doc import cli as cli_module

    setup_workspace(tmp_path)
    (tmp_path / ".lineage-cache.json").write_text(_FULL_CACHE, encoding="utf-8")
    nested = tmp_path / "sub" / "deep"
    nested.mkdir(parents=True)

    class FakeQuestionary:
        class Choice:
            def __init__(self, title, value):
                self.title = title
                self.value = value

        @staticmethod
        def select(message, choices):
            assert "Found coop-data-doc.yml" in message  # only the menu prompts

            class _Result:
                @staticmethod
                def unsafe_ask():
                    return "scan"

            return _Result()

    monkeypatch.setattr(cli_module, "questionary", FakeQuestionary)
    monkeypatch.setattr(cli_module, "_stdio_is_interactive", lambda: True)
    result = run([], nested)
    assert result.exit_code == 0, result.output
    # scan ran against the discovered parent config -> output lands in the parent
    assert (tmp_path / "data-docs" / "graph.json").is_file()


def test_interactive_menu_setup_from_subdir_edits_discovered_config(tmp_path: Path, monkeypatch):
    """issue #12: 'Change settings' is only offered because a config WAS found, so it must
    edit THAT (parent) config — not write a new nested one in cwd that shadows it."""
    from coop_data_doc import cli as cli_module
    from coop_data_doc import wizard as wizard_module

    setup_workspace(tmp_path)
    nested = tmp_path / "sub" / "deep"
    nested.mkdir(parents=True)

    class FakeQuestionary:
        class Choice:
            def __init__(self, title, value):
                self.title = title
                self.value = value

        @staticmethod
        def select(message, choices):
            assert "Found coop-data-doc.yml" in message

            class _Result:
                @staticmethod
                def unsafe_ask():
                    return "setup"

            return _Result()

    captured = {}

    def fake_run_setup(path, io=None):
        captured["path"] = path

    monkeypatch.setattr(cli_module, "questionary", FakeQuestionary)
    monkeypatch.setattr(cli_module, "_stdio_is_interactive", lambda: True)
    monkeypatch.setattr(wizard_module, "run_setup", fake_run_setup)
    result = run([], nested)
    assert result.exit_code == 0, result.output
    assert captured["path"] == tmp_path / "coop-data-doc.yml"  # the discovered parent config
    assert not (nested / "coop-data-doc.yml").exists()  # no shadowing nested config


def test_interactive_menu_status_uses_discovered_config(tmp_path: Path, monkeypatch):
    # issue #34: the menu's "Show project status" entry runs status against the
    # DISCOVERED config (possibly in a parent dir), like the other entries.
    from coop_data_doc import cli as cli_module

    setup_workspace(tmp_path)
    nested = tmp_path / "sub"
    nested.mkdir()

    class FakeQuestionary:
        class Choice:
            def __init__(self, title, value):
                self.title = title
                self.value = value

        @staticmethod
        def select(message, choices):
            assert "Found coop-data-doc.yml" in message
            assert "status" in [c.value for c in choices]

            class _Result:
                @staticmethod
                def unsafe_ask():
                    return "status"

            return _Result()

    monkeypatch.setattr(cli_module, "questionary", FakeQuestionary)
    monkeypatch.setattr(cli_module, "_stdio_is_interactive", lambda: True)
    result = run([], nested)
    assert result.exit_code == 0, result.output
    assert f"config:    {tmp_path / 'coop-data-doc.yml'}" in result.output


def _fake_site_builder(cli_module, monkeypatch):
    """Replace the real mkdocs build with a stub that still produces index.html,
    so portal-link assertions work without paying for a full site build."""

    def fake_build_site(mkdocs_config, site_dir, on_page=None):
        Path(site_dir).mkdir(parents=True, exist_ok=True)
        (Path(site_dir) / "index.html").write_text("<html></html>", encoding="utf-8")

    monkeypatch.setattr(cli_module, "build_site", fake_build_site)


def _fake_confirm_questionary(answer):
    class FakeQuestionary:
        @staticmethod
        def confirm(message, **kwargs):
            class _Result:
                @staticmethod
                def ask():
                    return answer

            return _Result()

    return FakeQuestionary


def test_setup_build_now_prints_first_run_tour(tmp_path: Path, monkeypatch):
    # issue #37: the happy first run (setup -> build now) closes with the four
    # adoption facts: portal link, what to commit, the CI gate, how to rebuild.
    from coop_data_doc import cli as cli_module
    from coop_data_doc import wizard as wizard_module
    from coop_data_doc.config import Config

    setup_workspace(tmp_path)
    monkeypatch.setattr(wizard_module, "run_setup", lambda p, io=None: Config.load(Path(p)))
    monkeypatch.setattr(cli_module, "questionary", _fake_confirm_questionary(True))
    _fake_site_builder(cli_module, monkeypatch)
    result = run(["setup"], tmp_path)
    assert result.exit_code == 0, result.output
    assert "✓ Docs built." in result.output
    assert "Open the portal:" in result.output and "file:///" in result.output
    assert "Commit these files:" in result.output and "data-docs" in result.output
    assert "Gate CI with:         coop-data-doc check" in result.output
    assert "Rebuild any time:     coop-data-doc build" in result.output


def test_setup_build_later_prints_no_tour(tmp_path: Path, monkeypatch):
    # answering "no" keeps today's single follow-up line — no tour
    from coop_data_doc import cli as cli_module
    from coop_data_doc import wizard as wizard_module
    from coop_data_doc.config import Config

    setup_workspace(tmp_path)
    monkeypatch.setattr(wizard_module, "run_setup", lambda p, io=None: Config.load(Path(p)))
    monkeypatch.setattr(cli_module, "questionary", _fake_confirm_questionary(False))
    result = run(["setup"], tmp_path)
    assert result.exit_code == 0, result.output
    assert "Build them whenever you're ready" in result.output
    assert "Open the portal:" not in result.output
    assert "Commit these files:" not in result.output


def test_plain_build_prints_no_tour(tmp_path: Path, monkeypatch):
    # a plain `build` (the CI path) must not grow a tour
    from coop_data_doc import cli as cli_module

    setup_workspace(tmp_path)
    _fake_site_builder(cli_module, monkeypatch)
    result = run(["build", "--non-interactive"], tmp_path)
    assert result.exit_code == 0, result.output
    assert "Commit these files:" not in result.output
    assert "Gate CI with:" not in result.output


def test_portal_url_is_valid_file_uri(tmp_path: Path, monkeypatch):
    # issue #34: the portal line must be a real file URL — produced via
    # Path.as_uri() (file:///C:/... on Windows, file:///... on POSIX), never
    # string concatenation that keeps backslashes and drops the third slash.
    from coop_data_doc import cli as cli_module

    setup_workspace(tmp_path)
    monkeypatch.setattr(cli_module, "build_site", lambda *args, **kwargs: None)
    result = run(["build", "--non-interactive"], tmp_path)
    assert result.exit_code == 0, result.output
    portal_line = next(line for line in result.output.splitlines() if "HTML portal:" in line)
    assert "file:///" in portal_line
    assert "\\" not in portal_line


def test_status_detects_staleness(tmp_path: Path):
    """status reports stale when the committed docs no longer match a fresh render."""
    setup_workspace(tmp_path)
    (tmp_path / ".lineage-cache.json").write_text(_FULL_CACHE, encoding="utf-8")
    assert run(["build", "--non-interactive", "--skip-html"], tmp_path).exit_code == 0
    assert "up to date" in run(["status"], tmp_path).output
    # an orphaned committed page (object no longer produced) makes docs stale
    (tmp_path / "data-docs" / "view" / "dbo-v_ghost.md").write_text(
        "---\nid: view:dbo.v_ghost\n---\n", encoding="utf-8"
    )
    result = run(["status"], tmp_path)
    assert result.exit_code == 0  # status reports, never fails the way `check` does
    assert "stale" in result.output


def test_build_prune_survives_unwritable_cache(tmp_path: Path, monkeypatch):
    # The post-render prune (persist=True) rewrites .lineage-cache.json when it
    # drops a stale entry. A locked/read-only file there must NOT abort a fully
    # rendered build: the write records a cache_write_failed warning (never
    # raises) and _run_build surfaces it. Seed a stale entry so the prune writes.
    from coop_data_doc.config import ParseWarning
    from coop_data_doc.linker.cache import LineageCache

    setup_workspace(tmp_path)
    (tmp_path / ".lineage-cache.json").write_text(
        '{\n  "version": 1,\n  "mappings": {\n'
        '    "pbi_table:sales.fact_sales": {\n'
        '      "target": "gold_table:dbo.gone",\n'  # target absent from the graph → pruned
        '      "method": "interactive"\n    }\n  }\n}\n',
        encoding="utf-8",
    )

    def failing_write(self):
        self.warnings.append(
            ParseWarning(
                file=str(self.path),
                message="could not write lineage cache: locked",
                category="cache_write_failed",
            )
        )
        return False

    monkeypatch.setattr(LineageCache, "write", failing_write)
    result = run(["build", "--non-interactive", "--skip-html"], tmp_path)
    assert result.exit_code == 0, result.output  # docs rendered; prune write is non-fatal
    assert "could not write lineage cache" in result.output


def test_undecodable_sql_file_fails_ci_gate(tmp_path: Path):
    # issue #16: a BOM-less UTF-16 (undecodable) SQL file means objects are silently
    # missing — an error-severity diagnostic that must fail build --strict AND check.
    setup_workspace(tmp_path)
    (tmp_path / "sql-repo" / "nobom.sql").write_bytes(
        "CREATE TABLE dbo.orders (id INT);\nGO\n".encode("utf-16-le")
    )
    build = run(["build", "--non-interactive", "--strict", "--skip-html"], tmp_path)
    assert build.exit_code == 2, build.output
    assert "encoding_unreadable" in build.output and "nobom.sql" in build.output
    chk = run(["check"], tmp_path)
    assert chk.exit_code == 2, chk.output
    assert "encoding_unreadable" in chk.output


def test_corrupt_bim_fails_ci_gate(tmp_path: Path):
    setup_workspace(tmp_path)
    (tmp_path / "pbi-repo" / "broken.bim").write_text("{ not valid json", encoding="utf-8")
    build = run(["build", "--non-interactive", "--strict", "--skip-html"], tmp_path)
    assert build.exit_code == 2, build.output
    assert "bim_parse" in build.output


def test_lenient_check_still_fails_on_error_severity(tmp_path: Path):
    # --lenient forgives risky-parse warnings, but a corrupt/undecodable file (error
    # severity) is never "known and accepted" — it still fails.
    setup_workspace(tmp_path)
    (tmp_path / "sql-repo" / "nobom.sql").write_bytes("SELECT 1\n".encode("utf-16-le"))
    assert run(["check", "--lenient"], tmp_path).exit_code == 2
