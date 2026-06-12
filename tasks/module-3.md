# MODULE 3 — Power BI Extractor (TMDL / BIM / M-code / DAX / PBIR / pbix) ✅ IMPLEMENTED

> Status: **done** — all six parsers, tests, and fixtures exist. Kept as the interface
> reference; do not reimplement.

**Files to create:** `src/coop_data_doc/parsers/tmdl.py`, `bim.py`, `mcode.py`, `dax.py`,
`pbir.py`, `pbix.py`; `tests/test_pbi_parsers.py`; fixtures under `tests/fixtures/repo_pbi/`.

**Inputs you can rely on:** `FileEntry`/`FileKind` from Module 1, `ParseWarning`, Module 0 graph.
**No third-party PBI libraries** — stdlib (`json`, `re`, `zipfile`) only.
All parsers are pure: `(content/paths, graph) -> list[ParseWarning]`.

Node-id conventions for PBI objects (no SQL-style schema):
- semantic model → `Node.make_id(SEMANTIC_MODEL, "", model_name)`
- pbi table → `make_id(PBI_TABLE, model_name, table_name)`
- measure → `make_id(MEASURE, model_name, measure_name)`
- report / page / visual → `make_id(REPORT, "", report_name)`,
  `make_id(REPORT_PAGE, report_name, page_name)`, `make_id(VISUAL, report_name, visual_id)`

## 1. `tmdl.py` — tolerant line parser (do NOT build a full grammar)

TMDL is indentation-scoped and line-oriented. A PBIP semantic model is the folder containing
`definition/` with `model.tmdl`, `database.tmdl`, and `tables/*.tmdl`. Model name = the folder
name minus `.SemanticModel` suffix.

Parse by tracking an object stack from header lines (indent level + first token):
- `table <name>` / `table '<name with spaces>'`
- `column <name>` … property lines below it: `dataType: <type>`, `isHidden`, `summarizeBy`
- `measure <name> = <DAX>` or `measure '<name>' =` followed by deeper-indented continuation
  lines (and triple-backtick blocks in newer TMDL) — capture the **full DAX text**
- `partition <name> = m` … `source` block contains the M expression (capture full text)
- `relationship <id>` blocks with `fromColumn:` / `toColumn:` properties

Emit: `semantic_model` node; `pbi_table` nodes with `Column` lists (TMDL dataType strings as-is);
`feeds` edge pbi_table→semantic_model; `measure` nodes with `metadata["dax"]` = expression and
`feeds` edge measure→semantic_model; relationships stored on the model node as
`metadata["relationships"] = [{"from": "t1.c1", "to": "t2.c2"}, ...]` (sorted).
For each partition, call `mcode.extract_source` (below) and stash the result on the table node:
`metadata["partition_source"] = {"schema": ..., "object": ..., "raw_kind": ...}` or
`metadata["partition_source_unresolved"] = True` + warning.

Be tolerant: unknown lines are skipped silently; never raise on malformed TMDL (warn instead,
category `"tmdl_parse"`).

## 2. `bim.py` — same outputs from `model.bim` JSON

`model.tables[]` → name, `columns[]` (`name`, `dataType`), `measures[]` (`name`, `expression` —
string OR list of strings: join with `\n`), `partitions[].source.expression` (same string/list
duality) → through `mcode`. `model.relationships[]` → same metadata shape as tmdl.

## 3. `mcode.py` — partition source extraction

```python
class SourceRef(BaseModel):
    schema_name: str; object_name: str; raw_kind: str  # "sql_database" | "native_query" | "lakehouse" | "fallback"

def extract_source(m_expression: str) -> tuple[SourceRef | None, list[str]]:
    """Returns (ref-or-None, list of raw SQL strings found in NativeQuery)."""
```

Ordered patterns (first hit wins; all case-sensitive M function names):
1. `Sql.Database("server","db")` followed by navigation `[Schema="s",Item="i"]` or
   `{[Schema="s",Item="i"]}` → SourceRef(s, i, "sql_database").
2. `Value.NativeQuery(<src>, "SELECT ...")` → capture the SQL string (unescape doubled `""`),
   return raw_kind "native_query" with the SQL attached — the caller (tmdl/bim) records it in
   `metadata["native_query_sql"]`; the orchestration layer later feeds it through Module 2's
   table extractor.
3. `Lakehouse.Contents()` / `Fabric.Warehouse` style navigation: find `[Id="..."]` /
   `{[Name="x"]}` chains; last `Name` = object, second-to-last (or `workspaceId` context) =
   schema-ish container → raw_kind "lakehouse", schema may be `""`.
4. Fallback: any `Schema\s*=\s*"([^"]+)"` and `Item\s*=\s*"([^"]+)"` anywhere → "fallback".
5. Nothing → `(None, [])`.

