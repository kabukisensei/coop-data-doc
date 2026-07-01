"""Repo crawler (Module 1).

Walks each configured repo, applies include/exclude globs, and classifies
every hit into a FileKind so downstream parsers (Modules 2 & 3) can pick up
exactly the files they understand. Pure stdlib; emits POSIX-style relative
paths so output is identical across operating systems.
"""

from __future__ import annotations

import fnmatch
import os
import re
from enum import Enum
from pathlib import Path

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
    # Deterministic cross-OS policy: globs match case-INSENSITIVELY on every
    # platform. fnmatch.fnmatch defers to os.path.normcase, which folds case
    # on Windows but not on POSIX — so the same repo would crawl a different
    # file set per OS (an uppercase REPORT.SQL silently skipped on Linux CI
    # but documented on a Windows dev box), breaking the cross-OS byte-identity
    # guarantee. Uniform insensitivity is the Windows-first-friendly choice:
    # casefold both sides ourselves and use fnmatchcase (which never normcases).
    #
    # fnmatch's '*' crosses '/', but '**/*.sql' still requires at least one
    # slash — also try the pattern with a leading '**/' stripped so
    # top-level files match the way users expect from glob syntax.
    name = rel_posix.casefold()
    for pattern in patterns:
        folded = pattern.casefold()
        if fnmatch.fnmatchcase(name, folded):
            return True
        if folded.startswith("**/") and fnmatch.fnmatchcase(name, folded[3:]):
            return True
    return False


def _dir_excluded(rel_dir: str, patterns: list[str]) -> bool:
    """True when an exclude glob covers the *entire* subtree under ``rel_dir``,
    so the walk can skip descending into it altogether.

    Conservative on purpose: it only prunes when a generic file at *arbitrary
    depth* beneath ``rel_dir`` would be excluded (e.g. ``**/archive/**`` or
    ``**/node_modules/**``). A narrow pattern like ``**/archive/*.tmp`` returns
    False, so the directory is still crawled and the per-file exclude drops the
    real matches while keeping the non-matches — identical to the old
    rglob-then-filter behaviour, just without the wasted descent. The ``\\x00``
    probe segments can never appear in a real path, so name/extension-specific
    patterns don't falsely trigger a prune.
    """
    return _matches(f"{rel_dir}/\x00", patterns) and _matches(f"{rel_dir}/\x00/\x00", patterns)


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
        # os.walk with in-place dir pruning (not rglob) so huge non-source trees
        # are never *descended into*. rglob("*") would enter .git / .venv /
        # node_modules and stat every file only to discard it — cheap on a local
        # Mac, but on a Windows VM each touch pays a Defender / OneDrive tax and
        # a big .git alone can stall the crawl for minutes. Pruning here is
        # behaviour-identical to the per-file skips below: hidden paths were
        # always skipped, and a subtree-wide exclude drops every file under it.
        for dirpath, dirnames, filenames in os.walk(root):
            base = Path(dirpath)
            dirnames[:] = sorted(
                d
                for d in dirnames
                if not d.startswith(".")  # .git, .venv, .pbi caches — never descend
                and not _dir_excluded((base / d).relative_to(root).as_posix(), repo.exclude)
            )
            for name in sorted(filenames):
                path = base / name
                rel = path.relative_to(root).as_posix()
                try:
                    if not path.is_file():
                        continue
                    # a hidden file at a non-hidden path (e.g. src/.notes.sql)
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
                except OSError as exc:
                    # locked, permission-denied, or deleted/disconnected between
                    # enumeration and stat (e.g. a .pbix held by Power BI Desktop or
                    # OneDrive on Windows): degrade to a warning, never abort the build.
                    warnings.append(
                        ParseWarning(
                            file=rel,
                            message=f"could not access file ({exc}); skipped",
                            category="file_unreadable",
                        )
                    )
                    continue
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
