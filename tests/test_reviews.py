"""Review-findings composition (issue #38): tolerant loading, the join rules,
the rendered Findings page, CLI surface, and determinism."""

import json
import shutil
from pathlib import Path

from test_cli import doc_page, run, setup_workspace
from test_determinism import tree_bytes

from coop_data_doc.graph.model import LineageGraph, Node, NodeType, normalize_identifier
from coop_data_doc.reviews import (
    DAX_REVIEW_TOOL,
    SQL_REVIEW_TOOL,
    ReviewEnvelope,
    ReviewFinding,
    join_reviews,
    load_review_file,
)

FIXTURES = Path(__file__).parent / "fixtures"


def copy_reviews(tmp_path: Path) -> Path:
    reviews = tmp_path / "reviews"
    shutil.copytree(FIXTURES / "reviews", reviews)
    return reviews


def make_node(node_type: NodeType, schema: str, name: str) -> Node:
    return Node(
        id=Node.make_id(node_type, schema, name),
        node_type=node_type,
        name=normalize_identifier(name),
        schema_name=normalize_identifier(schema),
    )


def make_graph(*specs: tuple[NodeType, str, str]) -> LineageGraph:
    graph = LineageGraph()
    for node_type, schema, name in specs:
        graph.add_node(make_node(node_type, schema, name))
    return graph


def sql_finding(obj: str, rule: str = "SQL-X", severity: str = "warning", line: int = 1) -> ReviewFinding:
    return ReviewFinding(
        tool=SQL_REVIEW_TOOL,
        rule_id=rule,
        severity=severity,
        message="msg",
        file="f.sql",
        line=line,
        object=obj,
    )


def dax_finding(model: str, obj: str, rule: str = "DAX-X", severity: str = "warning") -> ReviewFinding:
    return ReviewFinding(
        tool=DAX_REVIEW_TOOL,
        rule_id=rule,
        severity=severity,
        message="msg",
        file="m.tmdl",
        line=1,
        object=obj,
        model=model,
    )


def envelope(tool: str, *findings: ReviewFinding, agent: int = 0) -> ReviewEnvelope:
    return ReviewEnvelope(tool=tool, path="x.json", findings=list(findings), agent_review_count=agent)


# --- tolerant loading ---------------------------------------------------------


def test_load_missing_file_degrades_to_warning(tmp_path: Path):
    result, warnings = load_review_file(tmp_path / "nope.json", "nope.json")
    assert result is None
    assert [w.category for w in warnings] == ["reviews_unreadable"]
    assert warnings[0].file == "nope.json"


def test_load_non_json_degrades_to_warning(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text("not json at all {", encoding="utf-8")
    result, warnings = load_review_file(bad, "bad.json")
    assert result is None
    assert [w.category for w in warnings] == ["reviews_unreadable"]


def test_load_unknown_tool_degrades_to_warning(tmp_path: Path):
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"tool": "some-other-linter", "schema_version": 1, "findings": []}))
    result, warnings = load_review_file(other, "other.json")
    assert result is None
    assert [w.category for w in warnings] == ["reviews_unreadable"]
    assert "some-other-linter" in warnings[0].message


def test_load_unknown_integer_schema_version_still_joins(tmp_path: Path):
    future = tmp_path / "future.json"
    future.write_text(
        json.dumps(
            {
                "tool": "coop-sql-review",
                "schema_version": 99,
                "findings": [
                    {
                        "rule_id": "R",
                        "severity": "warning",
                        "file": "a.sql",
                        "line": 1,
                        "object": "s.t",
                        "message": "m",
                    }
                ],
                "agent_review": [],
            }
        )
    )
    result, warnings = load_review_file(future, "future.json")
    assert result is not None
    assert len(result.findings) == 1
    assert [w.category for w in warnings] == ["reviews_schema_mismatch"]


def test_load_non_integer_schema_version_unreadable(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"tool": "coop-sql-review", "schema_version": "4", "findings": []}))
    result, warnings = load_review_file(bad, "bad.json")
    assert result is None
    assert [w.category for w in warnings] == ["reviews_unreadable"]


def test_load_missing_findings_list_unreadable(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"tool": "coop-dax-review", "schema_version": 3}))
    result, warnings = load_review_file(bad, "bad.json")
    assert result is None
    assert [w.category for w in warnings] == ["reviews_unreadable"]


