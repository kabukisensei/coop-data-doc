"""Incremental file-level SQL parse cache (Module 2 support).

An opt-out, per-file *subgraph* cache so an unchanged SQL file is never
re-parsed. On a warm run each SQL parser replays a file's cached contribution
(the nodes/edges/warnings it produced) into the shared graph **in the exact
same sorted file order as a cold run**, so the merged graph — and therefore
every generated artifact — is byte-identical to a cold build. The cross-file
passes (resolve_stub_references, link_visual_bindings, prune_schemas,
assign_layers, the linker) still run every time; only the per-file parse is
skipped.

Cache key
---------
``(coop_data_doc.__version__, sql_dialect, parser-pass, file-path,
sha256(file_text))`` — the content hash (never mtime) means an edit changes the
key and forces a re-parse, while a tool upgrade or a dialect change invalidates
the whole cache. The parser-pass tag ("objects"/"procs") keeps each file's two
parses distinct. The repo-relative POSIX path keeps two *identical-content*
files at different paths distinct: a contribution embeds its file path (node
``source_file``, edge evidence, warning ``file``), so sharing one entry across
paths would mis-attribute all of those to whichever file was parsed first
(cost: a renamed file re-parses once). The key is serialized as a single
``version\x1fdialect\x1fpass\x1fpath\x1fsha256`` string so it can be a JSON
object key.

Cache value
-----------
The file's parser contribution:

- ``nodes``: ``{node_id: Node.model_dump(mode="python")}`` — **mode="python"**
  so ``source_code`` (which is ``exclude=True`` and therefore stripped by
  ``mode="json"``) is preserved. On replay ``Node.model_validate`` restores it.
- ``edges``: ``[Edge.model_dump(mode="python")]``, sorted by
  ``(source_id, target_id, edge_type)`` so replay order is stable.
- ``warnings``: ``[ParseWarning.model_dump()]`` for that file.

Storage
-------
A single JSON file (``.coop-data-doc-parse-cache.json``) sibling of
``.lineage-cache.json``. It is *derivable* (a cold rebuild reproduces it), so it
is gitignored and never a source of truth. Load is tolerant — missing, corrupt,
or version-mismatched files degrade to an empty cache (a cold run) plus a
warning, never a crash — mirroring ``linker/cache.py``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, Field

from coop_data_doc import __version__
from coop_data_doc.config import ParseWarning

# Bump when the cached shape or the merge semantics change so old caches are
# ignored rather than mis-replayed. Independent of the tool version (which is
# ALSO part of every per-file key), but a coarser guard on the whole file.
# v2: cache keys gained the file path (identical-content files at different
# paths previously shared one entry, mis-attributing warnings/evidence) — the
# bump drops pathless v1 entries instead of carrying them as dead weight.
CACHE_VERSION = 2


class ParseCacheEntry(BaseModel):
    """One file's parser contribution: nodes, edges, and warnings.

    ``nodes`` values are ``Node.model_dump(mode="python")`` dicts (source_code
    included); ``edges`` are ``Edge.model_dump(mode="python")`` dicts sorted by
    key; ``warnings`` are ``ParseWarning`` dumps. Kept as plain dicts/lists so
    the on-disk JSON is stable and the replay just re-validates them into
    Node/Edge/ParseWarning.
    """

    nodes: dict[str, dict] = Field(default_factory=dict)
    edges: list[dict] = Field(default_factory=list)
    warnings: list[dict] = Field(default_factory=list)


def file_sha256(text: str) -> str:
    """Content hash of a decoded file's text (UTF-8 bytes).

    Hashing the ALREADY-DECODED text (not the raw disk bytes) is deliberate: the
    pipeline decodes each file once via ``read_sql_file`` and shares the result
    across both parser passes, so the same normalized text drives both the cache
    key and the parse — a file re-encoded UTF-16→UTF-8 with identical content
    therefore reuses its cache, and CRLF/CR normalization in ``decode_text`` is
    already folded in.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cache_key(dialect: str, text: str, pass_tag: str, path: str) -> str:
    """The per-file cache key string: version, dialect, pass, path, content hash.

    ``pass_tag`` namespaces the two SQL parser passes ("objects" vs "procs")
    over the SAME file so their contributions don't collide on one key (the file
    is parsed once by each). ``path`` (the entry's repo-relative POSIX path)
    keeps identical-content files at different paths on separate entries — a
    contribution embeds its own path in source_file/evidence/warnings, so a
    shared entry would replay the FIRST file's attribution for both. Same file
    + same tag + same content => same key => a hit that replays exactly that
    pass's contribution.
    """
    return f"{__version__}\x1f{dialect}\x1f{pass_tag}\x1f{path}\x1f{file_sha256(text)}"


