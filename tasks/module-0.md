# MODULE 0 — Core Graph Model ✅ IMPLEMENTED

> Status: **done** — `src/coop_data_doc/graph/model.py`, `graph/serialize.py`,
> `tests/test_graph.py` exist and pass. This file is kept as the interface reference brief;
> do not reimplement. Paste `model.py` source alongside `_shared-context.md` when handing off
> any other module.

Summary of what was built (see `_shared-context.md` for the full interface table):
- `NodeType` / `EdgeType` enums, `normalize_identifier`, `Node.make_id`.
- `Node` uses field `schema_name` (pydantic shadows `schema`).
- `Edge.flow()` normalizes authored direction → data-flow direction
  (reads/references/visualizes are reversed).
- `LineageGraph.add_node` merges on id conflict (existing scalars win unless empty; columns
  unioned case-insensitively; existing metadata keys win). `add_edge` dedupes on
  (source, target, type) and backfills empty evidence.
- `upstream`/`downstream`: cycle-safe BFS over flow-normalized adjacency, optional depth,
  sorted ids returned.
- `retype_node` rewrites the id and all referencing edges (used by the layer-assignment
  pass in `layering.assign_layers`), then re-adds edges through `add_edge` so the rewrite
  can't leave duplicate-keyed edges.
- `serialize.to_json_str`: edges sorted by key, `sort_keys=True`, indent 2, trailing newline —
  git-diffable and byte-stable.