def test_load_non_object_finding_skipped_never_silently(tmp_path: Path):
    mixed = tmp_path / "mixed.json"
    mixed.write_text(
        json.dumps(
            {
                "tool": "coop-sql-review",
                "schema_version": 4,
                "findings": [
                    "not-a-finding",
                    {
                        "rule_id": "R",
                        "severity": "info",
                        "file": "a.sql",
                        "line": 2,
                        "object": "s.t",
                        "message": "m",
                    },
                ],
            }
        )
    )
    result, warnings = load_review_file(mixed, "mixed.json")
    assert result is not None
    assert len(result.findings) == 1  # the good one survives
    assert [w.category for w in warnings] == ["reviews_unreadable"]
    assert "#0" in warnings[0].message


# --- join rules ----------------------------------------------------------------


def test_sql_finding_attaches_to_all_colliding_sql_nodes():
    graph = make_graph(
        (NodeType.VIEW, "sales", "thing"),
        (NodeType.GOLD_TABLE, "sales", "thing"),
        (NodeType.PBI_TABLE, "sales", "thing"),  # NOT a SQL type: never a sql-join target
    )
    join = join_reviews(graph, [envelope(SQL_REVIEW_TOOL, sql_finding("sales.thing"))])
    assert sorted(join.by_node) == ["gold_table:sales.thing", "view:sales.thing"]
    assert join.unmatched == []
    assert join.total == 1  # a multi-attach finding still counts once


def test_sql_finding_normalizes_brackets_and_case():
    graph = make_graph((NodeType.GOLD_TABLE, "dbo", "fact sales"))
    join = join_reviews(graph, [envelope(SQL_REVIEW_TOOL, sql_finding("[dbo].[Fact Sales]"))])
    assert list(join.by_node) == ["gold_table:dbo.fact sales"]


def test_sql_empty_object_lands_in_unmatched():
    graph = make_graph((NodeType.VIEW, "sales", "thing"))
    join = join_reviews(graph, [envelope(SQL_REVIEW_TOOL, sql_finding(""))])
    assert join.by_node == {}
    assert len(join.unmatched) == 1


def test_dax_measure_tried_before_table():
    graph = make_graph(
        (NodeType.MEASURE, "sales", "margin"),
        (NodeType.PBI_TABLE, "sales", "margin"),
    )
    join = join_reviews(graph, [envelope(DAX_REVIEW_TOOL, dax_finding("Sales", "Margin"))])
    assert list(join.by_node) == ["measure:sales.margin"]


def test_dax_table_fallback_when_no_measure():
    graph = make_graph((NodeType.PBI_TABLE, "sales", "fact_sales"))
    join = join_reviews(graph, [envelope(DAX_REVIEW_TOOL, dax_finding("Sales", "fact_sales"))])
    assert list(join.by_node) == ["pbi_table:sales.fact_sales"]


def test_dax_model_level_finding_attaches_to_semantic_model():
    graph = make_graph((NodeType.SEMANTIC_MODEL, "", "sales"))
    for obj in ("", "Sales"):  # empty object or object == model → model-level
        join = join_reviews(graph, [envelope(DAX_REVIEW_TOOL, dax_finding("Sales", obj))])
        assert list(join.by_node) == ["semantic_model:sales"], obj


def test_unmatched_findings_are_never_dropped():
    graph = make_graph((NodeType.SEMANTIC_MODEL, "", "sales"))
    join = join_reviews(
        graph,
        [
            envelope(SQL_REVIEW_TOOL, sql_finding("dbo.ghost"), agent=2),
            envelope(DAX_REVIEW_TOOL, dax_finding("Sales", "Ghost Measure")),
        ],
    )
    assert join.by_node == {}
    assert len(join.unmatched) == 2
    assert join.agent_review_counts == {SQL_REVIEW_TOOL: 2, DAX_REVIEW_TOOL: 0}
    assert join.counts == {SQL_REVIEW_TOOL: {"warning": 1}, DAX_REVIEW_TOOL: {"warning": 1}}


