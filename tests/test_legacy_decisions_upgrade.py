"""DD3 upgrade regression: what v1.2.0 saved must survive the scope-aware build.

The estates under ``tests/fixtures/upgrade_from_1_2_0`` were produced by the
real v1.2.0 release (see the README there): ``lineage-cache.json`` holds the
human decisions v1.2.0 wrote through ``resolve-apply`` (no
``source_signature``), and ``published/`` is the v1.2.0 output folder with
authored Business Intent. Each test copies one estate, builds it with the
current code and checks the contract from master plan row DD3: identity
changes produce diagnostics, nothing is merged silently, and no decision or
intent is lost.

Tests marked ``xfail(strict=True)`` pin known migration bugs (described in
the PR that added this file); they start passing, and must be unmarked, once
the bug is fixed.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from coop_data_doc.cli import cli

FIXTURES = Path(__file__).parent / "fixtures" / "upgrade_from_1_2_0"

# Every decision v1.2.0 saved in mixed/lineage-cache.json, by key.
LEGACY_DECISIONS = {
    "pbi_table:sales.basket#sales.custs": "view:sales.v_customers",
    "pbi_table:sales.basket#sales.product": None,  # external
    "pbi_table:sales.customer": "view:sales.v_customers",
    "pbi_table:sales.orders": "view:sales.v_orders_2025",
    "pbi_table:sales.scratch": None,  # skip
    "pbi_table:sales.weblog": None,  # external
}
# orders has two M partitions: v1.2.0 kept one source and keyed the table
# alone; the current linker keys each partition source separately.
SPLIT_KEY = "pbi_table:sales.orders"
SAME_SHAPE_DECISIONS = {k: v for k, v in LEGACY_DECISIONS.items() if k != SPLIT_KEY}

AUTHORED_INTENT = {
    "mixed": {
        "pbi_table/sales-orders-ca4a47cd.md": "Orders combines the 2024 archive and the 2025 live partition.",
        "view/sales-v_customers-04f87d30.md": "Customer master for every sales report.",
    },
    "collision": {
        "pbi_table/sales-orders-ca4a47cd.md": "Orders combines the 2024 archive and the 2025 live partition.",
        "pbi_table/sales-legacy_customer-ed447519.md": "Archived customer list kept for audit.",
    },
}


def _estate(tmp_path: Path, name: str) -> Path:
    """Lay a v1.2.0 estate out exactly as the user's folder looked after 1.2.0."""
    source = FIXTURES / name
    root = tmp_path / name
    shutil.copytree(source, root, ignore=shutil.ignore_patterns("published", "lineage-cache.json"))
    shutil.copytree(source / "published", root / "data-docs")
    shutil.copyfile(source / "lineage-cache.json", root / ".lineage-cache.json")
    return root


def _build(root: Path):
    result = CliRunner().invoke(
        cli, ["build", "--config", str(root / "coop-data-doc.yml"), "--non-interactive", "--skip-html"]
    )
    out = root / "data-docs"
    graph = json.loads((out / "graph.json").read_text(encoding="utf-8"))
    issues = json.loads((out / "diagnostics.json").read_text(encoding="utf-8"))["issues"]
    return result, graph, issues


def _applied(graph: dict, key: str, target: str | None) -> bool:
    """The saved decision is in effect in the published graph."""
    node = graph["nodes"].get(key.split("#")[0])
    if node is None:
        return False
    metadata = node["metadata"]
    if target is None:
        return bool(metadata.get("external_source") or metadata.get("skipped"))
    resolution = metadata.get("source_resolutions", {}).get(key)
    return resolution is not None and resolution["method"] == "cache" and resolution["target"] == target


def _named(issues: list[dict], key: str, category: str | None = None) -> bool:
    """A diagnostic names this exact cache key (quoted, as the linker writes it)."""
    return any(
        repr(key) in issue["message"] and (category is None or issue["category"] == category)
        for issue in issues
    )


