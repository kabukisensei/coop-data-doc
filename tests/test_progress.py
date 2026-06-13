from pathlib import Path

from coop_data_doc.config import Config, RepoConfig
from coop_data_doc.crawler import FileKind, crawl
from coop_data_doc.graph import LineageGraph
from coop_data_doc.parsers.sql_objects import parse_sql_objects
from coop_data_doc.parsers.sql_procs import parse_sql_procs
from coop_data_doc.parsers.tmdl import parse_tmdl
from coop_data_doc.progress import Progress, should_enable
from coop_data_doc.render.markdown import render_markdown

FIXTURES = Path(__file__).parent / "fixtures"


def test_should_enable_respects_quiet_and_tty(monkeypatch):
    import sys

    class FakeTTY:
        @staticmethod
        def isatty():
            return True

    monkeypatch.setattr(sys, "stderr", FakeTTY())
    assert should_enable(quiet=False) is True
    assert should_enable(quiet=True) is False  # -q always wins

    class FakeNonTTY:
        @staticmethod
        def isatty():
            return False

    monkeypatch.setattr(sys, "stderr", FakeNonTTY())
    assert should_enable(quiet=False) is False  # piped / CI


def test_disabled_progress_is_silent(capsys):
    progress = Progress(enabled=False)
    progress.line("should not appear")
    with progress.bar("phase", total=5) as tick:
        for _ in range(5):
            tick("item")
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_enabled_bar_yields_working_tick():
    # enabled but we just exercise the tick path without asserting on the
    # rendered control characters (click handles the drawing)
    progress = Progress(enabled=True)
    with progress.bar("phase", total=3) as tick:
        for _ in range(3):
            tick()  # must not raise


def test_empty_phase_yields_noop():
    progress = Progress(enabled=True)
    with progress.bar("nothing", total=0) as tick:
        tick()  # no-op, must not raise


def sql_entries():
    config = Config(
        repos={
            "sql": RepoConfig(
                path=str(FIXTURES / "repo_sql"),
                include=["**/*.sql"],
                exclude=["**/archive/**"],
            )
        }
    )
    inventory, _ = crawl(config)
    return inventory.by_kind(FileKind.SQL_FILE)


def test_parsers_tick_once_per_file():
    entries = sql_entries()
    graph = LineageGraph()
    seen: list[str] = []
    parse_sql_objects(entries, graph, on_file=seen.append)
    assert len(seen) == len(entries)
    assert set(seen) == {e.path for e in entries}


def test_tmdl_ticks_per_file():
    config = Config(
        repos={
            "powerbi": RepoConfig(
                path=str(FIXTURES / "repo_pbi"),
                include=["**/*.tmdl"],
            )
        }
    )
    inventory, _ = crawl(config)
    tmdl = inventory.by_kind(FileKind.TMDL)
    graph = LineageGraph()
    count = []
    parse_tmdl(tmdl, graph, on_file=lambda _p: count.append(1))
    assert len(count) == len(tmdl)


def test_render_ticks_per_node(tmp_path: Path):
    entries = sql_entries()
    graph = LineageGraph()
    parse_sql_objects(entries, graph)
    parse_sql_procs(entries, graph)
    ticks: list[str] = []
    render_markdown(graph, tmp_path, "Test", on_node=ticks.append)
    assert len(ticks) == len(graph.nodes)


def test_callbacks_are_optional():
    # omitting on_file must behave exactly as before (no error)
    graph = LineageGraph()
    parse_sql_objects(sql_entries(), graph)
    assert len(graph.nodes) > 0
