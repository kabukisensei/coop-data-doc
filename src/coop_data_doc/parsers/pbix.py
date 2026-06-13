"""Best-effort .pbix extraction (Module 3).

A .pbix is a zip. Two members are recoverable offline:
- Report/Layout — UTF-16-LE JSON, same shape as legacy report.json
- DataMashup   — wraps a nested zip holding Formulas/Section1.m (M code)

The compiled DataModel is proprietary; when present without recoverable
M code we emit an opaque model node and advise saving as PBIP. Nothing in
here may raise on malformed input — every failure becomes a warning.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import PurePosixPath

from coop_data_doc.config import ParseWarning
from coop_data_doc.crawler import FileEntry
from coop_data_doc.graph.model import (
    Edge,
    EdgeType,
    LineageGraph,
    Node,
    NodeType,
    normalize_identifier,
)
from coop_data_doc.parsers.pbir import parse_layout_json
from coop_data_doc.parsers.tmdl import _attach_partition_source

_SHARED_RE = re.compile(r'\bshared\s+(?:#"([^"]+)"|([\w.]+))\s*=\s*(.*?);\s*(?=\bshared\b|\Z)', re.S)

PBIP_ADVICE = "open in Power BI Desktop and save as a .pbip project for full lineage"


def _extract_mashup_m(blob: bytes) -> str | None:
    start = blob.find(b"PK\x03\x04", 1)
    if start == -1:
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(blob[start:])) as inner:
            for name in inner.namelist():
                if name.endswith("Section1.m"):
                    return inner.read(name).decode("utf-8-sig", errors="replace")
    except (zipfile.BadZipFile, OSError, ValueError):
        return None
    return None


def parse_pbix(entries: list[FileEntry], graph: LineageGraph) -> list[ParseWarning]:
    """Best-effort extraction from .pbix archives; warns instead of raising
    on anything malformed or proprietary.
    """
    warnings: list[ParseWarning] = []
    for entry in sorted(entries, key=lambda e: e.path):
        stem = PurePosixPath(entry.path).stem
        try:
            archive = zipfile.ZipFile(entry.abs_path)
        except (zipfile.BadZipFile, OSError):
            warnings.append(
                ParseWarning(
                    file=entry.path,
                    message=f"not a readable .pbix archive; {PBIP_ADVICE}",
                    category="pbix_unreadable",
                )
            )
            continue
        with archive:
            names = set(archive.namelist())

            if "Report/Layout" in names:
                try:
                    raw = archive.read("Report/Layout").decode("utf-16-le", errors="replace").lstrip("﻿")
                    warnings += parse_layout_json(json.loads(raw), stem, entry.path, graph)
                except (json.JSONDecodeError, KeyError, ValueError):
                    warnings.append(
                        ParseWarning(
                            file=entry.path,
                            message="Report/Layout could not be decoded",
                            category="pbix_layout_parse",
                        )
                    )

            tables_found = False
            if "DataMashup" in names:
                try:
                    section = _extract_mashup_m(archive.read("DataMashup"))
                except (KeyError, OSError):
                    section = None
                if section:
                    model_node = graph.add_node(
                        Node(
                            id=Node.make_id(NodeType.SEMANTIC_MODEL, "", stem),
                            node_type=NodeType.SEMANTIC_MODEL,
                            name=normalize_identifier(stem),
                            source_file=entry.path,
                            metadata={"from_pbix": True},
                        )
                    )
                    for match in _SHARED_RE.finditer(section):
                        table_name = match.group(1) or match.group(2)
                        expression = match.group(3)
                        table_node = graph.add_node(
                            Node(
                                id=Node.make_id(NodeType.PBI_TABLE, stem, table_name),
                                node_type=NodeType.PBI_TABLE,
                                name=normalize_identifier(table_name),
                                schema_name=normalize_identifier(stem),
                                source_file=entry.path,
                            )
                        )
                        graph.add_edge(
                            Edge(
                                source_id=table_node.id,
                                target_id=model_node.id,
                                edge_type=EdgeType.FEEDS,
                                evidence=entry.path,
                            )
                        )
                        _attach_partition_source(table_node, expression, entry.path, warnings)
                        tables_found = True

            if "DataModel" in names and not tables_found:
                graph.add_node(
                    Node(
                        id=Node.make_id(NodeType.SEMANTIC_MODEL, "", stem),
                        node_type=NodeType.SEMANTIC_MODEL,
                        name=normalize_identifier(stem),
                        source_file=entry.path,
                        metadata={"pbix_model_opaque": True},
                    )
                )
                warnings.append(
                    ParseWarning(
                        file=entry.path,
                        message=f"compiled model is not extractable; {PBIP_ADVICE}",
                        category="pbix_opaque_model",
                    )
                )
    return warnings