def test_per_node_findings_sorted_severity_then_rule_then_line():
    graph = make_graph((NodeType.VIEW, "s", "v"))
    join = join_reviews(
        graph,
        [
            envelope(
                SQL_REVIEW_TOOL,
                sql_finding("s.v", rule="SQL-B", severity="warning", line=9),
                sql_finding("s.v", rule="SQL-A", severity="warning", line=5),
                sql_finding("s.v", rule="SQL-Z", severity="error", line=1),
                sql_finding("s.v", rule="SQL-A", severity="warning", line=2),
            )
        ],
    )
    rows = [(f.severity, f.rule_id, f.line) for f in join.by_node["view:s.v"]]
    assert rows == [
        ("error", "SQL-Z", 1),
        ("warning", "SQL-A", 2),
        ("warning", "SQL-A", 5),
        ("warning", "SQL-B", 9),
    ]


# --- CLI + rendering -----------------------------------------------------------


def build_with_reviews(tmp_path: Path) -> None:
    setup_workspace(tmp_path)
    copy_reviews(tmp_path)
    result = run(
        [
            "build",
            "--non-interactive",
            "--skip-html",
            "--reviews",
            "reviews/sql.json",
            "--reviews",
            "reviews/dax.json",
        ],
        tmp_path,
    )
    assert result.exit_code == 0, result.output


def test_build_with_reviews_renders_pages_and_findings(tmp_path: Path):
    build_with_reviews(tmp_path)
    docs = tmp_path / "data-docs"

    # matched object pages carry the chip + the findings table
    view_page = doc_page(docs, "view:sales.dim_customer").read_text(encoding="utf-8")
    assert "> ⚠ **1 review finding** — see below." in view_page
    assert "## Review findings" in view_page
    assert "SQL-SELECT-STAR" in view_page
    assert "views/sales/dim_customer.sql:4" in view_page

    gold_page = doc_page(docs, "gold_table:dbo.fact_sales").read_text(encoding="utf-8")
    assert "SQL-HEADER-COMMENT" in gold_page

    measure_page = doc_page(docs, "measure:sales.total sales").read_text(encoding="utf-8")
    assert "DAX-FORMAT-STRING" in measure_page

    pbi_table_page = doc_page(docs, "pbi_table:sales.fact_sales").read_text(encoding="utf-8")
    assert "DAX-TABLE-DESCRIPTION" in pbi_table_page

    # model-level finding lands on the semantic model's page
    model_page = doc_page(docs, "semantic_model:sales").read_text(encoding="utf-8")
    assert "DAX-AUTO-DATETIME" in model_page

    # the Findings page: summary, groups, unmatched, agent-review note
    findings = (docs / "findings.md").read_text(encoding="utf-8")
    assert "| coop-sql-review | warning | 2 |" in findings
    assert "| coop-sql-review | error | 1 |" in findings  # unmatched findings still count
    # matched findings group severity → rule (the only matched severities here)
    assert "## Warning" in findings and "## Info" in findings
    # unmatched findings are listed, never dropped: the ghost object, the
    # empty-object sql finding, and the unknown measure
    assert "## Not matched to a documented object" in findings
    assert "dbo.no_such_table" in findings
    assert "SQL-FILE-ENCODING" in findings  # the empty-object finding
    assert "Sales.No Such Measure" in findings
    # agent_review is counted only (v1 joins findings only)
    assert "- coop-sql-review: 1 agent-review item" in findings
    assert "- coop-dax-review: 0 agent-review items" in findings

    # index links the findings page
    index = (docs / "index.md").read_text(encoding="utf-8")
    assert "[8 review findings](findings.md)" in index

    # the graph/manifest contracts are untouched by the feature
    manifest = (docs / "manifest.json").read_text(encoding="utf-8")
    assert "SQL-SELECT-STAR" not in manifest and "findings" not in json.loads(manifest)


def test_unaffected_object_page_has_no_findings_section(tmp_path: Path):
    build_with_reviews(tmp_path)
    page = doc_page(tmp_path / "data-docs", "view:sales.v_orders_star").read_text(encoding="utf-8")
    assert "## Review findings" not in page
    assert "review finding" not in page


def test_reviews_flag_missing_file_is_exit_2(tmp_path: Path):
    setup_workspace(tmp_path)
    result = run(
        ["build", "--non-interactive", "--skip-html", "--reviews", "no-such.json"],
        tmp_path,
    )
    assert result.exit_code == 2
    assert "no-such.json" in result.output


