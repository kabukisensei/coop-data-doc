"""MkDocs HTML portal generation (Module 5).

Synthesizes a Material-for-MkDocs config (dark default with toggle, offline
plugin so search works over file://) and shells out to `mkdocs build`.

Material falls back to unpkg.com for the iframe-worker shim that powers
search over file://, so it is vendored in this package (templates/assets)
and injected by rewriting the built HTML.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from coop_data_doc.graph.model import EdgeType, LineageGraph, Node, NodeType, normalize_identifier
from coop_data_doc.render.paths import slug

_MAX_BRAND_BYTES = 8 * 1024 * 1024  # don't copy oversized logo/favicon files

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
  - md_in_html
  - pymdownx.superfences

extra_javascript:
  - assets/javascripts/vendor/doc-tree.js

extra_css:
  - assets/stylesheets/custom.css
  - assets/stylesheets/brand.css

nav:
{nav}
"""

_VENDOR_SRC = Path(__file__).resolve().parent.parent / "templates" / "assets"
_BRAND_SRC = _VENDOR_SRC / "brand"  # bundled default Cooptimize logo.png / favicon.png
_VENDOR_REL = Path("assets") / "javascripts" / "vendor"
_CSS_REL = Path("assets") / "stylesheets"
_SHIM_URL_RE = re.compile(r'src="https://unpkg\.com/iframe-worker/shim"')

# sibling of the docs dir — mkdocs refuses a config inside docs_dir
CONFIG_NAME = ".coop-mkdocs.yml"


def _page(node: Node, node_id: str) -> str:
    return f"{node.node_type.value}/{slug(node_id)}.md"


