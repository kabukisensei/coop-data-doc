"""Persistent resolution cache (Module 4).

`.lineage-cache.json` lives next to the user's config and is meant to be
committed: every interactive answer is written immediately (crash-safe),
keys are sorted, and formatting is stable so diffs stay minimal.
"""

from __future__ import annotations

import errno
import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path

from pydantic import BaseModel

from coop_data_doc.config import ParseWarning
from coop_data_doc.graph.model import LineageGraph


class CacheEntry(BaseModel):
    """One remembered answer: a target node id, or None for external/skip."""

    target: str | None  # node id, or None for external/skip
    method: str  # "interactive" | "external" | "skip"


class LineageCache:
    """Read/write wrapper for .lineage-cache.json (commit that file!)."""

    VERSION = 1

    def __init__(self, path: Path, mappings: dict[str, CacheEntry] | None = None):
        self.path = Path(path)
        self.mappings: dict[str, CacheEntry] = mappings or {}
        self.warnings: list[ParseWarning] = []
        # Keys whose target vanished from the CURRENT graph: ignored by get()
        # for this run but kept in mappings (and therefore on disk). Only a
        # persist=True prune_invalid actually deletes them — a check/status/
        # resolve/wizard-dry-run against a branch or a narrower scope must
        # never destroy committed human answers.
        self._ignored: set[str] = set()

    @classmethod
    def load(cls, path: Path | str) -> LineageCache:
        """Load a cache file; missing/invalid/unknown-version -> empty cache
        with warnings (the file itself is never deleted).
        """
        path = Path(path)
        cache = cls(path)
        if not path.is_file():
            return cache
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            cache.warnings.append(
                ParseWarning(
                    file=str(path),
                    message=f"cache unreadable, starting empty: {exc}",
                    category="cache_invalid",
                )
            )
            return cache
        # A structurally-valid-but-wrong-shape cache (top-level JSON list/number/string)
        # must degrade, not crash: .get() on a non-dict AttributeErrors. Guard the shape
        # before the version probe so "invalid -> empty cache with warnings" always holds.
        if not isinstance(data, dict):
            cache.warnings.append(
                ParseWarning(
                    file=str(path),
                    message="cache is not a JSON object; ignoring file",
                    category="cache_invalid",
                )
            )
            return cache
        if data.get("version") != cls.VERSION:
            cache.warnings.append(
                ParseWarning(
                    file=str(path),
                    message=f"unknown cache version {data.get('version')!r}; ignoring file",
                    category="cache_invalid",
                )
            )
            return cache
        if "mappings" in data:
            mappings = data["mappings"]
            if not isinstance(mappings, dict):
                cache.warnings.append(
                    ParseWarning(
                        file=str(path),
                        message="cache mappings is not a JSON object; ignoring file",
                        category="cache_invalid",
                    )
                )
                return cache
        else:
            mappings = {}
        for key, raw in mappings.items():
            try:
                cache.mappings[key] = CacheEntry.model_validate(raw)
            except Exception:  # noqa: BLE001 — a corrupt cache entry degrades to a warning
                cache.warnings.append(
                    ParseWarning(
                        file=str(path),
                        message=f"invalid cache entry {key!r}; dropped",
                        category="cache_invalid",
                    )
                )
        return cache

    def get(self, key: str) -> CacheEntry | None:
        """Look up a remembered answer by cache key (None for ignored entries)."""
        if key in self._ignored:
            return None
        return self.mappings.get(key)

    def put(self, key: str, entry: CacheEntry) -> None:
        """Store an answer and write the file immediately (crash-safe)."""
        self._ignored.discard(key)  # a fresh answer supersedes "ignored"
        self.mappings[key] = entry
        self.write()

    def put_many(self, entries: Iterable[tuple[str, CacheEntry]]) -> bool:
        """Store a batch in memory, then persist the complete cache exactly once.

        Interactive callers should continue to use :meth:`put`, which preserves
        the existing answer-by-answer durability contract. Machine-driven bulk
        callers such as ``resolve-apply`` use this method so a batch cannot cause
        N full-file rewrites followed by a redundant final write.
        """
        for key, entry in entries:
            self._ignored.discard(key)
            self.mappings[key] = entry
        return self.write()

    def prune_invalid(self, graph: LineageGraph, persist: bool = False) -> list[str]:
        """Handle entries whose target node isn't in ``graph``; return their keys.

        ``persist=False`` (the default — check/status/resolve/wizard dry-runs):
        the entries are merely *ignored this run* (``get`` returns None so the
        ladder re-resolves) but stay in ``mappings`` and on disk. The current
        graph may simply be a different branch or a narrower scope than the one
        the human answered against; deleting would destroy committed answers.

        ``persist=True`` (only after a successful explicit ``build``): the
        entries are deleted and the file rewritten.
        """
        dropped = sorted(
            key
            for key, entry in self.mappings.items()
            if entry.target is not None and entry.target not in graph.nodes
        )
        if persist:
            for key in dropped:
                del self.mappings[key]
                self._ignored.discard(key)
            if dropped:
                self.write()
        else:
            self._ignored.update(dropped)
        return dropped

    def write(self) -> bool:
        """Atomically persist with stable formatting for clean git diffs.

        The complete UTF-8/LF payload is written and flushed in a unique
        same-directory temporary file before ``os.replace`` swaps it into place.
        Consequently readers see either the previous complete cache or the new
        complete cache, never a partially-truncated destination. The destination
        is never opened for writing, which also keeps the operation compatible
        with Windows replacement semantics.

        File fsync failures are fatal before replacement, except for explicit
        platform "not supported" errors. After replacement, POSIX parent-folder
        fsync is best-effort because some filesystems reject directory handles;
        the cache is already complete at that point. Any pre-replacement
        ``OSError`` records one ``cache_write_failed`` warning and returns False.
        The in-memory mappings remain available for a later retry.
        """
        payload = {
            "version": self.VERSION,
            "mappings": {key: self.mappings[key].model_dump() for key in sorted(self.mappings)},
        }
        serialized = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        temp_path: Path | None = None
        temp_fd: int | None = None
        try:
            temp_fd, temp_name = tempfile.mkstemp(
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
            )
            temp_path = Path(temp_name)
            with os.fdopen(temp_fd, "w", encoding="utf-8", newline="\n") as handle:
                temp_fd = None  # fdopen owns and closes the descriptor from here.
                handle.write(serialized)
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError as exc:
                    unsupported = {errno.EINVAL, errno.ENOSYS}
                    if hasattr(errno, "ENOTSUP"):
                        unsupported.add(errno.ENOTSUP)
                    if exc.errno not in unsupported:
                        raise

            os.replace(temp_path, self.path)
            temp_path = None  # replacement consumed the temporary path.
        except OSError as exc:
            self.warnings.append(
                ParseWarning(
                    file=str(self.path),
                    message=f"could not write lineage cache: {exc}",
                    category="cache_write_failed",
                )
            )
            return False
        finally:
            if temp_fd is not None:
                try:
                    os.close(temp_fd)
                except OSError:
                    pass
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

        # Once the atomic replacement has succeeded, make the directory entry
        # durable where POSIX supports directory fsync. This is deliberately
        # best-effort: Windows has no equivalent, and some POSIX filesystems
        # reject directory handles even though the replacement itself succeeded.
        if os.name == "posix":
            directory_fd: int | None = None
            try:
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                directory_fd = os.open(self.path.parent, flags)
                os.fsync(directory_fd)
            except OSError:
                pass
            finally:
                if directory_fd is not None:
                    try:
                        os.close(directory_fd)
                    except OSError:
                        pass
        return True
