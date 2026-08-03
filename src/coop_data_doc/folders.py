"""Top-level-folder helpers shared by the setup wizard (terminal checkbox) and the
non-interactive ``folders`` / ``set-folders`` commands (driven by the coop agent or
CI). One implementation so the two front-ends never drift on which folders map to
which include globs.

Folder selection is an ALLOWLIST: documenting a top-level folder ``Foo`` is
expressed as file-type-scoped include globs derived from the repo's base
patterns (``**/*.sql`` → ``Foo/**/*.sql``). A repo whose include list contains
such folder-scoped globs (not starting with ``**/``) is in "allowlist mode" —
only the named folders are crawled. Configs written before this mode existed
(denylist: a broad ``**/`` include plus ``**/Name/**`` excludes) still read
correctly via the legacy rule. These helpers convert between the folder names
a user picks and those globs, round-tripping cleanly so re-running setup (or
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

    Legacy: kept for reading/pre-serving old denylist configs; new folder
    selections are written as include globs (see ``includes_for_folders``).
    """
    folder_excludes = [f"**/{glob.escape(name)}/**" for name in folders if name in skip]
    return custom + folder_excludes


def folder_scoped_includes(include: list[str]) -> dict[str, list[str]]:
    """Include globs scoped to a top-level folder, grouped by that folder.

    A scoped glob is one NOT starting with ``**/`` whose first path segment is
    the folder name (``Foo/**/*.sql`` → ``{"Foo": ["Foo/**/*.sql"]}``); the
    segment is unescaped so a ``glob.escape``d folder name maps back. A repo
    whose include list has any of these is in "allowlist mode": only the named
    folders are crawled.
    """
    scoped: dict[str, list[str]] = {}
    for pattern in include:
        if pattern.startswith("**/") or "/" not in pattern:
            continue
        folder = unescape_glob(pattern.split("/", 1)[0])
        scoped.setdefault(folder, []).append(pattern)
    return scoped


def base_patterns_from_includes(include: list[str]) -> list[str]:
    """The ``**/``-rooted file-type templates an include list was built from.

    Custom global globs (already ``**/``-rooted) pass through; folder-scoped
    globs contribute their stripped form (``Foo/**/*.sql`` → ``**/*.sql``).
    First-seen order, duplicates removed, so the result is deterministic.
    """
    bases: list[str] = []
    for pattern in include:
        if pattern.startswith("**/"):
            base = pattern
        elif "/" in pattern:
            rest = pattern.split("/", 1)[1]
            # the scoped shape the wizard/set-folders write is "Foo/**/*.sql":
            # stripping the folder prefix already leaves a **/-rooted template
            base = rest if rest.startswith("**/") else "**/" + rest
        else:
            continue
        if base not in bases:
            bases.append(base)
    return bases


def includes_for_folders(folders: list[str], base_patterns: list[str]) -> list[str]:
    """Folder-scoped include globs documenting exactly ``folders``: every base
    pattern re-rooted under every folder, sorted for determinism. Folder names
    are ``glob.escape``d so metacharacters match literally in the crawler."""
    return sorted(f"{glob.escape(folder)}/{pattern}" for folder in folders for pattern in base_patterns)


def folder_states(
    repo_abs: Path, exclude: list[str], include: list[str] | None = None
) -> tuple[list[dict], list[str], list[str]]:
    """For a repo: ``([{name, documented}], custom_excludes, custom_includes)``.

    Allowlist mode (any folder-scoped include globs): a folder is "documented"
    iff it has scoped include globs. Legacy mode (no scoped includes): the old
    rule — documented iff no matching ``**/Name/**`` exclude. ``custom_includes``
    are the include globs that aren't folder toggles (global ``**/`` patterns,
    plus scoped globs whose folder isn't on disk — preserved verbatim). The
    folder list is empty when the repo isn't on disk (callers then fall back
    to hand-written globs). Deterministic: folders are sorted.
    """
    folders = top_level_folders(repo_abs)
    include = include or []
    scoped = folder_scoped_includes(include)
    if scoped:
        states = [{"name": name, "documented": name in scoped} for name in folders]
        custom_includes = [
            pattern
            for pattern in include
            if pattern.startswith("**/")
            or "/" not in pattern
            or unescape_glob(pattern.split("/", 1)[0]) not in folders
        ]
    else:
        skipped, _ = split_excludes(folders, exclude)
        states = [{"name": name, "documented": name not in skipped} for name in folders]
        custom_includes = list(include)
    _, custom_excludes = split_excludes(folders, exclude)
    return states, custom_excludes, custom_includes
