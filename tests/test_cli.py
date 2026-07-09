import shutil
from pathlib import Path

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
    from coop_data_doc.config import Config, DEFAULT_CONFIG

    config = tmp_path / DEFAULT_CONFIG
    config.write_text("project_name: Test\nrepos:\n  sql:\n    path: ./sql\n  powerbi:\n    path: ./pbi\n")
    found = Config.find(start_dir=tmp_path)
    assert found is not None
    assert found.name == DEFAULT_CONFIG
    assert found.parent == tmp_path


def test_config_find_walks_up_parents(tmp_path: Path):
    """Config.find() walks up parent directories to find config."""
    from coop_data_doc.config import Config, DEFAULT_CONFIG

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

    def fake_run_setup(path):
        captured["path"] = path
        return None  # setup prints "Saved {path}" and returns cleanly

    monkeypatch.setattr(cli_module, "questionary", FakeQuestionary)
    monkeypatch.setattr(cli_module, "_stdio_is_interactive", lambda: True)
    monkeypatch.setattr(wizard_module, "run_setup", fake_run_setup)
    result = run([], nested)
    assert result.exit_code == 0, result.output
    assert captured["path"] == tmp_path / "coop-data-doc.yml"  # the discovered parent config
    assert not (nested / "coop-data-doc.yml").exists()  # no shadowing nested config


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