def _navkey(title: str) -> str:
    """Double-quote a nav section title so special chars (`:` `+`, and control
    chars like newlines/tabs that can appear in bracketed SQL identifiers)
    can't break the YAML. Mirrors markdown._quote's escaping."""
    escaped = (
        title.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return '"' + escaped + '"'


def _grouped_section(nodes, container, title, children, extra_groups=()) -> list[str]:
    """Nav for `container` nodes (semantic_model) with their child objects nested
    underneath, keyed by the child's schema_name == the container's normalized
    name. Children whose container is missing get their own subsection keyed by
    the bare name.

    ``extra_groups`` is an optional list of ``(subsection_title, {container_key:
    [node_id, ...]})`` rendered as additional labelled leaf-link subsections
    under each container — used to nest each report under every model it draws
    from."""
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
    extra_keys = {key for _title, members in extra_groups for key in members}
    if not containers and not grouped and not extra_keys:
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
        for sub_title, members in extra_groups:
            ids = sorted(members.get(key, []), key=lambda nid: nodes[nid].display.lower())
            if not ids:
                continue
            out.append(f"          - {sub_title}:")
            # labelled links: a report's display name isn't its H1 page title key
            out.extend(
                f"              - {_navkey(nodes[nid].display)}: {_page(nodes[nid], nid)}" for nid in ids
            )
        return out

    lines = [f"  - {title}:"]
    seen: set[str] = set()
    for cid in containers:
        # children carry the container's normalized name in schema_name
        key = normalize_identifier(nodes[cid].name)
        seen.add(key)
        lines.extend(block(nodes[cid].display, key, cid))
    for key in sorted((set(grouped) | extra_keys) - seen):  # children with no container node
        lines.extend(block(key, key, None))
    return lines


def _reports_by_model(graph: LineageGraph) -> dict[str, list[str]]:
    """Map each semantic-model key -> the report ids that draw from it (via the
    ``model --feeds--> report`` edges from link_reports_to_models). A report
    drawing from several models is listed under each. Reports with no model link
    don't appear in the nav (there's no separate top-level Reports section) —
    they still have a page, reachable via search and the model's lineage."""
    nodes = graph.nodes
    by_model: dict[str, list[str]] = {}
    for edge in graph.edges:
        if edge.edge_type is not EdgeType.FEEDS:
            continue
        src, tgt = nodes.get(edge.source_id), nodes.get(edge.target_id)
        if src is None or tgt is None:
            continue
        if src.node_type is NodeType.SEMANTIC_MODEL and tgt.node_type is NodeType.REPORT:
            by_model.setdefault(normalize_identifier(src.name), []).append(tgt.id)
    return {key: sorted(set(ids)) for key, ids in by_model.items()}


def _nav_section(graph: LineageGraph) -> str:
    """Build a nav tree grouped by layer (Bronze/Silver/Gold) then object
    type, with Power BI and any unlayered objects as their own sections."""
    nodes = graph.nodes
    lines = ["  - Overview: index.md", "  - Diagnostics: diagnostics.md"]

    # layered SQL objects: Layer -> schema -> object type -> pages
    for layer, layer_title in _LAYER_NAV:
        members = sorted(nid for nid, n in nodes.items() if _node_layer(n) == layer)
        if not members:
            continue
        lines.append(f"  - {layer_title}:")
        by_schema: dict[str, list[str]] = {}
        for nid in members:  # members already id-sorted, so each list stays sorted
            by_schema.setdefault(nodes[nid].schema_name, []).append(nid)
        for schema in sorted(by_schema):
            lines.append(f"      - {_navkey(schema or '(no schema)')}:")
            for sub_title, types in _SUBGROUPS:
                sub = [nid for nid in by_schema[schema] if nodes[nid].node_type in types]
                if not sub:
                    continue
                lines.append(f"          - {sub_title}:")
                lines.extend(f"              - {_page(nodes[nid], nid)}" for nid in sub)

    # Power BI: nest each semantic model's tables + measures under the model,
    # and each report under every model it draws from (a flat list of hundreds
    # of measures is unusable, and reports belong with their model). There's no
    # separate top-level Reports section.
    lines.extend(
        _grouped_section(
            nodes,
            container=NodeType.SEMANTIC_MODEL,
            title="Semantic Models",
            children=(("Tables", NodeType.PBI_TABLE), ("Measures", NodeType.MEASURE)),
            extra_groups=[("Reports", _reports_by_model(graph))],
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

        def _copy(src: Path, stem: str) -> str | None:
            if not src.is_file() or src.stat().st_size > _MAX_BRAND_BYTES:
                return None  # missing, or guard against huge/accidental files
            images.mkdir(parents=True, exist_ok=True)
            dest = images / f"{stem}{src.suffix.lower()}"
            shutil.copyfile(src, dest)
            return dest.relative_to(docs_dir).as_posix()

        def copy_image(rel: str, stem: str) -> str | None:
            return _copy((base / rel).expanduser(), stem)

        # logo: explicit config path, else the bundled Cooptimize default
        logo_rel = copy_image(branding.logo, "logo") if branding.logo else None
        if logo_rel is None:
            logo_rel = _copy(_BRAND_SRC / "logo.png", "logo")
        if logo_rel:
            theme_lines.append(f"  logo: {logo_rel}")
        # favicon: explicit, else the configured logo, else the bundled default
        fav = branding.favicon or branding.logo
        fav_rel = copy_image(fav, "favicon") if fav else None
        if fav_rel is None:
            fav_rel = _copy(_BRAND_SRC / "favicon.png", "favicon")
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
    for asset in ("doc-tree.js", "iframe-worker-shim.js"):
        shutil.copyfile(_VENDOR_SRC / asset, vendor_dir / asset)
    css_dir = docs_dir / _CSS_REL
    css_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_VENDOR_SRC / "custom.css", css_dir / "custom.css")
    theme_brand = _apply_branding(docs_dir, branding, config_dir)
    config_path = docs_dir.parent / CONFIG_NAME
    config_path.write_text(
        _MKDOCS_TEMPLATE.format(
            # JSON-quote scalars: a project name with ':' / '#' / leading '-'
            # (all valid names) would otherwise produce invalid YAML
            site_name=json.dumps(project_name),
            docs_dir=json.dumps(docs_dir.name),
            site_dir=json.dumps(str(Path(site_dir).resolve())),
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


def build_site(config_path: Path, site_dir: Path, *, on_page: Callable[..., None] | None = None) -> None:
    """Run `mkdocs build` and localize the search worker shim afterwards.

    ``on_page`` (optional) is called once per HTML page mkdocs builds, for a
    progress bar. Supplying it runs mkdocs verbosely and streams its output to
    count page events; without it mkdocs runs quietly as before.
    """
    base = [sys.executable, "-m", "mkdocs", "build", "-f", str(config_path)]
    if on_page is None:
        completed = subprocess.run(base, capture_output=True, text=True)
        if completed.returncode != 0:
            tail = "\n".join(completed.stderr.splitlines()[-15:])
            raise SiteBuildError(f"mkdocs build failed:\n{tail}")
    else:
        # `-v` makes mkdocs log "Building page <path>" once per page; stream it.
        proc = subprocess.Popen(
            [*base, "-v"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )
        captured: list[str] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            captured.append(line)
            if "Building page " in line:
                on_page()
        if proc.wait() != 0:
            # drop the DEBUG flood `-v` adds so the real error stays legible
            meaningful = [ln for ln in captured if not ln.lstrip().startswith("DEBUG")]
            tail = "".join((meaningful or captured)[-15:]).strip()
            raise SiteBuildError(f"mkdocs build failed:\n{tail}")
    localize_shim(site_dir)