def _intent(page: Path) -> str:
    text = page.read_text(encoding="utf-8")
    return text.split("<!-- intent:begin -->\n", 1)[1].split("\n<!-- intent:end -->", 1)[0]


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_legacy_cache_and_intent_survive_repeated_builds(tmp_path):
    root = _estate(tmp_path, "mixed")
    legacy_cache = (root / ".lineage-cache.json").read_bytes()
    for _ in range(2):
        result, _, _ = _build(root)
        assert result.exit_code == 0, result.output
        # Committed human answers are never rewritten, re-keyed or pruned.
        assert (root / ".lineage-cache.json").read_bytes() == legacy_cache
        for page, intent in AUTHORED_INTENT["mixed"].items():
            assert _intent(root / "data-docs" / page).startswith(intent)


@pytest.mark.parametrize("builds", [1, 2])
def test_same_shape_legacy_decisions_are_applied_or_diagnosed(tmp_path, builds):
    root = _estate(tmp_path, "mixed")
    for _ in range(builds):
        result, graph, issues = _build(root)
        assert result.exit_code == 0, result.output
    for key, target in SAME_SHAPE_DECISIONS.items():
        assert _applied(graph, key, target) or _named(issues, key), key


@pytest.mark.xfail(
    strict=True,
    reason="bug: a v1.2.0 table-level key is stranded when its sources split per partition, with no diagnostic",
)
@pytest.mark.parametrize("builds", [1, 2])
def test_split_legacy_key_is_diagnosed(tmp_path, builds):
    root = _estate(tmp_path, "mixed")
    for _ in range(builds):
        result, graph, issues = _build(root)
        assert result.exit_code == 0, result.output
    assert _applied(graph, SPLIT_KEY, LEGACY_DECISIONS[SPLIT_KEY]) or _named(issues, SPLIT_KEY)


def test_legacy_decisions_are_flagged_unverified_not_dropped(tmp_path):
    root = _estate(tmp_path, "mixed")
    _build(root)
    result, graph, issues = _build(root)
    assert result.exit_code == 0, result.output
    for key, target in SAME_SHAPE_DECISIONS.items():
        assert _applied(graph, key, target), key
        assert _named(issues, key, "cache_source_unverified"), key


@pytest.mark.xfail(
    strict=True,
    reason="bug: the first build after upgrading reports every unchanged v1.2.0 source as changed",
)
def test_first_upgrade_build_does_not_report_unchanged_sources_as_changed(tmp_path):
    root = _estate(tmp_path, "mixed")
    result, graph, issues = _build(root)
    assert result.exit_code == 0, result.output
    assert not [i for i in issues if i["category"] == "cache_source_changed"]
    for key, target in SAME_SHAPE_DECISIONS.items():
        assert _applied(graph, key, target), key


@pytest.mark.parametrize(
    "builds",
    [
        1,
        pytest.param(
            2,
            marks=pytest.mark.xfail(
                strict=True,
                reason="bug: a source change found on the first build is forgotten on the next build",
            ),
        ),
    ],
)
def test_source_changed_since_legacy_answer_stays_flagged(tmp_path, builds):
    root = _estate(tmp_path, "mixed")
    table = root / "pbi/Sales.SemanticModel/definition/tables/customer.tmdl"
    table.write_text(table.read_text().replace('Item="customer"', 'Item="client"'))
    key = "pbi_table:sales.customer"
    for _ in range(builds):
        result, graph, issues = _build(root)
        assert result.exit_code == 0, result.output
    # The answer was given for sales.customer; it must not silently carry over
    # to sales.client, however many builds have run since.
    assert not _applied(graph, key, LEGACY_DECISIONS[key])
    assert _named(issues, key, "cache_source_changed")


def test_same_name_models_from_1_2_0_stop_without_touching_anything(tmp_path):
    # v1.2.0 merged Sales.SemanticModel and Archive/Sales.SemanticModel into one
    # semantic_model:sales. The current build must refuse instead of merging,
    # and leave every saved decision, page and intent block byte-identical.
    root = _estate(tmp_path, "collision")
    before = _tree_bytes(root)
    result = CliRunner().invoke(
        cli, ["build", "--config", str(root / "coop-data-doc.yml"), "--non-interactive", "--skip-html"]
    )
    assert result.exit_code == 2, result.output
    assert "identity_collision" in result.output
    assert _tree_bytes(root) == before
    for page, intent in AUTHORED_INTENT["collision"].items():
        assert _intent(root / "data-docs" / page).startswith(intent)
