"""catalog_dependencies: the parsed lineage checked against the database's own
dependency list (`sqlsc dependencies` → Dependencies.json). It only warns; it
never adds an edge and never fails a gate."""

import json
from pathlib import Path

from test_cli import run, setup_workspace

from coop_data_doc.cli import run_pipeline
from coop_data_doc.config import Config


def _dep(schema, name, otype, ref_schema, ref_name, ref_type, **extra):
    return {
        "object": {"schema": schema, "name": name, "type": otype},
        "references": {"schema": ref_schema, "name": ref_name, "type": ref_type, **extra},
    }


DEPENDENCIES = [
    # linked by the parse: no warning
    _dep("sales", "dim_customer", "view", "silver", "customers", "table"),
    _dep("dbo", "usp_load_fact_sales", "procedure", "dbo", "fact_sales", "table"),
    _dep("dbo", "tr_fact_sales_audit", "trigger", "dbo", "fact_sales", "table"),
    # missed by the parse: one warning each
    _dep("sales", "dim_customer", "view", "dbo", "fn_customer_sales", "function"),
    _dep("dbo", "usp_load_fact_sales", "procedure", None, "lookup_rates", None),
    _dep("dbo", "usp_load_fact_sales", "procedure", "dbo", "rates", "table", database="FinanceDb"),
    # never warned: a sequence, a #temp table, an undocumented object, an ignored schema
    _dep("dbo", "usp_load_fact_sales", "procedure", "dbo", "order_no", "sequence"),
    _dep("dbo", "usp_load_fact_sales", "procedure", None, "#staged_orders", None),
    _dep("dbo", "usp_not_in_repo", "procedure", "dbo", "anything", "table"),
    _dep("dbo", "usp_load_fact_sales", "procedure", "sys", "objects", "view"),
]


def _missed(obj: str, ref: str) -> str:
    return (
        f"{obj} references {ref} in the database's dependency list "
        "(Dependencies.json), but the parsed lineage has no link between them"
    )


def _workspace(tmp_path: Path, document) -> Path:
    setup_workspace(tmp_path)
    (tmp_path / "Dependencies.json").write_text(json.dumps(document), encoding="utf-8")
    config = tmp_path / "coop-data-doc.yml"
    config.write_text(
        config.read_text(encoding="utf-8") + 'catalog_dependencies:\n  - "Dependencies.json"\n',
        encoding="utf-8",
    )
    return config


def _catalog_warnings(config: Path):
    _, _, warnings = run_pipeline(Config.load(config), interactive=False)
    return [(w.category, w.file, w.message) for w in warnings if w.category.startswith("catalog_")]


def test_reports_only_the_references_the_parse_missed(tmp_path: Path):
    config = _workspace(tmp_path, {"version": 1, "dependencies": DEPENDENCIES})
    assert _catalog_warnings(config) == [
        (
            "catalog_dependency_unparsed",
            "procs/usp_load_fact_sales.sql",
            _missed("dbo.usp_load_fact_sales", "financedb.dbo.rates"),
        ),
        (
            "catalog_dependency_unparsed",
            "procs/usp_load_fact_sales.sql",
            _missed("dbo.usp_load_fact_sales", "lookup_rates"),
        ),
        (
            "catalog_dependency_unparsed",
            "views/sales/dim_customer.sql",
            _missed("sales.dim_customer", "dbo.fn_customer_sales"),
        ),
    ]


def test_never_adds_edges_and_never_gates(tmp_path: Path):
    config = _workspace(tmp_path, {"version": 1, "dependencies": DEPENDENCIES})
    graph, _, _ = run_pipeline(Config.load(config), interactive=False)
    plain, _, _ = run_pipeline(Config.load(tmp_path / "coop-data-doc.yml"), interactive=False)
    assert {e.key() for e in graph.edges} == {e.key() for e in plain.edges}
    assert run(["scan", "--non-interactive"], tmp_path).exit_code == 0


def test_unusable_files_warn_and_are_skipped(tmp_path: Path):
    config = _workspace(tmp_path, {"version": 2, "dependencies": DEPENDENCIES})
    assert [c for c, _, _ in _catalog_warnings(config)] == ["catalog_dependencies_unreadable"]
    (tmp_path / "Dependencies.json").write_text("{not json", encoding="utf-8")
    assert [c for c, _, _ in _catalog_warnings(config)] == ["catalog_dependencies_unreadable"]
    (tmp_path / "Dependencies.json").unlink()
    assert _catalog_warnings(config) == [
        (
            "catalog_dependencies_unreadable",
            "Dependencies.json",
            "catalog dependency list not used: file not found",
        )
    ]


def test_config_round_trips_the_list(tmp_path: Path):
    _workspace(tmp_path, {"version": 1, "dependencies": []})
    shown = json.loads(run(["show-config"], tmp_path).output)
    assert shown["catalog_dependencies"] == ["Dependencies.json"]
    patch = json.dumps({"catalog_dependencies": ["../sql/database/Dependencies.json"]})
    assert run(["config-set", "--from-json", "-"], tmp_path, input=patch).exit_code == 0
    assert Config.load(tmp_path / "coop-data-doc.yml").catalog_dependencies == [
        "../sql/database/Dependencies.json"
    ]
