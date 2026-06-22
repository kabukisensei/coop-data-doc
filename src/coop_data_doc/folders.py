"""Top-level-folder helpers shared by the setup wizard (terminal checkbox) and the
non-interactive ``folders`` / ``set-folders`` commands (driven by the coop agent or
CI). One implementation so the two front-ends never drift on which folders map to
which skip globs.

A repo "documents" all of its top-level folders by default; skipping a folder is
expressed as a ``**/Name/**`` exclude glob. These helpers convert between the folder
names a user picks and those globs, round-tripping cleanly so re-running setup (or
re-reading via ``folders``) recovers the same checkboxes.
"""

from __future__ import annotations

import glob
from pathlib import Path


def top_level_folders(repo_abs: Path) -> list[str]:
    """Sorted names of the repo's top-level, non-hidden subfolders.

    Empty when the repo path doesn't exist yet or is flat. Hidden dirs are skipped
    to match the crawler, which never descends into them.
    """
    if not repo_abs.is_dir():
        return []
    return sorted(
        child.name for child in repo_abs.iterdir() if child.is_dir() and not child.name.startswith(".")
    )


def unescape_glob(escaped: str) -> str:
    """Reverse ``glob.escape``: ``[*]``→``*``, ``[?]``→``?``, ``[[]``→``[``."""
    return escaped.replace("[*]", "*").replace("[?]", "?").replace("[[]", "[")


def folder_name_from_glob(pattern: str) -> str | None:
    """The folder name a simple 'skip this folder' glob targets, else None.

    Recognizes ``**/Name/**`` (with fnmatch metacharacters in ``Name`` escaped the
    way :func:`excludes_for_skips` escapes them) plus the legacy ``Name/**`` / ``Name/*``
    forms. Anything else (nested paths, or a real wildcard like ``**/data*/**``)
    returns None and is preserved verbatim as a hand-written pattern.
    """
    body = pattern.strip()
    if body.startswith("**/"):
        body = body[3:]
    for suffix in ("/**", "/*"):
        if body.endswith(suffix):
            body = body[: -len(suffix)]
            break
    else:
        return None
    if not body or "/" in body:
        return None
    name = unescape_glob(body)
    # Only a literal folder name round-trips: a genuine wildcard pattern re-escapes
    # to something different, so it stays a custom pattern.
    if glob.escape(name) != body:
        return None
    return name


def split_excludes(folders: list[str], exclude: list[str]) -> tuple[set[str], list[str]]:
    """Split an exclude list into (skipped folder names matching a detected top-level
    folder, custom patterns carried through verbatim)."""
    skipped: set[str] = set()
    custom: list[str] = []
    for pattern in exclude:
        name = folder_name_from_glob(pattern)
        if name is not None and name in folders:
            skipped.add(name)
        else:
            custom.append(pattern)
    return skipped, custom


def excludes_for_skips(folders: list[str], skip: set[str], custom: list[str]) -> list[str]:
    """Build the exclude list for the skipped folders, preserving custom patterns.

    ``glob.escape`` keeps plain names byte-identical (archive → ``**/archive/**``) but
    makes a folder whose name contains ``[ ] ? *`` match literally in the crawler.
    Folder excludes are appended after customs and follow ``folders`` order (sorted),
    so the written config is deterministic.
    """
    folder_excludes = [f"**/{glob.escape(name)}/**" for name in folders if name in skip]
    return custom + folder_excludes


def folder_states(repo_abs: Path, exclude: list[str]) -> tuple[list[dict], list[str]]:
    """For a repo: ``([{name, documented}], custom_excludes)``.

    ``documented`` is True for a top-level folder not skipped by a matching exclude
    glob. The folder list is empty when the repo isn't on disk (callers then fall
    back to hand-written globs). Deterministic: folders are sorted.
    """
    folders = top_level_folders(repo_abs)
    skipped, custom = split_excludes(folders, exclude)
    states = [{"name": name, "documented": name not in skipped} for name in folders]
    return states, custom
