"""Repo crawler (Module 1).

Walks each configured repo, applies include/exclude globs, and classifies
every hit into a FileKind so downstream parsers (Modules 2 & 3) can pick up
exactly the files they understand. Pure stdlib; emits POSIX-style relative
paths so output is identical across operating systems.
"""

from __future__ import annotations

import fnmatch
import re
from enum import Enum

from pydantic import BaseModel, Field

from coop_data_doc.config import Config, ParseWarning

MAX_FILE_BYTES = 10 * 1024 * 1024  # files above this are skipped, except .pbix


class FileKind(str, Enum):
    """What a crawled file is, deciding which parser consumes it."""

    SQL_FILE = "sql_file"
    TMDL = "tmdl"
    BIM = "bim"
    PBIR_VISUAL = "pbir_visual"
    PBIR_PAGE = "pbir_page"
    REPORT_JSON_LEGACY = "report_json_legacy"
    PBIX = "pbix"


class FileEntry(BaseModel):
    """One crawled file; `path` is POSIX-style relative to its repo root."""

    path: str  # POSIX-style, relative to the repo root
    abs_path: str
    repo_key: str
    kind: FileKind
    size: int


class FileInventory(BaseModel):
    """All crawled files, sorted by (repo_key, path) for determinism."""

    entries: list[FileEntry] = Field(default_factory=list)  # sorted (repo_key, path)

    def by_kind(self, kind: FileKind) -> list[FileEntry]:
        """Entries of one FileKind, preserving the sorted order."""
        return [entry for entry in self.entries if entry.kind == kind]


_PBIR_VISUAL_RE = re.compile(r"definition/pages/[^/]+/visuals/[^/]+/visual\.json$")
_PBIR_PAGE_RE = re.compile(r"definition/pages/[^/]+/page\.json$")


def _matches(rel_posix: str, patterns: list[str]) -> bool:
    # fnmatch's '*' crosses '/', but '**/*.sql' still requires at least one
    # slash — also try the pattern with a leading '**/' stripped so
    # top-level files match the way users expect from glob syntax.
    for pattern in patterns:
        if fnmatch.fnmatch(rel_posix, pattern):
            return True
        if pattern.startswith("**/") and fnmatch.fnmatch(rel_posix, pattern[3:]):
            return True
    return False


def _classify(rel_posix: str) -> FileKind | None:
    name = rel_posix.rsplit("/", 1)[-1].lower()
    if name.endswith(".sql"):
        return FileKind.SQL_FILE
    if name.endswith(".tmdl"):
        return FileKind.TMDL
    if name.endswith(".bim"):
        return FileKind.BIM
    if name.endswith(".pbix"):
        return FileKind.PBIX
    if name == "visual.json" and _PBIR_VISUAL_RE.search(rel_posix):
        return FileKind.PBIR_VISUAL
    if name == "page.json" and _PBIR_PAGE_RE.search(rel_posix):
        return FileKind.PBIR_PAGE
    if name == "report.json" and "/definition/" not in f"/{rel_posix}":
        return FileKind.REPORT_JSON_LEGACY
    return None


def crawl(config: Config) -> tuple[FileInventory, list[ParseWarning]]:
    """Walk every configured repo, apply include/exclude globs, classify
    each hit, and return (inventory, warnings). Skips hidden dirs, files
    over the size cap (except .pbix), and symlinks escaping the repo.
    """
    entries: list[FileEntry] = []
    warnings: list[ParseWarning] = []

    for repo_key in sorted(config.repos):
        repo = config.repos[repo_key]
        root = config.repo_root(repo_key)
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            # hidden dirs/files (.git, .pbi caches, .lineage-cache.json)
            if any(part.startswith(".") for part in rel.split("/")):
                continue
            if path.is_symlink() and not path.resolve().is_relative_to(root):
                warnings.append(
                    ParseWarning(
                        file=rel,
                        message=f"symlink resolves outside repo '{repo_key}'; skipped",
                        category="symlink_escape",
                    )
                )
                continue
            if not _matches(rel, repo.include) or _matches(rel, repo.exclude):
                continue
            kind = _classify(rel)
            if kind is None:
                warnings.append(
                    ParseWarning(
                        file=rel,
                        message="included by globs but not a recognized file kind; skipped",
                        category="unclassified_file",
                    )
                )
                continue
            size = path.stat().st_size
            if size > MAX_FILE_BYTES and kind is not FileKind.PBIX:
                warnings.append(
                    ParseWarning(
                        file=rel,
                        message=f"file is {size} bytes (> {MAX_FILE_BYTES}); skipped",
                        category="file_too_large",
                    )
                )
                continue
            entries.append(
                FileEntry(
                    path=rel,
                    abs_path=str(path),
                    repo_key=repo_key,
                    kind=kind,
                    size=size,
                )
            )

    entries.sort(key=lambda entry: (entry.repo_key, entry.path))
    return FileInventory(entries=entries), warnings