def test_reviews_corrupt_file_warns_never_crashes(tmp_path: Path):
    setup_workspace(tmp_path)
    (tmp_path / "corrupt.json").write_text("{ not json", encoding="utf-8")
    result = run(
        ["build", "--non-interactive", "--skip-html", "--reviews", "corrupt.json"],
        tmp_path,
    )
    assert result.exit_code == 0, result.output
    assert "reviews_unreadable" in result.output
    # the feature was requested, so the Findings page still renders (empty)
    findings = (tmp_path / "data-docs" / "findings.md").read_text(encoding="utf-8")
    assert "No review findings were loaded" in findings


def test_reviews_config_key_and_flag_extends_config(tmp_path: Path):
    setup_workspace(tmp_path)
    copy_reviews(tmp_path)
    config = tmp_path / "coop-data-doc.yml"
    config.write_text(
        config.read_text(encoding="utf-8") + 'reviews: ["reviews/sql.json"]\n',
        encoding="utf-8",
        newline="\n",
    )
    # config supplies sql.json; the flag EXTENDS it with dax.json
    result = run(
        ["build", "--non-interactive", "--skip-html", "--reviews", "reviews/dax.json"],
        tmp_path,
    )
    assert result.exit_code == 0, result.output
    findings = (tmp_path / "data-docs" / "findings.md").read_text(encoding="utf-8")
    assert "coop-sql-review" in findings and "coop-dax-review" in findings
    assert "SQL-SELECT-STAR" in findings and "DAX-FORMAT-STRING" in findings


def test_reviews_config_and_flag_overlap_dedups_no_double_count(tmp_path: Path):
    # sql.json is listed in config AND passed via --reviews (plus dax.json):
    # the config+flag overlap is deduped, so counts/chips/index never double.
    setup_workspace(tmp_path)
    copy_reviews(tmp_path)
    config = tmp_path / "coop-data-doc.yml"
    config.write_text(
        config.read_text(encoding="utf-8") + 'reviews: ["reviews/sql.json"]\n',
        encoding="utf-8",
        newline="\n",
    )
    result = run(
        [
            "build",
            "--non-interactive",
            "--skip-html",
            "--reviews",
            "reviews/sql.json",
            "--reviews",
            "reviews/dax.json",
        ],
        tmp_path,
    )
    assert result.exit_code == 0, result.output
    findings = (tmp_path / "data-docs" / "findings.md").read_text(encoding="utf-8")
    # single-count: sql.json is read once despite config+flag overlap
    assert "| coop-sql-review | warning | 2 |" in findings
    index = (tmp_path / "data-docs" / "index.md").read_text(encoding="utf-8")
    assert "[8 review findings](findings.md)" in index


def test_reviews_flag_repeated_twice_dedups_no_double_count(tmp_path: Path):
    # the SAME --reviews flag repeated is read once (first occurrence wins).
    setup_workspace(tmp_path)
    copy_reviews(tmp_path)
    result = run(
        [
            "build",
            "--non-interactive",
            "--skip-html",
            "--reviews",
            "reviews/sql.json",
            "--reviews",
            "reviews/sql.json",
            "--reviews",
            "reviews/dax.json",
        ],
        tmp_path,
    )
    assert result.exit_code == 0, result.output
    findings = (tmp_path / "data-docs" / "findings.md").read_text(encoding="utf-8")
    assert "| coop-sql-review | warning | 2 |" in findings
    index = (tmp_path / "data-docs" / "index.md").read_text(encoding="utf-8")
    assert "[8 review findings](findings.md)" in index


def test_check_reviews_flag_missing_file_is_exit_2(tmp_path: Path):
    # check shares _review_inputs with build: a --reviews flag path that
    # doesn't exist is a usage error (exit 2) on the command CI actually runs.
    setup_workspace(tmp_path)
    build = run(["build", "--non-interactive", "--skip-html"], tmp_path)
    assert build.exit_code == 0, build.output
    result = run(["check", "--lenient", "--reviews", "no-such.json"], tmp_path)
    assert result.exit_code == 2
    assert "no-such.json" in result.output


