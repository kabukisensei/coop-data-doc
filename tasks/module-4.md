# MODULE 4 — Linker, Interactive Resolution & Cache ✅ IMPLEMENTED

> Status: **done** — kept as interface reference; do not reimplement.

**Files to create:** `src/coop_data_doc/linker/resolver.py`, `linker/cache.py`,
`linker/interactive.py`, `tests/test_linker.py`.

**Inputs you can rely on:** a merged `LineageGraph` already containing all SQL nodes (M2) and
all PBI nodes (M3); `Config.schema_mappings` (`[{schema, model}]`); pbi_table nodes carrying
`metadata["partition_source"] = {"schema", "object", "raw_kind"}` or
`metadata["partition_source_unresolved"]`; visuals possibly carrying
`metadata["pending_model_resolution"]`.

## Purpose
Join the SQL half of the graph to the Power BI half. View schemas and semantic-model names are
*similar but not identical* (schema `salespm` ↔ model "Sales and Project Management"), so
resolution is a ladder ending in an interactive prompt whose answers persist to
`.lineage-cache.json` — second run asks nothing.

## 1. `cache.py`

```python
class CacheEntry(BaseModel):
    target: str | None     # node id, or None for external/skip
    method: str            # "interactive" | "external" | "skip"

class LineageCache:
    VERSION = 1
    @classmethod
    def load(cls, path: Path) -> "LineageCache": ...   # missing file -> empty cache
    def get(self, key: str) -> CacheEntry | None: ...
    def put(self, key: str, entry: CacheEntry) -> None: ...  # writes file IMMEDIATELY (crash-safe)
    def prune_invalid(self, graph: LineageGraph) -> list[str]: ...  # drop entries whose target id no longer exists; return dropped keys
```

File format (keys sorted on every write, `indent=2`, trailing newline — clean git diffs):
```json
{
  "version": 1,
  "mappings": {
    "pbi_table:sales and project management.dim_customer": {
      "target": "view:salespm.dim_customer",
      "method": "interactive"
    }
  }
}
```
Cache key = the pbi-side node id (already stable/normalized). Unknown `version` → warning,
treat as empty (do not delete the file).

## 2. `resolver.py`

```python
class ResolutionResult(BaseModel):
    resolved: int; unresolved: list[str]; methods: dict[str, int]  # counts per method

def link_graph(graph: LineageGraph, config: Config, cache: LineageCache,
               interactive: bool) -> tuple[ResolutionResult, list[ParseWarning]]: ...
```

For every `pbi_table` with a `partition_source` (and every visual with pending model
resolution), run the ladder — **stop at first hit**, record the method in the created edge's
`evidence` (e.g. `"linker: fuzzy 0.94"`):

1. **Cache** — `cache.get(node.id)`; method `external`/`skip` ⇒ mark
   `metadata["external_source"]/["skipped"]`, no edge.
2. **Exact** — `Node.make_id(VIEW, src.schema, src.object)` or `make_id(GOLD_TABLE, ...)` or
   `make_id(SILVER_TABLE, ...)` exists in graph.
3. **Config rule** — `schema_mappings` entries whose `model` matches this table's semantic
   model name (case-insensitive) give candidate schema(s); exact object-name match within them.
4. **Fuzzy** — candidates = all `view`/`gold_table`/`silver_table` node ids; score with
   `difflib.SequenceMatcher(None, f"{src.schema}.{src.object}", candidate_qualified_name)`.
   Best ≥ 0.92 → auto-accept (warning category `"fuzzy_auto"` so it's visible in the summary).
   Best in [0.6, 0.92) → ambiguous → step 5.
5. **Interactive** (only if `interactive=True`) — see below; the chosen answer is `cache.put`
   immediately, then applied.
6. **Non-interactive leftover** — `metadata["unresolved"] = True`, listed in
   `ResolutionResult.unresolved`.

A resolution creates `Edge(source_id=<sql node>, target_id=<pbi_table>, edge_type=FEEDS)`.
Process nodes in sorted-id order (determinism). Call `cache.prune_invalid(graph)` first.

## 3. `interactive.py` (the ONLY module besides cli.py allowed to touch the terminal)

```python
def prompt_resolution(pbi_node: Node, source: dict, candidates: list[tuple[str, float]]) -> CacheEntry: ...
def run_interactive_session(pending: list[...], cache: LineageCache) -> ...: ...
```

- Group pending items by semantic model; print a header per model
  (`"── Sales and Project Management — 3 unresolved tables ──"`).
- `questionary.select` per item: message shows the PBI table and its raw partition source;
  choices = top-10 candidates as `f"{id}   ({score:.2f})"`, then separator, then
  `"🌐 Mark as external source (not in these repos)"` and `"⏭  Skip for now"`.
  Map answers to `CacheEntry(target=id|None, method=...)`.
- Ctrl-C mid-session: previously answered items are already cached; re-raise KeyboardInterrupt
  for the CLI to exit cleanly with a notice.

## Tests (mock questionary; never prompt in CI)
Build a fake graph with: a view `salespm.dim_customer`, gold `dbo.fact_sales`, pbi tables whose
sources are (a) exact match, (b) config-rule match, (c) fuzzy 0.95 match (slightly different
name), (d) ambiguous 0.7 match, (e) garbage. Assert: ladder order honored, methods counted,
ambiguous prompts exactly once (questionary mocked via monkeypatch), cache file written after
each answer and re-running `link_graph` with the same cache prompts **zero** times, strict
non-interactive leaves (d)/(e) in `unresolved`, prune_invalid drops a stale entry and causes a
re-prompt, cache file byte-stable across rewrites.

## Acceptance criteria
- Second run after an interactive session is fully silent.
- `.lineage-cache.json` diffs are minimal and sorted.
- No terminal I/O anywhere except `interactive.py`.
