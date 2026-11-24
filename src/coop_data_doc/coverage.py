"""Scoped offline evidence: declarations never upgrade observed gaps to certainty."""

from __future__ import annotations

import json
import re
from pathlib import Path

from coop_data_doc.config import Config, ParseWarning
from coop_data_doc.crawler import FileInventory, FileKind
from coop_data_doc.graph.model import LineageGraph, Node

# These omissions can remove previously documented objects. Refuse publication
# even in non-strict mode, rather than interpret inaccessible source as deletion.
OMISSION_CATEGORIES = frozenset({"crawl_incomplete", "file_unreadable", "file_too_large", "symlink_escape"})

# The manifest `snapshot` leaves in a live source's folder. Named here, not imported:
# the offline pipeline never loads the database-connected module.
_SNAPSHOT_MANIFEST = ".coop-data-doc-snapshot.json"
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def live_snapshot_days(config: Config) -> dict[str, str]:
    """The day each live source's repo was last read from its dev database, by repo key.

    The day only (``YYYY-MM-DD``, UTC), so a second read on the same day leaves the
    docs byte-identical. A repo with no readable manifest is left out: an unknown age
    is never shown as a date. Two live sources in one repo show the older read."""
    days: dict[str, str] = {}
    for name in sorted(config.live_sources):
        key = config.live_sources[name].repo
        repo = config.repos.get(key)
        if repo is None:
            continue
        path = (config.base_dir / Path(repo.path).expanduser()) / _SNAPSHOT_MANIFEST
        try:
            taken = json.loads(path.read_text(encoding="utf-8")).get("taken_at")
        except (OSError, ValueError, AttributeError):
            continue
        day = str(taken)[:10] if isinstance(taken, str) else ""
        if _DAY.fullmatch(day):
            days[key] = min(day, days.get(key, day))
    return days


def source_coverage(config: Config, inventory: FileInventory, warnings: list[ParseWarning]) -> dict:
    sources = {}
    snapshot_days = live_snapshot_days(config)
    for key in sorted(set(config.repos) | {"sql", "powerbi"}):
        entries = [entry for entry in inventory.entries if entry.repo_key == key]
        if key in {"sql", "powerbi"} and key not in config.repos:
            entries = [
                entry for entry in inventory.entries if (entry.kind == FileKind.SQL_FILE) == (key == "sql")
            ]
        declaration = config.coverage.get(key)
        repo = config.repos.get(key)
        sources[key] = {
            "declared": declaration.state if declaration else "unknown",
            "scope": declaration.scope if declaration else "",
            "availability": "local" if repo or entries else "missing",
            "files": len(entries),
            "include": sorted(repo.include) if repo else [],
            "exclude": sorted(repo.exclude) if repo else [],
        }
        if key in snapshot_days:
            # only on a live source's repo, so every other estate's graph is unchanged
            sources[key]["live_snapshot"] = snapshot_days[key]
    return {
        "sources": sources,
        "layers": {
            name: {
                "declared": config.coverage.get(f"layer:{name}").state
                if config.coverage.get(f"layer:{name}")
                else "unknown",
                "configured": f"layer:{name}" in config.coverage,
                "scope": config.coverage.get(f"layer:{name}").scope
                if config.coverage.get(f"layer:{name}")
                else "",
            }
            for name in ("bronze", "silver", "gold")
        },
        "include_schemas": sorted(config.include_schemas),
        "ignore_schemas": sorted(config.ignore_schemas),
        "observed": "degraded" if any(w.category in OMISSION_CATEGORIES for w in warnings) else "scanned",
    }


def evidence_summary(graph: LineageGraph, node: Node | None = None) -> dict:
    """Empty traversal results describe only known edges, never estate-wide absence."""
    states = set()
    # A clean report/measure cannot establish complete impact while another
    # part of its selected estate has unresolved or opaque upstream evidence.
    nodes = graph.nodes.values()
    for item in nodes:
        meta = item.metadata
        if not item.source_file and item.node_type.value in {
            "bronze_table",
            "silver_table",
            "gold_table",
            "view",
            "stored_proc",
        }:
            states.add("unknown")
        if meta.get("external_source"):
            states.add("external")
        if any(
            meta.get(key)
            for key in (
                "unresolved",
                "skipped",
                "partition_source_unresolved",
                "declared_model_unresolved",
                "pbix_model_opaque",
            )
        ):
            states.add("unresolved")
        if any(r.get("method") == "fuzzy" for r in meta.get("source_resolutions", {}).values()):
            states.add("partial")
        if (
            any(
                meta.get(key)
                for key in (
                    "dynamic_sql_untraced",
                    "dax_refs_heuristic",
                    "columns_unresolved",
                    "cache_source_unverified",
                )
            )
            or meta.get("parse_quality") == "regex_fallback"
        ):
            states.add("partial")
    coverage = graph.coverage
    sources = coverage.get("sources", {})
    if not sources or any(s.get("declared", "unknown") == "unknown" for s in sources.values()):
        states.add("unknown")
    for layer in coverage.get("layers", {}).values():
        if layer.get("configured") and layer.get("declared") != "complete":
            states.add(layer.get("declared", "unknown"))
    for source in sources.values():
        if not source.get("files") and source.get("availability") == "local":
            states.add("unknown")
        if source.get("availability") == "missing" or source.get("declared") == "missing":
            states.add("missing")
        if source.get("declared") in {"partial", "external"}:
            states.add(source["declared"])
    if coverage.get("observed") in {"degraded", "incomplete"}:
        states.add("partial")
    complete = bool(sources) and not states and coverage.get("observed") == "scanned"
    state = next(
        (s for s in ("unresolved", "missing", "partial", "unknown", "external") if s in states),
        "complete" if complete else "unknown",
    )
    return {
        "state": state,
        "states": sorted(states) if states else [state],
        "complete": complete,
        "impact_scope": "observed_graph",
        "empty_result_means": "no observed links; does not verify absence outside the declared and parsed scope",
        "coverage": coverage or {"observed": "unknown"},
        "provenance": {
            "origin": "local_source" if node.source_file else "declared_reference",
            "source_file": node.source_file,
            "metadata": node.metadata,
        }
        if node
        else {},
    }


def coverage_markdown(graph: LineageGraph) -> str:
    from coop_data_doc.render.markdown import _cell

    lines = [
        "## Evidence coverage",
        "",
        "Lineage describes observed local files. Empty links do not prove zero impact.",
        "",
        "| Source / layer | Declared coverage | Availability / scope |",
        "| --- | --- | --- |",
    ]
    for key, source in sorted(graph.coverage.get("sources", {}).items()):
        read = source.get("live_snapshot")
        scope = _cell(source.get("scope", ""))
        if read:
            scope = f"{scope}; read from the dev database on {_cell(read)}".lstrip("; ")
        lines.append(
            f"| {_cell(key)} | {_cell(source['declared'])} | {_cell(source['availability'])}: {scope} |"
        )
    for name, layer in sorted(graph.coverage.get("layers", {}).items()):
        lines.append(
            f"| layer:{_cell(name)} | {_cell(layer['declared'])} | {_cell(layer.get('scope', ''))} |"
        )
    if not graph.coverage:
        lines.append("| estate | unknown | legacy graph without coverage evidence |")
    return "\n".join(lines) + "\n"
