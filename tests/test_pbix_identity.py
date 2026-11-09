"""Same-named .pbix archives are distinct sources, never one merged model (#97)."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from coop_data_doc.cli import cli
from coop_data_doc.crawler import FileEntry, FileKind
from coop_data_doc.graph import LineageGraph
from coop_data_doc.parsers.bim import parse_bim
from coop_data_doc.parsers.pbix import parse_pbix
from coop_data_doc.parsers.tmdl import parse_tmdl


def _mashup(table: str) -> bytes:
    inner = io.BytesIO()
    expression = (
        'section Section1; shared Orders = let Source=Sql.Database("fixture-server","fixture-db"), '
        f'Data=Source{{[Schema="dbo",Item="{table}"]}}[Data] in Data;'
    )
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("Formulas/Section1.m", expression)
    return b"\x00\x00\x00\x00" + inner.getvalue()


def make_pbix(path: Path, table: str | None = None, *, opaque: bool = False, layout: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        if table:
            z.writestr("DataMashup", _mashup(table))
        if opaque:
            z.writestr("DataModel", b"\x00opaque")
        if layout:
            z.writestr("Report/Layout", json.dumps({"sections": [{"name": "Page1"}]}).encode("utf-16-le"))


def pbix_entry(root: Path, repo: str, rel: str, **kwargs) -> FileEntry:
    path = root / repo / rel
    make_pbix(path, **kwargs)
    return FileEntry(
        path=rel, abs_path=str(path), repo_key=repo, kind=FileKind.PBIX, size=path.stat().st_size
    )


def text_entry(root: Path, repo: str, rel: str, text: str, kind: FileKind) -> FileEntry:
    path = root / repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return FileEntry(path=rel, abs_path=str(path), repo_key=repo, kind=kind, size=path.stat().st_size)


def collisions(warnings) -> list[str]:
    return [w.file for w in warnings if w.category == "identity_collision"]


@pytest.mark.parametrize(
    "first, second",
    [
        (("a", "Sales.pbix"), ("b", "Sales.pbix")),  # two repository roots
        (("a", "east/Sales.pbix"), ("a", "west/Sales.pbix")),  # two folders of one root
    ],
)
@pytest.mark.parametrize("opaque", [False, True])
def test_same_named_archives_are_not_merged(tmp_path, first, second, opaque):
    kwargs = {"opaque": True} if opaque else {}
    entries = [
        pbix_entry(tmp_path, first[0], first[1], table=None if opaque else "OrdersA", **kwargs),
        pbix_entry(tmp_path, second[0], second[1], table=None if opaque else "OrdersB", **kwargs),
    ]
    graph = LineageGraph()
    warnings = parse_pbix(entries, graph)
    assert collisions(warnings) == [second[1]]
    model = graph.nodes["semantic_model:sales"]
    assert model.metadata["source_identity"] == [first[0], first[1]]
    if not opaque:
        sources = graph.nodes["pbi_table:sales.orders"].metadata["partition_sources"]
        assert [s["object"] for s in sources] == ["ordersa"]


def test_distinct_names_are_not_a_collision(tmp_path):
    entries = [
        pbix_entry(tmp_path, "a", "Sales.pbix", table="OrdersA"),
        pbix_entry(tmp_path, "b", "Finance.pbix", table="OrdersB"),
    ]
    graph = LineageGraph()
    assert collisions(parse_pbix(entries, graph)) == []
    assert {"semantic_model:sales", "semantic_model:finance"} <= set(graph.nodes)


def test_a_pbix_beside_a_tmdl_or_bim_model_of_that_name_is_not_merged(tmp_path):
    for kind in ("tmdl", "bim"):
        graph = LineageGraph()
        if kind == "tmdl":
            parse_tmdl(
                [
                    text_entry(
                        tmp_path / kind,
                        "pbi",
                        "Sales.SemanticModel/definition/tables/one.tmdl",
                        "table One",
                        FileKind.TMDL,
                    )
                ],
                graph,
            )
        else:
            parse_bim(
                [
                    text_entry(
                        tmp_path / kind,
                        "pbi",
                        "Sales.bim",
                        json.dumps({"name": "Sales", "model": {"tables": [{"name": "One"}]}}),
                        FileKind.BIM,
                    )
                ],
                graph,
            )
        before = set(graph.nodes)
        warnings = parse_pbix(
            [pbix_entry(tmp_path / kind, "pbi", "Sales.pbix", table="OrdersA", layout=True)], graph
        )
        assert collisions(warnings) == ["Sales.pbix"], kind
        assert set(graph.nodes) == before, "nothing from the archive is added: no table, no report"


def test_same_named_thin_reports_are_not_merged(tmp_path):
    entries = [
        pbix_entry(tmp_path, "a", "Sales.pbix", layout=True),
        pbix_entry(tmp_path, "b", "Sales.pbix", layout=True),
    ]
    graph = LineageGraph()
    assert collisions(parse_pbix(entries, graph)) == ["Sales.pbix"]
    assert graph.nodes["report:sales"].metadata["pbix_source_identity"] == ["a", "Sales.pbix"]


def test_one_archive_keeps_its_best_effort_extraction(tmp_path):
    graph = LineageGraph()
    warnings = parse_pbix([pbix_entry(tmp_path, "a", "Sales.pbix", table="OrdersA", layout=True)], graph)
    assert collisions(warnings) == []
    assert {"semantic_model:sales", "pbi_table:sales.orders", "report:sales"} <= set(graph.nodes)


def _estate(root: Path) -> Path:
    make_pbix(root / "a" / "Sales.pbix", "OrdersA")
    make_pbix(root / "b" / "Sales.pbix", "OrdersB")
    (root / "sql").mkdir()
    (root / "sql" / "tables.sql").write_text(
        "CREATE TABLE dbo.OrdersA (Id int);\nGO\nCREATE TABLE dbo.OrdersB (Id int);\n", encoding="utf-8"
    )
    config = root / "coop-data-doc.yml"
    config.write_text(
        "project_name: PBIX collision fixture\n"
        "repos:\n"
        '  sql: {path: ./sql, include: ["**/*.sql"]}\n'
        '  a: {path: ./a, include: ["**/*.pbix"]}\n'
        '  b: {path: ./b, include: ["**/*.pbix"]}\n'
        "output: {dir: ./docs, site_dir: ./site}\n",
        encoding="utf-8",
    )
    return config


@pytest.mark.parametrize("strict", [True, False])
def test_scan_rejects_publication_and_keeps_the_prior_output(tmp_path, strict):
    config = _estate(tmp_path)
    (tmp_path / "docs").mkdir()
    prior = tmp_path / "docs" / "graph.json"
    prior.write_text('{"prior": true}\n', encoding="utf-8")
    args = ["scan", "--config", str(config), "--non-interactive", "--no-parse-cache", "--jobs", "1"]
    result = CliRunner().invoke(cli, [*args, "--strict"] if strict else args, catch_exceptions=False)
    assert result.exit_code == 2
    assert "identity_collision" in result.output
    assert prior.read_text(encoding="utf-8") == '{"prior": true}\n'


def test_the_collision_result_is_deterministic(tmp_path):
    outputs = []
    for run in ("one", "two"):
        entries = [
            pbix_entry(tmp_path / run, "b", "Sales.pbix", table="OrdersB"),
            pbix_entry(tmp_path / run, "a", "Sales.pbix", table="OrdersA"),
        ]
        graph = LineageGraph()
        warnings = parse_pbix(entries, graph)
        outputs.append((sorted(graph.nodes), [(w.file, w.category) for w in warnings]))
        assert graph.nodes["semantic_model:sales"].metadata["source_identity"] == ["a", "Sales.pbix"]
    assert outputs[0] == outputs[1]
