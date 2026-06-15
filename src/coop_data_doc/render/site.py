"""MkDocs HTML portal generation (Module 5).

Synthesizes a Material-for-MkDocs config (dark default with toggle, offline
plugin so search works over file://) and shells out to `mkdocs build`.

Material falls back to unpkg.com for two things — the Mermaid library and
the iframe-worker shim that powers search over file:// — so both are
vendored in this package (templates/assets) and injected: Mermaid via
extra_javascript (Material skips its CDN fetch when window.mermaid is
already defined), the shim by rewriting the built HTML.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

from coop_data_doc.graph.model import LineageGraph, Node, NodeType
from coop_data_doc.render.mermaid import slug

_TABLE_TYPES = (NodeType.BRONZE_TABLE, NodeType.SILVER_TABLE, NodeType.GOLD_TABLE)
_TABLE_LAYER = {
    NodeType.BRONZE_TABLE: "bronze",
    NodeType.SILVER_TABLE: "silver",
    NodeType.GOLD_TABLE: "gold",
}
# left-nav top sections, in order
_LAYER_NAV = [("bronze", "Bronze Layer"), ("silver", "Silver Layer"), ("gold", "Gold Layer")]
# object-type subgroups within a layer, in order
_SUBGROUPS = (
    ("Stored Procedures", (NodeType.STORED_PROC,)),
    ("Tables", _TABLE_TYPES),
    ("Views", (NodeType.VIEW,)),
)
# Power BI top sections (not layered)
_PBI_NAV = [
    (
        "Semantic Models",
        [
            ("Models", NodeType.SEMANTIC_MODEL),
            ("Model Tables", NodeType.PBI_TABLE),
            ("Measures", NodeType.MEASURE),
        ],
    ),
    (
        "Reports",
        [("Reports", NodeType.REPORT), ("Pages", NodeType.REPORT_PAGE), ("Visuals", NodeType.VISUAL)],
    ),
]


def _node_layer(node: Node) -> str | None:
    """The medallion layer of a node for nav grouping, or None if unlayered."""
    if node.node_type in _TABLE_LAYER:
        return _TABLE_LAYER[node.node_type]
    if node.node_type in (NodeType.VIEW, NodeType.STORED_PROC):
        return node.metadata.get("layer")
    return None


class SiteBuildError(Exception):
    """mkdocs build failed; message carries the stderr tail."""

    pass


_MKDOCS_TEMPLATE = """\
site_name: {site_name}
docs_dir: {docs_dir}
site_dir: {site_dir}
use_directory_urls: false

theme:
  name: material
  font: false  # system fonts; no Google Fonts CDN
  palette:
    - scheme: slate
      primary: teal
      toggle:
        icon: material/weather-sunny
        name: Switch to light mode
    - scheme: default
      primary: teal
      toggle:
        icon: material/weather-night
        name: Switch to dark mode
  features:
    - navigation.sections
    - navigation.indexes
    - navigation.top
    - navigation.tracking
    - toc.follow
    - search.suggest
    - search.highlight
    - content.code.copy

plugins:
  - search
  - offline

markdown_extensions:
  - admonition
  - tables
  - pymdownx.superfences:
      custom_fences:
        - name: mermaid
          class: mermaid
          format: !!python/name:pymdownx.superfences.fence_code_format

extra_javascript:
  - assets/javascripts/vendor/mermaid.min.js

extra_css:
  - assets/stylesheets/custom.css

