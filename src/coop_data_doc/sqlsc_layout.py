"""Check SQL objects scripted by coop-sql-source-control against their file names.

In a repo that ``sqlsc`` keeps (a ``sqlsc.yml`` names its scripts folder), every
object lives in ``<scripts>/<Views|Stored Procedures|Functions|Triggers|Tables>/
<schema>.<name>.sql`` (``%XX`` escapes), and that file name is the database's own
name for the object. A module renamed with ``sp_rename`` keeps its old name in its
stored ``CREATE``; sqlsc releases before 1.0.4 scripted that text as it was, so the
file documents the old name. Each such file becomes an ``sqlsc_declaration_mismatch``
warning naming both.

The object stays documented under the name its ``CREATE`` declares: the file name
is evidence, not lineage, so nothing is renamed or redirected (hard rule 4: never
guess lineage), and a reference to the old name stays unresolved when nothing of
that name exists. Outside an sqlsc repo a file name means nothing and is not read.
Offline: only files under the configured repos and their ``sqlsc.yml`` are read.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from coop_data_doc.catalog_deps import SQL_NODE_TYPES
from coop_data_doc.config import Config, ParseWarning
from coop_data_doc.graph.model import LineageGraph, normalize_identifier

CONFIG_NAME = "sqlsc.yml"
OBJECT_FOLDERS = frozenset({"tables", "views", "stored procedures", "functions", "triggers"})
_FOLDER_KEY = re.compile(r"^folder:\s*['\"]?([^'\"#\n]+?)['\"]?\s*(?:#.*)?$", re.MULTILINE)
_SKIP_DIRS = frozenset({".git", ".venv", "node_modules", "__pycache__", "bin", "obj"})
_MAX_DEPTH = 3


def _decode(part: str) -> str:
    """sqlsc's file-name escape: %XX (UTF-8 bytes) back to characters."""
    try:
        return re.sub(
            r"(?:%[0-9A-Fa-f]{2})+",
            lambda m: bytes.fromhex(m.group().replace("%", "")).decode("utf-8"),
            part,
        )
    except UnicodeDecodeError:
        return part


def script_dirs(root: Path) -> set[Path]:
    """The scripts folders of every sqlsc.yml at, above, or up to three levels below `root`."""
    candidates = [d / CONFIG_NAME for d in (root, *root.parents)]
    if root.is_dir():
        base = len(root.parts)
        for here, dirs, files in os.walk(root):
            dirs[:] = sorted(
                d
                for d in dirs
                if d not in _SKIP_DIRS
                and not d.startswith(".")
                and len(Path(here, d).parts) - base <= _MAX_DEPTH
            )
            if CONFIG_NAME in files:
                candidates.append(Path(here) / CONFIG_NAME)
    found: set[Path] = set()
    for config in candidates:
        try:
            if not config.is_file():
                continue
            text = config.read_text(encoding="utf-8-sig", errors="replace")
            match = _FOLDER_KEY.search(text)
            found.add((config.parent / (match.group(1).strip() if match else "database")).resolve())
        except OSError:
            continue
    return found


def file_identity(path: Path, dirs: set[Path]) -> tuple[str, str] | None:
    """(schema, name), normalized, for a file in sqlsc's layout; None for any other file."""
    if path.parent.name.lower() not in OBJECT_FOLDERS or path.suffix.lower() != ".sql":
        return None
    try:
        if path.parent.parent.resolve() not in dirs:
            return None
    except OSError:
        return None
    schema, dot, name = path.stem.partition(".")
    if not dot or not schema or not name:
        return None
    return normalize_identifier(_decode(schema)), normalize_identifier(_decode(name))


def check(graph: LineageGraph, config: Config) -> list[ParseWarning]:
    """One warning per sqlsc file whose objects' declared names differ from its file name."""
    by_file: dict[str, list] = {}
    for node in graph.nodes.values():
        if node.node_type in SQL_NODE_TYPES and node.source_file:
            by_file.setdefault(node.source_file, []).append(node)
    if not by_file:
        return []
    warnings: list[ParseWarning] = []
    seen: set[str] = set()
    for repo_key in sorted(config.repos):
        root = config.repo_root(repo_key)
        dirs = script_dirs(root)
        if not dirs:
            continue
        for source_file in sorted(by_file):
            if source_file in seen:
                continue
            path = root / source_file
            identity = file_identity(path, dirs) if path.is_file() else None
            if identity is None:
                continue
            seen.add(source_file)
            nodes = by_file[source_file]
            if any((node.schema_name, node.name) == identity for node in nodes):
                continue
            declared = ", ".join(sorted(f"{n.schema_name}.{n.name}" for n in nodes))
            warnings.append(
                ParseWarning(
                    file=source_file,
                    message=(
                        f"sqlsc's file for {identity[0]}.{identity[1]} declares {declared} "
                        "(renamed after it was scripted?); documented under the declared name. "
                        "Script it again with sqlsc 1.0.4 or later to correct the file"
                    ),
                    category="sqlsc_declaration_mismatch",
                )
            )
    return sorted(warnings, key=lambda w: (w.file, w.message))
