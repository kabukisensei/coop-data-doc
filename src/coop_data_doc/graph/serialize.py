"""JSON round-trip for LineageGraph (Module 0).

The artifact (graph.json / manifest.json) is git-diffable: keys sorted,
edges sorted, 2-space indent, trailing newline.
"""

from __future__ import annotations

import json
from pathlib import Path

from coop_data_doc.graph.model import LineageGraph


def to_json_str(graph: LineageGraph) -> str:
    """Canonical JSON: sorted keys, sorted edges, 2-space indent."""
    payload = graph.model_dump(mode="json")
    payload["edges"] = sorted(
        payload["edges"],
        key=lambda e: (e["source_id"], e["target_id"], e["edge_type"]),
    )
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def to_json_file(graph: LineageGraph, path: Path | str) -> None:
    """Write the canonical JSON artifact (graph.json / manifest.json)."""
    Path(path).write_text(to_json_str(graph), encoding="utf-8")


def from_json_file(path: Path | str) -> LineageGraph:
    """Load a graph previously written by to_json_file."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return LineageGraph.model_validate(data)
