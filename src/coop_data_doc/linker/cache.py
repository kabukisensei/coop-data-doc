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

    @classmethod
    def load(cls, path: Path | str) -> "LineageCache":
        """Load a cache file; missing/invalid/unknown-version -> empty cache
        with warnings (the file itself is never deleted).
        """
        path = Path(path)
        cache = cls(path)
        if not path.is_file():
            return cache
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            cache.warnings.append(
                ParseWarning(
                    file=str(path),
                    message=f"cache unreadable, starting empty: {exc}",
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
        for key, raw in (data.get("mappings") or {}).items():
            try:
                cache.mappings[key] = CacheEntry.model_validate(raw)
            except Exception:
                cache.warnings.append(
                    ParseWarning(
                        file=str(path),
                        message=f"invalid cache entry {key!r}; dropped",
                        category="cache_invalid",
                    )
                )
        return cache

    def get(self, key: str) -> CacheEntry | None:
        """Look up a remembered answer by cache key."""
        return self.mappings.get(key)

    def put(self, key: str, entry: CacheEntry) -> None:
        """Store an answer and write the file immediately (crash-safe)."""
        self.mappings[key] = entry
        self.write()

    def prune_invalid(self, graph: LineageGraph) -> list[str]:
        """Drop entries whose target node no longer exists; return them."""
        dropped = sorted(
            key
            for key, entry in self.mappings.items()
            if entry.target is not None and entry.target not in graph.nodes
        )
        for key in dropped:
            del self.mappings[key]
        if dropped:
            self.write()
        return dropped

    def write(self) -> None:
        """Persist with sorted keys and stable formatting for clean git diffs."""
        payload = {
            "version": self.VERSION,
            "mappings": {
                key: self.mappings[key].model_dump() for key in sorted(self.mappings)
            },
        }
        self.path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
