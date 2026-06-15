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

from coop_data_doc.graph.model import LineageGraph, Node, NodeType, normalize_identifier
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
{theme_brand}  palette:
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
  - assets/stylesheets/brand.css

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


def _navkey(title: str) -> str:
    """Double-quote a nav section title so special chars (`:` `+` etc. in
    model/report names) can't break the YAML."""
    return '"' + title.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _grouped_section(nodes, container, title, children) -> list[str]:
    """Nav for `container` nodes (semantic_model / report) with their child
    objects nested underneath, keyed by the child's schema_name == the
    container's normalized name. Children whose container is missing get
    their own subsection keyed by the bare name."""
    # group child ids by their container key
    grouped: dict[str, dict[str, list[str]]] = {}
    for nid, node in nodes.items():
        for _child_title, child_type in children:
            if node.node_type is child_type:
                grouped.setdefault(node.schema_name, {}).setdefault(child_type.value, []).append(nid)

    containers = sorted(
        (nid for nid, n in nodes.items() if n.node_type is container),
        key=lambda nid: nodes[nid].display.lower(),
    )
    if not containers and not grouped:
        return []

    def block(section_title: str, key: str, container_id: str | None) -> list[str]:
        out = [f"      - {_navkey(section_title)}:"]
        if container_id is not None:
            out.append(f"          - Overview: {_page(nodes[container_id], container_id)}")
        for child_title, child_type in children:
            ids = sorted(grouped.get(key, {}).get(child_type.value, []))
            if not ids:
                continue
            out.append(f"          - {child_title}:")
            out.extend(f"              - {_page(nodes[nid], nid)}" for nid in ids)
        return out

    lines = [f"  - {title}:"]
    seen: set[str] = set()
    for cid in containers:
        # children carry the container's normalized name in schema_name
        key = normalize_identifier(nodes[cid].name)
        seen.add(key)
        lines.extend(block(nodes[cid].display, key, cid))
    for key in sorted(set(grouped) - seen):  # children with no container node
        lines.extend(block(key, key, None))
    return lines


def _nav_section(graph: LineageGraph) -> str:
    """Build a nav tree grouped by layer (Bronze/Silver/Gold) then object
    type, with Power BI and any unlayered objects as their own sections."""
    nodes = graph.nodes
    lines = ["  - Overview: index.md", "  - Diagnostics: diagnostics.md"]

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

    # Power BI: nest each semantic model's tables + measures under the model,
    # and each report's pages + visuals under the report (a flat list of
    # hundreds of measures/visuals is unusable).
    lines.extend(
        _grouped_section(
            nodes,
            container=NodeType.SEMANTIC_MODEL,
            title="Semantic Models",
            children=(("Tables", NodeType.PBI_TABLE), ("Measures", NodeType.MEASURE)),
        )
    )
    lines.extend(
        _grouped_section(
            nodes,
            container=NodeType.REPORT,
            title="Reports",
            children=(("Pages", NodeType.REPORT_PAGE), ("Visuals", NodeType.VISUAL)),
        )
    )

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


def _apply_branding(docs_dir: Path, branding, config_dir: Path | None) -> str:
    """Copy logo/favicon into the site, write brand.css, and return the YAML
    lines to splice under `theme:` (empty string when no logo/favicon)."""
    css_dir = docs_dir / _CSS_REL
    css_dir.mkdir(parents=True, exist_ok=True)
    theme_lines: list[str] = []
    brand_css = "/* brand colors — set branding.primary_color / accent_color in the config */\n"

    if branding is not None:
        images = docs_dir / "assets" / "images"
        base = Path(config_dir) if config_dir else docs_dir

        def copy_image(rel: str, stem: str) -> str | None:
            src = (base / rel).expanduser()
            if not src.is_file():
                return None
            images.mkdir(parents=True, exist_ok=True)
            dest = images / f"{stem}{src.suffix.lower()}"
            shutil.copyfile(src, dest)
            return dest.relative_to(docs_dir).as_posix()

        if branding.logo:
            logo_rel = copy_image(branding.logo, "logo")
            if logo_rel:
                theme_lines.append(f"  logo: {logo_rel}")
        fav = branding.favicon or branding.logo
        if fav:
            fav_rel = copy_image(fav, "favicon")
            if fav_rel:
                theme_lines.append(f"  favicon: {fav_rel}")

        primary = branding.primary_color
        accent = branding.accent_color
        if primary or accent:
            decls = []
            if primary:
                decls.append(f"  --md-primary-fg-color: {primary};")
            if accent:
                decls.append(f"  --md-accent-fg-color: {accent};")
            block = "\n".join(decls)
            brand_css = (
                "/* generated from branding.* in coop-data-doc.yml */\n"
                f':root, [data-md-color-scheme="default"], [data-md-color-scheme="slate"] {{\n{block}\n}}\n'
            )

    (css_dir / "brand.css").write_text(brand_css, encoding="utf-8", newline="\n")
    return ("\n".join(theme_lines) + "\n") if theme_lines else ""


def write_mkdocs_config(
    docs_dir: Path,
    site_dir: Path,
    project_name: str,
    graph: LineageGraph,
    branding=None,
    config_dir: Path | None = None,
) -> Path:
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
    theme_brand = _apply_branding(docs_dir, branding, config_dir)
    config_path = docs_dir.parent / CONFIG_NAME
    config_path.write_text(
        _MKDOCS_TEMPLATE.format(
            site_name=project_name,
            docs_dir=docs_dir.name,
            site_dir=str(Path(site_dir).resolve()),
            theme_brand=theme_brand,
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