def test_missing_config_listed_review_degrades_to_warning(tmp_path: Path):
    setup_workspace(tmp_path)
    config = tmp_path / "coop-data-doc.yml"
    config.write_text(
        config.read_text(encoding="utf-8") + 'reviews: ["gone.json"]\n',
        encoding="utf-8",
        newline="\n",
    )
    result = run(["build", "--non-interactive", "--skip-html"], tmp_path)
    assert result.exit_code == 0, result.output
    assert "reviews_unreadable" in result.output


def test_reviews_never_affect_strict_exit(tmp_path: Path):
    # an unreadable review file is advisory: it must not trip --strict (the
    # fixture strict categories are removed first so the build is clean)
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
    (tmp_path / "corrupt.json").write_text("{ not json", encoding="utf-8")
    result = run(
        ["build", "--non-interactive", "--strict", "--skip-html", "--reviews", "corrupt.json"],
        tmp_path,
    )
    assert result.exit_code == 0, result.output


# --- determinism + check parity --------------------------------------------------


def test_two_builds_with_reviews_byte_identical(tmp_path: Path):
    ws_a = tmp_path / "a"
    ws_b = tmp_path / "b"
    ws_a.mkdir()
    ws_b.mkdir()
    for ws in (ws_a, ws_b):
        setup_workspace(ws)
        copy_reviews(ws)
        result = run(
            [
                "build",
                "--non-interactive",
                "--skip-html",
                "--reviews",
                "reviews/sql.json",
                "--reviews",
                "reviews/dax.json",
            ],
            ws,
        )
        assert result.exit_code == 0, result.output
    docs_a = tree_bytes(ws_a / "data-docs")
    docs_b = tree_bytes(ws_b / "data-docs")
    assert docs_a.keys() == docs_b.keys()
    for relative in docs_a:
        assert docs_a[relative] == docs_b[relative], f"non-deterministic: {relative}"


def test_build_without_reviews_prunes_findings_and_matches_plain_build(tmp_path: Path):
    # strictly additive: after a reviews build, a plain build removes every
    # trace and is byte-identical to a workspace that never saw reviews
    ws_reviews = tmp_path / "a"
    ws_plain = tmp_path / "b"
    ws_reviews.mkdir()
    ws_plain.mkdir()
    build_with_reviews(ws_reviews)
    assert (ws_reviews / "data-docs" / "findings.md").is_file()
    assert run(["build", "--non-interactive", "--skip-html"], ws_reviews).exit_code == 0
    assert not (ws_reviews / "data-docs" / "findings.md").exists()

    setup_workspace(ws_plain)
    assert run(["build", "--non-interactive", "--skip-html"], ws_plain).exit_code == 0
    assert tree_bytes(ws_reviews / "data-docs") == tree_bytes(ws_plain / "data-docs")


def test_check_needs_the_same_reviews_arguments(tmp_path: Path):
    from test_cli import _FULL_CACHE

    setup_workspace(tmp_path)
    # resolve the fixtures' ambiguous cross-repo links up front, as an
    # interactive session would have (mirrors test_check_lenient_tolerates_...)
    (tmp_path / ".lineage-cache.json").write_text(_FULL_CACHE, encoding="utf-8")
    copy_reviews(tmp_path)
    result = run(
        [
            "build",
            "--non-interactive",
            "--skip-html",
            "--reviews",
            "reviews/sql.json",
            "--reviews",
            "reviews/dax.json",
        ],
        tmp_path,
    )
    assert result.exit_code == 0, result.output
    # same --reviews args: fresh render matches the committed tree
    same = run(
        ["check", "--lenient", "--reviews", "reviews/sql.json", "--reviews", "reviews/dax.json"],
        tmp_path,
    )
    assert same.exit_code == 0, same.output
    # without them the trees legitimately differ (findings.md + pages) → stale
    without = run(["check", "--lenient"], tmp_path)
    assert without.exit_code == 1
    assert "findings.md" in without.output