nav:
{nav}
"""

_VENDOR_SRC = Path(__file__).resolve().parent.parent / "templates" / "assets"
_VENDOR_REL = Path("assets") / "javascripts" / "vendor"
_CSS_REL = Path("assets") / "stylesheets"
_SHIM_URL_RE = re.compile(r'src="https://unpkg\.com/iframe-worker/shim"')

# sibling of the docs dir — mkdocs refuses a config inside docs_dir
CONFIG_NAME = ".coop-mkdocs.yml"


def _page(node: Node, node_id: str) -> str:
    return f"{node.node_type.value}/{slug(node_id)}.md"


def _nav_section(graph: LineageGraph) -> str:
    """Build a nav tree grouped by layer (Bronze/Silver/Gold) then object
    type, with Power BI and any unlayered objects as their own sections."""
    nodes = graph.nodes
    lines = ["  - Overview: index.md"]

    # layered SQL objects: Layer -> object type -> pages
    for layer, layer_title in _LAYER_NAV:
        members = sorted(nid for nid, n in nodes.items() if _node_layer(n) == layer)
        if not members:
            continue
        lines.append(f"  - {layer_title}:")
        for sub_title, types in _SUBGROUPS:
            sub = [nid for nid in members if nodes[nid].node_type in types]
            if not sub:
                continue
            lines.append(f"      - {sub_title}:")
            lines.extend(f"          - {_page(nodes[nid], nid)}" for nid in sub)

    # Power BI sections (not layered)
    for group_title, subgroups in _PBI_NAV:
        rendered: list[str] = []
        for sub_title, node_type in subgroups:
            ids = sorted(nid for nid, n in nodes.items() if n.node_type is node_type)
            if not ids:
                continue
            rendered.append(f"      - {sub_title}:")
            rendered.extend(f"          - {_page(nodes[nid], nid)}" for nid in ids)
        if rendered:
            lines.append(f"  - {group_title}:")
            lines.extend(rendered)

    # anything unlayered (views/procs no rule covered) — don't lose them
    other = sorted(
        nid
        for nid, n in nodes.items()
        if n.node_type in (NodeType.VIEW, NodeType.STORED_PROC) and _node_layer(n) is None
    )
    if other:
        lines.append("  - Other:")
        lines.extend(f"      - {_page(nodes[nid], nid)}" for nid in other)
    return "\n".join(lines)


def write_mkdocs_config(docs_dir: Path, site_dir: Path, project_name: str, graph: LineageGraph) -> Path:
    """Copy vendored assets into the docs tree and write the Material
    config as a sibling of the docs dir; returns the config path.
    """
    docs_dir = Path(docs_dir).resolve()
    vendor_dir = docs_dir / _VENDOR_REL
    vendor_dir.mkdir(parents=True, exist_ok=True)
    for asset in ("mermaid.min.js", "iframe-worker-shim.js"):
        shutil.copyfile(_VENDOR_SRC / asset, vendor_dir / asset)
    css_dir = docs_dir / _CSS_REL
    css_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_VENDOR_SRC / "custom.css", css_dir / "custom.css")
    config_path = docs_dir.parent / CONFIG_NAME
    config_path.write_text(
        _MKDOCS_TEMPLATE.format(
            site_name=project_name,
            docs_dir=docs_dir.name,
            site_dir=str(Path(site_dir).resolve()),
            nav=_nav_section(graph),
        ),
        encoding="utf-8",
        newline="\n",
    )
    return config_path


def localize_shim(site_dir: Path) -> int:
    """Rewrite the unpkg iframe-worker shim reference to the local copy.

    Material hardcodes the unpkg URL when the offline plugin is active;
    the shim is only fetched over file://, so without this rewrite search
    would silently require network access.
    """
    site_dir = Path(site_dir)
    shim = site_dir / _VENDOR_REL / "iframe-worker-shim.js"
    if not shim.is_file():
        return 0
    rewritten = 0
    for html_file in sorted(site_dir.rglob("*.html")):
        text = html_file.read_text(encoding="utf-8")
        depth = len(html_file.parent.relative_to(site_dir).parts)
        local = "../" * depth + shim.relative_to(site_dir).as_posix()
        new_text = _SHIM_URL_RE.sub(f'src="{local}"', text)
        if new_text != text:
            html_file.write_text(new_text, encoding="utf-8", newline="\n")
            rewritten += 1
    return rewritten


def build_site(config_path: Path, site_dir: Path) -> None:
    """Run `mkdocs build` and localize the search worker shim afterwards."""
    completed = subprocess.run(
        [sys.executable, "-m", "mkdocs", "build", "-f", str(config_path)],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        tail = "\n".join(completed.stderr.splitlines()[-15:])
        raise SiteBuildError(f"mkdocs build failed:\n{tail}")
    localize_shim(site_dir)
