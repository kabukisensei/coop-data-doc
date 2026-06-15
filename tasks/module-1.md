# MODULE 1 — Config Loader & Repo Crawler ✅ IMPLEMENTED

> Status: **done** — `config.py`, `crawler.py`, tests, and fixtures exist. Kept as the
> interface reference for downstream modules; do not reimplement.

**Files to create:** `src/coop_data_doc/config.py`, `src/coop_data_doc/crawler.py`,
`tests/test_config.py`, `tests/test_crawler.py`, fixture files under
`tests/fixtures/repo_sql/` and `tests/fixtures/repo_pbi/`.

## 1. `config.py`

Pydantic v2 models for the user-editable `coop-data-doc.yml`:

```yaml
project_name: Coop BI Estate
repos:
  sql:
    path: ../sql-repo
    include: ["**/*.sql"]
    exclude: ["**/archive/**"]
  powerbi:
    path: ../pbi-repo
    include: ["**/*.tmdl", "**/*.bim", "**/report.json", "**/visual.json", "**/*.pbix"]
    exclude: []
schema_mappings:            # view schema -> semantic model name hints (used by Module 4)
  - schema: sales
    model: "Sales Analytics"
output:
  dir: ./data-docs          # markdown output
  site_dir: ./data-docs-site  # mkdocs html output
sql_dialect: tsql
```

Requirements:
- Models: `RepoConfig(path, include, exclude)`, `SchemaMapping(schema, model)`,
  `OutputConfig(dir, site_dir)`, `Config(project_name, repos: dict[str, RepoConfig],
  schema_mappings, output, sql_dialect)`. Sensible defaults for everything except `repos`.
- `Config.load(path: Path) -> Config` — friendly errors: missing file, invalid YAML (show line),
  unknown keys (pydantic `extra="forbid"`, surface the bad key name), repo path that doesn't
  exist (error names the repo key and the resolved absolute path). Relative repo/output paths
  resolve relative to the **config file's directory**, not CWD.
- `Config.scaffold(path: Path) -> None` — writes a starter file containing the example above
  with explanatory `#` comments on every section (write it as a literal string, not yaml.dump,
  so comments survive). Raises `FileExistsError` if present.
- Define `ParseWarning(file: str, message: str, category: str)` here (or in a tiny
  `warnings.py`) — Modules 2–4 import it.

## 2. `crawler.py`

```python
class FileKind(str, Enum):
    SQL_FILE; TMDL; BIM; PBIR_VISUAL; PBIR_PAGE; REPORT_JSON_LEGACY; PBIX

class FileEntry(BaseModel):
    path: str        # POSIX-style, relative to the repo root
    abs_path: str
    repo_key: str    # "sql" / "powerbi"
    kind: FileKind
    size: int

class FileInventory(BaseModel):
    entries: list[FileEntry]   # sorted by (repo_key, path)
    def by_kind(self, kind: FileKind) -> list[FileEntry]: ...

def crawl(config: Config) -> tuple[FileInventory, list[ParseWarning]]: ...
```

Rules:
- stdlib only here: `pathlib.Path.rglob`/manual walk + `fnmatch` for include/exclude globs
  (match against the POSIX relative path). Exclude wins over include.
- Classification (after include filter): `.sql` → SQL_FILE; `.tmdl` → TMDL; `.bim` → BIM;
  path ending `definition/pages/*/visuals/*/visual.json` → PBIR_VISUAL; path ending
  `definition/pages/*/page.json` → PBIR_PAGE; basename `report.json` (not under a PBIR
  `definition/` tree) → REPORT_JSON_LEGACY; `.pbix` → PBIX. Unmatched included files → warning
  category `"unclassified_file"`.
- Skip files >10 MB (warning, category `"file_too_large"`) **except** `.pbix`.
- Never follow symlinks that resolve outside the repo root (warning `"symlink_escape"`).
- Always emit POSIX separators in `path` (use `Path.as_posix()`), so output is identical on
  Windows and macOS.

## 3. Fixtures (also used by Modules 2/3 — make them realistic)

`tests/fixtures/repo_sql/`:
- `procs/usp_load_fact_sales.sql` — proc with MERGE into `dbo.fact_sales` reading
  `silver.sales_orders` and `silver.customers`, a CTE, and a `#temp` table
- `tables/dbo.fact_sales.sql` — CREATE TABLE with 5+ typed columns, PK, NOT NULLs
- `views/sales/dim_customer.sql` — CREATE VIEW with a JOIN
- `archive/old_proc.sql` — must be excluded by the default exclude glob

`tests/fixtures/repo_pbi/`:
- `Sales.SemanticModel/definition/model.tmdl`, `.../definition/tables/dim_customer.tmdl`
  (table + columns + one measure + an M partition with `Sql.Database` source)
- `Sales.Report/definition/pages/page1/visuals/abc123/visual.json`
- `LegacyThing/report.json`

## Acceptance criteria
- `crawl()` on the fixtures returns a stable, sorted inventory; running twice yields equal models.
- `archive/old_proc.sql` excluded; each fixture file classified to the right `FileKind`.
- `Config.load` error messages name the offending key/path (assert message contents in tests).
- `Config.scaffold` output round-trips through `Config.load`.
