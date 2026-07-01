# MODULE 2 — SQL AST Parser (T-SQL via sqlglot + regex fallback) ✅ IMPLEMENTED

> Status: **done** — `parsers/sql_common.py`, `sql_objects.py`, `sql_procs.py`, tests, and
> fixtures exist. Kept as the interface reference; do not reimplement.

**Files to create:** `src/coop_data_doc/parsers/sql_common.py`,
`src/coop_data_doc/parsers/sql_objects.py`, `src/coop_data_doc/parsers/sql_procs.py`,
`tests/test_sql_parsers.py`, additional `.sql` fixtures as needed under
`tests/fixtures/repo_sql/`.

**Inputs you can rely on:** `FileEntry` (kind == SQL_FILE) from Module 1; `ParseWarning` from
`config.py`; the Module 0 graph API.

## 1. `sql_common.py` — shared helpers

- `split_batches(sql_text: str) -> list[str]` — strip BOM, split on `^\s*GO\s*;?\s*$`
  (case-insensitive, multiline). Preserve original line numbers in a parallel structure if cheap.
- `parse_batch(batch: str, dialect: str) -> list[sqlglot.Expression]` — wrap
  `sqlglot.parse(batch, read=dialect, error_level=sqlglot.ErrorLevel.IGNORE)`; filter Nones;
  on exception return `[]` (caller falls back to regex). *(Later change: gained an
  `error_level` parameter — proc-body chunks parse with `ErrorLevel.RAISE` so
  semicolon-less multi-statement chunks fall back to regex instead of being
  silently mangled into one statement.)*
- `table_name_parts(expr: sqlglot.exp.Table) -> tuple[str, str]` — return (schema, name),
  bracket-stripped lowercase, schema defaults to `"dbo"` when absent.
- `collect_source_tables(expr) -> set[tuple[str, str]]` — every `exp.Table` under the
  expression, **minus**: CTE aliases (gather `exp.CTE` alias names first), temp tables
  (name starts `#`), table variables (`@`), and table-valued functions.
- `is_temp(name: str) -> bool`.

## 2. `sql_objects.py`

```python
def parse_sql_objects(entries: list[FileEntry], graph: LineageGraph,
                      dialect: str = "tsql") -> list[ParseWarning]: ...
```

Per batch, handle:

**`CREATE TABLE`** (`exp.Create` with kind TABLE):
- Emit `gold_table` node (schema from the qualified name, default `dbo`),
  `source_file` = entry.path.
- Columns from `exp.ColumnDef`: name, full data type rendered via `.sql(dialect)` (keeps
  precision e.g. `DECIMAL(18, 2)`), `nullable` from NOT NULL constraint absence, constraints
  list collecting `PRIMARY KEY`, `IDENTITY`, `DEFAULT <expr>`, `UNIQUE`, `FOREIGN KEY` (table
  level constraints too — attach to the named columns).
- **CTAS** (`CREATE TABLE ... AS SELECT`, Fabric/Synapse): additionally emit `reads` edges
  table→source for each `collect_source_tables` of the SELECT, and derive columns from explicit
  projection aliases; un-aliased/star ⇒ `metadata["columns_unresolved"] = True`.

**`CREATE [OR ALTER] VIEW`**:
- Emit `view` node. `reads` edges view→source for every source table.
- Output columns from the projection: explicit alias or bare column name; expression without
  alias ⇒ column name = rendered expression, `metadata` flag; `SELECT *` ⇒
  `metadata["columns_unresolved"] = True` plus warning category `"select_star_view"`.
- Evidence on each edge: `f"{entry.path}: FROM {schema}.{name}"`.

## 3. `sql_procs.py`

```python
def parse_sql_procs(entries: list[FileEntry], graph: LineageGraph,
                    dialect: str = "tsql") -> list[ParseWarning]: ...
```

**`CREATE [OR ALTER] PROCEDURE`**: emit `stored_proc` node (schema default `dbo`), then walk
every statement in the body:

| statement | edges |
|---|---|
| `INSERT INTO t ... SELECT ...` | `writes` proc→t; `reads` proc→each source table |
| `MERGE [INTO] t USING s ...` | `writes` proc→t; `reads` proc→s (and any tables inside USING subquery) |
| `UPDATE t ... FROM ...` | `writes` proc→t; `reads` proc→FROM/JOIN tables |
| `SELECT ... INTO t FROM ...` | `writes` proc→t; `reads` sources |
| `DELETE FROM t` / `TRUNCATE TABLE t` | `writes` proc→t (`metadata` note on edge evidence) |
| `CREATE TABLE t (...)` inside proc | `defines` proc→t and create the `gold_table` node with columns |
| `EXEC[UTE] schema.other_proc` | `references` proc→other_proc (create stub node if unseen) |

Exclusions on **both** sides: temp tables, table variables, CTE aliases.

**Robustness ladder** (T-SQL proc bodies often defeat sqlglot — TRY/CATCH, cursors, dynamic SQL):
1. Try parsing the whole batch.
2. If that fails or yields no Create-procedure node, locate the body after `AS BEGIN`/`AS` and
   split into candidate statements (split on `;` and on DML keywords at line starts), parse each
   individually.
3. Statements still unparseable go through documented regex fallbacks:
   `INSERT\s+INTO\s+([#@\w\[\].]+)`, `MERGE\s+(?:INTO\s+)?([...])`, `UPDATE\s+([...])`,
   `INTO\s+([...])\s`, `FROM\s+([...])`, `JOIN\s+([...])`, `TRUNCATE\s+TABLE\s+([...])`,
   `EXEC(?:UTE)?\s+([...])`. Mark the proc node `metadata["parse_quality"] = "regex_fallback"`
   and emit warning category `"regex_fallback"`.
4. Dynamic SQL (`EXEC(@sql)` / `sp_executesql`): do NOT guess; warning category `"dynamic_sql"`.

**Silver classification** is *not* done here — the SQL parsers leave every read table as a
`gold_table` stub. The live gold→silver retype is the heuristic in `layering.assign_layers`
pass 2 (run later in the pipeline, after rule-based layering): any `gold_table` that is never
the target of a `writes`/`defines` edge is treated as a read-only source and retyped via
`graph.retype_node(id, NodeType.SILVER_TABLE)`. See `module-4.5` / `layering.py`.

## Tests (fixture-driven, golden expectations)
Fixture .sql files must cover: MERGE proc with CTE + temp table (assert no edges for either),
CTAS, `SELECT ... INTO`, `UPDATE ... FROM`, a view with `SELECT *`, nested EXEC, a cursor-based
proc that defeats sqlglot (assert `regex_fallback` engaged and edges still found), dynamic SQL
(assert warning, no false edge). Final assertion: full fixture repo parse produces the exact
expected node-id/edge-key set; the gold→silver retype of read-only sources is asserted via
`layering.assign_layers` (the live mechanism), not a parser-local pass.

## Acceptance criteria
- No false edges from CTE names, temp tables, or table variables.
- Column list for `dbo.fact_sales` fixture matches the DDL exactly (types incl. precision).
- Deterministic: parsing the fixture repo twice yields equal graphs (`to_json_str` equality).
