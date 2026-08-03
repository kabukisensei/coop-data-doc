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
from collections.abc import Callable
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

_SHARED_RE = re.compile(r'\bshared\s+(?:#"([^"]+)"|([\w.]+))\s*=\s*(.*?);\s*(?=\bshared\b|\Z)', re.DOTALL)

PBIP_ADVICE = "open in Power BI Desktop and save as a .pbip project for full lineage"

# refuse to decompress an absurdly large member — zip-bomb / DoS guard
_MAX_MEMBER_BYTES = 200 * 1024 * 1024


def _safe_read(archive: zipfile.ZipFile, name: str) -> bytes | None:
    """Read a zip member only if its declared uncompressed size is sane."""
    try:
        if archive.getinfo(name).file_size > _MAX_MEMBER_BYTES:
            return None
        return archive.read(name)
    except (KeyError, zipfile.BadZipFile, OSError):
        return None


def _extract_mashup_m(blob: bytes) -> str | None:
    start = blob.find(b"PK\x03\x04", 1)
    if start == -1:
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(blob[start:])) as inner:
            for info in inner.infolist():
                if info.filename.endswith("Section1.m"):
                    if info.file_size > _MAX_MEMBER_BYTES:
                        return None
                    return inner.read(info).decode("utf-8-sig", errors="replace")
    except (zipfile.BadZipFile, OSError, ValueError):
        return None
    return None


def parse_pbix(
    entries: list[FileEntry],
    graph: LineageGraph,
    *,
    on_file: Callable[..., None] | None = None,
) -> list[ParseWarning]:
    """Best-effort extraction from .pbix archives; warns instead of raising
    on anything malformed or proprietary.

    ``on_file`` (optional) is called once per entry for progress reporting.
    """
    warnings: list[ParseWarning] = []
    for entry in sorted(entries, key=lambda e: e.path):
        if on_file:
            on_file(entry.path)
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
                layout_bytes = _safe_read(archive, "Report/Layout")
                try:
                    if layout_bytes is None:
                        raise ValueError("oversized or unreadable Report/Layout")
                    raw = layout_bytes.decode("utf-16-le", errors="replace").lstrip("﻿")
                    layout = json.loads(raw)
                    if not isinstance(layout, dict):
                        # valid JSON but not a layout object — same degradation
                        # as an undecodable layout (hard rule 3, issue #41);
                        # the raise funnels into the except just below
                        raise ValueError("Report/Layout is not a JSON object")  # noqa: TRY004
                    warnings += parse_layout_json(layout, stem, entry.path, graph)
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
                mashup_bytes = _safe_read(archive, "DataMashup")
                section = _extract_mashup_m(mashup_bytes) if mashup_bytes is not None else None
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