Strip M comments (`//` to EOL, `/* */`) before matching. Unit-test each pattern separately.

## 4. `dax.py` — measure dependency extraction (heuristic, regex)

```python
def extract_refs(dax: str) -> tuple[set[str], set[str]]:
    """Returns (bare_bracket_refs, table_refs) from a DAX expression."""
def link_measures(graph: LineageGraph, model_id: str) -> list[ParseWarning]: ...
```

- Pre-clean: remove DAX comments (`//`, `--`, `/* */`) and double-quoted string literals.
- `'Table Name'[Col]` or `TableName[Col]` (identifier immediately before `[`) → table ref
  (the table part).
- Bare `[Something]` NOT preceded by an identifier/`'`/`]` → candidate measure ref.
- `link_measures` runs after all measures of a model are in the graph: candidates matching a
  known measure name (case-insensitive) → `references` edge measure→measure; table refs matching
  a known pbi_table → `references` edge measure→pbi_table; unmatched candidates →
  `metadata["unmatched_dax_refs"]` on the measure (sorted list). Set
  `metadata["dax_refs_heuristic"] = True` on every measure.

## 5. `pbir.py` — reports

**PBIR folder format:** for each `FileKind.PBIR_VISUAL` entry
(`<Report>/definition/pages/<page>/visuals/<id>/visual.json`):
- Derive report name (folder minus `.Report`), page (from path or sibling `page.json`
  `displayName`), visual id; emit `report`, `report_page`, `visual` nodes
  (visual `metadata["visual_type"]` from `visual.visualType`; title if present in objects).
- Bindings: recursively search the JSON for dicts containing
  `{"Entity": ...}`/`{"entity": ...}` and `{"Property": ...}`/`{"property": ...}` pairs (covers
  `query.queryState.*.projections[].field` with `Column`/`Measure`/`Aggregation` wrappers and
  `prototypeQuery` shapes). Each (entity, property) → `visualizes` edge visual→pbi_table
  (entity) and, if the property matches a known measure name once models are loaded,
  visual→measure. When the semantic model is not yet known, emit the edge against the
  *constructed* pbi_table id using the report's model name if resolvable from
  `definition.pbir` (`datasetReference.byPath.path` → model folder name); otherwise record
  `metadata["pending_model_resolution"]` entries for Module 4.
- `feeds` edge: page→report, visual→page (authoring direction visual→page with FEEDS means
  data flows visual→page→report — acceptable for containment; document this).

**Legacy `report.json`:** `sections[]` = pages; `sections[].visualContainers[].config` is a JSON
**string** — `json.loads` it; read `singleVisual.visualType`, `singleVisual.projections`
(`{role: [{queryRef: "Table.Field"}]}` — split on first `.`) and/or
`singleVisual.prototypeQuery.Select[].Property` + `From[].Entity` (resolve `From` aliases).
Same node/edge outputs. Malformed config strings → warning `"report_json_parse"`, never raise.

## 6. `pbix.py` — best-effort binary extraction

```python
def parse_pbix(entry: FileEntry, graph: LineageGraph) -> list[ParseWarning]: ...
```
- `zipfile.ZipFile`; if not a zip → warning `"pbix_unreadable"`, return.
- Member `Report/Layout` → decode `utf-16-le` (strip BOM) → reuse the legacy report.json parser.
- Member `DataMashup` → bytes contain a nested zip: find first `PK\x03\x04` AFTER offset 4,
  open that slice as a zip, read `Formulas/Section1.m`; split into `shared <name> = <expr>;`
  sections; each section through `mcode.extract_source` → create pbi_table nodes
  (model name = pbix filename stem) with partition_source metadata.
- Member `DataModel` present but model not extractable → `semantic_model` node with
  `metadata["pbix_model_opaque"] = True` + warning `"pbix_opaque_model"` whose message advises:
  *"Open in Power BI Desktop and save as a .pbip project for full lineage."*
- Never raise on any malformed member.

## Fixtures & tests
Hand-author: a TMDL model with 3 tables / 4 measures including one measure referencing another
measure and a table (assert both `references` edges), partitions covering `Sql.Database`,
`Value.NativeQuery`, and an unresolvable one; a PBIR visual.json with one column + one measure
binding; a legacy report.json with embedded config string; a tiny .pbix built in the test itself
with `zipfile` (Layout UTF-16 + a synthetic DataMashup with nested zip). Golden node/edge-set
assertions per format; determinism check (parse twice → `to_json_str` equal).

## Acceptance criteria
- Measure-of-measure DAX edge correct; string literals/comments never produce refs.
- Every partition yields either a `partition_source` or an unresolved flag + warning — no guesses.
- pbix path degrades gracefully on garbage input (truncated zip test).
