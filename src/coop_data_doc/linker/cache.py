"""Persistent resolution cache (Module 4).

`.lineage-cache.json` lives next to the user's config and is meant to be
committed: every interactive answer is written immediately (crash-safe),
keys are sorted, and formatting is stable so diffs stay minimal.
"""

from __future__ import annotations

import json
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
        """Persist with sorted keys and stable formatting for clean git diffs.

        Returns True on a successful write. An ``OSError`` (a locked or
        read-only file — Windows OneDrive/Defender routinely hold short-lived
        locks on small JSON files) is caught, recorded on ``self.warnings`` as
        a ``cache_write_failed`` warning, and reported as False rather than
        crashing the caller. The mapping was already stored in memory before
        this call, so a later successful ``write()`` in the same session
        persists every accumulated answer — transient locks self-heal. Unlike
        the derivable parse cache, these are human answers, so callers that
        can't rely on a later write (``resolve-apply``) must check the return
        value and surface a hard error.
        """
        payload = {
            "version": self.VERSION,
            "mappings": {key: self.mappings[key].model_dump() for key in sorted(self.mappings)},
        }
        try:
            self.path.write_text(
                json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                encoding="utf-8",
                newline="\n",
            )
        except OSError as exc:
            self.warnings.append(
                ParseWarning(
                    file=str(self.path),
                    message=f"could not write lineage cache: {exc}",
                    category="cache_write_failed",
                )
            )
            return False
        return True