def test_status_fresh_with_config_listed_reviews(tmp_path: Path):
    # status honors the config `reviews:` list (no flag): a build whose review
    # inputs all come from the config reads back 'up to date'. Pins the AGENTS.md
    # 'status honors the config list only (no flag)' contract, whose only caller
    # of the reviews render path is easy to drop in a refactor.
    from test_cli import _FULL_CACHE

    setup_workspace(tmp_path)
    (tmp_path / ".lineage-cache.json").write_text(_FULL_CACHE, encoding="utf-8")
    copy_reviews(tmp_path)
    config = tmp_path / "coop-data-doc.yml"
    config.write_text(
        config.read_text(encoding="utf-8") + 'reviews: ["reviews/sql.json", "reviews/dax.json"]\n',
        encoding="utf-8",
        newline="\n",
    )
    # build with NO flags — the config list supplies both review files
    build = run(["build", "--non-interactive", "--skip-html"], tmp_path)
    assert build.exit_code == 0, build.output
    fresh = run(["status"], tmp_path)
    assert fresh.exit_code == 0, fresh.output
    assert "up to date" in fresh.output

    # documented caveat: a portal built with an EXTRA --reviews flag not in the
    # config reads as stale from status (status honors the config list only)
    extra = tmp_path / "extra.json"
    extra.write_text((tmp_path / "reviews" / "sql.json").read_text(encoding="utf-8"), encoding="utf-8")
    # a distinct rule id so the extra file adds findings the config-only render won't reproduce
    extra.write_text(
        extra.read_text(encoding="utf-8").replace("SQL-SELECT-STAR", "SQL-EXTRA-ONLY"), encoding="utf-8"
    )
    rebuilt = run(["build", "--non-interactive", "--skip-html", "--reviews", "extra.json"], tmp_path)
    assert rebuilt.exit_code == 0, rebuilt.output
    stale = run(["status"], tmp_path)
    assert stale.exit_code == 0  # status reports, never fails the way check does
    assert "stale" in stale.output


# --- site nav + config round-trip -------------------------------------------------


def test_nav_gains_findings_entry_next_to_diagnostics(tmp_path: Path):
    from coop_data_doc.render.site import _nav_section

    graph = make_graph((NodeType.VIEW, "s", "v"))
    with_findings = _nav_section(graph, has_findings=True)
    assert "  - Diagnostics: diagnostics.md\n  - Findings: findings.md" in with_findings
    assert "findings.md" not in _nav_section(graph)


def test_write_mkdocs_config_detects_findings_page(tmp_path: Path):
    from coop_data_doc.render.site import write_mkdocs_config

    graph = make_graph((NodeType.VIEW, "s", "v"))
    docs = tmp_path / "docs"
    docs.mkdir()
    site = tmp_path / "site"
    config_path = write_mkdocs_config(docs, site, "Test", graph)
    assert "Findings: findings.md" not in config_path.read_text(encoding="utf-8")
    (docs / "findings.md").write_text("# Review findings — Test\n", encoding="utf-8")
    config_path = write_mkdocs_config(docs, site, "Test", graph)
    assert "Findings: findings.md" in config_path.read_text(encoding="utf-8")


def test_config_reviews_survive_config_set_round_trip(tmp_path: Path):
    setup_workspace(tmp_path)
    patch = json.dumps({"reviews": ["reviews/sql.json", "reviews/dax.json"]})
    (tmp_path / "patch.json").write_text(patch, encoding="utf-8")
    result = run(["config-set", "--from-json", "patch.json"], tmp_path)
    assert result.exit_code == 0, result.output
    from coop_data_doc.config import Config

    config = Config.load(tmp_path / "coop-data-doc.yml")
    assert config.reviews == ["reviews/sql.json", "reviews/dax.json"]
    shown = run(["show-config"], tmp_path)
    assert json.loads(shown.output)["reviews"] == ["reviews/sql.json", "reviews/dax.json"]
    # a second config-set touching another key preserves the reviews list
    (tmp_path / "patch2.json").write_text(json.dumps({"project_name": "Renamed"}), encoding="utf-8")
    result = run(["config-set", "--from-json", "patch2.json"], tmp_path)
    assert result.exit_code == 0, result.output
    config = Config.load(tmp_path / "coop-data-doc.yml")
    assert config.project_name == "Renamed"
    assert config.reviews == ["reviews/sql.json", "reviews/dax.json"]


def test_init_scaffold_has_no_reviews_block(tmp_path: Path):
    # the starter config is byte-stable: no reviews key until a user adds one
    assert run(["init"], tmp_path).exit_code == 0
    assert "reviews" not in (tmp_path / "coop-data-doc.yml").read_text(encoding="utf-8")