class ParseCache:
    """Read/write wrapper for ``.coop-data-doc-parse-cache.json``.

    Tolerant load (missing/corrupt/version-mismatch -> empty + warning), a
    ``get``/``put`` keyed by ``cache_key``, and hit/miss counters the CLI reads
    for the "Parsing SQL (N cached, M parsed)" summary. The file is derivable,
    so a failed write is non-fatal.
    """

    def __init__(self, path: Path, entries: dict[str, ParseCacheEntry] | None = None):
        self.path = Path(path)
        self.entries: dict[str, ParseCacheEntry] = entries or {}
        self.warnings: list[ParseWarning] = []
        # Distinct hit/miss counters for the one-line progress summary. Not
        # persisted; reset per process. Files whose cache is bypassed by
        # --no-parse-cache count as misses (they are re-parsed).
        self.hits = 0
        self.misses = 0

    @classmethod
    def load(cls, path: Path | str) -> ParseCache:
        """Load a cache file; missing -> empty (no warning, it is derivable);
        corrupt/unknown-version/invalid-entry -> empty (or partial) + warning.
        Never raises — a bad cache always degrades to a cold parse."""
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
                    message=f"parse cache unreadable, re-parsing from scratch: {exc}",
                    category="parse_cache_invalid",
                )
            )
            return cache
        # Guard the shape BEFORE calling .get(): a structurally-valid-but-wrong-shape
        # cache (a top-level JSON list/number/string) is truthy, so `data or {}` would
        # NOT fall back to {} and `.get()` would AttributeError — the exact crash the
        # "never raises" contract must not have. Handle non-dict explicitly, then version.
        if not isinstance(data, dict):
            cache.warnings.append(
                ParseWarning(
                    file=str(path),
                    message="parse cache is not a JSON object; ignoring file",
                    category="parse_cache_invalid",
                )
            )
            return cache
        if data.get("version") != CACHE_VERSION:
            cache.warnings.append(
                ParseWarning(
                    file=str(path),
                    message=f"unknown parse-cache version {data.get('version')!r}; ignoring file",
                    category="parse_cache_invalid",
                )
            )
            return cache
        if "cache" in data:
            entries = data["cache"]
            if not isinstance(entries, dict):
                cache.warnings.append(
                    ParseWarning(
                        file=str(path),
                        message="parse cache entries are not a JSON object; ignoring file",
                        category="parse_cache_invalid",
                    )
                )
                return cache
        else:
            entries = {}
        for key, raw in entries.items():
            try:
                cache.entries[key] = ParseCacheEntry.model_validate(raw)
            except Exception:  # noqa: BLE001 — a corrupt cache entry degrades to a cold re-parse
                cache.warnings.append(
                    ParseWarning(
                        file=str(path),
                        message=f"invalid parse-cache entry {key!r}; dropped (that file re-parses)",
                        category="parse_cache_invalid",
                    )
                )
        return cache

    def get(self, key: str) -> ParseCacheEntry | None:
        """Look up a file's cached contribution; None on miss."""
        return self.entries.get(key)

    def put(self, key: str, entry: ParseCacheEntry) -> None:
        """Store a file's contribution in memory (written once via ``write``)."""
        self.entries[key] = entry

    def write(self) -> None:
        """Persist with sorted keys + stable formatting; a write failure is
        swallowed (the cache is derivable — the next run just re-parses)."""
        payload = {
            "version": CACHE_VERSION,
            "cache": {key: self.entries[key].model_dump() for key in sorted(self.entries)},
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
                    message=f"could not write parse cache (non-fatal): {exc}",
                    category="parse_cache_invalid",
                )
            )
