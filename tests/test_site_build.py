"""Integration coverage for the HTML half of `build`: a REAL `mkdocs build`
(no mocked subprocess) over a tiny docs tree, the localize_shim file:// rewrite
that keeps offline search network-free (hard rule 2), and the SiteBuildError
friendly-failure path. Everything else in the suite runs --skip-html, so this
file is the only guard against a mkdocs/Material upgrade silently breaking the
portal build or the unpkg-shim localization."""

from pathlib import Path

import pytest

pytest.importorskip("mkdocs")

from coop_data_doc.graph import Edge, EdgeType, LineageGraph, Node, NodeType
from coop_data_doc.render.markdown import render_markdown
from coop_data_doc.render.site import SiteBuildError, build_site, localize_shim, write_mkdocs_config

_SHIM_REL = "assets/javascripts/vendor/iframe-worker-shim.js"


def make_node(node_type, schema, name, **kwargs):
    return Node(
        id=Node.make_id(node_type, schema, name),
        node_type=node_type,
        name=name,
        schema_name=schema,
        **kwargs,
    )


def tiny_graph() -> LineageGraph:
    """Smallest graph that still populates the nav: one layered gold table
    feeding one semantic-model table."""
    g = LineageGraph()
    gold = g.add_node(
        make_node(NodeType.GOLD_TABLE, "dbo", "fact_sales", source_file="t.sql", metadata={"layer": "gold"})
    )
    model = g.add_node(make_node(NodeType.SEMANTIC_MODEL, "", "sales", display_name="Sales"))
    pbit = g.add_node(make_node(NodeType.PBI_TABLE, "sales", "fact_sales", display_name="fact_sales"))
    g.add_edge(Edge(source_id=gold.id, target_id=pbit.id, edge_type=EdgeType.FEEDS))
    g.add_edge(Edge(source_id=pbit.id, target_id=model.id, edge_type=EdgeType.FEEDS))
    return g


def build_docs_tree(tmp_path: Path) -> tuple[Path, Path]:
    """Render the tiny graph to markdown and return (docs_dir, site_dir)."""
    docs = tmp_path / "data-docs"
    render_markdown(tiny_graph(), docs, "Tiny Estate")
    # the nav references diagnostics.md (written by the CLI in a real build)
    (docs / "diagnostics.md").write_text("# Diagnostics\n", encoding="utf-8", newline="\n")
    return docs, tmp_path / "site"


def test_real_mkdocs_build_produces_offline_site(tmp_path: Path):
    docs, site = build_docs_tree(tmp_path)
    config_path = write_mkdocs_config(docs, site, "Tiny Estate", tiny_graph())

    build_site(config_path, site)  # the real `mkdocs build` — must not raise

    assert (site / "index.html").is_file()
    assert (site / _SHIM_REL).is_file()  # vendored shim copied into the site
    html_files = sorted(site.rglob("*.html"))
    assert html_files
    pages = {p: p.read_text(encoding="utf-8") for p in html_files}
    # hard rule 2: the built HTML must work over file:// with no network.
    # Material hardcodes the unpkg.com search-worker shim; localize_shim must
    # have rewritten every occurrence to the vendored local copy.
    assert not any('src="https://unpkg.com' in text for text in pages.values())
    assert any("iframe-worker-shim.js" in text for text in pages.values())
    # object pages made it into the site
    assert any("fact_sales" in p.name for p in html_files)


def test_localize_shim_rewrites_relative_to_depth(tmp_path: Path):
    site = tmp_path / "site"
    shim = site / _SHIM_REL
    shim.parent.mkdir(parents=True)
    shim.write_text("// vendored shim\n", encoding="utf-8")
    unpkg = '<script src="https://unpkg.com/iframe-worker/shim"></script>'
    (site / "index.html").write_text(f"<html>{unpkg}</html>", encoding="utf-8")
    deep = site / "gold_table" / "page.html"
    deep.parent.mkdir(parents=True)
    deep.write_text(f"<html>{unpkg}</html>", encoding="utf-8")
    (site / "plain.html").write_text("<html>no shim here</html>", encoding="utf-8")

    assert localize_shim(site) == 2  # only the two files carrying the unpkg src

    assert f'src="{_SHIM_REL}"' in (site / "index.html").read_text(encoding="utf-8")
    assert f'src="../{_SHIM_REL}"' in deep.read_text(encoding="utf-8")  # depth-aware relative path
    assert "unpkg.com" not in deep.read_text(encoding="utf-8")


def test_localize_shim_noop_without_vendored_shim(tmp_path: Path):
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text('<script src="https://unpkg.com/iframe-worker/shim">', encoding="utf-8")
    assert localize_shim(site) == 0  # no vendored shim -> nothing rewritten


@pytest.mark.parametrize("with_progress", [False, True])
def test_sabotaged_config_raises_site_build_error_with_stderr_tail(tmp_path: Path, with_progress: bool):
    # both subprocess paths (quiet run and -v streaming with on_page) must
    # surface mkdocs' failure as a SiteBuildError carrying the output tail,
    # which cli.main() prints as a friendly one-liner
    config_path = tmp_path / ".coop-mkdocs.yml"
    config_path.write_text("site_name: x\ndocs_dir: does-not-exist\n", encoding="utf-8")
    on_page = (lambda *a: None) if with_progress else None
    with pytest.raises(SiteBuildError) as excinfo:
        build_site(config_path, tmp_path / "site", on_page=on_page)
    message = str(excinfo.value)
    assert message.startswith("mkdocs build failed:")
    assert "docs_dir" in message  # the mkdocs error tail names the real problem
